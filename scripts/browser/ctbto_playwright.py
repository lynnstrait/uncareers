import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.common.helpers import log, normalize_space, escape_html, format_dot_date, parse_any_date_to_ts
from scripts.common.models import JobItem
from scripts.common.state import load_state, save_state
from scripts.common.telegram_utils import telegram_send

SOURCE_LABEL = "CTBTO"
SOURCE_URL = os.environ.get("SOURCE_URL", "https://career2.successfactors.eu/career?company=ctbtoprepa&career%5fns=job%5flisting%5fsummary&navBarLevel=JOB%5fSEARCH").strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
STATE_FILE = Path(os.environ.get("STATE_FILE", "data/seen_ctbto.json"))
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "10"))
CTBTO_LOCATION_FILTER = os.environ.get("CTBTO_LOCATION_FILTER", "").strip().lower()
DISABLE_WEB_PAGE_PREVIEW = os.environ.get("DISABLE_WEB_PAGE_PREVIEW", "false").strip().lower()
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
BOOTSTRAP_MODE = os.environ.get("BOOTSTRAP_MODE", "false").strip().lower() == "true"

def build_message(job: JobItem) -> str:
    parts = [f"<b>{escape_html(SOURCE_LABEL)}</b>", "", f"<b>{escape_html(job.title)}</b>"]
    if job.level: parts.append(f"Level: {escape_html(job.level)}")
    if job.department: parts.append(f"Dept: {escape_html(job.department)}")
    if job.appointment_type: parts.append(f"Type: {escape_html(job.appointment_type)}")
    if job.open_date: parts.append(f"Open: {escape_html(format_dot_date(job.open_date))}")
    if job.closing_date: parts.append(f"Closing: {escape_html(format_dot_date(job.closing_date))}")
    if job.link: parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')
    return "\n".join(parts)

def first(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1).strip()
    return ""

def fetch_jobs() -> list[JobItem]:
    jobs = []
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(SOURCE_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        html = page.content()
        browser.close()
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    req_ids = set(re.findall(r"career_job_req_id[=:\\/"]+(\d+)", html, re.I))
    for req_id in req_ids:
        link = ("https://career2.successfactors.eu/career?"
                f"company=ctbtoprepa&career_ns=job_listing&navBarLevel=JOB_SEARCH"
                f"&career_job_req_id={req_id}&selected_lang=en_GB&rcm_site_locale=en_GB")
        if link in seen:
            continue
        seen.add(link)
        jobs.append(JobItem(
            id=f"ctbto:{req_id}",
            source=SOURCE_LABEL,
            title=f"CTBTO Vacancy {req_id}",
            link=link,
            location="Vienna, Austria" if "vienna" in text.lower() else "",
            raw_date="",
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
    jobs.sort(key=lambda j: parse_any_date_to_ts(j.raw_date), reverse=True)
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
