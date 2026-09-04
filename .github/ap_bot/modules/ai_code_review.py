# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — AI Code Review Module.

Uses Gemini AI to review pull request diffs for code quality, readability,
best practices, potential bugs, maintainability, documentation gaps, and
actionable suggestions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..config import config
from ..logger import logger

if TYPE_CHECKING:
    from ..gemini import GeminiClient
    from ..github_api import GitHubAPI

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_DIFF_LENGTH: int = 10_000

_CODE_REVIEW_PROMPT: str = (
    "You are an expert code reviewer for an open-source project.\n\n"
    "Review the following pull request diff and provide feedback on:\n\n"
    "1. **Code Quality** — Are there any anti-patterns or code smells?\n"
    "2. **Readability** — Is the code clear and well-structured?\n"
    "3. **Best Practices** — Does it follow language conventions and "
    "project standards?\n"
    "4. **Potential Bugs** — Are there any logic errors, edge cases, "
    "or runtime issues?\n"
    "5. **Maintainability** — Will this be easy to maintain and extend?\n"
    "6. **Missing Documentation** — Are there missing docstrings, "
    "comments, or type hints?\n"
    "7. **Suggestions** — Provide concrete, actionable improvements.\n\n"
    "Format your review using Markdown with headers for each section. "
    "Be constructive, specific, and reference line numbers or file names "
    "where possible.\n\n"
    "PR Title: {title}\n\n"
    "Diff:\n```diff\n{diff}\n```\n\n"
    "Review:"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(github_api: GitHubAPI, gemini_client: GeminiClient) -> Optional[str]:
    """Perform an AI-powered code review on a pull request.

    Fetches the PR diff, sends it to Gemini for analysis, and posts
    the resulting review as a PR comment.  Large diffs are truncated to
    ``_MAX_DIFF_LENGTH`` characters to stay within model context limits.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client for generating the review.

    Returns:
        The review text that was posted, or ``None`` on failure.
    """
    pr_number: Optional[int] = config.PR_NUMBER
    if not pr_number:
        logger.error("No PR_NUMBER found in config. Aborting AI code review.")
        return None

    try:
        # -- Fetch PR metadata and diff --------------------------------------
        pr = github_api.get_pr(pr_number)
        if not pr:
            logger.error(
                f"Could not fetch PR #{pr_number}. Aborting AI code review."
            )
            return None
        title: str = pr.get("title", "")
        diff: Optional[str] = github_api.get_pr_diff(pr_number)

        if not diff:
            logger.info(f"PR #{pr_number} has an empty diff. Skipping code review.")
            return None

        # -- Truncate diff if necessary --------------------------------------
        truncated = False
        original_length = len(diff)
        if original_length > _MAX_DIFF_LENGTH:
            diff = diff[:_MAX_DIFF_LENGTH]
            truncated = True
            logger.info(
                f"Diff for PR #{pr_number} truncated from {original_length:,} "
                f"to {_MAX_DIFF_LENGTH:,} characters."
            )

        logger.info(f"Running AI code review on PR #{pr_number}: {title!r}")

        # -- Ask Gemini to review --------------------------------------------
        prompt = _CODE_REVIEW_PROMPT.format(title=title, diff=diff)
        review: str = gemini_client.generate(prompt).strip()

        if not review:
            logger.warning(f"Gemini returned an empty review for PR #{pr_number}.")
            return None

        # -- Build and post comment ------------------------------------------
        truncation_notice = ""
        if truncated:
            truncation_notice = (
                "\n\n> ⚠️ _The diff was truncated due to size. "
                "This review covers only the first "
                f"{_MAX_DIFF_LENGTH:,} characters of the diff._\n"
            )

        comment_body = (
            f"🤖 **AI Code Review**\n\n"
            f"{review}"
            f"{truncation_notice}\n\n{config.BOT_FOOTER}"
        )
        github_api.add_comment(pr_number, comment_body)
        logger.info(f"Posted AI code review comment on PR #{pr_number}.")

        return review

    except Exception:
        logger.exception(f"Failed to run AI code review for PR #{pr_number}.")
        raise
