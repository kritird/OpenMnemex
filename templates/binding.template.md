---
# Project-level Mnemex binding. Lives at <project-repo>/.mnemex.md.
# Tells the plugin which knowledge graph THIS project reads from and writes to.
# Overrides the user-level default (~/.claude/mnemex/config.md).
#
# Set EXACTLY ONE of graph_remote / graph_path (if both are set, graph_path wins + a warning):
graph_remote:        # a git remote — cloned & synced; writes commit + push.  e.g. git@github.com:acme/payments-knowledge.git
graph_path:          # a local folder — used in place, no clone/push.          e.g. ~/knowledge/payments
default_team:        # optional — default routing team for this project
author:              # optional — identity for usage stamps / commits
---

# Mnemex binding

This project's work is captured into the knowledge graph at `graph_remote` (a git repo) or `graph_path`
(a local folder). The graph itself — including its decay/tier behavior (`mnemex.config.md`) — lives in
that separate location, not here.

A **local folder** that is itself a git repo gets a local commit per change; a plain (non-git) folder
gets an append-only audit trail in its `.mnemex/history.log`. A **git remote** commits and pushes.

Commit this file to share the binding with the team, or add `.mnemex.md` to `.gitignore` to keep it
personal. See `docs/10-binding-and-graph-sync.md`.
