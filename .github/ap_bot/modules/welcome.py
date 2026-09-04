# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Welcome Module.

Posts a warm welcome comment on newly opened issues and pull requests,
thanking contributors and informing them about AI-assisted review.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..config import config
from ..logger import logger

if TYPE_CHECKING:
    from ..gemini import GeminiClient
    from ..github_api import GitHubAPI

# ---------------------------------------------------------------------------
# Welcome message templates
# ---------------------------------------------------------------------------

_ISSUE_WELCOME: str = (
    "👋 **Welcome, @{author}!** Thank you for opening this issue.\n\n"
    "Our AI assistant will perform an initial review shortly, and a "
    "maintainer will follow up as soon as possible. In the meantime, "
    "please make sure you've provided:\n\n"
    "- A clear and descriptive title\n"
    "- Steps to reproduce (for bugs)\n"
    "- Expected vs. actual behavior\n"
    "- Any relevant logs or screenshots\n\n"
    "We appreciate your contribution to **Agent-2**! 🚀"
)

_PR_WELCOME: str = (
    "👋 **Welcome, @{author}!** Thank you for this pull request.\n\n"
    "Our AI assistant will review your changes shortly and provide "
    "automated feedback on code quality, security, and best practices. "
    "A maintainer will then conduct a final review.\n\n"
    "Please ensure:\n\n"
    "- Your branch is up to date with `main`\n"
    "- All tests pass locally\n"
    "- The PR description clearly explains the changes\n\n"
    "We're excited to review your contribution! 🎉"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(github_api: GitHubAPI, gemini_client: GeminiClient) -> None:
    """Post a welcome comment on a newly opened issue or pull request.

    Determines the event type from *config* and posts an appropriate
    welcome message.  The ``gemini_client`` parameter is accepted for
    interface consistency but is not used by this module.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client (unused — kept for interface parity).
    """
    issue_number: Optional[int] = config.ISSUE_NUMBER
    pr_number: Optional[int] = config.PR_NUMBER

    if pr_number:
        _welcome_pr(github_api, pr_number)
    elif issue_number:
        _welcome_issue(github_api, issue_number)
    else:
        logger.warning("Welcome module invoked but no issue or PR number found.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _welcome_issue(github_api: GitHubAPI, issue_number: int) -> None:
    """Post a welcome comment on an issue.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        issue_number: The issue number to comment on.
    """
    try:
        issue = github_api.get_issue(issue_number)
        if not issue:
            logger.error(
                f"Could not fetch issue #{issue_number}. Skipping welcome comment."
            )
            return
        author: str = issue.get("user", {}).get("login", "contributor")

        body = _ISSUE_WELCOME.format(author=author)
        body += f"\n\n{config.BOT_FOOTER}"

        github_api.add_comment(issue_number, body)
        logger.info(f"Posted welcome comment on issue #{issue_number} for @{author}.")
    except Exception:
        logger.exception(f"Failed to post welcome comment on issue #{issue_number}.")
        raise


def _welcome_pr(github_api: GitHubAPI, pr_number: int) -> None:
    """Post a welcome comment on a pull request.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        pr_number: The pull request number to comment on.
    """
    try:
        pr = github_api.get_pr(pr_number)
        if not pr:
            logger.error(
                f"Could not fetch PR #{pr_number}. Skipping welcome comment."
            )
            return
        author: str = pr.get("user", {}).get("login", "contributor")

        body = _PR_WELCOME.format(author=author)
        body += f"\n\n{config.BOT_FOOTER}"

        github_api.add_comment(pr_number, body)
        logger.info(f"Posted welcome comment on PR #{pr_number} for @{author}.")
    except Exception:
        logger.exception(f"Failed to post welcome comment on PR #{pr_number}.")
        raise
