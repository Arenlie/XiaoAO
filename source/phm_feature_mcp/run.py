from __future__ import annotations

import uvicorn

from app.config import get_settings


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.server:app",
        host=settings.host,
        port=settings.port,
        workers=settings.web_concurrency,
        log_level=settings.log_level.lower(),
        access_log=True,
        proxy_headers=True,
    )
