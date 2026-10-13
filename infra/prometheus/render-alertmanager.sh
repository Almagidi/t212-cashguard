#!/usr/bin/env sh
set -eu

# Rendered SMTP credentials must not be readable by other container users, or
# written through a pre-existing /tmp filename/symlink.
umask 077
rendered_dir=$(mktemp -d /tmp/cashguard-alertmanager.XXXXXX)
rendered_config=$rendered_dir/alertmanager.yml
trap 'rm -f "$rendered_config"; rmdir "$rendered_dir"' 0

SMTP_HOST=${SMTP_HOST:-localhost}
SMTP_PORT=${SMTP_PORT:-587}
SMTP_USER=${SMTP_USER:-}
SMTP_PASSWORD=${SMTP_PASSWORD:-}
ALERT_EMAIL_FROM=${ALERT_EMAIL_FROM:-alerts@cashguard.local}
ALERT_EMAIL_TO=${ALERT_EMAIL_TO:-admin@cashguard.local}
export SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD ALERT_EMAIL_FROM ALERT_EMAIL_TO

# The template uses single-quoted YAML scalars. Encode single quotes, and
# substitute only original template tokens once: token-like password text is data.
# YAML line breaks (CR/LF/NEL/Unicode separators) are unsupported; refuse without values.
awk '
BEGIN {
    split("SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD ALERT_EMAIL_FROM ALERT_EMAIL_TO", keys, " ")
    for (i in keys) {
        value = ENVIRON[keys[i]]
        if (value ~ /[\r\n]|\302\205|\342\200\250|\342\200\251/) {
            print "Alertmanager renderer refuses multiline SMTP/address settings" > "/dev/stderr"
            exit 1
        }
    }
}
{
    line = $0
    output = ""
    while (match(line, /\$\{(SMTP_HOST|SMTP_PORT|SMTP_USER|SMTP_PASSWORD|ALERT_EMAIL_FROM|ALERT_EMAIL_TO)\}/)) {
        key = substr(line, RSTART + 2, RLENGTH - 3)
        value = ENVIRON[key]
        gsub(/\047/, "\047\047", value)
        output = output substr(line, 1, RSTART - 1) value
        line = substr(line, RSTART + RLENGTH)
    }
    print output line
}' /etc/alertmanager/alertmanager.yml > "$rendered_config"

# exec preserves the private config for the running daemon/checker. On a render
# failure the shell trap removes only this invocation's file and directory.
if [ "${ALERTMANAGER_CHECK_CONFIG:-}" = "1" ]; then
    exec /bin/amtool check-config "$rendered_config"
fi

exec /bin/alertmanager \
  --config.file="$rendered_config" \
  --storage.path=/alertmanager \
  --web.external-url=http://alertmanager:9093
