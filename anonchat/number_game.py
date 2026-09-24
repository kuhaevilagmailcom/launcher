"""Правила игры «Числа»."""

NUMBER_ROUNDS = 3
NUMBER_DAILY_REWARD_LIMIT = 300
NUMBER_REWARDS: dict[int, int] = {
    10: 25,
    100: 50,
    1000: 100,
}

NUMBER_NEAR_DIFFS: dict[int, int] = {
    10: 1,
    100: 2,
    1000: 5,
}


def number_reward(range_max: int, first: int, second: int) -> int:
    """Награда каждому игроку за один раунд."""
    base = NUMBER_REWARDS.get(int(range_max), 0)
    if base <= 0:
        return 0
    diff = abs(int(first) - int(second))
    if diff == 0:
        return base
    if 1 <= diff <= NUMBER_NEAR_DIFFS.get(int(range_max), 0):
        return base // 2
    return 0
