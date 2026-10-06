"""wsgi.py - Vercel entrypoint for the web function.

Vercel's Python runtime looks, inside each built file, for a WSGI callable
named ``app`` / ``handler`` / ``__handler__``.  ``app.py`` is *not* importable
as an ASGI/WSGI module on its own because:

* it starts a background scraping thread at import time (serverless functions
  must not do that), and
* the local SQLite path may be read-only in the runtime.

So this tiny module prepares a writable database path, imports the Flask app
without touching ``__main__`` and exposes it as ``app``.
"""

from __future__ import annotations

import os


def _prepare_writable_data_dir() -> None:
    """Point DATABASE_PATH at a directory the runtime can actually write to.

    On Vercel the code directory is read-only; only ``/tmp`` is writable, and
    even that is ephemeral (use Turso/libSQL for real persistence).
    """
    if os.environ.get("DATABASE_PATH"):
        return  # explicitly configured - leave it alone

    candidates = ["/tmp", os.getcwd(), os.environ.get("HOME") or "/tmp"]
    for base in candidates:
        try:
            path = os.path.join(base, "data")
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".write-test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
        except OSError:
            continue
        os.environ["DATABASE_PATH"] = os.path.join(path, "mirror.sqlite3")
        return


_prepare_writable_data_dir()

import config  # noqa: E402  (must run after DATABASE_PATH is fixed up)
import db  # noqa: E402
from app import app  # noqa: E402  (the Flask object Vercel serves)

# Make sure the tables exist before the first request is served.
try:
    db.init_db()
except Exception as exc:  # noqa: BLE001 - never break the whole deployment
    print(f"[warn] init_db failed: {type(exc).__name__}: {exc}")

# Static assets live outside the function bundle; serve them through Flask so
# /static/... keeps working with the rewrites in vercel.json.
app.static_folder = config.STATIC_DIR
app.static_url_path = "/static"

__all__ = ["app"]
