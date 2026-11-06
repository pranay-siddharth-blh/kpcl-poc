-- =============================================================================
--  KPCL GMS · mart layer (SQLite) · the only tables the chatbot's SQL may read
--  Re-implements ../gms_mart.sql as physical tables built from core_*.
--  Differences from gms_mart.sql, on purpose:
--    * facts carry their date attributes (fy, water_year, month_label, ...) and the
--      station key / category label, so most questions need no join at all;
--    * monthly and FY roll-ups carry ratios pre-computed as SUM/SUM for that row,
--      plus the numerator/denominator helper sums so any re-aggregation stays correct;
--    * "now" is the end of the data ({{DATA_AS_OF}}), never the wall clock;
--    * facts backed by empty core tables (JMR, billing, commercial, DMS) are not built
--      here; semantic_capability says they are not loaded.
--  anchor_scope marks rows whose totals are pinned to the K-eRP document:
--    document_day          RTPS / SGS unit figures on 06-Apr-2025 (daily reports)
--    document_month_total  RTPS / SGS March-2025 unit totals (MPRs)
--    document_fy_total     RTPS FY 2024-25 unit totals (MPR)
--    workbook_fy_estimate  other stations, FY 2024-25 total estimated from the HO report
--    NULL                  synthetic value
-- =============================================================================

-- ============================================================== DIMENSIONS
CREATE TABLE mart_dim_date AS
SELECT cal_date AS date_key, fy, fy_start_date, fy_month_no, water_year, wy_month_no, month_start, year_month,
       month_label, fy_quarter, week_start, day_name, is_weekend, season,
       CAST(strftime('%d', date(month_start, '+1 month', '-1 day')) AS INTEGER) AS days_in_month
FROM core_dim_date;
CREATE UNIQUE INDEX ux_mart_dim_date ON mart_dim_date (date_key);

CREATE TABLE mart_dim_station AS
SELECT s.station_code AS station_key, s.station_name, s.company_code, co.company_name,
       s.plant_category_code, pc.dashboard_label AS plant_category_label, pc.dashboard_order AS category_order,
       pt.plant_type_code, pt.description AS plant_type_label, s.installed_capacity_mw, s.commissioning_date,
       s.report_order,
       (SELECT COUNT(*) FROM core_unit u WHERE u.station_code = s.station_code) AS unit_count,
       (SELECT group_concat(reservoir_name, ', ') FROM (
            SELECT r.reservoir_name FROM core_reservoir_station rs JOIN core_reservoir r USING (reservoir_id)
            WHERE rs.station_code = s.station_code ORDER BY r.reservoir_name)) AS fed_by_reservoirs
FROM core_station s
JOIN core_company co ON co.company_code = s.company_code
JOIN core_plant_category pc ON pc.plant_category_code = s.plant_category_code
JOIN core_plant_type pt ON pt.plant_type_code = pc.plant_type_code;
CREATE UNIQUE INDEX ux_mart_dim_station ON mart_dim_station (station_key);

CREATE TABLE mart_dim_unit AS
SELECT u.unit_id AS unit_key, u.station_code AS station_key, s.station_name, u.unit_no, u.capacity_mw,
       u.fuel_code, f.description AS fuel_label, u.technology, u.commissioning_date, u.decommissioning_date,
       u.design_heat_rate_kcal_kwh, s.plant_category_label, s.plant_type_code,
       u.opening_balance_date, u.opening_cum_gen_mu, u.opening_cum_running_hrs, u.opening_cum_trips
FROM core_unit u
JOIN mart_dim_station s ON s.station_key = u.station_code
JOIN core_fuel f ON f.fuel_code = u.fuel_code;
CREATE UNIQUE INDEX ux_mart_dim_unit ON mart_dim_unit (unit_key);

CREATE TABLE mart_dim_reservoir AS
SELECT r.reservoir_id AS reservoir_key, r.reservoir_name, r.level_unit, r.full_reservoir_level, r.min_draw_down_level,
       r.energy_at_frl_mu,
       (SELECT group_concat(station_code, ', ') FROM (
            SELECT station_code FROM core_reservoir_station rs WHERE rs.reservoir_id = r.reservoir_id
            ORDER BY station_code)) AS feeds_stations
FROM core_reservoir r;

CREATE TABLE mart_dim_outage_reason AS
SELECT reason_code, description, plant_type_code, 'OUTAGE' AS reason_class FROM core_outage_reason
UNION ALL
SELECT reason_code, description, NULL, 'BACKDOWN' FROM core_backdown_reason;

CREATE TABLE mart_dim_btf_type AS
SELECT failure_type_code AS btf_type_code, description FROM core_btf_failure_type;

CREATE TABLE mart_dim_btf_location AS
SELECT location_code AS btf_location_code, description FROM core_btf_location;

CREATE TABLE mart_dim_coal_supplier AS
SELECT supplier_code, supplier_name, is_imported FROM core_coal_supplier;

-- ============================================================== GENERATION FACTS
-- grain: unit x day
CREATE TABLE mart_fact_unit_generation_daily AS
SELECT d.gen_date AS date_key, c.fy, c.water_year, c.month_start, c.year_month, c.month_label, c.fy_quarter,
       c.season, c.is_weekend,
       d.unit_id AS unit_key, u.station_key, u.plant_category_label, u.plant_type_code, u.capacity_mw,
       u.capacity_mw * 24 / 1000.0 AS capacity_mu,
       d.gross_gen_mu, d.aux_consumption_mu, d.net_export_mu, d.deemed_gen_mu, d.declared_capacity_mu,
       d.peak_hour_gen_mu, d.peak_load_mw, d.running_hrs, d.forced_outage_hrs, d.planned_outage_hrs, d.rsd_hrs,
       d.trips, d.coal_consumed_mt, d.hfo_consumed_kl, d.ldo_consumed_kl, d.gas_consumed_mmscm, d.msw_consumed_mt,
       d.gcv_coal_kcal_kg, d.gcv_oil_kcal_l, d.gross_heat_rate_kcal_kwh, d.dm_water_cum, d.h2_cylinders_nos,
       d.water_used_mcft,
       CASE WHEN d.gross_heat_rate_kcal_kwh > 5000
              OR (COALESCE(d.hfo_consumed_kl, 0) + COALESCE(d.ldo_consumed_kl, 0)) / NULLIF(d.gross_gen_mu, 0) > 50
            THEN 1 ELSE 0 END AS is_ratio_outlier,
       CASE WHEN d.gen_date = '2025-04-06' AND u.station_key IN ('RTPS', 'SGS') THEN 'document_day'
            WHEN c.month_label = 'MAR-2025' AND u.station_key IN ('RTPS', 'SGS') THEN 'document_month_total'
            WHEN c.fy = '2024-25' AND u.station_key = 'RTPS' THEN 'document_fy_total'
            WHEN c.fy = '2024-25' THEN 'workbook_fy_estimate' END AS anchor_scope
FROM core_unit_daily_generation d
JOIN mart_dim_unit u ON u.unit_key = d.unit_id
JOIN core_dim_date c ON c.cal_date = d.gen_date;
CREATE INDEX ix_mfugd_unit_date ON mart_fact_unit_generation_daily (unit_key, date_key);
CREATE INDEX ix_mfugd_station_date ON mart_fact_unit_generation_daily (station_key, date_key);
CREATE INDEX ix_mfugd_date ON mart_fact_unit_generation_daily (date_key);
CREATE INDEX ix_mfugd_fy ON mart_fact_unit_generation_daily (fy, station_key);
CREATE INDEX ix_mfugd_month ON mart_fact_unit_generation_daily (month_label, station_key);

-- grain: unit x month (the unit columns of the Monthly Performance Report)
CREATE TABLE mart_fact_unit_generation_monthly AS
WITH m AS (
    SELECT unit_key, month_start, MIN(fy) AS fy, MIN(year_month) AS year_month, MIN(month_label) AS month_label,
           MIN(fy_quarter) AS fy_quarter, MIN(season) AS season, MIN(station_key) AS station_key,
           MIN(plant_category_label) AS plant_category_label, MIN(plant_type_code) AS plant_type_code,
           MIN(capacity_mw) AS capacity_mw, COUNT(*) AS days_covered,
           SUM(capacity_mu) AS capacity_mu, SUM(declared_capacity_mu) AS declared_capacity_mu,
           SUM(gross_gen_mu) AS gross_gen_mu, SUM(aux_consumption_mu) AS aux_consumption_mu,
           SUM(net_export_mu) AS net_export_mu, SUM(deemed_gen_mu) AS deemed_gen_mu,
           SUM(peak_hour_gen_mu) AS peak_hour_gen_mu, MAX(peak_load_mw) AS peak_load_mw,
           SUM(running_hrs) AS running_hrs, SUM(forced_outage_hrs) AS forced_outage_hrs,
           SUM(planned_outage_hrs) AS planned_outage_hrs, SUM(rsd_hrs) AS rsd_hrs, SUM(trips) AS trips,
           SUM(coal_consumed_mt) AS coal_consumed_mt, SUM(hfo_consumed_kl) AS hfo_consumed_kl,
           SUM(ldo_consumed_kl) AS ldo_consumed_kl, SUM(gas_consumed_mmscm) AS gas_consumed_mmscm,
           SUM(msw_consumed_mt) AS msw_consumed_mt, SUM(dm_water_cum) AS dm_water_cum,
           SUM(h2_cylinders_nos) AS h2_cylinders_nos, SUM(water_used_mcft) AS water_used_mcft,
           SUM(gcv_coal_kcal_kg * coal_consumed_mt) AS gcv_coal_x_coal_mt,
           SUM(CASE WHEN gcv_coal_kcal_kg IS NOT NULL THEN coal_consumed_mt END) AS coal_mt_with_gcv,
           SUM(gross_heat_rate_kcal_kwh * gross_gen_mu) AS heat_rate_x_gen_mu,
           SUM(CASE WHEN gross_heat_rate_kcal_kwh IS NOT NULL THEN gross_gen_mu END) AS gen_mu_with_heat_rate,
           CASE WHEN COUNT(anchor_scope) = COUNT(*) AND MIN(anchor_scope) = MAX(anchor_scope) THEN MIN(anchor_scope) END AS anchor_scope
    FROM mart_fact_unit_generation_daily GROUP BY unit_key, month_start),
top AS (
    SELECT unit_key, month_start, date_key AS max_day_gen_date, gross_gen_mu AS max_day_gen_mu FROM (
        SELECT unit_key, month_start, date_key, gross_gen_mu,
               ROW_NUMBER() OVER (PARTITION BY unit_key, month_start ORDER BY gross_gen_mu DESC, date_key) AS rn
        FROM mart_fact_unit_generation_daily) WHERE rn = 1)
SELECT m.month_start AS date_key, m.fy, m.year_month, m.month_label, m.fy_quarter, m.season,
       m.unit_key, m.station_key, m.plant_category_label, m.plant_type_code, m.capacity_mw, m.days_covered,
       m.capacity_mu, m.declared_capacity_mu, m.gross_gen_mu, m.aux_consumption_mu, m.net_export_mu, m.deemed_gen_mu,
       m.peak_hour_gen_mu, m.peak_load_mw, top.max_day_gen_mu, CASE WHEN top.max_day_gen_mu > 0 THEN top.max_day_gen_date END AS max_day_gen_date,
       m.running_hrs, m.forced_outage_hrs, m.planned_outage_hrs, m.rsd_hrs, m.trips,
       m.coal_consumed_mt, m.hfo_consumed_kl, m.ldo_consumed_kl, m.gas_consumed_mmscm, m.msw_consumed_mt,
       m.dm_water_cum, m.h2_cylinders_nos, m.water_used_mcft,
       m.gcv_coal_x_coal_mt, m.coal_mt_with_gcv, m.heat_rate_x_gen_mu, m.gen_mu_with_heat_rate,
       t.cea_target_mu, t.internal_target_mu,
       ROUND(m.gross_gen_mu / NULLIF(m.capacity_mu, 0) * 100, 2) AS plf_pct,
       ROUND(m.declared_capacity_mu / NULLIF(m.capacity_mu, 0) * 100, 2) AS paf_pct,
       ROUND((m.gross_gen_mu + m.deemed_gen_mu) / NULLIF(m.capacity_mu, 0) * 100, 2) AS plf_with_nld_pct,
       ROUND(m.aux_consumption_mu / NULLIF(m.gross_gen_mu, 0) * 100, 2) AS aux_pct,
       ROUND(m.coal_consumed_mt / 1000.0 / NULLIF(m.gross_gen_mu, 0), 3) AS sp_coal_kg_kwh,
       ROUND((COALESCE(m.hfo_consumed_kl, 0) + COALESCE(m.ldo_consumed_kl, 0)) / NULLIF(m.gross_gen_mu, 0), 3) AS sp_oil_ml_kwh,
       ROUND(m.gcv_coal_x_coal_mt / NULLIF(m.coal_mt_with_gcv, 0)) AS wa_gcv_coal_kcal_kg,
       ROUND(m.heat_rate_x_gen_mu / NULLIF(m.gen_mu_with_heat_rate, 0)) AS wa_gross_heat_rate_kcal_kwh,
       ROUND(m.gross_gen_mu / NULLIF(t.cea_target_mu, 0) * 100, 2) AS cea_target_achievement_pct,
       m.anchor_scope
FROM m
JOIN top ON top.unit_key = m.unit_key AND top.month_start = m.month_start
LEFT JOIN core_generation_target t ON t.unit_id = m.unit_key AND t.target_month = m.month_start;
CREATE INDEX ix_mfugm_unit ON mart_fact_unit_generation_monthly (unit_key, date_key);
CREATE INDEX ix_mfugm_station ON mart_fact_unit_generation_monthly (station_key, month_label);

-- grain: station x day (HO Daily Generation Report)
CREATE TABLE mart_fact_station_generation_daily AS
SELECT g.date_key, MIN(g.fy) AS fy, MIN(g.water_year) AS water_year, MIN(g.month_start) AS month_start,
       MIN(g.year_month) AS year_month, MIN(g.month_label) AS month_label, MIN(g.fy_quarter) AS fy_quarter,
       MIN(g.season) AS season, MIN(g.is_weekend) AS is_weekend,
       g.station_key, MIN(g.plant_category_label) AS plant_category_label, MIN(g.plant_type_code) AS plant_type_code,
       COUNT(*) AS units_reporting,
       SUM(g.capacity_mu) AS capacity_mu, SUM(g.declared_capacity_mu) AS declared_capacity_mu,
       SUM(g.gross_gen_mu) AS gross_gen_mu, SUM(g.aux_consumption_mu) AS unit_aux_mu,
       COALESCE(x.common_aux_mu, 0) AS common_aux_mu,
       SUM(g.aux_consumption_mu) + COALESCE(x.common_aux_mu, 0) AS aux_consumption_mu,
       SUM(g.gross_gen_mu) - SUM(g.aux_consumption_mu) - COALESCE(x.common_aux_mu, 0) AS net_export_mu,
       SUM(g.deemed_gen_mu) AS deemed_gen_mu, SUM(g.peak_hour_gen_mu) AS peak_hour_gen_mu,
       SUM(g.running_hrs) AS running_hrs, SUM(g.forced_outage_hrs) AS forced_outage_hrs,
       SUM(g.planned_outage_hrs) AS planned_outage_hrs, SUM(g.rsd_hrs) AS rsd_hrs, SUM(g.trips) AS trips,
       SUM(g.coal_consumed_mt) AS coal_consumed_mt, SUM(g.hfo_consumed_kl) AS hfo_consumed_kl,
       SUM(g.ldo_consumed_kl) AS ldo_consumed_kl, SUM(g.gas_consumed_mmscm) AS gas_consumed_mmscm,
       SUM(g.msw_consumed_mt) AS msw_consumed_mt, SUM(g.dm_water_cum) AS dm_water_cum,
       SUM(g.water_used_mcft) AS water_used_mcft,
       ROUND(SUM(g.gross_gen_mu) / NULLIF(SUM(g.capacity_mu), 0) * 100, 2) AS plf_pct,
       ROUND(SUM(g.declared_capacity_mu) / NULLIF(SUM(g.capacity_mu), 0) * 100, 2) AS paf_pct,
       ROUND((SUM(g.aux_consumption_mu) + COALESCE(x.common_aux_mu, 0)) / NULLIF(SUM(g.gross_gen_mu), 0) * 100, 2) AS aux_pct,
       CASE WHEN COUNT(g.anchor_scope) = COUNT(*) AND MIN(g.anchor_scope) = MAX(g.anchor_scope) THEN MIN(g.anchor_scope) END AS anchor_scope
FROM mart_fact_unit_generation_daily g
LEFT JOIN core_station_daily_consumption x ON x.station_code = g.station_key AND x.gen_date = g.date_key
GROUP BY g.date_key, g.station_key;
CREATE INDEX ix_mfsgd_station_date ON mart_fact_station_generation_daily (station_key, date_key);
CREATE INDEX ix_mfsgd_date ON mart_fact_station_generation_daily (date_key);
CREATE INDEX ix_mfsgd_fy ON mart_fact_station_generation_daily (fy);
CREATE INDEX ix_mfsgd_wy ON mart_fact_station_generation_daily (water_year);

-- grain: station x month (station columns of the MPR, with CEA / internal targets)
CREATE TABLE mart_fact_station_generation_monthly AS
WITH m AS (
    SELECT station_key, date_key AS month_start, MIN(fy) AS fy, MIN(year_month) AS year_month,
           MIN(month_label) AS month_label, MIN(fy_quarter) AS fy_quarter, MIN(season) AS season,
           MIN(plant_category_label) AS plant_category_label, MIN(plant_type_code) AS plant_type_code,
           COUNT(*) AS units_reporting, MAX(days_covered) AS days_covered,
           SUM(capacity_mu) AS capacity_mu, SUM(declared_capacity_mu) AS declared_capacity_mu,
           SUM(gross_gen_mu) AS gross_gen_mu, SUM(aux_consumption_mu) AS unit_aux_mu,
           SUM(net_export_mu) AS unit_net_export_mu, SUM(deemed_gen_mu) AS deemed_gen_mu,
           SUM(peak_hour_gen_mu) AS peak_hour_gen_mu,
           SUM(running_hrs) AS running_hrs, SUM(forced_outage_hrs) AS forced_outage_hrs,
           SUM(planned_outage_hrs) AS planned_outage_hrs, SUM(rsd_hrs) AS rsd_hrs, SUM(trips) AS trips,
           SUM(coal_consumed_mt) AS coal_consumed_mt, SUM(hfo_consumed_kl) AS hfo_consumed_kl,
           SUM(ldo_consumed_kl) AS ldo_consumed_kl, SUM(gas_consumed_mmscm) AS gas_consumed_mmscm,
           SUM(msw_consumed_mt) AS msw_consumed_mt, SUM(dm_water_cum) AS dm_water_cum,
           SUM(water_used_mcft) AS water_used_mcft,
           SUM(gcv_coal_x_coal_mt) AS gcv_coal_x_coal_mt, SUM(coal_mt_with_gcv) AS coal_mt_with_gcv,
           SUM(heat_rate_x_gen_mu) AS heat_rate_x_gen_mu, SUM(gen_mu_with_heat_rate) AS gen_mu_with_heat_rate,
           SUM(cea_target_mu) AS cea_target_mu, SUM(internal_target_mu) AS internal_target_mu,
           CASE WHEN COUNT(anchor_scope) = COUNT(*) AND MIN(anchor_scope) = MAX(anchor_scope) THEN MIN(anchor_scope) END AS anchor_scope
    FROM mart_fact_unit_generation_monthly GROUP BY station_key, date_key),
common AS (
    SELECT station_key, month_start, SUM(common_aux_mu) AS common_aux_mu
    FROM mart_fact_station_generation_daily GROUP BY station_key, month_start),
top AS (
    SELECT station_key, month_start, date_key AS max_day_gen_date, gross_gen_mu AS max_day_gen_mu FROM (
        SELECT station_key, month_start, date_key, gross_gen_mu,
               ROW_NUMBER() OVER (PARTITION BY station_key, month_start ORDER BY gross_gen_mu DESC, date_key) AS rn
        FROM mart_fact_station_generation_daily) WHERE rn = 1)
SELECT m.month_start AS date_key, m.fy, m.year_month, m.month_label, m.fy_quarter, m.season,
       m.station_key, m.plant_category_label, m.plant_type_code, m.units_reporting, m.days_covered,
       m.capacity_mu, m.declared_capacity_mu, m.gross_gen_mu,
       m.unit_aux_mu + common.common_aux_mu AS aux_consumption_mu,
       m.unit_net_export_mu - common.common_aux_mu AS net_export_mu,
       m.deemed_gen_mu, m.peak_hour_gen_mu, top.max_day_gen_mu,
       CASE WHEN top.max_day_gen_mu > 0 THEN top.max_day_gen_date END AS max_day_gen_date,
       m.running_hrs, m.forced_outage_hrs, m.planned_outage_hrs, m.rsd_hrs, m.trips,
       m.coal_consumed_mt, m.hfo_consumed_kl, m.ldo_consumed_kl, m.gas_consumed_mmscm, m.msw_consumed_mt,
       m.dm_water_cum, m.water_used_mcft,
       m.gcv_coal_x_coal_mt, m.coal_mt_with_gcv, m.heat_rate_x_gen_mu, m.gen_mu_with_heat_rate,
       m.cea_target_mu, m.internal_target_mu,
       ROUND(m.gross_gen_mu / NULLIF(m.capacity_mu, 0) * 100, 2) AS plf_pct,
       ROUND(m.declared_capacity_mu / NULLIF(m.capacity_mu, 0) * 100, 2) AS paf_pct,
       ROUND((m.gross_gen_mu + m.deemed_gen_mu) / NULLIF(m.capacity_mu, 0) * 100, 2) AS plf_with_nld_pct,
       ROUND((m.unit_aux_mu + common.common_aux_mu) / NULLIF(m.gross_gen_mu, 0) * 100, 2) AS aux_pct,
       ROUND(m.coal_consumed_mt / 1000.0 / NULLIF(m.gross_gen_mu, 0), 3) AS sp_coal_kg_kwh,
       ROUND((COALESCE(m.hfo_consumed_kl, 0) + COALESCE(m.ldo_consumed_kl, 0)) / NULLIF(m.gross_gen_mu, 0), 3) AS sp_oil_ml_kwh,
       ROUND(m.gcv_coal_x_coal_mt / NULLIF(m.coal_mt_with_gcv, 0)) AS wa_gcv_coal_kcal_kg,
       ROUND(m.heat_rate_x_gen_mu / NULLIF(m.gen_mu_with_heat_rate, 0)) AS wa_gross_heat_rate_kcal_kwh,
       ROUND(m.gross_gen_mu / NULLIF(m.cea_target_mu, 0) * 100, 2) AS cea_target_achievement_pct,
       m.anchor_scope
FROM m
JOIN common ON common.station_key = m.station_key AND common.month_start = m.month_start
JOIN top ON top.station_key = m.station_key AND top.month_start = m.month_start;
CREATE INDEX ix_mfsgm_station ON mart_fact_station_generation_monthly (station_key, date_key);
CREATE INDEX ix_mfsgm_month ON mart_fact_station_generation_monthly (month_label);

-- grain: station x financial year
CREATE TABLE mart_fact_station_generation_fy AS
WITH f AS (
    SELECT fy, station_key, MIN(plant_category_label) AS plant_category_label, MIN(plant_type_code) AS plant_type_code,
           COUNT(*) AS months_covered, SUM(days_covered) AS days_covered,
           SUM(capacity_mu) AS capacity_mu, SUM(declared_capacity_mu) AS declared_capacity_mu,
           SUM(gross_gen_mu) AS gross_gen_mu, SUM(aux_consumption_mu) AS aux_consumption_mu,
           SUM(net_export_mu) AS net_export_mu, SUM(deemed_gen_mu) AS deemed_gen_mu, MAX(max_day_gen_mu) AS max_day_gen_mu,
           SUM(running_hrs) AS running_hrs, SUM(forced_outage_hrs) AS forced_outage_hrs,
           SUM(planned_outage_hrs) AS planned_outage_hrs, SUM(rsd_hrs) AS rsd_hrs, SUM(trips) AS trips,
           SUM(coal_consumed_mt) AS coal_consumed_mt, SUM(hfo_consumed_kl) AS hfo_consumed_kl,
           SUM(ldo_consumed_kl) AS ldo_consumed_kl, SUM(gas_consumed_mmscm) AS gas_consumed_mmscm,
           SUM(msw_consumed_mt) AS msw_consumed_mt, SUM(water_used_mcft) AS water_used_mcft,
           SUM(gcv_coal_x_coal_mt) AS gcv_coal_x_coal_mt, SUM(coal_mt_with_gcv) AS coal_mt_with_gcv,
           SUM(heat_rate_x_gen_mu) AS heat_rate_x_gen_mu, SUM(gen_mu_with_heat_rate) AS gen_mu_with_heat_rate,
           SUM(cea_target_mu) AS cea_target_mu, SUM(internal_target_mu) AS internal_target_mu,
           -- an FY is document-pinned only if every month of it is (RTPS FY 2024-25: Apr-Feb 'document_fy_total',
           -- Mar 'document_month_total'); otherwise the common scope of all months, else NULL
           CASE WHEN COUNT(anchor_scope) = COUNT(*) AND SUM(anchor_scope LIKE 'document%') = COUNT(*) THEN 'document_fy_total'
                WHEN COUNT(anchor_scope) = COUNT(*) AND MIN(anchor_scope) = MAX(anchor_scope) THEN MIN(anchor_scope) END AS anchor_scope
    FROM mart_fact_station_generation_monthly GROUP BY fy, station_key)
SELECT f.fy, f.station_key, f.plant_category_label, f.plant_type_code, f.months_covered, f.days_covered,
       CASE WHEN f.days_covered < (SELECT CAST(julianday(date(MIN(d.fy_start_date), '+1 year')) - julianday(MIN(d.fy_start_date)) AS INTEGER)
                                   FROM core_dim_date d WHERE d.fy = f.fy) THEN 1 ELSE 0 END AS is_partial_period,
       f.capacity_mu, f.declared_capacity_mu, f.gross_gen_mu, f.aux_consumption_mu, f.net_export_mu, f.deemed_gen_mu,
       f.max_day_gen_mu,
       (SELECT date_key FROM mart_fact_station_generation_daily s WHERE s.station_key = f.station_key AND s.fy = f.fy
         AND f.max_day_gen_mu > 0 ORDER BY s.gross_gen_mu DESC, s.date_key LIMIT 1) AS max_day_gen_date,
       f.running_hrs, f.forced_outage_hrs, f.planned_outage_hrs, f.rsd_hrs, f.trips,
       f.coal_consumed_mt, f.hfo_consumed_kl, f.ldo_consumed_kl, f.gas_consumed_mmscm, f.msw_consumed_mt, f.water_used_mcft,
       f.gcv_coal_x_coal_mt, f.coal_mt_with_gcv, f.heat_rate_x_gen_mu, f.gen_mu_with_heat_rate,
       f.cea_target_mu, f.internal_target_mu,
       ROUND(f.gross_gen_mu / NULLIF(f.capacity_mu, 0) * 100, 2) AS plf_pct,
       ROUND(f.declared_capacity_mu / NULLIF(f.capacity_mu, 0) * 100, 2) AS paf_pct,
       ROUND((f.gross_gen_mu + f.deemed_gen_mu) / NULLIF(f.capacity_mu, 0) * 100, 2) AS plf_with_nld_pct,
       ROUND(f.aux_consumption_mu / NULLIF(f.gross_gen_mu, 0) * 100, 2) AS aux_pct,
       ROUND(f.coal_consumed_mt / 1000.0 / NULLIF(f.gross_gen_mu, 0), 3) AS sp_coal_kg_kwh,
       ROUND((COALESCE(f.hfo_consumed_kl, 0) + COALESCE(f.ldo_consumed_kl, 0)) / NULLIF(f.gross_gen_mu, 0), 3) AS sp_oil_ml_kwh,
       ROUND(f.gcv_coal_x_coal_mt / NULLIF(f.coal_mt_with_gcv, 0)) AS wa_gcv_coal_kcal_kg,
       ROUND(f.heat_rate_x_gen_mu / NULLIF(f.gen_mu_with_heat_rate, 0)) AS wa_gross_heat_rate_kcal_kwh,
       ROUND(f.gross_gen_mu / NULLIF(f.cea_target_mu, 0) * 100, 2) AS cea_target_achievement_pct,
       f.anchor_scope
FROM f;
CREATE INDEX ix_mfsgf ON mart_fact_station_generation_fy (station_key, fy);

-- ============================================================== OUTAGES, BACK-DOWN, TARGETS
-- grain: one outage event (trip -> re-synchronisation)
CREATE TABLE mart_fact_unit_outage AS
SELECT o.outage_id AS outage_key, date(o.trip_at) AS date_key, c.fy, c.water_year, c.month_start, c.year_month,
       c.month_label, c.fy_quarter, c.season,
       o.unit_id AS unit_key, u.station_key, u.plant_category_label, u.plant_type_code,
       o.outage_type, o.trip_at, o.sync_at, CASE WHEN o.sync_at IS NULL THEN 1 ELSE 0 END AS is_open,
       COALESCE(o.outage_hrs,
                ROUND((julianday(date('{{DATA_AS_OF}}', '+1 day')) - julianday(o.trip_at)) * 24, 2)) AS outage_hrs,
       o.reason_code, r.description AS reason_description, o.reason_detail, o.gen_loss_mu, o.is_structural,
       CASE WHEN b.btf_id IS NOT NULL THEN 1 ELSE 0 END AS is_btf,
       b.failure_type_code AS btf_type_code, bt.description AS btf_type,
       b.location_code AS btf_location_code, bl.description AS btf_location
FROM core_unit_outage o
JOIN mart_dim_unit u ON u.unit_key = o.unit_id
JOIN core_dim_date c ON c.cal_date = date(o.trip_at)
JOIN core_outage_reason r ON r.reason_code = o.reason_code
LEFT JOIN core_boiler_tube_failure b ON b.outage_id = o.outage_id
LEFT JOIN core_btf_failure_type bt ON bt.failure_type_code = b.failure_type_code
LEFT JOIN core_btf_location bl ON bl.location_code = b.location_code;
CREATE INDEX ix_mfuo_station ON mart_fact_unit_outage (station_key, date_key);
CREATE INDEX ix_mfuo_unit ON mart_fact_unit_outage (unit_key, date_key);

-- grain: unit x day x back-down reason
CREATE TABLE mart_fact_generation_loss_daily AS
SELECT l.loss_date AS date_key, c.fy, c.water_year, c.month_start, c.year_month, c.month_label, c.fy_quarter, c.season,
       l.unit_id AS unit_key, u.station_key, u.plant_category_label,
       l.reason_code, r.description AS reason_description, l.loss_mu
FROM core_unit_generation_loss l
JOIN mart_dim_unit u ON u.unit_key = l.unit_id
JOIN core_dim_date c ON c.cal_date = l.loss_date
JOIN core_backdown_reason r ON r.reason_code = l.reason_code;
CREATE INDEX ix_mfgld_station ON mart_fact_generation_loss_daily (station_key, date_key);
CREATE INDEX ix_mfgld_fy ON mart_fact_generation_loss_daily (fy, station_key);

-- grain: unit x month
CREATE TABLE mart_fact_generation_target_monthly AS
SELECT t.target_month AS date_key, c.fy, c.year_month, c.month_label, c.fy_quarter,
       t.unit_id AS unit_key, u.station_key, u.plant_category_label, t.cea_target_mu, t.internal_target_mu
FROM core_generation_target t
JOIN mart_dim_unit u ON u.unit_key = t.unit_id
JOIN core_dim_date c ON c.cal_date = t.target_month;
CREATE INDEX ix_mfgtm ON mart_fact_generation_target_monthly (station_key, date_key);

-- ============================================================== COAL
-- grain: coal station x day
CREATE TABLE mart_fact_coal_stock_daily AS
SELECT k.stock_date AS date_key, c.fy, c.water_year, c.month_start, c.year_month, c.month_label, c.fy_quarter, c.season,
       k.station_code AS station_key, k.opening_stock_mt, k.receipt_mt, k.indigenous_consumed_mt, k.imported_consumed_mt,
       k.total_consumed_mt, k.closing_stock_mt, k.days_of_stock, k.normative_days,
       CASE WHEN k.days_of_stock < k.normative_days THEN 1 ELSE 0 END AS is_below_norm
FROM core_station_coal_stock k
JOIN core_dim_date c ON c.cal_date = k.stock_date;
CREATE INDEX ix_mfcsd ON mart_fact_coal_stock_daily (station_key, date_key);

-- grain: one rake delivery
CREATE TABLE mart_fact_coal_receipt AS
SELECT cr.receipt_id AS receipt_key, cr.receipt_date AS date_key, c.fy, c.month_start, c.year_month, c.month_label,
       c.fy_quarter, c.season, cr.station_code AS station_key, cr.supplier_code, s.supplier_name, s.is_imported,
       cr.no_of_rakes, cr.quantity_mt, cr.gcv_received_kcal_kg, cr.gcv_received_kcal_kg * cr.quantity_mt AS gcv_x_quantity_mt
FROM core_coal_receipt cr
JOIN core_coal_supplier s ON s.supplier_code = cr.supplier_code
JOIN core_dim_date c ON c.cal_date = cr.receipt_date;
CREATE INDEX ix_mfcr ON mart_fact_coal_receipt (station_key, date_key);

-- ============================================================== HYDROLOGY & GRID
-- grain: reservoir x day
CREATE TABLE mart_fact_reservoir_daily AS
SELECT l.level_date AS date_key, c.fy, c.water_year, c.month_start, c.year_month, c.month_label, c.season,
       l.reservoir_id AS reservoir_key, r.reservoir_name, r.level_unit, l.water_level, r.full_reservoir_level,
       r.min_draw_down_level, l.water_level - r.min_draw_down_level AS level_above_mddl,
       l.capacity_pct, l.inflow_mcft, l.discharge_power_mcft, l.spill_sluice_mcft, l.rainfall_mm, l.energy_potential_mu,
       r.feeds_stations
FROM core_reservoir_daily_level l
JOIN mart_dim_reservoir r ON r.reservoir_key = l.reservoir_id
JOIN core_dim_date c ON c.cal_date = l.level_date;
CREATE INDEX ix_mfrd ON mart_fact_reservoir_daily (reservoir_key, date_key);

-- grain: one day, Karnataka state grid, with KPCL's own generation alongside
CREATE TABLE mart_fact_grid_daily AS
SELECT g.summary_date AS date_key, c.fy, c.water_year, c.month_start, c.year_month, c.month_label, c.fy_quarter, c.season,
       g.state_consumption_mu, g.state_generation_mu, g.state_import_mu, g.system_peak_mw, g.freq_max_hz, g.freq_min_hz,
       g.tb_share_mu, g.jurala_share_mu, k.kpcl_gross_gen_mu,
       ROUND(k.kpcl_gross_gen_mu / NULLIF(g.state_consumption_mu, 0) * 100, 2) AS kpcl_share_of_consumption_pct
FROM core_state_grid_daily g
JOIN core_dim_date c ON c.cal_date = g.summary_date
LEFT JOIN (SELECT date_key, SUM(gross_gen_mu) AS kpcl_gross_gen_mu FROM mart_fact_station_generation_daily
           GROUP BY date_key) k ON k.date_key = g.summary_date;
CREATE UNIQUE INDEX ux_mfgd ON mart_fact_grid_daily (date_key);

-- ============================================================== POINT-IN-TIME (as of the data end)
-- grain: one row per unit, status at the end of {{DATA_AS_OF}}
CREATE TABLE mart_fact_unit_current_status AS
SELECT u.unit_key, u.station_key, u.plant_category_label, '{{DATA_AS_OF}}' AS as_of_date,
       CASE WHEN u.decommissioning_date IS NOT NULL AND u.decommissioning_date <= '{{DATA_AS_OF}}' THEN 'DECOMMISSIONED'
            WHEN o.outage_id IS NOT NULL THEN o.outage_type || '_OUTAGE' ELSE 'RUNNING' END AS status,
       COALESCE(o.trip_at, (SELECT MAX(x.sync_at) FROM core_unit_outage x WHERE x.unit_id = u.unit_key
                              AND x.sync_at <= '{{DATA_AS_OF}} 23:59:59'), u.commissioning_date) AS status_since,
       CASE WHEN o.outage_id IS NOT NULL
            THEN ROUND(julianday(date('{{DATA_AS_OF}}', '+1 day')) - julianday(o.trip_at), 1) END AS days_in_status,
       o.reason_code, o.reason_detail, o.is_structural
FROM mart_dim_unit u
LEFT JOIN core_unit_outage o ON o.outage_id = (
    SELECT x.outage_id FROM core_unit_outage x
    WHERE x.unit_id = u.unit_key AND x.trip_at <= '{{DATA_AS_OF}} 23:59:59'
      AND (x.sync_at IS NULL OR x.sync_at > '{{DATA_AS_OF}} 23:59:59')
    ORDER BY x.trip_at DESC LIMIT 1);

-- grain: one row per unit, life-to-date totals where an opening balance exists
CREATE TABLE mart_fact_unit_cumulative AS
SELECT u.unit_key, u.station_key, u.opening_balance_date,
       u.opening_cum_gen_mu + COALESCE(SUM(d.gross_gen_mu), 0) AS cum_gen_mu,
       u.opening_cum_running_hrs + COALESCE(SUM(d.running_hrs), 0) AS cum_running_hrs,
       u.opening_cum_trips + COALESCE(SUM(d.trips), 0) AS cum_trips,
       MAX(d.gen_date) AS as_on_date
FROM mart_dim_unit u
LEFT JOIN core_unit_daily_generation d
       ON d.unit_id = u.unit_key AND (u.opening_balance_date IS NULL OR d.gen_date > u.opening_balance_date)
GROUP BY u.unit_key, u.station_key, u.opening_balance_date, u.opening_cum_gen_mu, u.opening_cum_running_hrs,
         u.opening_cum_trips;
