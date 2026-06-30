"""mnx_doctor.py — invariant checks + self-heal of derived files.

See docs/08-invariants-and-failure-modes.md (Part A) for the full invariant list.

check() is read-only. fix() only ever rebuilds DERIVED artifacts (index, cross-links)
from the nodes — nodes are truth and are never auto-edited by the doctor. fix() is
idempotent (running twice yields no further change).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import mnx_common
import mnx_config
import mnx_index
import mnx_lock
import mnx_phonebook
import mnx_regen
import mnx_resolve

VALID_TYPES = {"domain", "pattern"}
VALID_STATUS = {"active", "superseded", "archived", "dead"}


def _all_nodes(scope: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for cluster in mnx_common.iter_clusters(scope):
        for nf in mnx_common.iter_node_files(cluster):
            try:
                node = mnx_common.parse_node(nf)
            except Exception as exc:
                out[str(nf)] = {"_parse_error": str(exc), "_path": str(nf)}
                continue
            if node.get("id"):
                node["_cluster"] = str(Path(cluster).resolve())
                out[node["id"]] = node
    return out


def _team_of(root: Path, cluster: str) -> str:
    return mnx_common.team_of(root, cluster) or Path(cluster).name


def _boundary_rows(scope: str) -> dict[str, list[dict[str, str]]]:
    """Expected cross-links rows per team: same-team edges crossing cluster boundaries."""
    root = mnx_common.require_graph_root(scope)
    nodes = _all_nodes(scope)
    per_team: dict[str, list[dict[str, str]]] = {}
    for nid, node in nodes.items():
        if "_cluster" not in node:
            continue
        from_cluster = node["_cluster"]
        from_team = _team_of(root, from_cluster)
        for e in node.get("edges") or []:
            to_id = e.get("to") if isinstance(e, dict) else None
            tgt = nodes.get(to_id)
            if not tgt or "_cluster" not in tgt:
                continue
            if tgt["_cluster"] == from_cluster:
                continue  # intra-cluster
            if _team_of(root, tgt["_cluster"]) != from_team:
                continue  # cross-team → soft reference, not cross-links
            team_dir = root / from_team
            per_team.setdefault(from_team, []).append({
                "from_id": nid,
                "from_path": str(Path(node["_path"]).resolve().relative_to(team_dir.resolve())),
                "type": e.get("type", ""),
                "to_id": to_id,
                "to_path": str(Path(tgt["_path"]).resolve().relative_to(team_dir.resolve())),
            })
    return per_team


def check(scope: str) -> dict[str, Any]:
    """Read-only. Run the invariant suite; return {findings: [...]}."""
    findings: list[dict[str, Any]] = []

    def add(inv, sev, target, detail):
        findings.append({"invariant": inv, "severity": sev, "node_or_edge": target, "detail": detail})

    root = mnx_common.require_graph_root(scope)
    cfg = mnx_config.load(str(root))
    nodes = _all_nodes(scope)
    live_ids = {nid for nid, n in nodes.items() if n.get("status") != "dead" and "_parse_error" not in n}
    all_ids = {nid for nid, n in nodes.items() if "_parse_error" not in n}
    reverse = mnx_resolve.build_reverse_map(scope)

    # Schema (6, 7) + parse errors
    for nid, node in nodes.items():
        if "_parse_error" in node:
            add(6, "E", node["_path"], f"unparseable node: {node['_parse_error']}")
            continue
        if not mnx_common.is_valid_id(nid):
            add(6, "E", nid, "id is not a valid slug ([a-z0-9-]+)")
        if node.get("type") not in VALID_TYPES:
            add(6, "E", nid, f"invalid type: {node.get('type')!r}")
        if node.get("status") not in VALID_STATUS:
            add(6, "E", nid, f"invalid status: {node.get('status')!r}")
        if node.get("type") == "pattern" and not node.get("trigger"):
            add(6, "E", nid, "pattern node missing required non-null trigger")
        for ts_field in ("created", "updated"):
            if ts_field in node and not mnx_common.is_iso_utc(str(node[ts_field])):
                add(6, "E", nid, f"{ts_field} is not UTC ISO-8601: {node[ts_field]!r}")
        stem = Path(node["_path"]).stem
        if stem != nid:
            add(7, "E", nid, f"id does not match filename stem {stem!r} (ids never change)")
        # Node-size budget (14): completeness-of-atom, never truncate — over budget → split into
        # multiple nodes + an edge. Advisory (W), enforced at promote/apply time.
        body = node.get("_body") or ""
        if len(body) > int(cfg.get("node_body_max_chars", 6000)):
            add(14, "W", nid, f"node body {len(body)} chars > node_body_max_chars "
                              f"{cfg.get('node_body_max_chars', 6000)} (split into nodes + an edge)")

    # Referential integrity (1, 2, 13) + reverse-map consistency (3)
    for nid, node in nodes.items():
        if "_parse_error" in node:
            continue
        for e in node.get("edges") or []:
            to_id = e.get("to") if isinstance(e, dict) else None
            if to_id not in all_ids:
                add(1, "E", f"{nid}->{to_id}", "edge target does not exist")
            elif to_id not in live_ids:
                add(2, "E", f"{nid}->{to_id}", "live edge points at a tombstoned node (repoint to superseded-by)")
            if to_id in all_ids and nid not in reverse.get(to_id, []):
                add(3, "E", f"{nid}->{to_id}", "reverse map missing this edge")
        # Orphan flag (13): live node with zero inbound edges
        if nid in live_ids and not reverse.get(nid):
            add(13, "I", nid, "node has zero incoming edges (orphan candidate)")
        # Soft references (5)
        for ref in node.get("references") or []:
            if isinstance(ref, dict) and ref.get("to") and ref["to"] not in all_ids:
                add(5, "I", f"{nid}~>{ref.get('to')}", "soft cross-team reference is dangling (no integrity guarantee)")

    # Cross-links completeness + path accuracy (4)
    expected = _boundary_rows(scope)
    for team, rows in expected.items():
        cl = root / team / mnx_common.CROSSLINKS_FILENAME
        actual = mnx_common.parse_md_table(cl.read_text(encoding="utf-8")) if cl.is_file() else []
        exp_keys = {(r["from_id"], r["to_id"], r["type"]) for r in rows}
        act_keys = {(r.get("from_id"), r.get("to_id"), r.get("type")) for r in actual}
        for miss in exp_keys - act_keys:
            add(4, "E", str(miss), f"boundary edge missing from {team}/cross-links.md")
        for stale in act_keys - exp_keys:
            add(4, "E", str(stale), f"stale row in {team}/cross-links.md")

    # Derived-state freshness (8, 9, 10) + tier/budget (11, 12)
    for cluster in mnx_common.iter_clusters(scope):
        idx_path = Path(cluster) / mnx_common.INDEX_FILENAME
        active = {n["id"] for n in nodes.values()
                  if n.get("_cluster") == str(Path(cluster).resolve())
                  and n.get("status") != "dead" and "_parse_error" not in n}
        if not idx_path.is_file():
            if active:
                add(8, "E", str(cluster), "cluster has nodes but no index.md")
            continue
        idx = mnx_common.parse_index(idx_path)  # head only (hot/warm + budget live here)
        # Node-set spans the head AND any continuation chunks of a chained index (invariant 8).
        idx_ids = mnx_index.index_node_ids(str(cluster))
        if idx_ids != active:
            add(8, "E", str(cluster),
                f"index node-set != folder active nodes (only-index={sorted(idx_ids - active)}, "
                f"only-folder={sorted(active - idx_ids)})")
        for d in mnx_index.denorm_check(str(cluster)):
            add(9, "E", d["id"], f"index {d['field']} stale vs node")
        for idx_file in mnx_index._index_files(str(cluster)):
            try:
                fidx = mnx_common.parse_index(idx_file)
            except Exception:
                continue
            for tier in ("hot", "warm", "cold"):
                for r in fidx[tier]:
                    if not r.get("strength") or not r.get("last_update"):
                        add(10, "W", r["id"], "missing materialized strength/last_update in index")
        if len(idx["hot"]) > int(cfg.get("hot_k", 12)):
            add(11, "E", str(cluster), f"hot section length {len(idx['hot'])} > hot_k {cfg.get('hot_k')}")
        if len(active) > int(cfg.get("node_budget", 35)):
            add(12, "W", str(cluster), f"active nodes {len(active)} > node_budget {cfg.get('node_budget')}")

    # Phonebook completeness + path accuracy (18) + org directory (20) + derivability of W1 (17/19)
    team_dirs = sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("team-"))
    for team_dir in team_dirs:
        expected = {e["id"]: e["cluster_path"] for e in mnx_phonebook.entries(str(team_dir))}
        if not expected:
            continue
        pb = team_dir / mnx_phonebook.PHONEBOOK_FILENAME
        if not pb.is_file():
            add(18, "W", team_dir.name, "team has nodes but no phonebook.md (run mnx-doctor --fix)")
            continue
        actual = {r.get("id"): r.get("cluster_path")
                  for r in mnx_common.parse_md_table(pb.read_text(encoding="utf-8")) if r.get("id")}
        for miss in set(expected) - set(actual):
            add(18, "W", miss, f"node missing from {team_dir.name}/phonebook.md")
        for stale in set(actual) - set(expected):
            add(18, "W", stale, f"stale/dead row in {team_dir.name}/phonebook.md")
        for nid in set(expected) & set(actual):
            if expected[nid] != actual[nid]:
                add(18, "W", nid, f"phonebook cluster_path stale ({actual[nid]} != {expected[nid]})")
    # Org directory (20): every team with nodes appears in the org index.
    org = mnx_common.parse_md_table((root / mnx_common.INDEX_FILENAME).read_text(encoding="utf-8")
                                    if (root / mnx_common.INDEX_FILENAME).is_file() else "")
    listed = {r.get("team") for r in org if r.get("team")}
    for team_dir in team_dirs:
        if mnx_phonebook.entries(str(team_dir)) and team_dir.name not in listed:
            add(20, "I", team_dir.name, "team not listed in org directory (root index.md)")
    # Merge-driver registered (W1 keystone): without it, git 3-way-merges generated files.
    if (root / ".git").exists() and not mnx_regen.is_installed(str(root)):
        add(1, "W", str(root), "mnx-regen merge driver not registered "
            "(run: python3 mnx_regen.py install <repo>); derived-file conflicts will not auto-resolve")

    # Telemetry / state (15, 16)
    if mnx_config.changed_since_last_compaction(str(root), cfg):
        add(15, "W", str(root), "config_version/λ differ from last-compaction stamp (re-normalization pending)")
    for team_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("team-")):
        if mnx_lock.in_progress(str(team_dir)) and not mnx_lock.held(str(team_dir)):
            add(16, "E", team_dir.name, "stranded pass.plan.json without an active lock (crash recovery needed)")

    counts = {"E": 0, "W": 0, "I": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {"ok": counts["E"] == 0, "counts": counts, "findings": findings}


def fix(scope: str) -> dict[str, Any]:
    """Regenerate DERIVED files (indexes, cross-links) from the nodes. Idempotent."""
    root = mnx_common.require_graph_root(scope)
    actions: list[dict[str, Any]] = []
    for cluster in mnx_common.iter_clusters(scope):
        mnx_index.regenerate_index(str(cluster))
        actions.append({"regenerated_index": str(cluster)})
    for team, rows in _boundary_rows(scope).items():
        _write_crosslinks(root / team, team, rows)
        actions.append({"regenerated_crosslinks": team, "rows": len(rows)})
    # teams with no boundary edges still get an empty (truthful) cross-links file
    for team_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("team-")):
        if team_dir.name not in _boundary_rows(scope):
            _write_crosslinks(team_dir, team_dir.name, [])
    # Regenerate the W2 phonebook per team + the org directory (also derived from truth).
    for team_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("team-")):
        if mnx_phonebook.entries(str(team_dir)):
            mnx_phonebook.regenerate(str(team_dir))
            actions.append({"regenerated_phonebook": team_dir.name})
    mnx_phonebook.regenerate_org(str(root))
    actions.append({"regenerated_org_directory": True})
    # Register the W1 merge driver if this is a git repo and it is missing.
    if (root / ".git").exists() and not mnx_regen.is_installed(str(root)):
        try:
            mnx_regen.install(str(root))
            actions.append({"installed_merge_driver": True})
        except Exception:
            pass
    after = check(scope)
    return {"action": "fixed", "actions": actions, "post_check": after["counts"]}


def check_staging() -> dict[str, Any]:
    """Optional integrity check of the LOCAL capture staging tier (invariant 17).

    Staging lives outside the graph (per-author, never pushed), so it is checked separately from
    check(scope). Verifies: provisional ids are well-formed and unique; each atom's stored id still
    matches its content hash (untampered); provenance is present (promotable cold); the batch is
    within the hard budget. Read-only. Never raises — a missing/empty staging tier is clean."""
    findings: list[dict[str, Any]] = []

    def add(sev, target, detail):
        findings.append({"invariant": 17, "severity": sev, "node_or_edge": target, "detail": detail})

    try:
        import mnx_binding
        import mnx_stage
        binding = mnx_binding.resolve()
        if binding is None:
            return {"ok": True, "counts": {"E": 0, "W": 0, "I": 0}, "findings": [],
                    "note": "no graph configured"}
        atoms = mnx_stage._all_atoms(binding)
        st = mnx_stage.status(binding)
    except Exception as exc:
        return {"ok": True, "counts": {"E": 0, "W": 0, "I": 0}, "findings": [],
                "note": f"staging not inspectable: {exc}"}

    seen: set[str] = set()
    for a in atoms:
        pid = a.get("provisional_id")
        path = a.get("_path")
        if not pid or not str(pid).startswith(mnx_stage.ID_PREFIX):
            add("E", path, "staged atom has missing/invalid provisional id")
            continue
        if pid in seen:
            add("E", pid, "duplicate provisional id across staged atoms")
        seen.add(pid)
        try:
            recomputed = mnx_stage.provisional_id({**a, "body": a.get("_body", "")})
            if recomputed != pid:
                add("E", pid, f"provisional id does not match content hash ({recomputed}); atom tampered")
        except Exception:
            pass
        prov = a.get("provenance") or {}
        if not (prov.get("artifact") or prov.get("rationale")):
            add("W", pid, "staged atom has no artifact/rationale provenance — may not promote cold")
    if st.get("budget", {}).get("level") == "hard":
        add("W", st.get("staging_root"), "staging is over its hard budget — run /mnemex:mnx-promote")

    counts = {"E": 0, "W": 0, "I": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {"ok": counts["E"] == 0, "counts": counts, "findings": findings, "staging": st}


def _write_crosslinks(team_dir: Path, team: str, rows: list[dict[str, str]]) -> None:
    L = [f"# cross-links: {team}   (generated — regenerated by mnx-doctor; delta-updated by write/gc)",
         "| from_id | from_path | type | to_id | to_path |",
         "|---------|-----------|------|-------|---------|"]
    for r in sorted(rows, key=lambda x: (x["from_id"], x["to_id"])):
        L.append(f"| {r['from_id']} | {r['from_path']} | {r['type']} | {r['to_id']} | {r['to_path']} |")
    (team_dir / mnx_common.CROSSLINKS_FILENAME).write_text("\n".join(L) + "\n", encoding="utf-8")


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    try:
        scope = argv[2] if len(argv) > 2 else "."
        if cmd == "check":
            rep = check(scope)
            return mnx_common.emit(rep, ok=rep["ok"])
        if cmd == "fix":
            return mnx_common.emit(fix(scope))
        if cmd == "check-staging":
            rep = check_staging()
            return mnx_common.emit(rep, ok=rep["ok"])
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
