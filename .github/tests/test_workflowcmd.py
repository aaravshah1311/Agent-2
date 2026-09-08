# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for AUTHORING a workflow (``agent2/core/workflow/authoring.py``, Task 39) and
for the two surfaces that drive it — ``/workflow`` in the CLI and ``/api/workflows``
in the browser.

Run from the repo root:  python -m pytest .github/tests/test_workflowcmd.py -v

What this suite is for
──────────────────────
Task 38 gave this build a **reader**; Task 39 gives it a **writer**, and a writer
that reaches the filesystem on a name a user typed is the one place in the workflow
subsystem where a bug is not merely a wrong answer. So the bar here is three things
that have nothing to do with each other:

1. **What is written is what the reader accepts.** ``authoring.template()`` fed
   through ``loader.build()`` has to come back ``ok`` — a template that produced a
   plausible file the runner refuses would be discovered by the user, on their first
   ``/workflow run``, after they had already edited it.
2. **A name is validated before a path is built.** ``graph.fold_id`` lower-folds and
   collapses whitespace; it does *not* remove a separator or a ``..``. Feeding one
   straight to ``loader.path_for`` is how ``new ../../evil`` escapes the project, so
   the order is asserted directly and by outcome.
3. **A refusal is a return value.** Both surfaces print; neither catches. Every gate
   — ``fs.write``, ``fs.delete``, ``chat`` — is asked *live* by the module that owns
   it, so a denial has to arrive as ``ok=False`` plus a reason, with nothing written.

The tests that would go quiet first if the design were softened:

* ``test_the_template_this_build_writes_is_a_file_this_build_runs`` — end to end,
  ``template() → build() → instantiate()``. It is the workflow half of
  ``test_init.py::test_what_init_writes_is_in_the_very_next_prompt``: a writer whose
  output the reader rejects satisfies every unit test in isolation.
* ``test_a_name_that_escapes_the_project_is_refused_before_a_path_exists`` — written
  as a **pair**: the refusal, *and* a walk of the temp tree proving no file landed
  anywhere. Asserting only ``ok is False`` would stay green against a writer that
  refused *after* creating the directory.
* ``test_create_never_overwrites_a_plan_somebody_wrote`` — the file is the only copy.
* ``test_a_denied_capability_refuses_by_returning_and_writes_nothing`` and its delete
  twin — sabotage: turn either gate into an exception and the CLI prints a traceback
  where it used to print a sentence.
* ``test_ok_and_runnable_are_two_facts`` — a supplied body can be saved perfectly and
  still not be a graph. Collapse them and a panel says "created" about a file that
  cannot run, which is the ``✓`` a user believes.
* ``test_the_authoring_module_writes_only_through_one_helper`` — structural, the
  mirror of ``test_workflowfile.py::test_the_loader_has_no_writer``.
* ``test_a_freshly_written_workflow_is_visible_to_the_very_next_read`` — the TTL is
  five seconds and a human edits faster than that.
* ``test_delete_opens_on_keep_so_enter_and_escape_both_keep_the_file`` — the one
  irreversible verb, pinned at the CLI layer where the default lives.
* ``test_every_cli_verb_survives_a_project_with_nothing_in_it`` — totality across the
  whole grammar, including the two that take no argument.

conftest.py redirects AGENT2_DB to a throwaway temp DB, so nothing here touches a
developer's real agent2.db.
"""

import ast
import os
from pathlib import Path

import pytest
from flask import Flask

from agent2 import config
from agent2 import database as db
from agent2.core import permissions as perms
from agent2.core import workflow as WF
from agent2.core import workspace as W
from agent2.core.workflow import authoring as A
from agent2.core.workflow import graph as G
from agent2.core.workflow import loader as L
from agent2.core.workflow import runner as R
from agent2.server.routes import register_routes

_AUTHORING = (Path(__file__).resolve().parent.parent.parent
              / "agent2" / "core" / "workflow" / "authoring.py")


@pytest.fixture(autouse=True)
def _clean():
    """A cold discovery cache either side, and no denial left behind."""
    db.init_db()
    L.invalidate()
    os.environ.pop("AGENT2_DENY_CAPS", None)
    yield
    L.invalidate()
    os.environ.pop("AGENT2_DENY_CAPS", None)


class _Project:
    """`with _Project(tmp):` — *tmp* is the workspace, restored on the way out.

    `workspace.root()` is process-wide, so a test that left it pointing at a temp
    directory would make every later test in the session read a project that is gone.
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


def _files_under(root: Path) -> list[str]:
    """Every file in the tree, as POSIX-relative text. For "nothing was written"."""
    return sorted(p.relative_to(root).as_posix()
                  for p in root.rglob("*") if p.is_file())


# ── The bar: what this build writes is what this build runs ───────────────────

def test_the_template_this_build_writes_is_a_file_this_build_runs(tmp_path):
    """`template()` → `build()` → `instantiate()`, in one test, on purpose.

    ⚠️ Every step of that chain has its own tests and they would all pass against a
    template the *runner* refuses — the loader would parse it, the graph would
    validate in isolation, and the failure would surface on the user's first
    `/workflow run`, after they had edited the file. This is the same lesson
    `test_init.py::test_what_init_writes_is_in_the_very_next_prompt` pins for the
    project doc: a writer is only correct with respect to its reader.
    """
    with _Project(tmp_path):
        res = A.create("audit", description="probe the login form")
        assert res.ok, res.reason
        assert res.runnable, res.problems
        assert Path(res.path).is_file()

        # The reader's view of the bytes, not a second parse.
        wf = L.load("audit", force=True)
        assert wf is not None and wf.ok, wf and wf.summary()
        assert wf.nodes == A.describe()["template_nodes"]
        assert "probe the login form" in Path(res.path).read_text(encoding="utf-8")

        run = R.instantiate(wf.defn, chat_id="c-tmpl")
        assert run.ok, run.reason
        st = R.state_for(run.run_id)
        assert st.total == wf.nodes
        assert st.done == 0
        cur = st.current()
        assert cur is not None and cur.node == wf.defn.nodes[0].id


def test_a_template_carries_no_placeholder_that_reads_as_a_real_instruction(tmp_path):
    """A seeded example a human might mistake for a plan is worse than an empty one.

    `projectdoc`'s empty `Invariants` tables, for the same reason: the file is read
    back into every prompt of a run, so a node whose instruction is filler has to be
    obviously filler. Asserted as *the description says replace me*, and as *no node
    instruction claims to be about this project*.
    """
    text = A.template("demo")
    assert "replace" in text.lower()
    wf = A.preview(text, name="demo")
    assert wf.ok, wf.summary()
    # Titles are generic verbs, not statements about a codebase nobody scanned.
    assert [n.id for n in wf.defn.nodes] == ["plan", "build", "verify"]


# ── The bar: a name is validated before a path is built ───────────────────────

@pytest.mark.parametrize("bad", ["../../evil", "a/b", "..", "-nope", "", "   ",
                                 "C:\\windows\\system32\\x", "a\\b"])
def test_a_name_that_escapes_the_project_is_refused_before_a_path_exists(tmp_path, bad):
    """Refused, AND nothing anywhere in the tree — written as a pair.

    ⚠️ `graph.fold_id` lower-folds and collapses whitespace. It does **not** strip a
    separator or a `..`, so a writer that folded and then called `loader.path_for`
    would resolve `../../evil` outside the project and succeed. Asserting only
    `ok is False` would also stay green against a writer that refused *after*
    creating `.agent2/workflows/`, so the second half walks the whole temp tree.
    """
    with _Project(tmp_path):
        before = _files_under(tmp_path)
        res = A.create(bad)
        assert not res.ok
        assert res.reason, "a refusal has to say why"
        assert _files_under(tmp_path) == before, "the refusal still touched the disk"


def test_an_unsluggable_name_is_refused_with_a_suggestion_never_silently_slugged(tmp_path):
    """`new My Audit` is a typo, not a request to create `my-audit`.

    Silently slugging means `/workflow run "My Audit"` then finds nothing and the user
    has a file they did not name. The suggestion is in the *message*; the name is not
    applied on their behalf.
    """
    with _Project(tmp_path):
        res = A.create("My Audit")
        assert not res.ok
        assert "my-audit" in res.reason
        assert not (tmp_path / ".agent2" / "workflows" / "my-audit.yaml").exists()
        assert A.suggest("My Audit") == "my-audit"
        assert A.suggest("!!!") == "", "nothing usable must come back as nothing"


def test_a_legal_name_is_taken_as_written_and_found_case_folded(tmp_path):
    """The filename is the name (Task 38), so `show DEMO` finds `demo.yaml`."""
    with _Project(tmp_path):
        assert A.create("demo.v2").ok
        assert L.load("DEMO.V2", force=True) is not None


# ── The bar: a refusal is a return value ──────────────────────────────────────

def test_a_denied_capability_refuses_by_returning_and_writes_nothing(tmp_path):
    """`AGENT2_DENY_CAPS=fs.write`, the CLI's own gate, asked live.

    ⚠️ Sabotage: raise instead of returning here and `/workflow new` prints a
    traceback where it printed a sentence. Both callers are printing surfaces, which
    is why `projectdoc.apply()` has the same shape.
    """
    with _Project(tmp_path):
        os.environ["AGENT2_DENY_CAPS"] = "fs.write"
        res = A.create("blocked")
        assert not res.ok
        assert "fs.write" in res.reason
        assert not (tmp_path / ".agent2" / "workflows").exists()
        # `locate()` hands a path to an editor, so it is gated the same way.
        assert not A.locate("blocked").ok


def test_a_denied_delete_leaves_the_workflow_on_disk(tmp_path):
    with _Project(tmp_path):
        made = A.create("keeper")
        assert made.ok
        os.environ["AGENT2_DENY_CAPS"] = "fs.delete"
        res = A.delete("keeper")
        assert not res.ok
        assert "fs.delete" in res.reason
        assert Path(made.path).is_file(), "the file went despite the refusal"


def test_describe_reports_the_posture_rather_than_asserting_it(tmp_path):
    """`describe()` is what the two surfaces print, so it must follow the denial."""
    with _Project(tmp_path):
        assert A.describe()["can_write"] is True
        os.environ["AGENT2_DENY_CAPS"] = "fs.write"
        d = A.describe()
        assert d["can_write"] is False
        assert d["write_cap"] == "fs.write"
        assert d["can_delete"] is True, "one denial may not imply the other"


# ── Overwrite, ownership, and the two facts a write reports ───────────────────

def test_create_never_overwrites_a_plan_somebody_wrote(tmp_path):
    """⚠️ The file is the only copy, and there is no undo.

    `new audit` on a project that already has `audit.yaml` is far likelier a human
    who forgot than one who meant to discard their plan, so `existed` comes back and
    the caller sends them to `edit`.
    """
    with _Project(tmp_path):
        first = A.create("audit", description="the original")
        assert first.ok
        Path(first.path).write_text(
            "name: audit\ndescription: hand written\nnodes:\n  - id: only\n"
            "    instruction: do the one thing\n", encoding="utf-8")

        second = A.create("audit", description="clobber")
        assert not second.ok
        assert second.existed is True
        body = Path(first.path).read_text(encoding="utf-8")
        assert "hand written" in body and "clobber" not in body


def test_ok_and_runnable_are_two_facts(tmp_path):
    """A body can be written perfectly and still not be a graph.

    ⚠️ Collapsing them lets a surface report "created ✓" for a file the runner
    refuses. `ok` says the action happened; `runnable` says the bytes now on disk form
    a graph `instantiate()` would accept, and `problems` is the difference.
    """
    with _Project(tmp_path):
        # A ring of two: `tasks.ready()` deadlocks on it in silence, which is exactly
        # what `graph.validate()` exists to refuse.
        cyclic = ("name: ring\nnodes:\n"
                  "  - id: a\n    needs: b\n    instruction: first\n"
                  "  - id: b\n    needs: a\n    instruction: second\n")
        res = A.create("ring", body=cyclic)
        assert res.ok, res.reason              # the write happened
        assert res.runnable is False           # and the plan will not run
        assert res.problems, "a file that cannot run must say why"
        assert Path(res.path).is_file()
        pay = res.to_payload()
        assert pay["ok"] is True and pay["runnable"] is False

        run = R.instantiate(L.load("ring", force=True).defn, chat_id="c-ring")
        assert not run.ok, "the runner accepted a cyclic graph"


def test_a_freshly_written_workflow_is_visible_to_the_very_next_read(tmp_path):
    """⚠️ The discovery TTL is seconds and a human edits faster than that.

    `create()`/`delete()` drop the cache themselves — a user who just wrote a file
    and is told the project has none has no way to tell that from a broken walk.
    """
    with _Project(tmp_path):
        WF.discover()                          # warm the cache on an empty folder
        res = A.create("fresh")
        assert res.ok
        cat = WF.discover()                    # NOT force=True — that is the point
        assert [f.name for f in cat.files] == ["fresh"]

        assert A.delete("fresh").ok
        assert WF.discover().files == []


def test_locate_writes_nothing_and_hands_back_the_readers_view(tmp_path):
    """`[E]dit` is a path plus the parse the reader already did."""
    with _Project(tmp_path):
        made = A.create("editme")
        before = Path(made.path).read_text(encoding="utf-8")
        found = A.locate("EDITME")
        assert found.ok
        assert Path(found.path) == Path(made.path)
        assert found.wf is not None and found.wf.name == "editme"
        assert Path(made.path).read_text(encoding="utf-8") == before


def test_delete_refuses_a_name_nothing_matches_and_a_directory(tmp_path):
    """Three refusals rather than three ways to remove the wrong thing."""
    with _Project(tmp_path):
        assert not A.delete("ghost").ok
        (tmp_path / ".agent2" / "workflows" / "adir.yaml").mkdir(parents=True)
        L.invalidate()
        res = A.delete("adir")
        assert not res.ok
        assert (tmp_path / ".agent2" / "workflows" / "adir.yaml").is_dir()


# ── Structural: one writer, and it is here ────────────────────────────────────

def test_the_authoring_module_writes_only_through_one_helper():
    """The mirror of `test_workflowfile.py::test_the_loader_has_no_writer`.

    Task 39 owns creating a workflow file, so the write cannot be forbidden here —
    but it can be confined. One `open()` (the temp file `_write()` replaces from) and
    one `os.replace`, so "did this module touch the user's file" is answerable by
    reading one function.
    """
    src = _AUTHORING.read_text(encoding="utf-8")
    body = ast.get_docstring(ast.parse(src)) or ""
    code = "\n".join(ln for ln in src.replace(body, "").splitlines()
                     if not ln.lstrip().startswith("#"))
    assert code.count("open(") == 1, "a second writer appeared in authoring.py"
    assert code.count("os.replace") == 1
    assert "shutil" not in code
    # Temp-then-replace, so a crash mid-write cannot leave a half-parsed plan.
    assert ".tmp" in code


def test_the_writer_is_atomic_and_utf8_with_unix_newlines(tmp_path):
    """A workflow file is read on the turn path by a parser that counts columns.

    CRLF would put `\\r` inside a block scalar's text on Windows and nowhere else,
    which is a plan that behaves differently on one OS — so the newline is pinned.
    """
    with _Project(tmp_path):
        res = A.create("nl", description="ünïcode ✓")
        raw = Path(res.path).read_bytes()
        assert b"\r\n" not in raw
        assert "ünïcode ✓" in raw.decode("utf-8")
        assert not list((tmp_path / ".agent2" / "workflows").glob("*.tmp"))


# ── The CLI surface ───────────────────────────────────────────────────────────

def _cli():
    """`agent2cli`, imported lazily so a collection error is this test's alone."""
    import agent2cli
    return agent2cli


def test_every_cli_verb_survives_a_project_with_nothing_in_it(tmp_path, capsys):
    """Totality across the whole grammar — the CLI may not raise at a user.

    ⚠️ Includes the two verbs that take no argument and the three refusal paths, so
    an exception anywhere in the parser fails here rather than in a session.
    """
    C = _cli()
    with _Project(tmp_path):
        for cmd in ("/workflow list", "/workflow reload", "/workflow state",
                    "/workflow show", "/workflow run", "/workflow delete",
                    "/workflow bogus", "/workflow show nothing-here",
                    "/workflow run nothing-here", "/workflow"):
            if cmd == "/workflow":
                continue        # the bare form opens a menu; covered below
            C.cmd_workflow(cmd, "2.5-flash", "pro")
        out = capsys.readouterr().out
        assert "bogus" in out, "an unknown verb must be named back"
        for verb in C._WORKFLOW_ACTIONS:
            assert verb in out, f"the unknown-verb message omits {verb!r}"


def test_the_cli_parser_holds_no_verb_the_tuple_does_not(tmp_path, capsys):
    """`/mcp`'s rule: the grammar is one tuple, so a verb added there works at once.

    A literal in the dispatcher is a verb that silently never matches — or, worse,
    one the help row advertises and the parser rejects.
    """
    C = _cli()
    assert C._WORKFLOW_ACTIONS == ("list", "new", "edit", "run", "auto", "delete",
                                   "show", "state", "reload")
    menu = {v for v, _lbl, _hint in C._WORKFLOW_MENU}
    assert menu <= set(C._WORKFLOW_ACTIONS), "the menu offers a verb the parser lacks"
    with _Project(tmp_path):
        C.cmd_workflow("/workflow LIST", "2.5-flash", "pro")     # verb is folded
        assert "workflow" in capsys.readouterr().out.lower()


def test_the_cli_writes_runs_and_deletes_through_the_owning_modules(tmp_path, capsys):
    """`/workflow new → run → delete`, the whole Task 39 loop, on real rows."""
    C = _cli()
    with _Project(tmp_path):
        C.cmd_workflow("/workflow new demo", "2.5-flash", "pro")
        assert (tmp_path / ".agent2" / "workflows" / "demo.yaml").is_file()
        capsys.readouterr()

        C.cmd_workflow("/workflow run demo", "2.5-flash", "pro")
        out = capsys.readouterr().out
        assert "demo" in out
        live = R.live()
        assert live is not None and live.name == "demo"

        # ⚠️ A second run while one is live is refused, not queued: `for_turn()` has
        # to have one answer to "which node am I doing".
        C.cmd_workflow("/workflow run demo", "2.5-flash", "pro")
        assert "already running" in capsys.readouterr().out

        C._workflow_delete("demo", confirm=False)
        assert not (tmp_path / ".agent2" / "workflows" / "demo.yaml").exists()


def test_delete_opens_on_keep_so_enter_and_escape_both_keep_the_file(tmp_path, monkeypatch):
    """⚠️ The one irreversible verb, and its default is *keep*.

    `diffview`'s two-press `r` applied here: the confirmation picker opens on `keep`,
    so Enter (accept the highlighted option) and Esc (cancel → `None`) both leave the
    file alone, and only the literal `delete` value proceeds.
    """
    C = _cli()
    seen: dict = {}

    def _fake_picker(title, options, current_value=None, allow_empty=False):
        seen["title"] = title
        seen["values"] = [o.get("value") for o in options]
        seen["current"] = current_value
        return None                              # Esc

    # ⚠️ `agent2cli` imports the picker by name (`from …palette import
    # ephemeral_picker`), so THIS is the binding the call site resolves. Patching
    # `palette.ephemeral_picker` instead leaves the real full-screen renderer in
    # place and the test opens a prompt_toolkit app inside pytest.
    monkeypatch.setattr(C, "ephemeral_picker", _fake_picker)
    with _Project(tmp_path):
        made = A.create("precious")
        C._workflow_delete("precious", confirm=True)
        assert seen["current"] == "keep", "the picker did not open on keep"
        assert "delete" in seen["values"] and "keep" in seen["values"]
        assert Path(made.path).is_file(), "Esc deleted the file"

        # Enter on the highlighted option is the same answer.
        monkeypatch.setattr(C, "ephemeral_picker", lambda *a, **k: "keep")
        C._workflow_delete("precious", confirm=True)
        assert Path(made.path).is_file(), "Enter on `keep` deleted the file"

        monkeypatch.setattr(C, "ephemeral_picker", lambda *a, **k: "delete")
        C._workflow_delete("precious", confirm=True)
        assert not Path(made.path).exists(), "an explicit delete did nothing"


def test_the_cli_help_row_names_the_command(tmp_path):
    """A command absent from `/help` is a command nobody finds (rule 28)."""
    C = _cli()
    rows = [r for r in C.SLASH_COMMANDS if str(r[0]).startswith("/workflow")]
    assert rows, "/workflow is missing from SLASH_COMMANDS"
    blob = " ".join(str(x) for r in rows for x in r).lower()
    for verb in ("new", "edit", "run", "delete"):
        assert verb in blob, f"the help row omits [{verb[0].upper()}]{verb[1:]}"


# ── The web surface ───────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path):
    """A real app over a real temp project, workspace restored after."""
    db.init_db()
    prev = W.root()
    W.set_workspace(str(tmp_path))
    L.invalidate()
    app = Flask(__name__)
    app.secret_key = "wf"
    register_routes(app)
    try:
        with app.test_client() as c:
            yield c, tmp_path
    finally:
        W.set_workspace(prev or os.getcwd())
        L.invalidate()


def test_the_browser_gets_the_same_four_verbs_out_of_the_same_modules(client):
    """new · show · run · delete over HTTP, with no second declaration anywhere.

    ⚠️ Every verdict in these payloads comes from the module that owns it —
    `Catalog.to_payload()`, `WorkflowFile.to_payload()`, `RunState.to_payload()`,
    `Authored.to_payload()`. A browser-side "is it runnable" is the drift this pins.
    """
    c, root = client
    empty = c.get("/api/workflows").get_json()
    assert empty["catalog"]["workflows"] == []
    assert empty["live"] == {}
    assert empty["policy"]["authoring"]["can_write"] is True

    made = c.post("/api/workflows", json={"name": "webflow",
                                          "description": "from the browser"})
    assert made.status_code == 200
    body = made.get_json()
    assert body["ok"] is True and body["runnable"] is True
    assert (root / ".agent2" / "workflows" / "webflow.yaml").is_file()

    one = c.get("/api/workflows/webflow").get_json()
    assert one["name"] == "webflow"
    assert [n["id"] for n in one["definition"]["nodes"]] == ["plan", "build", "verify"]

    started = c.post("/api/workflows/webflow/run", json={"chat_id": "c-web"})
    assert started.status_code == 200
    run = started.get_json()
    assert run["ok"] is True
    assert run["run"]["total"] == 3 and run["run"]["done"] == 0

    again = c.post("/api/workflows/webflow/run", json={"chat_id": "c-web"})
    assert again.status_code == 409, "two live runs make the current node ambiguous"
    assert "already running" in (again.get_json().get("reason") or "")

    gone = c.delete("/api/workflows/webflow")
    assert gone.status_code == 200 and gone.get_json()["ok"] is True
    assert not (root / ".agent2" / "workflows" / "webflow.yaml").exists()


def test_the_web_half_reports_a_refusal_inside_a_200_not_as_a_crash(client):
    """⚠️ A denied capability is an answer, not a 500.

    `authoring` asks `fs.write` live *inside* the route, so the panel reads
    `ok: false` plus a reason — the shape `POST /api/project/init` already uses.
    """
    c, root = client
    os.environ["AGENT2_DENY_CAPS"] = "fs.write"
    try:
        res = c.post("/api/workflows", json={"name": "denied"})
        assert res.status_code == 200
        body = res.get_json()
        assert body["ok"] is False and "fs.write" in body["reason"]
        assert not (root / ".agent2" / "workflows").exists()
    finally:
        os.environ.pop("AGENT2_DENY_CAPS", None)


def test_a_missing_workflow_names_how_many_are_known(client):
    """"Not found" and "you have none" send a caller to two different places."""
    c, _root = client
    res = c.get("/api/workflows/nope")
    assert res.status_code == 404
    assert res.get_json()["known"] == []

    c.post("/api/workflows", json={"name": "here"})
    assert c.get("/api/workflows/nope").get_json()["known"] == ["here"]
    assert c.post("/api/workflows/nope/run", json={}).status_code == 404


def test_the_web_create_refuses_an_escaping_name_and_a_missing_one(client):
    c, root = client
    assert c.post("/api/workflows", json={}).status_code == 400
    bad = c.post("/api/workflows", json={"name": "../../evil"}).get_json()
    assert bad["ok"] is False
    assert not (root.parent / "evil.yaml").exists()
    assert not (root / ".agent2" / "workflows").exists()


def test_a_body_the_runner_refuses_is_saved_and_reported_as_not_runnable(client):
    """The web half of `test_ok_and_runnable_are_two_facts`.

    Task 40's planner posts a `body` rather than typing one, so the same two facts
    have to survive the round trip that machine will use.
    """
    c, root = client
    res = c.post("/api/workflows", json={
        "name": "ring", "body": ("name: ring\nnodes:\n"
                                 "  - id: a\n    needs: b\n    instruction: one\n"
                                 "  - id: b\n    needs: a\n    instruction: two\n")})
    body = res.get_json()
    assert body["ok"] is True and body["runnable"] is False
    assert body["problems"]
    assert (root / ".agent2" / "workflows" / "ring.yaml").is_file()
    assert c.post("/api/workflows/ring/run", json={}).status_code == 400


def test_the_workflow_routes_are_rated_and_never_default_to_destructive():
    """The route table already carried the rating; this asserts it still applies.

    ⚠️ `capability_for` falls to `destructive` for an unmatched mutating `/api/`
    path — safe, but it would make `/workflow run` an owner-only action in the
    browser while the terminal ran it under `chat`.
    """
    assert perms.capability_for("GET", "/api/workflows") == perms.CAP_READ
    for path in ("/api/workflows", "/api/workflows/x", "/api/workflows/x/run"):
        for method in ("POST", "DELETE"):
            assert perms.capability_for(method, path) == perms.CAP_CHAT, path


def test_the_master_switch_stops_a_run_without_hiding_the_files(client, monkeypatch):
    """`AGENT2_WORKFLOWS=0` — files are still read, no run may start.

    ⚠️ The asymmetry is deliberate and it is `AGENT2_SKILLS=0`'s: a switch that also
    emptied the list would make "you have no workflows" and "workflows are off" the
    same sentence, and only one of them names the fix.
    """
    c, _root = client
    assert c.post("/api/workflows", json={"name": "offflow"}).get_json()["ok"]
    monkeypatch.setattr(config, "WORKFLOW_ENABLED", False)

    listed = c.get("/api/workflows").get_json()
    assert [f["name"] for f in listed["catalog"]["workflows"]] == ["offflow"]
    assert listed["policy"]["enabled"] is False

    res = c.post("/api/workflows/offflow/run", json={})
    assert res.status_code == 400
    assert res.get_json()["ok"] is False
    assert WF.for_turn(chat_id="")["active"] is False


def test_nothing_in_the_authoring_path_raises_on_a_hostile_project(tmp_path):
    """Totality, swept: a file where the folder goes, and a folder where a file goes.

    Neither is a state a user creates on purpose, and both are states a shared
    checkout produces — so each has to be a refusal with a reason.
    """
    with _Project(tmp_path):
        wf_dir = tmp_path / ".agent2" / "workflows"
        (tmp_path / ".agent2").mkdir(parents=True, exist_ok=True)
        wf_dir.write_text("not a directory", encoding="utf-8")   # folder is a file
        res = A.create("blocked")
        assert not res.ok and res.reason
        assert WF.discover(force=True).files == []
        assert not A.delete("blocked").ok
        assert not A.locate("blocked").ok
        assert isinstance(WF.describe()["authoring"], dict)


def test_graph_names_are_folded_once_and_only_by_the_loader(tmp_path):
    """⚠️ `fold_id` is the ONE declaration of what a workflow may be called.

    So `new Mixed` writes `mixed.yaml` — the filename IS the name (Task 38) and it is
    case-folded, which is what makes one file one workflow on a case-insensitive
    filesystem. What the fold does **not** do is remove a separator or a space, and
    that is the whole reason `NAME_RE` is asked afterwards: a writer that trusted the
    fold to sanitise would resolve `../../evil` outside the project.

    The CLI lowercases the *verb* and passes the **name** through raw, so a second,
    earlier fold in the dispatcher would be a competing rule — and the two would
    disagree the first time either learned a character the other did not.
    """
    with _Project(tmp_path):
        made = A.create("Mixed")
        assert made.ok and made.name == "mixed", "the fold is not applied"
        assert (tmp_path / ".agent2" / "workflows" / "mixed.yaml").is_file()
        assert L.load("MIXED", force=True) is not None

        # What folding cannot make safe is refused, not folded further.
        assert G.fold_id("  My   Audit ") == "my audit"
        assert not A.create("My Audit").ok
        assert not A.create("../../evil").ok


# ══════════════════════════════════════════════════════════════════════════════════════
# Phase D3 — `/workflow` ON THE DAG SCHEDULER, AND LISTING STILL EXECUTES NOTHING
# ══════════════════════════════════════════════════════════════════════════════════════
#
# D3.22 — `/workflow` is integrated with the DAG scheduler, and **listing executes
# nothing**.  D3.23 — **explicit execution only**: `/workflow run <name>` is the one verb
# that starts anything, and the run path is
#     Validate → Build the DAG → display the plan → Execute → Verify → Complete.
#
# Everything below is driven through `cmd_workflow` — the real dispatcher, over real
# files on disk — because the claim is about the *grammar*, and a claim about a grammar
# cannot be pinned by reading the source for a call. Two spies and one row-count pair are
# what make each assertion falsifiable, and **every one of them is paired with a positive
# control in the same test**: "the counter did not move" and "the counter was never wired
# up" are otherwise the same observation, which is how a tautology gets in.
#
# ⚠️ The five extra imports sit here, beside their tests, so this section is a pure
# append to the file. `.github` is outside ruff's tree (see `pyproject.toml`), so the
# placement is not a lint question.

from agent2.cli import render as RND       # noqa: E402
from agent2.core import context as CTX     # noqa: E402
from agent2.core import tasks as T         # noqa: E402
from agent2.core import verify as V        # noqa: E402
from agent2.core.dag import model as DM    # noqa: E402

#: The glyph set proven to render on every terminal this project supports. A mark table
#: that reached outside it would draw a box on a plain Windows console.
_SAFE_GLYPHS = frozenset("· – — • … ℹ ← ↑ → ↓ ─ │ ┌ └ ═ ▶ ▸ ○ ● ★ ⚠ ⚡ ✓ ✗".split())


def _flat(text: str) -> str:
    """Captured output with every run of whitespace collapsed.

    Rich word-wraps to 80 columns under pytest capture, so a phrase that fits on one
    line in a terminal can arrive here split across two. Flattening is what lets a
    multi-word assertion be about the words rather than about the width.
    """
    return " ".join((text or "").split())


def _rowcounts(root: Path) -> tuple[int, int]:
    """`(exec_workflows, agent_tasks)` — the two tables a run writes and a read may not.

    ⚠️ Scoped to *root*, never a bare `COUNT(*)`. `conftest.py` points `AGENT2_DB` at one
    fixed path for every pytest process, so a global count is a number another process can
    move between the two reads — a delta assertion that would then fail for a reason this
    file has nothing to do with.

    ⚠️ The node rows are scoped through this project's **run** rows and not through
    `task_sessions.cwd`, because neither surface passes `cwd=` to `instantiate()`, so that
    column is `''` for every workflow session and would scope the count to nothing.
    Faithful all the same: `dag.store.create()` is the one writer of both, and it writes
    the run row and the node rows in the same call, so there is no break that could leave
    node rows behind without a run row to find them by.
    """
    key = CTX.project_key(str(root))
    runs = db.qone("SELECT COUNT(*) AS c FROM exec_workflows WHERE project=?", (key,))["c"]
    nodes = db.qone(
        "SELECT COUNT(*) AS c FROM agent_tasks WHERE session_id IN "
        "(SELECT session_id FROM exec_workflows WHERE project=? AND session_id<>'')",
        (key,))["c"]
    return (int(runs), int(nodes))


def _quiet_cli(C, monkeypatch) -> None:
    """Close the two doors that are neither a read nor a run.

    `delete` and the bare menu open `ephemeral_picker` (a prompt_toolkit app) and `edit`
    launches `$VISUAL`/`$EDITOR`. Both are real terminal work and neither is what these
    tests are about — a picker answering `None` is Esc, which is the *keep* answer.
    """
    monkeypatch.setattr(C, "ephemeral_picker", lambda *_a, **_k: None)
    monkeypatch.setattr(C.diffview, "open_in_editor", lambda *_a, **_k: False)


def test_run_is_the_only_verb_that_reaches_instantiate(tmp_path, monkeypatch, capsys):
    """D3.23 — *explicit execution only*, in the one form that can go red.

    A substring search for `instantiate` in `agent2cli.py` would prove nothing: the call
    exists, and the whole question is which of the nine verbs can reach it. So every verb
    in `_WORKFLOW_ACTIONS` is driven for real against a real workflow file, plus the bare
    no-argument form, and the call site itself is watched.

    ⚠️ THE SPY GOES ON `agent2.core.workflow`, NEVER ON `runner`. `workflow/__init__.py`
    does `from .runner import instantiate`, so the package holds its **own** binding and
    `_workflow_run` resolves `_wf.instantiate`; a spy on `runner.instantiate` would watch
    a name the CLI never reads and this test would then pass against anything at all.

    ⚠️ `auto` (Phase D4) IS DRIVEN HERE TOO AND MUST STILL COME BACK EMPTY — it plans,
    the picker `_quiet_cli` installs answers Esc, and Esc is the *keep* answer, so no run
    is instantiated. It reaches the writer through `runner.instantiate` rather than this
    binding, which is why `test_auto_writes_nothing_until_a_human_says_start` spies on
    the runner instead; both are the same function, and neither test is the other's proof.

    Turns red when: `cmd_workflow` routes any other action to `_workflow_run`, when a
    read-only verb gains a run step, or when a tenth verb is added without being weighed.
    """
    C = _cli()
    _quiet_cli(C, monkeypatch)
    started: list[str] = []

    def _spy(defn, **kw):
        started.append(str(getattr(defn, "name", "?")))
        return R.instantiate(defn, **kw)

    monkeypatch.setattr(WF, "instantiate", _spy)

    with _Project(tmp_path):
        assert A.create("solo").ok
        # The grammar this test is measured against — re-asserted, not re-typed elsewhere.
        assert C._WORKFLOW_ACTIONS == ("list", "new", "edit", "run", "auto", "delete",
                                       "show", "state", "reload")
        for action in C._WORKFLOW_ACTIONS:
            if action == "run":
                continue
            C.cmd_workflow(f"/workflow {action} solo", "2.5-flash", "pro")
        C.cmd_workflow("/workflow", "2.5-flash", "pro")          # bare — the menu, then Esc
        C.cmd_workflow("/workflow   ", "2.5-flash", "pro")       # and the whitespace form
        capsys.readouterr()
        assert started == [], f"a read-only verb started a run: {started}"

        # The positive control. Without it the assertion above would pass against a spy
        # that was never installed on the binding the CLI actually calls.
        C.cmd_workflow("/workflow run solo", "2.5-flash", "pro")
        capsys.readouterr()
        assert started == ["solo"], "the one verb that must start a run did not reach it"


def test_a_read_only_verb_writes_no_run_row_and_no_task_row(tmp_path, monkeypatch, capsys):
    """D3.22 — the scheduler is wired in, and listing still executes nothing.

    Counted at the two tables a run actually writes, because rows are the only place
    "nothing was started" is durable: a spy proves no *call* was made, and this proves no
    *state* was left behind by any other route.

    ⚠️ The negative half alone would be a tautology — a broken counter also reads
    unchanged. So the same counter is required to MOVE for `run`, by the exact node count
    of the seeded template, before the zero above is believed.
    """
    C = _cli()
    _quiet_cli(C, monkeypatch)
    with _Project(tmp_path):
        assert A.create("quiet").ok
        nodes = len(WF.load("quiet").defn.nodes)
        before = _rowcounts(tmp_path)
        for raw in ("/workflow", "/workflow list", "/workflow reload", "/workflow state",
                    "/workflow show quiet", "/workflow new quiet", "/workflow edit quiet",
                    "/workflow delete quiet", "/workflow nonsense quiet"):
            C.cmd_workflow(raw, "2.5-flash", "pro")
        capsys.readouterr()
        assert _rowcounts(tmp_path) == before, "a read-only verb wrote execution state"

        C.cmd_workflow("/workflow run quiet", "2.5-flash", "pro")
        capsys.readouterr()
        runs_after, tasks_after = _rowcounts(tmp_path)
        assert runs_after == before[0] + 1, "the one run row was not written"
        assert tasks_after == before[1] + nodes, "the node task rows were not written"


def test_the_run_path_shows_the_plan_before_it_hands_the_turn_over(tmp_path, monkeypatch,
                                                                  capsys):
    """Validate → build the DAG → **display the plan** → and then stop.

    The spec's order, on screen and in order: the started line, the run summary row, the
    wave rows, and a closing line naming the node the next turn works on. The last of
    those is the seam — Agent2 does one node per turn, so `run` queues work and hands the
    turn back rather than driving the model itself.
    """
    C = _cli()
    _quiet_cli(C, monkeypatch)
    with _Project(tmp_path):
        assert A.create("shown").ok
        total = len(WF.load("shown").defn.nodes)
        C.cmd_workflow("/workflow run shown", "2.5-flash", "pro")
        out = _flat(capsys.readouterr().out)

        marks = ["Started", "settled", "Plan ·", "Wave 1", "Next",
                 "Your next message works on"]
        at = {mark: out.find(mark) for mark in marks}
        assert all(i >= 0 for i in at.values()), f"the run path did not print a step: {at}"
        assert list(at.values()) == sorted(at.values()), f"steps printed out of order: {at}"

        # Nothing was executed: it says 0 settled, and the rows agree.
        assert f"0/{total} settled" in out
        assert "one node per turn" in out
        live = R.live(chat_id=(C.S.chat or {}).get("id", "") or "")
        assert live is not None and live.total == total
        assert live.done == 0 and not live.running, "starting a run ran a node"


def test_workflow_state_on_a_project_that_never_ran_one_starts_nothing(tmp_path, monkeypatch,
                                                                      capsys):
    """`/workflow state` with no history is guidance, not a run.

    The named verb is the one that starts anything, so the empty screen has to say which
    verb that is — a bare "nothing is running" leaves the reader looking for a button.
    """
    C = _cli()
    _quiet_cli(C, monkeypatch)
    with _Project(tmp_path):
        before = _rowcounts(tmp_path)
        C.cmd_workflow("/workflow state", "2.5-flash", "pro")
        out = _flat(capsys.readouterr().out)
        assert "No workflow has run in this project yet" in out
        assert "/workflow run <name>" in out, "the empty screen does not name the verb"
        assert _rowcounts(tmp_path) == before, "reading the state wrote execution state"
        assert R.live(chat_id="") is None


def test_workflow_state_on_a_settled_run_reaches_the_verification_block(tmp_path, monkeypatch,
                                                                       capsys):
    """Once a run has settled, `/workflow state` shows it **and** what the ledgers say.

    ⚠️ The run row has to be settled for this branch to be reached at all: `runner.live()`
    filters on the unsettled `exec_workflows` row, and `for_turn()` never consults
    `finished`, so a run whose nodes are all complete but whose row was never settled
    still reads as live and never gets here.
    """
    C = _cli()
    _quiet_cli(C, monkeypatch)
    with _Project(tmp_path):
        assert A.create("ledger").ok
        wf = WF.load("ledger")
        run = R.instantiate(wf.defn, chat_id="c-settled", model="2.5-flash", mode="pro")
        assert run.ok
        for task_id in run.node_tasks.values():
            T.complete(task_id, "ok")
        assert R.settle(run.run_id).finished
        assert R.live(chat_id="c-settled") is None, "the run row was not settled"

        monkeypatch.setattr(C.S, "chat", {"id": "c-settled"})
        C.cmd_workflow("/workflow state", "2.5-flash", "pro")
        out = _flat(capsys.readouterr().out)

    total = len(run.node_tasks)
    assert "No workflow is running" in out
    assert f"{total}/{total} settled" in out
    assert "What the ledgers say about" in out, "render_verification was not reached"
    assert "Verification ·" in out
    said = [word for word in V.VERDICTS if word in out]
    assert said, f"no verdict from core.verify reached the screen — expected one of {V.VERDICTS}"


def test_workflow_state_frees_what_a_dead_process_parked_and_never_a_human_hold(
        tmp_path, monkeypatch, capsys):
    """Two nodes read `paused` on screen and exactly one of them is released.

    ⚠️ THE ONLY DURABLE DIFFERENCE IS THE `CP_STOPPED` CHECKPOINT: `tasks.interrupt()`
    writes one and then parks the row, `tasks.pause()` writes none. `/pause` is a person's
    decision and recovering *execution* may never undo it, so the discriminator has to be
    the checkpoint and not the status.

    Turns red when: `release_interrupted()` widens to every PAUSED row (the human hold is
    freed, and the "Released …" line names it), or narrows to none (the crashed node stays
    parked and the line disappears entirely).
    """
    C = _cli()
    _quiet_cli(C, monkeypatch)
    with _Project(tmp_path):
        assert A.create("halted").ok
        wf = WF.load("halted")
        run = R.instantiate(wf.defn, chat_id="c-crash", model="2.5-flash", mode="pro")
        assert run.ok and len(run.node_tasks) >= 2
        crashed, held = list(run.node_tasks)[0], list(run.node_tasks)[1]

        # ⚠️ `interrupt()` only touches RUNNING rows, so the node has to be started first
        # — a PENDING row would be skipped and this would set up nothing at all.
        assert T.start(run.node_tasks[crashed]) is not None
        assert T.interrupt(run.session_id, "killed"), "no running task was interrupted"
        T.pause(run.node_tasks[held])                       # a human hold: no checkpoint

        parked = R.state_for(run.run_id)
        assert {n.node for n in parked.interrupted} == {crashed}, "the setup is not two holds"
        assert parked.node(held).state == DM.PAUSED

        monkeypatch.setattr(C.S, "chat", {"id": "c-crash"})
        C.cmd_workflow("/workflow state", "2.5-flash", "pro")
        out = _flat(capsys.readouterr().out)

        after = R.state_for(run.run_id)
        assert after.node(crashed).state == DM.READY, "the crashed node is still parked"
        assert after.node(held).state == DM.PAUSED, "recovery undid a hold a person chose"

    assert "Released 1 node(s) a dead process had parked" in out
    line = out[out.find("Released"):]
    line = line[:line.find("runnable again") + len("runnable again")]
    assert crashed in line, f"the freed node was not named: {line!r}"
    assert held not in line, f"a human hold was claimed as released: {line!r}"


def test_workflow_state_spends_no_recovery_pass_on_a_healthy_run(tmp_path, monkeypatch,
                                                                capsys):
    """The ordinary read costs nothing extra — the release is gated on `interrupted`.

    `recover()` is a `load()` plus a write path, and `/workflow state` is the screen a
    human refreshes; calling it unconditionally would spend that on every read.

    ⚠️ THE SPY GOES ON `agent2.core.workflow` (`_wf.recover`), not on `runner.recover` or
    `dag.store.release_interrupted` — the package re-binds the name at import, so those
    two are not the object the CLI resolves.

    Turns red when: the `if payload.get("interrupted"):` guard is dropped — the spy then
    fires on a run where nothing was ever parked.
    """
    C = _cli()
    _quiet_cli(C, monkeypatch)
    calls: list[str] = []

    def _spy(run_id):
        calls.append(str(run_id))
        return []

    monkeypatch.setattr(WF, "recover", _spy)

    with _Project(tmp_path):
        assert A.create("healthy").ok
        run = R.instantiate(WF.load("healthy").defn, chat_id="c-ok",
                            model="2.5-flash", mode="pro")
        assert run.ok
        monkeypatch.setattr(C.S, "chat", {"id": "c-ok"})

        C.cmd_workflow("/workflow state", "2.5-flash", "pro")
        out = _flat(capsys.readouterr().out)
        assert "Live workflow:" in out and "Plan ·" in out, "the live branch was not taken"
        assert calls == [], "a healthy run paid for a recovery pass"

        # The positive control: park a node and the very same spy has to fire. Without
        # this, the assertion above would also pass against a spy on the wrong binding.
        assert T.start(run.node_tasks[next(iter(run.node_tasks))]) is not None
        assert T.interrupt(run.session_id, "killed")
        C.cmd_workflow("/workflow state", "2.5-flash", "pro")
        capsys.readouterr()
        assert calls == [run.run_id], "the parked node never reached recover()"


def test_the_node_mark_table_is_total_over_both_vocabularies():
    """⚠️ THE VOCABULARIES ARE IMPORTED, NEVER RETYPED.

    `_WF_MARKS` has to be total over *both* word sets a payload can carry: the five
    `PHASE_*` words `runner.py` still ships for backward compatibility, and the nine
    `dag.model.STATES` carried beside them. A hard-coded list of ten strings here would
    keep passing on the day `dag.model` grew a tenth state — which is precisely the day
    the table stops being total and a real node prints `?` at a user.

    Turns red when: a state or phase word is added, renamed or removed on either side
    without the renderer learning it, or when a row is left behind pointing at a word no
    module produces any more.
    """
    phases = {getattr(R, name) for name in dir(R) if name.startswith("PHASE_")}
    states = set(DM.STATES)
    assert len(phases) == 5, f"the phase vocabulary changed: {sorted(phases)}"
    assert len(states) == 9, f"the state vocabulary changed: {sorted(states)}"
    vocab = phases | states

    assert set(RND._WF_MARKS) == vocab, (
        "the mark table and the two vocabularies disagree — unmarked: "
        f"{sorted(vocab - set(RND._WF_MARKS))}; dead rows: "
        f"{sorted(set(RND._WF_MARKS) - vocab)}")

    for word in sorted(vocab):
        mark, colour, style = RND._wf_mark(word)
        assert mark and mark.strip(), f"{word!r} has a blank mark"
        assert isinstance(colour, str) and isinstance(style, str) and style, word
        assert set(mark) <= _SAFE_GLYPHS, f"{word!r} draws an unproven glyph: {mark!r}"
        # `state` outranks `phase`, and either alone is enough — three shipped readers
        # key on `phase`, so a payload carrying only that one may not fall through.
        assert RND._wf_word({"state": word}) == word
        assert RND._wf_word({"phase": word}) == word
        assert RND._wf_word({"state": word, "phase": "pending"}) == word

    # The verification table is the same rule over `verify.VERDICTS`.
    assert set(RND._VERIFY_MARKS) == set(V.VERDICTS), (
        f"unmarked verdicts: {sorted(set(V.VERDICTS) - set(RND._VERIFY_MARKS))}")
    for verdict in V.VERDICTS:
        mark = RND._VERIFY_MARKS[verdict][0]
        assert set(mark) <= _SAFE_GLYPHS, f"{verdict!r} draws an unproven glyph: {mark!r}"

    # An unknown word degrades to a dim placeholder — never a tick, and never blank.
    for junk in ("", None, "wat", "COMPLETED", 0):
        mark, _colour, style = RND._wf_mark(junk)
        assert (mark, style) == ("?", "dim"), junk
    assert RND._wf_word({}) == ""
    assert RND._wf_word({"state": "", "phase": ""}) == ""


def test_the_plan_and_verification_renderers_are_total_and_claim_nothing(capsys):
    """Presentation may never raise into a turn, and absent data may never read as good.

    `render_*` returning **False** is how "there was nothing to draw" is said. The
    dangerous alternative is a header with no rows under it: a `Plan ·` heading over an
    empty body reads as a plan with nothing left to do, and a `Verification` heading with
    no findings reads as a clean bill of health.
    """
    for junk in ({}, None, {"waves": [], "next": [], "held": []},
                 {"waves": None, "next": None, "held": None},
                 {"total": 3, "width": 2}):
        assert RND.render_dag_plan(junk) is False, junk
    assert _flat(capsys.readouterr().out) == "", "an empty plan drew a heading"

    # A wave with nothing runnable under it SAYS so, rather than leaving the row blank.
    assert RND.render_dag_plan({"waves": [["a", "b"]]}) is True
    out = _flat(capsys.readouterr().out)
    assert "Plan · 2 node(s) · 1 wave(s) · up to 2 at once" in out
    assert "nothing may start now" in out

    # Missing keys throughout: no total, no width, and a held list of junk.
    assert RND.render_dag_plan({"next": ["a"], "held": [{}, "not a dict", None]}) is True
    out = _flat(capsys.readouterr().out)
    assert "Plan · 0 node(s) · 0 wave(s)" in out
    assert "? — ?" in out, "a held node with no code printed nothing at all"

    for junk in ({}, None, {"findings": [], "problems": [], "warnings": []},
                 {"findings": None, "counts": None, "verified": True}):
        assert RND.render_verification(junk) is False, junk
    assert _flat(capsys.readouterr().out) == "", "an empty report drew a verdict"

    # A finding with no verdict at all is a placeholder — and NOT the word `verified`.
    assert RND.render_verification({"findings": [{}]}) is True
    out = _flat(capsys.readouterr().out)
    assert "Verification · still running" in out
    assert "verified" not in out, "an unverified report claimed a verdict"
    assert RND.render_verification({"problems": ["the command wrote nothing"]}) is True
    assert "the command wrote nothing" in _flat(capsys.readouterr().out)
