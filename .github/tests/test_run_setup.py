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

These are drift guards, not behaviour tests: they load `run.py` as a module
WITHOUT calling `main()`, so importing it must stay free of side effects.
"""

import ast
import importlib.util
import pathlib
import sys

import pytest

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
