"""mnx_phonebook.py — the team link-resolution catalog + org directory (DERIVED).

See docs/link-reconciliation.md §3 (W2).

The phonebook is how a *blind* author forms a link. An author/atom mentions a target by
NAME/alias (like a wiki `[[…]]`), never by file path. Reconcile resolves that name against
the **team phonebook** — a denormalized rollup of every active node in the team
(`id · aliases · summary · cluster_path · tier · status`) — and writes a hard edge on the
SOURCE node. The referred-to node is never touched.

Scope rule (the scaling property): **resolution scope = edge scope.** Hard edges live inside
a team, so the workhorse phonebook is TEAM-sized; cross-team is soft, so the org level keeps
only a coarse directory (teams → domains, NOT nodes). There is deliberately no org-wide node
phonebook — that would be the global write-path index the architecture rejects.

DERIVED: regenerated from the cluster indexes (which are themselves derived from the nodes);
never hand-edited, never 3-way-merged (W1's `mnx-regen` driver regenerates it on conflict).

Python 3.9+, stdlib + PyYAML only. Imports mnx_common only.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Optional

import mnx_common

PHONEBOOK_FILENAME = "phonebook.md"
DEAD = "dead"


def _team_root(team: str) -> Path:
    """Accept a team folder path (…/team-payments) and return it resolved."""
    p = Path(team).resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"not a team folder: {team}")
    return p


def _tier_by_id(cluster: Path) -> dict[str, str]:
    """id → tier ('hot'|'warm'|'cold') from the cluster's head index. Nodes not in the head
    (only in cold continuation chunks) default to 'cold' — which is what they are."""
    out: dict[str, str] = {}
    idx_path = cluster / mnx_common.INDEX_FILENAME
    if not idx_path.is_file():
        return out
    try:
        idx = mnx_common.parse_index(idx_path)
    except Exception:
        return out
    for tier in ("hot", "warm", "cold"):
        for row in idx[tier]:
            if row.get("id"):
                out[row["id"]] = tier
    return out


def entries(team: str) -> list[dict[str, Any]]:
    """Every active node in the team as a phonebook row (sorted by cluster then id)."""
    root = _team_root(team)
    rows: list[dict[str, Any]] = []
    for cluster in mnx_common.iter_clusters(root):
        tiers = _tier_by_id(cluster)
        rel = str(Path(cluster).resolve().relative_to(root))
        for nf in mnx_common.iter_node_files(cluster):
            try:
                node = mnx_common.parse_node(nf)
            except Exception:
                continue
            nid = node.get("id")
            if not nid or node.get("status") == DEAD:
                continue
            rows.append({
                "id": nid,
                "aliases": mnx_common.aliases_to_index(node.get("aliases")),
                "summary": str(node.get("summary", "")),
                "cluster_path": rel,
                "tier": tiers.get(nid, "cold"),
                "status": node.get("status", "active"),
            })
    rows.sort(key=lambda r: (r["cluster_path"], r["id"]))
    return rows


def _esc(s: str) -> str:
    return mnx_common.escape_cell(s)


def regenerate(team: str) -> dict[str, Any]:
    """Write team-<name>/phonebook.md from the team's cluster indexes/nodes. Returns a summary."""
    root = _team_root(team)
    rows = entries(root)
    L = [f"# phonebook: {root.name}   (generated — do not edit; merge=mnx-regen)",
         "| id | aliases | summary | cluster_path | tier | status |",
         "|----|---------|---------|--------------|------|--------|"]
    for r in rows:
        L.append(f"| {r['id']} | {_esc(r['aliases'])} | {_esc(r['summary'])} | "
                 f"{r['cluster_path']} | {r['tier']} | {r['status']} |")
    L += ["",
          "<!-- GENERATED. Team-scoped link-resolution catalog. Regenerated at consolidate "
          "and by mnx-doctor --fix. -->", ""]
    (root / PHONEBOOK_FILENAME).write_text("\n".join(L), encoding="utf-8")
    return {"action": "regenerated", "team": root.name, "rows": len(rows),
            "path": str(root / PHONEBOOK_FILENAME)}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _supersede_map(team: str) -> dict[str, str]:
    """norm(old id or alias) → final LIVE successor id, for every superseded (dead) node in the team.

    A SUPERSEDE retires the old node (status dead + superseded-by=<new>) but leaves referrer bodies
    still saying `[[old-name]]`. The old id is absent from the active phonebook, so a naive resolve
    returns null and the mesh drops the edge to a red-link (F8). This map lets resolve FORWARD a
    superseded name to its replacement instead. Supersede chains (v1→v2→v3) are collapsed to the final
    live successor; a chain that dead-ends at a still-dead node forwards to that terminal id (resolve
    then declines it, since it is not in the active rows — the doctor's inv-2 backstop still applies).
    """
    root = _team_root(team)
    direct: dict[str, str] = {}          # norm(old_id) → immediate successor id
    alias_to_old: dict[str, str] = {}    # norm(alias)  → old_id (dead node's own aliases)
    for cluster in mnx_common.iter_clusters(root):
        for nf in mnx_common.iter_node_files(cluster):
            try:
                node = mnx_common.parse_node(nf)
            except Exception:
                continue
            nid = node.get("id")
            succ = node.get("superseded-by")
            if node.get("status") != DEAD or not (nid and succ):
                continue
            direct[_norm(nid)] = str(succ)
            aliases = node.get("aliases")
            if isinstance(aliases, str):
                aliases = mnx_common.aliases_from_index(aliases)
            for a in (aliases or []):
                alias_to_old.setdefault(_norm(str(a)), nid)
    out: dict[str, str] = {}
    for old_norm, succ in direct.items():
        seen = {old_norm}
        cur = succ
        while _norm(cur) in direct and _norm(cur) not in seen:
            seen.add(_norm(cur))
            cur = direct[_norm(cur)]
        out[old_norm] = cur
    for a_norm, old_id in alias_to_old.items():
        if _norm(old_id) in out:
            out.setdefault(a_norm, out[_norm(old_id)])
    return out


def resolve(name: str, team: str) -> dict[str, Any]:
    """Resolve a name/alias mention to a node id against the team phonebook.

    Deterministic, exact-first: (1) exact id; (2) exact alias; (3) exact summary-normalized;
    (4) superseded-by forwarding (a dead node's id/alias → its live successor); (5) substring/
    alias-token overlap as ranked candidates. Returns {resolved: id|None, cluster_path, match,
    candidates:[…]}; a superseded forward is marked match="superseded-by" + forwarded_from=<old>.
    Fuzzy/semantic near-misses are NOT this script's job — that is mnx_simindex (W8), consulted only
    when this returns no exact.
    """
    rows = _read_phonebook(team)
    q = _norm(name)
    qid = (name or "").strip()
    candidates: list[dict[str, Any]] = []
    for r in rows:
        aliases = [_norm(a) for a in mnx_common.aliases_from_index(r.get("aliases", ""))]
        if r["id"] == qid:
            return _hit(r, "id", rows)
        if q and q in aliases:
            return _hit(r, "alias", rows)
        if q and q == _norm(r.get("summary", "")):
            return _hit(r, "summary", rows)
        # candidate scoring: token overlap on aliases+summary
        hay = set(_norm(r.get("summary", "")).split()) | set(
            t for a in aliases for t in a.split())
        overlap = len(set(q.split()) & hay)
        if overlap:
            candidates.append({"id": r["id"], "cluster_path": r["cluster_path"],
                               "tier": r["tier"], "overlap": overlap})
    # No exact ACTIVE match. Forward a superseded (dead) name to its live successor so the mesh
    # repoints the edge instead of dropping it to a red-link (F8).
    fwd = _supersede_map(team)
    succ = fwd.get(qid) or (fwd.get(q) if q else None)
    if succ:
        for r in rows:
            if r["id"] == succ:  # successor must be active (present in the phonebook)
                hit = _hit(r, "superseded-by", rows)
                hit["forwarded_from"] = qid or name
                return hit
    candidates.sort(key=lambda c: (-c["overlap"], c["tier"] != "hot", c["id"]))
    return {"resolved": None, "match": None, "name": name,
            "candidates": candidates[:5],
            "red_link": not candidates,
            "note": ("no exact match; try mnx_simindex (W8) for fuzzy, else RED LINK "
                     "(record demand / HITL create-or-pick)")}


def _hit(row: dict[str, Any], how: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"resolved": row["id"], "cluster_path": row["cluster_path"],
            "tier": row["tier"], "match": how, "candidates": []}


def _read_phonebook(team: str) -> list[dict[str, Any]]:
    """Read the generated phonebook; regenerate in-memory if missing/stale-tolerant."""
    root = _team_root(team)
    p = root / PHONEBOOK_FILENAME
    if not p.is_file():
        return entries(root)  # derive on demand; never a hard dependency on the file existing
    secless = p.read_text(encoding="utf-8")
    return mnx_common.parse_md_table(secless)


def resolve_batch(names: list[str], team: str) -> dict[str, Any]:
    """Resolve many names at once against the team phonebook (Link Reconciliation Phase L1).

    Returns {resolved:{name:id}, red:[name], candidates:{name:[…]}}. Cross-team routing is left to
    the caller (org directory); this is the team-scoped exact resolver.
    """
    resolved: dict[str, str] = {}
    red: list[str] = []
    cand: dict[str, Any] = {}
    for n in names:
        r = resolve(n, team)
        if r.get("resolved"):
            resolved[n] = r["resolved"]
        else:
            red.append(n)
            if r.get("candidates"):
                cand[n] = r["candidates"]
    return {"resolved": resolved, "red": red, "candidates": cand}


def _iter_active_nodes(team: str):
    """Yield (node, cluster_rel) for every active node in the team."""
    root = _team_root(team)
    for cluster in mnx_common.iter_clusters(root):
        rel = str(Path(cluster).resolve().relative_to(root))
        for nf in mnx_common.iter_node_files(cluster):
            try:
                node = mnx_common.parse_node(nf)
            except Exception:
                continue
            if node.get("status") == DEAD:
                continue
            yield node, rel


def red_links(team: str) -> list[dict[str, Any]]:
    """Every outstanding red-link in the team (Link Reconciliation §3): a node `mentions[]` entry whose
    `resolved_id` is null — an authored [[name]] that has no page yet. Returns
    [{source_id, source_path, name, type}]. Never errors on a node without mentions.
    """
    out: list[dict[str, Any]] = []
    for node, _rel in _iter_active_nodes(team):
        for m in node.get("mentions") or []:
            if isinstance(m, dict) and not m.get("resolved_id") and m.get("name"):
                out.append({"source_id": node.get("id"), "source_path": node.get("_path"),
                            "name": m["name"], "type": m.get("type")})
    return out


def backfill(team: str, new_id: str, aliases: Any = None) -> list[dict[str, Any]]:
    """Red-links that a newly-introduced page id (+ its aliases) now resolves (Link Reconciliation §3 / Phase L2).

    Matches a red-link's normalized name against the new id or any alias. Returns the source notes
    whose red-link should turn live: [{source_id, source_path, name, type}]. Deterministic; the
    caller writes the back-link onto each source note under the lock.
    """
    keys = {_norm(new_id)}
    if isinstance(aliases, str):
        aliases = mnx_common.aliases_from_index(aliases)
    for a in (aliases or []):
        keys.add(_norm(str(a)))
    keys.discard("")
    return [rl for rl in red_links(team) if _norm(rl["name"]) in keys]


def regenerate_org(graph_root: str) -> dict[str, Any]:
    """Regenerate the org directory (root index.md): teams → domains + summary. COARSE — never
    lists nodes. Used only to route a CROSS-team mention to a candidate team for a soft reference.
    """
    root = mnx_common.require_graph_root(graph_root)
    teams = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or not d.name.startswith("team-"):
            continue
        domains = sorted({rel.parts[0] for c in mnx_common.iter_clusters(d)
                          for rel in [c.resolve().relative_to(d)] if rel.parts})
        summary = ""
        ti = d / mnx_common.INDEX_FILENAME
        if ti.is_file():
            try:
                summary = mnx_common.parse_index(ti).get("description", "")
            except Exception:
                summary = ""
        teams.append({"team": d.name, "domains": list(domains), "summary": summary})
    L = ["# org index   (generated — coarse cross-team directory; merge=mnx-regen)",
         "| team | domains | summary |", "|------|---------|---------|"]
    for t in teams:
        L.append(f"| {t['team']} | {_esc(', '.join(t['domains']))} | {_esc(t['summary'])} |")
    L += ["", "<!-- GENERATED. Routes cross-team mentions to a candidate team (soft references "
          "only). Never lists nodes. -->", ""]
    (root / mnx_common.INDEX_FILENAME).write_text("\n".join(L), encoding="utf-8")
    return {"action": "regenerated", "teams": len(teams), "path": str(root / mnx_common.INDEX_FILENAME)}


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "regenerate":
            return mnx_common.emit(regenerate(argv[2]))
        if cmd == "regenerate-org":
            return mnx_common.emit(regenerate_org(argv[2]))
        if cmd == "entries":
            return mnx_common.emit({"team": argv[2], "entries": entries(argv[2])})
        if cmd == "resolve":
            # resolve <name> <team>
            res = resolve(argv[2], argv[3])
            return mnx_common.emit(res, ok=res.get("resolved") is not None or bool(res.get("candidates")))
        if cmd == "red-links":
            # red-links <team>
            return mnx_common.emit({"team": argv[2], "red_links": red_links(argv[2])})
        if cmd == "backfill":
            # backfill <team> <new_id> [aliases;semicolon;list]
            aliases = argv[4] if len(argv) > 4 else None
            return mnx_common.emit({"team": argv[2], "new_id": argv[3],
                                    "backfill": backfill(argv[2], argv[3], aliases)})
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
