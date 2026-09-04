# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for ap_bot.config environment handling and validation."""

from ap_bot import config


def test_env_int_parses_valid(monkeypatch):
    monkeypatch.setenv("AP_TEST_INT", "42")
    assert config._env_int("AP_TEST_INT") == 42


def test_env_int_rejects_non_numeric(monkeypatch):
    monkeypatch.setenv("AP_TEST_INT", "abc")
    assert config._env_int("AP_TEST_INT") is None


def test_env_int_missing_returns_none(monkeypatch):
    monkeypatch.delenv("AP_TEST_INT", raising=False)
    assert config._env_int("AP_TEST_INT") is None


def test_validate_required_vars_all_present(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("REPO_NAME", "owner/repo")
    assert config.validate_required_vars(["GITHUB_TOKEN", "REPO_NAME"]) is True


def test_validate_required_vars_missing(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert config.validate_required_vars(["GITHUB_TOKEN"]) is False


def test_labels_are_defined():
    assert "bug" in config.LABELS
    assert config.LABELS["bug"].startswith("#")
