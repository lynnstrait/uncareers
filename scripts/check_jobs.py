import json
import os
import re
import sys
import time
import html
import urllib.parse
import urllib.request
import urllib.error

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser


# =========================================================
# Environment
# =========================================================

SOURCE_ADAPTER = os.environ["SOURCE_ADAPTER"].strip().lower()
SOURCE_LABEL = os.environ.get("SOURCE_LABEL", SOURCE_ADAPTER.upper()).strip()
SOURCE_URL = os.environ["SOURCE_URL"].strip()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

RAW_KEYWORD = os.environ.get("KEYWORD", "").strip()
KEYWORDS = [k.strip().lower() for k in RAW_KEYWORD.split(",") if k.strip()]

MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "10"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "data/seen_jobs.json"))

UNIDO_LOCATION_FILTER = os.environ.get("UNIDO_LOCATION_FILTER", "Vienna, Austria").strip().lower()
CTBTO_LOCATION_FILTER = os.environ.get("CTBTO_LOCATION_FILTER", "Vienna").strip().lower()

DISABLE_WEB_PAGE_PREVIEW = os.environ.get("DISABLE_WEB_PAGE_PREVIEW", "false").strip().lower()
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
BOOTSTRAP_MODE = os.environ.get("BOOTSTRAP_MODE", "false").strip().lower() == "true"


# =========================================================
# Logging / IO
# =========================================================

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


# =========================================================
# Helpers
# =========================================================

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


def escape_html(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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

    log("feedparser returned 0 items, trying fallback parser...")
    fallback_items = parse_rss_fallback(xml_bytes)
    log(f"Fallback parser items: {len(fallback_items)}")
    return fallback_items


def extract_field(description: str, field_name: str) -> str:
    pattern = rf"{re.escape(field_name)}\s*:\s*(.*?)(?:\n|$)"
    m = re.search(pattern, description, re.IGNORECASE)
    if m:
        value = m.group(1).strip()
        if value.lower() == "undefined":
            return ""
        return value
    return ""


def clean_link(link: str) -> str:
    if not link:
        return ""
    return link.split("#", 1)[0].strip()


# =========================================================
# Job model
# =========================================================

@dataclass
class JobItem:
    id: str
    source: str
    title: str
    link: str = ""
    description: str = ""
    published: str = ""
    location: str = ""
    level: str = ""
    department: str = ""
    category: str = ""
    duration: str = ""
    competitive: str = ""
    appointment_type: str = ""
    open_date: str = ""
    closing_date: str = ""
    raw_date: str = ""

    def sort_ts(self) -> float:
        for candidate in (
            self.raw_date,
            self.closing_date,
            self.published,
            self.open_date,
        ):
            ts = parse_any_date_to_ts(candidate)
            if ts > 0:
                return ts
        return 0.0

    def haystack(self) -> str:
        return " ".join([
            self.title,
            self.description,
            self.link,
            self.location,
            self.level,
            self.department,
            self.category,
            self.duration,
            self.competitive,
            self.appointment_type,
        ]).lower()


# =========================================================
# Adapter base
# =========================================================

class SourceAdapter(ABC):
    source_name: str = "unknown"

    def __init__(self, source_url: str):
        self.source_url = source_url

    @abstractmethod
    def fetch_jobs(self) -> list[JobItem]:
        raise NotImplementedError

    def matches_keyword(self, job: JobItem) -> bool:
        if not KEYWORDS:
            return True
        haystack = job.haystack()
        return any(keyword in haystack for keyword in KEYWORDS)

    def is_real_job(self, job: JobItem) -> bool:
        title = (job.title or "").strip().lower()
        if not title:
            return False
        if "more jobs available on career section" in title:
            return False
        return True

    @abstractmethod
    def build_message(self, job: JobItem) -> str:
        raise NotImplementedError


# =========================================================
# RSS adapters
# =========================================================

class RSSAdapter(SourceAdapter, ABC):
    def fetch_rss_items(self) -> list[dict]:
        xml_bytes = fetch(self.source_url)
        return parse_rss(xml_bytes)


class UNCareersAdapter(RSSAdapter):
    source_name = "UN Careers"

    def fetch_jobs(self) -> list[JobItem]:
        items = self.fetch_rss_items()
        jobs = []

        for item in items:
            title = (item.get("title") or "").strip()
            description = (item.get("description") or "").strip()
            link = (item.get("link") or "").strip()

            level = extract_field(description, "Level")
            dept = extract_field(description, "Department/Office")
            location = extract_field(description, "Duty Station")
            open_date = extract_field(description, "Posted Date")
            closing_date = extract_field(description, "Deadline")

            job_id = item.get("guid") or link or title
            jobs.append(JobItem(
                id=job_id,
                source=self.source_name,
                title=title,
                link=link,
                description=description,
                published=(item.get("published") or "").strip(),
                location=location,
                level=level,
                department=dept,
                open_date=open_date,
                closing_date=closing_date,
                raw_date=closing_date or (item.get("published") or ""),
            ))

        return jobs

    def build_message(self, job: JobItem) -> str:
        parts = [
            f"<b>{escape_html(self.source_name)}</b>",
            "",
            f"<b>{escape_html(job.title)}</b>",
        ]

        if job.location:
            parts.append(f"Location: {escape_html(job.location)}")
        if job.level:
            parts.append(f"Level: {escape_html(job.level)}")
        if job.department:
            parts.append(f"Dept: {escape_html(job.department)}")
        if job.open_date:
            parts.append(f"Open: {escape_html(format_dot_date(job.open_date))}")
        if job.closing_date:
            parts.append(f"Closing: {escape_html(format_dot_date(job.closing_date))}")
        if job.link:
            parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')

        return "\n".join(parts)


class IAEAAdapter(RSSAdapter):
    source_name = "IAEA"

    def clean_taleo_title(self, title: str) -> tuple[str, str]:
        title = title.strip()
        m = re.search(r"\((P\d+|G\d+|NO?[A-Z]?\d+|FS\d+)\)\s*(MULT)?\s*$", title, re.IGNORECASE)
        if m:
            level = m.group(1).upper()
            title = title[:m.start()].strip()
            return title, level
        return title, ""

    def extract_duration(self, description: str) -> str:
        for pattern in (
            r"Duration\s*\n\s*([^\n]+)",
            r"Duration[:\s]+([^\n]+)",
        ):
            m = re.search(pattern, description, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return ""

    def extract_closing_date(self, description: str) -> str:
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
                return m.group(1)
        return ""

    def extract_competitive(self, description: str) -> str:
        patterns = [
            r"Full Competitive Recruitment[:\s]+([^\n]+)",
            r"Competitive[:\s]+([^\n]+)",
        ]
        for p in patterns:
            m = re.search(p, description, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return ""

    def extract_page_field(self, page_text: str, field_name: str) -> str:
        pattern = rf"{re.escape(field_name)}\s*:\s*(.*?)(?:\n|$)"
        m = re.search(pattern, page_text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return ""

    def fetch_detail_fields(self, link: str) -> dict:
        if not link:
            return {}

        try:
            html_bytes = fetch(link, timeout=30)
            page_text = strip_html(html_bytes.decode("utf-8", errors="replace"))
        except Exception as e:
            log(f"Failed to fetch IAEA detail page: {e}")
            return {}

        return {
            "open": self.extract_page_field(page_text, "Job Posting"),
            "closing": self.extract_page_field(page_text, "Closing Date"),
            "duration": self.extract_page_field(page_text, "Duration in Months"),
            "competitive": self.extract_page_field(page_text, "Full Competitive Recruitment"),
        }

    def fetch_jobs(self) -> list[JobItem]:
        items = self.fetch_rss_items()
        jobs = []

        for item in items:
            title_raw = (item.get("title") or "").strip()
            description = (item.get("description") or "").strip()
            published = (item.get("published") or "").strip()
            link = (item.get("link") or "").strip()

            title, level = self.clean_taleo_title(title_raw)
            detail = self.fetch_detail_fields(link)

            duration = detail.get("duration") or self.extract_duration(description)
            competitive = detail.get("competitive") or self.extract_competitive(description)
            open_date = detail.get("open") or published
            closing_date = detail.get("closing") or self.extract_closing_date(description)

            job_id = item.get("guid") or link or title_raw
            jobs.append(JobItem(
                id=job_id,
                source=self.source_name,
                title=title,
                link=link,
                description=description,
                published=published,
                level=level,
                duration=duration,
                competitive=competitive,
                open_date=open_date,
                closing_date=closing_date,
                raw_date=closing_date or published,
            ))

        return jobs

    def build_message(self, job: JobItem) -> str:
        parts = [
            f"<b>{escape_html(self.source_name)}</b>",
            "",
            f"<b>{escape_html(job.title)}</b>",
        ]

        if job.level:
            parts.append(f"Level: {escape_html(job.level)}")
        if job.duration:
            parts.append(f"Duration: {escape_html(job.duration)}")
        if job.competitive:
            parts.append(f"Competitive: {escape_html(job.competitive)}")
        if job.open_date:
            parts.append(f"Open: {escape_html(format_dot_date(job.open_date))}")
        if job.closing_date:
            parts.append(f"Closing: {escape_html(format_dot_date(job.closing_date))}")
        if job.link:
            parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')

        return "\n".join(parts)


# =========================================================
# UNIDO adapter
# =========================================================

class UNIDOAdapter(SourceAdapter):
    source_name = "UNIDO"

    def normalize_link(self, link: str) -> str:
        return clean_link(link.split("?", 1)[0].strip())

    def normalize_grade(self, value: str) -> str:
        value = normalize_space(value)
        value = value.replace("ISA -", "ISA-").replace("ISA - ", "ISA-")
        return value

    def parse_jobs_from_html(self, html_bytes: bytes) -> list[JobItem]:
        text = html_bytes.decode("utf-8", errors="replace")

        pattern = re.compile(
            r'<a[^>]+href="(?P<link>https://careers\.unido\.org/job/[^"]+|/job/[^"]+)"[^>]*>(?P<title>.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )

        matches = list(pattern.finditer(text))
        if not matches:
            return []

        jobs = []
        seen_urls = set()

        for idx, match in enumerate(matches):
            raw_link = html.unescape(match.group("link")).strip()
            link = urllib.parse.urljoin("https://careers.unido.org", raw_link)
            link = self.normalize_link(link)

            title = strip_html(match.group("title"))
            if not title:
                continue

            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else min(len(text), start + 1200)
            block = strip_html(text[start:end])
            block = normalize_space(block)

            deadline_match = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", block)
            deadline_raw = deadline_match.group(1) if deadline_match else ""

            grade_match = re.search(
                r"\b(ISA\s*-\s*[A-Z0-9]+|ISA-[A-Z0-9]+|[PDGNLFS]\d|D1|D2|Intern)\b",
                block,
                re.IGNORECASE,
            )
            level = self.normalize_grade(grade_match.group(1)) if grade_match else ""

            category = ""
            category_candidates = [
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
            for c in category_candidates:
                if c.lower() in block.lower():
                    category = c
                    break

            location = ""
            if category:
                cat_match = re.search(re.escape(category), block, re.IGNORECASE)
                if cat_match:
                    location = block[:cat_match.start()].strip()
            else:
                temp = block
                if deadline_raw:
                    temp = temp.replace(deadline_raw, "").strip()
                if level:
                    temp = re.sub(re.escape(level) + r"\b", "", temp, flags=re.IGNORECASE).strip()
                location = temp.strip()

            location = normalize_space(location)

            if not location:
                continue
            if location.lower() != UNIDO_LOCATION_FILTER:
                continue
            if link in seen_urls:
                continue

            seen_urls.add(link)
            jobs.append(JobItem(
                id=link,
                source=self.source_name,
                title=title,
                link=link,
                location=location,
                level=level,
                category=category,
                closing_date=deadline_raw,
                raw_date=deadline_raw,
            ))

        return jobs

    def fetch_jobs(self) -> list[JobItem]:
        html_bytes = fetch(self.source_url)
        return self.parse_jobs_from_html(html_bytes)

    def is_real_job(self, job: JobItem) -> bool:
        if not super().is_real_job(job):
            return False
        return "/job/" in (job.link or "").lower()

    def build_message(self, job: JobItem) -> str:
        parts = [
            f"<b>{escape_html(self.source_name)}</b>",
            "",
            f"<b>{escape_html(job.title)}</b>",
        ]

        if job.location:
            parts.append(f"Location: {escape_html(job.location)}")
        if job.level:
            parts.append(f"Level: {escape_html(job.level)}")
        if job.category:
            parts.append(f"Dept: {escape_html(job.category)}")
        if job.closing_date:
            parts.append(f"Closing: {escape_html(format_dot_date(job.closing_date))}")
        if job.link:
            parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')

        return "\n".join(parts)


# =========================================================
# CTBTO adapter
# =========================================================

class CTBTOAdapter(SourceAdapter):
    source_name = "CTBTO"

    def normalize_sf_link(self, link: str) -> str:
        link = html.unescape(link or "").strip()
        if not link:
            return ""
        if link.startswith("/career"):
            link = urllib.parse.urljoin("https://career2.successfactors.eu", link)
        return clean_link(link)

    def extract_query_param(self, url: str, key: str) -> str:
        try:
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            values = query.get(key, [])
            return values[0].strip() if values else ""
        except Exception:
            return ""

    def build_job_id_from_link(self, link: str) -> str:
        req_id = self.extract_query_param(link, "career_job_req_id")
        if req_id:
            return f"ctbto:{req_id}"
        return f"ctbto:{link}"

    def parse_detail_page(self, link: str) -> JobItem | None:
        try:
            html_bytes = fetch(link, timeout=30)
            page_text = strip_html(html_bytes.decode("utf-8", errors="replace"))
        except Exception as e:
            log(f"Failed to fetch CTBTO detail page: {e}")
            return None

        lowered = page_text.lower()
        if "this job cannot be viewed at the moment" in lowered:
            return None
        if "is no longer available for application" in lowered:
            return None

        patterns = {
            "title": [
                r"Job title[:\s]+([^\n]+)",
                r"Title[:\s]+([^\n]+)",
                r"#\s*([^\n]+)",
            ],
            "level": [
                r"Grade Level[:\s]+([^\n]+)",
                r"Grade[:\s]+([^\n]+)",
            ],
            "dept": [
                r"Division[:\s]+([^\n]+)",
                r"Department[:\s]+([^\n]+)",
            ],
            "appointment_type": [
                r"Type of Appointment[:\s]+([^\n]+)",
            ],
            "open_date": [
                r"Date of Issuance[:\s]+([^\n]+)",
                r"Date of Issue[:\s]+([^\n]+)",
                r"Date Posted[:\s]+([^\n]+)",
            ],
            "closing_date": [
                r"Deadline for Applications[:\s]+([^\n]+)",
                r"Deadline[:\s]+([^\n]+)",
                r"Application deadline[:\s]+([^\n]+)",
            ],
        }

        def first_match(candidates: list[str]) -> str:
            for p in candidates:
                m = re.search(p, page_text, re.IGNORECASE)
                if m:
                    return m.group(1).strip()
            return ""

        title = first_match(patterns["title"])
        level = first_match(patterns["level"])
        dept = first_match(patterns["dept"])
        appointment_type = first_match(patterns["appointment_type"])
        open_date = first_match(patterns["open_date"])
        closing_date = first_match(patterns["closing_date"])

        location = "Vienna, Austria" if re.search(r"vienna", page_text, re.IGNORECASE) else ""

        if not title:
            req_id = self.extract_query_param(link, "career_job_req_id")
            title = f"CTBTO Vacancy {req_id}" if req_id else "CTBTO Vacancy"

        return JobItem(
            id=self.build_job_id_from_link(link),
            source=self.source_name,
            title=title,
            link=link,
            description=page_text[:1500],
            location=location,
            level=level,
            department=dept,
            appointment_type=appointment_type,
            open_date=open_date,
            closing_date=closing_date,
            raw_date=closing_date or open_date,
        )

    def parse_embedded_links(self, text: str) -> list[str]:
        links = set()

        for m in re.finditer(
            r'(https://career2\.successfactors\.eu/career\?[^"\s<>]+career_job_req_id=\d+[^"\s<>]*)',
            text,
            re.IGNORECASE,
        ):
            links.add(self.normalize_sf_link(m.group(1)))

        for m in re.finditer(
            r'(/career\?[^"\s<>]+career_job_req_id=\d+[^"\s<>]*)',
            text,
            re.IGNORECASE,
        ):
            links.add(self.normalize_sf_link(m.group(1)))

        for req_id in set(re.findall(r'"career_job_req_id"\s*:\s*"?(\\?\d+)"?', text, re.IGNORECASE)):
            rid = req_id.replace("\\", "")
            link = (
                "https://career2.successfactors.eu/career?"
                f"company=ctbtoprepa&career_ns=job_listing&navBarLevel=JOB_SEARCH"
                f"&career_job_req_id={rid}&selected_lang=en_GB&rcm_site_locale=en_GB"
            )
            links.add(self.normalize_sf_link(link))

        for req_id in set(re.findall(r'career_job_req_id[=:\\/"]+(\d+)', text, re.IGNORECASE)):
            link = (
                "https://career2.successfactors.eu/career?"
                f"company=ctbtoprepa&career_ns=job_listing&navBarLevel=JOB_SEARCH"
                f"&career_job_req_id={req_id}&selected_lang=en_GB&rcm_site_locale=en_GB"
            )
            links.add(self.normalize_sf_link(link))

        return [x for x in links if x]

    def parse_summary_blocks(self, text: str) -> list[JobItem]:
        jobs = []

        block_pattern = re.compile(
            r'(?P<title>There\s+is\s+a\s+position\s+open\s+for\s+.*?)(?=There\s+is\s+a\s+position\s+open\s+for\s+|\Z)',
            re.IGNORECASE | re.DOTALL,
        )

        for block_match in block_pattern.finditer(text):
            block = normalize_space(strip_html(block_match.group(0)))

            title = ""
            m_title = re.search(r"There\s+is\s+a\s+position\s+open\s+for\s+(.+?)\s+at\s+the", block, re.IGNORECASE)
            if m_title:
                title = m_title.group(1).strip()

            level = ""
            m_level = re.search(r"Grade Level[:\s]+([^\n]+?)(?:Division:|Section:|Unit:|Type of Appointment:|Date of Issuance:|Deadline for Applications:)", block, re.IGNORECASE)
            if m_level:
                level = normalize_space(m_level.group(1))

            dept = ""
            m_div = re.search(r"Division[:\s]+([^\n]+?)(?:Section:|Unit:|Type of Appointment:|Date of Issuance:|Deadline for Applications:)", block, re.IGNORECASE)
            if m_div:
                dept = normalize_space(m_div.group(1))

            appointment_type = ""
            m_type = re.search(r"Type of Appointment[:\s]+([^\n]+?)(?:Date of Issuance:|Deadline for Applications:)", block, re.IGNORECASE)
            if m_type:
                appointment_type = normalize_space(m_type.group(1))

            open_date = ""
            m_open = re.search(r"Date of Issuance[:\s]+([^\n]+?)(?:Deadline for Applications:|Vacancy Reference|Reporting Date:)", block, re.IGNORECASE)
            if m_open:
                open_date = normalize_space(m_open.group(1))

            closing_date = ""
            m_close = re.search(r"Deadline for Applications[:\s]+([^\n]+?)(?:Vacancy Reference|Reporting Date:|Please note)", block, re.IGNORECASE)
            if m_close:
                closing_date = normalize_space(m_close.group(1))

            req_id = ""
            m_req = re.search(r"VA ID[:\s]+(\d+)", block, re.IGNORECASE)
            if m_req:
                req_id = m_req.group(1).strip()

            link = ""
            m_link = re.search(r"(https://career2\.successfactors\.eu/career\?[^ ]+career_job_req_id=\d+[^ ]*)", block, re.IGNORECASE)
            if m_link:
                link = self.normalize_sf_link(m_link.group(1))
            elif req_id:
                link = self.normalize_sf_link(
                    "https://career2.successfactors.eu/career?"
                    f"company=ctbtoprepa&career_ns=job_listing&navBarLevel=JOB_SEARCH"
                    f"&career_job_req_id={req_id}&selected_lang=en_GB&rcm_site_locale=en_GB"
                )

            if not title:
                continue

            jobs.append(JobItem(
                id=f"ctbto:{req_id}" if req_id else f"ctbto:{title}",
                source=self.source_name,
                title=title,
                link=link,
                description=block[:1500],
                location="Vienna, Austria",
                level=level,
                department=dept,
                appointment_type=appointment_type,
                open_date=open_date,
                closing_date=closing_date,
                raw_date=closing_date or open_date,
            ))

        return jobs

    def fetch_jobs(self) -> list[JobItem]:
        html_bytes = fetch(self.source_url)
        text = html_bytes.decode("utf-8", errors="replace")

        links = self.parse_embedded_links(text)
        jobs = []

        if links:
            log(f"CTBTO discovered detail links: {len(links)}")
            seen_ids = set()
            for link in links:
                job = self.parse_detail_page(link)
                if not job:
                    continue
                if CTBTO_LOCATION_FILTER and CTBTO_LOCATION_FILTER not in (job.location or "").lower():
                    continue
                if job.id in seen_ids:
                    continue
                seen_ids.add(job.id)
                jobs.append(job)

            if jobs:
                return jobs

        return self.parse_summary_blocks(text)

    def is_real_job(self, job: JobItem) -> bool:
        if not super().is_real_job(job):
            return False

        title = (job.title or "").strip().lower()
        if title == "loading":
            return False
        if title == "career opportunities":
            return False
        return True

    def build_message(self, job: JobItem) -> str:
        parts = [
            f"<b>{escape_html(self.source_name)}</b>",
            "",
            f"<b>{escape_html(job.title)}</b>",
        ]

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


# =========================================================
# Adapter registry
# =========================================================

def get_adapter(name: str, source_url: str) -> SourceAdapter:
    registry: dict[str, type[SourceAdapter]] = {
        "iaea": IAEAAdapter,
        "un_careers": UNCareersAdapter,
        "unido": UNIDOAdapter,
        "ctbto": CTBTOAdapter,
    }

    adapter_cls = registry.get(name.lower())
    if not adapter_cls:
        raise ValueError(f"Unsupported SOURCE_ADAPTER: {name}")

    return adapter_cls(source_url)


# =========================================================
# Main pipeline
# =========================================================

def main() -> int:
    log(f"Source adapter: {SOURCE_ADAPTER}")
    log(f"Source label: {SOURCE_LABEL}")
    log(f"Source URL: {SOURCE_URL}")
    log(f"State file: {STATE_FILE}")
    log(f"DRY_RUN={DRY_RUN}")
    log(f"BOOTSTRAP_MODE={BOOTSTRAP_MODE}")

    try:
        adapter = get_adapter(SOURCE_ADAPTER, SOURCE_URL)
    except Exception as e:
        log(f"Failed to initialize adapter: {e}")
        return 1

    try:
        jobs = adapter.fetch_jobs()
    except Exception as e:
        log(f"Failed to fetch jobs: {e}")
        return 1

    log(f"Fetched jobs: {len(jobs)}")

    if not jobs:
        log("No jobs found.")
        return 0

    jobs = [job for job in jobs if adapter.is_real_job(job) and adapter.matches_keyword(job)]
    jobs.sort(key=lambda x: x.sort_ts(), reverse=True)

    log(f"Matched jobs: {len(jobs)}")

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
        try:
            telegram_send(adapter.build_message(job))
            alerts_sent += 1
            new_ids.append(job.id)
            time.sleep(1)
        except Exception as e:
            log(f"Failed to send Telegram message: {e}")

    merged = list(dict.fromkeys(new_ids + state.get("seen_ids", [])))[:1000]
    save_state({"seen_ids": merged})

    log(f"Alerts sent: {alerts_sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
