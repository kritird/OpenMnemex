"""mnx_resolve.py — id↔path resolution and the reverse-edge map.

The shared resolver used by read, write, and gc; they must all agree.
STATUS: v0.1.0 CONTRACT STUB. See docs/06-script-contracts.md.
"""
from __future__ import annotations
from typing import Any


def resolve(node_id: str, scope: str) -> str | None:
    """id → path. Local index for intra-cluster; team cross-links.md for
    cross-cluster; None if absent.
    """
    raise NotImplementedError


def build_reverse_map(scope: str) -> dict[str, list[str]]:
    """Map node_id → [referrer_ids], from node edges + cross-links.md.
    Invariant: COLD AND TOMBSTONED nodes are INCLUDED (logical tiering safety).
    """
    raise NotImplementedError


def in_degree(node_id: str, reverse_map: dict[str, list[str]], cross_links: Any) -> tuple[int, int]:
    """Return (local_in_degree, cross_cluster_in_degree). Soft cross-team
    'references' are EXCLUDED.
    """
    raise NotImplementedError


def referrers(node_id: str, reverse_map: dict[str, list[str]], cross_links: Any) -> list[dict]:
    """Return [{id, path}] of nodes pointing at node_id — for transactional severing."""
    raise NotImplementedError


def sole_referrer_of(node_id: str, reverse_map: dict[str, list[str]]) -> list[str]:
    """Return ids of live nodes whose ONLY inbound edge is from node_id
    (sole-referrer reluctance; prevents orphan cascade).
    """
    raise NotImplementedError
