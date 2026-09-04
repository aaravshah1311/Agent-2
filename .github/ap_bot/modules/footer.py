# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Footer Module.

Provides utility functions for appending the standard bot disclaimer
footer to any comment body.
"""

from __future__ import annotations

from ..config import config


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_footer() -> str:
    """Return the standard bot disclaimer footer.

    Returns:
        The configured ``BOT_FOOTER`` string.
    """
    return config.BOT_FOOTER


def format_response(body: str) -> str:
    """Append the bot footer to a comment body.

    Ensures a blank-line separator between the body content and the
    footer for proper Markdown rendering.

    Args:
        body: The main comment body text.

    Returns:
        The body with the bot footer appended.
    """
    return f"{body}\n\n{config.BOT_FOOTER}"
