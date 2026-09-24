"""Самопроверка ядра: уровни, матчмейкер, база. Запуск: pytest -q (или python -m tests.core)."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anonchat.db import Database, number_reward_day_start, referral_day_start  # noqa: E402
from anonchat.levels import RANKS, rank_for  # noqa: E402
from anonchat.matching import Matchmaker  # noqa: E402


# --------------------------------------------------------------------------------- levels
def test_config_defaults(monkeypatch=None) -> None:
    """from_env должен читать дефолты полей, а не slots-дескрипторы (был реальный баг)."""
    import os

    from anonchat.config import Config

    saved = {
        k: os.environ.get(k)
        for k in (
            "BOT_TOKEN", "CITY_NAME", "ADMIN_IDS", "TELEGRAM_ADMIN_ID",
            "AUTO_MUTE_REPORTS", "XP_GOOD_RATING", "RECENT_PARTNER_COOLDOWN_MINUTES",
        )
    }
    os.environ["BOT_TOKEN"] = "12:TEST"
    os.environ.pop("CITY_NAME", None)
    os.environ.pop("TELEGRAM_ADMIN_ID", None)
    os.environ["ADMIN_IDS"] = "777, 888"
    try:
        cfg = Config.from_env(dotenv=".__no_such_env__.local")
        assert cfg.city == "Магнитогорск" and cfg.city_short == "МГН"
        assert cfg.admin_ids == (777, 888)
        assert cfg.auto_mute_reports == 3 and isinstance(cfg.auto_mute_reports, int)
        assert cfg.drop_pending_updates is False
        assert cfg.report_context_retention_days == 7
        assert cfg.emoji_pack_url.startswith("https://t.me/addemoji/")
        assert cfg.max_message_len == 3000
        assert cfg.xp_good_rating == 10
        assert cfg.recent_partner_cooldown_minutes == 30
        assert cfg.subscription_channel == "@anonmgn"
        assert cfg.subscription_reward == 100
        assert cfg.miniapp_enabled is True
        assert cfg.miniapp_url == ""

        # id администраторов не зашиваются в публичный код
        os.environ["ADMIN_IDS"] = ""
        assert Config.from_env(dotenv=".__no_such_env__.local").admin_ids == ()
        # алиас от панелей хостинга
        os.environ.pop("ADMIN_IDS")
        os.environ["TELEGRAM_ADMIN_ID"] = "4242"
        assert Config.from_env(dotenv=".__no_such_env__.local").admin_ids == (4242,)
        # явный отказ от админов (форк под своего владельца)
        os.environ["ADMIN_IDS"] = "none"
        assert Config.from_env(dotenv=".__no_such_env__.local").admin_ids == ()
        os.environ["ADMIN_IDS"] = "777, 888"

        os.environ["CITY_NAME"] = "Челябинск"
        os.environ["AUTO_MUTE_REPORTS"] = "5"
        cfg2 = Config.from_env(dotenv=".__no_such_env__.local")
        assert cfg2.city == "Челябинск" and cfg2.auto_mute_reports == 5

        os.environ["BOT_TOKEN"] = ""
        try:
            Config.from_env(dotenv=".__no_such_env__.local")
        except RuntimeError:
            pass
        else:
            raise AssertionError("пустой BOT_TOKEN обязан ронять запуск")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_levels_progress() -> None:
    """Уровни достижимы без сотен тысяч сообщений."""
    first = rank_for(0)
    assert first.index == 1 and first.title == "Новичок"
    assert first.progress == 0.0 and first.to_next == 25 and first.next_title == "Общительный"

    bronze = rank_for(25)
    assert bronze.title == "Общительный" and bronze.progress == 0.0
    assert bronze.to_next == 75 and bronze.next_title == "Знакомый"

    mid = rank_for(60)
    assert mid.title == "Общительный" and 0.4 < mid.progress < 0.6
    assert len(mid.bar) == 8 and mid.bar.startswith("▰▰▰▰▱")

    silver = rank_for(100)
    assert silver.title == "Знакомый"
    gold = rank_for(350)
    assert gold.title == "Свой"
    vip = rank_for(1_000)
    assert vip.title == "Легенда" and not vip.is_max
    assert vip.to_next == 1_500 and vip.next_title == "Ветеран"
    assert rank_for(2_500).title == "Ветеран"
    assert rank_for(5_000).title == "Элита"
    titan = rank_for(10_000)
    assert titan.title == "Титан" and titan.is_max and titan.to_next is None
    assert rank_for(99_999_999).index == len(RANKS)

    # человекочитаемые числа с неразрывными пробелами
    assert rank_for(15_000).pretty(12345) == "12 345"
    assert "1 000" in rank_for(1_000).label
    assert rank_for(0).name.endswith("Новичок")


# --------------------------------------------------------------------------------- matching
def test_queue_then_pair() -> None:
    mm = Matchmaker()
    assert mm.connect(1) == ("queued", 1)
    assert mm.status(1) == "queued"
    assert mm.connect(2) == ("paired", 1)
    assert mm.status(1) == "paired" and mm.partner(1) == 2
    assert mm.queue_size() == 0

    # счётчик сообщений + корректный разбор пары
    assert mm.count_message(1) == (2, 1)
    mm.count_message(2)
    partner, summary = mm.release(1)
    assert partner == 2 and summary["counts"][1] == 1
    assert mm.status(1) == "free" and mm.status(2) == "free"


def test_matchmaker_snapshot_restore() -> None:
    mm = Matchmaker()
    mm.connect(1)
    mm.connect(2)
    mm.count_message(1)
    mm.record_text(1, "секретный текст не должен попасть в SQLite")
    snapshot = mm.snapshot()
    assert snapshot["pairs"] and "history" not in snapshot["pairs"][0]
    restored = Matchmaker()
    restored.restore(snapshot)
    assert restored.partner(1) == 2
    assert restored.dialog_stats(1)["counts"] == {1: 1}
    assert restored.dialog_stats(1)["history"] == []

    queued = Matchmaker()
    queued.connect(3, district="Левобережный", same_district=True)
    restored.restore(queued.snapshot())
    assert restored.status(3) == "queued"


def test_district_priority_and_fallback() -> None:
    # Если своего берега нет — другой берег подходит сразу, без вечного ожидания.
    mm = Matchmaker()
    assert mm.connect(1, district="Правый берег") == ("queued", 1)
    assert mm.connect(2, district="Левый берег") == ("paired", 1)

    # Если есть выбор, свой берег приоритетнее даже если человек с другого берега ждёт дольше.
    # Первые двое взаимно исключены, чтобы оба успели оказаться в очереди для проверки выбора.
    mm2 = Matchmaker()
    assert mm2.connect(
        10, district="Левый берег", excluded={11}
    ) == ("queued", 1)
    assert mm2.connect(
        11, district="Правый берег", excluded={10}
    ) == ("queued", 2)
    assert mm2.connect(
        12, district="Правый берег"
    ) == ("paired", 11)
    assert mm2.status(10) == "queued"


def test_forget_and_ratings() -> None:
    mm = Matchmaker()
    mm.connect(1)
    mm.connect(2)
    summary = mm.forget(2)
    assert summary.get("partner") == 1
    assert mm.status(1) == "free"

    mm.remember_rating([1, 2], match_id=42)
    assert mm.pop_rating(1) == (42, 2)
    assert mm.pop_rating(1) is None  # повторно уже не сработает


def test_queue_limit() -> None:
    mm = Matchmaker(queue_limit=2)
    # Только блокировки остаются жёстким ограничением.
    assert mm.connect(1, excluded={2}) == ("queued", 1)
    assert mm.connect(2, excluded={1}) == ("queued", 2)
    assert mm.connect(3) == ("full", None)


def test_excluded_users_do_not_match() -> None:
    mm = Matchmaker()
    assert mm.connect(1, excluded={2}) == ("queued", 1)
    assert mm.connect(2) == ("queued", 2)
    assert mm.connect(3) == ("paired", 1)


def test_gender_filter() -> None:
    # Если выбранного пола нет, бот не держит людей в очереди бесконечно — матчится любой.
    fallback = Matchmaker()
    assert fallback.connect(1, gender="m", looking_for="f") == ("queued", 1)
    assert fallback.connect(2, gender="m", looking_for="f") == ("paired", 1)

    # Если есть выбор, желаемый пол важнее берега.
    mm = Matchmaker()
    assert mm.connect(
        10, district="Левый берег", gender="f", looking_for="m", excluded={11}
    ) == ("queued", 1)
    assert mm.connect(
        11, district="Правый берег", gender="m", looking_for="", excluded={10}
    ) == ("queued", 2)
    assert mm.connect(
        12, district="Правый берег", gender="m", looking_for="f"
    ) == ("paired", 10)
    assert mm.status(11) == "queued"

    restored = Matchmaker()
    restored.restore(mm.snapshot())
    assert restored.status(11) == "queued"


def test_restored_queue_is_permanent_and_sweeps() -> None:
    mm = Matchmaker()
    mm.restore({
        "queue": [
            {
                "user_id": 10, "district": "Правый берег",
                "gender": "m", "looking_for": "m", "excluded": [],
                "joined_at": 100.0,
            },
            {
                "user_id": 11, "district": "Правый берег",
                "gender": "f", "looking_for": "m", "excluded": [],
                "joined_at": 900.0,
            },
            {
                "user_id": 12, "district": "",
                "gender": "m", "looking_for": "f", "excluded": [],
                "joined_at": 910.0,
            },
        ],
        "pairs": [],
        "pending_rating": {},
    })
    # Старый кандидат остаётся в очереди бессрочно, пока сам не остановит поиск.
    assert mm.status(10) == "queued"
    assert mm.queue_size() == 3

    debug = mm.queue_debug_snapshot()
    assert debug[0]["user_id"] == 10
    assert debug[1]["gender"] == "f" and debug[1]["looking_for"] == "m"

    pairs = mm.sweep()
    assert pairs == [(11, 12)]
    assert mm.partner(11) == 12
    assert mm.status(10) == "queued" and mm.queue_size() == 1


# --------------------------------------------------------------------------------- database
def test_database() -> None:
    holder: dict[str, object] = {}

    async def scenario() -> None:
        path = Path(tempfile.mkdtemp()) / "t.db"
        db = await Database(path).start()
        holder["db"] = db

        row = await db.ensure_user(10, "petr", "Пётр")
        assert row["xp"] == 0 and row["district"] == ""

        await db.set_profile(10, district="Правобережный")
        row = await db.get_user(10)
        assert row["district"] == "Правобережный"

        assert await db.award_xp(10, 70) == 70
        assert rank_for(70).title == "Общительный"
        assert rank_for(1_000).title == "Легенда"

        await db.ensure_user(11, "friend", "Друг")
        assert await db.award_referral(11, 10, 50) is True
        assert await db.referral_stats(10) == (1, 50)
        assert (await db.get_user(10))["xp"] == 120
        assert await db.award_referral(11, 10, 50) is False
        assert await db.award_referral(21, 21, 50) is False

        assert await db.reward_claimed(10, "channel_subscription_v1") is False
        before_reward = int((await db.get_user(10))["xp"])
        assert await db.claim_one_time_reward(10, "channel_subscription_v1", 100) is True
        after_reward = int((await db.get_user(10))["xp"])
        assert after_reward == before_reward + 100
        assert await db.reward_claimed(10, "channel_subscription_v1") is True
        assert await db.claim_one_time_reward(10, "channel_subscription_v1", 100) is False
        assert int((await db.get_user(10))["xp"]) == after_reward

        created, support_total = await db.record_payment(
            10, "support", 1, "charge-1", "", "support:10:1:x"
        )
        assert created and support_total == 1
        duplicate, duplicate_total = await db.record_payment(
            10, "support", 1, "charge-1", "", "support:10:1:x"
        )
        assert duplicate is False and duplicate_total == support_total
        created2, extended = await db.record_payment(
            10, "support", 10, "charge-2", "", "support:10:10:y"
        )
        assert created2 and extended == 11

        perms = await db.set_admin(11, {"reports", "mute"}, 10)
        assert perms == {"reports", "mute"}
        assert await db.get_admin_permissions(11) == perms
        assert "ban" not in await db.get_admin_permissions(11)
        assert await db.get_admin_permissions(10, (10,))
        assert await db.remove_admin(11) is True
        assert not await db.get_admin_permissions(11)
        assert await db.adjust_xp(11, 50) == 50
        assert await db.adjust_xp(11, -80) == 0

        await db.ensure_user(11, None, "Аня")
        rid, day_count = await db.add_report(10, 11, "spam", "реклама казино", "10:11:1")
        assert rid >= 1 and day_count == 1
        duplicate, _ = await db.add_report(10, 11, "spam", "ещё", "10:11:1")
        assert duplicate is None
        rid2, unique_count = await db.add_report(10, 11, "spam", "новый диалог", "10:11:2")
        assert rid2 is not None and unique_count == 1, "один человек не накручивает авто-мут"
        await db.ensure_user(12, None, "Катя")
        excluded = await db.excluded_partners(10, recent_seconds=0)
        assert excluded == set(), "вечных скрытых собеседников больше нет"
        rid3, unique_count = await db.add_report(12, 11, "spam", "независимая", "11:12:1")
        assert rid3 is not None and unique_count == 2
        reports = await db.list_reports("new")
        assert reports and reports[0]["target_id"] == 11

        until = await db.set_mute(11, 30)
        assert until > 0 and await db.is_restricted(11) == "muted"
        muted, muted_total = await db.list_restricted("mute")
        assert muted_total == 1 and int(muted[0]["user_id"]) == 11
        assert await db.resolve_report(rid, 10) is True
        assert await db.resolve_report(rid2, 10) is True
        assert await db.resolve_report(rid3, 10) is True
        assert await db.list_reports("new") == []
        await db.db.execute("UPDATE reports SET context='private', handled_at=1 WHERE id=?", (rid,))
        await db.db.commit()
        assert await db.cleanup_report_context(7) == 1
        assert (await db.get_report(rid))["context"] == ""

        match_id = await db.log_dialog(10, 11, 5, 4, 1_000, 10)
        assert 11 not in await db.excluded_partners(10), "недавний диалог не должен ломать очередь"
        assert await db.rate_dialog(match_id, 10, 1) == 11
        rated = await db.get_user(11)
        assert rated["good_ratings"] == 1

        stats = await db.stats()
        assert stats["users"] == 3 and stats["dialogs"] == 1

        await db.bump(10, "messages", 5)
        assert (await db.get_user(10))["messages"] == 5
        top = await db.top(5)
        assert top[0]["user_id"] == 10

        await db.set_ban(11, True, "спам")
        assert await db.is_restricted(11) == "banned"
        banned, banned_total = await db.list_restricted("ban")
        assert banned_total == 1 and banned[0]["ban_reason"] == "спам"
        await db.forget_user(11)
        assert await db.is_restricted(11) == "banned", "/forget не снимает бан"
        deleted = await db.ensure_user(11, "restored", "Настоящее имя")
        assert deleted["username"] is None and deleted["first_name"] == "Удалённый пользователь"
        assert deleted["profile_deleted"] == 1
        await db.set_ban(11, False)

        await db.set_mute(12, 30)
        await db.forget_user(12)
        assert await db.is_restricted(12) == "muted", "/forget не снимает действующий мут"

        await db.forget_user(10)
        assert await db.get_user(10) is None
        assert await db.reward_claimed(10, "channel_subscription_v1") is True
        assert (await db.stats())["dialogs"] == 1  # обезличенная история нужна для статистики
        await db.close()

    async def guarded() -> None:
        try:
            await scenario()
        finally:
            db = holder.get("db")
            if db is not None:
                await db.close()

    asyncio.run(guarded())


def test_battle_game_persists_and_synchronizes() -> None:
    async def scenario() -> None:
        path = Path(tempfile.mkdtemp()) / "battle.db"
        db = await Database(path).start()
        await db.ensure_user(101, "first", "First")
        await db.ensure_user(202, "second", "Second")
        invite, created = await db.create_battle_invite(101, 202)
        assert created
        game_id = int(invite["id"])
        duplicate, duplicate_created = await db.create_battle_invite(202, 101)
        assert not duplicate_created and int(duplicate["id"]) == game_id
        game = await db.accept_battle(game_id, 202, [1, 2, 3, 4, 5])
        assert game is not None and game["status"] == "active"
        await db.close()

        db = await Database(path).start()
        restored = await db.get_battle(game_id)
        assert restored is not None and restored["status"] == "active"
        assert restored["question_ids"] == "[1, 2, 3, 4, 5]"
        active_games, active_total = await db.list_battles()
        assert active_total == 1 and int(active_games[0]["id"]) == game_id

        for index in range(5):
            state, _ = await db.answer_battle(game_id, 101, index, 0)
            assert state == "waiting"
            state, _ = await db.answer_battle(game_id, 101, index, 1)
            assert state == "already", "ответ нельзя изменять"
            state, game = await db.answer_battle(game_id, 202, index, 0)
            assert state == "resolved" and game is not None
            if index < 4:
                assert game["status"] == "round_done"
                game = await db.advance_battle(game_id, 202, index)
                assert game is not None and int(game["question_index"]) == index + 1
            else:
                assert game["status"] == "finished" and int(game["matches"]) == 5

        assert await db.get_battle(game_id) is None, "завершённая игра удаляется из БД"
        history, history_total = await db.list_battles(history=True)
        assert history == [] and history_total == 0

        assert int((await db.get_user(101))["xp"]) == 25
        assert int((await db.get_user(202))["xp"]) == 25
        perfect, created = await db.create_battle_invite(202, 101, 10)
        assert created and int(perfect["total_questions"]) == 10
        perfect_id = int(perfect["id"])
        game = await db.accept_battle(perfect_id, 101, list(range(1, 11)))
        assert game is not None
        for index in range(10):
            assert (await db.answer_battle(perfect_id, 202, index, 1))[0] == "waiting"
            state, game = await db.answer_battle(perfect_id, 101, index, 1)
            assert state == "resolved" and game is not None
            if index < 9:
                game = await db.advance_battle(perfect_id, 101, index)
                assert game is not None
        assert game["status"] == "finished" and int(game["matches"]) == 10
        assert int(game["reward_awarded"]) == 1
        assert int((await db.get_user(101))["xp"]) == 50
        assert int((await db.get_user(202))["xp"]) == 50
        assert await db.get_battle(perfect_id) is None
        assert (await db.answer_battle(perfect_id, 101, 9, 0))[0] == "missing"
        assert int((await db.get_user(101))["xp"]) == 50, "награда выдаётся только один раз"
        await db.close()

    asyncio.run(scenario())


def test_number_game_three_rounds_and_rewards() -> None:
    from anonchat.number_game import (
        NUMBER_DAILY_REWARD_LIMIT,
        NUMBER_NEAR_DIFFS,
        NUMBER_REWARDS,
        NUMBER_ROUNDS,
        number_reward,
    )

    assert NUMBER_ROUNDS == 3
    assert NUMBER_DAILY_REWARD_LIMIT == 300
    assert NUMBER_REWARDS == {10: 25, 100: 50, 1000: 100}
    assert NUMBER_NEAR_DIFFS == {10: 1, 100: 2, 1000: 5}
    assert number_reward(10, 5, 5) == 25
    assert number_reward(10, 5, 6) == 12
    assert number_reward(100, 44, 44) == 50
    assert number_reward(100, 44, 45) == 25
    assert number_reward(100, 44, 46) == 25
    assert number_reward(100, 44, 47) == 0
    assert number_reward(1000, 777, 777) == 100
    assert number_reward(1000, 777, 778) == 50
    assert number_reward(1000, 777, 782) == 50
    assert number_reward(1000, 777, 783) == 0
    assert number_reward(10, 1, 3) == 0

    async def scenario() -> None:
        path = Path(tempfile.mkdtemp()) / "numbers.db"
        db = await Database(path).start()
        try:
            await db.ensure_user(301, "numbers_a", "A")
            await db.ensure_user(302, "numbers_b", "B")

            invite, created = await db.create_number_invite(301, 302, 10)
            assert created and invite["game_type"] == "numbers"
            assert int(invite["total_questions"]) == 3 and int(invite["range_max"]) == 10
            game_id = int(invite["id"])

            conflict, conflict_created = await db.create_battle_invite(302, 301, 5)
            assert not conflict_created and int(conflict["id"]) == game_id

            game = await db.accept_number(game_id, 302)
            assert game is not None and game["status"] == "active"
            assert int(game["reward_awarded"]) == 1
            assert await db.number_pair_reward_available(301, 302) is False

            # Обычный рестарт не должен отключать награду уже начатой первой игры.
            await db.close()
            db = await Database(path).start()
            game = await db.get_battle(game_id)
            assert game is not None and int(game["reward_awarded"]) == 1

            # Раунд 1: точное совпадение = 25 каждому.
            assert (await db.answer_number(game_id, 301, 0, 5))[0] == "waiting"
            state, game, reward_a, reward_b = await db.answer_number(game_id, 302, 0, 5)
            assert state == "resolved" and game is not None
            assert (reward_a, reward_b) == (25, 25)
            assert int(game["reward_total_a"]) == 25 and int(game["reward_total_b"]) == 25
            game = await db.advance_number(game_id, 301, 0)
            assert game is not None and int(game["question_index"]) == 1

            # Раунд 2: разница 1 = половина, для 25 это 12 целых ⭐.
            assert (await db.answer_number(game_id, 301, 1, 4))[0] == "waiting"
            state, game, reward_a, reward_b = await db.answer_number(game_id, 302, 1, 5)
            assert state == "resolved" and game is not None
            assert (reward_a, reward_b) == (12, 12)
            assert int(game["reward_total_a"]) == 37 and int(game["reward_total_b"]) == 37
            game = await db.advance_number(game_id, 302, 1)
            assert game is not None and int(game["question_index"]) == 2

            # Раунд 3: большая разница = без награды, после него игра удаляется.
            assert (await db.answer_number(game_id, 301, 2, 1))[0] == "waiting"
            state, game, reward_a, reward_b = await db.answer_number(game_id, 302, 2, 9)
            assert state == "resolved" and game is not None
            assert (reward_a, reward_b) == (0, 0)
            assert game["status"] == "finished"
            assert int(game["reward_total_a"]) == 37 and int(game["reward_total_b"]) == 37
            assert await db.get_battle(game_id) is None
            assert int((await db.get_user(301))["xp"]) == 37
            assert int((await db.get_user(302))["xp"]) == 37

            # С той же парой следующая игра идёт без награды.
            again, created = await db.create_number_invite(302, 301, 1000)
            assert created
            again_id = int(again["id"])
            again = await db.accept_number(again_id, 301)
            assert again is not None and int(again["reward_awarded"]) == 0
            assert (await db.answer_number(again_id, 302, 0, 999))[0] == "waiting"
            state, again, reward_a, reward_b = await db.answer_number(again_id, 301, 0, 999)
            assert state == "resolved" and (reward_a, reward_b) == (0, 0)
            assert int((await db.get_user(301))["xp"]) == 37
            assert int((await db.get_user(302))["xp"]) == 37
            await db.cancel_battle(again_id)

            # Дневной лимит личный: одному можно упереться в 300, второму получить полную награду.
            await db.ensure_user(303, "numbers_c", "C")
            day_start = number_reward_day_start()
            await db.db.execute(
                """INSERT INTO number_daily_rewards(user_id, day_start, stars)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id, day_start) DO UPDATE SET stars=excluded.stars""",
                (301, day_start, 290),
            )
            await db.db.commit()
            capped, created = await db.create_number_invite(301, 303, 1000)
            assert created
            capped_id = int(capped["id"])
            capped = await db.accept_number(capped_id, 303)
            assert capped is not None and int(capped["reward_awarded"]) == 1
            assert (await db.answer_number(capped_id, 301, 0, 500))[0] == "waiting"
            state, capped, reward_a, reward_b = await db.answer_number(capped_id, 303, 0, 500)
            assert state == "resolved"
            assert (reward_a, reward_b) == (10, 100)
            assert await db.number_daily_reward(301) == 300
            assert await db.number_daily_reward(303) == 100
            await db.cancel_battle(capped_id)

            await db.forget_user(301)
            assert await db.number_daily_reward(301) == 0
            assert await db.number_pair_reward_available(301, 302) is True
            assert await db.number_pair_reward_available(301, 303) is True

            # Если пара закрыла чат до первого завершённого раунда, попытка не сгорает.
            await db.ensure_user(304, "numbers_d", "D")
            await db.ensure_user(305, "numbers_e", "E")
            abandoned, created = await db.create_number_invite(304, 305, 100)
            assert created
            abandoned_id = int(abandoned["id"])
            abandoned = await db.accept_number(abandoned_id, 305)
            assert abandoned is not None and int(abandoned["reward_awarded"]) == 1
            assert await db.number_pair_reward_available(304, 305) is False
            assert await db.close_battles_for_users(304, 305) == 1
            assert await db.number_pair_reward_available(304, 305) is True
        finally:
            await db.close()

    asyncio.run(scenario())


def test_stale_games_cleanup_after_two_days() -> None:
    async def scenario() -> None:
        path = Path(tempfile.mkdtemp()) / "stale-games.db"
        db = await Database(path).start()
        try:
            for uid in range(401, 411):
                await db.ensure_user(uid, f"u{uid}", f"U{uid}")

            stale_battle, _ = await db.create_battle_invite(401, 402, 5)
            fresh_battle, _ = await db.create_battle_invite(403, 404, 5)

            stale_number, _ = await db.create_number_invite(405, 406, 10)
            stale_number_id = int(stale_number["id"])
            accepted = await db.accept_number(stale_number_id, 406)
            assert accepted is not None and int(accepted["reward_awarded"]) == 1
            assert (await db.answer_number(stale_number_id, 405, 0, 5))[0] == "waiting"
            assert await db.number_pair_reward_available(405, 406) is False

            played_number, _ = await db.create_number_invite(407, 408, 10)
            played_number_id = int(played_number["id"])
            accepted = await db.accept_number(played_number_id, 408)
            assert accepted is not None
            assert (await db.answer_number(played_number_id, 407, 0, 5))[0] == "waiting"
            state, played, _, _ = await db.answer_number(played_number_id, 408, 0, 5)
            assert state == "resolved" and played is not None and played["status"] == "round_done"
            assert await db.number_pair_reward_available(407, 408) is False

            old_ts = 1_000
            fresh_ts = 200_000
            await db.db.execute(
                "UPDATE battle_games SET updated_at=? WHERE id IN (?, ?, ?)",
                (
                    old_ts,
                    int(stale_battle["id"]),
                    stale_number_id,
                    played_number_id,
                ),
            )
            await db.db.execute(
                "UPDATE battle_games SET updated_at=? WHERE id=?",
                (fresh_ts, int(fresh_battle["id"])),
            )
            await db.db.commit()

            removed = await db.cleanup_stale_games(
                max_age=2 * 24 * 60 * 60,
                timestamp=fresh_ts + 1,
            )
            assert removed == 3
            assert await db.get_battle(int(stale_battle["id"])) is None
            assert await db.get_battle(stale_number_id) is None
            assert await db.get_battle(played_number_id) is None
            assert await db.get_battle(int(fresh_battle["id"])) is not None

            # Если игра «Числа» протухла до первого завершённого раунда,
            # наградная попытка пары возвращается.
            assert await db.number_pair_reward_available(405, 406) is True
            # После хотя бы одного завершённого раунда попытка уже использована.
            assert await db.number_pair_reward_available(407, 408) is False
        finally:
            await db.close()

    asyncio.run(scenario())


def test_referral_daily_limit_and_mass_cleanup() -> None:
    async def scenario() -> None:
        path = Path(tempfile.mkdtemp()) / "referrals.db"
        db = await Database(path).start()
        try:
            referrer = 7_300_000_001
            await db.ensure_user(referrer, "referrer", "Реферер")

            for number in range(30):
                assert await db.award_referral(9_000_000 + number, referrer, 50) is True
            assert await db.award_referral(9_000_030, referrer, 50) is False
            assert await db.referral_stats(referrer) == (30, 1_500)
            assert int((await db.get_user(referrer))["xp"]) == 1_500

            # На следующие магнитогорские сутки начисление снова доступно.
            await db.db.execute(
                "UPDATE referrals SET created_at=0 WHERE referrer_id=?", (referrer,)
            )
            await db.db.commit()
            assert await db.award_referral(9_000_030, referrer, 50) is True

            # Больше стандартного лимита SQLite в 999 параметров: проверяем пакетную очистку.
            invitees = list(range(10_000_000, 10_001_002))
            await db.db.executemany(
                "INSERT INTO users (user_id, first_name) VALUES (?, ?)",
                [(user_id, f"Fake {user_id}") for user_id in invitees],
            )
            await db.db.executemany(
                "INSERT INTO referrals (invitee_id, referrer_id, xp_awarded, created_at) "
                "VALUES (?, ?, 50, 1)",
                [(user_id, referrer) for user_id in invitees],
            )
            await db.db.execute("UPDATE users SET xp=999999 WHERE user_id=?", (referrer,))
            await db.db.commit()

            paid_id, admin_id, ordinary_id = invitees[0], invitees[1], invitees[2]
            low, high = sorted((ordinary_id, referrer))
            await db.db.execute(
                "INSERT INTO number_game_pairs(user_low, user_high, consumed_at) VALUES (?, ?, 1)",
                (low, high),
            )
            await db.db.execute(
                "INSERT INTO number_daily_rewards(user_id, day_start, stars) VALUES (?, ?, 25)",
                (ordinary_id, number_reward_day_start()),
            )
            await db.db.commit()

            created, _ = await db.record_payment(
                paid_id, "support", 1, "cleanup-payment", "", "support:test"
            )
            assert created
            await db.set_admin(admin_id, {"users"}, referrer)

            preview = await db.referral_cleanup_preview(referrer)
            assert preview["referrals"] == 1_033
            assert preview["users_to_delete"] == 1_000
            assert preview["protected_users"] == 2

            result = await db.purge_referral_abuse(referrer)
            assert result["referrals_removed"] == 1_033
            assert result["users_deleted"] == 1_000
            assert result["protected_users"] == 2
            assert int((await db.get_user(referrer))["xp"]) == 948_349
            assert await db.referral_stats(referrer) == (0, 0)
            assert await db.get_user(ordinary_id) is None
            assert await db.number_pair_reward_available(ordinary_id, referrer) is True
            assert await db.number_daily_reward(ordinary_id) == 0
            assert await db.get_user(paid_id) is not None
            assert await db.get_user(admin_id) is not None
            payment = await db._fetchone(
                "SELECT stars FROM payments WHERE telegram_payment_charge_id=?",
                ("cleanup-payment",),
            )
            assert payment is not None and int(payment["stars"]) == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_recent_partner_cooldown_and_empty_dialog_counter() -> None:
    async def scenario() -> None:
        path = Path(tempfile.mkdtemp()) / "recent-pairs.db"
        db = await Database(path).start()
        try:
            await db.ensure_user(801, "u801", "U801")
            await db.ensure_user(802, "u802", "U802")
            await db.log_dialog(801, 802, 0, 0, 1, 801, count_dialog=False)
            assert int((await db.get_user(801))["dialogs"]) == 0
            assert int((await db.get_user(802))["dialogs"]) == 0
            assert 802 in await db.excluded_partners(801, recent_seconds=1800)
            assert 802 not in await db.excluded_partners(801, recent_seconds=0)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_battle_question_files() -> None:
    from anonchat.battle_questions import questions

    loaded = questions()
    assert len(loaded) == 630 and set(loaded) == set(range(1, 631))
    assert all(item.text and item.first and item.second for item in loaded.values())


# --------------------------------------------------------------------------------- ники
def test_nickname_rules() -> None:
    from anonchat import nick

    assert nick.validate("  Ким   Вайнон  ")[0] == "Ким Вайнон"
    assert nick.validate("Магнитка1743")[1] is None
    assert nick.validate("  ")[0] == "" and nick.validate("  ")[1] is None  # пусто = сброс на авто-ник
    assert nick.validate("-")[1] is not None  # сам «-» разбирает set_nick, а не validate
    assert nick.validate("a")[1] and "минимум" in nick.validate("a")[1]
    assert nick.validate("з" * 30)[1] and "максимум" in nick.validate("з" * 30)[1]
    for bad in ("<b>ник</b>", "ник/соslash", "@username", "ник`x", "back\\slash"):
        assert nick.validate(bad)[1] is not None, bad
    assert nick.validate("Йцукен7 !?-_()")[1] is None
    assert nick.auto_nick(1001).startswith("Аноним-") and "1001" not in nick.auto_nick(1001)
    assert nick.auto_nick(-98765432).startswith("Аноним-")
    assert nick.display("", 5) == nick.auto_nick(5)
    assert nick.display("  ", 5) == nick.auto_nick(5)
    assert nick.display("Лена", 5) == "Лена"
    assert nick.display("Лена", 5, 1) == "Лена 💎"
    assert nick.display("Лена", 5, 0) == "Лена"
    assert nick.validate("Лена ✦")[1] is not None


# --------------------------------------------------------------------------------- кнопки
def test_keyboard_styles_and_icons() -> None:
    """Все клавиатуры: иконки — числовые id пака, цвета — только те, что принимает Bot API.

    «warning» Bot API отвергает (проверено живьём: Invalid button style specified),
    поэтому любая опечатка в style роняет отправку сообщения.
    """
    from anonchat import keyboards as K

    markups = [
        K.menu_keyboard("free"), K.menu_keyboard("queued", 3), K.menu_keyboard("paired"),
        K.menu_keyboard("free", admin=True), K.continue_keyboard(), K.age_keyboard(),
        K.chat_keyboard(), K.profile_keyboard("https://t.me/test_bot?start=ref_1"),
        K.district_keyboard(), K.gender_keyboard(), K.looking_for_keyboard(),
        K.settings_keyboard(True, "Правый берег", "Лена О", "m", "f"),
        K.games_keyboard(), K.number_range_keyboard(), K.number_invite_keyboard(1),
        K.number_input_keyboard(1, 0), K.number_input_keyboard(1, 0, "10"),
        K.number_next_keyboard(1, 0), K.number_end_keyboard(),
        K.battle_invite_keyboard(1),
        K.battle_answer_keyboard(1, 0, "ночь", "утро"),
        K.battle_next_keyboard(1, 0), K.battle_end_keyboard(),
        K.report_keyboard(), K.rating_keyboard(), K.confirm_stop_keyboard(),
        K.confirm_forget_keyboard(),
        K.back_menu_keyboard(), K.skip_cancel_keyboard(),
        K.admin_report_keyboard(1),
        K.restricted_list_keyboard("ban", [10, 11], 0, 2),
        K.game_watch_keyboard(),
        K.admin_panel_keyboard(3, {"stats", "reports", "queue", "users", "broadcast", "mute", "ban", "points"}, True),
        K.users_page_keyboard(0, 30), K.panel_back_keyboard(),
        K.panel_cancel_keyboard(),
    ]
    icons = set()
    total = 0
    for markup in markups:
        for row in markup.inline_keyboard:
            for btn in row:
                data = btn.model_dump()
                total += 1
                style = data.get("style")
                assert style in (None, "") or style in K.STYLES, f"{btn.text}: style={style}"
                icon = data.get("icon_custom_emoji_id")
                if icon:
                    assert icon.isdigit(), f"{btn.text}: иконка не id — {icon}"
                    icons.add(icon)
    assert total >= 40, f"клавиатур стало подозрительно мало: {total} кнопок"
    assert len(icons) >= 12, f"иконки должны брать из пака, а не из одного места: {len(icons)}"
    # Эмодзи в подписях разрешены только там, где это часть выбора пола/поиска.
    emoji_labels = {"👨 М", "👩 Д", "👨 Ищу М", "👩 Ищу Д", "🤷 Без разницы"}
    for markup in markups:
        for row in markup.inline_keyboard:
            for btn in row:
                if btn.text in emoji_labels or btn.text == "👍 Норм" or "⭐" in btn.text:
                    continue
                assert all(
                    ord(c) < 0x2500 or c in "\ufe0f\ufe0e\u200d" for c in btn.text
                ), f"в подписи кнопки остался неожиданный юникодный эмодзи: {btn.text!r}"

    def texts_of(markup):
        return [btn.text for row in markup.inline_keyboard for btn in row]

    # в диалоге из меню остаются только действия диалога
    assert texts_of(K.district_keyboard()) == ["Правый берег", "Левый берег", "Назад"]

    paired = K.menu_keyboard("paired")
    assert texts_of(paired) == ["Следующий", "Стоп", "Жалоба", "Игры"]
    assert [[button.text for button in row] for row in paired.inline_keyboard] == [
        ["Следующий"], ["Стоп", "Жалоба"], ["Игры"],
    ]
    assert texts_of(K.games_keyboard()) == ["Битва мнений", "Числа", "Объясни слово", "Вернуться в чат"]
    assert texts_of(K.number_range_keyboard()) == [
        "1–10 · 25 ⭐", "1–100 · 50 ⭐", "1–1000 · 100 ⭐", "Назад",
    ]
    assert texts_of(K.battle_length_keyboard()) == ["5 вопросов", "10 вопросов", "Назад"]
    # панель модератора: 9 разделов, счётчик жалоб в подписи
    panel = texts_of(K.admin_panel_keyboard(2, {"reports", "mute"}))
    assert panel == ["Жалобы · 2", "Мут по id", "Мут-лист", "В меню"], panel
    owner_panel = texts_of(K.admin_panel_keyboard(0, {"stats"}, True))
    assert all(item in owner_panel for item in (
        "Администраторы", "Скачать базу", "Игры пользователей", "Чаты: ВЫКЛ",
    ))
    assert "Игры пользователей" in texts_of(K.admin_panel_keyboard(0, {"monitor"}))
    assert "Игры пользователей" not in texts_of(K.admin_panel_keyboard(0, {"reports"}))
    assert "Чаты: ВКЛ" in texts_of(K.admin_panel_keyboard(0, {"stats"}, True, True))
    # кнопка входа в панель появляется только у админа
    assert texts_of(K.menu_keyboard("free", admin=True))[-1] == "Панель модератора"
    assert "Поддержать проект" not in texts_of(K.menu_keyboard("free"))
    assert "Поддержать проект" in texts_of(K.settings_keyboard(False, "", "Ник"))
    assert "Обратная связь" in texts_of(K.settings_keyboard(False, "", "Ник"))


def test_contact_filter() -> None:
    from anonchat.safety import contains_contact

    for value in ("+7 999 123-45-67", "mail@example.com", "ул. Ленина 10"):
        assert contains_contact(value), value
    for value in ("@username", "https://example.com", "t.me/test"):
        assert not contains_contact(value), value

    from anonchat.permissions import parse_permissions
    assert "monitor" in parse_permissions("all")
    assert not contains_contact("Привет, как дела?")


def test_stars_payment_validation() -> None:
    from types import SimpleNamespace

    from anonchat.handlers.support import _valid_payload

    good = SimpleNamespace(
        invoice_payload="support:42:25:abcdef", from_user=SimpleNamespace(id=42),
        currency="XTR", total_amount=25,
    )
    assert _valid_payload(good)
    premium = SimpleNamespace(
        invoice_payload="premium:42:30:abcdef", from_user=SimpleNamespace(id=42),
        currency="XTR", total_amount=129,
    )
    assert not _valid_payload(premium)
    assert not _valid_payload(SimpleNamespace(**{**good.__dict__, "currency": "RUB"}))
    assert not _valid_payload(SimpleNamespace(**{**good.__dict__, "total_amount": 24}))
    assert not _valid_payload(SimpleNamespace(**{**good.__dict__, "invoice_payload": "bad"}))


def test_retry_after_retries_real_delivery() -> None:
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    from anonchat.actions import DeliveryResult, send_copy_to, send_to

    class RetryBot:
        def __init__(self) -> None:
            self.calls = 0

        async def send_message(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TelegramRetryAfter(SendMessage(chat_id=1, text="x"), "retry", 0)
            return object()

        async def send_chat_action(self, *args, **kwargs):
            return True

    class RetryMessage:
        def __init__(self) -> None:
            self.calls = 0

        async def send_copy(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TelegramRetryAfter(SendMessage(chat_id=1, text="x"), "retry", 0)
            return object()

    async def scenario() -> None:
        bot = RetryBot()
        assert await send_to(bot, 1, "ok") is DeliveryResult.DELIVERED and bot.calls == 2
        message = RetryMessage()
        assert await send_copy_to(bot, message, 1) is DeliveryResult.DELIVERED and message.calls == 2

    asyncio.run(scenario())


def test_delivery_results() -> None:
    from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
    from aiogram.methods import SendMessage

    from anonchat.actions import DeliveryResult, send_to

    class ErrorBot:
        def __init__(self, exc: Exception) -> None:
            self.exc = exc

        async def send_message(self, *args, **kwargs):
            raise self.exc

    async def scenario() -> None:
        method = SendMessage(chat_id=1, text="x")
        forbidden = ErrorBot(TelegramForbiddenError(method, "bot was blocked"))
        temporary = ErrorBot(TelegramAPIError(method, "temporary failure"))
        assert await send_to(forbidden, 1, "x") is DeliveryResult.UNAVAILABLE
        assert await send_to(temporary, 1, "x") is DeliveryResult.TEMP_ERROR

    asyncio.run(scenario())


def test_screen_fallback_without_image() -> None:
    from types import SimpleNamespace

    from anonchat.actions import Ctx
    from anonchat.config import Config
    from anonchat.matching import Matchmaker
    from anonchat.pack import EmojiPack

    class Target:
        def __init__(self) -> None:
            self.sent = ""

        async def answer(self, text: str, **kwargs):
            self.sent = text
            return self

    async def scenario() -> None:
        target = Target()
        ctx = Ctx(
            bot=SimpleNamespace(), db=SimpleNamespace(), mm=Matchmaker(),
            cfg=Config(bot_token="1:T"), pack=EmojiPack(), event=target, user_id=1,
        )
        await ctx.render_screen("missing-screen.png", "fallback text")
        assert "fallback text" in target.sent

    asyncio.run(scenario())


def test_database_open_error_is_explicit() -> None:
    async def scenario() -> None:
        root = Path(tempfile.mkdtemp())
        parent_file = root / "not-a-directory"
        parent_file.write_text("x", encoding="utf-8")
        try:
            await Database(parent_file / "bot.db").start()
        except RuntimeError as exc:
            assert "SQLite" in str(exc)
        else:
            raise AssertionError("недоступный production DB path обязан завершать запуск")

    asyncio.run(scenario())


def test_supporter_persists_restart() -> None:
    async def scenario() -> None:
        path = Path(tempfile.mkdtemp()) / "support.db"
        db = await Database(path).start()
        await db.ensure_user(77, None, "Тест")
        created, support_total = await db.record_payment(
            77, "support", 1, "persist-charge", "", "support:77:1:x"
        )
        assert created and support_total == 1
        await db.close()
        reopened = await Database(path).start()
        try:
            assert int((await reopened.get_user(77))["support_stars"]) == support_total
        finally:
            await reopened.close()

    asyncio.run(scenario())


def test_live_database_backup_contains_runtime_state() -> None:
    async def scenario() -> None:
        root = Path(tempfile.mkdtemp())
        db = await Database(root / "live.db").start()
        mm = Matchmaker()
        try:
            await db.ensure_user(101, "backup_user", "Backup")
            mm.connect(101)
            db.schedule_matchmaker_save(mm)
            save_task = db._matchmaker_task
            for _ in range(1000):
                db.schedule_matchmaker_save(mm)
            assert db._matchmaker_task is save_task, "частые сообщения должны объединяться в одну запись"
            await db.flush_matchmaker(mm)
            await db.backup_to(root / "backup.db")
        finally:
            await db.close()

        backup = await Database(root / "backup.db").start()
        try:
            assert (await backup.get_user(101))["username"] == "backup_user"
            state = await backup.load_matchmaker()
            restored = Matchmaker()
            restored.restore(state or {})
            assert restored.status(101) == "queued"
        finally:
            await backup.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------------- пак эмодзи
def test_pack_emoji() -> None:
    from anonchat.pack import ICONS, PACK, EmojiPack

    # id пака зашиты в код: премиум-эмодзи работают с первого сообщения, ничего ждать не надо
    assert len(PACK) >= 20 and len(ICONS) == len(PACK)
    assert all(emoji_id.isdigit() for emoji_id, _, _ in PACK.values()), "custom_emoji_id — цифры"
    fresh = EmojiPack()
    assert fresh.wrap("📊 Профиль") == '<tg-emoji emoji-id="5231200819986047254">📊</tg-emoji> Профиль'

    pack = EmojiPack("https://t.me/addemoji/NewsEmoji")
    assert pack.wrap("🧲 старт") == "🧲 старт"  # магнита в паке нет — остаётся юникодом
    assert not hasattr(pack, "harvest"), "сообщения пользователей не меняют UI emoji"

    # алиасы: в тексте «✅», в паке этот же знак «✔️» — внутрь тега у канонический символ
    aliased = pack.wrap("✅ готово")
    assert aliased == '<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> готово', aliased

    # один и тот же знак в сообщении оборачиваем один раз — глазами это один акцент
    twice = pack.wrap("📊 и ещё 📊")
    assert twice.count("<tg-emoji") == 1, twice

    # лимит подмены: украшаем максимум N эмодзи в сообщении, начиная с заголовка
    limited = pack.wrap("📊⭐⚙️💬", limit=2)
    assert limited.count("<tg-emoji") == 2, limited

    # Telegram запретил тег — откатываемся и больше не пробуем
    assert pack.accept(Exception("Bad Request: can't parse entities: tg-emoji is unsupported")) is True
    assert pack.enabled is False
    assert pack.wrap("🧲 старт") == "🧲 старт"


# --------------------------------------------------------------------------------- база: ник + миграция
def test_db_nickname_and_kv() -> None:
    import sqlite3

    async def scenario() -> None:
        tmp = Path(tempfile.mkdtemp()) / "migrate.db"
        # база, созданная более ранней версией бота: без колонки nickname и таблицы kv
        old = sqlite3.connect(tmp)
        old.execute(
            """CREATE TABLE users (
                   user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                   created_at INTEGER NOT NULL DEFAULT 0, last_seen INTEGER NOT NULL DEFAULT 0,
                   xp INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
                   dialogs INTEGER NOT NULL DEFAULT 0, good_ratings INTEGER NOT NULL DEFAULT 0,
                   bad_ratings INTEGER NOT NULL DEFAULT 0, reports_sent INTEGER NOT NULL DEFAULT 0,
                   reports_received INTEGER NOT NULL DEFAULT 0, district TEXT NOT NULL DEFAULT '',
                   gender TEXT NOT NULL DEFAULT '', same_district INTEGER NOT NULL DEFAULT 0,
                   about TEXT NOT NULL DEFAULT '', banned INTEGER NOT NULL DEFAULT 0,
                   ban_reason TEXT NOT NULL DEFAULT '', mute_until INTEGER NOT NULL DEFAULT 0)"""
        )
        old.execute("INSERT INTO users (user_id, first_name, xp) VALUES (7, 'Олд', 100)")
        old.execute(
            """CREATE TABLE battle_games (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_a INTEGER NOT NULL, user_b INTEGER NOT NULL,
                   inviter_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'invited',
                   question_ids TEXT NOT NULL DEFAULT '[]',
                   question_index INTEGER NOT NULL DEFAULT 0,
                   answer_a INTEGER, answer_b INTEGER,
                   matches INTEGER NOT NULL DEFAULT 0,
                   total_questions INTEGER NOT NULL DEFAULT 5,
                   reward_awarded INTEGER NOT NULL DEFAULT 0,
                   created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
               )"""
        )
        old.execute(
            """INSERT INTO battle_games(
                   user_a, user_b, inviter_id, status, created_at, updated_at
               ) VALUES (7, 8, 7, 'invited', 1, 1)"""
        )
        old.commit()
        old.close()

        db = await Database(tmp).start()
        try:
            assert await db.get_user(7) is not None, "старые данные не потерялись"
            assert (await db.get_user(7))["nickname"] == "", "колонка nickname добавлена на лету"
            assert (await db.get_user(7))["looking_for"] == "", "предпочтение пола мигрируется без потери базы"
            old_game = await db.get_battle(1)
            assert old_game is not None and old_game["game_type"] == "battle"
            assert int(old_game["range_max"]) == 0 and int(old_game["reward_total"]) == 0
            assert int(old_game["reward_total_a"]) == 0 and int(old_game["reward_total_b"]) == 0
            assert await db.number_pair_reward_available(7, 8) is True
            assert await db.number_daily_reward(7) == 0

            await db.set_profile(7, nickname="Старожил")
            assert (await db.get_user(7))["nickname"] == "Старожил"
            assert await db.nickname_taken("старожил") == 7, "кириллица тоже сравнивается без регистра"
            assert await db.nickname_taken("СТАРОЖИЛ") == 7
            assert await db.nickname_taken("Старожил", except_user_id=7) is None
            assert await db.nickname_taken("Свободный") is None
            assert await db.nickname_taken("") is None

            await db.set_kv("emoji_ids", '[["🧲","AAA"]]')
            assert await db.get_kv("emoji_ids") == '[["🧲","AAA"]]'
            await db.set_kv("emoji_ids", '[["🧲","BBB"]]')
            assert await db.get_kv("emoji_ids") == '[["🧲","BBB"]]'
            assert await db.get_kv("нет-такого", "дефолт") == "дефолт"

            top = await db.top(5)
            assert top[0]["nickname"] == "Старожил", "в топ уходит ник, а не настоящее имя"
            assert "first_name" not in top[0].keys()
            assert "username" not in top[0].keys()
        finally:
            await db.close()

    asyncio.run(scenario())  # внутри scenario db закрывается в finally — процесс не зависнет


def test_engagement_activity_streak_achievements_and_quests() -> None:
    async def scenario() -> None:
        path = Path(tempfile.mkdtemp()) / "engagement.db"
        db = await Database(path).start()
        try:
            await db.ensure_user(501, "u501", "U501")
            await db.ensure_user(502, "u502", "U502")

            day = referral_day_start()
            await db.activity_add(501, timestamp=day, messages=20, dialogs=1, xp_earned=25)
            today = await db.activity_totals(501, 1)
            assert today["messages"] == 20 and today["dialogs"] == 1
            top = await db.top_period(7, 10)
            assert top and int(top[0]["user_id"]) == 501 and int(top[0]["xp"]) == 25

            streak, best = await db.update_streak(501, day)
            assert (streak, best) == (1, 1)
            assert await db.update_streak(501, day) == (1, 1)
            assert await db.update_streak(501, day + 86_400) == (2, 2)
            assert await db.update_streak(501, day + 3 * 86_400) == (1, 2)

            assert await db.unlock_achievement(501, "test_unique", 25) is True
            after = int((await db.get_user(501))["xp"])
            assert await db.unlock_achievement(501, "test_unique", 25) is False
            assert int((await db.get_user(501))["xp"]) == after

            results = await asyncio.gather(
                db.unlock_achievement(502, "race_unique", 50),
                db.unlock_achievement(502, "race_unique", 50),
            )
            assert sorted(results) == [False, True]
            assert int((await db.get_user(502))["xp"]) == 50

            assert await db.claim_daily_quest(501, day, "quest_test", 25) is True
            assert await db.claim_daily_quest(501, day, "quest_test", 25) is False
            assert "quest_test" in await db.daily_quest_claimed(501, day)
            next_day = day + 86_400
            assert await db.claim_daily_quest(501, next_day, "quest_test", 25) is True
            assert "quest_test" in await db.daily_quest_claimed(501, next_day)
            assert await db.daily_quest_claimed(501, day) == set()

            await db.record_game_engagement(
                501, "battle", matches=5, total=5
            )
            await db.record_game_engagement(
                501, "numbers", matches=1, total=3, number_exact=1, range_max=1000
            )
            state = await db.engagement_state(501)
            assert int(state["games_total"]) == 2
            assert int(state["battle_perfect_5"]) == 1
            assert int(state["number_exact_1000"]) == 1

            await db.forget_user(501)
            assert await db._fetchone("SELECT 1 FROM daily_activity WHERE user_id=501") is None
            assert await db._fetchone("SELECT 1 FROM user_engagement WHERE user_id=501") is None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_period_top_backfills_legacy_xp_once() -> None:
    async def scenario() -> None:
        path = Path(tempfile.mkdtemp()) / "legacy-top.db"
        db = await Database(path).start()
        try:
            await db.ensure_user(901, "legacy", "Legacy")
            created = referral_day_start()
            await db.db.execute(
                "UPDATE users SET xp=500, created_at=? WHERE user_id=?",
                (created, 901),
            )
            await db.activity_add(901, timestamp=created, xp_earned=120)
            await db.delete_kv("daily_activity_xp_backfill_v1")
            await db.db.commit()
        finally:
            await db.close()

        db = await Database(path).start()
        try:
            totals = await db.activity_totals(901, 7)
            assert totals["xp_earned"] == 500
            week = await db.top_period(7, 10)
            month = await db.top_period(30, 10)
            assert week and int(week[0]["user_id"]) == 901 and int(week[0]["xp"]) == 500
            assert month and int(month[0]["user_id"]) == 901 and int(month[0]["xp"]) == 500
        finally:
            await db.close()

        db = await Database(path).start()
        try:
            totals = await db.activity_totals(901, 7)
            assert totals["xp_earned"] == 500, "повторный старт не должен дублировать backfill"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_relay_state_reply_and_cleanup() -> None:
    from anonchat import relay_state

    relay_state.clear_user(1)
    relay_state.clear_user(2)
    relay_state.remember(1, 101, 2, 201)
    assert relay_state.resolve_reply(2, 1, 201) == 101
    assert relay_state.resolve_reply(1, 2, 101) == 201
    assert relay_state.size() >= 1
    relay_state.clear_pair(1, 2)
    assert relay_state.resolve_reply(2, 1, 201) is None
    assert relay_state.forwarded_target(1, 101) is None


def test_word_game_word_pool() -> None:
    from anonchat.word_game import WORDS, WORD_DAILY_REWARD_LIMIT

    assert WORD_DAILY_REWARD_LIMIT == 300
    assert len(WORDS) >= 500
    assert len(WORDS) == len(set(WORDS))
    assert {"магнитка", "могну", "магнитогорск", "черемша"} <= set(WORDS)


def test_miniapp_init_data_signature() -> None:
    import hashlib
    import hmac
    import json
    import time
    from urllib.parse import urlencode

    from aiohttp.web_exceptions import HTTPUnauthorized

    from anonchat.miniapp_api import validate_init_data

    token = "123456:TEST_TOKEN"
    params = {
        "auth_date": str(int(time.time())),
        "query_id": "test-query",
        "user": json.dumps(
            {"id": 42, "first_name": "Тест", "username": "tester"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    signed = urlencode(params)

    assert validate_init_data(signed, token)["id"] == 42
    try:
        validate_init_data(signed, "wrong-token")
    except HTTPUnauthorized:
        pass
    else:
        raise AssertionError("initData с чужой подписью должен отклоняться")


def test_miniapp_health_static_and_origin_guard() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from anonchat.miniapp_api import MiniAppServer

    async def scenario() -> None:
        web_dir = Path(__file__).resolve().parents[1] / "miniapp" / "web"
        miniapp = MiniAppServer(None, None, None, None, None, web_dir=web_dir)
        client = TestClient(TestServer(miniapp.create_app()))
        await client.start_server()
        try:
            health = await client.get("/api/miniapp/health")
            assert health.status == 200
            assert (await health.json())["service"] == "anon-mgn-miniapp"

            own_origin = str(client.make_url("/")).rstrip("/")
            same_origin = await client.get(
                "/api/miniapp/health", headers={"Origin": own_origin}
            )
            assert same_origin.status == 200
            assert same_origin.headers["Access-Control-Allow-Origin"] == own_origin

            foreign = await client.get(
                "/api/miniapp/health", headers={"Origin": "https://evil.example"}
            )
            assert foreign.status == 403

            index = await client.get("/")
            assert index.status == 200 and "АНОН МГН" in await index.text()
        finally:
            await client.close()

    asyncio.run(scenario())


def run_all() -> int:  # python -m tests.core
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nall {len(fns)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_all())
