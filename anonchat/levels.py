"""Ранги собеседника — по количеству написанных сообщений.

После «Легенды» прогресс продолжается: Ветеран → Элита → Титан.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Пороги намеренно достижимые: уровень отражает нормальное общение, а не спам.
RANKS: tuple[tuple[int, str, str], ...] = (
    (0, "🌱", "Новичок"),
    (25, "💬", "Общительный"),
    (100, "🤝", "Знакомый"),
    (350, "⭐", "Свой"),
    (1_000, "🏆", "Легенда"),
    (2_500, "🔥", "Ветеран"),
    (5_000, "💎", "Элита"),
    (10_000, "👑", "Титан"),
)


@dataclass(slots=True)
class RankInfo:
    index: int
    emoji: str
    title: str
    messages: int
    floor: int
    next_need: int | None
    next_title: str | None
    progress: float  # 0..1 внутри текущего ранга

    @property
    def name(self) -> str:
        return f"{self.emoji} {self.title}"

    @property
    def bar(self, width: int = 8) -> str:
        filled = max(0, min(width, round(self.progress * width)))
        return "▰" * filled + "▱" * (width - filled)

    @property
    def to_next(self) -> int | None:
        if self.next_need is None:
            return None
        return max(0, self.next_need - self.messages)

    @property
    def is_max(self) -> bool:
        return self.next_need is None

    def pretty(self, value: int) -> str:
        """1000 -> «1 000» — в тексте цифры с пробелами читаются лучше."""
        return f"{int(value):,}".replace(",", " ")

    @property
    def label(self) -> str:
        """Строка ранга для служебных карточек."""
        if self.is_max:
            return f"{self.name} · {self.pretty(self.messages)} сообщений"
        return (
            f"{self.name} · {self.pretty(self.messages)}/{self.pretty(self.next_need or 0)}"
        )


def rank_for(messages: int) -> RankInfo:
    count = max(0, int(messages or 0))
    idx = 0
    for i, (need, _, _) in enumerate(RANKS):
        if count >= need:
            idx = i
        else:
            break
    floor, emoji, title = RANKS[idx]
    if idx + 1 < len(RANKS):
        nxt_need, _, nxt_title = RANKS[idx + 1]
        span = max(1, nxt_need - floor)
        progress = (count - floor) / span
        return RankInfo(idx + 1, emoji, title, count, floor, nxt_need, nxt_title,
                        min(1.0, max(0.0, progress)))
    return RankInfo(idx + 1, emoji, title, count, floor, None, None, 1.0)
