## Preflight
Call `promote_begin`. It handles all of this atomically: locate + sync the graph, the
unpushed-promote guard (if a prior promote committed but did not push, it refuses and names
`promote_retry_push` — do **not** start a fresh merge in that case, that would double-apply
staging over the already-committed merge), stranded-plan recovery, and acquiring the team
lock. It returns the staged session batch (with provenance) and the team phonebook, or a
guard block naming the next step. If staging is empty, promote is just "consolidate the
graph" — `promote_begin` still returns normally; proceed (or stop if nothing is overdue
either).

