"""Оперативное сопоставление оригинальных сообщений и их анонимных копий."""
from __future__ import annotations

_FORWARD: dict[tuple[int, int], tuple[int, int]] = {}
_REVERSE: dict[tuple[int, int], tuple[int, int]] = {}
_LIMIT = 10_000

def remember(sender_id: int, source_message_id: int, partner_id: int, copied_message_id: int) -> None:
    source_key = (int(sender_id), int(source_message_id))
    copy_key = (int(partner_id), int(copied_message_id))
    _FORWARD[source_key] = copy_key
    _REVERSE[copy_key] = source_key
    if len(_FORWARD) > _LIMIT:
        for old_key in list(_FORWARD)[:2_000]:
            target = _FORWARD.pop(old_key, None)
            if target is not None:
                _REVERSE.pop(target, None)

def forwarded_target(sender_id: int, source_message_id: int) -> tuple[int, int] | None:
    return _FORWARD.get((int(sender_id), int(source_message_id)))

def resolve_reply(user_id: int, partner_id: int, replied_message_id: int) -> int | None:
    """Возвращает message_id, на который должна отвечать копия у partner_id."""
    uid, partner, mid = int(user_id), int(partner_id), int(replied_message_id)

    # Пользователь отвечает на полученную от бота копию сообщения собеседника.
    original = _REVERSE.get((uid, mid))
    if original is not None and original[0] == partner:
        return int(original[1])

    # Пользователь отвечает на собственное старое сообщение.
    copied = _FORWARD.get((uid, mid))
    if copied is not None and copied[0] == partner:
        return int(copied[1])
    return None

def forget_source(user_id: int, source_message_id: int) -> None:
    key = (int(user_id), int(source_message_id))
    target = _FORWARD.pop(key, None)
    if target is not None:
        _REVERSE.pop(target, None)

def clear_pair(user_a: int, user_b: int) -> None:
    users = {int(user_a), int(user_b)}
    for key, target in list(_FORWARD.items()):
        if key[0] in users or target[0] in users:
            _FORWARD.pop(key, None)
            _REVERSE.pop(target, None)

def clear_user(user_id: int) -> None:
    uid = int(user_id)
    for key, target in list(_FORWARD.items()):
        if key[0] == uid or target[0] == uid:
            _FORWARD.pop(key, None)
            _REVERSE.pop(target, None)

def size() -> int:
    return len(_FORWARD)
