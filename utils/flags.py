

from __future__ import annotations

import re


def _norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _key(s: str) -> str:
    """Normalize country names for matching."""
    s = _norm_spaces(s).lower()
    s = s.replace("ё", "е")
    # keep letters/numbers/spaces and a few separators
    s = re.sub(r"[^a-z0-9а-я\-\s]", "", s, flags=re.IGNORECASE)
    s = _norm_spaces(s)
    return s


# Common country name variants -> flag emoji.
# Note: keep keys human-readable; we normalize via _key() at lookup time.
_FLAGS_RAW: dict[str, str] = {
    # CIS
    "Россия": "🇷🇺",
    "Russian Federation": "🇷🇺",
    "Украина": "🇺🇦",
    "Ukraine": "🇺🇦",
    "Беларусь": "🇧🇾",
    "Белоруссия": "🇧🇾",
    "Belarus": "🇧🇾",
    "Казахстан": "🇰🇿",
    "Kazakhstan": "🇰🇿",
    "Узбекистан": "🇺🇿",
    "Uzbekistan": "🇺🇿",
    "Кыргызстан": "🇰🇬",
    "Kyrgyzstan": "🇰🇬",
    "Таджикистан": "🇹🇯",
    "Tajikistan": "🇹🇯",
    "Туркменистан": "🇹🇲",
    "Turkmenistan": "🇹🇲",
    "Молдова": "🇲🇩",
    "Moldova": "🇲🇩",
    "Грузия": "🇬🇪",
    "Georgia": "🇬🇪",
    "Армения": "🇦🇲",
    "Armenia": "🇦🇲",
    "Азербайджан": "🇦🇿",
    "Azerbaijan": "🇦🇿",

    # Europe
    "Испания": "🇪🇸",
    "Spain": "🇪🇸",
    "Франция": "🇫🇷",
    "France": "🇫🇷",
    "Германия": "🇩🇪",
    "Germany": "🇩🇪",
    "Италия": "🇮🇹",
    "Italy": "🇮🇹",
    "Португалия": "🇵🇹",
    "Portugal": "🇵🇹",
    "Польша": "🇵🇱",
    "Poland": "🇵🇱",
    "Чехия": "🇨🇿",
    "Czech Republic": "🇨🇿",
    "Czechia": "🇨🇿",
    "Словакия": "🇸🇰",
    "Slovakia": "🇸🇰",
    "Венгрия": "🇭🇺",
    "Hungary": "🇭🇺",
    "Румыния": "🇷🇴",
    "Romania": "🇷🇴",
    "Болгария": "🇧🇬",
    "Bulgaria": "🇧🇬",
    "Сербия": "🇷🇸",
    "Serbia": "🇷🇸",
    "Хорватия": "🇭🇷",
    "Croatia": "🇭🇷",
    "Словения": "🇸🇮",
    "Slovenia": "🇸🇮",
    "Австрия": "🇦🇹",
    "Austria": "🇦🇹",
    "Швейцария": "🇨🇭",
    "Switzerland": "🇨🇭",
    "Нидерланды": "🇳🇱",
    "Netherlands": "🇳🇱",
    "Бельгия": "🇧🇪",
    "Belgium": "🇧🇪",
    "Греция": "🇬🇷",
    "Greece": "🇬🇷",
    "Швеция": "🇸🇪",
    "Sweden": "🇸🇪",
    "Норвегия": "🇳🇴",
    "Norway": "🇳🇴",
    "Финляндия": "🇫🇮",
    "Finland": "🇫🇮",
    "Дания": "🇩🇰",
    "Denmark": "🇩🇰",
    "Ирландия": "🇮🇪",
    "Ireland": "🇮🇪",

    # UK
    "Великобритания": "🇬🇧",
    "United Kingdom": "🇬🇧",
    "UK": "🇬🇧",
    "England": "🇬🇧",
    "Англия": "🇬🇧",

    # America
    "США": "🇺🇸",
    "Соединенные Штаты": "🇺🇸",
    "Соединённые Штаты": "🇺🇸",
    "Соединенные Штаты Америки": "🇺🇸",
    "Соединённые Штаты Америки": "🇺🇸",
    "United States": "🇺🇸",
    "United States of America": "🇺🇸",
    "USA": "🇺🇸",
    "US": "🇺🇸",
    "U.S.": "🇺🇸",
    "U.S.A.": "🇺🇸",
    "Канада": "🇨🇦",
    "Canada": "🇨🇦",
    "Мексика": "🇲🇽",
    "Mexico": "🇲🇽",
    "Бразилия": "🇧🇷",
    "Brazil": "🇧🇷",
    "Аргентина": "🇦🇷",
    "Argentina": "🇦🇷",
    "Чили": "🇨🇱",
    "Chile": "🇨🇱",
    "Колумбия": "🇨🇴",
    "Colombia": "🇨🇴",
    "Перу": "🇵🇪",
    "Peru": "🇵🇪",

    # Asia
    "Китай": "🇨🇳",
    "China": "🇨🇳",
    "Япония": "🇯🇵",
    "Japan": "🇯🇵",
    "Южная Корея": "🇰🇷",
    "South Korea": "🇰🇷",
    "Корея": "🇰🇷",
    "Republic of Korea": "🇰🇷",
    "Индия": "🇮🇳",
    "India": "🇮🇳",
    "Пакистан": "🇵🇰",
    "Pakistan": "🇵🇰",
    "Таиланд": "🇹🇭",
    "Thailand": "🇹🇭",
    "Вьетнам": "🇻🇳",
    "Vietnam": "🇻🇳",
    "Индонезия": "🇮🇩",
    "Indonesia": "🇮🇩",
    "Малайзия": "🇲🇾",
    "Malaysia": "🇲🇾",
    "Сингапур": "🇸🇬",
    "Singapore": "🇸🇬",
    "Филиппины": "🇵🇭",
    "Philippines": "🇵🇭",
    "Монголия": "🇲🇳",
    "Mongolia": "🇲🇳",

    # Middle East
    "ОАЭ": "🇦🇪",
    "UAE": "🇦🇪",
    "United Arab Emirates": "🇦🇪",
    "Израиль": "🇮🇱",
    "Israel": "🇮🇱",
    "Иран": "🇮🇷",
    "Iran": "🇮🇷",
    "Саудовская Аравия": "🇸🇦",
    "Saudi Arabia": "🇸🇦",
    "Катар": "🇶🇦",
    "Qatar": "🇶🇦",

    # Oceania
    "Австралия": "🇦🇺",
    "Australia": "🇦🇺",
    "Новая Зеландия": "🇳🇿",
    "New Zealand": "🇳🇿",

    # Africa (popular)
    "Египет": "🇪🇬",
    "Egypt": "🇪🇬",
    "Марокко": "🇲🇦",
    "Morocco": "🇲🇦",
    "Тунис": "🇹🇳",
    "Tunisia": "🇹🇳",
    "ЮАР": "🇿🇦",
    "South Africa": "🇿🇦",
}


FLAGS: dict[str, str] = {_key(k): v for k, v in _FLAGS_RAW.items()}

# Temporary display names for 2-letter ISO country codes.
# (We'll add profile language later.)
_CODE_DISPLAY: dict[str, str] = {
    "RU": "Россия",
    "US": "USA",
    "UA": "Украина",
    "BY": "Беларусь",
    "KZ": "Казахстан",

    "ES": "Spain",
    "FR": "France",
    "IT": "Italy",
    "DE": "Germany",
    "GB": "UK",
    "PL": "Poland",
    "PT": "Portugal",

    "CN": "China",
    "JP": "Japan",
    "KR": "South Korea",
    "IN": "India",
    "TR": "Turkey",
    "AE": "UAE",
}


def country_code_to_flag(code: str, default: str = "📍") -> str:
    """Convert 2-letter ISO country code to a flag emoji."""
    c = _norm_spaces(code or "").upper()
    if not re.fullmatch(r"[A-Z]{2}", c):
        return default
    base = 0x1F1E6
    return chr(base + (ord(c[0]) - 65)) + chr(base + (ord(c[1]) - 65))


def country_display(value: str) -> str:
    """Return a nice display name for a stored country value.

    If value is a 2-letter code -> use _CODE_DISPLAY (fallback to the code).
    Otherwise return the value as-is.
    """
    v = _norm_spaces(value or "")
    if not v:
        return ""
    if re.fullmatch(r"[A-Za-z]{2}", v):
        return _CODE_DISPLAY.get(v.upper(), v.upper())
    return v


def country_to_flag(country: str, default: str = "📍") -> str:
    """Return flag emoji for a stored country value (2-letter code or a name)."""
    c = _norm_spaces(country or "")
    if not c:
        return default

    # If it's a 2-letter ISO code, compute the flag directly
    if re.fullmatch(r"[A-Za-z]{2}", c):
        return country_code_to_flag(c, default=default)

    # Otherwise try to match by name
    return FLAGS.get(_key(c), default)