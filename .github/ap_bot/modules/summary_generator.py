# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — AI Summary Generator Module.

Uses Gemini AI to generate concise summaries for issues and pull requests
whose body exceeds a configurable length threshold.
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

_MIN_BODY_LENGTH: int = 500

_SUMMARY_PROMPT: str = (
    "You are a concise technical writer for a GitHub repository.\n\n"
    "Summarize the following {item_type} in 2-3 clear, informative "
    "sentences. Focus on the key points, the problem or change being "
    "described, and any important context.\n\n"
    "Title: {title}\n\n"
    "Body:\n{body}\n\n"
    "Summary:"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(
    github_api: GitHubAPI,
    gemini_client: GeminiClient,
    issue_number: Optional[int] = None,
    pr_number: Optional[int] = None,
) -> Optional[str]:
    """Generate an AI summary for an issue or pull request.

    If the body is shorter than ``_MIN_BODY_LENGTH`` characters, no
    summary is generated.  Otherwise, Gemini produces a concise 2-3
    sentence summary that is posted as a comment and returned for use
    by other modules.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client for generating summaries.
        issue_number: Optional issue number override (falls back to config).
        pr_number: Optional PR number override (falls back to config).

    Returns:
        The generated summary text, or ``None`` if no summary was needed
        or generation failed.
    """
    issue_number = issue_number or config.ISSUE_NUMBER
    pr_number = pr_number or config.PR_NUMBER
    target_number: Optional[int] = pr_number or issue_number
    item_type: str = "pull request" if pr_number else "issue"

    if not target_number:
        logger.error(
            "No ISSUE_NUMBER or PR_NUMBER found in config. "
            "Aborting summary generation."
        )
        return None

    try:
        # -- Fetch content ---------------------------------------------------
        if pr_number:
            item = github_api.get_pr(pr_number)
        else:
            item = github_api.get_issue(issue_number)  # type: ignore[arg-type]

        if not item:
            logger.error(
                f"Could not fetch {item_type} #{target_number}. "
                f"Aborting summary generation."
            )
            return None

        title: str = item.get("title", "")
        body: str = item.get("body", "") or ""

        if len(body) < _MIN_BODY_LENGTH:
            logger.info(
                f"{item_type.title()} #{target_number} body is under "
                f"{_MIN_BODY_LENGTH} chars — skipping summary."
            )
            return None

        logger.info(f"Generating summary for {item_type} #{target_number}: {title!r}")

        # -- Ask Gemini to summarize -----------------------------------------
        prompt = _SUMMARY_PROMPT.format(
            item_type=item_type, title=title, body=body
        )
        summary: str = gemini_client.generate(prompt).strip()

        if not summary:
            logger.warning(
                f"Gemini returned an empty summary for {item_type} #{target_number}."
            )
            return None

        # -- Post summary comment --------------------------------------------
        comment_body = (
            f"📝 **AI Summary**\n\n"
            f"{summary}\n\n{config.BOT_FOOTER}"
        )
        github_api.add_comment(target_number, comment_body)
        logger.info(f"Posted summary comment on {item_type} #{target_number}.")

        return summary

    except Exception:
        logger.exception(
            f"Failed to generate summary for {item_type} #{target_number}."
        )
        raise
