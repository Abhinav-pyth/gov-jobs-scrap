"""app.py - the web application (a working copy of a sarkari-naukri style portal).

Features
--------
* Homepage listing latest scraped jobs with NEW badges & quick-facts.
* Job detail pages rendering the scraped, sanitized content.
* Category / state / search / pagination support.
* Admit-card / result placeholder sections like the original layout.
* Built-in daily scheduler that runs scraper.run_scrape() once every day
  (default 06:00 local time) so new content is added automatically.
* /admin/scrape endpoint + CLI to trigger scraping manually.

Run:  python app.py            (serves on http://localhost:8000)
      python app.py --scrape   (run one scrape immediately and exit)
"""

from __future__ import annotations

import argparse
import threading
import time
from datetime import datetime, timedelta

from flask import (Flask, abort, jsonify, render_template, request,
                   redirect, url_for)

import config
import db
from scraper import run_scrape

app = Flask(__name__, static_folder=config.STATIC_DIR)
app.config["TEMPLATES_AUTO_RELOAD"] = True
PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def q(sql, args=()):
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def count(sql, args=()) -> int:
    with db.get_db() as conn:
        return conn.execute(sql, args).fetchone()[0]


def list_jobs(where="1=1", args=(), limit=PAGE_SIZE, offset=0):
    sql = (f"SELECT * FROM jobs WHERE {where} "
           f"ORDER BY COALESCE(date_posted,'') DESC, id DESC "
           f"LIMIT ? OFFSET ?")
    return q(sql, tuple(args) + (limit, offset))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    page = max(1, request.args.get("page", 1, int))
    total = count("SELECT COUNT(*) FROM jobs")
    jobs = list_jobs(limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    latest = list_jobs(limit=12)
    top_new = list_jobs("is_new=1", limit=8)
    categories = q("SELECT category, COUNT(*) n FROM jobs "
                   "WHERE category IS NOT NULL GROUP BY category "
                   "ORDER BY n DESC LIMIT 12")
    last_run = q("SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1")
    return render_template("home.html", jobs=jobs, latest=latest,
                           top_new=top_new, categories=categories,
                           page=page, pages=max(1, -(-total // PAGE_SIZE)),
                           total=total, last_run=last_run[0] if last_run else None,
                           site_name=config.SITE_NAME,
                           tagline=config.SITE_TAGLINE, now=datetime.now())


@app.route("/job/<int:job_id>")
def job_detail(job_id):
    rows = q("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not rows:
        abort(404)
    job = rows[0]
    related = list_jobs("id != ? AND (category = ? OR organization = ?)",
                        (job_id, job["category"], job["organization"]), limit=6)
    return render_template("job.html", job=job, related=related,
                           site_name=config.SITE_NAME)


@app.route("/category/<path:name>")
def category(name):
    page = max(1, request.args.get("page", 1, int))
    total = count("SELECT COUNT(*) FROM jobs WHERE category = ? OR state = ?",
                  (name, name))
    jobs = list_jobs("category = ? OR state = ?", (name, name),
                     limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    return render_template("list.html", jobs=jobs, title=name, page=page,
                           pages=max(1, -(-total // PAGE_SIZE)), total=total,
                           site_name=config.SITE_NAME)


@app.route("/search")
def search():
    kw = (request.args.get("q") or "").strip()
    page = max(1, request.args.get("page", 1, int))
    jobs, total = [], 0
    if kw:
        like = f"%{kw}%"
        where = ("title LIKE ? OR organization LIKE ? OR location LIKE ? "
                 "OR short_description LIKE ?")
        args = (like,) * 4
        total = count(f"SELECT COUNT(*) FROM jobs WHERE {where}", args)
        jobs = list_jobs(where, args, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return render_template("list.html", jobs=jobs, title=f"Search: {kw}" if kw else "Search",
                           page=page, pages=max(1, -(-total // PAGE_SIZE)),
                           total=total, q=kw, site_name=config.SITE_NAME)


@app.route("/admit-card")
@app.route("/sarkari-result")
@app.route("/answer-key")
@app.route("/syllabus")
@app.route("/employment-news")
@app.route("/latest-updates")
def section_pages():
    """Sections mirroring the original navigation; filtered views of scraped data."""
    path = request.path.strip("/")
    titles = {
        "admit-card": "Admit Card", "sarkari-result": "Sarkari Result",
        "answer-key": "Answer Key", "syllabus": "Syllabus",
        "employment-news": "Employment News", "latest-updates": "Latest Updates",
    }
    title = titles[path]
    page = max(1, request.args.get("page", 1, int))
    if path == "latest-updates":
        where, args = "1=1", ()
    elif path == "employment-news":
        where, args = "(title LIKE ? OR short_description LIKE ?)", ("%employment%",) * 2
    else:  # keyword-ish filter on title/description
        kws = {"admit-card": "%admit%", "sarkari-result": "%result%",
               "answer-key": "%answer key%", "syllabus": "%syllabus%"}
        where, args = "(title LIKE ? OR short_description LIKE ?)", (kws[path],) * 2
    total = count(f"SELECT COUNT(*) FROM jobs WHERE {where}", args)
    jobs = list_jobs(where, args, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return render_template("list.html", jobs=jobs, title=title, page=page,
                           pages=max(1, -(-total // PAGE_SIZE)), total=total,
                           site_name=config.SITE_NAME)


@app.route("/about-us")
@app.route("/contact")
@app.route("/privacy")
@app.route("/disclaimer")
def info_pages():
    return render_template("info.html", page=request.path.strip("/"),
                           site_name=config.SITE_NAME)


# ---------------------------------------------------------------------------
# Scrape control
# ---------------------------------------------------------------------------

_scrape_lock = threading.Lock()
_scrape_state = {"running": False, "last_result": None, "last_at": None}


@app.route("/admin/scrape", methods=["POST", "GET"])
def admin_scrape():
    if request.method == "GET":
        return jsonify(_scrape_state)

    def worker():
        with _scrape_lock:
            _scrape_state["running"] = True
            try:
                res = run_scrape()
                _scrape_state["last_result"] = res
            finally:
                _scrape_state["running"] = False
                _scrape_state["last_at"] = datetime.now().isoformat(timespec="seconds")

    if _scrape_state["running"]:
        return jsonify({"status": "already-running"})
    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"status": "started"})


# ---------------------------------------------------------------------------
# Daily scheduler (adds newly published content once per day)
# ---------------------------------------------------------------------------

def _next_run_time(hour: int, minute: int) -> datetime:
    now = datetime.now()
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return nxt


def scheduler_loop():
    nxt = _next_run_time(config.SCRAPE_HOUR, config.SCRAPE_MINUTE)
    while True:
        wait = (nxt - datetime.now()).total_seconds()
        print(f"[scheduler] next daily scrape at {nxt:%Y-%m-%d %H:%M}")
        time.sleep(max(30, wait))
        try:
            with _scrape_lock:
                res = run_scrape()
                _scrape_state["last_result"] = res
                _scrape_state["last_at"] = datetime.now().isoformat(timespec="seconds")
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] scrape error: {exc}")
        nxt = _next_run_time(config.SCRAPE_HOUR, config.SCRAPE_MINUTE)


def start_scheduler():
    t = threading.Thread(target=scheduler_loop, daemon=True, name="daily-scraper")
    t.start()
    return t


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scrape", action="store_true", help="run one scrape and exit")
    parser.add_argument("--no-scheduler", action="store_true")
    args = parser.parse_args()

    db.init_db()
    if args.scrape:
        run_scrape()
    else:
        if not args.no_scheduler:
            start_scheduler()
        app.run(host=config.HOST, port=config.PORT, debug=False)
