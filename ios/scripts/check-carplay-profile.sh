#!/bin/sh
# Fail early when a provisioning profile lacks the CarPlay Audio entitlement.
#
# Usage: check-carplay-profile.sh <decoded-profile.plist | ->
#   The argument is the decoded profile plist (output of `security cms -D -i <profile>`),
#   or '-' to read it from stdin.
set -eu

KEY="com.apple.developer.carplay-audio"

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <decoded-profile.plist | ->" >&2
  exit 2
fi

if [ "$1" = "-" ]; then
  CONTENT="$(cat)"
else
  CONTENT="$(cat "$1")"
fi

# The key must be present and its value must be <true/>.
VALUE="$(printf '%s\n' "$CONTENT" | tr -d '\t' | awk -v key="<key>$KEY</key>" '
  found { gsub(/^ +| +$/, ""); print; exit }
  index($0, key) { rest = substr($0, index($0, key) + length(key)); gsub(/^ +| +$/, "", rest); if (rest != "") { print rest; exit } found = 1 }
')"

case "$VALUE" in
  "<true/>"*)
    echo "OK: provisioning profile carries $KEY"
    ;;
  *)
    echo "ERROR: the installed provisioning profile does not contain $KEY set to true." >&2
    echo "The PROVISIONING_PROFILE_BASE64 secret holds a stale profile. Replace it with the" >&2
    echo "CarPlay-enabled App Store profile (see docs/CARPLAY_SETUP.md) and re-run the build." >&2
    exit 1
    ;;
esac
