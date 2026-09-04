# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Label Manager Module.

Manages repository labels, including syncing default configurations and
applying/removing labels on issues and pull requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

from ..config import LABELS
from ..logger import logger

if TYPE_CHECKING:
    from ..github_api import GitHubAPI


def run(github_api: GitHubAPI, gemini_client: None = None) -> None:
    """Sync all defined labels to the repository.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Unused (kept for interface consistency).
    """
    logger.info("Running label synchronization...")
    sync_labels(github_api)


def ensure_labels(github_api: GitHubAPI) -> None:
    """Ensure that all configured labels exist in the repository.

    Alias for sync_labels.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
    """
    sync_labels(github_api)


def sync_labels(github_api: GitHubAPI) -> None:
    """Create any missing configured labels in the repository.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
    """
    try:
        # Check config.LABELS mapping (label_name -> hex color)
        for name, color_with_hash in LABELS.items():
            # Strip '#' from hex color
            color = color_with_hash.lstrip("#")
            
            logger.info(f"Checking/Creating label: '{name}' with color '{color}'")

            # GitHub API endpoint: POST /repos/{owner}/{repo}/labels
            # Body: { name, color, description }
            # NB: github_api.base_url already includes '/repos/{owner}/{repo}'.
            url = f"{github_api.base_url}/labels"
            data = {
                "name": name,
                "color": color,
                "description": "Managed by AP Automated Bot",
            }
            
            response = github_api.session.post(url, json=data)
            if response.status_code == 201:
                logger.info(f"Successfully created label '{name}'.")
            elif response.status_code == 422:
                # 422 Unprocessable Entity usually means it already exists
                logger.debug(f"Label '{name}' already exists or invalid.")
            else:
                logger.warning(
                    f"Unexpected status code {response.status_code} "
                    f"when creating label '{name}': {response.text}"
                )
    except Exception:
        logger.exception("Failed to sync labels.")
        raise


def add_labels_to_issue(
    github_api: GitHubAPI, issue_number: int, labels: List[str]
) -> None:
    """Add a list of labels to an issue or pull request.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        issue_number: The issue or pull request number.
        labels: List of label names to apply.
    """
    if not labels:
        return
        
    try:
        github_api.add_labels(issue_number, labels)
        logger.info(f"Added labels {labels} to issue #{issue_number}.")
    except Exception:
        logger.exception(f"Failed to add labels {labels} to issue #{issue_number}.")
        raise
