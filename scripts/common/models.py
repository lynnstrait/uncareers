from dataclasses import dataclass

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
