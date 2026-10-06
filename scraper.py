"""scraper.py - daily content scraper for the Sarkari Naukri mirror.

How it works
------------
1. Reads the source site's XML sitemap index (https://.../sitemap.xml) and the
   "latest updates" page to discover job detail URLs.
2. Compares them against the local SQLite DB; only *new* URLs are fetched.
3. For every new URL it downloads the page, extracts structured data from the
   embedded JSON-LD (schema.org JobPosting + BreadcrumbList) plus the rendered
   article body (.job-rendered-body), sanitizes the HTML, and stores it.
4. Rewrites internal links so that pages pointing back at the source site open
   the source directly (we mirror listing/detail content we scraped, not their
   whole site).

Run manually:      python scraper.py
Or let app.py's built-in scheduler call run_scrape() once per day.

Politeness: honors robots.txt Crawl-delay (2s) and caps fetches per run.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape

import requests
from bs4 import BeautifulSoup

import config
import db

session = requests.Session()
session.headers.update({"User-Agent": config.USER_AGENT})

JOB_URL_RE = re.compile(r"^https?://[^/]+/(find|recruitment)/.+/\d{3,}$")


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch(url: str) -> str | None:
    try:
        r = session.get(url, timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        # Source sends no charset in headers; pages are UTF-8 (meta charset).
        ctype = r.headers.get("content-type", "")
        if "charset" not in ctype.lower():
            r.encoding = "utf-8"
        return r.text
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] fetch failed {url}: {exc}")
        return None
    finally:
        time.sleep(config.CRAWL_DELAY_SECONDS)


def discover_urls() -> list[tuple[str, str | None]]:
    """Return (url, lastmod) pairs from sitemaps + latest-updates page."""
    found: dict[str, str | None] = {}

    index_xml = fetch(config.SITEMAP_INDEX_URL)
    if index_xml:
        try:
            root = ET.fromstring(index_xml)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            for sm in root.findall("sm:sitemap", ns):
                loc = sm.findtext("sm:loc", "", ns).strip()
                path = "/" + loc.split("/", 3)[3] if loc else ""
                if any(path.endswith(p) for p in config.JOB_SITEMAP_PATHS):
                    xml = fetch(loc)
                    if not xml:
                        continue
                    sroot = ET.fromstring(xml)
                    for url_el in sroot.findall("sm:url", ns):
                        u = url_el.findtext("sm:loc", "", ns).strip()
                        lm = url_el.findtext("sm:lastmod", "", ns).strip() or None
                        if JOB_URL_RE.match(u):
                            found.setdefault(u, lm)
        except ET.ParseError as exc:
            print(f"[warn] sitemap parse error: {exc}")

    # Fallback / extra source: the latest-updates listing page
    html = fetch(config.LATEST_UPDATES_URL)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                href = config.SOURCE_BASE + href
            if JOB_URL_RE.match(href):
                found.setdefault(href, None)

    return list(found.items())


# ---------------------------------------------------------------------------
# Parsing one job page
# ---------------------------------------------------------------------------

def _jsonld_jobs(html: str) -> dict:
    out: dict = {}
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        t = data.get("@type")
        if t == "JobPosting":
            out["posting"] = data
        elif t == "BreadcrumbList":
            out["breadcrumbs"] = [i.get("name") for i in data.get("itemListElement", [])]
    return out


def _clean_body_html(html: str) -> str:
    """Extract .job-rendered-body (fallback: <main>) and sanitize it.

    Returns the *outer* HTML of the body node so the heading structure
    (<h2>/<h3>/<table>...) is preserved for readable rendering.
    """
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(".job-rendered-body") or soup.find("main")
    if node is None:
        return ""
    # drop scripts/styles/forms/embeds and promo widgets
    for bad in node.find_all(["script", "style", "form", "iframe", "noscript",
                              "button", "svg", "aside"]):
        bad.decompose()
    for el in node.select(".promo-box, .alert-subscribe, .share-box, .related-posts"):
        el.decompose()
    # rewrite links: keep external official links, neutralize internal ones
    for a in node.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            a["href"] = config.SOURCE_BASE + href
            a["target"] = "_blank"
            a["rel"] = "noopener nofollow"
        elif config.SOURCE_BASE in href:
            a["rel"] = (a.get("rel", "") + " noopener nofollow").strip()
            a["target"] = "_blank"
    # strip class attributes referencing their design system
    for el in node.find_all(True):
        cls = el.get("class") or []
        el["class"] = [c for c in cls if not c.startswith("msn-")]
        for attr in ("style", "onclick", "data-track"):
            if el.has_attr(attr):
                del el[attr]
    outer = str(node).strip()
    # remove obvious promotional blocks about *their* brand
    outer = re.sub(r"(?is)<(div|p)[^>]*>.*?(mysarkarinaukri|my sarkari naukri).*?</\1>",
                   "", outer)
    return outer


def _fix_mojibake(s: str | None) -> str | None:
    """Repair UTF-8 bytes that were wrongly decoded as latin-1 (Ã¢â‚¬ etc.)."""
    if not s:
        return s
    for _ in range(3):
        try:
            fixed = s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if fixed == s:
            break
        s = fixed
    # normalize leftover en/em dashes & smart quotes variants
    s = s.replace("\u00e2\u0080\u0093", "–").replace("\u00e2\u0080\u0094", "—")
    s = re.sub(r"[Ââ][€‚„]?[“”–—]", lambda m: {"–": "–", "—": "—"}.get(m.group(0), " "), s)
    return s


def _clean_value(v: str | None) -> str | None:
    if not v:
        return None
    v = _fix_mojibake(v.strip())
    v = unescape(v)
    v = re.sub(r"\s+", " ", v).strip(" -:")
    return v[:200] or None


def _field_from_text(text: str, *labels: str) -> str | None:
    """Find 'Label: value' in text, trying several label synonyms."""
    for label in labels:
        m = re.search(re.escape(label) + r"\s*[:\-]?\s*(.{2,160}?)(?:\n|$)",
                      text, flags=re.I)
        if m:
            return _clean_value(m.group(1))
    return None


def parse_job(url: str, html: str, lastmod: str | None) -> dict | None:
    ld = _jsonld_jobs(html)
    posting = ld.get("posting") or {}
    soup = BeautifulSoup(html, "html.parser")

    title = (posting.get("title")
             or (soup.find("h1").get_text(strip=True) if soup.find("h1") else None))
    if not title:
        return None

    desc_html = posting.get("description") or ""
    plain = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True) \
        or soup.get_text(" ", strip=True)[:600]

    org = ((posting.get("hiringOrganization") or {}).get("name")) or None
    ident = posting.get("identifier") or {}
    if isinstance(ident, dict):
        org = ident.get("name") or org

    breadcrumb = [b for b in ld.get("breadcrumbs", []) if b and b != "Home"]
    category = breadcrumb[0] if len(breadcrumb) > 1 else (breadcrumb[0] if breadcrumb else None)

    addr = (((posting.get("jobLocation") or {}).get("address")) or {})
    location = ", ".join(x for x in [addr.get("addressLocality"),
                                     addr.get("addressRegion")] if x) or None

    body_html = _clean_body_html(html)
    body_text = BeautifulSoup(body_html, "html.parser").get_text("\n", strip=True)

    date_posted = posting.get("datePosted") or lastmod or ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", date_posted)
    date_posted = m.group(1) if m else (lastmod or "")[:10]

    emp = posting.get("employmentType")
    if isinstance(emp, list):
        emp = ", ".join(emp).title()
    elif emp:
        emp = emp.title()

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    idm = re.search(r"/(\d{3,})$", url)
    return {
        "id": int(idm.group(1)) if idm else None,
        "source_url": url,
        "slug": url.rstrip("/").split("/")[-1],
        "title": _clean_value(title),
        "organization": _clean_value(org),
        "org_slug": url.rstrip("/").split("/")[-2] if "/find/" in url or "/recruitment/" in url else None,
        "category": _clean_value(category),
        "location": _clean_value(location),
        "state": _clean_value(addr.get("addressRegion")),
        "total_vacancies": _field_from_text(body_text, "Total Vacancies") or _field_from_text(plain, "Total Vacancies"),
        "qualification": _field_from_text(body_text, "Qualification") or _field_from_text(plain, "Eligibility"),
        "age_limit": _field_from_text(body_text, "Age Limit"),
        "salary": _field_from_text(body_text, "Salary") or _field_from_text(body_text, "Pay Scale"),
        "date_posted": date_posted,
        "valid_through": (posting.get("validThrough") or "")[:10] or None,
        "employment_type": emp,
        "short_description": _clean_value(plain)[:500] if plain else None,
        "content_html": body_html,
        "is_new": 1,
        "first_seen": now,
        "last_updated": now,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_scrape(max_new: int | None = None) -> dict:
    max_new = max_new if max_new is not None else config.MAX_NEW_DETAILS_PER_RUN
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.init_db()

    urls = discover_urls()
    stats = {"urls_found": len(urls), "new_jobs": 0, "updated_jobs": 0,
             "errors": 0, "note": ""}
    print(f"[info] discovered {len(urls)} job URLs")

    with db.get_db() as conn:
        known = db.known_urls(conn)
        todo = [(u, lm) for (u, lm) in urls if u not in known][:max_new]
        print(f"[info] {len(todo)} new URLs to fetch this run")

        for url, lm in todo:
            html = fetch(url)
            if not html:
                stats["errors"] += 1
                continue
            try:
                job = parse_job(url, html, lm)
                if job and db.upsert_job(conn, job) == "inserted":
                    stats["new_jobs"] += 1
                    print(f"[ok] + {job['title'][:70]}")
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                print(f"[warn] parse failed {url}: {exc}")

        finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO scrape_runs (started_at, finished_at, urls_found,"
            " new_jobs, updated_jobs, errors, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (started, finished, stats["urls_found"], stats["new_jobs"],
             stats["updated_jobs"], stats["errors"], stats["note"]),
        )
    print(f"[done] {stats}")
    return stats


if __name__ == "__main__":
    run_scrape()
