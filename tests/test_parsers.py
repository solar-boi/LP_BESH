from decimal import Decimal
from io import StringIO
import unittest

from lp_besh.models import PriceUnit
from lp_besh.parsers import (
    BlankKwhPolicy,
    DailyWideLoadCsvSchema,
    LoadCsvFormat,
    LoadCsvSchema,
    PriceCsvSchema,
    read_load_csv,
    read_load_csv_auto,
    read_price_csv_auto,
    read_price_csv,
)


class CsvParserTests(unittest.TestCase):
    def test_reads_load_csv_with_account_column(self) -> None:
        csv_text = "account,interval_start,kwh\nA-1,2024-01-01 00:00,12.5\n"

        rows = read_load_csv(
            StringIO(csv_text),
            LoadCsvSchema(account_id_column="account"),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].account_id, "A-1")
        self.assertEqual(rows[0].kwh, Decimal("12.5"))
        self.assertEqual(rows[0].interval_start.isoformat(), "2024-01-01T00:00:00-06:00")

    def test_normalizes_cents_per_kwh_prices(self) -> None:
        csv_text = "interval_start,price\n2024-01-01 00:00,5.2\n"

        rows = read_price_csv(
            StringIO(csv_text),
            PriceCsvSchema(price_unit=PriceUnit.CENTS_PER_KWH),
        )

        self.assertEqual(rows[0].price_per_kwh, Decimal("0.052"))

    def test_auto_detects_daily_wide_customer_load_csv(self) -> None:
        csv_text = (
            "date,0:00,1:00,2:00,3:00,4:00,5:00,6:00,7:00,8:00,9:00,"
            "10:00,11:00,12:00,13:00,14:00,15:00,16:00,17:00,18:00,"
            "19:00,20:00,21:00,22:00,23:00\n"
            "2024-01-01,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24\n"
        )

        rows = read_load_csv_auto(StringIO(csv_text), load_format=LoadCsvFormat.AUTO)

        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[0].interval_start.isoformat(), "2024-01-01T00:00:00-06:00")
        self.assertEqual(rows[23].interval_start.isoformat(), "2024-01-01T23:00:00-06:00")
        self.assertEqual(rows[23].kwh, Decimal("24"))

    def test_daily_wide_customer_load_skips_blank_cells_by_default(self) -> None:
        csv_text = (
            "date,0:00,1:00,2:00,3:00,4:00,5:00,6:00,7:00,8:00,9:00,"
            "10:00,11:00,12:00,13:00,14:00,15:00,16:00,17:00,18:00,"
            "19:00,20:00,21:00,22:00,23:00\n"
            "2024-03-10,1,,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24\n"
        )

        rows = read_load_csv_auto(
            StringIO(csv_text),
            load_format=LoadCsvFormat.DAILY_WIDE,
            daily_wide_schema=DailyWideLoadCsvSchema(blank_kwh_policy=BlankKwhPolicy.SKIP),
        )

        self.assertEqual(len(rows), 23)
        self.assertEqual(rows[1].interval_start.isoformat(), "2024-03-10T02:00:00-06:00")

    def test_normalizes_dollars_per_mwh_prices(self) -> None:
        csv_text = "interval_start,price\n2024-01-01 00:00,52\n"

        rows = read_price_csv(
            StringIO(csv_text),
            PriceCsvSchema(price_unit=PriceUnit.DOLLARS_PER_MWH),
        )

        self.assertEqual(rows[0].price_per_kwh, Decimal("0.052"))

    def test_auto_detects_date_hour_ending_price_csv(self) -> None:
        csv_text = "date,hour_ending,price\n2024-01-01,1,0.052\n2024-01-01,24,0.061\n"

        rows = read_price_csv_auto(StringIO(csv_text))

        self.assertEqual(rows[0].interval_start.isoformat(), "2024-01-01T00:00:00-06:00")
        self.assertEqual(rows[1].interval_start.isoformat(), "2024-01-01T23:00:00-06:00")

    def test_price_auto_detection_prefers_interval_start_when_available(self) -> None:
        csv_text = (
            "interval_start,date,hour_ending,price\n"
            "2024-01-01 00:00,2024-01-01,1,0.052\n"
        )

        rows = read_price_csv_auto(StringIO(csv_text))

        self.assertEqual(rows[0].interval_start.isoformat(), "2024-01-01T00:00:00-06:00")

    def test_timestamp_price_csv_can_shift_hour_ending_to_interval_start(self) -> None:
        csv_text = "interval_start,price\n2024-01-01 01:00,0.052\n"

        rows = read_price_csv(
            StringIO(csv_text),
            PriceCsvSchema(time_convention="hour_ending"),
        )

        self.assertEqual(rows[0].interval_start.isoformat(), "2024-01-01T00:00:00-06:00")


if __name__ == "__main__":
    unittest.main()
