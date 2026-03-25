import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from scripts.common.helpers import log, normalize_space, escape_html, format_dot_date
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


def fetch_detail_payload(url: str) -> tuple[str, str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)

        body_text = page.locator("body").inner_text()
        page_title = page.title()

        browser.close()
        return body_text, normalize_space(page_title)


def first_match(text: str, patterns: list[str]) -> str:
    for p in patterns:
        m = re.search(p, text, re.I | re.S)
        if m:
            return normalize_space(m.group(1))
    return ""


def clean_title(title: str, req_id: str) -> str:
    title = normalize_space(title)
    if not title:
        return f"CTBTO Vacancy {req_id}"

    title = re.sub(r"^(job title|title)\s*[:\-]?\s*", "", title, flags=re.I).strip()
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"\s*-\s*CTBTO.*$", "", title, flags=re.I).strip()
    title = re.sub(r"\s*\|\s*CTBTO.*$", "", title, flags=re.I).strip()

    if title.lower() in {"ctbto vacancy", "vacancy", "job title", "title"}:
        return f"CTBTO Vacancy {req_id}"

    return title


def clean_section(value: str) -> str:
    value = normalize_space(value)
    if not value:
        return ""

    m = re.search(r"([A-Za-z0-9&,\-()/' ]+Section)", value, re.I)
    if m:
        return normalize_space(m.group(1))

    value = re.sub(r"\bDivision[:\s].*?(?=Section[:\s]|$)", "", value, flags=re.I).strip()
    value = re.sub(r"\bOffice[:\s].*?(?=Section[:\s]|$)", "", value, flags=re.I).strip()
    value = re.sub(r"\bUnit[:\s].*$", "", value, flags=re.I).strip()
    value = re.sub(r"^Section[:\s]*", "", value, flags=re.I).strip()

    return normalize_space(value)


def clean_closing(value: str) -> str:
    value = normalize_space(value)
    if not value:
        return ""

    m = re.search(
        r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{4}-\d{2}-\d{2})",
        value,
        re.I,
    )
    if m:
        return normalize_space(m.group(1))

    return value


def clean_open(value: str) -> str:
    value = normalize_space(value)
    if not value:
        return ""

    m = re.search(
        r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{4}-\d{2}-\d{2})",
        value,
        re.I,
    )
    if m:
        return normalize_space(m.group(1))

    return value


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

        text, page_title = fetch_detail_payload(link)
        lowered = text.lower()

        if "this job cannot be viewed at the moment" in lowered:
            continue
        if "is no longer available for application" in lowered:
            continue

        title_raw = first_match(text, [
            r"Job Title[:\s]+(.+?)(?:\n|Grade Level|Grade|Division|Department|Section|Type of Appointment|Date of Issuance|Date of Issue|Deadline for Applications|Deadline)",
            r"Title[:\s]+(.+?)(?:\n|Grade Level|Grade|Division|Department|Section|Type of Appointment|Date of Issuance|Date of Issue|Deadline for Applications|Deadline)",
        ])

        if not normalize_space(title_raw):
            cleaned_page_title = re.sub(r"\s*-\s*CTBTO.*$", "", page_title, flags=re.I).strip()
            cleaned_page_title = re.sub(r"\s*\|\s*CTBTO.*$", "", cleaned_page_title, flags=re.I).strip()
            if cleaned_page_title and "ctbto vacancy" not in cleaned_page_title.lower():
                title_raw = cleaned_page_title

        title = clean_title(title_raw, req_id)

        level = first_match(text, [
            r"Grade Level[:\s]+(.+?)(?:Division|Department|Section|Type of Appointment|Date of Issuance|Date of Issue|Deadline for Applications|Deadline)",
            r"Grade[:\s]+(.+?)(?:Division|Department|Section|Type of Appointment|Date of Issuance|Date of Issue|Deadline for Applications|Deadline)",
        ])

        section_raw = first_match(text, [
            r"Section[:\s]+(.+?)(?:Unit|Type of Appointment|Date of Issuance|Date of Issue|Deadline for Applications|Deadline)",
            r"Division[:\s]+(.+?)(?:Section|Unit|Type of Appointment|Date of Issuance|Date of Issue|Deadline for Applications|Deadline)",
            r"Department[:\s]+(.+?)(?:Section|Unit|Type of Appointment|Date of Issuance|Date of Issue|Deadline for Applications|Deadline)",
        ])
        section = clean_section(section_raw)

        appointment_type = first_match(text, [
            r"Type of Appointment[:\s]+(.+?)(?:Date of Issuance|Date of Issue|Deadline for Applications|Deadline)",
        ])

        open_raw = first_match(text, [
            r"Date of Issuance[:\s]+(.+?)(?:Deadline for Applications|Deadline|Reporting Date|Please note|$)",
            r"Date of Issue[:\s]+(.+?)(?:Deadline for Applications|Deadline|Reporting Date|Please note|$)",
        ])
        open_date = clean_open(open_raw)

        closing_raw = first_match(text, [
            r"Deadline for Applications[:\s]+(.+?)(?:Reporting Date|Please note|$)",
            r"Deadline[:\s]+(.+?)(?:Reporting Date|Please note|$)",
        ])
        closing_date = clean_closing(closing_raw)

        jobs.append({
            "id": f"ctbto:{req_id}",
            "title": title,
            "link": link,
            "level": normalize_space(level),
            "section": section,
            "appointment_type": normalize_space(appointment_type),
            "open_date": open_date,
            "closing_date": closing_date,
        })

    return jobs


def build_message(job: dict) -> str:
    parts = [
        f"<b>{escape_html(SOURCE_LABEL)}</b>",
        "",
        f"<b>{escape_html(job['title'])}</b>",
    ]

    if job.get("level"):
        parts.append(f"Level: {escape_html(job['level'])}")
    if job.get("section"):
        parts.append(f"Sect: {escape_html(job['section'])}")
    if job.get("appointment_type"):
        parts.append(f"Type: {escape_html(job['appointment_type'])}")
    if job.get("open_date"):
        parts.append(f"Open: {escape_html(format_dot_date(job['open_date']))}")
    if job.get("closing_date"):
        parts.append(f"Closing: {escape_html(format_dot_date(job['closing_date']))}")
    if job.get("link"):
        parts.append(f'<a href="{escape_html(job["link"])}">Job Open</a>')

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
                TELEGRAM_BOT_TOKEN,
                TELEGRAM_CHAT_ID,
                message_html,
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
