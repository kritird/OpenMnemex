"""mnx_simindex.py — fuzzy link/dup candidate filter (W8). NON-AUTHORITATIVE.

See docs/13-resilient-mesh-roadmap.md §2 (W8) and the S2 soft limit (docs/08).

The phonebook (W2) resolves an author's mention to an id by EXACT alias. This is its fuzzy
twin: an approximate similarity index (MinHash over `summary + aliases`) used ONLY at promote
to *whisper candidates* — "this atom looks like a link to / duplicate of `<id>`" — to the
reconcile sub-agent, and to feed the doctor's S2 (cross-cluster duplication) worklist.

It is deliberately:
  * NON-AUTHORITATIVE — it proposes; reconcile/HITL disposes. It never writes an edge or a node.
  * READ-PATH-FREE — consulted only at promote, never by mnx-read. The read path keeps its
    no-search-infra purity.
  * ZERO-DEPENDENCY — pure-python MinHash + LSH banding (deterministic hashing via hashlib), so
    no model/embedding dependency. Embeddings are a possible later upgrade; MinHash ships first.

Python 3.9+, stdlib + PyYAML. Imports mnx_common only.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Any

import mnx_common

_PRIME = (1 << 61) - 1
_MASK32 = (1 << 32) - 1
DEAD = "dead"


def _h32(s: str) -> int:
    return int(hashlib.sha1(s.encode("utf-8")).hexdigest()[:8], 16)


def _perms(num_perm: int) -> list[tuple[int, int]]:
    """Deterministic (a, b) coefficients for universal hashing h_i(x) = (a·x + b) mod P."""
    out = []
    for i in range(num_perm):
        a = (_h32(f"a{i}") << 1) | 1          # odd → coprime-ish with 2^k
        b = _h32(f"b{i}")
        out.append((a, b))
    return out


def tokens(text: str) -> set[str]:
    """Word tokens + 3-char shingles of `text` (lowercased, alnum). Shingles catch typo-level
    near-misses ('authorisation' vs 'authorization') that word tokens alone miss."""
    norm = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    words = [w for w in norm.split() if w]
    shingles = set()
    flat = norm.replace(" ", "")
    for i in range(len(flat) - 2):
        shingles.add(flat[i:i + 3])
    return set(words) | shingles


def minhash(toks: set[str], perms: list[tuple[int, int]]) -> list[int]:
    """MinHash signature: for each permutation, the min over tokens of (a·hash(tok)+b) mod P."""
    if not toks:
        return [0] * len(perms)
    base = [_h32(t) for t in toks]
    sig = []
    for a, b in perms:
        sig.append(min(((a * x + b) % _PRIME) & _MASK32 for x in base))
    return sig


def jaccard(sig_a: list[int], sig_b: list[int]) -> float:
    """Estimated Jaccard similarity = fraction of agreeing signature slots."""
    if not sig_a or len(sig_a) != len(sig_b):
        return 0.0
    return sum(1 for x, y in zip(sig_a, sig_b) if x == y) / len(sig_a)


def _surface(node: dict[str, Any]) -> str:
    return f"{node.get('summary', '')} {mnx_common.aliases_to_index(node.get('aliases'))}"


def build(scope: str, num_perm: int = 64) -> dict[str, Any]:
    """Index every active node under scope: {id: {sig, cluster, summary, aliases}} + LSH bands."""
    perms = _perms(num_perm)
    items: dict[str, Any] = {}
    for cluster in mnx_common.iter_clusters(scope):
        cl = str(Path(cluster).resolve())
        for nf in mnx_common.iter_node_files(cluster):
            try:
                node = mnx_common.parse_node(nf)
            except Exception:
                continue
            nid = node.get("id")
            if not nid or node.get("status") == DEAD:
                continue
            items[nid] = {"sig": minhash(tokens(_surface(node)), perms),
                          "cluster": cl,
                          "summary": str(node.get("summary", "")),
                          "aliases": mnx_common.aliases_to_index(node.get("aliases"))}
    return {"perms": perms, "items": items, "num_perm": num_perm}


def query(text: str, scope: str, threshold: float = 0.4, k: int = 5,
          num_perm: int = 64) -> dict[str, Any]:
    """Rank nodes by estimated similarity to `text`. Candidates only — never authoritative."""
    idx = build(scope, num_perm)
    qsig = minhash(tokens(text), idx["perms"])
    scored = []
    for nid, it in idx["items"].items():
        est = jaccard(qsig, it["sig"])
        if est >= threshold:
            scored.append({"id": nid, "similarity": round(est, 3),
                           "cluster": it["cluster"], "summary": it["summary"]})
    scored.sort(key=lambda c: (-c["similarity"], c["id"]))
    return {"text": text, "threshold": threshold, "candidates": scored[:k],
            "note": "fuzzy candidates only — reconcile/HITL confirms; never an auto-edit"}


def pairs(scope: str, threshold: float = 0.5, num_perm: int = 64) -> dict[str, Any]:
    """Near-duplicate node pairs ACROSS clusters (the doctor's S2 worklist). Cross-cluster only —
    intra-cluster duplication is reconcile's local job; this surfaces the duplication the
    cluster-local view structurally cannot see."""
    idx = build(scope, num_perm)
    ids = list(idx["items"])
    out = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = idx["items"][ids[i]], idx["items"][ids[j]]
            if a["cluster"] == b["cluster"]:
                continue
            est = jaccard(a["sig"], b["sig"])
            if est >= threshold:
                out.append({"a": ids[i], "b": ids[j], "similarity": round(est, 3),
                            "a_cluster": a["cluster"], "b_cluster": b["cluster"]})
    out.sort(key=lambda p: -p["similarity"])
    return {"threshold": threshold, "candidate_pairs": out,
            "note": "possible cross-cluster duplicates (S2) — info-level worklist for human convergence"}


def _main(argv: list[str]) -> int:
    args = argv[2:]

    def opt(flag, default=None):
        return args[args.index(flag) + 1] if flag in args else default

    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "query":
            return mnx_common.emit(query(opt("--text", ""), opt("--scope", "."),
                                         float(opt("--threshold", "0.4"))))
        if cmd == "pairs":
            return mnx_common.emit(pairs(opt("--scope", "."), float(opt("--threshold", "0.5"))))
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
