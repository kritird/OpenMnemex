---
description: Validate the Mnemex graph against all integrity invariants (edge targets, index/node match, denormalized copies, reverse-map, dangling edges, hot bound, cross-links, config drift) and optionally self-heal derived files.
argument-hint: "[--fix] [--team <name>]"
---

Use the **mnx-doctor** skill to check the Mnemex graph's invariants.

Options: $ARGUMENTS

Run the full invariant suite from docs/08. Report findings by severity (error/warning/info). With
`--fix`, regenerate only DERIVED files (indexes, reverse map, cross-links) from the nodes — never edit
node knowledge. Error-level invariants that involve node truth (missing edge targets, invalid
front-matter, changed ids) require human/skill attention and must not be auto-edited.
