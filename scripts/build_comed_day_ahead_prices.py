"""Build the root ComEd day-ahead hourly pricing CSV."""

from __future__ import annotations

import argparse
import csv
import re
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


COMED_DAY_AHEAD_FEED_URL = "https://hourlypricing.comed.com/rrtp/ServletFeed"
COMED_DAY_AHEAD_SOURCE = "comed_rrtp_day_ahead"
COMED_DAY_AHEAD_SOURCE_EXISTING = "comed_besh_day_ahead"
DAY_AHEAD_POINT_RE = re.compile(
    r"Date\.UTC\((\d+),(\d+),(\d+),(\d+),0,0\),\s*([-+]?\d+(?:\.\d+)?)"
)


@dataclass(frozen=True)
class DayAheadPrice:
    """One day-ahead hourly price from ComEd's public day-ahead feed."""

    interval_start: datetime
    price_cents_per_kwh: Decimal
    source: str = COMED_DAY_AHEAD_SOURCE

    @property
    def hour_ending(self) -> int:
        return self.interval_start.hour + 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Build ComEd hourly day-ahead prices.")
    parser.add_argument("--start", help="First date or interval_start, e.g. 2022-07-03")
    parser.add_argument("--end", help="Exclusive end date or interval_start, e.g. 2026-07-05")
    parser.add_argument("--date", action="append", help="Specific date to fetch. Can be passed more than once.")
    parser.add_argument("--output", type=Path, default=Path("comed_day_ahead_hourly_pricing.csv"))
    parser.add_argument(
        "--existing",
        type=Path,
        help="Existing CSV to merge. Existing intervals are preserved unless --replace-existing is passed.",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Replace existing intervals with freshly fetched feed values.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL verification for local environments without a working certificate bundle.",
    )
    args = parser.parse_args()

    dates_to_fetch = _dates_to_fetch(args)

    existing_rows = read_existing_rows(args.existing) if args.existing else {}
    fetched_rows: dict[datetime, dict[str, str]] = {}
    empty_dates: list[date] = []
    context = ssl._create_unverified_context() if args.insecure else None

    for current in dates_to_fetch:
        points = fetch_day_ahead_prices(current, ssl_context=context)
        if not points:
            empty_dates.append(current)
        for point in points:
            fetched_rows[point.interval_start] = row_for_price(point)

    merged, added, replaced = merge_day_ahead_rows(
        existing_rows, fetched_rows, replace_existing=args.replace_existing
    )

    write_rows(args.output, merged)
    print(f"fetched {len(fetched_rows):,} day-ahead row(s)")
    print(f"preserved {len(existing_rows) - replaced:,} existing row(s)")
    print(f"added {added:,} missing/new row(s)")
    if replaced:
        print(f"replaced {replaced:,} existing row(s)")
    if empty_dates:
        preview = ", ".join(day.isoformat() for day in empty_dates[:10])
        suffix = "" if len(empty_dates) <= 10 else f", and {len(empty_dates) - 10} more"
        print(f"source returned no rows for {len(empty_dates):,} date(s): {preview}{suffix}")
    print(f"wrote {len(merged):,} hourly rows to {args.output}")
    return 0


def fetch_day_ahead_prices(
    day: date,
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> list[DayAheadPrice]:
    params = {"type": "daynexttoday", "date": day.strftime("%Y%m%d")}
    url = f"{COMED_DAY_AHEAD_FEED_URL}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=60, context=ssl_context) as response:
        body = response.read().decode("utf-8").strip()
    return parse_day_ahead_payload(body)


def parse_day_ahead_payload(payload: str) -> list[DayAheadPrice]:
    rows: list[DayAheadPrice] = []
    for year, month_zero_based, day, hour, price in DAY_AHEAD_POINT_RE.findall(payload):
        interval_start = datetime(
            int(year),
            int(month_zero_based) + 1,
            int(day),
            int(hour),
        )
        rows.append(
            DayAheadPrice(
                interval_start=interval_start,
                price_cents_per_kwh=Decimal(price),
            )
        )
    return rows


def merge_day_ahead_rows(
    existing: dict[datetime, dict[str, str]],
    fetched: dict[datetime, dict[str, str]],
    *,
    replace_existing: bool = False,
) -> tuple[dict[datetime, dict[str, str]], int, int]:
    """Merge freshly fetched rows into existing rows, keyed by interval_start.

    Returns (merged, added_count, replaced_count). Existing rows are preserved unless
    `replace_existing` is set, in which case fetched values overwrite matching intervals.
    """

    merged = dict(existing)
    added = 0
    replaced = 0
    for interval_start, row in fetched.items():
        if interval_start in merged and not replace_existing:
            continue
        if interval_start in merged:
            replaced += 1
        else:
            added += 1
        merged[interval_start] = row
    return merged, added, replaced


def read_existing_rows(path: Path) -> dict[datetime, dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows: dict[datetime, dict[str, str]] = {}
        for row_number, row in enumerate(reader, start=2):
            raw_interval = row.get("interval_start", "")
            try:
                interval_start = datetime.strptime(raw_interval, "%Y-%m-%d %H:%M")
            except ValueError as exc:
                raise ValueError(
                    f"{path}: row {row_number} has invalid interval_start {raw_interval!r}"
                ) from exc
            if interval_start in rows:
                raise ValueError(f"{path}: duplicate interval_start {raw_interval!r}")
            rows[interval_start] = {field: row.get(field, "") for field in fieldnames()}
        return rows


def row_for_price(price: DayAheadPrice) -> dict[str, str]:
    dollars = price.price_cents_per_kwh / Decimal("100")
    return {
        "interval_start": price.interval_start.strftime("%Y-%m-%d %H:%M"),
        "date": price.interval_start.date().isoformat(),
        "hour_ending": str(price.hour_ending),
        "price": _format_decimal(dollars, places=5),
        "price_dollars_per_kwh": _format_decimal(dollars, places=5),
        "price_cents_per_kwh": _format_decimal(price.price_cents_per_kwh, places=3),
        "source_timestamp_text": _format_source_timestamp(price.interval_start),
        "source": price.source,
    }


def write_rows(path: Path, rows: dict[datetime, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames())
        writer.writeheader()
        for interval_start in sorted(rows):
            writer.writerow(rows[interval_start])


def fieldnames() -> list[str]:
    return [
        "interval_start",
        "date",
        "hour_ending",
        "price",
        "price_dollars_per_kwh",
        "price_cents_per_kwh",
        "source_timestamp_text",
        "source",
    ]


def _parse_date_arg(value: str) -> date:
    return datetime.fromisoformat(value).date()


def _dates_to_fetch(args: argparse.Namespace) -> list[date]:
    if args.date:
        return sorted({_parse_date_arg(value) for value in args.date})

    if not args.start or not args.end:
        raise SystemExit("either pass --date at least once or pass both --start and --end")

    start_date = _parse_date_arg(args.start)
    end_date_exclusive = _parse_date_arg(args.end)
    if end_date_exclusive <= start_date:
        raise SystemExit("--end must be after --start")

    dates: list[date] = []
    current = start_date
    while current < end_date_exclusive:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def _format_decimal(value: Decimal, *, places: int) -> str:
    return f"{value:.{places}f}"


def _format_source_timestamp(value: datetime) -> str:
    hour = value.hour % 12 or 12
    marker = "AM" if value.hour < 12 else "PM"
    return f"{value.month}/{value.day}/{value.year} {hour}:00:00 {marker}"


if __name__ == "__main__":
    raise SystemExit(main())
