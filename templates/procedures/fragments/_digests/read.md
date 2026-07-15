Procedure: `read_frontier` to route (by description match) → `read_cluster` (hot tier
first, widen only if insufficient) → `read_nodes` for only the ids you'll actually use →
`record_usage` with a role (contributed/consulted/traversed) for every id loaded. Flag stale
rows and staged-overlay atoms in the answer; never body-merge a staged/graph contradiction.