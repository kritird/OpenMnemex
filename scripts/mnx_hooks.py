"""mnx_hooks.py — Claude Code hook entrypoints (advisory; never mutate knowledge).

Subcommands (argv[1]):
  session-start      : if cwd is a Mnemex repo, print a one-line status
                       (last compaction, overdue?, lock held?).
  session-end        : if knowledge-bearing work happened, PROMPT to run mnx-write.
                       Never auto-writes.
  pre-commit-gate    : if the pending bash command is a git commit in a Mnemex repo,
                       run mnx_doctor.check; block (non-zero exit) on error-level findings.
  post-apply-check   : after a gc/write apply, verify the team lock was released and no
                       pass.plan.json is stranded; surface crash recovery if so.

STATUS: v0.1.0 CONTRACT STUB. See docs/04-skills-commands-hooks.md §6.
Hooks read event JSON on stdin per the Claude Code hooks protocol.
"""
from __future__ import annotations
import sys


def main(argv: list[str]) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main(sys.argv))
