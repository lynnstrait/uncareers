import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from scripts.common.helpers import log, normalize_space, escape_html, format_dot_date, parse_any_date_to_ts
from scripts.common.models import JobItem
from scripts.common.state import load_state, save_state
from scripts.common.telegram_utils import telegram_send

SOURCE_LABEL = "UNIDO"
SOURCE_URL = os.environ.get("SOURCE_URL", "https://careers.unido.org/search/?createNewAlert=false&q=&optionsFacetsDD_country=&optionsFacetsDD_lang=&optionsFacetsDD_department=&optionsFacetsDD_location=&locationsearch=").strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
STATE_FILE = Path(os.environ.get("STATE_FILE", "data/seen_unido.json"))
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "10"))
UNIDO_LOCATION_FILTER = os.environ.get("UNIDO_LOCATION_FILTER", "Vienna, Austria").strip().lower()
DISABLE_WEB_PAGE_PREVIEW = os.environ.get("DISABLE_WEB_PAGE_PREVIEW", "false").strip().lower()
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
BOOTSTRAP_MODE = os.environ.get("BOOTSTRAP_MODE", "false").strip().lower() == "true"

CATEGORY_CANDIDATES = [
    "International Professionals",
    "General Service",
    "Consultancy opportunities",
    "Internship Programme",
    "Project Appointments",
    "Junior Professional Officer Programme(JPO)",
    "Junior Professional Officer Programme",
    "National Professional officers",
    "Local Support Personnel",
]

def extract_category(text: str) -> str:
    for c in CATEGORY_CANDIDATES:
        if c.lower() in text.lower():
            return c
    return ""

def extract_level(text: str) -> str:
    m = re.search(r"\b(ISA\s*-\s*[A-Z0-9]+|ISA-[A-Z0-9]+|[PDGNLFS]\d|D1|D2|Intern|L2|P5|G4|G5)\b", text, re.I)
    if not m:
        return ""
    return normalize_space(m.group(1)).replace("ISA -", "ISA-").replace("ISA - ", "ISA-")

def extract_deadline(text: str) -> str:
    m = re.search(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4})\b", text)
    return m.group(1) if m else ""

def build_message(job: JobItem) -> str:
    parts = [f"<b>{escape_html(SOURCE_LABEL)}</b>", "", f"<b>{escape_html(job.title)}</b>"]
    if job.location: parts.append(f"Location: {escape_html(job.location)}")
    if job.level: parts.append(f"Level: {escape_html(job.level)}")
    if job.category: parts.append(f"Dept: {escape_html(job.category)}")
    if job.closing_date: parts.append(f"Closing: {escape_html(format_dot_date(job.closing_date))}")
    if job.link: parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')
    return "\n".join(parts)

def fetch_jobs() -> list[JobItem]:
    jobs = []
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(SOURCE_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)
        html = page.content()
        browser.close()

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)
    for a in anchors:
        href = (a.get("href") or "").strip()
        if "/job/" not in href and "careers.unido.org/job/" not in href:
            continue
        title = normalize_space(a.get_text(" ", strip=True))
        if not title:
            continue
        link = href if href.startswith("http") else f"https://careers.unido.org{href}"
        container_text = ""
        parent = a.parent
        if parent:
            container_text += " " + normalize_space(parent.get_text(" ", strip=True))
            gp = getattr(parent, "parent", None)
            if gp:
                container_text += " " + normalize_space(gp.get_text(" ", strip=True))
        container_text = normalize_space(container_text)
        location = "Vienna, Austria" if "vienna, austria" in container_text.lower() else ""
        if location.lower() != UNIDO_LOCATION_FILTER:
            continue
        category = extract_category(container_text)
        level = extract_level(container_text)
        deadline = extract_deadline(container_text)
        if link in seen:
            continue
        seen.add(link)
        jobs.append(JobItem(
            id=link,
            source=SOURCE_LABEL,
            title=title,
            link=link,
            location=location,
            level=level,
            category=category,
            closing_date=deadline,
            raw_date=deadline,
        ))
    return jobs

def main() -> int:
    log(f"Source label: {SOURCE_LABEL}")
    log(f"Source URL: {SOURCE_URL}")
    log(f"State file: {STATE_FILE}")
    log(f"DRY_RUN={DRY_RUN}")
    log(f"BOOTSTRAP_MODE={BOOTSTRAP_MODE}")
    jobs = fetch_jobs()
    log(f"Fetched jobs: {len(jobs)}")
    if not jobs:
        log("No jobs found.")
        return 0
    jobs.sort(key=lambda j: parse_any_date_to_ts(j.raw_date or j.closing_date), reverse=True)
    state = load_state(STATE_FILE)
    seen_ids = set(state.get("seen_ids", []))
    new_jobs = [j for j in jobs if j.id and j.id not in seen_ids]
    if not new_jobs:
        log("No new matching jobs.")
        return 0
    if BOOTSTRAP_MODE:
        ids = [j.id for j in new_jobs[:MAX_ALERTS_PER_RUN]]
        save_state(STATE_FILE, {"seen_ids": list(dict.fromkeys(ids + state.get("seen_ids", [])))[:1000]})
        log(f"BOOTSTRAP_MODE saved items: {len(ids)}")
        return 0
    sent = 0
    new_ids = []
    for job in new_jobs[:MAX_ALERTS_PER_RUN]:
        telegram_send(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, build_message(job), dry_run=DRY_RUN, disable_preview=DISABLE_WEB_PAGE_PREVIEW)
        sent += 1
        if not DRY_RUN:
            new_ids.append(job.id)
        time.sleep(1)
    if not DRY_RUN:
        save_state(STATE_FILE, {"seen_ids": list(dict.fromkeys(new_ids + state.get("seen_ids", [])))[:1000]})
    log(f"Alerts sent: {sent}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
