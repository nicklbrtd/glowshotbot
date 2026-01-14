"""Ранги пользователей (тиеры) для GlowShot.

Модуль намеренно не зависит от БД.
Задаёт:
- уровни рангов (код, заголовок, эмодзи);
- пороги (очки -> ранг);
- хелперы для выбора ранга по очкам и форматирования.

Слой БД должен вычислять "rank_points" (int) и использовать этот модуль
для сопоставления очков -> рангу.

Почему очки?
- стабильны при изменениях интерфейса;
- легко кешировать в таблице пользователей;
- позволяют настраивать пороги без правки аналитических запросов.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class Rank:
    """Уровень пользователя (локализуется через i18n)."""

    code: str
    i18n_key: str
    emoji: str

    def label(self, lang: str = "ru") -> str:
        """Человекочитаемый текст для интерфейса через i18n."""
        # local import to avoid heavy imports at module import time
        from utils.i18n import t

        l = (lang or "ru").strip().lower().split("-")[0]
        try:
            title = t(self.i18n_key, l)
        except Exception:
            # Safe fallback
            title = self.code
        return f"{self.emoji} {title}".strip()


# --- Базовые ранги ---
RANK_BEGINNER = Rank(code="beginner", i18n_key="rank.beginner", emoji="🟢")
RANK_AMATEUR = Rank(code="amateur", i18n_key="rank.amateur", emoji="🔵")
RANK_EXPERT = Rank(code="expert", i18n_key="rank.expert", emoji="🟣")

DEFAULT_RANKS: tuple[Rank, ...] = (
    RANK_BEGINNER,
    RANK_AMATEUR,
    RANK_EXPERT,
)


# --- Базовые пороги ---
# Смысл: points >= threshold -> этот ранг
# Важно: пороги должны быть по возрастанию.
DEFAULT_THRESHOLDS: tuple[tuple[int, Rank], ...] = (
    (0, RANK_BEGINNER),
    (120, RANK_AMATEUR),
    (260, RANK_EXPERT),
)


def _normalize_thresholds(thresholds: Iterable[tuple[int, Rank]]) -> list[tuple[int, Rank]]:
    items = sorted(((int(p), r) for p, r in thresholds), key=lambda x: x[0])
    if not items:
        return [(0, RANK_BEGINNER)]

    # Ensure first threshold starts at 0
    if items[0][0] != 0:
        items.insert(0, (0, items[0][1]))

    # Remove duplicates by keeping the last rank for the same point threshold
    out: list[tuple[int, Rank]] = []
    for p, r in items:
        if out and out[-1][0] == p:
            out[-1] = (p, r)
        else:
            out.append((p, r))
    return out


def rank_from_points(points: int | None, thresholds: Iterable[tuple[int, Rank]] = DEFAULT_THRESHOLDS) -> Rank:
    """Выбрать уровень ранга по количеству очков.

    Args:
        points: очки ранга (int). None/отрицательные считаются как 0.
        thresholds: итерируемый (min_points, Rank) по возрастанию.

    Returns:
        Rank для переданных очков.
    """

    pts = int(points or 0)
    if pts < 0:
        pts = 0

    th = _normalize_thresholds(thresholds)

    current = th[0][1]
    for min_pts, r in th:
        if pts >= min_pts:
            current = r
        else:
            break
    return current


def format_rank(
    points: int | None,
    thresholds: Iterable[tuple[int, Rank]] = DEFAULT_THRESHOLDS,
    lang: str = "ru",
) -> str:
    """Вернёт короткий текст для интерфейса вроде '🟣 Expert' / '🟣 Эксперт'."""
    return rank_from_points(points, thresholds=thresholds).label(lang)


def rank_progress_bar(points: int | None, thresholds: Iterable[tuple[int, Rank]] = DEFAULT_THRESHOLDS, segments: int = 5) -> str:
    """
    Текстовая полоска прогресса ранга вида «▓▓▓░░».
    - points: текущие очки ранга
    - thresholds: список (min_points, Rank) по возрастанию
    - segments: всего сегментов (визуально 5)
    Правила: минимум 1 заполненный сегмент, максимум segments-1, если есть следующий ранг.
    Для максимального ранга возвращает все сегменты заполненными.
    """
    segs = max(int(segments or 5), 2)
    pts = max(int(points or 0), 0)
    th = _normalize_thresholds(thresholds)

    current_min = th[0][0]
    next_min: int | None = None
    for idx, (min_pts, _rank) in enumerate(th):
        if pts >= min_pts:
            current_min = min_pts
            if idx + 1 < len(th):
                next_min = th[idx + 1][0]
        else:
            break

    if next_min is None:
        # Уже на максимальном ранге — показываем полную полоску
        return "▓" * segs

    span = max(next_min - current_min, 1)
    rel = (pts - current_min) / span
    filled = int(round(rel * segs))
    filled = max(1, min(segs - 1, filled))
    empty = max(segs - filled, 0)
    return ("▓" * filled) + ("░" * empty)


def thresholds_from_mapping(mapping: Mapping[str, int], ranks: Iterable[Rank] = DEFAULT_RANKS) -> list[tuple[int, Rank]]:
    """Построить пороги из отображения {rank_code: min_points}.

    Example:
        thresholds_from_mapping({"beginner": 0, "amateur": 150, "expert": 300})

    Неизвестные коды игнорируются.
    """

    by_code = {r.code: r for r in ranks}
    out: list[tuple[int, Rank]] = []
    for code, pts in mapping.items():
        r = by_code.get(str(code))
        if not r:
            continue
        out.append((int(pts), r))
    return _normalize_thresholds(out)


# --- Необязательно: помощник для расчёта очков (чистая математика) ---

def photo_points(*, bayes_score: float | None, ratings_count: int | None) -> float:
    """Считаем вклад одной фотографии в очки ранга.

    Это чистая математика, не завязанная на БД. Слой БД может суммировать результат по последним N фото.

    Стратегия:
      points = bayes_score * log1p(ratings_count)

    - Нужен и счёт, и ненулевое число оценок.
    - Сильно занижает вклад при малом числе оценок.

    Returns:
        float — вклад в очки (>=0).
    """

    if bayes_score is None:
        return 0.0
    try:
        score = float(bayes_score)
    except Exception:
        return 0.0

    n = int(ratings_count or 0)
    if n <= 0:
        return 0.0

    # local import to keep module lightweight
    import math

    return max(0.0, score) * math.log1p(max(0, n))


def points_to_int(points: float | None) -> int:
    """Преобразовать float-очки в стабильный int для хранения/кеша."""
    try:
        p = float(points or 0.0)
    except Exception:
        p = 0.0
    if p < 0:
        p = 0.0
    return int(round(p))


# --- Вспомогательные бонусы/штрафы для активности ---

def ratings_activity_points(effective_ratings: int, *, weight: float = 0.4) -> float:
    """
    Небольшой стабильный бонус за оценки других фото.
    - effective_ratings: уже ограниченный антиспам-капом объём.
    """
    return weight * math.sqrt(max(0, effective_ratings))


def comments_activity_points(effective_comments: int, *, weight: float = 0.6) -> float:
    """
    Бонус за осмысленные комментарии (после фильтра длины и дневного лимита).
    """
    return weight * math.sqrt(max(0, effective_comments))


def reports_penalty(resolved_reports: int, *, weight: float = 6.0, cap: float = 80.0) -> float:
    """
    Мягкий штраф за подтверждённые жалобы.
    - weight: сколько очков снимаем за каждую.
    - cap: максимальный суммарный штраф.
    """
    penalty = max(0, resolved_reports) * weight
    return min(penalty, cap)


def streak_bonus_points(streak_days: int, *, weight: float = 0.4, cap_days: int = 30) -> float:
    """
    Небольшой бонус за серию активности (streak).
    Ограничен по дням, чтобы не доминировать над качеством фото.
    """
    return weight * min(max(0, streak_days), cap_days)
