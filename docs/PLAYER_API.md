# bookdoe Player API v1

Base path `/api/player/v1`. The contract the Player shell, the installed PWA and
reading devices (ESP32 readers, CrossPoint E Ink, KOReader/Kobo scripts, other
lightweight clients) use. The machine-readable contract is the server's
`/openapi.json` (tag `player`); `bookdoe/tests/test_player_api_contract.py` fails
when this document and that schema disagree about the route list or the auth rules.

## Authentication

| Credential | How | Reaches |
| --- | --- | --- |
| Browser session | cookie `bookdoe_session` + header `X-CSRF-Token` on writes | Player API, personal `/api/v1` routes, Management if admin |
| API key `alex_...` | `Authorization: Bearer alex_...` | whatever its level allows (unchanged) |
| Trusted-browser cookie `bookdoe_device` | HttpOnly cookie set by `POST /devices/trust`; header `X-CSRF-Token` (from `GET /me`, field `csrf`) on writes | The Player API, the media byte routes and the exact personal `/api/v1` routes the Player web app calls (below). Used only when there is no valid session cookie; never upgrades to a session |
| Device token `alxd_...` | `Authorization: Bearer alxd_...` | Player API and the media byte routes it points at (`/api/v1/library/{work}/files/{file}`, `/cover`). Nothing else: every `/api/v1` management or personal route answers 403, `/manage` answers 403, and `require_admin` refuses a device even when the owner is an administrator |

A device token carries exactly the owner's account type: a kid's device sees the kid's
shelf, an adult's device the whole library, an admin's device the whole library and no
administration. An account set to `pending` locks its devices out (403). Revocation
takes effect on the next request. No CSRF header is needed with a Bearer credential.

### Device onboarding (pairing)

bookdoe issues its own device credentials; no Authentik client secret, password or
admin token is ever placed on a device. The flow is shaped like the OAuth 2.0 Device
Authorization Grant (RFC 8628) so existing client code ports over:

1. `GET /capabilities` (no auth): `auth.verification_uri`, `poll_interval`, `code_lifetime`.
2. `POST /device/code` `{name?, platform?, client_version?}` (no auth; 10 per IP per 10 min) ->
   `201 {device_code, user_code, verification_uri, verification_uri_complete, expires_in: 900, interval: 5}`.
   Show `user_code` (`XXXX-XXXX`, no vowels or 0/1) and keep `device_code` private.
3. The person opens the Player (any browser), Settings -> Pair, types the code
   (`POST /device/approve {user_code, name?}`, session + CSRF only; a device or API key
   cannot approve, which prevents credential laundering). They may also `POST /device/deny`.
4. The device polls `POST /device/token {device_code}` every `interval` seconds.
   Until approval: `400 {"error":"authorization_pending"}`; too fast: `400 slow_down`;
   after `expires_in` or once used: `400 expired_token`; denied: `403 access_denied`.
   On approval: `200 {token, token_type: "Bearer", device_id, name, user}`. The token is
   shown once. Store it in the device's secure storage where the hardware has one.
5. From then on send `Authorization: Bearer <token>`.

Lifecycle: `GET /devices` (own devices, metadata only), `PATCH /devices/{id} {name}`,
`DELETE /devices/{id}` (own; an admin revokes anyone's via `DELETE /api/v1/devices/{id}`
and sees all via `GET /api/v1/devices`), `POST /devices/rotate` (device credential only:
returns a fresh token and revokes the current one in the same step). `device.registered`
and `device.revoked` are History events. Optional expiry is supported in the table
(`expires_at`) and honoured by the auth layer; pairing mints non-expiring tokens.

### Trusted browsers ("Trust this device")

A signed-in person (normal session plus CSRF; never a device, cookie device or API key)
calls `POST /devices/trust {name?}`. The server registers a `devices` row of kind
`browser` (user-agent summary such as "Safari on iOS", first/last seen, last IP /24 or
/48 prefix) and answers with `Set-Cookie: bookdoe_device=alxd_...; HttpOnly; Secure
(https); SameSite=Lax; Path=/; Max-Age=315360000`. The body never contains the token, so
page scripts and the service worker cannot read it. Only the SHA-256 is stored.

The cookie authenticates only when the request has no valid session cookie, as the
Player-only device lane: this API, `/api/v1/library/{work}/files|cover`, and these
personal routes the Player web app calls (each with the owner's visibility rules):
`GET /api/v1/library`, `GET /api/v1/library/{work}`, `PUT .../rating`, `PUT .../read-state`,
`GET .../read/{asset}`, `GET|PUT|DELETE .../progress/{asset}`, `GET /api/v1/authors[/{id}]`,
`GET /api/v1/series[/{id}]`, `GET /api/v1/tags`, plus `POST /api/v1/auth/logout` (which
revokes the browser). Management, admin routes, credential management, pairing approval, approvals/grants
(session only, for Bearer readers too) and `POST /devices/rotate` stay refused, administrators included; a Bearer device keeps
the narrower lane. Writes need `X-CSRF-Token` (`GET /me` returns it; it is derived from
the device id and server secret, so it survives rotation).

The token rotates after 30 days of use: the response re-sets the cookie and the replaced
token keeps working for 5 minutes (a request race; seen again in that window it re-rotates so a
lost response recovers; seen after it, the device is revoked as `device.reuse_detected`). A browser
token is never valid as a Bearer token, nor a paired token as the cookie. Over https the cookie is
`__Host-bookdoe_device`. Revocation (owner, administrator,
sign-out, demotion to pending, deletion, idle expiry when `auth.trusted_device_idle_days`
is set) takes effect on the next request and the dead cookie is cleared.

### Password login (native apps)

A phone has a keyboard, so the iOS app signs in with `POST /auth/login`
`{username, password, device_name?, platform?, client_version?}` and receives the
same device credential pairing would mint (`200 {token, token_type, device_id, name,
user}`, `Cache-Control: no-store`). It is throttled exactly like `/api/v1/auth/login`
(5 failures lock the IP and the username for 60 s, 429 with `Retry-After`), answers
400 in `proxy` and `none` auth modes (pair instead; `capabilities.auth.password_login`
says which applies) and 403 for a `pending` account. In `oidc` mode it reaches local
accounts only (kids accounts); SSO identities pair.

Authentik's own Device Authorization Grant is not used: the token a device needs is an
bookdoe one, and this flow needs no change in Authentik. It is documented in
`PLAYER_MANAGEMENT.md` as an alternative should the owner enable it on the brand.

## Conventions

- JSON only. Errors are `{"detail": "..."}` with 400 (bad input), 401 (no credential),
  403 (forbidden: wrong account type, device outside its lane, CSRF), 404 (not found
  or not visible: a kid never learns whether a hidden id exists), 409 (state), 413,
  422 (validation), 429 (rate limited, with `Retry-After`).
- Pagination: `limit` (max 200) and `offset`; answers carry `total`, `limit`, `offset`.
- Conditional requests: list and detail answers carry `ETag` and
  `Cache-Control: private, no-cache`; send `If-None-Match` to get `304`.
- Incremental catalogue: `GET /library?updated_since=<server_time>` lists only works
  changed since; `GET /sync?since=` returns the ids of changed works plus changed
  positions, favorites and bookmarks, and the `server_time` to store for next time. A
  device never has to re-download the whole catalogue after the first sync.
- Media bytes: `GET /api/v1/library/{work}/files/{file}` honours `Range` (206) for
  seeking and resumable downloads; `Content-Disposition: inline`; `Cache-Control:
  private, no-store`. Covers: `GET /library/{work}/cover?size=96|192|384|768` (scaled
  with ffmpeg when available, otherwise the original), `private, max-age=86400`.
- No server-side session state is assumed between calls; every request is complete
  on its own, so intermittent connectivity only delays sync.

## Routes

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/capabilities` | none | server/API version, `build` (`sha`, `built_at` only), `setup_required`, `auth.password_login`, pairing parameters, limits, formats, features |
| POST | `/auth/login` | none (rate limited) | native apps: username + password (+ `device_name`, `platform`, `client_version`) -> the same `{token, device_id, name, user}` as `/device/token`; 400 in `proxy`/`none` modes, 403 for a pending account |
| GET | `/me` | any | identity, `account_type`, `scopes`, `content_scope` (`approved_only` for kids), `device` when a device token |
| POST | `/device/code` | none (rate limited) | start pairing |
| POST | `/device/token` | none | poll for the token |
| POST | `/device/approve` | session | approve a code for this account |
| POST | `/device/deny` | session | deny a code |
| GET | `/devices` | any | own devices (paired readers and trusted browsers, `kind`), `current_device_id`, `trust.allowed/max` |
| POST | `/devices/trust` | session | trust this browser: sets the HttpOnly `bookdoe_device` cookie, returns no token; 5 per 10 min per account and address; 409 at the per-account limit |
| PATCH | `/devices/{id}` | any | rename own device |
| DELETE | `/devices/{id}` | any | revoke own device |
| POST | `/devices/rotate` | device | rotate this device's token |
| GET | `/kindle` | any (no device) | Send-to-Kindle status: configured, sender address to approve, own Kindle email, formats, limit |
| PUT | `/kindle` | any (no device) | set or clear own Kindle email (validated, audited) |
| GET | `/kindle/{work}` | any (no device) | sendable files of a visible work, best first |
| POST | `/kindle/send/{work}` | any (no device) | email the best (or named) file to own Kindle; 10 per 10 min per account |
| GET | `/library` | any | compact catalogue: `q`, `kind`, `author_id`, `series_id`, `updated_since`, `sort=title\|added\|updated`, paging |
| GET | `/library/{work}` | any | one work with description, progress, favorite |
| GET | `/library/{work}/cover` | any | cover art, `size` |
| GET | `/authors` | any | authors with visible, available books |
| GET | `/series` | any | series with visible, available books |
| GET | `/ebook/{work}/{asset}` | any | ebook manifest: `download_url`, format, size, position |
| GET | `/audio/{work}/{asset}` | any | audio manifest: ordered tracks with stream URLs, chapters, duration, position |
| GET | `/continue` | any | in-progress items, newest first (`kind` = `ebook`, `audiobook` or `podcast`). In-progress podcast episodes are interleaved with books in the unfiltered feed and in `kind=audiobook` (the listening shelf); `kind=podcast` returns episodes only; `kind=ebook` never includes episodes. Episode items carry `kind: "podcast"`, `episode_id`, `show_id`, `show_title`, `title`, `season_number`, `is_extras`, `duration_seconds`, `position_seconds`, `updated_at` (no `work_id`). |
| GET | `/podcasts/shows/{show}/next` | any | first unplayed episode in season order, Extras last (`episode` null when all played) |
| GET | `/podcasts/episodes/{episode}/next` | any | next unplayed episode after this one, across season boundaries |
| GET | `/podcasts` | any | every show: `id`, `title`, `cover_url`, `season_count`, `has_extras` (same body as `/api/v1/podcasts`) |
| GET | `/podcasts/shows/{show}` | any | one show with its seasons (Extras last) and episodes; each episode carries `missing` (file gone, still listed, never offered as next) |
| GET | `/podcasts/shows/{show}/cover` | any | the show's `cover.jpg`; 404 when absent |
| GET | `/podcasts/episodes/{episode}/audio` | any | the episode file, Range-capable; 404 when the episode is unknown or missing |
| GET | `/recent` | any | recently added, recently opened |
| GET | `/home` | any | one call for the Home screen |
| GET | `/progress` | any | all positions (`since`) |
| PUT | `/progress` | any | batch of position updates |
| GET | `/progress/{asset}` | any | one position |
| PUT | `/progress/{asset}` | any | one position update |
| DELETE | `/progress/{asset}` | any (CSRF for cookie devices) | forget this account's position in the asset ("mark unread"): leaves `/continue`; idempotent; 404 for an asset that is missing or off the account's shelf |
| GET | `/podcasts/episodes/{episode}/progress` | any | this account's resume position in a podcast episode, keyed by episode id (`null` when none); 404 for an unknown episode |
| PUT | `/podcasts/episodes/{episode}/progress` | any (CSRF for cookie devices) | save a position `{position_seconds, client_updated_at}` (timestamp required; played rule applies as for the POST); a write not newer than the stored one is not applied (`applied: false`) and the stored position is returned |
| GET | `/sync` | any | incremental sync bundle |
| GET | `/bookmarks` | any | own bookmarks and highlights (`asset_id`, `work_id`, `kind=bookmark\|highlight`) |
| POST | `/bookmarks` | any | add a bookmark, or a highlight (`kind: "highlight"`: `location` is the passage's EPUB CFI range, `title` the selected text, optional `note` and `color`; an audiobook bookmark's `position` is seconds from the start of the book) |
| PATCH | `/bookmarks/{id}` | any | change own bookmark's `note`, `title` or `color` (fields sent only; null clears) |
| DELETE | `/bookmarks/{id}` | any | remove own bookmark |
| GET | `/favorites` | any | own favorites |
| PUT | `/favorites/{work}` | any | add |
| DELETE | `/favorites/{work}` | any | remove |
| GET | `/preferences` | any | reader / audio / ui preferences |
| PUT | `/preferences` | any | merge sections |
| GET | `/requests/search` | any | what can be requested for a query |
| GET | `/requests` | any | own requests with friendly status and `search` (below) |
| POST | `/requests` | any | submit |
| GET | `/requests/{id}` | any | one request (own, or a kid you approve for) |
| DELETE | `/requests/{id}` | any | withdraw own pending request |
| GET | `/approvals` | approver (session only) | pending (or `status=`) requests you may decide |
| POST | `/approvals/{id}/approve` | approver | approve |
| POST | `/approvals/{id}/deny` | approver | deny |
| GET | `/approvals/kids` | approver | kids you approve for |
| GET | `/approvals/kids/{kid}/grants` | approver | books allowed for that kid |
| PUT | `/approvals/kids/{kid}/grants/{work}` | approver | allow a book |
| DELETE | `/approvals/kids/{kid}/grants/{work}` | approver | remove |
| GET | `/approvals/kids/{kid}/search` | approver | library works to allow |

| GET | `/discover/status` | session, trusted browser or device token | Discover chat: `available`, `account_type`, `parents_can_see`, limits (`message_chars`, `daily_cap`, `remaining_today`, `max_turns`, `timeout_seconds`), the AI notice |
| GET | `/discover/chat/conversations` | same | own conversations created under the current account type (chats from before an adult/kid switch are not listed) |
| POST | `/discover/chat/conversations` | same | new conversation |
| GET | `/discover/chat/conversations/{id}` | same | conversation with its messages (assistant messages carry `cards`); 404 for one created under another account type |
| DELETE | `/discover/chat/conversations/{id}` | same | delete (a kid's chat that approvers may see is hidden from the kid and kept for them until retention) |
| POST | `/discover/chat/conversations/{id}/messages` | same | `{text}` (max 600 chars): `202` at once with `message_id`, `status: pending`; the model runs off the request. 409 while one is running, the chat is full, or the chat was created under another account type (start a new chat), 429 over the burst or daily limit, 503 when the chat is unavailable |
| GET | `/discover/chat/conversations/{id}/messages/{message}` | same | poll until `status` is `done`, `failed` (`error`: `timeout`, `unreachable`, `bad_output`, `internal`, `type_changed`) or `cancelled`. A done answer has `text`, `follow_up`, `cards[]` (`group`: `library` or `not_in_library`; `title`, `author`, `why`, `work_id`, `metadata_id`, `cover_url`, `actions[]`, `request`, `feedback`, `unverified`), `profile_proposal`, `counts: {dropped, blocked}` |
| POST | `/discover/chat/conversations/{id}/messages/{message}/cancel` | same | stop waiting for a pending answer |
| POST | `/discover/chat/messages/{message}/feedback` | same | `{index, verdict: up\|down\|null}` thumbs on a card; feeds the profile |
| POST | `/discover/chat/messages/{message}/profile` | same | `{accept}` answers "Shall I remember that ...?" for the stored proposal |
| GET | `/discover/profile` | same | own taste profile |
| PUT | `/discover/profile` | same | replace it (validated and capped) |
| DELETE | `/discover/profile` | same | clear it |
| GET | `/discover/kids/{kid}/chats` | approver (session only) | a kid's conversations with blocked / dropped counts; 403 unless you approve for that kid and the kid-visibility setting is on |
| GET | `/discover/kids/{kid}/chats/{id}` | approver | one of them, with messages |
| DELETE | `/discover/kids/{kid}/chats/{id}` | approver | delete it for everyone |
| GET | `/discover/kids/{kid}/profile` | approver | the kid's taste profile |
| POST | `/podcasts/episodes/{id}/progress` | session, trusted browser or device token | `{position_seconds, client_updated_at?}` (>= 0): stores the resume position (same write path and timestamp guard as the PUT; without `client_updated_at` it is stamped with server time) and sets `played` with the rule: played at 95% of the duration or with 30 seconds or less left (an episode of 30 seconds or less only at its end). Returns the progress row; 404 for an unknown episode |
| POST | `/podcasts/episodes/{id}/played` | same | mark an episode played whatever the position; the stored position is kept. 404 for an unknown episode |
| POST | `/podcasts/episodes/{id}/unplayed` | same | mark an episode unplayed: position reset to 0, the progress row and the episode are kept. 404 for an unknown episode |
| GET | `/podcasts/shows/{id}/next` | session, trusted browser or device token | the first unplayed episode of the show in season order (numbered seasons ascending, Extras last) as `{episode}`; `episode` is null when everything is played. 404 for an unknown show |
| GET | `/podcasts/episodes/{id}/next` | same | the unplayed episode after this one in its show, crossing season boundaries, as `{episode}`; null at the end. 404 for an unknown episode |
| GET | `/podcasts/episodes/{id}/audio` | session, trusted browser or device token | the episode's audio file, inline, with Range support (206) so a player can seek and resume. 404 for an unknown episode or a file no longer on disk |
| GET | `/podcasts/episodes/{id}` | any | what a player needs to start an episode: `{episode_id, show_id, show_title, title, duration_seconds, url, position_seconds}` (`url` is the `/audio` route above); `position_seconds` is 0 for a played episode. 404 for an unknown episode or a file outside every enabled root |

"session or trusted browser" for the Discover rows: a signed-in session, the trusted-browser cookie (CSRF on writes) or a paired device's Bearer token (no CSRF), for every account type. An API key gets 403. `capabilities` also reports `discover_chat` (boolean).

Request items (`/requests`, `/requests/{id}`, `/approvals`) carry `search`: `null` until an
approved request has been searched, else `{state, at}` where `state` is `searching`, `grabbed`
(shown as "Found, downloading"), `no_match` or `no_result` ("Not found yet"), `error`,
`skipped_cooldown` or `skipped_cap`, and `at` is the time of the outcome. Approval starts one
background search per requested kind (setting `requests.search_on_approval`, default on).

"any" = any placed account (admin, adult, kid) with any credential; "any (no device)"
= the same but a device token is refused (403): sending mail is not a reading client's
business; "session" = a signed-in browser only; "approver" = a signed-in session (never a device token or trusted-browser cookie) of an admin, or an adult assigned
at least one kid.

### Asset format labels (`formats`, `primary_format`)

Each entry of a work's `assets` (in `GET /library`, `GET /library/{id}` and the Management work detail) also carries, additively: `formats` (every distinct format among the asset's present files, best first, e.g. `["epub","mobi"]` or `["mp3"]`), `primary_format` (the best of them by the default quality profile's `preferred_formats`; the one Read/Listen opens first), `file_count`, and in the Player payloads `size_bytes`; `duration_seconds` falls back to the sum of the track durations. `format` keeps its stored value for existing clients. The Player payloads never list files or paths.

### Converted copies (`formats`)

An administrator can have bookdoe make an M4B from an audiobook's MP3s or an EPUB from a MOBI (the original is always kept). The copy is a separate asset of the same Work. **By default a client never sees it**: `GET /library` and `GET /library/{id}` list the same assets as before. A client that can play or read a preferred format says so with `?formats=m4b,epub` or the header `X-bookdoe-Formats: m4b,epub` (anything other than `m4b` / `epub` is ignored). The response then also lists the converted copies of those formats (each asset carries `derived_from`, the id of the original) and a `preferred` object, `{ "audiobook": "<asset id>", "ebook": "<asset id>" }`, naming the best readable asset per kind: a declared-format copy if there is one, else the first original. `/audio/{work}/{asset}` and `/ebook/{work}/{asset}` accept either id. Reading positions are per asset, so a position in the MP3 version does not carry over to the M4B. `/capabilities` lists the `converted_assets` feature. Visibility is the Work's: a copy of a book a kid cannot see is not visible either.

### Send to Kindle

Delivery of a book already on the account's shelf to that account's own Kindle
address (`users.kindle_email`, set by the person in Player Settings or by an
administrator). The server chooses the file: EPUB first, then AZW3, MOBI, AZW, PDF,
DOCX, DOC, TXT (the formats Kindle's personal-document mail accepts). Visibility is
the same predicate as everything else (a kid cannot send a book off its shelf by
guessing an id: 404). Not configured server-side, no address on file, or no sendable
file: 400 with a plain message. Over the limit: 429 with `Retry-After`. The mail server
refusing: 424 with the relay's reason in `detail` (never a 5xx, which Cloudflare replaces with its own page). Every send is a History event (`device.sent`: user, work, file, address;
never the body). The Kindle must list the sender address (`sender_address` from
`GET /kindle`) among its approved personal-document senders.

## Sync

### Reading position (`PUT /progress`, item fields)

`asset_id` (edition identity: one asset = one edition's files), `location` (EPUB CFI or
PDF page), `percent` (0..1), `device_id`, `client_updated_at` (seconds, the device's
clock). Audio: `file_id` (track), `position` (seconds into the track), `duration`
(seconds of that track as the device sees it), `percent` (of the whole book).

### Conflict rule

Each item is judged on its own. An update is applied when it has no
`client_updated_at` (legacy clients) or its `client_updated_at` is newer than the
stored one; otherwise it is stale: the server answers `applied: false` with the stored
state (location, position, file, device, timestamp) so the device adopts it. Equal
timestamps lose (a retry is a no-op). Client timestamps more than 300 s ahead of the
server are clamped. Being the last HTTP request to arrive therefore never wins on its
own. The stored `updated_at` is the server's receipt time and is what `since` compares.

### Offline reconciliation

Queue updates with their original `client_updated_at`; on reconnect send them in one
`PUT /progress` batch (max 100 items). Items the server rejects as stale come back with
the current state; items for assets the account cannot see come back as `not_found`
(keep those positions and ask again later: the book may come back to the shelf). Writes
are budgeted per account and book (30 a minute): past it a single `PUT /progress/{asset}`
answers `429` with `Retry-After`, and a batch item comes back
`{"applied": false, "error": "rate_limited", "retry_after": n}`. A single `PUT` for a
missing or invisible asset is the same plain `404`. Clients back off on `429`/5xx and
network errors (exponential with jitter, honouring `Retry-After`) and must not retry a
`404` in a loop. Then `GET /sync?since=<last server_time>`.

### KOReader

Not claimed. A KOReader sync-server adapter (`/users/auth`, `/syncs/progress`) would map
onto `PUT /progress` with the document hash resolved to an asset; it has not been
written or tested.

## Sample session (device)

```
GET  /api/player/v1/capabilities
POST /api/player/v1/device/code            {"name":"Kitchen reader","platform":"esp32","client_version":"0.1"}
      -> 201 {"device_code":"...","user_code":"GHKM-7T4P","verification_uri":"https://books.example/app#pair",...}
POST /api/player/v1/device/token           {"device_code":"..."}   -> 400 {"error":"authorization_pending"}
   (person approves GHKM-7T4P in the Player)
POST /api/player/v1/device/token           {"device_code":"..."}   -> 200 {"token":"alxd_...","device_id":"...","user":{...}}
GET  /api/player/v1/me                     Authorization: Bearer alxd_...
GET  /api/player/v1/library?limit=50       -> items[], total, server_time (store)
GET  /api/player/v1/ebook/{work}/{asset}   -> download_url
GET  /api/v1/library/{work}/files/{file}   Range: bytes=0-65535  -> 206
PUT  /api/player/v1/progress               {"items":[{"asset_id":"...","location":"epubcfi(...)","percent":0.12,"device_id":"esp32-1","client_updated_at":1760000000}]}
GET  /api/player/v1/sync?since=<stored server_time>
```
