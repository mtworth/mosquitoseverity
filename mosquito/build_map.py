"""End-to-end pipeline: grid -> raw data -> features -> score -> static
HTML map. Run this daily (e.g. via cron) to regenerate the map.

    python -m mosquito.build_map
"""

import datetime as dt
import json
import os

import folium
import pandas as pd

from . import config
from .grid import build_grid
from .data_snow import fetch_snow_state, compute_melt_dates
from .data_weather import (
    fetch_degree_day_series,
    fetch_recent_precip,
    fetch_recent_temp,
    fetch_current_conditions,
)
from .data_habitat import fetch_habitat
from .features import build_features
from .scoring import score_dataframe
from .geocode import load_gazetteer

SEVERITY_COLORS = {
    "minimal": "#2ecc71",
    "noticeable": "#f1c40f",
    "bad": "#e67e22",
    "severe": "#e74c3c",
}

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache")


def run(today: dt.date = None, use_cache=False) -> str:
    today = today or dt.date.today()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    cache_path = os.path.join(CACHE_DIR, f"scored_{today.isoformat()}.csv")
    if use_cache and os.path.exists(cache_path):
        scored = pd.read_csv(cache_path)
    else:
        print("Building grid...")
        grid_df = build_grid()
        print(f"  {len(grid_df)} points in target elevation band")

        print("Fetching snow cover series and deriving melt dates...")
        snow_state = fetch_snow_state(grid_df, today)
        melt_df = compute_melt_dates(snow_state)

        print("Fetching degree-day series...")
        dd_series = fetch_degree_day_series(grid_df, today)

        print("Fetching precipitation series...")
        precip_series = fetch_recent_precip(grid_df, today, days=30)

        print("Fetching recent temperature series...")
        tmax_series, tmin_series = fetch_recent_temp(grid_df, today, days=7)

        print("Fetching current conditions (wind/humidity/temp)...")
        current_conditions = fetch_current_conditions(grid_df, today)

        print("Fetching habitat classification...")
        habitat_df = fetch_habitat(grid_df)

        print("Building derived features...")
        feat_df = build_features(
            grid_df, melt_df, dd_series, precip_series, tmax_series, tmin_series, today
        )

        merged = feat_df.merge(habitat_df, on="point_id", how="left")
        merged = merged.merge(current_conditions, on="point_id", how="left")

        print("Scoring...")
        scored = score_dataframe(merged)
        scored.to_csv(cache_path, index=False)

    print("Rendering map...")
    out_path = render_map(scored, today)
    print(f"Wrote {out_path}")
    return out_path


def render_map(scored: pd.DataFrame, today: dt.date) -> str:
    from folium.plugins import FastMarkerCluster

    center_lat = scored["lat"].mean()
    center_lon = scored["lon"].mean()
    # prefer_canvas: at tens of thousands of points, SVG/DOM markers (the
    # folium.CircleMarker default) make the page unusably slow to pan/zoom
    # even before considering file size. Canvas rendering handles this
    # point count smoothly.
    m = folium.Map(location=[center_lat, center_lon], zoom_start=8, tiles="OpenStreetMap", prefer_canvas=True)

    # FastMarkerCluster builds all markers client-side from one compact
    # data array + a single JS callback, instead of one Python
    # folium.Marker/Popup object per point (which is what produced a 90MB
    # HTML file at 65k points -- each carries its own repeated JS/DOM
    # boilerplate). Clustering also keeps the DOM light when zoomed out.
    data = []
    for row in scored.itertuples():
        color = SEVERITY_COLORS.get(row.severity_class, "#7f8c8d")
        popup_html = (
            f"Severity: {row.severity_score}/5 ({row.severity_class})<br>"
            f"Confidence: {row.confidence}<br>"
            f"Elevation: {row.elevation_m:.0f} m | Habitat: {row.habitat_class.replace('_', ' ')}<br>"
            f"Days since melt: {row.days_since_snowmelt} | Degree-days: {row.degree_days_since_melt:.0f}<br>"
            f"Precip last 30d: {row.precipitation_last_30_days_mm:.1f} mm<br>"
            f"{row.explanation}"
        )
        data.append([row.lat, row.lon, color, popup_html])

    # Square cells sized to the actual grid spacing so adjacent cells tile
    # without gaps (a rectangle in lat/lon degrees, not literal meters --
    # fine at this scale, and matches "this is a regular sample grid"
    # better than isolated circle markers do).
    half_step = config.GRID_SPACING_DEG / 2
    callback = f"""
    function (row) {{
        var half = {half_step};
        var bounds = [[row[0]-half, row[1]-half],[row[0]+half, row[1]+half]];
        var marker = L.rectangle(bounds, {{
            color: row[2], fillColor: row[2], fillOpacity: 0.8, weight: 0
        }});
        marker.bindPopup(row[3]);
        return marker;
    }}
    """
    FastMarkerCluster(data=data, callback=callback, disableClusteringAtZoom=13).add_to(m)

    legend_html = """
    <div style="position: fixed; bottom: 30px; left: 30px; z-index: 1000;
                background: white; padding: 10px 14px; border-radius: 6px;
                box-shadow: 0 1px 4px rgba(0,0,0,0.3); font-size: 13px;">
        <b>Mosquito potential</b><br>
        <span style="color:#2ecc71;">&#9679;</span> Minimal (0-1)<br>
        <span style="color:#f1c40f;">&#9679;</span> Noticeable (1-2)<br>
        <span style="color:#e67e22;">&#9679;</span> Bad (2-3)<br>
        <span style="color:#e74c3c;">&#9679;</span> Severe (3-5)<br>
        <small>Generated {date} &middot; <a href="methods.html" target="_blank">Methods</a></small>
    </div>
    """.format(date=today.isoformat())
    m.get_root().html.add_child(folium.Element(legend_html))

    _add_opacity_slider(m)
    _add_gazetteer_search(m)

    out_path = os.path.join(OUTPUT_DIR, f"mosquito_map_{today.isoformat()}.html")
    m.save(out_path)
    return out_path


def _add_opacity_slider(m: folium.Map):
    """Controls fillOpacity of the grid cells via the shared canvas's CSS
    opacity, not by restyling each of tens of thousands of individual
    layers (which would be slow) -- prefer_canvas means every rectangle
    is drawn on one shared <canvas>, so one CSS opacity change applies to
    all of them at once.
    """
    map_name = m.get_name()
    html = f"""
    <div style="position: fixed; top: 90px; right: 12px; z-index: 1000;
                background: white; padding: 8px 12px; border-radius: 6px;
                box-shadow: 0 1px 4px rgba(0,0,0,0.3); font-size: 12px; width: 160px;">
        <label for="opacity-slider">Cell opacity</label><br>
        <input id="opacity-slider" type="range" min="10" max="100" value="80" style="width: 100%;">
    </div>
    <script>
    window.addEventListener("load", function() {{
        var mapObj = window["{map_name}"];
        var slider = document.getElementById("opacity-slider");
        slider.addEventListener("input", function() {{
            var op = this.value / 100;
            var container = mapObj.getContainer();
            var canvases = container.querySelectorAll(".leaflet-overlay-pane canvas");
            canvases.forEach(function(c) {{ c.style.opacity = op; }});
        }});
    }});
    </script>
    """
    m.get_root().html.add_child(folium.Element(html))


def _add_gazetteer_search(m: folium.Map):
    """Client-side search-and-zoom over the USGS GNIS gazetteer (same
    Sierra-bbox, relevant-class subset used for HST place-name geocoding)
    -- no server involved, the whole lookup table is embedded in the page
    and filtered in JS as the user types.
    """
    gaz = load_gazetteer()
    # Compact arrays, not objects, to keep the embedded payload small:
    # [name, class, lat, lon].
    entries = gaz[["feature_name", "feature_class", "prim_lat_dec", "prim_long_dec"]].values.tolist()
    gaz_json = json.dumps(entries, separators=(",", ":"))

    map_name = m.get_name()
    html = f"""
    <div style="position: fixed; top: 12px; right: 12px; z-index: 1000;
                background: white; padding: 8px 12px; border-radius: 6px;
                box-shadow: 0 1px 4px rgba(0,0,0,0.3); font-size: 13px; width: 220px;">
        <input id="gaz-search" type="text" placeholder="Find a lake, pass, meadow..."
               style="width: 100%; box-sizing: border-box; padding: 4px;">
        <div id="gaz-results" style="max-height: 220px; overflow-y: auto;"></div>
    </div>
    <script>
    window.addEventListener("load", function() {{
        var mapObj = window["{map_name}"];
        var GAZ = {gaz_json};
        var input = document.getElementById("gaz-search");
        var results = document.getElementById("gaz-results");
        var highlight = null;

        input.addEventListener("input", function() {{
            var q = this.value.trim().toLowerCase();
            results.innerHTML = "";
            if (q.length < 2) return;
            var matches = [];
            for (var i = 0; i < GAZ.length && matches.length < 8; i++) {{
                if (GAZ[i][0].toLowerCase().indexOf(q) !== -1) matches.push(GAZ[i]);
            }}
            matches.forEach(function(row) {{
                var div = document.createElement("div");
                div.textContent = row[0] + " (" + row[1] + ")";
                div.style.cursor = "pointer";
                div.style.padding = "3px 0";
                div.style.borderBottom = "1px solid #eee";
                div.addEventListener("click", function() {{
                    mapObj.setView([row[2], row[3]], 14);
                    if (highlight) mapObj.removeLayer(highlight);
                    highlight = L.circleMarker([row[2], row[3]], {{
                        radius: 12, color: "#1976d2", fillOpacity: 0, weight: 3
                    }}).addTo(mapObj);
                    results.innerHTML = "";
                    input.value = row[0];
                }});
                results.appendChild(div);
            }});
        }});
    }});
    </script>
    """
    m.get_root().html.add_child(folium.Element(html))


if __name__ == "__main__":
    run()
