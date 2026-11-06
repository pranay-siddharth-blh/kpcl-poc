-- NL2SQL question bank for gms_mart (the star-schema chatbot layer).
-- Each block: a "Q:" comment with the natural-language question, then the reference SQL.
-- Usable as few-shot examples (build_semantic_layer.py embeds these into gms_semantic_layer.json)
-- and as a regression test set. Every query joins at most one dimension per filter -- that is
-- the point of gms_mart: compare these to the same questions against the "gms" core schema.
SET search_path = gms_mart;

-- ===================== 1. HO dashboard / HO Daily Generation Report =====================
-- Q: What was KPCL's gross generation and net export in FY 2024-25 by plant category?
SELECT s.plant_category_label, round(sum(f.gross_gen_mu), 3) AS gross_gen_mu, round(sum(f.net_export_mu), 3) AS net_export_mu
FROM fact_station_generation_daily f JOIN dim_date d USING (date_key) JOIN dim_station s USING (station_key)
WHERE d.fy = '2024-25' GROUP BY s.plant_category_label ORDER BY gross_gen_mu DESC;

-- Q: Which stations generated less than their CEA target in March 2025?
SELECT s.station_name, round(f.gross_gen_mu, 3) AS actual_mu, f.cea_target_mu,
       round(f.gross_gen_mu - f.cea_target_mu, 3) AS shortfall_mu
FROM fact_station_generation_monthly f JOIN dim_date d USING (date_key) JOIN dim_station s USING (station_key)
WHERE d.month_label = 'MAR-2025' AND f.gross_gen_mu < f.cea_target_mu ORDER BY shortfall_mu;

-- Q: FY 2024-25 generation vs last FY for each station
SELECT cur.station_key, cur.gross_gen_mu AS fy_2024_25, prev.gross_gen_mu AS fy_2023_24,
       round((cur.gross_gen_mu / NULLIF(prev.gross_gen_mu, 0) - 1) * 100, 2) AS yoy_pct
FROM fact_station_generation_fy cur LEFT JOIN fact_station_generation_fy prev
       ON prev.station_key = cur.station_key AND prev.fy = '2023-24'
WHERE cur.fy = '2024-25' ORDER BY cur.gross_gen_mu DESC;

-- Q: SGS generation in water year 2024-25 vs water year 2023-24
SELECT d.water_year, round(sum(f.gross_gen_mu), 3) AS gross_gen_mu
FROM fact_unit_generation_daily f JOIN dim_date d USING (date_key)
WHERE f.station_key = 'SGS' AND d.water_year IN ('2023-24', '2024-25') GROUP BY d.water_year;

-- ===================== 2. Station daily report =====================
-- Q: Unit-wise generation, peak load, PLF, running hours and trips at RTPS on 06-Apr-2025
SELECT u.unit_id, f.gross_gen_mu, f.peak_load_mw, round(f.gross_gen_mu / f.capacity_mu * 100, 2) AS plf_pct,
       f.running_hrs, f.forced_outage_hrs, f.trips
FROM fact_unit_generation_daily f JOIN dim_unit u USING (unit_key)
WHERE f.station_key = 'RTPS' AND f.date_key = '2025-04-06' ORDER BY u.unit_no;

-- Q: What is the highest single-day generation of RTPS ever, and in FY 2024-25?
SELECT 'ever' AS scope, date_key, gross_gen_mu FROM
  (SELECT date_key, gross_gen_mu FROM fact_station_generation_daily WHERE station_key = 'RTPS' ORDER BY gross_gen_mu DESC LIMIT 1) a
UNION ALL
SELECT 'FY 2024-25', f.date_key, f.gross_gen_mu FROM fact_station_generation_daily f JOIN dim_date d USING (date_key)
WHERE f.station_key = 'RTPS' AND d.fy = '2024-25' ORDER BY gross_gen_mu DESC LIMIT 1;

-- ===================== 3. Monthly Performance Report =====================
-- Q: RTPS unit-wise MPR for March 2025 (generation, PLF, PAF, aux %, specific coal, specific oil, GCV, heat rate)
SELECT u.unit_id, round(sum(f.gross_gen_mu), 3) gross_gen_mu,
       round(sum(f.gross_gen_mu) / sum(f.capacity_mu) * 100, 2) AS plf_pct,
       round(sum(f.declared_capacity_mu) / sum(f.capacity_mu) * 100, 2) AS paf_pct,
       round(sum(f.aux_consumption_mu) / NULLIF(sum(f.gross_gen_mu), 0) * 100, 2) AS aux_pct,
       round(sum(f.coal_consumed_mt) / 1000 / NULLIF(sum(f.gross_gen_mu), 0), 3) AS sp_coal_kg_kwh,
       round(sum(f.hfo_consumed_kl + f.ldo_consumed_kl) / NULLIF(sum(f.gross_gen_mu), 0), 3) AS sp_oil_ml_kwh,
       round(sum(f.gcv_coal_kcal_kg * f.coal_consumed_mt) / NULLIF(sum(f.coal_consumed_mt) FILTER (WHERE f.gcv_coal_kcal_kg IS NOT NULL), 0)) AS wa_gcv_kcal_kg,
       round(sum(f.gross_heat_rate_kcal_kwh * f.gross_gen_mu) / NULLIF(sum(f.gross_gen_mu) FILTER (WHERE f.gross_heat_rate_kcal_kwh IS NOT NULL), 0)) AS wa_heat_rate_kcal_kwh,
       max(f.gross_gen_mu) AS max_day_gen_mu
FROM fact_unit_generation_daily f JOIN dim_date d USING (date_key) JOIN dim_unit u USING (unit_key)
WHERE f.station_key = 'RTPS' AND d.month_label = 'MAR-2025' GROUP BY u.unit_id, u.unit_no ORDER BY u.unit_no;

-- Q: RTPS station totals for FY 2024-25 and March 2025 (check against the source MPR: 8310.351 and 831.212 MU)
SELECT 'FY 2024-25' AS period, round(gross_gen_mu, 3) FROM fact_station_generation_fy WHERE station_key = 'RTPS' AND fy = '2024-25'
UNION ALL
SELECT 'MAR-2025', round(f.gross_gen_mu, 3) FROM fact_station_generation_monthly f JOIN dim_date d USING (date_key)
WHERE f.station_key = 'RTPS' AND d.month_label = 'MAR-2025';

-- Q: Actual gross heat rate vs design heat rate per BTPS unit in FY 2024-25
SELECT u.unit_id, u.design_heat_rate_kcal_kwh,
       round(sum(f.gross_heat_rate_kcal_kwh * f.gross_gen_mu) / sum(f.gross_gen_mu)) AS actual_ghr
FROM fact_unit_generation_daily f JOIN dim_date d USING (date_key) JOIN dim_unit u USING (unit_key)
WHERE f.station_key = 'BTPS' AND d.fy = '2024-25' AND f.gross_gen_mu > 0
GROUP BY u.unit_id, u.design_heat_rate_kcal_kwh ORDER BY u.unit_id;

-- Q: Cumulative generation, running hours and trips since first synchronisation for RTPS units
SELECT unit_key, round(cum_gen_mu, 3) cum_gen_mu, cum_running_hrs, cum_trips, as_on_date
FROM fact_unit_cumulative WHERE station_key = 'RTPS' ORDER BY unit_key;

-- ===================== 4. Back-down / generation loss =====================
-- Q: RTPS back-down in FY 2024-25 by reason
SELECT r.description, round(sum(f.loss_mu), 3) AS loss_mu
FROM fact_generation_loss_daily f JOIN dim_date d USING (date_key) JOIN dim_outage_reason r ON r.reason_key = f.reason_code
WHERE f.station_key = 'RTPS' AND d.fy = '2024-25' AND r.reason_class = 'BACKDOWN'
GROUP BY r.description ORDER BY loss_mu DESC;

-- ===================== 5. Outages, trips, BTF =====================
-- Q: Number of forced outages and outage hours by reason at BTPS in FY 2023-24
SELECT r.description, count(*) AS outages, round(sum(f.outage_hrs), 2) AS outage_hrs
FROM fact_unit_outage f JOIN dim_date d USING (date_key) JOIN dim_outage_reason r ON r.reason_key = f.reason_code
WHERE f.station_key = 'BTPS' AND d.fy = '2023-24' AND f.outage_type = 'FORCED' AND r.reason_class = 'OUTAGE'
GROUP BY r.description ORDER BY outages DESC;

-- Q: List boiler tube failures of BTPS-U3 in January 2024 with failure type and location
SELECT f.outage_key, f.trip_at, f.sync_at, f.outage_hrs, t.description AS failure_type, l.description AS location
FROM fact_unit_outage f JOIN dim_date d USING (date_key)
JOIN dim_btf_type t ON t.btf_type_key = f.btf_type_key JOIN dim_btf_location l ON l.btf_location_key = f.btf_location_key
WHERE f.unit_key = 'BTPS-U3' AND f.is_btf AND d.month_start = '2024-01-01' ORDER BY f.trip_at;

-- Q: Which boiler tube failure location is most common across all thermal stations?
SELECT l.description AS location, count(*) AS failures
FROM fact_unit_outage f JOIN dim_btf_location l ON l.btf_location_key = f.btf_location_key
WHERE f.is_btf GROUP BY l.description ORDER BY failures DESC;

-- Q: Which units are currently not running, and since when?
SELECT unit_key, status, status_since, reason_code FROM fact_unit_current_status WHERE status <> 'RUNNING' ORDER BY status_since;

-- ===================== 6. Coal =====================
-- Q: On how many days in FY 2024-25 was each station's coal stock below normative days?
SELECT f.station_key, count(*) AS days_below_norm
FROM fact_coal_stock_daily f JOIN dim_date d USING (date_key)
WHERE d.fy = '2024-25' AND f.days_of_stock < f.normative_days GROUP BY f.station_key;

-- Q: Rakes, quantity and weighted GCV of coal received by supplier in FY 2024-25
SELECT f.supplier_code, sum(f.no_of_rakes) rakes, round(sum(f.quantity_mt)) quantity_mt,
       round(sum(f.gcv_received_kcal_kg * f.quantity_mt) / sum(f.quantity_mt)) AS wa_gcv_kcal_kg
FROM fact_coal_receipt f JOIN dim_date d USING (date_key)
WHERE d.fy = '2024-25' GROUP BY f.supplier_code ORDER BY quantity_mt DESC;

-- Q: RTPS coal position on 06-Apr-2025 (OB, receipt, consumption, CB)
SELECT opening_stock_mt, receipt_mt, indigenous_consumed_mt, imported_consumed_mt, total_consumed_mt, closing_stock_mt, days_of_stock
FROM fact_coal_stock_daily WHERE station_key = 'RTPS' AND date_key = '2025-04-06';

-- ===================== 7. Reservoirs =====================
-- Q: Linganamakki level, storage % and energy on 06-Apr-2025 vs the same day last year
SELECT date_key, water_level, capacity_pct, energy_potential_mu
FROM fact_reservoir_daily WHERE reservoir_key = 1 AND date_key IN ('2025-04-06', '2024-04-06');

-- Q: Which power stations are fed by Supa dam, and what did they generate in water year 2024-25?
SELECT s.station_key, round(sum(f.gross_gen_mu), 3) AS gross_gen_mu
FROM dim_reservoir r
JOIN fact_station_generation_daily f ON f.station_key = ANY (string_to_array(r.feeds_stations, ', '))
JOIN dim_date d USING (date_key) JOIN dim_station s USING (station_key)
WHERE r.reservoir_name = 'SUPA' AND d.water_year = '2024-25' GROUP BY s.station_key;

-- ===================== 8. State grid =====================
-- Q: KPCL share of state consumption in March 2025
SELECT round(sum(k.gross_gen_mu) / sum(g.state_consumption_mu) * 100, 2) AS kpcl_share_pct
FROM fact_grid_daily g JOIN dim_date d ON d.date_key = g.date_key
JOIN (SELECT date_key, sum(gross_gen_mu) gross_gen_mu FROM fact_station_generation_daily GROUP BY date_key) k ON k.date_key = g.date_key
WHERE d.month_label = 'MAR-2025';

-- ===================== 9. Trends =====================
-- Q: SGS generation by season in FY 2023-24
SELECT d.season, round(sum(f.gross_gen_mu), 3) AS gross_gen_mu
FROM fact_station_generation_daily f JOIN dim_date d USING (date_key)
WHERE f.station_key = 'SGS' AND d.fy = '2023-24' GROUP BY d.season ORDER BY gross_gen_mu DESC;

-- Q: Quarter-wise PLF of thermal stations in FY 2024-25
SELECT f.station_key, d.fy_quarter, round(sum(f.gross_gen_mu) / sum(f.capacity_mu) * 100, 2) AS plf_pct
FROM fact_station_generation_daily f JOIN dim_date d USING (date_key) JOIN dim_station s USING (station_key)
WHERE s.plant_category_label = 'THERMAL-COAL' AND d.fy = '2024-25'
GROUP BY f.station_key, d.fy_quarter ORDER BY f.station_key, d.fy_quarter;

-- Q: Weekday vs weekend average daily KPCL generation in FY 2024-25
SELECT d.is_weekend, round(sum(f.gross_gen_mu) / count(DISTINCT f.date_key), 3) AS avg_daily_gen_mu
FROM fact_station_generation_daily f JOIN dim_date d USING (date_key)
WHERE d.fy = '2024-25' GROUP BY d.is_weekend;
