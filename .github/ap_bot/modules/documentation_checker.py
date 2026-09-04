# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Documentation Checker Module.

Validates that essential documentation files (like README, LICENSE,
contributing guidelines, issue/PR templates) exist and are placed correctly.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Dict, Optional

from ..config import config
from ..logger import logger

if TYPE_CHECKING:
    from ..gemini import GeminiClient
    from ..github_api import GitHubAPI

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUIRED_DOCS: Dict[str, str] = {
    "README.md": "Main description of the project and setup instructions.",
    "LICENSE": "Legal license terms for the repository.",
    "CONTRIBUTING.md": "Guidelines for contributors.",
    "CODE_OF_CONDUCT.md": "Community standards and code of conduct.",
    ".github/PULL_REQUEST_TEMPLATE.md": "Template for submitting pull requests.",
    ".github/ISSUE_TEMPLATE/": "Templates for filing bugs, features, or questions.",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(github_api: GitHubAPI, gemini_client: Optional[GeminiClient] = None) -> None:
    """Validate repository documentation and log/comment results.

    Checks the local filesystem for the required documentation files
    listed in ``REQUIRED_DOCS``.  If triggered by a pull request, posts
    a status report as a comment.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Unused (kept for interface consistency).
    """
    logger.info("Running documentation validation...")
    
    # We check files in the current working directory (assumed to be repo root)
    workspace = os.getcwd()
    
    results: Dict[str, bool] = {}
    missing_count = 0
    
    for rel_path in REQUIRED_DOCS:
        full_path = os.path.join(workspace, rel_path)
        exists = os.path.exists(full_path)
        results[rel_path] = exists
        if not exists:
            missing_count += 1
            logger.warning(f"Missing documentation file/folder: '{rel_path}'")
        else:
            logger.info(f"Verified documentation exists: '{rel_path}'")

    report = _generate_markdown_report(results)
    
    pr_number = config.PR_NUMBER
    if pr_number:
        logger.info(f"Triggered by PR #{pr_number}. Posting report comment...")
        try:
            body = (
                f"📖 **Repository Documentation Review**\n\n"
                f"{report}\n\n"
                f"_Please ensure that missing documentation files are created "
                f"to help guide contributors._\n\n{config.BOT_FOOTER}"
            )
            github_api.add_comment(pr_number, body)
            logger.info("Documentation review report posted successfully.")
        except Exception:
            logger.exception(f"Failed to post documentation report on PR #{pr_number}.")
            raise
    else:
        logger.info(f"Documentation check complete. Missing: {missing_count} files.\n{report}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _generate_markdown_report(results: Dict[str, bool]) -> str:
    """Generate a clean markdown table showing documentation status.

    Args:
        results: Mapping of relative file paths to existence booleans.

    Returns:
        A formatted markdown string.
    """
    lines = [
        "| Documentation File / Folder | Status | Description |",
        "| :--- | :---: | :--- |",
    ]
    
    for path, exists in results.items():
        status = "✅ Found" if exists else "❌ **Missing**"
        description = REQUIRED_DOCS[path]
        lines.append(f"| `{path}` | {status} | {description} |")
        
    return "\n".join(lines)
