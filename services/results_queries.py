from __future__ import annotations

import asyncpg
from config import LINK_RATING_WEIGHT, RATE_BAYES_FALLBACK_MEAN


def _link_rating_weight() -> float:
    try:
        w = float(LINK_RATING_WEIGHT)
    except Exception:
        w = 0.5
    if w <= 0:
        return 0.0
    return w


async def get_global_mean_and_count(conn: asyncpg.Connection) -> tuple[float, int]:
    """Global mean for all ratings in DB."""
    try:
        w = float(LINK_RATING_WEIGHT)
    except Exception:
        w = 0.5
    row = await conn.fetchrow(
        """
        SELECT
            COALESCE(SUM(value * CASE WHEN source='link' THEN $1 ELSE 1 END), 0)::float AS sum_w,
            COALESCE(SUM(CASE WHEN source='link' THEN $1 ELSE 1 END), 0)::float AS cnt_w,
            COUNT(*)::int AS cnt_raw
        FROM ratings
        """,
        float(w),
    )
    cnt = int(row["cnt_raw"] or 0) if row else 0
    sum_w = float(row["sum_w"]) if row and row["sum_w"] is not None else 0.0
    cnt_w = float(row["cnt_w"]) if row and row["cnt_w"] is not None else 0.0

    # Neutral default if bot is new / no ratings
    mean = (sum_w / cnt_w) if cnt_w > 0 else float(RATE_BAYES_FALLBACK_MEAN)
    return mean, cnt


async def get_day_photo_rows_global(conn: asyncpg.Connection, *, day_key: str) -> list[dict]:
    """Агрегаты по фото за день для глобальных итогов.

    Включает: оценки, комменты, количество приглашённых друзей, pending жалобы, last_win_date.
    """
    w = _link_rating_weight()
    rows = await conn.fetch(
        """
        SELECT
            p.id::bigint AS photo_id,
            p.user_id::bigint AS user_id,
            p.file_id AS file_id,
            COALESCE(NULLIF(p.title, ''), 'Без названия') AS title,
            COALESCE(u.name, '') AS user_name,
            u.username AS user_username,
            (SELECT COUNT(*)::int FROM referrals rf WHERE rf.inviter_user_id = p.user_id AND rf.qualified = 1) AS invited_qualified,
            p.last_win_date AS last_win_date,

            COUNT(r.id)::int AS ratings_count,
            COUNT(DISTINCT r.user_id)::int AS rated_users,
            COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS sum_values,
            COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS ratings_weighted_count,
            CASE
                WHEN COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0) > 0 THEN
                    COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float
                    / COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)
                ELSE NULL
            END AS avg_rating,

            p.created_at AS created_at,

            (SELECT COUNT(*)::int FROM comments c WHERE c.photo_id=p.id) AS comments_count,
            (SELECT COUNT(*)::int FROM super_ratings sr WHERE sr.photo_id=p.id) AS super_count,
            (SELECT COUNT(*)::int FROM photo_reports pr WHERE pr.photo_id=p.id AND COALESCE(pr.status,'pending')='pending') AS pending_reports
        FROM photos p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN ratings r ON r.photo_id = p.id
        WHERE
            p.day_key = $1
            AND COALESCE(p.is_deleted, 0) = 0
            AND COALESCE(p.moderation_status, 'active') IN ('active','good')
        GROUP BY p.id, p.user_id, p.file_id, p.title, u.name, u.username, p.created_at, p.last_win_date
        """,
        str(day_key),
        float(w),
    )
    return [dict(r) for r in rows]


async def get_day_photo_rows_user(conn: asyncpg.Connection, *, day_key: str, user_id: int) -> list[dict]:
    """Агрегаты по фото конкретного пользователя за день (для чеклиста допуска)."""
    w = _link_rating_weight()
    rows = await conn.fetch(
        """
        SELECT
            p.id::bigint AS photo_id,
            p.user_id::bigint AS user_id,
            p.file_id AS file_id,
            COALESCE(NULLIF(p.title, ''), 'Без названия') AS title,
            COALESCE(u.name, '') AS user_name,
            u.username AS user_username,
            (SELECT COUNT(*)::int FROM referrals rf WHERE rf.inviter_user_id = p.user_id AND rf.qualified = 1) AS invited_qualified,
            p.last_win_date AS last_win_date,

            COUNT(r.id)::int AS ratings_count,
            COUNT(DISTINCT r.user_id)::int AS rated_users,
            COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0)::float AS sum_values,
            COALESCE(SUM(CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0)::float AS ratings_weighted_count,
            CASE
                WHEN COALESCE(SUM(CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0) > 0 THEN
                    COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0)::float
                    / COALESCE(SUM(CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0)
                ELSE NULL
            END AS avg_rating,

            p.created_at AS created_at,

            (SELECT COUNT(*)::int FROM comments c WHERE c.photo_id=p.id) AS comments_count,
            (SELECT COUNT(*)::int FROM super_ratings sr WHERE sr.photo_id=p.id) AS super_count,
            (SELECT COUNT(*)::int FROM photo_reports pr WHERE pr.photo_id=p.id AND COALESCE(pr.status,'pending')='pending') AS pending_reports
        FROM photos p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN ratings r ON r.photo_id = p.id
        WHERE
            p.day_key = $1
            AND p.user_id = $2
            AND COALESCE(p.is_deleted, 0) = 0
            AND COALESCE(p.moderation_status, 'active') IN ('active','good')
        GROUP BY p.id, p.user_id, p.file_id, p.title, u.name, u.username, p.created_at, p.last_win_date
        ORDER BY p.created_at ASC
        """,
        str(day_key),
        int(user_id),
        float(w),
    )
    return [dict(r) for r in rows]


async def count_active_authors_city(conn: asyncpg.Connection, *, city: str) -> int:
    row = await conn.fetchrow(
        """
        SELECT COUNT(DISTINCT p.user_id)::int AS cnt
        FROM photos p
        JOIN users u ON u.id=p.user_id
        WHERE
            COALESCE(p.is_deleted, 0) = 0
            AND COALESCE(p.moderation_status, 'active') IN ('active','good')
            AND COALESCE(u.city,'') = $1
        """,
        str(city),
    )
    return int(row["cnt"] or 0) if row else 0


async def count_active_authors_country(conn: asyncpg.Connection, *, country: str) -> int:
    row = await conn.fetchrow(
        """
        SELECT COUNT(DISTINCT p.user_id)::int AS cnt
        FROM photos p
        JOIN users u ON u.id=p.user_id
        WHERE
            COALESCE(p.is_deleted, 0) = 0
            AND COALESCE(p.moderation_status, 'active') IN ('active','good')
            AND COALESCE(u.country,'') = $1
        """,
        str(country),
    )
    return int(row["cnt"] or 0) if row else 0


async def get_day_photo_rows_city(conn: asyncpg.Connection, *, day_key: str, city: str) -> list[dict]:
    w = _link_rating_weight()
    rows = await conn.fetch(
        """
        SELECT
            p.id::bigint AS photo_id,
            p.user_id::bigint AS user_id,
            p.file_id AS file_id,
            COALESCE(NULLIF(p.title, ''), 'Без названия') AS title,
            COALESCE(u.name, '') AS user_name,
            u.username AS user_username,

            COUNT(r.id)::int AS ratings_count,
            COUNT(DISTINCT r.user_id)::int AS rated_users,
            COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0)::float AS sum_values,
            COALESCE(SUM(CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0)::float AS ratings_weighted_count,
            CASE
                WHEN COALESCE(SUM(CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0) > 0 THEN
                    COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0)::float
                    / COALESCE(SUM(CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0)
                ELSE NULL
            END AS avg_rating,

            p.created_at AS created_at,

            (SELECT COUNT(*)::int FROM comments c WHERE c.photo_id=p.id) AS comments_count,
            (SELECT COUNT(*)::int FROM super_ratings sr WHERE sr.photo_id=p.id) AS super_count
        FROM photos p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN ratings r ON r.photo_id = p.id
        WHERE
            p.day_key = $1
            AND COALESCE(p.is_deleted, 0) = 0
            AND COALESCE(p.moderation_status, 'active') IN ('active','good')
            AND COALESCE(u.city,'') = $2
        GROUP BY p.id, p.user_id, p.file_id, p.title, u.name, u.username, p.created_at
        """,
        str(day_key),
        str(city),
        float(w),
    )
    return [dict(r) for r in rows]


async def get_day_photo_rows_country(conn: asyncpg.Connection, *, day_key: str, country: str) -> list[dict]:
    w = _link_rating_weight()
    rows = await conn.fetch(
        """
        SELECT
            p.id::bigint AS photo_id,
            p.user_id::bigint AS user_id,
            p.file_id AS file_id,
            COALESCE(NULLIF(p.title, ''), 'Без названия') AS title,
            COALESCE(u.name, '') AS user_name,
            u.username AS user_username,

            COUNT(r.id)::int AS ratings_count,
            COUNT(DISTINCT r.user_id)::int AS rated_users,
            COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0)::float AS sum_values,
            COALESCE(SUM(CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0)::float AS ratings_weighted_count,
            CASE
                WHEN COALESCE(SUM(CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0) > 0 THEN
                    COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0)::float
                    / COALESCE(SUM(CASE WHEN r.source='link' THEN $3 ELSE 1 END), 0)
                ELSE NULL
            END AS avg_rating,

            p.created_at AS created_at,

            (SELECT COUNT(*)::int FROM comments c WHERE c.photo_id=p.id) AS comments_count,
            (SELECT COUNT(*)::int FROM super_ratings sr WHERE sr.photo_id=p.id) AS super_count
        FROM photos p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN ratings r ON r.photo_id = p.id
        WHERE
            p.day_key = $1
            AND COALESCE(p.is_deleted, 0) = 0
            AND COALESCE(p.moderation_status, 'active') IN ('active','good')
            AND COALESCE(u.country,'') = $2
        GROUP BY p.id, p.user_id, p.file_id, p.title, u.name, u.username, p.created_at
        """,
        str(day_key),
        str(country),
        float(w),
    )
    return [dict(r) for r in rows]
