---
id: REPLACE-with-stable-slug          # [a-z0-9-]+, assigned at creation, NEVER changes
type: domain                          # domain | pattern
title: Human-readable title (may change freely)
summary: One line that lands in the index verbatim — the match + routing surface.
aliases: [other name, abbreviation, synonym]
domain: [sub-domain]                  # may be a LIST (a node can belong to >1 sub-index)
status: active                        # active | superseded | archived | dead
confidence: high                      # high | medium | low
trigger: null                         # REQUIRED (non-null) for type: pattern; null for domain
edges:                                # OUTGOING edge instances owned by THIS node
  - { to: other-node-id, type: routes-through }
references: []                        # SOFT cross-TEAM pointers only (no integrity guarantee)
provenance:
  artifact: name-of-build-artifact
  reviews: []                         # human review-point ids that fed this node
  session: 1970-01-01T00:00:00Z
created: 1970-01-01T00:00:00Z
updated: 1970-01-01T00:00:00Z         # meaning-change time (NOT usage)
---

## Summary
One paragraph; read first on a body expansion.

## What
(domain nodes) The knowledge itself.

## How / Notes
(pattern nodes) The prescriptive procedure / rule and its rationale.

## Provenance
Why this node exists; trace to the artifact and the specific human review points.
