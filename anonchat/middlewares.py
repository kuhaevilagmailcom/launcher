"""Мидлвари: доступ к БД в каждом хендлере + защита от флуда."""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from .actions import Ctx
from .config import Config
from .db import Database
from .pack import EmojiPack
from .runtime_state import touch as touch_presence


def event_user(event: TelegramObject) -> User | None:
    """Достаём пользователя из любого апдейта, не полагаясь на UserContextMiddleware."""
    if isinstance(event, Message):
        return event.from_user
    if isinstance(event, CallbackQuery):
        return event.from_user
    getter = getattr(event, "from_user", None)
    return getter if isinstance(getter, User) else None


class DataContext(BaseMiddleware):
    """Плюсует cfg/db/matchmaker в data и собирает готовый Ctx для хендлеров."""

    def __init__(self, config: Config, db: Database, matchmaker, pack=None) -> None:
        self.config = config
        self.db = db
        self.mm = matchmaker
        self.pack = pack or EmojiPack(config.emoji_pack_url)

    async def __call__(self, handler, event: TelegramObject, data: dict):
        data["cfg"] = self.config
        data["db"] = self.db
        data["mm"] = self.mm
        data["pack"] = self.pack
        data["is_admin"] = False
        data["is_new_user"] = False
        data["ctx"] = None
        data["me"] = None

        user = data.get("event_from_user") or event_user(event)
        me: Any = None
        if user is not None and not user.is_bot:
            me = await self.db.get_user(user.id)
            if me is None:
                data["is_new_user"] = True
            touch_presence(user.id)
            me = await self.db.ensure_user(
                user.id, user.username, user.first_name, existing=me
            )
            permissions = await self.db.get_admin_permissions(user.id, self.config.admin_ids)
            data["is_admin"] = bool(permissions)
        else:
            permissions = frozenset()
        data["me"] = me

        if isinstance(event, (Message, CallbackQuery)) and user is not None:
            data["ctx"] = Ctx(
                bot=data["bot"],
                db=self.db,
                mm=self.mm,
                cfg=self.config,
                pack=self.pack,
                event=event,
                user_id=user.id,
                me=me,
                admin_permissions=permissions,
            )
        try:
            return await handler(event, data)
        finally:
            # Даже если обработчик упал после изменения очереди/пары,
            # сохраняем фактическое состояние и не откатываемся после рестарта.
            self.db.schedule_matchmaker_save(self.mm)


class Throttling(BaseMiddleware):
    """Скользящее окно: не больше `limit` апдейтов в минуту на пользователя."""

    def __init__(
        self, config: Config | None = None, limit: int | None = None, window: float = 60.0
    ) -> None:
        self.config = config
        self.limit = max(5, limit) if limit is not None else None
        self.window = window
        self._hits: dict[tuple[int, str], deque[float]] = {}

    async def __call__(self, handler, event: TelegramObject, data: dict):
        cfg = self.config or data.get("cfg")
        user = event_user(event)
        if user is None or user.is_bot or (cfg and user.id in cfg.admin_ids):
            return await handler(event, data)

        is_menu = isinstance(event, CallbackQuery) or (
            isinstance(event, Message) and bool(event.text) and event.text.startswith("/")
        )
        limit = self.limit or (cfg.menu_rate_limit if is_menu else cfg.inchat_rate_limit)
        kind = "menu" if is_menu else "message"
        bucket = self._hits.setdefault((user.id, kind), deque())
        ts = time.monotonic()
        while bucket and ts - bucket[0] > self.window:
            bucket.popleft()

        if len(bucket) >= limit:
            if isinstance(event, CallbackQuery):
                await event.answer("Слишком много действий. Попробуй через несколько секунд.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Слишком много сообщений. Попробуй через несколько секунд.")
            return

        bucket.append(ts)
        if len(self._hits) > 5000:  # самочищаемся, чтобы не расти бесконечно
            cutoff = ts - self.window
            for key in [k for k, b in self._hits.items() if not b or b[-1] < cutoff]:
                self._hits.pop(key, None)
        return await handler(event, data)
