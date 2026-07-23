"""Combine raw per-point time series into the derived features described
in the project plan (days_since_snowmelt, degree_days_since_melt, etc.).
"""

import datetime as dt

import pandas as pd


def _cumulative_degree_days_since(dd_series: dict, melt_date: str, today: dt.date) -> float:
    """Sum daily degree-day values from melt_date (exclusive) through today."""
    if melt_date is None:
        return 0.0
    total = 0.0
    for date_str, val in dd_series.items():
        if val is None:
            continue
        if date_str > melt_date and date_str <= today.isoformat():
            total += val
    return total


def _precip_window_sum(precip_series: dict, today: dt.date, days: int) -> float:
    start = (today - dt.timedelta(days=days)).isoformat()
    total = 0.0
    for date_str, val in precip_series.items():
        if val is not None and date_str > start:
            total += val
    return total


def _days_since_last_precip(precip_series: dict, today: dt.date, threshold_mm=1.0):
    dated = sorted(precip_series.items())
    last_rain_date = None
    for date_str, val in dated:
        if val is not None and val >= threshold_mm:
            last_rain_date = date_str
    if last_rain_date is None:
        return None
    delta = (today - dt.date.fromisoformat(last_rain_date)).days
    return delta


def build_features(
    grid_df: pd.DataFrame,
    melt_df: pd.DataFrame,
    dd_series_by_point: dict,
    precip_series_by_point: dict,
    tmax_by_point: dict,
    tmin_by_point: dict,
    today: dt.date = None,
) -> pd.DataFrame:
    today = today or dt.date.today()

    melt_lookup = melt_df.set_index("point_id").to_dict(orient="index")
    records = []

    for row in grid_df.itertuples():
        pid = int(row.point_id)
        melt_info = melt_lookup.get(pid, {})
        melt_date = melt_info.get("melt_date")
        still_snow_covered = melt_info.get("still_snow_covered", False)

        # pandas silently coerces None to NaN in some object-dtype paths
        # (e.g. via to_dict after a merge); NaN is truthy in Python, so
        # `if melt_date:` alone isn't a safe None-check here.
        if not isinstance(melt_date, str):
            melt_date = None

        days_since_melt = None
        if melt_date:
            days_since_melt = (today - dt.date.fromisoformat(melt_date)).days

        degree_days = _cumulative_degree_days_since(
            dd_series_by_point.get(pid, {}), melt_date, today
        )

        precip_series = precip_series_by_point.get(pid, {})
        precip_7 = _precip_window_sum(precip_series, today, 7)
        precip_30 = _precip_window_sum(precip_series, today, 30)
        days_since_rain = _days_since_last_precip(precip_series, today)

        tmax_series = tmax_by_point.get(pid, {})
        tmin_series = tmin_by_point.get(pid, {})
        mean_temps = [
            (tmax_series[d] + tmin_series[d]) / 2 - 273.15
            for d in tmax_series
            if tmax_series.get(d) is not None and tmin_series.get(d) is not None
        ]
        mean_temp_7 = sum(mean_temps) / len(mean_temps) if mean_temps else None
        min_temps = [v - 273.15 for v in tmin_series.values() if v is not None]
        min_temp_7 = min(min_temps) if min_temps else None

        records.append(
            {
                "point_id": pid,
                "lat": row.lat,
                "lon": row.lon,
                "elevation_m": row.elevation_m,
                "melt_date": melt_date,
                "still_snow_covered": still_snow_covered,
                "days_since_snowmelt": days_since_melt,
                "degree_days_since_melt": degree_days,
                "precipitation_last_7_days_mm": precip_7,
                "precipitation_last_30_days_mm": precip_30,
                "days_since_last_precipitation": days_since_rain,
                "mean_temperature_last_7_days_c": mean_temp_7,
                "minimum_temperature_last_7_days_c": min_temp_7,
            }
        )

    return pd.DataFrame.from_records(records)
