from __future__ import annotations

"""
LEGACY MODULE.

Scoring helpers below are currently used only by the legacy results_engine path.
Current production results flow relies on database_results.py aggregations.
"""

from dataclasses import dataclass
from typing import Iterable
from config import (
    RESULTS_BAYES_PRIOR,
    RESULTS_MIN_RATINGS_COMMON,
    RESULTS_MIN_UNIQUE_RATERS_COMMON,
    RESULTS_MIN_RATINGS_CITY,
    RESULTS_MIN_UNIQUE_RATERS_CITY,
    RESULTS_MIN_RATINGS_COUNTRY,
    RESULTS_MIN_UNIQUE_RATERS_COUNTRY,
    RESULTS_MIN_RATINGS_TAG,
    RESULTS_MIN_UNIQUE_RATERS_TAG,
)


@dataclass(frozen=True)
class ScoringRules:
    """Rules for eligibility + Bayesian ranking."""

    # Bayesian smoothing prior weight (virtual votes of global mean)
    prior_weight: int

    # Eligibility thresholds
    min_ratings: int
    min_unique_raters: int


def rules_for_scope(scope_type: str) -> ScoringRules:
    """Per-scope thresholds.

    Defaults:
      - global/common results: min 5 ratings
      - city results: min 10 ratings
      - country results: min 25 ratings
      - tag results: min 20 ratings

    Unique raters can be tuned separately.
    """
    prior = int(RESULTS_BAYES_PRIOR)

    # Common/global defaults
    common_min_ratings = int(RESULTS_MIN_RATINGS_COMMON)
    common_min_unique = int(RESULTS_MIN_UNIQUE_RATERS_COMMON)

    # City/Country
    city_min_ratings = int(RESULTS_MIN_RATINGS_CITY)
    city_min_unique = int(RESULTS_MIN_UNIQUE_RATERS_CITY)

    country_min_ratings = int(RESULTS_MIN_RATINGS_COUNTRY)
    country_min_unique = int(RESULTS_MIN_UNIQUE_RATERS_COUNTRY)

    # Tags are stricter
    tag_min_ratings = int(RESULTS_MIN_RATINGS_TAG)
    tag_min_unique = int(RESULTS_MIN_UNIQUE_RATERS_TAG)

    st = (scope_type or "").strip().lower()

    if st in ("tag", "tag_event", "tags"):
        return ScoringRules(prior_weight=prior, min_ratings=tag_min_ratings, min_unique_raters=tag_min_unique)

    if st in ("city",):
        return ScoringRules(prior_weight=prior, min_ratings=city_min_ratings, min_unique_raters=city_min_unique)

    if st in ("country",):
        return ScoringRules(prior_weight=prior, min_ratings=country_min_ratings, min_unique_raters=country_min_unique)

    return ScoringRules(prior_weight=prior, min_ratings=common_min_ratings, min_unique_raters=common_min_unique)


def bayes_score(*, sum_values: float, n: float, global_mean: float, prior: int) -> float | None:
    """Bayesian average for 1..10 ratings."""
    if n <= 0:
        return None
    return (prior * float(global_mean) + float(sum_values)) / (prior + float(n))


def pick_top_photos(
    rows: Iterable[dict],
    *,
    global_mean: float,
    rules: ScoringRules,
    limit: int = 10,
) -> list[dict]:
    """Filter+rank photo rows.

    Expected keys in each row:
      photo_id, user_id, file_id, title, user_name, user_username,
      ratings_count, rated_users, sum_values, avg_rating, created_at

    Sort order:
      1) bayes_score desc
      2) ratings_count desc
      3) created_at asc (stable)
    """
    candidates: list[dict] = []

    for r in rows:
        ratings_count = int(r.get("ratings_count") or 0)
        rated_users = int(r.get("rated_users") or 0)

        if ratings_count < int(rules.min_ratings):
            continue
        if rated_users < int(rules.min_unique_raters):
            continue

        sum_values = float(r.get("sum_values_weighted") or r.get("sum_values") or 0.0)
        weighted_count = r.get("ratings_weighted_count")
        n = float(weighted_count) if weighted_count is not None else float(ratings_count)
        b = bayes_score(
            sum_values=sum_values,
            n=n,
            global_mean=float(global_mean),
            prior=int(rules.prior_weight),
        )
        if b is None:
            continue

        rr = dict(r)
        rr["bayes_score"] = float(b)
        candidates.append(rr)

    candidates.sort(
        key=lambda x: (
            -(float(x.get("bayes_score") or 0.0)),
            -(int(x.get("ratings_count") or 0)),
            str(x.get("created_at") or ""),
        )
    )

    return candidates[: int(limit)]
