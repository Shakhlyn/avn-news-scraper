-- Block logs (data/logs/blocks/<scrape_date>/<HHMMSS>_<mode>.jsonl), imported once a
-- scrape ends. Kept permanently to study how and when sites block us.

-- One row per scrape: the whole log file as written, plus what identifies the run.
CREATE TABLE block_log_runs (
    run_id TEXT PRIMARY KEY,               -- UTC start, e.g. 20261118T025935Z
    mode TEXT NOT NULL,                    -- run | dry-run | check-sources
    scrape_date DATE NOT NULL,             -- day of the scrape in Asia/Dhaka
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,               -- NULL when the scrape died before its end
    window_from TIMESTAMPTZ,               -- news window the run covered (run / dry-run)
    window_to TIMESTAMPTZ,
    machine TEXT,
    git_commit TEXT,
    file_name TEXT NOT NULL,               -- path under data/logs/blocks/
    totals JSONB,
    raw JSONB NOT NULL,                    -- every line of the file, in order
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX block_log_runs_date ON block_log_runs(scrape_date);

-- One row per host per scrape (successes are counted here, not logged one by one).
CREATE TABLE block_log_hosts (
    run_id TEXT NOT NULL REFERENCES block_log_runs(run_id) ON DELETE CASCADE,
    host TEXT NOT NULL,
    source TEXT,
    client TEXT,                           -- plain | chrome
    requests INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    blocked INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    blocks JSONB,                          -- {"challenge": 3, "rate_limited": 1}
    statuses JSONB,
    first_block_seq INTEGER,               -- request number to this host of the first block
    stopped TEXT,                          -- breaker reason, NULL when not stopped
    PRIMARY KEY (run_id, host)
);

-- One row per failed request or breaker stop: the table to analyse.
CREATE TABLE block_log_events (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES block_log_runs(run_id) ON DELETE CASCADE,
    scrape_date DATE NOT NULL,
    mode TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    event TEXT NOT NULL,                   -- request | host_stopped
    host TEXT NOT NULL,
    source TEXT,
    client TEXT,
    purpose TEXT,                          -- homepage | tab | feed | sitemap | robots | article | api | search
    outcome TEXT,                          -- block | error | server_error
    block TEXT,                            -- challenge | rate_limited | forbidden
    status INTEGER,
    attempt INTEGER,
    host_seq INTEGER,
    gap_since_prev_s REAL,
    waited_s REAL,
    elapsed_s REAL,
    ok_before INTEGER,
    blocks_before INTEGER,
    strikes INTEGER,
    bytes INTEGER,
    server TEXT,
    cf_ray TEXT,
    cf_colo TEXT,
    cf_mitigated TEXT,
    retry_after TEXT,
    content_type TEXT,
    title TEXT,
    url TEXT,
    final_url TEXT,
    error TEXT,
    sample TEXT,
    reason TEXT,                           -- host_stopped only
    until TIMESTAMPTZ                      -- host_stopped only: cooldown end
);
CREATE INDEX block_log_events_host ON block_log_events(host, ts);
CREATE INDEX block_log_events_date ON block_log_events(scrape_date);
