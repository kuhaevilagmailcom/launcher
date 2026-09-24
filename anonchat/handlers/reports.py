"""Жалобы: кнопка «Жалоба» → причина → комментарий → карточка админу, авто-мут за серию жалоб."""

from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards as K
from .. import nick as nicklib
from .. import texts
from ..actions import Ctx, DeliveryResult, break_pair, send_copy_to, send_to
from ..config import Config
from ..db import Database
from ..matching import Matchmaker

router = Router(name="reports")

REASON_TITLES = K.REASON_TITLES


class ReportStates(StatesGroup):
    comment = State()


class FeedbackStates(StatesGroup):
    message = State()


async def notify_admins(ctx: Ctx, body: str, markup=None, report_id: int | None = None) -> None:
    admin_ids = await ctx.db.admin_ids_with_permission("reports", ctx.cfg.admin_ids)
    for admin_id in admin_ids:
        permissions = await ctx.db.get_admin_permissions(admin_id, ctx.cfg.admin_ids)
        actual_markup = K.admin_report_keyboard(report_id, permissions) if report_id else markup
        await send_to(ctx.bot, admin_id, body, actual_markup, ctx.pack)


def format_report_card(row, day_count: int | None = None, *, is_new: bool = False) -> str:
    target_id = int(row["target_id"])
    reporter_id = int(row["reporter_id"])
    target_nick = nicklib.display(
        row["target_nickname"] or "", target_id, int(row["target_support_stars"] or 0)
    )
    reporter_nick = nicklib.display(
        row["reporter_nickname"] or "", reporter_id, int(row["reporter_support_stars"] or 0)
    )
    target_username = f"@{row['target_username']}" if row["target_username"] else "нет username"
    reporter_username = f"@{row['reporter_username']}" if row["reporter_username"] else "нет username"
    reason = REASON_TITLES.get(str(row["reason"]), str(row["reason"]))
    context = texts.esc(row["context"]) if row["context"] else "<i>Текстового контекста нет</i>"
    comment = texts.esc(row["comment"]) if row["comment"] else "<i>Без комментария</i>"
    daily = f"\n📊 Жалоб за 24 часа: <b>{day_count}</b>" if day_count is not None else ""
    return (
        f"🚨 <b>{'НОВАЯ ЖАЛОБА' if is_new else 'ЖАЛОБА'} · #{row['id']}</b>\n"
        f"🕒 {time.strftime('%d.%m.%Y · %H:%M', time.localtime(row['created_at']))}\n\n"
        f"🎯 <b>Нарушитель</b>\n"
        f"├ Ник: <b>{texts.esc(target_nick)}</b>\n"
        f"├ ID: <code>{target_id}</code>\n"
        f"├ Telegram: {texts.esc(target_username)}\n"
        f"└ Имя: {texts.esc(row['target_name'] or '-')}\n\n"
        f"📝 <b>Причина:</b> {texts.esc(reason)}\n"
        f"💬 <b>Комментарий:</b> {comment}\n\n"
        f"📚 <b>Последние сообщения</b>\n<blockquote>{context}</blockquote>\n"
        f"🙋 <b>Отправитель:</b> {texts.esc(reporter_nick)} · "
        f"<code>{reporter_id}</code> · {texts.esc(reporter_username)}"
        f"{daily}\n\nВыбери действие кнопками ниже."
    )


def _feedback_header(ctx: Ctx, message: Message) -> str:
    username = f"@{message.from_user.username}" if message.from_user and message.from_user.username else "без username"
    first_name = message.from_user.first_name if message.from_user else "-"
    return (
        "💌 <b>Обратная связь</b>\n"
        f"От: <code>{ctx.user_id}</code> · {texts.esc(username)} · {texts.esc(first_name)}"
    )


async def _deliver_feedback(ctx: Ctx, message: Message, body: str = "") -> int:
    delivered = 0
    header = _feedback_header(ctx, message)
    for admin_id in await ctx.db.all_admin_ids(ctx.cfg.admin_ids):
        if body:
            result = await send_to(
                ctx.bot, admin_id, f"{header}\n\n{texts.esc(body[:3500])}", None, ctx.pack
            )
        else:
            result = await send_to(ctx.bot, admin_id, header, None, ctx.pack)
            if result is DeliveryResult.DELIVERED:
                result = await send_copy_to(ctx.bot, message, admin_id)
        if result is DeliveryResult.DELIVERED:
            delivered += 1
    return delivered


# ---------------------------------------------------------------------------------- старт жалобы
@router.message(Command("report", "complain", "жалоба"))
async def cmd_report(message: Message, ctx: Ctx) -> None:
    await open_report(ctx)


@router.callback_query(F.data == K.CB_REPORT)
async def cb_report(event: CallbackQuery, ctx: Ctx) -> None:
    await ctx.ack()
    await open_report(ctx)


async def open_report(ctx: Ctx) -> None:
    partner = ctx.mm.partner(ctx.user_id)
    kb = K.report_keyboard()
    if partner is None:
        await ctx.reply(texts.REPORT_NO_TARGET, markup=K.menu_keyboard())
        return
    await ctx.render_screen("07_report.png", texts.REPORT_INTRO, kb)


# ---------------------------------------------------------------------------------- причина
@router.callback_query(F.data == "rep:skip")
async def cb_skip_comment(event: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    await ctx.ack()
    data = await state.get_data()
    await finish_report(ctx, state, data.get("reason", "other"), "")


@router.callback_query(F.data.startswith("rep:"))
async def cb_reason(event: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    code = event.data.split(":", 1)[1]
    if code not in REASON_TITLES:
        await ctx.ack("Не понял причину", alert=True)
        return
    partner = ctx.mm.partner(ctx.user_id)
    if partner is None:
        await state.clear()
        await ctx.reply(texts.REPORT_NO_TARGET, markup=K.menu_keyboard())
        return
    await state.set_state(ReportStates.comment)
    await state.update_data(reason=code, partner=partner)
    prompt = texts.REPORT_COMMENT_PROMPT.format(reason=texts.esc(REASON_TITLES[code]))
    if not await ctx.edit(prompt, K.skip_cancel_keyboard()):
        await ctx.reply(prompt, K.skip_cancel_keyboard())
    await ctx.ack("Принято")


# ---------------------------------------------------------------------------------- текст жалобы
@router.message(ReportStates.comment, F.text, ~F.text.startswith("/"))
async def report_comment(message: Message, ctx: Ctx, state: FSMContext) -> None:
    data = await state.get_data()
    await finish_report(ctx, state, data.get("reason", "other"), (message.text or "").strip()[:500])


async def finish_report(ctx: Ctx, state: FSMContext, reason: str, comment: str) -> None:
    await state.clear()
    mm: Matchmaker = ctx.mm
    db: Database = ctx.db
    cfg: Config = ctx.cfg

    partner = mm.partner(ctx.user_id)
    if partner is None:
        await ctx.reply(texts.REPORT_NO_TARGET, markup=K.menu_keyboard())
        return

    dialog = mm.dialog_stats(ctx.user_id)
    dialog_key = str(dialog.get("dialog_key", ""))
    history = dialog.get("history", []) or []
    context = "\n".join(
        f"— {'жалующийся' if int(uid) == ctx.user_id else 'собеседник'}: {text}"
        for uid, text in history[-6:]
    )
    report_id, day_count = await db.add_report(
        ctx.user_id, partner, reason, comment, dialog_key=dialog_key, context=context
    )
    if report_id is None:
        await ctx.render_screen(
            "03_found.png",
            texts.REPORT_DUPLICATE,
            K.menu_keyboard("paired"),
        )
        return
    stored_report = await db.get_report(report_id)
    assert stored_report is not None
    card = format_report_card(stored_report, day_count, is_new=True)
    await notify_admins(ctx, card, report_id=report_id)

    auto = ""
    if cfg.auto_mute_reports > 0 and day_count >= cfg.auto_mute_reports:
        until = await db.set_mute(partner, cfg.auto_mute_minutes)
        mins = max(1, int((until - time.time()) // 60))
        await send_to(ctx.bot, partner, texts.MUTED.format(mins=mins), None, ctx.pack)
        await break_pair(ctx.bot, cfg, mm, partner, texts.MOD_CLOSED_DIALOG, ctx.pack, db)
        auto = texts.REPORT_AUTO_MUTE.format(mins=mins)

    status = ctx.mm.status(ctx.user_id)
    image = {
        "paired": "03_found.png",
        "queued": "02_search.png",
    }.get(status, "01_main_menu.png")
    await ctx.render_screen(
        image,
        texts.REPORT_TAKEN.format(
            rid=report_id,
            reason=texts.esc(REASON_TITLES.get(reason, reason)),
        ) + auto,
        K.menu_keyboard(status),
        live_menu=(status == "free"),
    )


# --------------------------------------------------------------------------------=> /feedback
@router.message(Command("feedback"))
async def cmd_feedback(message: Message, ctx: Ctx, state: FSMContext) -> None:
    body = (message.text or "").partition(" ")[2].strip()
    if not body:
        await state.set_state(FeedbackStates.message)
        await ctx.reply(texts.FEEDBACK_PROMPT, K.back_menu_keyboard())
        return
    await _deliver_feedback(ctx, message, body)
    await ctx.reply(texts.FEEDBACK_SENT)


@router.callback_query(F.data == K.CB_FEEDBACK)
async def cb_feedback(event: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    await state.set_state(FeedbackStates.message)
    await ctx.edit(texts.FEEDBACK_PROMPT, K.back_menu_keyboard())
    await ctx.ack()


@router.message(FeedbackStates.message, ~F.text.startswith("/"))
async def feedback_message(message: Message, ctx: Ctx, state: FSMContext) -> None:
    await state.clear()
    delivered = await _deliver_feedback(ctx, message)
    status = ctx.mm.status(ctx.user_id)
    image = {
        "paired": "03_found.png",
        "queued": "02_search.png",
    }.get(status, "01_main_menu.png")
    body = texts.FEEDBACK_SENT if delivered else "Не удалось отправить сообщение. Попробуй ещё раз."
    await ctx.render_screen(
        image,
        body,
        K.menu_keyboard(status),
        live_menu=(status == "free"),
    )
