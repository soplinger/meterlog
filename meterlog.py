#!/usr/bin/env python3
"""meterlog: an offline log for household utility meters.

Readings live in a plain-text file, one reading per line::

    DATE METER VALUE [# note]

Blank lines and lines starting with ``#`` are ignored. Every other line is
either parsed or reported with its line number, so nothing is skipped
silently. Money and meter values use ``decimal.Decimal`` throughout.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

CENT = Decimal("0.01")
RATE = Decimal("0.001")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
METER_RE = re.compile(r"[A-Za-z0-9_-]+")
NUMBER_RE = re.compile(r"[0-9]+(\.[0-9]*)?|\.[0-9]+")
LONG_GAP_DAYS = 62
DEFAULT_THRESHOLD_PERCENT = Decimal("50")
DEFAULT_LAST_PERIODS = 3

Issue = tuple[int, str]


class DataError(Exception):
    """A problem that stops a command from producing a trustworthy answer."""


@dataclass(frozen=True)
class Reading:
    line: int
    day: date
    meter: str
    value: Decimal
    note: str = ""


@dataclass(frozen=True)
class Period:
    """Usage between two consecutive readings of the same meter."""

    meter: str
    start: date
    end: date
    start_value: Decimal
    end_value: Decimal
    used: Decimal
    days: int

    @property
    def daily(self) -> Decimal:
        return self.used / self.days


@dataclass
class Data:
    readings: list[Reading]
    errors: list[Issue]
    warnings: list[Issue]


@dataclass(frozen=True)
class Tariff:
    symbol: str
    standing_per_day: Decimal
    vat_percent: Decimal
    tiers: list[tuple[Optional[Decimal], Decimal]]  # (cumulative up_to or None, rate)


@dataclass(frozen=True)
class Bill:
    period: Period
    energy: Decimal
    standing: Decimal
    vat: Decimal

    @property
    def total(self) -> Decimal:
        return self.energy + self.standing + self.vat


# --- parsing and validation -------------------------------------------------


def parse_readings(text: str) -> tuple[list[Reading], list[Issue]]:
    readings: list[Reading] = []
    errors: list[Issue] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        note = ""
        for index, token in enumerate(tokens):
            if token.startswith("#"):
                note = " ".join(tokens[index:])[1:].strip()
                tokens = tokens[:index]
                break
        if len(tokens) != 3:
            errors.append((line_no, f"expected 'DATE METER VALUE [# note]', found {len(tokens)} field(s)"))
            continue
        day_text, meter, value_text = tokens
        day = _parse_day(day_text)
        if day is None:
            errors.append((line_no, f"date must be a real YYYY-MM-DD date, got '{day_text}'"))
            continue
        if not METER_RE.fullmatch(meter):
            errors.append((line_no, f"meter name must be letters, digits, '_' or '-', got '{meter}'"))
            continue
        value = parse_number(value_text)
        if value is None:
            errors.append((line_no, f"value must be a plain number of zero or more, got '{value_text}'"))
            continue
        readings.append(Reading(line_no, day, meter, value, note))
    return readings, errors


def parse_number(text: str) -> Optional[Decimal]:
    """A plain ASCII decimal such as 12, 12.5 or .5. Rejects signs, separators and exponents."""
    if not NUMBER_RE.fullmatch(text):
        return None
    return Decimal(text)


def _parse_day(text: str) -> Optional[date]:
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def by_meter(readings: list[Reading]) -> dict[str, list[Reading]]:
    groups: dict[str, list[Reading]] = {}
    for reading in sorted(readings, key=lambda r: (r.meter, r.day, r.line)):
        groups.setdefault(reading.meter, []).append(reading)
    return groups


def check_readings(readings: list[Reading], rollover: Optional[Decimal]) -> tuple[list[Issue], list[Issue]]:
    errors: list[Issue] = []
    warnings: list[Issue] = []
    for meter, rows in by_meter(readings).items():
        if rollover is not None:
            for row in rows:
                if row.value >= rollover:
                    errors.append((row.line, f"{meter}: {row.value} is at or above the rollover of {rollover}"))
        for prev, cur in zip(rows, rows[1:]):
            if cur.day == prev.day:
                errors.append((cur.line, f"{meter}: second reading on {cur.day}, the first is on line {prev.line}"))
                continue
            if cur.value < prev.value and rollover is None:
                errors.append((
                    cur.line,
                    f"{meter}: {cur.value} is lower than {prev.value} on line {prev.line}; "
                    "pass --rollover if the meter wraps round",
                ))
            gap = (cur.day - prev.day).days
            if gap > LONG_GAP_DAYS:
                warnings.append((cur.line, f"{meter}: {gap} days since the reading on line {prev.line}"))
    return errors, warnings


def analyze(text: str, rollover: Optional[Decimal] = None) -> Data:
    readings, parse_errors = parse_readings(text)
    check_errors, warnings = check_readings(readings, rollover)
    errors = sorted(parse_errors + check_errors, key=lambda issue: issue[0])
    return Data(readings, errors, sorted(warnings, key=lambda issue: issue[0]))


def build_periods(readings: list[Reading], rollover: Optional[Decimal] = None) -> list[Period]:
    """Consecutive-reading usage for each meter. Assumes the readings passed check_readings."""
    periods: list[Period] = []
    for meter, rows in by_meter(readings).items():
        for prev, cur in zip(rows, rows[1:]):
            if cur.value >= prev.value:
                used = cur.value - prev.value
            else:
                used = rollover - prev.value + cur.value
            periods.append(Period(meter, prev.day, cur.day, prev.value, cur.value, used, (cur.day - prev.day).days))
    return periods


def group_periods(periods: list[Period]) -> dict[str, list[Period]]:
    groups: dict[str, list[Period]] = {}
    for period in periods:
        groups.setdefault(period.meter, []).append(period)
    return groups


# --- analysis ---------------------------------------------------------------


def forecast(readings: list[Reading], meter: str, on: date,
             rollover: Optional[Decimal] = None, last_periods: int = DEFAULT_LAST_PERIODS) -> tuple[Decimal, Decimal]:
    """Project the meter reading on a future date. Returns (reading, daily usage used)."""
    rows = by_meter(readings).get(meter)
    if not rows:
        raise DataError(f"no readings for meter '{meter}'")
    last = rows[-1]
    if on <= last.day:
        raise DataError(f"forecast date {on} must be after the last {meter} reading on {last.day}")
    periods = build_periods(rows, rollover)[-last_periods:]
    if not periods:
        raise DataError(f"{meter} needs at least two readings to forecast")
    used = sum((p.used for p in periods), Decimal(0))
    days = sum(p.days for p in periods)
    daily = used / days
    projected = last.value + daily * (on - last.day).days
    if rollover is not None:
        projected %= rollover
    return projected.quantize(CENT, rounding=ROUND_HALF_UP), daily


def find_anomalies(periods: list[Period], threshold: Decimal) -> list[tuple[Period, Decimal]]:
    """Periods whose daily rate is more than `threshold` away from the meter's median (needs 3+ periods)."""
    flagged: list[tuple[Period, Decimal]] = []
    for group in group_periods(periods).values():
        if len(group) < 3:
            continue
        median = statistics.median(p.daily for p in group)
        if median == 0:
            continue
        for period in group:
            deviation = period.daily / median - 1
            if abs(deviation) > threshold:
                flagged.append((period, deviation))
    return flagged


def bill_period(period: Period, tariff: Tariff) -> Bill:
    """Tiered energy charge within the period, standing charge per day, VAT. Each part is rounded to cents."""
    remaining = period.used
    lower = Decimal(0)
    energy = Decimal(0)
    for up_to, rate in tariff.tiers:
        if remaining <= 0:
            break
        band = remaining if up_to is None else min(remaining, up_to - lower)
        energy += band * rate
        remaining -= band
        if up_to is not None:
            lower = up_to
    energy = _cents(energy)
    standing = _cents(tariff.standing_per_day * period.days)
    vat = _cents((energy + standing) * tariff.vat_percent / 100)
    return Bill(period, energy, standing, vat)


def _cents(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def money(amount: Decimal, symbol: str) -> str:
    return f"{symbol}{amount:,.2f}"


def chart_lines(periods: list[Period], width: int) -> list[str]:
    if not periods:
        return []
    peak = max(p.daily for p in periods)
    lines = []
    for period in periods:
        size = 0 if peak == 0 else int((period.daily / peak * width).to_integral_value(rounding=ROUND_HALF_UP))
        bar = "#" * size
        lines.append(f"{period.end}  {bar:<{width}}  {period.daily.quantize(RATE)}/day")
    return lines


# --- tariff loading ---------------------------------------------------------


def load_tariff(path: str) -> Tariff:
    try:
        raw = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise DataError(f"tariff {path} is not valid JSON: {exc}")
    if not isinstance(raw, dict):
        raise DataError(f"tariff {path} must be a JSON object")
    symbol = str(raw.get("symbol", ""))
    standing = _tariff_decimal(raw.get("standing_charge_per_day", "0"), "standing_charge_per_day")
    vat = _tariff_decimal(raw.get("vat_percent", "0"), "vat_percent")
    tiers_raw = raw.get("tiers")
    if not isinstance(tiers_raw, list) or not tiers_raw:
        raise DataError("tariff needs a non-empty 'tiers' list")
    tiers: list[tuple[Optional[Decimal], Decimal]] = []
    previous = Decimal(0)
    for index, tier in enumerate(tiers_raw, start=1):
        label = f"tiers[{index}]"
        if not isinstance(tier, dict) or "rate" not in tier:
            raise DataError(f"{label} must be an object with a 'rate'")
        if previous is None:
            raise DataError(f"{label}: only the last tier may be open-ended (up_to null)")
        rate = _tariff_decimal(tier["rate"], f"{label}.rate")
        if tier.get("up_to") is None:
            up_to = None
        else:
            up_to = _tariff_decimal(tier["up_to"], f"{label}.up_to")
            if up_to <= previous:
                raise DataError(f"{label}.up_to must be greater than the previous limit {previous}")
        tiers.append((up_to, rate))
        previous = up_to
    if tiers[-1][0] is not None:
        raise DataError("the last tier must have up_to null so that all usage is billed")
    return Tariff(symbol, standing, vat, tiers)


def _tariff_decimal(value: object, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise DataError(f"{name} must be a number, got {value!r}")
    if not number.is_finite() or number < 0:
        raise DataError(f"{name} must be a finite number of zero or more, got {value!r}")
    return number


# --- file helpers -----------------------------------------------------------


def read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DataError(f"no such file: {path}")
    except UnicodeDecodeError:
        raise DataError(f"{path} is not UTF-8 text")
    except OSError as exc:
        raise DataError(f"cannot read {path}: {exc.strerror}")


def load_checked(path: str, rollover: Optional[Decimal]) -> Data:
    """Load a readings file for a command that needs clean data. Warnings go to stderr."""
    data = analyze(read_text(path), rollover)
    if data.errors:
        raise DataError(format_issues(data.errors, path))
    for line, message in data.warnings:
        print(f"warning: {path}:{line}: {message}", file=sys.stderr)
    return data


def format_issues(issues: list[Issue], path: str) -> str:
    return "\n".join(f"{path}:{line}: {message}" for line, message in issues)


def select_meter(readings: list[Reading], meter: Optional[str]) -> list[Reading]:
    if meter is None:
        return readings
    selected = [r for r in readings if r.meter == meter]
    if not selected:
        raise DataError(f"no readings for meter '{meter}'")
    return selected


def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]
    template = "  ".join(f"{{:<{width}}}" for width in widths)
    lines = [template.format(*headers), template.format(*("-" * width for width in widths))]
    lines += [template.format(*row) for row in rows]
    return "\n".join(line.rstrip() for line in lines)


def period_dict(period: Period) -> dict[str, object]:
    return {
        "meter": period.meter,
        "start": period.start.isoformat(),
        "end": period.end.isoformat(),
        "days": period.days,
        "start_value": str(period.start_value),
        "end_value": str(period.end_value),
        "used": str(period.used),
        "daily": str(period.daily.quantize(RATE, rounding=ROUND_HALF_UP)),
    }


def bill_dict(bill: Bill) -> dict[str, object]:
    payload = period_dict(bill.period)
    payload.update(energy=str(bill.energy), standing=str(bill.standing), vat=str(bill.vat), total=str(bill.total))
    return payload


# --- commands ---------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    data = analyze(read_text(args.file), args.rollover)
    for line, message in data.warnings:
        print(f"warning: {args.file}:{line}: {message}")
    if data.errors:
        print(format_issues(data.errors, args.file))
        print(f"{len(data.errors)} error(s) in {args.file}")
        return 1
    meters = by_meter(data.readings)
    print(f"OK: {len(data.readings)} reading(s) for {len(meters)} meter(s) in {args.file}")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    data = load_checked(args.file, args.rollover)
    readings = select_meter(data.readings, args.meter)
    periods = group_periods(build_periods(readings, args.rollover))
    rows = []
    for meter, rows_for_meter in by_meter(readings).items():
        meter_periods = periods.get(meter, [])
        used = sum((p.used for p in meter_periods), Decimal(0))
        days = sum(p.days for p in meter_periods)
        average = str((used / days).quantize(RATE, rounding=ROUND_HALF_UP)) if days else "-"
        rows.append([
            meter,
            str(len(rows_for_meter)),
            rows_for_meter[0].day.isoformat(),
            rows_for_meter[-1].day.isoformat(),
            str(rows_for_meter[-1].value),
            str(used) if days else "-",
            average,
        ])
    print(table(["METER", "READINGS", "FIRST", "LAST", "LATEST", "USED", "AVG/DAY"], rows))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    data = load_checked(args.file, args.rollover)
    periods = build_periods(select_meter(data.readings, args.meter), args.rollover)
    if args.json:
        print(json.dumps([period_dict(p) for p in periods], indent=2))
        return 0
    if not periods:
        print("no usage periods yet: a meter needs at least two readings")
        return 0
    rows = [[p.meter, p.start.isoformat(), p.end.isoformat(), str(p.days), str(p.used),
             str(p.daily.quantize(RATE, rounding=ROUND_HALF_UP))] for p in periods]
    print(table(["METER", "FROM", "TO", "DAYS", "USED", "PER DAY"], rows))
    return 0


def cmd_bill(args: argparse.Namespace) -> int:
    tariff = load_tariff(args.tariff)
    data = load_checked(args.file, args.rollover)
    periods = build_periods(select_meter(data.readings, args.meter), args.rollover)
    if args.month:
        periods = [p for p in periods if p.end.strftime("%Y-%m") == args.month]
    bills = [bill_period(p, tariff) for p in periods]
    if args.json:
        print(json.dumps([bill_dict(b) for b in bills], indent=2))
        return 0
    if not bills:
        print("no billable usage periods match")
        return 0
    rows = [[b.period.meter, b.period.start.isoformat(), b.period.end.isoformat(), str(b.period.used),
             money(b.energy, tariff.symbol), money(b.standing, tariff.symbol),
             money(b.vat, tariff.symbol), money(b.total, tariff.symbol)] for b in bills]
    print(table(["METER", "FROM", "TO", "USED", "ENERGY", "STANDING", "VAT", "TOTAL"], rows))
    grand_total = sum((b.total for b in bills), Decimal(0))
    print(f"\nTotal for {len(bills)} period(s): {money(grand_total, tariff.symbol)}")
    return 0


def cmd_forecast(args: argparse.Namespace) -> int:
    data = load_checked(args.file, args.rollover)
    value, daily = forecast(data.readings, args.meter, args.on, args.rollover, args.last)
    print(f"{args.meter} on {args.on}: about {value}")
    print(f"based on the last {args.last} period(s) at {daily.quantize(RATE, rounding=ROUND_HALF_UP)} per day")
    return 0


def cmd_anomalies(args: argparse.Namespace) -> int:
    data = load_checked(args.file, args.rollover)
    periods = build_periods(select_meter(data.readings, args.meter), args.rollover)
    threshold = args.threshold / 100
    flagged = find_anomalies(periods, threshold)
    if not flagged:
        print(f"no periods more than {args.threshold}% away from the median daily use")
        return 0
    rows = [[p.meter, p.start.isoformat(), p.end.isoformat(), str(p.daily.quantize(RATE, rounding=ROUND_HALF_UP)),
             f"{deviation * 100:+.1f}%"] for p, deviation in flagged]
    print(table(["METER", "FROM", "TO", "PER DAY", "VS MEDIAN"], rows))
    return 0


def cmd_chart(args: argparse.Namespace) -> int:
    data = load_checked(args.file, args.rollover)
    periods = build_periods(select_meter(data.readings, args.meter), args.rollover)
    if args.meter not in group_periods(periods):
        print(f"no usage periods yet for '{args.meter}'")
        return 0
    for line in chart_lines(group_periods(periods)[args.meter], args.width):
        print(line)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    existing = read_text(args.file) if Path(args.file).exists() else ""
    if "\n" in args.note:
        raise DataError("a note must fit on one line")
    new_line = f"{args.on.isoformat()} {args.meter} {args.value}"
    if args.note:
        new_line += f" # {args.note}"
    separator = "" if not existing or existing.endswith("\n") else "\n"
    combined = existing + separator + new_line + "\n"
    data = analyze(combined, args.rollover)
    if data.errors:
        raise DataError("not added; the file would have errors:\n" + format_issues(data.errors, args.file))
    with open(args.file, "a", encoding="utf-8") as handle:
        handle.write(separator + new_line + "\n")
    line_count = combined.count("\n")
    print(f"added line {line_count} to {args.file}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    data = load_checked(args.file, args.rollover)
    periods = build_periods(select_meter(data.readings, args.meter), args.rollover)
    if args.format == "json":
        print(json.dumps([period_dict(p) for p in periods], indent=2))
        return 0
    writer = csv.writer(sys.stdout)
    writer.writerow(["meter", "start", "end", "days", "start_value", "end_value", "used", "per_day"])
    for p in periods:
        writer.writerow([p.meter, p.start.isoformat(), p.end.isoformat(), p.days, p.start_value,
                         p.end_value, p.used, p.daily.quantize(RATE, rounding=ROUND_HALF_UP)])
    return 0


# --- argument parsing -------------------------------------------------------


def _decimal_arg(text: str) -> Decimal:
    value = parse_number(text)
    if value is None or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive plain number: {text!r}")
    return value


def _reading_arg(text: str) -> Decimal:
    value = parse_number(text)
    if value is None:
        raise argparse.ArgumentTypeError(f"must be a plain number of zero or more: {text!r}")
    return value


def _day_arg(text: str) -> date:
    parsed = _parse_day(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"expected a real YYYY-MM-DD date, got {text!r}")
    return parsed


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}")
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {text!r}")
    return value


def _month_arg(text: str) -> str:
    if not MONTH_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {text!r}")
    return text


def _meter_arg(text: str) -> str:
    if not METER_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"meter names are letters, digits, '_' or '-': {text!r}")
    return text


def build_parser() -> argparse.ArgumentParser:
    rollover = argparse.ArgumentParser(add_help=False)
    rollover.add_argument("--rollover", type=_decimal_arg, metavar="N",
                          help="the value at which the meter returns to 0, e.g. 100000 for a five-digit meter")

    parser = argparse.ArgumentParser(
        prog="meterlog",
        description="Offline utility meter log: readings, usage, bills, forecasts and anomalies.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = commands.add_parser("check", parents=[rollover], help="validate a readings file")
    p.add_argument("file")
    p.set_defaults(func=cmd_check)

    p = commands.add_parser("summary", parents=[rollover], help="per-meter totals and averages")
    p.add_argument("file")
    p.add_argument("--meter", type=_meter_arg)
    p.set_defaults(func=cmd_summary)

    p = commands.add_parser("usage", parents=[rollover], help="usage between consecutive readings")
    p.add_argument("file")
    p.add_argument("--meter", type=_meter_arg)
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.set_defaults(func=cmd_usage)

    p = commands.add_parser("bill", parents=[rollover], help="price each usage period from a tariff file")
    p.add_argument("file")
    p.add_argument("--tariff", required=True, metavar="TARIFF.JSON")
    p.add_argument("--meter", type=_meter_arg)
    p.add_argument("--month", type=_month_arg, metavar="YYYY-MM",
                   help="only periods that end in this month")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.set_defaults(func=cmd_bill)

    p = commands.add_parser("forecast", parents=[rollover], help="project a meter reading on a future date")
    p.add_argument("file")
    p.add_argument("meter", type=_meter_arg)
    p.add_argument("on", type=_day_arg, metavar="DATE")
    p.add_argument("--last", type=_positive_int, default=DEFAULT_LAST_PERIODS, metavar="N",
                   help="average the last N usage periods (default %(default)s)")
    p.set_defaults(func=cmd_forecast)

    p = commands.add_parser("anomalies", parents=[rollover], help="flag periods far from the median daily use")
    p.add_argument("file")
    p.add_argument("--meter", type=_meter_arg)
    p.add_argument("--threshold", type=_decimal_arg, default=DEFAULT_THRESHOLD_PERCENT, metavar="PERCENT",
                   help="how far from the median counts as odd (default %(default)s)")
    p.set_defaults(func=cmd_anomalies)

    p = commands.add_parser("chart", parents=[rollover], help="text bar chart of daily use for one meter")
    p.add_argument("file")
    p.add_argument("meter", type=_meter_arg)
    p.add_argument("--width", type=_positive_int, default=40, metavar="N")
    p.set_defaults(func=cmd_chart)

    p = commands.add_parser("add", parents=[rollover], help="append a checked reading to the file")
    p.add_argument("file")
    p.add_argument("on", type=_day_arg, metavar="DATE")
    p.add_argument("meter", type=_meter_arg)
    p.add_argument("value", type=_reading_arg, metavar="VALUE")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_add)

    p = commands.add_parser("export", parents=[rollover], help="export usage periods as CSV or JSON")
    p.add_argument("file")
    p.add_argument("--meter", type=_meter_arg)
    p.add_argument("--format", choices=["csv", "json"], default="csv")
    p.set_defaults(func=cmd_export)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DataError as exc:
        print(f"meterlog: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
