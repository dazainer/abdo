#!/usr/bin/env bash
# Register (or re-register) Abdo's Telegram webhook from values in .env.
#
# Usage:
#   scripts/set_webhook.sh https://abdo-prod.up.railway.app
#
# Pass the BASE Railway URL only — no trailing slash, no /tg path, and do NOT
# repeat ".up.railway.app" (a doubled domain caused a silent TLS failure once).
# If no URL is given, falls back to $APP_URL from the environment/.env.
set -euo pipefail

cd "$(dirname "$0")/.."

# Load .env (TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, optionally APP_URL).
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

APP_URL="${1:-${APP_URL:-}}"
: "${APP_URL:?Pass the base Railway URL, e.g. scripts/set_webhook.sh https://abdo-prod.up.railway.app}"
: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN missing from .env}"
: "${TELEGRAM_WEBHOOK_SECRET:?TELEGRAM_WEBHOOK_SECRET missing from .env}"

APP_URL="${APP_URL%/}"                      # strip any trailing slash
HOOK_URL="${APP_URL}/tg/${TELEGRAM_WEBHOOK_SECRET}"

echo "Registering webhook -> ${APP_URL}/tg/<secret>"
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${HOOK_URL}" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  --data-urlencode "drop_pending_updates=true"
echo

echo "Verifying (url shown with secret masked):"
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" \
  | sed "s#${TELEGRAM_WEBHOOK_SECRET}#<SECRET>#g"
echo
