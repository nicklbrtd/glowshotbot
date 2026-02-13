-- Polish pass: tail, daily author votes, delete reasons (2026-02-13)

ALTER TABLE photos ADD COLUMN IF NOT EXISTS deleted_reason TEXT;
ALTER TABLE photos ALTER COLUMN deleted_at TYPE TIMESTAMPTZ USING deleted_at::timestamptz;

CREATE TABLE IF NOT EXISTS daily_author_votes (
    day DATE NOT NULL,
    voter_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    author_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cnt INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(day, voter_id, author_id)
);

CREATE INDEX IF NOT EXISTS idx_daily_author_votes_voter_day ON daily_author_votes(voter_id, day);
