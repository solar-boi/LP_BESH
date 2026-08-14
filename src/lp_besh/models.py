"""Domain models for hourly load, price, and calculated cost rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class PriceUnit(str, Enum):
    """Supported input units for hourly price files."""

    DOLLARS_PER_KWH = "dollars_per_kwh"
    CENTS_PER_KWH = "cents_per_kwh"
    DOLLARS_PER_MWH = "dollars_per_mwh"


@dataclass(frozen=True)
class HourlyLoad:
    """One customer usage interval normalized to local hour beginning."""

    interval_start: datetime
    kwh: Decimal
    source_row: int
    account_id: str | None = None


@dataclass(frozen=True)
class HourlyPrice:
    """One hourly ComEd price normalized to dollars per kWh."""

    interval_start: datetime
    price_per_kwh: Decimal
    source_row: int


@dataclass(frozen=True)
class JoinedUsageCost:
    """A matched hourly load row with hourly price and calculated cost."""

    interval_start: datetime
    kwh: Decimal
    price_per_kwh: Decimal
    cost: Decimal
    load_source_row: int
    price_source_row: int
    account_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "interval_start": self.interval_start.isoformat(),
            "kwh": str(self.kwh),
            "price_per_kwh": str(self.price_per_kwh),
            "cost": str(self.cost),
            "load_source_row": self.load_source_row,
            "price_source_row": self.price_source_row,
        }


@dataclass(frozen=True)
class CalculationSummary:
    """Aggregate cost metrics for a completed hourly-pricing calculation."""

    requested_hours: int
    matched_hours: int
    missing_price_hours: int
    total_kwh: Decimal
    total_cost: Decimal
    effective_price_per_kwh: Decimal
    first_interval: datetime | None
    last_interval: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_hours": self.requested_hours,
            "matched_hours": self.matched_hours,
            "missing_price_hours": self.missing_price_hours,
            "total_kwh": str(self.total_kwh),
            "total_cost": str(self.total_cost),
            "effective_price_per_kwh": str(self.effective_price_per_kwh),
            "first_interval": self.first_interval.isoformat() if self.first_interval else None,
            "last_interval": self.last_interval.isoformat() if self.last_interval else None,
        }
