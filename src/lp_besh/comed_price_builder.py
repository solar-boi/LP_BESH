"""Build ComEd hourly price CSVs from public price feeds."""

from __future__ import annotations

import csv
import json
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo


COMED_REALTIME_API_URL = "https://hourlypricing.comed.com/api"
COMED_TIMEZONE = "America/Chicago"
_SSL_CERT_HINT = (
    "TLS certificate verification failed while contacting ComEd's pricing API.\n"
    "This is a common macOS python.org issue: this interpreter's default CA bundle isn't "
    "populated from the system trust store. Fix with one of:\n"
    "  1. /Applications/Python\\ 3.13/Install_Certificates.command\n"
    "  2. export SSL_CERT_FILE=$(python3 -m certifi)\n"
    "(--insecure disables verification as a last resort; not recommended for routine use.)"
)


@dataclass(frozen=True)
class FiveMinuteRealTimePrice:
    """One 5-minute real-time price point from ComEd."""

    timestamp_utc: datetime
    price_cents_per_kwh: Decimal


@dataclass(frozen=True)
class HourlyRealTimePrice:
    """One hourly real-time price calculated from available 5-minute points."""

    interval_start: datetime
    price_dollars_per_kwh: Decimal
    price_cents_per_kwh: Decimal
    raw_point_count: int
    source_bucket_start_local: datetime | None
    source_bucket_end_local: datetime | None
    source_bucket_start_utc: datetime | None
    source_bucket_end_utc: datetime | None
    price_quality: str
    source: str

    @property
    def hour_ending(self) -> int:
        return self.interval_start.hour + 1


def fetch_realtime_5min_prices(
    *,
    source_start_local: datetime,
    source_end_local: datetime,
    verify_ssl: bool = True,
) -> list[FiveMinuteRealTimePrice]:
    """Fetch ComEd 5-minute real-time prices for a local source timestamp range."""

    params = {
        "type": "5minutefeed",
        "datestart": source_start_local.strftime("%Y%m%d%H%M"),
        "dateend": source_end_local.strftime("%Y%m%d%H%M"),
        "format": "json",
    }
    context = None if verify_ssl else ssl._create_unverified_context()
    url = f"{COMED_REALTIME_API_URL}?{urlencode(params)}"
    try:
        with urlopen(url, timeout=60, context=context) as response:
            body = response.read().decode("utf-8").strip()
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            print(_SSL_CERT_HINT, file=sys.stderr)
        raise
    if not body:
        return []
    payload = json.loads(body)
    return parse_realtime_5min_payload(payload)


def fetch_realtime_5min_prices_for_interval_range(
    *,
    interval_start: datetime,
    interval_end: datetime,
    batch_days: int = 14,
    verify_ssl: bool = True,
) -> list[FiveMinuteRealTimePrice]:
    """Fetch all 5-minute points needed for the requested hourly interval range."""

    if interval_end < interval_start:
        raise ValueError("interval_end must be on or after interval_start")
    if batch_days < 1:
        raise ValueError("batch_days must be at least 1")

    source_start = interval_start - timedelta(hours=1)
    source_end = interval_end - timedelta(minutes=5)
    points_by_timestamp: dict[datetime, FiveMinuteRealTimePrice] = {}

    cursor = source_start
    batch_delta = timedelta(days=batch_days)
    while cursor <= source_end:
        batch_end = min(cursor + batch_delta - timedelta(minutes=5), source_end)
        for point in fetch_realtime_5min_prices(
            source_start_local=cursor,
            source_end_local=batch_end,
            verify_ssl=verify_ssl,
        ):
            points_by_timestamp[point.timestamp_utc] = point
        cursor = batch_end + timedelta(minutes=5)

    return sorted(points_by_timestamp.values(), key=lambda point: point.timestamp_utc)


def parse_realtime_5min_payload(payload: object) -> list[FiveMinuteRealTimePrice]:
    """Parse ComEd's JSON payload into typed 5-minute price points."""

    if not isinstance(payload, list):
        raise ValueError("ComEd 5-minute payload must be a JSON list")

    points: list[FiveMinuteRealTimePrice] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"5-minute payload item {index} is not an object")

        millis_raw = item.get("millisUTC")
        price_raw = item.get("price")
        if millis_raw is None or price_raw is None:
            raise ValueError(f"5-minute payload item {index} is missing millisUTC or price")

        timestamp_utc = datetime.fromtimestamp(int(millis_raw) / 1000, tz=timezone.utc)
        points.append(
            FiveMinuteRealTimePrice(
                timestamp_utc=timestamp_utc,
                price_cents_per_kwh=Decimal(str(price_raw)),
            )
        )

    return points


def aggregate_realtime_5min_to_hourly(
    points: Iterable[FiveMinuteRealTimePrice],
    *,
    local_timezone: str = COMED_TIMEZONE,
) -> list[HourlyRealTimePrice]:
    """Average all available 5-minute prices into hourly real-time prices."""

    zone = ZoneInfo(local_timezone)
    buckets: dict[datetime, list[tuple[FiveMinuteRealTimePrice, datetime]]] = {}
    for point in points:
        local_timestamp = point.timestamp_utc.astimezone(zone)
        source_hour_start = local_timestamp.replace(minute=0, second=0, microsecond=0)
        # ComEd's 5-minute timestamps represent the source bucket for the next
        # hour-ending label. Preserve that convention used by the CSV files.
        interval_start = (source_hour_start + timedelta(hours=1)).replace(tzinfo=None)
        buckets.setdefault(interval_start, []).append((point, local_timestamp))

    hourly_rows: list[HourlyRealTimePrice] = []
    for interval_start, bucket_points in sorted(buckets.items()):
        prices = [point.price_cents_per_kwh for point, _ in bucket_points]
        count = len(prices)
        if count == 0:
            continue

        average_cents = sum(prices, Decimal("0")) / Decimal(count)
        local_timestamps = [local_timestamp for _, local_timestamp in bucket_points]
        utc_timestamps = [point.timestamp_utc for point, _ in bucket_points]
        hourly_rows.append(
            HourlyRealTimePrice(
                interval_start=interval_start,
                price_dollars_per_kwh=average_cents / Decimal("100"),
                price_cents_per_kwh=average_cents,
                raw_point_count=count,
                source_bucket_start_local=min(local_timestamps),
                source_bucket_end_local=max(local_timestamps),
                source_bucket_start_utc=min(utc_timestamps),
                source_bucket_end_utc=max(utc_timestamps),
                price_quality="observed_full" if count == 12 else "observed_partial",
                source="comed_5minutefeed",
            )
        )

    return hourly_rows


def fill_internal_missing_realtime_hours(
    rows: Iterable[HourlyRealTimePrice],
) -> list[HourlyRealTimePrice]:
    """Fill internal hourly gaps with linearly interpolated prices.

    Only gaps bounded by observed hourly rows are filled. Leading and trailing
    hours with no later/earlier observed price remain absent.
    """

    observed = sorted(rows, key=lambda row: row.interval_start)
    if len(observed) < 2:
        return observed

    by_interval = {row.interval_start: row for row in observed}
    filled: list[HourlyRealTimePrice] = []
    cursor = observed[0].interval_start
    last_interval = observed[-1].interval_start
    next_index = 0

    while cursor <= last_interval:
        existing = by_interval.get(cursor)
        if existing is not None:
            filled.append(existing)
            next_index += 1
        else:
            while next_index < len(observed) and observed[next_index].interval_start < cursor:
                next_index += 1
            previous = filled[-1] if filled else None
            next_row = observed[next_index] if next_index < len(observed) else None
            if previous is not None and next_row is not None:
                filled.append(_interpolate_realtime_hour(cursor, previous, next_row))
        cursor += timedelta(hours=1)

    return filled


def _interpolate_realtime_hour(
    interval_start: datetime,
    previous: HourlyRealTimePrice,
    next_row: HourlyRealTimePrice,
) -> HourlyRealTimePrice:
    total_hours = Decimal(
        int((next_row.interval_start - previous.interval_start).total_seconds() // 3600)
    )
    elapsed_hours = Decimal(
        int((interval_start - previous.interval_start).total_seconds() // 3600)
    )
    if total_hours <= 0:
        interpolated_cents = previous.price_cents_per_kwh
    else:
        price_delta = next_row.price_cents_per_kwh - previous.price_cents_per_kwh
        interpolated_cents = previous.price_cents_per_kwh + (price_delta * elapsed_hours / total_hours)

    return HourlyRealTimePrice(
        interval_start=interval_start,
        price_dollars_per_kwh=interpolated_cents / Decimal("100"),
        price_cents_per_kwh=interpolated_cents,
        raw_point_count=0,
        source_bucket_start_local=None,
        source_bucket_end_local=None,
        source_bucket_start_utc=None,
        source_bucket_end_utc=None,
        price_quality="imputed_missing_hour",
        source="imputed_linear_interpolation",
    )


def read_realtime_hourly_csv(path: Path) -> dict[datetime, HourlyRealTimePrice]:
    """Read an existing real-time hourly CSV back into typed rows, keyed by interval_start."""

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows: dict[datetime, HourlyRealTimePrice] = {}
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
            rows[interval_start] = HourlyRealTimePrice(
                interval_start=interval_start,
                price_dollars_per_kwh=Decimal(row["price_dollars_per_kwh"]),
                price_cents_per_kwh=Decimal(row["price_cents_per_kwh"]),
                raw_point_count=int(row["raw_point_count"]),
                source_bucket_start_local=_parse_local(row.get("source_bucket_start_local", "")),
                source_bucket_end_local=_parse_local(row.get("source_bucket_end_local", "")),
                source_bucket_start_utc=_parse_utc(row.get("source_bucket_start_utc", "")),
                source_bucket_end_utc=_parse_utc(row.get("source_bucket_end_utc", "")),
                price_quality=row["price_quality"],
                source=row["source"],
            )
        return rows


def merge_realtime_rows(
    existing: dict[datetime, HourlyRealTimePrice],
    new: Iterable[HourlyRealTimePrice],
) -> list[HourlyRealTimePrice]:
    """Merge freshly fetched hourly rows into existing rows, keyed by interval_start.

    Existing rows are preserved on conflict; only intervals absent from `existing` are added.
    """

    merged = dict(existing)
    for row in new:
        merged.setdefault(row.interval_start, row)
    return [merged[interval_start] for interval_start in sorted(merged)]


def write_realtime_hourly_csv(path: Path, rows: Iterable[HourlyRealTimePrice]) -> None:
    """Write real-time hourly price rows using the project root CSV schema."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "interval_start",
                "date",
                "hour_ending",
                "price",
                "price_dollars_per_kwh",
                "price_cents_per_kwh",
                "raw_point_count",
                "source_bucket_start_local",
                "source_bucket_end_local",
                "source_bucket_start_utc",
                "source_bucket_end_utc",
                "price_quality",
                "source",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "interval_start": row.interval_start.strftime("%Y-%m-%d %H:%M"),
                    "date": row.interval_start.date().isoformat(),
                    "hour_ending": row.hour_ending,
                    "price": _format_decimal(row.price_dollars_per_kwh, places=8),
                    "price_dollars_per_kwh": _format_decimal(row.price_dollars_per_kwh, places=8),
                    "price_cents_per_kwh": _format_decimal(row.price_cents_per_kwh, places=6),
                    "raw_point_count": row.raw_point_count,
                    "source_bucket_start_local": _format_local(row.source_bucket_start_local),
                    "source_bucket_end_local": _format_local(row.source_bucket_end_local),
                    "source_bucket_start_utc": _format_utc(row.source_bucket_start_utc),
                    "source_bucket_end_utc": _format_utc(row.source_bucket_end_utc),
                    "price_quality": row.price_quality,
                    "source": row.source,
                }
            )


def _format_decimal(value: Decimal, *, places: int) -> str:
    return f"{value:.{places}f}"


def _format_local(value: datetime | None) -> str:
    return "" if value is None else value.strftime("%Y-%m-%d %H:%M:%S%z")


def _format_utc(value: datetime | None) -> str:
    return "" if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_local(value: str) -> datetime | None:
    return None if not value else datetime.strptime(value, "%Y-%m-%d %H:%M:%S%z")


def _parse_utc(value: str) -> datetime | None:
    return None if not value else datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
