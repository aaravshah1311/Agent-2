# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for ap_bot.modules.footer formatting helpers."""

from ap_bot.config import config
from ap_bot.modules import footer


def test_get_footer_returns_configured_footer():
    assert footer.get_footer() == config.BOT_FOOTER


def test_format_response_appends_footer():
    result = footer.format_response("Hello")
    assert result.startswith("Hello")
    assert result.endswith(config.BOT_FOOTER)
    assert "\n\n" in result
