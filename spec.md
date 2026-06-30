# PRD: Garmin Training Assistant — Local MCP Server

## Overview

A locally-running MCP (Model Context Protocol) server that gives Claude Desktop direct access to Garmin fitness data and athlete context. The interface for analysis and coaching is Claude Desktop itself — no separate web app or dashboard required.

---

## Problem

Garmin Connect's developer programme is closed to new applicants and third-party scraping has been blocked. The athlete wants training insights, load monitoring, and coaching recommendations derived from their actual Garmin data, surfaced conversationally via Claude Desktop.

---

## Solution

Three components:

1. **Data sync script** — pulls Garmin data to a local SQLite database on a schedule
2. **Athlete context store** — a YAML file containing structured personal goals, injuries, and key metrics, read by the MCP server
3. **MCP server** — exposes the database and context as tools that Claude Desktop can call during conversation

Claude Desktop becomes the coaching interface. The athlete asks questions in natural language; Claude queries the local data and reasons against it.

---

## Architecture

```
garmin-data-export (Python script)
        │
        ▼ runs on schedule (cron/launchd)
  SQLite database (local)
        │
        ▼
  MCP server (Python, stdio transport)
        │
        ▼
  Claude Desktop ←── athlete chats here
        │
        ▼ also reads
  athlete_context.yaml
```

---

## Component 1: Data Sync

Use the existing open-source tool: https://github.com/sirredbeard/garmin-data-export

- Run `garmin_export.py --all --compact` on first setup to populate history
- Schedule `garmin_export.py --update` to run daily (cron or launchd)
- Parse the export output into SQLite rather than leaving it as flat text files
- **Idempotency:** importer must be safe to re-run. Upsert (`INSERT OR REPLACE`) on natural keys so late-arriving Garmin data (sleep/HRV finalize hours after the day) overwrites earlier rows.
- **Dates:** store local calendar date as `YYYY-MM-DD` TEXT; keep a full local `start_time` on activities for detail. All lookback/week bucketing done in local time.
- Tables required (minimum):
    - `activities` — `activity_id` (TEXT, PRIMARY KEY — Garmin native id), date, start_time, type (raw Garmin type), category (normalized: run/hike/bike/walk/other), distance_m, duration_s, avg_hr, max_hr, avg_pace_s_per_km, elevation_gain_m, title
    - `daily_health` — date (PRIMARY KEY), steps, resting_hr, hrv_status, body_battery_high, body_battery_low, stress_avg, sleep_score, sleep_duration_s, intensity_minutes
    - `training_metrics` — date (PRIMARY KEY), vo2max, training_readiness, training_load_7d, training_load_28d, recovery_time_h. **All nullable** — the Instinct 3 Solar may not compute Garmin's advanced training-status suite; treat these as bonus-if-present (see Open Risks).
    - `sync_meta` — single-row table: `last_sync_ts`, `latest_data_date`. Written by the importer on each successful run; read by every tool for freshness reporting.

> `category` is derived by the importer from the raw Garmin type so that a filter for `running` returns `running` + `trail_running` + `treadmill_running` etc. Raw type is preserved for detail.

> `avg_pace_s_per_km` is taken from the export if present, otherwise derived from distance/duration by the importer.

---

## Component 2: Athlete Context

A human-editable YAML file at a configurable path (default: `~/.garmin-assistant/athlete_context.yaml`).

### Schema

```yaml
athlete:
    name: Julian
    sustainable_pace_s_per_km: 420 # 7:00/km
    ftp_running_hr: null # populate when known

goals:
    - id: panama_2028
      label: "Rat Race Panama Coast to Coast"
      date: "2028-03-01" # approximate
      description: "~125 mile jungle trek and packraft crossing. Multi-day expedition."
      priority: primary

injuries:
    - id: achilles_right
      label: "Insertional Achilles tendinopathy (right)"
      status: active # active | resolved | monitoring
      notes: "Strengthening with calf raises. Managed with run/walk intervals."
      onset: "2025-01"
    - id: achilles_left
      label: "Mid-portion Achilles tendinopathy (left)"
      status: active
      notes: "Lower grade than right."
      onset: "2025-01"

constraints:
    - "Fused right big toe — footwear fit is a consideration"
    - "Arthritic left big toe"

training:
    current_approach: "5 min run / 1 min walk intervals"
    weekly_volume_target_km: 50
    long_run_target_km: 30
    notes: "Building base for Panama. Conservative approach given bilateral Achilles."

gear:
    watch: "Garmin Instinct 3 Solar 45mm"
    poles: "TheFitLife carbon fibre trekking poles"
```

The MCP server reads this file on each tool call so edits take effect immediately without restarting.

---

## Component 3: MCP Server

### Transport

stdio (standard for local Claude Desktop MCP integration)

### Configuration

Registered in `claude_desktop_config.json`:

```json
{
    "mcpServers": {
        "garmin-assistant": {
            "command": "python",
            "args": ["/path/to/garmin_mcp_server.py"],
            "env": {
                "GARMIN_DB_PATH": "/path/to/garmin.db",
                "ATHLETE_CONTEXT_PATH": "/path/to/athlete_context.yaml"
            }
        }
    }
}
```

### Tools (MCP endpoints)

#### Common conventions (all tools)

- **Freshness:** every response includes `latest_data_date` and `days_since_sync` (from `sync_meta`) so Claude can warn when coaching on stale data.
- **Result caps:** list-returning tools cap at ~200 rows (date desc) and return `{ rows, returned_count, total_matched, truncated }`. A `true` `truncated` tells Claude to narrow the query.
- **`activity_type` filter** matches on normalized `category`, so `running` returns all run variants. Raw type is still present per row.
- **Errors:** missing/empty/unreadable DB or YAML parse failure returns a structured error in the tool response — never a crash. The DB is opened read-only (`mode=ro`).

#### `get_athlete_context`

Returns the full contents of `athlete_context.yaml` as structured data.

No parameters.

Returns: parsed YAML as JSON.

---

#### `get_recent_activities`

Returns activities from the last N days.

Parameters:

- `days` (int, default 14) — lookback window
- `activity_type` (string, optional) — filter e.g. "running", "hiking"

Returns: list of activity records ordered by date descending.

---

#### `get_training_load`

Returns weekly training load summary for the last N weeks.

Parameters:

- `weeks` (int, default 8)

Weeks are **Mon–Sun (ISO)**. The in-progress current week is flagged `is_partial: true` so a mid-week query doesn't read as a volume crash. Each row carries `week_start` (date).

Returns: per-week aggregates — `week_start`, `is_partial`, total distance, total duration, avg HR, number of activities, long run distance. This computed volume load is the primary fatigue input when Garmin's `training_load_*` columns are NULL.

---

#### `get_daily_health`

Returns daily health metrics for the last N days.

Parameters:

- `days` (int, default 14)

Returns: per-day records — HRV, body battery, resting HR, steps, sleep score, stress.

---

#### `get_training_metrics`

Returns training readiness, VO2 max trend, and load metrics.

Parameters:

- `days` (int, default 28)

Returns: chronological list of training metric snapshots.

---

#### `get_activity_detail`

Returns full detail for a single activity.

Parameters:

- `activity_id` (string, required)

Returns: the full flat `activities` row for that id. (Splits are out of scope for v1 — no splits table exists. Add later if a coaching question needs per-km data.)

---

#### `search_activities`

Search activities by type, date range, or distance threshold.

Parameters:

- `from_date` (string, optional, ISO format)
- `to_date` (string, optional, ISO format)
- `activity_type` (string, optional)
- `min_distance_km` (float, optional)

Returns: matching activity list.

---

## Out of Scope (v1)

- Web UI or dashboard
- Push notifications or proactive alerts
- Automatic context updates from conversation (athlete edits YAML manually)
- Multi-user support
- Cloud sync or remote access
- Strava integration (may be added later if Garmin sync proves unreliable)

---

## Non-Functional Requirements

- All data stays local — nothing leaves the machine
- MCP server must start in under 2 seconds (Claude Desktop times out on slow starts)
- Tool responses should return within 500ms for typical queries
- SQLite database should handle at least 5 years of activity history without performance issues
- YAML parse errors should surface a clear error message via the tool response, not a crash
- **Server always starts** (lazy DB connect). It must never crash on launch, or Claude Desktop hides all its tools. Missing/empty/unreadable DB surfaces as a structured per-tool error instead.
- `--check` is a separate CLI path (not the server start): validates DB connectivity, presence of required tables, non-zero row counts, and YAML parse; prints a human-readable report and exits non-zero on failure.

---

## Implementation Notes

- Python preferred (consistent with garmin-data-export toolchain)
- Use `mcp` Python SDK (`pip install mcp`) for the server
- Use `sqlite3` (stdlib) for database access
- Use `pyyaml` for context file parsing
- Keep the sync script and MCP server as separate processes — the MCP server is read-only
- Add a simple `--check` flag to the MCP server for verifying DB connection and context file on startup

---

## Setup Flow (for reference)

1. `pip install garminconnect garth pyyaml mcp`
2. `python garmin_export.py --login` (one-time auth)
3. `python garmin_export.py --all --compact` (initial history pull)
4. `python import_to_sqlite.py` (parse export into DB)
5. Edit `athlete_context.yaml` with personal details
6. Register MCP server in `claude_desktop_config.json`
7. Restart Claude Desktop
8. Schedule daily: `python garmin_export.py --update && python import_to_sqlite.py --update`

---

## Phase 0 findings (VERIFIED against a real Instinct 3 Solar export, 2026-06-28)

1. **Export field coverage — RESOLVED.** `garmin-data-export` writes a titled
   plaintext file; in `--compact` mode each section is one line of JSON. Mapping
   implemented in `import_to_sqlite.py::parse_export`:
   - activities ← `Activities[].summary` (activityId, startTimeLocal, activityType.typeKey,
     distance, duration, averageHR, maxHR, elevationGain, activityName, vO2MaxValue)
   - daily_health ← `Daily Health[date]` nested API blocks; Body Battery high/low
     derived from `bodyBatteryValuesArray`; pace derived from distance/duration.
   - training_metrics ← `Training Metrics.training_readiness[0]` (score, recoveryTime min→h).
2. **Instinct 3 Solar capability — RESOLVED.** Present: `training_readiness`,
   `recovery_time_h`, `vo2max` (from activities). **Absent (stay NULL):**
   `training_load_7d/28d` (`training_status` block is empty), `hrv_status` (not in
   daily health). Sleep is **sparse** (only ~last 2 days populated). The
   nullable + derived-fallback design holds: fatigue detection leans on Body
   Battery, resting HR, stress, sleep-when-present, and computed weekly volume load.
3. **Library pin — RESOLVED.** `garth 0.8.0` is a deprecation stub that breaks the
   tool (`garmin.garth` removed); pinned `garminconnect==0.3.6` + `garth==0.7.11`
   and patched the tool's token-dump call to `.client`.
4. **Sync fragility — mitigated, ongoing.** Daily launchd job runs
   `garmin_export.py --update && import_to_sqlite.py --update`. Garmin auth tokens
   cached ~1 year; `--login` re-auth is a manual once-a-year step. Silent breakage
   is surfaced by the `sync_meta` freshness fields in every tool response.

---

## Success Criteria

- Claude Desktop can answer "how has my training load looked over the last 4 weeks" with accurate data
- Claude Desktop can flag if HRV or body battery trends suggest accumulated fatigue
- Claude Desktop can contextualise observations against Panama 2028 goals and current Achilles status
- Athlete can update `athlete_context.yaml` and the next query reflects the change immediately

### Example coaching queries (these must work end-to-end)

Recovery / readiness signals (driven by `daily_health`, the primary fatigue path on Instinct 3):

- "Is my Body Battery trending down across this week?" → `get_daily_health` reads `body_battery_high/low` per day, Claude reports the trend.
- "Has my resting HR crept up over the last 2 weeks?" → `resting_hr` series.
- "What's my HRV status been doing lately?" → `hrv_status` series.
- "Did my sleep score and sleep duration drop on the nights after hard runs?" → join `sleep_score`/`sleep_duration_s` against `activities` dates.

Load × recovery correlation (the core coaching value):

- "Has my sleep debt tracked with my hardest training days this week?" → `get_training_load` (weekly volume, `is_partial`-aware) cross-referenced with `daily_health` sleep + Body Battery.
- "Given my weekly volume is climbing toward the 50km target, are my recovery markers keeping up?" → combines `get_training_load`, `daily_health`, and `athlete_context` (volume target, bilateral Achilles).

Each must degrade gracefully: if a metric is NULL (watch didn't record / didn't sync), the tool returns the gap explicitly rather than implying a value, and the freshness fields warn when the data is stale.
