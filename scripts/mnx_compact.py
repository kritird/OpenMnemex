"""mnx_compact.py — registry replay + checkpoint (the LSM merge).

See docs/02-architecture.md §2 and docs/06-script-contracts.md.

The registry is the write-ahead log; the index strengths are the SSTable; compaction
(inside mnx-consolidate, promote's back half) is the merge. Compaction REPLAYS registry lines after the cluster's
high-water mark and ADVANCES the mark — it never truncates, so a stamp appended during
a compaction is simply picked up next time (F2: no lost stamps).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import mnx_common
import mnx_config
import mnx_decay

FLAG_ROLE = "flag"
REVALIDATED_ROLE = "revalidated"   # freshness event (weight 0): advances `verified`, NEVER strength (Doc 14)
MAINT_SENTINEL = "__maintenance-due__"


def _highwater_path(cluster: str) -> Path:
    root = mnx_common.require_graph_root(cluster)
    key = mnx_common.cluster_key(root, cluster)
    return mnx_common.state_dir(root) / "highwater" / key


def read_highwater(cluster: str) -> str:
    """Return the high-water mark (ts) replayed up to for this cluster, or ''."""
    p = _highwater_path(cluster)
    return p.read_text(encoding="utf-8").strip() if p.is_file() else ""


def _parse_registry(cluster: str) -> list[dict[str, Any]]:
    reg = Path(cluster) / mnx_common.REGISTRY_FILENAME
    if not reg.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in reg.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        nid, ts, role = parts[0], parts[1], parts[2]
        try:
            weight = float(parts[3]) if len(parts) > 3 else 1.0
        except ValueError:
            weight = 1.0
        rows.append({"id": nid, "ts": ts, "role": role, "weight": weight})
    return rows


def deltas_after(cluster: str, mark: str) -> list[dict[str, Any]]:
    """Registry entries strictly after the mark, sorted by ts."""
    rows = _parse_registry(cluster)
    if mark:
        cut = mnx_common.parse_ts(mark)
        rows = [r for r in rows if _safe_after(r["ts"], cut)]
    rows.sort(key=lambda r: r["ts"])
    return rows


def _safe_after(ts: str, cut) -> bool:
    try:
        return mnx_common.parse_ts(ts) > cut
    except Exception:
        return False


def fold(materialized_state: dict[str, Any], deltas: list[dict[str, Any]],
         cfg: dict[str, Any], now: str) -> dict[str, Any]:
    """Pure: fold deltas (in ts order) onto materialized strengths → new state.

    materialized_state: {id: {strength, last_update, type}}. Order-independent over
    same-id deltas applied in ts order. The `flag`/maintenance sentinel rows are ignored, and so
    are `revalidated` (freshness) events — they carry weight 0 and must never touch strength or
    last_update; they advance the node's `verified` instead (see latest_revalidations, applied to
    node truth by the consolidate pass — Doc 14).
    """
    state = {k: dict(v) for k, v in materialized_state.items()}
    for d in sorted(deltas, key=lambda r: r["ts"]):
        if d.get("role") in (FLAG_ROLE, REVALIDATED_ROLE) or d.get("id") == MAINT_SENTINEL:
            continue
        nid = d["id"]
        cur = state.get(nid, {"strength": 0.0, "last_update": d["ts"], "type": "domain"})
        ntype = cur.get("type", "domain")
        new_strength, _ = mnx_decay.apply_use(
            float(cur.get("strength", 0.0)), cur.get("last_update", d["ts"]),
            d["ts"], d.get("role", "contributed"), ntype, cfg)
        cur["strength"] = round(new_strength, 6)
        cur["last_update"] = d["ts"]
        cur["type"] = ntype
        state[nid] = cur
    return state


def latest_revalidations(deltas: list[dict[str, Any]]) -> dict[str, str]:
    """Latest `revalidated` timestamp per node id among the given deltas (Doc 14).

    The consolidate pass uses this to advance each node's `verified` (a node truth-write): set
    node.verified = max(node.verified, latest_revalidations[id]). Monotonic; strength untouched.
    Pure — reads nothing but the deltas it is given.
    """
    out: dict[str, str] = {}
    for d in deltas:
        if d.get("role") != REVALIDATED_ROLE:
            continue
        nid, ts = d.get("id"), d.get("ts")
        if not nid or not ts:
            continue
        if nid not in out or _safe_after(ts, mnx_common.parse_ts(out[nid])):
            out[nid] = ts
    return out


def advance_highwater(cluster: str, mark: str) -> None:
    """Checkpoint forward only. Never truncates registry lines (F2)."""
    p = _highwater_path(cluster)
    prev = read_highwater(cluster)
    if prev:
        try:
            if mnx_common.parse_ts(mark) <= mnx_common.parse_ts(prev):
                return  # only ever moves forward
        except Exception:
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(mark.strip() + "\n", encoding="utf-8")


def retier(scope: str, advance: bool = True) -> dict[str, Any]:
    """Re-tier-only local pass (W5): fold the registry tail into the index strengths and rebuild
    the derived navigation — WITHOUT death, budget handling, contradiction resolution, or any
    commit/push. Deterministic, local, non-pushing; safe to run at session start so routing stays
    fresh between (human-gated) promotes.

    For each cluster: seed materialized state from the current index, fold registry deltas after
    the HWM, regenerate the index (which re-ranks → re-tiers), and advance the HWM. Then regenerate
    each team's phonebook and the org directory. It does NOT stamp last_compaction (a full
    maintenance pass owns that), so the maintenance-due nag still fires for the real consolidation.
    """
    import mnx_index
    import mnx_phonebook
    root = mnx_common.require_graph_root(scope)
    cfg = mnx_config.load(str(root))
    now = mnx_common.now_utc()
    clusters_done, teams = [], set()
    for cluster in mnx_common.iter_clusters(scope):
        cl = str(cluster)
        state = mnx_index._seed_from_index(cl)             # {id: {strength,last_update,type}}
        mark = read_highwater(cl)
        deltas = deltas_after(cl, mark)
        new_state = fold(state, deltas, cfg, now) if deltas else state
        mnx_index.regenerate_index(cl, new_state)
        if deltas and advance:
            advance_highwater(cl, max(d["ts"] for d in deltas))
        clusters_done.append({"cluster": cl, "deltas": len(deltas)})
        t = mnx_common.team_of(root, cluster)
        if t:
            teams.add(t)
    for t in sorted(teams):
        mnx_phonebook.regenerate(str(Path(root) / t))
    mnx_phonebook.regenerate_org(str(root))
    return {"action": "retier", "pushed": False, "stamped_last_compaction": False,
            "clusters": clusters_done, "teams": sorted(teams), "now": now}


def rotate(cluster: str, drop: bool = False) -> dict[str, Any]:
    """Registry segment rotation / WAL GC (W7): bound the append-only log.

    The registry is "checkpoint, never truncate" — but that lets it grow forever. Lines whose ts
    is at/below the confirmed HWM have ALREADY been folded into the index strength, so they are
    redundant for replay and safe to retire. This moves them to a segment archive under
    `.mnemex/registry-archive/<cluster_key>.md` (or drops them with `--drop`), keeping `registry.md`
    to just the un-applied tail (ts > HWM). This is the SSTable/segment-GC half of the LSM analogy
    the design leaned on but never finished.

    Conservative by design: only lines strictly covered by the confirmed mark are retired, so no
    un-folded stamp is ever lost (F2 preserved). No mark, nothing to rotate.
    """
    mark = read_highwater(cluster)
    reg = Path(cluster) / mnx_common.REGISTRY_FILENAME
    if not mark or not reg.is_file():
        return {"action": "rotate", "archived": 0, "kept": 0, "reason": "no mark or no registry"}
    cut = mnx_common.parse_ts(mark)
    header, kept, retired = [], [], []
    for line in reg.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            header.append(line)
            continue
        parts = s.split()
        ts = parts[1] if len(parts) > 1 else ""
        applied = False
        try:
            applied = mnx_common.parse_ts(ts) <= cut
        except Exception:
            applied = False
        (retired if applied else kept).append(line)
    if not retired:
        return {"action": "rotate", "archived": 0, "kept": len(kept), "cut": mark,
                "reason": "nothing at/below the mark"}
    if not drop:
        root = mnx_common.require_graph_root(cluster)
        key = mnx_common.cluster_key(root, cluster)
        arch = mnx_common.state_dir(root) / "registry-archive" / f"{key}.md"
        arch.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if arch.is_file() else f"# registry archive: {key} (retired segments)\n"
        with arch.open("a", encoding="utf-8") as fh:
            fh.write(prefix + f"# --- rotated at {mnx_common.now_utc()} (cut <= {mark}) ---\n")
            fh.write("\n".join(retired) + "\n")
    reg.write_text("\n".join(header + kept) + "\n", encoding="utf-8")
    return {"action": "rotate", "archived": len(retired), "kept": len(kept), "cut": mark,
            "dropped": drop}


def _last_compaction(team_path: str, team_name: str) -> str | None:
    root = mnx_common.require_graph_root(team_path)
    f = mnx_common.state_dir(root) / "last_compaction"
    if not f.is_file():
        return None
    for line in f.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == team_name:
                return v.strip()
    return None


def overdue(team: str, cfg: dict[str, Any], now: str) -> dict[str, Any]:
    """Return {due, days_overdue, config_drift} for the maintenance-due warning.

    mnx-read warns on this; it never acts (F6).
    """
    root = mnx_common.require_graph_root(team)
    team_name = mnx_common.team_of(root, team) or Path(team).name
    cadence = float(cfg.get("compaction_cadence_days", 14))
    drift = mnx_config.changed_since_last_compaction(team, cfg)
    last = _last_compaction(team, team_name)
    if last is None:
        return {"due": True, "days_overdue": 0, "never_compacted": True,
                "config_drift": drift, "team": team_name}
    days_since = mnx_common.clamp_dt(last, now) / mnx_common.SECONDS_PER_DAY
    due = days_since > cadence
    return {"due": due or drift,
            "days_overdue": max(0, int(math.floor(days_since - cadence))),
            "days_since": round(days_since, 2),
            "never_compacted": False,
            "config_drift": drift, "team": team_name}


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "overdue":
            target = argv[2]
            cfg = mnx_config.load(target)
            return mnx_common.emit(overdue(target, cfg, mnx_common.now_utc()))
        if cmd == "highwater":
            return mnx_common.emit({"cluster": argv[2], "mark": read_highwater(argv[2])})
        if cmd == "deltas":
            mark = read_highwater(argv[2])
            return mnx_common.emit({"cluster": argv[2], "mark": mark,
                                    "deltas": deltas_after(argv[2], mark)})
        if cmd == "revalidations":
            # ids with a fresh `revalidated` stamp since the HWM → {id: latest_ts} for the pass
            mark = read_highwater(argv[2])
            return mnx_common.emit({"cluster": argv[2], "mark": mark,
                                    "revalidations": latest_revalidations(deltas_after(argv[2], mark))})
        if cmd == "retier":
            return mnx_common.emit(retier(argv[2]))
        if cmd == "rotate":
            return mnx_common.emit(rotate(argv[2], drop="--drop" in argv[3:]))
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
