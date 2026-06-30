---
description: Retrieve knowledge from the Mnemex graph by structural routing and tiered, chunked reads; emit a usage manifest and stamp only nodes actually used.
argument-hint: <question or task to answer from the knowledge graph>
---

Use the **mnx-read** skill to answer the request below from the Mnemex knowledge graph (the graph the
binding resolves to — **not** the current working directory).

Request: $ARGUMENTS

Follow the skill exactly: run the **preflight** first (`mnx_binding.py status` → resolve the graph root,
or stop and point at `/mnemex:mnx-init` if unconfigured); run the overdue/config-drift check (warn only,
never compact); route
org → team → domain via chunk-1 index reads; scan Hot, then Warm, then Cold only as needed; expand only
the node bodies you commit to; follow edges within the frontier budget; then emit the `{id, role, why}`
usage manifest and append stamps to each used node's home-cluster registry. Never rewrite a node or an
index.
