"""Shared helpers for the Garmin Training Assistant.

Stdlib-only so it can be imported by both the importer (writer) and the MCP
server (reader), and unit-tested without the mcp runtime.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timezone

DEFAULT_DB_PATH = os.path.expanduser("~/.garmin-assistant/garmin.db")
DEFAULT_CONTEXT_PATH = os.path.expanduser("~/.garmin-assistant/athlete_context.yaml")

# Default cap on rows returned by list tools — protects latency and Claude's
# context budget. Callers see `truncated: true` when this bites.
DEFAULT_ROW_CAP = 200

# Raw Garmin activity type -> coarse category. Matching is done on a normalized
# (lowercased) raw type; anything containing these stems maps to the category.
_CATEGORY_STEMS = (
    ("run", "run"),       # running, trail_running, treadmill_running, track_running
    ("hik", "hike"),
    ("trek", "hike"),
    ("walk", "walk"),
    ("cycl", "bike"),
    ("bik", "bike"),       # biking, mountain_biking, road_biking, indoor_biking
)


def normalize_category(raw_type: str | None) -> str:
    """Map a raw Garmin activity type to a coarse category.

    Used by the importer to populate `activities.category` so that an
    `activity_type='running'` filter catches trail/treadmill/track runs too.
    """
    if not raw_type:
        return "other"
    t = raw_type.strip().lower()
    for stem, category in _CATEGORY_STEMS:
        if stem in t:
            return category
    return "other"


def db_exists(db_path: str) -> bool:
    return os.path.isfile(db_path)


def connect_ro(db_path: str) -> sqlite3.Connection:
    """Open the DB read-only. Raises sqlite3.OperationalError if missing."""
    # mode=ro fails loudly rather than creating an empty DB, which is what we want.
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def connect_rw(db_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the DB read-write for the importer."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_freshness(conn: sqlite3.Connection) -> dict:
    """Return latest_data_date + days_since_sync for embedding in every tool response."""
    out = {"latest_data_date": None, "last_sync_ts": None, "days_since_sync": None}
    try:
        row = conn.execute(
            "SELECT last_sync_ts, latest_data_date FROM sync_meta WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return out
    if not row:
        return out
    out["latest_data_date"] = row["latest_data_date"]
    out["last_sync_ts"] = row["last_sync_ts"]
    if row["latest_data_date"]:
        try:
            d = date.fromisoformat(row["latest_data_date"])
            out["days_since_sync"] = (date.today() - d).days
        except ValueError:
            pass
    return out
