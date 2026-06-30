---
description: Promote the locally-staged captures into the shared Mnemex graph — flush usage stamps, reconcile + merge every staged atom (clean-context sub-agent, human-in-the-loop on contradictions), consolidate the post-merge graph, run the doctor, push, and clear staging. Atomic and total; the deliberate `git push`/PR half of memory.
argument-hint: "[--dry-run | --retry-push]"
---

Use the **mnx-promote** skill to merge staging into the Mnemex graph — the deliberate, batched half.

Options: $ARGUMENTS

**`--retry-push`** (recovery): if a prior promote committed the merge but the push did not land
(`mnx_binding.py status` → `unpushed: true`, `ahead > 0`), this pushes that **existing** commit and then
clears staging on success — it does **not** re-run the merge (a blind re-promote would double-apply
staging on top of the commit). Run `mnx_binding.py push`; on success `mnx_stage.py clear`; on continued
failure surface the structured `recovery` guidance (clone path, branch, manual_fallback commands).

Pre-flight: resolve + sync the graph (`mnx_binding.py status`/`sync`, or stop and point at
`/mnemex:mnx-init`). **If `unpushed: true` and this is not `--retry-push`, STOP** and tell the user a
prior promote is committed-but-unpushed — run `/mnemex:mnx-promote --retry-push` (never start a fresh
merge over it). Otherwise acquire the team lock; recover any stranded pass. Then the cycle (decisions
#12/#13): **flush usage stamps** (`mnx_stamp.py flush`) → **reconcile + merge** every staged atom via a
**clean-context reconcile sub-agent** (input `{staged atoms, graph_root}`, returns plan + HITL items
only, may fork per cluster; plan in parallel, apply serially) assigning each atom a terminal disposition
(create / merge / drop-dup / supersede / resurrect) → **consolidate** the post-merge graph (the internal
`mnx-consolidate` skill: re-tier, death, edge hygiene, budget split → index chaining) folded into the
**same** approval plan → **STOP** for human approval (also stop on `--dry-run`) → apply serially under
the lock → `mnx_doctor.py check` (must pass) → **persist** (`mnx_binding.py persist` — commit+push /
commit / audit-append by kind) → **clear staging** only on confirmed persist (`mnx_stage.py clear`).
Contradictions are a hard HITL block: resolve all in-cycle or **abort** (staging untouched). Never
carry a provisional `stg-…` id into the graph; promotion mints a real slug id.
