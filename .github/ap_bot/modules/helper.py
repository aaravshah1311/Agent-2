# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Helper Utilities Module.

Contains common utility functions used across multiple AP Bot modules,
such as text truncation, date parsing, and label format processing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from ..config import LABELS
from ..logger import logger

if TYPE_CHECKING:
    from ..gemini import GeminiClient
    from ..github_api import GitHubAPI


def truncate_text(text: str, max_length: int = 10000) -> str:
    """Truncate text to a maximum length and append a truncation suffix if needed.

    Args:
        text: The string to truncate.
        max_length: Maximum allowed length. Defaults to 10000.

    Returns:
        The truncated string.
    """
    if len(text) <= max_length:
        return text
    return text[:max_length] + "\n\n... [truncated due to size constraints]"


def parse_labels_from_response(response: str) -> List[str]:
    """Parse comma-separated labels from Gemini's response.

    Cleans, deduplicates, and filters labels to only include those defined
    in the central label configuration (config.LABELS).

    Args:
        response: The raw string response from the AI.

    Returns:
        A list of cleaned and validated label names.
    """
    if not response:
        return []
        
    candidates = [
        lbl.strip().lower()
        for lbl in response.replace("\n", ",").split(",")
        if lbl.strip()
    ]
    
    # Check against keys in configuration labels (lowercased)
    valid_labels = {k.lower() for k in LABELS.keys()}
    parsed = sorted({lbl for lbl in candidates if lbl in valid_labels})
    return parsed


def format_markdown_list(items: List[str]) -> str:
    """Format a list of strings as markdown bullet points.

    Args:
        items: List of strings.

    Returns:
        A markdown-formatted list.
    """
    return "\n".join(f"- {item}" for item in items)


def is_valid_issue_or_pr(data: dict) -> bool:
    """Check if the provided issue or pull request dictionary has basic fields.

    Args:
        data: The dictionary returned by the GitHub API.

    Returns:
        True if fields are valid, False otherwise.
    """
    return bool(data and data.get("number") and "title" in data)


def days_since(date_string: str) -> int:
    """Calculate the number of days between a given ISO 8601 date string and now.

    Handles the 'Z' suffix returned by the GitHub API.

    Args:
        date_string: ISO 8601 date string (e.g. '2026-07-10T14:31:09Z').

    Returns:
        The integer number of days elapsed.
    """
    try:
        # Replace Z with +00:00 for ISO compliance
        clean_date_str = date_string.replace("Z", "+00:00")
        target_date = datetime.fromisoformat(clean_date_str)
        now = datetime.now(timezone.utc)
        delta = now - target_date
        return max(0, delta.days)
    except Exception:
        logger.exception(f"Failed to parse date string: {date_string!r}")
        return 0


def run_scheduler(github_api: GitHubAPI, gemini_client: Optional[GeminiClient] = None) -> None:
    """Dispatch workflow-dispatch capable maintenance jobs.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client.
    """
    workflows = [
        "stale-issues.yml",
        "auto-close.yml",
        "issue-statistics.yml",
    ]

    repo_data = github_api.get_repo_stats() or {}
    ref = repo_data.get("default_branch", "main")

    logger.info("Scheduler dispatching %d workflows on ref '%s'.", len(workflows), ref)
    for workflow_file in workflows:
        url = f"{github_api.base_url}/actions/workflows/{workflow_file}/dispatches"
        response = github_api.session.post(url, json={"ref": ref})
        if response.status_code in {204, 201}:
            logger.info("Dispatched workflow '%s'.", workflow_file)
        else:
            logger.error(
                "Failed to dispatch workflow '%s' (%s): %s",
                workflow_file,
                response.status_code,
                response.text,
            )
