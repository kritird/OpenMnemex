"""mnx_decay.py — the relevance math (pure functions, no I/O).

Deterministic given inputs. See docs/02-architecture.md and docs/06-script-contracts.md.

The model (Doc 02 §1):
    score(now) = strength · exp(−λ · Δt),   Δt = max(0, now − last_update)  [days]
    λ = ln(2) / half_life_days
On a confirmed use:  strength = min(strength_max, score(now) + boost(role)·recall_bonus)
Δt is computed in DAYS (clamp_dt returns seconds; we divide by SECONDS_PER_DAY) so λ
stays the natural ln(2)/half_life_days.
"""
from __future__ import annotations

import math
import sys
from typing import Any

import mnx_common


def lam(half_life_days: float) -> float:
    """Decay constant λ = ln(2) / half_life_days."""
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    return math.log(2.0) / float(half_life_days)


def half_life_for(node_type: str, cfg: dict[str, Any]) -> float:
    """Half-life for a node type. domain → H; pattern → H·(1+pattern_halflife_bonus)."""
    H = float(cfg["half_life_days"])
    if node_type == "pattern":
        return H * (1.0 + float(cfg.get("pattern_halflife_bonus", 0.0)))
    return H


def lam_for(node_type: str, cfg: dict[str, Any]) -> float:
    return lam(half_life_for(node_type, cfg))


def score(strength: float, last_update: str, now: str, lam_value: float) -> float:
    """Live relevance = strength · exp(−λ · Δt_days). Non-increasing in Δt."""
    dt_days = mnx_common.clamp_dt(last_update, now) / mnx_common.SECONDS_PER_DAY
    return float(strength) * math.exp(-lam_value * dt_days)


def boost(role: str, cfg: dict[str, Any]) -> float:
    """Stamp weight for a role: contributed | consulted | traversed."""
    return float(cfg.get("boost", {}).get(role, 0.0))


def recall_bonus(prev_score: float, cfg: dict[str, Any]) -> float:
    """Spaced-repetition multiplier in [1, cold_recall_multiplier], strictly larger
    for a lower prev_score (reviving a cold node rewards harder than refreshing a hot one)."""
    mult = float(cfg.get("cold_recall_multiplier", 1.6))
    smax = float(cfg.get("strength_max", 1.0)) or 1.0
    frac = max(0.0, min(1.0, prev_score / smax))
    return 1.0 + (mult - 1.0) * (1.0 - frac)


def apply_use(strength: float, last_update: str, now: str, role: str,
              node_type: str, cfg: dict[str, Any]) -> tuple[float, str]:
    """Return (new_strength, now) after a confirmed use. SATURATING at strength_max."""
    s = score(strength, last_update, now, lam_for(node_type, cfg))
    gained = s + boost(role, cfg) * recall_bonus(s, cfg)
    return min(float(cfg.get("strength_max", 1.0)), gained), now


def tier_of(score_value: float, rank_in_cluster: int, cfg: dict[str, Any]) -> str:
    """'hot' iff rank_in_cluster < hot_k; else 'warm' iff score >= warm_band; else 'cold'."""
    if rank_in_cluster < int(cfg.get("hot_k", 12)):
        return "hot"
    if score_value >= float(cfg.get("warm_band", 0.25)):
        return "warm"
    return "cold"


def retention(score_value: float, structural_strength: float, cfg: dict[str, Any]) -> float:
    """Combine usage score and structural strength. A node is demote/death-eligible
    only when BOTH are low, so retention is high if EITHER force is high."""
    return max(float(score_value), float(structural_strength))


def struct_g(weighted_in_degree: float, cfg: dict[str, Any]) -> float:
    """Map a LIVENESS-WEIGHTED in-degree to a structural strength in [0, strength_max].

    `weighted_in_degree` is Σ over referrers of each referrer's liveness weight (usage
    score), NOT a raw count — so a node propped up only by cold/dead referrers gets little
    structural strength (self-cleaning, W6). The map SATURATES (1 − e^(−w/scale)) so a hub
    can never become *structurally* immortal from sheer fan-in — the deterministic dual of
    the usage-side `strength_max` saturation that prevents immortal nodes (F3).
    """
    smax = float(cfg.get("strength_max", 1.0))
    scale = float(cfg.get("struct_scale", 2.0))
    w = max(0.0, float(weighted_in_degree))
    if scale <= 0:
        return smax if w > 0 else 0.0
    return smax * (1.0 - math.exp(-w / scale))


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "lam":
            return mnx_common.emit({"lam": lam(float(argv[2]))})
        if cmd == "score":
            # score <strength> <last_update> <now> <half_life_days>
            lv = lam(float(argv[5]))
            return mnx_common.emit({"score": score(float(argv[2]), argv[3], argv[4], lv)})
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
