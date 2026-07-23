"""End-to-end pipeline using the no-auth free_data backend (Open-Meteo +
OpenStreetMap Overpass) instead of Earth Engine. Same scoring/rendering
code as build_map.py -- only the data-fetch layer differs.

    python -m mosquito.build_map_free
"""

import datetime as dt
import os

import pandas as pd

from . import free_data
from .scoring import score_dataframe
from .build_map import render_map, OUTPUT_DIR, CACHE_DIR


def run(today: dt.date = None, use_cache=False) -> str:
    today = today or dt.date.today()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    cache_path = os.path.join(CACHE_DIR, f"scored_free_{today.isoformat()}.csv")
    if use_cache and os.path.exists(cache_path):
        scored = pd.read_csv(cache_path)
    else:
        print("Building grid (Open-Meteo elevation)...")
        grid_df = free_data.build_grid()
        print(f"  {len(grid_df)} points in target elevation band")

        print("Fetching daily weather + snow depth series (Open-Meteo archive)...")
        daily_series = free_data.fetch_daily_series(grid_df, today)

        print("Deriving melt dates...")
        melt_df = free_data.compute_melt_dates(daily_series)

        print("Building derived features...")
        feat_df = free_data.build_features_from_daily(grid_df, daily_series, melt_df, today)

        print("Fetching current conditions (Open-Meteo forecast)...")
        current_conditions = free_data.fetch_current_conditions(grid_df)

        print("Fetching habitat features (OpenStreetMap Overpass)...")
        habitat_df = free_data.fetch_habitat(grid_df)

        merged = feat_df.merge(habitat_df, on="point_id", how="left")
        merged = merged.merge(current_conditions, on="point_id", how="left")
        merged["habitat_class"] = merged["habitat_class"].fillna("open_dry")

        print("Scoring...")
        scored = score_dataframe(merged)
        scored.to_csv(cache_path, index=False)

    print("Rendering map...")
    out_path = render_map(scored, today)
    print(f"Wrote {out_path}")
    return out_path


if __name__ == "__main__":
    run()
