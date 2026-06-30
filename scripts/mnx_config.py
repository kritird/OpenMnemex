"""mnx_config.py — config load, derivation, version stamping, re-normalization.

See docs/07-configuration.md and docs/06-script-contracts.md.

The user sets ONE knob (half_life_days); everything else is defaulted or derived.
config_version + λ in force are stamped at each gc so a later parameter change is
detectable (F5) and re-normalized for score continuity before any tier decision.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import mnx_common
import mnx_decay

DEFAULTS: dict[str, Any] = {
    "config_version": 1,
    "half_life_days": 180,
    "pattern_halflife_bonus": 0.30,
    "hot_k": 12,
    "warm_band": 0.25,
    "cold_ttl_days": 120,
    "cold_recall_multiplier": 1.6,
    "strength_max": 1.0,
    "struct_scale": 2.0,           # liveness-weighted in-degree scale for structural strength (W6)
    "boost": {"contributed": 1.0, "consulted": 0.5, "traversed": 0.0},
    "node_budget": 35,
    "node_body_max_chars": 6000,   # soft per-node body budget; over → split into nodes + an edge
    "index_chunk_rows": 60,        # cold rows per index file; over → chain index.NNN.md (B-tree leaf)
    "compaction_cadence_days": 14,
    "reconcile_cold_on": "update",
    "purge_dead": False,
    "tier_files": False,           # W3: split warm/cold/dead into sibling files; index.md = slim router
}

VERSION_STAMP = "config_version"   # file under .mnemex/


def load(repo: str) -> dict[str, Any]:
    """Parse mnemex.config.md front-matter and apply defaults. Deterministic."""
    root = mnx_common.find_graph_root(repo) or Path(repo)
    cfg = dict(DEFAULTS)
    cfg["boost"] = dict(DEFAULTS["boost"])
    cfgfile = Path(root) / mnx_common.CONFIG_FILENAME
    if cfgfile.is_file():
        fm = mnx_common.read_frontmatter(cfgfile)
        for k, v in fm.items():
            if k == "boost" and isinstance(v, dict):
                cfg["boost"].update(v)
            else:
                cfg[k] = v
    cfg["_repo"] = str(root)
    return cfg


def derive(cfg: dict[str, Any]) -> dict[str, Any]:
    """Compute λ_domain, λ_pattern, and the derived pattern half-life."""
    out = dict(cfg)
    out["half_life_pattern"] = mnx_decay.half_life_for("pattern", cfg)
    out["lam_domain"] = mnx_decay.lam_for("domain", cfg)
    out["lam_pattern"] = mnx_decay.lam_for("pattern", cfg)
    return out


def version(cfg: dict[str, Any]) -> int:
    return int(cfg.get("config_version", 1))


def _stamp_path(team: str) -> Path:
    root = mnx_common.require_graph_root(team)
    return mnx_common.state_dir(root) / VERSION_STAMP


def read_stamp(team: str) -> dict[str, Any] | None:
    p = _stamp_path(team)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def stamp(team: str, cfg: dict[str, Any]) -> None:
    """Write config_version + λ in force to .mnemex/ for this graph."""
    d = derive(cfg)
    p = _stamp_path(team)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "config_version": version(cfg),
        "lam_domain": d["lam_domain"],
        "lam_pattern": d["lam_pattern"],
        "stamped_at": mnx_common.now_utc(),
    }, indent=2) + "\n", encoding="utf-8")


def changed_since_last_compaction(team: str, cfg: dict[str, Any]) -> bool:
    """True iff config_version/λ differs from the last-compaction stamp (drift)."""
    prev = read_stamp(team)
    if prev is None:
        return True  # never compacted under any stamp → treat as drift
    d = derive(cfg)
    if int(prev.get("config_version", -1)) != version(cfg):
        return True
    return (abs(float(prev.get("lam_domain", 0.0)) - d["lam_domain"]) > 1e-12 or
            abs(float(prev.get("lam_pattern", 0.0)) - d["lam_pattern"]) > 1e-12)


def renormalize(scope: str, old_lam: float, new_lam: float, now: str) -> dict[str, Any]:
    """Plan a one-time recompute of stored strengths so each node's LIVE score is
    continuous across a parameter change (F5).

    Strategy (collapse-to-current): set new_strength = score(old_strength, last_update,
    now, old_lam_for_type) and new_last_update = now. Then score_new(now) == new_strength
    == score_old(now) for every node regardless of new_lam, and decay restarts cleanly
    under the new λ. `old_lam`/`new_lam` are the DOMAIN constants; the pattern constants
    are recovered from the stamp ratio so both node types stay continuous.

    Returns a plan (read-only); the gc apply phase writes the new index strengths.
    """
    root = mnx_common.require_graph_root(scope)
    prev = read_stamp(root) or {}
    # derive per-type old λ: keep the domain/pattern ratio from the previous stamp.
    old_lam_domain = float(prev.get("lam_domain", old_lam)) or old_lam
    old_lam_pattern = float(prev.get("lam_pattern", old_lam_domain))
    changes: list[dict[str, Any]] = []
    for cluster in mnx_common.iter_clusters(scope):
        idx_path = Path(cluster) / mnx_common.INDEX_FILENAME
        if not idx_path.is_file():
            continue
        idx = mnx_common.parse_index(idx_path)
        for tier in ("hot", "warm", "cold"):
            for row in idx[tier]:
                try:
                    strength = float(row.get("strength", "0") or 0)
                    last_update = row.get("last_update", now)
                except ValueError:
                    continue
                lam_t = old_lam_pattern if row.get("type") == "pattern" else old_lam_domain
                live = mnx_decay.score(strength, last_update, now, lam_t)
                changes.append({
                    "cluster": str(cluster),
                    "id": row["id"],
                    "old_strength": strength,
                    "new_strength": round(live, 6),
                    "new_last_update": now,
                })
    return {"action": "renormalize", "old_lam": old_lam, "new_lam": new_lam,
            "now": now, "changes": changes}


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "load":
            return mnx_common.emit(load(argv[2]))
        if cmd == "derive":
            return mnx_common.emit(derive(load(argv[2])))
        if cmd == "drift":
            cfg = load(argv[2])
            return mnx_common.emit({
                "config_drift": changed_since_last_compaction(argv[2], cfg),
                "current": {"config_version": version(cfg)},
                "stamp": read_stamp(argv[2]),
            })
        if cmd == "stamp":
            stamp(argv[2], load(argv[2]))
            return mnx_common.emit({"action": "stamped", "stamp": read_stamp(argv[2])})
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
