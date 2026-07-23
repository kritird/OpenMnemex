"""mnx_mesh.py — Step 2b link reconciliation: the wiki mesh, built at promote.

See docs/link-reconciliation.md.

Two deterministic halves, mirroring the consolidate pass (MARK → SWEEP):

  * plan_links(notes, team)  — PURE / read-only. Resolves each note's inline [[wiki-links]] against
    the team phonebook (+ an in-batch catalog for pages created in the same promote), keeps a
    red-link for any [[name]] with no page yet, and back-fills older notes whose outstanding
    red-links the new/renamed pages now satisfy. Writes NOTHING; returns a link plan.

  * apply_links(plan, team) — writes the plan under the team lock: mirrors each note's resolved
    links into its front-matter `edges:` (the generated mirror the reverse map / cross-links /
    structural strength consume) and records outstanding red-links in `mentions:`.

Wiki-first (Link Reconciliation): body [[links]] are the source of truth; `edges:` is a generated mirror; links
are untyped by default; a link to a page that does not exist yet is a red-link, healed lazily when
that page is created. NON-authoritative fuzzy suggestions (mnx_simindex) are surfaced to the skill
for HITL, never written here.

Python 3.10+, stdlib + PyYAML. Imports mnx_common + mnx_phonebook.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Optional

import mnx_common
import mnx_phonebook

_NEW_DISPOSITIONS = {"create", "resurrect", "merge", "update"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _batch_catalog(notes: list[dict[str, Any]]) -> dict[str, str]:
    """norm(id or alias) → id for every note in THIS promote batch, so a note may link to a sibling
    page being created in the same cycle (not yet in the phonebook)."""
    cat: dict[str, str] = {}
    for n in notes:
        nid = n.get("id")
        if not nid:
            continue
        cat[_norm(nid)] = nid
        aliases = n.get("aliases")
        if isinstance(aliases, str):
            aliases = mnx_common.aliases_from_index(aliases)
        for a in (aliases or []):
            cat.setdefault(_norm(str(a)), nid)
    return cat


def plan_links(notes: list[dict[str, Any]], team: str) -> dict[str, Any]:
    """Propose the link plan for a promote batch. PURE — reads the frozen graph, writes nothing.

    `notes`: [{id, cluster_path?, body, aliases?, disposition?}] — the post-disposition notes being
    written this cycle. Returns:
      { links:[{source_id, to, type, origin}],       # resolved wiki-links to write on the source note
        red_links:[{source_id, name, type}],         # [[name]] with no page yet — kept latent
        backlinks:[{source_id, to, name, type, origin}],  # older notes healed by a new/renamed page
        sources:[id, ...] }  # batch notes whose FULL body was parsed → authoritative for apply's re-derive
    """
    catalog = _batch_catalog(notes)
    links: list[dict[str, Any]] = []
    red: list[dict[str, Any]] = []
    seen_link: set[tuple[str, str]] = set()

    # --- Phase L1: outbound — resolve THIS note's [[wiki-links]] ---
    for note in notes:
        nid = note.get("id")
        if not nid:
            continue
        typed = {_norm(m.get("name", "")): m.get("type")
                 for m in (note.get("mentions") or []) if isinstance(m, dict)}
        for wl in mnx_common.parse_wikilinks(note.get("body") or ""):
            name = wl["name"]
            r = mnx_phonebook.resolve(name, team)
            to = r.get("resolved") or catalog.get(_norm(name))
            ltype = typed.get(_norm(name))
            # a superseded name resolves to its live successor → mark the repoint so the skill can
            # optionally rewrite the body [[old]]→[[new]] (F8); the edge is already forwarded here.
            origin = "supersede-repoint" if r.get("match") == "superseded-by" else "wikilink"
            if to and _norm(to) != _norm(nid):
                key = (nid, _norm(to))
                if key not in seen_link:
                    seen_link.add(key)
                    # Always carry the body's surface form: apply mirrors it into mentions[].name,
                    # and doctor inv-22 requires every mention to trace back to a body [[link]].
                    # Without it apply falls back to the target id — a phantom whenever the link
                    # resolved via an alias that differs from the id (E2E 2026-07-12, finding G12:
                    # [[on-ledger escrow]] resolved to on-ledger-holds left a phantom mention).
                    link = {"source_id": nid, "to": to, "type": ltype, "origin": origin,
                            "name": name}
                    if origin == "supersede-repoint":
                        link["forwarded_from"] = r.get("forwarded_from") or name
                    links.append(link)
            elif not to:
                red.append({"source_id": nid, "name": name, "type": ltype})

    # --- Phase L2: inbound — back-fill older notes the new/renamed pages now satisfy ---
    backlinks: list[dict[str, Any]] = []
    batch_ids = {_norm(n.get("id")) for n in notes if n.get("id")}
    for note in notes:
        nid = note.get("id")
        disp = (note.get("disposition") or "create").lower()
        if not nid or disp not in _NEW_DISPOSITIONS:
            continue
        for rl in mnx_phonebook.backfill(team, nid, note.get("aliases")):
            # a sibling in THIS batch is handled by outbound resolution already; skip to avoid dup
            if _norm(rl.get("source_id")) in batch_ids:
                continue
            key = (rl["source_id"], _norm(nid))
            if key in seen_link:
                continue
            seen_link.add(key)
            backlinks.append({"source_id": rl["source_id"], "to": nid,
                              "name": rl["name"], "type": rl.get("type"), "origin": "backfill"})

    sources = [n["id"] for n in notes if n.get("id")]
    return {"links": links, "red_links": red, "backlinks": backlinks, "sources": sources,
            "counts": {"links": len(links), "red_links": len(red), "backlinks": len(backlinks)}}


# --- apply (SWEEP; lock-gated in the promote flow) --------------------------

def _id_to_path(team: str) -> dict[str, str]:
    root = mnx_phonebook._team_root(team)
    out: dict[str, str] = {}
    for cluster in mnx_common.iter_clusters(root):
        for nf in mnx_common.iter_node_files(cluster):
            try:
                node = mnx_common.parse_node(nf)
            except Exception:
                continue
            if node.get("id"):
                out[node["id"]] = str(nf)
    return out


def _write_node_fm(path: str, mutate) -> None:
    """Load a node, mutate its front-matter dict in place, re-serialize (front-matter is a generated
    mirror — Link Reconciliation §8 — so a normalized dump is acceptable). Body is preserved verbatim."""
    import yaml
    text = Path(path).read_text(encoding="utf-8")
    fm, body = mnx_common.split_frontmatter(text)
    mutate(fm)
    block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False).strip()
    Path(path).write_text(f"---\n{block}\n---\n{body}", encoding="utf-8")


def apply_links(plan: dict[str, Any], team: str) -> dict[str, Any]:
    """Write the link plan onto source nodes: mirror resolved links into `edges:`, record red-links
    in `mentions:`. Idempotent.

    Front-matter `edges:`/`mentions:` are a GENERATED MIRROR of the body's resolved [[wiki-links]]
    (Link Reconciliation §8), so for a BATCH note — one whose full body was parsed by plan_links,
    listed in plan['sources'] — apply RE-DERIVES the mirror from this cycle's plan (set-REPLACE):
    any edge/mention whose [[link]] the author removed is pruned, not left as a phantom. A
    backlink-only source (an older note healed by L2 back-fill, absent from plan['sources']) has only
    its incremental link in the plan, so it is APPENDED to that note's existing mirror. Returns a
    summary of edits."""
    paths = _id_to_path(team)
    # group every resolved link (outbound + backlink) by its source node
    by_source: dict[str, list[dict[str, Any]]] = {}
    for ln in plan.get("links", []) + plan.get("backlinks", []):
        by_source.setdefault(ln["source_id"], []).append(ln)
    reds_by_source: dict[str, list[dict[str, Any]]] = {}
    for rl in plan.get("red_links", []):
        reds_by_source.setdefault(rl["source_id"], []).append(rl)
    # batch notes: plan holds their COMPLETE resolved set → re-derive (prune removed links);
    # a batch note that dropped all its links still appears here so its stale mirror is cleared.
    batch = set(plan.get("sources", []))

    edited = 0
    missing: list[str] = []
    for sid in set(by_source) | set(reds_by_source) | batch:
        path = paths.get(sid)
        if not path:
            missing.append(sid)
            continue

        def mutate(fm, sid=sid):
            if sid in batch:
                # authoritative: rebuild the mirror from scratch so removed links leave no phantom
                edges, mentions = [], []
            else:
                # backlink-only older note: preserve its mirror, append the healed link
                edges = [e for e in (fm.get("edges") or []) if isinstance(e, dict) and e.get("to")]
                mentions = [m for m in (fm.get("mentions") or []) if isinstance(m, dict)]
            have = {_norm(e["to"]) for e in edges}
            m_have = {_norm(m.get("name", "")) for m in mentions}
            for ln in by_source.get(sid, []):
                if _norm(ln["to"]) not in have:
                    edge = {"to": ln["to"]}
                    if ln.get("type"):
                        edge["type"] = ln["type"]
                    edges.append(edge)
                    have.add(_norm(ln["to"]))
                # a resolved link is also a resolved mention
                mname = ln.get("name") or ln["to"]
                if _norm(mname) not in m_have:
                    mentions.append({"name": mname, "resolved_id": ln["to"], "type": ln.get("type")})
                    m_have.add(_norm(mname))
                else:
                    for m in mentions:
                        if _norm(m.get("name", "")) == _norm(mname):
                            m["resolved_id"] = ln["to"]
            for rl in reds_by_source.get(sid, []):
                if _norm(rl["name"]) not in m_have:
                    mentions.append({"name": rl["name"], "resolved_id": None, "type": rl.get("type")})
                    m_have.add(_norm(rl["name"]))
            fm["edges"] = edges
            fm["mentions"] = mentions

        _write_node_fm(path, mutate)
        edited += 1

    return {"action": "applied", "nodes_edited": edited, "missing_sources": missing,
            "links": len(plan.get("links", [])), "backlinks": len(plan.get("backlinks", [])),
            "red_links": len(plan.get("red_links", []))}


_USAGE = [
    "mnx_mesh.py plan <team> <notes.json>   — resolve each note's [[wiki-links]] into a link plan",
    'mnx_mesh.py apply <team> <plan.json>   — apply a link plan (mentions/edges mirrors, cross-links)',
]


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE)
    if handled is not None:
        return handled
    import json
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "plan":
            # plan <team> <notes.json>   (notes.json: [{id, body, aliases?, disposition?, mentions?}])
            notes = json.loads(Path(argv[3]).read_text(encoding="utf-8"))
            return mnx_common.emit(plan_links(notes, argv[2]))
        if cmd == "apply":
            # apply <team> <plan.json>
            plan = json.loads(Path(argv[3]).read_text(encoding="utf-8"))
            return mnx_common.emit(apply_links(plan, argv[2]))
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
