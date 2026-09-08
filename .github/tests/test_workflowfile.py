# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for workflow FILES (``agent2/core/workflow/loader.py``, Task 38).

Run from the repo root:  python -m pytest .github/tests/test_workflowfile.py -v

What this suite is for
──────────────────────
Task 38's bar is **"an old schema still runs"**, and a suite that only checked a
version number would miss most of it: a file can be older than the build reading it
by its schema, by the words it uses for a field, or by containing YAML this reader
learned to skip. So the bar is pinned three ways —
``test_a_file_written_for_an_older_schema_is_migrated_not_refused`` (the ladder, with
a step installed at runtime because the shipped table is deliberately empty),
``test_a_newer_schema_is_a_warning_and_still_runs`` (an upgrade may not be a one-way
door), and ``test_an_unmapped_field_is_kept_rather_than_dropped``.

The tests that would go quiet first if the design were softened:

* ``test_the_second_parser_reads_what_the_flat_one_provably_cannot`` — written as a
  **pair**. It parses a real `nodes:` sequence with a `|` block instruction, then
  hands the same text to `skills.normalize.split_frontmatter` and asserts that reader
  genuinely cannot express it. Without the second half, "we needed our own parser"
  is an assertion about a file nobody read.
* ``test_the_two_stdlib_readers_agree_about_the_scalars_they_share`` — the cost of
  that decision, pinned in the other direction: two readers may diverge on shapes
  neither owns (`true`, `12`, `[a, b]`, `'quoted'`), and an import would have made
  that impossible while making the load machine-dependent.
* ``test_a_truncated_file_is_refused_rather_than_read_as_far_as_it_got`` — the tail
  of a YAML file is where the last node's `needs:` lives, so a clipped read is a
  *different graph*, not a smaller one. It asserts the refusal AND the absence of the
  unknown-dependency pile a half-read file would produce.
* ``test_one_unreadable_file_costs_one_workflow`` — the guard is per file, never one
  around the walk. Sabotage: hoist the try and this fails while everything else stays
  green.
* ``test_a_subdirectory_is_reported_rather_than_silently_skipped`` and
  ``test_two_files_claiming_one_name_are_both_accounted_for`` — a file the user wrote
  and this build never mentions is indistinguishable from a broken walk.
* ``test_nothing_outside_the_declared_subset_is_ever_guessed_at`` — a flow mapping,
  an anchor, a tag and a tab. Each is *counted*; none becomes a plausible string.
* ``test_the_filename_is_the_name_and_a_disagreeing_name_line_is_reported`` —
  `/workflow run audit` has to find `audit.yaml` whatever the file calls itself.
* ``test_a_workflow_read_off_disk_runs_on_the_existing_task_rows`` — end to end. A
  loader that produced a beautiful record the runner would not accept would satisfy
  every other test here and deliver nothing, which is
  `test_init.py::test_what_init_writes_is_in_the_very_next_prompt`'s lesson.
* ``test_the_loader_has_no_writer`` — structural. Task 39 owns creating a file; a
  reader that could write is one bug away from editing the user's workflow.

conftest.py redirects AGENT2_DB to a throwaway temp DB, so nothing here touches a
developer's real agent2.db.
"""

import builtins
import json
import os
from importlib import import_module
from pathlib import Path

import pytest

from agent2 import config
from agent2 import database as db
from agent2.core import workflow as WF
from agent2.core import workspace as W
from agent2.core.workflow import graph as G
from agent2.core.workflow import loader as L
from agent2.core.workflow import runner as R

#: ⚠️ `import agent2.core.skills.normalize as NRM` binds a **function** — the skills
#: package exports `normalize()` under that name, and `import a.b as c` resolves by
#: attribute since 3.7. Asking `sys.modules` for it is the only spelling that gets
#: the module, and getting the function instead is an `AttributeError` a reader would
#: have to debug rather than read.
NRM = import_module("agent2.core.skills.normalize")

_LOADER = Path(__file__).resolve().parent.parent.parent / "agent2" / "core" / "workflow" / "loader.py"


@pytest.fixture(autouse=True)
def _clean():
    """A cold cache either side, and the ladder empty as shipped.

    `UPGRADES` is module state a test may install a step into, and leaving one
    behind would silently migrate every later file in the session.
    """
    db.init_db()
    L.invalidate()
    L.UPGRADES.clear()
    yield
    L.invalidate()
    L.UPGRADES.clear()


# ── Helpers ────────────────────────────────────────────────────────────────────

_SIMPLE = """name: {name}
description: two nodes
nodes:
  - id: first
    title: First
    instruction: do the first thing
  - id: second
    needs: first
    instruction: do the second thing
"""


def _write(root: Path, name: str, text: str = "", *, suffix: str = "yaml") -> Path:
    """Write one workflow file under ``<root>/.agent2/workflows/``."""
    d = root / ".agent2" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.{suffix}"
    path.write_text(text or _SIMPLE.format(name=name.lower()), encoding="utf-8")
    return path


class _Project:
    """`with _Project(tmp):` — *tmp* is the workspace, the cache cold either side.

    Restoring the previous workspace is not optional: `workspace.root()` is
    process-wide, so a test that left it pointing at a temp directory would make
    every later test in the session read a project that no longer exists.
    """

    def __init__(self, root: Path):
        self.root = root

    def __enter__(self):
        self._prev = W.root()
        W.set_workspace(str(self.root))
        L.invalidate()
        return self

    def __exit__(self, *_exc):
        W.set_workspace(self._prev or os.getcwd())
        L.invalidate()
        return False


# ── The bar: an old schema still runs ─────────────────────────────────────────

def test_a_file_written_for_an_older_schema_is_migrated_not_refused(monkeypatch):
    """The ladder, exercised with a step installed at runtime.

    ⚠️ `UPGRADES` ships **empty** — version 1 is the first version, and seeding a
    fictional `0 → 1` step would put a fiction in the one table a future reader
    trusts (`projectdoc`'s empty `Invariants` tables, for their reason). So the
    mechanism is proved here instead: a v1 file that spells its nodes `steps:` under
    a build whose current schema is 2 comes back migrated, runnable, and SAYS it was
    migrated.
    """
    monkeypatch.setattr(G, "SCHEMA_VERSION", 2)
    L.UPGRADES[1] = lambda raw: {**raw, "nodes": raw.pop("steps", raw.get("nodes"))}

    wf = L.build("name: old\nversion: 1\nsteps:\n  - id: a\n  - id: b\n    needs: a\n",
                 name="old")

    assert wf.ok, wf.summary()
    assert wf.nodes == 2
    assert wf.schema == 2 and wf.upgraded_from == 1
    assert [w["code"] for w in wf.warnings] == [L.L_UPGRADED]
    assert L.oldest_supported() == 1


def test_an_upgrade_step_that_forgets_to_bump_the_version_cannot_loop(monkeypatch):
    """The loader stamps the version, never the step. `database.py`'s ladder rule.

    A migration that can run twice is worse than one that refuses, and here "twice"
    would be "forever" — a `while` over a version the step never changed.
    """
    monkeypatch.setattr(G, "SCHEMA_VERSION", 3)
    L.UPGRADES[1] = lambda raw: raw          # forgetful on purpose
    L.UPGRADES[2] = lambda raw: raw

    raw = {"name": "o", "version": 1, "nodes": [{"id": "a"}]}
    out, applied = L.upgrade(raw, version=1)

    assert applied == (1, 2)                  # it walked, it did not spin
    assert out["version"] == 3


def test_a_newer_schema_is_a_warning_and_still_runs():
    """Refusing a newer file would make every upgrade a one-way door.

    Somebody shares `audit.yaml` between two machines; the older build must still
    run what it understands. The mirror case — a version too OLD to map — is the
    problem, and `test_workflow.py` already pins it at the graph.
    """
    wf = L.build(f"name: n\nversion: {G.SCHEMA_VERSION + 5}\nnodes:\n"
                 f"  - id: a\n    instruction: go\n", name="n")

    assert wf.ok
    assert G.W_NEW_SCHEMA in [w["code"] for w in wf.validation.warnings]


def test_an_unmapped_field_is_kept_rather_than_dropped():
    """A field a later schema adds must survive a read by this one.

    Dropping it would make an older build silently rewrite a newer workflow the
    moment Task 39's editor saves it back.
    """
    wf = L.build("name: k\nnodes:\n  - id: a\n    instruction: go\n"
                 "budget: 40\nowner: security\n", name="k")

    assert wf.ok
    assert wf.defn.extra.get("budget") == 40
    assert wf.defn.extra.get("owner") == "security"


# ── Why this module owns a parser at all ──────────────────────────────────────

def test_the_second_parser_reads_what_the_flat_one_provably_cannot():
    """A PAIR: what a workflow needs, and what the skills reader can express.

    ⚠️ The second half is the point. `skills.normalize.split_frontmatter` is
    deliberately flat — `key: value` and block lists of scalars — so it cannot
    represent `nodes:` as a sequence of mappings, and a node instruction has to be a
    multi-line `|` block. Without this assertion, "we needed our own parser" is a
    claim about a file nobody read, and the honest alternative (teach the skills
    reader to nest) would put two subsystems' requirements on one turn-path parser.
    """
    text = ("name: pair\n"
            "nodes:\n"
            "  - id: recon\n"
            "    instruction: |\n"
            "      Enumerate the host.\n"
            "\n"
            "      Note every open port.\n"
            "  - id: report\n"
            "    needs: recon\n")

    wf = L.build(text, name="pair")
    assert wf.ok, wf.summary()
    first = wf.defn.node("recon")
    assert first.instruction == "Enumerate the host.\n\nNote every open port."
    assert wf.defn.node("report").needs == ("recon",)

    # And the flat reader, on the very same bytes. ⚠️ It does not fail — it succeeds
    # with a DIFFERENT shape: `nodes` comes back as a list of the string "id: recon"
    # and the node's own `instruction` is hoisted to the top level, where it reads as
    # the workflow's. That is the case for a second parser rather than a shared one:
    # a silently wrong graph is worse than a refusal, and teaching this reader to
    # nest would put workflow requirements on the skills turn path.
    front, _body, _unparsed = NRM.split_frontmatter(f"---\n{text}---\n")
    assert front.get("nodes") == ["id: recon"], (
        "split_frontmatter grew nesting — re-check whether loader._Parser is still "
        "the smaller answer, and whether skills now carries workflow requirements"
    )
    assert "report" in str(front.get("instruction")), (
        "the flat reader hands the SECOND node's id back as the first node's "
        "instruction — a silently wrong graph, which is the whole argument"
    )


@pytest.mark.parametrize(("src", "want"), [
    ("true", True), ("yes", True), ("on", True),
    ("false", False), ("no", False), ("off", False),
    ("12", 12), ("-3", -3),
    ("'quoted'", "quoted"), ('"quoted"', "quoted"),
    ("[a, b]", ["a", "b"]), ("[]", []),
    ("plain words", "plain words"), ("", ""),
])
def test_the_two_stdlib_readers_agree_about_the_scalars_they_share(src, want):
    """Two readers, one meaning — checked, because nothing structural enforces it.

    This is the cost of not importing `normalize._scalar`, and it is paid on purpose:
    that reader may need to change for skills' reasons, and a workflow that already
    runs must not move underneath the user. What must never happen is the two
    *disagreeing* about `off` or `[a, b]`.
    """
    assert L._scalar(src) == want
    assert NRM._scalar(src) == want


@pytest.mark.parametrize(("src", "here", "there"), [
    ("null", None, ""), ("~", None, ""), ("none", None, ""),
    ("1.5", 1.5, "1.5"),
])
def test_where_the_two_readers_differ_the_difference_is_written_down(src, here, there):
    """⚠️ The divergence is DELIBERATE, so it is pinned rather than discovered.

    Both differences are this reader being more specific, and each is a field that
    exists here and not there:

      * `null` → `None`, because a workflow field distinguishes *absent* from
        *explicitly nothing* — an `instruction: null` is a node the author emptied on
        purpose. Skills has no such field: its tri-state is a `skill_state` row, not a
        line in somebody's file, so `''` costs it nothing.
      * `1.5` → a float, because Task 41's ceilings (`max_cost`) are decimals, and a
        budget silently read as the string "1.5" compares wrong rather than failing.

    If either side ever changes, this test says so before a workflow moves under a
    user who did not edit it.
    """
    assert L._scalar(src) == here
    assert NRM._scalar(src) == there


# ── What can be wrong with a file ─────────────────────────────────────────────

def test_a_truncated_file_is_refused_rather_than_read_as_far_as_it_got(monkeypatch):
    """⚠️ A clipped declaration is a DIFFERENT graph, not a smaller one.

    A skill is still useful half-read. A workflow is not: the tail of the file is
    where the last node's `needs:` lives, so reading as far as the ceiling allowed
    would surface as a pile of unknown-dependency errors pointing at nodes the author
    did write — a wrong answer that looks like the user's mistake.
    """
    monkeypatch.setattr(config, "WORKFLOW_MAX_BYTES", 64)
    text = _SIMPLE.format(name="big") + "\n# padding\n" * 40

    wf = L.build(text[:64], name="big", truncated=True)

    assert not wf.ok
    assert [p["code"] for p in wf.problems] == [L.L_TRUNCATED]
    assert "AGENT2_WORKFLOW_MAX_BYTES" in wf.problems[0]["message"]
    assert wf.validation is None, "a clipped file may not be graded as a graph"


@pytest.mark.parametrize(("text", "code"), [
    ("", L.L_EMPTY),
    ("# only a comment\n", L.L_EMPTY),
    ("- a\n- b\n", L.L_NOT_MAPPING),
    ("just a sentence\n", L.L_NOT_MAPPING),
])
def test_a_file_that_is_not_a_workflow_says_so_in_one_line(text, code):
    wf = L.build(text, name="x")
    assert not wf.ok
    assert [p["code"] for p in wf.problems] == [code]
    assert wf.summary()                      # a human gets a sentence, not a code


def test_malformed_json_is_reported_with_the_stdlibs_own_line_number():
    """JSON is strict, and that is a better answer than a subset reader's shrug."""
    wf = L.build('{"name": "j", "nodes": [{"id": "a"},]}', name="j", fmt="json")
    assert not wf.ok
    assert [p["code"] for p in wf.problems] == [L.L_PARSE]
    assert "line" in wf.problems[0]["message"].lower()


def test_json_is_read_by_the_stdlib_and_produces_the_same_graph():
    """`.json` exists so Task 40's planner can emit a workflow without a writer."""
    payload = {"name": "j", "nodes": [{"id": "a", "instruction": "go"},
                                      {"id": "b", "needs": ["a"], "instruction": "go"}]}
    wf = L.build(json.dumps(payload), name="j", fmt="json")
    assert wf.ok and wf.nodes == 2
    assert wf.validation.levels == (("a",), ("b",))


def test_nothing_outside_the_declared_subset_is_ever_guessed_at():
    """⚠️ COUNTED, NEVER GUESSED — `normalize`'s `unparsed` discipline.

    A flow mapping, an anchor and a tag are all valid YAML that a naive line reader
    would hand back as a *string that looks like data*. Each is skipped and counted,
    the key does not arrive as a plausible value, and the file still loads.
    """
    text = ("name: odd\n"
            "flow: {a: 1}\n"
            "anchored: &ref 1\n"
            "tagged: !!str hi\n"
            "nodes:\n"
            "  - id: a\n"
            "    instruction: go\n"
            "\tbad: tab indented\n")

    wf = L.build(text, name="odd")

    assert wf.ok, wf.summary()               # the workflow still runs
    assert wf.unparsed == 4
    assert L.L_UNPARSED in [w["code"] for w in wf.warnings]
    assert all(k not in wf.defn.extra for k in ("flow", "anchored", "tagged"))
    assert any("tab" in n for n in wf.notes)
    assert all(":" in n and n.startswith("line ") for n in wf.notes)


def test_notes_are_capped_so_a_broken_file_cannot_flood_two_surfaces():
    wf = L.build("name: f\nnodes:\n  - id: a\n" + "".join(
        f"k{i}: {{x: {i}}}\n" for i in range(40)), name="f")
    assert wf.unparsed == 40                 # every one is COUNTED
    assert len(wf.notes) <= L.Parsed.MAX_NOTES   # only the first few are quoted


def test_a_hash_inside_a_value_is_not_a_comment():
    """`instruction: curl http://h/#frag` keeps its fragment.

    A `#` starts a comment only at the start of a value or after whitespace, and a
    URL in a node instruction is not a rare shape in a security tool.
    """
    wf = L.build("name: h\nnodes:\n  - id: a\n"
                 "    instruction: curl http://h/p#frag   # really a comment\n",
                 name="h")
    assert wf.defn.node("a").instruction == "curl http://h/p#frag"


def test_ok_is_decided_by_file_problems_and_the_graph_never_by_warnings():
    """`core/health.py`'s rule. A warning that can block a run stops being read."""
    wf = L.build(f"name: elsewhere\nversion: {G.SCHEMA_VERSION + 1}\n"
                 f"stray: {{a: 1}}\nnodes:\n  - id: a\n", name="mine")

    codes = {w["code"] for w in wf.warnings}
    assert {L.L_NAME_MISMATCH, L.L_UNPARSED} <= codes
    assert wf.ok, "three warnings, and it still runs"

    cyclic = L.build("name: c\nnodes:\n  - id: a\n    needs: b\n"
                     "  - id: b\n    needs: a\n", name="c")
    assert not cyclic.problems, "the FILE was fine"
    assert not cyclic.ok, "the graph was not, and the file inherits that verdict"


# ── Identity ──────────────────────────────────────────────────────────────────

def test_the_filename_is_the_name_and_a_disagreeing_name_line_is_reported():
    """⚠️ `/workflow run audit` must find `audit.yaml` whatever it calls itself.

    Silently preferring `name:` gives the user a workflow they cannot run by the name
    they can see; silently ignoring it leaves them editing a line that does nothing.
    So the filename wins and the disagreement is said out loud —
    `skills.Skill.id`-vs-`name`, for its reason.
    """
    wf = L.build("name: something-else\nnodes:\n  - id: a\n", name="audit")

    assert wf.name == "audit" and wf.defn.name == "audit"
    assert wf.declared_name == "something-else"
    said = next(w for w in wf.warnings if w["code"] == L.L_NAME_MISMATCH)
    assert said["declared"] == "something-else" and said["used"] == "audit"


def test_a_name_is_folded_so_one_file_is_one_workflow_on_every_os(tmp_path):
    """`Audit.yaml` and `audit.yaml` are one file on Windows and two on Linux.

    Folding the stem is what makes `/workflow run audit` mean the same thing in both
    places — `graph.fold_id`'s own reason for being lower-case.
    """
    _write(tmp_path, "Audit")
    with _Project(tmp_path):
        cat = L.discover(force=True)
    assert cat.names() == ("audit",)
    assert cat.get("AUDIT") is not None and cat.find("Aud") is not None


def test_an_ambiguous_prefix_returns_nothing_rather_than_a_guess(tmp_path):
    """Running the wrong workflow is more expensive than asking again."""
    _write(tmp_path, "web-audit")
    _write(tmp_path, "web-recon")
    with _Project(tmp_path):
        cat = L.discover(force=True)
    assert cat.find("web") is None
    assert cat.find("web-a").name == "web-audit"


# ── Discovery ─────────────────────────────────────────────────────────────────

def test_a_missing_folder_is_empty_and_quiet(tmp_path):
    """No `.agent2/workflows/` is the normal case, not a fault."""
    with _Project(tmp_path):
        cat = L.discover(force=True)
    assert cat.files == [] and cat.errors == []
    assert cat.exists is False
    assert Path(cat.root) == tmp_path / ".agent2" / "workflows"


def test_the_root_is_the_live_workspace_not_a_latched_one(tmp_path):
    """A module-level path binds the directory the process started in.

    `skills.skills_root`'s rule: every later workspace switch would keep reading the
    first project's files, and in dual mode that is two surfaces disagreeing about
    which project they are in.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "alpha")
    _write(b, "beta")
    with _Project(a):
        assert L.discover(force=True).names() == ("alpha",)
    with _Project(b):
        assert L.discover(force=True).names() == ("beta",)


def test_one_unreadable_file_costs_one_workflow(tmp_path, monkeypatch):
    """⚠️ ONE GUARD PER FILE, never one around the walk.

    Sabotage: hoist the try out of the loop and this is the only test that fails —
    every other one still passes, because they all read folders with nothing broken
    in them.
    """
    _write(tmp_path, "good")
    bad = _write(tmp_path, "bad")
    real_open = builtins.open

    def refuse(path, *a, **k):
        if str(path) == str(bad):
            raise PermissionError("locked by another process")
        return real_open(path, *a, **k)

    # A module global shadows the builtin for code inside `loader`, which is the
    # closest a test gets to a file another process holds open.
    monkeypatch.setattr(L, "open", refuse, raising=False)
    with _Project(tmp_path):
        cat = L.discover(force=True)

    assert cat.names() == ("bad", "good")
    assert cat.get("good").ok, "a broken sibling may not cost a working workflow"
    broken = cat.get("bad")
    assert not broken.ok
    assert [p["code"] for p in broken.problems] == [L.L_UNREADABLE]
    assert "locked" in broken.problems[0]["message"]


def test_a_subdirectory_is_reported_rather_than_silently_skipped(tmp_path):
    """⚠️ A name is a WORD a human types, so the folder is flat — and says so.

    Two `audit.yaml` files in two subfolders would be one name with two definitions.
    Not descending is the right answer; not *mentioning* it is how a user ends up
    rewriting a file that was already fine.
    """
    d = tmp_path / ".agent2" / "workflows" / "nested"
    d.mkdir(parents=True)
    (d / "deep.yaml").write_text("name: deep\nnodes:\n  - id: a\n", encoding="utf-8")
    _write(tmp_path, "top")

    with _Project(tmp_path):
        cat = L.discover(force=True)

    assert cat.names() == ("top",)
    assert cat.nested == 1
    assert any("subdirector" in e for e in cat.errors)


def test_two_files_claiming_one_name_are_both_accounted_for(tmp_path):
    """`audit.yaml` beside `audit.json`: one wins, and the loser is NAMED.

    The loser is a file the user is editing and watching do nothing, which is the
    one failure mode they cannot debug from the outside.
    """
    _write(tmp_path, "audit")
    _write(tmp_path, "audit", text='{"name": "audit", "nodes": [{"id": "z"}]}',
           suffix="json")

    with _Project(tmp_path):
        cat = L.discover(force=True)

    assert cat.names() == ("audit",)
    assert cat.files_seen == 2
    assert any("already declared by" in e for e in cat.errors)


def test_the_file_ceiling_is_reported_when_it_engages(tmp_path, monkeypatch):
    for i in range(6):
        _write(tmp_path, f"w{i}")
    monkeypatch.setattr(config, "WORKFLOW_MAX_FILES", 2)
    with _Project(tmp_path):
        cat = L.discover(force=True)
    assert len(cat.files) == 2
    assert cat.truncated and cat.truncated_by == L.BY_COUNT


def test_the_time_ceiling_is_reported_when_it_engages(tmp_path, monkeypatch):
    """A partial answer may never read as a complete one — `/init`'s rule."""
    _write(tmp_path, "one")
    _write(tmp_path, "two")
    monkeypatch.setattr(config, "WORKFLOW_SCAN_BUDGET_SEC", -1.0)
    with _Project(tmp_path):
        cat = L.discover(force=True)
    assert cat.truncated and cat.truncated_by == L.BY_BUDGET


def test_the_order_is_deterministic_so_a_prefix_resolves_the_same_everywhere(tmp_path):
    """The filesystem hands back whatever order it likes; two machines may not
    disagree about which of two files a prefix resolves to."""
    for name in ("zulu", "alpha", "Mike", "bravo"):
        _write(tmp_path, name)
    with _Project(tmp_path):
        names = L.discover(force=True).names()
    assert list(names) == sorted(names)


def test_a_file_symlinked_outside_the_folder_is_refused_and_recorded(tmp_path):
    """A symlink is the filesystem's form of cross-project leakage.

    From every other angle it is an ordinary local file, which is why containment is
    asked per file through the sandbox's own predicate rather than assumed from the
    directory listing.
    """
    outside = tmp_path / "other" / "secret.yaml"
    outside.parent.mkdir(parents=True)
    outside.write_text("name: secret\nnodes:\n  - id: a\n", encoding="utf-8")
    here = tmp_path / "here"
    d = here / ".agent2" / "workflows"
    d.mkdir(parents=True)
    try:
        (d / "link.yaml").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted for this user")

    with _Project(here):
        cat = L.discover(force=True)

    linked = cat.get("link")
    assert linked is not None and not linked.ok
    assert [p["code"] for p in linked.problems] == [L.L_OUTSIDE]


def test_a_catalog_is_cached_and_force_is_what_bypasses_it(tmp_path):
    """`/workflow` redraws must be free; a human editing a file must not wait long."""
    _write(tmp_path, "one")
    with _Project(tmp_path):
        first = L.discover()
        assert L.discover() is first, "a second read inside the TTL is the same object"
        _write(tmp_path, "two")
        assert L.discover().names() == ("one",), "still the cached answer"
        assert L.discover(force=True).names() == ("one", "two")


def test_a_project_switch_cannot_serve_another_checkouts_workflows(tmp_path):
    """⚠️ THE KEY NAMES BOTH THE ROOT AND THE PROJECT, and each covers a case the
    other cannot.

    `skills.discovery`'s sabotage found the mirror of this: a warm entry keyed on
    anything constant answers project B with project A's folder for the whole TTL —
    recursive discovery reading the wrong directory, with a plausible list on both
    surfaces. So both halves are pinned:

      * a **workspace switch** cannot cross, with no invalidation at all (asserted
        without `force`, which is why the hook in the next test is a second belt
        rather than the only one);
      * an explicit `root=` cannot cross either — and that is what the *root* half of
        the key is for, because `isolation.current()` follows the workspace and would
        be identical for both of these calls.
    """
    a, b = tmp_path / "pa", tmp_path / "pb"
    _write(a, "from-a")
    _write(b, "from-b")
    prev = W.root()
    try:
        W.set_workspace(str(a))
        L.invalidate()
        assert L.discover().names() == ("from-a",)
        W.set_workspace(str(b))              # exactly what a workspace switch does
        assert L.discover().names() == ("from-b",), (
            "a warm catalog answered for the wrong project"
        )
        W.set_workspace(str(a))
        assert L.discover().names() == ("from-a",), "and back, still not crossed"

        # One project, two explicit roots — the workspace never moves, so only the
        # root half of the key separates these.
        assert L.discover(a).names() == ("from-a",)
        assert L.discover(b).names() == ("from-b",), (
            "two roots shared one cache entry — the key stopped naming the root"
        )
    finally:
        W.set_workspace(prev or os.getcwd())
        L.invalidate()


def test_the_cache_is_dropped_by_isolation_not_by_a_second_switch_listener(tmp_path):
    """⚠️ ONE registration, and it is `broker.isolation`'s — not a second
    `workspace.manager.on_switch` listener.

    `isolation` already owns that wiring *and* the `workspace` sync topic, so a
    switch in the **other process** counts too — which a local `on_switch` hook would
    miss entirely in dual mode. Registering in both places would be two hooks for one
    event, and the one that stopped firing would be invisible until another
    checkout's workflow showed up in this one.
    """
    from agent2.core.broker import isolation as ISO

    _write(tmp_path, "one")
    with _Project(tmp_path):
        L.discover()
        assert L._CACHE, "nothing was cached, so this test proves nothing"
        assert L.invalidate in ISO._listeners, (
            "the loader is not registered with isolation — a project change in the "
            "other process would leave this catalog warm"
        )
        ISO.invalidate()                     # what a switch or a scope change fires
        assert not L._CACHE, "the broadcast did not reach the loader"


def test_load_finds_one_workflow_by_name_or_prefix(tmp_path):
    _write(tmp_path, "audit")
    with _Project(tmp_path):
        L.discover(force=True)
        assert L.load("audit").name == "audit"
        assert L.load("aud").name == "audit"
        assert L.load("nope") is None


def test_the_suffix_a_new_file_gets_is_one_discovery_reads(tmp_path):
    """`path_for` and `SUFFIXES` may not drift: `/workflow new audit` has to produce
    a file the very next `/workflow` lists."""
    with _Project(tmp_path):
        target = L.path_for("Audit")
        assert target.suffix.lstrip(".") in L.SUFFIXES
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_SIMPLE.format(name="audit"), encoding="utf-8")
        assert L.discover(force=True).get("audit") is not None


# ── Totality ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("junk", [
    "", "\n\n\n", "---\n", "---\n---\n", ":\n", "-\n-\n-\n", "\t\t\n",
    "key:\n" * 50, "  indented: first\nname: x\n", "name: 'unterminated\n",
    "nodes:\n" + "  - id: a\n" * 3 + "    needs: [a, ,]\n",
    "name: x\nnodes: notalist\n", "name: x\nnodes: []\n",
    "﻿name: bom\nnodes:\n  - id: a\n",
    "name: crlf\r\nnodes:\r\n  - id: a\r\n",
])
def test_reading_junk_never_raises(junk):
    """Total by contract — `gitstate.snapshot()`'s bar.

    Every one of these is something an editor, a generator or a half-finished save
    can produce, and none of them may reach a caller as an exception.
    """
    wf = L.build(junk, name="junk")
    assert isinstance(wf.ok, bool)
    assert isinstance(wf.to_payload(), dict)
    assert isinstance(wf.summary(), str)


def test_deep_nesting_is_reported_never_recursed_into():
    """⚠️ `graph._find_cycles`' rule: a malformed file is *reported*, not a
    RecursionError on the turn path."""
    text = "name: deep\ntree:\n" + "".join(f"{'  ' * (i + 1)}k{i}:\n" for i in range(1, 40))
    wf = L.build(text, name="deep")
    assert isinstance(wf.ok, bool)
    assert wf.unparsed >= 1
    assert any(f"deeper than {L.MAX_NEST}" in n for n in wf.notes)


def test_a_pathological_file_is_bounded_in_time():
    """10 000 lines of nothing must not be a hang: every loop body consumes a line."""
    import time
    text = "name: big\nnodes:\n" + "  - id: n{}\n".format(0) + "".join(
        f"  - id: n{i}\n    needs: n{i - 1}\n" for i in range(1, 400))
    started = time.monotonic()
    wf = L.build(text, name="big")
    assert time.monotonic() - started < 2.0
    assert wf.unparsed == 0
    # ⚠️ The node CEILING refuses rather than truncating — `test_workflow.py` owns
    # that rule; here it only has to arrive as a stated problem.
    assert not wf.ok
    assert G.P_TOO_MANY in [p["code"] for p in wf.validation.problems]


def test_discover_never_raises_even_with_no_workspace(monkeypatch):
    monkeypatch.setattr(L, "workflows_root",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no root")))
    cat = L.discover()
    assert cat.files == [] and isinstance(cat.to_payload(), dict)


# ── Privacy and posture ───────────────────────────────────────────────────────

def test_a_node_instruction_is_not_in_the_default_payload():
    """⚠️ `nodes` DEFAULTS OFF — `skills.Skill.to_payload(body=False)`'s rule.

    A node instruction is prose a human wrote in their own checkout, and a listing
    surface has no need of it. The one caller that does need it asks.
    """
    secret = "INSTRUCTION-DO-NOT-LEAK-38"
    wf = L.build(f"name: p\nnodes:\n  - id: a\n    instruction: {secret}\n", name="p")

    assert secret not in json.dumps(wf.to_payload())
    assert secret in json.dumps(wf.to_payload(nodes=True))


def test_the_catalog_payload_is_one_answer_for_both_surfaces(tmp_path):
    """`/workflow` and `GET /api/workflows` are two renderers over this dict."""
    _write(tmp_path, "one")
    with _Project(tmp_path):
        payload = L.discover(force=True).to_payload()
    for key in ("root", "project", "exists", "enabled", "count", "runnable",
                "workflows", "truncated", "truncated_by", "errors", "files_seen",
                "nested", "ms"):
        assert key in payload, key
    assert payload["count"] == 1 and payload["runnable"] == 1


def test_describe_states_which_yaml_this_build_actually_reads():
    """⚠️ Derived, never documented twice: "what shape does a workflow file take"
    is the first question when one does not load, and a README can go stale."""
    d = L.describe()
    assert d["yaml"] is False and d["yaml_subset"] is True
    assert set(d["suffixes"]) == set(L.SUFFIXES)
    assert d["oldest_supported"] == G.MIN_SCHEMA and d["upgrades"] == []
    assert d["max_nest"] == L.MAX_NEST
    assert any("nodes" in line or "- " in line for line in d["subset"])
    assert set(d["file_problems"]) == set(L.FILE_PROBLEM_CODES)
    # ⚠️ Every code a payload can carry is declared, so a renderer can be total.
    assert d["max_files"] == config.WORKFLOW_MAX_FILES


def test_the_facade_reports_the_files_alongside_the_runs():
    """One `describe()`/`stats()`, so `/workflow` and `core.health` agree."""
    assert WF.describe()["yaml_subset"] is True
    assert "files" in WF.stats()
    assert WF.loader is L


def test_stats_are_counters_and_never_a_workflow_name(tmp_path):
    _write(tmp_path, "secretname")
    with _Project(tmp_path):
        L.discover(force=True)
        blob = json.dumps(L.stats())
    assert "secretname" not in blob
    for key in ("cached", "ttl", "scans", "hits", "max_files", "max_bytes"):
        assert key in L.stats(), key


def test_the_loader_has_no_writer():
    """⚠️ STRUCTURAL, not a promise. Task 39 owns creating a workflow file.

    `skills`' rule, for its reason: the folder belongs to the user, and a reader that
    can write is one bug away from editing the workflow they are running.
    """
    src = _LOADER.read_text(encoding="utf-8")
    for forbidden in ("write_text", "write_bytes", "mkdir", "unlink", "shutil",
                      "os.remove", "rename"):
        assert forbidden not in src, f"loader.py must not {forbidden}"
    # ⚠️ And the ONE read is explicitly read-only: a mode string is what the
    # assertion above cannot see, so it is pinned separately rather than trusted.
    assert src.count("open(") == 1
    assert 'open(path, "rb")' in src


def test_the_disabled_switch_is_reported_rather_than_pretended_away(tmp_path,
                                                                   monkeypatch):
    """`AGENT2_WORKFLOWS=0` — the catalog still says what is on disk AND that the
    feature is off, `/skills`' rule: a command that silently shows a list nothing
    will ever use is worse than one that names the switch."""
    _write(tmp_path, "one")
    monkeypatch.setattr(config, "WORKFLOW_ENABLED", False)
    with _Project(tmp_path):
        cat = L.discover(force=True)
    assert cat.enabled is False
    assert cat.names() == ("one",)


# ── End to end ────────────────────────────────────────────────────────────────

def test_a_workflow_read_off_disk_runs_on_the_existing_task_rows(tmp_path):
    """⚠️ THE ONE TEST THAT MAKES TASK 38 WORTH SHIPPING.

    A loader that produced a beautiful record `runner.instantiate()` would not accept
    passes every other test in this file and delivers nothing — the lesson
    `test_init.py::test_what_init_writes_is_in_the_very_next_prompt` was written for.
    So: a file a human could type, read through discovery, instantiated onto the
    task rows Task 37 uses, with the order the file declared.
    """
    _write(tmp_path, "audit", text=(
        "# a sweep somebody actually wrote\n"
        "name: audit\n"
        "description: recon then two scans then a report\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: recon\n"
        "    title: Recon\n"
        "    instruction: |\n"
        "      Enumerate the host.\n"
        "      Note every open port.\n"
        "  - id: scan-web\n"
        "    needs: recon\n"
        "    resource: burp\n"
        "    instruction: Scan the web app.\n"
        "  - id: scan-tls\n"
        "    needs: [recon]\n"
        "    instruction: Check the TLS configuration.\n"
        "  - id: report\n"
        "    needs: scan-web, scan-tls\n"
        "    priority: 9\n"
        "    instruction: Write it up.\n"))

    with _Project(tmp_path):
        wf = L.load("audit", force=True)
        assert wf is not None and wf.ok, wf.summary() if wf else "not found"
        # ⚠️ A level is in the FILE's order, not sorted: `levels_for` groups by depth
        # and keeps declaration order inside a group, so two independent scans run in
        # the order their author wrote them. Sorting here would be a second opinion
        # about the plan, and Task 41 dispatches off exactly this tuple.
        assert wf.validation.levels == (("recon",), ("scan-web", "scan-tls"),
                                        ("report",))

        run = R.instantiate(wf.defn, chat_id="chat-wf38")
        assert run.ok, run.reason

        state = R.state_for(run.run_id)
        assert {n.node for n in state.nodes} == {"recon", "scan-web", "scan-tls",
                                                "report"}
        assert state.total == 4 and state.done == 0
        assert [n.node for n in state.ready] == ["recon"], (
            "the file's dependencies became the task rows' dependencies"
        )
        assert {n.node for n in state.nodes if n.phase == R.PHASE_BLOCKED} == {
            "scan-web", "scan-tls", "report"}
        assert wf.defn.node("scan-web").resource == "burp", (
            "Task 41's `resource` survives the round trip through the file"
        )
    db.exe("DELETE FROM exec_workflows")
