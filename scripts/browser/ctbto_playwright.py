import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error

from pathlib import Path
from playwright.sync_api import sync_playwright

from scripts.common.helpers import log, strip_html, normalize_space, format_dot_date
from scripts.common.state import load_state, save_state
from scripts.common.telegram_utils import telegram_send


SOURCE_LABEL = "CTBTO"
SOURCE_URL = os.environ.get(
    "SOURCE_URL",
    "https://career2.successfactors.eu/career?company=ctbtoprepa&career%5fns=job%5flisting%5fsummary&navBarLevel=JOB%5fSEARCH",
).strip()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "10"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "data/seen_ctbto.json"))

DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
BOOTSTRAP_MODE = os.environ.get("BOOTSTRAP_MODE", "false").strip().lower() == "true"


def build_detail_link(req_id: str) -> str:
    return (
        "https://career2.successfactors.eu/career?"
        f"company=ctbtoprepa&career_ns=job_listing&navBarLevel=JOB_SEARCH"
        f"&career_job_req_id={req_id}&selected_lang=en_GB&rcm_site_locale=en_GB"
    )


def extract_req_ids_from_html(html: str) -> list[str]:
    req_ids = set()

    patterns = [
        r'career_job_req_id[=:\\/"]+(\d+)',
        r'"career_job_req_id"\s*:\s*"?(\\?\d+)"?',
        r'career_job_req_id=(\d+)',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, html, re.I):
            req_id = str(match).replace("\\", "").strip()
            if req_id.isdigit():
                req_ids.add(req_id)

    return sorted(req_ids)


def fetch_rendered_html(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)
        html = page.content()
        browser.close()
        return html


def fetch_detail_text(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)
        text = page.locator("body").inner_text()
        browser.close()
        return normalize_space(text)


def first_match(text: str, patterns: list[str]) -> str:
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return normalize_space(m.group(1))
    return ""


def fetch_ctbto_jobs() -> list[dict]:
    html = fetch_rendered_html(SOURCE_URL)
    req_ids = extract_req_ids_from_html(html)

    log(f"CTBTO discovered req ids: {len(req_ids)}")

    jobs = []
    seen_links = set()

    for req_id in req_ids:
        link = build_detail_link(req_id)
        if link in seen_links:
            continue
        seen_links.add(link)

        text = fetch_detail_text(link)
        lowered = text.lower()

        if "this job cannot be viewed at the moment" in lowered:
            continue
        if "is no longer available for application" in lowered:
            continue

        title = first_match(text, [
            r"Job Title[:\s]+(.+?)(?:Grade Level|Division|Type of Appointment|Date of Issuance|Deadline for Applications)",
            r"Title[:\s]+(.+?)(?:Grade Level|Division|Type of Appointment|Date of Issuance|Deadline for Applications)",
        ])

        level = first_match(text, [
            r"Grade Level[:\s]+(.+?)(?:Division|Type of Appointment|Date of Issuance|Deadline for Applications)",
            r"Grade[:\s]+(.+?)(?:Division|Type of Appointment|Date of Issuance|Deadline for Applications)",
        ])

        dept = first_match(text, [
            r"Division[:\s]+(.+?)(?:Type of Appointment|Date of Issuance|Deadline for Applications)",
            r"Department[:\s]+(.+?)(?:Type of Appointment|Date of Issuance|Deadline for Applications)",
        ])

        appointment_type = first_match(text, [
            r"Type of Appointment[:\s]+(.+?)(?:Date of Issuance|Deadline for Applications)",
        ])

        open_date = first_match(text, [
            r"Date of Issuance[:\s]+(.+?)(?:Deadline for Applications|Reporting Date)",
            r"Date of Issue[:\s]+(.+?)(?:Deadline for Applications|Reporting Date)",
        ])

        closing_date = first_match(text, [
            r"Deadline for Applications[:\s]+(.+?)(?:Reporting Date|Please note|$)",
            r"Deadline[:\s]+(.+?)(?:Reporting Date|Please note|$)",
        ])

        if not title:
            title = f"CTBTO Vacancy {req_id}"

        jobs.append({
            "id": f"ctbto:{req_id}",
            "title": title,
            "link": link,
            "level": level,
            "department": dept,
            "appointment_type": appointment_type,
            "open_date": open_date,
            "closing_date": closing_date,
        })

    return jobs


def build_message(job: dict) -> str:
    parts = [
        "<b>CTBTO</b>",
        "",
        f"<b>{job['title']}</b>",
    ]

    if job.get("level"):
        parts.append(f"Level: {job['level']}")
    if job.get("department"):
        parts.append(f"Dept: {job['department']}")
    if job.get("appointment_type"):
        parts.append(f"Type: {job['appointment_type']}")
    if job.get("open_date"):
        parts.append(f"Open: {format_dot_date(job['open_date'])}")
    if job.get("closing_date"):
        parts.append(f"Closing: {format_dot_date(job['closing_date'])}")
    if job.get("link"):
        parts.append(f'<a href="{job["link"]}">Job Open</a>')

    return "\n".join(parts)


def main() -> int:
    log(f"Source label: {SOURCE_LABEL}")
    log(f"Source URL: {SOURCE_URL}")
    log(f"State file: {STATE_FILE}")
    log(f"DRY_RUN={DRY_RUN}")
    log(f"BOOTSTRAP_MODE={BOOTSTRAP_MODE}")

    jobs = fetch_ctbto_jobs()
    log(f"Fetched jobs: {len(jobs)}")

    if not jobs:
        log("No jobs found.")
        return 0

    state = load_state(STATE_FILE)
    seen_ids = set(state.get("seen_ids", []))
    new_jobs = [job for job in jobs if job["id"] not in seen_ids]

    if not new_jobs:
        log("No new matching jobs.")
        return 0

    if BOOTSTRAP_MODE:
        bootstrap_ids = [job["id"] for job in new_jobs[:MAX_ALERTS_PER_RUN]]
        merged = list(dict.fromkeys(bootstrap_ids + state.get("seen_ids", [])))[:1000]
        save_state(STATE_FILE, {"seen_ids": merged})
        log(f"BOOTSTRAP_MODE saved items: {len(bootstrap_ids)}")
        return 0

    alerts_sent = 0
    new_ids = []

    for job in new_jobs[:MAX_ALERTS_PER_RUN]:
        try:
            message_html = build_message(job)
            telegram_send(
                bot_token=TELEGRAM_BOT_TOKEN,
                chat_id=TELEGRAM_CHAT_ID,
                message_html=message_html,
                disable_web_page_preview=False,
                dry_run=DRY_RUN,
            )
            alerts_sent += 1

            if not DRY_RUN:
                new_ids.append(job["id"])

            time.sleep(1)
        except Exception as e:
            log(f"Failed to send Telegram message: {e}")

    if not DRY_RUN:
        merged = list(dict.fromkeys(new_ids + state.get("seen_ids", [])))[:1000]
        save_state(STATE_FILE, {"seen_ids": merged})

    log(f"Alerts sent: {alerts_sent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
