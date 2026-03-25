import html
import os
import re
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime

def log(msg: str) -> None:
    print(msg, flush=True)

def fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; JobWatcher/4.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("&#13;", "\n").replace("\r", "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\n+", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()

def escape_html(s: str) -> str:
    return ((s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def format_dot_date(date_str: str) -> str:
    if not date_str:
        return ""
    normalized = " ".join(date_str.split())
    try:
        dt = parsedate_to_datetime(normalized)
        return dt.strftime("%Y. %m. %d.")
    except Exception:
        pass
    for fmt in ("%b %d, %Y","%B %d, %Y","%d-%b-%Y","%d-%B-%Y","%d/%m/%Y","%m/%d/%Y","%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt).strftime("%Y. %m. %d.")
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
    for fmt in ("%b %d, %Y","%B %d, %Y","%d-%b-%Y","%d-%B-%Y","%d/%m/%Y","%m/%d/%Y","%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt).timestamp()
        except Exception:
            pass
    return 0.0

def clean_link(link: str) -> str:
    return (link or "").split("#", 1)[0].strip()
