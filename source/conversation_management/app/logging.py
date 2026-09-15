from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from app.config import Settings


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-.")
    return cleaned or "app"


def configure_logging(
    settings_or_level: Settings | str,
    component: str | None = None,
) -> Path | None:
    """Configure JSON console logging and bounded local rotating files.

    Each systemd process writes to an independent file, for example api.log,
    worker-1.log and worker-2.log. RotatingFileHandler deletes the oldest
    backup automatically once ``log_backup_count`` is exceeded.
    """

    if isinstance(settings_or_level, Settings):
        settings = settings_or_level
        level_name = settings.log_level.upper()
        file_enabled = settings.log_file_enabled
        console_enabled = settings.log_console_enabled
        log_dir = Path(settings.log_dir).expanduser()
        max_bytes = max(1_048_576, int(settings.log_max_bytes))
        backup_count = max(1, int(settings.log_backup_count))
    else:
        settings = None
        level_name = str(settings_or_level).upper()
        file_enabled = False
        console_enabled = True
        log_dir = Path("./logs")
        max_bytes = 50 * 1024 * 1024
        backup_count = 10

    level = getattr(logging, level_name, logging.INFO)
    role = _safe_component(
        component
        or os.getenv("CONVERSATION_PROCESS_ROLE", "")
        or os.getenv("WORKER_INSTANCE", "")
        or "app"
    )

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer = structlog.processors.JSONRenderer(ensure_ascii=False)

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handlers: list[logging.Handler] = []
    log_path: Path | None = None
    if console_enabled:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(level)
        console.setFormatter(formatter)
        handlers.append(console)

    if file_enabled:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{role}.log"
        rotating = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        rotating.setLevel(level)
        rotating.setFormatter(formatter)
        handlers.append(rotating)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    for handler in handlers:
        root.addHandler(handler)

    # Let application, uvicorn, SQLAlchemy and Alembic records share the same
    # bounded handlers instead of opening additional unbounded files.
    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "sqlalchemy.engine",
        "alembic",
    ):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(level)

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return log_path
