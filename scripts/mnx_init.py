"""mnx_init.py — deterministic graph scaffolding + first-contact init.

The *mechanical* half of the mnx-init SKILL, made importable so the MCP ``init_graph``
tool (plan v2 §5.3, Phase 1 commit 1b) and the SKILL can share one scaffolder. The
*judgment* half — choosing create/bind/user-default mode, remote-vs-local, committing the
binding as shared or personal, picking the half-life horizons — stays with the host; this
module takes a decided ``{root|remote, team, org}`` and lays down a **doctor-clean graph on
day one** (mnx-init SKILL step 5 + the F1/F2 day-one-stamp fix cycle).

Nothing here asks the user anything or writes a binding file: it only writes graph truth +
derived skeletons + runs the day-one merge-driver / config-stamp / doctor steps. Choosing
*which* graph a project resolves to is the installer's / host's job (env pins or ``.mnemex.md``).

CLI (thin; the SKILL keeps driving the interactive flow):
    scaffold <root> [--team <name>] [--org <name>]   — lay down an empty graph skeleton
    init --path <dir> | --remote <url> [--team ...] [--org ...]
                                                     — scaffold + merge-driver + stamp + doctor

Dependencies: Python 3.9+ stdlib + PyYAML (via the other mnx_* helpers). See docs/binding-and-graph-sync.md.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import mnx_binding
import mnx_common
import mnx_config
import mnx_doctor
import mnx_phonebook
import mnx_regen

DEFAULT_TEAM = "team-core"

# Files whose presence means "already a graph here" — scaffold never overwrites these.
_CONFIG_FILE = "mnemex.config.md"


# --- template sources (single-sourced with the SKILL's hand path) -------------

def config_defaults() -> str:
    """The stock ``mnemex.config.md`` (front-matter + prose) a fresh graph is seeded with."""
    return mnx_common.config_dir().joinpath(_CONFIG_FILE).read_text(encoding="utf-8")


def _template(name: str) -> str:
    return mnx_common.templates_dir().joinpath(name).read_text(encoding="utf-8")


def _router_index(title: str, description: str, children: list[tuple[str, str]]) -> str:
    """An ORG or TEAM router index — lists child folders, never nodes (same shape as the
    generated routers so a subsequent regen/doctor pass agrees with what we wrote)."""
    lines = [f"# {title} — index", f"> {description}", "", "## Children"]
    lines += [f"- {name}/ — {desc}" for name, desc in children]
    lines += [""]
    return "\n".join(lines)


def _registry_header(key: str) -> str:
    return (f"# registry: {key}   (append-only — do not edit by hand)\n"
            "# columns: id    ts(UTC ISO-8601)    role(contributed|consulted|traversed|flag)    weight\n")


def _crosslinks_header(team: str) -> str:
    return f"# cross-links: {team}   (GENERATED — inter-cluster edges)\n"


# --- scaffolding --------------------------------------------------------------

def scaffold(root: str | Path, team: str = DEFAULT_TEAM,
             org: Optional[str] = None, team_description: Optional[str] = None) -> dict[str, Any]:
    """Write an empty-but-valid graph skeleton at ``root``. Non-destructive: an existing file
    is left untouched and reported under ``skipped``.

    Lays down: ``mnemex.config.md`` (from stock defaults), ``.mnemex/`` state dir,
    ``.gitignore`` + ``.gitattributes`` (so stranded locks never commit and the merge driver
    can bind), the org router ``index.md``, and a first ``team-*/`` skeleton (router index,
    append-only registry, generated cross-links). No domains/nodes — that is the author's job.
    """
    root = Path(root).expanduser()
    if not team.startswith("team-"):
        raise ValueError(f"team name must start with 'team-': {team!r}")
    org_name = org or root.name or "knowledge-graph"
    team_desc = team_description or "First team — describe its charter."

    created: list[str] = []
    skipped: list[str] = []

    def _write(rel: str, content: str) -> None:
        target = root / rel
        if target.exists():
            skipped.append(rel)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(rel)

    root.mkdir(parents=True, exist_ok=True)
    (root / mnx_common.STATE_DIRNAME).mkdir(exist_ok=True)

    _write(_CONFIG_FILE, config_defaults())
    _write(".gitignore", _template("gitignore.template"))
    _write(".gitattributes", _template("gitattributes.template"))
    _write("index.md", _router_index(org_name, "Organization knowledge graph.",
                                     [(team, team_desc)]))
    _write(f"{team}/index.md", _router_index(team, team_desc, []))
    _write(f"{team}/registry.md", _registry_header(team))
    _write(f"{team}/cross-links.md", _crosslinks_header(team))

    # The org router just written above is a day-one placeholder in the wrong SHAPE for this
    # file: every regen (mnx_doctor.fix, mnx_promote.apply, mnx_regen, mnx_compact) rewrites
    # index.md as the coarse team/domains/summary TABLE via mnx_phonebook.regenerate_org —
    # never the `> description` + `## Children` router shape `_router_index` just wrote. Left
    # unreconciled, a graph that has never been promoted/doctor-fixed has an org index.md that
    # `mnx_read.frontier` parses differently than a post-regen graph does. Normalize
    # immediately so day-one and post-regen graphs are identical from the start — but only
    # when THIS call actually created (not skipped) the org index, so scaffold stays
    # non-destructive toward a pre-existing org whose index.md a human may have hand-edited.
    if "index.md" in created:
        mnx_phonebook.regenerate_org(str(root))

    return {"root": str(root), "org": org_name, "team": team,
            "created": created, "skipped": skipped}


# --- first-contact init (scaffold + day-one clean) ----------------------------

def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _finish_clean(root: Path) -> dict[str, Any]:
    """The mnx-init SKILL step-5 tail, in code: register the merge driver on a git clone,
    stamp config so drift/overdue detection has a day-one baseline, then doctor-check.
    A freshly scaffolded graph is expected E0 (W0 too, once the driver is registered)."""
    result: dict[str, Any] = {}
    is_git = (root / ".git").exists()
    if is_git and not mnx_regen.is_installed(str(root)):
        try:
            mnx_regen.install(str(root))
            result["merge_driver"] = "installed"
        except Exception as exc:  # non-fatal: a plain checkout still works, doctor will warn
            result["merge_driver"] = f"install-failed: {exc}"
    elif is_git:
        result["merge_driver"] = "already-installed"
    else:
        result["merge_driver"] = "n/a (not a git repo)"

    cfg = mnx_config.load(str(root))
    mnx_config.stamp(str(root), cfg)  # inv-15 baseline (root scope is what doctor checks)
    result["config_stamped"] = True

    report = mnx_doctor.check(str(root))
    result["doctor"] = report.get("counts", {})
    result["doctor_clean"] = report.get("ok", False)
    if not report.get("ok", False):
        result["doctor_findings"] = report.get("findings", [])
    return result


def init_local(path: str | Path, team: str = DEFAULT_TEAM,
               org: Optional[str] = None, team_description: Optional[str] = None) -> dict[str, Any]:
    """Scaffold a local-folder graph (git repo or plain folder) and leave it day-one clean."""
    root = Path(path).expanduser()
    already = (root / _CONFIG_FILE).is_file()
    scaffold_result = scaffold(root, team=team, org=org, team_description=team_description)
    kind = "git-local" if (root / ".git").exists() else "plain-local"
    out = {"action": "already-graph" if already else "scaffolded",
           "kind": kind, "graph_root": str(root.resolve()),
           "scaffold": scaffold_result}
    out.update(_finish_clean(root))
    return out


def init_remote(remote: str, team: str = DEFAULT_TEAM,
                org: Optional[str] = None, team_description: Optional[str] = None) -> dict[str, Any]:
    """Scaffold onto an EMPTY git remote: probe read-only, init a clone, push the day-one graph.

    A non-empty remote is treated as an existing graph — this refuses and points the caller at
    binding (set ``MNEMEX_GRAPH_REMOTE``) instead of clobbering it.
    """
    probe = mnx_binding.probe_remote(remote)
    if not probe.get("reachable"):
        # Report the categorized diagnosis; the local-folder fallback is the actionable next step.
        raise _InitError("unreachable-remote", probe.get("message", "remote unreachable"),
                         probe.get("fallback") or "fix the remote/auth, or init a local folder instead",
                         extra={"probe": probe})
    if not probe.get("empty"):
        raise _InitError("remote-not-empty",
                         "The remote already has content — this looks like an existing graph.",
                         "bind to it instead: set MNEMEX_GRAPH_REMOTE (no init needed)",
                         extra={"probe": probe})

    clone = mnx_binding.cache_path_for(remote)
    if clone.exists() and any(clone.iterdir()):
        raise _InitError("clone-dirty",
                         f"The local clone dir already has content: {clone}",
                         "resolve or remove the stale clone dir, then retry")
    clone.mkdir(parents=True, exist_ok=True)

    init = _git(["init", "-b", "main"], clone)
    if init.returncode != 0:  # very old git without -b: fall back to default + rename later
        _git(["init"], clone)
    _git(["remote", "add", "origin", remote], clone)

    scaffold_result = scaffold(clone, team=team, org=org, team_description=team_description)
    finish = _finish_clean(clone)

    _git(["add", "-A"], clone)
    commit = _git(["commit", "-m", "mnx-init: scaffold graph (day-one)"], clone)
    if commit.returncode != 0:
        raise _InitError("commit-failed", commit.stderr.strip() or "git commit failed",
                         "check git identity/config in the clone")
    _git(["branch", "-M", "main"], clone)
    push = _git(["push", "-u", "origin", "main"], clone)
    if push.returncode != 0:
        raise _InitError("push-failed", push.stderr.strip() or "git push failed",
                         "the graph is scaffolded+committed locally; fix auth and push origin/main")

    out = {"action": "scaffolded-remote", "kind": "git-remote",
           "graph_root": str(clone), "graph_remote": remote,
           "scaffold": scaffold_result, "pushed": True}
    out.update(finish)
    return out


class _InitError(Exception):
    """Structured init failure carried to the CLI / MCP tool: code + message + action + extra."""

    def __init__(self, code: str, message: str, action: str, extra: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.action = action
        self.extra = extra or {}


def init_graph(remote: Optional[str] = None, path: Optional[str] = None,
               team: str = DEFAULT_TEAM, org: Optional[str] = None,
               team_description: Optional[str] = None) -> dict[str, Any]:
    """Dispatch to local/remote init after validating exactly one target is given."""
    if bool(remote) == bool(path):
        raise _InitError("bad-args", "init_graph needs exactly one of remote / path.",
                         "pass either path=<folder> or remote=<git-url>")
    if path:
        return init_local(path, team=team, org=org, team_description=team_description)
    return init_remote(remote, team=team, org=org, team_description=team_description)


# --- cli ----------------------------------------------------------------------

_USAGE = [
    "mnx_init.py scaffold <root> [--team <name>] [--org <name>]   — empty graph skeleton (non-destructive)",
    "mnx_init.py init --path <dir> [--team <name>] [--org <name>]  — scaffold + merge-driver + stamp + doctor",
    "mnx_init.py init --remote <url> [--team <name>] [--org <name>] — scaffold onto an EMPTY remote and push",
]
_FLAGS = {"--team": True, "--org": True, "--path": True, "--remote": True, "--desc": True}


def _flag(argv: list[str], name: str) -> Optional[str]:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE, _FLAGS)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else ""
    team = _flag(argv, "--team") or DEFAULT_TEAM
    org = _flag(argv, "--org")
    desc = _flag(argv, "--desc")
    try:
        if cmd == "scaffold":
            root = argv[2] if len(argv) > 2 and not argv[2].startswith("-") else None
            if not root:
                return mnx_common.emit({"error": "scaffold needs <root>", "usage": _USAGE}, ok=False)
            return mnx_common.emit(scaffold(root, team=team, org=org, team_description=desc))
        if cmd == "init":
            res = init_graph(remote=_flag(argv, "--remote"), path=_flag(argv, "--path"),
                             team=team, org=org, team_description=desc)
            return mnx_common.emit(res, ok=res.get("doctor_clean", True))
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}", "usage": _USAGE}, ok=False)
    except _InitError as ie:
        return mnx_common.emit({"error": ie.code, "message": str(ie), "action": ie.action, **ie.extra},
                               ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


main = _main  # back-compat alias (engine-wide dispatcher name; plan v2, 0e)

if __name__ == "__main__":
    sys.exit(_main(sys.argv))
