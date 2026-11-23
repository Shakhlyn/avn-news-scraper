# Scraping decisions: the rules, and why

`docs/scraping-rules.md` says **what** the scraper does. This file says **why**: for each
rule, the problem we had without it, the evidence, and what the choice costs. Many rules
replaced an earlier one; the old rule is named so nobody brings it back by accident.

Update this file with `scraping-rules.md` whenever a rule changes: add the new decision,
and mark the old one as replaced instead of deleting it.

Each entry below ("*inferred*" marks a reason reconstructed from the code, not recorded in a commit or ticket):

- **Rule**: what we do now.
- **Why / problem before**: what went wrong without it (with numbers when we had them).
- **Cost**: what we give up. Every rule has one.
- **Replaced**: the earlier rule, if any. **Evidence**: commit or ticket.

The guiding priorities, in order, when two rules pull against each other:

1. **Never get the IP banned.** A banned IP stops the daily run *and* every test.
2. **Don't lose news.** A missed story is a missed human-rights record.
3. **Finish on time and never hang.** The service shares one event loop with the API.
4. **Spend as few requests as possible.** That's what protects (1) and makes (3) possible.

---

## 1. Schedule and time window

### 1.1 One run a day at 23:30 Dhaka, looking back 30 hours

- **Rule:** the service scrapes at 23:30 Asia/Dhaka; the window is 17:30 the day before → now.
- **Why / problem before:** the run used to be 07:00 with a 24-hour window. Two windows that
  only touch leave no margin: a story published or listed late (after the previous run read
  that tab) fell between runs and was never seen. The 6-hour overlap catches those.
  (Why the clock time moved to 23:30 rather than staying 07:00 isn't recorded.)
- **Cost:** 6 extra hours of listings are read each night. Cheap: links already stored or
  judged (`seen_urls`) are skipped, so the overlap downloads nothing twice.
- **Replaced:** 07:00 run, 24-hour window (2026-10-21 → 2026-11-21).
- **Evidence:** 626833b, ticket-025.

### 1.2 Every run covers only that window; missed days are for backfill

- **Rule:** the nightly run, the catch-up after the PC was off, `scrape-now.sh` and the API's
  "scrape now" all look back exactly `window_hours` (30). Older news only via `avn backfill`
  (or `avn run --since N` by hand).
- **Why / problem before:** runs used `--since-last-run`, which stretched the window back to
  the last full run. After the PC was off three days, the catch-up queued **57,000 articles**,
  ran for hours and pushed memory to 20 GB (see 8.4). One unbounded run also means
  thousands of requests per site in one go, which is how IPs get banned.
- **Cost:** days the PC was off are not filled in automatically. Someone has to run a backfill.
- **Replaced:** `--since-last-run` (window = since the last full run).
- **Evidence:** 41c9b70.

### 1.3 Nothing starts the scraper automatically

- **Rule:** no cron, systemd timer or launchd. The user starts `./serve.sh`.
- **Why / problem before** *(inferred)*: the first version shipped OS timers. Once the FastAPI service
  owned the schedule (with catch-up), a timer would start a second scraper beside it, and
  two schedulers mean two runs fighting over sites and the lock.
- **Cost:** if nobody starts the service, nothing is scraped.
- **Replaced:** systemd / launchd / cron files. **Evidence:** 3575f50, 910282f.

### 1.4 One scrape at a time (run lock)

- **Rule:** `data/.run.lock`; a second scrape reports "already running" and stops.
- **Why / problem before:** a manual run and the scheduled one could overlap. Each has its
  own `Fetcher` with its own per-site pacing, so two runs hit each site **twice** as often
  as the pacing promises, and double-store the same links.
- **Cost:** a manual scrape has to wait for the nightly one.
- **Evidence:** 8d9eb78.

---

## 2. Finding links (discovery)

### 2.1 Homepage + print edition + every top-level tab + RSS + news sitemaps

- **Rule:** all five sources are read for every outlet, every run.
- **Why / problem before** *(inferred; not recorded in the commit)*: homepage, RSS and sitemaps alone showed only what each outlet
  chose to promote. Stories deep in a section or only in the print edition never reached the homepage or the feed.
- **Cost:** many more listing requests per outlet than reading the feed alone.
- **Replaced:** homepage + RSS + sitemaps only. **Evidence:** d8134fb, 1c7b217.

### 2.2 Tabs are found from the menu on every run, not listed by hand

- **Rule:** tabs are detected from the homepage's main menu each run; `sections:` in
  `sources.yaml` adds tabs, `auto_sections: false` turns detection off (JavaScript menus).
- **Why / problem before:** with ~210 outlets, a hand-kept tab list goes stale silently
  when an outlet renames or adds a section, and the missed section is never noticed.
- **Cost:** detection sometimes picks a sub-tab or a junk link (extra page requests only).

### 2.3 Only top-level menu items, never the footer, never dropdown sub-tabs

- **Rule:** see the tab rules in `scraping-rules.md`; junk list `_NOT_SECTION`.
- **Why / problem before** *(inferred; the commit gives no numbers)*: scanning everything in `<nav>` and the footer read dozens of
  sub-tabs per outlet (each a parent tab's subset), plus tags, login, polls and converters.
  Pages of requests for links we already had.
- **Cost:** a story only in a sub-tab that the parent tab doesn't list can be missed. RSS
  and sitemaps usually still bring it.
- **Evidence:** 1c7b217.

### 2.4 Category, tag and page-number links are not articles

- **Rule:** `/category?catid=1`, `?page=2`, `&pageno=3`, tag/topic/author paths with a query
  are skipped as article links.
- **Why / problem before:** ajkersylhet links tab pages as `/category?catid=1&&pageno=2`.
  These were taken for articles: 26 of its 52 date probes were wasted on them (all undated),
  and they were queued for download.
- **Evidence:** 3dbea07.

### 2.5 Off-topic sections are skipped

- **Rule:** sports, entertainment, lifestyle, tech, business, jobs, religion, horoscope,
  literature (English and Bangla names) are not scanned and their links not downloaded.
  `ict` is kept (International Crimes Tribunal); only the section part of the URL counts.
- **Why / problem before:** with every article downloaded (3.1), only 1-2% match. About
  **28% of tabs** in the 2026-11-19 run were such sections; of 1,337 saved articles, the
  **7** from them were all false matches. Pure wasted requests, plus noise for reviewers.
- **Cost:** a real case reported under "sports" (e.g. a player tortured in custody) is
  missed unless it also appears in a news section.
- **Evidence:** 734a2cd.

### 2.6 World news only when it is about Bangladesh

- **Rule:** links in world-news sections are downloaded only when headline, summary or URL
  names Bangladesh, Bangladeshis, expatriates (প্রবাসী) or a district.
- **Why / problem before:** of 87 saved world-news articles, **62** were foreign stories (a
  death sentence in another country matched `death_penalty`). The 25 kept are the ones we
  want: Bangladeshis executed or sentenced abroad, Bangladesh at the UN.
- **Cost:** the check sees only the headline and summary, not the body: a world story about
  a Bangladeshi with no Bangladesh word in its headline is missed.
- **Evidence:** 734a2cd.

### 2.7 Auto-discovered feeds and sitemaps must be on the outlet's own site

- **Why / problem before:** a page can link any feed. Following off-site feeds would let a
  hostile or hacked page point us at another site, or at addresses we must not fetch (10.1).
- **Evidence:** 8cd3fb4.

---

## 3. How far each tab is read (pagination)

### 3.1 Read page by page until the window, the previous run, or the end

- **Rule:** follow "next page" until (a) most dated items are older than the window, (b) at
  least half the page's links were seen by an earlier run (`listing_seen.json`, kept 4 days),
  (c) no next page, or (d) a cap.
- **Why / problem before:** reading only the first page of a tab lost stories on busy days,
  when a tab moves more than a page in 24 hours. Reading a fixed number of pages either wasted
  requests (quiet days) or still lost stories (busy days). Stopping on *data* (dates, links
  already seen) reads exactly as deep as needed.
- **Cost:** the first run has no memory and reads tabs without dates to the cap.
- **Evidence:** d8134fb, 1c7b217. Memory length of 4 days: user's call (ticket-025).

### 3.2 Regional tabs without dates: probe the last 3 articles on the page

- **Rule:** on a page with no `<time>` or URL dates, download its last `date_probe` (3) new
  links (not those also on the homepage) and stop when all are older than the window.
  Probed pages go on to classification; they are not downloaded twice.
- **Why / problem before:** regional sites show no machine-readable dates on listings, so
  stop (a) never fired and **every regional tab was read to the 20-page cap**. The bottom of
  the page is checked, not the top, because local sites pin old stories above the newest;
  homepage links are excluded because "latest" widgets are always fresh.
- **Cost:** up to 3 article downloads per listing page. They're reused, so mostly not extra.
- **Evidence:** d80bfa3.
- **Changed 2026-11-23 (8.7):** a probe keeps the page only for a link that will be
  downloaded; an old article keeps just its date.

### 3.3 Regional tabs: at most 3 pages; national tabs up to 20

- **Rule:** `sections.regional_max_pages: 3`, `sections.max_pages: 20`.
- **Why / problem before:** even with probes, bad dates made regional tabs page deep. A trace
  on 2026-11-22 had mkantho at **716 requests** and ajkersylhet at **664**, all old news past
  page 3. With the cap: 52 and 73, each outlet done in under two minutes.
- **Cost:** a regional outlet that publishes more than 3 pages a day in one tab loses the
  overflow from that tab (its RSS/sitemap may still bring it). None seen so far.
- **Evidence:** f35b984.

---

## 4. What gets downloaded

### 4.1 Every new link in the window is downloaded and judged on full text

- **Rule:** `prefilter: false`, no `max_articles_per_source`. Both can be switched back on.
- **Why / problem before:** the run downloaded only links with a keyword in the headline,
  and at most the 80 newest per outlet. On a busy day the cap cut **print-edition stories
  first** (they're older by timestamp), and a story with a plain headline ("যুবকের লাশ উদ্ধার")
  was never read at all. Losing news is worse than spending requests.
- **Cost:** ~12,000 requests a night instead of a few thousand, and a ~40-minute run. This
  is why 2.5, 2.6 and 3.3 exist: they bring the request count back down without a headline filter.
- **Replaced:** keyword pre-filter on, 80 per outlet. **Evidence:** 19f0b6f, ticket-020.

### 4.2 Each link is downloaded once, ever

- **Rule:** stored articles and judged links (`seen_urls`) are never downloaded again.
- **Why:** re-downloading the same pages every night multiplies requests (block risk) and
  re-stores duplicates.
- **Cost:** a page judged as "no match" is never re-judged, even if keywords improve. Its
  text isn't kept, so `reclassify` can't recover it (ticket-030 plans to keep it).

### 4.3 Permanent refusals are judged on the headline; temporary ones retried next run

- **Rule:** 403/404/410/robots-blocked: judge on the headline and summary we already have,
  save as `body_missing = 1`, never retry. Timeouts, 5xx, challenges and 429: retry next run.
- **Why / problem before:** blocked pages were either dropped (a matching headline lost) or
  retried every run (wasted requests on pages that will never answer, and repeated hits on a
  site that already refused us). A challenge or 429 means the *site* refused us, not the page,
  so it's temporary.
- **Cost:** headline-only records have no body for reviewers.
- **Evidence:** 8d9eb78, df259af.

### 4.4 Some outlets are read through their JSON API (adapters)

- **Why / problem before:** Channel 24 is a JavaScript-only site; the HTML has no article text.
- **Evidence:** 8d9eb78 (`scraper/adapters.py`).

---

## 5. Article dates

The date decides whether a story is "new" (in the window) or dropped. Every wrong date was
either a lost story (read as too old) or old news shown as new.

### 5.1 Date order: JSON-LD → publication meta → itemprop → `<time>` in the article → page `<time>`

- **Why / problem before:** a sidebar `<time>` (another story's date) won over the article's
  own date and **dropped fresh stories as too old**.
- **Evidence:** 91770bb.

### 5.2 Modified/updated dates are never used (except as the only date next to the headline)

- **Why / problem before:** an old story edited today would pass as new.
- **The exception:** mkantho's only date is "Update Time : 08:10 am, Sunday, 1 November
  2026". Ignoring it let trafilatura guess the header clock, so **every old mkantho story
  looked like today's**, and the date probe never saw an old page (3.2 never stopped).
- **Evidence:** 91770bb, f35b984.

### 5.3 A time without an offset is Dhaka time

- **Why / problem before:** sangbad and samakal give JSON-LD times with no offset. Read as
  UTC they were **6 hours late**, enough to push evening stories across the window edge.
- **Evidence:** fc14538.

### 5.4 A bare day (no time, or exactly 00:00:00) is compared by Dhaka day

- **Why / problem before:** dailystar, nayadiganta, bss give 00:00:00. Read as midnight, a
  story from yesterday afternoon was "before the window start" and **dropped**. A feed's full
  timestamp beats a bare day.
- **Evidence:** 91770bb, fc14538.

### 5.5 Site-wide JSON-LD is ignored

- **Rule:** skip a JSON-LD block whose page is the homepage or whose headline is the outlet name.
- **Why / problem before:** ekhontv repeats one NewsArticle block on every page with a
  **stale 2024 date**, so every ekhontv story looked two years old.
- **Evidence:** fc14538.

### 5.6 Local papers: read the written date next to the headline; never the masthead clock

- **Rule:** read "২৪ ডিসেম্বর ২০২৪", "৫ ঘণ্টা আগে", "প্রকাশিত: ২৭/১০/২০২৬" near the `<h1>`. A bare
  "today" guessed by trafilatura is dropped (it's the header clock); the article gets the scrape date.
- **Why / problem before:** most local outlets have no date markup. The last fallback took
  the **masthead's "today" date**, so old stories were saved as new. Numeric day-first dates
  (ajkersylhet) weren't read at all.
- **Evidence:** d80bfa3, f35b984.

### 5.7 Date patterns are bounded

- **Rule:** "N hours ago" takes 1-4 digits; text nodes over 300 characters aren't searched.
- **Why / problem before:** a long digit run made the regex backtrack (**20,000 digits → 9 s**),
  and "99999999999 days ago" crashed with OverflowError. Pages are untrusted input.
- **Evidence:** 116fd28.

---

## 6. Classification and storage

### 6.1 Strong terms are required; weak terms only add score; exclude terms veto

- **Why / problem before:** a single common word matched too much: "নিখোঁজ" (missing) is in
  countless drowning stories. It only counts next to a strong term like "গুম".
- **Cost:** a story using only weak vocabulary is missed. Fix by adding a strong term.

### 6.2 Bangla normalisation and suffix matching

- **Why:** Bangla digits, joiners (ZWJ/ZWNJ) and spelling variants differ between outlets,
  and suffixes attach to words (গুলি → গুলিতে). Without it, each keyword would need every form.

### 6.3 Postgres, not SQLite + JSON files

- **Why / problem before** *(partly inferred)*: the API (dashboard, accepting stories) and the scraper need to
  read and write at once; SQLite plus per-article JSON plus an R2 upload step was three copies
  of the truth. Raw pages and CSV/Excel stay on disk.
- **Replaced:** SQLite + JSON + `avn publish` to R2. **Evidence:** 3575f50.

### 6.4 Same story across outlets gets one `story_cluster_id`

- **Why:** reviewers would otherwise accept the same incident five times.

### 6.5 Spreadsheet formulas are neutralised in exports

- **Why:** a headline starting with `=`, `+`, `-`, `@` would run as a formula when the team
  opens the CSV in Excel (CSV injection). **Evidence:** 8cd3fb4.

---

## 7. Politeness and blocks: never get the IP banned

### 7.1 One request at a time per site, 1 s + 0-50% jitter apart; robots.txt respected

- **Why:** the per-site rate is what a site's firewall sees. Jitter removes the fixed rhythm
  that marks a bot. robots.txt is the site's stated rule; ignoring it is how a block becomes
  justified.
- **Cost:** a big outlet with 300 links takes ~6 minutes no matter how fast our machine is.

### 7.2 Concurrency 32 across sites

- **Rule:** up to 32 requests in flight in total, still one per site.
- **Why / problem before:** with 12, a dry run took **57 minutes**. Raising total concurrency
  reads more *sites* at once without asking any one site more often. The 32-wide dry run had
  16 block answers in 11,565 requests.
- **Replaced:** 12. **Evidence:** d5c87c8.

### 7.3 Block answers slow the site down, then stop it, then cool it down for 12 h

- **Rule:** 403/429/challenge → next request 10 s, then 20 s later; 3 in a row → no more
  requests this run; then 12-hour cooldown in `host_blocks.json`, respected by runs, dry runs
  and `check-sources`. `Retry-After` honoured; over 120 s → stop at once.
- **Why / problem before:** before 2026-11-18 a refusing site kept getting requests, and each
  refusal makes a firewall more likely to escalate to an IP ban. The cooldown is saved to disk
  because the next test run, minutes later, would otherwise hit the same site again.
- **Cost:** a site that blocked by mistake is lost for 12 hours.
- **Evidence:** df259af, ticket-016.

### 7.4 Cloudflare outlets are fetched with a Chrome TLS fingerprint, slower (3 s)

- **Rule:** `fetch: impersonate` in `sources.yaml` → curl_cffi, Chrome fingerprint, Referer =
  homepage, `impersonate_delay` 3 s; robots.txt still read as `AVNNewsScraper`; every safety
  check still applies.
- **Why / problem before:** Cloudflare challenged Python HTTP clients by **TLS fingerprint,
  whatever the User-Agent**. Six outlets returned a JS challenge to us and 200 to Chrome.
  The slower pace is because these sites are the ones actively watching.
- **Cost:** outlets that challenge even Chrome (somoytv, kalerkantho) stay off until a
  real-browser fallback exists (ticket-017).
- **Evidence:** df259af.

### 7.5 Dead sites: 5 timeouts in a row → skipped for this run, no cooldown

- **Why / problem before:** paharbarta.com timed out on every article, 25 s per try, one at a
  time: it alone stretched a dry run to 57 minutes. No cooldown, because a site that doesn't
  answer isn't refusing us; tomorrow it may be back.
- **Evidence:** d5c87c8.

### 7.6 Diagnose with a handful of spaced requests, never a crawl

- **Why:** a crawl to "see if it's fixed" against a site that just blocked us is exactly the
  pattern that turns a block into a ban.

### 7.7 Permanent block log, written to files first and to Postgres at the end

- **Rule:** every failure (block, timeout, 5xx, breaker stop) goes to
  `data/logs/blocks/<Dhaka date>/<time>_<mode>.jsonl` plus Parquet; imported to Postgres in
  one transaction after the scrape; never deleted.
- **Why / problem before:** after a block we couldn't tell what we'd sent, how fast, or what
  the site answered. Without history, every pacing change is a guess. Files first, so a
  database problem can't lose or stall the log; anything not imported catches up next run.
- **Evidence:** 2991093, ticket-018.

---

## 8. Time limits and resources: nothing hangs, nothing explodes

### 8.1 180-second hard limit per request

- **Why / problem before:** `timeout_seconds` (25) limits each *wait for data*. A server
  trickling bytes, or a stuck connection, could hold a request forever. Per page, not per
  outlet, so a slow outlet with many pages isn't cut short.
- **Evidence:** 8c7dc48, ticket-027.

### 8.2 Regional outlets get 5 minutes of tab paging; a whole run gets 180 minutes

- **Why / problem before:** a dry run spent its **last 12 minutes on four regional sites**.
  After 5 minutes a regional outlet stops paging tabs but keeps what it found and still reads
  feeds, sitemaps and articles. National outlets have no budget: they're where most stories
  are. The run cap is the last safety net, so a hung run can't block later scrapes.
- **Evidence:** 93c7239, ticket-028.

### 8.3 Postgres cancels a scrape query after 10 min and an API query after 30 s

- **Why:** a stuck lock should fail with an error, not hang the service. The timeout is set
  with `SET` after connecting, because passing it as `options` replaced the options in
  `DATABASE_URL` and **sent one test run to the real tables**. Tests now refuse any schema not
  named `test_*`.
- **Evidence:** 93c7239.

### 8.4 Judge and save each article as soon as it downloads; keep nothing finished

- **Rule:** download → judge → save → drop the page, one article at a time (`as_downloaded`).
  Seen links are written every 200 articles.
- **Why / problem before:** the run downloaded everything first and judged at the end: the
  57,000-article catch-up reached **20 GB with 1 GB of RAM left**. The first fix still grew
  ~1 GB a minute, because `asyncio.as_completed` over a task list keeps every finished
  task's result (the whole page). Now memory stays flat (four outlets, 882 downloads: 0.59 GB
  peak), saved articles appear during the run, and a stopped run doesn't re-download.
- **Evidence:** 98ac681, b24fbae.
- **Changed 2026-11-23 (8.8):** a fixed pool of download workers instead of one task per link,
  because finished pages still piled up when judging was slower than downloading.

### 8.5 A progress line every 60 s

- **Why / problem before:** a run looked stuck: **30 minutes at 0% CPU** with no new files.
  It wasn't; it finished in 36 minutes and 11,880 requests (paced requests don't use CPU,
  and the block log records only failures). Without the line, people kill healthy runs.
- **Evidence:** 8c7dc48.

### 8.6 Blocking work goes through `asyncio.to_thread`

- **Why:** the scrape shares the event loop with the API. Parsing, keyword matching, file
  writes and pandas on the loop would freeze the dashboard for the length of a scrape.
- **Evidence:** af4623f.

---

### 8.7 Probed pages are kept only for links that will be downloaded

- **Rule:** a date probe that finds an old article keeps only the date. After filtering,
  `_drop_unused_pages` clears the probed page of every link not selected (known, off-topic,
  foreign, capped, merged duplicates). Selected links still reuse their page (no second download).
- **Why / problem before:** every probed page stayed on its link (`c.prefetched`) until it
  was judged, and filtered-out links are never judged, so their pages lived until the run
  ended. Old articles are what the probe exists to find, so most probed pages were waste.
  Profile, 8 regional papers, dry run: 132 probed pages (26.7 MB), **all 132 unused** (131
  old); the largest single Python allocation after discovery.
- **After the fix** (same 8 papers, dry run): probed pages held **0** (was 132, 26.7 MB),
  Python memory after discovery **50.8 MB** (was 103.3; the page text and its jusText parse
  are gone), peak RSS **501 MB** (was 578). `malloc_trim` at the end returned only 76 MB
  (502 → 426), so at this size the allocator is not hoarding freed memory.
- **Not the whole story:** the 2026-11-23 14:01 daily run (first full run without the
  pre-filter, 100 min, 26,169 too-old downloads) held **12.3 GB**, 10.9 GB of it in the main
  heap. Probe pages scale to about 0.5 GB for all regional outlets, and the small profile
  doesn't reproduce the growth, so the rest is something that grows with run size. Found
  and fixed in 8.8.
- **Cost:** none; the same pages are downloaded and reused.

### 8.8 A fixed pool of download workers, links taken one site at a time

- **Rule:** `as_downloaded` runs 256 workers that take the links in order and hand each page to a
  queue of 128; the link list is first put in turns by site (`by_host_turns`: A1 B1 C1 A2 B2 ...).
  At most 256 + 128 pages are in memory. Daily run and backfill.
- **Why / problem before:** 8.4 started one task per link, all at once, with a bounded queue.
  A task that finished while the queue was full kept its page and waited for room, so when
  judging (one at a time, ~0.065 s each, ~15/s) was slower than downloading (~20/s), finished
  pages piled up in waiting tasks. Small runs never showed it (judging kept up). Full dry run,
  every link treated as new (42,894 downloads): **2,588 pages waiting at 14 min, memory 2.2 GB
  and rising ~200 MB/min**; the 2026-11-23 14:01 daily run reached **12.3 GB**. Stopped there.
- **After the fix**, same full dry run (42,992 downloads): memory **flat at 1.42 GB** from minute
  27 to the end (peak 1.43 GB; 845 MB after `malloc_trim`), 110.8 min. Judging rate in the first
  16 min rose from ~15 to ~21 articles/s: workers spread over many sites instead of queueing on
  one site's delay. The run's length is set by its biggest sites (dainikcoxsbazar 2,858,
  inqilab 2,820, sirajganjprotidin 2,539 requests at 1-1.5 s each), mostly too-old links.
- **Cost:** none seen. A site's pace is unchanged; workers only limit how many pages wait.
- **Evidence:** ticket-038.

## 9. Backfill (past months and days)

### 9.1 Backfill is on demand and separate from the daily run

- **Why:** the daily run must stay short and predictable (1.2). Months of history mean
  hours of requests; that's a deliberate decision someone makes, not a side effect.
- **Evidence:** 95cf166.

### 9.2 Old articles come from archive sitemaps, not tabs or homepages

- **Why:** homepages, tabs and RSS only show recent news; reaching 2024 by paging a tab
  would be thousands of listing pages. Dated sitemaps list a day's or month's articles
  directly. The index usually lists only recent files, so the URL pattern is learned and
  filled in for the dates asked.
- **Cost:** outlets without a usable sitemap (janakantha, ntv, observerbd, unb, rtv, ekhontv;
  jamunatv and dailysun list `#`) can't be backfilled.

### 9.3 Backfill is slower per site (2 s) and pauses while the daily run runs

- **Why:** a backfill runs for hours; a long steady stream is what firewalls notice. It
  pauses during the daily run because the two have separate `Fetcher`s, so running together
  would hit each site at **twice** the rate either one promises (same reason as 1.4).
- **Cost:** slower. Backfill time ≈ requests to the busiest site × 2-3 s.
- **Partly replaced 2026-11-23 by 9.8:** four sites with a clean block record go at 1 s.

### 9.8 Big sites with zero blocks go at 1 s in a backfill; 2 blocks in the run stop any site

- **Rule:** `backfill.fast_sources` (nayadiganta, prothomalo, dhakapost, bonikbarta) at
  `fast_delay` 1 s + jitter (1-1.5 s); every other site stays at 2 s. Homepage host only,
  never below the daily 1 s, never `impersonate` sites. A backfill stops a site after
  `backfill.block_threshold` 2 blocks in the run (not only in a row; daily run: 3 in a row).
- **Why / problem before:** the 2026-11-08..15 quarter took **89 min**, and nayadiganta alone
  set that length: 1,786 requests × ~2.5 s. Its sitemap has no headlines, so every article is
  downloaded (`untitled: download`); 112 of ~1,700 were saved. Most other sites had finished
  in the first 20 minutes, leaving the 32 download slots idle.
- **Evidence for the four:** zero blocks in every logged run, daily runs at 1 s included
  (requests at 1 s in daily runs / at 2 s in backfills: nayadiganta 1,350 / 2,029;
  prothomalo 1,471 / 1,103; dhakapost 290 / 1,352; bonikbarta 925 / 930). Bot blocks seen so far come from fingerprints and
  arrive within 2-3 requests at any pace (kalerkantho), not from speed.
- **Why the stricter breaker:** 1 s for 30-40 minutes non-stop is more than these sites have
  seen from us. Stopping at the 2nd block, counted across the run, backs off before a firewall
  escalates, including a site that alternates blocks and answers.
- **Cost / risk:** higher volume per minute to four sites than the epic's rule 1 allowed;
  the user chose it on 2026-11-23. A ban would apply to the IP, so the daily run on the same
  machine would lose the site too. Check the block log after each backfill.
- **Replaces:** the "speed up below the delay" rejection in section 11, for these four sites.

### 9.4 Pre-filter on the sitemap headline or URL slug; untitled entries downloaded by default

- **Rule:** with a headline, only keyword hits are downloaded. With none (Prothom Alo's
  `/uyrdgbm7hw`), `backfill.untitled: download` (default) downloads to judge; `skip` leaves out.
  Untitled media links (video, photo, live, ...) are never downloaded.
- **Why:** without the pre-filter a month is 3,000-9,000 downloads per outlet. Untitled
  entries default to download because skipping them silently drops most of Prothom Alo.
- **Cost:** the headline pre-filter misses stories whose keywords are only in the body; the
  daily run doesn't use it (4.1). How many is unmeasured (ticket-029).

### 9.5 Days, day ranges and quarters, not only months

- **Why / problem before:** a month can mean thousands of requests to one site over hours.
  Smaller periods read only the daily files they need and give natural stopping points.
  Entries from undated-name files are kept when `lastmod` is in the period plus a slack
  (2 days for a day, up to 31 for a month), because edits move `lastmod` later.
- **Cost:** an article edited later than the slack is missed.
- **Evidence:** b0af46b.

### 9.6 Progress per (outlet, period); a day never marks its month done

- **Why:** a backfill gets interrupted (blocks, PC off, cooldowns). Re-running the same
  command must skip finished work and retry the rest, never redo hours of requests.
- **Evidence:** 95cf166, b0af46b.

### 9.7 Undated sitemap series: stop after the first file, mark `no_archive`

- **Why:** many local sitemaps list articles with no `lastmod` and no date in the name, so no
  period can be picked. Reading the whole series would be hundreds of requests for nothing.
- **Evidence:** d80bfa3.

---

## 10. Safety against hostile pages

### 10.1 Only public addresses, checked on every redirect and on the connected peer

- **Why:** a page (or a DNS answer that changes between check and connect: DNS rebinding)
  could point the scraper at `localhost`, the router or cloud metadata. Fail closed on DNS errors.
- **Evidence:** 8cd3fb4, 8d9eb78.

### 10.2 Response size capped while streaming; gzip inflation bounded; stacked encodings refused

- **Why:** a 20 GB response or a gzip bomb would exhaust memory. **Evidence:** 8cd3fb4.

### 10.3 Bangla text in a response header doesn't break the fetch

- **Why / problem before:** a header with non-ASCII text crashed the request.
  **Evidence:** d80bfa3.

---

## 11. Decided against, or not yet

| Idea | Decision | Why |
|---|---|---|
| Speed up a site below its delay (adaptive) | Rejected for now; fixed 1 s for four clean sites since 2026-11-23 (9.8) | The breaker reacts after 3 blocks; some firewalls ban silently or extend bans on repeat. Our signals lag the damage. Revisit only for sites with months of zero blocks, never `impersonate` sites. (Epic faster-backfill, 2026-11-23) |
| Headline pre-filter in the daily run | Off by default | Lost plain-headline and print stories (4.1). |
| Fill missed days automatically | Rejected | Unbounded runs (1.2). |
| OCR of image-only e-papers | Not built | Expensive; most print stories are also on the website. |
| Real browser for hard challenges | Pending (ticket-017) | Heavy; only a few outlets need it. |
| Google News RSS catch-all | Not used | Its robots.txt disallows `/rss`. Bing News RSS exists, off by default (its terms). |

## 12. Known gaps (open)

- Pacing is keyed by host name, so `x.com` and `api.x.com` are paced separately though
  they're one server; sites on one shared hosting server are paced separately; robots.txt
  `Crawl-delay` isn't read (ticket-032).
- Downloaded non-matches aren't kept, so better keywords can't recover them (ticket-030).
- Backfill pre-filter recall is unmeasured (ticket-029).

## Timeline of changed decisions

| Date | From | To | Section |
|---|---|---|---|
| 2026-10-21 | Homepage + RSS + sitemaps | + every top-level tab with paging, + print edition | 2.1, 3.1 |
| 2026-10-31 | SQLite + JSON + R2 publish | Postgres + FastAPI | 6.3 |
| 2026-10-31 | systemd/cron timers | Service started by hand, owns the schedule | 1.3 |
| 2026-11-01 | Scrape in a thread, sync DB | All async on the API's loop | 8.6 |
| 2026-11-18 | Keep hitting refusing sites | Slow down → breaker → 12 h cooldown; Chrome fingerprint | 7.3, 7.4 |
| 2026-11-21 | Headline pre-filter, 80 per outlet | Every article, full text | 4.1 |
| 2026-11-21 | First date found on the page | JSON-LD first, Dhaka zone, bare days by day | 5.1-5.5 |
| 2026-11-21 | Concurrency 12, no timeout breaker | 32, dead-site breaker | 7.2, 7.5 |
| 2026-11-21 | 07:00, 24 h window | 23:30, 30 h window | 1.1 |
| 2026-11-22 | Regional tabs to 20 pages | Date probes, 3-page cap, 5-min budget | 3.2, 3.3, 8.2 |
| 2026-11-22 | `--since-last-run` | Fixed 30 h window; older = backfill | 1.2 |
| 2026-11-22 | All sections | Off-topic skipped; world news only about Bangladesh | 2.5, 2.6 |
| 2026-11-22 | Backfill by month only | Days, ranges, quarters | 9.5 |
| 2026-11-22 | Download all, then judge | Judge and save per download | 8.4 |
| 2026-11-23 | Probed pages kept until the run ends | Kept only for links that will be downloaded | 3.2, 8.7 |
| 2026-11-23 | Backfill 2 s for every site, 3 blocks in a row | 1 s for four clean sites, 2 blocks in the run | 9.3, 9.8 |
| 2026-11-23 | One download task per link, all started at once | 256 workers, links in turns by site; at most 384 pages in memory | 8.4, 8.8 |
