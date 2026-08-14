"""Streamlit dashboard for customer hourly-pricing comparisons."""

from __future__ import annotations

import logging
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from lp_besh.dashboard_analysis import (
    calculate_scenario,
    combine_hourly_results,
    combine_missing_pairs,
    daily_cost_frame,
    hourly_price_profile,
    load_customer_dataframes,
    load_price_dataframe,
    monthly_cost_frame,
)
from lp_besh.parsers import BlankKwhPolicy


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REALTIME_PRICE_FILE = PROJECT_ROOT / "comed_realtime_hourly_pricing.csv"
DAY_AHEAD_PRICE_FILE = PROJECT_ROOT / "comed_day_ahead_hourly_pricing.csv"
LOGGER = logging.getLogger(__name__)


def main() -> None:
    st.set_page_config(
        page_title="ComEd Hourly Pricing Calculator",
        layout="wide",
    )
    st.title("ComEd Hourly Pricing Calculator")
    st.caption(
        "Upload customer hourly interval data and compare estimated energy cost "
        "against ComEd day-ahead and settled real-time hourly prices."
    )

    _render_sidebar_status()

    with st.sidebar:
        st.header("Customer data")
        uploaded_files = st.file_uploader(
            "Upload interval CSVs",
            type=["csv"],
            accept_multiple_files=True,
            help=(
                "Upload one or more customer interval files. Supports date + 0:00..23:00 "
                "daily-wide files or interval_start,kwh long files."
            ),
        )
        blank_policy = st.selectbox(
            "Blank hourly cells",
            options=[policy.value for policy in BlankKwhPolicy],
            index=1,
            help="Use skip for DST/missing-hour blanks. Use zero only when blanks truly mean zero usage.",
        )
        st.divider()
        st.caption("On-peak rule: weekdays HE 7-23, excluding NERC holidays.")

    if not uploaded_files:
        _render_empty_state()
        return

    try:
        load_df = load_customer_dataframes(
            uploaded_files,
            blank_kwh_policy=BlankKwhPolicy(blank_policy),
        )
        realtime_prices = load_price_dataframe(REALTIME_PRICE_FILE)
        day_ahead_prices = load_price_dataframe(DAY_AHEAD_PRICE_FILE)
    except Exception as exc:
        LOGGER.exception("Could not load dashboard data")
        st.error(f"Could not load data: {exc}")
        return

    if load_df.empty:
        st.warning("The uploaded files parsed successfully, but no hourly kWh rows were found.")
        return

    try:
        results = [
            calculate_scenario(label="Day-ahead", load_df=load_df, price_df=day_ahead_prices),
            calculate_scenario(label="Real-time", load_df=load_df, price_df=realtime_prices),
        ]
    except Exception as exc:
        LOGGER.exception("Could not calculate dashboard scenarios")
        st.error(f"Could not calculate scenarios: {exc}")
        return

    _render_load_context(load_df, file_count=len(uploaded_files))
    _render_coverage_warnings(results)
    _render_pair_coverage(results)

    overview_tab, charts_tab, detail_tab = st.tabs(["Overview", "Charts", "Hourly detail"])
    with overview_tab:
        _render_overview(results)
    with charts_tab:
        _render_charts(results)
    with detail_tab:
        _render_detail(results)


def _render_sidebar_status() -> None:
    with st.sidebar:
        st.header("Price sources")
        for label, path in [
            ("Real-time", REALTIME_PRICE_FILE),
            ("Day-ahead", DAY_AHEAD_PRICE_FILE),
        ]:
            if path.exists():
                size_mb = path.stat().st_size / (1024 * 1024)
                st.success(f"{label}: {size_mb:.1f} MB")
            else:
                st.error(f"{label}: missing")


def _render_empty_state() -> None:
    st.info("Upload one or more customer interval CSVs to calculate aggregated day-ahead and real-time cost scenarios.")
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Accepted daily-wide format")
        st.code("date,0:00,1:00,...,23:00\n2024-01-01,10.5,8.25,...,11.1", language="csv")
    with col2:
        st.subheader("Accepted long format")
        st.code("interval_start,kwh\n2024-01-01 00:00,10.5", language="csv")


def _render_load_context(load_df: pd.DataFrame, *, file_count: int) -> None:
    first = load_df["interval_start"].min().strftime("%Y-%m-%d %H:%M")
    last = load_df["interval_start"].max().strftime("%Y-%m-%d %H:%M")
    total_kwh = load_df["kwh"].sum()

    st.subheader("Aggregated uploaded load")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Customer files", f"{file_count:,}")
    col2.metric("Aggregated hourly rows", f"{len(load_df):,}")
    col3.metric("Uploaded kWh", f"{total_kwh:,.0f}")
    col4.metric("Date range", f"{first} to {last}")


def _render_coverage_warnings(results) -> None:
    messages = []
    for result in results:
        missing = int(result.summary["missing_price_hours"] or 0)
        if missing:
            coverage = float(result.summary["coverage_pct"] or 0.0)
            messages.append(f"{result.label}: {missing:,} load hours did not match a price row ({coverage:.1f}% coverage).")
        if "price_quality" in result.hourly.columns:
            imputed = int((result.hourly["price_quality"] == "imputed_missing_hour").sum())
            if imputed:
                messages.append(f"{result.label}: {imputed:,} matched hours use imputed prices because no source price points were available.")
    if messages:
        st.warning(" ".join(messages))


def _render_pair_coverage(results) -> None:
    rows = []
    for result in results:
        summary = result.summary
        rows.append(
            {
                "Scenario": result.label,
                "Requested load-price pairs": int(summary["total_load_hours"] or 0),
                "Calculated pairs": int(summary["matched_hours"] or 0),
                "Skipped missing-price pairs": int(summary["missing_price_hours"] or 0),
                "Coverage": f"{float(summary['coverage_pct'] or 0.0):.1f}%",
                "On-peak calculated pairs": int(summary["on_peak_matched_hours"] or 0),
                "Off-peak calculated pairs": int(summary["off_peak_matched_hours"] or 0),
            }
        )

    st.subheader("Pair coverage")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    missing = combine_missing_pairs(results)
    if missing.empty:
        return

    detail = missing[
        _available_columns(
            missing,
            [
                "scenario",
                "interval_start",
                "date",
                "hour_ending",
                "period",
                "kwh",
                "source_files",
                "source_file_count",
                "source_row_count",
                "account_ids",
            ],
        )
    ].sort_values(["interval_start", "scenario"])
    with st.expander("Missing load-price pairs excluded from cost totals", expanded=True):
        st.dataframe(detail, use_container_width=True, hide_index=True)
        st.download_button(
            "Download missing pairs CSV",
            data=detail.to_csv(index=False).encode("utf-8"),
            file_name="missing_load_price_pairs.csv",
            mime="text/csv",
        )


def _render_overview(results) -> None:
    st.subheader("Scenario totals")
    cols = st.columns(len(results))
    for col, result in zip(cols, results, strict=False):
        summary = result.summary
        with col:
            st.metric(f"{result.label} cost", _money(float(summary["total_cost"] or 0.0)))
            st.metric("Effective price", _rate(float(summary["effective_price_per_kwh"] or 0.0)))
            st.caption(
                f"{int(summary['matched_hours'] or 0):,} matched hours; "
                f"{float(summary['coverage_pct'] or 0.0):.1f}% coverage"
            )

    st.subheader("On-peak vs off-peak")
    peak_rows = []
    for result in results:
        frame = result.peak_summary.copy()
        frame.insert(0, "scenario", result.label)
        peak_rows.append(frame)
    peak_df = pd.concat(peak_rows, ignore_index=True) if peak_rows else pd.DataFrame()
    if peak_df.empty:
        st.info("No matched priced hours available for peak/off-peak summary.")
    else:
        display = peak_df.copy()
        display["cost"] = display["cost"].map(_money)
        display["kwh"] = display["kwh"].map(lambda value: f"{value:,.0f}")
        display["effective_price_per_kwh"] = display["effective_price_per_kwh"].map(_rate)
        display = display.rename(
            columns={
                "scenario": "Scenario",
                "period": "Period",
                "kwh": "kWh",
                "cost": "Cost",
                "effective_price_per_kwh": "Effective price",
                "hours": "Calculated pairs",
            }
        )
        st.dataframe(display, use_container_width=True, hide_index=True)


def _render_charts(results) -> None:
    monthly = monthly_cost_frame(results)
    if not monthly.empty:
        st.subheader("Monthly cost")
        st.altair_chart(
            alt.Chart(monthly)
            .mark_line(point=False)
            .encode(
                x=alt.X("month:T", title="Month"),
                y=alt.Y("cost:Q", title="Cost ($)"),
                color=alt.Color("scenario:N", title="Scenario"),
                tooltip=[
                    alt.Tooltip("month:T", title="Month", format="%Y-%m"),
                    alt.Tooltip("scenario:N", title="Scenario"),
                    alt.Tooltip("cost:Q", title="Cost", format="$,.2f"),
                    alt.Tooltip("kwh:Q", title="kWh", format=",.0f"),
                ],
            )
            .properties(height=280),
            use_container_width=True,
        )

    profile = hourly_price_profile(results)
    if not profile.empty:
        st.subheader("Average hourly price shape")
        st.altair_chart(
            alt.Chart(profile)
            .mark_line(point=True)
            .encode(
                x=alt.X("hour_ending:O", title="Hour ending"),
                y=alt.Y("price:Q", title="Average $/kWh"),
                color=alt.Color("scenario:N", title="Scenario"),
                tooltip=[
                    alt.Tooltip("hour_ending:O", title="HE"),
                    alt.Tooltip("scenario:N", title="Scenario"),
                    alt.Tooltip("price:Q", title="$/kWh", format=".4f"),
                ],
            )
            .properties(height=280),
            use_container_width=True,
        )

    combined = combine_hourly_results(results)
    if not combined.empty:
        peak = (
            combined.groupby(["scenario", "period"], as_index=False)
            .agg(cost=("cost", "sum"), kwh=("kwh", "sum"))
        )
        peak["effective_price_per_kwh"] = peak["cost"] / peak["kwh"]
        st.subheader("Peak/off-peak effective price")
        st.altair_chart(
            alt.Chart(peak)
            .mark_bar()
            .encode(
                x=alt.X("scenario:N", title="Scenario"),
                y=alt.Y("effective_price_per_kwh:Q", title="Effective $/kWh"),
                color=alt.Color("period:N", title="Period"),
                xOffset="period:N",
                tooltip=[
                    alt.Tooltip("scenario:N", title="Scenario"),
                    alt.Tooltip("period:N", title="Period"),
                    alt.Tooltip("effective_price_per_kwh:Q", title="$/kWh", format=".4f"),
                    alt.Tooltip("cost:Q", title="Cost", format="$,.2f"),
                    alt.Tooltip("kwh:Q", title="kWh", format=",.0f"),
                ],
            )
            .properties(height=280),
            use_container_width=True,
        )

    daily = daily_cost_frame(results)
    if not daily.empty:
        st.subheader("Daily cost")
        st.altair_chart(
            alt.Chart(daily)
            .mark_line(point=False)
            .encode(
                x=alt.X("date:T", title="Date"),
                y=alt.Y("cost:Q", title="Daily cost ($)"),
                color=alt.Color("scenario:N", title="Scenario"),
                tooltip=[
                    alt.Tooltip("date:T", title="Date"),
                    alt.Tooltip("scenario:N", title="Scenario"),
                    alt.Tooltip("cost:Q", title="Cost", format="$,.2f"),
                ],
            )
            .properties(height=280),
            use_container_width=True,
        )


def _render_detail(results) -> None:
    combined = combine_hourly_results(results)
    if combined.empty:
        st.info("No matched hourly rows available.")
        return

    detail = combined[
        _available_columns(
            combined,
            [
            "scenario",
            "interval_start",
            "date",
            "hour_ending",
            "period",
            "kwh",
            "source_files",
            "source_file_count",
            "source_row_count",
            "account_ids",
            "price",
            "raw_point_count",
            "price_quality",
            "source",
            "cost",
            ],
        )
    ].sort_values(["interval_start", "scenario"])
    st.dataframe(detail, use_container_width=True, hide_index=True)
    st.download_button(
        "Download hourly comparison CSV",
        data=detail.to_csv(index=False).encode("utf-8"),
        file_name="hourly_pricing_comparison.csv",
        mime="text/csv",
    )


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _rate(value: float) -> str:
    return f"${value:.4f}/kWh"


def _available_columns(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    return [column for column in columns if column in frame.columns]


if __name__ == "__main__":
    main()
