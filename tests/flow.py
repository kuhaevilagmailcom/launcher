"""Интегральный прогон без сети: апдейты идут в Dispatcher, ответы пишутся в запись.

Запуск:  python -m tests.flow      (или pytest -q tests/flow.py)
Проверяет реальный сценарий: /start → 🔎 → ⏭/⏹ → профиль → 🚩 жалоба → оценка.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, PhotoSize, Update, User

from anonchat import commands as commands_mod
from anonchat import texts
from anonchat.commands import register_common
from anonchat.config import Config
from anonchat.db import Database
from anonchat.handlers import get_routers
from anonchat.matching import Matchmaker
from anonchat.middlewares import DataContext, Throttling
from anonchat.pack import ICONS as PACK_ICONS
from anonchat.pack import EmojiPack
from anonchat import relay_state

ADMIN = 999
A, B, C, D, E = 1001, 1002, 1003, 1004, 1005


class RecordingSession(BaseSession):
    """Фейковая Bot API: всё отправленное ботом попадает в self.outbox."""

    def __init__(self) -> None:
        super().__init__()
        self.outbox: list[dict[str, Any]] = []
        #: setMyCommands переживают clear() — проверяем, что именно уехало в Telegram
        self.menus: list[dict[str, Any]] = []
        self.fail_once: dict[str, str] = {}
        self._mid = 0

    async def close(self) -> None:  # pragma: no cover
        return None

    async def stream_content(self, *a: Any, **k: Any):  # pragma: no cover
        yield b""

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        name = method.__api_method__
        failure = self.fail_once.pop(name, "")
        if failure == "temp":
            raise TelegramAPIError(method, "temporary failure")
        if failure == "forbidden":
            raise TelegramForbiddenError(method, "bot was blocked")
        data = method.model_dump(exclude_none=True, by_alias=True)
        self.outbox.append({"method": name, **data})
        if name == "setMyCommands":
            self.menus.append(data)

        if name == "getMe":
            return User(id=777, is_bot=True, first_name="Анончат", username="anonchat_mgn_bot")
        if name in {"getUpdates", "deleteWebhook", "setMyCommands", "answerCallbackQuery"}:
            return True if name == "answerCallbackQuery" else []
        self._mid += 1
        if name in {"sendPhoto", "editMessageMedia"}:
            media = data.get("media") if isinstance(data.get("media"), dict) else {}
            return Message(
                message_id=self._mid,
                date=1_700_000_000,
                chat=Chat(id=int(data.get("chat_id", 0)), type="private"),
                photo=[PhotoSize(file_id=f"PHOTO{self._mid}", file_unique_id=f"P{self._mid}", width=1, height=1)],
                caption=data.get("caption") or media.get("caption"),
            )
        return Message(
            message_id=self._mid,
            date=1_700_000_000,
            chat=Chat(id=int(data.get("chat_id", 0)), type="private"),
            text=data.get("text"),
            caption=data.get("caption"),
        )

    # ------------------------------------------------------------------ helpers
    def to(self, chat_id: int) -> list[dict[str, Any]]:
        return [m for m in self.outbox if m.get("chat_id") == chat_id]

    def texts_to(self, chat_id: int) -> list[str]:
        values = []
        for item in self.to(chat_id):
            media = item.get("media") if isinstance(item.get("media"), dict) else {}
            value = item.get("text") or item.get("caption") or media.get("caption")
            if value:
                values.append(str(value))
        return values

    def last_to(self, chat_id: int) -> str:
        texts = self.texts_to(chat_id)
        return texts[-1] if texts else ""

    def has_keyboard(self, chat_id: int) -> bool:
        return any(m.get("reply_markup") for m in self.to(chat_id))

    def clear(self) -> None:
        self.outbox.clear()


def msg_update(
    bot: Bot, uid: int, text: str, update_id: int,
    entities: list[dict] | None = None, reply_to_message_id: int | None = None,
) -> Update:
    message: dict[str, Any] = {
        "message_id": update_id,
        "date": 1_700_000_000,
        "chat": {"id": uid, "type": "private", "first_name": f"U{uid}"},
        "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}", "username": f"user{uid}"},
        "text": text,
    }
    if entities:
        message["entities"] = entities
    if reply_to_message_id is not None:
        message["reply_to_message"] = {
            "message_id": int(reply_to_message_id),
            "date": 1_700_000_000,
            "chat": {"id": uid, "type": "private", "first_name": f"U{uid}"},
            "from": {"id": 777, "is_bot": True, "first_name": "Анончат"},
            "text": "анонимная копия",
        }
    payload = {"update_id": update_id, "message": message}
    return Update.model_validate(payload, context={"bot": bot})


def cb_update(bot: Bot, uid: int, data: str, update_id: int) -> Update:
    payload = {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "chat_instance": "1",
            "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}", "username": f"user{uid}"},
            "data": data,
            "message": {
                "message_id": update_id,
                "date": 1_700_000_000,
                "chat": {"id": uid, "type": "private", "first_name": f"U{uid}"},
                "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}"},
                "caption": "меню",
                "photo": [
                    {"file_id": "CURRENT_PHOTO", "file_unique_id": "CURRENT", "width": 1, "height": 1}
                ],
            },
        },
    }
    return Update.model_validate(payload, context={"bot": bot})


async def run_flow(holder: dict[str, Any] | None = None) -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = Config(bot_token="42:TEST", admin_ids=(ADMIN,), db_path=tmp / "flow.db")
    db = await Database(cfg.db_path).start()
    if holder is not None:
        holder["db"] = db
    mm = Matchmaker()
    session = RecordingSession()
    bot = Bot(
        cfg.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher(storage=MemoryStorage())
    pack = EmojiPack(cfg.emoji_pack_url)
    for observer in (dp.message, dp.edited_message, dp.callback_query):
        observer.outer_middleware(Throttling(cfg, limit=200))
        observer.middleware(DataContext(cfg, db, mm, pack))
    for router in get_routers():
        dp.include_router(router)
    await register_common(bot, cfg)  # как это делает main.py на старте

    step = 0

    async def send(
        uid: int, text: str, entities: list[dict] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        nonlocal step
        step += 1
        current = step
        await dp.feed_update(
            bot,
            msg_update(bot, uid, text, current, entities, reply_to_message_id),
        )
        return current

    async def press(uid: int, data: str) -> None:
        nonlocal step
        step += 1
        await dp.feed_update(bot, cb_update(bot, uid, data, step))

    def check(cond: bool, label: str) -> None:
        if not cond:
            raise AssertionError(f"{label}\noutbox A/B: {session.texts_to(A)} | {session.texts_to(B)}")
        print(f"  ok  {label}")

    # 1. /start — приветствие, авто-ник и минималистичное меню
    await send(A, "/start")
    check("Анонимный чат" in session.last_to(A), "/start показывает приветствие с меню")
    check("Онлайн сейчас" in session.last_to(A), "главное меню сразу показывает онлайн")
    check("Аноним-1001" in session.last_to(A), "при первом входе выдаётся авто-ник вместо имени из Telegram")
    check((await db.get_user(A))["nickname"] == "Аноним-1001", "авто-ник сохранился в базу")
    check(session.has_keyboard(A), "в меню есть инлайн-кнопки")
    kb = session.to(A)[-1]["reply_markup"]["inline_keyboard"]
    buttons = [btn for row in kb for btn in row]
    labels = [btn["text"] for btn in buttons]
    for needle in (
        "Поиск собеседника",
        "Следующий",
        "Стоп",
        "Жалоба",
        "Профиль",
        "Настройки",
        "Топ",
        "Правила",
        "Помощь",
    ):
        check(needle in labels, f"кнопка «{needle}» на месте")
    check(len(labels) == 8, f"в главном меню 8 кнопок, не {len(labels)}")
    check(
        all("url" not in str(btn) for btn in buttons),
        "кнопки с эмодзи-паком в меню больше нет (ссылка осталась в /help)",
    )
    # эмодзи на кнопках — анимированные из пака, а не юникодные в подписи
    check(
        all(btn.get("icon_custom_emoji_id") for btn in buttons),
        f"у каждой кнопки есть icon_custom_emoji_id: {[list(b) for b in buttons[:2]]}",
    )
    check(
        all(str(btn["icon_custom_emoji_id"]).isdigit() for btn in buttons),
        "id эмодзи — числовой, как в NewsEmoji",
    )
    check(
        all(not any(ord(c) > 0x2500 for c in t) for t in labels),
        f"подписи кнопок чистые, эмодзи — иконкой: {labels}",
    )
    styles = {btn["text"]: btn.get("style") for btn in buttons}
    check(styles["Поиск собеседника"] == "success", "главное действие подсвечено")
    check(styles["Стоп"] == "danger", "стоп — красный")
    check(
        f'<tg-emoji emoji-id="{PACK_ICONS["profile"]}">🙂</tg-emoji>' in session.last_to(A),
        "приветствие сразу пишет премиум-эмодзи пака (не юникодный значок)",
    )

    # 2. A жмёт поиск — встаёт в очередь
    session.clear()
    await press(A, "act:connect")
    check("Ищу собеседника" in session.last_to(A), "кнопка поиска ставит в очередь")
    check(mm.status(A) == "queued", "матчмейкер видит A в очереди")

    # 3. B жмёт поиск — сводим обоих
    await send(B, "/start")
    session.clear()
    await press(B, "act:connect")
    check(mm.partner(A) == B and mm.partner(B) == A, "A и B стали парой")
    check("Собеседник найден" in session.last_to(A), "A получил «Собеседник найден»")
    check("Собеседник найден" in session.last_to(B), "B получил «Собеседник найден»")
    matched_a = session.last_to(A)
    matched_b = session.last_to(B)
    check("Аноним-1002" not in matched_a and "Аноним-1001" not in matched_b,
          "ники собеседников внутри диалога скрыты")
    check("⭐" not in matched_a and "U1002" not in matched_a,
          "очки, username и id собеседника не показываются")
    check("U1001" not in matched_b and "Аня" not in matched_b,
          "реальные имя/id собеседнику не показываются")

    # 4. анонимная пересылка туда-сюда
    session.clear()
    await send(A, "Привет! Ты с какой стороны Магнитки?")
    check("Привет! Ты с какой стороны Магнитки?" in session.last_to(B), "сообщение дошло B")
    check(session.to(A) == [], "бот не пишет «доставлено анонимно» — человек и так всё понял")
    await send(B, "С Правобережного 🙂")
    check("С Правобережного" in session.last_to(A), "ответ дошёл A")

    session.clear()
    source_id = await send(A, "Сообщение для reply")
    copied_message_id = session._mid
    session.clear()
    await send(B, "Ответ именно на сообщение", reply_to_message_id=copied_message_id)
    reply_calls = [
        item for item in session.to(A)
        if item.get("method") == "sendMessage" and "Ответ именно" in str(item.get("text", ""))
    ]
    check(bool(reply_calls), "reply доставлен собеседнику")
    reply_params = reply_calls[-1].get("reply_parameters", {})
    check(int(reply_params.get("message_id", 0)) == source_id,
          "reply у собеседника привязан к исходному сообщению")
    await send(A, "О, тогда нам по пути — я от Вокзала")
    await send(B, "Бывает 🙂")
    await send(A, "Как тебе наш снег?")
    await send(B, "Хуже, чем обычно")

    # 5. мусорные типы не пересылаем, команды не теряем
    session.clear()
    await send(A, "/unknowncmd")
    check("Команда не найдена" in session.last_to(A), "неизвестная команда не улетает собеседнику")

    # 5b. пока идёт диалог — свои экраны закрыты, надо /stop
    session.clear()
    await send(A, "/profile")
    check("заверши диалог" in session.last_to(A).lower() and "/stop" in session.last_to(A),
          "в диалоге профиль закрыт: просит /stop")
    check(mm.status(A) == "paired", "диалог при этом не распался")
    session.clear()
    await press(A, "act:settings")
    check("заверши диалог" in session.last_to(A).lower(), "и настройки закрыты в диалоге")
    session.clear()
    await press(A, "act:top")
    check("заверши диалог" in session.last_to(A).lower(), "топ тоже закрыт до /stop")
    session.clear()
    await press(A, "act:connect")
    check("уже в диалоге" in session.last_to(A).lower(), "повторный поиск говорит, что ты в диалоге")
    kb_paired = session.to(A)[-1]["reply_markup"]["inline_keyboard"]
    paired_labels = [btn["text"] for row in kb_paired for btn in row]
    check(
        len(paired_labels) == 4 and "Профиль" not in paired_labels and "Настройки" not in paired_labels,
        f"в меню во время диалога только действия диалога: {paired_labels}",
    )
    check([button["text"] for button in kb_paired[-1]] == ["Игры"],
          "кнопка игр находится отдельным нижним рядом")

    # 6. стоп + начисление опыта
    session.clear()
    await send(A, "/stop")
    check(mm.status(A) == "free" and mm.status(B) == "free", "после /stop оба свободны")
    check("Закрыть диалог" in " ".join(session.texts_to(A)) or "диалог закрыт" in session.last_to(A).lower(),
          "A получил подтверждение остановки")
    check("собеседник вышел" in " ".join(session.texts_to(B)).lower(), "B узнал, что собеседник вышел")
    row_a = await db.get_user(A)
    check(row_a["dialogs"] == 1, "диалог записан в статистику")

    # 7. оценка собеседника (+XP тому, кого оценили)
    session.clear()
    before_b = (await db.get_user(B))["xp"]
    await press(A, "rate:1")
    after_b = (await db.get_user(B))["xp"]
    check(after_b - before_b == cfg.xp_good_rating, "👍 добавило собеседнику xp_good_rating")
    check("собеседнику" in session.last_to(A) and session.to(B) == [],
          "результат положительной оценки получает только нажавший")
    check((await db.get_user(A))["xp"] >= 2, "за сообщения потёк опыт")
    await press(A, "rate:1")
    check((await db.get_user(B))["xp"] == after_b, "повторная та же оценка ничего не добавляет")
    session.clear()
    await press(B, "rate:0")
    check(texts.RATING_DONE_BAD in session.last_to(B) and session.to(A) == [],
          "результат отрицательной оценки не отправляется собеседнику")

    # 8. настройки и профиль
    session.clear()
    await press(A, "act:settings")
    await press(A, "cfg:district:right")
    check((await db.get_user(A))["district"] == "Правый берег", "берег сохранился")
    await send(A, "/profile")
    card = session.last_to(A)
    check("Новичок" in card and "🌱" in card, "профиль показывает текущий ранг")
    check("<code>" in card and "▱" in card, "полоса прогресса до следующего ранга на месте")
    check("⭐" in card and "Сообщений" in card and "Диалогов" in card,
          "профиль показывает основные показатели")
    check("До «Общительный»" in card, "видно прогресс до следующего ранга")

    # 8b. свой ник вместо реального имени
    session.clear()
    await send(A, "/nick Ким Вайнон")
    check((await db.get_user(A))["nickname"] == "Ким Вайнон", "/nick сохранил выбранный ник")
    check("Ким Вайнон" in " ".join(session.texts_to(A)), "бот подтвердил ник")
    await send(B, "/nick Ким Вайнон")
    check("уже занят" in " ".join(session.texts_to(B)), "дубликат ника не проходят")
    await send(B, "/nick Магнит")
    check((await db.get_user(B))["nickname"] == "Магнит", "свободный ник принимается")
    await send(A, "/nick " + "а" * 40)
    check("максимум" in " ".join(session.texts_to(A)).lower(), "слишком длинный ник отклонён")
    await send(A, "/nick <b>хакер</b>")
    check("<b>хакер" not in " ".join(session.texts_to(A)), "HTML-мусор в ник не проскакивает")
    await send(A, "/nick Лена О")
    check((await db.get_user(A))["nickname"] == "Лена О", "ник с пробелом нормален")
    await send(A, "/nick -")
    check((await db.get_user(A))["nickname"] == "Аноним-1001", "«-» возвращает авто-ник")
    await send(A, "/nick Лена О")
    check((await db.get_user(A))["nickname"] == "Лена О", "ник можно вернуть обратно")
    # после команды с аргументом (даже неудачным) обычный текст должен доходить собеседнику
    await press(A, "act:connect")
    await press(B, "act:connect")
    check(mm.partner(A) == B, "A и B снова в паре")
    session.clear()
    await send(A, "обычное сообщение в чат")
    check("обычное сообщение в чат" in session.last_to(B), "текст уходит собеседнику, а не съедается вводом ника")
    await send(A, "/stop")

    session.clear()
    await send(ADMIN, "/top")
    top_text = " ".join(session.texts_to(ADMIN))
    check("Лена О" in top_text and "Магнит" in top_text, "в топе — выбранные ники")
    check("U1001" not in top_text and "U1002" not in top_text, "в топе нет реальных имён из Telegram")

    # 8c. эмодзи из городского пака в текстах бота
    session.clear()
    await send(
        A,
        "🧲 привет",
        entities=[{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "AQADBAD123"}],
    )
    check(pack.extra() == 1 and pack.has("🧲"), "бот подсмотрел id эмодзи из пака у пользователя")
    check(json.loads(await db.get_kv("emoji_ids")) == [["🧲", "AQADBAD123"]], "id эмодзи пережил рестарт (в базе)")
    await send(A, "/start")
    check(
        '<tg-emoji emoji-id="AQADBAD123">🧲</tg-emoji>' in session.last_to(A),
        "в своих текстах бот использует эмодзи из пака",
    )
    pack.enabled = False  # Telegram может запретить — проверяем откат
    await send(A, "/start")
    check("tg-emoji" not in session.last_to(A), "если эмодзи недоступны — текст уходит обычными смайлами")
    pack.enabled = True

    # 9. жалоба от A на C
    session.clear()
    await press(A, "act:connect")
    await send(C, "/start")
    await press(C, "act:connect")
    check(mm.partner(A) == C, "A и C в паре")
    await send(A, "тебе спамить буду")
    await press(A, "act:report")
    await press(A, "rep:spam")
    await send(A, "реклама казино, бесячье")
    card = " ".join(session.texts_to(ADMIN))
    check("НОВАЯ ЖАЛОБА" in card and "Нарушитель" in card and "Последние сообщения" in card
          and "Спам" in card, "админ получил новую удобную карточку жалобы")
    check(str(C) in card, "в карточке есть id нарушителя")
    reports = await db.list_reports("new")
    check(len(reports) == 1 and reports[0]["target_id"] == C, "жалоба легла в базу")
    check(mm.partner(A) == C, "жалоба сама по себе диалог не рвёт")
    report_markup = session.to(A)[-1].get("reply_markup", {})
    report_labels = [
        button["text"]
        for row in report_markup.get("inline_keyboard", [])
        for button in row
    ]
    check("Стоп" in report_labels and "Игры" in report_labels
          and "Найти собеседника" not in report_labels,
          "после жалобы остаётся меню текущего диалога")

    # 10. админ мутит нарушителя и закрывает жалобу
    session.clear()
    await press(ADMIN, f"adm:mute:{reports[0]['id']}")
    check(await db.is_restricted(C) == "muted", "по кнопке нарушитель ушёл в мут")
    check(await db.list_reports("new") == [], "жалоба закрыта")
    check(mm.status(C) == "free", "мут расцепил пару")
    await press(A, "act:stop")

    # 11. следующий собеседник
    session.clear()
    await press(A, "act:connect")
    await press(B, "act:connect")
    check(mm.partner(A) == B, "A снова в паре с B")
    session.clear()
    await press(A, "act:next")
    check(mm.status(A) in {"queued", "paired"}, "после «Следующий» A снова в поиске")
    check("собеседник сменил чат" in " ".join(session.texts_to(B)).lower()
          or mm.status(B) in {"free", "queued", "paired"}, "B уведомлён о скипе")

    # 12. чужие команды модерации недоступны
    session.clear()
    await send(A, "/stats")
    check("Доступ только у админов" in session.last_to(A), "/stats не для обычных пользователей")
    await send(ADMIN, "/stats")
    check("сводка" in session.last_to(ADMIN).lower(), "админ видит статистику")

    # 13. право на забвение
    session.clear()
    await press(A, "act:settings")
    await press(A, "cfg:forget:ask")
    await press(A, "cfg:forget:yes")
    check(await db.get_user(A) is None, "/forget стёр профиль полностью")

    # 14. админские команды модерации
    session.clear()
    # считаем, что при старте процесса админу ещё не могли поставить меню (чат «не найден»):
    # /start обязан донастроить личное меню модератора
    commands_mod._done.clear()
    await send(ADMIN, "/start")
    check(
        any(m["method"] == "setMyCommands" for m in session.outbox),
        "/start админа донастраивает личное меню модератора",
    )
    await send(ADMIN, "/queue")
    check("Очередь" in session.last_to(ADMIN), "/queue показывает очередь")
    await send(ADMIN, f"/find {B}")
    check(str(B) in session.last_to(ADMIN), "/find нашёл пользователя по id")
    await send(ADMIN, "/resolve")
    check("Формат" in session.last_to(ADMIN), "/resolve без аргументов подсказывает формат")

    await send(ADMIN, f"/ban {B} спам и хамство")
    check(await db.is_restricted(B) == "banned", "/ban забанил пользователя")
    session.clear()
    await press(B, "act:connect")
    check("заблокирован" in session.last_to(B).lower(), "баненному отказано в поиске пары")
    check(mm.status(B) == "free", "бан выкинул из очереди/пары")

    session.clear()
    await send(ADMIN, f"/unban {B}")
    check(await db.is_restricted(B) is None, "/unban вернул в игру")
    await send(ADMIN, "/bc 🌨 Первый снег — болтайте тёпло!")
    check("Рассылаю" in " ".join(session.texts_to(ADMIN)), "/bc запущен и отчитался")
    check(any("Первый снег" in t for t in session.texts_to(B)), "рассылка дошла пользователю")

    await send(ADMIN, "/mute 4242 5")
    check("заглушён на 5 мин" in session.last_to(ADMIN), "/mute работает даже по «сырому» id")
    check(await db.is_restricted(4242) == "muted", "мут применился и создал заглушку-профиль")

    # 15. панель модератора: всё кнопками, команды наружу не торчат
    session.clear()
    await send(ADMIN, "/admin")
    check("Панель модератора" in session.last_to(ADMIN), "/admin открыл панель модератора")
    kb = session.to(ADMIN)[-1]["reply_markup"]["inline_keyboard"]
    panel_labels = [btn["text"] for row in kb for btn in row]
    check(
        all(x in panel_labels for x in ("Сводка", "Жалобы", "Очередь", "Найти профиль", "Рассылка",
                                        "Мут по id", "Мут-лист", "Бан по id", "Разбан по id",
                                        "Бан-лист", "В меню")),
        f"в панели все разделы: {panel_labels}",
    )
    check(
        all(btn.get("icon_custom_emoji_id") for row in kb for btn in row),
        "кнопки панели тоже с эмодзи из пака",
    )

    session.clear()
    await press(ADMIN, "adm:panel:stats")
    check("сводка" in session.last_to(ADMIN).lower() and "Пользователей" in session.last_to(ADMIN),
          "кнопка «Сводка» показывает статистику, не уходя из панели")
    await press(ADMIN, "adm:panel:queue")
    check("Очередь" in session.last_to(ADMIN), "кнопка «Очередь» показывает очередь")
    await press(ADMIN, "adm:panel:back")
    check("Панель модератора" in session.last_to(ADMIN), "«Назад в панель» возвращает")

    await press(ADMIN, "adm:panel:mute_list")
    check("Мут-лист" in session.last_to(ADMIN) and "4242" in session.last_to(ADMIN),
          "мут-лист показывает активные муты")
    await press(ADMIN, "adm:restrict:unmute:4242:0")
    check(await db.is_restricted(4242) is None, "мут снимается прямо из списка")
    await press(ADMIN, "adm:panel:back")

    await press(ADMIN, "adm:panel:find")
    check("Кого ищем" in session.last_to(ADMIN), "«Найти профиль» спрашивает, кого искать")
    await send(ADMIN, str(B))
    admin_tape = " ".join(session.texts_to(ADMIN))
    check(str(B) in admin_tape, "поиск по id из панели сработал")
    check("Панель модератора" in session.last_to(ADMIN), "после действия вернулись в панель")

    await press(ADMIN, "adm:panel:ban")
    check("Кому бан" in session.last_to(ADMIN), "«Бан по id» просит id и причину")
    await send(ADMIN, f"{C} спам с панели")
    check(await db.is_restricted(C) == "banned", "бан из панели применился")
    check("забанен" in " ".join(session.texts_to(ADMIN)).lower(), "панель отчиталась о бане")
    await press(ADMIN, "adm:panel:ban_list")
    check("Бан-лист" in session.last_to(ADMIN) and str(C) in session.last_to(ADMIN),
          "бан-лист показывает заблокированных и причину")
    await press(ADMIN, f"adm:restrict:unban:{C}:0")
    check(await db.is_restricted(C) is None, "бан снимается прямо из списка")
    await press(ADMIN, "adm:panel:back")
    await press(ADMIN, "adm:panel:ban")
    await send(ADMIN, f"{C} повторный тест")
    await press(ADMIN, "adm:panel:unban")
    await send(ADMIN, str(C))
    check(await db.is_restricted(C) is None, "разбан из панели вернул пользователя")

    await press(ADMIN, "adm:panel:mute")
    await send(ADMIN, "-")
    check("Отменил" in " ".join(session.texts_to(ADMIN)), "«-» отменяет ввод и возвращает в панель")

    def menu_for(scope_type: str) -> list[str]:
        entry = next(
            (m for m in reversed(session.menus)
             if isinstance(m.get("scope"), dict) and m["scope"].get("type") == scope_type),
            None,
        )
        assert entry, f"в аутбоке нет setMyCommands для scope {scope_type}"
        return [c["command"] for c in entry["commands"]]

    admin_menu = menu_for("chat")
    check("admin" in admin_menu, "админу в меню виден /admin")
    check(
        not {"stats", "ban", "unban", "mute", "bc", "reports", "queue", "find"} & set(admin_menu),
        f"служебные команды не светятся даже админу: {admin_menu}",
    )
    public = menu_for("all_private_chats")
    check("admin" not in public and "stats" not in public, "обычный пользователь не видит админского")

    await bot.session.close()
    await db.close()
    print("\nflow test passed")


async def run_flow_modern(holder: dict[str, Any] | None = None) -> None:
    """Мобильный сценарий: профиль, безопасность, жалобы, блокировки, права и FSM."""
    tmp = Path(tempfile.mkdtemp())
    cfg = Config(
        bot_token="42:TEST", admin_ids=(ADMIN,), db_path=tmp / "flow-modern.db",
        auto_mute_reports=0,
    )
    db = await Database(cfg.db_path).start()
    if holder is not None:
        holder["db"] = db
    mm = Matchmaker()
    session = RecordingSession()
    bot = Bot(cfg.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    pack = EmojiPack(cfg.emoji_pack_url)
    for observer in (dp.message, dp.edited_message, dp.callback_query):
        observer.outer_middleware(Throttling(cfg, limit=200))
        observer.middleware(DataContext(cfg, db, mm, pack))
    for router in get_routers():
        dp.include_router(router)

    step = 1000

    async def send(uid: int, text: str) -> int:
        nonlocal step
        step += 1
        message_id = step
        await dp.feed_update(bot, msg_update(bot, uid, text, message_id))
        return message_id

    async def edit(uid: int, message_id: int, text: str) -> None:
        nonlocal step
        step += 1
        payload = {
            "update_id": step,
            "edited_message": {
                "message_id": message_id,
                "date": 1_700_000_000,
                "edit_date": 1_700_000_100,
                "chat": {"id": uid, "type": "private", "first_name": f"U{uid}"},
                "from": {
                    "id": uid, "is_bot": False, "first_name": f"U{uid}",
                    "username": f"user{uid}",
                },
                "text": text,
            },
        }
        await dp.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))

    async def press(uid: int, data: str) -> None:
        nonlocal step
        step += 1
        await dp.feed_update(bot, cb_update(bot, uid, data, step))

    async def payload(uid: int, **content: Any) -> int:
        nonlocal step
        step += 1
        message_id = step
        message = {
            "message_id": message_id, "date": 1_700_000_000,
            "chat": {"id": uid, "type": "private", "first_name": f"U{uid}"},
            "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}"},
            **content,
        }
        await dp.feed_update(bot, Update.model_validate(
            {"update_id": step, "message": message}, context={"bot": bot}
        ))
        return message_id

    def check(value: bool, label: str) -> None:
        if not value:
            raise AssertionError(f"{label}\noutbox={session.outbox[-8:]}")
        print(f"  ok  {label}")

    async def onboard(uid: int, age: int) -> None:
        await send(uid, "/start")
        check("Аноним-" in session.last_to(uid), "первый запуск сразу показывает главное меню")
        await press(uid, f"onboard:age:{age}")
        row = await db.get_user(uid)
        check(bool(row and row["age"] == age and row["nickname"]), "возраст и случайный ник сохранены")

    await onboard(A, 17)
    await onboard(B, 18)
    await onboard(C, 16)

    session.clear()
    await send(ADMIN, f"/adminadd {D} reports,users")
    await send(D, "/admin")
    dynamic_panel = next(
        item.get("reply_markup", {}) for item in reversed(session.to(D)) if item.get("reply_markup")
    )
    dynamic_labels = [
        button["text"] for row in dynamic_panel.get("inline_keyboard", []) for button in row
    ]
    check("Жалобы" in dynamic_labels and "Найти профиль" in dynamic_labels,
          "владелец выдаёт администратору выбранные разделы")
    check("Бан по id" not in dynamic_labels and "Рассылка" not in dynamic_labels,
          "невыданные права скрыты из панели")
    await press(D, "adm:panel:monitor")
    check(await db.get_kv(f"chat_monitor:{D}") != "1",
          "назначенный администратор не может включить слежение за чатами")
    await press(D, "adm:panel:backup")
    check(not any(item["method"] == "sendDocument" for item in session.to(D)),
          "скачивание базы недоступно назначенному администратору")
    await send(D, f"/ban {C} тест")
    check("Нет доступа" in session.last_to(D), "сервер запрещает действие без права ban")
    await send(ADMIN, f"/points {A} +50")
    check(int((await db.get_user(A))["xp"]) == 50, "владелец выдаёт очки")
    session.clear()
    await send(ADMIN, "/bc Тест рассылки")
    broadcast = next(item for item in session.to(A) if item.get("text") == "Тест рассылки")
    check(not broadcast.get("reply_markup"), "рассылка отправляется без кнопок")
    session.clear()
    await send(ADMIN, "/admin")
    await press(ADMIN, "adm:panel:users")
    check(any("Пользователи" in text for text in session.texts_to(ADMIN)),
          "кнопка «Все пользователи» открывает список отдельным сообщением")
    await press(ADMIN, "adm:panel:monitor")
    check(await db.get_kv(f"chat_monitor:{ADMIN}") == "1",
          "владелец включает слежение за активными чатами")
    session.clear()
    await press(ADMIN, "adm:panel:backup")
    check(any(item["method"] == "sendDocument" for item in session.to(ADMIN)),
          "владелец скачивает согласованную копию базы")

    session.clear()
    await press(A, "act:settings")
    check(any(item["method"] == "editMessageMedia" for item in session.outbox),
          "главное меню в настройки меняет картинку")
    first_settings_file = await db.get_kv("menu_file_id:05_settings.png")
    check(bool(first_settings_file), "file_id экрана сохраняется в SQLite")

    session.clear()
    await press(A, "act:profile")
    check(any(item["method"] == "editMessageMedia" for item in session.outbox),
          "настройки в профиль меняет картинку")

    session.clear()
    await press(A, "act:rules")
    check(any(item["method"] == "editMessageMedia" for item in session.outbox),
          "профиль в правила меняет картинку")

    session.clear()
    await press(A, "act:settings")
    edit_media = next(
        item for item in reversed(session.outbox) if item["method"] == "editMessageMedia"
    )
    media = edit_media.get("media", {})
    check(media.get("media") == first_settings_file, "повторный экран использует cached file_id")

    session.clear()
    await press(A, "cfg:feedback")
    await payload(A, photo=[{"file_id": "feedback-photo", "file_unique_id": "feedback", "width": 1, "height": 1}])
    check(any("Обратная связь" in text for text in session.texts_to(ADMIN)),
          "обратная связь уходит владельцу")
    check(any(item["method"] == "sendPhoto" for item in session.to(D)),
          "медиаотзыв уходит всем назначенным админам")

    session.clear()
    await press(A, "act:support")
    check("количество звёзд" in session.last_to(A), "поддержка спрашивает количество звёзд")
    await send(A, "25")
    invoice = next((item for item in reversed(session.outbox) if item["method"] == "sendInvoice"), None)
    check(
        bool(invoice and invoice["currency"] == "XTR" and invoice["prices"][0]["amount"] == 25),
        "бот создаёт счёт Telegram Stars на введённую сумму",
    )

    support_payment = {
        "currency": "XTR",
        "total_amount": 25,
        "invoice_payload": f"support:{A}:25:nonce",
        "telegram_payment_charge_id": "support-charge-1",
        "provider_payment_charge_id": "",
    }
    await payload(A, successful_payment=support_payment)
    support_stars = int((await db.get_user(A))["support_stars"])
    check(support_stars == 25, "successful payment сохраняет поддержку")
    await payload(A, successful_payment=support_payment)
    check(int((await db.get_user(A))["support_stars"]) == support_stars,
          "duplicate charge ID не начисляет поддержку повторно")
    await send(A, "/profile")
    check("💎" in session.last_to(A), "поддержавший получает постоянный marker")

    session.clear()
    await press(A, "act:connect")
    check(mm.status(A) == "queued" and "Ищу собеседника" in session.last_to(A), "поиск ставит в очередь")
    check(session.outbox[0]["method"] == "answerCallbackQuery",
          "кнопка поиска отпускает интерфейс сразу")
    await send(B, "/start")
    check("Онлайн сейчас" in session.last_to(B),
          "главное меню сразу показывает общий онлайн")
    await press(B, "act:settings")
    settings_message = next(
        item for item in reversed(session.to(B)) if item.get("reply_markup")
    )
    settings_markup = settings_message.get("reply_markup", {})
    settings_labels = [
        button["text"] for row in settings_markup.get("inline_keyboard", []) for button in row
    ]
    check("Онлайн сейчас" in settings_labels, "онлайн вынесен в настройки")
    await press(B, "cfg:online")
    check("Ищут собеседника: <b>1</b>" in session.last_to(B),
          "экран онлайна показывает очередь")
    await press(B, "act:menu")
    await press(B, "act:connect")
    check(mm.partner(A) == B, "возраст не разделяет очередь")
    check("Собеседник найден" in session.last_to(A), "экран найденного собеседника отправлен")
    check("@user1002" not in session.last_to(A) and f"<code>{B}</code>" not in session.last_to(A)
          and "⭐" not in session.last_to(A),
          "в найденном диалоге не раскрываются ник, очки и id")

    session.clear()
    original_id = await send(A, "текст до редактирования")
    check(any(item["method"] == "sendMessage" for item in session.to(B)),
          "исходный текст сначала доставлен собеседнику")
    session.clear()
    await edit(A, original_id, "текст после редактирования")
    edit_call = next(
        (item for item in session.to(B) if item["method"] == "editMessageText"), None
    )
    check(bool(edit_call), "редактирование исходного сообщения меняет копию у собеседника")
    check(edit_call["text"] == "текст после редактирования"
          and int(edit_call.get("message_id", 0)) > 0,
          "у собеседника редактируется именно ранее созданная копия")

    # Если Telegram больше не даёт редактировать копию, обработчик не падает
    # и забывает старую связь message_id.
    session.clear()
    unavailable_source = await send(A, "сообщение перед недоступным edit")
    check(relay_state.forwarded_target(A, unavailable_source) is not None,
          "reply-map помнит сообщение до edit")
    session.fail_once["editMessageText"] = "forbidden"
    await edit(A, unavailable_source, "редактирование при недоступном собеседнике")
    check(relay_state.forwarded_target(A, unavailable_source) is None,
          "недоступный edit безопасно очищает reply-map")

    # Фото отправляется напрямую по file_id и переживает кратковременный сбой Telegram.
    session.clear()
    session.fail_once["sendPhoto"] = "temp"
    await payload(
        A,
        photo=[{"file_id": "chat-photo", "file_unique_id": "chat-photo-u", "width": 1280, "height": 720}],
        caption="фото <3",
    )
    check(any(item["method"] == "sendPhoto" for item in session.to(B)),
          "фото доходит после временной ошибки Telegram")
    check(mm.partner(A) == B, "временная ошибка фото не разрывает диалог")

    # Reply на медиа должен сохранить привязку к исходному сообщению.
    session.clear()
    photo_source = await payload(
        A,
        photo=[{"file_id": "reply-photo", "file_unique_id": "reply-photo-u", "width": 640, "height": 640}],
        caption="фото для reply",
    )
    forwarded = relay_state.forwarded_target(A, photo_source)
    check(forwarded is not None and forwarded[0] == B, "reply-map запомнил фото")
    copied_photo_id = int(forwarded[1])
    session.clear()
    await payload(
        B,
        photo=[{"file_id": "reply-answer", "file_unique_id": "reply-answer-u", "width": 320, "height": 320}],
        caption="ответ на фото",
        reply_to_message={
            "message_id": copied_photo_id,
            "date": 1_700_000_000,
            "chat": {"id": B, "type": "private", "first_name": f"U{B}"},
            "from": {"id": 777, "is_bot": True, "first_name": "Анончат"},
            "text": "анонимная копия",
        },
    )
    media_reply = next(
        (item for item in session.to(A)
         if item.get("method") == "sendPhoto" and item.get("caption") == "ответ на фото"),
        None,
    )
    check(bool(media_reply), "media-reply доставлен")
    check(int((media_reply.get("reply_parameters") or {}).get("message_id", 0)) == photo_source,
          "media-reply привязан к исходному фото")

    # Некоторые Telegram-клиенты присылают картинку как image/document.
    session.clear()
    await payload(
        A,
        document={
            "file_id": "image-doc", "file_unique_id": "image-doc-u",
            "file_name": "photo.jpg", "mime_type": "image/jpeg",
        },
    )
    check(any(item["method"] == "sendDocument" for item in session.to(B)),
          "картинка, отправленная как файл, тоже доходит собеседнику")

    session.clear()
    await send(A, "/game")
    check("Битва мнений" in str(session.to(A)[-1].get("reply_markup")),
          "/game открывает игры в активном чате")
    await press(A, "game:battle")
    check("5 вопросов" in str(session.to(A)[-1].get("reply_markup"))
          and "10 вопросов" in str(session.to(A)[-1].get("reply_markup")),
          "перед игрой можно выбрать 5 или 10 вопросов")
    await press(A, "game:battle:5")
    battle = await db.battle_for_pair(A, B)
    check(bool(battle and battle["status"] == "invited"), "предложение игры сохранено в SQLite")
    battle_id = int(battle["id"])
    check("предлагает сыграть" in session.last_to(B), "второй игрок получает приглашение")
    await press(B, f"game:yes:{battle_id}")
    check("⚔️ <b>1/5</b>" in session.last_to(A) and "⚔️ <b>1/5</b>" in session.last_to(B),
          "согласие обоих запускает пять вопросов")
    await send(ADMIN, "/admin")
    await press(ADMIN, "adm:panel:games")
    watch = session.last_to(ADMIN)
    check("Активные игры" in watch and f"Игра #{battle_id}" in watch
          and "Ответ A" in watch and "Ответ B" in watch,
          "админ видит игру и ответы только при открытии экрана")
    await press(ADMIN, "adm:games:active")
    check("Данные читаются только" in session.last_to(ADMIN), "экран игр обновляется вручную")
    for question_index in range(5):
        session.clear()
        await press(A, f"game:answer:{battle_id}:{question_index}:0")
        check("Ждём собеседника" in session.last_to(A), "первый ответ зафиксирован")
        await press(A, f"game:answer:{battle_id}:{question_index}:1")
        await press(B, f"game:answer:{battle_id}:{question_index}:{0 if question_index < 4 else 1}")
        if question_index < 4:
            check("Совпало" in session.last_to(A), "совпадение показано обоим")
            await press(A, f"game:next:{battle_id}:{question_index}")
            check(f"<b>{question_index + 2}/5</b>" in session.last_to(B), "следующий вопрос синхронно показан обоим")
        else:
            battle_texts = session.texts_to(A)
            check(any("Битва окончена" in text and "4/5" in text for text in battle_texts),
                  "после пятого вопроса показан итог 4/5")

    check(await db.get_battle(battle_id) is None, "завершённая игра удалена из SQLite")
    await press(ADMIN, "adm:games:active")
    check("Сейчас здесь пусто" in session.last_to(ADMIN), "история игр не хранится")
    await press(A, "game:again")
    check("5 вопросов" in str(session.to(A)[-1].get("reply_markup")),
          "сыграть ещё работает без хранения старой игры")

    # Новая игра «Числа»: три раунда, диапазон выбирает инициатор.
    await send(A, "/game")
    check("Числа" in str(session.to(A)[-1].get("reply_markup")),
          "в меню игр появилась игра Числа")
    await press(A, "game:numbers")
    range_markup = str(session.to(A)[-1].get("reply_markup"))
    check("1–10" in range_markup and "1–1000" in range_markup,
          "инициатор выбирает диапазон игры Числа")

    xp_a_before = int((await db.get_user(A))["xp"])
    xp_b_before = int((await db.get_user(B))["xp"])
    number_reward_a_before = await db.number_daily_reward(A)
    number_reward_b_before = await db.number_daily_reward(B)
    await press(A, "game:numbers:range:10")
    number_game = await db.number_for_pair(A, B)
    check(bool(number_game and number_game["status"] == "invited"),
          "предложение игры Числа сохранено в SQLite")
    number_id = int(number_game["id"])
    check("Числа" in session.last_to(B) and "3" in session.last_to(B),
          "собеседник получает приглашение на три раунда")
    await press(B, f"game:num:yes:{number_id}")
    check("раунд 1/3" in session.last_to(A).lower()
          and "раунд 1/3" in session.last_to(B).lower(),
          "после согласия начинается первый раунд")

    # 1-й раунд: точное совпадение 5 и 5 = +25 каждому.
    await press(A, f"game:num:set:{number_id}:0:5")
    await press(A, f"game:num:submit:{number_id}:0:5")
    await press(B, f"game:num:set:{number_id}:0:5")
    await press(B, f"game:num:submit:{number_id}:0:5")
    check("Точное совпадение" in session.last_to(A) and "25" in session.last_to(A),
          "точное совпадение начисляет 25 звёзд в диапазоне 1–10")

    await press(A, f"game:num:next:{number_id}:0")
    check("раунд 2/3" in session.last_to(B).lower(), "игра переходит ко второму раунду")

    # 2-й раунд: разница ровно 1 = половина награды, то есть 12 целых ⭐.
    await press(A, f"game:num:set:{number_id}:1:4")
    await press(A, f"game:num:submit:{number_id}:1:4")
    await press(B, f"game:num:set:{number_id}:1:5")
    await press(B, f"game:num:submit:{number_id}:1:5")
    check("Почти совпало" in session.last_to(A) and "12" in session.last_to(A),
          "разница в один даёт половину целой награды")

    await press(B, f"game:num:next:{number_id}:1")
    check("раунд 3/3" in session.last_to(A).lower(), "игра переходит к третьему раунду")

    # 3-й раунд: далеко друг от друга = без награды.
    await press(A, f"game:num:set:{number_id}:2:1")
    await press(A, f"game:num:submit:{number_id}:2:1")
    await press(B, f"game:num:set:{number_id}:2:9")
    await press(B, f"game:num:submit:{number_id}:2:9")
    number_texts = session.texts_to(A)
    check(any("Игра окончена" in text and "37" in text for text in number_texts),
          "после трёх раундов показан общий заработок")
    check(await db.number_for_pair(A, B) is None,
          "завершённая игра Числа не хранится как история")
    check(await db.number_daily_reward(A) == number_reward_a_before + 37
          and await db.number_daily_reward(B) == number_reward_b_before + 37,
          "награды Чисел начисляются обоим игрокам")
    check(int((await db.get_user(A))["xp"]) >= xp_a_before + 37
          and int((await db.get_user(B))["xp"]) >= xp_b_before + 37,
          "дополнительные достижения не уменьшают награду Чисел")

    await press(ADMIN, "adm:panel:monitor")
    session.clear()
    await send(A, "слежение выключено")
    check(session.to(ADMIN) == [], "выключенное слежение не присылает копии")
    await press(ADMIN, "adm:panel:monitor")
    session.clear()
    await send(A, "слежение включено")
    check("слежение включено" in session.last_to(ADMIN),
          "слежение включается сразу без потери сообщений")

    await send(ADMIN, f"/adminperms {D} all")
    await send(D, "/admin")
    await press(D, "adm:panel:monitor")

    session.clear()
    await send(A, "@secret_user")
    check("@secret_user" in session.last_to(B), "обычный @username пересылается")
    check("@user1001" in session.last_to(ADMIN) and "@user1002" in session.last_to(ADMIN)
          and "@secret_user" in session.last_to(ADMIN),
          "владелец видит username обоих собеседников и текст")
    check("@secret_user" in session.last_to(D),
          "админ с правами all видит активные чаты")
    session.clear()
    await send(A, "+7 999 123-45-67")
    check("+7 999 123-45-67" in session.last_to(B), "телефон пересылается")
    session.clear()
    await send(A, "https://t.me/example")
    check("https://t.me/example" in session.last_to(B), "ссылка t.me пересылается")
    session.clear()
    await send(A, "https://example.com")
    check("https://example.com" in session.last_to(B), "ссылка пересылается собеседнику")

    session.clear()
    await send(A, "/send @explicit_user")
    check(session.to(B) == [] and "Команда не найдена" in session.last_to(A), "/send удалена")
    session.clear()
    await send(A, "/user https://t.me/example")
    check(session.to(B) == [] and "Команда не найдена" in session.last_to(A), "/user удалена")

    session.clear()
    await payload(A, contact={"phone_number": "+79991234567", "first_name": "X"})
    check(bool(session.to(B)), "контакт Telegram пересылается")
    check(any(item["method"] == "sendContact" for item in session.to(ADMIN)),
          "владелец получает копию медиа и контактов из чата")

    session.clear()
    await payload(A, location={"latitude": 53.4, "longitude": 58.9})
    check(any(item["method"] == "sendLocation" for item in session.to(B)),
          "геолокация пересылается собеседнику")

    session.clear()
    await payload(A, document={"file_id": "f", "file_unique_id": "u", "file_name": "archive.zip"})
    check(any(item["method"] == "sendDocument" for item in session.to(B)),
          "обычный документ пересылается собеседнику")

    session.clear()
    await payload(
        A,
        venue={
            "location": {"latitude": 53.4, "longitude": 58.9},
            "title": "Место",
            "address": "Магнитогорск",
        },
    )
    check(any(item["method"] == "sendVenue" for item in session.to(B)),
          "место/venue пересылается собеседнику")

    await send(A, "обычное сообщение")
    await send(B, "ответ")
    await press(A, "act:report")
    await press(A, "rep:spam")
    await send(A, "мешает общаться")
    reports = await db.list_reports("new")
    check(len(reports) == 1 and "обычное сообщение" in reports[0]["context"],
          "жалоба сохраняет только последние сообщения как контекст")
    check(await db.is_restricted(B) is None, "AUTO_MUTE_REPORTS=0 полностью отключает авто-мут")
    await press(A, "act:report")
    await press(A, "rep:spam")
    await send(A, "повтор")
    check(len(await db.list_reports("new")) == 1 and texts.REPORT_DUPLICATE in session.last_to(A),
          "повторная жалоба на тот же диалог не считается")

    await send(A, "/game")
    await press(A, "game:battle")
    await press(A, "game:battle:10")
    active_battle = await db.battle_for_pair(A, B)
    check(int(active_battle["total_questions"]) == 10, "выбор десяти вопросов сохраняется в SQLite")
    await press(B, f"game:yes:{int(active_battle['id'])}")
    await send(A, "/stop")
    closed_battle = await db.get_battle(int(active_battle["id"]))
    check(closed_battle is None, "/stop удаляет активную игру из SQLite")
    dialog_results = session.texts_to(A)
    check(any("Итог разговора" in text and "Отправлено сообщений:" in text and "Получено:" in text for text in dialog_results),
          "после диалога показывается персональный итог разговора")
    check(any("Битва мнений:" in text and "Числа:" in text for text in dialog_results),
          "итог диалога показывает только реально сыгранные игры")
    session.clear()
    check(B not in await db.excluded_partners(A, recent_seconds=0),
          "вечной блокировки собеседника больше нет")
    await press(A, "act:connect")
    await press(B, "act:connect")
    check(mm.partner(A) != B, "недавняя пара временно не соединяется повторно")
    await press(C, "act:connect")
    check(mm.partner(A) == C, "очередь выбирает следующего подходящего пользователя")

    await send(A, "/stop")
    await press(A, "act:settings")
    await press(A, "cfg:nick:ask")
    await send(A, "/start")
    old_nick = (await db.get_user(A))["nickname"]
    await send(A, "не новый ник")
    check((await db.get_user(A))["nickname"] == old_nick, "/start очищает FSM ввода ника")
    await press(A, "cfg:nick:ask")
    await press(A, "act:menu")
    old_nick = (await db.get_user(A))["nickname"]
    await send(A, "это обычный текст")
    check((await db.get_user(A))["nickname"] == old_nick, "выход в меню очищает FSM ввода")

    await db.set_ban(C, True, "тест")
    await db.forget_user(C)
    check(await db.is_restricted(C) == "banned", "/forget не снимает действующий бан")

    await onboard(D, 17)
    await onboard(E, 18)
    for uid in (A, B, C, ADMIN, D, E):
        mm.forget(uid)
    await press(D, "act:connect")
    await press(E, "act:connect")
    session.clear()
    await send(E, "сообщение админу в диалоге")
    check("Собеседник:" in session.last_to(D),
          "админ видит данные даже в своём активном чате")
    session.fail_once["sendMessage"] = "temp"
    await send(D, "временная ошибка")
    check(mm.partner(D) == E, "временная ошибка Telegram не разрывает пару")
    check(mm.dialog_stats(D).get("counts", {}).get(D, 0) == 1,
          "после успешного повтора сообщение учитывается один раз")
    check("временная ошибка" in session.last_to(E),
          "после временной ошибки сообщение доставляется повторной попыткой")
    session.fail_once["sendMessage"] = "forbidden"
    await send(D, "недоступен")
    check(mm.status(D) == "free" and mm.status(E) == "free", "UNAVAILABLE разрывает пару")

    abuser, fake = 7_300_000_001, 7_300_000_002
    await db.ensure_user(abuser, "abuser", "Накрутчик")
    await db.ensure_user(fake, "fake", "Фейк")
    check(await db.award_referral(fake, abuser, 50), "тестовая реферальная связь создана")
    session.clear()
    await send(A, f"/purge_referrals {abuser}")
    check("только владельцу" in session.last_to(A).lower(), "очистка закрыта от обычных пользователей")
    await send(ADMIN, f"/purge_referrals {abuser}")
    check("Предпросмотр очистки" in session.last_to(ADMIN), "владелец сначала видит предпросмотр")
    await press(ADMIN, f"adm:purge_refs:{abuser}")
    check(await db.get_user(fake) is None, "подтверждение удаляет накрученный профиль")
    check(int((await db.get_user(abuser))["xp"]) == 0, "подтверждение обнуляет очки накрутчика")
    check("Реферальная накрутка удалена" in session.last_to(ADMIN), "владелец получает итог очистки")

    # Новые экраны профиля, /ref, топы и диагностика.
    session.clear()
    await send(A, "/ref")
    check("Пригласить друга" in session.last_to(A), "/ref открывает отдельный реферальный экран")
    ref_message = next(item for item in reversed(session.to(A)) if item.get("reply_markup"))
    ref_markup = str(ref_message.get("reply_markup"))
    check("Скопировать ссылку" in ref_markup and "Отправить другу" in ref_markup,
          "/ref показывает copy/share кнопки")

    await send(A, "/profile")
    profile_message = next(item for item in reversed(session.to(A)) if item.get("reply_markup"))
    profile_markup = str(profile_message.get("reply_markup"))
    for label in ("Моя активность", "Серия активности", "Квесты дня", "Реферальная ссылка"):
        check(label in profile_markup, f"в профиле есть «{label}»")

    await press(A, "profile:activity")
    check("Моя активность" in session.last_to(A), "экран собственной активности открывается")
    await press(A, "profile:streak")
    check("Серия активности" in session.last_to(A), "экран серии активности открывается")
    await press(A, "profile:quests")
    check("Квесты дня" in session.last_to(A), "экран ежедневных квестов открывается")

    await send(A, "/top")
    check("Топ · Неделя" in session.last_to(A), "топ по умолчанию открывается за неделю")
    await press(A, "top:month")
    check("Топ · Месяц" in session.last_to(A), "топ переключается на месяц")
    await press(A, "top:all")
    check("Топ · Всё время" in session.last_to(A), "топ переключается на всё время")

    session.clear()
    await send(ADMIN, "/admin")
    await press(ADMIN, "adm:panel:diagnostics")
    check("Диагностика" in session.last_to(ADMIN) and "Reply-map" in session.last_to(ADMIN),
          "админская диагностика открывается")

    await bot.session.close()
    await db.close()
    print("\nmodern flow test passed")


def test_flow() -> None:
    """Гарантированно закрываем БД: иначе worker-поток aiosqlite вешает процесс при упавшем асерте."""
    holder: dict[str, Any] = {}

    async def guarded() -> None:
        try:
            await run_flow_modern(holder)
        finally:
            db = holder.get("db")
            if db is not None:
                await db.close()

    asyncio.run(guarded())


if __name__ == "__main__":
    test_flow()
