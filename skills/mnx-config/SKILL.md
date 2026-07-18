---
name: mnx-config
description: View or change a Mnemex Context Graph's behavior configuration (decay half-life, memory tiers, freshness horizon, usage boosts, budgets, death policy) with a guided explanation of each knob. Use this whenever the user wants to see the current config, understand what a setting does, tune how fast the graph forgets or goes stale, or change any value in mnemex.config.md — instead of hand-editing YAML. Runs in DISPLAY mode (explain the current settings) or MODIFY mode (validate, write, and safely version a change).
---

# mnx-config — view and tune graph behavior

Every graph's behavior lives in **one file at the graph root: `mnemex.config.md`** (YAML front-matter +
prose). This skill is the safe, guided front door to that file. It has two modes:

- **Display** — show the current values, defaults, and what each knob means. Read-only.
- **Modify** — change one value at a time: validate it, write it in place (preserving the file's
  comments), and **auto-bump `config_version`** so decay/freshness changes re-normalize safely.

Never hand-edit `mnemex.config.md` or invent your own YAML — always go through `mnx_config.py set`, which
validates the value and manages the version stamp. The config is owned by the **graph repo** and lives
only there — never copy it into a project binding (`.mnemex.md`) or the user config.

## Preflight — locate the graph (always first)
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" status --session <sid>` (the session id
from session-start, if you have one — see mnx-init step 1; honors a mid-session graph switch). If
`resolved` is false → **STOP** and point at `/mnemex:mnx-init`; if `clone_present` is false, run
`mnx_binding.py sync` once. All config operations use the returned **`graph_root`**, never the working
directory.

## Display mode (default when no key is given)
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_config.py" show <graph_root>` (add `--all` to include the
advanced/internal knobs). It returns every item as `{key, group, value, default, overridden, renorm,
help, choices?}` plus a `derived` block (the computed pattern half-life and the decay/freshness λ). Present
it to the user grouped by `group`, and for each item show **value (default if different) — one-line
meaning**, flagging which ones are `overridden` from the default. Lead with the sentence that frames the
whole model:

> You really only need to set **`half_life_days`**. Patterns automatically persist ~30% longer, and every
> other value has a sensible default — tune the rest only once real usage tells you to.

## Modify mode
1. **Explain before you change.** State what the knob does, its current value, and the effect of the new
   value (see the reference below). For a `renorm` knob (decay/freshness), warn that the change is *staged*
   and takes effect gradually — not instantly.
2. **Confirm** the exact key and new value with the user.
3. **Apply:** `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_config.py" set <graph_root> <key> <value>`. It
   validates the type/range, writes the value (comments preserved), and bumps `config_version`. On a bad
   value it fails **without touching the file** and returns a `{error}` explaining the constraint — relay
   that and re-ask; do not retry with a hand-edit.
4. **Report** the returned `old → new`, the `config_version` bump, and the `note`. If `renorm_pending` is
   true, tell the user: *the next `mnx-promote` will re-normalize stored scores so nothing jumps tiers
   abruptly, and `mnx-read` will warn until then.*
5. **Persist** (remote graphs): run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" persist --message "mnx-config: set <key>"`
   so the change is pushed rather than discarded at the next resync. (Skip for a local-folder graph, which
   is written in place.)

One value per `set`. For several changes, call `set` repeatedly (each bumps the version; that is fine —
the re-normalization at the next consolidation is one-time regardless of how many versions passed).

## The knobs (what to explain to the user)

**Decay — how fast the graph forgets** *(these are `renorm`: staged, gradual)*
- `half_life_days` (180) — **the one knob.** Days for an *unused* domain fact to lose half its relevance.
  Bigger = forgets more slowly. Start long (180) so nothing dies before you've watched real usage; tighten later.
- `pattern_halflife_bonus` (0.30) — patterns (the "how") persist this fraction longer than domain facts
  (the "what"). Derived, so you never juggle two decay rates. 0.30 = +30%.

**Tiers — what stays instantly visible vs. archived**
- `hot_k` (12) — top-K hottest nodes kept per cluster in the always-loaded chunk-1. Size it to how much
  you're happy to always load; 12–20 is reasonable.
- `warm_band` (0.25, 0..1) — live-score floor for WARM; below it a node drops to COLD.
- `cold_ttl_days` (120) — grace period in COLD before a node becomes a death candidate. Keep generous early.
- `cold_recall_multiplier` (1.6) — spaced-repetition over-reward: reviving a COLD node boosts harder.
- `strength_max` (1.0) — saturation cap; stops "immortal" nodes that never decay out.

**Freshness — whether a fact is still TRUE (a separate clock from decay)** *(these are `renorm`)*
- `freshness_ttl_days` (30) — days after a fact was last *verified* before it's flagged STALE for re-check.
  A hot fact can still be stale. This is about validity, not retention. Shorten if your domain moves fast.
- `freshness_pattern_bonus` (0.30) — patterns get this fraction longer freshness horizon.
- Mention the per-node escape hatches instead of over-tuning the global: tag `volatility: timeless` on
  eternal facts (never stale, never auto-dies) and `volatility: volatile` on fast-rotting ones (endpoints,
  versions, prices). Those are set per node at the promote gate, not here.

**Usage boosts — how much a use strengthens a node**
- `boost.contributed` (1.0) — node materially shaped the artifact.
- `boost.consulted` (0.5) — node informed reasoning but wasn't in the output.
- `boost.traversed` (0.0) — merely routed through; 0.0 = unstamped.

**Budget & maintenance**
- `node_budget` (35) — active-node count past which a cluster's index is logically split (nodes never
  move). Size it for write-path comfort — it keeps reconciliation's match surface small.
- `compaction_cadence_days` (14) — mnx-read warns when the last consolidation is older than this.
- `reconcile_cold_on` (update) — lazy cold reconciliation: `update` (recommended) scans cold only on
  update-intent; `always` scans every time (safer, costlier); `never` is cheapest but risks duplication.

**Death policy**
- `purge_dead` (false) — `false` (recommended) tombstones-and-retains dead nodes (audit-friendly, keeps
  supersession lineage); `true` hard-deletes from the working tree (git history still retains them).

Advanced/internal knobs (`freshness_volatile_factor`, `struct_scale`, `node_body_max_chars`,
`index_chunk_rows`, `tier_files`) appear only under `show --all`; leave them at defaults unless the user
asks specifically.

## Why config changes are safe (the version guard)
Decay is computed lazily against a *stored* strength, so editing `half_life_days` silently re-interprets
every stored number — without a guard a batch of nodes could flash warm→cold overnight. `set` bumps
`config_version`; `mnx-read` compares it against the last-compaction stamp and **warns**; the next
`mnx-promote` runs a one-time **re-normalization** so each node's live score is continuous across the
change (`score_new(now) == score_old(now)`) before any tier decision, then re-stamps. So decay/freshness
edits take effect *gradually*, never abruptly. Never hand-edit stored strengths to compensate.

## Never
- Never hand-edit `mnemex.config.md` YAML directly — always use `mnx_config.py set` (it validates + versions).
- Never write decay/tier parameters into a project binding (`.mnemex.md`) or the user config — they belong to the graph.
- Never change more than what the user confirmed; explain `renorm` staging before a decay/freshness edit.
