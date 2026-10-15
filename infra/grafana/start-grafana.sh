#!/bin/sh
set -eu
original_umask=$(umask)
umask 077

# Never print credentials, including invalid values. This is part of normal startup.
fail() {
    printf '%s\n' 'Grafana preflight rejected credentials or public HTTPS origin' >&2
    exit 1
}
password=${GF_SECURITY_ADMIN_PASSWORD:-}
[ "${#password}" -ge 16 ] && [ "${#password}" -le 4096 ] || fail
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
trap 'rm -rf "$private_dir"' 0
printf '%s' "$password" > "$private_dir/password"
GF_SECURITY_ADMIN_PASSWORD='$__file{'"$private_dir/password"'}'
export GF_SECURITY_ADMIN_PASSWORD
# This recipe supports one known server administrator (admin, ID 1), SQLite,
# the image's fixed data/config paths, and one stopped database writer. Refuse
# custom identities/backends/arguments instead of rotating the wrong account/DB.
[ "$#" -eq 0 ] || fail
[ "${GF_SECURITY_ADMIN_USER:-admin}" = admin ] || fail
[ "${GF_DATABASE_TYPE:-sqlite3}" = sqlite3 ] || fail
[ -z "${GF_DATABASE_URL:-}" ] || fail
[ "${GF_DATABASE_PATH:-/var/lib/grafana/grafana.db}" = /var/lib/grafana/grafana.db ] || fail
[ "${GF_PATHS_DATA:-/var/lib/grafana}" = /var/lib/grafana ] || fail
[ "${GF_PATHS_HOME:-/usr/share/grafana}" = /usr/share/grafana ] || fail
[ "${GF_PATHS_CONFIG:-/etc/grafana/grafana.ini}" = /etc/grafana/grafana.ini ] || fail
command -v grafana >/dev/null 2>&1 || fail
GF_DATABASE_TYPE=sqlite3
GF_DATABASE_PATH=/var/lib/grafana/grafana.db
export GF_DATABASE_TYPE GF_DATABASE_PATH
reset() {
    grafana cli --homepath /usr/share/grafana --config /etc/grafana/grafana.ini \
        --configOverrides 'cfg:default.paths.data=/var/lib/grafana' \
        admin reset-admin-password --user-id="$1" --password-from-stdin \
        < "$private_dir/password" > "$private_dir/result" 2>&1 || fail
}
changed() {
    # Pinned 11.4.0 returns zero even for several reset failures. Exit status
    # alone is not proof; never expose the CLI's captured diagnostics.
    grep -Fq 'Admin password changed successfully' "$private_dir/result" || fail
}
if [ ! -e "$GF_DATABASE_PATH" ]; then
    # The CLI initializes/migrates a new database with the vetted file provider.
    reset 1
    changed
fi
# Discover on a disposable cold copy: even an unexpected negative user ID can
# only be modified on this private copy. Preserve any SQLite WAL before reading.
cp "$GF_DATABASE_PATH" "$private_dir/identity.db" || fail
for suffix in -wal -shm; do
    if [ -e "$GF_DATABASE_PATH$suffix" ]; then
        cp "$GF_DATABASE_PATH$suffix" "$private_dir/identity.db$suffix" || fail
    fi
done
GF_DATABASE_PATH="$private_dir/identity.db" reset -1
# Only the known single-admin identity is supported. No guessed nondefault ID.
grep -Fq 'Admin user cannot be found' "$private_dir/result" || fail
# Validate the entire list, not one matching line: usernames can contain
# newlines and otherwise forge a canonical first line with a continuation.
awk '
    !listing && /^Please try to run the command again specifying a user-id \(--user-id\) from the list below:$/ { listing = 1; next }
    listing {
        if ($0 ~ /^[[:space:]]*$/) next
        if ($0 ~ /^[[:space:]]*Username: admin ID: 1[[:space:]]*$/) { records++; next }
        bad = 1
    }
    END { exit !(listing && records == 1 && !bad) }
' "$private_dir/result" || fail
reset 1
changed
# Remove diagnostics and discovery copy, retain only the private file provider.
rm -f "$private_dir/result" "$private_dir"/identity.db*
unset password
umask "$original_umask"
# exec forwards signals; tmpfs teardown bounds the remaining mode-600 file.
exec /run.sh
