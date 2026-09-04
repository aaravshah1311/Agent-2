# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — AI PR Classifier Module.

Uses Gemini AI to analyze a pull request's title, body, and changed files
to classify it into one or more predefined categories, then applies the
corresponding labels and posts a summary comment.
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
    "bug-fix",
    "new-feature",
    "refactor",
    "documentation",
    "performance",
    "security",
    "dependency-update",
    "testing",
    "ui-improvement",
}

_CLASSIFICATION_PROMPT: str = (
    "You are a GitHub pull request classifier for an open-source project.\n\n"
    "Analyze the following pull request and classify it into ONE or MORE of "
    "these categories (return ONLY a comma-separated list, nothing else):\n"
    "bug-fix, new-feature, refactor, documentation, performance, security, "
    "dependency-update, testing, ui-improvement\n\n"
    "PR Title: {title}\n\n"
    "PR Description:\n{body}\n\n"
    "Changed Files:\n{files}\n\n"
    "Categories:"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(github_api: GitHubAPI, gemini_client: GeminiClient) -> list[str]:
    """Classify a pull request using Gemini AI and apply labels.

    Fetches the PR details and changed file list from the GitHub API,
    sends them to Gemini for classification, parses the response into
    valid labels, applies them, and posts a summary comment.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client for generating classifications.

    Returns:
        A list of label strings that were applied.
    """
    pr_number: Optional[int] = config.PR_NUMBER
    if not pr_number:
        logger.error("No PR_NUMBER found in config. Aborting PR classification.")
        return []

    try:
        pr = github_api.get_pr(pr_number)
        if not pr:
            logger.error(
                f"Could not fetch PR #{pr_number}. Aborting PR classification."
            )
            return []
        title: str = pr.get("title", "")
        body: str = pr.get("body", "") or ""

        # Retrieve changed file names -----------------------------------------
        files_data: list[dict] = github_api.get_pr_files(pr_number)
        file_names: list[str] = [f.get("filename", "") for f in files_data]
        files_text: str = "\n".join(f"- {name}" for name in file_names) or "(none)"

        logger.info(f"Classifying PR #{pr_number}: {title!r} ({len(file_names)} files)")

        # -- Ask Gemini to classify ------------------------------------------
        prompt = _CLASSIFICATION_PROMPT.format(
            title=title, body=body, files=files_text
        )
        response: str = gemini_client.generate(prompt)

        labels = _parse_labels(response)
        if not labels:
            logger.warning(
                f"Gemini returned no valid labels for PR #{pr_number}. "
                f"Raw response: {response!r}"
            )
            return []

        # -- Apply labels ----------------------------------------------------
        github_api.add_labels(pr_number, labels)
        logger.info(f"Applied labels {labels} to PR #{pr_number}.")

        # -- Post summary comment --------------------------------------------
        label_list = ", ".join(f"`{lbl}`" for lbl in labels)
        comment_body = (
            f"🏷️ **AI PR Classification**\n\n"
            f"This pull request has been automatically classified into the "
            f"following categories:\n\n{label_list}\n\n"
            f"_Labels have been applied accordingly. A maintainer will verify "
            f"the classification shortly._\n\n{config.BOT_FOOTER}"
        )
        github_api.add_comment(pr_number, comment_body)
        logger.info(f"Posted classification comment on PR #{pr_number}.")

        return labels

    except Exception:
        logger.exception(f"Failed to classify PR #{pr_number}.")
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
