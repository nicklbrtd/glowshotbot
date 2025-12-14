from __future__ import annotations

import os
import random
from datetime import datetime, timedelta

import asyncpg

from utils.time import get_moscow_now, get_moscow_today, get_moscow_now_iso

DB_DSN = os.getenv("DATABASE_URL")

pool: asyncpg.Pool | None = None


async def init_db() -> None:
    """Initialize asyncpg pool. Must be called once on startup."""
    global pool
    if not DB_DSN:
        raise RuntimeError("DATABASE_URL is not set")
    if pool is None:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=10)


async def close_db() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None


def _assert_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("DB pool is not initialized. Call init_db() at startup.")
    return pool


# =========================================================
# AWARDS / ACHIEVEMENTS
# =========================================================

async def give_achievement_to_user_by_code(
    user_tg_id: int,
    code: str,
    granted_by_tg_id: int | None = None,
) -> bool:
    p = _assert_pool()

    async with p.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id FROM users WHERE tg_id = $1 AND is_deleted = 0",
            user_tg_id,
        )
        if not user_row:
            return False
        user_id = int(user_row["id"])

        existing = await conn.fetchrow(
            "SELECT id FROM awards WHERE user_id = $1 AND code = $2 LIMIT 1",
            user_id,
            code,
        )
        if existing:
            return False

        granted_by_user_id: int | None = None
        if granted_by_tg_id is not None:
            gb = await conn.fetchrow(
                "SELECT id FROM users WHERE tg_id = $1 AND is_deleted = 0",
                granted_by_tg_id,
            )
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

        now_iso = datetime.utcnow().isoformat(timespec="seconds")

        await conn.execute(
            """
            INSERT INTO awards (
                user_id, code, title, description, icon,
                is_special, granted_by_user_id, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            user_id, code, title, description, icon,
            is_special, granted_by_user_id, now_iso,
        )

    return True


async def get_awards_for_user(user_id: int) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
            FROM awards
            WHERE user_id = $1
            ORDER BY created_at DESC, id DESC
            """,
            int(user_id),
        )
    return [dict(r) for r in rows]


async def get_award_by_id(award_id: int) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM awards WHERE id = $1",
            int(award_id),
        )
    return dict(row) if row else None


async def delete_award_by_id(award_id: int) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "DELETE FROM awards WHERE id = $1",
            int(award_id),
        )


async def update_award_text(award_id: int, title: str, description: str | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            UPDATE awards
            SET title = $1, description = $2
            WHERE id = $3
            """,
            title, description, int(award_id),
        )


async def update_award_icon(award_id: int, icon: str | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            UPDATE awards
            SET icon = $1
            WHERE id = $2
            """,
            icon, int(award_id),
        )


async def create_custom_award_for_user(
    user_id: int,
    title: str,
    description: str | None,
    icon: str | None,
    code: str | None = None,
    is_special: bool = False,
    granted_by_user_id: int | None = None,
) -> int:
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    if code is None:
        ts = int(datetime.utcnow().timestamp())
        code = f"custom_{user_id}_{ts}"

    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO awards (
                user_id, code, title, description, icon,
                is_special, granted_by_user_id, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
            """,
            int(user_id), code, title, description, icon,
            1 if is_special else 0, granted_by_user_id, now_iso,
        )
        return int(row["id"])


# =========================================================
# ADMIN STATS
# =========================================================

async def get_user_admin_stats(user_id: int) -> dict:
    p = _assert_pool()
    uid = int(user_id)

    async with p.acquire() as conn:
        ratings_given = int((await conn.fetchval("SELECT COUNT(*) FROM ratings WHERE user_id = $1", uid)) or 0)
        comments_given = int((await conn.fetchval("SELECT COUNT(*) FROM comments WHERE user_id = $1", uid)) or 0)
        reports_created = int((await conn.fetchval("SELECT COUNT(*) FROM photo_reports WHERE user_id = $1", uid)) or 0)

        row = await conn.fetchrow(
            """
            SELECT
                SUM(CASE WHEN is_deleted = 0 THEN 1 ELSE 0 END) AS active_count,
                COUNT(*) AS total_count
            FROM photos
            WHERE user_id = $1
            """,
            uid,
        )
        active_photos = int((row["active_count"] or 0) if row else 0)
        total_photos = int((row["total_count"] or 0) if row else 0)

        upload_bans_count = int((await conn.fetchval("SELECT COUNT(*) FROM user_upload_bans WHERE user_id = $1", uid)) or 0)

    messages_total = int(ratings_given + comments_given + reports_created)
    return {
        "messages_total": messages_total,
        "ratings_given": ratings_given,
        "comments_given": comments_given,
        "reports_created": reports_created,
        "active_photos": active_photos,
        "total_photos": total_photos,
        "upload_bans_count": upload_bans_count,
    }


async def get_photo_admin_stats(photo_id: int) -> dict:
    p = _assert_pool()
    pid = int(photo_id)

    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT AVG(value) AS avg, COUNT(*) AS cnt FROM ratings WHERE photo_id = $1", pid)
        avg_rating = float(row["avg"]) if row and row["avg"] is not None else None
        ratings_count = int(row["cnt"] or 0) if row else 0

        super_ratings_count = int((await conn.fetchval("SELECT COUNT(*) FROM super_ratings WHERE photo_id = $1", pid)) or 0)
        comments_count = int((await conn.fetchval("SELECT COUNT(*) FROM comments WHERE photo_id = $1", pid)) or 0)

        reports_total = int((await conn.fetchval("SELECT COUNT(*) FROM photo_reports WHERE photo_id = $1", pid)) or 0)
        reports_pending = int((await conn.fetchval("SELECT COUNT(*) FROM photo_reports WHERE photo_id = $1 AND status = 'pending'", pid)) or 0)
        reports_resolved = int((await conn.fetchval("SELECT COUNT(*) FROM photo_reports WHERE photo_id = $1 AND status = 'resolved'", pid)) or 0)

    return {
        "avg_rating": avg_rating,
        "ratings_count": ratings_count,
        "super_ratings_count": super_ratings_count,
        "comments_count": comments_count,
        "reports_total": reports_total,
        "reports_pending": reports_pending,
        "reports_resolved": reports_resolved,
    }


# =========================================================
# PAYMENTS
# =========================================================

async def log_successful_payment(
    tg_id: int,
    method: str,
    period_code: str,
    days: int,
    amount: int,
    currency: str,
    telegram_charge_id: str | None = None,
    provider_charge_id: str | None = None,
) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id FROM users WHERE tg_id = $1 AND is_deleted = 0",
            tg_id,
        )
        if not user_row:
            return

        user_id = int(user_row["id"])
        now_iso = datetime.utcnow().isoformat(timespec="seconds")

        await conn.execute(
            """
            INSERT INTO payments (
                user_id, method, period_code, days, amount, currency,
                created_at, telegram_charge_id, provider_charge_id
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            user_id, method, period_code, int(days), int(amount), currency,
            now_iso, telegram_charge_id, provider_charge_id,
        )


async def get_payments_count() -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        return int((await conn.fetchval("SELECT COUNT(*) FROM payments")) or 0)


async def get_payments_page(page: int, page_size: int = 20) -> list[dict]:
    if page < 1:
        page = 1
    offset = (page - 1) * page_size

    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                p.*,
                u.tg_id AS user_tg_id,
                u.username AS user_username,
                u.name AS user_name
            FROM payments p
            JOIN users u ON u.id = p.user_id
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT $1 OFFSET $2
            """,
            int(page_size), int(offset),
        )
    return [dict(r) for r in rows]


async def get_revenue_summary(period: str) -> dict:
    now = datetime.utcnow()
    if period == "day":
        delta_days = 1
    elif period == "week":
        delta_days = 7
    else:
        delta_days = 30

    start_dt = now - timedelta(days=delta_days)
    start_iso = start_dt.isoformat(timespec="seconds")
    end_iso = now.isoformat(timespec="seconds")

    rub_total_minor = 0
    rub_count = 0
    stars_total = 0
    stars_count = 0

    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT currency, SUM(amount) AS total_amount, COUNT(*) AS cnt
            FROM payments
            WHERE created_at >= $1
            GROUP BY currency
            """,
            start_iso,
        )

    for row in rows or []:
        currency = row["currency"]
        total_amount = int(row["total_amount"] or 0)
        cnt = int(row["cnt"] or 0)
        if currency == "RUB":
            rub_total_minor = total_amount
            rub_count = cnt
        elif currency == "XTR":
            stars_total = total_amount
            stars_count = cnt

    rub_total = rub_total_minor / 100.0 if rub_total_minor else 0.0

    return {
        "period": period,
        "from": start_iso,
        "to": end_iso,
        "rub_total": rub_total,
        "rub_count": rub_count,
        "stars_total": stars_total,
        "stars_count": stars_count,
    }


async def get_subscriptions_total() -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        return int((await conn.fetchval("SELECT COUNT(DISTINCT user_id) FROM payments")) or 0)


async def get_subscriptions_page(page: int, page_size: int = 20) -> list[dict]:
    if page < 1:
        page = 1
    offset = (page - 1) * page_size

    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                u.id AS user_id,
                u.tg_id AS user_tg_id,
                u.username AS user_username,
                u.name AS user_name,
                MAX(p.created_at) AS last_payment_at,
                COUNT(*) AS payments_count,
                SUM(p.days) AS total_days,
                SUM(CASE WHEN p.currency = 'RUB' THEN p.amount ELSE 0 END) AS total_rub_minor,
                SUM(CASE WHEN p.currency = 'XTR' THEN p.amount ELSE 0 END) AS total_stars
            FROM payments p
            JOIN users u ON u.id = p.user_id
            GROUP BY u.id
            ORDER BY last_payment_at DESC
            LIMIT $1 OFFSET $2
            """,
            int(page_size), int(offset),
        )

    result: list[dict] = []
    for r in rows or []:
        d = dict(r)
        rub_minor = int(d.get("total_rub_minor") or 0)
        d["total_rub"] = rub_minor / 100.0 if rub_minor else 0.0
        result.append(d)
    return result


# =========================================================
# REFERRALS
# =========================================================

async def _generate_unique_referral_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    p = _assert_pool()

    while True:
        code = "GS" + "".join(random.choice(alphabet) for _ in range(6))
        async with p.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM users WHERE referral_code = $1 LIMIT 1",
                code,
            )
        if not exists:
            return code


async def get_or_create_referral_code(tg_id: int) -> str | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, referral_code FROM users WHERE tg_id = $1 AND is_deleted = 0",
            tg_id,
        )
        if not row:
            return None

        existing = row["referral_code"]
        if existing:
            return str(existing)

        user_id = int(row["id"])
        new_code = await _generate_unique_referral_code()
        await conn.execute(
            "UPDATE users SET referral_code = $1 WHERE id = $2",
            new_code, user_id,
        )
        return new_code


async def get_referral_stats_for_user(tg_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM users WHERE tg_id = $1 AND is_deleted = 0",
            tg_id,
        )
        if not row:
            return {"invited_total": 0, "invited_qualified": 0}

        user_id = int(row["id"])
        invited_total = int((await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE referred_by_user_id = $1",
            user_id,
        )) or 0)

        invited_qualified = int((await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE referred_by_user_id = $1 AND referral_qualified = 1",
            user_id,
        )) or 0)

    return {
        "invited_total": invited_total,
        "invited_qualified": invited_qualified,
    }


# =========================================================
# PREMIUM & NOTIFY
# =========================================================

async def set_user_premium_status(
    tg_id: int,
    is_premium: bool,
    premium_until: str | None = None,
) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_premium = $1, premium_until = $2 WHERE tg_id = $3",
            1 if is_premium else 0,
            premium_until,
            tg_id,
        )


async def get_user_premium_status(tg_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_premium, premium_until FROM users WHERE tg_id = $1 AND is_deleted = 0",
            tg_id,
        )
    if not row:
        return {"is_premium": False, "premium_until": None}
    return {
        "is_premium": bool(row["is_premium"]),
        "premium_until": row["premium_until"],
    }


async def get_premium_users() -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM users WHERE is_premium = 1 ORDER BY created_at DESC"
        )
    return [dict(r) for r in rows]


async def set_user_premium_role_by_tg_id(tg_id: int, value: bool) -> None:
    if value:
        await set_user_premium_status(tg_id, True, premium_until=None)
    else:
        await set_user_premium_status(tg_id, False, premium_until=None)


async def is_user_premium_active(tg_id: int) -> bool:
    data = await get_user_premium_status(tg_id)
    if not data.get("is_premium"):
        return False

    premium_until = data.get("premium_until")
    if not premium_until:
        return True

    try:
        until_dt = datetime.fromisoformat(premium_until)
    except Exception:
        return False

    now = get_moscow_now()
    return now < until_dt


async def get_user_notify_settings(tg_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT notify_likes, notify_comments FROM users WHERE tg_id = $1 AND is_deleted = 0",
            tg_id,
        )
    if not row:
        return {"notify_likes": True, "notify_comments": True}
    return {
        "notify_likes": bool(row["notify_likes"]),
        "notify_comments": bool(row["notify_comments"]),
    }


async def set_user_notify_likes(tg_id: int, enabled: bool) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET notify_likes = $1, updated_at = $2
            WHERE tg_id = $3 AND is_deleted = 0
            """,
            1 if enabled else 0,
            now,
            tg_id,
        )


async def set_user_notify_comments(tg_id: int, enabled: bool) -> None:
    """Включить или выключить уведомления о комментариях."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET notify_comments = $1, updated_at = $2
            WHERE tg_id = $3 AND is_deleted = 0
            """,
            1 if enabled else 0,
            now,
            tg_id,
        )


async def get_all_users_tg_ids() -> list[int]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("SELECT tg_id FROM users WHERE is_deleted = 0")
    return [int(r["tg_id"]) for r in rows if r["tg_id"] is not None]


# =========================================================
# DAILY SKIP
# =========================================================

async def get_daily_skip_info(tg_id: int) -> tuple[str | None, int]:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT daily_skip_date, daily_skip_count
            FROM users
            WHERE tg_id = $1 AND is_deleted = 0
            """,
            tg_id,
        )
    if not row:
        return None, 0
    return row["daily_skip_date"], int(row["daily_skip_count"] or 0)


async def update_daily_skip_info(tg_id: int, date_str: str, count: int) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET daily_skip_date = $1,
                daily_skip_count = $2,
                updated_at = $3
            WHERE tg_id = $4 AND is_deleted = 0
            """,
            date_str,
            int(count),
            now,
            tg_id,
        )


# =========================================================
# USERS
# =========================================================

async def get_user_by_tg_id(tg_id: int) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE tg_id = $1 AND is_deleted = 0",
            tg_id,
        )
    return dict(row) if row else None


async def get_user_by_username(username: str) -> dict | None:
    if not username:
        return None
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE username = $1 AND is_deleted = 0",
            username,
        )
    return dict(row) if row else None


async def get_user_by_id(user_id: int) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1 AND is_deleted = 0",
            int(user_id),
        )
    return dict(row) if row else None


async def create_user(
    tg_id: int,
    username: str | None,
    name: str,
    gender: str,
    age: int | None,
    bio: str,
) -> int:
    now = datetime.utcnow().isoformat(timespec="seconds")
    p = _assert_pool()

    async with p.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM users WHERE tg_id = $1", tg_id)
        if existing:
            user_id = int(existing["id"])
            await conn.execute(
                """
                UPDATE users
                SET username = $1, name = $2, gender = $3, age = $4, bio = $5,
                    updated_at = $6, is_deleted = 0
                WHERE tg_id = $7
                """,
                username, name, gender, age, bio, now, tg_id,
            )
            return user_id

        row = await conn.fetchrow(
            """
            INSERT INTO users (tg_id, username, name, gender, age, bio, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            RETURNING id
            """,
            tg_id, username, name, gender, age, bio, now, now,
        )
        return int(row["id"])


async def update_user_name(tg_id: int, name: str) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET name = $1, updated_at = $2 WHERE tg_id = $3 AND is_deleted = 0",
            name, now, tg_id,
        )


async def update_user_gender(tg_id: int, gender: str) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET gender = $1, updated_at = $2 WHERE tg_id = $3 AND is_deleted = 0",
            gender, now, tg_id,
        )


async def update_user_age(tg_id: int, age: int | None) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET age = $1, updated_at = $2 WHERE tg_id = $3 AND is_deleted = 0",
            age, now, tg_id,
        )


async def update_user_bio(tg_id: int, bio: str) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET bio = $1, updated_at = $2 WHERE tg_id = $3 AND is_deleted = 0",
            bio, now, tg_id,
        )


async def update_user_channel_link(tg_id: int, link: str | None) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET tg_channel_link = $1 WHERE tg_id = $2",
            link, tg_id,
        )


async def soft_delete_user(tg_id: int) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_deleted = 1, updated_at = $1 WHERE tg_id = $2",
            now, tg_id,
        )


# =========================================================
# PHOTOS COUNTS
# =========================================================

async def count_photos_by_user(user_id: int) -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        cnt = await conn.fetchval("SELECT COUNT(*) FROM photos WHERE user_id = $1", int(user_id))
    return int(cnt or 0)


async def count_active_photos_by_user(user_id: int) -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        cnt = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM photos
            WHERE user_id = $1
              AND is_deleted = 0
              AND moderation_status = 'active'
            """,
            int(user_id),
        )
    return int(cnt or 0)


# =========================================================
# PHOTOS CRUD / STATS
# =========================================================

async def get_today_photo_for_user(user_id: int) -> dict | None:
    today = get_moscow_today()
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM photos
            WHERE user_id = $1
              AND day_key = $2
              AND is_deleted = 0
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            int(user_id),
            today,
        )
    return dict(row) if row else None


async def create_today_photo(
    user_id: int,
    file_id: str,
    title: str,
    device_type: str,
    device_info: str | None,
    category: str,
    description: str | None,
) -> int:
    now_iso = get_moscow_now_iso()
    day_key = get_moscow_today()
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO photos (
                user_id, file_id, title, device_type, device_info,
                category, description, created_at, day_key,
                is_deleted, repeat_used, moderation_status
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,0,0,'active')
            RETURNING id
            """,
            int(user_id),
            file_id,
            title,
            device_type,
            device_info,
            category,
            description,
            now_iso,
            day_key,
        )
    return int(row["id"])


async def mark_photo_deleted(photo_id: int) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE photos SET is_deleted = 1 WHERE id = $1",
            int(photo_id),
        )


async def get_photo_by_id(photo_id: int) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM photos WHERE id = $1",
            int(photo_id),
        )
    return dict(row) if row else None


async def get_photo_stats(photo_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                SUM(CASE WHEN r.value > 0 THEN 1 ELSE 0 END) AS ratings_count,
                CASE
                    WHEN SUM(
                        CASE
                            WHEN r.value > 0
                                THEN CASE WHEN sr.user_id IS NOT NULL THEN 2 ELSE 1 END
                            ELSE 0
                        END
                    ) = 0
                    THEN NULL
                    ELSE
                        1.0 * SUM(
                            CASE
                                WHEN r.value > 0
                                    THEN r.value * (CASE WHEN sr.user_id IS NOT NULL THEN 2 ELSE 1 END)
                                ELSE 0
                            END
                        )
                        / SUM(
                            CASE
                                WHEN r.value > 0
                                    THEN CASE WHEN sr.user_id IS NOT NULL THEN 2 ELSE 1 END
                                ELSE 0
                            END
                        )
                END AS avg_rating,
                SUM(CASE WHEN r.value = 0 THEN 1 ELSE 0 END) AS skips_count
            FROM ratings r
            LEFT JOIN super_ratings sr
                ON sr.photo_id = r.photo_id
               AND sr.user_id = r.user_id
            WHERE r.photo_id = $1
            """,
            int(photo_id),
        )

    if not row:
        return {"ratings_count": 0, "avg_rating": None, "skips_count": 0}

    return {
        "ratings_count": int(row["ratings_count"] or 0),
        "avg_rating": row["avg_rating"],
        "skips_count": int(row["skips_count"] or 0),
    }


# =========================================================
# COMMENTS
# =========================================================

async def create_comment(
    user_id: int,
    photo_id: int,
    text: str,
    is_public: bool,
) -> None:
    now = get_moscow_now_iso()
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO comments (user_id, photo_id, text, is_public, created_at)
            VALUES ($1,$2,$3,$4,$5)
            """,
            int(user_id),
            int(photo_id),
            text,
            1 if is_public else 0,
            now,
        )


async def get_comments_for_photo(photo_id: int, limit: int = 10) -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.*,
                u.name,
                u.username,
                r.value AS rating_value
            FROM comments c
            JOIN users u ON c.user_id = u.id
            LEFT JOIN ratings r
                ON r.user_id = c.user_id
               AND r.photo_id = c.photo_id
            WHERE c.photo_id = $1
            ORDER BY c.created_at DESC
            LIMIT $2
            """,
            int(photo_id),
            int(limit),
        )
    return [dict(r) for r in rows]


# =========================================================
# RATING FLOW
# =========================================================

async def get_random_photo_for_rating(rater_user_id: int) -> dict | None:
    day_key = get_moscow_today()
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                p.*,
                u.is_premium      AS user_is_premium,
                u.tg_channel_link AS user_tg_channel_link
            FROM photos p
            JOIN users u ON p.user_id = u.id
            WHERE p.is_deleted = 0
              AND p.moderation_status = 'active'
              AND u.is_deleted = 0
              AND u.id != $1
              AND p.day_key = $2
              AND NOT EXISTS (
                  SELECT 1 FROM ratings r
                  WHERE r.user_id = $3 AND r.photo_id = p.id
              )
            ORDER BY RANDOM()
            LIMIT 1
            """,
            int(rater_user_id),
            day_key,
            int(rater_user_id),
        )
    return dict(row) if row else None


async def add_rating(user_id: int, photo_id: int, value: int) -> None:
    now = get_moscow_now_iso()
    p = _assert_pool()

    async with p.acquire() as conn:
        await conn.execute(
            "DELETE FROM super_ratings WHERE user_id = $1 AND photo_id = $2",
            int(user_id), int(photo_id),
        )
        await conn.execute(
            """
            INSERT INTO ratings (user_id, photo_id, value, created_at)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (user_id, photo_id)
            DO UPDATE SET value = EXCLUDED.value, created_at = EXCLUDED.created_at
            """,
            int(user_id), int(photo_id), int(value), now,
        )


async def set_super_rating(user_id: int, photo_id: int) -> None:
    now = get_moscow_now_iso()
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO super_ratings (user_id, photo_id, created_at)
            VALUES ($1,$2,$3)
            ON CONFLICT (user_id, photo_id)
            DO UPDATE SET created_at = EXCLUDED.created_at
            """,
            int(user_id), int(photo_id), now,
        )


# =========================================================
# USER RATING SUMMARY / MOST POPULAR / WEEKLY RANK
# =========================================================

async def get_user_rating_summary(user_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                CASE
                    WHEN SUM(
                        CASE
                            WHEN r.value IS NOT NULL AND r.value != 0
                                THEN CASE WHEN sr.user_id IS NOT NULL THEN 2 ELSE 1 END
                            ELSE 0
                        END
                    ) = 0
                    THEN NULL
                    ELSE
                        1.0 * SUM(
                            CASE
                                WHEN r.value IS NOT NULL AND r.value != 0
                                    THEN r.value * (CASE WHEN sr.user_id IS NOT NULL THEN 2 ELSE 1 END)
                                ELSE 0
                            END
                        )
                        / SUM(
                            CASE
                                WHEN r.value IS NOT NULL AND r.value != 0
                                    THEN CASE WHEN sr.user_id IS NOT NULL THEN 2 ELSE 1 END
                                ELSE 0
                            END
                        )
                END AS avg_rating,
                COUNT(CASE WHEN r.value IS NOT NULL AND r.value != 0 THEN 1 END) AS ratings_count
            FROM photos p
            LEFT JOIN ratings r ON r.photo_id = p.id
            LEFT JOIN super_ratings sr
                ON sr.photo_id = p.id
               AND sr.user_id = r.user_id
            WHERE p.user_id = $1
              AND p.is_deleted = 0
              AND p.moderation_status = 'active'
            """,
            int(user_id),
        )

    if not row:
        return {"avg_rating": None, "ratings_count": 0}

    return {
        "avg_rating": row["avg_rating"],
        "ratings_count": int(row["ratings_count"] or 0),
    }


async def get_most_popular_photo_for_user(user_id: int) -> dict | None:
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                p.id,
                p.title,
                COUNT(CASE WHEN r.value IS NOT NULL AND r.value != 0 THEN 1 END) AS ratings_count,
                CASE
                    WHEN SUM(
                        CASE
                            WHEN r.value IS NOT NULL AND r.value != 0
                                THEN CASE WHEN sr.user_id IS NOT NULL THEN 2 ELSE 1 END
                            ELSE 0
                        END
                    ) = 0
                    THEN NULL
                    ELSE
                        1.0 * SUM(
                            CASE
                                WHEN r.value IS NOT NULL AND r.value != 0
                                    THEN r.value * (CASE WHEN sr.user_id IS NOT NULL THEN 2 ELSE 1 END)
                                ELSE 0
                            END
                        )
                        / SUM(
                            CASE
                                WHEN r.value IS NOT NULL AND r.value != 0
                                    THEN CASE WHEN sr.user_id IS NOT NULL THEN 2 ELSE 1 END
                                ELSE 0
                            END
                        )
                END AS avg_rating
            FROM photos p
            LEFT JOIN ratings r ON r.photo_id = p.id
            LEFT JOIN super_ratings sr
                ON sr.photo_id = p.id
               AND sr.user_id = r.user_id
            WHERE p.user_id = $1
              AND p.is_deleted = 0
              AND p.moderation_status = 'active'
            GROUP BY p.id
            HAVING COUNT(CASE WHEN r.value IS NOT NULL AND r.value != 0 THEN 1 END) > 0
            ORDER BY avg_rating DESC, ratings_count DESC, p.created_at ASC
            LIMIT 1
            """,
            int(user_id),
        )
    return dict(row) if row else None


async def get_weekly_rank_for_user(user_id: int) -> int | None:
    today = get_moscow_now().date()
    start_date = today - timedelta(days=6)
    start_key = start_date.isoformat()
    end_key = today.isoformat()

    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                p.user_id,
                CASE
                    WHEN SUM(
                        CASE
                            WHEN r.value IS NOT NULL AND r.value != 0
                                THEN CASE WHEN sr.user_id IS NOT NULL THEN 2 ELSE 1 END
                            ELSE 0
                        END
                    ) = 0
                    THEN NULL
                    ELSE
                        1.0 * SUM(
                            CASE
                                WHEN r.value IS NOT NULL AND r.value != 0
                                    THEN r.value * (CASE WHEN sr.user_id IS NOT NULL THEN 2 ELSE 1 END)
                                ELSE 0
                            END
                        )
                        / SUM(
                            CASE
                                WHEN r.value IS NOT NULL AND r.value != 0
                                    THEN CASE WHEN sr.user_id IS NOT NULL THEN 2 ELSE 1 END
                                ELSE 0
                            END
                        )
                END AS avg_rating,
                COUNT(CASE WHEN r.value IS NOT NULL AND r.value != 0 THEN 1 END) AS ratings_count
            FROM photos p
            LEFT JOIN ratings r ON r.photo_id = p.id
            LEFT JOIN super_ratings sr
                ON sr.photo_id = p.id
               AND sr.user_id = r.user_id
            WHERE p.day_key BETWEEN $1 AND $2
              AND p.is_deleted = 0
              AND p.moderation_status = 'active'
            GROUP BY p.user_id
            HAVING COUNT(CASE WHEN r.value IS NOT NULL AND r.value != 0 THEN 1 END) > 0
            ORDER BY avg_rating DESC, ratings_count DESC
            """,
            start_key, end_key,
        )

    if not rows:
        return None

    for idx, row in enumerate(rows, start=1):
        if int(row["user_id"]) == int(user_id):
            return idx
    return None


async def save_pending_referral(new_user_tg_id: int, referral_code: str | None) -> None:
    """
    Сохраняем рефералку при /start, пока юзер ещё не зарегистрирован.
    Самый простой вариант: если юзера ещё нет — создадим "черновую" запись.
    Если не хочешь черновые записи — скажи, сделаю через отдельную таблицу pending_referrals.
    """
    if not referral_code:
        return

    p = _assert_pool()
    now = datetime.utcnow().isoformat(timespec="seconds")

    async with p.acquire() as conn:
        ref_owner = await conn.fetchrow(
            "SELECT id FROM users WHERE referral_code = $1 AND is_deleted = 0",
            referral_code,
        )
        if not ref_owner:
            return
        ref_owner_id = int(ref_owner["id"])

        existing = await conn.fetchrow("SELECT id FROM users WHERE tg_id = $1", new_user_tg_id)
        if existing:
            await conn.execute(
                """
                UPDATE users
                SET referred_by_user_id = $1, updated_at = $2
                WHERE tg_id = $3
                """,
                ref_owner_id, now, new_user_tg_id,
            )
        else:
            # черновик, чтобы не потерять рефералку
            await conn.execute(
                """
                INSERT INTO users (tg_id, name, gender, bio, created_at, updated_at, referred_by_user_id)
                VALUES ($1, 'User', 'unknown', '', $2, $2, $3)
                """,
                new_user_tg_id, now, ref_owner_id,
            )


async def get_total_users() -> int:
    p = _assert_pool()
    async with p.acquire() as conn:
        return int((await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_deleted = 0")) or 0)


async def get_moderators() -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users WHERE is_deleted = 0 AND is_moderator = 1 ORDER BY id DESC")
    return [dict(r) for r in rows]


async def get_helpers() -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users WHERE is_deleted = 0 AND is_helper = 1 ORDER BY id DESC")
    return [dict(r) for r in rows]


async def get_support_users() -> list[dict]:
    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users WHERE is_deleted = 0 AND is_support = 1 ORDER BY id DESC")
    return [dict(r) for r in rows]


async def is_moderator_by_tg_id(tg_id: int) -> bool:
    u = await get_user_by_tg_id(tg_id)
    return bool(u and int(u.get("is_moderator") or 0) == 1)


async def set_user_moderator_by_tg_id(tg_id: int, value: bool) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_moderator = $1 WHERE tg_id = $2",
            1 if value else 0,
            tg_id,
        )


async def set_user_admin_by_tg_id(tg_id: int, value: bool) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_admin = $1 WHERE tg_id = $2",
            1 if value else 0,
            tg_id,
        )


async def get_user_block_status_by_tg_id(tg_id: int) -> bool:
    """
    Пока у тебя нет поля is_blocked — вернём False.
    Если у тебя в коде реально используется блокировка, скажи — добавим колонку is_blocked и логику.
    """
    return False


async def set_photo_moderation_status(photo_id: int, status: str) -> None:
    p = _assert_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE photos SET moderation_status = $1 WHERE id = $2",
            status, int(photo_id),
        )


async def get_next_photo_for_moderation() -> dict | None:
    """
    Берём следующее фото со статусом pending.
    Если у тебя другой статус-нейминг — подстроим.
    """
    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.*, u.tg_id AS user_tg_id, u.username AS user_username, u.name AS user_name
            FROM photos p
            JOIN users u ON u.id = p.user_id
            WHERE p.is_deleted = 0
              AND p.moderation_status = 'pending'
            ORDER BY p.created_at ASC, p.id ASC
            LIMIT 1
            """
        )
    return dict(row) if row else None


async def create_photo_report(user_id: int, photo_id: int, reason: str, text: str | None) -> int:
    p = _assert_pool()
    now = get_moscow_now_iso()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO photo_reports (user_id, photo_id, reason, text, status, created_at)
            VALUES ($1,$2,$3,$4,'pending',$5)
            RETURNING id
            """,
            int(user_id), int(photo_id), reason, text, now,
        )
    return int(row["id"])


async def get_photo_report_stats(photo_id: int) -> dict:
    p = _assert_pool()
    async with p.acquire() as conn:
        total = int((await conn.fetchval("SELECT COUNT(*) FROM photo_reports WHERE photo_id = $1", int(photo_id))) or 0)
        pending = int((await conn.fetchval(
            "SELECT COUNT(*) FROM photo_reports WHERE photo_id = $1 AND status = 'pending'",
            int(photo_id),
        )) or 0)
    return {"total": total, "pending": pending}

async def get_daily_top_photos(day_key: str | None = None, limit: int = 4) -> list[dict]:
    """
    Топ дня: берём фото за day_key (yyyy-mm-dd) и сортируем по среднему рейтингу.
    """
    if day_key is None:
        day_key = get_moscow_today()

    p = _assert_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                p.*,
                COUNT(CASE WHEN r.value != 0 THEN 1 END) AS ratings_count,
                AVG(NULLIF(r.value, 0))               AS avg_rating
            FROM photos p
            LEFT JOIN ratings r ON r.photo_id = p.id
            WHERE p.is_deleted = 0
              AND p.moderation_status = 'active'
              AND p.day_key = $1
            GROUP BY p.id
            HAVING COUNT(CASE WHEN r.value != 0 THEN 1 END) > 0
            ORDER BY avg_rating DESC NULLS LAST, ratings_count DESC, p.id ASC
            LIMIT $2
            """,
            day_key, int(limit),
        )
    return [dict(r) for r in rows]


async def get_weekly_best_photo() -> dict | None:
    """
    Лучшее фото за последние 7 дней (по avg). Можно поменять на 'прошлая неделя' — но пока так.
    """
    today = get_moscow_now().date()
    start = (today - timedelta(days=6)).isoformat()
    end = today.isoformat()

    p = _assert_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                p.*,
                COUNT(CASE WHEN r.value != 0 THEN 1 END) AS ratings_count,
                AVG(NULLIF(r.value, 0))               AS avg_rating
            FROM photos p
            LEFT JOIN ratings r ON r.photo_id = p.id
            WHERE p.is_deleted = 0
              AND p.moderation_status = 'active'
              AND p.day_key BETWEEN $1 AND $2
            GROUP BY p.id
            HAVING COUNT(CASE WHEN r.value != 0 THEN 1 END) > 0
            ORDER BY avg_rating DESC NULLS LAST, ratings_count DESC, p.id ASC
            LIMIT 1
            """,
            start, end,
        )
    return dict(row) if row else None


async def save_pending_referral(new_user_tg_id: int, referral_code: str | None) -> None:
    """
    Сохраняем рефералку при /start, даже если юзер ещё не зарегистрирован.
    Потом при регистрации можно будет применить (link_and_reward_referral_if_needed).
    """
    if not referral_code:
        return

    global pool
    if pool is None:
        raise RuntimeError("DB pool is not initialized. Call init_db() first.")

    now = get_moscow_now_iso()

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_referrals (new_user_tg_id, referral_code, created_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (new_user_tg_id)
            DO UPDATE SET referral_code = EXCLUDED.referral_code, created_at = EXCLUDED.created_at
            """,
            int(new_user_tg_id),
            str(referral_code),
            now,
        )