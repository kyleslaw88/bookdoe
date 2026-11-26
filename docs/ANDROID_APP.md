# bookdoe for Android: what it does and what is assumed

Source: `android/` (Kotlin, Jetpack Compose, Material 3; `core` is a pure JVM
module, `app` the Android module). Build and ship: `docs/ANDROID_BUILD.md`.

The Android app is a port of the iOS app (`docs/IOS_APP.md`) with the same provider
design, the same models and the same unit-test scenarios, so the two stay in step.

## Features

- Several saved servers of three kinds behind one `MediaProvider` interface:
  bookdoe (Player API v1), Jellyfin (10.9+) and Audiobookshelf (2.26+).
- Sign-in: username and password for all three; bookdoe additionally by pairing
  code (RFC 8628-shaped device flow). Credentials live in
  `EncryptedSharedPreferences` (AES-256-GCM, key in the Android Keystore), one
  entry per server; the server list itself holds no secret and nothing is included
  in cloud backup or device transfer.
- Browse, search (debounced), sort, filter by audiobook/ebook, paging as you
  scroll; Home with "continue listening" and recently added, pull to refresh; detail
  with description, chapters, position.
- Playback with Media3 ExoPlayer behind a `MediaSessionService`: foreground
  notification, lock-screen controls, Bluetooth and Android Auto-friendly session,
  audio focus and headphone-unplug handling, multi-file books as one timeline,
  chapter list (from the server or derived from track boundaries), speed, skip
  15/30, sleep timer.
- Progress sync: reports every 15 s while playing and on pause, seek, track change
  and stop through an offline-safe queue (`ProgressQueue`, persisted; coalesced per
  book; flushed when a report succeeds). bookdoe's conflict rule is honoured: a
  stale report returns the server position and the player jumps to it.
- Offline downloads: WorkManager foreground job into app-private storage, an
  index that turns a downloaded book into the same `MediaDetail` the online path
  uses. Offline playback still queues progress for the next flush. Removing a
  server deletes its downloads.
- Ebooks: listed and downloadable; a downloaded ebook is handed to a reader app
  through a `FileProvider` URI (open-with, falling back to share). No in-app EPUB
  renderer in this version.
- Cleartext HTTP to any host is allowed through a network security config so LAN
  servers work; HTTPS servers use normal certificate validation with the system
  trust store. Trade-off and the Play justification: `docs/ANDROID_BUILD.md`.

## Server API assumptions

Identical to the iOS app: the bookdoe routes follow `docs/PLAYER_API.md` and are
exercised by the server's own tests; Jellyfin and Audiobookshelf are implemented
from `docs/COMPAT_PLAYERS.md` and the provider unit tests against fake answers. The
assumption tables in `docs/IOS_APP.md` apply unchanged; the Kotlin providers are
line-for-line ports (`android/core/src/main/kotlin/bookdoe/core/*Provider.kt`).
Nothing was run against a real Jellyfin or Audiobookshelf server from this app.

Differences in what the app sends:

| | iOS | Android |
| --- | --- | --- |
| bookdoe login/pairing `platform` | `ios` | `android` |
| Jellyfin `Authorization: MediaBrowser ... Device="..."` | `iOS` | `Android` |
| Audiobookshelf play session `deviceInfo.manufacturer/model`, `mediaPlayer` | `Apple`/`iPhone`, `AVPlayer` | `Android`/`Android`, `ExoPlayer` |

## Differences from the iOS app

- Media headers are attached by a `ResolvingDataSource` in the playback service and
  only to URLs under the configured server's base URL; Jellyfin streams also carry
  `api_key` in the query as on iOS.
- Ebooks are opened from the downloaded file (Android apps cannot hand a URL with
  an auth header to another app), so "open in another app" appears after the
  download completes.
- Sleep timer, speed and skip live in the app; the notification shows play/pause
  and the 15/30 s seek buttons provided by Media3.
- Covers are fetched with Coil using the provider's headers and cached by Coil on
  disk (iOS caches in memory only).
- Dynamic colour (Material You) on Android 12+, a warm fixed scheme below.

## Tests

- `android/core/src/test`: every `BookdoeKitTests` scenario ported to JUnit
  (providers, progress queue, stores, download planning, URL helpers, error
  mapping) plus three tests that run the real OkHttp client against MockWebServer.
- `android/app/src/test`: the app model (add/remove server, conflict surfacing,
  offline flush) and download job serialisation. No emulator is needed; CI runs
  `:core:test :app:testDebugUnitTest :app:assembleDebug :app:lintDebug`.

## Known gaps

- No in-app ebook reader; reading position for ebooks is not written by this app.
- Jellyfin "mark played" is sent once at the end; partial progress is per file.
- The notification artwork uses the cover URL through Media3's default loader,
  which does not send the auth header; servers that require a token for covers
  show the app icon in the notification instead.
- No Android Auto browse tree (the session is controllable from the car, but the
  library cannot be browsed there yet); no widgets; no tablet-specific layout.
- Playback and the UI run in one process; a controller reconnect after the system
  kills and restarts the process is not implemented beyond the session's own
  resumption.
