"""SQLite storage layer for the job mirror.

Schema is intentionally close to what a real job portal needs so that the
daily scraper can upsert rows without any ORM overhead.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

import config

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


def upsert_job(conn: sqlite3.Connection, job: dict) -> str:
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


def known_urls(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT source_url FROM jobs")}
