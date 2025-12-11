

"""Логика модерации и жалоб на фотографии.

Здесь собираем все константы и функции, связанные с жалобами и статусами
фотографий, чтобы не разбрасывать эту логику по разным хендлерам.

Идея:
- Пользователь может пожаловаться на фото, выбрав причину:
  - селфи
  - порнография / 18+
  - пропаганда / насилие
  - другое (с текстом)
- Когда фото набирает N жалоб (порог), оно автоматически уходит «на проверку»:
  - фото временно не показывается в выдаче для оценивания;
  - администратору прилетает пуш с подробностями и кнопками:
    «✅ Всё хорошо» и «⛔ Отключить».
- При нажатии админ-кнопок хендлер должен:
  - пометить жалобы как обработанные (ок / заблокировано);
  - если фото «отключено» — больше не показывать его в оценке;
  - опционально забанить автору загрузку новых работ на сутки.

Этот модуль НЕ привязан к aiogram. Он содержит только бизнес-логику и
константы, которые можно вызывать из хендлеров (`handlers/rate.py`,
`handlers/admin.py` и т.д.).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, Sequence


# ---- Причины жалоб ----

ReportReason = Literal["selfie", "porn", "propaganda", "other"]

REPORT_REASON_LABELS: Final[dict[ReportReason, str]] = {
    "selfie": "Селфи / портрет автора",
    "porn": "Порнография / 18+ контент",
    "propaganda": "Пропаганда / насилие / разжигание",
    "other": "Другое",
}


def get_report_reasons() -> Sequence[ReportReason]:
    return ("selfie", "porn", "propaganda", "other")


# ---- Порог модерации ----

REPORT_THRESHOLD: Final[int] = 3


@dataclass(slots=True)
class ReportStats:
    photo_id: int
    total_pending: int      
    total_all: int


@dataclass(slots=True)
class ModerationDecision:
    should_mark_under_review: bool 
    reached_threshold: bool       

def decide_after_new_report(stats: ReportStats) -> ModerationDecision:
    reached = stats.total_pending >= REPORT_THRESHOLD
    return ModerationDecision(
        should_mark_under_review=reached,
        reached_threshold=reached,
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
    "UploadBan",
    "get_one_day_ban_until",
]