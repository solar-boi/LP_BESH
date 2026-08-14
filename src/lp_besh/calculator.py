"""Core hourly-pricing calculation logic."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from lp_besh.models import CalculationSummary, HourlyLoad, HourlyPrice, JoinedUsageCost


class CalculationError(ValueError):
    """Raised when usage and pricing data cannot produce a valid calculation."""


class DuplicateTimestampError(CalculationError):
    """Raised when a file contains more than one row for the same interval."""


class MissingPriceDataError(CalculationError):
    """Raised when one or more usage hours do not have matching price data."""


@dataclass(frozen=True)
class CalculationResult:
    """Detailed hourly results plus aggregate summary metrics."""

    hourly_rows: list[JoinedUsageCost]
    summary: CalculationSummary


def calculate_hourly_costs(
    load_rows: Iterable[HourlyLoad],
    price_rows: Iterable[HourlyPrice],
    *,
    allow_missing_prices: bool = False,
) -> CalculationResult:
    """Join load and price rows by hour and calculate cost metrics.

    The join is intentionally strict. Missing prices and duplicate hourly
    timestamps are data-quality issues that can otherwise bias the effective
    price per kWh.
    """

    loads = list(load_rows)
    prices = list(price_rows)
    price_by_hour = _index_prices(prices)
    _validate_unique_load_hours(loads)

    joined: list[JoinedUsageCost] = []
    missing_hours: list[datetime] = []

    for load in sorted(loads, key=lambda row: row.interval_start):
        price = price_by_hour.get(load.interval_start)
        if price is None:
            missing_hours.append(load.interval_start)
            continue

        joined.append(
            JoinedUsageCost(
                interval_start=load.interval_start,
                kwh=load.kwh,
                price_per_kwh=price.price_per_kwh,
                cost=load.kwh * price.price_per_kwh,
                account_id=load.account_id,
                load_source_row=load.source_row,
                price_source_row=price.source_row,
            )
        )

    if missing_hours and not allow_missing_prices:
        formatted = ", ".join(hour.isoformat() for hour in missing_hours[:10])
        suffix = "" if len(missing_hours) <= 10 else f", and {len(missing_hours) - 10} more"
        raise MissingPriceDataError(
            f"{len(missing_hours)} usage hour(s) did not have matching price data: {formatted}{suffix}"
        )

    summary = _build_summary(loads=loads, joined=joined, missing_hours=missing_hours)
    return CalculationResult(hourly_rows=joined, summary=summary)


def _index_prices(price_rows: list[HourlyPrice]) -> dict[datetime, HourlyPrice]:
    by_hour: dict[datetime, HourlyPrice] = {}
    duplicates: list[datetime] = []

    for row in price_rows:
        if row.interval_start in by_hour:
            duplicates.append(row.interval_start)
        by_hour[row.interval_start] = row

    if duplicates:
        formatted = ", ".join(hour.isoformat() for hour in sorted(set(duplicates))[:10])
        raise DuplicateTimestampError(f"Duplicate price timestamp(s): {formatted}")

    return by_hour


def _validate_unique_load_hours(load_rows: list[HourlyLoad]) -> None:
    seen: set[datetime] = set()
    duplicates: list[datetime] = []

    for row in load_rows:
        if row.interval_start in seen:
            duplicates.append(row.interval_start)
        seen.add(row.interval_start)

    if duplicates:
        formatted = ", ".join(hour.isoformat() for hour in sorted(set(duplicates))[:10])
        raise DuplicateTimestampError(f"Duplicate load timestamp(s): {formatted}")


def _build_summary(
    *,
    loads: list[HourlyLoad],
    joined: list[JoinedUsageCost],
    missing_hours: list[datetime],
) -> CalculationSummary:
    total_kwh = sum((row.kwh for row in joined), Decimal("0"))
    total_cost = sum((row.cost for row in joined), Decimal("0"))
    effective_price = Decimal("0") if total_kwh == 0 else total_cost / total_kwh
    intervals = [row.interval_start for row in joined]

    return CalculationSummary(
        requested_hours=len(loads),
        matched_hours=len(joined),
        missing_price_hours=len(missing_hours),
        total_kwh=total_kwh,
        total_cost=total_cost,
        effective_price_per_kwh=effective_price,
        first_interval=min(intervals) if intervals else None,
        last_interval=max(intervals) if intervals else None,
    )
