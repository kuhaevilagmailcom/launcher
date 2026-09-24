"""Совместимость по именам: `python run.py` == `python main.py`.

Основная точка входа — main.py. Этот файл просто делегирует ему, чтобы старые
инструкции, systemd-юниты и Docker-команды не пришлось переписывать.
"""

from __future__ import annotations

import asyncio
import logging

from main import ADMIN_COMMANDS, COMMANDS, build, janitor, main, register_commands

__all__ = ["COMMANDS", "ADMIN_COMMANDS", "build", "janitor", "register_commands", "main"]

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger("anonchat").info("Остановили — пока из МГН!")
