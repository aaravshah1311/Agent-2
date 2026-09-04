# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/search.py
──────────────────────────
Workspace-wide file search — the engine behind the `search_workspace` tool.

Kinds
  content ...... regex/keyword across text-extractable files (code, txt, md,
                 html, csv, AND pdf/docx/etc. via the documents extractor)
  filename ..... match the query against file names
  recent ....... files modified within N days (options via `query`, e.g. "1")
  secrets ...... scan for API-key / credential-looking strings
  duplicates ... group files by sha256 to find exact duplicates

Presets map natural-language intents ("find every invoice", "locate every API
key", "all TODO comments") onto these kinds. Detection stays dependency-free;
document text extraction is best-effort (skips a file if its lib is missing).
"""

from __future__ import annotations

import re
from pathlib import Path

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
              "build", ".next", "target", ".idea", ".vscode", ".cache"}
_TEXT_EXTS = {"txt", "md", "html", "htm", "csv", "tsv", "json", "yaml", "yml",
              "xml", "py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "c",
              "cpp", "h", "hpp", "cs", "php", "css", "sql", "sh", "toml", "ini",
              "cfg", "log", "rst"}
_DOC_EXTS = {"pdf", "docx", "rtf", "odt", "epub"}
_MAX_HITS = 300

# Common secret / API-key signatures.
# Case-sensitive vendor token signatures.
_SECRET_TOKEN_RX = re.compile(
    r"(AKIA[0-9A-Z]{16}"                       # AWS access key
    r"|AIza[0-9A-Za-z\-_]{35}"                 # Google API key
    r"|sk-[A-Za-z0-9]{20,}"                    # OpenAI-style
    r"|ghp_[A-Za-z0-9]{36}"                    # GitHub PAT
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"           # Slack
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"  # private keys
)
# Case-insensitive key=value assignments (kept separate: an inline (?i) flag
# mid-pattern is a hard error on Python 3.11+).
_SECRET_KV_RX = re.compile(
    r"(?:api[_-]?key|secret|passwd|password|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]",
    re.IGNORECASE,
)


def _has_secret(line: str) -> bool:
    return bool(_SECRET_TOKEN_RX.search(line) or _SECRET_KV_RX.search(line))

_PRESETS = {
    "invoice": ("filename", r"invoice"),
    "invoices": ("filename", r"invoice"),
    "todo": ("content", r"\b(TODO|FIXME|XXX|HACK)\b"),
    "todos": ("content", r"\b(TODO|FIXME|XXX|HACK)\b"),
    "api key": ("secrets", ""),
    "api keys": ("secrets", ""),
    "secret": ("secrets", ""),
    "secrets": ("secrets", ""),
    "credentials": ("secrets", ""),
    "duplicate": ("duplicates", ""),
    "duplicates": ("duplicates", ""),
}


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and not any(part in _SKIP_DIRS for part in p.parts):
            yield p


def _extract_text_safe(p: Path, ext: str) -> str | None:
    """Read text-ish files directly; extract doc text best-effort (may skip)."""
    if ext in _TEXT_EXTS:
        try:
            return p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None
    if ext in _DOC_EXTS:
        try:
            from agent2.fileintel.plugins.documents import _extract_text
            return _extract_text(str(p))[0]
        except Exception:
            return None  # lib missing or unreadable → skip silently
    return None


def search(query: str, path: str = ".", kind: str = "content",
           workspace_root: str | None = None) -> dict:
    """Run a workspace search. Never raises."""
    root = Path(path).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    root = root.resolve()
    if workspace_root:
        try:
            root.relative_to(Path(workspace_root).resolve())
        except ValueError:
            return {"error": "search path escapes the workspace", "code": "unsafe_path"}
    if not root.exists():
        return {"error": f"Path not found: {root}", "code": "unsafe_path"}

    q = (query or "").strip()
    preset = _PRESETS.get(q.lower())
    if preset:
        kind, q = preset[0], (preset[1] or q)

    try:
        if kind == "filename":
            return _search_filename(root, q)
        if kind == "recent":
            return _search_recent(root, q)
        if kind == "secrets":
            return _search_secrets(root)
        if kind == "duplicates":
            return _search_duplicates(root)
        return _search_content(root, q)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "code": "search_error"}


def _search_content(root: Path, q: str) -> dict:
    try:
        rx = re.compile(q, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(q), re.IGNORECASE)
    hits, scanned = [], 0
    for p in _iter_files(root):
        ext = p.suffix.lstrip(".").lower()
        text = _extract_text_safe(p, ext)
        if text is None:
            continue
        scanned += 1
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{p}:{i}: {line.strip()[:200]}")
                if len(hits) >= _MAX_HITS:
                    return {"kind": "content", "query": q, "matches": hits,
                            "match_count": len(hits), "files_scanned": scanned,
                            "truncated": True}
    return {"kind": "content", "query": q, "matches": hits,
            "match_count": len(hits), "files_scanned": scanned}


def _search_filename(root: Path, q: str) -> dict:
    try:
        rx = re.compile(q, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(q), re.IGNORECASE)
    hits = [str(p) for p in _iter_files(root) if rx.search(p.name)]
    return {"kind": "filename", "query": q, "matches": hits[:_MAX_HITS],
            "match_count": len(hits)}


def _search_recent(root: Path, q: str) -> dict:
    import time
    try:
        days = float(re.search(r"[\d.]+", q).group()) if q else 1.0
    except Exception:
        days = 1.0
    cutoff = time.time() - days * 86400
    hits = []
    for p in _iter_files(root):
        try:
            if p.stat().st_mtime >= cutoff:
                hits.append(str(p))
        except Exception:
            pass
    return {"kind": "recent", "within_days": days, "matches": hits[:_MAX_HITS],
            "match_count": len(hits)}


def _search_secrets(root: Path) -> dict:
    hits = []
    for p in _iter_files(root):
        ext = p.suffix.lstrip(".").lower()
        if ext not in _TEXT_EXTS and ext not in {"env", "pem", "key", "cfg", "conf"}:
            if p.name not in (".env", ".env.local"):
                continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _has_secret(line):
                redacted = line.strip()[:120]
                hits.append(f"{p}:{i}: {redacted}")
                if len(hits) >= _MAX_HITS:
                    return {"kind": "secrets", "matches": hits,
                            "match_count": len(hits), "truncated": True}
    return {"kind": "secrets", "matches": hits, "match_count": len(hits)}


def _search_duplicates(root: Path) -> dict:
    import hashlib
    from collections import defaultdict
    by_hash: dict[str, list[str]] = defaultdict(list)
    for p in _iter_files(root):
        try:
            if p.stat().st_size > 100 * 1024 * 1024:
                continue
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            by_hash[h].append(str(p))
        except Exception:
            pass
    dups = {h: paths for h, paths in by_hash.items() if len(paths) > 1}
    return {"kind": "duplicates", "duplicate_groups": list(dups.values()),
            "group_count": len(dups)}
