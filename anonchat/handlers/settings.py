"""Настройки профиля: ник, возраст, берег, фильтр и удаление данных."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards as K
from .. import nick as nicklib
from .. import texts
from ..actions import (
    Ctx, DeliveryResult, announce_pairs, forget_everything, send_to,
    set_nick, show_menu, show_profile, show_online,
)
from ..db import Database
from ..matching import Matchmaker

router = Router(name="settings")


def _nick_prompt(ctx: Ctx) -> str:
    return texts.NICK_PROMPT.format(
        nick=texts.esc(ctx.nick), min=nicklib.NICK_MIN, max=nicklib.NICK_MAX
    )

DISTRICTS = {
    "none": "",
    "right": "Правый берег",
    "left": "Левый берег",
}

class ProfileStates(StatesGroup):
    nick = State()


class FeedbackStates(StatesGroup):
    user_message = State()
    admin_reply = State()


SUBSCRIPTION_REWARD_KEY = "channel_subscription_v1"


def _subscription_chat_id(raw: str) -> int | str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.startswith("https://t.me/"):
        value = value.removeprefix("https://t.me/").split("?", 1)[0].strip("/")
        if value.startswith("+"):
            return None
        value = f"@{value.lstrip('@')}"
    if value.lstrip("-").isdigit():
        return int(value)
    if not value.startswith("@"):
        value = f"@{value}"
    return value


def _subscription_url(channel: str, explicit_url: str = "") -> str:
    if str(explicit_url or "").strip():
        return str(explicit_url).strip()
    value = str(channel or "").strip()
    if value.startswith("https://t.me/"):
        return value
    if value.startswith("@"):
        return f"https://t.me/{value[1:]}"
    if value and not value.lstrip("-").isdigit():
        return f"https://t.me/{value.lstrip('@')}"
    return ""


def _member_is_subscribed(member: object) -> bool:
    status = getattr(getattr(member, "status", ""), "value", getattr(member, "status", ""))
    status = str(status)
    if status in {"member", "administrator", "creator"}:
        return True
    return status == "restricted" and bool(getattr(member, "is_member", False))


async def _apply(ctx: Ctx, db: Database, mm: Matchmaker, **fields) -> None:
    """Сохраняем профиль и пересобираем очередь с новыми фильтрами."""
    await db.set_profile(ctx.user_id, **fields)
    me = await db.get_user(ctx.user_id)
    if me is None:
        return
    ctx.me = me
    pairs = mm.refresh(
        ctx.user_id,
        district=me["district"],
        same_district=bool(me["same_district"]),
        gender=(me["gender"] or ""),
        looking_for=(me["looking_for"] or ""),
    )
    if pairs:
        await announce_pairs(ctx.bot, ctx.cfg, mm, pairs, ctx.pack, db)


async def settings_screen(ctx: Ctx) -> None:
    if await ctx.dialog_locked():
        return
    me = ctx.me
    district = (me["district"] if me else "") or ""
    same = False
    gender = (me["gender"] if me else "") or ""
    looking_for = (me["looking_for"] if me else "") or ""
    gender_text = "👨 М" if gender == "m" else "👩 Д" if gender == "f" else "не выбран"
    looking_text = "👨 М" if looking_for == "m" else "👩 Д" if looking_for == "f" else "🤷 Без разницы"
    body = (
        f"{texts.SETTINGS_TITLE}\n\n"
        f"🙋 Ник: <b>{texts.esc(ctx.nick)}</b>\n"
        f"Пол: <b>{gender_text}</b>\n"
        f"Ищу: <b>{looking_text}</b>\n"
        f"📍 Берег: <b>{texts.esc(district or 'не выбран')}</b>\n"
        f"Возраст: <b>{int(me['age']) if me and int(me['age'] or 0) else 'не указан'}</b> "
        f"<i>(необязательно)</i>\n\n"
        f"{texts.SETTINGS_NOTE}"
    )
    kb = K.settings_keyboard(same, district, ctx.nick, gender, looking_for)
    await ctx.render_screen("05_settings.png", body, kb)


@router.callback_query(F.data == "cfg:age:ask")
async def cb_age_ask(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    if await ctx.dialog_locked():
        return
    await ctx.edit("<b>Сколько тебе лет?</b>", K.age_keyboard())


@router.callback_query(F.data == "cfg:gender:ask")
async def cb_gender_ask(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    if await ctx.dialog_locked():
        return
    await ctx.edit("<b>Твой пол</b>", K.gender_keyboard())


@router.callback_query(F.data.in_({"cfg:gender:m", "cfg:gender:f", "cfg:gender:none"}))
async def cb_gender(event: CallbackQuery, ctx: Ctx, db: Database, mm: Matchmaker) -> None:
    if await ctx.dialog_locked():
        return
    key = (event.data or "").rsplit(":", 1)[-1]
    value = key if key in {"m", "f"} else ""
    await _apply(ctx, db, mm, gender=value)
    await ctx.ack("Пол обновлён")
    await settings_screen(ctx)


@router.callback_query(F.data == "cfg:looking:ask")
async def cb_looking_ask(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    if await ctx.dialog_locked():
        return
    await ctx.edit("<b>Кого ищем?</b>", K.looking_for_keyboard())


@router.callback_query(F.data.in_({"cfg:looking:m", "cfg:looking:f", "cfg:looking:any"}))
async def cb_looking(event: CallbackQuery, ctx: Ctx, db: Database, mm: Matchmaker) -> None:
    if await ctx.dialog_locked():
        return
    key = (event.data or "").rsplit(":", 1)[-1]
    value = key if key in {"m", "f"} else ""
    await _apply(ctx, db, mm, looking_for=value)
    await ctx.ack("Поиск обновлён")
    await settings_screen(ctx)


# ---------------------------------------------------------------------------------- экран настроек
@router.message(Command("settings", "config"))
async def cmd_settings(message: Message, ctx: Ctx) -> None:
    await settings_screen(ctx)


@router.callback_query(F.data == K.CB_SETTINGS)
async def cb_settings(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await settings_screen(ctx)


@router.callback_query(F.data.in_({K.CB_ONLINE, "cfg:online:refresh"}))
async def cb_online(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack("Обновлено" if event.data == "cfg:online:refresh" else "")
    if await ctx.dialog_locked():
        return
    await show_online(ctx)


# ---------------------------------------------------------------------------------- обратная связь
@router.callback_query(F.data == K.CB_FEEDBACK)
async def cb_feedback(event: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    await ctx.ack()
    if await ctx.dialog_locked():
        return
    await state.set_state(FeedbackStates.user_message)
    await ctx.edit(
        "💬 <b>Обратная связь</b>\n\n"
        "Напиши одним сообщением, что хочешь передать команде проекта.",
        K.back_menu_keyboard(),
    )


@router.message(FeedbackStates.user_message)
async def feedback_text(
    message: Message, ctx: Ctx, state: FSMContext, db: Database
) -> None:
    body = (message.text or message.caption or "").strip()
    if len(body) > 2000:
        await ctx.reply("Слишком длинно. Максимум 2000 символов.")
        return

    has_media = bool(
        message.photo or message.video or message.animation or message.document
        or message.voice or message.audio or message.video_note or message.sticker
    )
    if not body and not has_media:
        await ctx.reply("Пришли текст, фото, видео, голосовое или файл.")
        return

    admins = await db.all_admin_ids(ctx.cfg.admin_ids)
    delivered = 0
    card = (
        "💬 <b>Обратная связь</b>\n\n"
        f"От: <b>{texts.esc(ctx.nick)}</b>\n"
        f"ID: <code>{ctx.user_id}</code>"
        + (f"\n\n{texts.esc(body)}" if body else "")
    )
    for admin_id in admins:
        result = await send_to(
            ctx.bot,
            admin_id,
            card,
            K.feedback_admin_keyboard(ctx.user_id),
            ctx.pack,
        )
        delivered += int(result is DeliveryResult.DELIVERED)
        if has_media:
            try:
                await message.send_copy(chat_id=admin_id, reply_markup=None)
            except TelegramAPIError:
                pass

    await state.clear()
    if delivered:
        await ctx.reply(
            "✅ Спасибо. Сообщение отправлено команде проекта.",
            K.menu_keyboard(ctx.mm.status(ctx.user_id)),
        )
    else:
        await ctx.reply(
            "Не получилось отправить сообщение. Попробуй позже.",
            K.menu_keyboard(ctx.mm.status(ctx.user_id)),
        )


@router.callback_query(F.data.startswith("feedback:reply:"))
async def cb_feedback_reply(
    event: CallbackQuery, ctx: Ctx, state: FSMContext
) -> None:
    if not ctx.is_admin:
        await ctx.ack("Не для тебя", alert=True)
        return
    try:
        target_id = int((event.data or "").rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await ctx.ack("Пользователь не найден", alert=True)
        return
    await state.set_state(FeedbackStates.admin_reply)
    await state.update_data(feedback_reply_to=target_id)
    await ctx.ack()
    await ctx.reply(
        f"💬 Напиши ответ пользователю <code>{target_id}</code> одним сообщением."
    )


@router.message(FeedbackStates.admin_reply, F.text, ~F.text.startswith("/"))
async def feedback_admin_reply(
    message: Message, ctx: Ctx, state: FSMContext
) -> None:
    if not ctx.is_admin:
        await state.clear()
        return
    data = await state.get_data()
    target_id = int(data.get("feedback_reply_to") or 0)
    body = (message.text or "").strip()
    if not target_id or not body:
        await state.clear()
        await ctx.reply("Не получилось отправить ответ.")
        return
    if len(body) > 2000:
        await ctx.reply("Слишком длинно. Максимум 2000 символов.")
        return

    result = await send_to(
        ctx.bot,
        target_id,
        "💬 <b>Ответ на обратную связь</b>\n\n"
        f"{texts.esc(body)}",
        K.menu_keyboard(ctx.mm.status(target_id)),
        ctx.pack,
    )
    await state.clear()
    if result is DeliveryResult.DELIVERED:
        await ctx.reply("✅ Ответ отправлен.")
    else:
        await ctx.reply("Не удалось доставить ответ пользователю.")


# ---------------------------------------------------------------------------------- ник
@router.message(Command("nick", "ник"))
async def cmd_nick(message: Message, ctx: Ctx, state: FSMContext) -> None:
    """`/nick Ким` — сразу меняем; голая `/nick` или кнопка — спрашиваем следующим сообщением."""
    if await ctx.dialog_locked():
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        ok, answer = await set_nick(ctx, parts[1])
        await state.clear()  # иначе следующее сообщение пользователя уйдёт в ник вместо чата
        await message.answer(answer)
        return
    await state.set_state(ProfileStates.nick)
    await ctx.reply(_nick_prompt(ctx))


@router.callback_query(F.data == K.CB_NICK)
async def cb_nick_ask(event: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    await ctx.ack()
    if await ctx.dialog_locked():
        return
    await state.set_state(ProfileStates.nick)
    await ctx.edit(_nick_prompt(ctx), K.back_menu_keyboard())


@router.message(ProfileStates.nick, F.text, ~F.text.startswith("/"))
async def nick_text(message: Message, ctx: Ctx, state: FSMContext) -> None:
    if ctx.mm.status(ctx.user_id) == "paired":
        await state.clear()  # иначе текст диалога съедается вводом ника
        await message.answer(texts.DIALOG_LOCKED)
        return
    ok, answer = await set_nick(ctx, message.text or "")
    await message.answer(answer)
    if not ok:
        await state.set_state(ProfileStates.nick)  # даём шанс перебрать ник
        return
    await state.clear()
    await settings_screen(ctx)


# ---------------------------------------------------------------------------------- берег
@router.callback_query(F.data == "cfg:district:ask")
async def cb_district_ask(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    if await ctx.dialog_locked():
        return
    await ctx.edit(f"На каком берегу {texts.esc(ctx.cfg.city_short)} ты находишься?", K.district_keyboard())


@router.callback_query(F.data.startswith("cfg:district:"))
async def cb_district(event: CallbackQuery, ctx: Ctx, db: Database, mm: Matchmaker) -> None:
    if await ctx.dialog_locked():
        return
    key = event.data.split(":", 2)[2]
    await _apply(ctx, db, mm, district=DISTRICTS.get(key, ""))
    await ctx.ack("Берег обновлён")
    await settings_screen(ctx)


# Старые сообщения могли содержать прежнюю кнопку. Теперь приоритет берега автоматический.
@router.callback_query(F.data == "cfg:same:toggle")
async def cb_same_toggle(event: CallbackQuery, ctx: Ctx, db: Database, mm: Matchmaker) -> None:
    if await ctx.dialog_locked():
        return
    await _apply(ctx, db, mm, same_district=0)
    await ctx.ack("Приоритет берега работает автоматически")
    await settings_screen(ctx)


# ---------------------------------------------------------------------------------- сброс / удаление
@router.callback_query(F.data == "cfg:reset")
async def cb_reset(event: CallbackQuery, ctx: Ctx, db: Database, mm: Matchmaker) -> None:
    await ctx.ack()
    if await ctx.dialog_locked():
        return
    await _apply(
        ctx, db, mm,
        district="", same_district=0, gender="", looking_for="", age=0,
    )
    await settings_screen(ctx)


@router.callback_query(F.data == "cfg:forget:ask")
async def cb_forget_ask(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    if await ctx.dialog_locked():
        return
    await ctx.edit(
        "Удалить профиль? Опыт, статистика, ник и настройки будут удалены.",
        K.confirm_forget_keyboard(),
    )


@router.callback_query(F.data == "cfg:forget:yes")
async def cb_forget_yes(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    if await ctx.dialog_locked():
        return
    await forget_everything(ctx)


@router.callback_query(F.data == "cfg:forget:no")
async def cb_forget_no(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack("Отменено")
    await settings_screen(ctx)


@router.message(Command("forget"))
async def cmd_forget(message: Message, ctx: Ctx) -> None:
    if await ctx.dialog_locked():
        return
    await ctx.reply(
        "Удалить профиль? Это действие нельзя отменить.",
        markup=K.confirm_forget_keyboard(),
    )


# ---------------------------------------------------------------------------------- награда за подписку
@router.callback_query(F.data == K.CB_SUBSCRIBE_REWARD)
async def cb_subscription_reward(event: CallbackQuery, ctx: Ctx, db: Database) -> None:
    if await ctx.dialog_locked():
        return
    if await db.reward_claimed(ctx.user_id, SUBSCRIPTION_REWARD_KEY):
        await ctx.ack("Эта награда уже получена", alert=True)
        return

    target = _subscription_chat_id(ctx.cfg.subscription_channel)
    if target is None:
        await ctx.ack("Канал пока не подключён", alert=True)
        return

    await ctx.ack()
    amount = max(1, int(ctx.cfg.subscription_reward))
    body = (
        f"⭐️ <b>{amount} ⭐️ за подписку</b>\n\n"
        "Подпишись на наш Telegram-канал, затем нажми «Проверить подписку».\n\n"
        "Награда выдаётся один раз."
    )
    await ctx.edit(
        body,
        K.subscription_reward_keyboard(
            _subscription_url(ctx.cfg.subscription_channel, ctx.cfg.subscription_channel_url)
        ),
    )


@router.callback_query(F.data == K.CB_SUBSCRIBE_CHECK)
async def cb_subscription_check(event: CallbackQuery, ctx: Ctx, db: Database) -> None:
    if await ctx.dialog_locked():
        return

    amount = max(1, int(ctx.cfg.subscription_reward))
    if await db.reward_claimed(ctx.user_id, SUBSCRIPTION_REWARD_KEY):
        await ctx.ack("Ты уже получил эту награду", alert=True)
        return

    target = _subscription_chat_id(ctx.cfg.subscription_channel)
    if target is None:
        await ctx.ack("Канал пока не подключён", alert=True)
        return

    try:
        member = await ctx.bot.get_chat_member(chat_id=target, user_id=ctx.user_id)
    except TelegramAPIError:
        await ctx.ack("Не удалось проверить подписку. Попробуй позже.", alert=True)
        return

    if not _member_is_subscribed(member):
        await ctx.ack("Сначала подпишись на канал", alert=True)
        return

    claimed = await db.claim_one_time_reward(
        ctx.user_id, SUBSCRIPTION_REWARD_KEY, amount
    )
    if not claimed:
        await ctx.ack("Ты уже получил эту награду", alert=True)
        return

    ctx.me = await db.get_user(ctx.user_id)
    await ctx.ack(f"+{amount} ⭐️")
    await show_profile(ctx)


# ---------------------------------------------------------------------------------- профиль / отмена
@router.message(Command("profile", "me"))
async def cmd_profile(message: Message, ctx: Ctx) -> None:
    await show_profile(ctx)


@router.callback_query(F.data == K.CB_PROFILE)
async def cb_profile(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await show_profile(ctx)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, ctx: Ctx, state: FSMContext) -> None:
    if await state.get_state() is None:
        await ctx.reply("Нечего отменять.")
        return
    await state.clear()
    await show_menu(ctx)
