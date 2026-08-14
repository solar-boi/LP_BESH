# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

LP BESH calculates what a customer's hourly interval usage would have cost under ComEd hourly
pricing. It joins customer load CSVs to ComEd hourly price CSVs by local hour and produces hourly
cost rows plus summary metrics (total kWh, total cost, effective $/kWh). Interfaces: a CLI
(console script `lp-besh`, `lp_besh.cli:main`), an optional FastAPI upload endpoint
(`lp_besh.api`), and a Streamlit comparison dashboard.

## Commands

```bash
# setup (Python 3.11+, src layout)
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # pandas/streamlit/altair/pytest; ".[api]" adds FastAPI/uvicorn

# tests
python -m pytest -q
python -m pytest tests/test_parsers.py -q

# CLI (works uninstalled via PYTHONPATH=src)
PYTHONPATH=src python3 -m lp_besh.cli calculate \
  --load examples/customer_interval_sample.csv \
  --prices examples/comed_hourly_price_sample.csv \
  --output out/hourly_costs.csv --summary-output out/summary.json

# dashboard / API
streamlit run streamlit_app.py --server.fileWatcherType none
uvicorn lp_besh.api:app --reload          # needs ".[api]"

# rebuild root ComEd price CSVs from public feeds
PYTHONPATH=src python3 scripts/build_comed_realtime_prices.py --start 2022-07-03T00:00 --end 2026-07-04T00:00
PYTHONPATH=src python3 scripts/build_comed_day_ahead_prices.py
```

## Architecture

```
src/lp_besh/
  models.py               frozen dataclasses: HourlyLoad, HourlyPrice, JoinedUsageCost, CalculationSummary
  parsers.py              CSV reading/format detection/normalization — the only layer that parses files
  calculator.py           strict hourly join + cost math (calculate_hourly_costs)
  cli.py                  argparse entrypoint ("calculate" subcommand)
  api.py                  optional FastAPI upload skeleton (import-guarded)
  comed_price_builder.py  ComEd real-time 5-min fetch -> hourly aggregation/interpolation
  dashboard_analysis.py   pandas scenario analysis for the dashboard (ScenarioResult)
  streamlit_app.py        dashboard UI
scripts/                  build the two root price CSVs (real-time + day-ahead feeds)
streamlit_app.py          root shim: inserts src/ into sys.path and calls lp_besh.streamlit_app.main
```

**Core stays stdlib-only.** `models`, `parsers`, `calculator`, `cli` have zero third-party
dependencies and all money math uses `Decimal` (never float). pandas/streamlit/altair belong only
in `dashboard_analysis`/`streamlit_app` (via the `app`/`dev` extras); FastAPI only in `api.py`
behind its import guard. Don't add pandas to the core calculation path.

## Conventions and invariants

- **Join key is local hour-beginning.** Hour-ending sources (ComEd `date` + `hour_ending`,
  ComEdLP daily-wide exports) are normalized so HE 1 -> `00:00` and HE 24 -> `23:00` — the same
  convention used for PJM 5CP lookups. Don't invent a second timestamp convention.
- **The join is intentionally strict.** Duplicate hourly timestamps raise
  `DuplicateTimestampError`; usage hours without prices raise `MissingPriceDataError`.
  `allow_missing_prices` is explicit opt-in. Preserve this — silent gaps bias effective $/kWh.
- **Prices normalize to dollars/kWh internally.** Input units: `dollars_per_kwh` (default),
  `cents_per_kwh`, `dollars_per_mwh` (`PriceUnit`). Convert at parse time only.
- **Load CSV formats:** long (`interval_start,kwh`) and daily-wide (`date,0:00..23:00`), with
  auto-detection (`--load-format auto` default). Blank daily-wide cells follow `BlankKwhPolicy`:
  `skip` (default), `error`, or `zero` — `zero` only when blanks truly mean zero measured usage.
- **Price CSV formats:** `timestamp` and `date_hour_ending`, also auto-detected.
- **Real-time hourly prices are averages of ComEd 5-minute API points**
  (`https://hourlypricing.comed.com/api`, America/Chicago). `price_quality` is `observed_full`
  (12 points) or `observed_partial`; fully missing hours are linearly interpolated only when
  bounded by observed rows — leading/trailing gaps stay absent (`--no-fill-missing` to disable).
- **Dashboard reads the two root CSVs** (`comed_day_ahead_hourly_pricing.csv`,
  `comed_realtime_hourly_pricing.csv`) and compares scenarios; multiple uploaded customer files
  are summed by hourly timestamp before pricing. Dashboard math lives in `dashboard_analysis.py`,
  not in the Streamlit UI file.

## Data notes

- `examples/` holds one sample per supported CSV layout — keep them in sync with parser changes.
- Customer files exported from ComEdLP are hour-ending source data already normalized to the
  interval-start join key.
- Open data-contract questions (customer timestamp convention/timezone, DST fall-back handling,
  whether results should include additional ComEd charges) are tracked in README "Data Needed
  Next" — resolve there before hardcoding assumptions.
