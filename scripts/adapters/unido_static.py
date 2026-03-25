import html
import re
import urllib.parse
from scripts.common.models import JobItem
from scripts.common.helpers import fetch, strip_html, normalize_space, escape_html, format_dot_date

class UNIDOStaticAdapter:
    source_name = "UNIDO"

    def __init__(self, source_url: str, location_filter: str = "vienna, austria"):
        self.source_url = source_url
        self.location_filter = location_filter.lower()

    def normalize_link(self, link: str) -> str:
        return (link or "").split("?", 1)[0].strip()

    def normalize_grade(self, value: str) -> str:
        value = normalize_space(value)
        return value.replace("ISA -", "ISA-").replace("ISA - ", "ISA-")

    def fetch_jobs(self) -> list[JobItem]:
        text = fetch(self.source_url).decode("utf-8", errors="replace")
        pattern = re.compile(r'<a[^>]+href="(?P<link>https://careers\.unido\.org/job/[^"]+|/job/[^"]+)"[^>]*>(?P<title>.*?)</a>', re.I|re.S)
        matches = list(pattern.finditer(text))
        jobs = []
        seen = set()
        cats = ["International Professionals","General Service","Consultancy opportunities","Internship Programme","Project Appointments","Junior Professional Officer Programme(JPO)","Junior Professional Officer Programme","National Professional officers","Local Support Personnel"]
        for idx, match in enumerate(matches):
            raw_link = html.unescape(match.group("link")).strip()
            link = urllib.parse.urljoin("https://careers.unido.org", raw_link)
            link = self.normalize_link(link)
            title = normalize_space(strip_html(match.group("title")))
            if not title:
                continue
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else min(len(text), start + 1500)
            block = normalize_space(strip_html(text[start:end]))
            mloc = re.search(r"\b([A-Za-z .'\-]+,\s*[A-Za-z .'\-]+)\b", block)
            location = normalize_space(mloc.group(1)) if mloc else ""
            if location.lower() != self.location_filter:
                continue
            cat = ""
            for c in cats:
                if c.lower() in block.lower():
                    cat = c
                    break
            mlevel = re.search(r"\b(ISA\s*-\s*[A-Z0-9]+|ISA-[A-Z0-9]+|[PDGNLFS]\d|D1|D2|Intern)\b", block, re.I)
            level = self.normalize_grade(mlevel.group(1)) if mlevel else ""
            mdeadline = re.search(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4})\b", block)
            deadline = mdeadline.group(1) if mdeadline else ""
            key = (title.lower(), location.lower(), level.lower(), deadline.lower())
            if key in seen:
                continue
            seen.add(key)
            jobs.append(JobItem(id=f"unido:{title}|{location}|{level}|{deadline}", source=self.source_name, title=title, link=link, location=location, level=level, category=cat, closing_date=deadline, raw_date=deadline))
        return jobs

    def build_message(self, job: JobItem) -> str:
        parts = [f"<b>{escape_html(self.source_name)}</b>", "", f"<b>{escape_html(job.title)}</b>"]
        if job.location: parts.append(f"Location: {escape_html(job.location)}")
        if job.level: parts.append(f"Level: {escape_html(job.level)}")
        if job.category: parts.append(f"Dept: {escape_html(job.category)}")
        if job.closing_date: parts.append(f"Closing: {escape_html(format_dot_date(job.closing_date))}")
        if job.link: parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')
        return "\n".join(parts)
