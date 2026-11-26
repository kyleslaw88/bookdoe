#!/bin/sh
# Fail unless the built app declares a valid scene manifest for the phone and CarPlay.
#
# Usage: check-scene-manifest.sh <Bookdoe.app | Info.plist>
#
# Builds 16-18 crashed in CarPlay because UISceneConfigurations was an Array in the
# built Info.plist; iOS needs a dictionary keyed by scene role. This checks the built
# plist (macOS only: uses /usr/libexec/PlistBuddy) and, when given an .app, that the
# delegate classes named in the manifest exist as strings in the app binary.
set -u

PB=/usr/libexec/PlistBuddy
if [ "$#" -ne 1 ]; then echo "usage: $0 <Bookdoe.app | Info.plist>" >&2; exit 2; fi
if [ ! -x "$PB" ]; then echo "FAIL: $PB not found (macOS only)" >&2; exit 2; fi

TARGET="$1"; BIN=""
if [ -d "$TARGET" ]; then
  PLIST="$TARGET/Info.plist"
  EXE="$($PB -c 'Print :CFBundleExecutable' "$PLIST" 2>/dev/null || true)"
  [ -n "$EXE" ] && [ -f "$TARGET/$EXE" ] && BIN="$TARGET/$EXE"
else
  PLIST="$TARGET"
fi
[ -f "$PLIST" ] || { echo "FAIL: no Info.plist at $PLIST" >&2; exit 2; }

FAILED=0
fail() { echo "FAIL: $*" >&2; FAILED=1; }
pb() { "$PB" -c "$1" "$PLIST" 2>/dev/null; }

M=":UIApplicationSceneManifest:UISceneConfigurations"
# PlistBuddy prints "Dict {" or "Array {" for containers.
KIND="$(pb "Print $M" | head -1)"
case "$KIND" in
  "Dict {"*) ;;
  "Array {"*) fail "UISceneConfigurations is an Array; it must be a Dict keyed by scene role (see docs/CARPLAY_TESTING.md)" ;;
  *) fail "UISceneConfigurations is missing from UIApplicationSceneManifest" ;;
esac

CLASSES=""
check_role() { # role, required scene class ("" = none required)
  role="$1"; sceneclass="$2"
  entry="$M:$role:0"
  if [ -z "$(pb "Print $entry")" ]; then fail "role $role is missing or has no configuration entry (expected an Array with at least one Dict)"; return; fi
  delegate="$(pb "Print $entry:UISceneDelegateClassName")"
  if [ -z "$delegate" ]; then
    fail "$role: first entry has no UISceneDelegateClassName"
  else
    case "$delegate" in
      *'$('*) fail "$role: UISceneDelegateClassName '$delegate' contains an unexpanded build variable" ;;
      *) CLASSES="$CLASSES $delegate" ;;
    esac
  fi
  if [ -n "$sceneclass" ]; then
    got="$(pb "Print $entry:UISceneClassName")"
    [ "$got" = "$sceneclass" ] || fail "$role: UISceneClassName is '${got:-<unset>}', expected $sceneclass"
  fi
}
check_role CPTemplateApplicationSceneSessionRoleApplication CPTemplateApplicationScene
check_role UIWindowSceneSessionRoleApplication ""

if [ -n "$BIN" ]; then
  # Debug builds keep the code in <name>.debug.dylib next to a stub executable, so search both.
  STR="$(mktemp)"; trap 'rm -f "$STR"' EXIT
  for f in "$BIN" "$TARGET"/*.debug.dylib; do [ -f "$f" ] && strings -a "$f" >> "$STR"; done
  for c in $CLASSES; do
    short="${c##*.}"
    grep -q -F "$short" "$STR" || fail "class $short (from $c) not found in app binary (or its .debug.dylib)"
  done
elif [ -d "$TARGET" ]; then
  echo "WARN: app binary not found; skipped class-name check" >&2
else
  echo "note: Info.plist given; skipped class-name check in binary"
fi

if [ "$FAILED" -ne 0 ]; then echo "Scene manifest check FAILED for $PLIST" >&2; exit 1; fi
echo "OK: scene manifest valid in $PLIST (delegates:$CLASSES)"
