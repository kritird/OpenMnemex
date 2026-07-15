"""mnx_procedures.py — single-source builder for the judgment procedures (plan v2 §5.4).

Four procedures (read, capture, promote, curate) are authored once as ``templates/procedures/
*.core.md`` — shared judgment prose with ``{{CALL:x}}`` / ``{{PROC:x}}`` / ``{{BLOCK:x}}`` /
``{{INCLUDE:x}}`` placeholders marking the spots that differ by target — and rendered into:

  * the **claude** target — ``skills/mnx-{read,capture,promote}/SKILL.md`` (curate has no
    standalone Claude skill; its core is inlined into capture's via ``{{INCLUDE:curate}}``).
    This target must reproduce today's hand-written SKILL.md byte-for-byte (the Phase 3
    migration's "no behavioral diff" bar) — every fragment under ``fragments/*/*.claude.md``
    was extracted verbatim from the committed files, never retyped.
  * the **mcp** target — prompt bodies registered in ``mnx_mcp.py`` (commit 3b), phrased in
    terms of MCP tool calls instead of ``python3 scripts/mnx_*.py`` invocations.

``render_digest`` is a separate, simpler mechanism: a standalone 2-4 line blurb per procedure
(``fragments/_digests/<name>.md``), embedded in tool descriptions (commit 3c) and, later, the
Phase-5 instruction-file blocks — not a placeholder-substitution render of the core file (a
full document doesn't compress into 2-4 lines by swapping a few tokens; it needs its own
hand-written summary, single-sourced so 3c can't drift from the procedure without a human
editing both).

``build`` (the CLI) regenerates the three SKILL.md files in place; ``--check`` regenerates into
memory and diffs against the committed files without writing (used by the drift guard,
commit 3d / ``tests/test_procedure_sync.py``).

Stdlib only — no Jinja, no dependency beyond what the rest of the engine already needs.
"""
from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path
from typing import Optional

import mnx_common

PLACEHOLDER_RE = re.compile(r"\{\{(CALL|PROC|BLOCK|INCLUDE):([A-Za-z0-9_]+)\}\}")

PROCEDURES = ("read", "capture", "promote")  # each maps to a skills/mnx-<name>/SKILL.md
ALL_CORES = ("read", "capture", "promote", "curate")  # curate has no standalone skill file
TARGETS = ("claude", "mcp")  # placeholder-substitution targets; "digest" is a separate,
                              # standalone 2-4 line blurb per procedure (see render_digest)


def _procedures_dir() -> Path:
    return mnx_common.plugin_root().parent / "templates" / "procedures"


def _core_path(name: str) -> Path:
    return _procedures_dir() / f"{name}.core.md"


def _fragment_path(proc: str, key: str, target: str) -> Path:
    return _procedures_dir() / "fragments" / proc / f"{key}.{target}.md"


def _fragment(proc: str, key: str, target: str) -> str:
    path = _fragment_path(proc, key, target)
    if not path.is_file():
        raise FileNotFoundError(
            f"missing procedure fragment: {path} "
            f"(target={target!r} key={key!r} proc={proc!r})"
        )
    return path.read_text(encoding="utf-8")


def _split_frontmatter(text: str) -> tuple[str, str]:
    """(frontmatter_block_incl_trailing_blank, body) — frontmatter_block is '' if none."""
    if not text.startswith("---\n"):
        return "", text
    end = text.index("\n---\n", 4) + len("\n---\n")
    return text[:end], text[end:]


def render_core(name: str, target: str, *, include_frontmatter: bool) -> str:
    """Render one ``<name>.core.md`` for ``target``, resolving placeholders (recursively for
    ``{{INCLUDE:x}}``). ``include_frontmatter`` controls whether the leading ``---`` block is
    kept (claude target only — MCP/digest renders are body-only)."""
    text = _core_path(name).read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)

    def replace(m: "re.Match[str]") -> str:
        kind, key = m.group(1), m.group(2)
        if kind == "INCLUDE":
            return render_core(key, target, include_frontmatter=False)
        return _fragment(name, key, target)

    rendered_body = PLACEHOLDER_RE.sub(replace, body)
    if not include_frontmatter:
        return rendered_body
    rendered_frontmatter = PLACEHOLDER_RE.sub(replace, frontmatter)
    return rendered_frontmatter + rendered_body


def render_skill(name: str) -> str:
    """The claude-target SKILL.md body for ``name`` (one of PROCEDURES)."""
    return render_core(name, "claude", include_frontmatter=True)


def render_mcp_prompt(name: str) -> str:
    """The mcp-target prompt body for ``name`` (one of ALL_CORES, e.g. 'curate' too)."""
    return render_core(name, "mcp", include_frontmatter=False)


def _digest_path(name: str) -> Path:
    return _procedures_dir() / "fragments" / "_digests" / f"{name}.md"


def render_digest(name: str) -> str:
    """The compact 2-4 line digest for ``name`` (read/capture/promote), embedded in a tool
    description (commit 3c). Unlike ``render_skill``/``render_mcp_prompt`` this is not a
    placeholder-substitution render of the core file — it is its own short, hand-authored
    blurb, single-sourced from ``fragments/_digests/<name>.md`` so 3c can't drift from the
    procedure it summarizes without a human deliberately editing both."""
    return _digest_path(name).read_text(encoding="utf-8")


_SKILL_PATH = {
    "read": "skills/mnx-read/SKILL.md",
    "capture": "skills/mnx-capture/SKILL.md",
    "promote": "skills/mnx-promote/SKILL.md",
}


def _repo_root() -> Path:
    return mnx_common.plugin_root().parent


def skill_target_path(name: str) -> Path:
    return _repo_root() / _SKILL_PATH[name]


def check(names: Optional[list[str]] = None) -> dict[str, object]:
    """Regenerate the claude-target SKILL.md files in memory and diff against what's on disk.

    Returns ``{"ok": bool, "mismatches": [{"name", "path", "diff"}]}``; never writes."""
    mismatches = []
    for name in names or PROCEDURES:
        rendered = render_skill(name)
        path = skill_target_path(name)
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if rendered != current:
            diff = "".join(difflib.unified_diff(
                current.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=str(path), tofile=f"<generated {name}>",
            ))
            mismatches.append({"name": name, "path": str(path), "diff": diff})
    return {"ok": not mismatches, "mismatches": mismatches}


def build(names: Optional[list[str]] = None) -> dict[str, object]:
    """Regenerate and write the claude-target SKILL.md files. Returns {"written": [paths]}."""
    written = []
    for name in names or PROCEDURES:
        rendered = render_skill(name)
        path = skill_target_path(name)
        path.write_text(rendered, encoding="utf-8")
        written.append(str(path))
    return {"written": written}


_USAGE = [
    "mnx_procedures.py build                 — regenerate skills/mnx-{read,capture,promote}/"
    "SKILL.md from the core files",
    "mnx_procedures.py build --check          — diff only, write nothing; nonzero exit on drift",
    "mnx_procedures.py build [--check] <name>...  — restrict to specific procedures "
    "(read/capture/promote)",
]
_FLAGS = {"--check": False}


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE, _FLAGS)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else "build"
    try:
        if cmd != "build":
            return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
        rest = argv[2:]
        check_only = "--check" in rest
        names = [a for a in rest if a != "--check"] or None
        if names:
            unknown = [n for n in names if n not in PROCEDURES]
            if unknown:
                return mnx_common.emit(
                    {"error": f"unknown procedure(s): {unknown}", "usage": _USAGE}, ok=False)
        if check_only:
            result = check(names)
            if not result["ok"]:
                for m in result["mismatches"]:
                    print(m["diff"], file=sys.stderr)
            return mnx_common.emit(result, ok=result["ok"])
        return mnx_common.emit(build(names))
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
