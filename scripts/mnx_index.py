"""mnx_index.py — index regeneration (derived from truth; never the reverse).

STATUS: v0.1.0 CONTRACT STUB. See docs/06-script-contracts.md.
"""
from __future__ import annotations
from typing import Any


def regenerate_index(cluster: str, materialized_state: dict[str, Any]) -> None:
    """Rebuild a cluster's index.md HOT/WARM/COLD sections from its nodes.
    Denormalize summary+aliases from the nodes; carry strength/last_update;
    enforce hot section length ≤ hot_k. Invariant: index node-set == folder node-set.
    """
    raise NotImplementedError


def denorm_check(cluster: str) -> list[dict]:
    """Return drift records where index.summary/aliases != node.summary/aliases."""
    raise NotImplementedError


def shard_index(cluster: str, by: str = "domain") -> dict:
    """Plan a split of a generated index past node_budget along a declared sub-key.
    NEVER moves node files. Returns a plan; escalates if a single sub-key overflows.
    """
    raise NotImplementedError
