# bookdoe for iOS: what it does and what is assumed

Source: `ios/` (SwiftUI app target plus the `BookdoeKit` SwiftPM package).
Build and ship: `docs/IOS_BUILD.md`.

## Features

- Several saved servers of three kinds behind one `MediaProvider` protocol:
  bookdoe (Player API v1), Jellyfin (10.9+) and Audiobookshelf (2.26+).
- Sign-in: username and password for all three; bookdoe additionally by pairing
  code (RFC 8628-shaped device flow). Credentials live in the Keychain
  (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`), one item per server; the
  server list itself holds no secret.
- Browse, search, sort, filter by audiobook/ebook, paging; Home with "continue
  listening" and recently added; detail with description, chapters and position.
- Playback with `AVQueuePlayer`: multi-file books as one timeline, chapter list (from
  the server, or derived from track boundaries), speed, skip 15/30, sleep timer,
  lock-screen and Control Center controls (`MPRemoteCommandCenter`), background
  audio, interruption handling.
- Progress sync: reports every 15 s while playing and on pause/seek/stop through an
  offline-safe queue (`ProgressQueue`, persisted; coalesced per book; flushed when a
  report succeeds). bookdoe's conflict rule is honoured: a stale report returns
  the server position and the player jumps to it.
- Offline downloads: background `URLSession`, files in Application Support excluded
  from backup, an index that turns a downloaded book into the same `MediaDetail` the
  online path uses. Offline playback still queues progress for the next flush.
- Discover chat (bookdoe servers): a Discover tab with the AI book assistant. It appears only
  when the active server's unauthenticated `GET /api/player/v1/capabilities` says
  `discover_chat: true`; a missing key, `false`, another server type or an unreachable server
  hides it. Capabilities are re-read on launch and on every server switch. A suggested book
  that is in the library opens its detail page. A failed or cancelled reply shows its error
  and the conversation stays usable. Kids chats and profile editing are not in the app.
- Ebooks: listed and downloadable, handed to another app through the share sheet
  (Books, a reader of choice). No in-app EPUB renderer in this version.

## Server API assumptions

bookdoe routes are implemented against `docs/PLAYER_API.md` and are exercised by
the server's own tests. The other two are implemented from `docs/COMPAT_PLAYERS.md`
(server source and the official clients read on 2026-10-08, one live Jellyfin capture)
and from the package's unit tests against fake answers. Nothing below was run
against a real Jellyfin or Audiobookshelf server from this app. Each provider is one
file, so a wrong assumption is a local fix.

### Jellyfin (`JellyfinProvider.swift`)

| Assumption | Confidence |
| --- | --- |
| `POST /Users/AuthenticateByName` with `{Username, Pw}` and `Authorization: MediaBrowser Client=..., Device=..., DeviceId=..., Version=...`; later requests add `Token="..."` | captured against 12.2 |
| `/UserViews`, `/Items` (with `userId`, `parentId`, `recursive`, `includeItemTypes`, `searchTerm`, `sortBy`, `startIndex`, `limit`), `/Items/{id}`, `/UserItems/Resume`, `/UserItems/{id}/UserData` | captured / server source |
| `/Sessions/Playing`, `/Sessions/Playing/Progress`, `/Sessions/Playing/Stopped` with `PositionTicks` (100 ns), `/UserPlayedItems/{id}` | captured / server source |
| `/Audio/{id}/universal?api_key=...&container=...` streams with Range; `api_key` as a query parameter is accepted so AVPlayer needs no header | captured (header auth); query form is standard Jellyfin behaviour, not captured |
| Audiobook libraries are `CollectionType == "books"`; a single-file book is an `AudioBook` item; a multi-file book is a `Folder` or `MusicAlbum` whose children carry `IndexNumber`; per-file positions live in each child's `UserData` | unverified: the shape of audiobook libraries was not in the demo server |
| `Artists` / `AlbumArtists` hold the author, `SeriesName` the series, `Chapters[].StartPositionTicks` the chapters | unverified |

### Audiobookshelf (`AudiobookshelfProvider.swift`)

| Assumption | Confidence |
| --- | --- |
| `GET /status` (`serverVersion`, `isInit`, `authMethods`) | server source |
| `POST /login` with header `x-return-tokens: true` returns `user.accessToken`, `user.refreshToken`, `userDefaultLibraryId` | server source, official app |
| `POST /auth/refresh` with `x-refresh-token` returns a new `user.accessToken` (and refresh token) | server source, Plappa capture |
| `GET /api/libraries`, `/api/libraries/{id}/items?limit&page&sort&desc&minified=1&include=progress` returns `results[]`, `total` | server source, official app |
| `GET /api/libraries/{id}/search?q=` returns `book[].libraryItem` | ShelfPlayer capture (route), response shape from server source |
| `GET /api/libraries/{id}/personalized` returns shelves; the one with `id == "continue-listening"` holds in-progress items | official app |
| `GET /api/items/{id}?expanded=1&include=progress`: `media.metadata`, `media.tracks[]` (or `audioFiles[]`), `media.chapters[]`, `userMediaProgress.currentTime` | official app |
| `GET /api/items/{id}/file/{ino}` streams a track with Bearer and Range | Plappa capture |
| `POST /api/items/{id}/play` opens a session (`id`, `audioTracks[].contentUrl` under `/public/session/...`); `POST /api/session/{id}/sync {currentTime, duration, timeListened}`; `POST /api/session/{id}/close` | server source, official app |
| `PATCH /api/me/progress/{id} {currentTime, duration, progress, isFinished}` when no session is open | official app |
| `GET /api/items/{id}/cover?width=` with Bearer | server source |
| Minified listings use `authorName` / `narratorName` strings, expanded ones `authors[]` / `narrators[]` | official app (both handled) |

The app streams Audiobookshelf through the direct file route (Bearer header via
`AVURLAssetHTTPHeaderFieldsKey`) rather than the session-scoped `/public/session`
URLs; `sessionTracks(from:)` is there to switch if the file route proves
unreliable.

### bookdoe

Everything is in `docs/PLAYER_API.md`. The password login (`POST /auth/login`) and
the `setup_required` / `auth.password_login` capability fields were added for this
app (server 0.9.1+); an older server still works through pairing.

## Known gaps

- No in-app ebook reader; reading position for ebooks is not written by this app.
- Jellyfin "mark played" is sent once at the end; partial progress is per file.
- Cover images are cached in memory only.
- iPad layout is the phone layout scaled; no sidebar.
- No widgets, no Siri intents. (CarPlay Audio is supported; see docs/CARPLAY_SETUP.md.)
