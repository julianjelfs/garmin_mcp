#!/usr/bin/env python3
"""Garmin Training Assistant — read-only MCP server (stdio transport).

Exposes the local SQLite DB and the athlete context YAML as MCP tools for
Claude Desktop. The server is read-only and must never crash on launch, so the
DB is opened lazily per call and every failure is returned as a structured
error inside the tool response.

Run as MCP server:   python garmin_mcp_server.py
Validate setup:      python garmin_mcp_server.py --check
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

from mcp.server.fastmcp import FastMCP

import queries
from common import (
    DEFAULT_CONTEXT_PATH,
    DEFAULT_DB_PATH,
    connect_ro,
    db_exists,
    get_freshness,
)

DB_PATH = os.environ.get("GARMIN_DB_PATH", DEFAULT_DB_PATH)
CONTEXT_PATH = os.environ.get("ATHLETE_CONTEXT_PATH", DEFAULT_CONTEXT_PATH)
SYNC_SCRIPT = os.environ.get(
    "GARMIN_SYNC_SCRIPT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync.sh"),
)

mcp = FastMCP("garmin-assistant")


def _db_call(fn) -> dict:
    """Open the DB read-only, run fn(conn), attach freshness, return a dict.

    Any DB problem becomes a structured error rather than an exception, so the
    tool stays usable and Claude Desktop never loses the server.
    """
    if not db_exists(DB_PATH):
        return {"error": "database_not_found",
                "message": f"No database at {DB_PATH}. Run the importer first."}
    try:
        conn = connect_ro(DB_PATH)
    except sqlite3.Error as e:
        return {"error": "database_unreadable", "message": str(e)}
    try:
        result = fn(conn)
        result["freshness"] = get_freshness(conn)
        return result
    except sqlite3.Error as e:
        return {"error": "query_failed", "message": str(e)}
    finally:
        conn.close()


# --- Tools -----------------------------------------------------------------

@mcp.tool()
def get_athlete_context() -> dict:
    """Return the full athlete context (goals, injuries, constraints, training, gear)."""
    import yaml  # imported here so a missing dep never blocks server start
    if not os.path.isfile(CONTEXT_PATH):
        return {"error": "context_not_found",
                "message": f"No context file at {CONTEXT_PATH}."}
    try:
        with open(CONTEXT_PATH, "r") as f:
            return {"context": yaml.safe_load(f)}
    except yaml.YAMLError as e:
        return {"error": "yaml_parse_error", "message": str(e)}
    except OSError as e:
        return {"error": "context_unreadable", "message": str(e)}


@mcp.tool()
def get_recent_activities(days: int = 14, activity_type: str | None = None) -> dict:
    """Activities from the last N days, newest first. activity_type filters by
    category, so 'running' includes trail/treadmill runs."""
    return _db_call(lambda c: queries.recent_activities(c, days, activity_type))


@mcp.tool()
def get_training_load(weeks: int = 8) -> dict:
    """Weekly (Mon-Sun) training-load aggregates for the last N weeks. The
    current in-progress week is flagged is_partial."""
    return _db_call(lambda c: queries.training_load(c, weeks))


@mcp.tool()
def get_daily_health(days: int = 14) -> dict:
    """Per-day health metrics for the last N days: HRV (status label plus
    values in ms — last-night avg, weekly avg, 5-min high, personal baseline
    bounds), Body Battery, resting HR, steps, sleep, stress. Missing values
    are returned as null."""
    return _db_call(lambda c: queries.daily_health(c, days))


@mcp.tool()
def get_training_metrics(days: int = 28) -> dict:
    """Training readiness, VO2max, and load metrics over the last N days. Columns
    may be null if the watch does not record them."""
    return _db_call(lambda c: queries.training_metrics(c, days))


@mcp.tool()
def get_activity_detail(activity_id: str) -> dict:
    """Full record for a single activity by id."""
    return _db_call(lambda c: queries.activity_detail(c, activity_id))


@mcp.tool()
def get_activity_streams(
    activity_id: str,
    metrics: list[str] | None = None,
    downsample_s: float = 30,
    max_points: int = 300,
) -> dict:
    """Time-series samples for one activity (heart rate, pace/speed, power,
    cadence, elevation, run dynamics, ...), downsampled to keep the response
    small. `metrics` selects columns by name (e.g. ['hr','speed_mps']); omit for
    all. offset_s is the seconds-since-start axis; pace_s_per_km is derived from
    speed. Use this for any per-sample analysis; for HR drift call
    get_activity_analysis instead, which computes it server-side."""
    return _db_call(
        lambda c: queries.activity_streams(c, activity_id, metrics, downsample_s, max_points)
    )


@mcp.tool()
def get_activity_analysis(activity_id: str) -> dict:
    """HR drift / cardiac decoupling for one activity, plus time-in-zone and
    per-lap splits. decoupling_pct compares aerobic efficiency (speed/HR) of the
    first vs second half — positive means HR rose relative to pace (drift); under
    ~5% is well-coupled. Also returns hr_drift_pct (mean-HR rise) and each half's
    avg HR/pace. Requires captured streams; returns computable=false otherwise."""
    return _db_call(lambda c: queries.activity_analysis(c, activity_id))


@mcp.tool()
def search_activities(
    from_date: str | None = None,
    to_date: str | None = None,
    activity_type: str | None = None,
    min_distance_km: float | None = None,
) -> dict:
    """Search activities by ISO date range, category, and/or minimum distance."""
    return _db_call(
        lambda c: queries.search_activities(
            c, from_date, to_date, activity_type, min_distance_km
        )
    )


@mcp.tool()
def trigger_sync() -> dict:
    """Kick off a Garmin data sync (export --update + import) in the background.

    Returns immediately — the sync takes ~15s+, so it runs detached. Re-query any
    tool afterwards and check the freshness fields to see when it lands. This is
    the only tool that writes (indirectly, by spawning the separate sync script)."""
    if not os.path.isfile(SYNC_SCRIPT):
        return {"error": "sync_script_not_found", "message": f"No script at {SYNC_SCRIPT}."}
    try:
        subprocess.Popen(
            ["/bin/bash", SYNC_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach: survives this tool call returning
        )
    except OSError as e:
        return {"error": "sync_launch_failed", "message": str(e)}
    return {
        "status": "started",
        "message": "Sync running in background (~15-60s). Re-query and check "
                   "freshness.last_sync_ts to confirm it completed.",
        "log": os.path.join(os.path.dirname(SYNC_SCRIPT), "sync.log"),
    }


# --- --check CLI -----------------------------------------------------------

REQUIRED_TABLES = ("activities", "daily_health", "training_metrics", "sync_meta")


def run_check() -> int:
    """Validate DB connectivity, tables, row counts, and YAML. Return exit code."""
    ok = True
    print(f"DB path:      {DB_PATH}")
    print(f"Context path: {CONTEXT_PATH}")
    print("-" * 48)

    if not db_exists(DB_PATH):
        print(f"FAIL  database not found at {DB_PATH}")
        ok = False
    else:
        try:
            conn = connect_ro(DB_PATH)
            present = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for t in REQUIRED_TABLES:
                if t in present:
                    n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                    print(f"OK    table {t:<18} {n} rows")
                else:
                    print(f"FAIL  table {t:<18} MISSING")
                    ok = False
            fresh = get_freshness(conn)
            print(f"      latest_data_date={fresh['latest_data_date']} "
                  f"days_since_sync={fresh['days_since_sync']}")
            conn.close()
        except sqlite3.Error as e:
            print(f"FAIL  database unreadable: {e}")
            ok = False

    if not os.path.isfile(CONTEXT_PATH):
        print(f"WARN  context file not found at {CONTEXT_PATH}")
    else:
        try:
            import yaml
            with open(CONTEXT_PATH) as f:
                yaml.safe_load(f)
            print(f"OK    context YAML parses")
        except Exception as e:  # noqa: BLE001 - report any parse/read failure
            print(f"FAIL  context YAML: {e}")
            ok = False

    print("-" * 48)
    print("CHECK PASSED" if ok else "CHECK FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        sys.exit(run_check())
    mcp.run()
