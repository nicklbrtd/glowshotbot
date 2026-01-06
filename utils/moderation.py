"""Логика модерации и жалоб на фотографии.

Здесь собраны все константы и функции, связанные с жалобами и статусами
фотографий, чтобы не разбрасывать эту логику по разным хендлерам.

Идея:
- Пользователь может пожаловаться на фото, выбрав одну из причин:
  - селфи / портрет автора
  - порнография / 18+ контент
  - чужая фотография / ворованный контент
  - пропаганда
  - сцены насилия
  - разжигание ненависти
  - незаконная реклама / запрещённые товары и услуги
  - другое
- Когда фото набирает N жалоб (порог), оно автоматически уходит «на проверку»:
  - при достижении порога N жалоб фото помечается «на проверку» и перестаёт показываться в обычной выдаче для оценивания;
  - модераторы увидят такие кадры в своём разделе «Модерация жалоб» (реализуется в хендлерах), где уже будут кнопки вроде «✅ Всё хорошо» / «⛔ Отключить»;
  - сам модуль только считает пороги и принимает решение `should_mark_under_review`, без отправки пушей.

Этот модуль НЕ привязан к aiogram. Он содержит только бизнес-логику и
константы, которые можно вызывать из хендлеров (`handlers/rate.py`,
`handlers/admin.py` и т.д.).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, Sequence

from utils.time import get_moscow_now


# ---- Причины жалоб ----

ReportReason = Literal[
    "selfie",          # селфи / портрет автора
    "porn",            # порнография / 18+
    "stolen",          # чужое фото / ворованный контент
    "propaganda",      # пропаганда
    "violence",        # сцены насилия
    "hate",            # разжигание ненависти
    "illegal_ads",     # незаконная реклама / запрещённые товары и услуги
    "other",           # другое
]

REPORT_REASON_LABELS: Final[dict[ReportReason, str]] = {
    "selfie": "🤳 Селфи / Портрет автора",
    "porn": "🔞 18+ контент",
    "stolen": "🖼️ Украденная фотография",
    "propaganda": "📢 Пропаганда",
    "violence": "💣 Непреемлемый контент",
    "hate": "🔥 Разжигание ненависти",
    "illegal_ads": "🚫 Реклама",
    "other": "📝 Другое",
}


def get_report_reasons() -> Sequence[ReportReason]:
    return (
        "selfie",
        "porn",
        "stolen",
        "propaganda",
        "violence",
        "hate",
        "illegal_ads",
        "other",
    )


# ---- Порог модерации ----

REPORT_THRESHOLD: Final[int] = 1
# Порог количества активных жалоб, после которого фото считается требующим модерации
# и должно быть скрыто из выдачи для обычных пользователей.
# Отправка уведомлений и показ в интерфейсе модератору реализуются в хендлерах, а не здесь.

# ---- Ограничение частоты жалоб ----

REPORT_RATE_LIMIT_MAX: Final[int] = 2
REPORT_RATE_LIMIT_WINDOW_MINUTES: Final[int] = 20
REPORT_RATE_LIMIT_WINDOW: Final[timedelta] = timedelta(minutes=REPORT_RATE_LIMIT_WINDOW_MINUTES)


@dataclass(slots=True)
class ReportStats:
    """
    Статистика по жалобам на фото:
    - всего жалоб (total_all);
    - активных (ожидающих решения) жалоб (total_pending).
    Используется для подсчёта и принятия решения, помечать ли фото «на проверке».
    """
    photo_id: int
    total_pending: int
    total_all: int


@dataclass(slots=True)
class ModerationDecision:
    """
    Решение по результатам подсчёта жалоб:
    - should_mark_under_review: помечать ли фото «на проверке» (не показывать в выдаче);
    - reached_threshold: достигнут ли порог жалоб.
    """
    should_mark_under_review: bool
    reached_threshold: bool

def decide_after_new_report(stats: ReportStats) -> ModerationDecision:
    reached = stats.total_pending >= REPORT_THRESHOLD
    return ModerationDecision(
        should_mark_under_review=reached,
        reached_threshold=reached,
    )


@dataclass(slots=True)
class ReportRateLimitStatus:
    """Результат проверки лимита жалоб."""
    allowed: bool
    retry_after_seconds: int
    remaining_quota: int


def evaluate_report_rate_limit(
    reports_created_at: Sequence[datetime | str],
    now: datetime | None = None,
) -> ReportRateLimitStatus:
    """
    Проверяет, можно ли отправить жалобу с учётом лимита REPORT_RATE_LIMIT_MAX за REPORT_RATE_LIMIT_WINDOW.

    reports_created_at — список меток времени (datetime или ISO-строка) последних жалоб пользователя.
    Возвращает статус: можно/нельзя, сколько осталось до разблокировки, сколько жалоб осталось в окне.
    """
    if now is None:
        now = get_moscow_now()

    window_start = now - REPORT_RATE_LIMIT_WINDOW

    parsed: list[datetime] = []
    for raw in reports_created_at:
        try:
            if isinstance(raw, datetime):
                dt = raw
            else:
                dt = datetime.fromisoformat(str(raw))
        except Exception:
            continue
        parsed.append(dt)

    recent = [dt for dt in parsed if dt >= window_start]
    recent.sort(reverse=True)

    if len(recent) < REPORT_RATE_LIMIT_MAX:
        return ReportRateLimitStatus(
            allowed=True,
            retry_after_seconds=0,
            remaining_quota=REPORT_RATE_LIMIT_MAX - len(recent),
        )

    boundary = recent[REPORT_RATE_LIMIT_MAX - 1]
    retry_after = int((boundary + REPORT_RATE_LIMIT_WINDOW - now).total_seconds())
    if retry_after < 0:
        retry_after = 0

    return ReportRateLimitStatus(
        allowed=False,
        retry_after_seconds=retry_after,
        remaining_quota=0,
    )


# ---- Баны на загрузку новых работ ----

@dataclass(slots=True)
class UploadBan:

    user_id: int
    banned_until: datetime

    @property
    def is_active(self) -> bool:
        return datetime.utcnow() < self.banned_until


def get_one_day_ban_until(now: datetime | None = None) -> datetime:
    if now is None:
        now = datetime.utcnow()
    return now + timedelta(days=1)


__all__ = [
    "ReportReason",
    "REPORT_REASON_LABELS",
    "get_report_reasons",
    "REPORT_THRESHOLD",
    "ReportStats",
    "ModerationDecision",
    "decide_after_new_report",
    "REPORT_RATE_LIMIT_MAX",
    "REPORT_RATE_LIMIT_WINDOW",
    "REPORT_RATE_LIMIT_WINDOW_MINUTES",
    "ReportRateLimitStatus",
    "evaluate_report_rate_limit",
    "UploadBan",
    "get_one_day_ban_until",
]
