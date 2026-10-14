"""Log messages to the console and a daily file under <output>/_logs."""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

LOGGER_NAME = "vwapsb"


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.levelno >= logging.ERROR:
            return f"ERROR: {msg}"
        if record.levelno >= logging.WARNING:
            return f"WARNING: {msg}"
        return msg


def setup_logging(log_dir: Path, command: str) -> logging.Logger:
    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.propagate = False
    for stream in (sys.stdout, sys.stderr):  # The Windows console does not default to UTF-8.
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    con = logging.StreamHandler()
    con.setFormatter(_ConsoleFormatter())
    log.addHandler(con)
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_dir / f"vwapsb_{date.today()}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(fh)
    log.info("=== %s ===", command)
    return log


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
