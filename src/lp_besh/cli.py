"""Command-line entrypoint for hourly-pricing calculations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lp_besh.calculator import CalculationError, calculate_hourly_costs
from lp_besh.models import PriceUnit
from lp_besh.parsers import (
    BlankKwhPolicy,
    DailyWideLoadCsvSchema,
    DateHourEndingPriceCsvSchema,
    LoadCsvFormat,
    LoadCsvSchema,
    PriceCsvFormat,
    PriceCsvSchema,
    read_load_csv_auto_path,
    read_price_csv_auto_path,
    write_joined_costs_csv_path,
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "calculate":
        return _run_calculate(args)

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lp-besh",
        description="Calculate ComEd hourly-pricing costs from customer interval data.",
    )
    subparsers = parser.add_subparsers(dest="command")

    calculate = subparsers.add_parser("calculate", help="Join hourly load to hourly prices.")
    calculate.add_argument("--load", required=True, type=Path, help="Customer interval CSV path.")
    calculate.add_argument("--prices", required=True, type=Path, help="ComEd hourly price CSV path.")
    calculate.add_argument("--output", required=True, type=Path, help="Joined hourly output CSV path.")
    calculate.add_argument("--summary-output", type=Path, help="Optional JSON summary output path.")
    calculate.add_argument(
        "--load-format",
        choices=[load_format.value for load_format in LoadCsvFormat],
        default=LoadCsvFormat.AUTO.value,
        help="Customer load CSV format. Auto detects long and daily-wide layouts.",
    )
    calculate.add_argument("--load-time-column", default="interval_start")
    calculate.add_argument("--load-kwh-column", default="kwh")
    calculate.add_argument("--load-date-column", default="date")
    calculate.add_argument("--load-account-column")
    calculate.add_argument(
        "--blank-kwh-policy",
        choices=[policy.value for policy in BlankKwhPolicy],
        default=BlankKwhPolicy.SKIP.value,
        help="How daily-wide load files should handle blank hourly kWh cells.",
    )
    calculate.add_argument("--price-time-column", default="interval_start")
    calculate.add_argument(
        "--price-format",
        choices=[price_format.value for price_format in PriceCsvFormat],
        default=PriceCsvFormat.AUTO.value,
        help="ComEd price CSV format. Auto detects timestamp and date/hour-ending layouts.",
    )
    calculate.add_argument(
        "--price-time-convention",
        choices=["interval_start", "hour_ending"],
        default="interval_start",
        help="For timestamp price files, whether timestamps are interval starts or hour endings.",
    )
    calculate.add_argument("--price-date-column", default="date")
    calculate.add_argument("--price-hour-ending-column", default="hour_ending")
    calculate.add_argument("--price-column", default="price")
    calculate.add_argument(
        "--price-unit",
        choices=[unit.value for unit in PriceUnit],
        default=PriceUnit.DOLLARS_PER_KWH.value,
    )
    calculate.add_argument(
        "--allow-missing-prices",
        action="store_true",
        help="Skip unmatched usage hours instead of failing the calculation.",
    )
    return parser


def _run_calculate(args: argparse.Namespace) -> int:
    load_schema = LoadCsvSchema(
        timestamp_column=args.load_time_column,
        kwh_column=args.load_kwh_column,
        account_id_column=args.load_account_column,
    )
    daily_wide_schema = DailyWideLoadCsvSchema(
        date_column=args.load_date_column,
        account_id_column=args.load_account_column,
        blank_kwh_policy=BlankKwhPolicy(args.blank_kwh_policy),
    )
    price_schema = PriceCsvSchema(
        timestamp_column=args.price_time_column,
        price_column=args.price_column,
        price_unit=PriceUnit(args.price_unit),
        time_convention=args.price_time_convention,
    )
    date_hour_ending_price_schema = DateHourEndingPriceCsvSchema(
        date_column=args.price_date_column,
        hour_ending_column=args.price_hour_ending_column,
        price_column=args.price_column,
        price_unit=PriceUnit(args.price_unit),
    )

    try:
        load_rows = read_load_csv_auto_path(
            args.load,
            load_format=LoadCsvFormat(args.load_format),
            long_schema=load_schema,
            daily_wide_schema=daily_wide_schema,
        )
        price_rows = read_price_csv_auto_path(
            args.prices,
            price_format=PriceCsvFormat(args.price_format),
            timestamp_schema=price_schema,
            date_hour_ending_schema=date_hour_ending_price_schema,
        )
        result = calculate_hourly_costs(
            load_rows,
            price_rows,
            allow_missing_prices=args.allow_missing_prices,
        )
        write_joined_costs_csv_path(args.output, result.hourly_rows)

        summary_payload = result.summary.to_dict()
        if args.summary_output:
            args.summary_output.parent.mkdir(parents=True, exist_ok=True)
            args.summary_output.write_text(
                json.dumps(summary_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        print(json.dumps(summary_payload, indent=2, sort_keys=True))
        return 0
    except (CalculationError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
