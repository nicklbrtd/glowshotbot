-- ===== users: роли/премиум/рефералки/настройки =====
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_deleted        int DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin          int DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_moderator      int DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_helper         int DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_support        int DEFAULT 0;

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_premium        int DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until     text;

ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_likes      int DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_comments   int DEFAULT 1;

ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code           text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by_user_id     int;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_qualified      int DEFAULT 0;

ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_skip_date   text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_skip_count  int DEFAULT 0;

ALTER TABLE users ADD COLUMN IF NOT EXISTS tg_channel_link   text;

-- ===== photos: модерация / повторы / день =====
ALTER TABLE photos ADD COLUMN IF NOT EXISTS is_deleted         int DEFAULT 0;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS moderation_status  text DEFAULT 'active';
ALTER TABLE photos ADD COLUMN IF NOT EXISTS repeat_used        int DEFAULT 0;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS day_key            text;

-- ===== ratings: уникальность =====
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'ratings_user_photo_uniq'
    ) THEN
        CREATE UNIQUE INDEX ratings_user_photo_uniq ON ratings (user_id, photo_id);
    END IF;
END $$;

-- ===== super_ratings =====
CREATE TABLE IF NOT EXISTS super_ratings (
    user_id   int NOT NULL,
    photo_id  int NOT NULL,
    created_at text,
    PRIMARY KEY (user_id, photo_id)
);

-- ===== photo_reports =====
CREATE TABLE IF NOT EXISTS photo_reports (
    id         bigserial PRIMARY KEY,
    user_id    int NOT NULL,
    photo_id   int NOT NULL,
    reason     text NOT NULL,
    text       text,
    status     text DEFAULT 'pending',
    created_at text
);

CREATE INDEX IF NOT EXISTS photo_reports_photo_id_idx ON photo_reports(photo_id);
CREATE INDEX IF NOT EXISTS photo_reports_status_idx ON photo_reports(status);

-- ===== payments =====
CREATE TABLE IF NOT EXISTS payments (
    id bigserial PRIMARY KEY,
    user_id int NOT NULL,
    method text NOT NULL,
    period_code text,
    days int NOT NULL,
    amount int NOT NULL,
    currency text NOT NULL,
    created_at text,
    telegram_charge_id text,
    provider_charge_id text
);

CREATE INDEX IF NOT EXISTS payments_user_id_idx ON payments(user_id);
CREATE INDEX IF NOT EXISTS payments_created_at_idx ON payments(created_at);

-- ===== awards =====
CREATE TABLE IF NOT EXISTS awards (
    id bigserial PRIMARY KEY,
    user_id int NOT NULL,
    code text NOT NULL,
    title text,
    description text,
    icon text,
    is_special int DEFAULT 0,
    granted_by_user_id int,
    created_at text
);

CREATE INDEX IF NOT EXISTS awards_user_id_idx ON awards(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS awards_user_code_uniq ON awards(user_id, code);

-- ===== user_upload_bans (используется в get_user_admin_stats) =====
CREATE TABLE IF NOT EXISTS user_upload_bans (
    id bigserial PRIMARY KEY,
    user_id int NOT NULL,
    until_at text,
    reason text,
    created_at text
);

CREATE INDEX IF NOT EXISTS user_upload_bans_user_id_idx ON user_upload_bans(user_id);