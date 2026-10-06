"""End-to-end smoke test: run the fake Hrana server in-process, point db.py at
it via TURSO_DATABASE_URL, and exercise init_db + insert + select through the
Turso code path (libsql_http + _TursoAdapter)."""
import asyncio
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import websockets

import fake_hrana_server


async def _serve(stop_event):
    async with websockets.serve(fake_hrana_server.handler, "127.0.0.1", 8799):
        stop_event.set()
        await asyncio.sleep(30)


def main():
    stop = threading.Event()
    t = threading.Thread(target=lambda: asyncio.run(_serve(stop)), daemon=True)
    t.start()
    stop.wait(5)

    os.environ["TURSO_DATABASE_URL"] = "ws://127.0.0.1:8799"
    os.environ["TURSO_AUTH_TOKEN"] = "test-token"

    import db
    db.TURSO_DATABASE_URL = os.environ["TURSO_DATABASE_URL"]
    db.TURSO_AUTH_TOKEN = os.environ["TURSO_AUTH_TOKEN"]
    assert db.use_turso(), "turso mode not active"

    db.init_db()
    print("init_db OK")

    with db.get_db() as conn:
        r = conn.execute(
            "INSERT INTO jobs (source_url, title, first_seen, last_updated)"
            " VALUES (?, ?, ?, ?)",
            ("https://x/1", "Test Job", "2026-10-06", "2026-10-06"),
        )
        print("insert OK, lastrowid =", r.lastrowid)

    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM jobs").fetchall()
        assert rows, "no rows returned"
        d = dict(rows[0])
        assert d["title"] == "Test Job", d
        # sqlite3.Row-style access must also work
        assert rows[0]["title"] == "Test Job"
        assert rows[0][3] == "Test Job"
        print("select OK:", d["title"])

    # app.py's q() helper does [dict(r) for r in ...] - replicate that
    with db.get_db() as conn:
        out = [dict(r) for r in conn.execute("SELECT id, title FROM jobs")]
        assert out == [{"id": 1, "title": "Test Job"}], out
        print("dict conversion OK:", out)

    print("PASS: full Turso flow works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
