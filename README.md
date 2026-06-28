# Mnemex Protocol

**A self-pruning, human-memory-modeled knowledge context graph for AI agents.**

Mnemex (from *Mnemosyne*, the personification of memory, + the engineering suffix *-ex*) is a
specification **and** a Claude Code plugin for capturing the durable knowledge an agent produces —
the **what** (domain facts) and the **how** (the patterns and review decisions that govern good
work in a domain) — into a plain-Markdown graph that lives in a git repository, organizes itself
into human-like memory tiers (**hot / warm / cold**), and forgets what stops being useful while
protecting what is structurally important.

It is designed to give agents long-term, navigable, *context-budget-aware* memory **without a vector
database, without an embedding pipeline, and without a server.** Routing is structural (folder +
index traversal), decay is lazy (computed, never swept), and every mutation is a reviewable git
commit.

> **Status:** `v0.1.0` — specification draft + plugin scaffold. The deterministic helper scripts in
> `scripts/` are published as **contracts** (full signatures, inputs, outputs, and invariants) and
> are not yet implemented. See [`docs/06-script-contracts.md`](docs/06-script-contracts.md).

---

## Why this exists

Agents are excellent at producing knowledge inside a session and terrible at keeping it. The two
common fixes both have sharp costs:

- **Stuff everything into context** → context bloat, cost, and attention dilution.
- **Embed everything into a vector store** → infrastructure, an embedding pipeline, an index to
  operate, and opaque retrieval you cannot read or diff.

Mnemex takes a third path. Knowledge is **files**. Navigation is the **filesystem plus small index
files** you read in chunks. Relevance is a **number you compute on demand** from a usage log, modeled
on the *Ebbinghaus forgetting curve* and *spaced repetition*: things used often stay visible and
cheap to reach; things unused drift down the tiers and, eventually, die — unless something still
points at them. The result is a knowledge base that behaves like memory rather than a landfill.

The full reasoning, including the design decisions that were considered and rejected, is in
[`docs/01-rationale-and-concepts.md`](docs/01-rationale-and-concepts.md).

---

## The shape in one screen

```
your-knowledge-repo/            ← a normal git repo you point Mnemex at
  index.md                      ← org router (which teams exist)
  cross-links.md                ← GENERATED: inter-cluster edges only (within a team)
  mnemex.config.md              ← your tunable parameters (half-life, budgets, cadence …)
  .mnemex/                      ← protocol state (locks, high-water marks, version stamps)
  team-payments/
    index.md                    ← team router (which domains)  +  HOT / WARM / COLD sections
    registry.md                 ← append-only usage log (the write buffer)
    settlement/
      index.md                  ← domain sub-index (chunked: routing head, then node table)
      registry.md
      iso8583-field124.md       ← a NODE (pure knowledge; no bookkeeping inside)
      visanet-routing.md
      pat-settlement-recon.md   ← a PATTERN node (a "how", with a trigger)
```

Three kinds of file, three jobs (this separation is the core of the design):

| File | Holds | Mutated when |
|---|---|---|
| **Node** (`*.md`) | Pure knowledge: summary, body, edges, provenance. | Only on author / re-author / supersede / death. |
| **Index** (`index.md`) | Derived navigation + materialized memory state (strength, tier). | Only by the maintenance pass (and write-apply). |
| **Registry** (`registry.md`) | Append-only usage stamps. | Appended on confirmed use; truncated only by checkpointed compaction. |

---

## The four operations

Mnemex ships four skills, each fronted by a slash command. They map to the verbs of a memory system.

| Command | Skill | What it does | Mutates? |
|---|---|---|---|
| `/mnemex-protocol:mnx-read` | `mnx-read` | Route → read tiered indexes in chunks → expand only needed nodes → emit a **usage manifest** → append stamps for nodes actually used. | Registry append only (pure w.r.t. knowledge). |
| `/mnemex-protocol:mnx-write` | `mnx-write` | Ingest the **current session** (artifact + human review points) → extract domain/pattern candidates → **reconcile** against the graph → produce a **change plan** (human gate) → apply atomically. | Yes — gated, one commit. |
| `/mnemex-protocol:mnx-gc` | `mnx-gc` | The maintenance pass: compact registries, recompute decay + structural strength on a **frozen snapshot**, re-tier (hot/warm/cold), tombstone dead nodes, sever edges, regenerate navigation — under a team lock, one commit. | Yes — atomic, recoverable. |
| `/mnemex-protocol:mnx-doctor` | `mnx-doctor` | The validator: checks every invariant (edge targets exist, index matches nodes, denormalized copies are fresh, reverse map consistent, no dangling edges) and can self-heal derived files. | Repair mode only. |

A `/mnemex-protocol:mnx-init` command scaffolds a new knowledge repo.

Phase-by-phase breakdowns are in
[`docs/04-skills-commands-hooks.md`](docs/04-skills-commands-hooks.md) and
[`docs/05-maintenance-pass-algorithm.md`](docs/05-maintenance-pass-algorithm.md).

---

## Install

```bash
# In Claude Code, add this repo as a marketplace, then install the plugin:
/plugin marketplace add USERNAME/mnemex-protocol
/plugin install mnemex-protocol@mnemex-marketplace

# Scaffold a knowledge repo (run inside the repo you want to use as your graph):
/mnemex-protocol:mnx-init
```

Requirements: Claude Code, Python 3.9+ (standard library + `PyYAML` only — no other dependencies).

---

## Read the standard

The documents in [`docs/`](docs/) are written to be self-explanatory and read in order. Every acronym
is expanded on first use and collected in the appendix.

1. [`00-overview.md`](docs/00-overview.md) — the thesis and the design goals, in brief.
2. [`01-rationale-and-concepts.md`](docs/01-rationale-and-concepts.md) — every core concept, *why* it
   is shaped the way it is, and the alternatives that were rejected.
3. [`02-architecture.md`](docs/02-architecture.md) — the three-layer model, memory tiers, the
   lazy-decay math, and the unification of *budget = ranking = forgetting*.
4. [`03-data-model-and-schemas.md`](docs/03-data-model-and-schemas.md) — exact file formats for node,
   index, registry, cross-links, and config.
5. [`04-skills-commands-hooks.md`](docs/04-skills-commands-hooks.md) — the four skills, their command
   surfaces, and the hooks that do what skills cannot.
6. [`05-maintenance-pass-algorithm.md`](docs/05-maintenance-pass-algorithm.md) — the
   snapshot-then-apply algorithm in full, with ordering guarantees.
7. [`06-script-contracts.md`](docs/06-script-contracts.md) — deterministic helper contracts
   (signatures, I/O, invariants) that the skills call instead of hand-reasoning.
8. [`07-configuration.md`](docs/07-configuration.md) — the config schema, derived half-life, and
   config-version re-normalization.
9. [`08-invariants-and-failure-modes.md`](docs/08-invariants-and-failure-modes.md) — the validator
   invariant list and the pressure-test findings with their mitigations.
10. [`09-appendix-glossary-acronyms.md`](docs/09-appendix-glossary-acronyms.md) — glossary, acronym
    expansions, parameter reference, FAQ, references.

---

## License

MIT. See [`LICENSE`](LICENSE).
