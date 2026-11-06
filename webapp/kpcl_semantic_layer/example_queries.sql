-- KPCL GMS worked examples: natural-language question -> reviewed SQLite SQL over the mart_* tables.
-- Each block starts with "-- N. <question>" and ends with ";". The chatbot prompt includes the blocks most
-- similar to the user's question; tests/test_example_queries.py runs every block against kpcl_gms.db.
-- Blocks 1-24 are ../../nl2sql_question_bank.sql rewritten for SQLite and the mart; 25+ are new.

-- 1. What was KPCL's gross generation and net export in FY 2024-25 by plant category?
SELECT plant_category_label AS plant_category, ROUND(SUM(gross_gen_mu), 3) AS gross_gen_mu,
       ROUND(SUM(net_export_mu), 3) AS net_export_mu
FROM mart_fact_station_generation_fy WHERE fy = '2024-25'
GROUP BY plant_category_label ORDER BY gross_gen_mu DESC;

-- 2. Which stations generated less than their CEA target in March 2025?
SELECT s.station_name AS station, ROUND(f.gross_gen_mu, 3) AS actual_mu, ROUND(f.cea_target_mu, 3) AS cea_target_mu,
       ROUND(f.gross_gen_mu - f.cea_target_mu, 3) AS shortfall_mu
FROM mart_fact_station_generation_monthly f JOIN mart_dim_station s ON s.station_key = f.station_key
WHERE f.month_label = 'MAR-2025' AND f.gross_gen_mu < f.cea_target_mu ORDER BY shortfall_mu;

-- 3. FY 2024-25 generation vs last FY for each station
SELECT cur.station_key AS station, ROUND(cur.gross_gen_mu, 3) AS fy_2024_25_mu, ROUND(prev.gross_gen_mu, 3) AS fy_2023_24_mu,
       ROUND((cur.gross_gen_mu / NULLIF(prev.gross_gen_mu, 0) - 1) * 100, 2) AS yoy_growth_pct
FROM mart_fact_station_generation_fy cur
LEFT JOIN mart_fact_station_generation_fy prev ON prev.station_key = cur.station_key AND prev.fy = '2023-24'
WHERE cur.fy = '2024-25' ORDER BY cur.gross_gen_mu DESC;

-- 4. SGS generation in water year 2024-25 vs water year 2023-24
SELECT water_year, ROUND(SUM(gross_gen_mu), 3) AS gross_gen_mu
FROM mart_fact_station_generation_daily
WHERE station_key = 'SGS' AND water_year IN ('2023-24', '2024-25') GROUP BY water_year ORDER BY water_year;

-- 5. Unit-wise generation, peak load, PLF, running hours and trips at RTPS on 06-Apr-2025
SELECT unit_key AS unit, gross_gen_mu, peak_load_mw, ROUND(gross_gen_mu / capacity_mu * 100, 2) AS plf_pct,
       running_hrs, forced_outage_hrs, trips
FROM mart_fact_unit_generation_daily WHERE station_key = 'RTPS' AND date_key = '2025-04-06' ORDER BY unit_key;

-- 6. What is the highest single-day generation of RTPS ever, and in FY 2024-25?
SELECT * FROM (SELECT 'ever' AS scope, date_key AS on_date, ROUND(gross_gen_mu, 3) AS gross_gen_mu
               FROM mart_fact_station_generation_daily WHERE station_key = 'RTPS' ORDER BY gross_gen_mu DESC LIMIT 1)
UNION ALL
SELECT * FROM (SELECT 'FY 2024-25', date_key, ROUND(gross_gen_mu, 3)
               FROM mart_fact_station_generation_daily WHERE station_key = 'RTPS' AND fy = '2024-25'
               ORDER BY gross_gen_mu DESC LIMIT 1);

-- 7. RTPS unit-wise MPR for March 2025 (generation, PLF, PAF, aux %, specific coal, specific oil, GCV, heat rate)
SELECT unit_key AS unit, ROUND(gross_gen_mu, 3) AS gross_gen_mu, plf_pct, paf_pct, aux_pct, sp_coal_kg_kwh, sp_oil_ml_kwh,
       wa_gcv_coal_kcal_kg, wa_gross_heat_rate_kcal_kwh, max_day_gen_mu
FROM mart_fact_unit_generation_monthly WHERE station_key = 'RTPS' AND month_label = 'MAR-2025' ORDER BY unit_key;

-- 8. RTPS station totals for FY 2024-25 and March 2025
SELECT 'FY 2024-25' AS period, ROUND(gross_gen_mu, 3) AS gross_gen_mu FROM mart_fact_station_generation_fy
WHERE station_key = 'RTPS' AND fy = '2024-25'
UNION ALL
SELECT 'MAR-2025', ROUND(gross_gen_mu, 3) FROM mart_fact_station_generation_monthly
WHERE station_key = 'RTPS' AND month_label = 'MAR-2025';

-- 9. Actual gross heat rate vs design heat rate per BTPS unit in FY 2024-25
SELECT m.unit_key AS unit, u.design_heat_rate_kcal_kwh AS design_heat_rate_kcal_kwh,
       ROUND(SUM(m.heat_rate_x_gen_mu) / SUM(m.gen_mu_with_heat_rate)) AS actual_heat_rate_kcal_kwh
FROM mart_fact_unit_generation_monthly m JOIN mart_dim_unit u ON u.unit_key = m.unit_key
WHERE m.station_key = 'BTPS' AND m.fy = '2024-25'
GROUP BY m.unit_key, u.design_heat_rate_kcal_kwh ORDER BY m.unit_key;

-- 10. Cumulative generation, running hours and trips since first synchronisation for RTPS units
SELECT unit_key AS unit, ROUND(cum_gen_mu, 3) AS cum_gen_mu, ROUND(cum_running_hrs, 2) AS cum_running_hrs, cum_trips, as_on_date
FROM mart_fact_unit_cumulative WHERE station_key = 'RTPS' ORDER BY unit_key;

-- 11. RTPS back-down in FY 2024-25 by reason
SELECT reason_description AS reason, ROUND(SUM(loss_mu), 3) AS loss_mu
FROM mart_fact_generation_loss_daily WHERE station_key = 'RTPS' AND fy = '2024-25'
GROUP BY reason_description ORDER BY loss_mu DESC;

-- 12. Number of forced outages and outage hours by reason at BTPS in FY 2023-24
SELECT reason_description AS reason, COUNT(*) AS outages, ROUND(SUM(outage_hrs), 2) AS outage_hrs
FROM mart_fact_unit_outage WHERE station_key = 'BTPS' AND fy = '2023-24' AND outage_type = 'FORCED'
GROUP BY reason_description ORDER BY outages DESC;

-- 13. List boiler tube failures of BTPS-U3 in January 2024 with failure type and location
SELECT outage_key AS outage, trip_at, sync_at, outage_hrs, btf_type AS failure_type, btf_location AS location
FROM mart_fact_unit_outage WHERE unit_key = 'BTPS-U3' AND is_btf = 1 AND month_label = 'JAN-2024' ORDER BY trip_at;

-- 14. Which boiler tube failure location is most common across all thermal stations?
SELECT btf_location AS location, COUNT(*) AS failures
FROM mart_fact_unit_outage WHERE is_btf = 1 GROUP BY btf_location ORDER BY failures DESC;

-- 15. Which units are currently not running, and since when?
SELECT unit_key AS unit, status, status_since, days_in_status, reason_code AS reason
FROM mart_fact_unit_current_status WHERE status <> 'RUNNING' ORDER BY status_since;

-- 16. On how many days in FY 2024-25 was each station's coal stock below normative days?
SELECT station_key AS station, SUM(is_below_norm) AS days_below_norm
FROM mart_fact_coal_stock_daily WHERE fy = '2024-25' GROUP BY station_key ORDER BY days_below_norm DESC;

-- 17. Rakes, quantity and weighted GCV of coal received by supplier in FY 2024-25
SELECT supplier_name AS supplier, SUM(no_of_rakes) AS rakes, ROUND(SUM(quantity_mt)) AS quantity_mt,
       ROUND(SUM(gcv_x_quantity_mt) / SUM(quantity_mt)) AS weighted_gcv_kcal_kg
FROM mart_fact_coal_receipt WHERE fy = '2024-25' GROUP BY supplier_name ORDER BY quantity_mt DESC;

-- 18. RTPS coal position on 06-Apr-2025 (opening stock, receipt, consumption, closing stock)
SELECT opening_stock_mt, receipt_mt, indigenous_consumed_mt, imported_consumed_mt, total_consumed_mt, closing_stock_mt, days_of_stock
FROM mart_fact_coal_stock_daily WHERE station_key = 'RTPS' AND date_key = '2025-04-06';

-- 19. Linganamakki level, storage % and energy on 06-Apr-2025 vs the same day last year
SELECT date_key AS on_date, water_level, level_unit, capacity_pct, energy_potential_mu
FROM mart_fact_reservoir_daily WHERE reservoir_name = 'LINGANAMAKKI' AND date_key IN ('2025-04-06', '2024-04-06')
ORDER BY date_key;

-- 20. Which power stations are fed by Supa dam, and what did they generate in water year 2024-25?
SELECT f.station_key AS station, ROUND(SUM(f.gross_gen_mu), 3) AS gross_gen_mu
FROM mart_fact_station_generation_daily f
WHERE f.water_year = '2024-25'
  AND instr(', ' || (SELECT feeds_stations FROM mart_dim_reservoir WHERE reservoir_name = 'SUPA') || ',',
            ', ' || f.station_key || ',') > 0
GROUP BY f.station_key ORDER BY gross_gen_mu DESC;

-- 21. KPCL share of state consumption in March 2025
SELECT ROUND(SUM(kpcl_gross_gen_mu) / SUM(state_consumption_mu) * 100, 2) AS kpcl_share_pct
FROM mart_fact_grid_daily WHERE month_label = 'MAR-2025';

-- 22. SGS generation by season in FY 2023-24
SELECT season, ROUND(SUM(gross_gen_mu), 3) AS gross_gen_mu
FROM mart_fact_station_generation_daily WHERE station_key = 'SGS' AND fy = '2023-24'
GROUP BY season ORDER BY gross_gen_mu DESC;

-- 23. Quarter-wise PLF of thermal stations in FY 2024-25
SELECT station_key AS station, fy_quarter, ROUND(SUM(gross_gen_mu) / SUM(capacity_mu) * 100, 2) AS plf_pct
FROM mart_fact_station_generation_monthly WHERE plant_category_label = 'THERMAL-COAL' AND fy = '2024-25'
GROUP BY station_key, fy_quarter ORDER BY station_key, fy_quarter;

-- 24. Weekday vs weekend average daily KPCL generation in FY 2024-25
SELECT CASE is_weekend WHEN 1 THEN 'weekend' ELSE 'weekday' END AS day_type,
       ROUND(SUM(gross_gen_mu) / COUNT(DISTINCT date_key), 3) AS avg_daily_gen_mu
FROM mart_fact_station_generation_daily WHERE fy = '2024-25' GROUP BY is_weekend;

-- 25. Top 5 stations by PLF in FY 2024-25
SELECT s.station_name AS station, f.plf_pct
FROM mart_fact_station_generation_fy f JOIN mart_dim_station s ON s.station_key = f.station_key
WHERE f.fy = '2024-25' AND f.plf_pct IS NOT NULL ORDER BY f.plf_pct DESC LIMIT 5;

-- 26. Monthly trend of KPCL gross generation in FY 2024-25
SELECT year_month, ROUND(SUM(gross_gen_mu), 3) AS gross_gen_mu
FROM mart_fact_station_generation_monthly WHERE fy = '2024-25' GROUP BY year_month ORDER BY year_month;

-- 27. KPCL total gross generation by financial year with year-on-year growth
SELECT fy, ROUND(total_mu, 3) AS gross_gen_mu,
       ROUND((total_mu / LAG(total_mu) OVER (ORDER BY fy) - 1) * 100, 2) AS yoy_growth_pct
FROM (SELECT fy, SUM(gross_gen_mu) AS total_mu FROM mart_fact_station_generation_fy
      WHERE fy <> '2025-26' GROUP BY fy) ORDER BY fy;

-- 28. CEA target achievement of each thermal station in FY 2024-25
SELECT station_key AS station, ROUND(gross_gen_mu, 3) AS gross_gen_mu, ROUND(cea_target_mu, 3) AS cea_target_mu,
       cea_target_achievement_pct
FROM mart_fact_station_generation_fy WHERE fy = '2024-25' AND plant_category_label = 'THERMAL-COAL'
ORDER BY cea_target_achievement_pct DESC;

-- 29. Specific coal consumption of each thermal station in FY 2024-25
SELECT station_key AS station, sp_coal_kg_kwh
FROM mart_fact_station_generation_fy WHERE fy = '2024-25' AND plant_category_label = 'THERMAL-COAL' ORDER BY sp_coal_kg_kwh;

-- 30. Which month of FY 2024-25 had the highest KPCL generation?
SELECT month_label, ROUND(SUM(gross_gen_mu), 3) AS gross_gen_mu
FROM mart_fact_station_generation_monthly WHERE fy = '2024-25'
GROUP BY month_label ORDER BY gross_gen_mu DESC LIMIT 1;

-- 31. Auxiliary consumption % of each RTPS unit in FY 2024-25
SELECT unit_key AS unit, ROUND(SUM(aux_consumption_mu) / NULLIF(SUM(gross_gen_mu), 0) * 100, 2) AS aux_pct
FROM mart_fact_unit_generation_monthly WHERE station_key = 'RTPS' AND fy = '2024-25'
GROUP BY unit_key ORDER BY unit_key;

-- 32. Total hydro generation by water year
SELECT water_year, ROUND(SUM(gross_gen_mu), 3) AS gross_gen_mu
FROM mart_fact_station_generation_daily WHERE plant_type_code = 'HYDRO' GROUP BY water_year ORDER BY water_year;

-- 33. What is the PLF of thermal stations in FY 2024-25 overall?
SELECT ROUND(SUM(gross_gen_mu) / SUM(capacity_mu) * 100, 2) AS plf_pct
FROM mart_fact_station_generation_fy WHERE fy = '2024-25' AND plant_category_label = 'THERMAL-COAL';

-- 35. How much imported coal did RTPS receive in FY 2023-24?
SELECT ROUND(SUM(quantity_mt)) AS imported_coal_received_mt, SUM(no_of_rakes) AS rakes
FROM mart_fact_coal_receipt WHERE station_key = 'RTPS' AND fy = '2023-24' AND is_imported = 1;

-- 36. Which hydro unit had the lowest PLF in FY 2023-24, leaving out units that generated nothing?
SELECT unit_key AS unit, ROUND(SUM(gross_gen_mu) / SUM(capacity_mu) * 100, 2) AS plf_pct
FROM mart_fact_unit_generation_monthly WHERE plant_type_code = 'HYDRO' AND fy = '2023-24'
GROUP BY unit_key HAVING SUM(gross_gen_mu) > 0 ORDER BY plf_pct LIMIT 1;

-- 34. Compare BTPS and YTPS gross generation and PLF in FY 2024-25
SELECT station_key AS station, ROUND(gross_gen_mu, 3) AS gross_gen_mu, plf_pct
FROM mart_fact_station_generation_fy WHERE fy = '2024-25' AND station_key IN ('BTPS', 'YTPS') ORDER BY station_key;
