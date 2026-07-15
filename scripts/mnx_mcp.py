"""mnx_mcp.py — the OpenMnemex stdio MCP server (multi-agent plan v2 §5, Phase 1).

One stdio server, spawned by the host agent, dead with the session — no port, no daemon.
Every tool is a thin shim over an importable engine function (in-process, no subprocess
fan-out); no logic lives here beyond schema validation, the session guards, and the
path-confinement check. Commit 1b added the binding/health surface (bind_status, status,
doctor_check, doctor_fix, init_graph); commit 1c added the read surface (read_frontier,
read_cluster, read_nodes, record_usage); commit 1d added the capture surface (capture_status,
capture_add, capture_drop, capture_discard_all, glean_step); commit 2b adds the promote
surface (promote_begin, promote_context, promote_apply, promote_retry_push, promote_abort)
plus the held-contradictions queue (held_list, held_release, held_drop) — thin sessions over
``mnx_promote`` (§6.2), the plan-transaction orchestrator built in commit 2a.

Session-level contracts implemented here (§5.2):

  * **Error contract** — a tool result is either ``{"ok": true, ...payload}`` or
    ``{"ok": false, "error": {"code", "message", "action"}}`` where ``action`` is the
    human-actionable next step. Never a traceback. No tool mutates anything when it
    returns an internal error.
  * **Sync-once** — the first graph-touching tool call per server process runs
    ``mnx_binding.sync`` (blocking, same as the SessionStart hook); later calls skip it.
    ``offline`` / ``skipped-dirty`` / ``skipped-unpushed`` are available-but-DEGRADED,
    never errors (E2E finding F11): results carry ``degraded: true`` plus the sync detail,
    and ``skipped-unpushed`` points at ``promote_retry_push``.
  * **Mute** — consent on MCP hosts is implicit-by-invocation, but the per-session
    opt-out marker (mnx_hooks) is still honored: every tool checks it first and returns a
    structured ``muted`` refusal. The session id is ``$MNEMEX_SESSION_ID`` when the host
    pins one, else the shared ``"default"`` session (what ``mnx_hooks.py opt-out`` with
    no ``--session`` toggles).
  * **Confinement** — every path a tool reads or writes must resolve (symlinks followed)
    inside the graph root, the per-author mnemex home, or the ingest cache — never the
    caller's CWD/project. ``confine()`` is the one shared guard.

The ``mcp`` SDK is an OPTIONAL extra (``pip install openmnemex[mcp]``, Python 3.10+); this
module must stay importable without it (the packaging bridge imports every engine module,
and the engine keeps its 3.9 floor), so the SDK import is soft and only ``serve()``/
``create_server()`` require it. ``serve`` failures print to stderr — stdout belongs to the
JSON-RPC protocol.

CLI:
    serve   — run the stdio server (default; blocks until the host disconnects)
    info    — server identity + SDK availability as JSON (never starts the server)

Run: ``uvx openmnemex-mcp`` (packaged) or ``python3 scripts/mnx_mcp.py`` (checkout).
"""
from __future__ import annotations

import functools
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import mnx_binding
import mnx_common
import mnx_config
import mnx_doctor
import mnx_glean
import mnx_hooks
import mnx_init
import mnx_promote
import mnx_read
import mnx_resolve
import mnx_stage
import mnx_stamp
import mnx_status

# Soft SDK import: the engine (and the packaging bridge, which imports every mnx_* module)
# must work on 3.9 / without the [mcp] extra. Only building/running the server needs it.
try:
    from mcp.server.fastmcp import FastMCP
    _MCP_IMPORT_ERROR: Optional[BaseException] = None
except Exception as _exc:  # ImportError, or SyntaxError on very old Pythons
    FastMCP = None  # type: ignore[assignment]
    _MCP_IMPORT_ERROR = _exc

SERVER_NAME = "openmnemex"
_INSTRUCTIONS = (
    "OpenMnemex context-graph memory: a Markdown-in-git knowledge graph — no daemon, no "
    "database, no vector store. Tools are added phase by phase; read/capture/promote "
    "procedures arrive with them."
)


def engine_version() -> str:
    """The engine's own version, single-sourced with pyproject.toml.

    The plugin manifest next to the running engine wins (a checkout may coexist with an
    older pip install); a wheel install has no manifest and uses its package metadata.
    """
    manifest = mnx_common.plugin_root().parent / ".claude-plugin" / "plugin.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return version("openmnemex")
    except Exception:
        return "0+unknown"


# --- error contract -----------------------------------------------------------

class ToolError(Exception):
    """A structured, host-renderable tool failure: code + message + actionable next step."""

    def __init__(self, code: str, message: str, action: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.action = action

    def to_result(self) -> dict[str, Any]:
        return err(self.code, str(self), self.action)


def ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **payload}


def err(code: str, message: str, action: Optional[str] = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if action:
        error["action"] = action
    return {"ok": False, "error": error}


# --- confinement (§5.2 security) ------------------------------------------------

class ConfinementError(ToolError):
    def __init__(self, path: str):
        super().__init__("confined",
                         f"Path resolves outside the graph root / mnemex home: {path}",
                         "use paths inside the bound graph or the mnemex home")


def confine(path: str | Path, roots: list[str | Path]) -> Path:
    """Resolve ``path`` (symlinks followed) and require it inside one of ``roots``.

    Returns the resolved Path or raises ConfinementError — the server never touches the
    caller's CWD/project, only the graph, the per-author home, and the ingest cache.
    """
    resolved = Path(path).expanduser().resolve()
    for root in roots:
        root_resolved = Path(root).expanduser().resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved
    raise ConfinementError(str(path))


def allowed_roots(binding: Optional["mnx_binding.Binding"]) -> list[Path]:
    """The confinement whitelist: graph root + mnemex home + ingest cache (env-relocatable)."""
    roots = [mnx_common.mnemex_home()]
    ingest_cache = os.environ.get("MNEMEX_INGEST_CACHE")
    if ingest_cache:
        roots.append(Path(ingest_cache).expanduser())
    if binding is not None:
        roots.append(Path(binding.graph_root()))
    return roots


# --- mute (§5.2 consent) ---------------------------------------------------------

def session_id() -> str:
    """The mute-marker key: host-pinned $MNEMEX_SESSION_ID, else the shared default session."""
    return os.environ.get("MNEMEX_SESSION_ID") or "default"


def is_muted() -> bool:
    return mnx_hooks.core_is_muted(session_id())


_MUTED_RESULT = dict(code="muted",
                     message="Mnemex is muted for this session (the user opted out).",
                     action="stop calling mnemex tools; the user can opt back in with "
                            "mnx_hooks.py opt-in")


# --- sync-once (§5.2 session sync) ------------------------------------------------

# Sync actions that leave the graph usable but stale/local-only. NEVER errors (F11);
# unknown/new actions must also never be mapped to the error branch — that exact bug
# lived in the Claude hook adapter until the 2026-07-12 fix cycle.
_DEGRADED_HINTS = {
    "offline": None,
    "skipped-dirty": "persist or discard the local work, then resync",
    "skipped-unpushed": "run promote_retry_push",
}

_session_state: dict[str, Any] = {"synced": False, "sync": None}


def reset_session_state() -> None:
    """Forget the once-per-process sync (tests; a real server process never needs it)."""
    _session_state["synced"] = False
    _session_state["sync"] = None


def _resolve_binding() -> "mnx_binding.Binding":
    try:
        binding = mnx_binding.resolve()
    except Exception as exc:  # malformed binding file — report, don't traceback
        raise ToolError("binding-error", str(exc),
                        "fix or remove the malformed binding file") from exc
    if binding is None:
        raise ToolError("unresolved", "No Mnemex graph configured for this project or user.",
                        "run init_graph")
    return binding


def ensure_synced() -> dict[str, Any]:
    """Resolve the binding and sync the graph clone, once per server process.

    Returns ``{binding, sync}`` where ``sync`` carries ``degraded``/``offline_degraded``
    flags for tools to surface. A hard sync failure (missing local folder, clone failed
    with no local copy) raises ToolError and does NOT cache, so the next call retries.
    """
    binding = _resolve_binding()
    if _session_state["synced"]:
        return {"binding": binding, "sync": _session_state["sync"]}
    result = mnx_binding.sync(binding)
    action = result.get("action")
    if action == "error":
        raise ToolError("sync-failed", result.get("message", "Graph sync failed."),
                        "check the graph path/remote or run init_graph")
    sync_info: dict[str, Any] = {"action": action, "message": result.get("message"),
                                 "degraded": action in _DEGRADED_HINTS,
                                 "offline_degraded": action == "offline"}
    if _DEGRADED_HINTS.get(action):
        sync_info["next_step"] = _DEGRADED_HINTS[action]
    _session_state["synced"] = True
    _session_state["sync"] = sync_info
    return {"binding": binding, "sync": sync_info}


# --- the tool guard ---------------------------------------------------------------

def tool_guard(sync_first: bool = True) -> Callable:
    """Wrap an engine-calling tool body in the session contracts, in order:
    mute check → (optional) sync-once → run → shape the result; exceptions become the
    structured error result, never a traceback. The wrapped body returns a plain payload
    dict; graph-touching bodies receive ``binding=`` and ``sync=`` keyword arguments.
    """
    def deco(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                if is_muted():
                    return err(**_MUTED_RESULT)
                if sync_first:
                    session = ensure_synced()
                    kwargs.setdefault("binding", session["binding"])
                    kwargs.setdefault("sync", session["sync"])
                    payload = fn(*args, **kwargs)
                    if session["sync"].get("degraded"):
                        payload.setdefault("degraded", True)
                        payload.setdefault("sync", session["sync"])
                    return ok(payload)
                return ok(fn(*args, **kwargs))
            except ToolError as te:
                return te.to_result()
            except Exception as exc:  # never a traceback over the wire
                return err("internal", f"{type(exc).__name__}: {exc}",
                           "report this; the graph was not modified by this call")
        return wrapper
    return deco


# --- tool bodies (Phase 1 commit 1b: binding / health) ------------------------------
#
# Each body is a plain function returning a payload dict; the guard wraps it in the
# {ok,...}/{ok:false,error} envelope and injects binding=/sync= for graph-touching tools.
# The MCP-facing wrappers (registered on the server) expose ONLY the user-facing params —
# binding/sync never leak into a tool's public schema.

@tool_guard()
def _bind_status(binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Which graph resolved, how, and whether the clone is present + has unpushed work."""
    root = Path(binding.graph_root())
    present = mnx_binding._is_git_repo(root) if binding.remote else root.is_dir()
    payload: dict[str, Any] = {**binding.to_dict(), "clone_present": present}
    if present and binding.remote:
        try:  # surface a committed-but-unpushed promote so the host can offer retry_push
            payload.update(mnx_binding.unpushed_state(binding))
        except Exception:
            pass
    return payload


@tool_guard()
def _status(binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """The full at-a-glance snapshot: tiers per team, staged/held counts, pending stamps, health."""
    return mnx_status.status()


@tool_guard()
def _doctor_check(binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Read-only invariant suite over the resolved graph. Never mutates."""
    return mnx_doctor.check(binding.graph_root())


@tool_guard()
def _doctor_fix(confirm: bool = False, binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Regenerate derived files (indexes, cross-links, phonebook) from node truth. Mutating —
    refuses without ``confirm`` so a stray call can never rewrite the graph."""
    if not confirm:
        raise ToolError("confirm-required",
                        "doctor_fix rewrites derived files; it needs explicit confirmation.",
                        "call doctor_fix again with confirm=true")
    return mnx_doctor.fix(binding.graph_root())


@tool_guard(sync_first=False)
def _init_graph(remote: Optional[str] = None, path: Optional[str] = None,
                team: str = mnx_init.DEFAULT_TEAM, org: Optional[str] = None) -> dict[str, Any]:
    """Scaffold a brand-new graph (the one tool that ESTABLISHES a root, so it does not
    sync-first an existing binding). Probes a remote read-only before touching it, installs
    the merge driver, stamps config, and doctor-checks — a fresh graph is clean on day one."""
    try:
        return mnx_init.init_graph(remote=remote, path=path, team=team, org=org)
    except mnx_init._InitError as ie:
        raise ToolError(ie.code, str(ie), ie.action) from ie


# --- tool bodies (Phase 1 commit 1c: read) ------------------------------------------
#
# Deterministic mechanics only (mnx_read.py, §6.1) — routing (which team/cluster matches
# the request), the stop-early tier judgment, and disposing loaded bodies in the usage
# manifest all stay host/model judgment (the mnx-read SKILL / procedure prompt).

@tool_guard()
def _read_frontier(binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Org head + team heads (descriptions + child cluster descriptions), plus the
    graph-wide consolidation-overdue warning. Never tier rows — call read_cluster next."""
    return mnx_read.frontier(binding.graph_root(), mnx_common.now_utc())


@tool_guard()
def _read_cluster(cluster: str, tiers: Optional[list[str]] = None,
                  binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """A cluster's Hot/Warm/Cold tier tables (`stale` flagged per row) + the staged-capture
    overlay for that cluster's domain. `cluster` is confined to the resolved graph root."""
    candidate = Path(cluster)
    if not candidate.is_absolute():
        candidate = Path(binding.graph_root()) / candidate
    confined = confine(candidate, allowed_roots(binding))
    try:
        return mnx_read.scan(confined, mnx_common.now_utc(), tiers=tiers, binding=binding)
    except ValueError as ve:
        raise ToolError("not-a-cluster", str(ve),
                        "call read_frontier for valid cluster paths") from ve


@tool_guard()
def _read_nodes(ids: list[str], max_bytes: Optional[int] = None,
                binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Node bodies for `ids` + each node's governed-by pattern companions, budget-capped.
    Refuses `stg-` ids — those bodies already came from read_cluster's overlay."""
    return mnx_read.expand(ids, binding.graph_root(), max_bytes)


@tool_guard()
def _record_usage(manifest: list[dict[str, Any]],
                  binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Append one usage stamp per manifest entry against its node's home-cluster registry.
    `traversed` entries are accepted and silently ignored (traversed boost = 0). Remote
    graphs flush the spill immediately — there is no Stop hook to batch it on MCP hosts."""
    root = str(binding.graph_root())
    stamped: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for entry in manifest:
        nid = entry.get("id")
        role = entry.get("role", "contributed")
        if role == "traversed":
            ignored.append({"id": nid, "role": role})
            continue
        path = mnx_resolve.resolve(nid, root)
        if not path:
            errors.append({"id": nid, "message": "unknown node id"})
            continue
        weight = entry.get("weight")
        res = mnx_stamp.append(str(Path(path).parent), nid, role,
                               float(weight) if weight is not None else 1.0)
        stamped.append({"id": nid, "role": role, **res})
    out: dict[str, Any] = {"stamped": stamped, "ignored": ignored, "errors": errors}
    if stamped and binding.kind() == "git-remote":
        out["flush"] = mnx_stamp.flush()
    return out


# --- tool bodies (Phase 1 commit 1d: capture) ---------------------------------------
#
# Straight passthroughs to mnx_stage (the local, per-author staging tier) and mnx_glean
# (the bounded guardrail loop). Extraction/scoring judgment stays with the host model
# (the mnx-capture SKILL / capture-procedure prompt) — these tools only stage, list,
# drop, and bound the loop.

@tool_guard()
def _capture_status(binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Staging budget level (soft/hard) plus the full staged ledger (id·type·score·summary·
    age) — the delta ledger the capture procedure walks each pass."""
    st = mnx_stage.status(binding=binding)
    st["atoms"] = mnx_stage.list_atoms(binding=binding)["atoms"]
    return st


@tool_guard()
def _capture_add(type: str = "domain", summary: str = "", aliases: Optional[list[str]] = None,
                 domain: Optional[list[str]] = None, trigger: Optional[str] = None,
                 score: str = "later", urgent: bool = False, volatility: Any = "default",
                 provenance: Optional[dict[str, Any]] = None, body: str = "",
                 binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Stage one atom. Idempotent by content hash (a re-capture of identical content is a
    no-op restage, not a duplicate). Refuses a NEW atom once the session batch is past the
    hard budget — the refusal message names both ways out (promote to drain, or drop/
    discard-all to make room). node_body_max_chars is a SOFT budget (not enforced by the
    doctor gate) — a body over it is flagged in the result as `over_budget`; the host is
    expected to split it into multiple linked atoms rather than stage one oversized body."""
    atom = {"type": type, "summary": summary, "aliases": aliases, "domain": domain,
            "trigger": trigger, "score": score, "urgent": urgent, "volatility": volatility,
            "provenance": provenance or {}, "body": body}
    try:
        result = mnx_stage.add(atom)
    except ValueError as ve:
        raise ToolError("invalid-atom", str(ve), "fix the atom fields and retry") from ve
    cap = int(mnx_config.load(binding.graph_root()).get("node_body_max_chars", 6000))
    if len(body) > cap:
        result["over_budget"] = {"chars": len(body), "cap": cap,
                                 "note": "not enforced — split into multiple linked atoms "
                                         "before promoting, or the body lands unsplit"}
    return result


@tool_guard()
def _capture_drop(pid: str, binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Drop one staged atom by its provisional id. Not-found is a noop, not an error."""
    return mnx_stage.clear_one(pid, binding=binding)


@tool_guard()
def _capture_discard_all(confirm: bool = False, binding: Any = None,
                         sync: Any = None) -> dict[str, Any]:
    """Discard the ENTIRE staged batch for this graph. Mutating — refuses without confirm."""
    if not confirm:
        raise ToolError("confirm-required",
                        "capture_discard_all removes every staged atom for this graph; it "
                        "needs explicit confirmation.",
                        "call capture_discard_all again with confirm=true")
    return mnx_stage.clear(binding=binding)


@tool_guard(sync_first=False)
def _glean_step(before: int, after: int, pass_no: int,
                max_passes: int = mnx_glean.DEFAULT_MAX_PASSES) -> dict[str, Any]:
    """One guardrail tick of the capture glean loop: did the last pass add a new staged atom,
    and should another pass run. Pure computation — no binding, no staging access."""
    return mnx_glean.step(before, after, pass_no, max_passes)


# --- tool bodies (Phase 2 commit 2b: promote) ----------------------------------------
#
# Thin sessions over mnx_promote (§6.2) — the plan-transaction orchestrator built in commit
# 2a. The host does the reconcile judgment (dispositions in the plan); these tools only
# guard the call (mute/sync) and translate mnx_promote's ValueErrors into the error contract.
# No `team` parameter is exposed — every tool uses the resolved binding's default_team,
# matching the §5.3 catalog's `{}` param shape.

_NO_TEAM_ACTION = (
    "the binding has no default_team configured — this cannot be fixed from within a tool "
    "call (the graph may have several teams and nothing here should guess which one). Ask "
    "whoever set up this MCP server to set MNEMEX_DEFAULT_TEAM in the server's environment, "
    "or add `default_team: <team-name>` to the project's .mnemex.md binding file. "
    "bind_status's `default_team` field is null when this is the blocker."
)


def _map_promote_value_error(ve: ValueError, fallback_code: str, fallback_action: str) -> ToolError:
    """Every mnx_promote entry point resolves the team via the same _resolve() helper, so any
    of them can raise the same 'no default_team' ValueError — not just promote_begin. Route it
    to one clear, correctly-actionable error regardless of which tool surfaced it, instead of
    letting it get mismapped to that tool's OWN fallback code (e.g. promote_apply mapping it to
    "call promote_begin first", which is not the actual fix)."""
    msg = str(ve)
    if "default_team" in msg:
        return ToolError("no-team", msg, _NO_TEAM_ACTION)
    return ToolError(fallback_code, msg, fallback_action)


@tool_guard()
def _promote_begin(binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Preflight (flush stamps, D7 unpushed guard, stranded-plan recovery) then acquire the
    team lock. Returns the staged session batch + team phonebook, or a guard block (busy/
    unpushed/ingest-batch) naming the next step."""
    try:
        return mnx_promote.begin(binding=binding)
    except ValueError as ve:
        raise _map_promote_value_error(ve, "no-team", _NO_TEAM_ACTION) from ve


@tool_guard()
def _promote_context(pids: Optional[list[str]] = None, clusters: Optional[list[str]] = None,
                     binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Everything the reconcile judgment needs in one call: the staged batch (optionally
    filtered), mnx_simindex near-match candidates per atom, routed cluster index rows, and
    a mnx_mesh link-plan preview."""
    try:
        return mnx_promote.context(binding=binding, pids=pids, clusters=clusters)
    except ValueError as ve:
        raise _map_promote_value_error(ve, "no-team", _NO_TEAM_ACTION) from ve


@tool_guard()
def _promote_apply(plan: dict[str, Any], approved: bool = False,
                   binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Execute the promote SKILL's Step 5 (writes -> mesh -> consolidate -> regen -> doctor
    gate -> persist -> settle) under the lock acquired by promote_begin. MUTATING: refuses
    without approved=true — the human approval named in the procedure's Step 4. A rejected
    plan (validation or doctor-gate) or a committed-but-unpushed merge comes back as a
    structured, non-error payload, not a ToolError."""
    if not approved:
        raise ToolError("approval-required",
                        "promote_apply executes a plan transaction; it needs explicit human "
                        "approval.",
                        "present the plan to the user, then call promote_apply again with "
                        "approved=true")
    try:
        return mnx_promote.apply(plan, approved=True, binding=binding)
    except ValueError as ve:
        raise _map_promote_value_error(ve, "no-lock", "call promote_begin first") from ve


@tool_guard()
def _promote_retry_push(binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Push an already-committed promote merge, then perform the deferred per-atom settle
    from the persisted plan."""
    try:
        return mnx_promote.retry_push(binding=binding)
    except ValueError as ve:
        raise _map_promote_value_error(
            ve, "no-pending-plan",
            "nothing to retry; call promote_begin to start a new promote") from ve


@tool_guard()
def _promote_abort(binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Release the team lock and drop any pending plan. Staging is left untouched."""
    try:
        return mnx_promote.abort(binding=binding)
    except ValueError as ve:
        raise _map_promote_value_error(ve, "no-team", _NO_TEAM_ACTION) from ve


# --- tool bodies (Phase 2 commit 2b: held queue) -------------------------------------
#
# Straight passthroughs to mnx_stage's held-contradictions queue (populated by a `hold`
# disposition in promote_apply).

@tool_guard()
def _held_list(binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """The held-contradictions queue: count, items (provisional id, reason, contradicts,
    age), and the lingering-bound nag. Read-only."""
    return mnx_stage.held_status(binding=binding)


@tool_guard()
def _held_release(pid: str, binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Return a held atom to the active staging queue for re-reconciliation on the next
    promote (the contradiction was resolved in the atom's favour). Not-held is a noop."""
    return mnx_stage.release_held(pid, binding=binding)


@tool_guard()
def _held_drop(pid: str, binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Discard a held atom outright (the contradiction was resolved in the graph's favour).
    Not-held is a noop, not an error."""
    return mnx_stage.drop_held(pid, binding=binding)


def register_tools(server: "FastMCP") -> None:
    """Register the Phase-1 binding/health + read + capture surface, and the Phase-2
    promote + held-queue surface (commit 2b)."""

    @server.tool(name="bind_status",
                 description="Report which Mnemex graph is resolved for this project/user, the "
                             "resolution source, the graph root, whether the clone is present, "
                             "and any committed-but-unpushed promote. Read-only. Check this "
                             "before capturing/promoting on a multi-team graph: if "
                             "`default_team` is null, every promote_* tool will refuse until "
                             "the server's operator sets one (MNEMEX_DEFAULT_TEAM env var, or "
                             "default_team: in the project's .mnemex.md) — not something fixable "
                             "from a tool call.")
    def bind_status() -> dict[str, Any]:
        return _bind_status()

    @server.tool(name="status",
                 description="At-a-glance memory status: per-team node/tier counts, staged and "
                             "held capture counts, pending usage stamps, and a health summary. "
                             "Read-only.")
    def status() -> dict[str, Any]:
        return _status()

    @server.tool(name="doctor_check",
                 description="Run the graph invariant suite and return findings (errors/warnings/"
                             "info). Read-only — never repairs.")
    def doctor_check() -> dict[str, Any]:
        return _doctor_check()

    @server.tool(name="doctor_fix",
                 description="Regenerate derived files (indexes, cross-links, phonebook) from node "
                             "truth. MUTATING: pass confirm=true to proceed.")
    def doctor_fix(confirm: bool = False) -> dict[str, Any]:
        return _doctor_fix(confirm=confirm)

    @server.tool(name="init_graph",
                 description="Scaffold a brand-new Mnemex graph and leave it doctor-clean. Give "
                             "exactly one of path=<folder> or remote=<git-url>; a non-empty remote "
                             "is refused (bind to it instead). Optional team=/org= names.")
    def init_graph(path: Optional[str] = None, remote: Optional[str] = None,
                   team: str = mnx_init.DEFAULT_TEAM, org: Optional[str] = None) -> dict[str, Any]:
        return _init_graph(remote=remote, path=path, team=team, org=org)

    @server.tool(name="read_frontier",
                 description="Org head + team heads (one-line descriptions + child cluster "
                             "descriptions), plus the graph-wide consolidation-overdue warning. "
                             "Route by matching descriptions to the request, then call "
                             "read_cluster on the chosen cluster path(s). Read-only.")
    def read_frontier() -> dict[str, Any]:
        return _read_frontier()

    @server.tool(name="read_cluster",
                 description="A cluster's Hot/Warm/Cold tier tables (each row flags `stale` when "
                             "its freshness horizon has passed) plus the staged-capture overlay "
                             "for that cluster's domain (staged/unpromoted, newest-first — newer "
                             "than the graph, never body-merge a contradiction, flag it instead). "
                             "Pass tiers=['hot'] first and widen to warm/cold only if "
                             "insufficient. Read-only.")
    def read_cluster(cluster: str, tiers: Optional[list[str]] = None) -> dict[str, Any]:
        return _read_cluster(cluster, tiers)

    @server.tool(name="read_nodes",
                 description="Node bodies for `ids`, plus each node's governed-by pattern "
                             "companions, budget-capped by max_bytes. Refuses stg- ids (those "
                             "bodies already came from read_cluster's overlay). Load only the "
                             "ids you actually plan to use in the answer.")
    def read_nodes(ids: list[str], max_bytes: Optional[int] = None) -> dict[str, Any]:
        return _read_nodes(ids, max_bytes)

    @server.tool(name="record_usage",
                 description="Append one usage stamp per manifest entry "
                             "{id, role: contributed|consulted|traversed, weight?} against each "
                             "node's home-cluster registry. traversed entries are accepted and "
                             "ignored. Call once, at the end of the task, for every node body "
                             "you loaded via read_nodes.")
    def record_usage(manifest: list[dict[str, Any]]) -> dict[str, Any]:
        return _record_usage(manifest)

    @server.tool(name="capture_status",
                 description="Staging budget level (soft/hard) plus the full staged ledger "
                             "(provisional id, type, score, summary, age) for this graph. "
                             "Read-only.")
    def capture_status() -> dict[str, Any]:
        return _capture_status()

    @server.tool(name="capture_add",
                 description="Stage one durable atom extracted from the session: "
                             "{type: domain|pattern, summary, aliases?, domain?, trigger? "
                             "(required for pattern), score: now|later, urgent?, volatility?, "
                             "provenance?, body}. Idempotent by content hash. Refuses a NEW "
                             "atom once the session batch is past the hard budget — the "
                             "refusal names both ways out (promote, or capture_drop/"
                             "capture_discard_all). There is a soft per-atom body budget "
                             "(node_body_max_chars, default 6000) — NOT enforced here or at "
                             "promote time, so an oversized body is staged and later lands "
                             "unsplit unless the host splits it into multiple linked atoms "
                             "itself; the result carries `over_budget: {chars, cap}` when this "
                             "applies.")
    def capture_add(type: str = "domain", summary: str = "", aliases: Optional[list[str]] = None,
                    domain: Optional[list[str]] = None, trigger: Optional[str] = None,
                    score: str = "later", urgent: bool = False, volatility: Any = "default",
                    provenance: Optional[dict[str, Any]] = None,
                    body: str = "") -> dict[str, Any]:
        return _capture_add(type, summary, aliases, domain, trigger, score, urgent,
                            volatility, provenance, body)

    @server.tool(name="capture_drop",
                 description="Drop one staged atom by its provisional id (stg-...). Not-found "
                             "is a noop, not an error.")
    def capture_drop(pid: str) -> dict[str, Any]:
        return _capture_drop(pid)

    @server.tool(name="capture_discard_all",
                 description="Discard every staged atom for this graph. MUTATING: pass "
                             "confirm=true to proceed.")
    def capture_discard_all(confirm: bool = False) -> dict[str, Any]:
        return _capture_discard_all(confirm=confirm)

    @server.tool(name="glean_step",
                 description="One guardrail tick of the capture glean loop: pass the staged "
                             "count before and after a recall pass; returns whether the pass "
                             "made progress and whether to run another (stop at no-progress or "
                             "at the pass cap). Pure computation.")
    def glean_step(before: int, after: int, pass_no: int,
                   max_passes: int = mnx_glean.DEFAULT_MAX_PASSES) -> dict[str, Any]:
        return _glean_step(before, after, pass_no, max_passes)

    @server.tool(name="promote_begin",
                 description="Begin a promote transaction: preflight guards (D7 unpushed -> "
                             "promote_retry_push, stranded-plan recovery), flush pending usage "
                             "stamps, then acquire the team lock. Returns the staged session "
                             "batch (with provenance) and the team phonebook, or a guard block "
                             "(busy/unpushed/ingest-batch) naming the next step. Call "
                             "promote_context next.")
    def promote_begin() -> dict[str, Any]:
        return _promote_begin()

    @server.tool(name="promote_context",
                 description="Everything the reconcile judgment needs in one call: the staged "
                             "batch (optionally filtered by pids/clusters), near-match "
                             "candidates per atom, routed cluster index rows, and a mesh "
                             "link-plan preview. Call after promote_begin, before drafting the "
                             "plan.")
    def promote_context(pids: Optional[list[str]] = None,
                        clusters: Optional[list[str]] = None) -> dict[str, Any]:
        return _promote_context(pids, clusters)

    @server.tool(name="promote_apply",
                 description="Execute an approved plan transaction: node writes -> mesh links "
                             "-> consolidate -> regenerate indexes/cross-links/phonebook -> "
                             "doctor gate (rolls back on failure) -> persist -> per-atom settle. "
                             "MUTATING: pass approved=true after presenting the plan to the user "
                             "(the promote procedure's Step 4 approval). `plan` is "
                             "{plan_version: 1, dispositions: [{pid, op: create|merge|supersede|"
                             "resurrect|drop_dup|hold, ...op-specific fields}], splits?, links?, "
                             "consolidate?} — every staged pid from promote_begin's batch must "
                             "get exactly one disposition. op-specific fields: create/supersede "
                             "need cluster + fields (fields.title required); supersede also "
                             "needs old_id; merge needs id + cluster + changes (a dict — "
                             "REPLACEMENT values, not additive, for any of summary/aliases/"
                             "domain/confidence/references/mentions/edges/volatility/trigger/"
                             "body; an unrecognized key is a validation error, not a silent "
                             "no-op — to add an alias, pass the full new aliases list including "
                             "the old ones); resurrect needs id + cluster; drop_dup needs "
                             "dup_of; hold needs reason. `links.confirmed_suggestions` is "
                             "[{src, dst}]; `consolidate` is {run: bool, approved_deaths: "
                             "[id,...]} — both must be objects/omitted, not other JSON types. "
                             "src/dst may be either a real node id or the staged pid of an atom "
                             "disposed in this SAME plan (create/merge/supersede/resurrect) — "
                             "translated to its real id automatically, since a create's real id "
                             "isn't known until this call mints it. A pre-existing node's edges "
                             "into a just-superseded id are repointed to the successor "
                             "automatically — no plan field needed.")
    def promote_apply(plan: dict[str, Any], approved: bool = False) -> dict[str, Any]:
        return _promote_apply(plan, approved)

    @server.tool(name="promote_retry_push",
                 description="Push an already-committed promote merge (after a prior "
                             "promote_apply returned action=committed-not-pushed, or "
                             "promote_begin's unpushed guard fired), then perform the deferred "
                             "per-atom settle.")
    def promote_retry_push() -> dict[str, Any]:
        return _promote_retry_push()

    @server.tool(name="promote_abort",
                 description="Release the team lock and drop any pending plan. Staging is left "
                             "untouched — staged atoms remain for a later promote_begin.")
    def promote_abort() -> dict[str, Any]:
        return _promote_abort()

    @server.tool(name="held_list",
                 description="The held-contradictions queue: count, items (provisional id, "
                             "reason, contradicts, age), and the lingering-bound nag. Read-only.")
    def held_list() -> dict[str, Any]:
        return _held_list()

    @server.tool(name="held_release",
                 description="Return a held atom to the active staging queue so it is "
                             "re-reconciled on the next promote (the contradiction was resolved "
                             "in the atom's favour). Not-held is a noop.")
    def held_release(pid: str) -> dict[str, Any]:
        return _held_release(pid)

    @server.tool(name="held_drop",
                 description="Discard a held atom outright (the contradiction was resolved in "
                             "the graph's favour). Not-held is a noop, not an error.")
    def held_drop(pid: str) -> dict[str, Any]:
        return _held_drop(pid)


# --- the server --------------------------------------------------------------------

def _sdk_missing_message() -> str:
    if sys.version_info < (3, 10):
        return (f"The OpenMnemex MCP server needs Python 3.10+ (running "
                f"{sys.version_info.major}.{sys.version_info.minor}); the engine itself "
                f"keeps working on 3.9 — only the MCP surface is gated.")
    return ("The 'mcp' SDK is not installed. Install the optional extra: "
            "pip install 'openmnemex[mcp]'  (or run via: uvx openmnemex-mcp). "
            f"Import error: {_MCP_IMPORT_ERROR}")


def sdk_available() -> bool:
    return FastMCP is not None and sys.version_info >= (3, 10)


def create_server() -> "FastMCP":
    """Build the FastMCP stdio server with the currently-shipped tools.

    Phase 1 registers binding/health (1b), read (1c), and capture (1d); Phase 2 adds
    promote + the held queue (2b)."""
    if not sdk_available():
        raise RuntimeError(_sdk_missing_message())
    server = FastMCP(name=SERVER_NAME, instructions=_INSTRUCTIONS)
    # FastMCP doesn't expose a version parameter; the low-level server does, and without
    # this the host would see the SDK's version instead of ours in initialize.serverInfo.
    server._mcp_server.version = engine_version()
    register_tools(server)
    return server


def info() -> dict[str, Any]:
    """Server identity + environment readiness, without starting anything."""
    return {"name": SERVER_NAME, "version": engine_version(),
            "sdk_available": sdk_available(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            **({} if sdk_available() else {"sdk_error": _sdk_missing_message()})}


def serve() -> int:
    """Run the stdio server (blocks). Pre-flight failures go to stderr — stdout is JSON-RPC."""
    try:
        server = create_server()
    except RuntimeError as exc:
        print(f"openmnemex-mcp: {exc}", file=sys.stderr)
        return 1
    server.run(transport="stdio")
    return 0


# --- cli -----------------------------------------------------------------------------

_USAGE = [
    "mnx_mcp.py serve  — run the stdio MCP server (default; blocks until the host disconnects)",
    "mnx_mcp.py info   — server identity + SDK availability as JSON (never starts the server)",
]


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "info":
        return mnx_common.emit(info())
    if cmd == "serve":
        return serve()
    return mnx_common.emit({"error": f"unknown subcommand: {cmd}", "usage": _USAGE}, ok=False)


def main() -> int:
    """Console entry point (pyproject [project.scripts] openmnemex-mcp)."""
    return _main(sys.argv)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
