-- 1. Add FSRS columns to the 'cards' table
ALTER TABLE cards
ADD COLUMN IF NOT EXISTS stability FLOAT DEFAULT 1.0,
ADD COLUMN IF NOT EXISTS difficulty_score FLOAT DEFAULT 5.0,
ADD COLUMN IF NOT EXISTS last_review TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS next_review TIMESTAMPTZ;

-- 2. Create 'user_reviews' table to track history
-- This is essential for analytics and potential model training later
CREATE TABLE IF NOT EXISTS user_reviews (
    review_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id UUID REFERENCES cards(card_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    rating INTEGER NOT NULL, -- 1 (Again), 2 (Hard), 3 (Good), 4 (Easy)
    reviewed_at TIMESTAMPTZ DEFAULT now(),
    metadata JSONB -- To store extra context if needed (e.g., session info)
);

-- 3. Add index on next_review for faster retrieval of due cards
CREATE INDEX IF NOT EXISTS idx_cards_next_review ON cards(next_review);

-- 4. Add index on user_reviews for history lookup
CREATE INDEX IF NOT EXISTS idx_user_reviews_card_id ON user_reviews(card_id);
