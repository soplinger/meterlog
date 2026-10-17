# meterlog

**An offline log for your household utility meters: readings in, usage, bills, forecasts and anomalies out.**

meterlog keeps every reading in one plain-text file you can read, edit and back up. It turns those readings into exact usage figures, prices each billing period from a tariff you write yourself, projects where the meter will be on a future date, and flags the periods that look wrong. It runs on Python 3's standard library, makes no network calls, and needs no account.

## Features

- **Plain-text readings file.** One reading per line: `DATE METER VALUE [# note]`. Comments and blank lines are allowed.
- **Line-numbered validation.** Bad dates, non-numeric or negative values, malformed lines and duplicate days are each reported with their file and line number. Nothing is skipped silently.
- **`check` command.** Validates a file and exits non-zero if anything is wrong, so it can run in a script or pre-commit hook.
- **Usage between readings.** Per-period usage, days covered and per-day rate, all in exact `Decimal` arithmetic.
- **Meter rollover support.** `--rollover 100000` says the meter returns to 0 at 100000, so a reading that drops counts the usage across the wrap (99999.9 then 0.1 is 0.2 used).
- **Gap warnings.** Flags readings more than 62 days apart, which often means a missed read.
- **Per-meter summary.** Reading counts, first and latest dates, total used and average daily use.
- **Tiered billing.** Prices each usage period from a JSON tariff with cumulative tiers, a daily standing charge and VAT. Each line item is rounded half-up to the cent.
- **Month filter for bills.** `bill --month 2026-03` prices only periods that end in that month.
- **Forecasting.** Projects the reading on any future date from the average daily use over the last N periods (default 3), and wraps the result correctly at a rollover.
- **Anomaly detection.** Flags periods whose daily use is more than a set percentage away from that meter's median (default 50%).
- **Text bar chart.** A per-meter bar chart of daily use, scaled to the peak period.
- **Safe appends.** `add` checks the whole file with the new reading included, and refuses to write if the result would have errors.
- **CSV and JSON output.** `usage --json`, `bill --json` and `export --format csv|json` feed spreadsheets or other scripts.

## Quick start

From the project folder, with Python 3.9 or newer:

```sh
python3 meterlog.py check examples/readings.txt
```

Expected output:

```
OK: 10 reading(s) for 2 meter(s) in examples/readings.txt
```

Now forecast the electricity reading for 1 July 2026:

```sh
python3 meterlog.py forecast examples/readings.txt electricity 2026-07-01
```

```
electricity on 2026-07-01: about 11268.71
based on the last 3 period(s) at 5.725 per day
```

Price the electricity periods with the sample tariff:

```sh
python3 meterlog.py bill examples/readings.txt --tariff examples/tariff.json --meter electricity
```

To try `add` without touching the sample, copy the file first:

```sh
cp examples/readings.txt my-readings.txt
python3 meterlog.py add my-readings.txt 2026-06-01 electricity 11100 --note "June read"
```

## Readings file format

```
# Household meters, read on the first of each month.
2026-01-01 electricity 10234.5 # moved in
2026-02-01 electricity 10410.0
2026-01-01 gas 4411
```

- `DATE` is a real calendar date as `YYYY-MM-DD`.
- `METER` is letters, digits, `_` or `-`.
- `VALUE` is a plain decimal number of zero or more, such as `12`, `12.5` or `.5`. Thousands separators (`1,200`), underscores (`1_000`), exponents (`1e3`), signs and non-ASCII digits are rejected.
- A `#` starts an optional note.

## Tariff format

```json
{
  "symbol": "£",
  "standing_charge_per_day": "0.45",
  "vat_percent": "5",
  "tiers": [
    {"up_to": "150", "rate": "0.2814"},
    {"up_to": null, "rate": "0.3650"}
  ]
}
```

`up_to` is the cumulative limit for each tier within one usage period. The last tier must have `"up_to": null` so that all usage is billed. Tiers apply per usage period, which is the span between two consecutive readings.

## Commands

| Command | What it does |
| --- | --- |
| `check FILE` | Validate the readings file; exit 1 on errors |
| `summary FILE [--meter M]` | Per-meter totals and averages |
| `usage FILE [--meter M] [--json]` | Usage between consecutive readings |
| `bill FILE --tariff T.json [--meter M] [--month YYYY-MM] [--json]` | Price each period |
| `forecast FILE METER DATE [--last N]` | Project a reading on a future date |
| `anomalies FILE [--meter M] [--threshold PCT]` | Flag periods far from the median |
| `chart FILE METER [--width N]` | Text bar chart of daily use |
| `add FILE DATE METER VALUE [--note TEXT]` | Append a reading if the file stays valid |
| `export FILE [--meter M] [--format csv\|json]` | Export usage periods |

Every command that reads data also accepts `--rollover N`, where `N` is the value at which the meter returns to 0 (for example `100000` for a five-digit meter).

Exit codes: `0` on success, `1` for data or tariff problems (the message goes to stderr), and `2` for bad command-line arguments.

## How to run

Run any command with `python3 meterlog.py COMMAND ...`, or run `python3 meterlog.py --help` for the full list. Use your own file in place of `examples/readings.txt`.

## How to test

The application needs only the standard library. The tests use [pytest](https://pytest.org):

```sh
python3 -m pip install pytest
python3 -m pytest
```

The suite lives in `tests/test_meterlog.py`. It covers parsing and line-numbered errors, duplicate and falling readings, rollover handling, exact tiered billing and rounding, tariff validation, forecasting including rollover wrap, anomaly detection, chart scaling, and the command-line behaviour of `check`, `usage`, `bill`, `add`, `export` and unknown meters.

## Project layout

```
meterlog.py                 the whole application
examples/readings.txt       sample readings for two meters
examples/tariff.json        sample tariff
tests/conftest.py           puts the project folder on the import path
tests/test_meterlog.py      unit and command-line tests
```

## Notes and limits

- All arithmetic uses `Decimal`. Usage, prices and forecasts are never rounded before the final step.
- Forecasts assume the recent rate continues. They are estimates, not a substitute for a real read.
- A falling reading is an error unless you pass `--rollover`, because a falling value can also mean a replaced meter or a typo.
