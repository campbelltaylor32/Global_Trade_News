-- =========================================================================
--  Global Trade Risk Ledger — Warehouse analytics queries (MySQL 8+ dialect)
--  ADSP 31011 Final Project · UN Comtrade OLAP star schema
--
--  Schema referenced:
--    fact_trade_granular_v2   (fact, grain = reporter × partner × commodity
--                              × year × flow × qty_unit)
--    country_geo              (dim, country lat/lon + ISO attributes)
--    unit_quantity_mapping    (dim, qty_unit_code → qty_abbr, description)
--
--  Each query is annotated with the business question it answers and the
--  technique it demonstrates (CTE, window fn, multi-dim join, etc.).
-- =========================================================================


-- -------------------------------------------------------------------------
-- Q1.  Top-15 trade corridors of the most recent year, with prior-year value
--      and YoY growth.
--
--      Technique: multi-dim JOIN, CTE, window LAG over PARTITION BY corridor.
-- -------------------------------------------------------------------------
WITH corridor_year AS (
    SELECT
        v.ref_year,
        v.reporter_iso,
        v.reporter_desc,
        v.partner_iso,
        v.partner_desc,
        v.flow_code,
        SUM(v.primary_value_usd) AS value_usd
    FROM fact_trade_granular_v2 v
    JOIN country_geo g
        ON g.iso_alpha_3 = v.partner_iso
    WHERE v.partner_iso NOT IN ('W00', 'WLD')
      AND v.primary_value_usd > 0
      AND v.cmd_code = 'TOTAL'
    GROUP BY 1, 2, 3, 4, 5, 6
),
with_lag AS (
    SELECT
        cy.*,
        LAG(value_usd) OVER (
            PARTITION BY reporter_iso, partner_iso, flow_code
            ORDER BY ref_year
        ) AS value_prev,
        ROW_NUMBER() OVER (
            PARTITION BY ref_year, flow_code
            ORDER BY value_usd DESC
        ) AS rk
    FROM corridor_year cy
)
SELECT
    ref_year,
    reporter_desc            AS reporter,
    partner_desc             AS partner,
    flow_code,
    value_usd,
    value_prev,
    (value_usd - value_prev) / NULLIF(value_prev, 0) AS yoy_growth
FROM with_lag
WHERE ref_year = (SELECT MAX(ref_year) FROM fact_trade_granular_v2)
  AND rk <= 15
ORDER BY flow_code, value_usd DESC;


-- -------------------------------------------------------------------------
-- Q2.  Country-level partner concentration (HHI) and Top-3 dependency,
--      per year, with classification flag.
--
--      Technique: nested aggregation, CASE classification, square in aggregate.
-- -------------------------------------------------------------------------
WITH partner_share AS (
    SELECT
        v.ref_year,
        v.reporter_iso,
        v.reporter_desc,
        v.partner_iso,
        v.flow_code,
        SUM(v.primary_value_usd) AS partner_value,
        SUM(SUM(v.primary_value_usd)) OVER (
            PARTITION BY v.reporter_iso, v.flow_code, v.ref_year
        ) AS reporter_total
    FROM fact_trade_granular_v2 v
    WHERE v.partner_iso NOT IN ('W00', 'WLD')
      AND v.primary_value_usd > 0
      AND v.cmd_code = 'TOTAL'
    GROUP BY 1, 2, 3, 4, 5
),
with_share AS (
    SELECT
        ref_year, reporter_iso, reporter_desc, flow_code, partner_iso,
        partner_value,
        partner_value / NULLIF(reporter_total, 0) AS share
    FROM partner_share
),
ranked AS (
    SELECT
        ws.*,
        ROW_NUMBER() OVER (
            PARTITION BY reporter_iso, ref_year, flow_code
            ORDER BY share DESC
        ) AS pr
    FROM with_share ws
)
SELECT
    ref_year,
    reporter_desc,
    flow_code,
    SUM(POWER(share, 2)) * 10000 AS hhi,
    SUM(CASE WHEN pr <= 3 THEN share ELSE 0 END) AS top3_share,
    COUNT(*) AS n_partners,
    CASE
        WHEN SUM(POWER(share, 2)) * 10000 > 2500 THEN 'Highly concentrated'
        WHEN SUM(POWER(share, 2)) * 10000 > 1500 THEN 'Moderate'
        ELSE 'Diversified'
    END AS concentration_class
FROM ranked
GROUP BY 1, 2, 3
ORDER BY ref_year DESC, hhi DESC;


-- -------------------------------------------------------------------------
-- Q3.  3-year CAGR of total exports per country, ranked.
--
--      Technique: self-join via filtered CTEs, POWER for CAGR formula.
-- -------------------------------------------------------------------------
WITH cy AS (
    SELECT
        v.ref_year,
        v.reporter_iso,
        v.reporter_desc,
        SUM(v.primary_value_usd) AS value_usd
    FROM fact_trade_granular_v2 v
    WHERE v.flow_code = 'X'
      AND v.partner_iso NOT IN ('W00', 'WLD')
      AND v.cmd_code = 'TOTAL'
      AND v.primary_value_usd > 0
    GROUP BY 1, 2, 3
),
yr_bounds AS (
    SELECT MAX(ref_year) AS y_end, MAX(ref_year) - 3 AS y_start FROM cy
)
SELECT
    a.reporter_desc,
    a.value_usd                          AS exports_now,
    b.value_usd                          AS exports_then,
    POWER(a.value_usd / NULLIF(b.value_usd, 0), 1.0/3) - 1 AS cagr_3y,
    a.value_usd - b.value_usd            AS abs_change
FROM cy a
JOIN yr_bounds yb ON a.ref_year = yb.y_end
JOIN cy b        ON b.reporter_iso = a.reporter_iso AND b.ref_year = yb.y_start
ORDER BY cagr_3y IS NULL, cagr_3y DESC
LIMIT 25;


-- -------------------------------------------------------------------------
-- Q4.  Commodity market share: top-5 exporters per HS chapter, latest year,
--      with their share of global exports.
--
--      Technique: window aggregate (SUM OVER) + DENSE_RANK.
-- -------------------------------------------------------------------------
WITH cmd_exp AS (
    SELECT
        v.ref_year,
        v.cmd_code,
        v.cmd_desc,
        v.reporter_iso,
        v.reporter_desc,
        SUM(v.primary_value_usd) AS value_usd
    FROM fact_trade_granular_v2 v
    WHERE v.flow_code = 'X'
      AND v.partner_iso NOT IN ('W00', 'WLD')
      AND v.cmd_code <> 'TOTAL'
      AND v.primary_value_usd > 0
    GROUP BY 1, 2, 3, 4, 5
),
with_share AS (
    SELECT
        ce.*,
        value_usd
            / SUM(value_usd) OVER (PARTITION BY ref_year, cmd_code)
            AS market_share,
        DENSE_RANK() OVER (
            PARTITION BY ref_year, cmd_code
            ORDER BY value_usd DESC
        ) AS rk
    FROM cmd_exp ce
)
SELECT
    cmd_code,
    cmd_desc,
    reporter_desc                AS exporter,
    rk,
    value_usd,
    market_share
FROM with_share
WHERE ref_year = (SELECT MAX(ref_year) FROM fact_trade_granular_v2)
  AND rk <= 5
ORDER BY cmd_code, rk;


-- -------------------------------------------------------------------------
-- Q5.  Bilateral trade intensity for the latest year: corridors that matter
--      disproportionately to BOTH sides.
--
--      Technique: two window totals (reporter + partner), product of shares.
-- -------------------------------------------------------------------------
WITH cor AS (
    SELECT
        v.ref_year,
        v.reporter_iso,
        v.reporter_desc,
        v.partner_iso,
        v.partner_desc,
        SUM(v.primary_value_usd) AS value_usd
    FROM fact_trade_granular_v2 v
    WHERE v.flow_code = 'X'
      AND v.partner_iso NOT IN ('W00', 'WLD')
      AND v.cmd_code = 'TOTAL'
      AND v.primary_value_usd > 0
    GROUP BY 1, 2, 3, 4, 5
),
with_totals AS (
    SELECT
        c.*,
        SUM(value_usd) OVER (PARTITION BY ref_year, reporter_iso) AS rep_total,
        SUM(value_usd) OVER (PARTITION BY ref_year, partner_iso)  AS par_total
    FROM cor c
)
SELECT
    ref_year,
    reporter_desc,
    partner_desc,
    value_usd,
    value_usd / NULLIF(rep_total, 0) AS share_of_reporter,
    value_usd / NULLIF(par_total, 0) AS share_of_partner,
    (value_usd / NULLIF(rep_total, 0))
        * (value_usd / NULLIF(par_total, 0)) AS intensity
FROM with_totals
WHERE ref_year = (SELECT MAX(ref_year) FROM fact_trade_granular_v2)
ORDER BY intensity IS NULL, intensity DESC
LIMIT 25;


-- -------------------------------------------------------------------------
-- Q6.  Trade balance leaderboard: largest surpluses and deficits per year.
--
--      Technique: conditional aggregation via SUM(CASE WHEN ...),
--                 UNION ALL for top + bottom.
-- -------------------------------------------------------------------------
WITH cy AS (
    SELECT
        v.ref_year,
        v.reporter_iso,
        v.reporter_desc,
        SUM(CASE WHEN v.flow_code = 'X' THEN v.primary_value_usd ELSE 0 END) AS exports,
        SUM(CASE WHEN v.flow_code = 'M' THEN v.primary_value_usd ELSE 0 END) AS imports
    FROM fact_trade_granular_v2 v
    WHERE v.partner_iso NOT IN ('W00', 'WLD')
      AND v.cmd_code = 'TOTAL'
      AND v.primary_value_usd > 0
    GROUP BY 1, 2, 3
),
with_bal AS (
    SELECT
        ref_year, reporter_iso, reporter_desc,
        exports, imports,
        COALESCE(exports, 0) - COALESCE(imports, 0) AS balance
    FROM cy
)
(
  SELECT 'Surplus' AS kind, ref_year, reporter_desc, exports, imports, balance
  FROM with_bal
  WHERE ref_year = (SELECT MAX(ref_year) FROM fact_trade_granular_v2)
  ORDER BY balance DESC
  LIMIT 10
)
UNION ALL
(
  SELECT 'Deficit' AS kind, ref_year, reporter_desc, exports, imports, balance
  FROM with_bal
  WHERE ref_year = (SELECT MAX(ref_year) FROM fact_trade_granular_v2)
  ORDER BY balance ASC
  LIMIT 10
)
ORDER BY balance DESC;


-- -------------------------------------------------------------------------
-- Q7.  Commodity-level concentration over time, with rolling 3-yr average.
--
--      Technique: HHI per commodity-year + AVG OVER ROWS BETWEEN window.
-- -------------------------------------------------------------------------
WITH share AS (
    SELECT
        v.ref_year,
        v.cmd_code,
        v.cmd_desc,
        v.reporter_iso,
        SUM(v.primary_value_usd) AS value_usd,
        SUM(SUM(v.primary_value_usd)) OVER (
            PARTITION BY v.cmd_code, v.ref_year
        ) AS global_value
    FROM fact_trade_granular_v2 v
    WHERE v.flow_code = 'X'
      AND v.partner_iso NOT IN ('W00', 'WLD')
      AND v.cmd_code <> 'TOTAL'
      AND v.primary_value_usd > 0
    GROUP BY 1, 2, 3, 4
),
hhi_yr AS (
    SELECT
        ref_year, cmd_code, cmd_desc,
        SUM(POWER(value_usd / NULLIF(global_value, 0), 2)) * 10000 AS hhi
    FROM share
    GROUP BY 1, 2, 3
)
SELECT
    ref_year,
    cmd_desc,
    hhi,
    AVG(hhi) OVER (
        PARTITION BY cmd_code
        ORDER BY ref_year
        ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
    ) AS hhi_3yr_avg
FROM hhi_yr
ORDER BY cmd_code, ref_year;
