#!/usr/bin/env python3
"""Importer: parse garmin-data-export output into the local SQLite DB.

Writer process (the MCP server is read-only). Idempotent: every row is upserted
on its natural key (INSERT OR REPLACE), so re-runs and Garmin's late-arriving
data (sleep/HRV finalize hours later) simply overwrite earlier values.

    python import_to_sqlite.py             # full import
    python import_to_sqlite.py --update    # incremental import

NOTE: the parse step (export text/JSON -> row dicts) is stubbed pending Phase 0
field verification. The DB/upsert layer below is complete and is what seed.py
exercises.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from common import DEFAULT_DB_PATH, connect_rw, normalize_category, now_iso

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

_ACTIVITY_COLS = (
    "activity_id", "date", "start_time", "type", "category",
    "distance_m", "duration_s", "avg_hr", "max_hr",
    "avg_pace_s_per_km", "elevation_gain_m", "title",
)
_DAILY_HEALTH_COLS = (
    "date", "steps", "resting_hr", "hrv_status",
    "body_battery_high", "body_battery_low", "stress_avg",
    "sleep_score", "sleep_duration_s", "intensity_minutes",
)
_TRAINING_METRICS_COLS = (
    "date", "vo2max", "training_readiness",
    "training_load_7d", "training_load_28d", "recovery_time_h",
)
_STREAM_COLS = ("activity_id", "n_samples", "duration_s", "metrics", "streams_json")
_SPLIT_COLS = (
    "activity_id", "lap_index", "distance_m", "duration_s", "moving_duration_s",
    "avg_hr", "max_hr", "avg_pace_s_per_km", "avg_power_w", "avg_run_cadence",
    "elevation_gain_m", "elevation_loss_m",
)
_ZONE_COLS = ("activity_id", "zone_type", "zone_number", "secs_in_zone", "low_boundary")
_WEATHER_COLS = (
    "activity_id", "temp", "apparent_temp", "dew_point", "humidity",
    "wind_speed", "wind_gust", "wind_dir_deg", "wind_compass", "condition", "station",
)


def init_db(conn: sqlite3.Connection) -> None:
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()


def _upsert(conn: sqlite3.Connection, table: str, cols, rows) -> int:
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    n = 0
    for row in rows:
        conn.execute(sql, [row.get(c) for c in cols])
        n += 1
    conn.commit()
    return n


def upsert_activities(conn: sqlite3.Connection, rows) -> int:
    # Derive category from raw type if the caller didn't set it.
    for r in rows:
        r.setdefault("category", normalize_category(r.get("type")))
    return _upsert(conn, "activities", _ACTIVITY_COLS, rows)


def upsert_daily_health(conn: sqlite3.Connection, rows) -> int:
    return _upsert(conn, "daily_health", _DAILY_HEALTH_COLS, rows)


def upsert_training_metrics(conn: sqlite3.Connection, rows) -> int:
    return _upsert(conn, "training_metrics", _TRAINING_METRICS_COLS, rows)


def upsert_activity_streams(conn: sqlite3.Connection, rows) -> int:
    return _upsert(conn, "activity_streams", _STREAM_COLS, rows)


def upsert_activity_splits(conn: sqlite3.Connection, rows) -> int:
    """Replace each activity's splits wholesale so re-runs don't leave stale laps."""
    ids = {r["activity_id"] for r in rows}
    for aid in ids:
        conn.execute("DELETE FROM activity_splits WHERE activity_id = ?", [aid])
    return _upsert(conn, "activity_splits", _SPLIT_COLS, rows)


def upsert_activity_zones(conn: sqlite3.Connection, rows) -> int:
    ids = {r["activity_id"] for r in rows}
    for aid in ids:
        conn.execute("DELETE FROM activity_zones WHERE activity_id = ?", [aid])
    return _upsert(conn, "activity_zones", _ZONE_COLS, rows)


def upsert_activity_weather(conn: sqlite3.Connection, rows) -> int:
    return _upsert(conn, "activity_weather", _WEATHER_COLS, rows)


def update_sync_meta(conn: sqlite3.Connection) -> None:
    """Record this successful sync and the latest data date across tables."""
    latest = conn.execute(
        "SELECT MAX(d) AS d FROM ("
        "  SELECT MAX(date) AS d FROM activities "
        "  UNION ALL SELECT MAX(date) FROM daily_health "
        "  UNION ALL SELECT MAX(date) FROM training_metrics)"
    ).fetchone()["d"]
    conn.execute(
        "INSERT OR REPLACE INTO sync_meta (id, last_sync_ts, latest_data_date) "
        "VALUES (1, ?, ?)",
        [now_iso(), latest],
    )
    conn.commit()


# --- Parse step ------------------------------------------------------------
#
# garmin-data-export writes a single plaintext file of titled sections; in
# --compact mode each section is one line of JSON. Field mapping (verified
# against a real Instinct 3 Solar export, Phase 0):
#
#   Daily Health     dict keyed by 'YYYY-MM-DD' -> nested Garmin API blocks
#   Activities       list of {summary, detail, splits, ...}; we read .summary
#   Training Metrics dict; training_readiness[0] gives score + recoveryTime
#
# Reality on this device: training_status (acute/chronic load) is empty, HRV is
# absent from daily health, and sleep is sparse. Those columns stay NULL.

import json
from glob import glob

SECTION_NAMES = (
    "Profile", "Daily Health", "Activities", "Body Composition",
    "Training Metrics", "Goals and Records", "Trends", "Golf", "Gear",
    "Training Plans", "Workouts", "Hydration", "Nutrition", "Women's Health",
)


def _load_sections(text: str) -> dict:
    """Split the export into {section_name: parsed_json}. Section headers are
    lines exactly equal to a known name (TOC entries are numbered/indented)."""
    lines = text.splitlines()
    starts: dict[str, int] = {}
    for i, line in enumerate(lines):
        if line in SECTION_NAMES and line not in starts:
            starts[line] = i
    boundaries = sorted(starts.values())
    out: dict = {}
    for name, start in starts.items():
        # Bound the scan by the next section header so an empty section ("No data
        # available.") never absorbs the following section's JSON line.
        nexts = [b for b in boundaries if b > start]
        end = min(start + 14, nexts[0] if nexts else len(lines), len(lines))
        for j in range(start + 1, end):
            t = lines[j].strip()
            if t and t[0] in "[{":
                try:
                    out[name] = json.loads(t)
                except json.JSONDecodeError:
                    out[name] = None
                break
    return out


def _resolve_export_file(export_path: str) -> str:
    """Accept a file or a directory; for a directory pick the newest export."""
    if export_path and os.path.isfile(export_path):
        return export_path
    search_dir = export_path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "vendor-garmin-export", "export"
    )
    candidates = sorted(glob(os.path.join(search_dir, "garmin_export_*.txt")))
    if not candidates:
        raise FileNotFoundError(
            f"No garmin_export_*.txt found in {search_dir}. Run the export first."
        )
    return candidates[-1]


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _parse_activities(section) -> list[dict]:
    rows = []
    if not isinstance(section, list):  # empty/no-data sections parse to None or a dict
        return rows
    for item in section:
        s = (item or {}).get("summary") or {}
        aid = s.get("activityId")
        if aid is None:
            continue
        start_local = s.get("startTimeLocal")  # 'YYYY-MM-DD HH:MM:SS'
        day = start_local[:10] if start_local else None
        dist = _num(s.get("distance"))
        dur = _num(s.get("duration"))
        pace = dur / (dist / 1000.0) if dist and dur and dist > 0 else None
        rows.append({
            "activity_id": str(aid),
            "date": day,
            "start_time": start_local.replace(" ", "T") if start_local else None,
            "type": (s.get("activityType") or {}).get("typeKey"),
            "distance_m": dist,
            "duration_s": dur,
            "avg_hr": _num(s.get("averageHR")),
            "max_hr": _num(s.get("maxHR")),
            "avg_pace_s_per_km": pace,
            "elevation_gain_m": _num(s.get("elevationGain")),
            "title": s.get("activityName"),
            # carried for downstream derivations, popped before upsert:
            "_vo2max": _num(s.get("vO2MaxValue")),
            "_intensity": (_num(s.get("moderateIntensityMinutes")) or 0)
                          + (_num(s.get("vigorousIntensityMinutes")) or 0),
        })
    return rows


def _body_battery_high_low(day: dict):
    bb = day.get("Body Battery") or []
    if not bb or not isinstance(bb, list):
        return None, None
    arr = (bb[0] or {}).get("bodyBatteryValuesArray") or []
    vals = [e[1] for e in arr if isinstance(e, list) and len(e) > 1
            and isinstance(e[1], (int, float))]
    return (max(vals), min(vals)) if vals else (None, None)


def _parse_daily_health(section, intensity_by_date: dict) -> list[dict]:
    rows = []
    if not isinstance(section, dict):
        return rows
    for day_str, day in section.items():
        day = day or {}
        ds = day.get("Daily Summary") or {}
        hr = day.get("Heart Rate") or {}
        stress = day.get("Stress") or {}
        sleep_dto = (day.get("Sleep") or {}).get("dailySleepDTO") or {}
        scores = sleep_dto.get("sleepScores") or {}
        sleep_score = (scores.get("overall") or {}).get("value") if scores else None
        bb_high, bb_low = _body_battery_high_low(day)
        rows.append({
            "date": day_str,
            "steps": _num(ds.get("totalSteps")),
            "resting_hr": _num(hr.get("restingHeartRate")),
            "hrv_status": None,  # absent from this device's export
            "body_battery_high": bb_high,
            "body_battery_low": bb_low,
            "stress_avg": _num(stress.get("avgStressLevel")),
            "sleep_score": _num(sleep_score),
            "sleep_duration_s": _num(sleep_dto.get("sleepTimeSeconds")),
            "intensity_minutes": intensity_by_date.get(day_str),
        })
    return rows


def _parse_training_metrics(section, latest_vo2max) -> list[dict]:
    """Training Metrics is a point-in-time snapshot (readiness for one date).
    Daily --update runs accumulate the series over time."""
    if not isinstance(section, dict):
        return []
    rows = []
    seen = set()
    for key in ("training_readiness", "morning_readiness"):
        block = section.get(key)
        rec = block[0] if isinstance(block, list) and block else block
        if not isinstance(rec, dict):
            continue
        d = rec.get("calendarDate")
        if not d or d in seen:
            continue
        seen.add(d)
        rt = _num(rec.get("recoveryTime"))  # minutes
        rows.append({
            "date": d,
            "vo2max": latest_vo2max,
            "training_readiness": _num(rec.get("score")),
            "training_load_7d": None,   # training_status empty on this device
            "training_load_28d": None,
            "recovery_time_h": round(rt / 60.0, 1) if rt is not None else None,
        })
    return rows


# --- Per-activity cache (full-resolution detail) ---------------------------
#
# The compact text export omits time-series, splits, zones and weather "for
# size", but garmin-data-export still fetches them and writes a per-activity
# JSON cache next to the export. We read that cache directly: full resolution,
# no re-fetch, no vendor changes. Cache layout: <export_dir>/.cache/activities/<id>.json

# Garmin metricDescriptor key -> our stream column name. Timing keys feed the
# offset_s derivation and are dropped from the stored streams afterwards.
_STREAM_KEY_MAP = {
    "sumElapsedDuration": "_elapsed_s",
    "sumDuration": "_timer_s",
    "sumMovingDuration": "_moving_s",
    "directTimestamp": "_ts_ms",
    "directHeartRate": "hr",
    "directSpeed": "speed_mps",
    "directGradeAdjustedSpeed": "grade_adj_speed_mps",
    "sumDistance": "distance_m",
    "directPower": "power_w",
    "directRunCadence": "run_cadence_spm",
    "directDoubleCadence": "cadence_spm",
    "directElevation": "elevation_m",
    "directVerticalSpeed": "vertical_speed_mps",
    "directVerticalOscillation": "vertical_oscillation_mm",
    "directGroundContactTime": "ground_contact_ms",
    "directVerticalRatio": "vertical_ratio",
    "directStrideLength": "stride_length_cm",
    "directPerformanceCondition": "performance_condition",
    "directBodyBattery": "body_battery",
    "directLatitude": "lat",
    "directLongitude": "lon",
}
_TIMING_COLS = ("_elapsed_s", "_timer_s", "_moving_s", "_ts_ms")


def _cache_dir_for(export_file: str) -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(export_file)), ".cache", "activities"
    )


def _default_cache_dir() -> str:
    """Cache dir independent of any export file — used by --backfill."""
    base = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "vendor-garmin-export", "export",
    )
    return os.path.join(base, ".cache", "activities")


def _derive_offsets(cols: dict, n: int) -> list:
    """Per-sample seconds-since-start. Prefer elapsed, fall back to timer, then
    raw GMT timestamps normalized to the first sample, then the sample index."""
    for key in ("_elapsed_s", "_timer_s", "_moving_s"):
        arr = cols.get(key)
        if arr and any(isinstance(v, (int, float)) for v in arr):
            return [round(v, 1) if isinstance(v, (int, float)) else None for v in arr]
    ts = cols.get("_ts_ms")
    if ts and isinstance(ts[0], (int, float)):
        t0 = ts[0]
        return [round((v - t0) / 1000.0, 1) if isinstance(v, (int, float)) else None
                for v in ts]
    return list(range(n))


def _parse_streams(activity_id: str, details: dict) -> dict | None:
    descs = (details or {}).get("metricDescriptors") or []
    rows = (details or {}).get("activityDetailMetrics") or []
    if not descs or not rows:
        return None
    idx = {}
    for m in descs:
        name = _STREAM_KEY_MAP.get(m.get("key"))
        i = m.get("metricsIndex")
        if name and isinstance(i, int):
            idx[name] = i
    if not idx:
        return None
    cols = {name: [] for name in idx}
    for r in rows:
        vals = r.get("metrics") or []
        for name, i in idx.items():
            cols[name].append(vals[i] if i < len(vals) else None)

    n = len(rows)
    streams = {"offset_s": _derive_offsets(cols, n)}
    for name, arr in cols.items():
        if name not in _TIMING_COLS:
            streams[name] = arr
    offs = [o for o in streams["offset_s"] if isinstance(o, (int, float))]
    duration_s = max(offs) if offs else None
    metric_names = [k for k in streams if k != "offset_s"]
    return {
        "activity_id": activity_id,
        "n_samples": n,
        "duration_s": duration_s,
        "metrics": json.dumps(metric_names),
        "streams_json": json.dumps(streams),
    }


def _parse_splits(activity_id: str, splits: dict) -> list[dict]:
    laps = (splits or {}).get("lapDTOs") or []
    out = []
    for lap in laps:
        if not isinstance(lap, dict):
            continue
        speed = _num(lap.get("averageSpeed"))
        pace = 1000.0 / speed if speed and speed > 0 else None
        out.append({
            "activity_id": activity_id,
            "lap_index": _num(lap.get("lapIndex")),
            "distance_m": _num(lap.get("distance")),
            "duration_s": _num(lap.get("duration")),
            "moving_duration_s": _num(lap.get("movingDuration")),
            "avg_hr": _num(lap.get("averageHR")),
            "max_hr": _num(lap.get("maxHR")),
            "avg_pace_s_per_km": round(pace, 1) if pace else None,
            "avg_power_w": _num(lap.get("averagePower")),
            "avg_run_cadence": _num(lap.get("averageRunCadence")),
            "elevation_gain_m": _num(lap.get("elevationGain")),
            "elevation_loss_m": _num(lap.get("elevationLoss")),
        })
    return [r for r in out if r["lap_index"] is not None]


def _parse_zones(activity_id: str, zones, zone_type: str) -> list[dict]:
    out = []
    for z in zones or []:
        if not isinstance(z, dict):
            continue
        zn = _num(z.get("zoneNumber"))
        if zn is None:
            continue
        out.append({
            "activity_id": activity_id,
            "zone_type": zone_type,
            "zone_number": int(zn),
            "secs_in_zone": _num(z.get("secsInZone")),
            "low_boundary": _num(z.get("zoneLowBoundary")),
        })
    return out


def _parse_weather(activity_id: str, weather: dict) -> dict | None:
    w = weather or {}
    if not w:
        return None
    return {
        "activity_id": activity_id,
        "temp": _num(w.get("temp")),
        "apparent_temp": _num(w.get("apparentTemp")),
        "dew_point": _num(w.get("dewPoint")),
        "humidity": _num(w.get("relativeHumidity")),
        "wind_speed": _num(w.get("windSpeed")),
        "wind_gust": _num(w.get("windGust")),
        "wind_dir_deg": _num(w.get("windDirection")),
        "wind_compass": w.get("windDirectionCompassPoint"),
        "condition": (w.get("weatherTypeDTO") or {}).get("desc"),
        "station": (w.get("weatherStationDTO") or {}).get("name"),
    }


def _read_cache_rows(cache_dir: str, activity_ids):
    """Read per-activity cache for the given ids -> (streams, splits, zones,
    weather) row lists. Missing files/blocks degrade silently; never raises."""
    streams, splits, zones, weather = [], [], [], []
    if not os.path.isdir(cache_dir):
        return streams, splits, zones, weather
    for aid in activity_ids:
        path = os.path.join(cache_dir, f"{aid}.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        st = _parse_streams(aid, data.get("details") or {})
        if st:
            streams.append(st)
        splits.extend(_parse_splits(aid, data.get("splits") or {}))
        zones.extend(_parse_zones(aid, data.get("hr_zones"), "hr"))
        zones.extend(_parse_zones(aid, data.get("power_zones"), "power"))
        wx = _parse_weather(aid, data.get("weather") or {})
        if wx:
            weather.append(wx)
    return streams, splits, zones, weather


def parse_activity_cache(export_file: str, activity_ids):
    """Read full-resolution detail from the cache sibling of the export file."""
    cache_dir = _cache_dir_for(export_file)
    if not os.path.isdir(cache_dir):
        print(f"No activity cache at {cache_dir}; streams/splits/zones skipped.")
    return _read_cache_rows(cache_dir, activity_ids)


def backfill_activity_details(conn: sqlite3.Connection, cache_dir: str | None = None) -> dict:
    """Fill streams/splits/zones/weather for DB activities that have no streams
    row yet, reading the cache directly. Idempotent and independent of the
    export text — run any time to heal gaps (e.g. cache that arrived late)."""
    cache_dir = cache_dir or _default_cache_dir()
    have = {r["activity_id"] for r in
            conn.execute("SELECT activity_id FROM activity_streams")}
    ids = [r["activity_id"] for r in
           conn.execute("SELECT activity_id FROM activities ORDER BY date")]
    todo = [a for a in ids if a not in have]
    streams, splits, zones, weather = _read_cache_rows(cache_dir, todo)
    n_s = upsert_activity_streams(conn, streams)
    n_sp = upsert_activity_splits(conn, splits)
    n_z = upsert_activity_zones(conn, zones)
    n_w = upsert_activity_weather(conn, weather)
    update_sync_meta(conn)
    return {"candidates": len(todo), "streams": n_s, "splits": n_sp,
            "zones": n_z, "weather": n_w, "cache_dir": cache_dir}


def parse_export(export_path: str, update: bool = False):
    """Parse a garmin-data-export into row lists for every table.

    Returns (activities, daily_health, metrics, streams, splits, zones, weather).
    Scalar tables come from the compact text export; per-activity detail
    (streams/splits/zones/weather) comes from the sibling JSON cache."""
    path = _resolve_export_file(export_path)
    print(f"Parsing {path}")
    with open(path, encoding="utf-8") as f:
        sections = _load_sections(f.read())

    activities = _parse_activities(sections.get("Activities"))

    # Per-day intensity minutes derived from activities (no reliable daily field);
    # latest VO2max taken from the most recent activity that reports it.
    intensity_by_date: dict[str, int] = {}
    latest_vo2max = None
    latest_date = ""
    for a in activities:
        intensity = a.pop("_intensity", 0)
        if a["date"] and intensity:
            intensity_by_date[a["date"]] = intensity_by_date.get(a["date"], 0) + intensity
        vo2 = a.pop("_vo2max", None)
        if vo2 is not None and (a["date"] or "") >= latest_date:
            latest_vo2max, latest_date = vo2, a["date"] or latest_date

    daily_health = _parse_daily_health(sections.get("Daily Health"), intensity_by_date)
    metrics = _parse_training_metrics(sections.get("Training Metrics"), latest_vo2max)

    activity_ids = [a["activity_id"] for a in activities if a.get("activity_id")]
    streams, splits, zones, weather = parse_activity_cache(path, activity_ids)
    return activities, daily_health, metrics, streams, splits, zones, weather


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Import garmin-data-export into SQLite")
    p.add_argument("--update", action="store_true", help="incremental import")
    p.add_argument("--backfill", action="store_true",
                   help="fill streams/splits/zones/weather for DB activities "
                        "missing them, from the per-activity cache; skips the "
                        "export-text parse entirely")
    p.add_argument("--db", default=os.environ.get("GARMIN_DB_PATH", DEFAULT_DB_PATH))
    p.add_argument("--export-path", default=os.environ.get("GARMIN_EXPORT_PATH", ""))
    args = p.parse_args(argv)

    if args.backfill:
        conn = connect_rw(args.db)
        init_db(conn)
        res = backfill_activity_details(conn)
        conn.close()
        print(f"Backfill: {res['candidates']} candidates -> {res['streams']} streams, "
              f"{res['splits']} splits, {res['zones']} zones, {res['weather']} weather")
        return 0

    conn = connect_rw(args.db)
    init_db(conn)
    activities, health, metrics, streams, splits, zones, weather = parse_export(
        args.export_path, update=args.update
    )
    n_a = upsert_activities(conn, activities)
    n_h = upsert_daily_health(conn, health)
    n_m = upsert_training_metrics(conn, metrics)
    n_s = upsert_activity_streams(conn, streams)
    n_sp = upsert_activity_splits(conn, splits)
    n_z = upsert_activity_zones(conn, zones)
    n_w = upsert_activity_weather(conn, weather)
    update_sync_meta(conn)
    conn.close()
    print(f"Imported: {n_a} activities, {n_h} daily_health, {n_m} training_metrics, "
          f"{n_s} streams, {n_sp} splits, {n_z} zones, {n_w} weather")
    return 0


if __name__ == "__main__":
    sys.exit(main())
