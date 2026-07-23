"""Static habitat classification: land cover + water proximity -> the
simplified habitat classes used by config.HABITAT_MULTIPLIER.

Note: "dry_ridge_pass" is defined in config as a distinct multiplier but is
not separately detected here -- distinguishing a ridge/pass from generic
steep/exposed terrain would need neighborhood terrain analysis (e.g. local
convexity) beyond this MVP's scope. It currently collapses into
"steep_exposed_rock". Revisit once real trip-report data suggests it's
worth the extra complexity.

lake_rocky_shore: validating against HST trip reports found that treating
every near-water point as high-multiplier "lake_edge_slow_water" badly
over-predicted severity -- most Sierra lakes have steep, rocky, low-
mosquito shorelines, not the marshy/slow-drainage edges the multiplier is
meant to represent. Steep-shored lakes get a separate, lower multiplier
(see config.LAKE_ROCKY_SHORE_SLOPE_DEG).
"""

import ee
import pandas as pd

from . import config
from .ee_utils import init, reduce_in_chunks, chunk_size_for_bands

NLCD_WETLAND_CLASSES = {90, 95}
NLCD_FOREST_CLASSES = {41, 42, 43}
NLCD_OPEN_WATER = {11}
NLCD_BARREN = {31}
STEEP_SLOPE_DEG = 25


def fetch_habitat(grid_df: pd.DataFrame) -> pd.DataFrame:
    init()

    chunk_size = chunk_size_for_bands(n_bands=1, target_values_per_request=300000)

    # Land cover at the point itself.
    nlcd = ee.ImageCollection("USGS/NLCD_RELEASES/2021_REL/NLCD").filter(
        ee.Filter.eq("system:index", "2021")
    ).first().select("landcover")

    # Note: reduceRegions renames a single-band image's output column to
    # the reducer's name ("first"), not the band name -- it only preserves
    # per-band names for multi-band images.
    lc_rows = reduce_in_chunks(
        nlcd, grid_df, reducer=ee.Reducer.first(), scale=30,
        chunk_size=chunk_size, progress_label="land cover",
    )
    lc_lookup = {r["point_id"]: r.get("first") for r in lc_rows}

    # Any surface water within WATER_PROXIMITY_M of the point. Buffered
    # geometries are heavier to compute per-point than plain points, so
    # use a smaller chunk size to keep individual requests fast.
    water_mask = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence").gt(0)
    water_rows = reduce_in_chunks(
        water_mask, grid_df, reducer=ee.Reducer.max(), scale=30,
        chunk_size=max(200, chunk_size // 4), progress_label="water proximity",
        buffer_m=config.WATER_PROXIMITY_M,
    )
    water_lookup = {r["point_id"]: r.get("max") for r in water_rows}

    records = []
    for row in grid_df.itertuples():
        pid = int(row.point_id)
        lc = lc_lookup.get(pid)
        water_nearby = bool(water_lookup.get(pid))
        is_steep_lake = row.slope_deg is not None and row.slope_deg >= config.LAKE_ROCKY_SHORE_SLOPE_DEG

        if lc in NLCD_WETLAND_CLASSES:
            habitat_class = "wet_meadow_marsh"
        elif lc in NLCD_FOREST_CLASSES and water_nearby:
            habitat_class = "forested_near_water"
        elif lc in NLCD_FOREST_CLASSES:
            habitat_class = "ordinary_forest"
        elif (lc in NLCD_OPEN_WATER or water_nearby) and is_steep_lake:
            habitat_class = "lake_rocky_shore"
        elif lc in NLCD_OPEN_WATER or water_nearby:
            habitat_class = "lake_edge_slow_water"
        elif lc in NLCD_BARREN and row.slope_deg is not None and row.slope_deg >= STEEP_SLOPE_DEG:
            habitat_class = "steep_exposed_rock"
        else:
            habitat_class = "open_dry"

        records.append(
            {
                "point_id": pid,
                "nlcd_class": lc,
                "water_within_500m": water_nearby,
                "habitat_class": habitat_class,
            }
        )
    return pd.DataFrame.from_records(records)
