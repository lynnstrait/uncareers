import json
import os
import re
import sys
import time
import html
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path
from datetime import datetime

import feedparser

FEED_URL = os.environ["FEED_URL"].strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

KEYWORD = os.environ.get("KEYWORD", "").strip().lower()
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "10"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "data/seen_jobs.json"))
SOURCE_LABEL = os.environ.get("SOURCE_LABEL", "UN Careers").strip()


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; RSSJobWatcher/1.0)",
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen_ids": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("&#13;", "\n")
    text = text.replace("\r", "")
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\n+", "\n", text)

    replacements = {
        "�셲": "’s",
        "�": "'",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)

    return text.strip()


def parse_date(item: dict) -> float:
    value = item.get("published", "") or item.get("updated", "")
    if value:
        try:
            return parsedate_to_datetime(value).timestamp()
        except Exception:
            pass
    return 0.0


def parse_rss(xml_bytes: bytes) -> list[dict]:
    feed = feedparser.parse(xml_bytes)

    if getattr(feed, "bozo", 0):
        log(f"Feed parser warning: {feed.bozo_exception}")

    items = []
    for entry in feed.entries:
        title = (getattr(entry, "title", "") or "").strip()
        link = (getattr(entry, "link", "") or "").strip()

        description = ""
        if hasattr(entry, "summary"):
            description = entry.summary
        elif hasattr(entry, "description"):
            description = entry.description

        published = ""
        if hasattr(entry, "published"):
            published = entry.published
        elif hasattr(entry, "updated"):
            published = entry.updated

        guid = ""
        if hasattr(entry, "id"):
            guid = entry.id
        elif hasattr(entry, "guid"):
            guid = entry.guid

        items.append({
            "title": title,
            "link": link,
            "description": strip_html(description),
            "published": (published or "").strip(),
            "guid": (guid or "").strip(),
        })

    return items


def matches_keyword(item: dict) -> bool:
    if not KEYWORD:
        return True

    haystack = " ".join([
        item.get("title", ""),
        item.get("description", ""),
        item.get("link", ""),
    ]).lower()
    return KEYWORD in haystack


def is_real_job(item: dict) -> bool:
    title = (item.get("title") or "").strip().lower()
    if not title:
        return False
    if "more jobs available on career section" in title:
        return False
    return True


def make_id(item: dict) -> str:
    return item.get("guid") or item.get("link") or item.get("title")


def telegram_send(message_html: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        log(f"Telegram response: {body[:500]}")


def escape_html(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def convert_date(date_str: str) -> str:
    if not date_str:
        return ""

    normalized = " ".join(date_str.split())

    for fmt in ("%b %d, %Y", "%b %e, %Y"):
        try:
            dt = datetime.strptime(normalized, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return date_str


def extract_field(description: str, field_name: str) -> str:
    pattern = rf"^{re.escape(field_name)}\s*:\s*(.+)$"
    match = re.search(pattern, description, re.IGNORECASE | re.MULTILINE)
    if match:
        return match.group(1).strip()
    return ""


def clean_un_title(title: str) -> str:
    title = re.sub(r"\s*\(.*?\)", "", title)
    title = re.sub(r",\s*[A-Z]\d.*$", "", title)
    return title.strip()


def parse_taleo_title(title: str) -> tuple[str, str]:
    match = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", title.strip())
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return title.strip(), ""


def extract_un_agency(description: str) -> str:
    office = extract_field(description, "Department/Office")
    if office:
        return office
    return "United Nations"


def extract_taleo_dates(description: str, published: str) -> tuple[str, str]:
    start_date = ""
    end_date = ""

    try:
        if published:
            dt = parsedate_to_datetime(published)
            start_date = dt.strftime("%Y-%m-%d")
    except Exception:
        start_date = published

    patterns = [
        r"Closing Date[:\s]+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
        r"Deadline[:\s]+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
        r"Closing date[:\s]+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
    ]
    for p in patterns:
        m = re.search(p, description, re.IGNORECASE)
        if m:
            end_date = convert_date(m.group(1))
            break

    return start_date, end_date


def build_un_message(item: dict) -> str:
    title_raw = item.get("title", "").strip()
    description = item.get("description", "").strip()
    link = item.get("link", "").strip()

    agency = extract_un_agency(description)
    title = clean_un_title(title_raw)
    job_id = extract_field(description, "Job ID")
    level = extract_field(description, "Level")
    location = extract_field(description, "Duty Station")
    start_date = convert_date(extract_field(description, "Date Posted"))
    end_date = convert_date(extract_field(description, "Deadline"))

    parts = [
        "<b>[UN Careers]</b>",
        escape_html(agency),
        escape_html(title or title_raw),
    ]

    if job_id:
        parts.append(f"ID: {escape_html(job_id)}")
    if level:
        parts.append(f"Level: {escape_html(level)}")
    if location:
        parts.append(f"Location: {escape_html(location)}")
    if start_date:
        parts.append(f"Start: {escape_html(start_date)}")
    if end_date:
        parts.append(f"End: {escape_html(end_date)}")
    if link:
        parts.append(f'<a href="{escape_html(link)}">URL</a>')

    return "\n".join(parts)


def build_taleo_message(item: dict) -> str:
    title_raw = item.get("title", "").strip()
    description = item.get("description", "").strip()
    published = item.get("published", "").strip()

    title, level = parse_taleo_title(title_raw)
    start_date, end_date = extract_taleo_dates(description, published)

    parts = [
        "<b>[IAEA]</b>",
        escape_html(title),
    ]

    if level:
        parts.append(f"Level: {escape_html(level)}")
    if start_date:
        parts.append(f"Start: {escape_html(start_date)}")
    if end_date:
        parts.append(f"End: {escape_html(end_date)}")

    return "\n".join(parts)


def build_message(item: dict) -> str:
    if SOURCE_LABEL.lower() == "iaea":
        return build_taleo_message(item)
    return build_un_message(item)


def main() -> int:
    log(f"Fetching feed: {FEED_URL}")

    try:
        xml_bytes = fetch(FEED_URL)
    except Exception as e:
        log(f"Failed to fetch feed: {e}")
        return 1

    try:
        items = parse_rss(xml_bytes)
    except Exception as e:
        preview = xml_bytes[:500].decode("utf-8", errors="replace")
        log(f"Failed to parse feed: {e}")
        log(f"Feed preview: {preview}")
        return 1

    log(f"Fetched items: {len(items)}")

    if not items:
        log("No items found in feed.")
        return 0

    filtered = [x for x in items if is_real_job(x) and matches_keyword(x)]
    filtered.sort(key=parse_date, reverse=True)

    log(f"Matched items: {len(filtered)}")

    state = load_state()
    seen_ids = set(state.get("seen_ids", []))

    new_items = []
    for item in filtered:
        item_id = make_id(item)
        if item_id and item_id not in seen_ids:
            new_items.append(item)

    if not new_items:
        log("No new matching jobs.")
        return 0

    alerts_sent = 0
    new_ids = []

    for item in new_items[:MAX_ALERTS_PER_RUN]:
        item_id = make_id(item)
        try:
            telegram_send(build_message(item))
            alerts_sent += 1
            new_ids.append(item_id)
            time.sleep(1)
        except Exception as e:
            log(f"Failed to send Telegram message: {e}")

    merged = list(dict.fromkeys(new_ids + state.get("seen_ids", [])))[:1000]
    save_state({"seen_ids": merged})

    log(f"Alerts sent: {alerts_sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
