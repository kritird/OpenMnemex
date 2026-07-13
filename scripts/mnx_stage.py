"""mnx_stage.py — the capture staging tier (local, per-author, between session and graph).

Background: docs/staging-and-promotion.md, CAPTURE-PROMOTE-PLAN.md.

`mnx-capture` extracts durable atoms from the live session and STAGES them here — cheaply,
locally, with no lock and no graph mutation. `mnx-promote` later reconciles + merges the whole
staging batch into the shared graph and clears it. This helper owns the staging substrate:

  * one folder per graph, keyed by the graph slug, under
        <mnemex home>/staging/<graph-slug>/atoms/<provisional-id>.md
    (home resolved by mnx_common.mnemex_home(): ~/.claude/mnemex on existing installs, XDG on
    fresh ones; co-located with the read-stamp spill — see mnx_stamp). It lives OUTSIDE the graph clone so
    a remote clone's session-start hard-resync never destroys un-promoted captures, and it is
    NOT part of the shared graph (staging is per-author/local, never pushed).
  * provisional ids: a content hash (`stg-<sha1[:12]>`). They must NEVER enter the real graph's
    nodes or read stamps; promotion assigns the real slug id. A re-capture of identical content
    is idempotent (same hash → same file).
  * self-sufficient provenance: each atom serializes everything needed to reconcile COLD
    (artifact, the specific review ids, rejected alternatives, session ts, score, rationale),
    because the transcript is gone by promote time.
  * budgets (defaults below; tunable in the USER config, not the graph's mnemex.config.md —
    staging is per-author/local): a SOFT bound warns + nags; a HARD bound refuses to stage
    (backpressure) until a promote runs.

Strictly local: this script never clones, syncs, commits, or touches the graph clone. It only
reads the binding (via mnx_binding) to discover the per-graph staging folder.

Dependencies: Python 3.9+ stdlib + PyYAML only (via mnx_binding / mnx_common). See docs/06.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared in README
    yaml = None

import mnx_binding
import mnx_common

ATOMS_DIRNAME = "atoms"
HELD_DIRNAME = "held"     # per-atom held-contradictions queue (W9)
ID_PREFIX = "stg-"
VALID_SCORES = {"now", "later"}  # 'not-needed' is silently dropped, never staged
VALID_VOLATILITY = {"default", "timeless", "volatile"}  # + a positive int (day count); Freshness & Revalidation


def _norm_volatility(v: Any) -> Any:
    """Normalize an LLM-proposed volatility. Invalid → 'default' (freshness never blocks a capture)."""
    if isinstance(v, bool):
        return "default"
    if isinstance(v, int) and v > 0:
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in VALID_VOLATILITY:
            return s
        if s.isdigit() and int(s) > 0:
            return int(s)
    return "default"

# Budget defaults (numbers are tunable in the user config; staging is per-author/local).
STAGING_DEFAULTS: dict[str, Any] = {
    "staging_soft_count": 20,
    "staging_soft_age_days": 7,
    "staging_hard_count": 50,
    "staging_hard_age_days": 21,
    "staging_hard_bytes": 512 * 1024,
    "held_max_age_days": 14,   # a contradiction lingering past this nags (W9 held-queue bound)
    # Bulk profile (corpus ingest): a large cap meant to be DRAINED continuously by --bulk promote.
    # Labeled (ingest_batch) atoms are counted separately and never trip the per-session nag (DP8).
    "ingest_bulk_soft_atoms": 500,
    "ingest_bulk_hard_atoms": 5000,
}

# Corpus-provenance fields threaded onto an ingest-staged atom (docs/corpus-ingestion.md §3).
_CORPUS_PROV_KEYS = ("source_repo", "commit_sha", "source_path", "anchor", "kind", "ingest_batch")


# --- location ----------------------------------------------------------------

def _binding():
    b = mnx_binding.resolve()
    if b is None:
        raise RuntimeError("No Mnemex graph configured. Run /mnemex:mnx-init.")
    return b


def _atoms_dir(binding) -> Path:
    return mnx_binding.staging_path_for(binding) / ATOMS_DIRNAME


def _staging_cfg() -> dict[str, Any]:
    """Budget thresholds: defaults overlaid with any overrides in the USER config file.

    Deliberately NOT read from the graph's mnemex.config.md — staging is per-author/local."""
    cfg = dict(STAGING_DEFAULTS)
    user_cfg = mnx_binding.user_config_path()
    if user_cfg.is_file():
        try:
            fm = mnx_common.read_frontmatter(user_cfg)
            for k in STAGING_DEFAULTS:
                if k in fm:
                    cfg[k] = fm[k]
        except Exception:
            pass
    return cfg


# --- atom (de)serialization --------------------------------------------------

def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(";") if s.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def provisional_id(atom: dict[str, Any]) -> str:
    """Stable content hash for an atom. Identical content → identical id (idempotent capture)."""
    payload = json.dumps({
        "type": atom.get("type", "domain"),
        "summary": (atom.get("summary") or "").strip(),
        "body": (atom.get("body") or "").strip(),
        "aliases": sorted(_as_list(atom.get("aliases"))),
        "domain": sorted(_as_list(atom.get("domain"))),
        "trigger": (atom.get("trigger") or "").strip(),
    }, sort_keys=True, ensure_ascii=False)
    return ID_PREFIX + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _normalize(atom: dict[str, Any]) -> dict[str, Any]:
    atype = (atom.get("type") or "domain").strip()
    if atype not in ("domain", "pattern"):
        raise ValueError(f"invalid atom type: {atype!r} (domain|pattern)")
    score = (atom.get("score") or "later").strip()
    if score not in VALID_SCORES:
        raise ValueError(f"invalid score: {score!r} (now|later; 'not-needed' is dropped, never staged)")
    summary = (atom.get("summary") or "").strip()
    if not summary:
        raise ValueError("atom requires a non-empty summary")
    if atype == "pattern" and not (atom.get("trigger") or "").strip():
        raise ValueError("pattern atom requires a non-null trigger")
    prov = atom.get("provenance") or {}
    if not isinstance(prov, dict):
        raise ValueError("provenance must be a mapping")
    out = {
        "type": atype,
        "summary": summary,
        "aliases": _as_list(atom.get("aliases")),
        "domain": _as_list(atom.get("domain")),
        "score": score,
        "urgent": bool(atom.get("urgent", False)),
        "volatility": _norm_volatility(atom.get("volatility", "default")),
        "provenance": {
            "artifact": prov.get("artifact"),
            "reviews": _as_list(prov.get("reviews")),
            "rejected": _as_list(prov.get("rejected")),
            "session": prov.get("session") or mnx_common.now_utc(),
            "rationale": (prov.get("rationale") or "").strip(),
        },
        "body": (atom.get("body") or "").strip(),
    }
    if atype == "pattern":
        out["trigger"] = (atom.get("trigger") or "").strip()
    # Corpus provenance (ingest): thread the source-anchored fields through so a corpus atom is
    # promotable COLD. Accept them from provenance or the atom top level.
    batch = atom.get("ingest_batch") or prov.get("ingest_batch")
    for key in _CORPUS_PROV_KEYS:
        val = prov.get(key) if prov.get(key) is not None else atom.get(key)
        if val:
            out["provenance"][key] = val
    if batch:
        out["ingest_batch"] = str(batch)     # top-level label (partitions bulk from session atoms)
        out["bulk"] = True                   # DP8 isolation flag
        out["provenance"]["ingest_batch"] = str(batch)
    out["mentions"] = _hoist_mentions(out["body"], atom.get("mentions"))
    return out


def _hoist_mentions(body: str, explicit: Any = None) -> list[dict[str, Any]]:
    """Hoist the body's inline [[wiki-links]] into a mentions list (Link Reconciliation §2 — capture preserves
    links; promote resolves them). `resolved_id` is always null at capture. An explicit mentions
    list may carry an optional `type` per name (the rare typed link); it is merged by name."""
    types: dict[str, str] = {}
    for m in (explicit or []):
        if isinstance(m, dict) and m.get("name") and m.get("type"):
            types[m["name"].strip().lower()] = m["type"]
    out: list[dict[str, Any]] = []
    for wl in mnx_common.parse_wikilinks(body):
        entry = {"name": wl["name"], "resolved_id": None}
        t = types.get(wl["name"].strip().lower())
        if t:
            entry["type"] = t
        out.append(entry)
    return out


def _atom_path(binding, pid: str) -> Path:
    return _atoms_dir(binding) / f"{pid}.md"


def _serialize(atom: dict[str, Any], pid: str, staged_at: str) -> str:
    fm = {
        "provisional_id": pid,
        "type": atom["type"],
        "summary": atom["summary"],
        "aliases": atom["aliases"],
        "domain": atom["domain"],
        "score": atom["score"],
        "urgent": atom["urgent"],
        "volatility": atom.get("volatility", "default"),
        "provenance": atom["provenance"],
        "staged_at": staged_at,
    }
    if atom["type"] == "pattern":
        fm["trigger"] = atom["trigger"]
    if atom.get("ingest_batch"):
        fm["ingest_batch"] = atom["ingest_batch"]
        fm["bulk"] = True
    if atom.get("mentions"):
        fm["mentions"] = atom["mentions"]
    block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{block}\n---\n\n{atom['body']}\n"


def _load_atom(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    fm, body = mnx_common.split_frontmatter(text)
    fm["aliases"] = _as_list(fm.get("aliases"))
    fm["domain"] = _as_list(fm.get("domain"))
    fm["_body"] = body.strip()
    fm["_path"] = str(path)
    fm["_bytes"] = len(text.encode("utf-8"))
    return fm


def _all_atoms(binding) -> list[dict[str, Any]]:
    d = _atoms_dir(binding)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob(f"{ID_PREFIX}*.md")):
        try:
            out.append(_load_atom(p))
        except Exception:
            continue
    return out


# --- budget ------------------------------------------------------------------

def _oldest_age_days(atoms: list[dict[str, Any]], now: str) -> float:
    ages = []
    for a in atoms:
        try:
            ages.append(mnx_common.clamp_dt(str(a.get("staged_at")), now) / mnx_common.SECONDS_PER_DAY)
        except Exception:
            continue
    return max(ages) if ages else 0.0


def _verdict(count: int, oldest_age_days: float, total_bytes: int,
             cfg: dict[str, Any]) -> dict[str, Any]:
    """Classify the staging batch against the soft/hard budgets. Returns level + reasons."""
    hard_reasons, soft_reasons = [], []
    if count >= int(cfg["staging_hard_count"]):
        hard_reasons.append(f"{count} atoms ≥ hard cap {cfg['staging_hard_count']}")
    if oldest_age_days >= float(cfg["staging_hard_age_days"]):
        hard_reasons.append(f"oldest {oldest_age_days:.1f}d ≥ hard cap {cfg['staging_hard_age_days']}d")
    if total_bytes >= int(cfg["staging_hard_bytes"]):
        hard_reasons.append(f"{total_bytes}B ≥ hard cap {cfg['staging_hard_bytes']}B")
    if count >= int(cfg["staging_soft_count"]):
        soft_reasons.append(f"{count} atoms ≥ soft cap {cfg['staging_soft_count']}")
    if oldest_age_days >= float(cfg["staging_soft_age_days"]):
        soft_reasons.append(f"oldest {oldest_age_days:.1f}d ≥ soft cap {cfg['staging_soft_age_days']}d")
    level = "hard" if hard_reasons else "soft" if soft_reasons else "ok"
    return {"level": level, "hard_reasons": hard_reasons, "soft_reasons": soft_reasons}


def status(binding=None) -> dict[str, Any]:
    binding = binding or _binding()
    cfg = _staging_cfg()
    atoms = _all_atoms(binding)
    now = mnx_common.now_utc()
    count = len(atoms)
    total_bytes = sum(int(a.get("_bytes", 0)) for a in atoms)
    urgent = sum(1 for a in atoms if a.get("urgent"))
    # Label partition (DP8): the per-session budget is computed over SESSION atoms only, so an
    # in-flight bulk import never trips the episodic soft/hard nag. Bulk batches carry their own cap.
    session = [a for a in atoms if not a.get("ingest_batch")]
    by_label: dict[str, int] = {"_session": len(session)}
    for a in atoms:
        b = a.get("ingest_batch")
        if b:
            by_label[str(b)] = by_label.get(str(b), 0) + 1
    session_bytes = sum(int(a.get("_bytes", 0)) for a in session)
    verdict = _verdict(len(session), _oldest_age_days(session, now), session_bytes, cfg)
    return {
        "staging_root": binding.staging_root(),
        "count": count,
        "session_count": len(session),
        "urgent": urgent,
        "oldest_age_days": round(_oldest_age_days(session, now), 2),
        "total_bytes": total_bytes,
        "budget": verdict,
        "by_label": by_label,
        "held": held_status(binding),
        "thresholds": {k: cfg[k] for k in STAGING_DEFAULTS},
    }


# --- operations --------------------------------------------------------------

def add(atom: dict[str, Any]) -> dict[str, Any]:
    """Stage one atom. Idempotent by content hash. Refuses (backpressure) past the HARD cap."""
    binding = _binding()
    norm = _normalize(atom)
    pid = provisional_id(norm)
    path = _atom_path(binding, pid)

    # Hard-cap backpressure: refuse a NEW atom once the batch is over the hard bound. A
    # re-stage of already-present content is always allowed (it changes nothing).
    if not path.exists():
        st = status(binding)
        cfg = _staging_cfg()
        if norm.get("ingest_batch"):
            # Bulk atoms use the large bulk cap and are exempt from the per-session nag (DP8).
            batch_count = st["by_label"].get(norm["ingest_batch"], 0)
            if batch_count >= int(cfg["ingest_bulk_hard_atoms"]):
                return {"action": "refused", "reason": "ingest-bulk-hard-cap",
                        "provisional_id": pid, "ingest_batch": norm["ingest_batch"],
                        "message": (f"Ingest batch {norm['ingest_batch']} hit the bulk hard cap "
                                    f"({cfg['ingest_bulk_hard_atoms']}). Drain it with "
                                    f"/mnemex:mnx-promote --bulk before staging more."),
                        "status": st}
        elif st["budget"]["level"] == "hard":
            return {"action": "refused", "reason": "staging-hard-cap",
                    "provisional_id": pid, "budget": st["budget"],
                    "message": ("Staging is over its hard budget. Either run /mnemex:mnx-promote to "
                                "merge + drain it, or make room by discarding un-promoted captures "
                                "with /mnemex:mnx-capture --drop <id> (or --discard-all)."),
                    "status": st}

    path.parent.mkdir(parents=True, exist_ok=True)
    staged_at = mnx_common.now_utc()
    if path.exists():  # preserve original staged_at on idempotent re-capture
        try:
            staged_at = str(_load_atom(path).get("staged_at") or staged_at)
        except Exception:
            pass
    path.write_text(_serialize(norm, pid, staged_at), encoding="utf-8")
    st = status(binding)
    return {"action": "staged", "provisional_id": pid, "type": norm["type"],
            "score": norm["score"], "urgent": norm["urgent"], "path": str(path),
            "ingest_batch": norm.get("ingest_batch"), "bulk": bool(norm.get("ingest_batch")),
            "budget": st["budget"], "by_label": st["by_label"], "status": st}


def _label_match(a: dict[str, Any], label: Optional[str]) -> bool:
    """None → all atoms; '_session' → only unlabeled session atoms; else → that ingest batch."""
    if label is None:
        return True
    if label == "_session":
        return not a.get("ingest_batch")
    return str(a.get("ingest_batch") or "") == label


def list_atoms(binding=None, label: Optional[str] = None) -> dict[str, Any]:
    binding = binding or _binding()
    atoms = [a for a in _all_atoms(binding) if _label_match(a, label)]
    items = [{
        "provisional_id": a.get("provisional_id"),
        "type": a.get("type"),
        "summary": a.get("summary"),
        "aliases": a.get("aliases"),
        "domain": a.get("domain"),
        "score": a.get("score"),
        "urgent": bool(a.get("urgent")),
        "volatility": a.get("volatility", "default"),
        "trigger": a.get("trigger"),
        "ingest_batch": a.get("ingest_batch"),
        "mentions": a.get("mentions", []),
        "staged_at": a.get("staged_at"),
        "bytes": a.get("_bytes"),
        # Provenance must survive the projection: glean coverage keys on provenance.anchor and
        # promote reconciles COLD from it (E2E 2026-07-12 finding G6 — same projection-drop
        # class as the fixed volatility bug F5).
        "provenance": a.get("provenance", {}),
    } for a in sorted(atoms, key=lambda x: str(x.get("staged_at")), reverse=True)]
    return {"staging_root": binding.staging_root(), "count": len(items),
            "filtered_by": label, "atoms": items}


def overlay(domains: Optional[list[str]] = None, binding=None,
            label: Optional[str] = None) -> dict[str, Any]:
    """Staged atoms relevant to a read's routed cluster(s), NEWEST-FIRST (newest-wins).

    Filters by `domain` overlap when domains are given; otherwise returns the whole batch. `label`
    scopes to one ingest batch (or '_session'). The caller (mnx-read) marks results
    `staged/unpromoted`, flags contradictions against the graph, never body-merges, and never
    stamps — this helper only surfaces candidates + bodies."""
    binding = binding or _binding()
    want = {d.strip().lower() for d in (domains or []) if d.strip()}
    atoms = sorted(_all_atoms(binding), key=lambda x: str(x.get("staged_at")), reverse=True)
    out = []
    for a in atoms:
        if not _label_match(a, label):
            continue
        adoms = {d.lower() for d in a.get("domain", [])}
        if want and not (want & adoms):
            continue
        out.append({
            "provisional_id": a.get("provisional_id"),
            "type": a.get("type"),
            "summary": a.get("summary"),
            "aliases": a.get("aliases"),
            "domain": a.get("domain"),
            "score": a.get("score"),
            "urgent": bool(a.get("urgent")),
            "volatility": a.get("volatility", "default"),
            "trigger": a.get("trigger"),
            "ingest_batch": a.get("ingest_batch"),
            "mentions": a.get("mentions", []),
            "staged_at": a.get("staged_at"),
            "provenance": a.get("provenance"),
            "body": a.get("_body"),
            "state": "staged/unpromoted",
        })
    return {"staging_root": binding.staging_root(), "filtered_by": sorted(want),
            "count": len(out), "atoms": out}


def clear(binding=None, label: Optional[str] = None) -> dict[str, Any]:
    """Remove staged atoms. `label` scopes the drain to one ingest batch (DP8 — never touches the
    session atoms or other batches); no label removes ALL (the terminal step of a successful
    session promote). Leaves the stamp spill untouched — a different file in the same folder."""
    binding = binding or _binding()
    d = _atoms_dir(binding)
    removed = 0
    if d.is_dir():
        for p in d.glob(f"{ID_PREFIX}*.md"):
            try:
                if label is not None and not _label_match(_load_atom(p), label):
                    continue
                p.unlink()
                removed += 1
            except Exception:
                continue
    return {"action": "cleared", "removed": removed, "filtered_by": label,
            "staging_root": binding.staging_root()}


def clear_one(pid: str, binding=None) -> dict[str, Any]:
    binding = binding or _binding()
    path = _atom_path(binding, pid)
    if not path.exists():
        return {"action": "noop", "provisional_id": pid, "reason": "not-found"}
    path.unlink()
    return {"action": "cleared-one", "provisional_id": pid}


def clear_merged(pids: list[str], binding=None) -> dict[str, Any]:
    """Per-atom terminal disposition (W9): clear ONLY the atoms that reached a terminal merge
    disposition this cycle (created / merged / dropped-dup / superseded), leaving held atoms in
    place. This replaces the all-or-nothing `clear()` for the per-atom promote path: a single
    contradiction no longer forces aborting (and re-doing) the clean atoms."""
    binding = binding or _binding()
    removed, missing = [], []
    for pid in pids:
        p = _atom_path(binding, pid)
        if p.exists():
            p.unlink()
            removed.append(pid)
        else:
            missing.append(pid)
    return {"action": "cleared-merged", "removed": removed, "missing": missing,
            "remaining": len(_all_atoms(binding)), "held": held_status(binding)["count"]}


# --- held-contradictions queue (W9) -----------------------------------------
# When reconcile flags an atom as contradicting a graph node, the OLD model aborted the whole
# batch (resolve-all-or-abort) — at scale one contentious atom starves a growing batch (a liveness
# bug). Instead: merge the clean atoms per-atom (clear_merged), and move only the contradicting
# atom to a held queue for HITL. The held atom keeps its self-sufficient provenance, so it can be
# re-promoted COLD once the human resolves the contradiction — no lingering "in-flight" state on
# the graph side; the held state lives entirely in the local staging tier.

def _held_dir(binding) -> Path:
    return mnx_binding.staging_path_for(binding) / HELD_DIRNAME


def _held_meta(binding, pid: str) -> Path:
    return _held_dir(binding) / f"{pid}.hold.json"


def hold(pid: str, reason: str, contradicts: Optional[str] = None, binding=None) -> dict[str, Any]:
    """Move a staged atom into the held-contradictions queue with a reason (and the graph id it
    contradicts). Returns the disposition. Idempotent if already held."""
    binding = binding or _binding()
    src = _atom_path(binding, pid)
    dst = _held_dir(binding) / f"{pid}.md"
    if not src.exists():
        if dst.exists():
            return {"action": "noop", "provisional_id": pid, "reason": "already-held"}
        return {"action": "noop", "provisional_id": pid, "reason": "not-found"}
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)
    _held_meta(binding, pid).write_text(json.dumps({
        "provisional_id": pid, "reason": reason, "contradicts": contradicts,
        "held_at": mnx_common.now_utc()}, indent=2), encoding="utf-8")
    return {"action": "held", "provisional_id": pid, "reason": reason, "contradicts": contradicts}


def release_held(pid: str, binding=None) -> dict[str, Any]:
    """Return a held atom to the active staging queue (the human resolved the contradiction; it
    will be re-reconciled on the next promote)."""
    binding = binding or _binding()
    src = _held_dir(binding) / f"{pid}.md"
    if not src.exists():
        return {"action": "noop", "provisional_id": pid, "reason": "not-held"}
    src.replace(_atom_path(binding, pid))
    try:
        _held_meta(binding, pid).unlink()
    except FileNotFoundError:
        pass
    return {"action": "released", "provisional_id": pid}


def drop_held(pid: str, binding=None) -> dict[str, Any]:
    """Discard a held atom outright (the contradiction was resolved in the graph's favour)."""
    binding = binding or _binding()
    gone = False
    for p in (_held_dir(binding) / f"{pid}.md", _held_meta(binding, pid)):
        if p.exists():
            p.unlink()
            gone = True
    return {"action": "dropped-held" if gone else "noop", "provisional_id": pid}


def held_status(binding=None) -> dict[str, Any]:
    """Summary of the held queue: count, items (reason + age), and the lingering-bound nag."""
    binding = binding or _binding()
    d = _held_dir(binding)
    cfg = _staging_cfg()
    now = mnx_common.now_utc()
    items = []
    if d.is_dir():
        for meta in sorted(d.glob(f"{ID_PREFIX}*.hold.json")):
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                continue
            age = mnx_common.clamp_dt(str(m.get("held_at")), now) / mnx_common.SECONDS_PER_DAY
            items.append({"provisional_id": m.get("provisional_id"), "reason": m.get("reason"),
                          "contradicts": m.get("contradicts"), "held_at": m.get("held_at"),
                          "age_days": round(age, 2)})
    max_age = max((i["age_days"] for i in items), default=0.0)
    nag = max_age >= float(cfg["held_max_age_days"])
    return {"count": len(items), "items": items, "oldest_age_days": round(max_age, 2),
            "lingering_nag": nag, "held_max_age_days": cfg["held_max_age_days"]}


# --- cli --------------------------------------------------------------------

def _arg(argv: list[str], flag: str) -> Optional[str]:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def _atom_from_argv(argv: list[str]) -> dict[str, Any]:
    return {
        "type": _arg(argv, "--type") or "domain",
        "summary": _arg(argv, "--summary"),
        "aliases": _arg(argv, "--aliases"),
        "domain": _arg(argv, "--domain"),
        "trigger": _arg(argv, "--trigger"),
        "score": _arg(argv, "--score") or "later",
        "urgent": "--urgent" in argv,
        "volatility": _arg(argv, "--volatility") or "default",
        "body": _arg(argv, "--body") or "",
        "ingest_batch": _arg(argv, "--ingest-batch"),
        "source_repo": _arg(argv, "--source-repo"),
        "commit_sha": _arg(argv, "--commit-sha"),
        "source_path": _arg(argv, "--source-path"),
        "anchor": _arg(argv, "--anchor"),
        "kind": _arg(argv, "--kind"),
        "provenance": {
            "artifact": _arg(argv, "--artifact"),
            "reviews": _arg(argv, "--reviews"),
            "rejected": _arg(argv, "--rejected"),
            "rationale": _arg(argv, "--rationale"),
            "session": _arg(argv, "--session"),
        },
    }


_USAGE = [
    'mnx_stage.py add --json < atom.json   (or --type t --summary s --aliases a;b --domain d [--trigger t] [--score now|later] [--urgent] [--volatility v] [--body b] [--ingest-batch l] [--source-repo r --commit-sha c --source-path p --anchor a --kind k] [--artifact|--reviews|--rejected|--rationale|--session ...])  — stage one atom (idempotent by content hash)',
    "mnx_stage.py list [--ingest-batch <label>]        — staged atoms (label '_session' = unlabeled only)",
    'mnx_stage.py status | size-check                  — staging budget + counts',
    'mnx_stage.py overlay [--domain a,b] [--ingest-batch <label>]  — read-overlay projection',
    'mnx_stage.py clear [--ingest-batch <label>]       — drop staged atoms (a drained batch)',
    'mnx_stage.py clear-one --id <pid>                 — drop one staged atom',
    'mnx_stage.py clear-merged --ids <pid,pid,...>     — drop atoms merged by a promote',
    'mnx_stage.py hold --id <pid> [--reason r] [--contradicts <node-id>]  — park a contradicting atom',
    'mnx_stage.py held-list | release-held --id <pid> | drop-held --id <pid>',
]
_FLAGS = {"--json": False, "--urgent": False, "--type": True, "--summary": True, "--aliases": True, "--domain": True, "--trigger": True, "--score": True, "--volatility": True, "--body": True, "--ingest-batch": True, "--source-repo": True, "--commit-sha": True, "--source-path": True, "--anchor": True, "--kind": True, "--artifact": True, "--reviews": True, "--rejected": True, "--rationale": True, "--session": True, "--id": True, "--ids": True, "--reason": True, "--contradicts": True}


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE, _FLAGS)
    if handled is not None:
        return handled
    if yaml is None:
        return mnx_common.emit({"error": "PyYAML is required (pip install pyyaml)."}, ok=False)
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "add":
            if "--json" in argv:
                atom = json.loads(sys.stdin.read() or "{}")
            else:
                atom = _atom_from_argv(argv)
            res = add(atom)
            return mnx_common.emit(res, ok=res.get("action") != "refused")
        if cmd == "list":
            return mnx_common.emit(list_atoms(label=_arg(argv, "--ingest-batch")))
        if cmd in ("status", "size-check"):
            return mnx_common.emit(status())
        if cmd == "overlay":
            doms = _as_list(_arg(argv, "--domain"))
            return mnx_common.emit(overlay(doms, label=_arg(argv, "--ingest-batch")))
        if cmd == "clear":
            return mnx_common.emit(clear(label=_arg(argv, "--ingest-batch")))
        if cmd == "clear-one":
            pid = _arg(argv, "--id")
            if not pid:
                return mnx_common.emit({"error": "clear-one needs --id <provisional-id>"}, ok=False)
            return mnx_common.emit(clear_one(pid))
        if cmd == "clear-merged":
            ids = [s for s in (_arg(argv, "--ids") or "").split(",") if s]
            return mnx_common.emit(clear_merged(ids))
        if cmd == "hold":
            pid = _arg(argv, "--id")
            if not pid:
                return mnx_common.emit({"error": "hold needs --id <provisional-id>"}, ok=False)
            return mnx_common.emit(hold(pid, _arg(argv, "--reason") or "contradiction",
                                        _arg(argv, "--contradicts")))
        if cmd == "held-list":
            return mnx_common.emit(held_status())
        if cmd == "release-held":
            return mnx_common.emit(release_held(_arg(argv, "--id")))
        if cmd == "drop-held":
            return mnx_common.emit(drop_held(_arg(argv, "--id")))
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
