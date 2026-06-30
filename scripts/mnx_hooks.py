"""mnx_hooks.py — Claude Code hook entrypoints (advisory; never mutate knowledge).

Subcommands (argv[1]):
  session-start      : resolve the binding and sync the graph (blocking), then inject a one-time
                       primer that asks the agent to get the user's CONSENT for this session: use
                       Mnemex (read before domain work, write to capture) — or mute it. If the user
                       declines, the agent runs `opt-out` and Mnemex goes silent for the session. When
                       no graph is configured, emit a one-time (durable, fires once ever) onboarding
                       notice pointing at /mnemex:mnx-init instead of staying silent. Stays silent if
                       the session is already muted.
  opt-out / opt-in   : write / clear a per-session MUTE marker (argv: --session <id>). opt-out is how
                       the agent honors "don't use Mnemex this session" — every other Mnemex hook
                       checks the marker and goes silent, effectively dropping Mnemex for the session.
                       Take no stdin; safe to run as a plain command.
  stop               : when the agent wraps up a turn, batch-flush this turn's pending usage stamps
                       (mnx_stamp.flush; silent, advisory), then interrupt ONCE per session to have
                       it ask whether durable knowledge should be staged with mnx-capture. Guarded
                       against nag-loops (session marker + stop_hook_active); never auto-writes.
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
(the gate fails open). Hooks read event JSON on stdin per the Claude Code hooks protocol.
See docs/04-skills-commands-hooks.md §6.

Dependencies: Python 3.9+ stdlib + PyYAML only.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import mnx_binding
import mnx_stamp


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
    import mnx_common
    return mnx_common.now_utc()


def _read_event() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _run_dir() -> Path:
    return mnx_binding.claude_home() / "mnemex" / "run"


def _safe_session(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", session_id or "") or "default"


def _stop_marker(session_id: str) -> Path:
    """Per-session marker so the Stop nudge fires at most once, not on every turn."""
    return _run_dir() / f"stop-nudged-{_safe_session(session_id)}"


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


def _set_mute(session_id: str, on: bool) -> int:
    """opt-out (on=True) / opt-in (on=False). Writes or clears the per-session mute marker.
    Reads no stdin and never raises — the agent runs it as a plain command."""
    m = _mute_marker(session_id)
    try:
        m.parent.mkdir(parents=True, exist_ok=True)
        if on:
            m.write_text("muted", encoding="utf-8")
        else:
            m.unlink(missing_ok=True)
    except Exception:
        pass
    print(json.dumps({"mnemex": "muted" if on else "active", "session": session_id}))
    return 0


def _onboarded_marker() -> Path:
    """One-time marker so the 'no graph configured' onboarding notice fires once, ever."""
    return mnx_binding.claude_home() / "mnemex" / "run" / "onboarded"


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


def _onboard_notice() -> int:
    """Fire a one-time onboarding notice when Mnemex is installed but no graph is bound.

    Without this, a fresh install is completely silent at the one moment the user most
    needs direction. Fires at most once ever (a durable marker under ~/.claude), so it
    never nags users who run Mnemex in projects that intentionally have no graph.
    """
    marker = _onboarded_marker()
    if marker.exists():
        return 0
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("shown", encoding="utf-8")
    except Exception:
        return 0  # cannot persist the marker -> stay silent rather than nag every session
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "Mnemex is installed but no knowledge graph is configured for this project. "
                "If the user wants persistent, self-pruning agent memory, run /mnemex:mnx-init "
                "to create or bind a graph. Otherwise ignore this — it will not be shown again."
            ),
        }
    }))
    return 0


def session_start() -> int:
    """Blocking: sync the bound graph, then ask the agent to get the user's CONSENT for the session.

    Instead of nudging on every turn, Mnemex asks once at the top of the session whether to use the
    graph. If the user agrees, the agent reads before domain work and writes to capture; if not, the
    agent runs `opt-out` (the command is handed to it with this session's id baked in) and every other
    Mnemex hook goes silent for the rest of the session. Stays silent if already muted.
    """
    event = _read_event()
    binding = mnx_binding.resolve()
    if binding is None:
        return _onboard_notice()  # no graph configured — nudge to mnx-init (once)
    sid = str(event.get("session_id", ""))
    if _is_muted(sid):
        return 0  # user already muted Mnemex this session (e.g. SessionStart re-fired on resume)
    res = mnx_binding.sync(binding)
    action = res.get("action")
    label = _graph_label(binding)

    if action in ("cloned", "resynced", "local"):
        status, available = res.get("message"), True
    elif action == "offline":
        status, available = res.get("message"), True  # usable, read-only
    else:  # error
        status = f"could not prepare the graph. {res.get('detail', '')}".strip()
        available = False

    lines = [f"Mnemex: {status} ({label})"]
    if available:
        team = f" (default team: {binding.default_team})" if binding.default_team else ""
        mute_cmd = (f'python3 "${{CLAUDE_PLUGIN_ROOT}}/scripts/mnx_hooks.py" opt-out --session {sid}')
        lines.append(
            "A Mnemex knowledge graph is available" + team + ". Get the user's consent ONCE, up front, "
            "before doing domain work this session — ask plainly whether they want you to use Mnemex "
            "memory for this session.\n"
            "  • If YES — before working on a task in a domain it may cover, load prior knowledge first "
            "with the mnx-read skill (or /mnemex:mnx-read); if the session produces durable knowledge or "
            "human review decisions, stage it before the end with /mnemex:mnx-capture (cheap + local). "
            "Merging staged captures into the shared graph is the separate, deliberate "
            "/mnemex:mnx-promote step — do NOT auto-run it.\n"
            "  • If NO / not now — do NOT use any Mnemex skill this session, and silence further Mnemex "
            "prompts (the end-of-session capture reminder) by running this once:\n"
            f"      {mute_cmd}\n"
            "    After muting, do not bring Mnemex up again unless the user explicitly asks for it."
        )
        lines += _session_nags(binding)  # staged-pending / consolidation-overdue (nag only; no auto-run)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }))
    return 0


def stop() -> int:
    """Interrupt the agent's wrap-up ONCE per session to have it ask about capturing knowledge.

    Unlike session-end (which fires after the conversation is over and can only emit a one-way
    line), Stop fires while the session is live, so blocking with a reason gives the agent a turn
    to ask the user whether durable knowledge should be staged with mnx-capture.

    Loop-safety is layered: `stop_hook_active` short-circuits the immediate continuation, and a
    per-session marker prevents re-nudging on every subsequent turn. If the marker can't be
    written we stay silent rather than risk repeated interruptions. If the session is muted
    (the user declined Mnemex at session start), this is a no-op.
    """
    event = _read_event()
    sid = str(event.get("session_id", ""))
    if _is_muted(sid):
        return 0  # user declined Mnemex this session — no stamps, no capture nudge
    _flush_usage_stamps()  # batch-push this turn's usage stamps (silent; advisory)
    if event.get("stop_hook_active"):
        return 0  # this stop is already the result of our nudge — let it through
    binding = mnx_binding.resolve()
    if binding is None:
        return 0  # no graph bound here — nothing to capture
    marker = _stop_marker(sid)
    if marker.exists():
        return 0  # already nudged once this session
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("nudged", encoding="utf-8")
    except Exception:
        return 0  # cannot persist the marker -> don't block, to avoid a nag loop
    print(json.dumps({
        "decision": "block",
        "reason": (
            "Mnemex one-time check before you finish: if this session produced durable knowledge, "
            "human-review decisions, or reusable context, ask the user whether to stage it with "
            "/mnemex:mnx-capture before concluding (capture is cheap + local; merging into the graph "
            "is the separate /mnemex:mnx-promote step). If nothing durable was produced, say so "
            "briefly and stop. This reminder fires only once per session."
        ),
    }))
    return 0


def session_end() -> int:
    """Advisory nudge to persist knowledge. Never auto-writes.

    A hook script cannot inspect the transcript to confirm 'knowledge-bearing work happened', so this
    reminder is unconditional whenever a graph is bound. The agent/user decides whether to act.
    """
    event = _read_event()
    sid = str(event.get("session_id", ""))
    muted = _is_muted(sid)
    for marker in (_stop_marker(sid), _mute_marker(sid)):
        try:  # tidy per-session markers so the next session re-asks consent / re-nudges
            marker.unlink(missing_ok=True)
        except Exception:
            pass
    if muted:
        return 0  # user declined Mnemex this session — nothing to flush or nudge
    binding = mnx_binding.resolve()
    if binding is None:
        return 0
    res = _flush_usage_stamps()  # safety net: flush anything the per-turn Stop flush missed
    if res.get("action") == "flushed":
        print(f"Mnemex: flushed {res.get('pending')} usage stamp(s) to the graph.")
    elif res.get("action") == "deferred":
        print("Mnemex: usage stamps could not be pushed (offline?); they are retained and "
              "will flush next session.")
    print("Mnemex: if this session produced durable knowledge or review decisions, stage it with "
          "/mnemex:mnx-capture so it is not lost (merge it later with /mnemex:mnx-promote).")
    for nag in _session_nags(binding):  # staged-pending / consolidation-overdue (nag only)
        print(nag)
    return 0


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


def pre_commit_gate() -> int:
    """Deny a `git commit` inside the bound graph repo when the doctor finds error-level invariant
    violations — a structurally broken graph must not be committed. Only fires for commits in the
    graph repo (never the author's project), and fails OPEN on any internal error."""
    event = _read_event()
    command = _tool_command(event)
    if not command or not _GIT_COMMIT_RE.search(command):
        return 0  # not a git commit
    binding = mnx_binding.resolve()
    if binding is None:
        return 0  # no graph bound — nothing to gate
    graph_root = binding.graph_root()
    target = _effective_dir(command, event.get("cwd") or os.getcwd())
    if not _within_graph(target, graph_root):
        return 0  # commit targets the author's project, not the graph — never interfere
    try:
        import mnx_doctor
        report = mnx_doctor.check(graph_root)
    except Exception:
        return 0  # cannot validate (graph not scaffolded, parse error, …) — fail open
    errs = [f for f in report.get("findings", []) if f.get("severity") == "E"]
    if not errs:
        return 0  # graph is clean — allow the commit
    preview = "; ".join(
        f"[inv {f.get('invariant')}] {f.get('node_or_edge')}: {f.get('detail')}" for f in errs[:5])
    more = f" (+{len(errs) - 5} more)" if len(errs) > 5 else ""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Mnemex blocked this commit: the graph has {len(errs)} error-level invariant "
                f"violation(s); committing would persist a broken graph. {preview}{more}. "
                f"Run /mnemex:mnx-doctor --fix to rebuild derived files (indexes, cross-links, "
                f"reverse map); fix any node-level corruption by hand, then commit again."
            ),
        }
    }))
    return 0


def post_apply_check() -> int:
    """After a Mnemex mutation command, surface a stranded pass.plan.json / unreleased lock (a
    crashed gc/write) so it does not silently wedge the next pass. Advisory — never blocks."""
    event = _read_event()
    command = _tool_command(event)
    if not command or not _MNEMEX_CMD_RE.search(command):
        return 0  # not a mnemex command — don't scan the graph on every Bash call
    binding = mnx_binding.resolve()
    if binding is None:
        return 0
    root = Path(binding.graph_root())
    if not root.is_dir():
        return 0
    try:
        import mnx_lock
    except Exception:
        return 0
    stranded: list[tuple[str, dict]] = []
    for team_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("team-")):
        try:
            if mnx_lock.in_progress(str(team_dir)):
                stranded.append((team_dir.name, mnx_lock.recover(str(team_dir))))
        except Exception:
            continue
    if not stranded:
        return 0
    lines = ["Mnemex: a maintenance/write pass left state behind — a gc/write may have crashed "
             "(or one is still running concurrently):"]
    for name, rec in stranded:
        tree = "dirty" if rec.get("dirty") else "clean"
        lines.append(f"  - {name}: pass.plan.json present, working tree {tree} "
                     f"→ recommended: {rec.get('action')}")
    lines.append("If no pass is running, recover before the next write/gc: 'rollback' = "
                 "`git checkout .` in the graph (restore the last good commit); 'replay' = the "
                 "commit landed, just clear the stranded plan. mnx-doctor flags this as invariant 16.")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n".join(lines),
        }
    }))
    return 0


def _session_arg(argv: list[str]) -> str:
    if "--session" in argv:
        i = argv.index("--session")
        if i + 1 < len(argv):
            return argv[i + 1]
    return ""


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        # opt-out / opt-in are agent-run plain commands (argv-driven, no stdin event).
        if cmd == "opt-out":
            return _set_mute(_session_arg(argv), True)
        if cmd == "opt-in":
            return _set_mute(_session_arg(argv), False)
        if cmd == "session-start":
            return session_start()
        if cmd == "stop":
            return stop()
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


if __name__ == "__main__":
    sys.exit(main(sys.argv))
