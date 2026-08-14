"""CSV parsing and writing helpers for hourly-pricing inputs."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

from lp_besh.models import HourlyLoad, HourlyPrice, JoinedUsageCost, PriceUnit


class CsvDataError(ValueError):
    """Raised when a CSV file does not match the configured schema."""


class LoadCsvFormat(str, Enum):
    """Supported customer load CSV layouts."""

    AUTO = "auto"
    LONG = "long"
    DAILY_WIDE = "daily_wide"


class BlankKwhPolicy(str, Enum):
    """How to handle blank kWh cells in daily-wide load files."""

    ERROR = "error"
    SKIP = "skip"
    ZERO = "zero"


class PriceCsvFormat(str, Enum):
    """Supported ComEd hourly price CSV layouts."""

    AUTO = "auto"
    TIMESTAMP = "timestamp"
    DATE_HOUR_ENDING = "date_hour_ending"


DAILY_WIDE_HOUR_COLUMNS = tuple(f"{hour}:00" for hour in range(24))


@dataclass(frozen=True)
class LoadCsvSchema:
    """Column mapping for a customer interval CSV."""

    timestamp_column: str = "interval_start"
    kwh_column: str = "kwh"
    account_id_column: str | None = None
    timezone: str = "America/Chicago"


@dataclass(frozen=True)
class DailyWideLoadCsvSchema:
    """Column mapping for one-row-per-day CSVs with 24 hourly kWh columns."""

    date_column: str = "date"
    hour_columns: tuple[str, ...] = DAILY_WIDE_HOUR_COLUMNS
    account_id_column: str | None = None
    timezone: str = "America/Chicago"
    blank_kwh_policy: BlankKwhPolicy = BlankKwhPolicy.SKIP


@dataclass(frozen=True)
class PriceCsvSchema:
    """Column mapping for a ComEd hourly price CSV."""

    timestamp_column: str = "interval_start"
    price_column: str = "price"
    price_unit: PriceUnit = PriceUnit.DOLLARS_PER_KWH
    timezone: str = "America/Chicago"
    time_convention: str = "interval_start"


@dataclass(frozen=True)
class DateHourEndingPriceCsvSchema:
    """Column mapping for ComEd price CSVs with separate date and hour-ending fields."""

    date_column: str = "date"
    hour_ending_column: str = "hour_ending"
    price_column: str = "price"
    price_unit: PriceUnit = PriceUnit.DOLLARS_PER_KWH
    timezone: str = "America/Chicago"


def read_load_csv_auto_path(
    path: Path,
    *,
    load_format: LoadCsvFormat = LoadCsvFormat.AUTO,
    long_schema: LoadCsvSchema | None = None,
    daily_wide_schema: DailyWideLoadCsvSchema | None = None,
) -> list[HourlyLoad]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return read_load_csv_auto(
            file,
            load_format=load_format,
            long_schema=long_schema,
            daily_wide_schema=daily_wide_schema,
        )


def read_load_csv_auto(
    file: TextIO,
    *,
    load_format: LoadCsvFormat = LoadCsvFormat.AUTO,
    long_schema: LoadCsvSchema | None = None,
    daily_wide_schema: DailyWideLoadCsvSchema | None = None,
) -> list[HourlyLoad]:
    long_schema = long_schema or LoadCsvSchema()
    daily_wide_schema = daily_wide_schema or DailyWideLoadCsvSchema()

    if load_format == LoadCsvFormat.LONG:
        return read_load_csv(file, long_schema)
    if load_format == LoadCsvFormat.DAILY_WIDE:
        return read_daily_wide_load_csv(file, daily_wide_schema)
    if load_format != LoadCsvFormat.AUTO:
        raise CsvDataError(f"Unsupported load format: {load_format}")

    detected_format = detect_load_csv_format(
        file,
        long_schema=long_schema,
        daily_wide_schema=daily_wide_schema,
    )
    return read_load_csv_auto(
        file,
        load_format=detected_format,
        long_schema=long_schema,
        daily_wide_schema=daily_wide_schema,
    )


def detect_load_csv_format(
    file: TextIO,
    *,
    long_schema: LoadCsvSchema | None = None,
    daily_wide_schema: DailyWideLoadCsvSchema | None = None,
) -> LoadCsvFormat:
    long_schema = long_schema or LoadCsvSchema()
    daily_wide_schema = daily_wide_schema or DailyWideLoadCsvSchema()
    position = file.tell()
    try:
        reader = csv.reader(file)
        header = next(reader, [])
    finally:
        file.seek(position)

    columns = set(header)
    if {long_schema.timestamp_column, long_schema.kwh_column}.issubset(columns):
        return LoadCsvFormat.LONG
    if {daily_wide_schema.date_column, *daily_wide_schema.hour_columns}.issubset(columns):
        return LoadCsvFormat.DAILY_WIDE

    raise CsvDataError(
        "Could not detect customer load format. Expected either "
        f"{long_schema.timestamp_column!r}/{long_schema.kwh_column!r} columns "
        f"or {daily_wide_schema.date_column!r} plus hourly columns."
    )


def read_load_csv_path(path: Path, schema: LoadCsvSchema | None = None) -> list[HourlyLoad]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return read_load_csv(file, schema or LoadCsvSchema())


def read_price_csv_path(path: Path, schema: PriceCsvSchema | None = None) -> list[HourlyPrice]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return read_price_csv(file, schema or PriceCsvSchema())


def read_price_csv_auto_path(
    path: Path,
    *,
    price_format: PriceCsvFormat = PriceCsvFormat.AUTO,
    timestamp_schema: PriceCsvSchema | None = None,
    date_hour_ending_schema: DateHourEndingPriceCsvSchema | None = None,
) -> list[HourlyPrice]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return read_price_csv_auto(
            file,
            price_format=price_format,
            timestamp_schema=timestamp_schema,
            date_hour_ending_schema=date_hour_ending_schema,
        )


def read_load_csv(file: TextIO, schema: LoadCsvSchema | None = None) -> list[HourlyLoad]:
    schema = schema or LoadCsvSchema()
    rows = csv.DictReader(file)
    _require_columns(rows, [schema.timestamp_column, schema.kwh_column])
    if schema.account_id_column:
        _require_columns(rows, [schema.account_id_column])

    parsed: list[HourlyLoad] = []
    for row_number, row in enumerate(rows, start=2):
        interval_start = _parse_timestamp(
            row.get(schema.timestamp_column, ""),
            timezone=schema.timezone,
            row_number=row_number,
            column=schema.timestamp_column,
        )
        kwh = _parse_decimal(
            row.get(schema.kwh_column, ""),
            row_number=row_number,
            column=schema.kwh_column,
        )
        if kwh < 0:
            raise CsvDataError(f"row {row_number}: kWh cannot be negative")

        account_id = row.get(schema.account_id_column, "").strip() if schema.account_id_column else None
        parsed.append(
            HourlyLoad(
                interval_start=interval_start,
                kwh=kwh,
                account_id=account_id or None,
                source_row=row_number,
            )
        )
    return parsed


def read_daily_wide_load_csv(
    file: TextIO,
    schema: DailyWideLoadCsvSchema | None = None,
) -> list[HourlyLoad]:
    schema = schema or DailyWideLoadCsvSchema()
    rows = csv.DictReader(file)
    _require_columns(rows, [schema.date_column, *schema.hour_columns])
    if schema.account_id_column:
        _require_columns(rows, [schema.account_id_column])

    parsed: list[HourlyLoad] = []
    local_zone = ZoneInfo(schema.timezone)
    for row_number, row in enumerate(rows, start=2):
        row_date = _parse_date(
            row.get(schema.date_column, ""),
            row_number=row_number,
            column=schema.date_column,
        )
        account_id = row.get(schema.account_id_column, "").strip() if schema.account_id_column else None

        for hour, column in enumerate(schema.hour_columns):
            raw_kwh = row.get(column, "")
            if not raw_kwh.strip():
                if schema.blank_kwh_policy == BlankKwhPolicy.SKIP:
                    continue
                if schema.blank_kwh_policy == BlankKwhPolicy.ZERO:
                    raw_kwh = "0"
                elif schema.blank_kwh_policy != BlankKwhPolicy.ERROR:
                    raise CsvDataError(f"Unsupported blank kWh policy: {schema.blank_kwh_policy}")

            kwh = _parse_decimal(raw_kwh, row_number=row_number, column=column)
            if kwh < 0:
                raise CsvDataError(f"row {row_number}: {column} kWh cannot be negative")

            parsed.append(
                HourlyLoad(
                    interval_start=datetime(
                        row_date.year,
                        row_date.month,
                        row_date.day,
                        hour,
                        tzinfo=local_zone,
                    ),
                    kwh=kwh,
                    account_id=account_id or None,
                    source_row=row_number,
                )
            )

    return parsed


def read_price_csv(file: TextIO, schema: PriceCsvSchema | None = None) -> list[HourlyPrice]:
    schema = schema or PriceCsvSchema()
    rows = csv.DictReader(file)
    _require_columns(rows, [schema.timestamp_column, schema.price_column])

    parsed: list[HourlyPrice] = []
    for row_number, row in enumerate(rows, start=2):
        interval_start = _parse_timestamp(
            row.get(schema.timestamp_column, ""),
            timezone=schema.timezone,
            row_number=row_number,
            column=schema.timestamp_column,
        )
        interval_start = _normalize_timestamp_convention(
            interval_start,
            time_convention=schema.time_convention,
            row_number=row_number,
            column=schema.timestamp_column,
        )
        raw_price = _parse_decimal(
            row.get(schema.price_column, ""),
            row_number=row_number,
            column=schema.price_column,
        )
        parsed.append(
            HourlyPrice(
                interval_start=interval_start,
                price_per_kwh=_normalize_price(raw_price, schema.price_unit),
                source_row=row_number,
            )
        )
    return parsed


def read_price_csv_auto(
    file: TextIO,
    *,
    price_format: PriceCsvFormat = PriceCsvFormat.AUTO,
    timestamp_schema: PriceCsvSchema | None = None,
    date_hour_ending_schema: DateHourEndingPriceCsvSchema | None = None,
) -> list[HourlyPrice]:
    timestamp_schema = timestamp_schema or PriceCsvSchema()
    date_hour_ending_schema = date_hour_ending_schema or DateHourEndingPriceCsvSchema()

    if price_format == PriceCsvFormat.TIMESTAMP:
        return read_price_csv(file, timestamp_schema)
    if price_format == PriceCsvFormat.DATE_HOUR_ENDING:
        return read_date_hour_ending_price_csv(file, date_hour_ending_schema)
    if price_format != PriceCsvFormat.AUTO:
        raise CsvDataError(f"Unsupported price format: {price_format}")

    detected_format = detect_price_csv_format(
        file,
        timestamp_schema=timestamp_schema,
        date_hour_ending_schema=date_hour_ending_schema,
    )
    return read_price_csv_auto(
        file,
        price_format=detected_format,
        timestamp_schema=timestamp_schema,
        date_hour_ending_schema=date_hour_ending_schema,
    )


def detect_price_csv_format(
    file: TextIO,
    *,
    timestamp_schema: PriceCsvSchema | None = None,
    date_hour_ending_schema: DateHourEndingPriceCsvSchema | None = None,
) -> PriceCsvFormat:
    timestamp_schema = timestamp_schema or PriceCsvSchema()
    date_hour_ending_schema = date_hour_ending_schema or DateHourEndingPriceCsvSchema()
    position = file.tell()
    try:
        reader = csv.reader(file)
        header = next(reader, [])
    finally:
        file.seek(position)

    columns = set(header)
    if {timestamp_schema.timestamp_column, timestamp_schema.price_column}.issubset(columns):
        return PriceCsvFormat.TIMESTAMP
    if {
        date_hour_ending_schema.date_column,
        date_hour_ending_schema.hour_ending_column,
        date_hour_ending_schema.price_column,
    }.issubset(columns):
        return PriceCsvFormat.DATE_HOUR_ENDING

    raise CsvDataError(
        "Could not detect ComEd price format. Expected either "
        f"{timestamp_schema.timestamp_column!r}/{timestamp_schema.price_column!r} columns "
        f"or {date_hour_ending_schema.date_column!r}/"
        f"{date_hour_ending_schema.hour_ending_column!r}/"
        f"{date_hour_ending_schema.price_column!r} columns."
    )


def read_date_hour_ending_price_csv(
    file: TextIO,
    schema: DateHourEndingPriceCsvSchema | None = None,
) -> list[HourlyPrice]:
    schema = schema or DateHourEndingPriceCsvSchema()
    rows = csv.DictReader(file)
    _require_columns(rows, [schema.date_column, schema.hour_ending_column, schema.price_column])

    parsed: list[HourlyPrice] = []
    local_zone = ZoneInfo(schema.timezone)
    for row_number, row in enumerate(rows, start=2):
        row_date = _parse_date(
            row.get(schema.date_column, ""),
            row_number=row_number,
            column=schema.date_column,
        )
        hour_ending = _parse_hour_ending(
            row.get(schema.hour_ending_column, ""),
            row_number=row_number,
            column=schema.hour_ending_column,
        )
        raw_price = _parse_decimal(
            row.get(schema.price_column, ""),
            row_number=row_number,
            column=schema.price_column,
        )
        interval_start = datetime.combine(
            row_date,
            time(hour_ending - 1, 0),
            tzinfo=local_zone,
        )
        parsed.append(
            HourlyPrice(
                interval_start=interval_start,
                price_per_kwh=_normalize_price(raw_price, schema.price_unit),
                source_row=row_number,
            )
        )

    return parsed


def write_joined_costs_csv_path(path: Path, rows: Iterable[JoinedUsageCost]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        write_joined_costs_csv(file, rows)


def write_joined_costs_csv(file: TextIO, rows: Iterable[JoinedUsageCost]) -> None:
    fieldnames = [
        "account_id",
        "interval_start",
        "kwh",
        "price_per_kwh",
        "cost",
        "load_source_row",
        "price_source_row",
    ]
    writer = csv.DictWriter(file, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row.to_dict())


def _require_columns(reader: csv.DictReader, columns: list[str]) -> None:
    fieldnames = reader.fieldnames or []
    missing = [column for column in columns if column not in fieldnames]
    if missing:
        raise CsvDataError(f"CSV is missing required column(s): {', '.join(missing)}")


def _parse_timestamp(value: str, *, timezone: str, row_number: int, column: str) -> datetime:
    text = value.strip()
    if not text:
        raise CsvDataError(f"row {row_number}: {column} is blank")

    parsed = _parse_known_timestamp(text, row_number=row_number, column=column)
    local_zone = ZoneInfo(timezone)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_zone)
    else:
        parsed = parsed.astimezone(local_zone)

    if parsed.minute != 0 or parsed.second != 0 or parsed.microsecond != 0:
        raise CsvDataError(f"row {row_number}: {column} must be an exact hourly timestamp")

    return parsed


def _parse_known_timestamp(value: str, *, row_number: int, column: str) -> datetime:
    normalized = value.replace("Z", "+00:00")

    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass

    formats = [
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M",
        "%m/%d/%y %H:%M",
        "%Y-%m-%d %I:%M %p",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%y %I:%M %p",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    raise CsvDataError(f"row {row_number}: cannot parse {column} timestamp {value!r}")


def _parse_date(value: str, *, row_number: int, column: str) -> date:
    text = value.strip()
    if not text:
        raise CsvDataError(f"row {row_number}: {column} is blank")

    formats = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    raise CsvDataError(f"row {row_number}: cannot parse {column} date {value!r}")


def _parse_hour_ending(value: str, *, row_number: int, column: str) -> int:
    text = value.strip()
    if not text:
        raise CsvDataError(f"row {row_number}: {column} is blank")

    try:
        raw_hour_ending = Decimal(text)
    except InvalidOperation as exc:
        raise CsvDataError(f"row {row_number}: cannot parse {column} hour ending {value!r}") from exc

    if raw_hour_ending != raw_hour_ending.to_integral_value():
        raise CsvDataError(f"row {row_number}: {column} must be a whole hour ending")

    hour_ending = int(raw_hour_ending)
    if hour_ending < 1 or hour_ending > 24:
        raise CsvDataError(f"row {row_number}: {column} must be between 1 and 24")

    return hour_ending


def _normalize_timestamp_convention(
    timestamp: datetime,
    *,
    time_convention: str,
    row_number: int,
    column: str,
) -> datetime:
    if time_convention == "interval_start":
        return timestamp
    if time_convention == "hour_ending":
        return timestamp - timedelta(hours=1)
    raise CsvDataError(
        f"row {row_number}: unsupported time convention {time_convention!r} for {column}"
    )


def _parse_decimal(value: str, *, row_number: int, column: str) -> Decimal:
    text = value.strip().replace("$", "").replace(",", "")
    if not text:
        raise CsvDataError(f"row {row_number}: {column} is blank")

    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]

    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise CsvDataError(f"row {row_number}: cannot parse {column} decimal {value!r}") from exc


def _normalize_price(value: Decimal, unit: PriceUnit) -> Decimal:
    if unit == PriceUnit.DOLLARS_PER_KWH:
        return value
    if unit == PriceUnit.CENTS_PER_KWH:
        return value / Decimal("100")
    if unit == PriceUnit.DOLLARS_PER_MWH:
        return value / Decimal("1000")
    raise CsvDataError(f"Unsupported price unit: {unit}")
