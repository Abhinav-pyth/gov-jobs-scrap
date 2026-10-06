"""Central configuration for the SarkariNaukriMirror application."""

# ---------------------------------------------------------------------------
# Source site (the website we mirror / aggregate new content from)
# ---------------------------------------------------------------------------
SOURCE_BASE = "https://www.mysarkarinaukri.com"
SITEMAP_INDEX_URL = SOURCE_BASE + "/sitemap.xml"
LATEST_UPDATES_URL = SOURCE_BASE + "/latest-updates"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 MirrorBot/1.0"
)

# Politeness settings (respect robots.txt: Crawl-delay: 2)
CRAWL_DELAY_SECONDS = 2.0          # pause between page fetches
REQUEST_TIMEOUT = 25               # per-request timeout in seconds
MAX_NEW_DETAILS_PER_RUN = 40       # cap how many job pages we open per run

# Which sitemap files under SITEMAP_INDEX_URL to pull job URLs from
JOB_SITEMAP_PATHS = ["/sitemaps/jobs.xml"]

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
DATABASE_PATH = "data/mirror.sqlite3"
STATIC_DIR = "static"
HOST = "0.0.0.0"
PORT = 8000

# Scheduler: scrape once daily at this local time (24h clock).
SCRAPE_HOUR = 6
SCRAPE_MINUTE = 0

# Site branding for YOUR copy (do not impersonate the original brand).
SITE_NAME = "Sarkari Job Portal"
SITE_TAGLINE = "Latest Government Jobs, Results & Admit Cards - Updated Daily"
