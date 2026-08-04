"""JSON logging for the service.

One JSON object per line, from both structlog calls and the stdlib loggers
(uvicorn's access/error logs) — everything goes through the same renderer.
Anything bound with ``structlog.contextvars.bind_contextvars`` at the request
boundary rides along on every line emitted while handling that request.
"""

import logging
import os

import structlog

_SHARED = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def setup_logging(level: str | None = None) -> None:
    """Route structlog and stdlib logging into one JSON stream on stderr.

    Safe to call after uvicorn has configured logging — the ``uvicorn`` CLI does
    so before importing the app — since it replaces the root handlers and hands
    uvicorn's own loggers back to propagation. Level from ``CBAC_SERVICE_LOG_LEVEL``.
    """
    structlog.configure(
        processors=[*_SHARED, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_SHARED,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    logging.basicConfig(
        level=level or os.environ.get("CBAC_SERVICE_LOG_LEVEL", "INFO").upper(),
        handlers=[handler],
        force=True,
    )
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
