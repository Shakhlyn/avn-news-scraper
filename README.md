# AVN News Scraper

Runs every morning at 07:00 Bangladesh time and scans Bangladeshi newspapers, TV news portals and regional papers in both Bangla and English. It keeps only articles about:

| Category key | Topic |
|---|---|
| `extrajudicial_killing` | Extrajudicial killing (crossfire / "gunfight") |
| `border_killing` | Border killing (BSF) |
| `enforced_disappearance` | Enforced disappearance (গুম, Aynaghar) |
| `custodial_torture_death` | Custodial torture and death |
| `kneecapping` | Kneecapping (shooting in the leg) |
| `death_penalty` | Death penalty |
| `digital_security_act` | Digital Security Act / Cyber Security Act and Ordinance / ICT Act |
| `july_uprising` | July uprising ("July 36") |

Each matching article is saved with its full text, original HTML and metadata. It is stored in Postgres and exported to CSV/Excel.

## Setup

```bash
uv venv --python 3.12 .venv          # or: python3.12 -m venv .venv
uv pip install -e ".[dev]"           # or: .venv/bin/pip install -e ".[dev]"
cp .env.example .env                 # then fill in the database password
.venv/bin/avn migrate                # create the tables
.venv/bin/pytest                     # needs TEST_DATABASE_URL and the database running
```

## Commands

```bash
avn run                      # the daily job: scrape, classify, save, export
avn run --since-last-run     # look back to the last full run, at least window_hours
avn run --dry-run            # same, but only print what would be saved
avn run --since 72           # look back 72 hours instead of the default 24
avn run --sources prothomalo,dailystar
avn run --catchall           # also use the search catch-all for this run
avn check-sources            # which outlets work, and through which feed/sitemap
avn check-sources --refresh  # re-discover feeds/sitemaps
avn reclassify               # re-apply keywords.yaml to stored articles (no network)
avn export                   # rebuild CSV / Excel from the database
avn stats                    # counts per category and per outlet
avn migrate                  # create or update the database tables
avn serve                    # the service: API + daily scrape (or ./serve.sh)
```

Use `.venv/bin/avn`, or activate the venv first.

## How it works

1. **Discover**: for every outlet in `config/sources.yaml` it reads the homepage headlines, the print edition, every top-level section tab, RSS feeds and news sitemaps. Tabs and the print edition are found on the homepage on each run, and each one is read page by page back to 24 hours ago. The rules are in [docs/scraping-rules.md](docs/scraping-rules.md). Feeds and sitemaps are auto-discovered from the homepage and `robots.txt`, then cached for 7 days.
2. **Pre-filter**: only links whose headline, summary or URL contains a keyword get downloaded.
3. **Fetch and extract**: requests are polite (1 at a time per site, robots.txt respected, retries). `trafilatura` extracts the clean text, author and date. When the page's own structured data (JSON-LD `articleBody`) is clearly longer, that is used instead. Channel 24, a JavaScript-only site, is read through its JSON API (`src/avn_scraper/adapters.py`).
4. **Classify**: keyword rules from `config/keywords.yaml` are applied to the full text (see below).
5. **Store**: a row in Postgres and the raw HTML (gzipped) on disk. The same story from different outlets gets the same `story_cluster_id`, and the district is guessed from the text.
6. **Export**: daily CSV, one CSV per category, and `master.xlsx`.

URLs that were checked and didn't match are remembered, so they aren't downloaded again.

If an article page can't be downloaded:
- **Permanent failure** (403 bot protection, 404, robots.txt): the article is judged by its headline (and feed summary, if any). It is saved with `body_missing = 1` if it matches, and is not retried.
- **Temporary failure** (timeout, server error): it is retried on the next run.

The run log lists every failed download with its reason.

## Data layout

Postgres holds the articles, categories, dedup history, run history and the stories the team accepts. It is the Docker stack in `avn_postgres_db/pg-stack`, and `DATABASE_URL` in `.env` points at it (copy `.env.example`). Create or update the tables with `avn migrate`.

Files stay on disk for what does not belong in the database:

```
data/
  raw/YYYY/MM/DD/<source>/<id>.html.gz   original page, for re-parsing later (.json.gz for API-read sites)
  exports/
    daily/YYYY-MM-DD.csv                 articles found that day
    by_category/<category>.csv           everything so far, one file per topic
    master.xlsx                          Summary per month, All, one sheet per topic
  logs/runs/YYYY-MM-DD_HHMMSS.json       per-run stats (sources ok/failed, counts, failed downloads)
  cache/endpoints.json                   auto-discovered feeds/sitemaps
  cache/listing_seen.json                links seen on section pages (where paging stops)
```

An article is stored once. Category views come from the database and exports, since one article can belong to several categories.

CSV files are UTF-8 with BOM, so Excel displays Bangla correctly.

### Tables

- `articles`: one row per article (`title`, `body`, `source_name`, `published_at`, `published_date` (Dhaka), `district`, `story_cluster_id`, `body_missing`, …)
- `article_categories`: (`article_id`, `category`, `score`, `matched_terms`)
- `v_matches`: view joining the two, one row per article and category. Start here.
- `review`: your manual verdicts (`verified` = 1 relevant / 0 false positive, `notes`)
- `accepted`: stories accepted on the AVN site's `/dashboard`, each a copy of the story at that moment
- `sources`, `runs`, `seen_urls`, `schema_migrations`

```sql
SELECT published_date, category, count(DISTINCT article_id) FROM v_matches GROUP BY 1, 2;

-- search, Bangla or English
SELECT published_date, source_name, title FROM articles WHERE title ILIKE '%আয়নাঘর%' OR body ILIKE '%আয়নাঘর%';

-- mark a false positive
INSERT INTO review(article_id, verified, notes) VALUES ('<id>', 0, 'not relevant')
ON CONFLICT (article_id) DO UPDATE SET verified = excluded.verified, notes = excluded.notes;
```

## The service

`./serve.sh` starts the FastAPI service on `127.0.0.1:8100`, after starting the database if it is stopped. On this machine the `avn-api` user service keeps it running from boot (`deploy/avn-api.service`). It does three jobs:

- **Daily scrape:** at `daily_at` in `settings.yaml` (07:00 Dhaka). It checks every minute, so if the PC was off, it scrapes as soon as it is back with a network. A failed run is retried after 30 minutes. The look-back reaches the start of the last full run, and is never shorter than `window_hours`: after 3 days off it covers 72 hours. The 80-per-outlet cap still applies per run, and outlets may no longer list very old items, so a long gap may not be recovered in full.
- **News API:** what the AVN site's `/dashboard` reads.
- **Review desk API:** accepting and withdrawing stories.

`AVN_REVIEW=on` in `.env` behaves like staging: all news, Accept, and manual scrapes. Empty behaves like production: accepted stories only, read-only.

| Route | Does |
|---|---|
| `GET /api/news` | Stories, with `from`, `to`, `source`, `topic`, `q`, `page`, `limit` |
| `GET /api/accepted` | Accepted stories |
| `POST /api/accepted` | Accept a story: `{"id": "<story id>"}` |
| `DELETE /api/accepted/<id>` | Withdraw a story |
| `POST /api/scrape` | Scrape now |
| `GET /api/scrape` | State of the current or last scrape |
| `GET /api/runs` | Recent runs |
| `GET /api/health` | Service and database check |

The interactive docs are at `http://127.0.0.1:8100/docs`. The service answers on localhost only, and writes must carry a same-site `Origin`.

### Scraping by hand

```bash
curl -X POST -H 'Origin: http://127.0.0.1:8100' http://127.0.0.1:8100/api/scrape
curl http://127.0.0.1:8100/api/scrape     # follow it
./scrape-now.sh                           # same scrape, without the service
```

Only one scrape can go at a time, whichever way it was started. If the database is down, `avn run` stops with a clear error before any web request.

## Tuning the keywords

Edit `config/keywords.yaml`, then run `avn reclassify`. Each category has:

- **strong** terms: a single hit can qualify the article.
- **weak** terms: they add score but never qualify an article on their own. For example, "নিখোঁজ" (missing) appears in many drowning stories, so it only counts when a strong term like "গুম" is also present.
- **exclude** terms: these veto the category.

The text is normalized before matching, so you only need one spelling: Bangla digits become ASCII (৫৭ → 57), zero-width joiners are removed, and nukta letters are unified. Bangla terms also match suffixed forms (গুলি → গুলিতে). Prefix a term with `re:` to write a regular expression.

`reclassify` only re-labels articles that are already stored. To catch articles a new keyword would have found, re-run with a longer window, e.g. `avn run --since 72`.

Spot-check the daily CSV for the first week or two, mark false positives in `review`, and adjust the lists. Add a sample sentence to `tests/test_classify.py` for every fix.

## Sources and limitations

- `config/sources.yaml` lists ~60 outlets. Add one with just an `id`, `name`, `language`, `type` and `homepage`; feeds are discovered automatically. Then run `avn check-sources --sources <id>`.
- **Blocked outlets.** As of Oct 2026, these return HTTP 403 to every automated request because of Cloudflare bot protection: Kaler Kantho, Bangladesh Pratidin, Kalbela, Jai Jai Din, Banglanews24, Somoy TV, Jamuna TV, Daily Sun, Netra News, Jago News and Risingbd. They are `enabled: false`. Dhaka Post and Jugantor block their article pages but leave an RSS feed or sitemap open, so matching articles are stored from the headline (plus RSS summary for Dhaka Post) with `body_missing = 1`.
- **Search catch-all.** This is off by default (`settings.yaml → search_catchall`). It runs the queries in `keywords.yaml → catchall_queries` against Bing News RSS. That finds articles from blocked outlets (stored as headline and snippet) and from small or local outlets that aren't configured. Bing's feed terms allow "personal, non-commercial use", so check that your use fits before enabling it. Results can include foreign outlets (`source_type = 'web'`). Google News RSS is not used because its robots.txt disallows automated access.
- Pre-filtering is based on headlines and summaries. An article whose headline has no keyword and whose site gives no summary can be missed. `--no-prefilter` downloads everything but is much slower.

## Running manually

`~/.local/bin/avn` is a link to `.venv/bin/avn`, so `avn` works from any folder:

```bash
avn run                                   # scrape now, in this terminal
curl -X POST -H 'Origin: http://127.0.0.1:8100' http://127.0.0.1:8100/api/scrape   # or: ask the service to scrape now
journalctl --user -u avn-api -f            # watch the service log
```

A manual run doesn't disturb the schedule. Articles already saved are skipped, so running more often than daily is safe. Only one run can happen at a time: if the 07:00 job is running, `avn run` (and `avn reclassify`) says so and exits.

## Scheduling

Installed on this Linux PC: `systemctl --user status avn-api` shows the service. To stop it: `systemctl --user disable --now avn-api`.

| Where | How |
|---|---|
| Linux (recommended) | systemd user service, `deploy/avn-api.service`. The service scrapes at 07:00 Asia/Dhaka whatever the machine's timezone, and catches up if the PC was off. |
| macOS | launchd, `deploy/com.avn.newsscraper.plist` (uses the Mac's local time) |
| Plain cron | `deploy/crontab.example` |
| Cloud | `Dockerfile`: the image runs one scrape and exits. Schedule it at `0 1 * * *` UTC (= 07:00 Dhaka) and mount a volume at `/app/data`. |

### Cloud (Cloudflare) notes

Plain Cloudflare Workers are a poor fit. Python Workers run on Pyodide, have CPU-time and subrequest limits, and can't run `trafilatura`/`lxml` across ~50 sites. The recommended path:

- Run the Docker image on **Cloudflare Containers**, started by a Worker **Cron Trigger** (`0 1 * * *`).
- Point `DATABASE_URL` at a Postgres the container can reach. The `avn-postgres` stack on this machine listens on localhost only.

The same image also runs unchanged on GitHub Actions (scheduled workflow) or any VPS.

```bash
docker build -t avn-news-scraper .
docker run --rm -v "$PWD/data:/app/data" avn-news-scraper run
```

Set `AVN_HOME` to point the tool at a different project folder (`config/` + `data/`).

## The website

The AVN site's `/dashboard`, run locally with `npm run dev`, reads this service. See the avn repo's CLAUDE.md.

## Project layout

```
config/            settings.yaml, sources.yaml, keywords.yaml
src/avn_scraper/
  cli.py           commands
  publish.py       the news watch file for avnbd.org/dashboard, upload to R2
  pipeline.py      the daily run + reclassify
  discover/        rss, sitemap, listing (homepage links), sections (menu tabs + paging), catchall, per-source orchestration
  fetch.py         polite async HTTP client
  extract.py       article text/metadata
  classify.py      keyword scoring
  normalize.py     Bangla/English text normalization
  geo.py           district detection (64 districts, Bangla + English)
  dedupe.py        same-story clustering
  storage/         Storage interface + Postgres backend
  migrations/      SQL for the database tables (`avn migrate`)
  importer.py      one-off import of the old SQLite data
  api/             FastAPI service: routers/, repository.py (SQL), stories.py, scrape.py, scheduler.py
  export.py        CSV / Excel
deploy/            systemd, launchd, cron examples
tests/
```
