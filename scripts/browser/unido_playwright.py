import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from scripts.common.helpers import (
    log,
    normalize_space,
    escape_html,
    format_dot_date,
    parse_any_date_to_ts,
)
from scripts.common.models import JobItem
from scripts.common.state import load_state, save_state
from scripts.common.telegram_utils import telegram_send


SOURCE_LABEL = "UNIDO"
SOURCE_URL = os.environ.get(
    "SOURCE_URL",
    "https://careers.unido.org/search/?createNewAlert=false&q=&optionsFacetsDD_country=&optionsFacetsDD_lang=&optionsFacetsDD_department=&optionsFacetsDD_location=&locationsearch=",
).strip()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

STATE_FILE = Path(os.environ.get("STATE_FILE", "data/seen_unido.json"))
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "10"))
UNIDO_LOCATION_FILTER = os.environ.get("UNIDO_LOCATION_FILTER", "Vienna, Austria").strip().lower()

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


def extract_grade(text: str) -> str:
    m = re.search(
        r"\b(ISA\s*-\s*[A-Z0-9]+|ISA-[A-Z0-9]+|[PDGNLFS]\d|D1|D2|Intern|L2|P5|G4|G5)\b",
        text,
        re.I,
    )
    if not m:
        return ""
    return normalize_space(m.group(1)).replace("ISA -", "ISA-").replace("ISA - ", "ISA-")


def extract_duty(text: str) -> str:
    m = re.search(r"\b([A-Za-z .'\-]+,\s*[A-Za-z .'\-]+)\b", text)
    return normalize_space(m.group(1)) if m else ""


def extract_duration(text: str) -> str:
    patterns = [
        r"Duration[:\s]+([^\n|]+)",
        r"Contract Duration[:\s]+([^\n|]+)",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return normalize_space(m.group(1))
    return ""


def extract_application_deadline(text: str) -> str:
    m = re.search(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4})\b", text)
    return m.group(1) if m else ""


def build_message(job: JobItem) -> str:
    parts = [
        f"<b>{escape_html(SOURCE_LABEL)}</b>",
        "",
        f"<b>{escape_html(job.title)}</b>",
    ]

    if job.level:
        parts.append(f"Level: {escape_html(job.level)}")
    if job.location:
        parts.append(f"Duty: {escape_html(job.location)}")
    if job.duration:
        parts.append(f"Duration: {escape_html(job.duration)}")
    if job.closing_date:
        parts.append(f"Closing: {escape_html(format_dot_date(job.closing_date))}")
    if job.link:
        parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')

    return "\n".join(parts)


def fetch_rendered_page() -> tuple[str, str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(SOURCE_URL, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(5000)
        html = page.content()
        text = page.locator("body").inner_text()
        browser.close()
        return html, text


def build_title_to_links(html: str) -> dict[str, list[str]]:
    title_to_links: dict[str, list[str]] = {}

    for m in re.finditer(
        r'<a[^>]+href="(?P<link>https://careers\.unido\.org/job/[^"]+|/job/[^"]+)"[^>]*>(?P<title>.*?)</a>',
        html,
        re.I | re.S,
    ):
        href = (m.group("link") or "").strip()
        title = re.sub(r"<[^>]+>", " ", m.group("title") or "")
        title = normalize_space(title)

        if not title or not href:
            continue

        link = href if href.startswith("http") else f"https://careers.unido.org{href}"
        title_to_links.setdefault(title, []).append(link)

    return title_to_links


def parse_jobs_from_page_text(page_text: str, title_to_links: dict[str, list[str]]) -> list[JobItem]:
    lines = [normalize_space(x) for x in page_text.split("\n")]
    lines = [x for x in lines if x]

    jobs: list[JobItem] = []
    seen_links = set()

    i = 0
    while i + 2 < len(lines):
        title_line = lines[i]
        summary_line = lines[i + 1]
        metadata_line = lines[i + 2]

        # skip obvious noise/header rows
        if title_line.lower() in {
            "title",
            "location",
            "category",
            "grade",
            "application deadline",
            "title location category grade application deadline",
            "reset",
            "search results",
        }:
            i += 1
            continue

        # expected 3-line pattern:
        # title
        # title + Vienna, Austria + grade
        # Vienna, Austria + category + grade + deadline
        if title_line not in summary_line:
            i += 1
            continue

        if "vienna, austria" not in metadata_line.lower():
            i += 1
            continue

        duty = "Vienna, Austria"
        grade = extract_grade(metadata_line or summary_line)
        category = extract_category(metadata_line)
        duration = extract_duration(metadata_line) or extract_duration(summary_line)
        deadline = extract_application_deadline(metadata_line)

        if duty.lower() != UNIDO_LOCATION_FILTER:
            i += 1
            continue

        if not deadline:
            i += 1
            continue

        link_queue = title_to_links.get(title_line, [])
        if not link_queue:
            i += 1
            continue

        link = link_queue.pop(0)
        if link in seen_links:
            i += 1
            continue
        seen_links.add(link)

        jobs.append(
            JobItem(
                id=link,
                source=SOURCE_LABEL,
                title=title_line,
                link=link,
                location=duty,
                level=grade,
                category=category,
                duration=duration,
                closing_date=deadline,
                raw_date=deadline,
            )
        )

        i += 3

    return jobs


def fetch_jobs() -> list[JobItem]:
    html, page_text = fetch_rendered_page()
    title_to_links = build_title_to_links(html)
    jobs = parse_jobs_from_page_text(page_text, title_to_links)
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
        save_state(
            STATE_FILE,
            {"seen_ids": list(dict.fromkeys(ids + state.get("seen_ids", [])))[:1000]},
        )
        log(f"BOOTSTRAP_MODE saved items: {len(ids)}")
        return 0

    sent = 0
    new_ids = []

    for job in new_jobs[:MAX_ALERTS_PER_RUN]:
        telegram_send(
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            build_message(job),
            dry_run=DRY_RUN,
        )
        sent += 1
        if not DRY_RUN:
            new_ids.append(job.id)
        time.sleep(1)

    if not DRY_RUN:
        save_state(
            STATE_FILE,
            {"seen_ids": list(dict.fromkeys(new_ids + state.get("seen_ids", [])))[:1000]},
        )

    log(f"Alerts sent: {sent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
