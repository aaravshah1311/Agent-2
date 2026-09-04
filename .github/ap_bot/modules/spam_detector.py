# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Spam Detector Module.

Uses Gemini AI to evaluate whether an issue or pull request is spam,
promotional, nonsensical, or extremely low quality.  Flagged items
receive a ``possible-spam`` label and a warning comment — they are
**never** auto-closed.
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

_SPAM_PROMPT: str = (
    "You are a spam and quality detector for a GitHub repository.\n\n"
    "Evaluate the following issue/PR content and determine if it is:\n"
    "- Spam or promotional content\n"
    "- Nonsensical or gibberish text\n"
    "- Extremely low quality (e.g., single word, random characters)\n"
    "- Bot-generated junk\n\n"
    "Respond with EXACTLY one of the following on the first line:\n"
    "  SPAM: <brief reason>\n"
    "  NOT_SPAM\n\n"
    "Title: {title}\n\n"
    "Body:\n{body}\n\n"
    "Verdict:"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(github_api: GitHubAPI, gemini_client: GeminiClient) -> bool:
    """Evaluate an issue or PR for spam content.

    Sends the issue/PR content to Gemini for evaluation.  If flagged as
    spam, the ``possible-spam`` label is added and a warning comment is
    posted for maintainer review.  The item is **not** automatically closed.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client for spam evaluation.

    Returns:
        ``True`` if the content was flagged as spam, ``False`` otherwise.
    """
    issue_number: Optional[int] = config.ISSUE_NUMBER
    pr_number: Optional[int] = config.PR_NUMBER
    target_number: Optional[int] = pr_number or issue_number
    target_type: str = "PR" if pr_number else "issue"

    if not target_number:
        logger.error(
            "No ISSUE_NUMBER or PR_NUMBER found in config. "
            "Aborting spam detection."
        )
        return False

    try:
        # -- Fetch content ---------------------------------------------------
        if pr_number:
            item = github_api.get_pr(pr_number)
        else:
            item = github_api.get_issue(issue_number)  # type: ignore[arg-type]

        if not item:
            logger.error(
                f"Could not fetch {target_type} #{target_number}. "
                f"Aborting spam detection."
            )
            return False

        title: str = item.get("title", "")
        body: str = item.get("body", "") or ""

        logger.info(f"Evaluating {target_type} #{target_number} for spam: {title!r}")

        # -- Ask Gemini to evaluate ------------------------------------------
        prompt = _SPAM_PROMPT.format(title=title, body=body)
        response: str = gemini_client.generate(prompt)

        is_spam, reason = _parse_verdict(response)

        if is_spam:
            # -- Flag as spam ------------------------------------------------
            github_api.add_labels(target_number, ["possible-spam"])
            logger.warning(
                f"{target_type} #{target_number} flagged as spam: {reason}"
            )

            comment_body = (
                f"⚠️ **Possible Spam Detected**\n\n"
                f"This {target_type.lower()} has been automatically flagged "
                f"as potential spam by our AI assistant.\n\n"
                f"**Reason:** {reason}\n\n"
                f"_Maintainers, please review this {target_type.lower()} and "
                f"take appropriate action. This item has **not** been "
                f"automatically closed._\n\n{config.BOT_FOOTER}"
            )
            github_api.add_comment(target_number, comment_body)
            logger.info(
                f"Posted spam warning comment on {target_type} #{target_number}."
            )
            return True

        logger.info(f"{target_type} #{target_number} passed spam check.")
        return False

    except Exception:
        logger.exception(
            f"Failed to run spam detection for {target_type} #{target_number}."
        )
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_verdict(raw_response: str) -> tuple[bool, str]:
    """Parse the spam verdict from Gemini's response.

    Args:
        raw_response: The raw text returned by Gemini.

    Returns:
        A tuple of ``(is_spam, reason)``.
    """
    first_line = raw_response.strip().splitlines()[0].strip() if raw_response else ""

    if first_line.upper().startswith("SPAM:"):
        reason = first_line.split(":", 1)[1].strip() if ":" in first_line else "Flagged by AI"
        return True, reason

    return False, ""
