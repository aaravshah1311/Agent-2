# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.skills.normalize
────────────────────────────
THE normalization layer — Task 33. One reader for a skill written for **any**
agent, and the reason Agent2 needs no plugin architecture to accept one.

A skill from Claude Code arrives as `SKILL.md` with `name:` / `description:` in a
`---` fenced header. Codex users write `AGENTS.md`. Antigravity and the Gemini CLI
write `GEMINI.md`, sometimes with `title:` instead of `name:`, sometimes with
`whenToUse:` instead of `keywords:`, often with no header at all. Three ecosystems,
one idea: *a markdown file whose body is an instruction and whose header says when
it applies.*

⚠️ **THERE IS NO PER-VENDOR CODE PATH IN THIS MODULE, AND THAT IS THE TASK.**
The user's constraint was explicit — *do not build a universal plugin
architecture; create a skill loading/normalization layer* — so vendor support is
two **tables** and nothing else:

  * `ORIGINS` — the manifest filenames, in precedence order, each labelled with
    the ecosystem it came from;
  * `ALIASES` — every field spelling any of them uses, collapsed onto one
    canonical name.

`origin` is a **label for reporting only** — `/skills` prints it and
`GET /api/skills` returns it. Nothing branches on it, and a test pins that by
asserting the `ORIGIN_*` names appear nowhere else in the package. The moment a
fourth ecosystem needs `if origin == …` to work, this module has become the plugin
system it was written to avoid; the correct change is a row in a table.

⚠️ **NOTHING HERE WRITES.** Not a temp file, not a normalized copy, not a cache on
disk. The user's second constraint — *do not modify external skill files* — is
enforced structurally: `discovery.py` and this module contain no write call at
all, which is asserted by `test_skills.py` at the source level and again by
hashing a real skills tree either side of a full discover → select → toggle cycle.
A skill is someone else's file that Agent2 happens to be able to read.

⚠️ **STDLIB ONLY — PyYAML IS NOT A DEPENDENCY OF THIS PROJECT.** So the header
parser here is deliberately small: `key: value`, quoted or bare, block lists
(`- item`) and inline lists (`[a, b]`), `true`/`false`/numbers. That is the subset
every one of those ecosystems actually uses in a skill header. Anything richer
(nested maps, anchors, multi-line block scalars) is **counted, not guessed** — it
lands in `unparsed` and the body is still read, because a header this module cannot
fully understand is not a reason to discard a skill a human wrote. Importing yaml
"if available" would be worse than either: the same file would then normalize
differently on two machines, which is the class of drift this codebase is shaped
against.
"""

import re

# ── Manifest table: which filenames are a skill, and what wrote them ──────────
# ⚠️ ORDER IS PRECEDENCE. A directory holding both `SKILL.md` and `AGENTS.md` is
# one skill, read from the first row that matches — never two skills, and never a
# merge of the two, because a merge would invent instructions nobody wrote.
#
# Matching is case-insensitive: `SKILL.md`, `Skill.md` and `skill.md` are the same
# file on Windows and macOS, so treating them as different rows would make
# discovery depend on the filesystem.
ORIGIN_CLAUDE = "claude"
ORIGIN_CODEX = "codex"
ORIGIN_ANTIGRAVITY = "antigravity"
ORIGIN_AGENT2 = "agent2"
ORIGIN_PLAIN = "plain"

ORIGINS: tuple[tuple[str, str], ...] = (
    ("skill.md", ORIGIN_CLAUDE),          # Claude Code / Agent Skills
    ("skill.yaml", ORIGIN_CLAUDE),
    ("skill.yml", ORIGIN_CLAUDE),
    ("agents.md", ORIGIN_CODEX),          # Codex / OpenAI convention
    ("agent.md", ORIGIN_CODEX),
    ("gemini.md", ORIGIN_ANTIGRAVITY),    # Antigravity / Gemini CLI
    ("antigravity.md", ORIGIN_ANTIGRAVITY),
    ("agent2.md", ORIGIN_AGENT2),         # written here, by a human or by `/init`
    ("instructions.md", ORIGIN_PLAIN),
    ("prompt.md", ORIGIN_PLAIN),
)

# The lookup order `discovery.py` walks. Derived, so the two facts cannot drift.
MANIFESTS: tuple[str, ...] = tuple(name for name, _ in ORIGINS)
_ORIGIN_BY_FILE: dict[str, str] = dict(ORIGINS)

# ⚠️ The folder's own README is NOT a skill. `projectdoc.apply()` seeds
# `.agent2/skills/README.md` to explain the directory, so admitting it would give
# every project one phantom skill named after its documentation — and it would say
# "drop one directory per skill here" to the model, forever.
NOT_A_SKILL = frozenset({"readme.md", "readme", "license.md", "license",
                         "changelog.md", "notes.md", "todo.md"})


# ── Field table: one canonical name per fact, every spelling that reaches it ───
# ⚠️ ONE TABLE, and the squash below is what makes it total: keys are compared
# with everything but `[a-z0-9]` removed, so `alwaysApply`, `always_apply`,
# `always-apply` and `AlwaysApply` are one row rather than four. Adding the four
# spellings by hand is how a table like this grows a hole — the fifth spelling
# somebody uses is the one that silently reads as an unknown field.
ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "title", "skill", "skill_name", "display_name"),
    "description": ("description", "desc", "summary", "about", "purpose", "intent"),
    # When does this apply? Claude says `description`, Codex tends to list `tags`,
    # Antigravity writes `whenToUse`. All of them mean "match my request against
    # these words".
    "keywords": ("keywords", "tags", "triggers", "trigger", "when_to_use",
                 "use_when", "applies_to", "activation", "topics", "match"),
    "always": ("always", "always_apply", "global", "pinned", "sticky"),
    "priority": ("priority", "order", "rank", "weight"),
    "version": ("version", "schema_version", "skill_version"),
    "tools": ("allowed_tools", "tools", "allowed", "permissions"),
    "model": ("model", "models"),
    "license": ("license",),
    "author": ("author", "owner", "maintainer"),
}

CANONICAL: tuple[str, ...] = tuple(ALIASES)
_SQUASH_RE = re.compile(r"[^a-z0-9]+")


def squash(key: str) -> str:
    """`whenToUse` · `when_to_use` · `WHEN-TO-USE` → `whentouse`.

    The one place a header key is folded. ⚠️ Case AND separators, together: a fold
    that only lowercased would accept `always_apply` and miss `alwaysApply`, which
    is the exact spelling Antigravity writes.
    """
    return _SQUASH_RE.sub("", str(key or "").strip().lower())


# Reverse index, built once: squashed spelling → canonical field.
_FIELD_BY_KEY: dict[str, str] = {}
for _canon, _spellings in ALIASES.items():
    _FIELD_BY_KEY[squash(_canon)] = _canon
    for _sp in _spellings:
        _FIELD_BY_KEY[squash(_sp)] = _canon


def origin_for(manifest_name: str) -> str:
    """The ecosystem label for a manifest filename — **reporting only**.

    ⚠️ Never a dispatch key. It exists so a user can see *why* Agent2 read a file
    they wrote for another agent, and so a bug report can say "the Codex-shaped one
    was skipped". A branch on this value is the per-vendor plugin Task 33 forbids.
    """
    return _ORIGIN_BY_FILE.get(str(manifest_name or "").strip().lower(), ORIGIN_PLAIN)


# ── The header parser (stdlib, deliberately small) ────────────────────────────

_FENCE_RE = re.compile(r"^(-{3,}|\+{3,})\s*$")
_KEY_RE = re.compile(r"^([A-Za-z0-9_.\-]{1,64})\s*:\s*(.*)$")
_ITEM_RE = re.compile(r"^[-*]\s+(.*)$")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$")


def _unquote(raw: str) -> str:
    s = str(raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1].strip()
    return s


def _scalar(raw: str):
    """`true` → True, `12` → 12, `[a, b]` → ["a","b"], anything else → str."""
    s = _unquote(raw)
    if not s:
        return ""
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~"):
        return ""
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_unquote(part) for part in inner.split(",") if _unquote(part)]
    if re.fullmatch(r"-?\d{1,9}", s):
        return int(s)
    return s


def split_frontmatter(text: str) -> tuple[dict, str, int]:
    """`(header, body, unparsed)` — the header dict, the rest, and what was skipped.

    ⚠️ **A FILE WITH NO HEADER IS NOT AN ERROR.** It is the common case for a
    hand-written `AGENTS.md`, and it returns `({}, text, 0)` so the whole file is
    the instruction. `normalize()` then derives a name from the path and a
    description from the first heading — a skill with no metadata still works,
    which is what makes "read them as-is" true rather than aspirational.

    `unparsed` counts header lines this parser understood as *something it does not
    support* (nested maps, block scalars). It is reported, never guessed at: a
    header we half-read must not read as a header we fully read.
    """
    src = str(text or "")
    if src.startswith("﻿"):
        src = src[1:]
    lines = src.splitlines()
    # The fence must be the first non-empty line; a `---` further down is a
    # horizontal rule in somebody's markdown, not a header.
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or not _FENCE_RE.match(lines[start].strip()):
        return {}, src, 0

    header: dict = {}
    unparsed = 0
    key: str | None = None
    end = -1
    for i in range(start + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if _FENCE_RE.match(stripped) or stripped == "...":
            end = i
            break
        if not stripped or stripped.startswith("#"):
            continue
        item = _ITEM_RE.match(stripped)
        if item and key:
            cur = header.get(key)
            val = _unquote(item.group(1))
            if isinstance(cur, list):
                cur.append(val)
            elif cur in ("", None):
                header[key] = [val]
            else:
                header[key] = [cur, val]
            continue
        m = _KEY_RE.match(stripped)
        if not m:
            unparsed += 1
            continue
        key = m.group(1)
        rest = m.group(2).strip()
        if rest in ("|", ">", "|-", ">-", "{", "["):
            # A block scalar or an opening map. Counted, not invented.
            unparsed += 1
            header[key] = ""
            continue
        header[key] = _scalar(rest) if rest else ""

    if end < 0:
        # An opening fence with no closing one: the file is markdown that happens
        # to start with a rule. Treat the whole thing as body — dropping it would
        # lose the instruction over a punctuation guess.
        return {}, src, 0
    body = "\n".join(lines[end + 1:]).strip("\n")
    return header, body, unparsed


def _first_sentence(body: str, limit: int = 240) -> str:
    """The first heading or first prose line of a body, as a description."""
    for line in str(body or "").splitlines():
        s = line.strip()
        if not s or _FENCE_RE.match(s):
            continue
        h = _HEADING_RE.match(s)
        if h:
            s = h.group(1).strip()
        if not s:
            continue
        s = re.sub(r"[*_`]+", "", s).strip()
        if s:
            return s[:limit]
    return ""


def _as_list(value) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value]
    elif isinstance(value, str):
        parts = re.split(r"[,;/|]+", value)
    elif value in (None, ""):
        parts = []
    else:
        parts = [str(value)]
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        word = p.strip().strip("#").lower()
        if word and word not in seen:
            seen.add(word)
            out.append(word)
    return tuple(out)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "always")


def _as_int(value, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"\s*(-?\d{1,9})\s*", str(value or ""))
    return int(m.group(1)) if m else default


def normalize(text: str, *, manifest: str = "", fallback_name: str = "") -> dict:
    """One skill file → the canonical record every later step reads.

    Returns `header` (canonical fields), `extra` (recognised-but-unmapped keys),
    `body`, `origin` and `unparsed`. ⚠️ **Total by contract:** an empty file, a
    binary blob decoded with replacements, a header of nothing but junk — each
    yields a record with an empty body rather than an exception. Discovery calls
    this inside its own guard as well, but a normalizer that can raise makes every
    caller responsible for a file *it* chose to read.

    ⚠️ `fallback_name` is the skill's **path**-derived name, and it is what a
    missing `name:` falls back to. Deriving the display name from the header alone
    would leave a headerless `AGENTS.md` nameless, and a nameless skill cannot be
    enabled, reported or asked for by name.
    """
    header_raw, body, unparsed = split_frontmatter(text)
    fields: dict = {}
    extra: dict = {}
    for raw_key, raw_val in header_raw.items():
        canon = _FIELD_BY_KEY.get(squash(raw_key))
        if canon is None:
            extra[str(raw_key)] = raw_val
            continue
        # First spelling wins: a file with both `name:` and `title:` keeps the one
        # it wrote first, rather than whichever the dict happened to iterate last.
        if canon not in fields:
            fields[canon] = raw_val

    name = str(fields.get("name") or "").strip() or str(fallback_name or "").strip()
    desc = str(fields.get("description") or "").strip() or _first_sentence(body)
    return {
        "name": name[:120],
        "description": desc[:400],
        "keywords": _as_list(fields.get("keywords")),
        "always": _as_bool(fields.get("always")),
        "priority": _as_int(fields.get("priority")),
        "version": str(fields.get("version") or "").strip()[:32],
        "tools": _as_list(fields.get("tools")),
        "model": str(fields.get("model") or "").strip()[:64],
        "author": str(fields.get("author") or "").strip()[:80],
        "body": body.strip(),
        "origin": origin_for(manifest),
        "unparsed": unparsed,
        "extra": extra,
        "had_header": bool(header_raw),
    }


def describe() -> dict:
    """What this layer accepts — for `/skills` and `GET /api/skills`.

    Reported rather than documented-only, because "which filenames does it read"
    is the first question when a skill somebody wrote is not showing up, and a
    README can go stale while a derived payload cannot.
    """
    return {
        "manifests": list(MANIFESTS),
        "origins": sorted({o for _, o in ORIGINS}),
        "fields": list(CANONICAL),
        "aliases": {k: list(v) for k, v in ALIASES.items()},
        "ignored": sorted(NOT_A_SKILL),
        "yaml": False,
    }
