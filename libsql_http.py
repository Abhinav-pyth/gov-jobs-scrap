"""libsql_http.py - minimal Turso / libSQL client over the Hrana WebSocket API.

Why this exists
---------------
The project used to depend on the PyPI package ``libsql``, but that package is
**embedded-only**: it has no ``libsql.client`` module, so connecting to a hosted
Turso database raised::

    ModuleNotFoundError: No module named 'libsql.client'

which made every page of the deployed app return 500 on Vercel.

There is no reliable pure-Python pip package for the Turso HTTP/WS protocol, so
this module implements just enough of the Hrana v2 WebSocket protocol (the same
one the official Rust/JS clients use) with only the Python standard library plus
``websockets``.

The exposed objects mimic the small slice of the ``sqlite3`` API the rest of the
codebase relies on: ``execute()``, ``executescript()``, ``commit()``,
``close()``, and results with ``fetchall()`` / ``fetchone()`` returning rows that
support both ``row[0]`` and ``row["col"]`` / ``dict(row)``.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any

try:  # third-party, listed in requirements.txt
    from websockets.sync.client import connect as _ws_connect  # type: ignore
except Exception:  # pragma: no cover - fall back gracefully
    _ws_connect = None


class LibsqlError(RuntimeError):
    """Raised when Turso reports an error or the connection cannot be made."""


# ---------------------------------------------------------------------------
# Result / row shims (sqlite3-compatible-ish)
# ---------------------------------------------------------------------------


class Row:
    """Mimics ``sqlite3.Row``: indexable by position *and* by column name."""

    __slots__ = ("_values", "_columns")

    def __init__(self, values, columns):
        self._values = tuple(values)
        self._columns = tuple(columns)

    def _key(self, key):
        if isinstance(key, int):
            if key < 0:
                key += len(self._values)
            if not 0 <= key < len(self._values):
                raise IndexError(key)
            return key
        if isinstance(key, str):
            try:
                return self._columns.index(key)
            except ValueError:
                raise IndexError(f"no such column: {key!r}") from None
        raise TypeError(f"invalid index type: {type(key).__name__}")

    def __getitem__(self, key):
        return self._values[self._key(key)]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        if isinstance(other, Row):
            return self._values == other._values
        return NotImplemented

    def keys(self):
        return list(self._columns)

    def values(self):
        return list(self._values)

    def items(self):
        return list(zip(self._columns, self._values))

    def get(self, key, default=None):
        try:
            return self[key]
        except (IndexError, TypeError):
            return default

    def __repr__(self):
        return repr(dict(self.items()))


def _as_dict(row: Any) -> dict:
    """Hrana returns rows either as ``{"cols": [...], "rows": [[...]]}`` or as
    lists of ``{"columntype": value}`` dicts. Normalise both shapes."""
    if isinstance(row, dict) and "values" in row:  # older hrana shape
        return {v["column"]: v["value"] for v in row["values"]}
    if isinstance(row, dict):
        return row
    return {}


class Result:
    """Mimics a ``sqlite3.Cursor`` result object."""

    def __init__(self, columns: list[str], rows: list[list[Any]],
                 last_insert_rowid: Any = None, affected: int = 0):
        self.columns = columns
        self.rows = rows
        self.lastrowid = last_insert_rowid
        self.rowcount = affected

    @property
    def description(self):
        return [(c, None, None, None, None, None, None) for c in self.columns]

    def fetchall(self) -> list[Row]:
        return [Row(r, self.columns) for r in self.rows]

    def fetchone(self):
        return self.fetchall()[0] if self.rows else None

    def fetchmany(self, size=1):
        return self.fetchall()[:size]

    def __iter__(self):
        return iter(self.fetchall())


# ---------------------------------------------------------------------------
# Value conversion (Python <-> Hrana typed values)
# ---------------------------------------------------------------------------


def _to_hrana(value: Any) -> dict:
    if value is None:
        return {"null": None}
    if isinstance(value, bool):
        return {"integer": 1 if value else 0}
    if isinstance(value, int):
        return {"integer": value}
    if isinstance(value, float):
        return {"float": value}
    if isinstance(value, (bytes, bytearray)):
        return {"blob": bytes(value).hex()}
    return {"text": str(value)}


def _from_hrana(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        return value
    if "null" in value:
        return None
    if "text" in value:
        return value["text"]
    if "blob" in value:
        try:
            return bytes.fromhex(value["blob"] or "")
        except (ValueError, TypeError):
            return value["blob"]
    if "float" in value:
        return value["float"]
    if "integer" in value:
        try:
            return int(value["integer"])
        except (TypeError, ValueError):
            return value["integer"]
    return None


def _normalise_args(args: Any) -> list:
    if args is None:
        return []
    if isinstance(args, dict):
        return [args[k] for k in sorted(args)]
    if isinstance(args, (list, tuple)):
        return list(args)
    return [args]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class Connection:
    """A very small sqlite3-like connection talking to a Turso database."""

    def __init__(self, url: str, auth_token: str = "", timeout: float = 30.0):
        if _ws_connect is None:
            raise LibsqlError(
                "the 'websockets' package is required to talk to Turso "
                "(add it to requirements.txt)"
            )
        self.url = url
        self.auth_token = auth_token or ""
        self.timeout = timeout
        self._closed = False
        self._conn = None
        self._stream_id = ""
        self._lock = threading.RLock()
        self._hello: dict | None = None
        self._cm = None
        self._pending: dict[str, dict] = {}
        self._reader = None
        self._connect()

    # -- plumbing ----------------------------------------------------------
    def _ws_url(self) -> str:
        url = self.url.strip()
        if url.startswith("libsql://"):
            url = "wss://" + url[len("libsql://"):]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://"):]
        elif url.startswith("https://"):
            url = "wss://" + url[len("https://"):]
        elif not url.startswith(("ws://", "wss://")):
            url = "wss://" + url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}" + ("authToken=%s" % self.auth_token if self.auth_token else "")

    def _connect(self) -> None:
        last_error: Exception | None = None
        for attempt in range(2):  # one retry: serverless sockets can go stale
            try:
                try:  # websockets >= 14 returns a context-manager factory
                    conn = _ws_connect(
                        self._ws_url(), open_timeout=self.timeout,
                        close_timeout=5, max_size=None, ping_interval=None)
                    if not hasattr(conn, "send"):
                        self._cm = conn.__enter__()
                        conn = self._cm
                except TypeError:  # older signature without these kwargs
                    conn = _ws_connect(self._ws_url())
                self._conn = conn
                self._stream_id = str(uuid.uuid4())
                self._pending = {}
                self._hello = {"event": threading.Event()}
                self._reader = threading.Thread(
                    target=self._read_loop, args=(conn,), daemon=True,
                    name="hrana-reader")
                self._reader.start()
                # Hrana v2 handshake: {"type":"hello","jwt":...} -> hello_ok
                self._conn.send(json.dumps(
                    {"type": "hello",
                     **({"jwt": self.auth_token} if self.auth_token else {})}))
                if not self._hello["event"].wait(self.timeout):
                    raise LibsqlError("timed out waiting for the hrana hello")
                hello = self._hello.get("response") or {}
                if hello.get("type") != "hello_ok":
                    raise LibsqlError(
                        f"hrana handshake failed: {hello or self._hello.get('error')}")
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._teardown()
                if attempt == 1:
                    break
        raise LibsqlError(
            f"could not connect to {self.url}: {type(last_error).__name__}: {last_error}")

    def _teardown(self) -> None:
        conn, self._conn = self._conn, None
        cm, self._cm = getattr(self, "_cm", None), None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    def _read_loop(self, conn) -> None:
        """The single reader for this connection's socket.

        ``websockets`` forbids concurrent ``recv()`` calls, so *only* this
        thread ever reads (and only for its own socket: a reconnect starts a
        fresh thread bound to the new socket and stops this one).  Callers wait
        on their own ``threading.Event``.
        """
        while True:
            if conn is not self._conn:
                return  # superseded by a reconnect
            try:
                raw = conn.recv()
            except Exception:  # socket closed / broken
                for fut in list(self._pending.values()):
                    fut["error"] = LibsqlError("connection to Turso was lost")
                    fut["event"].set()
                if getattr(self, "_hello", None) is not None \
                        and not self._hello["event"].is_set():
                    self._hello["error"] = LibsqlError(
                        "connection closed during the hrana handshake")
                    self._hello["event"].set()
                return
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            mtype = msg.get("type")
            if mtype == "hello_ok":
                if self._hello is not None:
                    self._hello["response"] = msg
                    self._hello["event"].set()
                continue
            if mtype is not None:  # error / no_more_streams / keepalive_error
                err = msg.get("error") or {}
                message = err.get("message") or f"server sent {mtype}"
                if self._hello is not None and not self._hello["event"].is_set():
                    self._hello["error"] = LibsqlError(message)
                    self._hello["event"].set()
                    continue
                for fut in list(self._pending.values()):
                    fut["error"] = LibsqlError(message)
                    fut["event"].set()
                continue
            rid = msg.get("request_id")
            fut = self._pending.get(rid)
            if fut is not None:
                if msg.get("error"):
                    fut["error"] = LibsqlError(
                        (msg["error"] or {}).get("message") or "stream error")
                else:
                    fut["response"] = msg.get("response") or {}
                fut["event"].set()

    def _send(self, stream_request: dict) -> dict:
        """Send one stream request and wait for its response.

        The whole send/await cycle is serialised with ``self._lock``: the
        serverless code paths are single threaded per invocation, but Flask can
        still serve overlapping requests inside one warm instance.
        """
        with self._lock:
            return self._send_locked(stream_request)

    def _send_locked(self, stream_request: dict) -> dict:
        request_id = uuid.uuid4().hex
        event = threading.Event()
        entry = {"event": event, "response": None, "error": None}
        self._pending[request_id] = entry
        frame = {"stream_id": self._stream_id, "request_id": request_id,
                 "request": stream_request}
        try:
            self._conn.send(json.dumps(frame))
        except Exception:  # stale socket: reconnect once and resend
            self._pending.pop(request_id, None)
            self._teardown()
            self._connect()
            request_id = uuid.uuid4().hex
            event = threading.Event()
            entry = {"event": event, "response": None, "error": None}
            self._pending[request_id] = entry
            frame = {"stream_id": self._stream_id, "request_id": request_id,
                     "request": stream_request}
            self._conn.send(json.dumps(frame))
        if not event.wait(self.timeout):
            self._pending.pop(request_id, None)
            raise LibsqlError("timed out waiting for a response from Turso")
        self._pending.pop(request_id, None)
        if entry["error"]:
            raise entry["error"]
        return entry["response"] or {}

    # -- statements --------------------------------------------------------
    def _run(self, stmt: dict) -> dict:
        resp = self._send({"type": "batch",
                           "batch": {"steps": [stmt], "no_wait": False}})
        if resp.get("type") != "ok":
            err = resp.get("error") or {}
            raise LibsqlError(err.get("message") or f"batch failed: {resp}")
        results = resp.get("step_results") or resp.get("results") or []
        if not results:
            return {}
        first = results[0] or {}
        if first.get("type") == "error":
            err = first.get("error") or {}
            raise LibsqlError(err.get("message") or "statement failed")
        return first.get("result") or {}

    def execute(self, sql: str, args: Any = ()) -> Result:
        sql = (sql or "").strip().rstrip(";").strip()
        if not sql:
            raise LibsqlError("empty statement")
        stmt = {
            "type": "stmt",
            "stmt": {
                "sql": sql,
                "args": [_to_hrana(a) for a in _normalise_args(args)],
                "want_row_result": True,
            },
        }
        res = self._run(stmt)
        result = _build_result(res)
        if (not result.rows and "returning" not in sql.lower()
                and sql.lower().startswith(("insert", "replace"))):
            # No RETURNING clause: ask the server for the rowid it assigned.
            rid = self._run({"type": "stmt",
                             "stmt": {"sql": "SELECT last_insert_rowid() AS id",
                                      "args": []}})
            rows = rid.get("rows") or []
            if rows and rows[0]:
                result.lastrowid = _from_hrana(rows[0][0])
        return result

    def executescript(self, script: str) -> "Connection":
        statements = [s.strip() for s in _split_sql(script) if s.strip()]
        steps = [{"type": "stmt", "stmt": {"sql": s, "args": []}}
                 for s in statements]
        if not steps:
            return self
        resp = self._send({"type": "batch",
                           "batch": {"steps": steps, "no_wait": False}})
        if resp.get("type") != "ok":
            err = resp.get("error") or {}
            raise LibsqlError(err.get("message") or f"batch failed: {resp}")
        for item in resp.get("step_results") or resp.get("results") or []:
            if item and item.get("type") == "error":
                err = item.get("error") or {}
                raise LibsqlError(err.get("message") or "statement failed")
        return self

    def commit(self) -> None:
        return None  # every statement runs with want_autocommit

    def rollback(self) -> None:
        return None

    def cursor(self) -> "Cursor":
        return Cursor(self)

    def batch(self, statements: list[tuple[str, list]]) -> list[Result]:
        """Run several statements in one Hrana batch (atomic, 1 round trip)."""
        steps = [{"type": "stmt",
                  "stmt": {"sql": sql.strip().rstrip(";").strip(),
                           "args": [_to_hrana(a) for a in _normalise_args(args)]}}
                 for sql, args in statements]
        if not steps:
            return []
        with self._lock:
            resp = self._send_locked({"type": "batch",
                                      "batch": {"steps": steps,
                                                "no_wait": False}})
        if resp.get("type") != "ok":
            err = resp.get("error") or {}
            raise LibsqlError(err.get("message") or f"batch failed: {resp}")
        out: list[Result] = []
        for item in resp.get("step_results") or resp.get("results") or []:
            if item and item.get("type") == "error":
                err = item.get("error") or {}
                raise LibsqlError(err.get("message") or "statement failed")
            out.append(_build_result((item or {}).get("result") or {}))
        return out

    def close(self) -> None:
        self._closed = True
        self._teardown()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Cursor:
    """Thin wrapper so ``conn.cursor().execute(...).fetchall()`` also works."""

    def __init__(self, conn: Connection):
        self._conn = conn
        self._result = Result([], [])

    def execute(self, sql, args=()):
        self._result = self._conn.execute(sql, args)
        return self

    def executemany(self, sql, seq):
        for args in seq:
            self._conn.execute(sql, args)
        return self

    def executescript(self, script):
        self._conn.executescript(script)
        return self

    def fetchall(self):
        return self._result.fetchall()

    def fetchone(self):
        return self._result.fetchone()

    @property
    def description(self):
        return self._result.description

    @property
    def lastrowid(self):
        return self._result.lastrowid

    @property
    def rowcount(self):
        return self._result.rowcount

    def close(self):
        return None


def _split_sql(script: str) -> list[str]:
    """Split a SQL script on ``;`` while ignoring semicolons inside strings,
    comments and quoted identifiers."""
    parts, buf, i = [], [], 0
    quote = None
    text = script or ""
    n = len(text)
    while i < n:
        ch = text[i]
        two = text[i:i + 2]
        if quote:
            buf.append(ch)
            if ch == quote:
                if two == quote * 2:  # escaped quote
                    buf.append(text[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if two == "--":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if two == "/*":
            j = text.find("*/", i)
            i = n if j == -1 else j + 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts



def _build_result(res: dict) -> Result:
    """Turn a Hrana ``StmtResult`` into our sqlite3-like ``Result``."""
    cols: list[str] = []
    rows: list[list[Any]] = []
    row_result = res.get("row_result")
    if isinstance(row_result, dict) and ("cols" in row_result or "rows" in row_result):
        cols = [c["name"] if isinstance(c, dict) else str(c)
                for c in row_result.get("cols") or []]
        for raw in row_result.get("rows") or []:
            if isinstance(raw, dict) and "values" in raw:
                rows.append([_from_hrana(v) for v in raw["values"]])
            elif isinstance(raw, dict):
                if not cols:
                    cols = list(raw.keys())
                rows.append([_from_hrana(raw.get(c)) for c in cols])
            else:
                rows.append([_from_hrana(v) for v in raw])
    elif isinstance(row_result, list):  # legacy shape: list of column dicts
        for item in row_result:
            data = _as_dict(item)
            if not cols:
                cols = list(data.keys())
            rows.append([_from_hrana(data.get(c)) for c in cols])
    else:
        # some servers answer with top-level cols/rows
        cols = [c["name"] if isinstance(c, dict) else str(c)
                for c in res.get("cols") or []]
        for raw in res.get("rows") or []:
            if isinstance(raw, dict) and "values" in raw:
                rows.append([_from_hrana(v) for v in raw["values"]])
            else:
                rows.append([_from_hrana(v) for v in raw])
    affected = 0
    for key in ("replicated_row_count", "affected_row_count"):
        val = res.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        try:
            affected = int(val or 0)
            if affected:
                break
        except (TypeError, ValueError):
            affected = 0
    return Result(cols, rows, _from_hrana(res.get("last_insert_rowid")), affected)


def connect(url: str, auth_token: str = "", timeout: float = 30.0) -> Connection:
    return Connection(url, auth_token, timeout)


def is_turso_url(url: str) -> bool:
    return bool(re.match(r"^(libsql|http|https|ws|wss)://", (url or "").strip()))
