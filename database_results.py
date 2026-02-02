
from datetime import timedelta
from typing import Any, Iterable

from utils.time import get_moscow_now, get_moscow_today, get_moscow_now_iso


# We reuse the project's asyncpg pool stored in database.py
try:
    from database import _assert_pool, _bayes_prior_weight
except Exception:  # pragma: no cover
    _assert_pool = None  # type: ignore
    _bayes_prior_weight = None  # type: ignore


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

# ---- all-time leaderboard ----
ALL_TIME_MIN_VOTES = 10


HOF_STATUS_ACTIVE = "active"
HOF_STATUS_DELETED = "deleted_by_author"
HOF_STATUS_HIDDEN = "hidden"
HOF_STATUS_MODERATED = "moderated"


async def ensure_hof_schema() -> None:
    """Create hall_of_fame table if missing."""
    p = _pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hall_of_fame (
                photo_id BIGINT PRIMARY KEY,
                user_id BIGINT,
                best_rank INTEGER NOT NULL,
                best_score DOUBLE PRECISION,
                votes_at_best INTEGER,
                achieved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                title_snapshot TEXT,
                author_snapshot TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )


async def get_all_time_top(limit: int = 50, min_votes: int = ALL_TIME_MIN_VOTES) -> list[dict]:
    """Return all-time leaderboard (unique best photo per author)."""
    p = _pool()
    prior = _bayes_prior_weight()
    async with p.acquire() as conn:
        from database import _get_global_rating_mean  # lazy import to avoid cycle

        global_mean, _ = await _get_global_rating_mean(conn)

        rows = await conn.fetch(
            """
            WITH s AS (
                SELECT
                    ph.id AS photo_id,
                    ph.user_id,
                    ph.title,
                    ph.file_id_public AS file_id,
                    ph.created_at,
                    ph.moderation_status,
                    ph.is_deleted,
                    u.username,
                    u.name AS author_name,
                    COUNT(r.id)::int AS ratings_count,
                    COALESCE(SUM(r.value), 0)::float AS ratings_sum
                FROM photos ph
                LEFT JOIN ratings r ON r.photo_id = ph.id
                LEFT JOIN users u ON u.id = ph.user_id
                WHERE ph.is_deleted = 0
                  AND ph.moderation_status IN ('active','good')
                GROUP BY ph.id, ph.user_id, ph.title, ph.file_id_public, ph.created_at, ph.moderation_status, ph.is_deleted, u.username, u.name
            ),
            filtered AS (
                SELECT *,
                    CASE
                        WHEN ratings_count > 0 THEN (($1::float * $2::float) + ratings_sum) / ($1::float + ratings_count)
                        ELSE NULL
                    END AS bayes_score
                FROM s
                WHERE ratings_count >= $3
            ),
            ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY user_id
                        ORDER BY
                            bayes_score DESC NULLS LAST,
                            ratings_count DESC,
                            created_at DESC,
                            photo_id ASC
                    ) AS rn
                FROM filtered
            )
            SELECT *
            FROM ranked
            WHERE rn = 1
            ORDER BY
                bayes_score DESC NULLS LAST,
                ratings_count DESC,
                created_at DESC,
                photo_id ASC
            LIMIT $4
            """,
            float(prior),
            float(global_mean),
            int(min_votes),
            int(limit),
        )

    return [dict(r) for r in rows]


async def update_hall_of_fame_from_top(top_items: list[dict]) -> None:
    """Upsert hall_of_fame snapshot based on current all-time top."""
    if not top_items:
        return
    p = _pool()
    await ensure_hof_schema()
    async with p.acquire() as conn:
        # Build quick status map for photos
        photo_ids = [int(i.get("photo_id")) for i in top_items if i.get("photo_id")]
        statuses = {}
        if photo_ids:
            rows = await conn.fetch(
                "SELECT id, is_deleted, moderation_status FROM photos WHERE id = ANY($1::bigint[])",
                photo_ids,
            )
            for r in rows:
                if r["is_deleted"]:
                    statuses[int(r["id"])] = HOF_STATUS_DELETED
                elif str(r["moderation_status"] or "") not in ("active", "good"):
                    statuses[int(r["id"])] = HOF_STATUS_HIDDEN
                else:
                    statuses[int(r["id"])] = HOF_STATUS_ACTIVE

        for idx, item in enumerate(top_items, start=1):
            pid = int(item.get("photo_id"))
            uid = int(item.get("user_id")) if item.get("user_id") is not None else None
            score = item.get("bayes_score") or item.get("score")
            votes = int(item.get("ratings_count") or 0)
            title = item.get("title")
            author = item.get("author_name") or item.get("username")
            status = statuses.get(pid, HOF_STATUS_ACTIVE)

            row = await conn.fetchrow("SELECT best_rank, best_score FROM hall_of_fame WHERE photo_id=$1", pid)
            if row is None:
                await conn.execute(
                    """
                    INSERT INTO hall_of_fame
                        (photo_id, user_id, best_rank, best_score, votes_at_best, achieved_at, title_snapshot, author_snapshot, status)
                    VALUES
                        ($1,$2,$3,$4,$5,NOW(),$6,$7,$8)
                    ON CONFLICT (photo_id) DO NOTHING
                    """,
                    pid,
                    uid,
                    int(idx),
                    float(score) if score is not None else None,
                    votes,
                    title,
                    author,
                    status,
                )
                continue

            best_rank = int(row["best_rank"] or 999999)
            if idx < best_rank:
                await conn.execute(
                    """
                    UPDATE hall_of_fame
                    SET best_rank=$2, best_score=$3, votes_at_best=$4, achieved_at=NOW(), title_snapshot=$5, author_snapshot=$6, status=$7, updated_at=NOW()
                    WHERE photo_id=$1
                    """,
                    pid,
                    int(idx),
                    float(score) if score is not None else None,
                    votes,
                    title,
                    author,
                    status,
                )
            else:
                await conn.execute(
                    "UPDATE hall_of_fame SET status=$2, updated_at=NOW() WHERE photo_id=$1",
                    pid,
                    status,
                )


async def refresh_hof_statuses() -> None:
    """Refresh statuses for all hall_of_fame rows based on current photo state."""
    p = _pool()
    await ensure_hof_schema()
    async with p.acquire() as conn:
        await conn.execute(
            """
            UPDATE hall_of_fame hof
            SET status = CASE
                WHEN ph.id IS NULL THEN $4
                WHEN ph.is_deleted = 1 THEN $1
                WHEN ph.moderation_status NOT IN ('active','good') THEN $2
                ELSE $3
            END,
            updated_at = NOW()
            FROM photos ph
            WHERE ph.id = hof.photo_id
            """,
            HOF_STATUS_DELETED,
            HOF_STATUS_HIDDEN,
            HOF_STATUS_ACTIVE,
            HOF_STATUS_MODERATED,
        )


async def get_hof_items(limit: int = 50) -> list[dict]:
    p = _pool()
    await ensure_hof_schema()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT photo_id, user_id, best_rank, best_score, votes_at_best, achieved_at,
                   title_snapshot, author_snapshot, status
            FROM hall_of_fame
            ORDER BY best_rank ASC, best_score DESC NULLS LAST, achieved_at ASC
            LIMIT $1
            """,
            int(limit),
        )
    return [dict(r) for r in rows]

# ---- all-time leaderboard ----
ALL_TIME_MIN_VOTES = 10


HOF_STATUS_ACTIVE = "active"
HOF_STATUS_DELETED = "deleted_by_author"
HOF_STATUS_HIDDEN = "hidden"
HOF_STATUS_MODERATED = "moderated"


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
