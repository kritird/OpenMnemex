---
description: Set up Mnemex — bind this project (or your user account) to a knowledge-graph repo, or scaffold a brand-new graph. Run this first, or whenever a skill reports "No Mnemex graph configured".
argument-hint: "[--create | --bind | --user] [--team <name>]"
---

Use the **mnx-init** skill to set up Mnemex for the user.

Options: $ARGUMENTS

First run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" resolve` to see whether a graph is
already bound. Then follow the skill: choose a mode — **create** a new graph, **bind** this project to an
existing graph (`<project>/.mnemex.md`), or set a **user** default (`~/.claude/mnemex/config.md`). The
graph can be a **git remote** (`graph_remote`) or a **local folder** (`graph_path`, for authors with no
git repo) — set exactly one. For a **git remote**, run the read-only pre-flight first
(`mnx_binding.py probe-remote --remote <url>`): if it is not reachable, report the categorized
remediation and offer the no-auth local-folder fallback instead of writing a binding that will fail at
sync. Write the binding from the matching template and verify with `mnx_binding.py
sync`. A flag in `$ARGUMENTS` (`--create` / `--bind` / `--user`) preselects the mode; otherwise ask.
Never write graph behavior parameters (half-life, tiers) into a binding — those live only in the graph's
`mnemex.config.md`.
