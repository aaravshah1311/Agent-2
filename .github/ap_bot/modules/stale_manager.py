# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Stale Issue Manager Module.

Identifies inactive issues, marks them as stale with a warning comment,
and automatically closes them after a configured grace period if no activity occurs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from ..config import config
from ..logger import logger
from .helper import days_since

if TYPE_CHECKING:
    from ..gemini import GeminiClient
    from ..github_api import GitHubAPI

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_STALE_WARNING: str = (
    "⏰ **Stale Issue Notice**\n\n"
    "This issue has been automatically marked as **stale** because it has "
    "not had any activity in the last {stale_days} days.\n\n"
    "It will be **closed in {grace_days} days** if no further activity occurs.\n\n"
    "If this issue is still relevant, please leave a comment or update the description. "
    "Thank you for your contributions!"
)

_STALE_CLOSE: str = (
    "🔒 **Issue Closed — Stale**\n\n"
    "This issue has been automatically closed due to inactivity. It was marked "
    "as stale {grace_days} days ago and received no further updates.\n\n"
    "If this issue is still relevant, please feel free to reopen it or create "
    "a new issue with updated details. Thank you!"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(github_api: GitHubAPI, gemini_client: Optional[GeminiClient] = None) -> None:
    """Identify issues with no activity and mark them as stale.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Unused (kept for interface consistency).
    """
    logger.info("Running stale issue check...")
    try:
        # Fetch open issues
        issues: List[dict] = github_api.list_issues(state="open", per_page=100)
        
        for issue in issues:
            # Skip PRs (GitHub API lists PRs as issues, they have a 'pull_request' key)
            if "pull_request" in issue:
                continue
                
            issue_number = issue.get("number")
            if not issue_number:
                continue

            labels = [lbl.get("name") for lbl in issue.get("labels", [])]
            
            # Skip if already stale, or has exempt labels (critical, security)
            if "stale" in labels or "critical" in labels or "security" in labels:
                continue

            updated_at = issue.get("updated_at")
            if not updated_at:
                continue

            days = days_since(updated_at)
            if days >= config.STALE_DAYS:
                logger.info(
                    f"Issue #{issue_number} is inactive for {days} days. "
                    f"Marking as stale."
                )
                _mark_as_stale(github_api, issue_number)
                
    except Exception:
        logger.exception("Failed to run stale issue checker.")
        raise


def auto_close(
    github_api: GitHubAPI, gemini_client: Optional[GeminiClient] = None
) -> None:
    """Close stale issues that have exceeded the grace period.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Unused (kept for interface consistency).
    """
    logger.info("Running auto-close for stale issues...")
    try:
        # Fetch open issues with label 'stale'
        issues: List[dict] = github_api.list_issues(
            state="open", labels="stale", per_page=100
        )
        
        for issue in issues:
            if "pull_request" in issue:
                continue
                
            issue_number = issue.get("number")
            if not issue_number:
                continue

            updated_at = issue.get("updated_at")
            if not updated_at:
                continue

            # Since marking stale updates the issue, if it's still stale and
            # updated_at is older than the grace period, no one has commented.
            days = days_since(updated_at)
            if days >= config.GRACE_PERIOD_DAYS:
                logger.info(
                    f"Stale issue #{issue_number} has been inactive for "
                    f"{days} days since stale label. Closing it."
                )
                _close_stale_issue(github_api, issue_number)
                
    except Exception:
        logger.exception("Failed to run auto-close for stale issues.")
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _mark_as_stale(github_api: GitHubAPI, issue_number: int) -> None:
    """Add stale label and warning comment to an issue.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        issue_number: The issue number to mark.
    """
    try:
        github_api.add_labels(issue_number, ["stale"])
        comment = _STALE_WARNING.format(
            stale_days=config.STALE_DAYS, grace_days=config.GRACE_PERIOD_DAYS
        )
        comment += f"\n\n{config.BOT_FOOTER}"
        github_api.add_comment(issue_number, comment)
    except Exception:
        logger.exception(f"Failed to mark issue #{issue_number} as stale.")
        raise


def _close_stale_issue(github_api: GitHubAPI, issue_number: int) -> None:
    """Close a stale issue and post close comment.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        issue_number: The issue number to close.
    """
    try:
        comment = _STALE_CLOSE.format(grace_days=config.GRACE_PERIOD_DAYS)
        comment += f"\n\n{config.BOT_FOOTER}"
        github_api.add_comment(issue_number, comment)
        github_api.close_issue(issue_number)
    except Exception:
        logger.exception(f"Failed to close stale issue #{issue_number}.")
        raise
