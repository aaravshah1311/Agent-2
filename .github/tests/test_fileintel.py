# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the File Intelligence System (agent2/fileintel/).

Run from the repo root:  python -m pytest .github/tests/test_fileintel.py -v

These tests are pure-Python and require no optional libraries or system
binaries. Router fallback / graceful-degradation behaviour is verified with a
fake in-memory registry so the result is independent of which libs happen to be
installed; plugin smoke tests only exercise stdlib-backed operations.
"""

import zipfile

import pytest

from agent2.fileintel import detector, metadata, security
from agent2.fileintel.base import Capability, PluginContext
from agent2.fileintel.registry import CapabilityRegistry
from agent2.fileintel.router import OperationRouter
from agent2.fileintel.executor import ToolExecutor
from agent2.fileintel.errors import (
    MissingDependency, BackendError, UnsafePath, FileTooLarge,
    UnsupportedOperation, UnsupportedFormat,
)


# ── helpers ──────────────────────────────────────────────────────────────────────

def _write(path, data=b"data"):
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    with open(path, mode) as fh:
        fh.write(data)
    return str(path)


PNG_SIG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


# ── detector ──────────────────────────────────────────────────────────────────────

def test_detect_by_extension(tmp_path):
    p = _write(tmp_path / "notes.md", "# hi")
    det = detector.detect(p)
    assert det["format"] == "md"
    assert det["category"] == "documents"


def test_detect_pdf_magic(tmp_path):
    p = _write(tmp_path / "doc.pdf", b"%PDF-1.7\n%rest")
    det = detector.detect(p)
    assert det["format"] == "pdf"
    assert det["category"] == "documents"
    assert det["magic_format"] == "pdf"


def test_detect_png_magic(tmp_path):
    p = _write(tmp_path / "img.png", PNG_SIG)
    det = detector.detect(p)
    assert det["format"] == "png"
    assert det["category"] == "images"


def test_detect_zip_container_is_docx(tmp_path):
    p = tmp_path / "file.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("word/document.xml", "<xml/>")
        zf.writestr("[Content_Types].xml", "<xml/>")
    det = detector.detect(str(p))
    assert det["format"] == "docx"


def test_detect_extension_content_mismatch(tmp_path):
    # A PNG mislabelled as .txt → magic wins, mismatch flagged.
    p = _write(tmp_path / "sneaky.txt", PNG_SIG)
    det = detector.detect(p)
    assert det["format"] == "png"
    assert det["mismatch"] is True


def test_detect_missing_file():
    det = detector.detect("does/not/exist.pdf")
    assert det["exists"] is False
    assert det["format"] == "pdf"  # falls back to extension


# ── metadata ──────────────────────────────────────────────────────────────────────

def test_metadata_basic(tmp_path):
    p = _write(tmp_path / "a.txt", "hello world")
    meta = metadata.extract_metadata(p)
    assert meta["size"] == 11
    assert meta["extension"] == "txt"
    assert len(meta["sha256"]) == 64
    assert meta["modified"]


def test_metadata_missing_file_is_graceful():
    meta = metadata.extract_metadata("nope.txt")
    assert meta["exists"] is False
    assert "size" not in meta


# ── registry ───────────────────────────────────────────────────────────────────────

def test_registry_register_and_resolve():
    reg = CapabilityRegistry()
    reg.register("fake", ["xyz"], [Capability("read", lambda p, o, c: {"ok": 1}, backend="b1")])
    assert reg.operations_for("xyz") == ["read"]
    caps = reg.resolve("xyz", "read")
    assert len(caps) == 1 and caps[0].backend == "b1"


def test_registry_orders_backends_by_priority():
    reg = CapabilityRegistry()
    reg.register("p", ["fmt"], [
        Capability("op", lambda p, o, c: {}, backend="slow", priority=90),
        Capability("op", lambda p, o, c: {}, backend="fast", priority=10),
    ])
    order = [c.backend for c in reg.resolve("fmt", "op")]
    assert order == ["fast", "slow"]


# ── router: error recovery / fallback ────────────────────────────────────────────

def _ctx():
    return PluginContext(on_progress=lambda m: None)


def test_router_falls_back_on_missing_dependency():
    def backend_a(p, o, c):
        raise MissingDependency("libA")

    def backend_b(p, o, c):
        return {"used": "b"}

    reg = CapabilityRegistry()
    reg.register("p", ["fmt"], [
        Capability("op", backend_a, backend="a", priority=10),
        Capability("op", backend_b, backend="b", priority=20),
    ])
    router = OperationRouter(reg)
    result = router.run("fmt", "op", "x", {}, _ctx())
    assert result["used"] == "b"
    assert result["_backend"] == "b"


def test_router_all_backends_missing_raises_backenderror():
    reg = CapabilityRegistry()
    reg.register("p", ["fmt"], [
        Capability("op", lambda p, o, c: (_ for _ in ()).throw(MissingDependency("x")), backend="a"),
    ])
    router = OperationRouter(reg)
    with pytest.raises(BackendError):
        router.run("fmt", "op", "x", {}, _ctx())


def test_router_unknown_operation():
    reg = CapabilityRegistry()
    reg.register("p", ["fmt"], [Capability("read", lambda p, o, c: {}, backend="a")])
    router = OperationRouter(reg)
    with pytest.raises(UnsupportedOperation):
        router.run("fmt", "nonexistent", "x", {}, _ctx())


def test_router_unsupported_format():
    router = OperationRouter(CapabilityRegistry())
    with pytest.raises(UnsupportedFormat):
        router.run("weirdfmt", "op", "x", {}, _ctx())


def test_router_wraps_unexpected_exception_and_recovers():
    def boom(p, o, c):
        raise ValueError("raw lib error")

    def ok(p, o, c):
        return {"ok": True}

    reg = CapabilityRegistry()
    reg.register("p", ["fmt"], [
        Capability("op", boom, backend="boom", priority=10),
        Capability("op", ok, backend="ok", priority=20),
    ])
    # a raw exception in one backend must not escape; next backend wins
    assert OperationRouter(reg).run("fmt", "op", "x", {}, _ctx())["ok"] is True


# ── security ───────────────────────────────────────────────────────────────────────

def test_security_size_limit(tmp_path, monkeypatch):
    from agent2.fileintel import security as sec
    monkeypatch.setattr(sec, "MAX_FILE_SIZE", 4)
    p = _write(tmp_path / "big.txt", "toolong")
    with pytest.raises(FileTooLarge):
        sec.validate_size(__import__("pathlib").Path(p))


def test_security_blocks_executable(tmp_path):
    p = _write(tmp_path / "mal.exe", b"MZ")
    with pytest.raises(UnsafePath):
        security.preflight(p)
    # explicit override is allowed
    assert security.preflight(p, allow_executable=True)


def test_security_workspace_confinement(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    inside = _write(root / "ok.txt", "hi")
    outside = _write(tmp_path / "evil.txt", "x")
    assert security.preflight(inside, workspace_root=str(root))
    with pytest.raises(UnsafePath):
        security.preflight(outside, workspace_root=str(root))


# ── security: the OPTIONS dict carries paths too ──────────────────────────────────
# `options` is model-supplied, and ~26 plugin sites read a path out of it and
# then write to it. Confining only the subject path left that wide open.

def test_confine_options_rewrites_an_inside_path_and_refuses_every_escape(tmp_path):
    from pathlib import Path
    root = tmp_path / "ws"
    root.mkdir()

    # A relative option resolves against the ROOT, not the process cwd — the
    # rule workspace.validate_path already applies to every other tool path.
    opts = security.confine_options({"output_path": "out.txt"}, str(root))
    assert Path(opts["output_path"]) == root / "out.txt"

    for bad in ({"output_path": str(tmp_path / "evil.txt")},
                {"output_dir": ".." },
                {"other": str(tmp_path / "evil.txt")},
                {"compare_to": "../evil.txt"},
                {"paths": ["ok.txt", "../evil.txt"]}):
        with pytest.raises(UnsafePath):
            security.confine_options(bad, str(root))


def test_confine_options_leaves_non_path_options_and_bad_types_alone(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    raw = {"to_format": "txt", "quality": 80, "output_path": None, "paths": "notalist"}
    assert security.confine_options(raw, str(root)) == raw


def test_confine_options_is_a_no_op_without_a_root():
    # Direct library use keeps its whole-machine posture (resolve_safe's rule).
    raw = {"output_path": "/anywhere/out.txt", "paths": ["a", "b"]}
    assert security.confine_options(raw, None) == raw


def test_executor_refuses_an_output_dir_that_escapes_the_workspace(tmp_path):
    from agent2.fileintel import EXECUTOR
    root = tmp_path / "ws"
    root.mkdir()
    archive = root / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("inner.txt", "payload")

    escape = tmp_path / "outside_dir"
    res = EXECUTOR.run_operation(str(archive), "extract",
                                 {"output_dir": str(escape)},
                                 workspace_root=str(root))
    assert "error" in res
    assert not escape.exists(), "extraction wrote outside the workspace"

    # The same operation still works inside it — the guard confines, it does not
    # remove the ability to choose an output directory.
    ok = EXECUTOR.run_operation(str(archive), "extract",
                                {"output_dir": str(root / "out")},
                                workspace_root=str(root))
    assert "error" not in ok, ok
    assert (root / "out" / "inner.txt").read_text() == "payload"


# ── executor (end-to-end, stdlib path) ───────────────────────────────────────────

def test_executor_returns_structured_error_never_raises():
    reg = CapabilityRegistry()
    reg.register("p", ["fmt"], [
        Capability("op", lambda p, o, c: (_ for _ in ()).throw(MissingDependency("pypdf")),
                   backend="pypdf"),
    ])
    ex = ToolExecutor(reg)
    # nonexistent path → clean error dict, not an exception
    res = ex.run_operation("no/file.fmt", "op")
    assert "error" in res
    assert isinstance(res.get("progress"), list)


def test_executor_runs_txt_read(tmp_path):
    # uses the REAL registry (documents plugin, native backend — no optional lib)
    from agent2.fileintel import EXECUTOR
    p = _write(tmp_path / "doc.txt", "line one\nline two")
    res = EXECUTOR.run_operation(p, "read")
    assert "error" not in res, res
    assert "line one" in res["text"]
    assert res["progress"][-1] == "Completed."


# ── plugin smoke tests (stdlib-only operations) ──────────────────────────────────

def test_documents_read_markdown(tmp_path):
    from agent2.fileintel import run_op
    p = _write(tmp_path / "r.md", "# Title\n\nBody text")
    res = run_op(p, "read")
    assert "Title" in res["text"]


def test_spreadsheets_analyze_csv(tmp_path):
    from agent2.fileintel import run_op
    p = _write(tmp_path / "data.csv", "name,age\nalice,30\nbob,40\n")
    res = run_op(p, "analyze")
    assert res["rows"] == 2
    assert res["columns"] == 2
    stats = {c["name"]: c for c in res["column_stats"]}
    assert stats["age"]["type"] == "numeric"
    assert stats["age"]["mean"] == 35.0


def test_archives_zip_list_and_extract(tmp_path):
    from agent2.fileintel import run_op
    zp = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("a.txt", "aaa")
        zf.writestr("sub/b.txt", "bbb")
    listing = run_op(str(zp), "list")
    assert listing["count"] == 2
    out = run_op(str(zp), "extract", {"output_dir": str(tmp_path / "out")})
    assert out["extracted"] == 2


def test_code_analyze_python(tmp_path):
    from agent2.fileintel import run_op
    src = "import os\n\n# a comment\ndef foo():\n    return 1\n\nclass Bar:\n    pass\n"
    p = _write(tmp_path / "mod.py", src)
    res = run_op(p, "analyze")
    assert res["language"] == "Python"
    assert "foo" in res["symbols"]
    assert "Bar" in res["symbols"]
    assert res["comment_lines"] >= 1


# ── detect_file high-level API ────────────────────────────────────────────────────

def test_detect_file_lists_operations(tmp_path):
    from agent2.fileintel import detect_file
    p = _write(tmp_path / "x.csv", "a,b\n1,2\n")
    info = detect_file(p)
    assert info["format"] == "csv"
    assert "analyze" in info["operations"]
    assert "convert" in info["operations"]


def test_file_capabilities_by_category():
    from agent2.fileintel import file_capabilities
    caps = file_capabilities("images")
    assert caps["category"] == "images"
    assert "png" in caps["formats"]


# ── workspace search ──────────────────────────────────────────────────────────────

def test_search_content(tmp_path):
    from agent2.fileintel import search_workspace
    _write(tmp_path / "a.py", "x = 1  # TODO fix this\n")
    _write(tmp_path / "b.txt", "nothing here\n")
    res = search_workspace("TODO", path=str(tmp_path), kind="content")
    assert res["match_count"] == 1
    assert "a.py" in res["matches"][0]


def test_search_secrets_preset(tmp_path):
    from agent2.fileintel import search_workspace
    _write(tmp_path / "conf.py", 'API_KEY = "AKIAiosfodnn7EXAMPLE1"\n')
    res = search_workspace("API keys", path=str(tmp_path))
    assert res["kind"] == "secrets"
    assert res["match_count"] >= 1


def test_search_filename(tmp_path):
    from agent2.fileintel import search_workspace
    _write(tmp_path / "invoice_2024.pdf", b"%PDF-1.4")
    _write(tmp_path / "readme.md", "x")
    res = search_workspace("invoices", path=str(tmp_path))
    assert res["kind"] == "filename"
    assert res["match_count"] == 1


def test_search_duplicates(tmp_path):
    from agent2.fileintel import search_workspace
    _write(tmp_path / "one.txt", "identical")
    _write(tmp_path / "two.txt", "identical")
    _write(tmp_path / "diff.txt", "unique")
    res = search_workspace("duplicates", path=str(tmp_path))
    assert res["group_count"] == 1
    assert len(res["duplicate_groups"][0]) == 2


# ── dispatch integration (tools.py shims) ────────────────────────────────────────

@pytest.fixture
def sandbox_at(tmp_path):
    """Point the central WorkspaceManager at tmp_path so the dispatch shims —
    which now resolve every path through the sandbox (section 13) — accept files
    created under tmp_path. Restores the previous workspace afterwards."""
    from agent2.core import workspace as ws_mod
    from agent2.core.workspace import Workspace
    root = tmp_path.resolve()
    prev = ws_mod.manager._current
    ws_mod.manager._current = Workspace(id="fi-test", root=root,
                                        allowed_dirs=[root],
                                        metadata={"name": root.name})
    yield root
    ws_mod.manager._current = prev


def test_dispatch_run_file_op(tmp_path, sandbox_at):
    from agent2.tools import dispatch_tool
    p = _write(tmp_path / "d.txt", "hello dispatch")
    res = dispatch_tool("run_file_op", {"path": p, "operation": "read"})
    assert "hello dispatch" in res["text"]


def test_dispatch_detect_file(tmp_path, sandbox_at):
    from agent2.tools import dispatch_tool
    p = _write(tmp_path / "d.csv", "a,b\n1,2\n")
    res = dispatch_tool("detect_file", {"path": p})
    assert res["format"] == "csv"
    assert "analyze" in res["operations"]


def test_dispatch_run_file_op_refuses_an_output_dir_outside_the_workspace(tmp_path, sandbox_at):
    """A path-bearing OPTION is confined exactly as `path` is.

    `run_file_op` used to sanitise only its subject path and forward `options`
    verbatim, so a model could name any directory on disk as `output_dir` and the
    archive plugin would happily write there — a refusal its sibling
    `convert_file` had been giving for the identical key all along.
    """
    from agent2.tools import dispatch_tool
    archive = tmp_path / "z.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("inner.txt", "payload")

    escape = tmp_path.parent / "a2-escape-run-file-op"
    res = dispatch_tool("run_file_op", {
        "path": str(archive), "operation": "extract",
        "options": {"output_dir": str(escape)}})
    assert res.get("code") == "outside_workspace", res
    assert not (escape / "inner.txt").exists()


def test_dispatch_run_file_op_hands_the_workspace_root_to_the_library(tmp_path, sandbox_at, monkeypatch):
    """The tool layer must SEND the root, or the library gate is inert.

    `security.confine_options()` and `resolve_safe()` are both no-ops without a
    workspace root, so the executor's backstop only exists if this argument is
    passed — the omission is invisible from the outside.
    """
    import agent2.fileintel as fi
    from agent2.tools import dispatch_tool
    seen = {}

    def fake_run_op(path, operation, options=None, workspace_root=None, **kw):
        seen["root"] = workspace_root
        return {"ok": True}

    def fake_convert(path, to_format, output_path=None, workspace_root=None, **kw):
        seen["convert_root"] = workspace_root
        return {"ok": True}

    monkeypatch.setattr(fi, "run_op", fake_run_op)
    monkeypatch.setattr(fi, "convert_file", fake_convert)
    p = _write(tmp_path / "t.txt", "x")
    dispatch_tool("run_file_op", {"path": p, "operation": "read"})
    dispatch_tool("convert_file", {"path": p, "to_format": "md"})
    assert seen["root"] == str(sandbox_at)
    assert seen["convert_root"] == str(sandbox_at)


def test_dispatch_run_file_op_survives_a_non_dict_options(tmp_path, sandbox_at):
    """The model sends `options` as a JSON string often enough to matter."""
    from agent2.tools import dispatch_tool
    p = _write(tmp_path / "o.txt", "hello opts")
    for opts in ('{"encoding": "utf-8"}', "not json at all", 7, None):
        res = dispatch_tool("run_file_op",
                            {"path": p, "operation": "read", "options": opts})
        assert "hello opts" in res.get("text", ""), (opts, res)
