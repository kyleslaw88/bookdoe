# Client Compatibility Matrix and Minimal Endpoint Set for Audiobook Players

**Spike for issue #210.** Researched 2026-10-08. No server code.

## 0. How to read the verification marks

Every endpoint row carries exactly one of these marks. Nothing is marked verified unless the evidence is named in the row.

| Mark | Meaning |
|------|---------|
| **capture** | Seen in a request trace. Either run by us (section 4) or quoted from a third-party bug report that pasted a real client trace (cited by URL). |
| **client-src** | Read in the client's own public source (file and line cited). |
| **server-src** | The route exists in the server's router or controller (file and line cited), but we have no evidence that this client calls it. |
| **inferred** | Reasoned from the server API plus the client's behaviour. Unverified. |

Source references are to the default branch as fetched on 2026-10-08 (ABS server v2.37.1, released 2026-09-29; Jellyfin server v12.2, released 2026-10-05). Line numbers drift, so the route text is quoted too.

Limits of this spike, stated up front:

* **Plappa's source is not public.** `LeoKlaus/plappa` holds a README, issue templates and images only (checked through the GitHub tree API). Plappa rows can only be **capture** (quoted from user-pasted logs in its issue tracker), **server-src** or **inferred**.
* **ShelfPlayer's source is gone.** `rasmuslos/ShelfPlayer` was reduced to a single "Goodbye" commit on 2026-07-14: one README and no history, no tags and no releases. ShelfPlayer rows are **capture** (from a user-pasted request log in its issue #456) or **inferred**.
* **No ABS capture was run by us.** There is no Docker or Podman in this sandbox and no public ABS demo server. Building ABS from source needs native modules and an audio library, and was not attempted. ABS rows are therefore **client-src** (the official app's source) or **server-src**, plus the third-party traces above.
* **A Jellyfin capture was run by us**, against the public Jellyfin demo (`https://demo.jellyfin.org/stable`, server 12.2.0). The demo has no audiobook library (Movies, Music, Playlists, Shows only), so the playback trace uses a music track. Request and response shapes are identical for the `Audio` item type.

---

## 1. Client matrix

| Client | Platform | Server API | Version | Login method | Evidence |
|---|---|---|---|---|---|
| **Plappa** (the client the owner named in the issue) | iOS and iPadOS. Mac and Apple TV are only on its roadmap. | Audiobookshelf **or** Jellyfin. The user picks one server type at login. | ABS: no minimum published. User-reported working against ABS 2.36.0 with Plappa 1.6.2 (build 81). Jellyfin: no minimum published. | **ABS:** username and password (server `/login`); OIDC (a user reports signing in through Authelia, plappa#211); ABS API key (plappa#491 quotes the Plappa Settings suggestion to "create an API key in ABS and enter it in Plappa"). **Jellyfin:** not documented anywhere we could read. Username and password is assumed (**inferred**). | README: <https://github.com/LeoKlaus/plappa/blob/main/readme.md> says only "audiobook player/client for Jellyfin and AudioBookShelf". The login claims come from user-pasted logs and reports: <https://github.com/LeoKlaus/plappa/issues/211>, <https://github.com/LeoKlaus/plappa/issues/491>. The README does **not** mention API key or OIDC. An earlier draft of this doc claimed it did. That was wrong. |
| **Audiobookshelf official app** | Android and iOS (the repo has both `android/` and `ios/` projects). The iOS distribution channel was not verified. An earlier draft said TestFlight with no source. | ABS only | Token refresh against `/auth/refresh` needs server **2.26.0** or later (release notes: <https://github.com/advplyr/audiobookshelf/releases/tag/v2.26.0>). The `/public/session/:id/track/:index` stream URL is used from **2.22.0**. Older servers get `/api/items/:id/file/:ino?token=` (`AudioPlayer.swift` lines 564-568). | Username and password (`POST /login`, `ServerConnectForm.vue` line 642). OIDC with PKCE through `/auth/openid` (`ServerConnectForm.vue` line 292). The app's API-key support was not found in the source we read (**unverified**). | <https://github.com/advplyr/audiobookshelf-app> |
| **ShelfPlayer** | iOS and iPadOS | ABS only | v3.3.0 was the last release (README). It predates ABS 2.35.0, and issue #456 reports library browsing failing against ABS 2.35.0. The minimum ABS version is not documented. | OIDC: yes (issue #431 is an OIDC login failure, and PR #294 fixes OIDC callback strings). Username and password: **unverified**, no citation found. API key: **unverified**. | <https://github.com/rasmuslos/ShelfPlayer/issues/431>, <https://github.com/rasmuslos/ShelfPlayer/pull/294>, <https://github.com/rasmuslos/ShelfPlayer/issues/456>. Archived, with the IP sold to Space Mushrooms (README "Goodbye"). Source removed from GitHub. |
| **Jellyfin-API players, represented by jellyfin-web** (the reference Jellyfin client; Jellyfin Desktop wraps a web UI, but whether that is jellyfin-web was **not verified**) | Web, desktop | Jellyfin | The routes in section 2.4 exist from **10.8.13** (`AuthenticateByName`, `Sessions/Playing*`). The newer `UserPlayedItems` and `Items/{itemId}` forms exist from **10.9.0**. Checked by fetching the controllers at tags v10.8.13, v10.9.0, v10.10.0 and v10.11.0. | Username and password: `POST /Users/AuthenticateByName` (`apiClient.js` line 520). Quick Connect: `POST /Users/AuthenticateWithQuickConnect` (`apiClient.js` line 560). API key as `Token=` in the `Authorization: MediaBrowser` header: capture, section 4. OIDC is **not** in core Jellyfin (a plugin). | <https://github.com/jellyfin/jellyfin-apiclient-javascript>, <https://github.com/jellyfin/jellyfin-web> |
| **Emby-based players** | n/a | Emby | **Not researched.** No Emby app was named by the owner or the issue, and Emby's server source is closed. | Not researched | Not researched. Left out of the recommendation on purpose. |
| **OPDS clients** | n/a | OPDS 1.2 or 2.0 | **Not researched.** No audiobook player in this survey uses OPDS. | Not researched | None. Out of scope unless the owner names a client. |

**Which clients the owner uses is not established.** The issue names only Plappa and says "similar apps". The matrix covers Plappa, the official ABS app, ShelfPlayer and a Jellyfin reference client. the owner should strike or add rows (see section 3).

---

## 2. Per-client minimal endpoint sets (first login to playing one book with progress sync)

### 2.1 Audiobookshelf server facts that apply to every ABS client

All of these were read from `advplyr/audiobookshelf` `server/`:

* `Server.js` lines 355-356: `router.use('/api', ...authMiddleware, apiRouter)`, `router.use('/hls', ...)` and `router.use('/public', publicRouter)`. **`/public` has no auth middleware.**
* `Auth.js` line 322: `router.post('/login', ...)`. Line 331: `router.post('/auth/refresh', ...)`. Line 486: `router.post('/logout', ...)`. **These sit at the server root, not under `/api`.** An earlier draft of this doc said `/api/auth/login` and `/api/logout`. Both were wrong.
* `Server.js` line 384: `GET /status`. Line 401: `GET /ping`.
* The login body is `{username, password}`. The response includes `user.accessToken`. `user.refreshToken` is only in the body if the request sends `x-return-tokens: true` (`Auth.js` line 324). Otherwise the refresh token is set as a cookie.
* `/auth/refresh` takes the refresh token in the `x-refresh-token` header, and then returns it in the body (`Auth.js` lines 331-345).
* Playback is **not** `POST /api/session`. It is `POST /api/items/:id/play` (`ApiRouter.js` line 117), then `POST /api/session/:id/sync` (line 242) and `POST /api/session/:id/close` (line 243). `POST /api/session/local` (line 238) and `/api/session/local-all` (line 239) upload offline sessions in bulk.
* The track URL is `GET /public/session/:id/track/:index` (`PublicRouter.js` line 21).
* `GET /api/me/progress/:id/:episodeId?` is line 183. `PATCH /api/me/progress/:libraryItemId/:episodeId?` is line 185. `GET /api/me/progress` (line 176) returns all progress and is not called by the playback path.

### 2.2 Official ABS app (iOS and Android): all rows **client-src**

iOS `ios/App/Shared/util/ApiClient.swift`, Android `android/.../server/ApiHandler.kt`, and the Vue UI under `components/` and `store/`.

| # | Step | Method | Path | Request fields | Response fields used | Evidence |
|---|---|---|---|---|---|---|
| 1 | Probe server | GET | `/status` | none | `serverVersion`, `isInit`, `authMethods`, `authFormData` | `ServerConnectForm.vue` line 618; route `Server.js` line 384 |
| 2 | Login | POST | `/login` | headers `x-return-tokens: true`; body `{username, password}` | `user.{id, username, token or accessToken, refreshToken, mediaProgress[], permissions}`, `userDefaultLibraryId`, `serverSettings` | `ServerConnectForm.vue` line 642; route `Auth.js` line 322 |
| 2b | Login (OIDC alt.) | GET | `/auth/openid` with `code_challenge`, `code_challenge_method=S256`, `redirect_uri=audiobookshelf://oauth`, `client_id=Audiobookshelf-App`, `response_type=code`, then GET `/auth/openid/callback?state&code&code_verifier` | | same user payload | `ServerConnectForm.vue` lines 292 and 386; route `Auth.js` lines 362 and 380 |
| 3 | Authorize saved session | POST | `/api/authorize` | Bearer | `user.mediaProgress[]` | `ApiHandler.kt` line 716; route `ApiRouter.js` line 350 |
| 4 | Refresh token | POST | `/auth/refresh` | header `x-refresh-token` | `user.accessToken`, `user.refreshToken` | `ApiClient.swift` line 185; `store/user.js` line 180; route `Auth.js` line 331 |
| 5 | List libraries | GET | `/api/libraries` | Bearer | `libraries[].{id, name, mediaType}` | `store/libraries.js` line 80; route `ApiRouter.js` line 70 |
| 6 | Home shelves | GET | `/api/libraries/:id/personalized?minified=1&include=rssfeed,numEpisodesIncomplete` | | `[{id, label, type, entities[]}]` | `pages/bookshelf/index.vue` line 245; route `ApiRouter.js` line 82 |
| 7 | List items | GET | `/api/libraries/:id/items?limit=100&minified=1` | query `limit`, `minified`, `sort`, `filter` | `results[]`, `total` | `ApiHandler.kt` line 468; route `ApiRouter.js` line 75 |
| 8 | Item detail | GET | `/api/items/:id?expanded=1&include=progress` | | `media.{metadata, audioFiles, chapters, duration}`, `userMediaProgress` | `ApiClient.swift` line 621; route `ApiRouter.js` (`/items/:id`) |
| 9 | Cover | GET | `/api/items/:id/cover` | optional `width`, `height`, `format` | image | route `ApiRouter.js` line 112 |
| 10 | **Start playback** | POST | `/api/items/:id/play` | `{forceDirectPlay:"1" or "", forceTranscode:"1" or "", mediaPlayer:"AVPlayer", deviceInfo:{deviceId, manufacturer, model, clientVersion}}` | `id` (session id), `audioTracks[].{index, startOffset, duration, contentUrl, mimeType}`, `chapters`, `currentTime`, `duration`, `playMethod`, `libraryItem` | `ApiClient.swift` line 461; route `ApiRouter.js` line 117; response shape `PlaybackSession.toJSONForClient` |
| 11 | Stream track | GET | `/public/session/:sessionId/track/:index` | Range header | audio bytes, 206 | `AudioPlayer.swift` lines 564-566 (ABS 2.22.0 and later); route `PublicRouter.js` line 21 |
| 12 | **Sync progress** | POST | `/api/session/:id/sync` | `{currentTime, duration, timeListened}` | 200 | `ApiClient.swift` line 505 and `PlaybackReport.swift`; `ApiHandler.kt` line 613; server reads `currentTime`, `timeListened`, `duration` in `PlaybackSessionManager.syncSession` |
| 13 | Close session | POST | `/api/session/:id/close` | optional sync body | 200 | `ApiHandler.kt` line 706 (Android). The iOS `ApiClient.swift` has no close call. |
| 14 | Offline upload | POST | `/api/session/local`, `/api/session/local-all` | session objects | 200 | `ApiClient.swift` lines 514 and 526 |
| 15 | Single progress | GET and PATCH | `/api/me/progress/:libraryItemId/:episodeId?` | PATCH body `{currentTime, isFinished, ...}` | `mediaProgress` | `ApiClient.swift` lines 595 and 603 |
| 16 | Logout | POST | `/logout` | header `x-refresh-token` | 200 | `store/user.js` line 158 |

**Minimal path to one book playing with synced progress: rows 2, 8, 10, 11 and 12, plus 5 and 7 for browsing.**

### 2.3 Plappa

Plappa's source is not public, so every row below is **capture** (a third-party trace quoted from a Plappa bug report) or **inferred** (reasoned from the server API, unverified).

**ABS mode:**

| # | Step | Method | Path | Request fields | Response fields used | Evidence |
|---|---|---|---|---|---|---|
| 1 | Status probe | GET | `/status` | none | decoded as a status response | **capture**: plappa#211 log line "Encountered an error while decoding status response" (the server returned HTML) |
| 2 | Login | POST | `/login` | `{username, password}`, or an API key as the Bearer token, or OIDC | access token and refresh token | **inferred**. Login methods are from plappa#211 and #491. The path is not shown in any capture. |
| 3 | Refresh token | POST | `/auth/refresh` | header `X-Refresh-Token` | new access token | **capture**: plappa#491 proxy log `POST /auth/refresh X-Refresh-Token: <REDACTED> -> 200` |
| 4 | Browse | GET | `/api/libraries/<id>/authors` | Bearer | authors | **capture**: plappa#491 proxy log `GET /api/libraries/<LIBRARY_ID>/authors -> 304` |
| 5 | Stream track | GET | `/api/items/<id>/file/<fileId>` | Bearer plus `Range: bytes=0-1`; User-Agent `AppleCoreMedia`; the server returned 401 when the token had expired | audio bytes (206) | **capture**: plappa#491 proxy log. It uses the **file route, not** `/public/session/...`. |
| 6 | Report sessions | POST (path not shown) | probably `/api/session/local` or `/api/session/local-all` | | | **inferred** from the plappa#491 log line "Successfully posted sessions". Whether Plappa opens a live session with `POST /api/items/:id/play` and `/sync` is **unverified**. |
| 7 | Library listing, item detail and cover | GET | `/api/libraries/:id/items`, `/api/items/:id`, `/api/items/:id/cover` | | | **server-src** only. Not seen in any Plappa trace. |

The plappa#491 trace shows one thing for certain: an ABS server must serve `GET /api/items/:id/file/:fileId` with Range support and must accept `X-Refresh-Token` on `/auth/refresh`. Plappa also lists books by author (`/api/libraries/:id/authors`), not only by library items.

**Jellyfin mode:** Plappa's log lines name a `JellyfinAPIHandler` (plappa#211), but no Jellyfin request has been published. The endpoint list for this mode is **inferred**: it is the jellyfin-web sequence in 2.4, with `AuthenticateByName` for login (the only non-obsolete login route, since the `Users/{userId}/Authenticate` route is `[Obsolete]`, `UserController.cs` lines 178-184).

### 2.4 Jellyfin: jellyfin-web, the reference client

Rows are **client-src** for the client side (`jellyfin/jellyfin-apiclient-javascript` `src/apiClient.js`; `jellyfin/jellyfin-web` `src/components/playback/playbackmanager.js` and `src/plugins/htmlAudioPlayer/plugin.js`). Route definitions are in `jellyfin/jellyfin` `Jellyfin.Api/Controllers/`. The right-hand column also marks what **our own capture** (section 4) confirmed against server 12.2.0.

| # | Step | Method | Path | Request fields | Response fields used | Evidence |
|---|---|---|---|---|---|---|
| 1 | Server info | GET | `/System/Info/Public` | none | `ServerName`, `Version`, `Id` | `apiClient.js` line 1266; `SystemController.cs` line 92; **our capture: 200** |
| 2 | Public users | GET | `/Users/Public` | none | `[{Id, Name}]` | `UserController.cs` line 107; **our capture: 200** |
| 3 | **Login** | POST | `/Users/AuthenticateByName` | header `Authorization: MediaBrowser Client, Device, DeviceId, Version`; body `{Username, Pw}`. The field is **`Pw`**, not `Password` (`AuthenticateUserByName.cs`). | `AccessToken`, `User.Id`, `SessionInfo`, `ServerId` | `apiClient.js` line 520; `UserController.cs` line 209; **our capture: 200** |
| 4 | Capabilities | POST | `/Sessions/Capabilities/Full` | device profile | 204 | `apiClient.js` line 2061; `SessionController.cs` line 384 |
| 5 | Libraries (views) | GET | `/UserViews` (new) or `/Users/{userId}/Views` (legacy, still used by apiClient line 2907) | | `Items[].{Id, Name, CollectionType}` | `UserViewsController.cs` lines 65 and 107; **our capture: `/UserViews` 200** |
| 6 | List items | GET | `/Items` | `userId`, `parentId`, `includeItemTypes`, `recursive`, `fields=MediaSources`, `limit`, `startIndex` | `Items[]`, `TotalRecordCount` | `ItemsController.cs` line 171; **our capture: 200** (5 `Audio` items in the demo) |
| 7 | Resume list | GET | `/UserItems/Resume` (10.9+) or `/Users/{userId}/Items/Resume` (apiClient line 2879) | | `Items[]` | `ItemsController.cs` lines 921 and 1027 |
| 8 | Item detail | GET | `/Items/{itemId}` (10.9+) | `userId` | `MediaSources[].{Id, Container}`, `RunTimeTicks`, `UserData` | `UserLibraryController.cs` line 82; **our capture: 200** |
| 9 | Playback info | POST | `/Items/{itemId}/PlaybackInfo` | body `{DeviceProfile}`, query `MediaSourceId`, `UserId` | `MediaSources`, `PlaySessionId` | `apiClient.js` line 1347; `playbackmanager.js` line 504. **Not run by us.** |
| 10 | Stream | GET | `/Audio/{itemId}/universal` (jellyfin-web) or `/Audio/{itemId}/stream?static=true` | query `userId`, `container`, `audioCodec`, `deviceId`; Range | audio bytes | `plugins/htmlAudioPlayer/plugin.js` line 310; `UniversalAudioController.cs` line 92; `AudioController.cs` line 88; **our capture: both returned 206 `audio/mpeg`** |
| 11 | Cover | GET | `/Items/{itemId}/Images/Primary` | optional `maxWidth`, `tag` | image | `ImageController.cs` line 552. **Our capture: 404** for the demo track, which has no primary image. The route exists, but the response shape is not captured. |
| 12 | **Report start** | POST | `/Sessions/Playing` | `{ItemId, PlaySessionId, MediaSourceId, PositionTicks, CanSeek, PlayMethod, IsPaused}` | 204 | `apiClient.js` line 3230; `PlaystateController.cs` line 201; **our capture: 204** |
| 13 | **Report progress** | POST | `/Sessions/Playing/Progress` | `{ItemId, PlaySessionId, PositionTicks, IsPaused, PlayMethod}` | 204 | `apiClient.js` line 3287; `PlaystateController.cs` line 217; **our capture: 204** |
| 14 | **Report stop** | POST | `/Sessions/Playing/Stopped` | `{ItemId, PlaySessionId, PositionTicks}` | 204 | `apiClient.js` line 3413; `PlaystateController.cs` line 247; **our capture: 204** |
| 15 | Mark played | POST | `/UserPlayedItems/{itemId}` (10.9+) | query `userId`, `datePlayed` | `UserItemDataDto` | `PlaystateController.cs` line 72 |
| 16 | Read progress | GET | `/UserItems/{itemId}/UserData` | query `userId` | `PlaybackPositionTicks`, `Played`, `PlayCount`, `LastPlayedDate` | `ItemsController.cs` line 1072; **our capture: 200** |
| 17 | Logout | POST | `/Sessions/Logout` | none | 204 | `apiClient.js` line 498; **our capture: 204** |

Corrections to an earlier draft of this doc:

* The old `POST /Users/{userId}/Authenticate` route is `[Obsolete("Authenticate with username instead")]` and takes `pw` as a **query** parameter. The login route is `POST /Users/AuthenticateByName`. Our capture of the legacy route with an empty `pw` returned 400.
* Start, progress and stop are three different routes (rows 12-14). The draft used `POST /Users/{userId}/PlayingItems/{itemId}` for both start and progress. That route is a legacy start report only. It is `[Obsolete]` in `PlaystateController.cs` (line 321). Our capture of the legacy route returned 204.
* Mark played is `POST /UserPlayedItems/{itemId}` (10.9+).
* `Items/Root` is not on the playback path and is not needed. The `userId` form is `GET /Users/{userId}/Items/Root` (`apiClient.js` line 864).
* Cover is `GET /Items/{itemId}/Images/Primary`. An earlier draft cited "Auth.js ignorePatterns" for the ABS cover path, which was not evidence. The ABS cover route is `ApiRouter.js` line 112.

### 2.5 ShelfPlayer (ABS)

Source unavailable, so these are **capture** (issue #456) or **inferred**.

| # | Step | Method | Path | Request fields | Evidence |
|---|---|---|---|---|---|
| 1 | Reachability | GET | `/ping` | none | **capture**, <https://github.com/rasmuslos/ShelfPlayer/issues/456> |
| 2 | List libraries | GET | `/api/libraries` | Bearer | **capture** (#456) |
| 3 | Authorize | POST | `/api/authorize` | Bearer | **capture** (#456) |
| 4 | Library filter data | GET | `/api/libraries/<id>?include=filterdata` | | **capture** (#456) |
| 5 | Library items | GET | `/api/libraries/<id>/items?...&limit=100` (also `collapseseries=1`) | | **capture** (#456) |
| 6 | Home shelves | GET | `/api/libraries/<id>/personalized` | | **capture** (#456) |
| 7 | Item detail | GET | `/api/items/<id>?expanded=1` | | **capture** (#456) |
| 8 | Search | GET | `/api/libraries/<id>/search?q=...` | | **capture** (#456, which also notes the 2 MB response) |
| 9 | Realtime | GET | `/socket.io/?transport=websocket&EIO=4` | | **capture** (#456) |
| 10 | Login | POST | `/login` or OIDC | | **inferred** (OIDC exists per #431) |
| 11 | Start playback | POST | `/api/items/:id/play` | | **inferred**. #456 mentions saved "playbackSessions", and #447 mentions a `mediaPlayer` request value and ABS HLS sessions, so session-based playback is likely. Unverified. |
| 12 | Sync progress | POST | `/api/session/:id/sync` | | **inferred** |
| 13 | Track stream | GET | `/public/session/:id/track/:index` or HLS under `/hls` | | **inferred** (#447 discusses ABS HLS manifests being session-scoped) |

---

## 3. Recommendation

### Implement the Audiobookshelf API first

Reasoning, from the verified needs above:

1. **Reach.** Three of the four clients surveyed can only talk to ABS: the official ABS app, ShelfPlayer and (as a choice) Plappa. Jellyfin reaches only the Jellyfin-API clients plus Plappa's second mode. Plappa works with either, so ABS adds the other two ABS-only clients on top. (ShelfPlayer is archived and its ABS 2.35.0 compatibility is already in doubt, so its weight is low.)
2. **The endpoint counts are close, so size does not decide it.** The official-app path above is 12 distinct routes for a browse-and-play-with-sync flow (rows 1-3, 5, 7-8, 10-12, 16 plus cover and refresh). The jellyfin-web path is 14 (rows 1, 3, 5, 6, 8-10, 12-14, 16 plus 4, 15 and 17, with 9 and 11 optional). An earlier draft claimed ABS was "simpler" by counts that contradicted themselves. The deciding factor is reach, not count.
3. **Plappa's ABS mode has two non-obvious server requirements** that the capture in 2.3 exposed: the `/api/items/:id/file/:fileId` route with Range and Bearer auth, and the `X-Refresh-Token` header on `/auth/refresh`. Both are cheap to build once and should be covered by tests from the start.

### Size estimates (rough guesses, with their basis)

These are counts of distinct routes in the minimal tables above, not effort measurements. No multiplier was derived. Convert to days only after the owner picks a target client.

| API | Distinct routes in the minimal set | Basis | Notes |
|---|---|---|---|
| **ABS** | about 12 | official-app table, rows 1-5, 7-8, 10-12, 16 plus cover | Auth (login and refresh) is the part with the most state. |
| **Jellyfin** | about 12 to 14 | jellyfin-web table | Adds a device-profile and `PlaybackInfo` negotiation (row 9) and a different ID and tick (100 ns) model. |
| **OPDS** | not estimated | none of the surveyed clients uses it | The earlier draft's "~5 endpoints, 0.3x" had no basis and has been removed. |

### Open questions for the owner

* Which player(s) does the owner actually use, and does Plappa run in ABS or Jellyfin mode on the owner's setup? The recommendation above assumes the issue's Plappa plus "similar apps".
* Is there an ABS or Jellyfin test server with audiobooks that can be captured against? That would turn the **inferred** and **server-src** rows into **capture**.

---

## 4. Our captured session (Jellyfin demo, server 12.2.0, 2026-10-08)

Run with `curl` against the public demo (`https://demo.jellyfin.org/stable`), user `demo` (blank password, which is the demo's published login). IDs are trimmed. The access token was held in a temp file and is not recorded here. The test track is a music track, since the demo has no audiobook library.

```text
GET  /System/Info/Public                    -> 200  {"ServerName":"Stable Demo","Version":"12.2.0",...}
GET  /Users/Public                          -> 200  ["demo"]
POST /Users/AuthenticateByName              -> 200  keys: AccessToken, ServerId, SessionInfo, User
     body {"Username":"demo","Pw":""}  header Authorization: MediaBrowser Client="bookdoe-spike", Device="curl", DeviceId="spike-210", Version="0.0.1"
GET  /UserViews                             -> 200  Movies, Music, Playlists, Shows
GET  /Items?userId=..&includeItemTypes=Audio&recursive=true&limit=1&fields=MediaSources
                                            -> 200  TotalRecordCount 5; item keys include Id, RunTimeTicks, MediaSources[].{Id,Container}, UserData
GET  /Items/{id}?userId=..                  -> 200
GET  /Items/{id}/Images/Primary             -> 404  (no image on this track)
GET  /Audio/{id}/universal?...  Range 0-1023 -> 206 audio/mpeg
GET  /Audio/{id}/stream?static=true  Range 0-1023 -> 206 audio/mpeg
POST /Sessions/Playing                      -> 204  {"ItemId":..,"PlaySessionId":"spike210play","PositionTicks":0,"CanSeek":true,"PlayMethod":"DirectPlay"}
POST /Sessions/Playing/Progress             -> 204  {...,"PositionTicks":100000000,"IsPaused":false,"PlayMethod":"DirectPlay"}
POST /Sessions/Playing/Stopped              -> 204  {...,"PositionTicks":200000000}
GET  /UserItems/{id}/UserData?userId=..     -> 200  keys: PlaybackPositionTicks, PlayCount, IsFavorite, LastPlayedDate, Played, Key, ItemId
POST /Users/{userId}/Authenticate?pw=       -> 400  (legacy, obsolete)
POST /Users/{userId}/PlayingItems/{id}      -> 204  (legacy start report)
POST /Sessions/Logout                       -> 204
```

Side effect to know about: the demo is a shared public server, so this run left a few play-state writes on its `demo` user. Nothing else was changed.

## 5. Sources

**Audiobookshelf server** (`https://github.com/advplyr/audiobookshelf/blob/master/`): `server/Server.js`, `server/Auth.js`, `server/routers/ApiRouter.js`, `server/routers/PublicRouter.js`, `server/controllers/LibraryItemController.js`, `server/controllers/SessionController.js`, `server/managers/PlaybackSessionManager.js`, `server/objects/PlaybackSession.js`. Release notes: <https://github.com/advplyr/audiobookshelf/releases/tag/v2.26.0>.

**Audiobookshelf app** (`https://github.com/advplyr/audiobookshelf-app/blob/master/`): `ios/App/Shared/util/ApiClient.swift`, `ios/App/Shared/player/AudioPlayer.swift`, `ios/App/Shared/models/PlaybackReport.swift`, `android/app/src/main/java/com/audiobookshelf/app/server/ApiHandler.kt`, `components/connection/ServerConnectForm.vue`, `store/user.js`, `store/libraries.js`, `pages/bookshelf/index.vue`.

**Jellyfin** (`https://github.com/jellyfin/jellyfin/blob/master/Jellyfin.Api/Controllers/`): `UserController.cs`, `PlaystateController.cs`, `SessionController.cs`, `UserLibraryController.cs`, `ItemsController.cs`, `UserViewsController.cs`, `UniversalAudioController.cs`, `AudioController.cs`, `ImageController.cs`, `SystemController.cs`; `Jellyfin.Api/Models/UserDtos/AuthenticateUserByName.cs`. Clients: <https://github.com/jellyfin/jellyfin-apiclient-javascript> (`src/apiClient.js`), <https://github.com/jellyfin/jellyfin-web> (`src/components/playback/playbackmanager.js`, `src/plugins/htmlAudioPlayer/plugin.js`).

**Plappa:** <https://github.com/LeoKlaus/plappa> (README, issues #211 and #491).

**ShelfPlayer:** <https://github.com/rasmuslos/ShelfPlayer> (README, issues #431, #447 and #456, PR #294).
