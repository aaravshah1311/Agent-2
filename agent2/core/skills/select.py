# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.skills.select
─────────────────────────
WHICH skills reach this turn's prompt, in WHICH order, and WHY — Tasks 34 and 36
together, because they are one decision described from two sides.

Task 34's bar: *not every skill in every prompt — selection by task, request,
project, metadata, explicit choice.*
Task 36's bar: *deterministic order: request → `agent2.md` → enabled →
auto-relevant → general*, and *report which skills applied.*

⚠️ **THAT ORDER IS THE ONE DECLARATION, AND IT IS `REASONS`.** Every ranking,
every cap and every report indexes into the same tuple. A second ordering — a sort
in the renderer, a "priority" the CLI applies on top — is the drift this codebase
is shaped against: the prompt would then contain one selection while `/skills`
described another, and both halves would look right alone.

⚠️ **A SKILL TAKES ITS EARLIEST MATCHING REASON, NOT ALL OF THEM.** A skill that
is named in the request *and* enabled *and* relevant applies once, as `request`.
Reasons are a rank, not a score to be summed: adding them up would let three weak
signals outrank one explicit ask, which is precisely backwards — the user typing
the skill's name is the strongest statement of intent this system can receive.

⚠️ **AN EXPLICIT `off` BEATS EVERY OTHER SIGNAL, INCLUDING THE REQUEST.** A user
who disabled a skill here has made a standing decision about this project, and a
word in a message is not a revocation of it — it is far more often a coincidence
of vocabulary. It is reported as omitted-because-disabled rather than dropped
silently, so a user who did mean it can see why nothing happened and type
`/skills on <name>`. The opposite choice (a mention re-enables) is the one that
cannot be diagnosed from the transcript.

⚠️ **`always: true` IS THE *GENERAL* TIER — THE LOWEST — AND MAY BE DISPLACED.**
A skill file asking to be in every prompt is a request from a *file*, and Task 34's
bar is a ceiling on the whole selection, not on the un-pinned part of it. So
`always` outranks nothing and, past `config.SKILLS_IN_PROMPT`, it is omitted and
**said to be omitted**. The alternative — honouring every `always` before the cap —
means a folder with five pinned skills is a folder where the cap does nothing, and
one bad skill file could evict the request the user actually made.

⚠️ **DETERMINISM IS TESTABLE BECAUSE THIS MODULE IS PURE.** `choose()` is a
function of (catalog, message, states) and reads no clock, no environment beyond the
declared ceilings, no database and no filesystem — the catalog already carries the
project's `agent2.md` mentions (`discovery._mentions`) precisely so that this stays
true. Two runs with the same three inputs produce the same list in the same order,
including every tie-break, which is what "deterministic order" has to mean if it is
to be checked rather than asserted.

Nothing here raises: a selection that cannot be computed is an empty one, and the
prompt is then exactly what it was before Phase 11.
"""

import re
from dataclasses import dataclass, field

from agent2 import config
from agent2.core.skills.discovery import Catalog, Skill

# ── The one ordering ───────────────────────────────────────────────────────────
# ⚠️ Task 36's acceptance bar, verbatim, as data. Index in this tuple IS rank.
REASON_REQUEST = "request"      # the message names the skill
REASON_PROJECT = "project"      # `.agent2/agent2.md` names it
REASON_ENABLED = "enabled"      # the user switched it on in this project
REASON_RELEVANT = "relevant"    # its own declared keywords match the message
REASON_GENERAL = "general"      # `always: true`

REASONS: tuple[str, ...] = (REASON_REQUEST, REASON_PROJECT, REASON_ENABLED,
                            REASON_RELEVANT, REASON_GENERAL)

#: What each reason is called on screen and in the prompt. Presentation only —
#: never parsed back, so a translation could not break selection.
REASON_LABEL: dict[str, str] = {
    REASON_REQUEST: "named in the request",
    REASON_PROJECT: "named in agent2.md",
    REASON_ENABLED: "enabled for this project",
    REASON_RELEVANT: "matches this request",
    REASON_GENERAL: "always on",
}

# Why a skill did NOT reach the prompt. Reported, never inferred by a surface.
OUT_DISABLED = "disabled"       # switched off here; outranks every other signal
OUT_SHADOWED = "shadowed"       # another skill claims the same name
OUT_CAP = "cap"                 # past SKILLS_IN_PROMPT
OUT_CHARS = "chars"             # past SKILLS_MAX_CHARS
OUT_EMPTY = "empty"             # nothing to say

#: Score a skill needs before its own keywords count as "matches this request".
#: Two rather than one: a single shared word with a *description* is a coincidence
#: (`file`, `test`, `code`), and admitting it would make the relevant tier fire on
#: every turn — which is Task 34's failure mode wearing selection's clothes.
RELEVANT_MIN = 2

# Words that carry no topic. Deliberately short: this is a tie-break aid, not a
# language model, and a long list starts deciding what a user may not name a skill.
_STOP = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "for", "with", "without",
    "this", "that", "these", "those", "there", "here", "from", "into", "onto",
    "you", "your", "our", "its", "it", "is", "are", "was", "were", "be", "been",
    "do", "does", "did", "can", "will", "would", "should", "could", "please",
    "me", "my", "we", "us", "to", "of", "in", "on", "at", "by", "as", "not",
    "all", "any", "some", "how", "what", "why", "when", "which", "who",
    "file", "files", "code", "project", "make", "let", "just", "now", "also",
    "skill", "skills", "use", "using", "used", "run", "add", "new", "get",
})

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+#.-]*")
_MIN_TOKEN = 3


@dataclass
class Applied:
    """One skill that reached the prompt, and the reason it did."""

    skill: Skill
    reason: str
    score: int = 0
    chars: int = 0

    def to_payload(self) -> dict:
        return {
            "id": self.skill.id,
            "name": self.skill.name,
            "reason": self.reason,
            "label": REASON_LABEL.get(self.reason, self.reason),
            "score": self.score,
            "chars": self.chars,
            "origin": self.skill.origin,
            "priority": self.skill.priority,
        }


@dataclass
class Selection:
    """What was chosen, what was not, and why — the whole report in one object.

    ⚠️ `applied` and `omitted` are TWO HALVES OF ONE FACT and are produced by the
    same pass. Task 36 asks for "reporting which skills applied", and a report that
    could only name the winners is the one that cannot answer the question a user
    actually asks, which is why the skill they wrote did *not*.
    """

    applied: list[Applied] = field(default_factory=list)
    omitted: list[dict] = field(default_factory=list)
    considered: int = 0
    chars: int = 0
    limit: int = 0
    max_chars: int = 0
    catalog_truncated: bool = False
    truncated_by: str = ""
    errors: list[str] = field(default_factory=list)
    #: When the CATALOG was read (wall clock), carried through from `Catalog.at`.
    #: ⚠️ Not "when this selection was made" — selection is instant and free, and the
    #: fact the broker's `freshness` needs is the age of the tree read, which is
    #: cached for `discovery.SKILLS_TTL`. Copying it here is what lets the collector
    #: stamp its item honestly without this module reading a clock (see the module
    #: note on purity).
    at: float = 0.0

    @property
    def ids(self) -> list[str]:
        return [a.skill.id for a in self.applied]

    def to_payload(self) -> dict:
        return {
            "applied": [a.to_payload() for a in self.applied],
            "omitted": list(self.omitted),
            "considered": self.considered,
            "chars": self.chars,
            "limit": self.limit,
            "max_chars": self.max_chars,
            "catalog_truncated": self.catalog_truncated,
            "truncated_by": self.truncated_by,
            "errors": list(self.errors),
        }


# ── Relevance (declared metadata against the request) ──────────────────────────

def tokens(text: str) -> set[str]:
    """Topic words of *text*, lower-cased. The one tokenizer for this module."""
    return {w for w in _WORD_RE.findall(str(text or "").lower())
            if len(w) >= _MIN_TOKEN and w not in _STOP}


def _phrase_in(needle: str, haystack: str) -> bool:
    """Whole-phrase, word-boundary containment — not a substring test.

    ⚠️ `in` would make a skill named `xss` match the word `boxsscript`, and a skill
    named `go` match almost anything. A boundary check is the difference between
    "the user named this skill" and "these letters occurred".
    """
    n = str(needle or "").strip().lower()
    if len(n) < _MIN_TOKEN:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])",
                     str(haystack or "").lower()) is not None


def named_in(skill: Skill, text: str) -> bool:
    """Does *text* name this skill outright — by display name or by id?

    This is the `request` tier, and it is deliberately narrow: the name a user
    would type, and the path-derived id they would see in `/skills`. Keywords are
    NOT here — a keyword match is the skill's own claim about when it applies,
    which is the `relevant` tier, one rank lower.
    """
    if _phrase_in(skill.name, text):
        return True
    sid = skill.id
    if _phrase_in(sid, text):
        return True
    tail = sid.rsplit("/", 1)[-1]
    return bool(tail and tail != sid and _phrase_in(tail, text))


def relevance(skill: Skill, text: str) -> int:
    """How strongly this skill's OWN declared metadata matches *text*.

    ⚠️ Deliberately NOT `broker.sources.relevance_of`. That function measures an
    item's whole rendered text against the message, which is right for a git
    snapshot and wrong here: a 3 000-word skill would out-score a precisely
    triggered one-line skill on sheer surface area, so the *least* focused file in
    the folder would win every turn. Here only what the author *declared* counts —
    keywords first, then the name, then the description — so a skill's own claim
    about when it applies is what decides whether it does.

    Weights, in the order a human would defend them: a declared keyword appearing
    as a phrase is the author saying "this is my trigger" (3); a keyword or name
    token overlapping is weaker (2); the description is prose and counts once (1),
    capped, because a long description is not a stronger claim.
    """
    if not text:
        return 0
    words = tokens(text)
    if not words:
        return 0
    score = 0
    for kw in skill.keywords:
        if _phrase_in(kw, text):
            score += 3
        elif tokens(kw) & words:
            score += 2
    if tokens(skill.name) & words:
        score += 2
    if skill.description and (tokens(skill.description) & words):
        score += 1
    return score


# ── The decision ───────────────────────────────────────────────────────────────

def _reason_for(skill: Skill, message: str, doc: frozenset, chosen: bool | None,
                score: int) -> str:
    """The EARLIEST reason this skill applies, or `""`. The whole ranking rule.

    Order is `REASONS` and nothing else. `chosen is False` never reaches here —
    `choose()` removes an explicitly disabled skill before asking, so that "off
    beats everything" is true by construction rather than by the order of the
    branches below.
    """
    if named_in(skill, message):
        return REASON_REQUEST
    if skill.id in doc or (skill.name or "").strip().lower() in doc:
        return REASON_PROJECT
    if chosen is True:
        return REASON_ENABLED
    if score >= RELEVANT_MIN:
        return REASON_RELEVANT
    if skill.always:
        return REASON_GENERAL
    return ""


def _shadow_key(skill: Skill) -> str:
    """What two skills must share to be in conflict: their normalized NAME.

    Not the id — ids are paths and are unique by construction, so a conflict
    between `web/audit` and `api/audit` both called "audit" is exactly the case
    Task 36's "conflict resolution" is about. Two skills with one name in one
    prompt is two sets of instructions under one label, and the model has no way
    to tell which the user meant.
    """
    return re.sub(r"[^a-z0-9]+", "-", (skill.name or skill.id).strip().lower()).strip("-")


def choose(catalog: Catalog, message: str = "", states: dict | None = None, *,
           limit: int | None = None, max_chars: int | None = None) -> Selection:
    """THE selection. Pure, total, and deterministic in every tie-break.

    `states` is `{skill_id: bool}` — only skills the user has actually decided
    about (see `state.states()`); a missing key is *never chosen*, which is a third
    state and not a `False`.
    """
    cap = config.SKILLS_IN_PROMPT if limit is None else max(0, int(limit))
    char_cap = config.SKILLS_MAX_CHARS if max_chars is None else max(0, int(max_chars))
    sel = Selection(limit=cap, max_chars=char_cap,
                    catalog_truncated=bool(catalog.truncated),
                    truncated_by=str(catalog.truncated_by or ""),
                    errors=list(catalog.errors or []),
                    at=float(catalog.at or 0.0))
    skills = list(catalog.skills or [])
    sel.considered = len(skills)
    if not skills or not cap or not char_cap:
        return sel
    st = dict(states or {})
    doc = catalog.doc_mentions or frozenset()
    msg = str(message or "")

    candidates: list[Applied] = []
    for sk in skills:
        chosen = st.get(sk.id)
        if chosen is False:
            # ⚠️ Before any other signal is even computed — see the module note.
            # Only reported when something else WOULD have selected it, so a
            # folder of forty deliberately-off skills does not produce forty
            # lines of noise on every turn.
            score = relevance(sk, msg)
            if named_in(sk, msg) or score >= RELEVANT_MIN or sk.always or sk.id in doc:
                sel.omitted.append({"id": sk.id, "name": sk.name, "why": OUT_DISABLED})
            continue
        if not (sk.body or sk.description):
            sel.omitted.append({"id": sk.id, "name": sk.name, "why": OUT_EMPTY})
            continue
        score = relevance(sk, msg)
        reason = _reason_for(sk, msg, doc, chosen, score)
        if not reason:
            continue
        candidates.append(Applied(skill=sk, reason=reason, score=score))

    # ⚠️ ONE SORT, and every component of the key is deterministic. `-priority`
    # honours a skill file's own `priority:` **within** its tier and never across
    # one, because a file may not promote itself past the user's explicit ask.
    # `name`/`id` last so two otherwise-identical skills always order the same way
    # on every machine — the property "deterministic" is checked on.
    candidates.sort(key=lambda a: (REASONS.index(a.reason), -a.skill.priority,
                                   -a.score, a.skill.name.lower(), a.skill.id))

    # Conflict resolution: one name, one skill. The survivor is the shallower path
    # (a top-level skill is the project's, a nested one is a variant), then the
    # shorter id, then lexicographic — all three are ties broken by data, never by
    # discovery order, so it cannot depend on the filesystem.
    seen: dict[str, Applied] = {}
    ordered: list[Applied] = []
    for cand in candidates:
        key = _shadow_key(cand.skill)
        prev = seen.get(key)
        if prev is None:
            seen[key] = cand
            ordered.append(cand)
            continue
        mine = (cand.skill.depth, len(cand.skill.id), cand.skill.id)
        theirs = (prev.skill.depth, len(prev.skill.id), prev.skill.id)
        loser, winner = (cand, prev) if theirs <= mine else (prev, cand)
        if winner is cand:
            ordered[ordered.index(prev)] = cand
            seen[key] = cand
        sel.omitted.append({"id": loser.skill.id, "name": loser.skill.name,
                            "why": OUT_SHADOWED, "by": winner.skill.id})

    for cand in ordered:
        if len(sel.applied) >= cap:
            sel.omitted.append({"id": cand.skill.id, "name": cand.skill.name,
                                "why": OUT_CAP, "reason": cand.reason})
            continue
        text = render_one(cand)
        # ⚠️ SKIPPED, NOT A STOP SIGN — the same rule `broker/budget.py` states: one
        # oversized skill may not strip every smaller one ranked below it.
        if sel.chars + len(text) > char_cap:
            sel.omitted.append({"id": cand.skill.id, "name": cand.skill.name,
                                "why": OUT_CHARS, "reason": cand.reason})
            continue
        cand.chars = len(text)
        sel.chars += len(text)
        sel.applied.append(cand)
    return sel


# ── Rendering ──────────────────────────────────────────────────────────────────

HEADING = "## PROJECT SKILLS"

_PREAMBLE = ("The following skills were selected for THIS request from "
             "`.agent2/skills/`. Follow them for the work they describe; they are "
             "narrower than your general defaults and outrank them where they "
             "overlap. Ignore any that do not apply.")


def render_one(applied: Applied) -> str:
    """One skill as prompt text. ⚠️ The reason is stated, and that is deliberate.

    The model is told *why* a skill is in front of it — `named in the request` is a
    much stronger instruction than `always on`, and hiding the distinction would
    make five skills read as five equal mandates. It also means the transcript
    carries the same explanation `/skills` prints, so a user reading either one
    sees the same decision.

    A skill's declared `tools:`/`model:` are NOT rendered. They are metadata this
    build reports but does not enforce, and printing an unenforced restriction into
    the prompt would tell the model it has a limit that nothing checks.

    ⚠️ The description is **skipped when the body already opens with it.** A file
    with no `description:` header gets one derived from its first sentence
    (`normalize._first_sentence`), which is right for a listing and wrong here:
    rendered as well as the body it becomes the same line twice, paid for twice
    against `SKILLS_MAX_CHARS`, and reads to anyone looking at the prompt as a bug.
    """
    sk = applied.skill
    head = f"### {sk.name}"
    bits = [REASON_LABEL.get(applied.reason, applied.reason)]
    if sk.origin and sk.origin != "plain":
        bits.append(f"format: {sk.origin}")
    lines = [head, f"_({'; '.join(bits)})_"]
    desc = (sk.description or "").strip()
    body = (sk.body or "").strip()
    if desc and not body.startswith(desc):
        lines.append(desc)
    if body:
        lines.append(body)
    if sk.body_truncated:
        lines.append(f"_(skill text truncated at {config.SKILLS_MAX_BYTES} bytes)_")
    return "\n".join(lines).strip() + "\n"


def prompt_block(sel: Selection) -> str:
    """The whole `## PROJECT SKILLS` block, or `""` when nothing was selected.

    ⚠️ THE FOOTER NAMES WHAT WAS LEFT OUT, in the prompt itself and not only in
    `/skills`. If the cap displaced a skill the user's own message named, the model
    should know a narrower instruction exists — the same reason `budget.notice()`
    puts its trim into the prompt rather than only into a report.
    """
    if not sel.applied:
        return ""
    parts = [HEADING, _PREAMBLE, ""]
    parts.extend(render_one(a) for a in sel.applied)
    notes: list[str] = []
    dropped = [o for o in sel.omitted if o.get("why") in (OUT_CAP, OUT_CHARS)]
    if dropped:
        names = ", ".join(str(o.get("name") or o.get("id")) for o in dropped[:6])
        notes.append(f"{len(dropped)} further skill(s) matched but did not fit: {names}")
    if sel.catalog_truncated:
        notes.append(f"the skills folder was only partly read (limit: {sel.truncated_by})")
    if notes:
        parts.append("_(" + "; ".join(notes) + ".)_")
    return "\n".join(parts).strip()
