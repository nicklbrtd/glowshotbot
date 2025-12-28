"""User ranks (tiers) for GlowShot.

This module is intentionally DB-agnostic.
It defines:
- Rank tiers (code, title, emoji)
- Thresholds (points -> rank)
- Helpers to pick a rank from points and format it

The DB layer should calculate "rank_points" (int) and then use this module
for mapping points -> tier.

Why points?
- Stable across UI changes
- Easy to cache in users table
- Allows tuning thresholds without touching analytics queries
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class Rank:
    """A user tier (localized via i18n)."""

    code: str
    i18n_key: str
    emoji: str

    def label(self, lang: str = "ru") -> str:
        """Human label for UI using i18n."""
        # local import to avoid heavy imports at module import time
        from utils.i18n import t

        l = (lang or "ru").strip().lower().split("-")[0]
        try:
            title = t(self.i18n_key, l)
        except Exception:
            # Safe fallback
            title = self.code
        return f"{self.emoji} {title}".strip()


# --- Default tiers ---
RANK_BEGINNER = Rank(code="beginner", i18n_key="rank.beginner", emoji="🟢")
RANK_AMATEUR = Rank(code="amateur", i18n_key="rank.amateur", emoji="🔵")
RANK_EXPERT = Rank(code="expert", i18n_key="rank.expert", emoji="🟣")

DEFAULT_RANKS: tuple[Rank, ...] = (
    RANK_BEGINNER,
    RANK_AMATEUR,
    RANK_EXPERT,
)


# --- Default thresholds ---
# Meaning: points >= threshold -> that rank
# IMPORTANT: thresholds must be ascending.
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
    """Pick rank tier based on points.

    Args:
        points: rank points (int). None/negative treated as 0.
        thresholds: iterable of (min_points, Rank), ascending.

    Returns:
        Rank for the given points.
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
    """Return a short UI string like '🟣 Expert' / '🟣 Эксперт'."""
    return rank_from_points(points, thresholds=thresholds).label(lang)


def thresholds_from_mapping(mapping: Mapping[str, int], ranks: Iterable[Rank] = DEFAULT_RANKS) -> list[tuple[int, Rank]]:
    """Build thresholds from a {rank_code: min_points} mapping.

    Example:
        thresholds_from_mapping({"beginner": 0, "amateur": 150, "expert": 300})

    Unknown codes are ignored.
    """

    by_code = {r.code: r for r in ranks}
    out: list[tuple[int, Rank]] = []
    for code, pts in mapping.items():
        r = by_code.get(str(code))
        if not r:
            continue
        out.append((int(pts), r))
    return _normalize_thresholds(out)


# --- Optional: points model helper (pure math) ---

def photo_points(*, bayes_score: float | None, ratings_count: int | None) -> float:
    """Compute contribution of a single photo to rank points.

    This is DB-agnostic pure math. The DB layer can sum this across last N photos.

    Strategy:
      points = bayes_score * log1p(ratings_count)

    - Requires both a score and some ratings.
    - Heavily downweights tiny rating counts.

    Returns:
        float points contribution (>=0).
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
    """Convert float points to stable int for storage/caching."""
    try:
        p = float(points or 0.0)
    except Exception:
        p = 0.0
    if p < 0:
        p = 0.0
    return int(round(p))
