"""mnx_config.py — config load, derivation, version stamping, re-normalization.

See docs/configuration.md and docs/script-contracts.md.

The user sets ONE knob (half_life_days); everything else is defaulted or derived.
config_version + λ in force are stamped at each gc so a later parameter change is
detectable (F5) and re-normalized for score continuity before any tier decision.
"""
from __future__ import annotations

import json
import re
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

# --- Tunable schema: validation + human-facing metadata for `show`/`set`. --------
# `kind`: how the raw string is coerced/validated. `group`: display grouping.
# `renorm`: True ⇒ editing it needs the re-normalization pass (decay/freshness λ),
# so `set` warns and the next consolidation makes it take effect gradually (F5).
# `advanced`: internal knob, hidden from the default `show` unless --all.
SCHEMA: dict[str, dict[str, Any]] = {
    "half_life_days": {"kind": "pos", "group": "Decay", "renorm": True,
        "help": "The ONE knob. Days for an *unused* domain fact to lose half its relevance. "
                "Longer = graph forgets more slowly."},
    "pattern_halflife_bonus": {"kind": "nonneg", "group": "Decay", "renorm": True,
        "help": "Patterns (the 'how') persist this fraction longer than domain facts (the 'what'). "
                "0.30 = +30%. Derived — you never tune two decay rates."},
    "hot_k": {"kind": "posint", "group": "Tiers",
        "help": "Top-K hottest nodes kept per cluster in always-loaded chunk-1. ~12-20; size it to "
                "what you are happy to always load at zero extra read cost."},
    "warm_band": {"kind": "unit", "group": "Tiers",
        "help": "Live-score floor for the WARM tier; below this a node falls to COLD. 0..1."},
    "cold_ttl_days": {"kind": "nonneg", "group": "Tiers",
        "help": "Grace period a node sits in COLD before it becomes a death candidate. "
                "Keep generous early so nothing dies before you've observed real usage."},
    "cold_recall_multiplier": {"kind": "pos", "group": "Tiers",
        "help": "Spaced-repetition over-reward: reviving a COLD node boosts its strength harder "
                "than reviving a warm one. >1 = extra boost."},
    "strength_max": {"kind": "pos", "group": "Tiers",
        "help": "Saturation cap on stored strength — prevents 'immortal' nodes that never decay out."},
    "freshness_ttl_days": {"kind": "pos", "group": "Freshness", "renorm": True,
        "help": "A SEPARATE clock from decay: days after a fact was last *verified* before it is "
                "flagged STALE for re-check. About whether a fact is still TRUE, not still used."},
    "freshness_pattern_bonus": {"kind": "nonneg", "group": "Freshness", "renorm": True,
        "help": "Patterns get this fraction longer freshness horizon (derived, like the half-life bonus)."},
    "node_budget": {"kind": "posint", "group": "Budget",
        "help": "Active-node count past which a cluster's index is logically split (nodes never move). "
                "Sized for write-path comfort — it keeps reconciliation's match surface small."},
    "compaction_cadence_days": {"kind": "posint", "group": "Maintenance",
        "help": "mnx-read warns when the last consolidation (gc) is older than this many days."},
    "reconcile_cold_on": {"kind": "enum", "choices": ["always", "update", "never"], "group": "Maintenance",
        "help": "Lazy cold reconciliation. update (recommended) = scan cold only on update-intent; "
                "always = scan every reconcile (safer, costlier); never = cheapest, most duplication risk."},
    "purge_dead": {"kind": "bool", "group": "Death policy",
        "help": "false (recommended) = tombstone-and-retain dead nodes (audit-friendly, keeps lineage). "
                "true = hard-delete from the working tree (git history still retains them)."},
    # --- advanced / internal (hidden unless --all) ---
    "freshness_volatile_factor": {"kind": "pos", "group": "Freshness", "advanced": True, "renorm": True,
        "help": "volatility:volatile horizon = freshness_ttl_days x this factor."},
    "struct_scale": {"kind": "pos", "group": "Advanced", "advanced": True,
        "help": "Liveness-weighted in-degree scale for a node's structural strength."},
    "node_body_max_chars": {"kind": "posint", "group": "Advanced", "advanced": True,
        "help": "Soft per-node body budget; over this, capture proposes splitting into linked nodes."},
    "index_chunk_rows": {"kind": "posint", "group": "Advanced", "advanced": True,
        "help": "Cold rows per index file before chaining index.NNN.md (B-tree leaf)."},
    "tier_files": {"kind": "bool", "group": "Advanced", "advanced": True,
        "help": "Split warm/cold/dead into sibling files with index.md as a slim router."},
}

# nested boost weights addressed as boost.<name>
BOOST_KEYS = {
    "boost.contributed": "Stamp weight when a node *materially shaped* the artifact.",
    "boost.consulted": "Stamp weight when a node *informed reasoning* but wasn't in the output.",
    "boost.traversed": "Stamp weight when a node was merely *routed through* (0.0 = unstamped).",
}


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

    Precedence (Freshness & Revalidation §4): per-node `volatility` override → type-derived default.
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

    None ⇒ never stale: `volatility: timeless`, a dead (retired) node, or an unusable
    `verified` timestamp. `verified` falls back to `updated`→`created` for legacy/migration
    nodes. Pure; the only clock read is the node's own `verified`. See docs/14.
    """
    if str(node.get("status", "active")) == "dead":
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


def _coerce(key: str, kind: str, raw: str, choices: list[str] | None = None) -> Any:
    """Validate `raw` against `kind`; return the typed value or raise ValueError with a
    user-facing message. Also returns the canonical token via .canon (below)."""
    r = raw.strip()
    if kind == "bool":
        low = r.lower()
        if low in ("true", "false"):
            return low == "true"
        raise ValueError(f"{key}: expected true or false, got {raw!r}")
    if kind == "enum":
        if choices and r in choices:
            return r
        raise ValueError(f"{key}: expected one of {choices}, got {raw!r}")
    if kind in ("posint",):
        try:
            v = int(r)
        except ValueError:
            raise ValueError(f"{key}: expected a whole number, got {raw!r}")
        if v < 1:
            raise ValueError(f"{key}: must be >= 1, got {v}")
        return v
    # numeric kinds: pos (>0), nonneg (>=0), unit (0..1)
    try:
        v = float(r)
    except ValueError:
        raise ValueError(f"{key}: expected a number, got {raw!r}")
    if kind == "pos" and v <= 0:
        raise ValueError(f"{key}: must be > 0, got {v}")
    if kind == "nonneg" and v < 0:
        raise ValueError(f"{key}: must be >= 0, got {v}")
    if kind == "unit" and not (0.0 <= v <= 1.0):
        raise ValueError(f"{key}: must be between 0 and 1, got {v}")
    return v


def _fm_bounds(lines: list[str]) -> tuple[int, int]:
    """Return (start, end) indices of the YAML front-matter body (exclusive of the
    two `---` fences). Raises if the file has no front-matter block."""
    if not lines or lines[0].strip() != "---":
        raise ValueError("config file has no YAML front-matter block")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return 1, i
    raise ValueError("config front-matter is not terminated by ---")


def _edit_scalar(lines: list[str], start: int, end: int, key: str, canon: str) -> bool:
    """Replace the value of top-level `key` in lines[start:end], preserving any inline
    `# comment`. Returns True if the key line existed and was edited."""
    pat = re.compile(rf"^(?P<k>{re.escape(key)}\s*:\s*)(?P<v>.*?)(?P<c>\s*#.*)?$")
    for i in range(start, end):
        stripped = lines[i]
        if stripped.startswith((" ", "\t")):
            continue  # nested; not a top-level key
        m = pat.match(stripped)
        if m:
            lines[i] = f"{m.group('k')}{canon}{m.group('c') or ''}"
            return True
    return False


def _edit_boost(lines: list[str], start: int, end: int, child: str, canon: str) -> bool:
    """Replace boost.<child> under the `boost:` mapping, preserving inline comment."""
    in_boost = False
    child_pat = re.compile(rf"^(?P<i>\s+)(?P<k>{re.escape(child)}\s*:\s*)(?P<v>.*?)(?P<c>\s*#.*)?$")
    for i in range(start, end):
        if re.match(r"^boost\s*:\s*$", lines[i]):
            in_boost = True
            continue
        if in_boost:
            if lines[i] and not lines[i].startswith((" ", "\t")):
                break  # dedented out of the boost block
            m = child_pat.match(lines[i])
            if m:
                lines[i] = f"{m.group('i')}{m.group('k')}{canon}{m.group('c') or ''}"
                return True
    return False


def _canon(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        # keep 1.0 as 1.0 only if the default was fractional; safe general form:
        return repr(value) if value != int(value) else (f"{value:.1f}" if value in (0.0, 1.0) else str(int(value)))
    return str(value)


def show(repo: str, include_advanced: bool = False) -> dict[str, Any]:
    """Structured view of the effective config: value, default, overridden?, help.
    Read-only; used to explain the config to the user before any change."""
    cfg = load(repo)
    d = derive(cfg)
    root = cfg["_repo"]
    cfgfile = Path(root) / mnx_common.CONFIG_FILENAME
    items: list[dict[str, Any]] = []
    for key, meta in SCHEMA.items():
        if meta.get("advanced") and not include_advanced:
            continue
        value = cfg.get(key, DEFAULTS.get(key))
        items.append({
            "key": key, "group": meta["group"], "value": value,
            "default": DEFAULTS.get(key), "overridden": value != DEFAULTS.get(key),
            "renorm": bool(meta.get("renorm")), "advanced": bool(meta.get("advanced")),
            "help": meta["help"],
            **({"choices": meta["choices"]} if "choices" in meta else {}),
        })
    for bkey, help_ in BOOST_KEYS.items():
        child = bkey.split(".", 1)[1]
        value = cfg["boost"].get(child)
        items.append({
            "key": bkey, "group": "Usage boosts", "value": value,
            "default": DEFAULTS["boost"].get(child),
            "overridden": value != DEFAULTS["boost"].get(child),
            "renorm": False, "advanced": False, "help": help_,
        })
    return {
        "config_file": str(cfgfile), "config_version": version(cfg), "exists": cfgfile.is_file(),
        "items": items,
        "derived": {"half_life_pattern": d["half_life_pattern"], "lam_domain": d["lam_domain"],
                    "lam_pattern": d["lam_pattern"],
                    "freshness_horizon_domain": d["freshness_horizon_domain"],
                    "freshness_horizon_pattern": d["freshness_horizon_pattern"]},
    }


def set_value(repo: str, key: str, raw: str) -> dict[str, Any]:
    """Validate and write one config value into mnemex.config.md in place (preserving
    comments/body), then auto-bump config_version. Returns a result describing the
    change, including whether a re-normalization is now pending (renorm keys)."""
    root = str(mnx_common.find_graph_root(repo) or Path(repo))
    cfgfile = Path(root) / mnx_common.CONFIG_FILENAME
    if not cfgfile.is_file():
        raise ValueError(f"no {mnx_common.CONFIG_FILENAME} at the graph root — run mnx-init first")

    if key == "config_version":
        raise ValueError("config_version is managed automatically; it is bumped on every set")
    is_boost = key in BOOST_KEYS
    if not is_boost and key not in SCHEMA:
        known = ", ".join(list(SCHEMA) + list(BOOST_KEYS))
        raise ValueError(f"unknown config key {key!r}. Known keys: {known}")

    if is_boost:
        typed = _coerce(key, "nonneg", raw)
        renorm = False
    else:
        meta = SCHEMA[key]
        typed = _coerce(key, meta["kind"], raw, meta.get("choices"))
        renorm = bool(meta.get("renorm"))

    prev = load(repo)
    old_value = prev["boost"].get(key.split(".", 1)[1]) if is_boost else prev.get(key)
    old_version = version(prev)
    new_version = old_version + 1
    canon = _canon(typed)

    text = cfgfile.read_text(encoding="utf-8")
    lines = text.splitlines()
    start, end = _fm_bounds(lines)

    ok = _edit_boost(lines, start, end, key.split(".", 1)[1], canon) if is_boost \
        else _edit_scalar(lines, start, end, key, canon)
    if not ok:
        # key relies on a default and isn't written yet → append inside the block
        lines.insert(end, f"{key}: {canon}" if not is_boost else f"# set boost via editor; {key}={canon}")
        end += 1
        if is_boost:
            raise ValueError(f"{key} is not present in the file; add a `boost:` block manually first")

    # auto-bump config_version (edit if present, else insert at top of the block)
    if not _edit_scalar(lines, start, end, "config_version", str(new_version)):
        lines.insert(start, f"config_version: {new_version}")

    cfgfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "action": "set", "key": key, "old": old_value, "new": typed,
        "config_version": {"old": old_version, "new": new_version},
        "renorm_pending": renorm,
        "note": ("This is a decay/freshness parameter: the change is staged. The next mnx-promote "
                 "(consolidation) re-normalizes stored scores so nothing jumps tiers abruptly, then "
                 "stamps the new version. mnx-read will warn until then.") if renorm else
                ("Change applied. Non-decay parameter: it takes effect immediately on the next read/write."),
        "config_file": str(cfgfile),
    }


_USAGE = [
    "mnx_config.py load <team-or-graph>          — effective config (defaults + graph overrides)",
    "mnx_config.py derive <team-or-graph>        — derived decay parameters (λ per type)",
    "mnx_config.py drift <team-or-graph>         — config drift vs the last-compaction stamp",
    "mnx_config.py stamp <team-or-graph>         — stamp the current config version",
    "mnx_config.py show <repo> [--all]           — user-facing config view (--all includes advanced keys)",
    "mnx_config.py set <repo> <key> <value>      — set one config key in mnemex.config.md",
    "mnx_config.py horizon <node.md>             — the node's freshness horizon (stale_after)",
]
_FLAGS = {"--all": False}


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE, _FLAGS)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else ""
    # Every subcommand takes a scope path first; a missing one must be a usage error,
    # not a bare "list index out of range" (E2E 2026-07-12, finding G2).
    if cmd in ("load", "derive", "drift", "stamp", "show", "set", "horizon") and len(argv) < 3:
        return mnx_common.emit(
            {"error": f"usage: {cmd} <team-or-graph path>"
                      + (" <key> <value>" if cmd == "set" else "")}, ok=False)
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
        if cmd == "show":
            # show <repo> [--all]
            return mnx_common.emit(show(argv[2], include_advanced="--all" in argv[3:]))
        if cmd == "set":
            # set <repo> <key> <value>
            if len(argv) < 5:
                return mnx_common.emit({"error": "usage: set <repo> <key> <value>"}, ok=False)
            return mnx_common.emit(set_value(argv[2], argv[3], argv[4]))
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
