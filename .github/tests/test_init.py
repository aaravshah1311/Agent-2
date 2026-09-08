# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for `/init` — project analysis and `.agent2/` (Tasks 29–31).

Run from the repo root:  python -m pytest .github/tests/test_init.py -v

What this suite is for
──────────────────────
`/init` is the one command in this build that reads a whole project and then
**writes a file every later turn treats as fact**. Both halves of that sentence
are where the bugs live:

1. **A partial answer must not read as a complete one.** `projectscan` walks under
   three ceilings (`config.INIT_MAX_FILES` · `INIT_MAX_DEPTH` ·
   `INIT_SCAN_BUDGET_SEC`); when one engages, `truncated`/`truncated_by` say so,
   and each analysis step carries its own guard so one unreadable manifest costs
   one field rather than the report. A scan that quietly stopped at 20 000 files
   and wrote "no tests were found" into a committed file is the failure this pair
   of modules was shaped around.
2. **A rewrite must not eat what a human wrote.** Task 31: *"DO NOT blindly
   overwrite it … user-authored instructions must have priority."* The marker
   `<!-- agent2:generated -->` is the whole mechanism, so most of this file is
   about what survives a second, third and fourth `/init`.

The load-bearing tests
──────────────────────
* ``test_an_unmarked_section_survives_every_rerun`` — delete the marker, own the
  section. This is the promise the command's name most threatens.
* ``test_the_three_human_sections_are_seeded_unmarked`` — `Agent2 Instructions` is
  seeded *without* a marker, which is what makes rule 2 true from the first write
  rather than from the first edit. Seeded marked, it would be overwritten by the
  very next run — and it is the section a user is most likely to have filled in.
* ``test_a_marker_pasted_below_the_first_line_grants_nothing`` — ownership is the
  first body line only, so quoting the marker inside your own prose cannot hand
  your section away.
* ``test_a_second_init_with_no_changes_writes_nothing`` — an identical rewrite is
  not a write. It must not dirty the tree or bump an mtime.
* ``test_init_refuses_to_write_the_machine_wide_agent2_dir`` — Task 30's "do not
  create a global project configuration accidentally", made concrete: `~/.agent2`
  holds the master key.
* ``test_narration_failure_degrades_to_evidence`` — no key, no network, a refusal
  or junk JSON each leave a usable doc that *says* nobody described it.
* ``test_the_reader_never_invalidates_and_the_writer_always_does`` and
  ``test_the_scan_reads_git_only_through_gitstate`` — the two one-declaration
  rules `TASKS.md` names for this task, pinned by AST rather than by substring,
  because both modules *document* the rule in prose that an `in src` check would
  match while the code broke it.
"""

import ast
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent2 import config                              # noqa: E402
from agent2.core import projectdoc, projectscan        # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────
def _tree_of(mod) -> ast.Module:
    return ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))


def _calls(mod, attr: str) -> bool:
    """Does this module CALL `something.attr(...)` (or a bare `attr(...)`)?

    ⚠️ Deliberately not `"attr(" in source`: both modules explain these rules in
    their own docstrings, so a substring check passes on the prose and would keep
    passing after the rule itself was broken.
    """
    for node in ast.walk(_tree_of(mod)):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == attr:
                return True
            if isinstance(fn, ast.Name) and fn.id == attr:
                return True
    return False


def _imported(mod) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_tree_of(mod)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
            names.update(a.name for a in node.names)
    return names


def _doc(root: Path) -> str:
    return (root / ".agent2" / "agent2.md").read_text(encoding="utf-8")


def _sections(text: str) -> list[str]:
    return [ln[3:].strip() for ln in text.splitlines() if ln.startswith("## ")]


def _model(payload: str):
    """A stand-in for the one off-turn model call, so no test needs a network."""
    return lambda _prompt: payload


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A small but realistic project: manifest, source, tests, docs, entry point."""
    (tmp_path / "src" / "calc").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "calc"\ndependencies = ["flask"]\n', encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "# calc\n\nA calculator for the terminal.\n", encoding="utf-8")
    (tmp_path / "src" / "calc" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "calc" / "main.py").write_text(
        'def add(a, b):\n    return a + b\n\n\nif __name__ == "__main__":\n    add(1, 2)\n',
        encoding="utf-8")
    (tmp_path / "tests" / "test_calc.py").write_text(
        "def test_add():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def rep(tree: Path) -> dict:
    return projectscan.scan(str(tree))


# ══════════════════════════════════════════════════════════════════════════════
# 1. The scan (Task 29)
# ══════════════════════════════════════════════════════════════════════════════
def test_scan_returns_the_declared_shape_for_a_missing_directory():
    """⚠️ Total, like `gitstate.empty()`: every key a caller may read is present
    even when nothing was read, so no renderer has to ask first."""
    out = projectscan.scan(str(Path("does") / "not" / "exist" / "anywhere"))
    for key in projectscan.empty():
        assert key in out, key
    assert out["errors"]


def test_scan_finds_the_language_manifest_tests_and_entry_point(rep, tree):
    assert rep["name"] == tree.name
    assert rep["primary_language"] == "Python"
    assert any(m["label"] == "pip" for m in rep["package_managers"])
    assert rep["tests"]["files"] >= 1
    assert "pytest" in rep["tests"]["runners"]
    assert any(e["path"].endswith("main.py") for e in rep["entry_points"])
    assert rep["readme"] == "README.md"


def test_scan_derives_a_test_command_for_the_directory_it_actually_found(rep):
    """⚠️ Never a hard-coded `tests/`. This repository's own suite lives in
    `.github/tests/`, so a literal would emit a command that collects nothing —
    and `/init` writes that command into a file the agent later runs."""
    cmds = [c["cmd"] for c in rep["commands"]["test"]]
    assert cmds, "a project with test files and a proved runner must yield one"
    assert any("pytest" in c for c in cmds)
    assert any("tests" in c for c in cmds)


def test_a_runner_is_proved_never_guessed(tmp_path):
    """`a.test.ts` admits Jest, Vitest and Mocha, so it proves none of them: the
    honest answer is the test files with an EMPTY runner list, which is what stops
    `## Test Commands` from carrying a command that runs nothing."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.test.ts").write_text("it('x', () => {})\n", encoding="utf-8")
    out = projectscan.scan(str(tmp_path))
    assert out["tests"]["files"] >= 1
    assert out["tests"]["runners"] == []
    assert out["commands"]["test"] == []


def test_scan_reports_a_ceiling_rather_than_stopping_quietly(tmp_path, monkeypatch):
    """⚠️ The difference between "no tests" and "I never got that far"."""
    for i in range(40):
        (tmp_path / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(config, "INIT_MAX_FILES", 5)
    out = projectscan.scan(str(tmp_path))
    assert out["truncated"] is True
    assert out["truncated_by"] == "files"


def test_scan_never_raises_and_one_broken_step_costs_one_field(tree, monkeypatch):
    """⚠️ ONE GUARD PER STEP, never one around the sequence: a language census
    that explodes must not cost the caller the tests, the commands or the git
    state — the fields a later turn actually acts on."""
    def boom(*_a, **_kw):
        raise RuntimeError("census on fire")

    monkeypatch.setattr(projectscan, "_languages", boom)
    out = projectscan.scan(str(tree))
    assert out["languages"] == [], "the broken step produced nothing"
    assert out["tests"]["files"] >= 1, "a later step still ran"
    assert out["commands"]["test"], "and so did the one after that"
    assert any(e.get("where") == "languages" for e in out["errors"]), \
        "the failure is recorded, never silently swallowed"


def test_the_scan_reads_git_only_through_gitstate():
    """⚠️ `TASKS.md` for Task 29: "reads `core/gitstate.py`, never a second `git`
    subprocess". A second reader has no timeout and no cache, so a hung `git`
    hangs the turn and the status bar disagrees with the doc."""
    names = _imported(projectscan)
    assert "subprocess" not in names
    assert "gitstate" in names


def test_scan_writes_nothing(tree):
    """⚠️ The READER never writes — `.agent2/` is `projectdoc`'s alone."""
    before = sorted(p.name for p in tree.iterdir())
    projectscan.scan(str(tree))
    assert sorted(p.name for p in tree.iterdir()) == before
    assert not (tree / ".agent2").exists()


def test_the_reader_never_invalidates_and_the_writer_always_does():
    """⚠️ The reader does not invalidate; the writer does. Dropping the cache from
    a read path makes every scan cost the next caller three extra `git` forks and
    hides which component actually changed the tree."""
    assert _calls(projectscan, "invalidate") is False
    assert _calls(projectdoc, "invalidate") is True


# ══════════════════════════════════════════════════════════════════════════════
# 1b. What the project's own files say about themselves (`file_notes`)
#
# Asked for as: "/init should read several important files and then conclude all,
# several files, comments in file gives itea about project". The whole subsystem
# turns on one boundary — a comment may leave the machine, a statement may not —
# so these tests attack that boundary from both sides.
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def noted(tmp_path: Path) -> Path:
    """A project whose files describe themselves in four comment dialects."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "main.py").write_text(
        '#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n'
        '"""main.py The launcher. It wires the parser to the engine."""\n'
        'SECRET_IN_SOURCE = "do-not-send-me"\n', encoding="utf-8")
    (tmp_path / "pkg" / "__init__.py").write_text(
        '"""The engine package. Owns evaluation."""\n', encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "/* The browser half. Renders the keypad and nothing else. */\n"
        "const CANARY_JS = 'do-not-send-js';\n", encoding="utf-8")
    (tmp_path / "deploy.sh").write_text(
        "#!/bin/sh\n# Ships the container. Assumes the image is already built.\n"
        "export CANARY_SH=do-not-send-sh\n", encoding="utf-8")
    (tmp_path / "schema.sql").write_text(
        "-- The one schema. Every migration appends here.\n"
        "CREATE TABLE canary_sql (x TEXT);\n", encoding="utf-8")
    return tmp_path


def _note(rep: dict, path: str) -> dict:
    return next((n for n in rep["file_notes"] if n["path"] == path), {})


def test_the_scan_reads_what_the_important_files_say_about_themselves(noted):
    """The headline ask: several files, and the comments are what comes back."""
    rep = projectscan.scan(str(noted))
    got = {n["path"] for n in rep["file_notes"]}
    assert {"main.py", "pkg/__init__.py", "app.js", "deploy.sh", "schema.sql"} <= got
    assert "The launcher." in _note(rep, "main.py")["text"]
    assert "Owns evaluation." in _note(rep, "pkg/__init__.py")["text"]
    assert "Renders the keypad" in _note(rep, "app.js")["text"]
    assert "Ships the container." in _note(rep, "deploy.sh")["text"]
    assert "Every migration appends here." in _note(rep, "schema.sql")["text"]


def test_a_note_is_a_comment_and_never_a_line_of_code(noted):
    """⚠️ THE PRIVACY BOUNDARY. `_leading_comment` is a syntax gate: comment text
    out, never the statement beneath it. This is the assertion that lets
    `projectdoc._evidence()` put these notes in a prompt that leaves the machine."""
    rep = projectscan.scan(str(noted))
    blob = " ".join(n["text"] for n in rep["file_notes"])
    for canary in ("do-not-send-me", "do-not-send-js", "do-not-send-sh",
                   "canary_sql", "SECRET_IN_SOURCE", "CREATE TABLE"):
        assert canary not in blob, canary


def test_a_format_whose_comment_syntax_is_unknown_is_skipped_not_sampled(tmp_path):
    """⚠️ An unrecognised extension returns `""`, never the head of the file — the
    difference between a gate and a heuristic. A reader that fell back to "the
    first few lines" would ship code from every format nobody had declared yet."""
    (tmp_path / "notes.wat").write_text(
        "; this looks like a comment\nsecret-in-unknown-format\n", encoding="utf-8")
    (tmp_path / "data.psd").write_text("do-not-send-binary\n", encoding="utf-8")
    rep = projectscan.scan(str(tmp_path))
    assert rep["file_notes"] == []
    assert projectscan._leading_comment("# hi there\n", "") == ""


def test_the_shebang_and_the_coding_cookie_are_stepped_over(noted):
    """Both sit above the docstring in this repo's own files, and reporting
    `!/usr/bin/env python` as what a module is about is worse than saying nothing."""
    text = _note(projectscan.scan(str(noted)), "main.py")["text"]
    assert "usr/bin/env" not in text
    assert "coding" not in text
    assert text.startswith("The launcher.")


def test_a_credit_header_is_not_a_description(tmp_path):
    """⚠️ `_ATTRIB`. Four of this repository's own modules open with the same three
    credit lines and no docstring; a reader that kept them reported "Author: …
    Portfolio: …" as what each of them is about, identically."""
    (tmp_path / "tool.py").write_text(
        "# Author: Ada Lovelace\n# Portfolio: example.invalid\n"
        "# github: github.com/example\nimport os\n", encoding="utf-8")
    (tmp_path / "vendored.py").write_text(
        "# Copyright (c) 2026 Somebody Else\n# All rights reserved.\n"
        "# SPDX-License-Identifier: MIT\nimport sys\n", encoding="utf-8")
    (tmp_path / "real.py").write_text(
        "# The retry helper. Wraps urllib with backoff.\nimport sys\n", encoding="utf-8")
    rep = projectscan.scan(str(tmp_path))
    got = {n["path"] for n in rep["file_notes"]}
    assert got == {"real.py"}, "a note that is nothing but attribution is not a note"


def test_a_note_does_not_open_by_restating_its_own_filename(noted):
    """⚠️ `_strip_self_reference`. Every consumer prints the path beside the note,
    so a docstring that opens with the module's own path would spend the first
    third of the answer on the question."""
    rep = projectscan.scan(str(noted))
    assert _note(rep, "main.py")["text"].startswith("The launcher.")
    assert not _note(rep, "main.py")["summary"].startswith("main.py")


def test_a_note_that_names_a_different_file_is_reported_verbatim(tmp_path):
    """⚠️ Only a SELF reference is stripped. `agent2/server/routes.py` opens with a
    stale `agent2/routes.py`, and reporting that is the honest answer — the
    docstring really does say so, and a reader that "tidied" it would be editing
    the author's words."""
    (tmp_path / "b.py").write_text('"""a.py The old name. Still here."""\n',
                                   encoding="utf-8")
    rep = projectscan.scan(str(tmp_path))
    assert _note(rep, "b.py")["text"].startswith("a.py The old name.")


def test_every_note_states_why_that_file_was_read(noted):
    """⚠️ A list of files with no stated reason is indistinguishable from a random
    sample — and Task 31 writes this list into a file a later turn reads as fact."""
    rep = projectscan.scan(str(noted))
    assert rep["file_notes"]
    allowed = {"entry point", *projectscan.NOTE_WHY.values(), "source file"}
    for n in rep["file_notes"]:
        assert n["why"] in allowed, n
        assert n["language"], n
    assert _note(rep, "main.py")["why"] == "entry point", \
        "what the project declares as its start is read first"


def test_the_note_cap_engages_visibly(noted, monkeypatch):
    """⚠️ A cap that engages is reported, the same discipline the walk's three
    ceilings follow. `truncated` is the flag; a silent cut reads as a whole
    thought."""
    monkeypatch.setattr(config, "INIT_NOTE_CHARS", 120, raising=False)
    (noted / "long.py").write_text(f'"""{"word " * 300}"""\n', encoding="utf-8")
    n = _note(projectscan.scan(str(noted)), "long.py")
    assert n["truncated"] is True
    assert n["chars"] == 120
    assert len(n["text"]) <= 120
    assert n["summary"].endswith("…"), "a cut sentence is visibly cut"


def test_the_note_summary_is_shipped_so_three_printers_cannot_disagree(noted):
    """⚠️ ONE DECLARATION of "the first sentence of a note" — `projectdoc`'s
    `## Important Files`, `cli/render._scan_rows()` and the browser all print this
    field. A second trim in JavaScript would describe a different file than the
    one written to disk, the reason `core/highlight.py` ships `spans`."""
    rep = projectscan.scan(str(noted))
    assert _note(rep, "pkg/__init__.py")["summary"] == "The engine package."
    src = Path(projectdoc.__file__).read_text(encoding="utf-8")
    assert "_first_sentence" not in src, "the doc reads `summary`, it does not re-derive it"
    js = (Path(projectscan.__file__).parents[2] / "public" / "script.js").read_text(
        encoding="utf-8")
    assert "n.summary" in js and "file_notes" in js


def test_reading_notes_can_be_switched_off_with_a_number(noted, monkeypatch):
    """⚠️ THE ONE `/init` CEILING WHOSE `0` IS A FEATURE. A note is prose a human
    wrote and the only part of the scan that reaches the narration call, so "do not
    read my comments" is expressible as a number rather than as trust in a
    description — exactly as `AGENT2_SKILLS=0` and `AGENT2_METRICS=0` are."""
    monkeypatch.setattr(config, "INIT_NOTE_FILES", 0, raising=False)
    rep = projectscan.scan(str(noted))
    assert rep["file_notes"] == []
    assert "file_notes" in projectscan.empty(), "the key is total either way"
    assert projectdoc._evidence(rep, "", "").count("key files") == 0


def test_reading_notes_is_bounded_by_opens_not_by_a_deadline(tmp_path, monkeypatch):
    """⚠️ At most `INIT_NOTE_FILES × NOTE_READ_FACTOR` files are opened, and at most
    `NOTE_HEAD_BYTES` from each, in the shape of `_entry_points`' probe —
    deliberately not a second clock beside `INIT_SCAN_BUDGET_SEC`. Files with no
    leading comment are the ones that spend the budget, so the ceiling has to bound
    *reads* and not just results, or a directory of uncommented modules would open
    every one of them looking for a twelfth note."""
    for i in range(200):
        (tmp_path / f"m{i:03d}.py").write_text(f"x = {i}\n", encoding="utf-8")
    monkeypatch.setattr(config, "INIT_NOTE_FILES", 3, raising=False)
    # `NOTE_HEAD_BYTES` is `_notes`' own limit and no other step's, so the reads it
    # made are separable from the manifest, entry-point and convention readers.
    seen: list[int] = []
    real = projectscan._read_text
    monkeypatch.setattr(projectscan, "_read_text",
                        lambda p, n=None: seen.append(n) or real(p, n))
    projectscan.scan(str(tmp_path))
    mine = [n for n in seen if n == projectscan.NOTE_HEAD_BYTES]
    assert mine, "the step ran"
    assert len(mine) <= 3 * projectscan.NOTE_READ_FACTOR


def test_the_notes_reach_the_prompt_labelled_as_the_authors_own_words(noted):
    """⚠️ The label is what makes a note outrank a guess from a file name — and
    what stops the model reading one as this build's opinion of the file."""
    evidence = projectdoc._evidence(projectscan.scan(str(noted)), "", "")
    assert "key files" in evidence
    assert "the authors' words" in evidence
    assert "The launcher." in evidence
    assert "do-not-send-me" not in evidence


def test_important_files_names_what_to_open_and_why(noted):
    """⚠️ The half of Task 31 a census cannot write: `docs` and `config_files` are
    names, and a name does not tell an agent where the work lives."""
    rep = projectscan.scan(str(noted))
    projectdoc.apply(rep, write=True, describe=False)
    text = _doc(noted)
    # ⚠️ `"\n## Important Files\n"`, not `"## Important Files"`: `## How To Work Here`
    # *names* the other sections in its navigation bullets, so the loose form splits
    # inside that prose and this test would report the wrong block's contents.
    block = text.split("\n## Important Files\n", 1)[1].split("\n## ", 1)[0]
    # A table, because `why` is a third fact and a bullet can only carry two —
    # D3's shape, borrowed from the tables in this repo's own CLAUDE.md.
    assert "| File | What it says about itself | Why it was read |" in block
    assert "| `main.py` | The launcher. | entry point |" in block
    assert "| `schema.sql` | The one schema. |" in block
    assert "do-not-send-me" not in text, "the doc is a description, not a copy"


def test_the_cli_and_the_browser_report_the_same_notes(noted):
    """⚠️ The browser derives no fact of its own: one row and one block per
    surface, both off `file_notes`, both printing the scan's own `summary`."""
    from agent2.cli import render
    rep = projectscan.scan(str(noted))
    rows = dict(render._scan_rows(rep))
    assert "Key files" in rows
    assert f"{len(rep['file_notes'])} read" in rows["Key files"]
    assert "main.py (entry point)" in rows["Key files"]
    js = (Path(projectscan.__file__).parents[2] / "public" / "script.js").read_text(
        encoding="utf-8")
    assert "'Key files'" in js and "read · " in js


# ══════════════════════════════════════════════════════════════════════════════
# 2. Creating `.agent2/` (Task 30)
# ══════════════════════════════════════════════════════════════════════════════
def test_init_creates_the_directory_the_doc_and_the_skills_folder(rep, tree):
    res = projectdoc.apply(rep, write=True, describe=False)
    assert res["ok"] is True
    assert res["created"] is True and res["changed"] is True
    assert (tree / ".agent2" / "agent2.md").is_file()
    assert (tree / ".agent2" / "skills").is_dir()
    assert ".agent2" in res["dirs_created"]
    assert f".agent2/{projectdoc.SKILLS_DIR}" in res["dirs_created"]


def test_the_doc_path_is_the_one_the_broker_already_reads(rep, tree):
    """⚠️ ONE SPELLING. `/init` writing `.agent2/Agent2.md` while the broker reads
    `.agent2/agent2.md` produces a doc no turn ever sees — on a case-sensitive
    filesystem, silently."""
    from agent2.core import broker
    projectdoc.apply(rep, write=True, describe=False)
    assert (tree / broker.PRIMARY_DOC).is_file()


def test_init_refuses_to_write_the_machine_wide_agent2_dir(rep, monkeypatch, tmp_path):
    """⚠️ Task 30's "do not create a global project configuration accidentally".
    `~/.agent2` holds the master encryption key; a project doc there is not this
    project's — it is every project's, sitting next to the key."""
    from agent2.core import secrets
    fake_home = tmp_path / "home"
    (fake_home / ".agent2").mkdir(parents=True)
    monkeypatch.setattr(secrets, "key_file",
                        lambda: fake_home / ".agent2" / "secret.key")
    res = projectdoc.apply(rep, root=fake_home, write=True, describe=False)
    assert res["ok"] is False
    assert "master key" in res["reason"]
    assert not (fake_home / ".agent2" / "agent2.md").exists()


def test_init_refuses_the_filesystem_root(rep, tmp_path):
    res = projectdoc.apply(rep, root=Path(tmp_path.anchor), write=True, describe=False)
    assert res["ok"] is False
    assert "filesystem root" in res["reason"]


def test_init_refuses_when_fs_write_is_denied(rep, tree, monkeypatch):
    """⚠️ `/init` is a filesystem write like any other, and it refuses by
    RETURNING — both callers are printing surfaces, not exception handlers."""
    from agent2.core import permissions
    monkeypatch.setattr(permissions, "process_allows",
                        lambda cap: cap != permissions.CAP_FS_WRITE)
    res = projectdoc.apply(rep, write=True, describe=False)
    assert res["ok"] is False
    assert "fs.write" in res["reason"]
    assert not (tree / ".agent2").exists()


def test_a_broken_permission_check_fails_closed(rep, tree, monkeypatch):
    """⚠️ Everywhere else in `projectdoc` a broken helper degrades to doing less;
    here it degrades to doing NOTHING, because the helper being consulted is the
    one that says whether writing is allowed at all."""
    from agent2.core import permissions

    def boom(_cap):
        raise RuntimeError("permission table on fire")

    monkeypatch.setattr(permissions, "process_allows", boom)
    res = projectdoc.apply(rep, write=True, describe=False)
    assert res["ok"] is False
    assert "permission check unavailable" in res["reason"]
    assert not (tree / ".agent2").exists()


def test_a_dry_run_reports_the_whole_merge_and_touches_nothing(rep, tree):
    res = projectdoc.apply(rep, write=False, describe=False)
    assert res["ok"] is True and res["reason"] == "dry run"
    assert res["added"], "it still says what it would have written"
    assert res["bytes"] > 0
    assert not (tree / ".agent2").exists()


def test_apply_is_total_on_a_report_with_no_root():
    res = projectdoc.apply({}, write=True, describe=False)
    assert res["ok"] is False and res["reason"] == "no workspace root"
    for key in projectdoc.empty_result():
        assert key in res, key


# ══════════════════════════════════════════════════════════════════════════════
# 3. Generating and updating `agent2.md` (Task 31)
# ══════════════════════════════════════════════════════════════════════════════
def test_every_declared_section_is_present_on_a_fresh_write(rep, tree):
    projectdoc.apply(rep, write=True, describe=False)
    have = _sections(_doc(tree))
    for name in projectdoc.SECTIONS:
        assert name in have, name


def test_the_three_human_sections_are_seeded_unmarked(rep, tree):
    """⚠️ THE LOAD-BEARING LINE OF THE MODULE. Seeded *marked*, `Agent2
    Instructions` would be overwritten by the very next `/init` — and it is the
    section a user is most likely to have filled with standing orders."""
    projectdoc.apply(rep, write=True, describe=False)
    _pre, blocks = projectdoc.parse(_doc(tree))
    by_name = {b.heading: b for b in blocks}
    for name in projectdoc.HUMAN_SECTIONS:
        assert name in by_name, name
        assert by_name[name].generated is False, name
    assert by_name["Overview"].generated is True, "and the rest ARE Agent2's"


def test_a_marker_pasted_below_the_first_line_grants_nothing():
    """Ownership is the first body line only: a user quoting the marker while
    explaining it must not thereby hand their section away."""
    text = ("## Known Issues\n\nI mention the "
            + projectdoc.MARKER + "\nmarker here on purpose.\n")
    _pre, blocks = projectdoc.parse(text)
    assert blocks[0].generated is False


def test_an_unmarked_section_survives_every_rerun(rep, tree):
    """Delete the marker → own the section. Asserted over three further runs,
    because "preserved once" and "preserved forever" are different promises."""
    projectdoc.apply(rep, write=True, describe=False)
    path = tree / ".agent2" / "agent2.md"
    text = path.read_text(encoding="utf-8").replace(
        "## Tech Stack\n" + projectdoc.MARKER,
        "## Tech Stack\n\nWe use COBOL, actually.")
    path.write_text(text, encoding="utf-8")

    for _ in range(3):
        res = projectdoc.apply(rep, write=True, describe=False)
        assert "Tech Stack" in res["preserved"]
        assert "Tech Stack" not in res["updated"]
        assert "COBOL" in path.read_text(encoding="utf-8")


def test_a_section_agent2_does_not_generate_is_kept_in_place(rep, tree):
    projectdoc.apply(rep, write=True, describe=False)
    path = tree / ".agent2" / "agent2.md"
    text = path.read_text(encoding="utf-8").replace(
        "## Known Issues", "## Deployment\n\nssh to prod and pray.\n\n## Known Issues")
    path.write_text(text, encoding="utf-8")

    res = projectdoc.apply(rep, write=True, describe=False)
    after = path.read_text(encoding="utf-8")
    assert "Deployment" in res["preserved"]
    assert "ssh to prod and pray." in after
    assert _sections(after).index("Deployment") < _sections(after).index("Known Issues")


def test_a_marked_section_is_refreshed_when_the_project_changes(rep, tree):
    """The other half of the contract: a section Agent2 owns must actually track
    reality, or `/init` is a one-shot template rather than an update."""
    projectdoc.apply(rep, write=True, describe=False)
    (tree / "extra").mkdir()
    (tree / "extra" / "mod.py").write_text("y = 2\n", encoding="utf-8")

    res = projectdoc.apply(projectscan.scan(str(tree)), write=True, describe=False)
    assert res["changed"] is True
    assert "Project Structure" in res["updated"]
    assert "extra/" in _doc(tree)


def test_a_second_init_with_no_changes_writes_nothing(rep, tree):
    """⚠️ An identical rewrite is not a write: it would dirty the git tree and
    appear in a diff review as a change nobody made."""
    projectdoc.apply(rep, write=True, describe=False)
    path = tree / ".agent2" / "agent2.md"
    before = path.stat().st_mtime_ns
    time.sleep(0.02)

    res = projectdoc.apply(rep, write=True, describe=False)
    assert res["changed"] is False
    assert res["ok"] is True, "'already current' is a success, not a failure"
    assert path.stat().st_mtime_ns == before


def test_the_scan_never_measures_what_init_itself_wrote(tree):
    """⚠️ `/init` MAY NOT COUNT ITS OWN OUTPUT. `.agent2/` is what `/init` wrote, not
    the project, so it is off `KEEP_DOT_DIRS` and `PRIMARY_DOC` is skipped by
    `_docs`. Left in, a five-file project was reported as seven files in five
    directories, `.agent2/` appeared in the generated `## Architecture` as if a human
    had designed it, and the doc named ITSELF under `## Important Files`.

    ⚠️ Note the fresh `projectscan.scan()` on each line: the `rep` fixture is taken
    before any write, so a test that reuses it cannot see this class of bug at all —
    which is exactly why it survived. This is the real command's flow.
    """
    projectdoc.apply(projectscan.scan(str(tree)), write=True, describe=False)
    assert (tree / ".agent2" / "agent2.md").is_file(), "the write must have happened"

    after = projectscan.scan(str(tree))
    tops = [row["path"] for row in after["structure"]]
    assert ".agent2" not in tops, "Agent2's own folder is not the project's structure"
    assert ".agent2/agent2.md" not in after["docs"], "the doc may not list itself"
    assert not any(d.startswith(".agent2") for d in after["docs"])

    # And the fact IS still reported — by the one seam that owns it.
    assert after["agent2"]["present"] is True
    assert after["agent2"]["doc"] is True
    assert after["agent2"]["doc_chars"] > 0


def test_creating_the_doc_does_not_change_the_reported_size_of_the_project(tree):
    """The same rule as a measurement rather than an absence: every count and every
    row a human reads must be identical before and after `/init` ran."""
    before = projectscan.scan(str(tree))
    assert before["agent2"]["present"] is False

    projectdoc.apply(before, write=True, describe=False)
    after = projectscan.scan(str(tree))

    assert (after["files"], after["dirs"]) == (before["files"], before["dirs"])
    assert after["structure"] == before["structure"]
    assert after["docs"] == before["docs"]
    assert after["readme"] == before["readme"]


def test_a_real_rerun_of_a_hinted_project_is_byte_stable(tree):
    """The whole point of `/init` is that it can be run again. Scan → write → scan →
    write must reach a FIXED POINT, and the report must agree with the disk.

    ⚠️ `changed=False` with `updated=["Purpose"]` is the failure this pins: it said a
    section had been refreshed while the bytes were identical. `generate()` ends
    `## Purpose` with a blank line when a hint is present, `parse()` strips it, so the
    comparison fired on padding alone — forever, on every future `/init`.
    """
    hint = "a calculator for the terminal"
    first = projectdoc.apply(projectscan.scan(str(tree)), hint=hint,
                             write=True, describe=False)
    assert first["changed"] is True and first["created"] is True
    path = tree / ".agent2" / "agent2.md"
    text_after_first = path.read_text(encoding="utf-8")
    stamp = path.stat().st_mtime_ns
    time.sleep(0.02)

    for run in (2, 3):
        res = projectdoc.apply(projectscan.scan(str(tree)), write=True, describe=False)
        assert res["ok"] is True
        assert res["changed"] is False, f"run {run} rewrote a doc nothing had changed"
        assert res["updated"] == [], (
            f"run {run} reported {res['updated']} refreshed while writing nothing")
        assert res["hint"] == hint, "the recorded description must survive a rerun"
        assert path.read_text(encoding="utf-8") == text_after_first
        assert path.stat().st_mtime_ns == stamp, f"run {run} touched the file"


def test_a_generated_body_is_stored_the_way_it_is_read_back(rep):
    """⚠️ ONE canonical form. `render(parse(x))` must be a fixed point, so
    `generate()` emits bodies in the same shape `parse()` produces — no leading and
    no trailing blank lines — while INTERNAL blanks stay, because `## Architecture`
    separates its prose from the directory list on purpose."""
    gen = projectdoc.generate(rep, hint="a calculator")
    for name, lines in gen.items():
        assert not (lines and not lines[0].strip()), f"{name} starts on a blank line"
        assert not (lines and not lines[-1].strip()), f"{name} ends on a blank line"

    text, _ = projectdoc.render(rep, "", hint="a calculator")
    _pre, blocks = projectdoc.parse(text)
    for b in blocks:
        if b.heading in gen and b.generated:
            assert b.lines == gen[b.heading], (
                f"{b.heading} came back from the file in a different shape than it "
                "was generated in, so 'updated' would fire on padding")


def test_the_existing_doc_is_read_before_it_is_written(rep, tree):
    """Task 31's flow: *existing agent2.md → scan → update relevant sections*."""
    first = projectdoc.apply(rep, write=True, describe=False)
    assert first["existed"] is False and first["created"] is True
    second = projectdoc.apply(rep, write=True, describe=False)
    assert second["existed"] is True and second["created"] is False


def test_the_skills_readme_is_not_rewritten_over_a_users_empty_folder(rep, tree):
    """A user who emptied `.agent2/skills/` meant to: the seed file is written for
    a brand-new directory only."""
    projectdoc.apply(rep, write=True, describe=False)
    (tree / ".agent2" / "skills" / "README.md").unlink()
    res = projectdoc.apply(rep, write=True, describe=False)
    assert not (tree / ".agent2" / "skills" / "README.md").exists()
    assert res["files_created"] == []


def test_render_is_pure_and_needs_no_filesystem():
    """The merge is a function, not something `apply()` does inline — which is why
    every ownership rule above can be tested without a disk."""
    text, merged = projectdoc.render({"name": "x", "root": "/tmp/x"}, "")
    assert "## Overview" in text
    assert merged["added"] and not merged["preserved"]
    assert text.endswith("\n")


def test_a_malformed_existing_doc_is_never_destroyed(rep, tree):
    """A file with no headings at all is preamble, not garbage to discard."""
    adir = tree / ".agent2"
    adir.mkdir()
    (adir / "agent2.md").write_text("just some notes I typed\n", encoding="utf-8")
    projectdoc.apply(rep, write=True, describe=False)
    assert "just some notes I typed" in _doc(tree)


# ══════════════════════════════════════════════════════════════════════════════
# 4. What the project IS — narration, and the user's own sentence
# ══════════════════════════════════════════════════════════════════════════════
def test_a_model_description_becomes_purpose_features_and_architecture(rep, tree):
    reply = ('```json\n{"purpose": "A terminal calculator for accountants.",'
             ' "features": ["adds numbers", "subtracts numbers"],'
             ' "architecture": "One module behind a CLI."}\n```')
    res = projectdoc.apply(rep, write=True, describe=True, ask=_model(reply))
    text = _doc(tree)
    assert res["described"] is True and res["features"] == 2
    assert res["describe_note"] == ""
    assert "A terminal calculator for accountants." in text
    assert "- adds numbers" in text
    assert "One module behind a CLI." in text


@pytest.mark.parametrize("reply", ["", "I'm sorry, I can't help with that.", "{"])
def test_narration_failure_degrades_to_evidence(rep, tree, reply):
    """⚠️ No key, no network, a refusal and junk JSON are all the same outcome: a
    usable doc that SAYS nobody described it. An `/init` that failed without an
    API key would be useless in exactly the offline install this is built for."""
    res = projectdoc.apply(rep, write=True, describe=True, ask=_model(reply))
    assert res["ok"] is True and res["changed"] is True
    assert res["described"] is False
    assert res["describe_note"], "the reason is reported, never silent"
    assert "## Purpose" in _doc(tree), "the section exists either way"


def test_a_raising_model_never_reaches_the_caller(rep):
    def boom(_prompt):
        raise RuntimeError("network on fire")

    res = projectdoc.apply(rep, write=False, describe=True, ask=boom)
    assert res["ok"] is True
    assert res["described"] is False
    assert "model failed" in res["describe_note"]


def test_the_model_is_asked_once_at_most(rep):
    calls: list[str] = []
    projectdoc.apply(rep, write=False, describe=True,
                     ask=lambda p: calls.append(p) or "{}")
    assert len(calls) == 1


def test_describe_false_asks_nothing(rep):
    def boom(_prompt):
        raise AssertionError("no model call may happen when describe=False")

    res = projectdoc.apply(rep, write=False, describe=False, ask=boom)
    assert res["ok"] is True and res["described"] is False


def test_the_users_own_sentence_is_recorded_and_remembered(rep, tree):
    """`/init this is a project of calculator`, then a bare `/init` a year later.
    The hint lives in the doc rather than in `settings`, so it travels with a
    copied checkout and is corrected by editing the file."""
    res = projectdoc.apply(rep, write=True, describe=False,
                           hint="this is a project of calculator")
    assert res["hint"] == "this is a project of calculator"
    assert "this is a project of calculator" in _doc(tree)

    again = projectdoc.apply(rep, write=True, describe=False)
    assert again["hint"] == "this is a project of calculator", \
        "a later /init with no argument must not forget what the user said"
    assert projectdoc.prior_hint(_doc(tree)) == "this is a project of calculator"


def test_the_users_sentence_outranks_the_models(rep, tree):
    """Task 31: user-authored prose has priority over generated prose."""
    reply = '{"purpose": "A machine-learning framework.", "features": []}'
    projectdoc.apply(rep, write=True, describe=True, ask=_model(reply),
                     hint="this is a project of calculator")
    text = _doc(tree)
    assert projectdoc.HINT_PREFIX in text
    assert text.index("this is a project of calculator") < \
        text.index("A machine-learning framework."), "the user's line is printed first"


def test_the_hint_is_recorded_even_when_no_model_answers(rep, tree):
    """The user's sentence is the one piece of "why does this exist" that never
    needed a model, so a failed narration may not lose it."""
    res = projectdoc.apply(rep, write=True, describe=True, ask=_model(""),
                           hint="a calculator")
    assert res["described"] is False
    assert "a calculator" in _doc(tree)


def test_the_evidence_carries_no_source_code_or_secrets(tree):
    """⚠️ This string leaves the machine. Documentation the user wrote for readers
    is fair game; source files, manifests and `.env`s are not."""
    (tree / ".env").write_text("API_KEY=super-secret-canary\n", encoding="utf-8")
    (tree / "src" / "calc" / "main.py").write_text(
        "SECRET_IN_SOURCE = 'do-not-send-me'\n", encoding="utf-8")
    evidence = projectdoc._evidence(projectscan.scan(str(tree)), "", "")
    assert "super-secret-canary" not in evidence
    assert "do-not-send-me" not in evidence
    assert "A calculator for the terminal." in evidence, "the README IS evidence"


def test_the_previous_description_is_offered_back_as_continuity(rep, tree):
    """A second `/init` shows the model what the project was said to be, so an
    update corrects the description instead of reinventing it."""
    projectdoc.apply(rep, write=True, describe=True,
                     ask=_model('{"purpose": "A calculator.", "features": ["adds"]}'))
    seen: list[str] = []
    projectdoc.apply(rep, write=True, describe=True,
                     ask=lambda p: seen.append(p) or "")
    assert seen and "A calculator." in seen[0]


# ══════════════════════════════════════════════════════════════════════════════
# 4b. The narrative survives a model-less refresh (Task 31 — the auto-update path)
# ══════════════════════════════════════════════════════════════════════════════
# ⚠️ THIS IS THE DEFECT THE `update_project_doc` TOOL WOULD HAVE TRIPPED ON EVERY
# CALL. Agent2 refreshing its own doc after changing the project runs `apply()`
# with `describe=False` (a factual update, no model). Before the carry-forward, a
# describe=False re-`apply()` regenerated Purpose/Features/Architecture into the
# "_Not established._" placeholders and reported it as an ordinary `updated`
# section — silently erasing the description the doc exists to hold. These pin that
# a run which learns nothing new WRITES nothing new, while scan facts still refresh.
def _describe(rep, tree, purpose, features, architecture,
              dataflow="A key press enters `main`, which calls the engine."):
    import json
    reply = json.dumps({"purpose": purpose, "features": features,
                        "architecture": architecture, "dataflow": dataflow})
    return projectdoc.apply(rep, write=True, describe=True, ask=_model(reply))


def test_a_model_less_refresh_keeps_the_established_narrative(rep, tree):
    """The core of the fix: describe once with a model, refresh offline, and the
    Purpose/Features/Architecture paragraphs must still be there — and reported as
    carried, not as freshly written."""
    _describe(rep, tree, "A terminal calculator for accountants.",
              ["adds numbers", "subtracts numbers"], "One module behind a CLI.")

    res = projectdoc.apply(rep, write=True, describe=False)  # the auto-update shape
    text = _doc(tree)
    assert "A terminal calculator for accountants." in text
    assert "- adds numbers" in text
    assert "One module behind a CLI." in text
    assert "A key press enters `main`, which calls the engine." in text
    for placeholder in projectdoc._PLACEHOLDERS:
        assert placeholder not in text, "no field regenerated to a placeholder"
    # ⚠️ Every narrated field, from the one declaration — a literal tuple here would
    # have stayed green while `dataflow` was carried and never reported.
    assert set(res["narrative_carried"]) == set(projectdoc.NARRATED_FIELDS)


def test_a_model_less_refresh_is_byte_identical(rep, tree):
    """The strongest form of "learns nothing new ⇒ writes nothing new": with the
    scan unchanged the offline refresh is a true no-op — no dirty tree, no mtime
    bump, nothing for a diff review to show."""
    _describe(rep, tree, "A calculator.", ["adds"], "A CLI over one module.")
    before = _doc(tree)
    res = projectdoc.apply(rep, write=True, describe=False)
    assert res["changed"] is False
    assert _doc(tree) == before


def test_a_fresh_description_outranks_the_carried_one(rep, tree):
    """Carry-forward fills only what THIS run left blank. A model that answers
    replaces the field and it is not reported as carried."""
    _describe(rep, tree, "The old purpose.", ["old feature"], "Old architecture.")
    res = _describe(rep, tree, "The new purpose.", ["new feature"], "New architecture.")
    text = _doc(tree)
    assert "The new purpose." in text and "The old purpose." not in text
    assert "- new feature" in text and "old feature" not in text
    assert res["narrative_carried"] == []


def test_a_thin_reply_cannot_blank_a_section(rep, tree):
    """Per-field, not per-run: a model that returns a purpose but drops features
    has not retracted last run's features, so the list is carried, not emptied."""
    _describe(rep, tree, "First purpose.", ["kept feature"], "First architecture.")
    res = projectdoc.apply(
        rep, write=True, describe=True,
        ask=_model('{"purpose": "Second purpose.", "features": [], "architecture": ""}'))
    text = _doc(tree)
    assert "Second purpose." in text
    assert "- kept feature" in text, "features carried when the reply omitted them"
    assert "First architecture." in text
    assert "features" in res["narrative_carried"]
    assert "architecture" in res["narrative_carried"]
    assert "purpose" not in res["narrative_carried"]


def test_scan_facts_refresh_while_prose_is_carried(rep, tree):
    """The line between the two: a new module must appear in `## Project Structure`
    on an offline refresh even as the narrated prose stays put — carrying the lead
    paragraph must never freeze the scan-derived layout rows with it."""
    _describe(rep, tree, "A calculator.", ["adds"], "A CLI over one module.")
    (tree / "src" / "calc" / "engine.py").write_text(
        "def mul(a, b):\n    return a * b\n", encoding="utf-8")
    fresh = projectscan.scan(str(tree))  # ⚠️ re-scan, as the command/tool does

    projectdoc.apply(fresh, write=True, describe=False)
    text = _doc(tree)
    assert "A calculator." in text, "the narrated lead paragraph is kept"
    assert "engine.py" in text or "src/calc" in text.replace("\\", "/")


def test_a_user_owned_narrative_is_not_fed_back_as_generated(rep, tree):
    """If the human deleted the marker on `## Purpose`, that section is theirs. The
    marker rule already preserves it verbatim; the carry-forward must NOT also read
    it and re-emit it as generated prose — that would launder ownership."""
    _describe(rep, tree, "A purpose a model wrote.", ["adds"], "One module.")
    doc = tree / ".agent2" / "agent2.md"
    text = doc.read_text(encoding="utf-8")
    owned = text.replace(f"## Purpose\n{projectdoc.MARKER}\n", "## Purpose\n")
    assert owned != text, "the fixture must contain a generated Purpose to un-mark"
    doc.write_text(owned, encoding="utf-8")

    res = projectdoc.apply(rep, write=True, describe=False)
    after = doc.read_text(encoding="utf-8")
    assert "A purpose a model wrote." in after, "the marker rule preserves it"
    assert "Purpose" in res["preserved"], "it is the user's section now"
    assert "purpose" not in res["narrative_carried"], "an owned section is not carried"


# ══════════════════════════════════════════════════════════════════════════════
# 5. The surfaces
# ══════════════════════════════════════════════════════════════════════════════
def test_init_is_listed_once_for_help_and_autocomplete():
    """⚠️ `cli/render.SLASH_COMMANDS` is the single source for both."""
    from agent2.cli.render import SLASH_COMMANDS
    rows = [r for r in SLASH_COMMANDS if r[1] == "/init"]
    assert len(rows) == 1
    assert ".agent2" in rows[0][2], "the description must say that it WRITES"


def test_the_cli_branch_scans_describes_then_writes():
    """The command is scan → describe → write; a branch that only scanned would
    leave the user with a report and no `.agent2/`."""
    src = Path(__file__).resolve().parents[2] / "agent2cli.py"
    text = src.read_text(encoding="utf-8")
    branch = text.split('elif low == "/init"', 1)[1].split("\n        elif ", 1)[0]
    assert "_projectscan.scan()" in branch
    assert "_projectdoc.apply(" in branch
    assert "hint=" in branch, "free text after /init is a description"
    assert "render_project_doc(" in branch


def test_both_renderers_are_total_on_junk():
    """Presentation may never raise into a command."""
    from agent2.cli.render import render_project_doc, render_project_scan
    assert render_project_scan({}) is False
    assert render_project_doc({}) is False
    assert render_project_doc({"reason": "nope"}) is True
    assert render_project_scan(dict(projectscan.empty(), root="/x")) is True


def test_the_renderer_reports_a_no_op_run_as_a_success():
    """⚠️ `changed: False` is not a failure: calling a correct non-write "nothing
    happened" invites a user to go looking for the write that rightly did not
    occur, and the preserved count is the line that says their prose survived."""
    from agent2.cli.render import render_project_doc
    assert render_project_doc({
        "ok": True, "doc": "/x/.agent2/agent2.md", "changed": False,
        "created": False, "preserved": ["Agent2 Instructions"], "bytes": 10,
    }) is True


def test_the_audit_line_records_the_write(rep, monkeypatch):
    from agent2.core import logging as alog
    seen: list[dict] = []
    monkeypatch.setattr(alog, "project_doc_written", lambda **kw: seen.append(kw))
    projectdoc.apply(rep, write=True, describe=False)
    assert seen and seen[0]["created"] is True


def test_a_broken_logger_never_costs_the_write(rep, tree, monkeypatch):
    from agent2.core import logging as alog

    def boom(**_kw):
        raise RuntimeError("log on fire")

    monkeypatch.setattr(alog, "project_doc_written", boom)
    res = projectdoc.apply(rep, write=True, describe=False)
    assert res["ok"] is True
    assert (tree / ".agent2" / "agent2.md").is_file()


# ══════════════════════════════════════════════════════════════════════════════
# 6. The browser (`GET /api/project` · `POST /api/project/init`)
# ══════════════════════════════════════════════════════════════════════════════
# ⚠️ TWO SURFACES, ONE ENGINE. The whole risk of adding a web half to `/init` is
# that it re-derives *any* part of "what is this project" — because unlike a
# renderer's disagreement, this one is **committed to a file** that every later
# turn then reads as fact. So these tests pin the calls, not just the output.
def _view_calls(name: str) -> set[str]:
    """The calls made inside one `routes.py` view function, dotted where it can be.

    AST again, for `_calls`'s reason: `routes.py` explains this rule in a comment
    directly above the two views, so a substring check would match the prose.
    """
    src = Path(projectscan.__file__).resolve().parents[1] / "server" / "routes.py"
    out: set[str] = set()
    for node in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.FunctionDef) and node.name == name):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            if isinstance(fn, ast.Attribute):
                owner = fn.value
                head = f"{owner.id}." if isinstance(owner, ast.Name) else ""
                out.add(head + fn.attr)
            elif isinstance(fn, ast.Name):
                out.add(fn.id)
    return out


@pytest.fixture()
def web(tree: Path):
    """A Flask client whose ACTIVE WORKSPACE is the fixture project.

    There is deliberately no path argument on either route (see `projectscan`'s
    docstring), so pointing the workspace at `tree` is the only way to aim them —
    and the root is saved and restored for `test_authz.py`'s reason: `set_workspace`
    is global, persisted state shared with every other suite, so a test that leaves
    it inside its own `tmp_path` breaks an unrelated file later with no visible
    cause.
    """
    import os

    from flask import Flask

    from agent2 import database as db
    from agent2.core import workspace as ws
    from agent2.server import auth
    from agent2.server.routes import register_routes

    saved = {k: os.environ.get(k) for k in ("AGENT2_DENY_CAPS", "AGENT2_WEB_ROLE",
                                            "AGENT2_WEB_AUTH", "AGENT2_WEB_TOKEN")}
    for k in saved:
        os.environ.pop(k, None)
    db.init_db()
    try:
        original = str(ws.root())
    except Exception:
        original = ""
    ws.set_workspace(str(tree))
    auth.rate_reset()
    app = Flask(__name__)
    register_routes(app)
    try:
        with app.test_client() as c:
            yield c
    finally:
        if original:
            try:
                ws.set_workspace(original)
            except Exception:
                pass
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        auth.rate_reset()


def test_the_web_surface_calls_the_same_two_modules_the_cli_does():
    """⚠️ The one-declaration pin for the browser half.

    `projectscan` decides what is true and `projectdoc` writes it. A view that
    walked the tree itself, or built the markdown itself, would be a second
    declaration of what a project IS — and the CLI and the browser would commit
    different facts to the same file on alternating runs.
    """
    read = _view_calls("api_get_project")
    assert "projectscan.scan" in read
    assert "projectdoc.parse" in read, "ownership comes from the parser, not a regex"
    assert "projectdoc.prior_hint" in read
    init = _view_calls("api_project_init")
    assert "projectscan.scan" in init
    assert "projectdoc.apply" in init
    assert not ({"mkdir", "write_text", "walk", "os.walk", "render"} & init), \
        "the route may not write, walk or render — it calls the one writer"


def test_the_route_needs_fs_write_while_the_read_needs_only_read():
    """⚠️ `fs.write`, not `settings` and not `destructive`.

    The request creates a file inside the workspace, so the switch that stops every
    other write — `AGENT2_DENY_CAPS=fs.write` — has to stop this one too. Rated
    `settings` it would survive exactly the lockdown a user set up to prevent it.
    """
    from agent2.core import permissions as perms
    assert perms.capability_for("POST", "/api/project/init") == perms.CAP_FS_WRITE
    assert perms.capability_for("POST", "/api/project") == perms.CAP_FS_WRITE
    assert perms.capability_for("GET", "/api/project") == perms.CAP_READ


def test_the_web_read_reports_the_scan_and_creates_nothing(web, tree):
    """A panel must be able to show what `/init` *would* do before doing it."""
    body = web.get("/api/project").get_json()
    assert body["scan"]["name"] == tree.name
    assert body["scan"]["primary_language"] == "Python"
    assert body["doc"]["exists"] is False
    assert body["doc"]["path"].endswith("agent2.md")
    assert not (Path(body["scan"]["root"]) / ".agent2").exists()


def test_the_web_read_names_the_sections_but_never_their_text(web):
    """⚠️ HEADINGS AND OWNERSHIP, NEVER THE PROSE.

    `## Agent2 Instructions` is the user's, and `/api/health`'s rule applies here
    too: an endpoint that returns the body turns a project doc into one more place
    a stray reader finds text its author expected to live only in their checkout.
    """
    made = web.post("/api/project/init", json={"describe": False}).get_json()
    doc = Path(made["result"]["doc"])
    doc.write_text(
        doc.read_text(encoding="utf-8")
        + "\n## Local Notes\n\nThe staging box answers as bastion-7.\n",
        encoding="utf-8")

    resp = web.get("/api/project")
    body = resp.get_json()
    assert body["doc"]["exists"] is True
    assert "Local Notes" in body["doc"]["sections"]
    assert "Local Notes" in body["doc"]["preserved"]
    assert body["doc"]["generated"], "the generated sections are still reported"
    assert body["doc"]["bytes"] > 0
    assert "bastion-7" not in resp.get_data(as_text=True)


def test_a_web_dry_run_reports_the_merge_and_touches_nothing(web):
    """`write: false` is the panel's preview — the whole merge, no filesystem."""
    body = web.post("/api/project/init",
                    json={"write": False, "describe": False}).get_json()
    res = body["result"]
    assert res["ok"] is True
    assert res["reason"] == "dry run"
    assert res["changed"] is True and res["created"] is True
    assert res["added"], "a dry run still reports which sections it would add"
    assert not Path(res["dir"]).exists()


def test_the_web_write_creates_the_doc_and_the_rerun_preserves_a_human_section(web):
    """Task 31 over HTTP: the second run must not eat what a human wrote."""
    first = web.post("/api/project/init",
                     json={"hint": "this is a project of calculator",
                           "describe": False}).get_json()["result"]
    assert first["ok"] is True and first["created"] is True
    doc = Path(first["doc"])
    assert doc.is_file()
    assert ".agent2/skills" in " ".join(first["dirs_created"]).replace("\\", "/")

    mine = "Never touch the vendored tarballs."
    text = doc.read_text(encoding="utf-8")
    doc.write_text(text + f"\n## Local Notes\n\n{mine}\n", encoding="utf-8")

    second = web.post("/api/project/init", json={"describe": False}).get_json()["result"]
    assert second["ok"] is True and second["created"] is False
    assert "Local Notes" in second["preserved"]
    after = doc.read_text(encoding="utf-8")
    assert mine in after
    # ⚠️ The hint given the first time is recovered from the file, not re-typed:
    # `/init` a year later still knows what the project is.
    assert second["hint"] == "this is a project of calculator"
    assert projectdoc.prior_hint(after) == "this is a project of calculator"


def test_denying_fs_write_stops_the_browser_before_anything_is_written(web,
                                                                      monkeypatch):
    """⚠️ Both surfaces, one switch.

    The shared `before_request` gate refuses the POST because the route is rated
    `fs.write`; `projectdoc.apply()` then asks *again*, live, so a caller that
    reached the writer another way is refused there too. The read still answers —
    a deployment that can show nothing is indistinguishable from a broken one.
    """
    monkeypatch.setenv("AGENT2_DENY_CAPS", "fs.write")
    assert web.post("/api/project/init",
                    json={"describe": False}).status_code == 403
    body = web.get("/api/project").get_json()
    assert body["doc"]["exists"] is False
    assert not (Path(body["scan"]["root"]) / ".agent2").exists()


def test_the_web_routes_are_total_on_a_workspace_with_nothing_in_it(web, tmp_path):
    """Presentation and transport may never raise: an empty directory answers 200."""
    from agent2.core import workspace as ws
    empty = tmp_path / "hollow"
    empty.mkdir()
    ws.set_workspace(str(empty))
    read = web.get("/api/project")
    assert read.status_code == 200
    assert read.get_json()["doc"]["exists"] is False
    wrote = web.post("/api/project/init", json={"describe": False})
    assert wrote.status_code == 200
    assert wrote.get_json()["result"]["ok"] is True


# ══════════════════════════════════════════════════════════════════════════════
# 7. What the NEXT turn actually sees — the reason `/init` writes a file at all
# ══════════════════════════════════════════════════════════════════════════════
def test_what_init_writes_is_in_the_very_next_prompt(rep, tree):
    """⚠️ THE WHOLE POINT OF THE COMMAND, and the one thing no other test asserts.

    `.agent2/agent2.md` is to Agent2 what `CLAUDE.md` is to Claude Code: the file
    the agent reads to know the architecture *before it does anything*. Writing a
    correct document that no turn ever reads would satisfy Tasks 29–31 and deliver
    nothing — and it is a silent failure, because both halves keep working:
    `/init` reports a write, the model simply never learns the project.

    So this closes the loop across three modules: `projectscan` finds the facts,
    `projectdoc` commits them, and `broker`'s project source inlines them into the
    prompt tail every surface sends. The framing sentence is asserted too — the doc
    is *instructions*, and a block the model is not told to prefer is just prose.
    """
    from agent2.core import broker
    from agent2.core import workspace as ws

    projectdoc.apply(rep, hint="a calculator for the terminal",
                     write=True, describe=False)
    original = str(ws.root())
    ws.set_workspace(str(tree))
    try:
        tail = broker.assemble(chat_id="c-init",
                               message="where does the code live?").prompt_tail()
    finally:
        if original:
            ws.set_workspace(original)

    assert f"## PROJECT INSTRUCTIONS ({broker.PRIMARY_DOC})" in tail
    assert "Follow them over your general defaults" in tail
    # The architecture the scan proved, now readable by the model.
    assert "## Project Structure" in tail
    assert "src/calc" in tail.replace("\\", "/")
    assert "a calculator for the terminal" in tail
    # And the section a human owns travels with it.
    assert "## Agent2 Instructions" in tail


def test_one_projects_doc_never_leaks_into_another(rep, tree, tmp_path):
    """⚠️ Task 30: *per-project, never global*, and the read half proves it.

    The doc is found through `workspace.root()` on every collect rather than
    latched at startup, so switching checkouts switches instructions. A cached
    root would carry one project's architecture — and its standing orders — into
    the next one's turns, which is the same class of bug `mcp_state`'s
    read-through `enabled` exists to prevent.
    """
    from agent2.core import broker
    from agent2.core import workspace as ws

    projectdoc.apply(rep, hint="a calculator for the terminal",
                     write=True, describe=False)
    other = tmp_path / "elsewhere"
    other.mkdir()
    original = str(ws.root())
    try:
        ws.set_workspace(str(other))
        tail = broker.assemble(chat_id="c-other").prompt_tail()
        assert "a calculator for the terminal" not in tail
        assert "PROJECT INSTRUCTIONS" not in tail
        ws.set_workspace(str(tree))
        assert "a calculator for the terminal" in broker.assemble(
            chat_id="c-back").prompt_tail()
    finally:
        if original:
            ws.set_workspace(original)


# ══════════════════════════════════════════════════════════════════════════════
# 7b. The doc is BIGGER than the prompt slot — which half a turn gets (D3)
# ══════════════════════════════════════════════════════════════════════════════
# `broker.PROJECT_DOC_CHARS` owns how much of the doc a prompt may spend;
# `projectdoc.for_prompt()` owns WHICH part, because ownership is this module's fact.
# The clip it replaced was `body[:limit]` — head-first, and the sections a human owns
# are seeded at the END of the file, so the very first thing the prompt discarded was
# the user's own standing orders. Silently, and only once the doc grew useful.
def _pad(heading: str, chars: int) -> str:
    """A generated block of roughly `chars`, so a limit can be made to bite."""
    return f"## {heading}\n{projectdoc.MARKER}\n\n" + ("padding words here. " * (chars // 20)) + "\n\n"


def test_a_human_section_survives_a_doc_too_big_for_the_prompt(rep, tree, monkeypatch):
    """⚠️ THE ACCEPTANCE BAR: a human's section outranks every generated one, and a
    budget is where that claim is actually tested. The contrast is asserted, not
    described — the same limit applied head-first loses a section this clip keeps."""
    from agent2.core import broker
    from agent2.core import workspace as ws

    projectdoc.apply(rep, hint="a calculator for the terminal", write=True,
                     describe=False)
    text = _doc(tree)
    limit = 1_500
    assert len(text) > limit, "the fixture: the doc really does overflow"
    owned = sorted(projectdoc.HUMAN_SECTIONS)
    lost_to_a_head_clip = [h for h in owned if f"## {h}" not in text[:limit]]
    assert lost_to_a_head_clip, "the fixture: `body[:limit]` really did lose sections"

    monkeypatch.setattr(broker, "PROJECT_DOC_CHARS", limit)
    original = str(ws.root())
    ws.set_workspace(str(tree))
    try:
        bundle = broker.assemble(chat_id="c-clip", message="what do I run?")
    finally:
        if original:
            ws.set_workspace(original)
    tail = bundle.prompt_tail()

    for heading in owned:
        assert f"## {heading}" in tail, f"{heading} is the user's and must travel"
    item = next(i for i in bundle.items if i.source == "project")
    assert item.meta["primary_clipped"] is True
    assert "truncated" in item.text


def test_the_prompt_names_the_sections_it_left_out(rep, tree, monkeypatch):
    """⚠️ NAMED, NEVER COUNTED. "3 sections omitted" tells a model nothing it can
    act on; the heading tells it what it does not know, and `agent2.md` is a file it
    can read the rest of."""
    from agent2.core import broker
    from agent2.core import workspace as ws

    projectdoc.apply(rep, write=True, describe=False)
    monkeypatch.setattr(broker, "PROJECT_DOC_CHARS", 1_500)
    original = str(ws.root())
    ws.set_workspace(str(tree))
    try:
        bundle = broker.assemble(chat_id="c-named")
    finally:
        if original:
            ws.set_workspace(original)

    item = next(i for i in bundle.items if i.source == "project")
    dropped = item.meta["primary_dropped"]
    assert dropped, "something was left out"
    assert item.meta["primary_clipped"] is True
    for heading in dropped:
        assert heading in item.text, "the notice names it"
    assert broker.PRIMARY_DOC in item.text, "and says where the rest is"


def test_the_clip_surrenders_the_tree_before_the_test_command(rep, tree):
    """⚠️ SURVIVAL ORDER IS NOT PRINT ORDER — `sources.ORDER` vs `sources.PRIORITY`,
    applied inside one document. A directory tree is one `list_dir` away; the command
    that runs this project's tests is not guessable, and "run the tests before
    reporting done" is worthless without it."""
    projectdoc.apply(rep, write=True, describe=False)
    text = _doc(tree)
    both = ("## Project Structure" in text) and ("## Test Commands" in text)
    assert both, "the fixture has both sections"

    # A limit that cannot hold everything: whatever survives, the tree goes first.
    out, info = projectdoc.for_prompt(text, int(len(text) * 0.75))
    assert info["clipped"] is True
    assert "Project Structure" in info["dropped"]
    assert "Test Commands" in info["kept"]
    assert "## Test Commands" in out


def test_only_a_generated_section_is_ever_surrendered(rep, tree):
    """The rule as a property, not as a sample: at every limit from "almost none" to
    "almost all", nothing unmarked is ever dropped."""
    projectdoc.apply(rep, write=True, describe=False)
    text = _doc(tree)
    _pre, blocks = projectdoc.parse(text)
    owned = {b.heading for b in blocks if not b.generated}
    assert owned, "the seeded human sections are unmarked"

    for frac in (0.05, 0.25, 0.5, 0.9):
        out, info = projectdoc.for_prompt(text, int(len(text) * frac))
        assert not (owned & set(info["dropped"])), f"a human section went at {frac}"
        for heading in owned:
            assert f"## {heading}" in out, f"{heading} missing at {frac}"


def test_an_over_budget_clip_is_reported_rather_than_fixed(rep, tree):
    """⚠️ `budget.py`'s rule about pinned items, for its reason. If the sections a
    human owns exceed the slot on their own they are kept anyway and `over` says so:
    an honestly too-large prompt fails at the vendor with something an operator can
    read, and a quiet trim of the user's rules is the unrecoverable half."""
    projectdoc.apply(rep, write=True, describe=False)
    text = _doc(tree)
    out, info = projectdoc.for_prompt(text, 200)
    assert info["over"] is True
    assert info["dropped"], "every generated section was surrendered first"
    assert "## Agent2 Instructions" in out
    assert len(out) > 200, "kept anyway, and the caller is told"


def test_a_doc_with_no_headings_falls_back_to_a_head_clip(rep, tree):
    """⚠️ Fails OPEN to the behaviour it replaced. A user may keep the whole file as
    unsectioned prose, and an accounting helper that raised would cost the turn the
    entire project source — the one thing `/init` exists to put in the prompt."""
    out, info = projectdoc.for_prompt("just prose, no headings at all. " * 200, 300)
    assert info["clipped"] is True
    assert info["sections"] == 0
    assert len(out) <= 300
    assert out, "something still reaches the prompt"


def test_a_clip_that_fits_is_not_a_clip(rep, tree):
    projectdoc.apply(rep, write=True, describe=False)
    text = _doc(tree)
    out, info = projectdoc.for_prompt(text, len(text) + 10)
    assert info["clipped"] is False and info["dropped"] == []
    assert out == text.strip()


def test_a_rank_zero_section_missing_from_a_doc_comes_back_first(rep, tree):
    """⚠️ THE `insert_at` TRAP, made visible. It started its search at `len(blocks)`,
    so a canonical **first** section absent from an existing doc was appended LAST.
    Invisible while `## Overview` held rank 0 (nothing precedes it either way); live
    the moment D3 put a navigation section above it — the file would open on a
    directory tree and end with "read this file before you act"."""
    projectdoc.apply(rep, write=True, describe=False)
    doc = tree / ".agent2" / "agent2.md"
    text = doc.read_text(encoding="utf-8")
    first = projectdoc.SECTIONS[0]
    head, rest = text.split(f"\n## {first}\n", 1)
    doc.write_text(head + "\n## " + rest.split("\n## ", 1)[1], encoding="utf-8")
    assert first not in _sections(doc.read_text(encoding="utf-8"))

    res = projectdoc.apply(rep, write=True, describe=False)
    assert first in res["added"]
    assert _sections(doc.read_text(encoding="utf-8"))[0] == first


def test_the_navigation_section_leads_the_file(rep, tree):
    """A brief nobody reads first is not a brief. It is `SECTIONS[0]`, and the model
    meets it before any fact it is meant to route."""
    projectdoc.apply(rep, write=True, describe=False)
    order = _sections(_doc(tree))
    assert order[0] == "How To Work Here"
    assert order.index("How To Work Here") < order.index("Project Structure")


def test_a_pipe_in_a_projects_own_prose_cannot_break_a_table(tmp_path):
    """⚠️ ESCAPED, NOT DROPPED. The notes and rows are a human's words; a `|` inside
    one would silently split a cell and shift every column after it, so the doc would
    misattribute the file's own description. Stripping it instead would quietly edit
    what the author wrote."""
    assert projectdoc._cell("a | b") == "a \\| b"
    assert projectdoc._cell(None) == ""
    assert projectdoc._cell(" spread   over\nlines ") == "spread over lines"

    (tmp_path / "main.py").write_text(
        "# Routes a | b through the parser.\n\nif __name__ == '__main__':\n    pass\n",
        encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\n', encoding="utf-8")
    rep = projectscan.scan(str(tmp_path))
    projectdoc.apply(rep, write=True, describe=False)
    block = _doc(tmp_path).split("\n## Important Files\n", 1)[1].split("\n## ", 1)[0]
    row = next(ln for ln in block.splitlines() if "main.py" in ln)
    assert "a \\| b" in row
    assert row.count("|") - row.count("\\|") == 4, "still a three-column row"


def test_the_invariants_table_is_seeded_empty_not_with_an_example(rep, tree):
    """⚠️ A seeded example invariant would be indistinguishable from a real one — a
    rule this build invented, in a file the agent is told outranks its own defaults.
    So the table arrives with its headers and nothing in it."""
    projectdoc.apply(rep, write=True, describe=False)
    text = _doc(tree)
    block = text.split("\n## Invariants\n", 1)[1].split("\n## ", 1)[0]
    assert projectdoc.MARKER not in block, "the section is the human's from the start"
    assert "| Rule |" in block, "the shape is offered"
    # Only the rows BELOW a `|---|` separator are data; the header names the columns.
    # ⚠️ A separator has dashes — `|  |  |  |` is a blank DATA row and is the thing
    # being asserted, so folding the two would make this test vacuous.
    body, seen_rule = [], False
    for ln in block.splitlines():
        if not ln.startswith("|"):
            seen_rule = False
        elif "-" in ln and set(ln) <= set("|- "):
            seen_rule = True
        elif seen_rule:
            body.append(ln)
    assert body, "there is a row to fill in"
    for row in body:
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert not any(cells), f"a filled example row was seeded: {row}"



# The read half (section 7) makes the doc the model's brief. This is the write
# half: when Agent2 changes what the project CONTAINS, it refreshes the doc so the
# next turn is not briefed on a project that no longer exists.
#
# ⚠️ A TOOL, NOT A HIDDEN HOOK ON `write_file`. Firing a re-scan inside the file
# chokepoint would put a full project walk on every file the agent writes, hide a
# filesystem write from the transcript and the diff viewer, and need a fifth call
# site across four agent loops. As a tool it inherits `dispatch_tool`'s permission
# gate, its metrics and its audit line, and the user can see it happen.
@pytest.fixture()
def wsroot(tree: Path):
    """Point the global workspace at the fixture project, then put it back.

    Same discipline as the `web` fixture: `set_workspace` is persisted, global
    state, so a suite that leaves it inside its own `tmp_path` breaks an unrelated
    file later with no visible cause.
    """
    from agent2.core import workspace as ws
    try:
        original = str(ws.root())
    except Exception:
        original = ""
    ws.set_workspace(str(tree))
    try:
        yield tree
    finally:
        if original:
            try:
                ws.set_workspace(original)
            except Exception:
                pass


def _dispatch(name: str, args: dict) -> dict:
    from agent2.tools import ToolContext, dispatch_tool
    return dispatch_tool(name, args, ToolContext(sid="t-init", chat_id="c-init"))


def test_the_tool_is_advertised_dispatchable_and_rated_fs_write():
    """Three lists have to agree or the tool is a dead name in one surface.

    `_LOCAL_TOOLS` is dispatch (both web loops), `cli.tooling._SHARED_TOOLS` is
    dispatch in the terminal, and the two schema builders are what each model is
    TOLD exists. A name advertised but not dispatchable costs the turn; dispatchable
    but not advertised is a feature nothing can reach.
    """
    from agent2.agent import _LOCAL_TOOLS
    from agent2.cli.tooling import _SHARED_TOOLS, _build_tools as _cli_tools
    from agent2.core import permissions as perms
    from agent2.llm.providers import agent_tool_schema
    from agent2.tools import REGISTRY, _build_tools as _web_tools

    assert REGISTRY.has("update_project_doc")
    assert "update_project_doc" in _LOCAL_TOOLS
    assert "update_project_doc" in _SHARED_TOOLS
    assert any(f.name == "update_project_doc"
               for f in _web_tools().function_declarations)
    assert any(f.name == "update_project_doc"
               for f in _cli_tools().function_declarations)
    assert any(t["name"] == "update_project_doc" for t in agent_tool_schema()), \
        "custom providers dispatch it, so they must be told it exists"
    # ⚠️ `fs.write`, like `/api/project/init`: one switch stops every write.
    assert perms.capability_for_tool("update_project_doc") == perms.CAP_FS_WRITE


def test_the_tool_needs_no_arguments(wsroot):
    """The model is asked to call it as a habit at the end of a task, so anything
    it has to get right first is a call it will skip. `{}` must work."""
    out = _dispatch("update_project_doc", {})
    assert out.get("updated") is True, out
    assert Path(out["doc"]).is_file()
    assert out["created"] is True


def test_the_tool_writes_where_the_workspace_is_never_the_cwd(wsroot, tmp_path):
    """⚠️ The tool takes no path — it resolves `workspace.root()` on every call, so
    a `cd` inside the turn cannot make it write `.agent2/` somewhere else. This is
    the same defect the suite already caught once in `apply()` (a write into the CWD
    when the report had no root)."""
    out = _dispatch("update_project_doc", {})
    assert Path(out["doc"]).resolve().parent.parent == wsroot.resolve()
    assert not (Path.cwd() / ".agent2" / "agent2.md").exists() or \
        Path.cwd().resolve() == wsroot.resolve()


def test_the_tool_refreshes_the_facts_after_the_agent_changed_the_project(wsroot):
    """The whole reason it exists: Agent2 adds a module, then the doc names it."""
    _dispatch("update_project_doc", {})
    (wsroot / "src" / "calc" / "report.py").write_text(
        "def render():\n    return 'x'\n", encoding="utf-8")

    out = _dispatch("update_project_doc", {})
    assert out["created"] is False
    assert out["changed"] is True, "a new module is a change the doc must show"
    text = _doc(wsroot)
    assert "report.py" in text or "src/calc" in text.replace("\\", "/")


def test_the_tool_defaults_to_a_factual_refresh_and_asks_no_model(wsroot,
                                                                  monkeypatch):
    """⚠️ `describe` DEFAULTS TO FALSE. The agent calls this mid-task, possibly many
    times in a session; re-narrating "what is this project for" on each one would
    spend a model call per refresh and let the description drift. `describe=true` is
    the agent's explicit statement that the project's PURPOSE changed."""
    calls: list[str] = []

    def spy(rep, hint, existing, ask=None):
        calls.append(hint)
        return {"source": "model", "purpose": "spied", "features": [],
                "architecture": "", "note": ""}

    monkeypatch.setattr(projectdoc, "narrate", spy)
    assert _dispatch("update_project_doc", {}).get("updated") is True
    assert calls == [], "a default call must not reach the narrator"
    assert _dispatch("update_project_doc", {"describe": True}).get("updated") is True
    assert len(calls) == 1, "describe=true is the one shape that narrates"


def test_a_hint_from_the_agent_is_recorded_like_the_users_own(wsroot):
    assert _dispatch("update_project_doc",
                     {"hint": "a calculator for accountants"}).get("updated") is True
    assert "a calculator for accountants" in _doc(wsroot)
    assert projectdoc.prior_hint(_doc(wsroot)) == "a calculator for accountants"


def test_the_tool_preserves_a_human_section_exactly_as_init_does(wsroot):
    """Task 31 holds however the writer was reached. The agent calling this on its
    own is precisely the run a human is NOT watching, so it is the one that most
    needs the marker rule — and `## Agent2 Instructions` is where a user writes the
    standing orders this tool's own output sits next to."""
    _dispatch("update_project_doc", {})
    doc = wsroot / ".agent2" / "agent2.md"
    mine = "Never touch the vendored tarballs."
    doc.write_text(doc.read_text(encoding="utf-8")
                   + f"\n## Local Notes\n\n{mine}\n", encoding="utf-8")

    out = _dispatch("update_project_doc", {})
    assert "Local Notes" in out["preserved"]
    assert mine in doc.read_text(encoding="utf-8")


def test_the_tool_result_carries_no_document_text(wsroot):
    """⚠️ The result goes into the transcript and then back to the model as context.
    Counters and section HEADINGS only: shipping the body would re-send the whole
    doc through a channel the broker already budgets it in, and `## Agent2
    Instructions` / `## Local Notes` are the user's own text."""
    _dispatch("update_project_doc", {})
    doc = wsroot / ".agent2" / "agent2.md"
    doc.write_text(doc.read_text(encoding="utf-8")
                   + "\n## Local Notes\n\nThe staging box answers as bastion-7.\n",
                   encoding="utf-8")

    out = _dispatch("update_project_doc", {})
    blob = repr(out)
    assert "bastion-7" not in blob
    assert "A calculator for the terminal." not in blob, "no README prose either"
    assert "Local Notes" in out["preserved"], "the HEADING is still reported"


def test_denying_fs_write_stops_the_tool_and_reads_as_an_error(wsroot, monkeypatch):
    """⚠️ One switch, every surface — and a REFUSAL, not an exception. The model
    reads tool errors and adapts; an exception would end the turn over a
    configuration choice the user made on purpose."""
    monkeypatch.setenv("AGENT2_DENY_CAPS", "fs.write")
    out = _dispatch("update_project_doc", {})
    assert "error" in out
    assert not (wsroot / ".agent2" / "agent2.md").exists()


def test_the_tool_is_total_on_a_workspace_it_cannot_analyse(tmp_path, monkeypatch):
    """Nothing here may raise into a turn: an unreadable root is a returned error."""
    from agent2.core import workspace as ws
    monkeypatch.setattr(ws, "root", lambda: tmp_path / "gone" / "missing")
    out = _dispatch("update_project_doc", {})
    assert "error" in out and "updated" not in out


def test_the_tool_reports_a_no_op_without_claiming_a_write(wsroot):
    """⚠️ `changed=False` must be visible in the summary the model reads. Told
    "updated" on a run that wrote nothing, the agent learns the call is free and
    starts making it on every turn — which is how the doc stops being read."""
    from agent2.agent import _tool_result_summary
    _dispatch("update_project_doc", {})
    out = _dispatch("update_project_doc", {})
    assert out["changed"] is False
    assert "unchanged" in _tool_result_summary("update_project_doc", out)


def test_the_agents_own_refresh_is_what_the_next_turn_reads(wsroot):
    """The loop closed from the write side: the agent refreshes the doc, and the
    very next prompt carries what it wrote. This is the user's request in one
    assertion — Agent2 adds a feature, the doc learns it, the model is briefed."""
    from agent2.core import broker

    _dispatch("update_project_doc", {"hint": "a calculator for the terminal"})
    (wsroot / "src" / "calc" / "graphs.py").write_text(
        "def plot():\n    return None\n", encoding="utf-8")
    assert _dispatch("update_project_doc", {})["changed"] is True

    tail = broker.assemble(chat_id="c-tool", message="what does this do?").prompt_tail()
    assert f"## PROJECT INSTRUCTIONS ({broker.PRIMARY_DOC})" in tail
    assert "a calculator for the terminal" in tail
