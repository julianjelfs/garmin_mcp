"""Pure query functions for the MCP tools.

Each takes an open sqlite3 connection plus params and returns plain JSON-able
dicts. No mcp imports, no freshness wrapping (the server adds that uniformly),
so these are unit-testable in isolation.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta

from common import DEFAULT_ROW_CAP, normalize_category

ACTIVITY_COLUMNS = (
    "activity_id", "date", "start_time", "type", "category",
    "distance_m", "duration_s", "avg_hr", "max_hr",
    "avg_pace_s_per_km", "elevation_gain_m", "title",
)
DAILY_HEALTH_COLUMNS = (
    "date", "steps", "resting_hr", "hrv_status",
    "hrv_last_night_avg", "hrv_weekly_avg", "hrv_last_night_5min_high",
    "hrv_baseline_low_upper", "hrv_baseline_balanced_low",
    "hrv_baseline_balanced_upper",
    "body_battery_high", "body_battery_low", "stress_avg",
    "sleep_score", "sleep_duration_s", "intensity_minutes",
)
TRAINING_METRICS_COLUMNS = (
    "date", "vo2max", "training_readiness",
    "training_load_7d", "training_load_28d", "recovery_time_h",
)


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _capped(rows: list, cap: int) -> tuple[list, int, bool]:
    """Return (capped_rows, total_matched, truncated)."""
    total = len(rows)
    truncated = total > cap
    return rows[:cap], total, truncated


def _list_payload(rows: list[dict], cap: int) -> dict:
    capped, total, truncated = _capped(rows, cap)
    return {
        "rows": capped,
        "returned_count": len(capped),
        "total_matched": total,
        "truncated": truncated,
    }


def recent_activities(
    conn: sqlite3.Connection,
    days: int = 14,
    activity_type: str | None = None,
    cap: int = DEFAULT_ROW_CAP,
) -> dict:
    since = (date.today() - timedelta(days=days)).isoformat()
    sql = f"SELECT {', '.join(ACTIVITY_COLUMNS)} FROM activities WHERE date >= ?"
    params: list = [since]
    if activity_type:
        sql += " AND category = ?"
        params.append(normalize_category(activity_type))
    sql += " ORDER BY date DESC, start_time DESC"
    rows = _rows_to_dicts(conn.execute(sql, params).fetchall())
    payload = _list_payload(rows, cap)
    payload["lookback_days"] = days
    return payload


def daily_health(
    conn: sqlite3.Connection, days: int = 14, cap: int = DEFAULT_ROW_CAP
) -> dict:
    since = (date.today() - timedelta(days=days)).isoformat()
    sql = (
        f"SELECT {', '.join(DAILY_HEALTH_COLUMNS)} FROM daily_health "
        "WHERE date >= ? ORDER BY date DESC"
    )
    rows = _rows_to_dicts(conn.execute(sql, [since]).fetchall())
    payload = _list_payload(rows, cap)
    payload["lookback_days"] = days
    return payload


def training_metrics(
    conn: sqlite3.Connection, days: int = 28, cap: int = DEFAULT_ROW_CAP
) -> dict:
    since = (date.today() - timedelta(days=days)).isoformat()
    sql = (
        f"SELECT {', '.join(TRAINING_METRICS_COLUMNS)} FROM training_metrics "
        "WHERE date >= ? ORDER BY date DESC"
    )
    rows = _rows_to_dicts(conn.execute(sql, [since]).fetchall())
    payload = _list_payload(rows, cap)
    payload["lookback_days"] = days
    return payload


def activity_detail(conn: sqlite3.Connection, activity_id: str) -> dict:
    row = conn.execute(
        f"SELECT {', '.join(ACTIVITY_COLUMNS)} FROM activities WHERE activity_id = ?",
        [activity_id],
    ).fetchone()
    if row is None:
        return {"found": False, "activity_id": activity_id,
                "error": f"No activity with id {activity_id!r}"}
    return {"found": True, "activity": dict(row)}


def search_activities(
    conn: sqlite3.Connection,
    from_date: str | None = None,
    to_date: str | None = None,
    activity_type: str | None = None,
    min_distance_km: float | None = None,
    cap: int = DEFAULT_ROW_CAP,
) -> dict:
    sql = f"SELECT {', '.join(ACTIVITY_COLUMNS)} FROM activities WHERE 1=1"
    params: list = []
    if from_date:
        sql += " AND date >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND date <= ?"
        params.append(to_date)
    if activity_type:
        sql += " AND category = ?"
        params.append(normalize_category(activity_type))
    if min_distance_km is not None:
        sql += " AND distance_m >= ?"
        params.append(min_distance_km * 1000.0)
    sql += " ORDER BY date DESC, start_time DESC"
    rows = _rows_to_dicts(conn.execute(sql, params).fetchall())
    return _list_payload(rows, cap)


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def training_load(conn: sqlite3.Connection, weeks: int = 8) -> dict:
    """Mon-Sun weekly aggregates over the last `weeks` ISO weeks (incl. current).

    The in-progress current week is flagged is_partial=True.
    """
    today = date.today()
    this_monday = _monday(today)
    first_monday = this_monday - timedelta(weeks=weeks - 1)
    rows = conn.execute(
        f"SELECT {', '.join(ACTIVITY_COLUMNS)} FROM activities WHERE date >= ?",
        [first_monday.isoformat()],
    ).fetchall()

    # Bucket activities by their week's Monday.
    buckets: dict[str, list] = {}
    for r in rows:
        try:
            wk = _monday(date.fromisoformat(r["date"])).isoformat()
        except (ValueError, TypeError):
            continue
        buckets.setdefault(wk, []).append(r)

    out = []
    for i in range(weeks):
        wk_monday = first_monday + timedelta(weeks=i)
        wk = wk_monday.isoformat()
        acts = buckets.get(wk, [])
        dists = [a["distance_m"] for a in acts if a["distance_m"] is not None]
        durs = [a["duration_s"] for a in acts if a["duration_s"] is not None]
        hrs = [a["avg_hr"] for a in acts if a["avg_hr"] is not None]
        out.append({
            "week_start": wk,
            "is_partial": wk_monday == this_monday,
            "activity_count": len(acts),
            "total_distance_m": sum(dists) if dists else 0,
            "total_duration_s": sum(durs) if durs else 0,
            "avg_hr": round(sum(hrs) / len(hrs), 1) if hrs else None,
            "long_run_distance_m": max(dists) if dists else 0,
        })
    out.sort(key=lambda w: w["week_start"], reverse=True)
    return {"weeks": out, "weeks_requested": weeks}


# --- Per-activity time-series: streams + analysis --------------------------

def _load_streams(conn: sqlite3.Connection, activity_id: str) -> dict | None:
    row = conn.execute(
        "SELECT n_samples, duration_s, streams_json FROM activity_streams "
        "WHERE activity_id = ?",
        [activity_id],
    ).fetchone()
    if row is None:
        return None
    try:
        streams = json.loads(row["streams_json"])
    except (TypeError, ValueError):
        return None
    return {"n_samples": row["n_samples"], "duration_s": row["duration_s"],
            "streams": streams}


def _downsample(streams: dict, downsample_s: float, max_points: int):
    """Bucket samples into fixed time windows, averaging numerics per bucket.

    Returns (downsampled_streams, effective_window_s). The window grows past
    downsample_s when needed to keep the point count under max_points."""
    offs = streams.get("offset_s") or []
    metric_names = [k for k in streams if k != "offset_s"]
    valid_offs = [o for o in offs if isinstance(o, (int, float))]
    if not valid_offs:
        return streams, downsample_s
    dur = max(valid_offs)
    win = downsample_s
    if max_points and dur > 0:
        win = max(downsample_s, math.ceil((dur + 1) / max_points))
    win = max(1.0, float(win))

    buckets: dict[int, dict] = {}
    for i, o in enumerate(offs):
        if not isinstance(o, (int, float)):
            continue
        b = int(o // win)
        bk = buckets.get(b)
        if bk is None:
            bk = buckets[b] = {"_off": [], **{m: [] for m in metric_names}}
        bk["_off"].append(o)
        for m in metric_names:
            v = streams[m][i]
            if isinstance(v, (int, float)) and v is not True and v is not False:
                bk[m].append(v)

    out: dict = {"offset_s": []}
    for m in metric_names:
        out[m] = []
    for b in sorted(buckets):
        bk = buckets[b]
        out["offset_s"].append(round(sum(bk["_off"]) / len(bk["_off"]), 1))
        for m in metric_names:
            vals = bk[m]
            out[m].append(round(sum(vals) / len(vals), 2) if vals else None)
    return out, win


def _add_pace(streams: dict) -> None:
    """Derive pace_s_per_km from speed_mps in place (None where stopped)."""
    spd = streams.get("speed_mps")
    if spd is None:
        return
    streams["pace_s_per_km"] = [
        round(1000.0 / s, 1) if isinstance(s, (int, float)) and s > 0 else None
        for s in spd
    ]


def activity_streams(
    conn: sqlite3.Connection,
    activity_id: str,
    metrics: list[str] | None = None,
    downsample_s: float = 30,
    max_points: int = 300,
) -> dict:
    """Downsampled time-series for one activity. `metrics` filters which columns
    are returned (offset_s always included); None returns all captured metrics."""
    raw = _load_streams(conn, activity_id)
    if raw is None:
        return {"found": False, "activity_id": activity_id,
                "error": f"No stored streams for activity {activity_id!r}. "
                         "It may predate stream capture, or have no detail samples."}
    streams = raw["streams"]
    if metrics:
        wanted = set(metrics)
        streams = {k: v for k, v in streams.items()
                   if k == "offset_s" or k in wanted}
    ds, win = _downsample(streams, downsample_s, max_points)
    _add_pace(ds)
    return {
        "found": True,
        "activity_id": activity_id,
        "n_samples": raw["n_samples"],
        "duration_s": raw["duration_s"],
        "downsample_s": win,
        "point_count": len(ds.get("offset_s", [])),
        "metrics": [k for k in ds if k != "offset_s"],
        "streams": ds,
    }


def _half_stats(pts: list) -> dict:
    """pts: list of (offset_s, hr, speed_mps). Mean HR over valid HR; mean speed
    over moving samples (>0.5 m/s) so stops don't deflate pace."""
    hrs = [h for _, h, _ in pts if isinstance(h, (int, float))]
    spds = [s for _, _, s in pts if isinstance(s, (int, float)) and s > 0.5]
    avg_hr = sum(hrs) / len(hrs) if hrs else None
    avg_sp = sum(spds) / len(spds) if spds else None
    pace = 1000.0 / avg_sp if avg_sp else None
    eff = (avg_sp / avg_hr) if (avg_sp and avg_hr) else None
    return {
        "avg_hr": round(avg_hr, 1) if avg_hr is not None else None,
        "avg_speed_mps": round(avg_sp, 3) if avg_sp is not None else None,
        "avg_pace_s_per_km": round(pace, 1) if pace is not None else None,
        "efficiency": eff,
        "sample_count": len(pts),
    }


def _zone_time(conn: sqlite3.Connection, activity_id: str) -> dict:
    rows = conn.execute(
        "SELECT zone_type, zone_number, secs_in_zone, low_boundary "
        "FROM activity_zones WHERE activity_id = ? ORDER BY zone_type, zone_number",
        [activity_id],
    ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["zone_type"], []).append({
            "zone": r["zone_number"],
            "secs": r["secs_in_zone"],
            "low_boundary": r["low_boundary"],
        })
    return out


def activity_splits(conn: sqlite3.Connection, activity_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT lap_index, distance_m, duration_s, moving_duration_s, avg_hr, "
        "max_hr, avg_pace_s_per_km, avg_power_w, avg_run_cadence, "
        "elevation_gain_m, elevation_loss_m FROM activity_splits "
        "WHERE activity_id = ? ORDER BY lap_index",
        [activity_id],
    ).fetchall()
    return _rows_to_dicts(rows)


def activity_analysis(conn: sqlite3.Connection, activity_id: str) -> dict:
    """HR drift / cardiac decoupling plus zone time and splits for one activity.

    Decoupling compares aerobic efficiency (speed/HR) of the first vs second
    half: positive decoupling_pct means HR rose relative to pace (cardiac
    drift / fatigue). hr_drift_pct is the simpler second-vs-first mean-HR rise.
    Pa:HR convention — under ~5% is well-coupled for an aerobic effort."""
    raw = _load_streams(conn, activity_id)
    if raw is None:
        return {"found": False, "activity_id": activity_id,
                "error": f"No stored streams for activity {activity_id!r}."}
    streams = raw["streams"]
    offs = streams.get("offset_s") or []
    hr = streams.get("hr") or []
    spd = streams.get("speed_mps") or []
    n = len(offs)
    hr = (hr + [None] * n)[:n]
    spd = (spd + [None] * n)[:n]

    pts = [(o, h, s) for o, h, s in zip(offs, hr, spd)
           if isinstance(o, (int, float)) and isinstance(h, (int, float))]

    result: dict = {
        "found": True,
        "activity_id": activity_id,
        "n_samples": raw["n_samples"],
        "duration_s": raw["duration_s"],
        "zones": _zone_time(conn, activity_id),
        "splits": activity_splits(conn, activity_id),
    }

    if len(pts) < 4:
        result["hr_drift"] = {"computable": False,
                              "reason": "too few heart-rate samples"}
        return result

    mid = (pts[0][0] + pts[-1][0]) / 2.0
    first = [p for p in pts if p[0] <= mid]
    second = [p for p in pts if p[0] > mid]
    h1, h2 = _half_stats(first), _half_stats(second)

    decoupling = None
    if h1["efficiency"] and h2["efficiency"]:
        decoupling = (h1["efficiency"] - h2["efficiency"]) / h1["efficiency"] * 100.0
    hr_drift = None
    if h1["avg_hr"] and h2["avg_hr"]:
        hr_drift = (h2["avg_hr"] - h1["avg_hr"]) / h1["avg_hr"] * 100.0

    for h in (h1, h2):
        h.pop("efficiency", None)
    result["hr_drift"] = {
        "computable": True,
        "decoupling_pct": round(decoupling, 2) if decoupling is not None else None,
        "hr_drift_pct": round(hr_drift, 2) if hr_drift is not None else None,
        "first_half": h1,
        "second_half": h2,
        "method": "Pa:HR decoupling — efficiency=speed/HR, halves split by elapsed time; "
                  "moving samples (>0.5 m/s) for pace.",
    }
    return result
