#!/usr/bin/env python3
"""Seed the DB with synthetic data so the server can be exercised end-to-end
without a real Garmin export (Phase 0 not required for this).

    python seed.py --db /path/to/garmin.db

Uses the importer's real upsert layer, so it also verifies that path.
"""

from __future__ import annotations

import argparse
import os
from datetime import date, timedelta

from common import DEFAULT_DB_PATH, connect_rw
from import_to_sqlite import (
    init_db,
    update_sync_meta,
    upsert_activities,
    upsert_daily_health,
    upsert_training_metrics,
)

TYPES = ["running", "trail_running", "treadmill_running", "hiking", "walking"]


def build(days: int = 35):
    today = date.today()
    activities, health, metrics = [], [], []
    for i in range(days):
        d = today - timedelta(days=i)
        ds = d.isoformat()
        # An activity every other day, cycling through types.
        if i % 2 == 0:
            t = TYPES[i % len(TYPES)]
            dist = 8000 + (i % 5) * 2500  # 8-18 km
            dur = dist / 3.0               # ~3 m/s
            activities.append({
                "activity_id": f"seed-{ds}",
                "date": ds,
                "start_time": f"{ds}T07:30:00",
                "type": t,
                "distance_m": dist,
                "duration_s": dur,
                "avg_hr": 138 + (i % 4) * 5,
                "max_hr": 165 + (i % 3) * 4,
                "avg_pace_s_per_km": dur / (dist / 1000.0),
                "elevation_gain_m": 50 + (i % 6) * 40,
                "title": f"{t.replace('_', ' ').title()} {ds}",
            })
        health.append({
            "date": ds,
            "steps": 9000 + (i % 7) * 800,
            "resting_hr": 48 + (i % 5),
            "hrv_status": ["balanced", "balanced", "low", "unbalanced"][i % 4],
            "body_battery_high": 90 - (i % 6) * 4,
            "body_battery_low": 18 + (i % 5) * 3,
            "stress_avg": 28 + (i % 6) * 4,
            "sleep_score": 78 - (i % 8) * 3,
            "sleep_duration_s": (7.0 + (i % 4) * 0.4) * 3600,
            "intensity_minutes": 40 + (i % 5) * 20,
        })
        # Training metrics: leave advanced cols mostly NULL to mimic Instinct 3.
        metrics.append({
            "date": ds,
            "vo2max": 47.0 + (i % 3) * 0.3,
            "training_readiness": None,
            "training_load_7d": None,
            "training_load_28d": None,
            "recovery_time_h": None,
        })
    return activities, health, metrics


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Seed synthetic Garmin data")
    p.add_argument("--db", default=os.environ.get("GARMIN_DB_PATH", DEFAULT_DB_PATH))
    p.add_argument("--days", type=int, default=35)
    args = p.parse_args(argv)

    conn = connect_rw(args.db)
    init_db(conn)
    a, h, m = build(args.days)
    na = upsert_activities(conn, a)
    nh = upsert_daily_health(conn, h)
    nm = upsert_training_metrics(conn, m)
    update_sync_meta(conn)
    conn.close()
    print(f"Seeded: {na} activities, {nh} daily_health, {nm} training_metrics -> {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
