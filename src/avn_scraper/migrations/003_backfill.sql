-- Backfill of past months (avn backfill): one row per outlet per month, so a stopped
-- or interrupted backfill resumes where it was.
CREATE TABLE backfill_progress (
    source_id TEXT NOT NULL,
    month DATE NOT NULL,                   -- first day of the month
    status TEXT NOT NULL,                  -- done | partial | no_archive | failed
    layout TEXT,                           -- daily | monthly | yearly | lastmod | none
    candidates INTEGER,
    downloaded INTEGER,
    saved INTEGER,
    stats JSONB,
    run_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, month)
);
