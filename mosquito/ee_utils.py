"""Earth Engine session helpers."""

import os
import ee
import pandas as pd

_INITIALIZED = False

# Set this to your Google Cloud project id if ee.Initialize() fails asking
# for one (Earth Engine now requires a registered cloud project per user).
# Can also be supplied via the EE_PROJECT environment variable.
EE_PROJECT = os.environ.get("EE_PROJECT")

# For unattended runs (GitHub Actions, cron): personal OAuth (what
# `earthengine authenticate` sets up) can't run non-interactively, so CI
# uses a service account instead. Set EE_SERVICE_ACCOUNT_EMAIL and either
# EE_SERVICE_ACCOUNT_KEY (the raw JSON key content, e.g. from a GitHub
# secret) or EE_SERVICE_ACCOUNT_KEY_FILE (a path to the key file) to use
# it. Local interactive development is unaffected -- it keeps using
# whatever `earthengine authenticate` already set up when neither of
# these is set.
EE_SERVICE_ACCOUNT_EMAIL = os.environ.get("EE_SERVICE_ACCOUNT_EMAIL")
EE_SERVICE_ACCOUNT_KEY = os.environ.get("EE_SERVICE_ACCOUNT_KEY")
EE_SERVICE_ACCOUNT_KEY_FILE = os.environ.get("EE_SERVICE_ACCOUNT_KEY_FILE")


def init():
    """Initialize the Earth Engine session once per process."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    try:
        if EE_SERVICE_ACCOUNT_EMAIL and (EE_SERVICE_ACCOUNT_KEY or EE_SERVICE_ACCOUNT_KEY_FILE):
            if EE_SERVICE_ACCOUNT_KEY_FILE:
                creds = ee.ServiceAccountCredentials(EE_SERVICE_ACCOUNT_EMAIL, EE_SERVICE_ACCOUNT_KEY_FILE)
            else:
                creds = ee.ServiceAccountCredentials(EE_SERVICE_ACCOUNT_EMAIL, key_data=EE_SERVICE_ACCOUNT_KEY)
            ee.Initialize(creds, project=EE_PROJECT) if EE_PROJECT else ee.Initialize(creds)
        elif EE_PROJECT:
            ee.Initialize(project=EE_PROJECT)
        else:
            ee.Initialize()
    except Exception as exc:
        raise RuntimeError(
            "Earth Engine is not authenticated/initialized. For local use, run "
            "`earthengine authenticate` in a real terminal first, and set "
            "EE_PROJECT if it asks for a Google Cloud project id. For CI, set "
            "EE_SERVICE_ACCOUNT_EMAIL + EE_SERVICE_ACCOUNT_KEY (see README)."
        ) from exc
    _INITIALIZED = True


def chunk_size_for_bands(n_bands: int, target_values_per_request: int = 90000, floor: int = 200, ceiling: int = 5000) -> int:
    """Pick a point-chunk size so that (points_per_chunk * n_bands) stays
    near target_values_per_request. Earth Engine hard-caps any single
    getInfo() query at 5000 elements regardless, so that's the ceiling;
    the target keeps response payload/time reasonable well below that cap
    for wide (many-band) pulls like a season of daily weather.
    """
    size = max(1, target_values_per_request // max(1, n_bands))
    return max(floor, min(ceiling, size))


def points_to_fc(points_df: pd.DataFrame, extra_props=None, buffer_m=None):
    extra_props = extra_props or {}
    feats = []
    for row in points_df.itertuples():
        props = {"point_id": int(row.point_id)}
        for k, col in extra_props.items():
            props[k] = getattr(row, col)
        geom = ee.Geometry.Point([row.lon, row.lat])
        if buffer_m:
            geom = geom.buffer(buffer_m)
        feats.append(ee.Feature(geom, props))
    return ee.FeatureCollection(feats)


def reduce_in_chunks(image, points_df: pd.DataFrame, reducer, scale: int, chunk_size: int, progress_label: str = "", buffer_m=None):
    """Run image.reduceRegions against points_df in chunks, concatenating
    results into a list of {point_id, ...band properties}. Keeps every
    single EE request comfortably under the 5000-element query cap and
    bounds response size/time by chunk_size.
    """
    all_rows = []
    n = len(points_df)
    n_chunks = (n + chunk_size - 1) // chunk_size
    for i in range(0, n, chunk_size):
        batch = points_df.iloc[i:i + chunk_size]
        # Look up lat/lon by point_id from the input batch rather than the
        # response geometry: a buffered geometry (polygon) doesn't come
        # back as a flat [lon, lat] pair the way a plain Point does, so
        # trusting the response shape breaks (and previously crashed
        # silently mid-pipeline) whenever buffer_m is set.
        latlon_by_id = {int(r.point_id): (r.lat, r.lon) for r in batch.itertuples()}
        fc = points_to_fc(batch, buffer_m=buffer_m)
        sampled = image.reduceRegions(collection=fc, reducer=reducer, scale=scale)
        rows = sampled.getInfo()["features"]
        for row in rows:
            props = row["properties"]
            pid = int(props["point_id"])
            lat, lon = latlon_by_id[pid]
            props["_lat"] = lat
            props["_lon"] = lon
            all_rows.append(props)
        if progress_label:
            done_chunk = i // chunk_size + 1
            print(f"  {progress_label}: chunk {done_chunk}/{n_chunks} ({len(all_rows)}/{n} points)")
    return all_rows
