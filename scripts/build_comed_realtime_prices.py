"""Build the root ComEd real-time hourly pricing CSV."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from lp_besh.comed_price_builder import (
    aggregate_realtime_5min_to_hourly,
    fetch_realtime_5min_prices_for_interval_range,
    fill_internal_missing_realtime_hours,
    write_realtime_hourly_csv,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build ComEd hourly real-time prices from 5-minute data.")
    parser.add_argument("--start", required=True, help="First hourly interval_start, e.g. 2022-07-03T00:00")
    parser.add_argument("--end", required=True, help="Exclusive end hourly interval_start, e.g. 2026-07-04T00:00")
    parser.add_argument("--output", type=Path, default=Path("comed_realtime_hourly_pricing.csv"))
    parser.add_argument("--batch-days", type=int, default=14)
    parser.add_argument(
        "--no-fill-missing",
        action="store_true",
        help="Leave fully missing hourly buckets absent instead of adding imputed rows.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL verification for local environments without a working certificate bundle.",
    )
    args = parser.parse_args()

    interval_start = datetime.fromisoformat(args.start)
    interval_end_exclusive = datetime.fromisoformat(args.end)
    if interval_end_exclusive <= interval_start:
        raise SystemExit("--end must be after --start")

    points = fetch_realtime_5min_prices_for_interval_range(
        interval_start=interval_start,
        interval_end=interval_end_exclusive,
        batch_days=args.batch_days,
        verify_ssl=not args.insecure,
    )
    rows = aggregate_realtime_5min_to_hourly(points)
    rows = [row for row in rows if interval_start <= row.interval_start < interval_end_exclusive]
    if not args.no_fill_missing:
        rows = fill_internal_missing_realtime_hours(rows)
        rows = [row for row in rows if interval_start <= row.interval_start < interval_end_exclusive]
    write_realtime_hourly_csv(args.output, rows)

    partial_count = sum(1 for row in rows if row.raw_point_count < 12)
    imputed_count = sum(1 for row in rows if row.price_quality == "imputed_missing_hour")
    print(f"wrote {len(rows):,} hourly rows to {args.output}")
    print(f"included {partial_count:,} row(s) with fewer than 12 source points")
    print(f"imputed {imputed_count:,} fully missing internal hourly row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
