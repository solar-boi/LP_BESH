"""Catch up both root ComEd price CSVs to the latest available day, in one command.

Detects the trailing gap in each CSV independently (day-ahead by calendar date,
real-time by hour) and fetches only that gap, merging it into the existing file.
Existing rows are never touched. Run with no arguments for the common case:

    PYTHONPATH=src python3 scripts/update_comed_prices.py
"""

from __future__ import annotations

import argparse
import ssl
from datetime import date, datetime, timedelta
from pathlib import Path

import build_comed_day_ahead_prices as day_ahead

from lp_besh.comed_price_builder import (
    aggregate_realtime_5min_to_hourly,
    fetch_realtime_5min_prices_for_interval_range,
    fill_internal_missing_realtime_hours,
    merge_realtime_rows,
    read_realtime_hourly_csv,
    write_realtime_hourly_csv,
)

DEFAULT_DAY_AHEAD_CSV = Path("comed_day_ahead_hourly_pricing.csv")
DEFAULT_REALTIME_CSV = Path("comed_realtime_hourly_pricing.csv")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fill in any missing days on both root ComEd price CSVs, up to --through."
    )
    parser.add_argument(
        "--through",
        help="Last local date to catch up through, e.g. 2026-08-13. Defaults to today.",
    )
    parser.add_argument("--day-ahead-csv", type=Path, default=DEFAULT_DAY_AHEAD_CSV)
    parser.add_argument("--realtime-csv", type=Path, default=DEFAULT_REALTIME_CSV)
    parser.add_argument("--skip-day-ahead", action="store_true", help="Only catch up real-time prices.")
    parser.add_argument("--skip-realtime", action="store_true", help="Only catch up day-ahead prices.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the missing range for each file without fetching anything.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL verification for local environments without a working certificate bundle.",
    )
    args = parser.parse_args()

    through = date.fromisoformat(args.through) if args.through else date.today()

    if not args.skip_day_ahead:
        update_day_ahead(args.day_ahead_csv, through, insecure=args.insecure, dry_run=args.dry_run)
    if not args.skip_realtime:
        update_realtime(args.realtime_csv, through, insecure=args.insecure, dry_run=args.dry_run)
    return 0


def update_day_ahead(path: Path, through: date, *, insecure: bool, dry_run: bool) -> None:
    if not path.exists():
        raise SystemExit(
            f"{path} does not exist. Run scripts/build_comed_day_ahead_prices.py first to "
            "create the initial history."
        )

    existing = day_ahead.read_existing_rows(path)
    last_existing_date = max(existing).date()
    start_date = last_existing_date + timedelta(days=1)

    if start_date > through:
        print(f"day-ahead: already up to date through {last_existing_date}")
        return

    day_count = (through - start_date).days + 1
    if dry_run:
        print(f"day-ahead: missing {day_count:,} day(s), would fetch {start_date} through {through}")
        return

    print(f"day-ahead: last existing date {last_existing_date}, fetching {start_date} through {through}")
    context = ssl._create_unverified_context() if insecure else None
    fetched_rows: dict[datetime, dict[str, str]] = {}
    empty_dates: list[date] = []
    current = start_date
    while current <= through:
        points = day_ahead.fetch_day_ahead_prices(current, ssl_context=context)
        if not points:
            empty_dates.append(current)
        for point in points:
            fetched_rows[point.interval_start] = day_ahead.row_for_price(point)
        current += timedelta(days=1)

    merged, added, _replaced = day_ahead.merge_day_ahead_rows(existing, fetched_rows, replace_existing=False)
    day_ahead.write_rows(path, merged)

    print(f"day-ahead: fetched {len(fetched_rows):,} row(s), added {added:,} new row(s)")
    if empty_dates:
        preview = ", ".join(day.isoformat() for day in empty_dates[:10])
        suffix = "" if len(empty_dates) <= 10 else f", and {len(empty_dates) - 10} more"
        print(f"day-ahead: source had no data yet for {len(empty_dates):,} date(s): {preview}{suffix}")
    print(f"day-ahead: wrote {len(merged):,} total row(s) to {path}")


def update_realtime(path: Path, through: date, *, insecure: bool, dry_run: bool) -> None:
    if not path.exists():
        raise SystemExit(
            f"{path} does not exist. Run scripts/build_comed_realtime_prices.py first to "
            "create the initial history."
        )

    existing = read_realtime_hourly_csv(path)
    last_existing_hour = max(existing)
    start = last_existing_hour + timedelta(hours=1)
    end_exclusive = datetime(through.year, through.month, through.day) + timedelta(days=1)

    if start >= end_exclusive:
        print(f"real-time: already up to date through {last_existing_hour:%Y-%m-%d %H:%M}")
        return

    hour_count = int((end_exclusive - start).total_seconds() // 3600)
    if dry_run:
        print(f"real-time: missing {hour_count:,} hour(s), would fetch {start:%Y-%m-%d %H:%M} through {through}")
        return

    print(f"real-time: last existing hour {last_existing_hour:%Y-%m-%d %H:%M}, fetching through {through}")
    points = fetch_realtime_5min_prices_for_interval_range(
        interval_start=start,
        interval_end=end_exclusive,
        verify_ssl=not insecure,
    )
    rows = aggregate_realtime_5min_to_hourly(points)
    rows = [row for row in rows if start <= row.interval_start < end_exclusive]
    rows = fill_internal_missing_realtime_hours(rows)
    rows = [row for row in rows if start <= row.interval_start < end_exclusive]

    merged = merge_realtime_rows(existing, rows)
    write_realtime_hourly_csv(path, merged)

    partial_count = sum(1 for row in rows if row.raw_point_count < 12)
    imputed_count = sum(1 for row in rows if row.price_quality == "imputed_missing_hour")
    print(
        f"real-time: fetched {len(rows):,} hourly row(s) "
        f"({partial_count:,} partial, {imputed_count:,} imputed)"
    )
    print(f"real-time: wrote {len(merged):,} total row(s) to {path}")


if __name__ == "__main__":
    raise SystemExit(main())
