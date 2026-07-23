"""Build the sample-point grid over the Sierra extent, filtered to
mountain elevations, with terrain (elevation/slope/aspect) attached.

Chunked via ee_utils.reduce_in_chunks to survive Earth Engine's 5000-
element query cap at fine grid spacing (~1km => tens of thousands of
candidate points).
"""

import ee
import pandas as pd

from . import config
from .ee_utils import init, reduce_in_chunks, chunk_size_for_bands


def _candidate_points():
    """Regular lat/lon grid of candidate points over the bounding box."""
    b = config.SIERRA_BOUNDS
    step = config.GRID_SPACING_DEG
    points = []
    lat = b["min_lat"]
    pid = 0
    while lat <= b["max_lat"]:
        lon = b["min_lon"]
        while lon <= b["max_lon"]:
            points.append({"point_id": pid, "lat": lat, "lon": lon})
            pid += 1
            lon += step
        lat += step
    return points


def build_grid():
    """Return a DataFrame of grid points within the target elevation band,
    with elevation/slope/aspect attached via chunked Earth Engine calls.
    """
    init()

    candidates = pd.DataFrame(_candidate_points())
    print(f"{len(candidates)} candidate points before elevation filtering")

    elevation_img = ee.Image("NASA/NASADEM_HGT/001").select("elevation")
    terrain = ee.Terrain.products(elevation_img)  # elevation, slope, aspect

    # 3 bands per point -- large chunks are safe and fast here.
    chunk_size = chunk_size_for_bands(n_bands=3, target_values_per_request=300000)
    rows = reduce_in_chunks(
        terrain, candidates, reducer=ee.Reducer.first(), scale=90,
        chunk_size=chunk_size, progress_label="terrain",
    )

    records = [
        {
            "point_id": props["point_id"],
            "lon": props["_lon"],
            "lat": props["_lat"],
            "elevation_m": props.get("elevation"),
            "slope_deg": props.get("slope"),
            "aspect_deg": props.get("aspect"),
        }
        for props in rows
    ]

    df = pd.DataFrame.from_records(records)
    df = df.dropna(subset=["elevation_m"])
    df = df[
        (df["elevation_m"] >= config.MIN_ELEVATION_M)
        & (df["elevation_m"] <= config.MAX_ELEVATION_M)
    ].reset_index(drop=True)
    return df


if __name__ == "__main__":
    grid = build_grid()
    print(f"{len(grid)} grid points within elevation band")
    print(grid.head())
