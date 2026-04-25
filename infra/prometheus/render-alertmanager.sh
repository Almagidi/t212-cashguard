#!/usr/bin/env sh
set -eu

rendered_config=/tmp/alertmanager.yml
cp /etc/alertmanager/alertmanager.yml "$rendered_config"

escape_sed() {
  printf '%s' "$1" | sed 's/[\/&]/\\&/g'
}

replace_token() {
  token="$1"
  value="$2"
  escaped_value="$(escape_sed "$value")"
  sed -i "s|\${${token}}|${escaped_value}|g" "$rendered_config"
}

replace_token SMTP_HOST "${SMTP_HOST:-localhost}"
replace_token SMTP_PORT "${SMTP_PORT:-587}"
replace_token SMTP_USER "${SMTP_USER:-}"
replace_token SMTP_PASSWORD "${SMTP_PASSWORD:-}"
replace_token ALERT_EMAIL_FROM "${ALERT_EMAIL_FROM:-alerts@cashguard.local}"
replace_token ALERT_EMAIL_TO "${ALERT_EMAIL_TO:-admin@cashguard.local}"

if [ "${ALERTMANAGER_CHECK_CONFIG:-}" = "1" ]; then
  exec /bin/amtool check-config "$rendered_config"
fi

exec /bin/alertmanager \
  --config.file="$rendered_config" \
  --storage.path=/alertmanager \
  --web.external-url=http://alertmanager:9093
