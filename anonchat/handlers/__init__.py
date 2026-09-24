"""Сборка роутеров. Порядок важен: состояния и экраны раньше, «пересылай всё» — самым последним."""

from __future__ import annotations

from aiogram import Router

from .admin import router as admin_router
from .chat import router as chat_router
from .games import router as games_router
from .menu import router as menu_router
from .polls import router as polls_router
from .reports import router as reports_router
from .settings import router as settings_router
from .support import router as support_router


def get_routers() -> list[Router]:
    return [
        settings_router,  # /profile, /settings, FSM ника
        support_router,   # добровольная поддержка через Telegram Stars
        reports_router,   # /report, FSM комментария жалобы
        games_router,     # игры внутри активного диалога
        admin_router,     # модерация
        polls_router,     # активный опрос дня
        menu_router,      # /start, кнопки меню, оценки
        chat_router,      # catch-all: пересылка сообщений собеседнику
    ]


__all__ = ["get_routers"]
