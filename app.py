from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import date, datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, send_from_directory


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
POWER_PARAMETERS = (
    "T2M",
    "T2M_MAX",
    "T2M_MIN",
    "PRECTOTCORR",
    "ALLSKY_SFC_SW_DWN",
    "RH2M",
    "GWETTOP",   # surface soil wetness (modeled, MERRA-2)
    "GWETROOT",  # root zone soil wetness (modeled, MERRA-2)
)
PARAMETER_UNITS = {
    "T2M": "°C",
    "T2M_MAX": "°C",
    "T2M_MIN": "°C",
    "PRECTOTCORR": "mm/day",
    "ALLSKY_SFC_SW_DWN": "kWh/m²/day",
    "RH2M": "%",
    "GWETTOP": "fraction",
    "GWETROOT": "fraction",
}
CACHE_TTL_SECONDS = 60 * 60
CACHE_MAX_ENTRIES = 256
MISSING_VALUE = -999
SDA_URL = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"

app = Flask(__name__)
app.logger.setLevel(logging.INFO)
_cache: dict[tuple[float, float, str, str], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


class PowerServiceError(Exception):
    """Raised when NASA POWER cannot provide a usable response."""


def _parse_coordinate(raw_value: str | None, name: str, minimum: float, maximum: float) -> float:
    try:
        value = float(raw_value) if raw_value is not None else math.nan
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}.")
    return value


def _parse_date(raw_value: str | None, name: str) -> date:
    try:
        parsed = date.fromisoformat(raw_value or "")
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid date in YYYY-MM-DD format.") from exc
    if parsed.isoformat() != raw_value:
        raise ValueError(f"{name} must be a valid date in YYYY-MM-DD format.")
    if parsed < date(1981, 1, 1):
        raise ValueError(f"{name} must be on or after 1981-01-01, the start of the daily POWER record.")
    if parsed > datetime.now(timezone.utc).date():
        raise ValueError(f"{name} cannot be in the future.")
    return parsed


def _numeric_value(raw_value: Any) -> float | None:
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value == MISSING_VALUE:
        return None
    return value


def _summarize_response(
    payload: dict[str, Any],
    latitude: float,
    longitude: float,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    try:
        source_parameters = payload["properties"]["parameter"]
    except (KeyError, TypeError) as exc:
        raise PowerServiceError("NASA POWER returned no daily parameter data.") from exc

    daily: dict[str, dict[str, float | None]] = {}
    summary: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for parameter in POWER_PARAMETERS:
        series = source_parameters.get(parameter, {})
        if not isinstance(series, dict):
            series = {}
        values: list[float] = []
        for date_key, raw_value in series.items():
            value = _numeric_value(raw_value)
            daily.setdefault(date_key, {})[parameter] = value
            if value is not None:
                values.append(value)

        if not series:
            warnings.append(f"NASA POWER did not return {parameter} for this request.")
        if parameter == "PRECTOTCORR":
            aggregate = sum(values) if values else None
            aggregate_type = "sum"
        else:
            aggregate = sum(values) / len(values) if values else None
            aggregate_type = "mean"
        summary[parameter] = {
            "value": round(aggregate, 3) if aggregate is not None else None,
            "unit": PARAMETER_UNITS[parameter],
            "days_available": len(values),
            "aggregation": aggregate_type,
        }

    ordered_daily = []
    for date_key in sorted(daily):
        day_values = daily[date_key]
        ordered_daily.append(
            {
                "date": f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}",
                **{parameter: day_values.get(parameter) for parameter in POWER_PARAMETERS},
            }
        )

    if not any(item["days_available"] for item in summary.values()):
        raise PowerServiceError("NASA POWER returned no valid measurements for the selected date range.")

    return {
        "location": {"latitude": latitude, "longitude": longitude},
        "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "summary": summary,
        "daily": ordered_daily,
        "warnings": warnings,
        "source": "NASA POWER Daily API (AG community, UTC)",
        "source_url": POWER_URL,
        "soil_wetness_note": "GWETTOP and GWETROOT are model-derived (MERRA-2), not SMAP measurements.",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _get_power_data(
    latitude: float,
    longitude: float,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    cache_key = (round(latitude, 4), round(longitude, 4), start_date.isoformat(), end_date.isoformat())
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    query = urlencode(
        {
            "parameters": ",".join(POWER_PARAMETERS),
            "community": "AG",
            "longitude": f"{longitude:.4f}",
            "latitude": f"{latitude:.4f}",
            "start": start_date.strftime("%Y%m%d"),
            "end": end_date.strftime("%Y%m%d"),
            "format": "JSON",
            "time-standard": "UTC",
        }
    )
    req = Request(
        f"{POWER_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": "SmartCropRotationPlanner/1.0"},
    )
    try:
        with urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        app.logger.warning("NASA POWER returned HTTP %s", exc.code)
        raise PowerServiceError(f"NASA POWER returned HTTP {exc.code}. Please retry later.") from exc
    except (URLError, TimeoutError) as exc:
        app.logger.warning("NASA POWER request failed: %s", exc)
        raise PowerServiceError("Could not reach NASA POWER. Check your connection and try again.") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        app.logger.warning("NASA POWER returned invalid JSON")
        raise PowerServiceError("NASA POWER returned an unreadable response. Please retry later.") from exc

    result = _summarize_response(payload, latitude, longitude, start_date, end_date)
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            oldest_key = next(iter(_cache))
            _cache.pop(oldest_key, None)
        _cache[cache_key] = (time.monotonic(), result)
    return result


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "webpage.html")


@app.get("/api/power")
def power():
    try:
        latitude = _parse_coordinate(request.args.get("latitude"), "Latitude", -90, 90)
        longitude = _parse_coordinate(request.args.get("longitude"), "Longitude", -180, 180)
        start_date = _parse_date(request.args.get("start"), "Start date")
        end_date = _parse_date(request.args.get("end"), "End date")
        if start_date > end_date:
            raise ValueError("Start date must be on or before end date.")
        if (end_date - start_date).days > 365:
            raise ValueError("Choose a date range of 366 days or fewer.")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        return jsonify(_get_power_data(latitude, longitude, start_date, end_date))
    except PowerServiceError as exc:
        return jsonify({"error": str(exc)}), 502


@app.get("/api/soil")
def soil():
    """USDA NRCS Soil Data Access: soil horizons for the major components at a point (US only)."""
    try:
        latitude = _parse_coordinate(request.args.get("latitude"), "Latitude", -90, 90)
        longitude = _parse_coordinate(request.args.get("longitude"), "Longitude", -180, 180)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Coordinates are validated floats, so formatting them into the query is safe.
    # WKT order is longitude first, then latitude.
    sql = f"""
    SELECT c.compname, c.comppct_r, ch.hzname, ch.hzdept_r, ch.hzdepb_r,
           ch.sandtotal_r, ch.claytotal_r, ch.ph1to1h2o_r, ch.om_r, ch.awc_r
    FROM component c JOIN chorizon ch ON c.cokey = ch.cokey
    WHERE c.majcompflag = 'Yes' AND c.mukey IN
      (SELECT * FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('POINT({longitude:.6f} {latitude:.6f})'))
    ORDER BY c.comppct_r DESC, ch.hzdept_r
    """
    body = json.dumps({"format": "JSON+COLUMNNAME", "query": sql}).encode("utf-8")
    req = Request(
        SDA_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SmartCropRotationPlanner/1.0",
        },
    )
    try:
        with urlopen(req, timeout=30) as response:
            table = json.loads(response.read().decode("utf-8")).get("Table", [])
    except HTTPError as exc:
        app.logger.warning("USDA SDA returned HTTP %s", exc.code)
        return jsonify({"error": f"USDA Soil Data Access returned HTTP {exc.code}. Please retry later."}), 502
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        app.logger.warning("USDA SDA request failed: %s", exc)
        return jsonify({"error": "USDA Soil Data Access is unavailable. Please retry later."}), 502

    if len(table) < 2:
        return jsonify({"error": "No USDA soil data for this point (US coverage only)."}), 404

    columns, rows = table[0], table[1:]
    return jsonify(
        {
            "location": {"latitude": latitude, "longitude": longitude},
            "horizons": [dict(zip(columns, row)) for row in rows],
            "source": "USDA NRCS Soil Data Access",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)