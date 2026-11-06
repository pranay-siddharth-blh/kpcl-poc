-- =============================================================================
--  KPCL GMS · core layer · SQLite translation of gms_schema.sql sections 1-7
--  Generated once from gms_schema.sql (the PostgreSQL design, left unchanged)
--  and reviewed by hand. Every table is prefixed core_. Types: REAL for
--  numeric, INTEGER for ints and booleans (0/1), TEXT for dates ('YYYY-MM-DD'),
--  timestamps ('YYYY-MM-DD HH:MM:SS') and times. Column-suffix units are as in
--  gms_schema.sql (_mu = million kWh, _pct = 0-100, ...).
--  Only 23 of these 47 tables are loaded from the workbook; the rest are kept
--  so billing / JMR / DMS data can be loaded later without a schema change.
-- =============================================================================
-- =============================================================================

CREATE TABLE core_company (
    company_code        TEXT PRIMARY KEY,                 -- KPCL, RPCL, KPCGPCL
    company_name        TEXT NOT NULL
);

CREATE TABLE core_plant_type (
    plant_type_code     TEXT PRIMARY KEY,                 -- THERMAL, GAS, WTE, HYDRO, WIND, SOLAR
    description         TEXT NOT NULL
);

CREATE TABLE core_plant_category (
    plant_category_code TEXT PRIMARY KEY,                 -- THERMAL-COAL, GAS, WTE, MAJOR HYDRO, MINOR HYDRO, WIND, SOLAR
    plant_type_code     TEXT NOT NULL REFERENCES core_plant_type,
    dashboard_label     TEXT NOT NULL,                    -- label on the HO "Abstract Generation Details" panel
    dashboard_order     INTEGER NOT NULL
);

CREATE TABLE core_station (
    station_code          TEXT PRIMARY KEY,               -- RTPS, BTPS, SGS ... (the "Project" in K-eRP reports)
    station_name          TEXT NOT NULL UNIQUE,
    company_code          TEXT NOT NULL REFERENCES core_company,
    plant_category_code   TEXT NOT NULL REFERENCES core_plant_category,
    district              TEXT,
    installed_capacity_mw REAL NOT NULL CHECK (installed_capacity_mw > 0),
    commissioning_date    TEXT,
    report_order          INTEGER NOT NULL UNIQUE,       -- row order of the HO Daily Generation Report
    is_active             INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE core_fuel (
    fuel_code           TEXT PRIMARY KEY,                 -- COAL, GAS, MSW, WATER, WIND, SUN
    description         TEXT NOT NULL
);

CREATE TABLE core_unit (
    unit_id                    TEXT PRIMARY KEY,          -- '<station_code>-U<n>', e.g. RTPS-U8
    station_code               TEXT NOT NULL REFERENCES core_station,
    unit_no                    INTEGER NOT NULL CHECK (unit_no > 0),
    capacity_mw                REAL NOT NULL CHECK (capacity_mw > 0),
    fuel_code                  TEXT NOT NULL REFERENCES core_fuel,
    technology                 TEXT NOT NULL,             -- SUBCRITICAL, SUPERCRITICAL, CCGT, WTE, FRANCIS, WTG, PV
    commissioning_date         TEXT NOT NULL,             -- first synchronisation
    decommissioning_date       TEXT,
    design_heat_rate_kcal_kwh  REAL,
    opening_balance_date       TEXT,                      -- cumulative figures below are "as on" this date
    opening_cum_gen_mu         REAL,             -- NULL = opening balance not known (cumulative view returns NULL)
    opening_cum_running_hrs    REAL,
    opening_cum_trips          INTEGER,
    UNIQUE (station_code, unit_no),
    CHECK (decommissioning_date IS NULL OR decommissioning_date > commissioning_date)
);

CREATE TABLE core_dim_date (
    cal_date      TEXT PRIMARY KEY,
    fy            TEXT     NOT NULL,                      -- financial year Apr-Mar, '2024-25'
    fy_start_date TEXT     NOT NULL,
    fy_month_no   INTEGER NOT NULL CHECK (fy_month_no BETWEEN 1 AND 12),   -- Apr = 1
    water_year    TEXT     NOT NULL,                      -- hydro water year Jun-May, '2024-25'
    wy_month_no   INTEGER NOT NULL CHECK (wy_month_no BETWEEN 1 AND 12),   -- Jun = 1
    month_start   TEXT     NOT NULL,
    year_month    TEXT     NOT NULL,                      -- '2025-03'
    month_label   TEXT     NOT NULL,                      -- 'MAR-2025' (K-eRP style)
    fy_quarter    TEXT     NOT NULL CHECK (fy_quarter IN ('Q1','Q2','Q3','Q4')),
    week_start    TEXT     NOT NULL,
    day_name      TEXT     NOT NULL,
    is_weekend    INTEGER  NOT NULL,
    is_holiday    INTEGER  NOT NULL DEFAULT 0,
    holiday_name  TEXT,
    season        TEXT     NOT NULL CHECK (season IN ('SUMMER','MONSOON','POST_MONSOON','WINTER'))
);
CREATE INDEX ix_core_dim_date_fy ON core_dim_date (fy);
CREATE INDEX ix_core_dim_date_month_start ON core_dim_date (month_start);

CREATE TABLE core_reservoir (
    reservoir_id           INTEGER PRIMARY KEY,
    reservoir_name         TEXT NOT NULL UNIQUE,          -- LINGANAMAKKI, SUPA, MANI ...
    level_unit             TEXT NOT NULL CHECK (level_unit IN ('FT','M')),
    full_reservoir_level   REAL NOT NULL,         -- FRL
    min_draw_down_level    REAL NOT NULL,         -- MDDL
    gross_storage_frl_mcft REAL,
    energy_at_frl_mu       REAL,
    CHECK (full_reservoir_level > min_draw_down_level)
);

-- which reservoir feeds which power house (many-to-many; replaces the comma list)
CREATE TABLE core_reservoir_station (
    reservoir_id  INTEGER NOT NULL REFERENCES core_reservoir,
    station_code  TEXT     NOT NULL REFERENCES core_station,
    PRIMARY KEY (reservoir_id, station_code)
);

-- organisation, kept minimal (HRMS is the master for people)
CREATE TABLE core_division (
    division_code  TEXT PRIMARY KEY,                      -- 'EE(TM)-1', 'EE(CCM)/RTPS'
    division_name  TEXT NOT NULL,
    station_code   TEXT REFERENCES core_station                -- NULL for Head Office divisions
);

CREATE TABLE core_employee (
    employee_no    TEXT PRIMARY KEY,                      -- HRMS employee number
    employee_name  TEXT NOT NULL,
    designation    TEXT,                                  -- AE(M), AEE, EE ...
    division_code  TEXT REFERENCES core_division
);



-- UCB hourly log (Head Office > UCB Hourly Data screen)
CREATE TABLE core_unit_hourly_reading (
    unit_id          TEXT     NOT NULL REFERENCES core_unit,
    reading_date     TEXT     NOT NULL REFERENCES core_dim_date,
    hour_no          INTEGER NOT NULL CHECK (hour_no BETWEEN 1 AND 24),   -- hour ending
    emr_reading      REAL,                       -- energy meter register
    actual_load_mw   REAL CHECK (actual_load_mw >= 0),
    mvar             REAL,
    frequency_hz     REAL CHECK (frequency_hz BETWEEN 45 AND 55),
    coal_flow_tph    REAL CHECK (coal_flow_tph >= 0),
    gen_mu           REAL CHECK (gen_mu >= 0),
    gen_loss_mu      REAL CHECK (gen_loss_mu >= 0),
    entered_by       TEXT REFERENCES core_employee,
    PRIMARY KEY (unit_id, reading_date, hour_no)
);

-- One row per unit per day: every parameter of the station Daily Generation Report
CREATE TABLE core_unit_daily_generation (
    unit_id                  TEXT NOT NULL REFERENCES core_unit,
    gen_date                 TEXT NOT NULL REFERENCES core_dim_date,
    gross_gen_mu             REAL NOT NULL CHECK (gross_gen_mu >= 0),
    aux_consumption_mu       REAL NOT NULL DEFAULT 0 CHECK (aux_consumption_mu >= 0),   -- unit auxiliaries only
    net_export_mu            REAL GENERATED ALWAYS AS (gross_gen_mu - aux_consumption_mu) STORED,
    peak_hour_gen_mu         REAL CHECK (peak_hour_gen_mu >= 0),
    peak_hour_gen_morning_mu REAL CHECK (peak_hour_gen_morning_mu >= 0),
    peak_hour_gen_evening_mu REAL CHECK (peak_hour_gen_evening_mu >= 0),
    peak_load_mw             REAL  CHECK (peak_load_mw >= 0),
    peak_load_time           TEXT,
    running_hrs              REAL NOT NULL DEFAULT 0 CHECK (running_hrs        BETWEEN 0 AND 24),
    forced_outage_hrs        REAL NOT NULL DEFAULT 0 CHECK (forced_outage_hrs  BETWEEN 0 AND 24),
    planned_outage_hrs       REAL NOT NULL DEFAULT 0 CHECK (planned_outage_hrs BETWEEN 0 AND 24),
    rsd_hrs                  REAL NOT NULL DEFAULT 0 CHECK (rsd_hrs            BETWEEN 0 AND 24),  -- reserve shut down (= idle hours for hydro)
    trips                    INTEGER     NOT NULL DEFAULT 0 CHECK (trips >= 0),
    declared_capacity_mu     REAL CHECK (declared_capacity_mu >= 0),   -- DC; drives PAF
    deemed_gen_mu            REAL NOT NULL DEFAULT 0 CHECK (deemed_gen_mu >= 0),  -- NLD (no-load-demand deemed generation)
    coal_consumed_mt         REAL CHECK (coal_consumed_mt >= 0),
    hfo_consumed_kl          REAL CHECK (hfo_consumed_kl  >= 0),
    ldo_consumed_kl          REAL CHECK (ldo_consumed_kl  >= 0),
    gas_consumed_mmscm       REAL CHECK (gas_consumed_mmscm >= 0),
    msw_consumed_mt          REAL CHECK (msw_consumed_mt    >= 0),
    gcv_coal_kcal_kg         REAL  CHECK (gcv_coal_kcal_kg   > 0),      -- as-fired
    gcv_oil_kcal_l           REAL  CHECK (gcv_oil_kcal_l     > 0),
    gross_heat_rate_kcal_kwh REAL  CHECK (gross_heat_rate_kcal_kwh > 0),
    dm_water_cum             REAL CHECK (dm_water_cum      >= 0),
    h2_cylinders_nos         INTEGER      CHECK (h2_cylinders_nos  >= 0),
    water_used_mcft          REAL CHECK (water_used_mcft   >= 0),      -- hydro: water let out for generation
    remarks                  TEXT,
    data_source              TEXT NOT NULL DEFAULT 'KERP',
    PRIMARY KEY (unit_id, gen_date),
    CHECK (aux_consumption_mu <= gross_gen_mu OR gross_gen_mu = 0),
    CHECK (abs(running_hrs + forced_outage_hrs + planned_outage_hrs + rsd_hrs - 24) <= 0.05)
);
CREATE INDEX ix_core_unit_daily_generation_gen_date ON core_unit_daily_generation (gen_date);

-- Station-level daily items that are not attributable to a single unit
CREATE TABLE core_station_daily_consumption (
    station_code           TEXT NOT NULL REFERENCES core_station,
    gen_date               TEXT NOT NULL REFERENCES core_dim_date,
    common_aux_mu          REAL NOT NULL DEFAULT 0 CHECK (common_aux_mu >= 0),   -- station transformer / common aux
    local_feeder_kwh       REAL NOT NULL DEFAULT 0 CHECK (local_feeder_kwh >= 0),
    colony_consumption_kwh REAL NOT NULL DEFAULT 0 CHECK (colony_consumption_kwh >= 0),
    PRIMARY KEY (station_code, gen_date)
);

-- Reasons for back-down ("Reasons for back down generation in MU" block)
CREATE TABLE core_backdown_reason (
    reason_code    TEXT PRIMARY KEY,   -- QUALITY_COAL, COAL_STOCK_BUILD, SHEAR_PIN_FAILURE, UNIT_TRIP_LOSS, UNIT_STARTUP_LOSS,
                                       -- EQUIP_SYS_CONSTRAINT, ANNUAL_MAINTENANCE, NO_LOAD_DEMAND, FLY_ASH_SLAG
    description    TEXT NOT NULL,
    display_order  INTEGER NOT NULL
);

CREATE TABLE core_unit_generation_loss (
    unit_id      TEXT NOT NULL REFERENCES core_unit,
    loss_date    TEXT NOT NULL REFERENCES core_dim_date,
    reason_code  TEXT NOT NULL REFERENCES core_backdown_reason,
    loss_mu      REAL NOT NULL CHECK (loss_mu >= 0),
    PRIMARY KEY (unit_id, loss_date, reason_code)
);
CREATE INDEX ix_core_unit_generation_loss_loss_date ON core_unit_generation_loss (loss_date);

-- Outages & trips
CREATE TABLE core_outage_reason (
    reason_code      TEXT PRIMARY KEY,  -- TURBINE, ELECTRICAL, GRID_DISTURBANCE, BOILER_TUBE_LEAK, COAL_MILL, ANNUAL_OVERHAUL, NO_DEMAND ...
    description      TEXT NOT NULL,
    plant_type_code  TEXT REFERENCES core_plant_type          -- NULL = applies to all plant types
);

CREATE TABLE core_unit_outage (
    outage_id      INTEGER PRIMARY KEY,
    unit_id        TEXT NOT NULL REFERENCES core_unit,
    outage_type    TEXT NOT NULL CHECK (outage_type IN ('FORCED','PLANNED','RSD')),
    trip_at        TEXT NOT NULL,                    -- tripped / taken out
    sync_at        TEXT,                             -- re-synchronised; NULL = still out
    outage_hrs     REAL CHECK (outage_hrs >= 0), -- closed outages; open ones computed in v_unit_outage
    reason_code    TEXT NOT NULL REFERENCES core_outage_reason,
    reason_detail  TEXT,
    gen_loss_mu    REAL CHECK (gen_loss_mu >= 0),
    is_structural  INTEGER NOT NULL DEFAULT 0,        -- long-term shutdown (e.g. RTPS-U1 since 10-08-2022)
    data_source    TEXT NOT NULL DEFAULT 'KERP',
    CHECK (sync_at IS NULL OR sync_at > trip_at),
    CHECK ((sync_at IS NULL) = (outage_hrs IS NULL))
);
CREATE INDEX ix_core_unit_outage_unit_id_trip_at ON core_unit_outage (unit_id, trip_at);
CREATE INDEX ix_core_unit_outage_trip_at ON core_unit_outage (trip_at);

-- Boiler Tube Failure register (DMS > Boiler Tube Leakage menu); 1:1 with the trip
CREATE TABLE core_btf_failure_type (
    failure_type_code  TEXT PRIMARY KEY,   -- EROSION_SOOT_BLOWER, EROSION_FALLING_SLAG, WELD_DEFECT, STRESS_RUPTURE_HT_CREEP, OTHERS
    description        TEXT NOT NULL
);

CREATE TABLE core_btf_location (
    location_code  TEXT PRIMARY KEY,       -- WATER_WALL, RE_HEATER, SUPER_HEATER, STEAM_COOLED_WALL, ECONOMISER
    description    TEXT NOT NULL
);

CREATE TABLE core_boiler_tube_failure (
    btf_id              INTEGER PRIMARY KEY,
    outage_id           INTEGER NOT NULL UNIQUE REFERENCES core_unit_outage,
    failure_type_code   TEXT NOT NULL REFERENCES core_btf_failure_type,
    location_code       TEXT NOT NULL REFERENCES core_btf_location,
    history             TEXT,
    work_details        TEXT,
    inspection_details  TEXT,
    closure_at          TEXT,
    entered_by          TEXT REFERENCES core_employee
);

-- Monthly generation targets per unit
CREATE TABLE core_generation_target (
    unit_id             TEXT NOT NULL REFERENCES core_unit,
    target_month        TEXT NOT NULL CHECK (CAST(strftime('%d', target_month) AS INTEGER) = 1),
    cea_target_mu       REAL CHECK (cea_target_mu      >= 0),
    kerc_approved_mu    REAL CHECK (kerc_approved_mu   >= 0),
    internal_target_mu  REAL CHECK (internal_target_mu >= 0),
    PRIMARY KEY (unit_id, target_month)
);



CREATE TABLE core_coal_supplier (
    supplier_code  TEXT PRIMARY KEY,                      -- MCL, SCCL, WCL, IMPORTED
    supplier_name  TEXT NOT NULL,
    is_imported    INTEGER NOT NULL
);

CREATE TABLE core_coal_receipt (
    receipt_id            INTEGER PRIMARY KEY,
    station_code          TEXT NOT NULL REFERENCES core_station,
    receipt_date          TEXT NOT NULL REFERENCES core_dim_date,
    supplier_code         TEXT NOT NULL REFERENCES core_coal_supplier,
    no_of_rakes           INTEGER NOT NULL CHECK (no_of_rakes >= 0),
    quantity_mt           REAL NOT NULL CHECK (quantity_mt >= 0),
    gcv_received_kcal_kg  REAL CHECK (gcv_received_kcal_kg > 0)
);
CREATE INDEX ix_core_coal_receipt_station_code_receipt_date ON core_coal_receipt (station_code, receipt_date);

CREATE TABLE core_station_coal_stock (
    station_code            TEXT NOT NULL REFERENCES core_station,
    stock_date              TEXT NOT NULL REFERENCES core_dim_date,
    opening_stock_mt        REAL NOT NULL CHECK (opening_stock_mt >= 0),
    receipt_mt              REAL NOT NULL DEFAULT 0 CHECK (receipt_mt >= 0),
    indigenous_consumed_mt  REAL NOT NULL DEFAULT 0 CHECK (indigenous_consumed_mt >= 0),
    imported_consumed_mt    REAL NOT NULL DEFAULT 0 CHECK (imported_consumed_mt   >= 0),
    total_consumed_mt       REAL GENERATED ALWAYS AS (indigenous_consumed_mt + imported_consumed_mt) STORED,
    closing_stock_mt        REAL GENERATED ALWAYS AS
                              (opening_stock_mt + receipt_mt - indigenous_consumed_mt - imported_consumed_mt) STORED,
    days_of_stock           REAL,                 -- as reported (closing / normative daily requirement)
    normative_days          INTEGER,
    PRIMARY KEY (station_code, stock_date)
);



CREATE TABLE core_reservoir_daily_level (
    reservoir_id          INTEGER NOT NULL REFERENCES core_reservoir,
    level_date            TEXT     NOT NULL REFERENCES core_dim_date,
    water_level           REAL NOT NULL,          -- in reservoir.level_unit
    capacity_pct          REAL CHECK (capacity_pct BETWEEN 0 AND 100),
    inflow_mcft           REAL CHECK (inflow_mcft          >= 0),
    discharge_power_mcft  REAL CHECK (discharge_power_mcft >= 0),
    spill_sluice_mcft     REAL CHECK (spill_sluice_mcft    >= 0),
    rainfall_mm           REAL  CHECK (rainfall_mm          >= 0),
    energy_potential_mu   REAL CHECK (energy_potential_mu  >= 0),
    PRIMARY KEY (reservoir_id, level_date)
);

-- Head Office daily data entry: state-level numbers (Karnataka grid)
CREATE TABLE core_state_grid_daily (
    summary_date          TEXT PRIMARY KEY REFERENCES core_dim_date,
    state_consumption_mu  REAL NOT NULL CHECK (state_consumption_mu >= 0),
    state_generation_mu   REAL NOT NULL CHECK (state_generation_mu  >= 0),
    state_import_mu       REAL NOT NULL,
    system_peak_mw        REAL CHECK (system_peak_mw >= 0),
    system_peak_time      TEXT,
    freq_max_hz           REAL,
    freq_min_hz           REAL,
    tb_share_mu           REAL,                  -- Tungabhadra share
    jurala_share_mu       REAL,
    cgs_mu                REAL,                  -- central generating stations
    upcl_mu               REAL,
    jsw_mu                REAL,
    others_mu             REAL,
    remarks               TEXT,
    CHECK (freq_min_hz IS NULL OR freq_max_hz IS NULL OR freq_min_hz <= freq_max_hz)
);



CREATE TABLE core_energy_meter (
    meter_id        INTEGER PRIMARY KEY,
    station_code    TEXT NOT NULL REFERENCES core_station,
    unit_id         TEXT REFERENCES core_unit,                 -- NULL for common-aux / station export meters
    meter_no        TEXT NOT NULL,
    meter_role      TEXT NOT NULL CHECK (meter_role IN ('UNIT_GEN','UNIT_AUX','COMMON_AUX','STATION_EXPORT')),
    installed_on    TEXT,
    removed_on      TEXT,
    UNIQUE (station_code, meter_no),
    CHECK ((meter_role IN ('UNIT_GEN','UNIT_AUX')) = (unit_id IS NOT NULL))
);

-- one statement per station per month (the signed Annexure-I)
CREATE TABLE core_jmr_statement (
    jmr_id              INTEGER PRIMARY KEY,
    station_code        TEXT NOT NULL REFERENCES core_station,
    jmr_month           TEXT NOT NULL CHECK (CAST(strftime('%d', jmr_month) AS INTEGER) = 1),
    reading_upto_date   TEXT,                              -- NULL = full month; set for a part-month / provisional statement
    net_export_jmr_mu   REAL NOT NULL,           -- "Net energy transmitted as per JMR"
    signed_kptcl_by     TEXT,
    signed_escom_by     TEXT,
    signed_kpcl_by      TEXT REFERENCES core_employee,
    UNIQUE (station_code, jmr_month),
    CHECK (reading_upto_date IS NULL OR reading_upto_date >= jmr_month)
);

CREATE TABLE core_jmr_meter_reading (
    jmr_id            INTEGER NOT NULL REFERENCES core_jmr_statement ON DELETE CASCADE,
    meter_id          INTEGER NOT NULL REFERENCES core_energy_meter,
    initial_reading   REAL NOT NULL,
    final_reading     REAL NOT NULL,
    meter_constant    REAL NOT NULL CHECK (meter_constant > 0),   -- as printed on the JMR
    result_unit       TEXT NOT NULL CHECK (result_unit IN ('MU','KWH')),   -- unit of (difference x constant)
    computed_gen      REAL NOT NULL DEFAULT 0,                    -- "Compu. Gen." (meter-failure estimate), in result_unit
    difference        REAL GENERATED ALWAYS AS (final_reading - initial_reading) STORED,
    energy_mu         REAL GENERATED ALWAYS AS (
                        CASE result_unit
                          WHEN 'MU' THEN (final_reading - initial_reading) * meter_constant + computed_gen
                          ELSE          ((final_reading - initial_reading) * meter_constant + computed_gen) / 1000000
                        END) STORED,
    PRIMARY KEY (jmr_id, meter_id),
    CHECK (final_reading >= initial_reading)
);



CREATE TABLE core_counterparty (
    counterparty_code  TEXT PRIMARY KEY,                  -- BESCOM, MESCOM, CESC, HESCOM, GESCOM, KPTCL, NTPC, SECI, GOK
    counterparty_name  TEXT NOT NULL,
    counterparty_type  TEXT NOT NULL CHECK (counterparty_type IN ('ESCOM','TRANSCO','CENTRAL_PSU','SECI','GOVERNMENT')),
    gstin              TEXT,
    pan                TEXT
);

-- pools whose energy is allocated to ESCOMs in fixed shares (bill header rows)
CREATE TABLE core_allocation_pool (
    pool_code    TEXT PRIMARY KEY,                        -- SVP, KVP, OTHER_HYDRO, RTPS_U1_7, RTPS_U8, BTPS_U1_3 ...
    description  TEXT NOT NULL
);

CREATE TABLE core_escom_share (
    pool_code          TEXT NOT NULL REFERENCES core_allocation_pool,
    counterparty_code  TEXT NOT NULL REFERENCES core_counterparty,
    valid_from         TEXT NOT NULL,
    valid_to           TEXT,
    share_pct          REAL NOT NULL CHECK (share_pct BETWEEN 0 AND 100),
    PRIMARY KEY (pool_code, counterparty_code, valid_from),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- a billable line on the ESCOM bill ("Sharavathy Valley Project", "RTPS Unit 5 & 6" ...)
CREATE TABLE core_billing_group (
    billing_group_id    INTEGER PRIMARY KEY,
    billing_group_name  TEXT NOT NULL UNIQUE,
    pool_code           TEXT NOT NULL REFERENCES core_allocation_pool,
    bill_section        TEXT NOT NULL CHECK (bill_section IN ('HYDEL_RES','THERMAL')),
    display_order       INTEGER NOT NULL
);

CREATE TABLE core_billing_group_member (
    billing_group_id  INTEGER NOT NULL REFERENCES core_billing_group,
    station_code      TEXT    NOT NULL REFERENCES core_station,
    unit_id           TEXT    REFERENCES core_unit,            -- NULL = whole station
    UNIQUE (billing_group_id, station_code, unit_id)
);

CREATE TABLE core_tariff_rate (
    billing_group_id              INTEGER NOT NULL REFERENCES core_billing_group,
    valid_from                    TEXT NOT NULL,
    valid_to                      TEXT,
    capacity_charge_rs_month      REAL CHECK (capacity_charge_rs_month      >= 0),
    primary_energy_rate_rs_kwh    REAL  CHECK (primary_energy_rate_rs_kwh    >= 0),
    secondary_energy_rate_rs_kwh  REAL  CHECK (secondary_energy_rate_rs_kwh  >= 0),
    royalty_rate_rs_kwh           REAL  CHECK (royalty_rate_rs_kwh           >= 0),
    kerc_order_ref                TEXT,
    PRIMARY KEY (billing_group_id, valid_from)
);

CREATE TABLE core_energy_bill (
    bill_id          INTEGER PRIMARY KEY,
    bill_no          TEXT NOT NULL UNIQUE,
    bill_month       TEXT NOT NULL CHECK (CAST(strftime('%d', bill_month) AS INTEGER) = 1),
    bill_type        TEXT NOT NULL CHECK (bill_type IN ('INITIAL','REVISED','SUPPLEMENTARY','NOTIONAL')),
    bill_date        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'DRAFT'
                       CHECK (status IN ('DRAFT','AO_CHECKED','AGM_CHECKED','GM_APPROVED','JE_POSTED')),
    ao_checked_by    TEXT REFERENCES core_employee,  ao_checked_at   TEXT,
    agm_checked_by   TEXT REFERENCES core_employee,  agm_checked_at  TEXT,
    gm_approved_by   TEXT REFERENCES core_employee,  gm_approved_at  TEXT,
    je_no            TEXT,
    je_date          TEXT,
    UNIQUE (bill_month, bill_type, bill_date)
);

CREATE TABLE core_energy_bill_line (
    bill_line_id           INTEGER PRIMARY KEY,
    bill_id                INTEGER NOT NULL REFERENCES core_energy_bill ON DELETE CASCADE,
    billing_group_id       INTEGER NOT NULL REFERENCES core_billing_group,
    gross_generation_kwh   REAL CHECK (gross_generation_kwh >= 0),
    net_transmission_kwh   REAL NOT NULL CHECK (net_transmission_kwh >= 0),
    capacity_charges_rs    REAL,                  -- breakdown may be unavailable (source shows total only); use total_amount_rs
    primary_charges_rs     REAL,
    secondary_charges_rs   REAL,
    royalty_charges_rs     REAL,
    total_amount_rs        REAL NOT NULL,          -- always populated; = sum of the four charges when they are known
    UNIQUE (bill_id, billing_group_id),
    CHECK (((capacity_charges_rs IS NOT NULL) + (primary_charges_rs IS NOT NULL) + (secondary_charges_rs IS NOT NULL) + (royalty_charges_rs IS NOT NULL)) IN (0, 4)),
    CHECK (capacity_charges_rs IS NULL           -- either the full breakdown is present, or none of it is
           OR total_amount_rs = capacity_charges_rs + primary_charges_rs + secondary_charges_rs + royalty_charges_rs)
);

CREATE TABLE core_energy_bill_escom_split (
    bill_line_id       INTEGER NOT NULL REFERENCES core_energy_bill_line ON DELETE CASCADE,
    counterparty_code  TEXT    NOT NULL REFERENCES core_counterparty,
    share_pct          REAL  NOT NULL CHECK (share_pct BETWEEN 0 AND 100),
    transmitted_kwh    REAL NOT NULL CHECK (transmitted_kwh >= 0),
    amount_rs          REAL NOT NULL,
    PRIMARY KEY (bill_line_id, counterparty_code)
);

CREATE TABLE core_commercial_txn_type (
    txn_type_code  TEXT PRIMARY KEY,   -- ESCOM_BILL_CLAIM, ESCOM_BILL_WITHDRAWAL, RECEIPT, RE_SUBSIDY_GOK, SECI_RECEIPT
    description    TEXT NOT NULL
);

-- money in / claims against counterparties (Receipts, Sales & RE Subsidy screen)
CREATE TABLE core_commercial_transaction (
    txn_id             INTEGER PRIMARY KEY,
    txn_date           TEXT NOT NULL REFERENCES core_dim_date,
    txn_type_code      TEXT NOT NULL REFERENCES core_commercial_txn_type,
    counterparty_code  TEXT NOT NULL REFERENCES core_counterparty,
    station_code       TEXT REFERENCES core_station,           -- e.g. SECI solar project
    bill_id            INTEGER REFERENCES core_energy_bill,
    receipt_sub_type   TEXT,                              -- the "Type" drop-down on the screen
    amount_rs          REAL NOT NULL,
    tds_rs             REAL NOT NULL DEFAULT 0,
    rebate_rs          REAL NOT NULL DEFAULT 0,
    writeoff_rs        REAL NOT NULL DEFAULT 0,
    reference_no       TEXT,
    remarks            TEXT
);
CREATE INDEX ix_core_commercial_transaction_counterparty_code_txn_date ON core_commercial_transaction (counterparty_code, txn_date);

-- GoK tripartite adjustment of ESCOM dues
CREATE TABLE core_tripartite_adjustment (
    adjustment_id            INTEGER PRIMARY KEY,
    counterparty_code        TEXT NOT NULL REFERENCES core_counterparty,
    adjustment_date          TEXT NOT NULL REFERENCES core_dim_date,
    go_reference             TEXT,
    royalty_rs               REAL NOT NULL DEFAULT 0,
    electricity_tax_rs       REAL NOT NULL DEFAULT 0,
    guarantee_commission_rs  REAL NOT NULL DEFAULT 0,
    total_rs                 REAL GENERATED ALWAYS AS
                               (royalty_rs + electricity_tax_rs + guarantee_commission_rs) STORED,
    is_late_release_by_gok   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE core_seci_calibration_charge (
    station_code  TEXT NOT NULL REFERENCES core_station,
    charge_month  TEXT NOT NULL CHECK (CAST(strftime('%d', charge_month) AS INTEGER) = 1),
    amount_rs     REAL NOT NULL CHECK (amount_rs >= 0),
    gst_pct       REAL  NOT NULL CHECK (gst_pct BETWEEN 0 AND 100),
    total_rs      REAL GENERATED ALWAYS AS (amount_rs * (1 + gst_pct / 100)) STORED,
    PRIMARY KEY (station_code, charge_month)
);



CREATE TABLE core_asset (
    asset_id         INTEGER PRIMARY KEY,
    floc_code        TEXT NOT NULL UNIQUE,                -- functional location
    asset_name       TEXT NOT NULL,
    station_code     TEXT NOT NULL REFERENCES core_station,
    unit_id          TEXT REFERENCES core_unit,                -- NULL = common / station asset
    parent_asset_id  INTEGER REFERENCES core_asset,
    asset_type       TEXT
);

CREATE TABLE core_defect (
    defect_id            INTEGER PRIMARY KEY,
    station_code         TEXT NOT NULL REFERENCES core_station,
    unit_id              TEXT REFERENCES core_unit,            -- NULL = common (unit "0" in K-eRP)
    asset_id             INTEGER REFERENCES core_asset,
    raised_by_division   TEXT NOT NULL REFERENCES core_division,
    attending_division   TEXT REFERENCES core_division,
    logged_by            TEXT NOT NULL REFERENCES core_employee,
    logged_at            TEXT NOT NULL,
    priority             TEXT NOT NULL CHECK (priority IN ('IMMEDIATELY','HIGH','NORMAL','LOW')),
    defect_type          TEXT NOT NULL CHECK (defect_type IN ('MAINTENANCE','OPERATION','SAFETY','CIVIL','OTHER')),
    description          TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN
                           ('INITIATED','ACKNOWLEDGED','ASSIGNED','DIVERTED','IN_PROGRESS','COMPLETED','ACCEPTED','REJECTED')),
    acknowledged_at      TEXT,
    completed_at         TEXT,
    closed_at            TEXT,
    ptw_required         INTEGER NOT NULL DEFAULT 0,
    reason               TEXT
);
CREATE INDEX ix_core_defect_station_code_logged_at ON core_defect (station_code, logged_at);

CREATE TABLE core_permit_to_work (
    ptw_id       INTEGER PRIMARY KEY,
    defect_id    INTEGER NOT NULL REFERENCES core_defect,
    issued_by    TEXT REFERENCES core_employee,
    issued_at    TEXT NOT NULL,
    received_by  TEXT REFERENCES core_employee,
    returned_at  TEXT,
    status       TEXT NOT NULL CHECK (status IN ('ISSUED','RETURNED','CANCELLED')),
    CHECK (returned_at IS NULL OR returned_at >= issued_at)
);

CREATE TABLE core_line_clearance (
    lc_id          INTEGER PRIMARY KEY,
    station_code   TEXT NOT NULL REFERENCES core_station,
    asset_id       INTEGER REFERENCES core_asset,
    defect_id      INTEGER REFERENCES core_defect,
    requested_by   TEXT REFERENCES core_employee,
    requested_at   TEXT NOT NULL,
    forwarded_at   TEXT,
    approved_by    TEXT,                                  -- LD Centre official
    approved_at    TEXT,
    returned_at    TEXT,
    status         TEXT NOT NULL CHECK (status IN ('INITIATED','FORWARDED','APPROVED','REJECTED','RETURNED')),
    remarks        TEXT
);


-- =============================================================================

CREATE UNIQUE INDEX ux_core_billing_group_member ON core_billing_group_member (COALESCE(billing_group_id, ''), COALESCE(station_code, ''), COALESCE(unit_id, ''));
