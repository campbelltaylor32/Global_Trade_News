use comtrade;

-- =====================================================================
-- UN Comtrade -- MySQL schema (relational, with foreign keys)
-- =====================================================================
--
-- Relationships:
--
--   fact_trade_granular.freq_code         -> frequency_mapping.freq_code
--   fact_trade_granular.flow_code         -> tradeflow_mapping.flow_code
--   fact_trade_granular.reporter_code     -> country_mapping.country_code
--   fact_trade_granular.partner_code      -> country_mapping.country_code
--   fact_trade_granular.partner2_code     -> country_mapping.country_code
--   fact_trade_granular.cmd_code          -> commodity_code_mapping.cmd_code
--   fact_trade_granular.mot_code          -> transport_mapping.mot_code
--   fact_trade_granular.qty_unit_code     -> unit_quantity_mapping.qty_code
--   fact_trade_granular.alt_qty_unit_code -> unit_quantity_mapping.qty_code
--
--   commodity_code_mapping.parent_code -> commodity_code_mapping.cmd_code
--                                         (self-referential: HS hierarchy)
--
-- Important: FKs require the reference tables to be POPULATED BEFORE
-- the fact table is loaded.  Run load_reference_tables.py first.
--
-- Charset + collation are forced identical on every FK column pair
-- (utf8mb4 / utf8mb4_0900_ai_ci) -- different collations on the two
-- sides of an FK cause MySQL error 150 "constraint incorrectly formed".
--
-- ON DELETE RESTRICT: deletes of referenced lookup rows are blocked
-- so you cannot accidentally orphan a billion fact rows.
-- ON UPDATE CASCADE: if a code's spelling/casing is corrected, the
-- change propagates automatically.
--
-- HANDLING UNKNOWN CODES: the Comtrade API occasionally emits codes
-- that are not in the latest published mapping snapshot (e.g.,
-- partner '899' "Areas, not elsewhere specified", new HS revisions
-- before the mapping CSV is updated).  The loader is responsible for
-- inserting placeholder rows into the parent table when it encounters
-- one of these -- see ensure_codes_exist() in
-- comtrade_granular_loader.py.  This keeps inserts from failing FK
-- checks while preserving the relational integrity.
-- =====================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- If you want a dedicated schema, uncomment:
-- CREATE DATABASE IF NOT EXISTS comtrade_db
--     CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
-- USE comtrade_db;


-- =====================================================================
-- 1. FREQUENCY MAPPING
-- =====================================================================
CREATE TABLE IF NOT EXISTS frequency_mapping (
    freq_code   VARCHAR(2)  NOT NULL,
    freq_desc   VARCHAR(20) NOT NULL,
    PRIMARY KEY (freq_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- =====================================================================
-- 2. TRADEFLOW MAPPING
-- =====================================================================
CREATE TABLE IF NOT EXISTS tradeflow_mapping (
    flow_code   VARCHAR(8)  NOT NULL,
    flow_desc   VARCHAR(64) NOT NULL,
    PRIMARY KEY (flow_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- =====================================================================
-- 3. TRANSPORT (MOT) MAPPING
-- =====================================================================
CREATE TABLE IF NOT EXISTS transport_mapping (
    mot_code    VARCHAR(8)  NOT NULL,
    mot_desc    VARCHAR(80) NOT NULL,
    PRIMARY KEY (mot_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- =====================================================================
-- 4. CONSUMPTION (services modes) MAPPING
-- =====================================================================
CREATE TABLE IF NOT EXISTS consumption_mapping (
    consumption_code  VARCHAR(10) NOT NULL,
    consumption_desc  VARCHAR(64) NOT NULL,
    PRIMARY KEY (consumption_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- =====================================================================
-- 5. UNIT-QUANTITY MAPPING
-- qty_code is VARCHAR so it can FK to fact_trade_granular's
-- qty_unit_code / alt_qty_unit_code (which the Python loader keeps
-- as strings).
-- =====================================================================
CREATE TABLE IF NOT EXISTS unit_quantity_mapping (
    qty_code        VARCHAR(8)   NOT NULL,
    qty_abbr        VARCHAR(20)  NOT NULL,
    qty_description VARCHAR(120) NOT NULL,
    PRIMARY KEY (qty_code),
    KEY idx_unit_abbr (qty_abbr)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- =====================================================================
-- 6. COUNTRY (REPORTER / PARTNER) MAPPING
-- Parent table for THREE FKs from fact_trade_granular:
-- reporter_code, partner_code, partner2_code.
-- =====================================================================
CREATE TABLE IF NOT EXISTS country_mapping (
    country_code         VARCHAR(8)   NOT NULL COMMENT 'M49 numeric code as string',
    country_text         VARCHAR(80)  NOT NULL COMMENT 'Display name',
    reporter_note        VARCHAR(120) NULL,
    iso_alpha_2          CHAR(2)      NULL,
    iso_alpha_3          CHAR(3)      NULL,
    entry_effective_date DATETIME     NULL,
    entry_expired_date   DATETIME     NULL,
    is_group             TINYINT(1)   NOT NULL DEFAULT 0,
    PRIMARY KEY (country_code),
    KEY idx_country_iso3 (iso_alpha_3),
    KEY idx_country_iso2 (iso_alpha_2)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- =====================================================================
-- 7. COMMODITY CODE (HS) MAPPING -- self-referential hierarchy
-- =====================================================================
CREATE TABLE IF NOT EXISTS commodity_code_mapping (
    cmd_code           VARCHAR(10)  NOT NULL,
    cmd_text           VARCHAR(300) NOT NULL,
    parent_code        VARCHAR(10)  NULL,
    is_leaf            TINYINT(1)   NOT NULL DEFAULT 0,
    aggr_level         TINYINT      NOT NULL DEFAULT 0,
    standard_unit_abbr VARCHAR(20)  NULL,
    PRIMARY KEY (cmd_code),
    KEY idx_cmd_parent (parent_code),
    KEY idx_cmd_aggr (aggr_level),
    KEY idx_cmd_isleaf (is_leaf),
    CONSTRAINT fk_commodity_parent
        FOREIGN KEY (parent_code)
        REFERENCES commodity_code_mapping (cmd_code)
        ON DELETE RESTRICT
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- =====================================================================
-- 8. FACT TABLE -- raw / cleaned API responses, with full FKs
-- =====================================================================
CREATE TABLE IF NOT EXISTS fact_trade_granular (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    -- Provenance
    chunk_id        CHAR(32)      NULL,
    loaded_at_utc   DATETIME      NULL,

    -- Dataset
    dataset_code    VARCHAR(20)   NULL,
    type_code       VARCHAR(2)    NULL COMMENT 'C=goods, S=services',
    freq_code       VARCHAR(2)    NULL,

    -- Period
    ref_period_id   INT           NULL,
    ref_year        SMALLINT      NULL,
    ref_month       TINYINT       NULL,
    period          VARCHAR(8)    NOT NULL,

    -- Reporter
    reporter_code   VARCHAR(8)    NOT NULL,
    reporter_iso    CHAR(3)       NULL,
    reporter_desc   VARCHAR(80)   NULL,

    -- Flow
    flow_code       VARCHAR(8)    NOT NULL,
    flow_desc       VARCHAR(64)   NULL,

    -- Partner
    partner_code    VARCHAR(8)    NOT NULL,
    partner_iso     CHAR(3)       NULL,
    partner_desc    VARCHAR(80)   NULL,
    partner2_code   VARCHAR(8)    NOT NULL DEFAULT '0',
    partner2_iso    CHAR(3)       NULL,
    partner2_desc   VARCHAR(80)   NULL,

    -- Classification
    classification_code        VARCHAR(8)  NULL,
    classification_search_code VARCHAR(8)  NULL,
    is_original_classification TINYINT(1)  NULL,

    -- Commodity
    cmd_code        VARCHAR(10)   NOT NULL,
    cmd_desc        VARCHAR(300)  NULL,
    aggr_level      TINYINT       NULL,
    is_leaf         TINYINT(1)    NULL,

    -- Customs / mode of transport
    customs_code    VARCHAR(8)    NOT NULL DEFAULT 'C00',
    customs_desc    VARCHAR(120)  NULL,
    mot_code        VARCHAR(8)    NOT NULL DEFAULT '0',
    mot_desc        VARCHAR(80)   NULL,

    -- Quantities
    qty_unit_code        VARCHAR(8)     NULL,
    qty_unit_abbr        VARCHAR(20)    NULL,
    qty                  DECIMAL(28, 4) NULL,
    is_qty_estimated     TINYINT(1)     NULL,

    alt_qty_unit_code    VARCHAR(8)     NULL,
    alt_qty_unit_abbr    VARCHAR(20)    NULL,
    alt_qty              DECIMAL(28, 4) NULL,
    is_alt_qty_estimated TINYINT(1)     NULL,

    net_weight                DECIMAL(28, 4) NULL,
    is_net_weight_estimated   TINYINT(1)     NULL,
    gross_weight              DECIMAL(28, 4) NULL,
    is_gross_weight_estimated TINYINT(1)     NULL,

    -- Values (USD)
    cif_value_usd     DECIMAL(28, 4) NULL,
    fob_value_usd     DECIMAL(28, 4) NULL,
    primary_value_usd DECIMAL(28, 4) NULL,

    -- Flags
    legacy_estimation_flag INT        NULL,
    is_reported            TINYINT(1) NULL,
    is_aggregate           TINYINT(1) NULL,

    PRIMARY KEY (id),

    UNIQUE KEY uq_trade_row (
        period,
        reporter_code,
        flow_code,
        partner_code,
        partner2_code,
        cmd_code,
        customs_code,
        mot_code
    ),

    -- ---- Indexes for FK enforcement and analytical joins ----
    KEY idx_fact_period          (period),
    KEY idx_fact_reporter_period (reporter_code, period),
    KEY idx_fact_partner         (partner_code),
    KEY idx_fact_partner2        (partner2_code),
    KEY idx_fact_cmd             (cmd_code),
    KEY idx_fact_flow            (flow_code),
    KEY idx_fact_freq            (freq_code),
    KEY idx_fact_mot             (mot_code),
    KEY idx_fact_qty_unit        (qty_unit_code),
    KEY idx_fact_alt_qty_unit    (alt_qty_unit_code),
    KEY idx_fact_chunk           (chunk_id),
    KEY idx_fact_reporter_iso    (reporter_iso),
    KEY idx_fact_partner_iso     (partner_iso),

    -- ---- Foreign keys ----
    CONSTRAINT fk_fact_freq
        FOREIGN KEY (freq_code)
        REFERENCES frequency_mapping (freq_code)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    CONSTRAINT fk_fact_flow
        FOREIGN KEY (flow_code)
        REFERENCES tradeflow_mapping (flow_code)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    CONSTRAINT fk_fact_reporter
        FOREIGN KEY (reporter_code)
        REFERENCES country_mapping (country_code)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    CONSTRAINT fk_fact_partner
        FOREIGN KEY (partner_code)
        REFERENCES country_mapping (country_code)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    CONSTRAINT fk_fact_partner2
        FOREIGN KEY (partner2_code)
        REFERENCES country_mapping (country_code)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    CONSTRAINT fk_fact_cmd
        FOREIGN KEY (cmd_code)
        REFERENCES commodity_code_mapping (cmd_code)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    CONSTRAINT fk_fact_mot
        FOREIGN KEY (mot_code)
        REFERENCES transport_mapping (mot_code)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    CONSTRAINT fk_fact_qty_unit
        FOREIGN KEY (qty_unit_code)
        REFERENCES unit_quantity_mapping (qty_code)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    CONSTRAINT fk_fact_alt_qty_unit
        FOREIGN KEY (alt_qty_unit_code)
        REFERENCES unit_quantity_mapping (qty_code)
        ON DELETE RESTRICT ON UPDATE CASCADE

) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  ROW_FORMAT=DYNAMIC;


-- =====================================================================
-- 9. LOAD MANIFEST (optional SQL mirror of loader's manifest.csv)
-- =====================================================================
CREATE TABLE IF NOT EXISTS load_manifest (
    manifest_key   VARCHAR(120) NOT NULL,
    classification VARCHAR(8)   NULL,
    frequency      VARCHAR(2)   NULL,
    cmd_code       VARCHAR(10)  NULL,
    period         VARCHAR(8)   NULL,
    reporter_code  VARCHAR(8)   NULL,
    flow_code      VARCHAR(8)   NULL,
    partner_code   VARCHAR(8)   NULL,
    status         VARCHAR(24)  NULL,
    rows_loaded    INT          NULL,
    n_api_calls    INT          NULL,
    chunk_id       CHAR(32)     NULL,
    error          VARCHAR(500) NULL,
    updated_at     DATETIME     NULL,
    PRIMARY KEY (manifest_key),
    KEY idx_manifest_status (status),
    KEY idx_manifest_chunk  (chunk_id),
    KEY idx_manifest_period (period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


SET FOREIGN_KEY_CHECKS = 1;


-- =====================================================================
-- BOOTSTRAP ROWS
--
-- Values the API emits but the mapping CSVs don't include.
-- =====================================================================

-- 'World' partner aggregate (partner_code='0').  Heavy use in fact
-- rows; not present in Country_Mapping_Data.csv.
INSERT IGNORE INTO country_mapping
    (country_code, country_text, iso_alpha_2, iso_alpha_3, is_group)
VALUES
    ('0', 'World', NULL, 'W00', 1);

-- N/A quantity unit (qty_code='-1').  Already in
-- Unit_Quantity_Mapping.csv as code -1.  No bootstrap needed.

-- TOTAL modes of transport (mot_code='0') IS in Transport_Mapping.csv.
-- No bootstrap needed.

-- Default customs procedure code 'C00'.  The fact table uses 'C00'
-- as DEFAULT for customs_code, but customs has no mapping CSV in this
-- project, so customs_code is intentionally NOT foreign-keyed.

Show tables;