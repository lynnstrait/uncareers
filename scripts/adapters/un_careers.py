import feedparser
from scripts.common.models import JobItem
from scripts.common.helpers import fetch, strip_html, escape_html, format_dot_date

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

def extract_field(description: str, field_name: str) -> str:
    import re
    m = re.search(rf"{re.escape(field_name)}\s*:\s*(.*?)(?:\n|$)", description, re.I)
    if not m:
        return ""
    value = m.group(1).strip()
    return "" if value.lower() == "undefined" else value

class UNCareersAdapter:
    source_name = "UN Careers"

    def __init__(self, source_url: str, location_filters: list[str] | None = None):
        self.source_url = source_url
        self.location_filters = [x.upper() for x in (location_filters or [])]

    def fetch_jobs(self) -> list[JobItem]:
        items = parse_rss(fetch(self.source_url))
        jobs = []
        for item in items:
            description = item["description"]
            location = extract_field(description, "Duty Station")
            if self.location_filters and location.strip().upper() not in self.location_filters:
                continue
            jobs.append(JobItem(
                id=item.get("guid") or item["link"] or item["title"],
                source=self.source_name,
                title=item["title"],
                link=item["link"],
                description=description,
                published=item["published"],
                location=location,
                level=extract_field(description, "Level"),
                department=extract_field(description, "Department/Office"),
                open_date=extract_field(description, "Posted Date"),
                closing_date=extract_field(description, "Deadline"),
                raw_date=extract_field(description, "Deadline") or item["published"],
            ))
        return jobs

    def build_message(self, job: JobItem) -> str:
        parts = [f"<b>{escape_html(self.source_name)}</b>", "", f"<b>{escape_html(job.title)}</b>"]
        if job.location: parts.append(f"Location: {escape_html(job.location)}")
        if job.level: parts.append(f"Level: {escape_html(job.level)}")
        if job.department: parts.append(f"Dept: {escape_html(job.department)}")
        if job.open_date: parts.append(f"Open: {escape_html(format_dot_date(job.open_date))}")
        if job.closing_date: parts.append(f"Closing: {escape_html(format_dot_date(job.closing_date))}")
        if job.link: parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')
        return "\n".join(parts)
