# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — AI Issue Classifier Module.

Uses Gemini AI to analyze an issue's title and body and classify it
into one or more predefined categories, then applies the corresponding
labels and posts a summary comment.
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

VALID_LABELS: set[str] = {
    "bug",
    "feature",
    "documentation",
    "security",
    "question",
    "enhancement",
    "backend",
    "frontend",
    "api",
    "database",
    "performance",
    "ui",
    "ai-ml",
    "devops",
}

_CLASSIFICATION_PROMPT: str = (
    "You are a GitHub issue classifier for an open-source project.\n\n"
    "Analyze the following issue and classify it into ONE or MORE of these "
    "categories (return ONLY a comma-separated list, nothing else):\n"
    "bug, feature, documentation, security, question, enhancement, "
    "backend, frontend, api, database, performance, ui, ai-ml, devops\n\n"
    "Issue Title: {title}\n\n"
    "Issue Body:\n{body}\n\n"
    "Categories:"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(github_api: GitHubAPI, gemini_client: GeminiClient) -> list[str]:
    """Classify an issue using Gemini AI and apply labels.

    Fetches the issue from the GitHub API, sends its title and body to
    Gemini for classification, parses the response into valid labels,
    applies them, and posts a summary comment.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client for generating classifications.

    Returns:
        A list of label strings that were applied.
    """
    issue_number: Optional[int] = config.ISSUE_NUMBER
    if not issue_number:
        logger.error("No ISSUE_NUMBER found in config. Aborting issue classification.")
        return []

    try:
        issue = github_api.get_issue(issue_number)
        if not issue:
            logger.error(
                f"Could not fetch issue #{issue_number}. "
                f"Aborting issue classification."
            )
            return []
        title: str = issue.get("title", "")
        body: str = issue.get("body", "") or ""

        logger.info(f"Classifying issue #{issue_number}: {title!r}")

        # -- Ask Gemini to classify ------------------------------------------
        prompt = _CLASSIFICATION_PROMPT.format(title=title, body=body)
        response: str = gemini_client.generate(prompt)

        labels = _parse_labels(response)
        if not labels:
            logger.warning(
                f"Gemini returned no valid labels for issue #{issue_number}. "
                f"Raw response: {response!r}"
            )
            return []

        # -- Apply labels ----------------------------------------------------
        github_api.add_labels(issue_number, labels)
        logger.info(f"Applied labels {labels} to issue #{issue_number}.")

        # -- Post summary comment --------------------------------------------
        label_list = ", ".join(f"`{lbl}`" for lbl in labels)
        comment_body = (
            f"🏷️ **AI Classification**\n\n"
            f"This issue has been automatically classified into the following "
            f"categories:\n\n{label_list}\n\n"
            f"_Labels have been applied accordingly. A maintainer will verify "
            f"the classification shortly._\n\n{config.BOT_FOOTER}"
        )
        github_api.add_comment(issue_number, comment_body)
        logger.info(f"Posted classification comment on issue #{issue_number}.")

        return labels

    except Exception:
        logger.exception(f"Failed to classify issue #{issue_number}.")
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_labels(raw_response: str) -> list[str]:
    """Parse comma-separated labels from Gemini's response.

    Only labels present in ``VALID_LABELS`` are kept.

    Args:
        raw_response: The raw text returned by Gemini.

    Returns:
        A deduplicated, sorted list of valid label strings.
    """
    candidates = [
        token.strip().lower()
        for token in raw_response.replace("\n", ",").split(",")
        if token.strip()
    ]
    valid = sorted({lbl for lbl in candidates if lbl in VALID_LABELS})
    return valid
