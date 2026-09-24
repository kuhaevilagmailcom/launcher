"""Точка входа: python main.py (нужен BOT_TOKEN в .env или в окружении).

Здесь только сборка приложения: конфиг → база → матчмейкер → бот → диспетчер →
мидлвары → роутеры → регистрация команд → polling. Вся логика разговора — в пакете `anonchat`.
"""

from __future__ import annotations

import asyncio
import time
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent, MenuButtonWebApp, WebAppInfo

from anonchat.actions import announce_pairs, refresh_live_menus
from anonchat.commands import ADMIN_COMMANDS, COMMANDS, register_common
from anonchat.config import Config
from anonchat.db import Database
from anonchat.handlers import get_routers
from anonchat.matching import Matchmaker
from anonchat.middlewares import DataContext, Throttling
from anonchat.miniapp_api import start_miniapp_server
from anonchat.pack import EmojiPack
from anonchat.diagnostics import METRICS
from anonchat.runtime_state import online_count as presence_online_count

log = logging.getLogger("anonchat")

__all__ = [
    "COMMANDS",
    "ADMIN_COMMANDS",
    "build",
    "janitor",
    "register_commands",
    "main",
]


async def reconcile_queue(
    bot: Bot, cfg: Config, db: Database, mm: Matchmaker, pack: EmojiPack
) -> int:
    """Сводит совместимых людей, которые уже стоят в бессрочной очереди."""
    pairs = mm.sweep()
    made = 0
    if pairs:
        made = await announce_pairs(bot, cfg, mm, pairs, pack, db)
        db.schedule_matchmaker_save(mm)
    return made


async def janitor(
    bot: Bot, cfg: Config, db: Database, mm: Matchmaker, pack: EmojiPack
) -> None:
    """Подчищает устаревшее состояние и регулярно пересобирает возможные пары."""
    last_maintenance = 0.0
    while True:
        try:
            await asyncio.sleep(60)
            mm.drop_stale_ratings()
            removed_games = await db.cleanup_stale_games()
            await db.cleanup_daily_activity()
            await db.online_peak(presence_online_count())
            if time.time() - last_maintenance >= 3600:
                await db.cleanup_report_context(cfg.report_context_retention_days)
                await db.cleanup_service_data()
                last_maintenance = time.time()
            METRICS.last_cleanup_at = int(time.time())
            METRICS.janitor_removed_games += int(removed_games)
            paired = await reconcile_queue(bot, cfg, db, mm, pack)
            if removed_games:
                log.info("game janitor: удалено неактивных игр=%s", removed_games)
            if paired:
                log.info("queue janitor: создано пар=%s", paired)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("janitor: что-то пошло не так, продолжаем")


async def menu_refresher(
    bot: Bot, mm: Matchmaker, pack: EmojiPack, db: Database | None = None
) -> None:
    """Редко обновляет только свежие открытые главные меню."""
    while True:
        try:
            await asyncio.sleep(30)
            await refresh_live_menus(bot, mm, pack, db)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("menu_refresher: ошибка обновления меню")


def build(cfg: Config) -> tuple[Bot, Dispatcher, Database, Matchmaker, EmojiPack]:
    """Собираем всё приложение одной функцией — удобно для тестов и вебхук-режима."""
    db = Database(cfg.db_path)
    mm = Matchmaker(queue_limit=cfg.queue_soft_limit)
    pack = EmojiPack(cfg.emoji_pack_url)
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    for observer in (dp.message, dp.edited_message, dp.callback_query):
        observer.outer_middleware(Throttling(cfg))
        observer.middleware(DataContext(cfg, db, mm, pack))
    dp.pre_checkout_query.middleware(DataContext(cfg, db, mm, pack))

    for router in get_routers():
        dp.include_router(router)

    @dp.error()
    async def on_error(event: ErrorEvent) -> None:  # pragma: no cover
        log.exception("Необработанная ошибка: %s", event.exception)

    return bot, dp, db, mm, pack


async def register_commands(bot: Bot, cfg: Config) -> None:
    """Общее меню — всем; модерационные команды только админам (и донастраиваются при /start)."""
    await register_common(bot, cfg)


async def main() -> None:  # pragma: no cover
    import sys

    token_arg = sys.argv[1] if len(sys.argv) > 1 else None
    cfg = Config.from_env(token_arg=token_arg)
    logging.basicConfig(
        level=logging.DEBUG if cfg.debug else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    bot, dp, database, mm, pack = build(cfg)
    await database.start()
    saved_matchmaker = await database.load_matchmaker()
    if saved_matchmaker:
        mm.restore(saved_matchmaker)
    await database.cleanup_report_context(cfg.report_context_retention_days)
    removed_games = await database.cleanup_stale_games()
    await database.cleanup_daily_activity()
    await database.cleanup_service_data()
    await database.online_peak(presence_online_count())
    METRICS.last_cleanup_at = int(time.time())
    METRICS.janitor_removed_games += int(removed_games)
    if removed_games:
        log.info("startup game cleanup: удалено неактивных игр=%s", removed_games)

    janitor_task: asyncio.Task | None = None
    menu_task: asyncio.Task | None = None
    miniapp_server = None

    @dp.startup()
    async def on_startup(bot: Bot) -> None:
        nonlocal janitor_task, menu_task, miniapp_server
        try:
            await bot.delete_webhook(drop_pending_updates=cfg.drop_pending_updates)
        except TelegramAPIError as exc:
            log.warning("delete_webhook не сработал: %s", exc)
        await register_commands(bot, cfg)
        if cfg.miniapp_enabled:
            try:
                miniapp_server = await start_miniapp_server(
                    bot, cfg, database, mm, pack
                )
                log.info("Mini App API запущен")
            except Exception:  # noqa: BLE001
                log.exception("Mini App API не запустился; polling бота продолжает работу")
            if cfg.miniapp_url.startswith("https://"):
                try:
                    await bot.set_chat_menu_button(
                        menu_button=MenuButtonWebApp(
                            text="Открыть АНОН МГН",
                            web_app=WebAppInfo(url=cfg.miniapp_url),
                        )
                    )
                    log.info("Mini App menu button: %s", cfg.miniapp_url)
                except TelegramAPIError:
                    log.exception("Не удалось обновить кнопку Mini App")
        progress_count = await pack.load_progress_bar(bot)
        if progress_count:
            log.info("progressBarEmoji: загружено %s состояний для профиля", progress_count)
        top_flags_count = await pack.load_top_flags(bot)
        if top_flags_count:
            log.info("FestiveFlags: загружено %s эмодзи для топа", top_flags_count)
        me = await bot.get_me()
        log.info("Анонимный чат %s запущен: @%s (id=%s)", cfg.city_short, me.username, me.id)
        if cfg.admin_ids:
            log.info("модераторы: %s · панель — командой /admin", cfg.admin_markup())
        else:
            log.warning("Администраторы не настроены.")
        paired = await reconcile_queue(bot, cfg, database, mm, pack)
        if paired:
            log.info("startup queue reconcile: создано пар=%s", paired)
        janitor_task = asyncio.create_task(janitor(bot, cfg, database, mm, pack))
        menu_task = asyncio.create_task(menu_refresher(bot, mm, pack, database))

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if janitor_task is not None:
            janitor_task.cancel()
        if menu_task is not None:
            menu_task.cancel()
        if miniapp_server is not None:
            await miniapp_server.stop()
        await database.flush_matchmaker(mm)
        await dp.storage.close()
        await bot.session.close()
        await database.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger("anonchat").info("Остановили — пока из МГН!")
