# 🎚️ 07 — Configuration

All tunables live in `mnemex.config.md` at the repo root — Markdown with a YAML front-matter block, so
it is human-readable *and* machine-parseable. The protocol reads it via `mnx_config.py`. This document
is the schema, the defaults, and the rules around changing values.

> [!TIP]
> 🎛️ **One knob to start.** You really only set `half_life_days`. Patterns get a derived +30% half-life
> automatically, and every other value has a sensible default. Tune the rest only once real usage tells
> you to.

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

# --- Freshness (revalidation horizon; Doc 14) ---
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
volatility: timeless      → never stale (and never auto-dies — Doc 14 §7)
volatility: volatile      → freshness_ttl_days · 0.15         (URLs, versions, prices, on-call names)
volatility: <int days>    → exactly that many days
volatility: default →  pattern:  freshness_ttl_days · (1 + freshness_pattern_bonus)
                       domain:   freshness_ttl_days
```

~99% of atoms carry no `volatility` and inherit the type-derived default (layer B). `mnx-capture` **proposes**
a `volatility` from the atom's content shape and the human confirms/overrides at the promote gate — the author
never has to remember to annotate. At `mnx-init` the tool states the default explicitly, exactly as it does for
the half-life: *"Facts go stale after 30 days unless you tag them; patterns get +30%."* Full model:
[`14-freshness-and-revalidation.md`](14-freshness-and-revalidation.md).

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

Because decay is computed lazily against a *stored* strength, editing `half_life_days` (hence `λ`)
silently re-interprets every stored number — without a guard, a batch of nodes could jump tiers
overnight. The protocol guards this:

1. Any change should bump `config_version`.
2. `.mnemex/config_version` records the version and `λ` **in force at the last compaction**.
3. `mnx-read` compares the two and **warns** if they differ (*“parameters changed; recompaction needed
   before scores are valid”*) — it does not act.
4. The **next consolidation** (the back half of `mnx-promote`) runs a one-time **re-normalization** *before* any tier decision: it recomputes
   every node's stored strength so that each node's **live score is continuous** across the change
   (`score_new(now) == score_old(now)`), then stamps the new version/λ. The same pass recomputes every
   node's `stale_after` if `freshness_ttl_days`/`freshness_pattern_bonus` changed — so a freshness-horizon
   edit, like a half-life edit, takes effect gradually at the next consolidation rather than reinterpreting
   the index in place (Doc 14 §8).

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
[`09-appendix-glossary-acronyms.md`](09-appendix-glossary-acronyms.md).
