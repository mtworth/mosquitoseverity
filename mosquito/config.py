"""
Configurable parameters for the Sierra mosquito nowcast model.

Everything here is a hypothesis, not a scientific conclusion. Values should
be adjusted as Phase 2 (historical trip-report validation) comes online.
"""

# --- Geographic extent -------------------------------------------------
# Rough bounding box around the Sierra Nevada range (WGS84 degrees).
SIERRA_BOUNDS = {
    "min_lon": -120.6,
    "max_lon": -118.0,
    "min_lat": 35.5,
    "max_lat": 39.7,
}

# Grid points are only kept within this elevation band (meters). Below the
# low bound is foothill/valley (low recreational relevance); above the high
# bound is high alpine terrain that rarely melts out fully.
MIN_ELEVATION_M = 1500
MAX_ELEVATION_M = 3800

# Spacing between candidate grid points, in degrees. ~0.09 deg is roughly
# 8-10 km at Sierra latitudes. Coarser = faster to compute, finer = more
# spatially detailed map.
GRID_SPACING_DEG = 0.009

# --- Snowmelt ------------------------------------------------------------
# MOD10A2 gives 8-day composite snow cover. A point is considered
# "snow-free" for a given 8-day period if snow-covered area in that period
# falls below this fraction (0-1) of the pixel neighborhood.
SNOW_FREE_FRACTION_THRESHOLD = 0.1

# Number of consecutive snow-free 8-day periods required before we call a
# date "melt-out" (approximates the plan's "7 mostly snow-free days" rule
# at 8-day product resolution -- one snow-free composite alone is not
# sufficient evidence of melt-out).
CONSECUTIVE_SNOW_FREE_PERIODS_REQUIRED = 2

# --- Degree-day accumulation ---------------------------------------------
# Base temperature (Celsius) below which no development accumulates.
# Configurable / to be calibrated later against trip reports.
BASE_TEMP_C = 5.0

# Earliest calendar date (month, day) from which we bother pulling daily
# weather for degree-day accumulation. Sierra melt-out at the target
# elevation band essentially never starts before this, so it bounds how
# much daily data we need to pull without going all the way back to the
# snow-detection season start (Nov 1).
WEATHER_SEASON_START_MONTH_DAY = (3, 1)

# --- Emergence curve ------------------------------------------------------
# Initial hypothesis curve mapping (days_since_snowmelt) -> potential (0-1).
# Refined later using degree-days as the primary driver and days-since-melt
# as a sanity check / fallback when temperature data is missing.
EMERGENCE_CURVE_DAYS = [
    # (min_day, max_day, potential)
    (None, -1, 0.0),      # still snow-covered
    (0, 7, 0.2),           # low -- larvae still developing
    (8, 21, 0.6),          # rising
    (22, 45, 1.0),         # high -- peak emergence window
    (46, 70, 0.5),         # declining
    (71, None, 0.15),      # low -- late season
]

# Degree-day equivalent curve (used preferentially when temperature data is
# available, since heat accumulation is a better predictor than raw days).
#
# Tried recalibrating this 2026-07-22 against 381 geocoded HST
# observations by solving each bucket's potential from its mean reported
# severity -- that removed the mean-level bias (predicted mean matched
# reported mean almost exactly) but collapsed the curve's peak-to-trough
# range so much that predicted severity never exceeded ~3/5 across the
# whole dataset, destroying the ability to flag genuinely severe
# conditions (only 23 of 94 "high" reports still predicted high, vs 72
# before). Reverted: an unbiased-on-average model that can't distinguish
# severe from mild is worse for this tool's actual purpose than a
# somewhat-over-predicting one that still ranks severity correctly.
# Keeping the original hypothesis curve; habitat is the stronger lever
# for severity discrimination in this data (wet_meadow_marsh reports
# averaged 3.39 vs open_dry's 1.00 -- a bigger real spread than degree-
# days show) and is the better place to push calibration next.
EMERGENCE_CURVE_DEGREE_DAYS = [
    (None, 20, 0.0),
    (20, 60, 0.2),
    (60, 150, 0.6),
    (150, 300, 1.0),
    (300, 450, 0.5),
    (450, None, 0.15),
]

# --- Habitat multiplier ---------------------------------------------------
# Keyed by simplified habitat class (derived from NLCD + water proximity).
HABITAT_MULTIPLIER = {
    "wet_meadow_marsh": 1.4,
    "lake_edge_slow_water": 1.4,
    "lake_rocky_shore": 0.9,
    "forested_near_water": 1.2,
    "ordinary_forest": 1.0,
    "open_dry": 0.8,
    "steep_exposed_rock": 0.5,
    "dry_ridge_pass": 0.5,
}

# Slope (degrees) above which a lake is treated as a steep/rocky-shored
# alpine lake ("lake_rocky_shore") rather than "lake_edge_slow_water".
# Validation against HST reports found ~80% of geocoded points were
# classified lake_edge_slow_water and systematically over-predicted
# severity: geocoding resolves most named places to GNIS Lake features,
# and "is there water within 500m" is trivially true when the point *is*
# the lake -- it doesn't distinguish a flat marshy shoreline (real
# breeding habitat) from a steep granite-lined alpine lake (poor breeding
# habitat despite being "near water"), which the original plan's own
# habitat table already treats as low/moderate, not high.
LAKE_ROCKY_SHORE_SLOPE_DEG = 8

# Distance (meters) within which surface water is considered "near" a point
# for the *_near_water habitat classes.
WATER_PROXIMITY_M = 500

# --- Moisture multiplier ---------------------------------------------------
# Simple linear-ish bonus/penalty based on recent precipitation and days
# since last rain. Values are multipliers applied on top of habitat.
MOISTURE_RULES = {
    "recent_rain_days_threshold": 3,     # rain within this many days -> bonus
    "recent_rain_bonus": 1.15,
    "dry_spell_days_threshold": 14,      # no rain for this many days -> penalty
    "dry_spell_penalty": 0.8,
    "default_multiplier": 1.0,
}

# --- Current activity multiplier (biting conditions "today") -------------
# Wind reduces experienced severity; humidity/temp extremes reduce activity.
WIND_EFFECT_THRESHOLDS_MS = [
    # (min_wind_ms, max_wind_ms, multiplier)
    (0, 2, 1.0),
    (2, 5, 0.85),
    (5, 9, 0.6),
    (9, None, 0.35),
]

TEMP_ACTIVITY_THRESHOLDS_C = [
    # mosquitoes are less active outside this comfortable range
    (None, 5, 0.3),
    (5, 10, 0.7),
    (10, 30, 1.0),
    (30, 35, 0.7),
    (35, None, 0.4),
]

# --- Severity classification ----------------------------------------------
SEVERITY_CLASS_BREAKS = [
    (0, 1, "minimal"),
    (1, 2, "noticeable"),
    (2, 3, "bad"),
    (3, 5.01, "severe"),
]

# Final numeric score is scaled to 0-5 for display, matching the HST scale.
SCORE_SCALE_MAX = 5.0
