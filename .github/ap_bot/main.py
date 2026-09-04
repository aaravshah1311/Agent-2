# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Entry Point Dispatcher.

This module serves as the single entry point for all AP Bot operations.
It parses the requested module name from ``sys.argv[1]``, initialises
shared clients (GitHub API, Gemini), and dispatches execution to the
appropriate handler module.

Usage::

    python -m ap_bot.main <module_name>

Example::

    python -m ap_bot.main welcome
    python -m ap_bot.main issue_review
    python -m ap_bot.main pr_review
"""

import sys
from typing import Callable, Dict, Optional

from .config import (
    GEMINI_API_KEY,
    GITHUB_TOKEN,
    REPO_NAME,
    validate_required_vars,
)
from .gemini import GeminiClient
from .github_api import GitHubAPI
from .logger import logger


# Type alias for handler functions
HandlerFunc = Callable[[GitHubAPI, GeminiClient], None]


def _run_pipeline(
    steps: list,
    github_api: GitHubAPI,
    gemini_client: GeminiClient,
) -> None:
    """Run a sequence of review steps, isolating failures.

    Each step is executed independently: if one raises, the error is
    logged and the remaining steps still run.  This prevents a single
    failing module (e.g. a transient Gemini error during code review)
    from silently skipping later steps such as summary generation.

    Args:
        steps: A list of ``(name, callable)`` tuples to execute in order.
        github_api: Authenticated GitHub API wrapper instance.
        gemini_client: Gemini AI client.
    """
    failures = 0
    for name, step in steps:
        try:
            step(github_api, gemini_client)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            logger.error(
                "Pipeline step '%s' failed: %s", name, exc, exc_info=True
            )

    if failures:
        logger.warning(
            "%d of %d pipeline step(s) failed. See logs above.",
            failures,
            len(steps),
        )


def _get_handler(module_name: str) -> Optional[HandlerFunc]:
    """Resolve a module name to its handler function.

    Performs lazy imports to avoid loading every module at startup.
    This keeps cold-start times low and avoids import errors for
    modules that aren't needed for the current invocation.

    Args:
        module_name: The module identifier passed via CLI.

    Returns:
        The handler function, or ``None`` if the module is unknown.
    """
    try:
        if module_name == "welcome":
            from .modules.welcome import run
            return run

        elif module_name == "issue_review":
            def _issue_review_handler(
                github_api: GitHubAPI, gemini_client: GeminiClient
            ) -> None:
                """Run the full issue review pipeline."""
                from .modules.issue_classifier import run as classify_issue
                from .modules.summary_generator import run as generate_summary
                from .modules.priority_detector import run as detect_priority

                _run_pipeline(
                    [
                        ("issue_classifier", classify_issue),
                        ("summary_generator", generate_summary),
                        ("priority_detector", detect_priority),
                    ],
                    github_api,
                    gemini_client,
                )

            return _issue_review_handler

        elif module_name == "pr_review":
            def _pr_review_handler(
                github_api: GitHubAPI, gemini_client: GeminiClient
            ) -> None:
                """Run the full PR review pipeline."""
                from .modules.pr_classifier import run as classify_pr
                from .modules.ai_code_review import run as review_code
                from .modules.security_review import run as review_security
                from .modules.dependency_checker import run as review_deps
                from .modules.summary_generator import run as generate_summary

                _run_pipeline(
                    [
                        ("pr_classifier", classify_pr),
                        ("ai_code_review", review_code),
                        ("security_review", review_security),
                        ("dependency_checker", review_deps),
                        ("summary_generator", generate_summary),
                    ],
                    github_api,
                    gemini_client,
                )

            return _pr_review_handler

        elif module_name == "issue_labeler":
            from .modules.issue_classifier import run
            return run

        elif module_name == "duplicate_check":
            from .modules.duplicate_detector import run
            return run

        elif module_name == "dependency_check":
            from .modules.dependency_checker import run
            return run

        elif module_name == "label_sync":
            from .modules.label_manager import run
            return run

        elif module_name == "spam_check":
            from .modules.spam_detector import run
            return run

        elif module_name == "stale_manager":
            from .modules.stale_manager import run
            return run

        elif module_name == "documentation_checker":
            from .modules.documentation_checker import run
            return run

        elif module_name == "statistics":
            from .modules.statistics import run
            return run

        elif module_name == "contributor_manager":
            from .modules.contributor_manager import run
            return run

        elif module_name == "release_notes":
            from .modules.release_notes import run
            return run

        elif module_name == "auto_close":
            from .modules.stale_manager import auto_close
            return auto_close

        elif module_name == "scheduler":
            from .modules.helper import run_scheduler
            return run_scheduler

        else:
            logger.error("Unknown module: '%s'", module_name)
            return None

    except ImportError as exc:
        logger.error(
            "Failed to import handler for module '%s': %s",
            module_name,
            exc,
        )
        return None


def main() -> None:
    """Parse CLI arguments, initialise clients, and dispatch to the handler."""
    if len(sys.argv) < 2:
        logger.error("Usage: python -m ap_bot.main <module_name>")
        logger.error(
            "Available modules: welcome, issue_review, pr_review, "
            "issue_labeler, duplicate_check, dependency_check, label_sync, "
            "spam_check, stale_manager, documentation_checker, statistics, "
            "contributor_manager, release_notes, auto_close, scheduler"
        )
        sys.exit(1)

    module_name: str = sys.argv[1].strip().lower()
    logger.info("AP Bot dispatcher started — module: '%s'", module_name)

    # ------------------------------------------------------------------
    # Validate required environment variables
    # ------------------------------------------------------------------
    required_vars = ["GITHUB_TOKEN", "REPO_NAME"]

    # Gemini API key is required for most modules
    modules_without_gemini = {
        "welcome",
        "stale_manager",
        "auto_close",
        "scheduler",
        "label_sync",
        "statistics",
        "documentation_checker",
    }
    if module_name not in modules_without_gemini:
        required_vars.append("GEMINI_API_KEY")

    if not validate_required_vars(required_vars):
        logger.error("Environment validation failed. Exiting.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Initialise shared clients
    # ------------------------------------------------------------------
    github_api = GitHubAPI(
        token=GITHUB_TOKEN,  # type: ignore[arg-type]
        repo=REPO_NAME,      # type: ignore[arg-type]
    )

    gemini_client: Optional[GeminiClient] = None
    if GEMINI_API_KEY:
        gemini_client = GeminiClient(api_key=GEMINI_API_KEY)
    else:
        logger.warning(
            "GEMINI_API_KEY not set — Gemini features will be unavailable."
        )

    # ------------------------------------------------------------------
    # Resolve and execute the handler
    # ------------------------------------------------------------------
    handler = _get_handler(module_name)
    if handler is None:
        logger.error("No handler found for module '%s'. Exiting.", module_name)
        sys.exit(1)

    try:
        logger.info("Executing module '%s'…", module_name)
        handler(github_api, gemini_client)  # type: ignore[arg-type]
        logger.info("Module '%s' completed successfully.", module_name)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Module '%s' failed with error: %s", module_name, exc, exc_info=True
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
