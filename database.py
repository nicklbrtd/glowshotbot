from __future__ import annotations

import os
import random
from datetime import datetime, timedelta

import asyncpg

from utils.time import get_moscow_now, get_moscow_today, get_moscow_now_iso

import traceback

DB_DSN = os.getenv("DATABASE_URL")
pool: asyncpg.Pool | None = None


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

              is_admin INTEGER NOT NULL DEFAULT 0,
              is_moderator INTEGER NOT NULL DEFAULT 0,
              is_helper INTEGER NOT NULL DEFAULT 0,
              is_support INTEGER NOT NULL DEFAULT 0,

              is_premium INTEGER NOT NULL DEFAULT 0,
              premium_until TEXT,

              is_blocked INTEGER NOT NULL DEFAULT 0,
              block_reason TEXT,
              block_until TEXT,

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
              title TEXT,
              description TEXT,
              category TEXT DEFAULT 'photo',
              device_type TEXT,
              device_info TEXT,
              day_key TEXT,
              moderation_status TEXT NOT NULL DEFAULT 'active',
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
              status TEXT NOT NULL DEFAULT 'success',
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

        await conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_error_logs_created_at ON bot_error_logs(created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_error_logs_tg_user_id ON bot_error_logs(tg_user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_user_id ON photos(user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_day_key ON photos(day_key);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(moderation_status);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ratings_photo_id ON ratings(photo_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_photo_id ON comments(photo_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_photo_id ON photo_reports(photo_id);")


# -------------------- helpers --------------------

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

async def create_user(tg_id: int, username: str | None, name: str | None, gender: str | None,
                      age: int | None, bio: str | None) -> dict:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (tg_id, username, name, gender, age, bio, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$7)
            ON CONFLICT (tg_id) DO UPDATE
              SET username=EXCLUDED.username,
                  name=EXCLUDED.name,
                  gender=EXCLUDED.gender,
                  age=EXCLUDED.age,
                  bio=EXCLUDED.bio,
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


async def soft_delete_user(user_id: int) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE users SET is_deleted=1, updated_at=$1 WHERE id=$2",
                           get_moscow_now_iso(), int(user_id))


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

        await _add_premium_days(conn, inviter_user_id, days=2)
        await _add_premium_days(conn, invited_user_id, days=2)

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

# -------------------- user admin stats --------------------

# -------------------- comments --------------------

async def create_comment(user_id: int, photo_id: int, text: str, is_public: bool = True) -> None:
    """Create a comment.

    IMPORTANT:
    Some handlers may accidentally pass Telegram `tg_id` instead of internal `users.id`.
    This function resolves it to the internal id to avoid silent FK failures.

    Also logs any DB errors to bot_error_logs to make debugging possible.
    """

    p = _assert_pool()

    pid = int(photo_id)
    raw_uid = int(user_id)
    txt = (text or "").strip()
    if not txt:
        return

    try:
        async with p.acquire() as conn:
            # Ensure photo exists
            exists = await conn.fetchval("SELECT 1 FROM photos WHERE id=$1", pid)
            if not exists:
                raise ValueError(f"create_comment: photo {pid} not found")

            # Resolve internal users.id
            real_uid = await conn.fetchval("SELECT id FROM users WHERE id=$1 AND is_deleted=0", raw_uid)
            if real_uid is None:
                # maybe raw_uid is tg_id
                real_uid = await conn.fetchval("SELECT id FROM users WHERE tg_id=$1 AND is_deleted=0", raw_uid)

            # If still missing and it looks like tg_id, ensure user row exists
            if real_uid is None and raw_uid >= 10_000_000:
                u = await _ensure_user_row(raw_uid)
                if u is not None:
                    real_uid = int(u["id"])

            if real_uid is None:
                raise ValueError(f"create_comment: user {raw_uid} not found")

            await conn.execute(
                """
                INSERT INTO comments (photo_id, user_id, text, is_public, created_at)
                VALUES ($1,$2,$3,$4,$5)
                """,
                pid,
                int(real_uid),
                txt,
                1 if is_public else 0,
                get_moscow_now_iso(),
            )
    except Exception as e:
        # Log into bot_error_logs so you can see the exact failure reason
        try:
            await log_bot_error(
                chat_id=None,
                tg_user_id=raw_uid if raw_uid >= 10_000_000 else None,
                handler="create_comment",
                update_type="db",
                error_type=type(e).__name__,
                error_text=str(e),
                traceback_text=traceback.format_exc(),
            )
        except Exception:
            pass
        raise

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
    now = get_moscow_now_iso()

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


async def clear_bot_error_logs() -> None:
    """Полностью очищает таблицу bot_error_logs (для админки)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM bot_error_logs")

async def log_successful_payment(tg_id: int, provider: str = "unknown",
                                 amount_rub: int | None = None, amount_stars: int | None = None,
                                 period_code: str | None = None, inv_id: str | None = None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO payments (tg_id, provider, amount_rub, amount_stars, period_code, inv_id, status, created_at)
            VALUES ($1,$2,$3,$4,$5,$6,'success',$7)
            """,
            int(tg_id), str(provider), amount_rub, amount_stars, period_code, inv_id, get_moscow_now_iso()
        )


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
    title: str | None = None,
    description: str | None = None,
    category: str | None = None,
    device_type: str | None = None,
    device_info: str | None = None,
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
        row = await conn.fetchrow(
            """
            INSERT INTO photos (
                user_id,
                file_id,
                title,
                description,
                category,
                device_type,
                device_info,
                day_key,
                moderation_status,
                is_deleted,
                created_at
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'active',0,$9)
            RETURNING id
            """,
            int(user_id),
            str(file_id),
            title,
            description,
            category,
            device_type,
            device_info,
            day_key,
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


async def get_photo_stats(photo_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS ratings_count, AVG(value)::double precision AS avg_rating
            FROM ratings WHERE photo_id=$1
            """,
            int(photo_id)
        )
        comments_count = await conn.fetchval("SELECT COUNT(*) FROM comments WHERE photo_id=$1", int(photo_id))
    cnt = int((row["ratings_count"] if row else 0) or 0)
    avg = row["avg_rating"] if row else None
    if avg is not None:
        try:
            avg = float(avg)
        except Exception:
            avg = None
    return {"ratings_count": cnt, "avg_rating": avg, "comments_count": int(comments_count or 0)}


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

async def get_random_photo_for_rating(viewer_user_id: int) -> dict | None:
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
              AND p.moderation_status IN ('active')
              AND p.user_id <> $1
              AND NOT EXISTS (SELECT 1 FROM ratings r WHERE r.photo_id=p.id AND r.user_id=$1)
            ORDER BY random()
            LIMIT 1
            """,
            int(viewer_user_id)
        )
    return dict(row) if row else None


async def add_rating(user_id: int, photo_id: int, value: int) -> None:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ratings (photo_id, user_id, value, created_at)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (photo_id, user_id)
            DO UPDATE SET value=EXCLUDED.value, created_at=EXCLUDED.created_at
            """,
            int(photo_id), int(user_id), int(value), now
        )


async def set_super_rating(user_id: int, photo_id: int) -> bool:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
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


async def create_comment(user_id: int, photo_id: int, text: str) -> None:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        await conn.execute(
            "INSERT INTO comments (photo_id, user_id, text, created_at) VALUES ($1,$2,$3,$4)",
            int(photo_id), int(user_id), str(text), now
        )

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


async def get_photo_report_stats(photo_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM photo_reports WHERE photo_id=$1", int(photo_id))
        pending = await conn.fetchval(
            "SELECT COUNT(*) FROM photo_reports WHERE photo_id=$1 AND status='pending'",
            int(photo_id)
        )
    return {"total": int(total or 0), "pending": int(pending or 0)}


async def set_photo_moderation_status(photo_id: int, status: str) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE photos SET moderation_status=$1 WHERE id=$2", str(status), int(photo_id))


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
        row = await conn.fetchrow(
            """
            SELECT * FROM photos
            WHERE is_deleted=0 AND moderation_status='active'
            ORDER BY random()
            LIMIT 1
            """
        )
    return dict(row) if row else None


# -------------------- results --------------------

async def get_daily_top_photos(day_key: str | None = None, limit: int = 4) -> list[dict]:
    if not day_key:
        day_key = get_moscow_today()
    p = _assert_pool()
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
            str(day_key), int(limit)
        )
    return [dict(r) for r in rows]


async def get_weekly_best_photo() -> dict | None:
    p = _assert_pool()
    now = get_moscow_now().date()
    start = (now - timedelta(days=7)).isoformat()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.*,
                   COUNT(r.id) AS ratings_count,
                   AVG(r.value)::double precision AS avg_rating
            FROM photos p
            JOIN ratings r ON r.photo_id=p.id
            WHERE p.is_deleted=0 AND p.moderation_status='active' AND p.day_key >= $1
            GROUP BY p.id
            ORDER BY AVG(r.value) DESC, COUNT(r.id) DESC
            LIMIT 1
            """,
            start
        )
    return dict(row) if row else None


# -------------------- weekly / repeat / my_results --------------------

async def add_weekly_candidate(photo_id: int) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO weekly_candidates (photo_id, created_at)
            VALUES ($1,$2)
            ON CONFLICT (photo_id) DO NOTHING
            """,
            int(photo_id), get_moscow_now_iso()
        )


async def is_photo_in_weekly(photo_id: int) -> bool:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT 1 FROM weekly_candidates WHERE photo_id=$1", int(photo_id))
    return bool(v)


async def get_weekly_photos_for_user(user_id: int) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.* FROM photos p
            JOIN weekly_candidates w ON w.photo_id=p.id
            WHERE p.user_id=$1 AND p.is_deleted=0
            ORDER BY w.created_at DESC, p.id DESC
            """,
            int(user_id)
        )
    return [dict(r) for r in rows]


async def is_photo_repeat_used(photo_id: int) -> bool:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT 1 FROM photo_repeats WHERE photo_id=$1", int(photo_id))
    return bool(v)


async def mark_photo_repeat_used(photo_id: int) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO photo_repeats (photo_id, used_at)
            VALUES ($1,$2)
            ON CONFLICT (photo_id) DO NOTHING
            """,
            int(photo_id), get_moscow_now_iso()
        )


async def archive_photo_to_my_results(user_id: int, photo_id: int, kind: str,
                                     day_key: str | None = None, place: int | None = None) -> None:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        photo = await conn.fetchrow("SELECT * FROM photos WHERE id=$1", int(photo_id))
        if not photo:
            return
        stat = await conn.fetchrow(
            "SELECT COUNT(*) AS ratings_count, AVG(value)::double precision AS avg_rating FROM ratings WHERE photo_id=$1",
            int(photo_id)
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
            int(user_id), int(photo_id), str(photo["file_id"]), photo.get("title"),
            day_key or photo.get("day_key"), str(kind), place, avg, ratings_count, now
        )


async def get_my_results_for_user(user_id: int) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM my_results WHERE user_id=$1 ORDER BY created_at DESC, id DESC",
            int(user_id)
        )
    return [dict(r) for r in rows]


# -------------------- profile summary --------------------

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
    p = _assert_pool()
    async with p.acquire() as conn:
        given = await conn.fetchval("SELECT COUNT(*) FROM ratings WHERE user_id=$1", int(user_id))
        received = await conn.fetchval(
            """
            SELECT COUNT(r.id)
            FROM ratings r
            JOIN photos p ON p.id=r.photo_id
            WHERE p.user_id=$1
            """,
            int(user_id)
        )
        avg_received = await conn.fetchval(
            """
            SELECT AVG(r.value)::double precision
            FROM ratings r
            JOIN photos p ON p.id=r.photo_id
            WHERE p.user_id=$1
            """,
            int(user_id)
        )
    avg = None
    if avg_received is not None:
        try:
            avg = float(avg_received)
        except Exception:
            avg = None
    return {"ratings_given": int(given or 0), "ratings_received": int(received or 0), "avg_received": avg}


async def get_most_popular_photo_for_user(user_id: int) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.*, COUNT(r.id) AS ratings_count, AVG(r.value)::double precision AS avg_rating
            FROM photos p
            LEFT JOIN ratings r ON r.photo_id=p.id
            WHERE p.user_id=$1 AND p.is_deleted=0
            GROUP BY p.id
            ORDER BY COUNT(r.id) DESC, AVG(r.value) DESC NULLS LAST, p.id DESC
            LIMIT 1
            """,
            int(user_id)
        )
    return dict(row) if row else None


async def get_weekly_rank_for_user(user_id: int) -> int | None:
    p = _assert_pool()
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
            start
        )
    rank = 1
    for r in rows:
        if int(r["user_id"]) == int(user_id):
            return rank
        rank += 1
    return None


# ========== ADMIN & STATS ==========

# --- basic user lists / roles ---


async def get_total_users() -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        v = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_deleted=0")
    return int(v or 0)


async def get_all_users_tg_ids() -> list[int]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("SELECT tg_id FROM users WHERE is_deleted=0")
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


async def get_premium_users() -> list[dict]:
    """Full rows for all premium users (for admin lists)."""
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
            FROM users
            WHERE is_premium=1 AND is_deleted=0
            ORDER BY premium_until DESC NULLS LAST, id DESC
            """
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
    where = "WHERE is_deleted=0" if only_active else ""
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


async def get_active_users_last_24h(limit: int = 20) -> tuple[int, list[dict]]:
    """(total, sample) of users with any activity in last 24h."""
    p = _assert_pool()
    since_iso = (get_moscow_now() - timedelta(hours=24)).isoformat()
    async with p.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT user_id)
            FROM activity_events
            WHERE user_id IS NOT NULL AND created_at >= $1
            """,
            since_iso,
        )
        rows = await conn.fetch(
            """
            SELECT u.*
            FROM users u
            JOIN (
              SELECT DISTINCT user_id
              FROM activity_events
              WHERE user_id IS NOT NULL AND created_at >= $1
            ) a ON a.user_id = u.id
            WHERE u.is_deleted=0
            ORDER BY u.updated_at DESC NULLS LAST, u.created_at DESC, u.id DESC
            LIMIT $2
            """,
            since_iso,
            int(limit),
        )
    return int(total or 0), [dict(r) for r in rows]


async def get_online_users_recent(window_minutes: int = 5, limit: int = 20) -> tuple[int, list[dict]]:
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
            SELECT COUNT(DISTINCT user_id)
            FROM activity_events
            WHERE user_id IS NOT NULL AND created_at >= $1
            """,
            since_iso,
        )
        rows = await conn.fetch(
            """
            SELECT u.*
            FROM users u
            JOIN (
              SELECT DISTINCT user_id
              FROM activity_events
              WHERE user_id IS NOT NULL AND created_at >= $1
            ) a ON a.user_id = u.id
            WHERE u.is_deleted=0
            ORDER BY u.updated_at DESC NULLS LAST, u.created_at DESC, u.id DESC
            LIMIT $2
            """,
            since_iso,
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
            "SELECT COUNT(*) FROM users WHERE is_deleted=0 AND is_premium=1"
        )
        active = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM users
            WHERE is_deleted=0 AND is_premium=1
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
            WHERE is_deleted=0 AND is_premium=1
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


async def get_new_users_last_days(days: int = 3, limit: int = 20) -> tuple[int, list[dict]]:
    """(total, sample) for users created within last N days."""
    p = _assert_pool()
    cutoff = (get_moscow_now() - timedelta(days=int(days))).isoformat()
    async with p.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE is_deleted=0 AND created_at >= $1",
            cutoff,
        )
        rows = await conn.fetch(
            """
            SELECT *
            FROM users
            WHERE is_deleted=0 AND created_at >= $1
            ORDER BY created_at DESC, id DESC
            LIMIT $2
            """,
            cutoff,
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


# --- winners / top-3 ---


async def get_users_with_multiple_daily_top3(min_wins: int = 2, limit: int = 50) -> list[dict]:
    """
    Admin helper: users who hit daily top-3 multiple times.
    Uses my_results as source.
    """
    p = _assert_pool()
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


async def get_photo_admin_stats(photo_id: int) -> dict:
    """Подробная статистика по одному фото для админки."""
    p = _assert_pool()
    pid = int(photo_id)

    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT AVG(value) AS avg, COUNT(*) AS cnt
            FROM ratings
            WHERE photo_id = $1
            """,
            pid,
        )
        avg_rating = float(row["avg"]) if row and row["avg"] is not None else None
        ratings_count = int(row["cnt"] or 0) if row else 0

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

