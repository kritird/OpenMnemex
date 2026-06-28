---
config_version: 1

# --- Decay (the ONE knob you set) ---
half_life_days: 180
pattern_halflife_bonus: 0.30      # patterns persist ~30% longer than domain facts (derived)

# --- Tiers ---
hot_k: 12
warm_band: 0.25
cold_ttl_days: 120
cold_recall_multiplier: 1.6
strength_max: 1.0

# --- Usage boosts ---
boost:
  contributed: 1.0
  consulted: 0.5
  traversed: 0.0

# --- Budget / scale ---
node_budget: 35

# --- Maintenance ---
compaction_cadence_days: 14
reconcile_cold_on: update         # always | update | never

# --- Death policy ---
purge_dead: false                 # false = tombstone-and-retain (recommended)
---

# Mnemex configuration

This file controls how your knowledge graph remembers and forgets. It is plain Markdown with a YAML
front-matter block, so it is readable by both you and the protocol.

## The one decision that matters

You set **`half_life_days`** — how long an *unused* domain fact takes to lose half its relevance.
Everything else has a sensible default. Patterns (the "how") automatically persist longer than domain
facts (the "what") by `pattern_halflife_bonus` (default +30%); you do not tune two rates.

## When you change a value

Bump `config_version`. The next `mnx-gc` will **re-normalize** stored relevance so nothing jumps tiers
abruptly, and `mnx-read` will warn you if a change is pending. See `docs/07-configuration.md`.

## Quick guidance

- Start with a **long** half-life and **generous** `cold_ttl_days`; tighten once you have observed real
  usage. Nothing should die prematurely while you are still learning the rhythm of your graph.
- `hot_k` is how many nodes you want visible at *zero* extra read cost (chunk-1). 12–20 is reasonable.
- `node_budget` exists to keep reconciliation matching fast on writes; size it by write-path comfort.
- Keep `purge_dead: false` for audit-friendly, recoverable forgetting (tombstones, not deletion).
