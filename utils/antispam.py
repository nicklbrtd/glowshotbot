"""Простая in-memory защита от спама нажатиями.

Используем монотонное время и словарь { (user_id, action): last_ts }.
Не хранит ничего в БД, подходит для single-process бота.
"""

from time import monotonic

_LAST_HIT: dict[tuple[int, str], float] = {}


def should_throttle(user_id: int, action: str, min_interval: float = 1.0) -> bool:
    """Возвращает True, если между нажатиями прошло меньше min_interval секунд."""
    key = (int(user_id), str(action))
    now = monotonic()
    last = _LAST_HIT.get(key)
    if last is not None and (now - last) < min_interval:
        return True
    _LAST_HIT[key] = now
    return False

