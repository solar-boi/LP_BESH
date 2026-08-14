import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_comed_day_ahead_prices as day_ahead


class DayAheadMergeTests(unittest.TestCase):
    def test_merge_preserves_existing_and_adds_missing(self) -> None:
        existing = {
            datetime(2024, 1, 1, 0): {"interval_start": "2024-01-01 00:00", "price": "0.010"},
        }
        fetched = {
            datetime(2024, 1, 1, 0): {"interval_start": "2024-01-01 00:00", "price": "0.099"},
            datetime(2024, 1, 1, 1): {"interval_start": "2024-01-01 01:00", "price": "0.020"},
        }

        merged, added, replaced = day_ahead.merge_day_ahead_rows(existing, fetched)

        self.assertEqual(merged[datetime(2024, 1, 1, 0)]["price"], "0.010")
        self.assertEqual(merged[datetime(2024, 1, 1, 1)]["price"], "0.020")
        self.assertEqual(added, 1)
        self.assertEqual(replaced, 0)

    def test_merge_with_replace_existing_overwrites_conflicts(self) -> None:
        existing = {
            datetime(2024, 1, 1, 0): {"interval_start": "2024-01-01 00:00", "price": "0.010"},
        }
        fetched = {
            datetime(2024, 1, 1, 0): {"interval_start": "2024-01-01 00:00", "price": "0.099"},
        }

        merged, added, replaced = day_ahead.merge_day_ahead_rows(existing, fetched, replace_existing=True)

        self.assertEqual(merged[datetime(2024, 1, 1, 0)]["price"], "0.099")
        self.assertEqual(added, 0)
        self.assertEqual(replaced, 1)

    def test_parse_day_ahead_payload(self) -> None:
        payload = "[Date.UTC(2024,0,1,0,0,0), 5.1],[Date.UTC(2024,0,1,1,0,0), 4.6]"

        prices = day_ahead.parse_day_ahead_payload(payload)

        self.assertEqual(len(prices), 2)
        self.assertEqual(prices[0].interval_start, datetime(2024, 1, 1, 0))
        self.assertEqual(prices[0].price_cents_per_kwh, Decimal("5.1"))
        self.assertEqual(prices[0].hour_ending, 1)

    def test_row_for_price_and_read_existing_rows_round_trip(self) -> None:
        price = day_ahead.DayAheadPrice(interval_start=datetime(2024, 1, 1, 0), price_cents_per_kwh=Decimal("5.1"))
        row = day_ahead.row_for_price(price)
        rows = {price.interval_start: row}

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "day_ahead.csv"
            day_ahead.write_rows(output, rows)
            read_back = day_ahead.read_existing_rows(output)

        self.assertEqual(read_back[price.interval_start]["price_cents_per_kwh"], "5.100")


if __name__ == "__main__":
    unittest.main()
