from __future__ import annotations

import uvicorn

from app.config import get_settings
from app.mcp_server import app


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)
