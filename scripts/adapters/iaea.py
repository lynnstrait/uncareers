import re
import feedparser
from scripts.common.models import JobItem
from scripts.common.helpers import fetch, strip_html, format_dot_date, escape_html

def parse_rss(xml_bytes: bytes) -> list[dict]:
    feed = feedparser.parse(xml_bytes)
    items = []
    for entry in feed.entries:
        items.append({
            "title": (getattr(entry, "title", "") or "").strip(),
            "link": (getattr(entry, "link", "") or "").strip(),
            "description": strip_html(getattr(entry, "summary", "") or getattr(entry, "description", "")),
            "published": (getattr(entry, "published", "") or getattr(entry, "updated", "") or "").strip(),
            "guid": (getattr(entry, "id", "") or getattr(entry, "guid", "") or "").strip(),
        })
    return items

class IAEAAdapter:
    source_name = "IAEA"

    def __init__(self, source_url: str):
        self.source_url = source_url

    def clean_title(self, title: str) -> tuple[str, str]:
        m = re.search(r"\((P\d+|G\d+|NO?[A-Z]?\d+|FS\d+)\)\s*(MULT)?\s*$", title, re.I)
        if m:
            return title[:m.start()].strip(), m.group(1).upper()
        return title.strip(), ""

    def extract_duration(self, description: str) -> str:
        for p in (r"Duration\s*\n\s*([^\n]+)", r"Duration[:\s]+([^\n]+)"):
            m = re.search(p, description, re.I)
            if m:
                return m.group(1).strip()
        return ""

    def extract_closing(self, description: str) -> str:
        patterns = [
            r"Closing Date[:\s]+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
            r"Deadline[:\s]+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
            r"Closing Date[:\s]+(\d{4}-\d{2}-\d{2})",
        ]
        for p in patterns:
            m = re.search(p, description, re.I)
            if m:
                return m.group(1)
        return ""

    def extract_competitive(self, description: str) -> str:
        for p in (r"Full Competitive Recruitment[:\s]+([^\n]+)", r"Competitive[:\s]+([^\n]+)"):
            m = re.search(p, description, re.I)
            if m:
                return m.group(1).strip()
        return ""

    def extract_page_field(self, page_text: str, field_name: str) -> str:
        m = re.search(rf"{re.escape(field_name)}\s*:\s*(.*?)(?:\n|$)", page_text, re.I)
        return m.group(1).strip() if m else ""

    def fetch_detail_fields(self, link: str) -> dict:
        if not link:
            return {}
        try:
            page_text = strip_html(fetch(link, timeout=30).decode("utf-8", errors="replace"))
        except Exception:
            return {}
        return {
            "open": self.extract_page_field(page_text, "Job Posting"),
            "closing": self.extract_page_field(page_text, "Closing Date"),
            "duration": self.extract_page_field(page_text, "Duration in Months"),
            "competitive": self.extract_page_field(page_text, "Full Competitive Recruitment"),
        }

    def fetch_jobs(self) -> list[JobItem]:
        items = parse_rss(fetch(self.source_url))
        jobs = []
        for item in items:
            title, level = self.clean_title(item["title"])
            detail = self.fetch_detail_fields(item["link"])
            duration = detail.get("duration") or self.extract_duration(item["description"])
            competitive = detail.get("competitive") or self.extract_competitive(item["description"])
            open_date = detail.get("open") or item["published"]
            closing_date = detail.get("closing") or self.extract_closing(item["description"])
            jobs.append(JobItem(
                id=item.get("guid") or item["link"] or item["title"],
                source=self.source_name,
                title=title,
                link=item["link"],
                description=item["description"],
                published=item["published"],
                level=level,
                duration=duration,
                competitive=competitive,
                open_date=open_date,
                closing_date=closing_date,
                raw_date=closing_date or item["published"],
            ))
        return jobs

    def build_message(self, job: JobItem) -> str:
        parts = [f"<b>{escape_html(self.source_name)}</b>", "", f"<b>{escape_html(job.title)}</b>"]
        if job.level: parts.append(f"Level: {escape_html(job.level)}")
        if job.duration: parts.append(f"Duration: {escape_html(job.duration)}")
        if job.competitive: parts.append(f"Competitive: {escape_html(job.competitive)}")
        if job.open_date: parts.append(f"Open: {escape_html(format_dot_date(job.open_date))}")
        if job.closing_date: parts.append(f"Closing: {escape_html(format_dot_date(job.closing_date))}")
        if job.link: parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')
        return "\n".join(parts)
