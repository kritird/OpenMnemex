"""mnx_read.py — the deterministic read frontier (multi-agent plan v2 §6.1, Phase 1 commit 1c).

Extracts the MECHANICAL half of the mnx-read judgment procedure into one importable module,
shared by the mnx-read SKILL, the MCP `read_*` tools (`mnx_mcp.py`), and any future surface.
No scoring, no routing choice, no stamping happens here — those stay host/model judgment
(which team/cluster to route to, which tier to stop at, which node bodies to actually load)
or live in a separate tool (`record_usage` wraps `mnx_stamp.append` directly, unchanged).

Three helpers, one per SKILL step:

  * `frontier(graph_root, now)` — org head + team heads (descriptions + child cluster
    descriptions only, never tier rows) + the graph-wide consolidation-overdue warning
    (`mnx_compact.overdue`) + an `empty` flag (no team has any cluster yet). Replaces the
    SKILL's own raw `Read` calls on index.md heads.
  * `fill_offer(empty, staged_count)` — onboarding plan Phase 3: the empty-graph fork
    message, composed from `frontier()`'s `empty` flag and a staged-atom count the caller
    supplies (`mnx_stage.status()["count"]`) — `frontier()` itself stays binding-free.
  * `scan(cluster, now, tiers, binding)` — a cluster's tier tables (hot/warm/cold), each row
    carrying a `stale` flag computed from the already-materialized `stale_after` column
    (Freshness & Revalidation), PLUS the staged-capture overlay for that cluster's domain
    (`mnx_stage.overlay`, newest-first, marked `staged/unpromoted`).
  * `expand(ids, graph_root, max_bytes)` — resolves ids to node bodies (`mnx_resolve` +
    `mnx_common.parse_node`) and pulls each node's `governed-by` pattern companions,
    budget-capped. Refuses `stg-` ids (those bodies already came from `scan`'s overlay).

`now` is an explicit required parameter (not read internally), matching the
`mnx_compact.overdue` / `mnx_decay.score` convention elsewhere in the engine: callers
(CLI dispatch, `mnx_mcp` tools) supply `mnx_common.now_utc()`, so the comparison logic
stays deterministic and testable without monkeypatching the clock.

Dependencies: Python 3.10+ stdlib + PyYAML only (via mnx_common/mnx_config/mnx_compact/
mnx_index/mnx_resolve/mnx_stage). See docs/script-contracts.md.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import mnx_common
import mnx_compact
import mnx_config
import mnx_index
import mnx_resolve
import mnx_stage

# --- frontier (SKILL steps 1+2: overdue check + chunk-1 routing) -------------------


def _child_name(entry: str) -> str:
    """A Children-section line is 'name/ — one-line description'; take the folder name."""
    return entry.split("/", 1)[0].strip()


def frontier(graph_root: str | Path, now: str) -> dict[str, Any]:
    """Org head + team heads (descriptions + child cluster descriptions) + overdue warning.

    Read-only; parses each index.md fully in Python (cheap) but returns only descriptions/
    children, never tier rows — routing itself (which team/cluster matches the request) stays
    host judgment.
    """
    root = Path(graph_root)
    cfg = mnx_config.load(str(root))
    overdue = mnx_compact.overdue(str(root), cfg, now)

    # The org-level index.md is NOT a router index (`> description` + `## Children`, what
    # `parse_index` expects) — it is the coarse team/domains/summary table that
    # `mnx_phonebook.regenerate_org` writes (and every promote/doctor-fix regenerates it into
    # that shape, even when `mnx_init.scaffold` wrote a router-shaped file on day one). Parse it
    # with `parse_md_table`, matching `mnx_doctor`'s own org-directory check (inv 20).
    org_idx_path = root / mnx_common.INDEX_FILENAME
    org_head: dict[str, Any] = {}
    teams: list[dict[str, Any]] = []
    if org_idx_path.is_file():
        org_rows = mnx_common.parse_md_table(org_idx_path.read_text(encoding="utf-8"))
        org_head = {"description": "Organization knowledge graph — route to a team by its summary."}
        for row in org_rows:
            team_name = (row.get("team") or "").strip()
            if not team_name:
                continue
            team_dir = root / team_name
            team_idx_path = team_dir / mnx_common.INDEX_FILENAME
            if not team_idx_path.is_file():
                continue
            team_idx = mnx_common.parse_index(team_idx_path)
            clusters: list[dict[str, Any]] = []
            for cluster_child in team_idx.get("children", []):
                cluster_name = _child_name(cluster_child)
                cluster_dir = team_dir / cluster_name
                cluster_idx_path = cluster_dir / mnx_common.INDEX_FILENAME
                description = ""
                if cluster_idx_path.is_file():
                    description = mnx_common.parse_index(cluster_idx_path).get("description", "")
                clusters.append({"cluster": cluster_name, "path": str(cluster_dir),
                                 "description": description})
            teams.append({"team": team_name, "path": str(team_dir),
                         "description": team_idx.get("description", "") or row.get("summary", ""),
                         "clusters": clusters})

    empty = not any(t["clusters"] for t in teams)
    return {"graph_root": str(root), "overdue": overdue, "org_head": org_head, "teams": teams,
           "empty": empty}


# --- fill_offer (onboarding plan Phase 3, the empty-graph fork) --------------------


def fill_offer(empty: bool, staged_count: int) -> Optional[dict[str, Any]]:
    """The empty-graph fork suggestion — composed from two signals `frontier()` alone can't
    see (staging lives outside the graph, keyed by binding, not graph_root; see mnx_stage.py),
    so this stays a separate pure function the caller feeds both into. `None` when there is
    nothing to offer (graph not empty).

    Deliberately NOT folded into `frontier()` itself: that would force a binding dependency
    onto a function that today only needs a bare graph_root, and would make the always-empty
    check pay for a staging-dir scan on every single read even when the graph is non-empty
    (the common case). Callers (the MCP `read_frontier` tool; the mnx-read SKILL step) compose
    this explicitly, only when `empty` is true.
    """
    if not empty:
        return None
    if staged_count == 0:
        return {"offer": "fill", "message": (
            "This graph is empty. Seed it from a repo/docs now, or just keep working — "
            "I'll remember as we go.")}
    plural = "" if staged_count == 1 else "s"
    return {"offer": "promote", "message": f"{staged_count} item{plural} captured; promote to see them."}


# --- scan (SKILL steps 3+3b+3c: tier tables + staged overlay + stale cues) ---------

_TIERS = ("hot", "warm", "cold")


def _is_stale(stale_after: Optional[str], now: str) -> bool:
    """True iff `stale_after` is a real (non-'—') timestamp already in the past."""
    if not stale_after or stale_after == mnx_index.STALE_NULL:
        return False
    try:
        return mnx_common.parse_ts(stale_after) < mnx_common.parse_ts(now)
    except Exception:
        return False


def scan(cluster: str | Path, now: str, tiers: Optional[list[str]] = None,
        binding: Any = None) -> dict[str, Any]:
    """A cluster's tier tables (with per-row `stale`) + its staged-capture overlay.

    `tiers` (default all three) lets the "stop early" judgment ask for just Hot first, then
    widen to Warm/Cold. The overlay is independent of `tiers` — staged atoms are always
    relevant to the cluster's domain regardless of which graph tiers were requested.
    """
    cluster = Path(cluster)
    idx_path = cluster / mnx_common.INDEX_FILENAME
    if not idx_path.is_file():
        raise ValueError(f"{cluster}: not a cluster (no {mnx_common.INDEX_FILENAME})")
    idx = mnx_common.parse_index(idx_path)
    want = [t for t in (tiers or _TIERS) if t in _TIERS]

    tier_tables: dict[str, list[dict[str, Any]]] = {}
    stale: list[dict[str, Any]] = []
    for t in want:
        rows = []
        for row in idx.get(t, []):
            r = dict(row)
            r["stale"] = _is_stale(row.get("stale_after"), now)
            if r["stale"]:
                stale.append({"id": row.get("id"), "tier": t, "stale_after": row.get("stale_after")})
            rows.append(r)
        tier_tables[t] = rows

    overlay = mnx_stage.overlay(domains=[cluster.name], binding=binding)

    return {"cluster": str(cluster), "description": idx.get("description", ""),
           "tiers": tier_tables, "stale": stale, "overlay": overlay}


# --- expand (SKILL step 4: resolve ids -> bodies + governed-by companions) ---------

def _node_entry(nid: str, scope: str) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    path = mnx_resolve.resolve(nid, scope)
    if not path:
        return None, None
    node = mnx_common.parse_node(path)
    body = (node.get("_body") or "").strip()
    entry = {"id": nid, "path": str(path), "type": node.get("type"), "title": node.get("title"),
             "summary": node.get("summary"), "status": node.get("status", "active"), "body": body}
    return entry, node


def expand(ids: list[str], graph_root: str | Path, max_bytes: Optional[int] = None) -> dict[str, Any]:
    """Resolve `ids` to node bodies, pulling each node's `governed-by` pattern companions.

    Refuses `stg-` ids (those bodies already came from `scan`'s overlay, not the graph).
    Budget-capped by `max_bytes`; the very first accepted body is never starved by the
    budget, so a caller always gets at least one body back.
    """
    scope = str(Path(graph_root))
    refused = sorted({i for i in ids if i.startswith("stg-")})
    requested = [i for i in dict.fromkeys(ids) if not i.startswith("stg-")]

    nodes: list[dict[str, Any]] = []
    companions: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    truncated = False

    def _accept(size: int) -> bool:
        nonlocal total
        if max_bytes is not None and (nodes or companions) and total + size > max_bytes:
            return False
        total += size
        return True

    for nid in requested:
        if nid in seen:
            # Already pulled in as a governed-by companion — but it was ALSO explicitly
            # requested, so promote it to a first-class node instead of leaving it buried
            # in `companions` (budget already accounted for; no re-fetch, no duplication).
            idx = next((i for i, c in enumerate(companions) if c["id"] == nid), None)
            if idx is not None:
                promoted = companions.pop(idx)
                promoted.pop("governs", None)
                nodes.append(promoted)
            continue
        entry, node = _node_entry(nid, scope)
        if entry is None:
            nodes.append({"id": nid, "found": False})
            continue
        size = len(entry["body"].encode("utf-8"))
        if not _accept(size):
            truncated = True
            continue
        seen.add(nid)
        nodes.append(entry)
        for e in node.get("edges") or []:
            if not (isinstance(e, dict) and e.get("type") == "governed-by" and e.get("to")):
                continue
            gid = e["to"]
            if gid in seen:
                continue
            gentry, _gnode = _node_entry(gid, scope)
            if gentry is None:
                continue
            gsize = len(gentry["body"].encode("utf-8"))
            if not _accept(gsize):
                truncated = True
                continue
            seen.add(gid)
            gentry["governs"] = nid
            companions.append(gentry)

    return {"nodes": nodes, "companions": companions, "refused": refused,
           "truncated": truncated, "bytes": total}


# --- cli -----------------------------------------------------------------------------

def _arg(argv: list[str], flag: str) -> Optional[str]:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


_USAGE = [
    "mnx_read.py frontier <graph_root>                                     "
    "— org+team heads, chunk-1 routing, overdue warning",
    "mnx_read.py scan <cluster> [--tiers hot,warm,cold]                    "
    "— tier tables (+ stale flag) + staged overlay + stale cues",
    "mnx_read.py expand <id,id,...> --scope <graph_root> [--max-bytes N]   "
    "— node bodies + governed-by companions",
]
_FLAGS = {"--tiers": True, "--scope": True, "--max-bytes": True}


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE, _FLAGS)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "frontier":
            if len(argv) < 3:
                return mnx_common.emit({"error": "frontier needs <graph_root>"}, ok=False)
            return mnx_common.emit(frontier(argv[2], mnx_common.now_utc()))
        if cmd == "scan":
            if len(argv) < 3:
                return mnx_common.emit({"error": "scan needs <cluster>"}, ok=False)
            tiers_arg = _arg(argv, "--tiers")
            tiers = [t.strip() for t in tiers_arg.split(",") if t.strip()] if tiers_arg else None
            return mnx_common.emit(scan(argv[2], mnx_common.now_utc(), tiers))
        if cmd == "expand":
            if len(argv) < 3:
                return mnx_common.emit({"error": "expand needs <id,id,...>"}, ok=False)
            scope = _arg(argv, "--scope")
            if not scope:
                return mnx_common.emit({"error": "expand needs --scope <graph_root>"}, ok=False)
            ids = [s for s in argv[2].split(",") if s]
            mb = _arg(argv, "--max-bytes")
            return mnx_common.emit(expand(ids, scope, int(mb) if mb else None))
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
