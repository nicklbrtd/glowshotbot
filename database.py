import aiosqlite
import random
from datetime import datetime, timedelta
from utils.time import get_moscow_now, get_moscow_today, get_moscow_now_iso


DB_PATH = "db.sqlite3"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                name TEXT,
                gender TEXT,
                age INTEGER,
                bio TEXT,
                tg_channel_link TEXT,
                daily_skip_date TEXT,
                daily_skip_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_moderator INTEGER NOT NULL DEFAULT 0,
                is_support INTEGER NOT NULL DEFAULT 0,
                is_helper INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                is_premium INTEGER NOT NULL DEFAULT 0,
                premium_until TEXT,
                avatar_file_id TEXT,
                channel_username TEXT,
                notify_likes INTEGER NOT NULL DEFAULT 1,
                notify_comments INTEGER NOT NULL DEFAULT 1,
                is_blocked INTEGER NOT NULL DEFAULT 0,
                blocked_until TEXT,
                blocked_reason TEXT,
                referral_code TEXT,
                referred_by_user_id INTEGER,
                referral_qualified INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                title TEXT NOT NULL,
                device_type TEXT NOT NULL,
                device_info TEXT,
                category TEXT NOT NULL DEFAULT 'photo',
                description TEXT,
                created_at TEXT NOT NULL,
                day_key TEXT NOT NULL,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                moderation_status TEXT NOT NULL DEFAULT 'active',
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                photo_id INTEGER NOT NULL,
                value INTEGER NOT NULL CHECK (value BETWEEN 0 AND 10),
                created_at TEXT NOT NULL,
                UNIQUE(user_id, photo_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS super_ratings (
                user_id INTEGER NOT NULL,
                photo_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, photo_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                photo_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                is_public INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_photos_user_day
                ON photos(user_id, day_key);

            CREATE TABLE IF NOT EXISTS weekly_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id INTEGER UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS awards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                icon TEXT,
                is_special INTEGER NOT NULL DEFAULT 0,
                granted_by_user_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (granted_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_awards_user
                ON awards(user_id);

            CREATE TABLE IF NOT EXISTS photo_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                details TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by_admin_id INTEGER,
                FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (resolved_by_admin_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_photo_reports_photo_status
                ON photo_reports(photo_id, status);

            CREATE TABLE IF NOT EXISTS user_upload_bans (
                user_id INTEGER PRIMARY KEY,
                banned_until TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS moderator_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                moderator_id INTEGER NOT NULL,
                photo_id INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'self',
                reviewed_at TEXT NOT NULL,
                FOREIGN KEY (moderator_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_moderator_reviews_mod_photo
                ON moderator_reviews(moderator_id, photo_id);

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                method TEXT NOT NULL,          -- 'rub' или 'stars'
                period_code TEXT NOT NULL,     -- '7d', '30d', '90d', ...
                days INTEGER NOT NULL,
                amount INTEGER NOT NULL,       -- RUB: в копейках, XTR: кол-во звёзд
                currency TEXT NOT NULL,        -- 'RUB' или 'XTR'
                created_at TEXT NOT NULL,      -- ISO-строка UTC
                telegram_charge_id TEXT,
                provider_charge_id TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_payments_created_at
                ON payments(created_at);

            CREATE INDEX IF NOT EXISTS idx_payments_user
                ON payments(user_id, created_at);

            CREATE TABLE IF NOT EXISTS referral_pending (
                tg_id INTEGER PRIMARY KEY,
                referral_code TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # Миграции для добавления новых полей в существующие таблицы

        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN is_moderator INTEGER NOT NULL DEFAULT 0"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN is_support INTEGER NOT NULL DEFAULT 0"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN is_helper INTEGER NOT NULL DEFAULT 0"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN premium_until TEXT"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN avatar_file_id TEXT"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN channel_username TEXT"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN notify_likes INTEGER NOT NULL DEFAULT 1"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN notify_comments INTEGER NOT NULL DEFAULT 1"
            )
        except aiosqlite.OperationalError:
            pass

        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN tg_channel_link TEXT"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN daily_skip_date TEXT"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN daily_skip_count INTEGER NOT NULL DEFAULT 0"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN blocked_until TEXT"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN blocked_reason TEXT"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN referral_code TEXT"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN referred_by_user_id INTEGER"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN referral_qualified INTEGER NOT NULL DEFAULT 0"
            )
        except aiosqlite.OperationalError:
            pass

        try:
            await db.execute(
                "ALTER TABLE awards ADD COLUMN is_special INTEGER NOT NULL DEFAULT 0"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE awards ADD COLUMN granted_by_user_id INTEGER"
            )
        except aiosqlite.OperationalError:
            pass


# ====== AWARDS / ACHIEVEMENTS HELPERS ======

async def give_achievement_to_user_by_code(
    user_tg_id: int,
    code: str,
    granted_by_tg_id: int | None = None,
) -> bool:
    """
    Выдать пользователю ачивку по коду (например, «beta_tester»).

    Работает поверх таблицы awards и старается не дублировать награды с тем же code.

    Возвращает:
    - True, если награда была выдана впервые;
    - False, если у пользователя уже есть награда с таким code или пользователь не найден.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Ищем пользователя по Telegram ID
        cursor = await db.execute(
            "SELECT id FROM users WHERE tg_id = ? AND is_deleted = 0",
            (user_tg_id,),
        )
        user_row = await cursor.fetchone()
        await cursor.close()

        if not user_row:
            return False

        user_id = int(user_row["id"])

        # Проверяем, нет ли уже награды с таким code
        cursor = await db.execute(
            "SELECT id FROM awards WHERE user_id = ? AND code = ? LIMIT 1",
            (user_id, code),
        )
        existing = await cursor.fetchone()
        await cursor.close()

        if existing:
            return False

        # Опционально находим, кто выдал награду (по tg_id)
        granted_by_user_id: int | None = None
        if granted_by_tg_id is not None:
            cursor = await db.execute(
                "SELECT id FROM users WHERE tg_id = ? AND is_deleted = 0",
                (granted_by_tg_id,),
            )
            gb_row = await cursor.fetchone()
            await cursor.close()
            if gb_row:
                granted_by_user_id = int(gb_row["id"])

        # Маппинг code → человекочитаемые поля
        if code == "beta_tester":
            title = "Бета-тестер бота"
            description = "Ты помог(ла) тестировать GlowShot на ранних стадиях до релиза."
            icon = "🏆"
            is_special = 1
        else:
            # Фоллбек, если появится другой код
            title = code
            description = None
            icon = "🏅"
            is_special = 0

        now_iso = datetime.utcnow().isoformat(timespec="seconds")

        # Вставляем награду
        await db.execute(
            """
            INSERT INTO awards (
                user_id,
                code,
                title,
                description,
                icon,
                is_special,
                granted_by_user_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, code, title, description, icon, is_special, granted_by_user_id, now_iso),
        )
        await db.commit()

        return True


async def get_awards_for_user(user_id: int) -> list[dict]:
    """
    Получить список всех наград пользователя из таблицы awards.
    user_id — внутренний ID (users.id).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM awards
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()

    return [dict(r) for r in rows]


async def get_award_by_id(award_id: int) -> dict | None:
    """
    Получить одну награду по её ID.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM awards WHERE id = ?",
            (award_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row:
        return None
    return dict(row)


async def delete_award_by_id(award_id: int) -> None:
    """
    Удалить награду по её ID.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM awards WHERE id = ?",
            (award_id,),
        )
        await db.commit()


async def update_award_text(award_id: int, title: str, description: str | None) -> None:
    """
    Обновить название и описание награды.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE awards
            SET title = ?, description = ?
            WHERE id = ?
            """,
            (title, description, award_id),
        )
        await db.commit()


async def update_award_icon(award_id: int, icon: str | None) -> None:
    """
    Обновить смайлик/иконку награды.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE awards
            SET icon = ?
            WHERE id = ?
            """,
            (icon, award_id),
        )
        await db.commit()


async def create_custom_award_for_user(
    user_id: int,
    title: str,
    description: str | None,
    icon: str | None,
    code: str | None = None,
    is_special: bool = False,
    granted_by_user_id: int | None = None,
) -> int:
    """
    Создать кастомную ачивку для пользователя.

    user_id — внутренний ID (users.id).
    code — произвольный код (если не указан, будет сгенерирован автоматически).
    Возвращает ID созданной записи в таблице awards.
    """
    now_iso = datetime.utcnow().isoformat(timespec="seconds")

    if code is None:
        ts = int(datetime.utcnow().timestamp())
        code = f"custom_{user_id}_{ts}"

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO awards (
                user_id,
                code,
                title,
                description,
                icon,
                is_special,
                granted_by_user_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, code, title, description, icon, 1 if is_special else 0, granted_by_user_id, now_iso),
        )
        await db.commit()
        return cursor.lastrowid



async def get_user_admin_stats(user_id: int) -> dict:
    """
    Расширенная статистика активности пользователя для админ-раздела «Пользователи».

    Считаем по фактическим действиям в базе:
    • сколько оценок он поставил;
    • сколько оставил комментариев;
    • сколько создал жалоб;
    • сколько всего активных фото у него сейчас;
    • сколько всего фото он когда‑либо загружал;
    • сколько раз для него заводили ограничение на загрузку (user_upload_bans).

    Поле messages_total — суммарное число действий, которые считаем «сообщениями в боте».
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Оценки
        cursor = await db.execute(
            "SELECT COUNT(*) FROM ratings WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        ratings_given = int(row[0] or 0)

        # Комментарии
        cursor = await db.execute(
            "SELECT COUNT(*) FROM comments WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        comments_given = int(row[0] or 0)

        # Жалобы, созданные этим пользователем
        cursor = await db.execute(
            "SELECT COUNT(*) FROM photo_reports WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        reports_created = int(row[0] or 0)

        # Всего фото и активные фото
        cursor = await db.execute(
            """
            SELECT
                SUM(CASE WHEN is_deleted = 0 THEN 1 ELSE 0 END) AS active_count,
                COUNT(*) AS total_count
            FROM photos
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        active_photos = int((row[0] or 0) if row else 0)
        total_photos = int((row[1] or 0) if row else 0)

        # Ограничения на загрузку (user_upload_bans)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM user_upload_bans WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        upload_bans_count = int(row[0] or 0)

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


# ====== PHOTO ADMIN STATS ======
async def get_photo_admin_stats(photo_id: int) -> dict:
    """
    Расширенная статистика по фотографии для админ-панели.
    Возвращает словарь с ключами:
        avg_rating, ratings_count, super_ratings_count, comments_count,
        reports_total, reports_pending, reports_resolved.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Средний рейтинг и количество оценок
        cursor = await db.execute(
            "SELECT AVG(value), COUNT(*) FROM ratings WHERE photo_id = ?",
            (photo_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        avg_rating = float(row[0]) if row and row[0] is not None else None
        ratings_count = int(row[1] or 0) if row else 0

        # Супер-оценки
        cursor = await db.execute(
            "SELECT COUNT(*) FROM super_ratings WHERE photo_id = ?",
            (photo_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        super_ratings_count = int(row[0] or 0)

        # Комментарии
        cursor = await db.execute(
            "SELECT COUNT(*) FROM comments WHERE photo_id = ?",
            (photo_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        comments_count = int(row[0] or 0)

        # Жалобы всего
        cursor = await db.execute(
            "SELECT COUNT(*) FROM photo_reports WHERE photo_id = ?",
            (photo_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        reports_total = int(row[0] or 0)

        # Жалобы в ожидании
        cursor = await db.execute(
            "SELECT COUNT(*) FROM photo_reports WHERE photo_id = ? AND status = 'pending'",
            (photo_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        reports_pending = int(row[0] or 0)

        # Жалобы решено
        cursor = await db.execute(
            "SELECT COUNT(*) FROM photo_reports WHERE photo_id = ? AND status = 'resolved'",
            (photo_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        reports_resolved = int(row[0] or 0)

    return {
        "avg_rating": avg_rating,
        "ratings_count": ratings_count,
        "super_ratings_count": super_ratings_count,
        "comments_count": comments_count,
        "reports_total": reports_total,
        "reports_pending": reports_pending,
        "reports_resolved": reports_resolved,
    }


# ====== PAYMENTS & SUBSCRIPTIONS ======

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
    """
    Записать успешный платёж в таблицу payments.

    amount:
        - для RUB — сумма в копейках (как приходит от Telegram);
        - для XTR (Stars) — количество звёзд.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM users WHERE tg_id = ? AND is_deleted = 0",
            (tg_id,),
        )
        user_row = await cursor.fetchone()
        await cursor.close()

        if not user_row:
            return

        user_id = int(user_row["id"])
        now_iso = datetime.utcnow().isoformat(timespec="seconds")

        await db.execute(
            """
            INSERT INTO payments (
                user_id,
                method,
                period_code,
                days,
                amount,
                currency,
                created_at,
                telegram_charge_id,
                provider_charge_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                method,
                period_code,
                days,
                amount,
                currency,
                now_iso,
                telegram_charge_id,
                provider_charge_id,
            ),
        )
        await db.commit()


async def get_payments_count() -> int:
    """
    Общее количество записанных успешных платежей.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM payments")
        row = await cursor.fetchone()
        await cursor.close()
    return int(row[0] or 0) if row else 0


async def get_payments_page(page: int, page_size: int = 20) -> list[dict]:
    """
    Страница платежей с привязкой к пользователям.
    Сортировка: новые сверху.
    """
    if page < 1:
        page = 1
    offset = (page - 1) * page_size

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                p.*,
                u.tg_id AS user_tg_id,
                u.username AS user_username,
                u.name AS user_name
            FROM payments p
            JOIN users u ON u.id = p.user_id
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        )
        rows = await cursor.fetchall()
        await cursor.close()

    return [dict(r) for r in rows]


async def get_revenue_summary(period: str) -> dict:
    """
    Подсчёт доходов за период:
        period = 'day' | 'week' | 'month'
    Возвращает:
    {
        "period": period,
        "from": iso_start,
        "to": iso_end,
        "rub_total": float,   # сумма в рублях
        "rub_count": int,     # кол-во RUB-платежей
        "stars_total": int,   # кол-во звёзд
        "stars_count": int,   # кол-во XTR-платежей
    }
    """
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

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT currency, SUM(amount) AS total_amount, COUNT(*) AS cnt
            FROM payments
            WHERE created_at >= ?
            GROUP BY currency
            """,
            (start_iso,),
        )
        rows = await cursor.fetchall()
        await cursor.close()

    for row in rows or []:
        currency = row[0]
        total_amount = int(row[1] or 0)
        cnt = int(row[2] or 0)
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
    """
    Количество пользователей, у которых есть хотя бы один платёж.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(DISTINCT user_id) FROM payments")
        row = await cursor.fetchone()
        await cursor.close()
    return int(row[0] or 0) if row else 0


async def get_subscriptions_page(page: int, page_size: int = 20) -> list[dict]:
    """
    Страница по пользователям с платежами.
    Для каждого:
      - last_payment_at
      - payments_count
      - total_days
      - total_rub (float)
      - total_stars (int)
    """
    if page < 1:
        page = 1
    offset = (page - 1) * page_size

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
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
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        )
        rows = await cursor.fetchall()
        await cursor.close()

    result: list[dict] = []
    for r in rows or []:
        d = dict(r)
        rub_minor = int(d.get("total_rub_minor") or 0)
        d["total_rub"] = rub_minor / 100.0 if rub_minor else 0.0
        result.append(d)

    return result

# ====== REFERRALS ======

async def _generate_unique_referral_code(db: aiosqlite.Connection) -> str:
    """
    Сгенерировать уникальный реферальный код вида GSXXXXXX.
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "GS" + "".join(random.choice(alphabet) for _ in range(6))
        cursor = await db.execute(
            "SELECT 1 FROM users WHERE referral_code = ? LIMIT 1",
            (code,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return code

async def get_or_create_referral_code(tg_id: int) -> str | None:
    """
    Получить или создать реферальный код для пользователя по его Telegram ID.
    Возвращает строку-код или None, если пользователь не найден.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, referral_code FROM users WHERE tg_id = ? AND is_deleted = 0",
            (tg_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if not row:
            return None

        existing = row["referral_code"]
        if existing:
            return str(existing)

        user_id = int(row["id"])
        new_code = await _generate_unique_referral_code(db)
        await db.execute(
            "UPDATE users SET referral_code = ? WHERE id = ?",
            (new_code, user_id),
        )
        await db.commit()
        return new_code

async def get_referral_stats_for_user(tg_id: int) -> dict:
    """
    Простая статистика по рефералке для пользователя с заданным tg_id.
    Возвращает:
    {
        "invited_total": int,      # всего людей, у которых указан referred_by_user_id
        "invited_qualified": int,  # людей с referral_qualified = 1
    }
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM users WHERE tg_id = ? AND is_deleted = 0",
            (tg_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if not row:
            return {
                "invited_total": 0,
                "invited_qualified": 0,
            }

        user_id = int(row["id"])

        cursor = await db.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by_user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        invited_total = int(row[0] or 0) if row else 0

        cursor = await db.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by_user_id = ? AND referral_qualified = 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        invited_qualified = int(row[0] or 0) if row else 0

    return {
        "invited_total": invited_total,
        "invited_qualified": invited_qualified,
    }

# ====== PHOTOS COUNT BY USER ======

async def count_photos_by_user(user_id: int) -> int:
    """
    Вернуть количество активных (не удалённых) фотографий пользователя.
    user_id — внутренний ID из таблицы users.id.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM photos
            WHERE user_id = ?
              AND is_deleted = 0
              AND moderation_status = 'active'
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row or row[0] is None:
        return 0
    return int(row[0])


# ====== USER RATING SUMMARY & MOST POPULAR PHOTO ======

async def get_user_rating_summary(user_id: int) -> dict:
    """
    Вернуть среднюю оценку и количество оценок по всем активным фото пользователя.
    Значения 0 в ratings.value считаются пропуском и не учитываются.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
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
            WHERE p.user_id = ?
              AND p.is_deleted = 0
              AND p.moderation_status = 'active'
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row:
        return {"avg_rating": None, "ratings_count": 0}

    return {
        "avg_rating": row["avg_rating"],
        "ratings_count": row["ratings_count"] or 0,
    }


async def get_most_popular_photo_for_user(user_id: int) -> dict | None:
    """
    Вернуть самое «популярное» фото пользователя:
    с максимальной средней оценкой, затем по числу оценок, затем по дате.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
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
            WHERE p.user_id = ?
              AND p.is_deleted = 0
              AND p.moderation_status = 'active'
            GROUP BY p.id
            HAVING COUNT(CASE WHEN r.value IS NOT NULL AND r.value != 0 THEN 1 END) > 0
            ORDER BY avg_rating DESC,
                     ratings_count DESC,
                     p.created_at ASC
            LIMIT 1
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row:
        return None
    return dict(row)


# ====== WEEKLY RANK FOR USER ======

async def get_weekly_rank_for_user(user_id: int) -> int | None:
    """
    Позиция пользователя в «топе недели» по средней оценке его фото за последние 7 дней.
    Возвращает 1, 2, 3... или None, если у пользователя нет оценённых фото за период.
    """
    today = get_moscow_now().date()
    start_date = today - timedelta(days=6)
    start_key = start_date.isoformat()
    end_key = today.isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
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
            WHERE p.day_key BETWEEN ? AND ?
              AND p.is_deleted = 0
              AND p.moderation_status = 'active'
            GROUP BY p.user_id
            HAVING ratings_count > 0
            ORDER BY avg_rating DESC,
                     ratings_count DESC
            """,
            (start_key, end_key),
        )
        rows = await cursor.fetchall()
        await cursor.close()

    if not rows:
        return None

    for idx, row in enumerate(rows, start=1):
        if row["user_id"] == user_id:
            return idx

    return None
# ====== PREMIUM & NOTIFY SETTINGS ======

async def set_user_premium_status(
    tg_id: int,
    is_premium: bool,
    premium_until: str | None = None,
) -> None:
    """
    Выдать или снять премиум-статус пользователю.
    premium_until — строка с датой/временем в человекочитаемом или ISO-формате.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_premium = ?, premium_until = ? WHERE tg_id = ?",
            (1 if is_premium else 0, premium_until, tg_id),
        )
        await db.commit()


async def get_user_premium_status(tg_id: int) -> dict:
    """
    Получить премиум-статус пользователя по его Telegram ID.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT is_premium, premium_until FROM users WHERE tg_id = ? AND is_deleted = 0",
            (tg_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row:
        return {"is_premium": False, "premium_until": None}

    return {
        "is_premium": bool(row["is_premium"]),
        "premium_until": row["premium_until"],
    }


async def get_premium_users() -> list[dict]:
    """
    Получить список всех пользователей с флагом is_premium = 1.
    Используется в админке в разделе «Роли → Премиум».
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE is_premium = 1 ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def set_user_premium_role_by_tg_id(tg_id: int, value: bool) -> None:
    """
    Утилита для админки «Роли»:
    - value = True  -> выдать премиум (is_premium = 1, premium_until не задаём — бессрочно);
    - value = False -> снять премиум (is_premium = 0, premium_until сбрасываем).
    """
    if value:
        # Премиум без срока (можно потом переопределить отдельным интерфейсом)
        await set_user_premium_status(tg_id, True, premium_until=None)
    else:
        # Снимаем премиум и обнуляем дату
        await set_user_premium_status(tg_id, False, premium_until=None)


# ====== PREMIUM ACTIVE HELPER ======
async def is_user_premium_active(tg_id: int) -> bool:
    """
    Проверить, активен ли премиум у пользователя прямо сейчас.

    Логика:
    - Если is_premium = 0 -> False;
    - Если is_premium = 1 и premium_until = NULL/None -> считаем бессрочным премиумом -> True;
    - Если premium_until задана как ISO-строка -> сравниваем с текущим временем по Москве.
    """
    data = await get_user_premium_status(tg_id)
    if not data.get("is_premium"):
        return False

    premium_until = data.get("premium_until")
    if not premium_until:
        # Бессрочный премиум (например, вручную выданный навсегда)
        return True

    try:
        # premium_until ожидается в ISO-формате (как get_moscow_now_iso)
        until_dt = datetime.fromisoformat(premium_until)
    except Exception:
        # Если формат битый — безопаснее считать, что премиум не активен
        return False

    now = get_moscow_now()
    return now < until_dt


async def get_user_notify_settings(tg_id: int) -> dict:
    """
    Получить настройки уведомлений пользователя.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT notify_likes, notify_comments FROM users WHERE tg_id = ? AND is_deleted = 0",
            (tg_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row:
        return {"notify_likes": True, "notify_comments": True}

    return {
        "notify_likes": bool(row["notify_likes"]),
        "notify_comments": bool(row["notify_comments"]),
    }


async def set_user_notify_likes(tg_id: int, enabled: bool) -> None:
    """
    Включить или выключить уведомления о лайках.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET notify_likes = ? WHERE tg_id = ? AND is_deleted = 0",
            (1 if enabled else 0, tg_id),
        )
        await db.commit()


async def set_user_notify_comments(tg_id: int, enabled: bool) -> None:
    """
    Включить или выключить уведомления о комментариях.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET notify_comments = ? WHERE tg_id = ? AND is_deleted = 0",
            (1 if enabled else 0, tg_id),
        )
        await db.commit()
        try:
            await db.execute(
                "ALTER TABLE photos ADD COLUMN moderation_status TEXT NOT NULL DEFAULT 'active'"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE photos ADD COLUMN category TEXT NOT NULL DEFAULT 'photo'"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE photos ADD COLUMN description TEXT"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE comments ADD COLUMN is_moderator INTEGER NOT NULL DEFAULT 0"
            )
        except aiosqlite.OperationalError:
            pass

        await db.commit()


async def get_all_users_tg_ids() -> list[int]:
    """
    Вернуть список tg_id всех не удалённых пользователей.
    Используется для рассылки «всем пользователям».
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT tg_id FROM users WHERE is_deleted = 0"
        )
        rows = await cursor.fetchall()
        await cursor.close()
    # r[0] — tg_id
    return [int(r[0]) for r in rows if r[0] is not None]


# ====== DAILY SKIP HELPERS ======

async def get_daily_skip_info(tg_id: int) -> tuple[str | None, int]:
    """Вернуть дату и количество пропусков оценок за день для пользователя.

    Если пользователя нет или данные не заданы, вернём (None, 0).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT daily_skip_date, daily_skip_count FROM users WHERE tg_id = ? AND is_deleted = 0",
            (tg_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row:
        return None, 0

    date_str, count = row
    return date_str, int(count or 0)


async def update_daily_skip_info(tg_id: int, date_str: str, count: int) -> None:
    """Обновить дату и счётчик дневных пропусков оценок для пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE users
            SET daily_skip_date = ?, daily_skip_count = ?
            WHERE tg_id = ? AND is_deleted = 0
            """,
            (date_str, count, tg_id),
        )
        await db.commit()


async def get_user_by_tg_id(tg_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE tg_id = ? AND is_deleted = 0",
            (tg_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)


# Получить пользователя по username (без @), если он существует и не удалён.
async def get_user_by_username(username: str) -> dict | None:
    """
    Получить пользователя по username (без @), если он существует и не удалён.
    """
    if not username:
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE username = ? AND is_deleted = 0",
            (username,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)
    
async def get_user_by_id(user_id: int) -> dict | None:
    """
    Получить пользователя по внутреннему ID (поле users.id).
    Используется, когда у нас есть user_id из таблицы photos.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE id = ? AND is_deleted = 0",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)


async def create_user(
    tg_id: int,
    username: str | None,
    name: str,
    gender: str,
    age: int | None,
    bio: str,
) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM users WHERE tg_id = ?",
            (tg_id,),
        )
        row = await cursor.fetchone()

        if row:
            user_id = row[0]
            await db.execute(
                """
                UPDATE users
                SET username = ?, name = ?, gender = ?, age = ?, bio = ?,
                    updated_at = ?, is_deleted = 0
                WHERE tg_id = ?
                """,
                (username, name, gender, age, bio, now, tg_id),
            )
            await db.commit()
            return user_id

        cursor = await db.execute(
            """
            INSERT INTO users (tg_id, username, name, gender, age, bio, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tg_id, username, name, gender, age, bio, now, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_today_photo_for_user(user_id: int) -> dict | None:
    """
    Вернуть последнюю на сегодня запись фото пользователя (включая удалённые).
    Используется для отображения статуса суточного лимита и полного отображения работы.
    """
    today_key = get_moscow_now().date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM photos
            WHERE user_id = ?
              AND day_key = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, today_key),
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row:
        return None
    return dict(row)


async def create_today_photo(
    user_id: int,
    file_id: str,
    title: str,
    device_type: str,
    device_info: str | None,
    category: str,
    description: str | None,
) -> int:
    """
    Создать или обновить сегодняшнюю фотографию пользователя.

    Логика:
    - В таблице photos есть уникальный индекс (user_id, day_key), поэтому на один день
      у пользователя может быть только одна запись.
    - Если запись уже есть и мы снова вызываем эту функцию (например, после итогов дня),
      то она не падает с UNIQUE-ошибкой, а просто обновляет существующую строку
      (file_id, title, device_type, device_info, created_at, is_deleted, moderation_status).
    """
    now = get_moscow_now_iso()
    day_key = get_moscow_today()

    async with aiosqlite.connect(DB_PATH) as db:
        # UPSERT: при конфликте по (user_id, day_key) обновляем существующую запись
        await db.execute(
            """
            INSERT INTO photos (user_id, file_id, title, device_type, device_info, category, description, created_at, day_key, is_deleted, moderation_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'active')
            ON CONFLICT(user_id, day_key) DO UPDATE SET
                file_id = excluded.file_id,
                title = excluded.title,
                device_type = excluded.device_type,
                device_info = excluded.device_info,
                category = excluded.category,
                description = excluded.description,
                created_at = excluded.created_at,
                is_deleted = 0,
                moderation_status = 'active'
            """,
            (user_id, file_id, title, device_type, device_info, category, description, now, day_key),
        )
        await db.commit()

        # cursor.lastrowid при UPSERT может быть не тем, поэтому явно достаём id
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM photos WHERE user_id = ? AND day_key = ?",
            (user_id, day_key),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if not row:
            raise RuntimeError("Не удалось получить id фотографии после UPSERT")

        return row["id"]


async def mark_photo_deleted(photo_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE photos SET is_deleted = 1 WHERE id = ?",
            (photo_id,),
        )
        await db.commit()


async def get_photo_by_id(photo_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM photos WHERE id = ?",
            (photo_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)
    

async def get_photo_stats(photo_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
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
            WHERE r.photo_id = ?
            """,
            (photo_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"ratings_count": 0, "avg_rating": None, "skips_count": 0}

        return {
            "ratings_count": row["ratings_count"] or 0,
            "avg_rating": row["avg_rating"],  # может быть None
            "skips_count": row["skips_count"] or 0,
        }


async def create_comment(
    user_id: int,
    photo_id: int,
    text: str,
    is_public: bool,
) -> None:
    now = get_moscow_now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO comments (user_id, photo_id, text, is_public, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, photo_id, text, 1 if is_public else 0, now),
        )
        await db.commit()


async def get_comments_for_photo(photo_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
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
            WHERE c.photo_id = ?
            ORDER BY c.created_at DESC
            LIMIT ?
            """,
            (photo_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_user_name(tg_id: int, name: str) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET name = ?, updated_at = ? WHERE tg_id = ? AND is_deleted = 0",
            (name, now, tg_id),
        )
        await db.commit()


async def update_user_gender(tg_id: int, gender: str) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET gender = ?, updated_at = ? WHERE tg_id = ? AND is_deleted = 0",
            (gender, now, tg_id),
        )
        await db.commit()


async def update_user_age(tg_id: int, age: int | None) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET age = ?, updated_at = ? WHERE tg_id = ? AND is_deleted = 0",
            (age, now, tg_id),
        )
        await db.commit()


async def update_user_bio(tg_id: int, bio: str) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET bio = ?, updated_at = ? WHERE tg_id = ? AND is_deleted = 0",
            (bio, now, tg_id),
        )
        await db.commit()


async def update_user_channel_link(tg_id: int, link: str | None):
    """
    Обновляет ссылку на Telegram-канал/страницу в профиле пользователя.
    Ожидается уже нормализованная строка (например, https://t.me/username) или None.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET tg_channel_link = ? WHERE tg_id = ?",
            (link, tg_id),
        )
        await db.commit()


async def soft_delete_user(tg_id: int) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_deleted = 1, updated_at = ? WHERE tg_id = ?",
            (now, tg_id),
        )
        await db.commit()


async def get_random_photo_for_rating(rater_user_id: int) -> dict | None:
    day_key = get_moscow_today()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                p.*,
                u.is_premium     AS user_is_premium,
                u.tg_channel_link AS user_tg_channel_link
            FROM photos p
            JOIN users u ON p.user_id = u.id
            WHERE p.is_deleted = 0
              AND p.moderation_status = 'active'
              AND u.is_deleted = 0
              AND u.id != ?
              AND p.day_key = ?
              AND NOT EXISTS (
                  SELECT 1 FROM ratings r
                  WHERE r.user_id = ? AND r.photo_id = p.id
              )
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (rater_user_id, day_key, rater_user_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)


async def add_rating(user_id: int, photo_id: int, value: int) -> None:
    now = get_moscow_now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        # При любой новой оценке сбрасываем возможную супер-оценку
        await db.execute(
            "DELETE FROM super_ratings WHERE user_id = ? AND photo_id = ?",
            (user_id, photo_id),
        )
        await db.execute(
            """
            INSERT OR REPLACE INTO ratings (user_id, photo_id, value, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, photo_id, value, now),
        )
        await db.commit()


async def set_super_rating(user_id: int, photo_id: int) -> None:
    """
    Отметить, что пользователь поставил супер-оценку (15 баллов) данной работе.
    В таблице ratings хранится обычная десятка, а «+5» считаются через super_ratings.
    """
    now = get_moscow_now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO super_ratings (user_id, photo_id, created_at)
            VALUES (?, ?, ?)
            """,
            (user_id, photo_id, now),
        )
        await db.commit()
        

async def get_daily_best_photo(day_key: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                p.*,
                u.name AS user_name,
                u.username AS user_username,
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
                SUM(CASE WHEN r.value > 0 THEN 1 ELSE 0 END) AS ratings_count
            FROM photos p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN ratings r ON r.photo_id = p.id
            LEFT JOIN super_ratings sr
                ON sr.photo_id = p.id
               AND sr.user_id = r.user_id
            WHERE p.day_key = ?
              AND p.is_deleted = 0
              AND p.moderation_status = 'active'
              AND u.is_deleted = 0
            GROUP BY p.id
            HAVING SUM(CASE WHEN r.value > 0 THEN 1 ELSE 0 END) > 0
            ORDER BY avg_rating DESC, ratings_count DESC, p.created_at ASC
            LIMIT 1
            """,
            (day_key,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_weekly_best_photo(start_day: str, end_day: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                p.*,
                u.name AS user_name,
                u.username AS user_username,
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
                        SUM(
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
                SUM(CASE WHEN r.value > 0 THEN 1 ELSE 0 END) AS ratings_count
            FROM photos p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN ratings r ON r.photo_id = p.id
            LEFT JOIN super_ratings sr
                ON sr.photo_id = p.id
               AND sr.user_id = r.user_id
            WHERE p.day_key BETWEEN ? AND ?
              AND p.is_deleted = 0
              AND p.moderation_status = 'active'
              AND u.is_deleted = 0
            GROUP BY p.id
            HAVING
                SUM(CASE WHEN r.value > 0 THEN 1 ELSE 0 END) > 0
                AND avg_rating >= 9.0
            ORDER BY avg_rating DESC, ratings_count DESC, p.created_at ASC
            LIMIT 1
            """,
            (start_day, end_day),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    
async def get_daily_top_photos(day_key: str, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                p.id,
                p.file_id,
                p.title,
                u.name      AS user_name,
                u.username  AS user_username,
                COUNT(CASE WHEN r.value >= 9 THEN 1 END)                        AS best_count,
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
            JOIN users u ON p.user_id = u.id
            LEFT JOIN ratings r ON r.photo_id = p.id
            LEFT JOIN super_ratings sr
                ON sr.photo_id = p.id
               AND sr.user_id = r.user_id
            WHERE p.day_key = ?
              AND p.is_deleted = 0
              AND p.moderation_status = 'active'
            GROUP BY p.id
            HAVING ratings_count > 0
            ORDER BY avg_rating DESC,
                     best_count DESC,
                     ratings_count DESC,
                     p.id ASC
            LIMIT ?
            """,
            (day_key, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# ====== WEEKLY CANDIDATES HELPERS ======

async def add_weekly_candidate(photo_id: int) -> None:
    now = get_moscow_now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO weekly_candidates (photo_id, created_at)
            VALUES (?, ?)
            """,
            (photo_id, now),
        )
        await db.commit()


async def is_photo_in_weekly(photo_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM weekly_candidates WHERE photo_id = ?",
            (photo_id,),
        )
        row = await cursor.fetchone()
        return row is not None


async def get_weekly_photos_for_user(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                p.*,
                w.created_at AS weekly_created_at
            FROM weekly_candidates w
            JOIN photos p ON p.id = w.photo_id
            WHERE p.user_id = ?
              AND p.is_deleted = 0
            ORDER BY w.created_at DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    
async def set_user_admin_by_tg_id(tg_id: int, is_admin: bool = True) -> None:
    value = 1 if is_admin else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_admin = ? WHERE tg_id = ?",
            (value, tg_id),
        )
        await db.commit()


async def set_user_moderator_by_tg_id(tg_id: int, is_moderator: bool = True) -> None:
    """
    Выдать или снять статус модератора по Telegram ID пользователя.
    """
    value = 1 if is_moderator else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_moderator = ? WHERE tg_id = ?",
            (value, tg_id),
        )
        await db.commit()


async def is_moderator_by_tg_id(tg_id: int) -> bool:
    """
    Проверить, является ли пользователь модератором (и не удалён ли он).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT is_moderator FROM users WHERE tg_id = ? AND is_deleted = 0",
            (tg_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        return bool(row["is_moderator"])


async def get_moderators() -> list[dict]:
    """
    Получить список всех активных модераторов.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE is_moderator = 1 AND is_deleted = 0 ORDER BY created_at ASC",
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def set_user_support_by_tg_id(tg_id: int, is_support: bool) -> None:
    """
    Выдать или снять статус поддержки по Telegram ID пользователя.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_support = ? WHERE tg_id = ?",
            (1 if is_support else 0, tg_id),
        )
        await db.commit()


async def get_support_users() -> list[dict]:
    """
    Получить список всех пользователей, у которых включена роль поддержки.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE is_support = 1 AND is_deleted = 0 ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def set_user_helper_by_tg_id(tg_id: int, is_helper: bool) -> None:
    """
    Выдать или снять роль помощника по Telegram ID пользователя.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_helper = ? WHERE tg_id = ?",
            (1 if is_helper else 0, tg_id),
        )
        await db.commit()


async def get_helpers() -> list[dict]:
    """
    Получить список всех пользователей с ролью помощника.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE is_helper = 1 AND is_deleted = 0 ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ====== GLOBAL USER BLOCK HELPERS ======

async def set_user_block_status_by_tg_id(
    tg_id: int,
    is_blocked: bool,
    blocked_until: str | None = None,
    reason: str | None = None,
) -> None:
    """
    Установить или снять глобальную блокировку пользователя по его Telegram ID.

    blocked_until — строка с датой/временем (например, в ISO-формате) или None,
    reason — текстовая причина блокировки.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE users
            SET
                is_blocked = ?,
                blocked_until = ?,
                blocked_reason = ?
            WHERE tg_id = ?
            """,
            (1 if is_blocked else 0, blocked_until, reason if is_blocked else None, tg_id),
        )
        await db.commit()


async def get_user_block_status_by_tg_id(tg_id: int) -> dict:
    """
    Получить информацию о глобальной блокировке пользователя по Telegram ID.
    Возвращает словарь с ключами:
      - is_blocked: bool
      - blocked_until: str | None
      - blocked_reason: str | None
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT is_blocked, blocked_until, blocked_reason
            FROM users
            WHERE tg_id = ? AND is_deleted = 0
            """,
            (tg_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row:
        return {"is_blocked": False, "blocked_until": None, "blocked_reason": None}

    return {
        "is_blocked": bool(row["is_blocked"]),
        "blocked_until": row["blocked_until"],
        "blocked_reason": row["blocked_reason"],
    }


async def get_blocked_users() -> list[dict]:
    """
    Получить список всех глобально заблокированных пользователей.
    Используется в модераторском разделе «Список заблокированных».
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM users
            WHERE is_blocked = 1
              AND is_deleted = 0
            ORDER BY updated_at DESC, created_at DESC
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()

    return [dict(r) for r in rows]


# ====== ЖАЛОБЫ И БАНЫ ======

async def create_photo_report(
    photo_id: int,
    user_id: int,
    reason: str,
    details: str | None,
) -> int:
    now = get_moscow_now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO photo_reports (photo_id, user_id, reason, details, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (photo_id, user_id, reason, details, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_photo_report_stats(photo_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS total_pending,
                COUNT(*) AS total_all
            FROM photo_reports
            WHERE photo_id = ?
            """,
            (photo_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"total_pending": 0, "total_all": 0}

        return {
            "total_pending": row["total_pending"] or 0,
            "total_all": row["total_all"] or 0,
        }


async def set_photo_moderation_status(photo_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE photos SET moderation_status = ? WHERE id = ?",
            (status, photo_id),
        )
        await db.commit()


async def resolve_photo_reports(
    photo_id: int,
    admin_user_id: int | None,
    new_status: str,
) -> None:
    now = get_moscow_now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE photo_reports
            SET status = ?, resolved_at = ?, resolved_by_admin_id = ?
            WHERE photo_id = ? AND status = 'pending'
            """,
            (new_status, now, admin_user_id, photo_id),
        )
        await db.commit()


async def set_user_upload_ban(user_id: int, banned_until: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_upload_bans (user_id, banned_until)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET banned_until = excluded.banned_until
            """,
            (user_id, banned_until),
        )
        await db.commit()


async def get_user_upload_ban(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM user_upload_bans WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)

async def get_next_photo_for_moderation() -> dict | None:
    """
    Вернуть одну фотографию, которая находится в статусе 'under_review'
    и должна быть показана модераторам.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM photos WHERE moderation_status = 'under_review' ORDER BY id LIMIT 1"
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row:
        return None
    return dict(row)


async def get_next_photo_for_detailed_moderation() -> dict | None:
    """
    Вернуть одну фотографию, которая находится в статусе 'under_detailed_review'
    и должна быть показана модераторам в режиме детальной проверки.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM photos
            WHERE moderation_status = 'under_detailed_review'
              AND is_deleted = 0
            ORDER BY id
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row:
        return None
    return dict(row)


# ====== МОДЕРАТОРСКИЕ ПРОСМОТРЫ ======

async def add_moderator_review(
    moderator_id: int,
    photo_id: int,
    source: str = "self",
) -> None:
    """
    Зафиксировать, что модератор посмотрел конкретную фотографию.
    source:
      - 'self'   — просмотр в режиме «Проверять самостоятельно»
      - 'report' — просмотр по жалобам (можно использовать позже)
    """
    now = get_moscow_now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO moderator_reviews (moderator_id, photo_id, source, reviewed_at)
            VALUES (?, ?, ?, ?)
            """,
            (moderator_id, photo_id, source, now),
        )
        await db.commit()


async def get_next_photo_for_self_moderation(moderator_id: int) -> dict | None:
    """
    Вернуть одну фотографию для режима «Проверять самостоятельно».

    Логика:
    - Берём только активные и не удалённые фотографии.
    - Не показываем свои собственные работы модератора.
    - Не показываем те фото, которые этот модератор уже смотрел в self-режиме.
    - Сортируем по дате создания (сначала более новые).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT p.*
            FROM photos p
            JOIN users u ON p.user_id = u.id
            WHERE p.is_deleted = 0
              AND p.moderation_status = 'active'
              AND u.is_deleted = 0
              AND p.user_id != ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM moderator_reviews mr
                  WHERE mr.moderator_id = ?
                    AND mr.photo_id = p.id
                    AND mr.source = 'self'
              )
            ORDER BY p.created_at DESC
            LIMIT 1
            """,
            (moderator_id, moderator_id),
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row:
        return None
    return dict(row)

async def get_total_users() -> int:
    """
    Вернуть общее количество незаблокированных/неудалённых пользователей в боте.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM users WHERE is_deleted = 0"
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row or row[0] is None:
        return 0
    return int(row[0])
async def give_award(
    user_id: int,
    code: str,
    title: str,
    description: str | None = None,
    icon: str | None = None,
    is_special: bool = False,
    granted_by_user_id: int | None = None,
) -> int:
    """
    Выдать награду пользователю.

    code — внутренний код награды (можно использовать для проверки дублей),
    title — заголовок, icon — эмодзи или короткий маркер.
    is_special — «особая»/статусная награда (например, админская или бета-тестер),
    granted_by_user_id — внутренний ID пользователя, который выдал награду (users.id).

    TODO: часть наград могут выдавать не только админы, но и премиум-аккаунты.
    """
    now = get_moscow_now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO awards (
                user_id,
                code,
                title,
                description,
                icon,
                is_special,
                granted_by_user_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                code,
                title,
                description,
                icon,
                1 if is_special else 0,
                granted_by_user_id,
                now,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_awards_for_user(user_id: int) -> list[dict]:
    """
    Получить список всех наград пользователя.

    Сортировка:
    - сначала «особые» (is_special = 1),
    - затем по дате выдачи (created_at DESC, id DESC).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM awards
            WHERE user_id = ?
            ORDER BY is_special DESC, created_at DESC, id DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()

    return [dict(r) for r in rows]


# Подсчитать количество «особых» наград пользователя.
async def count_special_awards_for_user(user_id: int) -> int:
    """
    Подсчитать количество «особых» (is_special = 1) наград пользователя.
    Это можно использовать, чтобы в интерфейсе показать, например: «🏆 100 ачивок».
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM awards
            WHERE user_id = ? AND is_special = 1
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

    return int(row[0]) if row and row[0] is not None else 0
async def get_users_sample(limit: int = 20) -> list[dict]:
    """
    Вернуть до `limit` пользователей (TG ID, username, имя) для примеров/списков.
    Используются в админской статистике, когда общее количество небольшое.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT tg_id, username, name
            FROM users
            WHERE is_deleted = 0
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()

    return [dict(r) for r in rows]


async def get_active_users_last_24h(limit: int = 20) -> tuple[int, list[dict]]:
    """
    Вернуть количество и список пользователей, у которых была активность за последние 24 часа.
    Используем поле updated_at как последнюю активность.
    """
    cutoff = datetime.utcnow() - timedelta(hours=24)
    cutoff_iso = cutoff.isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Общее количество
        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE is_deleted = 0
              AND updated_at >= ?
            """,
            (cutoff_iso,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        total = int(row[0] or 0)

        sample: list[dict] = []
        if total > 0:
            cursor = await db.execute(
                """
                SELECT tg_id, username, name
                FROM users
                WHERE is_deleted = 0
                  AND updated_at >= ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (cutoff_iso, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            sample = [dict(r) for r in rows]

    return total, sample


async def get_online_users_recent(window_minutes: int = 5, limit: int = 20) -> tuple[int, list[dict]]:
    """
    Вернуть количество и список «онлайн» пользователей:
    считаем онлайн тех, у кого была активность за последние window_minutes минут.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)
    cutoff_iso = cutoff.isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE is_deleted = 0
              AND updated_at >= ?
            """,
            (cutoff_iso,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        total = int(row[0] or 0)

        sample: list[dict] = []
        if total > 0:
            cursor = await db.execute(
                """
                SELECT tg_id, username, name
                FROM users
                WHERE is_deleted = 0
                  AND updated_at >= ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (cutoff_iso, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            sample = [dict(r) for r in rows]

    return total, sample


async def get_new_users_last_days(days: int = 3, limit: int = 20) -> tuple[int, list[dict]]:
    """
    Вернуть количество и список пользователей, впервые запустивших бота за последние days дней.
    Основано на поле created_at.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    cutoff_iso = cutoff.isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE is_deleted = 0
              AND created_at >= ?
            """,
            (cutoff_iso,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        total = int(row[0] or 0)

        sample: list[dict] = []
        if total > 0:
            cursor = await db.execute(
                """
                SELECT tg_id, username, name
                FROM users
                WHERE is_deleted = 0
                  AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (cutoff_iso, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            sample = [dict(r) for r in rows]

    return total, sample


async def get_premium_stats(limit: int = 20) -> dict:
    """
    Статистика по премиум-пользователям:
    - total: общее количество;
    - total_paid: с премиумом по дате premium_until;
    - total_gift: бессрочный премиум (premium_until IS NULL);
    - paid_sample / gift_sample: примеры пользователей.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT tg_id, username, name, premium_until
            FROM users
            WHERE is_deleted = 0
              AND is_premium = 1
            """,
        )
        rows = await cursor.fetchall()
        await cursor.close()

    paid: list[dict] = []
    gift: list[dict] = []

    for r in rows:
        item = dict(r)
        if item.get("premium_until"):
            paid.append(item)
        else:
            gift.append(item)

    return {
        "total": len(rows),
        "total_paid": len(paid),
        "total_gift": len(gift),
        "paid_sample": paid[:limit],
        "gift_sample": gift[:limit],
    }


async def get_blocked_users_page(limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
    """
    Вернуть общее количество заблокированных пользователей и страницу со списком.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE is_deleted = 0
              AND is_blocked = 1
            """,
        )
        row = await cursor.fetchone()
        await cursor.close()
        total = int(row[0] or 0)

        users: list[dict] = []
        if total > 0:
            cursor = await db.execute(
                """
                SELECT tg_id, username, name, blocked_until, blocked_reason
                FROM users
                WHERE is_deleted = 0
                  AND is_blocked = 1
                ORDER BY blocked_until DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            users = [dict(r) for r in rows]

    return total, users


async def get_total_activity_events() -> int:
    """
    Считать суммарное количество ключевых действий пользователей:
    загрузки фото, оценки, супер-оценки, комментарии и жалобы.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM photos) +
                (SELECT COUNT(*) FROM ratings) +
                (SELECT COUNT(*) FROM super_ratings) +
                (SELECT COUNT(*) FROM comments) +
                (SELECT COUNT(*) FROM photo_reports)
            """
        )
        row = await cursor.fetchone()
        await cursor.close()

    if not row or row[0] is None:
        return 0
    return int(row[0])


async def get_users_with_multiple_daily_top3(
    min_wins: int = 2,
    limit: int = 50,
) -> list[dict]:
    """
    Найти пользователей, которые больше min_wins раз попадали в топ-3 дня.
    Основано на средних оценках по дням (логика аналогична get_daily_top_photos).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            WITH photo_stats AS (
                SELECT
                    p.id,
                    p.user_id,
                    p.day_key,
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
                WHERE p.is_deleted = 0
                  AND p.moderation_status = 'active'
                GROUP BY p.id, p.user_id, p.day_key
            ),
            ranked AS (
                SELECT
                    ps.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY ps.day_key
                        ORDER BY ps.avg_rating DESC, ps.ratings_count DESC, ps.id ASC
                    ) AS rn
                FROM photo_stats ps
                WHERE ps.avg_rating IS NOT NULL
            )
            SELECT
                u.tg_id,
                u.username,
                u.name,
                COUNT(*) AS wins_count
            FROM ranked r
            JOIN users u ON u.id = r.user_id
            WHERE r.rn <= 3
            GROUP BY r.user_id
            HAVING wins_count >= ?
            ORDER BY wins_count DESC, u.id ASC
            LIMIT ?
            """,
            (min_wins, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()

    return [dict(r) for r in rows]


async def save_pending_referral(tg_id: int, referral_code: str) -> None:
    """
    Сохранить отложенную реферальную инфу для пользователя, которого ещё может не быть в users.

    Хранит пару (tg_id, referral_code) в referral_pending, перезаписывая старое значение.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        now_iso = datetime.utcnow().isoformat(timespec="seconds")
        await db.execute(
            """
            INSERT INTO referral_pending (tg_id, referral_code, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                referral_code = excluded.referral_code,
                created_at = excluded.created_at
            """,
            (tg_id, referral_code, now_iso),
        )
        await db.commit()


async def link_and_reward_referral_if_needed(
    tg_id: int,
    bonus_days: int = 2,
) -> tuple[bool, int | None, int | None]:
    """
    Проверяет, нужно ли завершить реферальку для пользователя с данным tg_id.

    Логика:
    - Если referral_qualified = 1 — уже отработано, выходим.
    - Если referred_by_user_id None, но есть запись в referral_pending:
        ищем реферера по referral_code, записываем referred_by_user_id.
    - Если есть хотя бы одна оценка в ratings и есть реферер, а referral_qualified = 0:
        добавляем обоим по bonus_days премиума и ставим referral_qualified = 1.

    Возвращает:
        (success, referrer_tg_id, referee_tg_id)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # 1. Сам пользователь
        cursor = await db.execute(
            "SELECT id, referred_by_user_id, referral_qualified, is_deleted "
            "FROM users WHERE tg_id = ?",
            (tg_id,),
        )
        user_row = await cursor.fetchone()
        await cursor.close()

        if not user_row or user_row["is_deleted"]:
            return False, None, None

        user_id = int(user_row["id"])
        referred_by_user_id = user_row["referred_by_user_id"]
        referral_qualified = int(user_row["referral_qualified"] or 0)

        if referral_qualified:
            return False, None, None

        # 2. Если не знаем реферера — смотрим pending
        if referred_by_user_id is None:
            cursor = await db.execute(
                "SELECT referral_code FROM referral_pending WHERE tg_id = ?",
                (tg_id,),
            )
            pend_row = await cursor.fetchone()
            await cursor.close()

            if pend_row:
                ref_code = pend_row["referral_code"]

                cursor = await db.execute(
                    "SELECT id FROM users WHERE referral_code = ? AND is_deleted = 0 LIMIT 1",
                    (ref_code,),
                )
                ref_row = await cursor.fetchone()
                await cursor.close()

                if ref_row:
                    referred_by_user_id = int(ref_row["id"])
                    await db.execute(
                        "UPDATE users SET referred_by_user_id = ? WHERE id = ?",
                        (referred_by_user_id, user_id),
                    )

                await db.execute(
                    "DELETE FROM referral_pending WHERE tg_id = ?",
                    (tg_id,),
                )
                await db.commit()

        if referred_by_user_id is None:
            return False, None, None

        # 3. Проверяем, что человек реально кого-то оценил
        cursor = await db.execute(
            "SELECT COUNT(*) FROM ratings WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        ratings_count = int(row[0] or 0) if row else 0

        if ratings_count <= 0:
            return False, None, None

        # 4. Достаём обоих и выдаём прем
        cursor = await db.execute(
            "SELECT id, tg_id, premium_until, is_premium FROM users WHERE id IN (?, ?)",
            (user_id, referred_by_user_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        if not rows or len(rows) < 2:
            return False, None, None

        by_id = {int(r["id"]): r for r in rows}
        referee_row = by_id.get(user_id)
        referrer_row = by_id.get(referred_by_user_id)

        if not referee_row or not referrer_row:
            return False, None, None

        now = datetime.utcnow()

        def _calc_new_until(prev: str | None) -> str:
            base = now
            if prev:
                try:
                    prev_dt = datetime.fromisoformat(prev)
                    if prev_dt > now:
                        base = prev_dt
                except Exception:
                    pass
            new_dt = base + timedelta(days=bonus_days)
            return new_dt.isoformat(timespec="seconds")

        new_referee_until = _calc_new_until(referee_row["premium_until"])
        new_referrer_until = _calc_new_until(referrer_row["premium_until"])

        await db.execute(
            "UPDATE users SET premium_until = ?, is_premium = 1 WHERE id = ?",
            (new_referee_until, user_id),
        )
        await db.execute(
            "UPDATE users SET premium_until = ?, is_premium = 1 WHERE id = ?",
            (new_referrer_until, referred_by_user_id),
        )

        await db.execute(
            "UPDATE users SET referral_qualified = 1 WHERE id = ?",
            (user_id,),
        )

        await db.commit()

        referrer_tg_id = int(referrer_row["tg_id"]) if referrer_row["tg_id"] is not None else None
        referee_tg_id = int(referee_row["tg_id"]) if referee_row["tg_id"] is not None else None

        return True, referrer_tg_id, referee_tg_id