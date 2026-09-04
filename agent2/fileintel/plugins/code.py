# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/plugins/code.py
────────────────────────────────
Code plugin: source files (py, js, ts, java, go, rs, c, cpp, cs, php, css, sql,
json, yaml, xml, sh, …).

Operations
  read ...... file contents (capped)
  analyze ... line counts (code/comment/blank), rough symbol list, imports
  metadata .. size + language

Pure stdlib — no third-party deps. Deeper structural analysis (ASTs) is a future
plugin; this gives the agent a fast, dependency-free overview.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent2.fileintel.base import Capability, PluginContext
from agent2.fileintel.errors import BackendError

_CODE_FORMATS = ["py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "c", "cpp",
                 "h", "hpp", "cs", "php", "css", "sql", "json", "yaml", "yml",
                 "xml", "sh", "bat", "ps1", "rb", "toml", "ini", "cfg"]

_LANG = {"py": "Python", "js": "JavaScript", "ts": "TypeScript", "tsx": "TypeScript",
         "jsx": "JavaScript", "java": "Java", "go": "Go", "rs": "Rust", "c": "C",
         "cpp": "C++", "h": "C/C++ header", "hpp": "C++ header", "cs": "C#",
         "php": "PHP", "css": "CSS", "sql": "SQL", "json": "JSON", "yaml": "YAML",
         "yml": "YAML", "xml": "XML", "sh": "Shell", "bat": "Batch",
         "ps1": "PowerShell", "rb": "Ruby", "toml": "TOML", "ini": "INI", "cfg": "Config"}

# single-line comment markers by language family
_COMMENT = {"py": "#", "rb": "#", "sh": "#", "yaml": "#", "yml": "#", "toml": "#",
            "ini": "#", "cfg": "#", "ps1": "#"}
_SYMBOL_RX = {
    "py": re.compile(r"^\s*(?:def|class)\s+(\w+)"),
    "js": re.compile(r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\()"),
    "ts": re.compile(r"(?:function\s+(\w+)|class\s+(\w+)|interface\s+(\w+))"),
    "go": re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)"),
    "rs": re.compile(r"^\s*(?:pub\s+)?fn\s+(\w+)"),
    "java": re.compile(r"(?:class|interface)\s+(\w+)"),
    "cs": re.compile(r"(?:class|interface|struct)\s+(\w+)"),
}


def _fmt(path: str) -> str:
    from agent2.fileintel.detector import detect
    return detect(path)["format"]


def op_read(path: str, options: dict, ctx: PluginContext) -> dict:
    ctx.progress("Reading source…")
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise BackendError(f"Could not read file: {exc}")
    fmt = _fmt(path)
    return {"language": _LANG.get(fmt, fmt), "format": fmt,
            "text": text[:100_000], "chars": len(text),
            "lines": text.count("\n") + 1}


def op_analyze(path: str, options: dict, ctx: PluginContext) -> dict:
    ctx.progress("Analysing source…")
    fmt = _fmt(path)
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        raise BackendError(f"Could not read file: {exc}")
    marker = _COMMENT.get(fmt, "//")
    blank = sum(1 for ln in lines if not ln.strip())
    comment = sum(1 for ln in lines if ln.strip().startswith(marker))
    symbols = []
    rx = _SYMBOL_RX.get("ts" if fmt in ("tsx", "jsx") else
                        "js" if fmt in ("js",) else fmt)
    if rx:
        for ln in lines:
            m = rx.search(ln)
            if m:
                symbols.append(next((g for g in m.groups() if g), ""))
    imports = [ln.strip() for ln in lines
               if re.match(r"^\s*(import|from|#include|use|require|using)\b", ln)][:100]
    return {"language": _LANG.get(fmt, fmt), "format": fmt,
            "total_lines": len(lines), "blank_lines": blank,
            "comment_lines": comment, "code_lines": len(lines) - blank - comment,
            "symbols": [s for s in symbols if s][:200],
            "imports": imports}


def register(reg) -> None:
    reg.register("code", _CODE_FORMATS, [
        Capability("read", op_read, backend="native"),
        Capability("analyze", op_analyze, backend="native"),
        Capability("metadata", op_read, backend="native"),
    ])
