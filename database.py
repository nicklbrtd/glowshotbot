import json
import os
import random
import time
from datetime import datetime, timedelta, date

import asyncpg
from asyncpg.exceptions import UniqueViolationError

from utils.time import (
    get_moscow_now,
    get_moscow_today,
    get_moscow_now_iso,
    get_bot_now,
    get_bot_now_iso,
    get_bot_today,
    today_key,
    end_of_day,
    is_happy_hour,
)
from utils.watermark import generate_author_code
from config import (
    LINK_RATING_WEIGHT,
    RATE_POPULAR_MIN_RATINGS,
    RATE_LOW_RATINGS_MAX,
    RATE_PREMIUM_BOOST_CHANCE,
    RATE_BAYES_PRIOR,
    RATE_BAYES_FALLBACK_MEAN,
    STREAK_DAILY_RATINGS,
    STREAK_DAILY_COMMENTS,
    STREAK_DAILY_UPLOADS,
    STREAK_GRACE_HOURS,
    STREAK_MAX_NUDGES_PER_DAY,
)

DB_DSN = os.getenv("DATABASE_URL")
pool: asyncpg.Pool | None = None

# Cache global rating mean so we don't query it on every profile view.
# Stored as (ts, mean, count)
_GLOBAL_RATING_CACHE: tuple[float, float, int] | None = None
_GLOBAL_RATING_TTL_SECONDS = 300
_UPLOAD_RULES_ACK_INTERVAL = timedelta(days=14)


def _link_rating_weight() -> float:
    try:
        w = float(LINK_RATING_WEIGHT)
    except Exception:
        w = 0.5
    if w <= 0:
        return 0.0
    return w


def _bayes_prior_weight() -> int:
    """How many virtual votes the global mean contributes.

    Tunable constant (1..200). Lower value reacts faster to new votes.
    """
    try:
        v = int(RATE_BAYES_PRIOR)
    except Exception:
        v = 12
    if v < 1:
        return 1
    if v > 200:
        return 200
    return v


def _bayes_fallback_mean() -> float:
    """Neutral fallback for an empty project (no ratings yet)."""
    try:
        v = float(RATE_BAYES_FALLBACK_MEAN)
    except Exception:
        v = 7.0
    if v < 1.0:
        return 1.0
    if v > 10.0:
        return 10.0
    return v


def _premium_boost_chance() -> float:
    try:
        v = float(RATE_PREMIUM_BOOST_CHANCE)
    except Exception:
        v = 0.3
    if v < 0:
        return 0.0
    if v > 0.8:
        return 0.8
    return v


async def _get_global_rating_mean(conn: asyncpg.Connection) -> tuple[float, int]:
    """Return (global_mean, global_count) for all ratings.

    Uses a short in-process cache.
    """
    global _GLOBAL_RATING_CACHE
    now = time.time()
    if _GLOBAL_RATING_CACHE is not None:
        ts, mean, cnt = _GLOBAL_RATING_CACHE
        if (now - ts) < _GLOBAL_RATING_TTL_SECONDS:
            return float(mean), int(cnt)

    w = _link_rating_weight()
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
    cnt = int(row["cnt_raw"]) if row and row["cnt_raw"] is not None else 0
    sum_w = float(row["sum_w"]) if row and row["sum_w"] is not None else 0.0
    cnt_w = float(row["cnt_w"]) if row and row["cnt_w"] is not None else 0.0

    # If the bot is new / no ratings yet, use neutral fallback.
    mean = (sum_w / cnt_w) if cnt_w > 0 else _bayes_fallback_mean()

    _GLOBAL_RATING_CACHE = (now, mean, cnt)
    return mean, cnt


def _bayes_score(*, sum_values: float, n: float, global_mean: float, prior: int) -> float | None:
    if n <= 0:
        return None
    return (prior * float(global_mean) + float(sum_values)) / (prior + float(n))


def _assert_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("DB pool is not initialized. Call init_db() at startup.")
    return pool


def _today_key() -> str:
    """Day key in Moscow timezone, stored as ISO date string (YYYY-MM-DD)."""
    try:
        return today_key()
    except Exception:
        d = get_moscow_today()
        try:
            return d.isoformat()
        except Exception:
            return str(d)


def _week_key() -> str:
    """Week key (Monday date) in Moscow timezone, ISO string."""
    d = get_moscow_today()
    monday = d - timedelta(days=d.weekday())
    try:
        return monday.isoformat()
    except Exception:
        return str(monday)


# ---------------------------------------------------------------------------
# Credits / stats helpers
# ---------------------------------------------------------------------------


async def _ensure_user_stats_row(conn: asyncpg.Connection, user_id: int) -> dict:
    """Get or create user_stats row, resetting day counters if last_active_at is another day."""
    now_dt = get_bot_now()
    today = now_dt.date()
    row = await conn.fetchrow(
        """
        INSERT INTO user_stats (user_id, credits, show_tokens, last_active_at, votes_given_today, votes_given_happyhour_today, public_portfolio)
        VALUES ($1, 0, 0, $2, 0, 0, FALSE)
        ON CONFLICT (user_id) DO NOTHING
        RETURNING *
        """,
        int(user_id),
        now_dt,
    )
    if row is None:
        row = await conn.fetchrow("SELECT * FROM user_stats WHERE user_id=$1", int(user_id))

    if row is None:
        return {
            "user_id": int(user_id),
            "credits": 0,
            "show_tokens": 0,
            "votes_given_today": 0,
            "votes_given_happyhour_today": 0,
            "public_portfolio": False,
            "author_forward_allowed": True,
            "author_badge_enabled": True,
            "last_active_at": now_dt,
        }

    # reset daily counters when day changes
    last_active = row.get("last_active_at")
    if last_active is None or getattr(last_active, "date", lambda: today)() != today:
        row = await conn.fetchrow(
            """
            UPDATE user_stats
            SET votes_given_today=0,
                votes_given_happyhour_today=0,
                last_active_at=$2
            WHERE user_id=$1
            RETURNING *
            """,
            int(user_id),
            now_dt,
        )
    return dict(row) if row else {}


async def get_user_stats(user_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        return await _ensure_user_stats_row(conn, int(user_id))


async def get_upload_rules_ack_at(user_id: int) -> datetime | None:
    """Return timestamp of last upload-rules confirmation for user."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_user_stats_row(conn, int(user_id))
        value = await conn.fetchval(
            "SELECT upload_rules_ack_at FROM user_stats WHERE user_id=$1",
            int(user_id),
        )
    return value


async def should_show_upload_rules(user_id: int, now: datetime | None = None) -> bool:
    """Rules are shown at most once per 14 days; reset only after explicit ack."""
    ack_at = await get_upload_rules_ack_at(int(user_id))
    if ack_at is None:
        return True
    now_dt = now or get_moscow_now()
    try:
        if ack_at.tzinfo is None and now_dt.tzinfo is not None:
            ack_at = ack_at.replace(tzinfo=now_dt.tzinfo)
    except Exception:
        pass
    return now_dt >= (ack_at + _UPLOAD_RULES_ACK_INTERVAL)


async def set_upload_rules_ack_at(user_id: int, dt: datetime | None = None) -> datetime:
    """Store explicit rules acknowledgement timestamp."""
    p = _assert_pool()
    ts = dt or get_moscow_now()
    async with p.acquire() as conn:
        await _ensure_user_stats_row(conn, int(user_id))
        await conn.execute(
            "UPDATE user_stats SET upload_rules_ack_at=$2 WHERE user_id=$1",
            int(user_id),
            ts,
        )
    return ts


async def get_author_settings(*, user_id: int | None = None, tg_id: int | None = None) -> dict:
    """Return author UI settings from user_stats.

    Keys:
      - forward_allowed (bool): whether photo forwarding is allowed.
      - badge_enabled (bool): whether author badge is visible.
    """
    uid = None
    if user_id is not None:
        try:
            uid = int(user_id)
        except Exception:
            uid = None
    elif tg_id is not None:
        p = _assert_pool()
        async with p.acquire() as conn:
            uid = await conn.fetchval(
                "SELECT id FROM users WHERE tg_id=$1 AND is_deleted=0",
                int(tg_id),
            )
            if uid is None:
                return {"forward_allowed": True, "badge_enabled": True}
            await _ensure_user_stats_row(conn, int(uid))
            row = await conn.fetchrow(
                """
                SELECT
                    COALESCE(author_forward_allowed, TRUE) AS author_forward_allowed,
                    COALESCE(author_badge_enabled, TRUE) AS author_badge_enabled
                FROM user_stats
                WHERE user_id=$1
                """,
                int(uid),
            )
        return {
            "forward_allowed": bool((row or {}).get("author_forward_allowed", True)),
            "badge_enabled": bool((row or {}).get("author_badge_enabled", True)),
        }

    if uid is None:
        return {"forward_allowed": True, "badge_enabled": True}

    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_user_stats_row(conn, int(uid))
        row = await conn.fetchrow(
            """
            SELECT
                COALESCE(author_forward_allowed, TRUE) AS author_forward_allowed,
                COALESCE(author_badge_enabled, TRUE) AS author_badge_enabled
            FROM user_stats
            WHERE user_id=$1
            """,
            int(uid),
        )
    return {
        "forward_allowed": bool((row or {}).get("author_forward_allowed", True)),
        "badge_enabled": bool((row or {}).get("author_badge_enabled", True)),
    }


async def set_author_forward_allowed(user_id: int, allowed: bool) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_user_stats_row(conn, int(user_id))
        await conn.execute(
            "UPDATE user_stats SET author_forward_allowed=$2 WHERE user_id=$1",
            int(user_id),
            bool(allowed),
        )


async def set_author_badge_enabled(user_id: int, enabled: bool) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_user_stats_row(conn, int(user_id))
        await conn.execute(
            "UPDATE user_stats SET author_badge_enabled=$2 WHERE user_id=$1",
            int(user_id),
            bool(enabled),
        )


def _happy_hour_multiplier(now: datetime | None = None) -> int:
    return 4 if is_happy_hour(now) else 2


def _is_happy_hour_with_settings(now_dt: datetime, economy: dict) -> bool:
    if not bool(economy.get("happy_hour_enabled", True)):
        return False
    start_hour = _coerce_int(economy.get("happy_hour_start_hour"), 15, min_value=0, max_value=23)
    duration_minutes = _coerce_int(economy.get("happy_hour_duration_minutes"), 60, min_value=1, max_value=1440)
    local = now_dt.astimezone(get_bot_now().tzinfo).replace(second=0, microsecond=0)
    start = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=duration_minutes)
    if duration_minutes >= 1440:
        return True
    if end.date() == start.date():
        return start <= local < end
    # crossing midnight
    return local >= start or local < end


def _credit_multiplier_for_moment(now_dt: datetime, economy: dict) -> int:
    normal = _coerce_int(economy.get("credit_to_shows_normal"), 2, min_value=1, max_value=100)
    happy = _coerce_int(economy.get("credit_to_shows_happyhour"), 4, min_value=1, max_value=100)
    return happy if _is_happy_hour_with_settings(now_dt, economy) else normal


async def add_credits_on_vote(
    voter_id: int,
    *,
    delta: int = 1,
    now: datetime | None = None,
) -> dict:
    """Increment credits for voter and their daily counters. Returns updated stats."""
    p = _assert_pool()
    economy = await get_effective_economy_settings()
    async with p.acquire() as conn:
        stats = await _ensure_user_stats_row(conn, int(voter_id))
        now_dt = now or get_bot_now()
        is_hh = _is_happy_hour_with_settings(now_dt, economy)
        stats = await conn.fetchrow(
            """
            UPDATE user_stats
            SET credits = credits + $2,
                last_active_at = $3,
                votes_given_today = votes_given_today + 1,
                votes_given_happyhour_today = votes_given_happyhour_today + CASE WHEN $4 THEN 1 ELSE 0 END
            WHERE user_id=$1
            RETURNING *
            """,
            int(voter_id),
            int(delta),
            now_dt,
            is_hh,
        )
    return dict(stats) if stats else {}


async def add_credits(user_id: int, amount: int = 1) -> dict:
    """Public helper to add credits (e.g., bonus for premium upload)."""
    return await add_credits_on_vote(int(user_id), delta=int(amount), now=get_bot_now())


async def admin_add_credits(user_id: int, amount: int) -> dict:
    """Admin helper: add credits to a user by internal user_id."""
    delta = int(amount)
    if delta <= 0:
        raise ValueError("amount must be positive")

    p = _assert_pool()
    now_dt = get_bot_now()
    async with p.acquire() as conn:
        await _ensure_user_stats_row(conn, int(user_id))
        row = await conn.fetchrow(
            """
            UPDATE user_stats
            SET credits = credits + $2,
                last_active_at = $3
            WHERE user_id=$1
            RETURNING credits, show_tokens
            """,
            int(user_id),
            int(delta),
            now_dt,
        )
    return {
        "credits": int((row or {}).get("credits") or 0),
        "show_tokens": int((row or {}).get("show_tokens") or 0),
        "added": int(delta),
    }


async def admin_remove_credits(user_id: int, amount: int) -> dict:
    """Admin helper: remove credits from a user by internal user_id (not below zero)."""
    delta = int(amount)
    if delta <= 0:
        raise ValueError("amount must be positive")

    p = _assert_pool()
    now_dt = get_bot_now()
    async with p.acquire() as conn:
        await _ensure_user_stats_row(conn, int(user_id))
        async with conn.transaction():
            before_row = await conn.fetchrow(
                "SELECT credits, show_tokens FROM user_stats WHERE user_id=$1 FOR UPDATE",
                int(user_id),
            )
            before_credits = int((before_row or {}).get("credits") or 0)
            new_credits = max(0, before_credits - int(delta))
            row = await conn.fetchrow(
                """
                UPDATE user_stats
                SET credits = $2,
                    last_active_at = $3
                WHERE user_id=$1
                RETURNING credits, show_tokens
                """,
                int(user_id),
                int(new_credits),
                now_dt,
            )

    return {
        "credits": int((row or {}).get("credits") or 0),
        "show_tokens": int((row or {}).get("show_tokens") or 0),
        "removed": int(before_credits - new_credits),
    }


async def admin_reset_all_credits() -> int:
    """Admin helper: reset credits and show tokens for all users."""
    p = _assert_pool()
    async with p.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE user_stats
            SET credits=0,
                show_tokens=0,
                last_active_at=$1
            WHERE COALESCE(credits,0) <> 0
               OR COALESCE(show_tokens,0) <> 0
            """,
            get_bot_now(),
        )
    try:
        return int(str(res).split()[-1])
    except Exception:
        return 0


async def admin_add_credits_all(amount: int) -> dict:
    """Admin helper: add credits to all non-deleted users."""
    delta = int(amount)
    if delta <= 0:
        raise ValueError("amount must be positive")

    p = _assert_pool()
    now_dt = get_bot_now()
    async with p.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO user_stats (
                    user_id,
                    credits,
                    show_tokens,
                    last_active_at,
                    votes_given_today,
                    votes_given_happyhour_today,
                    public_portfolio
                )
                SELECT
                    u.id,
                    0,
                    0,
                    $1,
                    0,
                    0,
                    FALSE
                FROM users u
                LEFT JOIN user_stats us ON us.user_id = u.id
                WHERE COALESCE(u.is_deleted, 0) = 0
                  AND us.user_id IS NULL
                """,
                now_dt,
            )
            res = await conn.execute(
                """
                UPDATE user_stats us
                SET credits = us.credits + $2,
                    last_active_at = $1
                FROM users u
                WHERE u.id = us.user_id
                  AND COALESCE(u.is_deleted, 0) = 0
                """,
                now_dt,
                delta,
            )
            total_users = await conn.fetchval(
                "SELECT COUNT(*)::int FROM users WHERE COALESCE(is_deleted, 0) = 0"
            )

    try:
        affected = int(str(res).split()[-1])
    except Exception:
        affected = 0

    return {
        "added_per_user": int(delta),
        "affected_users": int(affected),
        "total_active_users": int(total_users or 0),
        "total_added": int(affected * delta),
    }


async def admin_delete_all_active_photos() -> int:
    """Admin helper: soft-delete all active photos for all users."""
    p = _assert_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, submit_day
                FROM photos
                WHERE is_deleted=0
                  AND COALESCE(status,'active')='active'
                FOR UPDATE
                """
            )
            if not rows:
                return 0
            photo_ids = [int(r["id"]) for r in rows]
            await conn.execute(
                """
                UPDATE photos
                SET is_deleted=1,
                    status='deleted',
                    deleted_reason='admin_bulk_active',
                    deleted_at=NOW()
                WHERE id = ANY($1::bigint[])
                """,
                photo_ids,
            )
            await conn.execute(
                """
                DELETE FROM result_ranks
                WHERE photo_id = ANY($1::bigint[])
                """,
                photo_ids,
            )
            await conn.execute(
                """
                DELETE FROM notification_queue
                WHERE status='pending'
                  AND type='daily_results_top'
                  AND (payload->>'photo_id') IS NOT NULL
                  AND (payload->>'photo_id')::bigint = ANY($1::bigint[])
                """,
                photo_ids,
            )
            submit_days = sorted(
                {
                    r.get("submit_day")
                    for r in rows
                    if r.get("submit_day") is not None
                }
            )
            if submit_days:
                await conn.execute(
                    """
                    DELETE FROM daily_results_cache
                    WHERE submit_day = ANY($1::date[])
                    """,
                    submit_days,
                )
            return len(photo_ids)


async def admin_delete_all_archived_photos() -> int:
    """Admin helper: soft-delete all archived photos for all users."""
    p = _assert_pool()
    async with p.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE photos
            SET is_deleted=1,
                status='deleted',
                deleted_reason='admin_bulk_archive',
                deleted_at=NOW()
            WHERE is_deleted=0
              AND status='archived'
            """
        )
    try:
        return int(str(res).split()[-1])
    except Exception:
        return 0


async def admin_reset_results_and_archives() -> dict:
    """
    Admin helper: full reset of results and archives to start from a clean slate.
    - Removes archived photos (soft-delete).
    - Clears legacy and v2 results caches/tables.
    - Clears queued daily results notifications.
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            archived_res = await _clear_archived_photos(conn)
            results_only = await _clear_results_tables(conn)

    archived_deleted = _affected_count(archived_res)
    return {
        **results_only,
        "archived_photos_deleted": int(archived_deleted),
        "total_rows_affected": int(archived_deleted + int(results_only.get("results_rows_deleted") or 0)),
    }


def _results_table_names() -> list[str]:
    return [
        "result_ranks",
        "daily_results_cache",
        "results_v2",
        "photo_day_metrics",
        "alltime_cache",
        "hall_of_fame",
        "weekly_candidates",
        "photo_repeats",
        "my_results",
    ]


def _affected_count(res: object) -> int:
    try:
        return int(str(res).split()[-1])
    except Exception:
        return 0


async def _clear_results_tables(conn: asyncpg.Connection) -> dict:
    notif_res = await conn.execute(
        """
        DELETE FROM notification_queue
        WHERE type='daily_results_top'
        """
    )

    table_counts: dict[str, int] = {}
    for table_name in _results_table_names():
        exists = await conn.fetchval("SELECT to_regclass($1)", f"public.{table_name}")
        if not exists:
            table_counts[table_name] = 0
            continue
        res = await conn.execute(f"DELETE FROM {table_name}")
        table_counts[table_name] = _affected_count(res)

    results_rows = _affected_count(notif_res) + sum(table_counts.values())
    return {
        "notifications_deleted": _affected_count(notif_res),
        "result_ranks_deleted": int(table_counts.get("result_ranks", 0)),
        "daily_results_cache_deleted": int(table_counts.get("daily_results_cache", 0)),
        "results_v2_deleted": int(table_counts.get("results_v2", 0)),
        "photo_day_metrics_deleted": int(table_counts.get("photo_day_metrics", 0)),
        "alltime_cache_deleted": int(table_counts.get("alltime_cache", 0)),
        "hall_of_fame_deleted": int(table_counts.get("hall_of_fame", 0)),
        "weekly_candidates_deleted": int(table_counts.get("weekly_candidates", 0)),
        "photo_repeats_deleted": int(table_counts.get("photo_repeats", 0)),
        "my_results_deleted": int(table_counts.get("my_results", 0)),
        "results_rows_deleted": int(results_rows),
    }


async def _clear_archived_photos(conn: asyncpg.Connection) -> object:
    return await conn.execute(
        """
        UPDATE photos
        SET is_deleted=1,
            status='deleted',
            deleted_reason='admin_reset_results',
            deleted_at=NOW()
        WHERE is_deleted=0
          AND status='archived'
        """
    )


async def admin_reset_results_only() -> dict:
    """Delete only results/caches queues, keep photos untouched."""
    p = _assert_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            res = await _clear_results_tables(conn)
    return {
        **res,
        "archived_photos_deleted": 0,
        "total_rows_affected": int(res.get("results_rows_deleted") or 0),
    }


async def admin_reset_archives_only() -> dict:
    """Delete only archived photos, keep results/caches untouched."""
    p = _assert_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            archived_res = await _clear_archived_photos(conn)
    archived_deleted = _affected_count(archived_res)
    return {
        "notifications_deleted": 0,
        "result_ranks_deleted": 0,
        "daily_results_cache_deleted": 0,
        "results_v2_deleted": 0,
        "photo_day_metrics_deleted": 0,
        "alltime_cache_deleted": 0,
        "hall_of_fame_deleted": 0,
        "weekly_candidates_deleted": 0,
        "photo_repeats_deleted": 0,
        "my_results_deleted": 0,
        "results_rows_deleted": 0,
        "archived_photos_deleted": int(archived_deleted),
        "total_rows_affected": int(archived_deleted),
    }


async def get_results_reset_preview_counts() -> dict:
    """Preview counts for reset confirmations."""
    p = _assert_pool()
    async with p.acquire() as conn:
        archived_photos = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM photos
            WHERE is_deleted=0
              AND status='archived'
            """
        )
        notifications = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM notification_queue
            WHERE type='daily_results_top'
            """
        )
        days_in_cache = 0
        daily_cache_exists = await conn.fetchval("SELECT to_regclass('public.daily_results_cache')")
        if daily_cache_exists:
            days_in_cache = int(
                await conn.fetchval(
                    "SELECT COUNT(DISTINCT submit_day)::int FROM daily_results_cache"
                )
                or 0
            )

        table_counts: dict[str, int] = {}
        for table_name in _results_table_names():
            exists = await conn.fetchval("SELECT to_regclass($1)", f"public.{table_name}")
            if not exists:
                table_counts[table_name] = 0
                continue
            cnt = await conn.fetchval(f"SELECT COUNT(*)::int FROM {table_name}")
            table_counts[table_name] = int(cnt or 0)

    results_rows_total = int(notifications or 0) + int(sum(table_counts.values()))
    return {
        "archived_photos_count": int(archived_photos or 0),
        "results_rows_count": int(results_rows_total),
        "parties_days_count": int(days_in_cache),
        "notifications_count": int(notifications or 0),
        "result_ranks_count": int(table_counts.get("result_ranks", 0)),
        "daily_results_cache_count": int(table_counts.get("daily_results_cache", 0)),
        "results_v2_count": int(table_counts.get("results_v2", 0)),
        "photo_day_metrics_count": int(table_counts.get("photo_day_metrics", 0)),
        "alltime_cache_count": int(table_counts.get("alltime_cache", 0)),
        "hall_of_fame_count": int(table_counts.get("hall_of_fame", 0)),
        "weekly_candidates_count": int(table_counts.get("weekly_candidates", 0)),
        "photo_repeats_count": int(table_counts.get("photo_repeats", 0)),
        "my_results_count": int(table_counts.get("my_results", 0)),
    }


async def consume_credits_on_show(author_user_id: int, *, now: datetime | None = None) -> bool:
    """
    Spend author's credits into show tokens and consume one token per show.

    Logic:
      - show_tokens is an intermediate balance of ready-to-spend show impressions.
      - If tokens are zero, convert 1 credit into X tokens (HH multiplier).
      - If after conversion tokens are still zero -> return False (no inventory).
      - One photo show consumes exactly 1 token.
    """
    p = _assert_pool()
    now_dt = now or get_bot_now()
    economy = await get_effective_economy_settings()
    multiplier = _credit_multiplier_for_moment(now_dt, economy)
    async with p.acquire() as conn:
        stats = await _ensure_user_stats_row(conn, int(author_user_id))
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT credits, show_tokens FROM user_stats WHERE user_id=$1 FOR UPDATE",
                int(author_user_id),
            )
            if not row:
                return False
            credits = int(row["credits"] or 0)
            tokens = int(row["show_tokens"] or 0)
            if tokens <= 0 and credits > 0:
                credits -= 1
                tokens += multiplier
            if tokens <= 0:
                await conn.execute(
                    "UPDATE user_stats SET credits=$2, show_tokens=$3, last_active_at=$4 WHERE user_id=$1",
                    int(author_user_id),
                    credits,
                    tokens,
                    now_dt,
                )
                return False
            tokens -= 1
            await conn.execute(
                "UPDATE user_stats SET credits=$2, show_tokens=$3, last_active_at=$4 WHERE user_id=$1",
                int(author_user_id),
                credits,
                tokens,
                now_dt,
            )
            return True


async def consume_credits_on_rating(author_user_id: int, *, now: datetime | None = None) -> bool:
    """
    Spend exactly one show impression for a rating event.
    Fixed economics here: 2 impressions = 1 credit.
    """
    p = _assert_pool()
    now_dt = now or get_bot_now()
    economy = await get_effective_economy_settings()
    multiplier = _coerce_int(economy.get("credit_to_shows_normal"), 2, min_value=1, max_value=100)
    async with p.acquire() as conn:
        await _ensure_user_stats_row(conn, int(author_user_id))
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT credits, show_tokens FROM user_stats WHERE user_id=$1 FOR UPDATE",
                int(author_user_id),
            )
            if not row:
                return False
            credits = int(row["credits"] or 0)
            tokens = int(row["show_tokens"] or 0)
            if tokens <= 0 and credits > 0:
                credits -= 1
                tokens += multiplier
            if tokens <= 0:
                await conn.execute(
                    "UPDATE user_stats SET credits=$2, show_tokens=$3, last_active_at=$4 WHERE user_id=$1",
                    int(author_user_id),
                    credits,
                    tokens,
                    now_dt,
                )
                return False
            tokens -= 1
            await conn.execute(
                "UPDATE user_stats SET credits=$2, show_tokens=$3, last_active_at=$4 WHERE user_id=$1",
                int(author_user_id),
                credits,
                tokens,
                now_dt,
            )
            return True


async def happy_hour_multiplier(now: datetime | None = None) -> int:
    now_dt = now or get_bot_now()
    economy = await get_effective_economy_settings()
    return _credit_multiplier_for_moment(now_dt, economy)
# -------------------- Notifications settings (likes/comments) --------------------

async def _ensure_notify_tables(conn: asyncpg.Connection) -> None:
    """Create notification tables if they don't exist (safe for Postgres)."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_notify_settings (
            tg_id BIGINT PRIMARY KEY,
            likes_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            comments_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notify_likes_daily (
            tg_id BIGINT NOT NULL,
            day_key TEXT NOT NULL,
            likes_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tg_id, day_key)
        );
        """
    )


async def get_notify_settings_by_tg_id(tg_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_notify_tables(conn)
        await conn.execute(
            """
            INSERT INTO user_notify_settings (tg_id)
            VALUES ($1)
            ON CONFLICT (tg_id) DO NOTHING
            """,
            int(tg_id),
        )
        row = await conn.fetchrow(
            "SELECT tg_id, likes_enabled, comments_enabled FROM user_notify_settings WHERE tg_id=$1",
            int(tg_id),
        )
        if not row:
            return {"tg_id": int(tg_id), "likes_enabled": True, "comments_enabled": True}
        return dict(row)


async def toggle_likes_notify_by_tg_id(tg_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_notify_tables(conn)
        await conn.execute(
            """
            INSERT INTO user_notify_settings (tg_id)
            VALUES ($1)
            ON CONFLICT (tg_id) DO NOTHING
            """,
            int(tg_id),
        )
        await conn.execute(
            """
            UPDATE user_notify_settings
            SET likes_enabled = NOT likes_enabled,
                updated_at = NOW()
            WHERE tg_id=$1
            """,
            int(tg_id),
        )

    return await get_notify_settings_by_tg_id(int(tg_id))


async def toggle_comments_notify_by_tg_id(tg_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_notify_tables(conn)
        await conn.execute(
            """
            INSERT INTO user_notify_settings (tg_id)
            VALUES ($1)
            ON CONFLICT (tg_id) DO NOTHING
            """,
            int(tg_id),
        )
        await conn.execute(
            """
            UPDATE user_notify_settings
            SET comments_enabled = NOT comments_enabled,
                updated_at = NOW()
            WHERE tg_id=$1
            """,
            int(tg_id),
        )

    return await get_notify_settings_by_tg_id(int(tg_id))


async def increment_likes_daily_for_tg_id(tg_id: int, day_key: str, delta: int = 1) -> None:
    """Accumulate likes for daily summary notifications."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_notify_tables(conn)
        await conn.execute(
            """
            INSERT INTO notify_likes_daily (tg_id, day_key, likes_count)
            VALUES ($1,$2,$3)
            ON CONFLICT (tg_id, day_key)
            DO UPDATE SET likes_count = notify_likes_daily.likes_count + EXCLUDED.likes_count,
                          updated_at = NOW()
            """,
            int(tg_id),
            str(day_key),
            int(delta),
        )

# -------------------- UI state (menu / rating keyboard) --------------------

async def _ensure_ui_state_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_ui_state (
            tg_id BIGINT PRIMARY KEY,
            menu_msg_id BIGINT,
            rate_kb_msg_id BIGINT,
            screen_msg_id BIGINT,
            banner_msg_id BIGINT,
            rate_cards_seen INTEGER NOT NULL DEFAULT 0,
            rate_tutorial_seen BOOLEAN NOT NULL DEFAULT FALSE,
            update_notice_seen_ver INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    await conn.execute(
        "ALTER TABLE user_ui_state ADD COLUMN IF NOT EXISTS screen_msg_id BIGINT;"
    )
    await conn.execute(
        "ALTER TABLE user_ui_state ADD COLUMN IF NOT EXISTS banner_msg_id BIGINT;"
    )
    await conn.execute(
        "ALTER TABLE user_ui_state ADD COLUMN IF NOT EXISTS rate_cards_seen INTEGER NOT NULL DEFAULT 0;"
    )
    await conn.execute(
        "ALTER TABLE user_ui_state ADD COLUMN IF NOT EXISTS rate_tutorial_seen BOOLEAN NOT NULL DEFAULT FALSE;"
    )
    await conn.execute(
        "ALTER TABLE user_ui_state ADD COLUMN IF NOT EXISTS update_notice_seen_ver INTEGER NOT NULL DEFAULT 0;"
    )


# -------------------- App settings (tech mode) --------------------

async def _ensure_app_settings_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER PRIMARY KEY,
            tech_enabled INTEGER NOT NULL DEFAULT 0,
            tech_start_at TEXT,
            update_enabled INTEGER NOT NULL DEFAULT 0,
            update_notice_ver INTEGER NOT NULL DEFAULT 0,
            update_notice_text TEXT,
            updated_at TEXT NOT NULL
        );
        """
    )
    await conn.execute(
        """
        INSERT INTO app_settings (id, tech_enabled, tech_start_at, updated_at)
        VALUES (1, 0, NULL, $1)
        ON CONFLICT (id) DO NOTHING
        """,
        get_moscow_now_iso(),
    )
    await conn.execute(
        "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS update_enabled INTEGER NOT NULL DEFAULT 0;"
    )
    await conn.execute(
        "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS update_notice_ver INTEGER NOT NULL DEFAULT 0;"
    )
    await conn.execute(
        "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS update_notice_text TEXT;"
    )
    await conn.execute(
        "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS upload_blocked INTEGER NOT NULL DEFAULT 0;"
    )
    await conn.execute(
        "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS rating_blocked INTEGER NOT NULL DEFAULT 0;"
    )
    await conn.execute(
        "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS results_blocked INTEGER NOT NULL DEFAULT 0;"
    )
    await conn.execute(
        "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS profile_blocked INTEGER NOT NULL DEFAULT 0;"
    )


async def _ensure_admin_settings_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_admin_settings_updated_at
        ON admin_settings (updated_at DESC);
        """
    )


async def get_setting(key: str, default: object = None) -> object:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_admin_settings_table(conn)
        row = await conn.fetchrow(
            "SELECT value FROM admin_settings WHERE key=$1",
            str(key),
        )
    if not row:
        return default
    return row.get("value")


async def set_setting(key: str, value: object) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_admin_settings_table(conn)
        await conn.execute(
            """
            INSERT INTO admin_settings (key, value, updated_at)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (key)
            DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
            """,
            str(key),
            json.dumps(value),
        )


async def delete_setting(key: str) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_admin_settings_table(conn)
        await conn.execute(
            "DELETE FROM admin_settings WHERE key=$1",
            str(key),
        )


async def get_settings_bulk(keys: list[str]) -> dict[str, object]:
    safe_keys = [str(k) for k in keys if str(k).strip()]
    if not safe_keys:
        return {}
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_admin_settings_table(conn)
        rows = await conn.fetch(
            """
            SELECT key, value
            FROM admin_settings
            WHERE key = ANY($1::text[])
            """,
            safe_keys,
        )
    return {str(r.get("key")): r.get("value") for r in rows}


async def set_settings_bulk(values: dict[str, object]) -> None:
    if not values:
        return
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_admin_settings_table(conn)
        async with conn.transaction():
            for k, v in values.items():
                await conn.execute(
                    """
                    INSERT INTO admin_settings (key, value, updated_at)
                    VALUES ($1, $2::jsonb, NOW())
                    ON CONFLICT (key)
                    DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
                    """,
                    str(k),
                    json.dumps(v),
                )


_TECH_NOTICE_KEY = "tech.notice_text"
_ACCESS_SETTING_KEYS = {
    "upload": "access.upload_blocked",
    "rate": "access.rating_blocked",
    "results": "access.results_blocked",
    "profile": "access.profile_blocked",
}


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return bool(int(value))
    try:
        s = str(value).strip().lower()
    except Exception:
        return bool(default)
    if s in {"1", "true", "yes", "on", "y", "t"}:
        return True
    if s in {"0", "false", "no", "off", "n", "f"}:
        return False
    return bool(default)


def _coerce_int(value: object, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    if min_value is not None and out < int(min_value):
        out = int(min_value)
    if max_value is not None and out > int(max_value):
        out = int(max_value)
    return out


def _coerce_float(
    value: object,
    default: float,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    try:
        out = float(value)
    except Exception:
        out = float(default)
    if min_value is not None and out < float(min_value):
        out = float(min_value)
    if max_value is not None and out > float(max_value):
        out = float(max_value)
    return out


async def _ensure_scheduled_broadcasts_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
            id BIGSERIAL PRIMARY KEY,
            target TEXT NOT NULL,
            text TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_by_tg_id BIGINT,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            total_count INTEGER,
            sent_count INTEGER,
            error_text TEXT,
            updated_at TEXT
        );
        """
    )


async def get_user_ui_state(tg_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_ui_state_table(conn)
        row = await conn.fetchrow(
            """
            SELECT menu_msg_id, rate_kb_msg_id, screen_msg_id, banner_msg_id, rate_cards_seen, rate_tutorial_seen, update_notice_seen_ver, updated_at
            FROM user_ui_state
            WHERE tg_id=$1
            """,
            int(tg_id),
        )
        if not row:
            return {
                "menu_msg_id": None,
                "rate_kb_msg_id": None,
                "screen_msg_id": None,
                "banner_msg_id": None,
                "rate_cards_seen": 0,
                "rate_tutorial_seen": False,
                "update_notice_seen_ver": 0,
                "updated_at": None,
            }
        d = dict(row)
        d.setdefault("rate_cards_seen", 0)
        d.setdefault("update_notice_seen_ver", 0)
        return d


async def set_user_menu_msg_id(tg_id: int, menu_msg_id: int | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_ui_state_table(conn)
        await conn.execute(
            """
            INSERT INTO user_ui_state (tg_id, menu_msg_id, updated_at)
            VALUES ($1,$2,NOW())
            ON CONFLICT (tg_id)
            DO UPDATE SET menu_msg_id=$2, updated_at=NOW()
            """,
            int(tg_id),
            int(menu_msg_id) if menu_msg_id is not None else None,
        )


async def set_user_rate_kb_msg_id(tg_id: int, rate_kb_msg_id: int | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_ui_state_table(conn)
        await conn.execute(
            """
            INSERT INTO user_ui_state (tg_id, rate_kb_msg_id, updated_at)
            VALUES ($1,$2,NOW())
            ON CONFLICT (tg_id)
            DO UPDATE SET rate_kb_msg_id=$2, updated_at=NOW()
            """,
            int(tg_id),
            int(rate_kb_msg_id) if rate_kb_msg_id is not None else None,
        )


async def set_user_screen_msg_id(tg_id: int, screen_msg_id: int | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_ui_state_table(conn)
        await conn.execute(
            """
            INSERT INTO user_ui_state (tg_id, screen_msg_id, updated_at)
            VALUES ($1,$2,NOW())
            ON CONFLICT (tg_id)
            DO UPDATE SET screen_msg_id=$2, updated_at=NOW()
            """,
            int(tg_id),
            int(screen_msg_id) if screen_msg_id is not None else None,
        )


async def set_user_banner_msg_id(tg_id: int, banner_msg_id: int | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_ui_state_table(conn)
        await conn.execute(
            """
            INSERT INTO user_ui_state (tg_id, banner_msg_id, updated_at)
            VALUES ($1,$2,NOW())
            ON CONFLICT (tg_id)
            DO UPDATE SET banner_msg_id=$2, updated_at=NOW()
            """,
            int(tg_id),
            int(banner_msg_id) if banner_msg_id is not None else None,
        )


async def set_user_rate_tutorial_seen(tg_id: int, seen: bool = True) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_ui_state_table(conn)
        await conn.execute(
            """
            INSERT INTO user_ui_state (tg_id, rate_tutorial_seen, updated_at)
            VALUES ($1,$2,NOW())
            ON CONFLICT (tg_id)
            DO UPDATE SET rate_tutorial_seen=$2, updated_at=NOW()
            """,
            int(tg_id),
            bool(seen),
        )


async def set_user_rate_cards_seen(tg_id: int, value: int) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_ui_state_table(conn)
        await conn.execute(
            """
            INSERT INTO user_ui_state (tg_id, rate_cards_seen, updated_at)
            VALUES ($1,$2,NOW())
            ON CONFLICT (tg_id)
            DO UPDATE SET rate_cards_seen=$2, updated_at=NOW()
            """,
            int(tg_id),
            max(0, int(value)),
        )

# -------------------- Tech mode settings --------------------

async def get_tech_mode_state() -> dict:
    """Return tech mode state."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_app_settings_table(conn)
        await _ensure_admin_settings_table(conn)
        row = await conn.fetchrow(
            "SELECT tech_enabled, tech_start_at FROM app_settings WHERE id=1"
        )
        notice = await conn.fetchval(
            "SELECT value FROM admin_settings WHERE key=$1",
            _TECH_NOTICE_KEY,
        )
        if not row:
            return {"tech_enabled": False, "tech_start_at": None, "tech_notice_text": None}
        return {
            "tech_enabled": bool(row.get("tech_enabled")),
            "tech_start_at": row.get("tech_start_at"),
            "tech_notice_text": str(notice).strip() if notice is not None else None,
        }


_UNSET = object()


async def set_tech_mode_state(
    *,
    enabled: bool,
    start_at: str | None,
    notice_text: str | None | object = _UNSET,
) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_app_settings_table(conn)
        await _ensure_admin_settings_table(conn)
        await conn.execute(
            """
            UPDATE app_settings
            SET tech_enabled=$1, tech_start_at=$2, updated_at=$3
            WHERE id=1
            """,
            1 if enabled else 0,
            start_at,
            get_moscow_now_iso(),
        )
        if notice_text is not _UNSET:
            text_value = (str(notice_text).strip() if notice_text is not None else "")
            if text_value:
                await conn.execute(
                    """
                    INSERT INTO admin_settings (key, value, updated_at)
                    VALUES ($1, $2::jsonb, NOW())
                    ON CONFLICT (key)
                    DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
                    """,
                    _TECH_NOTICE_KEY,
                    json.dumps(text_value),
                )
            else:
                await conn.execute(
                    "DELETE FROM admin_settings WHERE key=$1",
                    _TECH_NOTICE_KEY,
                )

# -------------------- Update mode (обновление) --------------------

async def get_update_mode_state() -> dict:
    """
    Return update mode state:
    {
      update_enabled: bool,
      update_notice_ver: int,
      update_notice_text: str | None
    }
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_app_settings_table(conn)
        row = await conn.fetchrow(
            "SELECT update_enabled, update_notice_ver, update_notice_text FROM app_settings WHERE id=1"
        )
        if not row:
            return {"update_enabled": False, "update_notice_ver": 0, "update_notice_text": None}
        return {
            "update_enabled": bool(row.get("update_enabled")),
            "update_notice_ver": int(row.get("update_notice_ver") or 0),
            "update_notice_text": row.get("update_notice_text"),
        }


async def set_update_mode_state(
    *,
    enabled: bool,
    notice_text: str | None = None,
    bump_version: bool = False,
) -> None:
    """
    Enable/disable update mode. If bump_version=True, increments notice version to resend one-time message.
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_app_settings_table(conn)
        if bump_version:
            await conn.execute(
                """
                UPDATE app_settings
                SET update_enabled=$1,
                    update_notice_ver=COALESCE(update_notice_ver,0)+1,
                    update_notice_text=COALESCE($2, update_notice_text),
                    updated_at=$3
                WHERE id=1
                """,
                1 if enabled else 0,
                notice_text,
                get_moscow_now_iso(),
            )
        else:
            await conn.execute(
                """
                UPDATE app_settings
                SET update_enabled=$1,
                    update_notice_text=COALESCE($2, update_notice_text),
                    updated_at=$3
                WHERE id=1
                """,
                1 if enabled else 0,
                notice_text,
                get_moscow_now_iso(),
            )


_SECTION_BLOCK_COLUMNS = {
    "upload": "upload_blocked",
    "rate": "rating_blocked",
    "results": "results_blocked",
    "profile": "profile_blocked",
}


async def get_section_access_state() -> dict:
    """Return section access flags (True means blocked)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_app_settings_table(conn)
        await _ensure_admin_settings_table(conn)
        row = await conn.fetchrow(
            """
            SELECT upload_blocked, rating_blocked, results_blocked, profile_blocked
            FROM app_settings
            WHERE id=1
            """
        )
        settings_rows = await conn.fetch(
            """
            SELECT key, value
            FROM admin_settings
            WHERE key = ANY($1::text[])
            """,
            list(_ACCESS_SETTING_KEYS.values()),
        )
        saved = {str(r.get("key")): r.get("value") for r in settings_rows}
        if not row:
            row = {}
        return {
            "upload_blocked": _coerce_bool(
                saved.get(_ACCESS_SETTING_KEYS["upload"]),
                bool((row or {}).get("upload_blocked")),
            ),
            "rating_blocked": _coerce_bool(
                saved.get(_ACCESS_SETTING_KEYS["rate"]),
                bool((row or {}).get("rating_blocked")),
            ),
            "results_blocked": _coerce_bool(
                saved.get(_ACCESS_SETTING_KEYS["results"]),
                bool((row or {}).get("results_blocked")),
            ),
            "profile_blocked": _coerce_bool(
                saved.get(_ACCESS_SETTING_KEYS["profile"]),
                bool((row or {}).get("profile_blocked")),
            ),
        }


async def set_section_blocked(section: str, blocked: bool) -> dict:
    key = str(section or "").strip().lower()
    column = _SECTION_BLOCK_COLUMNS.get(key)
    if column is None:
        raise ValueError("unknown section")

    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_app_settings_table(conn)
        await _ensure_admin_settings_table(conn)
        await conn.execute(
            f"UPDATE app_settings SET {column}=$1, updated_at=$2 WHERE id=1",
            1 if blocked else 0,
            get_moscow_now_iso(),
        )
        await conn.execute(
            """
            INSERT INTO admin_settings (key, value, updated_at)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (key)
            DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
            """,
            _ACCESS_SETTING_KEYS[key],
            json.dumps(bool(blocked)),
        )
    return await get_section_access_state()


async def toggle_section_blocked(section: str) -> dict:
    key = str(section or "").strip().lower()
    column = _SECTION_BLOCK_COLUMNS.get(key)
    if column is None:
        raise ValueError("unknown section")

    current_state = await get_section_access_state()
    current = bool(current_state.get(f"{'rating' if key == 'rate' else key}_blocked"))
    return await set_section_blocked(key, not current)


async def is_section_blocked(section: str) -> bool:
    key = str(section or "").strip().lower()
    if key not in _SECTION_BLOCK_COLUMNS:
        return False
    st = await get_section_access_state()
    field = f"{'rating' if key == 'rate' else key}_blocked"
    return bool(st.get(field))


async def set_section_access_state(
    *,
    upload_blocked: bool,
    rating_blocked: bool,
    results_blocked: bool,
    profile_blocked: bool,
) -> dict:
    await set_section_blocked("upload", bool(upload_blocked))
    await set_section_blocked("rate", bool(rating_blocked))
    await set_section_blocked("results", bool(results_blocked))
    await set_section_blocked("profile", bool(profile_blocked))
    return await get_section_access_state()


async def apply_access_preset(preset: str) -> dict:
    name = str(preset or "").strip().lower()
    if name == "normal":
        return await set_section_access_state(
            upload_blocked=False,
            rating_blocked=False,
            results_blocked=False,
            profile_blocked=False,
        )
    if name == "upload_off":
        return await set_section_access_state(
            upload_blocked=True,
            rating_blocked=False,
            results_blocked=False,
            profile_blocked=False,
        )
    if name == "rate_off":
        return await set_section_access_state(
            upload_blocked=False,
            rating_blocked=True,
            results_blocked=False,
            profile_blocked=False,
        )
    if name == "read_only":
        return await set_section_access_state(
            upload_blocked=True,
            rating_blocked=True,
            results_blocked=False,
            profile_blocked=False,
        )
    if name == "full_lock":
        return await set_section_access_state(
            upload_blocked=True,
            rating_blocked=True,
            results_blocked=True,
            profile_blocked=True,
        )
    raise ValueError("unknown preset")


_ECONOMY_SETTING_KEYS = [
    "economy.daily_free_credits_normal",
    "economy.daily_free_credits_premium",
    "economy.daily_free_credits_author",
    "economy.publish_bonus_credits",
    "economy.credit_to_shows_normal",
    "economy.credit_to_shows_happyhour",
    "economy.happy_hour_enabled",
    "economy.happy_hour_start_hour",
    "economy.happy_hour_duration_minutes",
    "economy.tail_probability",
    "economy.min_votes_for_normal_feed",
    "economy.max_active_photos_normal",
    "economy.max_active_photos_author",
    "economy.max_active_photos_premium",
]

_ADS_SETTING_KEYS = [
    "ads.enabled",
    "ads.frequency_n",
    "ads.only_nonpremium",
]

_PROTECTION_SETTING_KEYS = [
    "protection.mode_enabled",
    "protection.max_callbacks_per_minute_per_user",
    "protection.max_actions_per_10s",
    "protection.cooldown_on_spike_seconds",
    "protection.spam_suspect_threshold",
]


def _economy_defaults() -> dict[str, object]:
    from config import CREDIT_SHOWS_BASE, CREDIT_SHOWS_HAPPY, TAIL_PROBABILITY, MIN_VOTES_FOR_NORMAL_FEED
    return {
        "daily_free_credits_normal": 2,
        "daily_free_credits_premium": 3,
        "daily_free_credits_author": 3,
        "publish_bonus_credits": 2,
        "credit_to_shows_normal": int(CREDIT_SHOWS_BASE),
        "credit_to_shows_happyhour": int(CREDIT_SHOWS_HAPPY),
        "happy_hour_enabled": True,
        "happy_hour_start_hour": 15,
        "happy_hour_duration_minutes": 60,
        "tail_probability": float(TAIL_PROBABILITY),
        "min_votes_for_normal_feed": int(MIN_VOTES_FOR_NORMAL_FEED),
        "max_active_photos_normal": 2,
        "max_active_photos_author": 2,
        "max_active_photos_premium": 2,
    }


def _ads_defaults() -> dict[str, object]:
    return {
        "enabled": True,
        "frequency_n": 3,
        "only_nonpremium": True,
    }


def _protection_defaults() -> dict[str, object]:
    return {
        "mode_enabled": False,
        "max_callbacks_per_minute_per_user": 120,
        "max_actions_per_10s": 20,
        "cooldown_on_spike_seconds": 10,
        "spam_suspect_threshold": 120,
    }


async def get_effective_economy_settings() -> dict:
    defaults = _economy_defaults()
    raw = await get_settings_bulk(_ECONOMY_SETTING_KEYS)
    return {
        "daily_free_credits_normal": _coerce_int(raw.get("economy.daily_free_credits_normal"), int(defaults["daily_free_credits_normal"]), min_value=0, max_value=100),
        "daily_free_credits_premium": _coerce_int(raw.get("economy.daily_free_credits_premium"), int(defaults["daily_free_credits_premium"]), min_value=0, max_value=100),
        "daily_free_credits_author": _coerce_int(raw.get("economy.daily_free_credits_author"), int(defaults["daily_free_credits_author"]), min_value=0, max_value=100),
        "publish_bonus_credits": _coerce_int(raw.get("economy.publish_bonus_credits"), int(defaults["publish_bonus_credits"]), min_value=0, max_value=100),
        "credit_to_shows_normal": _coerce_int(raw.get("economy.credit_to_shows_normal"), int(defaults["credit_to_shows_normal"]), min_value=1, max_value=100),
        "credit_to_shows_happyhour": _coerce_int(raw.get("economy.credit_to_shows_happyhour"), int(defaults["credit_to_shows_happyhour"]), min_value=1, max_value=100),
        "happy_hour_enabled": _coerce_bool(raw.get("economy.happy_hour_enabled"), bool(defaults["happy_hour_enabled"])),
        "happy_hour_start_hour": _coerce_int(raw.get("economy.happy_hour_start_hour"), int(defaults["happy_hour_start_hour"]), min_value=0, max_value=23),
        "happy_hour_duration_minutes": _coerce_int(raw.get("economy.happy_hour_duration_minutes"), int(defaults["happy_hour_duration_minutes"]), min_value=1, max_value=1440),
        "tail_probability": _coerce_float(raw.get("economy.tail_probability"), float(defaults["tail_probability"]), min_value=0.0, max_value=1.0),
        "min_votes_for_normal_feed": _coerce_int(raw.get("economy.min_votes_for_normal_feed"), int(defaults["min_votes_for_normal_feed"]), min_value=0, max_value=500),
        "max_active_photos_normal": _coerce_int(raw.get("economy.max_active_photos_normal"), int(defaults["max_active_photos_normal"]), min_value=1, max_value=20),
        "max_active_photos_author": _coerce_int(raw.get("economy.max_active_photos_author"), int(defaults["max_active_photos_author"]), min_value=1, max_value=20),
        "max_active_photos_premium": _coerce_int(raw.get("economy.max_active_photos_premium"), int(defaults["max_active_photos_premium"]), min_value=1, max_value=20),
    }


async def get_effective_ads_settings() -> dict:
    defaults = _ads_defaults()
    raw = await get_settings_bulk(_ADS_SETTING_KEYS)
    return {
        "enabled": _coerce_bool(raw.get("ads.enabled"), bool(defaults["enabled"])),
        "frequency_n": _coerce_int(raw.get("ads.frequency_n"), int(defaults["frequency_n"]), min_value=1, max_value=1000),
        "only_nonpremium": _coerce_bool(raw.get("ads.only_nonpremium"), bool(defaults["only_nonpremium"])),
    }


async def get_effective_protection_settings() -> dict:
    defaults = _protection_defaults()
    raw = await get_settings_bulk(_PROTECTION_SETTING_KEYS)
    return {
        "mode_enabled": _coerce_bool(raw.get("protection.mode_enabled"), bool(defaults["mode_enabled"])),
        "max_callbacks_per_minute_per_user": _coerce_int(
            raw.get("protection.max_callbacks_per_minute_per_user"),
            int(defaults["max_callbacks_per_minute_per_user"]),
            min_value=10,
            max_value=2000,
        ),
        "max_actions_per_10s": _coerce_int(
            raw.get("protection.max_actions_per_10s"),
            int(defaults["max_actions_per_10s"]),
            min_value=1,
            max_value=500,
        ),
        "cooldown_on_spike_seconds": _coerce_int(
            raw.get("protection.cooldown_on_spike_seconds"),
            int(defaults["cooldown_on_spike_seconds"]),
            min_value=0,
            max_value=300,
        ),
        "spam_suspect_threshold": _coerce_int(
            raw.get("protection.spam_suspect_threshold"),
            int(defaults["spam_suspect_threshold"]),
            min_value=1,
            max_value=5000,
        ),
    }


async def get_user_update_notice_ver(tg_id: int) -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_ui_state_table(conn)
        row = await conn.fetchrow(
            "SELECT update_notice_seen_ver FROM user_ui_state WHERE tg_id=$1",
            int(tg_id),
        )
        return int(row.get("update_notice_seen_ver")) if row else 0


async def set_user_update_notice_ver(tg_id: int, version: int) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_ui_state_table(conn)
        await conn.execute(
            """
            INSERT INTO user_ui_state (tg_id, update_notice_seen_ver, updated_at)
            VALUES ($1,$2,$3)
            ON CONFLICT (tg_id)
            DO UPDATE SET update_notice_seen_ver=$2, updated_at=$3
            """,
            int(tg_id),
            int(version),
            get_moscow_now_iso(),
        )

# -------------------- Scheduled broadcasts --------------------

async def create_scheduled_broadcast(
    *,
    target: str,
    text: str,
    scheduled_at_iso: str,
    created_by_tg_id: int | None = None,
) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_scheduled_broadcasts_table(conn)
        now = get_moscow_now_iso()
        row = await conn.fetchrow(
            """
            INSERT INTO scheduled_broadcasts (target, text, scheduled_at, status, created_by_tg_id, created_at, updated_at)
            VALUES ($1,$2,$3,'pending',$4,$5,$5)
            RETURNING *
            """,
            str(target),
            str(text),
            str(scheduled_at_iso),
            int(created_by_tg_id) if created_by_tg_id is not None else None,
            now,
        )
    return dict(row) if row else {}


async def get_due_scheduled_broadcasts(limit: int = 10) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_scheduled_broadcasts_table(conn)
        rows = await conn.fetch(
            """
            SELECT *
            FROM scheduled_broadcasts
            WHERE status='pending'
              AND scheduled_at::timestamp <= $1::timestamp
            ORDER BY scheduled_at ASC, id ASC
            LIMIT $2
            """,
            get_moscow_now_iso(),
            int(limit),
        )
    return [dict(r) for r in rows]


async def mark_scheduled_broadcast_sent(
    broadcast_id: int,
    *,
    total_count: int,
    sent_count: int,
) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_scheduled_broadcasts_table(conn)
        await conn.execute(
            """
            UPDATE scheduled_broadcasts
            SET status='sent',
                sent_at=$2,
                total_count=$3,
                sent_count=$4,
                updated_at=$2
            WHERE id=$1
            """,
            int(broadcast_id),
            get_moscow_now_iso(),
            int(total_count),
            int(sent_count),
        )


async def mark_scheduled_broadcast_failed(broadcast_id: int, error_text: str | None = None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_scheduled_broadcasts_table(conn)
        await conn.execute(
            """
            UPDATE scheduled_broadcasts
            SET status='failed',
                error_text=$2,
                updated_at=$3
            WHERE id=$1
            """,
            int(broadcast_id),
            (error_text or "")[:1000] if error_text else None,
            get_moscow_now_iso(),
        )


async def list_scheduled_broadcasts(
    *,
    status: str | None = "pending",
    limit: int = 20,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_scheduled_broadcasts_table(conn)
        if status:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM scheduled_broadcasts WHERE status=$1",
                str(status),
            )
            rows = await conn.fetch(
                """
                SELECT *
                FROM scheduled_broadcasts
                WHERE status=$1
                ORDER BY scheduled_at ASC, id ASC
                OFFSET $2 LIMIT $3
                """,
                str(status),
                int(offset),
                int(limit),
            )
        else:
            total = await conn.fetchval("SELECT COUNT(*) FROM scheduled_broadcasts")
            rows = await conn.fetch(
                """
                SELECT *
                FROM scheduled_broadcasts
                ORDER BY scheduled_at ASC, id ASC
                OFFSET $1 LIMIT $2
                """,
                int(offset),
                int(limit),
            )
    return int(total or 0), [dict(r) for r in rows]


async def cancel_scheduled_broadcast(broadcast_id: int) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_scheduled_broadcasts_table(conn)
        await conn.execute(
            """
            UPDATE scheduled_broadcasts
            SET status='canceled',
                updated_at=$2
            WHERE id=$1 AND status='pending'
            """,
            int(broadcast_id),
            get_moscow_now_iso(),
        )

# -------------------- Feedback ideas --------------------

async def _ensure_feedback_tables(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_ideas (
            id BIGSERIAL PRIMARY KEY,
            idea_code BIGINT UNIQUE NOT NULL,
            tg_id BIGINT NOT NULL,
            username TEXT,
            text TEXT,
            attachments JSONB,
            status TEXT NOT NULL DEFAULT 'new',
            cancel_reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_ideas_tg_id ON feedback_ideas (tg_id);"
    )


async def create_feedback_idea(
    *,
    tg_id: int,
    username: str | None,
    text: str | None,
    attachments: list[dict] | None,
) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_feedback_tables(conn)

        if attachments is None:
            attachments_json = "[]"
        elif isinstance(attachments, str):
            attachments_json = attachments
        else:
            try:
                attachments_json = json.dumps(attachments, ensure_ascii=False)
            except Exception:
                attachments_json = "[]"

        for _ in range(6):
            idea_code = random.randint(100000, 999999)
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO feedback_ideas (idea_code, tg_id, username, text, attachments)
                    VALUES ($1,$2,$3,$4,$5)
                    RETURNING id, idea_code, created_at
                    """,
                    int(idea_code),
                    int(tg_id),
                    username,
                    text,
                    attachments_json,
                )
                return dict(row)
            except UniqueViolationError:
                continue

        # Fallback: larger random space
        idea_code = random.randint(100000000, 999999999)
        row = await conn.fetchrow(
            """
            INSERT INTO feedback_ideas (idea_code, tg_id, username, text, attachments)
            VALUES ($1,$2,$3,$4,$5)
            RETURNING id, idea_code, created_at
            """,
            int(idea_code),
            int(tg_id),
            username,
            text,
            attachments_json,
        )
        return dict(row)


async def list_feedback_ideas_by_tg_id(tg_id: int, limit: int = 20) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_feedback_tables(conn)
        rows = await conn.fetch(
            """
            SELECT id, idea_code, status, cancel_reason, created_at, text
            FROM feedback_ideas
            WHERE tg_id=$1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            int(tg_id),
            int(limit),
        )
        return [dict(r) for r in rows]


async def get_feedback_idea_by_id(idea_id: int) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_feedback_tables(conn)
        row = await conn.fetchrow(
            "SELECT * FROM feedback_ideas WHERE id=$1",
            int(idea_id),
        )
        return dict(row) if row else None


async def set_feedback_status(idea_id: int, status: str, reason: str | None = None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_feedback_tables(conn)
        await conn.execute(
            """
            UPDATE feedback_ideas
            SET status=$2,
                cancel_reason=$3,
                updated_at=NOW()
            WHERE id=$1
            """,
            int(idea_id),
            str(status),
            reason,
        )


# -------------------- ads --------------------

async def create_ad(title: str, body: str, is_active: bool = True) -> int:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO ads (title, body, is_active, created_at)
            VALUES ($1,$2,$3,$4)
            RETURNING id
            """,
            str(title),
            str(body),
            1 if is_active else 0,
            now,
        )
    return int(row["id"]) if row else 0


async def list_ads(limit: int = 50) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, body, is_active, created_at
            FROM ads
            ORDER BY created_at DESC, id DESC
            LIMIT $1
            """,
            int(limit),
        )
    return [dict(r) for r in rows]


async def set_ad_active(ad_id: int, active: bool) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE ads SET is_active=$2 WHERE id=$1",
            int(ad_id),
            1 if active else 0,
        )


async def delete_ad(ad_id: int) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM ads WHERE id=$1", int(ad_id))


async def get_random_active_ad() -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, title, body
            FROM ads
            WHERE is_active=1
            ORDER BY random()
            LIMIT 1
            """
        )
    return dict(row) if row else None

# -------------------- streak (🔥) API --------------------

_STREAK_DAILY_RATINGS = int(STREAK_DAILY_RATINGS)
_STREAK_DAILY_COMMENTS = int(STREAK_DAILY_COMMENTS)
_STREAK_DAILY_UPLOADS = int(STREAK_DAILY_UPLOADS)
_STREAK_GRACE_HOURS = int(STREAK_GRACE_HOURS)
_STREAK_MAX_NUDGES_PER_DAY = int(STREAK_MAX_NUDGES_PER_DAY)
_STREAK_REWARD_THRESHOLD = 111
_STREAK_REWARD_PREMIUM_DAYS = 11


def _row_val(row, key: str, default=None):
    try:
        return row[key]
    except Exception:
        return default


def _streak_target_day_key(now_dt: datetime) -> str:
    """During grace window after midnight attribute actions to yesterday."""
    try:
        grace_time = datetime(now_dt.year, now_dt.month, now_dt.day, _STREAK_GRACE_HOURS, 0, 0).time()
        if now_dt.time() < grace_time:
            return (now_dt.date() - timedelta(days=1)).isoformat()
        return now_dt.date().isoformat()
    except Exception:
        return _today_key()


def _streak_goal_done(rated: int, commented: int, uploaded: int) -> bool:
    return (
        uploaded >= _STREAK_DAILY_UPLOADS
        or rated >= _STREAK_DAILY_RATINGS
        or commented >= _STREAK_DAILY_COMMENTS
    )


async def streak_ensure_user_row(tg_id: int) -> None:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_streak (tg_id, visible, reward_111_given, created_at, updated_at)
            VALUES ($1,1,0,$2,$2)
            ON CONFLICT (tg_id) DO NOTHING
            """,
            int(tg_id), now
        )


async def streak_get_status_by_tg_id(tg_id: int) -> dict:
    await streak_ensure_user_row(int(tg_id))
    now_dt = get_moscow_now()
    today_key = now_dt.date().isoformat()

    p = _assert_pool()
    async with p.acquire() as conn:
        u = await conn.fetchrow("SELECT * FROM user_streak WHERE tg_id=$1", int(tg_id))
        await conn.execute(
            """
            INSERT INTO streak_daily (tg_id, day_key)
            VALUES ($1,$2)
            ON CONFLICT (tg_id, day_key) DO NOTHING
            """,
            int(tg_id), str(today_key)
        )
        d = await conn.fetchrow(
            "SELECT * FROM streak_daily WHERE tg_id=$1 AND day_key=$2",
            int(tg_id), str(today_key)
        )

    return {
        "tg_id": int(tg_id),
        "today_key": str(today_key),
        "streak": int(_row_val(u, "streak", 0) or 0) if u else 0,
        "best_streak": int(_row_val(u, "best_streak", 0) or 0) if u else 0,
        "freeze_tokens": int(_row_val(u, "freeze_tokens", 0) or 0) if u else 0,
        "last_completed_day": str(_row_val(u, "last_completed_day")) if u and _row_val(u, "last_completed_day") else None,
        "notify_enabled": bool(int(_row_val(u, "notify_enabled", 1) or 0)) if u else True,
        "notify_hour": int(_row_val(u, "notify_hour", 21) or 21) if u else 21,
        "notify_minute": int(_row_val(u, "notify_minute", 0) or 0) if u else 0,
        "visible": bool(int(_row_val(u, "visible", 1) or 1)) if u else True,
        "reward_111_given": bool(int(_row_val(u, "reward_111_given", 0) or 0)) if u else False,
        "rated_today": int(_row_val(d, "rated_count", 0) or 0) if d else 0,
        "commented_today": int(_row_val(d, "comment_count", 0) or 0) if d else 0,
        "uploaded_today": int(_row_val(d, "upload_count", 0) or 0) if d else 0,
        "goal_done_today": bool(int(_row_val(d, "goal_done", 0) or 0)) if d else False,
    }


async def streak_toggle_notify_by_tg_id(tg_id: int) -> bool:
    """Toggle streak reminder notifications for the user.

    Returns the new value (True = enabled).
    """
    await streak_ensure_user_row(int(tg_id))
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "UPDATE user_streak SET notify_enabled = 1 - COALESCE(notify_enabled, 1), updated_at=$2 WHERE tg_id=$1 RETURNING notify_enabled",
            int(tg_id),
            get_moscow_now_iso(),
        )
    return bool(int(v or 0))


async def streak_toggle_visibility_by_tg_id(tg_id: int) -> bool:
    """Toggle public visibility of streak badge."""
    await streak_ensure_user_row(int(tg_id))
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "UPDATE user_streak SET visible = 1 - COALESCE(visible, 1), updated_at=$2 WHERE tg_id=$1 RETURNING visible",
            int(tg_id),
            get_moscow_now_iso(),
        )
    return bool(int(v or 0))


def _extend_premium_until(current_until: str | None, days: int, *, now_dt: datetime) -> str | None:
    """Extend premium by days. Forever stays forever (None)."""
    if days <= 0:
        raise ValueError("days must be positive")
    if current_until is None:
        return None

    base = now_dt
    try:
        dt = datetime.fromisoformat(str(current_until))
        if dt > now_dt:
            base = dt
    except Exception:
        base = now_dt

    new_dt = base + timedelta(days=days)
    new_dt = new_dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return new_dt.isoformat()


async def _grant_streak_reward_if_needed(
    tg_id: int,
    best_before: int,
    best_after: int,
) -> None:
    """Grant reward once when crossing the threshold."""
    if best_before >= _STREAK_REWARD_THRESHOLD:
        return
    if best_after < _STREAK_REWARD_THRESHOLD:
        return

    await streak_ensure_user_row(int(tg_id))
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT reward_111_given FROM user_streak WHERE tg_id=$1",
            int(tg_id),
        )
        if not row or bool(int(row["reward_111_given"] or 0)):
            return

        user = await get_user_by_tg_id(int(tg_id))
        if not user:
            return

        current_until = user.get("premium_until")
        now_dt = get_moscow_now()
        try:
            new_until = _extend_premium_until(current_until, _STREAK_REWARD_PREMIUM_DAYS, now_dt=now_dt)
        except Exception:
            new_until = None if current_until is None else (get_moscow_now() + timedelta(days=_STREAK_REWARD_PREMIUM_DAYS)).isoformat()

        await set_user_premium_status(int(tg_id), True, premium_until=new_until)
        await conn.execute(
            "UPDATE user_streak SET reward_111_given=1, updated_at=$2 WHERE tg_id=$1",
            int(tg_id),
            get_moscow_now_iso(),
        )


async def streak_record_action_by_tg_id(tg_id: int, action: str) -> dict:
    await streak_ensure_user_row(int(tg_id))
    p = _assert_pool()

    now_dt = get_moscow_now()
    now_iso = get_moscow_now_iso()
    day_key = _streak_target_day_key(now_dt)

    async with p.acquire() as conn:
        await conn.execute(
            "INSERT INTO streak_actions (tg_id, action, created_at) VALUES ($1,$2,$3)",
            int(tg_id), str(action), now_iso
        )

        await conn.execute(
            """
            INSERT INTO streak_daily (tg_id, day_key)
            VALUES ($1,$2)
            ON CONFLICT (tg_id, day_key) DO NOTHING
            """,
            int(tg_id), str(day_key)
        )

        d = await conn.fetchrow(
            "SELECT * FROM streak_daily WHERE tg_id=$1 AND day_key=$2",
            int(tg_id), str(day_key)
        )

        rated = int(d["rated_count"] or 0) if d else 0
        comm = int(d["comment_count"] or 0) if d else 0
        upl = int(d["upload_count"] or 0) if d else 0
        goal_before = bool(int(d["goal_done"] or 0)) if d else False

        if action == "rate":
            rated += 1
        elif action == "comment":
            comm += 1
        elif action == "upload":
            upl += 1

        goal_after = _streak_goal_done(rated, comm, upl)

        await conn.execute(
            """
            UPDATE streak_daily
            SET rated_count=$3, comment_count=$4, upload_count=$5, goal_done=$6
            WHERE tg_id=$1 AND day_key=$2
            """,
            int(tg_id), str(day_key),
            int(rated), int(comm), int(upl),
            1 if goal_after else 0
        )

        streak_changed = False

        if (not goal_before) and goal_after:
            u = await conn.fetchrow("SELECT * FROM user_streak WHERE tg_id=$1", int(tg_id))
            streak = int(_row_val(u, "streak", 0) or 0) if u else 0
            best = int(_row_val(u, "best_streak", 0) or 0) if u else 0
            last_completed = str(_row_val(u, "last_completed_day")) if u and _row_val(u, "last_completed_day") else None
            reward_flag = bool(int(_row_val(u, "reward_111_given", 0) or 0)) if u else False

            if last_completed != str(day_key):
                if last_completed is None:
                    new_streak = 1
                else:
                    try:
                        last_d = datetime.fromisoformat(last_completed).date()
                        cur_d = datetime.fromisoformat(str(day_key)).date()
                        delta = (cur_d - last_d).days
                    except Exception:
                        delta = 999
                    new_streak = (streak + 1) if delta == 1 else 1

                new_best = max(best, int(new_streak))
                await conn.execute(
                    """
                    UPDATE user_streak
                    SET streak=$2, best_streak=$3, last_completed_day=$4, updated_at=$5
                    WHERE tg_id=$1
                    """,
                    int(tg_id), int(new_streak), int(new_best), str(day_key), now_iso
                )
                streak_changed = True
                try:
                    await _grant_streak_reward_if_needed(int(tg_id), best, new_best)
                except Exception:
                    pass

        u2 = await conn.fetchrow("SELECT * FROM user_streak WHERE tg_id=$1", int(tg_id))

    return {
        "day_key": str(day_key),
        "rated": int(rated),
        "commented": int(comm),
        "uploaded": int(upl),
        "goal_done_now": bool(goal_after),
        "streak_changed": bool(streak_changed),
        "streak": int(_row_val(u2, "streak", 0) or 0) if u2 else 0,
        "best": int(_row_val(u2, "best_streak", 0) or 0) if u2 else 0,
        "freeze": int(_row_val(u2, "freeze_tokens", 0) or 0) if u2 else 0,
        "visible": bool(int(_row_val(u2, "visible", 1) or 1)) if u2 else True,
    }


async def streak_add_freeze_by_tg_id(tg_id: int, amount: int = 1) -> int:
    """Add freeze tokens to a user's streak. Returns new freeze_tokens."""
    if amount <= 0:
        status = await streak_get_status_by_tg_id(int(tg_id))
        return int(status.get("freeze_tokens") or 0)

    await streak_ensure_user_row(int(tg_id))

    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "UPDATE user_streak "
            "SET freeze_tokens = COALESCE(freeze_tokens, 0) + $2, updated_at=$3 "
            "WHERE tg_id=$1 "
            "RETURNING freeze_tokens",
            int(tg_id),
            int(amount),
            get_moscow_now_iso(),
        )

    return int(v or 0)


async def streak_use_freeze_today(tg_id: int) -> dict:
    """
    Использовать одну заморозку для закрытия сегодняшнего дня.
    Если заморозок нет или день уже закрыт — возвращает текущее состояние без изменений.
    """
    await streak_ensure_user_row(int(tg_id))
    p = _assert_pool()
    now_dt = get_moscow_now()
    day_key = _streak_target_day_key(now_dt)
    now_iso = get_moscow_now_iso()

    async with p.acquire() as conn:
        u = await conn.fetchrow(
            "SELECT streak, best_streak, freeze_tokens, last_completed_day, reward_111_given FROM user_streak WHERE tg_id=$1",
            int(tg_id),
        )
        freeze_tokens = int(_row_val(u, "freeze_tokens", 0) or 0) if u else 0
        best_before = int(_row_val(u, "best_streak", 0) or 0) if u else 0
        last_completed = (str(_row_val(u, "last_completed_day", "")) or "").strip() if u else ""
        streak_before = int(_row_val(u, "streak", 0) or 0) if u else 0

        # Ensure daily row
        await conn.execute(
            """
            INSERT INTO streak_daily (tg_id, day_key)
            VALUES ($1,$2)
            ON CONFLICT (tg_id, day_key) DO NOTHING
            """,
            int(tg_id),
            str(day_key),
        )
        d = await conn.fetchrow(
            "SELECT rated_count, comment_count, upload_count, goal_done FROM streak_daily WHERE tg_id=$1 AND day_key=$2",
            int(tg_id),
            str(day_key),
        )
        goal_done = bool(int(d["goal_done"] or 0)) if d else False

        if goal_done or freeze_tokens <= 0:
            return await streak_get_status_by_tg_id(int(tg_id))

        # Spend freeze and mark day as done
        await conn.execute(
            "UPDATE user_streak SET freeze_tokens=GREATEST(freeze_tokens-1,0), updated_at=$2 WHERE tg_id=$1",
            int(tg_id),
            now_iso,
        )
        await conn.execute(
            "UPDATE streak_daily SET goal_done=1 WHERE tg_id=$1 AND day_key=$2",
            int(tg_id),
            str(day_key),
        )

        # Update streak counters
        new_streak = streak_before
        if last_completed != str(day_key):
            if not last_completed:
                new_streak = 1
            else:
                try:
                    last_d = datetime.fromisoformat(last_completed).date()
                    cur_d = datetime.fromisoformat(str(day_key)).date()
                    delta = (cur_d - last_d).days
                except Exception:
                    delta = 999
                new_streak = (streak_before + 1) if delta == 1 else 1

        new_best = max(best_before, int(new_streak))
        await conn.execute(
            """
            UPDATE user_streak
            SET streak=$2, best_streak=$3, last_completed_day=$4, updated_at=$5
            WHERE tg_id=$1
            """,
            int(tg_id),
            int(new_streak),
            int(new_best),
            str(day_key),
            now_iso,
        )

    status = await streak_get_status_by_tg_id(int(tg_id))
    try:
        await _grant_streak_reward_if_needed(int(tg_id), best_before, int(status.get("best_streak") or best_before))
    except Exception:
        pass
    return status


async def streak_rollover_if_needed_by_tg_id(tg_id: int) -> dict:
    """Daily rollover for streak counters.

    Safe to call often (on /start, menu open, etc).
    """
    await streak_ensure_user_row(int(tg_id))

    # day keys are stored as Moscow date ISO strings (YYYY-MM-DD)
    now_dt = get_moscow_now()
    today = now_dt.date().isoformat()
    yesterday = (now_dt.date() - timedelta(days=1)).isoformat()

    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tg_id, streak, freeze_tokens, last_completed_day "
            "FROM user_streak WHERE tg_id=$1 LIMIT 1",
            int(tg_id),
        )

        if not row:
            return await streak_get_status_by_tg_id(int(tg_id))

        last_completed = (str(_row_val(row, "last_completed_day", "")) or "").strip()
        cur_streak = int(_row_val(row, "streak", 0) or 0)
        freeze = int(_row_val(row, "freeze_tokens", 0) or 0)
        best_before = int(_row_val(row, "best_streak", 0) or 0)

        # Already completed today -> nothing to rollover
        if last_completed == today:
            return await streak_get_status_by_tg_id(int(tg_id))

        # New day: ensure today's daily row exists (daily counters are stored in streak_daily)
        await conn.execute(
            """
            INSERT INTO streak_daily (tg_id, day_key)
            VALUES ($1,$2)
            ON CONFLICT (tg_id, day_key) DO NOTHING
            """,
            int(tg_id),
            str(today),
        )

        # New day: reset nudge counter for today (so reminders can be sent again)
        await conn.execute(
            """
            UPDATE user_streak
            SET last_nudge_day=$2,
                nudge_count=CASE
                    WHEN COALESCE(last_nudge_day, '') <> $2 THEN 0
                    ELSE COALESCE(nudge_count, 0)
                END,
                updated_at=$3
            WHERE tg_id=$1
            """,
            int(tg_id),
            str(today),
            get_moscow_now_iso(),
        )

        # Missed day: if last completion wasn't yesterday
        if cur_streak > 0 and last_completed and last_completed != yesterday:
            if freeze > 0:
                await conn.execute(
                    "UPDATE user_streak "
                    "SET freeze_tokens=GREATEST(freeze_tokens-1,0), updated_at=$2 "
                    "WHERE tg_id=$1",
                    int(tg_id),
                    get_moscow_now_iso(),
                )
            else:
                await conn.execute(
                    "UPDATE user_streak "
                    "SET streak=0, updated_at=$2 "
                    "WHERE tg_id=$1",
                    int(tg_id),
                    get_moscow_now_iso(),
                )

    status = await streak_get_status_by_tg_id(int(tg_id))
    try:
        await _grant_streak_reward_if_needed(int(tg_id), best_before, int(status.get("best_streak") or best_before))
    except Exception:
        pass
    return status


async def count_today_photos_for_user(user_id: int, *, include_deleted: bool = False) -> int:
    """How many photos the user uploaded today (submit_day in bot timezone)."""
    p = _assert_pool()
    today = get_bot_today()
    day_key = _today_key()
    where_deleted = "" if include_deleted else "AND is_deleted=0"
    async with p.acquire() as conn:
        v = await conn.fetchval(
            f"""
            SELECT COUNT(*)
            FROM photos
            WHERE user_id=$1
              AND (
                    submit_day=$2::date
                 OR (submit_day IS NULL AND day_key=$3)
              )
              {where_deleted}
            """,
            int(user_id),
            today,
            day_key,
        )
    return int(v or 0)


async def get_photo_ratings_stats(photo_id: int) -> dict:
    """Aggregate rating stats for a photo.

    Returns keys:
      ratings_count: int
      avg_rating: float|None
      last_rating: int|None
      good_count: int  (value >= 6)
      bad_count: int   (value <= 5)
      rated_users: int (distinct user_id)
    """
    p = _assert_pool()
    w = _link_rating_weight()
    query = """
        SELECT
            COUNT(*)::int AS ratings_count,
            COALESCE(SUM(value * CASE WHEN source='link' THEN $2 ELSE 1 END), 0)::float AS sum_values,
            COALESCE(SUM(CASE WHEN source='link' THEN $2 ELSE 1 END), 0)::float AS sum_weights,
            SUM(CASE WHEN value >= 6 THEN 1 ELSE 0 END)::int AS good_count,
            SUM(CASE WHEN value <= 5 THEN 1 ELSE 0 END)::int AS bad_count,
            COUNT(DISTINCT user_id)::int AS rated_users
        FROM ratings
        WHERE photo_id = $1
    """

    async with p.acquire() as conn:
        row = await conn.fetchrow(query, int(photo_id), float(w))
        last = await conn.fetchval(
            "SELECT value FROM ratings WHERE photo_id=$1 ORDER BY created_at DESC, id DESC LIMIT 1",
            int(photo_id),
        )

    if not row:
        return {
            "ratings_count": 0,
            "avg_rating": None,
            "last_rating": None,
            "good_count": 0,
            "bad_count": 0,
            "rated_users": 0,
        }

    sum_values = float(row["sum_values"] or 0.0)
    sum_weights = float(row["sum_weights"] or 0.0)
    avg_rating = (sum_values / sum_weights) if sum_weights > 0 else None

    return {
        "ratings_count": int(row["ratings_count"] or 0),
        "avg_rating": avg_rating,
        "last_rating": int(last) if last is not None else None,
        "good_count": int(row["good_count"] or 0),
        "bad_count": int(row["bad_count"] or 0),
        "rated_users": int(row["rated_users"] or 0),
    }


# Aggregate stats for a photo including Bayes score.
async def get_photo_stats(photo_id: int) -> dict:
    """Aggregate stats for a photo including Bayes score.

    Returns keys:
      ratings_count: int
      avg_rating: float|None
      bayes_score: float|None
    """
    p = _assert_pool()
    w = _link_rating_weight()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              COUNT(r.id)::int AS ratings_count,
              COUNT(DISTINCT r.user_id)::int AS rated_users,
              COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS sum_values,
              COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS sum_weights,
              AVG(r.value)::float AS avg_raw
            FROM ratings r
            WHERE r.photo_id = $1
            """,
            int(photo_id),
            float(w),
        )

        ratings_count = int(row["ratings_count"] or 0) if row else 0
        rated_users = int(row["rated_users"] or 0) if row else 0
        sum_values = float(row["sum_values"] or 0) if row else 0.0
        sum_weights = float(row["sum_weights"] or 0) if row else 0.0
        avg_raw = row["avg_raw"] if row else None
        avg_rating = (sum_values / sum_weights) if sum_weights > 0 else avg_raw
        comments_count = await conn.fetchval(
            "SELECT COUNT(*) FROM comments WHERE photo_id=$1",
            int(photo_id),
        )

        global_mean, _global_cnt = await _get_global_rating_mean(conn)
        prior = _bayes_prior_weight()
        bayes = _bayes_score(
            sum_values=sum_values,
            n=sum_weights,
            global_mean=global_mean,
            prior=prior,
        )

    return {
        "ratings_count": ratings_count,
        "avg_rating": avg_rating,
        "bayes_score": bayes,
        "comments_count": int(comments_count or 0),
        "rated_users": rated_users,
    }


async def get_photo_stats_snapshot(photo_id: int, *, include_author_metrics: bool = False) -> dict:
    """Lightweight snapshot for photo stats UI.

    Uses counters from `photos` plus compact indexed aggregates for daily/extra metrics.
    """
    p = _assert_pool()
    now = get_bot_now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with p.acquire() as conn:
        photo = await conn.fetchrow(
            """
            SELECT
              p.id,
              p.user_id,
              p.submit_day,
              p.day_key,
              p.status,
              p.created_at,
              p.expires_at,
              COALESCE(p.avg_score, 0)::float AS avg_score,
              COALESCE(p.votes_count, 0)::int AS votes_count,
              COALESCE(p.views_count, 0)::int AS views_total
            FROM photos p
            WHERE p.id=$1
            LIMIT 1
            """,
            int(photo_id),
        )
        if not photo:
            return {}

        votes_row = await conn.fetchrow(
            """
            SELECT
              COALESCE(SUM(CASE WHEN score BETWEEN 6 AND 10 THEN 1 ELSE 0 END), 0)::int AS positive_votes,
              COALESCE(SUM(CASE WHEN created_at >= $2 THEN 1 ELSE 0 END), 0)::int AS votes_today,
              COUNT(*)::int AS votes_rows
            FROM votes
            WHERE photo_id=$1
            """,
            int(photo_id),
            today_start,
        )
        positive_votes = int((votes_row or {}).get("positive_votes") or 0)
        votes_today = int((votes_row or {}).get("votes_today") or 0)
        votes_rows = int((votes_row or {}).get("votes_rows") or 0)

        # Backward compatibility for old rows where votes mirror could be empty.
        votes_count = int(photo.get("votes_count") or 0)
        if votes_count > 0 and votes_rows <= 0:
            rating_row = await conn.fetchrow(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN value BETWEEN 6 AND 10 THEN 1 ELSE 0 END), 0)::int AS positive_votes
                FROM ratings
                WHERE photo_id=$1
                """,
                int(photo_id),
            )
            if rating_row:
                positive_votes = int(rating_row.get("positive_votes") or 0)

        comments_count = 0
        link_clicks = 0
        if include_author_metrics:
            comments_count = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM comments WHERE photo_id=$1",
                    int(photo_id),
                )
                or 0
            )
            link_clicks = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM ratings WHERE photo_id=$1 AND source='link'",
                    int(photo_id),
                )
                or 0
            )

        status = str(photo.get("status") or "active")
        submit_day = photo.get("submit_day")
        day_key = str(photo.get("day_key") or "")
        rank: int | None = None
        total_in_party: int | None = None

        if status == "archived":
            rank_row = await conn.fetchrow(
                """
                SELECT
                  rr.final_rank::int AS rank,
                  COALESCE(
                    drc.participants_count::int,
                    (SELECT COUNT(*)::int FROM result_ranks rr2 WHERE rr2.submit_day=rr.submit_day)
                  )::int AS total_in_party
                FROM result_ranks rr
                LEFT JOIN daily_results_cache drc ON drc.submit_day = rr.submit_day
                WHERE rr.photo_id=$1
                ORDER BY rr.submit_day DESC, rr.final_rank ASC
                LIMIT 1
                """,
                int(photo_id),
            )
            if rank_row:
                rank = int(rank_row.get("rank") or 0) or None
                total_in_party = int(rank_row.get("total_in_party") or 0) or None
        else:
            rank_row = await conn.fetchrow(
                """
                WITH party AS (
                    SELECT
                      p.id,
                      ROW_NUMBER() OVER (
                        ORDER BY
                          COALESCE(p.avg_score, 0) DESC,
                          COALESCE(p.votes_count, 0) DESC,
                          p.created_at ASC,
                          p.id ASC
                      )::int AS rank,
                      COUNT(*) OVER ()::int AS total_in_party
                    FROM photos p
                    WHERE COALESCE(p.is_deleted,0)=0
                      AND COALESCE(p.moderation_status,'active') IN ('active','good')
                      AND COALESCE(p.status,'active')='active'
                      AND (
                           ($1::date IS NOT NULL AND p.submit_day=$1::date)
                        OR ($1::date IS NULL AND p.day_key=$2)
                      )
                )
                SELECT rank, total_in_party
                FROM party
                WHERE id=$3
                LIMIT 1
                """,
                submit_day,
                day_key,
                int(photo_id),
            )
            if rank_row:
                rank = int(rank_row.get("rank") or 0) or None
                total_in_party = int(rank_row.get("total_in_party") or 0) or None

    return {
        "photo_id": int(photo_id),
        "user_id": int(photo.get("user_id") or 0),
        "submit_day": photo.get("submit_day"),
        "day_key": str(photo.get("day_key") or ""),
        "status": str(photo.get("status") or "active"),
        "created_at": photo.get("created_at"),
        "expires_at": photo.get("expires_at"),
        "avg_score": float(photo.get("avg_score") or 0.0),
        "votes_count": int(photo.get("votes_count") or 0),
        "views_total": int(photo.get("views_total") or 0),
        "positive_votes": int(positive_votes),
        "votes_today": int(votes_today),
        "comments_count": int(comments_count),
        "link_clicks": int(link_clicks),
        "rank": int(rank) if rank is not None else None,
        "total_in_party": int(total_in_party) if total_in_party is not None else None,
    }


async def get_user_spend_today_stats(user_id: int) -> dict:
    """Return today's approximate spend stats for author's shows."""
    p = _assert_pool()
    now = get_bot_now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with p.acquire() as conn:
        views_today = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM photo_views pv
            JOIN photos p ON p.id = pv.photo_id
            WHERE p.user_id=$1
              AND pv.created_at >= $2
            """,
            int(user_id),
            today_start,
        )
    views_today_i = int(views_today or 0)
    return {
        "views_today": views_today_i,
        "credits_spent_today": views_today_i / 2.0,
    }


async def count_super_ratings_for_photo(photo_id: int) -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM super_ratings WHERE photo_id=$1", int(photo_id))
    return int(v or 0)


async def count_comments_for_photo(photo_id: int) -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM comments WHERE photo_id=$1", int(photo_id))
    return int(v or 0)


async def count_active_users() -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_deleted=0",)
    return int(v or 0)


async def count_photo_reports_for_photo(photo_id: int) -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM photo_reports WHERE photo_id=$1", int(photo_id))
    return int(v or 0)


async def get_link_ratings_count_for_photo(photo_id: int) -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "SELECT COUNT(*) FROM ratings WHERE photo_id=$1 AND source='link'",
            int(photo_id),
        )
    return int(v or 0)


async def get_ratings_count_for_photo(photo_id: int) -> int:
    """Total ratings count for the photo (all sources)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "SELECT COUNT(*) FROM ratings WHERE photo_id=$1",
            int(photo_id),
        )
    return int(v or 0)


async def get_photo_ratings_list(photo_id: int) -> list[dict]:
    """List ratings with rater usernames for a photo."""
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.value, r.created_at, u.username, u.name, u.tg_id
            FROM ratings r
            JOIN users u ON u.id = r.user_id
            WHERE r.photo_id = $1
              AND r.value BETWEEN 1 AND 10
            ORDER BY r.value ASC, r.created_at DESC, r.id DESC
            """,
            int(photo_id),
        )
    return [dict(r) for r in rows] if rows else []


async def admin_delete_last_rating_for_photo(photo_id: int) -> dict:
    """
    Delete the latest 1..10 rating for a photo and recalc photo counters.
    Returns: {"deleted": bool, "value": int|None, "user_id": int|None, "votes_count": int}
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT id FROM photos WHERE id=$1 FOR UPDATE", int(photo_id))
            row = await conn.fetchrow(
                """
                SELECT id, user_id, value
                FROM ratings
                WHERE photo_id=$1
                  AND value BETWEEN 1 AND 10
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                int(photo_id),
            )
            if not row:
                return {"deleted": False, "value": None, "user_id": None, "votes_count": 0}

            r_user_id = int(row["user_id"])
            r_value = int(row["value"])
            await conn.execute(
                "DELETE FROM ratings WHERE photo_id=$1 AND user_id=$2",
                int(photo_id),
                r_user_id,
            )
            await conn.execute(
                "DELETE FROM votes WHERE photo_id=$1 AND voter_id=$2",
                int(photo_id),
                r_user_id,
            )

            agg = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::int AS cnt,
                    COALESCE(SUM(value), 0)::int AS sum_score
                FROM ratings
                WHERE photo_id=$1
                  AND value BETWEEN 1 AND 10
                """,
                int(photo_id),
            )
            votes_count = int((agg or {}).get("cnt") or 0)
            sum_score = int((agg or {}).get("sum_score") or 0)
            avg_score = (float(sum_score) / float(votes_count)) if votes_count > 0 else 0.0
            await conn.execute(
                """
                UPDATE photos
                SET votes_count=$2, sum_score=$3, avg_score=$4
                WHERE id=$1
                """,
                int(photo_id),
                votes_count,
                sum_score,
                avg_score,
            )
            return {
                "deleted": True,
                "value": r_value,
                "user_id": r_user_id,
                "votes_count": votes_count,
            }


async def admin_clear_ratings_for_photo(photo_id: int) -> dict:
    """
    Delete all 1..10 ratings for a photo and recalc photo counters.
    Returns: {"removed": int, "votes_count": int}
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT id FROM photos WHERE id=$1 FOR UPDATE", int(photo_id))
            removed = await conn.fetchval(
                """
                WITH d AS (
                    DELETE FROM ratings
                    WHERE photo_id=$1
                      AND value BETWEEN 1 AND 10
                    RETURNING 1
                )
                SELECT COUNT(*)::int FROM d
                """,
                int(photo_id),
            )
            await conn.execute("DELETE FROM votes WHERE photo_id=$1", int(photo_id))
            await conn.execute(
                "UPDATE photos SET votes_count=0, sum_score=0, avg_score=0 WHERE id=$1",
                int(photo_id),
            )
            return {"removed": int(removed or 0), "votes_count": 0}


async def get_photo_skip_count_for_photo(photo_id: int) -> int:
    """Placeholder for per-photo skip statistics.

    If a future table exists (e.g. photo_skips), this will return real counts.
    For now, safely returns 0.
    """
    p = _assert_pool()
    try:
        async with p.acquire() as conn:
            v = await conn.fetchval("SELECT COUNT(*) FROM photo_skips WHERE photo_id=$1", int(photo_id))
        return int(v or 0)
    except Exception:
        return 0


async def get_rating_feed_state(user_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT seq, last_author_id FROM rating_feed_state WHERE user_id=$1",
            int(user_id),
        )
    return {
        "seq": int(row["seq"]) if row and row["seq"] is not None else 0,
        "last_author_id": int(row["last_author_id"]) if row and row["last_author_id"] is not None else None,
    }


async def get_rating_feed_seq(user_id: int) -> int:
    state = await get_rating_feed_state(user_id)
    return int(state.get("seq") or 0)


async def set_rating_feed_state(user_id: int, seq: int, *, last_author_id: int | None = None) -> None:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO rating_feed_state (user_id, seq, last_author_id, updated_at)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (user_id)
            DO UPDATE SET
                seq=EXCLUDED.seq,
                last_author_id=COALESCE(EXCLUDED.last_author_id, rating_feed_state.last_author_id),
                updated_at=EXCLUDED.updated_at
            """,
            int(user_id),
            int(seq),
            int(last_author_id) if last_author_id is not None else None,
            now,
        )


async def set_rating_feed_seq(user_id: int, seq: int) -> None:
    await set_rating_feed_state(user_id, seq)

async def get_user_rating_day_stats(user_id: int, day_key: str) -> dict:
    """Return counts of ratings and ones for a user for a given Moscow day."""
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::int AS total_count,
                COALESCE(SUM(CASE WHEN value=1 THEN 1 ELSE 0 END), 0)::int AS ones_count
            FROM ratings
            WHERE user_id=$1
              AND COALESCE(source,'feed')='feed'
              AND value BETWEEN 1 AND 10
              AND created_at LIKE $2 || '%'
            """,
            int(user_id),
            str(day_key),
        )
    if row:
        return dict(row)
    return {"total_count": 0, "ones_count": 0}


async def mark_user_suspicious_rating(
    user_id: int,
    day_key: str,
    ones_count: int,
    total_count: int,
) -> None:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO rating_suspicious (user_id, day_key, ones_count, total_count, created_at)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (user_id, day_key)
            DO UPDATE SET ones_count=EXCLUDED.ones_count,
                          total_count=EXCLUDED.total_count,
                          created_at=EXCLUDED.created_at
            """,
            int(user_id),
            str(day_key),
            int(ones_count),
            int(total_count),
            now,
        )


async def init_db() -> None:
    global pool
    if not DB_DSN:
        raise RuntimeError("DATABASE_URL is not set")
    if pool is None:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=10)
        await ensure_schema()


async def close_db() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None


async def ensure_schema() -> None:
    """саздает таблици под текущи хендлери."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id BIGSERIAL PRIMARY KEY,
              tg_id BIGINT UNIQUE NOT NULL,
              username TEXT,
              name TEXT,
              gender TEXT,
              age INTEGER,
              bio TEXT,
              tg_channel_link TEXT,
              city TEXT,
              country TEXT,
              language TEXT NOT NULL DEFAULT 'ru',
              show_city INTEGER NOT NULL DEFAULT 1,
              show_country INTEGER NOT NULL DEFAULT 1,
              allow_ratings INTEGER NOT NULL DEFAULT 1,
              author_code TEXT,

              is_admin INTEGER NOT NULL DEFAULT 0,
              is_moderator INTEGER NOT NULL DEFAULT 0,
              is_helper INTEGER NOT NULL DEFAULT 0,
              is_support INTEGER NOT NULL DEFAULT 0,

              is_premium INTEGER NOT NULL DEFAULT 0,
              premium_until TEXT,

              is_blocked INTEGER NOT NULL DEFAULT 0,
              block_reason TEXT,
              block_until TEXT,

              ads_enabled INTEGER,

              referral_code TEXT,

              is_deleted INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photos (
              id BIGSERIAL PRIMARY KEY,
              user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              file_id TEXT NOT NULL,
              file_id_original TEXT,
              file_id_public TEXT,
              file_id_support TEXT,
              title TEXT,
              description TEXT,
              category TEXT DEFAULT 'photo',
              device_type TEXT,
              device_info TEXT,
              tag TEXT,
              day_key TEXT,
              submit_day DATE,
              expires_at TIMESTAMPTZ,
              status TEXT NOT NULL DEFAULT 'active',
              votes_count INTEGER NOT NULL DEFAULT 0,
              sum_score INTEGER NOT NULL DEFAULT 0,
              avg_score NUMERIC(6,3) NOT NULL DEFAULT 0,
              views_count INTEGER NOT NULL DEFAULT 0,
              daily_views_budget INTEGER NOT NULL DEFAULT 0,
              tg_file_id TEXT,
              moderation_status TEXT NOT NULL DEFAULT 'active',
              ratings_enabled INTEGER NOT NULL DEFAULT 1,
              is_deleted INTEGER NOT NULL DEFAULT 0,
              deleted_at TIMESTAMPTZ,
              created_at TEXT NOT NULL
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ratings (
              id BIGSERIAL PRIMARY KEY,
              photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              value INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(photo_id, user_id)
            );
            """
        )

        # new votes table (parallel to legacy ratings for new flows)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS votes (
              id BIGSERIAL PRIMARY KEY,
              photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              voter_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              score INTEGER NOT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              UNIQUE(photo_id, voter_id)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS super_ratings (
              id BIGSERIAL PRIMARY KEY,
              photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              UNIQUE(photo_id, user_id)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comments (
              id BIGSERIAL PRIMARY KEY,
              photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              text TEXT NOT NULL,
              is_public INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL
            );
            """
        )
        # migration: comments.is_public (needed for anonymous/public comments)
        await conn.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS is_public INTEGER NOT NULL DEFAULT 1;")

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_reports (
              id BIGSERIAL PRIMARY KEY,
              photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              reason TEXT NOT NULL,
              text TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              created_at TEXT NOT NULL
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS moderator_reviews (
              id BIGSERIAL PRIMARY KEY,
              moderator_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              action TEXT NOT NULL,
              note TEXT,
              created_at TEXT NOT NULL
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS awards (
              id BIGSERIAL PRIMARY KEY,
              user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              code TEXT NOT NULL,
              title TEXT NOT NULL,
              description TEXT,
              icon TEXT,
              is_special INTEGER NOT NULL DEFAULT 0,
              granted_by_user_id BIGINT,
              created_at TEXT NOT NULL,
              UNIQUE(user_id, code)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_referrals (
              new_user_tg_id BIGINT PRIMARY KEY,
              referral_code  TEXT NOT NULL,
              created_at     TEXT NOT NULL
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
              id BIGSERIAL PRIMARY KEY,
              inviter_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              invited_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              qualified INTEGER NOT NULL DEFAULT 0,
              qualified_at TEXT,
              UNIQUE(invited_user_id)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_rewards (
              id BIGSERIAL PRIMARY KEY,
              invited_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              inviter_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              rewarded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              reward_type TEXT NOT NULL DEFAULT 'premium_credits',
              reward_version TEXT NOT NULL DEFAULT 'v2_3h_2c',
              UNIQUE(invited_user_id)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id BIGSERIAL PRIMARY KEY,
                tg_id BIGINT NOT NULL,
                provider TEXT,
                amount_rub INTEGER,
                amount_stars INTEGER,
                period_code TEXT,
                inv_id TEXT,
                order_id TEXT,
                payment_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_skips (
              user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
              day_key TEXT NOT NULL,
              skips_used INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL
            );
            """
        )

        # ads
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ads (
              id BIGSERIAL PRIMARY KEY,
              title TEXT NOT NULL,
              body TEXT NOT NULL,
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idea_requests (
              id BIGSERIAL PRIMARY KEY,
              user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              week_key TEXT NOT NULL,
              count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(user_id, week_key)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS viewonly_views (
              user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              PRIMARY KEY (user_id, photo_id)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_stats (
              user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
              credits INTEGER NOT NULL DEFAULT 0,
              show_tokens INTEGER NOT NULL DEFAULT 0,
              last_active_at TIMESTAMPTZ,
              last_daily_grant_day DATE,
              votes_given_today INTEGER NOT NULL DEFAULT 0,
              votes_given_happyhour_today INTEGER NOT NULL DEFAULT 0,
              public_portfolio BOOLEAN NOT NULL DEFAULT FALSE
            );
            """
        )
        await conn.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS show_tokens INTEGER NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS last_daily_grant_day DATE;")
        await conn.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS migration_notified BOOLEAN NOT NULL DEFAULT FALSE;")
        await conn.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS upload_rules_ack_at TIMESTAMPTZ;")
        await conn.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS author_forward_allowed BOOLEAN NOT NULL DEFAULT TRUE;")
        await conn.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS author_badge_enabled BOOLEAN NOT NULL DEFAULT TRUE;")

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_author_votes (
              day DATE NOT NULL,
              voter_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              author_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              cnt INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(day, voter_id, author_id)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_views (
              photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              viewer_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              PRIMARY KEY (photo_id, viewer_id)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS result_ranks (
              photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              submit_day DATE NOT NULL,
              final_rank INTEGER NOT NULL,
              finalized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              PRIMARY KEY (photo_id, submit_day)
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_results_cache (
              submit_day DATE PRIMARY KEY,
              participants_count INTEGER NOT NULL DEFAULT 0,
              top_threshold INTEGER NOT NULL DEFAULT 0,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              notifications_enqueued_at TIMESTAMPTZ,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_queue (
              id BIGSERIAL PRIMARY KEY,
              user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              type TEXT NOT NULL,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              run_after TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              status TEXT NOT NULL DEFAULT 'pending',
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute("ALTER TABLE notification_queue ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE notification_queue ADD COLUMN IF NOT EXISTS last_error TEXT;")

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rating_suspicious (
              user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              day_key TEXT NOT NULL,
              ones_count INTEGER NOT NULL DEFAULT 0,
              total_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              PRIMARY KEY (user_id, day_key)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rating_feed_state (
              user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              seq INTEGER NOT NULL DEFAULT 0,
              last_author_id BIGINT,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (user_id)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_events (
              id BIGSERIAL PRIMARY KEY,
              user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
              kind TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS duels (
              id BIGSERIAL PRIMARY KEY,
              photo_a_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              photo_b_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              starts_at TIMESTAMPTZ NOT NULL,
              ends_at TIMESTAMPTZ NOT NULL,
              status TEXT NOT NULL DEFAULT 'scheduled',
              reward_credits INTEGER NOT NULL DEFAULT 0,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS duel_votes (
              duel_id BIGINT NOT NULL REFERENCES duels(id) ON DELETE CASCADE,
              voter_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              choice TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              PRIMARY KEY (duel_id, voter_id)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS collabs (
              id BIGSERIAL PRIMARY KEY,
              photo_a_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              photo_b_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
              author_a_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              author_b_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              starts_at TIMESTAMPTZ NOT NULL,
              ends_at TIMESTAMPTZ NOT NULL,
              status TEXT NOT NULL DEFAULT 'scheduled',
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS collab_votes (
              collab_id BIGINT NOT NULL REFERENCES collabs(id) ON DELETE CASCADE,
              voter_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              score INTEGER NOT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              PRIMARY KEY (collab_id, voter_id)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_error_logs (
              id BIGSERIAL PRIMARY KEY,
              chat_id BIGINT,
              tg_user_id BIGINT,
              handler TEXT,
              update_type TEXT,
              error_type TEXT,
              error_text TEXT,
              traceback_text TEXT,
              created_at TEXT NOT NULL
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS premium_news (
              id BIGSERIAL PRIMARY KEY,
              text TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS premium_benefits (
              id BIGSERIAL PRIMARY KEY,
              position INTEGER NOT NULL,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT,
              UNIQUE(position)
            );
            """
        )

        # напоминания о скором окончании премиума (дедуп по tg_id + premium_until)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS premium_expiry_reminders (
              tg_id BIGINT NOT NULL,
              premium_until TEXT NOT NULL,
              sent_at TEXT NOT NULL,
              PRIMARY KEY (tg_id, premium_until)
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_share_links (
                code TEXT PRIMARY KEY,
                owner_tg_id BIGINT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            """
        )
        # -------------------- notifications (likes/comments) --------------------
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_notify_settings (
                tg_id BIGINT PRIMARY KEY,
                likes_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                comments_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notify_likes_daily (
                tg_id BIGINT NOT NULL,
                day_key TEXT NOT NULL,
                likes_count INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (tg_id, day_key)
            );
            """
        )
        # We store streaks by Telegram user id (tg_id) because many flows are tg_id-first.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_streak (
              tg_id BIGINT PRIMARY KEY,
              streak INTEGER NOT NULL DEFAULT 0,
              best_streak INTEGER NOT NULL DEFAULT 0,
              freeze_tokens INTEGER NOT NULL DEFAULT 0,
              last_completed_day TEXT,

              notify_enabled INTEGER NOT NULL DEFAULT 1,
              notify_hour INTEGER NOT NULL DEFAULT 21,
              notify_minute INTEGER NOT NULL DEFAULT 0,
              visible INTEGER NOT NULL DEFAULT 1,
              reward_111_given INTEGER NOT NULL DEFAULT 0,

              last_nudge_day TEXT,
              nudge_count INTEGER NOT NULL DEFAULT 0,

              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS streak_daily (
              tg_id BIGINT NOT NULL,
              day_key TEXT NOT NULL,
              rated_count INTEGER NOT NULL DEFAULT 0,
              comment_count INTEGER NOT NULL DEFAULT 0,
              upload_count INTEGER NOT NULL DEFAULT 0,
              goal_done INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (tg_id, day_key)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS streak_actions (
              id BIGSERIAL PRIMARY KEY,
              tg_id BIGINT NOT NULL,
              action TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )

        # ======== ALTER TABLE ========
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS tag TEXT;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS city TEXT;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS country TEXT;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'ru';")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS show_city INTEGER NOT NULL DEFAULT 1;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS show_country INTEGER NOT NULL DEFAULT 1;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS rank_points INTEGER NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS rank_code TEXT;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS rank_updated_at TEXT;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_author INTEGER NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS author_verified_at TEXT;")
        await conn.execute("ALTER TABLE ratings ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'feed';")
        await conn.execute("ALTER TABLE ratings ADD COLUMN IF NOT EXISTS source_code TEXT;")
        await conn.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS order_id TEXT;")
        await conn.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS payment_id TEXT;")
        await conn.execute("ALTER TABLE payments ALTER COLUMN status SET DEFAULT 'pending';")
        await conn.execute("ALTER TABLE user_streak ADD COLUMN IF NOT EXISTS visible INTEGER NOT NULL DEFAULT 1;")
        await conn.execute("ALTER TABLE user_streak ADD COLUMN IF NOT EXISTS reward_111_given INTEGER NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS allow_ratings INTEGER NOT NULL DEFAULT 1;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ads_enabled INTEGER;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS author_code TEXT;")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_author_code_uniq ON users(author_code) WHERE author_code IS NOT NULL;"
        )
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS ratings_enabled INTEGER NOT NULL DEFAULT 1;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS file_id_original TEXT;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS file_id_public TEXT;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS file_id_support TEXT;")
        await conn.execute("ALTER TABLE rating_feed_state ADD COLUMN IF NOT EXISTS last_author_id BIGINT;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS submit_day DATE;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS votes_count INTEGER NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS sum_score INTEGER NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS avg_score NUMERIC(6,3) NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS views_count INTEGER NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS daily_views_budget INTEGER NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS tg_file_id TEXT;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS deleted_reason TEXT;")
        await conn.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;")

        # ======== CREATE INDEX IF NOT EXISTS ========
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ratings_photo_source ON ratings(photo_id, source);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_city ON users(city);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_rank_points ON users(rank_points);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_country ON users(country);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_error_logs_created_at ON bot_error_logs(created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_premium_news_created_at ON premium_news(created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_premium_expiry_reminders_sent_at ON premium_expiry_reminders(sent_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_premium_expiry_reminders_tg_id ON premium_expiry_reminders(tg_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_error_logs_tg_user_id ON bot_error_logs(tg_user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_error_logs_handler_created_at ON bot_error_logs(handler, created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_user_id ON photos(user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_day_key ON photos(day_key);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(moderation_status);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ratings_photo_id ON ratings(photo_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_photo_id ON comments(photo_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_photo_id ON photo_reports(photo_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_user_created_at ON photo_reports(user_id, created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_streak_updated_at ON user_streak(updated_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_streak_actions_tg_id ON streak_actions(tg_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_streak_actions_created_at ON streak_actions(created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photo_share_links_owner ON photo_share_links(owner_tg_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photo_share_links_active ON photo_share_links(is_active);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_tag ON photos(tag);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_submit_day ON photos(submit_day);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_status_new ON photos(status);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_expires_at ON photos(expires_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_status_expires ON photos(status, expires_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_submit_status ON photos(submit_day, status);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_user_status_submit_created ON photos(user_id, status, submit_day, created_at DESC);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_votes_photo_created ON votes(photo_id, created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_votes_voter_created ON votes(voter_id, created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photo_views_viewer ON photo_views(viewer_id, created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_events_created_at ON activity_events(created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_events_user_created_at ON activity_events(user_id, created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_events_kind_created_at ON activity_events(kind, created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_result_ranks_submit_day ON result_ranks(submit_day, final_rank);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_result_ranks_photo_submit ON result_ranks(photo_id, submit_day DESC);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_results_cache_published ON daily_results_cache(published_at DESC, submit_day DESC);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_queue_status_run ON notification_queue(status, run_after);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_stats_user_id ON user_stats(user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_stats_daily_grant ON user_stats(last_daily_grant_day, user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_author_votes_voter_day ON daily_author_votes(voter_id, day);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_referral_rewards_inviter ON referral_rewards(inviter_user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_referral_rewards_rewarded_at ON referral_rewards(rewarded_at DESC);")

        # ======== CREATE UNIQUE INDEX IF NOT EXISTS ========
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_provider_order_id ON payments(provider, order_id);")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_provider_payment_id ON payments(provider, payment_id);")
        

    from database_results import ensure_results_legacy_schema, ensure_results_schema as ensure_results_v2_schema
    await ensure_results_legacy_schema()
    await ensure_results_v2_schema()

# -------------------- helpers --------------------

# -------------------- share links / link ratings --------------------

async def ensure_user_minimal_row(tg_id: int, username: str | None = None) -> dict | None:
    """Ensure user row exists (minimal), without forcing full registration."""
    return await _ensure_user_row(int(tg_id), username=username)


def _make_share_code(n: int = 10) -> str:
    # no 0/O/I for readability
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alphabet) for _ in range(n))


async def get_or_create_share_link_code(owner_tg_id: int) -> str:
    """Return active share code for owner, or create a new one."""
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            """
            SELECT code
            FROM photo_share_links
            WHERE owner_tg_id=$1 AND is_active=1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            int(owner_tg_id),
        )

    if v:
        return str(v)

    now = get_moscow_now_iso()
    for _ in range(10):
        code = _make_share_code(10)
        try:
            async with p.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO photo_share_links (code, owner_tg_id, is_active, created_at)
                    VALUES ($1,$2,1,$3)
                    """,
                    code,
                    int(owner_tg_id),
                    now,
                )
            return code
        except Exception:
            # extremely rare collision, retry
            continue

    code = _make_share_code(14)
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO photo_share_links (code, owner_tg_id, is_active, created_at)
            VALUES ($1,$2,1,$3)
            """,
            code,
            int(owner_tg_id),
            now,
        )
    return code


async def refresh_share_link_code(owner_tg_id: int) -> str:
    """Disable previous active code and issue a new one."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE photo_share_links SET is_active=0 WHERE owner_tg_id=$1 AND is_active=1",
            int(owner_tg_id),
        )
    return await get_or_create_share_link_code(int(owner_tg_id))


async def get_owner_tg_id_by_share_code(code: str) -> int | None:
    c = (code or "").strip()
    if not c:
        return None
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "SELECT owner_tg_id FROM photo_share_links WHERE code=$1 AND is_active=1",
            c,
        )
    return int(v) if v is not None else None


async def get_active_photo_for_owner_tg_id(owner_tg_id: int) -> dict | None:
    """Return latest active (not deleted) photo for owner tg id."""
    owner = await get_user_by_tg_id(int(owner_tg_id))
    if not owner:
        return None

    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM photos
            WHERE user_id=$1 AND is_deleted=0 AND moderation_status IN ('active','good')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            int(owner["id"]),
        )
    return dict(row) if row else None


async def get_user_rating_value(photo_id: int, user_id: int) -> int | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "SELECT value FROM ratings WHERE photo_id=$1 AND user_id=$2 LIMIT 1",
            int(photo_id),
            int(user_id),
        )
    return int(v) if v is not None else None


async def has_user_commented(photo_id: int, user_id: int) -> bool:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "SELECT 1 FROM comments WHERE photo_id=$1 AND user_id=$2 LIMIT 1",
            int(photo_id),
            int(user_id),
        )
    return bool(v)


async def add_rating_by_tg_id(
    *,
    photo_id: int,
    rater_tg_id: int,
    value: int,
    source: str = "feed",
    source_code: str | None = None,
) -> bool:
    """Insert rating for a tg user. Returns True if inserted, False if already exists."""
    if value < 1 or value > 10:
        return False

    u = await ensure_user_minimal_row(int(rater_tg_id))
    if not u:
        return False

    p = _assert_pool()
    now = get_moscow_now_iso()
    try:
        async with p.acquire() as conn:
            # Проверяем, можно ли ставить оценки этому фото (не удалено, активно, оценки включены)
            photo_row = await conn.fetchrow(
                "SELECT ratings_enabled, is_deleted, moderation_status FROM photos WHERE id=$1",
                int(photo_id),
            )
            status = str(photo_row.get("moderation_status") or "").lower()
            if (
                not photo_row
                or int(photo_row.get("is_deleted") or 0) != 0
                or status not in ("active", "good")
                or not bool(photo_row.get("ratings_enabled", 1))
            ):
                return False

            await conn.execute(
                """
                INSERT INTO ratings (photo_id, user_id, value, source, source_code, created_at)
                VALUES ($1,$2,$3,$4,$5,$6)
                """,
                int(photo_id),
                int(u["id"]),
                int(value),
                str(source or "feed"),
                str(source_code) if source_code else None,
                now,
            )

            # Invalidate author's rank cache (their photo got a new rating)
            try:
                owner_user_id = await conn.fetchval(
                    "SELECT user_id FROM photos WHERE id=$1 LIMIT 1",
                    int(photo_id),
                )
                if owner_user_id is not None:
                    await conn.execute(
                        "UPDATE users SET rank_updated_at=NULL, updated_at=$1 WHERE id=$2",
                        now,
                        int(owner_user_id),
                    )
            except Exception:
                pass

        return True
    except asyncpg.exceptions.UniqueViolationError:
        return False

async def _ensure_user_row(tg_id: int, username: str | None = None) -> dict | None:
    u = await get_user_by_tg_id(tg_id)
    if u is not None:
        if username and (u.get("username") != username):
            p = _assert_pool()
            async with p.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET username=$1, updated_at=$2 WHERE id=$3",
                    username, get_moscow_now_iso(), int(u["id"])
                )
        return u

    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (tg_id, username, created_at)
            VALUES ($1,$2,$3)
            ON CONFLICT (tg_id) DO UPDATE SET username=EXCLUDED.username
            RETURNING *
            """,
            int(tg_id), username, now
        )
    return dict(row) if row else None


# -------------------- users --------------------

async def upsert_user_profile(
    *,
    tg_id: int,
    username: str | None,
    name: str | None,
    gender: str | None,
    age: int | None,
    bio: str | None,
) -> None:
    """Create or update a user profile.

    Needed because some flows create a minimal user row (e.g. rating-by-link), and registration
    should UPDATE that row instead of failing on unique constraints.

    This is a thin wrapper around `create_user`, which already performs an UPSERT.
    """
    await create_user(
        tg_id=int(tg_id),
        username=username,
        name=name,
        gender=gender,
        age=age,
        bio=bio,
    )

async def create_user(tg_id: int, username: str | None, name: str | None, gender: str | None,
                      age: int | None, bio: str | None) -> dict:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (tg_id, username, name, gender, age, bio, language, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,'ru',$7,$7)
            ON CONFLICT (tg_id) DO UPDATE
              SET username=EXCLUDED.username,
                  name=EXCLUDED.name,
                  gender=EXCLUDED.gender,
                  age=EXCLUDED.age,
                  bio=EXCLUDED.bio,
                  is_deleted=0,
                  updated_at=EXCLUDED.updated_at
            RETURNING *
            """,
            int(tg_id), username, name, gender, age, bio, now
        )
    return dict(row)



async def get_user_by_tg_id(tg_id: int) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE tg_id=$1 AND is_deleted=0",
            int(tg_id),
        )
    return dict(row) if row else None


async def set_user_author_status_by_tg_id(tg_id: int, is_author: bool) -> None:
    """
    Marks user as verified author (or removes flag).
    """
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_author=$1, author_verified_at=$2, updated_at=$3 WHERE tg_id=$4",
            int(is_author),
            now if is_author else None,
            now,
            int(tg_id),
        )


async def is_user_author_by_tg_id(tg_id: int) -> bool:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "SELECT is_author FROM users WHERE tg_id=$1",
            int(tg_id),
        )
    return bool(v)


async def get_user_by_tg_id_any(tg_id: int) -> dict | None:
    """Вернёт пользователя вне зависимости от is_deleted (используется для восстановления)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE tg_id=$1",
            int(tg_id),
        )
    return dict(row) if row else None


async def is_user_soft_deleted(tg_id: int) -> bool:
    """
    Проверяет, помечен ли пользователь как удалённый (is_deleted=1).
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "SELECT is_deleted FROM users WHERE tg_id=$1",
            int(tg_id),
        )
    return bool(v)


async def reactivate_user_by_tg_id(tg_id: int) -> None:
    """Снимает флаг is_deleted у пользователя (используется при повторной регистрации)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_deleted=0, updated_at=$1 WHERE tg_id=$2",
            get_moscow_now_iso(),
            int(tg_id),
        )


async def get_user_by_username(username: str) -> dict | None:
    """
    Ищет пользователя по username (без @). Регистр игнорируется.
    """
    p = _assert_pool()
    uname = (username or "").strip().lstrip("@")
    if not uname:
        return None

    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM users
            WHERE is_deleted=0 AND LOWER(username)=LOWER($1)
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            uname,
        )
    return dict(row) if row else None


async def get_user_language_by_tg_id(tg_id: int) -> str:
    u = await get_user_by_tg_id(int(tg_id))
    lang = (u.get("language") if u else None) or "ru"
    lang = str(lang).strip().lower()
    return lang if lang in {"ru", "en"} else "ru"


async def ensure_user_author_code_by_user_id(user_id: int, *, salt: str | None = None) -> str:
    """
    Гарантирует наличие author_code у пользователя (по внутреннему id).
    Если кода нет — генерирует детерминированно и сохраняет.
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT id, author_code FROM users WHERE id=$1", int(user_id))
        if not row:
            raise ValueError(f"user not found id={user_id}")
        code = row.get("author_code")
        if code:
            return str(code)

    salt_val = (salt or os.getenv("AUTHOR_CODE_SALT") or "default-author-salt").strip()
    # Пытаемся несколько раз (на случай коллизии уникального индекса)
    for attempt in range(5):
        candidate = generate_author_code(int(user_id), f"{salt_val}:{attempt}")
        try:
            async with p.acquire() as conn:
                res = await conn.execute(
                    "UPDATE users SET author_code=$2, updated_at=$3 WHERE id=$1",
                    int(user_id),
                    candidate,
                    get_moscow_now_iso(),
                )
                if res.startswith("UPDATE ") and not res.endswith(" 0"):
                    return candidate
        except UniqueViolationError:
            continue

    # Если не удалось записать — пробуем последний раз с рандомом
    rnd_candidate = generate_author_code(int(user_id), f"{salt_val}:{time.time()}:{random.random()}")
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET author_code=$2, updated_at=$3 WHERE id=$1",
            int(user_id),
            rnd_candidate,
            get_moscow_now_iso(),
        )
    return rnd_candidate


async def ensure_user_author_code(tg_id: int, *, salt: str | None = None) -> str:
    """
    Гарантирует наличие author_code по tg_id (создаёт user row при необходимости).
    """
    await _ensure_user_row(int(tg_id))
    user = await get_user_by_tg_id(int(tg_id))
    if not user:
        raise ValueError("user not found")
    return await ensure_user_author_code_by_user_id(int(user["id"]), salt=salt)


async def set_user_language_by_tg_id(tg_id: int, lang: str) -> None:
    await _ensure_user_row(int(tg_id))
    v = (lang or "").strip().lower()
    if v not in {"ru", "en"}:
        v = "ru"

    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET language=$1, updated_at=$2 WHERE tg_id=$3",
            v,
            get_moscow_now_iso(),
            int(tg_id),
        )


async def get_user_by_id(user_id: int) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id=$1", int(user_id))
    return dict(row) if row else None


async def get_user_by_username(username: str) -> dict | None:
    u = (username or "").lstrip("@").strip()
    if not u:
        return None
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE lower(username)=lower($1) AND is_deleted=0",
            u,
        )
    return dict(row) if row else None


async def update_user_name(user_id: int, name: str) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE users SET name=$1, updated_at=$2 WHERE id=$3",
                           name, get_moscow_now_iso(), int(user_id))


async def update_user_gender(user_id: int, gender: str | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE users SET gender=$1, updated_at=$2 WHERE id=$3",
                           gender, get_moscow_now_iso(), int(user_id))


async def update_user_age(user_id: int, age: int | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE users SET age=$1, updated_at=$2 WHERE id=$3",
                           age, get_moscow_now_iso(), int(user_id))


async def update_user_bio(user_id: int, bio: str | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE users SET bio=$1, updated_at=$2 WHERE id=$3",
                           bio, get_moscow_now_iso(), int(user_id))


async def update_user_channel_link(user_id: int, link: str | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE users SET tg_channel_link=$1, updated_at=$2 WHERE id=$3",
                           link, get_moscow_now_iso(), int(user_id))
        

async def update_user_city(user_id: int, city: str | None) -> None:
    p = _assert_pool()
    value = (city or "").strip() or None
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET city=$1, updated_at=$2 WHERE id=$3",
            value, get_moscow_now_iso(), int(user_id)
        )

async def update_user_country(user_id: int, country: str | None) -> None:
    p = _assert_pool()
    value = (country or "").strip() or None
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET country=$1, updated_at=$2 WHERE id=$3",
            value, get_moscow_now_iso(), int(user_id)
        )

async def set_user_city_visibility(user_id: int, show: bool) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET show_city=$1, updated_at=$2 WHERE id=$3",
            1 if show else 0, get_moscow_now_iso(), int(user_id)
        )

async def set_user_country_visibility(user_id: int, show: bool) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET show_country=$1, updated_at=$2 WHERE id=$3",
            1 if show else 0, get_moscow_now_iso(), int(user_id)
        )


async def set_user_allow_ratings_by_tg_id(tg_id: int, allow: bool) -> dict | None:
    """Включить/выключить оценки для пользователя и синхронно обновить все активные фото."""
    await _ensure_user_row(int(tg_id))
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE users
            SET allow_ratings=$2, updated_at=$3
            WHERE tg_id=$1
            RETURNING id, tg_id, allow_ratings
            """,
            int(tg_id),
            1 if allow else 0,
            now,
        )

        if row and row.get("id"):
            try:
                await conn.execute(
                    """
                    UPDATE photos
                    SET ratings_enabled=$2
                    WHERE user_id=$1 AND is_deleted=0
                    """,
                    int(row["id"]),
                    1 if allow else 0,
                )
            except Exception:
                # Не блокируем обновление пользователя, если массовое обновление фото не удалось
                pass

    return dict(row) if row else None


async def toggle_user_allow_ratings_by_tg_id(tg_id: int) -> dict | None:
    """Инвертировать флаг allow_ratings у пользователя и синхронизировать активные фото."""
    await _ensure_user_row(int(tg_id))
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE users
            SET allow_ratings = CASE WHEN allow_ratings=0 THEN 1 ELSE 0 END,
                updated_at=$2
            WHERE tg_id=$1
            RETURNING id, tg_id, allow_ratings
            """,
            int(tg_id),
            now,
        )

        if row and row.get("id") is not None:
            try:
                await conn.execute(
                    """
                    UPDATE photos
                    SET ratings_enabled=$2
                    WHERE user_id=$1 AND is_deleted=0
                    """,
                    int(row["id"]),
                    1 if bool(row["allow_ratings"]) else 0,
                )
            except Exception:
                pass

    return dict(row) if row else None


async def set_all_active_photos_ratings_enabled(user_id: int) -> None:
    """Принудительно включить оценки на всех активных фотографиях пользователя."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE photos SET ratings_enabled=1 WHERE user_id=$1 AND is_deleted=0",
            int(user_id),
        )


# backward-compatible alias
set_all_user_photos_ratings_enabled = set_all_active_photos_ratings_enabled


# -------------------- ads settings --------------------

async def get_ads_enabled_by_tg_id(tg_id: int) -> bool | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT ads_enabled FROM users WHERE tg_id=$1", int(tg_id))
    if v is None:
        return None
    return bool(v)


async def set_ads_enabled_by_tg_id(tg_id: int, enabled: bool) -> dict | None:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE users
            SET ads_enabled=$2, updated_at=$3
            WHERE tg_id=$1
            RETURNING id, tg_id, ads_enabled
            """,
            int(tg_id),
            1 if enabled else 0,
            now,
        )
    return dict(row) if row else None


async def soft_delete_user(tg_id: int) -> None:
    """Mark user as deleted by tg_id (soft delete, keeps row for audits)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_deleted=1, updated_at=$1 WHERE tg_id=$2",
            get_moscow_now_iso(),
            int(tg_id),
        )


# -------------------- roles / blocks --------------------

async def set_user_admin_by_tg_id(tg_id: int, is_admin: bool) -> None:
    await _ensure_user_row(tg_id)
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE users SET is_admin=$1, updated_at=$2 WHERE tg_id=$3",
                           1 if is_admin else 0, get_moscow_now_iso(), int(tg_id))


async def is_moderator_by_tg_id(tg_id: int) -> bool:
    u = await get_user_by_tg_id(tg_id)
    return bool(u and u.get("is_moderator"))


async def set_user_moderator_by_tg_id(tg_id: int, is_mod: bool) -> None:
    await _ensure_user_row(tg_id)
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE users SET is_moderator=$1, updated_at=$2 WHERE tg_id=$3",
                           1 if is_mod else 0, get_moscow_now_iso(), int(tg_id))


async def set_user_helper_by_tg_id(tg_id: int, is_helper: bool) -> None:
    await _ensure_user_row(tg_id)
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE users SET is_helper=$1, updated_at=$2 WHERE tg_id=$3",
                           1 if is_helper else 0, get_moscow_now_iso(), int(tg_id))


async def set_user_support_by_tg_id(tg_id: int, is_support: bool) -> None:
    await _ensure_user_row(tg_id)
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE users SET is_support=$1, updated_at=$2 WHERE tg_id=$3",
                           1 if is_support else 0, get_moscow_now_iso(), int(tg_id))


async def get_user_block_status_by_tg_id(tg_id: int) -> dict:
    u = await get_user_by_tg_id(tg_id)
    if not u:
        return {"is_blocked": False, "block_reason": None, "block_until": None}
    return {"is_blocked": bool(u.get("is_blocked")),
            "block_reason": u.get("block_reason"),
            "block_until": u.get("block_until")}


async def set_user_block_status_by_tg_id(tg_id: int, is_blocked: bool,
                                        reason: str | None = None, until_iso: str | None = None) -> None:
    await _ensure_user_row(tg_id)
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET is_blocked=$1, block_reason=$2, block_until=$3, updated_at=$4
            WHERE tg_id=$5
            """,
            1 if is_blocked else 0, reason, until_iso, get_moscow_now_iso(), int(tg_id)
        )


async def hide_active_photos_for_user(user_id: int, new_status: str = "blocked_by_ban") -> int:
    """
    Скрыть все активные/одобренные фото пользователя из выдачи (ставим новый статус).
    Возвращает количество обновлённых записей (best-effort).
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE photos
            SET moderation_status=$2
            WHERE user_id=$1 AND is_deleted=0 AND moderation_status IN ('active','good')
            """,
            int(user_id),
            str(new_status),
        )
    try:
        return int(res.split()[-1])
    except Exception:
        return 0


async def restore_photos_from_status(
    user_id: int,
    from_status: str = "blocked_by_ban",
    to_status: str = "active",
) -> int:
    """
    Вернуть фото пользователя из статуса `from_status` в `to_status`.
    Используем при разбане (чтобы не трогать другие статусы).
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE photos
            SET moderation_status=$3
            WHERE user_id=$1 AND is_deleted=0 AND moderation_status=$2
            """,
            int(user_id),
            str(from_status),
            str(to_status),
        )
    try:
        return int(res.split()[-1])
    except Exception:
        return 0


# -------------------- premium --------------------

async def set_user_premium_status(tg_id: int, is_premium: bool, premium_until: str | None = None) -> None:
    await _ensure_user_row(tg_id)
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE users SET is_premium=$1, premium_until=$2, updated_at=$3 WHERE tg_id=$4",
                           1 if is_premium else 0, premium_until, get_moscow_now_iso(), int(tg_id))


async def set_user_premium_role_by_tg_id(tg_id: int, is_premium_role: bool) -> None:
    st = await get_user_premium_status(tg_id)
    await set_user_premium_status(tg_id, bool(is_premium_role), st.get("premium_until"))


async def get_user_premium_status(tg_id: int) -> dict:
    u = await get_user_by_tg_id(tg_id)
    if not u:
        return {"is_premium": False, "premium_until": None}
    return {"is_premium": bool(u.get("is_premium")), "premium_until": u.get("premium_until")}


async def is_user_premium_active(tg_id: int) -> bool:
    u = await get_user_by_tg_id(tg_id)
    if not u or not u.get("is_premium"):
        return False
    until_raw = u.get("premium_until")
    until = str(until_raw).strip() if until_raw is not None else ""
    if not until:
        return True
    try:
        dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except Exception:
        return False
    now = get_moscow_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    try:
        return dt > now
    except Exception:
        return False


# --- Premium expiry reminders ---

async def get_users_with_premium_expiring_tomorrow(limit: int = 2000, offset: int = 0) -> list[dict]:
    """Пользователи, у которых premium_until приходится на завтрашний день (по Москве).

    Возвращает список: {"tg_id": int, "premium_until": str}
    limit/offset нужны, чтобы можно было безопасно обходить большую базу батчами.
    """
    p = _assert_pool()

    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tg_id, premium_until
            FROM users
            WHERE is_deleted=0
              AND is_premium=1
              AND premium_until IS NOT NULL
            ORDER BY tg_id
            OFFSET $1 LIMIT $2
            """,
            int(offset or 0),
            int(limit),
        )

    now = get_moscow_now()
    tomorrow = (now + timedelta(days=1)).date()

    res: list[dict] = []
    for r in rows:
        until_iso = r["premium_until"]
        if not until_iso:
            continue
        try:
            dt = datetime.fromisoformat(str(until_iso))
        except Exception:
            continue

        # если уже истёк — пропускаем
        if dt <= now:
            continue

        if dt.date() == tomorrow:
            res.append({"tg_id": int(r["tg_id"]), "premium_until": str(until_iso)})

    return res


async def mark_premium_expiry_reminder_sent(tg_id: int, premium_until: str) -> bool:
    """Идемпотентно отмечаем, что напоминание отправлено.

    True = только что отметили (значит можно отправлять)
    False = уже было отправлено раньше
    """
    p = _assert_pool()
    now_iso = get_moscow_now_iso()

    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO premium_expiry_reminders (tg_id, premium_until, sent_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (tg_id, premium_until) DO NOTHING
            RETURNING tg_id
            """,
            int(tg_id),
            str(premium_until),
            now_iso,
        )

    return row is not None


# -------------------- awards --------------------

async def give_achievement_to_user_by_code(user_tg_id: int, code: str, granted_by_tg_id: int | None = None) -> bool:
    u = await _ensure_user_row(user_tg_id)
    if not u:
        return False
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        user_id = int(u["id"])
        existed = await conn.fetchval("SELECT 1 FROM awards WHERE user_id=$1 AND code=$2 LIMIT 1", user_id, code)
        if existed:
            return False

        granted_by_user_id = None
        if granted_by_tg_id is not None:
            gb = await conn.fetchrow("SELECT id FROM users WHERE tg_id=$1 AND is_deleted=0", int(granted_by_tg_id))
            if gb:
                granted_by_user_id = int(gb["id"])

        if code == "beta_tester":
            title = "Бета-тестер бота"
            description = "Ты помог(ла) тестировать GlowShot на ранних стадиях до релиза."
            icon = "🏆"
            is_special = 1
        else:
            title = code
            description = None
            icon = "🏅"
            is_special = 0

        await conn.execute(
            """
            INSERT INTO awards (user_id, code, title, description, icon, is_special, granted_by_user_id, created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            user_id, code, title, description, icon, is_special, granted_by_user_id, now
        )
    return True


async def get_awards_for_user(user_id: int) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM awards WHERE user_id=$1 ORDER BY created_at DESC, id DESC",
                                int(user_id))
    return [dict(r) for r in rows]


async def get_award_by_id(award_id: int) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM awards WHERE id=$1", int(award_id))
    return dict(row) if row else None


async def delete_award_by_id(award_id: int) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM awards WHERE id=$1", int(award_id))


async def update_award_text(award_id: int, title: str, description: str | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE awards SET title=$1, description=$2 WHERE id=$3",
                           title, description, int(award_id))


async def update_award_icon(award_id: int, icon: str | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE awards SET icon=$1 WHERE id=$2", icon, int(award_id))


async def create_custom_award_for_user(user_id: int, title: str, description: str | None,
                                       icon: str | None, code: str | None = None,
                                       is_special: bool = False, granted_by_user_id: int | None = None) -> int:
    p = _assert_pool()
    now = get_moscow_now_iso()
    if code is None:
        code = f"custom_{user_id}_{int(get_moscow_now().timestamp())}"
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO awards (user_id, code, title, description, icon, is_special, granted_by_user_id, created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            RETURNING id
            """,
            int(user_id), code, title, description, icon, 1 if is_special else 0, granted_by_user_id, now
        )
    return int(row["id"])


# -------------------- referrals --------------------

async def get_or_create_referral_code(user_tg_id: int) -> str:
    u = await _ensure_user_row(user_tg_id)
    if not u:
        return ""
    if u.get("referral_code"):
        return str(u["referral_code"])

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "GS" + "".join(random.choice(alphabet) for _ in range(8))

    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE users SET referral_code=$1, updated_at=$2 WHERE id=$3",
                           code, get_moscow_now_iso(), int(u["id"]))
    return code


async def save_pending_referral(new_user_tg_id: int, referral_code: str | None) -> None:
    if not referral_code:
        return
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_referrals (new_user_tg_id, referral_code, created_at)
            VALUES ($1,$2,$3)
            ON CONFLICT (new_user_tg_id)
            DO UPDATE SET referral_code=EXCLUDED.referral_code, created_at=EXCLUDED.created_at
            """,
            int(new_user_tg_id), str(referral_code), now
        )


async def bind_referral_to_invited_tg_id(invited_tg_id: int, referral_code: str | None) -> bool:
    """Bind invited tg_id to inviter by referral code once. No rewards here."""
    code = (referral_code or "").strip()
    if not code:
        return False

    invited = await ensure_user_minimal_row(int(invited_tg_id))
    if not invited:
        return False
    invited_user_id = int(invited["id"])

    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        inviter = await conn.fetchrow(
            """
            SELECT id, tg_id
            FROM users
            WHERE referral_code=$1
              AND is_deleted=0
            LIMIT 1
            """,
            code,
        )
        if not inviter:
            return False

        inviter_user_id = int(inviter["id"])
        inviter_tg_id = int(inviter["tg_id"])
        if inviter_user_id == invited_user_id or inviter_tg_id == int(invited_tg_id):
            return False

        inserted = await conn.execute(
            """
            INSERT INTO referrals (inviter_user_id, invited_user_id, created_at, qualified, qualified_at)
            VALUES ($1, $2, $3, 0, NULL)
            ON CONFLICT (invited_user_id) DO NOTHING
            """,
            inviter_user_id,
            invited_user_id,
            now,
        )
        await conn.execute(
            "DELETE FROM pending_referrals WHERE new_user_tg_id=$1",
            int(invited_tg_id),
        )
    return inserted.endswith("1")


async def get_referral_stats_for_user(user_tg_id: int) -> dict:
    u = await get_user_by_tg_id(user_tg_id)
    if not u:
        return {"invited_qualified": 0}
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM referral_rewards
            WHERE inviter_user_id=$1
            """,
            int(u["id"]),
        )
    return {"invited_qualified": int(v or 0)}


async def _link_referral_from_pending_if_needed(
    conn: asyncpg.Connection,
    *,
    invited_tg_id: int,
    invited_user_id: int,
    now_iso: str,
) -> None:
    pending = await conn.fetchrow(
        "SELECT referral_code FROM pending_referrals WHERE new_user_tg_id=$1",
        int(invited_tg_id),
    )
    if not pending:
        return

    code = str(pending.get("referral_code") or "").strip()
    if not code:
        return

    inviter = await conn.fetchrow(
        """
        SELECT id, tg_id
        FROM users
        WHERE referral_code=$1
          AND is_deleted=0
        LIMIT 1
        """,
        code,
    )
    if not inviter:
        return

    inviter_user_id = int(inviter["id"])
    inviter_tg_id = int(inviter["tg_id"])
    if inviter_user_id == int(invited_user_id) or inviter_tg_id == int(invited_tg_id):
        return

    await conn.execute(
        """
        INSERT INTO referrals (inviter_user_id, invited_user_id, created_at, qualified, qualified_at)
        VALUES ($1, $2, $3, 0, NULL)
        ON CONFLICT (invited_user_id) DO NOTHING
        """,
        inviter_user_id,
        int(invited_user_id),
        now_iso,
    )


async def _add_premium_hours(conn: asyncpg.Connection, user_id: int, hours: int, *, now_dt: datetime) -> None:
    row = await conn.fetchrow(
        "SELECT premium_until FROM users WHERE id=$1 FOR UPDATE",
        int(user_id),
    )
    base = now_dt
    if row and row["premium_until"]:
        try:
            current = datetime.fromisoformat(str(row["premium_until"]))
            base = current if current > now_dt else now_dt
        except Exception:
            base = now_dt

    new_until = base + timedelta(hours=int(hours))
    await conn.execute(
        """
        UPDATE users
        SET is_premium=1,
            premium_until=$2,
            updated_at=$3
        WHERE id=$1
        """,
        int(user_id),
        new_until.isoformat(),
        now_dt.isoformat(),
    )


async def _apply_referral_reward_to_user(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    credits: int,
    premium_hours: int,
    now_dt: datetime,
) -> None:
    await _ensure_user_stats_row(conn, int(user_id))
    await conn.execute(
        """
        UPDATE user_stats
        SET credits = credits + $2,
            last_active_at = $3
        WHERE user_id=$1
        """,
        int(user_id),
        int(credits),
        now_dt,
    )
    await _add_premium_hours(conn, int(user_id), int(premium_hours), now_dt=now_dt)


async def try_award_referral(invited_tg_id: int) -> tuple[bool, int | None, int | None]:
    """Try to award referral bonus for invited user. Idempotent."""
    p = _assert_pool()
    now_dt = get_bot_now()
    now_iso = now_dt.isoformat()

    async with p.acquire() as conn:
        async with conn.transaction():
            invited = await conn.fetchrow(
                """
                SELECT id, tg_id, name
                FROM users
                WHERE tg_id=$1
                  AND is_deleted=0
                LIMIT 1
                """,
                int(invited_tg_id),
            )
            if not invited:
                return False, None, None

            invited_user_id = int(invited["id"])
            invited_name = str(invited.get("name") or "").strip()
            if not invited_name:
                return False, None, None

            has_vote = await conn.fetchval(
                """
                SELECT 1
                FROM votes
                WHERE voter_id=$1
                LIMIT 1
                """,
                invited_user_id,
            )
            if not has_vote:
                has_vote = await conn.fetchval(
                    """
                    SELECT 1
                    FROM ratings
                    WHERE user_id=$1
                    LIMIT 1
                    """,
                    invited_user_id,
                )
            if not has_vote:
                return False, None, None

            referral = await conn.fetchrow(
                """
                SELECT inviter_user_id
                FROM referrals
                WHERE invited_user_id=$1
                FOR UPDATE
                """,
                invited_user_id,
            )
            if not referral:
                await _link_referral_from_pending_if_needed(
                    conn,
                    invited_tg_id=int(invited_tg_id),
                    invited_user_id=invited_user_id,
                    now_iso=now_iso,
                )
                referral = await conn.fetchrow(
                    """
                    SELECT inviter_user_id
                    FROM referrals
                    WHERE invited_user_id=$1
                    FOR UPDATE
                    """,
                    invited_user_id,
                )
            if not referral:
                return False, None, None

            inviter_user_id = int(referral["inviter_user_id"])
            if inviter_user_id == invited_user_id:
                return False, None, None

            inviter = await conn.fetchrow(
                """
                SELECT id, tg_id
                FROM users
                WHERE id=$1
                  AND is_deleted=0
                FOR UPDATE
                """,
                inviter_user_id,
            )
            if not inviter:
                return False, None, None
            inviter_tg_id = int(inviter["tg_id"])
            if inviter_tg_id == int(invited_tg_id):
                return False, None, None

            reward_row = await conn.fetchrow(
                """
                INSERT INTO referral_rewards (invited_user_id, inviter_user_id, rewarded_at, reward_type, reward_version)
                VALUES ($1, $2, $3, 'premium_credits', 'v2_3h_2c')
                ON CONFLICT (invited_user_id) DO NOTHING
                RETURNING id
                """,
                invited_user_id,
                inviter_user_id,
                now_dt,
            )
            if not reward_row:
                return False, None, None

            await _apply_referral_reward_to_user(
                conn,
                user_id=invited_user_id,
                credits=2,
                premium_hours=3,
                now_dt=now_dt,
            )
            await _apply_referral_reward_to_user(
                conn,
                user_id=inviter_user_id,
                credits=2,
                premium_hours=3,
                now_dt=now_dt,
            )

            await conn.execute(
                """
                UPDATE referrals
                SET qualified=1,
                    qualified_at=$2
                WHERE invited_user_id=$1
                """,
                invited_user_id,
                now_iso,
            )
            await conn.execute(
                "DELETE FROM pending_referrals WHERE new_user_tg_id=$1",
                int(invited_tg_id),
            )

            return True, inviter_tg_id, int(invited_tg_id)


async def link_and_reward_referral_if_needed(invited_tg_id: int):
    """Backward-compatible alias for old handlers."""
    return await try_award_referral(int(invited_tg_id))

async def _add_premium_days(conn, user_id: int, days: int) -> None:
    row = await conn.fetchrow(
        "SELECT premium_until FROM users WHERE id=$1",
        int(user_id),
    )
    now = get_moscow_now()

    base = now
    if row and row["premium_until"]:
        try:
            current = datetime.fromisoformat(row["premium_until"])
            base = current if current > now else now
        except Exception:
            base = now

    new_until = base + timedelta(days=days)

    await conn.execute(
        """
        UPDATE users
        SET is_premium=1,
            premium_until=$2,
            updated_at=$3
        WHERE id=$1
        """,
        int(user_id),
        new_until.isoformat(),
        get_moscow_now_iso(),
    )

async def get_premium_users_page(limit: int = 20, offset: int = 0) -> list[dict]:
    """Страница премиум-пользователей (для админских списков)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
            FROM users
            WHERE is_premium=1 AND is_deleted=0
            ORDER BY premium_until DESC NULLS LAST, id DESC
            OFFSET $1 LIMIT $2
            """,
            int(offset or 0),
            int(limit),
        )
    return [dict(r) for r in rows]

async def get_top_users_by_activity_events(limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
    """(total, rows) — топ пользователей по количеству событий активности."""
    p = _assert_pool()

    async with p.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM (
              SELECT user_id
              FROM activity_events ae
              JOIN users u ON u.id = ae.user_id
              WHERE ae.user_id IS NOT NULL
                AND u.is_deleted=0
                AND COALESCE(NULLIF(trim(u.name), ''), NULL) IS NOT NULL
                AND COALESCE(u.is_blocked,0)=0
              GROUP BY user_id
            ) t
            """
        )

        rows = await conn.fetch(
            """
            SELECT u.*, COUNT(a.id) AS events_count
            FROM activity_events a
            JOIN users u ON u.id = a.user_id
            WHERE a.user_id IS NOT NULL
              AND u.is_deleted=0
              AND COALESCE(NULLIF(trim(u.name), ''), NULL) IS NOT NULL
              AND COALESCE(u.is_blocked,0)=0
            GROUP BY u.id
            ORDER BY COUNT(a.id) DESC, u.updated_at DESC NULLS LAST, u.id DESC
            OFFSET $1 LIMIT $2
            """,
            int(offset or 0),
            int(limit),
        )

    return int(total or 0), [dict(r) for r in rows]

# -------------------- comments --------------------

async def create_comment(user_id: int, photo_id: int, text: str, is_public: bool = True, **kwargs) -> None:
    """Сохранить комментарий к фото.
    is_public=True — публичный, False — анонимный.
    **kwargs — чтобы любые будущие аргументы не ломали вызовы.
    """
    p = _assert_pool()

    pid = int(photo_id)
    uid = int(user_id)
    txt = (text or "").strip()
    if not txt:
        return

    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO comments (photo_id, user_id, text, is_public, created_at)
            VALUES ($1,$2,$3,$4,$5)
            """,
            pid,
            uid,
            txt,
            1 if bool(is_public) else 0,
            get_moscow_now_iso(),
        )


async def get_comment_counts_for_photo(photo_id: int) -> dict:
    """Return counts of comments by visibility for a photo.

    Returns: {"public": int, "anonymous": int}
    """
    p = _assert_pool()
    query = """
        SELECT
            SUM(CASE WHEN is_public=1 THEN 1 ELSE 0 END) AS public,
            SUM(CASE WHEN is_public=0 THEN 1 ELSE 0 END) AS anonymous
        FROM comments
        WHERE photo_id = $1
    """
    async with p.acquire() as conn:
        row = await conn.fetchrow(query, int(photo_id))
    if not row:
        return {"public": 0, "anonymous": 0}
    return {"public": int(row["public"] or 0), "anonymous": int(row["anonymous"] or 0)}


async def get_comments_for_photo_sorted(
    photo_id: int,
    *,
    limit: int = 20,
    offset: int = 0,
    sort_key: str = "date",   # "date" | "score"
    sort_dir: str = "desc",   # "asc" | "desc"
) -> list[dict]:
    """Comments for photo with sorting and rating value attached as 'score'.

    ratings table uses column `value`.
    """
    sort_key = sort_key if sort_key in {"date", "score"} else "date"
    sort_dir = sort_dir if sort_dir in {"asc", "desc"} else "desc"

    if sort_key == "score":
        order_clause = f"ORDER BY r.value {sort_dir.upper()} NULLS LAST, c.created_at DESC"
    else:
        order_clause = f"ORDER BY c.created_at {sort_dir.upper()}"

    p = _assert_pool()
    query = f"""
        SELECT
            c.id,
            c.photo_id,
            c.user_id,
            c.text,
            c.is_public,
            c.created_at,
            u.username,
            u.name AS author_name,
            r.value AS score
        FROM comments c
        LEFT JOIN users u ON u.id = c.user_id
        LEFT JOIN ratings r ON r.user_id = c.user_id AND r.photo_id = c.photo_id
        WHERE c.photo_id = $1
        {order_clause}
        LIMIT $2 OFFSET $3
    """
    async with p.acquire() as conn:
        rows = await conn.fetch(query, int(photo_id), int(limit), int(offset))

    return [dict(r) for r in rows]


async def get_user_admin_stats(user_id: int) -> dict:
    """Статистика пользователя для админки.

    Ожидаемые ключи (см. handlers/admin.py):
    - messages_total: суммарно действий (оценки + комментарии + жалобы)
    - ratings_given: сколько оценок поставил
    - comments_given: сколько комментариев оставил
    - reports_created: сколько жалоб создал
    - active_photos: сколько фото сейчас активно (не удалено)
    - total_photos: сколько всего фото загружал (включая удалённые)
    - upload_bans_count: сколько раз получал ограничения на загрузку (если нет истории — 0)

    user_id здесь — внутренний users.id.
    """
    p = _assert_pool()

    async with p.acquire() as conn:
        ratings_given = await conn.fetchval(
            "SELECT COUNT(*) FROM ratings WHERE user_id=$1",
            int(user_id),
        )
        comments_given = await conn.fetchval(
            "SELECT COUNT(*) FROM comments WHERE user_id=$1",
            int(user_id),
        )
        reports_created = await conn.fetchval(
            "SELECT COUNT(*) FROM photo_reports WHERE user_id=$1",
            int(user_id),
        )

        # Фото пользователя
        total_photos = await conn.fetchval(
            "SELECT COUNT(*) FROM photos WHERE user_id=$1",
            int(user_id),
        )
        active_photos = await conn.fetchval(
            "SELECT COUNT(*) FROM photos WHERE user_id=$1 AND is_deleted=0",
            int(user_id),
        )

    ratings_given_i = int(ratings_given or 0)
    comments_given_i = int(comments_given or 0)
    reports_created_i = int(reports_created or 0)

    return {
        "messages_total": ratings_given_i + comments_given_i + reports_created_i,
        "ratings_given": ratings_given_i,
        "comments_given": comments_given_i,
        "reports_created": reports_created_i,
        "active_photos": int(active_photos or 0),
        "total_photos": int(total_photos or 0),
        "upload_bans_count": 0,
    }

# -------------------- premium news --------------------

async def add_premium_news(text: str) -> int:
    """Add a premium news item (for admin tooling).

    Returns inserted id.
    """
    p = _assert_pool()
    t = (text or "").strip()
    if not t:
        return 0
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO premium_news (text, created_at)
            VALUES ($1,$2)
            RETURNING id
            """,
            t,
            now,
        )
    return int(row["id"]) if row else 0


async def get_premium_news_since(since_iso: str, limit: int = 10) -> list[str]:
    """Get news items created since `since_iso` (ISO string), newest-first."""
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT text
            FROM premium_news
            WHERE created_at >= $1
            ORDER BY created_at DESC, id DESC
            LIMIT $2
            """,
            str(since_iso),
            int(limit),
        )
    return [str(r["text"]) for r in rows]


async def get_premium_benefits() -> list[dict]:
    """Return ordered list of premium benefits."""
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, position, title, description, created_at, updated_at
            FROM premium_benefits
            ORDER BY position ASC, id ASC
            """
        )
    return [dict(r) for r in rows]


async def add_premium_benefit(title: str, description: str) -> int:
    """Add benefit to the end of the list; returns inserted id."""
    p = _assert_pool()
    now = get_moscow_now_iso()
    t = (title or "").strip()
    d = (description or "").strip()
    if not t:
        return 0
    async with p.acquire() as conn:
        pos = await conn.fetchval("SELECT COALESCE(MAX(position), 0) + 1 FROM premium_benefits")
        row = await conn.fetchrow(
            """
            INSERT INTO premium_benefits (position, title, description, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$4)
            RETURNING id
            """,
            int(pos),
            t,
            d,
            now,
        )
    return int(row["id"]) if row else 0


async def update_premium_benefit(benefit_id: int, title: str, description: str) -> bool:
    """Update benefit title/description by id."""
    p = _assert_pool()
    t = (title or "").strip()
    d = (description or "").strip()
    if not t:
        return False
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE premium_benefits
            SET title=$1, description=$2, updated_at=$3
            WHERE id=$4
            """,
            t,
            d,
            now,
            int(benefit_id),
        )
    return res.lower().startswith("update") and "0" not in res.split()


async def swap_premium_benefits(order_idx1: int, order_idx2: int) -> bool:
    """Swap benefits by their 1-based order (position)."""
    if order_idx1 == order_idx2:
        return True
    benefits = await get_premium_benefits()
    n = len(benefits)
    if (
        order_idx1 <= 0
        or order_idx2 <= 0
        or order_idx1 > n
        or order_idx2 > n
    ):
        return False

    b1 = benefits[order_idx1 - 1]
    b2 = benefits[order_idx2 - 1]
    id1, pos1 = int(b1["id"]), int(b1["position"])
    id2, pos2 = int(b2["id"]), int(b2["position"])

    p = _assert_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE premium_benefits
                SET position = CASE
                    WHEN id=$1 THEN $3
                    WHEN id=$2 THEN $4
                    ELSE position
                END
                WHERE id IN ($1,$2)
                """,
                id1,
                id2,
                pos2,
                pos1,
            )
    return True

# -------------------- payments --------------------


async def log_bot_error(
    chat_id: int | None = None,
    tg_user_id: int | None = None,
    handler: str | None = None,
    update_type: str | None = None,
    error_type: str | None = None,
    error_text: str | None = None,
    traceback_text: str | None = None,
) -> None:
    """Сохраняет ошибку бота в таблицу bot_error_logs для админки."""
    p = _assert_pool()

    # Ограничим размеры, чтобы не убить базу огромным traceback
    def _cut(s: str | None, n: int) -> str | None:
        if s is None:
            return None
        s = str(s)
        return s if len(s) <= n else s[: n - 3] + "..."

    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_error_logs (
              chat_id,
              tg_user_id,
              handler,
              update_type,
              error_type,
              error_text,
              traceback_text,
              created_at
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,NOW())
            """,
            int(chat_id) if chat_id is not None else None,
            int(tg_user_id) if tg_user_id is not None else None,
            _cut(handler, 200),
            _cut(update_type, 100),
            _cut(error_type, 200),
            _cut(error_text, 2000),
            _cut(traceback_text, 20000),
        )


async def get_bot_error_logs_page(offset: int, limit: int) -> list[dict]:
    """Возвращает страницу логов ошибок (для админки), newest-first."""
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
            FROM bot_error_logs
            ORDER BY created_at DESC, id DESC
            OFFSET $1 LIMIT $2
            """,
            int(offset),
            int(limit),
        )
    return [dict(r) for r in rows]


async def get_bot_error_logs_count() -> int:
    """Total number of rows in bot_error_logs (for pagination in admin UI)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM bot_error_logs")
    return int(v or 0)


async def get_bot_error_log_by_id(log_id: int) -> dict | None:
    """Return one error log row by id (for admin details view)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM bot_error_logs
            WHERE id=$1
            LIMIT 1
            """,
            int(log_id),
        )
    return dict(row) if row else None


async def clear_bot_error_logs() -> None:
    """Полностью очищает таблицу bot_error_logs (для админки)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM bot_error_logs")

async def log_successful_payment(
    tg_id: int,
    provider: str = "unknown",
    amount_rub: int | None = None,
    amount_stars: int | None = None,
    period_code: str | None = None,
    inv_id: str | None = None,
    *,
    status: str = "success",
    order_id: str | None = None,
    payment_id: str | None = None,
) -> None:
    """Generic payment logger.
    Legacy: inv_id. TBank: order_id + payment_id.
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO payments (
              tg_id, provider, amount_rub, amount_stars, period_code, inv_id, order_id, payment_id, status, created_at
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            int(tg_id),
            str(provider),
            amount_rub,
            amount_stars,
            period_code,
            inv_id,
            order_id,
            payment_id,
            str(status),
            get_moscow_now_iso(),
        )


async def create_pending_tbank_payment(
    *,
    tg_id: int,
    period_code: str,
    amount_rub: int,
    order_id: str,
) -> None:
    """Логируем pending до редиректа на оплату."""
    p = _assert_pool()
    await _ensure_user_row(int(tg_id))
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO payments (tg_id, provider, amount_rub, period_code, order_id, status, created_at)
            VALUES ($1,'tbank',$2,$3,$4,'pending',$5)
            ON CONFLICT (provider, order_id) DO NOTHING
            """,
            int(tg_id),
            int(amount_rub),
            str(period_code),
            str(order_id),
            get_moscow_now_iso(),
        )


def _plan_to_days(plan: str) -> int:
    p = (plan or "").strip().lower()
    if p in {"w", "week", "7d", "7"}:
        return 7
    if p in {"m", "month", "30d", "30"}:
        return 30
    if p in {"q", "3m", "3month", "90d", "90"}:
        return 90
    return 30


async def apply_tbank_payment_confirmed(
    *,
    tg_id: int,
    plan: str,
    order_id: str,
    payment_id: str,
    amount_rub: int | None = None,
) -> bool:
    """Идемпотентно: помечаем платеж success и продлеваем премиум 1 раз.
    True = сейчас активировали/продлили, False = уже было обработано.
    """
    p = _assert_pool()
    await _ensure_user_row(int(tg_id))

    days = _plan_to_days(plan)
    now_iso = get_moscow_now_iso()

    async with p.acquire() as conn:
        async with conn.transaction():
            already = await conn.fetchval(
                """
                SELECT 1
                FROM payments
                WHERE provider='tbank'
                  AND order_id=$1
                  AND status='success'
                LIMIT 1
                """,
                str(order_id),
            )
            if already:
                return False

            # если pending не успели создать — создадим
            await conn.execute(
                """
                INSERT INTO payments (tg_id, provider, amount_rub, period_code, order_id, payment_id, status, created_at)
                VALUES ($1,'tbank',$2,$3,$4,$5,'pending',$6)
                ON CONFLICT (provider, order_id) DO NOTHING
                """,
                int(tg_id),
                int(amount_rub) if amount_rub is not None else None,
                str(plan),
                str(order_id),
                str(payment_id),
                now_iso,
            )

            # отмечаем успех
            await conn.execute(
                """
                UPDATE payments
                SET status='success',
                    payment_id=COALESCE($2, payment_id),
                    amount_rub=COALESCE($3, amount_rub)
                WHERE provider='tbank' AND order_id=$1
                """,
                str(order_id),
                str(payment_id) if payment_id else None,
                int(amount_rub) if amount_rub is not None else None,
            )

            # продление премиума
            user_id = await conn.fetchval(
                "SELECT id FROM users WHERE tg_id=$1 AND is_deleted=0 LIMIT 1",
                int(tg_id),
            )
            if not user_id:
                return False

            await _add_premium_days(conn, int(user_id), days=int(days))

    return True


async def get_payments_count() -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM payments")
    return int(v or 0)


async def get_payments_page(offset: int, limit: int) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM payments ORDER BY created_at DESC, id DESC OFFSET $1 LIMIT $2",
            int(offset), int(limit)
        )
    return [dict(r) for r in rows]


async def get_revenue_summary() -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        rub = await conn.fetchval("SELECT COALESCE(SUM(amount_rub),0) FROM payments")
        stars = await conn.fetchval("SELECT COALESCE(SUM(amount_stars),0) FROM payments")
    return {"sum_rub": int(rub or 0), "sum_stars": int(stars or 0)}


async def get_subscriptions_total() -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_premium=1 AND is_deleted=0")
    return int(v or 0)


async def get_subscriptions_page(offset: int, limit: int) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM users
            WHERE is_premium=1 AND is_deleted=0
            ORDER BY premium_until DESC NULLS LAST, id DESC
            OFFSET $1 LIMIT $2
            """,
            int(offset), int(limit)
        )
    return [dict(r) for r in rows]


# -------------------- photos / upload --------------------

async def create_today_photo(
    user_id: int,
    file_id: str,
    *,
    file_id_public: str | None = None,
    file_id_original: str | None = None,
    title: str | None = None,
    description: str | None = None,
    category: str | None = None,
    device_type: str | None = None,
    device_info: str | None = None,
    ratings_enabled: bool | None = None,
) -> int:
    """Создать новую фотографию пользователя на текущий день.

    Заполняем:
      - submit_day (date) для итогов/партий,
      - expires_at = конец следующего календарного дня (submit_day + 1),
      - status='active',
      - базовые счетчики (votes/avg/views) через DEFAULT.
    """
    p = _assert_pool()
    now_iso = get_bot_now_iso()
    today = get_bot_today()
    day_key = str(today)
    expires_at = end_of_day(today + timedelta(days=1))

    # Если описания нет, явно сохраняем метку "нет"
    if description is not None:
        description = description.strip() or None
    if not description:
        description = "нет"

    if not category:
        category = "photo"

    async with p.acquire() as conn:
        enabled = ratings_enabled
        if enabled is None:
            # По умолчанию оценки включены, глобальный флаг больше не используем
            enabled = True

        public_id = file_id_public or file_id
        orig_id = file_id_original or file_id

        row = await conn.fetchrow(
            """
            INSERT INTO photos (
                user_id,
                file_id,
                file_id_original,
                file_id_public,
                title,
                description,
                category,
            device_type,
            device_info,
            day_key,
            submit_day,
            expires_at,
                status,
                moderation_status,
                ratings_enabled,
                is_deleted,
                created_at
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'active','active',$13,0,$14)
            RETURNING id
            """,
            int(user_id),
            str(public_id),
            str(orig_id),
            str(public_id),
            title,
            description,
            category,
            device_type,
            device_info,
            day_key,
            today,
            expires_at,
            1 if enabled else 0,
            now_iso,
        )

    return int(row["id"]) if row else 0


async def get_today_photo_for_user(user_id: int) -> dict | None:
    items = await get_today_photos_for_user(user_id, limit=1)
    return items[0] if items else None


async def get_today_photos_for_user(user_id: int, limit: int = 50) -> list[dict]:
    p = _assert_pool()
    today = get_bot_today()
    day_key = _today_key()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
            FROM photos
            WHERE user_id=$1
              AND (
                    submit_day=$2::date
                 OR (submit_day IS NULL AND day_key=$3)
              )
              AND is_deleted=0
            ORDER BY created_at DESC, id DESC
            LIMIT $4
            """,
            int(user_id),
            today,
            day_key,
            int(limit),
        )
    return [dict(r) for r in rows]


async def check_can_upload_today(user_id: int, today: date | None = None) -> tuple[bool, str | None]:
    """
    Validate daily upload slot in bot timezone.

    Returns:
      (True, None) when user can publish now,
      (False, reason_text) when slot is already consumed for submit_day.
    """
    p = _assert_pool()
    target_day = today or get_bot_today()
    day_key = str(target_day)
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::int AS total,
                BOOL_OR(COALESCE(deleted_reason, '')='user') AS has_user_deleted
            FROM photos
            WHERE user_id=$1
              AND (
                    submit_day=$2::date
                 OR (submit_day IS NULL AND day_key=$3)
              )
            """,
            int(user_id),
            target_day,
            day_key,
        )
    total = int((row or {}).get("total") or 0)
    if total <= 0:
        return True, None

    if bool((row or {}).get("has_user_deleted")):
        return (
            False,
            "Ты удалил фото. Сегодня новая публикация недоступна — слот дня потрачен. Завтра можно.",
        )
    return False, "Сегодня ты уже публиковал фото. Новая публикация доступна завтра."


async def is_today_slot_locked(user_id: int) -> bool:
    """Backward-compatible slot check wrapper."""
    can_upload, _reason = await check_can_upload_today(int(user_id))
    return not can_upload


async def get_active_photos_for_user(user_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM photos
            WHERE user_id=$1
              AND is_deleted=0
              AND status='active'
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC, id DESC
            LIMIT $2 OFFSET $3
            """,
            int(user_id), int(limit), int(offset)
        )
    return [dict(r) for r in rows]


async def get_latest_photos_for_user(user_id: int, limit: int = 2) -> list[dict]:
    return await get_active_photos_for_user(user_id, limit=limit)


async def get_archived_photos_for_user(
    user_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
    min_votes: int = 0,
) -> list[dict]:
    """
    Archived photos for user with final rank metadata.

    Returns photo fields +:
      - final_rank
      - total_in_party
      - archived_at (from finalization timestamp if present)
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH archived AS (
                SELECT p.*
                FROM photos p
                WHERE p.user_id=$1
                  AND p.is_deleted=0
                  AND p.status='archived'
                  AND p.votes_count >= $4
                ORDER BY COALESCE(p.submit_day, DATE(p.created_at::timestamptz)) DESC, p.created_at DESC, p.id DESC
                LIMIT $2 OFFSET $3
            )
            SELECT
                a.*,
                rr.final_rank,
                totals.total_in_party,
                COALESCE(rr.finalized_at, a.expires_at) AS archived_at
            FROM archived a
            LEFT JOIN LATERAL (
                SELECT r.submit_day, r.final_rank, r.finalized_at
                FROM result_ranks r
                WHERE r.photo_id = a.id
                ORDER BY r.submit_day DESC
                LIMIT 1
            ) rr ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*)::int AS total_in_party
                FROM result_ranks r2
                WHERE rr.submit_day IS NOT NULL
                  AND r2.submit_day = rr.submit_day
            ) totals ON TRUE
            ORDER BY COALESCE(rr.submit_day, a.submit_day, DATE(a.created_at::timestamptz)) DESC,
                     a.created_at DESC,
                     a.id DESC
            """,
            int(user_id),
            int(limit),
            int(offset),
            int(min_votes),
        )
    return [dict(r) for r in rows]


async def get_archived_photos_count(user_id: int, *, min_votes: int = 0) -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM photos
            WHERE user_id=$1
              AND is_deleted=0
              AND status='archived'
              AND votes_count >= $2
            """,
            int(user_id),
            int(min_votes),
        )
    return int(value or 0)


async def get_archived_photo_details(photo_id: int, user_id: int) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                p.*,
                rr.final_rank,
                totals.total_in_party,
                COALESCE(rr.finalized_at, p.expires_at) AS archived_at
            FROM photos p
            LEFT JOIN LATERAL (
                SELECT r.submit_day, r.final_rank, r.finalized_at
                FROM result_ranks r
                WHERE r.photo_id = p.id
                ORDER BY r.submit_day DESC
                LIMIT 1
            ) rr ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*)::int AS total_in_party
                FROM result_ranks r2
                WHERE rr.submit_day IS NOT NULL
                  AND r2.submit_day = rr.submit_day
            ) totals ON TRUE
            WHERE p.id=$1
              AND p.user_id=$2
              AND p.is_deleted=0
              AND p.status='archived'
            LIMIT 1
            """,
            int(photo_id),
            int(user_id),
        )
    return dict(row) if row else None


async def set_public_portfolio(user_id: int, enabled: bool) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_user_stats_row(conn, int(user_id))
        await conn.execute(
            "UPDATE user_stats SET public_portfolio=$2 WHERE user_id=$1",
            int(user_id),
            bool(enabled),
        )


async def is_public_portfolio_enabled(user_id: int) -> bool:
    p = _assert_pool()
    async with p.acquire() as conn:
        val = await conn.fetchval(
            "SELECT public_portfolio FROM user_stats WHERE user_id=$1",
            int(user_id),
        )
    return bool(val)


async def get_public_portfolio_photos(user_id: int, *, limit: int = 9, min_votes: int = 7) -> list[dict]:
    """Top-N архивных фото для публичного портфолио, если включено."""
    p = _assert_pool()
    async with p.acquire() as conn:
        enabled = await conn.fetchval(
            "SELECT public_portfolio FROM user_stats WHERE user_id=$1",
            int(user_id),
        )
        if not enabled:
            return []
        rows = await conn.fetch(
            """
            SELECT *
            FROM photos
            WHERE user_id=$1
              AND is_deleted=0
              AND status='archived'
              AND votes_count >= $3
            ORDER BY avg_score DESC, votes_count DESC, created_at DESC
            LIMIT $2
            """,
            int(user_id),
            int(limit),
            int(min_votes),
        )
    return [dict(r) for r in rows]


async def get_photo_by_id(photo_id: int) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM photos WHERE id=$1", int(photo_id))
    return dict(row) if row else None


async def update_photo_editable_fields(
    photo_id: int,
    user_id: int,
    *,
    title: str | None = None,
    device_type: str | None = None,
    device_info: str | None = None,
    description: str | None = None,
    tag: str | None = None,
) -> bool:
    """
    Обновляет редактируемые поля фото ТОЛЬКО у владельца.
    Возвращает True, если строка обновилась.

    tag — опционально: если колонки нет, обновление тега тихо пропустится.
    """
    pool = _assert_pool()

    sets: list[str] = []
    args: list[object] = []

    def add(col: str, val: object):
        args.append(val)
        sets.append(f"{col}=${len(args)+2}")

    if title is not None:
        add("title", title)
    if device_type is not None:
        add("device_type", device_type)
    if device_info is not None:
        add("device_info", device_info)
    if description is not None:
        add("description", description)

    async with pool.acquire() as conn:
        updated = False

        if sets:
            q = f"UPDATE photos SET {', '.join(sets)} WHERE id=$1 AND user_id=$2"
            res = await conn.execute(q, int(photo_id), int(user_id), *args)
            updated = res.startswith("UPDATE ") and not res.endswith(" 0")

        if tag is not None:
            try:
                res2 = await conn.execute(
                    "UPDATE photos SET tag=$3 WHERE id=$1 AND user_id=$2",
                    int(photo_id),
                    int(user_id),
                    str(tag),
                )
                updated = updated or (res2.startswith("UPDATE ") and not res2.endswith(" 0"))
            except Exception:
                # если колонки tag нет — не ломаем редактирование
                pass

        return bool(updated)


async def set_photo_ratings_enabled(photo_id: int, enabled: bool, *, user_id: int | None = None) -> bool:
    """Явно включить/выключить оценки для фото. Если указан user_id — проверяем владельца."""
    p = _assert_pool()
    async with p.acquire() as conn:
        if user_id is None:
            res = await conn.execute(
                "UPDATE photos SET ratings_enabled=$2 WHERE id=$1",
                int(photo_id),
                1 if enabled else 0,
            )
        else:
            res = await conn.execute(
                "UPDATE photos SET ratings_enabled=$2 WHERE id=$1 AND user_id=$3",
                int(photo_id),
                1 if enabled else 0,
                int(user_id),
            )
    return res.startswith("UPDATE ") and not res.endswith(" 0")


async def toggle_photo_ratings_enabled(photo_id: int, user_id: int) -> bool | None:
    """Инвертировать флаг ratings_enabled. Возвращает новое значение или None, если фото не найдено."""
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE photos
            SET ratings_enabled = CASE WHEN ratings_enabled=0 THEN 1 ELSE 0 END
            WHERE id=$1 AND user_id=$2
            RETURNING ratings_enabled
            """,
            int(photo_id),
            int(user_id),
        )
    if not row:
        return None
    return bool(row["ratings_enabled"])


async def mark_photo_deleted(photo_id: int) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE photos SET is_deleted=1, status='deleted', deleted_reason=COALESCE(deleted_reason,'system'), deleted_at=NOW() WHERE id=$1",
            int(photo_id),
        )


async def mark_photo_deleted_by_user(photo_id: int, user_id: int) -> None:
    """Мягкое удаление пользователем — блокирует слот дня и вычищает из итогов."""
    p = _assert_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE photos
                SET is_deleted=1,
                    status='deleted',
                    deleted_reason='user',
                    deleted_at=NOW()
                WHERE id=$1 AND user_id=$2
                RETURNING submit_day
                """,
                int(photo_id),
                int(user_id),
            )
            if not row:
                return

            # Удаленная пользователем работа не должна участвовать в итогах/архиве.
            await conn.execute(
                "DELETE FROM result_ranks WHERE photo_id=$1",
                int(photo_id),
            )
            await conn.execute(
                """
                DELETE FROM notification_queue
                WHERE type='daily_results_top'
                  AND status='pending'
                  AND (payload->>'photo_id')::bigint = $1
                """,
                int(photo_id),
            )

            submit_day = row.get("submit_day")
            if submit_day is not None:
                # Сбрасываем кэш дня, чтобы при следующей публикации итогов снэпшот пересобрался без этой фото.
                await conn.execute(
                    "DELETE FROM daily_results_cache WHERE submit_day=$1::date",
                    submit_day,
                )


async def hard_delete_photo(photo_id: int) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM comments WHERE photo_id=$1", int(photo_id))
        await conn.execute("DELETE FROM ratings WHERE photo_id=$1", int(photo_id))
        await conn.execute("DELETE FROM super_ratings WHERE photo_id=$1", int(photo_id))
        await conn.execute("DELETE FROM photo_reports WHERE photo_id=$1", int(photo_id))
        await conn.execute("DELETE FROM weekly_candidates WHERE photo_id=$1", int(photo_id))
        await conn.execute("DELETE FROM photo_repeats WHERE photo_id=$1", int(photo_id))
        await conn.execute("DELETE FROM photos WHERE id=$1", int(photo_id))


async def get_comments_for_photo(photo_id: int, *, only_public: bool = False) -> list[dict]:
    p = _assert_pool()
    where_public = "AND c.is_public=1" if only_public else ""
    async with p.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT c.id, c.photo_id, c.user_id, c.text, c.is_public, c.created_at,
                   u.username, u.name
            FROM comments c
            LEFT JOIN users u ON u.id = c.user_id
            WHERE c.photo_id = $1 {where_public}
            ORDER BY c.created_at ASC
            """,
            int(photo_id),
        )
    return [dict(r) for r in rows]


async def get_photo_author_tg_id(photo_id: int) -> int | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            """
            SELECT u.tg_id
            FROM photos p
            JOIN users u ON u.id = p.user_id
            WHERE p.id = $1
            """,
            int(photo_id),
        )
    return int(v) if v is not None else None

# -------------------- rating flow --------------------

async def _abuse_vote_limit_exceeded(
    conn: asyncpg.Connection,
    voter_id: int,
    author_id: int,
    day: date | str,
) -> bool:
    """Return True if voter already превысил лимит голосов по автору за день, O(1) через daily_author_votes."""
    day_date: date
    if isinstance(day, date):
        day_date = day
    else:
        try:
            day_date = date.fromisoformat(str(day))
        except Exception:
            day_date = get_bot_today()
    try:
        from config import ANTI_ABUSE_MAX_VOTES_PER_AUTHOR_PER_DAY as LIMIT
    except Exception:
        LIMIT = 5
    if int(LIMIT) <= 0:
        return False
    row = await conn.fetchrow(
        "SELECT cnt FROM daily_author_votes WHERE day=$1 AND voter_id=$2 AND author_id=$3 FOR UPDATE",
        day_date,
        int(voter_id),
        int(author_id),
    )
    if row is None:
        await conn.execute(
            "INSERT INTO daily_author_votes (day, voter_id, author_id, cnt) VALUES ($1,$2,$3,1)",
            day_date,
            int(voter_id),
            int(author_id),
        )
        return False
    cnt = int(row["cnt"] or 0)
    if cnt >= int(LIMIT):
        return True
    await conn.execute(
        "UPDATE daily_author_votes SET cnt = cnt + 1 WHERE day=$1 AND voter_id=$2 AND author_id=$3",
        day_date,
        int(voter_id),
        int(author_id),
    )
    return False


async def next_photo_for_viewer(viewer_user_id: int) -> dict | None:
    """
    Smart выдача:
      - не показываем свои фото и уже оценённые/просмотренные;
      - фото активно (status=active, не истёк expires_at);
      - предпочитаем авторов с кредитами/токенами; иначе «хвост» редких показов.
      - защита от гонок: FOR UPDATE SKIP LOCKED + списание токена в той же транзакции.
    """
    p = _assert_pool()
    now = get_bot_now()
    economy = await get_effective_economy_settings()
    async def _pick(require_credit: bool, *, spend_token: bool, max_votes: int | None = None) -> dict | None:
        async with p.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    WITH cand AS (
                        SELECT p.id
                        FROM photos p
                        LEFT JOIN user_stats us ON us.user_id = p.user_id
                        WHERE p.is_deleted = 0
                          AND COALESCE(p.status,'active') = 'active'
                          AND p.moderation_status IN ('active','good')
                          AND COALESCE(p.ratings_enabled,1)=1
                          AND (p.expires_at IS NULL OR p.expires_at > NOW())
                          AND p.user_id <> $1
                          AND NOT EXISTS (SELECT 1 FROM ratings r WHERE r.photo_id=p.id AND r.user_id=$1)
                          AND NOT EXISTS (SELECT 1 FROM votes v WHERE v.photo_id=p.id AND v.voter_id=$1)
                          AND NOT EXISTS (SELECT 1 FROM photo_views pv WHERE pv.photo_id=p.id AND pv.viewer_id=$1)
                          AND ($2::bool = FALSE OR COALESCE(us.credits,0)+COALESCE(us.show_tokens,0) > 0)
                          AND ($3::int IS NULL OR COALESCE(p.votes_count,0) <= $3)
                        ORDER BY COALESCE(p.votes_count,0) ASC, p.created_at DESC
                        LIMIT 50
                    )
                    SELECT p.* FROM photos p
                    JOIN cand c ON c.id = p.id
                    ORDER BY COALESCE(p.votes_count,0) ASC, p.created_at DESC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    int(viewer_user_id),
                    bool(require_credit),
                    None if max_votes is None else int(max_votes),
                )
                if not row:
                    return None
                photo = dict(row)
                author_id = int(photo["user_id"])

                await _ensure_user_stats_row(conn, author_id)
                stats_row = await conn.fetchrow(
                    "SELECT credits, show_tokens FROM user_stats WHERE user_id=$1 FOR UPDATE",
                    int(author_id),
                )
                credits = int((stats_row or {}).get("credits") or 0)
                tokens = int((stats_row or {}).get("show_tokens") or 0)
                if require_credit and credits <= 0 and tokens <= 0:
                    return None

                multiplier = _credit_multiplier_for_moment(now, economy)
                new_credits = credits
                new_tokens = tokens
                if spend_token and new_tokens <= 0 and new_credits > 0:
                    new_credits -= 1
                    new_tokens += multiplier

                res = await conn.execute(
                    """
                    INSERT INTO photo_views (photo_id, viewer_id, created_at)
                    VALUES ($1,$2,$3)
                    ON CONFLICT DO NOTHING
                    """,
                    int(photo["id"]),
                    int(viewer_user_id),
                    now,
                )
                if not res.endswith(" 1"):
                    return None

                if spend_token:
                    new_tokens = max(0, new_tokens - 1)
                    await conn.execute(
                        "UPDATE user_stats SET credits=$2, show_tokens=$3, last_active_at=$4 WHERE user_id=$1",
                        int(author_id),
                        new_credits,
                        new_tokens,
                        now,
                    )
                await conn.execute(
                    "UPDATE photos SET views_count = views_count + 1 WHERE id=$1",
                    int(photo["id"]),
                )
                return photo

    # Списание показа происходит при действии пользователя (оценка/дальше),
    # а не в момент выдачи карточки.
    photo = await _pick(True, spend_token=False)
    if photo:
        return photo
    # tail без списания кредитов с вероятностью
    tail_probability = _coerce_float(economy.get("tail_probability"), 0.05, min_value=0.0, max_value=1.0)
    min_votes = _coerce_int(economy.get("min_votes_for_normal_feed"), 5, min_value=0, max_value=500)
    if random.random() <= float(tail_probability):
        return await _pick(False, spend_token=False, max_votes=int(min_votes))
    return await _pick(False, spend_token=False)


async def get_random_photo_for_rating_rateable(
    viewer_user_id: int,
    *,
    exclude_author_id: int | None = None,
    require_premium: bool | None = None,
) -> dict | None:
    p = _assert_pool()
    premium_flag = 1 if require_premium else None
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.*,
                   u.is_premium AS user_is_premium,
                   u.premium_until AS user_premium_until,
                   u.tg_channel_link AS user_tg_channel_link,
                   u.tg_channel_link AS tg_channel_link
            FROM photos p
            JOIN users u ON u.id=p.user_id
            WHERE p.is_deleted=0
              AND p.moderation_status IN ('active','good')
              AND COALESCE(p.status,'active')='active'
              AND COALESCE(p.ratings_enabled, 1)=1
              AND p.user_id <> $1
              AND ($2::int IS NULL OR p.user_id <> $2)
              AND ($3::int IS NULL OR u.is_premium=$3)
              AND NOT EXISTS (SELECT 1 FROM ratings r WHERE r.photo_id=p.id AND r.user_id=$1)
            ORDER BY random()
            LIMIT 1
            """,
            int(viewer_user_id),
            int(exclude_author_id) if exclude_author_id is not None else None,
            int(premium_flag) if premium_flag is not None else None,
        )
    return dict(row) if row else None


async def get_popular_photo_for_rating(
    viewer_user_id: int,
    *,
    exclude_author_id: int | None = None,
    require_premium: bool | None = None,
) -> dict | None:
    p = _assert_pool()
    w = _link_rating_weight()
    premium_flag = 1 if require_premium else None
    async with p.acquire() as conn:
        global_mean, _ = await _get_global_rating_mean(conn)
        prior = _bayes_prior_weight()
        row = await conn.fetchrow(
            """
            WITH stats AS (
                SELECT
                    p.*,
                    u.id AS u_id,
                    u.is_premium AS user_is_premium,
                    u.premium_until AS user_premium_until,
                    u.tg_channel_link AS user_tg_channel_link,
                    u.tg_channel_link AS tg_channel_link,
                    COUNT(r.id)::int AS ratings_count,
                    COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $5 ELSE 1 END), 0)::float AS sum_values,
                    COALESCE(SUM(CASE WHEN r.source='link' THEN $5 ELSE 1 END), 0)::float AS sum_weights
                FROM photos p
                JOIN users u ON u.id=p.user_id
                LEFT JOIN ratings r ON r.photo_id=p.id
                WHERE p.is_deleted=0
                  AND p.moderation_status IN ('active','good')
                  AND COALESCE(p.status,'active')='active'
                  AND COALESCE(p.ratings_enabled, 1)=1
                  AND p.user_id <> $1
                  AND ($6::int IS NULL OR p.user_id <> $6)
                  AND ($7::int IS NULL OR u.is_premium=$7)
                  AND NOT EXISTS (SELECT 1 FROM ratings r2 WHERE r2.photo_id=p.id AND r2.user_id=$1)
                GROUP BY p.id, u.id, u.is_premium, u.premium_until, u.tg_channel_link
            )
            SELECT *,
                   ((($3::float) * ($4::float)) + sum_values) / (($3::float) + sum_weights) AS bayes_score
            FROM stats
            WHERE ratings_count >= $2
            ORDER BY ratings_count DESC, bayes_score DESC, random()
            LIMIT 1
            """,
            int(viewer_user_id),
            int(RATE_POPULAR_MIN_RATINGS),
            float(prior),
            float(global_mean),
            float(w),
            int(exclude_author_id) if exclude_author_id is not None else None,
            int(premium_flag) if premium_flag is not None else None,
        )
    return dict(row) if row else None


async def _get_rateable_photo_by_ratings_count(
    viewer_user_id: int,
    *,
    min_ratings: int | None = None,
    max_ratings: int | None = None,
    exclude_author_id: int | None = None,
    require_premium: bool | None = None,
    order_sql: str = "random()",
) -> dict | None:
    p = _assert_pool()
    premium_flag = 1 if require_premium else None
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            WITH stats AS (
                SELECT
                    p.*,
                    u.id AS u_id,
                    u.is_premium AS user_is_premium,
                    u.premium_until AS user_premium_until,
                    u.tg_channel_link AS user_tg_channel_link,
                    u.tg_channel_link AS tg_channel_link,
                    COUNT(r.id)::int AS ratings_count
                FROM photos p
                JOIN users u ON u.id=p.user_id
                LEFT JOIN ratings r ON r.photo_id=p.id
                WHERE p.is_deleted=0
                  AND p.moderation_status IN ('active','good')
                  AND COALESCE(p.status,'active')='active'
                  AND COALESCE(p.ratings_enabled, 1)=1
                  AND p.user_id <> $1
                  AND ($4::int IS NULL OR p.user_id <> $4)
                  AND ($5::int IS NULL OR u.is_premium=$5)
                  AND NOT EXISTS (SELECT 1 FROM ratings r2 WHERE r2.photo_id=p.id AND r2.user_id=$1)
                GROUP BY p.id, u.id, u.is_premium, u.premium_until, u.tg_channel_link
            )
            SELECT *
            FROM stats
            WHERE ($2::int IS NULL OR ratings_count >= $2)
              AND ($3::int IS NULL OR ratings_count <= $3)
            ORDER BY {order_sql}
            LIMIT 1
            """,
            int(viewer_user_id),
            None if min_ratings is None else int(min_ratings),
            None if max_ratings is None else int(max_ratings),
            int(exclude_author_id) if exclude_author_id is not None else None,
            int(premium_flag) if premium_flag is not None else None,
        )
    return dict(row) if row else None


async def get_fresh_photo_for_rating(
    viewer_user_id: int,
    *,
    exclude_author_id: int | None = None,
    require_premium: bool | None = None,
) -> dict | None:
    # 0 оценок: предпочитаем свежие, но оставляем случайность
    return await _get_rateable_photo_by_ratings_count(
        viewer_user_id,
        min_ratings=0,
        max_ratings=0,
        exclude_author_id=exclude_author_id,
        require_premium=require_premium,
        order_sql="created_at DESC, id DESC, random()",
    )


async def get_low_photo_for_rating(
    viewer_user_id: int,
    *,
    exclude_author_id: int | None = None,
    require_premium: bool | None = None,
) -> dict | None:
    # 1..RATE_LOW_RATINGS_MAX
    if int(RATE_LOW_RATINGS_MAX) < 1:
        return None
    return await _get_rateable_photo_by_ratings_count(
        viewer_user_id,
        min_ratings=1,
        max_ratings=int(RATE_LOW_RATINGS_MAX),
        exclude_author_id=exclude_author_id,
        require_premium=require_premium,
        order_sql="ratings_count ASC, created_at DESC, id DESC, random()",
    )


async def get_mid_photo_for_rating(
    viewer_user_id: int,
    *,
    exclude_author_id: int | None = None,
    require_premium: bool | None = None,
) -> dict | None:
    # (RATE_LOW_RATINGS_MAX+1) .. (RATE_POPULAR_MIN_RATINGS-1)
    min_mid = int(RATE_LOW_RATINGS_MAX) + 1
    max_mid = int(RATE_POPULAR_MIN_RATINGS) - 1
    if max_mid < min_mid:
        return None
    return await _get_rateable_photo_by_ratings_count(
        viewer_user_id,
        min_ratings=min_mid,
        max_ratings=max_mid,
        exclude_author_id=exclude_author_id,
        require_premium=require_premium,
        order_sql="random()",
    )


async def get_nonpopular_photo_for_rating(
    viewer_user_id: int,
    *,
    exclude_author_id: int | None = None,
    require_premium: bool | None = None,
) -> dict | None:
    return await _get_rateable_photo_by_ratings_count(
        viewer_user_id,
        min_ratings=None,
        max_ratings=int(RATE_POPULAR_MIN_RATINGS) - 1,
        exclude_author_id=exclude_author_id,
        require_premium=require_premium,
        order_sql="random()",
    )


async def get_random_photo_for_rating_viewonly(
    viewer_user_id: int,
    *,
    exclude_author_id: int | None = None,
) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.*,
                   u.is_premium AS user_is_premium,
                   u.premium_until AS user_premium_until,
                   u.tg_channel_link AS user_tg_channel_link,
                   u.tg_channel_link AS tg_channel_link
            FROM photos p
            JOIN users u ON u.id=p.user_id
            WHERE p.is_deleted=0
              AND p.moderation_status IN ('active','good')
              AND COALESCE(p.status,'active')='active'
              AND COALESCE(p.ratings_enabled, 1)=0
              AND p.user_id <> $1
              AND ($2::int IS NULL OR p.user_id <> $2)
              AND NOT EXISTS (SELECT 1 FROM ratings r WHERE r.photo_id=p.id AND r.user_id=$1)
              AND NOT EXISTS (
                  SELECT 1 FROM viewonly_views v
                  WHERE v.photo_id=p.id AND v.user_id=$1
              )
            ORDER BY random()
            LIMIT 1
            """,
            int(viewer_user_id),
            int(exclude_author_id) if exclude_author_id is not None else None,
        )
    return dict(row) if row else None


async def get_random_photo_for_rating(viewer_user_id: int) -> dict | None:
    """
    Возвращает фото по умной схеме:
    - каждое 10-е: «передышка» (ratings_enabled=0), если доступна;
    - иначе чередование: свежее / низкое / среднее / популярное.
    - если подходящих нет в целевой группе, ищем в соседних, затем в любых.
    """
    # New smart feed (credits / no repeats). Best-effort; fallback на старую схему при ошибках.
    try:
        smart = await next_photo_for_viewer(int(viewer_user_id))
        if smart:
            return smart
    except Exception:
        pass

    state = await get_rating_feed_state(int(viewer_user_id))
    seq = int(state.get("seq") or 0)
    last_author_id = state.get("last_author_id")
    next_seq = seq + 1

    want_viewonly = (next_seq % 10) == 0
    if want_viewonly:
        vo = await get_random_photo_for_rating_viewonly(
            viewer_user_id,
            exclude_author_id=last_author_id if last_author_id is not None else None,
        )
        if not vo and last_author_id is not None:
            vo = await get_random_photo_for_rating_viewonly(viewer_user_id)
        if vo:
            author_id = int(vo.get("user_id")) if vo and vo.get("user_id") is not None else None
            await set_rating_feed_state(int(viewer_user_id), int(next_seq), last_author_id=author_id)
            return vo

    bucket = next_seq % 6
    if bucket == 1:
        order = ("fresh", "low", "mid", "popular")
    elif bucket == 2:
        order = ("low", "fresh", "mid", "popular")
    elif bucket == 3:
        order = ("mid", "low", "fresh", "popular")
    elif bucket == 4:
        order = ("popular", "mid", "low", "fresh")
    elif bucket == 5:
        order = ("mid", "popular", "low", "fresh")
    else:
        order = ("popular", "mid", "low", "fresh")

    getters = {
        "fresh": get_fresh_photo_for_rating,
        "low": get_low_photo_for_rating,
        "mid": get_mid_photo_for_rating,
        "popular": get_popular_photo_for_rating,
    }

    prefer_premium = random.random() < _premium_boost_chance()

    async def _try_get(getter):
        photo = None
        if last_author_id is not None:
            if prefer_premium:
                photo = await getter(viewer_user_id, exclude_author_id=last_author_id, require_premium=True)
                if not photo:
                    photo = await getter(viewer_user_id, exclude_author_id=last_author_id, require_premium=None)
            else:
                photo = await getter(viewer_user_id, exclude_author_id=last_author_id, require_premium=None)
            if not photo:
                if prefer_premium:
                    photo = await getter(viewer_user_id, exclude_author_id=None, require_premium=True)
                    if not photo:
                        photo = await getter(viewer_user_id, exclude_author_id=None, require_premium=None)
                else:
                    photo = await getter(viewer_user_id, exclude_author_id=None, require_premium=None)
        else:
            if prefer_premium:
                photo = await getter(viewer_user_id, exclude_author_id=None, require_premium=True)
                if not photo:
                    photo = await getter(viewer_user_id, exclude_author_id=None, require_premium=None)
            else:
                photo = await getter(viewer_user_id, exclude_author_id=None, require_premium=None)
        return photo

    photo = None
    for key in order:
        try:
            photo = await _try_get(getters[key])
        except Exception:
            photo = None
        if photo:
            break

    if photo:
        author_id = int(photo.get("user_id")) if photo and photo.get("user_id") is not None else None
        await set_rating_feed_state(int(viewer_user_id), int(next_seq), last_author_id=author_id)
        return photo

    # Fallback: если целевые группы пусты — берём любую непопулярную
    nonpopular = await _try_get(get_nonpopular_photo_for_rating)
    if nonpopular:
        author_id = int(nonpopular.get("user_id")) if nonpopular and nonpopular.get("user_id") is not None else None
        await set_rating_feed_state(int(viewer_user_id), int(next_seq), last_author_id=author_id)
        return nonpopular

    # Fallback: любые доступные для оценивания
    any_rateable = None
    if last_author_id is not None:
        if prefer_premium:
            any_rateable = await get_random_photo_for_rating_rateable(
                viewer_user_id,
                exclude_author_id=last_author_id,
                require_premium=True,
            )
            if not any_rateable:
                any_rateable = await get_random_photo_for_rating_rateable(
                    viewer_user_id,
                    exclude_author_id=last_author_id,
                    require_premium=None,
                )
        else:
            any_rateable = await get_random_photo_for_rating_rateable(
                viewer_user_id,
                exclude_author_id=last_author_id,
                require_premium=None,
            )
    if not any_rateable:
        if prefer_premium:
            any_rateable = await get_random_photo_for_rating_rateable(
                viewer_user_id,
                exclude_author_id=None,
                require_premium=True,
            )
            if not any_rateable:
                any_rateable = await get_random_photo_for_rating_rateable(
                    viewer_user_id,
                    exclude_author_id=None,
                    require_premium=None,
                )
        else:
            any_rateable = await get_random_photo_for_rating_rateable(
                viewer_user_id,
                exclude_author_id=None,
                require_premium=None,
            )
    if any_rateable:
        author_id = int(any_rateable.get("user_id")) if any_rateable and any_rateable.get("user_id") is not None else None
        await set_rating_feed_state(int(viewer_user_id), int(next_seq), last_author_id=author_id)
        return any_rateable

    # Fallback: если подходящих нет — попробуем передышку
    vo = await get_random_photo_for_rating_viewonly(
        viewer_user_id,
        exclude_author_id=last_author_id if last_author_id is not None else None,
    )
    if not vo and last_author_id is not None:
        vo = await get_random_photo_for_rating_viewonly(viewer_user_id)
    if vo:
        author_id = int(vo.get("user_id")) if vo and vo.get("user_id") is not None else None
        await set_rating_feed_state(int(viewer_user_id), int(next_seq), last_author_id=author_id)
        return vo
    return None


async def mark_viewonly_seen(user_id: int, photo_id: int) -> None:
    """Пометить фото-передышку как просмотренное, чтобы не показывать повторно."""
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO viewonly_views (user_id, photo_id, created_at)
            VALUES ($1,$2,$3)
            ON CONFLICT (user_id, photo_id) DO NOTHING
            """,
            int(user_id),
            int(photo_id),
            now,
        )


async def record_photo_view(photo_id: int, viewer_id: int) -> None:
    """Запомнить показ фото конкретному пользователю и инкрементировать счётчик просмотров."""
    p = _assert_pool()
    now = get_bot_now_iso()
    async with p.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO photo_views (photo_id, viewer_id, created_at)
                VALUES ($1,$2,$3)
                ON CONFLICT (photo_id, viewer_id) DO NOTHING
                """,
                int(photo_id),
                int(viewer_id),
                now,
            )
            await conn.execute(
                "UPDATE photos SET views_count = views_count + 1 WHERE id=$1",
                int(photo_id),
            )


async def has_viewonly_seen(user_id: int, photo_id: int) -> bool:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "SELECT 1 FROM viewonly_views WHERE user_id=$1 AND photo_id=$2",
            int(user_id),
            int(photo_id),
        )
    return bool(v)


# -------------------- parties / recap / notifications --------------------


async def enqueue_notification(user_id: int, n_type: str, payload: dict, *, run_after: datetime | None = None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO notification_queue (user_id, type, payload, run_after, status)
            VALUES ($1,$2,$3,$4,'pending')
            """,
            int(user_id),
            str(n_type),
            json.dumps(payload or {}),
            run_after or get_bot_now(),
        )


async def fetch_pending_notifications(limit: int = 50) -> list[dict]:
    """Берём pending задачи пачкой, помечая их sending (SKIP LOCKED)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH cte AS (
                SELECT *
                FROM notification_queue
                WHERE status='pending' AND run_after <= NOW()
                ORDER BY run_after ASC, id ASC
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE notification_queue n
            SET status='sending',
                attempts = attempts + 1,
                updated_at = NOW()
            FROM cte
            WHERE n.id = cte.id
            RETURNING n.*;
            """,
            int(limit),
        )
    return [dict(r) for r in rows]


async def mark_notification_done(
    notification_id: int,
    status: str = "sent",
    *,
    error: str | None = None,
    backoff_seconds: int | None = None,
) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        if status == "failed" and backoff_seconds:
            await conn.execute(
                """
                UPDATE notification_queue
                SET status='pending',
                    run_after = NOW() + $3 * INTERVAL '1 second',
                    last_error = $2,
                    updated_at = NOW()
                WHERE id=$1
                """,
                int(notification_id),
                error,
                int(backoff_seconds),
            )
        else:
            await conn.execute(
                """
                UPDATE notification_queue
                SET status=$2,
                    last_error=$3,
                    updated_at=NOW()
                WHERE id=$1
                """,
                int(notification_id),
                status,
                error,
            )


def _daily_results_top_threshold(participants_count: int) -> int:
    pc = max(int(participants_count), 0)
    if pc < 30:
        return 3
    if pc < 100:
        return 5
    return 10


async def build_daily_results_snapshot(submit_day: date, *, limit: int = 10) -> dict:
    """
    Build immutable daily results payload for archived party (submit_day).
    Uses result_ranks + precomputed photo counters (no heavy real-time aggregates).
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        participants_count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM result_ranks rr
            JOIN photos p ON p.id = rr.photo_id
            WHERE rr.submit_day=$1::date
              AND COALESCE(p.is_deleted,0)=0
            """,
            submit_day,
        )
        rows = await conn.fetch(
            """
            SELECT
                rr.final_rank,
                p.id AS photo_id,
                p.user_id,
                COALESCE(p.title, 'Без названия') AS title,
                COALESCE(p.file_id_public, p.file_id) AS file_id,
                COALESCE(p.avg_score, 0)::float AS avg_score,
                COALESCE(p.votes_count, 0)::int AS votes_count,
                COALESCE(NULLIF(u.name,''), '') AS author_name,
                COALESCE(NULLIF(u.username,''), '') AS author_username
            FROM result_ranks rr
            JOIN photos p ON p.id = rr.photo_id
            LEFT JOIN users u ON u.id = p.user_id
            WHERE rr.submit_day=$1::date
              AND COALESCE(p.is_deleted,0)=0
            ORDER BY rr.final_rank ASC
            LIMIT $2
            """,
            submit_day,
            int(limit),
        )

    participants = int(participants_count or 0)
    threshold = _daily_results_top_threshold(participants)
    top: list[dict] = []
    for r in rows:
        top.append(
            {
                "final_rank": int(r.get("final_rank") or 0),
                "photo_id": int(r.get("photo_id") or 0),
                "user_id": int(r.get("user_id") or 0),
                "title": str(r.get("title") or "Без названия"),
                "file_id": str(r.get("file_id") or ""),
                "avg_score": float(r.get("avg_score") or 0),
                "votes_count": int(r.get("votes_count") or 0),
                "author_name": str(r.get("author_name") or ""),
                "author_username": str(r.get("author_username") or ""),
            }
        )
    return {
        "submit_day": str(submit_day),
        "participants_count": participants,
        "top_threshold": threshold,
        "top": top,
    }


async def publish_daily_results(submit_day: date, *, limit: int = 10) -> dict:
    """
    Publish cached daily results snapshot.
    User DM notifications for daily results are disabled.
    """
    snapshot = await build_daily_results_snapshot(submit_day, limit=limit)
    p = _assert_pool()
    now = get_bot_now()
    payload_json = json.dumps(snapshot, ensure_ascii=False)
    enqueued_count = 0

    async with p.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT notifications_enqueued_at FROM daily_results_cache WHERE submit_day=$1 FOR UPDATE",
                submit_day,
            )
            already_enqueued = bool(row and row.get("notifications_enqueued_at") is not None)

            await conn.execute(
                """
                INSERT INTO daily_results_cache (
                    submit_day,
                    participants_count,
                    top_threshold,
                    payload,
                    published_at,
                    updated_at
                )
                VALUES ($1,$2,$3,$4::jsonb,$5,$5)
                ON CONFLICT (submit_day) DO UPDATE
                SET participants_count = EXCLUDED.participants_count,
                    top_threshold = EXCLUDED.top_threshold,
                    payload = EXCLUDED.payload,
                    published_at = EXCLUDED.published_at,
                    updated_at = EXCLUDED.updated_at
                """,
                submit_day,
                int(snapshot.get("participants_count") or 0),
                int(snapshot.get("top_threshold") or 0),
                payload_json,
                now,
            )

            if not already_enqueued:
                await conn.execute(
                    """
                    UPDATE daily_results_cache
                    SET notifications_enqueued_at=$2,
                        updated_at=$2
                    WHERE submit_day=$1
                    """,
                    submit_day,
                    now,
                )

    snapshot["notifications_enqueued"] = int(enqueued_count)
    return snapshot


async def get_daily_results_cache(submit_day: date | str) -> dict | None:
    p = _assert_pool()
    day = submit_day if isinstance(submit_day, date) else date.fromisoformat(str(submit_day))
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT submit_day, participants_count, top_threshold, payload, published_at
            FROM daily_results_cache
            WHERE submit_day=$1::date
            """,
            day,
        )
    if not row:
        return None
    payload = row.get("payload") or {}
    return {
        "submit_day": str(row.get("submit_day")),
        "participants_count": int(row.get("participants_count") or 0),
        "top_threshold": int(row.get("top_threshold") or 0),
        "published_at": row.get("published_at"),
        "payload": payload if isinstance(payload, dict) else {},
    }


async def get_latest_daily_results_cache() -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT submit_day, participants_count, top_threshold, payload, published_at
            FROM daily_results_cache
            ORDER BY submit_day DESC
            LIMIT 1
            """
        )
    if not row:
        return None
    payload = row.get("payload") or {}
    return {
        "submit_day": str(row.get("submit_day")),
        "participants_count": int(row.get("participants_count") or 0),
        "top_threshold": int(row.get("top_threshold") or 0),
        "published_at": row.get("published_at"),
        "payload": payload if isinstance(payload, dict) else {},
    }


async def list_daily_results_days(limit: int = 10, offset: int = 0) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT submit_day, participants_count, top_threshold, published_at
            FROM daily_results_cache
            ORDER BY submit_day DESC
            OFFSET $1 LIMIT $2
            """,
            int(offset),
            int(limit),
        )
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "submit_day": str(r.get("submit_day")),
                "participants_count": int(r.get("participants_count") or 0),
                "top_threshold": int(r.get("top_threshold") or 0),
                "published_at": r.get("published_at"),
            }
        )
    return out


async def grant_daily_credits_once(day: date | None = None, *, batch_size: int = 1000) -> dict:
    """
    Grant daily credits one time per day (idempotent by user_stats.last_daily_grant_day).
      - regular users: +2 credits
      - premium users: +3 credits
    """
    target_day = day or get_bot_today()
    economy = await get_effective_economy_settings()
    normal_credits = _coerce_int(economy.get("daily_free_credits_normal"), 2, min_value=0, max_value=100)
    premium_credits = _coerce_int(economy.get("daily_free_credits_premium"), 3, min_value=0, max_value=100)
    author_credits = _coerce_int(economy.get("daily_free_credits_author"), premium_credits, min_value=0, max_value=100)
    p = _assert_pool()
    granted_total = 0
    batches = 0

    # Ensure user_stats rows exist for all non-deleted users (cheap idempotent upsert).
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_stats (user_id, last_active_at)
            SELECT u.id, NOW()
            FROM users u
            LEFT JOIN user_stats us ON us.user_id = u.id
            WHERE COALESCE(u.is_deleted,0)=0
              AND us.user_id IS NULL
            ON CONFLICT (user_id) DO NOTHING
            """
        )

    while True:
        async with p.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    WITH picked AS (
                        SELECT us.user_id
                        FROM user_stats us
                        JOIN users u ON u.id = us.user_id
                        WHERE COALESCE(u.is_deleted,0)=0
                          AND COALESCE(us.last_daily_grant_day, DATE '1970-01-01') < $1::date
                        ORDER BY us.user_id
                        LIMIT $2
                        FOR UPDATE OF us SKIP LOCKED
                    )
                    UPDATE user_stats us
                    SET credits = us.credits + CASE
                        WHEN COALESCE(u.is_author,0)=1 THEN $3
                        WHEN COALESCE(u.is_premium,0)=1 THEN $4
                        ELSE $5
                    END,
                        last_daily_grant_day = $1::date,
                        last_active_at = COALESCE(us.last_active_at, NOW())
                    FROM picked p
                    JOIN users u ON u.id = p.user_id
                    WHERE us.user_id = p.user_id
                    RETURNING us.user_id
                    """,
                    target_day,
                    int(batch_size),
                    int(author_credits),
                    int(premium_credits),
                    int(normal_credits),
                )
        count = len(rows)
        if count <= 0:
            break
        granted_total += count
        batches += 1
        if count < int(batch_size):
            break

    return {
        "day": str(target_day),
        "granted_users": int(granted_total),
        "batches": int(batches),
    }


async def finalize_party(submit_day: date | None = None, *, min_votes: int = 7, limit: int = 500) -> list[dict]:
    """
    Закрываем партии:
      - если submit_day задан, закрываем её, но только если истёкла;
      - иначе закрываем все истёкшие (expires_at <= now) активные фото пачкой.
    """
    p = _assert_pool()
    results: list[dict] = []
    now = get_bot_now()
    rank_by_day: dict[str, int] = {}
    async with p.acquire() as conn:
        while True:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM photos
                    WHERE is_deleted=0
                      AND COALESCE(status,'active')='active'
                      AND (expires_at <= NOW())
                      AND ($1::date IS NULL OR submit_day=$1::date)
                    ORDER BY submit_day NULLS LAST, avg_score DESC, votes_count DESC, created_at ASC
                    LIMIT $2
                    """,
                    submit_day,
                    int(limit),
                )
                if not rows:
                    break
                batch_results: list[dict] = []
                for r in rows:
                    sd = r.get("submit_day")
                    if sd is None:
                        try:
                            created = datetime.fromisoformat(str(r.get("created_at")))
                            sd = created.date()
                        except Exception:
                            sd = now.date()
                    day_key = str(sd)
                    rank_by_day.setdefault(day_key, 0)
                    rank_by_day[day_key] += 1
                    rank = rank_by_day[day_key]

                    photo_id = int(r["id"])
                    await conn.execute(
                        """
                        INSERT INTO result_ranks (photo_id, submit_day, final_rank, finalized_at)
                        VALUES ($1,$2,$3,NOW())
                        ON CONFLICT (photo_id, submit_day)
                        DO UPDATE SET final_rank=EXCLUDED.final_rank, finalized_at=EXCLUDED.finalized_at
                        """,
                        photo_id,
                        sd,
                        rank,
                    )
                    await conn.execute(
                        "UPDATE photos SET status='archived' WHERE id=$1",
                        photo_id,
                    )
                    batch_results.append(dict(r) | {"final_rank": rank, "submit_day": sd})

                results.extend(batch_results)
            if len(rows) < limit:
                break
    return results


async def daily_recap(submit_day: date, *, min_votes: int = 7, top_n: int = 3) -> dict:
    """Формирует суточную сводку (без постановки пользовательских уведомлений)."""
    p = _assert_pool()
    top_list: list[dict] = []
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM photos
            WHERE submit_day=$1::date
              AND is_deleted=0
              AND COALESCE(status,'active')='active'
              AND (expires_at IS NULL OR expires_at > NOW())
              AND votes_count >= $2
            ORDER BY avg_score DESC, votes_count DESC, created_at ASC
            LIMIT $3
            """,
            submit_day,
            int(min_votes),
            int(top_n),
        )
        top_list = [dict(r) for r in rows]

    return {"top": top_list}


async def add_rating(user_id: int, photo_id: int, value: int) -> bool:
    p = _assert_pool()
    now_dt = get_bot_now()
    now_iso = get_bot_now_iso()
    today = get_bot_today()
    async with p.acquire() as conn:
        async with conn.transaction():
            photo_row = await conn.fetchrow(
                "SELECT id, user_id, ratings_enabled, is_deleted, moderation_status, votes_count, sum_score FROM photos WHERE id=$1 FOR UPDATE",
                int(photo_id),
            )
            if not photo_row:
                return False
            status = str(photo_row.get("moderation_status") or "").lower()
            if (
                int(photo_row.get("is_deleted") or 0) != 0
                or status not in ("active", "good")
                or not bool(photo_row.get("ratings_enabled", 1))
            ):
                return False

            author_id = int(photo_row["user_id"])
            if await _abuse_vote_limit_exceeded(conn, int(user_id), author_id, today):
                return False

            prev_score = await conn.fetchval(
                "SELECT score FROM votes WHERE photo_id=$1 AND voter_id=$2",
                int(photo_id),
                int(user_id),
            )
            if prev_score is not None:
                return False  # уникальность голоса

            await conn.execute(
                """
                INSERT INTO ratings (photo_id, user_id, value, created_at)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (photo_id, user_id)
                DO UPDATE SET value=EXCLUDED.value, created_at=EXCLUDED.created_at
                """,
                int(photo_id), int(user_id), int(value), now_iso
            )

            await conn.execute(
                """
                INSERT INTO votes (photo_id, voter_id, score, created_at)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (photo_id, voter_id)
                DO UPDATE SET score=EXCLUDED.score, created_at=EXCLUDED.created_at
                """,
                int(photo_id), int(user_id), int(value), now_dt
            )

            votes_count = int(photo_row.get("votes_count") or 0)
            sum_score = int(photo_row.get("sum_score") or 0)

            votes_count += 1
            sum_score += int(value)

            avg_score = float(sum_score) / votes_count if votes_count > 0 else 0.0

            await conn.execute(
                """
                UPDATE photos
                SET votes_count=$2, sum_score=$3, avg_score=$4
                WHERE id=$1
                """,
                int(photo_id),
                votes_count,
                sum_score,
                avg_score,
            )

            # Invalidate author's rank cache (their photo got a new rating)
            try:
                await conn.execute(
                    "UPDATE users SET rank_updated_at=NULL, updated_at=$2 WHERE id=$1",
                    int(author_id),
                    now_iso,
                )
            except Exception:
                pass

            # credits to voter
            if int(value) > 0:
                try:
                    await add_credits_on_vote(int(user_id), now=now_dt)
                except Exception:
                    pass
    return True


async def set_super_rating(user_id: int, photo_id: int) -> bool:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        photo_row = await conn.fetchrow(
            "SELECT ratings_enabled, is_deleted, moderation_status FROM photos WHERE id=$1",
            int(photo_id),
        )
        status = str(photo_row.get("moderation_status") or "").lower()
        if (
            not photo_row
            or int(photo_row.get("is_deleted") or 0) != 0
            or status not in ("active", "good")
            or not bool(photo_row.get("ratings_enabled", 1))
        ):
            return False

        existed = await conn.fetchval(
            "SELECT 1 FROM super_ratings WHERE photo_id=$1 AND user_id=$2",
            int(photo_id), int(user_id)
        )
        if existed:
            return False
        await conn.execute(
            "INSERT INTO super_ratings (photo_id, user_id, created_at) VALUES ($1,$2,$3)",
            int(photo_id), int(user_id), now
        )
    return True


async def get_daily_skip_info(user_id: int) -> dict:
    p = _assert_pool()
    day_key = _today_key()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT day_key, skips_used FROM daily_skips WHERE user_id=$1", int(user_id))
    if not row or str(row["day_key"]) != day_key:
        return {"day_key": day_key, "skips_used": 0}
    return {"day_key": str(row["day_key"]), "skips_used": int(row["skips_used"] or 0)}


async def update_daily_skip_info(user_id: int, skips_used: int) -> None:
    p = _assert_pool()
    now = get_moscow_now_iso()
    day_key = _today_key()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO daily_skips (user_id, day_key, skips_used, updated_at)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (user_id)
            DO UPDATE SET day_key=EXCLUDED.day_key, skips_used=EXCLUDED.skips_used, updated_at=EXCLUDED.updated_at
            """,
            int(user_id), day_key, int(skips_used), now
        )


async def get_weekly_idea_requests(user_id: int, week_key: str | None = None) -> int:
    p = _assert_pool()
    wk = week_key or _week_key()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT count FROM idea_requests WHERE user_id=$1 AND week_key=$2",
            int(user_id),
            str(wk),
        )
    if not row:
        return 0
    try:
        return int(row["count"] or 0)
    except Exception:
        return 0


async def increment_weekly_idea_requests(user_id: int, week_key: str | None = None) -> int:
    p = _assert_pool()
    wk = week_key or _week_key()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        val = await conn.fetchval(
            """
            INSERT INTO idea_requests (user_id, week_key, count, created_at, updated_at)
            VALUES ($1,$2,1,$3,$3)
            ON CONFLICT (user_id, week_key)
            DO UPDATE SET count=idea_requests.count+1, updated_at=$3
            RETURNING count
            """,
            int(user_id),
            str(wk),
            now,
        )
    try:
        return int(val or 0)
    except Exception:
        return 0


# -------------------- reports / moderation --------------------

async def create_photo_report(user_id: int, photo_id: int, reason: str, text: str | None) -> None:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO photo_reports (photo_id, user_id, reason, text, status, created_at)
            VALUES ($1,$2,$3,$4,'pending',$5)
            """,
            int(photo_id), int(user_id), str(reason), text, now
        )


async def get_user_reports_since(user_id: int, since_iso: str, limit: int | None = None) -> list[str]:
    """
    Возвращает created_at всех жалоб пользователя, созданных начиная с since_iso (ISO-строка).
    Используем для ограничения частоты отправки жалоб.
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        query = """
            SELECT created_at
            FROM photo_reports
            WHERE user_id=$1 AND created_at >= $2
            ORDER BY created_at DESC
        """
        params: list[object] = [int(user_id), str(since_iso)]
        if limit is not None:
            query += " LIMIT $3"
            params.append(int(limit))

        rows = await conn.fetch(query, *params)

    return [str(row["created_at"]) for row in rows]


# =====================
# Moderation chat message mapping (photo_id -> (chat_id, message_id))
# Used to edit the same moderation card when new reports arrive.
# =====================

async def _ensure_moderation_messages_table(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS moderation_messages (
            photo_id BIGINT PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """
    )

async def get_moderation_message_for_photo(photo_id: int) -> dict | None:
    """Return mapping for a photo moderation message: {chat_id, message_id}."""
    async with pool.acquire() as conn:
        await _ensure_moderation_messages_table(conn)
        row = await conn.fetchrow(
            "SELECT chat_id, message_id FROM moderation_messages WHERE photo_id=$1",
            int(photo_id),
        )
        if not row:
            return None
        return {"chat_id": int(row["chat_id"]), "message_id": int(row["message_id"])}

async def upsert_moderation_message_for_photo(photo_id: int, chat_id: int, message_id: int) -> None:
    """Create/update mapping for a photo moderation message."""
    async with pool.acquire() as conn:
        await _ensure_moderation_messages_table(conn)
        await conn.execute(
            """
            INSERT INTO moderation_messages(photo_id, chat_id, message_id, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (photo_id)
            DO UPDATE SET chat_id=EXCLUDED.chat_id, message_id=EXCLUDED.message_id, updated_at=NOW()
            """,
            int(photo_id),
            int(chat_id),
            int(message_id),
        )

async def delete_moderation_message_for_photo(photo_id: int) -> None:
    """Remove mapping so next report creates a fresh card."""
    async with pool.acquire() as conn:
        await _ensure_moderation_messages_table(conn)
        await conn.execute(
            "DELETE FROM moderation_messages WHERE photo_id=$1",
            int(photo_id),
        )

async def get_photo_ids_for_user(user_id: int) -> list[int]:
    """Return ALL non-deleted photo ids for a user (any moderation status/day)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM photos WHERE user_id=$1 AND COALESCE(is_deleted, FALSE)=FALSE",
            int(user_id),
        )
    return [int(r["id"]) for r in rows]


async def get_moderation_author_metrics(user_id: int, *, days: int = 30) -> dict:
    """Lightweight author metrics for moderator profile screen."""
    p = _assert_pool()
    since_iso = (get_moscow_now() - timedelta(days=max(1, int(days)))).isoformat()
    async with p.acquire() as conn:
        active_photos = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM photos
            WHERE user_id=$1
              AND COALESCE(is_deleted, 0)=0
              AND COALESCE(moderation_status,'') IN ('active','good','under_review','under_detailed_review')
            """,
            int(user_id),
        )
        deleted_by_mod_30d = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM moderator_reviews mr
            JOIN photos p ON p.id = mr.photo_id
            WHERE p.user_id=$1
              AND mr.created_at >= $2
              AND mr.action LIKE '%:delete:%'
            """,
            int(user_id),
            since_iso,
        )
        reports_30d = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM photo_reports pr
            JOIN photos p ON p.id = pr.photo_id
            WHERE p.user_id=$1
              AND pr.created_at >= $2
            """,
            int(user_id),
            since_iso,
        )
        bans_30d = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM moderator_reviews mr
            JOIN photos p ON p.id = mr.photo_id
            WHERE p.user_id=$1
              AND mr.created_at >= $2
              AND mr.action LIKE '%:ban:%'
            """,
            int(user_id),
            since_iso,
        )
    return {
        "active_photos": int(active_photos or 0),
        "deleted_by_mod_30d": int(deleted_by_mod_30d or 0),
        "reports_30d": int(reports_30d or 0),
        "bans_30d": int(bans_30d or 0),
    }


async def get_photo_report_stats(photo_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM photo_reports WHERE photo_id=$1", int(photo_id))
        pending = await conn.fetchval(
            "SELECT COUNT(*) FROM photo_reports WHERE photo_id=$1 AND status='pending'",
            int(photo_id)
        )
    total_i = int(total or 0)
    pending_i = int(pending or 0)
    return {
        "total": total_i,
        "pending": pending_i,
        "total_all": total_i,
        "total_pending": pending_i,
    }

async def set_photo_moderation_status(photo_id: int, status: str) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE photos SET moderation_status=$1 WHERE id=$2", str(status), int(photo_id))


async def set_photo_file_id_support(photo_id: int, file_id_support: str) -> None:
    if not file_id_support:
        return
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE photos SET file_id_support=$1 WHERE id=$2",
            str(file_id_support),
            int(photo_id),
        )


async def add_moderator_review(moderator_user_id: int, photo_id: int, action: str, note: str | None = None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "INSERT INTO moderator_reviews (moderator_user_id, photo_id, action, note, created_at) VALUES ($1,$2,$3,$4,$5)",
            int(moderator_user_id), int(photo_id), str(action), note, get_moscow_now_iso()
        )


async def get_next_photo_for_moderation() -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM photos
            WHERE is_deleted=0 AND moderation_status='under_review'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """
        )
    return dict(row) if row else None


async def get_next_photo_for_detailed_moderation() -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM photos
            WHERE is_deleted=0 AND moderation_status='under_detailed_review'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """
        )
    return dict(row) if row else None


async def get_next_photo_for_self_moderation(user_id: int | None = None) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        if user_id is None:
            row = await conn.fetchrow(
                """
                SELECT * FROM photos
                WHERE is_deleted=0 AND moderation_status='active'
                ORDER BY random()
                LIMIT 1
                """
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT p.*
                FROM photos p
                WHERE p.is_deleted=0
                  AND p.moderation_status='active'
                  AND p.user_id <> $1
                  AND NOT EXISTS (
                      SELECT 1
                      FROM moderator_reviews mr
                      WHERE mr.moderator_user_id=$1 AND mr.photo_id=p.id
                  )
                ORDER BY random()
                LIMIT 1
                """,
                int(user_id),
            )
    return dict(row) if row else None


# ====================================================
# ================ profile summary ===================
# ====================================================


# -------------------- ranks --------------------

async def calc_user_rank_points(user_id: int, *, limit_photos: int = 10) -> int:
    """Calculate rank points for a user.

    Strategy is defined in utils.ranks (pure math):
      photo_points = bayes_score * log1p(ratings_count)  # база
      + небольшой бонус за оценки других фото
      + бонус за осмысленные комментарии (с дневным лимитом)
      + бонус за streak активности
      - мягкий штраф за подтверждённые жалобы

    We intentionally DO NOT filter by is_deleted here.
    That way a user cannot "reset" rank by deleting photos.

    We DO filter moderation_status in ('active','good') to exclude rejected/hidden content.

    Returns an int suitable for caching in users.rank_points.
    """
    p = _assert_pool()
    limit_photos = int(limit_photos or 10)
    if limit_photos <= 0:
        limit_photos = 10

    from utils.ranks import (
        photo_points as _photo_points,
        points_to_int as _points_to_int,
        ratings_activity_points as _ratings_bonus,
        comments_activity_points as _comments_bonus,
        reports_penalty as _reports_penalty,
        streak_bonus_points as _streak_bonus,
    )
    from utils.time import get_moscow_now

    prior = _bayes_prior_weight()

    # Смотрим активность за последние N дней для бонусов/штрафов
    activity_window_days = 30
    comment_min_len = 15
    ratings_daily_cap = 40   # антиспам: сколько оценок в день засчитываем
    comments_daily_cap = 8   # антиспам: сколько комментов в день засчитываем

    now_dt = get_moscow_now()
    since_dt = now_dt - timedelta(days=activity_window_days)
    since_iso = since_dt.isoformat()

    # tg_id нужен для streak-бонуса
    tg_id: int | None = None

    async with p.acquire() as conn:
        # Узнаем tg_id пользователя (для streak)
        user_row = await conn.fetchrow("SELECT tg_id FROM users WHERE id=$1", int(user_id))
        if user_row and user_row.get("tg_id"):
            try:
                tg_id = int(user_row.get("tg_id"))
            except Exception:
                tg_id = None

        global_mean, _global_cnt = await _get_global_rating_mean(conn)

        w = _link_rating_weight()
        rows = await conn.fetch(
            """
            SELECT
              ph.id AS photo_id,
              COUNT(r.id)::int AS ratings_count,
              COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS sum_values_weighted,
              COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS ratings_weighted_count,
              MAX(ph.created_at) AS created_at_max
            FROM photos ph
            LEFT JOIN ratings r ON r.photo_id = ph.id
            WHERE ph.user_id=$1
              AND ph.moderation_status IN ('active','good')
            GROUP BY ph.id
            ORDER BY created_at_max DESC NULLS LAST, ph.id DESC
            LIMIT $3
            """,
            int(user_id),
            float(w),
            int(limit_photos),
        )

    total_points = 0.0
    for row in rows or []:
        n_weighted = float(row.get("ratings_weighted_count") or 0.0)
        s = float(row.get("sum_values_weighted") or 0.0)
        bayes = _bayes_score(sum_values=s, n=n_weighted, global_mean=global_mean, prior=prior)
        total_points += _photo_points(bayes_score=bayes, ratings_count=int(round(n_weighted)))

    # -------- Бонус за оценки других фото (anti-spam по дням) --------
    ratings_rows = []
    try:
        async with p.acquire() as conn:
            ratings_rows = await conn.fetch(
                "SELECT created_at FROM ratings WHERE user_id=$1 AND created_at >= $2",
                int(user_id),
                since_iso,
            )
    except Exception:
        ratings_rows = []

    ratings_per_day: dict[str, int] = {}
    for r in ratings_rows or []:
        day_key = str(r["created_at"])[:10]
        ratings_per_day[day_key] = ratings_per_day.get(day_key, 0) + 1
    effective_ratings = sum(min(count, ratings_daily_cap) for count in ratings_per_day.values())
    ratings_points = _ratings_bonus(effective_ratings)

    # -------- Бонус за комментарии (считаем только достаточной длины + дневной лимит) --------
    comments_rows = []
    try:
        async with p.acquire() as conn:
            comments_rows = await conn.fetch(
                "SELECT created_at, text FROM comments WHERE user_id=$1 AND created_at >= $2",
                int(user_id),
                since_iso,
            )
    except Exception:
        comments_rows = []

    comments_per_day: dict[str, int] = {}
    for r in comments_rows or []:
        try:
            txt = str(r["text"] or "").strip()
        except Exception:
            txt = ""
        if len(txt) < comment_min_len:
            continue
        day_key = str(r["created_at"])[:10]
        comments_per_day[day_key] = comments_per_day.get(day_key, 0) + 1
    effective_comments = sum(min(count, comments_daily_cap) for count in comments_per_day.values())
    comments_points = _comments_bonus(effective_comments)

    # -------- Штраф за подтверждённые жалобы --------
    resolved_reports = 0
    try:
        async with p.acquire() as conn:
            resolved_reports = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM photo_reports
                WHERE user_id=$1
                  AND created_at >= $2
                  AND status IN ('approved','accepted','resolved','done','action_taken','confirmed')
                """,
                int(user_id),
                since_iso,
            )
    except Exception:
        resolved_reports = 0
    reports_points = _reports_penalty(int(resolved_reports or 0))

    # -------- Бонус за streak --------
    streak_points = 0.0
    if tg_id:
        try:
            streak_status = await streak_get_status_by_tg_id(int(tg_id))
            streak_days = int(streak_status.get("streak") or 0)
            streak_points = _streak_bonus(streak_days)
        except Exception:
            streak_points = 0.0

    total_points += ratings_points
    total_points += comments_points
    total_points += streak_points
    total_points -= reports_points

    return _points_to_int(total_points)


async def refresh_user_rank_cache(user_id: int, *, limit_photos: int = 10) -> dict:
    """Recalculate and store user's rank cache in DB."""
    p = _assert_pool()
    from utils.ranks import rank_from_points, format_rank

    points = await calc_user_rank_points(int(user_id), limit_photos=limit_photos)
    rank = rank_from_points(points)
    now_iso = get_moscow_now_iso()

    async with p.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET rank_points=$1,
                rank_code=$2,
                rank_updated_at=$3,
                updated_at=$3
            WHERE id=$4
            """,
            int(points),
            str(rank.code),
            now_iso,
            int(user_id),
        )

    return {"rank_points": int(points), "rank_code": str(rank.code), "rank_label": format_rank(points)}


async def get_user_rank_cached(user_id: int, *, max_age_seconds: int = 6 * 60 * 60) -> dict:
    """Return user's cached rank; refresh if stale."""
    p = _assert_pool()
    max_age_seconds = int(max_age_seconds or 0)
    if max_age_seconds <= 0:
        max_age_seconds = 6 * 60 * 60

    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT rank_points, rank_code, rank_updated_at
            FROM users
            WHERE id=$1 AND is_deleted=0
            """,
            int(user_id),
        )

    if not row:
        return {"rank_points": 0, "rank_code": None, "rank_label": "🟢 Начинающий"}

    points = int(row["rank_points"] or 0)
    updated_at = row["rank_updated_at"]

    stale = True
    if updated_at:
        try:
            dt = datetime.fromisoformat(str(updated_at))
            stale = (get_moscow_now() - dt).total_seconds() > max_age_seconds
        except Exception:
            stale = True

    if stale:
        return await refresh_user_rank_cache(int(user_id))

    from utils.ranks import format_rank
    return {"rank_points": points, "rank_code": row["rank_code"], "rank_label": format_rank(points)}


async def get_user_rank_by_tg_id(tg_id: int, *, max_age_seconds: int = 6 * 60 * 60) -> dict:
    u = await get_user_by_tg_id(int(tg_id))
    if not u:
        return {"rank_points": 0, "rank_code": None, "rank_label": "🟢 Начинающий"}
    return await get_user_rank_cached(int(u["id"]), max_age_seconds=max_age_seconds)


async def invalidate_user_rank_cache(user_id: int) -> None:
    """Mark rank cache stale so next read recalculates."""
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET rank_updated_at=NULL, updated_at=$1 WHERE id=$2",
            now,
            int(user_id),
        )


async def count_photos_by_user(user_id: int) -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM photos WHERE user_id=$1", int(user_id))
    return int(v or 0)


async def count_active_photos_by_user(user_id: int) -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "SELECT COUNT(*) FROM photos WHERE user_id=$1 AND is_deleted=0 AND status='active'",
            int(user_id),
        )
    return int(v or 0)


async def get_user_rating_summary(user_id: int) -> dict:
    """Profile rating summary.

    Backward-compatible keys:
      - ratings_given
      - ratings_received
      - avg_received

    Added "smart" keys:
      - bayes_received
      - global_mean
      - prior

    Important: ratings_received/avg include BOTH active and soft-deleted photos
    so users cannot reset their profile stats by deleting a photo.
    Only moderation_status in ('active','good') photos are considered.
    """
    p = _assert_pool()
    prior = _bayes_prior_weight()

    async with p.acquire() as conn:
        global_mean, _global_cnt = await _get_global_rating_mean(conn)

        given = await conn.fetchval(
            "SELECT COUNT(*)::int FROM ratings WHERE user_id=$1",
            int(user_id),
        )

        w = _link_rating_weight()
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(r.id)::int AS ratings_received,
                COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS ratings_sum_w,
                COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS ratings_weighted_count,
                AVG(r.value)::float AS avg_raw
            FROM photos ph
            LEFT JOIN ratings r ON r.photo_id = ph.id
            WHERE ph.user_id=$1
              AND ph.moderation_status IN ('active','good')
            """,
            int(user_id),
            float(w),
        )

    ratings_received = int(row["ratings_received"] or 0) if row else 0
    ratings_sum = float(row["ratings_sum_w"] or 0.0) if row else 0.0
    ratings_weighted_count = float(row["ratings_weighted_count"] or 0.0) if row else 0.0
    avg_raw = row["avg_raw"] if row and row["avg_raw"] is not None else None
    avg_received = (ratings_sum / ratings_weighted_count) if ratings_weighted_count > 0 else avg_raw

    # Smart Bayesian average (stabilizes score for small n)
    bayes = _bayes_score(
        sum_values=ratings_sum,
        n=ratings_weighted_count,
        global_mean=global_mean,
        prior=prior,
    )

    return {
        "ratings_given": int(given or 0),
        "ratings_received": int(ratings_received),
        "avg_received": float(avg_received) if avg_received is not None else None,
        "bayes_received": float(bayes) if bayes is not None else None,
        "global_mean": float(global_mean),
        "prior": int(prior),
    }


async def get_user_stats_overview(
    user_id: int,
    *,
    include_premium_metrics: bool = False,
    include_author_metrics: bool = False,
) -> dict:
    """Compact profile stats overview for the "Моя статистика" screen."""
    p = _assert_pool()
    uid = int(user_id)
    since_7d = get_bot_now() - timedelta(days=7)

    async with p.acquire() as conn:
        stats_row = await _ensure_user_stats_row(conn, uid)
        credits = int((stats_row or {}).get("credits") or 0)
        u = await conn.fetchrow(
            """
            SELECT id, tg_id
            FROM users
            WHERE id=$1 OR tg_id=$1
            LIMIT 1
            """,
            uid,
        )
        candidate_user_ids: list[int] = [uid]
        if u:
            try:
                db_uid = int(u.get("id"))
                if db_uid not in candidate_user_ids:
                    candidate_user_ids.append(db_uid)
            except Exception:
                pass
            try:
                db_tg = int(u.get("tg_id"))
                if db_tg not in candidate_user_ids:
                    candidate_user_ids.append(db_tg)
            except Exception:
                pass

        photos_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::int AS photos_uploaded,
                COALESCE(SUM(votes_count) FILTER (WHERE COALESCE(is_deleted,0)=0), 0)::int AS my_votes_total,
                COALESCE(SUM(views_count) FILTER (WHERE COALESCE(is_deleted,0)=0), 0)::int AS my_views_total,
                COALESCE(SUM(sum_score), 0)::float AS rating_sum_all,
                COALESCE(SUM(votes_count), 0)::float AS rating_votes_all
            FROM photos
            WHERE user_id = ANY($1::bigint[])
            """,
            candidate_user_ids,
        )
        global_mean, _ = await _get_global_rating_mean(conn)
        prior = _bayes_prior_weight()
        rating_sum_all = float((photos_row or {}).get("rating_sum_all") or 0.0)
        rating_votes_all = float((photos_row or {}).get("rating_votes_all") or 0.0)
        my_bayes_score = _bayes_score(
            sum_values=rating_sum_all,
            n=rating_votes_all,
            global_mean=global_mean,
            prior=prior,
        )

        votes_given = await conn.fetchval(
            "SELECT COUNT(*)::int FROM votes WHERE voter_id=$1",
            uid,
        )

        ranks_row = await conn.fetchrow(
            """
            SELECT
                MIN(rr.final_rank)::int AS best_rank,
                AVG(rr.final_rank)::float AS avg_rank,
                COUNT(*) FILTER (WHERE rr.final_rank <= 10)::int AS top10_count
            FROM result_ranks rr
            JOIN photos p ON p.id = rr.photo_id
            WHERE p.user_id = ANY($1::bigint[])
              AND COALESCE(p.is_deleted,0)=0
            """,
            candidate_user_ids,
        )

        votes_7d = 0
        active_days_7d = 0
        if include_premium_metrics:
            v7_row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::int AS votes_7d,
                    COUNT(DISTINCT (created_at AT TIME ZONE 'Europe/Moscow')::date)::int AS active_days_7d
                FROM votes
                WHERE voter_id=$1
                  AND created_at >= $2
                """,
                uid,
                since_7d,
            )
            votes_7d = int((v7_row or {}).get("votes_7d") or 0)
            active_days_7d = int((v7_row or {}).get("active_days_7d") or 0)

        positive_percent = None
        if include_author_metrics:
            pos_row = await conn.fetchrow(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN v.score BETWEEN 6 AND 10 THEN 1 ELSE 0 END), 0)::int AS positive_votes,
                    COUNT(v.*)::int AS total_votes
                FROM photos p
                LEFT JOIN votes v ON v.photo_id = p.id
                WHERE p.user_id = ANY($1::bigint[])
                  AND COALESCE(p.is_deleted,0)=0
                """,
                candidate_user_ids,
            )
            pos_votes = int((pos_row or {}).get("positive_votes") or 0)
            total_votes = int((pos_row or {}).get("total_votes") or 0)
            if total_votes > 0:
                positive_percent = int(round((pos_votes / total_votes) * 100))

    return {
        "votes_given": int(votes_given or 0),
        "photos_uploaded": int((photos_row or {}).get("photos_uploaded") or 0),
        "my_avg_score": float(my_bayes_score) if my_bayes_score is not None else None,
        "best_rank": int((ranks_row or {}).get("best_rank") or 0) or None,
        "my_votes_total": int((photos_row or {}).get("my_votes_total") or 0),
        "my_views_total": int((photos_row or {}).get("my_views_total") or 0),
        "credits": int(credits),
        "votes_7d": int(votes_7d),
        "active_days_7d": int(active_days_7d),
        "avg_rank": float((ranks_row or {}).get("avg_rank")) if (ranks_row and ranks_row.get("avg_rank") is not None) else None,
        "top10_count": int((ranks_row or {}).get("top10_count") or 0),
        "positive_percent": positive_percent,
    }


async def get_most_popular_photo_for_user(user_id: int) -> dict | None:
    """Return user's best photo for profile.

    - Prefer active photos (is_deleted=0)
    - If none, fallback to archived
    - bayes_score is computed in SQL (NOT a real DB column)
    - Supports legacy where photos.user_id could be users.id or tg_id
    """
    p = _assert_pool()
    prior = _bayes_prior_weight()

    async with p.acquire() as conn:
        global_mean, _ = await _get_global_rating_mean(conn)
        w = _link_rating_weight()

        u = await conn.fetchrow(
            """
            SELECT id, tg_id
            FROM users
            WHERE id=$1 OR tg_id=$1
            LIMIT 1
            """,
            int(user_id),
        )
        if not u:
            return None

        uid = int(u["id"])
        tgid = int(u["tg_id"])
        candidate_user_ids = [uid]
        if tgid not in candidate_user_ids:
            candidate_user_ids.append(tgid)

        async def _pick(where_deleted_sql: str, *, require_rated: bool = False):
            having_clause = "HAVING COUNT(r.id) > 0" if require_rated else ""
            q = f"""
                WITH s AS (
                    SELECT
                        ph.*,
                        COUNT(r.id)::int AS ratings_count,
                        COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS ratings_sum_w,
                        COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS ratings_weighted_count,
                        CASE
                            WHEN COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0) > 0 THEN
                                COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float
                                / COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)
                            ELSE NULL
                        END AS avg_rating
                    FROM photos ph
                    LEFT JOIN ratings r ON r.photo_id = ph.id
                    WHERE ph.user_id = ANY($1::bigint[])
                    {where_deleted_sql}
                    GROUP BY ph.id
                    {having_clause}
                )
                SELECT
                    s.*,
                    CASE
                        WHEN s.ratings_weighted_count > 0 THEN
                            ($3::float * $4::float + s.ratings_sum_w) / ($3::float + s.ratings_weighted_count)
                        ELSE NULL
                    END AS bayes_score
                FROM s
                ORDER BY
                    ratings_count DESC,
                    bayes_score DESC NULLS LAST,
                    avg_rating DESC NULLS LAST,
                    id ASC
                LIMIT 1
            """
            return await conn.fetchrow(q, candidate_user_ids, float(w), float(prior), float(global_mean))

        try:
            # 1) Активные с оценками
            row = await _pick("AND ph.is_deleted=0", require_rated=True)
            if row:
                return dict(row)
            # 2) Любые с оценками (может быть архив)
            row = await _pick("", require_rated=True)
            if row:
                return dict(row)
            # 3) Активные без оценок (fallback)
            row = await _pick("AND ph.is_deleted=0", require_rated=False)
            if row:
                return dict(row)
            # 4) Любые без оценок (последний fallback)
            row = await _pick("", require_rated=False)
            return dict(row) if row else None
        except Exception:
            return None

        # First try: full stats (may fail if some columns like created_at/file_id don't exist)
        sql_full = """
            WITH stats AS (
                SELECT
                    ph.id,
                    ph.user_id,
                    ph.title,
                    ph.is_deleted,
                    ph.created_at,
                    COUNT(r.id)::int AS ratings_count,
                    COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS ratings_sum_w,
                    COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS ratings_weighted_count,
                    CASE
                        WHEN COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0) > 0 THEN
                            COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float
                            / COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)
                        ELSE NULL
                    END AS avg_rating
                FROM photos ph
                LEFT JOIN ratings r ON r.photo_id = ph.id
                WHERE ph.user_id = ANY($1::bigint[])
                GROUP BY ph.id
            )
            SELECT *
            FROM (
                SELECT
                    s.*,
                    CASE
                        WHEN s.ratings_weighted_count > 0
                            THEN (($3::float * $4::float) + s.ratings_sum_w) / (s.ratings_weighted_count + $3::float)
                        ELSE NULL
                    END::float AS bayes_score
                FROM stats s
            ) t
            ORDER BY
                (t.bayes_score IS NULL) ASC,
                t.bayes_score DESC,
                t.ratings_count DESC,
                t.id ASC
            LIMIT 1
        """

        sql_min = """
            WITH stats AS (
                SELECT
                    ph.id,
                    ph.user_id,
                    COUNT(r.id)::int AS ratings_count,
                    COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS ratings_sum_w,
                    COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float AS ratings_weighted_count,
                    CASE
                        WHEN COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0) > 0 THEN
                            COALESCE(SUM(r.value * CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)::float
                            / COALESCE(SUM(CASE WHEN r.source='link' THEN $2 ELSE 1 END), 0)
                        ELSE NULL
                    END AS avg_rating
                FROM photos ph
                LEFT JOIN ratings r ON r.photo_id = ph.id
                WHERE ph.user_id = ANY($1::bigint[])
                GROUP BY ph.id
            )
            SELECT *
            FROM (
                SELECT
                    s.*,
                    CASE
                        WHEN s.ratings_weighted_count > 0
                            THEN (($3::float * $4::float) + s.ratings_sum_w) / (s.ratings_weighted_count + $3::float)
                        ELSE NULL
                    END::float AS bayes_score
                FROM stats s
            ) t
            ORDER BY
                (t.bayes_score IS NULL) ASC,
                t.bayes_score DESC,
                t.ratings_count DESC,
                t.id ASC
            LIMIT 1
        """

        try:
            row = await conn.fetchrow(sql_full, candidate_ids, float(w), float(prior), float(global_mean))
        except Exception:
            row = await conn.fetchrow(sql_min, candidate_ids, float(w), float(prior), float(global_mean))

        if not row:
            return None

        result = dict(row)

        # If the minimal query was used, try to enrich with title/is_deleted/created_at if those columns exist.
        if "title" not in result or "is_deleted" not in result or "created_at" not in result:
            try:
                ph = await conn.fetchrow(
                    "SELECT title, is_deleted, created_at FROM photos WHERE id=$1 LIMIT 1",
                    int(result["id"]),
                )
                if ph:
                    if "title" not in result:
                        result["title"] = ph.get("title")
                    if "is_deleted" not in result:
                        result["is_deleted"] = ph.get("is_deleted")
                    if "created_at" not in result:
                        result["created_at"] = ph.get("created_at")
            except Exception:
                # Ignore enrichment errors; profile can fallback to defaults.
                pass

        return result


# ========== ADMIN & STATS ==========

# --- basic user lists / roles ---


async def get_total_users() -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM users
            WHERE is_deleted=0
              AND COALESCE(NULLIF(trim(name), ''), NULL) IS NOT NULL
              AND COALESCE(is_blocked, 0)=0
            """
        )
    return int(v or 0)

async def get_new_registered_today_count() -> int:
    """Количество зарегистрированных пользователей, добавившихся сегодня (по Москве)."""
    p = _assert_pool()
    today_start = get_moscow_now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM users
            WHERE is_deleted=0
              AND COALESCE(is_blocked,0)=0
              AND COALESCE(NULLIF(trim(name), ''), NULL) IS NOT NULL
              AND created_at >= $1
            """,
            today_start,
        )
    return int(v or 0)

async def get_unregistered_users_count() -> int:
    """Количество аккаунтов без имени (не завершили регистрацию), не удалённых и не заблокированных."""
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM users
            WHERE is_deleted=0
              AND COALESCE(is_blocked,0)=0
              AND COALESCE(NULLIF(trim(name), ''), NULL) IS NULL
            """
        )
    return int(v or 0)

async def get_unregistered_users_page(limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
    """(total, users_page) for unregistered users (no name)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM users
            WHERE is_deleted=0
              AND COALESCE(is_blocked,0)=0
              AND COALESCE(NULLIF(trim(name), ''), NULL) IS NULL
            """
        )
        rows = await conn.fetch(
            """
            SELECT *
            FROM users
            WHERE is_deleted=0
              AND COALESCE(is_blocked,0)=0
              AND COALESCE(NULLIF(trim(name), ''), NULL) IS NULL
            ORDER BY created_at DESC, id DESC
            OFFSET $1 LIMIT $2
            """,
            int(offset or 0),
            int(limit),
        )
    return int(total or 0), [dict(r) for r in rows]

async def get_referrals_total() -> int:
    """Всего переходов/регистраций по реферальным ссылкам (referrals записей)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM referrals")
    return int(v or 0)

async def get_referral_invited_users_page(limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
    """(total, users_page) for users who came via referral links."""
    p = _assert_pool()
    async with p.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM referrals")
        rows = await conn.fetch(
            """
            SELECT u.*
            FROM referrals r
            JOIN users u ON u.id = r.invited_user_id
            ORDER BY r.created_at DESC, r.id DESC
            OFFSET $1 LIMIT $2
            """,
            int(offset or 0),
            int(limit),
        )
    return int(total or 0), [dict(r) for r in rows]


async def get_all_users_tg_ids() -> list[int]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tg_id
            FROM users
            WHERE is_deleted=0
              AND COALESCE(NULLIF(trim(name), ''), NULL) IS NOT NULL
              AND COALESCE(is_blocked, 0)=0
            """
        )
    return [int(r["tg_id"]) for r in rows]


async def get_moderators() -> list[int]:
    """Telegram IDs of moderators."""
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tg_id FROM users WHERE is_moderator=1 AND is_deleted=0"
        )
    return [int(r["tg_id"]) for r in rows]


async def get_helpers() -> list[int]:
    """Telegram IDs of helpers."""
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tg_id FROM users WHERE is_helper=1 AND is_deleted=0"
        )
    return [int(r["tg_id"]) for r in rows]


async def get_support_users() -> list[int]:
    """Telegram IDs of support users."""
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tg_id FROM users WHERE is_support=1 AND is_deleted=0"
        )
    return [int(r["tg_id"]) for r in rows]


async def get_authors() -> list[int]:
    """Telegram IDs of verified authors."""
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tg_id FROM users WHERE is_author=1 AND is_deleted=0 AND tg_id IS NOT NULL"
        )
    return [int(r["tg_id"]) for r in rows]


async def get_support_users_full() -> list[dict]:
    """Support users with tg_id and username.

    Returns list of dicts: {"tg_id": int, "username": str|None}
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tg_id, username FROM users WHERE is_support=1 AND is_deleted=0"
        )
    out: list[dict] = []
    for r in rows:
        out.append({
            "tg_id": int(r["tg_id"]),
            "username": (str(r["username"]) if r["username"] is not None else None),
        })
    return out


async def get_premium_users(limit: int = 20, offset: int = 0) -> list[dict]:
    """Страница премиум-пользователей (для админских списков)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
            FROM users
            WHERE is_premium=1
              AND is_deleted=0
              AND COALESCE(NULLIF(trim(name), ''), NULL) IS NOT NULL
              AND COALESCE(is_blocked,0)=0
            ORDER BY premium_until DESC NULLS LAST, id DESC
            OFFSET $1 LIMIT $2
            """,
            int(offset),
            int(limit),
        )
    return [dict(r) for r in rows]


async def get_users_sample(
    limit: int = 20,
    offset: int | None = None,
    only_active: bool = True,
) -> list[dict]:
    """
    Универсальная выборка пользователей для админки.

    Поддерживает старые вызовы:
      - get_users_sample(limit=20)
      - get_users_sample(limit=20, offset=0)
      - get_users_sample(limit=20, only_active=False)
    """
    p = _assert_pool()
    base_filter = [
        "COALESCE(NULLIF(trim(name), ''), NULL) IS NOT NULL",
        "COALESCE(is_blocked,0)=0",
    ]
    if only_active:
        base_filter.append("is_deleted=0")
    where = "WHERE " + " AND ".join(base_filter)
    off = int(offset or 0)

    async with p.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT *
            FROM users
            {where}
            ORDER BY id DESC
            OFFSET $2 LIMIT $1
            """,
            int(limit),
            off,
        )
    return [dict(r) for r in rows]


# --- activity / online ---

async def log_activity_event(
    tg_id: int,
    *,
    kind: str = "any",
    username: str | None = None,
) -> None:
    """Log a lightweight activity event for online/activity charts."""
    try:
        user = await ensure_user_minimal_row(int(tg_id), username=username)
    except Exception:
        user = None
    if not user or not user.get("id"):
        return
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO activity_events (user_id, kind, created_at)
            VALUES ($1, $2, NOW())
            """,
            int(user["id"]),
            str(kind or "any"),
        )


async def get_active_users_last_24h(limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
    """(total, sample) of users with any activity in last 24h."""
    p = _assert_pool()
    since_iso = (get_moscow_now() - timedelta(hours=24)).isoformat()
    async with p.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(*) FROM (
              SELECT u.id
              FROM users u
              LEFT JOIN LATERAL (
                SELECT 1 FROM activity_events ae
                WHERE ae.user_id = u.id AND ae.created_at >= $1
                LIMIT 1
              ) ae ON TRUE
              WHERE u.is_deleted=0
                AND COALESCE(NULLIF(trim(u.name), ''), NULL) IS NOT NULL
                AND COALESCE(u.is_blocked,0)=0
                AND (
                  ae IS NOT NULL
                  OR COALESCE(u.updated_at, u.created_at) >= $1
                )
            ) t
            """,
            since_iso,
        )
        rows = await conn.fetch(
            """
            SELECT u.*
            FROM users u
            LEFT JOIN LATERAL (
              SELECT 1 FROM activity_events ae
              WHERE ae.user_id = u.id AND ae.created_at >= $1
              LIMIT 1
            ) ae ON TRUE
            WHERE u.is_deleted=0
              AND COALESCE(NULLIF(trim(u.name), ''), NULL) IS NOT NULL
              AND COALESCE(u.is_blocked,0)=0
              AND (
                ae IS NOT NULL
                OR COALESCE(u.updated_at, u.created_at) >= $1
              )
            ORDER BY COALESCE(u.updated_at, u.created_at) DESC, u.id DESC
            OFFSET $2 LIMIT $3
            """,
            since_iso,
            int(offset or 0),
            int(limit),
        )
    return int(total or 0), [dict(r) for r in rows]

async def get_active_users_today(limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
    """(total, sample) of users with any activity since start of Moscow day."""
    p = _assert_pool()
    since_iso = get_moscow_now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    async with p.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(*) FROM (
              SELECT u.id
              FROM users u
              LEFT JOIN LATERAL (
                SELECT 1 FROM activity_events ae
                WHERE ae.user_id = u.id AND ae.created_at >= $1
                LIMIT 1
              ) ae ON TRUE
              WHERE u.is_deleted=0
                AND COALESCE(NULLIF(trim(u.name), ''), NULL) IS NOT NULL
                AND COALESCE(u.is_blocked,0)=0
                AND (
                  ae IS NOT NULL
                  OR COALESCE(u.updated_at, u.created_at) >= $1
                )
            ) t
            """,
            since_iso,
        )
        rows = await conn.fetch(
            """
            SELECT u.*
            FROM users u
            LEFT JOIN LATERAL (
              SELECT 1 FROM activity_events ae
              WHERE ae.user_id = u.id AND ae.created_at >= $1
              LIMIT 1
            ) ae ON TRUE
            WHERE u.is_deleted=0
              AND COALESCE(NULLIF(trim(u.name), ''), NULL) IS NOT NULL
              AND COALESCE(u.is_blocked,0)=0
              AND (
                ae IS NOT NULL
                OR COALESCE(u.updated_at, u.created_at) >= $1
              )
            ORDER BY COALESCE(u.updated_at, u.created_at) DESC, u.id DESC
            OFFSET $2 LIMIT $3
            """,
            since_iso,
            int(offset or 0),
            int(limit),
        )
    return int(total or 0), [dict(r) for r in rows]


async def get_online_users_recent(window_minutes: int = 5, limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
    """
    Пользователи, у которых есть activity_events за последние N минут.
    Совместимо с вызовами:
      get_online_users_recent()
      get_online_users_recent(10)
      get_online_users_recent(window_minutes=5, limit=20)
    """
    p = _assert_pool()
    since_iso = (get_moscow_now() - timedelta(minutes=int(window_minutes))).isoformat()
    async with p.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(*) FROM (
              SELECT u.id
              FROM users u
              LEFT JOIN LATERAL (
                SELECT 1 FROM activity_events ae
                WHERE ae.user_id = u.id AND ae.created_at >= $1
                LIMIT 1
              ) ae ON TRUE
              WHERE u.is_deleted=0
                AND COALESCE(NULLIF(trim(u.name), ''), NULL) IS NOT NULL
                AND COALESCE(u.is_blocked,0)=0
                AND (
                  ae IS NOT NULL
                  OR COALESCE(u.updated_at, u.created_at) >= $1
                )
            ) t
            """,
            since_iso,
        )
        rows = await conn.fetch(
            """
            SELECT u.*
            FROM users u
            LEFT JOIN LATERAL (
              SELECT 1 FROM activity_events ae
              WHERE ae.user_id = u.id AND ae.created_at >= $1
              LIMIT 1
            ) ae ON TRUE
            WHERE u.is_deleted=0
              AND COALESCE(NULLIF(trim(u.name), ''), NULL) IS NOT NULL
              AND COALESCE(u.is_blocked,0)=0
              AND (
                ae IS NOT NULL
                OR COALESCE(u.updated_at, u.created_at) >= $1
              )
            ORDER BY COALESCE(u.updated_at, u.created_at) DESC, u.id DESC
            OFFSET $2 LIMIT $3
            """,
            since_iso,
            int(offset or 0),
            int(limit),
        )
    return int(total or 0), [dict(r) for r in rows]


async def get_total_activity_events() -> int:
    """Total number of activity events (admin stats)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM activity_events")
    return int(v or 0)


async def get_total_activity_events_last_days(days: int = 7) -> int:
    """Total events for last N days (for graphs)."""
    p = _assert_pool()
    since_iso = (get_moscow_now() - timedelta(days=int(days))).isoformat()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "SELECT COUNT(*) FROM activity_events WHERE created_at >= $1",
            since_iso,
        )
    return int(v or 0)


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            # fallback: try parsing date-only
            try:
                dt = datetime.fromisoformat(str(value) + "T00:00:00")
            except Exception:
                raise
    # Queries below cast params to timestamp (without timezone), so pass naive datetime.
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


async def get_activity_counts_by_hour(start_iso: object, end_iso: object) -> list[dict]:
    """Counts of activity_events grouped by hour for [start, end)."""
    p = _assert_pool()
    start_dt = _coerce_datetime(start_iso)
    end_dt = _coerce_datetime(end_iso)
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT date_trunc('hour', created_at::timestamp) AS bucket, COUNT(*) AS cnt
            FROM activity_events
            WHERE created_at::timestamp >= $1::timestamp
              AND created_at::timestamp < $2::timestamp
            GROUP BY 1
            ORDER BY 1 ASC
            """,
            start_dt,
            end_dt,
        )
    return [dict(r) for r in rows]


async def get_activity_counts_by_day(start_iso: object, end_iso: object) -> list[dict]:
    """Counts of activity_events grouped by day for [start, end)."""
    p = _assert_pool()
    start_dt = _coerce_datetime(start_iso)
    end_dt = _coerce_datetime(end_iso)
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT date_trunc('day', created_at::timestamp) AS bucket, COUNT(*) AS cnt
            FROM activity_events
            WHERE created_at::timestamp >= $1::timestamp
              AND created_at::timestamp < $2::timestamp
            GROUP BY 1
            ORDER BY 1 ASC
            """,
            start_dt,
            end_dt,
        )
    return [dict(r) for r in rows]


def _activity_section_case_sql() -> str:
    return """
        CASE
          WHEN lower(coalesce(kind, '')) LIKE '%rate%' OR lower(coalesce(kind, '')) LIKE '%vote%' THEN 'rate'
          WHEN lower(coalesce(kind, '')) LIKE '%upload%' OR lower(coalesce(kind, '')) LIKE '%photo%' THEN 'upload'
          WHEN lower(coalesce(kind, '')) LIKE '%profile%' THEN 'profile'
          WHEN lower(coalesce(kind, '')) LIKE '%result%' THEN 'results'
          WHEN lower(coalesce(kind, '')) LIKE '%support%' THEN 'support'
          WHEN lower(coalesce(kind, '')) LIKE '%admin%' THEN 'admin'
          WHEN lower(coalesce(kind, '')) = 'callback' THEN 'menu'
          WHEN lower(coalesce(kind, '')) = 'message' THEN 'messages'
          ELSE 'other'
        END
    """


async def get_activity_overview(start_iso: object, end_iso: object) -> dict:
    """Overview metrics for admin activity dashboard."""
    p = _assert_pool()
    start_dt = _coerce_datetime(start_iso)
    end_dt = _coerce_datetime(end_iso)
    section_case = _activity_section_case_sql()

    async with p.acquire() as conn:
        totals = await conn.fetchrow(
            """
            SELECT
              COUNT(*)::int AS total_events,
              COUNT(DISTINCT user_id)::int AS unique_users
            FROM activity_events
            WHERE created_at::timestamp >= $1::timestamp
              AND created_at::timestamp < $2::timestamp
            """,
            start_dt,
            end_dt,
        )
        top_section = await conn.fetchrow(
            f"""
            SELECT {section_case} AS section, COUNT(*)::int AS cnt
            FROM activity_events
            WHERE created_at::timestamp >= $1::timestamp
              AND created_at::timestamp < $2::timestamp
            GROUP BY 1
            ORDER BY cnt DESC, section ASC
            LIMIT 1
            """,
            start_dt,
            end_dt,
        )
        top_user = await conn.fetchrow(
            """
            SELECT
              u.id AS user_id,
              u.tg_id AS tg_id,
              u.name AS name,
              u.username AS username,
              u.author_code AS author_code,
              COUNT(*)::int AS cnt
            FROM activity_events ae
            JOIN users u ON u.id = ae.user_id
            WHERE ae.created_at::timestamp >= $1::timestamp
              AND ae.created_at::timestamp < $2::timestamp
              AND ae.user_id IS NOT NULL
            GROUP BY u.id
            ORDER BY cnt DESC, u.id DESC
            LIMIT 1
            """,
            start_dt,
            end_dt,
        )
        errors_total = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM bot_error_logs
            WHERE created_at::timestamp >= $1::timestamp
              AND created_at::timestamp < $2::timestamp
            """,
            start_dt,
            end_dt,
        )

    return {
        "total_events": int((totals or {}).get("total_events") or 0),
        "unique_users": int((totals or {}).get("unique_users") or 0),
        "errors_total": int(errors_total or 0),
        "top_section": (top_section or {}).get("section"),
        "top_section_cnt": int((top_section or {}).get("cnt") or 0),
        "top_user": dict(top_user) if top_user else None,
        "top_user_cnt": int((top_user or {}).get("cnt") or 0),
    }


async def get_top_users_activity(
    start_iso: object,
    end_iso: object,
    limit: int = 10,
    kind: str = "events",
) -> list[dict]:
    """Top users for selected activity metric from activity_events logs."""
    p = _assert_pool()
    start_dt = _coerce_datetime(start_iso)
    end_dt = _coerce_datetime(end_iso)
    lim = max(1, min(int(limit or 10), 100))

    vote_expr = "SUM(CASE WHEN lower(coalesce(ae.kind,'')) LIKE '%rate%' OR lower(coalesce(ae.kind,'')) LIKE '%vote%' THEN 1 ELSE 0 END)"
    upload_expr = "SUM(CASE WHEN lower(coalesce(ae.kind,'')) LIKE '%upload%' OR lower(coalesce(ae.kind,'')) LIKE '%photo%' THEN 1 ELSE 0 END)"
    report_expr = "SUM(CASE WHEN lower(coalesce(ae.kind,'')) LIKE '%report%' OR lower(coalesce(ae.kind,'')) LIKE '%complaint%' THEN 1 ELSE 0 END)"
    metric_map = {
        "events": "COUNT(*)",
        "votes": vote_expr,
        "uploads": upload_expr,
        "reports": report_expr,
    }
    metric_expr = metric_map.get(str(kind or "events").strip().lower(), "COUNT(*)")

    query = f"""
        SELECT
          u.id AS user_id,
          u.tg_id AS tg_id,
          u.name AS name,
          u.username AS username,
          u.author_code AS author_code,
          COUNT(*)::int AS total_events,
          {vote_expr}::int AS votes_count,
          {upload_expr}::int AS uploads_count,
          {report_expr}::int AS reports_count,
          {metric_expr}::int AS metric_count
        FROM activity_events ae
        JOIN users u ON u.id = ae.user_id
        WHERE ae.created_at::timestamp >= $1::timestamp
          AND ae.created_at::timestamp < $2::timestamp
          AND ae.user_id IS NOT NULL
        GROUP BY u.id
        HAVING {metric_expr} > 0
        ORDER BY metric_count DESC, total_events DESC, u.id DESC
        LIMIT $3
    """
    async with p.acquire() as conn:
        rows = await conn.fetch(query, start_dt, end_dt, lim)
    return [dict(r) for r in rows]


async def get_top_sections(start_iso: object, end_iso: object, limit: int = 10) -> list[dict]:
    """Top activity sections from activity_events.kind (mapped)."""
    p = _assert_pool()
    start_dt = _coerce_datetime(start_iso)
    end_dt = _coerce_datetime(end_iso)
    lim = max(1, min(int(limit or 10), 100))
    section_case = _activity_section_case_sql()
    query = f"""
        SELECT {section_case} AS section, COUNT(*)::int AS cnt
        FROM activity_events
        WHERE created_at::timestamp >= $1::timestamp
          AND created_at::timestamp < $2::timestamp
        GROUP BY 1
        ORDER BY cnt DESC, section ASC
        LIMIT $3
    """
    async with p.acquire() as conn:
        rows = await conn.fetch(query, start_dt, end_dt, lim)
    return [dict(r) for r in rows]


async def get_spam_suspects(start_iso: object, end_iso: object, limit: int = 10) -> list[dict]:
    """Suspicious users based on activity/error burst aggregates."""
    p = _assert_pool()
    start_dt = _coerce_datetime(start_iso)
    end_dt = _coerce_datetime(end_iso)
    lim = max(1, min(int(limit or 10), 100))

    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH act_min AS (
              SELECT
                user_id,
                date_trunc('minute', created_at::timestamp) AS minute_bucket,
                COUNT(*)::int AS cnt
              FROM activity_events
              WHERE created_at::timestamp >= $1::timestamp
                AND created_at::timestamp < $2::timestamp
                AND user_id IS NOT NULL
              GROUP BY user_id, minute_bucket
            ),
            act_agg AS (
              SELECT
                user_id,
                SUM(cnt)::int AS total_events,
                MAX(cnt)::int AS peak_per_minute,
                COUNT(*)::int AS active_minutes
              FROM act_min
              GROUP BY user_id
            ),
            act_kind AS (
              SELECT
                user_id,
                SUM(CASE WHEN lower(coalesce(kind,''))='callback' THEN 1 ELSE 0 END)::int AS callback_events,
                SUM(CASE WHEN lower(coalesce(kind,''))='message' THEN 1 ELSE 0 END)::int AS message_events
              FROM activity_events
              WHERE created_at::timestamp >= $1::timestamp
                AND created_at::timestamp < $2::timestamp
                AND user_id IS NOT NULL
              GROUP BY user_id
            ),
            err AS (
              SELECT
                tg_user_id,
                COUNT(*)::int AS errors_total,
                SUM(CASE WHEN lower(coalesce(error_type,'')) LIKE '%badrequest%' THEN 1 ELSE 0 END)::int AS bad_request_cnt,
                SUM(CASE WHEN lower(coalesce(error_type,'')) LIKE '%flood%' THEN 1 ELSE 0 END)::int AS flood_cnt
              FROM bot_error_logs
              WHERE created_at::timestamp >= $1::timestamp
                AND created_at::timestamp < $2::timestamp
                AND tg_user_id IS NOT NULL
              GROUP BY tg_user_id
            )
            SELECT
              u.id AS user_id,
              u.tg_id AS tg_id,
              u.name AS name,
              u.username AS username,
              u.author_code AS author_code,
              COALESCE(a.total_events, 0)::int AS total_events,
              COALESCE(a.peak_per_minute, 0)::int AS peak_per_minute,
              COALESCE(a.active_minutes, 0)::int AS active_minutes,
              COALESCE(k.callback_events, 0)::int AS callback_events,
              COALESCE(k.message_events, 0)::int AS message_events,
              COALESCE(e.errors_total, 0)::int AS errors_total,
              COALESCE(e.bad_request_cnt, 0)::int AS bad_request_cnt,
              COALESCE(e.flood_cnt, 0)::int AS flood_cnt,
              (
                COALESCE(a.peak_per_minute, 0) * 5
                + COALESCE(e.errors_total, 0) * 2
                + COALESCE(e.bad_request_cnt, 0) * 3
                + COALESCE(e.flood_cnt, 0) * 4
              )::int AS score
            FROM users u
            LEFT JOIN act_agg a ON a.user_id = u.id
            LEFT JOIN act_kind k ON k.user_id = u.id
            LEFT JOIN err e ON e.tg_user_id = u.tg_id
            WHERE COALESCE(a.total_events, 0) > 0
               OR COALESCE(e.errors_total, 0) > 0
            ORDER BY score DESC, errors_total DESC, total_events DESC, u.id DESC
            LIMIT $3
            """,
            start_dt,
            end_dt,
            lim,
        )
    return [dict(r) for r in rows]


async def get_error_counts_by_type(start_iso: object, end_iso: object, limit: int = 10) -> list[dict]:
    p = _assert_pool()
    start_dt = _coerce_datetime(start_iso)
    end_dt = _coerce_datetime(end_iso)
    lim = max(1, min(int(limit or 10), 100))
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(NULLIF(error_type, ''), 'Error') AS error_type, COUNT(*)::int AS cnt
            FROM bot_error_logs
            WHERE created_at::timestamp >= $1::timestamp
              AND created_at::timestamp < $2::timestamp
            GROUP BY 1
            ORDER BY cnt DESC, error_type ASC
            LIMIT $3
            """,
            start_dt,
            end_dt,
            lim,
        )
    return [dict(r) for r in rows]


async def get_error_counts_by_handler(start_iso: object, end_iso: object, limit: int = 10) -> list[dict]:
    p = _assert_pool()
    start_dt = _coerce_datetime(start_iso)
    end_dt = _coerce_datetime(end_iso)
    lim = max(1, min(int(limit or 10), 100))
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(NULLIF(handler, ''), 'unknown') AS handler, COUNT(*)::int AS cnt
            FROM bot_error_logs
            WHERE created_at::timestamp >= $1::timestamp
              AND created_at::timestamp < $2::timestamp
            GROUP BY 1
            ORDER BY cnt DESC, handler ASC
            LIMIT $3
            """,
            start_dt,
            end_dt,
            lim,
        )
    return [dict(r) for r in rows]


async def get_error_rate(start_iso: object, end_iso: object) -> float:
    p = _assert_pool()
    start_dt = _coerce_datetime(start_iso)
    end_dt = _coerce_datetime(end_iso)
    async with p.acquire() as conn:
        errors_total = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM bot_error_logs
            WHERE created_at::timestamp >= $1::timestamp
              AND created_at::timestamp < $2::timestamp
            """,
            start_dt,
            end_dt,
        )
        events_total = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM activity_events
            WHERE created_at::timestamp >= $1::timestamp
              AND created_at::timestamp < $2::timestamp
            """,
            start_dt,
            end_dt,
        )
    events_val = int(events_total or 0)
    if events_val <= 0:
        return 0.0
    return (int(errors_total or 0) * 100.0) / float(events_val)


async def get_errors_summary(start_iso: object, end_iso: object, limit: int = 10) -> dict:
    p = _assert_pool()
    start_dt = _coerce_datetime(start_iso)
    end_dt = _coerce_datetime(end_iso)
    lim = max(1, min(int(limit or 10), 100))
    by_type = await get_error_counts_by_type(start_dt, end_dt, lim)
    by_handler = await get_error_counts_by_handler(start_dt, end_dt, lim)
    rate = await get_error_rate(start_dt, end_dt)
    async with p.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM bot_error_logs
            WHERE created_at::timestamp >= $1::timestamp
              AND created_at::timestamp < $2::timestamp
            """,
            start_dt,
            end_dt,
        )
    return {
        "total_errors": int(total or 0),
        "error_rate": float(rate),
        "by_type": by_type,
        "by_handler": by_handler,
    }

# --- premium / new / blocked ---


async def get_premium_stats(limit: int = 20) -> dict:
    """
    Возвращает dict:
      - total   — всего премиум-пользователей
      - active  — у кого подписка ещё активна
      - expired — истёкшие
      - sample  — список пользователей (dict) для предпросмотра
    """
    p = _assert_pool()
    now_dt = get_moscow_now()
    async with p.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM users
            WHERE is_deleted=0
              AND is_premium=1
              AND COALESCE(NULLIF(trim(name), ''), NULL) IS NOT NULL
              AND COALESCE(is_blocked,0)=0
            """
        )
        active = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM users
            WHERE is_deleted=0
              AND is_premium=1
              AND COALESCE(NULLIF(trim(name), ''), NULL) IS NOT NULL
              AND COALESCE(is_blocked,0)=0
              AND (
                premium_until IS NULL
                OR premium_until = ''
                OR premium_until::timestamp > $1
              )
            """,
            now_dt,
        )
        expired = int(total or 0) - int(active or 0)
        sample_rows = await conn.fetch(
            """
            SELECT *
            FROM users
            WHERE is_deleted=0
              AND is_premium=1
              AND COALESCE(NULLIF(trim(name), ''), NULL) IS NOT NULL
              AND COALESCE(is_blocked,0)=0
            ORDER BY premium_until DESC NULLS LAST, id DESC
            LIMIT $1
            """,
            int(limit),
        )
    return {
        "total": int(total or 0),
        "active": int(active or 0),
        "expired": int(expired or 0),
        "sample": [dict(r) for r in sample_rows],
    }


async def get_new_users_last_days(days: int = 3, limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
    """(total, sample) for users created within last N days."""
    p = _assert_pool()
    cutoff = (get_moscow_now() - timedelta(days=int(days))).isoformat()
    async with p.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM users
            WHERE is_deleted=0
              AND COALESCE(NULLIF(trim(name), ''), NULL) IS NOT NULL
              AND COALESCE(is_blocked,0)=0
              AND created_at >= $1
            """,
            cutoff,
        )
        rows = await conn.fetch(
            """
            SELECT *
            FROM users
            WHERE is_deleted=0
              AND COALESCE(NULLIF(trim(name), ''), NULL) IS NOT NULL
              AND COALESCE(is_blocked,0)=0
              AND created_at >= $1
            ORDER BY created_at DESC, id DESC
            OFFSET $2 LIMIT $3
            """,
            cutoff,
            int(offset or 0),
            int(limit),
        )
    return int(total or 0), [dict(r) for r in rows]


async def get_blocked_users_page(limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
    """(total, users_page) for blocked users pagination."""
    p = _assert_pool()
    async with p.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE is_deleted=0 AND is_blocked=1"
        )
        rows = await conn.fetch(
            """
            SELECT *
            FROM users
            WHERE is_deleted=0 AND is_blocked=1
            ORDER BY updated_at DESC NULLS LAST, id DESC
            OFFSET $1 LIMIT $2
            """,
            int(offset),
            int(limit),
        )
    return int(total or 0), [dict(r) for r in rows]

async def get_exited_users_page(limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
    """(total, users_page) for users who left (deleted or blocked)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM users
            WHERE COALESCE(is_deleted,0)=1 OR COALESCE(is_blocked,0)=1
            """
        )
        rows = await conn.fetch(
            """
            SELECT *
            FROM users
            WHERE COALESCE(is_deleted,0)=1 OR COALESCE(is_blocked,0)=1
            ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
            OFFSET $1 LIMIT $2
            """,
            int(offset or 0),
            int(limit),
        )
    return int(total or 0), [dict(r) for r in rows]


# --- winners / top-3 ---


async def get_photo_admin_stats(photo_id: int) -> dict:
    """Подробная статистика по одному фото для админки."""
    p = _assert_pool()
    pid = int(photo_id)

    async with p.acquire() as conn:
        w = _link_rating_weight()
        row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(value * CASE WHEN source='link' THEN $2 ELSE 1 END), 0)::float AS sum_w,
                COALESCE(SUM(CASE WHEN source='link' THEN $2 ELSE 1 END), 0)::float AS cnt_w,
                COUNT(*)::int AS cnt_raw,
                AVG(value)::float AS avg_raw
            FROM ratings
            WHERE photo_id = $1
            """,
            pid,
            float(w),
        )
        sum_w = float(row["sum_w"]) if row and row["sum_w"] is not None else 0.0
        cnt_w = float(row["cnt_w"]) if row and row["cnt_w"] is not None else 0.0
        avg_raw = float(row["avg_raw"]) if row and row["avg_raw"] is not None else None
        avg_rating = (sum_w / cnt_w) if cnt_w > 0 else avg_raw
        ratings_count = int(row["cnt_raw"] or 0) if row else 0

        super_ratings_count = int(
            (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM super_ratings WHERE photo_id = $1",
                    pid,
                )
            )
            or 0
        )

        comments_count = int(
            (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM comments WHERE photo_id=$1",
                    pid,
                )
            ) or 0
        )

    return {
        "avg_rating": avg_rating,
        "ratings_count": ratings_count,
        "super_ratings_count": super_ratings_count,
        "comments_count": comments_count,
    }


async def ensure_user_minimal_row(tg_id: int, username: str | None = None) -> dict | None:
    return await _ensure_user_row(int(tg_id), username=username)


# -------------------- Notifications settings (likes/comments) --------------------

async def _ensure_notify_tables(conn) -> None:
    """Create notification tables if they don't exist (safe for Postgres)."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_notify_settings (
            tg_id BIGINT PRIMARY KEY,
            likes_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            comments_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notify_likes_daily (
            tg_id BIGINT NOT NULL,
            day_key TEXT NOT NULL,
            likes_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tg_id, day_key)
        );
        """
    )


async def get_notify_settings_by_tg_id(tg_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_notify_tables(conn)
        await conn.execute(
            """
            INSERT INTO user_notify_settings (tg_id)
            VALUES ($1)
            ON CONFLICT (tg_id) DO NOTHING
            """,
            int(tg_id),
        )
        row = await conn.fetchrow(
            "SELECT tg_id, likes_enabled, comments_enabled FROM user_notify_settings WHERE tg_id=$1",
            int(tg_id),
        )
        if not row:
            return {"tg_id": int(tg_id), "likes_enabled": True, "comments_enabled": True}
        return dict(row)


async def toggle_likes_notify_by_tg_id(tg_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_notify_tables(conn)
        await conn.execute(
            """
            INSERT INTO user_notify_settings (tg_id)
            VALUES ($1)
            ON CONFLICT (tg_id) DO NOTHING
            """,
            int(tg_id),
        )
        await conn.execute(
            """
            UPDATE user_notify_settings
            SET likes_enabled = NOT likes_enabled,
                updated_at = NOW()
            WHERE tg_id=$1
            """,
            int(tg_id),
        )

    return await get_notify_settings_by_tg_id(int(tg_id))


async def toggle_comments_notify_by_tg_id(tg_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_notify_tables(conn)
        await conn.execute(
            """
            INSERT INTO user_notify_settings (tg_id)
            VALUES ($1)
            ON CONFLICT (tg_id) DO NOTHING
            """,
            int(tg_id),
        )
        await conn.execute(
            """
            UPDATE user_notify_settings
            SET comments_enabled = NOT comments_enabled,
                updated_at = NOW()
            WHERE tg_id=$1
            """,
            int(tg_id),
        )

    return await get_notify_settings_by_tg_id(int(tg_id))


async def increment_likes_daily_for_tg_id(tg_id: int, day_key: str, delta: int = 1) -> None:
    """Accumulate likes for daily summary notifications."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_notify_tables(conn)
        await conn.execute(
            """
            INSERT INTO notify_likes_daily (tg_id, day_key, likes_count)
            VALUES ($1,$2,$3)
            ON CONFLICT (tg_id, day_key)
            DO UPDATE SET likes_count = notify_likes_daily.likes_count + EXCLUDED.likes_count,
                          updated_at = NOW()
            """,
            int(tg_id),
            str(day_key),
            int(delta),
        )
