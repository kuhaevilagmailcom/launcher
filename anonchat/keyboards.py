"""Инлайн-клавиатуры.

Здесь нет юникодных эмодзи в подписях: маркер кнопки — анимированный эмодзи из
пака NewsEmoji в поле ``icon_custom_emoji_id`` (тот же приём, что в limuzinov_shop_bot).
``style`` — цвет кнопки: основной/action/опасный.
"""

from __future__ import annotations

from urllib.parse import quote

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .pack import ICONS

# callback_data
CB_CONNECT = "act:connect"
CB_NEXT = "act:next"
CB_STOP = "act:stop"
CB_STOP_YES = "act:stop:yes"
CB_STOP_NO = "act:stop:no"
CB_REPORT = "act:report"
CB_MENU = "act:menu"
CB_PROFILE = "act:profile"
CB_SETTINGS = "act:settings"
CB_HELP = "act:help"
CB_RULES = "act:rules"
CB_TOP = "act:top"
CB_SUPPORT = "act:support"
CB_CONTINUE = "onboard:continue"
CB_NICK = "cfg:nick:ask"
CB_GAMES = "game:menu"
CB_BATTLE = "game:battle"
CB_NUMBERS = "game:numbers"
CB_WORDS = "game:words"
CB_FEEDBACK = "cfg:feedback"
CB_ACTIVITY = "profile:activity"
CB_STREAK = "profile:streak"
CB_QUESTS = "profile:quests"
CB_REFERRAL = "profile:ref"
CB_ONLINE = "cfg:online"
CB_POLL = "poll:open"
CB_SUBSCRIBE_REWARD = "profile:subscribe"
CB_SUBSCRIBE_CHECK = "profile:subscribe:check"


#: Bot API принимает только эти три цвета кнопки («warning» отвергает — проверено живьём)
STYLES = ("primary", "success", "danger")


def _button(
    b: InlineKeyboardBuilder, text: str, callback_data: str = "", url: str = "",
    icon: str = "", style: str = "",
) -> None:
    """Кнопка с иконкой из пака; если id неизвестен — обычная текстовая кнопка."""
    kwargs: dict[str, object] = {"text": text}
    if callback_data:
        kwargs["callback_data"] = callback_data
    if url:
        kwargs["url"] = url
    emoji_id = ICONS.get(icon)
    if emoji_id:
        kwargs["icon_custom_emoji_id"] = emoji_id
    if style in STYLES:
        kwargs["style"] = style
    b.button(**kwargs)


CB_ADMIN_PANEL = "adm:panel"


def menu_keyboard(
    status: str = "free", queue_size: int = 0, admin: bool = False, poll_active: bool = False
) -> InlineKeyboardMarkup:
    """Главное меню.

    В диалоге оставляем только действия диалога: профиль/настройки во время
    переписки не смотрят (и не тыкают в них собеседнику под «печатает…»).
    """
    b = InlineKeyboardBuilder()
    if status == "paired":
        _button(b, "Следующий", callback_data=CB_NEXT, icon="next")
        _button(b, "Стоп", callback_data=CB_STOP, icon="check", style="danger")
        _button(b, "Жалоба", callback_data=CB_REPORT, icon="warn")
        _button(b, "Игры", callback_data=CB_GAMES, icon="bonus", style="primary")
        b.adjust(1, 2, 1)
        return b.as_markup()

    if status == "queued":
        _button(b, "Отменить поиск", callback_data=CB_STOP, icon="check", style="danger")
        _button(b, "Профиль", callback_data=CB_PROFILE, icon="profile")
        _button(b, "Настройки", callback_data=CB_SETTINGS, icon="settings")
        b.adjust(1, 2)
        return b.as_markup()
    else:
        _button(b, "Найти собеседника", callback_data=CB_CONNECT, icon="view", style="success")
    _button(b, "Профиль", callback_data=CB_PROFILE, icon="profile")
    _button(b, "Настройки", callback_data=CB_SETTINGS, icon="settings")
    _button(b, "Топ", callback_data=CB_TOP, icon="stats")
    if poll_active:
        _button(b, "Опрос", callback_data=CB_POLL, icon="ticket", style="primary")
    _button(b, "Правила", callback_data=CB_RULES, icon="ticket")
    _button(b, "Помощь", callback_data=CB_HELP, icon="support")
    rows = [1, 2, 2, 1] if not poll_active else [1, 2, 2, 2]
    if admin:
        _button(b, "Панель модератора", callback_data=CB_ADMIN_PANEL, icon="bonus", style="primary")
        rows.append(1)
    b.adjust(*rows)
    return b.as_markup()


def continue_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Продолжить", callback_data=CB_CONTINUE, icon="next", style="success")
    return b.as_markup()


def age_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for age in range(13, 21):
        _button(b, str(age), callback_data=f"onboard:age:{age}")
    _button(b, "Не указывать", callback_data="onboard:age:0", icon="check")
    b.adjust(4, 4, 1)
    return b.as_markup()


def chat_keyboard() -> InlineKeyboardMarkup:
    return menu_keyboard("paired")


def profile_keyboard(
    referral_url: str = "", subscription_claimed: bool = False, subscription_reward: int = 100
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Моя активность", callback_data=CB_ACTIVITY, icon="stats")
    _button(b, "Серия активности", callback_data=CB_STREAK, icon="bonus")
    _button(b, "Квесты дня", callback_data=CB_QUESTS, icon="ticket")
    _button(b, "Реферальная ссылка", callback_data=CB_REFERRAL, icon="gift")

    # После успешного получения награды кнопку больше не показываем вообще.
    # Флаг берётся из БД через reward_claimed(), поэтому отписка и повторная
    # подписка не возвращают кнопку и не позволяют получить награду второй раз.
    if not subscription_claimed:
        _button(
            b, f"{int(subscription_reward)} ⭐️ за подписку",
            callback_data=CB_SUBSCRIBE_REWARD, icon="stars", style="success"
        )

    _button(b, "Изменить ник", callback_data=CB_NICK, icon="edit")
    _button(b, "Настройки", callback_data=CB_SETTINGS, icon="settings")
    _button(b, "Назад", callback_data=CB_MENU, icon="home")

    if subscription_claimed:
        b.adjust(2, 2, 2, 1)
    else:
        b.adjust(2, 2, 1, 2, 1)
    return b.as_markup()


def subscription_reward_keyboard(channel_url: str = "") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if channel_url:
        _button(b, "Подписаться", url=channel_url, icon="gift", style="success")
    _button(b, "Проверить подписку", callback_data=CB_SUBSCRIBE_CHECK, icon="check", style="primary")
    _button(b, "Назад в профиль", callback_data=CB_PROFILE, icon="home")
    b.adjust(1, 1, 1)
    return b.as_markup()


def referral_keyboard(referral_url: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(
        text="Скопировать ссылку",
        copy_text=CopyTextButton(text=referral_url),
        icon_custom_emoji_id=ICONS.get("link"),
    ))
    _button(
        b,
        "Отправить другу",
        url=f"https://t.me/share/url?url={quote(referral_url, safe='')}&text={quote('Заходи в анонимный чат', safe='')}",
        icon="gift",
    )
    _button(b, "Назад в профиль", callback_data=CB_PROFILE, icon="home")
    b.adjust(1, 1, 1)
    return b.as_markup()


def top_keyboard(period: str = "week") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    labels = (("week", "Неделя"), ("month", "Месяц"), ("all", "Всё время"))
    for key, title in labels:
        _button(
            b,
            f"• {title}" if key == period else title,
            callback_data=f"top:{key}",
            style="primary" if key == period else "",
        )
    _button(b, "Назад", callback_data=CB_MENU, icon="home")
    b.adjust(3, 1)
    return b.as_markup()


def profile_section_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Назад в профиль", callback_data=CB_PROFILE, icon="home")
    return b.as_markup()


def online_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Обновить", callback_data="cfg:online:refresh", icon="refresh", style="primary")
    _button(b, "Назад в настройки", callback_data=CB_SETTINGS, icon="home")
    b.adjust(1, 1)
    return b.as_markup()


def district_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for title, value in (
        ("Правый берег", "right"),
        ("Левый берег", "left"),
    ):
        _button(b, title, callback_data=f"cfg:district:{value}", icon="geo")
    _button(b, "Назад", callback_data=CB_SETTINGS, icon="home")
    b.adjust(1, 1, 1)
    return b.as_markup()


def gender_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "👨 М", callback_data="cfg:gender:m")
    _button(b, "👩 Д", callback_data="cfg:gender:f")
    _button(b, "Не указывать", callback_data="cfg:gender:none", icon="check")
    _button(b, "Назад", callback_data=CB_SETTINGS, icon="home")
    b.adjust(2, 1, 1)
    return b.as_markup()


def looking_for_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "👨 Ищу М", callback_data="cfg:looking:m")
    _button(b, "👩 Ищу Д", callback_data="cfg:looking:f")
    _button(b, "🤷 Без разницы", callback_data="cfg:looking:any")
    _button(b, "Назад", callback_data=CB_SETTINGS, icon="home")
    b.adjust(2, 1, 1)
    return b.as_markup()


def settings_keyboard(
    same_district: bool, district: str, nickname: str,
    gender: str = "", looking_for: str = "",
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    gender_label = "М" if gender == "m" else "Д" if gender == "f" else "не выбран"
    looking_label = "М" if looking_for == "m" else "Д" if looking_for == "f" else "Без разницы"
    _button(b, f"Ник: {nickname}", callback_data=CB_NICK, icon="profile")
    _button(b, f"Пол: {gender_label}", callback_data="cfg:gender:ask")
    _button(b, f"Ищу: {looking_label}", callback_data="cfg:looking:ask")
    _button(b, f"Берег: {district or 'не выбран'}", callback_data="cfg:district:ask", icon="geo")
    _button(b, "Возраст", callback_data="cfg:age:ask", icon="stars")
    _button(b, "Онлайн сейчас", callback_data=CB_ONLINE, icon="view")
    _button(b, "Обратная связь", callback_data=CB_FEEDBACK, icon="support")
    _button(b, "Поддержать проект", callback_data=CB_SUPPORT, icon="stars", style="success")
    _button(b, "Удалить профиль", callback_data="cfg:forget:ask", icon="delete", style="danger")
    _button(b, "В меню", callback_data=CB_MENU, icon="home")
    b.adjust(1, 1, 1, 1, 1, 1, 1, 1, 1, 1)
    return b.as_markup()


def games_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Битва мнений", callback_data=CB_BATTLE, icon="bonus", style="primary")
    _button(b, "Числа", callback_data=CB_NUMBERS, icon="stars", style="success")
    _button(b, "Объясни слово", callback_data=CB_WORDS, icon="ticket", style="primary")
    _button(b, "Вернуться в чат", callback_data="game:return", icon="home")
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def word_invite_keyboard(game_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Играть", callback_data=f"game:word:yes:{game_id}", icon="check", style="success")
    _button(b, "Не сейчас", callback_data=f"game:word:no:{game_id}", icon="delete")
    b.adjust(2)
    return b.as_markup()


def word_next_keyboard(game_id: int, round_index: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(
        b, "Следующее слово",
        callback_data=f"game:word:next:{game_id}:{round_index}",
        icon="next", style="primary",
    )
    _button(b, "Вернуться в чат", callback_data="game:return", icon="home")
    b.adjust(1, 1)
    return b.as_markup()


def word_end_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Сыграть ещё", callback_data=CB_WORDS, icon="refresh", style="success")
    _button(b, "Вернуться в чат", callback_data="game:return", icon="home")
    b.adjust(1, 1)
    return b.as_markup()


def feedback_admin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(
        b, "Ответить",
        callback_data=f"feedback:reply:{int(user_id)}",
        icon="edit", style="primary",
    )
    b.adjust(1)
    return b.as_markup()


def number_range_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "1–10 · 25 ⭐", callback_data="game:numbers:range:10", style="primary")
    _button(b, "1–100 · 50 ⭐", callback_data="game:numbers:range:100", style="primary")
    _button(b, "1–1000 · 100 ⭐", callback_data="game:numbers:range:1000", style="success")
    _button(b, "Назад", callback_data=CB_GAMES, icon="home")
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def number_invite_keyboard(game_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Играть", callback_data=f"game:num:yes:{game_id}", icon="check", style="success")
    _button(b, "Не сейчас", callback_data=f"game:num:no:{game_id}", icon="delete")
    b.adjust(2)
    return b.as_markup()


def number_input_keyboard(
    game_id: int, round_index: int, current: str = ""
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    current = "".join(ch for ch in str(current) if ch.isdigit())
    shown = current or "—"
    _button(b, f"Число: {shown}", callback_data="game:num:noop")
    for digit in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
        value = f"{current}{digit}" if current else digit
        _button(
            b,
            digit,
            callback_data=f"game:num:set:{game_id}:{round_index}:{value}",
        )
    back = current[:-1] or "x"
    _button(b, "Стереть", callback_data=f"game:num:set:{game_id}:{round_index}:{back}", icon="delete")
    zero = f"{current}0" if current else "0"
    _button(b, "0", callback_data=f"game:num:set:{game_id}:{round_index}:{zero}")
    _button(
        b,
        "Выбрать",
        callback_data=f"game:num:submit:{game_id}:{round_index}:{current or 'x'}",
        icon="check",
        style="success",
    )
    b.adjust(1, 3, 3, 3, 3)
    return b.as_markup()


def number_next_keyboard(game_id: int, round_index: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(
        b,
        "Следующий раунд",
        callback_data=f"game:num:next:{game_id}:{round_index}",
        icon="next",
        style="primary",
    )
    return b.as_markup()


def number_end_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Сыграть ещё", callback_data=CB_NUMBERS, icon="refresh", style="success")
    _button(b, "Вернуться в чат", callback_data="game:return", icon="home")
    b.adjust(1, 1)
    return b.as_markup()


def battle_invite_keyboard(game_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Играть", callback_data=f"game:yes:{game_id}", icon="check", style="success")
    _button(b, "Не сейчас", callback_data=f"game:no:{game_id}", icon="delete")
    b.adjust(2)
    return b.as_markup()


def battle_length_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "5 вопросов", callback_data="game:battle:5", style="primary")
    _button(b, "10 вопросов", callback_data="game:battle:10", style="success")
    _button(b, "Назад", callback_data=CB_GAMES, icon="home")
    b.adjust(2, 1)
    return b.as_markup()


def battle_answer_keyboard(
    game_id: int, question_index: int, first: str, second: str
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, first.capitalize(), callback_data=f"game:answer:{game_id}:{question_index}:0", style="primary")
    _button(b, second.capitalize(), callback_data=f"game:answer:{game_id}:{question_index}:1", style="success")
    b.adjust(1, 1)
    return b.as_markup()


def battle_next_keyboard(game_id: int, question_index: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Следующий вопрос", callback_data=f"game:next:{game_id}:{question_index}", icon="next", style="primary")
    return b.as_markup()


def battle_end_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Сыграть ещё", callback_data="game:again", icon="refresh", style="success")
    _button(b, "Вернуться в чат", callback_data="game:return", icon="home")
    b.adjust(1, 1)
    return b.as_markup()


#: (подпись, код причины, иконка)
REPORT_REASONS: tuple[tuple[str, str, str], ...] = (
    ("Оскорбления / травля", "insults", "warn"),
    ("18+ контент", "nsfw", "view"),
    ("Просит контакты / адрес", "contacts", "profile"),
    ("Мошенничество", "scam", "money"),
    ("Спам", "spam", "delete"),
    ("Другое", "other", "ticket"),
)

REASON_TITLES = {code: title for title, code, _ in REPORT_REASONS}


def report_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for title, value, icon in REPORT_REASONS:
        _button(b, title, callback_data=f"rep:{value}", icon=icon)
    _button(b, "Отмена", callback_data=CB_MENU, icon="check")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def rating_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "👍 Норм", callback_data="rate:1", icon="bonus", style="success")
    _button(b, "Не зашло", callback_data="rate:0", icon="warn")
    _button(b, "Найти ещё", callback_data=CB_CONNECT, icon="view")
    b.adjust(2, 1)
    return b.as_markup()


def confirm_stop_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Да, остановить", callback_data=CB_STOP_YES, icon="check", style="danger")
    _button(b, "Продолжить", callback_data=CB_STOP_NO, icon="next")
    b.adjust(2)
    return b.as_markup()


def confirm_forget_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Да, удалить всё", callback_data="cfg:forget:yes", icon="delete", style="danger")
    _button(b, "Отмена", callback_data="cfg:forget:no", icon="check")
    b.adjust(2)
    return b.as_markup()


def back_menu_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "В меню", callback_data=CB_MENU, icon="home")
    b.adjust(1)
    return b.as_markup()


def skip_cancel_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Пропустить", callback_data="rep:skip", icon="next")
    _button(b, "Отмена", callback_data=CB_MENU, icon="check")
    b.adjust(2)
    return b.as_markup()


def admin_report_keyboard(
    report_id: int, permissions: frozenset[str] | set[str] | None = None
) -> InlineKeyboardMarkup:
    permissions = set(permissions or {"reports", "users", "mute", "ban"})
    b = InlineKeyboardBuilder()
    if "mute" in permissions:
        _button(b, "Мут 60 минут", callback_data=f"adm:mute:{report_id}", icon="warn", style="primary")
    if "ban" in permissions:
        _button(b, "Забанить", callback_data=f"adm:ban:{report_id}", icon="delete", style="danger")
    if "users" in permissions:
        _button(b, "Профиль нарушителя", callback_data=f"adm:who:{report_id}", icon="profile")
    if "reports" in permissions:
        _button(b, "Закрыть без наказания", callback_data=f"adm:done:{report_id}", icon="check", style="success")
    b.adjust(2, 1, 1)
    return b.as_markup()


def plain_button(text: str, callback_data: str, icon: str = "", style: str = "") -> InlineKeyboardButton:
    """Одиночная кнопка для сборных клавиатур (используется хендлерами)."""
    kwargs: dict[str, object] = {"text": text, "callback_data": callback_data}
    emoji_id = ICONS.get(icon)
    if emoji_id:
        kwargs["icon_custom_emoji_id"] = emoji_id
    if style in STYLES:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------- панель модератора
CB_PANEL = "adm:panel"
CB_PANEL_STATS = "adm:panel:stats"
CB_PANEL_REPORTS = "adm:panel:reports"
CB_PANEL_QUEUE = "adm:panel:queue"
CB_PANEL_FIND = "adm:panel:find"
CB_PANEL_BC = "adm:panel:broadcast"
CB_PANEL_MUTE = "adm:panel:mute"
CB_PANEL_MUTE_LIST = "adm:panel:mute_list"
CB_PANEL_BAN = "adm:panel:ban"
CB_PANEL_UNBAN = "adm:panel:unban"
CB_PANEL_BAN_LIST = "adm:panel:ban_list"
CB_PANEL_USERS = "adm:panel:users"
CB_PANEL_POINTS = "adm:panel:points"
CB_PANEL_ADMINS = "adm:panel:admins"
CB_PANEL_MONITOR = "adm:panel:monitor"
CB_PANEL_GAMES = "adm:panel:games"
CB_PANEL_BACKUP = "adm:panel:backup"
CB_PANEL_DIAGNOSTICS = "adm:panel:diagnostics"
CB_PANEL_MULTIPLIER = "adm:panel:xp"
CB_PANEL_POLL = "adm:panel:poll"
CB_PANEL_BACK = "adm:panel:back"


def admin_panel_keyboard(
    open_reports: int = 0,
    permissions: frozenset[str] | set[str] | None = None,
    owner: bool = False,
    monitor_enabled: bool = False,
    xp_multiplier: int = 1,
    poll_active: bool = False,
) -> InlineKeyboardMarkup:
    """Панель модератора: никаких команд в счёт, всё кнопками."""
    b = InlineKeyboardBuilder()
    permissions = set(permissions or ())
    if "stats" in permissions:
        _button(b, "Сводка", callback_data=CB_PANEL_STATS, icon="stats")
        _button(b, "Диагностика", callback_data=CB_PANEL_DIAGNOSTICS, icon="settings", style="primary")
    if "reports" in permissions:
        _button(
            b,
            f"Жалобы · {open_reports}" if open_reports else "Жалобы",
            callback_data=CB_PANEL_REPORTS,
            icon="warn",
            style="danger" if open_reports else "",
        )
    if "queue" in permissions:
        _button(b, "Очередь", callback_data=CB_PANEL_QUEUE, icon="refresh")
    if "users" in permissions:
        _button(b, "Найти профиль", callback_data=CB_PANEL_FIND, icon="view")
        _button(b, "Все пользователи", callback_data=CB_PANEL_USERS, icon="profile")
    if "broadcast" in permissions:
        _button(b, "Рассылка", callback_data=CB_PANEL_BC, icon="support")
    if "mute" in permissions:
        _button(b, "Мут по id", callback_data=CB_PANEL_MUTE, icon="settings", style="primary")
        _button(b, "Мут-лист", callback_data=CB_PANEL_MUTE_LIST, icon="warn")
    if "ban" in permissions:
        _button(b, "Бан по id", callback_data=CB_PANEL_BAN, icon="delete", style="danger")
        _button(b, "Разбан по id", callback_data=CB_PANEL_UNBAN, icon="check")
        _button(b, "Бан-лист", callback_data=CB_PANEL_BAN_LIST, icon="delete")
    if "points" in permissions:
        _button(b, "Выдать / снять очки", callback_data=CB_PANEL_POINTS, icon="stars")
    if owner:
        _button(
            b, f"Множитель: x{int(xp_multiplier)}",
            callback_data=CB_PANEL_MULTIPLIER, icon="stars",
            style="success" if int(xp_multiplier) > 1 else "primary",
        )
        _button(
            b, f"Опрос дня{' · ВКЛ' if poll_active else ''}",
            callback_data=CB_PANEL_POLL, icon="ticket",
            style="success" if poll_active else "primary",
        )
        _button(b, "Администраторы", callback_data=CB_PANEL_ADMINS, icon="bonus", style="primary")
        _button(b, "Скачать базу", callback_data=CB_PANEL_BACKUP, icon="link", style="primary")
    if owner or "monitor" in permissions:
        _button(b, "Игры пользователей", callback_data=CB_PANEL_GAMES, icon="bonus", style="primary")
        _button(
            b,
            f"Чаты: {'ВКЛ' if monitor_enabled else 'ВЫКЛ'}",
            callback_data=CB_PANEL_MONITOR,
            icon="view",
            style="success" if monitor_enabled else "",
        )
    _button(b, "В меню", callback_data=CB_MENU, icon="home")
    b.adjust(2)
    return b.as_markup()


def xp_multiplier_keyboard(current: int = 1) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for value in (1, 2, 3):
        _button(
            b, f"• x{value}" if value == int(current) else f"x{value}",
            callback_data=f"adm:panel:xp:{value}",
            icon="stars",
            style="success" if value == int(current) else "",
        )
    _button(b, "В панель", callback_data=CB_PANEL_BACK, icon="home")
    b.adjust(3, 1)
    return b.as_markup()


def admin_poll_keyboard(active: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Создать новый", callback_data="adm:panel:poll:create", icon="add", style="success")
    if active:
        _button(b, "Кто голосовал", callback_data="adm:panel:poll:voters", icon="view")
        _button(b, "Закрыть опрос", callback_data="adm:panel:poll:close", icon="delete", style="danger")
    _button(b, "В панель", callback_data=CB_PANEL_BACK, icon="home")
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def poll_keyboard(
    poll_id: int, option_a: str, option_b: str, pct_a: int, pct_b: int,
    selected: int | None = None,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(
        b, f"{option_a[:48]} · {pct_a}%",
        callback_data=f"poll:vote:{int(poll_id)}:0",
        style="success" if selected == 0 else "primary",
    )
    _button(
        b, f"{option_b[:48]} · {pct_b}%",
        callback_data=f"poll:vote:{int(poll_id)}:1",
        style="success" if selected == 1 else "primary",
    )
    _button(b, "Назад", callback_data=CB_MENU, icon="home")
    b.adjust(1, 1, 1)
    return b.as_markup()


def diagnostics_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Обновить", callback_data=CB_PANEL_DIAGNOSTICS, icon="refresh", style="primary")
    _button(b, "В панель", callback_data=CB_PANEL_BACK, icon="home")
    b.adjust(1, 1)
    return b.as_markup()


def panel_back_keyboard() -> InlineKeyboardMarkup:
    """Из любого экрана панели — назад в панель и в меню."""
    b = InlineKeyboardBuilder()
    _button(b, "Назад в панель", callback_data=CB_PANEL_BACK, icon="next")
    _button(b, "В меню", callback_data=CB_MENU, icon="home")
    b.adjust(2)
    return b.as_markup()


def panel_cancel_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Отмена", callback_data=CB_PANEL_BACK, icon="check")
    b.adjust(1)
    return b.as_markup()


def users_page_keyboard(offset: int, count: int, page_size: int = 30) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if offset > 0:
        _button(b, "Назад", callback_data=f"adm:users:{max(0, offset - page_size)}", icon="next")
    if count == page_size:
        _button(b, "Дальше", callback_data=f"adm:users:{offset + page_size}", icon="next")
    _button(b, "В панель", callback_data=CB_PANEL_BACK, icon="home")
    b.adjust(2, 1)
    return b.as_markup()


def restricted_list_keyboard(
    kind: str, user_ids: list[int], offset: int, total: int, page_size: int = 10
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    action = "unban" if kind == "ban" else "unmute"
    label = "Разбанить" if kind == "ban" else "Снять мут"
    for user_id in user_ids:
        _button(
            b, f"{label} {user_id}",
            callback_data=f"adm:restrict:{action}:{user_id}:{offset}",
            icon="check", style="success",
        )
    if offset > 0:
        _button(
            b, "Назад", callback_data=f"adm:restrict:list:{kind}:{max(0, offset - page_size)}",
            icon="next",
        )
    if offset + page_size < total:
        _button(
            b, "Дальше", callback_data=f"adm:restrict:list:{kind}:{offset + page_size}",
            icon="next",
        )
    _button(b, "В панель", callback_data=CB_PANEL_BACK, icon="home")
    b.adjust(*([1] * len(user_ids)), 2, 1)
    return b.as_markup()


def purge_referrals_keyboard(user_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(
        b,
        "Удалить накрутку",
        callback_data=f"adm:purge_refs:{int(user_id)}",
        icon="delete",
        style="danger",
    )
    _button(b, "Отмена", callback_data=CB_PANEL_BACK, icon="check")
    b.adjust(1)
    return b.as_markup()


def game_watch_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _button(b, "Обновить", callback_data="adm:games:active", icon="refresh", style="primary")
    _button(b, "В панель", callback_data=CB_PANEL_BACK, icon="home")
    b.adjust(1, 1)
    return b.as_markup()
