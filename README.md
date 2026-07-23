# Sierra Mosquito Nowcast

Rule-based mosquito severity map for the Sierra Nevada, built from snowmelt
timing (MODIS), degree-day heat accumulation and habitat (gridMET, NLCD,
JRC surface water) via Google Earth Engine. Outputs a static HTML map --
no server, no database.

## Local development

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
earthengine authenticate   # one-time, opens a browser
python -m mosquito.build_map
```

Output lands in `output/mosquito_map_<date>.html`.

A no-auth fallback backend (Open-Meteo + OpenStreetMap Overpass, no Google
account needed) is also available: `python -m mosquito.build_map_free`.

## Daily rebuild via GitHub Actions -> GitHub Pages

`.github/workflows/daily-map.yml` runs on a daily cron, regenerates the
map, and commits it to `docs/index.html`. GitHub Pages serves that file
directly -- set the repo's Pages source to **main branch, `/docs` folder**
under Settings -> Pages.

Earth Engine can't do the interactive browser login this repo's local
setup uses when running unattended in CI, so the workflow needs a
**service account** instead:

1. In the Google Cloud project already registered for Earth Engine
   (console.cloud.google.com), create a service account
   (IAM & Admin -> Service Accounts -> Create).
2. Grant it Earth Engine access: register the service account's email at
   https://signup.earthengine.google.com/#!/service_accounts (or via
   `earthengine authenticate --service_account` locally as a check).
3. Create a JSON key for it and download the file.
4. In the GitHub repo, add these under Settings -> Secrets and variables
   -> Actions:
   - `EE_SERVICE_ACCOUNT_EMAIL` -- the service account's email address
   - `EE_SERVICE_ACCOUNT_KEY` -- the full contents of the downloaded JSON key
   - `EE_PROJECT` -- your Google Cloud project id (only if Earth Engine
     asks for one; same value as `EE_PROJECT` you'd use locally)

The workflow also needs push access to commit `docs/index.html` back to
the repo -- that's already covered by the default `GITHUB_TOKEN` and the
`permissions: contents: write` block in the workflow file, no extra setup
needed there.

Trigger a manual run any time from the Actions tab (`workflow_dispatch`)
to test before waiting for the daily schedule.

## Project layout

- `mosquito/` -- all pipeline code (grid, snow/weather/habitat fetch,
  scoring, map rendering)
- `mosquito/free_data.py`, `mosquito/build_map_free.py` -- the no-auth
  fallback backend
- `hst_observations.csv`, `hst_observations_geocoded.csv` -- extracted
  and geocoded High Sierra Topix trip-report observations (2024-2025),
  used for validation
- `validation_results.csv` -- model predictions vs. reported severity for
  those observations
- `data/hst_extracted_raw.py` -- the hand-extracted (post_id, date, place,
  severity) rows `build_observations.py` turns into `hst_observations.csv`
- `data/gnis_sierra.csv` -- USGS GNIS place names within the Sierra
  extent (pre-filtered to relevant feature classes), used both for HST
  place-name geocoding and the map's search box. Regenerate from a fresh
  raw GNIS download with `geocode.rebuild_filtered_gazetteer()` if the
  Sierra extent or relevant classes ever change.
- `output/` -- generated maps (gitignored; `docs/index.html` is the
  committed, published copy)

## Reproducing the HST dataset from scratch

The raw High Sierra Topix forum scrape (`threads.md`) isn't in this repo --
it's other people's forum posts reproduced verbatim, which is a different
copyright situation than our own derived/transformed data. What's here is
the already-extracted, already-geocoded output:
`hst_observations.csv` -> `hst_observations_geocoded.csv` ->
`validation_results.csv`. `data/hst_extracted_raw.py` is the hand-extracted
(post_id, date, place, severity) data that produced them; `parse_threads.py`
and `build_observations.py` document how that extraction was done but need
a local copy of the raw scrape to actually run.

```
python -m mosquito.geocode    # hst_observations.csv -> hst_observations_geocoded.csv
python -m mosquito.validate   # -> validation_results.csv
```
