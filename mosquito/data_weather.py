"""gridMET-derived weather features.

Deviation from the original plan: instead of live NWS API calls for
"current activity" conditions, everything here comes from gridMET (same
Earth Engine source as the rest of the model). Calling a live weather API
per grid point (hundreds of points) would mean hundreds of sequential HTTP
requests every time the map is regenerated; gridMET gives wind/humidity/
temperature in the same batched Earth Engine call as everything else, at
the cost of a ~1-2 day data lag instead of true real-time conditions.
"""

import datetime as dt

import ee
import pandas as pd

from . import config
from .ee_utils import init, reduce_in_chunks, chunk_size_for_bands

GRIDMET = "IDAHO_EPSCOR/GRIDMET"


def _weather_season_start(today: dt.date) -> dt.date:
    month, day = config.WEATHER_SEASON_START_MONTH_DAY
    return dt.date(today.year, month, day)


def _stack_and_sample(collection: ee.ImageCollection, band: str, grid_df: pd.DataFrame, scale=4000, label=""):
    """Stack a single-band daily collection into one band per date, sample
    all points across chunks sized for the number of dates, return
    {point_id: {date: value}}.
    """
    dates = collection.aggregate_array("system:time_start").getInfo()
    date_strs = [dt.date.fromtimestamp(d / 1000).isoformat() for d in dates]
    n_bands = len(date_strs)

    def rename_band(img):
        date_str = ee.Date(img.get("system:time_start")).format("YYYY-MM-dd")
        return img.select(band).rename(date_str)

    stacked = collection.map(rename_band).toBands()
    chunk_size = chunk_size_for_bands(n_bands)
    rows = reduce_in_chunks(
        stacked, grid_df, reducer=ee.Reducer.first(), scale=scale,
        chunk_size=chunk_size, progress_label=label,
    )

    out = {}
    for props in rows:
        point_id = props["point_id"]
        band_names = sorted(k for k in props.keys() if k not in ("point_id", "_lon", "_lat"))
        out[point_id] = {date_str: props.get(bn) for bn, date_str in zip(band_names, date_strs)}
    return out


def fetch_degree_day_series(grid_df: pd.DataFrame, today: dt.date = None) -> dict:
    """Daily (mean_temp_c - base_temp_c) clamped at 0, per point, from
    WEATHER_SEASON_START_MONTH_DAY through today. Cumulative summing from
    each point's own melt date happens later in features.py.
    """
    init()
    today = today or dt.date.today()
    start = _weather_season_start(today)

    coll = (
        ee.ImageCollection(GRIDMET)
        .filterDate(str(start), str(today + dt.timedelta(days=1)))
        .select(["tmmx", "tmmn"])
    )

    base_k = config.BASE_TEMP_C + 273.15

    def to_degree_day(img):
        mean_k = img.select("tmmx").add(img.select("tmmn")).divide(2)
        dd = mean_k.subtract(base_k).max(0).rename("dd")
        return dd.copyProperties(img, ["system:time_start"])

    dd_coll = ee.ImageCollection(coll.map(to_degree_day))
    return _stack_and_sample(dd_coll, "dd", grid_df, label="degree-days")


def fetch_recent_precip(grid_df: pd.DataFrame, today: dt.date = None, days=30) -> dict:
    init()
    today = today or dt.date.today()
    start = today - dt.timedelta(days=days)
    coll = (
        ee.ImageCollection(GRIDMET)
        .filterDate(str(start), str(today + dt.timedelta(days=1)))
        .select("pr")
    )
    return _stack_and_sample(coll, "pr", grid_df, label="precip")


def fetch_recent_temp(grid_df: pd.DataFrame, today: dt.date = None, days=7) -> tuple:
    init()
    today = today or dt.date.today()
    start = today - dt.timedelta(days=days)

    coll_max = (
        ee.ImageCollection(GRIDMET).filterDate(str(start), str(today + dt.timedelta(days=1))).select("tmmx")
    )
    coll_min = (
        ee.ImageCollection(GRIDMET).filterDate(str(start), str(today + dt.timedelta(days=1))).select("tmmn")
    )
    tmax_by_point = _stack_and_sample(coll_max, "tmmx", grid_df, label="recent tmax")
    tmin_by_point = _stack_and_sample(coll_min, "tmmn", grid_df, label="recent tmin")
    return tmax_by_point, tmin_by_point


def fetch_current_conditions(grid_df: pd.DataFrame, today: dt.date = None) -> pd.DataFrame:
    """Most recent available day's wind speed, humidity, and mean temp per
    point -- used for the current-activity multiplier.
    """
    init()
    today = today or dt.date.today()

    # gridMET has a short latency; look back a few days and take the most
    # recent available image rather than assuming "today" exists yet.
    coll = (
        ee.ImageCollection(GRIDMET)
        .filterDate(str(today - dt.timedelta(days=5)), str(today + dt.timedelta(days=1)))
        .sort("system:time_start", False)
    )
    latest = ee.Image(coll.first()).select(["vs", "rmin", "tmmx", "tmmn"])
    latest_date = ee.Date(coll.first().get("system:time_start")).format("YYYY-MM-dd").getInfo()

    chunk_size = chunk_size_for_bands(n_bands=4, target_values_per_request=300000)
    rows = reduce_in_chunks(
        latest, grid_df, reducer=ee.Reducer.first(), scale=4000,
        chunk_size=chunk_size, progress_label="current conditions",
    )

    records = []
    for props in rows:
        mean_temp_c = None
        if props.get("tmmx") is not None and props.get("tmmn") is not None:
            mean_temp_c = (props["tmmx"] + props["tmmn"]) / 2 - 273.15
        records.append(
            {
                "point_id": props["point_id"],
                "wind_speed_ms": props.get("vs"),
                "humidity_min_pct": props.get("rmin"),
                "current_mean_temp_c": mean_temp_c,
                "conditions_date": latest_date,
            }
        )
    return pd.DataFrame.from_records(records)
