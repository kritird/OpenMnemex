---
description: Scaffold a new Mnemex knowledge-graph repository — create the org index, config file, .mnemex state, and a first team skeleton. Idempotent.
argument-hint: "[--team <name>]"
---

Use the **mnx-init** workflow to scaffold a Mnemex knowledge graph in the current repository.

Options: $ARGUMENTS

Create (if absent): `index.md` (org router), `mnemex.config.md` (from the plugin's config defaults,
and explicitly tell the user that pattern nodes will persist ~30% longer than domain facts and how to
change that), `.mnemex/` state directory, and a first `team-<name>/` skeleton with its `index.md`,
`registry.md`, and `cross-links.md`. Do not overwrite existing files.
