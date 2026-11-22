# AVN News Scraper

Every night at 23:30 (Bangladesh time) this tool reads about 210 Bangladeshi newspapers,
TV news sites and local papers, in Bangla and English, and keeps only the stories about:

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

Matching stories are saved to a Postgres database with their full text, and exported to
CSV and Excel. A small web service serves them to the AVN website's `/dashboard`.

## What you need

- Linux or macOS (on Windows, use WSL)
- [Docker](https://docs.docker.com/get-docker/), for the database
- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs the right
  Python (3.14.5) for you
- git

## Setup (once)

**1. Get the code and install it**

```bash
git clone git@github.com:Shakhlyn/avn-news-scraper.git
cd avn-news-scraper
uv venv --python 3.14.5 .venv
uv pip install -e ".[dev]"
```

**2. Start the database**

Pick a password and put it in place of `CHOOSE_A_PASSWORD`:

```bash
docker run -d --name avn-postgres \
  -e POSTGRES_DB=avndb -e POSTGRES_USER=avn -e POSTGRES_PASSWORD=CHOOSE_A_PASSWORD \
  -p 127.0.0.1:5435:5432 -v avn_pgdata:/var/lib/postgresql \
  --health-cmd "pg_isready -U avn -d avndb" --health-interval 5s \
  postgres:18-alpine
```

The data lives in the Docker volume `avn_pgdata`, so it survives restarts. The name
`avn-postgres` matters: `./serve.sh` and `./scrape-now.sh` look for it and start it if
it's stopped. (If you already have the team's `avn_postgres_db/pg-stack`, run
`docker compose up -d` there instead. It creates the same container.)

**3. Write your settings file**

```bash
cp .env.example .env
```

Open `.env` and replace `CHANGE_ME` with your password, in both `DATABASE_URL` and
`TEST_DATABASE_URL`. The other values are fine for running on your own PC.

**4. Create the tables and check everything works**

```bash
.venv/bin/avn migrate                  # creates the tables
.venv/bin/python -m pytest -q          # all tests should pass
.venv/bin/avn check-sources --sources prothomalo,dailystar -v   # can we reach the sites?
```

Tip: activate the environment (`source .venv/bin/activate`) and you can type `avn`
instead of `.venv/bin/avn`.

## Everyday use

**Start the service** (and leave the terminal open):

```bash
./serve.sh
```

It starts the database if needed, then runs the web API on `http://127.0.0.1:8100` and
scrapes every night at 23:30. Each scrape looks back 30 hours. The 6 hours that overlap
the previous night catch late stories, and anything already saved is skipped. If the PC
was off at 23:30, it scrapes as soon as you start it, still looking back 30 hours: days the
PC was off are not filled in. Older news is only scraped with `avn backfill`.

Nothing starts it for you: no cron, no system service. You start it yourself.

**Scrape right now**, without the service:

```bash
./scrape-now.sh                        # looks back 30 hours, like the daily scrape
avn run --dry-run                      # try it: shows what would be saved, saves nothing
```

Only one scrape runs at a time. If the service is already scraping, these say so and
stop. A full scrape of all outlets can take up to an hour.

## Where the results are

- **Database:** start with the view `v_matches` (one row per article and category).
- **Files** in `data/exports/`:
  - `daily/YYYY-MM-DD.csv`: what that day's scrape found
  - `by_category/<category>.csv`: everything so far, one file per topic
  - `master.xlsx`: a summary per month, then one sheet per topic

The CSVs open correctly in Excel, Bangla included.

```sql
-- how many stories per day and topic
SELECT published_date, category, count(DISTINCT article_id) FROM v_matches GROUP BY 1, 2;

-- search (Bangla or English)
SELECT published_date, source_name, title FROM articles WHERE title ILIKE '%আয়নাঘর%' OR body ILIKE '%আয়নাঘর%';

-- mark a wrong match
INSERT INTO review(article_id, verified, notes) VALUES ('<id>', 0, 'not relevant')
ON CONFLICT (article_id) DO UPDATE SET verified = excluded.verified, notes = excluded.notes;
```

To connect with a database tool (DBeaver, psql, ...): host `localhost`, port `5435`,
database `avndb`, user `avn`, and your password.

## Commands

| Command | Does |
|---|---|
| `avn run` | Scrape now (`--dry-run` to save nothing, `--since 72` to look back 72 h, `--sources a,b` for some outlets only) |
| `avn backfill 2025-05` | Scrape a past month (also `2025-01..2025-06` or `2025`). Takes hours; try `--dry-run` first. `--status` shows progress |
| `avn check-sources` | Which outlets work, and how we read them (`--refresh` to look for feeds again) |
| `avn reclassify` | Re-apply the keywords to stored articles (no internet needed) |
| `avn export` | Rebuild the CSV / Excel files |
| `avn stats` | Counts per category and outlet |
| `avn block-report` | Which sites block us, and how |
| `avn migrate` | Create or update the database tables |
| `avn serve` | The service (`./serve.sh` does this, after starting the database) |

## Common tasks

**Add a news site.** Add a line to `config/sources.yaml` with an `id`, `name`,
`language`, `type` and `homepage` (copy a line next to it). Feeds and section pages are
found by themselves. Then check it: `avn check-sources --sources <id> -v`.

**Fix wrong or missed matches.** Edit `config/keywords.yaml`, then run `avn reclassify`.
Each category has:

- **strong** terms: one is enough to match
- **weak** terms: they add to the score but never match alone ("নিখোঁজ" is in many
  drowning stories, so it only counts next to a strong term like "গুম")
- **exclude** terms: they veto the category

Write a term once. Bangla digits, joiners and spellings are evened out, and suffixes are
matched (গুলি also finds গুলিতে). Start a term with `re:` for a regular expression.
`reclassify` only re-labels stored articles. To find articles a new keyword would have
caught, run `avn run --since 72`. Add a sample sentence to `tests/test_classify.py` for
every fix.

**Change the time or the look-back.** `daily_at` and `window_hours` in
`config/settings.yaml`. Restart `./serve.sh` afterwards.

## When something goes wrong

| You see | Do this |
|---|---|
| `Docker is not running` | Start Docker, then try again. |
| `No avn-postgres container` | Do setup step 2. |
| The service won't start and names `CORS_ORIGINS`, `ALLOWED_HOSTS` or `DB_POOL_MAX` | Your `.env` is missing that line. Copy it from `.env.example`. |
| `... keeps refusing us ... none for 12 h` | That site blocked us, so it's left alone for 12 hours to protect our IP. This is normal. To retry sooner, delete its entry in `data/cache/host_blocks.json`. |
| `... is not answering ...` | The site timed out 5 times in a row and is skipped for this scrape only. |
| Tests fail with `set TEST_DATABASE_URL` | Add it to `.env` (setup step 3), with the database running. |
| A story you expected is missing | Check the outlet with `avn check-sources --sources <id> -v`, then the keywords. |

The scrape's log is in `data/logs/runs/`, and every block is recorded in `data/logs/blocks/`.

## How it works

1. **Find links.** For each outlet in `config/sources.yaml`: homepage, section tabs, print
   edition, RSS feeds and news sitemaps, each read back to 30 hours ago.
2. **Download.** Every new link is downloaded once. Each site gets one request at a time,
   1 to 1.5 seconds apart, and `robots.txt` is respected. A site that starts blocking us
   is stopped for 12 hours. Sites behind Cloudflare (`fetch: impersonate` in
   `sources.yaml`) are fetched looking like Chrome.
3. **Read.** The article text, date and author are pulled out of the page.
4. **Match.** The text is scored against `config/keywords.yaml`.
5. **Save.** Matches go to Postgres, and the original page to `data/raw/` (gzipped). The
   same story from different outlets gets the same `story_cluster_id`, and the district is
   guessed from the text.
6. **Export.** The CSV and Excel files are rebuilt.

Links checked once are remembered, so they're never downloaded twice. If a page can't be
downloaded, a permanent refusal (403, 404) is judged on its headline alone. A temporary
one (timeout, server error) is tried again next scrape.

The exact rules (what's read, where paging stops, how blocks are handled) are in
[docs/scraping-rules.md](docs/scraping-rules.md).

## The web service

The AVN site's `/dashboard` (run with `npm run dev` in the avn repo) reads this service.
Interactive API docs are at `http://127.0.0.1:8100/docs`.

| Route | Does |
|---|---|
| `GET /api/v1/news` | Stories (`from`, `to`, `source`, `topic`, `q`, `page`, `limit`) |
| `GET /api/v1/accepted` | Stories the team accepted |
| `POST /api/v1/accepted` | Accept a story: `{"id": "<story id>"}` |
| `DELETE /api/v1/accepted/<id>` | Withdraw a story |
| `POST /api/v1/scrape` / `GET /api/v1/scrape` | Start a scrape / see how it's going |
| `GET /api/v1/runs` | Recent scrapes |
| `GET /api/v1/health` | Is the service and database OK |

The settings in `.env`:

- **`AVN_REVIEW=on`:** all news, accepting, and manual scrapes (like staging). Leave it
  empty for read-only, accepted stories only (like production).
- **`CORS_ORIGINS`:** the websites allowed to call the API.
- **`ALLOWED_HOSTS`:** the names the service answers to.
- **`DB_POOL_MAX`:** the most database connections the API holds at once.

The service only listens on localhost. Writes need a same-site `Origin` header, and
requests are rate-limited. To start a scrape through the API:

```bash
curl -X POST -H 'Origin: http://127.0.0.1:8100' http://127.0.0.1:8100/api/v1/scrape
```

## Running in Docker or the cloud

The `Dockerfile` builds an image that runs one scrape and exits. It needs a reachable
Postgres, and a volume for `data/`:

```bash
docker network create avnnet          # once
docker network connect avnnet avn-postgres
docker build -t avn-news-scraper .
docker run --rm --network avnnet -v "$PWD/data:/app/data" \
  -e DATABASE_URL=postgresql://avn:<password>@avn-postgres:5432/avndb avn-news-scraper run
```

From another container, the database port is 5432, not 5435. The same image works on
Cloudflare Containers (with a Cron Trigger `30 17 * * *`, which is 23:30 in Dhaka),
GitHub Actions or any VPS. Plain Cloudflare Workers can't run it.

## Project layout

```
config/            settings.yaml, sources.yaml (outlets), keywords.yaml
data/              raw pages, exports, logs, caches (not in git)
docs/              scraping-rules.md
src/avn_scraper/
  cli.py           the `avn` command
  main.py          the web service (FastAPI)
  api/             routes, under /api/v1
  core/            settings, database pool, rate limits, security
  repositories/    SQL
  services/        scrape manager, nightly scheduler
  scraper/         the scraping engine: discover/, fetch.py, extract.py, classify.py, pipeline.py, ...
  migrations/      the database tables (`avn migrate`)
tests/
```
