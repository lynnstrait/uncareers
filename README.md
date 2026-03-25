# UN Job Watcher

## Supported sources
- iaea
- un_careers
- unido
- ctbto

## Script
- scripts/check_jobs.py

## Workflow files
- .github/workflows/iaea.yml
- .github/workflows/un_careers.yml
- .github/workflows/unido.yml
- .github/workflows/ctbto.yml

## State files
- data/seen_iaea.json
- data/seen_un_careers.json
- data/seen_unido.json
- data/seen_ctbto.json

## Required GitHub Secrets
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- IAEA_FEED_URL
- UN_CAREERS_FEED_URL

## Notes
- UNIDO is filtered to `Vienna, Austria`
- CTBTO is filtered to `Vienna`

## Bootstrap mode
To initialize a source without sending historical alerts:
1. Set `BOOTSTRAP_MODE=true` in the workflow env
2. Run the workflow manually once
3. Set it back to `false`

## Dry run
To test parsing without sending Telegram messages:
- Set `DRY_RUN=true`
