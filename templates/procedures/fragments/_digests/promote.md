Procedure: `promote_begin` (locks + guards) → `promote_context` (near-matches, link preview)
→ draft one plan (create/merge/supersede/resurrect/drop_dup/hold per staged atom, plus link
and consolidate decisions) → present it for human approval → `promote_apply` with
`approved: true`. Contradictions go to `hold`, never force-resolved or silently dropped.