# AVN news scraper

Daily scraper (07:00 Asia/Dhaka) for human-rights news from Bangladeshi outlets. See README.md.

The FastAPI service (`src/avn_scraper/main.py`, `./serve.sh`) runs the daily scrape and serves the news
and review API that the AVN site's `/dashboard` reads. FastAPI lives in this repo only.
Routes go in `api/v1/endpoints/`, models in `schemas/`, SQL in `repositories/`, logic in `services/`;
the scraping engine is `scraper/`.

The whole service is async on one event loop, and a scrape shares that loop with the API. Anything that
waits uses async (`psycopg` `AsyncConnection`, `httpx`). Blocking or CPU-heavy work (parsing, keyword matching,
file writes, pandas) goes through `asyncio.to_thread`, or it freezes the API during a scrape.
The functions it calls stay plain `def`. No threads of our own, and no sync `psycopg`.

- The scraping rules (24-hour window, which tabs / print editions are read, pagination
  stops, what is skipped and kept) are in `docs/scraping-rules.md`. Follow them, and update
  that file whenever a change alters them.
- Tests: `.venv/bin/python -m pytest -q`. Check real sites with `.venv/bin/avn check-sources -v`
  and `.venv/bin/avn run --dry-run`. Never run a real `avn run` just to test; it writes data.

## Tickets (`dev-task/`, gitignored)

Every task gets a dated ticket. Do not read `dev-task/` at session start: read it only when told to or when
assigned a task, and then open the epic and ticket whose names match, not all of them.

- Layout: `dev-task/epics/<epic-name>/` holds `epic-<epic-name>.md`, `ticket-<number>-<name>.md` and `task-completion.md`.
- A ticket that fits no epic (rare) goes in `dev-task/epics/no-epic/`.
- Ticket numbers run across the repo. Name epics and tickets so the name alone says what is inside.
- `task-completion.md` tracks each ticket as `done`, `partial` or `pending`, with what is left and why. Keep it current.
- Keep epics and tickets short: quick to read, few tokens.
