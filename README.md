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

Each matching article is saved with its full text, original HTML and metadata. It is indexed in SQLite and exported to CSV/Excel.

## Setup

```bash
uv venv --python 3.12 .venv          # or: python3.12 -m venv .venv
uv pip install -e ".[dev]"           # or: .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                     # classifier + parser tests
```

## Commands

```bash
avn run                      # the daily job: scrape, classify, save, export
avn run --dry-run            # same, but only print what would be saved
avn run --since 72           # look back 72 hours instead of the default 30
avn run --sources prothomalo,dailystar
avn run --catchall           # also use the search catch-all for this run
avn check-sources            # which outlets work, and through which feed/sitemap
avn check-sources --refresh  # re-discover feeds/sitemaps
avn reclassify               # re-apply keywords.yaml to stored articles (no network)
avn export                   # rebuild CSV / Excel from the database
avn stats                    # counts per category and per outlet
```

Use `.venv/bin/avn`, or activate the venv first.

## How it works

1. **Discover**: for every outlet in `config/sources.yaml` it reads the homepage headlines, RSS feeds and news sitemaps. Feeds and sitemaps are auto-discovered from the homepage and `robots.txt`, then cached for 7 days.
2. **Pre-filter**: only links whose headline, summary or URL contains a keyword get downloaded.
3. **Fetch and extract**: requests are polite (1 at a time per site, robots.txt respected, retries). `trafilatura` extracts the clean text, author and date.
4. **Classify**: keyword rules from `config/keywords.yaml` are applied to the full text (see below).
5. **Store**: one JSON file per article, the raw HTML (gzipped), and a row in SQLite. The same story from different outlets gets the same `story_cluster_id`, and the district is guessed from the text.
6. **Export**: daily CSV, one CSV per category, and `master.xlsx`.

URLs that were checked and didn't match are remembered, so they aren't downloaded again.

## Data layout

```
data/
  db/news.sqlite                         master index (SQL / pandas)
  articles/YYYY/MM/DD/<source>/<id>.json full record: text, metadata, matched keywords
  raw/YYYY/MM/DD/<source>/<id>.html.gz   original page, for re-parsing later
  exports/
    daily/YYYY-MM-DD.csv                 articles found that day
    by_category/<category>.csv           everything so far, one file per topic
    master.xlsx                          Summary per month, All, one sheet per topic
  logs/runs/YYYY-MM-DD_HHMMSS.json       per-run stats (sources ok/failed, counts)
  cache/endpoints.json                   auto-discovered feeds/sitemaps
```

Folders are organized by publication date (Dhaka time). An article is stored once. Category views come from the database and exports, since one article can belong to several categories.

CSV files are UTF-8 with BOM, so Excel displays Bangla correctly.

### SQLite

- `articles`: one row per article (`title`, `body`, `source_name`, `published_at` (UTC), `published_date` (Dhaka), `district`, `story_cluster_id`, `body_missing`, …)
- `article_categories`: (`article_id`, `category`, `score`, `matched_terms`)
- `v_matches`: view joining the two, one row per article and category. Start here.
- `review`: your manual verdicts (`verified` = 1 relevant / 0 false positive, `notes`)
- `articles_fts`: full-text search that works for Bangla and English
- `sources`, `runs`, `seen_urls`

```python
import sqlite3, pandas as pd
con = sqlite3.connect("data/db/news.sqlite")
df = pd.read_sql("SELECT * FROM v_matches", con)
df.groupby(["category", "published_date"]).article_id.nunique()

# full-text search
pd.read_sql("""SELECT a.published_date, a.source_name, a.title FROM articles_fts f
               JOIN articles a ON a.rowid = f.rowid WHERE articles_fts MATCH '"আয়নাঘর"'""", con)

# mark a false positive
con.execute("INSERT OR REPLACE INTO review(article_id, verified, notes) VALUES (?, 0, 'not relevant')", ("<id>",)); con.commit()
```

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
- **Blocked outlets.** As of Oct 2026, these return HTTP 403 to every automated request because of Cloudflare bot protection: Kaler Kantho, Bangladesh Pratidin, Kalbela, Jai Jai Din, Banglanews24, Somoy TV, Jamuna TV, Daily Sun, Netra News, Jago News and Risingbd. They are `enabled: false`. Dhaka Post blocks its article pages, so only the headline and RSS summary are stored (`body_missing = 1`).
- **Search catch-all.** This is off by default (`settings.yaml → search_catchall`). It runs the queries in `keywords.yaml → catchall_queries` against Bing News RSS. That finds articles from blocked outlets (stored as headline and snippet) and from small or local outlets that aren't configured. Bing's feed terms allow "personal, non-commercial use", so check that your use fits before enabling it. Results can include foreign outlets (`source_type = 'web'`). Google News RSS is not used because its robots.txt disallows automated access.
- Pre-filtering is based on headlines and summaries. An article whose headline has no keyword and whose site gives no summary can be missed. `--no-prefilter` downloads everything but is much slower.

## Running manually

`~/.local/bin/avn` is a link to `.venv/bin/avn`, so `avn` works from any folder:

```bash
avn run                                   # scrape now, in this terminal
systemctl --user start avn-scraper        # or: run the scheduled job now, in the background
journalctl --user -u avn-scraper -f       # watch the scheduled job's log
```

A manual run doesn't disturb the schedule. Articles already saved are skipped, so running more often than daily is safe.

## Scheduling

Installed on this Linux PC: `systemctl --user list-timers avn-scraper.timer` shows the next run. To stop it: `systemctl --user disable --now avn-scraper.timer`.

| Where | How |
|---|---|
| Linux (recommended) | systemd user timer, `deploy/avn-scraper.{service,timer}`. Runs at 07:00 Asia/Dhaka whatever the machine's timezone, and catches up if the PC was off. |
| macOS | launchd, `deploy/com.avn.newsscraper.plist` (uses the Mac's local time) |
| Plain cron | `deploy/crontab.example` |
| Cloud | `Dockerfile`: the image runs one scrape and exits. Schedule it at `0 1 * * *` UTC (= 07:00 Dhaka) and mount a volume at `/app/data`. |

### Cloud (Cloudflare) notes

Plain Cloudflare Workers are a poor fit. Python Workers run on Pyodide, have CPU-time and subrequest limits, and can't run `trafilatura`/`lxml` across ~50 sites. The recommended path:

- Run the Docker image on **Cloudflare Containers**, started by a Worker **Cron Trigger** (`0 1 * * *`).
- Store results in **R2** (files) and **D1** (SQLite-compatible index) by writing a new backend that implements `src/avn_scraper/storage/base.py`. That adapter is not written yet.

The same image also runs unchanged on GitHub Actions (scheduled workflow) or any VPS.

```bash
docker build -t avn-news-scraper .
docker run --rm -v "$PWD/data:/app/data" avn-news-scraper run
```

Set `AVN_HOME` to point the tool at a different project folder (`config/` + `data/`).

## Project layout

```
config/            settings.yaml, sources.yaml, keywords.yaml
src/avn_scraper/
  cli.py           commands
  pipeline.py      the daily run + reclassify
  discover/        rss, sitemap, listing (homepage links), catchall, per-source orchestration
  fetch.py         polite async HTTP client
  extract.py       article text/metadata
  classify.py      keyword scoring
  normalize.py     Bangla/English text normalization
  geo.py           district detection (64 districts, Bangla + English)
  dedupe.py        same-story clustering
  storage/         Storage interface + local filesystem/SQLite backend
  export.py        CSV / Excel
deploy/            systemd, launchd, cron examples
tests/
```
