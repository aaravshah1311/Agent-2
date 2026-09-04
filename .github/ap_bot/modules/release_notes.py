# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Release Notes Generator Module.

Uses Gemini AI to compile recent merged pull requests into a structured,
user-friendly release notes draft for a newly created repository release tag.
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

_RELEASE_NOTES_PROMPT: str = (
    "You are a release manager compiling release notes for a software repository.\n"
    "Create professional release notes for version tag **{tag}** based on "
    "the list of merged Pull Requests below.\n\n"
    "Group changes into these sections if applicable:\n"
    "- ✨ New Features\n"
    "- 🐛 Bug Fixes\n"
    "- 🔧 Refactoring & Improvements\n"
    "- ⚠️ Breaking Changes\n"
    "- 📦 Dependency Updates\n\n"
    "For each PR, provide a one-line summary mentioning the PR number and the author.\n\n"
    "Merged Pull Requests:\n"
    "{pull_requests}\n\n"
    "Response:"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(github_api: GitHubAPI, gemini_client: GeminiClient) -> None:
    """Generate release notes for a new version tag.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client.
    """
    tag: Optional[str] = config.TAG_NAME
    if not tag:
        logger.error("No TAG_NAME found in config. Aborting release notes generation.")
        return

    try:
        logger.info(f"Generating release notes for tag '{tag}'...")
        
        # We can search for merged PRs.
        # Since we want to simplify, we list the last 30 closed pull requests.
        url = f"{github_api.base_url}/pulls?state=closed&per_page=30"
        response = github_api.session.get(url)
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch closed PRs: {response.text}")
            return
            
        closed_prs: List[dict] = response.json()
        merged_prs = [pr for pr in closed_prs if pr.get("merged_at") is not None]
        
        if not merged_prs:
            logger.warning("No merged PRs found to generate release notes from.")
            pr_text = "- No recent merged pull requests found."
        else:
            pr_text = "\n".join(
                f"- #{pr.get('number')}: {pr.get('title', '')} (by @{pr.get('user', {}).get('login', 'unknown')})"
                for pr in merged_prs
            )

        # -- Generate release notes via Gemini --------------------------------
        prompt = _RELEASE_NOTES_PROMPT.format(tag=tag, pull_requests=pr_text)
        logger.info("Sending PR compilation to Gemini for release notes draft...")
        notes = gemini_client.generate(prompt)

        # -- Save to cache ---------------------------------------------------
        cache_dir = os.path.join(os.getcwd(), ".github", "ap_bot", "cache")
        os.makedirs(cache_dir, exist_ok=True)
        
        notes_file = os.path.join(cache_dir, "release_notes.md")
        with open(notes_file, "w", encoding="utf-8") as f:
            f.write(notes)
            
        logger.info(f"Release notes successfully written to: {notes_file}")

    except Exception:
        logger.exception(f"Failed to generate release notes for tag '{tag}'.")
        raise
