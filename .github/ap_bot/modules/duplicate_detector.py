# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Duplicate Issue Detector Module.

Uses Gemini AI to compare a newly opened issue against recent open issues
and flag potential duplicates with confidence scores.
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

_MAX_EXISTING_ISSUES: int = 50
_HIGH_CONFIDENCE_THRESHOLD: float = 0.75

_DUPLICATE_PROMPT: str = (
    "You are a duplicate issue detector for a GitHub repository.\n\n"
    "A new issue has been opened. Compare it against the list of existing "
    "open issues below and determine if any are potential duplicates.\n\n"
    "For each potential duplicate, return a line in the format:\n"
    "  DUPLICATE: #<issue_number> | <confidence_score_0_to_1> | <brief_reason>\n\n"
    "If there are no duplicates, return exactly: NO_DUPLICATES\n\n"
    "New Issue Title: {new_title}\n"
    "New Issue Body:\n{new_body}\n\n"
    "Existing Open Issues:\n{existing_issues}\n\n"
    "Analysis:"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(github_api: GitHubAPI, gemini_client: GeminiClient) -> list[dict]:
    """Detect potential duplicate issues using Gemini AI.

    Fetches the new issue and the most recent open issues from GitHub,
    sends them to Gemini for comparison, and if a high-confidence
    duplicate is found, adds a ``duplicate`` label and posts a comment
    linking to the potential duplicate.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client for analysis.

    Returns:
        A list of dicts with keys ``issue_number``, ``confidence``, and
        ``reason`` for each detected duplicate.
    """
    issue_number: Optional[int] = config.ISSUE_NUMBER
    if not issue_number:
        logger.error("No ISSUE_NUMBER found in config. Aborting duplicate detection.")
        return []

    try:
        # -- Fetch the new issue ---------------------------------------------
        new_issue = github_api.get_issue(issue_number)
        if not new_issue:
            logger.error(
                f"Could not fetch issue #{issue_number}. "
                f"Aborting duplicate detection."
            )
            return []
        new_title: str = new_issue.get("title", "")
        new_body: str = new_issue.get("body", "") or ""

        # -- Fetch recent open issues ----------------------------------------
        open_issues: list[dict] = github_api.list_issues(
            state="open", per_page=_MAX_EXISTING_ISSUES
        )
        # Exclude the current issue from comparison
        open_issues = [
            iss for iss in open_issues if iss.get("number") != issue_number
        ]

        if not open_issues:
            logger.info("No other open issues to compare against. Skipping.")
            return []

        existing_text = "\n".join(
            f"- #{iss.get('number')}: {iss.get('title', '')}"
            for iss in open_issues
        )

        logger.info(
            f"Checking issue #{issue_number} against {len(open_issues)} "
            f"existing open issues for duplicates."
        )

        # -- Ask Gemini to detect duplicates ---------------------------------
        prompt = _DUPLICATE_PROMPT.format(
            new_title=new_title,
            new_body=new_body,
            existing_issues=existing_text,
        )
        response: str = gemini_client.generate(prompt)

        duplicates = _parse_duplicates(response)

        if not duplicates:
            logger.info(f"No duplicates detected for issue #{issue_number}.")
            return []

        # -- Process high-confidence duplicates ------------------------------
        high_confidence = [
            d for d in duplicates if d["confidence"] >= _HIGH_CONFIDENCE_THRESHOLD
        ]

        if high_confidence:
            github_api.add_labels(issue_number, ["duplicate"])
            logger.info(f"Added 'duplicate' label to issue #{issue_number}.")

            duplicate_lines = "\n".join(
                f"- **#{d['issue_number']}** — Confidence: "
                f"{d['confidence']:.0%} — {d['reason']}"
                for d in high_confidence
            )
            comment_body = (
                f"🔍 **Potential Duplicate Detected**\n\n"
                f"This issue appears to be a duplicate of the following:\n\n"
                f"{duplicate_lines}\n\n"
                f"_Please review the linked issue(s) and close this one if "
                f"it is indeed a duplicate._\n\n{config.BOT_FOOTER}"
            )
            github_api.add_comment(issue_number, comment_body)
            logger.info(
                f"Posted duplicate detection comment on issue #{issue_number}."
            )
        else:
            logger.info(
                f"Duplicates detected for issue #{issue_number} but below "
                f"confidence threshold ({_HIGH_CONFIDENCE_THRESHOLD:.0%})."
            )

        return duplicates

    except Exception:
        logger.exception(f"Failed to run duplicate detection for issue #{issue_number}.")
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_duplicates(raw_response: str) -> list[dict]:
    """Parse duplicate detection results from Gemini's response.

    Expected format per line::

        DUPLICATE: #123 | 0.85 | Both issues describe the same login bug

    Args:
        raw_response: The raw text returned by Gemini.

    Returns:
        A list of dicts with ``issue_number``, ``confidence``, and ``reason``.
    """
    if "NO_DUPLICATES" in raw_response.upper():
        return []

    results: list[dict] = []
    for line in raw_response.strip().splitlines():
        line = line.strip()
        if not line.upper().startswith("DUPLICATE:"):
            continue

        try:
            content = line.split(":", 1)[1].strip()
            parts = [p.strip() for p in content.split("|")]
            if len(parts) < 2:
                continue

            issue_num = int(parts[0].replace("#", "").strip())
            confidence = float(parts[1].strip())
            reason = parts[2].strip() if len(parts) >= 3 else "Similar issue"

            results.append(
                {
                    "issue_number": issue_num,
                    "confidence": confidence,
                    "reason": reason,
                }
            )
        except (ValueError, IndexError):
            logger.warning(f"Could not parse duplicate line: {line!r}")
            continue

    return results
