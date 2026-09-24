"""Меню, команды-дубликаты кнопок и оценка диалога."""

from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards as K
from .. import texts
from ..actions import (
    Ctx,
    act_connect,
    act_next,
    act_stop,
    apply_rating,
    send_to,
    show_help,
    show_menu,
    show_rules,
    show_top,
    show_welcome,
    show_referral,
    show_activity,
    show_streak,
    show_quests,
)
from ..commands import ensure_for_admin
from ..config import Config
from ..db import Database, REFERRAL_DAILY_LIMIT

router = Router(name="menu")
REFERRAL_XP = 50


# ---------------------------------------------------------------------------------- команды
@router.message(CommandStart())
async def cmd_start(
    message: Message, ctx: Ctx, cfg: Config, is_new_user: bool, state: FSMContext
) -> None:
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if is_new_user and len(parts) == 2 and parts[1].startswith("ref_"):
        raw_referrer = parts[1][4:]
        if raw_referrer.isdigit():
            referrer_id = int(raw_referrer)
            if await ctx.db.award_referral(ctx.user_id, referrer_id, REFERRAL_XP):
                await send_to(
                    ctx.bot,
                    referrer_id,
                    f"🎁 По твоей ссылке пришёл новый пользователь · +{REFERRAL_XP} ⭐",
                    pack=ctx.pack,
                )
    if ctx.is_admin:
        # при первом /start админа Telegram уже позволяет поставить его личное меню модератора
        await ensure_for_admin(ctx.bot, cfg, ctx.user_id, authorized=True)
    await show_welcome(ctx)
    if ctx.mm.status(ctx.user_id) == "queued":
        await ctx.reply("Ты всё ещё в очереди.")


@router.message(Command("ref", "invite"))
async def cmd_referral(message: Message, ctx: Ctx) -> None:
    await show_referral(ctx)


@router.message(Command("help"))
async def cmd_help(message: Message, ctx: Ctx, state: FSMContext) -> None:
    await state.clear()
    await show_help(ctx)


@router.message(Command("rules", "privacy"))
async def cmd_rules(message: Message, ctx: Ctx) -> None:
    await show_rules(ctx)


@router.message(Command("top", "leaderboard"))
async def cmd_top(message: Message, ctx: Ctx) -> None:
    await show_top(ctx)


@router.message(Command("connect", "find", "search"))
async def cmd_connect(message: Message, ctx: Ctx) -> None:
    await act_connect(ctx)


@router.message(Command("next", "skip"))
async def cmd_next(message: Message, ctx: Ctx) -> None:
    await act_next(ctx)


@router.message(Command("stop", "disconnect", "leave"))
async def cmd_stop(message: Message, ctx: Ctx) -> None:
    await act_stop(ctx)


# ---------------------------------------------------------------------------------- кнопки меню
@router.callback_query(F.data == K.CB_MENU)
async def cb_menu(event: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    await ctx.ack()
    await state.clear()
    await show_menu(ctx)


@router.callback_query(F.data == K.CB_CONTINUE)
async def cb_continue(event: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    await ctx.ack()
    await state.clear()
    await show_menu(ctx)


@router.callback_query(F.data.startswith("onboard:age:"))
async def cb_age(event: CallbackQuery, ctx: Ctx, db: Database, state: FSMContext) -> None:
    try:
        age = int((event.data or "").rsplit(":", 1)[1])
    except (ValueError, IndexError):
        age = 0
    if age != 0 and age not in range(13, 21):
        await ctx.ack("Выбери возраст от 13 до 20 или не указывай", alert=True)
        return
    await db.set_profile(ctx.user_id, age=age)
    ctx.me = await db.get_user(ctx.user_id)
    await ctx.ensure_nick()
    await state.clear()
    await ctx.ack()
    await show_menu(ctx)


@router.callback_query(F.data == K.CB_CONNECT)
async def cb_connect(event: CallbackQuery, ctx: Ctx) -> None:
    await act_connect(ctx)


@router.callback_query(F.data == K.CB_NEXT)
async def cb_next(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await act_next(ctx)


@router.callback_query(F.data == K.CB_STOP)
async def cb_stop(event: CallbackQuery, ctx: Ctx, cfg: Config) -> None:
    status = ctx.mm.status(ctx.user_id)
    if status == "queued":
        ctx.mm.forget(ctx.user_id)
        await ctx.ack("Поиск остановлен")
        await show_menu(ctx)
        return
    if status != "paired":
        await ctx.ack()
        await ctx.reply(texts.NO_DIALOG, markup=K.menu_keyboard())
        return
    stats = ctx.mm.dialog_stats(ctx.user_id)
    started = float(stats.get("started_at", 0) or 0)
    elapsed = max(0, int(time.time() - started)) if started else 0
    if elapsed >= 5 * 60:
        await ctx.ack()
        mins = max(1, elapsed // 60)
        await ctx.edit(
            f"Диалог идёт уже <b>{mins} мин</b>. Точно остановить?",
            K.confirm_stop_keyboard(),
        )
        return
    await ctx.ack()
    await act_stop(ctx)


@router.callback_query(F.data == K.CB_STOP_YES)
async def cb_stop_yes(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await act_stop(ctx)


@router.callback_query(F.data == K.CB_STOP_NO)
async def cb_stop_no(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack("Продолжаем")
    await show_menu(ctx)


@router.callback_query(F.data == K.CB_RULES)
async def cb_rules(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await show_rules(ctx)


@router.callback_query(F.data == K.CB_HELP)
async def cb_help(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await show_help(ctx)


@router.callback_query(F.data == K.CB_TOP)
async def cb_top(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await show_top(ctx, "week")


@router.callback_query(F.data.startswith("top:"))
async def cb_top_period(event: CallbackQuery, ctx: Ctx) -> None:
    period = (event.data or "").rsplit(":", 1)[-1]
    if period not in {"week", "month", "all"}:
        await ctx.ack("Кнопка устарела", alert=True)
        return
    await ctx.ack()
    await show_top(ctx, period)


@router.callback_query(F.data == K.CB_REFERRAL)
async def cb_referral(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await show_referral(ctx)


@router.callback_query(F.data == K.CB_ACTIVITY)
async def cb_activity(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await show_activity(ctx)


@router.callback_query(F.data == K.CB_STREAK)
async def cb_streak(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await show_streak(ctx)


@router.callback_query(F.data == K.CB_QUESTS)
async def cb_quests(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await show_quests(ctx)


@router.callback_query(F.data.startswith("act:"))
async def cb_unknown(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await show_menu(ctx)


# ---------------------------------------------------------------------------------- оценки
@router.callback_query(F.data == "rate:1")
async def cb_rate_good(event: CallbackQuery, ctx: Ctx) -> None:
    await apply_rating(ctx, positive=True)


@router.callback_query(F.data == "rate:0")
async def cb_rate_bad(event: CallbackQuery, ctx: Ctx) -> None:
    await apply_rating(ctx, positive=False)


@router.callback_query(F.data.startswith("rate:"))
async def cb_rate_unknown(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack("Эта оценка уже учтена")
