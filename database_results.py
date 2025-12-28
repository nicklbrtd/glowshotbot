
from datetime import timedelta
from typing import Any, Iterable

from utils.time import get_moscow_now, get_moscow_today, get_moscow_now_iso


# We reuse the project's asyncpg pool stored in database.py
try:
    from database import _assert_pool
except Exception:  # pragma: no cover
    _assert_pool = None  # type: ignore


# ---- constants for results_v2 ----
PERIOD_DAY = "day"
PERIOD_WEEK = "week"
PERIOD_ALL_TIME = "all_time"

SCOPE_GLOBAL = "global"
SCOPE_CITY = "city"
SCOPE_COUNTRY = "country"
SCOPE_RANK = "rank"
SCOPE_TAG_EVENT = "tag_event"

KIND_TOP_PHOTOS = "top_photos"
KIND_BEST_PHOTO = "best_photo"
KIND_BEST_AUTHOR = "best_author"


def _pool() -> Any:
    if _assert_pool is None:
        raise RuntimeError("DB pool is not available: cannot import _assert_pool from database.py")
    return _assert_pool()


async def ensure_results_schema() -> None:
    """Create results tables if they don't exist. Safe to call on startup."""
    p = _pool()
    async with p.acquire() as conn:
        # cache of per-photo daily metrics (optional for now, but we create it early)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_day_metrics (
                day_key TEXT NOT NULL,
                photo_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                ratings_count INTEGER NOT NULL DEFAULT 0,
                rated_users INTEGER NOT NULL DEFAULT 0,
                avg_rating DOUBLE PRECISION,
                sum_values DOUBLE PRECISION,
                bayes_score DOUBLE PRECISION,
                comments_count INTEGER NOT NULL DEFAULT 0,
                super_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (day_key, photo_id)
            );
            """
        )

        # final cached results
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS results_v2 (
                id BIGSERIAL PRIMARY KEY,
                period TEXT NOT NULL,
                period_key TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                place INTEGER NOT NULL,
                photo_id BIGINT,
                user_id BIGINT,
                score DOUBLE PRECISION,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (period, period_key, scope_type, scope_key, kind, place)
            );
            """
        )

        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_results_v2_lookup
            ON results_v2 (period, period_key, scope_type, scope_key, kind);
            """
        )


async def ensure_results_legacy_schema() -> None:
    """Create legacy results tables (weekly candidates, repeats, my_results)."""
    p = _pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_candidates (
              photo_id BIGINT PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_repeats (
              photo_id BIGINT PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
              used_at TEXT NOT NULL
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS my_results (
              id BIGSERIAL PRIMARY KEY,
              user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              photo_id BIGINT REFERENCES photos(id) ON DELETE SET NULL,
              file_id TEXT NOT NULL,
              title TEXT,
              day_key TEXT,
              kind TEXT,
              place INTEGER,
              avg_rating DOUBLE PRECISION,
              ratings_count INTEGER,
              created_at TEXT NOT NULL
            );
            """
        )


async def clear_results(period: str, period_key: str, scope_type: str, scope_key: str, kind: str) -> None:
    p = _pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM results_v2
            WHERE period=$1 AND period_key=$2 AND scope_type=$3 AND scope_key=$4 AND kind=$5
            """,
            str(period),
            str(period_key),
            str(scope_type),
            str(scope_key),
            str(kind),
        )


async def upsert_results_items(
    *,
    period: str,
    period_key: str,
    scope_type: str,
    scope_key: str,
    kind: str,
    items: Iterable[dict],
) -> None:
    """Upsert a full ordered list of result items. Places are taken from items[i]['place'] or enumerate+1."""
    p = _pool()
    async with p.acquire() as conn:
        await ensure_results_schema()
        # Upsert each row (lists are small: top-10/top-50)
        place_num = 1
        for it in items:
            place = int(it.get("place") or place_num)
            place_num += 1

            photo_id = it.get("photo_id")
            user_id = it.get("user_id")
            score = it.get("score")
            payload = it.get("payload") or {}

            await conn.execute(
                """
                INSERT INTO results_v2
                    (period, period_key, scope_type, scope_key, kind, place, photo_id, user_id, score, payload)
                VALUES
                    ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)
                ON CONFLICT (period, period_key, scope_type, scope_key, kind, place)
                DO UPDATE SET
                    photo_id=EXCLUDED.photo_id,
                    user_id=EXCLUDED.user_id,
                    score=EXCLUDED.score,
                    payload=EXCLUDED.payload,
                    created_at=NOW();
                """,
                str(period),
                str(period_key),
                str(scope_type),
                str(scope_key),
                str(kind),
                int(place),
                int(photo_id) if photo_id is not None else None,
                int(user_id) if user_id is not None else None,
                float(score) if score is not None else None,
                payload,
            )


async def get_results_items(
    *,
    period: str,
    period_key: str,
    scope_type: str,
    scope_key: str,
    kind: str,
    limit: int = 10,
) -> list[dict]:
    p = _pool()
    async with p.acquire() as conn:
        await ensure_results_schema()
        rows = await conn.fetch(
            """
            SELECT place, photo_id, user_id, score, payload
            FROM results_v2
            WHERE period=$1 AND period_key=$2 AND scope_type=$3 AND scope_key=$4 AND kind=$5
            ORDER BY place ASC
            LIMIT $6
            """,
            str(period),
            str(period_key),
            str(scope_type),
            str(scope_key),
            str(kind),
            int(limit),
        )
        return [dict(r) for r in rows]



async def has_results(
    *,
    period: str,
    period_key: str,
    scope_type: str,
    scope_key: str,
    kind: str,
) -> bool:
    p = _pool()
    async with p.acquire() as conn:
        await ensure_results_schema()
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM results_v2
            WHERE period=$1 AND period_key=$2 AND scope_type=$3 AND scope_key=$4 AND kind=$5
            LIMIT 1
            """,
            str(period),
            str(period_key),
            str(scope_type),
            str(scope_key),
            str(kind),
        )
        return row is not None


# Note: legacy results helpers were removed. Results are now served exclusively from results_v2
# via services/results_engine.py (calculation) and handlers/results.py (UI).
