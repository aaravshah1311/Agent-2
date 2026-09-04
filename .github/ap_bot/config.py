# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Configuration Module.

Centralizes all environment variable loading, constants, label definitions,
and configuration validation for the AP Bot system.
"""

import os
from typing import Dict, List, Optional

from dotenv import load_dotenv

from .logger import logger

# ---------------------------------------------------------------------------
# Load environment variables from .env file (if present, for local dev)
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# Environment Variables
# ---------------------------------------------------------------------------
def _env_int(name: str) -> Optional[int]:
    """Read an environment variable as a positive integer.

    Returns ``None`` when the variable is unset, empty, non-numeric, or
    not positive.  This keeps a malformed value (e.g. an empty string
    injected by a GitHub Actions expression) from crashing the whole
    bot at import time.

    Args:
        name: The environment variable name to read.

    Returns:
        The parsed integer, or ``None`` if it is missing or invalid.
    """
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Environment variable '%s' is not an integer: %r", name, raw)
        return None
    return value or None


GITHUB_TOKEN: Optional[str] = os.getenv("GITHUB_TOKEN")
GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")
REPO_NAME: Optional[str] = os.getenv("REPO_NAME") or os.getenv("GITHUB_REPOSITORY")
ISSUE_NUMBER: Optional[int] = _env_int("ISSUE_NUMBER")
PR_NUMBER: Optional[int] = _env_int("PR_NUMBER")
EVENT_ACTION: Optional[str] = os.getenv("EVENT_ACTION")
TAG_NAME: Optional[str] = os.getenv("TAG_NAME")
PR_AUTHOR: Optional[str] = os.getenv("PR_AUTHOR")

# ---------------------------------------------------------------------------
# API & Model Constants
# ---------------------------------------------------------------------------
GITHUB_API_BASE: str = "https://api.github.com"
GEMINI_MODEL: str = "gemini-2.5-flash-lite"
STALE_DAYS: int = 180
GRACE_PERIOD_DAYS: int = 7
MAX_SUMMARY_LENGTH: int = 500

# ---------------------------------------------------------------------------
# Bot Footer
# ---------------------------------------------------------------------------
BOT_FOOTER: str = (
    "\n\n---\n"
    "🤖 *This is an automated reply by **AP Automated Bot**. "
    "It can make mistakes. Human maintainers have the final decision.*"
)

# ---------------------------------------------------------------------------
# Label Definitions  (label_name -> hex color without '#')
# ---------------------------------------------------------------------------
LABELS: Dict[str, str] = {
    # Issue type labels
    "bug": "#d73a4a",
    "feature": "#a2eeef",
    "documentation": "#0075ca",
    "security": "#e4e669",
    "question": "#d876e3",
    "enhancement": "#a2eeef",
    # Component labels
    "backend": "#1d76db",
    "frontend": "#f9d0c4",
    "api": "#bfd4f2",
    "database": "#c2e0c6",
    "performance": "#ff9f1c",
    "ui": "#f9d0c4",
    "ai-ml": "#8957e5",
    "devops": "#0e8a16",
    # Workflow labels
    "needs-info": "#ffffff",
    "duplicate": "#cfd3d7",
    "possible-spam": "#b60205",
    "help-wanted": "#008672",
    "good-first-issue": "#7057ff",
    # Priority labels
    "critical": "#b60205",
    "high-priority": "#d93f0b",
    "medium-priority": "#fbca04",
    "low-priority": "#0e8a16",
    # Status labels
    "stale": "#ededed",
    # PR type labels
    "bug-fix": "#d73a4a",
    "new-feature": "#a2eeef",
    "refactor": "#1d76db",
    "testing": "#bfd4f2",
    "dependency-update": "#0366d6",
    "ui-improvement": "#f9d0c4",
}


def validate_required_vars(required: Optional[List[str]] = None) -> bool:
    """Validate that all required environment variables are set.

    Checks each variable in *required* and logs an error for every one
    that is missing or empty.  Returns ``True`` only when **all**
    variables are present.

    Args:
        required: List of environment variable names to validate.
                  Defaults to ``["GITHUB_TOKEN", "REPO_NAME"]`` when
                  not provided.

    Returns:
        ``True`` if all required variables are set, ``False`` otherwise.
    """
    if required is None:
        required = ["GITHUB_TOKEN", "REPO_NAME"]

    missing: List[str] = []

    for var in required:
        value = os.getenv(var)
        if not value:
            missing.append(var)
            logger.error("Required environment variable '%s' is not set.", var)

    if missing:
        logger.error(
            "Missing required environment variables: %s", ", ".join(missing)
        )
        return False

    logger.info("All required environment variables are present.")
    return True


# Self-reference for module import compatibility (e.g. from ..config import config)
import sys
config = sys.modules[__name__]

