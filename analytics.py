"""Learn what works, automatically, and score projects/angles with Thompson sampling.

LinkedIn does not let self-serve apps read post stats (r_member_social is partner-only),
so the default signal is measured on GitHub instead, fully automatically:
  - visitors GitHub attributes to linkedin.com / lnkd.in (traffic referrers API), and
  - stars gained in the week after a post.
Optional: a LinkedIn analytics export (xlsx) dropped into analytics_inbox/ overrides
the GitHub-based score for the posts it covers.
"""
from __future__ import annotations

import csv
import random
import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DATE_FORMATS = ["%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%b %d, %Y"]
LINKEDIN_REFERRER = re.compile(r"linkedin|lnkd", re.IGNORECASE)
PLATFORM_REFERRERS = [
    ("LinkedIn", re.compile(r"linkedin|lnkd", re.IGNORECASE)),
    ("Mastodon", re.compile(r"mastodon|mstdn|fosstodon|hachyderm|infosec\.exchange", re.IGNORECASE)),
    ("Bluesky", re.compile(r"bsky", re.IGNORECASE)),
    ("Dev.to", re.compile(r"dev\.to", re.IGNORECASE)),
    ("X", re.compile(r"(^|\.)(t\.co|x\.com|twitter\.com)$", re.IGNORECASE)),
    ("Search", re.compile(r"google|bing|duckduckgo|yahoo|baidu", re.IGNORECASE)),
    ("GitHub", re.compile(r"github", re.IGNORECASE)),
]


def platform_of(referrer: str) -> str:
    """Map a GitHub traffic referrer (e.g. 'mastodon.social') to the platform it came from."""
    for platform, pattern in PLATFORM_REFERRERS:
        if pattern.search(referrer or ""):
            return platform
    return "Other"
EXPOSURE_UNITS = 100  # pseudo-trials per post for the GitHub-based score
STAR_WEIGHT = 3       # one star counts like three LinkedIn visitors


def collect_github_signals(db: sqlite3.Connection, user: str, repos: dict, gh_api, log) -> int:
    """Daily snapshot per repo: LinkedIn-referred visitors (trailing 14 days) and star count."""
    day = datetime.now().strftime("%Y-%m-%d")
    count = 0
    by_platform: dict[str, list[int]] = {}
    for name, repo in repos.items():
        referrers = gh_api(f"repos/{user}/{name}/traffic/popular/referrers")
        if referrers is None:
            continue
        linkedin = [entry for entry in referrers if LINKEDIN_REFERRER.search(entry.get("referrer", ""))]
        for entry in referrers:
            totals = by_platform.setdefault(platform_of(entry.get("referrer", "")), [0, 0])
            totals[0] += entry.get("count", 0)
            totals[1] += entry.get("uniques", 0)
        db.execute(
            "INSERT OR REPLACE INTO github_signals(day, repo, li_views, li_uniques, stars) VALUES(?,?,?,?,?)",
            (
                day,
                name,
                sum(entry.get("count", 0) for entry in linkedin),
                sum(entry.get("uniques", 0) for entry in linkedin),
                repo.get("stargazers_count", 0),
            ),
        )
        count += 1
    db.execute("CREATE TABLE IF NOT EXISTS github_referrers(day TEXT, platform TEXT, views INTEGER, uniques INTEGER, "
               "PRIMARY KEY(day, platform))")
    for platform, (views, uniques) in by_platform.items():
        db.execute("INSERT OR REPLACE INTO github_referrers(day, platform, views, uniques) VALUES(?,?,?,?)",
                   (day, platform, views, uniques))
    db.commit()
    log.info("GitHub signals collected for %d repos", count)
    return count


def github_performance(db: sqlite3.Connection) -> list[dict]:
    """Score each posted repo post by LinkedIn->GitHub visitors and stars gained after it went out."""
    records = []
    posts = db.execute(
        "SELECT repo, angle, posted_at FROM posts WHERE status='posted' AND repo IS NOT NULL"
    ).fetchall()
    for repo, angle, posted_at in posts:
        post_day = datetime.strptime(posted_at[:10], "%Y-%m-%d")
        if (datetime.now() - post_day).days < 3:
            continue  # too early to judge

        def snapshot(column, start, end, pick="MAX"):
            row = db.execute(
                f"SELECT {pick}({column}) FROM github_signals WHERE repo=? AND day BETWEEN ? AND ?",
                (repo, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
            ).fetchone()
            return row[0]

        baseline_visitors = snapshot("li_uniques", post_day - timedelta(days=2), post_day) or 0
        after_visitors = snapshot("li_uniques", post_day + timedelta(days=3), post_day + timedelta(days=14))
        stars_before = snapshot("stars", post_day - timedelta(days=3), post_day)
        stars_after = snapshot("stars", post_day + timedelta(days=1), post_day + timedelta(days=7))
        try_clicks = 0
        try:
            try_clicks = db.execute(
                "SELECT COALESCE(SUM(clicks), 0) FROM try_clicks WHERE repo=? AND day BETWEEN ? AND ?",
                (repo, post_day.strftime("%Y-%m-%d"), (post_day + timedelta(days=14)).strftime("%Y-%m-%d")),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass  # table not created yet
        if after_visitors is None and stars_after is None and not try_clicks:
            continue
        visitors = max((after_visitors or 0) - baseline_visitors, 0)
        stars = max((stars_after or 0) - (stars_before or stars_after or 0), 0)
        score = min(visitors + try_clicks + STAR_WEIGHT * stars, EXPOSURE_UNITS)
        records.append({
            "repo": repo,
            "angle": angle,
            "date": posted_at[:10],
            "impressions": EXPOSURE_UNITS,
            "engagements": score,
            "visitors": visitors,
            "try_clicks": try_clicks,
            "stars": stars,
        })
    return records


def _parse_date(value) -> str | None:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _to_int(value) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except ValueError:
        return 0


def _rows_from_tables(rows: list[list]) -> dict[str, dict]:
    """Find every 'Post URL' header (LinkedIn puts two tables side by side) and read its columns."""
    found: dict[str, dict] = {}
    for row_index, row in enumerate(rows):
        for col_index, cell in enumerate(row):
            if str(cell or "").strip().lower() != "post url":
                continue
            headers = [str(value or "").strip().lower() for value in row[col_index:col_index + 4]]
            for data_row in rows[row_index + 1:]:
                cell_value = data_row[col_index] if col_index < len(data_row) else None
                url = str(cell_value or "").strip()
                if "linkedin.com" not in url:
                    break
                record = found.setdefault(url, {"url": url})
                for offset, header in enumerate(headers[1:], start=1):
                    value = data_row[col_index + offset] if col_index + offset < len(data_row) else None
                    if "date" in header:
                        record["post_date"] = _parse_date(value) or record.get("post_date")
                    elif "impression" in header:
                        record["impressions"] = _to_int(value)
                    elif "engagement" in header:
                        record["engagements"] = _to_int(value)
    return found


def parse_export(path: Path) -> dict[str, dict]:
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as handle:
            return _rows_from_tables([row for row in csv.reader(handle)])
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    merged: dict[str, dict] = {}
    try:
        for sheet in workbook.worksheets:
            rows = [list(row) for row in sheet.iter_rows(values_only=True)]
            for url, record in _rows_from_tables(rows).items():
                merged.setdefault(url, {"url": url}).update({k: v for k, v in record.items() if v is not None})
    finally:
        workbook.close()
    return merged


def ingest_inbox(inbox: Path, db: sqlite3.Connection, log) -> int:
    inbox.mkdir(exist_ok=True)
    processed = inbox / "processed"
    processed.mkdir(exist_ok=True)
    count = 0
    for path in sorted(inbox.glob("*")):
        if path.suffix.lower() not in (".xlsx", ".csv"):
            continue
        try:
            records = parse_export(path)
        except Exception as error:
            log.warning("Could not read analytics file %s: %s", path.name, error)
            continue
        for record in records.values():
            db.execute(
                """INSERT INTO metrics(url, post_date, impressions, engagements, updated_at)
                   VALUES(?,?,?,?,datetime('now'))
                   ON CONFLICT(url) DO UPDATE SET
                     post_date=COALESCE(excluded.post_date, metrics.post_date),
                     impressions=MAX(excluded.impressions, metrics.impressions),
                     engagements=MAX(excluded.engagements, metrics.engagements),
                     updated_at=excluded.updated_at""",
                (record["url"], record.get("post_date"), record.get("impressions", 0), record.get("engagements", 0)),
            )
            count += 1
        db.commit()
        shutil.move(str(path), processed / f"{datetime.now():%Y%m%d_%H%M%S}_{path.name}")
        log.info("Ingested %d post metrics from %s", len(records), path.name)
    return count


def performance(db: sqlite3.Connection) -> list[dict]:
    """GitHub-measured results for every post, overridden by LinkedIn export data where available."""
    rows = db.execute(
        """SELECT p.repo, p.angle, p.posted_at, m.impressions, m.engagements
           FROM posts p JOIN metrics m ON substr(p.posted_at, 1, 10) = m.post_date
           WHERE p.status = 'posted'"""
    ).fetchall()
    linkedin_records = [
        {"repo": repo, "angle": angle, "date": posted_at[:10], "impressions": impressions or 0, "engagements": engagements or 0}
        for repo, angle, posted_at, impressions, engagements in rows
    ]
    covered_dates = {record["date"] for record in linkedin_records}
    github_records = [record for record in github_performance(db) if record["date"] not in covered_dates]
    return linkedin_records + github_records


class ThompsonScorer:
    """Engagement rate per arm with a Beta prior centred on your overall average."""

    def __init__(self, records: list[dict], key: str, prior_strength: float = 300.0):
        total_impressions = sum(record["impressions"] for record in records)
        total_engagements = sum(record["engagements"] for record in records)
        mean = total_engagements / total_impressions if total_impressions else 0.02
        mean = min(max(mean, 0.002), 0.5)
        self.alpha0 = mean * prior_strength
        self.beta0 = (1 - mean) * prior_strength
        self.arms: dict[str, list[int]] = {}
        for record in records:
            if record.get(key) is None:
                continue
            arm = self.arms.setdefault(record[key], [0, 0])
            arm[0] += record["engagements"]
            arm[1] += max(record["impressions"] - record["engagements"], 0)

    def sample(self, arm: str, rng: random.Random) -> float:
        successes, failures = self.arms.get(arm, [0, 0])
        return rng.betavariate(self.alpha0 + successes, self.beta0 + failures)

    def mean(self, arm: str) -> float:
        successes, failures = self.arms.get(arm, [0, 0])
        return (self.alpha0 + successes) / (self.alpha0 + self.beta0 + successes + failures)

    def ranked(self) -> list[tuple[str, float]]:
        return sorted(((arm, self.mean(arm)) for arm in self.arms), key=lambda item: item[1], reverse=True)
