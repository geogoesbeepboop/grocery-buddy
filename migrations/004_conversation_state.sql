-- Per-user conversation state for the Telegram chat channel.
--
-- The Telegram webhook is stateless (each message is a separate HTTP POST), so
-- multi-turn flows like onboarding need their transcript persisted between calls.
--
-- mode      : 'idle' (normal command routing) | 'onboarding' (mid-interview)
-- messages  : the Anthropic message history for the active flow (JSON array)

CREATE TABLE IF NOT EXISTS conversation_state (
    user_id    UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    mode       TEXT NOT NULL DEFAULT 'idle',
    messages   JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
