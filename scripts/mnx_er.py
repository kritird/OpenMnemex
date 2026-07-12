"""mnx_er.py — entity resolution for bulk ingest (block → score → cluster → dispose). PURE PROPOSER.

Background: docs/corpus-ingestion.md §9, INGESTION-BUILD-PLAN.md phase A2.

A corpus is redundant by nature — the same fact in a README, a design doc, and a code comment. Episodic
promote dedups with the phonebook + simindex, right for a handful of atoms; a corpus needs an explicit
entity-resolution stage: the industry-standard **blocking → pairwise scoring → clustering** pipeline
(Fellegi-Sunter). This script sequences the pieces we already ship and **writes nothing** — it proposes
a disposition (CREATE / MERGE / COLLAPSE) per cluster plus a `possible` HITL band; reconcile/HITL disposes.

The Fellegi-Sunter three-way split maps to the HITL gate:
    score ≥ match     → same entity (deterministic)
    possible ≤ s < match → the `possible` band → ⚠ suggested at gate #2 (the ONLY place the LLM judge runs)
    score < possible  → distinct

One entity → one node (DP5): intra-batch duplicates collapse BEFORE staging; a cluster that matches an
existing page MERGEs into it. Runs once per delta batch over {new atoms ∪ existing graph pages}.

Dependencies: Python 3.9+ stdlib + PyYAML (via mnx_common). Imports mnx_simindex (the blocker) + mnx_common.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

import mnx_common
import mnx_simindex

MATCH_DEFAULT = 0.85
POSSIBLE_DEFAULT = 0.60
BLOCK_THRESHOLD = 0.20   # low → the blocker over-generates candidates; the weighted score filters

# Feature weights (docs/corpus-ingestion.md §9 / plan §2.5.5).
W_ALIAS, W_SUMMARY, W_DOMAIN, W_LINK = 0.4, 0.3, 0.2, 0.1

_STOP = {"the", "a", "an", "of", "to", "and", "or", "is", "in", "on", "for", "by", "with", "at"}


def _words(text: str) -> set[str]:
    return {w for w in re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()
            if len(w) >= 2 and w not in _STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _norm_alias(s: str) -> str:
    """Whole-alias normal form for exact-identity matching (mirrors mnx_phonebook's exact-alias
    semantics): lowercase, punctuation folded to single spaces. Unlike `_words` this keeps the
    alias as ONE string — 'ILPv4' == 'ilpv4', but 'ILP address' != 'address'."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# --- item model (a staged atom or a graph node, uniformly) ------------------

def _atom_item(a: dict[str, Any]) -> dict[str, Any]:
    aid = a.get("id") or a.get("provisional_id")
    aliases = a.get("aliases") or []
    links = {m.get("resolved_id") for m in (a.get("mentions") or []) if m.get("resolved_id")}
    return {"id": aid, "graph": False,
            "summary": str(a.get("summary", "")),
            "aliases": [str(x) for x in aliases],
            "domain": {str(d).lower() for d in (a.get("domain") or [])},
            "links": {str(x) for x in links},
            "name_tokens": _words(" ".join(str(x) for x in aliases)) or _words(a.get("summary", "")),
            "summary_tokens": _words(a.get("summary", ""))}


def _graph_items(graph: str, team: Optional[str]) -> dict[str, dict[str, Any]]:
    scope = Path(graph)
    if team:
        scope = scope / team
    out: dict[str, dict[str, Any]] = {}
    for cl in mnx_common.iter_clusters(scope):
        cname = Path(cl).name
        for nf in mnx_common.iter_node_files(cl):
            try:
                node = mnx_common.parse_node(nf)
            except Exception:
                continue
            nid = node.get("id")
            if not nid or node.get("status") == "dead":
                continue
            aliases = node.get("aliases") or []
            domain = node.get("domain") or [cname]
            if isinstance(domain, str):
                domain = [domain]
            links = {e.get("id") if isinstance(e, dict) else e for e in (node.get("edges") or [])}
            out[nid] = {"id": nid, "graph": True,
                        "summary": str(node.get("summary", "")),
                        "aliases": [str(x) for x in aliases],
                        "domain": {str(d).lower() for d in domain},
                        "links": {str(x) for x in links if x},
                        "name_tokens": _words(" ".join(str(x) for x in aliases)) or _words(node.get("summary", "")),
                        "summary_tokens": _words(node.get("summary", ""))}
    return out


def score_pair(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Weighted Fellegi-Sunter-style match score for a blocked pair."""
    alias = _jaccard(a["name_tokens"], b["name_tokens"])
    summ = _jaccard(a["summary_tokens"], b["summary_tokens"])
    dom = 1.0 if (a["domain"] & b["domain"]) else 0.0
    link = 1.0 if (a["links"] & b["links"]) else 0.0
    return W_ALIAS * alias + W_SUMMARY * summ + W_DOMAIN * dom + W_LINK * link


# --- union-find --------------------------------------------------------------

class _UF:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: str, y: str) -> None:
        self.parent[self.find(x)] = self.find(y)


# --- resolve -----------------------------------------------------------------

def resolve(graph: str, atoms: list[dict[str, Any]], team: Optional[str] = None,
            match: float = MATCH_DEFAULT, possible: float = POSSIBLE_DEFAULT) -> dict[str, Any]:
    """Block → score → cluster → dispose over {staged atoms ∪ existing graph pages}. Writes nothing."""
    items: dict[str, dict[str, Any]] = _graph_items(graph, team)
    staged_ids: list[str] = []
    for a in atoms or []:
        it = _atom_item(a)
        if not it["id"]:
            continue
        items[it["id"]] = it
        staged_ids.append(it["id"])

    # BLOCK — reuse the simindex blocker over {graph ∪ staged}, intra so intra-batch dups surface.
    blocked = mnx_simindex.pairs(graph, threshold=BLOCK_THRESHOLD, with_atoms=atoms, intra=True)
    cand = {(p["a"], p["b"]) for p in blocked["candidate_pairs"]}
    # Also consider every staged↔staged and staged↔graph pair directly (the blocker is recall-bounded;
    # the weighted score is the real gate, so widen candidates cheaply for the small delta batch).
    for i, sid in enumerate(staged_ids):
        for other in list(items):
            if other != sid:
                cand.add(tuple(sorted((sid, other))))

    # SCORE + split
    uf = _UF()
    for sid in staged_ids:
        uf.find(sid)

    # NAME IDENTITY (deterministic, before any scoring): if item A carries an alias that IS
    # what item B is called (B's normalized id), they are the same entity. The weighted token
    # score alone misses alias-SUBSET duplicates across clusters (E2E 2026-07-12, finding G7:
    # entity "ilpv4" vs "interledger-protocol-v4" whose alias list contains "ILPv4" scored below
    # the possible band — token jaccard diluted by the richer alias set, domain overlap 0).
    # Keyed on the ID, not any shared alias: two FACT atoms tagged with the same topical alias
    # ("field 124") are not thereby one entity. stg- content-hash ids never normalize to an
    # alias, so this only fires for named entity candidates and graph pages. Graph∧graph pairs
    # stay out (mirrors the skip in the scoring loop below).
    id_of: dict[str, str] = {iid: _norm_alias(str(iid)) for iid in items}
    by_norm_id: dict[str, list[str]] = {}
    for iid, key in id_of.items():
        if key:
            by_norm_id.setdefault(key, []).append(iid)
    for iid, it in items.items():
        for al in it["aliases"]:
            for named in by_norm_id.get(_norm_alias(al), []):
                if named == iid or (items[named]["graph"] and it["graph"]):
                    continue
                uf.union(iid, named)
    possible_pairs: list[dict[str, Any]] = []
    pair_score: dict[tuple[str, str], float] = {}
    for a_id, b_id in cand:
        if a_id not in items or b_id not in items:
            continue
        if items[a_id]["graph"] and items[b_id]["graph"]:
            continue  # ER acts on the delta batch; two existing pages are not re-merged here
        s = score_pair(items[a_id], items[b_id])
        pair_score[tuple(sorted((a_id, b_id)))] = s
        if s >= match:
            uf.union(a_id, b_id)
        elif s >= possible:
            possible_pairs.append({"a": a_id, "b": b_id, "score": round(s, 3)})

    # CLUSTER
    groups: dict[str, list[str]] = {}
    for sid in staged_ids:
        groups.setdefault(uf.find(sid), []).append(sid)
    # pull graph members that got unioned into a staged cluster
    for gid, it in items.items():
        if it["graph"] and gid in uf.parent:
            root = uf.find(gid)
            if root in groups:
                groups[root].append(gid)

    clusters = []
    counts = {"create": 0, "merge": 0, "collapse": 0, "possible": 0}
    for members in groups.values():
        graph_members = [m for m in members if items[m]["graph"]]
        staged_members = [m for m in members if not items[m]["graph"]]
        if not staged_members:
            continue
        aliases = _union_aliases(items, members)
        if graph_members:
            target = max(graph_members, key=len)  # longest existing graph-id
            disposition, canonical = "MERGE", target
            counts["merge"] += 1
        elif len(staged_members) > 1:
            disposition, target = "COLLAPSE", None
            canonical = _canonical_slug(items, staged_members)
            counts["collapse"] += 1
        else:
            disposition, target = "CREATE", None
            canonical = _canonical_slug(items, staged_members)
            counts["create"] += 1
        confidence = _cluster_confidence(members, pair_score)
        clusters.append({"canonical": canonical, "members": sorted(staged_members),
                         "aliases": aliases, "disposition": disposition,
                         "target_id": target, "confidence": round(confidence, 3)})

    # possible band: dedupe, drop pairs already merged into the same cluster
    seen = set()
    dedup_possible = []
    for p in sorted(possible_pairs, key=lambda x: -x["score"]):
        key = tuple(sorted((p["a"], p["b"])))
        if key in seen:
            continue
        if uf.parent.get(p["a"]) and uf.find(p["a"]) == uf.find(p["b"]):
            continue
        seen.add(key)
        dedup_possible.append(p)
    counts["possible"] = len(dedup_possible)

    clusters.sort(key=lambda c: (c["disposition"], c["canonical"]))
    return {"clusters": clusters, "possible": dedup_possible, "counts": counts}


def _union_aliases(items: dict[str, dict[str, Any]], members: list[str]) -> list[str]:
    seen, out = set(), []
    for m in members:
        for al in items[m]["aliases"]:
            k = al.strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(al.strip())
    return out


def _canonical_slug(items: dict[str, dict[str, Any]], staged_members: list[str]) -> str:
    best = max(staged_members, key=lambda m: len(items[m]["summary"]))
    return mnx_common.slugify(items[best]["summary"][:60]) or mnx_common.slugify(best)


def _cluster_confidence(members: list[str], pair_score: dict[tuple[str, str], float]) -> float:
    if len(members) < 2:
        return 1.0
    scores = [pair_score.get(tuple(sorted((members[i], members[j]))), 0.0)
              for i in range(len(members)) for j in range(i + 1, len(members))]
    scores = [s for s in scores if s > 0]
    return max(scores) if scores else 1.0


# --- cli --------------------------------------------------------------------

def _arg(argv: list[str], flag: str) -> Optional[str]:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "resolve":
            atoms_path = _arg(argv, "--atoms")
            data = json.loads(open(atoms_path, encoding="utf-8").read()) if atoms_path else []
            atoms = data.get("atoms", data) if isinstance(data, dict) else data
            return mnx_common.emit(resolve(
                _arg(argv, "--graph") or ".", atoms, _arg(argv, "--team"),
                float(_arg(argv, "--match") or MATCH_DEFAULT),
                float(_arg(argv, "--possible") or POSSIBLE_DEFAULT)))
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
