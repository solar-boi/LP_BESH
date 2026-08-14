from datetime import datetime, timezone
from decimal import Decimal
import tempfile
import unittest
from pathlib import Path

from lp_besh.comed_price_builder import (
    FiveMinuteRealTimePrice,
    aggregate_realtime_5min_to_hourly,
    fetch_realtime_5min_prices,
    fill_internal_missing_realtime_hours,
    parse_realtime_5min_payload,
    write_realtime_hourly_csv,
)


class ComedPriceBuilderTests(unittest.TestCase):
    def test_aggregates_partial_5minute_bucket_instead_of_dropping_hour(self) -> None:
        points = [
            FiveMinuteRealTimePrice(datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc), Decimal("2.0")),
            FiveMinuteRealTimePrice(datetime(2024, 1, 1, 6, 5, tzinfo=timezone.utc), Decimal("4.0")),
            FiveMinuteRealTimePrice(datetime(2024, 1, 1, 6, 10, tzinfo=timezone.utc), Decimal("6.0")),
        ]

        rows = aggregate_realtime_5min_to_hourly(points)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].interval_start.isoformat(), "2024-01-01T01:00:00")
        self.assertEqual(rows[0].raw_point_count, 3)
        self.assertEqual(rows[0].price_cents_per_kwh, Decimal("4.0"))
        self.assertEqual(rows[0].price_dollars_per_kwh, Decimal("0.04"))
        self.assertEqual(rows[0].price_quality, "observed_partial")

    def test_fills_internal_missing_hour_with_interpolation(self) -> None:
        rows = aggregate_realtime_5min_to_hourly(
            [
                FiveMinuteRealTimePrice(datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc), Decimal("2.0")),
                FiveMinuteRealTimePrice(datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc), Decimal("6.0")),
            ]
        )

        filled = fill_internal_missing_realtime_hours(rows)

        self.assertEqual([row.interval_start.hour for row in filled], [1, 2, 3])
        self.assertEqual(filled[1].raw_point_count, 0)
        self.assertEqual(filled[1].price_quality, "imputed_missing_hour")
        self.assertEqual(filled[1].price_cents_per_kwh, Decimal("4.0"))

    def test_parses_comed_json_payload(self) -> None:
        payload = [{"millisUTC": "1704096000000", "price": "2.5"}]

        points = parse_realtime_5min_payload(payload)

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].timestamp_utc.isoformat(), "2024-01-01T08:00:00+00:00")
        self.assertEqual(points[0].price_cents_per_kwh, Decimal("2.5"))

    def test_writes_partial_point_count_to_csv(self) -> None:
        rows = aggregate_realtime_5min_to_hourly(
            [
                FiveMinuteRealTimePrice(datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc), Decimal("2.0")),
                FiveMinuteRealTimePrice(datetime(2024, 1, 1, 6, 5, tzinfo=timezone.utc), Decimal("4.0")),
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "realtime.csv"
            write_realtime_hourly_csv(output, rows)
            text = output.read_text(encoding="utf-8")

        self.assertIn("raw_point_count", text)
        self.assertIn(",2,", text)

    def test_empty_api_body_is_treated_as_no_points(self) -> None:
        class EmptyResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b""

        def fake_urlopen(url, timeout, context):
            return EmptyResponse()

        import lp_besh.comed_price_builder as builder

        original_urlopen = builder.urlopen
        builder.urlopen = fake_urlopen
        try:
            points = fetch_realtime_5min_prices(
                source_start_local=datetime(2024, 1, 1, 0, 0),
                source_end_local=datetime(2024, 1, 1, 0, 55),
            )
        finally:
            builder.urlopen = original_urlopen

        self.assertEqual(points, [])


if __name__ == "__main__":
    unittest.main()
