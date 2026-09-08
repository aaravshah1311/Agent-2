# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Guards on run.py's dependency tables and the global `agent2` launcher.

⚠️ WHY THIS FILE EXISTS
────────────────────────
`run.py` is the installer: it decides what lands in the .venv and where the
global `agent2` command is written. Nothing imports it, so nothing checked it,
and two classes of silent failure had already accumulated:

1. **Libraries the app can reach but the installer never installs.**
   `agent2/fileintel/` resolves its backends through
   `base.require("<module>", "<pip name>")` — a STRING handed to
   `importlib.import_module`. Those names appear nowhere as an `import`
   statement, so every audit that greps for imports missed them. Five
   (`xlrd`, `odfpy`, `rarfile`, `docx2pdf`, `pdf2image`) were genuinely absent
   from `FILEINTEL_PACKAGES`: setup reported every package present while the
   features needing them reported "pip install X" forever.

2. **A launcher written where nothing looks** — see
   `test_the_launcher_lives_in_one_place_on_every_os`.

⚠️ It also owns the ONE fact `run.py` shares with `agent2/database.py`: the name
the legacy `.env` is retired to. `PRESERVE` lives here and decides what survives
`--update`, and there are **two** writers of that name — `run._migrate_env_once()`
and `database.migrate_env_keys()`, the latter running on every startup including
the direct-entry, Docker and `install.py` paths that never execute `run.py` at
all. Both are pinned here rather than one of them in a database test, because the
invariant is their *agreement* and `PRESERVE` is the third party to it; splitting
them would need a second copy of `_load_run_py()`, which is the drift this file
exists to catch.

These are drift guards, not behaviour tests: they load `run.py` as a module
WITHOUT calling `main()`, so importing it must stay free of side effects.
"""

import ast
import importlib.util
import pathlib
import sys

import pytest

from agent2 import config as a2config

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_run_py():
    """Import run.py by path. It is a script, never a package member."""
    spec = importlib.util.spec_from_file_location("_run_py_under_test", ROOT / "run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


run = _load_run_py()

# Libraries deliberately installed on SOME platforms only. `docx2pdf` drives
# Microsoft Word through COM, so installing it on Linux/macOS is pure noise and
# the DOCX→PDF path already falls back to LibreOffice/reportlab there. The gate
# is asserted (below) rather than merely tolerated, so it cannot quietly become
# "missing everywhere".
_PLATFORM_GATED = {"docx2pdf": "Windows"}


# ── Dependency completeness ────────────────────────────────────────────────────

def _require_call_sites() -> dict[str, str]:
    """Every `require("mod", "pip-name")` under agent2/fileintel/, as mod → pip.

    Read from the AST rather than executed: these calls sit inside operation
    bodies that need real files to run, and the whole point is to see the ones
    that never execute in a test run.
    """
    found: dict[str, str] = {}
    for path in sorted((ROOT / "agent2" / "fileintel").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name != "require":
                continue
            mod = node.args[0]
            if not isinstance(mod, ast.Constant) or not isinstance(mod.value, str):
                continue
            # require("PIL.Image", "Pillow") → the installable unit is `PIL`.
            top = mod.value.split(".")[0]
            pip = None
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                pip = node.args[1].value
            found.setdefault(top, pip or top)
    return found


def test_the_require_scan_actually_finds_call_sites():
    """⚠️ Tautology guard for the two tests below.

    If the AST walk silently matched nothing, `test_run_py_installs_every_
    fileintel_library` would pass by comparing an EMPTY set against the package
    table — green forever, checking nothing. That exact shape (a test comparing
    a collection to itself) already slipped through once in this suite and was
    only caught by sabotage, so the discovery half is asserted separately from
    the comparison half.
    """
    sites = _require_call_sites()
    assert len(sites) >= 12, f"the require() scan found only {sites} — it is broken"
    assert "pypdf" in sites, "the single-argument form is not being matched"
    assert sites.get("docx") == "python-docx", "the pip-name argument is being dropped"
    assert "PIL" in sites, "a dotted module is not being reduced to its top level"


def test_run_py_installs_every_fileintel_library():
    """Every library fileintel can require must be one run.py installs.

    A `require()` name missing from the package table is a feature that can
    NEVER work on a fresh install, however many times setup is re-run — and it
    surfaces as "pip install X yourself", which reads like a deliberate choice
    rather than a packaging bug.
    """
    installed = {imp for imp, _, _ in run._mode_packages("all")}
    missing = {mod: pip for mod, pip in _require_call_sites().items()
               if mod not in installed and mod not in _PLATFORM_GATED}
    assert not missing, (
        "agent2/fileintel/ can require these, but run.py never installs them: "
        + ", ".join(f"{m} (pip: {p})" for m, p in sorted(missing.items())))


def test_the_platform_gated_libraries_are_installed_on_their_platform():
    """A gate is only correct if the library IS installed where it belongs.

    Without this, deleting the `if IS_WIN:` append entirely would leave the
    test above green — the exemption would have quietly become "never
    installed anywhere", which is the original bug wearing a comment.
    """
    installed = {imp for imp, _, _ in run._mode_packages("all")}
    for mod, os_name in _PLATFORM_GATED.items():
        assert mod in _require_call_sites(), (
            f"{mod} is exempted as {os_name}-only but nothing requires it any "
            "more — drop the exemption")
        if os_name == run.OS_NAME:
            assert mod in installed, (
                f"{mod} is required by fileintel and gated to {os_name}, but "
                f"run.py does not install it on {os_name}")
        else:
            assert mod not in installed, (
                f"{mod} is {os_name}-only but run.py installs it on "
                f"{run.OS_NAME} — that is a pointless download for every user")


def test_run_py_installs_nothing_the_code_cannot_reach():
    """The reverse direction: no package in the tables that nothing uses.

    A dead entry costs every user a download and possibly a wheel build on
    first setup, and it silently outlives the feature that needed it.
    """
    # Imported directly (not via require), so they never appear in the scan.
    direct = {
        "google.genai", "mcp",                    # core
        "flask", "flask_socketio",                # web
        "rich", "prompt_toolkit",                 # CLI UI
        "PIL", "pypdf", "bs4", "charset_normalizer",  # imported in try/except
    }
    unused = {imp for imp, _, _ in run._mode_packages("all")}
    unused -= set(_require_call_sites()) | direct
    assert not unused, f"run.py installs packages nothing imports or requires: {unused}"


def test_every_fileintel_package_is_optional():
    """A failed fileintel wheel must WARN and continue, never abort setup.

    `install_deps()` exits(1) on a required package it cannot install. Every
    fileintel library is lazy and degrades to an install hint at runtime, so
    dropping one out of `OPTIONAL_IMPORTS` would turn a flaky wheel build —
    py7zr wanting a compiler, say — into a machine that cannot set Agent2 up
    at all. This is the failsafe half of the dependency work: adding packages
    must not add ways for setup to fail.
    """
    for imp, pip_name, _ in run.FILEINTEL_PACKAGES:
        assert imp in run.OPTIONAL_IMPORTS, (
            f"{imp} ({pip_name}) is not optional — a failed wheel would abort setup")


def test_the_fast_start_path_ignores_optional_packages():
    """`deps_present()` gates the fast start. An optional package counted as
    required means every launch where one cosmetic wheel is missing drops into
    the full, chatty reinstall — the exact loop the fast path exists to avoid,
    and it would now trigger on any of the four new libraries."""
    for mode in ("web", "cli", "all"):
        overlap = set(run._required_imports(mode)) & run.OPTIONAL_IMPORTS
        assert not overlap, f"mode {mode!r} treats optional packages as required: {overlap}"


def test_requirements_txt_lists_what_run_py_installs():
    """Docker builds from requirements.txt; `python run.py` builds from the
    tables above. When they disagree the container and the local venv are two
    different applications, and the difference only ever shows up as a feature
    that works on one of them."""
    listed = set()
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].split(";")[0].strip()
        if line:
            listed.add(line.lower().replace("_", "-"))
    for _imp, pip_name, label in run._mode_packages("all"):
        assert pip_name.lower().replace("_", "-") in listed, (
            f"{pip_name} ({label}) is installed by run.py but missing from "
            "requirements.txt — the Docker image would not have it")


# ── Global launcher ────────────────────────────────────────────────────────────

def test_the_launcher_lives_in_one_place_on_every_os():
    """⚠️ Windows used to write the wrapper to ~/.agent2/bin.

    Nothing else on a Windows box puts anything there, so the command was
    invisible both to a user looking for it and to tooling that scans the
    conventional per-user script directories. `~/.local/bin` is what pip and
    pipx already use on all three platforms, which also means the PATH entry is
    one many users already have.
    """
    assert run._global_bin_dir() == pathlib.Path.home() / ".local" / "bin"


def test_the_legacy_windows_dir_is_still_swept(monkeypatch):
    """Moving the launcher is only safe if the OLD copy is still found.

    A wrapper left in ~/.agent2/bin points at whatever run.py existed when it
    was written. If that directory sits on PATH ahead of ~/.local/bin, the
    stale copy wins and the freshly-installed command is never the one that
    runs — with no error, because the stale wrapper starts a real (old) Agent2.

    ⚠️ PATH IS CLEARED FIRST, AND THAT IS THE POINT. `_candidate_bin_dirs()`
    also appends every PATH entry, and on a machine with a pre-move install
    ~/.agent2/bin IS on PATH — so this assertion passed with the explicit entry
    deleted. Proved by sabotage: the break was MISSED until PATH was emptied.
    A fresh machine, which is exactly where a stale wrapper cannot be found by
    accident, is the case that must hold.
    """
    monkeypatch.setenv("PATH", "")
    dirs = {str(d).rstrip("\\/").lower() for d in run._candidate_bin_dirs()}
    legacy = str(pathlib.Path.home() / ".agent2" / "bin").rstrip("\\/").lower()
    assert legacy in dirs, "the pre-move Windows location is no longer swept"
    assert str(run._global_bin_dir()).rstrip("\\/").lower() in dirs, (
        "the primary location is not swept, so a stale wrapper there survives")


@pytest.mark.skipif(not run.IS_WIN, reason="wrapper filenames are per-platform")
def test_the_windows_wrapper_is_a_batch_file():
    """The Windows launcher is a .bat/.cmd — deliberately not a compiled .exe.

    `_refresh_stale_wrappers()` rewrites wrappers BY NAME, so a name added here
    without a matching writer in `install_global_command()` would be created
    once and then never updated again.
    """
    assert run._wrapper_names() == ["agent2.bat", "agent2.cmd"]


# ── Uninstall ──────────────────────────────────────────────────────────────────

def _uninstall_targets() -> list[pathlib.Path]:
    """The paths `uninstall()` would delete, without running it.

    `uninstall()` prompts and then removes trees, so it cannot be called; the
    list it builds is read out of its AST and evaluated against run.py's own
    module-level paths. `eval` here reads this repository's own source, in a
    namespace holding four `Path` objects and no builtins.
    """
    tree = ast.parse((ROOT / "run.py").read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "uninstall"), None)
    assert fn is not None, "run.py no longer defines uninstall()"
    ns = {"__builtins__": {}, "ROOT": run.ROOT, "VENV": run.VENV,
          "ENV_FILE": run.ENV_FILE, "DB_FILE": run.DB_FILE}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "to_delete" for t in node.targets):
            out: list[pathlib.Path] = []
            for elt in node.value.elts:                       # type: ignore[attr-defined]
                starred = isinstance(elt, ast.Starred)
                expr = elt.value if starred else elt
                # `ns` is the GLOBALS, not the locals: a generator expression runs
                # in its own function scope and sees globals only, so a starred
                # `*(... for s in ...)` raises NameError when these are passed as
                # locals — which is how the starred form silently went unchecked.
                val = eval(ast.unparse(expr), ns)
                out.extend(val) if starred else out.append(val)
            return out
    raise AssertionError("uninstall() no longer builds a `to_delete` list")


def test_uninstall_names_only_files_an_install_can_actually_create():
    """Every path uninstall wipes must be one this installer can produce.

    ⚠️ THE BUG THIS EXISTS FOR PRODUCED NO ERROR, AND THE TWO HALVES OF IT
    CANCELLED. `to_delete` read `ENV_FILE.with_suffix(".env.migrated")`, which
    looks like the obvious way to name the sibling file and yields
    `.env.env.migrated`: `.env` is all suffix and has no stem for `with_suffix` to
    replace. `_migrate_env_once()` had been written the same way — so uninstall was
    deleting the right file by accident, and correcting this list ALONE made it
    worse, naming a file the writer never created while leaving the one it did.
    Both names are wiped now, and the writer is pinned separately by
    `test_the_retired_env_file_is_named_the_thing_preserve_protects`.

    `PRESERVE` is the independent declaration this is checked against: it is
    what an update must keep, so it is also the complete list of state an
    install creates. Comparing the two lists in both directions is what makes
    this neither a tautology nor a restatement of the wipe list.
    """
    targets = _uninstall_targets()
    names = {p.name for p in targets}
    creatable = set(run.PRESERVE) | {run.VENV.name}

    unreal = sorted(names - creatable)
    assert not unreal, (
        "uninstall() names files no install can create, so they are never "
        f"deleted and the real ones survive: {unreal}")

    missed = sorted(set(run.PRESERVE) - names)
    assert not missed, (
        "PRESERVE names state an install creates that uninstall leaves behind: "
        f"{missed}")


def _rename_arg(source: pathlib.Path, fn_name: str, ns: dict) -> pathlib.Path:
    """The path `fn_name` in `source` renames the legacy .env to, without running it.

    Read out of the AST and EVALUATED against the module's own path constant,
    exactly as `_uninstall_targets()` is: both writers reach the rename only when a
    real `.env` is on disk, both swallow every exception, and the whole point of the
    bug is that the expression *looks* right — so evaluating it is the only way to
    see which name it actually produces.

    One walk for both writers on purpose. Two copies of this search would be the
    same duplication the file is guarding against, and the second copy is always
    the one that stops matching.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == fn_name), None)
    assert fn is not None, f"{source.name} no longer defines {fn_name}()"
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "rename" and node.args):
            return eval(ast.unparse(node.args[0]), {"__builtins__": {}, **ns})
    raise AssertionError(f"{fn_name}() no longer retires the legacy .env")


def _migrate_rename_target() -> pathlib.Path:
    """Where `run._migrate_env_once()` retires the legacy .env."""
    return _rename_arg(ROOT / "run.py", "_migrate_env_once",
                       {"ROOT": run.ROOT, "ENV_FILE": run.ENV_FILE})


def _database_rename_target() -> pathlib.Path:
    """Where `database.migrate_env_keys()` retires the legacy .env.

    `ENV` is a *local* import inside that function (`from agent2.config import ENV`,
    to avoid a cycle), so the namespace supplies it the same way the function would.
    """
    return _rename_arg(ROOT / "agent2" / "database.py", "migrate_env_keys",
                       {"ENV": a2config.ENV})


def test_the_retired_env_file_is_named_the_thing_preserve_protects():
    """⚠️ The WRITER's half of the dotfile trap, and the half that decides what is
    actually on disk.

    `_migrate_env_once()` imports a legacy `GEMINI_API_KEY` into agent2.db and then
    renames `.env` aside — and it renamed through
    `ENV_FILE.with_suffix(".env.migrated")`, which APPENDS on a dotfile and yields
    `.env.env.migrated`. Nothing raised. But `PRESERVE` names `.env.migrated`, and
    `self_update()`'s prune step deletes every top-level item `PRESERVE` does not
    name, so the retired file — still holding the key the user typed in — was
    removed by the next `python run.py --update` and reported only as a count of
    "stale item(s)". The state latches too: after the rename `.env` is gone, so the
    function returns at its existence guard forever and never gets a second chance.

    Asserted on the EVALUATED expression, not on the source text, so `with_suffix`
    cannot satisfy it; and asserted against `PRESERVE` as well as against the
    literal, so renaming the retired file later cannot silently escape protection.
    """
    target = _migrate_rename_target()

    assert target.name == ".env.migrated", (
        f"the legacy .env is retired as {target.name!r} — `with_suffix` appends on a "
        "dotfile, use `with_name`")
    assert target.parent == run.ROOT, (
        f"the retired .env lands outside the install at {target.parent}")
    assert target.name in run.PRESERVE, (
        f"{target.name!r} is not in PRESERVE, so self_update()'s prune step deletes "
        "the user's retired credential file")

    # Derived from pathlib's real behaviour rather than restated, so it doubles as a
    # canary: if a future Python ever made `with_suffix` REPLACE on a dotfile, the
    # first assertion here fails and this comment is what explains why.
    legacy = run.ENV_FILE.with_suffix(".env.migrated").name
    assert legacy != target.name, (
        "with_suffix no longer appends on a dotfile — re-read this test and "
        "run.py's two comments about it")
    assert legacy in run.PRESERVE, (
        f"an install that ran the old launcher holds its legacy key as {legacy!r}; "
        "dropping it from PRESERVE deletes that file on the next update")


def test_database_retires_the_env_to_the_same_name_run_py_does():
    """⚠️ THE SECOND WRITER, AND THE ONE THAT ACTUALLY RUNS ON MOST STARTUPS.

    `run.py` fixing its own copy of the dotfile bug is half a fix.
    `database.migrate_env_keys()` is called from `init_db()`, so it runs on **every**
    startup of every surface — including `.venv/bin/python agent2cli.py`, the Docker
    entrypoint and `install.py`, none of which execute `run.py` at all. On those
    paths it is the *only* migrator, and it carried the same
    `ENV.with_suffix(".env.migrated")` — which appends on a dotfile and produces
    `.env.env.migrated`.

    The consequence is not a cosmetic name. `self_update()`'s prune step deletes
    every top-level item `PRESERVE` does not name, so the retired file — still
    holding the key the user typed in — was removed by the next
    `python run.py --update` and reported as a count of "stale item(s)". And the
    state latches: the rename succeeds, `.env` is gone, so the function returns at
    its existence guard forever and the key is never recovered.

    Asserted against run.py's own writer rather than against a second literal, so
    the two can never disagree again: whichever one is edited, this fails. The
    literal is still asserted once, in the run.py test above.
    """
    from_run = _migrate_rename_target()
    from_db = _database_rename_target()

    assert from_db.name == from_run.name, (
        f"database.py retires the legacy .env as {from_db.name!r} while run.py uses "
        f"{from_run.name!r} — one of the two is `with_suffix` on a dotfile, and "
        "database.py is the writer that runs when run.py does not")
    assert from_db.name in run.PRESERVE, (
        f"{from_db.name!r} is not in PRESERVE, so `--update` deletes the credentials "
        "database.py just retired")
    assert from_db.parent.resolve() == run.ROOT, (
        f"database.py retires the legacy .env outside the install, to {from_db.parent}")

