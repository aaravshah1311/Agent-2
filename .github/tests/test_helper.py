# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for ap_bot.modules.helper pure-logic utilities."""

from datetime import datetime, timedelta, timezone

from ap_bot.modules import helper


def test_truncate_text_leaves_short_text_untouched():
    assert helper.truncate_text("hello", max_length=100) == "hello"


def test_truncate_text_truncates_long_text():
    result = helper.truncate_text("x" * 50, max_length=10)
    assert result.startswith("x" * 10)
    assert "truncated" in result


def test_parse_labels_filters_unknown_and_dedupes():
    result = helper.parse_labels_from_response("bug, bug, not-a-label, feature")
    assert result == ["bug", "feature"]


def test_parse_labels_handles_newlines_and_empty():
    assert helper.parse_labels_from_response("") == []
    assert helper.parse_labels_from_response("bug\nsecurity") == ["bug", "security"]


def test_format_markdown_list():
    assert helper.format_markdown_list(["a", "b"]) == "- a\n- b"


def test_is_valid_issue_or_pr():
    assert helper.is_valid_issue_or_pr({"number": 1, "title": "x"}) is True
    assert helper.is_valid_issue_or_pr({"number": 1}) is False
    assert helper.is_valid_issue_or_pr({}) is False


def test_days_since_recent_date():
    two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
    stamp = two_days_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert helper.days_since(stamp) == 2


def test_days_since_invalid_returns_zero():
    assert helper.days_since("not-a-date") == 0
