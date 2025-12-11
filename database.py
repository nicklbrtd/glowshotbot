import aiosqlite
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
                notify_comments INTEGER NOT NULL DEFAULT 1
                ,
                is_blocked INTEGER NOT NULL DEFAULT 0,
                blocked_until TEXT,
                blocked_reason TEXT
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
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
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
            """
        )

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
                        SUM(
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
                        SUM(
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
                        SUM(
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
                        SUM(
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
) -> int:
    """
    Выдать награду пользователю.
    code — внутренний код награды (можно использовать для проверки дублей),
    title — заголовок, icon — эмодзи или короткий маркер.
    """
    now = get_moscow_now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO awards (user_id, code, title, description, icon, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, code, title, description, icon, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_awards_for_user(user_id: int) -> list[dict]:
    """
    Получить список всех наград пользователя.
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