import html
import re
import urllib.parse
from scripts.common.models import JobItem
from scripts.common.helpers import fetch, strip_html, escape_html, format_dot_date, clean_link

class CTBTOStaticAdapter:
    source_name = "CTBTO"

    def __init__(self, source_url: str, location_filter: str = ""):
        self.source_url = source_url
        self.location_filter = location_filter.lower()

    def normalize_sf_link(self, link: str) -> str:
        link = html.unescape(link or "").strip()
        if link.startswith("/career"):
            link = urllib.parse.urljoin("https://career2.successfactors.eu", link)
        return clean_link(link)

    def extract_query_param(self, url: str, key: str) -> str:
        try:
            parsed = urllib.parse.urlparse(url)
            q = urllib.parse.parse_qs(parsed.query)
            vals = q.get(key, [])
            return vals[0].strip() if vals else ""
        except Exception:
            return ""

    def build_job_id_from_link(self, link: str) -> str:
        req_id = self.extract_query_param(link, "career_job_req_id")
        return f"ctbto:{req_id}" if req_id else f"ctbto:{link}"

    def parse_detail_page(self, link: str):
        try:
            page_text = strip_html(fetch(link, timeout=30).decode("utf-8", errors="replace"))
        except Exception:
            return None
        if "this job cannot be viewed at the moment" in page_text.lower() or "is no longer available for application" in page_text.lower():
            return None
        def first(patterns):
            for p in patterns:
                m = re.search(p, page_text, re.I)
                if m:
                    return m.group(1).strip()
            return ""
        title = first([r"Job title[:\s]+([^\n]+)", r"Title[:\s]+([^\n]+)", r"#\s*([^\n]+)"]) or "CTBTO Vacancy"
        location = "Vienna, Austria" if re.search(r"vienna", page_text, re.I) else ""
        return JobItem(
            id=self.build_job_id_from_link(link),
            source=self.source_name,
            title=title,
            link=link,
            description=page_text[:1500],
            location=location,
            level=first([r"Grade Level[:\s]+([^\n]+)", r"Grade[:\s]+([^\n]+)"]),
            department=first([r"Division[:\s]+([^\n]+)", r"Department[:\s]+([^\n]+)"]),
            appointment_type=first([r"Type of Appointment[:\s]+([^\n]+)"]),
            open_date=first([r"Date of Issuance[:\s]+([^\n]+)", r"Date of Issue[:\s]+([^\n]+)", r"Date Posted[:\s]+([^\n]+)"]),
            closing_date=first([r"Deadline for Applications[:\s]+([^\n]+)", r"Deadline[:\s]+([^\n]+)", r"Application deadline[:\s]+([^\n]+)"]),
            raw_date=first([r"Deadline for Applications[:\s]+([^\n]+)", r"Date of Issuance[:\s]+([^\n]+)"]),
        )

    def fetch_jobs(self):
        text = fetch(self.source_url).decode("utf-8", errors="replace")
        links = set()
        for m in re.finditer(r'https://career2\.successfactors\.eu/career\?[^"\s<>]+career_job_req_id=\d+[^"\s<>]*', text, re.I):
            links.add(self.normalize_sf_link(m.group(0)))
        for m in re.finditer(r'/career\?[^"\s<>]+career_job_req_id=\d+[^"\s<>]*', text, re.I):
            links.add(self.normalize_sf_link(m.group(0)))
        jobs = []
        seen = set()
        for link in links:
            job = self.parse_detail_page(link)
            if not job:
                continue
            if self.location_filter and self.location_filter not in (job.location or "").lower():
                continue
            if job.id in seen:
                continue
            seen.add(job.id)
            jobs.append(job)
        return jobs

    def build_message(self, job: JobItem) -> str:
        parts = [f"<b>{escape_html(self.source_name)}</b>", "", f"<b>{escape_html(job.title)}</b>"]
        if job.level: parts.append(f"Level: {escape_html(job.level)}")
        if job.department: parts.append(f"Dept: {escape_html(job.department)}")
        if job.appointment_type: parts.append(f"Type: {escape_html(job.appointment_type)}")
        if job.open_date: parts.append(f"Open: {escape_html(format_dot_date(job.open_date))}")
        if job.closing_date: parts.append(f"Closing: {escape_html(format_dot_date(job.closing_date))}")
        if job.link: parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')
        return "\n".join(parts)
