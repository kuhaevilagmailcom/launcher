"""Добровольная поддержка проекта через Telegram Stars."""

from __future__ import annotations

import secrets

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from .. import keyboards as K
from .. import texts
from ..actions import Ctx
from ..db import Database

router = Router(name="support")

MIN_STARS = 1
MAX_STARS = 10_000


class SupportStates(StatesGroup):
    amount = State()


async def ask_amount(ctx: Ctx, state: FSMContext, *, edit: bool) -> None:
    await state.set_state(SupportStates.amount)
    body = texts.SUPPORT_PROMPT.format(min_stars=MIN_STARS, max_stars=MAX_STARS)
    if edit and await ctx.edit(body, K.back_menu_keyboard()):
        return
    await ctx.reply(body, K.back_menu_keyboard())


@router.message(Command("support", "donate"))
async def cmd_support(message: Message, ctx: Ctx, state: FSMContext) -> None:
    await ask_amount(ctx, state, edit=False)


@router.callback_query(F.data == K.CB_SUPPORT)
async def cb_support(event: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    await ask_amount(ctx, state, edit=True)
    await ctx.ack()


@router.message(SupportStates.amount, F.text, ~F.text.startswith("/"))
async def support_amount(message: Message, ctx: Ctx, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not MIN_STARS <= int(raw) <= MAX_STARS:
        await ctx.reply(
            texts.SUPPORT_BAD_AMOUNT.format(min_stars=MIN_STARS, max_stars=MAX_STARS),
            K.back_menu_keyboard(),
        )
        return

    stars = int(raw)
    payload = f"support:{ctx.user_id}:{stars}:{secrets.token_hex(8)}"
    await state.clear()
    await message.answer_invoice(
        title=texts.SUPPORT_INVOICE_TITLE,
        description=texts.SUPPORT_INVOICE_DESCRIPTION,
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label="Поддержка проекта", amount=stars)],
        provider_token="",
    )


def _valid_payload(query: PreCheckoutQuery) -> bool:
    parts = (query.invoice_payload or "").split(":")
    if len(parts) != 4 or parts[0] != "support" or not parts[3]:
        return False
    try:
        user_id, value = int(parts[1]), int(parts[2])
    except ValueError:
        return False
    return (
        user_id == query.from_user.id and query.currency == "XTR"
        and MIN_STARS <= value <= MAX_STARS and query.total_amount == value
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    if _valid_payload(query):
        await query.answer(ok=True)
        return
    await query.answer(ok=False, error_message="Счёт устарел. Создай новый в меню бота.")


@router.message(F.successful_payment)
async def successful_payment(message: Message, ctx: Ctx, db: Database) -> None:
    payment = message.successful_payment
    if payment is None or payment.currency != "XTR" or not payment.telegram_payment_charge_id:
        await ctx.reply(texts.SUPPORT_PAYMENT_ERROR)
        return
    parts = (payment.invoice_payload or "").split(":")
    if len(parts) != 4 or parts[0] != "support" or not parts[3]:
        await ctx.reply(texts.SUPPORT_PAYMENT_ERROR)
        return
    try:
        payload_user = int(parts[1])
        value = int(parts[2])
    except ValueError:
        await ctx.reply(texts.SUPPORT_PAYMENT_ERROR)
        return
    kind = parts[0]
    valid = payload_user == ctx.user_id and (
        value == payment.total_amount and MIN_STARS <= value <= MAX_STARS
    )
    if not valid:
        await ctx.reply(texts.SUPPORT_PAYMENT_ERROR)
        return
    created, support_total = await db.record_payment(
        ctx.user_id,
        kind,
        payment.total_amount,
        payment.telegram_payment_charge_id,
        payment.provider_payment_charge_id,
        payment.invoice_payload,
    )
    if not created:
        await ctx.reply("Этот платёж уже учтён.", K.menu_keyboard(ctx.mm.status(ctx.user_id)))
        return
    ctx.me = await db.get_user(ctx.user_id)
    await ctx.reply(
        texts.SUPPORT_THANKS.format(stars=payment.total_amount, total=support_total),
        K.menu_keyboard(ctx.mm.status(ctx.user_id)),
    )
