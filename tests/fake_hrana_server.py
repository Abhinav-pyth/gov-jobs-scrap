"""Fake Hrana v2 WebSocket server used to test libsql_http without Turso.

It stores statements in an in-memory sqlite3 database, so what gets exercised is
the protocol layer (handshake, batching, typed values, row shapes, errors) -
not SQL semantics.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import websockets

DB = sqlite3.connect(":memory:")


def _to_value(v):
    if v is None:
        return {"null": True}
    if isinstance(v, bool):
        return {"integer": int(v)}
    if isinstance(v, int):
        return {"integer": str(v)}
    if isinstance(v, float):
        return {"float": v}
    if isinstance(v, bytes):
        return {"blob": v.hex()}
    return {"text": str(v)}


def _from_arg(a):
    if "null" in a:
        return None
    if "text" in a:
        return a["text"]
    if "integer" in a:
        try:
            return int(a["integer"])
        except (TypeError, ValueError):
            return a["integer"]
    if "float" in a:
        return a["float"]
    if "blob" in a:
        return bytes.fromhex(a["blob"])
    return None


def run_stmt(stmt):
    sql = stmt.get("sql", "")
    args = [_from_arg(a) for a in stmt.get("args") or []]
    cur = DB.cursor()
    try:
        cur.execute(sql, args)
    except Exception as exc:  # noqa: BLE001
        return {"type": "error", "error": {"message": str(exc), "code": "SQL"}}
    rows = cur.fetchall() if cur.description else []
    res = {
        "cols": [{"name": d[0]} for d in (cur.description or [])],
        "rows": [[_to_value(v) for v in row] for row in rows],
        "last_insert_rowid": _to_value(cur.lastrowid),
        "replicated_row_count": {"value": str(max(cur.rowcount, 0))},
    }
    return {"type": "ok", "result": res}


async def handler(ws):
    async for raw in ws:
        msg = json.loads(raw)
        rid = msg.get("request_id")
        req = msg.get("request") or {}
        if req.get("type") == "hello":
            await ws.send(json.dumps({"type": "hello_ok",
                                      "server": {"version": "0.0.0-test"}}))
            continue
        if req.get("type") == "batch":
            steps = req.get("batch", {}).get("steps") or []
            results = [run_stmt(s.get("stmt", {})) for s in steps]
            failed = any(r["type"] == "error" for r in results)
            payload = {"step_results": results, "results": results, "failed": failed}
            if failed:
                payload["type"] = "error"
                payload["error"] = next(r["error"] for r in results
                                        if r["type"] == "error")
            else:
                payload["type"] = "ok"
        else:
            payload = {"type": "error",
                       "error": {"message": f"unsupported {req.get('type')!r}"}}
        await ws.send(json.dumps({"response": payload, "request_id": rid,
                                  "halted": False}))


async def main():
    async with websockets.serve(handler, "127.0.0.1", 8799):
        print("fake hrana server listening on 8799", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
