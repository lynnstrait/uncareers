# Job Watcher

## Structure
- `.github/workflows/`
- `data/`
- `scripts/common/`
- `scripts/adapters/`
- `scripts/browser/`
- `scripts/runners/`

## Required secrets
- `IAEA_FEED_URL`
- `UN_FEED_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Notes
- `un_careers.yml` uses `UN_FEED_URL`
- UN Careers location filter is controlled in workflow env:
  - `UN_CAREERS_LOCATION_FILTERS: "VIENNA"` or `VIENNA,GENEVA,SEOUL`
- `unido.yml` and `ctbto.yml` are Playwright-based.
- Keep `DRY_RUN: "true"` for first validation run.
