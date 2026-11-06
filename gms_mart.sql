-- =============================================================================
--  KPCL GMS  ·  gms_mart  ·  star schema for the NL2SQL chatbot
--  Built entirely on top of gms_schema.sql (views only — no data is copied,
--  so the mart can never drift from the gms core; promote to materialized
--  views later if query latency needs it).
--
--  Why a star on top of a normalized core:
--    * gms (core) models transactions and workflow correctly: a bill has
--      lines, a line has an ESCOM split, a JMR statement has meter readings.
--      That needs real foreign keys and belongs in 3NF.
--    * An NL2SQL model does better against a FEW WIDE TABLES than against
--      a web of 47 joins. Every fact below already carries the attributes
--      you would normally reach via 3-4 joins (station name, category,
--      fy, month_label, season, ...), so most questions compile to
--      "SELECT ... FROM one_fact JOIN dim_date" with zero guesswork about
--      which bridge table to cross.
--    * Every ratio (PLF, PAF, aux%, specific coal, heat rate) is exposed
--      pre-computed as SUM/SUM, never AVG of a percentage column, so a
--      naive "AVG(plf_pct)" query still gives the right answer.
--
--  Naming: dim_* = flat, wide dimension. fact_* = one row per grain,
--  numeric measures only + the surrogate/business keys needed to slice.
--  Every table and column carries a COMMENT; the chatbot's schema prompt
--  should be built FROM these comments (see gms_semantic_layer.sql), not
--  by hand, so the two never fall out of sync.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS gms_mart;
SET search_path = gms_mart, gms;


-- =============================================================================
-- DIMENSIONS
-- =============================================================================

CREATE VIEW dim_date AS
SELECT cal_date AS date_key, cal_date, fy, fy_start_date, fy_month_no, water_year, wy_month_no,
       month_start, year_month, month_label, fy_quarter, week_start, day_name, is_weekend, is_holiday,
       holiday_name, season
FROM gms.dim_date;
COMMENT ON VIEW dim_date IS
  'One row per calendar day, 01-Apr-2015 onward. Join any fact''s date_key here for fy (Apr-Mar financial year, '
  '"2024-25"), water_year (Jun-May hydro year), month_label ("MAR-2025"), fy_quarter (Q1-Q4) and season '
  '(SUMMER/MONSOON/POST_MONSOON/WINTER). Always group by fy or month_label here rather than by extract(year from date).';

CREATE VIEW dim_station AS
SELECT s.station_code AS station_key, s.station_code, s.station_name, s.company_code, co.company_name,
       s.plant_category_code, pc.dashboard_label AS plant_category_label, pt.plant_type_code, pt.description AS plant_type_label,
       s.installed_capacity_mw, s.commissioning_date, s.report_order, s.is_active,
       (SELECT count(*) FROM gms.unit u WHERE u.station_code = s.station_code) AS unit_count,
       (SELECT string_agg(rs.reservoir_id::text || ':' || r.reservoir_name, ', ')
          FROM gms.reservoir_station rs JOIN gms.reservoir r USING (reservoir_id)
          WHERE rs.station_code = s.station_code) AS fed_by_reservoirs
FROM gms.station s
JOIN gms.company co ON co.company_code = s.company_code
JOIN gms.plant_category pc ON pc.plant_category_code = s.plant_category_code
JOIN gms.plant_type pt ON pt.plant_type_code = pc.plant_type_code;
COMMENT ON VIEW dim_station IS
  'One row per power station (33 rows). plant_category_label matches the HO dashboard groups exactly: '
  'THERMAL-COAL, MAJOR HYDRO, MINOR HYDRO, GAS, RES (waste-to-energy), SOLAR, WIND. Use plant_category_label '
  'for "by category" questions, plant_type_label for the coarser THERMAL/HYDRO/GAS/WTE/SOLAR/WIND split.';

CREATE VIEW dim_unit AS
SELECT u.unit_id AS unit_key, u.unit_id, u.unit_no, u.station_code, s.station_name, s.company_code,
       s.plant_category_code, s.plant_category_label, s.plant_type_code, s.plant_type_label,
       u.capacity_mw, u.fuel_code, f.description AS fuel_label, u.technology,
       u.commissioning_date, u.decommissioning_date,
       (u.decommissioning_date IS NOT NULL AND u.decommissioning_date <= current_date) AS is_decommissioned,
       u.design_heat_rate_kcal_kwh, u.opening_balance_date, u.opening_cum_gen_mu,
       u.opening_cum_running_hrs, u.opening_cum_trips
FROM gms.unit u
JOIN dim_station s ON s.station_code = u.station_code
JOIN gms.fuel f ON f.fuel_code = u.fuel_code;
COMMENT ON VIEW dim_unit IS
  'One row per generating unit (103 rows), pre-joined to its station''s name, company, category and type. '
  'unit_id is the natural key used everywhere else, e.g. RTPS-U8. Prefer this over joining unit->station yourself.';

CREATE VIEW dim_reservoir AS
SELECT r.reservoir_id AS reservoir_key, r.reservoir_id, r.reservoir_name, r.level_unit,
       r.full_reservoir_level, r.min_draw_down_level, r.gross_storage_frl_mcft, r.energy_at_frl_mu,
       (SELECT string_agg(station_code, ', ' ORDER BY station_code) FROM gms.reservoir_station rs
         WHERE rs.reservoir_id = r.reservoir_id) AS feeds_stations
FROM gms.reservoir r;
COMMENT ON VIEW dim_reservoir IS 'One row per dam/reservoir (6 rows). feeds_stations lists the hydro stations it supplies, comma separated.';

CREATE VIEW dim_outage_reason AS
SELECT reason_code AS reason_key, reason_code, description, plant_type_code, 'OUTAGE'::text AS reason_class
FROM gms.outage_reason
UNION ALL
SELECT reason_code, reason_code, description, NULL, 'BACKDOWN'
FROM gms.backdown_reason;
COMMENT ON VIEW dim_outage_reason IS
  'Two families of reason code sharing one lookup: reason_class = OUTAGE (why a unit tripped/was shut down; '
  'used by fact_unit_outage) or BACKDOWN (why running generation was curtailed; used by fact_generation_loss_daily). '
  'Do not mix the two families in one WHERE reason_code = ... across both facts.';

CREATE VIEW dim_btf_type AS
SELECT failure_type_code AS btf_type_key, failure_type_code, description FROM gms.btf_failure_type;
COMMENT ON VIEW dim_btf_type IS 'Boiler tube failure root cause, e.g. EROSION_SOOT_BLOWER, WELD_DEFECT.';

CREATE VIEW dim_btf_location AS
SELECT location_code AS btf_location_key, location_code, description FROM gms.btf_location;
COMMENT ON VIEW dim_btf_location IS 'Boiler tube failure location, e.g. WATER_WALL, RE_HEATER, SUPER_HEATER.';

CREATE VIEW dim_coal_supplier AS
SELECT supplier_code AS supplier_key, supplier_code, supplier_name, is_imported FROM gms.coal_supplier;
COMMENT ON VIEW dim_coal_supplier IS 'Coal supplier: MCL, SCCL, WCL (all domestic) or IMPORTED.';

CREATE VIEW dim_counterparty AS
SELECT counterparty_code AS counterparty_key, counterparty_code, counterparty_name, counterparty_type, gstin, pan
FROM gms.counterparty;
COMMENT ON VIEW dim_counterparty IS 'ESCOMs and other billing counterparties: BESCOM, MESCOM, CESCOM, HESCOM, GESCOM, KPTCL, NTPC, SECI, GoK.';

CREATE VIEW dim_billing_group AS
SELECT bg.billing_group_id AS billing_group_key, bg.billing_group_id, bg.billing_group_name, bg.bill_section,
       bg.pool_code, bg.display_order,
       string_agg(DISTINCT bgm.station_code, ', ' ORDER BY bgm.station_code) AS stations,
       string_agg(DISTINCT bgm.unit_id, ', ' ORDER BY bgm.unit_id) FILTER (WHERE bgm.unit_id IS NOT NULL) AS units
FROM gms.billing_group bg
LEFT JOIN gms.billing_group_member bgm USING (billing_group_id)
GROUP BY bg.billing_group_id, bg.billing_group_name, bg.bill_section, bg.pool_code, bg.display_order;
COMMENT ON VIEW dim_billing_group IS
  'One row per line item on the ESCOM energy bill, e.g. "Sharavathy Valley Project (SVP)", "RTPS Unit 1 & 2". '
  'bill_section is HYDEL_RES or THERMAL, matching the two halves of the bill.';

CREATE VIEW dim_division AS SELECT division_code AS division_key, division_code, division_name, station_code FROM gms.division;
CREATE VIEW dim_employee AS SELECT employee_no AS employee_key, employee_no, employee_name, designation, division_code FROM gms.employee;


-- =============================================================================
-- FACTS
-- =============================================================================

-- grain: one row per unit per day  (replaces unit_daily_generation + station_daily_consumption join)
CREATE VIEW fact_unit_generation_daily AS
SELECT d.gen_date AS date_key, d.unit_id AS unit_key, u.station_code AS station_key,
       d.gross_gen_mu, d.aux_consumption_mu, d.net_export_mu, d.deemed_gen_mu,
       d.peak_hour_gen_mu, d.peak_hour_gen_morning_mu, d.peak_hour_gen_evening_mu, d.peak_load_mw, d.peak_load_time,
       d.running_hrs, d.forced_outage_hrs, d.planned_outage_hrs, d.rsd_hrs, d.trips, d.declared_capacity_mu,
       d.coal_consumed_mt, d.hfo_consumed_kl, d.ldo_consumed_kl, d.gas_consumed_mmscm, d.msw_consumed_mt,
       d.gcv_coal_kcal_kg, d.gcv_oil_kcal_l, d.gross_heat_rate_kcal_kwh, d.dm_water_cum, d.h2_cylinders_nos,
       d.water_used_mcft, u.capacity_mw * 24 / 1000.0 AS capacity_mu
FROM gms.unit_daily_generation d
JOIN gms.unit u ON u.unit_id = d.unit_id;
COMMENT ON VIEW fact_unit_generation_daily IS
  'GRAIN: one row per unit per calendar day (365,132 rows). The base fact for every generation/PLF/fuel question. '
  'Join dim_date on date_key and dim_unit on unit_key to slice by fy/month/season/station/category. '
  'Ratios are NOT stored here: compute PLF as SUM(gross_gen_mu)/SUM(capacity_mu)*100, aux% as '
  'SUM(aux_consumption_mu)/SUM(gross_gen_mu)*100, specific coal as SUM(coal_consumed_mt)/1000/SUM(gross_gen_mu) '
  '-- always SUM/SUM over the rows in scope, never AVG a per-row ratio. For a station total, SUM over its units; '
  'station net export additionally subtracts common-aux — see fact_station_common_consumption_daily.';

-- grain: one row per station per day, for the small set of items that are station- not unit-level
CREATE VIEW fact_station_common_consumption_daily AS
SELECT gen_date AS date_key, station_code AS station_key, common_aux_mu, local_feeder_kwh, colony_consumption_kwh
FROM gms.station_daily_consumption;
COMMENT ON VIEW fact_station_common_consumption_daily IS
  'GRAIN: one row per station per day. Station-level (not unit-level) auxiliary/colony/feeder consumption. '
  'True station net export = SUM(unit net_export_mu from fact_unit_generation_daily) - common_aux_mu. '
  'Most stations have no row here on most days (common_aux defaults to 0) — LEFT JOIN and COALESCE to 0.';

-- grain: one row per unit per outage event (trip -> re-sync); BTF columns denormalized in for one-hop queries
CREATE VIEW fact_unit_outage AS
SELECT o.outage_id AS outage_key, o.unit_id AS unit_key, u.station_code AS station_key,
       o.trip_at::date AS date_key, o.trip_at, o.sync_at, (o.sync_at IS NULL) AS is_open,
       COALESCE(o.outage_hrs, round(extract(epoch FROM (localtimestamp - o.trip_at)) / 3600, 2)) AS outage_hrs,
       o.outage_type, o.reason_code, o.reason_detail, o.gen_loss_mu, o.is_structural,
       (b.btf_id IS NOT NULL) AS is_btf, b.failure_type_code AS btf_type_key, b.location_code AS btf_location_key,
       b.work_details, b.inspection_details, b.closure_at
FROM gms.unit_outage o
JOIN gms.unit u ON u.unit_id = o.unit_id
LEFT JOIN gms.boiler_tube_failure b ON b.outage_id = o.outage_id;
COMMENT ON VIEW fact_unit_outage IS
  'GRAIN: one row per trip/shutdown event (5,877 rows). outage_type is FORCED, PLANNED or RSD. is_open = true '
  'means the unit is still out (sync_at is NULL); outage_hrs is then computed live to now. is_btf = true for '
  'boiler tube failures, with btf_type_key/btf_location_key filled from the DMS register. '
  'For "currently down" questions, filter is_open = true directly rather than re-deriving status per unit.';

-- grain: one row per unit per day per back-down reason
CREATE VIEW fact_generation_loss_daily AS
SELECT l.loss_date AS date_key, l.unit_id AS unit_key, u.station_code AS station_key, l.reason_code, l.loss_mu
FROM gms.unit_generation_loss l
JOIN gms.unit u ON u.unit_id = l.unit_id;
COMMENT ON VIEW fact_generation_loss_daily IS
  'GRAIN: one row per unit per day per back-down reason (185,988 rows). "Reasons for back-down generation" from '
  'the daily report — MU of generation foregone, only for thermal/gas stations (RTPS, BTPS, YTPS, YCCPP). '
  'reason_code joins dim_outage_reason WHERE reason_class = ''BACKDOWN''. NO_LOAD_DEMAND rows are also the '
  'source of deemed_gen_mu (NLD) in fact_unit_generation_daily.';

-- grain: one row per unit per target month
CREATE VIEW fact_generation_target_monthly AS
SELECT target_month AS date_key, unit_id AS unit_key, station_code AS station_key,
       cea_target_mu, kerc_approved_mu, internal_target_mu
FROM gms.generation_target g JOIN gms.unit u USING (unit_id);
COMMENT ON VIEW fact_generation_target_monthly IS
  'GRAIN: one row per unit per month (date_key = first day of month). Join dim_date on date_key for fy/month_label. '
  'kerc_approved_mu is NULL in almost every row (not yet approved) — do not filter on it being non-null unless asked.';

-- grain: one row per station per day (coal stations only)
CREATE VIEW fact_coal_stock_daily AS
SELECT stock_date AS date_key, station_code AS station_key, opening_stock_mt, receipt_mt,
       indigenous_consumed_mt, imported_consumed_mt, total_consumed_mt, closing_stock_mt, days_of_stock, normative_days
FROM gms.station_coal_stock;
COMMENT ON VIEW fact_coal_stock_daily IS
  'GRAIN: one row per coal station per day (RTPS, BTPS, YTPS). closing_stock_mt = opening + receipt - consumed. '
  '"Below norm" = days_of_stock < normative_days.';

-- grain: one row per coal delivery
CREATE VIEW fact_coal_receipt AS
SELECT receipt_id AS receipt_key, receipt_date AS date_key, station_code AS station_key, supplier_code,
       no_of_rakes, quantity_mt, gcv_received_kcal_kg
FROM gms.coal_receipt;
COMMENT ON VIEW fact_coal_receipt IS
  'GRAIN: one row per rake delivery (22,300 rows). For a weighted-average GCV over many receipts use '
  'SUM(gcv_received_kcal_kg*quantity_mt)/SUM(quantity_mt), never AVG(gcv_received_kcal_kg).';

-- grain: one row per reservoir per day
CREATE VIEW fact_reservoir_daily AS
SELECT level_date AS date_key, reservoir_id AS reservoir_key, water_level, capacity_pct, inflow_mcft,
       discharge_power_mcft, spill_sluice_mcft, rainfall_mm, energy_potential_mu
FROM gms.reservoir_daily_level;
COMMENT ON VIEW fact_reservoir_daily IS 'GRAIN: one row per dam per day. water_level is in dim_reservoir.level_unit (FT or M) for that reservoir.';

-- grain: one row per day (state grid)
CREATE VIEW fact_grid_daily AS
SELECT summary_date AS date_key, state_consumption_mu, state_generation_mu, state_import_mu, system_peak_mw,
       system_peak_time, freq_max_hz, freq_min_hz, tb_share_mu, jurala_share_mu, cgs_mu, upcl_mu, jsw_mu, others_mu
FROM gms.state_grid_daily;
COMMENT ON VIEW fact_grid_daily IS
  'GRAIN: one row per day, Karnataka state grid totals entered at Head Office. KPCL share of state demand = '
  'SUM(fact_unit_generation_daily.gross_gen_mu) / SUM(state_consumption_mu) for the same dates.';

-- grain: one row per meter per JMR statement (i.e. per meter per station per month)
CREATE VIEW fact_jmr_meter_monthly AS
SELECT j.jmr_month AS date_key, j.station_code AS station_key, m.unit_id AS unit_key, m.meter_id, m.meter_role,
       r.initial_reading, r.final_reading, r.meter_constant, r.computed_gen, r.difference, r.energy_mu,
       j.reading_upto_date, j.net_export_jmr_mu AS station_net_export_jmr_mu
FROM gms.jmr_meter_reading r
JOIN gms.jmr_statement j USING (jmr_id)
JOIN gms.energy_meter m USING (meter_id);
COMMENT ON VIEW fact_jmr_meter_monthly IS
  'GRAIN: one row per meter per station per month (the signed Joint Meter Reading / Annexure-I). meter_role is '
  'UNIT_GEN, UNIT_AUX, COMMON_AUX or STATION_EXPORT. station_net_export_jmr_mu repeats the one billing figure '
  'for the whole statement (safe to SUM DISTINCT per station-month, not per row). Compare against '
  'fact_unit_generation_daily summed over the month to reconcile billing vs operational energy.';

-- grain: one row per ESCOM per billing group per bill (the sale-of-energy bill)
CREATE VIEW fact_energy_bill_escom AS
SELECT eb.bill_month AS date_key, el.billing_group_id AS billing_group_key, es.counterparty_code AS counterparty_key,
       eb.bill_id, eb.bill_no, eb.bill_type, eb.status,
       es.transmitted_kwh, es.share_pct, es.amount_rs,
       el.net_transmission_kwh AS line_total_transmitted_kwh, el.total_amount_rs AS line_total_amount_rs
FROM gms.energy_bill_escom_split es
JOIN gms.energy_bill_line el USING (bill_line_id)
JOIN gms.energy_bill eb USING (bill_id);
COMMENT ON VIEW fact_energy_bill_escom IS
  'GRAIN: one row per ESCOM per billing-group line per bill ("Bill for Sale of Energy to ESCOMs"). '
  'amount_rs is that ESCOM''s share of that line; SUM(amount_rs) grouped by counterparty_key gives the '
  'per-ESCOM total on the bill. line_total_amount_rs repeats the line''s grand total (do not SUM it across ESCOM rows).';

-- grain: one row per commercial transaction (receipts, withdrawals, RE subsidy, SECI)
CREATE VIEW fact_commercial_transaction AS
SELECT txn_date AS date_key, counterparty_code AS counterparty_key, station_code AS station_key,
       txn_type_code, receipt_sub_type, amount_rs, tds_rs, rebate_rs, writeoff_rs, bill_id
FROM gms.commercial_transaction;
COMMENT ON VIEW fact_commercial_transaction IS
  'GRAIN: one row per transaction. txn_type_code: ESCOM_BILL_CLAIM, ESCOM_BILL_WITHDRAWAL, RECEIPT, RE_SUBSIDY_GOK, SECI_RECEIPT.';

-- grain: one row per defect
CREATE VIEW fact_defect AS
SELECT d.defect_id AS defect_key, d.logged_at::date AS date_key, d.station_code AS station_key, d.unit_id AS unit_key,
       d.priority, d.defect_type, d.status, d.raised_by_division, d.attending_division,
       d.logged_at, d.acknowledged_at, d.completed_at, d.closed_at, d.ptw_required,
       extract(epoch FROM (COALESCE(d.closed_at, now()) - d.logged_at)) / 3600 AS age_or_open_hrs
FROM gms.defect d;
COMMENT ON VIEW fact_defect IS 'GRAIN: one row per logged defect (DMS). age_or_open_hrs is hours-to-close, or hours-still-open if not yet closed.';


-- =============================================================================
-- Convenience pre-aggregated facts (station level, monthly/FY) — the exact
-- grain of the HO Daily Generation Report / Monthly & Financial-Year summaries.
-- Kept as views so they stay derived, never re-entered.
-- =============================================================================

CREATE VIEW fact_station_generation_daily AS
SELECT g.date_key, g.station_key, sum(g.gross_gen_mu) gross_gen_mu, sum(g.aux_consumption_mu) unit_aux_mu,
       COALESCE(x.common_aux_mu, 0) common_aux_mu,
       sum(g.gross_gen_mu) - sum(g.aux_consumption_mu) - COALESCE(x.common_aux_mu, 0) AS net_export_mu,
       sum(g.deemed_gen_mu) deemed_gen_mu, sum(g.peak_hour_gen_mu) peak_hour_gen_mu, max(g.peak_load_mw) peak_load_mw,
       sum(g.running_hrs) running_hrs, sum(g.forced_outage_hrs) forced_outage_hrs,
       sum(g.planned_outage_hrs) planned_outage_hrs, sum(g.rsd_hrs) rsd_hrs, sum(g.trips) trips,
       sum(g.coal_consumed_mt) coal_consumed_mt, sum(g.hfo_consumed_kl) hfo_consumed_kl, sum(g.ldo_consumed_kl) ldo_consumed_kl,
       sum(g.water_used_mcft) water_used_mcft, sum(g.declared_capacity_mu) declared_capacity_mu, sum(g.capacity_mu) capacity_mu
FROM fact_unit_generation_daily g
LEFT JOIN fact_station_common_consumption_daily x USING (date_key, station_key)
GROUP BY g.date_key, g.station_key, x.common_aux_mu;
COMMENT ON VIEW fact_station_generation_daily IS
  'GRAIN: one row per station per day — pre-summed from fact_unit_generation_daily so station-level questions '
  'need no GROUP BY unit. PLF% = gross_gen_mu/capacity_mu*100; PAF% = declared_capacity_mu/capacity_mu*100.';

CREATE VIEW fact_station_generation_monthly AS
SELECT d.month_start AS date_key, g.station_key,
       sum(g.gross_gen_mu) gross_gen_mu, sum(g.unit_aux_mu + g.common_aux_mu) aux_consumption_mu,
       sum(g.net_export_mu) net_export_mu, sum(g.deemed_gen_mu) deemed_gen_mu, max(g.gross_gen_mu) max_day_gen_mu,
       sum(g.forced_outage_hrs) forced_outage_hrs, sum(g.planned_outage_hrs) planned_outage_hrs,
       sum(g.running_hrs) running_hrs, sum(g.trips) trips, sum(g.coal_consumed_mt) coal_consumed_mt,
       sum(g.hfo_consumed_kl) hfo_consumed_kl, sum(g.ldo_consumed_kl) ldo_consumed_kl, sum(g.water_used_mcft) water_used_mcft,
       sum(g.capacity_mu) capacity_mu, sum(g.declared_capacity_mu) declared_capacity_mu,
       sum(t.cea_target_mu) cea_target_mu, sum(t.internal_target_mu) internal_target_mu
FROM fact_station_generation_daily g
JOIN gms.dim_date d ON d.cal_date = g.date_key
LEFT JOIN (SELECT date_key, station_key, sum(cea_target_mu) cea_target_mu, sum(internal_target_mu) internal_target_mu
             FROM fact_generation_target_monthly GROUP BY date_key, station_key) t
       ON t.date_key = d.month_start AND t.station_key = g.station_key
GROUP BY d.month_start, g.station_key, t.cea_target_mu, t.internal_target_mu;
COMMENT ON VIEW fact_station_generation_monthly IS
  'GRAIN: one row per station per month (the Monthly Performance Report, station columns). date_key = first of month.';

CREATE VIEW fact_station_generation_fy AS
SELECT dd.fy, m.station_key, sum(m.gross_gen_mu) gross_gen_mu, sum(m.aux_consumption_mu) aux_consumption_mu,
       sum(m.net_export_mu) net_export_mu, sum(m.deemed_gen_mu) deemed_gen_mu, max(m.max_day_gen_mu) max_day_gen_mu,
       sum(m.forced_outage_hrs) forced_outage_hrs, sum(m.planned_outage_hrs) planned_outage_hrs, sum(m.trips) trips,
       sum(m.coal_consumed_mt) coal_consumed_mt, sum(m.hfo_consumed_kl) hfo_consumed_kl,
       sum(m.capacity_mu) capacity_mu, sum(m.declared_capacity_mu) declared_capacity_mu,
       sum(m.cea_target_mu) cea_target_mu, sum(m.internal_target_mu) internal_target_mu
FROM fact_station_generation_monthly m
JOIN gms.dim_date dd ON dd.month_start = m.date_key AND dd.cal_date = dd.month_start
GROUP BY dd.fy, m.station_key;
COMMENT ON VIEW fact_station_generation_fy IS
  'GRAIN: one row per station per financial year (Apr-Mar). PLF% = gross_gen_mu/capacity_mu*100. '
  'Use for "this FY vs last FY" and "top N stations by annual generation" questions.';


-- =============================================================================
-- Derived point-in-time facts (no history table backs these; always "as of now")
-- =============================================================================

CREATE VIEW fact_unit_current_status AS
SELECT u.unit_id AS unit_key, u.station_code AS station_key,
       CASE WHEN u.decommissioning_date IS NOT NULL AND u.decommissioning_date <= current_date THEN 'DECOMMISSIONED'
            WHEN o.outage_key IS NOT NULL THEN o.outage_type || '_OUTAGE' ELSE 'RUNNING' END AS status,
       COALESCE(o.trip_at, last_sync.sync_at, u.commissioning_date::timestamp) AS status_since,
       o.reason_code, o.reason_detail
FROM gms.unit u
LEFT JOIN LATERAL (SELECT * FROM fact_unit_outage x WHERE x.unit_key = u.unit_id AND x.is_open ORDER BY x.trip_at DESC LIMIT 1) o ON true
LEFT JOIN LATERAL (SELECT max(sync_at) sync_at FROM gms.unit_outage x WHERE x.unit_id = u.unit_id) last_sync ON true;
COMMENT ON VIEW fact_unit_current_status IS
  'GRAIN: one row per unit, always "right now" (no date_key — this is not history). status is RUNNING, '
  'FORCED_OUTAGE, PLANNED_OUTAGE, RSD_OUTAGE or DECOMMISSIONED. Use for "which units are down right now" questions.';

CREATE VIEW fact_unit_cumulative AS
SELECT u.unit_id AS unit_key, u.station_code AS station_key,
       u.opening_cum_gen_mu + COALESCE(sum(d.gross_gen_mu), 0) AS cum_gen_mu,
       u.opening_cum_running_hrs + COALESCE(sum(d.running_hrs), 0) AS cum_running_hrs,
       u.opening_cum_trips + COALESCE(sum(d.trips), 0) AS cum_trips,
       max(d.gen_date) AS as_on_date
FROM gms.unit u
LEFT JOIN gms.unit_daily_generation d ON d.unit_id = u.unit_id
       AND (u.opening_balance_date IS NULL OR d.gen_date > u.opening_balance_date)
GROUP BY u.unit_id, u.station_code, u.opening_cum_gen_mu, u.opening_cum_running_hrs, u.opening_cum_trips;
COMMENT ON VIEW fact_unit_cumulative IS
  'GRAIN: one row per unit, cumulative generation/hours/trips since first synchronisation (opening balance + '
  'everything in fact_unit_generation_daily since). "Life-to-date" questions.';


COMMENT ON SCHEMA gms_mart IS
  'Star-schema NL2SQL layer for KPCL GMS. dim_* = flat dimension, fact_* = numeric measures at a stated grain '
  '(see each view''s comment). Point every chatbot query here, not at schema "gms" directly — gms_mart needs '
  'at most one join per filter, gms needs three or four. All views; always reflects live gms data.';
