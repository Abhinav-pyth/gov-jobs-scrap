# Deploying to Vercel

This project is a Flask app plus a daily scraper. On Vercel it runs as:

| Piece | File | Role |
|---|---|---|
| Web app | `app.py` | Serverless function serving all pages (via `rewrites`) |
| Daily scrape | `api/scrape/run_scrape.py` | Serverless function invoked by Vercel Cron |
| Config | `vercel.json` | Builds, cron schedule (`0 6 * * *` = 06:00 UTC daily), rewrites |
| Deps | `requirements.txt` | flask, requests, beautifulsoup4, libsql |

## Important: storage
Vercel serverless functions have a **read-only, ephemeral filesystem**, so the
local SQLite file cannot persist there. The app therefore supports **Turso
(libSQL)** automatically whenever these env vars are set; otherwise it falls
back to local SQLite (fine for `vercel dev`).

## Steps

1. Create the Turso database (free tier is enough):
   ```bash
   npm i -g turso-cli && turso auth login
   turso db create sarkari-jobs
   turso db show sarkari-jobs --url     # -> TURSO_DATABASE_URL
   turso db tokens create sarkari-jobs  # -> TURSO_AUTH_TOKEN
   ```
   (Tables are created automatically on first run — no manual schema step.)

2. Push this repo to GitHub and import it at https://vercel.com/new
   (Framework: Other / Python — `vercel.json` handles everything).

3. In Project Settings → Environment Variables add:
   - `TURSO_DATABASE_URL` = `libsql://<name>.turso.io`
   - `TURSO_AUTH_TOKEN`   = `<token>`
   - `SCRAPE_SECRET`      = any random string (recommended)

4. Deploy. Vercel installs `requirements.txt`, builds both functions and
   registers the cron. Every day at **06:00 UTC** the scraper fetches newly
   published jobs from mysarkarinaukri.com and inserts them into Turso, so
   fresh content appears on the site daily.

5. Verify / trigger manually:
   ```bash
   curl -X POST -H "Authorization: Bearer $SCRAPE_SECRET" \
        https://<your-app>.vercel.app/api/scrape/run_scrape
   ```
   If you don't set `SCRAPE_SECRET`, the endpoint is open — anyone could
   trigger a scrape (harmless but wasteful), so setting it is recommended.
   Note: when `SCRAPE_SECRET` is set, also protect `/admin/scrape` similarly
   or remove that route in production.

## Local development
```bash
pip install -r requirements.txt
python app.py            # http://localhost:8000 (in-process daily scheduler)
python app.py --scrape   # one scrape now, then exit
vercel dev               # emulates functions + rewrites locally (uses SQLite)
```

## Notes / limits
- Hobby-plan crons run at most once per day; `0 6 * * *` matches the
  built-in scheduler's default hour (UTC). Adjust in `vercel.json`.
- Scrape runtime: with `CRAWL_DELAY_SECONDS=2` and
  `MAX_NEW_DETAILS_PER_RUN=40` a full run can exceed the 60s function
  limit. Lower `MAX_NEW_DETAILS_PER_RUN` in `config.py` (e.g. 15) or raise
  `maxDuration` in `vercel.json` (Pro plan allows up to 300s) if runs get
  truncated — partial results are fine since the next run continues where
  this one stopped (already-known URLs are skipped).
