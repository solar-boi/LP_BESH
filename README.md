# LP BESH Hourly Pricing Skeleton

This project is a first-pass skeleton for calculating what a customer's hourly interval usage would have cost under ComEd hourly pricing.

The core workflow is:

1. Load customer hourly interval usage.
2. Load ComEd hourly price data.
3. Normalize both files to the same local hourly timestamp.
4. Join usage to price by hour.
5. Calculate hourly cost, total kWh, total cost, and effective price paid per kWh.

The load CSV parser now supports two layouts. The CLI defaults to `--load-format auto`.

Long customer usage CSV:

```csv
interval_start,kwh
2024-01-01 00:00,10.5
```

Daily-wide customer usage CSV:

```csv
date,0:00,1:00,2:00,...,23:00
2024-01-01,10.5,8.25,9.0,...,11.2
```

ComEd price CSV:

```csv
interval_start,price
2024-01-01 00:00,0.052
```

ComEd date/hour-ending price CSV:

```csv
date,hour_ending,price
2024-01-01,1,0.052
```

By default, `price` is interpreted as dollars per kWh. The CLI also supports `cents_per_kwh` and `dollars_per_mwh`.

## Run A Calculation

```bash
PYTHONPATH=src python3 -m lp_besh.cli calculate \
  --load examples/customer_interval_sample.csv \
  --prices examples/comed_hourly_price_sample.csv \
  --output out/hourly_costs.csv \
  --summary-output out/summary.json
```

For the daily-wide customer format, use the same command and leave `--load-format` unset, or pass `--load-format daily_wide`.

Daily-wide files can contain blank hourly cells. By default, blanks are skipped with `--blank-kwh-policy skip`; use `error` to fail on blanks or `zero` only when blanks truly mean zero measured usage.

The customer files from ComEdLP are hour-ending source data normalized to an interval-start join key. This follows the same convention used for PJM 5CP lookups: HE 1 maps to `00:00`, HE 24 maps to `23:00`. ComEd price files with `date` + `hour_ending` are normalized the same way before joining.

If installed locally:

```bash
pip install -e .
lp-besh calculate --load customer.csv --prices prices.csv --output hourly_costs.csv
```

## Optional API Skeleton

The upload/API layer is in `src/lp_besh/api.py`. Install optional API dependencies to run it:

```bash
pip install -e ".[api]"
uvicorn lp_besh.api:app --reload
```

Then post two CSV files to `POST /calculate`.

## Streamlit Dashboard

The Streamlit page lets you upload one or more customer interval files and compare estimated cost under the generated day-ahead and real-time ComEd price files:

```bash
streamlit run streamlit_app.py --server.fileWatcherType none
```

It uses `comed_day_ahead_hourly_pricing.csv` and `comed_realtime_hourly_pricing.csv` from the project root. When multiple files are uploaded, customer kWh is summed by hourly timestamp before pricing. The dashboard shows total cost, effective $/kWh, matched-price coverage, monthly cost, average hourly price shape, daily cost, and on-peak/off-peak pricing.

## Data Needed Next

To finish the adapters, the important details are:

- Customer interval file columns and a few sample rows.
- Whether customer timestamps are hour-beginning or hour-ending.
- Customer timestamp timezone and how repeated fall-back DST hours are represented.
- Whether blank hourly cells mean skipped DST hours, missing meter data, or true zero usage.
- ComEd hourly pricing columns and a few sample rows.
- ComEd price unit: dollars/kWh, cents/kWh, or dollars/MWh.
- Whether the ComEd timestamp is hour-beginning or hour-ending.
- Whether the desired result is energy-only hourly price or should include additional ComEd hourly-pricing charges later.
