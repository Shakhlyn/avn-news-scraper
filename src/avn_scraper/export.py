"""CSV / Excel exports for analysis in spreadsheets.

data/exports/
  daily/YYYY-MM-DD.csv        articles scraped that day (one row per article+category)
  by_category/<category>.csv  everything so far for one topic
  master.xlsx                 Summary, All, and one sheet per topic
CSV files are UTF-8 with BOM so Excel shows Bangla correctly.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from .storage import Storage

_MATCHES_SQL = """
SELECT a.published_date, c.category, a.source_name, a.source_type, a.language, a.title, a.url,
       a.district, a.districts_mentioned, c.score, c.matched_terms, a.story_cluster_id,
       a.published_at, a.scraped_at, substr(a.body, 1, 400) AS excerpt, a.body_missing,
       a.id AS article_id, a.json_path, r.verified, r.notes
FROM article_categories c
JOIN articles a ON a.id = c.article_id
LEFT JOIN review r ON r.article_id = a.id
ORDER BY a.published_date DESC, c.category, c.score DESC
"""


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def export_all(storage: Storage, exports_dir: Path, labels: dict[str, str], tz: str,
               day: date | None = None) -> dict[str, int]:
    df = storage.query_df(_MATCHES_SQL)
    counts = {"rows": len(df)}
    if df.empty:
        return counts

    scraped_local = pd.to_datetime(df["scraped_at"], utc=True).dt.tz_convert(tz).dt.date
    if day is not None:
        daily = df[scraped_local == day]
        _write_csv(daily, exports_dir / "daily" / f"{day.isoformat()}.csv")
        counts["daily"] = len(daily)

    for category, group in df.groupby("category"):
        _write_csv(group, exports_dir / "by_category" / f"{category}.csv")

    # One row per article with its categories joined, for the "All" sheet.
    all_articles = (df.groupby("article_id", sort=False)
                      .agg({**{c: "first" for c in df.columns if c not in ("article_id", "category", "score", "matched_terms")},
                            "category": lambda s: ", ".join(sorted(s))})
                      .reset_index())
    summary = (df.assign(month=df["published_date"].str[:7])
                 .pivot_table(index="month", columns="category", values="article_id",
                              aggfunc="nunique", fill_value=0)
                 .sort_index(ascending=False))

    with pd.ExcelWriter(exports_dir / "master.xlsx", engine="openpyxl") as xl:
        summary.to_excel(xl, sheet_name="Summary (per month)")
        all_articles.to_excel(xl, sheet_name="All", index=False)
        for category, group in df.groupby("category"):
            group.to_excel(xl, sheet_name=labels.get(category, category)[:31].replace("/", "-"), index=False)
    counts["articles"] = len(all_articles)
    return counts
