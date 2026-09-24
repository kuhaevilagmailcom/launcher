"""Пересылка сообщений между собеседниками — то, ради чего всё затевалось."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from .. import keyboards as K
from .. import texts
from ..actions import (
    Ctx, DeliveryResult, edit_copied_message, send_copy_to,
    send_copy_to_message, send_to, show_menu,
)
from ..config import Config
from ..matching import Matchmaker
from ..monitoring import enqueue_chat_monitor
from ..diagnostics import METRICS
from ..engagement import collect_progress_notifications
from .. import relay_state
from .. import word_game as WG

router = Router(name="chat")


@router.message(Command("helpcmd", "menu"))
async def cmd_menu(message: Message, ctx: Ctx, state: FSMContext) -> None:
    await state.clear()
    await show_menu(ctx)


@router.message(F.chat.type == "private")
async def relay_to_partner(
    message: Message, ctx: Ctx, cfg: Config, mm: Matchmaker
) -> None:
    """Ловим ВСЁ остальное в личке: если есть пара — отправляем копию собеседнику."""
    if ctx.user_id == 0 or message.from_user is None:
        return

    if message.text and message.text.startswith("/"):
        await ctx.reply(
            texts.UNKNOWN_COMMAND,
            markup=K.menu_keyboard(mm.status(ctx.user_id)),
        )
        return

    if await ctx.restricted():
        return

    current_partner = mm.partner(ctx.user_id)
    if (
        message.text
        and current_partner is not None
        and WG.explainer_used_secret(ctx.user_id, current_partner, message.text)
    ):
        await ctx.reply(
            "🗣 Не пиши само слово. Объясни его другими словами.",
            K.chat_keyboard(),
        )
        return

    result = mm.count_message(ctx.user_id)
    if result is None:
        if mm.status(ctx.user_id) == "queued":
            await ctx.reply(texts.QUEUED_MESSAGE, markup=K.menu_keyboard("queued"))
            return
        await ctx.reply(
            texts.NO_DIALOG,
            markup=K.menu_keyboard(mm.status(ctx.user_id)),
        )
        return

    partner, _sent = result

    body = message.text if message.text is not None else (message.caption or "")
    if len(body) > cfg.max_message_len:
        await ctx.reply(texts.TOO_LONG.format(limit=cfg.max_message_len))
        mm.uncount_message(ctx.user_id)
        return

    reply_target = None
    if message.reply_to_message is not None:
        reply_target = relay_state.resolve_reply(
            ctx.user_id, partner, message.reply_to_message.message_id
        )

    delivery, copied = await send_copy_to_message(
        ctx.bot, message, partner, reply_to_message_id=reply_target
    )
    if delivery is DeliveryResult.TEMP_ERROR:
        METRICS.temp_errors += 1
        mm.uncount_message(ctx.user_id)
        await ctx.reply(texts.DELIVERY_TEMP_ERROR)
        return
    if delivery is DeliveryResult.UNAVAILABLE:
        METRICS.unavailable += 1
        mm.uncount_message(ctx.user_id)
        await ctx.db.close_battles_for_users(ctx.user_id, partner)
        mm.forget(ctx.user_id)
        relay_state.clear_pair(ctx.user_id, partner)
        await ctx.reply(
            texts.PARTNER_UNREACHABLE,
            markup=K.menu_keyboard(),
        )
        return
    if copied is not None:
        relay_state.remember(ctx.user_id, message.message_id, partner, copied.message_id)

    if message.text:
        guessed = WG.resolve_guess(ctx.user_id, partner, message.text)
        if guessed is not None:
            awarded = await ctx.db.award_word_guess(
                guessed.guesser_id, guessed.explainer_id
            )
            game = WG.get_by_id(guessed.game_id)
            reward_line = (
                f"+<b>{awarded} ⭐</b>."
                if awarded > 0
                else "Сегодня награда за эту игру уже исчерпана."
            )
            end_line = (
                f"\n\n🏁 <b>Игра окончена</b> · {guessed.total_rounds} слов."
                if guessed.finished
                else ""
            )
            markup = (
                K.word_end_keyboard()
                if guessed.finished
                else K.word_next_keyboard(guessed.game_id, guessed.round_index)
            )
            await send_to(
                ctx.bot,
                guessed.guesser_id,
                f"🎯 <b>Угадал!</b>\nСлово: <b>{texts.esc(guessed.word)}</b>\n"
                f"{reward_line}{end_line}",
                markup,
                ctx.pack,
            )
            await send_to(
                ctx.bot,
                guessed.explainer_id,
                f"🎯 <b>Слово угадано!</b>\n"
                f"Слово: <b>{texts.esc(guessed.word)}</b>{end_line}",
                markup,
                ctx.pack,
            )
            if guessed.finished and game is not None:
                mm.record_game(game.user_a, "words", guessed.correct_total, guessed.total_rounds)
                for uid in (game.user_a, game.user_b):
                    await ctx.db.record_game_engagement(
                        uid, "words",
                        matches=guessed.correct_total,
                        total=guessed.total_rounds,
                    )
                    for notice in await collect_progress_notifications(ctx.db, uid):
                        await send_to(ctx.bot, uid, notice, pack=ctx.pack)
                WG.remove(guessed.game_id)

    # x2/x3 не пишет SQLite на каждое сообщение: бонус копится в RAM диалога
    # и начисляется одним запросом при завершении.
    multiplier = await ctx.db.xp_multiplier()
    if multiplier > 1 and _sent <= max(0, int(cfg.xp_message_cap)):
        mm.add_bonus_xp(
            ctx.user_id,
            (multiplier - 1) * max(0, int(cfg.xp_per_message)),
        )

    if message.text:
        mm.record_text(ctx.user_id, message.text)
    await enqueue_chat_monitor(message, ctx, partner)
    # молча: человек знает, что написал в анонимный чат, подтверждений не просил


@router.edited_message(F.chat.type == "private")
async def relay_edited_message(
    message: Message, ctx: Ctx, cfg: Config, mm: Matchmaker
) -> None:
    """Обновляет уже отправленную анонимную копию после редактирования исходника."""
    if ctx.user_id == 0 or message.from_user is None:
        return

    target = relay_state.forwarded_target(ctx.user_id, message.message_id)
    if target is None:
        return

    partner_id, copied_message_id = target
    if mm.partner(ctx.user_id) != partner_id:
        # После смены собеседника старые сообщения не трогаем.
        relay_state.forget_source(ctx.user_id, message.message_id)
        return

    body = message.text if message.text is not None else message.caption
    if (
        message.text
        and WG.explainer_used_secret(ctx.user_id, partner_id, message.text)
    ):
        await ctx.reply(
            "🗣 Не пиши само слово. Объясни его другими словами.",
            K.chat_keyboard(),
        )
        return
    if body is not None and len(body) > cfg.max_message_len:
        await ctx.reply(texts.TOO_LONG.format(limit=cfg.max_message_len))
        return

    delivery = await edit_copied_message(
        ctx.bot, message, partner_id, copied_message_id
    )
    if delivery is DeliveryResult.DELIVERED:
        if message.text:
            mm.record_text(ctx.user_id, message.text)
        return

    if delivery is DeliveryResult.UNAVAILABLE:
        relay_state.forget_source(ctx.user_id, message.message_id)
        return

    # Если Telegram не дал отредактировать конкретный тип, отправляем актуальную
    # версию новым анонимным сообщением и дальше синхронизируем уже её.
    retry, copied = await send_copy_to_message(ctx.bot, message, partner_id)
    if retry is DeliveryResult.DELIVERED and copied is not None:
        relay_state.remember(ctx.user_id, message.message_id, partner_id, copied.message_id)
    elif retry is DeliveryResult.TEMP_ERROR:
        await ctx.reply(texts.DELIVERY_TEMP_ERROR)
