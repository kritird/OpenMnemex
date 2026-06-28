# 09 — Appendix: Glossary, Acronyms, Parameters, FAQ

---

## A. Glossary

**Cluster** — a leaf folder that directly contains node files (e.g. `team-payments/settlement/`). The
unit of locking, compaction, and budget.

**Cold tier** — the lowest active memory tier; nodes reachable only by deep search or by an edge from a
live node; subject to TTL death.

**Cross-links file** — a generated, per-team file listing edge instances that cross cluster boundaries
within the team; keeps cross-cluster structural strength and death-severing cheap.

**Decay** — the exponential reduction of a node's relevance with elapsed time since last use; computed
on demand, never swept.

**Denormalization** — storing a copy of a node's `summary`/`aliases` in the index so reconciliation can
match without loading node bodies; kept fresh by a validator invariant.

**Domain node** — a node holding a fact about the business/system (the *what*).

**Edge instance** — a concrete relationship *A → B via type T*, owned by node A. The thing that is
created and severed.

**Edge type / vocabulary** — the controlled set of predicates (`routes-through`, `governs`, …); lives
in the write skill, not in graph data; essentially never deleted.

**Hot tier** — the top-K most-relevant nodes per cluster, listed in the index's chunk-1; zero extra
read hops.

**Index** — a generated `index.md` per folder; routing head + the materialized memory state
(strength/last_update/tier) for the folder's nodes.

**Memory tier** — `hot | warm | cold | dead`; a **logical** label in the index (not a physical
location), corresponding to read-cost.

**Node** — a Markdown file of **pure knowledge** (no decay bookkeeping); the only ground truth.

**Pattern node** — a node holding prescriptive procedural knowledge (the *how*), with a `trigger`
(when it applies); usually mined from human review points.

**Provenance** — the record on a node of the artifact, the human review points, and the session that
produced it.

**Registry** — an append-only per-cluster usage log; the write buffer (memtable/WAL) for relevance.

**Resurrection** — promoting a cold node back up on use, with an over-sized boost (spaced repetition).

**Reverse-edge map** — “who points at X”, built from node edges + cross-links; includes cold and dead
nodes; the basis of structural strength and safe severing.

**Snapshot-then-apply** — the core maintenance principle: decide everything against a frozen view, then
apply; never read state already mutated in the same pass.

**Soft reference** — a cross-**team** pointer that is informational only; not in structural strength,
not severed, carries a no-integrity disclaimer.

**Stamp** — an append to the registry recording a confirmed use `{id, ts, role, weight}`.

**Structural strength** — a node's retention contribution from its in-degree/centrality; the
deterministic counterweight to usage decay that protects hubs.

**Tombstone** — a dead node whose body is cleared but whose id/front-matter is retained for audit
(default death mode; hard delete is opt-in).

**Usage manifest** — the read skill's end-of-task `{id, role, why}` list; the gate that defines what
counts as “used”.

---

## B. Acronyms (full forms)

| Acronym | Expansion |
|---|---|
| **BFS** | Breadth-First Search |
| **CBRN** | (not used here) |
| **DAG** | Directed Acyclic Graph |
| **HNSW** | Hierarchical Navigable Small World (a vector index — *deliberately not used*) |
| **HWM** | High-Water Mark |
| **I/O** | Input/Output |
| **ISO 8583** | International Organization for Standardization standard 8583 (financial messaging) |
| **LFU** | Least-Frequently-Used (cache eviction policy) |
| **LLM** | Large Language Model |
| **LRU** | Least-Recently-Used (cache eviction policy) |
| **LSM** | Log-Structured Merge-tree |
| **MCP** | Model Context Protocol |
| **MLFQ** | Multi-Level Feedback Queue |
| **RAG** | Retrieval-Augmented Generation |
| **SSTable** | Sorted String Table (the durable, materialized tier of an LSM) |
| **TTL** | Time-To-Live |
| **UTC** | Coordinated Universal Time |
| **WAL** | Write-Ahead Log |
| **YAML** | YAML Ain't Markup Language (the front-matter format) |

---

## C. Parameter reference

| Parameter | Default | Meaning | Failure it guards / job |
|---|---|---|---|
| `half_life_days` | 180 | Domain half-life `H`; `λ = ln2/H`. | The one decay knob. |
| `pattern_halflife_bonus` | 0.30 | `H_pattern = H·(1+bonus)`. | Patterns persist longer (derived). |
| `hot_k` | 12 | Top-K hot per cluster. | Bounds chunk-1 size (retrieval budget). |
| `warm_band` | 0.25 | Score floor for warm. | Warm/cold boundary. |
| `cold_ttl_days` | 120 | Grace in cold before death. | Forgetting horizon. |
| `cold_recall_multiplier` | 1.6 | Over-reward for reviving a cold node. | Spaced-repetition durability. |
| `strength_max` | 1.0 | Saturation cap. | F3 immortal nodes. |
| `boost.contributed` | 1.0 | Strong stamp weight. | Usage signal. |
| `boost.consulted` | 0.5 | Medium stamp weight. | Usage signal. |
| `boost.traversed` | 0.0 | Unstamped. | Usage signal. |
| `node_budget` | 35 | Cluster index split threshold. | Reconciliation match-surface bound. |
| `compaction_cadence_days` | 14 | Overdue-warning threshold. | F6 (warn, not act). |
| `reconcile_cold_on` | update | Lazy cold-scan policy. | Duplication vs. write cost. |
| `purge_dead` | false | Tombstone vs. hard delete. | Audit retention. |
| `config_version` | 1 | Bumped on change. | F5 retroactive drift. |

---

## D. FAQ

**Why not just use a vector database / RAG?** Cost and opacity. An embedding pipeline and a vector
index are infrastructure to operate, and their retrieval is not human-readable or git-diffable. Mnemex
routes structurally and keeps everything as files. If structural routing ever proves too brittle at
scale, a *tiny* vector sidecar over only node **headers** (not bodies) is the sanctioned escalation —
but the standard starts without it and most deployments should not need it.

**Why are reads allowed to write at all?** They append one or more lines to an append-only registry —
the usage stamps. They never rewrite a node or an index. This keeps retrieval effectively pure while
still feeding the relevance signal.

**What stops the graph from quietly deleting important knowledge?** The conjunction gate: a node dies
only when its decayed usage is low **and** it is structurally weak (nothing points at it). Hubs are
protected by deterministic structural strength even if rarely hit directly. And death is tombstoning,
recoverable from git.

**Can two teams' graphs reference each other?** Only via **soft** `references` that carry an explicit
no-integrity disclaimer. Hard edges stay within a team to preserve the single-team lock/snapshot model.
Cross-team convergence is a human ritual, aided by `same-as` edges and shared ids.

**What is the one principle to remember?** Snapshot-then-apply. Compute every maintenance decision
against a frozen view of the graph, then apply them together. It dissolves most of the ordering and
concurrency hazards on its own.

---

## E. References (concepts this design draws on)

- Ebbinghaus forgetting curve; spaced-repetition scheduling (memory strength, recall-driven interval
  growth).
- Log-Structured Merge-trees and Write-Ahead-Log checkpointing (registry-as-memtable, compaction,
  high-water marks).
- LFU/LRU cache eviction with lazy time-decayed counters (Redis-style relevance without a sweep).
- Generational garbage collection and Multi-Level Feedback Queues (tiering without re-sorting on every
  access; mark-then-sweep).
- Claude Code plugin system: skills, slash commands, hooks, marketplaces (the distribution surface).
