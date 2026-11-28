-- 001_b_message: household messaging (contracts/events.md, C-4).
--
-- One row per message per household per channel. 'sms' rows mirror what
-- went to the SMS provider (the mock today); 'app' rows are the
-- household's in-app alert feed. Written only by cloud/messaging.notify.

-- Language a household receives messages in.
ALTER TABLE household
  ADD COLUMN IF NOT EXISTS lang TEXT NOT NULL DEFAULT 'kn' CHECK (lang IN ('en', 'kn'));

CREATE TABLE IF NOT EXISTS message (
  id           BIGSERIAL PRIMARY KEY,
  -- CASCADE: re-recording a run (sim/loop.py) and rebuilding the world
  -- (world/build.py) delete runs and households without knowing this table.
  run_id       TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
  household_id TEXT NOT NULL REFERENCES household(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,                 -- template key: load_shedding_notice, grid_loss, dr_offer, ...
  channel      TEXT NOT NULL CHECK (channel IN ('sms', 'app', 'ivr')),
  lang         TEXT NOT NULL CHECK (lang IN ('en', 'kn')),
  body         TEXT NOT NULL,
  payload      JSONB,                         -- the fields the body was rendered from
  sent_at      TIMESTAMPTZ NOT NULL,          -- simulated time of sending, not wall-clock
  delivered    BOOLEAN NOT NULL,
  error        TEXT,                          -- why delivery failed, if it did
  provider_id  TEXT,                          -- SMS provider's message id
  dedupe_key   TEXT,                          -- same key + household + channel is sent once per run
  reply        TEXT,
  replied_at   TIMESTAMPTZ,
  read_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_message_feed ON message (run_id, household_id, sent_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_message_dedupe
  ON message (run_id, household_id, channel, dedupe_key) WHERE dedupe_key IS NOT NULL;
