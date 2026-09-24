"""Активность, серии, достижения и ежедневные квесты.

Здесь нет текстов переписки: только компактные числовые агрегаты.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .db import referral_day_start

@dataclass(frozen=True, slots=True)
class Quest:
    key: str
    title: str
    field: str
    target: int
    reward: int

QUEST_POOL = (
    Quest("msg20", "Отправить 20 сообщений", "messages", 20, 25),
    Quest("msg50", "Отправить 50 сообщений", "messages", 50, 50),
    Quest("dialogs3", "Провести 3 диалога", "dialogs", 3, 25),
    Quest("dialogs5", "Провести 5 диалогов", "dialogs", 5, 50),
    Quest("ratings2", "Поставить 2 оценки", "ratings_given", 2, 25),
    Quest("good1", "Получить хорошую оценку", "good_ratings", 1, 25),
    Quest("game1", "Сыграть одну игру", "games", 1, 25),
    Quest("game2", "Сыграть 2 игры", "games", 2, 50),
    Quest("battle1", "Сыграть в Битву мнений", "battle_games", 1, 25),
    Quest("numbers1", "Сыграть в Числа", "number_games", 1, 25),
)

ACHIEVEMENTS = (
    ("dialog_1", "Первый диалог", "dialogs", 1, 25),
    ("dialog_10", "10 диалогов", "dialogs", 10, 25),
    ("dialog_50", "50 диалогов", "dialogs", 50, 50),
    ("dialog_100", "100 диалогов", "dialogs", 100, 100),
    ("msg_100", "100 сообщений", "messages", 100, 25),
    ("msg_500", "500 сообщений", "messages", 500, 50),
    ("msg_1000", "1000 сообщений", "messages", 1000, 100),
    ("good_1", "Первая хорошая оценка", "good_ratings", 1, 25),
    ("good_10", "10 хороших оценок", "good_ratings", 10, 50),
    ("good_50", "50 хороших оценок", "good_ratings", 50, 100),
    ("game_1", "Первая игра", "games_total", 1, 25),
    ("game_10", "10 игр", "games_total", 10, 50),
    ("game_50", "50 игр", "games_total", 50, 100),
    ("battle_5", "Идеальная Битва мнений 5/5", "battle_perfect_5", 1, 50),
    ("battle_10", "Идеальная Битва мнений 10/10", "battle_perfect_10", 1, 100),
    ("number_1000", "Точное совпадение в Числах 1–1000", "number_exact_1000", 1, 100),
    ("streak_3", "Серия 3 дня", "current_streak", 3, 25),
    ("streak_7", "Серия 7 дней", "current_streak", 7, 50),
    ("streak_14", "Серия 14 дней", "current_streak", 14, 75),
    ("streak_30", "Серия 30 дней", "current_streak", 30, 100),
)

def quests_for(user_id: int, day_start: int | None = None) -> tuple[Quest, ...]:
    day = referral_day_start() if day_start is None else int(day_start)
    rng = random.Random((int(user_id) * 1_000_003) ^ day)
    return tuple(rng.sample(list(QUEST_POOL), 3))

def progress_value(activity: dict[str, int], quest: Quest) -> int:
    return max(0, int(activity.get(quest.field, 0)))

async def collect_progress_notifications(db, user_id: int) -> list[str]:
    """Выдаёт только новые достижения/квесты и возвращает короткие уведомления."""
    user = await db.get_user(user_id)
    if user is None:
        return []
    engagement = await db.engagement_state(user_id)
    totals = {
        "dialogs": int(engagement["dialogs_total"] or 0),
        "messages": int(user["messages"] or 0),
        "good_ratings": int(user["good_ratings"] or 0),
        "games_total": int(engagement["games_total"] or 0),
        "battle_perfect_5": int(engagement["battle_perfect_5"] or 0),
        "battle_perfect_10": int(engagement["battle_perfect_10"] or 0),
        "number_exact_1000": int(engagement["number_exact_1000"] or 0),
        "current_streak": int(engagement["current_streak"] or 0),
    }
    notices: list[str] = []
    for key, title, field, target, reward in ACHIEVEMENTS:
        if totals.get(field, 0) < target:
            continue
        if await db.unlock_achievement(user_id, key, reward):
            notices.append(f"🏆 <b>Достижение выполнено</b>\n{title}\n+<b>{reward} ⭐</b>")

    today = referral_day_start()
    activity = await db.activity_totals(user_id, 1)
    for quest in quests_for(user_id, today):
        if progress_value(activity, quest) < quest.target:
            continue
        if await db.claim_daily_quest(user_id, today, quest.key, quest.reward):
            notices.append(f"✅ <b>Квест выполнен</b>\n{quest.title}\n+<b>{quest.reward} ⭐</b>")
    return notices

async def format_quests(db, user_id: int) -> str:
    day = referral_day_start()
    activity = await db.activity_totals(user_id, 1)
    claimed = await db.daily_quest_claimed(user_id, day)
    lines = ["📋 <b>Квесты дня</b>", ""]
    for quest in quests_for(user_id, day):
        current = min(progress_value(activity, quest), quest.target)
        done = quest.key in claimed
        mark = "✅" if done else "▫️"
        lines.append(
            f"{mark} {quest.title} · <b>{current}/{quest.target}</b> · {quest.reward} ⭐"
        )
    return "\n".join(lines)
