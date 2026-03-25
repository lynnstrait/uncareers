import html
import re
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime

import feedparser


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; JobWatcher/3.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("&#13;", "\n")
    text = text.replace("\r", "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\n+", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    replacements = {
        "�셲": "’s",
        "�": "'",
        "\xa0": " ",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)

    return text.strip()


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def format_dot_date(date_str: str) -> str:
    if not date_str:
        return ""

    normalized = " ".join(date_str.split())

    try:
        dt = parsedate_to_datetime(normalized)
        return dt.strftime("%Y. %m. %d.")
    except Exception:
        pass

    for fmt in (
        "%b %d, %Y",
        "%B %d, %Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(normalized, fmt)
            return dt.strftime("%Y. %m. %d.")
        except ValueError:
            pass

    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", normalized)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}. {mm}. {dd}."

    return date_str


def parse_any_date_to_ts(value: str) -> float:
    if not value:
        return 0.0

    normalized = " ".join(value.split())

    try:
        return parsedate_to_datetime(normalized).timestamp()
    except Exception:
        pass

    for fmt in (
        "%b %d, %Y",
        "%B %d, %Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(normalized, fmt).timestamp()
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

    return parse_rss_fallback(xml_bytes)
