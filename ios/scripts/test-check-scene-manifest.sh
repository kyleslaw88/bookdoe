#!/bin/sh
# Self-test for check-scene-manifest.sh (macOS only). Usage: test-check-scene-manifest.sh
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

head='<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>UIApplicationSceneManifest</key><dict><key>UISceneConfigurations</key>'
tail='</dict></dict></plist>'
cp_entry='<dict><key>UISceneConfigurationName</key><string>CarPlay</string><key>UISceneClassName</key><string>CPTemplateApplicationScene</string><key>UISceneDelegateClassName</key><string>Bookdoe.CarPlaySceneDelegate</string></dict>'
ph_entry='<dict><key>UISceneConfigurationName</key><string>Default Configuration</string><key>UISceneDelegateClassName</key><string>Bookdoe.PhoneSceneDelegate</string></dict>'

printf '%s<dict><key>CPTemplateApplicationSceneSessionRoleApplication</key><array>%s</array><key>UIWindowSceneSessionRoleApplication</key><array>%s</array></dict>%s' "$head" "$cp_entry" "$ph_entry" "$tail" > "$T/good.plist"
printf '%s<array>%s%s</array>%s' "$head" "$cp_entry" "$ph_entry" "$tail" > "$T/array.plist"
printf '%s<dict><key>UIWindowSceneSessionRoleApplication</key><array>%s</array></dict>%s' "$head" "$ph_entry" "$tail" > "$T/nocarplay.plist"

sh "$HERE/check-scene-manifest.sh" "$T/good.plist" >/dev/null || { echo "FAIL: good plist rejected"; exit 1; }
for bad in array nocarplay; do
  if sh "$HERE/check-scene-manifest.sh" "$T/$bad.plist" >/dev/null 2>&1; then echo "FAIL: $bad plist accepted"; exit 1; fi
done
echo "OK: check-scene-manifest.sh accepts the good plist and rejects Array and missing-CarPlay plists"
