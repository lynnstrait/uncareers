import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

FEED_URL = os.environ["FEED_URL"].strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

# 기본값: Vienna
KEYWORD = os.environ.get("KEYWORD", "VIENNA").strip().lower()
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "10"))

STATE_FILE = Path("data/seen_jobs.json")


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; UNJobWatcher/1.0)",
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
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_date(item: dict) -> float:
    for key in ("published", "pubDate", "updated"):
        value = item.get(key)
        if value:
            try:
                return parsedate_to_datetime(value).timestamp()
            except Exception:
                pass
    return 0.0


def parse_rss(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items = []

    # RSS
    for item in root.findall(".//item"):
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "description": strip_html(item.findtext("description") or ""),
            "published": (item.findtext("pubDate") or "").strip(),
            "guid": (item.findtext("guid") or "").strip(),
        })

    # Atom fallback
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns):
            link = ""
            for l in entry.findall("atom:link", ns):
                href = l.attrib.get("href", "").strip()
                rel = l.attrib.get("rel", "alternate")
                if href and rel == "alternate":
                    link = href
                    break
            items.append({
                "title": (entry.findtext("atom:title", default="", namespaces=ns) or "").strip(),
                "link": link,
                "description": strip_html(entry.findtext("atom:summary", default="", namespaces=ns) or ""),
                "published": (entry.findtext("atom:published", default="", namespaces=ns) or
                              entry.findtext("atom:updated", default="", namespaces=ns) or "").strip(),
                "guid": (entry.findtext("atom:id", default="", namespaces=ns) or "").strip(),
            })

    return items


def matches_keyword(item: dict) -> bool:
    haystack = " ".join([
        item.get("title", ""),
        item.get("description", ""),
        item.get("link", ""),
    ]).lower()
    return KEYWORD in haystack


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
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def build_message(item: dict) -> str:
    title = escape_html(item.get("title", "Untitled"))
    link = escape_html(item.get("link", ""))
    desc = escape_html(item.get("description", ""))[:500]
    published = escape_html(item.get("published", ""))

    parts = [
        "🆕 <b>UN Careers Vienna job alert</b>",
        f"<b>{title}</b>",
    ]
    if published:
        parts.append(f"Date: {published}")
    if desc:
        parts.append(desc)
    if link:
        parts.append(f'<a href="{link}">Open job posting</a>')

    return "\n".join(parts)


def main() -> int:
    log(f"Fetching feed: {FEED_URL}")
    xml_bytes = fetch(FEED_URL)
    items = parse_rss(xml_bytes)
    log(f"Fetched items: {len(items)}")

    if not items:
        log("No items found in feed.")
        return 0

    filtered = [x for x in items if matches_keyword(x)]
    filtered.sort(key=parse_date, reverse=True)

    log(f"Matched keyword '{KEYWORD}': {len(filtered)}")

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

    # seen 목록 길이 제한
    merged = list(dict.fromkeys(new_ids + state.get("seen_ids", [])))[:1000]
    save_state({"seen_ids": merged})

    log(f"Alerts sent: {alerts_sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
