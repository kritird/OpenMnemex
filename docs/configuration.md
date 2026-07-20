# 🎚️ Configuration

All tunables live in `mnemex.config.md` at the repo root — Markdown with a YAML front-matter block, so
it is human-readable *and* machine-parseable. The protocol reads it via `mnx_config.py`. This document
is the schema, the defaults, and the rules around changing values.

> [!IMPORTANT]
> 🎚️ **Don't hand-edit the YAML.** View and change config through the **`mnx-config`** skill
> (`/mnemex:mnx-config` — no args to display, `<key> <value>` to modify). It explains each knob, validates
> the value, writes it in place, and auto-bumps `config_version` so decay/freshness changes re-normalize
> safely. Raw edits are error-prone and easy to forget the version bump on.

> [!TIP]
> 🎛️ **One knob to start.** You really only set `half_life_days`. Patterns get a derived +30% half-life
> automatically, and every other value has a sensible default. Tune the rest only once real usage tells
> you to.

---

## 📂 Where user-level state lives (the mnemex home)

Everything per-author and per-machine — the user config (`config.md`), the graph discovery registry
(`graphs.md`), graph clones (`graphs/`), capture staging + stamp spill (`staging/`), hook run markers
(`run/`), and the ingest cache (`ingest-cache/`) — lives under one **mnemex home** directory, resolved
by `mnx_common.mnemex_home()` with this precedence (first hit wins):

1. **`$MNEMEX_HOME`** — explicit override; works identically under every agent host.
2. **`$CLAUDE_CONFIG_DIR/mnemex`** — respected when Claude Code runs with a relocated config dir.
3. **`~/.claude/mnemex`** *if it already exists* — back-compat: installs created before the
   multi-agent work keep their state exactly where it is, untouched.
4. **`$XDG_DATA_HOME/mnemex`** (default `~/.local/share/mnemex`) — fresh installs; agent-neutral,
   so state staged under one agent (Claude Code, an MCP client, …) is visible to all the others.

Resolution never creates directories; the location materializes on first write. The one
deliberately separate override is `$MNEMEX_INGEST_CACHE`, which relocates only the ingest clone
cache (it can get large and is safe to delete).

### The local-folder default (guided setup)

A first-time user with no graph should never hit a dead end, so guided setup proposes a **plain local
folder** — no git remote, no credentials, so it always succeeds:

- `mnx_init.suggest_default_graph(cwd)` proposes `<mnemex home>/graphs/<project-name>` (pure
  computation, writes nothing) — a folder named after the current project, so different projects get
  distinct default graphs.
- `mnx_binding.write_user_default(path=…)` writes that choice to `<mnemex home>/config.md` (the user
  default, resolution rung 3), storing an **absolute** path so it resolves identically from any cwd.
  It refuses to clobber an existing user default without `force=True`.
- The installer's `--init-graph` flag and the MCP `init_graph(use_default=true)` tool chain these two
  with `mnx_init.init_graph` to go from nothing to a bound, doctor-clean graph in one step. The
  read-only `init_suggest` tool returns the proposal without writing, for a preview.

A git remote stays fully supported (`graph_remote`) — it is the right choice for sharing a graph across
machines or a team — but it is no longer the default, because auth/network can fail and onboarding must
not.

### Knowing what graphs you have

`<mnemex home>/graphs.md` is an append-only registry of every graph you've created or bound — the
`list_graphs` MCP tool, the `mnx_binding.py list-graphs` CLI, and `mnx-status`'s `known_graphs` field
all read it. Full spec (format, write triggers, the `present` flag): see the "Graph registry &
discovery" section of [`binding-and-graph-sync.md`](binding-and-graph-sync.md).

### The viewer (`openmnemex-serve`) and its knobs

The view-only web viewer ([`viewer.md`](viewer.md)) needs the **`[viewer]` extra**
(`pip install 'openmnemex[viewer]'` — FastAPI + uvicorn; without it the command exits with the
install hint, same pattern as `[mcp]`). It takes three flags: `--port N` (default 8765,
auto-increments when taken), `--no-open` (don't launch the browser), and `--graph PATH` (register
a graph the discovery wouldn't find). Discovery itself has no config file: the viewer reads the
**same `graphs.md` registry** above (one ledger for every surface), offers a bounded rescan of
the mnemex home, and "Open a folder…" registers any path you point it at — registration is the
only thing rescan ever writes.

---

## ⚙️ Schema and defaults

```yaml
config_version: 1                 # integer; bump on any change (drives re-normalization)

# --- Decay (the ONE knob the user sets) ---
half_life_days: 180               # H_domain. Untouched domain node halves its score in 180 days.
pattern_halflife_bonus: 0.30      # patterns persist longer: H_pattern = H_domain·(1+bonus). +30%.
                                  #   Derived internally — you set one number; you are TOLD patterns
                                  #   get the bonus at config time. You never tune two rates.

# --- Tiers ---
hot_k: 12                         # top-K hot nodes per cluster (capacity bound → chunk-1 stays small)
warm_band: 0.25                   # live-score floor for warm; below → cold
cold_ttl_days: 120               # grace in cold before a node becomes a death candidate
cold_recall_multiplier: 1.6       # spaced-repetition over-reward: reviving a COLD node boosts harder
strength_max: 1.0                 # saturation cap (prevents immortal nodes)

# --- Freshness (revalidation horizon; Freshness & Revalidation) ---
freshness_ttl_days: 30            # a domain fact goes STALE this long after it was last VERIFIED.
                                  #   Asked at mnx-init like half_life_days. Independent of decay/tiers.
freshness_pattern_bonus: 0.30    # patterns are more durable → longer horizon (derived, like the half-life bonus)
                                  #   H_fresh_pattern = freshness_ttl_days·(1+bonus). You set one number.
                                  #   Per-node `volatility` overrides this: timeless | volatile | <int days>.

# --- Usage boosts (the stamp weights) ---
boost:
  contributed: 1.0                # materially shaped the artifact
  consulted:   0.5                # informed reasoning, not directly in output
  traversed:   0.0                # routed through; not relied on → unstamped

# --- Budget / scale ---
node_budget: 35                   # split a cluster's index past this many active nodes
                                  #   (logical/index split along `domain:` sub-key; nodes never move)

# --- Maintenance ---
compaction_cadence_days: 14       # mnx-read warns when last gc is older than this
reconcile_cold_on: update         # always | update | never — lazy cold reconciliation:
                                  #   update → scan cold for update-intent candidates (resurrection),
                                  #            lazily for create-intent (alias/domain overlap only).
                                  #   always → scan cold every reconcile (safer, costlier).
                                  #   never  → never scan cold (cheapest, highest duplication risk).

# --- Death policy ---
purge_dead: false                 # false = tombstone-and-retain (default, audit-friendly).
                                  # true  = hard-delete from working tree (git history still retains).

# --- Ingestion (bootstrapping the graph from an existing repo; corpus-ingestion.md) ---
ingest_bulk_soft_atoms: 500       # a bulk (labeled ingest) batch past this WARNS — drained continuously
                                  #   by --bulk promote; it never trips the per-session capture nag (DP8).
ingest_bulk_hard_atoms: 5000      # a bulk batch REFUSES past this — drain it with mnx-promote --bulk first.
ingest_max_atoms_per_run: 2000    # per-run cost ceiling; excess resumes on the next run (--resume).
er_match_threshold: 0.85          # entity-resolution: score ≥ this → same entity (deterministic merge).
er_possible_threshold: 0.60       # [possible, match) → the HITL "⚠ suggested" band (the LLM judge runs
                                  #   ONLY here; below possible → distinct). Fixed thresholds with override.
code_extract: gated               # gated | deep | off — the code value-gate: gated stages only public /
                                  #   documented / config-only symbols (per-subtree overridable at gate #1).
# max_glean_passes: 2             # in USER config (<mnemex home>/config.md), not here — the glean
                                  #   recall loop is shared with episodic capture. Bounds passes; default 2.
```

---

## 🧮 The derived-half-life rule (why one knob)

Domain facts and procedural patterns should not fade at the same rate — a hard-won *how* is more
expensive to relose than a lookup *what*. But asking a user to maintain two decay rates is a burden and
an inconsistency risk. So Mnemex exposes **one** `half_life_days` and derives:

```
λ_domain  = ln(2) / half_life_days
λ_pattern = ln(2) / (half_life_days · (1 + pattern_halflife_bonus))
```

At `mnx-init` / first config, the tool **states this explicitly** to the user: *“Patterns will persist
~30% longer than domain facts; set `pattern_halflife_bonus` to change that.”* The user stays in control
without juggling two numbers.

---

## ⏳ The freshness horizon (a second, orthogonal clock)

Heat (decay/tiers) decides whether to **surface** a fact; freshness decides whether to still **trust** it.
They are independent — a `hot` fact can be `stale`. You set **one** freshness number and three layers resolve
the per-atom horizon (`stale_after = verified + horizon`), in precedence order:

```
volatility: timeless      → never stale (and never auto-dies — Freshness & Revalidation §7)
volatility: volatile      → freshness_ttl_days · 0.15         (URLs, versions, prices, on-call names)
volatility: <int days>    → exactly that many days
volatility: default →  pattern:  freshness_ttl_days · (1 + freshness_pattern_bonus)
                       domain:   freshness_ttl_days
```

~99% of atoms carry no `volatility` and inherit the type-derived default (layer B). `mnx-capture` **proposes**
a `volatility` from the atom's content shape and the human confirms/overrides at the promote gate — the author
never has to remember to annotate. At `mnx-init` the tool states the default explicitly, exactly as it does for
the half-life: *"Facts go stale after 30 days unless you tag them; patterns get +30%."* Full model:
[`freshness-and-revalidation.md`](freshness-and-revalidation.md).

## 🛡️ Changing config safely (version + re-normalization)

> [!WARNING]
> Because decay is computed lazily against a *stored* strength, editing `half_life_days` (hence `λ`)
> silently re-interprets every stored number. Without a guard, a batch of nodes could **flash from warm
> to cold overnight**. The protocol guards this — never hand-edit strengths to compensate.

```mermaid
flowchart LR
    EDIT[✏️ edit half_life_days<br/>bump config_version] --> WARN[🔔 mnx-read warns<br/><i>parameters changed</i>]
    WARN --> NEXT[🚀 next mnx-promote]
    NEXT --> RENORM[🧮 re-normalize<br/>score_new(now) == score_old(now)]
    RENORM --> STAMP[🚩 stamp new version / λ] --> TIER[🎯 then tier decisions]
    classDef warn fill:#a86a12,stroke:#fb3,color:#fff;
    classDef safe fill:#0e7a0d,stroke:#0a5,color:#fff;
    class EDIT,WARN warn;
    class RENORM,STAMP,TIER safe;
```

The protocol guards this:

1. Any change should bump `config_version`.
2. `.mnemex/config_version` records the version and `λ` **in force at the last compaction**.
3. `mnx-read` compares the two and **warns** if they differ (*“parameters changed; recompaction needed
   before scores are valid”*) — it does not act.
4. The **next consolidation** (the back half of `mnx-promote`) runs a one-time **re-normalization** *before* any tier decision: it recomputes
   every node's stored strength so that each node's **live score is continuous** across the change
   (`score_new(now) == score_old(now)`), then stamps the new version/λ. The same pass recomputes every
   node's `stale_after` if `freshness_ttl_days`/`freshness_pattern_bonus` changed — so a freshness-horizon
   edit, like a half-life edit, takes effect gradually at the next consolidation rather than reinterpreting
   the index in place (Freshness & Revalidation §8).

This makes parameter changes safe and gradual rather than abrupt and surprising.

---

## 🎯 Tuning guidance (start conservative, then tighten)

None of these values are knowable up front. Recommended posture:

- **Start with long half-life and generous TTL** (e.g. `half_life_days: 180`, `cold_ttl_days: 120`) so
  nothing dies prematurely while you observe real usage. Tighten later.
- **Set `hot_k` to the number of nodes you want visible at zero extra read cost** — roughly the size of
  a chunk-1 you are happy to always load (12–20 is reasonable).
- **Set `node_budget` to where reconciliation matching starts to feel slow** — the budget exists to keep
  the *match surface* small, so size it by write-path comfort, not retrieval.
- **`reconcile_cold_on: update`** is the recommended default — it pays the cold-scan cost only when it
  is most likely to catch a duplicate or trigger a resurrection.
- **Keep `purge_dead: false`** for any enterprise/audit context; git history makes true deletion
  semi-meaningless anyway, and tombstones preserve supersession lineage.
- **Set `freshness_ttl_days` to how long your facts stay true**, not how long you keep them — it is about
  *validity*, not retention. Start at 30; shorten if your domain moves fast. Tag the two outliers a single
  number handles worst: `volatility: timeless` for definitions/invariants (also exempts them from death),
  `volatility: volatile` for anything that rots fast (endpoints, versions, prices).

Every parameter is also collected in the reference table in
[`appendix-glossary-acronyms.md`](appendix-glossary-acronyms.md).
