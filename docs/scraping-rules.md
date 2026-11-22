# Scraping rules

These rules say what the scraper reads, how far back it goes, and what it keeps.
Change the code only together with this file. Settings live in `config/settings.yaml`
and outlets in `config/sources.yaml`.

## Schedule and time window

- The service (`./serve.sh`) runs the job **once a day at 23:30 Asia/Dhaka**, while it is running.
  Nothing on the PC starts it automatically.
- Each run looks back **30 hours** (`window_hours: 30`): from 17:30 the day before to now.
  The 6 hours that overlap the previous run catch stories it missed (published late, or
  listed late). Links the previous run already stored or judged (`seen_urls`) are skipped,
  so the overlap downloads nothing twice and news is not repeated across days.
- If a day was missed (PC off, run failed), catch up with `avn run --since 54`, or with
  24 × the number of days missed + 6.
- Older news (past months, years) is scraped on demand with `avn backfill` (see below).
- An article is stored once. Links that were downloaded and didn't match are remembered
  and not downloaded again.

## What is read for each outlet

Every enabled outlet in `sources.yaml` is read in this order:

1. **Homepage**: all headline links.
2. **Print edition / e-paper**: when the homepage links to an article list of the print
   paper on the outlet's own site, it is read. Examples: `/printversion`, `/print-version`,
   `/printed`, `/todays-paper`, `/epaper/<date>`, "আজকের পত্রিকা", "ছাপা সংস্করণ".
   It is found by the link's path or text.
3. **Every top-level tab** of the main menu: national, international, latest, সারাদেশ,
   politics, sports, etc. They are found from the homepage menu on every run, so new or
   renamed tabs are picked up automatically.
4. **RSS feeds and news sitemaps**, auto-discovered from the homepage and `robots.txt`.
5. Optional **search catch-all** (Bing News RSS), off by default.

### Which menu links count as tabs

A link is scanned as a tab when it:
- is in the site's main menu (`<nav>`, `<header>`, or an element whose class/id has
  "menu"/"nav"), but **not in the footer**;
- is a **top-level** menu item, not an item inside a dropdown under another tab.
  A tab whose item opens a dropdown is still top-level;
- is on the outlet's own host (`epaper.`, `en.` and other subdomains are not tabs);
- looks like a section, not an article: at most 3 path segments, no long number id,
  and a short label (at most 4 words).

These are **never** scanned: tags, topics, videos, photos/galleries, logins/registration,
search, archive, about/contact/privacy/terms, ads, subscriptions, RSS/sitemaps, authors,
podcasts, live, polls, quizzes, horoscope, converters, apps, press releases, jobs.
The full list is `_NOT_SECTION` in `src/avn_scraper/discover/sections.py`.

On a homepage or tab page, links to tag, topic, category, author, video, photo, search and
login pages (`/category/x`, `/category?catid=1`) and page-number links (`?page=2`,
`&pageno=3`) are not taken as articles, so they are neither downloaded nor used as date probes.

Tabs listed under `sections:` for an outlet in `sources.yaml` are always scanned as well.
`auto_sections: false` turns auto-detection off for that outlet, so only the listed tabs
are scanned. Use this for outlets whose menu is built by JavaScript (`avn check-sources`
then shows 0 tabs).

### How far each tab is read (pagination)

Each tab (and the print edition) is read page by page, following its "next page" link
(`rel=next`, `?page=N`, `/page/N`). Reading stops at the first of:

1. **Window reached**: most dated items on the page are older than the window. Dates
   come from `<time datetime>` on the page, or else from dates in the article URLs.
   **Regional outlets** mostly show no such dates on their lists (only text like
   "২০ নভেম্বর" or "১ মাস আগে", next to the header's "today" date). For a page without dates,
   the last `sections.date_probe` (3) articles on it are downloaded and their own dates read:
   when all are older than the window, the tab stops. The bottom of the page is checked, not
   the top, because local sites often pin older stories above the newest. Links also on the
   homepage ("latest" / "most read" widgets) are not sampled. Probed articles are not
   downloaded again: old ones are dropped, the rest go on to classification.
2. **Previous run reached**: at least half of the page's links were already seen by an
   earlier run (`data/cache/listing_seen.json`), kept 4 days. Since the job runs every
   24 hours, that is where the last run stopped. Dry runs and `check-sources` never update this file.
3. **No next page**, or the next page brings no new links.
4. **Safety caps**: `sections.max_pages` (20) pages per tab and `sections.max_per_source`
   (60) tabs per outlet. **Regional outlets read at most `sections.regional_max_pages` (3)
   pages per tab**: their new stories sit on a tab's first pages, and deeper pages hold old
   news (a trace on 2026-11-22 found mkantho paging 11 pages deep and ajkersylhet 15, all
   old stories).

The very first run has no memory yet, so tabs without dates are read to the page cap.

## What gets downloaded and kept

- **Every article is read**: every new link in the window, from every page of every tab,
  the print edition, RSS and sitemaps, is downloaded once and judged on its full text, so a
  story with a plain headline or deep in the print edition is not missed. Links already
  stored or already judged (`seen_urls`) are not downloaded again.
- **Keyword pre-filter (off)**: `prefilter: true` in `settings.yaml` (or `avn run --prefilter`)
  downloads only links whose headline, summary or URL contains a keyword from
  `config/keywords.yaml`. Faster, but misses stories whose headline has no keyword.
- **No per-outlet cap**: `max_articles_per_source` is empty, so every article is downloaded.
  Set a number to cap downloads per outlet per run (newest kept). Articles a cap leaves out
  are counted in the run's stats as `capped` and logged as a warning.
- **Classification**: the full text is scored against `keywords.yaml`. An article is kept
  only if it qualifies for a category. A strong term is required, and weak terms only add
  score.
- **Date check**: an article published before the 30-hour window is dropped, even if a
  link to it was found. The publication date is read from the page in this order: JSON-LD
  `datePublished` (of the article, not the web page), publication meta tags
  (`article:published_time`, `pubdate`, ...), `itemprop="datePublished"`, a `<time>` in the
  article or header, then any `<time>` on the page. Pages with none of these (most local
  outlets) give a date written out next to the headline: in the headline's own block or the
  text right after the `<h1>`, in an element named like a date/meta line
  ("December 24, 2024", "২৪ ডিসেম্বর ২০২৪", "৫ ঘণ্টা আগে"), or a numeric day-first date after a
  "published" label, whatever the element is called ("প্রকাশিত: ২৭/১০/২০২৬", "Published: 27-10-2026").
  The masthead's "today" date and clocks are never used. An "updated" stamp next to the
  headline is used only when nothing else is there (some local themes print the only date as
  "Update Time : ..."). A date guessed by trafilatura that is a bare "today" is dropped (it is
  the header clock); the article then gets the scrape date. Modified/updated meta tags are
  never used, so an old story edited today does not pass, and a site-wide JSON-LD block (its page is the
  homepage, or its headline is the outlet name) is ignored. A time without an offset is Dhaka
  time. A full timestamp beats a bare day. When the only date is a bare day (no time, or
  exactly 00:00:00), it is compared by day in Dhaka: a story dated with yesterday's day is
  kept, not dropped as "before 17:30".
- **Blocked pages** (403, 404, robots.txt) are judged on the headline and summary we already
  have. If they match, they are saved with `body_missing = 1`, and they are not retried.
  **Temporary failures** (timeouts, 5xx) are retried on the next run.

## Politeness and safety

- `robots.txt` is respected. Requests to each site go one at a time, `per_host_delay`
  (1 s) apart plus a random 0–50% (`per_host_jitter`), with retries.
- Only public addresses are fetched, and response size is capped (`fetch.py`).
- **Dead sites:** after `error_threshold` (5) timeouts or connection errors in a row (each
  retry counts), the site gets no more requests this run, so one site that never answers
  can't hold up the whole run (each try waits up to `timeout_seconds`, 25 s). There's no
  cooldown: the next run tries it again, and its articles are retried then.
- **Hard limit per request:** one request (redirects and the whole page) gets at most
  `request_deadline_seconds` (180 s), then it counts as a timeout. `timeout_seconds` only
  limits each wait for data, so without this a site that trickles data, or a stuck
  connection, could hang the run. It is per page, not per outlet, so a slow outlet with many
  pages is never cut short.

## Blocks: never get the IP banned

A banned IP also stops every later test, so the scraper backs off before a site escalates.

- **Block answers** are 403, 429, and bot-challenge pages (Cloudflare "Just a moment",
  `cf-mitigated` header, "Bot Verification", even when sent with status 200).
- **Slow down:** each block answer pushes that site's next request back (10 s, then 20 s).
  A 429 waits out its `Retry-After`.
- **Stop:** after `block_threshold` (3) block answers in a row, the site gets no more
  requests this run. A 429 asking to wait more than `max_retry_after` (120 s) stops it at once.
- **Cooldown:** a stopped site is saved in `data/cache/host_blocks.json` and left alone for
  `block_cooldown_hours` (12). `avn run`, dry runs and `check-sources` all skip it and print
  "Not fetched". Delete its entry to retry sooner, but only after fixing the cause.
- A challenge or 429 is temporary (the site refused us, not the page), so those articles
  retry next run. A plain 403 stays a permanent failure for that page.
- **Chrome fingerprint:** Cloudflare challenges Python HTTP clients by their TLS fingerprint
  whatever the User-Agent. Outlets with `fetch: impersonate` in `sources.yaml` are fetched
  through curl_cffi with a Chrome fingerprint and browser headers (Referer = the homepage),
  at least `impersonate_delay` (3 s) apart. The same address, redirect and size checks
  apply, and `robots.txt` is still read as `AVNNewsScraper`.
- Outlets whose blocks a fingerprint doesn't fix are `enabled: false`, with the reason as
  a comment in `sources.yaml`.
- **Time limits, so nothing hangs:** each request `request_deadline_seconds` (180 s); each
  **regional** paper's tab paging `sections.regional_budget_minutes` (5 min; then no new tab
  page, what was found is kept and its feeds, sitemaps and articles are still read). National
  papers (and online, TV, agency) have no budget and always finish, short of errors or blocks; the whole run
  `max_run_minutes` (180 min; then it stops, saved articles stay, no run record, so the next run
  looks back further). Postgres cancels a scrape query after 10 min and an API query after
  30 s (a stuck lock fails with an error instead of hanging).
- **Progress:** a run (and the service's daily scrape) logs a line every `progress_seconds`
  (60 s), national and regional apart: outlets scanned or articles downloaded so far out of
  the total and requests sent, then failed attempts, sites stopped, and time taken. A full run takes about 40 min at near 0% CPU, and
  the block log only records failures, so this line is how to tell slow from stuck.
- **Block log (permanent):** successful requests are only counted. Each scrape (run,
  dry run, `check-sources`) writes, in a folder named after the scrape's day in Asia/Dhaka:
  - `data/logs/blocks/2026-11-18/085935_run.jsonl`: the complete record. It holds
    `run_start` (window, machine, git commit, settings), one `request` line per block,
    timeout/connection error or 5xx, `host_stopped` when the breaker trips, and a
    `run_end` per-host summary;
  - `085935_run_events.parquet`: one typed row per failure, and
    `085935_run_hosts.parquet`: one row per host (requests, ok, blocked, block_rate). Parquet
    is the analysis format: columnar, typed and compressed. DuckDB, pandas and Polars read all
    runs at once, e.g. `SELECT host, block, count(*) FROM 'data/logs/blocks/*/*_events.parquet'
    GROUP BY ALL`;
  - `085935_run/blocked/`: the first 2 block pages per host.

  The time in the name is the start time in Dhaka, as in `data/logs/runs/2026-11-18_085935.json`.
  Every line also has `scrape_date` and `run_id` (UTC start). Nothing is written to Postgres
  during a scrape. At its end, every finished file not yet imported goes in, in one
  transaction: `block_log_runs` gets the whole file in `raw`, `block_log_hosts` gets the
  per-host summary, and `block_log_events` gets one row per failure (the table to analyse).
  If Postgres is down, the next scrape or `avn sync-block-logs` imports it. Files are never
  deleted. `avn block-report [--days N] [--host H]` summarises the files.
- To diagnose a site, probe it with a handful of spaced requests, never a crawl: a 403 with
  `cf-mitigated: challenge` means try `fetch: impersonate`; a challenge even then needs a
  real browser (not built); a timeout or 403 only from one machine means an IP block.

## Backfill: past months (`avn backfill`)

The daily run only looks back 30 hours. Past months are scraped on demand:

```
avn backfill 2025-05                  # one month
avn backfill 2025-05 2025-08          # several months
avn backfill 2025-01..2025-06         # a range
avn backfill 2025                     # a whole year (this year: up to yesterday)
avn backfill 2024-11..2026-10 --sources prothomalo,samakal --dry-run
avn backfill --status                 # what is done, partial, left to retry
```

- **Where old articles come from:** each outlet's archive sitemaps, found from `robots.txt`
  (or `/sitemap.xml`). Outlets split them by date: one file per day
  (`sitemap-daily-2025-05-01.xml`, `daily-sitemap/2025-05-01/sitemap.xml`), per month
  (`2025-05-01.xml`, `archive_2025-05-01.xml`) or per year (`sitemaps/2025/sitemap-2025-1.xml`).
  The index usually lists only recent files, so the URL pattern is learned from those and
  filled in for the month asked for (`discover/archive.py`). WordPress-style numbered files
  (`post-sitemap12.xml`) are read newest first by `lastmod`, stopping at the first file older
  than the month. Homepages, tabs and RSS are not used: they only show recent news.
- **Which entries:** entries of a file named after the month all belong to it. In yearly and
  WordPress files, an entry is kept when its `lastmod` is in the month or up to 31 days after
  (an article edited much later can be missed). The article's own date then decides: one
  published more than a day outside the month is not saved.
- **Pre-filter:** on the sitemap's headline (`news:title`) or a headline slug in the URL
  (`.../3-banks-get-emergency-funds-3789026`, Bangla slugs too). Entries with neither (URL ends
  in an id, like Prothom Alo's `/uyrdgbm7hw`) can't be judged without downloading them:
  `backfill.untitled: download` (default, complete but slower) or `skip`, or `--untitled`.
  Untitled links in video, photo, gallery, live, podcast and horoscope sections are never downloaded.
- **Same rules as the daily run** after that: classification, aliases, districts, story
  clusters (against articles published within 3 days), raw page storage. No per-outlet cap.
  Links an earlier daily run skipped only for being too old are not skipped here, and the
  backfill never marks a link as too old.
- **Politeness:** one site at a time is read `backfill.per_host_delay` (2 s, plus jitter)
  apart, slower than the daily run because a backfill runs for hours. The block breaker,
  cooldown and Chrome fingerprint apply as in the daily run. Each month is one run in the
  block log (`<time>_backfill.jsonl`).
- **Next to the daily run:** a backfill has its own lock (`data/.backfill.lock`, one at a time).
  While the daily scrape or `avn run` holds the run lock it pauses, so the two never hit a
  site together. Backfill runs are saved in `runs` with `stats.backfill`, and never count as
  the daily run (the scheduler and `--since-last-run` ignore them).
- **Outlets without dates are skipped:** many local outlets' sitemaps list articles with no
  `lastmod` and no date in the file name, so no month can be picked. After the first such
  file the rest of the series is not read, and the outlet is `no_archive` (layout `undated`).
  The daily run still reads them.
- **Resuming:** progress is kept per outlet and month in `backfill_progress`: `done`,
  `no_archive` (no dated sitemaps found, or entries without dates: nothing to do), `partial` (the site was stopped or
  on cooldown, downloads failed temporarily, or the month isn't over) or `failed` (sitemap
  not reachable). Articles are saved one by one. Run the same command again: `done` and
  `no_archive` are skipped, the rest retried, known links not downloaded again. `--redo`
  repeats done months.
- **Not possible from sitemaps (2026-11 probe):** janakantha, ntv, observerbd, unb, rtv and
  ekhontv have no sitemap; jamunatv and dailysun daily sitemaps list `#` instead of links;
  gramerkagoj's article sitemaps carry no dates.
- **Cost:** a big outlet has 100-300 articles a day. With headlines, only keyword hits are
  downloaded; without, every article is (about 3,000-9,000 a month at 2 s each, all outlets
  in parallel). Try one month with `--dry-run` before a long range.

## Known limits

- **Image-only e-papers** (`epaper.*` sites showing scanned page images, e.g. Samakal,
  Ittefaq, Janakantha) are not read: there is no text to match without OCR. Most print
  articles are also published on the website, where the tabs and sitemaps find them.
- E-papers blocked by `robots.txt` (Prothom Alo, New Age) or that need JavaScript
  (Dhaka Tribune) are not read.
- "Load more" buttons driven by JavaScript are not followed. Only real next-page links are.
- Some menus put sub-tabs at the top level, so a few sub-tabs get scanned. That costs only
  extra page requests.
- A headline without any keyword, from a site that gives no summary, can be missed by the
  pre-filter.

## Checking

- `avn check-sources [--sources a,b] -v` shows per outlet: homepage links, tabs/pages
  scanned, links from tabs, RSS, sitemap, and the tab list.
- `avn run --dry-run` does everything except saving.
