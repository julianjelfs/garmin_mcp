# Garmin Training Assistant — Local MCP Server

Local, read-only MCP server giving Claude Desktop access to your Garmin fitness
data and athlete context. See `spec.md` for the PRD and
`plans/garmin-training-assistant.md` for the phased build plan.

## Components

| File | Role |
|------|------|
| `garmin_mcp_server.py` | Read-only MCP server (stdio). Never crashes on launch. |
| `import_to_sqlite.py`  | Writer. Idempotent upsert importer. **Parse step stubbed pending Phase 0.** |
| `schema.sql`           | SQLite schema (activities, daily_health, training_metrics, sync_meta). |
| `common.py` / `queries.py` | Shared helpers + pure query functions (unit-testable). |
| `seed.py`              | Synthetic data — exercise the server without a real export. |
| `test_smoke.py`        | Smoke tests for the query layer. |

## Status

- **Read side (server + all 7 tools): working.** Verified end-to-end over stdio.
- **Write side (export parser): stubbed.** Blocked on Phase 0 — run the export on
  the real account, map raw fields to schema columns, then implement
  `parse_export()` in `import_to_sqlite.py`.

## Quick start (with synthetic data)

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# Seed a DB so the server has data to serve
./.venv/bin/python seed.py --db ~/.garmin-assistant/garmin.db

# Validate setup (DB, tables, row counts, YAML)
GARMIN_DB_PATH=~/.garmin-assistant/garmin.db \
ATHLETE_CONTEXT_PATH=$PWD/athlete_context.example.yaml \
  ./.venv/bin/python garmin_mcp_server.py --check

# Run the smoke tests
./.venv/bin/python test_smoke.py
```

Then copy `claude_desktop_config.example.json` into your Claude Desktop config
(edit the paths), and restart Claude Desktop.

## Tools

- `get_athlete_context` — goals, injuries, constraints, training, gear (read fresh per call).
- `get_recent_activities(days=14, activity_type?)` — `activity_type` matches a normalized
  category, so `running` includes trail/treadmill runs.
- `get_training_load(weeks=8)` — Mon–Sun weekly aggregates; current week flagged `is_partial`.
- `get_daily_health(days=14)` — HRV, Body Battery, resting HR, sleep, stress, steps.
- `get_training_metrics(days=28)` — readiness/VO2max/load; nullable (Instinct 3 may not record).
- `get_activity_detail(activity_id)` — full flat row.
- `search_activities(from_date?, to_date?, activity_type?, min_distance_km?)`.
- `trigger_sync` — kicks off a background sync (export `--update` + import) and returns
  immediately; re-query and check `freshness.last_sync_ts` to see it land. The only
  tool that writes (indirectly — it spawns the separate `sync.sh`).

Every response carries `freshness` (`latest_data_date`, `days_since_sync`). List
tools cap ~200 rows and return `total_matched` + `truncated`.

## Editing your athlete context

Two files, easy to confuse:

| File | Read by server? | Edit this? |
|------|-----------------|------------|
| `~/.garmin-assistant/athlete_context.yaml` | **YES** (every tool call) | ✅ this one |
| `athlete_context.example.yaml` (in repo) | no — template only | ✗ leave alone |

```bash
open -e ~/.garmin-assistant/athlete_context.yaml   # or: code / vim / nano
```

Changes take effect on the **next query** — the server re-reads the file each
call, no restart. Keep it valid YAML; a parse error comes back as a clear tool
error, not a crash. Both files carry a banner header saying which is which.

## Real-data setup flow (once Phase 0 is done)

1. `pip install garminconnect garth pyyaml mcp`
2. `python garmin_export.py --login` (one-time MFA auth)
3. `python garmin_export.py --all --compact` (initial history)
4. `python import_to_sqlite.py` (parse export into DB)
5. Edit `~/.garmin-assistant/athlete_context.yaml`
6. Register the server in `claude_desktop_config.json`, restart Claude Desktop
7. Schedule daily (Phase 6): `python garmin_export.py --update && python import_to_sqlite.py --update`
