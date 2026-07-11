# 🗃️ Data Model and File Schemas

Exact on-disk formats for every file the protocol reads or writes. All front-matter is YAML; all
timestamps are UTC ISO-8601 (`2026-06-28T14:03:00Z`). Templates live in `templates/`.

> [!NOTE]
> 📄 **Node** = pure knowledge (truth) · 🗂️ **Index** = generated navigation + materialized state ·
> 📝 **Registry** = append-only telemetry · 🔗 **Cross-links** = generated boundary edges ·
> 📥 **Staged atom** = local, pre-promotion knowledge unit. The first three never blur into each other.

---

## 1️⃣ Repository layout

```
<knowledge-repo>/
  index.md                 # ORG index: lists teams
  cross-links.md           # GENERATED (per team root in multi-team; org-level lists soft refs only)
  mnemex.config.md         # config (see 07)
  .mnemex/                 # protocol state (not knowledge)
    last_compaction        # ISO ts of last gc per team:  "team-payments=2026-06-20T...Z"
    config_version         # version + λ in force at last compaction
    highwater/             # per-cluster registry high-water marks
      team-payments__settlement
    team.lock              # present only while a mutating op holds the lock
    pass.plan.json         # present only during an in-progress maintenance pass
  team-<name>/
    index.md               # TEAM index: lists domains (child folders)
    registry.md            # usage log for nodes directly in this folder (usually empty here)
    cross-links.md         # GENERATED: edge instances crossing cluster boundaries within this team
    <domain>/
      index.md             # DOMAIN index: HOT / WARM / COLD node sections (the materialized state)
      registry.md          # append-only usage log for this cluster
      <node-id>.md         # a node (domain or pattern)
      ...
```

A **cluster** = a leaf folder containing nodes (e.g. `team-payments/settlement/`).

```mermaid
flowchart TD
    ORG["🏛️ &lt;knowledge-repo&gt;/<br/>index.md · mnemex.config.md · .mnemex/"]
    ORG --> TEAM["👥 team-payments/<br/>index.md · registry.md · cross-links.md"]
    TEAM --> C1["📁 settlement/ <i>(cluster)</i>"]
    TEAM --> C2["📁 rails/ <i>(cluster)</i>"]
    C1 --> N1["📄 iso8583-field124.md"]
    C1 --> N2["📄 ledger-routing.md"]
    C1 --> N3["🧭 pat-settlement-recon.md"]
    C1 --> IX["🗂️ index.md + 📝 registry.md"]
    classDef org fill:#4b2e83,stroke:#a98ce0,color:#fff;
    classDef team fill:#14507a,stroke:#39c,color:#fff;
    classDef cl fill:#0e5a5a,stroke:#3cc,color:#fff;
    classDef nd fill:#0e7a0d,stroke:#0a5,color:#fff;
    class ORG org;
    class TEAM team;
    class C1,C2 cl;
    class N1,N2,N3,IX nd;
```

---

## 2️⃣ Node file

A node is **pure knowledge**. It carries **no** `strength`, `last_update`, `tier`, or `last_used` —
those are materialized in the index. The node's front-matter copies that the *index* needs for
matching (`summary`, `aliases`) are authored here and denormalized into the index by the write/gc
apply step (the validator keeps them in sync — see Invariants & Failure Modes).

```markdown
---
id: iso8583-field124            # stable slug, NEVER changes. The spine.
type: domain                    # domain | pattern
title: ISO 8583 Field 124 — stablecoin routing instructions
summary: One line that lands in the index verbatim (the match + routing surface).
aliases: [field 124, DE124, data element 124, stablecoin routing field]
domain: [settlement]            # may be a LIST: a node can belong to >1 sub-index (routing DAG)
status: active                  # active | dead   (retired-why lives in fields: superseded-by, died)
confidence: high                # high | medium | low
volatility: default             # default | timeless | volatile | <int days> — freshness horizon (Freshness & Revalidation)
trigger: null                   # REQUIRED for type: pattern; null for domain (see below)
mentions:                       # authored links, by NAME. GENERATED from body [[wiki-links]] at promote (Link Reconciliation).
  - { name: ledger-routing,  resolved_id: ledger-routing, type: null }   # live link (untyped default)
  - { name: de124-legacy,    resolved_id: null,           type: null }   # 🔴 red-link: no page yet (latent)
edges:                          # OUTGOING edges. GENERATED MIRROR of resolved `mentions` (Link Reconciliation §8).
  - { to: ledger-routing,    type: null }        # untyped by default; type optional (routes-through, …)
  - { to: pat-settle-recon,  type: governed-by }
references:                     # SOFT, cross-TEAM pointers ONLY. Not integrity-guaranteed.
  - { to: risk-settlement-finality, team: team-risk, note: "related finality model" }
provenance:
  artifact: tap-vic-settlement-spec      # what build produced this
  reviews: [r3, r7]                       # which human review points fed it
  session: 2026-06-01T09:12:00Z
created: 2026-06-01T09:12:00Z
updated: 2026-06-01T09:12:00Z   # meaning-change timestamp (NOT usage)
verified: 2026-06-01T09:12:00Z  # last confirmed-still-true timestamp (NOT usage, NOT meaning-change). Freshness & Revalidation
---

## Summary
<one-paragraph head — read first on a body expansion>

## What            # for domain nodes
<the domain knowledge>

## How / Notes      # for pattern nodes, this is the prescriptive content
<the procedure / the rule / the rationale>

## Provenance
<why this node exists; traceable to the artifact and the specific human reviews>
```

### Freshness fields (`verified`, `volatility`)

`verified` is a **third truth clock**, distinct from `updated`: `updated` moves when the *meaning* changes;
`verified` moves when the atom is *confirmed still true*. Re-confirming an unchanged atom bumps `verified`
only — never `updated`, never strength. `volatility` selects the atom's revalidation horizon
(`default` → derived from type; `timeless` → never stale **and never auto-dies**; `volatile` / `<int days>`
→ short/explicit). The horizon is materialized into the index as `stale_after` (§3). Full model:
[`freshness-and-revalidation.md`](freshness-and-revalidation.md).

### Pattern nodes

For `type: pattern`, **`trigger` is required** — the *when* clause, the condition under which the
pattern applies. Matching of patterns is on `trigger` (structured), not on prose, to prevent sprawl.

```yaml
type: pattern
trigger: "curating or reviewing a settlement specification"
edges:
  - { to: iso8583-field124, type: governs }
```

### Tombstone (dead) node

```yaml
status: dead
supersedes: null
superseded-by: iso8583-field124-v2   # if replaced; else null
died: 2026-09-01T00:00:00Z
# body RETAINED on tombstone (audit + resurrection). id + front-matter kept. Hard-delete only on --purge.
```

---

## 3️⃣ Index file (`index.md`)

The index is **generated**. It is chunked so chunk 1 is enough to route. It carries the **materialized
memory state**.

```markdown
# settlement — domain index
> ISO 8583 messaging and settlement nodes for the payments team.   <!-- chunk 1: route on this -->

## Children                          <!-- chunk 1 continues: sub-folders, if any -->
- (none — this is a leaf cluster)

## Hot                               <!-- chunk 1 tail: top-K by live score; ALWAYS small -->
| id | type | summary | aliases | strength | last_update | stale_after |
|----|------|---------|---------|----------|-------------|-------------|
| iso8583-field124 | domain | ISO 8583 Field 124 — stablecoin routing… | field 124; DE124 | 0.94 | 2026-06-20T… | 2026-07-01T… |

## Warm                              <!-- chunk 2: read only if routed here -->
| id | type | summary | aliases | strength | last_update | stale_after |
| ledger-routing | domain | Ledger routing topology for… | ledger routing | 0.55 | 2026-05-30T… | 2026-06-29T… |

## Cold                              <!-- chunk 3+: deep search / edge-reached only -->
| id | type | summary | aliases | strength | last_update | stale_after | expires |
| legacy-de124-fmt | domain | Pre-2024 Field 124 layout… | old de124 | 0.08 | 2026-02-01T… | — | 2026-08-01T… |
```

Notes:
- `summary` and `aliases` are **denormalized copies** of the node's values — the validator enforces
  `index.summary == node.summary` and `index.aliases == node.aliases`.
- `strength`/`last_update` are the materialized decay state (Architecture).
- `stale_after` is the **precomputed freshness horizon** (Freshness & Revalidation): read flags an atom `stale` when
  `now > stale_after`. It is denormalized from the node like `summary`/`aliases` (validator-enforced), is
  `—`/null for `timeless` and dead (retired) nodes, and is recomputed by consolidation. It is independent
  of `expires` (the Cold-tier death time) — a node can be stale long before it is near death, or fresh yet
  cold.
- Hot = top-K (`hot_k`); the line count of the Hot section is therefore bounded.
- **Chained index.** When the Cold tier outgrows `index_chunk_rows`, the head keeps Hot + Warm + the
  first cold chunk and a `## Continuations` list, and the overflow spills into ordered continuation
  files `index.001.md`, `index.002.md`, … (B-tree-leaf style). One head-read still routes; a deep cold
  search walks the chain. Consumers needing the full node-set merge head + continuations
  (`mnx_index.index_node_ids`). See [`staging-and-promotion.md`](staging-and-promotion.md).

---

## 4️⃣ Registry file (`registry.md`)

Append-only. The write buffer / WAL. One event per line. Human-readable but treated as a log.

```markdown
# registry: team-payments/settlement   (append-only — do not edit by hand)
iso8583-field124    2026-06-20T14:03:00Z    contributed    1.0
ledger-routing      2026-06-20T14:03:01Z    consulted      0.5
iso8583-field124    2026-06-25T09:00:00Z    contributed    1.0
iso8583-field124    2026-06-28T11:00:00Z    revalidated    0.0
__maintenance-due__ 2026-06-27T08:00:00Z    flag           0
```

Columns: `id`, `ts` (UTC ISO-8601), `role`, `weight`. The `__maintenance-due__` sentinel is the
read-skill's overdue flag (Architecture §7). The `revalidated` role (weight `0`) is a **freshness** event, not a
usage boost: read appends it when the model re-confirms a stale atom is still true; consolidation consumes it
to advance the node's `verified` and does **not** fold it into strength (Freshness & Revalidation). Compaction replays lines
**after** the cluster's high-water mark and advances the mark; it does not delete (Architecture §2).

---

## 5️⃣ Cross-links file (`cross-links.md`)

Per **team root**. **Generated/incrementally maintained.** Lists only edge instances that cross
cluster boundaries *within the team*, so cross-cluster structural strength and death-severing stay
cheap without scanning sibling clusters.

```markdown
# cross-links: team-payments   (generated — regenerated by mnx-doctor; delta-updated by write/gc)
| from_id | from_path | type | to_id | to_path |
|---------|-----------|------|-------|---------|
| rails-topology | rails/rails-topology.md | routes-through | iso8583-field124 | settlement/iso8583-field124.md |
```

Each row carries both ids **and** paths, so death-severing can open exactly the referrer nodes without
loading whole clusters. **Cross-*team*** relationships are NOT here — they are soft `references` in the
node front-matter and carry a disclaimer; they never enter structural strength or severing.

---

## 6️⃣ Config file (`mnemex.config.md`)

Markdown with a YAML front-matter block (so it is human-readable *and* machine-parseable). Full schema
and defaults in [`configuration.md`](configuration.md). Sketch:

```markdown
---
config_version: 1
half_life_days: 180            # domain half-life H_domain (the ONE knob)
pattern_halflife_bonus: 0.30   # patterns get +30% half-life (derived, you are informed)
hot_k: 12                      # top-K hot per cluster
warm_band: 0.25                # score floor for warm; below → cold
cold_ttl_days: 120             # grace in cold before death
node_budget: 35                # split a cluster's index past this many nodes
node_body_max_chars: 6000      # soft per-node body budget; over → split into nodes + an edge (never truncate)
index_chunk_rows: 60           # cold rows per index file; over → chain index.NNN.md (B-tree leaf)
boost: { contributed: 1.0, consulted: 0.5, traversed: 0.0 }
cold_recall_multiplier: 1.6    # spaced-repetition over-reward for reviving a cold node
strength_max: 1.0
compaction_cadence_days: 14
reconcile_cold_on: update      # always | update | never  (lazy cold reconciliation)
purge_dead: false              # tombstone-and-retain (true = hard delete)
---
# Mnemex configuration
Human-readable notes about these values…
```

---

## 7️⃣ Staged atom (capture staging tier)

A **staged atom** is a provisional, local, pre-promotion knowledge unit written by `mnx-capture` and
consumed by `mnx-promote`. It lives **outside the graph** (per-author, never pushed) under
`~/.claude/mnemex/staging/<graph-slug>/atoms/<provisional-id>.md`, where the **provisional id** is a
content hash: `stg-` + the first 12 hex of `sha1(type|summary|body|aliases|domain|trigger)`. It must
**never** become a real node id or enter a read stamp; promotion mints a real slug id. Full model:
[`staging-and-promotion.md`](staging-and-promotion.md).

```markdown
---
provisional_id: stg-d3d3ad9d7fbe   # content hash, NEVER enters the graph
type: domain                       # domain | pattern
summary: One line — the match/routing surface.
aliases: [field 124, DE124]
domain: [settlement]               # routing key(s)
score: now                         # now | later  ('not-needed' is dropped, never staged)
urgent: true                       # sharpens the nag; never inline-pushes
volatility: default                # LLM-PROPOSED from content shape; human overrides at the promote gate (Freshness & Revalidation)
mentions:                          # GENERATED from the body's [[wiki-links]] at capture (Link Reconciliation); resolved_id
  - { name: iso8583-field124, resolved_id: null, type: null }   #   ALWAYS null here — promote resolves.
trigger: "reviewing a settlement spec"   # REQUIRED for type: pattern
provenance:                        # self-sufficient to reconcile COLD (transcript gone by promote)
  artifact: tap-vic-settlement-spec
  reviews: [r3, r7]
  rejected: ["post-then-reconcile (orphans legs)"]
  session: 2026-06-29T09:12:00Z
  rationale: "human correction in review r7"
staged_at: 2026-06-29T10:11:21Z
---

The atom's knowledge body. May contain inline [[wiki-links]] to other pages by NAME (Link Reconciliation) — capture
hoists them into `mentions:` and preserves them verbatim; **promote** resolves them and splits an
over-budget body into sibling pages + a link (capture never splits or resolves).
```

### Corpus-atom variant (ingest)

An atom staged by [`mnx-ingest`](corpus-ingestion.md) instead of a live session carries a **source-anchored
provenance** (self-sufficient to reconcile *cold* and to diff on re-run) plus a top-level **`ingest_batch`
label** (which sets `bulk: true`, partitioning it from hand-captures):

```yaml
ingest_batch: ing-2026-07-11-a1b2   # top-level label → sets bulk: true; drained by mnx-promote --bulk
bulk: true
provenance:
  source_repo: github.com/acme/payments-service   # or an absolute local path
  commit_sha: 9f3c1a…                              # the exact ref distilled from
  source_path: settlement/reconcile.md             # file within the corpus
  anchor: "## Cut-off handling"                     # heading | Lx-Ly | sym:Name — also the glean coverage key
  kind: doc                                         # doc | interface | code-doc | config
  rationale: "distilled from settlement design doc"
```

---

## 8️⃣ `.mnemex/` state files

Small, machine-managed, **not knowledge** (safe to `.gitignore` the lock and plan; commit the
high-water/version stamps so state is reproducible across clones).

| File | Purpose |
|---|---|
| `last_compaction` | per-team ISO timestamp of last `gc`. Drives the overdue warning. |
| `config_version` | version + `λ` in force at last compaction. Drives re-normalization. |
| `highwater/<team>__<cluster>` | registry line/timestamp replayed up to. WAL checkpoint. |
| `team.lock` | present only while a mutating op holds the team lock. |
| `pass.plan.json` | Phase-A plan; presence + dirty tree ⇒ crash recovery on next run. |
| `ingest/<source-slug>.json` | ingest manifest: `source_path@commit → {hash, node_ids}`. **Committed** with the graph (like `highwater/`), so any clone knows the corpus was imported to commit X and a re-ingest diffs correctly. Written by `mnx-promote --bulk` on confirmed persist; drives the re-ingest delta. |
