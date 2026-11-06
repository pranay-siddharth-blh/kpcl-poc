-- =============================================================================
--  KPCL K-eRP  ·  GMS (Generation Management System)  ·  PostgreSQL 15+ schema
--  Designed as the single source of truth for NL2SQL over GMS data.
--
--  Conventions
--    * snake_case, singular table names; *_code = business key, *_id = surrogate
--    * unit of measure is always the column suffix:
--        _mu  million kWh      _kwh  kWh          _mw  megawatt      _hrs  hours
--        _mt  tonnes           _kl   kilolitres   _rs  rupees        _pct  0-100
--        _mcft million cu.ft   _cum  cubic metre  _kcal_kg / _kcal_l / _kcal_kwh
--    * base tables hold only measured / entered facts; ratios (PLF, PAF, aux %,
--      specific coal, heat rate roll-ups) live in the v_* views, computed as
--      SUM/SUM so they are correct at every level of aggregation.
--    * every fact joins to dim_date, so FY / water year / season / month label
--      questions never need date arithmetic.
--
--  Sections
--    1  Reference & masters          5  Metering / Joint Meter Reading (JMR)
--    2  Generation operations        6  Commercial (billing, ESCOM receipts)
--    3  Fuel (coal)                  7  DMS (defects, PTW, line clearance)
--    4  Hydrology & state grid       8  Reporting views (NL2SQL surface)
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS gms;
SET search_path = gms;


-- =============================================================================
-- 1. REFERENCE & MASTERS
-- =============================================================================

CREATE TABLE company (
    company_code        text PRIMARY KEY,                 -- KPCL, RPCL, KPCGPCL
    company_name        text NOT NULL
);

CREATE TABLE plant_type (
    plant_type_code     text PRIMARY KEY,                 -- THERMAL, GAS, WTE, HYDRO, WIND, SOLAR
    description         text NOT NULL
);

CREATE TABLE plant_category (
    plant_category_code text PRIMARY KEY,                 -- THERMAL-COAL, GAS, WTE, MAJOR HYDRO, MINOR HYDRO, WIND, SOLAR
    plant_type_code     text NOT NULL REFERENCES plant_type,
    dashboard_label     text NOT NULL,                    -- label on the HO "Abstract Generation Details" panel
    dashboard_order     smallint NOT NULL
);

CREATE TABLE station (
    station_code          text PRIMARY KEY,               -- RTPS, BTPS, SGS ... (the "Project" in K-eRP reports)
    station_name          text NOT NULL UNIQUE,
    company_code          text NOT NULL REFERENCES company,
    plant_category_code   text NOT NULL REFERENCES plant_category,
    district              text,
    installed_capacity_mw numeric(10,3) NOT NULL CHECK (installed_capacity_mw > 0),
    commissioning_date    date,
    report_order          smallint NOT NULL UNIQUE,       -- row order of the HO Daily Generation Report
    is_active             boolean NOT NULL DEFAULT true
);

CREATE TABLE fuel (
    fuel_code           text PRIMARY KEY,                 -- COAL, GAS, MSW, WATER, WIND, SUN
    description         text NOT NULL
);

CREATE TABLE unit (
    unit_id                    text PRIMARY KEY,          -- '<station_code>-U<n>', e.g. RTPS-U8
    station_code               text NOT NULL REFERENCES station,
    unit_no                    smallint NOT NULL CHECK (unit_no > 0),
    capacity_mw                numeric(10,3) NOT NULL CHECK (capacity_mw > 0),
    fuel_code                  text NOT NULL REFERENCES fuel,
    technology                 text NOT NULL,             -- SUBCRITICAL, SUPERCRITICAL, CCGT, WTE, FRANCIS, WTG, PV
    commissioning_date         date NOT NULL,             -- first synchronisation
    decommissioning_date       date,
    design_heat_rate_kcal_kwh  numeric(8,1),
    opening_balance_date       date,                      -- cumulative figures below are "as on" this date
    opening_cum_gen_mu         numeric(14,3),             -- NULL = opening balance not known (cumulative view returns NULL)
    opening_cum_running_hrs    numeric(12,2),
    opening_cum_trips          integer,
    UNIQUE (station_code, unit_no),
    CHECK (decommissioning_date IS NULL OR decommissioning_date > commissioning_date)
);

CREATE TABLE dim_date (
    cal_date      date PRIMARY KEY,
    fy            text     NOT NULL,                      -- financial year Apr-Mar, '2024-25'
    fy_start_date date     NOT NULL,
    fy_month_no   smallint NOT NULL CHECK (fy_month_no BETWEEN 1 AND 12),   -- Apr = 1
    water_year    text     NOT NULL,                      -- hydro water year Jun-May, '2024-25'
    wy_month_no   smallint NOT NULL CHECK (wy_month_no BETWEEN 1 AND 12),   -- Jun = 1
    month_start   date     NOT NULL,
    year_month    text     NOT NULL,                      -- '2025-03'
    month_label   text     NOT NULL,                      -- 'MAR-2025' (K-eRP style)
    fy_quarter    text     NOT NULL CHECK (fy_quarter IN ('Q1','Q2','Q3','Q4')),
    week_start    date     NOT NULL,
    day_name      text     NOT NULL,
    is_weekend    boolean  NOT NULL,
    is_holiday    boolean  NOT NULL DEFAULT false,
    holiday_name  text,
    season        text     NOT NULL CHECK (season IN ('SUMMER','MONSOON','POST_MONSOON','WINTER'))
);
CREATE INDEX ON dim_date (fy);
CREATE INDEX ON dim_date (month_start);

CREATE TABLE reservoir (
    reservoir_id           smallint PRIMARY KEY,
    reservoir_name         text NOT NULL UNIQUE,          -- LINGANAMAKKI, SUPA, MANI ...
    level_unit             text NOT NULL CHECK (level_unit IN ('FT','M')),
    full_reservoir_level   numeric(8,2) NOT NULL,         -- FRL
    min_draw_down_level    numeric(8,2) NOT NULL,         -- MDDL
    gross_storage_frl_mcft numeric(12,2),
    energy_at_frl_mu       numeric(10,2),
    CHECK (full_reservoir_level > min_draw_down_level)
);

-- which reservoir feeds which power house (many-to-many; replaces the comma list)
CREATE TABLE reservoir_station (
    reservoir_id  smallint NOT NULL REFERENCES reservoir,
    station_code  text     NOT NULL REFERENCES station,
    PRIMARY KEY (reservoir_id, station_code)
);

-- organisation, kept minimal (HRMS is the master for people)
CREATE TABLE division (
    division_code  text PRIMARY KEY,                      -- 'EE(TM)-1', 'EE(CCM)/RTPS'
    division_name  text NOT NULL,
    station_code   text REFERENCES station                -- NULL for Head Office divisions
);

CREATE TABLE employee (
    employee_no    text PRIMARY KEY,                      -- HRMS employee number
    employee_name  text NOT NULL,
    designation    text,                                  -- AE(M), AEE, EE ...
    division_code  text REFERENCES division
);


-- =============================================================================
-- 2. GENERATION OPERATIONS
-- =============================================================================

-- UCB hourly log (Head Office > UCB Hourly Data screen)
CREATE TABLE unit_hourly_reading (
    unit_id          text     NOT NULL REFERENCES unit,
    reading_date     date     NOT NULL REFERENCES dim_date,
    hour_no          smallint NOT NULL CHECK (hour_no BETWEEN 1 AND 24),   -- hour ending
    emr_reading      numeric(16,3),                       -- energy meter register
    actual_load_mw   numeric(8,2) CHECK (actual_load_mw >= 0),
    mvar             numeric(8,2),
    frequency_hz     numeric(5,2) CHECK (frequency_hz BETWEEN 45 AND 55),
    coal_flow_tph    numeric(8,2) CHECK (coal_flow_tph >= 0),
    gen_mu           numeric(10,6) CHECK (gen_mu >= 0),
    gen_loss_mu      numeric(10,6) CHECK (gen_loss_mu >= 0),
    entered_by       text REFERENCES employee,
    PRIMARY KEY (unit_id, reading_date, hour_no)
);

-- One row per unit per day: every parameter of the station Daily Generation Report
CREATE TABLE unit_daily_generation (
    unit_id                  text NOT NULL REFERENCES unit,
    gen_date                 date NOT NULL REFERENCES dim_date,
    gross_gen_mu             numeric(10,4) NOT NULL CHECK (gross_gen_mu >= 0),
    aux_consumption_mu       numeric(10,4) NOT NULL DEFAULT 0 CHECK (aux_consumption_mu >= 0),   -- unit auxiliaries only
    net_export_mu            numeric(10,4) GENERATED ALWAYS AS (gross_gen_mu - aux_consumption_mu) STORED,
    peak_hour_gen_mu         numeric(10,4) CHECK (peak_hour_gen_mu >= 0),
    peak_hour_gen_morning_mu numeric(10,4) CHECK (peak_hour_gen_morning_mu >= 0),
    peak_hour_gen_evening_mu numeric(10,4) CHECK (peak_hour_gen_evening_mu >= 0),
    peak_load_mw             numeric(8,2)  CHECK (peak_load_mw >= 0),
    peak_load_time           time,
    running_hrs              numeric(5,2) NOT NULL DEFAULT 0 CHECK (running_hrs        BETWEEN 0 AND 24),
    forced_outage_hrs        numeric(5,2) NOT NULL DEFAULT 0 CHECK (forced_outage_hrs  BETWEEN 0 AND 24),
    planned_outage_hrs       numeric(5,2) NOT NULL DEFAULT 0 CHECK (planned_outage_hrs BETWEEN 0 AND 24),
    rsd_hrs                  numeric(5,2) NOT NULL DEFAULT 0 CHECK (rsd_hrs            BETWEEN 0 AND 24),  -- reserve shut down (= idle hours for hydro)
    trips                    smallint     NOT NULL DEFAULT 0 CHECK (trips >= 0),
    declared_capacity_mu     numeric(10,4) CHECK (declared_capacity_mu >= 0),   -- DC; drives PAF
    deemed_gen_mu            numeric(10,4) NOT NULL DEFAULT 0 CHECK (deemed_gen_mu >= 0),  -- NLD (no-load-demand deemed generation)
    coal_consumed_mt         numeric(12,2) CHECK (coal_consumed_mt >= 0),
    hfo_consumed_kl          numeric(10,3) CHECK (hfo_consumed_kl  >= 0),
    ldo_consumed_kl          numeric(10,3) CHECK (ldo_consumed_kl  >= 0),
    gas_consumed_mmscm       numeric(10,4) CHECK (gas_consumed_mmscm >= 0),
    msw_consumed_mt          numeric(10,2) CHECK (msw_consumed_mt    >= 0),
    gcv_coal_kcal_kg         numeric(7,1)  CHECK (gcv_coal_kcal_kg   > 0),      -- as-fired
    gcv_oil_kcal_l           numeric(7,1)  CHECK (gcv_oil_kcal_l     > 0),
    gross_heat_rate_kcal_kwh numeric(7,1)  CHECK (gross_heat_rate_kcal_kwh > 0),
    dm_water_cum             numeric(10,2) CHECK (dm_water_cum      >= 0),
    h2_cylinders_nos         smallint      CHECK (h2_cylinders_nos  >= 0),
    water_used_mcft          numeric(12,3) CHECK (water_used_mcft   >= 0),      -- hydro: water let out for generation
    remarks                  text,
    data_source              text NOT NULL DEFAULT 'KERP',
    PRIMARY KEY (unit_id, gen_date),
    CHECK (aux_consumption_mu <= gross_gen_mu OR gross_gen_mu = 0),
    CHECK (abs(running_hrs + forced_outage_hrs + planned_outage_hrs + rsd_hrs - 24) <= 0.05)
);
CREATE INDEX ON unit_daily_generation (gen_date);

-- Station-level daily items that are not attributable to a single unit
CREATE TABLE station_daily_consumption (
    station_code           text NOT NULL REFERENCES station,
    gen_date               date NOT NULL REFERENCES dim_date,
    common_aux_mu          numeric(10,4) NOT NULL DEFAULT 0 CHECK (common_aux_mu >= 0),   -- station transformer / common aux
    local_feeder_kwh       numeric(12,2) NOT NULL DEFAULT 0 CHECK (local_feeder_kwh >= 0),
    colony_consumption_kwh numeric(12,2) NOT NULL DEFAULT 0 CHECK (colony_consumption_kwh >= 0),
    PRIMARY KEY (station_code, gen_date)
);

-- Reasons for back-down ("Reasons for back down generation in MU" block)
CREATE TABLE backdown_reason (
    reason_code    text PRIMARY KEY,   -- QUALITY_COAL, COAL_STOCK_BUILD, SHEAR_PIN_FAILURE, UNIT_TRIP_LOSS, UNIT_STARTUP_LOSS,
                                       -- EQUIP_SYS_CONSTRAINT, ANNUAL_MAINTENANCE, NO_LOAD_DEMAND, FLY_ASH_SLAG
    description    text NOT NULL,
    display_order  smallint NOT NULL
);

CREATE TABLE unit_generation_loss (
    unit_id      text NOT NULL REFERENCES unit,
    loss_date    date NOT NULL REFERENCES dim_date,
    reason_code  text NOT NULL REFERENCES backdown_reason,
    loss_mu      numeric(10,4) NOT NULL CHECK (loss_mu >= 0),
    PRIMARY KEY (unit_id, loss_date, reason_code)
);
CREATE INDEX ON unit_generation_loss (loss_date);

-- Outages & trips
CREATE TABLE outage_reason (
    reason_code      text PRIMARY KEY,  -- TURBINE, ELECTRICAL, GRID_DISTURBANCE, BOILER_TUBE_LEAK, COAL_MILL, ANNUAL_OVERHAUL, NO_DEMAND ...
    description      text NOT NULL,
    plant_type_code  text REFERENCES plant_type          -- NULL = applies to all plant types
);

CREATE TABLE unit_outage (
    outage_id      bigint PRIMARY KEY,
    unit_id        text NOT NULL REFERENCES unit,
    outage_type    text NOT NULL CHECK (outage_type IN ('FORCED','PLANNED','RSD')),
    trip_at        timestamp NOT NULL,                    -- tripped / taken out
    sync_at        timestamp,                             -- re-synchronised; NULL = still out
    outage_hrs     numeric(10,2) CHECK (outage_hrs >= 0), -- closed outages; open ones computed in v_unit_outage
    reason_code    text NOT NULL REFERENCES outage_reason,
    reason_detail  text,
    gen_loss_mu    numeric(12,4) CHECK (gen_loss_mu >= 0),
    is_structural  boolean NOT NULL DEFAULT false,        -- long-term shutdown (e.g. RTPS-U1 since 10-08-2022)
    data_source    text NOT NULL DEFAULT 'KERP',
    CHECK (sync_at IS NULL OR sync_at > trip_at),
    CHECK ((sync_at IS NULL) = (outage_hrs IS NULL))
);
CREATE INDEX ON unit_outage (unit_id, trip_at);
CREATE INDEX ON unit_outage (trip_at);

-- Boiler Tube Failure register (DMS > Boiler Tube Leakage menu); 1:1 with the trip
CREATE TABLE btf_failure_type (
    failure_type_code  text PRIMARY KEY,   -- EROSION_SOOT_BLOWER, EROSION_FALLING_SLAG, WELD_DEFECT, STRESS_RUPTURE_HT_CREEP, OTHERS
    description        text NOT NULL
);

CREATE TABLE btf_location (
    location_code  text PRIMARY KEY,       -- WATER_WALL, RE_HEATER, SUPER_HEATER, STEAM_COOLED_WALL, ECONOMISER
    description    text NOT NULL
);

CREATE TABLE boiler_tube_failure (
    btf_id              bigint PRIMARY KEY,
    outage_id           bigint NOT NULL UNIQUE REFERENCES unit_outage,
    failure_type_code   text NOT NULL REFERENCES btf_failure_type,
    location_code       text NOT NULL REFERENCES btf_location,
    history             text,
    work_details        text,
    inspection_details  text,
    closure_at          timestamp,
    entered_by          text REFERENCES employee
);

-- Monthly generation targets per unit
CREATE TABLE generation_target (
    unit_id             text NOT NULL REFERENCES unit,
    target_month        date NOT NULL CHECK (extract(day FROM target_month) = 1),
    cea_target_mu       numeric(12,3) CHECK (cea_target_mu      >= 0),
    kerc_approved_mu    numeric(12,3) CHECK (kerc_approved_mu   >= 0),
    internal_target_mu  numeric(12,3) CHECK (internal_target_mu >= 0),
    PRIMARY KEY (unit_id, target_month)
);


-- =============================================================================
-- 3. FUEL (COAL)
-- =============================================================================

CREATE TABLE coal_supplier (
    supplier_code  text PRIMARY KEY,                      -- MCL, SCCL, WCL, IMPORTED
    supplier_name  text NOT NULL,
    is_imported    boolean NOT NULL
);

CREATE TABLE coal_receipt (
    receipt_id            bigint PRIMARY KEY,
    station_code          text NOT NULL REFERENCES station,
    receipt_date          date NOT NULL REFERENCES dim_date,
    supplier_code         text NOT NULL REFERENCES coal_supplier,
    no_of_rakes           smallint NOT NULL CHECK (no_of_rakes >= 0),
    quantity_mt           numeric(12,2) NOT NULL CHECK (quantity_mt >= 0),
    gcv_received_kcal_kg  numeric(7,1) CHECK (gcv_received_kcal_kg > 0)
);
CREATE INDEX ON coal_receipt (station_code, receipt_date);

CREATE TABLE station_coal_stock (
    station_code            text NOT NULL REFERENCES station,
    stock_date              date NOT NULL REFERENCES dim_date,
    opening_stock_mt        numeric(14,2) NOT NULL CHECK (opening_stock_mt >= 0),
    receipt_mt              numeric(14,2) NOT NULL DEFAULT 0 CHECK (receipt_mt >= 0),
    indigenous_consumed_mt  numeric(14,2) NOT NULL DEFAULT 0 CHECK (indigenous_consumed_mt >= 0),
    imported_consumed_mt    numeric(14,2) NOT NULL DEFAULT 0 CHECK (imported_consumed_mt   >= 0),
    total_consumed_mt       numeric(14,2) GENERATED ALWAYS AS (indigenous_consumed_mt + imported_consumed_mt) STORED,
    closing_stock_mt        numeric(14,2) GENERATED ALWAYS AS
                              (opening_stock_mt + receipt_mt - indigenous_consumed_mt - imported_consumed_mt) STORED,
    days_of_stock           numeric(6,1),                 -- as reported (closing / normative daily requirement)
    normative_days          smallint,
    PRIMARY KEY (station_code, stock_date)
);


-- =============================================================================
-- 4. HYDROLOGY & STATE GRID
-- =============================================================================

CREATE TABLE reservoir_daily_level (
    reservoir_id          smallint NOT NULL REFERENCES reservoir,
    level_date            date     NOT NULL REFERENCES dim_date,
    water_level           numeric(8,2) NOT NULL,          -- in reservoir.level_unit
    capacity_pct          numeric(5,2) CHECK (capacity_pct BETWEEN 0 AND 100),
    inflow_mcft           numeric(12,2) CHECK (inflow_mcft          >= 0),
    discharge_power_mcft  numeric(12,2) CHECK (discharge_power_mcft >= 0),
    spill_sluice_mcft     numeric(12,2) CHECK (spill_sluice_mcft    >= 0),
    rainfall_mm           numeric(8,2)  CHECK (rainfall_mm          >= 0),
    energy_potential_mu   numeric(10,2) CHECK (energy_potential_mu  >= 0),
    PRIMARY KEY (reservoir_id, level_date)
);

-- Head Office daily data entry: state-level numbers (Karnataka grid)
CREATE TABLE state_grid_daily (
    summary_date          date PRIMARY KEY REFERENCES dim_date,
    state_consumption_mu  numeric(12,4) NOT NULL CHECK (state_consumption_mu >= 0),
    state_generation_mu   numeric(12,4) NOT NULL CHECK (state_generation_mu  >= 0),
    state_import_mu       numeric(12,4) NOT NULL,
    system_peak_mw        numeric(10,2) CHECK (system_peak_mw >= 0),
    system_peak_time      time,
    freq_max_hz           numeric(5,2),
    freq_min_hz           numeric(5,2),
    tb_share_mu           numeric(10,4),                  -- Tungabhadra share
    jurala_share_mu       numeric(10,4),
    cgs_mu                numeric(12,4),                  -- central generating stations
    upcl_mu               numeric(12,4),
    jsw_mu                numeric(12,4),
    others_mu             numeric(12,4),
    remarks               text,
    CHECK (freq_min_hz IS NULL OR freq_max_hz IS NULL OR freq_min_hz <= freq_max_hz)
);


-- =============================================================================
-- 5. METERING / JOINT METER READING (JMR)
-- =============================================================================

CREATE TABLE energy_meter (
    meter_id        integer PRIMARY KEY,
    station_code    text NOT NULL REFERENCES station,
    unit_id         text REFERENCES unit,                 -- NULL for common-aux / station export meters
    meter_no        text NOT NULL,
    meter_role      text NOT NULL CHECK (meter_role IN ('UNIT_GEN','UNIT_AUX','COMMON_AUX','STATION_EXPORT')),
    installed_on    date,
    removed_on      date,
    UNIQUE (station_code, meter_no),
    CHECK ((meter_role IN ('UNIT_GEN','UNIT_AUX')) = (unit_id IS NOT NULL))
);

-- one statement per station per month (the signed Annexure-I)
CREATE TABLE jmr_statement (
    jmr_id              integer PRIMARY KEY,
    station_code        text NOT NULL REFERENCES station,
    jmr_month           date NOT NULL CHECK (extract(day FROM jmr_month) = 1),
    reading_upto_date   date,                              -- NULL = full month; set for a part-month / provisional statement
    net_export_jmr_mu   numeric(12,6) NOT NULL,           -- "Net energy transmitted as per JMR"
    signed_kptcl_by     text,
    signed_escom_by     text,
    signed_kpcl_by      text REFERENCES employee,
    UNIQUE (station_code, jmr_month),
    CHECK (reading_upto_date IS NULL OR reading_upto_date >= jmr_month)
);

CREATE TABLE jmr_meter_reading (
    jmr_id            integer NOT NULL REFERENCES jmr_statement ON DELETE CASCADE,
    meter_id          integer NOT NULL REFERENCES energy_meter,
    initial_reading   numeric(18,4) NOT NULL,
    final_reading     numeric(18,4) NOT NULL,
    meter_constant    numeric(12,6) NOT NULL CHECK (meter_constant > 0),   -- as printed on the JMR
    result_unit       text NOT NULL CHECK (result_unit IN ('MU','KWH')),   -- unit of (difference x constant)
    computed_gen      numeric(18,6) NOT NULL DEFAULT 0,                    -- "Compu. Gen." (meter-failure estimate), in result_unit
    difference        numeric(18,4) GENERATED ALWAYS AS (final_reading - initial_reading) STORED,
    energy_mu         numeric(14,6) GENERATED ALWAYS AS (
                        CASE result_unit
                          WHEN 'MU' THEN (final_reading - initial_reading) * meter_constant + computed_gen
                          ELSE          ((final_reading - initial_reading) * meter_constant + computed_gen) / 1000000
                        END) STORED,
    PRIMARY KEY (jmr_id, meter_id),
    CHECK (final_reading >= initial_reading)
);


-- =============================================================================
-- 6. COMMERCIAL  (Bill preparation · Sale of Energy to ESCOMs · Receipts & RE subsidy)
-- =============================================================================

CREATE TABLE counterparty (
    counterparty_code  text PRIMARY KEY,                  -- BESCOM, MESCOM, CESC, HESCOM, GESCOM, KPTCL, NTPC, SECI, GOK
    counterparty_name  text NOT NULL,
    counterparty_type  text NOT NULL CHECK (counterparty_type IN ('ESCOM','TRANSCO','CENTRAL_PSU','SECI','GOVERNMENT')),
    gstin              text,
    pan                text
);

-- pools whose energy is allocated to ESCOMs in fixed shares (bill header rows)
CREATE TABLE allocation_pool (
    pool_code    text PRIMARY KEY,                        -- SVP, KVP, OTHER_HYDRO, RTPS_U1_7, RTPS_U8, BTPS_U1_3 ...
    description  text NOT NULL
);

CREATE TABLE escom_share (
    pool_code          text NOT NULL REFERENCES allocation_pool,
    counterparty_code  text NOT NULL REFERENCES counterparty,
    valid_from         date NOT NULL,
    valid_to           date,
    share_pct          numeric(7,4) NOT NULL CHECK (share_pct BETWEEN 0 AND 100),
    PRIMARY KEY (pool_code, counterparty_code, valid_from),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- a billable line on the ESCOM bill ("Sharavathy Valley Project", "RTPS Unit 5 & 6" ...)
CREATE TABLE billing_group (
    billing_group_id    integer PRIMARY KEY,
    billing_group_name  text NOT NULL UNIQUE,
    pool_code           text NOT NULL REFERENCES allocation_pool,
    bill_section        text NOT NULL CHECK (bill_section IN ('HYDEL_RES','THERMAL')),
    display_order       smallint NOT NULL
);

CREATE TABLE billing_group_member (
    billing_group_id  integer NOT NULL REFERENCES billing_group,
    station_code      text    NOT NULL REFERENCES station,
    unit_id           text    REFERENCES unit,            -- NULL = whole station
    UNIQUE NULLS NOT DISTINCT (billing_group_id, station_code, unit_id)
);

CREATE TABLE tariff_rate (
    billing_group_id              integer NOT NULL REFERENCES billing_group,
    valid_from                    date NOT NULL,
    valid_to                      date,
    capacity_charge_rs_month      numeric(16,2) CHECK (capacity_charge_rs_month      >= 0),
    primary_energy_rate_rs_kwh    numeric(8,4)  CHECK (primary_energy_rate_rs_kwh    >= 0),
    secondary_energy_rate_rs_kwh  numeric(8,4)  CHECK (secondary_energy_rate_rs_kwh  >= 0),
    royalty_rate_rs_kwh           numeric(8,4)  CHECK (royalty_rate_rs_kwh           >= 0),
    kerc_order_ref                text,
    PRIMARY KEY (billing_group_id, valid_from)
);

CREATE TABLE energy_bill (
    bill_id          integer PRIMARY KEY,
    bill_no          text NOT NULL UNIQUE,
    bill_month       date NOT NULL CHECK (extract(day FROM bill_month) = 1),
    bill_type        text NOT NULL CHECK (bill_type IN ('INITIAL','REVISED','SUPPLEMENTARY','NOTIONAL')),
    bill_date        date NOT NULL,
    status           text NOT NULL DEFAULT 'DRAFT'
                       CHECK (status IN ('DRAFT','AO_CHECKED','AGM_CHECKED','GM_APPROVED','JE_POSTED')),
    ao_checked_by    text REFERENCES employee,  ao_checked_at   timestamp,
    agm_checked_by   text REFERENCES employee,  agm_checked_at  timestamp,
    gm_approved_by   text REFERENCES employee,  gm_approved_at  timestamp,
    je_no            text,
    je_date          date,
    UNIQUE (bill_month, bill_type, bill_date)
);

CREATE TABLE energy_bill_line (
    bill_line_id           integer PRIMARY KEY,
    bill_id                integer NOT NULL REFERENCES energy_bill ON DELETE CASCADE,
    billing_group_id       integer NOT NULL REFERENCES billing_group,
    gross_generation_kwh   numeric(16,2) CHECK (gross_generation_kwh >= 0),
    net_transmission_kwh   numeric(16,2) NOT NULL CHECK (net_transmission_kwh >= 0),
    capacity_charges_rs    numeric(16,2),                  -- breakdown may be unavailable (source shows total only); use total_amount_rs
    primary_charges_rs     numeric(16,2),
    secondary_charges_rs   numeric(16,2),
    royalty_charges_rs     numeric(16,2),
    total_amount_rs        numeric(16,2) NOT NULL,          -- always populated; = sum of the four charges when they are known
    UNIQUE (bill_id, billing_group_id),
    CHECK (num_nonnulls(capacity_charges_rs, primary_charges_rs, secondary_charges_rs, royalty_charges_rs) IN (0, 4)),
    CHECK (capacity_charges_rs IS NULL           -- either the full breakdown is present, or none of it is
           OR total_amount_rs = capacity_charges_rs + primary_charges_rs + secondary_charges_rs + royalty_charges_rs)
);

CREATE TABLE energy_bill_escom_split (
    bill_line_id       integer NOT NULL REFERENCES energy_bill_line ON DELETE CASCADE,
    counterparty_code  text    NOT NULL REFERENCES counterparty,
    share_pct          numeric(7,4)  NOT NULL CHECK (share_pct BETWEEN 0 AND 100),
    transmitted_kwh    numeric(16,2) NOT NULL CHECK (transmitted_kwh >= 0),
    amount_rs          numeric(16,2) NOT NULL,
    PRIMARY KEY (bill_line_id, counterparty_code)
);

CREATE TABLE commercial_txn_type (
    txn_type_code  text PRIMARY KEY,   -- ESCOM_BILL_CLAIM, ESCOM_BILL_WITHDRAWAL, RECEIPT, RE_SUBSIDY_GOK, SECI_RECEIPT
    description    text NOT NULL
);

-- money in / claims against counterparties (Receipts, Sales & RE Subsidy screen)
CREATE TABLE commercial_transaction (
    txn_id             bigint PRIMARY KEY,
    txn_date           date NOT NULL REFERENCES dim_date,
    txn_type_code      text NOT NULL REFERENCES commercial_txn_type,
    counterparty_code  text NOT NULL REFERENCES counterparty,
    station_code       text REFERENCES station,           -- e.g. SECI solar project
    bill_id            integer REFERENCES energy_bill,
    receipt_sub_type   text,                              -- the "Type" drop-down on the screen
    amount_rs          numeric(16,2) NOT NULL,
    tds_rs             numeric(16,2) NOT NULL DEFAULT 0,
    rebate_rs          numeric(16,2) NOT NULL DEFAULT 0,
    writeoff_rs        numeric(16,2) NOT NULL DEFAULT 0,
    reference_no       text,
    remarks            text
);
CREATE INDEX ON commercial_transaction (counterparty_code, txn_date);

-- GoK tripartite adjustment of ESCOM dues
CREATE TABLE tripartite_adjustment (
    adjustment_id            integer PRIMARY KEY,
    counterparty_code        text NOT NULL REFERENCES counterparty,
    adjustment_date          date NOT NULL REFERENCES dim_date,
    go_reference             text,
    royalty_rs               numeric(16,2) NOT NULL DEFAULT 0,
    electricity_tax_rs       numeric(16,2) NOT NULL DEFAULT 0,
    guarantee_commission_rs  numeric(16,2) NOT NULL DEFAULT 0,
    total_rs                 numeric(16,2) GENERATED ALWAYS AS
                               (royalty_rs + electricity_tax_rs + guarantee_commission_rs) STORED,
    is_late_release_by_gok   boolean NOT NULL DEFAULT false
);

CREATE TABLE seci_calibration_charge (
    station_code  text NOT NULL REFERENCES station,
    charge_month  date NOT NULL CHECK (extract(day FROM charge_month) = 1),
    amount_rs     numeric(14,2) NOT NULL CHECK (amount_rs >= 0),
    gst_pct       numeric(5,2)  NOT NULL CHECK (gst_pct BETWEEN 0 AND 100),
    total_rs      numeric(14,2) GENERATED ALWAYS AS (amount_rs * (1 + gst_pct / 100)) STORED,
    PRIMARY KEY (station_code, charge_month)
);


-- =============================================================================
-- 7. DMS - DEFECT MANAGEMENT
-- =============================================================================

CREATE TABLE asset (
    asset_id         integer PRIMARY KEY,
    floc_code        text NOT NULL UNIQUE,                -- functional location
    asset_name       text NOT NULL,
    station_code     text NOT NULL REFERENCES station,
    unit_id          text REFERENCES unit,                -- NULL = common / station asset
    parent_asset_id  integer REFERENCES asset,
    asset_type       text
);

CREATE TABLE defect (
    defect_id            bigint PRIMARY KEY,
    station_code         text NOT NULL REFERENCES station,
    unit_id              text REFERENCES unit,            -- NULL = common (unit "0" in K-eRP)
    asset_id             integer REFERENCES asset,
    raised_by_division   text NOT NULL REFERENCES division,
    attending_division   text REFERENCES division,
    logged_by            text NOT NULL REFERENCES employee,
    logged_at            timestamp NOT NULL,
    priority             text NOT NULL CHECK (priority IN ('IMMEDIATELY','HIGH','NORMAL','LOW')),
    defect_type          text NOT NULL CHECK (defect_type IN ('MAINTENANCE','OPERATION','SAFETY','CIVIL','OTHER')),
    description          text NOT NULL,
    status               text NOT NULL CHECK (status IN
                           ('INITIATED','ACKNOWLEDGED','ASSIGNED','DIVERTED','IN_PROGRESS','COMPLETED','ACCEPTED','REJECTED')),
    acknowledged_at      timestamp,
    completed_at         timestamp,
    closed_at            timestamp,
    ptw_required         boolean NOT NULL DEFAULT false,
    reason               text
);
CREATE INDEX ON defect (station_code, logged_at);

CREATE TABLE permit_to_work (
    ptw_id       bigint PRIMARY KEY,
    defect_id    bigint NOT NULL REFERENCES defect,
    issued_by    text REFERENCES employee,
    issued_at    timestamp NOT NULL,
    received_by  text REFERENCES employee,
    returned_at  timestamp,
    status       text NOT NULL CHECK (status IN ('ISSUED','RETURNED','CANCELLED')),
    CHECK (returned_at IS NULL OR returned_at >= issued_at)
);

CREATE TABLE line_clearance (
    lc_id          bigint PRIMARY KEY,
    station_code   text NOT NULL REFERENCES station,
    asset_id       integer REFERENCES asset,
    defect_id      bigint REFERENCES defect,
    requested_by   text REFERENCES employee,
    requested_at   timestamp NOT NULL,
    forwarded_at   timestamp,
    approved_by    text,                                  -- LD Centre official
    approved_at    timestamp,
    returned_at    timestamp,
    status         text NOT NULL CHECK (status IN ('INITIATED','FORWARDED','APPROVED','REJECTED','RETURNED')),
    remarks        text
);


-- =============================================================================
-- 8. REPORTING VIEWS  -  the preferred NL2SQL surface
--    Ratios are SUM/SUM, never AVG of daily percentages.
-- =============================================================================

-- unit x day, with station / category / calendar attributes and daily KPIs
CREATE VIEW v_unit_daily AS
SELECT d.unit_id, u.unit_no, u.station_code, s.station_name, s.plant_category_code, pc.plant_type_code,
       u.capacity_mw, u.fuel_code,
       d.gen_date, c.fy, c.water_year, c.month_start, c.year_month, c.month_label, c.fy_quarter, c.season,
       c.is_weekend, c.is_holiday,
       d.gross_gen_mu, d.aux_consumption_mu, d.net_export_mu, d.deemed_gen_mu,
       d.peak_hour_gen_mu, d.peak_hour_gen_morning_mu, d.peak_hour_gen_evening_mu, d.peak_load_mw, d.peak_load_time,
       d.running_hrs, d.forced_outage_hrs, d.planned_outage_hrs, d.rsd_hrs, d.trips,
       d.coal_consumed_mt, d.hfo_consumed_kl, d.ldo_consumed_kl, d.gas_consumed_mmscm, d.msw_consumed_mt,
       d.gcv_coal_kcal_kg, d.gcv_oil_kcal_l, d.gross_heat_rate_kcal_kwh,
       d.dm_water_cum, d.h2_cylinders_nos, d.water_used_mcft, d.declared_capacity_mu,
       u.capacity_mw * 24 / 1000.0                                                AS capacity_mu,
       round(d.gross_gen_mu / (u.capacity_mw * 24 / 1000.0) * 100, 2)             AS plf_pct,
       round(d.declared_capacity_mu / (u.capacity_mw * 24 / 1000.0) * 100, 2)     AS paf_pct,
       round(d.aux_consumption_mu / NULLIF(d.gross_gen_mu, 0) * 100, 2)          AS aux_pct,
       round(d.coal_consumed_mt / 1000 / NULLIF(d.gross_gen_mu, 0), 3)           AS sp_coal_kg_kwh,
       round((d.hfo_consumed_kl + d.ldo_consumed_kl) / NULLIF(d.gross_gen_mu, 0), 3) AS sp_oil_ml_kwh
FROM unit_daily_generation d
JOIN unit u            ON u.unit_id = d.unit_id
JOIN station s         ON s.station_code = u.station_code
JOIN plant_category pc ON pc.plant_category_code = s.plant_category_code
JOIN dim_date c        ON c.cal_date = d.gen_date;

-- station x day (HO Daily Generation Report / station daily report totals)
CREATE VIEW v_station_daily AS
SELECT g.station_code, s.station_name, s.plant_category_code, s.installed_capacity_mw, s.report_order,
       g.gen_date, c.fy, c.water_year, c.month_start, c.year_month, c.month_label, c.season,
       g.gross_gen_mu,
       g.unit_aux_mu,
       COALESCE(x.common_aux_mu, 0)                                           AS common_aux_mu,
       g.unit_aux_mu + COALESCE(x.common_aux_mu, 0)                           AS total_aux_mu,
       g.gross_gen_mu - g.unit_aux_mu - COALESCE(x.common_aux_mu, 0)          AS net_export_mu,
       g.peak_hour_gen_mu, g.deemed_gen_mu,
       g.running_hrs, g.forced_outage_hrs, g.planned_outage_hrs, g.rsd_hrs, g.trips,
       g.coal_consumed_mt, g.hfo_consumed_kl, g.ldo_consumed_kl, g.dm_water_cum, g.water_used_mcft,
       x.colony_consumption_kwh, x.local_feeder_kwh,
       g.capacity_mu, g.declared_capacity_mu,
       round(g.gross_gen_mu / NULLIF(g.capacity_mu, 0) * 100, 2)                          AS plf_pct,
       round(g.declared_capacity_mu / NULLIF(g.capacity_mu, 0) * 100, 2)                  AS paf_pct,
       round((g.unit_aux_mu + COALESCE(x.common_aux_mu, 0)) / NULLIF(g.gross_gen_mu, 0) * 100, 2) AS aux_pct,
       round(g.coal_consumed_mt / 1000 / NULLIF(g.gross_gen_mu, 0), 3)                    AS sp_coal_kg_kwh
FROM (
    SELECT u.station_code, d.gen_date,
           sum(d.gross_gen_mu) gross_gen_mu, sum(d.aux_consumption_mu) unit_aux_mu,
           sum(d.peak_hour_gen_mu) peak_hour_gen_mu, sum(d.deemed_gen_mu) deemed_gen_mu,
           sum(d.running_hrs) running_hrs, sum(d.forced_outage_hrs) forced_outage_hrs,
           sum(d.planned_outage_hrs) planned_outage_hrs, sum(d.rsd_hrs) rsd_hrs, sum(d.trips) trips,
           sum(d.coal_consumed_mt) coal_consumed_mt, sum(d.hfo_consumed_kl) hfo_consumed_kl,
           sum(d.ldo_consumed_kl) ldo_consumed_kl, sum(d.dm_water_cum) dm_water_cum,
           sum(d.water_used_mcft) water_used_mcft, sum(d.declared_capacity_mu) declared_capacity_mu,
           sum(u.capacity_mw * 24 / 1000.0) capacity_mu
    FROM unit_daily_generation d JOIN unit u ON u.unit_id = d.unit_id
    GROUP BY u.station_code, d.gen_date
) g
JOIN station s  ON s.station_code = g.station_code
JOIN dim_date c ON c.cal_date = g.gen_date
LEFT JOIN station_daily_consumption x ON x.station_code = g.station_code AND x.gen_date = g.gen_date;

-- unit x month = the unit columns of the Monthly Performance Report (MPR)
CREATE VIEW v_unit_monthly AS
SELECT u.station_code, d.unit_id, c.fy, c.month_start, c.year_month, c.month_label,
       sum(d.gross_gen_mu)        gross_gen_mu,
       sum(d.aux_consumption_mu)  aux_consumption_mu,
       sum(d.net_export_mu)       net_export_mu,
       sum(d.deemed_gen_mu)       deemed_gen_mu,
       max(d.gross_gen_mu)        max_day_gen_mu,
       CASE WHEN max(d.gross_gen_mu) > 0
            THEN (array_agg(d.gen_date ORDER BY d.gross_gen_mu DESC))[1] END AS max_day_gen_date,
       max(d.peak_load_mw)        peak_load_mw,
       sum(d.peak_hour_gen_morning_mu) peak_hour_gen_morning_mu,
       sum(d.peak_hour_gen_evening_mu) peak_hour_gen_evening_mu,
       sum(d.running_hrs) running_hrs, sum(d.forced_outage_hrs) forced_outage_hrs,
       sum(d.planned_outage_hrs) planned_outage_hrs, sum(d.rsd_hrs) rsd_hrs, sum(d.trips) trips,
       sum(d.coal_consumed_mt) coal_consumed_mt, sum(d.hfo_consumed_kl) hfo_consumed_kl, sum(d.ldo_consumed_kl) ldo_consumed_kl,
       sum(d.dm_water_cum) dm_water_cum, sum(d.water_used_mcft) water_used_mcft,
       sum(u.capacity_mw * 24 / 1000.0) capacity_mu,
       round(sum(d.gross_gen_mu) / sum(u.capacity_mw * 24 / 1000.0) * 100, 2)                      AS plf_pct,
       round(sum(d.declared_capacity_mu) / sum(u.capacity_mw * 24 / 1000.0) * 100, 2)              AS paf_pct,
       round((sum(d.gross_gen_mu) + sum(d.deemed_gen_mu)) / sum(u.capacity_mw * 24 / 1000.0) * 100, 2) AS plf_with_nld_pct,
       round(sum(d.aux_consumption_mu) / NULLIF(sum(d.gross_gen_mu), 0) * 100, 2)                 AS aux_pct,
       round(sum(d.coal_consumed_mt) / 1000 / NULLIF(sum(d.gross_gen_mu), 0), 3)                  AS sp_coal_kg_kwh,
       round((sum(d.hfo_consumed_kl) + sum(d.ldo_consumed_kl)) / NULLIF(sum(d.gross_gen_mu), 0), 3) AS sp_oil_ml_kwh,
       round(sum(d.gcv_coal_kcal_kg * d.coal_consumed_mt) / NULLIF(sum(d.coal_consumed_mt) FILTER (WHERE d.gcv_coal_kcal_kg IS NOT NULL), 0)) AS wa_gcv_coal_kcal_kg,
       round(sum(d.gross_heat_rate_kcal_kwh * d.gross_gen_mu) / NULLIF(sum(d.gross_gen_mu) FILTER (WHERE d.gross_heat_rate_kcal_kwh IS NOT NULL), 0)) AS wa_gross_heat_rate_kcal_kwh
FROM unit_daily_generation d
JOIN unit u     ON u.unit_id = d.unit_id
JOIN dim_date c ON c.cal_date = d.gen_date
GROUP BY u.station_code, d.unit_id, c.fy, c.month_start, c.year_month, c.month_label;

-- station x month = station column of the MPR, plus targets
CREATE VIEW v_station_monthly AS
SELECT sd.station_code, sd.station_name, sd.plant_category_code, sd.fy, sd.month_start, sd.year_month, sd.month_label,
       sum(sd.gross_gen_mu)   gross_gen_mu,
       sum(sd.total_aux_mu)   aux_consumption_mu,
       sum(sd.net_export_mu)  net_export_mu,
       sum(sd.deemed_gen_mu)  deemed_gen_mu,
       max(sd.gross_gen_mu)   max_day_gen_mu,
       sum(sd.forced_outage_hrs) forced_outage_hrs, sum(sd.planned_outage_hrs) planned_outage_hrs,
       sum(sd.running_hrs) running_hrs, sum(sd.rsd_hrs) rsd_hrs, sum(sd.trips) trips,
       sum(sd.coal_consumed_mt) coal_consumed_mt, sum(sd.hfo_consumed_kl) hfo_consumed_kl, sum(sd.ldo_consumed_kl) ldo_consumed_kl,
       sum(sd.water_used_mcft) water_used_mcft,
       sum(sd.capacity_mu) capacity_mu, sum(sd.declared_capacity_mu) declared_capacity_mu,
       round(sum(sd.gross_gen_mu) / sum(sd.capacity_mu) * 100, 2)                      AS plf_pct,
       round(sum(sd.declared_capacity_mu) / sum(sd.capacity_mu) * 100, 2)              AS paf_pct,
       round(sum(sd.total_aux_mu) / NULLIF(sum(sd.gross_gen_mu), 0) * 100, 2)          AS aux_pct,
       round(sum(sd.coal_consumed_mt) / 1000 / NULLIF(sum(sd.gross_gen_mu), 0), 3)     AS sp_coal_kg_kwh,
       t.cea_target_mu, t.kerc_approved_mu, t.internal_target_mu
FROM v_station_daily sd
LEFT JOIN (
    SELECT u.station_code, g.target_month,
           sum(g.cea_target_mu) cea_target_mu, sum(g.kerc_approved_mu) kerc_approved_mu, sum(g.internal_target_mu) internal_target_mu
    FROM generation_target g JOIN unit u ON u.unit_id = g.unit_id
    GROUP BY u.station_code, g.target_month
) t ON t.station_code = sd.station_code AND t.target_month = sd.month_start
GROUP BY sd.station_code, sd.station_name, sd.plant_category_code, sd.fy, sd.month_start, sd.year_month, sd.month_label,
         t.cea_target_mu, t.kerc_approved_mu, t.internal_target_mu;

-- station x financial year
CREATE VIEW v_station_fy AS
SELECT station_code, station_name, plant_category_code, fy,
       sum(gross_gen_mu) gross_gen_mu, sum(aux_consumption_mu) aux_consumption_mu, sum(net_export_mu) net_export_mu,
       sum(deemed_gen_mu) deemed_gen_mu, sum(forced_outage_hrs) forced_outage_hrs, sum(planned_outage_hrs) planned_outage_hrs,
       sum(trips) trips, sum(coal_consumed_mt) coal_consumed_mt, sum(hfo_consumed_kl) hfo_consumed_kl,
       round(sum(gross_gen_mu) / sum(capacity_mu) * 100, 2)                        AS plf_pct,
       round(sum(declared_capacity_mu) / sum(capacity_mu) * 100, 2)                AS paf_pct,
       round(sum(aux_consumption_mu) / NULLIF(sum(gross_gen_mu), 0) * 100, 2)      AS aux_pct,
       round(sum(coal_consumed_mt) / 1000 / NULLIF(sum(gross_gen_mu), 0), 3)       AS sp_coal_kg_kwh,
       sum(cea_target_mu) cea_target_mu, sum(internal_target_mu) internal_target_mu
FROM v_station_monthly
GROUP BY station_code, station_name, plant_category_code, fy;

-- open and closed outages with live duration; BTF details attached
CREATE VIEW v_unit_outage AS
SELECT o.outage_id, o.unit_id, u.station_code, o.outage_type, o.trip_at, o.sync_at,
       (o.sync_at IS NULL) AS is_open,
       COALESCE(o.outage_hrs, round(extract(epoch FROM (localtimestamp - o.trip_at)) / 3600, 2)) AS outage_hrs,
       o.reason_code, r.description AS reason_description, o.reason_detail, o.gen_loss_mu, o.is_structural,
       c.fy, c.month_start,
       b.btf_id IS NOT NULL AS is_btf, b.failure_type_code, b.location_code,
       b.history, b.work_details, b.inspection_details, b.closure_at
FROM unit_outage o
JOIN unit u           ON u.unit_id = o.unit_id
JOIN outage_reason r  ON r.reason_code = o.reason_code
JOIN dim_date c       ON c.cal_date = o.trip_at::date
LEFT JOIN boiler_tube_failure b ON b.outage_id = o.outage_id;

-- "Unit Status" line of the daily report: running since / force tripped since
CREATE VIEW v_unit_current_status AS
SELECT u.unit_id, u.station_code, u.capacity_mw,
       CASE WHEN u.decommissioning_date IS NOT NULL AND u.decommissioning_date <= current_date THEN 'DECOMMISSIONED'
            WHEN o.outage_id IS NOT NULL THEN o.outage_type || '_OUTAGE'
            ELSE 'RUNNING' END                                       AS status,
       COALESCE(o.trip_at, last_sync.sync_at, u.commissioning_date)  AS status_since,
       o.reason_code, o.reason_detail
FROM unit u
LEFT JOIN LATERAL (SELECT * FROM unit_outage x WHERE x.unit_id = u.unit_id AND x.sync_at IS NULL
                   ORDER BY x.trip_at DESC LIMIT 1) o ON true
LEFT JOIN LATERAL (SELECT max(x.sync_at) sync_at FROM unit_outage x WHERE x.unit_id = u.unit_id) last_sync ON true;

-- "Cumulative data since unit's first synchronisation"
CREATE VIEW v_unit_cumulative AS
SELECT u.unit_id, u.station_code, u.commissioning_date,
       u.opening_cum_gen_mu      + COALESCE(sum(d.gross_gen_mu), 0) AS cum_gen_mu,
       u.opening_cum_running_hrs + COALESCE(sum(d.running_hrs), 0)  AS cum_running_hrs,
       u.opening_cum_trips       + COALESCE(sum(d.trips), 0)        AS cum_trips,
       max(d.gen_date)                                               AS as_on_date
FROM unit u
LEFT JOIN unit_daily_generation d
       ON d.unit_id = u.unit_id AND (u.opening_balance_date IS NULL OR d.gen_date > u.opening_balance_date)
GROUP BY u.unit_id, u.station_code, u.commissioning_date,
         u.opening_cum_gen_mu, u.opening_cum_running_hrs, u.opening_cum_trips;

-- JMR vs GMS reconciliation (billing net vs operational net)
CREATE VIEW v_jmr_reconciliation AS
SELECT j.station_code, j.jmr_month,
       sum(r.energy_mu) FILTER (WHERE m.meter_role = 'UNIT_GEN')                 AS jmr_gross_gen_mu,
       sum(r.energy_mu) FILTER (WHERE m.meter_role IN ('UNIT_AUX','COMMON_AUX')) AS jmr_aux_mu,
       j.net_export_jmr_mu,
       g.gross_gen_mu  AS gms_gross_gen_mu,
       g.net_export_mu AS gms_net_export_mu,
       j.net_export_jmr_mu - g.net_export_mu AS net_diff_mu
FROM jmr_statement j
JOIN jmr_meter_reading r ON r.jmr_id = j.jmr_id
JOIN energy_meter m      ON m.meter_id = r.meter_id
LEFT JOIN v_station_monthly g ON g.station_code = j.station_code AND g.month_start = j.jmr_month
GROUP BY j.station_code, j.jmr_month, j.net_export_jmr_mu, g.gross_gen_mu, g.net_export_mu;

-- reservoir x day with the power houses it feeds and headroom above MDDL
CREATE VIEW v_reservoir_daily AS
SELECT l.reservoir_id, r.reservoir_name, l.level_date, c.fy, c.water_year, c.season,
       l.water_level, r.level_unit, r.full_reservoir_level, r.min_draw_down_level,
       l.water_level - r.min_draw_down_level AS level_above_mddl,
       l.capacity_pct, l.inflow_mcft, l.discharge_power_mcft, l.spill_sluice_mcft, l.rainfall_mm, l.energy_potential_mu,
       (SELECT string_agg(rs.station_code, ',' ORDER BY rs.station_code)
          FROM reservoir_station rs WHERE rs.reservoir_id = l.reservoir_id) AS feeds_stations
FROM reservoir_daily_level l
JOIN reservoir r ON r.reservoir_id = l.reservoir_id
JOIN dim_date c  ON c.cal_date = l.level_date;


-- =============================================================================
-- NL2SQL metadata: comments are read by the model when building the prompt
-- =============================================================================
COMMENT ON SCHEMA gms IS 'KPCL Generation Management System. Energy in MU (million kWh). FY = Apr-Mar, water year = Jun-May.';
COMMENT ON TABLE station IS 'Power station (called "project" in K-eRP reports). 33 stations: thermal, gas, WtE, hydro, wind, solar.';
COMMENT ON TABLE unit IS 'Generating unit of a station. unit_id = station_code || ''-U'' || unit_no.';
COMMENT ON TABLE dim_date IS 'Calendar. Join any date column here for fy, water_year, month_label, season.';
COMMENT ON TABLE unit_daily_generation IS 'One row per unit per day: generation, hours, trips, fuel, performance. Station totals: v_station_daily.';
COMMENT ON COLUMN unit_daily_generation.rsd_hrs IS 'Reserve shut down hours (unit available but not required). For hydro this is idle hours.';
COMMENT ON COLUMN unit_daily_generation.deemed_gen_mu IS 'NLD: deemed generation lost to no-load-demand back-down; used in "PLF with NLD".';
COMMENT ON COLUMN unit_daily_generation.declared_capacity_mu IS 'Declared capacity for the day; PAF % = declared_capacity_mu / (capacity_mw*24/1000).';
COMMENT ON TABLE station_daily_consumption IS 'Station-level common auxiliary, colony and local-feeder consumption. Station net = sum(unit net) - common_aux.';
COMMENT ON TABLE unit_generation_loss IS 'Thermal/gas back-down: MU lost per unit per day by reason (reasons for back down generation).';
COMMENT ON TABLE unit_outage IS 'Every trip or shutdown of a unit. sync_at NULL = unit still out. Use v_unit_outage for live duration and BTF details.';
COMMENT ON TABLE boiler_tube_failure IS 'Boiler Tube Failure (BTF/BTL) register; exactly one per BTF outage.';
COMMENT ON TABLE generation_target IS 'Monthly generation target per unit (CEA, KERC-approved, internal). target_month = first day of month.';
COMMENT ON TABLE station_coal_stock IS 'Daily coal stock balance per thermal station. closing = opening + receipt - consumed.';
COMMENT ON TABLE coal_receipt IS 'Coal rakes received per station per day by supplier, with GCV as received.';
COMMENT ON TABLE reservoir_daily_level IS 'Daily dam level, storage %, inflow, discharge, spill and rainfall.';
COMMENT ON TABLE reservoir_station IS 'Which reservoir feeds which hydro station.';
COMMENT ON TABLE state_grid_daily IS 'Karnataka state grid daily figures entered at Head Office: consumption, import, peak, frequency, shares.';
COMMENT ON TABLE energy_meter IS 'Energy meters used for Joint Meter Reading, by role (unit generation, unit aux, common aux, station export).';
COMMENT ON TABLE jmr_statement IS 'Monthly Joint Meter Reading statement (Annexure-I) per station, signed by KPTCL, ESCOM and KPCL.';
COMMENT ON TABLE jmr_meter_reading IS 'Initial/final meter reading per meter in a JMR statement. energy_mu is normalised to MU.';
COMMENT ON TABLE energy_bill IS 'Monthly bill for sale of energy to ESCOMs, with approval workflow and journal entry.';
COMMENT ON TABLE energy_bill_line IS 'One billing group (e.g. Sharavathy Valley Project, RTPS Unit 5 & 6) on a bill, with charges.';
COMMENT ON TABLE energy_bill_escom_split IS 'Split of a bill line across ESCOMs by allocation share.';
COMMENT ON TABLE commercial_transaction IS 'Claims, withdrawals, receipts and RE subsidy against ESCOMs, SECI, NTPC, KPTCL, GoK.';
COMMENT ON TABLE defect IS 'DMS defect logged against a station / unit / asset, with workflow status.';
COMMENT ON VIEW v_unit_daily IS 'Preferred for unit-level daily questions. Includes fy, month_label, season and daily PLF/PAF/aux%/specific coal.';
COMMENT ON VIEW v_station_daily IS 'Preferred for station-level daily questions (HO Daily Generation Report). Net export includes common aux.';
COMMENT ON VIEW v_unit_monthly IS 'Unit Monthly Performance Report (MPR): monthly totals and correctly weighted ratios.';
COMMENT ON VIEW v_station_monthly IS 'Station Monthly Performance Report with CEA/KERC/internal targets.';
COMMENT ON VIEW v_station_fy IS 'Station totals per financial year.';
COMMENT ON VIEW v_unit_current_status IS 'Current status of each unit (running / forced outage / planned outage) and since when.';
COMMENT ON VIEW v_unit_cumulative IS 'Cumulative generation, running hours and trips since first synchronisation.';
COMMENT ON VIEW v_jmr_reconciliation IS 'JMR (billing) energy vs GMS (operational) energy per station-month.';
