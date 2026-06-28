"""mnx_doctor.py — invariant checks + self-heal of derived files.

STATUS: v0.1.0 CONTRACT STUB. See docs/08-invariants-and-failure-modes.md (Part A).
"""
from __future__ import annotations
from typing import Any


def check(scope: str) -> dict:
    """Read-only. Run the full invariant suite; return a Report:
    {findings: [{invariant, severity('E'|'W'|'I'), node_or_edge, detail}]}.
    """
    raise NotImplementedError


def fix(scope: str) -> dict:
    """Regenerate DERIVED files (index, reverse map, cross-links) from the nodes.
    Invariants: idempotent; never alters node knowledge — only navigation/telemetry.
    """
    raise NotImplementedError
