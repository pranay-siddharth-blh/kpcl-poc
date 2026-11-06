"""
Evaluation bank for the live run (tests/run_eval.py). Each question has reference SQL whose first-row figures the
chatbot's answer must contain (as text or in its result rows). Questions are phrased differently from the
verified library on purpose, so most of them exercise the LLM path.
"""
QUESTIONS = [
    {"id": 1, "q": "How much did YTPS generate in total during FY 2023-24?",
     "ref": "SELECT ROUND(gross_gen_mu,3) FROM mart_fact_station_generation_fy WHERE station_key='YTPS' AND fy='2023-24'"},
    {"id": 2, "q": "Give me the PAF of each BTPS unit for March 2025",
     "ref": "SELECT paf_pct FROM mart_fact_unit_generation_monthly WHERE station_key='BTPS' AND month_label='MAR-2025' ORDER BY unit_key"},
    {"id": 3, "q": "Which hydro station generated the most in water year 2023-24 and how much?",
     "ref": "SELECT station_key, ROUND(SUM(gross_gen_mu),3) g FROM mart_fact_station_generation_daily WHERE plant_type_code='HYDRO' "
            "AND water_year='2023-24' GROUP BY station_key ORDER BY g DESC LIMIT 1"},
    {"id": 4, "q": "What was the aux consumption percentage of YTPS across FY 2024-25 overall?",
     "ref": "SELECT ROUND(SUM(aux_consumption_mu)/SUM(gross_gen_mu)*100,2) FROM mart_fact_station_generation_fy WHERE station_key='YTPS' AND fy='2024-25'"},
    {"id": 5, "q": "How many boiler tube failures did RTPS units have in FY 2022-23, unit by unit?",
     "ref": "SELECT unit_key, COUNT(*) FROM mart_fact_unit_outage WHERE station_key='RTPS' AND fy='2022-23' AND is_btf=1 GROUP BY unit_key ORDER BY 2 DESC LIMIT 1"},
    {"id": 6, "q": "Compare KPCL's total generation in FY 2022-23 and FY 2023-24",
     "ref": "SELECT ROUND(SUM(gross_gen_mu),3) FROM mart_fact_station_generation_fy WHERE fy='2023-24'"},
    {"id": 7, "q": "Which month in FY 2023-24 did SGS generate the least?",
     "ref": "SELECT month_label, ROUND(gross_gen_mu,3) FROM mart_fact_station_generation_monthly WHERE station_key='SGS' AND fy='2023-24' ORDER BY gross_gen_mu LIMIT 1"},
    {"id": 8, "q": "What was the weighted average GCV of coal burnt at BTPS in FY 2024-25?",
     "ref": "SELECT wa_gcv_coal_kcal_kg FROM mart_fact_station_generation_fy WHERE station_key='BTPS' AND fy='2024-25'"},
    {"id": 9, "q": "Total forced outage hours of YTPS units in FY 2024-25",
     "ref": "SELECT ROUND(SUM(forced_outage_hrs),2) FROM mart_fact_station_generation_fy WHERE station_key='YTPS' AND fy='2024-25'"},
    {"id": 10, "q": "What was the maximum state system peak demand in FY 2024-25?",
     "ref": "SELECT MAX(system_peak_mw) FROM mart_fact_grid_daily WHERE fy='2024-25'"},
    {"id": 11, "q": "How many days in FY 2023-24 was the Linganamakki reservoir below 30% storage?",
     "ref": "SELECT COUNT(*) FROM mart_fact_reservoir_daily WHERE reservoir_name='LINGANAMAKKI' AND fy='2023-24' AND capacity_pct < 30"},
    {"id": 12, "q": "What was the solar stations' combined generation in FY 2024-25?",
     "ref": "SELECT ROUND(SUM(gross_gen_mu),3) FROM mart_fact_station_generation_fy WHERE plant_category_label='SOLAR' AND fy='2024-25'"},
    {"id": 13, "q": "Which thermal unit had the lowest PLF in FY 2024-25 excluding units that were shut all year?",
     "ref": "SELECT unit_key, ROUND(SUM(gross_gen_mu)/SUM(capacity_mu)*100,2) p FROM mart_fact_unit_generation_monthly "
            "WHERE plant_category_label='THERMAL-COAL' AND fy='2024-25' GROUP BY unit_key HAVING SUM(gross_gen_mu)>0 ORDER BY p LIMIT 1"},
    {"id": 14, "q": "Coal consumption at RTPS in March 2025 in tonnes",
     "ref": "SELECT ROUND(coal_consumed_mt) FROM mart_fact_station_generation_monthly WHERE station_key='RTPS' AND month_label='MAR-2025'"},
    {"id": 15, "q": "How much imported coal did BTPS receive in FY 2024-25?",
     "ref": "SELECT ROUND(SUM(quantity_mt)) FROM mart_fact_coal_receipt WHERE station_key='BTPS' AND fy='2024-25' AND is_imported=1"},
]
