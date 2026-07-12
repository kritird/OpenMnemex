---
name: mnx-init
description: Set up Mnemex for the user — bind the current project (or the user account) to a knowledge-graph repo, or scaffold a brand-new graph. Use this when no Mnemex graph is configured yet (a read/write skill reported "No Mnemex graph configured"), when the user says "set up mnemex", "create a knowledge graph", "point mnemex at my repo", or wants to change which graph this project uses. Establishes the binding that every other Mnemex skill resolves.
---

# mnx-init — preflight setup and binding

Mnemex separates two repos: the **project** the author works in, and the **graph** repo where knowledge
is stored. This skill establishes the **binding** between them. Every other skill (`mnx-read`,
`mnx-capture`, `mnx-promote`, `mnx-doctor`) resolves that binding before doing anything; if it is missing
they stop and send the user here. (`mnx-capture` is the exception that needs only the binding, not a
synced clone — it writes to the local staging tier.) Background: `docs/binding-and-graph-sync.md`.

Helper you call: `scripts/mnx_binding.py` (resolve / sync / status / probe-remote).

## 1. Check current state first

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" resolve`.

- If it resolves (exit 0), tell the user the current binding (`graph_remote` + `source`) and ask whether
  they want to **keep**, **re-point**, or **add a project-level override**. Do not silently overwrite.
- If it does not resolve (exit 2), proceed to choose a setup mode.

## 2. Choose a mode (ask the user)

| Mode | When | Result |
|---|---|---|
| **Create a new graph** | The user has no graph yet. | Scaffold a graph (git repo **or** local folder), then write a binding to it. |
| **Bind this project** | A graph already exists; this project should use it. | Write `<project>/.mnemex.md`. |
| **Set user default** | The user wants one fallback graph for all projects. | Write `~/.claude/mnemex/config.md`. |

Independently, the graph can live as a **git remote** (`graph_remote` — cloned, synced, pushed) or a
**local folder** (`graph_path` — used in place; for authors with no git repo). Ask which; default to git
remote when the user has one, local folder otherwise.

`.mnemex.md` (project) **overrides** the user-level file, which is overridden only by the
`MNEMEX_GRAPH_REMOTE` / `MNEMEX_GRAPH_PATH` env vars (the resolution chain). Pick the narrowest scope that
fits the intent.

## 3a. Create a new graph

In the location the user will use as their graph — a **git repo** or a **local folder** (create the
folder if it does not exist):
- Scaffold (do not overwrite existing files): `index.md` (org router), `mnemex.config.md` (from
  `config/mnemex.config.md` defaults), the `.mnemex/` state directory, a `.gitignore` (from
  `templates/gitignore.template`, so a stranded lock/pass-plan under `.mnemex/locks/` or
  `.mnemex/plans/` is never committed into the graph), and a first `team-<name>/` skeleton with
  `index.md`, `registry.md`, `cross-links.md`. (This is the original scaffold contract.)
- Explicitly tell the user that **pattern nodes persist ~30% longer than domain facts**
  (`pattern_halflife_bonus`, default +30%) and how to change it.
- State the **two time horizons** and ask the user to confirm or adjust them, since both are conscious
  policy: (1) `half_life_days` (default 180) — how long an *unused* fact keeps half its relevance; and
  (2) `freshness_ttl_days` (default 30) — how long after it was last **verified** a fact is flagged
  **stale** so the agent re-checks it (a separate axis from decay; patterns get +30% here too). Mention
  that individual facts can be tagged `volatility: timeless` (never stale, never auto-dies) or
  `volatile` (short horizon). Write the chosen `freshness_ttl_days` into `mnemex.config.md`.
- **Git remote:** have the user create the remote and push, then capture its remote URL → `graph_remote`,
  and run the **remote pre-flight** below before binding (so an auth/URL problem surfaces now, not at sync).
- **Local folder:** capture its path → `graph_path`. If it is a git repo, writes commit locally; if it is
  a plain folder, writes append an audit trail to `.mnemex/history.log` (mention this to the user).
- Then write the binding (step 4).

## 3b / 3c. Bind to an existing graph

Get the graph's **git remote URL** (`graph_remote`) **or local folder path** (`graph_path`) from the
user — set exactly one. **Do not clone a remote manually** — the session-start sync (`mnx_binding.py
sync`) materializes it. Then write the binding (step 4).

### Remote pre-flight (do this BEFORE writing a remote binding)
For any `graph_remote` (whether creating or binding), test reachability + auth *before* you write the
binding, so the user fails fast with a fix instead of hitting a sync error later:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" probe-remote --remote <url>
```

- `reachable: true` → proceed to write the binding (step 4). If `empty: true`, tell the user the remote
  has no branches yet — they must push an initial commit first so `sync` has a HEAD to clone.
- `reachable: false` → **do not write the binding.** Report `message` + the `remediation` text verbatim
  (it is tailored to the `category`: `auth` / `not-found` / `network`). Then **offer the local-folder
  fallback** (the `fallback` field): re-run as local-folder mode (`graph_path`), which needs no git auth.
  Only retry the remote after the user fixes the cause (keys/token/URL).

This probe is read-only (`git ls-remote` with prompts disabled) — it never clones or writes.

## 4. Write the binding

- **Project scope:** write `<project-root>/.mnemex.md` from `templates/binding.template.md`, filling
  exactly one of `graph_remote` / `graph_path` and `default_team`/`author` if known. Ask whether to
  commit it (shared) or add it to `.gitignore` (personal).
- **User scope:** write `~/.claude/mnemex/config.md` from `templates/user-config.template.md`. Create the
  `~/.claude/mnemex/` directory if absent. Never write user config into `${CLAUDE_PLUGIN_ROOT}` — it is
  wiped on plugin updates.

## 5. Verify — and leave the graph doctor-clean on day one

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" sync`. Confirm `action` is `cloned` or
`resynced`. If `error` (rare once the pre-flight passed — e.g. credentials changed between probe and
sync), surface the git message, re-run `probe-remote` for a categorized diagnosis + remediation, and
offer the local-folder fallback.

Then finish the setup **inside the synced `graph_root`** (skipping these leaves two permanent doctor
warnings on a brand-new graph — inv-1 and inv-15):
1. `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_regen.py" install <graph_root>` — registers the
   merge driver for generated files (git config is per-clone, so this runs after sync, in the clone).
2. `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_config.py" stamp <graph_root>/team-<name>` — stamp each
   scaffolded team so config-drift detection has a baseline and the overdue nag starts its clock now.
3. `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_doctor.py" check <graph_root>` — expect **E0/W0**; then
   `mnx_binding.py persist --message "mnx-init: day-one stamp"` to commit/push the stamp.

On success, tell the user mnemex is ready and which graph it is bound to.

## First-contact graph behavior config

If the bound graph has **no** `mnemex.config.md` yet (a freshly created remote), write it from the
defaults and state the half-life and pattern bonus. The behavior config is owned by the graph repo and
lives **only** there — never copy it into the project binding.

## Never
- Never write decay/tier parameters into `.mnemex.md` or the user config — those belong to the graph.
- Never overwrite an existing binding without confirmation.
- Never clone the graph by hand from this skill — that is `sync`'s job.
