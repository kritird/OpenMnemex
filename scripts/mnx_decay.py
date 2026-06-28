"""mnx_decay.py — the relevance math (pure functions, no I/O).

Deterministic given inputs. STATUS: v0.1.0 CONTRACT STUB.
See docs/02-architecture.md and docs/06-script-contracts.md.
"""
from __future__ import annotations
from typing import Any


def lam(half_life_days: float) -> float:
    """Decay constant λ = ln(2) / half_life_days."""
    raise NotImplementedError


def half_life_for(node_type: str, cfg: dict[str, Any]) -> float:
    """Half-life for a node type. domain → H; pattern → H·(1+pattern_halflife_bonus)."""
    raise NotImplementedError


def score(strength: float, last_update: str, now: str, lam_value: float) -> float:
    """Live relevance = strength · exp(−λ · clamp_dt(last_update, now)).

    Invariant: monotonically non-increasing in Δt.
    """
    raise NotImplementedError


def boost(role: str, cfg: dict[str, Any]) -> float:
    """Stamp weight for a role: contributed | consulted | traversed."""
    raise NotImplementedError


def recall_bonus(prev_score: float, cfg: dict[str, Any]) -> float:
    """Spaced-repetition multiplier: strictly larger for a lower prev_score
    (reviving a cold node rewards harder than refreshing a hot one).
    """
    raise NotImplementedError


def apply_use(strength: float, last_update: str, now: str, role: str,
              node_type: str, cfg: dict[str, Any]) -> tuple[float, str]:
    """Return (new_strength, now) after a confirmed use.

    new_strength = min(strength_max, score(...) + boost(role)·recall_bonus(...)).
    Invariant: SATURATING — never exceeds strength_max (no immortal nodes).
    """
    raise NotImplementedError


def tier_of(score_value: float, rank_in_cluster: int, cfg: dict[str, Any]) -> str:
    """Return 'hot' | 'warm' | 'cold'. hot iff rank_in_cluster < hot_k; else warm
    iff score >= warm_band; else cold.
    """
    raise NotImplementedError


def retention(score_value: float, structural_strength: float, cfg: dict[str, Any]) -> float:
    """Combine usage score and structural strength into a retention value.
    A node is demote/death-eligible only when BOTH inputs are low.
    """
    raise NotImplementedError
