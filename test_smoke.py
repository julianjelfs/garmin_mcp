#!/usr/bin/env python3
"""Smoke tests — seed a temp DB and assert the query layer behaves.

Runs without pytest:  python test_smoke.py
"""

from __future__ import annotations

import os
import tempfile

import json

import queries
from common import connect_ro, normalize_category, get_freshness
from seed import build
from import_to_sqlite import (
    init_db, upsert_activities, upsert_daily_health,
    upsert_training_metrics, update_sync_meta,
    upsert_activity_streams, upsert_activity_splits, upsert_activity_zones,
    backfill_activity_details,
)
from common import connect_rw

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}")


def main() -> int:
    # category normalization
    check("trail_running -> run", normalize_category("trail_running") == "run")
    check("treadmill_running -> run", normalize_category("treadmill_running") == "run")
    check("hiking -> hike", normalize_category("hiking") == "hike")
    check("trekking -> hike", normalize_category("trekking") == "hike")
    check("mountain_biking -> bike", normalize_category("mountain_biking") == "bike")
    check("None -> other", normalize_category(None) == "other")

    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "garmin.db")
    conn = connect_rw(db)
    init_db(conn)
    a, h, m = build(35)
    upsert_activities(conn, a)
    upsert_daily_health(conn, h)
    upsert_training_metrics(conn, m)

    # Synthetic run streams: HR climbs 130->150 at constant 3.0 m/s -> drift > 0.
    stream_aid = a[0]["activity_id"]
    npts = 120
    offset = [i * 30.0 for i in range(npts)]          # 0..3570s
    hr = [130.0 + 20.0 * (i / (npts - 1)) for i in range(npts)]
    speed = [3.0] * npts
    streams = {"offset_s": offset, "hr": hr, "speed_mps": speed}
    upsert_activity_streams(conn, [{
        "activity_id": stream_aid, "n_samples": npts, "duration_s": offset[-1],
        "metrics": json.dumps(["hr", "speed_mps"]), "streams_json": json.dumps(streams),
    }])
    upsert_activity_splits(conn, [{
        "activity_id": stream_aid, "lap_index": 1, "distance_m": 1000.0,
        "duration_s": 333.0, "moving_duration_s": 333.0, "avg_hr": 140, "max_hr": 150,
        "avg_pace_s_per_km": 333.0, "avg_power_w": None, "avg_run_cadence": 168.0,
        "elevation_gain_m": 5.0, "elevation_loss_m": 3.0,
    }])
    upsert_activity_zones(conn, [{
        "activity_id": stream_aid, "zone_type": "hr", "zone_number": 1,
        "secs_in_zone": 1200.0, "low_boundary": 120,
    }])

    # Backfill: a second activity has no streams row but does have a cache file.
    backfill_aid = a[2]["activity_id"]
    cache_dir = os.path.join(tmp, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, f"{backfill_aid}.json"), "w") as f:
        json.dump({"details": {
            "metricDescriptors": [
                {"key": "directHeartRate", "metricsIndex": 0},
                {"key": "directSpeed", "metricsIndex": 1},
                {"key": "sumElapsedDuration", "metricsIndex": 2},
            ],
            "activityDetailMetrics": [
                {"metrics": [120.0 + i, 3.0, i * 10.0]} for i in range(20)
            ],
        }}, f)
    bf = backfill_activity_details(conn, cache_dir=cache_dir)
    check("backfill picks up missing activity", bf["candidates"] >= 1)
    check("backfill wrote one stream", bf["streams"] == 1)

    update_sync_meta(conn)
    conn.close()

    ro = connect_ro(db)

    # recent activities + category filter
    ra = queries.recent_activities(ro, days=14)
    check("recent_activities returns rows", ra["returned_count"] > 0)
    check("recent_activities has freshness-independent shape",
          {"rows", "total_matched", "truncated"} <= set(ra))
    run_only = queries.recent_activities(ro, days=60, activity_type="running")
    cats = {r["category"] for r in run_only["rows"]}
    check("running filter returns only run category", cats <= {"run"})
    check("running filter catches trail/treadmill",
          any(r["type"] in ("trail_running", "treadmill_running") for r in run_only["rows"]))

    # caps / truncation
    capped = queries.recent_activities(ro, days=60, cap=2)
    check("cap limits returned_count", capped["returned_count"] == 2)
    check("cap sets truncated", capped["truncated"] is True)
    check("cap keeps total_matched", capped["total_matched"] > 2)

    # daily health nulls preserved
    dh = queries.daily_health(ro, days=10)
    check("daily_health returns rows", dh["returned_count"] > 0)

    # training metrics nullable advanced cols
    tm = queries.training_metrics(ro, days=10)
    check("training_metrics readiness is null (Instinct-like)",
          all(r["training_readiness"] is None for r in tm["rows"]))

    # training load weekly buckets
    tl = queries.training_load(ro, weeks=4)
    check("training_load returns 4 weeks", len(tl["weeks"]) == 4)
    check("exactly one partial week",
          sum(1 for w in tl["weeks"] if w["is_partial"]) == 1)
    check("weeks sorted desc",
          [w["week_start"] for w in tl["weeks"]] ==
          sorted([w["week_start"] for w in tl["weeks"]], reverse=True))

    # detail found / not found
    first_id = ra["rows"][0]["activity_id"]
    det = queries.activity_detail(ro, first_id)
    check("activity_detail found", det["found"] is True)
    missing = queries.activity_detail(ro, "does-not-exist")
    check("activity_detail not found returns structured error",
          missing["found"] is False and "error" in missing)

    # search filters
    sr = queries.search_activities(ro, min_distance_km=15)
    check("search min_distance filters",
          all(r["distance_m"] >= 15000 for r in sr["rows"]))

    # streams: downsample + derived pace
    st = queries.activity_streams(ro, stream_aid, downsample_s=60, max_points=50)
    check("streams found", st["found"] is True)
    check("streams downsampled under cap", st["point_count"] <= 50)
    check("streams derive pace from speed", "pace_s_per_km" in st["streams"])
    check("streams missing id returns structured error",
          queries.activity_streams(ro, "nope")["found"] is False)
    check("streams metric filter narrows columns",
          set(queries.activity_streams(ro, stream_aid, metrics=["hr"])["metrics"]) == {"hr"})

    # analysis: HR drift positive when HR climbs at constant pace
    an = queries.activity_analysis(ro, stream_aid)
    check("analysis found", an["found"] is True)
    check("hr_drift computable", an["hr_drift"]["computable"] is True)
    check("hr_drift positive for rising HR", an["hr_drift"]["hr_drift_pct"] > 0)
    check("decoupling positive for rising HR", an["hr_drift"]["decoupling_pct"] > 0)
    check("analysis carries zones", "hr" in an["zones"])
    check("analysis carries splits", len(an["splits"]) == 1)
    check("analysis missing id structured error",
          queries.activity_analysis(ro, "nope")["found"] is False)

    # backfilled activity is now queryable
    check("backfilled activity has streams",
          queries.activity_streams(ro, backfill_aid)["found"] is True)

    # freshness
    fr = get_freshness(ro)
    check("freshness has latest_data_date", fr["latest_data_date"] is not None)
    check("freshness days_since_sync == 0", fr["days_since_sync"] == 0)

    ro.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
