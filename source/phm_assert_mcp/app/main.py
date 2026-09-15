from __future__ import annotations

import asyncio
import logging
import sys

from pydantic import ValidationError

from app.config import get_settings
from app.db import preflight_database
from app.logging_config import configure_logging
from app.mcp.server import build_mcp

logger = logging.getLogger(__name__)


def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    configure_logging(settings.log_level)
    try:
        result = asyncio.run(preflight_database(settings))
        logger.info("startup_preflight_ok", extra={"fields": result})
    except Exception as exc:
        logger.exception("startup_preflight_failed")
        print(f"Startup failed: PostgreSQL/schema preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc

    mcp = build_mcp(settings)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
