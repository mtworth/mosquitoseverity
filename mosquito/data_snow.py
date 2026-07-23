"""Snowmelt timing from MODIS 8-day snow cover (MOD10A2).

We deliberately use the 8-day composite product rather than daily
(MOD10A1) snow cover: it is far lighter to pull for a whole grid over a
whole season, and cloud gaps are largely pre-resolved by the composite.
The trade-off, per the project plan, is that "7 consecutive snow-free
days" becomes an approximation: N consecutive snow-free 8-day periods
(see config.CONSECUTIVE_SNOW_FREE_PERIODS_REQUIRED).
"""

import datetime as dt

import ee
import pandas as pd

from . import config
from .ee_utils import init, reduce_in_chunks, chunk_size_for_bands

# MOD10A2 Maximum_Snow_Extent pixel values.
SNOW_VALUE = 200
NO_SNOW_VALUE = 25


def _season_start(today: dt.date) -> dt.date:
    """Look back to the previous Nov 1 so we can catch melt-out that
    happened before Jan 1 at lower elevations."""
    year = today.year if today.month >= 11 else today.year - 1
    return dt.date(year, 11, 1)


def fetch_snow_state(grid_df: pd.DataFrame, today: dt.date = None) -> pd.DataFrame:
    """Returns a long DataFrame: point_id, date, snow_free (True/False/None)."""
    init()
    today = today or dt.date.today()
    start = _season_start(today)

    coll = (
        ee.ImageCollection("MODIS/061/MOD10A2")
        .filterDate(str(start), str(today + dt.timedelta(days=1)))
        .select("Maximum_Snow_Extent")
    )

    dates = coll.aggregate_array("system:time_start").getInfo()
    date_strs = [dt.date.fromtimestamp(d / 1000).isoformat() for d in dates]
    n_bands = len(date_strs)

    # Stack every 8-day image into one multi-band image (one band per date)
    # so the whole season can be sampled per point-chunk instead of one
    # reduceRegions call per date.
    def rename_band(img):
        date_str = ee.Date(img.get("system:time_start")).format("YYYY-MM-dd")
        return img.rename(date_str)

    stacked = coll.map(rename_band).toBands()

    chunk_size = chunk_size_for_bands(n_bands)
    rows = reduce_in_chunks(
        stacked, grid_df, reducer=ee.Reducer.first(), scale=500,
        chunk_size=chunk_size, progress_label="snow cover",
    )

    records = []
    for props in rows:
        point_id = props["point_id"]
        # reduceRegions renames bands with a numeric prefix; match by
        # position (sorted order matches the stacking order) rather than
        # by the original band name.
        band_names = sorted(k for k in props.keys() if k not in ("point_id", "_lon", "_lat"))
        for band_name, date_str in zip(band_names, date_strs):
            val = props.get(band_name)
            if val == SNOW_VALUE:
                snow_free = False
            elif val == NO_SNOW_VALUE:
                snow_free = True
            else:
                snow_free = None  # cloud / fill / lake / ocean / unknown
            records.append(
                {"point_id": point_id, "date": date_str, "snow_free": snow_free}
            )

    return pd.DataFrame.from_records(records)


def compute_melt_dates(snow_state_df: pd.DataFrame) -> pd.DataFrame:
    """Collapse the long snow-state table into one melt date per point.

    Unknown/cloud periods are forward-filled from the last known state so a
    single obscured composite doesn't break a run of otherwise snow-free
    periods. If no melt-out is found in the observed window, melt_date is
    None (still snow-covered, or data insufficient).

    Melt date is taken as the period *after the last snow-covered period*
    seen in the record, not the first N-period snow-free run scanning
    forward -- the Sierra season regularly has brief early-season
    snow-free stretches (a warm November week, a dry January spell)
    followed by more snowfall, which a forward scan would mistake for
    melt-out.
    """
    n_required = config.CONSECUTIVE_SNOW_FREE_PERIODS_REQUIRED
    results = []
    for point_id, group in snow_state_df.groupby("point_id"):
        group = group.sort_values("date")
        states = group["snow_free"].ffill().tolist()
        dates = group["date"].tolist()

        last_snowy_idx = None
        for i, state in enumerate(states):
            if state is False:
                last_snowy_idx = i

        if last_snowy_idx is None:
            # No snow observed all season within the window.
            melt_date = dates[0] if dates else None
        elif last_snowy_idx == len(states) - 1:
            melt_date = None
        else:
            melt_date = dates[last_snowy_idx + 1]
            # Require the resulting snow-free run to actually be at least
            # n_required periods long (guards against a single trailing
            # cloud/unknown gap masquerading as melt-out).
            if len(states) - (last_snowy_idx + 1) < n_required:
                melt_date = None

        still_snow_covered = melt_date is None and (
            len(states) > 0 and states[-1] is False
        )
        results.append(
            {
                "point_id": point_id,
                "melt_date": melt_date,
                "still_snow_covered": still_snow_covered,
                "n_obscured_periods": sum(1 for s in group["snow_free"] if s is None),
                "n_total_periods": len(group),
            }
        )
    return pd.DataFrame.from_records(results)
