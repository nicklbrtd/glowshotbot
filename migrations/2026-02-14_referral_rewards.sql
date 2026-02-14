-- Referral rewards idempotency + backfill (2026-02-14)

CREATE TABLE IF NOT EXISTS referral_rewards (
    id BIGSERIAL PRIMARY KEY,
    invited_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    inviter_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rewarded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reward_type TEXT NOT NULL DEFAULT 'premium_credits',
    reward_version TEXT NOT NULL DEFAULT 'v2_3h_2c',
    UNIQUE(invited_user_id)
);

CREATE INDEX IF NOT EXISTS idx_referral_rewards_inviter ON referral_rewards(inviter_user_id);
CREATE INDEX IF NOT EXISTS idx_referral_rewards_rewarded_at ON referral_rewards(rewarded_at DESC);

-- Backfill rewards from already qualified referrals (legacy logic)
INSERT INTO referral_rewards (invited_user_id, inviter_user_id, rewarded_at, reward_type, reward_version)
SELECT
    r.invited_user_id,
    r.inviter_user_id,
    CASE
        WHEN COALESCE(NULLIF(TRIM(r.qualified_at), ''), '') ~ '^\\d{4}-\\d{2}-\\d{2}'
            THEN r.qualified_at::timestamptz
        ELSE NOW()
    END,
    'premium_credits',
    'legacy_pre_v2_3h_2c'
FROM referrals r
WHERE COALESCE(r.qualified, 0) = 1
ON CONFLICT (invited_user_id) DO NOTHING;
