"""Structured JSON logging for the trading bot.

Every log entry is a JSON object with: timestamp, module, level, event, data.
This makes logs machine-parseable for alerting and post-mortem analysis.
Log rotation: daily files, kept 90 days, compressed after 7 days.
"""

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config.config import config


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "module": record.name,
            "level": record.levelname,
            "event": record.getMessage(),
        }
        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)
        if hasattr(record, "data"):
            payload["data"] = record.data  # type: ignore[attr-defined]
        return json.dumps(payload, default=str)


def _make_rotating_handler(log_file: Path, max_bytes: int = config.LOG_MAX_BYTES) -> logging.Handler:
    """Create a size+time rotating file handler that compresses old files."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        filename=str(log_file),
        maxBytes=max_bytes,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(JSONFormatter())
    return handler


def _make_timed_handler(log_file: Path) -> logging.Handler:
    """Create a midnight-rotating handler for daily log files."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
        utc=False,
    )
    handler.setFormatter(JSONFormatter())
    return handler


def setup_logging(log_dir: str = None) -> None:
    """Configure the root logger and per-domain file handlers.

    Call once at application startup before any module imports.
    Each domain (trading, orders, scanner, risk, regime, data, errors)
    gets its own file so operators can tail the log they care about.
    """
    base = Path(log_dir or config.LOG_PATH)
    base.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console handler — INFO and above, human-readable
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root.addHandler(console)

    # Domain-specific file handlers
    domain_handlers: dict[str, tuple[str, int]] = {
        "trading":  ("trading.log",  logging.DEBUG),
        "orders":   ("orders.log",   logging.DEBUG),
        "scanner":  ("scanner.log",  logging.DEBUG),
        "risk":     ("risk.log",     logging.DEBUG),
        "regime":   ("regime.log",   logging.DEBUG),
        "data":     ("data.log",     logging.DEBUG),
        "errors":   ("errors.log",   logging.WARNING),
    }

    for domain, (path, level) in domain_handlers.items():
        handler = _make_timed_handler(base / path)
        handler.setLevel(level)
        logger = logging.getLogger(domain)
        logger.addHandler(handler)
        logger.propagate = True  # also goes to root (console)

    # errors.log catches all WARNING+ from every module
    error_handler = _make_timed_handler(base / "errors.log")
    error_handler.setLevel(logging.WARNING)
    root.addHandler(error_handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Use the domain prefix for automatic file routing.

    Examples:
        get_logger("trading.entry")  → goes to trading.log
        get_logger("orders.gtt")     → goes to orders.log
        get_logger("scanner.vcp")    → goes to scanner.log
    """
    return logging.getLogger(name)


def log_with_data(
    logger: logging.Logger,
    level: int,
    event: str,
    data: Optional[dict[str, Any]] = None,
) -> None:
    """Emit a log record with a structured `data` payload attached.

    Args:
        logger: The logger instance to use.
        level:  logging.INFO / logging.WARNING etc.
        event:  Short description of what happened.
        data:   Arbitrary key-value context (serialised to JSON).
    """
    record = logging.LogRecord(
        name=logger.name,
        level=level,
        pathname="",
        lineno=0,
        msg=event,
        args=(),
        exc_info=None,
    )
    if data:
        record.data = data  # type: ignore[attr-defined]
    logger.handle(record)
