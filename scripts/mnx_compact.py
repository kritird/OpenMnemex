"""mnx_compact.py — registry replay + checkpoint (the LSM merge).

STATUS: v0.1.0 CONTRACT STUB. See docs/02-architecture.md, docs/06-script-contracts.md.
"""
from __future__ import annotations
from typing import Any


def read_highwater(cluster: str) -> str:
    """Return the high-water mark (ts/line) replayed up to for this cluster."""
    raise NotImplementedError


def deltas_after(cluster: str, mark: str) -> list[dict]:
    """Return registry entries after the mark: [{id, ts, role, weight}]."""
    raise NotImplementedError


def fold(materialized_state: dict[str, Any], deltas: list[dict], cfg: dict[str, Any], now: str) -> dict[str, Any]:
    """Pure: fold deltas (in ts order) onto materialized strengths → new state.
    Invariant: order-independent over same-id deltas applied in ts order.
    """
    raise NotImplementedError


def advance_highwater(cluster: str, mark: str) -> None:
    """Advance the checkpoint. Invariant: only moves forward; does NOT truncate
    registry lines below an unconfirmed mark (no lost stamps; F2).
    """
    raise NotImplementedError


def overdue(team: str, cfg: dict[str, Any], now: str) -> dict:
    """Return {due: bool, days_overdue: int, config_drift: bool} for the
    maintenance-due warning (mnx-read warns, never acts).
    """
    raise NotImplementedError
