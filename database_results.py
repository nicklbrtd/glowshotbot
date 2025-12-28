
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


# -------------------- legacy results helpers --------------------

async def get_daily_top_photos(day_key: str | None = None, limit: int = 4) -> list[dict]:
    if not day_key:
        day_key = get_moscow_today()
    p = _pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
              p.*,
              u.name     AS user_name,
              u.username AS user_username,
              COUNT(r.id) AS ratings_count,
              AVG(r.value)::double precision AS avg_rating
            FROM photos p
            JOIN ratings r ON r.photo_id = p.id
            JOIN users   u ON u.id = p.user_id
            WHERE p.is_deleted=0
              AND p.day_key = $1
              AND p.moderation_status = 'active'
              AND u.is_deleted = 0
            GROUP BY p.id, u.id
            ORDER BY AVG(r.value) DESC, COUNT(r.id) DESC, p.id DESC
            LIMIT $2
            """,
            str(day_key),
            int(limit),
        )
    return [dict(r) for r in rows]


async def count_users_with_city(city: str) -> int:
    c = (city or "").strip()
    if not c:
        return 0
    p = _pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_deleted=0 AND city=$1", c)
    return int(v or 0)


async def count_users_with_country(country: str) -> int:
    c = (country or "").strip()
    if not c:
        return 0
    p = _pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_deleted=0 AND country=$1", c)
    return int(v or 0)


async def get_daily_top_photos_by_city(day_key: str, city: str, limit: int = 10) -> list[dict]:
    c = (city or "").strip()
    if not day_key or not c:
        return []
    p = _pool()
    query = """
        SELECT
          ph.*,
          u.name     AS user_name,
          u.username AS user_username,
          COUNT(r.id)::int AS ratings_count,
          AVG(r.value)::double precision AS avg_rating
        FROM photos ph
        JOIN users u ON u.id = ph.user_id
        JOIN ratings r ON r.photo_id = ph.id
        WHERE ph.is_deleted=0
          AND ph.moderation_status='active'
          AND u.is_deleted=0
          AND ph.day_key=$1
          AND u.city=$2
        GROUP BY ph.id, u.id
        ORDER BY AVG(r.value) DESC, COUNT(r.id) DESC, ph.id DESC
        LIMIT $3
    """
    async with p.acquire() as conn:
        rows = await conn.fetch(query, str(day_key), c, int(limit))
    return [dict(r) for r in rows]


async def get_daily_top_photos_by_country(day_key: str, country: str, limit: int = 10) -> list[dict]:
    c = (country or "").strip()
    if not day_key or not c:
        return []
    p = _pool()
    query = """
        SELECT
          ph.*,
          u.name     AS user_name,
          u.username AS user_username,
          COUNT(r.id)::int AS ratings_count,
          AVG(r.value)::double precision AS avg_rating
        FROM photos ph
        JOIN users u ON u.id = ph.user_id
        JOIN ratings r ON r.photo_id = ph.id
        WHERE ph.is_deleted=0
          AND ph.moderation_status='active'
          AND u.is_deleted=0
          AND ph.day_key=$1
          AND u.country=$2
        GROUP BY ph.id, u.id
        ORDER BY AVG(r.value) DESC, COUNT(r.id) DESC, ph.id DESC
        LIMIT $3
    """
    async with p.acquire() as conn:
        rows = await conn.fetch(query, str(day_key), c, int(limit))
    return [dict(r) for r in rows]


async def count_active_photos_in_day(day_key: str) -> int:
    """How many active, non-deleted photos exist for a given day_key."""
    p = _pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM photos
            WHERE is_deleted=0
              AND moderation_status='active'
              AND day_key=$1
            """,
            str(day_key),
        )
    return int(v or 0)


async def get_user_best_photo_in_day(user_id: int, day_key: str) -> dict | None:
    """Return user's best photo for a specific day_key (even if it has 0 ratings)."""
    p = _pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH s AS (
              SELECT
                ph.*,
                COALESCE(AVG(r.value), 0)::double precision AS avg_rating,
                COUNT(r.id)::int AS ratings_count
              FROM photos ph
              LEFT JOIN ratings r ON r.photo_id = ph.id
              WHERE ph.user_id=$1
                AND ph.day_key=$2
                AND ph.is_deleted=0
                AND ph.moderation_status='active'
              GROUP BY ph.id
            )
            SELECT *
            FROM s
            ORDER BY avg_rating DESC, ratings_count DESC, created_at ASC, id ASC
            LIMIT 1
            """,
            int(user_id),
            str(day_key),
        )
    return dict(row) if row else None


async def get_weekly_best_photo(start_iso: str | None = None, end_iso: str | None = None) -> dict | None:
    """Best weekly photo within [start_iso, end_iso] (day_key range), Moscow days."""
    p = _pool()

    now = get_moscow_now().date()
    if not start_iso or not end_iso:
        start_iso = (now - timedelta(days=6)).isoformat()
        end_iso = now.isoformat()

    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              ph.*,
              u.name     AS user_name,
              u.username AS user_username,
              COUNT(r.id)::int AS ratings_count,
              AVG(r.value)::double precision AS avg_rating
            FROM photos ph
            JOIN ratings r ON r.photo_id = ph.id
            JOIN users   u ON u.id = ph.user_id
            WHERE ph.is_deleted=0
              AND ph.moderation_status='active'
              AND u.is_deleted=0
              AND ph.day_key >= $1
              AND ph.day_key <= $2
            GROUP BY ph.id, u.id
            HAVING AVG(r.value) >= 9.0
            ORDER BY AVG(r.value) DESC, COUNT(r.id) DESC, ph.id DESC
            LIMIT 1
            """,
            str(start_iso),
            str(end_iso),
        )
    return dict(row) if row else None


async def add_weekly_candidate(photo_id: int) -> None:
    await ensure_results_legacy_schema()
    p = _pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO weekly_candidates (photo_id, created_at)
            VALUES ($1,$2)
            ON CONFLICT (photo_id) DO NOTHING
            """,
            int(photo_id),
            get_moscow_now_iso(),
        )


async def is_photo_in_weekly(photo_id: int) -> bool:
    await ensure_results_legacy_schema()
    p = _pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT 1 FROM weekly_candidates WHERE photo_id=$1", int(photo_id))
    return bool(v)


async def get_weekly_photos_for_user(user_id: int) -> list[dict]:
    await ensure_results_legacy_schema()
    p = _pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.* FROM photos p
            JOIN weekly_candidates w ON w.photo_id=p.id
            WHERE p.user_id=$1 AND p.is_deleted=0
            ORDER BY w.created_at DESC, p.id DESC
            """,
            int(user_id),
        )
    return [dict(r) for r in rows]


async def is_photo_repeat_used(photo_id: int) -> bool:
    await ensure_results_legacy_schema()
    p = _pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT 1 FROM photo_repeats WHERE photo_id=$1", int(photo_id))
    return bool(v)


async def mark_photo_repeat_used(photo_id: int) -> None:
    await ensure_results_legacy_schema()
    p = _pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO photo_repeats (photo_id, used_at)
            VALUES ($1,$2)
            ON CONFLICT (photo_id) DO NOTHING
            """,
            int(photo_id),
            get_moscow_now_iso(),
        )


async def archive_photo_to_my_results(
    user_id: int,
    photo_id: int,
    kind: str,
    day_key: str | None = None,
    place: int | None = None,
) -> None:
    await ensure_results_legacy_schema()
    p = _pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        photo = await conn.fetchrow("SELECT * FROM photos WHERE id=$1", int(photo_id))
        if not photo:
            return
        stat = await conn.fetchrow(
            "SELECT COUNT(*) AS ratings_count, AVG(value)::double precision AS avg_rating FROM ratings WHERE photo_id=$1",
            int(photo_id),
        )
        ratings_count = int((stat["ratings_count"] if stat else 0) or 0)
        avg = stat["avg_rating"] if stat else None
        try:
            avg = float(avg) if avg is not None else None
        except Exception:
            avg = None

        await conn.execute(
            """
            INSERT INTO my_results (user_id, photo_id, file_id, title, day_key, kind, place, avg_rating, ratings_count, created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            int(user_id),
            int(photo_id),
            str(photo["file_id"]),
            photo.get("title"),
            day_key or photo.get("day_key"),
            str(kind),
            place,
            avg,
            ratings_count,
            now,
        )


async def get_my_results_for_user(user_id: int) -> list[dict]:
    await ensure_results_legacy_schema()
    p = _pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM my_results WHERE user_id=$1 ORDER BY created_at DESC, id DESC",
            int(user_id),
        )
    return [dict(r) for r in rows]


async def get_photo_rank_in_day(photo_id: int, day_key: str) -> int | None:
    """Return place (rank) of the photo within its day_key among active photos."""
    if not day_key:
        return None

    p = _pool()
    query = """
        WITH s AS (
            SELECT
                p.id,
                COALESCE(AVG(r.value), 0)::float AS avg_rating,
                COUNT(r.id)::int AS ratings_count,
                p.created_at
            FROM photos p
            LEFT JOIN ratings r ON r.photo_id = p.id
            WHERE p.day_key = $1 AND p.is_deleted=0 AND p.moderation_status='active'
            GROUP BY p.id, p.created_at
        ),
        ranked AS (
            SELECT
                id,
                RANK() OVER (ORDER BY avg_rating DESC, ratings_count DESC, created_at ASC) AS place
            FROM s
        )
        SELECT place FROM ranked WHERE id = $2
    """

    async with p.acquire() as conn:
        v = await conn.fetchval(query, str(day_key), int(photo_id))
    return int(v) if v is not None else None


async def get_weekly_rank_for_user(user_id: int) -> int | None:
    p = _pool()
    now = get_moscow_now().date()
    start = (now - timedelta(days=7)).isoformat()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.user_id, AVG(r.value)::double precision AS avg_rating
            FROM photos p
            JOIN ratings r ON r.photo_id=p.id
            WHERE p.is_deleted=0 AND p.day_key >= $1
            GROUP BY p.user_id
            ORDER BY AVG(r.value) DESC
            """,
            start,
        )
    rank = 1
    for r in rows:
        if int(r["user_id"]) == int(user_id):
            return rank
        rank += 1
    return None


async def get_users_with_multiple_daily_top3(min_wins: int = 2, limit: int = 50) -> list[dict]:
    """
    Admin helper: users who hit daily top-3 multiple times.
    Uses my_results as source.
    """
    await ensure_results_legacy_schema()
    p = _pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.*, COUNT(m.id) AS wins
            FROM my_results m
            JOIN users u ON u.id=m.user_id
            WHERE u.is_deleted=0
              AND (m.kind = 'daily' OR m.kind IS NULL)
              AND m.place IS NOT NULL AND m.place <= 3
            GROUP BY u.id
            HAVING COUNT(m.id) >= $1
            ORDER BY wins DESC, u.id DESC
            LIMIT $2
            """,
            int(min_wins),
            int(limit),
        )
    return [dict(r) for r in rows]


async def get_users_with_multiple_daily_top3_by_hits(min_hits: int = 2, limit: int = 50) -> list[dict]:
    """Alias for backward compatibility with old name."""
    return await get_users_with_multiple_daily_top3(min_wins=min_hits, limit=limit)
