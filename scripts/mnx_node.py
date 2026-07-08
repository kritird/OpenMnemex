"""mnx_node.py — the deterministic node writer (promote/consolidate truth-writes).

Background: docs/script-contracts.md, docs/staging-and-promotion.md, docs/maintenance-pass-algorithm.md.

Node persistence is the ONE deterministic write in the write cycle that used to be hand-authored by the
LLM filling templates/node.template.md. That freehand path could mint an inconsistent id, stamp a
timestamp from somewhere other than `now_utc()`, or drift the front-matter shape — violating the stated
invariants that `now_utc()` is the only clock and `mnx_common.slugify` is the only id source. This helper
is the missing peer to the other write scripts (`mnx_stage` writes staged atoms, `mnx_mesh` writes edges,
`mnx_index` writes indexes): it executes an ALREADY-DECIDED disposition (the reconcile judgment stays in
the skill / sub-agent) as a mechanical, invariant-preserving file write.

Operations (Python API + CLI), each returning the shared JSON + STATUS contract:

  * create(cluster, fields)                 — CREATE a new node (mint unique slug; created=updated=verified=now)
  * merge(id, cluster, changes, meaning_change) — MERGE/UPDATE in place (keep id+created; verified=now; updated only on meaning-change)
  * supersede(old_id, cluster, new_fields)  — SUPERSEDE: create the replacement, retire the old (dead + superseded-by + died)
  * resurrect(id, cluster)                  — revive a dead node (dead->active; verified=now; clear died/superseded-by)
  * tombstone(id, cluster)                  — consolidate death (dead + died; keep body; refuses a timeless node)
  * revalidate(id, cluster, ts)             — consolidate freshness (verified = max(current, ts); monotonic)

Invariants (docs/script-contracts.md): timestamps come only from now_utc() (revalidate takes an external
confirmation ts but never regresses `verified`); ids come only from slugify + a uniqueness suffix; a `stg-`
provisional id is never accepted; merge/tombstone/revalidate preserve the body verbatim; a dead node keeps
its body (never hollowed); `verified` is monotonic and never precedes `created`. These make the doctor's
freshness invariants (9b, 9d) hold BY CONSTRUCTION.

Called under the team lock in the promote/consolidate flow (the skill holds it — same expectation as
mnx_mesh / mnx_index; this writer does not take the lock itself).

Dependencies: Python 3.9+ stdlib + PyYAML. Imports mnx_common.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared in README
    yaml = None

import mnx_common

VALID_TYPES = {"domain", "pattern"}
VALID_CONFIDENCE = {"high", "medium", "low"}
_BASE_VOLATILITY = {"default", "timeless", "volatile"}
STG_PREFIX = "stg-"

# Canonical front-matter key order for a CREATE (mirrors templates/node.template.md).
_FIELD_ORDER = ["id", "type", "title", "summary", "aliases", "domain", "status", "confidence",
                "volatility", "trigger", "mentions", "edges", "references", "provenance",
                "created", "updated", "verified"]


# --- validation -------------------------------------------------------------

def _valid_volatility(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return v > 0
    s = str(v).strip().lower()
    if s in _BASE_VOLATILITY:
        return True
    try:
        return int(s) > 0
    except (TypeError, ValueError):
        return False


def _norm_volatility(v: Any) -> Any:
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _BASE_VOLATILITY:
        return s
    try:
        return int(s)
    except (TypeError, ValueError):
        return "default"


# --- graph / id helpers -----------------------------------------------------

def _graph_root(cluster: str | Path) -> Path:
    return mnx_common.require_graph_root(cluster)


def _existing_ids(cluster: str | Path) -> set[str]:
    """Every node id currently on disk graph-wide. Ids equal the filename stem (inv 7), so we can
    read them without parsing every file."""
    root = _graph_root(cluster)
    ids: set[str] = set()
    for c in mnx_common.iter_clusters(root):
        for nf in mnx_common.iter_node_files(c):
            ids.add(nf.stem)
    return ids


def _unique_slug(title: str, existing: set[str]) -> str:
    base = mnx_common.slugify(title)
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _node_path(cluster: str | Path, nid: str) -> Path:
    return Path(cluster) / f"{nid}.md"


def _load(cluster: str | Path, nid: str) -> tuple[dict[str, Any], str, Path]:
    path = _node_path(cluster, nid)
    if not path.is_file():
        raise FileNotFoundError(f"node not found: {path}")
    fm, body = mnx_common.split_frontmatter(path.read_text(encoding="utf-8"))
    if not fm:
        raise ValueError(f"{path}: missing or malformed front-matter")
    return fm, body, path


def _dump(path: Path, fm: dict[str, Any], body: str) -> None:
    """Re-serialize front-matter (insertion order preserved) + body verbatim."""
    block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False).strip()
    path.write_text(f"---\n{block}\n---{body}", encoding="utf-8")


# --- operations -------------------------------------------------------------

def create(cluster: str | Path, fields: dict[str, Any]) -> dict[str, Any]:
    """Write a brand-new node. Mints a unique slug from the title; stamps created=updated=verified=now.
    Rejects a provisional stg- id (it must never enter the graph)."""
    ntype = (fields.get("type") or "domain").strip().lower()
    if ntype not in VALID_TYPES:
        raise ValueError(f"invalid type: {ntype!r} (domain | pattern)")
    title = (fields.get("title") or "").strip()
    if not title:
        raise ValueError("create requires a non-empty title (the id is minted from it)")
    if str(fields.get("id", "")).startswith(STG_PREFIX):
        raise ValueError("refusing a provisional stg- id — the graph mints a real slug")
    trigger = fields.get("trigger")
    if ntype == "pattern" and not (trigger and str(trigger).strip()):
        raise ValueError("a pattern node requires a non-null trigger")
    vol = fields.get("volatility", "default")
    if not _valid_volatility(vol):
        raise ValueError(f"invalid volatility: {vol!r} (default | timeless | volatile | positive int)")
    conf = (fields.get("confidence") or "high").strip().lower()
    if conf not in VALID_CONFIDENCE:
        raise ValueError(f"invalid confidence: {conf!r} (high | medium | low)")

    nid = _unique_slug(title, _existing_ids(cluster))
    now = mnx_common.now_utc()
    prov = dict(fields.get("provenance") or {})
    prov.setdefault("artifact", "")
    prov.setdefault("reviews", [])
    prov.setdefault("session", now)

    fm: dict[str, Any] = {
        "id": nid,
        "type": ntype,
        "title": title,
        "summary": fields.get("summary") or "",
        "aliases": list(fields.get("aliases") or []),
        "domain": list(fields.get("domain") or []),
        "status": "active",
        "confidence": conf,
        "volatility": _norm_volatility(vol),
        "trigger": trigger if ntype == "pattern" else None,
        "mentions": list(fields.get("mentions") or []),
        "edges": list(fields.get("edges") or []),
        "references": list(fields.get("references") or []),
        "provenance": prov,
        "created": now,
        "updated": now,
        "verified": now,
    }
    fm = {k: fm[k] for k in _FIELD_ORDER if k in fm}
    body = fields.get("body") or ""
    body = "\n\n" + body.lstrip("\n").rstrip() + "\n"
    path = _node_path(cluster, nid)
    _dump(path, fm, body)
    return {"action": "created", "id": nid, "path": str(path)}


def merge(id: str, cluster: str | Path, changes: dict[str, Any],
          meaning_change: bool = False) -> dict[str, Any]:
    """Fold new knowledge into an existing node in place. Keeps id + created. verified=now always;
    updated=now ONLY on a meaning-change (a use/confirm is not a meaning change). Body preserved
    verbatim unless `changes` supplies a new body."""
    fm, body, path = _load(cluster, id)
    now = mnx_common.now_utc()
    for key in ("summary", "aliases", "domain", "confidence", "references", "mentions", "edges"):
        if key in changes and changes[key] is not None:
            fm[key] = changes[key]
    if "volatility" in changes and changes["volatility"] is not None:
        if not _valid_volatility(changes["volatility"]):
            raise ValueError(f"invalid volatility: {changes['volatility']!r}")
        fm["volatility"] = _norm_volatility(changes["volatility"])
    if fm.get("type") == "pattern" and "trigger" in changes:
        if not (changes["trigger"] and str(changes["trigger"]).strip()):
            raise ValueError("a pattern node requires a non-null trigger")
        fm["trigger"] = changes["trigger"]
    fm["verified"] = now
    if meaning_change:
        fm["updated"] = now
    if changes.get("body") is not None:
        body = "\n\n" + str(changes["body"]).lstrip("\n").rstrip() + "\n"
    _dump(path, fm, body)
    return {"action": "merged", "id": id, "path": str(path),
            "meaning_change": bool(meaning_change)}


def supersede(old_id: str, cluster: str | Path, new_fields: dict[str, Any]) -> dict[str, Any]:
    """Tombstone-with-successor: create the replacement node, then retire the old one (status dead,
    superseded-by=<new-id>, died=now, body kept). Referrer repoint stays with the caller (mnx_mesh /
    mnx_resolve) — this writer only sets the fields on the two endpoints."""
    created = create(cluster, new_fields)
    new_id = created["id"]
    fm, body, path = _load(cluster, old_id)
    now = mnx_common.now_utc()
    fm["status"] = "dead"
    fm["superseded-by"] = new_id
    fm["died"] = now
    _dump(path, fm, body)
    return {"action": "superseded", "old_id": old_id, "new_id": new_id,
            "old_path": str(path), "new_path": created["path"]}


def resurrect(id: str, cluster: str | Path) -> dict[str, Any]:
    """Revive a dead node: status dead->active; verified=now; clear died + superseded-by."""
    fm, body, path = _load(cluster, id)
    now = mnx_common.now_utc()
    fm["status"] = "active"
    fm.pop("died", None)
    fm.pop("superseded-by", None)
    fm["verified"] = now
    _dump(path, fm, body)
    return {"action": "resurrected", "id": id, "path": str(path)}


def tombstone(id: str, cluster: str | Path) -> dict[str, Any]:
    """Consolidate death: status dead + died=now, KEEP body + id + front-matter (never hollow), no
    successor. Refuses a `volatility: timeless` node — it must never be auto-tombstoned (inv 9d;
    Freshness & Revalidation §7); such a node can leave only via an explicit human SUPERSEDE."""
    fm, body, path = _load(cluster, id)
    if str(fm.get("volatility", "default")).strip().lower() == "timeless":
        raise ValueError(f"refusing to tombstone timeless node {id!r} "
                         "(timeless is never auto-tombstoned — use supersede)")
    now = mnx_common.now_utc()
    fm["status"] = "dead"
    fm["died"] = now
    _dump(path, fm, body)
    return {"action": "tombstoned", "id": id, "path": str(path)}


def revalidate(id: str, cluster: str | Path, ts: str) -> dict[str, Any]:
    """Advance freshness (consolidate step 3b): verified = max(current verified, ts) — monotonic, never
    regresses. Backfills a missing verified from updated first. Leaves updated + strength untouched."""
    if not mnx_common.is_iso_utc(ts):
        raise ValueError(f"revalidate ts is not UTC ISO-8601: {ts!r}")
    fm, body, path = _load(cluster, id)
    current = fm.get("verified") or fm.get("updated")
    if current and mnx_common.parse_ts(str(current)) >= mnx_common.parse_ts(ts):
        new_verified = mnx_common.canon_ts(current)
        regressed = True
    else:
        new_verified = ts
        regressed = False
    fm["verified"] = new_verified
    _dump(path, fm, body)
    return {"action": "revalidated", "id": id, "path": str(path),
            "verified": new_verified, "no_op": regressed}


# --- cli --------------------------------------------------------------------

def _arg(argv: list[str], flag: str) -> Optional[str]:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def _json_stdin(argv: list[str]) -> dict[str, Any]:
    if "--json" in argv:
        return json.loads(sys.stdin.read() or "{}")
    jf = _arg(argv, "--json-file")
    if jf:
        return json.loads(Path(jf).read_text(encoding="utf-8"))
    raise ValueError("this subcommand needs node fields via --json (stdin) or --json-file <path>")


def _main(argv: list[str]) -> int:
    if yaml is None:
        return mnx_common.emit({"error": "PyYAML is required (pip install pyyaml)."}, ok=False)
    cmd = argv[1] if len(argv) > 1 else ""
    cluster = _arg(argv, "--cluster")
    try:
        if cmd == "create":
            if not cluster:
                return mnx_common.emit({"error": "create needs --cluster <dir>"}, ok=False)
            return mnx_common.emit(create(cluster, _json_stdin(argv)))
        if cmd == "merge":
            nid = _arg(argv, "--id")
            if not (nid and cluster):
                return mnx_common.emit({"error": "merge needs --id and --cluster"}, ok=False)
            return mnx_common.emit(merge(nid, cluster, _json_stdin(argv),
                                         meaning_change="--meaning-change" in argv))
        if cmd == "supersede":
            old = _arg(argv, "--old-id")
            if not (old and cluster):
                return mnx_common.emit({"error": "supersede needs --old-id and --cluster"}, ok=False)
            return mnx_common.emit(supersede(old, cluster, _json_stdin(argv)))
        if cmd == "resurrect":
            nid = _arg(argv, "--id")
            if not (nid and cluster):
                return mnx_common.emit({"error": "resurrect needs --id and --cluster"}, ok=False)
            return mnx_common.emit(resurrect(nid, cluster))
        if cmd == "tombstone":
            nid = _arg(argv, "--id")
            if not (nid and cluster):
                return mnx_common.emit({"error": "tombstone needs --id and --cluster"}, ok=False)
            return mnx_common.emit(tombstone(nid, cluster))
        if cmd == "revalidate":
            nid = _arg(argv, "--id")
            ts = _arg(argv, "--ts")
            if not (nid and cluster and ts):
                return mnx_common.emit({"error": "revalidate needs --id, --cluster, --ts"}, ok=False)
            return mnx_common.emit(revalidate(nid, cluster, ts))
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
