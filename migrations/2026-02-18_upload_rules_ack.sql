ALTER TABLE user_stats
    ADD COLUMN IF NOT EXISTS upload_rules_ack_at TIMESTAMPTZ;
