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
