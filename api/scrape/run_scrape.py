"""Vercel Serverless Function: api/scrape/run_scrape.py

Invoked daily by the cron schedule declared in vercel.json
(cron = "0 6 * * *" -> 06:00 UTC every day). It scrapes newly published
jobs from the source site and stores them in the Turso/libSQL database
configured through environment variables.

Required environment variables (set in the Vercel dashboard):
    TURSO_DATABASE_URL   e.g. libsql://<db>.turso.io
    TURSO_AUTH_TOKEN     db access token
Optional:
    SCRAPE_SECRET        if set, requests must carry
                         Authorization: Bearer <SCRAPE_SECRET>
                         (vercel.json appends ?key=$SCRAPE_SECRET for cron)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

# Make the project root importable inside the serverless bundle.
sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from flask import jsonify, request, make_response  # noqa: E402

import db  # noqa: E402
from scraper import run_scrape  # noqa: E402


def _authorized() -> bool:
    secret = os.environ.get("SCRAPE_SECRET")
    if not secret:
        return True  # no shared secret configured - rely on Vercel's own auth
    header = request.headers.get("Authorization", "")
    key = request.args.get("key") or header.removeprefix("Bearer ").strip()
    return key == secret


def handler(req=None):
    """Vercel Python function entrypoint (req is a Werkzeug Request).

    Also reachable locally at POST /api/scrape/run_scrape when running
    `python app.py` (see the route registered in app.py), so one
    implementation serves both environments.
    """
    import app as wsgi_app

    path = "/api/scrape/run_scrape"
    headers = {}
    if req is not None:
        try:
            path += "?" + req.query_string.decode()
        except Exception:  # noqa: BLE001
            pass
        auth = req.headers.get("authorization") if hasattr(req.headers, "get") \
            else req.headers.get("Authorization", "")
        if auth:
            headers["Authorization"] = auth

    with wsgi_app.app.test_request_context(path, method="POST", headers=headers):
        if not _authorized():
            return make_response(jsonify({"error": "unauthorized"}), 401)

        db.init_db()
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            result = run_scrape()
        except Exception as exc:  # noqa: BLE001
            return make_response(
                jsonify({"ok": False, "started_at": started,
                         "error": str(exc)}), 500)
        return make_response(
            jsonify({"ok": True, "started_at": started, "result": result}), 200)
