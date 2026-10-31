# AVN news scraper

Daily scraper (07:00 Asia/Dhaka) for human-rights news from Bangladeshi outlets. See README.md.

The FastAPI service (`src/avn_scraper/api/`, `./serve.sh`) runs the daily scrape and serves the news
and review API that the AVN site's `/dashboard` reads. FastAPI lives in this repo only.

- The scraping rules (24-hour window, which tabs / print editions are read, pagination
  stops, what is skipped and kept) are in `docs/scraping-rules.md`. Follow them, and update
  that file whenever a change alters them.
- Tests: `.venv/bin/python -m pytest -q`. Check real sites with `.venv/bin/avn check-sources -v`
  and `.venv/bin/avn run --dry-run`. Never run a real `avn run` just to test; it writes data.
