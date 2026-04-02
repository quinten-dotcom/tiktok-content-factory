"""
TikTok Content Factory — Centralized Logging Configuration

All modules should use:
    from log_config import get_logger
    logger = get_logger(__name__)

Logs are written to both console (colorized) and a rotating file.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path


# ── Defaults (override via env vars) ─────────────────────────────────────────
LOG_LEVEL = os.environ.get("TIKTOK_LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(os.environ.get("TIKTOK_LOG_DIR", Path(__file__).resolve().parent.parent / "logs"))
LOG_FILE = LOG_DIR / "tiktok_factory.log"
MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB per file
BACKUP_COUNT = 5                  # Keep 5 rotated files


# ── Formatters ────────────────────────────────────────────────────────────────
_CONSOLE_FMT = "%(asctime)s  %(levelname)-7s  [%(name)s]  %(message)s"
_FILE_FMT = "%(asctime)s  %(levelname)-7s  [%(name)s:%(funcName)s:%(lineno)d]  %(message)s"
_DATE_FMT = "%H:%M:%S"
_FILE_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _ensure_log_dir() -> None:
    """Create the log directory if it doesn't exist."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # Fall back to console-only if we can't write logs


_root_configured = False


def _configure_root() -> None:
    """One-time setup of the root 'tiktok' logger hierarchy."""
    global _root_configured
    if _root_configured:
        return
    _root_configured = True

    root = logging.getLogger("tiktok")
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Prevent duplicate handlers on reload
    if root.handlers:
        return

    # ── Console handler ──────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt=_DATE_FMT))
    root.addHandler(console)

    # ── File handler (rotating) ──────────────────────────────────────────
    _ensure_log_dir()
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=MAX_LOG_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_FILE_DATE_FMT))
        root.addHandler(fh)
    except OSError:
        root.warning("Could not create log file at %s — logging to console only", LOG_FILE)

    # Don't propagate to Python root logger
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """
    Return a logger under the 'tiktok' namespace.

    Usage:
        from log_config import get_logger
        logger = get_logger(__name__)
        logger.info("Starting pipeline for %s", app_slug)
        logger.error("Upload failed: %s", err, exc_info=True)
    """
    _configure_root()
    # Nest under 'tiktok' so all loggers share the same handlers
    if name.startswith("tiktok."):
        return logging.getLogger(name)
    return logging.getLogger(f"tiktok.{name}")
