-- Archive UI performance indexes
-- Safe to run multiple times.

CREATE INDEX IF NOT EXISTS idx_photos_user_status_submit_created
ON photos(user_id, status, submit_day, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_result_ranks_photo_submit
ON result_ranks(photo_id, submit_day DESC);
