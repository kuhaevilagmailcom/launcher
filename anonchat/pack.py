"""Эмодзи из городского пака NewsEmoji (https://t.me/addemoji/NewsEmoji).

В Telegram эмодзи из пака — это не юникодный символ, а анимированный файл с id.
Бот может ставить его двумя способами:

* в **текст** — тегом ``<tg-emoji emoji-id="…">👀</tg-emoji>`` (обычные символы
  внутри тега Telegram заменит на анимацию; у тех, у кого нет премиума — статичная
  картинка пака);
* в **кнопку** — полем ``icon_custom_emoji_id`` (работает и у inline-, и у reply-кнопок).

Таблица ``PACK`` ниже фиксирована в коде: сообщения пользователей никогда не меняют
оформление интерфейса бота.

Алиасы нужны, чтобы не переписывать тексты: в них ``✅``, а в паке этот же знак
называется ``✔️`` — пишем одно, показываем другое.
"""

from __future__ import annotations

import re

TG_EMOJI_OPEN = re.compile(r"<tg-emoji[^>]*>")
TG_EMOJI_CLOSE = re.compile(r"</tg-emoji>")

#: сколько эмодзи пака в одном сообщении бота. Больше — уже карнавал.
MAX_WRAP_PER_MESSAGE = 5

#: имя -> (custom_emoji_id, как знак выглядит без анимации, какие символы ещё сводим на него)
PACK: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "support": ("5443038326535759644", "💬", ("💌",)),
    "check": ("5206607081334906820", "✔️", ("✅", "☑️")),
    "warn": ("5447644880824181073", "⚠️", ("❗", "🚩")),
    "money": ("5409048419211682843", "💵", ("💸", "💰")),
    "catalog": ("5229064374403998351", "🛍", ()),
    "ticket": ("5222444124698853913", "🔖", ("📜",)),
    "profile": ("5461117441612462242", "🙂", ("🙋", "👤")),
    "bonus": ("5427168083074628963", "💎", ()),
    "settings": ("5341715473882955310", "⚙️", ("⚙",)),
    "home": ("5416041192905265756", "🏠", ()),
    "next": ("5416117059207572332", "➡️", ("⏭",)),
    "stars": ("5438496463044752972", "⭐️", ("⭐", "🌟")),
    "geo": ("5391032818111363540", "📍", ("📌",)),
    "gift": ("5461151367559141950", "🎉", ("🎁",)),
    "refresh": ("5375338737028841420", "🔄", ("♻️", "🔁", "🔃")),
    "promo": ("5341498088408234504", "💯", ()),
    "link": ("5271604874419647061", "🔗", ()),
    "stats": ("5231200819986047254", "📊", ("📈",)),
    "add": ("5397916757333654639", "➕", ()),
    "edit": ("5395444784611480792", "✏️", ("✍️", "📝")),
    "view": ("5210956306952758910", "👀", ()),
    "delete": ("5445267414562389170", "🗑", ("🧹",)),
}

#: имя -> id, для кнопок (icon_custom_emoji_id)
ICONS: dict[str, str] = {name: emoji_id for name, (emoji_id, _, _) in PACK.items()}


class EmojiPack:
    def __init__(self, url: str = "") -> None:
        self.url = url
        self.enabled = True
        # символ -> (id, чем его показывать внутри тега)
        self._map: dict[str, tuple[str, str]] = {}
        self._progress_bar: list[tuple[str, str]] = []
        self._top_flags: dict[int, tuple[str, str]] = {}
        for _name, (emoji_id, canonical, aliases) in PACK.items():
            for glyph in (canonical, *aliases):
                self._map.setdefault(glyph, (emoji_id, canonical))

    async def load_progress_bar(self, bot, name: str = "progressBarEmoji") -> int:
        """Загружает отдельный custom-emoji набор только для прогресс-бара профиля."""
        try:
            sticker_set = await bot.get_sticker_set(name=name)
        except Exception:
            self._progress_bar = []
            return 0

        sticker_type = getattr(sticker_set, "sticker_type", "")
        sticker_type = getattr(sticker_type, "value", sticker_type)
        if sticker_type and str(sticker_type) != "custom_emoji":
            self._progress_bar = []
            return 0

        items: list[tuple[str, str]] = []
        for sticker in getattr(sticker_set, "stickers", ()) or ():
            custom_id = str(getattr(sticker, "custom_emoji_id", "") or "")
            glyph = str(getattr(sticker, "emoji", "") or "").strip()
            if custom_id and glyph:
                items.append((custom_id, glyph))
        self._progress_bar = items
        return len(items)

    def progress_bar(self, progress: float) -> str:
        """Возвращает один emoji состояния прогресса из progressBarEmoji."""
        if not self.enabled or not self._progress_bar:
            return ""
        value = min(1.0, max(0.0, float(progress or 0.0)))
        index = min(len(self._progress_bar) - 1, round(value * (len(self._progress_bar) - 1)))
        emoji_id, glyph = self._progress_bar[index]
        return f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>'

    async def load_top_flags(self, bot, name: str = "FestiveFlags") -> int:
        """FestiveFlags для топа: используем только фиксированные ID мест 1–10."""
        self._top_flags = {
            1: ("5461033346152804686", "1️⃣"),
            2: ("5469622950032321924", "2️⃣"),
            3: ("5460917699863393660", "3️⃣"),
            4: ("5188675391010645868", "4️⃣"),
            5: ("5190762616267485914", "5️⃣"),
            6: ("5469750759669118937", "6️⃣"),
            7: ("5469710391271501767", "7️⃣"),
            8: ("5469662901818108143", "8️⃣"),
            9: ("5474619534595862352", "9️⃣"),
            0: ("5458531398853865378", "0️⃣"),
        }
        return 10

    def top_flag(self, place: int) -> str:
        """Цифровые custom emoji мест. Для 10 выводим отдельные custom emoji 1 и 0."""
        number = int(place)
        if not self.enabled:
            return ""
        if number == 10:
            one = self._top_flags.get(1)
            zero = self._top_flags.get(0)
            if not one or not zero:
                return ""
            return (
                f'<tg-emoji emoji-id="{one[0]}">{one[1]}</tg-emoji>'
                f'<tg-emoji emoji-id="{zero[0]}">{zero[1]}</tg-emoji>'
            )
        item = self._top_flags.get(number)
        if not item:
            return ""
        emoji_id, glyph = item
        return f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>'

    def known(self) -> int:
        return len(self._map)

    def has(self, emoji: str) -> bool:
        return emoji in self._map

    # ------------------------------------------------------------------ render
    def wrap(self, text: str, limit: int = MAX_WRAP_PER_MESSAGE) -> str:
        """Первые `limit` эмодзи, для которых есть id, заворачиваем в тег пака.

        Идём слева направо, поэтому достается в первую очередь заголовкам.
        Внутри тега всегда канонический знак пака (``✅`` → ``✔️``), иначе Telegram
        может счесть вложенный символ не соответствующим id.
        """
        if not self.enabled or not self._map or not text:
            return text
        keys = sorted(self._map, key=len, reverse=True)
        used: set[str] = set()
        out: list[str] = []
        i, n, budget = 0, len(text), limit
        while i < n:
            # Уже собранный custom emoji (например progressBarEmoji) не трогаем,
            # чтобы не получить вложенный <tg-emoji>.
            if text.startswith("<tg-emoji", i):
                end = text.find("</tg-emoji>", i)
                if end != -1:
                    end += len("</tg-emoji>")
                    out.append(text[i:end])
                    i = end
                    continue
            matched = False
            if budget:
                for key in keys:
                    if text.startswith(key, i) and key not in used:
                        emoji_id, canonical = self._map[key]
                        out.append(f'<tg-emoji emoji-id="{emoji_id}">{canonical}</tg-emoji>')
                        used.add(key)
                        # тот же знак больше не оборачиваем: глазами это один акцент
                        i += len(key)
                        budget -= 1
                        matched = True
                        break
            if not matched:
                out.append(text[i])
                i += 1
        return "".join(out)

    def accept(self, exc: Exception) -> bool:
        """Telegram ругнулся на эмодзи-тег? Отключаем пак и пробуем обычным текстом."""
        message = str(getattr(exc, "message", "") or exc)
        if self.enabled and self.looks_like_emoji_error(message):
            self.enabled = False
            return True
        return False

    @staticmethod
    def strip(text: str) -> str:
        return TG_EMOJI_CLOSE.sub("", TG_EMOJI_OPEN.sub("", text))

    @staticmethod
    def looks_like_emoji_error(message: str) -> bool:
        low = (message or "").lower()
        return "tg-emoji" in low or "custom_emoji" in low or "emoji" in low
