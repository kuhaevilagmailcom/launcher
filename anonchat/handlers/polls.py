"""Публичный опрос дня: открыть, проголосовать, увидеть только проценты."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from .. import keyboards as K
from .. import texts
from ..actions import Ctx, show_menu
from ..db import Database

router = Router(name="polls")


async def _show_poll(ctx: Ctx, db: Database) -> None:
    poll = await db.active_poll()
    if poll is None:
        await ctx.ack("Опрос уже закрыт")
        await show_menu(ctx)
        return

    poll_id = int(poll["id"])
    results = await db.poll_results(poll_id)
    selected = await db.poll_vote_for(poll_id, ctx.user_id)
    body = (
        "<b>Опрос дня</b>\n\n"
        f"{texts.esc(poll['question'])}\n\n"
        "Выбери вариант. На кнопках показываются только проценты."
    )
    markup = K.poll_keyboard(
        poll_id,
        str(poll["option_a"]),
        str(poll["option_b"]),
        int(results["pct_a"]),
        int(results["pct_b"]),
        selected,
    )
    if not await ctx.edit(body, markup):
        await ctx.render_screen("01_main_menu.png", body, markup)


@router.callback_query(F.data == K.CB_POLL)
async def cb_open_poll(event: CallbackQuery, ctx: Ctx, db: Database) -> None:
    await ctx.ack()
    await _show_poll(ctx, db)


@router.callback_query(F.data.startswith("poll:vote:"))
async def cb_vote_poll(event: CallbackQuery, ctx: Ctx, db: Database) -> None:
    parts = (event.data or "").split(":")
    if len(parts) != 4 or not parts[2].isdigit() or parts[3] not in {"0", "1"}:
        await ctx.ack("Кнопка устарела", alert=True)
        return

    poll_id = int(parts[2])
    choice = int(parts[3])
    if not await db.vote_poll(poll_id, ctx.user_id, choice):
        await ctx.ack("Опрос уже закрыт", alert=True)
        await show_menu(ctx)
        return

    await ctx.ack("Голос учтён")
    await _show_poll(ctx, db)
