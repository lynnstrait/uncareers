import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

SOURCE_LABEL = "CTBTO"
SOURCE_URL = os.environ.get(
    "SOURCE_URL",
    "https://career2.successfactors.eu/career?company=ctbtoprepa&career%5fns=job%5flisting%5fsummary&navBarLevel=JOB%5fSEARCH",
).strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "10"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "data/seen_ctbto.json"))
DISABLE_WEB_PAGE_PREVIEW = os.environ.get("DISABLE_WEB_PAGE_PREVIEW", "false").strip().lower()
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
BOOTSTRAP_MODE = os.environ.get("BOOTSTRAP_MODE", "false").strip().lower() == "true"


def log(msg: str) -> None:
    print(msg, flush=True)


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_dot_date(date_str: str) -> str:
    if not date_str:
        return ""
    normalized = " ".join(date_str.split())
    try:
        dt = parsedate_to_datetime(normalized)
        return dt.strftime("%Y. %m. %d.")
    except Exception:
        pass
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%b %d, %Y"):
        try:
            dt = datetime.strptime(normalized, fmt)
            return dt.strftime("%Y. %m. %d.")
        except ValueError:
            pass
    return date_str


def parse_any_date_to_ts(value: str) -> float:
    if not value:
        return 0.0
    normalized = " ".join(value.split())
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%b %d, %Y"):
        try:
            return datetime.strptime(normalized, fmt).timestamp()
        except Exception:
            pass
    try:
        return parsedate_to_datetime(normalized).timestamp()
    except Exception:
        return 0.0


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen_ids": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def telegram_send(message_html: str) -> None:
    if DRY_RUN:
        log("DRY_RUN=true, skipping Telegram send.")
        log(message_html)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": DISABLE_WEB_PAGE_PREVIEW,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        log(f"Telegram response: {body[:500]}")


@dataclass
class JobItem:
    id: str
    title: str
    link: str
    level: str
    department: str
    appointment_type: str
    open_date: str
    closing_date: str

    def sort_ts(self) -> float:
        return parse_any_date_to_ts(self.closing_date or self.open_date)


def build_message(job: JobItem) -> str:
    parts = [f"<b>{SOURCE_LABEL}</b>", "", f"<b>{escape_html(job.title)}</b>"]
    if job.level:
        parts.append(f"Level: {escape_html(job.level)}")
    if job.department:
        parts.append(f"Dept: {escape_html(job.department)}")
    if job.appointment_type:
        parts.append(f"Type: {escape_html(job.appointment_type)}")
    if job.open_date:
        parts.append(f"Open: {escape_html(format_dot_date(job.open_date))}")
    if job.closing_date:
        parts.append(f"Closing: {escape_html(format_dot_date(job.closing_date))}")
    if job.link:
        parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')
    return "\n".join(parts)


def fetch_ctbto_jobs() -> list[JobItem]:
    jobs: list[JobItem] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(SOURCE_URL, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(8000)

        links = page.locator('a[href*="career_job_req_id="]')
        count = links.count()
        log(f"CTBTO rendered job links found: {count}")
        seen = set()

        for i in range(count):
            a = links.nth(i)
            href = a.get_attribute("href") or ""
            link = urllib.parse.urljoin("https://career2.successfactors.eu", href.split("#", 1)[0])
            if not link or link in seen:
                continue
            seen.add(link)

            title = normalize_space(a.inner_text())
            row_text = ""
            try:
                row_text = normalize_space(a.locator("xpath=ancestor::*[self::tr or self::li or self::div][1]").inner_text())
            except Exception:
                row_text = title

            def first(patterns):
                for pat in patterns:
                    m = re.search(pat, row_text, re.IGNORECASE)
                    if m:
                        return normalize_space(m.group(1))
                return ""

            level = first([r"Grade Level[:\s]+([^\n]+)", r"Grade[:\s]+([^\n]+)"])
            department = first([r"Division[:\s]+([^\n]+)", r"Department[:\s]+([^\n]+)"])
            appointment_type = first([r"Type of Appointment[:\s]+([^\n]+)"])
            open_date = first([r"Date of Issuance[:\s]+([^\n]+)", r"Date Posted[:\s]+([^\n]+)"])
            closing_date = first([r"Deadline for Applications[:\s]+([^\n]+)", r"Deadline[:\s]+([^\n]+)"])

            if not title:
                continue

            jobs.append(JobItem(
                id=link,
                title=title,
                link=link,
                level=level,
                department=department,
                appointment_type=appointment_type,
                open_date=open_date,
                closing_date=closing_date,
            ))

        browser.close()

    return jobs


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

    jobs.sort(key=lambda x: x.sort_ts(), reverse=True)

    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    new_jobs = [job for job in jobs if job.id and job.id not in seen_ids]

    if not new_jobs:
        log("No new matching jobs.")
        return 0

    if BOOTSTRAP_MODE:
        bootstrap_ids = [job.id for job in new_jobs[:MAX_ALERTS_PER_RUN]]
        merged = list(dict.fromkeys(bootstrap_ids + state.get("seen_ids", [])))[:1000]
        save_state({"seen_ids": merged})
        log(f"BOOTSTRAP_MODE saved items: {len(bootstrap_ids)}")
        return 0

    alerts_sent = 0
    new_ids = []
    for job in new_jobs[:MAX_ALERTS_PER_RUN]:
        telegram_send(build_message(job))
        alerts_sent += 1
        if not DRY_RUN:
            new_ids.append(job.id)
        time.sleep(1)

    if not DRY_RUN:
        merged = list(dict.fromkeys(new_ids + state.get("seen_ids", [])))[:1000]
        save_state({"seen_ids": merged})

    log(f"Alerts sent: {alerts_sent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
