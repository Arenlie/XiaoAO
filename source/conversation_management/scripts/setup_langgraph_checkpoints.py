from __future__ import annotations

import asyncio

from app.config import get_settings
from app.orchestration.checkpoint import open_checkpointer


async def main() -> None:
    settings = get_settings()
    settings.langgraph_checkpoint_enabled = True
    settings.langgraph_checkpoint_setup_on_start = True
    async with open_checkpointer(settings):
        print("LangGraph checkpoint tables are ready")


if __name__ == "__main__":
    asyncio.run(main())
