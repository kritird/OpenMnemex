"""mnx_serve.py — the OpenMnemex local viewer server (viewer plan V1.1).

A permanently VIEW-ONLY web surface over any local graph: ``openmnemex-serve`` (or
``uvx openmnemex serve``) starts a localhost HTTP server whose JSON API renders what the
engine read paths already expose — tree, nodes+edges, node detail, search, health, the
revalidation queue, effective config, and machine-level agent connection status. The SPA
frontend (``viewer/static/``, no build step — plain ES modules + vendored Cytoscape) is
served from ``/`` with ``/g/{graph}/…`` deep links and ``/static/*`` assets; the files
resolve through ``mnx_common.viewer_static_dir()`` (checkout first, else the
``openmnemex.data.viewer`` wheel copy).

Write surface (the FULL list; base decision 2026-07-19, connect added 2026-07-20 —
docs/viewer-build-plan.md §1):
  * ``POST /api/graphs/create`` — scaffold a brand-new graph via the shared
    ``mnx_init.init_graph`` (the same code path as MCP ``init_graph`` and the mnx-init
    skill), refused unless the target folder is fresh/empty;
  * ``POST /api/graphs/rescan`` — a registry-only write (the known-graphs ledger);
  * ``POST /api/agents/connect`` — write an agent's machine-level Mnemex config via the
    shared ``mnx_install.install`` (the same code path as ``openmnemex install``), only
    on the user's Connect click. Touches agent config files, never graph knowledge.
Every graph-scoped route is read-only, forever. No capture, promote, revalidate, or
config editing from this surface — those belong to the agents (MCP/plugin) and the CLI.

Decisions settled here per the plan's "decide in V1.1" markers:
  * Graph discovery reuses the ENGINE's own known-graphs registry
    (``mnx_binding.graphs_registry_path()`` + ``register_graph``/``list_graphs``) instead
    of a second viewer-private JSON file — one ledger, shared with MCP ``list_graphs``.
    The bounded $HOME scan and the manual open both feed it.
  * Search walks the denormalized index rows (id/aliases/summary — the same match surface
    the phonebook uses) first, then falls back to a capped body substring scan. The
    simindex minhash path is similarity-, not substring-oriented, so it is not used here.
  * Claude Code plugin detection: best-effort scan of the Claude plugins directory for the
    mnemex plugin manifest/name; degrades to ``"unknown"`` (check with /plugin) when the
    directory layout is unrecognizable — see ``_detect_claude_plugin``.

All decay/freshness numbers are computed SERVER-SIDE by the engine (``mnx_decay`` /
``mnx_config.resolve_horizon``) — the single source of truth. ``?at=TIMESTAMP`` recomputes
at a projected clock for the time scrubber; the frontend never reimplements λ math.

FastAPI/uvicorn are an OPTIONAL extra (``pip install 'openmnemex[viewer]'``); this module
must stay importable without them (the packaging bridge imports every engine module), so
the web imports are soft and only ``create_app()``/``serve()`` require them. Payload
builders are plain functions over the engine — importable and testable with no server.

Server behavior: binds 127.0.0.1 ONLY (no remote access/auth by design), ``--port``
(default 8765, auto-increment when taken), ``--no-open``, ``--graph PATH`` (register a
graph in an unusual location), one quiet human log line per request. Reads tolerate a
graph mid-maintenance: the team-lock state is surfaced as a ``maintenance`` flag and
partially-written/unparseable files are skipped and reported under ``warnings`` — a
broken file must never 500 the whole view.

CLI:
    serve [--port N] [--no-open] [--graph PATH]  — run the viewer (default; blocks)
    graphs                                       — the discovered-graphs payload as JSON
    info                                         — identity + web-deps availability

Dependencies: engine = Python 3.9+ stdlib + PyYAML; serving needs the [viewer] extra.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any, Optional

import mnx_binding
import mnx_common
import mnx_config
import mnx_decay
import mnx_doctor
import mnx_index
import mnx_init
import mnx_lock
import mnx_resolve
import mnx_stage

# Soft web imports: the engine (and the packaging bridge) must work without the
# [viewer] extra. Only building/running the server needs FastAPI + uvicorn.
try:
    import fastapi  # noqa: F401
    import uvicorn  # noqa: F401
    # Module-global so the routes' postponed annotations (PEP 563, `from __future__
    # import annotations`) resolve when FastAPI inspects the handler signatures.
    from fastapi import Request
    _VIEWER_IMPORT_ERROR: Optional[BaseException] = None
except Exception as _exc:  # ImportError; anything else means a broken install
    fastapi = None  # type: ignore[assignment]
    uvicorn = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]
    _VIEWER_IMPORT_ERROR = _exc

SERVER_NAME = "openmnemex-viewer"
DEFAULT_PORT = 8765
DUE_SOON_DAYS_DEFAULT = 7.0     # freshness ring turns amber this many days before stale
_GHOST_PREFIX = "ghost:"        # synthetic ids for red-link ghost nodes (no atom behind them)

# Bounded $HOME scan limits (discovery layer 2 — registry stays the fast path).
_SCAN_MAX_DEPTH = 6
_SCAN_MAX_DIRS = 25000
_SCAN_MAX_SECONDS = 10.0
_SCAN_SKIP_DIRS = {
    "node_modules", "Library", "Applications", "Pictures", "Movies", "Music",
    "__pycache__", "venv", "env", "site-packages", "dist", "build", "target",
    "Downloads",  # transient by convention; a graph living there can be opened manually
}


def engine_version() -> str:
    """Single-sourced engine version (same resolution as the MCP server's)."""
    import mnx_mcp
    return mnx_mcp.engine_version()


# --- error contract -----------------------------------------------------------

class ViewerError(Exception):
    """A structured, HTTP-renderable failure: code + message + actionable next step."""

    def __init__(self, code: str, message: str, action: Optional[str] = None,
                 http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.action = action
        self.http_status = http_status

    def to_payload(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.action:
            err["action"] = self.action
        return {"ok": False, "error": err}


def _parse_at(at: Optional[str]) -> str:
    """Validate the time-scrubber ``?at=`` timestamp; default to the real clock."""
    if not at:
        return mnx_common.now_utc()
    try:
        mnx_common.parse_ts(at)
    except Exception:
        raise ViewerError("bad-at", f"?at= is not an ISO-8601 timestamp: {at!r}",
                          "pass e.g. ?at=2026-08-01T00:00:00Z")
    return at


# --- graph discovery (registry cache + bounded scan + manual) ------------------

def _binding_for(root: str | Path) -> "mnx_binding.Binding":
    return mnx_binding.Binding("viewer", local_path=str(root))


def _root_for_registry_row(row: dict[str, Any]) -> Optional[Path]:
    if row.get("kind") == "git-remote":
        return mnx_binding.graphs_cache_root() / row["slug"]
    return Path(row["location"]).expanduser() if row.get("location") else None


def _last_activity(root: Path) -> Optional[str]:
    """Newest mtime across the graph's markdown files, best-effort."""
    latest = 0.0
    try:
        for cluster in mnx_common.iter_clusters(root):
            for p in Path(cluster).glob("*.md"):
                latest = max(latest, p.stat().st_mtime)
        cfg = root / mnx_common.CONFIG_FILENAME
        if cfg.is_file():
            latest = max(latest, cfg.stat().st_mtime)
    except Exception:
        pass
    if not latest:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(latest, timezone.utc).strftime(mnx_common.ISO_FMT)


def graph_card(root: str | Path) -> dict[str, Any]:
    """The welcome-screen card for one graph: name, path, counts, last activity."""
    root = Path(root)
    binding = _binding_for(root)
    clusters = mnx_common.iter_clusters(root)
    nodes = sum(len(mnx_common.iter_node_files(c)) for c in clusters)
    teams = sorted({mnx_common.team_of(root, c) or "(root)" for c in clusters})
    staged = 0
    try:
        staged = int(mnx_stage.status(binding).get("count", 0))
    except Exception:
        pass
    return {
        "slug": binding.slug(),
        "name": binding.display_name(),
        "path": str(root),
        "kind": binding.kind(),
        "teams": teams,
        "clusters": len(clusters),
        "nodes": nodes,
        "staged": staged,
        "last_activity": _last_activity(root),
    }


def known_graph_roots() -> dict[str, Path]:
    """slug → validated graph root, from the engine's known-graphs registry."""
    out: dict[str, Path] = {}
    for row in mnx_binding.list_graphs():
        root = _root_for_registry_row(row)
        if root and (root / mnx_common.CONFIG_FILENAME).is_file():
            out.setdefault(row["slug"], root)
    return out


def graphs_payload() -> dict[str, Any]:
    """``GET /api/graphs`` — every registered graph, validated and carded."""
    roots = known_graph_roots()
    cards = [graph_card(root) for _slug, root in sorted(roots.items())]
    cards.sort(key=lambda c: c.get("last_activity") or "", reverse=True)
    return {"ok": True, "count": len(cards), "graphs": cards,
            "empty": not cards,
            **({"empty_state": {
                "message": "No graphs found yet. Create your first graph, open a folder, "
                           "or rescan this machine.",
            }} if not cards else {})}


def register_root(root: str | Path) -> Path:
    """Manual open (discovery layer 3): validate a folder holds a graph and register it."""
    start = Path(root).expanduser()
    found = mnx_common.find_graph_root(start)
    if found is None:
        raise ViewerError(
            "not-a-graph", f"No Mnemex graph ({mnx_common.CONFIG_FILENAME}) at or above {start}.",
            "pick the graph's folder, or create one via POST /api/graphs/create",
            http_status=404)
    mnx_binding.register_graph(_binding_for(found))
    return found


def scan_home() -> dict[str, Any]:
    """Bounded $HOME walk for graph markers + ``.mnemex.md`` breadcrumbs (layer 2).

    Skip-list + depth cap + dir/time budgets keep it fast; hidden directories are skipped
    wholesale (the engine's own homes are covered separately via the registry and the
    remote-clone cache). Every hit is registered, so later launches read the registry
    and never pay for the walk again.
    """
    home = Path.home()
    found: list[str] = []
    breadcrumbs: list[str] = []
    visited = 0
    started = time.monotonic()
    capped = False

    for dirpath, dirnames, filenames in os.walk(home, topdown=True):
        visited += 1
        if visited > _SCAN_MAX_DIRS or time.monotonic() - started > _SCAN_MAX_SECONDS:
            capped = True
            dirnames[:] = []
            continue
        rel_depth = len(Path(dirpath).relative_to(home).parts)
        if rel_depth >= _SCAN_MAX_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames
                           if d not in _SCAN_SKIP_DIRS and not d.startswith(".")]
        if mnx_common.CONFIG_FILENAME in filenames:
            found.append(dirpath)
            mnx_binding.register_graph(_binding_for(dirpath))
            dirnames[:] = []  # a graph root never nests another
            continue
        if mnx_binding.BINDING_FILENAME in filenames:
            # Project binding breadcrumb: follow it to its graph and register that.
            try:
                fm = mnx_binding.read_frontmatter(Path(dirpath) / mnx_binding.BINDING_FILENAME)
            except Exception:
                continue
            gp = fm.get("graph_path")
            if gp and (Path(gp).expanduser() / mnx_common.CONFIG_FILENAME).is_file():
                breadcrumbs.append(str(Path(gp).expanduser()))
                mnx_binding.register_graph(_binding_for(Path(gp).expanduser()))
            elif fm.get("graph_remote"):
                breadcrumbs.append(str(fm["graph_remote"]))
                mnx_binding.register_graph(
                    mnx_binding.Binding("viewer", remote=str(fm["graph_remote"])))

    return {"scanned_dirs": visited, "capped": capped,
            "found": sorted(set(found)), "breadcrumbs": sorted(set(breadcrumbs))}


def rescan_payload(path: Optional[str] = None) -> dict[str, Any]:
    """``POST /api/graphs/rescan`` — registry-only write. With ``path``, register that
    one folder ("Open a folder…"); without, re-run the bounded $HOME scan."""
    if path:
        root = register_root(path)
        return {"ok": True, "action": "opened", "graph": graph_card(root)}
    scan = scan_home()
    return {"ok": True, "action": "rescanned", **scan, **graphs_payload()}


def create_graph_payload(path: str, org: Optional[str] = None,
                         team: Optional[str] = None) -> dict[str, Any]:
    """``POST /api/graphs/create`` — the ONE write exception: scaffold a brand-new empty
    graph via the shared ``mnx_init`` scaffolder. Refuses any non-fresh target."""
    if not path or not str(path).strip():
        raise ViewerError("bad-args", "create needs a target folder path.",
                          'POST {"path": "~/graphs/my-knowledge"}')
    target = Path(path).expanduser()
    existing = mnx_common.find_graph_root(target)  # covers ancestors even before mkdir
    if existing is not None:
        raise ViewerError(
            "already-a-graph", f"A graph already exists at {existing}.",
            "open it instead (POST /api/graphs/rescan with that path)", http_status=409)
    if target.exists() and not target.is_dir():
        raise ViewerError("target-not-a-folder", f"{target} exists and is not a folder.",
                          "pick a folder path", http_status=409)
    if target.is_dir() and any(target.iterdir()):
        raise ViewerError(
            "target-not-empty",
            f"{target} is not empty — the viewer only creates graphs in fresh/empty folders "
            "and never touches existing data.",
            "pick an empty or new folder", http_status=409)
    try:
        result = mnx_init.init_graph(path=str(target), team=team or mnx_init.DEFAULT_TEAM,
                                     org=org)
    except (mnx_init._InitError, ValueError) as exc:
        code = getattr(exc, "code", "init-failed")
        action = getattr(exc, "action", "fix the arguments and retry")
        raise ViewerError(code, str(exc), action)
    return {"ok": True, "action": "created", "init": result,
            "graph": graph_card(result["graph_root"])}


def resolve_graph(slug: str) -> Path:
    """URL ``{g}`` slug → validated graph root, via the registry."""
    root = known_graph_roots().get(slug)
    if root is None:
        raise ViewerError("unknown-graph", f"No known graph with slug {slug!r}.",
                          "GET /api/graphs lists known graphs; POST /api/graphs/rescan "
                          "to discover more", http_status=404)
    return root


# --- shared per-graph helpers --------------------------------------------------

def _confine_scope(root: Path, scope: Optional[str]) -> Path:
    """Resolve a relative scope path inside the graph root; refuse escapes."""
    if not scope:
        return root
    candidate = (root / scope).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise ViewerError("bad-scope", f"scope escapes the graph root: {scope!r}",
                          "pass a path relative to the graph root, e.g. team-payments")
    if not candidate.is_dir():
        raise ViewerError("bad-scope", f"scope is not a folder in this graph: {scope!r}",
                          "GET /api/graph/{g}/tree lists valid scopes", http_status=404)
    return candidate


def _index_state(cluster: Path) -> dict[str, dict[str, Any]]:
    """id → {strength, last_update, tier} from the cluster index (head + chained chunks
    + W3 tier files, via the same merge paths the engine reads). Best-effort."""
    state: dict[str, dict[str, Any]] = {}
    for idx_path in mnx_index._index_files(str(cluster)):
        try:
            idx = mnx_common.parse_index(idx_path)
        except Exception:
            continue
        for tier in ("hot", "warm", "cold"):
            for row in idx[tier]:
                try:
                    strength = float(row.get("strength", "") or 0.0)
                except ValueError:
                    strength = 0.0
                state.setdefault(row["id"], {
                    "strength": strength,
                    "last_update": row.get("last_update", ""),
                    "tier": tier,
                })
    return state


def _maintenance_state(root: Path) -> dict[str, Any]:
    """Team-lock / mid-pass state → the "maintenance in progress" banner flag."""
    busy: list[str] = []
    for team_dir in sorted(p for p in root.iterdir()
                           if p.is_dir() and p.name.startswith("team-")):
        try:
            if mnx_lock.held(str(team_dir)) or mnx_lock.in_progress(str(team_dir)):
                busy.append(team_dir.name)
        except Exception:
            continue
    return {"maintenance": bool(busy), "busy_teams": busy}


def _freshness_state(stale_at: Optional[str], at: str, due_soon_days: float,
                     volatility: Any, status: str) -> Optional[str]:
    """fresh | due_soon | stale | timeless; None for tombstones/unknowable horizons."""
    if status == "dead":
        return None
    if stale_at is None:
        vol = str(volatility or "default").strip().lower()
        return "timeless" if vol == "timeless" else None
    try:
        from datetime import timedelta
        stale_dt = mnx_common.parse_ts(stale_at)
        at_dt = mnx_common.parse_ts(at)
        if stale_dt <= at_dt:
            return "stale"
        if stale_dt <= at_dt + timedelta(days=float(due_soon_days)):
            return "due_soon"
        return "fresh"
    except Exception:
        return None


def _hotness_bucket(strength_now: float, strength_max: float) -> str:
    frac = strength_now / strength_max if strength_max > 0 else 0.0
    return "hot" if frac >= (2.0 / 3.0) else "cooling" if frac >= (1.0 / 3.0) else "cold"


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _ghost_id(name: str) -> str:
    return _GHOST_PREFIX + mnx_common.slugify(_norm_name(name))


def _node_entry(node: dict[str, Any], root: Path, cluster: Path, cfg: dict[str, Any],
                idx_state: dict[str, dict[str, Any]], at: str,
                due_soon_days: float) -> dict[str, Any]:
    """One node's viewer payload — every number computed by the engine, at time ``at``."""
    nid = node["id"]
    ntype = node.get("type", "domain")
    status = str(node.get("status", "active"))
    st = idx_state.get(nid)
    if st and st.get("last_update"):
        strength, last_update = st["strength"], st["last_update"]
    else:  # not (yet) in the index — fall back to node truth
        strength = float(cfg.get("strength_max", 1.0))
        last_update = node.get("updated") or node.get("created") or at
    try:
        strength_now = mnx_decay.score(strength, last_update, at,
                                       mnx_decay.lam_for(ntype, cfg))
    except Exception:
        strength_now = 0.0
    try:
        # exposed so the viewer tooltip can show it — the frontend never does λ math
        half_life_days = round(mnx_decay.half_life_for(ntype, cfg), 1)
    except Exception:
        half_life_days = None
    stale_at = mnx_config.resolve_horizon(node, cfg)
    return {
        "id": nid,
        "title": node.get("title") or nid,
        "summary": node.get("summary", ""),
        "path": str(Path(node["_path"]).resolve().relative_to(root.resolve())),
        "cluster": str(Path(cluster).resolve().relative_to(root.resolve())),
        "team": mnx_common.team_of(root, cluster) or "(root)",
        "tier": (st or {}).get("tier"),
        "staged": False,
        "node_type": ntype,
        "volatility": node.get("volatility", "default"),
        "strength_now": round(strength_now, 6),
        "half_life_days": half_life_days,
        "hotness_bucket": _hotness_bucket(strength_now, float(cfg.get("strength_max", 1.0))),
        "verified": node.get("verified"),
        "stale_at": stale_at,
        "freshness_state": _freshness_state(stale_at, at, due_soon_days,
                                            node.get("volatility"), status),
        "superseded_by": node.get("superseded-by"),
        "tombstoned": status == "dead",
        "ghost": False,
    }


def _iter_scope_nodes(root: Path, scope_dir: Path, warnings: list[dict[str, Any]]):
    """Yield (node, cluster, idx_state) for every parseable node under the scope.
    Unparseable files are skipped and reported — never fatal (mid-write tolerance)."""
    for cluster in mnx_common.iter_clusters(scope_dir):
        idx_state = _index_state(cluster)
        for nf in mnx_common.iter_node_files(cluster):
            try:
                node = mnx_common.parse_node(nf)
            except Exception as exc:
                warnings.append({"path": str(nf), "error": str(exc),
                                 "detail": "unparseable node skipped (possibly mid-write)"})
                continue
            if not node.get("id"):
                warnings.append({"path": str(nf), "error": "node has no id", "detail": "skipped"})
                continue
            yield node, Path(cluster), idx_state


def _graph_wide_ids(root: Path) -> dict[str, dict[str, str]]:
    """id → {path, cluster, status} for the WHOLE graph (for boundary-edge stubs)."""
    ctx = mnx_resolve._scan(str(root))
    return {nid: {"path": ctx["id_to_path"][nid],
                  "cluster": ctx["cluster_by_id"].get(nid, ""),
                  "status": ctx["status_by_id"].get(nid, "active")}
            for nid in ctx["id_to_path"]}


def _staged_entries(root: Path, scope_dir: Path,
                    warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Staged (unpromoted) captures for this graph as overlay node entries. Included when
    the atom's domain matches a cluster under the scope, or at whole-graph scope."""
    try:
        atoms = mnx_stage.list_atoms(_binding_for(root)).get("atoms", [])
    except Exception as exc:
        warnings.append({"path": "(staging)", "error": str(exc),
                         "detail": "staging overlay unavailable"})
        return []
    whole_graph = scope_dir.resolve() == root.resolve()
    scope_clusters = {Path(c).name for c in mnx_common.iter_clusters(scope_dir)}
    out = []
    for a in atoms:
        domains = a.get("domain")
        domains = domains if isinstance(domains, list) else [domains] if domains else []
        if not whole_graph and not ({str(d) for d in domains} & scope_clusters):
            continue
        out.append({
            "id": a.get("provisional_id"),
            "title": a.get("summary") or a.get("provisional_id"),
            "summary": a.get("summary", ""),
            "path": None,
            "cluster": None,
            "team": None,
            "tier": "staged",
            "staged": True,
            "node_type": a.get("type", "domain"),
            "volatility": a.get("volatility", "default"),
            "strength_now": None,
            "hotness_bucket": None,
            "verified": None,
            "stale_at": None,
            "freshness_state": None,
            "superseded_by": None,
            "tombstoned": False,
            "ghost": False,
            "domains": domains,
            "staged_at": a.get("staged_at"),
            "score": a.get("score"),
        })
    return out


# --- graph-scoped payloads ------------------------------------------------------

def tree_payload(root: Path) -> dict[str, Any]:
    """``GET /api/graph/{g}/tree`` — org → teams → clusters (+ staging summary)."""
    teams: dict[str, dict[str, Any]] = {}
    for cluster in mnx_common.iter_clusters(root):
        team = mnx_common.team_of(root, cluster) or "(root)"
        t = teams.setdefault(team, {"team": team, "clusters": []})
        description = ""
        idx_path = Path(cluster) / mnx_common.INDEX_FILENAME
        if idx_path.is_file():
            try:
                description = mnx_common.parse_index(idx_path).get("description", "")
            except Exception:
                pass
        t["clusters"].append({
            "name": Path(cluster).name,
            "path": str(Path(cluster).resolve().relative_to(root.resolve())),
            "nodes": len(mnx_common.iter_node_files(cluster)),
            "description": description,
        })
    staged = 0
    try:
        staged = int(mnx_stage.status(_binding_for(root)).get("count", 0))
    except Exception:
        pass
    return {"ok": True, "org": root.name,
            "teams": sorted(teams.values(), key=lambda t: t["team"]),
            "staging": {"count": staged},
            **_maintenance_state(root)}


def nodes_payload(root: Path, scope: Optional[str] = None, at: Optional[str] = None,
                  include: Optional[str] = None) -> dict[str, Any]:
    """``GET /api/graph/{g}/nodes?scope=&at=&include=tombstoned`` — nodes + edges.

    Tombstones are excluded by default (``include=tombstoned`` for history mode).
    Red-links surface as ghost node entries; edges crossing the scope boundary render as
    stubs to the outside node so cross-team links never silently vanish.
    """
    at = _parse_at(at)
    include_tombstoned = include == "tombstoned"
    scope_dir = _confine_scope(root, scope)
    cfg = mnx_config.load(str(root))
    due_soon = float(cfg.get("due_soon_days", DUE_SOON_DAYS_DEFAULT))
    warnings: list[dict[str, Any]] = []

    entries: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    ghost_wanted_by: dict[str, dict[str, Any]] = {}
    raw_nodes: dict[str, dict[str, Any]] = {}

    for node, cluster, idx_state in _iter_scope_nodes(root, scope_dir, warnings):
        entry = _node_entry(node, root, cluster, cfg, idx_state, at, due_soon)
        if entry["tombstoned"] and not include_tombstoned:
            continue
        entries[entry["id"]] = entry
        raw_nodes[entry["id"]] = node

    graph_ids = _graph_wide_ids(root)

    for nid, node in raw_nodes.items():
        for e in node.get("edges") or []:
            if not (isinstance(e, dict) and e.get("to")):
                continue
            to = e["to"]
            if to in entries:
                edges.append({"from": nid, "to": to, "type": e.get("type", ""),
                              "kind": "edge"})
            elif to in graph_ids:
                if graph_ids[to]["status"] == "dead" and not include_tombstoned:
                    continue
                edges.append({"from": nid, "to": to, "type": e.get("type", ""),
                              "kind": "edge", "stub": True})
            else:
                warnings.append({"path": node["_path"],
                                 "error": f"edge target {to!r} does not exist",
                                 "detail": "dangling edge (doctor invariant 1)"})
        for ref in node.get("references") or []:
            # soft cross-team references appear both as {to, type} dicts and bare id strings
            to = ref.get("to") if isinstance(ref, dict) else (str(ref) if ref else None)
            rtype = ref.get("type", "") if isinstance(ref, dict) else ""
            if to and (to in entries or to in graph_ids):
                edges.append({"from": nid, "to": to, "type": rtype,
                              "kind": "reference", **({} if to in entries else
                                                      {"stub": True})})
        for m in node.get("mentions") or []:
            if isinstance(m, dict) and m.get("name") and not m.get("resolved_id"):
                gid = _ghost_id(m["name"])
                g = ghost_wanted_by.setdefault(gid, {"name": m["name"], "wanted_by": []})
                if nid not in g["wanted_by"]:
                    g["wanted_by"].append(nid)
                edges.append({"from": nid, "to": gid, "type": "red-link", "kind": "red-link"})

    # Boundary-edge stub entries: minimal cards for outside-of-scope targets.
    stub_ids = {e["to"] for e in edges if e.get("stub")} - set(entries)
    stubs = [{"id": sid,
              "title": sid,
              "path": str(Path(graph_ids[sid]["path"]).resolve()
                          .relative_to(root.resolve())) if sid in graph_ids else None,
              "cluster": (str(Path(graph_ids[sid]["cluster"]).resolve()
                              .relative_to(root.resolve()))
                          if sid in graph_ids and graph_ids[sid]["cluster"] else None),
              "team": (mnx_common.team_of(root, graph_ids[sid]["cluster"])
                       if sid in graph_ids and graph_ids[sid]["cluster"] else None),
              "stub": True, "ghost": False, "staged": False,
              "tombstoned": sid in graph_ids and graph_ids[sid]["status"] == "dead"}
             for sid in sorted(stub_ids)]

    ghosts = [{"id": gid, "title": g["name"], "name": g["name"], "ghost": True,
               "staged": False, "stub": False, "tombstoned": False,
               "wanted_by": g["wanted_by"],
               "detail": "red-link: not written yet"}
              for gid, g in sorted(ghost_wanted_by.items())]

    staged = _staged_entries(root, scope_dir, warnings)

    return {"ok": True, "graph_root": str(root), "scope": scope or "", "at": at,
            "count": len(entries),
            "nodes": sorted(entries.values(), key=lambda n: n["id"]),
            "stubs": stubs, "ghosts": ghosts, "staged": staged,
            "edges": edges, "warnings": warnings,
            **_maintenance_state(root)}


def node_payload(root: Path, node_id: str, at: Optional[str] = None) -> dict[str, Any]:
    """``GET /api/graph/{g}/node/{id}`` — full node: mesh, history chain, raw atom."""
    at = _parse_at(at)
    cfg = mnx_config.load(str(root))
    due_soon = float(cfg.get("due_soon_days", DUE_SOON_DAYS_DEFAULT))
    warnings: list[dict[str, Any]] = []

    if node_id.startswith(_GHOST_PREFIX) or node_id.startswith(mnx_stage.ID_PREFIX):
        raise ViewerError(
            "no-atom", f"{node_id!r} has no atom file behind it "
            f"({'red-link ghost' if node_id.startswith(_GHOST_PREFIX) else 'staged capture'}).",
            "ghosts/staged entries carry their detail inline in the nodes payload",
            http_status=404)

    # One graph-wide pass: paths, front-matter, reverse map, supersession chains.
    all_nodes: dict[str, dict[str, Any]] = {}
    cluster_of: dict[str, Path] = {}
    for node, cluster, idx_state in _iter_scope_nodes(root, root, warnings):
        all_nodes[node["id"]] = node
        cluster_of[node["id"]] = cluster
    if node_id not in all_nodes:
        raise ViewerError("unknown-node", f"No node {node_id!r} in this graph.",
                          "GET /api/graph/{g}/search?q= to find nodes", http_status=404)

    node = all_nodes[node_id]
    cluster = cluster_of[node_id]
    entry = _node_entry(node, root, cluster, cfg, _index_state(cluster), at, due_soon)

    def _brief(nid: str) -> dict[str, Any]:
        n = all_nodes.get(nid)
        if n is None:
            return {"id": nid, "found": False}
        return {"id": nid, "title": n.get("title") or nid, "summary": n.get("summary", ""),
                "status": n.get("status", "active"),
                "cluster": str(cluster_of[nid].resolve().relative_to(root.resolve()))}

    mesh_out = [{**_brief(e["to"]), "type": e.get("type", "")}
                for e in node.get("edges") or [] if isinstance(e, dict) and e.get("to")]
    reverse = mnx_resolve.build_reverse_map(str(root))
    mesh_in = []
    for rid in reverse.get(node_id, []):
        rtype = ""
        for e in (all_nodes.get(rid, {}).get("edges") or []):
            if isinstance(e, dict) and e.get("to") == node_id:
                rtype = e.get("type", "")
                break
        mesh_in.append({**_brief(rid), "type": rtype})
    red_links = [{"name": m["name"], "ghost_id": _ghost_id(m["name"])}
                 for m in node.get("mentions") or []
                 if isinstance(m, dict) and m.get("name") and not m.get("resolved_id")]

    # History chain: forward via superseded-by, backward via whoever points here.
    chain_forward, seen = [], {node_id}
    cur = node.get("superseded-by")
    while cur and cur not in seen:
        seen.add(cur)
        chain_forward.append(_brief(cur))
        cur = (all_nodes.get(cur) or {}).get("superseded-by")
    superseded_nodes = [_brief(nid) for nid, n in sorted(all_nodes.items())
                        if n.get("superseded-by") == node_id]

    fm = {k: v for k, v in node.items() if not k.startswith("_")}
    return {"ok": True, "at": at, "node": entry,
            "mesh": {"out": mesh_out, "in": mesh_in, "red_links": red_links},
            "history": {"superseded_by_chain": chain_forward,
                        "supersedes": superseded_nodes},
            "atom": {"front_matter": fm, "body": node.get("_body", "")},
            "warnings": warnings,
            **_maintenance_state(root)}


def search_payload(root: Path, q: str, limit: int = 30) -> dict[str, Any]:
    """``GET /api/graph/{g}/search?q=`` — name/alias/summary matches from the index rows
    (the phonebook's own match surface), then a capped body substring scan."""
    if not q or not q.strip():
        raise ViewerError("bad-args", "search needs a non-empty q.", "GET ...?q=settlement")
    needle = q.strip().lower()
    name_hits: list[dict[str, Any]] = []
    content_hits: list[dict[str, Any]] = []

    for cluster in mnx_common.iter_clusters(root):
        cluster_rel = str(Path(cluster).resolve().relative_to(root.resolve()))
        for idx_path in mnx_index._index_files(str(cluster)):
            try:
                idx = mnx_common.parse_index(idx_path)
            except Exception:
                continue
            for tier in ("hot", "warm", "cold"):
                for row in idx[tier]:
                    hay = " ".join([row.get("id", ""), row.get("aliases", ""),
                                    row.get("summary", "")]).lower()
                    if needle in hay:
                        kind = ("name" if needle in row.get("id", "").lower()
                                or needle in row.get("aliases", "").lower() else "summary")
                        name_hits.append({"id": row.get("id"), "cluster": cluster_rel,
                                          "tier": tier, "match": kind,
                                          "summary": row.get("summary", "")})
        if len(name_hits) >= limit:
            break

    seen_ids = {h["id"] for h in name_hits}
    if len(name_hits) < limit:
        for cluster in mnx_common.iter_clusters(root):
            cluster_rel = str(Path(cluster).resolve().relative_to(root.resolve()))
            for nf in mnx_common.iter_node_files(cluster):
                if nf.stem in seen_ids:
                    continue
                try:
                    text = nf.read_text(encoding="utf-8")
                except Exception:
                    continue
                pos = text.lower().find(needle)
                if pos < 0:
                    continue
                snippet = text[max(0, pos - 60):pos + len(needle) + 60].replace("\n", " ")
                content_hits.append({"id": nf.stem, "cluster": cluster_rel,
                                     "match": "content", "snippet": snippet.strip()})
                if len(name_hits) + len(content_hits) >= limit:
                    break
            if len(name_hits) + len(content_hits) >= limit:
                break

    hits = (name_hits + content_hits)[:limit]
    return {"ok": True, "q": q, "count": len(hits), "hits": hits}


def health_payload(root: Path) -> dict[str, Any]:
    """``GET /api/graph/{g}/health`` — doctor findings, mapped to node ids for overlay
    pinning; findings that don't anchor to a node stay listed unmapped."""
    rep = mnx_doctor.check(str(root))
    known = set(_graph_wide_ids(root))
    findings = []
    by_node: dict[str, list[int]] = {}
    for i, f in enumerate(rep.get("findings", [])):
        target = str(f.get("node_or_edge", ""))
        node_ids = [p for p in re.split(r"->|~>", target) if p in known] or \
                   ([target] if target in known else [])
        entry = {**f, "node_ids": node_ids}
        findings.append(entry)
        for nid in node_ids:
            by_node.setdefault(nid, []).append(i)
    return {"ok": True, "clean": rep.get("ok", False), "counts": rep.get("counts", {}),
            "findings": findings, "by_node": by_node,
            **_maintenance_state(root)}


def queue_payload(root: Path, at: Optional[str] = None) -> dict[str, Any]:
    """``GET /api/graph/{g}/queue`` — the revalidation queue: active nodes ordered by
    ``stale_at`` (soonest horizon first); timeless/dead nodes never appear."""
    at = _parse_at(at)
    cfg = mnx_config.load(str(root))
    due_soon = float(cfg.get("due_soon_days", DUE_SOON_DAYS_DEFAULT))
    warnings: list[dict[str, Any]] = []
    items = []
    for node, cluster, idx_state in _iter_scope_nodes(root, root, warnings):
        if str(node.get("status", "active")) == "dead":
            continue
        stale_at = mnx_config.resolve_horizon(node, cfg)
        if stale_at is None:
            continue
        days_left = (mnx_common.parse_ts(stale_at) - mnx_common.parse_ts(at)).total_seconds() \
            / mnx_common.SECONDS_PER_DAY
        items.append({
            "id": node["id"],
            "title": node.get("title") or node["id"],
            "cluster": str(Path(cluster).resolve().relative_to(root.resolve())),
            "team": mnx_common.team_of(root, cluster) or "(root)",
            "node_type": node.get("type", "domain"),
            "volatility": node.get("volatility", "default"),
            "verified": node.get("verified"),
            "stale_at": stale_at,
            "days_until_stale": round(days_left, 2),
            "freshness_state": _freshness_state(stale_at, at, due_soon,
                                                node.get("volatility"), "active"),
            "revalidate_command": f"/mnemex:mnx-revalidate {node['id']}",
        })
    items.sort(key=lambda x: x["stale_at"])
    return {"ok": True, "at": at, "count": len(items), "queue": items,
            "warnings": warnings, **_maintenance_state(root)}


def config_payload(root: Path) -> dict[str, Any]:
    """``GET /api/graph/{g}/config`` — effective knobs + derived λ/horizons. Display only:
    editing stays in the config file / agent surfaces (view-only stance §1)."""
    shown = mnx_config.show(str(root), include_advanced=True)
    return {"ok": True, **shown,
            "view_only_note": ("The viewer never edits config. Change values in "
                               f"{mnx_common.CONFIG_FILENAME} or via your agent "
                               "(mnx-config), then reload.")}


# --- machine-level agent connection status (backs the Connections screen) -------

def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _detect_claude_plugin() -> str:
    """yes | no | unknown — best-effort scan of Claude Code's plugin state for the
    mnemex plugin (V1.1 decision: directory/manifest scan; 'unknown' degrades to the
    '/plugin in Claude Code' hint in the UI)."""
    claude_dir = mnx_binding.claude_home()
    # enabledPlugins in settings.json is how a plugin-manager install manifests durably.
    for settings_name in ("settings.json", "settings.local.json"):
        enabled = _read_json(claude_dir / settings_name).get("enabledPlugins") or {}
        if any("mnemex" in str(k).lower() for k in enabled):
            return "yes"
    plugins_dir = claude_dir / "plugins"
    if not plugins_dir.is_dir():
        return "unknown"
    try:
        for cfg_name in ("config.json", "installed_plugins.json"):
            cfg = plugins_dir / cfg_name
            if cfg.is_file() and "mnemex" in cfg.read_text(encoding="utf-8").lower():
                return "yes"
        for manifest in plugins_dir.glob("**/.claude-plugin/plugin.json"):
            if "mnemex" in manifest.read_text(encoding="utf-8").lower():
                return "yes"
        for entry in plugins_dir.rglob("*"):
            if entry.is_dir() and "mnemex" in entry.name.lower():
                return "yes"
    except Exception:
        return "unknown"
    return "no"


def _detect_claude_mcp() -> str:
    """yes | no — mnemex present in Claude Code's user-level MCP config (~/.claude.json;
    project-scoped .mcp.json files are per-repo and not scanned machine-wide)."""
    candidates = [Path.home() / ".claude.json"]
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg_dir:
        candidates.append(Path(cfg_dir).expanduser() / ".claude.json")
    for cand in candidates:
        data = _read_json(cand)
        if not data:
            continue
        if "mnemex" in (data.get("mcpServers") or {}):
            return "yes"
        for proj in (data.get("projects") or {}).values():
            if isinstance(proj, dict) and "mnemex" in (proj.get("mcpServers") or {}):
                return "yes"
    return "no"


def _agent_row(agent: str, installed: Optional[bool], connected: str,
               command: str, **extra: Any) -> dict[str, Any]:
    return {"agent": agent, "installed": installed, "state": connected,
            "connect_command": command, **extra}


def agents_payload() -> dict[str, Any]:
    """``GET /api/agents`` — detected agents + Mnemex connection state (adapter-marker
    reads only; writes happen solely through ``connect_agent_payload``, on the user's
    explicit Connect click). Claude Code is dual-path-aware:
    plugin vs MCP, plugin recommended, double-connection warned (plan §2 J4)."""
    home = Path.home()
    rows: list[dict[str, Any]] = []

    # claude-code: the dual-path special case.
    plugin, mcp = _detect_claude_plugin(), _detect_claude_mcp()
    claude_installed = bool(shutil.which("claude")) or mnx_binding.claude_home().is_dir()
    if plugin == "yes" and mcp == "yes":
        state, note = "double-connected", (
            "Connected twice — remove the MCP entry "
            "(openmnemex install --agent claude-code --uninstall); the plugin path covers it.")
    elif plugin == "yes":
        state, note = "connected-via-plugin", "Connected via the plugin (recommended path)."
    elif mcp == "yes":
        state, note = "connected-via-mcp", (
            "Connected via MCP. The plugin path is richer (7 auto-hooks, full tier) — "
            "see LIMITATIONS.md.")
    elif plugin == "unknown":
        state, note = "unknown", ("Plugin state undetectable here — check with /plugin "
                                  "inside Claude Code. MCP entry: not found.")
    else:
        state, note = "not-connected", (
            "Two options: the plugin (recommended — richer: 7 auto-hooks, full tier) via "
            "/plugin inside Claude Code, or MCP (works for any client, see LIMITATIONS.md).")
    rows.append(_agent_row(
        "claude-code", claude_installed, state,
        "openmnemex install --agent claude-code",
        connection={"plugin": plugin, "mcp": mcp}, note=note,
        recommended="plugin"))

    # single-path agents: one MCP + instruction-block adapter each (mnx_install owns the
    # formats; the viewer only reads the same markers).
    single = [
        ("gemini-cli", (home / ".gemini").is_dir() or bool(shutil.which("gemini")),
         "mnemex" in (_read_json(home / ".gemini" / "settings.json").get("mcpServers") or {})),
        ("codex", (home / ".codex").is_dir() or bool(shutil.which("codex")),
         "[mcp_servers.mnemex]" in _read_text_safe(home / ".codex" / "config.toml")),
        ("cursor", (home / ".cursor").is_dir(),
         "mnemex" in (_read_json(home / ".cursor" / "mcp.json").get("mcpServers") or {})),
        ("opencode", bool(shutil.which("opencode")) or (home / ".config" / "opencode").is_dir(),
         "mnemex" in (_read_json(home / ".config" / "opencode" / "opencode.json").get("mcp") or {})),
    ]
    for agent, installed, connected in single:
        rows.append(_agent_row(agent, installed,
                               "connected" if connected else "not-connected",
                               f"openmnemex install --agent {agent}"))
    rows.append(_agent_row(
        "copilot", None, "unknown",
        "openmnemex install --agent copilot --project",
        note="Copilot's MCP config is per-project (.vscode/mcp.json) — no machine-level "
             "state to read; run the install command inside the project."))

    return {"ok": True, "agents": rows,
            "note": "Connect writes the same machine-level config the CLI installer would "
                    "(openmnemex install --agent <a> --user); knowledge stays read-only."}


# Agents the Connect button can wire machine-wide (scope="user"), so the write lands
# exactly where agents_payload's detection reads. copilot is excluded by upstream
# design: VS Code has no static user-level MCP file (per-project .vscode/mcp.json only).
_CONNECTABLE_AGENTS = ("claude-code", "gemini-cli", "codex", "cursor", "opencode")


def connect_agent_payload(agent: str) -> dict[str, Any]:
    """``POST /api/agents/connect`` — one-click connect (Kriti, 2026-07-20: the screen
    should connect, not hand out commands to paste). Delegates to the SAME shared
    installer the CLI uses (``mnx_install.install``), user scope, so the viewer can
    never drift from ``openmnemex install`` behavior. This is the viewer's only
    agent-config write, and it happens solely on the user's button press."""
    if agent not in _CONNECTABLE_AGENTS:
        raise ViewerError(
            "not-connectable", f"{agent!r} cannot be connected machine-wide from here",
            "copilot's MCP config is per-project — run "
            "'openmnemex install --agent copilot --project' inside the project",
            http_status=400)
    import mnx_install
    try:
        result = mnx_install.install(agent, scope="user", yes=True)
    except mnx_install.InstallError as exc:
        raise ViewerError("connect-failed", str(exc),
                          f"try the CLI: openmnemex install --agent {agent} --user",
                          http_status=500)
    if not result.get("ok"):
        detail = result.get("error") or "; ".join(result.get("shell_errors", []) or []) \
            or "; ".join(r.get("stderr", "") for r in result.get("ran", []) if r.get("returncode"))
        raise ViewerError("connect-failed", detail or "the installer reported a failure",
                          f"try the CLI: openmnemex install --agent {agent} --user",
                          http_status=500)
    # fresh detection so the UI can re-render the new state without a second request
    return {**agents_payload(), "connected_agent": agent,
            "install_notes": result.get("notes", [])}


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def fs_dirs_payload(path: Optional[str] = None) -> dict[str, Any]:
    """``GET /api/fs/dirs?path=`` — subfolders of one directory, for the UI's folder
    browser. Browsers never reveal a native picker's absolute path to a web page (even
    on localhost), so the "Open a folder…"/"create graph" dialogs browse via the server
    instead. Read-only, names only, directories only; hidden folders are skipped.
    Viewer-surface-specific by design (CLAUDE.md parity note: MCP/skill callers browse
    the filesystem directly — no equivalent needed there)."""
    base = Path(path).expanduser() if path else Path.home()
    try:
        base = base.resolve()
    except OSError:
        pass
    if not base.is_dir():
        raise ViewerError("not-a-folder", f"{base} is not a folder.",
                          "pass a directory path, or omit path for $HOME",
                          http_status=404)
    dirs: list[dict[str, Any]] = []
    denied = False
    try:
        for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if not child.is_dir() or child.is_symlink():
                    continue
                dirs.append({"name": child.name, "path": str(child),
                             "is_graph": (child / mnx_common.CONFIG_FILENAME).is_file()})
            except OSError:
                continue
    except PermissionError:
        denied = True
    return {"ok": True, "path": str(base),
            "parent": str(base.parent) if base.parent != base else None,
            "home": str(Path.home()),
            "is_graph": (base / mnx_common.CONFIG_FILENAME).is_file(),
            "dirs": dirs,
            **({"denied": True} if denied else {})}


# --- the FastAPI app -----------------------------------------------------------

def _deps_missing_message() -> str:
    return ("The OpenMnemex viewer needs FastAPI + uvicorn. Install the optional extra: "
            "pip install 'openmnemex[viewer]'  (or run via: "
            "uvx --from 'openmnemex[viewer]' openmnemex-serve). "
            f"Import error: {_VIEWER_IMPORT_ERROR}")


def deps_available() -> bool:
    return fastapi is not None and uvicorn is not None


# Fallback landing when the SPA files are missing (broken/partial install): the API
# still works, so say so instead of 404ing the root.
_NO_FRONTEND = """<!doctype html><meta charset="utf-8"><title>OpenMnemex viewer</title>
<body style="font-family:system-ui;max-width:44rem;margin:3rem auto;color:#222">
<h1 style="font-family:monospace">OpenMnemex viewer</h1>
<p>The API is up, but the frontend files (viewer/static/) were not found in this
install — reinstall the package, or run from a checkout.</p>
<ul>
<li><a href="/api/graphs">/api/graphs</a> — discovered graphs</li>
<li>/api/graph/{slug}/tree · /nodes · /node/{id} · /search?q= · /health · /queue · /config</li>
<li><a href="/api/agents">/api/agents</a> — agent connection status</li>
</ul></body>"""


def static_asset(rel: str):
    """Resolve a path inside viewer/static via the Traversable API — one code path for
    both a checkout (``pathlib.Path``) and a wheel install (``importlib.resources``).
    Returns the file node, or ``None`` for misses and any traversal attempt."""
    root = mnx_common.viewer_static_dir()
    node = root
    for part in Path(rel).parts:
        if part in ("..", "/", "\\") or part.startswith("~"):
            return None
        node = node / part
    try:
        return node if node.is_file() else None
    except (OSError, ValueError):
        return None


def index_html() -> Optional[str]:
    """The SPA entry (served for ``/`` and every ``/g/...`` deep link)."""
    idx = static_asset("index.html")
    return idx.read_text(encoding="utf-8") if idx is not None else None


def create_app():
    """Build the FastAPI app (requires the [viewer] extra). Read-only by construction:
    the only POSTs are create-graph, rescan, and agent connect (see module docstring)."""
    if not deps_available():
        raise RuntimeError(_deps_missing_message())
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, Response

    app = FastAPI(title=SERVER_NAME, version=engine_version(), docs_url="/api/docs",
                  redoc_url=None)

    @app.exception_handler(ViewerError)
    async def _viewer_error(_req, exc: ViewerError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(Exception)
    async def _internal_error(_req, exc: Exception):
        return JSONResponse(status_code=500, content={
            "ok": False, "error": {"code": "internal", "message": str(exc),
                                   "action": "check the server terminal for details"}})

    @app.middleware("http")
    async def _quiet_log(request: Request, call_next):
        t0 = time.monotonic()
        response = await call_next(request)
        ms = (time.monotonic() - t0) * 1000.0
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if path != "/favicon.ico" and not path.startswith("/static/"):
            print(f"  {request.method} {path} → {response.status_code} ({ms:.0f} ms)")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def landing():
        # no-store: the SPA shell must never go stale across an upgrade
        return HTMLResponse(index_html() or _NO_FRONTEND,
                            headers={"cache-control": "no-store"})

    @app.get("/g/{rest:path}", response_class=HTMLResponse)
    async def spa_deep_link(rest: str):
        # Path routing (viewer plan V1.2): /g/{graph}/… is deep-linkable; the SPA
        # router (viewer/static/js/router.js) reads the URL after load.
        return HTMLResponse(index_html() or _NO_FRONTEND,
                            headers={"cache-control": "no-store"})

    @app.get("/connections", response_class=HTMLResponse)
    async def spa_connections():
        # V1.4: the agent-connections screen is deep-linkable like /g/* routes.
        return HTMLResponse(index_html() or _NO_FRONTEND,
                            headers={"cache-control": "no-store"})

    @app.get("/static/{rel:path}")
    async def static_file(rel: str):
        node = static_asset(rel)
        if node is None:
            raise ViewerError("not-found", f"no static file {rel!r}",
                              "the viewer's own assets live under /static/",
                              http_status=404)
        import mimetypes
        media, _enc = mimetypes.guess_type(rel)
        return Response(content=node.read_bytes(),
                        media_type=media or "application/octet-stream",
                        headers={"cache-control": "no-store"})

    @app.get("/api/graphs")
    async def api_graphs():
        return graphs_payload()

    @app.post("/api/graphs/rescan")
    async def api_rescan(request: Request):
        body = await _json_body(request)
        return rescan_payload(path=body.get("path"))

    @app.post("/api/graphs/create")
    async def api_create(request: Request):
        body = await _json_body(request)
        return create_graph_payload(path=body.get("path", ""),
                                    org=body.get("org"), team=body.get("team"))

    @app.get("/api/graph/{g}/tree")
    async def api_tree(g: str):
        return tree_payload(resolve_graph(g))

    @app.get("/api/graph/{g}/nodes")
    async def api_nodes(g: str, scope: Optional[str] = None, at: Optional[str] = None,
                        include: Optional[str] = None):
        return nodes_payload(resolve_graph(g), scope=scope, at=at, include=include)

    @app.get("/api/graph/{g}/node/{node_id}")
    async def api_node(g: str, node_id: str, at: Optional[str] = None):
        return node_payload(resolve_graph(g), node_id, at=at)

    @app.get("/api/graph/{g}/search")
    async def api_search(g: str, q: str = "", limit: int = 30):
        return search_payload(resolve_graph(g), q, limit=max(1, min(int(limit), 200)))

    @app.get("/api/graph/{g}/health")
    async def api_health(g: str):
        return health_payload(resolve_graph(g))

    @app.get("/api/graph/{g}/queue")
    async def api_queue(g: str, at: Optional[str] = None):
        return queue_payload(resolve_graph(g), at=at)

    @app.get("/api/graph/{g}/config")
    async def api_config(g: str):
        return config_payload(resolve_graph(g))

    @app.get("/api/agents")
    async def api_agents():
        return agents_payload()

    @app.post("/api/agents/connect")
    async def api_agents_connect(request: Request):
        body = await _json_body(request)
        return connect_agent_payload(str(body.get("agent", "")))

    @app.get("/api/fs/dirs")
    async def api_fs_dirs(path: Optional[str] = None):
        return fs_dirs_payload(path)

    return app


async def _json_body(request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


# --- server lifecycle -----------------------------------------------------------

def _free_port(start: int) -> int:
    """First free localhost port at or above ``start`` (auto-increment on clashes)."""
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise ViewerError("no-free-port", f"no free port in {start}..{start + 49}",
                      "pass --port with a free port")


def serve(port: int = DEFAULT_PORT, open_browser: bool = True,
          graph: Optional[str] = None) -> int:
    """Run the viewer (blocks). Binds 127.0.0.1 only; auto-opens the browser."""
    if not deps_available():
        print(f"openmnemex-serve: {_deps_missing_message()}", file=sys.stderr)
        return 1
    if graph:
        root = register_root(graph)
        print(f"registered graph: {root}")
    app = create_app()
    try:
        port = _free_port(int(port))
    except ViewerError as exc:
        print(f"openmnemex-serve: {exc}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{port}/"
    cards = graphs_payload()
    names = ", ".join(c["name"] for c in cards["graphs"]) or "(none — welcome screen offers create)"
    print(f"OpenMnemex viewer · {url} · pid {os.getpid()}")
    print(f"graphs: {names}")
    print("Ctrl+C to stop; nothing to clean up.")
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(0.4, webbrowser.open, [url]).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def info() -> dict[str, Any]:
    """Identity + web-deps readiness, without starting anything."""
    return {"name": SERVER_NAME, "version": engine_version(),
            "deps_available": deps_available(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            **({} if deps_available() else {"deps_error": _deps_missing_message()})}


# --- cli -------------------------------------------------------------------------

_USAGE = [
    "mnx_serve.py serve [--port N] [--no-open] [--graph PATH]  — run the local viewer "
    "(127.0.0.1 only; default port 8765, auto-increment)",
    "mnx_serve.py graphs                                       — discovered graphs as JSON",
    "mnx_serve.py info                                         — identity + [viewer]-extra readiness",
]
_CLI_FLAGS = {"--port": True, "--no-open": False, "--graph": True}


def _flag(argv: list[str], name: str) -> Optional[str]:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE, _CLI_FLAGS)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else "serve"
    try:
        if cmd == "serve":
            port = _flag(argv, "--port")
            return serve(port=int(port) if port else DEFAULT_PORT,
                         open_browser="--no-open" not in argv,
                         graph=_flag(argv, "--graph"))
        if cmd == "graphs":
            return mnx_common.emit(graphs_payload())
        if cmd == "info":
            return mnx_common.emit(info())
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}", "usage": _USAGE},
                               ok=False)
    except ViewerError as ve:
        return mnx_common.emit(ve.to_payload(), ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


def main() -> int:
    """Console entry point (pyproject [project.scripts] openmnemex-serve)."""
    return _main(sys.argv)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
