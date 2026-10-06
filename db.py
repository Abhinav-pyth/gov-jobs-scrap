"""SQLite storage layer for the job mirror.

Schema is intentionally close to what a real job portal needs so that the
daily scraper can upsert rows without any ORM overhead.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

import config


# ---------------------------------------------------------------------------
# Backend selection: Turso / libSQL on Vercel, plain SQLite locally.
# Vercel's serverless filesystem is read-only and ephemeral, so the daily
# scrape must persist to a hosted DB. Create a free Turso database and set
#   TURSO_DATABASE_URL = libsql://<name>.turso.io
#   TURSO_AUTH_TOKEN   = <token>
# in the Vercel project settings. Without them the app falls back to the
# local SQLite file (fine for `vercel dev` / local runs).
# ---------------------------------------------------------------------------

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

_TURSO_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY,
    source_url        TEXT UNIQUE NOT NULL,
    slug              TEXT,
    title             TEXT NOT NULL,
    organization      TEXT,
    org_slug          TEXT,
    category          TEXT,
    location          TEXT,
    state             TEXT,
    total_vacancies   TEXT,
    qualification     TEXT,
    age_limit         TEXT,
    salary            TEXT,
    date_posted       TEXT,
    valid_through     TEXT,
    employment_type   TEXT,
    short_description TEXT,
    content_html      TEXT,
    is_new            INTEGER DEFAULT 1,
    first_seen        TEXT NOT NULL,
    last_updated      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    urls_found   INTEGER DEFAULT 0,
    new_jobs     INTEGER DEFAULT 0,
    updated_jobs INTEGER DEFAULT 0,
    errors       INTEGER DEFAULT 0,
    note         TEXT
);
"""


def use_turso() -> bool:
    return bool(TURSO_DATABASE_URL)


class _TursoAdapter:
    """Tiny adapter giving the libSQL client a sqlite3-like interface
    (`execute(...).fetchall()/fetchone()`, `executescript`, `commit`) so the
    rest of the codebase keeps working unchanged."""

    def __init__(self, client):
        self._client = client

    def execute(self, sql, args=()):
        # libsql_http.Connection.execute returns a Result object that already
        # mimics sqlite3.Cursor (fetchall/fetchone/lastrowid/rowcount), so we
        # can pass it straight through.
        return self._client.execute(sql, tuple(args))

    def executescript(self, script):
        self._client.executescript(script)

    def commit(self):
        pass  # libSQL HTTP client commits per statement

    def close(self):
        pass


class _TursoRow:
    """Mimics a sqlite3.Row: indexable by integer and by column name."""

    def __init__(self, values, columns):
        self._values = list(values)
        self._map = dict(zip(columns, self._values))

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else self._map[key]

    def keys(self):
        return list(self._map.keys())

    def __iter__(self):
        return iter(self._values)


class _TursoResult:
    def __init__(self, rows, columns):
        self._rows = [tuple(r.values() if isinstance(r, dict) else r)
                      for r in rows]
        self._columns = columns

    def fetchall(self):
        return [_TursoRow(r, self._columns) for r in self._rows]

    def fetchone(self):
        return self.fetchall()[0] if self._rows else None

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY,          -- id taken from source URL
    source_url        TEXT UNIQUE NOT NULL,
    slug              TEXT,
    title             TEXT NOT NULL,
    organization      TEXT,
    org_slug          TEXT,
    category          TEXT,                         -- e.g. Andhra Pradesh Govt Jobs
    location          TEXT,
    state             TEXT,
    total_vacancies   TEXT,
    qualification     TEXT,
    age_limit         TEXT,
    salary            TEXT,
    date_posted       TEXT,
    valid_through     TEXT,
    employment_type   TEXT,
    short_description TEXT,                         -- plain-text summary
    content_html      TEXT,                         -- sanitized detail HTML
    is_new            INTEGER DEFAULT 1,            -- shown with NEW badge
    first_seen        TEXT NOT NULL,
    last_updated      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_date_posted ON jobs(date_posted DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_category    ON jobs(category);
CREATE INDEX IF NOT EXISTS idx_jobs_is_new      ON jobs(is_new);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    urls_found   INTEGER DEFAULT 0,
    new_jobs     INTEGER DEFAULT 0,
    updated_jobs INTEGER DEFAULT 0,
    errors       INTEGER DEFAULT 0,
    note         TEXT
);
"""


def db_path() -> str:
    return os.path.abspath(config.DATABASE_PATH)


@contextmanager
def get_db():
    if use_turso():
        # NOTE: we deliberately do NOT use the PyPI `libsql` package here -
        # it is embedded-only (no `libsql.client` module), which caused
        # ModuleNotFoundError -> 500 on every page once Turso env vars were
        # set.  `libsql_http` speaks the Hrana v2 WebSocket protocol directly
        # using only the standard library + `websockets`.
        import libsql_http

        conn = libsql_http.connect(TURSO_DATABASE_URL, TURSO_AUTH_TOKEN)
        try:
            yield _TursoAdapter(conn)
        finally:
            conn.close()
        return

    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        if use_turso():
            conn.executescript(_TURSO_SCHEMA)
        else:
            conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------

JOB_FIELDS = [
    "source_url", "slug", "title", "organization", "org_slug", "category",
    "location", "state", "total_vacancies", "qualification", "age_limit",
    "salary", "date_posted", "valid_through", "employment_type",
    "short_description", "content_html", "is_new", "first_seen",
    "last_updated",
]


def upsert_job(conn, job: dict) -> str:
    """Insert or update a job keyed by source_url. Returns 'inserted'|'skipped'."""
    existing = conn.execute(
        "SELECT id FROM jobs WHERE source_url = ?", (job["source_url"],)
    ).fetchone()
    if existing:
        return "skipped"
    cols = list(JOB_FIELDS)
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({placeholders})",
        [job.get(c) for c in cols],
    )
    return "inserted"


def known_urls(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT source_url FROM jobs")}
