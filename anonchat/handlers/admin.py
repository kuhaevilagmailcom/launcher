"""Панель модератора прямо в телеге: сводка, жалобы, санкции, рассылка.

Служебные команды (/stats, /ban, …) остались и работают, если ввести их руками, но
в меню команд Telegram они не показываются — наружу торчит только <code>/admin</code>,
дальше всё кнопками (см. anonchat/commands.py).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from .. import keyboards as K
from .. import nick as nicklib
from .. import texts
from ..actions import Ctx, DeliveryResult, break_pair, send_to
from ..battle_questions import get_question
from ..commands import ensure_for_admin, remove_admin_commands
from ..config import Config
from ..db import Database
from ..levels import rank_for
from ..matching import Matchmaker
from ..permissions import ALL_ADMIN_PERMISSIONS, PERMISSION_LABELS, parse_permissions
from ..diagnostics import METRICS
from .. import relay_state
from ..monitoring import invalidate_monitor_cache, pending_count
from .reports import format_report_card

router = Router(name="admin")


def _is_admin(ctx: Ctx) -> bool:
    return ctx.is_admin


async def _deny(ctx: Ctx, permission: str | None = None) -> bool:
    if _is_admin(ctx) and (permission is None or ctx.can(permission)):
        return False
    await ctx.reply("Нет доступа к этому разделу модерации.")
    return True


def _parse_args(text: str) -> list[str]:
    return (text or "").split(maxsplit=3)[1:] if text else []


def _body(text: str, *drop: str) -> str:
    """Текст команды без первой(ых) страниц: «/ban 123 спам» без «/ban»."""
    parts = (text or "").split(maxsplit=len(drop))
    return parts[-1].strip() if len(parts) > len(drop) else ""


async def clear_kb(event: CallbackQuery) -> None:
    """Снимаем кнопки с карточки жалобы, когда она отработана."""
    message = event.message
    if message is None or not hasattr(message, "edit_reply_markup"):
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass


# ---------------------------------------------------------------------------------- тексты экранов
async def stats_text(db: Database, mm: Matchmaker, cfg: Config) -> str:
    s = await db.stats()
    return (
        f"📈 <b>Анончат {texts.esc(cfg.city_short)} · сводка</b>\n\n"
        f"👥 Пользователей: <b>{s['users']}</b>\n"
        f"🆕 Пришло сегодня: <b>{s['new_today']}</b>\n"
        f"🟢 Активны за 7 дней: <b>{s['active_week']}</b>\n"
        f"💬 Диалогов сыграно: <b>{s['dialogs']}</b>\n"
        f"✉️ Сообщений переслано: <b>{s['messages']}</b>\n"
        f"⏳ В очереди: <b>{mm.queue_size()}</b> · в парах: <b>{mm.online_pairs()}</b>\n"
        f"🚩 Открытых жалоб: <b>{s['open_reports']}</b>"
    )



async def diagnostics_text(db: Database, mm: Matchmaker) -> str:
    stats = await db.stats()
    games = await db.game_diagnostics()
    queue = mm.queue_debug_snapshot(1)
    longest = int(queue[0]["waiting_seconds"]) if queue else 0
    size = db.path.stat().st_size if db.path.exists() else 0
    uptime = METRICS.uptime_seconds()
    hours, rem = divmod(uptime, 3600)
    mins = rem // 60
    last_cleanup = (
        time.strftime("%H:%M:%S", time.localtime(METRICS.last_cleanup_at))
        if METRICS.last_cleanup_at else "ещё не было"
    )
    last_save = (
        time.strftime("%H:%M:%S", time.localtime(METRICS.last_matchmaker_save_at))
        if METRICS.last_matchmaker_save_at else "ещё не было"
    )
    return (
        "🛠 <b>Диагностика</b>\n\n"
        f"Аптайм: <b>{hours} ч {mins} мин</b>\n"
        f"Версия: <code>{texts.esc(METRICS.version)}</code>\n"
        f"SQLite: <b>{size / 1024 / 1024:.2f} МБ</b>\n\n"
        f"Пользователей: <b>{stats['users']}</b>\n"
        f"Очередь: <b>{mm.queue_size()}</b> · самый долгий: <b>{longest // 60} мин</b>\n"
        f"Активные диалоги: <b>{mm.online_pairs()}</b>\n"
        f"Активные игры: <b>{games['total']}</b> "
        f"(⚔️ {games['battle']} · 🔢 {games['numbers']})\n"
        f"Просроченных игр: <b>{games['stale']}</b>\n"
        f"Открытых жалоб: <b>{stats['open_reports']}</b>\n\n"
        f"Временные ошибки Telegram: <b>{METRICS.temp_errors}</b>\n"
        f"Недоступные пользователи: <b>{METRICS.unavailable}</b>\n"
        f"Janitor удалил игр: <b>{METRICS.janitor_removed_games}</b>\n"
        f"Последняя очистка: <b>{last_cleanup}</b>\n"
        f"Последнее сохранение очереди: <b>{last_save}</b>\n"
        f"Reply-map в памяти: <b>{relay_state.size()}</b>\n"
        f"Monitor queue: <b>{pending_count()}</b>\n"
        f"Matchmaker dirty: <b>{'да' if db._matchmaker_dirty else 'нет'}</b>"
    )


def queue_text(mm: Matchmaker) -> str:
    snap = mm.queue_debug_snapshot(15)
    lines = [f"⏳ <b>Очередь · {mm.queue_size()}</b> · в парах: {mm.online_pairs()}", ""]
    gender_label = {"m": "👨 М", "f": "👩 Д", "": "пол —"}
    looking_label = {"m": "ищет М", "f": "ищет Д", "": "ищет любого"}
    for i, item in enumerate(snap, start=1):
        wait_min = max(0, int(item["waiting_seconds"]) // 60)
        lines.append(
            f"<code>{i}</code> <code>{item['user_id']}</code> · "
            f"{texts.esc(item['district'] or 'берег не указан')} · "
            f"{gender_label.get(item['gender'], 'пол —')} · "
            f"{looking_label.get(item['looking_for'], 'ищет любого')} · {wait_min} мин."
        )
    if not snap:
        lines.append("Пусто — никто не ждёт.")
    return "\n".join(lines)


async def find_text(db: Database, query: str) -> str:
    if not query:
        return "🔎 Пришли @username, имя, ник или id."
    if query.lstrip("-").isdigit():
        rows = [r for r in [await db.get_user(int(query))] if r]
    else:
        rows = await db.find_user_ids(query, 10)
    if not rows:
        return "🔎 Никого не нашёл."
    lines = ["🔎 <b>Найдено</b>", ""]
    for r in rows:
        rank = rank_for(int(r["messages"]))
        lines.append(
            f"<code>{r['user_id']}</code> · 🙋 "
            f"<b>{texts.esc(nicklib.display(r['nickname'], int(r['user_id']), r['support_stars']))}</b>"
            f" · {texts.esc(r['first_name'])} ({texts.esc(r['username'] or '-')})\n"
            f"   {rank.name} · {rank.pretty(int(r['messages']))} сообщ. · ⭐ {rank.pretty(int(r['xp']))}"
            f" · диалогов {r['dialogs']} · жалоб {r['reports_received']}"
            + (" · ⛔ бан" if r["banned"] else "")
        )
    return "\n".join(lines)


def report_card(r) -> str:
    return format_report_card(r)


async def who_text(db: Database, uid: int) -> str | None:
    row = await db.get_user(uid)
    if row is None:
        return None
    rank = rank_for(int(row["messages"]))
    return (
        f"👤 <code>{uid}</code> · 🙋 "
        f"<b>{texts.esc(nicklib.display(row['nickname'], uid, row['support_stars']))}</b>\n"
        f"📛 {texts.esc(row['first_name'])} ({texts.esc(row['username'] or '-')})\n"
        f"{rank.name} · {rank.pretty(int(row['messages']))} сообщ. · ⭐ {rank.pretty(int(row['xp']))}\n"
        f"💬 диалогов: {row['dialogs']} · 👍 {row['good_ratings']} · 👎 {row['bad_ratings']}\n"
        f"🚩 жалоб: {row['reports_received']} · {texts.esc(row['district'] or 'район не указан')}\n"
        f"💎 Поддержка: {int(row['support_stars'])} ⭐\n"
        f"в чате с {time.strftime('%d.%m.%Y', time.localtime(row['created_at']))}"
        + ("\n⛔ в бане" if row["banned"] else "")
    )


async def users_text(db: Database, limit: int = 30, offset: int = 0) -> tuple[str, int]:
    rows = await db.list_users(limit, offset)
    lines = [f"👥 <b>Пользователи · {offset + 1}–{offset + len(rows)}</b>", ""]
    for row in rows:
        username = f"@{row['username']}" if row["username"] else "без username"
        lines.append(
            f"<code>{row['user_id']}</code> · {texts.esc(username)} · "
            f"{texts.esc(row['first_name'] or '-')} · {int(row['xp'])} ⭐"
        )
    return "\n".join(lines), len(rows)


async def restricted_text(
    db: Database, kind: str, limit: int = 10, offset: int = 0
) -> tuple[str, list[int], int]:
    rows, total = await db.list_restricted(kind, limit, offset)
    title = "⛔ <b>Бан-лист</b>" if kind == "ban" else "🔇 <b>Мут-лист</b>"
    lines = [f"{title} · всего: <b>{total}</b>", ""]
    for index, row in enumerate(rows, start=offset + 1):
        user_id = int(row["user_id"])
        nick = nicklib.display(row["nickname"], user_id, row["support_stars"])
        username = f"@{row['username']}" if row["username"] else "нет username"
        lines.append(
            f"<b>{index}. {texts.esc(nick)}</b> · {texts.esc(username)}\n"
            f"ID: <code>{user_id}</code>"
        )
        if kind == "ban":
            lines.append(f"Причина: {texts.esc(row['ban_reason'] or 'не указана')}\n")
        else:
            remaining = max(1, (int(row["mute_until"]) - int(time.time()) + 59) // 60)
            lines.append(
                f"До: <b>{time.strftime('%d.%m.%Y · %H:%M', time.localtime(row['mute_until']))}</b>"
                f" · осталось {remaining} мин.\n"
            )
    if not rows:
        lines.append("Список пуст.")
    return "\n".join(lines), [int(row["user_id"]) for row in rows], total


async def restriction_screen(ctx: Ctx, db: Database, kind: str, offset: int = 0) -> None:
    body, user_ids, total = await restricted_text(db, kind, offset=offset)
    if not user_ids and offset > 0 and total:
        offset = max(0, offset - 10)
        body, user_ids, total = await restricted_text(db, kind, offset=offset)
    markup = K.restricted_list_keyboard(kind, user_ids, offset, total)
    if not await ctx.edit(body, markup):
        await ctx.reply(body, markup)


def _game_user(row, side: str) -> str:
    user_id = int(row[f"user_{side}"])
    nick = nicklib.display(
        row[f"user_{side}_nickname"] or "", user_id,
        int(row[f"user_{side}_support_stars"] or 0),
    )
    username = f"@{row[f'user_{side}_username']}" if row[f"user_{side}_username"] else "нет username"
    return f"<b>{texts.esc(nick)}</b> · {texts.esc(username)} · <code>{user_id}</code>"


async def game_watch_text(db: Database) -> str:
    rows, total = await db.list_battles(False, 6)
    lines = [f"🎮 <b>Активные игры</b> · всего: <b>{total}</b>", ""]
    status_labels = {
        "invited": "ожидает согласия", "active": "идёт", "round_done": "ответили оба",
    }
    for row in rows:
        status = str(row["status"])
        game_type = str(row["game_type"] or "battle")
        total_questions = int(row["total_questions"])
        current = min(int(row["question_index"]) + 1, total_questions)
        title = "Числа" if game_type == "numbers" else "Битва мнений"
        unit = "Раунд" if game_type == "numbers" else "Вопрос"
        lines.extend([
            f"<b>Игра #{row['id']} · {title} · {status_labels.get(status, status)}</b>",
            f"{unit}: <b>{current}/{total_questions}</b> · совпадений: <b>{row['matches']}</b>",
            f"A: {_game_user(row, 'a')}",
            f"B: {_game_user(row, 'b')}",
        ])
        if game_type == "numbers":
            answer_a = "ждёт ответа" if row["answer_a"] is None else str(row["answer_a"])
            answer_b = "ждёт ответа" if row["answer_b"] is None else str(row["answer_b"])
            lines.extend([
                f"Диапазон: <b>1–{int(row['range_max'])}</b>",
                f"Ответ A: <b>{texts.esc(answer_a)}</b>",
                f"Ответ B: <b>{texts.esc(answer_b)}</b>",
                f"Награды: <b>A {int(row['reward_total_a'])} ⭐ · B {int(row['reward_total_b'])} ⭐</b>",
                (
                    "Награда этой паре доступна"
                    if int(row["reward_awarded"] or 0)
                    else "Повторная игра пары · без награды"
                ),
            ])
        else:
            question_ids = json.loads(str(row["question_ids"] or "[]"))
            if question_ids and int(row["question_index"]) < len(question_ids):
                question = get_question(int(question_ids[int(row["question_index"])]))
                answer_a = "ждёт ответа" if row["answer_a"] is None else question.option(int(row["answer_a"]))
                answer_b = "ждёт ответа" if row["answer_b"] is None else question.option(int(row["answer_b"]))
                lines.extend([
                    f"Тема: {texts.esc(question.text)}",
                    f"Ответ A: <b>{texts.esc(answer_a)}</b>",
                    f"Ответ B: <b>{texts.esc(answer_b)}</b>",
                ])
        lines.extend([f"Обновлено: {time.strftime('%d.%m · %H:%M:%S', time.localtime(row['updated_at']))}", ""])
    if not rows:
        lines.append("Сейчас здесь пусто.")
    lines.append("Данные читаются только при открытии или обновлении этого экрана.")
    return "\n".join(lines)


async def game_watch_screen(ctx: Ctx, db: Database) -> None:
    body = await game_watch_text(db)
    markup = K.game_watch_keyboard()
    if not await ctx.edit(body, markup):
        await ctx.reply(body, markup)


async def admins_text(db: Database, owner_ids: tuple[int, ...]) -> str:
    lines = ["👮 <b>Администраторы</b>", ""]
    for owner_id in owner_ids:
        lines.append(f"<code>{owner_id}</code> · владелец · все права")
    for row in await db.list_admins():
        permissions = ", ".join(
            PERMISSION_LABELS.get(item, item)
            for item in str(row["permissions"] or "").split(",") if item
        )
        username = f"@{row['username']}" if row["username"] else (row["first_name"] or "-")
        lines.append(f"<code>{row['user_id']}</code> · {texts.esc(username)}\n{permissions}")
    return "\n".join(lines)


async def poll_admin_text(db: Database) -> str:
    poll = await db.active_poll()
    if poll is None:
        return "📊 <b>Опрос дня</b>\n\nСейчас активного опроса нет."
    results = await db.poll_results(int(poll["id"]))
    return (
        "📊 <b>Опрос дня · активен</b>\n\n"
        f"{texts.esc(poll['question'])}\n\n"
        f"1. {texts.esc(poll['option_a'])} — <b>{results['pct_a']}%</b>\n"
        f"2. {texts.esc(poll['option_b'])} — <b>{results['pct_b']}%</b>\n\n"
        f"Всего голосов: <b>{results['total']}</b>"
    )


async def poll_voters_text(db: Database) -> str:
    poll = await db.active_poll()
    if poll is None:
        return "Активного опроса нет."
    rows = await db.poll_voters(int(poll["id"]), 50)
    lines = ["👀 <b>Кто как проголосовал</b>", ""]
    for row in rows:
        uid = int(row["user_id"])
        nick = nicklib.display(row["nickname"] or "", uid, int(row["support_stars"] or 0))
        username = f"@{row['username']}" if row["username"] else "без username"
        answer = poll["option_a"] if int(row["choice"]) == 0 else poll["option_b"]
        lines.append(
            f"<code>{uid}</code> · <b>{texts.esc(nick)}</b> · {texts.esc(username)}\n"
            f"↳ {texts.esc(answer)}"
        )
    if not rows:
        lines.append("Пока никто не проголосовал.")
    if len(rows) >= 50:
        lines += ["", "<i>Показаны последние 50 голосов.</i>"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------- санкции
async def do_ban(ctx: Ctx, db: Database, mm: Matchmaker, cfg: Config, uid: int, reason: str) -> str:
    await db.set_ban(uid, True, reason)
    await break_pair(ctx.bot, cfg, mm, uid, texts.PARTNER_LEFT, ctx.pack, db)
    await send_to(
        ctx.bot, uid, texts.BANNED.format(city=texts.esc(cfg.city), reason=texts.esc(reason)),
        None, ctx.pack,
    )
    return f"⛔ <code>{uid}</code> забанен. Причина: {texts.esc(reason)}"


async def do_unban(db: Database, uid: int) -> str:
    await db.set_ban(uid, False)
    return f"✅ <code>{uid}</code> разбанен, добро пожаловать обратно в город."


async def do_mute(
    ctx: Ctx, db: Database, mm: Matchmaker, cfg: Config, uid: int, mins: int
) -> str:
    until = await db.set_mute(uid, mins)
    await send_to(
        ctx.bot, uid, texts.MUTED.format(mins=max(1, int((until - time.time()) // 60))), None, ctx.pack
    )
    await break_pair(ctx.bot, cfg, mm, uid, texts.MOD_CLOSED_DIALOG, ctx.pack, db)
    return f"🔇 <code>{uid}</code> заглушён на {mins} мин."


async def do_broadcast(ctx: Ctx, db: Database, body: str) -> str:
    ids = await db.active_ids(days=7)
    await ctx.reply(texts.PANEL_BC_PROGRESS.format(total=len(ids)))
    sent = 0
    for uid in ids:
        if await send_to(ctx.bot, uid, body, None, ctx.pack) is DeliveryResult.DELIVERED:
            sent += 1
        await asyncio.sleep(0.05)  # бережём лимиты Telegram
    return texts.PANEL_BC_DONE.format(sent=sent, total=len(ids))


async def do_broadcast_message(ctx: Ctx, db: Database, message: Message) -> str:
    """Копирует текст/фото/видео как есть, всегда без inline-кнопок."""
    ids = await db.active_ids(days=7)
    await ctx.reply(texts.PANEL_BC_PROGRESS.format(total=len(ids)))
    sent = 0
    for uid in ids:
        try:
            await message.send_copy(chat_id=uid, reply_markup=None)
            sent += 1
        except TelegramAPIError:
            pass
        await asyncio.sleep(0.05)
    return texts.PANEL_BC_DONE.format(sent=sent, total=len(ids))


def _id_args(raw: str) -> tuple[int | None, str]:
    """«123 причина» → (123, «причина»). None — не распарсилось."""
    parts = (raw or "").strip().split(maxsplit=1)
    if not parts or not parts[0].lstrip("-").isdigit():
        return None, ""
    return int(parts[0]), (parts[1].strip() if len(parts) > 1 else "")


# ---------------------------------------------------------------------------------- панель
class AdminStates(StatesGroup):
    """Один ввод — одно состояние: ждём id/текст после нажатия кнопки панели."""

    await_input = State()


async def panel_screen(ctx: Ctx, db: Database, mm: Matchmaker, edit: bool = True) -> None:
    await db.cleanup_report_context(ctx.cfg.report_context_retention_days)
    s = await db.stats()
    multiplier = await db.xp_multiplier()
    active_poll = await db.active_poll()
    body = (
        f"{texts.PANEL_TITLE.format(city=texts.esc(ctx.cfg.city))}\n\n"
        f"👥 {s['users']} · 🆕 сегодня {s['new_today']} · 🟢 {s['active_week']}\n"
        f"⏳ {mm.queue_size()} · 💬 {mm.online_pairs()} · ⭐ x{multiplier}\n"
        f"🚩 открытых жалоб: <b>{s['open_reports']}</b>\n\n"
        f"{texts.PANEL_NOTE}"
    )
    kb = K.admin_panel_keyboard(
        int(s["open_reports"]), ctx.admin_permissions, owner=ctx.is_owner,
        monitor_enabled=await db.get_kv(f"chat_monitor:{ctx.user_id}") == "1",
        xp_multiplier=multiplier,
        poll_active=active_poll is not None,
    )
    if edit and await ctx.edit(body, kb):
        return
    await ctx.reply(body, kb)


async def _ask(ctx: Ctx, state: FSMContext, what: str, prompt: str) -> None:
    await state.set_state(AdminStates.await_input)
    await state.update_data(adm=what)
    await ctx.edit(prompt, K.panel_cancel_keyboard())


#: кнопка панели → (что ждём, подсказка); None — действие выполняется сразу
PANEL_PROMPTS = {
    K.CB_PANEL_FIND: ("find", texts.PANEL_ASK_FIND),
    K.CB_PANEL_BAN: ("ban", texts.PANEL_ASK_BAN),
    K.CB_PANEL_UNBAN: ("unban", texts.PANEL_ASK_UNBAN),
    K.CB_PANEL_MUTE: ("mute", texts.PANEL_ASK_MUTE),
    K.CB_PANEL_BC: ("bc", texts.PANEL_ASK_BC),
    K.CB_PANEL_POINTS: (
        "points",
        "⭐ Пришли <code>id +50</code> для выдачи или <code>id -50</code> для снятия очков.",
    ),
    K.CB_PANEL_ADMINS: (
        "admins",
        "👮 Пришли <code>id права</code>. Права через запятую: "
        + ", ".join(sorted(ALL_ADMIN_PERMISSIONS))
        + ". Можно указать <code>all</code> или <code>remove</code> для снятия.",
    ),
}


@router.message(Command("admin", "mod", "панель"))
async def cmd_admin_panel(message: Message, ctx: Ctx, db: Database, mm: Matchmaker) -> None:
    if await _deny(ctx):
        return
    await panel_screen(ctx, db, mm, edit=False)


@router.callback_query(F.data == K.CB_ADMIN_PANEL)
async def cb_open_panel(event: CallbackQuery, ctx: Ctx, db: Database, mm: Matchmaker, state: FSMContext) -> None:
    if not _is_admin(ctx):
        await ctx.ack("Не для тебя", alert=True)
        return
    await state.clear()
    await panel_screen(ctx, db, mm)


# --------------------------------------------------------------- команды (вне меню, но живые)
@router.message(Command("stats"))
async def cmd_stats(message: Message, ctx: Ctx, db: Database, mm: Matchmaker) -> None:
    if await _deny(ctx, "stats"):
        return
    await ctx.reply(await stats_text(db, mm, ctx.cfg))


@router.message(Command("queue"))
async def cmd_queue(message: Message, ctx: Ctx, mm: Matchmaker) -> None:
    if await _deny(ctx, "queue"):
        return
    await ctx.reply(queue_text(mm))


@router.message(Command("reports"))
async def cmd_reports(message: Message, ctx: Ctx, db: Database) -> None:
    if await _deny(ctx, "reports"):
        return
    await send_report_cards(ctx, db)


async def send_report_cards(ctx: Ctx, db: Database) -> None:
    rows = await db.list_reports("new", 10)
    if not rows:
        await ctx.reply("🚩 Открытых жалоб нет — город вежливый.")
        return
    for r in rows:
        await ctx.reply(
            report_card(r), markup=K.admin_report_keyboard(int(r["id"]), ctx.admin_permissions)
        )


@router.message(Command("resolve"))
async def cmd_resolve(message: Message, ctx: Ctx, db: Database) -> None:
    if await _deny(ctx, "reports"):
        return
    args = _parse_args(message.text or "")
    if not args or not args[0].isdigit():
        await ctx.reply("Формат: <code>/resolve 12</code>")
        return
    ok = await db.resolve_report(int(args[0]), ctx.user_id)
    await ctx.reply("🚩 Жалоба закрыта." if ok else "Не нашёл открытую жалобу с таким номером.")


@router.message(Command("ban"))
async def cmd_ban(message: Message, ctx: Ctx, db: Database, mm: Matchmaker, cfg: Config) -> None:
    if await _deny(ctx, "ban"):
        return
    uid, reason = _id_args(_body(message.text or "", "/ban"))
    if uid is None:
        await ctx.reply("Формат: <code>/ban 123456 спам и оскорбления</code>")
        return
    await ctx.reply(await do_ban(ctx, db, mm, cfg, uid, reason or "решение модератора"))


@router.message(Command("unban"))
async def cmd_unban(message: Message, ctx: Ctx, db: Database) -> None:
    if await _deny(ctx, "ban"):
        return
    uid, _ = _id_args(_body(message.text or "", "/unban"))
    if uid is None:
        await ctx.reply("Формат: <code>/unban 123456</code>")
        return
    await ctx.reply(await do_unban(db, uid))


@router.message(Command("mute"))
async def cmd_mute(message: Message, ctx: Ctx, db: Database, mm: Matchmaker, cfg: Config) -> None:
    if await _deny(ctx, "mute"):
        return
    parts = _parse_args(message.text or "")
    if len(parts) < 2 or not parts[0].lstrip("-").isdigit() or not parts[1].isdigit():
        await ctx.reply("Формат: <code>/mute 123456 60</code> (id и минуты)")
        return
    await ctx.reply(await do_mute(ctx, db, mm, cfg, int(parts[0]), int(parts[1])))


@router.message(Command("find"))
async def cmd_find(message: Message, ctx: Ctx, db: Database) -> None:
    if await _deny(ctx, "users"):
        return
    await ctx.reply(await find_text(db, _body(message.text or "", "/find")))


@router.message(Command("bc"))
async def cmd_broadcast(message: Message, ctx: Ctx, db: Database) -> None:
    if await _deny(ctx, "broadcast"):
        return
    body = _body(message.text or "", "/bc")
    if not body:
        await ctx.reply("Формат: <code>/bc текст рассылки</code>")
        return
    await ctx.reply(await do_broadcast(ctx, db, body))


@router.message(Command("users"))
async def cmd_users(message: Message, ctx: Ctx, db: Database) -> None:
    if await _deny(ctx, "users"):
        return
    parts = _parse_args(message.text or "")
    offset = int(parts[0]) if parts and parts[0].isdigit() else 0
    body, count = await users_text(db, offset=offset)
    await ctx.reply(body, K.users_page_keyboard(offset, count))


@router.message(Command("points"))
async def cmd_points(message: Message, ctx: Ctx, db: Database) -> None:
    if await _deny(ctx, "points"):
        return
    parts = _parse_args(message.text or "")
    if len(parts) < 2 or not parts[0].isdigit():
        await ctx.reply("Формат: <code>/points 123456 +50</code> или <code>/points 123456 -50</code>")
        return
    try:
        amount = int(parts[1])
    except ValueError:
        await ctx.reply("Количество очков должно быть целым числом со знаком.")
        return
    balance = await db.adjust_xp(int(parts[0]), amount)
    await ctx.reply(f"⭐ Баланс <code>{parts[0]}</code>: <b>{balance}</b> очков.")


@router.message(Command("adminadd", "adminperms"))
async def cmd_admin_add(message: Message, ctx: Ctx, db: Database) -> None:
    if not ctx.is_owner:
        await ctx.reply("Назначать администраторов может только владелец.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await ctx.reply("Формат: <code>/adminadd 123456 reports,users,mute</code>")
        return
    permissions = parse_permissions(parts[2])
    if not permissions:
        await ctx.reply("Не нашёл допустимых прав.")
        return
    uid = int(parts[1])
    if uid in ctx.cfg.admin_ids:
        await ctx.reply("Это владелец из ADMIN_IDS — его права всегда полные.")
        return
    await db.set_admin(uid, permissions, ctx.user_id)
    await ensure_for_admin(ctx.bot, ctx.cfg, uid, quiet=True, authorized=True)
    await ctx.reply(f"Администратор <code>{uid}</code> сохранён: {', '.join(sorted(permissions))}.")


@router.message(Command("admindel"))
async def cmd_admin_del(message: Message, ctx: Ctx, db: Database) -> None:
    if not ctx.is_owner:
        await ctx.reply("Снимать администраторов может только владелец.")
        return
    parts = _parse_args(message.text or "")
    if not parts or not parts[0].isdigit():
        await ctx.reply("Формат: <code>/admindel 123456</code>")
        return
    uid = int(parts[0])
    if uid in ctx.cfg.admin_ids:
        await ctx.reply("Владельца из ADMIN_IDS нужно убирать через конфигурацию сервера.")
        return
    removed = await db.remove_admin(uid)
    await remove_admin_commands(ctx.bot, uid)
    await ctx.reply("Администратор снят." if removed else "Такого назначенного администратора нет.")


@router.message(Command("adminlist"))
async def cmd_admin_list(message: Message, ctx: Ctx, db: Database) -> None:
    if not ctx.is_owner:
        await ctx.reply("Список администраторов доступен только владельцу.")
        return
    await ctx.reply(await admins_text(db, ctx.cfg.admin_ids))


@router.message(Command("purge_referrals"))
async def cmd_purge_referrals(message: Message, ctx: Ctx, db: Database) -> None:
    """Только владелец: сначала показывает точный предпросмотр, затем просит подтверждение."""
    if not ctx.is_owner:
        await ctx.reply("Очистка реферальной накрутки доступна только владельцу.")
        return
    args = _parse_args(message.text or "")
    if not args or not args[0].isdigit() or int(args[0]) <= 0:
        await ctx.reply("Формат: <code>/purge_referrals 123456789</code>")
        return
    user_id = int(args[0])
    preview = await db.referral_cleanup_preview(user_id, ctx.cfg.admin_ids)
    await ctx.reply(
        "🧹 <b>Предпросмотр очистки накрутки</b>\n\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Прямых рефералов: <b>{preview['referrals']}</b>\n"
        f"Начислено по ним: <b>{preview['referral_xp']} ⭐</b>\n"
        f"Текущий баланс станет 0: <b>{preview['current_xp']} ⭐</b>\n"
        f"Профилей будет удалено: <b>{preview['users_to_delete']}</b>\n"
        f"Защищено (платёж/админ): <b>{preview['protected_users']}</b>\n\n"
        "Платежи и оплаченные Telegram Stars не удаляются. Действие необратимо.",
        K.purge_referrals_keyboard(user_id),
    )


# --------------------------------------------------------------- кнопки панели
@router.callback_query(F.data.startswith("adm:panel:"))
async def cb_panel(event: CallbackQuery, ctx: Ctx, db: Database, mm: Matchmaker, state: FSMContext) -> None:
    if not _is_admin(ctx):
        await ctx.ack("Не для тебя", alert=True)
        return
    data = event.data or ""

    if data.startswith("adm:panel:xp:"):
        if not ctx.is_owner:
            await ctx.ack("Только для владельца", alert=True)
            return
        raw = data.rsplit(":", 1)[-1]
        if raw not in {"1", "2", "3"}:
            await ctx.ack("Кнопка устарела", alert=True)
            return
        value = await db.set_xp_multiplier(int(raw))
        await ctx.ack(f"Множитель x{value} включён")
        await ctx.edit(
            f"⭐ <b>Множитель очков за сообщения</b>\n\nСейчас: <b>x{value}</b>",
            K.xp_multiplier_keyboard(value),
        )
        return

    if data.startswith("adm:panel:poll:"):
        if not ctx.is_owner:
            await ctx.ack("Только для владельца", alert=True)
            return
        action = data.rsplit(":", 1)[-1]
        if action == "create":
            await state.set_state(AdminStates.await_input)
            await state.update_data(adm="poll_question")
            await ctx.edit(
                "📊 <b>Новый опрос</b>\n\nПришли вопрос одним сообщением.",
                K.panel_cancel_keyboard(),
            )
            await ctx.ack()
            return
        if action == "close":
            closed = await db.close_active_poll()
            await ctx.ack("Опрос закрыт" if closed else "Активного опроса нет")
            await ctx.edit(await poll_admin_text(db), K.admin_poll_keyboard(False))
            return
        if action == "voters":
            await ctx.ack()
            await ctx.edit(await poll_voters_text(db), K.admin_poll_keyboard(True))
            return

    required = {
        K.CB_PANEL_STATS: "stats",
        K.CB_PANEL_DIAGNOSTICS: "stats",
        K.CB_PANEL_REPORTS: "reports",
        K.CB_PANEL_QUEUE: "queue",
        K.CB_PANEL_FIND: "users",
        K.CB_PANEL_USERS: "users",
        K.CB_PANEL_BC: "broadcast",
        K.CB_PANEL_MUTE: "mute",
        K.CB_PANEL_MUTE_LIST: "mute",
        K.CB_PANEL_BAN: "ban",
        K.CB_PANEL_UNBAN: "ban",
        K.CB_PANEL_BAN_LIST: "ban",
        K.CB_PANEL_POINTS: "points",
        K.CB_PANEL_MONITOR: "monitor",
        K.CB_PANEL_GAMES: "monitor",
    }.get(data)
    if required and not ctx.can(required):
        await ctx.ack("У тебя нет этого права", alert=True)
        return
    if data == K.CB_PANEL_ADMINS and not ctx.is_owner:
        await ctx.ack("Только для владельца", alert=True)
        return
    if data == K.CB_PANEL_BACKUP and not ctx.is_owner:
        await ctx.ack("Только для владельца", alert=True)
        return
    if data == K.CB_PANEL_MULTIPLIER:
        if not ctx.is_owner:
            await ctx.ack("Только для владельца", alert=True)
            return
        value = await db.xp_multiplier()
        await ctx.ack()
        await ctx.edit(
            f"⭐ <b>Множитель очков за сообщения</b>\n\nСейчас: <b>x{value}</b>\n"
            "x2/x3 действует только на сообщения, отправленные пока событие включено.",
            K.xp_multiplier_keyboard(value),
        )
        return
    if data == K.CB_PANEL_POLL:
        if not ctx.is_owner:
            await ctx.ack("Только для владельца", alert=True)
            return
        poll = await db.active_poll()
        await ctx.ack()
        await ctx.edit(await poll_admin_text(db), K.admin_poll_keyboard(poll is not None))
        return
    if data == K.CB_PANEL_BACK:
        await state.clear()
        await panel_screen(ctx, db, mm)
        return
    if data == K.CB_PANEL_STATS:
        await ctx.ack()
        await ctx.edit(await stats_text(db, mm, ctx.cfg), K.panel_back_keyboard())
        return
    if data == K.CB_PANEL_DIAGNOSTICS:
        await ctx.ack("Обновлено")
        await ctx.edit(await diagnostics_text(db, mm), K.diagnostics_keyboard())
        return
    if data == K.CB_PANEL_QUEUE:
        await ctx.edit(queue_text(mm), K.panel_back_keyboard())
        return
    if data == K.CB_PANEL_USERS:
        body, count = await users_text(db)
        await ctx.reply(body, K.users_page_keyboard(0, count))
        await ctx.ack()
        return
    if data == K.CB_PANEL_BAN_LIST:
        await ctx.ack()
        await restriction_screen(ctx, db, "ban")
        return
    if data == K.CB_PANEL_MUTE_LIST:
        await ctx.ack()
        await restriction_screen(ctx, db, "mute")
        return
    if data == K.CB_PANEL_GAMES:
        await ctx.ack()
        await game_watch_screen(ctx, db)
        return
    if data == K.CB_PANEL_MONITOR:
        key = f"chat_monitor:{ctx.user_id}"
        enabled = await db.get_kv(key) != "1"
        await db.set_kv(key, "1" if enabled else "0")
        invalidate_monitor_cache()
        await ctx.ack(f"Слежение за чатами {'включено' if enabled else 'выключено'}")
        await panel_screen(ctx, db, mm)
        return
    if data == K.CB_PANEL_BACKUP:
        await ctx.ack("Готовлю базу…")
        await db.flush_matchmaker(mm)
        with tempfile.NamedTemporaryFile(prefix="anonchat_backup_", suffix=".db", delete=False) as tmp:
            backup_path = Path(tmp.name)
        try:
            await db.backup_to(backup_path)
            if event.message is not None:
                await event.message.answer_document(
                    FSInputFile(backup_path, filename=f"anonchat_{time.strftime('%Y%m%d_%H%M%S')}.db"),
                    caption="Резервная копия базы AnonchatMgn.",
                )
        finally:
            backup_path.unlink(missing_ok=True)
        return
    if data == K.CB_PANEL_ADMINS:
        await state.set_state(AdminStates.await_input)
        await state.update_data(adm="admins")
        await ctx.edit(
            (await admins_text(db, ctx.cfg.admin_ids))
            + "\n\n"
            + PANEL_PROMPTS[K.CB_PANEL_ADMINS][1],
            K.panel_cancel_keyboard(),
        )
        return
    if data == K.CB_PANEL_REPORTS:
        rows = await db.list_reports("new", 10)
        if not rows:
            await ctx.edit("🚩 Открытых жалоб нет — город вежливый.", K.panel_back_keyboard())
            return
        await ctx.edit(
            f"🚩 <b>Открытые жалобы · {len(rows)}</b>\n"
            "Ниже — карточки с кнопками; сама карточка не меняется.",
            K.panel_back_keyboard(),
        )
        for r in rows:
            await ctx.reply(
                report_card(r),
                markup=K.admin_report_keyboard(int(r["id"]), ctx.admin_permissions),
            )
        await ctx.ack(f"{len(rows)} карточек")
        return

    if data in PANEL_PROMPTS:
        what, prompt = PANEL_PROMPTS[data]
        await _ask(ctx, state, what, prompt)
        await ctx.ack()
        return

    await ctx.ack("Не понимаю кнопку")


@router.message(AdminStates.await_input)
async def panel_input(message: Message, ctx: Ctx, db: Database, mm: Matchmaker, cfg: Config,
                      state: FSMContext) -> None:
    """Ввод после кнопки панели: id, id+причина, id+минуты или текст рассылки."""
    if await _deny(ctx):
        return
    data = await state.get_data()
    what = (data or {}).get("adm", "")
    raw = (message.text or message.caption or "").strip()

    if what in {"poll_question", "poll_options"} and not ctx.is_owner:
        await state.clear()
        await ctx.reply("Создавать опрос может только владелец.")
        return

    if what == "poll_question":
        if raw in {"-", "—", "--", "/cancel", "отмена"}:
            await state.clear()
            await ctx.reply(texts.PANEL_CANCELLED)
            await panel_screen(ctx, db, mm, edit=False)
            return
        if not raw:
            await ctx.reply("Вопрос не может быть пустым.")
            return
        await state.set_state(AdminStates.await_input)
        await state.update_data(adm="poll_options", poll_question=raw[:250])
        await ctx.reply(
            "Теперь пришли <b>два варианта</b> каждый с новой строки.\n\n"
            "Например:\n<code>Ночь\nДень</code>",
            K.panel_cancel_keyboard(),
        )
        return

    if what == "poll_options":
        question = str((data or {}).get("poll_question", "")).strip()
        options = [part.strip() for part in raw.splitlines() if part.strip()]
        if len(options) != 2:
            options = [part.strip() for part in raw.split("|") if part.strip()]
        if len(options) != 2:
            await ctx.reply("Нужно ровно два варианта: две строки или через <code>|</code>.")
            return
        await state.clear()
        poll_id = await db.create_poll(question, options[0], options[1], ctx.user_id)
        await ctx.reply(
            f"✅ Опрос #{poll_id} запущен. Кнопка «Опрос» уже появилась в главном меню.",
            K.admin_poll_keyboard(True),
        )
        return

    await state.clear()

    required = {
        "find": "users", "ban": "ban", "unban": "ban", "mute": "mute",
        "bc": "broadcast", "points": "points",
    }.get(what)
    if required and not ctx.can(required):
        await ctx.reply("У тебя нет этого права.")
        return
    if what == "admins" and not ctx.is_owner:
        await ctx.reply("Назначать администраторов может только владелец.")
        return

    if raw in {"-", "—", "--", "/cancel", "отмена"}:
        await ctx.reply(texts.PANEL_CANCELLED)
        await panel_screen(ctx, db, mm, edit=False)
        return

    if what == "find":
        await ctx.reply(await find_text(db, raw))
    elif what == "ban":
        uid, reason = _id_args(raw)
        await ctx.reply(
            texts.PANEL_NO_ID if uid is None
            else await do_ban(ctx, db, mm, cfg, uid, reason or "решение модератора")
        )
    elif what == "unban":
        uid, _ = _id_args(raw)
        await ctx.reply(texts.PANEL_NO_ID if uid is None else await do_unban(db, uid))
    elif what == "mute":
        uid, tail = _id_args(raw)
        mins = int(tail.split(maxsplit=1)[0]) if tail and tail.split(maxsplit=1)[0].isdigit() else 60
        await ctx.reply(texts.PANEL_NO_ID if uid is None else await do_mute(ctx, db, mm, cfg, uid, mins))
    elif what == "bc":
        await ctx.reply(await do_broadcast_message(ctx, db, message))
    elif what == "points":
        uid, tail = _id_args(raw)
        try:
            amount = int(tail)
        except ValueError:
            amount = 0
        if uid is None or amount == 0:
            await ctx.reply("Формат: <code>123456 +50</code> или <code>123456 -50</code>.")
        else:
            balance = await db.adjust_xp(uid, amount)
            await ctx.reply(f"⭐ Баланс <code>{uid}</code>: <b>{balance}</b> очков.")
    elif what == "admins":
        uid, tail = _id_args(raw)
        permissions = parse_permissions(tail)
        remove = tail.strip().lower() in {"remove", "del", "снять", "удалить"}
        if uid is None or (not permissions and not remove):
            await ctx.reply("Формат: <code>123456 reports,users,mute</code>.")
        elif uid in cfg.admin_ids:
            await ctx.reply("Это владелец из ADMIN_IDS — его права всегда полные.")
        elif remove:
            await db.remove_admin(uid)
            await remove_admin_commands(ctx.bot, uid)
            await ctx.reply(f"Администратор <code>{uid}</code> снят.")
        else:
            await db.set_admin(uid, permissions, ctx.user_id)
            await ensure_for_admin(ctx.bot, cfg, uid, quiet=True, authorized=True)
            await ctx.reply(f"Администратор <code>{uid}</code> сохранён.")
    else:
        await ctx.reply("Кнопку панели не помню — открой /admin заново.")
        return
    await panel_screen(ctx, db, mm, edit=False)


# ---------------------------------------------------------------------------------- страницы пользователей
@router.callback_query(F.data.startswith("adm:users:"))
async def cb_users_page(event: CallbackQuery, ctx: Ctx, db: Database) -> None:
    if not ctx.can("users"):
        await ctx.ack("У тебя нет этого права", alert=True)
        return
    try:
        offset = max(0, int((event.data or "").rsplit(":", 1)[1]))
    except (ValueError, IndexError):
        offset = 0
    body, count = await users_text(db, offset=offset)
    if not await ctx.edit(body, K.users_page_keyboard(offset, count)):
        await ctx.reply(body, K.users_page_keyboard(offset, count))
    await ctx.ack()


@router.callback_query(F.data.startswith("adm:restrict:"))
async def cb_restricted_list(event: CallbackQuery, ctx: Ctx, db: Database) -> None:
    if not _is_admin(ctx):
        await ctx.ack("Не для тебя", alert=True)
        return
    parts = (event.data or "").split(":")
    if len(parts) != 5:
        await ctx.ack("Кнопка устарела", alert=True)
        return
    action, value, raw_offset = parts[2], parts[3], parts[4]
    try:
        offset = max(0, int(raw_offset))
    except ValueError:
        await ctx.ack("Кнопка устарела", alert=True)
        return
    if action == "list" and value in {"ban", "mute"}:
        if not ctx.can(value):
            await ctx.ack("У тебя нет этого права", alert=True)
            return
        await ctx.ack()
        await restriction_screen(ctx, db, value, offset)
        return
    if action not in {"unban", "unmute"} or not value.isdigit():
        await ctx.ack("Кнопка устарела", alert=True)
        return
    permission = "ban" if action == "unban" else "mute"
    if not ctx.can(permission):
        await ctx.ack("У тебя нет этого права", alert=True)
        return
    user_id = int(value)
    if action == "unban":
        await db.set_ban(user_id, False)
        await ctx.ack("Пользователь разбанен")
        await restriction_screen(ctx, db, "ban", offset)
    else:
        await db.set_mute(user_id, 0)
        await ctx.ack("Мут снят")
        await restriction_screen(ctx, db, "mute", offset)


@router.callback_query(F.data.startswith("adm:games:"))
async def cb_game_watch(event: CallbackQuery, ctx: Ctx, db: Database) -> None:
    if not _is_admin(ctx) or not ctx.can("monitor"):
        await ctx.ack("У тебя нет этого права", alert=True)
        return
    view = (event.data or "").rsplit(":", 1)[-1]
    if view != "active":
        await ctx.ack("Кнопка устарела", alert=True)
        return
    await ctx.ack("Обновлено")
    await game_watch_screen(ctx, db)


@router.callback_query(F.data.startswith("adm:purge_refs:"))
async def cb_purge_referrals(
    event: CallbackQuery, ctx: Ctx, db: Database, mm: Matchmaker
) -> None:
    if not ctx.is_owner:
        await ctx.ack("Только для владельца", alert=True)
        return
    raw_id = (event.data or "").rsplit(":", 1)[-1]
    if not raw_id.isdigit() or int(raw_id) <= 0:
        await ctx.ack("Кнопка устарела", alert=True)
        return

    user_id = int(raw_id)
    await ctx.ack("Очищаю…")
    result = await db.purge_referral_abuse(user_id, ctx.cfg.admin_ids)
    removed = set(result["deleted_user_ids"])
    partners: set[int] = set()
    for removed_id in removed:
        summary = mm.forget(removed_id)
        partner = summary.get("partner")
        if isinstance(partner, int) and partner not in removed:
            partners.add(partner)
    await db.flush_matchmaker(mm)
    for partner in partners:
        await send_to(
            ctx.bot,
            partner,
            "Собеседник больше недоступен. Можно начать новый поиск.",
            pack=ctx.pack,
        )

    await clear_kb(event)
    await ctx.reply(
        "✅ <b>Реферальная накрутка удалена</b>\n\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Удалено реферальных связей: <b>{result['referrals_removed']}</b>\n"
        f"Списано реферальных начислений: <b>{result['referral_xp_removed']} ⭐</b>\n"
        f"Баланс пользователя: <b>0 ⭐</b>\n"
        f"Удалено фейк-профилей: <b>{result['users_deleted']}</b>\n"
        f"Сохранено защищённых профилей: <b>{result['protected_users']}</b>\n\n"
        "Платежи и оплаченные Telegram Stars сохранены.",
        K.panel_back_keyboard(),
    )


# ---------------------------------------------------------------------------------- кнопки в карточке жалобы
@router.callback_query(F.data.startswith("adm:"))
async def cb_admin(event: CallbackQuery, ctx: Ctx, db: Database, mm: Matchmaker, cfg: Config) -> None:
    if not _is_admin(ctx):
        await ctx.ack("Не для тебя", alert=True)
        return
    parts = (event.data or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        return  # adm:panel:* живёт в своём хендлере выше
    action, raw_id = parts[1], parts[2]
    required = {"done": "reports", "who": "users", "mute": "mute", "ban": "ban"}.get(action)
    if required and not ctx.can(required):
        await ctx.ack("У тебя нет этого права", alert=True)
        return
    report = await db.get_report(int(raw_id))
    if report is None:
        await ctx.ack("Жалоба не найдена", alert=True)
        return
    target = int(report["target_id"])

    if action == "done":
        await db.resolve_report(int(raw_id), ctx.user_id)
        await ctx.ack("Закрыто")
        await clear_kb(event)
        return

    if action == "who":
        card = await who_text(db, target)
        if card is None:
            await ctx.ack("Нет такого профиля", alert=True)
            return
        await ctx.reply(card)
        await ctx.ack("Показал профиль")
        return

    if action == "mute":
        await do_mute(ctx, db, mm, cfg, target, 60)
        await db.resolve_report(int(raw_id), ctx.user_id)
        await ctx.ack("Мут на 60 мин")
        await clear_kb(event)
        return

    if action == "ban":
        await do_ban(ctx, db, mm, cfg, target, f"жалоба #{raw_id}: {report['reason']}")
        await db.resolve_report(int(raw_id), ctx.user_id)
        await ctx.ack("Забанен")
        await clear_kb(event)
        return

    await ctx.ack("Не понимаю кнопку")
