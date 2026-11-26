#!/usr/bin/env sh
# Run the Android unit tests, debug build and lint inside Docker, with no Android
# SDK on the host. Usage:
#   android/ci/build.sh                 # tests + assembleDebug + lint (what CI runs)
#   android/ci/build.sh :core:test      # any Gradle tasks
# The Gradle cache persists in $BOOKDOE_ANDROID_GRADLE_CACHE (default
# ~/.cache/bookdoe-android-gradle) so the second run is fast. Containers are
# named bookdoe-android-build-<pid>.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
android_dir=$(cd "$here/.." && pwd)
cache=${BOOKDOE_ANDROID_GRADLE_CACHE:-"$HOME/.cache/bookdoe-android-gradle"}
image=${BOOKDOE_ANDROID_IMAGE:-bookdoe-android-ci}
mkdir -p "$cache"

if [ "$image" = "bookdoe-android-ci" ]; then
  docker build -q -t "$image" "$here" >/dev/null
fi

if [ "$#" -eq 0 ]; then
  set -- :core:test :app:testDebugUnitTest :app:assembleDebug :app:lintDebug
fi

exec docker run --rm --name "bookdoe-android-build-$$" \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp/home -e GRADLE_USER_HOME=/gradle \
  -v "$android_dir:/workspace" -v "$cache:/gradle" \
  -w /workspace "$image" ./gradlew --no-daemon --console=plain "$@"
