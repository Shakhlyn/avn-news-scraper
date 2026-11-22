-- Backfill by day as well as by month (avn backfill 2024-08-05, 2024-08-01..2024-08-07).
-- A period is `month` (its first day, name kept from 003) to `until` (its last day, included).
-- Rows from before are whole months.
ALTER TABLE backfill_progress ADD COLUMN until DATE;
UPDATE backfill_progress SET until = (month + interval '1 month' - interval '1 day')::date;
ALTER TABLE backfill_progress ALTER COLUMN until SET NOT NULL;
ALTER TABLE backfill_progress DROP CONSTRAINT backfill_progress_pkey;
ALTER TABLE backfill_progress ADD PRIMARY KEY (source_id, month, until);
