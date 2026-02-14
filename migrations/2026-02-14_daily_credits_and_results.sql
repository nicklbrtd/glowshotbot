-- GlowShot 2.1: daily credits, daily results cache, lifecycle alignment (2026-02-14)

-- Photos lifecycle fields (idempotent)
ALTER TABLE photos ADD COLUMN IF NOT EXISTS submit_day DATE;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE photos ADD COLUMN IF NOT EXISTS deleted_reason TEXT;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE photos
    ALTER COLUMN deleted_at TYPE TIMESTAMPTZ
    USING (
        CASE
            WHEN deleted_at IS NULL THEN NULL
            WHEN deleted_at::text ~ '^\d{4}-\d{2}-\d{2}' THEN deleted_at::timestamptz
            ELSE NULL
        END
    );

-- user_stats daily grant guard (idempotent)
ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS last_daily_grant_day DATE;

-- Cache of published daily results (available to all users + archive by dates)
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

-- Helpful indexes
CREATE INDEX IF NOT EXISTS idx_photos_status_expires ON photos(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_photos_submit_status ON photos(submit_day, status);
CREATE INDEX IF NOT EXISTS idx_notification_queue_status_run ON notification_queue(status, run_after);
CREATE INDEX IF NOT EXISTS idx_result_ranks_submit_day ON result_ranks(submit_day, final_rank);
CREATE INDEX IF NOT EXISTS idx_daily_results_cache_published ON daily_results_cache(published_at DESC, submit_day DESC);
CREATE INDEX IF NOT EXISTS idx_user_stats_daily_grant ON user_stats(last_daily_grant_day, user_id);

-- Backfill submit_day from submit_day/day_key/created_at
WITH base AS (
    SELECT
        p.id,
        COALESCE(
            p.submit_day,
            CASE
                WHEN NULLIF(p.day_key, '') ~ '^\d{4}-\d{2}-\d{2}$' THEN NULLIF(p.day_key, '')::date
                ELSE NULL
            END,
            CASE
                WHEN NULLIF(p.created_at, '') ~ '^\d{4}-\d{2}-\d{2}' THEN ((p.created_at)::timestamptz AT TIME ZONE 'Europe/Moscow')::date
                ELSE (NOW() AT TIME ZONE 'Europe/Moscow')::date
            END
        ) AS sd
    FROM photos p
)
UPDATE photos p
SET submit_day = b.sd
FROM base b
WHERE p.id = b.id
  AND p.submit_day IS NULL;

-- Align expires_at for active photos to end of submit_day+1 in bot timezone (Europe/Moscow)
WITH base AS (
    SELECT
        p.id,
        COALESCE(
            p.submit_day,
            CASE
                WHEN NULLIF(p.day_key, '') ~ '^\d{4}-\d{2}-\d{2}$' THEN NULLIF(p.day_key, '')::date
                ELSE NULL
            END,
            CASE
                WHEN NULLIF(p.created_at, '') ~ '^\d{4}-\d{2}-\d{2}' THEN ((p.created_at)::timestamptz AT TIME ZONE 'Europe/Moscow')::date
                ELSE (NOW() AT TIME ZONE 'Europe/Moscow')::date
            END
        ) AS sd
    FROM photos p
)
UPDATE photos p
SET expires_at = timezone('Europe/Moscow', ((b.sd + 2)::timestamp)) - interval '1 microsecond'
FROM base b
WHERE p.id = b.id
  AND COALESCE(p.status, 'active') = 'active';

-- Archive already-expired active photos
UPDATE photos
SET status = 'archived'
WHERE COALESCE(status, 'active') = 'active'
  AND expires_at IS NOT NULL
  AND expires_at <= NOW();
