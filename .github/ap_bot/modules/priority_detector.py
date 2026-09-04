# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — AI Priority Detector Module.

Uses Gemini AI to analyze an issue's content, estimate its priority,
apply the appropriate priority label, and explain the reasoning in a comment.
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

VALID_PRIORITIES: set[str] = {
    "critical",
    "high-priority",
    "medium-priority",
    "low-priority",
}

_PRIORITY_PROMPT: str = (
    "You are an AI assistant that triages GitHub issues and assesses their priority.\n\n"
    "Consider the following criteria:\n"
    "1. Security impact (e.g. data breach, remote code execution)\n"
    "2. User impact (how many users/deployments are affected)\n"
    "3. Data loss or corruption risk\n"
    "4. Core functionality affected vs. edge case\n"
    "5. Workaround availability\n\n"
    "Based on these, choose exactly one priority label from this list:\n"
    "critical, high-priority, medium-priority, low-priority\n\n"
    "Provide your response in this exact format:\n"
    "PRIORITY: <priority-label>\n"
    "REASON: <one-sentence justification explaining the priority level>\n\n"
    "Issue Title: {title}\n"
    "Issue Body:\n{body}\n\n"
    "Response:"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(github_api: GitHubAPI, gemini_client: GeminiClient) -> Optional[str]:
    """Assess issue priority using Gemini AI and apply priority labels.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client.

    Returns:
        The applied priority label, or None if failed.
    """
    issue_number: Optional[int] = config.ISSUE_NUMBER
    if not issue_number:
        logger.error("No ISSUE_NUMBER found in config. Aborting priority detection.")
        return None

    try:
        issue = github_api.get_issue(issue_number)
        if not issue:
            logger.error(
                f"Could not fetch issue #{issue_number}. "
                f"Aborting priority detection."
            )
            return None
        title: str = issue.get("title", "")
        body: str = issue.get("body", "") or ""

        logger.info(f"Detecting priority for issue #{issue_number}: {title!r}")

        # -- Ask Gemini to assess priority ------------------------------------
        prompt = _PRIORITY_PROMPT.format(title=title, body=body)
        response: str = gemini_client.generate(prompt)

        priority, reason = _parse_priority_response(response)
        if not priority:
            logger.warning(
                f"Gemini returned no valid priority for issue #{issue_number}. "
                f"Raw response: {response!r}"
            )
            return None

        # -- Apply priority label --------------------------------------------
        github_api.add_labels(issue_number, [priority])
        logger.info(f"Applied priority label '{priority}' to issue #{issue_number}.")

        # -- Post explanation comment ---------------------------------------
        comment_body = (
            f"⚡ **AI Priority Assessment**\n\n"
            f"**Priority:** `{priority}`\n"
            f"**Justification:** {reason}\n\n"
            f"_Priority labels are advisory. Maintainers can adjust priority levels "
            f"at any time._\n\n{config.BOT_FOOTER}"
        )
        github_api.add_comment(issue_number, comment_body)
        logger.info(f"Posted priority assessment comment on issue #{issue_number}.")

        return priority

    except Exception:
        logger.exception(f"Failed to detect priority for issue #{issue_number}.")
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_priority_response(raw_response: str) -> tuple[Optional[str], str]:
    """Parse priority and reason from Gemini's response.

    Expected format:
        PRIORITY: high-priority
        REASON: The issue affects core database operations and causes data loss.

    Args:
        raw_response: The raw text returned by Gemini.

    Returns:
        A tuple of (priority_label, reason_text).
    """
    priority: Optional[str] = None
    reason: str = "Triage based on initial issue description."

    for line in raw_response.strip().splitlines():
        line = line.strip()
        if line.upper().startswith("PRIORITY:"):
            p_val = line.split(":", 1)[1].strip().lower()
            if p_val in VALID_PRIORITIES:
                priority = p_val
        elif line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()

    return priority, reason
