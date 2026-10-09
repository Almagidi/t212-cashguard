#!/usr/bin/env bash
set -euo pipefail

if [ "${1:-}" = "--staged" ]; then
    files="$(git diff --cached --name-only --diff-filter=ACMR)"
elif [ "$#" -eq 0 ]; then
    files="$(git ls-files)"
else
    echo "Usage: $0 [--staged]" >&2
    exit 2
fi

violations="$({
    printf '%s\n' "$files" | awk '
        NF {
            base = $0
            sub(/^.*\//, "", base)
            if (base == ".env" || index(base, ".env.") == 1) {
                if (base != ".env.example") {
                    print $0
                }
            }
        }
    '
} || true)"

if [ -n "$violations" ]; then
    echo "ERROR: secret-bearing environment files must not be committed:" >&2
    printf '%s\n' "$violations" >&2
    exit 1
fi

echo "Secret-file boundary check passed."
