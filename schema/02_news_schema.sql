-- =====================================================================
-- News tables: GDELT articles and events linked to HS commodity codes
-- =====================================================================
--
-- Designed to plug into the existing comtrade schema.  All four tables
-- foreign-key into commodity_code_mapping.cmd_code, so a join from
-- fact_trade_granular to news data is a straight equi-join on cmd_code
-- (+ period for time-aligned lookups).
--
-- Tables:
--   1. commodity_search_terms  -- one row per (cmd_code, term, language)
--   2. news_articles           -- GDELT DOC API hits (deduped on URL)
--   3. news_events             -- GDELT Event 2.0 matches
--   4. news_linking            -- pre-aggregated cmd_code x year_month
--                                 rollup; the table you'll actually join
--                                 to fact_trade_granular
--
-- BEFORE LOADING SEARCH TERMS:
--   Chapter-level cmd_codes ('01' through '99') must exist in
--   commodity_code_mapping.  If your loader populated it from Comtrade's
--   Commodity_Code_Mapping.csv with aggrLevel filtering, the chapters
--   should already be present.  If not, the news loader's
--   _ensure_parent_rows() (mirrored from the comtrade loader) will
--   insert placeholders.
-- =====================================================================
USE comtrade;
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;


-- =====================================================================
-- 1. COMMODITY SEARCH TERMS
-- Many terms per commodity.  term_type lets you distinguish the main
-- name from synonyms or context filters; priority lets the loader
-- spend its token budget on the strongest terms first.
-- =====================================================================
CREATE TABLE IF NOT EXISTS commodity_search_terms (
    term_id      INT UNSIGNED NOT NULL AUTO_INCREMENT,
    cmd_code     VARCHAR(10)  NOT NULL,
    search_term  VARCHAR(120) NOT NULL,
    term_type    ENUM('primary','synonym','context','exclude')
                 NOT NULL DEFAULT 'primary',
    language     CHAR(2)      NOT NULL DEFAULT 'en',
    priority     TINYINT      NOT NULL DEFAULT 5
                 COMMENT '1=highest, 9=lowest; loader filters by this',
    is_active    TINYINT(1)   NOT NULL DEFAULT 1,
    source       VARCHAR(20)  NULL
                 COMMENT 'curated | extracted | manual',
    notes        VARCHAR(200) NULL,

    PRIMARY KEY (term_id),
    UNIQUE KEY uq_term (cmd_code, search_term, language),
    KEY idx_term_active   (is_active, priority),
    KEY idx_term_cmd      (cmd_code, is_active),

    CONSTRAINT fk_term_cmd
        FOREIGN KEY (cmd_code)
        REFERENCES commodity_code_mapping (cmd_code)
        ON DELETE CASCADE
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- =====================================================================
-- 2. NEWS ARTICLES (GDELT DOC API)
-- One row per unique article (deduped on URL hash).  Under strict
-- attribution, an article appears exactly once with the cmd_code that
-- scored the strongest match.
-- =====================================================================
CREATE TABLE IF NOT EXISTS news_articles (
    article_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    url_hash       CHAR(32) NOT NULL COMMENT 'MD5(url) for dedup',
    cmd_code       VARCHAR(10) NOT NULL
                   COMMENT 'Dominant commodity under strict attribution',
    matched_term   VARCHAR(120) NULL
                   COMMENT 'Which term produced the winning score',
    match_score    DECIMAL(8,3) NULL
                   COMMENT 'Higher = stronger match; used for tie-break',
    runner_up_cmd  VARCHAR(10) NULL
                   COMMENT 'Second-best commodity, for audit/QA',

    title          VARCHAR(500) NULL,
    url            VARCHAR(1000) NOT NULL,
    source_domain  VARCHAR(120) NULL,
    article_date   DATE NULL,
    year_month_date     CHAR(7) NULL COMMENT 'YYYY-MM, join key',
    period         VARCHAR(8) NULL COMMENT 'YYYYMM, fact-table join key',
    language       VARCHAR(8) NULL,

    sentiment      DECIMAL(6,4) NULL,
    trade_signals  VARCHAR(200) NULL
                   COMMENT 'pipe-separated: tariff|shortage|...',

    chunk_id       CHAR(32) NULL,
    loaded_at_utc  DATETIME NULL,

    PRIMARY KEY (article_id),
    UNIQUE KEY uq_article_url (url_hash),

    KEY idx_article_cmd_period (cmd_code, period),
    KEY idx_article_date       (article_date),
    KEY idx_article_chunk      (chunk_id),

    CONSTRAINT fk_article_cmd
        FOREIGN KEY (cmd_code)
        REFERENCES commodity_code_mapping (cmd_code)
        ON DELETE RESTRICT
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- =====================================================================
-- 3. NEWS EVENTS (GDELT Event 2.0)
-- =====================================================================
CREATE TABLE IF NOT EXISTS news_events (
    event_id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    cmd_code         VARCHAR(10) NOT NULL,
    matched_term     VARCHAR(120) NULL,

    event_date       DATE NULL,
    year_month_date       CHAR(7) NULL,
    period           VARCHAR(8) NULL,

    actor1_name      VARCHAR(120) NULL,
    actor1_country   VARCHAR(8)   NULL,
    actor2_name      VARCHAR(120) NULL,
    actor2_country   VARCHAR(8)   NULL,

    event_code       VARCHAR(8)   NULL COMMENT 'CAMEO code',
    event_label      VARCHAR(120) NULL,

    goldstein_scale  DECIMAL(6,3)  NULL,
    num_mentions     INT           NULL,
    avg_tone         DECIMAL(8,4)  NULL,

    location         VARCHAR(200)  NULL,
    source_url       VARCHAR(1000) NULL,
    source_url_hash  CHAR(32)      NULL,

    chunk_id         CHAR(32) NULL,
    loaded_at_utc    DATETIME NULL,

    PRIMARY KEY (event_id),
    UNIQUE KEY uq_event (cmd_code, source_url_hash, event_code),

    KEY idx_event_cmd_period (cmd_code, period),
    KEY idx_event_date       (event_date),

    CONSTRAINT fk_event_cmd
        FOREIGN KEY (cmd_code)
        REFERENCES commodity_code_mapping (cmd_code)
        ON DELETE RESTRICT
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- =====================================================================
-- 4. NEWS LINKING TABLE -- the join target for fact_trade_granular
--
-- The loader rebuilds this from news_articles + news_events after each
-- collection run (truncate + insert).  Treat it as a materialized view.
--
-- Join example:
--   SELECT f.*, n.article_count, n.avg_sentiment, n.signal_tariff
--   FROM fact_trade_granular f
--   LEFT JOIN news_linking n
--     ON n.cmd_code = f.cmd_code
--    AND n.period   = f.period;
-- =====================================================================
CREATE TABLE IF NOT EXISTS news_linking (
    cmd_code        VARCHAR(10) NOT NULL,
    year_month_date      CHAR(7)     NOT NULL,
    period          VARCHAR(8)  NOT NULL
                    COMMENT 'YYYYMM, matches fact_trade_granular.period',

    article_count   INT NOT NULL DEFAULT 0,
    event_count     INT NOT NULL DEFAULT 0,
    avg_sentiment   DECIMAL(6,4) NULL,
    avg_tone        DECIMAL(8,4) NULL,
    avg_goldstein   DECIMAL(6,3) NULL,

    signal_tariff       INT NOT NULL DEFAULT 0,
    signal_sanction     INT NOT NULL DEFAULT 0,
    signal_embargo      INT NOT NULL DEFAULT 0,
    signal_shortage     INT NOT NULL DEFAULT 0,
    signal_surplus      INT NOT NULL DEFAULT 0,
    signal_ban          INT NOT NULL DEFAULT 0,
    signal_quota        INT NOT NULL DEFAULT 0,
    signal_price_spike  INT NOT NULL DEFAULT 0,
    signal_weather      INT NOT NULL DEFAULT 0,
    signal_strike       INT NOT NULL DEFAULT 0,
    signal_export_ban   INT NOT NULL DEFAULT 0,

    updated_at      DATETIME NOT NULL,

    PRIMARY KEY (cmd_code, year_month_date),
    KEY idx_link_period (cmd_code, period),

    CONSTRAINT fk_link_cmd
        FOREIGN KEY (cmd_code)
        REFERENCES commodity_code_mapping (cmd_code)
        ON DELETE CASCADE
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


SET FOREIGN_KEY_CHECKS = 1;
