from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import FilteringBoundLogger

LOG_FORMAT_JSON = "json"
LOG_FORMAT_TEXT = "text"
supported_log_formats = [LOG_FORMAT_JSON, LOG_FORMAT_TEXT]


def configure_logging(log_level: str = "INFO", log_format: str = LOG_FORMAT_JSON) -> None:
    if log_format not in supported_log_formats:
        raise ValueError(
            f"unsupported log_format: {log_format}. Supported formats: {supported_log_formats}"
        )

    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"unsupported log_level: {log_level}")

    renderer = (
        structlog.processors.JSONRenderer()
        if log_format == LOG_FORMAT_JSON
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "agentdna") -> FilteringBoundLogger:
    return structlog.get_logger(name)
