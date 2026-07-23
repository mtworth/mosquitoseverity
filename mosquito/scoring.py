"""Rule-based mosquito severity scoring.

mosquito_potential = emergence_score * habitat_multiplier
                      * moisture_multiplier * current_activity_multiplier

Kept deliberately transparent and table-driven (see config.py) rather than
fit to data -- this is a hypothesis to be calibrated later against
historical trip reports (Phase 2).
"""

import pandas as pd

from . import config


def _lookup_bucket(value, buckets, value_index=(0, 1), result_index=2):
    """Generic 'find the (lo, hi, result) row that contains value' helper.
    lo=None means -inf, hi=None means +inf.
    """
    if value is None:
        return None
    for row in buckets:
        lo, hi = row[value_index[0]], row[value_index[1]]
        if lo is not None and value < lo:
            continue
        if hi is not None and value >= hi:
            continue
        return row[result_index]
    return None


def emergence_score(days_since_snowmelt, degree_days_since_melt, still_snow_covered):
    if still_snow_covered:
        return 0.0
    # Prefer degree-days (better predictor); fall back to raw days if
    # temperature data is missing.
    if degree_days_since_melt is not None:
        score = _lookup_bucket(degree_days_since_melt, config.EMERGENCE_CURVE_DEGREE_DAYS)
        if score is not None:
            return score
    if days_since_snowmelt is not None:
        score = _lookup_bucket(days_since_snowmelt, config.EMERGENCE_CURVE_DAYS)
        if score is not None:
            return score
    return 0.0


def habitat_multiplier(habitat_class):
    return config.HABITAT_MULTIPLIER.get(habitat_class, 1.0)


def moisture_multiplier(days_since_last_precipitation):
    rules = config.MOISTURE_RULES
    if days_since_last_precipitation is None:
        return rules["default_multiplier"]
    if days_since_last_precipitation <= rules["recent_rain_days_threshold"]:
        return rules["recent_rain_bonus"]
    if days_since_last_precipitation >= rules["dry_spell_days_threshold"]:
        return rules["dry_spell_penalty"]
    return rules["default_multiplier"]


def current_activity_multiplier(wind_speed_ms, current_mean_temp_c):
    wind_mult = _lookup_bucket(wind_speed_ms, config.WIND_EFFECT_THRESHOLDS_MS) if wind_speed_ms is not None else 1.0
    temp_mult = (
        _lookup_bucket(current_mean_temp_c, config.TEMP_ACTIVITY_THRESHOLDS_C)
        if current_mean_temp_c is not None
        else 1.0
    )
    wind_mult = wind_mult if wind_mult is not None else 1.0
    temp_mult = temp_mult if temp_mult is not None else 1.0
    return wind_mult * temp_mult


def severity_class(score_0_5):
    for lo, hi, label in config.SEVERITY_CLASS_BREAKS:
        if lo <= score_0_5 < hi:
            return label
    return "severe"


def _confidence(row, habitat_confidence_note=None):
    reasons = []
    confidence = "moderate"

    if row.get("still_snow_covered"):
        confidence = "high"  # unambiguous case
    elif row.get("melt_date") is None:
        confidence = "low"
        reasons.append("melt date could not be determined from available snow data")

    if row.get("degree_days_since_melt") is None and not row.get("still_snow_covered"):
        confidence = "low"
        reasons.append("temperature data unavailable, falling back to days-since-melt only")

    if habitat_confidence_note:
        reasons.append(habitat_confidence_note)

    return confidence, reasons


def _explanation(row, e_score, h_mult, h_class, m_mult, a_mult, wind_speed_ms):
    if row.get("still_snow_covered"):
        return "This location appears to still be snow-covered, so mosquito potential is minimal."

    parts = []
    if row.get("days_since_snowmelt") is not None:
        parts.append(f"melted out about {row['days_since_snowmelt']} days ago")
    if row.get("degree_days_since_melt") is not None:
        warmth = "substantial" if row["degree_days_since_melt"] > 150 else "limited"
        parts.append(f"has accumulated {warmth} post-melt warmth ({row['degree_days_since_melt']:.0f} degree-days)")

    habitat_desc = h_class.replace("_", " ")
    parts.append(f"and sits in {habitat_desc} habitat")

    explanation = ", ".join(parts) + "."

    if wind_speed_ms is not None and wind_speed_ms >= 5:
        explanation += " Winds today may meaningfully reduce biting activity."
    elif m_mult < 1.0:
        explanation += " Recent dry conditions may be reducing available breeding habitat."
    elif m_mult > 1.0:
        explanation += " Recent rain may be sustaining breeding habitat."

    return explanation


def score_row(row: dict) -> dict:
    e_score = emergence_score(
        row.get("days_since_snowmelt"),
        row.get("degree_days_since_melt"),
        row.get("still_snow_covered"),
    )
    h_class = row.get("habitat_class", "open_dry")
    h_mult = habitat_multiplier(h_class)
    m_mult = moisture_multiplier(row.get("days_since_last_precipitation"))
    a_mult = current_activity_multiplier(row.get("wind_speed_ms"), row.get("current_mean_temp_c"))

    raw_potential = e_score * h_mult * m_mult * a_mult
    # e_score in [0,1]; multipliers hover around 1.0 (roughly 0.3-1.4), so
    # scale to the 0-5 display range by design, not derivation.
    score_0_5 = min(raw_potential * config.SCORE_SCALE_MAX, config.SCORE_SCALE_MAX)

    confidence, confidence_reasons = _confidence(row)
    explanation = _explanation(row, e_score, h_mult, h_class, m_mult, a_mult, row.get("wind_speed_ms"))

    return {
        **row,
        "emergence_score": e_score,
        "habitat_multiplier": h_mult,
        "moisture_multiplier": m_mult,
        "current_activity_multiplier": a_mult,
        "severity_score": round(score_0_5, 2),
        "severity_class": severity_class(score_0_5),
        "confidence": confidence,
        "confidence_reasons": confidence_reasons,
        "explanation": explanation,
    }


def score_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame.from_records([score_row(r) for r in df.to_dict(orient="records")])
