-- Garmin Training Assistant — SQLite schema
-- Dates are stored as local 'YYYY-MM-DD' TEXT. All lookback/week math is local time.

CREATE TABLE IF NOT EXISTS activities (
    activity_id       TEXT PRIMARY KEY,   -- Garmin native activity id
    date              TEXT NOT NULL,      -- local YYYY-MM-DD
    start_time        TEXT,               -- local ISO datetime, for detail
    type              TEXT,               -- raw Garmin activity type
    category          TEXT,               -- normalized: run | hike | bike | walk | other
    distance_m        REAL,
    duration_s        REAL,
    avg_hr            INTEGER,
    max_hr            INTEGER,
    avg_pace_s_per_km REAL,
    elevation_gain_m  REAL,
    title             TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_date     ON activities(date);
CREATE INDEX IF NOT EXISTS idx_activities_category ON activities(category);

CREATE TABLE IF NOT EXISTS daily_health (
    date               TEXT PRIMARY KEY,  -- local YYYY-MM-DD
    steps              INTEGER,
    resting_hr         INTEGER,
    hrv_status         TEXT,
    body_battery_high  INTEGER,
    body_battery_low   INTEGER,
    stress_avg         INTEGER,
    sleep_score        INTEGER,
    sleep_duration_s   REAL,
    intensity_minutes  INTEGER
);

-- All metric columns nullable: the Instinct 3 Solar may not compute Garmin's
-- advanced training-status suite. Columns stay NULL when absent.
CREATE TABLE IF NOT EXISTS training_metrics (
    date                TEXT PRIMARY KEY, -- local YYYY-MM-DD
    vo2max              REAL,
    training_readiness  INTEGER,
    training_load_7d    REAL,
    training_load_28d   REAL,
    recovery_time_h     REAL
);

-- Per-activity time-series streams (HR, pace, power, cadence, elevation, run
-- dynamics, ...). Stored columnar: streams_json is a JSON object mapping each
-- metric name to a parallel array indexed by sample, always including offset_s.
-- Source: the garmin-data-export per-activity cache (get_activity_details),
-- which holds full-resolution samples even though the compact text export omits
-- them. This is what makes HR drift / cardiac decoupling computable.
CREATE TABLE IF NOT EXISTS activity_streams (
    activity_id   TEXT PRIMARY KEY,
    n_samples     INTEGER,
    duration_s    REAL,   -- elapsed seconds spanned by the series
    metrics       TEXT,   -- JSON array of metric names present (besides offset_s)
    streams_json  TEXT    -- JSON {metric_name: [v0, v1, ...]} incl. offset_s
);

-- Per-lap splits (one row per lap/split). Small and high-value for pacing.
CREATE TABLE IF NOT EXISTS activity_splits (
    activity_id        TEXT NOT NULL,
    lap_index          INTEGER NOT NULL,
    distance_m         REAL,
    duration_s         REAL,
    moving_duration_s  REAL,
    avg_hr             INTEGER,
    max_hr             INTEGER,
    avg_pace_s_per_km  REAL,
    avg_power_w        REAL,
    avg_run_cadence    REAL,
    elevation_gain_m   REAL,
    elevation_loss_m   REAL,
    PRIMARY KEY (activity_id, lap_index)
);
CREATE INDEX IF NOT EXISTS idx_splits_activity ON activity_splits(activity_id);

-- Time-in-zone for HR and power zones (zone_type = 'hr' | 'power').
CREATE TABLE IF NOT EXISTS activity_zones (
    activity_id   TEXT NOT NULL,
    zone_type     TEXT NOT NULL,
    zone_number   INTEGER NOT NULL,
    secs_in_zone  REAL,
    low_boundary  REAL,
    PRIMARY KEY (activity_id, zone_type, zone_number)
);

-- Per-activity weather snapshot. Units are whatever the Garmin account reports
-- (US accounts give temp in Fahrenheit / wind in mph); stored raw, not converted.
CREATE TABLE IF NOT EXISTS activity_weather (
    activity_id   TEXT PRIMARY KEY,
    temp          REAL,
    apparent_temp REAL,
    dew_point     REAL,
    humidity      INTEGER,
    wind_speed    REAL,
    wind_gust     REAL,
    wind_dir_deg  INTEGER,
    wind_compass  TEXT,
    condition     TEXT,
    station       TEXT
);

-- Single-row freshness table, written by the importer on each successful run.
CREATE TABLE IF NOT EXISTS sync_meta (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    last_sync_ts     TEXT,   -- ISO timestamp of last successful import
    latest_data_date TEXT    -- max date present across data tables
);
