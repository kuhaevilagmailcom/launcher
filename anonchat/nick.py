"""Публичные ники и постоянная отметка поддержки проекта."""

from __future__ import annotations

import hashlib

from .safety import contains_contact

NICK_MIN = 2
NICK_MAX = 24
_ALLOWED_EXTRA = set("-_.!?()[]*+~:;='\" ")
_FORBIDDEN = set("<>`\\/@\t\n\r✦✧★☆")


def normalize(raw: str) -> str:
    return " ".join((raw or "").split()).strip()[: NICK_MAX + 8]


def validate(raw: str) -> tuple[str, str | None]:
    """Возвращает (ник, ошибка). Пустая строка — «хочу сбросить на авто-ник»."""
    nick = normalize(raw)
    if not nick:
        return "", None
    if len(nick) < NICK_MIN:
        return "", f"Коротко: минимум {NICK_MIN} символа."
    if len(nick) > NICK_MAX:
        return "", f"Длинно: максимум {NICK_MAX} символов (сейчас {len(nick)})."
    if contains_contact(nick):
        return "", "Не добавляй контакты, ссылки, телефон или почту."
    for ch in nick:
        if ch in _FORBIDDEN:
            return "", "Такие символы в нике запрещены: < > ` \\ / @ и перенос строки."
        if ch.isspace() or ch in _ALLOWED_EXTRA:
            continue
        if not (ch.isalpha() or ch.isdigit()):
            return "", "Только буквы, цифры, пробел и обычные знаки препинания."
    return nick, None


def auto_nick(user_id: int) -> str:
    """Стабильный случайно выглядящий номер, который не раскрывает часть Telegram ID."""
    digest = hashlib.blake2s(f"anonchat-mgn:{int(user_id)}".encode(), digest_size=4).digest()
    number = 1000 + int.from_bytes(digest, "big") % 9000
    return f"Аноним-{number:04d}"


def is_supporter(support_stars: int) -> bool:
    return int(support_stars or 0) > 0


def display(
    row_nickname: str | None,
    user_id: int,
    support_stars: int = 0,
) -> str:
    nick = normalize(row_nickname or "")
    value = nick or auto_nick(user_id)
    if is_supporter(support_stars):
        return f"{value} 💎"
    return value
