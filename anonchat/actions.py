"""Действия бота: поиск пары, следующий, стоп, профиль, ник, настройки-экраны.

Хендлеры только разбирают апдейт и вызывают отсюда нужное действие — кнопка
«🔎 Поиск собеседника» и команда /connect делают буквально одно и то же.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    ReplyParameters,
)

from . import nick as nicklib
from . import texts
from .config import Config
from .db import Database, referral_day_start
from .keyboards import (
    back_menu_keyboard, chat_keyboard, menu_keyboard,
    profile_keyboard, rating_keyboard, referral_keyboard, top_keyboard,
    profile_section_keyboard, online_keyboard,
)
from .levels import rank_for
from .matching import Matchmaker
from .pack import EmojiPack
from .engagement import collect_progress_notifications, format_quests
from . import relay_state
from . import word_game as WG
from .runtime_state import online_count as presence_online_count

ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "menu"
log = logging.getLogger(__name__)

# Только память процесса: никаких записей message_id/онлайна в SQLite.
# Нужны для замены старого меню и обновления счётчика без мусора в БД.
_SCREEN_MESSAGES: dict[int, tuple[int, int]] = {}
_LIVE_MENUS: dict[int, tuple[int, int, str, bool, int, float]] = {}
_FILE_ID_CACHE: dict[str, str] = {}


class DeliveryResult(Enum):
    DELIVERED = "delivered"
    TEMP_ERROR = "temp_error"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class Ctx:
    bot: Bot
    db: Database
    mm: Matchmaker
    cfg: Config
    pack: EmojiPack
    event: Message | CallbackQuery
    user_id: int
    me: Any = None
    admin_permissions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.pack is None:
            self.pack = EmojiPack(self.cfg.emoji_pack_url)

    # ------------------------------------------------------------------ answers
    async def reply(self, text: str, markup: InlineKeyboardMarkup | None = None, **kw: Any) -> Message | None:
        target = self.event.message if isinstance(self.event, CallbackQuery) else self.event
        if target is None:
            return None
        return await _send_text(target, text, markup, self.pack, **kw)

    async def ack(self, text: str = "", alert: bool = False) -> None:
        if isinstance(self.event, CallbackQuery):
            try:
                await self.event.answer(text, show_alert=alert)
            except TelegramAPIError:
                pass

    async def edit(self, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
        if not isinstance(self.event, CallbackQuery) or self.event.message is None:
            return False
        message = self.event.message
        has_media = bool(getattr(message, "photo", None))
        for attempt in range(2):
            body = self.pack.wrap(text) if attempt == 0 else self.pack.strip(text)
            try:
                if has_media:
                    await message.edit_caption(caption=body, reply_markup=markup)
                else:
                    await message.edit_text(text=body, reply_markup=markup)
                return True
            except TelegramBadRequest as exc:
                if attempt == 0 and self.pack.accept(exc):
                    continue
                return False
            except TelegramAPIError:
                return False
        return False

    async def render_screen(
        self, image: str, caption: str, markup: InlineKeyboardMarkup | None = None,
        *, live_menu: bool = False,
    ) -> Message | None:
        """Держит один актуальный экран: старое меню удаляется, новое редактируется/заменяется."""
        if not live_menu:
            _LIVE_MENUS.pop(self.user_id, None)
        target = self.event.message if isinstance(self.event, CallbackQuery) else self.event
        if target is None:
            return None
        path = ASSET_DIR / image
        if not path.exists():
            if await self.edit(caption, markup):
                return target
            return await self.reply(caption, markup)

        key = f"menu_file_id:{image}"
        cached = _FILE_ID_CACHE.get(key, "")
        if not cached:
            cached = await self.db.get_kv(key)
            if cached:
                _FILE_ID_CACHE[key] = cached
        sources: list[str | FSInputFile] = ([cached] if cached else []) + [FSInputFile(path)]
        for source in sources:
            for wrapped in (True, False):
                body = self.pack.wrap(caption) if wrapped else self.pack.strip(caption)
                try:
                    if isinstance(self.event, CallbackQuery) and getattr(target, "photo", None):
                        result = await target.edit_media(
                            InputMediaPhoto(media=source, caption=body), reply_markup=markup
                        )
                    else:
                        result = await target.answer_photo(source, caption=body, reply_markup=markup)
                    final = result if isinstance(result, Message) else target
                    if isinstance(final, Message):
                        if final.photo:
                            new_file_id = final.photo[-1].file_id
                            if new_file_id != cached:
                                _FILE_ID_CACHE[key] = new_file_id
                                await self.db.set_kv(key, new_file_id)
                                cached = new_file_id
                        if (
                            isinstance(self.event, CallbackQuery)
                            and target.message_id != final.message_id
                        ):
                            try:
                                await self.bot.delete_message(target.chat.id, target.message_id)
                            except TelegramAPIError:
                                pass
                        await _remember_screen(self.bot, self.user_id, final)
                        if live_menu:
                            _LIVE_MENUS[self.user_id] = (
                                final.chat.id, final.message_id, self.nick, self.is_admin,
                                online_count(self.mm), time.monotonic(),
                            )
                    return final
                except TelegramBadRequest as exc:
                    if wrapped and self.pack.accept(exc):
                        continue
                    break
                except TelegramAPIError:
                    break
            if isinstance(source, str):
                _FILE_ID_CACHE.pop(key, None)
                await self.db.delete_kv(key)
        fallback = await self.reply(caption, markup)
        if fallback is not None:
            if (
                isinstance(self.event, CallbackQuery)
                and target.message_id != fallback.message_id
            ):
                try:
                    await self.bot.delete_message(target.chat.id, target.message_id)
                except TelegramAPIError:
                    pass
            await _remember_screen(self.bot, self.user_id, fallback)
            if live_menu:
                _LIVE_MENUS[self.user_id] = (
                    fallback.chat.id, fallback.message_id, self.nick, self.is_admin,
                    online_count(self.mm),
                )
        return fallback

    async def screen(
        self, image: str, caption: str, markup: InlineKeyboardMarkup | None = None
    ) -> Message | None:
        return await self.render_screen(image, caption, markup)

    # ------------------------------------------------------------------ профиль
    @property
    def is_admin(self) -> bool:
        return self.user_id in self.cfg.admin_ids or bool(self.admin_permissions)

    @property
    def is_owner(self) -> bool:
        return self.user_id in self.cfg.admin_ids

    def can(self, permission: str) -> bool:
        return self.is_owner or permission in self.admin_permissions

    @property
    def nick(self) -> str:
        return nicklib.display(
            self.me["nickname"] if self.me else "",
            self.user_id,
            self.me["support_stars"] if self.me else 0,
        )

    @property
    def prefs(self) -> dict[str, Any]:
        me = self.me
        return {
            "district": (me["district"] if me else "") or "",
            "same_district": bool(me["same_district"]) if me else False,
            "gender": (me["gender"] if me else "") or "",
            "looking_for": (me["looking_for"] if me else "") or "",
        }

    async def ensure_nick(self) -> str:
        """Авто-ник при первом же контакте — чтобы нигде не светилось настоящее имя."""
        if self.me is None:
            return self.nick
        if not (self.me["nickname"] or "").strip():
            auto = nicklib.auto_nick(self.user_id)
            for salt in range(32):
                candidate = nicklib.auto_nick(self.user_id + salt * 1_000_003)
                if await self.db.set_unique_nickname(self.user_id, candidate):
                    auto = candidate
                    break
            else:
                await self.db.set_profile(self.user_id, nickname=auto)
            self.me = await self.db.get_user(self.user_id)
        return self.nick

    # ------------------------------------------------------------------ guards
    async def restricted(self) -> bool:
        reason = await self.db.is_restricted(self.user_id)
        if reason == "banned":
            row = await self.db.get_user(self.user_id)
            why = (row["ban_reason"] if row else "") or "нарушение правил"
            await self.reply(
                texts.BANNED.format(city=texts.esc(self.cfg.city), reason=texts.esc(why)),
                markup=menu_keyboard(),
            )
            return True
        if reason == "muted":
            row = await self.db.get_user(self.user_id)
            mins = max(1, int(((row["mute_until"] if row else 0) - time.time()) // 60) + 1)
            await self.reply(
                texts.MUTED.format(mins=mins), markup=menu_keyboard()
            )
            return True
        return False

    async def dialog_locked(self) -> bool:
        """Пока идёт диалог, свои экраны (профиль, настройки, топ, ник) закрыты.

        Иначе человек посреди переписки уходит смотреть статистику, а собеседник
        остаётся с молчаливым «печатает…». Сначала /stop.
        """
        if self.mm.status(self.user_id) == "paired":
            await self.reply(texts.DIALOG_LOCKED)
            return True
        return False


# --------------------------------------------------------------------- low-level
async def _remember_screen(bot: Bot, user_id: int, message: Message) -> None:
    current = (message.chat.id, message.message_id)
    previous = _SCREEN_MESSAGES.get(user_id)
    _SCREEN_MESSAGES[user_id] = current
    if previous is None or previous == current:
        return
    try:
        await bot.delete_message(previous[0], previous[1])
    except TelegramAPIError:
        pass


def online_count(mm: Matchmaker | None = None) -> int:
    """Реальный онлайн: пользователи, взаимодействовавшие с ботом за последние 5 минут."""
    return presence_online_count()


async def refresh_live_menus(
    bot: Bot, mm: Matchmaker, pack: EmojiPack | None = None, db: Database | None = None
) -> None:
    """Редко обновляет только свежие главные меню, не устраивая массовый edit-шторм."""
    size = online_count(mm)
    poll_active = bool(await db.active_poll()) if db is not None else False
    now_mono = time.monotonic()
    edited = 0
    for user_id, item in list(_LIVE_MENUS.items()):
        chat_id, message_id, nickname, is_admin, previous_size, opened_at = item
        if now_mono - opened_at > 10 * 60:
            _LIVE_MENUS.pop(user_id, None)
            continue
        if previous_size == size:
            continue
        status = mm.status(user_id)
        if status != "free":
            _LIVE_MENUS.pop(user_id, None)
            continue
        if edited >= 50:
            break
        body = (
            f"<b>{texts.esc(nickname)}</b>\n\n"
            f"{texts.STATUS_FREE}\n\n"
            f"🟢 Онлайн сейчас: <b>{size}</b>"
        )
        markup = menu_keyboard("free", size, admin=is_admin, poll_active=poll_active)
        for attempt in range(2):
            wrapped = pack.wrap(body) if pack and attempt == 0 else (pack.strip(body) if pack else body)
            try:
                await bot.edit_message_caption(
                    chat_id=chat_id, message_id=message_id,
                    caption=wrapped, reply_markup=markup,
                )
                _LIVE_MENUS[user_id] = (
                    chat_id, message_id, nickname, is_admin, size, opened_at
                )
                edited += 1
                break
            except TelegramBadRequest as exc:
                if pack and attempt == 0 and pack.accept(exc):
                    continue
                _LIVE_MENUS.pop(user_id, None)
                break
            except TelegramAPIError:
                _LIVE_MENUS.pop(user_id, None)
                break


async def _send_text(
    target: Message, text: str, markup: InlineKeyboardMarkup | None, pack: EmojiPack, **kw: Any
) -> Message | None:
    for attempt in range(3):
        body = pack.wrap(text) if attempt == 0 else pack.strip(text)
        try:
            return await target.answer(text=body, reply_markup=markup, **kw)
        except TelegramBadRequest as exc:
            if attempt == 0 and pack.accept(exc):
                continue
            return None
    return None


async def send_to(
    bot: Bot,
    chat_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    pack: EmojiPack | None = None,
) -> DeliveryResult:
    if not chat_id:
        return DeliveryResult.UNAVAILABLE
    for attempt in range(2):
        body = pack.wrap(text) if (pack and attempt == 0) else text
        try:
            await bot.send_message(chat_id, body, reply_markup=markup)
            return DeliveryResult.DELIVERED
        except TelegramBadRequest as exc:
            if attempt == 0 and pack is not None and pack.accept(exc):
                continue
            message = str(exc).lower()
            return DeliveryResult.UNAVAILABLE if "chat not found" in message else DeliveryResult.TEMP_ERROR
        except TelegramRetryAfter as exc:
            await asyncio.sleep(max(0.0, float(exc.retry_after)))
            continue
        except TelegramForbiddenError:
            return DeliveryResult.UNAVAILABLE
        except TelegramAPIError:
            return DeliveryResult.TEMP_ERROR
    return DeliveryResult.TEMP_ERROR


async def send_copy_to_message(
    bot: Bot, message: Message, chat_id: int, reply_to_message_id: int | None = None
) -> tuple[DeliveryResult, Message | None]:
    """Пересылает сообщение и возвращает созданную копию для последующего редактирования.

    Фото отправляем напрямую по Telegram file_id. Остальные типы копируем средствами
    Telegram Bot API без forward, чтобы не раскрывать отправителя.
    """
    photo = getattr(message, "photo", None)
    reply_parameters = (
        ReplyParameters(message_id=int(reply_to_message_id))
        if reply_to_message_id
        else None
    )
    action = "upload_photo" if photo else "typing"
    try:
        await bot.send_chat_action(chat_id, action)
    except TelegramAPIError:
        pass

    for attempt in range(3):
        try:
            if photo:
                sent = await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo[-1].file_id,
                    caption=message.caption,
                    parse_mode=None,
                    caption_entities=message.caption_entities or None,
                    has_spoiler=bool(getattr(message, "has_media_spoiler", False)),
                    reply_parameters=reply_parameters,
                )
            else:
                sent = await message.send_copy(
                    chat_id=chat_id,
                    reply_parameters=reply_parameters,
                )
            return DeliveryResult.DELIVERED, sent if isinstance(sent, Message) else None
        except TelegramRetryAfter as exc:
            await asyncio.sleep(max(0.0, float(exc.retry_after)))
        except TelegramForbiddenError:
            return DeliveryResult.UNAVAILABLE, None
        except TelegramBadRequest as exc:
            error = str(exc).lower()
            log.warning(
                "relay bad request: type=%s chat_id=%s error=%s",
                message.content_type, chat_id, exc,
            )
            if "chat not found" in error or "user is deactivated" in error:
                return DeliveryResult.UNAVAILABLE, None
            # Повторяем: часть ошибок Telegram с медиа бывает кратковременной.
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
                continue
            return DeliveryResult.TEMP_ERROR, None
        except TelegramAPIError as exc:
            log.warning(
                "relay api error: type=%s chat_id=%s attempt=%s error=%s",
                message.content_type, chat_id, attempt + 1, exc,
            )
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
                continue
            return DeliveryResult.TEMP_ERROR, None
    return DeliveryResult.TEMP_ERROR, None


async def send_copy_to(bot: Bot, message: Message, chat_id: int) -> DeliveryResult:
    result, _ = await send_copy_to_message(bot, message, chat_id)
    return result


async def edit_copied_message(
    bot: Bot, message: Message, chat_id: int, message_id: int
) -> DeliveryResult:
    """Синхронизирует редактирование текста или подписи у уже отправленной копии."""
    try:
        if message.text is not None:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=message.text,
                parse_mode=None,
                entities=message.entities or None,
            )
        elif message.caption is not None or any((
            message.photo, message.video, message.animation,
            message.audio, message.document,
        )):
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=message.caption,
                parse_mode=None,
                caption_entities=message.caption_entities or None,
            )
        else:
            return DeliveryResult.TEMP_ERROR
        return DeliveryResult.DELIVERED
    except TelegramForbiddenError:
        return DeliveryResult.UNAVAILABLE
    except TelegramBadRequest as exc:
        error = str(exc).lower()
        # Одинаковый текст после повторного edit — это не ошибка доставки.
        if "message is not modified" in error:
            return DeliveryResult.DELIVERED
        if "chat not found" in error or "message to edit not found" in error:
            return DeliveryResult.UNAVAILABLE
        log.warning(
            "edit relay bad request: type=%s chat_id=%s message_id=%s error=%s",
            message.content_type, chat_id, message_id, exc,
        )
        return DeliveryResult.TEMP_ERROR
    except TelegramAPIError as exc:
        log.warning(
            "edit relay api error: type=%s chat_id=%s message_id=%s error=%s",
            message.content_type, chat_id, message_id, exc,
        )
        return DeliveryResult.TEMP_ERROR


async def send_screen_to(
    bot: Bot, chat_id: int, image: str, caption: str,
    markup: InlineKeyboardMarkup | None = None, pack: EmojiPack | None = None,
    db: Database | None = None,
) -> DeliveryResult:
    path = ASSET_DIR / image
    if not path.exists():
        return await send_to(bot, chat_id, caption, markup, pack)
    key = f"menu_file_id:{image}"
    cached = _FILE_ID_CACHE.get(key, "")
    if not cached and db:
        cached = await db.get_kv(key)
        if cached:
            _FILE_ID_CACHE[key] = cached
    photo: str | FSInputFile = cached or FSInputFile(path)
    for attempt in range(2):
        body = pack.wrap(caption) if pack and attempt == 0 else (pack.strip(caption) if pack else caption)
        try:
            sent = await bot.send_photo(chat_id, photo, caption=body, reply_markup=markup)
            if db and sent.photo:
                new_file_id = sent.photo[-1].file_id
                if new_file_id != cached:
                    _FILE_ID_CACHE[key] = new_file_id
                    await db.set_kv(key, new_file_id)
                    cached = new_file_id
            _LIVE_MENUS.pop(chat_id, None)
            await _remember_screen(bot, chat_id, sent)
            return DeliveryResult.DELIVERED
        except TelegramBadRequest as exc:
            if attempt == 0 and pack is not None and pack.accept(exc):
                continue
            if cached:
                _FILE_ID_CACHE.pop(key, None)
                await db.delete_kv(key)
                cached = ""
                photo = FSInputFile(path)
                continue
            message = str(exc).lower()
            return DeliveryResult.UNAVAILABLE if "chat not found" in message else DeliveryResult.TEMP_ERROR
        except TelegramRetryAfter as exc:
            await asyncio.sleep(max(0.0, float(exc.retry_after)))
        except TelegramForbiddenError:
            return DeliveryResult.UNAVAILABLE
        except TelegramAPIError:
            return DeliveryResult.TEMP_ERROR
    return DeliveryResult.TEMP_ERROR


# --------------------------------------------------------------------- экраны
async def show_menu(ctx: Ctx) -> None:
    status = ctx.mm.status(ctx.user_id)
    state = {
        "paired": texts.STATUS_PAIRED,
        "queued": texts.STATUS_QUEUED,
    }.get(status, texts.STATUS_FREE)

    body = (
        f"<b>{texts.esc(ctx.nick)}</b>\n\n"
        f"{state}\n\n"
        f"🟢 Онлайн сейчас: <b>{online_count(ctx.mm)}</b>"
    )
    poll_active = bool(await ctx.db.active_poll()) if status == "free" else False
    kb = menu_keyboard(
        status, ctx.mm.queue_size(), admin=ctx.is_admin, poll_active=poll_active
    )
    image = {"paired": "03_found.png", "queued": "02_search.png"}.get(status, "01_main_menu.png")
    await ctx.render_screen(image, body, kb, live_menu=(status == "free"))


async def show_welcome(ctx: Ctx) -> None:
    await ctx.ensure_nick()
    await show_menu(ctx)


async def show_help(ctx: Ctx) -> None:
    _LIVE_MENUS.pop(ctx.user_id, None)
    if await ctx.edit(texts.HELP, back_menu_keyboard()):
        return
    await ctx.reply(texts.HELP, markup=back_menu_keyboard())


async def show_rules(ctx: Ctx) -> None:
    await ctx.render_screen("06_rules.png", texts.RULES, back_menu_keyboard())


async def show_top(ctx: Ctx, period: str = "week") -> None:
    if await ctx.dialog_locked():
        return
    periods = {
        "week": (7, "Неделя"),
        "month": (30, "Месяц"),
        "all": (0, "Всё время"),
    }
    days, title = periods.get(period, periods["week"])
    rows = await ctx.db.top_period(days, 10)
    lines = [f"🏆 <b>Топ · {title}</b>", ""]
    if not rows:
        lines.append("Пока пусто.")
    else:
        for i, row in enumerate(rows, start=1):
            place = ctx.pack.top_flag(i) or f"<code>{i}</code>"
            lines.append(
                f"{place} <b>{texts.esc(nicklib.display(row['nickname'], int(row['user_id']), row['support_stars']))}</b>"
                f" · <b>{int(row['xp'] or 0)} ⭐</b>"
            )
    lines += ["", "<i>Ники участники придумывают сами.</i>"]
    await ctx.render_screen("08_top.png", "\n".join(lines), top_keyboard(period))


async def show_referral(ctx: Ctx) -> None:
    bot = await ctx.bot.me()
    link = f"https://t.me/{bot.username}?start=ref_{ctx.user_id}"
    invited, earned = await ctx.db.referral_stats(ctx.user_id)
    body = (
        "🎁 <b>Пригласить друга</b>\n\n"
        f"Приглашено: <b>{invited}</b>\n"
        f"Получено: <b>{earned} ⭐</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>"
    )
    await ctx.render_screen("04_profile.png", body, referral_keyboard(link))


async def show_activity(ctx: Ctx) -> None:
    if await ctx.dialog_locked():
        return
    today = await ctx.db.activity_totals(ctx.user_id, 1)
    week = await ctx.db.activity_totals(ctx.user_id, 7)
    month = await ctx.db.activity_totals(ctx.user_id, 30)
    engagement = await ctx.db.engagement_state(ctx.user_id)
    me = await ctx.db.get_user(ctx.user_id)
    body = (
        "📊 <b>Моя активность</b>\n\n"
        f"<b>Сегодня</b>\n"
        f"Диалогов: {today.get('dialogs', 0)} · сообщений: {today.get('messages', 0)} · игр: {today.get('games', 0)}\n\n"
        f"<b>За 7 дней</b>\n"
        f"Диалогов: {week.get('dialogs', 0)} · сообщений: {week.get('messages', 0)} · игр: {week.get('games', 0)}\n\n"
        f"<b>За 30 дней</b>\n"
        f"Диалогов: {month.get('dialogs', 0)} · сообщений: {month.get('messages', 0)} · игр: {month.get('games', 0)}\n\n"
        f"<b>Всего</b>\n"
        f"Диалогов: {int(engagement['dialogs_total'] or 0)} · сообщений: {int(me['messages'] or 0) if me else 0}\n"
        f"Хороших оценок: {int(me['good_ratings'] or 0) if me else 0} · игр: {int(engagement['games_total'] or 0)}"
    )
    await ctx.render_screen("04_profile.png", body, profile_section_keyboard())


async def show_streak(ctx: Ctx) -> None:
    if await ctx.dialog_locked():
        return
    row = await ctx.db.engagement_state(ctx.user_id)
    today = await ctx.db.activity_totals(ctx.user_id, 1)
    done = int(today.get("dialogs", 0)) > 0
    today_start = referral_day_start()
    last_day = int(row["last_active_day"] or 0)
    current = int(row["current_streak"] or 0)
    if last_day and last_day < today_start - 86_400:
        current = 0
    body = (
        "🔥 <b>Серия активности</b>\n\n"
        f"Текущая серия: <b>{current} дней</b>\n"
        f"Лучшая серия: <b>{int(row['best_streak'] or 0)} дней</b>\n"
        f"Сегодня: {'выполнено ✅' if done else 'ещё нет'}"
    )
    await ctx.render_screen("04_profile.png", body, profile_section_keyboard())


async def show_quests(ctx: Ctx) -> None:
    if await ctx.dialog_locked():
        return
    await ctx.render_screen(
        "04_profile.png",
        await format_quests(ctx.db, ctx.user_id),
        profile_section_keyboard(),
    )


async def show_online(ctx: Ctx) -> None:
    current = online_count(ctx.mm)
    chatting = ctx.mm.online_pairs() * 2
    queued = ctx.mm.queue_size()
    free = max(0, current - chatting - queued)
    peak = await ctx.db.online_peak(current)
    body = (
        "🟢 <b>Онлайн сейчас</b>\n\n"
        f"Всего: <b>{current}</b>\n"
        f"Общаются: <b>{chatting}</b>\n"
        f"Ищут собеседника: <b>{queued}</b>\n"
        f"Свободны: <b>{free}</b>\n"
        f"Пик сегодня: <b>{peak}</b>"
    )
    await ctx.render_screen("05_settings.png", body, online_keyboard())


async def show_profile(ctx: Ctx) -> None:
    if await ctx.dialog_locked():
        return
    if ctx.me is None:
        await ctx.reply(texts.PROFILE_MISSING)
        return
    me = ctx.me
    invited, referral_xp = await ctx.db.referral_stats(ctx.user_id)
    messages = int(me["messages"])
    rank = rank_for(messages)
    lines = [
        f"<b>{texts.esc(ctx.nick)}</b>",
        f"{rank.emoji} {texts.esc(rank.title)}",
        "",
        f"Очки: <b>{int(me['xp'])} ⭐</b>",
        f"Сообщений: <b>{messages}</b>",
        f"Диалогов: <b>{me['dialogs']}</b>",
        f"Оценки: 👍 {me['good_ratings']} · 👎 {me['bad_ratings']}",
    ]
    if not rank.is_max:
        progress_bar = ctx.pack.progress_bar(rank.progress)
        lines += [
            progress_bar or f"<code>{rank.bar}</code>",
            f"До «{texts.esc(rank.next_title)}»: <b>{rank.to_next}</b> сообщений",
        ]
    lines += [
        "",
        f"Возраст: <b>{me['age'] if int(me['age'] or 0) else 'не указан'}</b>",
        f"Пол: <b>{'👨 М' if me['gender'] == 'm' else '👩 Д' if me['gender'] == 'f' else 'не указан'}</b>",
        f"Берег: <b>{texts.esc(me['district']) if me['district'] else 'не указан'}</b>",
        "",
        f"Приглашено: <b>{invited}</b> · +<b>{referral_xp} ⭐</b>",
    ]
    if nicklib.is_supporter(me["support_stars"]):
        lines += ["", f"💎 Поддержал проект: {int(me['support_stars'])} ⭐"]
    subscription_claimed = await ctx.db.reward_claimed(
        ctx.user_id, "channel_subscription_v1"
    )
    await ctx.render_screen(
        "04_profile.png",
        "\n".join(lines),
        profile_keyboard(
            subscription_claimed=subscription_claimed,
            subscription_reward=ctx.cfg.subscription_reward,
        ),
    )


# --------------------------------------------------------------------- ники
async def ask_nick(ctx: Ctx) -> None:
    await ctx.reply(
        texts.NICK_PROMPT.format(
            nick=texts.esc(ctx.nick), min=nicklib.NICK_MIN, max=nicklib.NICK_MAX
        ),
        markup=menu_keyboard(ctx.mm.status(ctx.user_id)),
    )


async def set_nick(ctx: Ctx, raw: str) -> tuple[bool, str]:
    """Валидация + уникальность. Возвращает (успех, текст ответа)."""
    value = (raw or "").strip()
    if value in {"-", "—", "--", "auto"}:
        await ctx.db.set_profile(ctx.user_id, nickname="")
        ctx.me = await ctx.db.get_user(ctx.user_id)
        return True, texts.NICK_RESET.format(nick=texts.esc(await ctx.ensure_nick()))

    candidate, error = nicklib.validate(value)
    if error:
        return False, texts.NICK_BAD.format(error=texts.esc(error))
    if not await ctx.db.set_unique_nickname(ctx.user_id, candidate):
        return False, texts.NICK_TAKEN
    ctx.me = await ctx.db.get_user(ctx.user_id)
    return True, texts.NICK_SAVED.format(nick=texts.esc(candidate))


# --------------------------------------------------------------------- пары
async def announce_pair(ctx: Ctx, user_id: int, partner_id: int) -> bool:
    """Сообщаем о найденной паре без ника, очков и других идентификаторов собеседника."""
    found_kb = chat_keyboard()
    result = await send_screen_to(
        ctx.bot, partner_id, "03_found.png", texts.MATCHED, found_kb, ctx.pack, ctx.db
    )
    if result is DeliveryResult.UNAVAILABLE:
        return False
    own_result = await send_screen_to(
        ctx.bot, user_id, "03_found.png", texts.MATCHED, found_kb, ctx.pack, ctx.db
    )
    if own_result is DeliveryResult.UNAVAILABLE:
        ctx.mm.forget(user_id)
        await send_to(ctx.bot, partner_id, texts.PARTNER_LEFT, menu_keyboard(), ctx.pack)
        return False
    return True


async def announce_pairs(
    bot: Bot,
    cfg: Config,
    mm: Matchmaker,
    pairs: list[tuple[int, int]],
    pack: EmojiPack | None = None,
    db: Database | None = None,
) -> int:
    """Разослать «собеседник найден» без раскрытия публичного ника внутри чата."""
    kb = menu_keyboard()
    made = 0
    for a, b in pairs:
        result_b = await send_screen_to(
            bot, b, "03_found.png", texts.MATCHED, chat_keyboard(), pack, db,
        )
        if result_b is DeliveryResult.UNAVAILABLE:
            mm.forget(b)
            await send_to(bot, a, texts.PARTNER_LEFT, kb, pack)
            continue
        result_a = await send_screen_to(
            bot, a, "03_found.png", texts.MATCHED, chat_keyboard(), pack, db,
        )
        if result_a is DeliveryResult.UNAVAILABLE:
            mm.forget(a)
            await send_to(bot, b, texts.PARTNER_LEFT, kb, pack)
            continue
        made += 1
    return made


async def break_pair(
    bot: Bot, cfg: Config, mm: Matchmaker, user_id: int, note: str,
    pack: EmojiPack | None = None, db: Database | None = None,
) -> None:
    kb = menu_keyboard()
    partner, _ = mm.release(user_id)
    if partner is None:
        return
    if db is not None:
        await db.close_battles_for_users(user_id, partner)
    WG.clear_pair(user_id, partner)
    relay_state.clear_pair(user_id, partner)
    await send_to(bot, partner, note, kb, pack)
    await send_to(bot, user_id, note, kb, pack)


async def _send_progress_notices(ctx: Ctx, user_id: int) -> None:
    for notice in await collect_progress_notifications(ctx.db, user_id):
        await send_to(ctx.bot, user_id, notice, pack=ctx.pack)


def _dialog_summary_text(summary: dict, user_id: int, earned_xp: int) -> str:
    started = float(summary.get("started_at", time.time()))
    seconds = max(0, int(time.time() - started))
    if seconds < 60:
        duration = "меньше минуты"
    else:
        duration = f"{max(1, seconds // 60)} мин"
    counts = summary.get("counts", {}) or {}
    sent = int(counts.get(int(user_id), 0))
    game = summary.get("game_stats", {}) or {}
    lines = [
        "💬 <b>Итог разговора</b>",
        f"Диалог длился: <b>{duration}</b>",
        f"Отправлено сообщений: <b>{sent}</b>",
        f"Получено: <b>+{max(0, int(earned_xp))} ⭐</b>",
    ]
    if int(game.get("battle_games", 0)):
        lines.append(
            f"Битва мнений: <b>{int(game.get('battle_matches', 0))}/{int(game.get('battle_questions', 0))}</b>"
        )
    if int(game.get("number_games", 0)):
        lines.append(f"Числа: <b>{int(game.get('number_exact', 0))}</b> точных совпадений")
    return "\n".join(lines)


async def _end_dialog(ctx: Ctx, ended_by: int, note: str, notify_partner: str) -> None:
    partner, summary = ctx.mm.release(ctx.user_id)
    if partner is None:
        await ctx.reply(texts.NO_DIALOG, markup=menu_keyboard())
        return
    await ctx.db.close_battles_for_users(ctx.user_id, partner)
    WG.clear_pair(ctx.user_id, partner)
    relay_state.clear_pair(ctx.user_id, partner)

    counts: dict[int, int] = summary.get("counts", {}) or {}
    bonus_xp: dict[int, int] = summary.get("bonus_xp", {}) or {}
    mine = int(counts.get(ctx.user_id, 0))
    theirs = int(counts.get(partner, 0))
    started = int(summary.get("started_at", time.time()))

    live = mine > 0 and theirs > 0 and (mine + theirs) >= 6
    match_id = await ctx.db.log_dialog(
        ctx.user_id, partner, mine, theirs, started, ended_by,
        count_dialog=live, commit=False,
    )
    cap = ctx.cfg.xp_message_cap
    earned_xp: dict[int, int] = {}
    for uid, sent in ((ctx.user_id, mine), (partner, theirs)):
        gain = (
            min(sent, cap) * ctx.cfg.xp_per_message
            + int(bonus_xp.get(uid, 0))
            + (ctx.cfg.xp_per_dialog if live else 0)
        )
        if gain:
            await ctx.db.award_xp(uid, gain, commit=False)
        if sent:
            await ctx.db.bump(uid, "messages", sent, commit=False)
        await ctx.db.activity_add(
            uid, messages=sent, dialogs=1 if live else 0, commit=False
        )
        if live:
            await ctx.db.record_dialog_engagement(uid, commit=False)
            await ctx.db.update_streak(uid, commit=False)
        earned_xp[uid] = gain
    await ctx.db.db.commit()

    ctx.mm.remember_rating([ctx.user_id, partner], match_id)
    my_summary = _dialog_summary_text(summary, ctx.user_id, earned_xp.get(ctx.user_id, 0))
    partner_summary = _dialog_summary_text(summary, partner, earned_xp.get(partner, 0))

    await send_to(
        ctx.bot, partner,
        f"{notify_partner}\n\n{partner_summary}",
        menu_keyboard(), ctx.pack,
    )
    await ctx.reply(
        f"{note}\n\n{my_summary}",
        markup=rating_keyboard(),
    )
    await send_to(ctx.bot, partner, texts.RATING_ASK, rating_keyboard(), ctx.pack)

    await _send_progress_notices(ctx, ctx.user_id)
    await _send_progress_notices(ctx, partner)


def _queued_search_text(ctx: Ctx, pos: int) -> str:
    body = texts.QUEUED.format(
        city=texts.esc(ctx.cfg.city),
        pos=max(1, int(pos)),
        size=ctx.mm.queue_size(),
    )
    # Онлайн — люди, взаимодействовавшие с ботом за последние 5 минут.
    # Предупреждение нужно только при реально небольшом количестве людей.
    if online_count(ctx.mm) < 5:
        body += f"\n\n<i>{texts.LOW_ONLINE_NOTICE}</i>"
    return body


async def act_connect(ctx: Ctx) -> None:
    await ctx.ack()
    if await ctx.restricted():
        return
    await ctx.ensure_nick()
    status = ctx.mm.status(ctx.user_id)
    if status == "paired":
        await ctx.reply(texts.ALREADY_PAIRED, markup=menu_keyboard("paired"))
        return
    if status == "queued":
        await ctx.render_screen(
            "02_search.png",
            _queued_search_text(ctx, ctx.mm.position(ctx.user_id) or 1),
            menu_keyboard("queued", ctx.mm.queue_size()),
        )
        return

    prefs = ctx.prefs
    prefs["excluded"] = await ctx.db.excluded_partners(
        ctx.user_id,
        recent_seconds=max(0, int(ctx.cfg.recent_partner_cooldown_minutes)) * 60,
    )
    for _ in range(8):
        outcome, payload = ctx.mm.connect(ctx.user_id, **prefs)
        if outcome == "paired":
            if await announce_pair(ctx, ctx.user_id, payload):
                return
            ctx.mm.forget(payload)
            continue
        if outcome == "queued":
            await ctx.render_screen(
                "02_search.png",
                _queued_search_text(ctx, payload or 1),
                menu_keyboard("queued", ctx.mm.queue_size()),
            )
            return
        await ctx.reply(
            texts.QUEUE_FULL.format(limit=ctx.cfg.queue_soft_limit),
            markup=menu_keyboard(),
        )
        return
    await ctx.reply("Не удалось подобрать собеседника. Попробуй ещё раз.",
                    markup=menu_keyboard())


async def act_next(ctx: Ctx) -> None:
    if await ctx.restricted():
        return
    if ctx.mm.status(ctx.user_id) != "paired":
        await act_connect(ctx)
        return
    await _end_dialog(ctx, ended_by=ctx.user_id, note="Пропустил.", notify_partner=texts.PARTNER_SKIPPED)
    await act_connect(ctx)


async def act_stop(ctx: Ctx) -> None:
    if ctx.mm.status(ctx.user_id) == "queued":
        ctx.mm.forget(ctx.user_id)
        await ctx.ack("Поиск остановлен")
        await show_menu(ctx)
        return
    if ctx.mm.status(ctx.user_id) != "paired":
        await show_menu(ctx)
        return
    await _end_dialog(ctx, ended_by=ctx.user_id, note=texts.DIALOG_STOPPED, notify_partner=texts.PARTNER_LEFT)


async def apply_rating(ctx: Ctx, positive: bool) -> None:
    entry = ctx.mm.pending_rating(ctx.user_id)
    if entry is None:
        await ctx.ack(texts.RATING_STALE, alert=True)
        return
    match_id, partner = entry
    rated_partner = await ctx.db.rate_dialog(match_id, ctx.user_id, 1 if positive else 0)
    if rated_partner is None:
        await ctx.ack(texts.RATING_STALE, alert=True)
        return
    await ctx.db.activity_add(ctx.user_id, ratings_given=1)
    if positive:
        await ctx.db.award_xp(partner, ctx.cfg.xp_good_rating)
        await ctx.db.activity_add(partner, good_ratings=1)
        await ctx.ack("Спасибо")
        result_text = texts.RATING_DONE_GOOD.format(xp=ctx.cfg.xp_good_rating)
    else:
        await ctx.ack("Записал")
        result_text = texts.RATING_DONE_BAD
    await ctx.reply(
        result_text, markup=menu_keyboard(ctx.mm.status(ctx.user_id))
    )
    await _send_progress_notices(ctx, ctx.user_id)
    if positive:
        await _send_progress_notices(ctx, partner)


async def forget_everything(ctx: Ctx) -> None:
    partner = ctx.mm.partner(ctx.user_id)
    await ctx.db.close_battles_for_users(ctx.user_id, partner or 0)
    if partner is not None:
        WG.clear_pair(ctx.user_id, partner)
    ctx.mm.forget(ctx.user_id)
    relay_state.clear_user(ctx.user_id)
    await ctx.db.forget_user(ctx.user_id)
    await ctx.reply(
        texts.FORGET_DONE, markup=menu_keyboard()
    )
