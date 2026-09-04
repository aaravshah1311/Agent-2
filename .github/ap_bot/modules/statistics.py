# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Repository Statistics Module.

Queries the GitHub API to gather repository activity metrics, including
open/closed issues, open/merged PRs, label distributions, and top contributors,
then formats them into a comprehensive markdown report.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Dict, List, Optional

from ..config import config
from ..logger import logger

if TYPE_CHECKING:
    from ..gemini import GeminiClient
    from ..github_api import GitHubAPI


def run(github_api: GitHubAPI, gemini_client: Optional[GeminiClient] = None) -> None:
    """Gather repository statistics and log/report them.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Unused (kept for interface consistency).
    """
    logger.info("Gathering repository statistics...")
    try:
        # 1. Fetch Repository Info
        repo_data = github_api.get_repo_stats()
        if not repo_data:
            logger.error("Could not fetch repository statistics.")
            return

        forks = repo_data.get("forks_count", 0)
        stars = repo_data.get("stargazers_count", 0)

        # 2. Fetch Open Issues & PRs
        issues_list = github_api.list_issues(state="all", per_page=100)

        open_issues = 0
        closed_issues = 0
        open_prs = 0
        closed_prs = 0

        label_counter: Counter = Counter()

        for iss in issues_list:
            is_pr = "pull_request" in iss
            state = iss.get("state")

            if is_pr:
                if state == "open":
                    open_prs += 1
                elif state == "closed":
                    # The issues list endpoint does not expose merge status,
                    # so closed PRs are counted together (merged + declined).
                    closed_prs += 1
            else:
                if state == "open":
                    open_issues += 1
                    # Count labels on open issues
                    for lbl in iss.get("labels", []):
                        label_name = lbl.get("name")
                        if label_name:
                            label_counter[label_name] += 1
                elif state == "closed":
                    closed_issues += 1

        # 3. Fetch Contributors
        contributors = github_api.list_contributors()
        top_contributors = sorted(
            contributors, key=lambda x: x.get("contributions", 0), reverse=True
        )[:5]

        # 4. Generate Report
        report = _generate_report_markdown(
            stars=stars,
            forks=forks,
            open_issues=open_issues,
            closed_issues=closed_issues,
            open_prs=open_prs,
            closed_prs=closed_prs,
            labels=label_counter,
            contributors=top_contributors,
        )

        logger.info(f"\nREPOSITORY METRICS REPORT\n{'='*30}\n{report}\n{'='*30}")

    except Exception:
        logger.exception("Failed to compile repository statistics.")
        raise


def _generate_report_markdown(
    stars: int,
    forks: int,
    open_issues: int,
    closed_issues: int,
    open_prs: int,
    closed_prs: int,
    labels: Counter,
    contributors: List[dict],
) -> str:
    """Generate a clean markdown report of repository statistics.

    Args:
        stars: Number of stargazers.
        forks: Number of forks.
        open_issues: Count of open issues.
        closed_issues: Count of closed issues.
        open_prs: Count of open pull requests.
        closed_prs: Count of closed pull requests (merged or declined).
        labels: Label distribution counter.
        contributors: List of top contributor dicts.

    Returns:
        Formatted markdown report.
    """
    total_issues = open_issues + closed_issues
    total_prs = open_prs + closed_prs

    lines = [
        "## 📊 Repository Statistics Report",
        "",
        "### 📈 General Metrics",
        f"- ⭐ **Stars:** {stars}",
        f"- 🍴 **Forks:** {forks}",
        "",
        "### 🐛 Issue Triage Summary",
        f"- 🟢 **Open Issues:** {open_issues}",
        f"- 🔴 **Closed Issues:** {closed_issues}",
        f"- 📊 **Resolution Rate:** {closed_issues / max(1, total_issues):.1%}",
        "",
        "### 🔀 Pull Request Summary",
        f"- 🟢 **Open Pull Requests:** {open_prs}",
        f"- 🟣 **Closed Pull Requests:** {closed_prs}",
        f"- 📊 **Close Rate:** {closed_prs / max(1, total_prs):.1%}",
        "",
        "### 🏷️ Label Distribution (Open Issues)",
    ]

    if not labels:
        lines.append("- No labels applied to open issues.")
    else:
        for lbl, count in labels.most_common(8):
            lines.append(f"- `{lbl}`: {count} issues")

    lines.extend([
        "",
        "### 🏆 Top Contributors",
    ])

    if not contributors:
        lines.append("- No contributor data available.")
    else:
        for idx, c in enumerate(contributors, 1):
            username = c.get("login", "unknown")
            contributions = c.get("contributions", 0)
            lines.append(f"{idx}. **@{username}** — {contributions} contributions")

    return "\n".join(lines)
