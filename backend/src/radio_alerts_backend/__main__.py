from __future__ import annotations

import sys

import uvicorn

from .app import create_app
from .config import load_settings
from .errors import ConfigError
from .logging_setup import setup_logging


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuración inválida: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.log_dir)
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.api_port,
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
