# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the Skills subsystem (``agent2/core/skills/``) — Phase 11, Tasks 32–36.

Run from the repo root:  python -m pytest .github/tests/test_skills.py -v

Coverage (Task 32 — recursive discovery in ``.agent2/skills/``)
  - the walk is recursive and ids are PATH-derived and lower-cased, so a user
    editing their own ``name:`` line does not silently forget which skills the
    project had enabled
  - the root is ``workspace.root()`` read LIVE, never latched at import
  - **no cross-project leakage**, asserted four separate ways because the leak has
    four shapes: two projects never see each other's catalog, the TTL cache is
    keyed by project *and* dropped on a switch, a symlink out of the tree is
    refused **and recorded**, and enablement is a separate per-project store
  - a partial walk is reported (``truncated`` + ``truncated_by``) for every ceiling
  - there is no home-directory / global skills folder to leak *from*

Coverage (Task 33 — Claude / Codex / Antigravity via a normalization layer)
  - every documented manifest is recognised, and its ``origin`` is REPORTING ONLY:
    no module in the package branches on it, which is the per-vendor plugin the
    task forbids
  - manifest precedence yields ONE skill, never two and never a merge
  - alias folding is a single squashed table — ``whenToUse``, ``when_to_use``,
    ``WHEN-TO-USE``, ``tags`` and ``triggers`` are one field
  - a header field this build does not know is COUNTED (``unparsed``), not dropped
  - **external skill files are never modified**: asserted structurally (no write
    API reaches the four modules) and empirically (a full
    discover → select → render → toggle → turn cycle leaves every byte and every
    mtime in the tree untouched)

Coverage (Task 34 — skills into the Context Broker)
  - the declared ``skills`` slot is filled by registration, and the block reaches
    the prompt tail through ``broker.assemble()``
  - **not every skill in every prompt**: an unrelated message selects nothing at
    all, and each of the five signals (request · project · enabled · relevant ·
    general) selects on its own
  - both ceilings (``SKILLS_IN_PROMPT``, ``SKILLS_MAX_CHARS``) skip rather than
    stop, and what they skipped is reported
  - totality: a discovery that raises costs the skills block and nothing else

Coverage (Task 35 — ``/skills`` on the Task 13 ephemeral menu)
  - registered in the ONE command table, and built on the ONE menu system
  - the menu's no-op is not destructive: turning a skill back ON restores
    *automatic*, it does not pin it into every prompt
  - enable/disable never touches a skill file, and state is workspace-aware
  - the browser gets the same three reads and the same one write

Coverage (Task 36 — priority + conflict resolution + reporting)
  - the order is exactly request → ``agent2.md`` → enabled → auto-relevant →
    general, asserted with one skill per tier in one selection
  - a skill file's own ``priority:`` ranks it WITHIN its tier and never across one
  - two skills claiming one name conflict, and the survivor is decided by data
    (depth → id length → lexicographic), never by filesystem order
  - the report has both halves: what applied and *why each omission was omitted*

⚠️ THE LOAD-BEARING TESTS IN THIS FILE
  * ``test_one_projects_skills_never_appear_in_another`` — Task 32's whole
    acceptance bar, and the one failure mode that leaves no trace on screen.
  * ``test_no_skill_file_is_ever_modified_by_a_full_cycle`` — the user's
    constraint, checked against real bytes rather than against intent.
  * ``test_not_every_skill_reaches_every_prompt`` — Task 34's bar. A subsystem that
    injected all of them would pass every other test in this file.
  * ``test_the_selection_order_is_request_project_enabled_relevant_general`` —
    Task 36's bar, and the only test that would notice the tiers being reordered.
"""

import ast
import hashlib
import os
import random
import re
from importlib import import_module
from pathlib import Path

import pytest

from agent2 import config
from agent2 import database as db
from agent2.core import broker as B
from agent2.core import skills as SK
from agent2.core import workspace as W
from agent2.core.skills import discovery as D
from agent2.core.skills import select as S
from agent2.core.skills import state as ST

# ⚠️ `import_module`, not `from agent2.core.skills import normalize`. The package
# re-exports the *function* `normalize`, which shadows the submodule of the same
# name as a package attribute — so the `from … import` form binds the function and
# every `N.MANIFESTS` is an AttributeError. `discovery.py` gets the module because
# it imports it before `__init__` rebinds the name; a test has no such ordering.
N = import_module("agent2.core.skills.normalize")

_PKG = Path(__file__).resolve().parent.parent.parent / "agent2" / "core" / "skills"
_MODULES = ("discovery.py", "normalize.py", "select.py", "state.py", "__init__.py")


@pytest.fixture(autouse=True)
def _schema(monkeypatch):
    """`skill_state` is migration 29 — `state.py` degrades to "nothing chosen"
    without it, which would make every toggle test pass for the wrong reason.

    ⚠️ **AND THE DISCOVERY BUDGET IS RAISED OUT OF THE WAY FOR EVERY TEST HERE,
    BECAUSE THIS FILE'S FIXTURES ARE NOT WHAT `SKILLS_SCAN_BUDGET_SEC` BOUNDS.**
    That ceiling exists to keep a *user's* `.agent2/skills/` walk off the turn
    path's critical section; a `tmp_path` holding five markdown files is never the
    thing it protects against. Left at its 2.0 s default it is a **timing
    dependency inside an assertion about selection**: `_walk()` tests the deadline
    once per directory and stops, so under a loaded full-suite run the catalog
    silently loses a suffix of a sorted listing and whichever test asserted on the
    alphabetically-last skill fails somewhere unrelated to what it was testing.
    That is a measured flake, not a hypothetical — `test_each_of_the_five_signals…`
    failed as `assert '' == 'relevant'` in a full run and passed alone. Nothing in
    this module asserts `BY_BUDGET` (the ceiling that *is* tested lives in
    `test_workflowfile.py`, and the count/depth/byte ceilings here each patch their
    own knob), so raising it removes a whole class of flake and hides no behaviour.
    """
    monkeypatch.setattr(config, "SKILLS_SCAN_BUDGET_SEC", 120.0)
    db.init_db()
    yield


# ── Helpers ────────────────────────────────────────────────────────────────────

def _skill(root: Path, rel: str, *, manifest: str = "SKILL.md", body: str = "Do the thing.",
           **header) -> Path:
    """Write one skill under ``<root>/.agent2/skills/<rel>/`` and return its file."""
    d = root / ".agent2" / "skills" / rel
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for key, val in header.items():
        if isinstance(val, bool):
            val = "true" if val else "false"
        lines.append(f"{key}: {val}")
    front = "---\n" + "\n".join(lines) + "\n---\n\n" if lines else ""
    path = d / manifest
    path.write_text(f"{front}{body}\n", encoding="utf-8")
    return path


class _Project:
    """`with _Project(tmp):` — *tmp* is the workspace, both caches cold either side.

    Restoring the previous workspace in `finally` is not optional: `workspace.root()`
    is process-wide, and a test that left it pointing at a temp directory would make
    every later test in the session read a project that no longer exists.
    """

    def __init__(self, root: Path):
        self.root = root

    def __enter__(self):
        self._prev = W.root()
        W.set_workspace(str(self.root))
        D.invalidate()
        ST.invalidate()
        return self

    def __exit__(self, *_exc):
        W.set_workspace(self._prev or os.getcwd())
        D.invalidate()
        ST.invalidate()
        return False


def _fingerprint(root: Path) -> dict:
    """Every file under *root*: size, mtime_ns and sha256. The evidence for Task 33."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            raw = p.read_bytes()
            st = p.stat()
            out[str(p.relative_to(root)).replace("\\", "/")] = (
                st.st_size, st.st_mtime_ns, hashlib.sha256(raw).hexdigest())
    return out


def _code_only(path: Path) -> str:
    """Source with comments and docstrings removed — the half that actually runs.

    ⚠️ Without this, every source-level assertion in this file is a tautology: these
    modules DOCUMENT at length that they never write to a skill file and never
    branch on a vendor, so a plain substring search finds `mkdir` and
    `ORIGIN_CLAUDE` in the prose and reports a violation that does not exist — or,
    worse, is written to accept the prose and then passes after the guarantee is
    deleted. `test_continuity.py` carries the same helper for the same reason.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            kept = [st for st in body
                    if not (isinstance(st, ast.Expr)
                            and isinstance(st.value, ast.Constant)
                            and isinstance(st.value.value, str))]
            node.body = kept or [ast.Pass()]
    return ast.unparse(tree)


def _ids(sel) -> list[str]:
    return [a.skill.id for a in sel.applied]


def _why(sel, sid: str) -> str:
    for o in sel.omitted:
        if o.get("id") == sid:
            return str(o.get("why") or "")
    return ""


def _reason_of(sel, sid: str) -> str:
    for a in sel.applied:
        if a.skill.id == sid:
            return a.reason
    return ""


# ── Task 32 · recursive discovery, and no cross-project leakage ────────────────

def test_discovery_is_recursive_and_ids_are_path_derived(tmp_path):
    _skill(tmp_path, "alpha", description="top level")
    _skill(tmp_path, "web/xss", description="nested one deep")
    _skill(tmp_path, "web/deep/csrf", description="nested two deep")
    with _Project(tmp_path):
        cat = D.discover(force=True)
    assert sorted(s.id for s in cat.skills) == ["alpha", "web/deep/csrf", "web/xss"]
    assert cat.exists is True
    assert {s.depth for s in cat.skills} == {1, 2, 3}


def test_a_skill_id_is_lower_cased_so_it_survives_a_case_sensitive_filesystem(tmp_path):
    """The state key must not depend on which OS the user toggled it from."""
    _skill(tmp_path, "Web/XSS", description="mixed case on disk")
    with _Project(tmp_path):
        cat = D.discover(force=True)
    assert [s.id for s in cat.skills] == ["web/xss"]


def test_the_id_does_not_move_when_the_display_name_changes(tmp_path):
    """⚠️ `id` is path-derived, never the header's `name` — see `Skill`'s docstring."""
    p = _skill(tmp_path, "audit", name="First Name")
    with _Project(tmp_path):
        first = D.discover(force=True).skills[0]
        p.write_text("---\nname: Completely Different\n---\n\nbody\n", encoding="utf-8")
        second = D.discover(force=True).skills[0]
    assert first.id == second.id == "audit"
    assert first.name != second.name, "the display name is expected to have moved"


def test_the_skills_root_is_the_live_workspace_not_a_latched_one(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    with _Project(a):
        first = D.skills_root()
    with _Project(b):
        second = D.skills_root()
    assert first != second
    assert first == a / ".agent2" / "skills"
    assert second == b / ".agent2" / "skills"


def test_one_projects_skills_never_appear_in_another(tmp_path):
    """⚠️ TASK 32'S WHOLE ACCEPTANCE BAR, end to end, and the failure with nothing
    on screen.

    Deliberately WITHOUT `force=True` and without a manual `invalidate()` between the
    two reads: the cache is *supposed* to be warm, because a warm cache is where
    leakage lives — both surfaces would render a perfectly plausible catalog for
    `SKILLS_TTL` seconds and nothing would look wrong.

    ⚠️ WHAT IT PINS IS THE COMPOSITION, not one mechanism, and sabotage says which:
    latching `skills_root()` at first call reddens it (project B reads A's folder),
    and so does the cache key ceasing to name the root. Unwiring
    `isolation.on_invalidate` does **not**, and that is worth writing down rather than
    dressing up — the key already separates two projects, because both the key and the
    root derive from `workspace.root()`. The hook bounds *staleness within* one
    project, which is a different fact and not this test's.
    """
    a, b = tmp_path / "alpha-proj", tmp_path / "beta-proj"
    a.mkdir(), b.mkdir()
    _skill(a, "alpha-only", description="belongs to A")
    _skill(b, "beta-only", description="belongs to B")

    with _Project(a):
        first = D.discover()                       # warms the cache and installs hooks
    assert [s.id for s in first.skills] == ["alpha-only"]

    W.set_workspace(str(b))                        # a switch, exactly as `/workspace` does
    try:
        second = D.discover()                      # NO force, NO invalidate
        assert [s.id for s in second.skills] == ["beta-only"], (
            "project A's catalog leaked into project B")
        assert second.project != first.project, "a catalog must say which project it is"
    finally:
        W.set_workspace(os.getcwd())
        D.invalidate()


def test_two_skill_roots_read_back_to_back_never_share_one_cache_entry(tmp_path):
    """The second half of Task 32's bar: the cache KEY, with no switch to rescue it.

    The sibling test above cannot see this, and that is the point. It switches
    workspace, which fires `isolation.on_invalidate` and empties `_CACHE` wholesale —
    so a cache keyed on one constant string still answers correctly there. Here both
    reads happen in one project with the cache deliberately warm and only the *root*
    differing, which is the one arrangement where the key is the only thing standing
    between two projects. `discover(root=…)` is a real caller shape, not a test hook:
    `/api/skills` and `/init` both pass a root.

    Keyed on `normcase(base)` this returns two catalogs. Keyed on anything constant it
    returns the first one twice — recursive discovery reading the wrong folder, inside
    the TTL, with a plausible skill list on both surfaces.
    """
    a, b = tmp_path / "root-a", tmp_path / "root-b"
    a.mkdir(), b.mkdir()
    _skill(a, "from-a", description="belongs to A")
    _skill(b, "from-b", description="belongs to B")

    with _Project(tmp_path):                       # one project, two roots
        first = D.discover(root=a)                 # warms the cache
        second = D.discover(root=b)                # NO force, NO invalidate
        again = D.discover(root=a)                 # and the warm entry still holds
    assert [s.id for s in first.skills] == ["from-a"]
    assert [s.id for s in second.skills] == ["from-b"], (
        "one cache entry served two skill roots — the key does not name the root")
    assert [s.id for s in again.skills] == ["from-a"], "the first root's entry was evicted"
    assert Path(first.root) != Path(second.root), "a catalog must say which root it read"



    _skill(tmp_path, "one")
    with _Project(tmp_path):
        cat = D.discover(force=True)
    assert cat.project, "a catalog with no project cannot be checked for leakage"
    assert Path(cat.root) == tmp_path / ".agent2" / "skills"


def test_a_manifest_symlinked_outside_the_tree_is_refused_and_recorded(tmp_path):
    """The filesystem's form of cross-project leakage, and it looks ordinary."""
    outside = tmp_path / "other-project" / "secret"
    outside.mkdir(parents=True)
    real = outside / "SKILL.md"
    real.write_text("---\nname: Exfiltrated\n---\n\nsecrets\n", encoding="utf-8")
    here = tmp_path / "here"
    d = here / ".agent2" / "skills" / "borrowed"
    d.mkdir(parents=True)
    try:
        (d / "SKILL.md").symlink_to(real)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this platform/account cannot create symlinks")
    with _Project(here):
        cat = D.discover(force=True)
    assert [s.name for s in cat.skills] == [], "a symlink out of the tree was admitted"
    assert cat.errors, "a refused skill must be recorded, not silently absent"


def test_enablement_is_scoped_per_project_not_globally(tmp_path):
    """"On here" must never mean "on everywhere" — the fourth leakage mechanism.

    ⚠️ The RESET half is not decoration. `set_enabled(id, None)` is the only DELETE on
    this path, and sabotage found it blind: dropping `project=?` from that statement
    left this test green while a `/skills` reset in one checkout silently cleared the
    same skill's row in every other one. A read predicate and a write predicate are
    two statements, so both directions are exercised here.
    """
    a, b = tmp_path / "pa", tmp_path / "pb"
    a.mkdir(), b.mkdir()
    _skill(a, "shared-name", description="in A")
    _skill(b, "shared-name", description="in B")
    with _Project(a):
        ok, _msg = SK.toggle("shared-name", True)
        assert ok
        assert ST.get("shared-name") is True
    with _Project(b):
        assert ST.get("shared-name") is None, (
            "an enablement row crossed into another project")
        assert ST.set_enabled("shared-name", False)
        assert ST.get("shared-name") is False
    with _Project(a):
        assert ST.get("shared-name") is True, "B's choice overwrote A's"
        assert ST.set_enabled("shared-name", None)          # reset, here only
        assert ST.get("shared-name") is None
    with _Project(b):
        assert ST.get("shared-name") is False, (
            "a reset in one project deleted another project's row")



def test_a_partial_walk_is_reported_for_the_count_ceiling(tmp_path, monkeypatch):
    for i in range(6):
        _skill(tmp_path, f"s{i}")
    monkeypatch.setattr(config, "SKILLS_MAX", 2)
    with _Project(tmp_path):
        cat = D.discover(force=True)
    assert len(cat.skills) <= 2
    assert cat.truncated is True
    assert cat.truncated_by == D.BY_COUNT


def test_a_partial_walk_is_reported_for_the_depth_ceiling(tmp_path, monkeypatch):
    _skill(tmp_path, "a/b/c/d/e/f/g/deep")
    monkeypatch.setattr(config, "SKILLS_MAX_DEPTH", 2)
    with _Project(tmp_path):
        cat = D.discover(force=True)
    assert cat.truncated is True
    assert cat.truncated_by == D.BY_DEPTH


def test_an_over_long_skill_is_clipped_and_says_so(tmp_path, monkeypatch):
    _skill(tmp_path, "long", body="x" * 8000)
    monkeypatch.setattr(config, "SKILLS_MAX_BYTES", 2048)
    with _Project(tmp_path):
        sk = D.discover(force=True).skills[0]
    assert sk.body_truncated is True
    assert sk.size <= 2048


def test_there_is_no_home_or_global_skills_folder_to_leak_from():
    """A machine-wide skill would be the global project config we may not create."""
    code = _code_only(_PKG / "discovery.py")
    for forbidden in ("expanduser", "Path.home", "environ.get('AGENT2_SKILLS_DIR'",
                      "'~'", '"~"'):
        assert forbidden not in code, f"discovery reaches outside the workspace: {forbidden}"


def test_discovery_is_total_when_the_folder_does_not_exist(tmp_path):
    with _Project(tmp_path):
        cat = D.discover(force=True)
    assert cat.skills == []
    assert cat.exists is False
    assert cat.errors == [], '"no skills folder" is not an error'


def test_the_folders_own_readme_is_never_a_skill(tmp_path):
    """`projectdoc.apply()` seeds this file; admitting it gives every project a
    phantom skill that tells the model to "drop one directory per skill here"."""
    root = tmp_path / ".agent2" / "skills"
    root.mkdir(parents=True)
    (root / "README.md").write_text("# Skills\n\nDrop one directory per skill here.\n",
                                    encoding="utf-8")
    _skill(tmp_path, "real-one")
    with _Project(tmp_path):
        cat = D.discover(force=True)
    assert [s.id for s in cat.skills] == ["real-one"]


def test_a_loose_markdown_file_at_the_root_is_a_skill_named_by_its_stem(tmp_path):
    root = tmp_path / ".agent2" / "skills"
    root.mkdir(parents=True)
    (root / "quick-note.md").write_text("---\nname: Quick Note\n---\n\nbe brief\n",
                                        encoding="utf-8")
    with _Project(tmp_path):
        cat = D.discover(force=True)
    assert [s.id for s in cat.skills] == ["quick-note"]
    assert all(s.id for s in cat.skills), "the skills folder itself is not a skill"


def test_a_directory_with_several_loose_docs_is_skipped_not_guessed(tmp_path):
    """`references/` and `examples/` are support folders far more often than mistakes."""
    d = tmp_path / ".agent2" / "skills" / "helper" / "references"
    d.mkdir(parents=True)
    (d / "one.md").write_text("a", encoding="utf-8")
    (d / "two.md").write_text("b", encoding="utf-8")
    _skill(tmp_path, "helper")
    with _Project(tmp_path):
        cat = D.discover(force=True)
    assert [s.id for s in cat.skills] == ["helper"]
    assert cat.skipped >= 1, "a skipped directory must be counted"


# ── Task 33 · the normalization layer ─────────────────────────────────────────

def test_every_documented_manifest_is_recognised_with_its_origin(tmp_path):
    for i, manifest in enumerate(N.MANIFESTS):
        _skill(tmp_path, f"m{i}", manifest=manifest, name=f"M{i}")
    with _Project(tmp_path):
        cat = D.discover(force=True)
    assert len(cat.skills) == len(N.MANIFESTS), (
        f"a documented manifest was not read: {sorted(s.manifest for s in cat.skills)}")
    for sk in cat.skills:
        assert sk.origin == N.origin_for(sk.manifest)


def test_manifest_precedence_yields_one_skill_never_two_and_never_a_merge(tmp_path):
    d = tmp_path / ".agent2" / "skills" / "both"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: The Claude One\n---\n\nclaude body\n",
                                encoding="utf-8")
    (d / "AGENTS.md").write_text("---\nname: The Codex One\n---\n\ncodex body\n",
                                 encoding="utf-8")
    with _Project(tmp_path):
        cat = D.discover(force=True)
    assert len(cat.skills) == 1
    sk = cat.skills[0]
    assert sk.name == "The Claude One", "precedence is the ORIGINS order"
    assert sk.origin == N.ORIGIN_CLAUDE
    assert "codex body" not in sk.body, "two manifests were merged into invented prose"


def test_alias_spellings_fold_to_one_canonical_field():
    for spelling in ("keywords", "tags", "triggers", "whenToUse", "when_to_use",
                     "WHEN-TO-USE", "use_when", "appliesTo", "topics"):
        got = N.normalize(f"---\n{spelling}: alpha, beta\n---\n\nbody\n",
                          manifest="SKILL.md")
        assert tuple(got["keywords"]) == ("alpha", "beta"), f"{spelling} did not fold"
    for spelling in ("always", "always_apply", "alwaysApply", "ALWAYS-APPLY",
                     "global", "pinned", "sticky"):
        got = N.normalize(f"---\n{spelling}: true\n---\n\nbody\n", manifest="SKILL.md")
        assert got["always"] is True, f"{spelling} did not fold"


def test_the_squash_is_case_and_separator_together():
    """A fold that only lowercased would miss `alwaysApply` — the Antigravity spelling."""
    assert N.squash("whenToUse") == N.squash("when_to_use") == N.squash("WHEN-TO-USE")
    assert N.squash("alwaysApply") == N.squash("always_apply")


def test_a_header_field_this_build_does_not_know_is_kept_not_dropped():
    """An unrecognised key lands in `extra`, and it costs the known keys nothing.

    Vendors add header fields on their own schedule, so "a field written for an
    agent that is not us" is the normal case rather than an error. Dropping it
    silently is what makes a normalization layer look like it mangled the file.
    """
    got = N.normalize(
        "---\nname: Future\nsome_field_from_2027: yes\n---\n\nbody text\n",
        manifest="SKILL.md")
    assert got["name"] == "Future", "an unknown field cost a known one"
    assert "some_field_from_2027" in got["extra"]
    assert got["unparsed"] == 0, "a key this parser READ is not a line it failed on"


def test_a_header_shape_this_parser_cannot_read_is_counted_never_guessed(tmp_path):
    """⚠️ `unparsed` is the honesty counter: a half-read header must not read as
    a fully-read one. Nested maps and block scalars are the two shapes stdlib
    frontmatter parsing cannot do, and PyYAML is deliberately not a dependency."""
    d = tmp_path / ".agent2" / "skills" / "blocky"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: Blocky\ndescription: |\n  a block scalar\nmatrix: {\n---\n\nbody\n",
        encoding="utf-8")
    with _Project(tmp_path):
        sk = D.discover(force=True).skills[0]
    assert sk.name == "Blocky"
    assert sk.unparsed >= 2, "an unreadable header shape vanished without a trace"


def test_a_file_with_no_frontmatter_is_still_a_skill(tmp_path):
    d = tmp_path / ".agent2" / "skills" / "bare"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# Bare Skill\n\nAlways prefer explicit imports.\n",
                                encoding="utf-8")
    with _Project(tmp_path):
        sk = D.discover(force=True).skills[0]
    assert sk.id == "bare"
    assert sk.name, "a headerless skill still needs a name"
    assert "explicit imports" in sk.body


def test_the_body_is_read_verbatim(tmp_path):
    body = "Steps:\n\n```python\nx = {'a': 1}   # keep  spacing\n```\n\n- one\n- two"
    _skill(tmp_path, "verbatim", body=body, name="Verbatim")
    with _Project(tmp_path):
        sk = D.discover(force=True).skills[0]
    assert body in sk.body, "the normalization layer rewrote a skill's prose"


def test_no_module_outside_the_table_names_a_vendor():
    """⚠️ A BRANCH ON A VENDOR IS THE PER-VENDOR PLUGIN TASK 33 FORBIDS.

    `normalize.py` owns the whole vendor vocabulary: `ORIGINS` maps a filename to a
    label and `origin_for()` is that table's only reader. The word must not appear
    anywhere else in the package, because the first thing a per-vendor plugin looks
    like is one `if skill.origin == ORIGIN_CLAUDE:` that "just" adjusts a heading —
    and the second is a second table of them.

    `select.render_one` does test `origin != "plain"`, and that is deliberately
    allowed: `plain` is the *absence* of a convention, so suppressing the
    `format:` label for it treats every real vendor identically. The property
    asserted here is the one that matters — no vendor is singled out.
    """
    vendors = ("ORIGIN_CLAUDE", "ORIGIN_CODEX", "ORIGIN_ANTIGRAVITY",
               '"claude"', "'claude'", '"codex"', "'codex'",
               '"antigravity"', "'antigravity'", '"gemini"', "'gemini'")
    for name in _MODULES:
        if name == "normalize.py":
            continue
        code = _code_only(_PKG / name)
        for word in vendors:
            assert word not in code, f"{name} singles out a vendor: {word}"


def test_the_same_skill_under_every_vendor_manifest_behaves_identically(tmp_path):
    """The behavioural half: the layer FLATTENS the vendors, it does not adapt to them.

    One body, one header, three filenames — Claude's, Codex's and Antigravity's.
    Every field a later step reads must agree; only the reported provenance differs.
    """
    header = ("---\nname: Same Skill\ndescription: One description\n"
              "keywords: alpha, beta\npriority: 7\nalways: true\n---\n\nOne body.\n")
    for i, manifest in enumerate(("SKILL.md", "AGENTS.md", "GEMINI.md")):
        d = tmp_path / ".agent2" / "skills" / f"v{i}"
        d.mkdir(parents=True)
        (d / manifest).write_text(header, encoding="utf-8")
    with _Project(tmp_path):
        cat = D.discover(force=True)
        sel = S.choose(cat, "", {}, limit=3)
    assert len(cat.skills) == 3
    shape = {(s.name, s.description, s.keywords, s.always, s.priority, s.body)
             for s in cat.skills}
    assert len(shape) == 1, f"the vendors were read differently: {shape}"
    assert len({s.origin for s in cat.skills}) == 3, "provenance must still be reported"
    assert len({a.reason for a in sel.applied}) == 1, "one vendor ranked differently"


def test_no_write_api_reaches_the_skills_package():
    """Task 33's "do not modify external skill files" as a structural property.

    Two halves, because a write has two shapes: a named helper (`write_text`,
    `mkdir`, `shutil.copy`) and an `open()` whose *mode* is the whole difference
    between reading a skill and rewriting it. The second half walks the AST rather
    than the text, so `open(path, "rb")` — what discovery actually does — is
    distinguished from `open(path, "w")` instead of both reading as "calls open".
    """
    forbidden = ("write_text", "write_bytes", "mkdir", "makedirs", "shutil",
                 "unlink", "os.remove", "os.rename", "os.replace", "touch(")
    for name in _MODULES:
        code = _code_only(_PKG / name)
        for token in forbidden:
            assert token not in code, f"{name} can modify a file: {token}"
        for node in ast.walk(ast.parse(code)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "open"):
                continue
            modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
            modes += [k.value.value for k in node.keywords
                      if k.arg == "mode" and isinstance(k.value, ast.Constant)]
            assert modes, f"{name} calls open() with no explicit mode"
            for mode in modes:
                assert set(str(mode)) <= {"r", "b", "t"}, (
                    f"{name} opens a file for writing (mode {mode!r})")


def test_no_skill_file_is_ever_modified_by_a_full_cycle(tmp_path):
    """⚠️ THE USER'S CONSTRAINT, checked against real bytes.

    A full round trip — discover, select, render into a prompt, toggle off, toggle
    back on, assemble a turn — and then every file in the tree is compared by size,
    mtime_ns and sha256. A normalization layer that "helpfully" rewrote a header,
    or a cache that wrote a normalized copy next to the original, fails here and
    nowhere else.
    """
    _skill(tmp_path, "audit", name="Audit", keywords="audit, review", body="Check things.")
    _skill(tmp_path, "web/xss", name="XSS", always=True, body="Escape output.")
    tree = tmp_path / ".agent2" / "skills"
    before = _fingerprint(tree)
    assert before, "the fixture wrote nothing"
    with _Project(tmp_path):
        cat = D.discover(force=True)
        sel = S.choose(cat, "please audit this", ST.states())
        S.prompt_block(sel)
        assert SK.toggle("audit", False)[0]
        assert SK.toggle("audit", None)[0]
        SK.for_turn("please audit this")
        SK.block("anything")
        SK.available(force=True)
        SK.stats()
    assert _fingerprint(tree) == before, "a skill file was modified"


# ── Task 34 · into the Context Broker, and NOT every skill every turn ─────────

def test_the_block_reaches_the_prompt_through_the_broker(tmp_path):
    _skill(tmp_path, "threat-model", name="Threat Model", always=True,
           body="Start with trust boundaries.")
    with _Project(tmp_path):
        tail = B.assemble(chat_id="c1", message="anything").prompt_tail()
    assert S.HEADING in tail
    assert "Threat Model" in tail
    assert "trust boundaries" in tail


def test_not_every_skill_reaches_every_prompt(tmp_path):
    """⚠️ TASK 34'S BAR. A subsystem that injected all of them passes everything else.

    Ten skills, none of them `always`, none named by the message and none whose
    declared keywords match it: the correct answer is an empty block. Then one
    message that names one skill selects exactly that one.
    """
    for i in range(10):
        _skill(tmp_path, f"tool{i}", name=f"Tool{i}", keywords=f"topic{i}",
               description=f"handles topic{i}")
    with _Project(tmp_path):
        cat = D.discover(force=True)
        quiet = S.choose(cat, "what time is it", {})
        assert quiet.applied == [], f"unrelated turn pulled in {_ids(quiet)}"
        assert S.prompt_block(quiet) == ""
        aimed = S.choose(cat, "use Tool7 on this", {})
    assert _ids(aimed) == ["tool7"]
    assert aimed.considered == 10, "the report must say how many were considered"


def test_each_of_the_five_signals_selects_on_its_own(tmp_path):
    """Five skills, five different reasons to be here, one catalog.

    ⚠️ Asserted by looking each skill's reason UP, never by indexing `applied[0]`:
    the tiers are ranked, so the top slot always belongs to whichever tier is
    highest — an index would test the ordering (Task 36) and quietly say nothing at
    all about whether the *other four* signals fire.

    ⚠️ **AND THE CATALOG IS ASSERTED COMPLETE BEFORE ANY REASON IS LOOKED UP.**
    Five signals need five skills, so a walk that dropped one would fail on
    whichever reason happened to go missing rather than on the thing that broke —
    measured, that read `assert '' == 'relevant'` and pointed at the relevance tier,
    which was never involved. `_walk()` already reports a partial answer
    (`truncated` + `truncated_by` exist *"precisely so a partial answer cannot read
    as a complete one"*), so reading its own report first is what makes the failure
    legible; the autouse fixture above is what makes it rare.
    """
    _skill(tmp_path, "byname", name="Byname", body="b")
    _skill(tmp_path, "bydoc", name="Bydoc", body="b")
    _skill(tmp_path, "bytoggle", name="Bytoggle", body="b")
    _skill(tmp_path, "bywords", name="Bywords", keywords="gobuster", body="b")
    _skill(tmp_path, "byalways", name="Byalways", always=True, body="b")
    doc = tmp_path / ".agent2" / "agent2.md"
    doc.write_text("# Doc\n\nAlways consult bydoc for this project.\n", encoding="utf-8")
    with _Project(tmp_path):
        cat = D.discover(force=True)
        assert cat.truncated is False, f"the walk stopped early: {cat.truncated_by}"
        assert sorted(s.id for s in cat.skills) == [
            "byalways", "bydoc", "byname", "bytoggle", "bywords"], (
            "five signals need five skills; a partial catalog fails on whichever "
            "reason is missing instead of on what actually broke")
        assert "bydoc" in cat.doc_mentions
        sel = S.choose(cat, "run byname with gobuster", {"bytoggle": True}, limit=5)
        quiet = S.choose(cat, "nothing to do with any of them", {}, limit=5)
    assert _reason_of(sel, "byname") == S.REASON_REQUEST
    assert _reason_of(sel, "bydoc") == S.REASON_PROJECT
    assert _reason_of(sel, "bytoggle") == S.REASON_ENABLED
    assert _reason_of(sel, "bywords") == S.REASON_RELEVANT
    assert _reason_of(sel, "byalways") == S.REASON_GENERAL
    # The same catalog, a message that names nothing: only the two standing signals.
    assert sorted(_ids(quiet)) == ["byalways", "bydoc"]


def test_a_disabled_skill_is_selected_by_no_signal_at_all(tmp_path):
    """⚠️ "Off" outranks every other tier, `request` included."""
    _skill(tmp_path, "blocked", name="Blocked", always=True, keywords="blocked, gobuster")
    with _Project(tmp_path):
        cat = D.discover(force=True)
        sel = S.choose(cat, "use Blocked with gobuster", {"blocked": False})
    assert sel.applied == []
    assert _why(sel, "blocked") == S.OUT_DISABLED


def test_a_skill_never_chosen_is_not_the_same_fact_as_one_switched_off(tmp_path):
    """`None` is a third answer, and the store must be able to say it out loud.

    ⚠️ THE `default=True` PROBE IS THE ONLY THING THAT SEES A COLLAPSE. Every caller
    on the turn path asks with `default=False`, so `bool(None)` and
    `default if val is None else val` return the same answer there — sabotage replacing
    the tri-state body with `bool(val)` left this test green until this line existed.
    A report asking "is this on unless told otherwise" is the caller that would then
    silently read *never chosen* as *switched off*.
    """
    _skill(tmp_path, "auto-one", name="Autoone", always=True)
    with _Project(tmp_path):
        cat = D.discover(force=True)
        assert _ids(S.choose(cat, "x", {})) == ["auto-one"], (
            "a missing state row means automatic, not off"
        )
        assert S.choose(cat, "x", {"auto-one": False}).applied == []

        assert ST.get("auto-one") is None
        assert ST.is_enabled("auto-one", default=True) is True, (
            "never-chosen was collapsed into False"
        )
        assert ST.is_enabled("auto-one", default=False) is False
        assert ST.set_enabled("auto-one", False)
        assert ST.is_enabled("auto-one", default=True) is False, (
            "an explicit False must beat the caller's default"
        )



def test_the_prompt_cap_skips_and_reports_the_rest(tmp_path):
    for i in range(6):
        _skill(tmp_path, f"g{i}", name=f"G{i}", always=True, priority=10 - i)
    with _Project(tmp_path):
        cat = D.discover(force=True)
        sel = S.choose(cat, "", {}, limit=2)
    assert len(sel.applied) == 2
    assert len([o for o in sel.omitted if o.get("why") == S.OUT_CAP]) == 4
    assert sel.limit == 2


def test_the_char_ceiling_skips_rather_than_stops(tmp_path):
    """One oversized skill may not strip every smaller one ranked below it."""
    _skill(tmp_path, "huge", name="Huge", always=True, priority=99, body="z" * 4000)
    _skill(tmp_path, "tiny", name="Tiny", always=True, priority=1, body="t")
    with _Project(tmp_path):
        cat = D.discover(force=True)
        sel = S.choose(cat, "", {}, limit=5, max_chars=1200)
    assert _ids(sel) == ["tiny"], "an oversized skill acted as a stop sign"
    assert _why(sel, "huge") == S.OUT_CHARS


def test_a_selection_that_cannot_be_computed_costs_only_the_skills_block(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("discovery exploded")
    monkeypatch.setattr(SK, "discover", boom)
    sel = SK.for_turn("anything")
    assert sel.applied == []
    assert SK.block("anything") == ""


def test_the_subsystem_can_be_turned_off_entirely(tmp_path, monkeypatch):
    _skill(tmp_path, "on-disk", name="Ondisk", always=True)
    monkeypatch.setattr(config, "SKILLS_ENABLED", False)
    with _Project(tmp_path):
        assert SK.enabled() is False
        assert SK.for_turn("anything").applied == []
        assert SK.block("anything") == ""


def test_the_turn_writes_one_audit_line_and_the_facade_owns_it(tmp_path, monkeypatch):
    """One turn, one record — the same rule `budget.apply()` follows for its trim."""
    from agent2.core import logging as alog
    seen = []
    monkeypatch.setattr(alog, "skills_applied", lambda **kw: seen.append(kw))
    _skill(tmp_path, "logged", name="Logged", always=True)
    with _Project(tmp_path):
        SK.for_turn("anything")
    assert len(seen) == 1
    assert "logged" in seen[0]["applied"]
    assert seen[0]["reasons"] == S.REASON_GENERAL


def test_the_broker_slot_and_the_facade_agree_about_what_applied(tmp_path):
    _skill(tmp_path, "agreed", name="Agreed", always=True, body="one body")
    with _Project(tmp_path):
        B.assemble(chat_id="c1", message="anything")
        last = SK.last_applied()
    assert [a["id"] for a in last["applied"]] == ["agreed"]


# ── Task 35 · `/skills` on the one menu system ────────────────────────────────

def test_slash_skills_is_in_the_one_command_table():
    from agent2.cli.render import SLASH_COMMANDS
    rows = [r for r in SLASH_COMMANDS if str(r[1]).strip() == "/skills"]
    assert len(rows) == 1, "/skills must be declared exactly once — /help and the completer"
    assert "on" in rows[0][2] and "off" in rows[0][2] and "list" in rows[0][2]


def test_cmd_skills_is_built_on_the_one_menu_system():
    """⚠️ A second `prompt_toolkit.Application` is the bug Task 13 was filed for."""
    src = (Path(__file__).resolve().parent.parent.parent / "agent2cli.py").read_text(
        encoding="utf-8")
    start = src.index("def cmd_skills(")
    body = src[start:src.index("\ndef ", start + 10)]
    assert "ephemeral_toggle_menu" in body
    assert "prompt_toolkit" not in body
    assert "Application(" not in body


def test_the_menus_no_op_is_not_destructive():
    """⚠️ ON→`None` (automatic), never ON→`True` (pinned into every prompt).

    Opening the menu and pressing Enter must change nothing. Mapping the ON
    position to `True` would silently pin every discovered skill into every prompt
    — a menu whose no-op is destructive.
    """
    src = (Path(__file__).resolve().parent.parent.parent / "agent2cli.py").read_text(
        encoding="utf-8")
    start = src.index("def cmd_skills(")
    body = src[start:src.index("\ndef ", start + 10)]
    assert "None if now_on else False" in body, "the ON position must restore automatic"
    assert "stored is not False" in body, (
        "`bool(stored)` would render every unchosen skill as OFF")


def test_enable_disable_never_touches_a_skill_file(tmp_path):
    _skill(tmp_path, "toggled", name="Toggled")
    tree = tmp_path / ".agent2" / "skills"
    before = _fingerprint(tree)
    with _Project(tmp_path):
        for value in (True, False, None, False, True):
            ok, msg = SK.toggle("toggled", value)
            assert ok, msg
        ST.set_many({"toggled": None})
        ST.clear()
    assert _fingerprint(tree) == before


def test_a_toggle_resolves_the_same_way_on_both_surfaces(tmp_path):
    """`Catalog.find()` — id, then exact name, then a UNIQUE prefix."""
    _skill(tmp_path, "sql-injection", name="SQL Injection")
    _skill(tmp_path, "sql-mapping", name="SQL Mapping")
    with _Project(tmp_path):
        cat = D.discover(force=True)
        assert cat.find("sql-injection").id == "sql-injection"
        assert cat.find("SQL Mapping").id == "sql-mapping"
        assert cat.find("sql-i").id == "sql-injection"
        assert cat.find("sql") is None, "an ambiguous prefix must be refused"
        ok, msg = SK.toggle("sql", True)
    assert ok is False
    assert "sql" in msg


def test_the_state_store_reports_its_own_scope():
    """A report that cannot say whether it is scoped cannot be checked for leakage."""
    d = ST.describe()
    assert d["topic"] == ST.TOPIC
    assert set(d) >= {"topic", "cached", "chosen", "on", "off", "scoped"}
    assert isinstance(d["scoped"], bool)
    assert ST.project() == ST.project(), "the project key must be stable within a read"
    for key in d:
        assert "path" not in key and "root" not in key, (
            "`describe()` may carry counters, never a project path")


def test_the_browser_gets_the_same_three_reads_and_the_one_write():
    src = (Path(__file__).resolve().parent.parent.parent / "agent2" / "server"
           / "routes.py").read_text(encoding="utf-8")
    assert '"/api/skills", methods=["GET"]' in src
    assert '"/api/skills/<sid>", methods=["PUT"]' in src
    assert '"/api/skills", methods=["PUT"]' in src
    for token in ("write_text", "mkdir", "unlink"):
        block = src[src.index("def api_get_skills"):src.index("def api_set_skills")]
        assert token not in block, f"a skills route can modify a file: {token}"


def test_the_skills_routes_are_capability_gated():
    from agent2.core import permissions as P
    assert P.capability_for("PUT", "/api/skills") == P.CAP_SETTINGS
    assert P.capability_for("PUT", "/api/skills/some-id") == P.CAP_SETTINGS


# ── Task 36 · priority, conflict resolution, and the report ───────────────────

def test_the_selection_order_is_request_project_enabled_relevant_general(tmp_path):
    """⚠️ TASK 36'S BAR — one skill per tier, one selection, one asserted order."""
    _skill(tmp_path, "reqone", name="Reqone", body="r")
    _skill(tmp_path, "projtwo", name="Projtwo", body="p")
    _skill(tmp_path, "enabthree", name="Enabthree", body="e")
    _skill(tmp_path, "relvfour", name="Relvfour", keywords="gobuster", body="v")
    _skill(tmp_path, "genlfive", name="Genlfive", always=True, body="g")
    (tmp_path / ".agent2" / "agent2.md").write_text(
        "# Doc\n\nprojtwo is this project's convention.\n", encoding="utf-8")
    with _Project(tmp_path):
        cat = D.discover(force=True)
        sel = S.choose(cat, "reqone with gobuster", {"enabthree": True}, limit=5)
    assert [a.reason for a in sel.applied] == [
        S.REASON_REQUEST, S.REASON_PROJECT, S.REASON_ENABLED,
        S.REASON_RELEVANT, S.REASON_GENERAL]
    assert _ids(sel) == ["reqone", "projtwo", "enabthree", "relvfour", "genlfive"]


def test_a_skill_file_cannot_promote_itself_past_the_users_ask(tmp_path):
    """`priority:` orders WITHIN a tier and never across one."""
    _skill(tmp_path, "asked", name="Asked", priority=0, body="a")
    _skill(tmp_path, "pushy", name="Pushy", always=True, priority=9999, body="p")
    with _Project(tmp_path):
        cat = D.discover(force=True)
        sel = S.choose(cat, "please use Asked", {}, limit=5)
    assert _ids(sel) == ["asked", "pushy"]


def test_priority_orders_within_one_tier(tmp_path):
    _skill(tmp_path, "low", name="Low", always=True, priority=1, body="l")
    _skill(tmp_path, "high", name="High", always=True, priority=50, body="h")
    with _Project(tmp_path):
        cat = D.discover(force=True)
        sel = S.choose(cat, "", {}, limit=5)
    assert _ids(sel) == ["high", "low"]


def test_two_skills_claiming_one_name_conflict_and_the_shallower_wins(tmp_path):
    _skill(tmp_path, "web/audit", name="Audit", always=True, body="the web one")
    _skill(tmp_path, "api/deep/audit", name="Audit", always=True, body="the api one")
    with _Project(tmp_path):
        cat = D.discover(force=True)
        sel = S.choose(cat, "", {}, limit=5)
    assert _ids(sel) == ["web/audit"], "two skills shared one label in one prompt"
    shadowed = [o for o in sel.omitted if o.get("why") == S.OUT_SHADOWED]
    assert len(shadowed) == 1
    assert shadowed[0]["id"] == "api/deep/audit"
    assert shadowed[0]["by"] == "web/audit", "the report must name the winner"


def test_the_selection_is_deterministic_under_filesystem_order(tmp_path):
    """Every tie is broken by data, so two machines cannot disagree."""
    for i in range(8):
        _skill(tmp_path, f"same{i}", name=f"Same{i}", always=True, priority=5, body="s")
    with _Project(tmp_path):
        cat = D.discover(force=True)
        first = _ids(S.choose(cat, "", {}, limit=8))
        rng = random.Random(1311)
        for _ in range(5):
            rng.shuffle(cat.skills)
            assert _ids(S.choose(cat, "", {}, limit=8)) == first


def test_the_report_says_why_each_omission_was_omitted(tmp_path):
    _skill(tmp_path, "offhere", name="Offhere", always=True)
    _skill(tmp_path, "web/dup", name="Dup", always=True)
    _skill(tmp_path, "api/deep/dup", name="Dup", always=True)
    _skill(tmp_path, "capped", name="Capped", always=True, priority=-5)
    with _Project(tmp_path):
        cat = D.discover(force=True)
        sel = S.choose(cat, "", {"offhere": False}, limit=1)
    whys = {o["why"] for o in sel.omitted}
    assert S.OUT_SHADOWED in whys
    assert S.OUT_CAP in whys
    for o in sel.omitted:
        assert o.get("id"), "an omission with no id cannot be looked up"
        assert o.get("why"), f"{o['id']} was omitted for no stated reason"
    payload = sel.to_payload()
    assert payload["considered"] == 4
    assert len(payload["omitted"]) == len(sel.omitted)


def test_the_reason_reaches_the_prompt_text(tmp_path):
    """The model is told WHY a skill is there — see `render_one`'s docstring."""
    _skill(tmp_path, "named-one", name="Namedone", body="do it")
    with _Project(tmp_path):
        cat = D.discover(force=True)
        text = S.prompt_block(S.choose(cat, "use namedone", {}))
    assert S.HEADING in text
    assert "Namedone" in text
    assert S.REASON_LABEL[S.REASON_REQUEST].split()[0].lower() in text.lower()


def test_every_reason_has_a_label_and_the_vocabulary_is_shared():
    for reason in S.REASONS:
        assert S.REASON_LABEL.get(reason), f"{reason} has no human label"
    d = SK.describe()
    assert list(d["reasons"]) == list(S.REASONS)
    assert set(d["labels"]) == set(S.REASON_LABEL)


def test_the_source_table_declares_skills_and_reads_its_ttl_live(monkeypatch):
    """`broker.sources` must not carry a copy of `SKILLS_TTL`."""
    from agent2.core.broker import sources as SRC
    assert B.SOURCE_SKILLS in B.ORDER
    assert SRC.PRIORITY[B.SOURCE_SKILLS] > 0
    monkeypatch.setattr(D, "SKILLS_TTL", 42.0)
    assert SRC.ttl_for(B.SOURCE_SKILLS) == 42.0
