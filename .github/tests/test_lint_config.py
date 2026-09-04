# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the tooling config in pyproject.toml.

`ruff check agent2` is an ENFORCED CI gate (.github/workflows/lint.yml has no
continue-on-error on it). That only stays meaningful if two things hold:

  1. The config parses and the enabled rule set is actually broad — a config
     that silently narrowed to a handful of rules would keep passing CI while
     checking nothing.
  2. The graceful-degradation allowances stay *scoped*. Agent2's failsafe
     design is `except Exception: -> degrade`, which BLE001/S110 flag. Those
     are allowed per-file for the modules that genuinely need it, NOT globally,
     so new code in other modules is still told to narrow its except clause.

The failsafe rule is the reason SIM105 (contextlib.suppress) is off codebase
wide: suppress() is ~28x slower than bare try/except/pass (measured over 2M
iterations), and Agent2's suppressed blocks sit in the DB connection pool and
the PIL learning path. See the design notes in pyproject.toml.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"


try:                                  # 3.11+ stdlib
    import tomllib
except ModuleNotFoundError:           # pragma: no cover
    tomllib = None


def _ruff_bin() -> str:
    """Path to ruff, preferring the one in THIS interpreter's environment.

    A ruff on PATH may be a different version with a different rule registry,
    which would make these tests report on a linter CI never runs.
    """
    for cand in (Path(sys.prefix) / "Scripts" / "ruff.exe",
                 Path(sys.prefix) / "bin" / "ruff"):
        if cand.exists():
            return str(cand)
    found = shutil.which("ruff")
    if found is None:                 # pragma: no cover - guarded by skipif
        pytest.skip("ruff not installed in this environment")
    return found


@pytest.fixture(scope="module")
def cfg():
    if tomllib is None:
        pytest.skip("tomllib unavailable (needs Python 3.11+)")
    if not PYPROJECT.exists():
        pytest.fail("pyproject.toml is missing — the ruff CI gate has no config")
    with open(PYPROJECT, "rb") as fh:
        return tomllib.load(fh)


def test_pyproject_parses_and_configures_ruff(cfg):
    assert "tool" in cfg
    assert "ruff" in cfg["tool"], "no [tool.ruff] section"
    assert "lint" in cfg["tool"]["ruff"], "no [tool.ruff.lint] section"


def test_enabled_rule_set_is_broad(cfg):
    """A narrowed `select` would keep CI green while checking almost nothing."""
    select = cfg["tool"]["ruff"]["lint"]["select"]
    # The categories that catch real defects here: pyflakes (unused/undefined),
    # bugbear (late-binding closures, mutable defaults), and perf.
    for essential in ("F", "E", "B", "PERF", "RUF"):
        assert essential in select, f"ruff rule family {essential!r} must stay enabled"
    assert len(select) >= 10, f"rule set looks narrowed: {select}"


# The forms ruff accepts to enable each failsafe rule: the exact code, its
# linter prefix, or ALL. Deliberately an explicit set rather than a
# `startswith` test — "B" (bugbear) is always in `select` and IS a string
# prefix of "BLE001", so a startswith check would pass even if "BLE" were
# deleted. That is the vacuous-guard trap this file exists to avoid.
_ENABLES = {
    "BLE001": {"BLE", "BLE001", "ALL"},
    "S110": {"S", "S110", "ALL"},
}


def test_failsafe_allowances_are_scoped_not_global(cfg):
    """BLE001/S110 must be ENFORCED, with per-file allowances — never global.

    The failsafe pattern (`except Exception: -> degrade`) is deliberate in
    specific modules, not a licence to swallow exceptions everywhere.

    This is asserted in BOTH directions so the test cannot go vacuous:
      * BLE001/S110 must be in `select` — if they were dropped from the rule
        set entirely, the per-file allowances below would document intent that
        nothing enforces, and a NEW module could swallow exceptions silently.
      * they must NOT be in the global `ignore` — that would stop ruff telling
        new code to narrow its except clause.
    """
    select = set(cfg["tool"]["ruff"]["lint"]["select"])
    for rule, accepted in _ENABLES.items():
        assert select & accepted, (
            f"{rule} is not selected (needs one of {sorted(accepted)}) — the "
            "per-file allowances are inert documentation. Enforce the rule, "
            "then allow it per-file."
        )

    global_ignore = cfg["tool"]["ruff"]["lint"].get("ignore", [])
    for rule in ("BLE001", "S110"):
        assert rule not in global_ignore, (
            f"{rule} is globally ignored — the failsafe allowance must stay "
            "per-file so new code is still checked"
        )

    per_file = cfg["tool"]["ruff"]["lint"]["per-file-ignores"]
    # The modules whose whole job is to degrade rather than raise.
    for path in ("agent2/database.py", "agent2/tools.py", "agent2/terminal.py"):
        assert path in per_file, f"{path} lost its documented failsafe allowance"
        assert "BLE001" in per_file[path]


@pytest.mark.skipif(shutil.which("ruff") is None
                    and not (Path(sys.prefix) / "Scripts" / "ruff.exe").exists()
                    and not (Path(sys.prefix) / "bin" / "ruff").exists(),
                    reason="ruff not installed in this environment")
def test_a_new_blind_except_is_actually_reported():
    """End-to-end proof the failsafe gate has teeth.

    Every other assertion in this file reads config. Config can be *shaped*
    right and still enforce nothing — that is exactly how BLE001 sat unselected
    while eleven per-file allowances "documented" it. So this test stops reading
    and asks ruff: writing a fresh blind except in a module that has NO
    allowance must produce BLE001 and S110.

    If this fails, new code can swallow exceptions silently no matter what the
    rest of this file claims.
    """
    probe = ROOT / "agent2" / "_lint_probe_tmp.py"
    probe.write_text(
        "def _probe():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:\n"
        "        pass\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [_ruff_bin(), "check", str(probe.relative_to(ROOT)),
             "--no-cache", "--output-format", "concise"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        for rule in ("BLE001", "S110"):
            assert rule in out, (
                f"{rule} did NOT fire on a brand-new blind except. The failsafe "
                f"gate is not enforcing anything.\nruff said:\n{out}"
            )
    finally:
        probe.unlink(missing_ok=True)


def test_suppress_rule_stays_off_for_performance(cfg):
    """SIM105 would trade real throughput for a cosmetic win. See module docstring."""
    assert "SIM105" in cfg["tool"]["ruff"]["lint"].get("ignore", []), (
        "SIM105 must stay ignored: contextlib.suppress is ~28x slower than "
        "try/except/pass and Agent2 suppresses in hot paths (DB pool, PIL)"
    )


def test_mypy_is_configured(cfg):
    assert "mypy" in cfg["tool"], "no [tool.mypy] section"
    # Third-party stubs are not vendored, so this must stay on or mypy drowns.
    assert cfg["tool"]["mypy"]["ignore_missing_imports"] is True


@pytest.mark.skipif(shutil.which("ruff") is None
                    and not (Path(sys.prefix) / "Scripts" / "ruff.exe").exists()
                    and not (Path(sys.prefix) / "bin" / "ruff").exists(),
                    reason="ruff not installed in this environment")
def test_ruff_check_is_clean():
    """The enforced gate must actually pass on agent2/.

    This is the test that would have caught the 435-finding baseline. If it
    fails, either fix the finding or add a *documented* allowance — do not
    widen the global ignore list.
    """
    proc = subprocess.run(
        [_ruff_bin(), "check", "agent2", "--no-cache", "--output-format", "concise"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        "ruff check agent2 failed — CI will fail too:\n"
        + (proc.stdout or "") + (proc.stderr or "")
    )
