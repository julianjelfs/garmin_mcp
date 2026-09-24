# Garmin Training Assistant MCP server

A read-only MCP server that gives Claude my Garmin data and athlete context. It
runs on the Raspberry Pi and is published to the tailnet, so any Claude client on
a tailnet device can use it whether or not the laptop is on.

```
Claude Code / Claude Desktop (on a tailnet device)
      │  https://pi.tail50bfbf.ts.net:8447/mcp
      ▼
Raspberry Pi ("pi", 192.168.68.69)
  tailscale serve :8447 ──> 127.0.0.1:8020  garmin-mcp.service (streamable HTTP)
  garmin-sync.timer    06:00 and 14:00, sync.sh: export --update, import, backfill
  garmin-backup.timer  04:10, SQLite online backup to ~/garmin-backups, keeps 14
```

`spec.md` has the PRD and `plans/garmin-training-assistant.md` the original build
plan.

## Where things live on the Pi

| Path | What |
|------|------|
| `~/garmin_mcp` | This repo, cloned from GitHub. `.venv` inside it. |
| `~/garmin_mcp/vendor-garmin-export` | The export tool and its cache (`export/`). Not in git; copied across once. It has a local patch to `garmin_export.py`. |
| `~/.garmin-assistant/garmin.db` | The database. Only `sync.sh` writes to it. |
| `~/.garmin-assistant/athlete_context.yaml` | Goals, injuries, gear. The server reads it on every call. |
| `~/.garminconnect/garmin_tokens.json` | Garmin auth tokens, valid about a year. |
| `~/garmin_mcp/sync.log` | Sync output. The server logs to the journal. |

## Connecting Claude

Claude Code, once per machine:

```bash
claude mcp add --scope user --transport http garmin-assistant https://pi.tail50bfbf.ts.net:8447/mcp
```

Claude Desktop's config file only takes stdio servers, so it goes through
`mcp-remote`. See `claude_desktop_config.example.json`.

The claude.ai web and mobile apps can't use it. Their custom connectors call the
server from Anthropic's cloud, which isn't on the tailnet.

## Day to day

`scripts/garmin` drives the Pi over SSH:

```
garmin                 status: server, data freshness, sync, backups, Pi health
garmin context         edit the athlete context on the Pi (validated before it replaces the old one)
garmin sync            run a Garmin sync now
garmin sync-log [n]    show the sync log
garmin logs [n]        follow the server log
garmin deploy          after pushing code: pull on the Pi, reinstall, restart
garmin backup          take a backup now and pull it to ~/Backups/garmin-mcp
```

To change code, commit and push, then `garmin deploy`. The Pi pulls from GitHub,
so it won't see uncommitted work.

If the Garmin tokens expire, the export fails and the sync log says so. Log in
again on the Pi:

```bash
garmin ssh
cd garmin_mcp/vendor-garmin-export && ../.venv/bin/python garmin_export.py --login
```

## Development

The server still speaks stdio by default, so it runs locally against a seeded
database:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python seed.py --db /tmp/garmin.db
GARMIN_DB_PATH=/tmp/garmin.db ./.venv/bin/python garmin_mcp_server.py --check

./.venv/bin/python test_smoke.py    # query layer and importer
./.venv/bin/python test_deploy.py   # HTTP transport, backup script, sync.sh
```

`--http` serves streamable HTTP on `127.0.0.1:$GARMIN_MCP_PORT` (default 8020).
It only accepts loopback Host headers plus whatever is in
`GARMIN_MCP_ALLOWED_HOSTS`. Anything else gets a 421, which keeps DNS rebinding
out. On the Pi that variable holds the tailnet name, because `tailscale serve`
passes the Host header through.

`mcp` is pinned to 1.28.1. Version 2 renamed `FastMCP` and the server won't import.

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

## Setting up from scratch

`deploy/install-pi.sh` is safe to re-run and never touches the data. It installs
the dependencies and systemd units, starts everything, waits on `/health` and adds
the `tailscale serve` rule. The data goes across by hand, once:

```bash
ssh julian_jelfs@pi.local 'git clone https://github.com/julianjelfs/garmin_mcp.git'
sqlite3 ~/.garmin-assistant/garmin.db ".backup '/tmp/garmin.db'"   # never cp a live database
scp /tmp/garmin.db ~/.garmin-assistant/athlete_context.yaml julian_jelfs@pi.local:.garmin-assistant/
scp -p ~/.garminconnect/garmin_tokens.json julian_jelfs@pi.local:.garminconnect/
rsync -az vendor-garmin-export/ julian_jelfs@pi.local:garmin_mcp/vendor-garmin-export/
ssh julian_jelfs@pi.local 'cd garmin_mcp && ./deploy/install-pi.sh'
```

The server moved off the laptop on 2026-09-24. The laptop's launchd agent is
disabled (`~/Library/LaunchAgents/com.julian.garmin-sync.plist.disabled`) and its
last database is at `~/.garmin-assistant/garmin.db.bak-laptop-20260924`.
