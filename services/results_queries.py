from __future__ import annotations

import asyncpg


async def get_global_mean_and_count(conn: asyncpg.Connection) -> tuple[float, int]:
    """Global mean for all ratings in DB."""
    row = await conn.fetchrow(
        "SELECT AVG(value)::float AS mean, COUNT(*)::int AS cnt FROM ratings"
    )
    cnt = int(row["cnt"] or 0) if row else 0
    mean_raw = row["mean"] if row else None

    # Neutral default if bot is new / no ratings
    mean = float(mean_raw) if mean_raw is not None else 7.0
    return mean, cnt


async def get_day_photo_rows_global(conn: asyncpg.Connection, *, day_key: str) -> list[dict]:
    """Агрегаты по фото за день для глобальных итогов.

    Включает: оценки, комменты, количество приглашённых друзей, pending жалобы, last_win_date.
    """
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
            COALESCE(SUM(r.value), 0)::float AS sum_values,
            AVG(r.value)::float AS avg_rating,

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
    )
    return [dict(r) for r in rows]


async def get_day_photo_rows_user(conn: asyncpg.Connection, *, day_key: str, user_id: int) -> list[dict]:
    """Агрегаты по фото конкретного пользователя за день (для чеклиста допуска)."""
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
            COALESCE(SUM(r.value), 0)::float AS sum_values,
            AVG(r.value)::float AS avg_rating,

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
            COALESCE(SUM(r.value), 0)::float AS sum_values,
            AVG(r.value)::float AS avg_rating,

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
    )
    return [dict(r) for r in rows]


async def get_day_photo_rows_country(conn: asyncpg.Connection, *, day_key: str, country: str) -> list[dict]:
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
            COALESCE(SUM(r.value), 0)::float AS sum_values,
            AVG(r.value)::float AS avg_rating,

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
    )
    return [dict(r) for r in rows]
