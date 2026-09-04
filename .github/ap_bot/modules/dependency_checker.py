# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Dependency Checker Module.

Scans changed files in a pull request for package manager files,
extracts dependency modifications, and uses Gemini AI to review them for
known vulnerabilities, licensing issues, or major updates.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, List, Optional

from ..config import config
from ..logger import logger

if TYPE_CHECKING:
    from ..gemini import GeminiClient
    from ..github_api import GitHubAPI

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEPENDENCY_FILES: set[str] = {
    "requirements.txt",
    "setup.py",
    "pyproject.toml",
    "package.json",
    "Pipfile",
    "Cargo.toml",
    "go.mod",
}

_DEPENDENCY_PROMPT: str = (
    "You are a software security and dependency reviewer.\n"
    "Review the following changes in dependency definitions from a pull request.\n\n"
    "Look out for:\n"
    "1. Packages with known critical vulnerabilities or security risks.\n"
    "2. Major version jumps that might introduce breaking changes.\n"
    "3. Potentially malicious packages (typosquatting, e.g. 'reqeusts' instead of 'requests').\n"
    "4. Licensing incompatibilities (e.g. GPL packages in a commercial/MIT project).\n\n"
    "Dependency File Diff:\n"
    "{diff}\n\n"
    "Provide a concise markdown report summarizing any issues or warning flags. "
    "Use ✅ for safe/routine upgrades, ⚠️ for warnings (e.g. major version jumps), "
    "and 🔴 for high risk packages (vulnerable or typosquatting). "
    "If everything looks perfect and clean, respond with exactly: "
    "✅ No dependency concerns or risky updates detected."
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(github_api: GitHubAPI, gemini_client: GeminiClient) -> None:
    """Analyze dependency changes in a pull request.

    Checks the files modified in a PR, retrieves the diff for any package
    manager configuration files, sends the diff to Gemini for safety analysis,
    and posts a comment with the review results.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client.
    """
    pr_number: Optional[int] = config.PR_NUMBER
    if not pr_number:
        logger.error("No PR_NUMBER found in config. Aborting dependency checker.")
        return

    try:
        # -- Fetch files in PR ------------------------------------------------
        logger.info(f"Checking for dependency file changes in PR #{pr_number}...")
        pr_files = github_api.get_pr_files(pr_number)
        
        changed_dep_files: List[str] = []
        for file in pr_files:
            filename = file.get("filename", "")
            # Get the basename to match
            basename = os.path.basename(filename)
            if basename in DEPENDENCY_FILES:
                changed_dep_files.append(filename)

        if not changed_dep_files:
            logger.info("No dependency configuration files modified in this PR.")
            return

        logger.info(f"Dependency files changed in PR: {changed_dep_files}")

        # -- Get the PR diff and isolate dependency files --------------------
        # For simplicity, we can request the entire diff and extract lines related to the dep files,
        # or use patch fields from PR files API. Let's build a diff from the file patches.
        dep_diffs = []
        for file in pr_files:
            filename = file.get("filename", "")
            if filename in changed_dep_files:
                patch = file.get("patch", "")
                dep_diffs.append(f"--- a/{filename}\n+++ b/{filename}\n{patch}\n")

        full_dep_diff = "\n".join(dep_diffs)
        
        # Truncate if extremely large
        if len(full_dep_diff) > 10000:
            full_dep_diff = full_dep_diff[:10000] + "\n... [truncated]"

        # -- Send to Gemini for review ---------------------------------------
        logger.info("Sending dependency changes to Gemini for security review...")
        prompt = _DEPENDENCY_PROMPT.format(diff=full_dep_diff)
        response = gemini_client.generate(prompt)

        # -- Post the review comment -----------------------------------------
        comment_body = (
            f"📦 **AI Dependency Review**\n\n"
            f"{response}\n\n"
            f"_Always verify dependency updates manually. Maintainers should "
            f"inspect version tags and package sources._\n\n{config.BOT_FOOTER}"
        )
        
        github_api.add_comment(pr_number, comment_body)
        logger.info(f"Posted dependency review on PR #{pr_number}.")

    except Exception:
        logger.exception(f"Failed to run dependency review on PR #{pr_number}.")
        raise

