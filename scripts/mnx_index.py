"""mnx_index.py — index regeneration (derived from truth; never the reverse).

See docs/06-script-contracts.md and docs/03-data-model-and-schemas.md §3.

The index is GENERATED from the nodes. It denormalizes each node's summary+aliases
(so matching needs no body load), carries the materialized strength/last_update, and
ranks nodes into HOT (top-K) / WARM / COLD. Nodes are never moved; only the index changes.
"""
from __future__ import annotations

import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import mnx_common
import mnx_config
import mnx_decay

DEAD = "dead"
_CONT_RE = re.compile(r"^index\.(\d{3})\.md$")


def _continuation_paths(cluster: str) -> list[Path]:
    """Cold-tier continuation files (index.001.md, index.002.md, …) in chain order."""
    c = Path(cluster)
    return sorted((p for p in c.glob("index.*.md") if _CONT_RE.match(p.name)),
                  key=lambda p: p.name)


def _index_files(cluster: str) -> list[Path]:
    """The head index plus every continuation chunk, in chain order."""
    head = Path(cluster) / mnx_common.INDEX_FILENAME
    files = [head] if head.is_file() else []
    return files + _continuation_paths(cluster)


def index_node_ids(cluster: str) -> set[str]:
    """Union of node ids across the head index AND all continuation chunks.

    A chained index spreads cold rows over index.NNN.md files, so any consumer that needs the
    full index node-set (e.g. the doctor's node-set invariant) must merge them, not read the head."""
    ids: set[str] = set()
    for path in _index_files(cluster):
        try:
            idx = mnx_common.parse_index(path)
        except Exception:
            continue
        for tier in ("hot", "warm", "cold"):
            ids |= {r["id"] for r in idx[tier]}
    return ids


def _active_nodes(cluster: str) -> list[dict[str, Any]]:
    nodes = []
    for nf in mnx_common.iter_node_files(cluster):
        try:
            node = mnx_common.parse_node(nf)
        except Exception:
            continue
        if node.get("status") != DEAD and node.get("id"):
            nodes.append(node)
    return nodes


def _dead_nodes(cluster: str) -> list[dict[str, Any]]:
    """Tombstoned nodes — surfaced in the dead.md audit tier (W3), never read in normal routing."""
    out = []
    for nf in mnx_common.iter_node_files(cluster):
        try:
            node = mnx_common.parse_node(nf)
        except Exception:
            continue
        if node.get("status") == DEAD and node.get("id"):
            out.append(node)
    return out


def _seed_from_index(cluster: str) -> dict[str, dict[str, Any]]:
    """Recover existing strength/last_update from a current index (head + continuations), if any."""
    state: dict[str, dict[str, Any]] = {}
    for idx_path in _index_files(cluster):
        try:
            idx = mnx_common.parse_index(idx_path)
        except Exception:
            continue
        for tier in ("hot", "warm", "cold"):
            for row in idx[tier]:
                try:
                    strength = float(row.get("strength", "") or 0)
                except ValueError:
                    strength = 0.0
                state[row["id"]] = {"strength": strength,
                                    "last_update": row.get("last_update", ""),
                                    "type": row.get("type", "domain")}
    return state


def _expires(last_update: str, cfg: dict[str, Any]) -> str:
    try:
        dt = mnx_common.parse_ts(last_update) + timedelta(days=float(cfg.get("cold_ttl_days", 120)))
        return dt.strftime(mnx_common.ISO_FMT)
    except Exception:
        return ""


def _existing_header(cluster: str) -> tuple[str, list[str]]:
    idx_path = Path(cluster) / mnx_common.INDEX_FILENAME
    if idx_path.is_file():
        idx = mnx_common.parse_index(idx_path)
        return idx.get("description", ""), idx.get("children", [])
    return f"{Path(cluster).name} nodes.", []


def regenerate_index(cluster: str, materialized_state: Optional[dict[str, Any]] = None) -> None:
    """Rebuild a cluster's index.md HOT/WARM/COLD from its nodes.

    Denormalizes summary+aliases; carries strength/last_update from materialized_state
    (seeded from the existing index when not supplied); enforces hot ≤ hot_k.
    """
    cluster = str(cluster)
    root = mnx_common.require_graph_root(cluster)
    cfg = mnx_config.load(str(root))
    now = mnx_common.now_utc()
    state = dict(materialized_state) if materialized_state is not None else _seed_from_index(cluster)

    ranked = []
    for node in _active_nodes(cluster):
        nid, ntype = node["id"], node.get("type", "domain")
        st = state.get(nid, {})
        strength = float(st.get("strength", cfg["strength_max"]))
        last_update = st.get("last_update") or node.get("updated") or now
        live = mnx_decay.score(strength, last_update, now, mnx_decay.lam_for(ntype, cfg))
        ranked.append({
            "id": nid, "type": ntype,
            "summary": str(node.get("summary", "")).replace("|", "\\|"),
            "aliases": mnx_common.aliases_to_index(node.get("aliases")).replace("|", "\\|"),
            "strength": strength, "last_update": last_update, "score": live,
        })
    ranked.sort(key=lambda r: r["score"], reverse=True)

    hot_k = int(cfg.get("hot_k", 12))
    hot, warm, cold = [], [], []
    for rank, r in enumerate(ranked):
        tier = mnx_decay.tier_of(r["score"], rank, cfg)
        (hot if tier == "hot" else warm if tier == "warm" else cold).append(r)

    description, children = _existing_header(cluster)
    name = Path(cluster).name

    if cfg.get("tier_files"):
        _regenerate_split(cluster, name, description, children, hot, warm, cold, cfg)
        return

    # Chain the index (B-tree-leaf style) when the COLD tier outgrows one file: the head keeps
    # hot + warm + the first cold chunk and records the continuation count, so one head-read still
    # routes; the overflow spills into index.001.md, index.002.md, …  (decision #14, docs/11).
    chunk_rows = int(cfg.get("index_chunk_rows", cfg.get("node_budget", 35)))
    head_cold, cont_chunks = cold, []
    if chunk_rows > 0 and len(cold) > chunk_rows:
        head_cold = cold[:chunk_rows]
        rest = cold[chunk_rows:]
        cont_chunks = [rest[i:i + chunk_rows] for i in range(0, len(rest), chunk_rows)]

    _write_continuations(cluster, name, cont_chunks, cfg)
    Path(Path(cluster) / mnx_common.INDEX_FILENAME).write_text(
        _render(name, description, children, hot, warm, head_cold, cfg, len(cont_chunks)),
        encoding="utf-8")


def _regenerate_split(cluster: str, name: str, description: str, children: list[str],
                      hot, warm, cold, cfg) -> None:
    """Tier-per-file layout (W3): a slim ROUTER index.md (Hot + counts + freshness, always read)
    plus sibling warm.md / cold.md (+ cold.NNN.md chain) / dead.md opened only on demand. The
    file read most (hot) stops being rewritten by cold/dead churn; dead.md is audit-only."""
    c = Path(cluster)
    dead_rows = [{"id": n["id"], "type": n.get("type", "domain"),
                  "summary": str(n.get("summary", "")).replace("|", "\\|"),
                  "died": n.get("died", "")} for n in _dead_nodes(cluster)]

    chunk_rows = int(cfg.get("index_chunk_rows", cfg.get("node_budget", 35)))
    cold_head, cold_cont = cold, []
    if chunk_rows > 0 and len(cold) > chunk_rows:
        cold_head, rest = cold[:chunk_rows], cold[chunk_rows:]
        cold_cont = [rest[i:i + chunk_rows] for i in range(0, len(rest), chunk_rows)]

    # Router: hot only + per-tier counts + freshness + continuation pointers.
    counts = {"hot": len(hot), "warm": len(warm), "cold": len(cold), "dead": len(dead_rows)}
    L = [f"# {name} — index (router)",
         f"> {description}   <!-- slim router: hot + counts; tiers in sibling files -->", "",
         f"<!-- tiers: hot={counts['hot']} warm={counts['warm']} cold={counts['cold']} "
         f"dead={counts['dead']}; warm.md cold.md{' + cold.NNN.md' if cold_cont else ''} dead.md -->",
         f"<!-- regenerated: {mnx_common.now_utc()} -->", "",
         "## Children"]
    L += [f"- {ch}" for ch in children] or ["- (none — this is a leaf cluster)"]
    L += ["", "## Hot                               <!-- always read; top-K -->",
          "| id | type | summary | aliases | strength | last_update |",
          "|----|------|---------|---------|----------|-------------|"]
    L += [_row(r, False, cfg) for r in hot]
    if any([warm, cold, dead_rows]):
        L += ["", "## Tiers                             <!-- open on demand -->"]
        L += [f"- warm → warm.md ({counts['warm']})", f"- cold → cold.md ({counts['cold']})"]
        L += [f"- cold continuation → cold.{n:03d}.md" for n in range(1, len(cold_cont) + 1)]
        L += [f"- dead → dead.md ({counts['dead']}, audit only)"]
    L += ["", "<!-- GENERATED ROUTER. Do not hand-edit. merge=mnx-regen. -->", ""]
    (c / mnx_common.INDEX_FILENAME).write_text("\n".join(L), encoding="utf-8")

    _write_tier_file(c / mnx_common.WARM_FILENAME, name, "Warm", warm, cfg, cold=False)
    _write_tier_file(c / mnx_common.COLD_FILENAME, name, "Cold", cold_head, cfg, cold=True)
    for n, chunk in enumerate(cold_cont, start=1):
        _write_tier_file(c / f"cold.{n:03d}.md", name, "Cold", chunk, cfg, cold=True)
    _write_dead_file(c / mnx_common.DEAD_FILENAME, name, dead_rows)

    # Prune stale artifacts from a prior layout / longer chain.
    for p in _continuation_paths(cluster):  # old index.NNN.md from single-file chaining
        p.unlink()
    for p in c.glob("cold.*.md"):
        m = re.match(r"^cold\.(\d+)\.md$", p.name)
        if m and int(m.group(1)) > len(cold_cont):
            p.unlink()


def _write_tier_file(path: Path, name: str, tier: str, rows, cfg, cold: bool) -> None:
    hdr = ("| id | type | summary | aliases | strength | last_update | expires |"
           if cold else "| id | type | summary | aliases | strength | last_update |")
    sep = ("|----|------|---------|---------|----------|-------------|---------|"
           if cold else "|----|------|---------|---------|----------|-------------|")
    L = [f"# {name} — {tier.lower()} tier", f"> {tier} nodes; route on index.md.", "",
         f"## {tier}", hdr, sep]
    L += [_row(r, cold, cfg) for r in rows]
    L += ["", "<!-- GENERATED tier file (W3). Do not hand-edit. merge=mnx-regen. -->", ""]
    path.write_text("\n".join(L), encoding="utf-8")


def _write_dead_file(path: Path, name: str, dead_rows: list[dict[str, Any]]) -> None:
    L = [f"# {name} — dead tier (audit)", "> Tombstones; never read in normal routing.", "",
         "## Dead", "| id | type | summary | died |", "|----|------|---------|------|"]
    L += [f"| {r['id']} | {r['type']} | {r['summary']} | {r['died']} |" for r in dead_rows]
    L += ["", "<!-- GENERATED audit tier (W3). Retained tombstones. merge=mnx-regen. -->", ""]
    path.write_text("\n".join(L), encoding="utf-8")


def _write_continuations(cluster: str, name: str, chunks: list[list[dict[str, Any]]],
                         cfg: dict[str, Any]) -> None:
    """Write index.NNN.md continuation chunks and delete any now-stale ones."""
    for n, chunk in enumerate(chunks, start=1):
        path = Path(cluster) / f"index.{n:03d}.md"
        nxt = f"index.{n + 1:03d}.md" if n < len(chunks) else None
        path.write_text(_render_continuation(name, chunk, cfg, n, len(chunks), nxt),
                        encoding="utf-8")
    for p in _continuation_paths(cluster):  # prune chunks left over from a larger prior chain
        m = _CONT_RE.match(p.name)
        if m and int(m.group(1)) > len(chunks):
            try:
                p.unlink()
            except Exception:
                pass


def _row(r: dict[str, Any], cold: bool, cfg: dict[str, Any]) -> str:
    base = f"| {r['id']} | {r['type']} | {r['summary']} | {r['aliases']} | {r['strength']:.2f} | {r['last_update']} |"
    if cold:
        base += f" {_expires(r['last_update'], cfg)} |"
    return base


def _render(name: str, description: str, children: list[str],
            hot, warm, cold, cfg, cont_count: int = 0) -> str:
    L = [f"# {name} — index",
         f"> {description}   <!-- chunk 1: route on this -->", ""]
    if cont_count:
        L += [f"<!-- continuation: {cont_count} (cold continues in "
              f"index.001.md … index.{cont_count:03d}.md) -->", ""]
    L += ["## Children                          <!-- chunk 1 -->"]
    L += [f"- {c}" for c in children] or ["- (none — this is a leaf cluster)"]
    L += ["",
          "## Hot                               <!-- chunk 1 tail; top-K -->",
          "| id | type | summary | aliases | strength | last_update |",
          "|----|------|---------|---------|----------|-------------|"]
    L += [_row(r, False, cfg) for r in hot]
    L += ["",
          "## Warm                              <!-- chunk 2 -->",
          "| id | type | summary | aliases | strength | last_update |",
          "|----|------|---------|---------|----------|-------------|"]
    L += [_row(r, False, cfg) for r in warm]
    cold_hdr = "## Cold                              <!-- chunk 3+ -->"
    if cont_count:
        cold_hdr = ("## Cold                              "
                    f"<!-- chunk 3+; continues in index.001.md … index.{cont_count:03d}.md -->")
    L += ["", cold_hdr,
          "| id | type | summary | aliases | strength | last_update | expires |",
          "|----|------|---------|---------|----------|-------------|---------|"]
    L += [_row(r, True, cfg) for r in cold]
    if cont_count:
        L += ["",
              "## Continuations                     <!-- chunk 1: follow only for a deep cold search -->"]
        L += [f"- index.{n:03d}.md" for n in range(1, cont_count + 1)]
    L += ["",
          "<!-- GENERATED FILE. Do not hand-edit. Regenerated by mnx-promote apply and mnx-doctor --fix. -->",
          ""]
    return "\n".join(L)


def _render_continuation(name: str, cold, cfg, n: int, total: int, nxt: Optional[str]) -> str:
    """A cold-tier continuation chunk. Routing reads the head; this is opened only on a deep
    cold search that walks the chain. Same Cold table shape so mnx_common.parse_index reads it."""
    tail = f"next: {nxt}" if nxt else "end of chain"
    L = [f"# {name} — index (continuation {n} of {total})",
         f"> Cold-tier continuation chunk ({tail}); route on the head index.md.   "
         f"<!-- chunk: cold continuation {n} -->", "",
         "## Cold                               <!-- chunk 3+ -->",
         "| id | type | summary | aliases | strength | last_update | expires |",
         "|----|------|---------|---------|----------|-------------|---------|"]
    L += [_row(r, True, cfg) for r in cold]
    L += ["",
          f"<!-- GENERATED FILE. Do not hand-edit. {('Continues in ' + nxt) if nxt else 'Last chunk.'} -->",
          ""]
    return "\n".join(L)


def denorm_check(cluster: str) -> list[dict[str, Any]]:
    """Return drift records where index.summary/aliases != node.summary/aliases (head + chain)."""
    by_id = {n["id"]: n for n in _active_nodes(cluster)}
    drift = []
    for idx_path in _index_files(cluster):
        try:
            idx = mnx_common.parse_index(idx_path)
        except Exception:
            continue
        for tier in ("hot", "warm", "cold"):
            for row in idx[tier]:
                node = by_id.get(row["id"])
                if not node:
                    continue
                want_summary = str(node.get("summary", "")).replace("|", "\\|")
                if row.get("summary", "") != want_summary:
                    drift.append({"id": row["id"], "field": "summary",
                                  "index": row.get("summary"), "node": want_summary})
                want_aliases = mnx_common.aliases_from_index(mnx_common.aliases_to_index(node.get("aliases")))
                if mnx_common.aliases_from_index(row.get("aliases", "")) != want_aliases:
                    drift.append({"id": row["id"], "field": "aliases",
                                  "index": row.get("aliases"), "node": "; ".join(want_aliases)})
    return drift


def shard_index(cluster: str, by: str = "domain") -> dict[str, Any]:
    """Plan a split of an over-budget index along a declared sub-key. Never moves nodes."""
    root = mnx_common.require_graph_root(cluster)
    cfg = mnx_config.load(str(root))
    budget = int(cfg.get("node_budget", 35))
    nodes = _active_nodes(cluster)
    if len(nodes) <= budget:
        return {"action": "ok", "count": len(nodes), "budget": budget}
    groups: dict[str, list[str]] = {}
    for n in nodes:
        keys = n.get(by) or ["(none)"]
        if isinstance(keys, str):
            keys = [keys]
        for k in keys:
            groups.setdefault(str(k), []).append(n["id"])
    overflowing = {k: v for k, v in groups.items() if len(v) > budget}
    if overflowing:
        # A single sub-key still overflows: do NOT escalate first. Chain the index into linked
        # continuation chunks (B-tree-leaf style) so one head-read still routes (decision #14);
        # regenerate_index performs the chaining. Human escalation is the genuine last resort.
        chunk_rows = int(cfg.get("index_chunk_rows", budget))
        return {"action": "chain", "by": by, "budget": budget, "index_chunk_rows": chunk_rows,
                "overflowing_subkeys": {k: len(v) for k, v in overflowing.items()},
                "detail": ("A single sub-key still exceeds node_budget; the cold tier is chained "
                           "into index.NNN.md continuation chunks (no node moves, no human needed). "
                           "Escalate to a human split only if even chaining is undesirable.")}
    return {"action": "split", "by": by, "budget": budget,
            "groups": {k: len(v) for k, v in groups.items()}}


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "regenerate":
            regenerate_index(argv[2])
            return mnx_common.emit({"action": "regenerated", "cluster": argv[2]})
        if cmd == "denorm-check":
            return mnx_common.emit({"cluster": argv[2], "drift": denorm_check(argv[2])})
        if cmd == "shard":
            return mnx_common.emit(shard_index(argv[2]))
        if cmd == "node-ids":
            ids = sorted(index_node_ids(argv[2]))
            return mnx_common.emit({"cluster": argv[2], "count": len(ids),
                                    "continuations": len(_continuation_paths(argv[2])), "ids": ids})
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
