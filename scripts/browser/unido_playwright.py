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
    level = job.level or "-"
    duty = job.location or "-"
    duration = job.duration or "-"
    closing = format_dot_date(job.closing_date) if job.closing_date else "-"

    parts = [
        f"<b>{escape_html(SOURCE_LABEL)}</b>",
        "",
        f"<b>{escape_html(job.title)}</b>",
        f"Level: {escape_html(level)}",
        f"Duty: {escape_html(duty)}",
        f"Duration: {escape_html(duration)}",
        f"Closing: {escape_html(closing)}",
    ]

    if job.link:
        parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')

    return "\n".join(parts)


def fetch_rendered_page() -> tuple[str, str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(SOURCE_URL, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(6000)

        html = page.content()
        body_text = page.locator("body").inner_text()

        browser.close()
        return html, body_text


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

        if not href or not title:
            continue

        link = href if href.startswith("http") else f"https://careers.unido.org{href}"
        title_to_links.setdefault(title, []).append(link)

    return title_to_links


def extract_jobs_from_text(body_text: str, title_to_links: dict[str, list[str]]) -> list[JobItem]:
    jobs: list[JobItem] = []
    seen_links = set()

    text = body_text.replace("\r", "")
    all_titles = [t for t in title_to_links.keys() if t]

    # Find every occurrence of every title in the rendered body text
    occurrences: list[tuple[int, str]] = []
    for title in all_titles:
        for m in re.finditer(re.escape(title), text):
            occurrences.append((m.start(), title))

    occurrences.sort(key=lambda x: x[0])

    for idx, (start, title) in enumerate(occurrences):
        end = occurrences[idx + 1][0] if idx + 1 < len(occurrences) else min(len(text), start + 800)
        snippet = normalize_space(text[start:end])

        if "vienna, austria" not in snippet.lower():
            continue

        duty = extract_duty(snippet)
        if duty.lower() != UNIDO_LOCATION_FILTER:
            continue

        grade = extract_grade(snippet)
        duration = extract_duration(snippet)
        deadline = extract_application_deadline(snippet)

        link_queue = title_to_links.get(title, [])
        if not link_queue:
            continue

        link = link_queue.pop(0)
        if link in seen_links:
            continue
        seen_links.add(link)

        jobs.append(
            JobItem(
                id=link,
                source=SOURCE_LABEL,
                title=title,
                link=link,
                location=duty,
                level=grade,
                duration=duration,
                closing_date=deadline,
                raw_date=deadline,
            )
        )

    return jobs


def fetch_jobs() -> list[JobItem]:
    html, body_text = fetch_rendered_page()
    title_to_links = build_title_to_links(html)

    jobs = extract_jobs_from_text(body_text, title_to_links)

    # Final dedupe by link while preserving order
    deduped: list[JobItem] = []
    seen = set()
    for job in jobs:
        if job.link in seen:
            continue
        seen.add(job.link)
        deduped.append(job)

    return deduped


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
