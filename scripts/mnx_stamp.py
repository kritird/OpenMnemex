"""mnx_stamp.py — usage-stamp recording with a reset-durable spill.

Background: docs/architecture.md §2 (the registry is the WAL), USER-JOURNEY-FINDINGS #2.

A usage stamp is one append-only registry line `{id} {ts} {role} {weight}`. For a
git-remote graph the local clone is hard-reset to origin/HEAD every session start
(mnx_binding.sync), so a stamp written straight to the registry dies unless it is
committed AND pushed. To make stamping durable and quiet, remote stamps are written
to a session-durable SPILL outside the clone (co-located with capture staging atoms under
<mnemex home>/staging/<graph-slug>/ — mnx_common.mnemex_home(), see docs/11) and flushed to the registry + pushed
in one batch at end of turn / session (mnx_hooks stop / session-end).

Invariant: the spill is the source of truth for un-pushed stamps; the registry is
rebuilt from HEAD + spill at flush; the spill is cleared only on a confirmed push.
Because session-start resets the registry to HEAD, replay is idempotent across
sessions; the flush also removes exact spill-line matches before re-appending, so a
retried flush within a session cannot duplicate.

Local kinds (git-local / plain-local) are never reset, so stamps are appended straight
to the registry (already durable) and flush is a noop.

Dependencies: Python 3.9+ stdlib + PyYAML only (via mnx_binding). See docs/06.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import mnx_binding
import mnx_common


# --- spill location (outside the clone, survives reset --hard) ---------------

def _spill_path(binding) -> Path:
    """Co-located with the capture staging atoms under staging/<graph-slug>/ (docs/11).

    Kept OUTSIDE the clone so it survives the session-start hard-resync; same slug as
    mnx_stage so one folder per graph holds both the staged atoms and the stamp spill."""
    return mnx_binding.staging_path_for(binding) / "stamps.jsonl"


def _cluster_rel(binding, cluster: str) -> str:
    root = Path(binding.graph_root()).resolve()
    c = Path(cluster).resolve()
    return c.relative_to(root).as_posix()  # raises if cluster is outside the graph


def _fmt(nid: str, ts: str, role: str, weight: float) -> str:
    return f"{nid} {ts} {role} {weight:g}"


# --- append -----------------------------------------------------------------

def append(cluster: str, nid: str, role: str,
           weight: float = 1.0, ts: Optional[str] = None) -> dict[str, Any]:
    """Record one usage stamp against `cluster`'s registry, durably for its graph kind."""
    binding = mnx_binding.resolve()
    if binding is None:
        return {"action": "error", "message": "No Mnemex graph configured."}
    ts = ts or mnx_common.now_utc()
    rel = _cluster_rel(binding, cluster)
    line = _fmt(nid, ts, role, weight)

    if binding.kind() == "git-remote":
        sp = _spill_path(binding)
        sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"cluster_rel": rel, "line": line}) + "\n")
        return {"action": "spilled", "id": nid, "ts": ts, "spill": str(sp)}

    # local kinds: never reset — append straight to the registry (already durable)
    reg = Path(binding.graph_root()) / rel / mnx_common.REGISTRY_FILENAME
    with reg.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return {"action": "appended", "id": nid, "ts": ts, "registry": str(reg)}


# --- flush ------------------------------------------------------------------

def _read_spill(sp: Path) -> list[dict[str, str]]:
    if not sp.is_file():
        return []
    out = []
    for ln in sp.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    return out


def _idempotent_append(reg: Path, lines: list[str]) -> None:
    """Append `lines`, first dropping any exact duplicates so a retried flush can't dup."""
    existing = reg.read_text(encoding="utf-8").splitlines() if reg.is_file() else []
    drop = set(lines)
    kept = [l for l in existing if l.strip() not in drop]
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text("\n".join(kept + lines) + "\n", encoding="utf-8")


def flush(message: str = "read: usage stamps (batched)") -> dict[str, Any]:
    """Replay the spill into the registries and persist; clear the spill only on success."""
    binding = mnx_binding.resolve()
    if binding is None:
        return {"action": "noop", "reason": "no graph configured"}
    if binding.kind() != "git-remote":
        return {"action": "noop", "reason": "local graph; stamps already durable"}

    sp = _spill_path(binding)
    records = _read_spill(sp)
    if not records:
        return {"action": "noop", "pending": 0}

    root = Path(binding.graph_root())
    by_cluster: dict[str, list[str]] = {}
    for r in records:
        by_cluster.setdefault(r["cluster_rel"], []).append(r["line"])
    for rel, lines in by_cluster.items():
        _idempotent_append(root / rel / mnx_common.REGISTRY_FILENAME, lines)

    res = mnx_binding.persist(binding, message)
    durable = res.get("push") == "ok" or res.get("action") == "nothing-to-commit"
    # `res` carries persist's own "action" (e.g. "committed"); spread it FIRST so our
    # flushed/deferred label wins, and rename persist's action to "persist".
    out = {**res, "persist": res.get("action"),
           "pending": len(records), "clusters": len(by_cluster)}
    if durable:
        try:
            sp.unlink(missing_ok=True)
        except Exception:
            pass
        out["action"] = "flushed"
        return out
    out["action"] = "deferred"
    out["reason"] = "push not confirmed; spill retained"
    return out


def status() -> dict[str, Any]:
    binding = mnx_binding.resolve()
    if binding is None:
        return {"resolved": False}
    kind = binding.kind()
    if kind != "git-remote":
        return {"resolved": True, "kind": kind, "pending": 0, "durability": "on-disk"}
    sp = _spill_path(binding)
    return {"resolved": True, "kind": kind, "pending": len(_read_spill(sp)), "spill": str(sp)}


# --- cli --------------------------------------------------------------------

def _arg(argv: list[str], flag: str) -> Optional[str]:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


_USAGE = [
    'mnx_stamp.py append --cluster <dir> --id <node> [--role <r>] [--weight <w>]  — queue a usage stamp',
    'mnx_stamp.py flush [--message <m>]   — write queued stamps through to the graph',
    'mnx_stamp.py status                  — queued-stamp state',
]
_FLAGS = {"--cluster": True, "--id": True, "--role": True, "--weight": True, "--message": True}


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE, _FLAGS)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "append":
            cluster = _arg(argv, "--cluster")
            nid = _arg(argv, "--id")
            role = _arg(argv, "--role") or "contributed"
            w = _arg(argv, "--weight")
            if not cluster or not nid:
                return mnx_common.emit({"error": "append needs --cluster and --id"}, ok=False)
            res = append(cluster, nid, role, float(w) if w else 1.0)
            return mnx_common.emit(res, ok=res.get("action") != "error")
        if cmd == "flush":
            return mnx_common.emit(flush(_arg(argv, "--message") or "read: usage stamps (batched)"))
        if cmd == "status":
            return mnx_common.emit(status())
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
