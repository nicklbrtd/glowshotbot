from __future__ import annotations

import re
from typing import Final


_COMMON_TLDS: Final[tuple[str, ...]] = (
    ".com",
    ".ru",
    ".org",
    ".net",
    ".io",
    ".gg",
    ".me",
    ".dev",
    ".app",
    ".info",
    ".biz",
    ".online",
    ".site",
    ".store",
    ".shop",
    ".club",
    ".photo",
    ".art",
    ".media",
    ".tv",
)

_DOMAIN_RE: Final[re.Pattern[str]] = re.compile(r"\b[\w-]{2,}\.[a-z]{2,}\b", re.IGNORECASE)
_AT_SIGNS: Final[tuple[str, ...]] = ("@", "＠", "﹫")


def has_links_or_usernames(text: str | None) -> bool:
    if not text:
        return False

    lowered = text.lower()

    if "://" in lowered:
        return True

    if "www." in lowered:
        return True

    if "t.me/" in lowered or "telegram.me/" in lowered:
        return True

    if " тг" in lowered or lowered.startswith("тг") or " tg" in lowered or lowered.startswith("tg"):
        return True

    if " тгк" in lowered or lowered.startswith("тгк") or " tgk" in lowered or lowered.startswith("tgk"):
        return True

    if any(tld in lowered for tld in _COMMON_TLDS):
        return True

    if _DOMAIN_RE.search(lowered):
        return True

    if any(sign in text for sign in _AT_SIGNS):
        return True

    return False


# --- Детект "рекламы тг-канала" ---


_SUBSCRIBE_MARKERS: Final[tuple[str, ...]] = (
    "подписывайся",
    "подписывайтесь",
    "подпишись",
    "подпишитесь",
    "подписка",
    "подписок",
    "подпиш",
    "фолов"
    "подписку"
    "падпишис"
)

_CHANNEL_MARKERS: Final[tuple[str, ...]] = (
    "тгк",
    "тг",
    "тгканал",
    "тг канал",
    "tg канал",
    "tg-канал",
    "tg-канал",
    "tgk",
    "tg",
    "телеграм канал",
    "телеграм-канал",
    "канал в тг",
    "канал в tg",
    "тгшка"
    "тгшечка"
)

_POSSESSIVE_MARKERS: Final[tuple[str, ...]] = (
    "мой канал",
    "моя канал",
    "моё канал",
    "мое канал",
    "наш канал",
    "свой канал",
)


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def has_promo_channel_invite(text: str | None) -> bool:
    if not text:
        return False

    normalized = _normalize_spaces(text)

    if "канал" in normalized and any(marker in normalized for marker in _SUBSCRIBE_MARKERS):
        return True

    if any(marker in normalized for marker in _POSSESSIVE_MARKERS):
        return True

    if any(marker in normalized for marker in _CHANNEL_MARKERS):
        return True

    return False
