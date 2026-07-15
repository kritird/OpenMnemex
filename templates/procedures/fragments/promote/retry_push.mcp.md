### Retry-push recovery
The merge is already committed in the clone; only the push is missing. Skip straight to
`promote_retry_push` — no flush, no reconcile, no consolidate, no new plan. On success it
pushes the commit, then performs the deferred settle recorded in `pass.plan.json` (hold each
contradicting atom, clear-merge the promoted ones) and reports success. On `conflict`/`failed`
it returns the structured `recovery` block (`guidance`, `clone_path`, `branch`,
`manual_fallback`); staging stays untouched — do not loop a full promote.

