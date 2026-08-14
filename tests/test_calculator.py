from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
import unittest

from lp_besh.calculator import (
    DuplicateTimestampError,
    MissingPriceDataError,
    calculate_hourly_costs,
)
from lp_besh.models import HourlyLoad, HourlyPrice


CHICAGO = ZoneInfo("America/Chicago")


def hour(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=CHICAGO)


class CalculateHourlyCostsTests(unittest.TestCase):
    def test_calculates_total_cost_and_effective_rate(self) -> None:
        loads = [
            HourlyLoad(hour("2024-01-01T00:00:00"), Decimal("10"), 2),
            HourlyLoad(hour("2024-01-01T01:00:00"), Decimal("20"), 3),
        ]
        prices = [
            HourlyPrice(hour("2024-01-01T00:00:00"), Decimal("0.05"), 2),
            HourlyPrice(hour("2024-01-01T01:00:00"), Decimal("0.10"), 3),
        ]

        result = calculate_hourly_costs(loads, prices)

        self.assertEqual(result.summary.total_kwh, Decimal("30"))
        self.assertEqual(result.summary.total_cost, Decimal("2.50"))
        self.assertEqual(result.summary.effective_price_per_kwh, Decimal("0.08333333333333333333333333333"))
        self.assertEqual(result.summary.matched_hours, 2)

    def test_fails_when_price_hour_is_missing(self) -> None:
        loads = [
            HourlyLoad(hour("2024-01-01T00:00:00"), Decimal("10"), 2),
            HourlyLoad(hour("2024-01-01T01:00:00"), Decimal("20"), 3),
        ]
        prices = [
            HourlyPrice(hour("2024-01-01T00:00:00"), Decimal("0.05"), 2),
        ]

        with self.assertRaises(MissingPriceDataError):
            calculate_hourly_costs(loads, prices)

    def test_fails_on_duplicate_load_hour(self) -> None:
        loads = [
            HourlyLoad(hour("2024-01-01T00:00:00"), Decimal("10"), 2),
            HourlyLoad(hour("2024-01-01T00:00:00"), Decimal("20"), 3),
        ]
        prices = [
            HourlyPrice(hour("2024-01-01T00:00:00"), Decimal("0.05"), 2),
        ]

        with self.assertRaises(DuplicateTimestampError):
            calculate_hourly_costs(loads, prices)


if __name__ == "__main__":
    unittest.main()
