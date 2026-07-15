"""mnx_promote.py — the plan-transaction orchestrator (multi-agent plan v2 §6.2, Phase 2 commit 2a).

Design change vs v1: v1 exposed promote as discrete mechanics tools the host drives itself
(lock/write/regen/doctor/persist) — one missed step wedges the lock or leaves an inconsistent
graph, on the least-controllable host. Here the host submits ONE declarative plan (the promote
SKILL's Step 4 approval) and this module executes the whole transaction (the SKILL's Step 5, in
fixed order): the host does judgment (dispositions), the engine does sequencing.

Public API, each a thin session over the existing engine writers:

  * begin(binding=None, team=None)          — preflight guards, stranded-plan recovery, lock
  * context(binding=None, team=None, pids=None, clusters=None)
                                             — everything the reconcile judgment needs in one call
  * validate_plan(plan, batch_pids, graph_root) -> list[str]  — schema + coverage errors ([] = valid)
  * apply(plan, approved=True, binding=None, team=None)
                                             — the Step-5 sequence, doctor-gated, with rollback
  * retry_push(binding=None, team=None)     — push an already-committed merge, deferred settle
  * abort(binding=None, team=None)          — release lock, drop the plan, staging untouched

apply()'s fixed order (truth before derived), mirroring SKILL.md Step 5 exactly:
  1. validate  2. write pass.plan.json (crash-recovery artifact)  3. mnx_node truth writes
  4. mnx_mesh.apply_links  5. consolidate (approved-death tombstones)  6. regenerate indexes/
  cross-links/phonebook, stamp last_compaction  7. doctor gate (E==0, else roll back)
  8. mnx_binding.persist (push failure -> stop, plan stays for retry_push)  9. per-atom settle
  (hold contradictions, clear-merged the rest), remove plan, release lock.

A team lock handle is NOT threaded across calls (a CLI retry_push/abort runs in a fresh process
after a crash) — the lock file path is instead deterministically rederived from graph_root+team
via mnx_common.state_dir, matching mnx_lock's own private _lock_path() for a real team folder.

Dependencies: Python 3.9+ stdlib + PyYAML (via mnx_binding). See docs/06.
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
import mnx_index
import mnx_lock
import mnx_mesh
import mnx_node
import mnx_phonebook
import mnx_resolve
import mnx_simindex
import mnx_stage
import mnx_stamp

PLAN_VERSION = 1
_OPS = {"create", "merge", "supersede", "drop_dup", "hold", "resurrect"}
_STG_PREFIX = "stg-"
_NEW_DISPOSITION_FOR_OP = {"create": "create", "merge": "merge", "resurrect": "resurrect"}


# --- binding / team resolution ------------------------------------------------------

def _resolve(binding: Optional["mnx_binding.Binding"],
             team: Optional[str]) -> tuple["mnx_binding.Binding", str, str]:
    binding = binding or mnx_binding.resolve()
    if binding is None:
        raise ValueError("No Mnemex graph configured. Run /mnemex:mnx-init.")
    team = team or binding.default_team
    if not team:
        raise ValueError("no team given and the binding has no default_team")
    team_path = str(Path(binding.graph_root()) / team)
    return binding, team, team_path


def _lock_handle(graph_root: str, team_name: str) -> dict[str, str]:
    """Rederive the team lock's file handle from graph_root+team alone (see module docstring)."""
    return {"path": str(mnx_common.state_dir(graph_root) / "locks" / f"{team_name}.lock")}


def _release_lock_if_held(graph_root: str, team_name: str, team_path: str) -> None:
    if mnx_lock.held(team_path):
        mnx_lock.release(_lock_handle(graph_root, team_name))


def _cluster_path(graph_root: str, cluster: str) -> Path:
    p = Path(cluster)
    if not p.is_absolute():
        p = Path(graph_root) / cluster
    p.mkdir(parents=True, exist_ok=True)
    return p


# --- plan validation -----------------------------------------------------------------

def validate_plan(plan: dict[str, Any], batch_pids: set[str], graph_root: str) -> list[str]:
    """Schema + coverage errors. [] means the plan is safe to apply."""
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["plan must be an object"]
    if plan.get("plan_version") != PLAN_VERSION:
        errors.append(f"plan_version must be {PLAN_VERSION}")

    dispositions = plan.get("dispositions")
    if not isinstance(dispositions, list):
        errors.append("dispositions must be a list")
        dispositions = []

    seen: set[str] = set()
    for i, d in enumerate(dispositions):
        if not isinstance(d, dict):
            errors.append(f"dispositions[{i}] must be an object")
            continue
        pid, op = d.get("pid"), d.get("op")
        if not pid:
            errors.append(f"dispositions[{i}] missing pid")
            continue
        if pid in seen:
            errors.append(f"{pid}: disposed more than once")
        seen.add(pid)
        if op not in _OPS:
            errors.append(f"{pid}: invalid op {op!r} (one of {sorted(_OPS)})")
            continue

        if op in ("create", "supersede"):
            fields = d.get("fields")
            if not isinstance(fields, dict):
                errors.append(f"{pid}: {op} requires fields")
                fields = {}
            if not str(fields.get("title") or "").strip():
                errors.append(f"{pid}: {op} fields.title is required")
            if str(fields.get("id", "")).startswith(_STG_PREFIX):
                errors.append(f"{pid}: {op} fields.id must not be a stg- id")
            cluster = d.get("cluster")
            if not cluster and not fields.get("new_cluster"):
                errors.append(f"{pid}: {op} requires cluster (or fields.new_cluster: true)")
            elif cluster:
                cpath = Path(cluster) if Path(cluster).is_absolute() else Path(graph_root) / cluster
                if not cpath.is_dir() and not fields.get("new_cluster"):
                    errors.append(f"{pid}: cluster {cluster!r} does not exist "
                                  "(set fields.new_cluster: true to create it)")
            if op == "supersede":
                old_id = d.get("old_id")
                if not old_id:
                    errors.append(f"{pid}: supersede requires old_id")
                elif str(old_id).startswith(_STG_PREFIX):
                    errors.append(f"{pid}: old_id must not be a stg- id")
        elif op == "merge":
            nid = d.get("id")
            if not nid:
                errors.append(f"{pid}: merge requires id")
            elif str(nid).startswith(_STG_PREFIX):
                errors.append(f"{pid}: merge id must not be a stg- id")
            if not d.get("cluster"):
                errors.append(f"{pid}: merge requires cluster")
            changes = d.get("changes")
            if not isinstance(changes, dict):
                errors.append(f"{pid}: merge requires changes")
            else:
                unknown = set(changes) - mnx_node.MERGE_CHANGE_KEYS
                if unknown:
                    errors.append(
                        f"{pid}: merge changes has unrecognized field(s) {sorted(unknown)} "
                        f"(recognized: {sorted(mnx_node.MERGE_CHANGE_KEYS)}; aliases/edges/"
                        "references/mentions/body REPLACE the existing value, they do not "
                        "append — include the entries you want kept)")
        elif op == "resurrect":
            nid = d.get("id")
            if not nid:
                errors.append(f"{pid}: resurrect requires id")
            elif str(nid).startswith(_STG_PREFIX):
                errors.append(f"{pid}: resurrect id must not be a stg- id")
            if not d.get("cluster"):
                errors.append(f"{pid}: resurrect requires cluster")
        elif op == "drop_dup":
            if not d.get("dup_of"):
                errors.append(f"{pid}: drop_dup requires dup_of")
        elif op == "hold":
            if not d.get("reason"):
                errors.append(f"{pid}: hold requires reason")

    splits = plan.get("splits")
    if splits is not None and not isinstance(splits, list):
        errors.append("splits must be a list (or omitted)")
        splits = []
    for i, split in enumerate(splits or []):
        pid = split.get("pid") if isinstance(split, dict) else None
        if not pid:
            errors.append(f"splits[{i}] missing pid")
            continue
        if pid in seen:
            errors.append(f"{pid}: disposed more than once (both dispositions and splits)")
        seen.add(pid)
        pieces = split.get("pieces")
        if not isinstance(pieces, list) or not pieces:
            errors.append(f"{pid}: split requires a non-empty pieces list")
            continue
        for j, piece in enumerate(pieces):
            fields = piece.get("fields") if isinstance(piece, dict) else None
            if not isinstance(fields, dict) or not str(fields.get("title") or "").strip():
                errors.append(f"{pid}: splits pieces[{j}] requires fields.title")

    # `apply()` dereferences both of these with `.get(...)` unconditionally (mnx_promote.py
    # ~410-417) — a wrong JSON type here must fail validation cleanly, not raise a raw
    # AttributeError from inside apply() after the lock is already held.
    links = plan.get("links")
    if links is not None and not isinstance(links, dict):
        errors.append("links must be an object (or omitted)")
    elif isinstance(links, dict):
        cs = links.get("confirmed_suggestions")
        if cs is not None and not isinstance(cs, list):
            errors.append("links.confirmed_suggestions must be a list")
        else:
            for i, item in enumerate(cs or []):
                if not isinstance(item, dict) or not item.get("src") or not item.get("dst"):
                    errors.append(f"links.confirmed_suggestions[{i}] requires src and dst")

    consolidate = plan.get("consolidate")
    if consolidate is not None and not isinstance(consolidate, dict):
        errors.append("consolidate must be an object (or omitted)")
    elif isinstance(consolidate, dict):
        deaths = consolidate.get("approved_deaths")
        if deaths is not None and not isinstance(deaths, list):
            errors.append("consolidate.approved_deaths must be a list")

    missing = batch_pids - seen
    if missing:
        errors.append(f"{len(missing)} staged pid(s) undisposed: {sorted(missing)}")
    extra = seen - batch_pids
    if extra:
        errors.append(f"{len(extra)} disposition(s) reference pids outside the batch: {sorted(extra)}")

    return errors


# --- git rollback (doctor-gate failure / crash recovery) ----------------------------

def _git_rollback(graph_root: str) -> bool:
    """Discard uncommitted writes (tracked + untracked) back to HEAD. No-op for non-git graphs."""
    root = Path(graph_root)
    if not (root / ".git").exists():
        return False
    subprocess.run(["git", "checkout", "--", "."], cwd=str(root), capture_output=True, text=True)
    subprocess.run(["git", "clean", "-fd"], cwd=str(root), capture_output=True, text=True)
    return True


# --- settle (per-atom terminal disposition) ------------------------------------------

def _settle(binding: "mnx_binding.Binding", promoted_pids: list[str],
            held_atoms: list[dict[str, Any]]) -> dict[str, Any]:
    held_results = [mnx_stage.hold(h["pid"], h.get("reason") or "contradiction",
                                   h.get("contradicts"), binding=binding)
                    for h in held_atoms]
    cleared = mnx_stage.clear_merged(promoted_pids, binding=binding)
    return {"held": held_results, "cleared": cleared}


def _promoted_and_held_from_plan(plan: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    promoted, held = [], []
    for d in plan.get("dispositions", []) or []:
        if d.get("op") == "hold":
            held.append({"pid": d["pid"], "reason": d.get("reason", "contradiction"),
                         "contradicts": d.get("contradicts")})
        else:
            promoted.append(d["pid"])
    for split in plan.get("splits", []) or []:
        promoted.append(split["pid"])
    return promoted, held


# --- begin ----------------------------------------------------------------------------

def begin(binding: Optional["mnx_binding.Binding"] = None, team: Optional[str] = None) -> dict[str, Any]:
    """Preflight: flush stamps, D7 unpushed guard, stranded-plan recovery, then acquire the lock."""
    binding, team_name, team_path = _resolve(binding, team)
    graph_root = binding.graph_root()
    stamps_flushed = mnx_stamp.flush()

    unpushed = mnx_binding.unpushed_state(binding)
    if unpushed.get("unpushed"):
        return {"guard": "unpushed", "action": "retry_push", "team": team_name,
                "unpushed": unpushed, "stamps_flushed": stamps_flushed}

    recovered = None
    rec = mnx_lock.recover(team_path)
    if rec["action"] == "rollback":
        _git_rollback(graph_root)
        mnx_lock.remove_plan(team_path)
        recovered = {"action": "rollback", "detail": "discarded a partial promote apply"}
        _release_lock_if_held(graph_root, team_name, team_path)
    elif rec["action"] == "replay":
        plan = mnx_lock.read_plan(team_path)
        settle_result = None
        if plan:
            promoted, held = _promoted_and_held_from_plan(plan)
            settle_result = _settle(binding, promoted, held)
        mnx_lock.remove_plan(team_path)
        recovered = {"action": "replay", "settle": settle_result}
        _release_lock_if_held(graph_root, team_name, team_path)

    session_batch = mnx_stage.list_atoms(binding=binding, label="_session")["atoms"]
    staging_status = mnx_stage.status(binding=binding)
    if not session_batch and any(k != "_session" for k in staging_status.get("by_label", {})):
        return {"guard": "ingest-batch", "action": "no session atoms staged; drain the pending "
                "ingest batch via the mnx-promote --bulk skill path instead",
                "team": team_name, "by_label": staging_status.get("by_label"),
                "recovered": recovered, "stamps_flushed": stamps_flushed}

    try:
        handle = mnx_lock.acquire(team_path)
    except mnx_lock.Busy as busy:
        return {"guard": "busy", "action": "wait and retry", "team": team_name,
                "detail": str(busy), "recovered": recovered, "stamps_flushed": stamps_flushed}

    return {"guard": "none", "action": "locked", "team": team_name, "lock": handle,
            "recovered": recovered, "stamps_flushed": stamps_flushed,
            "batch": session_batch, "batch_count": len(session_batch),
            "phonebook": mnx_phonebook.entries(team_path)}


# --- context --------------------------------------------------------------------------

def context(binding: Optional["mnx_binding.Binding"] = None, team: Optional[str] = None,
            pids: Optional[list[str]] = None, clusters: Optional[list[str]] = None) -> dict[str, Any]:
    """Everything the reconcile judgment needs: staged atoms + near-match candidates + routed
    cluster index rows + a link-plan preview, in one call."""
    binding, team_name, team_path = _resolve(binding, team)
    graph_root = binding.graph_root()

    batch = mnx_stage.overlay(binding=binding, label="_session")["atoms"]
    if pids:
        want = set(pids)
        batch = [a for a in batch if a.get("provisional_id") in want]

    near_matches: dict[str, Any] = {}
    for a in batch:
        # Query text must mirror mnx_simindex._surface's indexed shape (summary + aliases) —
        # mixing in `body` was diluting Jaccard similarity against every candidate (indexed
        # items are short; a body-heavy query adds tokens no node has, systematically pushing
        # even a near-verbatim restatement below threshold) and silently produced empty
        # near_matches for every atom. threshold=0.3 (below mnx_simindex.query's 0.4 default):
        # this call is explicitly HITL-only ("fuzzy candidates only ... never an auto-edit"),
        # so a false positive just costs the host one extra candidate to reject, while missing
        # a true near-duplicate/merge/resurrect target silently defeats the whole point of the
        # call. Real merge/resurrect pairs in practice score 0.37-0.44 with only 64
        # permutations' worth of estimator noise, i.e. routinely straddle the stricter 0.4 —
        # not a fluke of any one example.
        text = f"{a.get('summary', '')} {mnx_common.aliases_to_index(a.get('aliases'))}".strip()
        try:
            near_matches[a["provisional_id"]] = mnx_simindex.query(
                text, team_path, threshold=0.3)["candidates"]
        except Exception:
            near_matches[a["provisional_id"]] = []

    target_clusters = clusters or [str(c) for c in mnx_common.iter_clusters(team_path)]
    cluster_index: dict[str, Any] = {}
    for c in target_clusters:
        cpath = Path(c) if Path(c).is_absolute() else Path(graph_root) / c
        idx_file = cpath / mnx_common.INDEX_FILENAME
        if idx_file.is_file():
            try:
                cluster_index[str(cpath)] = mnx_common.parse_index(idx_file)
            except Exception:
                pass

    notes = [{"id": a["provisional_id"], "body": a.get("body", ""),
             "aliases": a.get("aliases", []), "disposition": "create"} for a in batch]
    mesh_preview = (mnx_mesh.plan_links(notes, team_path) if notes
                    else {"links": [], "red_links": [], "backlinks": [], "sources": [],
                          "counts": {"links": 0, "red_links": 0, "backlinks": 0}})

    return {"team": team_name, "batch": batch, "near_matches": near_matches,
            "cluster_index": cluster_index, "mesh_preview": mesh_preview}


# --- apply ------------------------------------------------------------------------------

def apply(plan: dict[str, Any], approved: bool = True,
          binding: Optional["mnx_binding.Binding"] = None,
          team: Optional[str] = None) -> dict[str, Any]:
    """Execute the promote SKILL's Step 5, in fixed order, under the team lock. See module docstring."""
    binding, team_name, team_path = _resolve(binding, team)
    graph_root = binding.graph_root()

    if not approved:
        raise ValueError("apply requires approved=true (Step 4 human approval)")
    if not mnx_lock.held(team_path):
        raise ValueError(f"team lock not held for {team_name!r} — call begin() first")

    batch_pids = {a["provisional_id"]
                 for a in mnx_stage.list_atoms(binding=binding, label="_session")["atoms"]}
    errors = validate_plan(plan, batch_pids, graph_root)
    if errors:
        return {"action": "rejected", "reason": "validation", "errors": errors}

    mnx_lock.write_plan(team_path, plan)

    touched_clusters: set[str] = set()
    touched_teams: set[str] = set()
    notes: list[dict[str, Any]] = []
    disposition_summary: list[dict[str, Any]] = []

    def _touch(cluster: Path) -> None:
        touched_clusters.add(str(cluster))
        t = mnx_common.team_of(graph_root, cluster)
        if t:
            touched_teams.add(t)

    noted_ids: set[str] = set()

    def _note(cluster: Path, nid: str, disposition: str) -> None:
        if nid in noted_ids:
            return
        noted_ids.add(nid)
        node = mnx_common.parse_node(cluster / f"{nid}.md")
        notes.append({"id": nid, "cluster_path": str(cluster), "body": node.get("_body", ""),
                     "aliases": node.get("aliases", []), "disposition": disposition})

    def _repoint_referrers(old_id: str) -> None:
        """Pre-existing nodes (outside this batch) whose `edges:` mirror still points at
        `old_id` — found so their mirror gets re-derived in the SAME plan_links/apply_links
        pass below. `mnx_phonebook.resolve` already forwards a superseded id to its live
        successor (F8), but that forwarding only takes effect when a note's body is
        re-parsed; a referrer untouched by this batch keeps a stale mirror and fails the
        doctor gate (inv 2, live edge -> tombstoned node) forever otherwise. Disposition
        "repoint" is deliberately not in mnx_mesh._NEW_DISPOSITIONS, so this only triggers
        outbound re-resolution (Phase L1), not a spurious backfill pass (Phase L2) for a
        node that isn't actually new."""
        for cluster in mnx_common.iter_clusters(Path(team_path)):
            for nf in mnx_common.iter_node_files(cluster):
                try:
                    node = mnx_common.parse_node(nf)
                except Exception:
                    continue
                if node.get("status") == "dead" or node.get("id") in noted_ids:
                    continue
                if any(isinstance(e, dict) and e.get("to") == old_id
                       for e in (node.get("edges") or [])):
                    _touch(cluster)
                    _note(cluster, node["id"], "repoint")

    # A host only has the staged pid for a not-yet-created node at plan-drafting time (the
    # real slug is minted by mnx_node.create below), so confirmed_suggestions naturally gets
    # written with a pid on one or both sides. Track pid -> real id as each disposition lands
    # so those get translated instead of producing a phantom missing_sources entry (the pid
    # was never a real node path) alongside the correctly-resolved body-wikilink link.
    pid_to_id: dict[str, str] = {}

    for d in plan.get("dispositions", []):
        pid, op = d["pid"], d["op"]
        if op == "create":
            cluster = _cluster_path(graph_root, d["cluster"])
            res = mnx_node.create(cluster, d["fields"])
            _touch(cluster)
            _note(cluster, res["id"], "create")
            pid_to_id[pid] = res["id"]
            disposition_summary.append({"pid": pid, "op": op, "id": res["id"]})
        elif op == "merge":
            cluster = _cluster_path(graph_root, d["cluster"])
            res = mnx_node.merge(d["id"], cluster, d.get("changes") or {}, d.get("meaning_change", False))
            _touch(cluster)
            _note(cluster, d["id"], "merge")
            pid_to_id[pid] = d["id"]
            disposition_summary.append({"pid": pid, "op": op, "id": d["id"]})
        elif op == "supersede":
            cluster = _cluster_path(graph_root, d["cluster"])
            res = mnx_node.supersede(d["old_id"], cluster, d["fields"])
            _touch(cluster)
            _note(cluster, res["new_id"], "create")
            _repoint_referrers(d["old_id"])
            pid_to_id[pid] = res["new_id"]
            disposition_summary.append({"pid": pid, "op": op, "old_id": d["old_id"], "new_id": res["new_id"]})
        elif op == "resurrect":
            cluster = _cluster_path(graph_root, d["cluster"])
            mnx_node.resurrect(d["id"], cluster)
            _touch(cluster)
            _note(cluster, d["id"], "resurrect")
            pid_to_id[pid] = d["id"]
            disposition_summary.append({"pid": pid, "op": op, "id": d["id"]})
        elif op == "drop_dup":
            disposition_summary.append({"pid": pid, "op": op, "dup_of": d.get("dup_of")})
        elif op == "hold":
            disposition_summary.append({"pid": pid, "op": op, "reason": d.get("reason")})

    for split in plan.get("splits", []) or []:
        pid = split["pid"]
        pieces_summary = []
        for piece in split["pieces"]:
            cluster = _cluster_path(graph_root, piece.get("cluster") or split.get("cluster"))
            res = mnx_node.create(cluster, piece["fields"])
            _touch(cluster)
            _note(cluster, res["id"], "create")
            pieces_summary.append(res["id"])
        if pieces_summary:
            pid_to_id[pid] = pieces_summary[0]
        disposition_summary.append({"pid": pid, "op": "split", "pieces": pieces_summary})

    link_summary = None
    if notes:
        link_plan = mnx_mesh.plan_links(notes, team_path)
        for cs in (plan.get("links") or {}).get("confirmed_suggestions", []):
            src = pid_to_id.get(cs["src"], cs["src"])
            dst = pid_to_id.get(cs["dst"], cs["dst"])
            link_plan["links"].append({"source_id": src, "to": dst, "type": None,
                                       "origin": "confirmed-suggestion", "name": dst})
        link_summary = mnx_mesh.apply_links(link_plan, team_path)

    consolidate = plan.get("consolidate") or {}
    deaths: list[str] = []
    if consolidate.get("run") and consolidate.get("approved_deaths"):
        for nid in consolidate["approved_deaths"]:
            node_path = mnx_resolve.resolve(nid, graph_root)
            if not node_path:
                continue
            cluster = Path(node_path).parent
            mnx_node.tombstone(nid, cluster)
            _touch(cluster)
            deaths.append(nid)

    for cluster in sorted(touched_clusters):
        mnx_index.regenerate_index(cluster)
    if touched_clusters:
        mnx_doctor.regen_crosslinks(graph_root)
    for t in sorted(touched_teams):
        mnx_phonebook.regenerate(str(Path(graph_root) / t))
    if touched_teams:
        mnx_phonebook.regenerate_org(graph_root)
        cfg = mnx_config.load(graph_root)
        for t in sorted(touched_teams):
            mnx_config.stamp(str(Path(graph_root) / t), cfg)

    report = mnx_doctor.check(graph_root)
    if not report["ok"]:
        rolled_back = _git_rollback(graph_root)
        mnx_lock.remove_plan(team_path)
        _release_lock_if_held(graph_root, team_name, team_path)
        return {"action": "rejected", "reason": "doctor-gate", "doctor": report,
                "rolled_back": rolled_back}

    message = f"mnx-promote: {len(disposition_summary)} disposition(s) for {team_name}"
    persist_res = mnx_binding.persist(binding, message)
    if persist_res.get("push") in ("failed", "conflict"):
        # Commit already landed locally — do NOT settle staging or clear the plan; the merge must
        # not be re-run. pass.plan.json stays for retry_push.
        recovery = {k: persist_res[k] for k in
                   ("guidance", "retry_command", "manual_fallback", "clone_path", "branch", "ahead")
                   if k in persist_res}
        return {"action": "committed-not-pushed", "dispositions": disposition_summary,
                "persist": persist_res, "recovery": recovery}

    promoted = [d["pid"] for d in plan.get("dispositions", []) if d.get("op") != "hold"]
    promoted += [s["pid"] for s in (plan.get("splits") or [])]
    held_atoms = [{"pid": d["pid"], "reason": d.get("reason", "contradiction"),
                  "contradicts": d.get("contradicts")}
                 for d in plan.get("dispositions", []) if d.get("op") == "hold"]
    settle_result = _settle(binding, promoted, held_atoms)
    mnx_lock.remove_plan(team_path)
    _release_lock_if_held(graph_root, team_name, team_path)

    return {"action": "applied", "dispositions": disposition_summary, "links": link_summary,
            "deaths": deaths, "doctor": report, "persist": persist_res, "settle": settle_result}


# --- retry_push -------------------------------------------------------------------------

def retry_push(binding: Optional["mnx_binding.Binding"] = None,
               team: Optional[str] = None) -> dict[str, Any]:
    """Push an already-committed merge, then perform the deferred settle from pass.plan.json."""
    binding, team_name, team_path = _resolve(binding, team)
    graph_root = binding.graph_root()

    plan = mnx_lock.read_plan(team_path)
    if plan is None:
        raise ValueError(f"no pending promote plan for team {team_name!r} to retry")

    push_res = mnx_binding.push(binding)
    if push_res.get("push") not in (None, "ok"):
        return {"action": "still-failing", "push": push_res}

    promoted, held = _promoted_and_held_from_plan(plan)
    settle_result = _settle(binding, promoted, held)
    mnx_lock.remove_plan(team_path)
    _release_lock_if_held(graph_root, team_name, team_path)

    return {"action": "pushed-and-settled", "push": push_res, "settle": settle_result}


# --- abort ------------------------------------------------------------------------------

def abort(binding: Optional["mnx_binding.Binding"] = None, team: Optional[str] = None) -> dict[str, Any]:
    """Release the lock and drop any pending plan. Staging is left untouched."""
    binding, team_name, team_path = _resolve(binding, team)
    graph_root = binding.graph_root()
    had_plan = mnx_lock.in_progress(team_path)
    mnx_lock.remove_plan(team_path)
    _release_lock_if_held(graph_root, team_name, team_path)
    return {"action": "aborted", "team": team_name, "had_plan": had_plan}


# --- cli ----------------------------------------------------------------------------------

def _arg(argv: list[str], flag: str) -> Optional[str]:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def _json_stdin(argv: list[str]) -> dict[str, Any]:
    import json
    if "--json" in argv:
        return json.loads(sys.stdin.read() or "{}")
    jf = _arg(argv, "--json-file")
    if jf:
        return json.loads(Path(jf).read_text(encoding="utf-8"))
    raise ValueError("apply needs a plan via --json (stdin) or --json-file <path>")


_USAGE = [
    "mnx_promote.py begin [--team <t>]                              — preflight guards + lock",
    "mnx_promote.py context [--team <t>] [--pids p1,p2] [--clusters c1,c2]  — reconcile context",
    "mnx_promote.py apply [--team <t>] [--json < plan.json | --json-file <f>]  — apply an approved plan",
    "mnx_promote.py retry-push [--team <t>]                          — retry a failed push + settle",
    "mnx_promote.py abort [--team <t>]                               — release lock, drop the plan",
]
_FLAGS = {"--team": True, "--pids": True, "--clusters": True, "--json": False, "--json-file": True}


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE, _FLAGS)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else ""
    team = _arg(argv, "--team")
    try:
        if cmd == "begin":
            return mnx_common.emit(begin(team=team))
        if cmd == "context":
            pids = (_arg(argv, "--pids") or "").split(",") if _arg(argv, "--pids") else None
            clusters = (_arg(argv, "--clusters") or "").split(",") if _arg(argv, "--clusters") else None
            return mnx_common.emit(context(team=team, pids=pids, clusters=clusters))
        if cmd == "apply":
            plan = _json_stdin(argv)
            res = apply(plan, team=team)
            return mnx_common.emit(res, ok=res.get("action") not in ("rejected",))
        if cmd == "retry-push":
            return mnx_common.emit(retry_push(team=team))
        if cmd == "abort":
            return mnx_common.emit(abort(team=team))
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


main = _main  # back-compat alias; `_main(argv)` is the engine-wide dispatcher name (plan v2, 0e)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
