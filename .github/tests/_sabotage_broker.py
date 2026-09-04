"""Sabotage harness: break one invariant, prove the test that pins it goes red.

Not part of the suite. Run from the repo root:
    .venv\\Scripts\\python.exe .github/tests/_sabotage_broker.py
Each case edits a real source file, runs the one test that should catch it, then
restores the file byte-for-byte from the in-memory original.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable

CASES = [
    # (file, find, replace, test node id, what the sabotage models)
    (
        "agent2/agent.py",
        "_MEM_CACHE = _broker.MEM_CACHE",
        '_MEM_CACHE = __import__("agent2.core.sync", fromlist=["x"]).VersionedCache('
        '"memories", _broker._build_memory_block)',
        ".github/tests/test_broker.py::test_agent_prompt_caches_are_the_brokers_not_copies",
        "agent keeps its OWN memories cache (a copy, not the broker's)",
    ),
    (
        "agent2/core/broker/__init__.py",
        """    bundle = ContextBundle(request=req)
    for source in ORDER:
        fn = table.get(source)
        if fn is None or not req.wants(source):
            continue
        try:
            got = fn(req) or []
        except Exception as exc:                      # never into a turn
            bundle.errors[source] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        for it in got:
            if isinstance(it, ContextItem):
                bundle.items.append(_sources.measure(it, req))
    return bundle""",
        """    bundle = ContextBundle(request=req)
    try:
        for source in ORDER:
            fn = table.get(source)
            if fn is None or not req.wants(source):
                continue
            got = fn(req) or []
            for it in got:
                if isinstance(it, ContextItem):
                    bundle.items.append(_sources.measure(it, req))
    except Exception as exc:
        bundle.errors["*"] = f"{type(exc).__name__}: {exc}"[:200]
    return bundle""",
        ".github/tests/test_broker.py::test_one_broken_collector_cannot_cost_the_user_their_rules",
        "ONE guard around the whole loop instead of one per collector",
    ),
    (
        "agent2/agent.py",
        "    sp += context.prompt_tail() if context is not None else _broker.base_tail()",
        "    sp += _broker.assemble().prompt_tail()",
        ".github/tests/test_broker.py::test_system_prompt_never_assembles_a_bundle_itself",
        "system_prompt assembles the bundle itself instead of taking it as a parameter",
    ),
    # ── Task 21 ────────────────────────────────────────────────────────────────
    (
        "agent2/core/broker/sources.py",
        "        from agent2.llm import router as _router\n"
        "        return max(0, int(_router.estimate_tokens(text)))",
        "        return max(0, len(str(text or '')) // 4)",
        ".github/tests/test_broker.py::test_token_cost_delegates_to_the_router_estimator",
        "a second token estimator instead of the router's",
    ),
    (
        "agent2/core/broker/__init__.py",
        "SOURCE_GIT          = _sources.SOURCE_GIT",
        'SOURCE_GIT          = "git_state"',
        ".github/tests/test_broker.py::test_the_source_names_are_re_exported_not_re_declared",
        "a source name re-declared as a literal (interning hides it from `is`)",
    ),
    (
        "agent2/core/broker/sources.py",
        "            from agent2.core import gitstate as _git\n"
        "            return max(0.0, float(_git.GIT_TTL))",
        "            return 15.0",
        ".github/tests/test_broker.py::test_ttl_for_git_is_read_live_from_gitstate",
        "git's freshness window copied instead of read from gitstate",
    ),
    (
        "agent2/core/broker/__init__.py",
        "                bundle.items.append(_sources.measure(it, req))",
        "                bundle.items.append(it)",
        ".github/tests/test_broker.py::test_collect_measures_every_item_it_accepts",
        "collect() stops measuring, so a later phase's source ranks last unmeasured",
    ),
    (
        "agent2/core/broker/sources.py",
        "        if pinned or str(source) in ALWAYS:\n            return 1.0",
        "        pass",
        ".github/tests/test_broker.py::test_the_standing_sources_are_always_fully_relevant",
        "the user's own rules scored by word overlap like any other source",
    ),
    # ── Task 22 ────────────────────────────────────────────────────────────────
    (
        "agent2/core/broker/__init__.py",
        "    bundle = collect(request(**kwargs))\n    _budget.apply(bundle)\n    return bundle",
        "    return collect(request(**kwargs))",
        ".github/tests/test_broker.py::test_assemble_always_plans_a_budget",
        "assemble() forgets to budget, so every prompt injects everything again",
    ),
    (
        "agent2/core/broker/__init__.py",
        "        return _render(self.sent()) + _budget.notice(self.plan)",
        "        return _render(self.items) + _budget.notice(self.plan)",
        ".github/tests/test_broker.py"
        "::test_the_prompt_tail_renders_what_fit_and_keeps_the_pinned_blocks",
        "prompt_tail() renders everything COLLECTED, so the plan is decoration",
    ),
    (
        "agent2/core/broker/__init__.py",
        "        return _render(self.sent()) + _budget.notice(self.plan)",
        "        return _render(self.sent())",
        ".github/tests/test_broker.py::test_the_model_is_told_which_sources_were_omitted",
        "a source vanishes from the prompt with no word to the model",
    ),
    (
        "agent2/core/broker/__init__.py",
        "        got = {it.source for it in self.sent() if it.rendered}",
        "        got = {it.source for it in self.items if it.rendered}",
        ".github/tests/test_broker.py::test_a_dropped_source_is_not_reported_as_used",
        "the report describes a prompt that was never sent",
    ),
    (
        "agent2/core/broker/budget.py",
        '        pinned = [i for i in ranked if bool(getattr(i, "pinned", False))]\n'
        '        loose = [i for i in ranked if not bool(getattr(i, "pinned", False))]',
        "        pinned = []\n        loose = list(ranked)",
        ".github/tests/test_broker.py"
        "::test_pinned_items_are_never_dropped_even_alone_over_the_limit",
        "the budget trims until the numbers work — and drops the user's rules",
    ),
    (
        "agent2/core/broker/budget.py",
        "            else:\n                out.dropped.append(item)",
        "            else:\n"
        "                out.dropped.extend(loose[loose.index(item):])\n"
        "                break",
        ".github/tests/test_broker.py::test_an_item_that_does_not_fit_is_skipped_not_a_stop_sign",
        "one oversized source strips every source below it",
    ),
    (
        "agent2/core/broker/budget.py",
        '        if isinstance(row, dict) and int(row.get("max_tokens") or 0) > 0:\n'
        '            return int(row["max_tokens"])',
        "        if isinstance(row, dict):\n            return 8192",
        ".github/tests/test_broker.py::test_the_output_allowance_is_read_from_the_mode_table",
        "the mode's max_tokens copied as a literal instead of read from config.MODES",
    ),
    (
        "agent2/core/broker/budget.py",
        '        "basis": BASIS_MODEL if known else BASIS_ASSUMED,',
        '        "basis": BASIS_MODEL,',
        ".github/tests/test_broker.py::test_an_unknown_window_is_assumed_and_reported_as_assumed",
        "an assumed ceiling reported as a known one",
    ),
    (
        "agent2/core/broker/budget.py",
        '        return {"limit": forced, "basis": BASIS_ENV, "window": window,',
        '        return {"limit": max(MIN_LIMIT, forced), "basis": BASIS_ENV, "window": window,',
        ".github/tests/test_broker.py::test_the_env_ceiling_is_used_verbatim_and_never_floored",
        "the operator's explicit ceiling quietly raised to the floor",
    ),
]


def main() -> int:
    bad = 0
    for rel, find, repl, node, why in CASES:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        if find not in original:
            print(f"SKIP  {rel}: anchor not found — sabotage harness is stale")
            bad += 1
            continue
        try:
            path.write_text(original.replace(find, repl, 1), encoding="utf-8")
            proc = subprocess.run([PY, "-m", "pytest", node, "-q", "--no-header",
                                   "-p", "no:cacheprovider"],
                                  cwd=str(ROOT), capture_output=True, text=True,
                                  # A failing assertion's message can contain the
                                  # docstrings' em dashes, and cp1252 cannot decode
                                  # what the child encoded as UTF-8 — the reader
                                  # thread then dies and the harness reports nothing
                                  # about the very case that went red.
                                  encoding="utf-8", errors="replace")
        finally:
            path.write_text(original, encoding="utf-8")
        if proc.returncode == 0:
            print(f"TAUTOLOGY  {node}\n  stayed GREEN with: {why}")
            bad += 1
        else:
            print(f"ok  {node}\n  went RED with: {why}")
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
