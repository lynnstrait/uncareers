import re

from scripts.adapters.base import RSSAdapter
from scripts.common.helpers import (
    fetch,
    strip_html,
    normalize_space,
    format_dot_date,
    escape_html,
)
from scripts.common.models import JobItem


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
        "remuneration",
    }

    def clean_taleo_title(self, title: str) -> tuple[str, str]:
        title = title.strip()
        m = re.search(r"\((P\d+|G\d+|NO?[A-Z]?\d+|FS\d+)\)\s*(MULT)?\s*$", title, re.IGNORECASE)
        if m:
            level = m.group(1).upper()
            title = title[:m.start()].strip()
            return title, level
        return title, ""

    def extract_duration_from_description(self, description: str) -> str:
        for pattern in (
            r"Duration\s*\n\s*([^\n]+)",
            r"Duration[:\s]+([^\n]+)",
        ):
            m = re.search(pattern, description, re.IGNORECASE)
            if m:
                return normalize_space(m.group(1))
        return ""

    def extract_closing_from_description(self, description: str) -> str:
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
                return normalize_space(m.group(1))
        return ""

    def extract_competitive(self, description: str) -> str:
        patterns = [
            r"Full Competitive Recruitment[:\s]+([^\n]+)",
            r"Competitive[:\s]+([^\n]+)",
        ]
        for p in patterns:
            m = re.search(p, description, re.IGNORECASE)
            if m:
                return normalize_space(m.group(1))
        return ""

    def extract_page_field(self, page_text: str, field_name: str) -> str:
        lines = [normalize_space(x) for x in page_text.split("\n")]
        lines = [x for x in lines if x]

        for i, line in enumerate(lines):
            # Case 1: label on its own line, value on one of the next few lines
            if line.lower() == field_name.lower():
                for j in range(i + 1, min(i + 5, len(lines))):
                    candidate = lines[j].strip()
                    if not candidate or candidate == ":":
                        continue
                    if candidate.lower() in self.KNOWN_LABELS:
                        break
                    return candidate

            # Case 2: "Field Name: value" on one line
            m = re.match(rf"^{re.escape(field_name)}\s*:\s*(.*)$", line, re.IGNORECASE)
            if m:
                value = m.group(1).strip()
                if not value or value == ":":
                    # look ahead a few lines
                    for j in range(i + 1, min(i + 5, len(lines))):
                        candidate = lines[j].strip()
                        if not candidate or candidate == ":":
                            continue
                        if candidate.lower() in self.KNOWN_LABELS:
                            break
                        return candidate
                    return ""
                if value.lower() in self.KNOWN_LABELS:
                    return ""
                return value

        return ""

    def extract_date_after_label(self, page_text: str, field_name: str) -> str:
        """
        More robust fallback for TAL pages where the label/value layout is odd.
        Searches the text region immediately after the field label and extracts
        the first date-looking token.
        """
        patterns = [
            rf"{re.escape(field_name)}[\s:]*([A-Za-z]{{3,9}}\s+\d{{1,2}},\s+\d{{4}})",
            rf"{re.escape(field_name)}[\s:]*([A-Za-z]{{3,9}}\s+\d{{1,2}}\s+\d{{4}})",
            rf"{re.escape(field_name)}[\s:]*([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})",
        ]
        for p in patterns:
            m = re.search(p, page_text, re.IGNORECASE)
            if m:
                return normalize_space(m.group(1))

        idx = page_text.lower().find(field_name.lower())
        if idx >= 0:
            snippet = page_text[idx: idx + 200]
            m = re.search(
                r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}|[A-Za-z]{3,9}\s+\d{1,2}\s+\d{4}|\d{4}-\d{2}-\d{2})",
                snippet,
                re.IGNORECASE,
            )
            if m:
                return normalize_space(m.group(1))

        return ""

    def fetch_detail_fields(self, link: str) -> dict:
        if not link:
            return {}

        try:
            html_bytes = fetch(link, timeout=30)
            page_text = strip_html(html_bytes.decode("utf-8", errors="replace"))
        except Exception:
            return {}

        open_value = self.extract_page_field(page_text, "Job Posting")
        closing_value = self.extract_page_field(page_text, "Closing Date")

        if not open_value or open_value == ":":
            open_value = self.extract_date_after_label(page_text, "Job Posting")

        if not closing_value or closing_value == ":":
            closing_value = self.extract_date_after_label(page_text, "Closing Date")

        return {
            "open": open_value,
            "closing": closing_value,
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

            title, level_from_title = self.clean_taleo_title(title_raw)
            detail = self.fetch_detail_fields(link)

            duration = detail.get("duration") or self.extract_duration_from_description(description)
            open_date = detail.get("open") or published
            closing_date = detail.get("closing") or self.extract_closing_from_description(description)

            job_id = item.get("guid") or link or title_raw
            jobs.append(
                JobItem(
                    id=job_id,
                    source=self.source_name,
                    title=title,
                    link=link,
                    description=description,
                    published=published,
                    level=level_from_title,
                    duration=duration,
                    open_date=open_date,
                    closing_date=closing_date,
                    raw_date=closing_date or open_date or published,
                )
            )

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
        if job.open_date:
            parts.append(f"Open: {escape_html(format_dot_date(job.open_date))}")
        if job.closing_date:
            parts.append(f"Closing: {escape_html(format_dot_date(job.closing_date))}")
        if job.link:
            parts.append(f'<a href="{escape_html(job.link)}">Job Open</a>')

        return "\n".join(parts)
