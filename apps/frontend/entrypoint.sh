#!/bin/sh
# Runtime injection of NEXT_PUBLIC_* config into the pre-built Next.js bundle.
#
# The image is built ONCE with placeholder values (each NEXT_PUBLIC_<X> is baked as the
# literal string `APP_NEXT_PUBLIC_<X>`). At container start we read the REAL NEXT_PUBLIC_*
# values from the environment and sed-replace the placeholders in the built assets, so a
# single generic image serves any environment/domain — nothing is baked at build time and
# no domain lives in the repo. (Same pattern as frontend/asgard-admin + bank-client-web.)
#
# Only NEXT_PUBLIC_* vars are ever substituted, and those are public-by-design client
# config — no secret is written into the bundle.
set -e

SED_SCRIPT="$(mktemp)"

# Longest keys first so a shorter key can't partially match a longer one.
printenv \
  | grep '^NEXT_PUBLIC' \
  | awk -F'=' '{ print length($1), $0 }' \
  | sort -nr \
  | cut -d' ' -f2- \
  | awk -F'=' '{ key=$1; $1=""; value=substr($0,2); printf "s|APP_%s|%s|g\n", key, value }' \
  > "$SED_SCRIPT"

if [ -s "$SED_SCRIPT" ]; then
  [ -d .next ] && find .next -type f -exec sed -i -f "$SED_SCRIPT" {} +
  [ -d public ] && find public -type f -name '*.js' -exec sed -i -f "$SED_SCRIPT" {} +
fi

rm -f "$SED_SCRIPT"

exec "$@"
