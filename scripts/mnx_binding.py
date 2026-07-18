"""mnx_binding.py — graph binding resolution, session sync, and persistence.

Connects an author working in ANY project to the knowledge graph, which may be either:
  * a git remote  (graph_remote)  — cloned to a local cache and kept in sync; writes push;
  * a local folder (graph_path)   — used in place; no clone/sync/push.

Persistence is kind-aware (see persist()):
  * git-remote  -> commit + push (bounded retry)
  * git-local   -> commit (no push)
  * plain-local -> append an audit record to <graph>/.mnemex/history.log

This is the FIRST implemented helper (not a contract stub): self-contained, deterministic,
stdlib + PyYAML only. See docs/binding-and-graph-sync.md and docs/script-contracts.md.

Resolution precedence (most specific wins); within a source, graph_path beats graph_remote:
    1. <project>/.mnemex.md         (nearest ancestor of cwd)
    2. $MNEMEX_GRAPH_PATH / $MNEMEX_GRAPH_REMOTE (+ peers)
    3. <mnemex home>/config.md      (user default; home resolved by mnx_common.mnemex_home()
                                     — ~/.claude/mnemex on existing installs, XDG on fresh ones)
    4. none -> caller must run /mnemex:mnx-init

CLI (each subcommand emits one JSON object on stdout):
    resolve | sync | status | persist --message "…" | push | cache-path | graph-root
    | staging-path | probe-remote --remote <url>   (read-only auth/reachability check BEFORE binding)

Exit codes: 0 ok (incl. offline-degraded), 2 unresolved (run init), 1 error.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared in README
    yaml = None

import mnx_common

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNRESOLVED = 2

BINDING_FILENAME = ".mnemex.md"
STATE_DIR = ".mnemex"
HISTORY_FILE = "history.log"

_ENV = {
    "graph_remote": "MNEMEX_GRAPH_REMOTE",
    "graph_path": "MNEMEX_GRAPH_PATH",
    "default_team": "MNEMEX_DEFAULT_TEAM",
    "author": "MNEMEX_AUTHOR",
}


# --- paths ------------------------------------------------------------------

def claude_home() -> Path:
    """Claude Code's user config root. Kept only as the back-compat input to
    mnx_common.mnemex_home(); new code resolves state paths through mnemex_home()."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def user_config_path() -> Path:
    return mnx_common.mnemex_home() / "config.md"


def graphs_cache_root() -> Path:
    return mnx_common.mnemex_home() / "graphs"


def staging_cache_root() -> Path:
    """Root for per-graph LOCAL side-stores (capture staging atoms + the read-stamp spill).

    These are per-author and live OUTSIDE the graph clone so they survive the session-start
    hard-resync of a remote clone. NOT part of the shared graph — see docs/11."""
    return mnx_common.mnemex_home() / "staging"


def slug_for_remote(remote: str) -> str:
    """Stable, collision-resistant directory name for a graph remote."""
    tail = re.sub(r"\.git$", "", remote.rstrip("/").rsplit("/", 1)[-1])
    tail = re.sub(r"[^A-Za-z0-9._-]", "-", tail) or "graph"
    digest = hashlib.sha1(remote.encode("utf-8")).hexdigest()[:8]
    return f"{tail}-{digest}"


def cache_path_for(remote: str) -> Path:
    return graphs_cache_root() / slug_for_remote(remote)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- frontmatter ------------------------------------------------------------

def read_frontmatter(path: Path) -> dict[str, Any]:
    """Parse the leading YAML front-matter block of a Markdown file. Returns {} if absent."""
    if yaml is None:
        raise RuntimeError("PyYAML is required (pip install pyyaml).")
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    data = yaml.safe_load(parts[1]) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: front-matter is not a mapping")
    return data


# --- binding ----------------------------------------------------------------

class Binding:
    def __init__(self, source: str, remote: Optional[str] = None,
                 local_path: Optional[str] = None, default_team: Optional[str] = None,
                 author: Optional[str] = None, warning: Optional[str] = None):
        self.source = source            # "project:<path>" | "env" | "user:<path>"
        self.remote = remote
        self.local_path = local_path
        self.default_team = default_team
        self.author = author
        self.warning = warning

    def graph_root(self) -> str:
        """The directory skills read/write — the cache clone (remote) or the folder (local)."""
        if self.local_path:
            return os.path.abspath(os.path.expanduser(self.local_path))
        return str(cache_path_for(self.remote))

    def kind(self) -> str:
        if self.local_path:
            return "git-local" if (Path(self.graph_root()) / ".git").exists() else "plain-local"
        return "git-remote"

    def slug(self) -> str:
        """Stable per-graph slug for local side-stores (staging atoms, stamp spill)."""
        return graph_slug(self)

    def staging_root(self) -> str:
        """Local folder holding this graph's capture staging atoms + read-stamp spill."""
        return str(staging_path_for(self))

    def source_kind(self) -> str:
        """The bucket of the resolution source: 'project' | 'env' | 'user'."""
        return self.source.split(":", 1)[0]

    def source_path(self) -> Optional[str]:
        """The file the binding came from, for project/user sources (None for env)."""
        return self.source.split(":", 1)[1] if ":" in self.source else None

    def is_default_fallback(self) -> bool:
        """True when NO project `.mnemex.md` (and no env) matched and we fell through to the
        user-default graph. This is the silent case worth flagging prominently at capture/read
        time — the author may not realize they are writing into their personal graph."""
        return self.source_kind() == "user"

    def display_name(self) -> str:
        """Human-readable graph name: the repo name (remote) or the folder name (local)."""
        if self.local_path:
            return Path(self.local_path).expanduser().name or self.local_path
        tail = re.sub(r"\.git$", "", self.remote.rstrip("/").rsplit("/", 1)[-1])
        return tail or self.remote

    def resolution_line(self) -> str:
        """One-line 'which graph, and why' for a skill to echo at capture/read/promote time.

        Removes the silent-binding gap (LIMITATIONS.md #2): the author is always shown which graph,
        and via which source, they are about to act on — so a wrong-cwd or personal-default misfire
        is caught at capture time, not promote time."""
        kind = self.source_kind()
        if kind == "project":
            detail = f"source: project {os.path.basename(self.source_path() or BINDING_FILENAME)}"
        elif kind == "env":
            detail = "source: environment variable"
        elif kind == "user":
            detail = "source: user default — no project binding found, using your personal graph"
        else:
            detail = f"source: {self.source}"
        return f"{self.display_name()} ({detail})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_remote": self.remote,
            "graph_path": self.local_path,
            "kind": self.kind(),
            "graph_root": self.graph_root(),
            "graph_slug": self.slug(),
            "staging_root": self.staging_root(),
            "default_team": self.default_team,
            "author": self.author,
            "source": self.source,
            "source_kind": self.source_kind(),
            "display_name": self.display_name(),
            "resolution": self.resolution_line(),
            "default_fallback": self.is_default_fallback(),
            "warning": self.warning,
        }


def graph_slug(binding: "Binding") -> str:
    """Stable, collision-resistant slug for a resolved graph's local side-stores.

    Remote graphs key on the remote URL; local graphs key on the absolute folder path
    (prefixed so a folder and a same-named remote never collide). The capture staging
    folder and the usage-stamp spill both live under this slug, co-located for tidiness."""
    if binding.remote:
        return slug_for_remote(binding.remote)
    local = str(Path(binding.local_path).expanduser().resolve())
    return slug_for_remote("local:" + local)


def staging_path_for(binding: "Binding") -> Path:
    return staging_cache_root() / graph_slug(binding)


def _binding_from(getter: Callable[[str], Any], source: str) -> Optional[Binding]:
    path = getter("graph_path")
    remote = getter("graph_remote")
    warning = None
    if path and remote:
        warning = "Both graph_path and graph_remote set; using graph_path (local)."
        remote = None
    if path:
        return Binding(source, local_path=str(path), default_team=getter("default_team"),
                       author=getter("author"), warning=warning)
    if remote:
        return Binding(source, remote=str(remote), default_team=getter("default_team"),
                       author=getter("author"))
    return None


def find_project_binding(start: Path) -> Optional[Path]:
    """Nearest .mnemex.md walking up from `start` to the filesystem root."""
    cur = start.resolve()
    for d in [cur, *cur.parents]:
        candidate = d / BINDING_FILENAME
        if candidate.is_file():
            return candidate
    return None


def resolve(start_dir: Optional[str] = None) -> Optional[Binding]:
    start = Path(start_dir or os.getcwd())

    proj = find_project_binding(start)
    if proj:
        fm = read_frontmatter(proj)
        b = _binding_from(fm.get, f"project:{proj}")
        if b:
            return b

    b = _binding_from(lambda k: os.environ.get(_ENV[k]), "env")
    if b:
        return b

    user = user_config_path()
    if user.is_file():
        fm = read_frontmatter(user)
        b = _binding_from(fm.get, f"user:{user}")
        if b:
            return b

    return None


# --- user-default persistence (guided setup) --------------------------------

def _render_user_default(path: Optional[str], remote: Optional[str],
                         default_team: Optional[str], author: Optional[str]) -> str:
    """The stock ``config.md`` a guided-setup run writes: valid YAML front-matter (exactly one of
    graph_remote / graph_path filled) + a short prose reminder. Empty optional fields render as a
    bare ``key:`` (parses to None), matching templates/user-config.template.md."""
    lines = ["---",
             "# User-level Mnemex default. Lives at <mnemex_home>/config.md (durable; NOT in the plugin dir).",
             "# Fallback graph for any project without its own .mnemex.md and no env override.",
             "#",
             "# Exactly one of graph_remote / graph_path is set below."]
    if remote:
        lines += [f"graph_remote: {remote}", "graph_path:"]
    else:
        lines += ["graph_remote:", f"graph_path: {path}"]
    lines.append(f"default_team: {default_team}" if default_team else "default_team:")
    lines.append(f"author: {author}" if author else "author:")
    lines += ["---", "",
              "# Mnemex user default", "",
              "Your fallback knowledge graph. A project's own `.mnemex.md` (and the "
              "`MNEMEX_GRAPH_REMOTE` / `MNEMEX_GRAPH_PATH` env vars) take precedence over this "
              "file. See `docs/binding-and-graph-sync.md`.", ""]
    return "\n".join(lines)


def write_user_default(path: Optional[str] = None, remote: Optional[str] = None, *,
                       force: bool = False, default_team: Optional[str] = None,
                       author: Optional[str] = None) -> dict[str, Any]:
    """Write ``<mnemex_home>/config.md`` — the user-default binding used by any project with no own
    ``.mnemex.md`` and no env override. Set EXACTLY ONE of ``path`` / ``remote``.

    Refuses to clobber an existing user default unless ``force`` (guided setup must never silently
    replace a binding the user already has — reported as ``{"ok": false, "action": "exists"}`` so a
    caller can surface it, not a traceback). Local paths are stored ABSOLUTE so the default resolves
    identically from every cwd (a relative graph_path in the user default would follow the caller's
    working directory — exactly the silent-misroute this file exists to avoid).
    """
    if bool(path) == bool(remote):
        raise ValueError("write_user_default needs exactly one of path / remote.")
    if path:
        path = os.path.abspath(os.path.expanduser(str(path)))
    target = user_config_path()
    existed = target.is_file()
    if existed and not force:
        return {"ok": False, "action": "exists", "path": str(target),
                "message": (f"A user default already exists at {target}. Pass force=True to "
                            "overwrite it, or bind this project with a .mnemex.md instead.")}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_user_default(path, remote, default_team, author), encoding="utf-8")
    return {"ok": True, "action": "overwritten" if existed else "written",
            "path": str(target), "graph_path": path, "graph_remote": remote,
            "default_team": default_team}


# --- git --------------------------------------------------------------------

def _git(args: list[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)


def _is_git_repo(path: Path) -> bool:
    return path.is_dir() and (path / ".git").exists()


def _default_branch(repo: Path) -> str:
    r = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split("/", 1)[-1]
    _git(["remote", "set-head", "origin", "--auto"], cwd=repo)
    r = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split("/", 1)[-1]
    return "main"


# --- remote preflight (before binding) --------------------------------------

_LOCAL_FALLBACK = (
    "If you'd rather not manage git auth, re-run /mnemex:mnx-init and choose local-folder "
    "mode (graph_path) — a plain folder needs no remote and no credentials."
)

_REMEDIATION = {
    "auth": (
        "Authentication failed. SSH remote: confirm your key is loaded (`ssh-add -l`) and "
        "authorized on the host. HTTPS remote: set up a credential helper or token "
        "(`gh auth login`, or `git config --global credential.helper ...`). Verify you have "
        "access to this repository."
    ),
    "not-found": (
        "The remote does not exist or you lack access. Double-check the URL — and that the repo "
        "was actually created and pushed at least once — then re-run init."
    ),
    "network": (
        "Could not reach the host. Check your network/VPN and that the hostname is correct. "
        "An existing local clone (if any) still works read-only offline."
    ),
    "unknown": (
        "git could not read the remote. Inspect the detail below and verify the URL and access."
    ),
}


def _classify_remote_error(stderr: str) -> str:
    s = (stderr or "").lower()
    if any(k in s for k in ("authentication failed", "permission denied", "could not read username",
                            "access denied", "publickey", "terminal prompts disabled",
                            "host key verification failed", "correct access rights",
                            "could not read from remote repository",
                            "403", "401", "invalid username or password")):
        return "auth"
    if any(k in s for k in ("repository not found", "not found", "does not exist",
                            "no such repository", "404")):
        return "not-found"
    if any(k in s for k in ("could not resolve host", "couldn't resolve", "unable to access",
                            "connection", "timed out", "network is unreachable",
                            "could not connect", "operation timed out")):
        return "network"
    return "unknown"


def probe_remote(remote: str, timeout: float = 20.0) -> dict[str, Any]:
    """Read-only reachability + auth check for a candidate graph remote, BEFORE binding.

    Runs `git ls-remote --heads` with interactive prompts DISABLED (GIT_TERMINAL_PROMPT=0,
    ssh BatchMode), so a missing credential helper fails fast with a clear category instead
    of hanging on a username/passphrase prompt. On failure it classifies the error and returns
    concrete remediation plus the always-available local-folder fallback, so init can guide the
    user instead of just echoing a raw git error after a wasted clone attempt.
    """
    if not remote or not remote.strip():
        return {"reachable": False, "category": "invalid", "message": "No remote URL given."}
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")}
    try:
        proc = subprocess.run(["git", "ls-remote", "--heads", remote],
                              capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"reachable": False, "category": "network", "remote": remote,
                "message": f"Timed out after {int(timeout)}s reaching {remote}.",
                "remediation": _REMEDIATION["network"], "fallback": _LOCAL_FALLBACK}
    except FileNotFoundError:
        return {"reachable": False, "category": "no-git", "remote": remote,
                "message": "git is not installed or not on PATH."}
    if proc.returncode == 0:
        heads = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        return {"reachable": True, "remote": remote, "heads": len(heads), "empty": not heads,
                "message": ("Remote reachable but it has no branches yet — push an initial commit "
                            "before binding so sync has a HEAD to clone." if not heads
                            else "Remote reachable and readable.")}
    category = _classify_remote_error(proc.stderr)
    last = (proc.stderr.strip().splitlines() or ["git ls-remote failed."])[-1]
    return {"reachable": False, "category": category, "remote": remote, "message": last,
            "detail": proc.stderr.strip(), "remediation": _REMEDIATION[category],
            "fallback": _LOCAL_FALLBACK}


# --- sync -------------------------------------------------------------------

def sync(binding: Binding) -> dict[str, Any]:
    """Make the graph ready for the session.

    Local graph: used in place — verify the folder exists (no clone/reset/push).
    Remote graph: materialize at the remote HEAD. A CLEAN clone is hard-reset to origin;
    a clone carrying uncommitted work or unpushed commits is NEVER destroyed (E2E finding
    F11): resync is skipped with a `skipped-dirty` / `skipped-unpushed` action so a
    half-finished promote, a stranded retry-push commit, or a manual edit survives the
    next session start. The clone stays usable (merely possibly stale); persisting or
    discarding is an explicit follow-up. Offline with an existing clone -> degraded
    read-only warning, not a hard failure.
    """
    result: dict[str, Any] = {"kind": binding.kind(), "graph_root": binding.graph_root()}

    if binding.local_path:
        root = Path(binding.graph_root())
        if root.is_dir():
            result.update(action="local", mode="in-place",
                          message=f"Using local graph at {root} (no sync).")
        else:
            result.update(action="error",
                          message=f"Local graph path does not exist: {root}. "
                                  f"Run /mnemex:mnx-init to create it.")
        return result

    path = cache_path_for(binding.remote)
    result["graph_remote"] = binding.remote
    if _is_git_repo(path):
        fetch = _git(["fetch", "--prune", "origin"], cwd=path)
        if fetch.returncode != 0:
            result.update(action="offline", mode="read-only",
                          message="Remote unreachable; using last local clone (read-only).",
                          detail=fetch.stderr.strip())
            return result
        branch = _default_branch(path)
        porcelain = _git(["status", "--porcelain"], cwd=path).stdout.strip()
        if porcelain:
            files = [ln[3:].strip() for ln in porcelain.splitlines()]
            result.update(
                action="skipped-dirty", branch=branch, dirty_files=files[:20],
                message=(f"Graph clone has uncommitted local work ({len(files)} path(s)) — "
                         "resync skipped so nothing is lost; reads may be stale vs origin. "
                         "Persist it (mnx_binding.py persist) or discard it "
                         f"(git -C {path} reset --hard && git -C {path} clean -fd), then resync."))
            return result
        ahead_out = _git(["rev-list", "--count", f"origin/{branch}..HEAD"], cwd=path)
        ahead = int(ahead_out.stdout.strip() or 0) if ahead_out.returncode == 0 else 0
        if ahead > 0:
            result.update(
                action="skipped-unpushed", branch=branch, ahead=ahead,
                message=(f"Graph clone has {ahead} unpushed commit(s) — a previous promote "
                         "committed but did not push. Resync skipped so the commit survives. "
                         "Run mnx_binding.py push (or /mnemex:mnx-promote --retry-push)."))
            return result
        _git(["reset", "--hard", f"origin/{branch}"], cwd=path)
        _git(["clean", "-fd"], cwd=path)
        result.update(action="resynced", branch=branch,
                      message=f"Graph resynced to origin/{branch}.")
        return result

    path.parent.mkdir(parents=True, exist_ok=True)
    clone = _git(["clone", binding.remote, str(path)])
    if clone.returncode != 0:
        result.update(action="error",
                      message="Could not clone the graph and no local copy exists.",
                      detail=clone.stderr.strip())
        return result
    result.update(action="cloned", branch=_default_branch(path), message="Graph cloned.")
    return result


# --- persistence ------------------------------------------------------------

def _ahead_count(repo: Path, branch: str) -> int:
    """Commits on local HEAD not yet on origin/<branch> (best-effort; 0 if undeterminable).

    A value > 0 after a promote means the merge committed but the push did not land — the state
    that makes a blind re-promote dangerous (it would re-apply staging on top of that commit)."""
    r = _git(["rev-list", "--count", f"origin/{branch}..HEAD"], cwd=repo)
    if r.returncode != 0:
        return 0
    try:
        return int(r.stdout.strip() or "0")
    except ValueError:
        return 0


def _recovery(repo: Path, branch: str, kind: str, detail: str) -> dict[str, Any]:
    """Structured, actionable guidance for a push that did not land — not a bare 'retry manually'.

    The promote already COMMITTED locally before the push, so the fix is to push that existing
    commit, NOT to redo the merge. `/mnemex:mnx-promote --retry-push` does exactly that. The manual
    git fallback is scoped to the clone-cache dir as a last resort."""
    return {
        "recoverable": True,
        "retry_command": "/mnemex:mnx-promote --retry-push",
        "clone_path": str(repo),
        "branch": branch,
        "ahead": _ahead_count(repo, branch),
        "manual_fallback": [f"git -C {repo} fetch origin",
                            f"git -C {repo} rebase origin/{branch}",
                            f"git -C {repo} push origin HEAD:{branch}"],
        "guidance": ("The merge is committed locally but the push did not land. Do NOT re-run a full "
                     "promote (it would re-apply staging on top of this commit). Push the existing "
                     "commit with /mnemex:mnx-promote --retry-push; if that keeps failing, resolve it "
                     "by hand with the manual_fallback commands."),
        "detail": detail,
    }


def _push(repo: Path, branch: str, retries: int = 3) -> dict[str, Any]:
    last = ""
    for attempt in range(1, retries + 1):
        _git(["fetch", "origin"], cwd=repo)
        rebase = _git(["rebase", f"origin/{branch}"], cwd=repo)
        if rebase.returncode != 0:
            _git(["rebase", "--abort"], cwd=repo)
            return {"push": "conflict", "attempt": attempt,
                    "message": "Rebase conflict; resolve and retry the push (the commit is preserved).",
                    **_recovery(repo, branch, "conflict", rebase.stderr.strip())}
        p = _git(["push", "origin", f"HEAD:{branch}"], cwd=repo)
        if p.returncode == 0:
            return {"push": "ok", "attempt": attempt}
        last = p.stderr.strip()
    return {"push": "failed", "attempts": retries,
            "message": "Push failed after retries (the commit is preserved).",
            **_recovery(repo, branch, "failed", last)}


def push(binding: Binding, retries: int = 3) -> dict[str, Any]:
    """Push an already-made local commit (remote git graphs only)."""
    if binding.kind() != "git-remote":
        return {"action": "noop", "kind": binding.kind(), "message": "No remote to push to."}
    repo = Path(binding.graph_root())
    if not _is_git_repo(repo):
        return {"action": "error", "message": "No local clone to push from."}
    res = _push(repo, _default_branch(repo), retries)
    return {"action": "pushed" if res.get("push") == "ok" else res.get("push"), **res}


def unpushed_state(binding: Binding) -> dict[str, Any]:
    """Whether the clone has a committed-but-unpushed promote (HEAD ahead of origin).

    Lets mnx-promote distinguish 'start a fresh merge' from 'retry the push of a prior promote that
    committed but failed to push' — the latter must NOT re-run the merge (double-apply). Only
    meaningful for git-remote graphs; everything else reports ahead=0."""
    if binding.kind() != "git-remote":
        return {"ahead": 0, "unpushed": False, "kind": binding.kind()}
    repo = Path(binding.graph_root())
    if not _is_git_repo(repo):
        return {"ahead": 0, "unpushed": False, "kind": binding.kind()}
    branch = _default_branch(repo)
    _git(["fetch", "origin"], cwd=repo)
    ahead = _ahead_count(repo, branch)
    return {"ahead": ahead, "unpushed": ahead > 0, "branch": branch, "kind": "git-remote"}


def persist(binding: Binding, message: str) -> dict[str, Any]:
    """Persist a completed mutation, the right way for the graph's kind.

      git-remote  -> commit all changes, then push (bounded retry).
      git-local   -> commit all changes (no push).
      plain-local -> append an audit record to <graph>/.mnemex/history.log.
    """
    root = Path(binding.graph_root())
    kind = binding.kind()
    result: dict[str, Any] = {"kind": kind, "graph_root": str(root)}

    if kind == "plain-local":
        statedir = root / STATE_DIR
        statedir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": _now_utc(), "message": message, "author": binding.author}
        with (statedir / HISTORY_FILE).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        result.update(action="audit-recorded", history=str(statedir / HISTORY_FILE))
        return result

    # git graphs (local or remote)
    _git(["add", "-A"], cwd=root)
    status = _git(["status", "--porcelain"], cwd=root)
    if not status.stdout.strip():
        result.update(action="nothing-to-commit")
        return result
    commit = _git(["commit", "-m", message], cwd=root)
    if commit.returncode != 0:
        result.update(action="error", message="git commit failed",
                      detail=commit.stderr.strip())
        return result
    if kind == "git-local":
        result.update(action="committed")
        return result
    # git-remote: commit + push
    result.update(action="committed", **_push(root, _default_branch(root)))
    return result


# --- cli --------------------------------------------------------------------

def _emit(obj: dict[str, Any], code: int) -> int:
    print(json.dumps(obj))
    return code


def _arg_after(argv: list[str], flag: str) -> Optional[str]:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


_USAGE = [
    'mnx_binding.py resolve                          — the active binding (project > user > env)',
    'mnx_binding.py sync                             — materialize/refresh the graph clone (never destroys local work)',
    'mnx_binding.py status                           — binding + clone-present + unpushed state',
    'mnx_binding.py unpushed-state                   — committed-but-unpushed promote state',
    'mnx_binding.py graph-root | staging-path        — print the resolved path (exit 2 if unbound)',
    'mnx_binding.py probe-remote --remote <url>      — read-only reachability + auth pre-flight',
    'mnx_binding.py write-user-default --path <dir> | --remote <url> [--force] [--default-team <t>]'
    '  — write the <mnemex_home>/config.md user default (refuses to clobber without --force)',
    'mnx_binding.py persist [--message <m>]          — commit (+push) graph changes',
    'mnx_binding.py push                             — push the current branch',
]
_FLAGS = {"--remote": True, "--message": True, "--path": True, "--force": False,
          "--default-team": True, "--author": True}


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE, _FLAGS)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else "resolve"

    if yaml is None:
        return _emit({"resolved": False, "error": "missing-dependency",
                      "message": "PyYAML is required. Install it with: pip install pyyaml"},
                     EXIT_ERROR)

    if cmd == "probe-remote":  # runs BEFORE binding exists — must not call resolve()
        remote = _arg_after(argv, "--remote")
        if not remote and len(argv) > 2 and not argv[2].startswith("-"):
            remote = argv[2]
        if not remote:
            return _emit({"error": "probe-remote needs --remote <url>"}, EXIT_ERROR)
        res = probe_remote(remote)
        return _emit(res, EXIT_OK if res.get("reachable") else EXIT_ERROR)

    if cmd == "write-user-default":  # runs BEFORE a binding exists — must not call resolve()
        path = _arg_after(argv, "--path")
        remote = _arg_after(argv, "--remote")
        if bool(path) == bool(remote):
            return _emit({"error": "write-user-default needs exactly one of --path / --remote"},
                         EXIT_ERROR)
        res = write_user_default(path=path, remote=remote, force="--force" in argv,
                                 default_team=_arg_after(argv, "--default-team"),
                                 author=_arg_after(argv, "--author"))
        return _emit(res, EXIT_OK if res.get("ok") else EXIT_ERROR)

    try:
        b = resolve()
    except Exception as exc:  # malformed binding / parse error — report, don't traceback
        return _emit({"resolved": False, "error": "binding-error", "message": str(exc)},
                     EXIT_ERROR)
    if not b:
        if cmd in ("cache-path", "graph-root", "staging-path"):
            return EXIT_UNRESOLVED
        return _emit({"resolved": False,
                      "message": "No Mnemex graph configured. Run /mnemex:mnx-init."},
                     EXIT_UNRESOLVED)

    if cmd == "graph-root":
        print(b.graph_root())
        return EXIT_OK
    if cmd == "staging-path":
        print(b.staging_root())
        return EXIT_OK
    if cmd == "cache-path":  # back-compat alias
        print(b.graph_root())
        return EXIT_OK
    if cmd == "resolve":
        return _emit({"resolved": True, **b.to_dict()}, EXIT_OK)
    if cmd == "sync":
        res = sync(b)
        code = EXIT_ERROR if res.get("action") == "error" else EXIT_OK
        return _emit({"resolved": True, **b.to_dict(), **res}, code)
    if cmd == "status":
        root = Path(b.graph_root())
        present = _is_git_repo(root) if b.remote else root.is_dir()
        extra: dict[str, Any] = {}
        if present and b.remote:
            try:  # surface a committed-but-unpushed promote so callers can offer --retry-push
                extra = unpushed_state(b)
            except Exception:
                extra = {}
        return _emit({"resolved": True, **b.to_dict(), "clone_present": present, **extra}, EXIT_OK)
    if cmd == "unpushed-state":
        return _emit({"resolved": True, **b.to_dict(), **unpushed_state(b)}, EXIT_OK)
    if cmd == "persist":
        msg = _arg_after(argv, "--message") or "mnemex: update"
        return _emit({"resolved": True, **b.to_dict(), **persist(b, msg)}, EXIT_OK)
    if cmd == "push":
        return _emit({"resolved": True, **b.to_dict(), **push(b)}, EXIT_OK)

    return _emit({"error": f"unknown subcommand: {cmd}"}, EXIT_ERROR)


main = _main  # back-compat alias; `_main(argv)` is the engine-wide dispatcher name (plan v2, 0e)

if __name__ == "__main__":
    sys.exit(_main(sys.argv))
