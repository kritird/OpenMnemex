# Contributing to OpenMnemex

Thanks for your interest in helping build OpenMnemex! 🧠

OpenMnemex is self-curating, git-backed **memory for AI agents** — plain Markdown in a git
repo, no vector database, no server. Contributions of every size are welcome: bug reports,
docs fixes, new tests, and features.

This guide explains how the project is put together and how to make a change that lands
cleanly. If anything here is unclear or out of date, that itself is a great first contribution.

---

## Ways to contribute

- **Report a bug** — open an [issue](https://github.com/kritird/OpenMnemex/issues) with steps
  to reproduce, what you expected, and what actually happened.
- **Suggest a feature** — open an issue describing the problem you want solved (not just the
  solution). Context helps us design the right thing.
- **Improve docs** — typos, unclear wording, missing examples. `README.md`, `FEATURES.md`,
  `LIMITATIONS.md`, and everything under `docs/` are all fair game.
- **Write code** — bug fixes, new tests, or features. Please open (or comment on) an issue
  first for anything non-trivial, so we can agree on the approach before you invest time.

---

## Project setup

You need **Python 3.10 or newer** (3.9 is not supported — see the note in `pyproject.toml`).

```bash
# 1. Fork the repo on GitHub, then clone your fork
git clone https://github.com/<your-username>/OpenMnemex.git
cd OpenMnemex

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install the project in editable mode, plus the MCP extra and pytest
pip install -e ".[mcp]"
pip install pytest
```

Editable mode (`-e`) means your code changes take effect without reinstalling.

To try the app the way a user would:

```bash
openmnemex        # opens the local OpenMnemex Console in your browser
```

---

## Running the tests

The test suite lives in `tests/` and runs with **pytest**:

```bash
pytest              # run the full unit suite
pytest -q           # quieter output
pytest tests/test_promote.py        # run one file
pytest -k ingest                    # run tests matching a keyword
```

Some tests are marked and **skipped by default** because they are slow or need the network.
Opt into them explicitly:

| Marker     | What it does                                                        | Run with                  |
|------------|---------------------------------------------------------------------|---------------------------|
| `e2e`      | Live end-to-end scenario against a real graph repo (network + git)  | `pytest -m e2e`           |
| `scaling`  | Exercises index sharding at large node counts                       | `pytest -m scaling`       |
| `slow`     | Slower than a typical unit test                                     | `pytest -m slow`          |

**Please run `pytest` before opening a pull request, and add tests for any behavior you
change or add.** This project treats tests as the safety net for a system whose whole job is
to not lose people's memories.

---

## How the codebase is organized

A few landmarks so you know where things go:

- **`scripts/mnx_*.py`** — the shared engine. This is the real logic: capture, promote,
  decay, indexing, entity resolution, and so on. Despite living under `scripts/`, this *is*
  the installable `openmnemex` package (`scripts/__init__.py` is the bridge).
- **`skills/*/SKILL.md`** — the Claude Code **plugin** surface. Prose instructions that drive
  the same engine by hand.
- **`scripts/mnx_mcp.py`** — the **MCP server** surface. Tools any MCP client can call, which
  also drive the same engine.
- **`viewer/`** — the local web Console (read-only viewer).
- **`config/`, `templates/`, `integrations/`** — data the plugin and installer read.
- **`docs/`** — architecture, overview, and design notes.
- **`tests/`** — the pytest suite.

### ⚠️ The most important rule: keep the two surfaces in sync

OpenMnemex ships the **same functionality through two paths**:

1. the **MCP server** (`scripts/mnx_mcp.py`), and
2. the **Claude plugin** (`skills/*/SKILL.md`).

Both ultimately call the same engine functions in `scripts/mnx_*.py`, **but a fix made to one
surface does not automatically reach the other.** For example, a validation check added only
to an MCP tool leaves the plugin/CLI path exposed to the exact same bug.

**So, whenever you fix or enhance one surface, make the matching change on the other too.**
Concretely:

- Prefer putting correctness/validation fixes in the **shared engine function**
  (`scripts/mnx_*.py`) that sits *below* both surfaces — then every caller benefits at once.
- If a change is genuinely surface-specific (an MCP tool's structured error payload, or a
  skill's prose), still check whether the *other* surface needs an equivalent update, and do
  it in the same pull request.
- If you deliberately leave the two out of parity (e.g. a capability is MCP-only for now),
  say so explicitly in your PR description — don't let it be discovered later as a surprise.

---

## Making a change

1. **Create a branch** off `main`:
   ```bash
   git checkout -b fix/short-description
   ```
2. **Make your change.** Match the style, naming, and comment density of the surrounding
   code. This codebase leans on clear, explanatory comments where the *why* isn't obvious.
3. **Add or update tests** for the behavior you touched.
4. **Run the suite:** `pytest`.
5. **Keep both surfaces in sync** (see the rule above) if your change touches shared behavior.
6. **Commit** with a clear message describing the *why*, not just the *what*.

### Commit messages

Write short, present-tense summaries, e.g.:

```
Fix promote losing cross-links when a node is superseded
```

Reference an issue number where relevant (`Fixes #123`).

---

## Opening a pull request

- Push your branch to your fork and open a PR against `kritird/OpenMnemex` `main`.
- In the description, explain **what** changed and **why**, how you tested it, and — if
  relevant — confirm you kept the MCP and plugin surfaces in sync.
- Make sure `pytest` passes.
- Keep PRs focused: one logical change per PR is much easier to review than a grab-bag.

A maintainer will review, may ask for adjustments, and merge once it looks good. Please be
patient — this is a small project.

---

## Reporting security issues

If you find a security or data-integrity problem (for example, something that could silently
corrupt or leak a user's memory graph), please **do not** open a public issue. Instead, email
the maintainer directly so it can be fixed before disclosure.

---

## License

By contributing, you agree that your contributions will be licensed under the project's
[MIT License](LICENSE), the same terms that cover the rest of OpenMnemex.

---

Thank you for helping make agent memory better. Every issue, doc fix, and test counts. 💛
