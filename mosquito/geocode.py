"""Resolve HST trip-report place_text strings to lat/lon using the USGS
GNIS gazetteer (Sierra bbox, relevant feature classes) + fuzzy matching.

place_text is often a compound route description ("Rock Creek to Box
Lake", "Kennedy Creek between Kennedy Lake and Sharon Lake outlet
creek"), not a single named feature. We split on common route connectors
into candidate substrings, fuzzy-match each against the gazetteer, and
keep the single best-scoring match as the representative point for that
observation -- full place_text is preserved either way so nothing is lost,
this only picks which single point stands in for the row.

Confidence bands (on rapidfuzz token_set_ratio, 0-100):
  >= 92  -> high    (auto-resolved, treated as reliable)
  80-91  -> medium  (plausible match, but flagged for spot-check)
  < 80   -> unresolved (left with null lat/lon rather than guessed)
"""

import csv
import re
import sys
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

from . import config

REPO_ROOT = Path(__file__).resolve().parent.parent

# Raw USGS GNIS California file (national gazetteer, ~50k CA rows) -- a
# one-time manual download, not committed (it's 2MB+ zipped and mostly
# irrelevant outside the Sierra). Only needed if regenerating
# data/gnis_sierra.csv from scratch; the deployed pipeline (including the
# GitHub Actions daily build) reads the small pre-filtered subset instead,
# since that raw file wouldn't exist on a fresh checkout.
RAW_GNIS_PATH = str(REPO_ROOT / "cache" / "Text" / "DomesticNames_CA.txt")
FILTERED_GNIS_PATH = REPO_ROOT / "data" / "gnis_sierra.csv"

RELEVANT_CLASSES = [
    "Stream", "Summit", "Lake", "Populated Place", "Valley",
    "Gap", "Reservoir", "Basin", "Falls", "Spring",
]

SPLIT_RE = re.compile(
    r"\bto\b|\band\b|\bover\b|\bvia\b|\bthrough\b|\bnear\b|\bbetween\b|"
    r"\bthru\b|\bfrom\b|,|/|\bat\b|\baround\b|\btowards?\b|\balong\b",
    flags=re.IGNORECASE,
)
PAREN_RE = re.compile(r"\([^)]*\)")


def load_gazetteer():
    """Small, tracked, pre-filtered subset (data/gnis_sierra.csv) used by
    the deployed pipeline. Use rebuild_filtered_gazetteer() to regenerate
    it from the raw GNIS download if the Sierra extent or relevant
    feature classes ever change.
    """
    return pd.read_csv(FILTERED_GNIS_PATH)


def rebuild_filtered_gazetteer():
    """One-time/manual: filter the full raw California GNIS download down
    to the Sierra bbox + relevant classes and overwrite the tracked
    data/gnis_sierra.csv. Requires RAW_GNIS_PATH to exist locally (a
    manual download -- see README) -- not needed for normal pipeline runs.
    """
    df = pd.read_csv(RAW_GNIS_PATH, sep="|", encoding="utf-8-sig")
    b = config.SIERRA_BOUNDS
    df = df[
        (df["prim_lat_dec"] >= b["min_lat"]) & (df["prim_lat_dec"] <= b["max_lat"]) &
        (df["prim_long_dec"] >= b["min_lon"]) & (df["prim_long_dec"] <= b["max_lon"]) &
        (df["feature_class"].isin(RELEVANT_CLASSES))
    ]
    df = df[["feature_name", "feature_class", "prim_lat_dec", "prim_long_dec"]].reset_index(drop=True)
    df.to_csv(FILTERED_GNIS_PATH, index=False)
    return df


def _candidate_substrings(place_text: str):
    cleaned = PAREN_RE.sub("", place_text)
    parts = [p.strip(" '\"-") for p in SPLIT_RE.split(cleaned)]
    parts = [p for p in parts if len(p) >= 3]
    return parts or [cleaned.strip()]


def _confidence_band(score: float) -> str:
    if score >= 92:
        return "high"
    if score >= 80:
        return "medium"
    return "unresolved"


def geocode_place(place_text: str, gaz_names: list, gaz_lookup: dict):
    best = None  # (score, substring, gaz_name)
    for substring in _candidate_substrings(place_text):
        # token_set_ratio was tried first and rejected: it treats a
        # gazetteer name that's a strict word-subset of the query as a
        # perfect match (e.g. "Fourth Lake" scores 100 against "Fourth
        # Recess Lake", a different, real place on the Recess chain).
        # token_sort_ratio still ignores word order but does not ignore
        # extra/missing words, so it correctly demotes those.
        match = process.extractOne(substring, gaz_names, scorer=fuzz.token_sort_ratio)
        if match is None:
            continue
        gaz_name, score, _ = match
        if best is None or score > best[0]:
            best = (score, substring, gaz_name)

    if best is None:
        return {"matched_substring": None, "gaz_name": None, "gaz_class": None,
                "latitude": None, "longitude": None, "match_score": None,
                "location_confidence": "unresolved"}

    score, substring, gaz_name = best
    row = gaz_lookup[gaz_name]
    return {
        "matched_substring": substring,
        "gaz_name": gaz_name,
        "gaz_class": row["feature_class"],
        "latitude": row["prim_lat_dec"] if score >= 80 else None,
        "longitude": row["prim_long_dec"] if score >= 80 else None,
        "match_score": score,
        "location_confidence": _confidence_band(score),
    }


def main():
    gaz = load_gazetteer()
    # Use the first occurrence per name (many duplicate names exist across
    # nearby features; picking one is an acceptable simplification here
    # since these are same-named, usually-adjacent features).
    gaz_lookup = {}
    for row in gaz.itertuples():
        if row.feature_name not in gaz_lookup:
            gaz_lookup[row.feature_name] = {
                "feature_class": row.feature_class,
                "prim_lat_dec": row.prim_lat_dec,
                "prim_long_dec": row.prim_long_dec,
            }
    gaz_names = list(gaz_lookup.keys())
    print(f"Gazetteer: {len(gaz_names)} unique names loaded")

    df = pd.read_csv(REPO_ROOT / "hst_observations.csv")
    unique_places = df["place_text"].dropna().unique().tolist()
    print(f"Geocoding {len(unique_places)} unique place_text values...")

    results = {}
    for i, place in enumerate(unique_places):
        results[place] = geocode_place(place, gaz_names, gaz_lookup)
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(unique_places)}")

    for col in ["matched_substring", "gaz_name", "gaz_class", "latitude",
                "longitude", "match_score", "location_confidence"]:
        df[col] = df["place_text"].map(lambda p: results.get(p, {}).get(col))

    out_path = str(REPO_ROOT / "hst_observations_geocoded.csv")
    df.to_csv(out_path, index=False)
    print(f"Wrote {out_path}")
    print(df["location_confidence"].value_counts())


if __name__ == "__main__":
    main()
