"""Same-process HTTP API и статика Telegram Mini App."""

from __future__ import annotations

import hashlib
import html
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web
from aiogram.exceptions import TelegramAPIError

from . import keyboards as K
from . import nick as nicklib
from .actions import DeliveryResult, announce_pairs, send_to
from .levels import rank_for
from .number_game import NUMBER_DAILY_REWARD_LIMIT, NUMBER_NEAR_DIFFS, NUMBER_REWARDS, NUMBER_ROUNDS
from .runtime_state import online_count as presence_online_count
from .runtime_state import touch as presence_touch


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
    return value if value.startswith("@") else f"@{value}"


def _subscription_url(channel: str, explicit_url: str = "") -> str:
    if str(explicit_url or "").strip():
        return str(explicit_url).strip()
    value = str(channel or "").strip()
    if value.startswith("https://t.me/"):
        return value
    return f"https://t.me/{value.lstrip('@')}" if value and not value.lstrip("-").isdigit() else ""


def _member_is_subscribed(member: object) -> bool:
    status = getattr(getattr(member, "status", ""), "value", getattr(member, "status", ""))
    status = str(status)
    return status in {"member", "administrator", "creator"} or (
        status == "restricted" and bool(getattr(member, "is_member", False))
    )


def _json_error(status: int, message: str) -> web.HTTPException:
    classes = {
        400: web.HTTPBadRequest,
        401: web.HTTPUnauthorized,
        403: web.HTTPForbidden,
        404: web.HTTPNotFound,
        409: web.HTTPConflict,
        413: web.HTTPRequestEntityTooLarge,
        503: web.HTTPServiceUnavailable,
    }
    return classes.get(status, web.HTTPBadRequest)(
        text=json.dumps({"message": message}, ensure_ascii=False),
        content_type="application/json",
    )


def validate_init_data(raw: str, bot_token: str, max_age: int = 3600) -> dict:
    """Проверяет подпись Telegram WebApp initData по официальной HMAC-схеме."""
    if not raw:
        raise _json_error(401, "Открой Mini App внутри Telegram")
    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise _json_error(401, "Нет подписи Telegram")
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise _json_error(401, "Неверная подпись Telegram")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise _json_error(401, "Некорректная дата сессии") from exc
    if max_age > 0 and (not auth_date or abs(int(time.time()) - auth_date) > max_age):
        raise _json_error(401, "Сессия Mini App устарела")
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise _json_error(401, "Не удалось прочитать пользователя") from exc
    if not isinstance(user, dict) or not int(user.get("id", 0) or 0):
        raise _json_error(401, "Telegram user не найден")
    return user


class MiniAppServer:
    def __init__(self, bot, cfg, db, mm, pack, *, web_dir: str | Path | None = None) -> None:
        self.bot = bot
        self.cfg = cfg
        self.db = db
        self.mm = mm
        self.pack = pack
        self.web_dir = Path(
            web_dir or os.getenv("MINIAPP_WEB_DIR", "miniapp/web")
        ).resolve()
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._bot_username = ""
        self.serve_static = os.getenv("MINIAPP_SERVE_STATIC", "true").lower() in {
            "1", "true", "yes", "on"
        }
        self.allowed_origins = {
            item.strip().rstrip("/")
            for item in os.getenv("MINIAPP_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        }

    async def _auth(self, request: web.Request) -> tuple[int, dict, object]:
        raw = request.headers.get("X-Telegram-Init-Data", "")
        user = validate_init_data(
            raw,
            self.cfg.bot_token,
            int(os.getenv("MINIAPP_INITDATA_MAX_AGE", "3600")),
        )
        user_id = int(user["id"])
        presence_touch(user_id)
        row = await self.db.ensure_user(
            user_id, user.get("username"), user.get("first_name") or "Пользователь"
        )
        return user_id, user, row

    def _profile_json(self, row, user: dict) -> dict:
        rank = rank_for(int(row["messages"] or 0))
        return {
            "id": int(row["user_id"]),
            "nick": nicklib.display(
                row["nickname"], int(row["user_id"]), int(row["support_stars"] or 0)
            ),
            "photo_url": user.get("photo_url") or "",
            "rank": rank.title,
            "stars": int(row["xp"] or 0),
            "age": int(row["age"] or 0),
            "district": str(row["district"] or ""),
            "gender": str(row["gender"] or ""),
            "looking_for": str(row["looking_for"] or ""),
        }

    async def _stats(self, user_id: int, row=None) -> dict:
        row = row or await self.db.get_user(user_id)
        today = await self.db.activity_totals(user_id, 1)
        engagement = await self.db.engagement_state(user_id)
        return {
            "online": presence_online_count(),
            "chatting": self.mm.online_pairs() * 2,
            "searching": self.mm.queue_size(),
            "dialogs": int(row["dialogs"] or 0) if row else 0,
            "messages": int(row["messages"] or 0) if row else 0,
            "ratings": int(row["good_ratings"] or 0) if row else 0,
            "games": int(engagement["games_total"] or 0),
            "battle_games": int(engagement["battle_games_total"] or 0),
            "number_games": int(engagement["number_games_total"] or 0),
            "streak": int(engagement["current_streak"] or 0),
            "best_streak": int(engagement["best_streak"] or 0),
            "quest_current": int(today.get("messages", 0)),
            "quest_target": 20,
        }

    async def _username(self) -> str:
        if not self._bot_username:
            me = await self.bot.get_me()
            self._bot_username = me.username or "AnonChatMgn_Bot"
        return self._bot_username

    async def me(self, request: web.Request) -> web.Response:
        uid, user, row = await self._auth(request)
        invited, earned = await self.db.referral_stats(uid)
        bot_username = await self._username()
        return web.json_response(
            {
                "user": self._profile_json(row, user),
                "stats": await self._stats(uid, row),
                "status": self.mm.status(uid),
                "position": self.mm.position(uid),
                "referral": {"invited": invited, "earned": earned},
                "referral_url": f"https://t.me/{bot_username}?start=ref_{uid}",
                "bot_url": f"https://t.me/{bot_username}",
                "notifications": await self._notifications(uid),
            }
        )

    async def settings(self, request: web.Request) -> web.Response:
        uid, user, _ = await self._auth(request)
        if self.mm.status(uid) == "paired":
            raise _json_error(409, "Настройки нельзя менять во время диалога")
        data = await request.json()
        try:
            age = int(data.get("age", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise _json_error(400, "Некорректный возраст") from exc
        if age and age not in range(13, 21):
            raise _json_error(400, "Возраст: 13–20 или не указывать")
        districts = {"", "Правый берег", "Левый берег"}
        district = str(data.get("district", "") or "")
        if district not in districts:
            raise _json_error(400, "Неизвестный берег")
        gender = str(data.get("gender", "") or "")
        looking_for = str(data.get("looking_for", "") or "")
        if gender not in {"", "m", "f"} or looking_for not in {"", "m", "f"}:
            raise _json_error(400, "Некорректные настройки поиска")
        await self.db.set_profile(
            uid, age=age, district=district, same_district=0,
            gender=gender, looking_for=looking_for,
        )
        row = await self.db.get_user(uid)
        if self.mm.status(uid) == "queued" and row is not None:
            pairs = self.mm.refresh(
                uid, district=district, same_district=False,
                gender=gender, looking_for=looking_for,
            )
            if pairs:
                await announce_pairs(self.bot, self.cfg, self.mm, pairs, self.pack, self.db)
            self.db.schedule_matchmaker_save(self.mm)
        return web.json_response({"user": self._profile_json(row, user)})

    async def settings_reset(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        if self.mm.status(uid) == "paired":
            raise _json_error(409, "Сначала заверши диалог")
        await self.db.set_profile(
            uid, age=0, district="", same_district=0, gender="", looking_for=""
        )
        if self.mm.status(uid) == "queued":
            pairs = self.mm.refresh(
                uid, district="", same_district=False, gender="", looking_for=""
            )
            if pairs:
                await announce_pairs(self.bot, self.cfg, self.mm, pairs, self.pack, self.db)
            self.db.schedule_matchmaker_save(self.mm)
        return web.json_response({"ok": True})

    async def nick(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        if self.mm.status(uid) == "paired":
            raise _json_error(409, "Ник нельзя менять во время диалога")
        data = await request.json()
        candidate, error = nicklib.validate(str(data.get("nick", "")).strip())
        if error:
            raise _json_error(400, str(error))
        if await self.db.nickname_taken(candidate, except_user_id=uid):
            raise _json_error(409, "Этот ник уже занят")
        await self.db.set_profile(uid, nickname=candidate)
        row = await self.db.get_user(uid)
        return web.json_response(
            {"nick": nicklib.display(row["nickname"], uid, int(row["support_stars"] or 0))}
        )

    async def search_start(self, request: web.Request) -> web.Response:
        uid, _, row = await self._auth(request)
        if bool(row["banned"]):
            raise _json_error(403, "Поиск недоступен")
        if int(row["mute_until"] or 0) > int(time.time()):
            raise _json_error(403, "Поиск временно недоступен")
        if self.mm.status(uid) == "paired":
            return web.json_response({"status": "paired", "stats": await self._stats(uid, row)})
        excluded = await self.db.excluded_partners(uid)
        outcome, payload = self.mm.connect(
            uid,
            district=str(row["district"] or ""),
            same_district=False,
            gender=str(row["gender"] or ""),
            looking_for=str(row["looking_for"] or ""),
            excluded=excluded,
        )
        if outcome == "full":
            raise _json_error(503, "Очередь заполнена. Попробуй чуть позже")
        if outcome == "paired":
            made = await announce_pairs(
                self.bot, self.cfg, self.mm, [(uid, int(payload))], self.pack, self.db
            )
            if not made:
                self.mm.forget(uid)
                outcome, payload = "queued", self.mm.connect(
                    uid,
                    district=str(row["district"] or ""),
                    same_district=False,
                    gender=str(row["gender"] or ""),
                    looking_for=str(row["looking_for"] or ""),
                    excluded=excluded,
                )[1]
        self.db.schedule_matchmaker_save(self.mm)
        return web.json_response(
            {
                "status": outcome,
                "position": payload if outcome == "queued" else None,
                "stats": await self._stats(uid, row),
            }
        )

    async def search_stop(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        status = self.mm.status(uid)
        if status == "paired":
            raise _json_error(409, "Активный диалог заверши в боте через Стоп")
        if status == "queued":
            self.mm.forget(uid)
            self.db.schedule_matchmaker_save(self.mm)
        return web.json_response({"status": "free"})

    async def online(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        current = presence_online_count()
        chatting = self.mm.online_pairs() * 2
        searching = self.mm.queue_size()
        key = time.strftime("miniapp_peak:%Y-%m-%d", time.gmtime(time.time() + 18000))
        peak = max(current, int(await self.db.get_kv(key, "0") or 0))
        if peak == current:
            await self.db.set_kv(key, str(peak))
        return web.json_response(
            {
                "online": current,
                "chatting": chatting,
                "searching": searching,
                "free": max(0, current - chatting - searching),
                "peak": peak,
                "status": self.mm.status(uid),
            }
        )

    async def activity(self, request: web.Request) -> web.Response:
        uid, _, row = await self._auth(request)
        engagement = await self.db.engagement_state(uid)
        result = {
            "today": await self.db.activity_totals(uid, 1),
            "week": await self.db.activity_totals(uid, 7),
            "month": await self.db.activity_totals(uid, 30),
            "all": {
                "dialogs": int(row["dialogs"] or 0),
                "messages": int(row["messages"] or 0),
                "games": int(engagement["games_total"] or 0),
                "battle_games": int(engagement["battle_games_total"] or 0),
                "number_games": int(engagement["number_games_total"] or 0),
                "good_ratings": int(row["good_ratings"] or 0),
            },
        }
        return web.json_response(result)

    async def streak(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        row = await self.db.engagement_state(uid)
        return web.json_response(
            {"current": int(row["current_streak"] or 0), "best": int(row["best_streak"] or 0)}
        )

    async def quests(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        today = await self.db.activity_totals(uid, 1)
        specs = (("Отправь 20 сообщений", "messages", 20), ("Проведи 3 диалога", "dialogs", 3), ("Сыграй 1 игру", "games", 1))
        items = []
        for title, key, target in specs:
            current = min(target, int(today[key]))
            items.append({"title": title, "current": current, "target": target, "done": current >= target})
        return web.json_response({"items": items})

    async def top(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        rows = await self.db.top(10)
        return web.json_response(
            {
                "items": [
                    {
                        "place": place,
                        "user_id": int(row["user_id"]),
                        "nick": nicklib.display(row["nickname"], int(row["user_id"]), row["support_stars"]),
                        "stars": int(row["xp"] or 0),
                        "rank": rank_for(int(row["messages"] or 0)).title,
                    }
                    for place, row in enumerate(rows, 1)
                ],
                "me": uid,
            }
        )

    async def _notifications(self, uid: int) -> list[dict]:
        status = self.mm.status(uid)
        if status == "paired":
            return [{"id": "dialog-active", "type": "personal", "icon": "message-circle", "title": "Диалог активен", "text": "Собеседник найден. Возвращайся в чат.", "time": "сейчас", "unread": True}]
        if status == "queued":
            return [{"id": "search-active", "type": "system", "icon": "search", "title": "Поиск идёт", "text": "Можно закрыть Mini App — очередь сохранится.", "time": "сейчас", "unread": True}]
        return []

    async def notifications(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        return web.json_response({"items": await self._notifications(uid)})

    async def notification_read(self, request: web.Request) -> web.Response:
        await self._auth(request)
        return web.json_response({"ok": True})

    async def feedback(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        data = await request.json()
        text = str(data.get("text", "")).strip()
        if not 1 <= len(text) <= 1000:
            raise _json_error(400, "Сообщение должно быть от 1 до 1000 символов")
        admin_ids = await self.db.all_admin_ids(self.cfg.admin_ids)
        sent = 0
        for admin_id in admin_ids:
            try:
                await self.bot.send_message(
                    admin_id,
                    f"💬 <b>Обратная связь из Mini App</b>\n\n{html.escape(text)}\n\n<code>{uid}</code>",
                )
                sent += 1
            except TelegramAPIError:
                pass
        if admin_ids and not sent:
            raise _json_error(503, "Не получилось доставить сообщение")
        return web.json_response({"ok": True})

    async def game_battle(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        partner = self.mm.partner(uid)
        if partner is None:
            raise _json_error(409, "Сначала найди собеседника")
        data = await request.json()
        total = int(data.get("total", 10) or 10)
        if total not in {5, 10}:
            total = 10
        existing = await self.db.game_for_pair(uid, partner)
        if existing is not None:
            raise _json_error(409, "У вас уже есть активная игра")
        game, created = await self.db.create_battle_invite(uid, partner, total)
        if not created:
            raise _json_error(409, "Предложение уже создано")
        result = await send_to(
            self.bot,
            partner,
            f"⚔️ <b>Собеседник предлагает сыграть в Битву мнений</b>\nВопросов: <b>{total}</b>",
            K.battle_invite_keyboard(int(game["id"])),
            self.pack,
        )
        if result is DeliveryResult.UNAVAILABLE:
            await self.db.cancel_battle(int(game["id"]))
            raise _json_error(503, "Не удалось доставить приглашение")
        return web.json_response({"ok": True, "message": "Приглашение отправлено"})

    async def game_numbers(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        partner = self.mm.partner(uid)
        if partner is None:
            raise _json_error(409, "Сначала найди собеседника")
        data = await request.json()
        try:
            range_max = int(data.get("range_max", 0))
        except (TypeError, ValueError) as exc:
            raise _json_error(400, "Неверный диапазон") from exc
        if range_max not in NUMBER_REWARDS:
            raise _json_error(400, "Можно выбрать 1–10, 1–100 или 1–1000")
        if await self.db.game_for_pair(uid, partner) is not None:
            raise _json_error(409, "У вас уже есть активная игра")
        reward_available = await self.db.number_pair_reward_available(uid, partner)
        game, created = await self.db.create_number_invite(uid, partner, range_max)
        if not created:
            raise _json_error(409, "Предложение уже создано")
        reward = NUMBER_REWARDS[range_max]
        near = reward // 2
        result = await send_to(
            self.bot,
            partner,
            f"🔢 <b>Собеседник предлагает сыграть в Числа</b>\n"
            f"Диапазон: <b>1–{range_max}</b> · раундов: <b>{NUMBER_ROUNDS}</b>\n"
            f"Точное совпадение: <b>{reward} ⭐</b> · "
            f"разница до {NUMBER_NEAR_DIFFS[range_max]}: <b>{near} ⭐</b>\n"
            + (
                f"Награды доступны · дневной лимит {NUMBER_DAILY_REWARD_LIMIT} ⭐."
                if reward_available else "Вы уже играли вместе — эта игра будет без награды."
            ),
            K.number_invite_keyboard(int(game["id"])),
            self.pack,
        )
        if result is DeliveryResult.UNAVAILABLE:
            await self.db.cancel_battle(int(game["id"]))
            raise _json_error(503, "Не удалось доставить приглашение")
        return web.json_response({"ok": True, "message": "Приглашение отправлено"})

    async def subscription(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        return web.json_response(
            {
                "claimed": await self.db.reward_claimed(uid, SUBSCRIPTION_REWARD_KEY),
                "amount": max(1, int(self.cfg.subscription_reward)),
                "url": _subscription_url(
                    self.cfg.subscription_channel, self.cfg.subscription_channel_url
                ),
            }
        )

    async def subscription_claim(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        if await self.db.reward_claimed(uid, SUBSCRIPTION_REWARD_KEY):
            raise _json_error(409, "Эта награда уже получена")
        target = _subscription_chat_id(self.cfg.subscription_channel)
        if target is None:
            raise _json_error(503, "Канал пока не подключён")
        try:
            member = await self.bot.get_chat_member(chat_id=target, user_id=uid)
        except TelegramAPIError as exc:
            raise _json_error(503, "Не удалось проверить подписку") from exc
        if not _member_is_subscribed(member):
            raise _json_error(403, "Сначала подпишись на канал")
        amount = max(1, int(self.cfg.subscription_reward))
        if not await self.db.claim_one_time_reward(uid, SUBSCRIPTION_REWARD_KEY, amount):
            raise _json_error(409, "Эта награда уже получена")
        row = await self.db.get_user(uid)
        return web.json_response(
            {"ok": True, "amount": amount, "stars": int(row["xp"] or 0) if row else 0}
        )

    async def forget(self, request: web.Request) -> web.Response:
        uid, _, _ = await self._auth(request)
        if self.mm.status(uid) == "paired":
            raise _json_error(409, "Сначала заверши текущий диалог")
        self.mm.forget(uid)
        await self.db.forget_user(uid)
        self.db.schedule_matchmaker_save(self.mm)
        return web.json_response({"ok": True})

    async def health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "anon-mgn-miniapp"})

    @web.middleware
    async def security_headers(self, request: web.Request, handler):
        origin = request.headers.get("Origin", "").rstrip("/")
        origin_host = urlsplit(origin).netloc.lower() if origin else ""
        request_host = request.headers.get("X-Forwarded-Host", request.host).lower()
        same_origin = bool(origin_host and origin_host == request_host)
        origin_allowed = same_origin or origin in self.allowed_origins
        if origin and request.path.startswith("/api/") and not origin_allowed:
            raise _json_error(403, "Источник Mini App не разрешён")
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                response = exc
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; "
            "connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org"
        )
        if origin and origin_allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, X-Telegram-Init-Data"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Max-Age"] = "600"
        return response

    def create_app(self) -> web.Application:
        if self.serve_static and (
            not self.web_dir.is_dir() or not (self.web_dir / "index.html").is_file()
        ):
            raise RuntimeError(f"MINIAPP_WEB_DIR не найден: {self.web_dir}")
        app = web.Application(client_max_size=64 * 1024, middlewares=[self.security_headers])
        app.router.add_get("/api/miniapp/health", self.health)
        app.router.add_get("/api/miniapp/me", self.me)
        app.router.add_post("/api/miniapp/settings", self.settings)
        app.router.add_post("/api/miniapp/settings/reset", self.settings_reset)
        app.router.add_post("/api/miniapp/profile/nick", self.nick)
        app.router.add_post("/api/miniapp/profile/forget", self.forget)
        app.router.add_post("/api/miniapp/search/start", self.search_start)
        app.router.add_post("/api/miniapp/search/stop", self.search_stop)
        app.router.add_get("/api/miniapp/online", self.online)
        app.router.add_get("/api/miniapp/activity", self.activity)
        app.router.add_get("/api/miniapp/streak", self.streak)
        app.router.add_get("/api/miniapp/quests", self.quests)
        app.router.add_get("/api/miniapp/top", self.top)
        app.router.add_get("/api/miniapp/notifications", self.notifications)
        app.router.add_post("/api/miniapp/notifications/{notification_id}/read", self.notification_read)
        app.router.add_post("/api/miniapp/feedback", self.feedback)
        app.router.add_post("/api/miniapp/games/battle/invite", self.game_battle)
        app.router.add_post("/api/miniapp/games/numbers/invite", self.game_numbers)
        app.router.add_get("/api/miniapp/subscription", self.subscription)
        app.router.add_post("/api/miniapp/subscription/claim", self.subscription_claim)
        app.router.add_route("OPTIONS", "/api/miniapp/{tail:.*}", self.health)

        async def index(_request: web.Request) -> web.StreamResponse:
            return web.FileResponse(self.web_dir / "index.html")

        if self.serve_static:
            app.router.add_get("/", index)
            app.router.add_static("/", self.web_dir, show_index=False, append_version=True)
        return app

    async def start(self) -> None:
        if self.runner is not None:
            return
        self.runner = web.AppRunner(self.create_app(), access_log=None)
        await self.runner.setup()
        host = os.getenv("MINIAPP_HOST", "0.0.0.0")
        port = int(os.getenv("PORT", os.getenv("MINIAPP_PORT", "8080")))
        self.site = web.TCPSite(self.runner, host=host, port=port)
        await self.site.start()

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None


async def start_miniapp_server(bot, cfg, db, mm, pack, *, web_dir=None) -> MiniAppServer:
    server = MiniAppServer(bot, cfg, db, mm, pack, web_dir=web_dir)
    await server.start()
    return server
