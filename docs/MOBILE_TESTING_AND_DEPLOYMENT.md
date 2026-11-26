# Testing and deploying the bookdoe iOS and Android apps

A checklist for the owner. It assumes a Mac, a paid Apple Developer account and
GitHub access. It links to the detailed docs instead of copying them:

- iOS: [IOS_BUILD.md](IOS_BUILD.md) (commands), [IOS_APP.md](IOS_APP.md) (features and
  provider assumptions)
- Android: [ANDROID_BUILD.md](ANDROID_BUILD.md), [ANDROID_APP.md](ANDROID_APP.md)
- Server side: [PLAYER_API.md](PLAYER_API.md), [COMPAT_PLAYERS.md](COMPAT_PLAYERS.md),
  [INSTALL.md](INSTALL.md)

Steps marked "(general)" are standard Apple or Google console steps that are not
described in the repo docs. Check them against Apple's and Google's current pages,
because those consoles change.

## Contents

1. Orientation
2. Test plan
3. iOS deployment
4. Android deployment
5. Release checklist, rollback and hotfix
6. Decisions and placeholders still open
7. Troubleshooting

---

## 1. Orientation

### What exists

- An iOS app (`ios/`): SwiftUI app plus the `BookdoeKit` Swift package.
- An Android app (`android/`): Kotlin, Compose, Media3. Modules `core` and `app`.
- Three server types in both apps: bookdoe (Player API v1), Jellyfin (10.9+) and
  Audiobookshelf (2.26+).
- CI that builds and tests both apps, and optional upload jobs for TestFlight and
  the Play internal track.

### What has never been run on a real device

Be clear about this before you start.

| Part | What has been done | What has not |
| --- | --- | --- |
| iOS `BookdoeKit` package | `swift test` passes (Linux and CI) | nothing on a device |
| iOS app target | compiles in CI (simulator build) | never run on a simulator or iPhone by a person |
| Android `core` and `app` | unit tests, `assembleDebug`, lint in CI and Docker | never run on an emulator or phone |
| bookdoe provider | Player API is tested by the server's own tests | not driven by the apps on a device |
| Jellyfin provider | unit tests against fake answers; one live capture of the API | never run against a real Jellyfin server from the app |
| Audiobookshelf provider | unit tests against fake answers; routes read from source | never run against a real Audiobookshelf server from the app |

State plainly: the first real device runs are expected to surface bugs. Plan for
fixes. Compile errors in the iOS app target, UI problems, playback details and
provider mismatches are all likely. Each provider is one file per platform, so a
wrong assumption is usually a local fix.

### Order of work

1. Run the automated checks (section 2a).
2. Build a throwaway test setup (section 2b).
3. Run the manual matrix on a simulator or emulator, then on real phones (2c).
4. Only then set up signing and the store consoles (sections 3 and 4).

---

## 2. Test plan

### 2a. Automated checks

These need no device. Run them first and after every fix.

| Check | Command | Where it runs |
| --- | --- | --- |
| iOS package tests | `cd ios/Packages/BookdoeKit && swift test` (or `make ios-test`) | Mac, Linux, CI (`ios.yml`, job `package-tests`, macOS runner) |
| iOS simulator build | `cd ios && xcodegen generate && xcodebuild -project Bookdoe.xcodeproj -scheme Bookdoe -destination 'platform=iOS Simulator,name=iPhone 15' build` | Mac, CI (`simulator-build`, generic simulator destination, signing off) |
| Android unit tests | `cd android && ./gradlew :core:test :app:testDebugUnitTest` (or `make android-test`) | any machine with JDK 17 and SDK 35, Docker, CI |
| Android debug APK | `cd android && ./gradlew :app:assembleDebug` | same |
| Android lint | `cd android && ./gradlew :app:lintDebug` (or `make android-build` for APK plus lint) | same |
| Android in Docker, no SDK | `android/ci/build.sh` (tests, APK, lint) or `android/ci/build.sh :core:test` | any Docker host |
| Server tests | `make test` (OpenReads and bookdoe suites) | any machine with Python |
| Everything the server CI does | `make ci` | any machine with Python and Node |
| Secret and hygiene scan | `make secret-scan` (gitleaks if installed, otherwise the Python scanner); `make hygiene` runs the Python scanner only | any machine, CI job `secret-scan` |

Notes:

- The `ios` workflow runs on pushes and pull requests that touch `ios/`. The
  `android` workflow does the same for `android/`. The `server` workflow covers the
  rest, including gitleaks over the full history.
- The iOS CI job selects Xcode 16.4. The repo doc asks for Xcode 15.4 or later. Use
  the newest Xcode on your Mac unless it causes trouble.
- On Apple silicon the iOS simulator name `iPhone 15` must exist in your Xcode.
  Pick any installed iPhone simulator if it does not.

Checklist:

- [ ] `swift test` passes on the Mac.
- [ ] `xcodegen generate` and the simulator build pass on the Mac.
- [ ] `./gradlew :core:test :app:testDebugUnitTest` passes.
- [ ] `./gradlew :app:assembleDebug :app:lintDebug` passes.
- [ ] `make test` and `make secret-scan` pass.
- [ ] The latest `ios`, `android` and `server` runs on GitHub are green.

### 2b. One-time test server setup

Use throwaway servers and small libraries. Never test against your real library
first.

#### bookdoe (throwaway)

1. On a Docker host: `git clone <repository> bookdoe-test && cd bookdoe-test`.
2. Run `sh install.sh` (or `sh install.sh --yes` for defaults, data under `./data`).
   See [INSTALL.md](INSTALL.md).
3. Point `BOOKS_DIR` and `AUDIOBOOKS_DIR` at small test folders (section "Test
   libraries" below) when the script asks.
4. Open `http://<your-server>:<port>/setup` and finish the wizard. The default port
   in `.env.example` is 8795. Create an administrator account.
5. Check `http://<your-server>:<port>/readyz` answers.
6. Confirm the server is new enough: password login for the apps needs server
   0.9.1 or later. Older servers still work through the pairing code only.
7. Create a second, non-admin test user. Use it for the app tests, so you also check
   the normal user path.
8. For the pairing code test: sign in to the web Player, open Settings, Pair, and
   type the code the app shows (`XXXX-XXXX`). Codes last 15 minutes.
9. For an HTTPS test, put a reverse proxy in front and set `BOOKDOE_PUBLIC_URL`
   (see "Reverse proxy and HTTPS" in [INSTALL.md](INSTALL.md)).

- [ ] Test bookdoe answers on HTTP from the phone's network.
- [ ] A second URL over HTTPS exists (needed for the HTTP vs HTTPS tests).
- [ ] Admin account and a normal test account exist.

#### Jellyfin (throwaway)

The Jellyfin setup is not in this repo. Use Jellyfin's own docs. The app expects
10.9 or later.

1. Run Jellyfin in Docker on any host, finish its first-run wizard.
2. Add a library of type "Books" pointing at the audiobook test folder. The app
   assumes audiobook libraries have `CollectionType == "books"`.
3. Create a test user with a password. Do not use the admin account for tests.
4. Include one single-file book and one multi-file book (a folder of numbered
   tracks). The multi-file shape is the least certain assumption (see
   [IOS_APP.md](IOS_APP.md), Jellyfin table).
5. Record the Jellyfin version.

#### Audiobookshelf (throwaway)

Also not in this repo. Use Audiobookshelf's own docs. The app expects 2.26 or later.

1. Run Audiobookshelf in Docker, create the root user and an audiobook library on
   the test folder.
2. Create a normal test user with a password.
3. Include one single-file and one multi-file book, one with embedded chapters.
4. Record the Audiobookshelf version.

#### Test libraries

Keep them small so scans and downloads are quick. Use files you own or public-domain
audio.

- [ ] 6 to 10 audiobooks. At least: one single-file, one multi-file (3+ tracks),
      one with chapters, one with no cover, one with a very long title.
- [ ] One audiobook long enough to test the sleep timer and sync (30+ minutes).
- [ ] 3 to 5 ebooks (EPUB and PDF).
- [ ] Enough books to need a second page of results (the page size is set in the
      app, so add more small books until paging kicks in, and note the count).
- [ ] Two books by the same author and one book in a series, for filter checks.

#### Devices

- [ ] An iPhone on iOS 17 or later (deployment target is 17.0).
- [ ] An Android phone on Android 8 (API 26) or later. Ideally also one on
      Android 12 or later (dynamic colour) and Android 13 or later (notification
      permission prompt).
- [ ] Bluetooth headphones or a car head unit.
- [ ] Both phones on the same Wi-Fi as the test servers.

### 2c. Manual device test matrix

How to use it:

- Run the matrix once per provider per platform. That is 6 passes
  (bookdoe, Jellyfin, Audiobookshelf; iOS, Android). Start with Bookdoe.
- Copy the tables into a the issue tracker issue or a notes file and tick as you go.
- Mark each row Pass, Fail or N/A. For Fail, follow "What to record" below.

Test run header (fill in once per pass):

```
Date:
App: iOS or Android, version (MARKETING_VERSION or versionName), build
Device and OS version:
Server type and version:
Server URL scheme: http or https
Account: admin or normal
```

#### Table A: servers and sign-in

| Done | Test | Expected |
| --- | --- | --- |
| [ ] | Add a server with a correct URL and password | Signs in, library picker or Home appears |
| [ ] | bookdoe only: sign in with the pairing code | App shows `XXXX-XXXX`, approving in the web Player signs the app in |
| [ ] | bookdoe only: let a pairing code expire (15 min) | A clear "expired" error, can start again |
| [ ] | Wrong password | Clear error, no crash, stays on the form |
| [ ] | Unreachable server (wrong IP or server stopped) | Clear error within a reasonable time, no crash |
| [ ] | URL typed without a scheme, with a trailing slash, with a path prefix | Works or gives a clear message |
| [ ] | Two servers of different kinds saved | Can switch the active server; each keeps its own sign-in |
| [ ] | Kill the app and reopen | Still signed in, no password prompt |
| [ ] | Log out or remove the server | Credentials gone: reopening shows no signed-in state for that server |
| [ ] | bookdoe: revoke the device in the web Player (Settings, devices) | The next request shows a sign-in required state, not a crash |
| [ ] | bookdoe: check `capabilities` on a server in `proxy` or `none` auth mode | Password login is refused (400); pairing still works |

#### Table B: browse, search, filter, paging

| Done | Test | Expected |
| --- | --- | --- |
| [ ] | Home | "Continue listening" and recently added show correct books |
| [ ] | Library list | Covers load, titles and authors correct, no blank rows |
| [ ] | Scroll to the end | More items load (paging), no duplicates, no endless spinner |
| [ ] | Search for a title, an author, a nonsense word | Right hits, then an empty state for nonsense |
| [ ] | Search while typing fast (Android search is debounced) | No stale results shown after the last keystroke |
| [ ] | Sort options | Order changes as labelled |
| [ ] | Filter audiobook vs ebook | Only that kind shows; the filter survives scrolling |
| [ ] | Pull to refresh (Android) or refresh (iOS) | New book added on the server appears |
| [ ] | Account scope | A kid or restricted account only sees its allowed books (bookdoe) |

#### Table C: detail and chapters

| Done | Test | Expected |
| --- | --- | --- |
| [ ] | Open a book | Title, author, narrator, description, duration, position |
| [ ] | Book with server chapters | Chapter list matches the server |
| [ ] | Multi-file book with no chapters | Chapters are derived from track boundaries |
| [ ] | Tap a chapter | Playback jumps to the chapter start |
| [ ] | Book with no cover | A placeholder, no crash |
| [ ] | Jellyfin only: author, series and chapters | Shown; if blank, see troubleshooting |

#### Table D: playback

| Done | Test | Expected |
| --- | --- | --- |
| [ ] | Play a single-file book | Starts within a few seconds |
| [ ] | Play a multi-file book | One continuous timeline: total length and seek bar cover all files |
| [ ] | Let playback cross a file boundary | No gap, no stall, position keeps counting |
| [ ] | Seek far forward and back | Lands near the target and resumes |
| [ ] | Speed (try 0.75, 1.0, 1.5, 2.0) | Pitch is natural, speed holds after pause and resume |
| [ ] | Skip back 15 s and forward 30 s | Exact jumps (skip 15/30) |
| [ ] | Sleep timer (short value) | Playback stops at the end of the timer |
| [ ] | Sleep timer cancelled | Playback does not stop |
| [ ] | Lock the screen while playing | Audio continues |
| [ ] | Lock-screen or Control Center controls (iOS); notification controls (Android) | Play, pause, skip buttons work; title and cover show |
| [ ] | Android: notification artwork on a server that needs a token for covers | App icon shown (known gap, not a bug) |
| [ ] | Switch to another app for 10 minutes | Playback continues, no crash on return |
| [ ] | Bluetooth headphones: play, pause, next, previous buttons | Buttons control the app |
| [ ] | Car (Bluetooth or Android Auto): start and pause from the car | Works. Android Auto cannot browse the library yet (known gap) |
| [ ] | Disconnect the headphones while playing | Playback pauses |
| [ ] | Incoming phone call during playback | Pauses, and resumes or stays paused sensibly after the call |
| [ ] | Another app starts audio (navigation prompt, video) | Ducks or pauses, then recovers (audio focus on Android, interruptions on iOS) |
| [ ] | Alarm or timer rings | Pauses; resume works |
| [ ] | Kill the app while playing, reopen | Last position is restored on the book |
| [ ] | Android: deny the notification permission (Android 13+) | Playback still works; controls may be limited |

#### Table E: progress sync

Use a second client (the web Player, or the other phone) to see the server side.

| Done | Test | Expected |
| --- | --- | --- |
| [ ] | Play for a minute, check the server | Position updated (reports every 15 s and on pause, seek, stop) |
| [ ] | Pause and check the server | Position matches |
| [ ] | Play on the web Player, open the book in the app | App shows the newer position |
| [ ] | Airplane mode, play 2 minutes, then reconnect | The queued report is sent after the next successful report; server position catches up |
| [ ] | Offline queue survives killing the app | After restart and reconnect the position still reaches the server |
| [ ] | bookdoe stale-report conflict: set the position in the web Player so it is newer than the phone's offline progress, then reconnect the phone | Server answers "stale"; the app jumps to the server position |
| [ ] | bookdoe: phone clock set wrong by minutes | No bad overwrite (server clamps clocks more than 300 s ahead) |
| [ ] | Jellyfin: finish a book | Marked played once at the end (partial progress is per file; known gap) |
| [ ] | Audiobookshelf: play, then check the web UI progress | Matches. If not, check session vs `PATCH /api/me/progress` path |
| [ ] | Continue listening on Home updates after playing | The book moves to the top |

#### Table F: downloads and offline

| Done | Test | Expected |
| --- | --- | --- |
| [ ] | Download a small audiobook | Progress shown, completes, shows as downloaded |
| [ ] | Download while the app is in the background | iOS: background URLSession finishes. Android: foreground job notification, finishes |
| [ ] | Airplane mode, open the downloaded book | Detail opens offline with chapters |
| [ ] | Play the downloaded book offline | Plays all files as one timeline |
| [ ] | Offline playback then reconnect | Progress reaches the server |
| [ ] | Cancel a download midway | Cleans up; can restart |
| [ ] | Delete a download | Space freed, book streams again |
| [ ] | Remove the server | Android: its downloads are deleted. iOS: check and note what happens |
| [ ] | Phone storage nearly full | Clear error, no crash |
| [ ] | Download an ebook | Completes |
| [ ] | Ebook open-with: iOS share sheet to Books or a reader; Android open-with, falling back to share | The file opens in the other app (no in-app reader; known gap) |
| [ ] | Android: ebook "open in another app" appears only after the download finishes | As described |

#### Table G: network and security

| Done | Test | Expected |
| --- | --- | --- |
| [ ] | HTTP to a LAN address | Works on both platforms. iOS asks for local network permission the first time: allow it |
| [ ] | iOS: deny the local network prompt | A sensible error; re-enabling in Settings fixes it |
| [ ] | HTTPS with a valid certificate | Works |
| [ ] | HTTPS with a self-signed or wrong-host certificate | Refused with an error (system trust only, no user-added CAs on Android) |
| [ ] | Server restarts during playback | Playback continues from buffer, reports queue and flush later |
| [ ] | Switch Wi-Fi to mobile data with a LAN-only server | Clear unreachable error, queue holds progress |
| [ ] | bookdoe behind a reverse proxy with a path or different port | Works |
| [ ] | Log out | Keychain item (iOS) or encrypted entry (Android) is gone; sign-in required again |

#### Table H: stability

| Done | Test | Expected |
| --- | --- | --- |
| [ ] | Rotate the screen on every screen | No lost state, no crash |
| [ ] | Dark mode and light mode | Readable |
| [ ] | Large text size in system settings | Layout usable |
| [ ] | Library of 1000+ books (optional, later) | Scrolling stays smooth |
| [ ] | Use the app for a full 30-minute listen | No crash, no big battery drain |

#### What to record when something fails

1. Which row failed, and exactly what you did (numbered steps).
2. App, version and build number, platform and OS version, device model.
3. Server type and version, `http` or `https`, account type.
4. What you expected, and what happened. Add a screenshot or a screen recording.
5. Logs:
   - Android: `adb logcat -d > logcat.txt` right after the failure. For a crash,
     the stack trace is in the file. Filter with `adb logcat -d | grep -i bookdoe`
     if it is long.
   - iOS on a Mac: Xcode, Window, Devices and Simulators, select the phone, Open
     Console, or run from Xcode and copy the debug console. Crash logs are under
     Settings, Privacy and Security, Analytics and Improvements, Analytics Data
     (general).
   - Server side: `docker compose logs --since 10m bookdoe` for bookdoe; the
     Jellyfin or Audiobookshelf log for those servers.
6. Remove anything private (tokens, passwords, your real hostnames) from the files
   before attaching them. Device tokens start with `alxd_`.

#### How to file it

1. Open the issue tracker, repository `<owner>/bookdoe`, New Issue.
2. Title: `[ios] or [android] <short symptom> (<provider>)`.
3. Body: the test run header, the steps, expected vs actual, and attach the logs.
4. Labels: platform and provider, if your labels allow it.
5. For a wrong provider assumption, name the row in the assumption table in
   [IOS_APP.md](IOS_APP.md) that looks wrong, and paste the server's real answer.

---

## 3. iOS deployment

Detailed commands: [IOS_BUILD.md](IOS_BUILD.md).

### 3.1 Mac prerequisites

- [ ] Xcode 15.4 or later from the App Store. Open it once, accept the licence,
      install the iOS simulator.
- [ ] XcodeGen: `brew install xcodegen`.
- [ ] Signed in to Xcode with the paid developer account: Xcode, Settings, Accounts.
- [ ] A clone of the repository.

### 3.2 Set the bundle id and team id

1. The bundle id is the one on the App Store Connect app record (My Apps, your app,
   App Information, Bundle ID). It is permanent for that record. It is not committed
   to the repo.
2. `ios/project.yml` keeps only generic defaults (`org.bookdoe`,
   `com.bookdoe.app`). CI injects the real id from the `BUNDLE_ID` secret as
   `PRODUCT_BUNDLE_IDENTIFIER`. The Keychain service and the background download
   session id are derived at runtime from `Bundle.main.bundleIdentifier`, so they
   follow the real id automatically.
3. Find your team id: developer.apple.com, Account, Membership details. It has 10
   characters.
4. Either tick "Automatically manage signing" and pick the team in Xcode, or put the
   team id into `DEVELOPMENT_TEAM` in `project.yml`. Do not commit a personal team
   id if the repo is public; use Xcode's setting instead.
5. Regenerate: `cd ios && xcodegen generate`. The `Bookdoe.xcodeproj` is
   git-ignored and must be regenerated after each change to `project.yml`.

### 3.3 Run on a simulator, then a physical iPhone

1. `cd ios && xcodegen generate && open Bookdoe.xcodeproj`.
2. Expect compile errors the first time. The app target has only ever compiled in
   CI. Fix them in the SwiftUI or AVFoundation code; the logic is in the package.
3. Select an iPhone simulator and press Run. Add the test server
   `http://<your-server>:8795`. The simulator uses the Mac's network.
4. On the iPhone: Settings, Privacy and Security, Developer Mode, turn on and
   restart (general; required for iOS 16 and later).
5. Plug the iPhone in with a cable. Tap Trust on the phone and enter the passcode.
6. In Xcode pick the iPhone as the destination and press Run.
7. First install: on the phone go to Settings, General, VPN & Device Management and
   trust your developer certificate.
8. Run the manual matrix (section 2c) on the phone.

- [ ] App runs on the simulator.
- [ ] App runs on a physical iPhone.
- [ ] Matrix pass done for at least Bookdoe.

### 3.4 Archive and upload by hand (first upload)

Do the first upload by hand with Xcode, so you see any error message directly.

1. Set `MARKETING_VERSION` (for example `0.1.0`) and a new `CURRENT_PROJECT_VERSION`
   in `project.yml`. Regenerate. App Store Connect rejects a duplicate build number.
2. Add the app icon: a 1024x1024 PNG in
   `ios/App/Assets.xcassets/AppIcon.appiconset`, referenced in `Contents.json`
   (`"filename": "icon.png"`).
3. Create the app record (general; details in IOS_BUILD.md): App Store Connect,
   Apps, plus, New App. Platform iOS, name "bookdoe" (or your final name), pick
   the bundle id from the list (it appears after you register it under
   Certificates, Identifiers & Profiles, Identifiers, or after Xcode's automatic
   signing registers it), SKU any unique string.
4. In Xcode: Product, Destination, "Any iOS Device (arm64)".
5. Product, Archive.
6. In the Organizer: Distribute App, App Store Connect, Upload. Automatic signing
   creates the distribution certificate and profile.
7. Wait for processing (10 to 30 minutes). The build appears in App Store Connect,
   TestFlight tab.
8. If asked about export compliance: the app sets `ITSAppUsesNonExemptEncryption`
   to false, so the question is answered for you.

### 3.5 TestFlight internal testing

1. App Store Connect, your app, TestFlight, Internal Testing, plus, create a group.
2. Add yourself (and up to the allowed number of other team members with an App
   Store Connect role) as testers.
3. Add the processed build to the group.
4. Install the TestFlight app on the iPhone, sign in with the same Apple ID, install
   Bookdoe.
5. Internal testing does not need Beta App Review (IOS_BUILD.md notes that only
   external testers need a short beta review).

- [ ] Build visible in TestFlight.
- [ ] Installed through TestFlight on a physical iPhone.
- [ ] Matrix re-run on the TestFlight build (it is a release configuration build).

### 3.6 TestFlight from GitHub Actions

Workflow: `.github/workflows/ios.yml`, job `testflight`. It runs only on a `v*` tag
or a manual run with "upload" ticked, and only when the secrets exist. It also
needs the simulator build job to pass first. If the secrets are missing it skips
without failing.

Set these under GitHub, Settings, Secrets and variables, Actions, as repository
secrets:

| Secret | How to create it |
| --- | --- |
| `APPLE_TEAM_ID` | developer.apple.com, Account, Membership details. 10 characters. |
| `BUNDLE_ID` | The bundle id you chose, exactly as registered. |
| `ASC_KEY_ID` | App Store Connect, Users and Access, Integrations, App Store Connect API, create a key with the "App Manager" role. Copy its Key ID. |
| `ASC_ISSUER_ID` | Same page, the Issuer ID shown at the top. |
| `ASC_KEY_P8_BASE64` | Download the `.p8` once (you cannot download it again). Then run `base64 -i AuthKey_XXXX.p8` and paste the output. |
| `DIST_CERT_P12_BASE64` | An Apple Distribution certificate as a `.p12`. Steps (general): Keychain Access, Certificate Assistant, Request a Certificate from a Certificate Authority, save the request file. developer.apple.com, Certificates, plus, Apple Distribution, upload the request, download the `.cer`, double-click to install. In Keychain Access find it under My Certificates, right-click, Export, choose `.p12`, set a password. Then `base64 -i dist.p12`. |
| `DIST_CERT_PASSWORD` | The password you set during that export. |
| `PROVISIONING_PROFILE_BASE64` | developer.apple.com, Profiles, plus, "App Store Connect" distribution type, choose the app id for your bundle id and the Apple Distribution certificate, download the `.mobileprovision`, then `base64 -i file.mobileprovision`. |

Steps to run it:

1. Make sure the bundle id in `project.yml` matches `BUNDLE_ID` and the app record
   exists.
2. Merge your changes to `main` and wait for green runs.
3. Either push a tag (section 5) or open GitHub, Actions, `ios`, Run workflow, tick
   "upload".
4. The workflow imports the certificate into a temporary keychain, archives with
   manual signing, exports with `method: app-store-connect`, uploads with the API
   key, and deletes the key material at the end.
5. The CI build number (`CURRENT_PROJECT_VERSION`) is the GitHub run number, so
   builds do not collide. The marketing version still comes from `project.yml`.
6. Check App Store Connect for the processed build.

Notes:

- Delete the local `.p12`, `.p8` and password copies from shared places after you
  store them in a password manager. Never commit them.
- If the upload fails with a signing error, see troubleshooting.

### 3.7 App Review notes

Fill these in under App Store, your app version, App Review Information.

1. Review notes text (adapt it):

   > bookdoe is a client for the user's own self-hosted media servers
   > (bookdoe, Jellyfin and Audiobookshelf). The app does not contain content
   > and has no accounts of its own. A demo server and demo account are given
   > below for review.

2. ATS justification (App Transport Security): the global `NSAllowsArbitraryLoads` exception is removed (F-1), so no blanket justification is needed. Plain HTTP is allowed only to local-network hosts (`NSAllowsLocalNetworking`), and public servers must be HTTPS. If a specific non-HTTPS host is ever required, scope an exception to that exact domain under `NSExceptionDomains` and state the App Review justification in this document (the old blanket justification no longer applies). Local network use is still described to users by `NSLocalNetworkUsageDescription`.
3. Sign-in required: untick "Sign-in required" only if you really offer no login. The
   app does need a server. So tick it and provide credentials.
4. Demo server: Apple reviewers need a reachable server with a login. You must
   provide it. Recommendation:
   - Run a separate public demo bookdoe with only public-domain books.
   - Put it behind HTTPS on a domain you control, with a normal user account (not
     admin) and a password that you can rotate after review.
   - Add Jellyfin and Audiobookshelf demo servers only if you want the reviewers to
     see those; one working server is enough, and bookdoe is the primary one.
   - Keep it up for the whole review period, and check it from outside your network
     on the day you submit.
   - Say in the notes which server type to choose and give the URL and credentials.
5. Contact details: name, phone, email for the reviewer.

### 3.8 Privacy nutrition label answers

From IOS_BUILD.md: the app sends no data anywhere except the servers the user adds.

- [ ] Data collection question: choose "Data Not Collected".
- [ ] Tracking: no.
- [ ] Privacy policy URL: required (see section 6). A short page is enough: the app
      talks only to servers the user configures and collects nothing.

Re-check the wording against Apple's current form when you submit. If you later add
crash reporting or analytics, this answer must change.

### 3.9 Other App Store Connect items

- [ ] Screenshots for 6.7 inch and 6.5 inch phones (and iPad if
      `TARGETED_DEVICE_FAMILY` stays "1,2").
- [ ] Description, keywords, support URL, category (Books or Entertainment).
- [ ] Age rating questionnaire.
- [ ] Export compliance: already set false in the app.
- [ ] Background audio: `UIBackgroundModes` declares `audio` (and `fetch`). Be ready
      to say "plays audiobooks in the background".

### 3.10 From internal TestFlight to the App Store

1. Internal testers on the build for a few days. Fix bugs, upload a new build number
   for each fix.
2. External beta (optional): TestFlight, External Testing, create a group, add
   testers by email or a public link, add the build. The first external build goes
   through Beta App Review (short). Fill in the Test Information, including the demo
   server details.
3. App Store release: App Store tab, the version, attach the finished build, fill
   in all metadata, App Review notes (3.7) and privacy (3.8), then Add for Review
   and Submit.
4. Choose the release option: manual release is safest for a first version.
5. After approval, release it. Watch for crashes (Xcode Organizer, App Store
   Connect).

---

## 4. Android deployment

Detailed commands: [ANDROID_BUILD.md](ANDROID_BUILD.md).

### 4.1 Prerequisites

Pick one path.

Android Studio:

- [ ] Android Studio (Ladybug or later) with the Android 15 (API 35) SDK.
- [ ] File, Open, choose the `android` folder, wait for the Gradle sync.
- [ ] Run on an emulator (API 26 or newer) or a phone with USB debugging.
- [ ] From an emulator, the host machine is reached by the emulator's special
      loopback alias address, not `localhost` (see the Android emulator networking
      docs). A real test server on the LAN is simpler.

Command line:

- [ ] JDK 17 and the command-line SDK tools with `platforms;android-35` and
      `build-tools;35.0.0`.
- [ ] `cd android && ./gradlew :app:assembleDebug`.
- [ ] Without an SDK: use Docker with `android/ci/build.sh`.

### 4.2 Install the CI debug APK on a phone

The quickest way to get the app on a phone, with no signing.

1. GitHub, Actions, the `android` workflow, open a green run.
2. Under Artifacts download `bookdoe-debug-apk` and unzip it. It holds
   `app-debug.apk`.
3. Option A (cable): enable Developer options and USB debugging on the phone, then
   `adb install -r app-debug.apk`.
4. Option B (sideload): copy the file to the phone, open it, and allow installs from
   that source when asked.
5. Debug builds use the application id suffix `.debug`, so they install next to a
   release build.

- [ ] Debug APK installed and opens.

### 4.3 Decide the application id

1. Choose a reverse-DNS id, for example `com.yourname.bookdoe`. It cannot change
   after the app is published.
2. Set it in two places:
   - `android/app/build.gradle.kts`, `applicationId` (marked `TODO(maintainer)`)
   - `.github/workflows/android.yml`, `packageName` in the Play upload step
3. Commit, and let CI run.

### 4.4 Generate the upload keystore

Do this once. Keep it outside the repository.

```sh
keytool -genkeypair -v -keystore bookdoe-upload.jks -alias upload \
  -keyalg RSA -keysize 4096 -validity 10000
```

- [ ] Store `bookdoe-upload.jks`, the store password, the alias and the key
      password in a password manager (attach the file).
- [ ] Make an offline backup copy.
- [ ] Do not put it in the repo or in a synced public folder.

With Play App Signing (recommended) this is only the upload key. If it is lost it
can be reset through Play Console support.

To build a signed bundle locally, set these, then run the build:

```sh
export BOOKDOE_KEYSTORE_PATH=/path/outside/repo/bookdoe-upload.jks
export BOOKDOE_KEYSTORE_PASSWORD=<store-password>
export BOOKDOE_KEY_ALIAS=upload
export BOOKDOE_KEY_PASSWORD=<key-password>
cd android && ./gradlew :app:bundleRelease
```

Output: `android/app/build/outputs/bundle/release/app-release.aab`. Without the
variables the release build is unsigned and Gradle prints a warning. You can also put
the same names in `~/.gradle/gradle.properties`.

### 4.5 Play Console account and app

1. Create a Google Play developer account (one-time fee) (general). Complete identity
   verification. Personal and organisation accounts have different rules: check
   Google's current requirements.
2. Create app: name "bookdoe" (or the final name), default language, type "App",
   free, accept the declarations.
3. Use the application id from section 4.3. Play ties the app to the id of the first
   uploaded bundle.

### 4.6 Closed testing requirement for new personal accounts

This repo does not cover it. Check Google's current rules before you plan the
release: Google has had requirements for new personal developer accounts to run a
closed test with a minimum number of testers for a minimum number of days before they
can apply for production access. Do not rely on any number written here. Read the
"Testing requirements for new personal developer accounts" help page in Play Console
before planning dates.

- [ ] Checked Google's current rules and noted the tester count and days required.
- [ ] If required: recruit the testers early and start the closed test as soon as the
      internal test passes.

### 4.7 Internal testing, manual AAB first

1. Build a signed AAB (4.4) or download `bookdoe-release-aab` from a workflow run
   (needs the secrets from 4.8).
2. Play Console, your app, Testing, Internal testing, Create new release.
3. Accept Play App Signing when asked.
4. Upload the `.aab`. Add release notes.
5. Testers tab: create an email list, add testers, save. Copy the opt-in link.
6. Save, Review release, Start rollout to internal testing. Internal testing does
   not require review.
7. On the phone, open the opt-in link with a tester account, accept, then install
   from Play.
8. Fill in the store listing basics once: short and full description, 512x512 icon,
   feature graphic, two phone screenshots.

- [ ] First AAB uploaded and installable from the internal track.
- [ ] Matrix re-run on the Play-installed release build (R8 minification is on, so
      this build can behave differently from debug).

### 4.8 Internal testing from GitHub Actions

Workflow: `.github/workflows/android.yml`, job `release`. It runs on a `v*` tag or a
manual run with "release" ticked. Without the signing secrets it skips cleanly.

| Secret | How to create it |
| --- | --- |
| `ANDROID_KEYSTORE_BASE64` | `base64 -w0 bookdoe-upload.jks` (on a Mac use `base64 -i bookdoe-upload.jks`) |
| `ANDROID_KEYSTORE_PASSWORD` | The store password from 4.4. |
| `ANDROID_KEY_ALIAS` | The alias (`upload` in the example). |
| `ANDROID_KEY_PASSWORD` | The key password from 4.4. |
| `PLAY_SERVICE_ACCOUNT_JSON` | Optional. Google Cloud Console, IAM, Service accounts, create one and a JSON key. Then Play Console, Users and permissions, invite that account's email with release permission for the app (ANDROID_BUILD.md: "Release to testing tracks"). Paste the whole JSON. |

Behaviour:

- With only the four signing secrets: the workflow builds a signed AAB and keeps it
  as the artifact `bookdoe-release-aab`. You upload it by hand.
- With `PLAY_SERVICE_ACCOUNT_JSON` too: it uploads to the `internal` track with
  status "completed".
- Do the first upload by hand (4.7) before using the service account. Play needs the
  app to exist and the first release to be created in the console (general).
- `versionCode` must go up for every upload (section 5).

### 4.9 Play Console forms (App content)

From ANDROID_BUILD.md:

- [ ] Privacy policy URL (section 6): says the app talks only to servers the user
      configures and collects nothing.
- [ ] Ads: no.
- [ ] Data safety: "no data collected or shared". Progress and credentials go only to
      the user's own server. Re-check the wording against Google's current form.
- [ ] Target audience: 18+ or 13+, your choice.
- [ ] News app: no. COVID app: no. Government app: no.
- [ ] Foreground services. The app declares two types. Give a justification for
      each:
      - `FOREGROUND_SERVICE_MEDIA_PLAYBACK`: "plays audiobooks in the background with
        media controls".
      - `FOREGROUND_SERVICE_DATA_SYNC`: "downloads books the user selected for
        offline use".
- [ ] Cleartext traffic. The network security config allows plain HTTP. Play does not
      block this. If asked, say: "connects to the user's own media server, which may
      be HTTP on a private network; HTTPS servers are validated normally." To avoid
      it, set `cleartextTrafficPermitted="false"` in
      `android/app/src/main/res/xml/network_security_config.xml`.
- [ ] Content rating questionnaire (general).
- [ ] Reviewer access: if Google asks for login details, give the same demo server as
      for Apple (section 3.7).

### 4.10 Closed testing, then production

1. Promote or create a closed testing release (Testing, Closed testing) when the
   internal build is good. Meet the requirement from 4.6.
2. Production: Production, Create new release, choose the same AAB or upload a new
   one, add release notes, Review release.
3. Choose a staged rollout (for example a small percentage first) so you can halt it
   (section 5).
4. Submit for review. The first production review can take days (general).
5. After approval, raise the rollout percentage in steps while you watch crashes and
   ratings in Play Console.

---

## 5. Release checklist, rollback and hotfix

### Version bump places

| What | File and key | Rule |
| --- | --- | --- |
| iOS version shown to users | `ios/project.yml`, `MARKETING_VERSION` | Raise for each store release |
| iOS build number | `ios/project.yml`, `CURRENT_PROJECT_VERSION` | Must be unique per upload by hand. CI overrides it with the GitHub run number |
| Android version name | `android/app/build.gradle.kts`, `versionName` | Shown to users |
| Android version code | `android/app/build.gradle.kts`, `versionCode` | Integer, must be higher on every Play upload |

Keep the iOS and Android `versionName`/`MARKETING_VERSION` the same so a bug report
names one version.

### Release steps

1. [ ] All issues for this release closed or deferred in the issue tracker.
2. [ ] Automated checks green on `main` (section 2a).
3. [ ] Manual matrix done on both platforms for the providers you claim to support.
4. [ ] Bump the four values above in one commit.
5. [ ] Write the changelog: what is new, what is fixed, known gaps (use the "Known
       gaps" lists in IOS_APP.md and ANDROID_APP.md). Put it in the release notes
       fields of both stores and in the GitHub release. The repo has no changelog
       file for the apps today; decide where it lives (section 6).
6. [ ] Merge to `main`.
7. [ ] Tag and push (this starts the upload jobs):

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

   Tags matching `v*` start the TestFlight job in `ios.yml` and the release job in
   `android.yml`. A tag push on the same commit runs both. Use a tag only when the
   secrets are set; without them the jobs skip.
8. [ ] Check the two workflow runs, then the builds in App Store Connect and Play
       Console.
9. [ ] Install the exact uploaded builds and run a short smoke test (add server, sign
       in, play one book for a minute, check progress on the server).
10. [ ] Roll out to testers, then to production (3.10 and 4.10).

### If a build is bad

| Where | What to do |
| --- | --- |
| TestFlight | App Store Connect, TestFlight, the build, Expire Build. Testers can no longer install it. Upload a fixed build with a higher build number. |
| App Store (live) | App Store Connect, Remove from sale only for a severe problem. Otherwise ship a fix and request expedited review (general). |
| Play staged rollout | Play Console, Production, the release, Halt rollout. Users who already got it keep it; others stop getting it. Then release a fixed build with a higher `versionCode`. |
| Play internal or closed track | Create a new release with a higher `versionCode`. Removing the old release from the track stops new installs. |
| GitHub workflow produced a bad artifact | Delete the artifact and the tag if the tag was wrong: `git tag -d v0.1.0 && git push origin :refs/tags/v0.1.0`. Never reuse a version code or build number that reached a store. |

There is no way to force users back to an older build on either store. The fix is
always a new build with higher numbers.

### Hotfix procedure

1. [ ] Branch from the released tag: `git checkout -b hotfix/0.1.1 v0.1.0`.
2. [ ] Fix, with a test where possible. Run section 2a.
3. [ ] Bump `versionCode`, `versionName`, `MARKETING_VERSION` and the iOS build number
       (patch number up).
4. [ ] Merge to `main`, tag `v0.1.1`, push the tag.
5. [ ] Smoke test the new builds, then release (staged rollout on Play).
6. [ ] Note the fix in the changelog and close the the issue tracker issue.

---

## 6. Decisions and placeholders still open

| Item | Where it lives now | Status | Needed before |
| --- | --- | --- | --- |
| Android application id | `android/app/build.gradle.kts` and `packageName` in `android.yml` (`com.bookdoe.app`, `TODO(maintainer)`) | Open | First Play upload (cannot change after) |
| iOS bundle id | `BUNDLE_ID` secret (value from the App Store Connect record; not committed). `ios/project.yml` holds a generic default | Resolved | App record in App Store Connect |
| iOS bundle id prefix | `ios/project.yml`, `bundleIdPrefix` (generic `org.bookdoe`) | Resolved | Same time as bundle id |
| Apple team id | `APPLE_TEAM_ID` secret, or chosen in Xcode | Open | First device run |
| App name | "bookdoe" is the working name in both store docs | Open (confirm the name is available on both stores) | Store listings |
| Privacy policy URL | Does not exist | Open | TestFlight external testing, App Store, Play data safety |
| App icons | iOS: `AppIcon.appiconset` needs a 1024x1024 PNG. Android: placeholder vector in `android/app/src/main/res/drawable/ic_launcher_foreground.xml`; Play needs a 512x512 icon and a feature graphic | Open | Store submission |
| Screenshots | None | Open | Store submission |
| Demo server for review | None | Open. You must provide a reachable demo server with a login (3.7) | App Review, and Play if asked |
| Keep cleartext HTTP on | On in both apps | Decide: keep for LAN users, or turn off for an HTTPS-only app | Store review |
| Changelog location | None for the apps | Open | First release |
| Support URL and contact email | None | Open | Store listings |
| Minimum bookdoe server version | Password login needs server 0.9.1 or later | Decide how the store text says it | Store listings |
| Jellyfin and Audiobookshelf support claim | Unverified against real servers | Decide after the matrix whether the store text lists them | Store listings |
| F-Droid | Possible (ANDROID_BUILD.md) | Optional, later | Not needed now |
| CarPlay, Android Auto browsing, in-app EPUB reader | Known gaps | Out of scope for v0.1 | None |

---

## 7. Troubleshooting

### iOS

| Symptom | Likely cause and fix |
| --- | --- |
| Xcode cannot open the project or `Bookdoe.xcodeproj` is missing | It is generated and git-ignored. Run `cd ios && xcodegen generate`. Regenerate after any `project.yml` change. |
| `xcodegen: command not found` | `brew install xcodegen`. |
| Compile errors in `ios/App` on the first build | Expected (the app target has only compiled in CI). Fix the SwiftUI or AVFoundation lines. Check the CI log of the `simulator-build` job to compare. |
| "Signing requires a development team" | Pick a team in Signing & Capabilities, or set `DEVELOPMENT_TEAM` and regenerate. |
| "No profiles for ... were found" or bundle id unavailable | The bundle id is still `com.example...` or is taken. Choose your own in `project.yml` and regenerate. |
| "Untrusted developer" on the phone | Settings, General, VPN & Device Management, trust the certificate. Developer Mode must be on. |
| Phone not listed as a destination | Cable, Trust prompt, Developer Mode, and the phone must be unlocked. |
| Upload rejected: build number already used | Raise `CURRENT_PROJECT_VERSION`. |
| CI `testflight` job fails at the archive step | Profile and certificate must match: App Store profile for the same `BUNDLE_ID`, made with the same Apple Distribution certificate you exported. Re-create the profile after a new certificate. |
| CI job skips with "Signing secrets are not configured" | `ASC_KEY_ID` or `DIST_CERT_P12_BASE64` is empty. Set all seven secrets. |
| Local network prompt never shown, LAN server unreachable | Delete and reinstall the app, then allow Local Network in Settings, Bookdoe. |
| Plain HTTP server blocked | Expected since F-1: `NSAllowsArbitraryLoads` is removed, so ATS blocks plain HTTP to non-local hosts. Plain HTTP works only for local-network hosts (`NSAllowsLocalNetworking`). Use HTTPS, or scope an exception to the exact domain under `NSExceptionDomains`. |

### Android

| Symptom | Likely cause and fix |
| --- | --- |
| Gradle sync fails, "SDK location not found" | Install the SDK, set `ANDROID_HOME` or `android/local.properties` (`sdk.dir=...`). Or use `android/ci/build.sh`. |
| "Failed to find Build Tools 35.0.0" or platform 35 missing | Install `platforms;android-35` and `build-tools;35.0.0` from SDK Manager. |
| Wrong Java version | Gradle needs JDK 17. Set the Gradle JDK in Android Studio settings. |
| Docker build on an arm64 host fails with `aapt2` errors | Register amd64 emulation once: `docker run --privileged --rm tonistiigi/binfmt --install amd64`. |
| Release build "unsigned" warning | The four `BOOKDOE_KEYSTORE_*`/`BOOKDOE_KEY_*` variables are missing or the path does not exist. |
| Play rejects the upload: version code already used | Raise `versionCode`. |
| Play rejects: package name mismatch or `com.example` not allowed | Set your own application id (4.3). |
| CI release job skips | `ANDROID_KEYSTORE_BASE64` or `ANDROID_KEYSTORE_PASSWORD` is empty. |
| Play upload step fails with a permission error | The service account was not invited in Play Console with release permission, or it has not accepted yet. |
| App cannot reach an `http://` server | Check that `network_security_config.xml` still has `cleartextTrafficPermitted="true"`. From an emulator, `localhost` is the emulator itself, so use the host alias or a LAN address. |
| HTTPS server with a private CA fails | The app trusts the system store only, not user-added CAs. Use a public certificate. |
| Debug and release installs clash | They should not: debug uses the `.debug` id suffix. Uninstall the old one if signatures differ. |
| Release build crashes but debug works | R8 minification is on for release. Check `adb logcat` for `ClassNotFoundException` or serialization errors and add keep rules in `android/app/proguard-rules.pro`. |

### Provider sign-in and data problems

Check the assumption tables in [IOS_APP.md](IOS_APP.md) first. The Android providers
are line-for-line ports, so the same tables apply.

| Symptom | What to check |
| --- | --- |
| Jellyfin: sign-in fails | The app posts `/Users/AuthenticateByName` with `{Username, Pw}` and a `MediaBrowser` Authorization header. Check the server version is 10.9+, the user has a password, and no reverse proxy strips the `Authorization` header. |
| Jellyfin: signed in but the library is empty | The app looks for libraries with `CollectionType == "books"`. Check the library type. This audiobook-library shape is marked "unverified" in IOS_APP.md. |
| Jellyfin: multi-file book shows as separate items, or wrong order | Assumption: a multi-file book is a `Folder` or `MusicAlbum` whose children carry `IndexNumber`. Check the server's real answer and fix `JellyfinProvider.swift` or `JellyfinProvider.kt`. |
| Jellyfin: no author, series or chapters | Assumption: `Artists`/`AlbumArtists` hold the author, `SeriesName` the series, `Chapters[].StartPositionTicks` the chapters. Unverified. |
| Jellyfin: playback fails but browsing works | The stream uses `/Audio/{id}/universal?api_key=...` with the token in the query. A proxy that blocks query tokens breaks it. |
| Audiobookshelf: sign-in fails | The app calls `GET /status` then `POST /login` with `x-return-tokens: true`. Check the version is 2.26+, and that the login is a local username and password (OIDC-only servers are not supported by the app's sign-in form). |
| Audiobookshelf: logged out after a while | Token refresh uses `POST /auth/refresh` with `x-refresh-token`. Needs 2.26+. |
| Audiobookshelf: books list but will not play | The app streams the direct file route `/api/items/{id}/file/{ino}` with a Bearer header, not the `/public/session` URLs. IOS_APP.md says `sessionTracks(from:)` is there to switch if the file route proves unreliable. |
| Audiobookshelf: progress not saved | Progress goes through `/api/session/{id}/sync`, or `PATCH /api/me/progress/{id}` when no session is open. Check the server log for 4xx. |
| bookdoe: password login refused | The server is older than 0.9.1, or runs in `proxy`, `none` or OIDC mode. Use the pairing code. Check `GET /api/player/v1/capabilities` for `auth.password_login`. |
| bookdoe: 429 on sign-in | Five failed attempts lock the IP and username for 60 seconds. Wait. |
| bookdoe: 403 after working | The account may be `pending`, or the device was revoked in the web Player. |
| bookdoe: progress jumps back | That is the stale-report rule working. A newer position on the server wins. See "Conflict rule" in [PLAYER_API.md](PLAYER_API.md). |
| Any provider: works on Wi-Fi, fails on mobile data | The server is LAN only. Expose it over HTTPS first (see INSTALL.md). |

When a provider answer does not match the table, capture the real response (with the
token removed), file a the issue tracker issue (section 2c) and fix the single provider file.
