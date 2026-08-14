from datetime import datetime
from io import BytesIO
import unittest

import pandas as pd

from lp_besh.dashboard_analysis import (
    DashboardDataError,
    aggregate_customer_load_dataframes,
    calculate_scenario,
    classify_on_peak,
    combine_missing_pairs,
    load_price_dataframe,
    load_customer_dataframes,
)


class NamedBytesIO(BytesIO):
    def __init__(self, content: bytes, name: str) -> None:
        super().__init__(content)
        self.name = name


class DashboardAnalysisTests(unittest.TestCase):
    def test_on_peak_uses_weekday_he_7_to_23_and_holidays_off_peak(self) -> None:
        timestamps = pd.Series(
            [
                datetime(2024, 1, 2, 5),  # HE 6
                datetime(2024, 1, 2, 6),  # HE 7
                datetime(2024, 1, 2, 22),  # HE 23
                datetime(2024, 1, 2, 23),  # HE 24
                datetime(2024, 1, 1, 12),  # New Years Day
                datetime(2024, 1, 6, 12),  # Saturday
            ]
        )

        self.assertEqual(
            classify_on_peak(timestamps).tolist(),
            [False, True, True, False, False, False],
        )

    def test_calculate_scenario_skips_and_reports_missing_prices(self) -> None:
        load_df = pd.DataFrame(
            {
                "interval_start": pd.to_datetime(["2024-01-02 06:00", "2024-01-02 07:00"]),
                "kwh": [10.0, 20.0],
                "account_id": [None, None],
                "load_source_row": [2, 3],
            }
        )
        price_df = pd.DataFrame(
            {
                "interval_start": pd.to_datetime(["2024-01-02 06:00"]),
                "price": [0.05],
                "price_cents_per_kwh": [5.0],
            }
        )

        result = calculate_scenario(label="Day-ahead", load_df=load_df, price_df=price_df)

        self.assertEqual(result.summary["matched_hours"], 1)
        self.assertEqual(result.summary["missing_price_hours"], 1)
        self.assertEqual(result.summary["coverage_pct"], 50.0)
        self.assertEqual(result.summary["on_peak_matched_hours"], 1)
        self.assertEqual(result.summary["off_peak_matched_hours"], 0)
        self.assertEqual(result.summary["total_cost"], 0.5)
        self.assertEqual(len(result.missing_pairs), 1)
        self.assertEqual(result.missing_pairs.iloc[0]["interval_start"], pd.Timestamp("2024-01-02 07:00"))

        missing = combine_missing_pairs([result])
        self.assertEqual(missing.iloc[0]["scenario"], "Day-ahead")
        self.assertEqual(missing.iloc[0]["period"], "On-peak")

    def test_calculate_scenario_summarizes_fully_matched_cost(self) -> None:
        load_df = pd.DataFrame(
            {
                "interval_start": pd.to_datetime(["2024-01-02 06:00", "2024-01-02 07:00"]),
                "kwh": [10.0, 20.0],
                "account_id": [None, None],
                "load_source_row": [2, 3],
            }
        )
        price_df = pd.DataFrame(
            {
                "interval_start": pd.to_datetime(["2024-01-02 06:00", "2024-01-02 07:00"]),
                "price": [0.05, 0.10],
                "price_cents_per_kwh": [5.0, 10.0],
            }
        )

        result = calculate_scenario(label="Day-ahead", load_df=load_df, price_df=price_df)

        self.assertEqual(result.summary["matched_hours"], 2)
        self.assertEqual(result.summary["missing_price_hours"], 0)
        self.assertEqual(result.summary["on_peak_matched_hours"], 2)
        self.assertEqual(result.summary["off_peak_matched_hours"], 0)
        self.assertEqual(result.summary["total_cost"], 2.5)
        self.assertEqual(result.summary["effective_price_per_kwh"], 2.5 / 30.0)
        self.assertTrue(result.missing_pairs.empty)

    def test_aggregates_customer_load_dataframes_by_hour(self) -> None:
        first = pd.DataFrame(
            {
                "interval_start": pd.to_datetime(["2024-01-02 06:00", "2024-01-02 07:00"]),
                "kwh": [10.0, 20.0],
                "account_id": ["A", "A"],
                "load_source_row": [2, 3],
                "source_file": ["a.csv", "a.csv"],
            }
        )
        second = pd.DataFrame(
            {
                "interval_start": pd.to_datetime(["2024-01-02 06:00", "2024-01-02 08:00"]),
                "kwh": [5.0, 30.0],
                "account_id": ["B", "B"],
                "load_source_row": [2, 3],
                "source_file": ["b.csv", "b.csv"],
            }
        )

        aggregated = aggregate_customer_load_dataframes([first, second])

        self.assertEqual(len(aggregated), 3)
        six_am = aggregated.loc[aggregated["interval_start"] == pd.Timestamp("2024-01-02 06:00")].iloc[0]
        self.assertEqual(six_am["kwh"], 15.0)
        self.assertEqual(six_am["source_files"], "a.csv|b.csv")
        self.assertEqual(six_am["source_file_count"], 2)
        self.assertEqual(six_am["source_row_count"], 2)
        self.assertEqual(six_am["account_ids"], "A|B")

    def test_load_customer_dataframes_parses_and_aggregates_multiple_uploads(self) -> None:
        first = NamedBytesIO(b"interval_start,kwh\n2024-01-02 06:00,10\n", "first.csv")
        second = NamedBytesIO(b"interval_start,kwh\n2024-01-02 06:00,5\n", "second.csv")

        aggregated = load_customer_dataframes([first, second])

        self.assertEqual(len(aggregated), 1)
        self.assertEqual(aggregated.iloc[0]["kwh"], 15.0)
        self.assertEqual(aggregated.iloc[0]["source_files"], "first.csv|second.csv")

    def test_aggregate_customer_load_dataframes_fails_on_invalid_dataframe_values(self) -> None:
        frame = pd.DataFrame(
            {
                "interval_start": ["not-a-date"],
                "kwh": [10.0],
            }
        )

        with self.assertRaisesRegex(DashboardDataError, "invalid interval_start"):
            aggregate_customer_load_dataframes([frame])

    def test_load_customer_dataframes_fails_on_empty_customer_file(self) -> None:
        uploaded = NamedBytesIO(b"interval_start,kwh\n", "empty.csv")

        with self.assertRaisesRegex(DashboardDataError, "did not contain any hourly kWh rows"):
            load_customer_dataframes([uploaded])

    def test_load_price_dataframe_fails_on_duplicate_price_hours(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "prices.csv"
            path.write_text(
                "interval_start,price\n"
                "2024-01-02 06:00,0.05\n"
                "2024-01-02 06:00,0.06\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(DashboardDataError, "duplicate price interval_start"):
                load_price_dataframe(path)

    def test_load_price_dataframe_fails_on_invalid_price_values(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "prices.csv"
            path.write_text("interval_start,price\n2024-01-02 06:00,not-a-price\n", encoding="utf-8")

            with self.assertRaisesRegex(DashboardDataError, "invalid price"):
                load_price_dataframe(path)

    def test_load_price_dataframe_fails_on_non_integer_raw_point_count(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "prices.csv"
            path.write_text(
                "interval_start,price,raw_point_count\n"
                "2024-01-02 06:00,0.05,1.5\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(DashboardDataError, "invalid raw_point_count integer"):
                load_price_dataframe(path)


if __name__ == "__main__":
    unittest.main()
