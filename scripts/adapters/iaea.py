import re

from playwright.sync_api import sync_playwright

from scripts.adapters.base import RSSAdapter
from scripts.common.helpers import (
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
        m = re.search(
            r"\((P\d+|G\d+|NO?[A-Z]?\d+|FS\d+)\)\s*(MULT)?\s*$",
            title,
            re.IGNORECASE,
        )
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

    def extract_page_field(self, page_text: str, field_name: str) -> str:
        lines = [normalize_space(x) for x in page_text.split("\n")]
        lines = [x for x in lines if x]

        for i, line in enumerate(lines):
            if line.lower() == field_name.lower():
                for j in range(i + 1, min(i + 6, len(lines))):
                    candidate = lines[j].strip()
                    if not candidate or candidate == ":":
                        continue
                    if candidate.lower() in self.KNOWN_LABELS:
                        break
                    return candidate

            m = re.match(rf"^{re.escape(field_name)}\s*:\s*(.*)$", line, re.IGNORECASE)
            if m:
                value = m.group(1).strip()
                if value and value != ":" and value.lower() not in self.KNOWN_LABELS:
                    return value

                for j in range(i + 1, min(i + 6, len(lines))):
                    candidate = lines[j].strip()
                    if not candidate or candidate == ":":
                        continue
                    if candidate.lower() in self.KNOWN_LABELS:
                        break
                    return candidate

        return ""

    def fetch_detail_text(self, browser, link: str) -> str:
        page = browser.new_page()
        try:
            page.goto(link, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(2500)
            return page.locator("body").inner_text()
        finally:
            page.close()

    def fetch_detail_fields(self, browser, link: str) -> dict:
        if not link:
            return {}

        try:
            page_text = self.fetch_detail_text(browser, link)
        except Exception:
            return {}

        return {
            "open": self.extract_page_field(page_text, "Job Posting"),
            "closing": self.extract_page_field(page_text, "Closing Date"),
            "duration": self.extract_page_field(page_text, "Duration in Months"),
            "grade": self.extract_page_field(page_text, "Grade"),
        }

    def fetch_jobs(self) -> list[JobItem]:
        items = self.fetch_rss_items()
        jobs = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            for item in items:
                title_raw = (item.get("title") or "").strip()
                description = (item.get("description") or "").strip()
                published = (item.get("published") or "").strip()
                link = (item.get("link") or "").strip()

                title, level_from_title = self.clean_taleo_title(title_raw)
                detail = self.fetch_detail_fields(browser, link)

                level = level_from_title or detail.get("grade", "")
                duration_raw = detail.get("duration") or self.extract_duration_from_description(description)
                duration = ""
                if duration_raw:
                    duration = normalize_space(duration_raw)
                    if re.fullmatch(r"\d+", duration):
                        duration = f"{duration} months"             
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
                        level=level,
                        duration=duration,
                        open_date=open_date,
                        closing_date=closing_date,
                        raw_date=closing_date or open_date or published,
                    )
                )

            browser.close()

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
