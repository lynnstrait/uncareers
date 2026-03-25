# Job Watcher

## Sources
- iaea (RSS)
- un_careers (RSS)
- unido (Playwright)
- ctbto (Playwright)

## Secrets required
- IAEA_FEED_URL
- UN_FEED_URL
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

## Notes
- UN Careers location filter is controlled in `.github/workflows/un_careers.yml`
- UNIDO and CTBTO workflows default to `DRY_RUN: "true"`
- For first live run, use `BOOTSTRAP_MODE: "true"` once if you do not want historical jobs sent.
