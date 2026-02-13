-- Party finalization & migration fixes (2026-02-13)

-- New flags/columns
ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS migration_notified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE notification_queue ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notification_queue ADD COLUMN IF NOT EXISTS last_error TEXT;

-- Helpful composite indexes
CREATE INDEX IF NOT EXISTS idx_photos_status_expires ON photos(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_photos_submit_status ON photos(submit_day, status);
CREATE INDEX IF NOT EXISTS idx_votes_photo ON votes(photo_id);
CREATE INDEX IF NOT EXISTS idx_votes_voter_created ON votes(voter_id, created_at);
CREATE INDEX IF NOT EXISTS idx_photo_views_viewer_created ON photo_views(viewer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_user_stats_user_id ON user_stats(user_id);
CREATE INDEX IF NOT EXISTS idx_notification_queue_status_run ON notification_queue(status, run_after);

-- Backfill submit_day / expires_at for legacy photos
WITH updated AS (
    UPDATE photos p
    SET submit_day = COALESCE(
            p.submit_day,
            (p.created_at)::timestamptz AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Moscow')::date,
        expires_at = GREATEST(
            p.expires_at,
            timezone('Europe/Moscow', ((COALESCE(p.submit_day, (p.created_at)::timestamptz AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Moscow')::date) + 1)::timestamp) - interval '1 microsecond' + interval '72 hours'),
            timezone('Europe/Moscow', (CURRENT_DATE + 1)::timestamp) - interval '1 microsecond' + interval '72 hours'
            )
    WHERE p.is_deleted = 0
      AND COALESCE(p.status,'active') <> 'archived'
    RETURNING p.id, p.user_id, p.submit_day, p.expires_at
)
SELECT 1;

-- Ensure status aligns with expiry
UPDATE photos
SET status = CASE WHEN expires_at <= NOW() THEN 'archived' ELSE 'active' END
WHERE is_deleted = 0;

-- Seed user_stats rows for photo owners
INSERT INTO user_stats (user_id)
SELECT DISTINCT user_id FROM photos
ON CONFLICT (user_id) DO NOTHING;

-- One-time migration notification with jittered run_after
INSERT INTO notification_queue (user_id, type, payload, run_after, status)
SELECT DISTINCT p.user_id,
       'migration_notice',
       jsonb_build_object(
         'photo_id', p.id,
         'expires_at', p.expires_at,
         'submit_day', p.submit_day
       ),
       NOW() + (floor(random()*900)) * interval '1 second',
       'pending'
FROM photos p
JOIN user_stats us ON us.user_id = p.user_id
WHERE p.is_deleted = 0
  AND COALESCE(p.status,'active') = 'active'
  AND us.migration_notified = FALSE;

UPDATE user_stats SET migration_notified = TRUE WHERE migration_notified = FALSE;
