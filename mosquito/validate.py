"""Validate the rule-based environmental model against the geocoded HST
trip-report observations.

For each observation, we need the environmental state *as it would have
been knowable on that observation's date* -- computed only from data up
to that date, never later. This is the standard no-lookahead requirement
for any real validation; getting it wrong (e.g. computing melt date using
the full season's data regardless of observation date) would silently
leak future information into the "prediction" and make the model look
better than it is.

Approach: pull each unique point's full daily weather/snow series once
(covering both 2024 and 2025 seasons in one batched Open-Meteo archive
call per batch of points), then for each observation, truncate that
point's series to <= its reference date before computing melt date /
degree-days / precip / current conditions. Habitat doesn't depend on date
so it's fetched once per unique point.
"""

import datetime as dt
import json
import os
from pathlib import Path

import pandas as pd
import requests

from mosquito import free_data
from mosquito.scoring import score_row

REPO_ROOT = Path(__file__).resolve().parent.parent
SERIES_CACHE_PATH = str(REPO_ROOT / "cache" / "validate_series_cache.json")
HABITAT_CACHE_PATH = str(REPO_ROOT / "cache" / "validate_habitat_cache.csv")

DAILY_VARS = "temperature_2m_max,temperature_2m_min,precipitation_sum,snow_depth_max,wind_speed_10m_max"
SEASON_SPAN_START = dt.date(2023, 11, 1)
SEASON_SPAN_END = dt.date(2025, 9, 5)


def fetch_full_series(points_df: pd.DataFrame, chunk_size=20) -> dict:
    out = {}
    rows = list(points_df.itertuples())
    for batch in free_data._chunked(rows, chunk_size):
        lats = ",".join(str(r.lat) for r in batch)
        lons = ",".join(str(r.lon) for r in batch)
        resp = free_data._get_with_retry(
            free_data.OPEN_METEO_ARCHIVE,
            {
                "latitude": lats, "longitude": lons,
                "start_date": str(SEASON_SPAN_START), "end_date": str(SEASON_SPAN_END),
                "daily": DAILY_VARS, "timezone": "auto",
            },
            timeout=60,
        )
        payload = resp.json()
        import time as _time
        _time.sleep(1.5)
        entries = payload if isinstance(payload, list) else [payload]
        for r, entry in zip(batch, entries):
            daily = entry["daily"]
            dates = daily["time"]
            series = {}
            for i, date_str in enumerate(dates):
                series[date_str] = {
                    "tmax_c": daily["temperature_2m_max"][i],
                    "tmin_c": daily["temperature_2m_min"][i],
                    "precip_mm": daily["precipitation_sum"][i],
                    "snow_depth_m": daily.get("snow_depth_max", [None] * len(dates))[i],
                    "wind_ms": (daily["wind_speed_10m_max"][i] / 3.6) if daily.get("wind_speed_10m_max") else None,
                }
            out[int(r.point_id)] = series
    return out


def features_as_of(series: dict, ref_date: dt.date) -> dict:
    """Everything computed using only dates <= ref_date (no lookahead)."""
    truncated = {d: v for d, v in series.items() if d <= ref_date.isoformat()}
    melt_df = free_data.compute_melt_dates({0: truncated})
    melt_info = melt_df.iloc[0].to_dict()
    feat_df = free_data.build_features_from_daily(
        pd.DataFrame([{"point_id": 0, "lat": 0, "lon": 0, "elevation_m": 0}]),
        {0: truncated}, melt_df, today=ref_date,
    )
    feat = feat_df.iloc[0].to_dict()

    ref_day = truncated.get(ref_date.isoformat(), {})
    feat["wind_speed_ms"] = ref_day.get("wind_ms")
    if ref_day.get("tmax_c") is not None and ref_day.get("tmin_c") is not None:
        feat["current_mean_temp_c"] = (ref_day["tmax_c"] + ref_day["tmin_c"]) / 2
    else:
        feat["current_mean_temp_c"] = None
    return feat


def main():
    df = pd.read_csv(REPO_ROOT / "hst_observations_geocoded.csv")
    geo = df.dropna(subset=["latitude", "longitude"]).copy()

    unique_points = geo[["latitude", "longitude"]].drop_duplicates().reset_index(drop=True)
    unique_points["point_id"] = unique_points.index
    unique_points = unique_points.rename(columns={"latitude": "lat", "longitude": "lon"})
    geo = geo.merge(
        unique_points, left_on=["latitude", "longitude"], right_on=["lat", "lon"], how="left"
    )

    if os.path.exists(SERIES_CACHE_PATH):
        print(f"Loading cached daily series from {SERIES_CACHE_PATH}...")
        series_by_point = {int(k): v for k, v in json.loads(Path(SERIES_CACHE_PATH).read_text()).items()}
    else:
        print(f"Fetching full 2-season daily series for {len(unique_points)} unique points...")
        series_by_point = fetch_full_series(unique_points)
        with open(SERIES_CACHE_PATH, "w") as f:
            json.dump(series_by_point, f)

    if os.path.exists(HABITAT_CACHE_PATH):
        print(f"Loading cached habitat from {HABITAT_CACHE_PATH}...")
        habitat_df = pd.read_csv(HABITAT_CACHE_PATH)
    else:
        print("Fetching elevation/slope for unique points (needed by habitat's lake_rocky_shore check)...")
        elev_records = free_data.fetch_elevation_and_slope(unique_points.to_dict("records"))
        slope_df = pd.DataFrame(elev_records)[["point_id", "slope_deg"]]
        points_with_slope = unique_points.merge(slope_df, on="point_id", how="left")

        print("Fetching habitat for unique points...")
        habitat_df = free_data.fetch_habitat(points_with_slope)
        habitat_df.to_csv(HABITAT_CACHE_PATH, index=False)
    habitat_lookup = habitat_df.set_index("point_id").to_dict(orient="index")

    print("Scoring each observation as-of its reference date (no lookahead)...")
    results = []
    for row in geo.itertuples():
        ref_date = dt.date.fromisoformat(row.date_end)
        series = series_by_point.get(int(row.point_id), {})
        feat = features_as_of(series, ref_date)
        habitat = habitat_lookup.get(int(row.point_id), {"habitat_class": "open_dry"})
        feat["habitat_class"] = habitat["habitat_class"]
        scored = score_row(feat)
        results.append({
            "post_id": row.post_id,
            "place_text": row.place_text,
            "ref_date": row.date_end,
            "reported_severity": row.reported_severity,
            "predicted_severity": scored["severity_score"],
            "predicted_class": scored["severity_class"],
            "days_since_snowmelt": scored.get("days_since_snowmelt"),
            "degree_days_since_melt": scored.get("degree_days_since_melt"),
            "habitat_class": habitat["habitat_class"],
            "elevation_m": row.elevation_m if "elevation_m" in geo.columns else None,
            "location_confidence": row.location_confidence,
            "emergence_score": scored.get("emergence_score"),
            "habitat_multiplier": scored.get("habitat_multiplier"),
            "moisture_multiplier": scored.get("moisture_multiplier"),
            "current_activity_multiplier": scored.get("current_activity_multiplier"),
        })

    out = pd.DataFrame(results)
    out_path = str(REPO_ROOT / "validation_results.csv")
    out.to_csv(out_path, index=False)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
