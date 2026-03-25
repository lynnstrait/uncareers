import re

from scripts.common.helpers import fetch, strip_html, normalize_space, format_dot_date
from scripts.common.models import JobItem
from scripts.adapters.base import RSSAdapter


class IAEAAdapter(RSSAdapter):
    source_name = "IAEA"

    KNOWN_LABELS = {
        "job posting",
        "closing date",
        "duration in months",
        "full competitive recruitment",
        "organizational setting",
        "reporting to",
        "grade",
        "duty station",
        "type/ duration of appointment",
    }

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
        lines = [normalize_space(x) for x in page_text.split("\n")]
        lines = [x for x in lines if x]

        for i, line in enumerate(lines):
            if line.lower() == field_name.lower():
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if next_line.lower() in self.KNOWN_LABELS:
                        return ""
                    return next_line

            m = re.match(rf"^{re.escape(field_name)}\s*:\s*(.+)$", line, re.IGNORECASE)
            if m:
                value = m.group(1).strip()
                if value.lower() in self.KNOWN_LABELS:
                    return ""
                return value

        return ""

    def fetch_detail_fields(self, link: str) -> dict:
        if not link:
            return {}

        try:
            html_bytes = fetch(link, timeout=30)
            page_text = strip_html(html_bytes.decode("utf-8", errors="replace"))
        except Exception:
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
            jobs.append(
                JobItem(
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
                )
            )

        return jobs

    def build_message(self, job: JobItem) -> str:
        parts = [
            f"<b>{self.source_name}</b>",
            "",
            f"<b>{job.title}</b>",
        ]

        if job.level:
            parts.append(f"Level: {job.level}")
        if job.duration:
            parts.append(f"Duration: {job.duration}")
        if job.competitive:
            parts.append(f"Competitive: {job.competitive}")
        if job.open_date:
            parts.append(f"Open: {format_dot_date(job.open_date)}")
        if job.closing_date:
            parts.append(f"Closing: {format_dot_date(job.closing_date)}")
        if job.link:
            parts.append(f'<a href="{job.link}">Job Open</a>')

        return "\n".join(parts)
