"""Вопросы для «Битвы мнений» из текстовых наборов проекта."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


QUESTION_DIR = Path(__file__).resolve().parents[1] / "assets" / "battle_questions"


@dataclass(frozen=True, slots=True)
class BattleQuestion:
    id: int
    text: str
    first: str
    second: str
    category: str

    def option(self, choice: int) -> str:
        return self.first if choice == 0 else self.second


@lru_cache(maxsize=1)
def questions() -> dict[int, BattleQuestion]:
    result: dict[int, BattleQuestion] = {}
    for path in sorted(QUESTION_DIR.glob("*.txt")):
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            parts = [part.strip() for part in value.split("|")]
            if len(parts) != 5 or not parts[0].isdigit():
                raise RuntimeError(f"Неверный вопрос: {path.name}: {line}")
            question = BattleQuestion(int(parts[0]), parts[1], parts[2], parts[3], parts[4])
            if question.id in result:
                raise RuntimeError(f"Повтор ID вопроса: {question.id}")
            result[question.id] = question
    if len(result) < 5:
        raise RuntimeError("Для игры нужно минимум 5 вопросов")
    return result


def get_question(question_id: int) -> BattleQuestion:
    try:
        return questions()[int(question_id)]
    except KeyError as exc:
        raise RuntimeError(f"Вопрос {question_id} не найден") from exc
