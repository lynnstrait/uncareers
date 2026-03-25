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


def extract_category(text: str) -> str:
    candidates = [
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
    for c in candidates:
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


def fetch_jobs() -> list[JobItem]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)

        records = page.eval_on_selector_all(
            'a[href*="/job/"]',
            """
            (anchors) => {
              function norm(s) {
                return (s || "").replace(/\\s+/g, " ").trim();
              }

              const out = [];

              for (const a of anchors) {
                const href = a.getAttribute("href") || "";
                const title = norm(a.innerText || a.textContent || "");
                if (!href || !title) continue;

                let node = a;
                let bestText = "";
                let depth = 0;

                while (node && depth < 8) {
                  const txt = norm(node.innerText || node.textContent || "");
                  if (txt && txt.toLowerCase().includes("vienna, austria")) {
                    if (!bestText || txt.length < bestText.length) {
                      bestText = txt;
                    }
                  }
                  node = node.parentElement;
                  depth += 1;
                }

                // Fallback: look at nearby siblings if ancestor search failed
                if (!bestText && a.parentElement) {
                  let txts = [];
                  let sib = a.parentElement.previousElementSibling;
                  let count = 0;
                  while (sib && count < 3) {
                    txts.push(norm(sib.innerText || sib.textContent || ""));
                    sib = sib.previousElementSibling;
                    count += 1;
                  }
                  sib = a.parentElement.nextElementSibling;
                  count = 0;
                  while (sib && count < 3) {
                    txts.push(norm(sib.innerText || sib.textContent || ""));
                    sib = sib.nextElementSibling;
                    count += 1;
                  }
                  const merged = norm(txts.join(" | "));
                  if (merged.toLowerCase().includes("vienna, austria")) {
                    bestText = merged;
                  }
                }

                out.push({
                  href,
                  title,
                  context: bestText
                });
              }

              return out;
            }
            """,
        )

        browser.close()

    jobs: list[JobItem] = []
    seen_links = set()

    for rec in records:
        href = normalize_space(rec.get("href", ""))
        title = normalize_space(rec.get("title", ""))
        context = normalize_space(rec.get("context", ""))

        if not href or not title or not context:
            continue

        link = href if href.startswith("http") else f"https://careers.unido.org{href}"

        duty = extract_duty(context)
        if duty.lower() != UNIDO_LOCATION_FILTER:
            continue

        grade = extract_grade(context)
        category = extract_category(context)
        duration = extract_duration(context)
        deadline = extract_application_deadline(context)

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
                category=category,
                duration=duration,
                closing_date=deadline,
                raw_date=deadline,
            )
        )

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
