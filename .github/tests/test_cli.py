# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Characterization tests for agent2cli.py.

⚠️ WHY THIS FILE EXISTS
────────────────────────
`agent2cli.py` is 3,398 lines and, until this file, had **zero** test coverage:
it is launched by *path* as a subprocess (run.py / agent2dual.py) and imported
by nothing, so no existing test touched it. Every other performance/cleanup item
in this project was accepted by sabotage — break the guard, watch a test go red.
A refactor of this module had nothing that could go red.

These tests are deliberately **characterization** tests: they pin the CURRENT
observable behaviour so the planned refactor (palette globals → shared object,
constants → agent2.config, rotator → agent2.llm.keys, split into a cli/ package)
is provably behaviour-preserving rather than merely "it still starts up".

Two invariants here are load-bearing and easy to break silently:

1. **The palette is mutable module state.** `/theme` and `/color` REBIND the
   module globals `PU`/`CY`/`MG`/`ACCENT`. Every colour helper reads them
   lazily, which is why a theme change is instant. The moment an extracted
   module does `from .theme import PU` it binds a *snapshot* and stops
   repainting — with no exception and no traceback. So these tests assert
   through the **helper functions** (`pu()`, `cy()`), never by reading the
   global directly: reading the global would still pass after that break.

2. **`status()` must never emit a whole key.** There is no auth on any Flask
   route and the CLI prints this straight to a terminal, so the redaction is a
   security property, not cosmetics.
"""

import importlib

import pytest

cli = importlib.import_module("agent2cli")
config = importlib.import_module("agent2.config")


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def palette():
    """Snapshot and restore the mutable palette.

    The theme-dependent channels live on the shared `cli.P` object; `GR`/`YW`/
    `RD`/`WH` are theme-independent constants and are asserted to STAY that way
    by test_apply_accent_overrides_the_primary_but_spares_semantic_colours.

    This is process-wide state, so a test that repaints it would leak into every
    later test in the session.

    The current theme NAME is one of those channels (`P.THEME`), so the slot loop
    restores it too. It used to need a second save/restore line of its own,
    because it was a `_CURRENT_THEME` module global — the one shape that could not
    survive the split into `agent2/cli/`, since an importer would have bound the
    startup value and `/theme` would have appeared to do nothing to it.
    """
    saved = {n: getattr(cli.P, n) for n in cli._Palette.__slots__}
    try:
        yield
    finally:
        for n, v in saved.items():
            setattr(cli.P, n, v)


@pytest.fixture
def fake_keys(monkeypatch):
    """Give the SHARED rotator a deterministic key set without touching the DB.

    ⚠️ These patch `agent2.llm.keys`, not `agent2cli`. The CLI no longer owns a
    rotator — it imports the package singleton — so the seams that matter are
    that module's `list_api_keys` (where entries come from) and `_client` (which
    otherwise needs google-genai installed and would make every assertion below
    pass vacuously against a None client).

    `_load_pin`/`_save_pin` are stubbed so instances start unpinned and cannot
    leak a pin into the next test through the shared settings table. Real
    persistence is covered by test_a_pin_survives_a_restart, which opts back in.
    """
    import agent2.llm.keys as keys_mod

    recs = [
        {"api_key": "AAAAAAAAAAAAAAAAAAAAkey-one", "label": "one", "active": 1, "name": "K1"},
        {"api_key": "BBBBBBBBBBBBBBBBBBBBkey-two", "label": "two", "active": 1, "name": "K2"},
        {"api_key": "CCCCCCCCCCCCCCCCCCCCkey-tre", "label": "tre", "active": 1, "name": "K3"},
    ]
    monkeypatch.setattr(keys_mod, "list_api_keys", lambda: [dict(r) for r in recs])
    monkeypatch.setattr(keys_mod, "qall", lambda *a, **k: [])
    monkeypatch.setattr(keys_mod, "genai", object())
    monkeypatch.setattr(keys_mod.KeyRotator, "_client", lambda self, k: f"client:{k}")
    monkeypatch.setattr(keys_mod.KeyRotator, "_load_pin", lambda self: None)
    monkeypatch.setattr(keys_mod.KeyRotator, "_save_pin", lambda self, label: None)
    return [{"key": r["api_key"], "label": r["label"]} for r in recs]


# ── Import safety ──────────────────────────────────────────────────────────────

def test_module_imports_without_a_tty_or_network():
    """The module must be importable in a bare test process.

    It runs real work at import time (mkdir ~/.agent2, init_db, and builds a
    KeyRotator). If any of that grew a hard dependency on a TTY, a network, or
    a populated DB, the CLI would become untestable again.
    """
    assert cli.ROOT.exists()
    assert isinstance(cli._rotator, cli.KeyRotator)


def test_optional_dependencies_degrade_to_flags_not_crashes():
    """Every optional import is a flag, never a hard failure — this is the
    documented failsafe posture (`--help` must work on a fresh machine)."""
    for flag in ("_RICH", "_PTK", "_GENAI", "_BURP_OK", "_DB_OK"):
        assert isinstance(getattr(cli, flag), bool), flag


# ── Constants vs agent2.config (pins the de-duplication step) ──────────────────

def test_cli_model_keys_match_config():
    """Kept as a characterization pin from BEFORE the de-duplication.

    It recorded the facts while `agent2cli` still carried its own literal MODELS
    dict, so the shapes could be unified without changing behaviour. It is now
    also the coarse guard that a re-declared local copy has to defeat.
    """
    assert set(cli.MODELS) == set(config.MODELS)


def test_cli_model_api_strings_match_config():
    """The CLI maps key→api-string; config maps key→dict. Different shapes, same
    facts. This pins the facts so the shapes can be unified safely."""
    for key, api in cli.MODELS.items():
        assert api == config.MODELS[key]["api"], key


def test_cli_mode_tokens_and_thinking_match_config():
    assert set(cli.MODES) == set(config.MODES)
    for name, spec in cli.MODES.items():
        ref = config.MODES[name]
        assert spec["max_tokens"] == ref["max_tokens"], name
        assert spec["thinking"] == ref["thinking"], name


def test_cli_defaults_match_config():
    """⚠️ ONE-DIRECTIONAL by design — and that is not a defect.

    `cli.DEFAULT_MODEL` is now imported from config, so changing config's value
    moves BOTH sides and this assertion cannot see it. That direction is covered
    by test_the_offline_fallback_cannot_drift_from_config, which compares the
    literal `_FALLBACK_*` copy against config; sabotage confirmed it goes red.

    What this test still owns is the direction that CAN drift silently: someone
    re-declaring a local default in agent2cli.py instead of taking config's.
    """
    assert cli.DEFAULT_MODEL == config.DEFAULT_MODEL
    assert cli.DEFAULT_MODE == config.DEFAULT_MODE


def test_cli_detect_shell_agrees_with_config():
    """Was two independent implementations; now one, imported.

    Kept because it is the behavioural pin that made the unification safe — the
    triple this returns decides how every `run_command` is executed, so a future
    local re-implementation must reproduce it exactly or go red.
    """
    assert cli.detect_shell() == config.detect_shell()


def test_shell_argv_wraps_the_command_for_the_current_platform():
    argv = cli.shell_argv("echo hi")
    assert argv[-1] == "echo hi"
    assert len(argv) == 3


def test_the_offline_fallback_cannot_drift_from_config():
    """⚠️ The fallback is still a COPY, so it is pinned too.

    MODELS/MODES are now derived from agent2.config, which makes the *normal*
    path drift-proof. But the `except` branch that keeps the CLI importable on a
    broken install carries literals, and an untested copy is exactly the hazard
    this change removes — it would simply have moved the drift somewhere nothing
    looks. A normal run never reaches these values; CI checks them anyway.
    """
    assert cli._FALLBACK_MODELS == {k: v["api"] for k, v in config.MODELS.items()}
    assert set(cli._FALLBACK_MODES) == set(config.MODES)
    for name, spec in cli._FALLBACK_MODES.items():
        assert spec["max_tokens"] == config.MODES[name]["max_tokens"], name
        assert spec["thinking"] == config.MODES[name]["thinking"], name
        assert spec["icon"] == config.MODES[name]["icon"], name
    assert cli._FALLBACK_DEFAULT_MODEL == config.DEFAULT_MODEL
    assert cli._FALLBACK_DEFAULT_MODE == config.DEFAULT_MODE


def test_models_is_derived_from_config_not_redeclared():
    """The point of the change: config is the source of truth, so the CLI's map
    must be the flattened form of config's specs — not a hand-maintained twin."""
    assert cli.MODELS == {k: v["api"] for k, v in config.MODELS.items()}
    assert cli.MODES is config.MODES, "MODES should be config's dict, not a copy"


# ── Palette: the silent-failure surface ────────────────────────────────────────

def test_apply_theme_repaints_through_the_helpers(palette):
    """⚠️ Asserts on `pu()` output, NOT on the `PU` global.

    A split that does `from .theme import PU` keeps the global correct in its
    home module while every helper elsewhere renders the stale colour. Reading
    the global would still pass; rendering through the helper would not.
    """
    assert cli.apply_theme("purple", persist=False) is True
    purple = cli.pu("x")
    assert cli.apply_theme("amber", persist=False) is True
    amber = cli.pu("x")
    assert purple != amber, "theme change did not reach the pu() helper"
    assert cli.THEMES["amber"]["PU"] in amber


def test_apply_theme_also_repaints_the_secondary_and_hex_accents(palette):
    """Each channel is asserted SEPARATELY on purpose.

    Comparing the three as one tuple let a sabotage that stopped repainting CY
    still pass, because ACCENT/ACCENT2 changing was enough to make the tuples
    differ. A per-channel assertion is what actually pins each one.
    """
    cli.apply_theme("purple", persist=False)
    cy_before, accent_before, accent2_before = cli.cy("x"), cli.P.ACCENT, cli.P.ACCENT2
    cli.apply_theme("ocean", persist=False)
    assert cli.cy("x") != cy_before, "secondary CY did not repaint"
    assert cli.P.ACCENT != accent_before, "hex ACCENT did not repaint"
    assert cli.P.ACCENT2 != accent2_before, "hex ACCENT2 did not repaint"
    assert cli.THEMES["ocean"]["CY"] in cli.cy("x")
    assert cli.P.ACCENT == cli.THEMES["ocean"]["accent"]


def test_apply_theme_records_the_current_theme(palette):
    cli.apply_theme("emerald", persist=False)
    assert cli.P.THEME == "emerald"


def test_unknown_theme_is_rejected_and_changes_nothing(palette):
    cli.apply_theme("rose", persist=False)
    keep = cli.pu("x")
    assert cli.apply_theme("chartreuse", persist=False) is False
    assert cli.pu("x") == keep, "a rejected theme still repainted the palette"


def test_apply_accent_overrides_the_primary_but_spares_semantic_colours(palette):
    """GR/YW/RD stay semantic across themes so success/warning/error cues never
    change meaning. An accent override must not touch them."""
    cli.apply_theme("purple", persist=False)
    green, yellow, red = cli.ok("x"), cli.warn("x"), cli.err("x")
    assert cli.apply_accent("teal", persist=False) is True
    assert cli.ACCENT_CHOICES["teal"][0] in cli.pu("x")   # primary really moved
    assert (cli.ok("x"), cli.warn("x"), cli.err("x")) == (green, yellow, red)


def test_apply_accent_accepts_a_raw_256_colour_code(palette):
    assert cli.apply_accent("42", persist=False) is True
    assert "38;5;42m" in cli.pu("x")


def test_apply_accent_rejects_garbage_and_out_of_range(palette):
    keep = cli.pu("x")
    for bad in ("", "not-a-colour", "999", "-1", None):
        assert cli.apply_accent(bad, persist=False) is False, bad
    assert cli.pu("x") == keep


def test_mutable_channels_live_only_on_the_shared_object():
    """⚠️ Regression guard for the whole reason `P` exists.

    If a module-level `PU`/`CY`/`MG`/`ACCENT` is reintroduced, two forms of the
    same value exist and can disagree — and, worse, `from .theme import PU`
    becomes available again as the *natural* import in an extracted cli/
    package. That import binds a snapshot: the colour freezes with no exception
    and no traceback, which is precisely the failure this refactor removed.
    """
    for name in cli._Palette.__slots__:
        assert hasattr(cli.P, name), f"{name} missing from the shared palette"
        assert not hasattr(cli, name), (
            f"{name} is a module global again — 'from .theme import {name}' "
            "would bind a snapshot and silently freeze that colour")


def test_no_function_shadows_the_palette_name():
    """⚠️ Regression guard for a bug the palette refactor actually SHIPPED.

    Five functions in `agent2cli.py` carried `from agent2.llm import providers
    as P` — harmless while the colours were module globals named `CY`/`PU`,
    because the shadow only hid a name nothing in those bodies read. Renaming
    those globals onto the shared palette turned every `{CY}` into `{P.CY}`,
    and inside those five bodies `P` was the providers MODULE:

      * `/provider list` and `/provider add` raised
        `AttributeError: module 'agent2.llm.providers' has no attribute 'CY'`.
      * Worse in `main()`: an assignment anywhere in a function makes the name
        local for the WHOLE function, so `/history` and `/search` — hundreds of
        lines above the import — raised `UnboundLocalError` instead.

    None of the 474 tests saw it: they assert through `pu()`/`cy()`, which live
    in a module with no such shadow. So this guard works on the SOURCE, and it
    is deliberately not limited to `providers`: any `as P` rebinding is the
    same trap. The fix is the alias `_prov`, which is also what
    `agent2/cli/interactive.py` uses.

    Every module in `agent2/cli/` is checked too, not just the entry point —
    the shadow is a property of the name, not of the file it lives in.
    """
    import ast
    import inspect
    import pkgutil

    import agent2.cli as clipkg

    modules = [cli] + [
        importlib.import_module(f"agent2.cli.{m.name}")
        for m in pkgutil.iter_modules(clipkg.__path__)
    ]

    offenders = []
    for mod in modules:
        tree = ast.parse(inspect.getsource(mod))
        where = mod.__name__
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                offenders += [f"{where}:{node.lineno} import … as P"
                              for a in node.names if a.asname == "P"]
            elif (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                  and node.id == "P" and where != "agent2.cli.theme"):
                # theme.py is where `P` is legitimately created.
                offenders.append(f"{where}:{node.lineno} assignment to P")

    assert not offenders, (
        "`P` is the shared palette and must never be rebound outside "
        "agent2/cli/theme.py — a local binding shadows it for the whole "
        "enclosing function: " + "; ".join(offenders))


def test_palette_rejects_a_misspelled_channel():
    """__slots__ turns a typo into an AttributeError instead of silently
    creating a dead attribute that every reader then ignores."""
    with pytest.raises(AttributeError):
        cli.P.NOT_A_CHANNEL = "x"


def test_semantic_colours_are_plain_constants():
    """GR/YW/RD/WH intentionally do NOT live on the palette: they must stay
    fixed across themes so success/warning/error cues never change meaning.
    Binding them by name in a split module is therefore safe."""
    for name in ("GR", "YW", "RD", "WH", "R", "B", "D"):
        assert isinstance(getattr(cli, name), str)
        assert name not in cli._Palette.__slots__


# ── Tool dispatch ──────────────────────────────────────────────────────────────

def test_unknown_tool_returns_the_registry_error_and_never_raises():
    """The agent loop feeds this straight back to the model. Raising here would
    kill the whole turn instead of costing one tool call."""
    out = cli.dispatch_tool("no_such_tool", {})
    assert out == {"error": 'Tool "no_such_tool" is not registered.'}


def test_shared_tools_are_delegated_to_the_package(monkeypatch):
    """The CLI must not re-implement the shared tools it lists in _SHARED_TOOLS —
    that is the drift hazard this whole cleanup is about."""
    import agent2.tools as shared

    seen = {}

    def spy(n, a, ctx=None):
        seen["call"] = (n, a)
        seen["ctx"] = ctx
        return {"ok": True}

    monkeypatch.setattr(shared, "dispatch_tool", spy)
    name = sorted(cli._SHARED_TOOLS)[0]
    assert cli.dispatch_tool(name, {"probe": 1}) == {"ok": True}
    assert seen["call"] == (name, {"probe": 1})
    # ⚠️ The CLI must pass a real ToolContext, not None. Dispatching without one
    # sent `update_todo` to the module-level fallback list, so the CLI's task
    # list was never persisted and never rendered.
    assert seen["ctx"] is not None
    assert hasattr(seen["ctx"], "todos") and hasattr(seen["ctx"], "task_session")


def test_every_advertised_tool_is_dispatchable(monkeypatch):
    """⚠️ Pinned against `_build_tools()`, NOT against `_SHARED_TOOLS`.

    `_build_tools()` is the list of names the model is TOLD exist. Any name it
    advertises that `dispatch_tool` cannot route falls through to the
    "not registered" branch — the model calls a tool it was promised and is told
    it does not exist, which costs the turn.

    Iterating `_SHARED_TOOLS` here would be tautological: comparing the registry
    against itself passes no matter which names are dropped from it. Sabotage
    proved exactly that — removing "list_dir" from the frozenset left the
    original version of this test green.

    ⚠️ The `_impl_*` stubs patch `agent2.cli.tooling`, NOT `agent2cli`.
    `dispatch_tool` lives in that module and resolves the impls through its own
    globals; `cli._impl_search` is a re-export, i.e. a SEPARATE binding, so
    patching it changes nothing the dispatcher reads. This is the same snapshot
    hazard the palette docstring describes, and it bit for real during the split:
    with the stub landing on the alias, `web_search` fell through to the live
    implementation and the test made an actual DuckDuckGo request.
    """
    declared = {fd.name for fd in cli._build_tools().function_declarations}
    # run_command is streamed by terminal.stream_command, never dispatched here.
    declared.discard("run_command")
    assert declared, "no tools advertised — _build_tools() returned nothing"

    import agent2.tools as shared
    tooling = importlib.import_module("agent2.cli.tooling")
    monkeypatch.setattr(shared, "dispatch_tool", lambda n, a, ctx=None: {"ok": True})
    for local in ("_impl_search", "_impl_save_mem", "_impl_plan"):
        monkeypatch.setattr(tooling, local, lambda a: {"ok": True})

    unroutable = [n for n in sorted(declared)
                  if "is not registered" in str(cli.dispatch_tool(n, {}).get("error", ""))]
    assert not unroutable, f"advertised but not dispatchable: {unroutable}"


def test_cli_local_tools_stay_local(monkeypatch):
    """web_search / save_memory / emit_plan are presentation-layer tools the CLI
    owns. Routing them to the package would change their behaviour.

    Patches `agent2.cli.tooling` for the reason given above — and still calls
    through `cli.dispatch_tool`, which is what proves the entry point re-exports
    the same function object rather than a copy."""
    import agent2.tools as shared
    tooling = importlib.import_module("agent2.cli.tooling")
    monkeypatch.setattr(shared, "dispatch_tool",
                        lambda n, a, ctx=None: pytest.fail(f"{n} should not be delegated"))
    monkeypatch.setattr(tooling, "_impl_plan", lambda a: {"plan": "local"})
    assert cli.dispatch_tool("emit_plan", {"title": "t", "steps": []}) == {"plan": "local"}


# ── History conversion ─────────────────────────────────────────────────────────

def test_msgs_to_history_keeps_only_conversational_roles_in_order():
    """tool_call / tool_result rows are context-assembly concerns; the CLI's
    in-memory history is user/assistant only."""
    rows = [
        {"role": "user", "content": "one", "created_at": "t1"},
        {"role": "tool_call", "content": "ignored", "created_at": "t2"},
        {"role": "assistant", "content": "two", "created_at": "t3"},
        {"role": "tool_result", "content": "ignored", "created_at": "t4"},
        {"role": "system", "content": "ignored", "created_at": "t5"},
    ]
    hist = cli._msgs_to_history(rows)
    assert [h["role"] for h in hist] == ["user", "assistant"]
    assert [h["content"] for h in hist] == ["one", "two"]
    assert [h["ts"] for h in hist] == ["t1", "t3"]


def test_msgs_to_history_tolerates_missing_fields():
    """Rows come from SQLite and older schemas lack columns; a KeyError here
    would take out /load and /resume."""
    assert cli._msgs_to_history([{"role": "user"}]) == [
        {"role": "user", "content": "", "ts": ""}]
    assert cli._msgs_to_history([]) == []
    assert cli._msgs_to_history([{"content": "no role"}]) == []


# ── KeyRotator — now the SHARED one (agent2/llm/keys.py) ──────────────────────
#
# ⚠️ Two behaviours here changed ON PURPOSE when the CLI stopped carrying its own
# rotator, and the tests were updated deliberately rather than silently:
#
#   * get() ROUND-ROBINS. The CLI's copy returned active[0] every call, so key #1
#     burned to quota while the others idled. That was the bug, not the contract.
#   * next_active() is gone. Rotation policy now lives in exactly one place;
#     agent.py's idiom (fail(), then get(), then guard on `k2 != key`) is what
#     the CLI uses too.

def test_cli_uses_the_shared_rotator():
    """⚠️ The whole point of the unification.

    If someone reintroduces a CLI-local rotator, the CLI silently goes back to
    not rotating, not recording usage, and not sharing its pin — none of which
    raises anything. Identity is the only assertion that catches it.
    """
    import agent2.llm.keys as keys_mod
    assert cli.KeyRotator is keys_mod.KeyRotator
    assert cli._rotator is keys_mod.rotator


def test_status_never_emits_a_whole_key(fake_keys):
    """⚠️ SECURITY. This is printed to a terminal by /keys. The redaction is the
    only thing between a shoulder-surfer and a live API key."""
    rot = cli.KeyRotator()
    for entry, shown in zip(fake_keys, rot.status()):
        assert entry["key"] not in shown["preview"]
        assert shown["preview"].endswith("…")
        assert len(shown["preview"]) <= 15
        assert "key" not in shown, "status() leaked the raw key field"


def test_get_returns_none_triple_when_there_are_no_keys(monkeypatch):
    """Failsafe: an unconfigured CLI must degrade to a clean 'no key' path, not
    raise out of the agent loop."""
    import agent2.llm.keys as keys_mod
    monkeypatch.setattr(keys_mod, "list_api_keys", list)
    monkeypatch.setattr(keys_mod, "qall", lambda *a, **k: [])
    monkeypatch.setattr(keys_mod, "genai", object())
    monkeypatch.setattr(keys_mod.KeyRotator, "_load_pin", lambda self: None)
    assert cli.KeyRotator().get() == (None, None, None)


def test_get_returns_none_triple_when_genai_is_missing(fake_keys, monkeypatch):
    import agent2.llm.keys as keys_mod
    monkeypatch.setattr(keys_mod, "genai", None)
    assert cli.KeyRotator().get() == (None, None, None)


def test_get_rotates_across_active_keys(fake_keys):
    """⚠️ This is the divergence that cost real quota.

    The CLI's own rotator handed back the first active key on every call, so a
    user with three keys spent all of key #1's quota before key #2 was ever
    touched. Asserting "not always the same key" would pass on a two-cycle bug;
    asserting the full sweep is what pins round-robin.
    """
    rot = cli.KeyRotator()
    labels = [rot.get()[2] for _ in range(3)]
    assert len(set(labels)) == 3, f"did not sweep every active key: {labels}"
    assert set(labels) == {"one", "two", "tre"}


def test_rotation_skips_a_deactivated_key(fake_keys):
    rot = cli.KeyRotator()
    rot.fail(fake_keys[1]["key"], quota=True)
    labels = {rot.get()[2] for _ in range(6)}
    assert "two" not in labels, "kept handing back an exhausted key"
    assert labels == {"one", "tre"}


def test_quota_failure_deactivates_the_key_immediately(fake_keys):
    rot = cli.KeyRotator()
    rot.fail(fake_keys[0]["key"], quota=True)
    assert [s["active"] for s in rot.status()] == [False, True, True]


def test_non_quota_failures_deactivate_only_on_the_third_strike(fake_keys):
    rot = cli.KeyRotator()
    key = fake_keys[0]["key"]
    rot.fail(key); rot.fail(key)
    assert rot.status()[0]["active"] is True, "deactivated too eagerly"
    rot.fail(key)
    assert rot.status()[0]["active"] is False


def test_the_cli_retry_idiom_finds_a_different_key(fake_keys):
    """Replaces the old test_next_active_* pair.

    The CLI used to call a local `next_active(key)`. It now does what agent.py
    does — fail() the key, ask get() for another, and guard on `k2 != key` — so
    there is ONE rotation policy. This pins that the idiom actually yields a
    different key rather than the dead one.
    """
    rot = cli.KeyRotator()
    _, key, _ = rot.get()
    rot.fail(key, quota=True)
    _, k2, _ = rot.get()
    assert k2 is not None and k2 != key


def test_get_revives_every_key_once_all_are_exhausted(fake_keys):
    """Failsafe: total exhaustion resets rather than hard-failing, so a quota
    window that has since rolled over is retried instead of bricking the CLI."""
    rot = cli.KeyRotator()
    for e in fake_keys:
        rot.fail(e["key"], quota=True)
    client, key, label = rot.get()
    assert key is not None and client is not None


def test_pin_selects_that_key_and_reactivates_it(fake_keys):
    rot = cli.KeyRotator()
    rot.fail(fake_keys[2]["key"], quota=True)
    rot.pin("tre")
    _, key, label = rot.get()
    assert label == "tre", "pin was not honoured"
    assert key == fake_keys[2]["key"]


def test_a_pinned_key_is_returned_every_time(fake_keys):
    """Pinning must DEFEAT round-robin — that is the point of pinning."""
    rot = cli.KeyRotator()
    rot.pin("two")
    assert {rot.get()[2] for _ in range(5)} == {"two"}


def test_unpinning_restores_automatic_selection(fake_keys):
    rot = cli.KeyRotator()
    rot.pin("tre")
    rot.pin(None)
    assert len({rot.get()[2] for _ in range(3)}) == 3


def test_status_reports_which_key_is_pinned(fake_keys):
    rot = cli.KeyRotator()
    rot.pin("two")
    assert [s["pinned"] for s in rot.status()] == [False, True, False]


def test_a_pin_survives_a_restart(fake_keys, monkeypatch):
    """⚠️ The capability the CLI had and the shared rotator did not.

    Opts back into the real _save_pin/_load_pin the fixture stubs out (against
    the temp DB conftest provides), because "the pin is remembered" is exactly
    what an in-memory-only pin fails at. A second rotator stands in for a
    restart, and — since this is now ONE setting rather than the CLI-only
    `cli_pinned_key` — for the other surface in dual mode.
    """
    import agent2.llm.keys as keys_mod
    from agent2.database import set_setting
    monkeypatch.undo()   # drop the fixture's _save_pin/_load_pin stubs
    monkeypatch.setattr(keys_mod, "list_api_keys", lambda: [
        {"api_key": "AAAAAAAAAAAAAAAAAAAAkey-one", "label": "one", "active": 1, "name": "K1"},
        {"api_key": "BBBBBBBBBBBBBBBBBBBBkey-two", "label": "two", "active": 1, "name": "K2"},
    ])
    monkeypatch.setattr(keys_mod, "qall", lambda *a, **k: [])
    monkeypatch.setattr(keys_mod, "genai", object())
    monkeypatch.setattr(keys_mod.KeyRotator, "_client", lambda self, k: f"client:{k}")
    try:
        cli.KeyRotator().pin("two")
        assert cli.KeyRotator().get()[2] == "two", "pin did not survive"
    finally:
        set_setting(keys_mod.KeyRotator._PIN_SETTING, "")
        set_setting(keys_mod.KeyRotator._PIN_SETTING_LEGACY, "")


def test_a_legacy_cli_pin_is_adopted_not_dropped(fake_keys, monkeypatch):
    """The old CLI wrote `cli_pinned_key`. Ignoring it would silently unpin
    every existing CLI user on upgrade."""
    import agent2.llm.keys as keys_mod
    from agent2.database import set_setting
    monkeypatch.undo()
    monkeypatch.setattr(keys_mod, "list_api_keys", lambda: [
        {"api_key": "AAAAAAAAAAAAAAAAAAAAkey-one", "label": "one", "active": 1, "name": "K1"},
        {"api_key": "BBBBBBBBBBBBBBBBBBBBkey-two", "label": "two", "active": 1, "name": "K2"},
    ])
    monkeypatch.setattr(keys_mod, "qall", lambda *a, **k: [])
    monkeypatch.setattr(keys_mod, "genai", object())
    monkeypatch.setattr(keys_mod.KeyRotator, "_client", lambda self, k: f"client:{k}")
    try:
        set_setting(keys_mod.KeyRotator._PIN_SETTING, "")
        set_setting(keys_mod.KeyRotator._PIN_SETTING_LEGACY, "two")
        assert cli.KeyRotator().get()[2] == "two"
    finally:
        set_setting(keys_mod.KeyRotator._PIN_SETTING, "")
        set_setting(keys_mod.KeyRotator._PIN_SETTING_LEGACY, "")


def test_token_usage_is_attributed_to_the_key_that_paid(monkeypatch):
    """⚠️ The CLI used to PRINT the token count and throw it away.

    So `/keys` and the Web key panel reported different usage for the same keys,
    and a CLI-only user saw zeros forever. This asserts on the rotator's own
    counters rather than on stdout — the number has to reach the shared store,
    not just the screen.
    """
    import agent2.llm.keys as keys_mod
    monkeypatch.setattr(keys_mod, "list_api_keys", lambda: [
        {"api_key": "AAAAAAAAAAAAAAAAAAAAkey-one", "label": "one", "active": 1, "name": "K1"},
    ])
    monkeypatch.setattr(keys_mod, "qall", lambda *a, **k: [])
    monkeypatch.setattr(keys_mod, "genai", object())
    monkeypatch.setattr(keys_mod.KeyRotator, "_client", lambda self, k: f"client:{k}")
    monkeypatch.setattr(keys_mod.KeyRotator, "_load_pin", lambda self: None)
    rot = cli.KeyRotator()

    written = []
    monkeypatch.setattr(keys_mod, "exemany", lambda sql, rows: written.extend(rows))
    rot.record_usage("one", 1234)

    snap = rot.status()[0]
    assert snap["tokens"] == 1234, "tokens were not attributed in memory"
    assert snap["requests"] == 1
    assert written, "usage was never persisted — /keys and the web panel diverge"


def test_the_cli_calls_record_usage_on_the_token_path(monkeypatch):
    """Pins the WIRING, which the counter test above cannot see.

    `record_usage` working is useless if the CLI never calls it, and that is
    exactly the state the CLI shipped in.

    ⚠️ Parses the AST rather than grepping the source. A substring check passed
    when sabotage replaced the CALL with `pass`, because the explanatory comment
    above it still contained the word "record_usage" — the test was asserting on
    prose. An AST walk only sees real calls.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cli.run_agent)))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "record_usage" in called, (
        "the CLI agent loop no longer records token usage — /keys will report "
        "zeros forever for a CLI-only user")


def test_a_broken_rotator_import_still_leaves_it_callable():
    """⚠️ FAILSAFE. There are ten `_rotator.<method>()` call sites.

    If the import fails and `_rotator` is None, a broken install turns from "the
    CLI starts and says it has no keys" into an AttributeError out of the agent
    loop. The null object answers every method with the same no-key shape the
    CLI already handles.

    ⚠️ The `except` branch does NOT run on a healthy install, so asserting on
    `cli._rotator` alone cannot see a regression there — sabotage proved it:
    reverting the branch to `_rotator = None` left the object assertions green,
    because they were inspecting the successfully-imported singleton. The branch
    itself is therefore checked in the source, by AST.

    The guard moved to `agent2/cli/keyring.py` in the package split, so that is
    the module parsed — `agent2cli.py` only re-exports what it decided. The
    object-level half above still runs against `cli`, which is what proves the
    re-export survived.
    """
    import ast
    import inspect

    # 1. The null object honours the whole surface the CLI calls.
    null = cli._NullRotator()
    for name in ("get", "status", "reload", "fail", "pin", "record_usage",
                 "add", "remove", "reset_key", "flush_usage"):
        assert callable(getattr(null, name, None)), f"_NullRotator.{name} missing"
    assert null.get() == (None, None, None)
    assert null.status() == []
    assert null.add("k") == (False, "database unavailable")
    null.fail("k", quota=True); null.pin("x"); null.record_usage("x", 1); null.reload()

    # 2. The live object satisfies it too (healthy install).
    assert cli._rotator is not None
    for name in ("get", "status", "reload", "fail", "pin", "record_usage"):
        assert callable(getattr(cli._rotator, name, None)), f"_rotator.{name} missing"

    # 3. The unreachable-in-test fallback branch really installs it.
    keyring = importlib.import_module("agent2.cli.keyring")
    tree = ast.parse(inspect.getsource(keyring))
    handlers = [
        h
        for node in ast.walk(tree) if isinstance(node, ast.Try)
        for h in node.handlers
        if any(isinstance(s, ast.ImportFrom) and s.module == "agent2.llm.keys"
               for s in ast.walk(node))
    ]
    assert handlers, "the rotator import is no longer guarded at all"
    assigned = {
        t.id: n.value
        for h in handlers for n in ast.walk(h)
        if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
    fallback = assigned.get("_rotator")
    assert fallback is not None, "the fallback branch does not assign _rotator"
    assert not (isinstance(fallback, ast.Constant) and fallback.value is None), (
        "_rotator falls back to None — ten call sites would raise AttributeError "
        "on a broken install instead of reporting 'no keys'")


def test_a_placeholder_key_is_never_handed_to_the_model(monkeypatch):
    """The CLI's own loader filtered this and the shared one did not, so the CLI
    was stricter than the Web UI over one table. The filter moved to the shared
    loader rather than being dropped."""
    import agent2.llm.keys as keys_mod
    monkeypatch.setattr(keys_mod, "list_api_keys", lambda: [
        {"api_key": keys_mod._PLACEHOLDER_KEY, "label": "ph", "active": 1, "name": "P"},
    ])
    monkeypatch.setattr(keys_mod, "qall", lambda *a, **k: [])
    monkeypatch.setattr(keys_mod, "genai", object())
    monkeypatch.setattr(keys_mod.KeyRotator, "_load_pin", lambda self: None)
    assert cli.KeyRotator().get() == (None, None, None)
