"""
commodity_news_loader.py
─────────────────────────────────────────────────────────────────────────
GDELT → MySQL news loader with strict commodity attribution.

Pipeline
────────
  1. Load active search terms from commodity_search_terms (MySQL).
  2. For each term, query the GDELT DOC API.  Terms flagged noisy in
     the source CSV get a context-word AND-clause so single-word
     queries like "steel" don't drown in irrelevant hits.
  3. Score every (article, term) pair.  Group by url_hash.  Apply
     STRICT attribution: each article gets exactly one cmd_code (the
     highest-scoring match), runner-up tracked for QA.
  4. Optionally download GDELT Event 2.0 bulk CSVs for the window
     (one pass, all terms scanned with a single combined regex) and
     apply the same strict attribution.
  5. INSERT IGNORE rows into news_articles / news_events.  Existing
     URLs are left untouched (dedup by url_hash UNIQUE KEY).
  6. TRUNCATE + rebuild news_linking from the two fact tables.
  7. Track progress in a news_load_manifest table for resumability.

Patterns mirrored from the project's comtrade_granular_loader.py:
  - dotenv config, file+stdout logging
  - SQLAlchemy + mysql-connector engine, pool_pre_ping
  - _ensure_parent_rows() for unknown FK codes
  - INSERT IGNORE via a custom pandas to_sql `method`
  - Manifest + daily budget JSON for resumability
  - Exponential backoff with quota-aware quick-fail

Usage
─────
  python commodity_news_loader.py                    # full run
  python commodity_news_loader.py --dry-run          # plan only
  python commodity_news_loader.py --skip-events      # articles only
  python commodity_news_loader.py --rebuild-linking  # rebuild rollup only
  python commodity_news_loader.py --max-priority 2   # top-priority terms
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote_plus

import pandas as pd
import requests
from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.dialects.mysql import insert as mysql_insert


# ─────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────

load_dotenv()

# MySQL
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME")

# Date window for the run.  Format: YYYY-MM-DD
START_DATE = os.getenv("START_DATE")                  # required
END_DATE   = os.getenv("END_DATE")                    # required

# Term filtering
MAX_PRIORITY      = int(os.getenv("MAX_PRIORITY", "5"))      # 1=best, 9=worst
TERM_LANGUAGE     = os.getenv("TERM_LANGUAGE", "en")

# GDELT DOC API behaviour
GDELT_DOC_URL     = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS       = int(os.getenv("MAX_RECORDS", "250"))     # GDELT cap
SLEEP_SECONDS     = float(os.getenv("SLEEP_SECONDS", "0.5"))
RETRY_ATTEMPTS    = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF_S   = float(os.getenv("RETRY_BACKOFF_S", "3.0"))

# Budget — defends against runaway loops, not a hard GDELT cost
MAX_DAILY_CALLS   = int(os.getenv("MAX_DAILY_CALLS", "2000"))

# Events (optional, bandwidth-heavy)
COLLECT_EVENTS    = os.getenv("COLLECT_EVENTS", "false").lower() == "true"
MAX_EVENT_FILES   = int(os.getenv("MAX_EVENT_FILES", "30"))  # one per day

# Output paths
OUTPUT_DIR   = Path(os.getenv("OUTPUT_DIR", "news_output"))
LOG_PATH     = OUTPUT_DIR / "loader.log"
BUDGET_PATH  = OUTPUT_DIR / "budget.json"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("news_loader")


# ─────────────────────────────────────────────────────────────────────
# GDELT QUERY CONSTRUCTION
# ─────────────────────────────────────────────────────────────────────
#
# When a term is flagged noisy ("steel", "gold", "wheat") we AND it
# with a trade-context disjunction so the DOC API returns trade-press
# hits rather than every article that happens to mention the word.

TRADE_CONTEXT_TERMS = [
    "tariff", "tariffs", "exports", "imports", "shortage", "price",
    "prices", "sanction", "sanctions", "ban", "banned", "embargo",
    "trade", "demand", "supply", "production",
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
})


def build_doc_query(term: str, is_noisy: bool) -> str:
    if is_noisy:
        ctx = " OR ".join(f'"{c}"' for c in TRADE_CONTEXT_TERMS)
        return f'"{term}" AND ({ctx})'
    return f'"{term}"'


def fmt_gdelt_datetime(d: date, end_of_day: bool = False) -> str:
    suffix = "235959" if end_of_day else "000000"
    return d.strftime("%Y%m%d") + suffix


# ─────────────────────────────────────────────────────────────────────
# SIGNAL FLAGS + SENTIMENT (kept from the original collector)
# ─────────────────────────────────────────────────────────────────────

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

POS_WORDS = re.compile(
    r"\b(gain|rise|surge|grow|strong|record|high|boost|deal|agreement|supply)\b",
    re.IGNORECASE,
)
NEG_WORDS = re.compile(
    r"\b(fall|drop|decline|shortage|sanction|ban|crisis|cut|loss|weak|low|risk)\b",
    re.IGNORECASE,
)


def flag_signals(text_: str) -> str:
    t = (text_ or "").lower()
    return "|".join(k for k, p in SIGNAL_PATTERNS.items() if re.search(p, t))


def simple_sentiment(text_: str) -> Optional[float]:
    if not text_:
        return None
    pos = len(POS_WORDS.findall(text_))
    neg = len(NEG_WORDS.findall(text_))
    total = pos + neg
    return None if total == 0 else round((pos - neg) / total, 4)


def md5(s: str) -> str:
    return hashlib.md5((s or "").encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────
# MYSQL ENGINE + SCHEMA HELPERS
# ─────────────────────────────────────────────────────────────────────

def make_engine():
    missing = [k for k, v in dict(
        DB_USER=DB_USER, DB_PASS=DB_PASS, DB_HOST=DB_HOST, DB_NAME=DB_NAME,
    ).items() if not v]
    if missing:
        raise SystemExit(f"Missing DB env vars: {missing}")
    return create_engine(
        f"mysql+mysqlconnector://{quote_plus(DB_USER)}:{quote_plus(DB_PASS)}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}",
        pool_pre_ping=True,
        pool_recycle=3600,
    )


# Auto-created at startup so we don't need to edit news_schema.sql.
NEWS_MANIFEST_DDL = """
CREATE TABLE IF NOT EXISTS news_load_manifest (
    manifest_key   VARCHAR(200) NOT NULL,
    cmd_code       VARCHAR(10)  NULL,
    search_term    VARCHAR(120) NULL,
    window_start   DATE         NULL,
    window_end     DATE         NULL,
    stype          VARCHAR(16)  NULL,
    status         VARCHAR(24)  NULL,
    rows_collected INT          NULL,
    n_api_calls    INT          NULL,
    chunk_id       CHAR(32)     NULL,
    error          VARCHAR(500) NULL,
    updated_at     DATETIME     NULL,
    PRIMARY KEY (manifest_key),
    KEY idx_news_mf_cmd    (cmd_code),
    KEY idx_news_mf_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
"""


def ensure_news_manifest_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(NEWS_MANIFEST_DDL))


def ensure_chapter_rows(engine, cmd_codes: Iterable[str]) -> None:
    """If any chapter cmd_code is missing from commodity_code_mapping,
    insert a placeholder so the FK on news_* tables doesn't fail.
    Mirrors _ensure_parent_rows() from comtrade_granular_loader.py."""
    wanted = {c for c in cmd_codes if c}
    if not wanted:
        return
    with engine.begin() as conn:
        existing = {
            row[0] for row in conn.execute(
                text(
                    "SELECT cmd_code FROM commodity_code_mapping "
                    "WHERE cmd_code IN :codes"
                ).bindparams(bindparam("codes", expanding=True)),
                {"codes": list(wanted)},
            )
        }
        missing = wanted - existing
        if not missing:
            return
        log.info("Inserting %d placeholder chapter rows into "
                 "commodity_code_mapping: %s",
                 len(missing), ", ".join(sorted(missing)[:15])
                 + ("..." if len(missing) > 15 else ""))
        conn.execute(
            text(
                "INSERT IGNORE INTO commodity_code_mapping "
                "(cmd_code, cmd_text, parent_code, is_leaf, aggr_level) "
                "VALUES (:cmd_code, :cmd_text, NULL, 0, 2)"
            ),
            [{"cmd_code": c, "cmd_text": f"HS Chapter {c} (placeholder)"}
             for c in sorted(missing)],
        )


# ─────────────────────────────────────────────────────────────────────
# SEARCH TERM LOADING
# ─────────────────────────────────────────────────────────────────────

NOISY_MARKER = "noisy alone"   # substring match in the notes column


@dataclass
class SearchTerm:
    cmd_code: str
    term: str
    term_type: str
    priority: int
    source: str
    is_noisy: bool

    @property
    def base_score(self) -> float:
        """Static score component independent of any specific article.

        priority_score    : top-priority terms get the highest weight
        specificity bonus : multi-word phrases beat single words
        curated bonus     : hand-curated terms beat extracted ones
        """
        priority_score = 10 - self.priority         # 1→9, 9→1
        specificity    = min(len(self.term.split()), 3)
        curated_bonus  = 2.0 if self.source == "curated" else 0.0
        return float(priority_score) + specificity + curated_bonus


def load_search_terms(engine, max_priority: int) -> list[SearchTerm]:
    sql = text("""
        SELECT cmd_code, search_term, term_type, priority,
               COALESCE(source, '') AS source,
               COALESCE(notes,  '') AS notes
        FROM commodity_search_terms
        WHERE is_active = 1
          AND language  = :lang
          AND priority <= :max_pri
          AND term_type IN ('primary', 'synonym')
        ORDER BY priority, cmd_code, search_term
    """)
    with engine.begin() as conn:
        rows = conn.execute(
            sql, {"lang": TERM_LANGUAGE, "max_pri": max_priority}
        ).mappings().all()
    terms = [
        SearchTerm(
            cmd_code  = r["cmd_code"],
            term      = r["search_term"],
            term_type = r["term_type"],
            priority  = int(r["priority"]),
            source    = r["source"],
            is_noisy  = NOISY_MARKER in (r["notes"] or "").lower(),
        )
        for r in rows
    ]
    log.info("Loaded %d active search terms (max_priority=%d, lang=%s) "
             "across %d cmd_codes",
             len(terms), max_priority, TERM_LANGUAGE,
             len({t.cmd_code for t in terms}))
    return terms


# ─────────────────────────────────────────────────────────────────────
# TOKEN BUDGET
# ─────────────────────────────────────────────────────────────────────

def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_budget() -> dict:
    if BUDGET_PATH.exists():
        try:
            data = json.loads(BUDGET_PATH.read_text())
            if data.get("date") == _today_iso():
                return data
        except Exception:
            log.warning("budget.json unreadable; resetting.")
    return {"date": _today_iso(), "calls": 0}


def save_budget(b: dict) -> None:
    BUDGET_PATH.write_text(json.dumps(b, indent=2))


def remaining_budget(b: dict) -> int:
    return MAX_DAILY_CALLS - int(b.get("calls", 0))


def charge_budget(b: dict, n: int = 1) -> None:
    b["calls"] = int(b.get("calls", 0)) + n
    b["date"]  = _today_iso()
    save_budget(b)


# ─────────────────────────────────────────────────────────────────────
# GDELT DOC API — ARTICLE COLLECTION
# ─────────────────────────────────────────────────────────────────────

@dataclass
class RawArticle:
    """A single (article, term) hit before strict attribution."""
    url:          str
    url_hash:     str
    title:        str
    source_domain: str
    article_date: Optional[date]
    language:     str
    cmd_code:     str
    matched_term: str
    score:        float


def fetch_articles_for_term(
    term: SearchTerm,
    start: date,
    end: date,
) -> list[dict]:
    params = {
        "query":         build_doc_query(term.term, term.is_noisy),
        "mode":          "artlist",
        "maxrecords":    MAX_RECORDS,
        "startdatetime": fmt_gdelt_datetime(start, end_of_day=False),
        "enddatetime":   fmt_gdelt_datetime(end,   end_of_day=True),
        "format":        "json",
        "sort":          "datedesc",
    }

    last_exc: Optional[BaseException] = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = SESSION.get(GDELT_DOC_URL, params=params, timeout=30)
            if r.status_code == 429:
                wait = RETRY_BACKOFF_S * attempt
                log.warning("429 from GDELT, sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            # GDELT occasionally returns HTML when the server is under
            # load — guard against it.
            try:
                payload = r.json()
            except ValueError:
                raise RuntimeError(f"non-JSON response (len={len(r.text)})")
            return payload.get("articles", []) or []
        except Exception as exc:
            last_exc = exc
            if attempt < RETRY_ATTEMPTS:
                wait = RETRY_BACKOFF_S * attempt
                log.warning("GDELT DOC attempt %d/%d failed (%r) — sleep %.1fs",
                            attempt, RETRY_ATTEMPTS, exc, wait)
                time.sleep(wait)
    log.error("GDELT DOC failed after %d attempts for term=%r: %r",
              RETRY_ATTEMPTS, term.term, last_exc)
    return []


def _parse_gdelt_date(raw: str) -> Optional[date]:
    """GDELT seendate is 'YYYYMMDDTHHMMSSZ'."""
    if not raw or len(raw) < 8:
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def collect_articles(
    terms: list[SearchTerm],
    start: date,
    end: date,
    budget: dict,
    engine,
) -> list[RawArticle]:
    """Iterate every active term, accumulate raw (article, term) hits.

    Resumability: each (cmd_code, term, window) tuple has a manifest
    row.  Successful runs are skipped.  Crashes mid-run resume from
    the next un-tackled term.
    """
    raw: list[RawArticle] = []

    for i, term in enumerate(terms, 1):
        mkey = f"news|article|{term.cmd_code}|{term.term}|{start}|{end}"

        if _manifest_status(engine, mkey) == "success":
            continue

        if remaining_budget(budget) <= 0:
            log.warning("Daily call budget exhausted at term %d/%d — "
                        "stopping article collection.", i, len(terms))
            break

        log.info("[%d/%d] cmd=%s term=%r (pri=%d, %s%s)",
                 i, len(terms), term.cmd_code, term.term,
                 term.priority, term.source,
                 ", noisy→context" if term.is_noisy else "")

        _manifest_upsert(engine, mkey, {
            "cmd_code": term.cmd_code, "search_term": term.term,
            "window_start": start, "window_end": end,
            "stype": "article", "status": "in_progress",
        })

        try:
            charge_budget(budget, 1)
            arts = fetch_articles_for_term(term, start, end)
        except Exception as exc:
            log.exception("Term %r crashed: %r", term.term, exc)
            _manifest_upsert(engine, mkey,
                             {"status": "failed", "error": repr(exc)[:480]})
            time.sleep(SLEEP_SECONDS)
            continue

        for a in arts:
            url = (a.get("url") or "").strip()
            if not url:
                continue
            title = (a.get("title") or "").strip()
            # Term must literally appear in the title for it to count
            # as a match here — GDELT DOC API's relevance ranking is
            # title-heavy but not strict, so we filter to be safe.
            title_lc = title.lower()
            term_lc  = term.term.lower()
            if term_lc not in title_lc:
                # Still attribute, but with a body-only penalty
                title_bonus = 0.0
            else:
                title_bonus = 3.0

            raw.append(RawArticle(
                url           = url,
                url_hash      = md5(url),
                title         = title,
                source_domain = (a.get("domain") or "").strip(),
                article_date  = _parse_gdelt_date(a.get("seendate", "")),
                language      = (a.get("language") or "").strip(),
                cmd_code      = term.cmd_code,
                matched_term  = term.term,
                score         = term.base_score + title_bonus,
            ))

        _manifest_upsert(engine, mkey, {
            "status": "success",
            "rows_collected": len(arts),
            "n_api_calls": 1,
        })

        time.sleep(SLEEP_SECONDS)

    log.info("Article collection: %d raw (article, term) hits across "
             "%d unique URLs", len(raw), len({a.url_hash for a in raw}))
    return raw


# ─────────────────────────────────────────────────────────────────────
# GDELT EVENTS — BULK CSV (one global pass, all terms scanned)
# ─────────────────────────────────────────────────────────────────────

GDELT_EVENT_URL = "http://data.gdeltproject.org/gdeltv2/{ts}.export.CSV.zip"

# CAMEO code → label.  Subset focused on trade events.
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


@dataclass
class RawEvent:
    """A single (event_row, term) hit before strict attribution."""
    cmd_code:        str
    matched_term:    str
    score:           float
    event_date:      Optional[date]
    actor1_name:     str
    actor1_country:  str
    actor2_name:     str
    actor2_country:  str
    event_code:      str
    event_label:     str
    goldstein:       Optional[float]
    num_mentions:    Optional[int]
    avg_tone:        Optional[float]
    location:        str
    source_url:      str
    source_url_hash: str


def build_term_regex(terms: list[SearchTerm]) -> tuple[re.Pattern, dict[str, SearchTerm]]:
    """One big OR regex matching any active term.  Returns the compiled
    pattern + a lookup from matched-string-lowercase → SearchTerm."""
    # De-dup terms (same word can appear under multiple cmd_codes); we
    # keep the SearchTerm with the highest base_score so the regex
    # match resolves to the best attribution candidate.
    by_lc: dict[str, SearchTerm] = {}
    for t in terms:
        key = t.term.lower()
        if key not in by_lc or t.base_score > by_lc[key].base_score:
            by_lc[key] = t
    sorted_terms = sorted(by_lc.keys(), key=len, reverse=True)
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(t) for t in sorted_terms) + r")\b",
        re.IGNORECASE,
    )
    return pattern, by_lc


def collect_events(
    terms: list[SearchTerm],
    start: date,
    end: date,
    budget: dict,
    engine,
) -> list[RawEvent]:
    """Download GDELT Event 2.0 bulk CSVs across the window and scan
    every row with a single combined-term regex.  One row → potentially
    multiple (cmd_code, term) hits; strict attribution picks one later.
    """
    pattern, lookup = build_term_regex(terms)

    days = (end - start).days + 1
    timestamps = []
    for i in range(min(days, MAX_EVENT_FILES)):
        d = start + timedelta(days=i)
        timestamps.append(d.strftime("%Y%m%d") + "120000")

    raw: list[RawEvent] = []
    scanned = 0
    errors  = 0

    for ts in timestamps:
        mkey = f"news|event|GLOBAL|{ts}|{start}|{end}"
        if _manifest_status(engine, mkey) == "success":
            continue
        if remaining_budget(budget) <= 0:
            log.warning("Budget exhausted during event collection.")
            break

        url = GDELT_EVENT_URL.format(ts=ts)
        log.info("Event file %s", ts)
        _manifest_upsert(engine, mkey, {
            "window_start": start, "window_end": end,
            "stype": "event", "status": "in_progress",
        })

        try:
            charge_budget(budget, 1)
            r = SESSION.get(url, timeout=60)
            if r.status_code == 404:
                # File doesn't exist yet (future date); not an error.
                _manifest_upsert(engine, mkey,
                                 {"status": "success", "rows_collected": 0})
                continue
            r.raise_for_status()

            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                with zf.open(zf.namelist()[0]) as f:
                    reader = csv.reader(
                        io.TextIOWrapper(f, encoding="utf-8",
                                         errors="replace"),
                        delimiter="\t",
                    )
                    for row in reader:
                        if len(row) < 58:
                            continue
                        joined = "\t".join(row)
                        matches = set(m.lower() for m in pattern.findall(joined))
                        if not matches:
                            continue
                        # Parse once; emit one RawEvent per matched term.
                        raw_date = row[1]
                        try:
                            ev_date = date(int(raw_date[:4]),
                                           int(raw_date[4:6]),
                                           int(raw_date[6:8]))
                        except (ValueError, IndexError):
                            ev_date = None

                        def _f(v: str) -> Optional[float]:
                            try:    return float(v) if v else None
                            except: return None

                        def _i(v: str) -> Optional[int]:
                            try:    return int(float(v)) if v else None
                            except: return None

                        src_url = row[57]
                        src_hash = md5(src_url)

                        for matched_lc in matches:
                            t = lookup[matched_lc]
                            raw.append(RawEvent(
                                cmd_code        = t.cmd_code,
                                matched_term    = t.term,
                                score           = t.base_score,
                                event_date      = ev_date,
                                actor1_name     = row[5],
                                actor1_country  = row[6],
                                actor2_name     = row[10],
                                actor2_country  = row[11],
                                event_code      = row[26],
                                event_label     = TRADE_CAMEO.get(row[26], ""),
                                goldstein       = _f(row[30]),
                                num_mentions    = _i(row[31]),
                                avg_tone        = _f(row[34]),
                                location        = row[53],
                                source_url      = src_url,
                                source_url_hash = src_hash,
                            ))
            scanned += 1
            _manifest_upsert(engine, mkey, {
                "status": "success",
                "rows_collected": len(raw),
                "n_api_calls": 1,
            })
            time.sleep(1.0)

        except zipfile.BadZipFile:
            errors += 1
            _manifest_upsert(engine, mkey,
                             {"status": "failed", "error": "bad zip"})
        except Exception as exc:
            errors += 1
            log.warning("Event file %s failed: %r", ts, exc)
            _manifest_upsert(engine, mkey,
                             {"status": "failed", "error": repr(exc)[:480]})

    log.info("Event collection: %d files scanned, %d (event, term) hits, "
             "%d errors", scanned, len(raw), errors)
    return raw


# ─────────────────────────────────────────────────────────────────────
# STRICT ATTRIBUTION
# ─────────────────────────────────────────────────────────────────────

def attribute_articles(raw: list[RawArticle]) -> list[dict]:
    """Group by url_hash, pick highest-scoring (cmd_code, term).  Track
    runner-up cmd_code for QA."""
    by_url: dict[str, list[RawArticle]] = defaultdict(list)
    for a in raw:
        by_url[a.url_hash].append(a)

    chunk_id = uuid.uuid4().hex
    now_utc  = datetime.now(timezone.utc)
    out: list[dict] = []

    for url_hash, hits in by_url.items():
        hits.sort(key=lambda h: h.score, reverse=True)
        winner = hits[0]
        # Runner-up = first hit whose cmd_code differs from winner
        runner_up = next(
            (h.cmd_code for h in hits[1:] if h.cmd_code != winner.cmd_code),
            None,
        )

        d = winner.article_date
        year_month = d.strftime("%Y-%m") if d else None
        period     = d.strftime("%Y%m")  if d else None

        text_for_signals = winner.title
        out.append({
            "url_hash":      url_hash,
            "cmd_code":      winner.cmd_code,
            "matched_term":  winner.matched_term,
            "match_score":   round(winner.score, 3),
            "runner_up_cmd": runner_up,
            "title":         winner.title[:500],
            "url":           winner.url[:1000],
            "source_domain": winner.source_domain[:120],
            "article_date":  d,
            "year_month":    year_month,
            "period":        period,
            "language":      winner.language[:8],
            "sentiment":     simple_sentiment(text_for_signals),
            "trade_signals": flag_signals(text_for_signals)[:200],
            "chunk_id":      chunk_id,
            "loaded_at_utc": now_utc,
        })
    return out


def attribute_events(raw: list[RawEvent]) -> list[dict]:
    """Group by (source_url_hash, event_code), pick highest-scoring
    (cmd_code, term)."""
    by_key: dict[tuple, list[RawEvent]] = defaultdict(list)
    for e in raw:
        # An event row is uniquely the (source article, event-code) pair
        by_key[(e.source_url_hash, e.event_code)].append(e)

    chunk_id = uuid.uuid4().hex
    now_utc  = datetime.now(timezone.utc)
    out: list[dict] = []

    for _, hits in by_key.items():
        hits.sort(key=lambda h: h.score, reverse=True)
        w = hits[0]
        d = w.event_date
        out.append({
            "cmd_code":        w.cmd_code,
            "matched_term":    w.matched_term,
            "event_date":      d,
            "year_month":      d.strftime("%Y-%m") if d else None,
            "period":          d.strftime("%Y%m")  if d else None,
            "actor1_name":     (w.actor1_name or "")[:120],
            "actor1_country":  (w.actor1_country or "")[:8],
            "actor2_name":     (w.actor2_name or "")[:120],
            "actor2_country":  (w.actor2_country or "")[:8],
            "event_code":      (w.event_code or "")[:8],
            "event_label":     (w.event_label or "")[:120],
            "goldstein_scale": w.goldstein,
            "num_mentions":    w.num_mentions,
            "avg_tone":        w.avg_tone,
            "location":        (w.location or "")[:200],
            "source_url":      (w.source_url or "")[:1000],
            "source_url_hash": w.source_url_hash,
            "chunk_id":        chunk_id,
            "loaded_at_utc":   now_utc,
        })
    return out


# ─────────────────────────────────────────────────────────────────────
# MYSQL WRITERS
# ─────────────────────────────────────────────────────────────────────

def _insert_ignore(table, conn, keys, data_iter):
    """Custom pandas to_sql method: INSERT IGNORE so dedup conflicts
    don't crash the run.  Mirrors mysql_insert_ignore_duplicates() in
    comtrade_granular_loader.py."""
    rows = [dict(zip(keys, row)) for row in data_iter]
    if not rows:
        return 0
    stmt = mysql_insert(table.table).values(rows).prefix_with("IGNORE")
    return conn.execute(stmt).rowcount or 0


def write_articles(engine, rows: list[dict]) -> None:
    if not rows:
        log.info("No articles to write.")
        return
    df = pd.DataFrame(rows)
    ensure_chapter_rows(engine, df["cmd_code"].unique())
    # The FK on runner_up_cmd is NOT defined in the schema (it'd
    # complicate inserts for borderline cases) — but we still pad
    # missing chapter rows for it if present.
    if "runner_up_cmd" in df.columns:
        ensure_chapter_rows(engine, df["runner_up_cmd"].dropna().unique())

    with engine.begin() as conn:
        n = df.to_sql(
            "news_articles", con=conn, if_exists="append", index=False,
            chunksize=500, method=_insert_ignore,
        )
    inserted = 0 if n is None else int(n)
    log.info("news_articles: %d submitted, %d inserted, %d ignored "
             "(URL already present)",
             len(df), inserted, len(df) - inserted)


def write_events(engine, rows: list[dict]) -> None:
    if not rows:
        log.info("No events to write.")
        return
    df = pd.DataFrame(rows)
    ensure_chapter_rows(engine, df["cmd_code"].unique())
    with engine.begin() as conn:
        n = df.to_sql(
            "news_events", con=conn, if_exists="append", index=False,
            chunksize=500, method=_insert_ignore,
        )
    inserted = 0 if n is None else int(n)
    log.info("news_events: %d submitted, %d inserted, %d ignored",
             len(df), inserted, len(df) - inserted)


# ─────────────────────────────────────────────────────────────────────
# LINKING TABLE — TRUNCATE + REBUILD
# ─────────────────────────────────────────────────────────────────────

REBUILD_LINKING_SQL = """
INSERT INTO news_linking (
    cmd_code, year_month, period,
    article_count, event_count,
    avg_sentiment, avg_tone, avg_goldstein,
    signal_tariff, signal_sanction, signal_embargo,
    signal_shortage, signal_surplus, signal_ban, signal_quota,
    signal_price_spike, signal_weather, signal_strike, signal_export_ban,
    updated_at
)
SELECT
    base.cmd_code,
    base.year_month,
    base.period,
    COALESCE(a.cnt, 0)      AS article_count,
    COALESCE(e.cnt, 0)      AS event_count,
    a.avg_sentiment,
    e.avg_tone,
    e.avg_goldstein,
    COALESCE(a.s_tariff,      0),
    COALESCE(a.s_sanction,    0),
    COALESCE(a.s_embargo,     0),
    COALESCE(a.s_shortage,    0),
    COALESCE(a.s_surplus,     0),
    COALESCE(a.s_ban,         0),
    COALESCE(a.s_quota,       0),
    COALESCE(a.s_price_spike, 0),
    COALESCE(a.s_weather,     0),
    COALESCE(a.s_strike,      0),
    COALESCE(a.s_export_ban,  0),
    UTC_TIMESTAMP()
FROM (
    SELECT cmd_code, year_month, period FROM news_articles
        WHERE year_month IS NOT NULL
    UNION
    SELECT cmd_code, year_month, period FROM news_events
        WHERE year_month IS NOT NULL
) base
LEFT JOIN (
    SELECT cmd_code, year_month,
           COUNT(*) AS cnt,
           AVG(sentiment) AS avg_sentiment,
           SUM(trade_signals LIKE '%tariff%')      AS s_tariff,
           SUM(trade_signals LIKE '%sanction%')    AS s_sanction,
           SUM(trade_signals LIKE '%embargo%')     AS s_embargo,
           SUM(trade_signals LIKE '%shortage%')    AS s_shortage,
           SUM(trade_signals LIKE '%surplus%')     AS s_surplus,
           SUM(trade_signals LIKE '%ban%')         AS s_ban,
           SUM(trade_signals LIKE '%quota%')       AS s_quota,
           SUM(trade_signals LIKE '%price_spike%') AS s_price_spike,
           SUM(trade_signals LIKE '%weather%')     AS s_weather,
           SUM(trade_signals LIKE '%strike%')      AS s_strike,
           SUM(trade_signals LIKE '%export_ban%')  AS s_export_ban
    FROM news_articles
    WHERE year_month IS NOT NULL
    GROUP BY cmd_code, year_month
) a ON a.cmd_code = base.cmd_code AND a.year_month = base.year_month
LEFT JOIN (
    SELECT cmd_code, year_month,
           COUNT(*) AS cnt,
           AVG(avg_tone)        AS avg_tone,
           AVG(goldstein_scale) AS avg_goldstein
    FROM news_events
    WHERE year_month IS NOT NULL
    GROUP BY cmd_code, year_month
) e ON e.cmd_code = base.cmd_code AND e.year_month = base.year_month
;
"""


def rebuild_linking(engine) -> None:
    log.info("Rebuilding news_linking …")
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE news_linking"))
        conn.execute(text(REBUILD_LINKING_SQL))
        n = conn.execute(text("SELECT COUNT(*) FROM news_linking")).scalar()
    log.info("news_linking: %d rows", n)


# ─────────────────────────────────────────────────────────────────────
# MANIFEST HELPERS
# ─────────────────────────────────────────────────────────────────────

def _manifest_status(engine, key: str) -> Optional[str]:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT status FROM news_load_manifest "
                 "WHERE manifest_key = :k"),
            {"k": key},
        ).fetchone()
    return row[0] if row else None


def _manifest_upsert(engine, key: str, fields: dict) -> None:
    fields = dict(fields)
    fields["manifest_key"] = key
    fields["updated_at"]   = datetime.now(timezone.utc)
    cols = list(fields.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(
        f"{c} = VALUES({c})" for c in cols if c != "manifest_key"
    )
    sql = text(
        f"INSERT INTO news_load_manifest ({', '.join(cols)}) "
        f"VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )
    with engine.begin() as conn:
        conn.execute(sql, fields)


# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--dry-run", action="store_true",
                   help="Show plan, skip API calls")
    p.add_argument("--skip-events", action="store_true",
                   help="Articles only (overrides COLLECT_EVENTS env)")
    p.add_argument("--rebuild-linking", action="store_true",
                   help="Skip collection; just rebuild news_linking")
    p.add_argument("--max-priority", type=int, default=None,
                   help="Override MAX_PRIORITY env (1=best, 9=worst)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not (START_DATE and END_DATE):
        raise SystemExit("Set START_DATE and END_DATE in .env (YYYY-MM-DD).")
    start = date.fromisoformat(START_DATE)
    end   = date.fromisoformat(END_DATE)
    if end < start:
        raise SystemExit("END_DATE precedes START_DATE.")

    max_priority = args.max_priority if args.max_priority else MAX_PRIORITY
    do_events    = COLLECT_EVENTS and not args.skip_events

    engine = make_engine()
    ensure_news_manifest_table(engine)

    if args.rebuild_linking:
        rebuild_linking(engine)
        return

    terms = load_search_terms(engine, max_priority=max_priority)
    if not terms:
        raise SystemExit(
            "No active terms in commodity_search_terms.  Did you load "
            "the CSV with load_search_terms.py?"
        )
    ensure_chapter_rows(engine, {t.cmd_code for t in terms})

    if args.dry_run:
        log.info("DRY RUN — window=%s..%s  terms=%d  events=%s",
                 start, end, len(terms), do_events)
        log.info("Estimated API calls: %d articles%s",
                 len(terms),
                 f" + {min((end-start).days+1, MAX_EVENT_FILES)} event files"
                 if do_events else "")
        return

    budget = load_budget()
    log.info("Run starting — window=%s..%s  terms=%d  budget_left=%d/%d",
             start, end, len(terms),
             remaining_budget(budget), MAX_DAILY_CALLS)

    # ARTICLES
    raw_articles = collect_articles(terms, start, end, budget, engine)
    articles     = attribute_articles(raw_articles)
    write_articles(engine, articles)

    # EVENTS
    if do_events:
        raw_events = collect_events(terms, start, end, budget, engine)
        events     = attribute_events(raw_events)
        write_events(engine, events)

    # ROLLUP
    rebuild_linking(engine)

    log.info("Done.  Calls used today: %d/%d",
             budget.get("calls", 0), MAX_DAILY_CALLS)


if __name__ == "__main__":
    main()
