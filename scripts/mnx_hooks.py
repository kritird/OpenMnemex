"""mnx_hooks.py — session-lifecycle hook logic + the Claude Code hook adapter.

Layered per the multi-agent plan (v2 §7, Phase 0 commit 0f):

  * ``core_*(event: dict) -> HookOutcome`` — the per-event logic, host-neutral. Takes an
    already-parsed event dict, performs the side effects (marker state, stamp flushes,
    doctor checks), and returns a ``HookOutcome`` describing what to deliver. Never reads
    stdin, never prints. Foreign-host adapters (the OpenCode plugin, Phase 4) normalize
    their event payload into the same dict shape and render the outcome themselves.
  * Claude adapters (``session_start`` … ``post_apply_check``, dispatched by ``_main``) —
    read the event JSON from stdin per the Claude Code hooks protocol and render the
    outcome in Claude's hook output shape (``_emit_claude``).

Subcommands (argv[1]):
  session-start      : resolve the binding and sync the graph (blocking), then inject a one-time
                       primer that asks the agent to get the user's CONSENT for this session: use
                       Mnemex (read before domain work, write to capture) — or mute it. If the user
                       declines, the agent runs `opt-out` and Mnemex goes silent for the session. When
                       no graph is configured, emit a one-time (durable, fires once ever) onboarding
                       notice pointing at /mnemex:mnx-init instead of staying silent. Stays silent if
                       the session is already muted.
  opt-out / opt-in   : toggle the paired per-session MUTE + CONSENT markers (argv: --session <id>).
                       opt-out (mute ON, consent OFF) is how the agent honors "don't use Mnemex this
                       session" — every other Mnemex hook checks the mute marker and goes silent.
                       opt-in (mute OFF, consent ON) records the user's "yes" and arms the per-prompt
                       reminder below. Take no stdin; safe to run as a plain command.
  user-prompt-submit : (UserPromptSubmit) once the user has consented (opt-in), inject a short
                       read-before-domain-work / capture reminder on EVERY prompt. Silent when muted or
                       when the consent question has not been answered yet (SessionStart owns the ask).
  stop               : when the agent wraps up a turn, batch-flush this turn's pending usage stamps
                       (mnx_stamp.flush; silent, advisory), then interrupt ONCE per session (or once
                       more per compaction — see pre-compact) to have it ask whether durable knowledge
                       should be staged with mnx-capture. Guarded against nag-loops (session marker +
                       stop_hook_active); never auto-writes.
  pre-compact        : (PreCompact) fires right before the transcript is summarized — the one moment
                       session detail is actually LOST. PreCompact cannot inject context or block, so
                       it instead RE-ARMS the Stop capture nudge (clears the once-per-session marker and
                       records that a compaction happened), so the very next Stop re-asks the agent to
                       stage the delta from the window about to be compacted. Best-effort flush of
                       pending stamps too. No-op when muted; never auto-writes.
  session-end        : flush any remaining usage stamps (safety net) and prompt to capture durable
                       knowledge with mnx-capture, and nag on staged-pending / consolidation-overdue
                       (advisory; never auto-writes, never auto-promotes).
  pre-commit-gate    : (PreToolUse/Bash) if the pending command is a git commit INSIDE the bound
                       graph repo, run mnx_doctor.check and DENY the commit on error-level findings,
                       so a structurally broken graph cannot be committed. Only fires for commits in
                       the graph repo — never the author's project repo. Fails OPEN on any internal
                       error (never blocks on our own bug).
  post-apply-check   : (PostToolUse/Bash) after a Mnemex mutation command, surface a stranded
                       pass.plan.json / unreleased team lock (a crashed gc/write) so it does not
                       silently wedge the next pass. Advisory — never blocks.

Contract: with the single exception of the pre-commit-gate DENY, hooks are ADVISORY. They never
raise and never block on internal errors — a failing hook must not disrupt the user's session
(the gate fails open). See docs/skills-commands-hooks.md §6.

Dependencies: Python 3.9+ stdlib + PyYAML only.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import mnx_binding
import mnx_common
import mnx_stamp


# --------------------------------------------------------------------------- #
# HookOutcome — the host-neutral result of one hook event
# --------------------------------------------------------------------------- #

@dataclass
class HookOutcome:
    """What a hook event wants delivered, independent of any host's wire shape.

    At most one delivery intent is set per event (the adapter renders the first that
    applies, in the order below); ``notices`` may accompany none-of-the-above surfaces
    like SessionEnd. All-empty means the hook stays silent — the common case.

      deny_reason  — deny the pending tool call (the pre-commit gate; the ONE blocker)
      block_reason — interrupt the host's wrap-up so the agent gets one more turn (Stop)
      context      — advisory text to inject into the model's context
      notices      — plain one-way lines for surfaces that cannot inject context
    """
    deny_reason: str | None = None
    block_reason: str | None = None
    context: str | None = None
    notices: list[str] = field(default_factory=list)

    @property
    def silent(self) -> bool:
        return not (self.deny_reason or self.block_reason or self.context or self.notices)


# The command generator for the consent primer's opt-in/opt-out instructions. This is one of
# the two places the literal ${CLAUDE_PLUGIN_ROOT} survives (with hooks/hooks.json) — Claude
# Code expands it. Foreign hosts pass their own base (e.g. `python3 -m openmnemex.mnx_hooks`).
CLAUDE_HOOK_CMD_BASE = 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_hooks.py"'


def _session_nags(binding) -> list[str]:
    """One-line nags for session start / end — staged-pending and consolidation-overdue.

    Advisory only: NEVER auto-runs capture/promote/consolidate (HITL intrusion + violates
    deliberate-promote; read's tail-fold keeps routing correct between consolidations). Best-effort
    and fully guarded so a partial/broken graph never breaks the hook."""
    nags: list[str] = []
    try:  # staged-pending (sharper past the soft bound or with any urgent atom)
        import mnx_stage
        st = mnx_stage.status(binding)
        count, urgent, level = st.get("count", 0), st.get("urgent", 0), st.get("budget", {}).get("level")
        if count:
            sharp = " — promote is due" if level in ("soft", "hard") else ""
            urg = f", {urgent} urgent" if urgent else ""
            nags.append(f"Mnemex: {count} staged capture(s){urg} not yet promoted{sharp}. "
                        f"Run /mnemex:mnx-promote to merge them into the graph.")
    except Exception:
        pass
    try:  # committed-but-unpushed promote (a push that crashed/went offline) — risk of double-apply
        st = mnx_binding.unpushed_state(binding)
        if st.get("unpushed"):
            nags.append(f"Mnemex: a previous promote committed locally but did not push "
                        f"({st.get('ahead')} commit(s) ahead). Run /mnemex:mnx-promote --retry-push to "
                        f"push it — do NOT start a fresh promote (it would double-apply).")
    except Exception:
        pass
    try:  # consolidation-overdue (any team past its cadence)
        import mnx_compact
        import mnx_config
        root = Path(binding.graph_root())
        now = mnx_common_now()
        overdue_teams = []
        if root.is_dir():
            for team_dir in sorted(p for p in root.iterdir()
                                   if p.is_dir() and p.name.startswith("team-")):
                try:
                    cfg = mnx_config.load(str(team_dir))
                    ov = mnx_compact.overdue(str(team_dir), cfg, now)
                    if ov.get("due"):
                        overdue_teams.append((team_dir.name, int(ov.get("days_overdue") or 0)))
                except Exception:
                    continue
        if overdue_teams:
            worst = max(d for _, d in overdue_teams)
            nags.append(f"Mnemex: graph consolidation overdue ({len(overdue_teams)} team(s), up to "
                        f"{worst}d). Run /mnemex:mnx-promote (consolidation is its back half).")
    except Exception:
        pass
    return nags


def mnx_common_now() -> str:
    return mnx_common.now_utc()


def _read_event() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _run_dir() -> Path:
    return mnx_common.mnemex_home() / "run"


def _safe_session(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", session_id or "") or "default"


def _stop_marker(session_id: str) -> Path:
    """Per-session marker so the Stop nudge fires at most once, not on every turn."""
    return _run_dir() / f"stop-nudged-{_safe_session(session_id)}"


def _compaction_marker(session_id: str) -> Path:
    """Per-session marker set by PreCompact and consumed by the next Stop nudge.

    Its presence tells Stop that its nudge follows a compaction (a real transcript-loss event),
    so Stop can sharpen the reason to 'stage the delta from the window that was just summarized'
    instead of the generic once-per-session prompt. Cleaned up at SessionEnd.
    """
    return _run_dir() / f"compaction-seen-{_safe_session(session_id)}"


def _mute_marker(session_id: str) -> Path:
    """Per-session marker set when the user declines Mnemex for this session (opt-out).

    Every Mnemex hook checks this first and goes silent when it is present — that is how
    'drop Mnemex for this session' is enforced without unregistering skills. Cleared at
    SessionEnd so the next session asks for consent again.
    """
    return _run_dir() / f"muted-{_safe_session(session_id)}"


def _is_muted(session_id: str) -> bool:
    try:
        return _mute_marker(session_id).exists()
    except Exception:
        return False


def _consent_marker(session_id: str) -> Path:
    """Per-session marker set when the user AGREES to Mnemex for this session (opt-in).

    It is the positive counterpart to the mute marker: mute records 'no', consent records 'yes'.
    Its presence is what arms the per-prompt UserPromptSubmit reminder — the hook injects the
    read/capture context on every prompt only while this marker exists. Absent (and not muted)
    means the user has not answered the session-start consent question yet, so the per-prompt
    hook stays silent. Cleared at SessionEnd so the next session re-asks.
    """
    return _run_dir() / f"consented-{_safe_session(session_id)}"


def _is_consented(session_id: str) -> bool:
    try:
        return _consent_marker(session_id).exists()
    except Exception:
        return False


def core_set_mute(session_id: str, on: bool) -> dict:
    """opt-out (on=True) / opt-in (on=False). Toggles the paired mute + consent markers.

    opt-out  -> mute ON,  consent OFF (user declined; every hook goes silent).
    opt-in   -> mute OFF, consent ON  (user agreed; arms the per-prompt read reminder).
    Never raises; returns the resulting state for the adapter to render."""
    m, c = _mute_marker(session_id), _consent_marker(session_id)
    try:
        m.parent.mkdir(parents=True, exist_ok=True)
        if on:
            m.write_text("muted", encoding="utf-8")
            c.unlink(missing_ok=True)
        else:
            m.unlink(missing_ok=True)
            c.write_text("consented", encoding="utf-8")
    except Exception:
        pass
    return {"mnemex": "muted" if on else "active", "session": session_id}


def _onboarded_marker() -> Path:
    """One-time marker so the 'no graph configured' onboarding notice fires once, ever."""
    return _run_dir() / "onboarded"


def _graph_label(binding) -> str:
    return binding.remote or binding.local_path or "graph"


def _flush_usage_stamps() -> dict:
    """Best-effort batched flush of pending remote usage stamps. Never raises.

    mnx-read appends stamps to a session-durable spill outside the clone (see mnx_stamp);
    this is where they get replayed into the registry and pushed, in one batch, so reads
    no longer commit+push per stamp and the signal survives the session-start reset.
    """
    try:
        return mnx_stamp.flush()
    except Exception as exc:
        return {"action": "skipped", "error": str(exc)}


def _onboard_outcome() -> HookOutcome:
    """The one-time onboarding notice when Mnemex is installed but no graph is bound.

    Without this, a fresh install is completely silent at the one moment the user most
    needs direction. Fires at most once ever (a durable marker under the mnemex home), so
    it never nags users who run Mnemex in projects that intentionally have no graph.
    """
    marker = _onboarded_marker()
    if marker.exists():
        return HookOutcome()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("shown", encoding="utf-8")
    except Exception:
        return HookOutcome()  # cannot persist the marker -> stay silent rather than nag every session
    return HookOutcome(context=(
        "Mnemex is installed but no knowledge graph is configured for this project. "
        "If the user wants persistent, self-pruning agent memory, run /mnemex:mnx-init "
        "to create or bind a graph. Otherwise ignore this — it will not be shown again."
    ))


# --------------------------------------------------------------------------- #
# core per-event logic (host-neutral: event dict in, HookOutcome out)
# --------------------------------------------------------------------------- #

def core_session_start(event: dict, hook_cmd_base: str = CLAUDE_HOOK_CMD_BASE) -> HookOutcome:
    """Blocking: sync the bound graph, then ask the agent to get the user's CONSENT for the session.

    Instead of nudging on every turn, Mnemex asks once at the top of the session whether to use the
    graph. If the user agrees, the agent reads before domain work and writes to capture; if not, the
    agent runs `opt-out` (the command is handed to it with this session's id baked in) and every other
    Mnemex hook goes silent for the rest of the session. Stays silent if already muted.
    ``hook_cmd_base`` is how the primer tells the agent to run opt-in/opt-out on THIS host.
    """
    binding = mnx_binding.resolve()
    if binding is None:
        return _onboard_outcome()  # no graph configured — nudge to mnx-init (once)
    sid = str(event.get("session_id", ""))
    if _is_muted(sid):
        return HookOutcome()  # user already muted Mnemex this session (e.g. SessionStart re-fired on resume)
    res = mnx_binding.sync(binding)
    action = res.get("action")
    label = _graph_label(binding)

    if action in ("cloned", "resynced", "local"):
        status, available = res.get("message"), True
    elif action in ("offline", "skipped-dirty", "skipped-unpushed"):
        # usable but degraded: read-only offline, or local work preserved instead of resyncing
        status, available = res.get("message"), True
    else:  # error
        status = f"could not prepare the graph. {res.get('detail', '')}".strip()
        available = False

    lines = [f"Mnemex: {status} ({label})"]
    if available:
        team = f" (default team: {binding.default_team})" if binding.default_team else ""
        optin_cmd = f"{hook_cmd_base} opt-in --session {sid}"
        mute_cmd = f"{hook_cmd_base} opt-out --session {sid}"
        lines.append(
            "A Mnemex knowledge graph is available" + team + ". Get the user's consent ONCE, up front, "
            "before doing domain work this session — ask plainly whether they want you to use Mnemex "
            "memory for this session.\n"
            "  • If YES — record the consent by running this once:\n"
            f"      {optin_cmd}\n"
            "    From then on a per-prompt reminder keeps the read/capture routing in front of you: "
            "before working on a task in a domain it may cover, load prior knowledge first with the "
            "mnx-read skill (or /mnemex:mnx-read); if the session produces durable knowledge or human "
            "review decisions, stage it before the end with /mnemex:mnx-capture (cheap + local). "
            "Merging staged captures into the shared graph is the separate, deliberate "
            "/mnemex:mnx-promote step — do NOT auto-run it.\n"
            "  • If NO / not now — do NOT use any Mnemex skill this session, and silence further Mnemex "
            "prompts (the per-prompt reminder and the end-of-session capture reminder) by running this "
            "once:\n"
            f"      {mute_cmd}\n"
            "    After muting, do not bring Mnemex up again unless the user explicitly asks for it."
        )
        lines += _session_nags(binding)  # staged-pending / consolidation-overdue (nag only; no auto-run)
    return HookOutcome(context="\n".join(lines))


def core_user_prompt_submit(event: dict) -> HookOutcome:
    """Once the user has CONSENTED, inject a short read/capture reminder on every prompt.

    Silent otherwise: muted (user said no) or not-yet-answered (no consent marker) both no-op,
    and it stays silent when no graph is bound. This is the per-turn counterpart to the one-time
    session-start primer — consent is asked once, then this keeps the routing in front of the agent.
    """
    sid = str(event.get("session_id", ""))
    if _is_muted(sid) or not _is_consented(sid):
        return HookOutcome()  # user declined, or has not agreed yet — the session-start primer owns the ask
    binding = mnx_binding.resolve()
    if binding is None:
        return HookOutcome()  # no graph bound here — nothing to route to
    return HookOutcome(context=(
        "Mnemex is active for this session. Before working on a task in a domain the graph may cover, "
        "load prior knowledge first with the mnx-read skill (or /mnemex:mnx-read). If this turn produces "
        "durable knowledge or review decisions, stage it with /mnemex:mnx-capture (cheap + local; do NOT "
        "auto-promote)."
    ))


def core_stop(event: dict) -> HookOutcome:
    """Interrupt the agent's wrap-up ONCE per session to have it ask about capturing knowledge.

    Unlike session-end (which fires after the conversation is over and can only emit a one-way
    line), Stop fires while the session is live, so blocking with a reason gives the agent a turn
    to ask the user whether durable knowledge should be staged with mnx-capture.

    Loop-safety is layered: `stop_hook_active` short-circuits the immediate continuation, and a
    per-session marker prevents re-nudging on every subsequent turn. The nudge re-arms exactly once
    per compaction (PreCompact clears the marker), so re-prompts are bounded by real transcript-loss
    events, not a timer. If the marker can't be written we stay silent rather than risk repeated
    interruptions. If the session is muted (the user declined Mnemex at session start), this is a no-op.
    """
    sid = str(event.get("session_id", ""))
    if _is_muted(sid):
        return HookOutcome()  # user declined Mnemex this session — no stamps, no capture nudge
    _flush_usage_stamps()  # batch-push this turn's usage stamps (silent; advisory)
    if event.get("stop_hook_active"):
        return HookOutcome()  # this stop is already the result of our nudge — let it through
    binding = mnx_binding.resolve()
    if binding is None:
        return HookOutcome()  # no graph bound here — nothing to capture
    marker = _stop_marker(sid)
    if marker.exists():
        return HookOutcome()  # already nudged this session (and no compaction has re-armed it since)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("nudged", encoding="utf-8")
    except Exception:
        return HookOutcome()  # cannot persist the marker -> don't block, to avoid a nag loop
    # Was this nudge re-armed by a compaction? If so, sharpen it to the delta that was just lost.
    after_compaction = False
    cmark = _compaction_marker(sid)
    try:
        after_compaction = cmark.exists()
        cmark.unlink(missing_ok=True)  # consume it so we don't repeat this framing next turn
    except Exception:
        after_compaction = False
    if after_compaction:
        reason = (
            "Mnemex: the conversation was just summarized — the detail in the compacted window is now "
            "gone from your context. If that window produced durable knowledge or human-review "
            "decisions, stage it now with /mnemex:mnx-capture before continuing. Capture is "
            "incremental: it consults what is already staged and stages only the delta (re-capturing "
            "identical content is a no-op), so run it freely at these checkpoints. Merging into the "
            "graph stays the separate /mnemex:mnx-promote step. If nothing durable was produced in that "
            "window, say so briefly and continue."
        )
    else:
        reason = (
            "Mnemex one-time check before you finish: if this session produced durable knowledge, "
            "human-review decisions, or reusable context, ask the user whether to stage it with "
            "/mnemex:mnx-capture before concluding (capture is cheap, local, and incremental — it "
            "stages only the delta over what is already staged). Merging into the graph is the separate "
            "/mnemex:mnx-promote step. If nothing durable was produced, say so briefly and stop. This "
            "reminder fires once per session (and once more after any compaction)."
        )
    return HookOutcome(block_reason=reason)


def core_pre_compact(event: dict) -> HookOutcome:
    """Right before the transcript is summarized, re-arm the Stop capture nudge for the lost window.

    Compaction is the one moment session detail is actually destroyed — precisely when uncaptured
    knowledge is at risk. PreCompact cannot inject context or block the compaction, so it does the
    one durable thing it can: it clears the once-per-session Stop marker and records that a compaction
    happened, so the very next Stop re-prompts the agent (with a delta-flavored reason) to stage what
    the compacted window produced. Re-nudging is therefore bounded by real loss events, not a timer.
    Best-effort flush of pending usage stamps too. No-op when muted; never auto-writes.
    """
    sid = str(event.get("session_id", ""))
    if _is_muted(sid):
        return HookOutcome()  # user declined Mnemex this session — no re-arm, no flush
    binding = mnx_binding.resolve()
    if binding is None:
        return HookOutcome()  # no graph bound here — nothing to capture
    _flush_usage_stamps()  # safety: batch out any stamps this turn's Stop hasn't flushed yet
    try:  # re-arm the Stop nudge: drop the once-per-session marker, mark the loss event
        _compaction_marker(sid).parent.mkdir(parents=True, exist_ok=True)
        _stop_marker(sid).unlink(missing_ok=True)
        _compaction_marker(sid).write_text("compacted", encoding="utf-8")
    except Exception:
        pass  # can't persist markers -> stay silent rather than misbehave
    return HookOutcome()


def core_session_end(event: dict) -> HookOutcome:
    """Advisory nudge to persist knowledge. Never auto-writes.

    A hook script cannot inspect the transcript to confirm 'knowledge-bearing work happened', so this
    reminder is unconditional whenever a graph is bound. The agent/user decides whether to act.
    """
    sid = str(event.get("session_id", ""))
    muted = _is_muted(sid)
    for marker in (_stop_marker(sid), _mute_marker(sid), _consent_marker(sid), _compaction_marker(sid)):
        try:  # tidy per-session markers so the next session re-asks consent / re-nudges
            marker.unlink(missing_ok=True)
        except Exception:
            pass
    if muted:
        return HookOutcome()  # user declined Mnemex this session — nothing to flush or nudge
    binding = mnx_binding.resolve()
    if binding is None:
        return HookOutcome()
    notices: list[str] = []
    res = _flush_usage_stamps()  # safety net: flush anything the per-turn Stop flush missed
    if res.get("action") == "flushed":
        notices.append(f"Mnemex: flushed {res.get('pending')} usage stamp(s) to the graph.")
    elif res.get("action") == "deferred":
        notices.append("Mnemex: usage stamps could not be pushed (offline?); they are retained and "
                       "will flush next session.")
    notices.append("Mnemex: if this session produced durable knowledge or review decisions, stage it with "
                   "/mnemex:mnx-capture so it is not lost (merge it later with /mnemex:mnx-promote).")
    notices.extend(_session_nags(binding))  # staged-pending / consolidation-overdue (nag only)
    return HookOutcome(notices=notices)


# --- tool-call gates (PreToolUse / PostToolUse, Bash matcher) ---------------

_GIT_COMMIT_RE = re.compile(r"\bgit\b[^\n|;&]*\bcommit\b")
_MNEMEX_CMD_RE = re.compile(r"mnx[_-]|mnemex")


def _tool_command(event: dict) -> str:
    """The bash command string from a PreToolUse/PostToolUse event (Bash tool)."""
    ti = event.get("tool_input") or {}
    return ti.get("command", "") if isinstance(ti, dict) else ""


def _unquote_path(token: str) -> str:
    t = token.strip()
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        t = t[1:-1]
    return os.path.expanduser(t)


def _effective_dir(command: str, cwd: str) -> str:
    """Best-effort working directory of `command`: an explicit `git -C <dir>`, a leading
    `cd <dir> && …`, otherwise the session cwd. Relative dirs are resolved against cwd."""
    m = re.search(r"\bgit\s+-C\s+(\"[^\"]+\"|'[^']+'|\S+)", command)
    if not m:
        m = re.match(r"\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*(?:&&|;)", command)
    if m:
        d = _unquote_path(m.group(1))
        return d if os.path.isabs(d) else os.path.join(cwd, d)
    return cwd


def _within_graph(target: str, graph_root: str) -> bool:
    """True iff `target` is the graph root or a directory inside it (a commit there hits the graph)."""
    try:
        t = Path(target).resolve()
        g = Path(graph_root).resolve()
    except Exception:
        return False
    return t == g or g in t.parents


def core_pre_commit_gate(event: dict) -> HookOutcome:
    """Deny a `git commit` inside the bound graph repo when the doctor finds error-level invariant
    violations — a structurally broken graph must not be committed. Only fires for commits in the
    graph repo (never the author's project), and fails OPEN on any internal error."""
    command = _tool_command(event)
    if not command or not _GIT_COMMIT_RE.search(command):
        return HookOutcome()  # not a git commit
    binding = mnx_binding.resolve()
    if binding is None:
        return HookOutcome()  # no graph bound — nothing to gate
    graph_root = binding.graph_root()
    target = _effective_dir(command, event.get("cwd") or os.getcwd())
    if not _within_graph(target, graph_root):
        return HookOutcome()  # commit targets the author's project, not the graph — never interfere
    try:
        import mnx_doctor
        report = mnx_doctor.check(graph_root)
    except Exception:
        return HookOutcome()  # cannot validate (graph not scaffolded, parse error, …) — fail open
    errs = [f for f in report.get("findings", []) if f.get("severity") == "E"]
    if not errs:
        return HookOutcome()  # graph is clean — allow the commit
    preview = "; ".join(
        f"[inv {f.get('invariant')}] {f.get('node_or_edge')}: {f.get('detail')}" for f in errs[:5])
    more = f" (+{len(errs) - 5} more)" if len(errs) > 5 else ""
    return HookOutcome(deny_reason=(
        f"Mnemex blocked this commit: the graph has {len(errs)} error-level invariant "
        f"violation(s); committing would persist a broken graph. {preview}{more}. "
        f"Run /mnemex:mnx-doctor --fix to rebuild derived files (indexes, cross-links, "
        f"reverse map); fix any node-level corruption by hand, then commit again."
    ))


def core_post_apply_check(event: dict) -> HookOutcome:
    """After a Mnemex mutation command, surface a stranded pass.plan.json / unreleased lock (a
    crashed gc/write) so it does not silently wedge the next pass. Advisory — never blocks."""
    command = _tool_command(event)
    if not command or not _MNEMEX_CMD_RE.search(command):
        return HookOutcome()  # not a mnemex command — don't scan the graph on every Bash call
    binding = mnx_binding.resolve()
    if binding is None:
        return HookOutcome()
    root = Path(binding.graph_root())
    if not root.is_dir():
        return HookOutcome()
    try:
        import mnx_lock
    except Exception:
        return HookOutcome()
    stranded: list[tuple[str, dict]] = []
    for team_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("team-")):
        try:
            if mnx_lock.in_progress(str(team_dir)):
                stranded.append((team_dir.name, mnx_lock.recover(str(team_dir))))
        except Exception:
            continue
    if not stranded:
        return HookOutcome()
    lines = ["Mnemex: a maintenance/write pass left state behind — a gc/write may have crashed "
             "(or one is still running concurrently):"]
    for name, rec in stranded:
        tree = "dirty" if rec.get("dirty") else "clean"
        lines.append(f"  - {name}: pass.plan.json present, working tree {tree} "
                     f"→ recommended: {rec.get('action')}")
    lines.append("If no pass is running, recover before the next write/gc: 'rollback' = "
                 "`git checkout .` in the graph (restore the last good commit); 'replay' = the "
                 "commit landed, just clear the stranded plan. mnx-doctor flags this as invariant 16.")
    return HookOutcome(context="\n".join(lines))


# --------------------------------------------------------------------------- #
# Claude Code adapter (stdin event JSON in, Claude hook output shape on stdout)
# --------------------------------------------------------------------------- #

def _emit_claude(event_name: str, outcome: HookOutcome) -> int:
    """Render a HookOutcome in Claude Code's hook output shape. Silent outcome emits nothing."""
    if outcome.deny_reason is not None:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "permissionDecision": "deny",
                "permissionDecisionReason": outcome.deny_reason,
            }
        }))
    elif outcome.block_reason is not None:
        print(json.dumps({"decision": "block", "reason": outcome.block_reason}))
    elif outcome.context is not None:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": outcome.context,
            }
        }))
    for line in outcome.notices:
        print(line)
    return 0


def _set_mute(session_id: str, on: bool) -> int:
    """Claude adapter for opt-out/opt-in: argv-driven plain command, prints the state JSON."""
    print(json.dumps(core_set_mute(session_id, on)))
    return 0


def session_start() -> int:
    """Claude adapter: stdin event → core_session_start → SessionStart context injection."""
    return _emit_claude("SessionStart", core_session_start(_read_event()))


def user_prompt_submit() -> int:
    """Claude adapter: stdin event → core_user_prompt_submit → UserPromptSubmit context injection."""
    return _emit_claude("UserPromptSubmit", core_user_prompt_submit(_read_event()))


def stop() -> int:
    """Claude adapter: stdin event → core_stop → Stop block decision (or silence)."""
    return _emit_claude("Stop", core_stop(_read_event()))


def pre_compact() -> int:
    """Claude adapter: stdin event → core_pre_compact (side effects only; always silent)."""
    return _emit_claude("PreCompact", core_pre_compact(_read_event()))


def session_end() -> int:
    """Claude adapter: stdin event → core_session_end → plain advisory lines."""
    return _emit_claude("SessionEnd", core_session_end(_read_event()))


def pre_commit_gate() -> int:
    """Claude adapter: stdin event → core_pre_commit_gate → PreToolUse deny (or silence)."""
    return _emit_claude("PreToolUse", core_pre_commit_gate(_read_event()))


def post_apply_check() -> int:
    """Claude adapter: stdin event → core_post_apply_check → PostToolUse context injection."""
    return _emit_claude("PostToolUse", core_post_apply_check(_read_event()))


def _session_arg(argv: list[str]) -> str:
    if "--session" in argv:
        i = argv.index("--session")
        if i + 1 < len(argv):
            return argv[i + 1]
    return ""


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        # opt-out / opt-in are agent-run plain commands (argv-driven, no stdin event).
        if cmd == "opt-out":
            return _set_mute(_session_arg(argv), True)
        if cmd == "opt-in":
            return _set_mute(_session_arg(argv), False)
        if cmd == "session-start":
            return session_start()
        if cmd == "user-prompt-submit":
            return user_prompt_submit()
        if cmd == "stop":
            return stop()
        if cmd == "pre-compact":
            return pre_compact()
        if cmd == "session-end":
            return session_end()
        if cmd == "pre-commit-gate":
            return pre_commit_gate()
        if cmd == "post-apply-check":
            return post_apply_check()
        _read_event()  # unknown subcommand — consume stdin and exit cleanly
        return 0
    except Exception as exc:  # advisory hooks must never break a session (the gate fails open)
        sys.stderr.write(f"mnx_hooks: {cmd} skipped: {exc}\n")
        return 0


main = _main  # back-compat alias; `_main(argv)` is the engine-wide dispatcher name (plan v2, 0e)

if __name__ == "__main__":
    sys.exit(_main(sys.argv))
