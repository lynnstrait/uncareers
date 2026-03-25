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


def first_match(text: str, patterns: list[str]) -> str:
    for p in patterns:
        m = re.search(p, text, re.I | re.S)
        if m:
            return normalize_space(m.group(1))
    return ""


def extract_grade(text: str) -> str:
    value = first_match(text, [
        r"Grade[:\s]+([^\n]+)",
        r"Grade Level[:\s]+([^\n]+)",
        r"\b(ISA\s*-\s*[A-Z0-9]+|ISA-[A-Z0-9]+|[PDGNLFS]\d|D1|D2|Intern|L2|P5|G4|G5)\b",
    ])
    return value.replace("ISA -", "ISA-").replace("ISA - ", "ISA-")


def extract_duty_station(text: str) -> str:
    value = first_match(text, [
        r"Duty Station[:\s]+([^\n]+)",
        r"Location[:\s]+([^\n]+)",
    ])
    if value:
        return value

    m = re.search(r"\b([A-Za-z .'\-]+,\s*[A-Za-z .'\-]+)\b", text)
    return normalize_space(m.group(1)) if m else ""


def extract_duration(text: str) -> str:
    return first_match(text, [
        r"Duration[:\s]+([^\n]+)",
        r"Contract Duration[:\s]+([^\n]+)",
        r"Duration of Appointment[:\s]+([^\n]+)",
    ])


def extract_application_deadline(text: str) -> str:
    # Prefer labeled field first
    value = first_match(text, [
        r"Application Deadline[:\s]+([^\n]+)",
        r"Deadline[:\s]+([^\n]+)",
    ])
    if value:
        m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4}|\d{4}-\d{2}-\d{2})", value)
        if m:
            return m.group(1)

    m = re.search(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4}|\d{4}-\d{2}-\d{2})\b", text)
    return m.group(1) if m else ""


def build_message(job: JobItem) -> str:
    parts = [
        f"<b>{escape_html(SOURCE_LABEL)}</b>",
        "",
        f"<b>{escape_html(job.title)}</b>",
        f"Level: {escape_html(job.level or '-')}",
        f"Duty: {escape_html(job.location or '-')}",
        f"Duration: {escape_html(job.duration or '-')}",
        f"Closing: {escape_html(format_dot_date(job.closing_date) if job.closing_date else '-')}",
    ]

    if job.link:
        parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')

    return "\n".join(parts)


def fetch_listing_links() -> list[tuple[str, str]]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(SOURCE_URL, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(5000)

        items = page.eval_on_selector_all(
            'a[href*="/job/"]',
            """
            (anchors) => {
              function norm(s) {
                return (s || "").replace(/\\s+/g, " ").trim();
              }
              const out = [];
              const seen = new Set();

              for (const a of anchors) {
                const href = norm(a.getAttribute("href") || "");
                const title = norm(a.innerText || a.textContent || "");
                if (!href || !title) continue;

                const full = href.startsWith("http") ? href : ("https://careers.unido.org" + href);
                const key = title + "||" + full;
                if (seen.has(key)) continue;
                seen.add(key);

                out.push([title, full]);
              }
              return out;
            }
            """,
        )

        browser.close()

    cleaned: list[tuple[str, str]] = []
    seen_links = set()
    for title, link in items:
        title = normalize_space(title)
        link = normalize_space(link)
        if not title or not link:
            continue
        if link in seen_links:
            continue
        seen_links.add(link)
        cleaned.append((title, link))
    return cleaned


def fetch_detail_fields(browser, title: str, link: str) -> JobItem | None:
    page = browser.new_page()
    try:
        page.goto(link, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)
        text = page.locator("body").inner_text()
    finally:
        page.close()

    text = normalize_space(text)

    duty = extract_duty_station(text)
    if duty.lower() != UNIDO_LOCATION_FILTER:
        return None

    grade = extract_grade(text)
    duration = extract_duration(text)
    deadline = extract_application_deadline(text)

    return JobItem(
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


def fetch_jobs() -> list[JobItem]:
    listing_items = fetch_listing_links()
    log(f"UNIDO listing links: {len(listing_items)}")

    jobs: list[JobItem] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for title, link in listing_items:
            try:
                job = fetch_detail_fields(browser, title, link)
                if job:
                    jobs.append(job)
            except Exception as e:
                log(f"Failed UNIDO detail parse: {title} | {e}")

        browser.close()

    # final dedupe by link
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
