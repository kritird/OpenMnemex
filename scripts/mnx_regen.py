"""mnx_regen.py — the `mnx-regen` git merge driver: regenerate derived files from truth (W1).

See docs/13-resilient-mesh-roadmap.md §0–§2.

THE KEYSTONE. The graph has only two kinds of truth — node files and the append-only
`registry.md`. Every other file (`index*.md`, `phonebook.md`, `cross-links.md`, the org
directory) is DERIVED: regenerable from truth, never hand-merged. Two authors promoting to the
same team rewrite the same generated strength cells / tier tables → a git merge conflict on a
machine-generated file. Repairing that by hand is absurd; instead:

  * `registry.md` and the highwater stamps get git's built-in `merge=union` (append logs
    concatenate cleanly; duplicate lines are idempotent under HWM replay).
  * every derived file gets THIS driver (`merge=mnx-regen`): on conflict, discard BOTH sides and
    REGENERATE the file from the working-tree truth.

A push conflict therefore becomes "union the logs, regenerate, done" — no human untangling
generated text. Configured via `.gitattributes` + a one-line `git config` (`install` below; a
doctor check verifies it is registered).

Git invokes:  mnx_regen.py merge %O %A %B %P
  %O ancestor, %A current/ours (WRITE THE RESULT HERE), %B other, %P pathname in the repo.
Exit 0 = resolved; non-zero = leave conflict for a human.

Python 3.9+, stdlib + PyYAML. Imports mnx_common, mnx_index, mnx_phonebook.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import mnx_common
import mnx_index
import mnx_phonebook

_CONT = mnx_common._INDEX_CONT_RE  # index.NNN.md / cold.NNN.md
_TIER = {mnx_common.WARM_FILENAME, mnx_common.COLD_FILENAME, mnx_common.DEAD_FILENAME}


def _is_index_file(name: str) -> bool:
    return name == mnx_common.INDEX_FILENAME or bool(_CONT.match(name))


def _is_tier_file(name: str) -> bool:
    """Split-layout tier files (W3): warm.md / cold.md / dead.md / cold.NNN.md — all regenerated
    by regenerating the owning cluster's index."""
    return name in _TIER or bool(mnx_common._COLD_CONT_RE.match(name))


def regen_content(path: str) -> Optional[str]:
    """Regenerate the derived file at `path` from truth and return its fresh content.

    Returns None if `path` is not a recognized derived file (caller should NOT auto-resolve).
    Regeneration writes the affected derived files to disk (the working tree) as a side effect,
    then this returns the freshly-written bytes of the specific `path`.
    """
    p = Path(path).resolve()
    name = p.name
    parent = p.parent
    root = mnx_common.find_graph_root(parent)
    if root is None:
        return None

    if name == mnx_phonebook.PHONEBOOK_FILENAME:
        mnx_phonebook.regenerate(str(parent))
    elif name == mnx_common.CROSSLINKS_FILENAME:
        _regen_crosslinks(parent, root)
    elif _is_tier_file(name):
        mnx_index.regenerate_index(str(parent))                  # split tier files (W3)
    elif _is_index_file(name):
        if mnx_common.is_cluster(parent):
            mnx_index.regenerate_index(str(parent))              # cluster index (+ continuations)
        elif parent.resolve() == Path(root).resolve():
            mnx_phonebook.regenerate_org(str(root))              # org directory
        else:
            _regen_team_index(parent)                            # team index (children listing)
    else:
        return None

    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _regen_crosslinks(team_root: Path, root: Path) -> None:
    """Rebuild a team's cross-links.md: every hard edge that crosses a cluster boundary WITHIN
    the team (both ids + both paths). Cross-TEAM relationships are soft references, never here."""
    # id -> (cluster_path, abs_path) for nodes in this team
    loc: dict[str, tuple[str, str]] = {}
    edges: list[tuple[str, str, str]] = []  # (from_id, type, to_id)
    for cluster in mnx_common.iter_clusters(team_root):
        rel = str(Path(cluster).resolve().relative_to(team_root.resolve()))
        for nf in mnx_common.iter_node_files(cluster):
            try:
                node = mnx_common.parse_node(nf)
            except Exception:
                continue
            nid = node.get("id")
            if not nid:
                continue
            loc[nid] = (rel, f"{rel}/{nf.name}")
            for e in node.get("edges") or []:
                if isinstance(e, dict) and e.get("to"):
                    edges.append((nid, e.get("type", "relates-to"), e["to"]))
    rows = []
    for from_id, etype, to_id in edges:
        if from_id in loc and to_id in loc and loc[from_id][0] != loc[to_id][0]:
            rows.append((from_id, loc[from_id][1], etype, to_id, loc[to_id][1]))
    rows.sort()
    L = [f"# cross-links: {team_root.name}   (generated — merge=mnx-regen)",
         "| from_id | from_path | type | to_id | to_path |",
         "|---------|-----------|------|-------|---------|"]
    for r in rows:
        L.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |")
    L += ["", "<!-- GENERATED. Boundary edges within the team only. -->", ""]
    (team_root / mnx_common.CROSSLINKS_FILENAME).write_text("\n".join(L), encoding="utf-8")


def _regen_team_index(team_root: Path) -> None:
    """Rebuild a team index.md children listing (the domain sub-folders that hold clusters)."""
    children = sorted({Path(c).resolve().relative_to(team_root.resolve()).parts[0]
                       for c in mnx_common.iter_clusters(team_root)
                       if Path(c).resolve().relative_to(team_root.resolve()).parts})
    L = [f"# {team_root.name} — index", f"> {team_root.name} domains.", "", "## Children"]
    L += [f"- {c}" for c in children] or ["- (none)"]
    L += ["", "<!-- GENERATED. -->", ""]
    (team_root / mnx_common.INDEX_FILENAME).write_text("\n".join(L), encoding="utf-8")


def merge_driver(ancestor: str, current: str, other: str, path: str) -> int:
    """Git merge driver. Regenerate `path` from truth into `current` (%A). Returns exit code."""
    content = regen_content(path)
    if content is None:
        return 1  # not a known derived file — let git keep the conflict for a human
    Path(current).write_text(content, encoding="utf-8")
    return 0


# .gitattributes patterns the driver expects to be present (install writes/verifies the config).
GITATTRIBUTES_MARKER = "# Mnemex: truth unions, derived regenerates (W1)"


def install(repo: str = ".") -> dict:
    """Register the `mnx-regen` merge driver in the repo's git config (idempotent).

    `.gitattributes` (committed) maps files to drivers; the driver COMMAND is local config that
    each clone must register once — so this is also what a fresh clone / mnx-init runs, and what
    the doctor checks.
    """
    plugin_root = Path(__file__).resolve().parent
    cmd = (f'python3 "{plugin_root / "mnx_regen.py"}" merge %O %A %B %P')
    calls = [
        ["git", "-C", repo, "config", "merge.mnx-regen.name",
         "Mnemex: regenerate derived files from truth"],
        ["git", "-C", repo, "config", "merge.mnx-regen.driver", cmd],
    ]
    for c in calls:
        subprocess.run(c, check=True, capture_output=True, text=True)
    return {"action": "installed", "driver": cmd}


def is_installed(repo: str = ".") -> bool:
    r = subprocess.run(["git", "-C", repo, "config", "--get", "merge.mnx-regen.driver"],
                       capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "merge":
            # merge %O %A %B %P  — the git driver entrypoint (no STATUS line: git reads exit code)
            return merge_driver(argv[2], argv[3], argv[4], argv[5])
        if cmd == "regen":
            content = regen_content(argv[2])
            ok = content is not None
            return mnx_common.emit({"path": argv[2], "regenerated": ok,
                                    "bytes": len(content or "")}, ok=ok)
        if cmd == "install":
            return mnx_common.emit(install(argv[2] if len(argv) > 2 else "."))
        if cmd == "is-installed":
            return mnx_common.emit({"installed": is_installed(argv[2] if len(argv) > 2 else ".")})
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
