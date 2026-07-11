"""mnx_common.py — shared primitives for the Mnemex Context Graph.

The single source of truth for time, parsing, and id rules. Every other Mnemex
script imports from here; nothing else writes a timestamp or mints an id.

Dependencies: Python 3.9+ stdlib + PyYAML only. See docs/script-contracts.md.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared in README
    yaml = None

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"
SECONDS_PER_DAY = 86400.0

CONFIG_FILENAME = "mnemex.config.md"
INDEX_FILENAME = "index.md"
REGISTRY_FILENAME = "registry.md"
CROSSLINKS_FILENAME = "cross-links.md"
PHONEBOOK_FILENAME = "phonebook.md"
STATE_DIRNAME = ".mnemex"
# Tier-per-file derived files (W3): the split warm/cold/dead tiers. Like the index, they are
# generated navigation, NOT nodes — they must never be iterated as node files.
_TIER_FILES = {"warm.md", "cold.md", "dead.md"}
NON_NODE_FILES = {INDEX_FILENAME, REGISTRY_FILENAME, CROSSLINKS_FILENAME, CONFIG_FILENAME,
                  PHONEBOOK_FILENAME} | _TIER_FILES

_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Chained continuation chunks (index.001.md, cold.001.md, …) are derived navigation, not nodes.
_INDEX_CONT_RE = re.compile(r"^(?:index|cold)\.\d+\.md$")


def _require_yaml() -> None:
    if yaml is None:
        raise RuntimeError("PyYAML is required for Mnemex (pip install pyyaml).")


# --- user-level home ---------------------------------------------------------

def mnemex_home() -> Path:
    """User-level Mnemex state root: config.md, graph clones, staging, run markers, caches.

    Durable across plugin/package updates and shared by every agent host (Claude Code, MCP
    clients, …) so captures staged under one agent are visible to all the others.

    Resolution precedence (first hit wins; documented in docs/configuration.md):
      1. $MNEMEX_HOME                      — explicit override, agent-agnostic
      2. $CLAUDE_CONFIG_DIR/mnemex         — Claude Code with a relocated config dir
      3. ~/.claude/mnemex  IF IT EXISTS    — back-compat: existing installs stay untouched
      4. $XDG_DATA_HOME/mnemex (default ~/.local/share/mnemex) — fresh installs, agent-neutral

    Resolution is side-effect-free: this never creates a directory — writers mkdir what
    they need, so the fresh-install location only materializes on first write.
    """
    env = os.environ.get("MNEMEX_HOME")
    if env:
        return Path(env).expanduser()
    claude_cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if claude_cfg:
        return Path(claude_cfg).expanduser() / "mnemex"
    legacy = Path.home() / ".claude" / "mnemex"
    if legacy.is_dir():
        return legacy
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "mnemex"


def plugin_root() -> Path:
    """The engine directory — the folder holding the mnx_*.py modules themselves.

    Precedence: $MNEMEX_ROOT (explicit override, may point at either the engine dir or a
    checkout root containing scripts/ — both normalize to the engine dir) → this file's own
    resolved location. Self-location is authoritative so a pip-installed engine never mixes
    with a plugin checkout; the literal ``${CLAUDE_PLUGIN_ROOT}`` reference survives only in
    the generated hook commands (hooks/hooks.json, mnx_hooks) where Claude Code expands it.
    """
    val = os.environ.get("MNEMEX_ROOT")
    if val:
        p = Path(val).expanduser()
        return p / "scripts" if (p / "scripts" / "mnx_common.py").is_file() else p
    return Path(__file__).resolve().parent


# --- time -------------------------------------------------------------------

def now_utc() -> str:
    """Current time as an ISO-8601 UTC string, second precision. The ONLY clock."""
    return datetime.now(timezone.utc).strftime(ISO_FMT)


def canon_ts(v: Any) -> Any:
    """Normalize a datetime/date (PyYAML auto-parses ISO timestamps) to a canonical
    UTC 'YYYY-MM-DDTHH:MM:SSZ' string. Non-temporal values pass through unchanged."""
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc)
        return v.strftime(ISO_FMT)
    if isinstance(v, date):
        return v.strftime("%Y-%m-%dT00:00:00Z")
    return v


def _normalize_dates(obj: Any) -> Any:
    """Recursively coerce any datetime/date values to canonical Z-strings."""
    if isinstance(obj, dict):
        return {k: _normalize_dates(val) for k, val in obj.items()}
    if isinstance(obj, list):
        return [_normalize_dates(x) for x in obj]
    return canon_ts(obj)


def parse_ts(s: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    if isinstance(s, (datetime, date)):
        s = canon_ts(s)
    s = (s or "").strip()
    if not s:
        raise ValueError("empty timestamp")
    if s.endswith("Z"):
        try:
            return datetime.strptime(s, ISO_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def clamp_dt(t_from: str, t_to: str) -> float:
    """max(0.0, seconds) between two ISO-8601 UTC timestamps.

    Clamped so clock skew across machines/sessions can never invert decay into
    growth (F4). Note: callers that work in *days* divide by SECONDS_PER_DAY.
    """
    return max(0.0, (parse_ts(t_to) - parse_ts(t_from)).total_seconds())


def is_iso_utc(s: str) -> bool:
    try:
        parse_ts(s)
        return isinstance(s, str) and s.strip().endswith("Z")
    except Exception:
        return False


# --- ids --------------------------------------------------------------------

def slugify(title: str) -> str:
    """Return a candidate id slug ([a-z0-9-]+). Caller ensures uniqueness."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "node"


def is_valid_id(s: str) -> bool:
    """True iff s is a valid stable id slug ([a-z0-9-]+, no spaces, no edge/dup hyphens)."""
    return isinstance(s, str) and bool(_ID_RE.match(s))


# --- front-matter -----------------------------------------------------------

def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (front-matter dict, body). Empty dict if no leading YAML block."""
    _require_yaml()
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    # The closing fence is the next line that is EXACTLY '---'. Splitting on the raw
    # substring '---' is wrong: a comment inside the block (e.g. '# --- Tiers ---')
    # would be mistaken for the fence, truncating the parsed front-matter.
    close = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close = i
            break
    if close is None:
        return {}, text
    fm_text = "".join(lines[1:close])
    # Preserve the original body semantics: everything after the closing fence's
    # '---' (including the remainder of the fence line), matching the prior split.
    fence_line = lines[close]
    dash = fence_line.index("---")
    body = fence_line[dash + 3:] + "".join(lines[close + 1:])
    data = yaml.safe_load(fm_text) or {}
    if not isinstance(data, dict):
        raise ValueError("front-matter is not a mapping")
    return _normalize_dates(data), body


def read_frontmatter(path: str | Path) -> dict[str, Any]:
    fm, _ = split_frontmatter(Path(path).read_text(encoding="utf-8"))
    return fm


# --- markdown helpers -------------------------------------------------------

def split_h2_sections(text: str) -> dict[str, str]:
    """Split markdown into {h2-title: section-body} keyed by '## ' headers."""
    sections: dict[str, str] = {}
    current: Optional[str] = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = re.sub(r"\s*<!--.*?-->\s*$", "", m.group(1)).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def parse_md_table(section_text: str) -> list[dict[str, str]]:
    """Parse a GitHub-flavoured markdown table into a list of row dicts keyed by
    the (lower-cased) header cells. Separator rows are skipped."""
    rows = [ln for ln in section_text.splitlines() if ln.strip().startswith("|")]
    if len(rows) < 1:
        return []
    headers = [h.strip().lower() for h in rows[0].strip().strip("|").split("|")]
    out: list[dict[str, str]] = []
    for ln in rows[1:]:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c or "-") for c in cells):
            continue  # separator
        row = {headers[i]: (cells[i] if i < len(cells) else "") for i in range(len(headers))}
        if any(v for v in row.values()):
            out.append(row)
    return out


def aliases_to_index(aliases: Any) -> str:
    """Serialize a node's aliases list to the index's '; '-joined cell form."""
    if isinstance(aliases, str):
        aliases = [aliases]
    return "; ".join(str(a).strip() for a in (aliases or []) if str(a).strip())


def aliases_from_index(cell: str) -> list[str]:
    """Parse an index aliases cell back into a list."""
    return [a.strip() for a in (cell or "").split(";") if a.strip()]


# --- wiki-links (Link Reconciliation) ----------------------------------------------------

_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


def parse_wikilinks(body: str) -> list[dict[str, Any]]:
    """Extract inline [[wiki-links]] from a node/atom body (Link Reconciliation, the mesh authoring surface).

    Wiki-native: `[[name]]` is an untyped link; `[[name|Display]]` carries display text (the pipe
    means DISPLAY, exactly like Obsidian/Wikipedia — NOT a relationship type). An optional link
    `type` is a rare escape and lives in front-matter `mentions[].type`, never inline, so the body
    stays pure wiki. Returns [{name, display?}] de-duplicated by normalized name, first occurrence
    wins, source order preserved. Empty/whitespace targets are skipped.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _WIKILINK_RE.finditer(body or ""):
        raw = m.group(1)
        parts = [p.strip() for p in raw.split("|")]
        name = parts[0]
        if not name:
            continue
        key = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        entry: dict[str, Any] = {"name": name}
        if len(parts) > 1 and parts[1]:
            entry["display"] = parts[1]
        out.append(entry)
    return out


# --- node / index parsing ---------------------------------------------------

def parse_node(path: str | Path) -> dict[str, Any]:
    """Parse a node file. Rejects malformed front-matter rather than guessing."""
    p = Path(path)
    fm, body = split_frontmatter(p.read_text(encoding="utf-8"))
    if not fm:
        raise ValueError(f"{p}: missing or malformed front-matter")
    node = dict(fm)
    node.setdefault("aliases", [])
    node.setdefault("edges", [])
    node.setdefault("mentions", [])
    node.setdefault("references", [])
    node["_path"] = str(p)
    node["_body"] = body
    node["_sections"] = split_h2_sections(body)
    return node


WARM_FILENAME = "warm.md"
COLD_FILENAME = "cold.md"
DEAD_FILENAME = "dead.md"
_COLD_CONT_RE = re.compile(r"^cold\.\d+\.md$")


def parse_index(path: str | Path) -> dict[str, Any]:
    """Parse an index.md into {description, children[], hot[], warm[], cold[], dead[]}.

    Tier-per-file aware (W3): when the cluster uses the split layout, the head index.md is a slim
    ROUTER (Hot only) and the Warm/Cold/Dead tiers live in sibling warm.md / cold.md (+ cold.NNN.md)
    / dead.md files. parse_index merges them transparently, so every consumer (seed, renorm,
    doctor, status) sees the full tier set regardless of layout. When no tier files exist (the
    single-file layout), behavior is identical to before.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    description = ""
    for ln in text.splitlines():
        if ln.strip().startswith(">"):
            description = ln.strip().lstrip(">").strip()
            # Strip a trailing render-annotation comment (e.g. "<!-- chunk 1: route on
            # this -->") so a regenerated description does not re-append it each pass —
            # otherwise the marker accumulates and index regeneration is not idempotent.
            description = re.sub(r"\s*<!--.*?-->\s*$", "", description).strip()
            break
    sec = split_h2_sections(text)
    children = []
    for ln in sec.get("Children", "").splitlines():
        ln = ln.strip()
        if ln.startswith("- ") and "none" not in ln.lower():
            children.append(ln[2:].strip())
    out = {
        "description": description,
        "children": children,
        "hot": parse_md_table(sec.get("Hot", "")),
        "warm": parse_md_table(sec.get("Warm", "")),
        "cold": parse_md_table(sec.get("Cold", "")),
        "dead": parse_md_table(sec.get("Dead", "")),
    }
    if p.name == INDEX_FILENAME:  # only the head router pulls in sibling tier files
        _merge_tier_files(p.parent, out)
    return out


def _tier_table_rows(file_path: Path, *section_names: str) -> list[dict[str, str]]:
    if not file_path.is_file():
        return []
    sec = split_h2_sections(file_path.read_text(encoding="utf-8"))
    for name in section_names:
        if name in sec:
            return parse_md_table(sec[name])
    return []


def _merge_tier_files(dirp: Path, out: dict[str, Any]) -> None:
    """If split tier files exist, extend the parsed tiers from them (W3)."""
    out["warm"] += _tier_table_rows(dirp / WARM_FILENAME, "Warm")
    out["dead"] += _tier_table_rows(dirp / DEAD_FILENAME, "Dead")
    cold_files = [dirp / COLD_FILENAME] + sorted(
        q for q in dirp.glob("cold.*.md") if _COLD_CONT_RE.match(q.name))
    for cf in cold_files:
        out["cold"] += _tier_table_rows(cf, "Cold")


def read_chunk(path: str | Path, section: str) -> str:
    """Ranged read of a single labeled section: head | hot | warm | cold | body."""
    text = Path(path).read_text(encoding="utf-8")
    section = section.lower()
    if section == "body":
        _, body = split_frontmatter(text)
        return body.strip()
    if section == "head":
        # description line through the end of Children (chunk 1 before Hot)
        out: list[str] = []
        for ln in text.splitlines():
            if re.match(r"^##\s+Hot", ln):
                break
            out.append(ln)
        return "\n".join(out).strip()
    sec = split_h2_sections(text)
    for key in (section.capitalize(), section):
        if key in sec:
            return sec[key]
    return ""


# --- graph / cluster layout -------------------------------------------------

def find_graph_root(start: str | Path) -> Optional[Path]:
    """Nearest ancestor (inclusive) containing mnemex.config.md — the graph root."""
    cur = Path(start).resolve()
    for d in [cur, *cur.parents]:
        if (d / CONFIG_FILENAME).is_file():
            return d
    return None


def require_graph_root(start: str | Path) -> Path:
    root = find_graph_root(start)
    if root is None:
        raise FileNotFoundError(
            f"No Mnemex graph root (mnemex.config.md) found at or above {start}."
        )
    return root


def state_dir(graph_root: str | Path) -> Path:
    return Path(graph_root) / STATE_DIRNAME


def iter_node_files(cluster: str | Path) -> list[Path]:
    """The node files in a cluster folder (excludes index/registry/cross-links)."""
    c = Path(cluster)
    if not c.is_dir():
        return []
    return sorted(p for p in c.glob("*.md")
                  if p.name not in NON_NODE_FILES and not _INDEX_CONT_RE.match(p.name))


def is_cluster(path: str | Path) -> bool:
    return bool(iter_node_files(path))


def iter_clusters(scope: str | Path) -> list[Path]:
    """All leaf node-folders at or under scope (a cluster, team, or graph root)."""
    scope = Path(scope)
    found: set[Path] = set()
    if is_cluster(scope):
        found.add(scope.resolve())
    for d in scope.rglob("*"):
        if d.is_dir() and STATE_DIRNAME not in d.parts and is_cluster(d):
            found.add(d.resolve())
    return sorted(found)


def cluster_key(graph_root: str | Path, cluster: str | Path) -> str:
    """Stable highwater/state key for a cluster, e.g. 'team-payments__settlement'."""
    rel = Path(cluster).resolve().relative_to(Path(graph_root).resolve())
    return str(rel).replace(os.sep, "__")


def team_of(graph_root: str | Path, path: str | Path) -> Optional[str]:
    """The top-level team folder name for a path inside the graph (e.g. 'team-payments')."""
    rel = Path(path).resolve().relative_to(Path(graph_root).resolve())
    return rel.parts[0] if rel.parts else None


# --- shared CLI output ------------------------------------------------------

def emit(payload: dict[str, Any], ok: bool = True) -> int:
    """Print the JSON payload, then the STATUS line. Return a process exit code."""
    print(json.dumps(payload, default=str))
    print("STATUS=OK" if ok else "STATUS=FAIL")
    return 0 if ok else 1


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "now"
    try:
        if cmd == "now":
            return emit({"now": now_utc()})
        if cmd == "slugify":
            return emit({"slug": slugify(argv[2])})
        if cmd == "is-valid-id":
            return emit({"id": argv[2], "valid": is_valid_id(argv[2])})
        if cmd == "parse-node":
            node = parse_node(argv[2])
            node.pop("_body", None)
            return emit(node)
        if cmd == "parse-index":
            return emit(parse_index(argv[2]))
        if cmd == "clusters":
            return emit({"clusters": [str(c) for c in iter_clusters(argv[2])]})
        return emit({"error": f"unknown subcommand: {cmd}"}, ok=False)
    except Exception as exc:
        return emit({"error": str(exc)}, ok=False)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
