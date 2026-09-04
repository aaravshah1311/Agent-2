# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for THE Context Broker (``agent2/core/broker/``) and the one git reader it
depends on (``agent2/core/gitstate.py``).

Run from the repo root:  python -m pytest .github/tests/test_broker.py -v

Coverage (Task 20)
  - composition: ``ORDER`` is the ONE ordering, memory renders before rules, and
    both `base_tail()` and `prompt_tail()` sort through it
  - the alias contract: ``agent._MEM_CACHE is broker.MEM_CACHE``. This is the one
    that matters most and it is the least obvious — a *copy* would leave every
    existing test green while the prompt served stale memories forever
  - the zero-query guarantee: a warm ``system_prompt()`` still costs no queries,
    and passing a bundle does not put them back
  - per-source guarding: ONE bad collector costs exactly that source, and in
    particular cannot strip the user's rules (which render last)
  - the sources themselves: project docs deduped case-insensitively, git silent
    outside a repo, MCP rendering only the *negative* case, conversation pinned
    and text-free, skills/workflow declared but empty until Phases 11/12
  - `gitstate`: total outside a repo, total with no git binary, and cached

Coverage (Task 21 — ``broker/sources.py``)
  - the source table: every source in ``ORDER`` has a priority, and every
    always-on source outranks every droppable one
  - ``ORDER`` (where a source prints) and ``PRIORITY`` (what survives a budget)
    are allowed to disagree, and in a real bundle they DO
  - the measures: ``token_cost`` delegates to ``router.estimate_tokens``,
    ``ttl_for(git)`` reads ``gitstate.GIT_TTL`` live, ``relevance_of`` is 1.0 for
    the standing sources and ``UNKNOWN_RELEVANCE`` (not 0) with no message
  - ``measure()`` fills only what a collector left unset, and ``collect()`` is the
    one place it happens
  - ``rank()`` is the ONE survival ordering and ``ContextBundle.ranked()``
    delegates to it

Coverage (Task 22 — ``broker/budget.py``)
  - the ceiling is DERIVED: the window from ``capabilities.context_window()``, the
    output allowance from ``config.MODES[mode]["max_tokens"]``, and neither
    reservation may eat the window or drive the ceiling to zero
  - an unknown window is assumed AND reported as assumed; an empty model key
    budgets ``config.DEFAULT_MODEL`` rather than the assumption
  - both env knobs are read at call time, and an operator's explicit ceiling is
    used verbatim — never floored
  - pinned items are never dropped, and over-budget is REPORTED rather than fixed
  - the conversation counts against the ceiling and is never trimmed
  - the fill skips an item that does not fit instead of stopping, and keeps a
    zero-cost item even over budget
  - ``assemble()`` always plans (a surface cannot forget to), ``collect()`` never
    does, and ``prompt_tail()``/``sources_used()`` describe only what was SENT
  - the model is told which sources were omitted, and nothing is said when none were
  - totality: a budget that cannot be computed keeps everything

⚠️ THE LOAD-BEARING TESTS IN THIS FILE
  * ``test_agent_prompt_caches_are_the_brokers_not_copies`` — sabotage-verified:
    rebinding `agent._MEM_CACHE` to a fresh `VersionedCache("memories", …)` turns
    it red, and NOTHING else in the suite notices that break.
  * ``test_one_broken_collector_cannot_cost_the_user_their_rules`` —
    sabotage-verified: moving `collect()`'s try/except from per-source to around
    the loop turns it red.
  * ``test_system_prompt_never_assembles_a_bundle_itself`` — sabotage-verified:
    guards the reason the bundle is a *parameter* rather than an internal call, and
    does it by counting collector calls, because on a warm cache the regression
    costs no queries and a query counter stays green through it.
  * ``test_token_cost_delegates_to_the_router_estimator`` — sabotage-verified: a
    second `len // 4` in the broker leaves both halves self-consistent and makes
    them disagree only in production.
  * ``test_pinned_items_are_never_dropped_even_alone_over_the_limit`` —
    sabotage-verified: a budget that trims until the arithmetic works discards the
    user's own rules, silently, on exactly the turns that matter most.
  * ``test_assemble_always_plans_a_budget`` and
    ``test_the_prompt_tail_renders_what_fit_and_keeps_the_pinned_blocks`` —
    sabotage-verified: dropping `_budget.apply()` from `assemble()`, or rendering
    `self.items` instead of `self.sent()`, both leave every other test green.

conftest.py redirects AGENT2_DB to a throwaway temp DB, so these tests never
touch the developer's real agent2.db.
"""

import os
import time

import pytest

from agent2 import agent as A
from agent2 import database as db
from agent2.core import broker as B
from agent2.core import gitstate as G
from agent2.core import memory as M
from agent2.core import rules as R
from agent2.core import sync as S
from agent2.core.broker import budget as BD
from agent2.core.broker import sources as SRC
from agent2.llm import capabilities as CAPS
from agent2.llm import router as RT

# The query counter lives with the perf guards that first needed it — see the
# comment above the zero-query tests for why it may not be reimplemented here.
from tests.test_perf_guards import counting_queries


@pytest.fixture(autouse=True)
def _schema():
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _warm_caches():
    """Every test starts with both always-on caches cold but valid."""
    B.MEM_CACHE.invalidate()
    B.RULES_CACHE.invalidate()
    yield


# ── Composition: ORDER is the one declaration ─────────────────────────────────

def test_order_lists_every_source_exactly_once():
    names = [getattr(B, n) for n in dir(B) if n.startswith("SOURCE_")]
    assert sorted(B.ORDER) == sorted(names), (
        "a SOURCE_* constant exists that ORDER does not rank — it would render "
        "last by accident and never appear in sources_used()")
    assert len(set(B.ORDER)) == len(B.ORDER), "a source is ranked twice"


def test_memory_renders_before_rules_and_both_are_last():
    """The ordering `test_agent_loop` pins, stated here as the broker's own rule."""
    assert B.ORDER.index(B.SOURCE_MEMORY) < B.ORDER.index(B.SOURCE_RULES)
    assert B.ORDER[-2:] == (B.SOURCE_MEMORY, B.SOURCE_RULES), (
        "the prompt must end on the user's standing instructions")


def test_every_source_in_order_has_a_collector():
    assert B.registered() == list(B.ORDER)


def test_render_sorts_by_order_not_by_insertion(monkeypatch):
    items = [
        B.ContextItem(source=B.SOURCE_RULES, text="RULES"),
        B.ContextItem(source=B.SOURCE_MEMORY, text="MEM"),
        B.ContextItem(source=B.SOURCE_PROJECT, text="PROJ"),
    ]
    assert B._render(items) == "PROJMEMRULES"


def test_render_is_stable_within_one_source():
    items = [B.ContextItem(source=B.SOURCE_SKILLS, text=f"s{i}") for i in range(4)]
    assert B._render(items) == "s0s1s2s3"


def test_an_accounting_only_item_costs_no_prompt_space():
    item = B.ContextItem(source=B.SOURCE_CONVERSATION, text="", pinned=True)
    assert item.rendered is False
    assert B._render([item]) == ""


# ── The alias contract ─────────────────────────────────────────────────────────

def test_agent_prompt_caches_are_the_brokers_not_copies():
    """⚠️ THE ONE THAT WOULD OTHERWISE BE INVISIBLE.

    `agent._MEM_CACHE` / `_RULES_CACHE` must BE the broker's objects. A second
    `VersionedCache("memories", …)` is byte-identical in behaviour when read, so
    every other test in the suite stays green — while the surface that invalidated
    keeps serving its own stale copy and insists it refreshed.
    """
    assert A._MEM_CACHE is B.MEM_CACHE
    assert A._RULES_CACHE is B.RULES_CACHE
    assert A._memories_block is B.memory_block
    assert A._rules_block is B.rules_block


def test_invalidating_through_the_agent_name_refreshes_the_prompt(monkeypatch):
    """The path the existing tests use must still reach the block the prompt reads."""
    monkeypatch.setattr(M, "top_memories", lambda limit=12: [
        {"content": "first fact", "importance": 5, "tags": ""}])
    monkeypatch.setattr(M, "count_memories", lambda: 1)
    A._MEM_CACHE.invalidate()
    assert "first fact" in A.system_prompt()

    monkeypatch.setattr(M, "top_memories", lambda limit=12: [
        {"content": "second fact", "importance": 5, "tags": ""}])
    A._MEM_CACHE.invalidate()
    sp = A.system_prompt()
    assert "second fact" in sp and "first fact" not in sp


def test_broker_does_not_declare_a_second_cache_for_the_same_resource():
    """Both caches must be subscribed to the resource whose writer notifies them."""
    assert B.MEM_CACHE.resource == "memories"
    assert B.RULES_CACHE.resource == "rules"
    assert B.MEM_CACHE is not B.RULES_CACHE


# ── The zero-query guarantee ───────────────────────────────────────────────────
# ⚠️ COUNTED THROUGH `test_perf_guards.counting_queries`, DELIBERATELY IMPORTED
# RATHER THAN REIMPLEMENTED. That helper patches `database._checkout`, and its
# docstring explains why nothing else works: `core/memory` and `core/rules` do
# `from agent2.database import qall`, binding the function into their own namespace
# at import time, so a spy on `database.qall` counts nothing and the guard can only
# ever report zero — a test that always passes. A first draft of this file made
# exactly that mistake and was caught by the sabotage harness.


def test_base_tail_is_memories_then_rules(monkeypatch):
    monkeypatch.setattr(M, "top_memories", lambda limit=12: [
        {"content": "remember me", "importance": 5, "tags": ""}])
    monkeypatch.setattr(M, "count_memories", lambda: 1)
    monkeypatch.setattr(R, "list_rules", lambda active_only=True: [
        {"content": "always lint", "active": 1}])
    B.MEM_CACHE.invalidate()
    B.RULES_CACHE.invalidate()

    tail = B.base_tail()
    assert "remember me" in tail and "always lint" in tail
    assert tail.index("remember me") < tail.index("always lint")


def test_a_bare_system_prompt_still_costs_zero_queries():
    A.system_prompt()                      # warm every cache
    with counting_queries() as q:
        A.system_prompt()
        A.system_prompt()
    assert q["n"] == 0, f"a warm prompt queried the DB {q['n']}x"


def test_system_prompt_with_a_bundle_still_costs_zero_queries():
    """A pre-assembled bundle must not put DB queries back on the prompt path."""
    bundle = B.assemble(chat_id="c-zero")
    A.system_prompt(context=bundle)        # warm
    with counting_queries() as q:
        A.system_prompt(context=bundle)
        A.system_prompt(context=bundle)
    assert q["n"] == 0, (
        f"a warm prompt with a pre-assembled bundle ran {q['n']} queries")


def test_system_prompt_never_assembles_a_bundle_itself(monkeypatch):
    """⚠️ WHY THE BUNDLE IS A PARAMETER AND NOT AN INTERNAL CALL.

    The prompt is built once per turn but read on every one of up to
    MAX_AGENT_ITERS iterations. Collection is the expensive half — file reads, up
    to three `git` subprocesses, a task-state query — so `run_agent` assembles ONE
    bundle per turn and hands it in. A `system_prompt()` that collected its own
    would pay all of that per iteration, and (worse) could hand the model a
    *different* prompt halfway through a turn than the one the first half of the
    conversation was answered under.

    This asserts the contract directly rather than by cost: on a warm cache the
    collection happens to issue no queries, so a query counter alone stays green
    through exactly this regression. Sabotage-verified.
    """
    bundle = B.assemble(chat_id="c-once")      # the turn's ONE collection
    calls: list = []
    real = B.collect

    def spy(req):
        calls.append(req)
        return real(req)

    monkeypatch.setattr(B, "collect", spy)     # patched AFTER the legitimate one
    A.system_prompt(context=bundle)
    A.system_prompt()                          # and with no bundle at all
    assert calls == [], (
        "system_prompt() collected context itself — it must render the bundle it "
        "was given, and base_tail() when it was given none")


def test_a_bundle_prompt_tail_contains_the_always_on_blocks(monkeypatch):
    monkeypatch.setattr(M, "top_memories", lambda limit=12: [
        {"content": "bundle fact", "importance": 5, "tags": ""}])
    monkeypatch.setattr(M, "count_memories", lambda: 1)
    B.MEM_CACHE.invalidate()
    tail = B.assemble(chat_id="c1").prompt_tail()
    assert "bundle fact" in tail


# ── Per-source guarding ────────────────────────────────────────────────────────

def test_one_broken_collector_cannot_cost_the_user_their_rules(monkeypatch):
    """⚠️ ONE GUARD PER COLLECTOR, NOT ONE AROUND THE LOOP.

    `ORDER` puts memory and rules LAST, so a single try/except around the loop
    would let a broken git binary silently strip the user's own standing rules
    from the prompt — with no error anywhere.
    """
    monkeypatch.setattr(R, "list_rules", lambda active_only=True: [
        {"content": "never force push", "active": 1}])
    B.RULES_CACHE.invalidate()

    def boom(_req):
        raise RuntimeError("git exploded")

    monkeypatch.setitem(B._COLLECTORS, B.SOURCE_GIT, boom)
    bundle = B.assemble(chat_id="c1")

    assert B.SOURCE_GIT in bundle.errors
    assert "git exploded" in bundle.errors[B.SOURCE_GIT]
    assert "never force push" in bundle.prompt_tail(), (
        "a failing early source dropped a later one — the guard is around the loop")
    assert bundle.get(B.SOURCE_RULES), "the rules item never made it into the bundle"


def test_a_failed_source_is_reported_not_silently_absent(monkeypatch):
    def boom(_req):
        raise ValueError("nope")

    monkeypatch.setitem(B._COLLECTORS, B.SOURCE_TASKS, boom)
    payload = B.assemble(chat_id="c1").to_payload()
    assert payload["errors"][B.SOURCE_TASKS].startswith("ValueError:")
    assert B.SOURCE_TASKS not in payload["sources"]


def test_a_collector_returning_junk_is_ignored_not_rendered(monkeypatch):
    monkeypatch.setitem(B._COLLECTORS, B.SOURCE_SKILLS,
                        lambda _req: ["not an item", None, 42])
    bundle = B.assemble(chat_id="c1")
    assert bundle.get(B.SOURCE_SKILLS) == []
    assert bundle.errors == {} or B.SOURCE_SKILLS not in bundle.errors


def test_a_collector_returning_none_is_treated_as_empty(monkeypatch):
    monkeypatch.setitem(B._COLLECTORS, B.SOURCE_SKILLS, lambda _req: None)
    bundle = B.assemble(chat_id="c1")
    assert bundle.get(B.SOURCE_SKILLS) == []


# ── include / exclude ──────────────────────────────────────────────────────────

def test_exclude_skips_a_source_and_include_narrows_to_one():
    req = B.request(chat_id="c1", exclude=frozenset({B.SOURCE_GIT}))
    assert req.wants(B.SOURCE_GIT) is False
    assert req.wants(B.SOURCE_MEMORY) is True
    assert B.collect(req).get(B.SOURCE_GIT) == []

    only = B.request(chat_id="c1", include=frozenset({B.SOURCE_MEMORY}))
    bundle = B.collect(only)
    assert bundle.get(B.SOURCE_PROJECT) == []
    assert len(bundle.get(B.SOURCE_MEMORY)) == 1


def test_exclude_beats_include_when_both_name_a_source():
    req = B.request(include=frozenset({B.SOURCE_GIT}),
                    exclude=frozenset({B.SOURCE_GIT}))
    assert req.wants(B.SOURCE_GIT) is False


# ── The individual sources ─────────────────────────────────────────────────────

def test_the_conversation_is_pinned_accounted_for_and_never_re_read():
    """`agent.build_context()` stays THE reader of the messages table."""
    bundle = B.assemble(chat_id="c1", conversation_messages=7,
                        conversation_tokens=123)
    item = bundle.get(B.SOURCE_CONVERSATION)[0]
    assert item.pinned is True
    assert item.text == "", "the broker must not re-render the turn history"
    assert item.meta == {"messages": 7, "tokens": 123}
    assert bundle.to_payload()["conversation"] == {"messages": 7, "tokens": 123}


def test_project_source_names_docs_and_inlines_only_the_agent_file(tmp_path,
                                                                   monkeypatch):
    from agent2.core import workspace as W
    (tmp_path / "README.md").write_text("# human prose\n" * 50, encoding="utf-8")
    agent_dir = tmp_path / ".agent2"
    agent_dir.mkdir()
    (agent_dir / "agent2.md").write_text("# Project\nNever run `rm -rf`.",
                                         encoding="utf-8")
    W.set_workspace(str(tmp_path))
    try:
        item = B.assemble(chat_id="c1").get(B.SOURCE_PROJECT)[0]
    finally:
        W.set_workspace(os.getcwd())

    assert "Never run `rm -rf`." in item.text, (
        ".agent2/agent2.md is instructions — it has to be IN the prompt")
    assert "human prose" not in item.text, (
        "a README is for humans and is named, not inlined")
    assert "README.md" in item.text
    assert item.meta["primary"] == B.PRIMARY_DOC


def test_a_huge_project_file_is_clipped_with_a_visible_marker(tmp_path):
    from agent2.core import workspace as W
    agent_dir = tmp_path / ".agent2"
    agent_dir.mkdir()
    (agent_dir / "agent2.md").write_text("x" * (B.PROJECT_DOC_CHARS + 5_000),
                                         encoding="utf-8")
    W.set_workspace(str(tmp_path))
    try:
        item = B.assemble(chat_id="c1").get(B.SOURCE_PROJECT)[0]
    finally:
        W.set_workspace(os.getcwd())
    assert "truncated" in item.text
    assert len(item.text) < B.PROJECT_DOC_CHARS + 2_000


def test_project_docs_are_deduped_case_insensitively(tmp_path):
    """`PROJECT_DOCS` lists README.md AND readme.md for case-sensitive filesystems.

    On Windows and macOS both names resolve to the same file, and the naive list
    told the model this project had two documentation files where it has one.
    """
    from agent2.core import workspace as W
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    W.set_workspace(str(tmp_path))
    try:
        item = B.assemble(chat_id="c1").get(B.SOURCE_PROJECT)[0]
    finally:
        W.set_workspace(os.getcwd())
    seen = [d.lower() for d in item.meta["docs"]]
    assert len(seen) == len(set(seen)), f"the same file was listed twice: {seen}"


def test_git_source_is_silent_outside_a_repository(tmp_path):
    from agent2.core import workspace as W
    W.set_workspace(str(tmp_path))
    G.invalidate()
    try:
        assert B.assemble(chat_id="c1").get(B.SOURCE_GIT) == [], (
            '"not a git repo" is a fact the model cannot act on')
    finally:
        W.set_workspace(os.getcwd())
        G.invalidate()


def test_mcp_source_renders_only_the_negative_case(monkeypatch):
    """A connected bridge already ships its own prompt block — see the collector."""
    from agent2.integrations import registry as reg

    monkeypatch.setattr(reg, "health", lambda: [
        {"key": "burp", "label": "Burp Suite", "state": "ok", "text": "connected",
         "ok": True, "enabled": True, "connected": True},
    ])
    item = B.assemble(chat_id="c1").get(B.SOURCE_MCP)[0]
    assert item.text == "", "a connected server must not be described twice"
    assert item.meta["connected"] == 1

    monkeypatch.setattr(reg, "health", lambda: [
        {"key": "zap", "label": "OWASP ZAP", "state": "failing",
         "text": "unreachable", "ok": False, "enabled": True, "connected": False},
    ])
    item = B.assemble(chat_id="c1").get(B.SOURCE_MCP)[0]
    assert "OWASP ZAP" in item.text and "do not call them" in item.text
    assert item.meta["unavailable"] == ["zap"]


def test_a_disabled_mcp_server_is_not_reported_as_missing(monkeypatch):
    """Off on purpose is not a gap — the same rule `/api/health` follows."""
    from agent2.integrations import registry as reg
    monkeypatch.setattr(reg, "health", lambda: [
        {"key": "zap", "label": "OWASP ZAP", "state": "off", "text": "disabled",
         "ok": True, "enabled": False, "connected": False},
    ])
    assert B.assemble(chat_id="c1").get(B.SOURCE_MCP)[0].text == ""


def test_files_source_reports_this_sessions_changes(monkeypatch):
    from agent2.core import diffs as D
    store = D.DiffStore()
    store.add(D.FileChange(path="a.py", kind="modified", added=3, removed=1))
    monkeypatch.setattr(D, "store", store)
    item = B.assemble(chat_id="c1").get(B.SOURCE_FILES)[0]
    assert "a.py" in item.text and "+3" in item.text
    assert "THIS SESSION" in item.text.upper()


def test_skills_and_workflow_are_declared_but_empty_until_their_phases():
    """Declared now so Phase 11/12 arrive by `register()`, not by editing collect()."""
    bundle = B.assemble(chat_id="c1")
    assert bundle.get(B.SOURCE_SKILLS) == []
    assert bundle.get(B.SOURCE_WORKFLOW) == []
    assert B.SOURCE_SKILLS in B.registered()
    assert B.SOURCE_WORKFLOW in B.registered()


def test_a_later_phase_can_replace_a_source_without_editing_collect():
    original = B._COLLECTORS[B.SOURCE_SKILLS]
    try:
        B.register(B.SOURCE_SKILLS, lambda _req: [B.ContextItem(
            source=B.SOURCE_SKILLS, text="\n\n## SKILLS\n- security-audit",
            label="Skills")])
        bundle = B.assemble(chat_id="c1")
        assert "security-audit" in bundle.prompt_tail()
        assert B.SOURCE_SKILLS in bundle.sources_used()
    finally:
        B.register(B.SOURCE_SKILLS, original)


# ── Request defaults, reporting ────────────────────────────────────────────────

def test_project_defaults_to_the_canonical_workspace_key():
    from agent2.core import context as C
    from agent2.core import workspace as W
    assert B.request().project == C.project_key(W.root())


def test_an_explicit_project_is_never_overwritten():
    assert B.request(project="p-explicit").project == "p-explicit"


def test_sources_used_is_in_render_order_and_omits_empty_ones():
    bundle = B.ContextBundle(request=B.request())
    bundle.items = [
        B.ContextItem(source=B.SOURCE_RULES, text="R"),
        B.ContextItem(source=B.SOURCE_MEMORY, text="M"),
        B.ContextItem(source=B.SOURCE_GIT, text=""),
    ]
    assert bundle.sources_used() == [B.SOURCE_MEMORY, B.SOURCE_RULES]


def test_payload_carries_chars_and_never_the_prompt_text():
    bundle = B.assemble(chat_id="c1", surface="cli")
    payload = bundle.to_payload()
    assert payload["surface"] == "cli"
    assert payload["chars"] == len(bundle.prompt_tail())
    for item in payload["items"]:
        assert "text" not in item, "a report must not ship the prompt body"
        assert isinstance(item["chars"], int)


def test_stats_reports_registration_and_cache_counters():
    st = B.stats()
    assert st["sources"] == list(B.ORDER)
    assert st["always"] == sorted(B.ALWAYS)
    assert set(st["memory_cache"]) == {"hits", "misses"}


def test_always_holds_exactly_the_two_standing_instruction_sources():
    assert B.ALWAYS == frozenset({B.SOURCE_MEMORY, B.SOURCE_RULES})


def test_the_always_on_sources_are_pinned_in_the_bundle():
    """Task 22 budgets against `pinned`; a stray False there would drop rules."""
    bundle = B.assemble(chat_id="c1")
    for source in B.ALWAYS:
        items = bundle.get(source)
        assert items and all(i.pinned for i in items), f"{source} is not pinned"


def test_assemble_is_total_on_a_request_it_knows_nothing_about():
    bundle = B.assemble()
    assert isinstance(bundle, B.ContextBundle)
    assert bundle.request.project


# ── gitstate: the one git reader ───────────────────────────────────────────────

def test_gitstate_is_total_outside_a_repository(tmp_path):
    G.invalidate()
    snap = G.snapshot(str(tmp_path))
    assert snap["repo"] is False
    assert snap["branch"] == "" and snap["commits"] == []
    assert G.describe(str(tmp_path)) == ""
    assert G.is_repo(str(tmp_path)) is False


def test_gitstate_is_total_when_git_is_missing(monkeypatch, tmp_path):
    """No `git` on PATH must read as "no git information", never as an exception."""
    monkeypatch.setattr(G.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")))
    G.invalidate()
    assert G.snapshot(str(tmp_path))["repo"] is False
    assert G.branch_now(str(tmp_path)) == ""


def test_gitstate_survives_a_nonzero_exit(monkeypatch, tmp_path):
    class _Res:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a git repository"

    monkeypatch.setattr(G.subprocess, "run", lambda *a, **k: _Res())
    G.invalidate()
    assert G.snapshot(str(tmp_path)) ["repo"] is False


def test_the_snapshot_is_cached_so_a_turn_does_not_fork_git_three_times(monkeypatch,
                                                                        tmp_path):
    """⚠️ `assemble()` runs per turn; uncached this is 3 subprocesses per turn."""
    calls = []
    real = G._run
    monkeypatch.setattr(G, "_run", lambda args, cwd: (calls.append(args[0]),
                                                      real(args, cwd))[1])
    G.invalidate()
    G.snapshot(str(tmp_path))
    first = len(calls)
    G.snapshot(str(tmp_path))
    G.snapshot(str(tmp_path))
    assert len(calls) == first, f"the cache did not hold: {calls}"


def test_refresh_bypasses_the_cache(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(G, "_run", lambda args, cwd: calls.append(args[0]) or None)
    G.invalidate()
    G.snapshot(str(tmp_path))
    n = len(calls)
    G.snapshot(str(tmp_path), refresh=True)
    assert len(calls) > n


def test_a_snapshot_caller_cannot_poison_the_cache(tmp_path):
    G.invalidate()
    first = G.snapshot(str(tmp_path))
    first["branch"] = "tampered"
    first["commits"].append({"hash": "x"})
    second = G.snapshot(str(tmp_path))
    assert second["branch"] == "" and second["commits"] == []


def test_empty_returns_a_fresh_dict_every_time():
    a, b = G.empty(), G.empty()
    a["commits"].append({"hash": "x"})
    assert b["commits"] == []


def test_ahead_behind_are_parsed_from_the_branch_header():
    snap = G.empty()
    G._parse_status(
        "## main...origin/main [ahead 3, behind 2]\n M a.py\n?? new.txt\nA  b.py\n",
        snap)
    assert (snap["branch"], snap["upstream"]) == ("main", "origin/main")
    assert (snap["ahead"], snap["behind"]) == (3, 2)
    assert (snap["staged"], snap["unstaged"], snap["untracked"]) == (1, 1, 1)
    assert snap["dirty"] == 3


def test_a_detached_head_is_reported_as_detached():
    snap = G.empty()
    G._parse_status("## HEAD (no branch)\n", snap)
    assert snap["detached"] is True


def test_a_branch_with_no_upstream_has_no_ahead_behind():
    snap = G.empty()
    G._parse_status("## feature/x\n", snap)
    assert snap["upstream"] == "" and snap["ahead"] == 0 and snap["behind"] == 0


def test_the_status_bar_reads_git_through_gitstate(monkeypatch):
    """⚠️ ONE git reader. A second `subprocess.run(["git", …])` in the status bar
    drifts from this one the day a timeout or a Windows flag changes."""
    sb = pytest.importorskip("agent2.cli.statusbar")
    monkeypatch.setattr(G, "branch_now", lambda cwd=None: "from-gitstate")
    assert sb._git_branch_now() == "from-gitstate"


def test_describe_reads_as_one_human_line(monkeypatch, tmp_path):
    snap = G.empty()
    snap.update({"repo": True, "branch": "main", "ahead": 1, "unstaged": 3,
                 "untracked": 1})
    monkeypatch.setattr(G, "snapshot", lambda cwd=None, refresh=False: snap)
    line = G.describe(str(tmp_path))
    assert line.startswith("main") and "↑1" in line
    assert "3 modified" in line and "1 untracked" in line


def test_describe_says_clean_rather_than_nothing(monkeypatch, tmp_path):
    snap = G.empty()
    snap.update({"repo": True, "branch": "main"})
    monkeypatch.setattr(G, "snapshot", lambda cwd=None, refresh=False: snap)
    assert G.describe(str(tmp_path)).endswith("clean")


# ── Integration with the sync layer ────────────────────────────────────────────

def test_a_memory_write_invalidates_the_block_the_prompt_reads():
    """The broker must not have broken the notify → invalidate → rebuild chain."""
    B.MEM_CACHE.invalidate()
    before = B.memory_block()
    M.add_memory("broker chain fact", importance=9, tags="test")
    after = B.memory_block()
    assert after != before or "broker chain fact" in after
    assert "broker chain fact" in after


def test_the_broker_subscribes_to_resources_the_poller_republishes():
    for resource in ("memories", "rules"):
        assert resource in S.RESOURCES, (
            f"{resource} is not in sync.RESOURCES — a write in the other process "
            "would never invalidate this one's block")


# ══════════════════════════════════════════════════════════════════════════════
# Task 21 — the source table: priority · relevance · token_cost · freshness
# ══════════════════════════════════════════════════════════════════════════════

# ── The names, the order and the table are ONE declaration ─────────────────────

def test_the_source_names_are_re_exported_not_re_declared():
    """⚠️ `broker.SOURCE_*` must be BOUND from `sources`, not restated as literals.

    A second copy of these strings is the quiet failure the one-declaration rule
    exists for: rename a source in one file and the other keeps registering a
    collector nobody collects, with both files reading correctly on their own.

    ⚠️ AND IT CANNOT BE CHECKED WITH `is`. CPython interns identifier-shaped
    literals, so a re-declared `SOURCE_GIT = "git_state"` *would* be the same
    object and an identity assertion would stay green through exactly the break it
    claims to catch — a tautology of the kind this repo has already been bitten by
    twice. So the binding is checked in the source text, where the difference is
    real, and `ORDER`/`ALWAYS` (a tuple and a frozenset, neither interned) are
    checked by identity.
    """
    import re
    from pathlib import Path

    names = [n for n in dir(SRC) if n.startswith("SOURCE_")]
    assert len(names) == len(B.ORDER), "the source list and the constants disagree"
    for name in names:
        assert getattr(B, name) == getattr(SRC, name), f"{name} disagrees"

    text = Path(B.__file__).read_text(encoding="utf-8")
    literals = re.findall(r"^SOURCE_[A-Z_]+\s*=\s*['\"]", text, re.M)
    assert literals == [], (
        f"broker/__init__.py declares {len(literals)} source name(s) as literals — "
        "they must be re-exported from sources.py")

    assert B.ORDER is SRC.ORDER
    assert B.ALWAYS is SRC.ALWAYS


def test_every_source_has_a_priority():
    """A source with no entry would rank below an unregistered third-party one."""
    missing = [s for s in B.ORDER if s not in SRC.PRIORITY]
    assert missing == [], f"no priority declared for {missing}"


def test_every_always_on_source_outranks_every_droppable_one():
    """⚠️ `pinned` and `priority` must not be two contradictory opinions.

    A table that put `git_state` above `rules` would let anything reading the
    numbers alone — a report, a future budget, a human — conclude the wrong thing
    about what is safe to cut.
    """
    floor = min(SRC.priority_of(s) for s in B.ALWAYS)
    for s in B.ORDER:
        if s in B.ALWAYS:
            continue
        assert SRC.priority_of(s) < floor, (
            f"{s} ranks at or above an always-on source")


def test_render_order_and_priority_are_allowed_to_disagree():
    """The headline Task 21 invariant, asserted on a real bundle.

    Memory and rules render LAST (the prompt ends on the user's standing orders)
    and rank FIRST (they are the last thing that may ever be dropped). Deriving
    either from the other forces a choice between the two.
    """
    monkey_git = B.ContextItem(source=B.SOURCE_GIT, text="\n\n## GIT STATE\nx")
    items = SRC.measure_all([
        monkey_git,
        B.ContextItem(source=B.SOURCE_RULES, text="\n\n## RULES\ny", pinned=True),
    ], B.request())
    assert B._render(items).index("GIT STATE") < B._render(items).index("RULES")
    assert [it.source for it in SRC.rank(items)] == [B.SOURCE_RULES, B.SOURCE_GIT]


def test_an_unknown_source_ranks_below_every_declared_one():
    assert SRC.priority_of("something-a-plugin-invented") == SRC.DEFAULT_PRIORITY
    assert SRC.DEFAULT_PRIORITY < min(SRC.PRIORITY.values())


def test_the_table_report_covers_every_source_in_order():
    rows = SRC.table()
    assert [r["source"] for r in rows] == list(B.ORDER)
    assert all(isinstance(r["priority"], int) for r in rows)
    assert [r["source"] for r in rows if r["always"]] == [
        s for s in B.ORDER if s in B.ALWAYS]


def test_stats_reports_the_table_it_is_actually_applying():
    """One table, read — never restated in the reporting layer."""
    assert B.stats()["table"] == SRC.table()


# ── token_cost: the router's estimate, never a second one ──────────────────────

def test_token_cost_delegates_to_the_router_estimator(monkeypatch):
    """⚠️ ONE ESTIMATOR. Sabotage-verified.

    `router.estimate_tokens` is what decides whether a turn needs a long-context
    model. A second `len // 4` here would let the broker trim to a budget measured
    one way while the model was chosen against another — and because each half
    stays self-consistent, nothing would ever surface the disagreement.
    """
    seen: list[str] = []

    def fake(text):
        seen.append(text)
        return 4242

    monkeypatch.setattr(RT, "estimate_tokens", fake)
    assert SRC.token_cost("hello world") == 4242
    assert seen == ["hello world"]


def test_token_cost_survives_a_broken_estimator(monkeypatch):
    """Total: the fallback is the same arithmetic, and only reachable in a crash."""
    def boom(_text):
        raise RuntimeError("no router")

    monkeypatch.setattr(RT, "estimate_tokens", boom)
    assert SRC.token_cost("x" * 40) == 10
    assert SRC.token_cost(None) == 0


def test_bundle_tokens_include_the_conversation_the_broker_never_reads():
    """The conversation is an accounting item — its size counts, its rows do not."""
    bundle = B.assemble(chat_id="c-tok", conversation_tokens=500,
                        conversation_messages=7)
    items = sum(it.tokens() for it in bundle.items)
    assert bundle.total_tokens() == items + 500
    assert bundle.to_payload()["tokens"] == bundle.total_tokens()


def test_an_item_can_report_its_cost_before_it_is_measured():
    raw = B.ContextItem(source=B.SOURCE_FILES, text="y" * 80)
    assert raw.token_cost is None
    assert raw.tokens() == 20


# ── freshness: only a source that serves cached data can be stale ──────────────

def test_ttl_for_git_is_read_live_from_gitstate(monkeypatch):
    """⚠️ Sabotage-verified in spirit: a literal `15.0` here would go stale silently.

    Raise the window in `gitstate` and a copied constant keeps reporting a
    two-minute-old snapshot as perfectly fresh.
    """
    monkeypatch.setattr(G, "GIT_TTL", 99.0)
    assert SRC.ttl_for(B.SOURCE_GIT) == 99.0


def test_every_other_source_is_read_live_so_it_cannot_be_stale():
    for s in B.ORDER:
        if s == B.SOURCE_GIT:
            continue
        assert SRC.ttl_for(s) == 0.0
        assert SRC.freshness_of(s, stamp=time.time() - 10_000) == 1.0


def test_freshness_decays_across_the_ttl_and_floors_at_zero():
    now = 1_000_000.0
    ttl = SRC.ttl_for(B.SOURCE_GIT)
    assert SRC.freshness_of(B.SOURCE_GIT, now, now=now) == 1.0
    mid = SRC.freshness_of(B.SOURCE_GIT, now - ttl / 2, now=now)
    assert 0.4 < mid < 0.6
    assert SRC.freshness_of(B.SOURCE_GIT, now - ttl * 3, now=now) == 0.0


def test_an_unstamped_or_future_reading_is_fresh_not_an_error():
    """Clock skew is not a fact about context; there is nothing useful to report."""
    now = 1_000_000.0
    assert SRC.freshness_of(B.SOURCE_GIT, 0.0, now=now) == 1.0
    assert SRC.freshness_of(B.SOURCE_GIT, now + 500, now=now) == 1.0
    assert SRC.freshness_of("nonsense", "not-a-number") == 1.0


def test_the_git_item_is_stamped_from_the_snapshot_not_from_collect_time(monkeypatch):
    """⚠️ Git is the one source that can serve something it read earlier.

    Stamping "now" would make a `GIT_TTL`-old snapshot indistinguishable from one
    read this instant, which is the entire content of the freshness measure.
    """
    old = dict(G.empty(), repo=True, branch="main", at=time.time() - G.GIT_TTL * 0.8,
               commits=[])
    monkeypatch.setattr(G, "snapshot", lambda *a, **k: old)
    monkeypatch.setattr(G, "describe", lambda *a, **k: "main · clean")
    bundle = B.assemble(chat_id="c-git", include=frozenset((B.SOURCE_GIT,)))
    item = bundle.get(B.SOURCE_GIT)[0]
    assert item.stamp == old["at"]
    assert 0.0 < item.freshness < 0.5


# ── relevance: unknown is not zero ─────────────────────────────────────────────

def test_the_standing_sources_are_always_fully_relevant():
    """⚠️ Scoring the user's own rules by word overlap would rank them irrelevant
    to most questions — the one conclusion a budget must never be able to reach."""
    assert SRC.relevance_of(B.SOURCE_RULES, "\n\n## RULES\nuse tabs",
                            "deploy the website") == 1.0
    assert SRC.relevance_of(B.SOURCE_MEMORY, "anything", "unrelated words") == 1.0
    assert SRC.relevance_of(B.SOURCE_FILES, "anything", "unrelated",
                            pinned=True) == 1.0


def test_no_message_means_unknown_relevance_not_zero():
    """A zero would be the first thing a budget dropped, on no evidence at all."""
    r = SRC.relevance_of(B.SOURCE_GIT, "\n\n## GIT STATE\nbranch main", "")
    assert r == SRC.UNKNOWN_RELEVANCE
    assert r > 0.0


def test_relevance_rises_with_overlap_and_never_leaves_the_unit_range():
    msg = "fix the failing scanner tests in the broker"
    hit = SRC.relevance_of(B.SOURCE_FILES, "broker scanner tests failing", msg)
    miss = SRC.relevance_of(B.SOURCE_FILES, "unrelated prose about cooking", msg)
    assert SRC.BASE_RELEVANCE <= miss < hit <= 1.0
    assert SRC.relevance_of(B.SOURCE_FILES, "", msg) == SRC.BASE_RELEVANCE


def test_relevance_is_deterministic():
    """Two identical inputs must score identically — a budget is not a dice roll."""
    args = (B.SOURCE_FILES, "the scanner writes report.json", "scanner report")
    assert SRC.relevance_of(*args) == SRC.relevance_of(*args)


# ── measure(): one place, and it never overwrites a stated value ───────────────

def test_collect_measures_every_item_it_accepts():
    """A source registered by a later phase gets measured without knowing it must."""
    original = B._COLLECTORS[B.SOURCE_SKILLS]
    try:
        B.register(B.SOURCE_SKILLS, lambda _r: [B.ContextItem(
            source=B.SOURCE_SKILLS, text="\n\n## SKILLS\n- audit", label="Skills")])
        bundle = B.assemble(chat_id="c-measure", message="audit this")
        assert bundle.items, "nothing collected — the rest of this test is vacuous"
        for it in bundle.items:
            assert it.priority is not None, f"{it.source} unmeasured: priority"
            assert it.relevance is not None, f"{it.source} unmeasured: relevance"
            assert it.token_cost is not None, f"{it.source} unmeasured: token_cost"
            assert it.freshness is not None, f"{it.source} unmeasured: freshness"
    finally:
        B.register(B.SOURCE_SKILLS, original)


def test_a_collector_that_states_a_measure_keeps_it():
    """⚠️ `None` means "not stated" for exactly this reason.

    Phase 11's skills source computes its own relevance from the request. A generic
    word-overlap pass that overwrote it would silently replace a real judgement
    with a word count — and 0.0 as the default would be indistinguishable from a
    measured zero.
    """
    original = B._COLLECTORS[B.SOURCE_SKILLS]
    try:
        B.register(B.SOURCE_SKILLS, lambda _r: [B.ContextItem(
            source=B.SOURCE_SKILLS, text="\n\n## SKILLS\n- audit",
            relevance=0.99, priority=7, token_cost=3, freshness=0.5)])
        item = B.assemble(chat_id="c-stated", message="nothing in common").get(
            B.SOURCE_SKILLS)[0]
        assert (item.relevance, item.priority, item.token_cost, item.freshness) == \
            (0.99, 7, 3, 0.5)
    finally:
        B.register(B.SOURCE_SKILLS, original)


def test_measure_is_total_on_something_that_is_not_an_item():
    """A measure is an ordering hint; losing one may never cost the turn."""
    class Odd:
        source = "weird"

    odd = Odd()
    assert SRC.measure(odd, None) is odd
    assert SRC.measure_all([], None) == []


def test_measure_fills_a_partially_stated_item():
    it = SRC.measure(B.ContextItem(source=B.SOURCE_FILES, text="abcd" * 10,
                                   relevance=0.42), B.request(message="files"))
    assert it.relevance == 0.42                      # stated, kept
    assert it.priority == SRC.priority_of(B.SOURCE_FILES)
    assert it.token_cost == 10
    assert it.freshness == 1.0


# ── rank(): the ONE survival ordering ─────────────────────────────────────────

def test_rank_puts_pinned_first_even_at_a_lower_priority():
    pinned_low = B.ContextItem(source="x", pinned=True, priority=1)
    loose_high = B.ContextItem(source="y", pinned=False, priority=100)
    assert SRC.rank([loose_high, pinned_low])[0] is pinned_low


def test_rank_falls_back_through_priority_then_relevance_then_freshness():
    a = B.ContextItem(source="a", priority=50, relevance=0.9, freshness=1.0)
    b = B.ContextItem(source="b", priority=50, relevance=0.1, freshness=1.0)
    c = B.ContextItem(source="c", priority=60, relevance=0.1, freshness=1.0)
    d = B.ContextItem(source="d", priority=50, relevance=0.9, freshness=0.2)
    assert [i.source for i in SRC.rank([b, d, a, c])] == ["c", "a", "d", "b"]


def test_rank_is_stable_and_loses_nothing():
    items = [B.ContextItem(source=f"s{i}", priority=5) for i in range(6)]
    ranked = SRC.rank(items)
    assert [i.source for i in ranked] == [i.source for i in items]
    assert len(ranked) == len(items)
    assert {id(i) for i in ranked} == {id(i) for i in items}


def test_the_bundle_ranks_through_sources_and_does_not_sort_twice():
    """A second sort is how a report comes to disagree with the trim it describes."""
    bundle = B.assemble(chat_id="c-rank", message="anything")
    assert [id(i) for i in bundle.ranked()] == [id(i) for i in SRC.rank(bundle.items)]


def test_the_payload_reports_the_measures_and_still_never_the_text():
    payload = B.assemble(chat_id="c-pay", message="report").to_payload()
    assert payload["ranked"], "the survival order is missing from the report"
    for item in payload["items"]:
        assert "text" not in item
        for field_name in ("priority", "relevance", "tokens", "freshness"):
            assert field_name in item, f"{field_name} missing from the payload"


# ══════════════════════════════════════════════════════════════════════════════
# Task 22 — the budget: what fits, and what is left out
# ══════════════════════════════════════════════════════════════════════════════

def _item(source, text="", *, pinned=False, tokens=None, priority=None):
    """A measured item, so a budget test never depends on the estimator's arithmetic."""
    return B.ContextItem(source=source, text=text, label=source, pinned=pinned,
                         token_cost=tokens, priority=priority, relevance=1.0,
                         freshness=1.0)


# ── The ceiling is DERIVED — three numbers, none of them declared here ─────────

def test_the_ceiling_comes_from_the_model_window_and_is_smaller_than_it():
    lim = BD.limits("2.5-flash", "pro")
    assert lim["window"] == CAPS.context_window("2.5-flash") > 0
    assert lim["basis"] == BD.BASIS_MODEL
    assert lim["window_known"] is True
    assert 0 < lim["limit"] < lim["window"], (
        "the ceiling must leave room for the output and the static prompt")
    assert lim["limit"] == lim["window"] - lim["output"] - lim["reserve"]


def test_the_output_allowance_is_read_from_the_mode_table():
    """⚠️ `config.MODES` declares 2 048 / 8 192 / 16 384 exactly once.

    A copy of those numbers in the budget would keep working and reserve the wrong
    amount for whichever mode was edited — and the direction that goes wrong is the
    one that overflows the window mid-turn.
    """
    from agent2 import config
    for mode, row in config.MODES.items():
        assert BD.limits("2.5-flash", mode)["output"] == row["max_tokens"], (
            f"mode {mode} reserved something other than its declared max_tokens")


def test_an_unknown_mode_reserves_the_largest_declared_allowance():
    """Reserving too little is what overflows, so an unstated mode assumes the worst."""
    from agent2 import config
    biggest = max(r["max_tokens"] for r in config.MODES.values())
    assert BD.limits("2.5-flash", "no-such-mode")["output"] == biggest
    assert BD.limits("2.5-flash", "")["output"] == biggest


def test_an_unknown_window_is_assumed_and_reported_as_assumed():
    """⚠️ `context_window()` returns 0 for "unknown", and 0 is never "small".

    A budget still needs a number, so the assumption is made — and named. A surface
    that could not tell an assumed ceiling from a read one would report a limit as
    fact when nobody has ever recorded that model's window.
    """
    assert CAPS.context_window("no-such-model") == 0
    lim = BD.limits("no-such-model", "pro")
    assert lim["window"] == BD.ASSUMED_WINDOW
    assert lim["window_known"] is False
    assert lim["basis"] == BD.BASIS_ASSUMED
    assert lim["limit"] > 0, "an unknown model must still get a workable ceiling"


def test_an_empty_model_key_budgets_the_default_model_not_an_assumption():
    """An unnamed turn runs `config.DEFAULT_MODEL`, so it is budgeted as that model.

    Reading "" as unknown would put a 32k ceiling on a million-token model for every
    surface that does not name its model — a silent, permanent trim.
    """
    from agent2.config import DEFAULT_MODEL
    lim = BD.limits("", "pro")
    assert lim["basis"] == BD.BASIS_MODEL
    assert lim["window"] == CAPS.context_window(DEFAULT_MODEL)


def test_neither_reservation_may_eat_the_whole_window(monkeypatch):
    """An 8k model in `thinking` mode is incoherent; the ceiling must survive it."""
    monkeypatch.setattr(BD, "window_for", lambda key="": 8_000)
    lim = BD.limits("tiny", "thinking")
    assert lim["output"] <= 4_000, "the output allowance took more than half the window"
    assert lim["reserve"] <= 2_000
    assert lim["limit"] >= BD.MIN_LIMIT
    assert lim["limit"] > 0, (
        "a ceiling of zero would drop every situational source on every turn")


def test_a_derived_ceiling_never_falls_below_the_floor(monkeypatch):
    monkeypatch.setattr(BD, "window_for", lambda key="": 40)
    assert BD.limits("micro", "thinking")["limit"] == BD.MIN_LIMIT


def test_the_env_ceiling_is_used_verbatim_and_never_floored(monkeypatch):
    """An operator who wrote a number down made a decision. Raising it would lie."""
    monkeypatch.setenv("AGENT2_CONTEXT_BUDGET", "1234")
    lim = BD.limits("2.5-flash", "pro")
    assert lim["limit"] == 1234
    assert lim["basis"] == BD.BASIS_ENV
    monkeypatch.setenv("AGENT2_CONTEXT_BUDGET", "7")
    assert BD.limits("2.5-flash", "pro")["limit"] == 7 < BD.MIN_LIMIT


def test_both_env_knobs_are_read_at_call_time_not_at_import(monkeypatch):
    """Read at import, an override could not be tested or reported honestly."""
    wide = BD.limits("2.5-flash", "pro")["limit"]
    monkeypatch.setenv("AGENT2_CONTEXT_RESERVE", str(BD.RESERVE_TOKENS + 50_000))
    assert BD.limits("2.5-flash", "pro")["limit"] < wide
    assert BD.describe()["reserve"] == BD.RESERVE_TOKENS + 50_000


# ── Pinned items are never dropped ─────────────────────────────────────────────

def test_pinned_items_are_never_dropped_even_alone_over_the_limit():
    """⚠️ THE ONE RULE THE BUDGET MAY NOT BREAK.

    Trimming until the arithmetic looks right means the user's own standing rules are
    what a budget silently discards. An honestly-too-large prompt fails at the vendor
    with something an operator can read; a prompt that quietly lost its rules
    produces confident wrong behaviour nobody can trace to a cause.
    """
    rules = _item(B.SOURCE_RULES, "R" * 4_000, pinned=True, tokens=1_000)
    mem = _item(B.SOURCE_MEMORY, "M" * 4_000, pinned=True, tokens=1_000)
    loose = _item(B.SOURCE_GIT, "G" * 400, tokens=100)
    p = BD.plan([loose, rules, mem], limit=10)
    assert rules in p.kept and mem in p.kept
    assert p.dropped == [loose]
    assert p.over is True
    assert p.overflow == p.used - p.limit > 0


def test_over_budget_is_reported_rather_than_fixed():
    p = BD.plan([_item(B.SOURCE_RULES, "R" * 400, pinned=True, tokens=100)], limit=5)
    payload = p.to_payload()
    assert payload["over"] is True and payload["overflow"] == 95
    assert payload["kept"] == [B.SOURCE_RULES]
    assert payload["dropped"] == []


def test_the_conversation_counts_against_the_ceiling_and_is_never_trimmed():
    """History is bounded by `MAX_CTX_MESSAGES` in `build_context`, not here.

    Two components dropping turns from one history is a state neither can report.
    So the broker charges for the conversation and never touches it — which is
    exactly why a long session squeezes out the situational sources first.
    """
    conv = _item(B.SOURCE_CONVERSATION, "", pinned=True, tokens=0)
    doc = _item(B.SOURCE_PROJECT, "D" * 400, tokens=100)
    p = BD.plan([conv, doc], limit=150, conversation_tokens=120)
    assert conv in p.kept
    assert p.dropped == [doc], "the conversation's size must crowd out other sources"
    assert p.used == 120 and p.conversation == 120


# ── The fill ───────────────────────────────────────────────────────────────────

def test_an_item_that_does_not_fit_is_skipped_not_a_stop_sign():
    """⚠️ One oversized source may not strip every source below it.

    Stopping at the first item that did not fit would leave most of the budget
    unspent and let the size of one block decide the fate of unrelated ones.
    """
    big = _item(B.SOURCE_TASKS, "T" * 4_000, tokens=1_000, priority=70)
    small = _item(B.SOURCE_GIT, "G" * 40, tokens=10, priority=30)
    p = BD.plan([big, small], limit=100)
    assert p.kept == [small] and p.dropped == [big]
    assert p.used == 10


def test_a_zero_cost_item_is_kept_even_over_budget():
    """Dropping something free buys nothing and would report a trim that never was."""
    free = _item(B.SOURCE_MCP, "", tokens=0)
    p = BD.plan([_item(B.SOURCE_RULES, "R", pinned=True, tokens=500), free], limit=1)
    assert free in p.kept and p.dropped == []


def test_the_trim_consumes_the_one_ranking_and_does_not_sort_again(monkeypatch):
    """⚠️ A second ordering makes the report describe a trim that did not happen."""
    calls = []
    real = SRC.rank

    def spy(items):
        calls.append(list(items))
        return real(items)

    monkeypatch.setattr(SRC, "rank", spy)
    BD.plan([_item(B.SOURCE_GIT, "g", tokens=1)], limit=1_000)
    assert calls, "budget.plan() ordered items without going through sources.rank()"


def test_priority_decides_who_survives_not_collection_order():
    """`git_state` is collected before `task_results`; it must still be cut first."""
    git = _item(B.SOURCE_GIT, "G" * 400, tokens=100,
                priority=SRC.priority_of(B.SOURCE_GIT))
    tasks = _item(B.SOURCE_TASKS, "T" * 400, tokens=100,
                  priority=SRC.priority_of(B.SOURCE_TASKS))
    p = BD.plan([git, tasks], limit=100)
    assert p.kept == [tasks] and p.dropped == [git]


def test_a_real_bundle_on_a_real_model_drops_nothing():
    """The budget must not be a trim in normal operation — a million-token window."""
    bundle = B.assemble(chat_id="c-fits", message="hello", model_key="2.5-flash",
                        mode_key="pro")
    assert bundle.plan is not None
    assert bundle.plan.dropped == [], "a normal turn was trimmed on a 1M window"
    assert bundle.plan.over is False


# ── Wiring: a surface cannot forget to budget ─────────────────────────────────

def test_assemble_always_plans_a_budget():
    """⚠️ Applied in `assemble()`, not by the caller.

    Two loops assemble a bundle today and Phases 11–13 add more. "Remember to
    budget it" is exactly the step a new caller omits, and the omission is invisible
    until the one turn that overflows.
    """
    bundle = B.assemble(chat_id="c-plan", message="anything")
    assert bundle.plan is not None
    assert bundle.plan.limit > 0


def test_collect_is_still_pure_gathering():
    """The untrimmed bundle has to remain observable, or no report can show a trim."""
    bundle = B.collect(B.request(chat_id="c-raw"))
    assert bundle.plan is None
    assert bundle.sent() == bundle.items, "no plan must mean nothing was trimmed"


def test_the_prompt_tail_renders_what_fit_and_keeps_the_pinned_blocks(monkeypatch):
    M.add_memory("budget-visible fact", importance=9, tags="test")
    B.MEM_CACHE.invalidate()
    monkeypatch.setenv("AGENT2_CONTEXT_BUDGET", "1")
    bundle = B.assemble(chat_id="c-trim", message="anything", model_key="2.5-flash")
    tail = bundle.prompt_tail()
    assert bundle.plan.dropped, "a 1-token ceiling dropped nothing"
    assert "budget-visible fact" in tail, "a pinned block was trimmed"
    assert "## PROJECT CONTEXT" not in tail, "a dropped source still rendered"


def test_a_dropped_source_is_not_reported_as_used(monkeypatch):
    monkeypatch.setenv("AGENT2_CONTEXT_BUDGET", "1")
    bundle = B.assemble(chat_id="c-used", message="anything", model_key="2.5-flash")
    for source in bundle.plan.dropped_sources():
        assert source not in bundle.sources_used(), (
            f"{source} was dropped but is still reported as part of the prompt")


def test_the_model_is_told_which_sources_were_omitted(monkeypatch):
    """⚠️ The same reasoning `_collect_mcp` uses: absence is read as "nothing there".

    An agent that cannot see the files it changed concludes it changed none. Naming
    the omitted sources costs ~30 tokens and turns a silent gap into something the
    model can close with a tool call.
    """
    monkeypatch.setenv("AGENT2_CONTEXT_BUDGET", "1")
    bundle = B.assemble(chat_id="c-note", message="anything", model_key="2.5-flash")
    tail = bundle.prompt_tail()
    assert "## CONTEXT OMITTED" in tail
    for source in bundle.plan.dropped_sources():
        assert source in tail, f"{source} was dropped without telling the model"


def test_nothing_dropped_means_no_notice_at_all():
    bundle = B.assemble(chat_id="c-quiet", message="anything", model_key="2.5-flash")
    assert bundle.plan.dropped == []
    assert BD.notice(bundle.plan) == ""
    assert "## CONTEXT OMITTED" not in bundle.prompt_tail()


def test_the_notice_is_not_a_source():
    """It describes the tail rather than contributing to it, so it has no ORDER slot."""
    assert "context_omitted" not in B.ORDER
    p = BD.plan([_item(B.SOURCE_GIT, "G" * 40, tokens=10)], limit=1)
    assert p.kept_sources() == [] and p.dropped_sources() == [B.SOURCE_GIT]
    assert BD.notice(p).startswith("\n\n## CONTEXT OMITTED")


def test_the_collected_total_and_the_sent_total_are_different_numbers(monkeypatch):
    """A trim that did not show up in its own accounting would be invisible."""
    monkeypatch.setenv("AGENT2_CONTEXT_BUDGET", "1")
    bundle = B.assemble(chat_id="c-two", message="anything", model_key="2.5-flash")
    assert bundle.plan.dropped
    assert bundle.total_tokens() > bundle.plan.used, (
        "total_tokens() must still report what was COLLECTED, not what was sent")


def test_a_bundle_the_caller_built_by_hand_renders_everything():
    """No plan is not an empty plan — see the `plan` field."""
    bundle = B.ContextBundle(request=B.request(chat_id="c-hand"))
    bundle.items.append(_item(B.SOURCE_GIT, "\n\nHAND", tokens=9_999_999))
    assert bundle.plan is None
    assert "HAND" in bundle.prompt_tail()
    assert bundle.dropped() == []


# ── Totality and reporting ────────────────────────────────────────────────────

def test_the_budget_keeps_everything_when_it_cannot_be_computed(monkeypatch):
    """⚠️ Fails OPEN on purpose. Failing closed would let an accounting bug strip
    the user's rules — the one outcome this module exists to prevent."""
    monkeypatch.setattr(BD, "limits", lambda *a, **k: 1 / 0)
    items = [_item(B.SOURCE_RULES, "R", pinned=True, tokens=5),
             _item(B.SOURCE_GIT, "G", tokens=5)]
    p = BD.plan(items, model_key="2.5-flash")
    assert p.kept == items and p.dropped == []


def test_an_item_whose_measure_explodes_is_still_kept():
    class Boom:
        source = B.SOURCE_FILES
        label = "boom"
        pinned = False
        text = "x"

        def tokens(self):
            raise RuntimeError("measure exploded")

    p = BD.plan([Boom()], limit=100)
    assert len(p.kept) == 1 and p.dropped == []
    assert p.to_payload()["dropped_items"] == []


def test_planning_something_that_is_not_a_list_is_not_an_error():
    for junk in (None, [], ()):
        p = BD.plan(junk, limit=10)
        assert p.kept == [] and p.dropped == []


def test_the_payload_reports_the_plan_and_still_never_the_text(monkeypatch):
    monkeypatch.setenv("AGENT2_CONTEXT_BUDGET", "1")
    payload = B.assemble(chat_id="c-bp", message="anything",
                         model_key="2.5-flash").to_payload()
    budget = payload["budget"]
    assert budget is not None
    for key in ("limit", "used", "basis", "window", "window_known", "over",
                "overflow", "kept", "dropped", "conversation", "pinned"):
        assert key in budget, f"{key} missing from the budget report"
    for row in budget["dropped_items"]:
        assert "text" not in row


def test_an_unbudgeted_bundle_reports_none_rather_than_an_empty_trim():
    payload = B.collect(B.request(chat_id="c-none")).to_payload()
    assert payload["budget"] is None, (
        "'nobody planned a budget' and 'nothing was dropped' must not look alike")


def test_stats_reports_the_budget_policy_it_is_applying():
    assert B.stats()["budget"] == BD.describe()


def test_a_trim_is_never_silent(monkeypatch):
    """A trim logged per surface ends up logged by only one of them."""
    seen = []
    from agent2.core import logging as alog
    monkeypatch.setattr(alog, "context_trimmed",
                        lambda *a, **k: seen.append((a, k)))
    monkeypatch.setenv("AGENT2_CONTEXT_BUDGET", "1")
    bundle = B.assemble(chat_id="c-log", message="anything", model_key="2.5-flash")
    assert bundle.plan.dropped
    assert seen, "context was dropped and nothing was logged"
    assert B.SOURCE_PROJECT in seen[0][0][0]


def test_a_plan_that_kept_everything_logs_nothing(monkeypatch):
    seen = []
    from agent2.core import logging as alog
    monkeypatch.setattr(alog, "context_trimmed",
                        lambda *a, **k: seen.append((a, k)))
    B.assemble(chat_id="c-nolog", message="anything", model_key="2.5-flash")
    assert seen == [], "a normal turn logged a trim that did not happen"

