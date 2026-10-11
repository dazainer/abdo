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

# Read only the keys this script needs from .env. Don't `source` the whole file:
# values like GOOGLE_SERVICE_ACCOUNT_JSON aren't valid shell and break it.
# Values already set in the environment win.
env_get() {
  [[ -f .env ]] || return 0
  # `|| true`: a missing key (e.g. no APP_URL line) must not trip `set -e`/pipefail.
  { grep -E "^$1=" .env || true; } | tail -n 1 | cut -d= -f2- \
    | sed -E "s/[[:space:]]+#.*$//; s/^[\"']//; s/[\"'][[:space:]]*$//"
}
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(env_get TELEGRAM_BOT_TOKEN)}"
TELEGRAM_WEBHOOK_SECRET="${TELEGRAM_WEBHOOK_SECRET:-$(env_get TELEGRAM_WEBHOOK_SECRET)}"
APP_URL="${APP_URL:-$(env_get APP_URL)}"

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
