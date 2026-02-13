-- Smart activity & credits system (2026-02-13)

-- Photos: lifecycle + counters
ALTER TABLE photos ADD COLUMN IF NOT EXISTS submit_day DATE;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE photos ADD COLUMN IF NOT EXISTS votes_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS sum_score INTEGER NOT NULL DEFAULT 0;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS avg_score NUMERIC(6,3) NOT NULL DEFAULT 0;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS views_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS daily_views_budget INTEGER NOT NULL DEFAULT 0;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS tg_file_id TEXT;

CREATE INDEX IF NOT EXISTS idx_photos_submit_day ON photos(submit_day);
CREATE INDEX IF NOT EXISTS idx_photos_status_new ON photos(status);
CREATE INDEX IF NOT EXISTS idx_photos_expires_at ON photos(expires_at);

-- Votes (separate from legacy ratings)
CREATE TABLE IF NOT EXISTS votes (
    id BIGSERIAL PRIMARY KEY,
    photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    voter_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    score INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(photo_id, voter_id)
);
CREATE INDEX IF NOT EXISTS idx_votes_photo_created ON votes(photo_id, created_at);
CREATE INDEX IF NOT EXISTS idx_votes_voter_created ON votes(voter_id, created_at);

-- Per-user activity/credits
CREATE TABLE IF NOT EXISTS user_stats (
    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    credits INTEGER NOT NULL DEFAULT 0,
    show_tokens INTEGER NOT NULL DEFAULT 0,
    last_active_at TIMESTAMPTZ,
    votes_given_today INTEGER NOT NULL DEFAULT 0,
    votes_given_happyhour_today INTEGER NOT NULL DEFAULT 0,
    public_portfolio BOOLEAN NOT NULL DEFAULT FALSE
);

-- Unique views (anti-repeat)
CREATE TABLE IF NOT EXISTS photo_views (
    photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    viewer_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (photo_id, viewer_id)
);
CREATE INDEX IF NOT EXISTS idx_photo_views_viewer ON photo_views(viewer_id, created_at);

-- Final ranks per submit_day
CREATE TABLE IF NOT EXISTS result_ranks (
    photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    submit_day DATE NOT NULL,
    final_rank INTEGER NOT NULL,
    finalized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (photo_id, submit_day)
);

-- Notification queue (batched sending)
CREATE TABLE IF NOT EXISTS notification_queue (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    run_after TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notification_queue_status_run ON notification_queue(status, run_after);

-- Duels (skeleton)
CREATE TABLE IF NOT EXISTS duels (
    id BIGSERIAL PRIMARY KEY,
    photo_a_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    photo_b_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    reward_credits INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS duel_votes (
    duel_id BIGINT NOT NULL REFERENCES duels(id) ON DELETE CASCADE,
    voter_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    choice TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (duel_id, voter_id)
);

-- Collabs (skeleton)
CREATE TABLE IF NOT EXISTS collabs (
    id BIGSERIAL PRIMARY KEY,
    photo_a_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    photo_b_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    author_a_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    author_b_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS collab_votes (
    collab_id BIGINT NOT NULL REFERENCES collabs(id) ON DELETE CASCADE,
    voter_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    score INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (collab_id, voter_id)
);
