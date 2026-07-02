"""mnx_config.py — config load, derivation, version stamping, re-normalization.

See docs/07-configuration.md and docs/06-script-contracts.md.

The user sets ONE knob (half_life_days); everything else is defaulted or derived.
config_version + λ in force are stamped at each gc so a later parameter change is
detectable (F5) and re-normalized for score continuity before any tier decision.
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
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
    "freshness_ttl_days": 30,       # revalidation horizon: a fact goes STALE this long after `verified`
    "freshness_pattern_bonus": 0.30,  # patterns get a longer horizon (derived, orthogonal to decay)
    "freshness_volatile_factor": 0.15,  # volatility:volatile → freshness_ttl_days · this
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
    """Compute λ_domain, λ_pattern, the derived pattern half-life, and freshness horizons."""
    out = dict(cfg)
    out["half_life_pattern"] = mnx_decay.half_life_for("pattern", cfg)
    out["lam_domain"] = mnx_decay.lam_for("domain", cfg)
    out["lam_pattern"] = mnx_decay.lam_for("pattern", cfg)
    out["freshness_horizon_domain"] = mnx_decay.freshness_horizon_days("domain", cfg)
    out["freshness_horizon_pattern"] = mnx_decay.freshness_horizon_days("pattern", cfg)
    return out


def horizon_days(node: dict[str, Any], cfg: dict[str, Any]) -> float | None:
    """Resolve a node's freshness horizon in DAYS. None ⇒ never stale (timeless).

    Precedence (Doc 14 §4): per-node `volatility` override → type-derived default.
    """
    vol = node.get("volatility", "default")
    if isinstance(vol, str):
        vol = vol.strip().lower()
    if vol == "timeless":
        return None
    if vol == "volatile":
        return float(cfg.get("freshness_ttl_days", 30)) * float(cfg.get("freshness_volatile_factor", 0.15))
    # explicit integer day-count override (int, or a digit string)
    if isinstance(vol, bool):
        pass
    elif isinstance(vol, (int, float)):
        return float(vol)
    elif isinstance(vol, str) and vol.isdigit():
        return float(vol)
    # default → derive from type
    return mnx_decay.freshness_horizon_days(node.get("type", "domain"), cfg)


def resolve_horizon(node: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Precomputed `stale_after` = verified + horizon, as an ISO-8601 UTC string.

    None ⇒ never stale: `volatility: timeless`, a dead/superseded node, or an unusable
    `verified` timestamp. `verified` falls back to `updated`→`created` for legacy/migration
    nodes. Pure; the only clock read is the node's own `verified`. See docs/14.
    """
    if str(node.get("status", "active")) in ("dead", "superseded"):
        return None
    days = horizon_days(node, cfg)
    if days is None:
        return None
    verified = node.get("verified") or node.get("updated") or node.get("created")
    if not verified:
        return None
    try:
        dt = mnx_common.parse_ts(verified) + timedelta(days=float(days))
        return dt.strftime(mnx_common.ISO_FMT)
    except Exception:
        return None


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
        if cmd == "horizon":
            # horizon <node_path>  → the node's stale_after (freshness) resolution
            node = mnx_common.parse_node(argv[2])
            cfg = load(argv[2])
            return mnx_common.emit({"id": node.get("id"), "volatility": node.get("volatility", "default"),
                                    "verified": node.get("verified"),
                                    "horizon_days": horizon_days(node, cfg),
                                    "stale_after": resolve_horizon(node, cfg)})
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
