"""Text-level SQL guard and the KPCL grain guards (fail closed), plus the NULLS LAST rewrite."""
import pytest

from app.sql_safety import UnsafeSQLError, validate_readonly_sql

OK = "SELECT SUM(gross_gen_mu) AS g FROM mart_fact_station_generation_fy WHERE fy = '2024-25'"


def test_plain_select_passes():
    assert validate_readonly_sql(OK + ";") == OK


@pytest.mark.parametrize("sql,why", [
    ("", "Empty"),
    ("DELETE FROM mart_dim_station", "SELECT"),
    ("SELECT 1; DELETE FROM mart_dim_station", "Multiple"),
    ("SELECT * FROM mart_dim_station WHERE station_key = '{station}'", "placeholder"),
    ("WITH x AS (SELECT 1) SELECT * FROM x; DROP TABLE y", "Multiple"),
    ("SELECT * FROM core_station", "not available"),
    ("SELECT * FROM mart_dim_station JOIN raw_units ON 1=1", "not available"),
    ("SELECT 1 FROM mart_dim_station /* x */ WHERE 1=1 AND 'a'='a' ATTACH", "Forbidden"),
    ("SELECT AVG(plf_pct) AS p FROM mart_fact_station_generation_monthly WHERE fy = '2024-25'", "ratio"),
    ("SELECT SUM(f.paf_pct) AS p FROM mart_fact_station_generation_fy f", "ratio"),
    ("SELECT AVG(gross_heat_rate_kcal_kwh) AS h FROM mart_fact_unit_generation_daily", "ratio"),
    ("SELECT SUM(days_of_stock) AS d FROM mart_fact_coal_stock_daily", "ratio"),
    ("SELECT SUM(m.gross_gen_mu) AS g FROM mart_fact_station_generation_monthly m "
     "JOIN mart_fact_unit_generation_daily d ON d.station_key = m.station_key", "different grain"),
    ("SELECT r.description AS r, COUNT(*) AS n FROM mart_fact_unit_outage o "
     "JOIN mart_dim_outage_reason r ON r.reason_code = o.reason_code GROUP BY r.description", "reason_class"),
])
def test_rejected(sql, why):
    with pytest.raises(UnsafeSQLError, match=why):
        validate_readonly_sql(sql)


@pytest.mark.parametrize("sql", [
    # ratios computed correctly, weighted helpers, MAX of a ratio, UNION ALL across grains, self-join, CTE
    "SELECT SUM(gross_gen_mu) / SUM(capacity_mu) * 100 AS plf FROM mart_fact_station_generation_monthly",
    "SELECT SUM(gcv_coal_kcal_kg * coal_consumed_mt) / SUM(coal_consumed_mt) AS g FROM mart_fact_unit_generation_daily",
    "SELECT MAX(plf_pct) AS p FROM mart_fact_station_generation_monthly",
    "SELECT AVG(days_of_stock) AS d FROM mart_fact_coal_stock_daily",
    "SELECT SUM(closing_stock_mt) AS c FROM mart_fact_coal_stock_daily WHERE date_key = '2025-04-06'",
    "SELECT 'FY' AS p, gross_gen_mu AS g FROM mart_fact_station_generation_fy WHERE fy='2024-25' AND station_key='RTPS' "
    "UNION ALL SELECT 'M', gross_gen_mu FROM mart_fact_station_generation_monthly WHERE month_label='MAR-2025' AND station_key='RTPS'",
    "WITH t AS (SELECT fy, SUM(gross_gen_mu) AS g FROM mart_fact_station_generation_fy GROUP BY fy) SELECT fy, g FROM t",
    "SELECT r.description AS r FROM mart_dim_outage_reason r WHERE r.reason_class = 'OUTAGE'",
    "SELECT station_key AS s FROM mart_dim_station WHERE station_name LIKE '%delete from%'",
])
def test_legitimate_patterns_pass(sql):
    validate_readonly_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT station_key AS s, ROUND(SUM(aux_consumption_mu) * 100.0 / NULLIF(SUM(aux_consumption_mu), 0), 2) AS aux_pct "
    "FROM mart_fact_station_generation_fy GROUP BY station_key",
    "SELECT ROUND(SUM(declared_capacity_mu) / SUM(capacity_mu) * 100, 2) AS plf_pct FROM mart_fact_station_generation_fy",
])
def test_kpi_alias_with_wrong_formula_rejected(sql):
    with pytest.raises(UnsafeSQLError, match="not computed with its formula"):
        validate_readonly_sql(sql)


def test_kpi_alias_right_formula_or_bare_column_passes():
    validate_readonly_sql("SELECT station_key AS s, ROUND(SUM(aux_consumption_mu) * 100.0 / NULLIF(SUM(gross_gen_mu), 0), 2) "
                          "AS aux_pct FROM mart_fact_station_generation_fy GROUP BY station_key")
    validate_readonly_sql("SELECT f.plf_pct AS plf_pct FROM mart_fact_station_generation_fy f WHERE fy = '2024-25'")


def test_status_table_for_past_period_rejected():
    entities = {"periods": [{"kind": "month", "value": "MAR-2024", "relative": None}]}
    with pytest.raises(UnsafeSQLError, match="end of the data"):
        validate_readonly_sql("SELECT unit_key AS u FROM mart_fact_unit_current_status WHERE status <> 'RUNNING'", entities)
    # "now" questions may use it
    validate_readonly_sql("SELECT unit_key AS u FROM mart_fact_unit_current_status", {"as_of": "2025-04-30"})


def test_nulls_last_added_to_ascending_sorts_only():
    out = validate_readonly_sql("SELECT station_key AS s, plf_pct AS p FROM mart_fact_station_generation_fy "
                                "ORDER BY plf_pct ASC, station_key LIMIT 3")
    assert "ORDER BY plf_pct ASC NULLS LAST, station_key NULLS LAST LIMIT 3" in out
    out = validate_readonly_sql("SELECT plf_pct AS p FROM mart_fact_station_generation_fy ORDER BY plf_pct DESC LIMIT 1")
    assert "NULLS" not in out
