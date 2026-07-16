"""mnx_install.py — the per-agent installer (plan v2 §6.3 / §7 Phase 5 / §8).

``openmnemex install --agent {claude-code,opencode,gemini-cli,codex,copilot,cursor}
[--project|--user] [--uninstall] [--check] [--dry-run] [--yes] [--pin-graph]``

Emits, per target agent: an MCP server entry (JSON or TOML) pointing at ``uvx openmnemex-mcp``,
plus (except claude-code, which already has the richer plugin path) a marker-delimited
instruction-file block (§5.5) built from the SAME digests the MCP tool descriptions use
(``mnx_procedures.render_digest``) — one prose source, no hand-retyped copy to drift (risk R3).

Properties:
  * idempotent — re-running with identical inputs writes nothing (``changed: False`` per file);
  * never clobbers unrelated content — JSON entries are merged into the parsed document (existing
    keys/values preserved; the file is re-serialized at 2-space-indent, so pre-existing
    non-canonical *whitespace* may normalize — this file's docstring on ``merge_json_server``
    calls that out, it is a deliberate scope cut, not an oversight); TOML tables and the Markdown
    block are located and replaced by pure text-splice, so bytes outside them are untouched;
  * ``--uninstall`` removes exactly what install would have written, nothing more;
  * ``--dry-run`` prints the plan without writing; interactive runs ask before writing, ``--yes``
    skips the prompt (for CI / scripted installs).

Each agent adapter is isolated (risk R5: upstream config formats drift independently) and
snapshot-tested in ``tests/test_install_emit.py``.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import mnx_common
import mnx_procedures

MCP_COMMAND = "uvx"
MCP_ARGS = ["openmnemex-mcp"]
SERVER_KEY = "mnemex"

MD_BEGIN = "<!-- openmnemex:begin (generated; `openmnemex install --agent X` updates this) -->"
MD_END = "<!-- openmnemex:end -->"

_ENV_KEYS = ("MNEMEX_GRAPH_PATH", "MNEMEX_GRAPH_REMOTE", "MNEMEX_HOME")


class InstallError(Exception):
    """A precondition the installer cannot safely work around (bad JSON, unsupported scope)."""


# --- shared text-splice editors -------------------------------------------------------
#
# JSON: parse -> merge into the dict -> re-serialize at a fixed 2-space indent. This preserves
# every pre-existing key/value; it does NOT preserve arbitrary pre-existing *formatting* (an
# already-2-space-indent file — what our own installer produces — round-trips byte-for-byte, so
# idempotent re-runs and our own snapshot fixtures are exact; a hand-formatted file with different
# spacing gets normalized). A true byte-preserving JSON patch needs a position-tracking parser;
# not worth it for six known, simple, flat server-entry shapes.

def _load_json_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallError(f"{path}: not valid JSON ({exc}); fix or remove it, then retry")
    if not isinstance(data, dict):
        raise InstallError(f"{path}: expected a JSON object at the top level")
    return data


def merge_json_server(path: Path, top_key: str, entry: dict) -> tuple[str, bool]:
    """Set ``data[top_key][SERVER_KEY] = entry`` in the document at ``path``. Returns
    ``(new_text, changed)``; ``changed`` is False when the entry already matches (idempotent)."""
    data = _load_json_object(path)
    section = data.get(top_key)
    if section is None:
        section = {}
        data[top_key] = section
    elif not isinstance(section, dict):
        raise InstallError(f"{path}: {top_key!r} is not an object")
    changed = section.get(SERVER_KEY) != entry
    section[SERVER_KEY] = entry
    return json.dumps(data, indent=2) + "\n", changed


def remove_json_server(path: Path, top_key: str) -> tuple[Optional[str], bool]:
    """Drop ``data[top_key][SERVER_KEY]``, and ``top_key`` itself if left empty. Returns
    ``(new_text_or_None, changed)`` — ``None`` means "nothing to remove, file untouched"."""
    if not path.is_file():
        return None, False
    data = _load_json_object(path)
    section = data.get(top_key)
    if not isinstance(section, dict) or SERVER_KEY not in section:
        return None, False
    del section[SERVER_KEY]
    if not section:
        del data[top_key]
    return json.dumps(data, indent=2) + "\n", True


# TOML: our tables are always flat (`command`/`args`/…), so a pure line-based text-splice keyed
# on the `[table.header]` line is exact and leaves every other byte in the file untouched —
# unlike JSON there is no need to parse the rest of the document at all.

_TOML_HEADER_RE = re.compile(r"(?m)^\[(?P<name>[^\]\r\n]+)\][ \t]*$")


def _toml_table_body(command: str, args: list[str]) -> str:
    args_str = ", ".join(json.dumps(a) for a in args)
    return f"command = {json.dumps(command)}\nargs = [{args_str}]\n"


def merge_toml_table(path: Path, table: str, body: str) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    block = f"[{table}]\n{body}"
    matches = list(_TOML_HEADER_RE.finditer(text))
    for i, m in enumerate(matches):
        if m.group("name") != table:
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        existing = text[start:end]
        if existing.rstrip("\n") == block.rstrip("\n"):
            return text, False
        return text[:start] + block.rstrip("\n") + "\n" + text[end:], True
    if not text:
        return block, True
    sep = "\n" if text.endswith("\n") else "\n\n"
    return text + sep + block, True


def remove_toml_table(path: Path, table: str) -> tuple[Optional[str], bool]:
    if not path.is_file():
        return None, False
    text = path.read_text(encoding="utf-8")
    matches = list(_TOML_HEADER_RE.finditer(text))
    for i, m in enumerate(matches):
        if m.group("name") != table:
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        return text[:start] + text[end:], True
    return None, False


# Markdown: a single marker-delimited block, pure text-splice — everything outside the markers
# is untouched byte-for-byte, matching every other project's convention for generated blocks.

_MD_BLOCK_RE = re.compile(re.escape(MD_BEGIN) + r".*?" + re.escape(MD_END) + r"\n?", re.S)


def merge_md_block(path: Path, body: str) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    block = f"{MD_BEGIN}\n{body.strip()}\n{MD_END}\n"
    if _MD_BLOCK_RE.search(text):
        new_text = _MD_BLOCK_RE.sub(block, text, count=1)
        return new_text, new_text != text
    if not text.strip():
        return block, True
    sep = "\n" if text.endswith("\n\n") else ("\n\n" if text.endswith("\n") else "\n\n")
    return text + sep + block, True


def remove_md_block(path: Path) -> tuple[Optional[str], bool]:
    if not path.is_file():
        return None, False
    text = path.read_text(encoding="utf-8")
    if not _MD_BLOCK_RE.search(text):
        return None, False
    return _MD_BLOCK_RE.sub("", text), True


# --- the §5.5 instruction block, generated from the single-sourced digests -------------

def instruction_block_body() -> str:
    read = mnx_procedures.render_digest("read").strip()
    capture = mnx_procedures.render_digest("capture").strip()
    promote = mnx_procedures.render_digest("promote").strip()
    return (
        "## Memory (OpenMnemex)\n\n"
        "Tool names are prefixed by this host (e.g. `mnemex.read_frontier`). If your host "
        "supports MCP prompts, run `read-procedure` / `capture-procedure` / `promote-procedure` "
        "for the full judgment steps — the digests below are the compact version. See "
        "`LIMITATIONS.md` in the graph for what differs from the Claude plugin (no session-start "
        "consent primer, no auto-capture nudge on most hosts).\n\n"
        f"**Read.** {read}\n\n"
        f"**Capture.** {capture}\n\n"
        f"**Promote.** {promote}\n"
    )


# --- install plan -----------------------------------------------------------------------

@dataclass
class FileChange:
    path: Path
    new_text: Optional[str]   # None => this change removes the file entirely (uninstall only)
    changed: bool
    label: str
    binary_src: Optional[Path] = None  # set for the OpenCode plugin file copy


@dataclass
class InstallPlan:
    agent: str
    scope: str
    changes: list[FileChange] = field(default_factory=list)
    shell_actions: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unsupported: Optional[str] = None


def _pin_env(binding_root: Optional[Path]) -> Optional[dict]:
    if binding_root is None:
        return None
    import os as _os
    import mnx_binding
    b = mnx_binding.resolve(str(binding_root))
    if b is None:
        return None
    env: dict[str, str] = {}
    if b.remote:
        env["MNEMEX_GRAPH_REMOTE"] = b.remote
    elif b.local_path:
        env["MNEMEX_GRAPH_PATH"] = _os.path.abspath(_os.path.expanduser(b.local_path))
    return env or None


def _entry_mcpservers(pin_env: Optional[dict]) -> dict:
    entry: dict[str, Any] = {"command": MCP_COMMAND, "args": list(MCP_ARGS)}
    if pin_env:
        entry["env"] = pin_env
    return entry


def _entry_opencode(pin_env: Optional[dict]) -> dict:
    entry: dict[str, Any] = {"type": "local", "command": [MCP_COMMAND, *MCP_ARGS], "enabled": True}
    if pin_env:
        entry["environment"] = pin_env
    return entry


def _entry_copilot(pin_env: Optional[dict]) -> dict:
    entry: dict[str, Any] = {"type": "stdio", "command": MCP_COMMAND, "args": list(MCP_ARGS)}
    if pin_env:
        entry["env"] = pin_env
    return entry


def _json_change(path: Path, top_key: str, entry: dict, *, uninstall: bool, label: str) -> FileChange:
    if uninstall:
        new_text, changed = remove_json_server(path, top_key)
    else:
        new_text, changed = merge_json_server(path, top_key, entry)
    return FileChange(path=path, new_text=new_text, changed=changed, label=label)


def _md_change(path: Path, *, uninstall: bool, label: str) -> FileChange:
    if uninstall:
        new_text, changed = remove_md_block(path)
    else:
        new_text, changed = merge_md_block(path, instruction_block_body())
    return FileChange(path=path, new_text=new_text, changed=changed, label=label)


# --- per-agent adapters (isolated per R5: each owns its own path + shape) --------------

def _adapt_claude_code(scope: str, project_root: Path, pin_env: Optional[dict],
                        uninstall: bool) -> InstallPlan:
    plan = InstallPlan(agent="claude-code", scope=scope)
    if scope == "project":
        entry = _entry_mcpservers(pin_env)
        plan.changes.append(_json_change(project_root / ".mcp.json", "mcpServers", entry,
                                          uninstall=uninstall, label="MCP server entry (.mcp.json)"))
    else:  # user — Claude Code has no documented static user-config path; use its own CLI.
        cmd = (["claude", "mcp", "remove", SERVER_KEY] if uninstall else
               ["claude", "mcp", "add", SERVER_KEY, "--", MCP_COMMAND, *MCP_ARGS])
        plan.shell_actions.append(cmd)
    plan.notes.append(
        "claude-code already has the richer plugin path (7 auto-hooks, Full tier); this MCP "
        "entry is an alternative, not a replacement — no instruction-file block is written here.")
    return plan


def _adapt_opencode(scope: str, project_root: Path, pin_env: Optional[dict],
                     uninstall: bool) -> InstallPlan:
    plan = InstallPlan(agent="opencode", scope=scope)
    cfg_path = (project_root / "opencode.json" if scope == "project"
                else Path.home() / ".config" / "opencode" / "opencode.json")
    entry = _entry_opencode(pin_env)
    plan.changes.append(_json_change(cfg_path, "mcp", entry, uninstall=uninstall,
                                      label=f"MCP server entry ({cfg_path.name})"))
    plan.changes.append(_md_change(project_root / "AGENTS.md", uninstall=uninstall,
                                    label="instruction block (AGENTS.md)"))
    if scope == "project":
        plugin_dst = project_root / ".opencode" / "plugin" / "mnemex.ts"
        plugin_src = mnx_common.plugin_root().parent / "integrations" / "opencode" / "mnemex.ts"
        if uninstall:
            if plugin_dst.is_file():
                plan.changes.append(FileChange(path=plugin_dst, new_text=None, changed=True,
                                                label="OpenCode hook plugin (removed)"))
        elif plugin_src.is_file():
            new_text = plugin_src.read_text(encoding="utf-8")
            changed = not plugin_dst.is_file() or plugin_dst.read_text(encoding="utf-8") != new_text
            plan.changes.append(FileChange(path=plugin_dst, new_text=new_text, changed=changed,
                                            label="OpenCode hook plugin (.opencode/plugin/mnemex.ts)",
                                            binary_src=plugin_src))
        else:
            plan.notes.append(
                f"OpenCode hook plugin source not found at {plugin_src} (only available from a "
                "repo checkout until commit 5c ships it as package data) — skipped; the MCP "
                "entry + AGENTS.md block above still give read/capture/promote on this host.")
    return plan


def _adapt_gemini_cli(scope: str, project_root: Path, pin_env: Optional[dict],
                       uninstall: bool) -> InstallPlan:
    plan = InstallPlan(agent="gemini-cli", scope=scope)
    cfg_path = (project_root / ".gemini" / "settings.json" if scope == "project"
                else Path.home() / ".gemini" / "settings.json")
    entry = _entry_mcpservers(pin_env)
    plan.changes.append(_json_change(cfg_path, "mcpServers", entry, uninstall=uninstall,
                                      label=f"MCP server entry ({cfg_path})"))
    plan.changes.append(_md_change(project_root / "GEMINI.md", uninstall=uninstall,
                                    label="instruction block (GEMINI.md)"))
    return plan


def _adapt_codex(scope: str, project_root: Path, pin_env: Optional[dict],
                  uninstall: bool) -> InstallPlan:
    plan = InstallPlan(agent="codex", scope=scope)
    if pin_env:
        plan.notes.append(
            "--pin-graph is not implemented for codex: config.toml's [mcp_servers.*] pins "
            "*names* of host env vars to forward (env_vars = [...]), not literal values, and "
            "that indirection isn't verified against current Codex docs — pin manually if needed.")
    cfg_path = (project_root / ".codex" / "config.toml" if scope == "project"
                else Path.home() / ".codex" / "config.toml")
    table = f"mcp_servers.{SERVER_KEY}"
    if uninstall:
        new_text, changed = remove_toml_table(cfg_path, table)
    else:
        new_text, changed = merge_toml_table(cfg_path, table, _toml_table_body(MCP_COMMAND, MCP_ARGS))
    plan.changes.append(FileChange(path=cfg_path, new_text=new_text, changed=changed,
                                    label=f"MCP server entry ({cfg_path})"))
    plan.changes.append(_md_change(project_root / "AGENTS.md", uninstall=uninstall,
                                    label="instruction block (AGENTS.md)"))
    if scope == "project":
        plan.notes.append("Project-scoped .codex/config.toml is honored by Codex only for "
                           "trusted projects (upstream docs) — trust the project if the entry "
                           "doesn't take effect.")
    return plan


def _adapt_copilot(scope: str, project_root: Path, pin_env: Optional[dict],
                    uninstall: bool) -> InstallPlan:
    plan = InstallPlan(agent="copilot", scope=scope)
    if scope == "user":
        plan.unsupported = (
            "copilot has no fixed user-config file path — VS Code's user MCP config is edited "
            "via the 'MCP: Open User Configuration' command palette entry, not a static file. "
            "Use --project (workspace .vscode/mcp.json), or add the server through that command.")
        return plan
    entry = _entry_copilot(pin_env)
    plan.changes.append(_json_change(project_root / ".vscode" / "mcp.json", "servers", entry,
                                      uninstall=uninstall, label="MCP server entry (.vscode/mcp.json)"))
    plan.changes.append(_md_change(project_root / ".github" / "copilot-instructions.md",
                                    uninstall=uninstall,
                                    label="instruction block (.github/copilot-instructions.md)"))
    return plan


def _adapt_cursor(scope: str, project_root: Path, pin_env: Optional[dict],
                   uninstall: bool) -> InstallPlan:
    plan = InstallPlan(agent="cursor", scope=scope)
    cfg_path = (project_root / ".cursor" / "mcp.json" if scope == "project"
                else Path.home() / ".cursor" / "mcp.json")
    entry = _entry_mcpservers(pin_env)
    plan.changes.append(_json_change(cfg_path, "mcpServers", entry, uninstall=uninstall,
                                      label=f"MCP server entry ({cfg_path})"))
    if scope == "project":
        rule_path = project_root / ".cursor" / "rules" / "openmnemex.mdc"
        if uninstall:
            if rule_path.is_file():
                plan.changes.append(FileChange(path=rule_path, new_text=None, changed=True,
                                                label="rule file (removed)"))
        else:
            body = ("---\ndescription: OpenMnemex memory tool usage\nalwaysApply: true\n---\n\n"
                    + instruction_block_body().replace(MD_BEGIN + "\n", "").replace(
                        "\n" + MD_END + "\n", "\n"))
            changed = not rule_path.is_file() or rule_path.read_text(encoding="utf-8") != body
            plan.changes.append(FileChange(path=rule_path, new_text=body, changed=changed,
                                            label="rule file (.cursor/rules/openmnemex.mdc)"))
    else:
        plan.notes.append("cursor rules (.cursor/rules/*.mdc) are project-scoped only; "
                           "--user installs the MCP entry alone.")
    return plan


_ADAPTERS: dict[str, Callable[[str, Path, Optional[dict], bool], InstallPlan]] = {
    "claude-code": _adapt_claude_code,
    "opencode": _adapt_opencode,
    "gemini-cli": _adapt_gemini_cli,
    "codex": _adapt_codex,
    "copilot": _adapt_copilot,
    "cursor": _adapt_cursor,
}

# scopes each agent actually supports as a *file-writing* target (drives default + validation)
_DEFAULT_SCOPE = {
    "claude-code": "project",
    "opencode": "project",
    "gemini-cli": "project",
    "codex": "user",
    "copilot": "project",
    "cursor": "project",
}


def build_plan(agent: str, *, scope: Optional[str] = None, project_root: Optional[Path] = None,
                pin_graph: bool = False, uninstall: bool = False) -> InstallPlan:
    if agent not in _ADAPTERS:
        raise InstallError(f"unknown agent {agent!r}; choose one of {sorted(_ADAPTERS)}")
    root = (project_root or Path.cwd()).resolve()
    resolved_scope = scope or _DEFAULT_SCOPE[agent]
    if resolved_scope not in ("project", "user"):
        raise InstallError(f"scope must be 'project' or 'user', got {resolved_scope!r}")
    pin_env = _pin_env(root) if pin_graph else None
    return _ADAPTERS[agent](resolved_scope, root, pin_env, uninstall)


# --- applying a plan ---------------------------------------------------------------------

def _diff_summary(change: FileChange) -> str:
    if change.new_text is None:
        return f"remove {change.path}"
    if not change.changed:
        return f"unchanged {change.path}"
    return f"write {change.path}"


def apply_plan(plan: InstallPlan, *, dry_run: bool = False, yes: bool = False) -> dict[str, Any]:
    if plan.unsupported:
        return {"ok": False, "agent": plan.agent, "scope": plan.scope,
                "error": plan.unsupported}
    to_write = [c for c in plan.changes if c.changed]
    result: dict[str, Any] = {
        "ok": True, "agent": plan.agent, "scope": plan.scope,
        "changes": [{"path": str(c.path), "label": c.label, "changed": c.changed,
                     "action": _diff_summary(c)} for c in plan.changes],
        "shell_actions": [" ".join(a) for a in plan.shell_actions],
        "notes": list(plan.notes),
    }
    if dry_run:
        result["dry_run"] = True
        return result
    if (to_write or plan.shell_actions) and not yes:
        result["ok"] = False
        result["error"] = "confirmation required: pass yes=True (CLI: --yes) to write"
        return result
    written = []
    for change in to_write:
        if change.new_text is None:
            if change.path.is_file():
                change.path.unlink()
                written.append(str(change.path))
            continue
        change.path.parent.mkdir(parents=True, exist_ok=True)
        change.path.write_text(change.new_text, encoding="utf-8")
        written.append(str(change.path))
    result["written"] = written
    ran = []
    for cmd in plan.shell_actions:
        if shutil.which(cmd[0]) is None:
            result["ok"] = False
            result.setdefault("shell_errors", []).append(f"{cmd[0]!r} not found on PATH")
            continue
        proc = subprocess.run(cmd, capture_output=True, text=True)
        ran.append({"cmd": cmd, "returncode": proc.returncode, "stderr": proc.stderr.strip()})
        if proc.returncode != 0:
            result["ok"] = False
    if ran:
        result["ran"] = ran
    return result


def install(agent: str, *, scope: Optional[str] = None, project_root: Optional[str] = None,
            uninstall: bool = False, check: bool = False, dry_run: bool = False,
            yes: bool = False, pin_graph: bool = False) -> dict[str, Any]:
    """The importable delegate behind ``openmnemex install`` — build a plan and (unless
    ``dry_run``) apply it. ``check`` short-circuits into environment verification instead."""
    root = Path(project_root).resolve() if project_root else Path.cwd()
    if check:
        return check_install(root)
    plan = build_plan(agent, scope=scope, project_root=root, pin_graph=pin_graph,
                       uninstall=uninstall)
    return apply_plan(plan, dry_run=dry_run, yes=yes)


def check_install(project_root: Path) -> dict[str, Any]:
    """``--check``: resolve the binding, list MCP tools if the SDK is available, verify the
    merge driver — a live smoke test, never starts a persistent server."""
    result: dict[str, Any] = {"ok": True}
    import mnx_binding
    binding = mnx_binding.resolve(str(project_root))
    result["binding"] = {"resolved": binding is not None}
    if binding is None:
        result["ok"] = False
        result["binding"]["hint"] = "run 'openmnemex install' from inside a bound project, or " \
                                     "run mnx_init.py init first"

    import mnx_mcp
    info = mnx_mcp.info()
    result["mcp"] = info
    if mnx_mcp.sdk_available():
        try:
            result["tools"] = mnx_mcp.list_tool_names()
        except Exception as exc:  # pragma: no cover - defensive; SDK internals may vary
            result["ok"] = False
            result["mcp_error"] = str(exc)
    else:
        result["ok"] = False

    import mnx_regen
    try:
        result["merge_driver_installed"] = mnx_regen.is_installed(str(project_root))
        if not result["merge_driver_installed"]:
            result["ok"] = False
    except Exception as exc:  # pragma: no cover - not a git repo, etc.
        result["merge_driver_installed"] = False
        result["merge_driver_error"] = str(exc)
        result["ok"] = False
    return result


# --- cli -----------------------------------------------------------------------------

_USAGE = [
    "mnx_install.py install --agent {claude-code,opencode,gemini-cli,codex,copilot,cursor}"
    " [--project|--user] [--uninstall] [--check] [--dry-run] [--yes] [--pin-graph]"
    "  — emit/remove the MCP entry + instruction-file block for one target agent",
]
_FLAGS = {"--agent": True, "--project": False, "--user": False, "--uninstall": False,
          "--check": False, "--dry-run": False, "--yes": False, "--pin-graph": False}


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE, _FLAGS)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else None
    if cmd != "install":
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}", "usage": _USAGE}, ok=False)
    rest = argv[2:]
    agent = None
    scope = None
    uninstall = check = dry_run = yes = pin_graph = False
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--agent":
            i += 1
            agent = rest[i] if i < len(rest) else None
        elif tok == "--project":
            scope = "project"
        elif tok == "--user":
            scope = "user"
        elif tok == "--uninstall":
            uninstall = True
        elif tok == "--check":
            check = True
        elif tok == "--dry-run":
            dry_run = True
        elif tok == "--yes":
            yes = True
        elif tok == "--pin-graph":
            pin_graph = True
        i += 1
    if not agent and not check:
        return mnx_common.emit({"error": "--agent is required", "usage": _USAGE}, ok=False)
    try:
        result = install(agent or "claude-code", scope=scope, uninstall=uninstall, check=check,
                          dry_run=dry_run, yes=yes, pin_graph=pin_graph)
        return mnx_common.emit(result, ok=result.get("ok", True))
    except InstallError as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


def main() -> int:
    """Console entry point (pyproject [project.scripts] openmnemex)."""
    return _main(sys.argv)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
