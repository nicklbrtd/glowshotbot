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
    """Aggregated per-photo rows for a day_key (global scope).

    NOTE: We rely on photos.day_key (Moscow date string).
    We DO NOT rely on created_at types (in your DB they are TEXT).
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
            AND COALESCE(p.moderation_status, 'active') = 'active'
        GROUP BY p.id, p.user_id, p.file_id, p.title, u.name, u.username, p.created_at
        """,
        str(day_key),
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
            AND COALESCE(p.moderation_status, 'active') = 'active'
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
            AND COALESCE(p.moderation_status, 'active') = 'active'
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
            AND COALESCE(p.moderation_status, 'active') = 'active'
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
            AND COALESCE(p.moderation_status, 'active') = 'active'
            AND COALESCE(u.country,'') = $2
        GROUP BY p.id, p.user_id, p.file_id, p.title, u.name, u.username, p.created_at
        """,
        str(day_key),
        str(country),
    )
    return [dict(r) for r in rows]