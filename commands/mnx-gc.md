---
description: Run the Mnemex maintenance pass — compact usage, recompute decay and structural strength on a frozen snapshot, re-tier hot/warm/cold, tombstone dead nodes, sever edges, and regenerate navigation; locked and atomic.
argument-hint: "[--team <name>] [--apply] [--dry-run]"
---

Use the **mnx-gc** skill to run the maintenance pass on the Mnemex graph.

Options: $ARGUMENTS

Strictly snapshot-then-apply. Pre-flight: acquire the team lock; recover any in-progress pass;
re-normalize if config_version/λ changed. Phase A (MARK, read-only): freeze snapshot + cross-links;
replay registry deltas since the high-water mark; compute scores, structural strength, retention, target
tiers, and death candidates (conjunction gate + sole-referrer reluctance); write the plan file. Phase B
(SWEEP, serial, locked): apply tier relabels, tombstone dead nodes and transactionally sever their edges
(cold included), delta-update cross-links, advance high-water marks, stamp last_compaction/config_version,
run mnx-doctor, one git commit. Honor `node_budget`: split along the `domain:` sub-key, escalate to the
human if a single sub-key still overflows. Default proposes a plan and asks for confirmation; `--apply`
runs end-to-end.
