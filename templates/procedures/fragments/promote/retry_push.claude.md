### Retry-push recovery (`--retry-push`, or an unpushed prior promote)
The merge is already committed in the clone; only the push is missing. **Skip Steps 1–4 entirely** —
no flush, no reconcile, no consolidate, no new plan:
- `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" push`.
- On `push: ok` → **now** do the deferred settle recorded in `pass.plan.json`: `mnx_stage.py hold` each
  contradicting atom, then `mnx_stage.py clear-merged --ids …` for the atoms the stranded commit
  promoted (if the plan predates per-atom settle and recorded no pid split, `mnx_stage.py clear` is the
  legacy fallback). Remove `pass.plan.json`, release any lock. Report success.
- On `conflict` / `failed` → surface the structured `recovery` block (its `guidance`, `clone_path`,
  `branch`, and `manual_fallback` commands). **Leave staging untouched.** Do not loop a full promote.

