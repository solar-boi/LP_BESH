"""Analysis helpers for the Streamlit hourly-pricing dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import BinaryIO

import pandas as pd

from lp_besh.models import HourlyLoad
from lp_besh.parsers import (
    BlankKwhPolicy,
    DailyWideLoadCsvSchema,
    LoadCsvFormat,
    LoadCsvSchema,
    read_load_csv_auto,
)


@dataclass(frozen=True)
class ScenarioResult:
    """One pricing scenario joined to uploaded customer load."""

    label: str
    hourly: pd.DataFrame
    missing_pairs: pd.DataFrame
    summary: dict[str, float | int | str | None]
    peak_summary: pd.DataFrame


class DashboardDataError(ValueError):
    """Raised when dashboard input data cannot be trusted for cost analysis."""


def load_customer_dataframe(
    uploaded_file: BinaryIO,
    *,
    blank_kwh_policy: BlankKwhPolicy = BlankKwhPolicy.SKIP,
    source_file: str | None = None,
) -> pd.DataFrame:
    """Parse an uploaded customer interval file into a local hourly DataFrame."""

    text = uploaded_file.read().decode("utf-8-sig")
    rows = read_load_csv_auto(
        StringIO(text),
        load_format=LoadCsvFormat.AUTO,
        long_schema=LoadCsvSchema(),
        daily_wide_schema=DailyWideLoadCsvSchema(blank_kwh_policy=blank_kwh_policy),
    )
    if not rows:
        label = f" {source_file!r}" if source_file else ""
        raise DashboardDataError(f"Customer file{label} did not contain any hourly kWh rows")
    return load_rows_to_dataframe(rows, source_file=source_file)


def load_customer_dataframes(
    uploaded_files: list[BinaryIO],
    *,
    blank_kwh_policy: BlankKwhPolicy = BlankKwhPolicy.SKIP,
) -> pd.DataFrame:
    """Parse multiple customer files and aggregate their load by hour."""

    frames = [
        load_customer_dataframe(
            uploaded_file,
            blank_kwh_policy=blank_kwh_policy,
            source_file=getattr(uploaded_file, "name", None),
        )
        for uploaded_file in uploaded_files
    ]
    return aggregate_customer_load_dataframes(frames)


def load_rows_to_dataframe(rows: list[HourlyLoad], *, source_file: str | None = None) -> pd.DataFrame:
    """Convert parsed load rows to a pandas frame keyed by local wall-clock hour."""

    records = [
        {
            "interval_start": row.interval_start.replace(tzinfo=None),
            "kwh": float(row.kwh),
            "account_id": row.account_id,
            "load_source_row": row.source_row,
            "source_file": source_file,
        }
        for row in rows
    ]
    df = pd.DataFrame.from_records(records)
    if df.empty:
        return pd.DataFrame(columns=["interval_start", "kwh", "account_id", "load_source_row", "source_file"])

    _normalize_required_datetime(df, "interval_start", context="customer load rows")
    _normalize_required_number(df, "kwh", context="customer load rows")
    _require_non_negative(df, "kwh", context="customer load rows")
    return df.sort_values("interval_start").reset_index(drop=True)


def aggregate_customer_load_dataframes(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Aggregate parsed customer load frames into one row per interval_start."""

    columns = [
        "interval_start",
        "kwh",
        "source_files",
        "source_file_count",
        "source_row_count",
        "account_ids",
    ]
    non_empty = [frame for frame in frames if not frame.empty]
    if not non_empty:
        return pd.DataFrame(columns=columns)

    combined = pd.concat(non_empty, ignore_index=True)
    _require_columns(combined, ["interval_start", "kwh"], context="customer load frames")
    _normalize_required_datetime(combined, "interval_start", context="customer load frames")
    _normalize_required_number(combined, "kwh", context="customer load frames")
    _require_non_negative(combined, "kwh", context="customer load frames")
    if combined.empty:
        return pd.DataFrame(columns=columns)

    if "source_file" not in combined.columns:
        combined["source_file"] = None
    if "account_id" not in combined.columns:
        combined["account_id"] = None

    aggregated = (
        combined.groupby("interval_start", as_index=False)
        .agg(
            kwh=("kwh", "sum"),
            source_files=("source_file", _join_unique_values),
            source_file_count=("source_file", _count_unique_values),
            source_row_count=("kwh", "count"),
            account_ids=("account_id", _join_unique_values),
        )
        .sort_values("interval_start")
        .reset_index(drop=True)
    )
    return aggregated.loc[:, columns]


def load_price_dataframe(path: Path) -> pd.DataFrame:
    """Load a generated ComEd hourly price CSV for dashboard joins."""

    df = pd.read_csv(path)
    required = {"interval_start", "price"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path.name} is missing required column(s): {', '.join(sorted(missing))}")

    out = df.copy()
    _normalize_required_datetime(out, "interval_start", context=path.name)
    _normalize_required_number(out, "price", context=path.name)
    if "price_cents_per_kwh" in out.columns:
        _normalize_required_number(out, "price_cents_per_kwh", context=path.name)
    else:
        out["price_cents_per_kwh"] = out["price"] * 100

    duplicated = out["interval_start"].duplicated(keep=False)
    if duplicated.any():
        examples = _format_examples(out.loc[duplicated, "interval_start"])
        raise DashboardDataError(f"{path.name} has duplicate price interval_start value(s): {examples}")

    aggregations = {
        "price": ("price", "mean"),
        "price_cents_per_kwh": ("price_cents_per_kwh", "mean"),
    }
    if "raw_point_count" in out.columns:
        _normalize_required_integer(out, "raw_point_count", context=path.name)
        _require_non_negative(out, "raw_point_count", context=path.name)
        aggregations["raw_point_count"] = ("raw_point_count", "sum")
    if "source" in out.columns:
        aggregations["source"] = ("source", _join_unique_values)
    if "price_quality" in out.columns:
        aggregations["price_quality"] = ("price_quality", _join_unique_values)
    else:
        out["price_quality"] = "observed"
        aggregations["price_quality"] = ("price_quality", _join_unique_values)

    out = out.groupby("interval_start", as_index=False).agg(**aggregations)
    out = out.sort_values("interval_start").reset_index(drop=True)
    return out


def calculate_scenario(
    *,
    label: str,
    load_df: pd.DataFrame,
    price_df: pd.DataFrame,
) -> ScenarioResult:
    """Join one price source to load and calculate summary metrics."""

    if load_df.empty:
        hourly = _empty_hourly_result()
    else:
        _require_columns(load_df, ["interval_start", "kwh"], context=f"{label} load data")
        _require_columns(price_df, ["interval_start", "price"], context=f"{label} price data")
        hourly = load_df.merge(
            price_df,
            on="interval_start",
            how="left",
            validate="many_to_one",
        )
        hourly["price_source"] = label
        hourly["cost"] = hourly["kwh"] * hourly["price"]
        hourly["hour_ending"] = hourly["interval_start"].dt.hour + 1
        hourly["date"] = hourly["interval_start"].dt.date
        hourly["is_on_peak"] = classify_on_peak(hourly["interval_start"])
        hourly["period"] = hourly["is_on_peak"].map({True: "On-peak", False: "Off-peak"})

    matched = hourly.dropna(subset=["price", "cost"]).copy()
    missing_pairs = hourly[hourly["price"].isna()].copy()
    total_load_hours = int(len(load_df))
    matched_hours = int(len(matched))
    missing_hours = total_load_hours - matched_hours
    total_kwh = float(matched["kwh"].sum()) if not matched.empty else 0.0
    total_cost = float(matched["cost"].sum()) if not matched.empty else 0.0
    effective_rate = total_cost / total_kwh if total_kwh else 0.0
    peak_summary = build_peak_summary(matched)

    summary: dict[str, float | int | str | None] = {
        "label": label,
        "total_load_hours": total_load_hours,
        "matched_hours": matched_hours,
        "missing_price_hours": missing_hours,
        "coverage_pct": (matched_hours / total_load_hours * 100) if total_load_hours else 0.0,
        "on_peak_matched_hours": _period_hours(peak_summary, "On-peak"),
        "off_peak_matched_hours": _period_hours(peak_summary, "Off-peak"),
        "total_kwh": total_kwh,
        "total_cost": total_cost,
        "effective_price_per_kwh": effective_rate,
        "first_matched_interval": _fmt_timestamp(matched["interval_start"].min()) if not matched.empty else None,
        "last_matched_interval": _fmt_timestamp(matched["interval_start"].max()) if not matched.empty else None,
    }
    return ScenarioResult(
        label=label,
        hourly=hourly,
        missing_pairs=missing_pairs,
        summary=summary,
        peak_summary=peak_summary,
    )


def build_peak_summary(matched: pd.DataFrame) -> pd.DataFrame:
    """Summarize kWh, cost, and weighted rate by on/off-peak period."""

    columns = ["period", "kwh", "cost", "effective_price_per_kwh", "hours"]
    if matched.empty:
        return pd.DataFrame(columns=columns)

    grouped = (
        matched.groupby("period", as_index=False)
        .agg(kwh=("kwh", "sum"), cost=("cost", "sum"), hours=("interval_start", "count"))
        .sort_values("period")
    )
    grouped["effective_price_per_kwh"] = grouped.apply(
        lambda row: row["cost"] / row["kwh"] if row["kwh"] else 0.0,
        axis=1,
    )
    return grouped.loc[:, columns]


def build_summary_frame(results: list[ScenarioResult]) -> pd.DataFrame:
    """Return one row per scenario for display."""

    return pd.DataFrame([result.summary for result in results])


def combine_missing_pairs(results: list[ScenarioResult]) -> pd.DataFrame:
    """Return skipped load-price pairs for all scenarios in tidy form."""

    frames = []
    for result in results:
        missing = result.missing_pairs.copy()
        if missing.empty:
            continue
        missing["scenario"] = result.label
        frames.append(missing)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def combine_hourly_results(results: list[ScenarioResult]) -> pd.DataFrame:
    """Return matched hourly rows for all scenarios in tidy form."""

    frames = []
    for result in results:
        hourly = result.hourly.dropna(subset=["price", "cost"]).copy()
        if hourly.empty:
            continue
        hourly["scenario"] = result.label
        frames.append(hourly)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def monthly_cost_frame(results: list[ScenarioResult]) -> pd.DataFrame:
    """Monthly total cost and kWh by scenario."""

    combined = combine_hourly_results(results)
    if combined.empty:
        return pd.DataFrame(columns=["month", "scenario", "cost", "kwh"])
    combined["month"] = combined["interval_start"].dt.to_period("M").dt.to_timestamp()
    return (
        combined.groupby(["month", "scenario"], as_index=False)
        .agg(cost=("cost", "sum"), kwh=("kwh", "sum"))
        .sort_values(["month", "scenario"])
    )


def hourly_price_profile(results: list[ScenarioResult]) -> pd.DataFrame:
    """Average price by hour ending and scenario."""

    combined = combine_hourly_results(results)
    if combined.empty:
        return pd.DataFrame(columns=["hour_ending", "scenario", "price"])
    return (
        combined.groupby(["hour_ending", "scenario"], as_index=False)
        .agg(price=("price", "mean"))
        .sort_values(["hour_ending", "scenario"])
    )


def daily_cost_frame(results: list[ScenarioResult]) -> pd.DataFrame:
    """Daily total cost by scenario."""

    combined = combine_hourly_results(results)
    if combined.empty:
        return pd.DataFrame(columns=["date", "scenario", "cost"])
    return (
        combined.groupby(["date", "scenario"], as_index=False)
        .agg(cost=("cost", "sum"))
        .sort_values(["date", "scenario"])
    )


def classify_on_peak(timestamps: pd.Series) -> pd.Series:
    """Return True for weekday non-holiday HE 7-23 rows."""

    ts = pd.to_datetime(timestamps, errors="coerce")
    if ts.empty:
        return pd.Series(dtype=bool)

    min_year = int(ts.dt.year.min())
    max_year = int(ts.dt.year.max())
    holidays = nerc_holidays(min_year, max_year)
    dates = ts.dt.date
    is_weekday = ts.dt.weekday < 5
    is_not_holiday = ~dates.isin(holidays)
    is_peak_hour = ts.dt.hour.between(6, 22)
    return is_weekday & is_not_holiday & is_peak_hour


def nerc_holidays(start_year: int, end_year: int) -> set[date]:
    """NERC holidays using the ComEdLP observance convention."""

    holidays: set[date] = set()
    for year in range(start_year, end_year + 1):
        holidays.add(_observe_sunday(date(year, 1, 1)))
        holidays.add(_last_weekday(year, 5, 0))
        holidays.add(_observe_sunday(date(year, 7, 4)))
        holidays.add(_nth_weekday(year, 9, 0, 1))
        holidays.add(_nth_weekday(year, 11, 3, 4))
        holidays.add(_observe_sunday(date(year, 12, 25)))
    return holidays


def _observe_sunday(day: date) -> date:
    return day + timedelta(days=1) if day.weekday() == 6 else day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year, 12, 31)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def _fmt_timestamp(value: pd.Timestamp | datetime) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")


def _join_unique_values(values: pd.Series) -> str:
    unique = sorted({str(value).strip() for value in values.dropna() if str(value).strip()})
    return "|".join(unique)


def _count_unique_values(values: pd.Series) -> int:
    return len({str(value).strip() for value in values.dropna() if str(value).strip()})


def _period_hours(peak_summary: pd.DataFrame, period: str) -> int:
    if peak_summary.empty:
        return 0
    matches = peak_summary.loc[peak_summary["period"] == period, "hours"]
    if matches.empty:
        return 0
    return int(matches.iloc[0])


def _require_columns(frame: pd.DataFrame, columns: list[str], *, context: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise DashboardDataError(f"{context} is missing required column(s): {', '.join(missing)}")


def _normalize_required_datetime(frame: pd.DataFrame, column: str, *, context: str) -> None:
    parsed = pd.to_datetime(frame[column], errors="coerce")
    invalid = parsed.isna()
    if invalid.any():
        examples = _format_examples(frame.loc[invalid, column])
        raise DashboardDataError(f"{context} has invalid {column} value(s): {examples}")
    frame[column] = parsed


def _normalize_required_number(frame: pd.DataFrame, column: str, *, context: str) -> None:
    parsed = pd.to_numeric(frame[column], errors="coerce")
    invalid = parsed.isna()
    if invalid.any():
        examples = _format_examples(frame.loc[invalid, column])
        raise DashboardDataError(f"{context} has invalid {column} value(s): {examples}")
    frame[column] = parsed


def _normalize_required_integer(frame: pd.DataFrame, column: str, *, context: str) -> None:
    parsed = pd.to_numeric(frame[column], errors="coerce")
    invalid = parsed.isna() | (parsed % 1 != 0)
    if invalid.any():
        examples = _format_examples(frame.loc[invalid, column])
        raise DashboardDataError(f"{context} has invalid {column} integer value(s): {examples}")
    frame[column] = parsed.astype(int)


def _require_non_negative(frame: pd.DataFrame, column: str, *, context: str) -> None:
    negative = frame[column] < 0
    if negative.any():
        examples = _format_examples(frame.loc[negative, column])
        raise DashboardDataError(f"{context} has negative {column} value(s): {examples}")


def _format_examples(values: pd.Series, *, limit: int = 10) -> str:
    unique = []
    for value in values:
        text = _fmt_timestamp(value) if isinstance(value, (pd.Timestamp, datetime)) else str(value)
        if text not in unique:
            unique.append(text)
        if len(unique) == limit:
            break
    suffix = "" if len(values) <= limit else f", and {len(values) - limit} more"
    return ", ".join(unique) + suffix


def _empty_hourly_result() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "interval_start",
            "kwh",
            "account_id",
            "load_source_row",
            "source_files",
            "source_file_count",
            "source_row_count",
            "account_ids",
            "price",
            "price_cents_per_kwh",
            "price_source",
            "cost",
            "hour_ending",
            "date",
            "is_on_peak",
            "period",
        ]
    )
