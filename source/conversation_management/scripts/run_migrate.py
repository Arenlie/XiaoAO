from __future__ import annotations

from alembic import command
from alembic.config import Config

from app.config import get_settings
from app.logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings, component="migrate")
    config = Config("alembic.ini")
    command.upgrade(config, "head")


if __name__ == "__main__":
    main()
