import json
import os
import re
import sys
import time
import html
import urllib.parse
import urllib.request
import urllib.error
from email.utils import parsedate_to_datetime
from pathlib import Path
from datetime import datetime

import feedparser

FEED_URL = os.environ["FEED_URL"].strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

RAW_KEYWORD = os.environ.get("KEYWORD", "").strip()
KEYWORDS = [k.strip().lower() for k in RAW_KEYWORD.split(",") if k.strip()]
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
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\n+", "\n", text)

    replacements = {
        "�셲": "’s",
        "�": "'",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)

    return text.strip()


def escape_html(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def parse_date(item: dict) -> float:
    value = item.get("published", "") or item.get("updated", "")
    if value:
        try:
            return parsedate_to_datetime(value).timestamp()
        except Exception:
            pass
    return 0.0


def extract_tag(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", block, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return strip_html(m.group(1)).strip()


def parse_rss_fallback(xml_bytes: bytes) -> list[dict]:
    text = xml_bytes.decode("utf-8", errors="replace")

    items = []
    for block in re.findall(r"<item\b.*?>.*?</item>", text, re.IGNORECASE | re.DOTALL):
        title = extract_tag(block, "title")
        link = extract_tag(block, "link")
        description = extract_tag(block, "description")
        pub_date = extract_tag(block, "pubDate")
        guid = extract_tag(block, "guid")

        if title or link:
            items.append({
                "title": title,
                "link": html.unescape(link),
                "description": description,
                "published": pub_date,
                "guid": guid,
            })

    return items


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

    if items:
        return items

    log("feedparser returned 0 items, trying fallback parser...")
    fallback_items = parse_rss_fallback(xml_bytes)
    log(f"Fallback parser items: {len(fallback_items)}")
    return fallback_items


def matches_keyword(item: dict) -> bool:
    if not KEYWORDS:
        return True

    haystack = " ".join([
        item.get("title", ""),
        item.get("description", ""),
        item.get("link", ""),
    ]).lower()

    return any(keyword in haystack for keyword in KEYWORDS)


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

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            log(f"Telegram response: {body[:500]}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        log(f"Telegram HTTPError {e.code}: {error_body}")
        raise


# -------------------------
# UN Careers helpers
# -------------------------

def format_un_dot_date(date_str: str) -> str:
    if not date_str:
        return ""

    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", date_str)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}. {mm}. {dd}."

    return date_str


def extract_un_field(description: str, field_name: str) -> str:
    pattern = rf"{re.escape(field_name)}\s*:\s*(.*?)(?:\n|$)"
    match = re.search(pattern, description, re.IGNORECASE)
    if match:
        value = match.group(1).strip()
        if value.lower() == "undefined":
            return ""
        return value
    return ""


def build_un_message(item: dict) -> str:
    title = (item.get("title") or "").strip()
    description = (item.get("description") or "").strip()
    link = (item.get("link") or "").strip()

    level = extract_un_field(description, "Level")
    dept = extract_un_field(description, "Department/Office")
    location = extract_un_field(description, "Duty Station")
    open_date = format_un_dot_date(extract_un_field(description, "Posted Date"))
    closing_date = format_un_dot_date(extract_un_field(description, "Deadline"))

    parts = [
        "<b>UN Careers</b>",
        "",
        f"<b>{escape_html(title)}</b>",
    ]

    if level:
        parts.append(f"<code>Level: {escape_html(level)}</code>")
    if dept:
        parts.append(f"<code>Dept: {escape_html(dept)}</code>")
    if location:
        parts.append(f"<code>Location: {escape_html(location)}</code>")
    if open_date:
        parts.append(f"<code>Open: {escape_html(open_date)}</code>")
    if closing_date:
        parts.append(f"<code>Closing: {escape_html(closing_date)}</code>")
    if link:
        parts.append(f'<a href="{escape_html(link)}">Job Open</a>')

    return "\n".join(parts)


# -------------------------
# IAEA helpers
# -------------------------

def format_dot_date(date_str: str) -> str:
    if not date_str:
        return ""

    normalized = " ".join(date_str.split())

    try:
        dt = parsedate_to_datetime(normalized)
        return dt.strftime("%Y. %m. %d.")
    except Exception:
        pass

    for fmt in ("%b %d, %Y", "%b %e, %Y"):
        try:
            dt = datetime.strptime(normalized, fmt)
            return dt.strftime("%Y. %m. %d.")
        except ValueError:
            pass

    # 상세 페이지 스타일: 2026-03-30, 11:59:00 PM
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", normalized)
    if m:
        yyyy, mm, dd = m.groups()
        return f"{yyyy}. {mm}. {dd}."

    return date_str


def clean_taleo_title(title: str) -> tuple[str, str]:
    title = title.strip()

    level = ""
    m = re.search(r"\((P\d+|G\d+|NO?[A-Z]?\d+|FS\d+)\)\s*(MULT)?\s*$", title, re.IGNORECASE)
    if m:
        level = m.group(1).upper()
        title = title[:m.start()].strip()
        return title, level

    m = re.search(r"\((P\d+|G\d+|NO?[A-Z]?\d+|FS\d+)\)\s*$", title, re.IGNORECASE)
    if m:
        level = m.group(1).upper()
        title = title[:m.start()].strip()
        return title, level

    return title, ""


def extract_duration(description: str) -> str:
    m = re.search(r"Duration\s*\n\s*([^\n]+)", description, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    m = re.search(r"Duration[:\s]+([^\n]+)", description, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return ""


def extract_closing_date(description: str) -> str:
    patterns = [
        r"Closing Date[:\s]+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
        r"Closing date[:\s]+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
        r"Deadline[:\s]+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
        r"Closing Date[:\s]+(\d{4}-\d{2}-\d{2})",
        r"Closing date[:\s]+(\d{4}-\d{2}-\d{2})",
    ]
    for p in patterns:
        m = re.search(p, description, re.IGNORECASE)
        if m:
            return format_dot_date(m.group(1))
    return ""


def extract_competitive(description: str) -> str:
    patterns = [
        r"Full Competitive Recruitment[:\s]+([^\n]+)",
        r"Competitive[:\s]+([^\n]+)",
    ]
    for p in patterns:
        m = re.search(p, description, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def build_iaea_message(item: dict) -> str:
    title_raw = (item.get("title") or "").strip()
    description = (item.get("description") or "").strip()
    published = (item.get("published") or "").strip()
    link = (item.get("link") or "").strip()

    title, level = clean_taleo_title(title_raw)
    duration = extract_duration(description)
    competitive = extract_competitive(description)
    open_date = format_dot_date(published)
    closing_date = extract_closing_date(description)

    parts = [
        "<b>IAEA</b>",
        "",
        f"<b>{escape_html(title)}</b>",
    ]

    if level:
        parts.append(f"<code>Level: {escape_html(level)}</code>")
    if duration:
        parts.append(f"<code>Duration: {escape_html(duration)}</code>")
    if competitive:
        parts.append(f"<code>Competitive: {escape_html(competitive)}</code>")
    if open_date:
        parts.append(f"<code>Open: {escape_html(open_date)}</code>")
    if closing_date:
        parts.append(f"<code>Closing: {escape_html(closing_date)}</code>")
    if link:
        parts.append(f'<a href="{escape_html(link)}">Job Open</a>')

    return "\n".join(parts)


def build_message(item: dict) -> str:
    if SOURCE_LABEL.strip().lower() == "iaea":
        return build_iaea_message(item)
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
