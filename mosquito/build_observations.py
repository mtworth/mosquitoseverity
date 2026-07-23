"""Join the manually-extracted (post_id, date, place, severity) rows with
post metadata (author, post date) to produce the full observation schema
from the project plan. Geocoding (place_text -> lat/lon) is a separate,
later step -- left null here per the plan's rule against forcing
precision that isn't there.
"""

import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT / "data"))
from hst_extracted_raw import ROWS  # noqa: E402

FIELDS = [
    "source", "source_url", "post_id", "author_id", "trip_id",
    "observation_date", "date_start", "date_end", "date_precision",
    "place_text", "latitude", "longitude", "location_confidence",
    "reported_severity", "severity_min", "severity_max", "raw_text",
]


def main():
    posts = json.loads((REPO_ROOT / "cache" / "parsed_posts.json").read_text())
    post_by_id = {p["post_id"]: p for p in posts}

    out_rows = []
    for (post_id, date_start, date_end, date_precision, place_text,
         sev_min, sev_max, sev_reported, raw_text) in ROWS:
        post = post_by_id[post_id]
        observation_date = date_start if date_start == date_end else None
        out_rows.append({
            "source": "high_sierra_topix",
            "source_url": None,
            "post_id": post_id,
            "author_id": post["author"],
            "trip_id": post_id,
            "observation_date": observation_date,
            "date_start": date_start,
            "date_end": date_end,
            "date_precision": date_precision,
            "place_text": place_text,
            "latitude": None,
            "longitude": None,
            "location_confidence": None,
            "reported_severity": sev_reported,
            "severity_min": sev_min,
            "severity_max": sev_max,
            "raw_text": raw_text,
        })

    out_path = str(REPO_ROOT / "hst_observations.csv")
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"{len(out_rows)} observations written to {out_path}")
    posts_covered = len(set(r[0] for r in ROWS))
    print(f"Covering {posts_covered} of {len(posts) - 2} trip-report posts (excludes 2 pinned rules posts)")


if __name__ == "__main__":
    main()
