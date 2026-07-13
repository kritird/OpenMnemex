# 🛡️ Invariants and Failure Modes

Two halves. First, the **invariants** `mnx-doctor` enforces — the contract that keeps an LLM-authored
graph trustworthy. Second, the **failure-mode register** — the failure modes the design must defend
against, each with its mitigation and where in the standard it is handled. A correct implementation must
satisfy every invariant and defend every failure mode marked *hardened*; the two *soft* items are
documented limits, not defects.

---

## 🅰️ Part A — Validator invariants (`mnx-doctor`)

> [!NOTE]
> **Severity legend** — 🔴 **E** = error (graph is corrupt; block commits) · 🟡 **W** = warning (drift;
> auto-fixable via `--fix`) · 🔵 **I** = info (advisory).

### 🔗 Referential integrity
1. **(E) Edge targets exist.** Every `to` in every node's `edges` resolves to an existing node id.
2. **(E) No dangling edges to tombstoned/removed nodes.** After any death, no live edge points at the
   dead id unless repointed to its `superseded-by`.
3. **(E) Reverse map consistency.** The built reverse map covers all tiers **and** tombstones; for every
   edge A→B, A appears in `referrers(B)`.
4. **(E) Cross-links completeness + path accuracy.** Every boundary edge appears in `cross-links.md`
   with correct `from_path`/`to_path`; no stale rows.
5. **(I) Soft references flagged.** Cross-team `references` are present-but-unverified; report dangling
   ones without failing (they carry the no-integrity disclaimer).

### 📋 Schema
6. **(E) Front-matter valid.** `id` valid slug; `type ∈ {domain,pattern}`; `status` valid; `pattern`
   nodes have a non-null `trigger`; `volatility ∈ {default, timeless, volatile}` or a positive integer;
   timestamps are UTC ISO-8601.
7. **(E) Id stability.** A node's `id` matches its historical id (ids never change); detect a changed id
   as corruption.

### 🔄 Derived-state freshness
8. **(E) Index ↔ folder node-set match.** The index's node-set equals the folder's actual nodes.
9. **(E) Denormalization fresh.** `index.summary == node.summary` and `index.aliases == node.aliases`
   for every node. *(This is the invariant that justifies storing `summary`/`aliases` copies in the
   index for match-without-body-load.)*
10. **(W) Materialized state present.** Every active node has `strength`/`last_update` in the index.

### ⏳ Freshness (Freshness & Revalidation)
9b. **(E) `verified` monotonic + ordered.** A node's `verified` never regresses across revisions, and
    `created ≤ verified` and `created ≤ updated`.
9c. **(E) `stale_after` denormalization.** For every active node, `index.stale_after ==
    resolve_horizon(node)` — the same denormalization guarantee as invariant 9. It is null iff
    `volatility: timeless` **or** `status == dead` (retired).
9d. **(E) Timeless never auto-tombstoned.** No consolidation pass marks a `volatility: timeless` node as a
    death candidate; a timeless node reaches `dead` only via an explicit human SUPERSEDE.

### 🌡️ Tier / budget
11. **(E) Hot bound.** Each cluster's `hot` section length ≤ `hot_k`.
12. **(W) Budget.** No cluster index exceeds `node_budget` active nodes (else: split or escalate).
13. **(I) Orphan flag.** Nodes with zero incoming edges (local + cross) are flagged (candidates the
    conjunction gate may eventually demote). Reported as **one aggregate Info per cluster** — count +
    first ids — never one finding per node (G13: a fresh bulk import produced 103 orphan Infos that
    drowned every other finding).

### 📡 Telemetry / state
14. **(E) High-water monotonic.** Each cluster's HWM only advances; registry has no lines below a
    truncated mark.
15. **(W) Config drift.** `config_version`/`λ` in `.mnemex/` matches `mnemex.config.md`, or a
    re-normalization is pending.
16. **(E) No stranded pass.** No `pass.plan.json` present without an active lock (else: crash recovery).

### 🕸️ Derivability & mesh
17. **(E) Derivability.** Every derived file (`index*`, tier files, `cross-links.md`, `phonebook*`, org
    `index.md`) regenerates from truth (nodes + registry). This is the invariant that makes "throw away
    and recompute" always safe, and it requires the `mnx-regen` merge driver to be registered in a git
    graph (else derived-file conflicts are 3-way-merged instead of regenerated).
18. **(W) Phonebook completeness + path accuracy.** Every active node appears once in its team phonebook
    with correct `cluster_path`/`tier`/`status`; no stale or dead rows.
19. **(I) Unresolved mentions (red links).** Every name-mention either resolves to an id or is flagged as
    a red link (demand for an unwritten node).
20. **(I) Org-directory completeness.** Every team with nodes appears in the org directory (root
    `index.md`) with its domains.
21. **(W) Mesh mirror consistency.** For every node, each resolved `mentions[].resolved_id` appears in
    that node's `edges` (the front-matter `edges:` list is a generated mirror of the resolved
    `[[wiki-links]]` — Link Reconciliation §8/§10); no resolved mention is missing its mirrored edge.
22. **(W) Mirror ⊆ body.** For every node, each `mentions[].name` (and thus each mirrored `edge`) traces
    back to a `[[wiki-link]]` still present in the body. An entry with no matching body link is a
    **phantom** left by an edit that removed/renamed the link — the mirror is generated from the body
    (§8), so it must never carry links the body dropped. Cleared by re-running link reconciliation
    (`mnx_mesh`), which re-derives the mirror; inv-21 alone misses this (a phantom sits in both
    `mentions` and `edges`, so `mentions ⊆ edges` still holds).

`--fix` resolves all **W** items by regenerating derived files from the nodes (and registers the
`mnx-regen` merge driver); **E** items involving truth (1, 2, 6, 7) require human/skill attention because
they indicate node-level corruption, not derived drift.

---

## 🅱️ Part B — Failure-mode register

Each entry: the break, its mitigation, and the doc that owns the fix.

### ✅ Hardened (must be defended)

| # | Failure | Mitigation | Owned by |
|---|---|---|---|
| F1 | **Ordering corruption** — re-tiering reads structural strength that the same pass then mutates ⇒ non-deterministic outcome. | **Snapshot-then-apply**: all decisions against a frozen view; struct measured once. | Maintenance Pass Algorithm |
| F2 | **Compaction race** — a read appends a stamp during replay-then-truncate ⇒ lost stamp. | **WAL high-water checkpoint**, never truncate. | Architecture §2 |
| F3 | **Immortal nodes** — unbounded `strength += boost` ⇒ a node decay can never demote. | **Saturating add** (`strength_max`) / diminishing boost. | Architecture §1 |
| F4 | **Clock-skew decay inversion** — negative Δt across machines/timezones turns decay into growth. | **UTC ISO-8601 via one helper** + **clamp Δt ≥ 0**. | Architecture §1, Script Contracts |
| F5 | **Retroactive config drift** — editing half-life silently re-interprets stored strengths ⇒ mass flash-cold. | **config_version stamp** + one-time **re-normalization** before tier decisions. | Architecture §8, Configuration |
| F6 | **Read-triggers-write** — read performs “overdue” compaction ⇒ dirty tree, races, mutation on read path. | **Detect-and-warn only**; never compact in `mnx-read`; at most an append-only marker. | Architecture §7, Skills, Commands & Hooks |
| F7 | **Budget re-overflow** — sweeping cold still leaves a cluster over budget on one sub-key. | **Split along declared sub-key**; if a single sub-key exceeds budget, **escalate to human** (never auto-invent structure). | Maintenance Pass Algorithm |
| F8 | **Orphan cascade** — demoting/killing a node orphans a live node it was the sole inbound for. | **Sole-referrer reluctance** + **conjunction gate** (low usage AND structurally weak), checked on snapshot. | Maintenance Pass Algorithm, Rationale & Concepts §9 |
| F9 | **Dangling/dead edges** — tombstoned node leaves edges pointing at it; cold node missed by a partial reverse map. | **Reverse map includes cold+dead**; **transactional edge-severing** at death. | Maintenance Pass Algorithm, Script Contracts |
| F10 | **Half-applied pass** — model times out mid-sweep. | **Plan file + single end-commit + crash-recovery** (`git checkout` to last good commit). | Architecture §10, Maintenance Pass Algorithm |
| F11 | **Cross-cluster severing race** — two `gc`s rewrite a shared referrer concurrently. | **Team-root lock**; parallel mark, **serial sweep**. | Architecture §9 |
| F12 | **Cross-cluster structural blindness** — per-cluster struct misses cross-cluster in-degree ⇒ a hub looks weak and dies. | **`cross-links.md` in the snapshot**; struct = local + cross in-degree. | Architecture §5, Data Model & Schemas §5 |
| F13 | **Denormalization staleness** — re-authoring a node's summary leaves a stale index copy ⇒ matching on wrong text. | **Invariant 9** enforced by `mnx-doctor`; regenerate on write/gc apply. | Part A, Script Contracts |
| F14 | **Usage starvation** — model under-reports the manifest ⇒ used nodes decay and wrongly go cold (silent knowledge loss). | **Mandatory disposition on every body-load**; structural strength as deterministic ballast. | Rationale & Concepts §6, §9 |
| F15 | **Pattern sprawl** — the same “how” authored three ways. | **`trigger` field**; match patterns on trigger, merge near-dupes at the plan gate. | Rationale & Concepts §2 |
| F16 | **Plugin-state contamination** — a process writes state (config, markers, locks, staging) into the plugin's own directory/git ⇒ state lost on reinstall/upgrade, dirty tree in the plugin checkout, breaks on a read-only install. | **State isolation**: graph root resolved explicitly (never cwd, never plugin path); all state in the graph repo, `~/.claude/mnemex/`, or the project's `.mnemex.md`; the plugin dir is read-only. | Architecture §12 |
| F17 | **Stale-but-trusted (truth decay)** — a frequently-read fact stays hot forever while its content silently goes out of date; heat *masks* staleness, so the model confidently serves outdated knowledge. | **Freshness axis**: a separate `verified` clock + precomputed `stale_after`; `mnx-read` emits a **refresh cue** on any stale atom (independent of heat); re-confirmation advances `verified` via a weight-0 `revalidated` stamp. | Freshness & Revalidation, Skills, Commands & Hooks |
| F18 | **Foundational-fact death** — an eternal truth (definition/invariant) decays in heat from disuse and gets tombstoned by the cold-TTL gate. | **`volatility: timeless`** pins the node against automatic death (exempt from the conjunction gate); it can leave only by explicit SUPERSEDE. | Freshness & Revalidation §7, Maintenance Pass Algorithm |

### 🏗️ Ingest invariants (corpus bootstrap — DP1–DP8)

Bootstrapping the graph from an existing repo ([`corpus-ingestion.md`](corpus-ingestion.md)) inherits every
invariant above and adds its own load-bearing set.

| DP | Invariant | Why it holds |
|---|---|---|
| DP1 | **Single writer** — ingest never writes the graph; it only stages. | `mnx-ingest` stages only; `mnx-promote --bulk` is the sole writer (e2e asserts zero graph writes during ingest). |
| DP2 | **Distill, never transcribe** — no file body copied wholesale; zero atoms from a file is valid. | Extraction is LLM judgment gated by the code value-gate; the deterministic walk only chunks + hashes. |
| DP3 | **Read-only source** — a remote is cloned to a read-only cache, a local path read in place; secrets are never read. | `mnx_ingest` never mutates the source; the secret guard counts but never opens `.env`/`*.pem`/`*_rsa`/`credentials*`. |
| DP4 | **Idempotent re-ingest** — a re-run never blindly re-creates; a deleted file never auto-tombstones. | manifest delta → content-hash idempotency → ER/reconcile dedup; a deleted file surfaces as an **orphan candidate** (human decides). |
| DP5 | **One entity → one node** — intra-batch duplicates collapse *before* staging. | `mnx_er` clusters + COLLAPSEs; redundancy becomes provenance + unioned aliases, never duplicate nodes. |
| DP6 | **Exact resolves, fuzzy proposes** — an exact catalog/phonebook match links deterministically; a fuzzy association is `⚠ suggested`. | A wrong link is a false edge; the ER `possible` band + simindex near-matches are HITL at gate #2, never auto-written. |
| DP7 | **No structure from global clustering** — community detection may only *propose* a folder map at gate #1. | Path-based routing is the default; Leiden never mints structure and never runs at read time (guards S2). |
| DP8 | **Bulk isolation** — ingest atoms are label-partitioned from hand-captures. | The `ingest_batch` label + its own bulk cap; `clear --ingest-batch` drains only that batch, never session atoms. |

**Orphan-candidate flow.** A source file deleted since the last ingest is *not* a death signal: the manifest
delta surfaces its `node_ids` as orphan candidates in the report, and the human decides (SUPERSEDE, keep, or
tombstone). Deletion of a doc ≠ death of the knowledge.

### 🌫️ Soft (documented limits, not defects)

| # | Limit | Why it can't be fully hardened | Containment |
|---|---|---|---|
| S1 | **Self-reported usage quality** | No deterministic backstop for “did the model *really* use this.” | Graded manifest + justification rule; structural strength carries weight that usage cannot erode. |
| S2 | **Cross-cluster / cross-team duplication** | Reconciliation is cluster-local by design (the efficiency win); the same concept can be authored in two clusters. Going global re-introduces the search infra we reject. | Stable ids + aliases + `same-as` edges enable a **periodic human convergence ritual**; cross-team links are explicitly **soft** with a disclaimer. |

---

## 🔎 How the soft limits are surfaced to users

Honesty is part of the standard. `mnx-doctor` reports S2 candidates (info-level: “possible duplicate of
`<id>` in `<other-cluster>` by alias overlap”) so the human convergence ritual has a worklist, and the
cross-team `references` disclaimer is printed wherever such a pointer is followed. The protocol does
**not** claim global deduplication or cross-team integrity, and an implementation must not imply it does.
