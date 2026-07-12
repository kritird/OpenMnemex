---
name: mnx-ingest
description: Bootstrap or update a Mnemex knowledge graph from an EXISTING code or documentation repository (a local folder or a git remote) — no live session required. Use this when the user wants to seed a graph from a repo, says "ingest this repo", "bootstrap the graph from <repo>", "import our docs/code into memory", "index this codebase into the graph", or wants to re-sync a previously ingested corpus. Walks the source, DISTILLS durable atoms (never transcribes), discovers a deduped entity catalog, wikifies links, and stages a labeled bulk batch — then /mnemex:mnx-promote --bulk merges it. Two gates only (scope up front, bulk summary at the end); no per-atom review. Idempotent on re-run (a deleted source file surfaces as an orphan candidate, never auto-death). Never writes the graph (staging only) and never mutates the source.
argument-hint: "<local-path|git-url> [--into <graph>] [--dry-run] [--resume <ingest-batch>]"
---

# mnx-ingest — bootstrap the graph from an existing repo (a *source adapter*, not a new subsystem)

A live session is one producer of staged atoms; **a corpus is a second.** Ingest walks a repo, *distills*
durable atoms, discovers the entity structure a lived transcript would have handed capture for free,
wikifies links, and stages a **labeled bulk batch** into the same staging tier. Everything downstream —
reconcile, MERGE/SUPERSEDE, contradiction HITL, the wiki mesh, consolidate, doctor, push — is the
**existing** `/mnemex:mnx-promote --bulk`, reused unchanged. **`mnx-promote` stays the only writer to the
graph; ingest only stages.**

The deterministic mechanics (walk · classify · chunk · hash · delta · ER blocking/clustering · bulk
staging) live in `mnx_*.py`; the **judgment** (is this a durable atom? which `[[link]]`? which merge?)
lives here in prose + sub-agents and cannot move into Python.

Background: `docs/corpus-ingestion.md` (the full model), `docs/staging-and-promotion.md` (the pipeline
this reuses), `docs/link-reconciliation.md` (the mesh), `docs/multi-graph-and-team-routing.md` (routing).
Helpers: `mnx_ingest` (walk/probe/delta/manifest), `mnx_glean` (the bounded recall loop), `mnx_er` (entity
resolution), `mnx_simindex` (the ER blocker), `mnx_stage` (bulk staging), `mnx_binding` (locate/clone),
`mnx_phonebook`/`mnx_mesh` (wikification catalog).

## The two invariants that shape everything
- **Distill, never transcribe (DP2).** No file body is copied wholesale into a node; **zero atoms from a
  file is valid and common** (boilerplate, generated code, changelogs, lockfiles). The graph is distilled
  durable memory, not a RAG index over the repo.
- **Single writer + read-only source (DP1, DP3).** Ingest stages only; it never writes the graph and never
  mutates the source (a remote is cloned to a read-only cache; a local path is read in place). Secrets are
  never read.

---

## Preflight — resolve the target graph
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" status` (or honor an explicit `--into <graph>`).
- If unresolved and no `--into` → **STOP**: *"No target graph. Pass `--into <graph>` or run `/mnemex:mnx-init`."*
- **Echo the resolved target graph** before anything stages, exactly like capture/promote — the user must
  see where the import will land. If `default_fallback` is true, flag it prominently and confirm.
- If `clone_present` is false → `mnx_binding.py sync` once. Note `graph_root`.

## SOURCE — acquire the corpus (read-only)
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_ingest.py" acquire --source <path|url>`.
- A **local path** is used in place; a **remote URL / `*.git`** is shallow-cloned into a read-only cache.
- Note `root` (what you walk), `commit` (the exact ref distilled from), and the source slug (derive it with
  `mnx_ingest.py source-slug --source <…>` — it keys the manifest). **Never** write under `root`.

## PROBE + DELTA — scope the run (feeds gate #1)
1. `mnx_ingest.py probe --root <root>` → `{units[], counts, est_atoms, bytes_total, skipped_secrets}`.
   Units are already classified (`doc | interface | code-doc | config | skip`) and chunked along structure
   (docs by heading, code by **exported** symbol — private symbols are never emitted).
2. On a **re-ingest**, diff against the prior manifest:
   `mnx_ingest.py delta --root <root> --manifest <graph_root>/.mnemex/ingest/<slug>.json`
   → `{added[], changed[], unchanged, orphans[]}`. **Extract only from `added` + `changed`** — unchanged
   files are skipped before any sub-agent runs (the dominant re-run cost saver). Hold the `orphans` list
   (deleted source files' node_ids) for the report — **never auto-tombstone** them (DP4).

## GATE #1 — scope + source-tree → cluster map  (STOP for the human)
Emit the scope preview and **wait**:

```
INGEST SCOPE  source: github.com/acme/payments-service @ 9f3c1a  → graph: payments-knowledge
  files: doc 42 · interface 30 · code-doc 18 · config 6 · skip 210 · secrets-skipped 3
  est. atoms ≈ 140   (cost ceiling ingest_max_atoms_per_run: 2000 — excess resumes next run)
  re-ingest delta: 12 added · 4 changed · 900 unchanged (skipped) · 2 orphan candidates
  SOURCE-TREE → CLUSTER MAP  (proposed from paths; editable — the bulk analog of per-atom domain:)
    settlement/**   → team-payments/settlement
    rails/**        → team-payments/rails
    docs/risk/**    → team-risk/finality        (cross-team → needs default_team or existing cluster)
    proto/**        → (classify per file — mixed)
  code_extract: gated   (public/documented/config-only; deep opt-in per subtree)
```

The **source-tree → cluster map** is the bulk analog of capture's per-atom `domain:` — approve/edit it
**once** here, and every atom under a subtree inherits that placement. Routing still flows through the
normal promote precedence (org→team match, `default_team` fallback); the map is a *default*, not a bypass.
Confirm scope (or trim it) and the `code_extract` policy. **`--dry-run` stops here** (probe only, nothing
staged).

## PASS 1 — extract + glean + entity-resolve (per subtree, bulk batches)
Mint one **ingest batch id** for the run (e.g. `ing-<date>-<rand>`). Work **per source subtree**, in
bounded batches, honoring `ingest_max_atoms_per_run` (excess resumes next run via `--resume`).

**1a — Distil candidate atoms + entities (kind-aware, LLM judgment).** For each unit, decide what durable
knowledge it holds — *"a fact (the *what*) or a prescriptive pattern (the *how*) a future agent would want
months from now without the source open?"* Kind-aware policy:
- **doc** → domain facts from sections; **patterns** from ADRs / decisions / "gotchas" / runbooks / CONTRIBUTING.
  **Every `type: pattern` atom MUST carry a `trigger`** — the one-line "when does this pattern fire?"
  (e.g. `"when sizing expiry windows in a connector"`). `mnx_stage.add` refuses a pattern without one,
  so distil the trigger from the section's context at extraction time, not later.
- **interface** → the *contract* (public API signature, `.proto`/GraphQL/OpenAPI message, exported type).
- **code-doc** → the intent/semantics the author wrote down (docstrings, module/dir headers).
- **config** → declared knobs + their meaning. **Secrets are never read.**
- **Code value-gate (non-negotiable noise control):** a code-derived atom is staged **only** if it is a
  public/exported contract, **or** carries an author-written docstring/comment explaining *why*, **or** is a
  config knob with a declared meaning. A bare private helper with no doc is **not** an atom. Function
  *bodies* are never transcribed — only the distilled semantics. Emit `[[wiki-links]]` inline by name.

**1b — Glean (checklist mode — bounded recall).** A single extraction pass under-captures. Build the
candidate-unit list as the coverage checklist and run the shared *gleanings* loop:
```
mnx_glean.py coverage --units <units.json> --staged <staged-ledger.json> --pass <k>
```
It returns the **zero-atom units** (`uncovered`) + a `stop` signal (`complete` = every unit produced ≥1
atom, or `cap` = `max_glean_passes` reached, default 2). For each uncovered unit, re-examine it once —
*"what durable fact/entity did this unit contain that I did not extract?"* — then re-run `coverage`. A unit
is *covered* when a staged atom carries its `anchor` in provenance. Stop on `complete`/`cap`. Re-staging
identical content stays an idempotent no-op (DP10). The judgment stays here; `mnx_glean` only bounds/bookkeeps.

**1c — Assemble the in-batch entity catalog + entity-resolve (dedup, DP5).** Collect the candidate entities
(canonical name + aliases + type) across the whole delta corpus, then run ER over `{new atoms ∪ existing
graph pages}`:
```
mnx_er.py resolve --graph <graph_root> --atoms <candidates.json> [--team <t>]
```
It blocks (via `mnx_simindex.pairs --with --intra`), scores, clusters, and proposes a disposition per
cluster: **CREATE** (no graph match) · **MERGE** (folds into an existing page, keeps its id) · **COLLAPSE**
(intra-batch duplicates → one CREATE). The `possible` band is the **only** place you (the LLM judge) rule
on a merge — everything at/above `match` or below `possible` is deterministic. The output is your deduped,
canonical entity set (aliases unioned). **One entity → one node**: many corroborating sources collapse into
one well-provenanced node, never duplicate nodes.

## PASS 2 — wikify (rewrite atom bodies against the catalog) + stage
Resolving each name greedily as you meet it fragments a large graph. Instead, link every atom against the
**one** catalog you just built (∪ the team phonebook — `mnx_phonebook.py resolve` / `resolve-batch`):
- For each mention of a catalog/graph entity in an atom body → emit `[[canonical-name]]` (piped display if
  the surface form differs). A mention with **no** catalog/graph entity → still `[[bracket]]` it → a
  **red-link** that heals the moment its page is created (later batches in this same run knit the mesh).
- **Precision discipline (DP6):** an **exact** catalog/phonebook match links deterministically; a
  **fuzzy/semantic** near-match is a `⚠ suggested` link surfaced at gate #2, **never** auto-written. A wrong
  link is a false edge. High recall in Pass 1 (bracket generously — red-links are cheap); high precision in
  Pass 2 (only *confident* links go live).

**Stage each atom under the bulk label** with source-anchored provenance so it is promotable **cold**:
```
mnx_stage.py add --json <<'JSON'
{ "type": "domain", "summary": "Settlement cut-off is 23:00 UTC; post-cutoff addenda ride in field 124",
  "aliases": ["field 124", "settlement cutoff"], "domain": ["settlement"], "score": "later",
  "ingest_batch": "ing-2026-07-11-a1b2",
  "provenance": { "source_repo": "github.com/acme/payments-service", "commit_sha": "9f3c1a…",
                  "source_path": "settlement/reconcile.md", "anchor": "Cut-off handling", "kind": "doc",
                  "rationale": "distilled from settlement design doc" },
  "body": "Settlement reconciles the batch before posting; post-cutoff addenda ride in [[iso8583-field124]]." }
JSON
```
- **`provenance.anchor` = the unit's `anchor` from `probe` output, verbatim** (bare heading text, no
  leading `#`s) — it is the glean-coverage key AND what the manifest ties node ids back to. A `pattern`
  atom additionally carries the top-level `"trigger"` field (see the kind-aware policy above).

**Hub atoms — make the mesh knit (do this, or the import lands ~all-red).** Fact atoms link to entity
*names* ([[ilp-address]]), but no fact atom IS the entity page, so without hubs nearly every link stays
a red-link and the whole import scores in-degree ≈ 0 (inert tiers, orphan-flood). For each **catalog
entity** (Pass 1c output) that (a) is mentioned by **≥3** staged atoms and (b) has **no** graph match
(ER said CREATE, not MERGE), stage **one hub atom**: `type: domain`, the entity's canonical name +
aliases, `volatility: timeless` when definitional, a 1–3 sentence body that says what the entity *is*
and `[[links]]` to its closest siblings. Red-links to it heal deterministically at promote
(`mnx_phonebook.backfill`), knitting the mesh in the same run. Below the mention threshold, leave the
red-link latent — a hub nobody points at is noise.
The `--ingest-batch` label sets `bulk: true` and partitions these atoms from any hand-captures (DP8) — the
per-session nag never fires, and the batch has its own large cap. Re-staging identical content is a no-op.

## DRAIN — hand off to bulk promote
Ingest **never writes the graph.** Drain the staged batch with the existing writer:
`/mnemex:mnx-promote --bulk --ingest-batch <id>` (gate #2 = the bulk summary; it auto-accepts plain
CREATE/MERGE and stops only on contradictions + new-cluster creation). Promote writes the manifest
(`source_path@commit → node_ids`) on confirmed persist, so the next ingest diffs correctly.

## REPORT
Summarize by cluster: created / merged / superseded / dropped-dup / held, atoms staged this run, the
**orphan candidates** (deleted source files — surfaced for the human, never auto-tombstoned), and whether
the cost ceiling forced a `--resume`. If `--resume <ingest-batch>` was given, continue from the manifest +
remaining staged atoms rather than restarting.

## Never
- Never write into `graph_root` — ingest **only stages**; `/mnemex:mnx-promote` is the sole writer.
- Never mutate the source corpus, and never read a secret (the walk skips + counts them; you never open them).
- Never transcribe a file body into a node — distill; zero atoms from a file is valid.
- Never stage 5 atoms for one entity — ER collapses intra-batch duplicates **before** staging (one entity → one node).
- Never auto-write a fuzzy link (⚠ suggested → gate #2) and never auto-tombstone a deleted file's nodes (orphan candidate).
- Never do per-atom review — two gates only (scope up front, bulk summary at the end).
- Never let community detection mint structure — path-based routing is the default; Leiden may only *propose* at gate #1.
