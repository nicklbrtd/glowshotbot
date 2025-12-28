from __future__ import annotations

from typing import Any

from database_results import (
    PERIOD_DAY,
    SCOPE_GLOBAL,
    KIND_TOP_PHOTOS,
    ensure_results_schema,
    upsert_results_items,
)

from services.results_queries import get_global_mean_and_count, get_day_photo_rows_global
from services.results_scoring import rules_for_scope, pick_top_photos


try:
    from database import _assert_pool
except Exception:  # pragma: no cover
    _assert_pool = None  # type: ignore


def _pool() -> Any:
    if _assert_pool is None:
        raise RuntimeError("DB pool is not available: cannot import _assert_pool from database.py")
    return _assert_pool()


async def recalc_day_global(*, day_key: str, limit: int = 10) -> int:
    """
    Compute and cache GLOBAL day top photos into results_v2.

    Bayesian score is NOT stored during rating.
    We compute it here from ratings aggregates (sum/count) + global mean.
    """
    await ensure_results_schema()

    p = _pool()
    async with p.acquire() as conn:
        global_mean, _cnt = await get_global_mean_and_count(conn)
        rows = await get_day_photo_rows_global(conn, day_key=str(day_key))

    rules = rules_for_scope(SCOPE_GLOBAL)  # -> min 5 ratings, etc.
    top = pick_top_photos(rows, global_mean=float(global_mean), rules=rules, limit=int(limit))

    items: list[dict] = []
    for idx, r in enumerate(top, start=1):
        items.append(
            {
                "place": idx,
                "photo_id": int(r["photo_id"]),
                "user_id": int(r["user_id"]),
                "score": float(r.get("bayes_score") or 0.0),
                "payload": {
                    # Rendering-ready fields for handlers/results.py
                    "photo_id": int(r["photo_id"]),
                    "file_id": r.get("file_id"),
                    "title": r.get("title") or "Без названия",
                    "avg_rating": r.get("avg_rating"),
                    "ratings_count": int(r.get("ratings_count") or 0),
                    "rated_users": int(r.get("rated_users") or 0),
                    "user_name": r.get("user_name") or "",
                    "user_username": r.get("user_username"),
                    "comments_count": int(r.get("comments_count") or 0),
                    "super_count": int(r.get("super_count") or 0),
                },
            }
        )

    await upsert_results_items(
        period=PERIOD_DAY,
        period_key=str(day_key),
        scope_type=SCOPE_GLOBAL,
        scope_key="global",
        kind=KIND_TOP_PHOTOS,
        items=items,
    )

    return len(items)


from database_results import SCOPE_CITY, SCOPE_COUNTRY
from services.results_queries import (
    get_day_photo_rows_city,
    get_day_photo_rows_country,
    count_active_authors_city,
    count_active_authors_country,
)

async def recalc_day_city(*, day_key: str, city: str, limit: int = 10) -> int:
    await ensure_results_schema()

    p = _pool()
    async with p.acquire() as conn:
        pop = await count_active_authors_city(conn, city=str(city))
        if int(pop) < 5:
            return 0

        global_mean, _ = await get_global_mean_and_count(conn)
        rows = await get_day_photo_rows_city(conn, day_key=str(day_key), city=str(city))

    rules = rules_for_scope(SCOPE_CITY)
    top = pick_top_photos(rows, global_mean=float(global_mean), rules=rules, limit=int(limit))

    items: list[dict] = []
    for idx, r in enumerate(top, start=1):
        items.append(
            {
                "place": idx,
                "photo_id": int(r["photo_id"]),
                "user_id": int(r["user_id"]),
                "score": float(r.get("bayes_score") or 0.0),
                "payload": {
                    "photo_id": int(r["photo_id"]),
                    "file_id": r.get("file_id"),
                    "title": r.get("title") or "Без названия",
                    "avg_rating": r.get("avg_rating"),
                    "ratings_count": int(r.get("ratings_count") or 0),
                    "rated_users": int(r.get("rated_users") or 0),
                    "user_name": r.get("user_name") or "",
                    "user_username": r.get("user_username"),
                    "comments_count": int(r.get("comments_count") or 0),
                    "super_count": int(r.get("super_count") or 0),
                },
            }
        )

    await upsert_results_items(
        period=PERIOD_DAY,
        period_key=str(day_key),
        scope_type=SCOPE_CITY,
        scope_key=str(city),
        kind=KIND_TOP_PHOTOS,
        items=items,
    )
    return len(items)


async def recalc_day_country(*, day_key: str, country: str, limit: int = 10) -> int:
    await ensure_results_schema()

    p = _pool()
    async with p.acquire() as conn:
        pop = await count_active_authors_country(conn, country=str(country))
        if int(pop) < 100:
            return 0

        global_mean, _ = await get_global_mean_and_count(conn)
        rows = await get_day_photo_rows_country(conn, day_key=str(day_key), country=str(country))

    rules = rules_for_scope(SCOPE_COUNTRY)
    top = pick_top_photos(rows, global_mean=float(global_mean), rules=rules, limit=int(limit))

    items: list[dict] = []
    for idx, r in enumerate(top, start=1):
        items.append(
            {
                "place": idx,
                "photo_id": int(r["photo_id"]),
                "user_id": int(r["user_id"]),
                "score": float(r.get("bayes_score") or 0.0),
                "payload": {
                    "photo_id": int(r["photo_id"]),
                    "file_id": r.get("file_id"),
                    "title": r.get("title") or "Без названия",
                    "avg_rating": r.get("avg_rating"),
                    "ratings_count": int(r.get("ratings_count") or 0),
                    "rated_users": int(r.get("rated_users") or 0),
                    "user_name": r.get("user_name") or "",
                    "user_username": r.get("user_username"),
                    "comments_count": int(r.get("comments_count") or 0),
                    "super_count": int(r.get("super_count") or 0),
                },
            }
        )

    await upsert_results_items(
        period=PERIOD_DAY,
        period_key=str(day_key),
        scope_type=SCOPE_COUNTRY,
        scope_key=str(country),
        kind=KIND_TOP_PHOTOS,
        items=items,
    )
    return len(items)