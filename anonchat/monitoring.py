"""Неблокирующая доставка копий чатов администраторам."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from . import texts
from .actions import DeliveryResult, send_copy_to, send_to

_CACHE_UNTIL = 0.0
_CACHE_IDS: tuple[int, ...] = ()
_PENDING: set[asyncio.Task] = set()
_SEMAPHORE = asyncio.Semaphore(4)
_MAX_PENDING = 100


def invalidate_monitor_cache() -> None:
    global _CACHE_UNTIL, _CACHE_IDS
    _CACHE_UNTIL = 0.0
    _CACHE_IDS = ()


async def _monitor_ids(db, owner_ids: tuple[int, ...]) -> tuple[int, ...]:
    global _CACHE_UNTIL, _CACHE_IDS
    now_mono = time.monotonic()
    if now_mono < _CACHE_UNTIL:
        return _CACHE_IDS
    candidates = await db.admin_ids_with_permission("monitor", owner_ids)
    enabled: list[int] = []
    for admin_id in candidates:
        if await db.get_kv(f"chat_monitor:{admin_id}") == "1":
            enabled.append(int(admin_id))
    _CACHE_IDS = tuple(enabled)
    _CACHE_UNTIL = now_mono + 15
    return _CACHE_IDS


def _identity(row: Any, user_id: int) -> str:
    username = f"@{row['username']}" if row is not None and row["username"] else "без username"
    nickname = row["nickname"] if row is not None and row["nickname"] else f"Аноним-{user_id}"
    return f"{texts.esc(username)} · {texts.esc(nickname)} · <code>{int(user_id)}</code>"


async def _deliver(message, bot, pack, monitor_ids: tuple[int, ...], header: str) -> None:
    async with _SEMAPHORE:
        for admin_id in monitor_ids:
            if message.text:
                await send_to(
                    bot, admin_id, f"{header}\n\n{texts.esc(message.text)}", None, pack
                )
            else:
                result = await send_to(bot, admin_id, header, None, pack)
                if result is DeliveryResult.DELIVERED:
                    await send_copy_to(bot, message, admin_id)


async def enqueue_chat_monitor(message, ctx, partner_id: int) -> None:
    """Ставит monitor-copy в ограниченный фон, не тормозя основной диалог."""
    ids = await _monitor_ids(ctx.db, ctx.cfg.admin_ids)
    if not ids or len(_PENDING) >= _MAX_PENDING:
        return

    sender = ctx.me or await ctx.db.get_user(ctx.user_id)
    partner = await ctx.db.get_user(int(partner_id))
    header = (
        "👁 <b>Сообщение в активном чате</b>\n"
        f"От: {_identity(sender, ctx.user_id)}\n"
        f"Собеседник: {_identity(partner, int(partner_id))}"
    )
    task = asyncio.create_task(_deliver(message, ctx.bot, ctx.pack, ids, header))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    # Отдаём задаче один такт event loop, но не ждём Telegram API.
    await asyncio.sleep(0)


def pending_count() -> int:
    return len(_PENDING)
