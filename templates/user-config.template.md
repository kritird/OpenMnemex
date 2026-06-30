---
# User-level Mnemex default. Lives at ~/.claude/mnemex/config.md (durable; NOT in the plugin dir).
# Used for any project that does not have its own .mnemex.md and when no env override is set.
#
# Set EXACTLY ONE of graph_remote / graph_path (if both are set, graph_path wins + a warning):
graph_remote:        # a git remote — cloned & synced; writes commit + push.  e.g. git@github.com:acme/payments-knowledge.git
graph_path:          # a local folder — used in place, no clone/push.          e.g. ~/knowledge/payments
default_team:        # optional — default routing team
author:              # optional — identity for usage stamps / commits
---

# Mnemex user default

Your fallback knowledge graph. A project's own `.mnemex.md` (and the `MNEMEX_GRAPH_REMOTE` env var)
take precedence over this file. See `docs/10-binding-and-graph-sync.md`.
