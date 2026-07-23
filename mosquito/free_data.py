"""No-auth data backend used while Earth Engine authentication is pending.

Uses only free, keyless public APIs:
  - Open-Meteo elevation API      (terrain)
  - Open-Meteo archive API        (historical daily weather + snow depth)
  - Open-Meteo forecast API       (current conditions)
  - OpenStreetMap Overpass API    (water/wetland/forest features for habitat)

This intentionally mirrors the feature set of the Earth Engine modules
(grid.py, data_snow.py, data_weather.py, data_habitat.py) at coarser
spatial/temporal fidelity, so scoring.py and features.py work unchanged
against either backend. Swap back to the EE backend once
`earthengine authenticate` succeeds -- see ee_utils.py.
"""

import datetime as dt
import math
import time

import numpy as np
import pandas as pd
import requests

from . import config

OPEN_METEO_ELEVATION = "https://api.open-meteo.com/v1/elevation"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OVERPASS_URL = "https://lz4.overpass-api.de/api/interpreter"
USER_AGENT = "sierra-mosquito-nowcast/0.1 (personal research prototype)"

SNOW_DEPTH_FREE_THRESHOLD_M = 0.02  # below this, treat pixel as snow-free


# --- Grid / terrain --------------------------------------------------------

def _candidate_points():
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


def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _get_with_retry(url, params, timeout, max_retries=8, base_delay=10):
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            # Network hiccups (read timeouts especially) happen routinely
            # on large multi-location archive queries; retry with backoff
            # instead of letting the whole pipeline die on one flaky call.
            last_exc = exc
            time.sleep(base_delay * (2 ** attempt))
            continue
        if resp.status_code == 429:
            time.sleep(base_delay * (2 ** attempt))
            continue
        resp.raise_for_status()
        return resp
    if last_exc:
        raise last_exc
    resp.raise_for_status()
    return resp


def fetch_elevation_and_slope(points: list, chunk_size=50) -> list:
    """points: list of {point_id, lat, lon} dicts (or equivalent rows).
    Returns records with elevation_m/slope_deg attached, via Open-Meteo's
    elevation API. Slope is a rough estimate from a small offset neighbor
    (no full terrain raster available in the free-data backend) -- shared
    by build_grid() and anything else (e.g. validate.py) that needs
    slope_deg for arbitrary points, not just the regular map grid.
    """
    records = []
    for batch in _chunked(points, chunk_size):
        lats = [str(p["lat"]) for p in batch] + [str(p["lat"] + 0.01) for p in batch]
        lons = [str(p["lon"]) for p in batch] + [str(p["lon"]) for p in batch]

        r = _get_with_retry(
            OPEN_METEO_ELEVATION,
            {"latitude": ",".join(lats), "longitude": ",".join(lons)},
            timeout=30,
        )
        all_elevations = r.json()["elevation"]
        n = len(batch)
        elevations, neighbor_elevations = all_elevations[:n], all_elevations[n:]
        time.sleep(1.5)

        for p, elev, neighbor_elev in zip(batch, elevations, neighbor_elevations):
            # ~0.01 deg lat =~ 1111 m; rough rise/run slope in degrees.
            rise = neighbor_elev - elev
            run_m = 1111.0
            slope_deg = math.degrees(math.atan2(abs(rise), run_m)) if elev is not None else None
            records.append(
                {
                    "point_id": p["point_id"],
                    "lat": p["lat"],
                    "lon": p["lon"],
                    "elevation_m": elev,
                    "slope_deg": slope_deg,
                }
            )
    return records


def build_grid(chunk_size=50):
    """Regular grid over the Sierra extent, filtered to the target
    elevation band using Open-Meteo's elevation API (batched requests).

    Main points and their neighbor offsets are combined into a single
    request per batch (rather than two) to cut API calls in half and stay
    under the free tier's rate limit.
    """
    candidates = _candidate_points()
    records = fetch_elevation_and_slope(candidates, chunk_size=chunk_size)

    df = pd.DataFrame.from_records(records)
    df = df.dropna(subset=["elevation_m"])
    df = df[
        (df["elevation_m"] >= config.MIN_ELEVATION_M)
        & (df["elevation_m"] <= config.MAX_ELEVATION_M)
    ].reset_index(drop=True)
    return df


# --- Snow / weather (Open-Meteo archive) -----------------------------------

def _weather_season_start(today: dt.date) -> dt.date:
    year = today.year if today.month >= 11 else today.year - 1
    return dt.date(year, 11, 1)


def fetch_daily_series(grid_df: pd.DataFrame, today: dt.date = None, chunk_size=40):
    """One archive-API call per small batch of points (Open-Meteo supports
    comma-separated multi-location requests). Returns a dict keyed by
    point_id -> {date_str: {var: value}}.
    """
    today = today or dt.date.today()
    start = _weather_season_start(today)
    daily_vars = "temperature_2m_max,temperature_2m_min,precipitation_sum,snow_depth_max"

    out = {}
    rows = list(grid_df.itertuples())
    for batch in _chunked(rows, chunk_size):
        lats = ",".join(str(r.lat) for r in batch)
        lons = ",".join(str(r.lon) for r in batch)
        resp = _get_with_retry(
            OPEN_METEO_ARCHIVE,
            {
                "latitude": lats,
                "longitude": lons,
                "start_date": str(start),
                "end_date": str(today),
                "daily": daily_vars,
                "timezone": "auto",
            },
            timeout=60,
        )
        payload = resp.json()
        time.sleep(1.5)
        # Multi-location responses come back as a list; single-location as
        # a dict. Normalize to a list.
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
                }
            out[int(r.point_id)] = series
    return out


def compute_melt_dates(daily_series_by_point: dict) -> pd.DataFrame:
    """Melt date = the day after the *last* day snow was present this
    season (i.e. the start of the snow-free run that persists through to
    today), not the first isolated snow-free stretch.

    Sierra snowpack routinely has brief early-season thaws (a warm
    November week, a dry January spell) that look like "melt-out" if you
    just scan forward for the first N-day snow-free run -- but more snow
    falls afterward. Scanning from the *end* of the record backward for
    the most recent snow-covered day avoids that false positive. A short
    trailing-max smoothing pass absorbs single-day sensor/model noise so
    one spurious high reading doesn't reset the date.
    """
    smoothing_window = 3
    results = []
    for point_id, series in daily_series_by_point.items():
        dates = sorted(series.keys())
        depths = [series[d]["snow_depth_m"] for d in dates]

        smoothed = []
        for i in range(len(depths)):
            window = [v for v in depths[max(0, i - smoothing_window + 1) : i + 1] if v is not None]
            smoothed.append(max(window) if window else None)

        last_snowy_idx = None
        for i, v in enumerate(smoothed):
            if v is not None and v >= SNOW_DEPTH_FREE_THRESHOLD_M:
                last_snowy_idx = i

        if last_snowy_idx is None:
            # No snow observed all season (e.g. low-elevation edge of grid).
            melt_date = dates[0] if dates else None
            still_snow_covered = False
        elif last_snowy_idx == len(smoothed) - 1:
            melt_date = None
            still_snow_covered = True
        else:
            melt_date = dates[last_snowy_idx + 1]
            still_snow_covered = False

        results.append(
            {
                "point_id": point_id,
                "melt_date": melt_date,
                "still_snow_covered": still_snow_covered,
            }
        )
    return pd.DataFrame.from_records(results)


def build_features_from_daily(
    grid_df: pd.DataFrame,
    daily_series_by_point: dict,
    melt_df: pd.DataFrame,
    today: dt.date = None,
) -> pd.DataFrame:
    today = today or dt.date.today()
    melt_lookup = melt_df.set_index("point_id").to_dict(orient="index")
    base_temp = config.BASE_TEMP_C

    records = []
    for row in grid_df.itertuples():
        pid = int(row.point_id)
        series = daily_series_by_point.get(pid, {})
        melt_info = melt_lookup.get(pid, {})
        melt_date = melt_info.get("melt_date")
        still_snow_covered = melt_info.get("still_snow_covered", False)

        days_since_melt = (today - dt.date.fromisoformat(melt_date)).days if melt_date else None

        degree_days = 0.0
        if melt_date:
            for date_str, vals in series.items():
                if date_str <= melt_date or date_str > today.isoformat():
                    continue
                if vals["tmax_c"] is None or vals["tmin_c"] is None:
                    continue
                mean_c = (vals["tmax_c"] + vals["tmin_c"]) / 2
                degree_days += max(0.0, mean_c - base_temp)

        last_30 = [d for d in series if (today - dt.date.fromisoformat(d)).days <= 30 and d <= today.isoformat()]
        last_7 = [d for d in last_30 if (today - dt.date.fromisoformat(d)).days <= 7]

        precip_30 = sum(series[d]["precip_mm"] or 0 for d in last_30)
        precip_7 = sum(series[d]["precip_mm"] or 0 for d in last_7)

        rain_days = sorted(d for d in series if (series[d]["precip_mm"] or 0) >= 1.0)
        days_since_rain = (today - dt.date.fromisoformat(rain_days[-1])).days if rain_days else None

        temps_7 = [
            (series[d]["tmax_c"] + series[d]["tmin_c"]) / 2
            for d in last_7
            if series[d]["tmax_c"] is not None and series[d]["tmin_c"] is not None
        ]
        mins_7 = [series[d]["tmin_c"] for d in last_7 if series[d]["tmin_c"] is not None]

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
                "mean_temperature_last_7_days_c": (sum(temps_7) / len(temps_7)) if temps_7 else None,
                "minimum_temperature_last_7_days_c": min(mins_7) if mins_7 else None,
            }
        )
    return pd.DataFrame.from_records(records)


def fetch_current_conditions(grid_df: pd.DataFrame, chunk_size=40) -> pd.DataFrame:
    """Current temp/wind/humidity per point via Open-Meteo's forecast API
    'current' block."""
    records = []
    rows = list(grid_df.itertuples())
    for batch in _chunked(rows, chunk_size):
        lats = ",".join(str(r.lat) for r in batch)
        lons = ",".join(str(r.lon) for r in batch)
        resp = _get_with_retry(
            OPEN_METEO_FORECAST,
            {
                "latitude": lats,
                "longitude": lons,
                "current": "temperature_2m,wind_speed_10m,relative_humidity_2m",
                "timezone": "auto",
            },
            timeout=30,
        )
        payload = resp.json()
        time.sleep(1.5)
        entries = payload if isinstance(payload, list) else [payload]
        for r, entry in zip(batch, entries):
            cur = entry["current"]
            records.append(
                {
                    "point_id": int(r.point_id),
                    "wind_speed_ms": cur["wind_speed_10m"] / 3.6 if cur.get("wind_speed_10m") is not None else None,
                    "humidity_min_pct": cur.get("relative_humidity_2m"),
                    "current_mean_temp_c": cur.get("temperature_2m"),
                    "conditions_date": cur.get("time"),
                }
            )
    return pd.DataFrame.from_records(records)


# --- Habitat (OpenStreetMap Overpass) ---------------------------------------

def _overpass_query(bbox):
    south, west, north, east = bbox
    return f"""
    [out:json][timeout:60];
    (
      way["natural"="wetland"]({south},{west},{north},{east});
      way["natural"="water"]({south},{west},{north},{east});
      way["waterway"~"river|stream"]({south},{west},{north},{east});
      way["landuse"="forest"]({south},{west},{north},{east});
      way["natural"="wood"]({south},{west},{north},{east});
      way["natural"~"bare_rock|scree"]({south},{west},{north},{east});
    );
    out geom;
    """


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _fetch_osm_features():
    b = config.SIERRA_BOUNDS
    bbox = (b["min_lat"], b["min_lon"], b["max_lat"], b["max_lon"])
    resp = requests.get(
        OVERPASS_URL,
        params={"data": _overpass_query(bbox)},
        headers={"User-Agent": USER_AGENT},
        timeout=120,
    )
    resp.raise_for_status()
    elements = resp.json()["elements"]

    wetland_pts, water_pts, forest_pts, rock_pts = [], [], [], []
    for el in elements:
        tags = el.get("tags", {})
        geom = el.get("geometry", [])
        pts = [(g["lat"], g["lon"]) for g in geom]
        if not pts:
            continue
        if tags.get("natural") == "wetland":
            wetland_pts.extend(pts)
        elif tags.get("natural") == "water" or tags.get("waterway") in ("river", "stream"):
            water_pts.extend(pts)
        elif tags.get("landuse") == "forest" or tags.get("natural") == "wood":
            forest_pts.extend(pts)
        elif tags.get("natural") in ("bare_rock", "scree"):
            rock_pts.extend(pts)

    return {
        "wetland": np.array(wetland_pts) if wetland_pts else np.empty((0, 2)),
        "water": np.array(water_pts) if water_pts else np.empty((0, 2)),
        "forest": np.array(forest_pts) if forest_pts else np.empty((0, 2)),
        "rock": np.array(rock_pts) if rock_pts else np.empty((0, 2)),
    }


def _min_distance_m(lat, lon, pts):
    if pts.shape[0] == 0:
        return None
    # Fast approximate flat-earth distance for the initial nearest-neighbor
    # pass (fine at this scale/precision), refined with haversine on the
    # single closest candidate.
    dlat = pts[:, 0] - lat
    dlon = (pts[:, 1] - lon) * math.cos(math.radians(lat))
    d2 = dlat ** 2 + dlon ** 2
    idx = int(np.argmin(d2))
    return _haversine_m(lat, lon, pts[idx, 0], pts[idx, 1])


def fetch_habitat(grid_df: pd.DataFrame) -> pd.DataFrame:
    features = _fetch_osm_features()
    steep_threshold_deg = 25

    records = []
    for row in grid_df.itertuples():
        d_wetland = _min_distance_m(row.lat, row.lon, features["wetland"])
        d_water = _min_distance_m(row.lat, row.lon, features["water"])
        d_forest = _min_distance_m(row.lat, row.lon, features["forest"])
        d_rock = _min_distance_m(row.lat, row.lon, features["rock"])

        prox = config.WATER_PROXIMITY_M
        near_wetland = d_wetland is not None and d_wetland <= prox
        near_water = d_water is not None and d_water <= prox
        near_forest = d_forest is not None and d_forest <= 150
        near_rock = d_rock is not None and d_rock <= 150

        is_steep_lake = (
            row.slope_deg is not None and row.slope_deg >= config.LAKE_ROCKY_SHORE_SLOPE_DEG
        )

        if near_wetland:
            habitat_class = "wet_meadow_marsh"
        elif near_water and near_forest:
            habitat_class = "forested_near_water"
        elif near_water and is_steep_lake:
            habitat_class = "lake_rocky_shore"
        elif near_water:
            habitat_class = "lake_edge_slow_water"
        elif near_forest:
            habitat_class = "ordinary_forest"
        elif near_rock and row.slope_deg is not None and row.slope_deg >= steep_threshold_deg:
            habitat_class = "steep_exposed_rock"
        else:
            habitat_class = "open_dry"

        records.append(
            {
                "point_id": int(row.point_id),
                "water_within_500m": bool(near_water or near_wetland),
                "habitat_class": habitat_class,
            }
        )
    return pd.DataFrame.from_records(records)
