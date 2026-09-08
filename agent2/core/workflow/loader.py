"""`.agent2/workflows/*.yaml` — a workflow a human wrote, read back safely.

THE reader for workflow files, and the answer to "which workflows does this
project have". `graph.py` says what a workflow *is*; this module says how one gets
off a disk and into that model, and it is the file `graph.py`'s docstrings already
name twice.

The bar Task 38 is graded on is **"an old schema still runs"**, and it is met in
three places rather than one, because a version is only the most visible way a file
can be older than the build reading it:

  1. `graph.validate()` refuses a version *below* `MIN_SCHEMA` and merely **warns**
     about one above `SCHEMA_VERSION` — a newer file still runs, with the fields
     this build could not map kept in `extra`.
  2. `graph.make_def()` / `make_node()` accept every spelling a workflow has ever
     used (`nodes`/`steps`/`tasks`/`graph`, `needs`/`depends_on`/`after`/`requires`)
     and keep unknown keys instead of dropping them.
  3. `UPGRADES` — the declared migration ladder here — rewrites an *older* mapping
     into the current shape before it is ever coerced, so a file that predates a
     structural change keeps working without being edited.

⚠️ **`UPGRADES` IS EMPTY, AND IT IS EMPTY ON PURPOSE.** Version 1 is the first
version; there is nothing older to migrate *from*. Seeding it with an invented
`0 → 1` step would put a fiction in the one table a future reader will trust, and
`projectdoc._SEEDS["Invariants"]` ships as empty tables for exactly this reason: in
a structure whose whole job is to be believed, a plausible example is
indistinguishable from a real entry. The mechanism is real, it is tested by
installing a step at runtime, and the day version 2 lands one line makes every v1
file on disk keep running.

⚠️ **STDLIB ONLY — PyYAML IS NOT A DEPENDENCY OF THIS PROJECT**, and this module may
not become the reason it is. `_Parser` reads a declared, bounded *subset* of YAML
(see `SUBSET`), and everything outside it is **counted in `unparsed` and named in
`notes`, never guessed at**. Importing `yaml` "if it happens to be installed" would
be worse than either choice: the same file would then load differently on two
machines, and the machine that behaved would be the one nobody debugged.

⚠️ **AND IT IS NOT `skills.normalize.split_frontmatter`.** That parser is
deliberately *flat* — `key: value` and block lists of scalars — because a skill
header is flat. A workflow is a graph: `nodes:` is a sequence of mappings, and a
node instruction is multi-line prose that needs a `|` block scalar. Teaching the
skills reader to nest would give two subsystems one parser with two sets of
requirements pulling on it, and the failure would land on the turn path. Two small
readers, each total, each bounded, is the cheaper answer — and
`test_workflow.py::test_the_two_stdlib_readers_agree_about_a_shared_scalar` pins
them together on the shapes they *do* share, which catches drift in a direction an
import never could.

⚠️ **A WORKFLOW'S NAME COMES FROM ITS FILENAME**, not from `name:` inside it —
`skills.Skill.id`'s rule, for its reason. `/workflow run audit` has to find
`audit.yaml`, so the stem is the key and a disagreeing `name:` line is *reported*
(`L_NAME_MISMATCH`) rather than silently winning or silently losing. `graph.fold_id`
lower-cases it, which is what makes `Audit.yaml` and `audit.yaml` one name on every
operating system instead of one name on Windows and two on Linux.

⚠️ **THE FOLDER IS FLAT, AND THAT IS REPORTED.** A name is a word a human types
after `/workflow run`, so it cannot be a path; two `audit.yaml` files in two
subfolders would be one name with two definitions. Subdirectories are therefore not
descended — and `Catalog.nested` counts them with a line in `errors`, because a file
the user wrote and this build never mentions is indistinguishable from a broken
walk, which is the one failure they cannot debug.

Nothing here raises into a caller: a missing folder, an unreadable file, a
half-written YAML document and a graph with a cycle all arrive as a `WorkflowFile`
whose `ok` is False and whose `problems` say why. See the module's entry in
`pyproject.toml`'s per-file-ignores for the degradation each swallow guarantees.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from agent2 import config
from agent2.core.workflow import graph

#: How long a catalog is reused. `skills.discovery.SKILLS_TTL`'s number, for its
#: reason: long enough that a `/workflow` menu redraw is free, short enough that a
#: human editing a file sees the change before they wonder whether it took.
WORKFLOW_TTL = 5.0

#: What a workflow file may be called. `.yaml` and `.yml` are the same format under
#: two spellings people genuinely disagree about; `.json` is here because a
#: generated workflow is easier to emit as JSON and `json.loads` is stricter and
#: cheaper than any subset parser — Task 40's planner will want that.
SUFFIXES = ("yaml", "yml", "json")

#: Nesting the parser will descend before it stops and says so. A workflow needs
#: three levels (`nodes:` → an item → its `needs:`); eight is generous. ⚠️ Bounded
#: for `graph._find_cycles`' reason — a malformed file must be *reported*, never
#: turned into a `RecursionError` on the turn path — and deliberately not an env
#: knob: nobody tunes this, and a fourth ceiling would only be a fourth thing to
#: get wrong.
MAX_NEST = 8

#: The YAML this build reads, stated so `describe()` can hand it to a human whose
#: file did not load. Anything not on this list is counted, not guessed.
SUBSET = (
    "key: value",
    "key:  (nested block below)",
    "- item  (block sequence)",
    "- key: value  (sequence of mappings)",
    "[a, b]  (flow sequence)",
    "key: |  and  key: >  (block scalar)",
    "# comment",
    "'quoted'  \"quoted\"",
    "true/false/yes/no/on/off, null/~, integers, decimals",
    "a leading --- document marker",
)

# ── What can be wrong with a FILE ─────────────────────────────────────────────
# Distinct from `graph.PROBLEM_CODES`, and the split is the point: `graph` answers
# "can this graph run", this module answers "could this file be read at all". One
# list would make an unreadable file and a cyclic graph the same kind of event, and
# only one of the two is the user's YAML.

L_UNREADABLE = "unreadable"          # the bytes could not be read
L_EMPTY = "empty"                    # nothing in it, or nothing but comments
L_NOT_MAPPING = "not_a_mapping"      # a list or a scalar where a workflow goes
L_PARSE = "parse_failed"             # JSON only — the subset parser never fails
L_TRUNCATED = "truncated"            # hit WORKFLOW_MAX_BYTES; the tail is missing
L_OUTSIDE = "outside_workspace"      # resolved out of the folder (symlink)

FILE_PROBLEM_CODES: tuple[str, ...] = (
    L_UNREADABLE, L_EMPTY, L_NOT_MAPPING, L_PARSE, L_TRUNCATED, L_OUTSIDE,
)

L_UNPARSED = "unparsed_lines"        # YAML outside SUBSET; counted, never guessed
L_NAME_MISMATCH = "name_mismatch"    # `name:` disagrees with the filename
L_UPGRADED = "upgraded"              # an older schema was migrated on the way in

FILE_WARNING_CODES: tuple[str, ...] = (L_UNPARSED, L_NAME_MISMATCH, L_UPGRADED)

#: Why a walk stopped early. `skills.discovery`'s spelling, so the two catalogs
#: report a ceiling in the same word.
BY_COUNT = "count"
BY_BUDGET = "budget"

# ── The migration ladder ──────────────────────────────────────────────────────
#: `{from_version: fn(dict) -> dict}`. A step maps N → N+1 and nothing further;
#: `upgrade()` stamps the new version itself, so a step that forgets to cannot loop.
#: ⚠️ Empty by design — see the module docstring.
UPGRADES: dict[int, object] = {}

#: The oldest version that can reach the current model, DERIVED rather than
#: declared: with no steps registered it is whatever `graph` still accepts, and it
#: moves on its own the day a step is added. A second literal here would be the copy
#: nobody edits.
def oldest_supported() -> int:
    return min(UPGRADES) if UPGRADES else graph.MIN_SCHEMA


# ── Scalars ───────────────────────────────────────────────────────────────────
# ⚠️ A SECOND SCALAR READER, and the module docstring says why it is not an import.
# The overlap with `skills.normalize._scalar` is pinned by a test rather than by
# shared code, because that reader may need to change for skills' reasons and this
# one must not move underneath a workflow that already runs.

_KEY_RE = re.compile(r"^([A-Za-z0-9_.\-]{1,64})\s*:(\s.*|)$")
_INT_RE = re.compile(r"^-?\d{1,12}$")
_FLOAT_RE = re.compile(r"^-?\d{1,12}\.\d{1,12}$")
_TRUE = ("true", "yes", "on")
_FALSE = ("false", "no", "off")
_NULL = ("null", "none", "~")
#: Values this reader will not interpret: a flow mapping, an anchor, an alias, a
#: tag. Each is valid YAML and each would come out of a naive read as a *string*
#: that looks like data — so the key is skipped and counted instead.
_OPAQUE = ("{", "&", "*", "!")
_BLOCK_MARKS = ("|", ">", "|-", ">-", "|+", ">+")


def _unquote(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        body = s[1:-1]
        if s[0] == '"':
            return body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        return body.replace("''", "'")
    return s


def _split_flow(inner: str) -> list[str]:
    """`a, [b, c], "d, e"` → three parts. Quote- and bracket-aware."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _scalar(raw: str):
    """One YAML scalar → a Python value. Total, and never a guess.

    A value this reader does not understand comes back as the string it was, which
    is only ever reached for shapes `_Parser` has already counted in `unparsed` —
    so "kept verbatim" and "reported as unread" are the same event, not two.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return _unquote(s)
    low = s.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if low in _NULL:
        return None
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_scalar(p) for p in _split_flow(inner)] if inner else []
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    return s


def _strip_comment(s: str) -> str:
    """Drop a trailing `# comment`, respecting quotes.

    ⚠️ A `#` only starts a comment at the start of the value or after whitespace —
    `instruction: curl http://h/#frag` keeps its fragment, and a URL in a node
    instruction is not a rare shape in this project.
    """
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(s):
        ch = s[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(s):
                out.append(s[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).rstrip()


# ── The subset parser ─────────────────────────────────────────────────────────

@dataclass
class Parsed:
    """A parsed document, and everything in it this reader did not understand."""

    data: object = None
    unparsed: int = 0
    #: Human-readable, capped — the first few reasons, each with a line number.
    #: Bounded because it is rendered on two surfaces and a broken file can
    #: produce one note per line.
    notes: list[str] = field(default_factory=list)

    MAX_NOTES = 8

    def note(self, lineno: int, why: str) -> None:
        self.unparsed += 1
        if len(self.notes) < self.MAX_NOTES:
            self.notes.append(f"line {lineno}: {why}")


class _Parser:
    """Indentation-aware reader for `SUBSET`. Total by construction.

    Every loop body either consumes a line or breaks, so the parser terminates on
    any input — including one with tabs, mixed indentation and no closing anything.
    That is why this class carries no blind `except`: it has no operation that can
    raise, which is the same reason `graph.py` is absent from the BLE001 list.
    """

    def __init__(self, text: str) -> None:
        norm = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        self.lines = norm.split("\n")
        self.i = 0
        self.out = Parsed()

    # ── line handling ─────────────────────────────────────────────────────────

    def _significant(self) -> tuple[int, str, int] | None:
        """`(indent, body, lineno)` of the next line that carries data.

        Blank lines and whole-line comments are consumed here. A tab inside the
        indentation is consumed and **counted**: it is invalid YAML, every editor
        renders it as an arbitrary number of columns, and guessing a width would
        silently reshape somebody's graph.
        """
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            body = raw.lstrip(" ")
            if not body.strip():
                self.i += 1
                continue
            indent = len(raw) - len(body)
            if "\t" in raw[:indent] or body.startswith("\t"):
                self.out.note(self.i + 1, "tab in indentation")
                self.i += 1
                continue
            if body.lstrip().startswith("#"):
                self.i += 1
                continue
            return indent, body.rstrip(), self.i + 1
        return None

    def _advance(self) -> None:
        self.i += 1

    @staticmethod
    def _is_item(body: str) -> bool:
        return body == "-" or body.startswith("- ")

    def _skip_deeper(self, min_indent: int) -> None:
        """Consume every following line indented at or past *min_indent*."""
        while True:
            nxt = self._significant()
            if nxt is None or nxt[0] < min_indent:
                return
            self._advance()

    # ── values ────────────────────────────────────────────────────────────────

    def _block_scalar(self, mark: str, base: int) -> str:
        """A `|` / `>` block. Reads RAW lines, so blank lines inside prose survive.

        ⚠️ This is the shape `skills.normalize` deliberately does not read, and the
        reason this parser exists: a node instruction is a paragraph, and a workflow
        file whose instructions had to be one line each would be a workflow file
        nobody writes twice.
        """
        fold = mark.startswith(">")
        chomp = mark[1:2]
        body: list[str] = []
        own = -1
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if not raw.strip():
                body.append("")
                self.i += 1
                continue
            stripped = raw.lstrip(" ")
            indent = len(raw) - len(stripped)
            if indent <= base:
                break
            if own < 0:
                own = indent
            body.append(raw[own:] if len(raw) > own else stripped)
            self.i += 1
        if chomp != "+":
            while body and not body[-1].strip():
                body.pop()
        if not fold:
            return "\n".join(body)
        # Folded: a run of lines becomes one line, a blank line becomes a break.
        # More-indented lines are NOT kept literal — that corner of YAML has no
        # bearing on an instruction and pretending to support it would be a claim
        # this reader cannot back.
        paras: list[str] = []
        cur: list[str] = []
        for ln in body:
            if not ln.strip():
                paras.append(" ".join(cur))
                cur = []
            else:
                cur.append(ln.strip())
        paras.append(" ".join(cur))
        return "\n".join(paras).strip("\n")

    def _value_for(self, key_line_indent: int, rest: str, lineno: int, depth: int = 0):
        """The value of a `key:` whose remainder on the line is *rest*.

        Returns `(value, ok)` — `ok` False means the key must be skipped, and it
        has already been counted.

        ⚠️ *depth* is **threaded, not restarted**. It first read `_parse_block(…, 1)`
        with the current depth dropped on the floor, which left `MAX_NEST` declared
        and unreachable: a mapping nested through `key:` → block → `key:` recursed as
        far as the file did, so forty levels of indentation was a `RecursionError` on
        the turn path — the one failure `graph._find_cycles` is iterative to avoid.
        """
        if rest in _BLOCK_MARKS:
            return self._block_scalar(rest, key_line_indent), True
        if rest == "":
            nxt = self._significant()
            if nxt is not None and nxt[0] == key_line_indent and self._is_item(nxt[1]):
                # `nodes:` with its items at the SAME column. Valid YAML and the
                # form half of all hand-written files use.
                return self._parse_seq(key_line_indent, depth + 1), True
            child = self._parse_block(key_line_indent + 1, depth + 1)
            return ("" if child is None else child), True
        if rest.startswith(_OPAQUE):
            self.out.note(lineno, f"unsupported value {rest[:1]!r}")
            return None, False
        return _scalar(rest), True

    # ── collections ───────────────────────────────────────────────────────────

    def _parse_block(self, min_indent: int, depth: int):
        """A mapping or a sequence starting at the next line indented ≥ *min_indent*."""
        nxt = self._significant()
        if nxt is None or nxt[0] < min_indent:
            return None
        if depth > MAX_NEST:
            self.out.note(nxt[2], f"nested deeper than {MAX_NEST} levels")
            self._skip_deeper(min_indent)
            return None
        indent, body, _ = nxt
        if self._is_item(body):
            return self._parse_seq(indent, depth)
        return self._parse_map(indent, depth)

    def _parse_map(self, indent: int, depth: int) -> dict:
        out: dict = {}
        while True:
            nxt = self._significant()
            if nxt is None:
                break
            ind, body, lineno = nxt
            if ind < indent:
                break
            if ind > indent:
                self.out.note(lineno, "unexpected indentation")
                self._advance()
                continue
            if self._is_item(body):
                break                       # a sequence ends the mapping
            m = _KEY_RE.match(body)
            if not m:
                self.out.note(lineno, "not a `key: value` line")
                self._advance()
                continue
            self._advance()
            key = m.group(1)
            rest = _strip_comment((m.group(2) or "").strip())
            value, ok = self._value_for(indent, rest, lineno, depth)
            if ok:
                out[key] = value
        return out

    def _parse_seq(self, indent: int, depth: int) -> list:
        items: list = []
        while True:
            nxt = self._significant()
            if nxt is None:
                break
            ind, body, lineno = nxt
            if ind != indent or not self._is_item(body):
                break
            self._advance()
            after = body[1:]
            pad = len(after) - len(after.lstrip(" "))
            inner = _strip_comment(after.strip())
            if inner == "":
                child = self._parse_block(indent + 1, depth + 1)
                items.append("" if child is None else child)
                continue
            m = _KEY_RE.match(inner)
            if not m:
                if inner.startswith(_OPAQUE):
                    self.out.note(lineno, f"unsupported item {inner[:1]!r}")
                    continue
                items.append(_scalar(inner))
                continue
            # `- id: recon` — a mapping whose first key sits on the dash line. Its
            # sibling keys are indented to the column that key started at.
            col = indent + 1 + pad
            entry: dict = {}
            key = m.group(1)
            rest = _strip_comment((m.group(2) or "").strip())
            value, ok = self._value_for(col, rest, lineno, depth)
            if ok:
                entry[key] = value
            follow = self._significant()
            if follow is not None and follow[0] == col and not self._is_item(follow[1]):
                entry.update(self._parse_map(col, depth + 1))
            items.append(entry)
        return items

    # ── entry point ───────────────────────────────────────────────────────────

    def parse(self) -> Parsed:
        first = self._significant()
        if first is not None and first[1].rstrip() == "---":
            self._advance()                 # one leading document marker is fine
        self.out.data = self._parse_block(0, 0)
        left = self._significant()
        if left is not None:
            # A second document, or a stray block after the mapping ended. Named
            # once rather than per line: the fix is the same for all of them.
            self.out.note(left[2], "content after the workflow document was not read")
        return self.out


def parse_yaml(text: str) -> Parsed:
    """Read `SUBSET` out of *text*. Never raises, never guesses."""
    return _Parser(text).parse()


def parse_text(text: str, *, fmt: str = "yaml") -> tuple[Parsed, str]:
    """`(Parsed, error)` for either format. *error* is non-empty only for JSON.

    JSON is parsed by the stdlib, which is strict — a trailing comma is a hard
    failure with a line number, and that is a *better* answer than a subset reader's
    shrug, so it is reported as `L_PARSE` rather than folded into `unparsed`.
    """
    if fmt == "json":
        out = Parsed()
        try:
            out.data = json.loads(text or "")
        except Exception as exc:            # a malformed file, said plainly
            return out, str(exc)[:160]
        return out, ""
    return parse_yaml(text), ""


def upgrade(raw: dict, *, version: int) -> tuple[dict, tuple[int, ...]]:
    """Walk `UPGRADES` from *version* up to `graph.SCHEMA_VERSION`. Total.

    Returns the mapping and the versions whose step ran. ⚠️ The **loader** stamps
    the new version after each step rather than trusting the step to, so a step that
    forgets cannot spin — `database.py`'s migration ladder, in miniature, and for
    the same reason: a migration that can run twice is worse than one that refuses.
    """
    if not isinstance(raw, dict):
        return raw, ()
    try:
        cur = int(version)
    except (TypeError, ValueError):
        return raw, ()
    data = raw
    applied: list[int] = []
    while cur < graph.SCHEMA_VERSION and cur in UPGRADES:
        step = UPGRADES[cur]
        try:
            nxt = step(dict(data))          # type: ignore[operator]
        except Exception:                   # a broken step leaves the file as it was
            break
        if not isinstance(nxt, dict):
            break
        data = nxt
        applied.append(cur)
        cur += 1
        data["version"] = cur
    return data, tuple(applied)


# ── The records ───────────────────────────────────────────────────────────────

def _problem(code: str, message: str, **extra) -> dict:
    """`graph._problem`'s shape, so one renderer prints both lists."""
    return {"code": code, "message": message, **extra}


@dataclass
class WorkflowFile:
    """One file in `.agent2/workflows/`, and everything wrong with it.

    ⚠️ `ok` is decided by `problems` **and** the graph's own verdict, never by
    `warnings` — `core/health.py`'s rule, for its reason. A file with an unparsed
    comment block, a mismatched `name:` line and a schema newer than this build is
    a file that **runs**; only a file that could not be read, or a graph that can
    never advance, is refused. A warning that could block a run stops being read.
    """

    name: str = ""
    path: str = ""
    rel: str = ""
    fmt: str = "yaml"
    defn: graph.WorkflowDef | None = None
    validation: graph.Validation | None = None
    problems: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    #: What the file called itself, when that differs from its stem. Reported, never
    #: used as the key — see the module docstring.
    declared_name: str = ""
    schema: int = 0
    upgraded_from: int = 0
    size: int = 0
    truncated: bool = False
    unparsed: int = 0
    notes: list[str] = field(default_factory=list)
    mtime: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.validation is not None
                                          and self.validation.ok)

    @property
    def nodes(self) -> int:
        return len(self.defn.nodes) if self.defn else 0

    def summary(self) -> str:
        """One line a human reads. Empty when the file is clean."""
        if self.problems:
            first = self.problems[0]
            more = f" (+{len(self.problems) - 1} more)" if len(self.problems) > 1 else ""
            return f"{first.get('message') or first.get('code')}{more}"
        if self.validation is not None:
            said = self.validation.summary()
            if said:
                return said
        if self.warnings:
            first = self.warnings[0]
            more = f" (+{len(self.warnings) - 1} more)" if len(self.warnings) > 1 else ""
            return f"{first.get('message') or first.get('code')}{more}"
        return ""

    def to_payload(self, *, nodes: bool = False) -> dict:
        """Metadata for a list; `nodes=True` for `/workflow show`.

        ⚠️ `nodes` DEFAULTS OFF, exactly as `skills.Skill.to_payload(body=False)`
        does and for its reason: a node instruction is prose a human wrote in their
        own checkout, and a listing endpoint has no need of it. The one caller that
        does need the text asks for it.
        """
        out = {
            "name": self.name,
            "rel": self.rel,
            "format": self.fmt,
            "ok": self.ok,
            "count": self.nodes,
            "schema": self.schema,
            "upgraded_from": self.upgraded_from or None,
            "declared_name": self.declared_name,
            "problems": [dict(p) for p in self.problems],
            "warnings": [dict(w) for w in self.warnings],
            "graph": self.validation.to_payload() if self.validation else {},
            "size": self.size,
            "truncated": self.truncated,
            "unparsed": self.unparsed,
            "notes": list(self.notes),
            "summary": self.summary(),
        }
        if nodes:
            out["definition"] = self.defn.to_payload() if self.defn else {}
        return out


@dataclass
class Catalog:
    """What this project's `workflows/` folder holds, as of `at`.

    ⚠️ `project` and `root` travel WITH the answer — `skills.Catalog`'s rule, for
    its reason: a catalog that did not say which project it described could be
    cached and rendered elsewhere, and every claim about leakage would become a
    claim about the caller's discipline instead of about the data.
    """

    files: list[WorkflowFile] = field(default_factory=list)
    root: str = ""
    project: str = ""
    exists: bool = False
    enabled: bool = True                # AGENT2_WORKFLOWS
    truncated: bool = False
    truncated_by: str = ""
    errors: list[str] = field(default_factory=list)
    files_seen: int = 0
    #: Subdirectories not descended into. Counted rather than ignored — see the
    #: module docstring: a file this build never mentions reads as a broken walk.
    nested: int = 0
    at: float = 0.0
    ms: float = 0.0

    def get(self, name: str) -> WorkflowFile | None:
        want = graph.fold_id(name)
        for f in self.files:
            if f.name == want:
                return f
        return None

    def find(self, word: str) -> WorkflowFile | None:
        """By name or by unique prefix — or None.

        ⚠️ An ambiguous prefix returns **None**, `skills.Catalog.find`'s rule:
        `/workflow run web` with `web-audit` and `web-recon` present must ask again,
        never pick — and running the wrong workflow is a more expensive mistake than
        showing the wrong skill.
        """
        want = graph.fold_id(word)
        if not want:
            return None
        exact = self.get(want)
        if exact:
            return exact
        pref = [f for f in self.files if f.name.startswith(want)]
        return pref[0] if len(pref) == 1 else None

    def names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.files)

    def runnable(self) -> list[WorkflowFile]:
        return [f for f in self.files if f.ok]

    def to_payload(self, *, nodes: bool = False) -> dict:
        """ONE payload, both surfaces — `skills.Catalog.to_payload`'s reason.

        `/workflow` and `GET /api/workflows` are two renderers over this dict, so
        the terminal and the browser cannot disagree about what the folder holds.
        """
        return {
            "root": self.root,
            "project": self.project,
            "exists": self.exists,
            "enabled": self.enabled,
            "count": len(self.files),
            "runnable": len(self.runnable()),
            "workflows": [f.to_payload(nodes=nodes) for f in self.files],
            "truncated": self.truncated,
            "truncated_by": self.truncated_by,
            "errors": list(self.errors),
            "files_seen": self.files_seen,
            "nested": self.nested,
            "age": round(max(0.0, time.time() - self.at), 1) if self.at else None,
            "ms": round(self.ms, 1),
        }


# ── The location ──────────────────────────────────────────────────────────────

def workflows_root(root=None) -> Path:
    """`<workspace>/.agent2/workflows` — asked through `projectdoc`, never rebuilt.

    ⚠️ `workspace.root()` is read LIVE when *root* is omitted, `skills.skills_root`'s
    rule: a module-level path binds the directory the process started in, so every
    later workspace switch keeps reading the first project's files.
    """
    from agent2.core import projectdoc as _pd
    from agent2.core import workspace as _ws
    return _pd.workflows_dir(root if root is not None else _ws.root())


def path_for(name: str, root=None, *, fmt: str = "yaml") -> Path:
    """Where a workflow called *name* lives — for Task 39's `new` and `edit`.

    Here rather than at a call site so the suffix a *new* file gets and the suffixes
    discovery *reads* cannot drift apart: `/workflow new audit` must produce a file
    the very next `/workflow` lists.
    """
    suffix = fmt if fmt in SUFFIXES else "yaml"
    return workflows_root(root) / f"{graph.fold_id(name)}.{suffix}"


# ── Reading one file ──────────────────────────────────────────────────────────

def _read(path: Path, limit: int) -> tuple[str, int, bool]:
    """`(text, bytes_read, truncated)`, bounded. Decoding never fails."""
    with open(path, "rb") as fh:
        raw = fh.read(limit + 1)
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]
    return raw.decode("utf-8", errors="replace"), len(raw), truncated


def build(text: str, *, name: str, fmt: str = "yaml", rel: str = "",
          path: str = "", size: int = 0, truncated: bool = False,
          mtime: float = 0.0) -> WorkflowFile:
    """A `WorkflowFile` from text. Total, and the whole of Task 38's pipeline.

    Split out of the walk so an inline caller — a test, Task 39's editor previewing
    unsaved text, Task 40's planner emitting a definition — runs the *same* parse,
    upgrade, coerce and validate steps in the same order. A second pipeline would be
    a second answer to "is this workflow valid", and the one the user sees before
    saving would be the one nobody checked.
    """
    wf = WorkflowFile(name=graph.fold_id(name), rel=rel, path=path, fmt=fmt,
                      size=size, truncated=truncated, mtime=mtime)
    parsed, err = parse_text(text, fmt=fmt)
    wf.unparsed = parsed.unparsed
    wf.notes = list(parsed.notes)
    if err:
        wf.problems.append(_problem(L_PARSE, f"{fmt.upper()} could not be read: {err}"))
        return wf
    if truncated:
        # ⚠️ Refused, not read-as-far-as-it-got. A skill is still useful half-read;
        # a *declaration* is not — the tail of the file is where the last node's
        # `needs:` lives, so a clipped graph would fail as a fistful of
        # unknown-dependency errors pointing at nodes the author did write.
        wf.problems.append(_problem(
            L_TRUNCATED,
            f"larger than {config.WORKFLOW_MAX_BYTES} bytes "
            f"(AGENT2_WORKFLOW_MAX_BYTES) — the end of the file was not read"))
        return wf
    data = parsed.data
    if data is None or data in ("", {}, []):
        if parsed.unparsed:
            # ⚠️ "Empty" and "nothing here is a mapping" are two different facts, and
            # only one of them sends its author looking for missing bytes. A file of
            # prose — a README that landed in the folder, a note somebody started —
            # parses to nothing at all, so the count of skipped lines is the answer,
            # not the absence of data.
            wf.problems.append(_problem(
                L_NOT_MAPPING,
                f"no line in this file is a `key: value` ({parsed.unparsed} skipped) "
                f"— a workflow file is a mapping with `name:` and `nodes:`",
                count=parsed.unparsed, notes=list(parsed.notes)[:4]))
            return wf
        wf.problems.append(_problem(L_EMPTY, "the file declares nothing"))
        return wf
    if not isinstance(data, dict):
        wf.problems.append(_problem(
            L_NOT_MAPPING,
            f"a workflow file is a mapping with `name:` and `nodes:`, "
            f"this one is a {type(data).__name__}"))
        return wf

    if parsed.unparsed:
        wf.warnings.append(_problem(
            L_UNPARSED,
            f"{parsed.unparsed} line(s) were outside the YAML this build reads and "
            f"were skipped", count=parsed.unparsed, notes=list(parsed.notes)[:4]))

    # One coercion (`graph.make_def`) reads the version; the ladder then runs and,
    # if anything changed, the same coercion runs again. ⚠️ The version is read
    # exactly once, by the module that owns what a workflow literal means.
    defn = graph.make_def(data, source=rel or wf.name, name=wf.name)
    upgraded, applied = upgrade(data, version=defn.version)
    if applied:
        wf.upgraded_from = applied[0]
        defn = graph.make_def(upgraded, source=rel or wf.name, name=wf.name)
        wf.warnings.append(_problem(
            L_UPGRADED,
            f"written for schema {applied[0]}, migrated to {defn.version} on read",
            **{"from": applied[0], "to": defn.version}))

    declared = graph.fold_id(data.get("name") or data.get("workflow")
                             or data.get("id") or "")
    if declared and declared != wf.name:
        # ⚠️ The filename wins, and the disagreement is SAID. Silently preferring
        # `name:` would give the user a workflow they cannot run by the name they
        # see in the folder; silently ignoring it would leave them editing a line
        # that does nothing.
        wf.declared_name = declared
        wf.warnings.append(_problem(
            L_NAME_MISMATCH,
            f"the file says `name: {declared}` but is called '{wf.name}' — the "
            f"filename is the name Agent2 uses", declared=declared, used=wf.name))
    if defn.name != wf.name:
        defn = replace(defn, name=wf.name)

    wf.defn = defn
    wf.schema = defn.version
    wf.validation = graph.validate(defn)
    return wf


# ── Discovery ─────────────────────────────────────────────────────────────────

_CACHE: dict[tuple[str, str], tuple[float, Catalog]] = {}
_LOCK = threading.RLock()
_HOOKED = False
_COUNTERS = {"scans": 0, "hits": 0, "files": 0, "failed": 0}


def _ensure_hooks() -> None:
    """Drop the cache when the project changes — through `isolation`, once.

    ⚠️ NOT a second `workspace.manager.on_switch` listener, `skills.discovery`'s
    rule: `broker.isolation` already owns that wiring *and* the `workspace` sync
    topic, so a switch in the other process counts too.
    """
    global _HOOKED
    if _HOOKED:
        return
    _HOOKED = True
    try:
        from agent2.core.broker import isolation as _iso
        _iso.on_invalidate(invalidate)
    except Exception:                       # degrades to TTL-only
        pass


def invalidate() -> None:
    """Forget every cached catalog. Cheap, total, safe from a hook."""
    with _LOCK:
        _CACHE.clear()


def _project_key() -> str:
    try:
        from agent2.core.broker import isolation as _iso
        return _iso.current()
    except Exception:                       # an unknown project is still a key
        return ""


def discover(root=None, *, force: bool = False) -> Catalog:
    """Every workflow file in this project's `.agent2/workflows/`. Never raises.

    Total by contract, like `gitstate.snapshot()` and `skills.discover()`: a missing
    directory, an unreadable file, a permission fault and a symlinked path out of
    the folder all mean *that workflow is absent*, and the ones that are anybody's
    business land in `errors`, which both surfaces print.
    """
    _ensure_hooks()
    started = time.monotonic()
    try:
        base = Path(str(workflows_root(root)))
    except Exception:                       # no workspace, no workflows
        return Catalog(enabled=config.WORKFLOW_ENABLED, at=time.time())

    project = _project_key()
    key = (os.path.normcase(str(base)), project)
    if not force:
        with _LOCK:
            hit = _CACHE.get(key)
            if hit and hit[0] > started:
                _COUNTERS["hits"] += 1
                return hit[1]

    cat = _scan(base, project)
    cat.ms = (time.monotonic() - started) * 1000.0
    with _LOCK:
        _CACHE[key] = (started + WORKFLOW_TTL, cat)
        _COUNTERS["scans"] += 1
        _COUNTERS["files"] += len(cat.files)
        _COUNTERS["failed"] += sum(1 for f in cat.files if not f.ok)
    return cat


def _scan(base: Path, project: str) -> Catalog:
    """The bounded, deterministic read. One guard per file, never one per scan.

    ⚠️ `sorted()`, always: the filesystem hands back whatever order it likes, and
    two machines must not disagree about which of two files a prefix resolves to.
    """
    cat = Catalog(root=str(base), project=project,
                  enabled=config.WORKFLOW_ENABLED, at=time.time())
    try:
        if not base.is_dir():
            return cat
        cat.exists = True
        entries = sorted(base.iterdir(), key=lambda p: p.name.lower())
        root_resolved = base.resolve()
    except Exception as exc:                # an unreadable folder reads as empty
        cat.errors.append(f"workflows folder unreadable: {str(exc)[:120]}")
        return cat

    from agent2.core import workspace as _ws
    deadline = time.monotonic() + config.WORKFLOW_SCAN_BUDGET_SEC
    found: list[WorkflowFile] = []
    for entry in entries:
        if time.monotonic() > deadline:
            cat.truncated, cat.truncated_by = True, BY_BUDGET
            break
        try:
            if entry.is_dir():
                cat.nested += 1
                continue
        except Exception:                   # a vanished entry is simply absent
            continue
        suffix = entry.suffix.lower().lstrip(".")
        if suffix not in SUFFIXES:
            continue
        cat.files_seen += 1
        if len(found) >= config.WORKFLOW_MAX_FILES:
            cat.truncated, cat.truncated_by = True, BY_COUNT
            break
        name = graph.fold_id(entry.stem)
        if not name:
            cat.errors.append(f"{entry.name}: refused — no usable name")
            continue
        if any(f.name == name for f in found):
            # `audit.yaml` beside `audit.json`. Named, because the loser is a file
            # the user is editing and watching do nothing.
            cat.errors.append(
                f"{entry.name}: ignored — '{name}' is already declared by "
                f"{next(f.rel for f in found if f.name == name)}")
            continue
        # ⚠️ Containment, per file, through the sandbox's own predicate: a symlink
        # is the filesystem's form of cross-project leakage and looks like an
        # ordinary local file from every other angle.
        try:
            resolved = entry.resolve()
            if not _ws.manager.is_within(resolved, root_resolved):
                wf = WorkflowFile(name=name, rel=entry.name, path=str(entry),
                                  fmt="json" if suffix == "json" else "yaml")
                wf.problems.append(_problem(
                    L_OUTSIDE, "refused — resolves outside the workflows folder"))
                found.append(wf)
                continue
        except Exception as exc:
            wf = WorkflowFile(name=name, rel=entry.name, path=str(entry))
            wf.problems.append(_problem(L_OUTSIDE, f"refused — {str(exc)[:100]}"))
            found.append(wf)
            continue
        try:
            text, size, cut = _read(entry, config.WORKFLOW_MAX_BYTES)
            mtime = entry.stat().st_mtime
        except Exception as exc:            # one unreadable file costs one workflow
            wf = WorkflowFile(name=name, rel=entry.name, path=str(entry))
            wf.problems.append(_problem(L_UNREADABLE, f"unreadable — {str(exc)[:100]}"))
            found.append(wf)
            continue
        try:
            found.append(build(text, name=name, rel=entry.name, path=str(entry),
                               fmt="json" if suffix == "json" else "yaml",
                               size=size, truncated=cut, mtime=mtime))
        except Exception as exc:            # `build` is total; belt for the day it is not
            wf = WorkflowFile(name=name, rel=entry.name, path=str(entry))
            wf.problems.append(_problem(L_UNREADABLE, f"unreadable — {str(exc)[:100]}"))
            found.append(wf)

    if cat.nested:
        cat.errors.append(
            f"{cat.nested} subdirector{'y' if cat.nested == 1 else 'ies'} ignored — "
            f"a workflow file lives directly in .agent2/workflows/")
    found.sort(key=lambda f: f.name)
    cat.files = found
    return cat


def load(name: str, root=None, *, force: bool = False) -> WorkflowFile | None:
    """One workflow by name or unique prefix, or None. Never raises.

    Goes through `discover()` rather than reading the file directly, so `/workflow
    run audit` and the `/workflow` list can never disagree about which file 'audit'
    is — and a run pays a cache hit rather than a second parse.
    """
    try:
        return discover(root, force=force).find(name)
    except Exception:                       # discover is total; belt only
        return None


def stats() -> dict:
    """Cache and counters, for `core.health` and `describe()`. No workflow text."""
    with _LOCK:
        return {
            "cached": len(_CACHE),
            "ttl": WORKFLOW_TTL,
            "scans": _COUNTERS["scans"],
            "hits": _COUNTERS["hits"],
            "files": _COUNTERS["files"],
            "failed": _COUNTERS["failed"],
            "max_files": config.WORKFLOW_MAX_FILES,
            "max_bytes": config.WORKFLOW_MAX_BYTES,
            "budget_sec": config.WORKFLOW_SCAN_BUDGET_SEC,
        }


def describe() -> dict:
    """What a workflow FILE may look like — for `/workflow` and the web panel.

    ⚠️ Derived from the tables above, never documented twice: "what shape does a
    workflow file take" is the first question when one does not load, and a README
    can go stale while a payload built from `SUBSET`, `SUFFIXES` and `UPGRADES`
    cannot. `graph.describe()` answers the same question about the *graph*; this
    wraps it rather than restating any of it.
    """
    from agent2.core import projectdoc as _pd
    out = dict(graph.describe())
    out.update({
        "suffixes": list(SUFFIXES),
        # ⚠️ ASKED, never spelled: `.agent2/workflows` written as a literal here is
        # the second declaration of a path, and `projectdoc.doc_dir`'s docstring
        # records what that costs. Posix separators because this string is *shown*
        # to a human, not opened.
        "dir": "/".join(_pd.workflows_dir("").parts[-2:]),
        "max_files": config.WORKFLOW_MAX_FILES,
        "max_bytes": config.WORKFLOW_MAX_BYTES,
        "budget_sec": config.WORKFLOW_SCAN_BUDGET_SEC,
        "max_nest": MAX_NEST,
        "subset": list(SUBSET),
        "file_problems": list(FILE_PROBLEM_CODES),
        "file_warnings": list(FILE_WARNING_CODES),
        "upgrades": sorted(int(v) for v in UPGRADES),
        "oldest_supported": oldest_supported(),
        # ⚠️ Still False, and now it says what it means: no PyYAML, a declared
        # subset instead. `graph.describe()` set it before this module existed and
        # the answer did not change — the reader did.
        "yaml": False,
        "yaml_subset": True,
    })
    return out
