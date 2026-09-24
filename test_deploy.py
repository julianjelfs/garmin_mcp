#!/usr/bin/env python3
"""Tests for running on the Pi: the HTTP transport, the backup script, and sync.sh.

Each test names the invariant it pins.

Runs without pytest:  python test_deploy.py
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile

from starlette.testclient import TestClient

from common import connect_rw
from import_to_sqlite import init_db, upsert_daily_health
from seed import build

HERE = os.path.dirname(os.path.abspath(__file__))
TAILNET_HOST = "pi.tail50bfbf.ts.net"

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


def _seeded_db(tmp: str) -> str:
    db = os.path.join(tmp, "garmin.db")
    conn = connect_rw(db)
    init_db(conn)
    _, health, _ = build(10)
    upsert_daily_health(conn, health)
    conn.commit()
    conn.close()
    return db


def _http_client(base_url: str) -> TestClient:
    import garmin_mcp_server as server
    server.configure_http(port=8020, extra_hosts=TAILNET_HOST)
    # The session manager runs once per instance; each client needs a fresh one.
    server.mcp._session_manager = None
    return TestClient(server.mcp.streamable_http_app(), base_url=base_url)


def _list_tools(client: TestClient):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"accept": "application/json, text/event-stream"},
    )


def test_tailnet_host_lists_tools():
    """Invariant 1: over HTTP, a request carrying the tailnet Host header (as
    `tailscale serve` forwards it) gets the tool list."""
    with _http_client(f"https://{TAILNET_HOST}:8447") as c:
        r = _list_tools(c)
    check("tailnet Host -> 200", r.status_code == 200)
    check("tailnet Host -> tools listed", "get_daily_health" in r.text)


def test_foreign_host_rejected():
    """Invariant 2: over HTTP, a Host header not on the allowlist is refused with
    421, so a DNS-rebinding page can't reach the server."""
    with _http_client("http://evil.example:8020") as c:
        r = _list_tools(c)
    check("foreign Host -> 421", r.status_code == 421)


def test_health_endpoint():
    """Invariant 3: GET /health answers 200 so install-pi.sh can wait on it."""
    with _http_client("http://127.0.0.1:8020") as c:
        r = c.get("/health")
    check("/health -> 200", r.status_code == 200 and r.json().get("ok") is True)


def test_backup_rejects_empty_db():
    """Invariant 4: backup.sh fails, and leaves no file, when the database holds
    no health data, so an empty copy can never rotate a good one away."""
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "garmin.db")
    conn = connect_rw(db)
    init_db(conn)
    conn.close()
    dest = os.path.join(tmp, "backups")
    r = subprocess.run(
        [os.path.join(HERE, "deploy", "backup.sh")],
        env={**os.environ, "GARMIN_DB_PATH": db, "GARMIN_BACKUP_DIR": dest,
             "ATHLETE_CONTEXT_PATH": os.path.join(tmp, "none.yaml")},
        capture_output=True, text=True,
    )
    left = [f for f in os.listdir(dest) if f.endswith(".db")] if os.path.isdir(dest) else []
    check("empty DB -> backup fails", r.returncode != 0)
    check("empty DB -> no backup file left", left == [])


def test_backup_copies_seeded_db():
    """Invariant 5: backup.sh writes a readable copy holding the same health rows."""
    tmp = tempfile.mkdtemp()
    db = _seeded_db(tmp)
    dest = os.path.join(tmp, "backups")
    r = subprocess.run(
        [os.path.join(HERE, "deploy", "backup.sh")],
        env={**os.environ, "GARMIN_DB_PATH": db, "GARMIN_BACKUP_DIR": dest,
             "ATHLETE_CONTEXT_PATH": os.path.join(tmp, "none.yaml")},
        capture_output=True, text=True,
    )
    copies = [f for f in os.listdir(dest) if f.endswith(".db")] if os.path.isdir(dest) else []
    check("seeded DB -> backup succeeds", r.returncode == 0)
    check("seeded DB -> one copy", len(copies) == 1)
    if copies:
        n = sqlite3.connect(os.path.join(dest, copies[0])).execute(
            "select count(*) from daily_health").fetchone()[0]
        check("copy holds the health rows", n == 10)


def test_sync_script_has_no_laptop_paths():
    """Invariant 6: sync.sh finds its checkout relative to itself, so the same file
    runs on the laptop and on the Pi."""
    with open(os.path.join(HERE, "sync.sh")) as f:
        body = f.read()
    check("sync.sh has no /Users/ paths", "/Users/" not in body)


def main() -> int:
    test_tailnet_host_lists_tools()
    test_foreign_host_rejected()
    test_health_endpoint()
    test_backup_rejects_empty_db()
    test_backup_copies_seeded_db()
    test_sync_script_has_no_laptop_paths()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
