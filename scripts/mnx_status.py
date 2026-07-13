"""mnx_status.py — one-move "what's bound / what's in my graph / is it healthy" surface.

mnx-doctor validates and repairs; it is not an at-a-glance status. This helper aggregates
the read-only signals a user wants to glance at — the binding, the graph kind, node/tier
counts per team, pending (un-pushed) usage stamps, last gc per team, and a health summary —
into ONE JSON object, so /mnemex:mnx-status can answer "is my memory set up and healthy?"
without the user running four different tools.

Strictly read-only: it never clones, syncs, commits, or repairs. Every section is computed
best-effort and guarded, so a partially-scaffolded or broken graph still yields a useful
status instead of a traceback. See USER-JOURNEY-FINDINGS #2.

Dependencies: Python 3.9+ stdlib + PyYAML only (via the other mnx_* helpers). See docs/06.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import mnx_binding
import mnx_common
import mnx_stamp


def _is_present(binding) -> bool:
    root = Path(binding.graph_root())
    if binding.remote:
        return root.is_dir() and (root / ".git").exists()
    return root.is_dir()


def _last_compaction_map(root: Path) -> dict[str, str]:
    """Parse <graph>/.mnemex/last_compaction ('team=<iso>' lines) into a dict. {} if absent."""
    f = mnx_common.state_dir(root) / "last_compaction"
    out: dict[str, str] = {}
    if not f.is_file():
        return out
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


def _tier_counts(cluster: Path) -> dict[str, int]:
    """Hot/warm/cold row counts from the cluster index (best-effort; {} if unparseable).

    Cold rows of a chained index spill into index.NNN.md continuation chunks, so sum those too."""
    if not (cluster / mnx_common.INDEX_FILENAME).is_file():
        return {}
    try:
        import mnx_index
        counts = {"hot": 0, "warm": 0, "cold": 0}
        for idx_file in mnx_index._index_files(str(cluster)):
            parsed = mnx_common.parse_index(idx_file)
            for t in ("hot", "warm", "cold"):
                counts[t] += len(parsed.get(t, []))
        return counts
    except Exception:
        return {}


def _gc_overdue_days(team_dir: Path, last_gc: str | None) -> int | None:
    """Whole days the team's gc is overdue vs. its cadence, best-effort. None if unknown."""
    if not last_gc:
        return None
    try:
        import mnx_compact
        import mnx_config
        cfg = mnx_config.load(str(team_dir))
        ov = mnx_compact.overdue(str(team_dir), cfg, mnx_common.now_utc())
        return int(ov.get("days_overdue") or 0) if ov.get("due") else 0
    except Exception:
        return None


def _graph_summary(binding) -> dict[str, Any]:
    """Per-team node/cluster/tier counts + last gc, plus graph totals. Read-only."""
    root = Path(binding.graph_root())
    last_gc = _last_compaction_map(root)
    teams: dict[str, dict[str, Any]] = {}
    totals = {"teams": 0, "clusters": 0, "nodes": 0, "hot": 0, "warm": 0, "cold": 0}

    for cluster in mnx_common.iter_clusters(root):
        team = mnx_common.team_of(root, cluster) or "(root)"
        t = teams.setdefault(team, {"team": team, "clusters": 0, "nodes": 0,
                                    "hot": 0, "warm": 0, "cold": 0, "cluster_names": []})
        t["clusters"] += 1
        t["cluster_names"].append(cluster.name)
        nodes = len(mnx_common.iter_node_files(cluster))
        t["nodes"] += nodes
        tiers = _tier_counts(cluster)
        for tier in ("hot", "warm", "cold"):
            t[tier] += tiers.get(tier, 0)

    for team, t in teams.items():
        team_dir = root / team if team != "(root)" else root
        gc = last_gc.get(team)
        t["last_gc"] = gc
        t["gc_overdue_days"] = _gc_overdue_days(team_dir, gc)
        totals["teams"] += 1
        for k in ("clusters", "nodes", "hot", "warm", "cold"):
            totals[k] += t[k]

    return {"teams": sorted(teams.values(), key=lambda x: x["team"]), "totals": totals}


def _staging_summary(binding) -> dict[str, Any]:
    """The local staging tier as an inspectable list, not just a count, so the user can review
    (and then drop/discard via mnx-capture) what is pending promotion. Read-only.

    Staging lives outside the graph clone (per-author/local), so this works whether or not the
    graph is materialized this session. Atoms are returned newest-first with their provisional id,
    so /mnemex:mnx-capture --drop <id> can target a specific one."""
    import mnx_stage
    listing = mnx_stage.list_atoms(binding)
    st = mnx_stage.status(binding)
    return {
        "count": listing.get("count", 0),
        "budget_level": st.get("budget", {}).get("level"),
        "urgent": st.get("urgent", 0),
        "oldest_age_days": st.get("oldest_age_days"),
        "atoms": listing.get("atoms", []),
        "held": st.get("held", {"count": 0}),
    }


def _health(root: Path) -> dict[str, Any]:
    """Doctor error/warning counts only (not the full finding list). Best-effort."""
    try:
        import mnx_doctor
        rep = mnx_doctor.check(str(root))
        counts = rep.get("counts", {})
        return {"ok": rep.get("ok", False),
                "errors": counts.get("E", 0), "warnings": counts.get("W", 0)}
    except Exception as exc:
        return {"ok": None, "error": str(exc)}


def status() -> dict[str, Any]:
    binding = mnx_binding.resolve()
    if binding is None:
        return {"resolved": False,
                "message": "No Mnemex graph configured for this project. Run /mnemex:mnx-init."}

    out: dict[str, Any] = {"resolved": True, "binding": binding.to_dict()}
    present = _is_present(binding)
    out["clone_present"] = present

    # pending usage stamps (un-pushed reads) — durability signal
    try:
        st = mnx_stamp.status()
        out["pending_stamps"] = st.get("pending", 0)
        out["stamp_durability"] = st.get("durability", "batched")
    except Exception:
        out["pending_stamps"] = None

    # staged captures (local, un-promoted) — makes the count inspectable, not a black box.
    # Staging is local and independent of the clone, so report it even when not materialized.
    try:
        out["staging"] = _staging_summary(binding)
    except Exception as exc:
        out["staging"] = {"error": str(exc)}

    if not present:
        out["available"] = False
        out["note"] = ("The graph is bound but not materialized in this session yet — run "
                       "/mnemex:mnx-read (or any session start) to sync it before browsing.")
        return out

    out["available"] = True
    try:
        out.update(_graph_summary(binding))
    except Exception as exc:
        out["summary_error"] = str(exc)
    out["health"] = _health(Path(binding.graph_root()))
    return out


_USAGE = [
    'mnx_status.py [status]   — binding + staging + graph health snapshot',
]


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else "status"
    try:
        if cmd == "status":
            res = status()
            # "not configured" is a valid status, not a script failure — only "error" fails.
            return mnx_common.emit(res, ok="error" not in res)
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
