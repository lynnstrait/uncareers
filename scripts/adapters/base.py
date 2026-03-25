from abc import ABC, abstractmethod

from scripts.common.models import JobItem
from scripts.common.helpers import parse_rss


class SourceAdapter(ABC):
    source_name: str = "unknown"

    def __init__(self, source_url: str):
        self.source_url = source_url

    @abstractmethod
    def fetch_jobs(self) -> list[JobItem]:
        raise NotImplementedError

    def matches_keyword(self, job: JobItem) -> bool:
        return True

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


class RSSAdapter(SourceAdapter, ABC):
    def fetch_rss_items(self) -> list[dict]:
        from scripts.common.helpers import fetch
        xml_bytes = fetch(self.source_url)
        return parse_rss(xml_bytes)
