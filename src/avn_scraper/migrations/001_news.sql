-- The news watch: articles from the scraper and the stories the team accepts.
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public;

CREATE TABLE sources (
    id TEXT PRIMARY KEY, name TEXT, language TEXT, type TEXT, district TEXT, homepage TEXT
);

CREATE TABLE articles (
    id TEXT PRIMARY KEY,                 -- hash of the canonical URL
    url TEXT NOT NULL,
    canonical_url TEXT,
    source_id TEXT,                      -- no foreign key: catch-all results use web:<host>
    source_name TEXT,
    source_type TEXT,
    language TEXT,
    title TEXT,
    summary TEXT,
    body TEXT,
    author TEXT,
    image TEXT,
    published_at TIMESTAMPTZ,
    published_date DATE NOT NULL,        -- in Asia/Dhaka (falls back to scrape date)
    scraped_at TIMESTAMPTZ NOT NULL,
    discovered_via TEXT,                 -- rss | sitemap | listing | catchall
    district TEXT,                       -- best guess
    districts_mentioned TEXT[] NOT NULL DEFAULT '{}',
    story_cluster_id TEXT,               -- same story across outlets share this id
    body_missing BOOLEAN NOT NULL DEFAULT FALSE,
    raw_path TEXT,                       -- under data_dir; raw pages stay on disk
    run_id TEXT
);
CREATE INDEX idx_articles_date ON articles(published_date);
CREATE INDEX idx_articles_scraped ON articles(scraped_at);
CREATE INDEX idx_articles_source ON articles(source_id);
CREATE INDEX idx_articles_cluster ON articles(story_cluster_id);
CREATE INDEX idx_articles_title_trgm ON articles USING gin (title public.gin_trgm_ops);
CREATE INDEX idx_articles_body_trgm ON articles USING gin (body public.gin_trgm_ops);

CREATE TABLE article_categories (
    article_id TEXT REFERENCES articles(id) ON DELETE CASCADE,
    category TEXT,
    score INTEGER,
    matched_terms JSONB NOT NULL DEFAULT '[]',
    PRIMARY KEY (article_id, category)
);
CREATE INDEX idx_cat_category ON article_categories(category);

-- Manual review: verified = 1 relevant, 0 false positive, NULL not reviewed.
CREATE TABLE review (
    article_id TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    verified INTEGER,
    notes TEXT,
    reviewed_at TIMESTAMPTZ
);

-- URLs fetched but not stored (no match / too old), so they are not re-fetched.
CREATE TABLE seen_urls (
    id TEXT PRIMARY KEY, url TEXT, status TEXT, first_seen TIMESTAMPTZ
);

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, stats JSONB
);

-- Stories the team accepted. A copy of the story at that moment, not a link:
-- what was accepted stays as it was if the articles change later.
CREATE TABLE accepted (
    id TEXT PRIMARY KEY,
    story JSONB NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL,
    accepted_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_accepted_at ON accepted(accepted_at DESC);

-- One row per (article, category): the main view for analysis.
CREATE VIEW v_matches AS
SELECT a.published_date, c.category, a.source_name, a.source_type, a.language,
       a.title, a.url, a.district, c.score, c.matched_terms, a.story_cluster_id,
       a.published_at, a.scraped_at, a.id AS article_id, r.verified, r.notes
FROM article_categories c
JOIN articles a ON a.id = c.article_id
LEFT JOIN review r ON r.article_id = a.id;
