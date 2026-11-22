from datetime import datetime, timezone

from avn_scraper.scraper.dedupe import find_cluster
from avn_scraper.scraper.discover.catchall import unwrap_bing_link
from avn_scraper.scraper.discover.listing import find_feed_links, parse_listing
from avn_scraper.scraper.discover.rss import parse_feed
from avn_scraper.scraper.discover.sitemap import parse_sitemap
from avn_scraper.scraper.extract import extract
from avn_scraper.scraper.geo import detect_districts
from avn_scraper.scraper.urls import article_id, canonicalize, slug_text

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>BSF firing kills youth</title><link>https://example.com/a/1</link>
<description>&lt;p&gt;Youth killed at &lt;b&gt;border&lt;/b&gt;&lt;/p&gt;</description>
<pubDate>Sat, 18 Oct 2026 01:00:00 GMT</pubDate></item></channel></rss>"""

NEWS_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://example.com/bd/123</loc><news:news><news:publication_date>2026-10-18T06:00:00+06:00</news:publication_date>
<news:title>হেফাজতে মৃত্যু</news:title></news:news></url></urlset>""".encode()

SITEMAP_INDEX = b"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://example.com/sitemap-news.xml</loc><lastmod>2026-10-18</lastmod></sitemap></sitemapindex>"""


def test_parse_feed():
    [c] = parse_feed(RSS, "ex")
    assert c.title == "BSF firing kills youth"
    assert "border" in c.summary and "<b>" not in c.summary
    assert c.published_at == datetime(2026, 10, 18, 1, tzinfo=timezone.utc)


def test_parse_news_sitemap():
    children, urls = parse_sitemap(NEWS_SITEMAP)
    assert children == []
    assert urls[0]["title"] == "হেফাজতে মৃত্যু"
    assert urls[0]["published_at"] == datetime(2026, 10, 18, 0, tzinfo=timezone.utc)


def test_parse_sitemap_index():
    children, urls = parse_sitemap(SITEMAP_INDEX)
    assert children[0][0] == "https://example.com/sitemap-news.xml" and urls == []


def test_parse_listing_and_feed_links():
    html = """<html><head><link rel="alternate" type="application/rss+xml" href="/feed"></head><body>
    <a href="/national/123">সীমান্তে বিএসএফের গুলিতে যুবক নিহত</a>
    <a href="/national/123"><img src="x.jpg"></a>
    <a href="https://other.com/x">Some other site's headline here</a>
    <a href="/tag/crime">Crime news and more crime news</a>
    <a href="/about">About</a></body></html>"""
    cands = parse_listing(html, "https://www.example.com/", "https://www.example.com/", "ex")
    assert [c.url for c in cands] == ["https://www.example.com/national/123"]
    assert find_feed_links(html, "https://www.example.com/") == ["https://www.example.com/feed"]


def test_canonicalize():
    a = canonicalize("http://www.Example.com/news/1/?utm_source=fb&id=2#top")
    assert a == "https://example.com/news/1?id=2"
    assert article_id("https://example.com/news/1/amp") == article_id("https://www.example.com/news/1")


def test_slug_text_decodes_bangla():
    assert "সীমান্তে" in slug_text("https://x.com/bd/%E0%A6%B8%E0%A7%80%E0%A6%AE%E0%A6%BE%E0%A6%A8%E0%A7%8D%E0%A6%A4%E0%A7%87-1")


def test_unwrap_bing():
    link = "http://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&url=https%3a%2f%2fwww.thedailystar.net%2fnews%2f1&c=1"
    assert unwrap_bing_link(link) == "https://www.thedailystar.net/news/1"


def test_districts():
    best, mentioned = detect_districts("লালমনিরহাট সীমান্তে যুবক নিহত", "Dhaka, Oct 18 - A youth from Lalmonirhat ...")
    assert best == "Lalmonirhat" and "Dhaka" in mentioned


def test_cluster():
    recent = [("a1", "a1", "Youth shot dead by BSF at Lalmonirhat border")]
    assert find_cluster("BSF shoots dead youth at Lalmonirhat border", recent) == "a1"
    assert find_cluster("Stock market gains", recent) is None


def test_extract():
    html = """<html><head><title>X</title><meta property="og:title" content="Man dies in custody">
    <meta property="article:published_time" content="2026-10-18T07:30:00+06:00">
    <link rel="canonical" href="https://example.com/n/1"></head><body><article><h1>Man dies in custody</h1>
    <p>A man died in police custody in Gazipur on Friday night, his family alleged torture.</p>
    <p>Police denied the allegation and said he fell ill in the lock-up. An inquiry committee was formed.</p>
    </article></body></html>"""
    ex = extract(html, "https://example.com/n/1?utm_source=x")
    assert "police custody" in ex.body
    assert ex.canonical_url == "https://example.com/n/1"
    assert ex.published_at == datetime(2026, 10, 18, 1, 30, tzinfo=timezone.utc)


def test_channel24_adapter():
    import json

    from avn_scraper.scraper.adapters import find_adapter

    url = "https://www.channel24bd.tv/national/article/353669/%E0%A6%B8%E0%A7%8C%E0%A6%A6%E0%A6%BF"
    adapter, api = find_adapter(url)
    assert api == "https://backoffice.channel24bd.tv/api/v2/content-details/national/353669"
    assert find_adapter("https://www.prothomalo.com/bangladesh/abc") is None

    payload = json.dumps({
        "contentDetails": [{"ContentHeading": "দুই বাংলাদেশির মৃত্যুদণ্ড", "ContentBrief": "সংক্ষেপ",
                            "ContentDetails": "<p><strong>ঢাকা:</strong> প্রথম অনুচ্ছেদ।</p><p>দ্বিতীয়।</p>",
                            "WriterName": "ডেস্ক", "create_date": "2026-10-18 16:02:19"}],
        "writerInfo": {"WriterName": "বিশেষ প্রতিনিধি"},
    }).encode()
    ex = adapter.parse(payload)
    assert ex.title == "দুই বাংলাদেশির মৃত্যুদণ্ড"
    assert ex.body == "ঢাকা: প্রথম অনুচ্ছেদ।\nদ্বিতীয়।"
    assert ex.author == "বিশেষ প্রতিনিধি"
    assert ex.published_at == datetime(2026, 10, 18, 10, 2, 19, tzinfo=timezone.utc)


def test_extract_prefers_longer_json_ld_body():
    long_text = "সীমান্তে বিএসএফের গুলিতে যুবক নিহত হয়েছেন। " * 40
    html = f"""<html><head><script type="application/ld+json">
    {{"@context": "https://schema.org", "@type": "NewsArticle", "articleBody": "{long_text}"}}
    </script></head><body><article><h1>শিরোনাম</h1><p>সংক্ষিপ্ত অংশ মাত্র।</p></article></body></html>"""
    ex = extract(html, "https://example.com/n/2")
    assert len(ex.body) >= len(long_text.strip()) - 5


def test_find_section_links():
    from avn_scraper.scraper.discover.sections import find_section_links

    html = """<html><body><header><nav class="main-menu"><ul>
    <li><a href="/">প্রচ্ছদ</a></li>
    <li><a href="/national">জাতীয়</a>
      <ul class="dropdown-menu"><li><a href="/national">জাতীয়</a></li><li><a href="/national/capital">রাজধানী</a></li></ul></li>
    <li><a href="/%E0%A6%B8%E0%A6%BE%E0%A6%B0%E0%A6%BE%E0%A6%A6%E0%A7%87%E0%A6%B6">সারাদেশ</a></li>
    <li><a href="https://example.com/category/international/">International</a></li>
    <li><a href="/videos">ভিডিও</a></li><li><a href="/tag/crime">Crime</a></li><li><a href="/converter">Converter</a></li>
    <li><a href="https://epaper.example.com/today">ই-পেপার</a></li><li><a href="https://other.com/sports">Sports</a></li>
    <li><a href="/national/news/123456">সীমান্তে বিএসএফের গুলিতে যুবক নিহত</a></li>
    <li><a href="#">আরও</a><ul class="sub-menu"><li><a href="/law">আইন-আদালত</a></li></ul></li>
    </ul></nav></header>
    <div class="mobile-menu"><ul><li><a href="/national/capital">রাজধানী</a></li></ul></div>
    <div class="mega-menu"><ul><li><a href="/economy-all">অর্থনীতি</a></li></ul></div>
    <div class="lead"><a href="/economy">Economy</a></div>
    <footer><nav><a href="/about-us">About us</a><a href="/sports">Sports</a></nav></footer></body></html>"""
    links = find_section_links(html, "https://www.example.com/", "https://www.example.com/", limit=40)
    # sub-tab /national/capital is left out even though the flat mobile menu repeats it;
    # /law, only under a "More" button (not a tab, like a full-site mega menu), counts neither way
    assert links == ["https://www.example.com/national",
                     "https://www.example.com/%E0%A6%B8%E0%A6%BE%E0%A6%B0%E0%A6%BE%E0%A6%A6%E0%A7%87%E0%A6%B6",
                     "https://example.com/category/international/"]
    assert len(find_section_links(html, "https://www.example.com/", "https://www.example.com/", limit=2)) == 2


def test_find_print_links():
    from avn_scraper.scraper.discover.sections import find_print_links

    html = """<html><body><a href="/printversion">প্রিন্ট সংস্করণ</a>
    <a href="/topic/%E0%A6%9B%E0%A6%BE%E0%A6%AA%E0%A6%BE-%E0%A6%B8%E0%A6%82%E0%A6%B8%E0%A7%8D%E0%A6%95%E0%A6%B0%E0%A6%A3">ছাপা সংস্করণ</a>
    <a href="/epaper/2026-10-21/">ই-পেপার</a>
    <a href="https://epaper.example.com/">ই-পেপার</a>
    <a href="/about-us">আজকের পত্রিকা</a>
    <a href="/printversion/details/987654">A print article headline</a></body></html>"""
    assert find_print_links(html, "https://example.com/", "https://example.com/") == [
        "https://example.com/printversion",
        "https://example.com/topic/%E0%A6%9B%E0%A6%BE%E0%A6%AA%E0%A6%BE-%E0%A6%B8%E0%A6%82%E0%A6%B8%E0%A7%8D%E0%A6%95%E0%A6%B0%E0%A6%A3",
        "https://example.com/epaper/2026-10-21/"]


def test_page_is_past_window():
    from avn_scraper.scraper.discover.sections import page_is_past_window

    cutoff = datetime(2026, 10, 20, 1, tzinfo=timezone.utc)
    old = '<time datetime="2026-10-19T08:00:00+06:00"></time>'
    new = '<time datetime="2026-10-20T08:00:00+06:00"></time>'
    assert page_is_past_window(f"<html><body>{old}{old}{new}</body></html>", cutoff)
    assert not page_is_past_window(f"<html><body>{old}{new}{new}</body></html>", cutoff)
    # no <time>: dates in the article URLs (day precision)
    urls = ["https://x.com/2026/10/18/a", "https://x.com/2026/10/18/b", "https://x.com/news/1"]
    assert page_is_past_window("<html></html>", cutoff, urls)
    assert not page_is_past_window("<html></html>", cutoff, ["https://x.com/2026/10/20/a"])
    assert not page_is_past_window("<html></html>", cutoff, ["https://x.com/news/1"])


def test_find_next_page():
    from avn_scraper.scraper.discover.sections import find_next_page

    def page(*hrefs, head=""):
        return f"<html><head>{head}</head><body>" + "".join(f'<a href="{h}">{i}</a>' for i, h in enumerate(hrefs)) + "</body></html>"

    base = "https://example.com/national"
    assert find_next_page(page(head='<link rel="next" href="/national?page=2">'), base) == base + "?page=2"
    assert find_next_page(page("/national?page=1", "/national?page=2", "/national?page=3"), base) == base + "?page=2"
    assert find_next_page(page("/national?page=1"), base) == base + "?page=1"  # 0-based (Drupal)
    assert find_next_page(page("/national?page=2", "/national?page=4"), base + "?page=3") == base + "?page=4"
    assert find_next_page(page("/national/page/2/"), base + "/") == base + "/page/2/"
    assert find_next_page(page("/national/page/2/", "/national/page/3/"), base + "/page/2/") == base + "/page/3/"
    assert find_next_page(page("/sports?page=2", "/national/1234"), base) is None


_BODY = "<p>" + "পুলিশ হেফাজতে এক যুবকের মৃত্যু হয়েছে বলে অভিযোগ করেছে পরিবার। " * 8 + "</p>"


def test_extract_json_ld_date_beats_sidebar_time():
    html = f"""<html><head><script type="application/ld+json">
    {{"@context": "https://schema.org", "@graph": [{{"@type": "WebSite", "name": "x"}},
      {{"@type": "NewsArticle", "datePublished": "2026-11-20T10:30:00+06:00"}}]}}
    </script></head><body><aside><time datetime="2026-11-01T01:00:00Z">পুরনো</time></aside>
    <article><h1>হেফাজতে যুবকের মৃত্যু</h1>{_BODY}</article></body></html>"""
    ex = extract(html, "https://example.com/n/3")
    assert ex.published_at == datetime(2026, 11, 20, 4, 30, tzinfo=timezone.utc)
    assert not ex.date_only


def test_extract_rfc2822_and_itemprop_dates_keep_the_time():
    rfc = f"""<html><head><meta property="article:published_time" content="Fri, 20 Nov 2026 10:30:00 +0600">
    </head><body><article><h1>শিরোনাম এখানে</h1>{_BODY}</article></body></html>"""
    item = f"""<html><body><article><h1>শিরোনাম এখানে</h1>
    <span itemprop="datePublished" content="২০২৬-১১-২০T১০:৩০:০০+০৬:০০">২০ নভেম্বর</span>{_BODY}</article></body></html>"""
    for html in (rfc, item):
        ex = extract(html, "https://example.com/n/4")
        assert ex.published_at == datetime(2026, 11, 20, 4, 30, tzinfo=timezone.utc)


def test_extract_ignores_modified_date_and_flags_bare_day():
    html = f"""<html><head><meta property="article:modified_time" content="2026-11-20T10:30:00+06:00">
    <meta name="pubdate" content="2026-11-19"></head><body><article><h1>শিরোনাম এখানে</h1>{_BODY}</article></body></html>"""
    ex = extract(html, "https://example.com/n/5")
    assert ex.published_at.date().isoformat() == "2026-11-19"
    assert ex.date_only


def test_extract_title_drops_outlet_name():
    head = '<meta property="og:site_name" content="প্রথম আলো">'
    # the page headline (<h1>) matches the longer side of the title
    html = f"""<html><head><title>হেফাজতে যুবকের মৃত্যু | প্রথম আলো</title>{head}</head>
    <body><h1>প্রথম আলো</h1><article><h1>হেফাজতে যুবকের মৃত্যু</h1>{_BODY}</article></body></html>"""
    assert extract(html, "https://example.com/n/6").title == "হেফাজতে যুবকের মৃত্যু"
    # no usable <h1>: the og:site_name part is dropped
    html = f"""<html><head><title>প্রথম আলো - হেফাজতে যুবকের মৃত্যু</title>{head}</head>
    <body><article>{_BODY}</article></body></html>"""
    assert extract(html, "https://example.com/n/7").title == "হেফাজতে যুবকের মৃত্যু"


def test_extract_prefers_longer_itemprop_body():
    long_text = "<p>সীমান্তে বিএসএফের গুলিতে যুবক নিহত হয়েছেন।</p>" * 40
    html = f"""<html><body><h1>শিরোনাম এখানে</h1><p>সংক্ষিপ্ত অংশ মাত্র।</p>
    <div itemprop="articleBody">{long_text}</div></body></html>"""
    assert extract(html, "https://example.com/n/9").body.count("নিহত") >= 40


def test_judge_compares_bare_day_by_day():
    import asyncio
    from zoneinfo import ZoneInfo

    from avn_scraper.scraper.discover import Candidate
    from avn_scraper.scraper.extract import Extracted
    from avn_scraper.scraper.pipeline import Judge

    class NoMatch:
        def classify(self, *_):
            return []

    tz = ZoneInfo("Asia/Dhaka")
    judge = Judge(classifier=NoMatch(), storage=None, tz=tz, run_id="r", by_id={}, recent=[],
                  window_from=datetime(2026, 11, 20, 7, 0, tzinfo=tz),
                  scraped_at=datetime(2026, 11, 21, 1, 0, tzinfo=timezone.utc))

    def outcome(day: datetime, feed: datetime | None = None) -> str:
        c = Candidate(url="https://example.com/n/10", source_id="x", title="t", published_at=feed)
        ex = Extracted("t", "body", "", "", day, None, None, date_only=True)
        return asyncio.run(judge.article(c, "<html>", ex, "")).outcome

    # yesterday as a bare day is midnight, before 07:00, but the day is in the window
    assert outcome(datetime(2026, 11, 20, tzinfo=timezone.utc)) == "no_match"
    assert outcome(datetime(2026, 11, 19, tzinfo=timezone.utc)) == "too_old"
    # a full timestamp from the feed wins over the bare day
    assert outcome(datetime(2026, 11, 20, tzinfo=timezone.utc),
                   feed=datetime(2026, 11, 20, 0, 30, tzinfo=timezone.utc)) == "too_old"


def test_extract_huge_title_is_cut_not_split():
    import time

    title = " | ".join(["শব্দ"] * 50_000)
    html = f"<html><head><title>{title}</title></head><body><h1>শিরোনাম এখানে</h1>{_BODY}</body></html>"
    start = time.monotonic()
    ex = extract(html, "https://example.com/n/11")
    assert time.monotonic() - start < 5
    assert len(ex.title) <= 500


def test_extract_naive_time_is_dhaka_and_midnight_is_a_bare_day():
    html = f"""<html><head><script type="application/ld+json">
    {{"@type": "NewsArticle", "datePublished": "2026-10-19 22:03:00"}}</script></head>
    <body><article><h1>শিরোনাম এখানে</h1>{_BODY}</article></body></html>"""
    ex = extract(html, "https://example.com/n/12")
    assert ex.published_at == datetime(2026, 10, 19, 16, 3, tzinfo=timezone.utc)
    html = f"""<html><head><meta property="article:published_time" content="2026-10-27T00:00:00+06:00">
    </head><body><article><h1>শিরোনাম এখানে</h1>{_BODY}</article></body></html>"""
    assert extract(html, "https://example.com/n/13").date_only


def test_extract_skips_site_wide_json_ld_date():
    # same NewsArticle block on every page: homepage as its page, stale date
    html = f"""<html><head><script type="application/ld+json">
    {{"@type": "NewsArticle", "mainEntityOfPage": {{"@type": "WebPage", "@id": "https://example.com/"}},
      "headline": "এখন টিভি", "datePublished": "2024-03-19"}}</script></head>
    <body><article><h1>শিরোনাম এখানে</h1><time datetime="2026-10-27T15:15:13.840Z">আজ</time>{_BODY}</article></body></html>"""
    ex = extract(html, "https://example.com/n/14")
    assert ex.published_at == datetime(2026, 10, 27, 15, 15, 13, 840000, tzinfo=timezone.utc)


def test_extract_visible_date_next_to_headline_not_the_masthead_clock():
    from zoneinfo import ZoneInfo
    now = datetime(2026, 11, 21, 20, 0, tzinfo=ZoneInfo("Asia/Dhaka"))
    head = '<div class="top_bar"><div class="top_bar_date">শনিবার, ২১ নভেম্বর ২০২৬</div></div>'
    head += '<span class="today-date">Saturday , November 21 2026</span>'
    related = '<aside><p class="post-meta"><span class="tie-date">November 21, 2026</span></p></aside>'
    # date written in the meta line after the headline (WordPress theme)
    html = f"""<html><body>{head}<div class="post"><h1 class="post-title">২০২৫ সালের ছুটির তালিকা প্রকাশ</h1>
    <p class="post-meta"><span class="tie-date">December 24, 2024</span></p>{_BODY}</div>{related}</body></html>"""
    ex = extract(html, "https://example.com/9420", now)
    assert ex.published_at.date().isoformat() == "2024-12-24" and ex.date_only
    # Bangla date in the headline's own header block, above the <h1>
    html = f"""<html><body>{head}<header class="entry-header"><div class="meta"><span>প্রকাশ: ১৫ অক্টোবর ২০২৬,
    ১০:১৩ অপরাহ্ন</span></div><h1>সৌদিতে দুই বাংলাদেশির মৃত্যুদণ্ড কার্যকর</h1></header>{_BODY}</body></html>"""
    assert extract(html, "https://example.com/1", now).published_at.date().isoformat() == "2026-10-15"
    # relative date
    html = f"""<html><body>{head}<h1>শিরোনাম এখানে আছে</h1><span class="time">৩ দিন আগে</span>{_BODY}</body></html>"""
    assert extract(html, "https://example.com/2", now).published_at.date().isoformat() == "2026-11-18"


def test_extract_local_news_templates_give_the_story_date_not_today():
    """Regional sites seen on 2026-11-22: without these, every old story read as today and
    tab paging never stopped."""
    from zoneinfo import ZoneInfo
    now = datetime(2026, 11, 22, 20, 56, tzinfo=ZoneInfo("Asia/Dhaka"))
    clock = '<div class="container"><div class="date">8:56 pm, Sunday, 22 November 2026</div></div>'
    # mkantho (WordPress "itpolly" theme): the only date is labelled "Update Time"
    html = f"""<html><body>{clock}<div class="single-white2"><h1>পল্লবীতে পুলিশের গাড়িতে আগুন</h1>
    <div class="update-time"><ul><li>Update Time :
    08:10:35 am, Sunday, 1 November 2026</li></ul></div>{_BODY}</div></body></html>"""
    ex = extract(html, "https://mkantho.com/x/", now)
    assert ex.published_at.date().isoformat() == "2026-11-01" and ex.date_only
    # ajkersylhet: numeric day/month/year after a "published" label, in Bangla digits
    clock = '<div class="top_ber"><div class="top_ber_left" id="get_date">রবিবার, ২২ নভেম্বর ২০২৬</div></div>'
    html = f"""<html><body>{clock}<div class="row"><h1>ডেঙ্গু ও হামে মৃত্যু নেই ২৪ ঘণ্টায়</h1>
    <div class="col-sm-8"><div class="pDate"><p>প্রকাশিত: ২৭/১০/২০২৬ ০৩:০৩:১২ অপরাহ্ন</p></div></div>
    {_BODY}</div></body></html>"""
    assert extract(html, "https://ajkersylhet.com/news-details?nid=1", now).published_at.date().isoformat() == "2026-10-27"
    html = f"""<html><body><h1>Headline of the story</h1><p>Published: 05-11-2026</p>{_BODY}</body></html>"""
    assert extract(html, "https://example.com/3", now).published_at.date().isoformat() == "2026-11-05"


def test_extract_a_real_date_beats_an_update_stamp():
    from zoneinfo import ZoneInfo
    now = datetime(2026, 11, 22, 20, 0, tzinfo=ZoneInfo("Asia/Dhaka"))
    html = f"""<html><body><h1>শিরোনাম এখানে আছে</h1><span class="post-date">১০ নভেম্বর ২০২৬</span>
    <span class="updated">আপডেট: ২২ নভেম্বর ২০২৬</span>{_BODY}</body></html>"""
    assert extract(html, "https://example.com/4", now).published_at.date().isoformat() == "2026-11-10"


def test_extract_drops_a_guessed_bare_today():
    """trafilatura's guess on a page whose only date is the header clock: no date, not today."""
    from zoneinfo import ZoneInfo
    from avn_scraper.scraper import extract as X
    from selectolax.lexbor import LexborHTMLParser
    now = datetime(2026, 11, 22, 20, 0, tzinfo=ZoneInfo("Asia/Dhaka"))
    tree = LexborHTMLParser("<html><body><h1>no date here at all</h1></body></html>")
    assert X._published(tree, [], "2026-11-22", now) == (None, False)
    dt, bare = X._published(tree, [], "2026-11-20", now)
    assert dt.date().isoformat() == "2026-11-20" and bare
    dt, bare = X._published(tree, [], "2026-11-22T09:15:00+06:00", now)
    assert dt.hour == 9 and not bare
