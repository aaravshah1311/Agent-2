# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
AP Bot — Structured Logger Module.

Provides a consistent, structured logging setup for all AP Bot modules.
Logs are directed to stdout for seamless GitHub Actions log capture.
"""

import logging
import sys
from typing import Optional


def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Create and configure a structured logger instance.

    Sets up a logger with a consistent format that includes timestamps,
    module names, and log levels. Output is directed to stdout so that
    GitHub Actions can capture and display logs in real time.

    Args:
        name: The name for the logger (typically the module name).
        level: The logging level threshold. Defaults to logging.INFO.

    Returns:
        A configured logging.Logger instance.
    """
    logger_instance = logging.getLogger(name)

    # Avoid adding duplicate handlers if the logger already exists
    if logger_instance.handlers:
        return logger_instance

    logger_instance.setLevel(level)

    # Stream handler targeting stdout for GitHub Actions compatibility
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger_instance.addHandler(handler)

    # Prevent log propagation to the root logger to avoid duplicate output
    logger_instance.propagate = False

    return logger_instance


# Default logger instance for the ap_bot package
logger: logging.Logger = setup_logger("ap_bot")
