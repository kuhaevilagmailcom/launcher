"""Быстрый старт одной командой: python dev.py — сам поднимает .venv и ставит зависимости.

Нужен только Python 3.11+ в PATH. Для продакшена по-прежнему `pip install -r requirements.txt`
и `python main.py` (см. README).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
BIN = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(*cmd: str) -> int:
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def ensure_venv() -> None:
    if not BIN.exists():
        print("📦 создаю виртуальное окружение…")
        if run(sys.executable, "-m", "venv", str(VENV)) != 0:
            sys.exit("не удалось создать .venv")
    print("📦 проверяю зависимости…")
    if run(str(BIN), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", "requirements.txt") != 0:
        sys.exit("pip install упал — посмотри вывод выше")


def main() -> int:
    if (ROOT / ".env").exists():
        print("🔑 .env найден")
    else:
        sys.exit("нет .env — скопируй .env.example в .env и впиши BOT_TOKEN (и ADMIN_IDS)")
    ensure_venv()
    print("🧲 поднимаю бота (Ctrl+C — остановить)…")
    return run(str(BIN), "main.py")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nостановили")
