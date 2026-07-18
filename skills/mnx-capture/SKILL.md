---
name: mnx-capture
description: Capture the durable knowledge produced in the current build session into the local Mnemex staging tier — cheap, fast, no lock, no graph mutation. Use this when a user finishes building or designing and wants to persist what was learned — domain facts AND the patterns/decisions from human review — or says "save this to the knowledge graph", "remember this for next time", "capture this", "stage this". Run it INCREMENTALLY at natural checkpoints too, not only at session end — after a sub-task lands, a review settles, or a compaction is signalled — staging only the delta (re-capturing identical content is a no-op), keeping keypoints loss-proof. Runs in-session to mine the artifact and review points from the transcript; extracts atoms, scores each now/later/not-needed, stages them with self-sufficient provenance. Also curates staging (--drop <id>, --discard-all) — the escape valve at the hard cap. Does NOT reconcile or merge into the shared graph — that is the deliberate, batched /mnemex:mnx-promote step.
---

# mnx-capture — stage a session's knowledge (the `git commit` of memory)

Turn what this session produced — the artifact **and** the human review/clarification points — into
**staged atoms**: provisional, local, self-sufficient knowledge units. The *how* lives in the
conversation (the corrections, the rejected alternatives), so mine the transcript, not just the final
artifact, **now** — by promote time the transcript is gone.

Capture is the **fast, local half** of the capture/promote split. It is cheap, takes no lock, never
reads the graph's cluster indexes, and **never mutates the graph**. Reconcile / merge / consolidate /
push all happen later in `/mnemex:mnx-promote`. (Analogy: capture = `git commit`; promote = `git push`/PR.)

**Capture incrementally, at checkpoints — not only at the very end.** Since that transcript shrinks at
every compaction, one end-of-session dump is the most loss-exposed way to run this: prefer to capture the
**delta** at each natural checkpoint (a sub-task lands, a review decision settles, a compaction is
signalled). Capture is built for repetition — it stages only what is new and re-staging identical content
is an idempotent no-op (content-hash id) — so it stays cheap; the delta mechanics are Phase 0b + Phase 1.

Background: `docs/staging-and-promotion.md` (the whole model), `docs/rationale-and-concepts.md`
(node types, ids), `docs/data-model-and-schemas.md` (staged-atom front-matter). Helper:
`mnx_stage` (the only writer here) and `mnx_binding` (locate the graph).

## Curate mode — review / drop / discard (no extraction)
If invoked with `--drop <provisional-id>` or `--discard-all`, this is the local **un-stage** path — the
cheap way to prune staging (and the escape valve when the hard cap is blocking new captures). It still
runs the locate preflight (to find the staging tier) but does **no** extraction, scoring, or staging:
- **Review first** when helpful: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_stage.py" list` shows the
  staged atoms (provisional id · score · summary · age). (`/mnemex:mnx-status` shows the same list.)
- `--drop <id>` → `mnx_stage.py clear-one --id <id>`; report the dropped atom's id + summary (or that it
  was not found).
- `--discard-all` → show the list and **confirm with the user**, then `mnx_stage.py clear`; report the
  count removed.
This touches **only** the local staging tier — never the graph, never the stamp spill. Then stop.

## Preflight — locate the graph (always first)
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" status --session <sid>` (the session id
from session-start, if you have one — see mnx-init step 1; honors a mid-session graph switch).
- If `resolved` is false → **STOP**: *"No Mnemex graph configured. Run `/mnemex:mnx-init`."*
- **Echo the resolved graph before staging** so the author sees where atoms will land: show the
  `resolution` line, e.g. *"Capturing into **payments-knowledge** (source: project .mnemex.md)."*
  If `default_fallback` is true, make it **prominent** — there was no project `.mnemex.md`, so this is
  going to the user's personal graph: *"⚠️ No project binding here — capturing into your personal
  graph **personal-notes**. If that's wrong, cd into the right repo and re-capture."* This closes the
  silent-binding gap (LIMITATIONS.md #2): the graph choice is now visible at capture time, not discovered
  at promote time.
- Note `graph_root` (for routing intent only — capture writes **nothing** there) and `staging_root`
  (where atoms land). Capture is local; it does **not** need `clone_present` / a sync.

## Phase 0 — Budget pre-check (backpressure)
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_stage.py" status`.
- `budget.level == "hard"` → **STOP capturing** and give the user the two ways out (the backpressure
  bound): either *"run `/mnemex:mnx-promote` to merge + drain staging"* **or** *"make room by discarding
  with `/mnemex:mnx-capture --drop <id>` or `--discard-all`."* Show the staged list (`mnx_stage.py list`)
  so they can choose what to drop.
- `budget.level == "soft"` → proceed, but **warn** the user once that a promote is due.

## Phase 0b — Delta ledger (what is already staged)
Before extracting, load the staged ledger so you capture only the **delta** and can see what is already
covered:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_stage.py" list
```

Each row is `provisional-id · type · score · summary · age`. Read it as the running record of "keypoints
already captured this session." In Phase 1, extract only what this ledger does **not** already cover, then
look for the next uncaptured keypoint. You do not need to diff by hand — re-staging identical content is an
idempotent no-op (content-hash id) — but consulting the ledger keeps you from re-mining ground you already
staged and makes the incremental checkpoint loop cheap. A genuinely refined atom (same concept, better
body) will hash differently and stage as a new atom; that is fine — reconcile collapses the pair at promote.

## Phase 1 — Extract the delta (mine the transcript, honor the node-size budget)
Decompose the artifact + transcript into candidate atoms, skipping anything the Phase 0b ledger already
covers — extract the **new** keypoints since your last capture. For each, decide:
- **`domain`** (a fact about the system/business — the *what*), or
- **`pattern`** (prescriptive *how*, with a `trigger` = the *when* it applies). **Mine human review
  points specifically**: a correction or a rejected alternative becomes a pattern — *"do X not Y,
  because…"* — with a trigger describing the situation it governs.

Draft `summary` (one line), `aliases` (other names the concept goes by), `domain` (routing key(s)),
and a tight body. **Propose a `volatility`** (freshness horizon, Freshness & Revalidation) from the atom's content shape —
`volatile` for a fact that rots fast (a URL, version, price, on-call name), `timeless` for a durable
definition/invariant (also exempts it from ever auto-dying), or leave it `default` (the type-derived
horizon) for everything else. It is a *suggestion*: the human confirms or overrides it at the promote
gate, so bias toward `default` when unsure.

**Embed links inline as `[[wiki-links]]` (the mesh authoring surface, Link Reconciliation).** When a note refers to
another concept, name it inline in the body with double brackets — *"…settles against
`[[iso8583-field124]]` before posting…"*. Link freely **by name**, even to a page that does not exist in
the graph yet: promote resolves each `[[name]]`, and an unresolved one becomes a **red-link** that goes
live automatically the day that page is created. The pipe means display text, wiki-native
(`[[iso8583-field124|Field 124]]`), **not** a relationship type. `mnx_stage` hoists these into the atom's
`mentions:` for you — you just write natural `[[links]]`. Do **not** hand-author `edges:`; that is a
generated mirror promote builds.

**Node-size budget — do NOT split at capture.** Keep each atom's body focused, but if a genuinely
single idea runs past the soft cap (`node_body_max_chars`, default ~6000), **capture it whole** — never
truncate. **Splitting an over-budget note into sibling pages + a link is promote's job, not capture's**
(promote is graph-aware; capture is local and dumb). Cap the number of atoms per session to what the
session actually produced; do not pad.

## Phase 1b — Glean (one bounded "what did I miss?" re-scan)
Pass 1 (Phase 1) is your first extraction pass; a single pass reliably **under-captures the *how*** — the
review-point patterns are the easiest to miss. So run **one** bounded recall pass (this is the shared
*gleanings* technique, guardrail mode):

- Re-read the transcript once and ask specifically: *"what durable fact — **especially a review-point
  pattern** (a correction, a rejected alternative, a 'gotcha') — did I not stage yet?"*, guided by the
  Phase-0b delta ledger so you look only at ground you have **not** already covered.
- Any new candidate still passes the Phase-2 `now/later/not-needed` scoring gate and Phase-3 staging;
  re-staging identical content stays an idempotent no-op (content-hash id), so this pass can only add.
- **Bound the loop deterministically.** After the pass, count staged atoms before vs after and check
  whether to stop:

  ```
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_glean.py" step --before <n_before> --after <n_after> --pass 1
  ```

  `stop:true` (`reason: no-progress` = the pass added nothing new, or `reason: cap` = the pass budget
  `max_glean_passes` is reached — default **2**, from user config) → stop gleaning and go to Phase 2/4.
  Only if `stop:false` do you run one more pass (`--pass 2`). This is **not** a per-topic walk — it is at
  most a couple of bounded re-scans, keeping capture cheap/fast (the design goal). Episodic capture does
  **not** build a coverage checklist — that (checklist mode) is ingest-only.

## Phase 2 — Score each atom (`now | later | not-needed`)
A momentary judgement of **intrinsic importance — NOT novelty**. Drift between sessions is fine; there
is no rigid rubric. Novelty/dedup is decided later at promote (reconcile may drop an atom as a
duplicate), so do **not** pre-judge "probably already known."
- **`now`** → stage **with `--urgent`**. (Urgent never inline-pushes — promote is still the only
  writer; urgent only sharpens the nag.)
- **`later`** → stage normally.
- **`not-needed`** → **silently drop.** No staging, no audit, no asking the user. Reserve this for the
  clearly ephemeral or trivially derivable.

## Phase 3 — Stage (the only write)
For each kept atom, write it to the staging tier. Provenance must be **self-sufficient for a cold
promote** — artifact ref, the specific review ids, rejected alternative(s), the rationale, and the
session timestamp:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_stage.py" add --json <<'JSON'
{ "type": "pattern",
  "summary": "Reconcile settlement before posting",
  "aliases": ["settle-recon"],
  "domain": ["settlement"],
  "trigger": "reviewing or curating a settlement spec",
  "score": "now", "urgent": true, "volatility": "default",
  "provenance": { "artifact": "tap-vic-settlement-spec", "reviews": ["r3","r7"],
                  "rejected": ["post-then-reconcile (causes orphaned legs)"],
                  "rationale": "human correction in review r7" },
  "body": "Always reconcile the settlement batch against [[iso8583-field124]] before posting legs, because …" }
JSON
```

(Or use flags for a simple atom: `add --type domain --summary "…" --domain settlement --score later
--aliases "a;b" --artifact <id> --reviews "r3;r7" --rationale "…" --body "…"`.) The helper mints the
**provisional id** (a content hash, `stg-…`) — never invent an id, never reuse a real node id. A
re-capture of identical content is idempotent and reports `action: already-staged` (vs `staged` for
new content) — count the two separately so your report reflects the true delta.

## Phase 4 — Report
Summarize what was staged **this pass**: the new atoms (counts by score), any `urgent`, and the
post-stage `budget.level`. When capturing incrementally, distinguish this pass's delta from the total
staged set so the checkpoint loop is legible ("staged 2 new; 7 total staged"). If the helper
**refused** an atom (`action: refused`), surface the hard-cap message and give both ways out —
`/mnemex:mnx-promote` to drain staging, or `/mnemex:mnx-capture --drop <id>` / `--discard-all` to make
room. Then stop — **do not** offer to push or merge; that is promote's job.

## Never
- Never reconcile, merge, re-tier, or open the graph's cluster indexes — capture is local-only.
- Never write into `graph_root`, never take the team lock, never commit or push.
- Never stamp a staged atom or give it a real node id (the `stg-` provisional id is content-derived).
- Never `not-needed`-drop on a *novelty* guess — only the clearly ephemeral/derivable.
- Never split, resolve a `[[link]]`, or hand-author `edges:` — capture preserves links; **promote** splits
  over-budget notes and resolves links (Link Reconciliation). Never truncate an over-budget atom — capture it whole.
