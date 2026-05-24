"""
commodity_news_collector.py
────────────────────────────────────────────────────────────────────────────
Collects news articles and events for trade commodities using GDELT only.
No API key, no BigQuery, completely free.

How GDELT works (no BigQuery needed)
──────────────────────────────────────
  Articles  →  GDELT DOC 2.0 REST API
               https://api.gdeltproject.org/api/v2/doc/doc
               Returns JSON list of articles matching a keyword + date range.

  Events    →  GDELT Event 2.0 bulk CSV files
               GDELT publishes a new 15-minute CSV zip every 15 minutes at:
               http://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.export.CSV.zip
               Each row is one real-world event (protest, sanction, trade deal)
               with 58 columns including actor, location, tone, event code.
               This script downloads files across your date window, filters rows
               mentioning your commodity keyword, and saves matches.
               No database, no BigQuery, no account required.

Install
───────
  pip install requests

Usage
─────
  # Dry run first — shows token plan, no API calls
  python commodity_news_collector.py --plan commodity_news_plan.csv --dry-run

  # Real run
  python commodity_news_collector.py --plan commodity_news_plan.csv

  # Control how many event files are scanned per commodity (default 30 = one per day)
  python commodity_news_collector.py --plan commodity_news_plan.csv --event-files 60

Output
──────
  output/
    articles.csv       one row per article
    events.csv         one row per matched GDELT event
    linking_table.csv  aggregated commodity x month, JOIN-ready for trade data
    run_log.json       token spend ledger
"""

import argparse
import csv
import io
import json
import logging
import os
import re
import sqlite3
import hashlib
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    raise SystemExit("Missing dependency:  pip install requests")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("commodity_news")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
})


# ═══════════════════════════════════════════════════════════════════════════
# TOKEN BUDGET
# ═══════════════════════════════════════════════════════════════════════════

class TokenBudget:
    COSTS = {
        ("recent",  "article"): 1,
        ("1year",   "article"): 5,
        ("2year",   "article"): 10,
        ("5year",   "article"): 25,
        ("10year",  "article"): 50,
        ("recent",  "event"):   5,
        ("1year",   "event"):   20,
        ("2year",   "event"):   40,
        ("5year",   "event"):   100,
    }
    BUFFER = 100

    def __init__(self, ceiling: int):
        self.ceiling = ceiling
        self.spent   = 0
        self.ledger  = []

    @property
    def available(self):
        return self.ceiling - self.BUFFER - self.spent

    def cost_of(self, window: str, stype: str) -> int:
        return self.COSTS.get((window, stype), 1)

    def charge(self, commodity: str, window: str, stype: str):
        cost = self.cost_of(window, stype)
        if cost > self.available:
            raise RuntimeError(
                f"Budget exhausted — {self.available} tokens left, "
                f"need {cost} for '{commodity}' ({stype})"
            )
        self.spent += cost
        self.ledger.append({
            "commodity": commodity, "type": stype,
            "window": window, "cost": cost,
            "ts": datetime.utcnow().isoformat(),
        })
        log.info(
            f"  [{commodity}] -{cost} tok ({stype})  "
            f"|  {self.available} remaining"
        )

    def summary(self):
        return {
            "ceiling":   self.ceiling,
            "buffer":    self.BUFFER,
            "spent":     self.spent,
            "available": self.available,
            "ledger":    self.ledger,
        }


# ═══════════════════════════════════════════════════════════════════════════
# SQLITE CACHE  — never re-query what you already have
# ═══════════════════════════════════════════════════════════════════════════

class Cache:
    def __init__(self, path: str):
        self.con = sqlite3.connect(path)
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS r "
            "(k TEXT PRIMARY KEY, body TEXT, ts TEXT)"
        )
        self.con.commit()

    def _k(self, *parts) -> str:
        return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()

    def get(self, *parts) -> Optional[list]:
        row = self.con.execute(
            "SELECT body FROM r WHERE k=?", (self._k(*parts),)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, data: list, *parts):
        self.con.execute(
            "INSERT OR REPLACE INTO r VALUES (?,?,?)",
            (self._k(*parts), json.dumps(data), datetime.utcnow().isoformat()),
        )
        self.con.commit()


# ═══════════════════════════════════════════════════════════════════════════
# DATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def date_range(window: str):
    today = datetime.utcnow().date()
    if window == "recent":
        return str(today - timedelta(days=30)), str(today)
    years = int(re.match(r"(\d+)year", window).group(1))
    return str(today - timedelta(days=365 * years)), str(today)


# ═══════════════════════════════════════════════════════════════════════════
# GDELT DOC 2.0  —  ARTICLE SEARCH
# ═══════════════════════════════════════════════════════════════════════════

GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"


def gdelt_articles(commodity: str, window: str, retries: int = 3) -> list:
    from_str, to_str = date_range(window)

    # GDELT datetime format: YYYYMMDDHHMMSS
    from_dt = from_str.replace("-", "") + "000000"
    to_dt   = to_str.replace("-", "")   + "235959"

    params = {
        "query":         f'"{commodity}"',
        "mode":          "artlist",
        "maxrecords":    250,
        "startdatetime": from_dt,
        "enddatetime":   to_dt,
        "format":        "json",
    }

    for attempt in range(retries):
        try:
            resp = SESSION.get(GDELT_DOC, params=params, timeout=30)
            resp.raise_for_status()
            data     = resp.json()
            raw_list = data.get("articles", [])
            results  = []
            for a in raw_list:
                raw_date = a.get("seendate", "")   # YYYYMMDDTHHMMSSZ
                date_clean = raw_date[:8]
                if len(date_clean) == 8:
                    date_clean = f"{date_clean[:4]}-{date_clean[4:6]}-{date_clean[6:8]}"
                results.append({
                    "title":    a.get("title", "").strip(),
                    "url":      a.get("url", ""),
                    "date":     date_clean,
                    "source":   a.get("domain", ""),
                    "language": a.get("language", ""),
                })
            log.info(f"  GDELT articles: {len(results)} returned")
            return results

        except Exception as e:
            wait = 2 ** attempt
            log.warning(f"  Attempt {attempt+1} failed ({e}), retrying in {wait}s")
            time.sleep(wait)

    log.error("  Article search failed after all retries")
    return []


# ═══════════════════════════════════════════════════════════════════════════
# GDELT EVENT 2.0  —  BULK CSV DOWNLOAD  (no BigQuery)
# ═══════════════════════════════════════════════════════════════════════════
#
# GDELT publishes a new zip every 15 minutes containing all events detected
# in that window.  Each file is tab-separated with 58 columns.
# Key columns used here:
#   [1]  SQLDATE          YYYYMMDD
#   [5]  Actor1Name
#   [6]  Actor1CountryCode
#   [10] Actor2Name
#   [11] Actor2CountryCode
#   [26] EventCode        CAMEO code
#   [30] GoldsteinScale   cooperation/conflict score -10 to +10
#   [31] NumMentions
#   [34] AvgTone          average tone of source articles
#   [53] ActionGeo_FullName
#   [57] SOURCEURL
#
# Full schema:
#   https://www.gdeltproject.org/data/lookups/CSV.header.historical.txt

GDELT_EVENT_URL = "http://data.gdeltproject.org/gdeltv2/{ts}.export.CSV.zip"

TRADE_CAMEO = {
    "042":  "Appeal to impose sanctions",
    "0421": "Appeal for economic sanctions",
    "0423": "Appeal for embargo",
    "061":  "Cooperate economically",
    "171":  "Impose administrative sanction",
    "172":  "Impose embargo",
    "173":  "Impose embargo",
    "174":  "Impose sanctions",
    "1741": "Impose economic sanctions",
    "200":  "Use conventional military force",
}


def gdelt_events(commodity: str, window: str, max_files: int = 30) -> list:
    from_str, to_str = date_range(window)
    start = datetime.strptime(from_str, "%Y-%m-%d")
    end   = datetime.strptime(to_str,   "%Y-%m-%d")

    timestamps = []
    current = start

    while current <= end and len(timestamps) < max_files:
        timestamps.append(current.strftime("%Y%m%d") + "120000")
        current += timedelta(days=1)

    keyword = commodity.lower()
    results = []
    scanned = 0
    errors = 0

    for ts in timestamps:
        url = GDELT_EVENT_URL.format(ts=ts)

        retries = 3
        delay = 5

        for attempt in range(retries):
            try:
                resp = SESSION.get(url, timeout=30)

                if resp.status_code == 404:
                    break

                # RATE LIMIT HANDLING
                if resp.status_code == 429:
                    log.warning(f"Rate limited on {ts}, sleeping {delay}s")
                    time.sleep(delay)
                    delay *= 2
                    continue

                resp.raise_for_status()

                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    with zf.open(zf.namelist()[0]) as f:

                        reader = csv.reader(
                            io.TextIOWrapper(
                                f,
                                encoding="utf-8",
                                errors="replace"
                            ),
                            delimiter="\t"
                        )

                        for row in reader:
                            if len(row) < 58:
                                continue

                            joined = "\t".join(row).lower()

                            if keyword not in joined:
                                continue

                            raw_date = row[1]

                            date_fmt = (
                                f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                                if len(raw_date) == 8 else raw_date
                            )

                            code = row[26]

                            results.append({
                                "date": date_fmt,
                                "actor1": row[5],
                                "actor1_country": row[6],
                                "actor2": row[10],
                                "actor2_country": row[11],
                                "event_code": code,
                                "event_label": TRADE_CAMEO.get(code, ""),
                                "goldstein_scale": row[30],
                                "num_mentions": row[31],
                                "avg_tone": row[34],
                                "location": row[53],
                                "source_url": row[57],
                            })

                scanned += 1

                # MUCH SAFER DELAY
                time.sleep(1.5)

                break

            except zipfile.BadZipFile:
                errors += 1
                break

            except Exception as e:
                errors += 1
                log.warning(f"Event file {ts}: {e}")

                if attempt < retries - 1:
                    time.sleep(delay)
                    delay *= 2

    log.info(
        f"GDELT events: {scanned} files scanned, "
        f"{len(results)} matches, {errors} errors"
    )

    return results


# ═══════════════════════════════════════════════════════════════════════════
# SIGNAL FLAGS + SENTIMENT
# ═══════════════════════════════════════════════════════════════════════════

SIGNAL_PATTERNS = {
    "tariff":      r"\btariff",
    "sanction":    r"\bsanction",
    "embargo":     r"\bembargo",
    "shortage":    r"\bshortage",
    "surplus":     r"\bsurplus",
    "ban":         r"\bban\b",
    "quota":       r"\bquota",
    "price_spike": r"\bprice.{0,10}(spike|surge|jump|soar|rise)",
    "weather":     r"\b(drought|flood|frost|hurricane|typhoon|heatwave|freeze)",
    "strike":      r"\b(strike|walkout|labor.dispute)",
    "export_ban":  r"\bexport.{0,6}ban",
}


def flag_signals(text: str) -> str:
    t = text.lower()
    return "|".join(k for k, p in SIGNAL_PATTERNS.items() if re.search(p, t))


def simple_sentiment(text: str) -> Optional[float]:
    if not text:
        return None
    pos = len(re.findall(
        r"\b(gain|rise|surge|grow|strong|record|high|boost|deal|agreement|supply)\b",
        text.lower()
    ))
    neg = len(re.findall(
        r"\b(fall|drop|decline|shortage|sanction|ban|crisis|cut|loss|weak|low|risk)\b",
        text.lower()
    ))
    total = pos + neg
    return 0.0 if total == 0 else round((pos - neg) / total, 4)


# ═══════════════════════════════════════════════════════════════════════════
# AGGREGATION HELPER
# ═══════════════════════════════════════════════════════════════════════════

def _agg_update(agg, jk, name, hs, sector, tier, ym,
                sent=None, signals="", is_article=False, is_event=False):
    if jk not in agg:
        agg[jk] = {
            "commodity": name, "hs_code": hs, "sector": sector,
            "tier": tier, "year_month": ym,
            "article_count": 0, "event_count": 0,
            "sentiment_sum": 0.0, "sentiment_n": 0,
            **{f"signal_{k}": 0 for k in SIGNAL_PATTERNS},
        }
    if is_article:
        agg[jk]["article_count"] += 1
    if is_event:
        agg[jk]["event_count"] += 1
    if sent is not None:
        agg[jk]["sentiment_sum"] += sent
        agg[jk]["sentiment_n"]   += 1
    for sig in (signals or "").split("|"):
        key = f"signal_{sig}"
        if key in agg[jk]:
            agg[jk][key] += 1


def _write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log.info(f"Wrote {len(rows):>6} rows  →  {path}")


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def collect(plan_csv, out_dir, budget_ceil, event_files, dry_run):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    cache  = Cache(os.path.join(out_dir, "cache.db"))
    budget = TokenBudget(budget_ceil)

    if not os.path.exists(plan_csv):
        raise FileNotFoundError(
            f"Plan file not found: {plan_csv}\n"
            "Export it from the token planner widget, paste into Notepad, "
            "save as commodity_news_plan.csv in the same folder as this script."
        )

    with open(plan_csv, newline="", encoding="utf-8-sig") as f:
        plan = list(csv.DictReader(f))

    log.info(f"Plan loaded: {len(plan)} commodities from {plan_csv}")

    # Preview total token cost
    preview = sum(
        budget.cost_of(r["search_window"], "article") +
        (budget.cost_of(r["search_window"], "event")
         if r.get("include_event_search", "no").strip().lower() == "yes" else 0)
        for r in plan
    )
    status = "OK" if preview <= budget_ceil - budget.BUFFER else "OVER BUDGET"
    log.info(f"Estimated token spend: {preview} / {budget_ceil}  [{status}]")

    if dry_run:
        log.info("\nDry-run plan:")
        for r in plan:
            ev = r.get("include_event_search", "no").strip().lower() == "yes"
            log.info(
                f"  {r['commodity']:<35}  tier={r['tier']}  "
                f"window={r['search_window']}  events={'yes' if ev else 'no '}"
                f"  tokens={budget.cost_of(r['search_window'],'article') + (budget.cost_of(r['search_window'],'event') if ev else 0)}"
            )
        log.info(f"\nTotal: {preview} tokens.  Re-run without --dry-run to collect.")
        return

    art_fields = [
        "join_key", "commodity", "hs_code", "sector", "tier",
        "date", "year_month", "title", "source", "url", "language",
        "sentiment", "trade_signals",
    ]
    ev_fields = [
        "join_key", "commodity", "hs_code", "tier",
        "date", "year_month",
        "actor1", "actor1_country", "actor2", "actor2_country",
        "event_code", "event_label",
        "goldstein_scale", "num_mentions", "avg_tone",
        "location", "source_url",
    ]

    all_articles = []
    all_events   = []
    link_agg     = {}

    for row in plan:
        name   = row["commodity"]
        hs     = row["hs_code"]
        sector = row["sector"]
        tier   = int(row["tier"])
        window = row["search_window"]
        do_ev  = row.get("include_event_search", "no").strip().lower() == "yes"

        log.info(f"\n── {name}  (tier {tier}, {window})")

        # Articles
        try:
            budget.charge(name, window, "article")
        except RuntimeError as e:
            log.warning(f"SKIP {name}: {e}")
            continue

        cached = cache.get(name, window, "article")
        if cached is not None:
            articles = cached
            log.info(f"  cache hit: {len(articles)} articles")
        else:
            articles = gdelt_articles(name, window)
            cache.put(articles, name, window, "article")
            time.sleep(0.5)

        for art in articles:
            dt   = art.get("date", "")
            ym   = dt[:7] if len(dt) >= 7 else dt
            jk   = f"{hs}_{ym.replace('-', '')}"
            text = art.get("title", "")
            sent = simple_sentiment(text)
            sigs = flag_signals(text)
            all_articles.append({
                "join_key": jk, "commodity": name, "hs_code": hs,
                "sector": sector, "tier": tier, "date": dt,
                "year_month": ym, "title": text,
                "source": art.get("source", ""), "url": art.get("url", ""),
                "language": art.get("language", ""),
                "sentiment": sent, "trade_signals": sigs,
            })
            _agg_update(link_agg, jk, name, hs, sector, tier, ym,
                        sent=sent, signals=sigs, is_article=True)

        # Events
        if do_ev:
            try:
                budget.charge(name, window, "event")
            except RuntimeError as e:
                log.warning(f"SKIP events for {name}: {e}")
            else:
                cached_ev = cache.get(name, window, "event")
                if cached_ev is not None:
                    events = cached_ev
                    log.info(f"  cache hit: {len(events)} events")
                else:
                    events = gdelt_events(name, window, max_files=event_files)
                    cache.put(events, name, window, "event")
                    time.sleep(0.3)

                for ev in events:
                    dt = ev.get("date", "")
                    ym = dt[:7] if len(dt) >= 7 else dt
                    jk = f"{hs}_{ym.replace('-', '')}"
                    all_events.append({
                        "join_key": jk, "commodity": name, "hs_code": hs,
                        "tier": tier, "date": dt, "year_month": ym,
                        **{k: ev.get(k, "") for k in [
                            "actor1", "actor1_country", "actor2",
                            "actor2_country", "event_code", "event_label",
                            "goldstein_scale", "num_mentions", "avg_tone",
                            "location", "source_url",
                        ]},
                    })
                    _agg_update(link_agg, jk, name, hs, sector, tier, ym,
                                is_event=True)

    # Write outputs
    _write_csv(os.path.join(out_dir, "articles.csv"),  art_fields, all_articles)
    _write_csv(os.path.join(out_dir, "events.csv"),    ev_fields,  all_events)

    link_fields = [
        "join_key", "commodity", "hs_code", "sector", "tier", "year_month",
        "article_count", "event_count", "avg_sentiment",
        *[f"signal_{k}" for k in SIGNAL_PATTERNS],
    ]
    link_rows = []
    for jk, a in sorted(link_agg.items()):
        avg_s = (
            round(a["sentiment_sum"] / a["sentiment_n"], 4)
            if a["sentiment_n"] > 0 else None
        )
        link_rows.append({
            "join_key": jk, "commodity": a["commodity"],
            "hs_code": a["hs_code"], "sector": a["sector"],
            "tier": a["tier"], "year_month": a["year_month"],
            "article_count": a["article_count"],
            "event_count":   a["event_count"],
            "avg_sentiment": avg_s,
            **{f"signal_{k}": a.get(f"signal_{k}", 0) for k in SIGNAL_PATTERNS},
        })
    _write_csv(os.path.join(out_dir, "linking_table.csv"), link_fields, link_rows)

    with open(os.path.join(out_dir, "run_log.json"), "w") as f:
        json.dump(budget.summary(), f, indent=2)

    log.info(f"\n{'─'*55}")
    log.info(f"  Articles:     {len(all_articles)}")
    log.info(f"  Events:       {len(all_events)}")
    log.info(f"  Linking rows: {len(link_rows)}")
    log.info(f"  Tokens spent: {budget.spent} / {budget.ceiling}")
    log.info(f"  Output dir:   {out_dir}/")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Collect GDELT commodity news → trade data linking table.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Join example (paste into your notebook):
  import pandas as pd
  trade = pd.read_csv("trade_data.csv")
  trade["join_key"] = trade["hs_code"].astype(str) + "_" + trade["year_month"].astype(str).str.replace("-","")
  news  = pd.read_csv("output/linking_table.csv")
  merged = trade.merge(news, on="join_key", how="left")
"""
    )
    parser.add_argument("--plan", required=True,
        help="Path to commodity_news_plan.csv from the token planner")
    parser.add_argument("--out", default="./output",
        help="Output directory (default: ./output)")
    parser.add_argument("--budget", type=int, default=2000,
        help="Token budget ceiling (default: 2000)")
    parser.add_argument("--event-files", type=int, default=30,
        help="Max GDELT event CSV files to scan per commodity (default: 30 = one per day)")
    parser.add_argument("--dry-run", action="store_true",
        help="Preview plan and token cost without making API calls")

    args = parser.parse_args()
    collect(
        plan_csv    = args.plan,
        out_dir     = args.out,
        budget_ceil = args.budget,
        event_files = args.event_files,
        dry_run     = args.dry_run,
    )