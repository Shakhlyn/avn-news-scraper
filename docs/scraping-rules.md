# Scraping rules

These rules say what the scraper reads, how far back it goes, and what it keeps.
Change the code only together with this file. Settings live in `config/settings.yaml`
and outlets in `config/sources.yaml`.

## Schedule and time window

- The job runs **once a day at 07:00 Asia/Dhaka** (`deploy/`).
- Each run looks back **24 hours** (`window_hours: 24`): from 07:00 yesterday to now.
  Nothing older is kept, so news is not repeated across days.
- If a day was missed (PC off, run failed), catch up with `avn run --since 48`, or with
  24 × the number of days missed.
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

Tabs listed under `sections:` for an outlet in `sources.yaml` are always scanned as well.
`auto_sections: false` turns auto-detection off for that outlet, so only the listed tabs
are scanned. Use this for outlets whose menu is built by JavaScript (`avn check-sources`
then shows 0 tabs).

### How far each tab is read (pagination)

Each tab (and the print edition) is read page by page, following its "next page" link
(`rel=next`, `?page=N`, `/page/N`). Reading stops at the first of:

1. **24 hours reached**: most dated items on the page are older than the window. Dates
   come from `<time datetime>` on the page, or else from dates in the article URLs.
2. **Previous run reached**: at least half of the page's links were already seen by an
   earlier run (`data/cache/listing_seen.json`). Since the job runs every 24 hours, that
   is where the last run stopped. Dry runs and `check-sources` never update this file.
3. **No next page**, or the next page brings no new links.
4. **Safety caps**: `sections.max_pages` (20) pages per tab and `sections.max_per_source`
   (60) tabs per outlet.

The very first run has no memory yet, so tabs without dates are read to the page cap.

## What gets downloaded and kept

- **Keyword pre-filter**: a link is downloaded only if its headline, summary or URL contains
  a keyword from `config/keywords.yaml`. `--no-prefilter` downloads everything.
- **Per-outlet cap**: at most `max_articles_per_source` (80) downloads per outlet per run.
- **Classification**: the full text is scored against `keywords.yaml`. An article is kept
  only if it qualifies for a category. A strong term is required, and weak terms only add
  score.
- **Date check**: an article published before the 24-hour window is dropped, even if a
  link to it was found.
- **Blocked pages** (403, 404, robots.txt) are judged on the headline and summary we already
  have. If they match, they are saved with `body_missing = 1`, and they are not retried.
  **Temporary failures** (timeouts, 5xx) are retried on the next run.

## Politeness and safety

- `robots.txt` is respected. Requests to each site go one at a time, `per_host_delay`
  (1 s) apart, with retries.
- Only public addresses are fetched, and response size is capped (`fetch.py`).
- Outlets that block all automated access (Cloudflare 403) are `enabled: false` in
  `sources.yaml` and are not worked around.

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
