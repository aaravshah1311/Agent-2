# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the centralized ``agent2/core/`` package (sections 1-14).

Run from the repo root:  python -m pytest .github/tests/test_core.py -v

Coverage
  - workspace : root discovery order, the hard path sandbox (``..`` escape,
                absolute-outside, UNC/network, empty), relative-to-root
                resolution, switching + persistence + on_switch callbacks.
  - session   : per-(sid, chat_id) isolation, fresh cancel token per turn, the
                Stop-button (sid-wide) cancel, stream ownership, task tracking.
  - memory /
    rules /
    context   : the shared DB-backed CRUD + prompt-block renderers.
  - tools     : the ToolRegistry (unknown-tool message, dispatch) and that every
                filesystem tool refuses paths outside the active workspace with
                the EXACT required message.

conftest.py redirects AGENT2_DB to a throwaway temp DB, so these tests never
touch the developer's real agent2.db. The workspace singleton is reset and
re-pointed at a tmp_path per test via the ``ws`` fixture.
"""

import os
from pathlib import Path

import pytest

from agent2 import database as db
from agent2.core import workspace as ws_mod
from agent2.core.workspace import (
    WorkspaceManager, WorkspaceViolation, BLOCKED_MSG, Workspace,
)
from agent2.core.session import SessionManager
from agent2.core import memory as mem_mod
from agent2.core import rules as rules_mod
from agent2.core import context as ctx_mod
from agent2 import tools as tools_mod


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True, scope="session")
def _schema():
    """Create the schema once in the throwaway DB before any test runs."""
    db.init_db()


@pytest.fixture(autouse=True)
def _clean_tables():
    """Wipe the DB-backed tables between tests so CRUD assertions are isolated."""
    for table in ("memories", "rules", "chats", "messages", "settings"):
        try:
            db.exe(f"DELETE FROM {table}")
        except Exception:
            pass
    yield


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A fresh WorkspaceManager pinned to *tmp_path* as its sandbox root.

    We bypass discovery by injecting the Workspace directly so the sandbox tests
    are deterministic regardless of what markers happen to exist on disk.
    """
    mgr = WorkspaceManager()
    root = tmp_path.resolve()
    mgr._current = Workspace(id="test", root=root, allowed_dirs=[root],
                             metadata={"name": root.name})
    # Point the module singleton (and everything importing it) at our manager.
    monkeypatch.setattr(ws_mod, "manager", mgr)
    monkeypatch.setattr(tools_mod, "_ws", ws_mod)
    return mgr


# ── Workspace: discovery order (section 3) ────────────────────────────────────

def _confine_fs_to(root, monkeypatch):
    """Make Path.exists() report False for anything outside *root*.

    Discovery walks upward from the start dir looking for `.git` / marker files.
    On a real machine an ancestor of the OS temp dir (e.g. the home directory)
    may legitimately contain such markers, which would pollute the fallback
    tests. Confining the view to *root* makes discovery deterministic.
    """
    root = root.resolve()
    real_exists = Path.exists

    def scoped_exists(self):
        try:
            self.resolve().relative_to(root)
        except ValueError:
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", scoped_exists)


def test_discovery_prefers_git_root(tmp_path):
    mgr = WorkspaceManager()
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    nested = proj / "src" / "deep"
    nested.mkdir(parents=True)
    assert mgr._discover_root(nested) == proj.resolve()


def test_discovery_falls_back_to_marker_file(tmp_path, monkeypatch):
    _confine_fs_to(tmp_path, monkeypatch)
    mgr = WorkspaceManager()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[tool]\n")
    nested = proj / "a" / "b"
    nested.mkdir(parents=True)
    assert mgr._discover_root(nested) == proj.resolve()


def test_discovery_fallback_is_start_dir(tmp_path, monkeypatch):
    _confine_fs_to(tmp_path, monkeypatch)
    mgr = WorkspaceManager()
    lonely = tmp_path / "no_markers"
    lonely.mkdir()
    # With the FS view confined to tmp_path, no ancestor marker/.git exists.
    assert mgr._discover_root(lonely) == lonely.resolve()


def test_discovery_launch_dir_wins_over_saved_workspace(tmp_path, monkeypatch):
    # A launch dir that IS a project (git root) must win over a previously
    # persisted workspace. Otherwise launching `agent2` from a new folder would
    # stay pinned to the old saved path.
    _confine_fs_to(tmp_path, monkeypatch)
    saved = tmp_path / "old_ws"
    saved.mkdir()
    db.set_setting("active_workspace", str(saved.resolve()))
    mgr = WorkspaceManager()
    proj = tmp_path / "new_proj"
    (proj / ".git").mkdir(parents=True)
    assert mgr._discover_root(proj) == proj.resolve()


def test_discovery_uses_saved_workspace_when_launch_dir_is_neutral(tmp_path, monkeypatch):
    # Launched from a NEUTRAL spot (the Agent2 install dir / home) with no
    # project markers → fall back to the persisted workspace so the Web UI
    # "remember my last folder" behavior survives a relaunch.
    _confine_fs_to(tmp_path, monkeypatch)
    saved = tmp_path / "remembered"
    saved.mkdir()
    db.set_setting("active_workspace", str(saved.resolve()))
    mgr = WorkspaceManager()
    neutral = tmp_path / "install_root"
    neutral.mkdir()
    # Make the launch dir count as neutral: pretend it's the Agent2 install dir.
    import agent2.config as cfg
    monkeypatch.setattr(cfg, "ROOT", neutral.resolve())
    assert mgr._discover_root(neutral) == saved.resolve()


def test_discovery_plain_folder_wins_over_saved_workspace(tmp_path, monkeypatch):
    # Launched from an ARBITRARY folder (not the install dir / home) with no
    # git/marker files → that folder wins. A saved workspace must NOT hijack a
    # deliberate `agent2` launch from some other directory.
    _confine_fs_to(tmp_path, monkeypatch)
    saved = tmp_path / "remembered"
    saved.mkdir()
    db.set_setting("active_workspace", str(saved.resolve()))
    mgr = WorkspaceManager()
    work = tmp_path / "elsewhere"
    work.mkdir()
    assert mgr._discover_root(work) == work.resolve()


def test_discovery_stops_at_home_ceiling(tmp_path, monkeypatch):
    # A stray marker file (or .git) living in the user's HOME directory must NOT
    # hijack a launch from a subfolder of home. Otherwise `agent2` from any
    # project under home climbs up, finds e.g. a leftover package.json in
    # C:\Users\<name>, and wrongly adopts home as the project root. The launch
    # subfolder (which has no markers of its own) must win instead.
    _confine_fs_to(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    (home / "package.json").write_text("{}\n")   # stray marker in home
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    mgr = WorkspaceManager()
    work = home / "projects" / "myapp"           # a subfolder of home, no markers
    work.mkdir(parents=True)
    assert mgr._discover_root(work) == work.resolve()


def test_repair_root_never_returns_relative(tmp_path):
    mgr = WorkspaceManager()
    repaired = mgr._repair_root(Path("does_not_exist_rel"), tmp_path)
    assert repaired.is_absolute()
    assert repaired.is_dir()


# ── Workspace: the sandbox (section 2) ────────────────────────────────────────

def test_validate_accepts_path_inside(ws, tmp_path):
    (tmp_path / "sub").mkdir()
    resolved = ws.validate_path("sub/file.txt", tool="write_file")
    assert str(resolved).startswith(str(tmp_path.resolve()))


def test_validate_relative_resolves_against_root_not_cwd(ws, tmp_path, monkeypatch):
    # Even if the process cwd is elsewhere, a relative path binds to the root.
    other = tmp_path.parent
    monkeypatch.chdir(other)
    resolved = ws.validate_path("inside.txt", tool="read_file")
    assert Path(resolved).parent == tmp_path.resolve()


def test_validate_rejects_parent_escape(ws):
    with pytest.raises(WorkspaceViolation) as ei:
        ws.validate_path("../../etc/passwd", tool="read_file")
    assert str(ei.value) == BLOCKED_MSG


def test_validate_rejects_absolute_outside(ws):
    outside = os.path.abspath(os.sep + "definitely_outside_root_xyz")
    with pytest.raises(WorkspaceViolation):
        ws.validate_path(outside, tool="write_file")


def test_validate_rejects_unc_and_network(ws):
    for bad in (r"\\server\share\x", "//server/share/x"):
        with pytest.raises(WorkspaceViolation):
            ws.validate_path(bad, tool="read_file")


def test_validate_rejects_empty(ws):
    for bad in ("", "   ", None):
        with pytest.raises(WorkspaceViolation):
            ws.validate_path(bad, tool="read_file")


def test_violation_message_is_exact():
    assert str(WorkspaceViolation("whatever")) == BLOCKED_MSG


# ── Workspace: switching + persistence + callbacks (section 12) ───────────────

def test_set_workspace_switches_and_persists(ws, tmp_path):
    dest = tmp_path.parent / "dest_ws"
    dest.mkdir()
    new = ws.set_workspace(str(dest))
    assert Path(new.path).resolve() == dest.resolve()
    assert db.get_setting("active_workspace") == str(dest.resolve())


def test_set_workspace_same_root_is_noop(ws, tmp_path):
    before = ws.get_current_workspace()
    same = ws.set_workspace(str(tmp_path))
    assert same.id == before.id  # unchanged instance → no-op


def test_on_switch_callback_fires(ws, tmp_path):
    seen = {}
    ws.on_switch(lambda old, new: seen.update(old=old, new=new.path))
    dest = tmp_path.parent / "cb_ws"
    dest.mkdir()
    ws.set_workspace(str(dest))
    assert seen["new"] == str(dest.resolve())
    assert Path(seen["old"]).resolve() == tmp_path.resolve()


# ── Session: per-chat isolation + cancellation (sections 6-9) ─────────────────

def test_sessions_are_isolated_per_chat():
    sm = SessionManager()
    a = sm.open("sid1", "chatA")
    b = sm.open("sid1", "chatB")
    assert a.cancel is not b.cancel
    # Cancelling A must not touch B.
    sm.cancel("sid1", "chatA")
    assert a.stopped and not b.stopped


def test_new_turn_mints_fresh_cancel_token():
    sm = SessionManager()
    first = sm.open("sid1", "chatA")
    first.cancel.set()
    second = sm.open("sid1", "chatA")   # same chat, new turn
    assert second is first              # same session record...
    assert not second.stopped           # ...but a brand-new, unset token


def test_stop_button_cancels_all_chats_on_sid():
    sm = SessionManager()
    a = sm.open("sid1", "chatA")
    b = sm.open("sid1", "chatB")
    c = sm.open("sid2", "chatC")
    n = sm.cancel("sid1")               # no chat_id → sid-wide
    assert n == 2
    assert a.stopped and b.stopped and not c.stopped


# ── Section 12: switching workspaces cancels running tasks ───────────────────
# This wiring lives in a module-level side effect of importing core.session, and
# the registration used to sit inside `try/except: pass`. That made the whole
# guarantee silently optional — a rename of `manager` or `on_switch` would
# disable task cancellation with nothing logged. The two tests below cover the
# registration and the behaviour separately, because a passing behaviour test
# that calls the callback DIRECTLY would not notice it was never registered.

def test_session_module_registered_its_workspace_switch_hook():
    """The hook must actually be on the real manager, not merely defined.

    Deliberately reads the module singleton rather than the `ws` fixture's
    throwaway manager: registration happens once at import time against the
    singleton, so that is the only object that can prove it happened.
    """
    from agent2.core import session as sess_mod

    registered = getattr(ws_mod.manager, "_on_switch", [])
    assert sess_mod._on_workspace_switch in registered, (
        "core.session never registered its workspace-switch hook — switching "
        "workspaces would leave tasks running against the OLD root"
    )


def test_workspace_switch_cancels_running_tasks(ws, tmp_path):
    """End-to-end: a real set_workspace() must stop in-flight tasks."""
    from agent2.core import session as sess_mod
    from agent2.core.session import sessions

    # Register the PRODUCTION callback against the fixture's manager (the
    # import-time registration is bound to the real singleton this fixture
    # replaced). Re-implementing the cancellation here would only test the
    # test.
    ws.on_switch(sess_mod._on_workspace_switch)

    live = sessions.open("sid-ws", "chat-ws")
    assert not live.stopped and live.task.status == "running"
    try:
        dest = tmp_path.parent / "switch_ws"
        dest.mkdir(exist_ok=True)
        ws.set_workspace(str(dest))

        assert live.stopped, "the task kept running against the old workspace root"
        assert live.task.status == "cancelled"
    finally:
        sessions.cleanup_sid("sid-ws")


def test_stream_ownership_rejects_foreign_task():
    sm = SessionManager()
    sess = sm.open("sid1", "chatA")
    task_id = sess.task.id
    assert sm.owns_stream("sid1", "chatA", task_id) is True
    assert sm.owns_stream("sid1", "chatA", "not-the-task") is False
    # A cancelled session no longer owns its stream.
    sm.cancel("sid1", "chatA")
    assert sm.owns_stream("sid1", "chatA", task_id) is False


def test_active_tasks_and_close():
    sm = SessionManager()
    sm.open("sid1", "chatA")
    assert len(sm.active_tasks()) == 1
    sm.close("sid1", "chatA")
    assert sm.active_tasks() == []


def test_cleanup_sid_drops_and_cancels():
    sm = SessionManager()
    a = sm.open("sid1", "chatA")
    sm.cleanup_sid("sid1")
    assert a.stopped
    assert sm.get("sid1", "chatA") is None


# ── Memory / rules / context: shared DB backends ──────────────────────────────

def test_memory_crud_and_prompt_block():
    assert mem_mod.list_memories() == []
    row = mem_mod.add_memory("remember this")
    assert row and row["content"] == "remember this"
    assert mem_mod.add_memory("   ") is None      # empty rejected
    block = mem_mod.memory_prompt_block()
    assert "remember this" in block and "MEMORIES" in block
    mem_mod.delete_memory(row["id"])
    assert mem_mod.list_memories() == []


def test_memory_dedups_on_exact_content():
    """The agent re-derives the same fact across sessions. Storing it twice costs
    prompt tokens on EVERY turn and teaches the model nothing."""
    a = mem_mod.add_memory("user prefers dark mode")
    b = mem_mod.add_memory("user prefers dark mode")
    assert mem_mod.count_memories() == 1
    assert a["id"] == b["id"]          # the caller cannot tell dedup from insert


def test_memory_dedup_reinforces_importance_and_never_lowers_it():
    mem_mod.add_memory("deploy target is fly.io", importance=9)
    mem_mod.add_memory("deploy target is fly.io", importance=2)
    row = mem_mod.list_memories()[0]
    assert row["importance"] == 9      # a weaker repeat must not demote it
    mem_mod.add_memory("deploy target is fly.io", importance=10)
    assert mem_mod.list_memories()[0]["importance"] == 10


def test_memory_persists_importance_and_tags():
    """Both are in the save_memory tool schema; they used to be dropped."""
    row = mem_mod.add_memory("uses pnpm", importance=8, tags=["tooling", "js"])
    assert row["importance"] == 8
    assert set(row["tags"].split(",")) == {"tooling", "js"}
    assert mem_mod.add_memory("plain", importance=99)["importance"] == 10   # clamped
    assert mem_mod.add_memory("floor", importance=-4)["importance"] == 1


def test_prompt_block_is_bounded_and_ranked():
    """⚠️ This block is re-sent on every iteration of every turn, so an unbounded
    one multiplies its token cost by MAX_AGENT_ITERS."""
    for i in range(mem_mod.PROMPT_LIMIT + 25):
        mem_mod.add_memory(f"low priority fact {i}", importance=2)
    mem_mod.add_memory("CRITICAL: prod db is read-only", importance=10)

    block = mem_mod.memory_prompt_block()
    lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert len(lines) == mem_mod.PROMPT_LIMIT + 1        # + the "not shown" note
    assert "CRITICAL: prod db is read-only" in block     # importance wins the cut
    assert "not shown" in block                          # partial recall is stated


def test_prompt_block_omits_the_note_when_everything_fits():
    mem_mod.add_memory("only fact")
    block = mem_mod.memory_prompt_block()
    assert "only fact" in block and "not shown" not in block


def test_top_memories_limits_in_sql_not_in_python(monkeypatch):
    """A heavy user's table can hold thousands; the block needs at most 40."""
    seen = {}
    real = mem_mod.qall

    def spy(sql, params=()):
        seen["sql"] = sql
        return real(sql, params)

    monkeypatch.setattr(mem_mod, "qall", spy)
    mem_mod.top_memories(5)
    assert "LIMIT" in seen["sql"].upper()


def test_bulk_delete_is_one_transaction_and_one_notify(monkeypatch):
    """Clearing N memories used to be N transactions AND N prompt rebuilds."""
    ids = [mem_mod.add_memory(f"m{i}")["id"] for i in range(5)]
    notifies = []
    monkeypatch.setattr(mem_mod, "_notify", lambda *a, **k: notifies.append(a))

    assert mem_mod.delete_memories(ids) == 5
    assert mem_mod.count_memories() == 0
    assert len(notifies) == 1


def test_bulk_delete_rejects_an_empty_set():
    """Must delete NOTHING rather than fall through to 'no filter, so all rows'."""
    mem_mod.add_memory("keep me")
    assert mem_mod.delete_memories([]) == 0
    assert mem_mod.delete_memories(None) == 0
    assert mem_mod.count_memories() == 1


def test_bulk_delete_applies_atomically_on_write_failure(monkeypatch):
    """⚠️ Fail the write itself, not the call before it — a delete that never
    started proves nothing about the transaction."""
    ids = [mem_mod.add_memory(f"m{i}")["id"] for i in range(4)]
    monkeypatch.setattr(mem_mod, "exemany",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    assert mem_mod.delete_memories(ids) == 0
    assert mem_mod.count_memories() == 4        # nothing half-deleted


def test_prune_spares_important_memories():
    for i in range(30):
        mem_mod.add_memory(f"noise {i}", importance=3)
    mem_mod.add_memory("never drop this", importance=10)

    removed = mem_mod.prune_memories(keep=10, min_importance=9)
    assert removed == 20                                  # 30 noise - keep 10
    bodies = [m["content"] for m in mem_mod.list_memories()]
    assert "never drop this" in bodies                    # exempt from the cap
    assert len(bodies) == 11


def test_prune_is_a_noop_when_under_the_cap():
    mem_mod.add_memory("a")
    assert mem_mod.prune_memories(keep=500) == 0
    assert mem_mod.count_memories() == 1


def test_clear_memories_reports_what_it_removed():
    for i in range(3):
        mem_mod.add_memory(f"m{i}")
    assert mem_mod.clear_memories() == 3
    assert mem_mod.list_memories() == []
    assert mem_mod.clear_memories() == 0        # empty is not an error


def test_save_memory_tool_goes_through_the_centralized_backend():
    """⚠️ tools.add_mem used to INSERT raw SQL, so it skipped sync.notify() and a
    memory the agent saved was absent from its own next system prompt."""
    from agent2 import tools

    notified = []
    from agent2.core import sync

    def listener(topic, payload):
        notified.append(payload)

    # Bind ONE name: unsubscribe matches by identity, so passing an equivalent
    # lambda would leave this listener registered for every later test.
    sync.subscribe("memories", listener)
    try:
        tools.add_mem("agent-saved fact", 7, ["ctf"])
    finally:
        sync.unsubscribe("memories", listener)

    row = mem_mod.list_memories()[0]
    assert row["content"] == "agent-saved fact"
    assert row["importance"] == 7          # was silently discarded
    assert row["tags"] == "ctf"
    assert notified                        # the prompt cache was invalidated


def test_rules_toggle_and_active_only():
    r = rules_mod.add_rule("always be careful")
    assert r["active"] == 1
    assert len(rules_mod.list_rules(active_only=True)) == 1
    rules_mod.toggle_rule(r["id"])
    assert rules_mod.list_rules(active_only=True) == []
    assert "CUSTOM RULES" not in rules_mod.rules_prompt_block()


def test_rules_bulk_delete_and_set_active():
    ids = [rules_mod.add_rule(f"rule {i}")["id"] for i in range(4)]

    assert rules_mod.set_rules_active(ids, False) == 4
    assert rules_mod.list_rules(active_only=True) == []
    # Uniform, not a toggle: re-running must not flip them back on.
    assert rules_mod.set_rules_active(ids, False) == 4
    assert rules_mod.list_rules(active_only=True) == []
    assert rules_mod.set_rules_active(ids, True) == 4
    assert len(rules_mod.list_rules(active_only=True)) == 4

    assert rules_mod.delete_rules(ids[:2]) == 2
    assert len(rules_mod.list_rules()) == 2
    # ⚠️ Assert the ROWS survive, not just the return value: a bulk delete that
    # falls through to "no filter, so everything" also returns 0.
    assert rules_mod.delete_rules([]) == 0
    assert len(rules_mod.list_rules()) == 2
    assert rules_mod.delete_rules(None) == 0
    assert len(rules_mod.list_rules()) == 2


def test_context_project_scoped_chats(monkeypatch):
    chat = ctx_mod.new_chat("gemini-2.5-flash", "pro", title="T")
    assert chat["cwd"] == ctx_mod.current_cwd()
    # A chat under a different cwd must not leak into this project's list.
    db.exe("UPDATE chats SET cwd=? WHERE id=?", ("/some/other/proj", chat["id"]))
    assert ctx_mod.list_chats_for_cwd() == []
    assert len(ctx_mod.list_all_chats()) == 1  # cross-project view still sees it


def test_get_or_create_chat_reuses_active():
    a = ctx_mod.get_or_create_chat("gemini-2.5-flash", "pro")
    b = ctx_mod.get_or_create_chat("gemini-2.5-flash", "pro")
    assert a["id"] == b["id"]


# ── Tools: registry + sandbox integration (sections 4, 5, 13) ─────────────────

def test_registry_unknown_tool_message():
    out = tools_mod.dispatch_tool("no_such_tool", {})
    assert out["error"] == 'Tool "no_such_tool" is not registered.'
    assert out["code"] == "unregistered_tool"


def test_registry_lists_core_tools():
    names = tools_mod.REGISTRY.list()
    for expected in ("read_file", "write_file", "grep_search", "update_todo"):
        assert expected in names


def test_registry_rejects_duplicate_and_invalid():
    reg = tools_mod.ToolRegistry()
    reg.register("x", lambda a: {})
    with pytest.raises(ValueError):
        reg.register("x", lambda a: {})          # duplicate
    with pytest.raises(ValueError):
        reg.register("", lambda a: {})           # empty name
    with pytest.raises(ValueError):
        reg.register("y", None)                  # not callable


def test_read_tool_blocks_escape(ws):
    out = tools_mod.dispatch_tool("read_file", {"path": "../../secret.txt"})
    assert out["error"] == BLOCKED_MSG
    assert out["code"] == "outside_workspace"


def test_write_tool_blocks_absolute_outside(ws):
    outside = os.path.abspath(os.sep + "outside_write_test.txt")
    out = tools_mod.dispatch_tool("write_file", {"path": outside, "content": "x"})
    assert out["error"] == BLOCKED_MSG


def test_write_then_read_inside_workspace(ws, tmp_path):
    w = tools_mod.dispatch_tool(
        "write_file", {"path": "notes/hello.txt", "content": "hi there"})
    assert w.get("success") is True
    assert (tmp_path / "notes" / "hello.txt").read_text() == "hi there"
    r = tools_mod.dispatch_tool("read_file", {"path": "notes/hello.txt"})
    assert "hi there" in r["content"]


def test_update_todo_uses_ctx_not_global(ws):
    # Two independent contexts must keep independent TODO lists (section 6).
    ctx_a = tools_mod.ToolContext(sid="s", chat_id="a")
    ctx_b = tools_mod.ToolContext(sid="s", chat_id="b")
    tools_mod.dispatch_tool(
        "update_todo", {"todos": [{"task": "one", "status": "pending"}]}, ctx_a)
    assert ctx_a.todos and not ctx_b.todos
