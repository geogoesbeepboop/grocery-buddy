-- 008_amazon_auth_challenges.sql
--
-- Self-healing Amazon re-login. When the saved browser session expires, the
-- import workflow now re-authenticates on its own — filling stored credentials
-- (unattended) or opening a login window — instead of asking the user to run a
-- terminal command.
--
-- If Amazon prompts for a 2FA one-time code during that automated login, the
-- worker can't read the user's authenticator. So it relays the prompt to the user
-- over Telegram and waits for them to reply with the code. This table is the
-- mailbox that carries that code from the webhook process (which receives the
-- reply) back to the worker activity (which is holding the browser open on the
-- OTP page, polling for it).

CREATE TABLE IF NOT EXISTS amazon_auth_challenges (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'otp' CHECK (kind IN ('otp')),
    -- pending  → the login activity is waiting for the user's code
    -- answered → the user replied; `code` holds it, not yet consumed
    -- consumed → the activity read the code and submitted it to Amazon
    -- expired  → the activity gave up (timeout) or a newer challenge superseded it
    status      TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'answered', 'consumed', 'expired')),
    code        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    answered_at TIMESTAMPTZ
);

-- The activity looks up "my latest pending challenge"; the webhook writes the
-- code onto it. Both are user-scoped, newest-first.
CREATE INDEX IF NOT EXISTS idx_amazon_auth_user_status
    ON amazon_auth_challenges (user_id, status, created_at DESC);
