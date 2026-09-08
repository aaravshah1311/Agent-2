# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for **web ↔ CLI parity** — asked for as *"i want full sync within web and
cli mode"*, and for the gap that made the point: *"you didnt add /init in web
mode"*.

Run from the repo root:  python -m pytest .github/tests/test_webparity.py -v

The thing being pinned is not "the panels exist". It is that they are **renderers
over routes that already answer the question for the terminal**, because a browser
that derives its own answer is a second declaration of a fact this repo keeps in
one place — and the two surfaces then disagree with nothing on screen to show it.
So the assertions run in both directions:

  Reachability
    - every CLI command in `PARITY` has a browser entry point (a sidebar button,
      a modal, or a `SLASH_CMDS` name), and the slash name is spelled the way the
      terminal spells it
    - every modal `openMod('x')` can open has a branch in `openMod` that loads it.
      ⚠️ A modal with no branch opens EMPTY and stays empty — no error, no clue,
      and it looks exactly like a broken route

  Wiring, which is where a browser panel dies silently
    - every `onclick="fn(...)"` in the four new modals resolves to a function
      DEFINED in script.js (a typo'd handler is an inert button)
    - every `getElementById('...')` the new renderers write into is an id that
      exists in the markup (a renamed id is a panel that renders into nothing)
    - every CSS class the new renderers emit exists in style.css (a missing class
      is a panel that renders and cannot be seen)
    - every `fetch('/api/…')` in the new code resolves to a route the Flask app
      actually has, with a rule that accepts that method

  One declaration
    - the browser's verdict marks are `core.health`'s four states EXACTLY — not a
      subset, not ✓/✗ only. ⚠️ `off` is not a lesser `warn`, and a fifth state
      added server-side must reach the tab rather than rendering as `?`
    - the ✓/⚠/✗/○ list is read from the payload's `sections`, never re-derived
    - `ok` is read from the payload, never recomputed from `problems`
    - Apply is ONE `PUT /api/skills`, not N per-skill writes
    - the skills widget can send all THREE states; two-state loses "never chosen"
    - `/init` in the browser posts `write` and can post it false — the route's own
      dry run, not a browser-side simulation of the command
    - the panels carry no project scan, no health rule and no skill selection of
      their own

conftest.py redirects AGENT2_DB to a throwaway temp DB, so these tests never
touch the developer's real agent2.db.
"""

import json
import re
from pathlib import Path

import pytest

from agent2.config import ROOT

# ── the files under test, read as text ─────────────────────────────────────────


def js() -> str:
    return Path(ROOT, "public", "script.js").read_text(encoding="utf-8")


def css() -> str:
    return Path(ROOT, "public", "style.css").read_text(encoding="utf-8")


def html() -> str:
    from agent2.server.ui import get_html
    return get_html()


def _no_comments(text: str, marker: str = "//") -> str:
    """`text` with its whole-line comments removed.

    The new panels DOCUMENT the invariants they hold — "the ✓/⚠/✗/○ list is
    `core.health.report()["sections"]` verbatim", "A BLOCK LIST, NOT A FORCE
    LIST" — so a bare substring search finds the prose and passes for the wrong
    reason. That is the tautology trap this repo has already been bitten by twice.
    Every assertion below about behaviour runs against this.
    """
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(marker))


def _no_html_comments(text: str) -> str:
    """`text` with `<!-- … -->` blocks removed, for the same reason."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


# ── the parity table ──────────────────────────────────────────────────────────
#
# ⚠️ This is the declaration, and it is deliberately a table rather than a loop
# over `agent2cli`'s help: not every terminal command CAN have a browser twin
# (`/read`, `/run` are the terminal's own), and a test that discovered its own
# expectations would pass the moment a command was quietly dropped. `SKIP` below
# says which ones are deliberate and why, so an omission has to be argued for in
# writing rather than by silence.

PARITY = {
    # CLI command  ->  the modal that answers the same question in the browser
    "init":     "project",
    "scan":     "project",
    "skills":   "skills",
    "workflow": "workflow",
    "ultracode": "ultracode",
    "health":   "status",
    "metrics":  "status",
    "recovery": "status",
    "models":   "models",
    "routing":  "models",
    "memory":   "mem",
    "rules":    "rules",
    "keys":     "settings",
    "mcp":      "settings",
    "offline":  "offline",
}

NEW_MODALS = ("project", "skills", "workflow", "ultracode", "status", "models")


# ══════════════════════════════════════════════════════════════════════════════
# 1 · REACHABILITY
# ══════════════════════════════════════════════════════════════════════════════

def test_every_parity_command_has_a_browser_entry_point():
    """The headline ask: a CLI command the browser cannot reach is the gap.

    `/init` was exactly this — the route existed and shipped in Phase 10, and no
    pixel in the browser called it, so the feature was complete and unreachable.
    """
    body, markup = _no_comments(js()), _no_html_comments(html())
    for cmd, modal in PARITY.items():
        assert f"'{cmd}'" in body, f"/{cmd} has no SLASH_CMDS entry"
        assert f"id=\"mod-{modal}\"" in markup, f"/{cmd} points at a modal that does not exist"


def test_the_slash_names_are_the_names_the_terminal_uses():
    """A browser that renames a command teaches the user the wrong word."""
    names = set(re.findall(r"\{name:'([a-z0-9]+)'", _no_comments(js())))
    missing = sorted(set(PARITY) - names)
    assert not missing, f"not offered in the slash menu: {missing}"


def test_every_new_modal_is_opened_by_a_button_or_a_slash_command():
    markup = _no_html_comments(html())
    body = _no_comments(js())
    for name in NEW_MODALS:
        reachable = (f"openMod('{name}')" in markup) or (f"openMod('{name}')" in body)
        assert reachable, f"mod-{name} exists and nothing opens it"


def test_openMod_loads_every_modal_it_can_open():
    """⚠️ A modal with no branch in `openMod` opens EMPTY and stays empty.

    There is no error and no clue: an unloaded panel is indistinguishable from a
    broken route, which is the one failure a user cannot debug. So the branch is
    asserted per modal rather than trusting that the renderers exist.
    """
    body = _no_comments(js())
    m = re.search(r"function openMod\(n\)\{(.*?)\n", body, flags=re.S)
    assert m, "openMod() not found"
    branch = m.group(1)
    for name in NEW_MODALS:
        assert f"n==='{name}'" in branch, f"openMod() never loads mod-{name}"


# ══════════════════════════════════════════════════════════════════════════════
# 2 · WIRING — where a browser panel dies with no error at all
# ══════════════════════════════════════════════════════════════════════════════

def _modal_markup(name: str) -> str:
    """Just one modal's markup, comments stripped."""
    markup = _no_html_comments(html())
    start = markup.index(f'id="mod-{name}"')
    nxt = markup.find('class="mov"', start)
    return markup[start:nxt if nxt > 0 else len(markup)]


@pytest.mark.parametrize("modal", NEW_MODALS)
def test_every_handler_in_the_new_modals_is_a_real_function(modal):
    """A typo'd `onclick` is a button that does nothing, forever, in silence."""
    body = js()
    calls = set(re.findall(r"onclick=\"([A-Za-z_$][\w$]*)\(", _modal_markup(modal)))
    for fn in sorted(calls):
        assert re.search(rf"\bfunction {fn}\s*\(", body), \
            f"mod-{modal} calls {fn}() and script.js does not define it"


_HANDLER_ATTR = re.compile(r"\bon[a-z]+=\"([^\"]*)\"")


def test_no_inline_handler_is_built_by_a_serializer_that_can_emit_a_quote():
    """⚠️ `JSON.stringify` of a STRING emits its own double quotes, and those close
    the attribute the template just opened.

    Three sites shipped this, and each looked right in the source:

        onclick="se(${JSON.stringify(c)})"                     — the welcome chips
        onclick="editMsg('${msgId}',${JSON.stringify(text)})"  — the message Edit button
        onclick="setSkill(${i},${JSON.stringify(val)})"        — the skills tristate

    The first two were never one handler. The browser read `onclick="se("`, took the
    rest of the suggestion as junk attribute names, and every chip silently did
    nothing — no error, no console trace, and the one feature whose only user is
    somebody on their first launch. The Edit button was worse: a message is
    arbitrary user prose landing unescaped in an attribute, so a `"` in it wrote
    attributes of its own into the row. The third worked **by accident** —
    `true`/`null`/`false` stringify without quotes — which is exactly why the rule
    has to be total rather than three fixed call sites: it was one string literal
    away from the same silence.

    So the invariant is *shape*, not escaping: a value interpolated into an inline
    handler may never come from a serializer that can produce a `"`. Bind the
    listener in JS (`addEventListener`, closing over the value) and the question
    does not arise — which is also the only form that hands the handler the
    original bytes.
    """
    offenders = [
        (i, attr)
        for i, ln in enumerate(_no_comments(js()).splitlines(), 1)
        for attr in _HANDLER_ATTR.findall(ln)
        if "JSON.stringify" in attr
    ]
    assert not offenders, (
        "an inline event handler is built with JSON.stringify — bind the listener "
        f"instead: {offenders}"
    )


@pytest.mark.parametrize("modal", NEW_MODALS)
def test_every_id_the_renderers_write_into_exists_in_the_markup(modal):
    """A renamed id is a renderer painting into nothing.

    `getElementById` returns null, the guard swallows it, and the panel is blank —
    which reads as "the server returned nothing" rather than as a typo.
    """
    markup = _modal_markup(modal)
    ids = set(re.findall(r'id="([\w-]+)"', markup))
    # Every id declared in this modal must be addressed by the JS, and every id
    # the JS addresses must be declared: an orphan on either side is a bug.
    body = _no_comments(js())
    touched = set(re.findall(r"getElementById\('([\w-]+)'\)", body))
    touched |= set(re.findall(r"getElementById\('cap-in-'\+\w+\)", body)) and \
        {i for i in ids if i.startswith("cap-in-")}
    stray = sorted(i for i in ids
                   if i not in touched and not i.endswith("-p") and i != f"mod-{modal}")
    assert not stray, f"mod-{modal} declares id(s) nothing renders into: {stray}"


def test_every_class_the_new_renderers_emit_exists_in_the_stylesheet():
    """A missing class renders and cannot be seen — the quietest failure of all."""
    sheet, body = css(), _no_comments(js())
    start = body.index("async function loadProject()")
    emitted = set()
    for group in re.findall(r'class="([a-z0-9 \-]+)"', body[start:]):
        emitted.update(group.split())
    known = set(re.findall(r"\.([a-z][\w-]*)", sheet))
    missing = sorted(c for c in emitted if c not in known)
    assert not missing, f"emitted by a renderer, absent from style.css: {missing}"


def test_the_new_panels_only_fetch_routes_that_exist():
    """A fetch to an invented path is a panel that is permanently empty.

    Resolved against the real Flask url map rather than against `routes.py` as
    text, so a route that exists only in a docstring cannot satisfy it.
    """
    from agent2.server.routes import register_routes
    try:
        from flask import Flask
    except Exception:  # noqa: BLE001            # pragma: no cover
        pytest.skip("Flask is not installed")

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_routes(app)

    body = _no_comments(js())
    start = body.index("async function loadProject()")
    seen = []
    for path, method in re.findall(
            r"fetch\('(/api/[\w/]+)'(?:\+[^,)]*)?(?:,\s*\{method:'(\w+)')?", body[start:]):
        # A path ending in `/` is a base the code concatenates an id onto
        # (`/api/models/` + the key). Its resolvable form is pinned by
        # `test_the_dynamic_recovery_and_model_paths_resolve_too`, so matching the
        # bare prefix here would only ever fail for being a prefix.
        if path.endswith("/"):
            continue
        seen.append((path, method or "GET"))
    assert seen, "the new panels fetch nothing — they cannot be renderers over routes"

    for path, method in seen:
        adapter = app.url_map.bind("localhost")
        try:
            adapter.match(path, method=method)
        except Exception as exc:                          # noqa: BLE001 - reported, not raised
            pytest.fail(f"{method} {path} is fetched by the browser and the app has no such rule ({exc})")


def test_the_dynamic_recovery_and_model_paths_resolve_too():
    """The two fetches built from values, which the literal sweep above cannot see."""
    from agent2.server.routes import register_routes
    try:
        from flask import Flask
    except Exception:  # noqa: BLE001            # pragma: no cover
        pytest.skip("Flask is not installed")
    app = Flask(__name__)
    register_routes(app)
    adapter = app.url_map.bind("localhost")
    adapter.match("/api/recovery/units/command/abc123", method="POST")
    adapter.match("/api/models/2.5-flash", method="PUT")
    adapter.match("/api/models/2.5-flash", method="DELETE")
    adapter.match("/api/models/rank", method="POST")
    adapter.match("/api/models/routing", method="PUT")
    # The workflow panel builds both of its mutating paths from a name, so neither
    # is a literal the sweep above can see. ⚠️ `/run` is the ONE verb in the whole
    # panel that starts anything — the spec's rule that bare `/workflow` executes
    # nothing is only true while that stays a single call site.
    adapter.match("/api/workflows/audit-site/run", method="POST")
    adapter.match("/api/workflows/audit-site", method="DELETE")
    adapter.match("/api/workflows/audit-site", method="GET")


# ══════════════════════════════════════════════════════════════════════════════
# 3 · ONE DECLARATION — the reason the panels are renderers
# ══════════════════════════════════════════════════════════════════════════════

def test_the_browser_knows_every_verdict_word_core_health_can_produce():
    """⚠️ `off` IS NOT A LESSER `warn`, and this is the direction that rots.

    The WAL checkpointer, the scheduler and both MCP bridges can be off on
    purpose. A browser that knew only ✓/✗ would print a cross at a deliberate
    choice, which is how an alert stops being read. And if a fifth state is ever
    added server-side, this fails rather than rendering it as `?`.
    """
    from agent2.core import health
    from agent2.cli import render

    words = {health.OK, health.WARN, health.FAIL, health.OFF}
    assert words == set(render._HEALTH_MARKS), \
        "the CLI's mark table and core.health's words already disagree"

    m = re.search(r"const V_MARK=\{(.*?)\};", _no_comments(js()), flags=re.S)
    assert m, "V_MARK not found in script.js"
    browser = set(re.findall(r"(\w+):", m.group(1)))
    assert browser == words, (
        f"the browser knows {sorted(browser)}; core.health produces {sorted(words)}")


def test_the_verdict_list_is_read_from_the_payload_and_not_re_derived():
    """`_rows()` on the server is the one place a state is decided.

    A browser-side `if problems.length` ladder would let the tab say healthy while
    the monitor reading the same JSON gets a 503 — both halves right alone.
    """
    body = _no_comments(js())
    fn = body[body.index("async function loadHealth()"):body.index("async function loadMetrics()")]
    assert "d.sections" in fn, "loadHealth() does not render the server's own section list"
    assert "V_MARK[st]" in fn, "the mark is not taken from the section's state"
    assert "d.ok" in fn, "the verdict is not read from the payload"
    # The one thing it may not do: decide the verdict itself.
    assert not re.search(r"problems\.length\s*(===|==|>|<)", fn), \
        "loadHealth() recomputes ok/unhealthy from problems — that decision has one home"


def test_the_browser_carries_no_second_project_scan_or_skill_selection():
    """The panels may format facts; they may not derive them.

    A browser-side walk would be a second answer to "what is this project", and
    `/init` COMMITS its answer to a file every later turn reads as fact — so the
    two derivations would write different truths into one file on alternating runs.
    """
    body = _no_comments(js())
    start = body.index("async function loadProject()")
    tail = body[start:]
    for banned, why in (
        ("PRIORITY", "skill/source ranking belongs to core.skills.select / broker.sources"),
        ("REASONS", "the five tiers are select.REASONS, server-side"),
        ("estimate_tokens", "token cost is llm.router.estimate_tokens"),
        ("agent2:generated", "section ownership is projectdoc's marker, parsed server-side"),
    ):
        assert banned not in tail, f"{banned} appears in the browser: {why}"


def test_apply_writes_every_skill_state_in_one_request():
    """⚠️ ONE write, not N.

    `PUT /api/skills` is `state.set_many()` — one batch, one notify. In dual mode
    the two surfaces are two processes over one DB, so N round trips is N chances
    for the other one to read a half-applied selection.
    """
    body = _no_comments(js())
    fn = body[body.index("async function applySkills()"):body.index("function renderSkillLast()")]
    assert fn.count("fetch(") == 1, "applySkills() makes more than one request"
    assert "'/api/skills'" in fn and "method:'PUT'" in fn
    assert "states:SK_PEND" in fn, "it does not send the pending map as one bulk body"
    assert "/api/skills/" not in fn, "it falls back to the per-skill route, one call each"


def test_the_skills_widget_can_express_all_three_states():
    """⚠️ THREE states, and the third is the point.

    `None` (never chosen) still allows automatic selection; `False` beats every
    signal including the request naming the skill. A checkbox cannot say "never
    chosen", so collapsing the two makes skills read as broken in both directions.
    """
    body = _no_comments(js())
    fn = body[body.index("function renderSkills()"):body.index("function setSkill(")]
    for label, value in (("On", "true"), ("Auto", "null"), ("Off", "false")):
        assert f"btn('{label}',{value}" in fn, f"the {label}/{value} state is not offered"
    # And "on" must mean "not blocked", not "pinned into every prompt".
    st = body[body.index("function skStateOf("):body.index("function renderSkills()")]
    assert "hasOwnProperty.call(SK_PEND,id)" in st and "hasOwnProperty.call(st,id)" in st, \
        "a missing row is not distinguished from an explicit false"


def test_init_in_the_browser_uses_the_routes_own_dry_run():
    """Preview is `write:false` on the server, not a simulation of /init here.

    A browser-side "what would happen" is a second merge algorithm, and the one
    that is wrong is the one nobody runs against a real repository.
    """
    body = _no_comments(js())
    fn = body[body.index("async function runInit(write)"):body.index("let SKILLS=null")]
    assert "'/api/project/init'" in fn and "method:'POST'" in fn
    assert "write" in fn and "describe" in fn and "hint:" in fn
    assert "runInit(false)" in _no_html_comments(html()), "nothing offers the dry run"
    assert "runInit(true)" in _no_html_comments(html()), "nothing offers the real run"


def test_init_reports_a_refusal_as_a_refusal():
    """⚠️ `projectdoc.apply()` declines by RETURNING a reason in a 200 body.

    It re-asks `fs.write` live and refuses outright to create the `.agent2` that
    holds the master key. Rendering that as a crash hides the one thing the user
    can act on.
    """
    body = _no_comments(js())
    fn = body[body.index("async function runInit(write)"):body.index("let SKILLS=null")]
    assert "if(!r.ok)" in fn, "runInit() never checks ok:false"
    assert "r.reason" in fn, "the refusal reason is never shown"


def test_the_dry_run_says_nothing_was_written():
    """The CLI's verb contract, in the browser's words.

    `render_project_doc` prints "would create" / "would update" / "is already
    current" for a dry run and the plain verbs otherwise. A preview that said
    "created" would be a lie about a file on disk.
    """
    body = _no_comments(js())
    fn = body[body.index("async function runInit(write)"):body.index("let SKILLS=null")]
    for phrase in ("would create", "would update", "is already current",
                   "created", "updated", "already current", "nothing written"):
        assert phrase in fn, f"the browser never says {phrase!r}"
    assert "r.reason==='dry run'" in fn, "the dry run is not detected the way the route reports it"


def test_the_preserved_count_is_printed():
    """"Yours, untouched" is the sentence that tells a user their own prose survived
    a command whose name sounds like initialisation. `render_project_doc`'s
    docstring says it prints even when it is the only line."""
    body = _no_comments(js())
    fn = body[body.index("async function runInit(write)"):body.index("let SKILLS=null")]
    assert "r.preserved" in fn and "Yours, untouched" in fn


def test_a_truncated_scan_says_so_in_the_browser_too():
    """⚠️ A partial answer may never read as a complete one.

    `/init` writes its answer into a committed file; a scan that quietly stopped
    at 20 000 files and reported "no tests were found" is the failure
    `core/projectscan.py` is shaped around, and the browser is a second place that
    can hide it.
    """
    body = _no_comments(js())
    fn = body[body.index("function renderProject()"):body.index("function renderProjectDoc()")]
    # ⚠️ Asserted on the GUARD, not on the mention. `"s.truncated" in fn` was a
    # tautology: `s.truncated_by` inside the warning's own text contains it as a
    # substring, so deleting the `if` left the assertion green — the exact trap
    # this repo has already found twice. Sabotage caught it here.
    assert "if(s.truncated)" in fn, "a scan that stopped early is not reported"
    assert "truncated_by" in fn, "it does not say WHICH ceiling engaged"
    assert "AGENT2_INIT_MAX_FILES" in fn, "it does not name the ceiling that engaged"
    assert "s.errors" in fn, "a failed analysis step is not reported"


def test_an_unestablished_capability_prints_as_unknown_not_as_no():
    """⚠️ THREE values, and `None` is not `False`.

    A model that can see images, recorded as unknown and rendered as "no", is
    deranked forever by a UI that cannot say "nobody has checked".
    """
    body = _no_comments(js())
    fn = body[body.index("function capTag(name,v)"):body.index("function renderModels()")]
    assert "v===true" in fn and "v===false" in fn and "'?'" in fn, \
        "capTag() collapses unknown into no"


def test_a_capability_correction_is_validated_by_the_server():
    """The CLI's grammar — `field=value` — sent as raw strings.

    `capabilities._coerce` is the one place a tier, a speed or a boolean is
    decided; a browser that parsed them first would accept what the CLI rejects.
    """
    body = _no_comments(js())
    fn = body[body.index("async function saveCaps(i)"):body.index("async function clearCaps(i)")]
    assert "part.slice(eq+1)" in fn, "the value is not passed through verbatim"
    assert "r.error" in fn, "the server's own rejection message is not surfaced"
    for coerced in ("=== 'true'", "parseInt", "Number("):
        assert coerced not in fn, f"the browser coerces the value itself ({coerced})"


def test_metrics_states_its_scope():
    """⚠️ Per-process series next to an install-wide LLM ledger.

    In dual mode the two halves hold two disjoint registries, so a reader who does
    not know that averages two windows in their head. `report()["scope"]` says it,
    and the browser prints it rather than summarising it away.
    """
    body = _no_comments(js())
    fn = body[body.index("async function loadMetrics()"):body.index("async function loadRecoveryUnits()")]
    assert "scope.series" in fn and "scope.llm" in fn
    assert "AGENT2_METRICS=0" in fn, "a disabled registry is not distinguished from an empty one"
    # ⚠️ The guard again, not the mention: each of these prints its own value, so
    # `"meta.folded" in fn` stayed true with the `if` deleted.
    assert "if(meta.folded)" in fn and "if(meta.dropped)" in fn, \
        "a folded label is silent — the measurement destroyed by what it measures"
    assert "borrowed" in fn, "the three borrowed signals are not labelled as borrowed"


def test_recovery_actions_do_not_pretend_to_overrule_the_permission_gate():
    """An operator overrules the VERDICT, never the capability.

    `crash.retry()` / `terminate()` ask `core.permissions` inside themselves, so a
    refusal comes back as `ok:false` — and the browser must show it rather than
    retry or claim success.
    """
    body = _no_comments(js())
    fn = body[body.index("async function actRecoveryUnit("):body.index("async function scanRecovery()")]
    assert "r.ok?" in fn, "the result is not branched on"
    assert "r.reason" in fn, "a refusal reason is never shown"
    assert "confirm(" in fn, "terminate is not confirmed"


def test_the_recovery_panel_reads_the_ledger_not_a_second_endpoint():
    """⚠️ Never two endpoints for one panel.

    `/api/recovery` is the resume picker the first paint already reads; this is
    Phase 8's review queue. A panel polling both is how the browser shows a
    session resumable while the ledger has already marked its command
    `needs_review`.
    """
    body = _no_comments(js())
    fn = body[body.index("async function loadRecoveryUnits()"):body.index("async function actRecoveryUnit(")]
    assert "'/api/recovery/units'" in fn
    assert "fetch('/api/recovery')" not in fn


def test_the_skills_panel_never_touches_a_skill_file():
    """Task 33, structurally: nothing in the browser writes to a path.

    Enablement is a `skill_state` row precisely because a skill is usually somebody
    else's file in somebody else's repository.
    """
    body = _no_comments(js())
    start = body.index("let SKILLS=null")
    # ⚠️ Ends at the WORKFLOW panel, not at `V_MARK`. The workflow renderers sit
    # between the two and happen to contain none of the banned tokens either — so
    # the wider slice passed for a reason that has nothing to do with skills, and a
    # test that reads more than it names is one edit away from being a tautology.
    tail = body[start:body.index("const WF_MARK=")]
    assert "/api/skills" in tail
    for banned in ("body:", "content", "writeFile", "path:"):
        if banned == "body:":
            continue                                     # the bulk PUT is a body
        assert banned not in tail, f"the skills panel mentions {banned!r}"


def test_the_last_turn_report_explains_every_omission():
    """"Why did my skill not fire" is the question people open this on.

    A skill in the folder and absent from the prompt with no stated reason is
    indistinguishable from a broken walk — the one failure a user cannot debug.
    """
    from agent2.core.skills import select

    body = _no_comments(js())
    fn = body[body.index("function renderSkillLast()"):body.index("const WF_MARK=")]
    reasons = {select.OUT_DISABLED, select.OUT_SHADOWED, select.OUT_CAP,
               select.OUT_CHARS, select.OUT_EMPTY}
    m = re.search(r"const why=\{(.*?)\};", fn, flags=re.S)
    assert m, "the omission map is missing"
    known = set(re.findall(r"(\w+):", m.group(1)))
    assert reasons <= known, f"unexplained omission reason(s): {sorted(reasons - known)}"
    assert "o.by" in fn, "a shadowed skill does not say which one shadowed it"


def test_the_skills_block_is_not_advertised_as_every_skill_in_every_prompt():
    """Task 34's bar, restated where a user reads it: selection is per request."""
    markup = _no_html_comments(html())
    panel = markup[markup.index('id="mod-skills"'):markup.index('id="mod-status"')]
    assert "selected per request" in panel or "not every skill" in panel.lower()


def _wf_panel() -> str:
    """The whole workflow panel's JavaScript, comments stripped.

    Bounded at both ends by the declarations either side of it, so it cannot
    silently widen into a neighbour's renderers and pass for their reasons.

    ⚠️ Ends at `let UC=null` — the UltraCode panel's first declaration — and NOT at
    `const V_MARK=`, which is where it used to stop. UltraCode's renderers landed
    between the two, so the old slice quietly grew to include them: every assertion
    below kept passing, and `test_the_workflow_panel_derives_no_plan_of_its_own` in
    particular passed for a reason that has nothing to do with the workflow panel —
    UltraCode's renderers happen to contain none of the banned tokens either. That is
    the same tautology the skills slice is bounded against three functions above, and
    it is why a slice here names the declaration it stops at rather than the next
    convenient constant.
    """
    body = _no_comments(js())
    return body[body.index("const WF_MARK="):body.index("let UC=null")]


def test_opening_the_workflow_panel_executes_nothing():
    """⚠️ THE SPEC'S OWN RULE: bare `/workflow` must execute nothing.

    In the terminal that is a menu that prints. In the browser it is `openMod` →
    `loadWorkflows()`, and the assertion is on the CALL SHAPE rather than on prose:
    the loader may only GET, and `?force=1` (re-read the folder) is the one thing it
    is allowed to vary. A panel that started a run on open would be an execution
    nobody asked for, on the surface where a stray click is cheapest.
    """
    body = _no_comments(js())
    fn = body[body.index("async function loadWorkflows("):body.index("function renderWorkflows(")]
    assert "fetch('/api/workflows'" in fn, "loadWorkflows() does not read the catalog route"
    assert "method:" not in fn, "loadWorkflows() issues a non-GET — opening the panel writes"
    assert "/run" not in fn, "loadWorkflows() reaches the run route"


def test_run_is_the_only_verb_that_starts_anything():
    """`/workflow run <name>` is the one verb, and one call site is what keeps it so.

    Two callers of `/run` is two places for a confirmation, a capability check or a
    409 to be handled differently — and the permissive one always wins.
    """
    panel = _wf_panel()
    starts = re.findall(r"fetch\('/api/workflows/'\+encodeURIComponent\(name\)\+'/run'", panel)
    assert len(starts) == 1, f"the run route is POSTed from {len(starts)} call site(s), not 1"
    fn = panel[panel.index("async function runWorkflow("):]
    fn = fn[:fn.index("\n}")]
    assert "'/run'" in fn, "runWorkflow() is not the caller"
    assert "method:'POST'" in fn


def test_the_run_button_sends_the_three_things_the_route_reads():
    """⚠️ `S.model` / `S.mode` / `S.chat` DO NOT EXIST — the names are `S.curModel`,
    `S.curMode`, `S.chatId`, and the wrong one is `undefined`, which `JSON.stringify`
    drops. The run would start against the default model with no error anywhere."""
    panel = _wf_panel()
    fn = panel[panel.index("async function runWorkflow("):]
    fn = fn[:fn.index("\n}")]
    for real in ("S.chatId", "S.curModel", "S.curMode"):
        assert real in fn, f"runWorkflow() does not send {real}"
    for wrong in ("S.model", "S.mode", "S.chat|"):
        assert wrong not in fn, f"runWorkflow() reads {wrong}, which is not a field of S"


def test_the_workflow_marks_are_total_over_both_state_vocabularies():
    """⚠️ TWO VOCABULARIES REACH ONE TABLE, and only one of them is nine words long.

    `NodeState` carries `state` (the DAG's nine) *and* `phase` (the runner's five),
    because `_PHASE_FOR` folds `cancelled`/`skipped`→`failed` and `paused`→`blocked` —
    right for a five-word summary, wrong on a screen, where a run somebody cancelled
    would be printed as a failure to diagnose. So the browser's table must cover the
    union, and a tenth state added server-side fails here rather than rendering `?`.
    """
    from agent2.core.dag import model
    from agent2.core.workflow import runner

    words = set(model.STATES) | {
        runner.PHASE_DONE, runner.PHASE_FAILED, runner.PHASE_RUNNING,
        runner.PHASE_READY, runner.PHASE_BLOCKED,
    }
    panel = _wf_panel()
    for table in ("WF_MARK", "WF_CLS"):
        m = re.search(rf"const {table}=\{{(.*?)\}};", panel, flags=re.S)
        assert m, f"{table} not found"
        known = set(re.findall(r"(\w+):", m.group(1)))
        assert words <= known, f"{table} cannot print: {sorted(words - known)}"


def test_the_node_word_prefers_state_over_phase():
    """⚠️ THE FOLD IS THE FALLBACK, NEVER THE ANSWER. `wfWord` must read `state`
    first: preferring `phase` would print a cancelled node as `failed` on a screen
    that has room for the true word, which is exactly the loss `state` exists to
    avoid."""
    panel = _wf_panel()
    m = re.search(r"function wfWord\(nd\)\{(.*?)\n", panel, flags=re.S)
    assert m, "wfWord() not found"
    inner = m.group(1)
    assert ".state" in inner and ".phase" in inner, "wfWord() no longer reads both words"
    assert inner.index(".state") < inner.index(".phase"), \
        "wfWord() consults phase before state — the five-word fold would win"


def test_the_browser_knows_every_verdict_word_core_verify_can_produce():
    """⚠️ `unconfirmed` IS NOT A LESSER `contradicted`, and this is the direction
    that rots — `off`-is-not-`warn` again, one subsystem over.

    A node whose whole job was to read and reason leaves no ledger row, so
    `unconfirmed` is the ORDINARY outcome; printing `✗` at it is how a report stops
    being read. Cross-checked against the terminal's own table so the two surfaces
    cannot drift apart while each stays internally consistent.
    """
    from agent2.core import verify
    from agent2.cli import render

    words = set(verify.VERDICTS)
    assert words == set(render._VERIFY_MARKS), \
        "the CLI's verdict marks and core.verify's words already disagree"
    panel = _wf_panel()
    for table in ("WV_MARK", "WV_CLS"):
        m = re.search(rf"const {table}=\{{(.*?)\}};", panel, flags=re.S)
        assert m, f"{table} not found"
        known = set(re.findall(r"(\w+):", m.group(1)))
        assert known == words, (
            f"{table} knows {sorted(known)}; core.verify produces {sorted(words)}")


def test_verification_is_asked_for_and_never_volunteered():
    """⚠️ `runner.verify()` READS TWO LEDGERS AND WRITES AN AUDIT LINE PER CALL.

    So the plain loader may not ask for one: a panel somebody leaves open would pay
    for a verification on every refresh and fill the audit file with verdicts nobody
    requested. `?verify=1` is `?force=1`'s shape for `?force=1`'s reason — one route,
    one question, the caller says how much of the answer it wants.
    """
    panel = _wf_panel()
    assert panel.count("?verify=1") == 1, "the verify query has more than one call site"
    fn = panel[panel.index("async function verifyWorkflow("):]
    fn = fn[:fn.index("\n}")]
    assert "?verify=1" in fn, "verifyWorkflow() is not the one that asks"
    body = _no_comments(js())
    loader = body[body.index("async function loadWorkflows("):body.index("function renderWorkflows(")]
    assert "verify" not in loader, "loadWorkflows() asks for a verification it was not asked for"


def test_a_verification_verdict_never_outlives_the_state_it_describes():
    """⚠️ ABSENT MUST CLEAR, NOT LEAVE THE LAST VERDICT STANDING.

    A plain Refresh does not ask for a verification, so the payload has no
    `verification` key — and a renderer that only painted when one was present would
    leave a stale verdict sitting above run state it no longer describes. That is
    worse than no verdict, because only one of the two is visibly missing. Hence one
    unconditional call from `renderWorkflowRun()`, before any early return.
    """
    panel = _wf_panel()
    fn = panel[panel.index("function renderWorkflowRun()"):]
    fn = fn[:fn.index("\nconst WV_MARK=")]
    calls = re.findall(r"renderWorkflowVerify\(", fn)
    assert len(calls) == 1, f"the verify renderer is called {len(calls)} times from run state"
    head = fn.split("renderWorkflowVerify(", 1)[0]
    # The ONE return allowed above the clear is the null-element guard: with no box
    # there is nothing to clear either. A return that depended on the PAYLOAD — no
    # live run, no recorded runs — is the bug, because those are exactly the states a
    # stale verdict would be left describing.
    assert re.findall(r"\breturn\b", head) == ["return"], \
        "a payload-dependent path through renderWorkflowRun() returns before clearing"
    assert "if(!box) return;" in head
    assert "if(rep===undefined||rep===null||!Object.keys(rep).length){ box.innerHTML='';" in panel, \
        "an absent report no longer clears the box"
    assert "(WF||{}).verification" in fn, "the report is not read from the payload"


def test_the_workflow_panel_derives_no_plan_of_its_own():
    """The panels table's rule, at the one panel where breaking it is tempting.

    Waves, `next` and every hold come from `schedule.plan_next()`; readiness is
    `tasks.ready()`'s and nothing else's. A browser that sorted a graph would be a
    second scheduler — and the spec's *never blindly run every READY node* is a
    property of the one that owns the rows, not of a renderer.

    ⚠️ THE BAN IS ON DERIVING A GRAPH, NOT ON THE WORD `needs`, and Phase D4 is where
    the difference became load-bearing: `renderDraft` reports the edges the *planner*
    already dropped (`_acyclic()` removed them in Python, and "dropped, never silent"
    is why they are printed at all). So the sweep runs over the panel with that one
    renderer excised — and `renderDraft` is then held to the stricter bar underneath,
    because a renderer nobody asserts on is the one that grows a topological sort.
    """
    panel = _wf_panel()
    draft = panel[panel.index("function renderDraft("):]
    draft = draft[:draft.index("\n}")]
    graphs = panel.replace(draft, "")
    for banned in ("topolog", "toposort", "indegree", "in_degree",
                   "findCycle", "isReady", "readyNodes", r"\.needs"):
        assert not re.search(banned, graphs), f"the workflow panel computes {banned!r} itself"
    assert "st.waves" in panel and "st.next" in panel and "st.held" in panel, \
        "the plan is not read from the payload"
    assert "h.code" in panel, "a held node does not print the scheduler's own HOLD_CODES code"

    # The excised renderer, pinned rather than exempted. Its ONE edge mention must be
    # the reported drop; a `needs` read off a *planned* node would be this renderer
    # drawing dependencies, which is the thing the sweep above forbids.
    edges = re.findall(r"\.needs\b", draft)
    assert len(edges) == 1, f"renderDraft() reads node edges {len(edges)} time(s), not 1"
    assert "r.dropped" in draft and "never planned" in draft, \
        "renderDraft()'s edge mention is not the planner's own reported drop"
    assert "r.plan" in draft and "n.title" in draft, "the nodes are not read from the payload"
    assert "sort(" not in draft, "renderDraft() orders the plan itself"


def test_an_empty_next_is_said_out_loud():
    """⚠️ NOTHING RUNNABLE IS THREE SITUATIONS, and a blank cell names none of them.

    Finished · a ceiling binds · an upstream closed the branch. The held lines are
    the only place a reader can tell which, so the empty case must say so rather
    than render an empty span that reads as a rendering bug.
    """
    panel = _wf_panel()
    assert "nothing may start now" in panel
    assert "tasks.ready() releases on settled" in panel, \
        "a SKIPPED node downstream of a failure is left looking like a bug"


def test_the_workflow_panel_says_where_editing_lives():
    """Rule 28: a plausible-but-absent option costs the reader more than an omission.

    There is no edit route on purpose — a browser tab may be on another machine, so
    the web half edits by POSTing a `body` and a text editor is the terminal's. A
    reader who is not told that concludes the browser cannot.
    """
    panel = _wf_panel()
    assert "/workflow edit" in panel, "the panel never says where editing a plan lives"
    assert "$EDITOR" in panel


def test_delete_is_confirmed_and_says_what_is_at_stake():
    """The file is the only copy of a plan somebody wrote."""
    panel = _wf_panel()
    fn = panel[panel.index("async function delWorkflow("):]
    fn = fn[:fn.index("\n}")]
    # ⚠️ A GUARD, NOT A MENTION. `confirm(` appearing somewhere in the function is
    # exactly what a disabled confirmation still looks like — `if(0&&!confirm(…))`
    # keeps the word and deletes the file anyway. So: a declining answer must
    # `return`, and it must do so BEFORE the request is built.
    m = re.search(r"if\(!confirm\(.*?\)\) return;", fn, flags=re.S)
    assert m, "delWorkflow() no longer returns when the human declines"
    assert m.start() < fn.index("fetch("), "the confirmation is asked after the request"
    assert "only copy" in fn, "the confirmation does not say what is lost"
    assert "method:'DELETE'" in fn


def test_a_workflow_refusal_is_a_refusal_and_not_a_crash():
    """⚠️ EVERY WORKFLOW WRITE REFUSES BY *RETURNING* A REASON INSIDE A 200.

    `authoring` asks `fs.write`/`fs.delete` live and `runner.instantiate()` asks
    `chat` live; all three decline with `{"ok": false, "reason": …}`. A panel that
    only handled a non-2xx would report a permission refusal as success — and
    `existed: true` is the one refusal that must not read as an error, because the
    answer to it is "edit the file you already have".
    """
    panel = _wf_panel()
    for fn_name in ("runWorkflow", "newWorkflow", "delWorkflow"):
        fn = panel[panel.index(f"async function {fn_name}("):]
        fn = fn[:fn.index("\n}")]
        assert "if(!r.ok)" in fn, f"{fn_name}() does not handle a returned refusal"
        assert "r.reason" in fn, f"{fn_name}() drops the reason the route gave"
    new = panel[panel.index("async function newWorkflow("):]
    new = new[:new.index("\n}")]
    assert "r.existed" in new, "an already-written plan is reported as an error"
    assert "r.runnable" in new, \
        "`ok` and `runnable` are two facts — a saved file may still not form a graph"


def test_the_two_word_workflow_slash_name_is_offered():
    """⚠️ INVISIBLE TO THE SWEEP ABOVE: `test_the_slash_names_are_the_names…` matches
    `[a-z0-9]+`, so a name with a space in it cannot be found there and would be
    silently unpinned. `slashActivate` clears the input and calls `act()` without
    ever inserting the name, which is what makes a two-word entry safe."""
    body = _no_comments(js())
    assert "{name:'workflow state'" in body, "the live-run entry is not in the slash menu"
    m = re.search(r"function slashActivate\((.*?)\n\}", body, flags=re.S)
    assert m, "slashActivate() not found"
    assert "act()" in m.group(1) and ".name" not in m.group(1), \
        "slashActivate inserts the command name — a two-word name would be typed into the box"


def test_the_panels_reach_the_browser_at_all():
    """The last link in the chain: `get_html()` must actually ship the markup.

    `ui.py` builds one string; a modal defined outside the returned literal is
    code that exists and never renders.
    """
    markup = html()
    for name in NEW_MODALS:
        assert f'id="mod-{name}"' in markup
    assert "&lt;!--" not in markup, "an HTML comment was escaped into visible text"


# ══════════════════════════════════════════════════════════════════════════════
# THE SHAPE THE RENDERERS ADDRESS
# ══════════════════════════════════════════════════════════════════════════════
# ⚠️ THIS IS THE HALF `node --check` CANNOT REACH, and it is where a browser panel
# actually dies. A renderer that reads `d.ok` where the route answers
# `{"result": {"ok": …}}` parses fine, runs fine, renders an empty box and throws
# nothing — the symptom is a blank panel, not an error, and no amount of asserting
# on the JavaScript finds it. So every key path the four panels address is pinned
# against a REAL response from a REAL app over a REAL project.
#
# Four of these were one rename from silence: `/init`'s nesting under `result`, the
# exact string `"dry run"`, `series` being a mapping rather than a list, and the
# routing policy living under `router` rather than `policy`.

from flask import Flask                                        # noqa: E402

from agent2 import database as _db                              # noqa: E402
from agent2.core import workspace as _ws                        # noqa: E402
from agent2.server.routes import register_routes                # noqa: E402


@pytest.fixture
def project(tmp_path):
    """A real little project, a real client, and the workspace put back after.

    Scanning the *repo* would work and would be slow and noisy; what matters is
    that the payload comes from `core.projectscan` rather than from this file.
    """
    root = tmp_path / "calcproj"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname="calc"\n[tool.pytest.ini_options]\ntestpaths=["tests"]\n', encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "src" / "main.py").write_text("def main():\n    return 4\n", encoding="utf-8")
    (root / "tests" / "test_main.py").write_text("def test_main():\n    assert 1\n", encoding="utf-8")
    sk = root / ".agent2" / "skills" / "xss"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: xss-hunting\ndescription: cross-site scripting\nkeywords: [xss]\n---\n\nProbe.\n",
        encoding="utf-8")

    _db.init_db()
    prev = _ws.root()
    _ws.set_workspace(str(root))
    app = Flask(__name__)
    app.secret_key = "parity"
    register_routes(app)
    try:
        with app.test_client() as c:
            yield c, root
    finally:
        _ws.set_workspace(prev)


def _reads(fn_name: str) -> str:
    """The body of one renderer, comments stripped."""
    src = js()
    i = src.index(f"function {fn_name}(")
    return _no_comments(src[i:src.index("\n}", i)])


def test_the_init_routes_answer_is_nested_under_result(project):
    """⚠️ `ok` is NOT a top-level key, and reading it as one reports every write as
    a refusal — a working `/init` that says "refused" on every single click."""
    c, _ = project
    body = c.post("/api/project/init", json={"hint": "a calculator", "describe": False,
                                             "write": False}).get_json()
    assert set(body) >= {"result", "scan"}
    assert "ok" not in body, "the flags moved to the top level — runInit must move with them"
    r = body["result"]
    for key in ("ok", "reason", "created", "changed", "added", "updated", "preserved",
                "bytes", "hint", "doc", "errors", "dirs_created", "files_created", "describe_note"):
        assert key in r, f"runInit prints r.{key} and the route no longer answers it"
    assert "d.result" in _reads("runInit"), "the renderer stopped unwrapping `result`"


def test_the_dry_run_marker_is_the_exact_string_the_browser_compares(project):
    """`dry=r.reason==='dry run'` is a string comparison across two languages.

    Reword it server-side and the browser reports a preview as a write — the one
    direction of this mistake that can cost a user their own prose.
    """
    c, root = project
    dry = c.post("/api/project/init", json={"describe": False, "write": False}).get_json()["result"]
    assert dry["reason"] == "dry run"
    assert not (root / ".agent2" / "agent2.md").exists(), "a dry run wrote the file"
    real = c.post("/api/project/init", json={"describe": False, "write": True}).get_json()["result"]
    assert real["reason"] == "", "a real write must not carry the dry-run marker"
    assert (root / ".agent2" / "agent2.md").exists()
    assert "r.reason==='dry run'" in _no_comments(js())


def test_every_project_field_the_panel_prints_is_in_the_payload(project):
    """`projRows` addresses twenty-three fields; a renamed one prints `?` forever."""
    c, _ = project
    body = c.get("/api/project").get_json()
    scan = body["scan"]
    for key in ("root", "name", "files", "dirs", "primary_language", "elapsed_ms", "languages",
                "package_managers", "frameworks", "entry_points", "tests", "build", "docs",
                "config_files", "git", "structure", "agent2", "commands", "conventions",
                "file_notes", "truncated", "truncated_by", "errors"):
        assert key in scan, f"renderProject prints s.{key} and the scan no longer answers it"
    assert {"files", "dirs", "runners"} <= set(scan["tests"])
    assert {"repo", "unknown", "describe"} <= set(scan["git"])
    assert {"present", "doc", "doc_chars", "skills"} <= set(scan["agent2"])
    assert {"exists", "path", "bytes", "sections", "generated", "preserved", "hint"} <= set(body["doc"])


def test_the_key_files_table_prints_the_scans_own_summary(project):
    """⚠️ The panel prints `n.summary`; `projectscan._note_summary` computes it.

    "The first sentence of a note" is one declaration read by three printers — this
    table, `cli/render._scan_rows()` and the `## Important Files` section written to
    disk. A trim in JavaScript would describe a file differently from the doc a later
    turn reads as fact, the same reason `core/highlight.py` ships `spans` rather than
    letting the browser tokenize.
    """
    c, root = project
    (root / "engine.py").write_text(
        '"""The evaluator. Walks the parse tree once."""\nX = 1\n', encoding="utf-8")
    notes = c.get("/api/project").get_json()["scan"]["file_notes"]
    assert notes, "the scan found no note to print"
    for key in ("path", "why", "summary", "text", "chars", "truncated", "language"):
        assert key in notes[0], f"the panel reads n.{key}"
    src = _no_comments(js())
    assert "n.summary" in src
    assert "file_notes" in src
    # ⚠️ Not a substring check on the whole file: `_note_summary`'s sentence split is
    # a regex, and a browser that grew its own would be invisible in a key list.
    assert "split('.')" not in src, "the browser must not re-derive a first sentence"


def test_the_command_table_reads_the_kinds_the_scan_can_produce(project):
    """The table walks a declared list of kinds, so a kind absent from that list is
    a command the browser silently never shows."""
    c, _ = project
    cmds = c.get("/api/project").get_json()["scan"]["commands"]
    order = re.search(r"order=\[([^\]]*)\]", _reads("renderProject"))
    assert order, "the command kinds are no longer a declared list"
    known = set(re.findall(r"'([a-z]+)'", order.group(1)))
    assert set(cmds) <= known, f"unrendered command kind(s): {sorted(set(cmds) - known)}"


def test_the_skills_payload_carries_what_the_panel_prints(project):
    """Including the two ceilings the omission reasons quote back to the user.

    ⚠️ `last` is what THIS PROCESS last selected, so it is `{}` until a turn has
    happened — the panel's "No skill applied" line is that state, not a failure. A
    selection is run here first, because the keys only exist once there is an answer.
    """
    c, _ = project
    from agent2.core import skills as SK
    SK.for_turn(message="find an xss payload")
    d = c.get("/api/skills?force=1").get_json()
    assert {"catalog", "states", "last", "policy", "stats"} <= set(d)
    assert "skills" in d["catalog"]
    assert {"id", "name", "description", "origin", "priority"} <= set(d["catalog"]["skills"][0])
    # ⚠️ `states` holds *chosen* skills only — a discovered skill absent from it is
    # `None` (never chosen), which is the state that is not `False`.
    assert isinstance(d["states"], dict)
    assert d["catalog"]["skills"][0]["id"] not in d["states"]
    assert {"applied", "omitted", "considered", "limit", "max_chars", "truncated_by"} <= set(d["last"])


def test_the_health_sections_carry_the_three_things_a_row_prints(project):
    c, _ = project
    d = c.get("/api/health").get_json()
    assert {"ok", "problems", "warnings", "sections"} <= set(d)
    assert d["sections"], "the verdict list is empty — every row would vanish"
    for s in d["sections"]:
        assert {"key", "label", "state", "text"} <= set(s)


def test_the_metrics_series_is_a_mapping_of_signal_to_label(project):
    """⚠️ A LIST HERE WOULD RENDER AN EMPTY TABLE, silently.

    `loadMetrics` walks `Object.keys(series)` then `Object.keys(series[sig])`; over a
    list those keys are `"0"`, `"1"`, … and every label is gone.
    """
    from agent2.core import metrics as M
    with M.timer(M.TOOL_LATENCY, "read_file"):
        pass
    c, _ = project
    d = c.get("/api/metrics").get_json()
    assert {"series", "counters", "meta", "signals", "borrowed", "scope"} <= set(d)
    assert isinstance(d["series"], dict) and isinstance(d["counters"], dict)
    assert isinstance(d["signals"], dict), "loadMetrics reads signals[name].unit"
    for labels in d["series"].values():
        assert isinstance(labels, dict)
        for stats in labels.values():
            assert {"count", "avg", "p50", "p95", "max"} <= set(stats)
    assert {"enabled", "dropped", "folded", "max_series"} <= set(d["meta"])
    assert {"series", "llm"} <= set(d["scope"])
    assert {"total", "failed", "fallbacks", "avg_latency_ms"} <= set(d["borrowed"]["llm"])
    assert "denied" in d["borrowed"]["permissions"]


def test_the_recovery_counters_are_the_four_the_header_prints(project):
    c, _ = project
    d = c.get("/api/recovery/units").get_json()
    assert {"units", "counters"} <= set(d)
    assert {"review", "recovered", "retried", "scans"} <= set(d["counters"])


def test_the_routing_policy_lives_under_router_not_policy(project):
    """⚠️ `MDLS.policy` would be `undefined` and the routing tab would print blanks
    with no error — the browser cannot tell an absent key from an off switch."""
    c, _ = project
    d = c.get("/api/models").get_json()
    assert "router" in d and "policy" not in d
    r = d["router"]
    for key in ("routing", "routing_modes", "candidates", "max_hops", "long_context_threshold"):
        assert key in r, f"renderRouting prints router.{key}"
    assert {"key", "source", "provider"} <= set(d["models"][0])
    assert "attempts" in d
    # `Calls recorded` reads `stats`, which is `router.stats()` — the same numbers
    # `/api/metrics` forwards as `borrowed.llm`, and deliberately not a second count.
    assert {"total", "failed", "fallbacks", "avg_latency_ms"} <= set(d["stats"])
    assert "(MDLS||{}).router" in _no_comments(js()), "the renderer reads a different key now"
    assert "(MDLS||{}).stats" in _no_comments(js())


def test_the_capability_words_the_browser_prints_are_the_catalogs(project):
    """`capTag` prints yes / no / ? and nothing else, so a fourth truth value added
    server-side must not silently render as "no"."""
    c, _ = project
    for m in c.get("/api/models").get_json()["models"]:
        for field in ("vision", "tool_use", "thinking", "structured_output"):
            assert m.get(field) in (True, False, None), f"{m['key']}.{field} is not tri-state"


# ── the workflow panel, against a real plan on a real disk ────────────────────
#
# ⚠️ THE FIXTURE WRITES ITS WORKFLOW THROUGH THE ROUTE, not with `write_text`. The
# seeded template is `authoring.template()`'s, so what the panel is pinned against is
# the plan a user actually gets from the **New** button — and a template edit that
# stopped producing a runnable graph fails here rather than shipping a folder of
# files whose Run button is disabled.

@pytest.fixture
def wfproject(project):
    """`project`, plus one seeded-and-runnable workflow, discovered fresh."""
    c, root = project
    made = c.post("/api/workflows", json={"name": "audit-site"}).get_json()
    assert made["ok"], f"the seeded plan was refused: {made.get('reason')!r}"
    assert made["runnable"], "authoring.template() no longer produces a runnable graph"
    return c, root, made


def test_the_workflow_catalog_carries_what_the_cards_print(wfproject):
    """⚠️ THE HALF `node --check` CANNOT REACH. `renderWorkflows` reads sixteen keys
    off two objects; a rename on either side is a card of blanks and no error."""
    c, _, _ = wfproject
    d = c.get("/api/workflows?force=1").get_json()
    assert {"catalog", "live", "runs", "policy"} <= set(d)
    assert "verification" not in d, \
        "a plain read volunteered a verification — it writes an audit line per call"

    cat = d["catalog"]
    for key in ("root", "exists", "enabled", "count", "runnable", "workflows",
                "truncated", "truncated_by", "errors", "age", "ms"):
        assert key in cat, f"renderWorkflows prints cat.{key} and the route dropped it"
    assert cat["exists"] and cat["count"] >= 1 and cat["runnable"] >= 1
    # `age`/`ms` are what `#wf-dirty` prints, and what "Reload folder" acts on.
    assert cat["age"] is None or isinstance(cat["age"], (int, float))
    assert isinstance(cat["ms"], (int, float))

    wf = next(w for w in cat["workflows"] if w["name"] == "audit-site")
    for key in ("name", "ok", "summary", "schema", "upgraded_from", "count", "rel", "size"):
        assert key in wf, f"a workflow card prints wf.{key} and the loader dropped it"
    assert wf["ok"] is True and wf["count"] >= 2

    pol = d["policy"]
    for key in ("max_nodes", "state_chars", "named_nodes", "yaml"):
        assert key in pol, f"the footer prints pol.{key}"


def test_the_run_route_answers_under_run_not_state(wfproject):
    """⚠️ `r.state` WOULD BE `undefined` AND THE TOAST WOULD SAY `0 node(s) queued`
    after every successful start — a working feature that reads as broken, forever,
    with nothing in a console."""
    c, _, _ = wfproject
    r = c.post("/api/workflows/audit-site/run", json={}).get_json()
    assert r["ok"] is True, r.get("reason")
    assert "run" in r and "state" not in r
    assert r["run"]["total"] >= 2
    assert "(r.run||{}).total" in _no_comments(js()), "runWorkflow reads a different key now"


def test_the_live_run_carries_every_field_the_run_tab_prints(wfproject):
    """The plan half and the node half, both read from the payload and neither
    re-derived. A missing `waves` renders the Plan block away silently."""
    c, _, _ = wfproject
    c.post("/api/workflows/audit-site/run", json={})
    st = c.get("/api/workflows").get_json()["live"]
    for key in ("exists", "name", "status", "done", "total", "current", "width",
                "waves", "next", "held", "interrupted", "nodes", "run_id",
                "source", "schema", "error"):
        assert key in st, f"renderWorkflowRun prints st.{key} and the runner dropped it"
    assert st["exists"] is True
    assert isinstance(st["waves"], list) and isinstance(st["next"], list)
    assert isinstance(st["held"], list)
    for nd in st["nodes"]:
        for key in ("node", "title", "state", "phase", "blocked_by",
                    "upstream_failed", "error"):
            assert key in nd, f"a node row prints nd.{key}"
        # ⚠️ BOTH WORDS, ALWAYS. `wfWord` prefers `state`; `phase` is the fold, and a
        # payload carrying only one of them makes the preference unexpressible.
        assert nd["state"] and nd["phase"]
    # `source` and `schema` are the provenance the footer prints, and they are the
    # pair `execstate.workflow_step()` used to overwrite on the first transition.
    assert st["source"] and st["schema"] not in (None, "")


def test_a_second_run_is_refused_while_one_is_live(wfproject):
    """⚠️ TWO LIVE RUNS MAKE `for_turn()`'s "the current node" AMBIGUOUS, so the
    route answers 409 — and the panel must print that as a refusal, not as a start."""
    c, _, _ = wfproject
    first = c.post("/api/workflows/audit-site/run", json={})
    assert first.get_json()["ok"] is True
    second = c.post("/api/workflows/audit-site/run", json={})
    assert second.status_code == 409
    body = second.get_json()
    assert body["ok"] is False and body["reason"]
    assert "live" in body, "the 409 does not name the run that is already going"


def test_the_recorded_runs_carry_the_columns_the_history_rows_print(wfproject):
    """The `runs` list is raw `exec_workflows` columns, and the browser prints five."""
    c, _, _ = wfproject
    c.post("/api/workflows/audit-site/run", json={})
    runs = c.get("/api/workflows").get_json()["runs"]
    assert runs, "a started run left no record"
    for key in ("id", "name", "status", "step_index", "total_steps", "updated_at", "error"):
        assert key in runs[0], f"a history row prints r.{key}"


def test_a_verification_is_returned_only_when_it_is_asked_for(wfproject):
    """⚠️ ASKED FOR, NEVER VOLUNTEERED — and the shape must be the one the renderer
    reads. `verified` is the single boolean that licenses the word *Complete*."""
    c, _, _ = wfproject
    c.post("/api/workflows/audit-site/run", json={})
    plain = c.get("/api/workflows").get_json()
    assert "verification" not in plain

    rep = c.get("/api/workflows?verify=1").get_json()["verification"]
    for key in ("ok", "complete", "verified", "truncated", "counts",
                "problems", "warnings", "findings"):
        assert key in rep, f"renderWorkflowVerify prints rep.{key}"
    from agent2.core import verify
    assert set(rep["counts"]) == set(verify.VERDICTS), \
        "the tally would print a verdict the browser has no mark for"
    for f in rep["findings"]:
        for key in ("ref", "title", "verdict", "evidence"):
            assert key in f, f"a finding row prints f.{key}"
        assert f["verdict"] in verify.VERDICTS
    # ⚠️ THREE BOOLEANS, THREE QUESTIONS, DELIBERATELY NOT COLLAPSED — and this is the
    # state that proves it: the plan validated and nothing failed, so `ok` is True,
    # while no node has settled, so `complete` is False. Only `verified` may license
    # the word *Complete*, and a renderer reading `ok` for it would print it here.
    assert rep["ok"] is True and rep["complete"] is False
    assert rep["verified"] is False, "a run whose nodes have not settled reported itself verified"


def test_an_unknown_workflow_says_how_many_are_known(wfproject):
    """"Not found" and "you have none" send a caller to two different places."""
    c, _, _ = wfproject
    r = c.get("/api/workflows/nope-not-here")
    assert r.status_code == 404
    body = r.get_json()
    assert "known" in body and "audit-site" in body["known"]


def test_writing_a_plan_twice_never_overwrites_it(wfproject):
    """⚠️ THE FILE IS THE ONLY COPY OF A PLAN SOMEBODY WROTE, so `create()` refuses
    and reports `existed` — a refusal the panel prints as `info`, not as an error,
    because the answer to it is "edit the file you already have"."""
    c, root, _ = wfproject
    again = c.post("/api/workflows", json={"name": "audit-site"}).get_json()
    assert again["ok"] is False and again["existed"] is True
    for key in ("ok", "reason", "name", "rel", "existed", "runnable"):
        assert key in again, f"newWorkflow prints r.{key}"
    text = (root / ".agent2" / "workflows" / "audit-site.yaml").read_text(encoding="utf-8")
    assert "nodes:" in text, "the second create truncated the plan it refused to write"


def test_deleting_a_plan_removes_the_file_and_the_catalog_agrees(wfproject):
    c, root, _ = wfproject
    path = root / ".agent2" / "workflows" / "audit-site.yaml"
    assert path.exists()
    r = c.delete("/api/workflows/audit-site")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert not path.exists()
    cat = c.get("/api/workflows?force=1").get_json()["catalog"]
    assert not [w for w in cat["workflows"] if w["name"] == "audit-site"]


# ── the dynamic planner panel, against a real route and a real ledger ──────────
#
# ⚠️ THE SEAM IS `capabilities.ask_one_shot`, NOT AN `ask=` ARGUMENT. `plan()` and
# `replan()` take an injector, but `POST /api/workflows/auto` calls `dynamic.start()`
# and passes none — so the only seam a *route* test has is the module attribute
# `dynamic._ask()` resolves at call time. Patching it is what makes these tests
# network-free while still driving the whole real path: route → planner → coercion
# → `graph.make_def()` → `store.plan()`.

_LEAKED_INSTRUCTION = "INSTRUCTION-DO-NOT-LEAK-d431"


def _steps_json(*steps, **extra) -> str:
    """One model answer, in the shape `dynamic._coerce()` reads."""
    return json.dumps({"steps": list(steps), **extra})


def _step(nid: str, *, title: str = "", instruction: str = "", needs=()) -> dict:
    return {"id": nid, "title": title or f"{nid} title",
            "instruction": instruction or f"do {nid}", "needs": list(needs)}


def _ledger() -> dict:
    """The three tables a run writes rows into, counted."""
    return {t: _db.qone(f"SELECT COUNT(*) AS c FROM {t}")["c"]
            for t in ("exec_workflows", "task_sessions", "agent_tasks")}


@pytest.fixture
def planner(project, monkeypatch):
    """`project`, plus the planner's one model call answered locally.

    Yields `(client, root, answer)`; `answer(*steps, **extra)` installs the reply the
    next `POST /api/workflows/auto` will plan from.
    """
    from agent2.llm import capabilities as caps

    def answer(*steps, **extra):
        body = _steps_json(*steps, **extra)
        monkeypatch.setattr(caps, "ask_one_shot", lambda _prompt: body)

    c, root = project
    answer(_step("recon"), _step("scan", needs=["recon"]))
    return c, root, answer


def test_every_key_the_draft_renderer_addresses_is_in_a_real_auto_response(planner):
    """⚠️ THE HALF `node --check` CANNOT REACH. `renderDraft` and `autoWorkflow`
    together read nineteen keys off one draft payload; a rename on either side is a
    panel of blanks with no error and no traceback.

    The expected set is **extracted from the JavaScript**, not enumerated here, so a
    key the renderer starts reading tomorrow is checked tomorrow with no test edit.
    """
    c, _, answer = planner
    # A prose step id and a dangling dependency on purpose: they are what make
    # `renames` and `dropped` non-empty, so the receipt rows are pinned against real
    # rows rather than against two empty lists that would agree with anything.
    answer(_step("recon the site", title="Probe the login form"),
           _step("scan", needs=["recon", "ghost"]))
    r = c.post("/api/workflows/auto", json={"goal": "audit the staging site"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True, f"the planner refused a good plan: {body['reason']!r}"

    reading = _reads("autoWorkflow") + "\n" + _reads("renderDraft")
    addressed = set(re.findall(r"\br\.([A-Za-z_]\w*)", reading))
    # ⚠️ `error` is the ONE key deliberately absent from a draft — it belongs to the
    # empty-goal 400 below, which is the only answer here that is not a 200.
    assert "error" in addressed, "autoWorkflow lost the fallback the 400 needs"
    assert len(addressed) > 15, "the key extraction stopped finding what the panel reads"
    for key in sorted(addressed - {"error"}):
        assert key in body, f"renderDraft prints r.{key} and the planner never answered it"

    # The per-row keys, named because the loop variables differ per list and `x` is
    # shared by two of them. The equality is the guard: a renderer that starts reading
    # a row key nobody checked fails here instead of rendering a blank cell.
    rows = set(re.findall(r"\b([nxp])\.([A-Za-z_]\w*)", reading))
    assert rows == {("n", "node"), ("n", "title"), ("p", "code"), ("p", "message"),
                    ("x", "from"), ("x", "needs"), ("x", "step"), ("x", "to")}, \
        f"the draft renderer reads a per-row key this test has never checked: {rows}"
    assert body["plan"], "a runnable draft previewed no node at all"
    for n in body["plan"]:
        assert "node" in n and "title" in n, "a plan row prints n.node and n.title"
    assert body["renames"], "a prose step id was folded and the receipt was not reported"
    for x in body["renames"]:
        assert "from" in x and "to" in x, "a rename row prints x.from and x.to"
    assert body["dropped"], "a dependency on a step nobody planned was dropped silently"
    for x in body["dropped"]:
        assert "step" in x and "needs" in x, "a dropped row prints x.step and x.needs"


def test_planning_a_goal_writes_no_row_at_all(planner):
    """⚠️ PLAN IS THE DEFAULT AND IT COMMITS NOTHING — the whole reason the panel can
    show a graph before anybody agreed to run it. AUTO is asked for by name, and the
    contrast is in this same test on purpose: without it a green row-count assertion
    would prove only that these three tables are never written by anything.
    """
    c, _, _ = planner
    before = _ledger()
    body = c.post("/api/workflows/auto", json={"goal": "audit the staging site"}).get_json()
    assert body["ok"] is True and body["runnable"] is True
    assert _ledger() == before, "planning a goal wrote a row somebody has to clean up"
    assert body["mode"] == "plan", "an omitted mode did not default to planning"
    assert body["started"] is False and body["run_id"] == ""

    started = c.post("/api/workflows/auto",
                     json={"goal": "audit the staging site", "mode": "auto"}).get_json()
    assert started["started"] is True and started["run_id"]
    after = _ledger()
    assert after["exec_workflows"] == before["exec_workflows"] + 1
    assert after["task_sessions"] == before["task_sessions"] + 1
    assert after["agent_tasks"] == before["agent_tasks"] + 2, \
        "the counters cannot see a write, so the assertion above proves nothing"

    # And the browser half of it: typing a goal or pressing the Plan button asks for
    # PLAN, and only the button that appears *after* a plan asks for AUTO.
    markup = _no_html_comments(html())
    assert markup.count("autoWorkflow('plan')") == 2, \
        "the goal box or the Plan button stopped asking for a plan"
    assert "autoWorkflow('auto')" not in markup, \
        "⚠️ the markup can start a run before a plan was ever shown"
    assert "autoWorkflow('auto')" in _reads("renderDraft"), \
        "Start it is not offered by the renderer that drew the plan"


def test_a_planner_refusal_comes_back_inside_a_200_with_a_declared_reason(planner):
    """⚠️ A REFUSAL IS A REFUSAL, NOT A CRASH — `dynamic.start()` declines by
    *returning* a `Draft`, so the panel's `if(!r.ok)` is what prints it. A non-2xx
    here would make the caller take the error path and the reason would never be read.

    The word itself comes from a closed vocabulary, because the browser prints it raw.
    """
    from agent2.core.workflow import dynamic

    c, _, _ = planner
    before = _ledger()
    r = c.post("/api/workflows/auto", json={"goal": "audit the site", "mode": "sideways"})
    assert r.status_code == 200, "a refusal came back as a status the panel cannot read"
    body = r.get_json()
    assert body["ok"] is False
    assert body["reason"] in dynamic.REFUSALS, \
        f"{body['reason']!r} is a word the browser has no vocabulary for"
    assert body["reason"] == dynamic.X_BAD_MODE
    # ⚠️ An unrecognised mode may not be coerced into the one that WRITES.
    assert body["started"] is False and body["run_id"] == ""
    assert _ledger() == before, "a refused mode still wrote rows"
    assert "r.reason" in _reads("autoWorkflow"), "the refusal reason is never read"


def test_the_one_answer_that_is_not_a_200_is_a_goal_with_nothing_in_it(planner):
    """The route's single documented deviation: an empty goal is a **400** carrying
    `error`, not a draft carrying `reason`.

    ⚠️ So `if(!r.ok)` alone would print `undefined` at the user — the renderer's
    `r.reason||r.error` chain is the half that makes this legible, and the composer
    guards it before the request is ever sent.
    """
    c, _, _ = planner
    r = c.post("/api/workflows/auto", json={"goal": "   "})
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"], "the 400 carries no message to print"
    assert "ok" not in body and "reason" not in body, \
        "the 400 grew a draft's shape — the fallback below is then untested"
    auto = _reads("autoWorkflow").replace(" ", "")
    assert "r.reason||r.error" in auto, \
        "⚠️ autoWorkflow lost the fallback that makes this 400 readable"
    assert "Describe the goal first" in _reads("autoWorkflow"), \
        "the composer no longer refuses an empty goal before spending a request"


def test_a_plan_the_validator_refuses_carries_the_problem_rows_the_panel_prints(
        planner, monkeypatch):
    """⚠️ `ok` AND `runnable` ARE TWO FACTS, and this is the state that separates
    them: the model answered, the steps coerced, and the graph is still one the
    runner will not accept. The panel prints `p.message||p.code` per row, so a
    problem list of bare strings, or of dicts missing both keys, renders `undefined`.
    """
    from agent2 import config
    from agent2.core.workflow import dynamic

    c, _, answer = planner
    monkeypatch.setattr(config, "WORKFLOW_MAX_NODES", 1)
    answer(_step("recon"), _step("scan", needs=["recon"]))
    before = _ledger()
    r = c.post("/api/workflows/auto", json={"goal": "audit the staging site"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is False and body["runnable"] is False
    assert body["reason"] == dynamic.X_UNRUNNABLE
    assert body["steps"] == 2, "the refusal lost the count of what was planned"
    assert body["problems"], "a refused graph named no problem at all"
    for p in body["problems"]:
        assert isinstance(p, dict), f"a problem row is {type(p).__name__}, not a mapping"
        assert p.get("message") or p.get("code"), "a problem row prints as undefined"
    assert "p.message||p.code" in _reads("renderDraft").replace(" ", ""), \
        "the renderer stopped reading the problem rows this payload carries"
    assert _ledger() == before, "an unrunnable plan still wrote rows"


def test_a_clipped_plan_says_which_ceiling_clipped_it(planner):
    """⚠️ A PARTIAL ANSWER MAY NEVER READ AS A COMPLETE ONE. `truncated_by` is the
    word the panel prints, so it has to be a real word and not an empty string.
    """
    from agent2 import config
    from agent2.core.workflow import dynamic

    c, _, answer = planner
    over = int(config.DYNAMIC_MAX_STEPS) + 6
    answer(*[_step(f"s{i}") for i in range(over)])
    body = c.post("/api/workflows/auto", json={"goal": "do everything"}).get_json()
    assert body["truncated"] is True, f"{over} steps past the ceiling clipped silently"
    assert body["truncated_by"] == dynamic.T_STEPS
    assert body["steps"] == int(config.DYNAMIC_MAX_STEPS)
    assert "r.truncated_by" in _reads("renderDraft"), "the ceiling is never named on screen"


def test_no_step_instruction_ever_reaches_the_browser(planner):
    """⚠️ A PLANNED INSTRUCTION IS PROSE ABOUT SOMEBODY'S OWN PROJECT, and this
    payload is what a route hands a tab that may be on another machine.

    `test_dynamic.py` pins the same promise on `Draft.to_payload()`; this pins it on
    the **response**, which is the object that actually leaves the process — the two
    are one `jsonify()` apart and only one of them is the wire.
    """
    c, _, answer = planner
    answer(_step("recon", title="Probe the login form", instruction=_LEAKED_INSTRUCTION),
           _step("scan", needs=["recon"]))
    body = c.post("/api/workflows/auto", json={"goal": "audit the staging site"}).get_json()
    assert body["ok"] is True and body["steps"] == 2
    blob = json.dumps(body)
    assert _LEAKED_INSTRUCTION not in blob, "a step's instruction reached the browser"
    assert "instruction" not in blob, "the payload grew a field for it"
    # Anti-vacuity: the step really WAS carried — its id and the title beside it came
    # back — so this cannot pass against a planner that dropped the step whole.
    assert "Probe the login form" in blob, "the plan lost the step, so nothing was withheld"
    assert [n["node"] for n in body["plan"]] == ["recon", "scan"]


# ── the UltraCode panel, against the real engine and the real routes ──────────
#
# ⚠️ TWO SEAMS, AND BOTH ARE THE ONES `test_ultracode.py` USES. `plan.brief()` walks
# the whole project and `dynamic.draft()` would reach a model, so a route test patches
# the module attributes each one is resolved through at call time — `engine` holds
# `_plan` and `_dyn` as module aliases, so patching the modules is what the whole real
# path then runs against: route → engine.start() → plan.graph_for() → store.create().
#
# ⚠️ AND THE PANEL'S OWN SLICE IS BOUNDED, for `_wf_panel()`'s reason. UltraCode's
# renderers are the block that quietly widened the workflow slice; a slice here that
# reached past `// ── /health` would do the same thing to somebody else.

_UC_LEAK = "INSTRUCTION-DO-NOT-LEAK-e531"


@pytest.fixture
def ucproject(project, monkeypatch):
    """`project`, plus UltraCode's two seams answered locally.

    Yields `(client, root, plant)`; `plant(*specs)` installs the graph the next
    `POST /api/ultracode {"action": "start"}` will be planned from. A spec is
    `"id"` or `"id:dep,dep"`, exactly as in `test_ultracode.py`.
    """
    from agent2.core.dag import model as _m
    from agent2.core.ultracode import plan as _plan
    from agent2.core.workflow import dynamic as _dyn

    monkeypatch.setattr(_plan, "brief", lambda goal, **kw: _plan.Brief(goal=str(goal or "")))

    def plant(*specs):
        nodes = []
        for i, spec in enumerate(specs):
            nid, _, needs = str(spec).partition(":")
            nodes.append(_m.Node(id=nid, title=nid.replace("-", " ").title(),
                                 instruction=f"{_UC_LEAK} do {nid}", seq=i,
                                 needs=tuple(n for n in needs.split(",") if n)))
        defn = _m.Graph(name="uc-run", source=_dyn.SRC_GOAL, nodes=tuple(nodes))
        draft = _dyn.Draft(ok=True, mode=_dyn.MODE_AUTO, goal="g", defn=defn,
                           source=_dyn.SRC_GOAL, added=tuple(n.id for n in nodes))
        monkeypatch.setattr(_dyn, "draft", lambda goal, **kw: draft)
        return draft

    c, root = project
    plant("recon", "scan:recon")
    return c, root, plant


def _uc_panel() -> str:
    """The whole UltraCode panel's JavaScript, comments stripped.

    Bounded by the declaration it starts at and the banner it stops before, so it
    cannot widen into the health renderers below it and pass for their reasons.
    """
    body = _no_comments(js())
    return body[body.index("let UC=null"):body.index("const V_MARK=")]


def test_opening_the_ultracode_panel_executes_nothing():
    """⚠️ THE SAME BAR AS THE WORKFLOW PANEL, and here it is the stronger one: the
    terminal's `/ultracode` menu prints, and `POST /api/ultracode {"action":"start"}`
    plans a graph and writes rows. So the loader may only GET — a `method:` anywhere in
    it means opening the panel from the footer starts autonomous execution.
    """
    body = _no_comments(js())
    fn = body[body.index("async function loadUltracode("):body.index("function renderUltracode(")]
    assert "fetch('/api/ultracode'" in fn, "loadUltracode() does not read the state route"
    assert "method:" not in fn, "loadUltracode() issues a non-GET — opening the panel writes"
    assert "action:" not in fn, "loadUltracode() names an action — the read route takes none"


def test_reading_ultracode_writes_no_row(ucproject):
    """The `GET` is one call over `engine.state()`, which is a pure read. A panel the
    footer opens on every visit may not leave a ledger behind."""
    c, _, _ = ucproject
    before = _ledger()
    assert c.get("/api/ultracode").status_code == 200
    assert _ledger() == before, "reading the UltraCode panel wrote rows"


def test_ok_is_only_the_master_switch(ucproject):
    """⚠️ `ok` AND `run` ARE TWO FACTS. `state()` answers `ok: bool(ULTRACODE_ENABLED)`
    and leaves `run: None`, so a healthy install with nothing going answers
    `{ok: true, run: null}` — folded together, the panel prints *UltraCode is off* at
    an install that is fine, which is the failure `renderUltracode`'s first two
    branches are separate for.
    """
    from agent2 import config

    c, _, _ = ucproject
    assert config.ULTRACODE_ENABLED, "the suite is running with UltraCode disabled"
    body = c.get("/api/ultracode").get_json()
    assert body["ok"] is True, "a healthy install with nothing live reported not-ok"
    assert body["run"] is None, "nothing was started and a run came back anyway"
    assert body["reason"] == "", "an idle read carried a refusal"
    assert body["mine"] is False and body["awaiting"] == []
    # The panel tests `!UC.ok` and then `!st` where `const st=UC.run` — two branches.
    fn = _reads("renderUltracode")
    assert "!UC.ok" in fn.replace(" ", ""), "the master-switch branch is gone"
    assert "UC.run" in fn, "the panel stopped reading the run apart from the switch"


def test_every_top_level_key_the_ultracode_panel_reads_is_in_a_real_response(ucproject):
    """⚠️ THE HALF `node --check` CANNOT REACH. Three renderers read this one payload;
    a rename on either side is a panel of blanks with no error and no traceback.

    The expected set is **extracted from the JavaScript**, so a key the panel starts
    reading tomorrow is checked tomorrow with no edit here.
    """
    c, _, _ = ucproject
    started = c.post("/api/ultracode", json={"action": "start", "goal": "harden the login"})
    assert started.status_code == 200
    assert started.get_json()["ok"] is True, started.get_json().get("reason")

    body = c.get("/api/ultracode").get_json()
    reading = "\n".join(_reads(f) for f in ("loadUltracode", "renderUltracode",
                                            "renderUltracodePolicy"))
    addressed = set(re.findall(r"\bUC\.([A-Za-z_]\w*)", reading))
    assert len(addressed) > 3, "the key extraction stopped finding what the panel reads"
    for key in sorted(addressed):
        assert key in body, f"the panel reads UC.{key} and the route never answered it"
    # Named as well as extracted: these eight are `engine.state()`'s declaration, and a
    # key it stops answering must fail here even if the panel stopped reading it too.
    for key in ("ok", "reason", "run", "stage", "stage_label", "awaiting", "mine", "policy"):
        assert key in body, f"engine.state() no longer answers {key!r}"
    assert body["stage"] and body["stage_label"], \
        "the stage came back nameless — the panel prints `stage_label||stage`"


def test_the_run_carries_every_field_the_node_rows_print(ucproject):
    """`renderUltracode` reads the run and one row per node, and neither is re-derived
    here: `state`/`kind`/`blocked_by` are `dag.store.NodeView`'s own words."""
    from agent2.core.dag import model as _m

    c, _, _ = ucproject
    c.post("/api/ultracode", json={"action": "start", "goal": "harden the login"})
    st = c.get("/api/ultracode").get_json()["run"]
    assert st, "a started run reported nothing live"
    for key in ("run_id", "name", "current", "done", "total", "failed", "finished",
                "source", "growth", "nodes"):
        assert key in st, f"the run foot prints st.{key} and the payload dropped it"
    assert st["nodes"], "a planned run carried no node row"
    for nd in st["nodes"]:
        for key in ("node", "title", "state", "kind", "blocked_by", "upstream_failed", "error"):
            assert key in nd, f"a node row prints nd.{key}"
        assert nd["state"] in _m.STATES, \
            f"node state {nd['state']!r} is outside dag.model.STATES — WF_MARK has no mark"
    # `wfWord` prefers `state` over `phase`, so `state` is the one that must be present.
    assert "wfWord(nd)" in _reads("renderUltracode"), \
        "the panel stopped reusing the workflow marks and grew a second table"


def test_the_policy_rows_come_from_the_engine_not_from_the_browser(ucproject):
    """⚠️ THE CEILINGS COME FROM THE ENVIRONMENT. A panel stating its own numbers is
    describing a different install — `engine.describe()` is the one declaration, and
    `plan.max_nodes` / `stages.stages` are the two nested keys the footer prints."""
    from agent2.core.ultracode import engine as _uc

    c, _, _ = ucproject
    pol = c.get("/api/ultracode").get_json()["policy"]
    for key in ("enabled", "max_cycles", "budget_sec", "approval", "worker",
                "refusals", "replan_on", "plan", "stages"):
        assert key in pol, f"renderUltracodePolicy prints p.{key}"
    assert pol["plan"].get("max_nodes"), "the Max nodes row would print `?`"
    assert pol["stages"].get("stages"), "the Stages footer would print `?`"
    assert pol == _uc.describe(), "the route reshaped the posture instead of forwarding it"
    # Anti-vacuity: the refusal list the cancel path filters on is really carried.
    assert set(pol["refusals"]) == set(_uc.REFUSALS)


def test_a_cancel_is_read_on_reason_and_never_on_ok(ucproject):
    """⚠️ A SUCCESSFUL CANCEL ANSWERS `ok: false`. `Finish.ok` says whether the run did
    its job and a cancelled one did not, while `reason` carries `schedule.R_CANCELLED`
    — readable as such precisely because `U_*` and `schedule.REASONS` share no word.

    A panel guarding on `ok` would report every successful cancel as a failure, so this
    pins both halves: the response really does answer `ok: false`, and the renderer
    really does test `reason` against the policy's own refusal list.
    """
    from agent2.core.dag import schedule as _sched
    from agent2.core.ultracode import engine as _uc

    c, _, _ = ucproject
    c.post("/api/ultracode", json={"action": "start", "goal": "harden the login"})
    r = c.post("/api/ultracode", json={"action": "cancel"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["reason"] == _sched.R_CANCELLED, \
        f"a cancel answered {body.get('reason')!r} — the panel reads it as a refusal"
    assert body["reason"] not in _uc.REFUSALS, \
        "⚠️ the cancel verdict entered the refusal vocabulary — every cancel now reads as failed"
    assert body["ok"] is False, "the docstring the renderer relies on is no longer true"

    fn = _reads("cancelUltracode").replace(" ", "")
    assert "refusals.includes" in fn, "cancelUltracode stopped reading the refusal list"
    assert "if(!r.ok)" not in fn, \
        "⚠️ cancelUltracode guards on `ok` — a successful cancel now reports as an error"

    # ⚠️ AND THE BARE READ NOW ANSWERS `run: null`, WHICH IS NOT AN ERROR EITHER.
    # `_load_live()` is "the one **unsettled** run in this project", so a settled one
    # is deliberately not it — the panel goes back to *Nothing is live*, which is the
    # honest answer. The run is still readable by id, and that is the half that proves
    # a cancel settled the graph rather than merely losing sight of it.
    after = c.get("/api/ultracode").get_json()
    assert after["run"] is None, "a settled run is still reported as live"
    named = c.get("/api/ultracode", query_string={"run_id": body["run_id"]}).get_json()
    assert named["run"] and named["run"]["finished"] is True, \
        "the cancelled run is unreadable by id — nothing proves the graph settled"


def test_cancelling_asks_before_it_reaches_the_route(ucproject):
    """The confirm is before the fetch, `delWorkflow`'s shape: a cancel is not
    destructive to completed nodes and it does end the run, so it is asked once."""
    fn = _reads("cancelUltracode")
    m = re.search(r"if\(!confirm\(.*?\)\) return;", fn)
    assert m, "cancelUltracode no longer asks at all"
    assert m.start() < fn.index("fetch("), "the confirm moved below the request"
    assert "Completed nodes stay completed" in fn, \
        "the prompt stopped saying what a cancel does NOT undo"


def test_a_refusal_is_a_refusal_and_not_a_crash(ucproject):
    """⚠️ REFUSE BY RETURNING. Every engine refusal is `{"ok": false, "reason": …}`
    inside a `200`, and all three panel paths read `r.reason` — a panel that only
    handled non-2xx would report a refusal as success.
    """
    from agent2.core.ultracode import engine as _uc

    c, _, _ = ucproject
    # Nothing is live: approve and cancel each refuse with the route's own prose.
    for action in ("approve", "cancel"):
        r = c.post("/api/ultracode", json={"action": action})
        assert r.status_code == 200, f"{action} refused with a status code, not a reason"
        body = r.get_json()
        assert body["ok"] is False and body["reason"], f"{action} refused silently"

    # A second start while one is live is the engine's own `U_LIVE`, not a 409.
    assert c.post("/api/ultracode", json={"action": "start", "goal": "one"}).get_json()["ok"]
    second = c.post("/api/ultracode", json={"action": "start", "goal": "two"})
    assert second.status_code == 200
    body = second.get_json()
    assert body["ok"] is False and body["reason"] == _uc.U_LIVE
    assert body["reason"] in _uc.REFUSALS, "the refusal is outside the vocabulary the panel filters on"
    for fn_name in ("startUltracode", "approveUltracode", "cancelUltracode"):
        assert "r.reason" in _reads(fn_name), f"{fn_name} stopped printing the reason"


def test_an_empty_goal_is_the_one_answer_that_is_not_a_200(ucproject):
    """⚠️ THE ONE 400, AND IT CARRIES `error` RATHER THAN `reason` — there is nothing
    to refuse yet. That is why `startUltracode` reads `r.reason||r.error`, and why the
    composer refuses an empty goal before spending the request at all."""
    c, _, _ = ucproject
    r = c.post("/api/ultracode", json={"action": "start", "goal": "   "})
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"], "the 400 says nothing"
    assert "ok" not in body and "reason" not in body, \
        "the 400 grew a draft's shape — the fallback below is then untested"
    fn = _reads("startUltracode")
    assert "r.reason||r.error" in fn.replace(" ", ""), \
        "⚠️ startUltracode lost the fallback that makes this 400 readable"
    assert "Describe the goal first" in fn, \
        "the composer no longer refuses an empty goal before spending a request"
    assert fn.index("Describe the goal first") < fn.index("fetch("), \
        "the guard moved below the request it exists to save"


def test_an_unknown_action_names_the_ones_that_exist(ucproject):
    """Rule 28 — a plausible-but-absent option costs a reader more than an omission,
    so the 400 lists the three actions this surface has AND says where `run` went."""
    c, _, _ = ucproject
    for payload in ({}, {"action": "work"}, {"action": "replan"}, {"action": "finalize"}):
        r = c.post("/api/ultracode", json=payload)
        assert r.status_code == 400, f"{payload!r} was accepted"
        body = r.get_json()
        assert body["error"], f"{payload!r} refused without saying why"
        assert body["actions"] == ["start", "approve", "cancel"], \
            "the 400 stopped naming the actions this surface has"
        assert "terminal only" in body["run"], \
            "⚠️ the 400 stopped saying where `run` lives — a reader is left guessing"
        assert "read" in body, "the 400 does not name the route that reports the run"


def test_working_the_nodes_is_named_as_the_terminals_and_offered_by_neither(ucproject):
    """⚠️ TWO DOORS ONLY, AND `run` IS NOT ONE OF THEM ON THIS SURFACE. The panel says
    so in a foot rather than shipping a button that would be a third door, and no
    `fetch` in the panel names any action but the three the route has."""
    panel = _uc_panel()
    actions = set(re.findall(r"action:'(\w+)'", panel))
    assert actions == {"start", "approve", "cancel"}, \
        f"the panel reaches an action outside the route's three: {sorted(actions)}"
    assert "/ultracode run" in _reads("renderUltracode"), \
        "the panel stopped naming the terminal command that works the nodes"
    assert "workflow.for_turn()" in _reads("renderUltracode"), \
        "the panel stopped saying what advances the run on this surface"


def test_the_panel_derives_no_stage_label_and_no_verdict_of_its_own(ucproject):
    """⚠️ `stage_label` IS COMPUTED IN EVERY PAYLOAD (`_st.STAGE_LABEL.get(...)`), so
    the browser needs no stage table — and the node marks are the workflow panel's
    `WF_MARK`/`WF_CLS`, reused rather than copied. A second table drifts silently the
    first time a stage or a state is added.
    """
    panel = _uc_panel()
    for banned in ("STAGE_LABEL", "UC_MARK", "UC_CLS", "STAGES=", "topolog", "toposort",
                   "indegree", "in_degree", "isReady", "readyNodes"):
        assert banned not in panel, \
            f"the UltraCode panel derives its own {banned!r} instead of reading the payload"
    assert "WF_MARK[" in panel and "WF_CLS[" in panel, \
        "the panel stopped reusing the workflow marks"
    assert "UC.stage_label" in panel, "the panel stopped reading the label the engine computed"


def test_no_node_instruction_ever_reaches_the_browser(ucproject):
    """⚠️ A PLANNED INSTRUCTION IS PROSE ABOUT SOMEBODY'S OWN PROJECT, and this payload
    is what a route hands a tab that may be on another machine.

    `NodeView` holds the instruction and its `to_payload()` omits it; `test_dag.py` and
    `test_ultracode.py` pin that on the object. This pins it on the **response**, which
    is the thing that actually leaves the process.
    """
    c, _, plant = ucproject
    plant("recon", "scan:recon")
    started = c.post("/api/ultracode", json={"action": "start", "goal": "harden the login"})
    assert started.get_json()["ok"] is True
    blob = json.dumps(started.get_json()) + json.dumps(c.get("/api/ultracode").get_json())
    assert _UC_LEAK not in blob, "a node's instruction reached the browser"
    assert "instruction" not in blob, "the payload grew a field for it"
    # Anti-vacuity: the nodes really WERE carried, so this cannot pass against a
    # planner that dropped them whole.
    assert "Recon" in blob, "the run lost its nodes, so nothing was withheld"
    assert "scan" in blob
