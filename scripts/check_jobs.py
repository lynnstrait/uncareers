import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.common.helpers import log, parse_any_date_to_ts
from scripts.common.state import load_state, save_state
from scripts.common.telegram_utils import telegram_send
from scripts.adapters.iaea import IAEAAdapter
from scripts.adapters.un_careers import UNCareersAdapter
from scripts.adapters.unido_static import UNIDOStaticAdapter
from scripts.adapters.ctbto_static import CTBTOStaticAdapter

SOURCE_ADAPTER = os.environ["SOURCE_ADAPTER"].strip().lower()
SOURCE_LABEL = os.environ.get("SOURCE_LABEL", SOURCE_ADAPTER.upper()).strip()
SOURCE_URL = os.environ["SOURCE_URL"].strip()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "10"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "data/seen_jobs.json"))

UN_CAREERS_LOCATION_FILTERS = [
    x.strip().upper()
    for x in os.environ.get("UN_CAREERS_LOCATION_FILTERS", "VIENNA,GENEVA,SEOUL").split(",")
    if x.strip()
]

UNIDO_LOCATION_FILTER = os.environ.get("UNIDO_LOCATION_FILTER", "Vienna, Austria").strip().lower()
CTBTO_LOCATION_FILTER = os.environ.get("CTBTO_LOCATION_FILTER", "").strip().lower()

DISABLE_WEB_PAGE_PREVIEW = os.environ.get("DISABLE_WEB_PAGE_PREVIEW", "false").strip().lower()
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
BOOTSTRAP_MODE = os.environ.get("BOOTSTRAP_MODE", "false").strip().lower() == "true"

def get_adapter():
    if SOURCE_ADAPTER == "iaea":
        return IAEAAdapter(SOURCE_URL)
    if SOURCE_ADAPTER == "un_careers":
        return UNCareersAdapter(SOURCE_URL, location_filters=UN_CAREERS_LOCATION_FILTERS)
    if SOURCE_ADAPTER == "unido":
        return UNIDOStaticAdapter(SOURCE_URL, location_filter=UNIDO_LOCATION_FILTER)
    if SOURCE_ADAPTER == "ctbto":
        return CTBTOStaticAdapter(SOURCE_URL, location_filter=CTBTO_LOCATION_FILTER)
    raise ValueError(f"Unsupported SOURCE_ADAPTER: {SOURCE_ADAPTER}")

def main() -> int:
    log(f"Source adapter: {SOURCE_ADAPTER}")
    log(f"Source label: {SOURCE_LABEL}")
    log(f"Source URL: {SOURCE_URL}")
    log(f"State file: {STATE_FILE}")
    log(f"DRY_RUN={DRY_RUN}")
    log(f"BOOTSTRAP_MODE={BOOTSTRAP_MODE}")

    try:
        adapter = get_adapter()
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

    jobs = [job for job in jobs if adapter.is_real_job(job) and adapter.matches_keyword(job)] if hasattr(adapter, 'is_real_job') else jobs
    jobs.sort(key=lambda x: parse_any_date_to_ts(getattr(x, "raw_date", "") or getattr(x, "closing_date", "") or getattr(x, "published", "") or getattr(x, "open_date", "")), reverse=True)

    log(f"Matched jobs: {len(jobs)}")

    state = load_state(STATE_FILE)
    seen_ids = set(state.get("seen_ids", []))

    new_jobs = [job for job in jobs if job.id and job.id not in seen_ids]

    if not new_jobs:
        log("No new matching jobs.")
        return 0

    if BOOTSTRAP_MODE:
        bootstrap_ids = [job.id for job in new_jobs[:MAX_ALERTS_PER_RUN]]
        merged = list(dict.fromkeys(bootstrap_ids + state.get("seen_ids", [])))[:1000]
        save_state(STATE_FILE, {"seen_ids": merged})
        log(f"BOOTSTRAP_MODE saved items: {len(bootstrap_ids)}")
        return 0

    alerts_sent = 0
    new_ids = []

    for job in new_jobs[:MAX_ALERTS_PER_RUN]:
        try:
            telegram_send(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, adapter.build_message(job), dry_run=DRY_RUN, disable_preview=DISABLE_WEB_PAGE_PREVIEW)
            alerts_sent += 1
            if not DRY_RUN:
                new_ids.append(job.id)
            time.sleep(1)
        except Exception as e:
            log(f"Failed to send Telegram message: {e}")

    if not DRY_RUN:
        merged = list(dict.fromkeys(new_ids + state.get("seen_ids", [])))[:1000]
        save_state(STATE_FILE, {"seen_ids": merged})

    log(f"Alerts sent: {alerts_sent}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
