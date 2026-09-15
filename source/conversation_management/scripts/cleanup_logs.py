from __future__ import annotations

import time
from pathlib import Path

import structlog

from app.config import get_settings
from app.logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings, component="log-cleanup")
    log = structlog.get_logger(__name__)
    root = Path(settings.log_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - max(1, settings.log_retention_days) * 86400
    removed = 0
    released_bytes = 0

    for path in root.glob("*.log.*"):
        try:
            stat = path.stat()
            if stat.st_mtime < cutoff:
                released_bytes += stat.st_size
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("log_cleanup_file_failed", path=str(path), error=str(exc))

    log.info(
        "log_cleanup_completed",
        log_dir=str(root.resolve()),
        retention_days=settings.log_retention_days,
        removed_files=removed,
        released_bytes=released_bytes,
    )


if __name__ == "__main__":
    main()
