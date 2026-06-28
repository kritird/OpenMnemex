"""mnx_lock.py — team lock + crash recovery.

STATUS: v0.1.0 CONTRACT STUB. See docs/02-architecture.md §9-10, docs/06.
"""
from __future__ import annotations
from typing import Any


class Busy(Exception):
    """Raised when the team lock is already held."""


def acquire(team: str) -> Any:
    """Acquire the team lock; return a handle. Raise Busy if held.
    Invariant: at most one holder per team.
    """
    raise NotImplementedError


def release(handle: Any) -> None:
    """Release a previously acquired lock handle."""
    raise NotImplementedError


def plan_path(team: str) -> str:
    """Path to the team's pass.plan.json."""
    raise NotImplementedError


def write_plan(team: str, plan: dict) -> None:
    """Persist a Phase-A maintenance plan (for crash recovery / replay)."""
    raise NotImplementedError


def read_plan(team: str) -> dict | None:
    """Return the persisted plan, or None."""
    raise NotImplementedError


def in_progress(team: str) -> bool:
    """True iff a pass plan is present (a pass is mid-flight)."""
    raise NotImplementedError


def recover(team: str) -> dict:
    """Inspect for a stranded pass + dirty tree. Return
    {dirty: bool, action: 'rollback'|'replay'|'none'}. Rollback = git checkout
    to last good commit (F10).
    """
    raise NotImplementedError
