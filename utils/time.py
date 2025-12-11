from datetime import datetime, timedelta


def get_moscow_now() -> datetime:
    """
    Текущее время по Москве (UTC+3) как datetime.
    Используется как базовая функция для today/now_iso.
    """
    return datetime.utcnow() + timedelta(hours=3)


def get_moscow_today() -> str:
    """
    Текущая дата по Москве (UTC+3) в формате YYYY-MM-DD.
    Используется как day_key для фотографий и выборки рейтингов за день.
    """
    return get_moscow_now().date().isoformat()


def get_moscow_now_iso() -> str:
    """
    Текущее время по Москве (UTC+3) в ISO-формате.
    Используется для created_at / updated_at и других временных меток.
    """
    return get_moscow_now().isoformat()