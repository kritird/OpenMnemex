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

The ``mcp`` SDK is an OPTIONAL extra (``pip install openmnemex[mcp]``); this module must stay
importable without it (the packaging bridge imports every engine module, and the base install
has no ``mcp`` dependency), so the SDK import is soft and only ``serve()``/``create_server()``
require it. ``serve`` failures print to stderr — stdout belongs to the JSON-RPC protocol.

CLI:
    serve   — run the stdio server (default; blocks until the host disconnects)
    info    — server identity + SDK availability as JSON (never starts the server)

Run: ``uvx --from 'openmnemex[mcp]' openmnemex-mcp`` (packaged) or ``python3 scripts/mnx_mcp.py``
(checkout).
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
import mnx_er
import mnx_glean
import mnx_hooks
import mnx_ingest
import mnx_init
import mnx_procedures
import mnx_promote
import mnx_read
import mnx_resolve
import mnx_stage
import mnx_stamp
import mnx_status

# Soft SDK import: the engine (and the packaging bridge, which imports every mnx_* module)
# must work without the [mcp] extra. Only building/running the server needs it.
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

# Keyed by graph_slug(), NOT a single global (F8, onboarding plan Phase 5 prereq): a mid-session
# use_graph() switch resolves to a DIFFERENT binding, which must get its OWN sync verdict rather
# than reusing (or clobbering) whatever the previously-bound graph already cached.
_session_state: dict[str, dict[str, Any]] = {}

# Whether this process has shown the once-per-session `needs_graph_confirm` signal yet (Phase 5a).
# Separate from _session_state because confirming is a SESSION-level fact ("the human has been
# told which graph"), not a per-graph one — switching graphs via use_graph re-confirms implicitly
# (that IS the human choosing), it does not reset this back to False.
_confirm_state: dict[str, bool] = {"shown": False}


def reset_session_state() -> None:
    """Forget the once-per-process sync + confirm state (tests; a real server process never
    needs this — it dies with the session)."""
    _session_state.clear()
    _confirm_state["shown"] = False


def mark_graph_confirmed() -> None:
    """Suppress the `needs_graph_confirm` signal for the rest of this process: establishing a
    binding (init_graph) or explicitly switching one (use_graph) already IS the human's confirmed
    choice — asking them to re-confirm the very graph they just picked would be pure noise."""
    _confirm_state["shown"] = True


def _resolve_binding() -> "mnx_binding.Binding":
    try:
        binding = mnx_binding.resolve(session_id=session_id())
    except Exception as exc:  # malformed binding file — report, don't traceback
        raise ToolError("binding-error", str(exc),
                        "fix or remove the malformed binding file") from exc
    if binding is None:
        raise ToolError("unresolved", "No Mnemex graph configured for this project or user.",
                        "run init_graph, or call list_graphs if you expect one to already exist")
    return binding


def ensure_synced() -> dict[str, Any]:
    """Resolve the binding and sync the graph clone, once per graph per server process.

    Returns ``{binding, sync}`` plus, when applicable, ``needs_graph_confirm`` (Phase 5a — the
    first graph-touching call this process has made, at all: "tell the user which graph and let
    them pick another") and ``override_notice`` (Phase 5b — a session override is active and
    differs from what this project/user would otherwise resolve; re-attached on EVERY call, not
    just the first, since it is a standing "you are not where you think" warning, not a one-shot
    confirmation). ``sync`` carries ``degraded``/``offline_degraded`` flags for tools to surface.
    A hard sync failure (missing local folder, clone failed with no local copy) raises ToolError
    and does NOT cache, so the next call retries.
    """
    binding = _resolve_binding()
    slug = binding.slug()
    cached = _session_state.get(slug)
    if cached is None:
        result = mnx_binding.sync(binding)
        action = result.get("action")
        if action == "error":
            raise ToolError("sync-failed", result.get("message", "Graph sync failed."),
                            "check the graph path/remote or run init_graph")
        cached = {"action": action, "message": result.get("message"),
                  "degraded": action in _DEGRADED_HINTS,
                  "offline_degraded": action == "offline"}
        if _DEGRADED_HINTS.get(action):
            cached["next_step"] = _DEGRADED_HINTS[action]
        _session_state[slug] = cached
    out: dict[str, Any] = {"binding": binding, "sync": cached}
    if not _confirm_state["shown"]:
        _confirm_state["shown"] = True
        out["needs_graph_confirm"] = True
    notice = mnx_binding.override_mismatch(binding)
    if notice:
        out["override_notice"] = notice
    return out


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
                    if session.get("needs_graph_confirm"):
                        payload.setdefault("needs_graph_confirm", True)
                        payload.setdefault("resolution", session["binding"].resolution_line())
                    if session.get("override_notice"):
                        payload.setdefault("override_notice", session["override_notice"])
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
    # session_id() so a Phase 5b override is resolved consistently with tool_guard's OWN binding
    # (`binding`, above) — resolving without it here could silently report a DIFFERENT graph.
    return mnx_status.status(session_id=session_id())


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
def _init_suggest() -> dict[str, Any]:
    """Propose a local-folder default graph for a host with none configured. Read-only — pure
    computation, writes nothing (the read-only companion to init_graph's use_default)."""
    return mnx_init.suggest_default_graph(os.getcwd())


@tool_guard(sync_first=False)
def _init_graph(remote: Optional[str] = None, path: Optional[str] = None,
                team: str = mnx_init.DEFAULT_TEAM, org: Optional[str] = None,
                use_default: bool = False) -> dict[str, Any]:
    """Scaffold a brand-new graph (the one tool that ESTABLISHES a root, so it does not
    sync-first an existing binding). Probes a remote read-only before touching it, installs
    the merge driver, stamps config, and doctor-checks — a fresh graph is clean on day one.

    ``use_default=True`` is the zero-argument onboarding path: take the ``init_suggest`` proposal
    (a local folder under the mnemex home, named after this project), scaffold it, AND write it as
    the user default so the very next tool call resolves it — no path/remote needed.

    A freshly scaffolded graph is always empty, so the result carries ``seed_available``/
    ``next_step`` (onboarding plan Phase 3 — the same fork ``read_frontier`` offers on first
    read, surfaced here too so a host can offer it immediately after setup)."""
    if use_default:
        proposal = mnx_init.suggest_default_graph(os.getcwd())
        try:
            result = mnx_init.init_graph(path=proposal["path"], team=proposal["team"],
                                         org=proposal["org"])
        except mnx_init._InitError as ie:
            raise ToolError(ie.code, str(ie), ie.action) from ie
        result["suggested"] = proposal
        # Bind it so the next call resolves without a project .mnemex.md. Never clobber an existing
        # user default (write_user_default refuses without force) — reported, not fatal.
        result["user_default"] = mnx_binding.write_user_default(
            path=proposal["path"], default_team=proposal["team"])
        mark_graph_confirmed()  # Phase 5a: creating it IS confirming it — don't re-ask next call
        return result
    try:
        result = mnx_init.init_graph(remote=remote, path=path, team=team, org=org)
    except mnx_init._InitError as ie:
        raise ToolError(ie.code, str(ie), ie.action) from ie
    mark_graph_confirmed()
    return result


@tool_guard(sync_first=False)
def _list_graphs() -> dict[str, Any]:
    """Every graph Mnemex knows about (onboarding plan Phase 4): the discovery registry unioned
    with a scan of the remote-clone cache, each flagged `present`. Read-only, no binding
    required — works before init_graph has ever been called."""
    return {"graphs": mnx_binding.list_graphs()}


@tool_guard(sync_first=False)
def _use_graph(slug: str) -> dict[str, Any]:
    """Switch THIS session to a different known graph (onboarding plan Phase 5b) — a session
    override that outranks the project/user default until cleared or it expires (TTL-bounded;
    never survives past this session). `slug` must be one `list_graphs` returned. Does not
    sync-first against the OLD binding (this call is itself establishing a new one, like
    init_graph); refuses with a `busy` action if the CURRENT graph has an open promote lock /
    in-flight plan (finish or abort it first — switching out from under it would strand it)."""
    graphs = {g["slug"]: g for g in mnx_binding.list_graphs()}
    g = graphs.get(slug)
    if g is None:
        raise ToolError("unknown-slug", f"No known graph with slug '{slug}'.",
                        "call list_graphs to see valid slugs")
    kwargs = {"remote": g["location"]} if g["kind"] == "git-remote" else {"path": g["location"]}
    result = mnx_binding.set_session_override(session_id(), **kwargs)
    if not result.get("ok"):
        raise ToolError("switch-blocked", result.get("message", "Could not switch graphs."),
                        "resolve the blocker, then retry use_graph")
    reset_session_state()  # F8: forget any cached sync verdict — the new graph must sync fresh
    mark_graph_confirmed()  # an explicit switch IS the human's confirmed choice
    return result


@tool_guard(sync_first=False)
def _clear_graph_override() -> dict[str, Any]:
    """Drop this session's graph override (onboarding plan Phase 5b), reverting to normal
    project/env/user resolution. Not-overridden is a no-op, not an error."""
    result = mnx_binding.clear_session_override(session_id())
    reset_session_state()  # the effective binding may change — forget any cached sync verdict
    return result


# --- tool bodies (Phase 1 commit 1c: read) ------------------------------------------
#
# Deterministic mechanics only (mnx_read.py, §6.1) — routing (which team/cluster matches
# the request), the stop-early tier judgment, and disposing loaded bodies in the usage
# manifest all stay host/model judgment (the mnx-read SKILL / procedure prompt).

@tool_guard()
def _read_frontier(binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Org head + team heads (descriptions + child cluster descriptions), plus the
    graph-wide consolidation-overdue warning. Never tier rows — call read_cluster next.

    When the graph is empty, also attaches `fill_offer` (onboarding plan Phase 3 — the
    empty-graph fork): composed here rather than inside `mnx_read.frontier()` because only a
    resolved binding can see staging state (`mnx_stage.status()`); frontier() stays binding-
    free and pays no extra cost on the common non-empty path."""
    result = mnx_read.frontier(binding.graph_root(), mnx_common.now_utc())
    if result["empty"]:
        staged_count = mnx_stage.status(binding=binding)["count"]
        result["fill_offer"] = mnx_read.fill_offer(True, staged_count)
    return result


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
                               float(weight) if weight is not None else 1.0,
                               binding=binding)
        stamped.append({"id": nid, "role": role, **res})
    out: dict[str, Any] = {"stamped": stamped, "ignored": ignored, "errors": errors}
    if stamped and binding.kind() == "git-remote":
        out["flush"] = mnx_stamp.flush(binding=binding)
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
                 ingest_batch: Optional[str] = None,
                 binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Stage one atom. Idempotent by content hash (a re-capture of identical content is a
    no-op restage, not a duplicate). Refuses a NEW atom once the session batch is past the
    hard budget — the refusal message names both ways out (promote to drain, or drop/
    discard-all to make room). node_body_max_chars is a SOFT budget (not enforced by the
    doctor gate) — a body over it is flagged in the result as `over_budget`; the host is
    expected to split it into multiple linked atoms rather than stage one oversized body.

    ``ingest_batch`` labels the atom as part of a bulk corpus import: it sets bulk=true, gives
    the batch its own large cap, and partitions it from hand-captures (DP8) so the per-session
    nag never fires. The corpus provenance fields (source_repo/commit_sha/source_path/anchor/
    kind) travel in ``provenance`` — that makes the atom promotable COLD via mnx-promote --bulk."""
    atom = {"type": type, "summary": summary, "aliases": aliases, "domain": domain,
            "trigger": trigger, "score": score, "urgent": urgent, "volatility": volatility,
            "provenance": provenance or {}, "body": body, "ingest_batch": ingest_batch}
    try:
        result = mnx_stage.add(atom, binding=binding)
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


# --- tool bodies (Phase 2: bulk ingest) ----------------------------------------------
#
# Thin shims over the deterministic corpus front-end (mnx_ingest), the coverage primitive
# (mnx_glean.coverage), and the entity-resolution proposer (mnx_er) — exposing the SAME engine the
# Claude mnx-ingest skill drives so a foreign MCP host can bootstrap/re-import a repo (plan Phase 2,
# closes F4). Judgment (is this a durable atom? which merge?) stays with the host model + the
# ingest-procedure prompt; these tools only walk/probe/delta/dedupe. DP1/DP3: ingest NEVER writes
# graph_root and NEVER mutates the source — acquire clones a remote read-only into the ingest cache;
# probe/delta only read the (user-directed) source; manifest_write is the sole ingest writer and
# lands strictly under <graph>/.mnemex/ingest/.

def _require_source_root(root: str) -> Path:
    """Resolve an ingest source/walk root. Unlike confine(), the source is a user-directed external
    corpus read (DP3 — read-only), so it is deliberately NOT restricted to the graph/home/cache set;
    it need only exist and be a directory. Every WRITE and graph-scoped read still goes through the
    binding + confine(), so this exception widens reads of an explicitly-named source only."""
    p = Path(root).expanduser().resolve()
    if not p.is_dir():
        raise ToolError("bad-root", f"ingest root is not a directory: {root}",
                        "pass the `root` returned by ingest_acquire")
    return p


@tool_guard(sync_first=False)
def _ingest_source_slug(source: str) -> dict[str, Any]:
    """The stable manifest slug for a source URL / path. Pure; no I/O."""
    return {"slug": mnx_ingest.source_slug(source)}


@tool_guard(sync_first=False)
def _ingest_acquire(source: str, cache: Optional[str] = None) -> dict[str, Any]:
    """Materialize a read-only corpus: a remote URL is shallow-cloned into the ingest cache; a local
    path is used in place. Returns {kind, root, commit, cached, slug}. Never mutates the source."""
    try:
        res = mnx_ingest.acquire(source, cache)
    except FileNotFoundError as fe:
        raise ToolError("source-not-found", str(fe), "check the source path/URL") from fe
    except Exception as exc:
        raise ToolError("acquire-failed", f"{type(exc).__name__}: {exc}",
                        "check the source URL and network/auth") from exc
    res["slug"] = mnx_ingest.source_slug(source)
    return res


@tool_guard(sync_first=False)
def _ingest_probe(root: str, include: Optional[str] = None, exclude: Optional[str] = None,
                  max_bytes: int = mnx_ingest.MAX_BYTES_DEFAULT) -> dict[str, Any]:
    """Walk → classify → chunk → hash the corpus into candidate units + a scope estimate (gate #1).
    Read-only; secrets are counted but never opened."""
    return mnx_ingest.probe(str(_require_source_root(root)), include, exclude, max_bytes)


@tool_guard()
def _ingest_delta(root: str, source_slug: str, include: Optional[str] = None,
                  exclude: Optional[str] = None, max_bytes: int = mnx_ingest.MAX_BYTES_DEFAULT,
                  binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Re-import diff: added/changed/unchanged/orphans vs the prior manifest for `source_slug`
    (stored under the BOUND graph). Extract only added+changed; orphans surface, never auto-die."""
    manifest = mnx_ingest.manifest_path(binding.graph_root(), source_slug)
    confine(manifest, allowed_roots(binding))
    # allow_missing_manifest: the path is DERIVED here (not caller-typed), so no manifest
    # legitimately means "first import of this slug" — the result carries first_import: true.
    return mnx_ingest.delta(str(_require_source_root(root)), str(manifest), include, exclude,
                            max_bytes, allow_missing_manifest=True)


@tool_guard()
def _ingest_manifest_write(source_slug: str, files: dict[str, Any],
                           source_repo: Optional[str] = None, last_commit: Optional[str] = None,
                           binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Record the ingest manifest (source_path@commit → node_ids) under the BOUND graph so the next
    re-import diffs correctly. The sole ingest writer; always lands under <graph>/.mnemex/ingest/."""
    path = mnx_ingest.manifest_path(binding.graph_root(), source_slug)
    confine(path, allowed_roots(binding))
    return mnx_ingest.manifest_write(binding.graph_root(), source_slug, files or {},
                                     source_repo, last_commit)


@tool_guard(sync_first=False)
def _glean_coverage(units: list[dict[str, Any]], staged: list[dict[str, Any]], pass_no: int,
                    max_passes: int = mnx_glean.DEFAULT_MAX_PASSES) -> dict[str, Any]:
    """Ingest coverage checklist: which enumerated units still have zero staged atoms + a stop
    signal (complete/cap). Distinct from glean_step (the episodic before/after guardrail). Pure."""
    return mnx_glean.coverage(units, staged, pass_no, max_passes)


@tool_guard()
def _er_resolve(atoms: list[dict[str, Any]], team: Optional[str] = None,
                match: float = mnx_er.MATCH_DEFAULT, possible: float = mnx_er.POSSIBLE_DEFAULT,
                binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Entity resolution over {staged atoms ∪ existing graph pages} in the BOUND graph: propose a
    CREATE / MERGE / COLLAPSE disposition per cluster + a `possible` HITL band. Reads the graph
    (already merges into existing pages — the existing-graph import case); writes nothing."""
    return mnx_er.resolve(binding.graph_root(), atoms or [], team or binding.default_team,
                          match, possible)


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
def _promote_begin(ingest_batch: Optional[str] = None,
                   binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Preflight (flush stamps, D7 unpushed guard, stranded-plan recovery) then acquire the
    team lock. Returns the staged batch + team phonebook, or a guard block (busy/unpushed/
    ingest-batch) naming the next step. Pass ingest_batch=<id> to drain a BULK corpus batch
    (same transaction, just a different staged batch) instead of the session hand-captures."""
    try:
        return mnx_promote.begin(binding=binding, ingest_batch=ingest_batch)
    except ValueError as ve:
        raise _map_promote_value_error(ve, "no-team", _NO_TEAM_ACTION) from ve


@tool_guard()
def _promote_context(pids: Optional[list[str]] = None, clusters: Optional[list[str]] = None,
                     ingest_batch: Optional[str] = None,
                     binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Everything the reconcile judgment needs in one call: the staged batch (optionally
    filtered), mnx_simindex near-match candidates per atom, routed cluster index rows, and
    a mnx_mesh link-plan preview. ingest_batch selects the same bulk batch as promote_begin."""
    try:
        return mnx_promote.context(binding=binding, pids=pids, clusters=clusters,
                                   ingest_batch=ingest_batch)
    except ValueError as ve:
        raise _map_promote_value_error(ve, "no-team", _NO_TEAM_ACTION) from ve


@tool_guard()
def _promote_apply(plan: dict[str, Any], approved: bool = False, ingest_batch: Optional[str] = None,
                   binding: Any = None, sync: Any = None) -> dict[str, Any]:
    """Execute the promote SKILL's Step 5 (writes -> mesh -> consolidate -> regen -> doctor
    gate -> persist -> settle) under the lock acquired by promote_begin. MUTATING: refuses
    without approved=true — the human approval named in the procedure's Step 4. A rejected
    plan (validation or doctor-gate) or a committed-but-unpushed merge comes back as a
    structured, non-error payload, not a ToolError. ingest_batch must match promote_begin's
    (the plan is validated against, and settles, that bulk batch's staged pids)."""
    if not approved:
        raise ToolError("approval-required",
                        "promote_apply executes a plan transaction; it needs explicit human "
                        "approval.",
                        "present the plan to the user, then call promote_apply again with "
                        "approved=true")
    try:
        return mnx_promote.apply(plan, approved=True, binding=binding, ingest_batch=ingest_batch)
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

    @server.tool(name="init_suggest",
                 description="Propose a local-folder default graph for a host with none configured "
                             "(a folder under the mnemex home, named after this project). Read-only "
                             "— writes nothing. Use it to preview what init_graph(use_default=true) "
                             "would create before committing.")
    def init_suggest() -> dict[str, Any]:
        return _init_suggest()

    @server.tool(name="init_graph",
                 description="Scaffold a brand-new Mnemex graph and leave it doctor-clean. Give "
                             "exactly one of path=<folder> or remote=<git-url>; a non-empty remote "
                             "is refused (bind to it instead). Optional team=/org= names. Or pass "
                             "use_default=true for the zero-argument onboarding path: it takes the "
                             "init_suggest proposal, scaffolds it, AND binds it as your user "
                             "default so the next tool call resolves it (no path/remote needed).")
    def init_graph(path: Optional[str] = None, remote: Optional[str] = None,
                   team: str = mnx_init.DEFAULT_TEAM, org: Optional[str] = None,
                   use_default: bool = False) -> dict[str, Any]:
        return _init_graph(remote=remote, path=path, team=team, org=org, use_default=use_default)

    @server.tool(name="list_graphs",
                 description="Enumerate every graph Mnemex knows about: the discovery registry "
                             "(<mnemex_home>/graphs.md, populated as graphs are created or "
                             "bound) unioned with a scan of the remote-clone cache, each entry "
                             "flagged `present` (its folder/clone currently exists on disk). "
                             "Read-only; works with no graph bound. Use it to help the user "
                             "pick or confirm which graph to use.")
    def list_graphs() -> dict[str, Any]:
        return _list_graphs()

    @server.tool(name="use_graph",
                 description="Switch THIS session to a different known graph (slug from "
                             "list_graphs) — a session-scoped override that outranks the "
                             "project/user default until cleared or its TTL expires; never "
                             "durable, never survives past this session. Use when the user "
                             "responds to `needs_graph_confirm` (or an `override_notice`) by "
                             "picking a different graph than the one resolved. Refuses with "
                             "action=busy if the CURRENT graph has an open promote lock / "
                             "in-flight plan — finish or abort it first.")
    def use_graph(slug: str) -> dict[str, Any]:
        return _use_graph(slug)

    @server.tool(name="clear_graph_override",
                 description="Drop this session's graph override (see use_graph), reverting to "
                             "normal project/env/user resolution. Not-overridden is a no-op.")
    def clear_graph_override() -> dict[str, Any]:
        return _clear_graph_override()

    @server.tool(name="read_frontier",
                 description="Org head + team heads (one-line descriptions + child cluster "
                             "descriptions), plus the graph-wide consolidation-overdue warning. "
                             "Route by matching descriptions to the request, then call "
                             "read_cluster on the chosen cluster path(s). Read-only.\n\n"
                             + mnx_procedures.render_digest("read"))
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
                             "applies. Pass ingest_batch=<id> to stage a bulk corpus atom (sets "
                             "bulk=true, own large cap, no per-session nag); corpus provenance "
                             "(source_repo/commit_sha/source_path/anchor/kind) rides in "
                             "provenance.\n\n"
                             + mnx_procedures.render_digest("capture"))
    def capture_add(type: str = "domain", summary: str = "", aliases: Optional[list[str]] = None,
                    domain: Optional[list[str]] = None, trigger: Optional[str] = None,
                    score: str = "later", urgent: bool = False, volatility: Any = "default",
                    provenance: Optional[dict[str, Any]] = None,
                    body: str = "", ingest_batch: Optional[str] = None) -> dict[str, Any]:
        return _capture_add(type, summary, aliases, domain, trigger, score, urgent,
                            volatility, provenance, body, ingest_batch)

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

    @server.tool(name="ingest_source_slug",
                 description="The stable manifest slug for a source URL or path (keys the re-import "
                             "manifest). Pure — no I/O.")
    def ingest_source_slug(source: str) -> dict[str, Any]:
        return _ingest_source_slug(source)

    @server.tool(name="ingest_acquire",
                 description="Materialize a read-only corpus for ingest: a remote URL (or *.git) is "
                             "shallow-cloned into the ingest cache; a local path is used in place. "
                             "Returns {kind, root, commit, cached, slug}. Never mutates the source; "
                             "secrets are never read. Call ingest_probe on the returned root next.\n\n"
                             + mnx_procedures.render_digest("ingest"))
    def ingest_acquire(source: str, cache: Optional[str] = None) -> dict[str, Any]:
        return _ingest_acquire(source, cache)

    @server.tool(name="ingest_probe",
                 description="Walk → classify → chunk → hash the corpus at `root` into candidate "
                             "extraction units (doc|interface|code-doc|config, chunked by heading / "
                             "exported symbol) plus a scope estimate {counts, est_atoms, "
                             "bytes_total, skipped_secrets} for gate #1. YAML/JSON are shape-gated "
                             "(OpenAPI/JSON-Schema → interface, commented config → config, data "
                             "blobs/lockfiles → skip). Read-only; private symbols and secrets are "
                             "never emitted. Optional include/exclude globs.")
    def ingest_probe(root: str, include: Optional[str] = None, exclude: Optional[str] = None,
                     max_bytes: int = mnx_ingest.MAX_BYTES_DEFAULT) -> dict[str, Any]:
        return _ingest_probe(root, include, exclude, max_bytes)

    @server.tool(name="ingest_delta",
                 description="Re-import diff against the prior manifest for source_slug (stored under "
                             "the bound graph): {added, changed, unchanged, orphans}. Extract only "
                             "added+changed (unchanged files are skipped — the re-run cost saver); "
                             "orphans are deleted source files' node_ids, surfaced for the human and "
                             "NEVER auto-tombstoned. Read-only.")
    def ingest_delta(root: str, source_slug: str, include: Optional[str] = None,
                     exclude: Optional[str] = None,
                     max_bytes: int = mnx_ingest.MAX_BYTES_DEFAULT) -> dict[str, Any]:
        return _ingest_delta(root, source_slug, include, exclude, max_bytes)

    @server.tool(name="glean_coverage",
                 description="Ingest coverage checklist (distinct from glean_step): given the probe "
                             "units and the staged ledger, return which units still have zero staged "
                             "atoms (the re-ask worklist) + a stop signal (complete = every unit "
                             "covered, or cap = max_glean_passes). A unit is covered when a staged "
                             "atom carries its anchor in provenance. Pure.")
    def glean_coverage(units: list[dict[str, Any]], staged: list[dict[str, Any]], pass_no: int,
                       max_passes: int = mnx_glean.DEFAULT_MAX_PASSES) -> dict[str, Any]:
        return _glean_coverage(units, staged, pass_no, max_passes)

    @server.tool(name="er_resolve",
                 description="Entity resolution over {staged atoms ∪ existing graph pages} in the "
                             "bound graph: block → score → cluster → propose one disposition per "
                             "cluster — CREATE (no graph match) / MERGE (folds into an existing "
                             "page, keeps its id — this is how a re-import merges instead of "
                             "duplicating) / COLLAPSE (intra-batch dups → one CREATE) — plus a "
                             "`possible` HITL band (the only place you rule on a merge). Writes "
                             "nothing. `atoms` are the staged candidates ({id/provisional_id, "
                             "summary, aliases, domain, mentions}); match/possible are score bands.")
    def er_resolve(atoms: list[dict[str, Any]], team: Optional[str] = None,
                   match: float = mnx_er.MATCH_DEFAULT,
                   possible: float = mnx_er.POSSIBLE_DEFAULT) -> dict[str, Any]:
        return _er_resolve(atoms, team, match, possible)

    @server.tool(name="ingest_manifest_write",
                 description="Record the ingest manifest (source_path → {hash, nodes:[id,...]}) "
                             "under the bound graph so the next re-import diffs correctly. The sole "
                             "ingest writer — always lands under <graph>/.mnemex/ingest/<slug>.json, "
                             "never the source. Normally called by promote on confirmed persist; "
                             "exposed here for hosts driving the bulk flow by hand.")
    def ingest_manifest_write(source_slug: str, files: dict[str, Any],
                              source_repo: Optional[str] = None,
                              last_commit: Optional[str] = None) -> dict[str, Any]:
        return _ingest_manifest_write(source_slug, files, source_repo, last_commit)

    @server.tool(name="promote_begin",
                 description="Begin a promote transaction: preflight guards (D7 unpushed -> "
                             "promote_retry_push, stranded-plan recovery), flush pending usage "
                             "stamps, then acquire the team lock. Returns the staged session "
                             "batch (with provenance) and the team phonebook, or a guard block "
                             "(busy/unpushed/ingest-batch) naming the next step. Call "
                             "promote_context next. Pass ingest_batch=<id> to drain a bulk corpus "
                             "batch (from capture_add ingest_batch / ingest) — the SAME transaction, "
                             "just a different staged batch.\n\n"
                             + mnx_procedures.render_digest("promote"))
    def promote_begin(ingest_batch: Optional[str] = None) -> dict[str, Any]:
        return _promote_begin(ingest_batch)

    @server.tool(name="promote_context",
                 description="Everything the reconcile judgment needs in one call: the staged "
                             "batch (optionally filtered by pids/clusters), near-match "
                             "candidates per atom, routed cluster index rows, and a mesh "
                             "link-plan preview. Call after promote_begin, before drafting the "
                             "plan. ingest_batch selects the same bulk batch as promote_begin.")
    def promote_context(pids: Optional[list[str]] = None,
                        clusters: Optional[list[str]] = None,
                        ingest_batch: Optional[str] = None) -> dict[str, Any]:
        return _promote_context(pids, clusters, ingest_batch)

    @server.tool(name="promote_apply",
                 description="Execute an approved plan transaction: node writes -> mesh links "
                             "-> consolidate -> regenerate indexes/cross-links/phonebook -> "
                             "doctor gate (rolls back on failure) -> persist -> per-atom settle. "
                             "MUTATING: pass approved=true after presenting the plan to the user "
                             "(the promote procedure's Step 4 approval). `plan` is "
                             "{plan_version: 1, dispositions: [{pid, op: create|merge|supersede|"
                             "resurrect|drop_dup|hold, ...op-specific fields}], splits?, links?, "
                             "consolidate?} — every staged pid from promote_begin's batch must "
                             "get exactly one disposition. `cluster` is a graph-root-relative "
                             "'<team>/<cluster-name>' path (a bare name is auto-prefixed with "
                             "the transaction's team; pass fields.new_cluster=true to create a "
                             "cluster that does not exist yet). op-specific fields: create/"
                             "supersede need cluster + fields (fields.title required; any of "
                             "summary/body/aliases/domain/type/volatility/trigger/mentions/"
                             "provenance left unset are inherited from the staged atom, so the "
                             "captured content is never lost); supersede also "
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
                             "automatically — no plan field needed. ingest_batch must match "
                             "promote_begin's for a bulk drain (validates + settles that batch).")
    def promote_apply(plan: dict[str, Any], approved: bool = False,
                      ingest_batch: Optional[str] = None) -> dict[str, Any]:
        return _promote_apply(plan, approved, ingest_batch)

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


def register_prompts(server: "FastMCP") -> None:
    """Register the four judgment-procedure prompts (plan v2 §5.4, commit 3b).

    Bodies are generated from ``templates/procedures/*.core.md`` via ``mnx_procedures`` — the
    same build that produces the Claude skills (commit 3a) — so the procedure prose can never
    hand-drift between the plugin and MCP surfaces (CLAUDE.md: keep both in lockstep)."""

    @server.prompt(name="read-procedure",
                    description="The read judgment procedure: route via read_frontier, scan "
                                "tiers via read_cluster (hot first), expand via read_nodes, "
                                "then record_usage for every node body loaded.")
    def read_procedure() -> str:
        return mnx_procedures.render_mcp_prompt("read")

    @server.prompt(name="capture-procedure",
                    description="The capture judgment procedure: mine the transcript for the "
                                "delta capture_status hasn't covered, glean once, score "
                                "now/later/not-needed, then capture_add per atom with "
                                "self-sufficient provenance.")
    def capture_procedure() -> str:
        return mnx_procedures.render_mcp_prompt("capture")

    @server.prompt(name="promote-procedure",
                    description="The promote judgment procedure: promote_begin, "
                                "promote_context, draft one disposition+link+consolidate "
                                "plan, present it for approval, then promote_apply.")
    def promote_procedure() -> str:
        return mnx_procedures.render_mcp_prompt("promote")

    @server.prompt(name="curate-procedure",
                    description="The curate judgment procedure: review, drop, or discard "
                                "atoms already staged (capture_status / capture_drop / "
                                "capture_discard_all) — the un-stage escape valve.")
    def curate_procedure() -> str:
        return mnx_procedures.render_mcp_prompt("curate")


# --- the server --------------------------------------------------------------------

def _sdk_missing_message() -> str:
    if sys.version_info < (3, 10):
        return (f"The OpenMnemex MCP server needs Python 3.10+ (running "
                f"{sys.version_info.major}.{sys.version_info.minor}); the engine itself "
                f"keeps working on 3.9 — only the MCP surface is gated.")
    return ("The 'mcp' SDK is not installed. Install the optional extra: "
            "pip install 'openmnemex[mcp]'  (or run via: uvx --from 'openmnemex[mcp]' openmnemex-mcp). "
            f"Import error: {_MCP_IMPORT_ERROR}")


def sdk_available() -> bool:
    return FastMCP is not None and sys.version_info >= (3, 10)


def create_server() -> "FastMCP":
    """Build the FastMCP stdio server with the currently-shipped tools + prompts.

    Phase 1 registers binding/health (1b), read (1c), and capture (1d); Phase 2 adds
    promote + the held queue (2b); Phase 3 adds the four judgment-procedure prompts (3b)."""
    if not sdk_available():
        raise RuntimeError(_sdk_missing_message())
    server = FastMCP(name=SERVER_NAME, instructions=_INSTRUCTIONS)
    # FastMCP doesn't expose a version parameter; the low-level server does, and without
    # this the host would see the SDK's version instead of ours in initialize.serverInfo.
    server._mcp_server.version = engine_version()
    register_tools(server)
    register_prompts(server)
    return server


def info() -> dict[str, Any]:
    """Server identity + environment readiness, without starting anything."""
    return {"name": SERVER_NAME, "version": engine_version(),
            "sdk_available": sdk_available(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            **({} if sdk_available() else {"sdk_error": _sdk_missing_message()})}


def list_tool_names() -> list[str]:
    """The registered tool names over a real in-process client session (never over the wire,
    never starts a persistent process) — used by ``mnx_install.check_install`` (§7 Phase 5) so
    the `mcp`/`anyio` imports stay confined to this module (risk R4)."""
    if not sdk_available():
        raise RuntimeError(_sdk_missing_message())
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session as client_session

    server = create_server()

    async def _list() -> list[str]:
        async with client_session(server._mcp_server) as session:
            tools = await session.list_tools()
            return sorted(t.name for t in tools.tools)

    return anyio.run(_list)


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
