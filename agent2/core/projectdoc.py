# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/projectdoc.py
─────────────────────────
THE writer of `.agent2/` — the project structure (Task 30) and `agent2.md` itself
(Task 31). `core/projectscan.py` decides *what is true about this project*; this
module decides *what of that goes on disk, and what a human already wrote there
that must survive*.

`/init` is the whole flow: scan → render → write. After it, the project has

    .agent2/
      agent2.md          ← the sections `SECTIONS` names (Task 31's fifteen + four)
      skills/            ← Phase 11 discovers skills here (Task 32)
        README.md        ← only when the directory is new

and `core.broker.PRIMARY_DOC` is `.agent2/agent2.md`, so from the next turn on
every surface's system prompt carries it. That is the point of the file: `/init`
is not a report, it is how a project tells the agent what it is.

⚠️ WHAT THE PROJECT *IS* COMES FROM A MODEL; WHAT IT *CONTAINS* COMES FROM THE
WALK. `projectscan` can count Python files and find `pytest`; nothing in a
directory listing says "a self-hosted agent with three surfaces sharing one SQLite
brain, built so a security researcher can drive Burp from a terminal". So `/init`
makes exactly one bounded model call (`narrate()`) with the scan and the README as
evidence, and fills `## Purpose`, `## Features` and `## Architecture` from the
reply. All of it is optional: no key, no network, a refusal or an unparseable
answer each degrade to evidence-only prose that says what is missing, because the
offline install is the one this project is built for. `described` in the result
says which happened — "we asked and it said this" and "no model was reachable" are
different claims about the same file.

⚠️ THE SHAPE IS BORROWED FROM A FILE THAT WORKS, AND THE BORROWING IS THE POINT.
This repository's own `CLAUDE.md` is the reference: it is useful not because it is
long but because of four devices, and `SECTIONS` reproduces all four so a generated
doc can grow into one instead of staying a census with headings.

  * **It says how to read itself first.** `## How To Work Here` is that — the marker
    rule, where the commands live, that `_Not established_` means *unknown* rather
    than *empty*, and when to refresh the file. Without it, the precedence this
    whole module is built around is a fact only this source file knows, and the
    agent reading the doc every turn never learns it.
  * **It states invariants, not descriptions.** `## Invariants` is seeded with the
    two tables `CLAUDE.md` uses — *rule · why · pinned by* and *thing · the one
    place · never* — because "what must not break, and what happens if it does" is
    the half of a codebase that a walk cannot see and a reader cannot infer.
    ⚠️ Seeded **unmarked** and never written by a scan: an invariant is hard-won,
    and a walker inventing one is worse than a gap a human can fill.
  * **It explains how a request moves.** `## Data Flow` is the fourth narrated
    section, for the one architectural question a directory listing cannot answer
    at all.
  * **It says what each part is FOR, not just that it exists.** `## Important
    Files` is a table — file · what it says about itself · why it was read — built
    from `projectscan.file_notes`, which is the project's own leading comments.
    That is the machine-derivable analogue of `CLAUDE.md`'s subsystem briefs, and
    the `why` column is the fact a flat bullet list drops.

⚠️ EVERY ONE OF THOSE SECTIONS IS STILL THE USER'S TO TAKE. The marker rule below
is what makes the borrowing safe: seeding a template is an offer, and deleting one
line makes it theirs forever.


The user's own sentence is labelled authoritative in the evidence, printed on its
own line under `## Purpose`, and read back out of the file by `prior_hint()` — so
`/init` run a year later with no argument still knows what they said. It lives in
the doc rather than in `settings` because the sentence belongs to the project, it
survives a copied checkout, and it is correctable by editing the file the user was
already told to edit.


⚠️ THE MARKER DECIDES OWNERSHIP, AND THAT IS THE WHOLE DESIGN.
Task 31: *"DO NOT blindly overwrite it. … User-authored instructions must have
priority over automatically generated descriptions."* A rewrite therefore needs to
know which prose it wrote and which prose a human wrote, and there is exactly one
honest way to know: say so at the time of writing. Every section Agent2 generates
carries `<!-- agent2:generated -->` on the line after its heading.

  * heading present **with** the marker  → Agent2 owns the body; it is regenerated,
    and a changed body is the *stale information* Task 31 asks to identify.
  * heading present **without** the marker → a human owns it. Preserved byte for
    byte, forever, including a heading Agent2 would otherwise generate. Deleting
    the marker is how a user says "this section is mine now", and it is the only
    gesture needed.
  * heading Agent2 generates and the file lacks → inserted at its canonical
    position, marked.
  * heading nobody generates → preserved in place, in the user's own order.

⚠️ `## Known Issues`, `## Security Notes` AND `## Agent2 Instructions` ARE SEEDED
**UNMARKED**. They are the three sections a scan cannot know — and seeding them
unmarked is what makes "user prose outranks generated prose" true from the *first*
write rather than from the first edit. A marked placeholder would be overwritten
by the next `/init`, which is precisely the standing-orders section a user is most
likely to have filled in.

⚠️ NEVER A GLOBAL CONFIGURATION, AND THE HOME DIRECTORY IS THE TRAP.
Task 30: *"Do not create a global project configuration accidentally."* The
concrete danger in this build is not abstract: `config`'s secret backend defaults
to `~/.agent2/secret.key`, so an `/init` run with the home directory as the
workspace root would write `agent2.md` into the folder holding the master
encryption key — a per-machine directory that every project would then read as its
project doc. `_refuse_root()` blocks the home directory, a filesystem root, and
any parent of the secret-key directory, and it says which one it blocked.

⚠️ THE DOC IS BIGGER THAN THE PROMPT SLOT, SO SOMETHING IS CLIPPED — AND WHAT GETS
CLIPPED IS THIS MODULE'S DECISION, NOT THE BROKER'S. `broker.PROJECT_DOC_CHARS`
owns *how much* of `agent2.md` a prompt may spend; `for_prompt()` here owns *which
part*, because "who owns this section" is a fact of this module and of nowhere else.
The old `body[:PROJECT_DOC_CHARS]` slice was a head-first cut, and the sections a
human owns are the ones a document ends with — so the very first thing the prompt
discarded was the user's own standing orders, silently, at the exact moment the doc
grew useful. `for_prompt()` keeps every **unmarked** section whole, drops generated
sections from the end until it fits, and names what it left out. ⚠️ If the human
sections alone exceed the limit they are kept anyway and `over` is reported rather
than fixed — the same choice `core/broker/budget.py` makes about pinned items, for
the same reason: an honestly-too-large prompt fails somewhere a human can read,
while a quiet trim of a user's rules cannot be recovered from.

 The new text is written to a temp
file beside the target and `os.replace`d, so an interrupted `/init` cannot leave a
half-truncated `agent2.md` — a file whose whole purpose is to be read by every
later turn. And an identical rewrite is skipped outright (`changed: False`), so
running `/init` twice does not touch the file's mtime, does not dirty the git tree
and does not appear in a diff review as a change nobody made.

Layer: projectscan → projectdoc → cli.render · server.routes.
"""

import os
import re
import time
from pathlib import Path

# The marker that means "Agent2 wrote this body". ⚠️ One spelling, matched by
# `_MARKER_RE` for tolerance of stray whitespace but written in exactly this form.
MARKER = "<!-- agent2:generated -->"
_MARKER_RE = re.compile(r"^\s*<!--\s*agent2:generated.*?-->\s*$", re.I)
_HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")

DOC_REL = "agent2.md"          # inside `.agent2/`; the full path is `broker.PRIMARY_DOC`
SKILLS_DIR = "skills"
WORKFLOWS_DIR = "workflows"    # Task 38: `.agent2/workflows/*.yaml`


def doc_dir(root) -> Path:
    """`<root>/.agent2`. ⚠️ ONE SPELLING OF THE LOCATION, for every reader.

    `apply()` writes here, the web surface reports on it and `broker.PRIMARY_DOC`
    names the same file relatively. A second `Path(root) / ".agent2" / "agent2.md"`
    at a call site is the copy that keeps working after this one moves.
    """
    return Path(str(root)) / ".agent2"


def doc_path(root) -> Path:
    return doc_dir(root) / DOC_REL


def skills_dir(root) -> Path:
    """`<root>/.agent2/skills`. ⚠️ ONE SPELLING, for the same reason `doc_dir` is.

    `apply()` creates it and seeds its README; `core.skills.discovery` reads it on
    the turn path. Two spellings would mean `/init` seeds one folder while the
    broker reads another — a skill collection nothing ever loads, silently, and on
    a case-sensitive filesystem the two would never even converge (exactly how
    `.agent2/Agent2.md` vs `.agent2/agent2.md` is documented above).
    """
    return doc_dir(root) / SKILLS_DIR


def workflows_dir(root) -> Path:
    """`<root>/.agent2/workflows`. ⚠️ ONE SPELLING, for `skills_dir`'s reason.

    `core.workflow.loader` reads it; Task 39's `/workflow new` is what creates it.
    ⚠️ It is deliberately **not** seeded by `apply()` the way `skills/` is: a folder
    exists here because a human put a workflow in it, and creating an empty one in
    every project would add a directory to somebody's repository that nothing reads
    and nobody asked for.
    """
    return doc_dir(root) / WORKFLOWS_DIR


def read_existing(root) -> str:
    """The project's current `agent2.md`, or `""`. Never raises.

    ⚠️ Reading is the FIRST half of "never blindly overwrite", so it is a public
    function: a caller that wants to show what a rerun would preserve must see
    exactly the text `apply()` will merge, not its own re-read of a path it guessed.
    """
    try:
        p = doc_path(root)
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    except Exception:
        return ""

# Task 31's fifteen sections, in the order it lists them, PLUS four this build
# adds — `Agent2 Instructions`, `How To Work Here`, `Invariants` and `Data Flow`.
# ⚠️ Do not "correct" the count by deleting them. Three of the four are what makes
# this file the thing `CLAUDE.md` is rather than a census with headings:
#
#   * `How To Work Here` states how to READ the rest — the marker rule, and that
#     `_Not established_` means *unknown*, not *empty*. Without it the precedence
#     the whole module is built around is a fact only this source file knows.
#   * `Agent2 Instructions` is where a user's standing orders live, or they have
#     nowhere except a generated section the next `/init` overwrites.
#   * `Invariants` is where the rules that have already been broken once go —
#     ⚠️ seeded UNMARKED and never written by a scan, because an invariant is
#     hard-won human knowledge and a walker inventing one is worse than a gap.
#   * `Data Flow` is narrated: how a request moves through the parts, which is the
#     one architectural question a directory listing cannot answer at all.
#
# ⚠️ THIS IS THE ONE ORDERING — a missing section is inserted relative to it, and
# a renderer or a later phase reads it rather than restating it. It is also the
# order the PROMPT is clipped in (`for_prompt()` + `broker.PROJECT_DOC_CHARS`),
# which is why the instruction and the two human sections come first: the front of
# this tuple is the part of the document that always survives.
SECTIONS: tuple[str, ...] = (
    "How To Work Here", "Agent2 Instructions", "Invariants",
    "Overview", "Purpose", "Features", "Architecture", "Data Flow", "Tech Stack",
    "Project Structure", "Important Files", "Entry Points", "Build Commands",
    "Test Commands", "Development Commands", "Configuration",
    "Important Conventions", "Known Issues", "Security Notes",
)

# ⚠️ THE ORDER GENERATED SECTIONS ARE GIVEN UP IN WHEN THE DOC DOES NOT FIT A
# PROMPT — most expendable first — AND IT IS NOT `SECTIONS`. The same deliberate
# disagreement `core/broker/sources.py` documents between its `ORDER` (where a source
# prints) and its `PRIORITY` (what survives a budget): `SECTIONS` is written for a
# human reading the file top to bottom, this is written for an agent that cannot have
# all of it. Derive either from the other and you must choose between a file that
# reads in a useless order and a prompt that surrenders this project's test command
# to keep a directory tree the agent could have produced with one `list_dir`.
#
# The rule behind the list: what an agent can rebuild for itself goes first; what
# tells it what to RUN goes last, because a command it cannot see is a command it
# guesses. ⚠️ `How To Work Here` is last of all — it is the section that states the
# marker rule, so it is the one whose absence makes every surviving section
# ambiguous about who owns it.
# ⚠️ A generated heading NOT named here is given up BEFORE anything that is: it can
# only be a section some older Agent2 wrote and this build no longer generates, so it
# is stale by construction. ⚠️ Unmarked sections are not in this list at all and
# never can be — `for_prompt()` never offers them, whatever their heading.
CLIP_ORDER: tuple[str, ...] = (
    "Project Structure",        # a tree — one `list_dir` rebuilds it
    "Tech Stack",               # language percentages
    "Overview",                 # counts; the project name is already in the prompt
    "Features",                 # what it does, not how to work on it
    "Architecture",
    "Data Flow",
    "Purpose",                  # why it exists — frames a judgement call
    "Important Files",          # where to look first, and why
    "Configuration",
    "Important Conventions",    # how to write code that fits in
    "Entry Points",
    "Build Commands",
    "Development Commands",
    "Test Commands",            # ⚠️ "run the tests before reporting done" needs this
    "How To Work Here",         # the instruction for reading everything above
)
_CLIP_RANK: dict[str, int] = {name: i for i, name in enumerate(CLIP_ORDER)}


def _clip_rank(heading: str) -> int:
    """Where `heading` sits in the surrender order. Unknown ⇒ first to go."""
    return _CLIP_RANK.get(heading, -1)


# The four sections a *scan* cannot write — what this project is, why it exists,
# what it does and how a request moves through it — so `narrate()` asks a model for
# them and they degrade to evidence-only prose when no model is reachable.
# ⚠️ They are generated (marked) like any other: a model's guess must be as
# rewritable as a walker's count. ⚠️ These are HEADINGS; `narrate()`'s keys are the
# lowercase forms, mapped in `prior_narrative()` and `generate()`.
NARRATED: tuple[str, ...] = ("Purpose", "Features", "Architecture", "Data Flow")

# The same four as `narrate()` spells them. ⚠️ ONE DECLARATION, because `apply()`
# reports which fields the carry-forward reused and a literal tuple there goes stale
# the moment a field is added: `dataflow` arrived in D3 and a hand-written
# `("purpose", "features", "architecture")` would have carried it silently while
# telling the user — and `res["narrative_carried"]` is what both surfaces print —
# that three things were reused. `_carry_narrative()` still spells each rule out,
# because the rules differ per field (a hint-seeded purpose, a list of features).
NARRATED_FIELDS: tuple[str, ...] = ("purpose", "features", "architecture", "dataflow")


# ⚠️ THE "NOTHING WAS ESTABLISHED" PROSE, DECLARED ONCE, BECAUSE `carry_narrative()`
# HAS TO RECOGNISE IT. These four strings are the *absence* of a narrative, and the
# carry-forward must never mistake one for prose worth keeping — a doc whose
# `## Features` had been refreshed from a placeholder back into a placeholder would
# then be reported as `unchanged` while the real feature list stayed lost. Matching
# the text loosely (`startswith("_Not established")`) would also match a sentence a
# *user* wrote, so the comparison is against these exact constants and nothing else.
NO_PURPOSE = ("_Not established. Run `/init <one line about this project>` "
              "to state it, or write it here and delete the marker above._")
NO_FEATURES = ("_Not established from the evidence. List what this project does here "
               "and delete the marker above to keep your version._")
NO_ARCHITECTURE = ("_Layout only — no description was established. Replace this with "
                   "the real design and delete the marker above to keep it._")
NO_DATAFLOW = ("_Not established — a directory walk cannot see how a request moves. "
               "Trace it here and delete the marker above to keep it._")
_PLACEHOLDERS: frozenset[str] = frozenset({
    NO_PURPOSE, NO_FEATURES, NO_ARCHITECTURE, NO_DATAFLOW,
})

# Where a user's own `/init <description>` is recorded, so a later `/init` with no
# argument still knows it. ⚠️ It lives in the DOC, not in `settings`: the sentence
# belongs to the project, it survives a copied checkout, and the user can correct
# it by editing the file they were told to edit.
HINT_PREFIX = "**Stated purpose**:"

# ⚠️ The four a scan cannot know. Seeded UNMARKED on creation — see the docstring.
HUMAN_SECTIONS: frozenset[str] = frozenset({
    "Known Issues", "Security Notes", "Agent2 Instructions", "Invariants",
})

# What a brand-new file says in those four, so the reader knows they are theirs.
# ⚠️ A seed is an OFFER, not content: it must read as an empty form a human fills,
# never as an answer. `Invariants` therefore ships the two tables and no rows —
# a seeded *example* invariant would be indistinguishable from a real one, and a
# reader who believed it would be following a rule this project never had.
_SEEDS: dict[str, tuple[str, ...]] = {
    "Known Issues": (
        "_Not known automatically — `/init` scans structure, not behaviour._",
        "",
        "- ",
    ),
    "Security Notes": (
        "_Not known automatically. Anything the agent must never touch belongs here._",
        "",
        "- ",
    ),
    "Invariants": (
        "Rules that must not break, and what happens when they do. This section is",
        "**yours** — `/init` seeds these two tables once and never rewrites them.",
        "",
        "Write down the ones that have already been broken once. A rule with no",
        "consequence beside it gets \"simplified\" away by the next reader.",
        "",
        "| Rule | Why it exists | Pinned by |",
        "|------|---------------|-----------|",
        "|  |  |  |",
        "",
        "And anything that has exactly one home, so a second copy cannot drift:",
        "",
        "| Thing | The one place | Never |",
        "|-------|---------------|-------|",
        "|  |  |  |",
    ),
    "Agent2 Instructions": (
        "Standing instructions for Agent2 in this project. This section is **yours** —",
        "`/init` seeds it once and never rewrites it.",
        "",
        "- Ask before changing anything outside this project's root.",
        "- Run the test command in `## Test Commands` before reporting a change done.",
    ),
}



def empty_result() -> dict:
    """The shape `apply()` always returns. Every key a caller may read is here."""
    return {
        "ok": False, "reason": "", "root": "", "dir": "", "doc": "",
        "created": False, "changed": False, "existed": False,
        "dirs_created": [], "files_created": [],
        "added": [], "updated": [], "unchanged": [], "preserved": [],
        "hint": "", "described": False, "describe_note": "", "features": 0,
        "narrative_carried": [],
        "bytes": 0, "at": 0.0, "errors": [],
    }


# ── Where `/init` may not write ───────────────────────────────────────────────
def _refuse_root(base: Path) -> str:
    """Return a refusal reason, or "" when this root may hold a `.agent2/`.

    ⚠️ Task 30's "do not create a global project configuration accidentally",
    made concrete. Two cases produce a `.agent2/` that is not *a project's*, and
    the second is a real collision rather than a hypothetical: `core.secrets`
    keeps the master encryption key at `~/.agent2/secret.key`, so an `/init` whose
    workspace root happens to be the home directory writes `agent2.md` into the
    per-machine Agent2 folder — where it is not this project's doc but every
    project's, sitting next to the key that protects the database.

    ⚠️ The comparison asks `secrets.key_file()` rather than restating `~/.agent2`,
    so an install that moved the key with `AGENT2_SECRET_KEY_FILE` is still
    protected at the location it actually uses.
    """
    try:
        real = Path(os.path.abspath(str(base)))
    except Exception:
        return "unreadable root"
    try:
        if real.parent == real:
            return "filesystem root"
    except Exception:
        pass
    try:
        from agent2.core import secrets as _secrets
        keydir = _secrets.key_file().parent.resolve()
        if doc_dir(real).resolve() == keydir:
            return ("that `.agent2` is the per-machine Agent2 folder — it holds the "
                    "master key, and a project doc there would be read by every project")
    except Exception:
        pass
    return ""


# ── Parsing an existing file ──────────────────────────────────────────────────
class _Block:
    """One `## Heading` and its body, plus who owns it."""

    __slots__ = ("generated", "heading", "lines")

    def __init__(self, heading: str, lines: list[str], generated: bool):
        self.heading = heading
        self.lines = lines
        self.generated = generated


def _body(lines) -> list[str]:
    """A section body in CANONICAL form: no leading, no trailing blank lines.

    ⚠️ ONE normalization, applied by `parse()` on the way in and by `generate()` on
    the way out, because `render(parse(x)) == x` has to be a **fixed point**: the
    merge decides `updated` vs `unchanged` by comparing a generated body against a
    parsed one, and two spellings of "the same body" make that comparison report a
    refresh that never happened. Internal blank lines are structure and are kept —
    `## Architecture` deliberately separates its prose from the directory list.
    """
    out = list(lines)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def parse(text: str) -> tuple[list[str], list[_Block]]:
    """Split a doc into (preamble, blocks). Total — malformed input is preamble."""
    preamble: list[str] = []
    blocks: list[_Block] = []
    cur: _Block | None = None
    for raw in (text or "").splitlines():
        m = _HEADING_RE.match(raw)
        if m:
            cur = _Block(m.group(1), [], False)
            blocks.append(cur)
            continue
        if cur is None:
            preamble.append(raw)
            continue
        # The marker counts only as the FIRST non-empty line of a body: a marker
        # a user pasted lower down does not hand Agent2 the section they wrote.
        if not cur.lines and _MARKER_RE.match(raw):
            cur.generated = True
            continue
        if not cur.lines and not raw.strip():
            continue
        cur.lines.append(raw)
    for b in blocks:
        b.lines = _body(b.lines)
    return preamble, blocks


# ── What the project IS: the narrated sections ────────────────────────────────
# ⚠️ A SCAN CANNOT ANSWER "WHY DOES THIS EXIST". It can count Python files and
# find `pytest`; it cannot say "a self-hosted agent with three surfaces sharing one
# SQLite brain". So `/init` asks a model once, with the scan as evidence, and the
# answer is clearly marked as generated. Everything about this step is optional:
# no key, no network, a refusal or a malformed reply all degrade to the
# evidence-only prose below, because an `/init` that fails without an API key
# would be useless in exactly the offline install this project is built for.

README_EXCERPT = 4000        # chars of README handed to the model as evidence
MAX_FEATURES = 12            # bullets kept from a reply, so one turn cannot bloat the doc
NARRATE_TIMEOUT_NOTE = "no model was reachable"

# `projectscan.file_notes` — what the project's own important files say about
# themselves. ⚠️ THESE ARE THE EVIDENCE THAT ANSWERS "WHY", and they are why the
# narration stops inferring a purpose from directory names: a docstring is the
# author telling a reader what a module is for, in a sentence no census produces.
# Bounded twice over — `config.INIT_NOTE_CHARS` caps what the scan kept, and these
# cap what reaches one prompt, so the notes cannot crowd out the README excerpt.
NOTE_EVIDENCE_FILES = 10
NOTE_EVIDENCE_CHARS = 600
# How many files `## Important Files` names from `file_notes`. The one-line summary
# it prints per file is the scan's own `summary` field, never re-derived here — see
# `projectscan.NOTE_SUMMARY_CHARS`, which owns "the first sentence of a note" for
# this section, `cli/render._scan_rows()` and the browser's Project panel alike.
MAX_DOC_NOTES = 10
# What `## Data Flow` may spend. ⚠️ A ceiling on a *narrated* section, unlike the
# other three, because it is the one a model is invited to answer with many short
# lines — and a flow that keeps growing is the section that pushes the human-owned
# ones towards `for_prompt()`'s clip.
DATAFLOW_CHARS = 1200

_NARRATE_PROMPT = """You are documenting a software project for an AI coding agent
that will read your description before every future task in this repository.

Answer ONLY with a JSON object, no prose and no code fences:

{
  "purpose":  "2-4 sentences: what this project IS and WHY it exists. Say who it is
               for and what problem it solves. Do not list technologies here.",
  "features": ["one short line per user-facing feature or capability, most
                important first, at most 12"],
  "architecture": "3-6 sentences on how it is put together: the major parts, what
               each one is responsible for, and what state they share. Describe the
               PARTS here, not the sequence.",
  "dataflow": "3-6 lines tracing ONE request, command or input end to end through
               those parts, in order, naming the file or module at each hop. Use the
               form 'Input -> a.py:handler -> b.py -> storage'. This is the question
               a directory listing cannot answer; if the evidence does not show the
               sequence, set it to ''."
}

Rules:
- Use ONLY the evidence below. If the evidence does not support a claim, leave it out.
- If the evidence is too thin to say what the project is, set "purpose" to "".
- Never invent a feature, a framework or a guarantee that is not evidenced.
- The stated purpose from the project's owner, if present, is authoritative.
- The "key files" section is each file's own leading comment or docstring — the
  authors describing their own code. Trust it over anything you would infer from a
  file name, and use it to say what the major parts ARE.

EVIDENCE
────────
%s
"""


def _evidence(rep: dict, hint: str = "", existing: str = "") -> str:
    """The bounded digest handed to the model. Structure and prose only.

    ⚠️ NO FILE CONTENTS EXCEPT PROSE A HUMAN WROTE FOR A READER. The README and an
    existing `agent2.md` are here because they are addressed to whoever opens the
    project; so are `file_notes`, which hold nothing but the leading comment or
    docstring of a few important files — the author describing their own module.
    Source *statements*, `.env` values and manifest bodies are not, and this string
    leaves the machine.

    ⚠️ THE BOUNDARY IS ENFORCED IN `projectscan._leading_comment()`, NOT HERE.
    That function is a syntax gate — comment syntax in, comment text out, `""` for
    a format it does not know — so this module never has to decide whether a line
    it was handed is prose or code. Widening the rule to "and comments" was safe
    only because the widening came with that gate; a scan that sampled the first N
    lines of a file would have broken this docstring's promise silently.
    """
    rep = rep or {}
    langs = ", ".join(f"{lang.get('name')} ({lang.get('share', 0)}%)"
                      for lang in (rep.get("languages") or [])[:6])
    fws = ", ".join(str(f.get("name")) for f in (rep.get("frameworks") or []))
    eps = "; ".join(f"{e.get('path')} ({e.get('why')})"
                    for e in (rep.get("entry_points") or [])[:8])
    dirs = ", ".join(f"{d.get('path')}/" for d in (rep.get("structure") or [])[:14])
    cmds = rep.get("commands") or {}
    cmdline = "; ".join(str(c.get("cmd")) for kind in
                        ("install", "dev", "build", "test", "lint")
                        for c in (cmds.get(kind) or [])[:2])
    parts = [
        f"Project name: {rep.get('name') or '?'}",
        f"Size: {rep.get('files', 0)} files, {rep.get('dirs', 0)} directories",
        f"Languages: {langs or 'none recognised'}",
        f"Frameworks/libraries detected: {fws or 'none recognised'}",
        "Package managers: " + (", ".join(str(m.get('label'))
                                           for m in (rep.get('package_managers') or []))
                                 or "none"),
        f"Top-level directories: {dirs or 'none'}",
        f"Entry points: {eps or 'none found'}",
        f"Declared commands: {cmdline or 'none'}",
        f"Tests: {(rep.get('tests') or {}).get('files', 0)} file(s), runners: " +
        (", ".join((rep.get("tests") or {}).get("runners") or []) or "none declared"),
        "Config files: " + (", ".join((rep.get("config_files") or [])[:12]) or "none"),
        "Conventions observed: " + ("; ".join(rep.get("conventions") or []) or "none"),
    ]
    if hint:
        # ⚠️ FIRST AND LABELLED AUTHORITATIVE. The user typed this sentence about
        # their own project; a model that contradicts it is wrong by definition.
        parts.insert(0, f"STATED PURPOSE FROM THE PROJECT OWNER (authoritative): {hint}")
    doc = _read_head(rep.get("root") or "", rep.get("readme") or "", README_EXCERPT)
    if doc:
        parts.append(f"--- {rep.get('readme')} (excerpt) ---\n{doc}")
    notes = rep.get("file_notes") or []
    if notes:
        # ⚠️ LABELLED AS THE AUTHORS' OWN WORDS, because that is what makes it
        # outrank a guess from a file name — and the label is what stops the model
        # reading a note as this build's opinion of the file.
        lines = "\n".join(
            f"* {n.get('path')} ({n.get('why')}): "
            f"{str(n.get('text') or '')[:NOTE_EVIDENCE_CHARS]}"
            for n in notes[:NOTE_EVIDENCE_FILES])
        parts.append("--- key files, described by their own leading comments and "
                     "docstrings (the authors' words; no code) ---\n" + lines)
    prior = _prior_prose(existing)
    if prior:
        parts.append("--- the previous description of this project, for continuity "
                     "(correct it where the evidence disagrees) ---\n" + prior)
    return "\n".join(parts)


def _read_head(root: str, rel: str, limit: int) -> str:
    """Read at most `limit` chars of one documentation file. Total."""
    if not root or not rel:
        return ""
    try:
        p = Path(root) / rel
        if not p.is_file():
            return ""
        return p.read_text(encoding="utf-8", errors="replace")[:limit].strip()
    except Exception:
        return ""


def prior_hint(text: str) -> str:
    """The `/init <description>` a previous run recorded, or "".

    ⚠️ This is why `/init` twice does not forget what the user told it once. The
    hint is stored as a line in the doc rather than in `settings`, so it travels
    with the project and can be corrected by editing the file.
    """
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith(HINT_PREFIX):
            return line[len(HINT_PREFIX):].strip().strip("_*").strip()
    return ""


def _prior_prose(text: str) -> str:
    """The previous narrated bodies, as continuity evidence."""
    if not text:
        return ""
    _pre, blocks = parse(text)
    out: list[str] = [f"## {b.heading}\n" + "\n".join(b.lines[:20])
                      for b in blocks if b.heading in NARRATED and b.lines]
    return "\n\n".join(out)[:3000]


def prior_narrative(text: str) -> dict:
    """The narrative an earlier run already established, read back out of the doc.

    ⚠️ THIS IS WHAT STOPS A MODEL-LESS REFRESH FROM ERASING THE DESCRIPTION. The
    narrated three are *generated* sections, so `render()` rewrites them from
    `generate()` — and `generate()` with no narrative writes the "nothing was
    established" placeholder. That is correct on a first run and catastrophic on a
    second: `/init` once with a key, then once offline (or any automatic refresh
    after Agent2 changes a file) would replace four sentences of real description
    with `_Not established._`, report it as an ordinary `updated` section, and leave
    the user no way to notice. Recovering the prior narrative here means a run that
    learns nothing new *says* nothing new.

    ⚠️ Only a **marked** section is recovered. An unmarked one belongs to the user
    and the marker rule already preserves it verbatim; reading it here as well would
    feed the user's own prose back through `generate()` as though Agent2 wrote it.
    A placeholder body recovers as empty — the absence of a narrative is not a
    narrative worth carrying, and treating it as one would make the placeholder
    permanent even after a model became reachable.
    """
    out: dict = {"purpose": "", "features": [], "architecture": "", "dataflow": ""}
    if not text:
        return out
    try:
        _pre, blocks = parse(text)
    except Exception:
        return out
    by = {b.heading: b for b in blocks if b.generated}

    if pb := by.get("Purpose"):
        # Everything except the user's own recorded hint line — that one is rebuilt
        # from the live `hint`, so carrying it too would pin a corrected description.
        prose = [ln.strip() for ln in pb.lines
                 if ln.strip() and not ln.strip().startswith(HINT_PREFIX)]
        joined = " ".join(prose).strip()
        if joined and joined not in _PLACEHOLDERS:
            out["purpose"] = joined

    if fb := by.get("Features"):
        feats: list[str] = []
        for ln in fb.lines:
            s = ln.strip()
            if not s or s in _PLACEHOLDERS:
                continue
            feats.append(s[2:].strip() if s.startswith("- ") else s)
        out["features"] = feats

    if ab := by.get("Architecture"):
        # The lead paragraph only. The `- \`dir/\` — N file(s)` rows below it are
        # SCAN FACTS and must keep refreshing; carrying them would freeze the layout
        # at whatever it was the last time a model happened to answer.
        lead: list[str] = []
        for ln in ab.lines:
            s = ln.strip()
            if s.startswith("- "):
                break
            if s:
                lead.append(s)
        joined = " ".join(lead).strip()
        if joined and joined not in _PLACEHOLDERS:
            out["architecture"] = joined

    if db := by.get("Data Flow"):
        # ⚠️ Recovered LINE BY LINE, not joined into one paragraph like the other
        # three. A flow is a sequence and the newlines are what make it readable as
        # one; flattening it here would degrade a traced path into a run-on sentence
        # a little more on every refresh that failed to reach a model.
        flow = [ln.rstrip() for ln in db.lines]
        while flow and not flow[0].strip():
            flow.pop(0)
        while flow and not flow[-1].strip():
            flow.pop()
        body = "\n".join(flow).strip()
        if body and body not in _PLACEHOLDERS:
            out["dataflow"] = body
    return out


def _carry_narrative(nar: dict, prior: dict, hint: str) -> dict:
    """Fill each narrated field THIS run did not establish from the prior doc.

    ⚠️ Per FIELD, not per run. A model that answers with a purpose but an empty
    feature list has not retracted the features it gave last time, so a thin reply
    must not be able to blank a section either. `purpose` counts as established only
    when it differs from `hint`: `narrate()` seeds it *with* the hint when no model
    answers, and `generate()` prints the hint on its own line, so "purpose == hint"
    means no model paragraph was obtained.
    """
    out = dict(nar or {})
    if not (out.get("purpose") and out.get("purpose") != hint) and prior.get("purpose"):
        out["purpose"] = prior["purpose"]
    if not out.get("features") and prior.get("features"):
        out["features"] = list(prior["features"])
    if not out.get("architecture") and prior.get("architecture"):
        out["architecture"] = prior["architecture"]
    if not out.get("dataflow") and prior.get("dataflow"):
        out["dataflow"] = prior["dataflow"]
    return out


def narrate(rep: dict, hint: str = "", existing: str = "", *, ask=None) -> dict:
    """Ask a model what this project is, why it exists and what it does.

    Returns `{"purpose", "features", "architecture", "dataflow", "source", "note"}`.
    `source` is `"model"` or `"evidence"` — ⚠️ and a caller must be able to tell
    them apart, because "we asked and it said this" and "no model was reachable so
    here are the file counts" are different claims about the same document.

    `ask` is injectable so the merge can be tested without a network; production
    passes nothing and gets `llm.capabilities.ask_one_shot`.
    """
    out = {"purpose": "", "features": [], "architecture": "", "dataflow": "",
           "source": "evidence", "note": ""}

    if hint:
        # ⚠️ Recorded even when no model answers. The user's sentence is the one
        # piece of "why does this exist" that never needed a model.
        out["purpose"] = hint
    if ask is None:
        try:
            from agent2.llm.capabilities import ask_one_shot as ask
        except Exception as exc:
            out["note"] = f"model unavailable: {type(exc).__name__}"
            return out
    try:
        reply = ask(_NARRATE_PROMPT % _evidence(rep, hint, existing))
    except Exception as exc:
        out["note"] = f"model failed: {type(exc).__name__}"
        return out
    if not str(reply or "").strip():
        out["note"] = NARRATE_TIMEOUT_NOTE
        return out
    data = _extract_json(reply)
    if not data:
        out["note"] = "the model's reply was not usable"
        return out

    purpose = " ".join(str(data.get("purpose") or "").split())
    arch = " ".join(str(data.get("architecture") or "").split())
    feats = [" ".join(str(f).split()) for f in (data.get("features") or [])
             if str(f or "").strip()]
    # ⚠️ `dataflow` keeps its LINE BREAKS while the other three are whitespace-
    # collapsed, because a flow is a sequence and the newlines are the sequence. It
    # is still bounded and stripped per line; a model that answered with a list gets
    # a list, one that answered with a paragraph gets a paragraph.
    flow = "\n".join(" ".join(ln.split()) for ln
                     in str(data.get("dataflow") or "").splitlines()
                     if ln.strip())[:DATAFLOW_CHARS].strip()
    if purpose:
        # ⚠️ The hint is not replaced, it is kept alongside — see `generate()`.
        out["purpose"] = purpose
    out["architecture"] = arch
    out["features"] = feats[:MAX_FEATURES]
    out["dataflow"] = flow
    out["source"] = "model" if (purpose or arch or feats or flow) else "evidence"
    if out["source"] == "evidence":
        out["note"] = "the model returned nothing usable"
    return out


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a reply. ⚠️ Same tolerance, same reason as
    `llm.capabilities._extract_json`: models wrap JSON in fences and prose despite
    being told not to, and failing over a stray "Here you go:" would silently
    downgrade the whole description to file counts."""
    raw = str(text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        import json
        data = json.loads(raw[start:end + 1])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# ── Generating the sections ───────────────────────────────────────────────────
def _cell(value) -> str:
    """One markdown table cell. ⚠️ A `|` in the text is escaped, not dropped.

    `summary` and `why` come from a scan of somebody else's code, so a docstring
    holding `read|write` or a shell pipe is ordinary. An unescaped one splits the
    row: the table still renders, the columns silently shift, and `## Important
    Files` starts attributing one file's description to the next file's `why` — a
    wrong answer with no error, in a file every later turn reads as fact. Newlines
    go the same way, since a cell cannot hold one at all.
    """
    return " ".join(str(value or "").split()).replace("|", "\\|")


def _fmt_commands(rep: dict, kinds: tuple[str, ...]) -> list[str]:
    cmds = rep.get("commands") or {}
    out: list[str] = []
    for kind in kinds:
        for c in cmds.get(kind) or []:
            cmd = str(c.get("cmd") or "").strip()
            if cmd:
                out.append(f"- `{cmd}`  — {c.get('from') or kind}")
    return out


def generate(rep: dict, *, hint: str = "", narrative: dict | None = None) -> dict[str, list[str]]:
    """The generated body of each section Agent2 owns, from one scan result.

    ⚠️ It states only what the scan actually found, and says so when it found
    nothing. A generated `## Test Commands` that guesses `pytest tests/` is worse
    than an empty one: the next turn reads this file as fact and runs the guess.

    `narrative` is `narrate()`'s answer — the four sections a scan cannot write.
    When it is absent or empty they still appear, saying what is missing and how to
    fill it, because a doc with no `## Purpose` reads as "this project has none".
    """
    rep = rep or {}
    nar = narrative or {}
    langs = rep.get("languages") or []
    fws = rep.get("frameworks") or []
    mgrs = rep.get("package_managers") or []
    tests = rep.get("tests") or {}
    build = rep.get("build") or {}
    git = rep.get("git") or {}
    out: dict[str, list[str]] = {}

    # ── How To Work Here ──
    # ⚠️ IT REFERENCES SECTIONS AND RESTATES NO FACT FROM ONE. Naming the test
    # command here would put a second copy of it in the same file as `## Test
    # Commands`, and the copy nobody edits is the one an agent would keep running
    # after the real command changed — the one-declaration rule, applied inside a
    # document. Every bullet below is either navigation or the precedence rule,
    # which is the one thing no other section can state.
    out["How To Work Here"] = [
        f"**{rep.get('name') or 'This project'}** — read this file before you act. "
        "It is this project's own instructions, and every later turn is given it.",
        "",
        "- A section **without** the `agent2:generated` marker under its heading was "
        "written by a human here. It **outranks** anything generated, including this "
        "section. Delete a marker to take that section over for good.",
        "- `_Not established…_` means **nobody has established it yet** — not that "
        "the project has none. Do not fill the gap with a guess, and do not act as "
        "though the answer is \"no\".",
        "- Where things are: `## Project Structure`. What each part is for: "
        "`## Important Files`. How a request moves: `## Data Flow`.",
        "- How to install, test and run: the three command sections below. ⚠️ Run "
        "only what is written there — if a command you need is missing, ask rather "
        "than inventing one.",
        "- Standing orders live in `## Agent2 Instructions`; rules that must not "
        "break live in `## Invariants`. Both are the user's, and both win.",
        "- After a **structural** change — a new module, entry point, command or "
        "dependency — refresh this file with the `update_project_doc` tool. Not "
        "after a read, a one-line edit or a question: a doc that churns every turn "
        "stops being read.",
    ]

    # ── Overview ──
    ov: list[str] = []
    prim = rep.get("primary_language") or ""
    ov.append(f"- **Name**: {rep.get('name') or '?'}")
    ov.append(f"- **Primary language**: {prim or 'not recognised'}")
    ov.append(f"- **Size**: {rep.get('files', 0):,} file(s) in "
              f"{rep.get('dirs', 0):,} directory(ies)")
    if mgrs:
        ov.append("- **Installed with**: " + ", ".join(str(m.get("label")) for m in mgrs))
    if git.get("repo") and not git.get("unknown"):
        ov.append(f"- **Git**: {git.get('describe') or 'clean'}")
    elif git.get("repo"):
        ov.append("- **Git**: repository detected, state unknown")
    else:
        ov.append("- **Git**: not a git repository")
    if rep.get("readme"):
        ov.append(f"- **Read first**: `{rep['readme']}`")
    out["Overview"] = ov

    # ── Purpose · Features · Architecture · Data Flow: the narrated four ──
    # ⚠️ THE USER'S OWN SENTENCE IS PRINTED SEPARATELY AND FIRST, never merged into
    # the model's paragraph. That line is `prior_hint()`'s anchor — a later `/init`
    # with no argument reads it back out of this file — and it is also the one claim
    # in the section that did not come from a guess.
    purpose: list[str] = []
    if hint:
        purpose.append(f"{HINT_PREFIX} {hint}")
        purpose.append("")
    model_purpose = str(nar.get("purpose") or "")
    if model_purpose and model_purpose != hint:
        purpose.append(model_purpose)
    elif not hint:
        purpose.append(NO_PURPOSE)
    out["Purpose"] = purpose

    feats = list(nar.get("features") or [])
    out["Features"] = [f"- {f}" for f in feats] or [NO_FEATURES]

    arch_text = str(nar.get("architecture") or "")
    arch: list[str] = []
    if arch_text:
        arch.append(arch_text)
        arch.append("")
    else:
        arch.append(NO_ARCHITECTURE)
        arch.append("")
    arch.extend(f"- `{d.get('path')}/` — {d.get('files')} file(s)"
                for d in (rep.get("structure") or [])[:10])
    if not (rep.get("structure") or []):
        arch.append("- Flat layout: every file is at the project root.")
    out["Architecture"] = arch

    # ⚠️ NARRATIVE ONLY — no directory rows, no entry-point list. `## Architecture`
    # above already prints the layout and `## Entry Points` below already names the
    # starts; padding this section with either would make it a third copy of a fact
    # the doc states twice, and leave the reader unable to tell a traced flow from a
    # restated listing. When nobody has traced it, the honest body is one line
    # saying so.
    flow_text = str(nar.get("dataflow") or "")
    out["Data Flow"] = flow_text.splitlines() if flow_text else [NO_DATAFLOW]

    # ── Tech Stack ──
    ts: list[str] = [f"- {lang.get('name')} — {lang.get('files')} file(s) "
                     f"({lang.get('share', 0)}%)" for lang in langs[:8]]
    if fws:
        ts.append("")
        ts.extend(f"- **{f.get('name')}** ({f.get('evidence')})" for f in fws)
    if not ts:
        ts.append("_No recognised languages or frameworks._")
    out["Tech Stack"] = ts

    # ── Project Structure ──
    st: list[str] = ["```", f"{rep.get('name') or '.'}/"]
    st.extend(f"  {d.get('path')}/    ({d.get('files')} files)"
              for d in (rep.get("structure") or [])[:14])
    st.append("```")
    out["Project Structure"] = st

    # ── Important Files ──
    imp: list[str] = [f"- `{doc}` — project documentation"
                      for doc in (rep.get("docs") or [])]
    imp.extend(f"- `{cf}` — configuration"
               for cf in (rep.get("config_files") or [])[:10])
    # ⚠️ WHAT THE FILE SAYS ABOUT ITSELF, NEVER WHAT IT CONTAINS. `file_notes`
    # holds the leading docstring or comment `projectscan` extracted — the author's
    # own answer to "what is this module" — so this section can tell the next turn
    # which code to open first and why, without pasting a line of it. That is the
    # half of Task 31 a census cannot write: `docs` and `config_files` above are
    # names, and a name does not tell an agent where the work lives.
    # ⚠️ A TABLE, AND THE THIRD COLUMN IS THE POINT. `why` is the scan's own reason
    # for having opened the file ("entry point", "package root", "largest module") —
    # the fact a flat bullet list drops, and the one that tells a reader whether a
    # file is central or merely large. `summary` is the scan's field, never
    # re-derived here (see `projectscan.NOTE_SUMMARY_CHARS`).
    notes = rep.get("file_notes") or []
    if notes:
        if imp:
            imp.append("")
        imp.append("| File | What it says about itself | Why it was read |")
        imp.append("|------|---------------------------|-----------------|")
        imp.extend("| `{}` | {} | {} |".format(_cell(n.get("path")),
                                               _cell(n.get("summary")),
                                               _cell(n.get("why")))
                   for n in notes[:MAX_DOC_NOTES])
    if not imp:
        imp.append("_None recognised._")
    out["Important Files"] = imp


    # ── Entry Points ──
    eps = rep.get("entry_points") or []
    out["Entry Points"] = [f"- `{e.get('path')}` — {e.get('why')}" for e in eps] or \
        ["_None found. Add the real entry point here and delete the marker above._"]

    # ── The three command sections ──
    out["Build Commands"] = _fmt_commands(rep, ("install", "build")) or \
        ["_No build or install command was declared by this project._"]
    tc = _fmt_commands(rep, ("test",))
    if not tc:
        if tests.get("files"):
            tc = [f"_{tests['files']} test file(s) found in "
                  f"`{(tests.get('dirs') or ['?'])[0]}`, but no runner is declared — "
                  "add the command you actually use._"]
        else:
            tc = ["_No tests were found._"]
    out["Test Commands"] = tc
    out["Development Commands"] = _fmt_commands(rep, ("dev", "lint", "other")) or \
        ["_None declared._"]

    # ── Configuration ──
    cfg: list[str] = [f"- `{cf}`" for cf in (rep.get("config_files") or [])]
    if build.get("systems"):
        cfg.append(f"- Build system(s): {', '.join(build['systems'])}")
    if tests.get("runners"):
        cfg.append(f"- Test runner(s): {', '.join(tests['runners'])}")
    # ⚠️ Named, never read. `/init` opens only the manifest list in
    # `projectscan.MANIFEST_FILES`, so no value from a `.env` can reach this file.
    if any(c.startswith(".env") for c in (rep.get("config_files") or [])):
        cfg.append("- An `.env` example is present; real secrets are **not** recorded here.")
    out["Configuration"] = cfg or ["_No configuration files recognised at the root._"]

    # ── Important Conventions ──
    out["Important Conventions"] = [f"- {c}" for c in (rep.get("conventions") or [])] or \
        ["_None observed._"]
    # ⚠️ CANONICAL FORM, AND IT IS THE SAME ONE `parse()` PRODUCES: no leading and
    # no trailing blank lines. `render()` compares a generated body against a parsed
    # one to decide "updated" vs "unchanged", so the two must agree on padding or the
    # comparison answers a question nobody asked. It did not: `## Purpose` ends with a
    # blank line whenever a hint is present, `parse()` strips it on the way back in,
    # so `new != b.lines` was permanently true on padding alone and EVERY later
    # `/init` reported "Sections refreshed: Purpose" while writing byte-identical
    # content — `changed=False` and `updated=["Purpose"]` in one result. Normalising
    # here rather than in `render()` keeps it a property of the body itself, so a
    # section added by a later phase cannot reintroduce the mismatch.
    return {k: _body(v) for k, v in out.items()}


def render(rep: dict, existing: str = "", *, hint: str = "",
           narrative: dict | None = None) -> tuple[str, dict]:
    """Build the new `agent2.md` text, plus what happened to each section.

    ⚠️ The merge, not a rewrite. `existing` is parsed first and every unmarked
    block survives verbatim and in its own position; only marked blocks are
    regenerated, and only generated headings that are missing get inserted.
    """
    gen = generate(rep, hint=hint, narrative=narrative)
    pre, blocks = parse(existing)
    added: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    preserved: list[str] = []

    seen: dict[str, _Block] = {}
    for b in blocks:
        seen.setdefault(b.heading, b)

    for b in blocks:
        if b.heading in gen and b.generated:
            new = list(gen[b.heading])
            if new != b.lines:
                updated.append(b.heading)
            else:
                unchanged.append(b.heading)
            b.lines = new
        else:
            # Unmarked, or a heading Agent2 does not generate. Either way: theirs.
            preserved.append(b.heading)

    order = {name: i for i, name in enumerate(SECTIONS)}

    def insert_at(name: str) -> int:
        """Just after the last existing block that precedes `name` canonically.

        ⚠️ `pos` STARTS AT 0, NOT AT `len(blocks)`. A section whose canonical rank is
        0 has nothing that precedes it, so no block can ever satisfy `rank < want`
        and the loop body never runs — a `len(blocks)` default would then append the
        very first section to the very END of the document. It was invisible while
        `## Overview` held rank 0 and no real doc lacked it; `## How To Work Here`
        made it live, and the symptom would have been the file's own reading
        instructions arriving after everything they explain, with no error.

        A heading Agent2 does not generate keeps rank `len(SECTIONS) + 1`, so it
        never advances `pos` for anything: the canonical spine stays in canonical
        order and a user's invented sections stay where the user put them.
        """
        want = order.get(name, len(SECTIONS))
        pos = 0
        for i, b in enumerate(blocks):
            rank = order.get(b.heading, len(SECTIONS) + 1)
            if rank < want:
                pos = i + 1
        return pos

    for name in SECTIONS:
        if name in seen:
            continue
        if name in HUMAN_SECTIONS:
            # Seeded UNMARKED — see the module docstring. `generated=False` here is
            # the single most load-bearing line in this file.
            body = list(_SEEDS.get(name, ("- ",)))
            blocks.insert(insert_at(name), _Block(name, body, False))
        elif name in gen:
            blocks.insert(insert_at(name), _Block(name, list(gen[name]), True))
        else:
            continue
        added.append(name)

    # Preamble: regenerated only when it is ours (or absent).
    stamp = time.strftime("%Y-%m-%d", time.localtime(rep.get("at") or time.time()))
    ours = (not [ln for ln in pre if ln.strip()]) or any(_MARKER_RE.match(ln) for ln in pre)
    if ours:
        pre = [
            "# Project",
            MARKER,
            f"_{rep.get('name') or 'This project'} — written by Agent2 `/init` on "
            f"{stamp}. Sections marked `agent2:generated` are refreshed by the next "
            "`/init`; delete a marker to make that section yours._",
        ]
    else:
        pre = list(pre)
        while pre and not pre[-1].strip():
            pre.pop()

    parts: list[str] = ["\n".join(pre).rstrip(), ""]
    for b in blocks:
        parts.extend(_block_lines(b))
    text = "\n".join(parts).rstrip() + "\n"
    return text, {"added": added, "updated": updated,
                  "unchanged": unchanged, "preserved": preserved}


# ── What of the doc reaches a prompt ──────────────────────────────────────────
def _block_lines(b: _Block) -> list[str]:
    """One section as it appears on disk. ⚠️ ONE SPELLING OF THE ON-DISK SHAPE.

    `render()` writes the file and `for_prompt()` re-assembles a subset of it, so
    both ask here. A second copy of "heading, then the marker if ours, then a blank,
    then the body" drifts the moment the format changes — and the drift would be a
    clipped prompt whose sections no longer carry the marker that decides ownership,
    which is the one fact the agent reading them needs most.
    """
    out = [f"## {b.heading}"]
    if b.generated:
        out.append(MARKER)
    out.append("")
    out.extend(b.lines)
    out.append("")
    return out


def for_prompt(text: str, limit: int) -> tuple[str, dict]:
    """The part of `agent2.md` that fits in a prompt, and what was left out.

    ⚠️ A CLIP MAY NEVER DROP A SECTION A HUMAN OWNS. `broker.PROJECT_DOC_CHARS` owns
    *how much* of the doc a prompt may spend; this function owns *which part*,
    because "who owns this section" is this module's fact and nowhere else's. The
    slice it replaced was `body[:limit]` — a head-first cut, and a document ends with
    the sections a human took over, so the first thing the prompt discarded was the
    user's own standing orders. Silently, and only once the doc grew useful.

    So: the preamble and every **unmarked** block are kept whole, and generated
    blocks are surrendered in `CLIP_ORDER` — most expendable first — until the rest
    fits.

    ⚠️ SURVIVAL ORDER IS NOT PRINT ORDER, and the disagreement is deliberate — the
    same one `core/broker/sources.py` documents between its `ORDER` and its
    `PRIORITY`. `SECTIONS` decides where a section appears for a human reading the
    file; `CLIP_ORDER` decides what an agent keeps when it cannot have all of it, and
    those are different questions. Deriving one from the other means either the file
    reads in a useless order or the prompt gives up this project's test command to
    keep a directory tree the agent could have listed itself.

    ⚠️ IF THE UNMARKED SECTIONS ALONE EXCEED THE LIMIT THEY ARE KEPT ANYWAY and
    `over` is set — `budget.py`'s rule about pinned items, for its reason. An
    honestly over-budget prompt fails at the vendor with something an operator can
    read; a quiet trim of a user's rules is unrecoverable and invisible.

    Returns `(text, info)` where `info` carries `clipped`, `over`, `kept` and
    `dropped` (headings, both in document order) and `chars`. ⚠️ It does NOT carry
    the notice: the wording names `broker.PRIMARY_DOC`, which is the caller's
    constant, so the caller composes it from `dropped`.

    ⚠️ Total, and it fails OPEN to the old behaviour. It runs inside the broker's
    per-collector guard, but an accounting helper that raised would cost the turn the
    entire project source — the one thing `/init` exists to put in the prompt — so an
    unparseable doc degrades to a plain head clip rather than to nothing.
    """
    body = (text or "").strip()
    info: dict = {"clipped": False, "over": False, "sections": 0,
                  "kept": [], "dropped": [], "chars": len(body), "limit": int(limit or 0)}
    lim = info["limit"]
    if lim <= 0 or len(body) <= lim:
        return body, info
    info["clipped"] = True
    try:
        pre, blocks = parse(body)
    except Exception:
        blocks = []
    if not blocks:
        # No headings to reason about — a head clip is the only honest answer, and it
        # is exactly what this function replaced. ⚠️ Reachable: a user may keep their
        # whole doc as unsectioned prose, and a truncated read has no headings either.
        out = body[:lim].rstrip()
        info["chars"] = len(out)
        return out, info

    head = "\n".join(pre).rstrip()
    chunks = [(b, "\n".join(_block_lines(b)).rstrip()) for b in blocks]
    info["sections"] = len(chunks)

    # Start from "keep everything" and surrender, rather than "keep nothing" and
    # admit. ⚠️ That direction is what makes the ranking mean anything: admitting in
    # rank order would let one oversized cheap section be skipped while a costlier
    # valuable one below it still fitted, so the doc a prompt got would depend on
    # section *sizes* rather than on what the sections are worth.
    keep = set(range(len(chunks)))
    used = len(head) + 2 + sum(len(c) + 2 for _b, c in chunks)
    give = sorted((i for i, (b, _c) in enumerate(chunks) if b.generated),
                  key=lambda i: (_clip_rank(chunks[i][0].heading), -i))
    for i in give:
        if used <= lim:
            break
        keep.discard(i)
        used -= len(chunks[i][1]) + 2
    if used > lim:
        # Only the unmarked sections are left and they still do not fit. Keep them.
        info["over"] = True

    parts: list[str] = [head, ""]
    for i, (_b, chunk) in enumerate(chunks):
        if i in keep:
            parts.append(chunk)
            parts.append("")
    out = "\n".join(parts).rstrip() + "\n"
    info["kept"] = [b.heading for i, (b, _c) in enumerate(chunks) if i in keep]
    info["dropped"] = [b.heading for i, (b, _c) in enumerate(chunks) if i not in keep]
    info["chars"] = len(out)
    return out, info


# ── The write ─────────────────────────────────────────────────────────────────
def _atomic_write(path: Path, text: str) -> None:
    """⚠️ Temp-then-replace: an interrupted `/init` may not truncate the doc."""
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


_SKILLS_README = """# Skills

Drop one directory per skill here — Agent2 discovers them recursively:

    .agent2/skills/security-audit/SKILL.md

`SKILL.md` carries the instructions; any supporting files live beside it. Skills
written for Claude, Codex or Antigravity are read as-is — Agent2 normalizes them
on load and never modifies a skill file.

Enable and disable them with `/skills` (per project).
"""


def apply(report: dict, root=None, *, write: bool = True, hint: str = "",
          describe: bool = True, ask=None) -> dict:
    """Create or update `.agent2/` from a scan. The one writer. Never raises.

    ⚠️ IT ASKS `core.permissions` FOR `fs.write`, AND IT REFUSES BY RETURNING.
    `/init` is a filesystem write like any other, so `AGENT2_DENY_CAPS=fs.write`
    must stop it — and a refusal is a `reason` in the result rather than an
    exception, because both callers are printing surfaces.

    ⚠️ THE SECOND RUN READS THE FIRST ONE'S FILE BEFORE IT WRITES. `existing` is
    loaded, the user's sections are parsed out of it, and `prior_hint()` recovers
    the description they gave the first time — so `/init` with no argument, run a
    year later, still knows what the project is and only refreshes what changed.

    `hint` is the user's own `/init <description>`; it outranks the model's guess
    and is recorded in the doc. `describe=False` skips the model call (and so does
    a missing key — see `narrate()`); `write=False` performs the whole merge and
    reports what *would* change, touching nothing, which is what a dry run uses and
    what makes the merge testable without a filesystem.
    """
    out = empty_result()
    out["at"] = time.time()
    rep = report or {}
    # ⚠️ `Path("")` IS `Path(".")`, AND `.` IS WHEREVER THE PROCESS HAPPENS TO BE.
    # A report with no root must refuse rather than write `.agent2/agent2.md` into
    # the current working directory: that doc describes a project nobody named, it
    # lands outside the sandbox the workspace defines, and in dual mode the CWD is
    # not even the same directory in both halves. It read as a successful `/init`.
    raw = str(root) if root else str(rep.get("root") or "")
    if not raw.strip():
        out["reason"] = "no workspace root"
        return out
    try:
        base = Path(raw)
        is_dir = base.is_dir()
    except Exception:
        out["reason"] = "unreadable workspace root"
        return out
    out["root"] = str(base)
    if not is_dir:
        # `mkdir(parents=True)` would happily materialise the whole phantom tree.
        out["reason"] = "workspace root is not a directory"
        return out

    if refusal := _refuse_root(base):
        out["reason"] = f"refusing to write here: {refusal}"
        return out

    if write:
        try:
            from agent2.core import permissions as _perms
            if not _perms.process_allows(_perms.CAP_FS_WRITE):
                out["reason"] = ("not permitted: fs.write is denied for this process "
                                 "(AGENT2_DENY_CAPS)")
                return out
        except Exception as exc:
            # ⚠️ FAILS CLOSED. Everywhere else in this module a broken helper
            # degrades to doing less; here it degrades to doing NOTHING, because
            # the helper being consulted is the one that says whether writing is
            # allowed at all.
            out["reason"] = f"permission check unavailable: {type(exc).__name__}"
            return out

    adir = doc_dir(base)
    doc = doc_path(base)
    out["dir"], out["doc"] = str(adir), str(doc)

    existing = ""
    try:
        out["existed"] = doc.is_file()
        if out["existed"]:
            existing = doc.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        out["errors"].append(f"read: {type(exc).__name__}: {exc}")

    # A hint given now wins; otherwise the one this project already recorded.
    hint = " ".join(str(hint or "").split()) or prior_hint(existing)
    out["hint"] = hint

    nar: dict = {}
    if describe:
        nar = narrate(rep, hint, existing, ask=ask)
        out["described"] = nar.get("source") == "model"
        out["describe_note"] = str(nar.get("note") or "")
        out["features"] = len(nar.get("features") or [])

    # ⚠️ CARRY FORWARD WHAT THIS RUN DID NOT ESTABLISH. A refresh with no model
    # (offline, no key, or an automatic re-`apply()` after Agent2 changed a file)
    # otherwise regenerates the narrated three into "_Not established._" and reports
    # it as an ordinary update — silently destroying the description the doc's whole
    # purpose is to hold. `prior_narrative()` reads the last real answer back out of
    # the file so a run that learns nothing new writes nothing new. Facts (the scan
    # counts, the structure rows) still refresh; only the prose a model produced is
    # preserved when no fresher prose replaced it.
    prior = prior_narrative(existing)
    carried = _carry_narrative(nar, prior, hint)
    # ⚠️ Reported only when the merge ACTUALLY took something from the old file. A
    # flag set from "the old file had prose" would read as "we reused it" on exactly
    # the runs where a fresh model answer replaced every field.
    out["narrative_carried"] = sorted(
        k for k in NARRATED_FIELDS
        if carried.get(k) and carried.get(k) != nar.get(k))
    nar = carried
    if not out["features"]:
        out["features"] = len(nar.get("features") or [])

    try:
        text, merged = render(rep, existing, hint=hint, narrative=nar)
    except Exception as exc:
        out["reason"] = f"could not build the document: {type(exc).__name__}: {exc}"
        return out
    out.update({k: merged[k] for k in ("added", "updated", "unchanged", "preserved")})
    out["bytes"] = len(text.encode("utf-8"))
    # ⚠️ An identical rewrite is not a write. Running `/init` twice must not dirty
    # the tree, bump an mtime or show up in a diff review as a change nobody made.
    out["changed"] = (text != existing)
    out["created"] = not out["existed"]

    if not write:
        out["ok"] = True
        out["reason"] = "dry run"
        return out

    try:
        if not adir.is_dir():
            adir.mkdir(parents=True, exist_ok=True)
            out["dirs_created"].append(".agent2")
        skills = skills_dir(base)
        if not skills.is_dir():
            skills.mkdir(parents=True, exist_ok=True)
            out["dirs_created"].append(f".agent2/{SKILLS_DIR}")
            # Written only for a brand-new directory: a user who emptied it meant to.
            readme = skills / "README.md"
            if not readme.exists():
                _atomic_write(readme, _SKILLS_README)
                out["files_created"].append(f".agent2/{SKILLS_DIR}/README.md")
        if out["changed"]:
            _atomic_write(doc, text)
            if out["created"]:
                out["files_created"].append(f".agent2/{DOC_REL}")
        out["ok"] = True
    except Exception as exc:
        out["reason"] = f"write failed: {type(exc).__name__}: {exc}"
        return out

    # The doc and the skills dir are new files in the tree, so the cached `git`
    # snapshot taken during the scan is now wrong. ⚠️ The READER never invalidates
    # (`projectscan` must not); the WRITER does, or the status bar reports a clean
    # tree for up to `gitstate.GIT_TTL` after `/init` created a file.
    if out["changed"] or out["dirs_created"]:
        try:
            from agent2.core import gitstate as _git
            _git.invalidate()
        except Exception:
            pass
    try:
        from agent2.core import logging as alog
        alog.project_doc_written(
            doc=str(doc), created=out["created"], changed=out["changed"],
            added=len(out["added"]), updated=len(out["updated"]),
            preserved=len(out["preserved"]),
        )
    except Exception:
        pass
    return out
