-- 0005_mail_outbox.sql -- the one mail path (mailer.py): idempotency per Message-ID, retries,
-- resume after restart, per-recipient rate limit. No plaintext address (SHA-256 only); the MIME
-- blob (contains the mail) is kept only until delivery and set NULL afterwards.
CREATE TABLE IF NOT EXISTS mail_outbox (
    message_id      TEXT PRIMARY KEY,
    recipient_hash  TEXT NOT NULL,
    audience        TEXT NOT NULL CHECK (audience IN ('doi', 'confirmed', 'internal')),
    mime            BLOB,
    state           TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'sent', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    done_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_mail_outbox_rcpt ON mail_outbox(recipient_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_mail_outbox_state ON mail_outbox(state);
