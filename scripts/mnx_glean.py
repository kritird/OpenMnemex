"""mnx_glean.py — the bounded "what did I miss?" recall primitive (Gleanings).

Background: docs/corpus-ingestion.md §8 (GraphRAG *gleanings*), INGESTION-BUILD-PLAN.md phase G0.

Gleaning is a bounded, source-agnostic recall pass that lifts *extraction completeness* for both
episodic capture and corpus ingest. The **judgment** — "what durable fact/entity did I not stage
yet?" — always stays in the skill/LLM; this script owns only the mechanical half: bound the loop
(never run forever) and bookkeep it (did the last pass add anything; which enumerated units are
still empty). It writes nothing and reads no graph.

Two modes, matching the asymmetry between the two sources:

  * guardrail (light) — episodic capture. Pure before/after bookkeeping: keep capture cheap, so
    there is no coverage checklist, only a "did the last pass add a new atom" signal.
        step(before, after, pass_no, max_passes) -> {pass, added, stop, reason}
  * checklist (rich) — ingest. A real coverage map over an enumerated unit set: a unit is COVERED
    when ≥1 staged atom carries its `anchor` in provenance; the loop re-examines the uncovered.
        coverage(units, staged, pass_no, max_passes) -> {total, covered, uncovered, stop, reason}

Both stop on no-progress / full-coverage OR at the pass cap (config `max_glean_passes`, default 2).

Dependencies: Python 3.9+ stdlib only (JSON I/O). Imports mnx_common for emit. See docs/script-contracts.md.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Optional

import mnx_common

DEFAULT_MAX_PASSES = 2


def _anchor_of(obj: Any) -> Optional[str]:
    """The coverage key of a unit or a staged atom: its `anchor` (top-level or under provenance),
    NORMALIZED so both sides of the match agree regardless of authoring style — probe units carry
    bare heading text while extractors often write the `## Heading` line (live E2E 2026-07-12,
    finding G6): leading `#`s stripped, whitespace trimmed, case-folded."""
    if not isinstance(obj, dict):
        return None
    raw = obj.get("anchor")
    if not raw:
        prov = obj.get("provenance")
        if isinstance(prov, dict):
            raw = prov.get("anchor")
    if not raw:
        return None
    return re.sub(r"^#+\s*", "", str(raw).strip()).casefold() or None


# --- guardrail mode (episodic capture) --------------------------------------

def step(before: int, after: int, pass_no: int, max_passes: int = DEFAULT_MAX_PASSES) -> dict[str, Any]:
    """One guardrail tick. `added` = atoms staged this pass; stop on no-progress or at the cap.

    no-progress takes precedence over cap (it is the more informative reason): a pass that added
    nothing has converged regardless of how many passes remain."""
    added = int(after) - int(before)
    if added <= 0:
        stop, reason = True, "no-progress"
    elif int(pass_no) >= int(max_passes):
        stop, reason = True, "cap"
    else:
        stop, reason = False, "continue"
    return {"pass": int(pass_no), "added": added, "stop": stop, "reason": reason}


# --- checklist mode (ingest) ------------------------------------------------

def coverage(units: list[dict[str, Any]], staged: list[dict[str, Any]],
             pass_no: int, max_passes: int = DEFAULT_MAX_PASSES) -> dict[str, Any]:
    """Coverage of an enumerated unit set by the staged ledger.

    A unit is COVERED when ≥1 staged atom carries the unit's `anchor` in its provenance. Returns the
    still-uncovered unit ids (the re-ask worklist) + the same stop signal: stop when every unit is
    covered (complete) OR at the pass cap; otherwise continue re-examining the uncovered."""
    staged_anchors = {a for a in (_anchor_of(s) for s in (staged or [])) if a}
    uncovered: list[str] = []
    for u in (units or []):
        anchor = _anchor_of(u)
        uid = str(u.get("id") or anchor or "")
        if anchor is None or anchor not in staged_anchors:
            uncovered.append(uid)
    total = len(units or [])
    covered = total - len(uncovered)
    if not uncovered:
        stop, reason = True, "complete"
    elif int(pass_no) >= int(max_passes):
        stop, reason = True, "cap"
    else:
        stop, reason = False, "continue"
    return {"total": total, "covered": covered, "uncovered": uncovered,
            "pass": int(pass_no), "stop": stop, "reason": reason}


# --- cli --------------------------------------------------------------------

def _arg(argv: list[str], flag: str) -> Optional[str]:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def _load_json(path: Optional[str]) -> list[dict[str, Any]]:
    if not path:
        return []
    data = json.loads(open(path, encoding="utf-8").read())
    # accept either a bare list or {"units": [...]} / {"atoms": [...]}
    if isinstance(data, dict):
        for key in ("units", "atoms", "staged"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


_USAGE = [
    'mnx_glean.py step --before <n> --after <n> [--pass <n>] [--max <n>]  — should another glean pass run?',
    'mnx_glean.py coverage --units <units.json> --staged <staged.json> [--pass <n>] [--max <n>]  — per-unit extraction coverage',
]
_FLAGS = {"--max": True, "--pass": True, "--before": True, "--after": True, "--units": True, "--staged": True}


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE, _FLAGS)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else ""
    max_passes = int(_arg(argv, "--max") or DEFAULT_MAX_PASSES)
    pass_no = int(_arg(argv, "--pass") or 1)
    try:
        if cmd == "step":
            before = int(_arg(argv, "--before") or 0)
            after = int(_arg(argv, "--after") or 0)
            return mnx_common.emit(step(before, after, pass_no, max_passes))
        if cmd == "coverage":
            units = _load_json(_arg(argv, "--units"))
            staged = _load_json(_arg(argv, "--staged"))
            return mnx_common.emit(coverage(units, staged, pass_no, max_passes))
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
