# Building the Android app

Everything under `android/` was written and verified on Linux without Android
Studio: the unit tests, `assembleDebug` and lint ran inside Docker
(`android/ci/build.sh`), and the same commands run in GitHub Actions. The app has
not yet been run on a device or emulator, so expect the first on-device session to
turn up UI and playback details to adjust.

## One-time setup

1. Android Studio (Ladybug or later) with the Android 15 (API 35) SDK, or just a
   JDK 17 and the command-line SDK tools with `platforms;android-35` and
   `build-tools;35.0.0`.
2. Decide the application id. `com.bookdoe.app` in
   `android/app/build.gradle.kts` is a placeholder marked `TODO(maintainer)`; replace it
   with your own reverse-DNS id before the first Play upload (it cannot change
   afterwards). The same id goes into `packageName` in
   `.github/workflows/android.yml`.

## Open and run

Android Studio: File, Open, pick the `android` folder. Let it sync, then Run on an
emulator (API 26 or newer) or a device with USB debugging. Add a server: the app
needs a reachable bookdoe (`http://<lan-ip>:8795`), Jellyfin or Audiobookshelf.
An emulator reaches the host machine through the emulator's loopback alias
address (see the Android emulator networking docs), not `localhost`.

Command line:

```sh
cd android
./gradlew :core:test :app:testDebugUnitTest     # unit tests, no emulator
./gradlew :app:assembleDebug                    # app/build/outputs/apk/debug/app-debug.apk
./gradlew :app:lintDebug                        # app/build/reports/lint-results-debug.html
```

From the repository root, `make android-test` and `make android-build` wrap the
same commands.

### Without an Android SDK on the machine

`android/ci/build.sh` runs the whole thing in a container built from
`android/ci/Dockerfile` (JDK, SDK 35, build-tools), with a persistent Gradle cache
under `~/.cache/bookdoe-android-gradle`:

```sh
android/ci/build.sh                 # tests, assembleDebug, lint
android/ci/build.sh :core:test      # any Gradle tasks
```

On an arm64 host the Android build tools are still x86_64 binaries; the image adds
the amd64 C runtime and the host needs binfmt emulation registered once
(`docker run --privileged --rm tonistiigi/binfmt --install amd64`). Only `aapt2`
runs emulated; Gradle, Kotlin and the tests stay native.

On an arm64 host with neither Docker nor amd64 emulation, an aarch64 JDK 17 and
the command-line SDK tools are enough for `:core:test`, `:app:compileDebugKotlin`
and `:app:testDebugUnitTest`. Only the stock `aapt2` cannot start, so point AGP at
a native arm64 build (for example the static aarch64 `aapt2` from
`lzhiyong/android-sdk-tools` 35.0.2) for that invocation only:

```sh
cd android
ANDROID_HOME=$HOME/sdk ./gradlew -Pandroid.aapt2FromMavenOverride=/path/to/arm64/aapt2 \
  :core:test :app:compileDebugKotlin :app:testDebugUnitTest
```

## Sideloading the debug APK

Every CI run of the `android` workflow uploads `bookdoe-debug-apk` as an
artifact. Download it, copy `app-debug.apk` to the phone (or `adb install -r
app-debug.apk`), allow installs from that source when asked. Debug builds use the
id suffix `.debug`, so they install next to a release build.

## Release signing

Signing material never lives in the repository. The release build reads these from
the environment, or from Gradle properties of the same names in
`~/.gradle/gradle.properties`:

| Variable | Value |
| --- | --- |
| `BOOKDOE_KEYSTORE_PATH` | path to the upload keystore (`.jks`) |
| `BOOKDOE_KEYSTORE_PASSWORD` | its store password |
| `BOOKDOE_KEY_ALIAS` | key alias inside the keystore |
| `BOOKDOE_KEY_PASSWORD` | the key password |

Create the keystore once and keep it outside the repository (a password manager
attachment is a good place):

```sh
keytool -genkeypair -v -keystore bookdoe-upload.jks -alias upload \
  -keyalg RSA -keysize 4096 -validity 10000
```

Then `./gradlew :app:bundleRelease` produces
`app/build/outputs/bundle/release/app-release.aab`. Without the variables the
release build is unsigned (Gradle prints a warning); the debug build always works
with the SDK's debug keystore. Play App Signing is recommended: Google keeps the
app signing key and this keystore is only the upload key, which can be reset
through Play Console support if lost.

## Play Console: internal testing

1. Create a developer account (one-time fee) and an app with the chosen
   application id, name "bookdoe" (or the final name), app type "App", free.
2. Testing, Internal testing, Create new release. Accept Play App Signing. Upload
   the AAB from `bundleRelease` or from the `bookdoe-release-aab` workflow
   artifact. Add testers by email list; share the opt-in link.
3. Fill the store listing basics once (short and full description, icon
   512x512, feature graphic, two phone screenshots). Internal testing does not
   require review.
4. Policy forms under App content: privacy policy URL (a short page saying the app
   talks only to servers the user configures and collects nothing), ads "no",
   data safety "no data collected or shared" (progress and credentials go only to
   the user's own server), target audience 18+ or 13+ as you prefer, news app
   "no", COVID "no", government app "no".
5. The app declares `FOREGROUND_SERVICE_MEDIA_PLAYBACK` (audio playback) and
   `FOREGROUND_SERVICE_DATA_SYNC` (downloads). Play asks for a justification for
   each foreground service type from late 2024: "plays audiobooks in the
   background with media controls" and "downloads books the user selected for
   offline use".
6. Cleartext traffic: the network security config allows plain HTTP so self-hosted
   servers on a private LAN work. Play does not block this, but if asked during
   review the honest justification is "connects to the user's own media server,
   which may be HTTP on a private network; HTTPS servers are validated normally".
   Users who only use HTTPS can set `cleartextTrafficPermitted="false"` in
   `android/app/src/main/res/xml/network_security_config.xml`.

### From GitHub Actions

`.github/workflows/android.yml` runs tests, a debug APK and lint on every push or
pull request that touches `android/`. The `release` job runs on a `v*` tag or a
manual dispatch with "release" ticked, and only when these repository secrets
exist (Settings, Secrets and variables, Actions); it skips cleanly otherwise:

| Secret | Value |
| --- | --- |
| `ANDROID_KEYSTORE_BASE64` | `base64 -w0 bookdoe-upload.jks` |
| `ANDROID_KEYSTORE_PASSWORD` | the store password |
| `ANDROID_KEY_ALIAS` | the key alias |
| `ANDROID_KEY_PASSWORD` | the key password |
| `PLAY_SERVICE_ACCOUNT_JSON` | optional: a Play Console service account JSON with "Release to testing tracks" permission; when absent the AAB is only kept as an artifact |

To create the service account: Google Cloud Console, IAM, Service accounts, create
one and a JSON key; then Play Console, Users and permissions, invite that account's
email with release permission for the app. The upload goes to the internal track
with status "completed".

Bump `versionCode` and `versionName` in `android/app/build.gradle.kts` for every
upload; Play rejects a reused `versionCode`.

## F-Droid notes

The build is reproducible from source with the Gradle wrapper and has no
proprietary dependencies (AndroidX, Media3, OkHttp, Coil, kotlinx). F-Droid's
metadata would point at `android/`, subdir `app`, Gradle flavour none, and the
`versionCode`/`versionName` in `build.gradle.kts`. The `release` build type uses
R8; F-Droid builds unsigned and signs with its own key, so a Play upload key is not
involved. The one thing to decide is whether to keep cleartext HTTP on (F-Droid
users are usually fine with it).

## Checklist before a public release

- Application id replaced; `versionCode` bumped.
- Keystore created and stored safely; the four signing secrets set in GitHub.
- Privacy policy page published; data safety form answered as "no data collected".
- Launcher icon replaced with real artwork if desired
  (`android/app/src/main/res/drawable/ic_launcher_foreground.xml`).
- Tested on a device against each server kind you care about; the Jellyfin and
  Audiobookshelf paths are unverified against real servers.
