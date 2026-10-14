#!/bin/sh
set -eu
umask 077

# Never print credentials, including invalid values. This is part of normal startup.
fail() {
    printf '%s\n' 'Grafana preflight rejected credentials or public HTTPS origin' >&2
    exit 1
}
password=${GF_SECURITY_ADMIN_PASSWORD:-}
[ "${#password}" -ge 16 ] || fail
# A supported printable-ASCII contract avoids Unicode TrimSpace mutation.
if ! (LC_ALL=C; export LC_ALL; case "$password" in *[![:print:]]*) exit 1 ;; esac); then
    fail
fi
case "$password" in
    change-me|CHANGE-ME|admin|ADMIN) fail ;;
esac
# Whitespace-only passwords are invalid regardless of length.
case "$password" in
    *[![:space:]]*) ;;
    *) fail ;;
esac
root=${GF_SERVER_ROOT_URL:-}
case "$root" in
    https://*/grafana/) ;;
    *) fail ;;
esac
origin=${root#https://}
authority=${origin%/grafana/}
case "$authority" in
    ''|*[!A-Za-z0-9.:-]*) fail ;;
esac
# Hostname (IPv4/DNS) plus an optional valid TCP port; IPv6 literals need a separate contract.
printf '%s\n' "$authority" | awk -F: '
    NF > 2 { exit 1 }
    {
        if (length($1) == 0 || length($1) > 253) exit 1
        n = split($1, labels, ".")
        for (i = 1; i <= n; i++)
            if (length(labels[i]) == 0 || length(labels[i]) > 63 ||
                labels[i] !~ /^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$/) exit 1
        if (NF == 2 && ($2 !~ /^[0-9]+$/ || $2 < 1 || $2 > 65535)) exit 1
    }
' || fail
# Grafana expands ${...} in environment settings before its file provider.
# Supplying the literal value through that later provider prevents substitution.
# The file provider trims boundary whitespace; refuse it instead of changing it.
case "$password" in
    [[:space:]]*|*[[:space:]]) fail ;;
esac
private_dir=$(mktemp -d /tmp/cashguard-grafana.XXXXXX)
trap 'rm -f "$private_dir/password"; rmdir "$private_dir"' 0
printf '%s' "$password" > "$private_dir/password"
GF_SECURITY_ADMIN_PASSWORD='$__file{'"$private_dir/password"'}'
export GF_SECURITY_ADMIN_PASSWORD
# exec retains the mode-600 file for startup; only this container owns it.
exec /run.sh "$@"
