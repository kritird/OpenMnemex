"""mnx_resolve.py — id↔path resolution and the reverse-edge map.

The shared resolver used by read, write, and gc; they must all agree.
See docs/06-script-contracts.md.

The reverse map is built from node front-matter `edges` (hard, integrity-guaranteed)
across ALL tiers and tombstones — a cold or dead node is still visible to integrity
checks, which is what makes logical tiering safe. Soft cross-TEAM `references` are
EXCLUDED from in-degree by design.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import mnx_common

# Populated by build_reverse_map / _scan so referrers() can return paths and
# in_degree() can classify local vs cross-cluster. Single-process CLI cache.
_CTX: dict[str, Any] = {}


def _scan(scope: str) -> dict[str, Any]:
    """Walk every node under scope and index id→path, edges, status, cluster."""
    id_to_path: dict[str, str] = {}
    edges_by_id: dict[str, list[str]] = {}
    status_by_id: dict[str, str] = {}
    cluster_by_id: dict[str, str] = {}
    for cluster in mnx_common.iter_clusters(scope):
        for nf in mnx_common.iter_node_files(cluster):
            try:
                node = mnx_common.parse_node(nf)
            except Exception:
                continue
            nid = node.get("id")
            if not nid:
                continue
            id_to_path[nid] = str(nf)
            status_by_id[nid] = node.get("status", "active")
            cluster_by_id[nid] = str(Path(cluster).resolve())
            outs = []
            for e in node.get("edges") or []:
                if isinstance(e, dict) and e.get("to"):
                    outs.append(e["to"])
            edges_by_id[nid] = outs
    ctx = {
        "scope": str(scope),
        "id_to_path": id_to_path,
        "edges_by_id": edges_by_id,
        "status_by_id": status_by_id,
        "cluster_by_id": cluster_by_id,
        "cross_rows": _cross_rows(scope),
    }
    _CTX.clear()
    _CTX.update(ctx)
    return ctx


def _cross_rows(scope: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    root = mnx_common.find_graph_root(scope) or Path(scope)
    for cl in Path(root).rglob(mnx_common.CROSSLINKS_FILENAME):
        for r in mnx_common.parse_md_table(cl.read_text(encoding="utf-8")):
            if r.get("from_id") and r.get("to_id"):
                rows.append(r)
    return rows


def resolve(node_id: str, scope: str) -> Optional[str]:
    """id → path. None if absent. Looks across all clusters under scope."""
    ctx = _CTX if _CTX.get("scope") == str(scope) else _scan(scope)
    return ctx["id_to_path"].get(node_id)


def build_reverse_map(scope: str) -> dict[str, list[str]]:
    """Map node_id → [referrer_ids] from node edges. COLD + TOMBSTONED included."""
    ctx = _scan(scope)
    reverse: dict[str, list[str]] = {nid: [] for nid in ctx["id_to_path"]}
    for src, outs in ctx["edges_by_id"].items():
        for dst in outs:
            reverse.setdefault(dst, [])
            if src not in reverse[dst]:
                reverse[dst].append(src)
    return reverse


def in_degree(node_id: str, reverse_map: dict[str, list[str]], cross_links: Any = None) -> tuple[int, int]:
    """Return (local_in_degree, cross_cluster_in_degree). Soft cross-team refs EXCLUDED."""
    refs = reverse_map.get(node_id, [])
    cluster_by_id = _CTX.get("cluster_by_id", {})
    target_cluster = cluster_by_id.get(node_id)
    local = cross = 0
    for r in refs:
        if target_cluster and cluster_by_id.get(r) == target_cluster:
            local += 1
        else:
            cross += 1
    return local, cross


def referrers(node_id: str, reverse_map: dict[str, list[str]], cross_links: Any = None) -> list[dict]:
    """Return [{id, path}] of nodes pointing at node_id — for transactional severing."""
    id_to_path = _CTX.get("id_to_path", {})
    return [{"id": r, "path": id_to_path.get(r)} for r in reverse_map.get(node_id, [])]


def weighted_in_degree(node_id: str, reverse_map: dict[str, list[str]],
                       weight_by_id: dict[str, float]) -> float:
    """Liveness-weighted in-degree: Σ over referrers of each referrer's weight (usage score).

    A dead/cold referrer carries a small weight, so it props the target up only weakly —
    this is what makes structural strength self-cleaning (W6). Soft cross-team `references`
    are already excluded (they never enter the reverse map). A referrer with no known weight
    (e.g. a tombstone the caller omitted) contributes 0.
    """
    return sum(max(0.0, float(weight_by_id.get(r, 0.0))) for r in reverse_map.get(node_id, []))


def structural_strength_map(scope: str, score_by_id: dict[str, float],
                            cfg: dict[str, Any]) -> dict[str, float]:
    """Compute liveness-weighted structural strength for every node under scope.

    One-pass approximation: referrer liveness weight = its usage `score` (from `score_by_id`),
    which breaks the score↔struct circularity deterministically. Returns {id: struct in
    [0, strength_max]}. `score_by_id` should already be decayed-to-now (the consolidation
    snapshot). Requires mnx_decay for the saturating `struct_g`.
    """
    import mnx_decay  # local import: keep the pure resolver importable without the math module
    reverse = build_reverse_map(scope)
    out: dict[str, float] = {}
    for nid in reverse:
        w = weighted_in_degree(nid, reverse, score_by_id)
        out[nid] = mnx_decay.struct_g(w, cfg)
    return out


def sole_referrer_of(node_id: str, reverse_map: dict[str, list[str]]) -> list[str]:
    """Return ids of LIVE nodes whose ONLY inbound edge is from node_id."""
    status_by_id = _CTX.get("status_by_id", {})
    out: list[str] = []
    for target, refs in reverse_map.items():
        if refs == [node_id] and status_by_id.get(target, "active") != "dead":
            out.append(target)
    return out


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "resolve":
            path = resolve(argv[2], argv[3])
            return mnx_common.emit({"id": argv[2], "path": path, "found": path is not None},
                                   ok=path is not None)
        if cmd == "reverse-map":
            return mnx_common.emit({"reverse_map": build_reverse_map(argv[2])})
        if cmd == "in-degree":
            rm = build_reverse_map(argv[3])
            local, cross = in_degree(argv[2], rm)
            return mnx_common.emit({"id": argv[2], "local": local, "cross": cross})
        if cmd == "referrers":
            rm = build_reverse_map(argv[3])
            return mnx_common.emit({"id": argv[2], "referrers": referrers(argv[2], rm)})
        if cmd == "struct":
            # struct <scope> — liveness-weighted structural strength for every node, using the
            # live (decayed-to-now) index score as each referrer's weight.
            import mnx_config
            import mnx_decay
            scope = argv[2]
            root = mnx_common.require_graph_root(scope)
            cfg = mnx_config.load(str(root))
            now = mnx_common.now_utc()
            score_by_id: dict[str, float] = {}
            for cl in mnx_common.iter_clusters(scope):
                idx_path = Path(cl) / mnx_common.INDEX_FILENAME
                if not idx_path.is_file():
                    continue
                idx = mnx_common.parse_index(idx_path)
                for tier in ("hot", "warm", "cold"):
                    for row in idx[tier]:
                        try:
                            s = float(row.get("strength", "0") or 0)
                        except ValueError:
                            s = 0.0
                        lam = mnx_decay.lam_for(row.get("type", "domain"), cfg)
                        score_by_id[row["id"]] = mnx_decay.score(
                            s, row.get("last_update", now), now, lam)
            struct = structural_strength_map(scope, score_by_id, cfg)
            return mnx_common.emit({"scope": scope, "struct": struct})
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
