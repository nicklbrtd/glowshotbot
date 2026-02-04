from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timedelta

import asyncpg
from asyncpg.exceptions import UniqueViolationError

from utils.time import get_moscow_now, get_moscow_today, get_moscow_now_iso
from utils.watermark import generate_author_code

import traceback

DB_DSN = os.getenv("DATABASE_URL")
pool: asyncpg.Pool | None = None

# Cache global rating mean so we don't query it on every profile view.
# Stored as (ts, mean, count)
_GLOBAL_RATING_CACHE: tuple[float, float, int] | None = None
_GLOBAL_RATING_TTL_SECONDS = 300
LINK_RATING_WEIGHT = float(os.getenv("LINK_RATING_WEIGHT", "0.5"))


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

    Tunable constant. 20 works well for 1..10 ratings.
    """
    return 20


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

    # If the bot is new / no ratings yet, use a neutral default.
    mean = (sum_w / cnt_w) if cnt_w > 0 else 7.0

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
            rate_tutorial_seen BOOLEAN NOT NULL DEFAULT FALSE,
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
        "ALTER TABLE user_ui_state ADD COLUMN IF NOT EXISTS rate_tutorial_seen BOOLEAN NOT NULL DEFAULT FALSE;"
    )


# -------------------- App settings (tech mode) --------------------

async def _ensure_app_settings_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER PRIMARY KEY,
            tech_enabled INTEGER NOT NULL DEFAULT 0,
            tech_start_at TEXT,
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
            SELECT menu_msg_id, rate_kb_msg_id, screen_msg_id, banner_msg_id, rate_tutorial_seen
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
                "rate_tutorial_seen": False,
            }
        return dict(row)


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

# -------------------- Tech mode settings --------------------

async def get_tech_mode_state() -> dict:
    """Return tech mode state: {tech_enabled: bool, tech_start_at: str | None}."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_app_settings_table(conn)
        row = await conn.fetchrow(
            "SELECT tech_enabled, tech_start_at FROM app_settings WHERE id=1"
        )
        if not row:
            return {"tech_enabled": False, "tech_start_at": None}
        return {
            "tech_enabled": bool(row.get("tech_enabled")),
            "tech_start_at": row.get("tech_start_at"),
        }


async def set_tech_mode_state(*, enabled: bool, start_at: str | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await _ensure_app_settings_table(conn)
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

_STREAK_DAILY_RATINGS = int(os.getenv("STREAK_DAILY_RATINGS", "3"))
_STREAK_DAILY_COMMENTS = int(os.getenv("STREAK_DAILY_COMMENTS", "1"))
_STREAK_DAILY_UPLOADS = int(os.getenv("STREAK_DAILY_UPLOADS", "1"))
_STREAK_GRACE_HOURS = int(os.getenv("STREAK_GRACE_HOURS", "6"))
_STREAK_MAX_NUDGES_PER_DAY = int(os.getenv("STREAK_MAX_NUDGES_PER_DAY", "2"))
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
    """How many photos the user uploaded today (Moscow day_key)."""
    p = _assert_pool()
    day_key = _today_key()
    where_deleted = "" if include_deleted else "AND is_deleted=0"
    async with p.acquire() as conn:
        v = await conn.fetchval(
            f"SELECT COUNT(*) FROM photos WHERE user_id=$1 AND day_key=$2 {where_deleted}",
            int(user_id),
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
    """Создаёт таблицы под текущие хендлеры (если их нет)."""
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
              moderation_status TEXT NOT NULL DEFAULT 'active',
              ratings_enabled INTEGER NOT NULL DEFAULT 1,
              is_deleted INTEGER NOT NULL DEFAULT 0,
              deleted_at TEXT,
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
    until = u.get("premium_until")
    if not until:
        return True
    try:
        return datetime.fromisoformat(until) > get_moscow_now()
    except Exception:
        return True


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


async def get_referral_stats_for_user(user_tg_id: int) -> dict:
    u = await get_user_by_tg_id(user_tg_id)
    if not u:
        return {"invited_qualified": 0}
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM referrals WHERE inviter_user_id=$1 AND qualified=1",
                                int(u["id"]))
    return {"invited_qualified": int(v or 0)}


async def link_and_reward_referral_if_needed(invited_tg_id: int):
    p = _assert_pool()

    async with p.acquire() as conn:
        invited = await conn.fetchrow(
            "SELECT id FROM users WHERE tg_id=$1 AND is_deleted=0",
            int(invited_tg_id),
        )
        if not invited:
            return False, None, None

        invited_user_id = int(invited["id"])

        exists = await conn.fetchval(
            "SELECT 1 FROM referrals WHERE invited_user_id=$1",
            invited_user_id
        )
        if exists:
            return False, None, None

        pending = await conn.fetchrow(
            "SELECT referral_code FROM pending_referrals WHERE new_user_tg_id=$1",
            int(invited_tg_id),
        )
        if not pending:
            return False, None, None

        inviter = await conn.fetchrow(
            "SELECT id, tg_id FROM users WHERE referral_code=$1 AND is_deleted=0",
            pending["referral_code"]
        )
        if not inviter:
            return False, None, None

        inviter_user_id = int(inviter["id"])
        inviter_tg_id = int(inviter["tg_id"])
        now = get_moscow_now_iso()

        await conn.execute(
            """
            INSERT INTO referrals (inviter_user_id, invited_user_id, qualified, qualified_at, created_at)
            VALUES ($1, $2, 1, $3, $3)
            """,
            inviter_user_id,
            invited_user_id,
            now
        )

        await conn.execute(
            "DELETE FROM pending_referrals WHERE new_user_tg_id=$1",
            int(invited_tg_id)
        )

        await _add_premium_days(conn, inviter_user_id, days=1)
        await _add_premium_days(conn, invited_user_id, days=1)

        return True, inviter_tg_id, invited_tg_id

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
    # В разных БД колонка created_at может быть TEXT или TIMESTAMP.
    # Передаём datetime, чтобы подошло под TIMESTAMP, а в TEXT каст прошло автоматически.
    now = get_moscow_now()

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
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            int(chat_id) if chat_id is not None else None,
            int(tg_user_id) if tg_user_id is not None else None,
            _cut(handler, 200),
            _cut(update_type, 100),
            _cut(error_type, 200),
            _cut(error_text, 2000),
            _cut(traceback_text, 20000),
            now,
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
    """Создать новую фотографию пользователя на текущий день (по Москве).
    Никакого авто-удаления. day_key — только ключ дня для лимитов/итогов.
    """
    p = _assert_pool()
    now_iso = get_moscow_now_iso()
    day_key = _today_key()

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
                moderation_status,
                ratings_enabled,
                is_deleted,
                created_at
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'active',$11,0,$12)
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
            1 if enabled else 0,
            now_iso,
        )

    return int(row["id"]) if row else 0


async def get_today_photo_for_user(user_id: int) -> dict | None:
    items = await get_today_photos_for_user(user_id, limit=1)
    return items[0] if items else None


async def get_today_photos_for_user(user_id: int, limit: int = 50) -> list[dict]:
    p = _assert_pool()
    day_key = _today_key()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
            FROM photos
            WHERE user_id=$1
              AND day_key=$2
              AND is_deleted=0
            ORDER BY created_at DESC, id DESC
            LIMIT $3
            """,
            int(user_id),
            day_key,
            int(limit),
        )
    return [dict(r) for r in rows]


async def get_active_photos_for_user(user_id: int, limit: int = 50) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM photos
            WHERE user_id=$1 AND is_deleted=0
            ORDER BY created_at DESC, id DESC
            LIMIT $2
            """,
            int(user_id), int(limit)
        )
    return [dict(r) for r in rows]


async def get_latest_photos_for_user(user_id: int, limit: int = 2) -> list[dict]:
    return await get_active_photos_for_user(user_id, limit=limit)


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
        await conn.execute("UPDATE photos SET is_deleted=1, deleted_at=$1 WHERE id=$2",
                           get_moscow_now_iso(), int(photo_id))


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

async def get_random_photo_for_rating_rateable(viewer_user_id: int) -> dict | None:
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
              AND COALESCE(p.ratings_enabled, 1)=1
              AND p.user_id <> $1
              AND (
                u.is_premium=1
                OR p.id IN (
                    SELECT id
                    FROM photos
                    WHERE user_id=u.id AND is_deleted=0 AND moderation_status IN ('active','good')
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                )
              )
              AND NOT EXISTS (SELECT 1 FROM ratings r WHERE r.photo_id=p.id AND r.user_id=$1)
            ORDER BY random()
            LIMIT 1
            """,
            int(viewer_user_id)
        )
    return dict(row) if row else None


async def get_random_photo_for_rating_viewonly(viewer_user_id: int) -> dict | None:
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
              AND COALESCE(p.ratings_enabled, 1)=0
              AND p.user_id <> $1
              AND (
                u.is_premium=1
                OR p.id IN (
                    SELECT id
                    FROM photos
                    WHERE user_id=u.id AND is_deleted=0 AND moderation_status IN ('active','good')
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                )
              )
              AND NOT EXISTS (SELECT 1 FROM ratings r WHERE r.photo_id=p.id AND r.user_id=$1)
              AND NOT EXISTS (
                  SELECT 1 FROM viewonly_views v
                  WHERE v.photo_id=p.id AND v.user_id=$1
              )
            ORDER BY random()
            LIMIT 1
            """,
            int(viewer_user_id)
        )
    return dict(row) if row else None


async def get_random_photo_for_rating(viewer_user_id: int) -> dict | None:
    """
    Возвращает либо обычное фото для оценивания, либо «передышку» (ratings_enabled=0).
    Вероятность передышки ~10–15% на попытку.
    """
    prob = random.uniform(0.10, 0.15)

    try_viewonly_first = random.random() < prob

    if try_viewonly_first:
        vo = await get_random_photo_for_rating_viewonly(viewer_user_id)
        if vo:
            return vo

    rated = await get_random_photo_for_rating_rateable(viewer_user_id)
    if rated:
        return rated

    # Если обычных нет — попробуем передышку вне зависимости от вероятности
    return await get_random_photo_for_rating_viewonly(viewer_user_id)


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


async def has_viewonly_seen(user_id: int, photo_id: int) -> bool:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval(
            "SELECT 1 FROM viewonly_views WHERE user_id=$1 AND photo_id=$2",
            int(user_id),
            int(photo_id),
        )
    return bool(v)


async def add_rating(user_id: int, photo_id: int, value: int) -> None:
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
            return

        await conn.execute(
            """
            INSERT INTO ratings (photo_id, user_id, value, created_at)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (photo_id, user_id)
            DO UPDATE SET value=EXCLUDED.value, created_at=EXCLUDED.created_at
            """,
            int(photo_id), int(user_id), int(value), now
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
        v = await conn.fetchval("SELECT COUNT(*) FROM photos WHERE user_id=$1 AND is_deleted=0", int(user_id))
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
    now_iso = get_moscow_now_iso()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO activity_events (user_id, kind, created_at)
            VALUES ($1, $2, $3)
            """,
            int(user["id"]),
            str(kind or "any"),
            now_iso,
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
        return value
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        # fallback: try parsing date-only
        try:
            return datetime.fromisoformat(str(value) + "T00:00:00")
        except Exception:
            raise


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
