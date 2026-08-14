"""Optional FastAPI upload skeleton for hourly-pricing calculations."""

from __future__ import annotations

import logging
from io import StringIO

from lp_besh.calculator import calculate_hourly_costs
from lp_besh.models import PriceUnit
from lp_besh.parsers import PriceCsvSchema, read_load_csv_auto, read_price_csv

try:
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
except ImportError as exc:  # pragma: no cover - import guard for optional dependency
    raise RuntimeError(
        "FastAPI is optional. Install API dependencies with: pip install -e '.[api]'"
    ) from exc


app = FastAPI(title="LP BESH Hourly Pricing")
LOGGER = logging.getLogger(__name__)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/calculate")
async def calculate(
    load_file: UploadFile = File(...),
    price_file: UploadFile = File(...),
    price_unit: PriceUnit = Form(default=PriceUnit.DOLLARS_PER_KWH),
) -> dict[str, object]:
    """Upload customer load and ComEd price CSVs and return summary metrics."""

    try:
        load_text = (await load_file.read()).decode("utf-8-sig")
        price_text = (await price_file.read()).decode("utf-8-sig")
        load_rows = read_load_csv_auto(StringIO(load_text))
        price_rows = read_price_csv(
            StringIO(price_text),
            PriceCsvSchema(price_unit=price_unit),
        )
        result = calculate_hourly_costs(load_rows, price_rows)
    except Exception as exc:
        LOGGER.exception("API calculation failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "summary": result.summary.to_dict(),
        "hourly_rows": [row.to_dict() for row in result.hourly_rows],
    }
