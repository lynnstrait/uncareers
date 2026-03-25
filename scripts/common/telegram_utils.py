import urllib.parse
import urllib.request
import urllib.error
from .helpers import log

def telegram_send(bot_token: str, chat_id: str, message_html: str, dry_run: bool=False, disable_preview: str="false") -> None:
    if dry_run:
        log("DRY_RUN=true, skipping Telegram send.")
        log(message_html)
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        log(f"Telegram response: {body[:500]}")
