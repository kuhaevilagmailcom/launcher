"""Права администраторов. Владельцы из ADMIN_IDS всегда имеют полный доступ."""

from __future__ import annotations

ALL_ADMIN_PERMISSIONS = frozenset(
    {
        "stats",
        "reports",
        "queue",
        "users",
        "broadcast",
        "mute",
        "ban",
        "points",
        "monitor",
    }
)

PERMISSION_LABELS = {
    "stats": "статистика",
    "reports": "жалобы",
    "queue": "очередь",
    "users": "пользователи и username",
    "broadcast": "рассылка",
    "mute": "мут",
    "ban": "бан и разбан",
    "points": "выдача и снятие очков",
    "monitor": "наблюдение за чатами",
}


def parse_permissions(raw: str) -> frozenset[str]:
    value = (raw or "").strip().lower()
    if value in {"all", "все", "*"}:
        return ALL_ADMIN_PERMISSIONS
    aliases = {
        "жалобы": "reports",
        "очередь": "queue",
        "юзеры": "users",
        "пользователи": "users",
        "рассылка": "broadcast",
        "бан": "ban",
        "мут": "mute",
        "очки": "points",
        "статистика": "stats",
    }
    chunks = value.replace(";", ",").replace(" ", ",").split(",")
    return frozenset(
        aliases.get(chunk, chunk)
        for chunk in (item.strip() for item in chunks)
        if chunk and aliases.get(chunk, chunk) in ALL_ADMIN_PERMISSIONS
    )


def serialize_permissions(permissions: set[str] | frozenset[str]) -> str:
    return ",".join(sorted(set(permissions) & ALL_ADMIN_PERMISSIONS))
