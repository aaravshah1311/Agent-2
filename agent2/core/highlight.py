# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/highlight.py
────────────────────────
THE tokenizer for syntax-highlighted diffs. Shared by the CLI's Ctrl+B viewer and
the Web UI's diff panel — computation only, no rendering.

⚠️ WHY THIS IS IN `core/` AND NOT `cli/`.
Same rule as `core/diffs.py`: both surfaces highlight the same diff, so a second
tokenizer would drift and the drift would be invisible — each surface looks right
on its own. The CLI maps a token kind to an ANSI colour, the browser maps it to a
CSS class; the *decision about what a token is* is made exactly once, here.

⚠️ THIS IS PRESENTATION DATA AND MAY NEVER RAISE.
Highlighting is decoration on top of a diff that is itself decoration. A weird
encoding, a pathological line, an unknown language — all degrade to one plain
`("text", line)` token. Every public entry point is total.

⚠️ NOT A PARSER, AND DELIBERATELY SO.
No AST, no per-language grammar, no Pygments dependency (which is optional at
best and absent on the minimal install this repo must keep working). One regex
alternation, ordered so the greedy cases win: strings and comments swallow their
contents before keyword matching can see inside them. That is why `# TODO: def`
does not highlight `def`, and `"class"` stays a string.

Layer: (nothing) → core.highlight. Imports stdlib only.
"""

import re

# Token kinds a renderer must handle. Part of the contract — the CLI's ANSI map
# and the browser's CSS both switch on these, so adding a kind means touching
# both surfaces.
KINDS = ("text", "keyword", "string", "comment", "number", "function", "type", "operator")

# Language detection by extension. A file whose extension is not here is still
# highlighted with the generic ruleset — worst case a word is not coloured, which
# is strictly better than refusing to highlight.
_EXT_LANG = {
    ".py": "python", ".pyw": "python", ".pyi": "python",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "js", ".tsx": "js",
    ".json": "json", ".jsonc": "json",
    ".html": "markup", ".htm": "markup", ".xml": "markup", ".svg": "markup",
    ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "shell",
    ".c": "clike", ".h": "clike", ".cpp": "clike", ".hpp": "clike",
    ".cc": "clike", ".cs": "clike", ".java": "clike", ".go": "clike",
    ".rs": "clike", ".swift": "clike", ".kt": "clike", ".php": "clike",
    ".rb": "ruby", ".yml": "yaml", ".yaml": "yaml", ".toml": "yaml",
    ".sql": "sql", ".md": "markdown", ".markdown": "markdown",
}

_PY_KW = (
    "False|None|True|and|as|assert|async|await|break|class|continue|def|del|elif|"
    "else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|"
    "pass|raise|return|try|while|with|yield|match|case|self|cls"
)
_JS_KW = (
    "abstract|any|as|async|await|boolean|break|case|catch|class|const|constructor|"
    "continue|debugger|declare|default|delete|do|else|enum|export|extends|false|"
    "finally|for|from|function|get|if|implements|import|in|instanceof|interface|"
    "let|new|null|number|of|private|protected|public|readonly|return|set|static|"
    "string|super|switch|this|throw|true|try|type|typeof|undefined|var|void|while|"
    "yield"
)
_C_KW = (
    "auto|bool|break|case|catch|char|class|const|constexpr|continue|default|"
    "defer|delete|do|double|else|enum|explicit|extern|false|final|float|fn|for|"
    "func|go|goto|if|impl|import|inline|int|interface|let|long|match|mut|"
    "namespace|new|nil|null|nullptr|override|package|private|protected|public|"
    "pub|return|short|signed|sizeof|static|struct|switch|template|this|throw|"
    "true|try|typedef|typename|union|unsigned|use|using|var|virtual|void|while"
)
_SH_KW = (
    "case|do|done|elif|else|esac|exit|export|fi|for|function|if|in|local|return|"
    "select|set|shift|source|then|unset|until|while"
)
_SQL_KW = (
    "ALTER|AND|AS|ASC|BY|CREATE|DELETE|DESC|DISTINCT|DROP|EXISTS|FROM|GROUP|"
    "HAVING|INDEX|INNER|INSERT|INTO|JOIN|LEFT|LIMIT|NOT|NULL|ON|OR|ORDER|"
    "OUTER|SELECT|SET|TABLE|UNION|UPDATE|VALUES|WHERE"
)

# ⚠️ ORDER IS THE WHOLE DESIGN. Comment and string alternatives come first, so a
# keyword inside either is consumed as part of it and never re-examined.
_LANG_RULES: dict[str, str] = {
    "python": (
        r"(?P<comment>#[^\n]*)"
        r"|(?P<string>[rRbBfFuU]{0,2}(?:\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'))"
        r"|(?P<decorator>@[A-Za-z_][\w.]*)"
        rf"|(?P<keyword>\b(?:{_PY_KW})\b)"
        r"|(?P<function>\b[A-Za-z_]\w*(?=\s*\())"
        r"|(?P<number>\b(?:0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?)\b)"
        r"|(?P<operator>[+\-*/%=<>!&|^~:]+)"
    ),
    "js": (
        r"(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)"
        r"|(?P<string>`(?:\\.|[^`\\])*`|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
        rf"|(?P<keyword>\b(?:{_JS_KW})\b)"
        r"|(?P<function>\b[A-Za-z_$]\w*(?=\s*\())"
        r"|(?P<number>\b(?:0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?)\b)"
        r"|(?P<operator>[+\-*/%=<>!&|^~?:]+)"
    ),
    "clike": (
        r"(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)"
        r"|(?P<string>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
        r"|(?P<meta>^\s*#\s*\w+)"
        rf"|(?P<keyword>\b(?:{_C_KW})\b)"
        r"|(?P<type>\b[A-Z]\w*\b)"
        r"|(?P<function>\b[A-Za-z_]\w*(?=\s*\())"
        r"|(?P<number>\b(?:0[xXbB][0-9a-fA-F_]+|\d[\d_]*\.?[\d_]*[fFlLuU]*)\b)"
        r"|(?P<operator>[+\-*/%=<>!&|^~?:]+)"
    ),
    "shell": (
        r"(?P<comment>#[^\n]*)"
        r"|(?P<string>\"(?:\\.|[^\"\\])*\"|'[^']*')"
        rf"|(?P<keyword>\b(?:{_SH_KW})\b)"
        r"|(?P<type>\$\{?\w+\}?)"
        r"|(?P<operator>[|&;<>()]+|--?\w[\w-]*)"
        r"|(?P<number>\b\d+\b)"
    ),
    "json": (
        r"(?P<type>\"(?:\\.|[^\"\\])*\"(?=\s*:))"
        r"|(?P<string>\"(?:\\.|[^\"\\])*\")"
        r"|(?P<keyword>\b(?:true|false|null)\b)"
        r"|(?P<number>-?\b\d+\.?\d*(?:[eE][+-]?\d+)?\b)"
        r"|(?P<operator>[:,])"
    ),
    "yaml": (
        r"(?P<comment>#[^\n]*)"
        r"|(?P<string>\"(?:\\.|[^\"\\])*\"|'[^']*')"
        r"|(?P<type>^\s*-?\s*[\w.\-]+(?=\s*:))"
        r"|(?P<keyword>\b(?:true|false|null|yes|no|on|off)\b)"
        r"|(?P<number>\b\d+\.?\d*\b)"
    ),
    "css": (
        r"(?P<comment>/\*[\s\S]*?\*/)"
        r"|(?P<string>\"(?:\\.|[^\"\\])*\"|'[^']*')"
        r"|(?P<type>[.#][\w-]+|@[\w-]+|:{1,2}[\w-]+)"
        r"|(?P<keyword>\b[\w-]+(?=\s*:))"
        r"|(?P<number>-?\b\d+\.?\d*(?:px|em|rem|%|vh|vw|s|ms|deg|fr)?\b)"
        r"|(?P<operator>[{}:;,>+~]+)"
    ),
    "markup": (
        r"(?P<comment><!--[\s\S]*?-->)"
        r"|(?P<string>\"(?:\\.|[^\"\\])*\"|'[^']*')"
        r"|(?P<keyword></?[A-Za-z][\w:-]*)"
        r"|(?P<type>\b[\w-]+(?==))"
        r"|(?P<operator>/?>|=)"
    ),
    "ruby": (
        r"(?P<comment>#[^\n]*)"
        r"|(?P<string>\"(?:\\.|[^\"\\])*\"|'[^']*'|:[A-Za-z_]\w*)"
        r"|(?P<keyword>\b(?:def|end|class|module|if|elsif|else|unless|while|until|"
        r"do|then|begin|rescue|ensure|yield|return|require|attr_accessor|nil|true|"
        r"false|self|and|or|not|puts)\b)"
        r"|(?P<function>\b[A-Za-z_]\w*[?!]?(?=\s*\())"
        r"|(?P<number>\b\d[\d_]*\.?\d*\b)"
        r"|(?P<operator>[+\-*/%=<>!&|^~]+)"
    ),
    "sql": (
        r"(?P<comment>--[^\n]*|/\*[\s\S]*?\*/)"
        r"|(?P<string>'(?:''|[^'])*')"
        rf"|(?P<keyword>\b(?i:{_SQL_KW})\b)"
        r"|(?P<function>\b[A-Za-z_]\w*(?=\s*\())"
        r"|(?P<number>\b\d+\.?\d*\b)"
        r"|(?P<operator>[=<>!*,;()]+)"
    ),
    "markdown": (
        r"(?P<keyword>^#{1,6}\s.*$)"
        r"|(?P<string>`[^`]*`|```[\s\S]*?```)"
        r"|(?P<type>\*\*[^*]+\*\*|__[^_]+__)"
        r"|(?P<comment>^\s*>.*$)"
        r"|(?P<operator>^\s*[-*+]\s|\[[^\]]*\]\([^)]*\))"
    ),
    # Generic: no language matched. Strings, numbers and the keywords common to
    # nearly every curly-brace language, which is enough to make a diff readable.
    "generic": (
        r"(?P<comment>#[^\n]*|//[^\n]*)"
        r"|(?P<string>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
        r"|(?P<keyword>\b(?:if|else|for|while|return|function|def|class|const|let|"
        r"var|import|from|export|true|false|null|None|True|False)\b)"
        r"|(?P<number>\b\d[\d_]*\.?\d*\b)"
        r"|(?P<operator>[+\-*/%=<>!&|^~]+)"
    ),
}

# Group names that are not themselves token kinds get folded onto one that is, so
# a renderer only ever sees a member of KINDS.
_ALIAS = {"decorator": "function", "meta": "comment"}

_COMPILED: dict[str, re.Pattern] = {}


def lang_for(path: str) -> str:
    """Language id for *path*, or "generic". Never raises."""
    try:
        name = str(path or "").lower().replace("\\", "/").rsplit("/", 1)[-1]
        if "." not in name:
            # Extension-less files that are still recognisable by name.
            if name in ("dockerfile", "makefile", "rakefile", "gemfile"):
                return "shell" if name in ("dockerfile", "makefile") else "ruby"
            return "generic"
        return _EXT_LANG.get("." + name.rsplit(".", 1)[-1], "generic")
    except Exception:
        return "generic"


def _pattern(lang: str) -> re.Pattern | None:
    """Compiled rules for *lang*, cached. None when the language has no rules."""
    if lang in _COMPILED:
        return _COMPILED[lang]
    src = _LANG_RULES.get(lang)
    if not src:
        return None
    try:
        pat = re.compile(src, re.MULTILINE)
    except re.error:
        # A malformed rule must not take highlighting down for every language.
        pat = None
    if pat is not None:
        _COMPILED[lang] = pat
    return pat


def tokenize(line: str, lang: str = "generic") -> list[tuple[str, str]]:
    """Split one line into [(kind, text), …]. Total: falls back to one plain token.

    The concatenation of every text is EXACTLY the input line — renderers rely on
    that to keep column alignment in side-by-side mode. Callers pass a single
    line; multi-line constructs (a docstring spanning rows) are highlighted per
    line, which is the standard tradeoff for a line-oriented diff.
    """
    if not line:
        return []
    try:
        pat = _pattern(lang) or _pattern("generic")
        if pat is None:
            return [("text", line)]

        out: list[tuple[str, str]] = []
        pos = 0
        for m in pat.finditer(line):
            kind = m.lastgroup or "text"
            kind = _ALIAS.get(kind, kind)
            if kind not in KINDS:
                kind = "text"
            start, end = m.span()
            if start > pos:
                out.append(("text", line[pos:start]))
            out.append((kind, line[start:end]))
            pos = end
        if pos < len(line):
            out.append(("text", line[pos:]))
        return out or [("text", line)]
    except Exception:
        return [("text", line)]


def tokenize_lines(lines: list[str], lang: str = "generic") -> list[list[tuple[str, str]]]:
    """`tokenize` over a list of lines. Never raises."""
    try:
        return [tokenize(ln, lang) for ln in (lines or [])]
    except Exception:
        return [[("text", ln)] for ln in (lines or [])]
