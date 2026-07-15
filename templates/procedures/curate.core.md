---
name: curate
description: Review, drop, or discard atoms already staged in the local Mnemex staging tier — no extraction, no graph mutation. The un-stage escape valve, including at the hard capture-budget cap.
---
## Curate mode — review / drop / discard (no extraction)
{{BLOCK:trigger_condition}}
- **Review first** when helpful: {{CALL:capture_status_inline}} shows the
  staged atoms (provisional id · score · summary · age). (`{{PROC:status}}` shows the same list.)
- `--drop <id>` → {{CALL:capture_drop}}; report the dropped atom's id + summary (or that it
  was not found).
- `--discard-all` → show the list and **confirm with the user**, then {{CALL:capture_discard_all}}; report the
  count removed.
This touches **only** the local staging tier — never the graph, never the stamp spill. Then stop.
