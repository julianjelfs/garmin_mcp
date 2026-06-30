# Plan: Garmin Training Assistant — Local MCP Server

> Source PRD: `spec.md`

## Architectural decisions

Durable decisions that apply across all phases:

- **Processes**: two separate processes. `import_to_sqlite.py` (writer) and `garmin_mcp_server.py` (read-only MCP server). They share only the SQLite file.
- **Transport**: stdio MCP server for Claude Desktop.
- **DB access**: SQLite opened read-only (`mode=ro`) by the server. Writer upserts.
- **Schema** (tables): `activities` (PK `activity_id`), `daily_health` (PK `date`), `training_metrics` (PK `date`, all metric cols nullable), `sync_meta` (single row).
- **Idempotency**: importer uses `INSERT OR REPLACE` on natural keys; safe to re-run, absorbs Garmin late-arriving data.
- **Dates**: stored as local `YYYY-MM-DD` TEXT; all lookback / week math done in local time.
- **Category**: importer derives a normalized `category` (run/hike/bike/walk/other) from raw Garmin type; `activity_type` filters match on category so "running" returns all run variants.
- **Common tool conventions**: every tool response carries `latest_data_date` + `days_since_sync`; list tools cap ~200 rows and return `total_matched` + `truncated`; missing/empty DB and YAML parse errors return structured errors, never crash.
- **Testability**: tool logic lives in pure functions (`queries.py`) taking a connection + params and returning plain dicts — unit-testable without the MCP runtime. `garmin_mcp_server.py` is a thin FastMCP wrapper.

---

## Phase 0: Verify foundations (no code — gating)

**User stories**: Open Risks 1 & 2.

### What to build

Nothing yet. Run `garmin_export.py` on the real account; inspect raw output; map actual fields → schema columns. In Garmin Connect, confirm which advanced training-status metrics the Instinct 3 Solar actually records.

### Acceptance criteria

- [ ] Raw export output captured and inspected
- [ ] Field map: each schema column → export source field (or "absent → nullable")
- [ ] Confirmed which of `training_readiness` / `training_load_*` / `recovery_time_h` exist on Instinct 3

---

## Phase 1: Tracer bullet — full pipe, one table, one tool

**User stories**: SC "training load last 4 weeks" (data path); staleness.

### What to build

Thinnest end-to-end spine. Schema (all tables) + `sync_meta`. Idempotent importer DB layer (parse step stubbed pending Phase 0). MCP server starts over stdio, lazy-connects, never crashes. `get_recent_activities` returns real rows in Claude Desktop. Common conventions baked in (freshness, caps/`truncated`, read-only, structured errors). `--check` CLI. Seed script for synthetic test data.

### Acceptance criteria

- [ ] `schema.sql` creates all four tables with correct keys
- [ ] Server starts even when DB missing; tools return structured error
- [ ] `get_recent_activities(days, activity_type)` returns capped rows + freshness + `truncated`
- [ ] `--check` validates DB, tables, row counts, YAML; non-zero exit on failure
- [ ] Seed data lets the whole path be exercised without a real export

---

## Phase 2: Athlete context

**User stories**: SC "contextualise against Panama/Achilles"; "edit YAML reflects immediately".

### What to build

`get_athlete_context` reads the YAML fresh on every call. Parse errors surface as a structured tool response.

### Acceptance criteria

- [ ] Returns parsed YAML as JSON
- [ ] Edit to YAML reflected on next call with no restart
- [ ] Malformed YAML returns a clear error, not a crash

---

## Phase 3: Daily health (fatigue data)

**User stories**: SC "flag fatigue from HRV/Body Battery"; recovery example queries.

### What to build

`daily_health` importer slice + `get_daily_health` — sleep, Body Battery, HRV, resting HR, stress, steps, intensity minutes. NULLs reported explicitly.

### Acceptance criteria

- [ ] `get_daily_health(days)` returns per-day records + freshness
- [ ] NULL metrics surfaced as null, not fabricated
- [ ] Body Battery / HRV / resting HR trend queries answerable

---

## Phase 4: Training load + training metrics

**User stories**: SC "training load 4 weeks"; load×recovery queries.

### What to build

`get_training_load` — Mon–Sun weekly aggregates from activities, `is_partial` flag on current week. `training_metrics` importer slice (all nullable) + `get_training_metrics`.

### Acceptance criteria

- [ ] Weekly buckets Mon–Sun with `week_start` + `is_partial`
- [ ] Aggregates: distance, duration, avg HR, count, long run
- [ ] `get_training_metrics(days)` returns snapshots; nullable cols handled

---

## Phase 5: Activity detail + search

**User stories**: deeper activity querying.

### What to build

`get_activity_detail(activity_id)` returns the flat row. `search_activities` filters by date range / category / min distance, capped.

### Acceptance criteria

- [ ] `get_activity_detail` returns full row or structured not-found
- [ ] `search_activities` honors all optional filters + caps/`truncated`

---

## Phase 6: Scheduling + ops

**User stories**: daily automation; sync fragility mitigation.

### What to build

launchd/cron entry running `garmin_export.py --update && import_to_sqlite.py --update`. End-to-end staleness test (kill sync → freshness warns). Setup docs + `claude_desktop_config.json` example.

### Acceptance criteria

- [ ] Scheduled job documented and runnable
- [ ] Stale data produces a visible `days_since_sync` warning in responses
- [ ] README covers full setup flow
