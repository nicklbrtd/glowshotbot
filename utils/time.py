from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

from config import BOT_TIMEZONE, HAPPY_HOUR_START, HAPPY_HOUR_END


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(BOT_TIMEZONE)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def get_bot_now() -> datetime:
    """Возвращает timezone-aware datetime в таймзоне бота."""
    return datetime.now(tz=_tz())


def get_moscow_now() -> datetime:  # backward compat
    return get_bot_now()


def get_bot_today() -> date:
    return get_bot_now().date()


def get_moscow_today() -> str:  # backward compat, keeps str for existing code
    return get_bot_today().isoformat()


def get_bot_now_iso() -> str:
    return get_bot_now().isoformat()


def get_moscow_now_iso() -> str:  # backward compat
    return get_bot_now_iso()


def end_of_day(dt: date) -> datetime:
    """Конец дня в таймзоне бота."""
    return datetime.combine(dt, time(23, 59, 59, 999999), tzinfo=_tz())


def is_happy_hour(ts: datetime | None = None) -> bool:
    now = ts or get_bot_now()
    local = now.astimezone(_tz()).time()
    start = HAPPY_HOUR_START
    end = HAPPY_HOUR_END
    if start <= end:
        return start <= local < end
    # ночь через полночь
    return local >= start or local < end


def today_key() -> str:
    return get_bot_today().isoformat()


_MONTHS_RU_SHORT = {
    1: "янв",
    2: "фев",
    3: "мар",
    4: "апр",
    5: "май",
    6: "июн",
    7: "июл",
    8: "авг",
    9: "сен",
    10: "окт",
    11: "ноя",
    12: "дек",
}

_MONTHS_EN_SHORT = {
    1: "Jan",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dec",
}


def _parse_iso_day(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def format_day_short(submit_day: date | str | None, lang: str = "ru") -> str:
    """Compact day label like '18 фев'."""
    d = _parse_iso_day(submit_day)
    if d is None:
        return "—"
    months = _MONTHS_EN_SHORT if str(lang or "ru").startswith("en") else _MONTHS_RU_SHORT
    return f"{d.day} {months.get(d.month, d.month)}"


def format_party_id(submit_day: date | str | None, include_year_if_needed: bool = True) -> str:
    """
    Return compact party id:
      2026-02-18 -> #218
      2026-01-31 -> #131
      2026-11-05 -> #1105
    """
    d = _parse_iso_day(submit_day)
    if d is None:
        return "#—"
    base = f"#{d.month}{d.day:02d}"
    if not include_year_if_needed:
        return base
    current_year = get_bot_today().year
    if d.year == current_year:
        return base
    return f"{base}/{str(d.year)[-2:]}"


def format_party_label(submit_day: date | str | None, lang: str = "ru", mode: str = "full") -> str:
    """
    full: "Партия #218 · 18 фев"
    short: "#218"
    """
    mode_safe = str(mode or "full").lower()
    pid = format_party_id(submit_day, include_year_if_needed=True)
    if mode_safe == "short":
        return pid
    day_short = format_day_short(submit_day, lang=lang)
    if str(lang or "ru").startswith("en"):
        return f"Party {pid} · {day_short}"
    return f"Партия {pid} · {day_short}"
