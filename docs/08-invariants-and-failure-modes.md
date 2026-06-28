# 08 — Invariants and Failure Modes

Two halves. First, the **invariants** `mnx-doctor` enforces — the contract that keeps an LLM-authored
graph trustworthy. Second, the **failure-mode register** — the breaks found while pressure-testing the
design, each with its mitigation and where in the standard it is handled. A correct implementation must
satisfy every invariant and defend every failure mode marked *hardened*; the two *soft* items are
documented limits, not defects.

---

## Part A — Validator invariants (`mnx-doctor`)

Severity: **E** = error (graph is corrupt; block commits), **W** = warning (drift; auto-fixable),
**I** = info (advisory).

### Referential integrity
1. **(E) Edge targets exist.** Every `to` in every node's `edges` resolves to an existing node id.
2. **(E) No dangling edges to tombstoned/removed nodes.** After any death, no live edge points at the
   dead id unless repointed to its `superseded-by`.
3. **(E) Reverse map consistency.** The built reverse map covers all tiers **and** tombstones; for every
   edge A→B, A appears in `referrers(B)`.
4. **(E) Cross-links completeness + path accuracy.** Every boundary edge appears in `cross-links.md`
   with correct `from_path`/`to_path`; no stale rows.
5. **(I) Soft references flagged.** Cross-team `references` are present-but-unverified; report dangling
   ones without failing (they carry the no-integrity disclaimer).

### Schema
6. **(E) Front-matter valid.** `id` valid slug; `type ∈ {domain,pattern}`; `status` valid; `pattern`
   nodes have a non-null `trigger`; timestamps are UTC ISO-8601.
7. **(E) Id stability.** A node's `id` matches its historical id (ids never change); detect a changed id
   as corruption.

### Derived-state freshness
8. **(E) Index ↔ folder node-set match.** The index's node-set equals the folder's actual nodes.
9. **(E) Denormalization fresh.** `index.summary == node.summary` and `index.aliases == node.aliases`
   for every node. *(This is the invariant that justifies storing `summary`/`aliases` copies in the
   index for match-without-body-load.)*
10. **(W) Materialized state present.** Every active node has `strength`/`last_update` in the index.

### Tier / budget
11. **(E) Hot bound.** Each cluster's `hot` section length ≤ `hot_k`.
12. **(W) Budget.** No cluster index exceeds `node_budget` active nodes (else: split or escalate).
13. **(I) Orphan flag.** Nodes with zero incoming edges (local + cross) are flagged (candidates the
    conjunction gate may eventually demote).

### Telemetry / state
14. **(E) High-water monotonic.** Each cluster's HWM only advances; registry has no lines below a
    truncated mark.
15. **(W) Config drift.** `config_version`/`λ` in `.mnemex/` matches `mnemex.config.md`, or a
    re-normalization is pending.
16. **(E) No stranded pass.** No `pass.plan.json` present without an active lock (else: crash recovery).

`--fix` resolves all **W** items by regenerating derived files from the nodes; **E** items involving
truth (1, 2, 6, 7) require human/skill attention because they indicate node-level corruption, not
derived drift.

---

## Part B — Failure-mode register

Each entry: the break, its mitigation, and the doc that owns the fix.

### Hardened (must be defended)

| # | Failure | Mitigation | Owned by |
|---|---|---|---|
| F1 | **Ordering corruption** — re-tiering reads structural strength that the same pass then mutates ⇒ non-deterministic outcome. | **Snapshot-then-apply**: all decisions against a frozen view; struct measured once. | Doc 05 |
| F2 | **Compaction race** — a read appends a stamp during replay-then-truncate ⇒ lost stamp. | **WAL high-water checkpoint**, never truncate. | Doc 02 §2 |
| F3 | **Immortal nodes** — unbounded `strength += boost` ⇒ a node decay can never demote. | **Saturating add** (`strength_max`) / diminishing boost. | Doc 02 §1 |
| F4 | **Clock-skew decay inversion** — negative Δt across machines/timezones turns decay into growth. | **UTC ISO-8601 via one helper** + **clamp Δt ≥ 0**. | Doc 02 §1, Doc 06 |
| F5 | **Retroactive config drift** — editing half-life silently re-interprets stored strengths ⇒ mass flash-cold. | **config_version stamp** + one-time **re-normalization** before tier decisions. | Doc 02 §8, Doc 07 |
| F6 | **Read-triggers-write** — read performs “overdue” compaction ⇒ dirty tree, races, mutation on read path. | **Detect-and-warn only**; never compact in `mnx-read`; at most an append-only marker. | Doc 02 §7, Doc 04 |
| F7 | **Budget re-overflow** — sweeping cold still leaves a cluster over budget on one sub-key. | **Split along declared sub-key**; if a single sub-key exceeds budget, **escalate to human** (never auto-invent structure). | Doc 05 |
| F8 | **Orphan cascade** — demoting/killing a node orphans a live node it was the sole inbound for. | **Sole-referrer reluctance** + **conjunction gate** (low usage AND structurally weak), checked on snapshot. | Doc 05, Doc 01 §9 |
| F9 | **Dangling/dead edges** — tombstoned node leaves edges pointing at it; cold node missed by a partial reverse map. | **Reverse map includes cold+dead**; **transactional edge-severing** at death. | Doc 05, Doc 06 |
| F10 | **Half-applied pass** — model times out mid-sweep. | **Plan file + single end-commit + crash-recovery** (`git checkout` to last good commit). | Doc 02 §10, Doc 05 |
| F11 | **Cross-cluster severing race** — two `gc`s rewrite a shared referrer concurrently. | **Team-root lock**; parallel mark, **serial sweep**. | Doc 02 §9 |
| F12 | **Cross-cluster structural blindness** — per-cluster struct misses cross-cluster in-degree ⇒ a hub looks weak and dies. | **`cross-links.md` in the snapshot**; struct = local + cross in-degree. | Doc 02 §5, Doc 03 §5 |
| F13 | **Denormalization staleness** — re-authoring a node's summary leaves a stale index copy ⇒ matching on wrong text. | **Invariant 9** enforced by `mnx-doctor`; regenerate on write/gc apply. | Part A, Doc 06 |
| F14 | **Usage starvation** — model under-reports the manifest ⇒ used nodes decay and wrongly go cold (silent knowledge loss). | **Mandatory disposition on every body-load**; structural strength as deterministic ballast. | Doc 01 §6, §9 |
| F15 | **Pattern sprawl** — the same “how” authored three ways. | **`trigger` field**; match patterns on trigger, merge near-dupes at the plan gate. | Doc 01 §2 |

### Soft (documented limits, not defects)

| # | Limit | Why it can't be fully hardened | Containment |
|---|---|---|---|
| S1 | **Self-reported usage quality** | No deterministic backstop for “did the model *really* use this.” | Graded manifest + justification rule; structural strength carries weight that usage cannot erode. |
| S2 | **Cross-cluster / cross-team duplication** | Reconciliation is cluster-local by design (the efficiency win); the same concept can be authored in two clusters. Going global re-introduces the search infra we reject. | Stable ids + aliases + `same-as` edges enable a **periodic human convergence ritual**; cross-team links are explicitly **soft** with a disclaimer. |

---

## How the soft limits are surfaced to users

Honesty is part of the standard. `mnx-doctor` reports S2 candidates (info-level: “possible duplicate of
`<id>` in `<other-cluster>` by alias overlap”) so the human convergence ritual has a worklist, and the
cross-team `references` disclaimer is printed wherever such a pointer is followed. The protocol does
**not** claim global deduplication or cross-team integrity, and an implementation must not imply it does.
