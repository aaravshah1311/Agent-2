# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — AI Security Review Module.

Uses Gemini AI to scan pull request diffs for potential security
vulnerabilities including hardcoded secrets, SQL injection, unsafe
subprocess usage, insecure deserialization, weak input validation,
and exposed credentials.
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

_MAX_DIFF_LENGTH: int = 10_000

_SECURITY_PROMPT: str = (
    "You are an expert security auditor reviewing a pull request diff.\n\n"
    "Scan the diff for the following security concerns:\n\n"
    "1. **Hardcoded Secrets** — API keys, passwords, tokens, or "
    "private keys embedded in code\n"
    "2. **SQL Injection** — Unsanitized user input in SQL queries\n"
    "3. **Unsafe Subprocess** — Shell injection via subprocess calls "
    "with shell=True or unsanitized input\n"
    "4. **Insecure Deserialization** — Use of pickle, yaml.load(), or "
    "eval() on untrusted data\n"
    "5. **Weak Input Validation** — Missing or insufficient input "
    "sanitization and validation\n"
    "6. **Exposed Credentials** — Configuration files or environment "
    "variable handling that may leak secrets\n\n"
    "For each issue found, provide:\n"
    "- **Severity**: CRITICAL, HIGH, MEDIUM, or LOW\n"
    "- **Location**: File name and approximate line reference\n"
    "- **Description**: What the issue is\n"
    "- **Recommendation**: How to fix it\n\n"
    "If NO security issues are found, respond with exactly: "
    "NO_SECURITY_ISSUES\n\n"
    "Format your response using Markdown.\n\n"
    "Diff:\n```diff\n{diff}\n```\n\n"
    "Security Analysis:"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(github_api: GitHubAPI, gemini_client: GeminiClient) -> bool:
    """Perform an AI-powered security review on a pull request.

    Fetches the PR diff, sends it to Gemini for security analysis, and
    posts the results as a PR comment.  If security issues are found,
    the ``security`` label is added to the PR.

    Args:
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client for generating the review.

    Returns:
        ``True`` if security issues were found, ``False`` otherwise.
    """
    pr_number: Optional[int] = config.PR_NUMBER
    if not pr_number:
        logger.error("No PR_NUMBER found in config. Aborting security review.")
        return False

    try:
        # -- Fetch diff ------------------------------------------------------
        diff: str = github_api.get_pr_diff(pr_number)

        if not diff:
            logger.info(f"PR #{pr_number} has an empty diff. Skipping security review.")
            return False

        # -- Truncate diff if necessary --------------------------------------
        truncated = False
        original_length = len(diff)
        if original_length > _MAX_DIFF_LENGTH:
            diff = diff[:_MAX_DIFF_LENGTH]
            truncated = True
            logger.info(
                f"Diff for PR #{pr_number} truncated from {original_length:,} "
                f"to {_MAX_DIFF_LENGTH:,} characters for security review."
            )

        logger.info(f"Running security review on PR #{pr_number}.")

        # -- Ask Gemini to scan ----------------------------------------------
        prompt = _SECURITY_PROMPT.format(diff=diff)
        response: str = gemini_client.generate(prompt).strip()

        if not response:
            logger.warning(
                f"Gemini returned an empty response for security review "
                f"of PR #{pr_number}."
            )
            return False

        # -- Determine if issues were found ----------------------------------
        has_issues = "NO_SECURITY_ISSUES" not in response.upper()

        if has_issues:
            # -- Security issues found ---------------------------------------
            github_api.add_labels(pr_number, ["security"])
            logger.warning(
                f"Security issues detected in PR #{pr_number}. "
                f"Added 'security' label."
            )

            truncation_notice = ""
            if truncated:
                truncation_notice = (
                    "\n\n> ⚠️ _The diff was truncated due to size. "
                    "This review covers only the first "
                    f"{_MAX_DIFF_LENGTH:,} characters of the diff. "
                    "A full manual review is recommended._\n"
                )

            comment_body = (
                f"🔒 **AI Security Review**\n\n"
                f"{response}"
                f"{truncation_notice}\n\n"
                f"_Maintainers, please review the flagged issues above "
                f"before merging._\n\n{config.BOT_FOOTER}"
            )
        else:
            # -- No security issues ------------------------------------------
            logger.info(f"No security concerns detected in PR #{pr_number}.")

            comment_body = (
                f"🔒 **AI Security Review**\n\n"
                f"✅ No security concerns were detected in this pull request.\n\n"
                f"_This is an automated scan. A manual review is still "
                f"recommended for critical changes._\n\n{config.BOT_FOOTER}"
            )

        github_api.add_comment(pr_number, comment_body)
        logger.info(f"Posted security review comment on PR #{pr_number}.")

        return has_issues

    except Exception:
        logger.exception(
            f"Failed to run security review for PR #{pr_number}."
        )
        raise
