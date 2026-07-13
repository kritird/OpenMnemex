"""mnx_ingest.py — the corpus front-end (walk · classify · chunk · hash · manifest · delta).

Background: docs/corpus-ingestion.md, INGESTION-BUILD-PLAN.md phases A0–A1.

Ingest is a *source adapter*, not a new subsystem: a live session produces staged atoms; a corpus
is a second producer. This script owns the **deterministic** front half only — it acquires a source
(local path in place, or a shallow clone to a read-only cache), walks it, classifies each file into a
`kind`, chunks large files along structure into candidate *units*, hashes them, and reads/writes the
ingest manifest to compute a re-run delta. It makes **no judgment** ("is this an atom?" is the skill's
job) and **never writes the graph** and **never mutates the source** — it only reads.

Subcommands (see docs/script-contracts.md §mnx_ingest):
  acquire  --source <path|url> [--cache <dir>]         -> {kind, root, commit, cached}
  probe    --root <dir> [--include g;g] [--exclude g] [--max-bytes N]
                                                        -> {units[], counts, est_atoms, bytes_total, skipped_secrets}
  delta    --root <dir> --manifest <path>              -> {added[], changed[], unchanged, orphans[]}
  manifest-write --graph <root> --source-slug <s> --json  (stdin: files map) -> {path, files}

Dependencies: Python 3.9+ stdlib only (hashlib/re/subprocess). Imports mnx_common for emit.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator, Optional

import mnx_common

MAX_BYTES_DEFAULT = 1024 * 1024   # 1 MiB per-file cap
INGEST_STATE_SUBDIR = "ingest"    # <graph>/.mnemex/ingest/<slug>.json

# --- classification tables ---------------------------------------------------

DOC_EXTS = {".md", ".rst", ".adoc", ".txt"}
CODE_EXTS = {".py", ".ts", ".tsx", ".go", ".java", ".js", ".rb", ".rs"}
SCHEMA_EXTS = {".proto", ".graphql", ".gql"}
CONFIG_EXTS = {".tf", ".tfvars"}
BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".tar", ".so",
               ".dylib", ".dll", ".bin", ".ico", ".woff", ".woff2", ".ttf", ".jar",
               ".class", ".pyc", ".wasm", ".mp4", ".mp3", ".exe"}

# Skip-list: directories and file-shape patterns that never yield knowledge.
_SKIP_DIRS = {"node_modules", "dist", "build", "vendor", ".git", "__pycache__",
              ".mnemex", ".venv", "venv", "target"}
_SKIP_NAME_RE = re.compile(r".*\.lock$|.*\.min\..*|.*\.map$|.*\.snap$", re.I)
_CHANGELOG_RE = re.compile(r"^(changelog|changes|history|authors|contributors)\b", re.I)

# Secret guard — matched files are COUNTED but their bytes are NEVER opened.
def _is_secret(name: str) -> bool:
    low = name.lower()
    if low in (".env",) or low.startswith(".env.") and not low.endswith((".example", ".sample", ".template")):
        return True
    if low.startswith(".env"):  # bare .env variants without example/sample/template suffix handled above
        pass
    return (low.endswith((".pem", ".key", ".p12", ".pfx"))
            or "_rsa" in low or "_dsa" in low or "_ed25519" in low
            or low.startswith("credentials") or low.startswith("id_rsa")
            or low == ".npmrc" or low == ".pypirc")


def _is_env_example(name: str) -> bool:
    low = name.lower()
    return low.endswith((".env.example", ".env.sample", ".env.template")) or low in (
        ".env.example", ".env.sample", ".env.template", "env.example")


# --- unit ids ----------------------------------------------------------------

def _unit_id(rel_path: str, anchor: str) -> str:
    h = hashlib.sha1(f"{rel_path}::{anchor}".encode("utf-8")).hexdigest()[:10]
    return f"u-{h}"


def _content_hash(text: str) -> str:
    return "sha1:" + hashlib.sha1(text.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    return "sha1:" + hashlib.sha1(path.read_bytes()).hexdigest()


# --- classification ----------------------------------------------------------

def classify(rel_path: str) -> str:
    """File-level kind by extension + path. One of: doc|interface|code-doc|config|skip.

    (A code file gets a finer per-unit kind at chunk time — interface for exported symbols,
    code-doc for module/docstring headers — but its file-level bucket here is 'interface'.)"""
    name = os.path.basename(rel_path)
    ext = os.path.splitext(name)[1].lower()
    parts = Path(rel_path).parts
    if any(seg in _SKIP_DIRS for seg in parts):
        return "skip"
    if ext in BINARY_EXTS or _SKIP_NAME_RE.match(name):
        return "skip"
    if _CHANGELOG_RE.match(name):
        return "skip"
    if _is_env_example(name):
        return "config"
    if ext in CONFIG_EXTS:
        return "config"
    if ext in SCHEMA_EXTS:
        return "interface"
    if ext in CODE_EXTS:
        return "interface"     # finer split (interface vs code-doc) happens per unit
    if ext in DOC_EXTS:
        # README-in-dir reads as code-doc (author-written intent), other docs are 'doc'
        if name.lower().startswith("readme"):
            return "code-doc"
        return "doc"
    return "skip"


# --- chunking (structure-aware; never truncate) ------------------------------

_H_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_PY_DEF_RE = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_EXPORT_SYM_RE = re.compile(
    r"^\s*(?:export\s+)?(?:public\s+)?(?:async\s+)?"
    r"(?:func|function|class|interface|type|def|struct|enum)\s+([A-Za-z_][A-Za-z0-9_]*)")
_PROTO_MSG_RE = re.compile(r"^\s*(?:message|service|enum)\s+([A-Za-z_][A-Za-z0-9_]*)")
_GRAPHQL_RE = re.compile(r"^\s*(?:type|input|enum|interface)\s+([A-Za-z_][A-Za-z0-9_]*)")


def _doc_units(rel_path: str, text: str) -> list[dict[str, Any]]:
    """Split a doc along h2 headings; a file with no h2 stays one whole unit (never truncated)."""
    lines = text.splitlines(keepends=True)
    # find h2 boundaries
    idxs = [i for i, ln in enumerate(lines) if re.match(r"^##\s+", ln)]
    units: list[dict[str, Any]] = []
    if not idxs:
        anchor = _doc_title(text) or rel_path
        units.append(_mk_unit(rel_path, "doc", anchor, text))
        return units
    # preamble before first h2 (only if it has non-blank content)
    if idxs[0] > 0:
        pre = "".join(lines[: idxs[0]])
        if pre.strip():
            anchor = _doc_title(pre) or f"{rel_path}#preamble"
            units.append(_mk_unit(rel_path, "doc", anchor, pre))
    bounds = idxs + [len(lines)]
    for k in range(len(idxs)):
        seg = "".join(lines[bounds[k]: bounds[k + 1]])
        m = re.match(r"^##\s+(.+?)\s*$", lines[idxs[k]])
        anchor = "## " + (m.group(1).strip() if m else f"section-{k}")
        units.append(_mk_unit(rel_path, "doc", anchor, seg))
    return units


def _doc_title(text: str) -> Optional[str]:
    for ln in text.splitlines():
        m = _H_RE.match(ln)
        if m:
            return m.group(2).strip()
    return None


def _code_units(rel_path: str, text: str) -> list[dict[str, Any]]:
    """Emit a code-doc unit for a module/docstring header + an interface unit per EXPORTED symbol.

    Private symbols (leading underscore) are not emitted as units at all — the value-gate the
    aggressive-code choice demands starts here (a bare private helper never becomes a unit)."""
    ext = os.path.splitext(rel_path)[1].lower()
    units: list[dict[str, Any]] = []
    stem = Path(rel_path).stem

    # module/docstring header → code-doc
    header = _module_header(ext, text)
    if header:
        units.append(_mk_unit(rel_path, "code-doc", f"module:{stem}", header))

    # exported symbols → interface
    if ext == ".proto":
        sym_re = _PROTO_MSG_RE
    elif ext in (".graphql", ".gql"):
        sym_re = _GRAPHQL_RE
    elif ext == ".py":
        sym_re = None  # handled specially (public = no leading underscore)
    else:
        sym_re = _EXPORT_SYM_RE

    lines = text.splitlines()
    if ext == ".py":
        for i, ln in enumerate(lines):
            m = _PY_DEF_RE.match(ln)
            if m and not m.group(1).startswith("_"):
                body = _symbol_block(lines, i)
                units.append(_mk_unit(rel_path, "interface", f"sym:{m.group(1)}", body))
    else:
        for i, ln in enumerate(lines):
            m = sym_re.match(ln) if sym_re else None
            if m and not m.group(1).startswith("_"):
                body = _symbol_block(lines, i)
                units.append(_mk_unit(rel_path, "interface", f"sym:{m.group(1)}", body))
    return units


def _module_header(ext: str, text: str) -> Optional[str]:
    """The leading module docstring / block header, if the author wrote one."""
    stripped = text.lstrip()
    if ext == ".py":
        m = re.match(r'^(?:from __future__.*?\n\s*)?("""|\'\'\')(.*?)(\1)', stripped, re.S)
        if m and m.group(2).strip():
            return m.group(2).strip()
    # generic: a leading /* ... */ or // block or # block comment
    lead = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(("//", "#", "*", "/*")) and not s.startswith("#!"):
            lead.append(re.sub(r"^[/*#\s]+", "", ln).rstrip())
        elif not s:
            if lead:
                break
        else:
            break
    header = "\n".join(l for l in lead if l).strip()
    return header or None


def _symbol_block(lines: list[str], start: int, max_lines: int = 60) -> str:
    """A bounded slice starting at a symbol's declaration (signature + leading body/docstring).
    Never the whole file; the skill distills semantics, so this is context, not a transcription."""
    end = min(start + max_lines, len(lines))
    # stop at the next top-level declaration for tighter bounds
    for j in range(start + 1, end):
        if lines[j] and not lines[j][0].isspace() and (
                _PY_DEF_RE.match(lines[j]) or _EXPORT_SYM_RE.match(lines[j])
                or _PROTO_MSG_RE.match(lines[j])):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def _mk_unit(rel_path: str, kind: str, anchor: str, text: str) -> dict[str, Any]:
    return {
        "id": _unit_id(rel_path, anchor),
        "path": rel_path,
        "kind": kind,
        "anchor": anchor,
        "hash": _content_hash(text),
        "bytes": len(text.encode("utf-8")),
    }


# --- walk --------------------------------------------------------------------

def _globs(spec: Optional[str]) -> list[str]:
    if not spec:
        return []
    return [g.strip() for g in re.split(r"[;,]", spec) if g.strip()]


def _matches_any(rel_path: str, globs: list[str]) -> bool:
    from fnmatch import fnmatch
    name = os.path.basename(rel_path)
    return any(fnmatch(rel_path, g) or fnmatch(name, g) for g in globs)


def _walk(root: Path, include: list[str], exclude: list[str]) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        # prune skip dirs in place
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = str(p.relative_to(root))
            if exclude and _matches_any(rel, exclude):
                continue
            if include and not _matches_any(rel, include):
                continue
            yield p


def probe(root: str, include: Optional[str] = None, exclude: Optional[str] = None,
          max_bytes: int = MAX_BYTES_DEFAULT) -> dict[str, Any]:
    """Enumerate → classify → chunk → hash. Returns candidate units + a scope estimate (gate #1)."""
    rootp = Path(root).resolve()
    inc, exc = _globs(include), _globs(exclude)
    units: list[dict[str, Any]] = []
    counts = {"doc": 0, "interface": 0, "code-doc": 0, "config": 0, "skip": 0}
    skipped_secrets = 0
    bytes_total = 0
    for p in sorted(_walk(rootp, inc, exc)):
        rel = str(p.relative_to(rootp))
        name = p.name
        if _is_secret(name):
            skipped_secrets += 1          # counted; bytes NEVER opened
            continue
        kind = classify(rel)
        if kind == "skip":
            counts["skip"] += 1
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > max_bytes:
            counts["skip"] += 1           # over the per-file cap → not read
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            counts["skip"] += 1           # binary/undecodable → skip
            continue
        bytes_total += len(text.encode("utf-8"))
        file_units = _units_for(rel, kind, text)
        for u in file_units:
            counts[u["kind"]] = counts.get(u["kind"], 0) + 1
        units.extend(file_units)
    est_atoms = sum(1 for u in units if u["kind"] != "skip")
    return {"root": str(rootp), "units": units, "counts": counts,
            "est_atoms": est_atoms, "bytes_total": bytes_total,
            "skipped_secrets": skipped_secrets}


def _units_for(rel: str, kind: str, text: str) -> list[dict[str, Any]]:
    ext = os.path.splitext(rel)[1].lower()
    if kind == "doc":
        return _doc_units(rel, text)
    if ext in CODE_EXTS or ext in SCHEMA_EXTS:
        cu = _code_units(rel, text)
        return cu or [_mk_unit(rel, "interface", f"file:{Path(rel).stem}", text)]
    # config / code-doc README / other single-unit kinds → one whole unit
    anchor = _doc_title(text) or rel
    return [_mk_unit(rel, kind, anchor, text)]


# --- source acquisition ------------------------------------------------------

def _git_sha(root: Path) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def acquire(source: str, cache: Optional[str] = None) -> dict[str, Any]:
    """Local path → use in place; remote URL → shallow clone into a read-only cache dir. Never
    mutates the source working tree; the graph is untouched."""
    if _looks_remote(source):
        cache_dir = Path(cache) if cache else Path(
            os.environ.get("MNEMEX_INGEST_CACHE") or mnx_common.mnemex_home() / "ingest-cache")
        slug = source_slug(source)
        dest = cache_dir / slug
        if not (dest / ".git").is_dir():
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--depth", "1", source, str(dest)],
                           check=True, capture_output=True, text=True, timeout=300)
        return {"kind": "remote", "root": str(dest.resolve()), "commit": _git_sha(dest),
                "cached": True}
    p = Path(source).resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"source path not found: {source}")
    return {"kind": "local", "root": str(p), "commit": _git_sha(p), "cached": False}


def _looks_remote(source: str) -> bool:
    return bool(re.match(r"^(https?://|git@|ssh://|git://)", source)) or source.endswith(".git")


def source_slug(source: str) -> str:
    """Stable slug for a source URL / abs path — mirrors mnx_binding.graph_slug's hashing scheme so
    it matches the staging slug family."""
    norm = source.rstrip("/").lower()
    norm = re.sub(r"^(https?://|git@|ssh://|git://)", "", norm).replace(":", "/")
    norm = re.sub(r"\.git$", "", norm)
    base = mnx_common.slugify(os.path.basename(norm)) or "corpus"
    h = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{h}"


# --- ingest manifest & delta -------------------------------------------------

def manifest_path(graph_root: str, source_slug_: str) -> Path:
    return mnx_common.state_dir(graph_root) / INGEST_STATE_SUBDIR / f"{source_slug_}.json"


def read_manifest(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def manifest_write(graph_root: str, source_slug_: str, files: dict[str, Any],
                   source_repo: Optional[str] = None, last_commit: Optional[str] = None) -> dict[str, Any]:
    """Write/refresh the ingest manifest (protocol state, committed with the graph beside highwater).
    `files` maps source_path -> {hash, nodes:[id…]}."""
    path = manifest_path(graph_root, source_slug_)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_manifest(path)
    merged = dict(existing.get("files", {}))
    merged.update(files)
    payload = {
        "source_repo": source_repo or existing.get("source_repo"),
        "last_commit": last_commit or existing.get("last_commit"),
        "ingested_at": mnx_common.now_utc(),
        "files": merged,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"path": str(path), "files": len(merged)}


def delta(root: str, manifest: str, include: Optional[str] = None,
          exclude: Optional[str] = None, max_bytes: int = MAX_BYTES_DEFAULT) -> dict[str, Any]:
    """Diff the walked source against a prior manifest at FILE granularity.

    added   = files present now, absent from the manifest
    changed = files whose content hash differs from the manifest
    orphans = files in the manifest that are gone from the source now (their node_ids surface as
              orphan CANDIDATES — never auto-tombstoned; the human decides)."""
    rootp = Path(root).resolve()
    inc, exc = _globs(include), _globs(exclude)
    man = read_manifest(manifest)
    man_files: dict[str, Any] = man.get("files", {})
    seen: set[str] = set()
    added, changed = [], []
    unchanged = 0
    for p in sorted(_walk(rootp, inc, exc)):
        rel = str(p.relative_to(rootp))
        if _is_secret(p.name) or classify(rel) == "skip":
            continue
        try:
            if p.stat().st_size > max_bytes:
                continue
            fh = _file_hash(p)
        except (OSError, UnicodeDecodeError):
            continue
        seen.add(rel)
        rec = {"path": rel, "hash": fh}
        if rel not in man_files:
            added.append(rec)
        elif man_files[rel].get("hash") != fh:
            changed.append(rec)
        else:
            unchanged += 1
    orphans = [{"path": rel, "node_ids": man_files[rel].get("nodes", [])}
               for rel in sorted(man_files) if rel not in seen]
    return {"added": added, "changed": changed, "unchanged": unchanged, "orphans": orphans}


# --- cli --------------------------------------------------------------------

def _arg(argv: list[str], flag: str) -> Optional[str]:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


_USAGE = [
    'mnx_ingest.py acquire --source <url-or-path> [--cache <dir>]  — materialize a read-only corpus cache',
    'mnx_ingest.py probe --root <dir> [--include <glob>] [--exclude <glob>] [--max-bytes <n>]  — classify files into extraction units',
    'mnx_ingest.py delta --root <dir> --manifest <file> [--include <glob>] [--exclude <glob>] [--max-bytes <n>]  — added/changed/unchanged/orphans vs a prior ingest manifest',
    'mnx_ingest.py manifest-write --graph <root> --source-slug <slug> [--json < files.json]  — record the ingest manifest for re-run deltas',
    'mnx_ingest.py source-slug --source <url-or-path>  — the manifest slug for a source',
]
_FLAGS = {"--source": True, "--cache": True, "--root": True, "--include": True, "--exclude": True, "--max-bytes": True, "--manifest": True, "--graph": True, "--source-slug": True, "--json": False}


def _main(argv: list[str]) -> int:
    handled = mnx_common.cli_guard(argv, _USAGE, _FLAGS)
    if handled is not None:
        return handled
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "acquire":
            return mnx_common.emit(acquire(_arg(argv, "--source"), _arg(argv, "--cache")))
        if cmd == "probe":
            return mnx_common.emit(probe(
                _arg(argv, "--root") or ".", _arg(argv, "--include"), _arg(argv, "--exclude"),
                int(_arg(argv, "--max-bytes") or MAX_BYTES_DEFAULT)))
        if cmd == "delta":
            return mnx_common.emit(delta(
                _arg(argv, "--root") or ".", _arg(argv, "--manifest") or "",
                _arg(argv, "--include"), _arg(argv, "--exclude"),
                int(_arg(argv, "--max-bytes") or MAX_BYTES_DEFAULT)))
        if cmd == "manifest-write":
            payload = json.loads(sys.stdin.read() or "{}") if "--json" in argv else {}
            files = payload.get("files", payload) if isinstance(payload, dict) else {}
            return mnx_common.emit(manifest_write(
                _arg(argv, "--graph") or ".", _arg(argv, "--source-slug") or "corpus",
                files, payload.get("source_repo"), payload.get("last_commit")))
        if cmd == "source-slug":
            return mnx_common.emit({"slug": source_slug(_arg(argv, "--source") or "")})
        return mnx_common.emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return mnx_common.emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
