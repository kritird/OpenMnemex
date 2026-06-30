```
███╗   ███╗  ███╗   ██╗  ███████╗  ███╗   ███╗  ███████╗  ██╗  ██╗
████╗ ████║  ████╗  ██║  ██╔════╝  ████╗ ████║  ██╔════╝  ╚██╗██╔╝
██╔████╔██║  ██╔██╗ ██║  █████╗    ██╔████╔██║  █████╗     ╚███╔╝ 
██║╚██╔╝██║  ██║╚██╗██║  ██╔══╝    ██║╚██╔╝██║  ██╔══╝     ██╔██╗ 
██║ ╚═╝ ██║  ██║ ╚████║  ███████╗  ██║ ╚═╝ ██║  ███████╗  ██╔╝ ██╗
╚═╝     ╚═╝  ╚═╝  ╚═══╝  ╚══════╝  ╚═╝     ╚═╝  ╚══════╝  ╚═╝  ╚═╝
```

<p align="center"><b>Long-term agent memory for LLMs — a self-pruning, human-memory-modeled knowledge context graph.</b></p>

<p align="center">
  <img alt="status" src="https://img.shields.io/badge/status-v0.1.0-success">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white">
  <img alt="claude code" src="https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2">
  <img alt="storage" src="https://img.shields.io/badge/storage-Markdown%20%2B%20git-181717?logo=git&logoColor=white">
  <img alt="vectors" src="https://img.shields.io/badge/vectors-none-critical">
</p>

> 🧠 Persistent, navigable, context-budget-aware **agent memory** with no vector database, no embedding
> pipeline, and no server. Just Markdown in a git repo. Works as a Claude Code plugin.

Mnemex (from *Mnemosyne*, the personification of memory, + the engineering suffix *-ex*) is a
specification **and** a Claude Code plugin for capturing the durable knowledge an agent produces —
the **what** (domain facts) and the **how** (the patterns and review decisions that govern good
work in a domain) — into a plain-Markdown graph that lives in a git repository, organizes itself
into human-like memory tiers (**🔥 hot / 🌤️ warm / ❄️ cold**), and forgets what stops being useful while
protecting what is structurally important.

It is designed to give agents long-term, navigable, *context-budget-aware* memory **without a vector
database, without an embedding pipeline, and without a server.** Routing is structural (folder +
index traversal), decay is lazy (computed, never swept), and every mutation is a reviewable git
commit.

> [!NOTE]
> **Status:** `v0.1.0` — specification + working plugin. The deterministic helper scripts in
> `scripts/` are **implemented** against their published contracts (full signatures, inputs, outputs,
> and invariants) covering binding, decay, compaction, index regeneration, locking, and the validator.
> The repository does not include an automated test suite. See [`docs/06-script-contracts.md`](docs/06-script-contracts.md).

---

## 💡 Why this exists

Agents are excellent at producing knowledge inside a session and terrible at keeping it. The two
common fixes both have sharp costs:

| ❌ Common fix | 💸 The cost it carries |
|---|---|
| **Stuff everything into context** | Context bloat, cost, and attention dilution. |
| **Embed everything into a vector store** | Infrastructure, an embedding pipeline, an index to operate, and opaque retrieval you cannot read or diff. |

Mnemex takes a **third path**. Knowledge is **files**. Navigation is the **filesystem plus small index
files** you read in chunks. Relevance is a **number you compute on demand** from a usage log, modeled
on the *Ebbinghaus forgetting curve* and *spaced repetition*: things used often stay visible and
cheap to reach; things unused drift down the tiers and, eventually, die — unless something still
points at them. The result is a knowledge base that behaves like memory rather than a landfill.

```mermaid
flowchart LR
    A([🤖 Agent builds<br/>something]) --> B{How to keep<br/>the knowledge?}
    B -->|"Stuff context"| C[💸 Bloat + cost]
    B -->|"Vector store"| D[🛠️ Infra + opacity]
    B -->|"Mnemex"| E[📄 Markdown graph<br/>in git]
    E --> F[🔥 Hot · 🌤️ Warm · ❄️ Cold<br/>self-tiering memory]
    F --> G([🔮 Future agent<br/>navigates cheaply])
    classDef good fill:#0e7a0d,stroke:#0a5,color:#fff;
    classDef bad fill:#7a1f1f,stroke:#a33,color:#fff;
    class C,D bad;
    class E,F,G good;
```

The full reasoning behind each design choice is in
[`docs/01-rationale-and-concepts.md`](docs/01-rationale-and-concepts.md).

---

## 🗺️ The shape in one screen

```
your-knowledge-repo/            ← a normal git repo you point Mnemex at
  index.md                      ← org router (which teams exist)
  mnemex.config.md              ← your tunable parameters (half-life, budgets, cadence …)
  .mnemex/                      ← protocol state (locks, high-water marks, version stamps)
  team-payments/
    index.md                    ← team router (which domains)  +  HOT / WARM / COLD sections
    registry.md                 ← append-only usage log (the write buffer)
    cross-links.md              ← GENERATED: inter-cluster edges within this team
    settlement/
      index.md                  ← domain sub-index (chunked: routing head, then node table)
      registry.md
      iso8583-field124.md       ← a NODE (pure knowledge; no bookkeeping inside)
      ledger-routing.md
      pat-settlement-recon.md   ← a PATTERN node (a "how", with a trigger)
```

### How the three file kinds relate

```mermaid
flowchart TD
    subgraph TRUTH["🟢 TRUTH — authored by the LLM"]
        N1[📄 Node<br/>iso8583-field124]
        N2[📄 Node<br/>ledger-routing]
        P1[🧭 Pattern<br/>pat-settlement-recon]
    end
    subgraph DERIVED["🔵 DERIVED — generated by code"]
        IDX[(🗂️ index.md<br/>HOT / WARM / COLD)]
        XL[(🔗 cross-links.md)]
    end
    subgraph TELEMETRY["🟠 TELEMETRY — append-only"]
        REG[(📝 registry.md<br/>usage stamps)]
    end
    N1 -- routes-through --> N2
    P1 -- governs --> N1
    N1 & N2 & P1 -. regenerates .-> IDX
    N1 & N2 & P1 -. boundary edges .-> XL
    REG -. folded at gc .-> IDX
    classDef truth fill:#0e7a0d,stroke:#0a5,color:#fff;
    classDef derived fill:#14507a,stroke:#39c,color:#fff;
    classDef tel fill:#7a4a0d,stroke:#d90,color:#fff;
    class N1,N2,P1 truth;
    class IDX,XL derived;
    class REG tel;
```

Three kinds of file, three jobs (this separation is the core of the design):

| File | Holds | Mutated when |
|---|---|---|
| 📄 **Node** (`*.md`) | Pure knowledge: summary, body, edges, provenance. | Only on author / re-author / supersede / death. |
| 🗂️ **Index** (`index.md`) | Derived navigation + materialized memory state (strength, tier). | Only by the maintenance pass (and write-apply). |
| 📝 **Registry** (`registry.md`) | Append-only usage stamps. | Appended on confirmed use; truncated only by checkpointed compaction. |

---

## ⚙️ The four operations

Four of Mnemex's skills are memory operations, each fronted by a slash command. They map to the verbs of
a memory system (two more skills, `mnx-init` and `mnx-status`, handle setup and status — see below).
Knowledge writing is split **capture / promote** — the `git commit` vs `git push`/PR of memory.

| 🎛️ Command | Skill | What it does | Mutates? |
|---|---|---|---|
| 🔍 `/mnemex:mnx-read` | `mnx-read` | Route → read tiered indexes in chunks → **overlay** local staged atoms → expand only needed nodes → emit a **usage manifest** → append stamps for nodes actually used. | Registry append only (pure w.r.t. knowledge). |
| ✍️ `/mnemex:mnx-capture` | `mnx-capture` | Capture the **current session** (artifact + human review points) → extract atoms → **score** each `now/later/not-needed` → **stage** locally with self-sufficient provenance. Cheap, local, no lock. Also **curates** staging: `--drop <id>` / `--discard-all` un-stage (review via `mnx-status`) — the local un-stage and the hard-cap escape valve. | No — writes only the local staging tier. |
| 🚀 `/mnemex:mnx-promote` | `mnx-promote` | The deliberate merge: flush stamps → **reconcile + merge** staged atoms (clean-context sub-agent, HITL on contradictions) → **consolidate** the post-merge graph (decay/re-tier/death/edge-hygiene/budget) → doctor → push → clear staging. If a push fails after commit, `--retry-push` lands the existing commit (never re-merges). | Yes — gated, atomic, one commit. |
| 🩺 `/mnemex:mnx-doctor` | `mnx-doctor` | The validator: checks every invariant (edge targets exist, index matches nodes, denormalized copies are fresh, reverse map consistent, no dangling edges) and can self-heal derived files. | Repair mode only. |

The maintenance pass (`mnx-consolidate`) is **internal** — the back half of `mnx-promote`, with no
standalone slash command.

```mermaid
flowchart LR
    R[🔍 mnx-read<br/><i>recall</i>] -.->|append stamps| REG[(📝 registry)]
    C[✍️ mnx-capture<br/><i>stage locally</i>] -->|atoms| STG[(📥 staging tier)]
    STG -->|reconcile + merge| P[🚀 mnx-promote<br/><i>commit + push</i>]
    REG -->|flush| P
    P -->|consolidate + commit| G[(🗃️ shared graph)]
    G -->|validate / heal| D[🩺 mnx-doctor]
    D --> G
    classDef op fill:#4b2e83,stroke:#a98ce0,color:#fff;
    classDef store fill:#14507a,stroke:#39c,color:#fff;
    class R,C,P,D op;
    class REG,STG,G store;
```

Two further skills round out the surface. `/mnemex:mnx-init` is the setup/preflight: it **binds** a
project (or your user account) to a graph repo — creating and scaffolding a new graph, or pointing at an
existing one — and is what every other command resolves first. For a git-remote graph it runs a
read-only reachability/auth pre-flight before binding and, on failure, offers a no-auth local-folder
fallback. `/mnemex:mnx-status` is a read-only at-a-glance status: what graph is bound, its kind,
node/tier counts per team, pending usage stamps, last gc, and a health summary. See
[`docs/10-binding-and-graph-sync.md`](docs/10-binding-and-graph-sync.md).

Phase-by-phase breakdowns are in
[`docs/04-skills-commands-hooks.md`](docs/04-skills-commands-hooks.md) and
[`docs/05-maintenance-pass-algorithm.md`](docs/05-maintenance-pass-algorithm.md).

---

## 📦 Install

```bash
# 1. Install the one runtime dependency (Python standard library covers everything else):
pip install pyyaml

# 2. In Claude Code, add this repo as a marketplace, then install the plugin:
/plugin marketplace add kritird/Mnemex-Context-Graph
/plugin install mnemex@mnemex-marketplace

# 3. Scaffold or bind a knowledge repo (the binding step every other command resolves):
/mnemex:mnx-init
```

> [!TIP]
> Requirements: Claude Code and Python 3.9+. The only third-party Python package is
> [`PyYAML`](https://pypi.org/project/PyYAML/) (`pip install pyyaml`); everything else is the
> standard library. If `PyYAML` is missing, the Mnemex commands report it and tell you to install it
> rather than failing cryptically.

A complete walkthrough — from install to daily usage, with the hooks that fire automatically — is in
[`docs/12-user-journey.md`](docs/12-user-journey.md). 🧭

---

## 📚 Read the standard

The documents in [`docs/`](docs/) are written to be self-explanatory and read in order. Every acronym
is expanded on first use and collected in the appendix.

| # | Document | What it covers |
|---|---|---|
| 0️⃣ | [`00-overview.md`](docs/00-overview.md) | The thesis and the design goals, in brief. |
| 1️⃣ | [`01-rationale-and-concepts.md`](docs/01-rationale-and-concepts.md) | Every core concept and *why* it is shaped that way. |
| 2️⃣ | [`02-architecture.md`](docs/02-architecture.md) | The three-layer model, memory tiers, lazy-decay math, and *budget = ranking = forgetting*. |
| 3️⃣ | [`03-data-model-and-schemas.md`](docs/03-data-model-and-schemas.md) | Exact file formats for node, index, registry, cross-links, config, and staged atom. |
| 4️⃣ | [`04-skills-commands-hooks.md`](docs/04-skills-commands-hooks.md) | The skills, their command surfaces, and the hooks that do what skills cannot. |
| 5️⃣ | [`05-maintenance-pass-algorithm.md`](docs/05-maintenance-pass-algorithm.md) | The snapshot-then-apply algorithm in full, with ordering guarantees. |
| 6️⃣ | [`06-script-contracts.md`](docs/06-script-contracts.md) | Deterministic helper contracts (signatures, I/O, invariants). |
| 7️⃣ | [`07-configuration.md`](docs/07-configuration.md) | The config schema, derived half-life, and config-version re-normalization. |
| 8️⃣ | [`08-invariants-and-failure-modes.md`](docs/08-invariants-and-failure-modes.md) | The validator invariant list and the failure-mode register with mitigations. |
| 9️⃣ | [`09-appendix-glossary-acronyms.md`](docs/09-appendix-glossary-acronyms.md) | Glossary, acronym expansions, parameter reference, FAQ, references. |
| 🔟 | [`10-binding-and-graph-sync.md`](docs/10-binding-and-graph-sync.md) | How an author in any repo binds to a separate knowledge-graph repo. |
| 1️⃣1️⃣ | [`11-staging-and-promotion.md`](docs/11-staging-and-promotion.md) | The **capture / promote** split: staging tier, atom schema, budgets, read overlay, atomic promote. |
| 1️⃣2️⃣ | [`12-user-journey.md`](docs/12-user-journey.md) | 🧭 End-to-end journey: install → bind → daily read/capture/promote, with auto-hook touchpoints. |

---

## 📄 License

MIT. See [`LICENSE`](LICENSE).
