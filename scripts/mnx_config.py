"""mnx_config.py — config load, derivation, version stamping, re-normalization.

STATUS: v0.1.0 CONTRACT STUB. See docs/07-configuration.md, docs/06.
"""
from __future__ import annotations
from typing import Any


def load(repo: str) -> dict[str, Any]:
    """Parse mnemex.config.md front-matter; apply defaults. Deterministic."""
    raise NotImplementedError


def derive(cfg: dict[str, Any]) -> dict[str, Any]:
    """Compute derived values: λ_domain, λ_pattern (from pattern_halflife_bonus), etc."""
    raise NotImplementedError


def version(cfg: dict[str, Any]) -> int:
    """Return config_version."""
    raise NotImplementedError


def stamp(team: str, cfg: dict[str, Any]) -> None:
    """Write config_version + λ in force to .mnemex/ for this team."""
    raise NotImplementedError


def changed_since_last_compaction(team: str, cfg: dict[str, Any]) -> bool:
    """True iff config_version/λ differs from the last-compaction stamp (drift)."""
    raise NotImplementedError


def renormalize(scope: str, old_lam: float, new_lam: float, now: str) -> dict:
    """Plan a one-time recompute of stored strengths so each node's LIVE score is
    continuous across a parameter change (score_new(now) == score_old(now)). F5.
    Runs before any tier decision in the next gc.
    """
    raise NotImplementedError
