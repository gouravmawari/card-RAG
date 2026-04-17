-- ============================================================================
-- CurMath Flashcard Engine — Schema v2
-- Run this in Supabase SQL Editor.
-- DESTRUCTIVE: drops columns/tables. Safe only if no production data.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Drop obsolete tables (replaced by sessions + user_topic_stats)
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS public.user_schedule CASCADE;
DROP TABLE IF EXISTS public.user_stats CASCADE;

-- ----------------------------------------------------------------------------
-- 2. Drop obsolete columns
-- ----------------------------------------------------------------------------
-- Supabase Auth owns passwords; no plaintext in public.users.
ALTER TABLE public.users DROP COLUMN IF EXISTS password;

-- FSRS state was moved to per-user tracking; user picks retest date manually.
ALTER TABLE public.cards DROP COLUMN IF EXISTS stability CASCADE;
ALTER TABLE public.cards DROP COLUMN IF EXISTS difficulty_score CASCADE;
ALTER TABLE public.cards DROP COLUMN IF EXISTS last_review CASCADE;
ALTER TABLE public.cards DROP COLUMN IF EXISTS next_review CASCADE;

-- source_type on cards is derivable from sources.source_type via join.
ALTER TABLE public.cards DROP COLUMN IF EXISTS source_type CASCADE;

-- ----------------------------------------------------------------------------
-- 3. Augment sources (async-ingestion observability)
-- ----------------------------------------------------------------------------
ALTER TABLE public.sources
  ADD COLUMN IF NOT EXISTS status text
    CHECK (status IN ('pending','processing','completed','failed'))
    DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS topics        jsonb    DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS chunk_count   integer  DEFAULT 0,
  ADD COLUMN IF NOT EXISTS processed_at  timestamptz,
  ADD COLUMN IF NOT EXISTS error_message text,
  ADD COLUMN IF NOT EXISTS subject       text,
  ADD COLUMN IF NOT EXISTS board         text,
  ADD COLUMN IF NOT EXISTS chapter       text;

-- ----------------------------------------------------------------------------
-- 4. Create sessions (first-class — drives retest scheduling)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.sessions (
  session_id        uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid         NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
  source_id         text         NOT NULL REFERENCES public.sources(source_id) ON DELETE CASCADE,
  num_cards         integer      NOT NULL CHECK (num_cards BETWEEN 1 AND 15),
  status            text         NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled','in_progress','completed','abandoned')),
  scheduled_for     timestamptz,
  started_at        timestamptz,
  completed_at      timestamptz,
  final_report_json jsonb,
  created_at        timestamptz  DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- 5. Evolve cards: attach to a session + direct user_id for fast user queries
-- ----------------------------------------------------------------------------
ALTER TABLE public.cards
  ADD COLUMN IF NOT EXISTS session_id uuid REFERENCES public.sessions(session_id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS user_id    uuid REFERENCES public.users(user_id)      ON DELETE CASCADE;

-- Tighten card_type to the 4 types actually used.
ALTER TABLE public.cards DROP CONSTRAINT IF EXISTS cards_card_type_check;
ALTER TABLE public.cards ADD  CONSTRAINT cards_card_type_check
  CHECK (card_type IN ('long_answer','mcq','true_false','spot_the_error'));

-- ----------------------------------------------------------------------------
-- 6. Evolve user_reviews: hint tracking + deterministic correctness + proper FK
-- ----------------------------------------------------------------------------
-- session_id was text; convert to uuid FK. Drop + re-add since no prod data.
ALTER TABLE public.user_reviews DROP COLUMN IF EXISTS session_id CASCADE;

ALTER TABLE public.user_reviews
  ADD COLUMN session_id uuid REFERENCES public.sessions(session_id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS used_hint  boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS is_correct boolean;
-- is_correct is NULL for long_answer (uses feedback_json ratings instead),
-- true/false for mcq + true_false cards.

-- ----------------------------------------------------------------------------
-- 7. Create user_topic_stats (live-maintained — drives weak/strong report)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_topic_stats (
  user_id         uuid        NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
  topic           text        NOT NULL,
  correct_count   integer     NOT NULL DEFAULT 0,
  incorrect_count integer     NOT NULL DEFAULT 0,
  skipped_count   integer     NOT NULL DEFAULT 0,
  hinted_count    integer     NOT NULL DEFAULT 0,
  last_seen_at    timestamptz DEFAULT now(),
  PRIMARY KEY (user_id, topic)
);

-- ----------------------------------------------------------------------------
-- 8. Indexes for hot query paths
-- ----------------------------------------------------------------------------
-- Cards lookups
CREATE INDEX IF NOT EXISTS idx_cards_user           ON public.cards (user_id);
CREATE INDEX IF NOT EXISTS idx_cards_session        ON public.cards (session_id);
CREATE INDEX IF NOT EXISTS idx_cards_source         ON public.cards (source_id);
CREATE INDEX IF NOT EXISTS idx_cards_topic_filter   ON public.cards (subject, board, chapter, topic);

-- Session lookups (user dashboard: upcoming/recent sessions)
CREATE INDEX IF NOT EXISTS idx_sessions_user_date   ON public.sessions (user_id, scheduled_for DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_source      ON public.sessions (source_id);
CREATE INDEX IF NOT EXISTS idx_sessions_active      ON public.sessions (status)
  WHERE status IN ('scheduled','in_progress');

-- Review lookups (your requirement: one query to get all hinted/skipped for a user)
CREATE INDEX IF NOT EXISTS idx_reviews_user_hint    ON public.user_reviews (user_id) WHERE used_hint  = true;
CREATE INDEX IF NOT EXISTS idx_reviews_user_skipped ON public.user_reviews (user_id) WHERE is_skipped = true;
CREATE INDEX IF NOT EXISTS idx_reviews_user_created ON public.user_reviews (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_session      ON public.user_reviews (session_id);

-- Weak-topic lookups (for generating focused retests)
CREATE INDEX IF NOT EXISTS idx_topic_stats_weak     ON public.user_topic_stats (user_id, incorrect_count DESC);

-- Sources by owner (user's past PDFs scroll list)
CREATE INDEX IF NOT EXISTS idx_sources_user         ON public.sources (user_id, created_at DESC);

-- ----------------------------------------------------------------------------
-- 9. Uniqueness — one review per (user, card)
-- ----------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_user_card
  ON public.user_reviews (user_id, card_id);

-- ============================================================================
-- NOTE ON RLS (Row-Level Security):
-- The backend uses SUPABASE_SERVICE_ROLE_KEY which bypasses RLS, so RLS is not
-- strictly required today. If the frontend ever calls Supabase directly with
-- the anon key, enable RLS on: sessions, user_reviews, user_topic_stats, cards,
-- sources — with policies like `user_id = auth.uid()`. Not included here to
-- keep this migration focused.
-- ============================================================================
