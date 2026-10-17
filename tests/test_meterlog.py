import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import meterlog
from meterlog import (
    DataError,
    Period,
    Tariff,
    analyze,
    bill_period,
    build_periods,
    chart_lines,
    find_anomalies,
    forecast,
    load_tariff,
    main,
    parse_readings,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "readings.txt"
TARIFF = ROOT / "examples" / "tariff.json"


def write(tmp_path, text, name="readings.txt"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# --- parsing ----------------------------------------------------------------


def test_parse_skips_blank_and_comment_lines_and_keeps_notes():
    text = "# header\n\n2026-01-01 gas 4411 # moved in\n"
    readings, errors = parse_readings(text)
    assert errors == []
    assert len(readings) == 1
    assert readings[0].line == 3
    assert readings[0].value == Decimal("4411")
    assert readings[0].note == "moved in"


def test_example_file_parses_cleanly():
    readings, errors = parse_readings(EXAMPLE.read_text(encoding="utf-8"))
    assert errors == []
    assert len(readings) == 10


@pytest.mark.parametrize(
    "line, fragment",
    [
        ("2026-02-30 gas 1", "real YYYY-MM-DD date"),
        ("02/03/2026 gas 1", "real YYYY-MM-DD date"),
        ("2026-01-01 gas 1,200", "plain number"),
        ("2026-01-01 gas 1_000", "plain number"),
        ("2026-01-01 gas 1e3", "plain number"),
        ("2026-01-01 gas -5", "plain number"),
        ("2026-01-01 gas nan", "plain number"),
        ("2026-01-01 gas ١٢", "plain number"),
        ("2026-01-01 gas", "found 2 field(s)"),
        ("2026-01-01 g$s 5", "meter name"),
    ],
)
def test_bad_lines_are_reported_with_their_line_number(line, fragment):
    readings, errors = parse_readings("2026-01-01 gas 10\n" + line + "\n")
    assert len(readings) == 1
    assert len(errors) == 1
    assert errors[0][0] == 2
    assert fragment in errors[0][1]


# --- validation -------------------------------------------------------------


def test_duplicate_day_is_an_error_pointing_at_the_first_reading():
    data = analyze("2026-01-01 gas 10\n2026-01-01 gas 11\n")
    assert [line for line, _ in data.errors] == [2]
    assert "line 1" in data.errors[0][1]


def test_falling_reading_is_an_error_without_rollover():
    data = analyze("2026-01-01 gas 990\n2026-02-01 gas 20\n")
    assert len(data.errors) == 1
    assert "pass --rollover" in data.errors[0][1]


def test_long_gap_is_a_warning_not_an_error():
    data = analyze("2026-01-01 gas 10\n2026-04-01 gas 20\n")
    assert data.errors == []
    assert data.warnings and "90 days" in data.warnings[0][1]


def test_rollover_allows_a_wrap_and_counts_the_usage_across_it():
    data = analyze("2026-01-01 gas 990\n2026-02-01 gas 20\n", rollover=Decimal("1000"))
    assert data.errors == []
    (period,) = build_periods(data.readings, Decimal("1000"))
    assert period.used == Decimal("30")


def test_rollover_is_the_modulus_so_a_wrap_counts_the_true_usage():
    data = analyze("2026-01-01 elec 99999.9\n2026-01-02 elec 0.1\n", rollover=Decimal("100000"))
    assert data.errors == []
    (period,) = build_periods(data.readings, Decimal("100000"))
    assert period.used == Decimal("0.2")


def test_reading_equal_to_the_rollover_is_an_error():
    data = analyze("2026-01-01 gas 100000\n", rollover=Decimal("100000"))
    assert len(data.errors) == 1
    assert "at or above the rollover" in data.errors[0][1]


def test_reading_above_rollover_is_an_error():
    data = analyze("2026-01-01 gas 1200\n", rollover=Decimal("1000"))
    assert len(data.errors) == 1


# --- usage ------------------------------------------------------------------


def test_usage_is_exact_between_consecutive_readings():
    data = analyze(EXAMPLE.read_text(encoding="utf-8"))
    gas = [p for p in build_periods(data.readings) if p.meter == "gas"]
    assert gas[0] == Period("gas", date(2026, 1, 1), date(2026, 2, 1),
                            Decimal("4411"), Decimal("4502.5"), Decimal("91.5"), 31)


# --- billing ----------------------------------------------------------------


def test_bill_applies_tiers_standing_charge_and_vat_in_exact_cents():
    tariff = load_tariff(str(TARIFF))
    period = Period("electricity", date(2026, 2, 1), date(2026, 3, 1),
                    Decimal("10410.0"), Decimal("10602.5"), Decimal("192.5"), 28)
    bill = bill_period(period, tariff)
    # 150 * 0.2814 + 42.5 * 0.3650 = 57.7225 -> 57.72
    assert bill.energy == Decimal("57.72")
    # 28 * 0.45 = 12.60; VAT 5% of 70.32 = 3.516 -> 3.52
    assert bill.standing == Decimal("12.60")
    assert bill.vat == Decimal("3.52")
    assert bill.total == Decimal("73.84")


def test_bill_rounds_half_up_to_the_cent():
    tariff = Tariff("", Decimal("0"), Decimal("0"), [(None, Decimal("0.5"))])
    period = Period("gas", date(2026, 1, 1), date(2026, 1, 2), Decimal(0), Decimal("0.25"), Decimal("0.25"), 1)
    assert bill_period(period, tariff).energy == Decimal("0.13")


def _tariff_file(tmp_path, tiers):
    return write(tmp_path, json.dumps({"tiers": tiers}), "tariff.json")


def test_tariff_must_end_with_an_open_tier(tmp_path):
    path = _tariff_file(tmp_path, [{"up_to": "100", "rate": "0.3"}])
    with pytest.raises(DataError, match="up_to null"):
        load_tariff(path)


def test_tariff_limits_must_increase(tmp_path):
    path = _tariff_file(tmp_path, [{"up_to": "100", "rate": "0.3"}, {"up_to": "50", "rate": "0.4"},
                                   {"up_to": None, "rate": "0.5"}])
    with pytest.raises(DataError, match="greater than the previous limit"):
        load_tariff(path)


# --- forecast and analysis --------------------------------------------------


def test_forecast_projects_from_the_last_three_periods():
    data = analyze(EXAMPLE.read_text(encoding="utf-8"))
    electricity = [r for r in data.readings if r.meter == "electricity"]
    # last 3 periods: 509.5 units over 89 days; 61 days past 2026-05-01
    value, _ = forecast(electricity, "electricity", date(2026, 7, 1), None, 3)
    assert value == Decimal("11268.71")


def test_forecast_wraps_at_the_rollover():
    data = analyze("2026-01-01 gas 9990\n2026-01-11 gas 9999\n", rollover=Decimal("10000"))
    value, daily = forecast(data.readings, "gas", date(2026, 1, 21), Decimal("10000"))
    assert daily == Decimal("0.9")
    assert value == Decimal("8.00")


def test_forecast_refuses_a_date_on_or_before_the_last_reading():
    data = analyze(EXAMPLE.read_text(encoding="utf-8"))
    with pytest.raises(DataError, match="must be after"):
        forecast(data.readings, "gas", date(2026, 5, 1))


def test_anomaly_flags_periods_more_than_half_away_from_the_median():
    data = analyze(EXAMPLE.read_text(encoding="utf-8"))
    flagged = find_anomalies(build_periods(data.readings), Decimal("0.5"))
    # electricity Mar->Apr is 73% under its median; gas Mar->Apr is 71% under and Apr->May 58% over
    assert sorted((p.meter, p.start) for p, _ in flagged) == [
        ("electricity", date(2026, 3, 1)),
        ("gas", date(2026, 3, 1)),
        ("gas", date(2026, 4, 1)),
    ]


def test_chart_bars_scale_to_the_peak_rate():
    periods = [
        Period("gas", date(2026, 1, 1), date(2026, 1, 11), Decimal(0), Decimal(20), Decimal(20), 10),
        Period("gas", date(2026, 1, 11), date(2026, 1, 21), Decimal(20), Decimal(25), Decimal(5), 10),
    ]
    lines = chart_lines(periods, 10)
    # the second period is 0.25 of the peak, so 2.5 bars rounds half up to 3
    assert lines[0].split()[1] == "#" * 10
    assert lines[1].split()[1] == "#" * 3


# --- command line -----------------------------------------------------------


def test_check_reports_ok_for_the_example(capsys):
    assert main(["check", str(EXAMPLE)]) == 0
    assert "OK: 10 reading(s) for 2 meter(s)" in capsys.readouterr().out


def test_check_reports_errors_with_file_and_line(tmp_path, capsys):
    path = write(tmp_path, "2026-01-01 gas 10\n2026-13-01 gas 11\n")
    assert main(["check", path]) == 1
    out = capsys.readouterr().out
    assert f"{path}:2: date must be a real YYYY-MM-DD date" in out


def test_usage_json_lists_every_period(capsys):
    assert main(["usage", str(EXAMPLE), "--json"]) == 0
    periods = json.loads(capsys.readouterr().out)
    assert len(periods) == 8
    assert periods[0]["used"] == "175.5"


def test_bill_month_filter_keeps_only_periods_ending_in_that_month(capsys):
    code = main(["bill", str(EXAMPLE), "--tariff", str(TARIFF), "--meter", "electricity",
                 "--month", "2026-03", "--json"])
    assert code == 0
    (bill,) = json.loads(capsys.readouterr().out)
    assert bill["total"] == "73.84"


def test_unknown_meter_is_an_error(capsys):
    assert main(["summary", str(EXAMPLE), "--meter", "water"]) == 1
    assert "no readings for meter 'water'" in capsys.readouterr().err


def test_add_appends_a_valid_reading(tmp_path, capsys):
    path = write(tmp_path, "2026-01-01 gas 10\n")
    assert main(["add", path, "2026-02-01", "gas", "12.5", "--note", "winter"]) == 0
    assert (tmp_path / "readings.txt").read_text(encoding="utf-8").endswith(
        "2026-02-01 gas 12.5 # winter\n")


def test_add_refuses_a_reading_that_breaks_the_file(tmp_path, capsys):
    original = "2026-01-01 gas 10\n"
    path = write(tmp_path, original)
    assert main(["add", path, "2026-01-01", "gas", "11"]) == 1
    assert (tmp_path / "readings.txt").read_text(encoding="utf-8") == original


def test_export_csv_has_a_header_and_one_row_per_period(capsys):
    assert main(["export", str(EXAMPLE), "--meter", "gas"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "meter,start,end,days,start_value,end_value,used,per_day"
    assert len(lines) == 5
