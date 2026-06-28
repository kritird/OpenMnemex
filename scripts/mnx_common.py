"""mnx_common.py — shared primitives for the Mnemex Protocol.

The single source of truth for time, parsing, and id rules. Every other Mnemex
script imports from here; nothing else writes a timestamp or mints an id.

STATUS: v0.1.0 CONTRACT STUB — signatures and invariants are final; bodies are
not yet implemented. See docs/06-script-contracts.md.

Dependencies: Python 3.9+ stdlib + PyYAML only.
"""
from __future__ import annotations
from typing import Any


def now_utc() -> str:
    """Return the current time as an ISO-8601 UTC string (second precision).

    This is the ONLY timestamp source in the protocol. Invariant: never returns
    a local-time or naive timestamp; always 'YYYY-MM-DDTHH:MM:SSZ'.
    """
    raise NotImplementedError


def parse_node(path: str) -> dict[str, Any]:
    """Parse a node file into a dict with keys: id, type, title, summary, aliases,
    domain, status, confidence, trigger, edges, references, provenance, created,
    updated, and body sections. Reject malformed front-matter rather than guessing.
    """
    raise NotImplementedError


def parse_index(path: str) -> dict[str, Any]:
    """Parse an index.md into {description, children[], hot[], warm[], cold[]}."""
    raise NotImplementedError


def read_chunk(path: str, section: str) -> str:
    """Ranged read of a single labeled section: one of
    'head' | 'hot' | 'warm' | 'cold' | 'body'. Used to keep reads chunked.
    """
    raise NotImplementedError


def slugify(title: str) -> str:
    """Return a candidate id slug ([a-z0-9-]+). Caller ensures uniqueness."""
    raise NotImplementedError


def is_valid_id(s: str) -> bool:
    """True iff s is a valid stable id slug ([a-z0-9-]+, no spaces)."""
    raise NotImplementedError


def clamp_dt(t_from: str, t_to: str) -> float:
    """Return max(0.0, seconds between two ISO-8601 UTC timestamps). Used wherever
    Δt appears so clock skew can never invert decay into growth.
    """
    raise NotImplementedError
