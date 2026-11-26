# Security model

## Authentication

bookdoe supports local accounts, API keys, native OIDC, trusted reverse-proxy identity, or explicit no-auth mode for controlled testing. Local passwords use Argon2. Browser sessions use signed HttpOnly cookies and CSRF validation. OIDC uses Authorization Code flow with PKCE, state and nonce verification and provider JWKS validation.

`none` mode is not appropriate for exposure outside a trusted test network.

### Hardening settings

- `BOOKDOE_SECRET_KEY` signs sessions and keys the encrypted secret store. Outside `none` mode an empty value stops startup; in `none` mode it logs a warning and falls back to the container hostname.
- `BOOKDOE_TRUSTED_PROXIES` (comma list of IPs/CIDRs, e.g. `172.18.0.0/16`) names the reverse proxies allowed to assert the proxy-mode identity header (`X-authentik-username`) and `X-Forwarded-For`. The header is honoured only when the TCP peer is inside the list. In proxy mode an empty list fails closed: the header is refused for every request and a startup warning says so. Never publish the container port directly in proxy mode. Caveat: the image starts uvicorn with `--proxy-headers --forwarded-allow-ips=*`, so `request.client.host` is whatever `X-Forwarded-For` claims, from any peer. The trusted-proxy check is therefore only as strong as that flag: before relying on proxy mode, also restrict `--forwarded-allow-ips` to the proxy's address (compose `command:` override), or every client could name itself the proxy.
- `BOOKDOE_OIDC_ALLOWED_GROUPS` (comma list, optional). When set, an OIDC sign-in that would create a new account or link an existing one must carry one of these in its `groups` claim, otherwise it gets 403. Already-linked accounts are not re-checked. Unset means any identity the IdP authenticates may be provisioned, so restrict access in the IdP application instead.
- OIDC accounts are matched on the IdP `sub` (stored in `users.auth_subject`), then on a verified email (`email_verified` true), and only then, for the first link of an existing local account, on the username. `auth_source` is left alone so local break-glass login keeps working. `auth.account_linked` is written once per link.
- `POST /api/v1/auth/login` and OPDS Basic lock a client IP and a username for 60 s after 5 failures (in memory, per process; resets on restart). Successful OPDS Basic logins are cached for 5 minutes per credential hash and dropped on password change or demotion.
- Sessions carry the user's `session_version`; changing a password, demoting an account (admin to anything, anything to kid or pending, by an administrator or by an authoritative Authentik group claim) bumps it and invalidates that user's existing cookies. Logout only clears the browser cookie; the installed Player also deletes that user's offline content cache on sign-out.

### Staying signed in (optional)

Two opt-in conveniences, both off the Authentik path and neither requiring any change in
Authentik. Settings (Management > Settings > Accounts, administrators, audited as
`settings.signin`) are `auth.remember_days` (default 90, 0 disables),
`auth.remember_allowed_types`, `auth.remember_absolute_days` (180),
`auth.trust_device_allowed_types`, `auth.trusted_devices_max` (10) and
`auth.trusted_device_idle_days` (0 = never).

**Keep me signed in.** Without it a session cookie lasts 24 h, with no renewal. With it the
cookie lasts `remember_days`. The wish is stored in the signed `bookdoe_oidc` cookie that
already binds `state`, `nonce` and the PKCE verifier, and the callback reads it only from
there, so a link with `&remember=1` on the callback buys nothing. A remembered session
re-issues its cookie (same session id) once less than half its life is left, never past
`remember_absolute_days` from sign-in, and stops renewing if the setting is turned off or
the account type is no longer allowed. `session_version` bumps (password change, demotion,
sign out everywhere) end it as before. Audit: `session.remembered`.

**Trust this device.** A signed-in person (session + CSRF) can register the browser; the
server sets the HttpOnly `bookdoe_device` cookie (Secure over https, SameSite=Lax,
10-year Max-Age) holding an `alxd_` token and stores only its hash. The token never
appears in a response body, so scripts and the service worker cannot read it. The cookie
works only when there is no valid session, only in the Player-only device lane (see Device
tokens), and writes need a CSRF token derived from the device id and the server secret;
SameSite=Lax alone is not relied on. An administrator's trusted browser is still
Player-only: Management, admin APIs and credential management always need the normal sign-in.

Threat model: a stolen cookie gives Player access as that person (their shelf; for a kid the
kids shelf) and nothing administrative. It can be revoked from Player Settings or by an
administrator and that takes effect on the next request. The token rotates after 30 days of
use (the old one stays valid 5 more minutes), so a copied token has a limited life while the
real browser keeps working. Because the lifetime is "until revoked", trusted devices stay
through a password change, a role change (except to pending) and "sign out everywhere" unless
the administrator (or the person, for sign out everywhere) ticks "revoke trusted devices".
Demotion to pending, account deletion, signing out of that browser and explicit revocation
remove them; removing an account type from `auth.trust_device_allowed_types`, or setting
`auth.trusted_device_idle_days`, ends them too. Audit: `device.trusted`, `device.rotated`,
`device.revoked`, `device.reuse_detected`.

Hardening details: a browser token is accepted only as the cookie and a paired-reader token only as
Bearer (a copied cookie value cannot skip CSRF or mint a paired token through `/devices/rotate`).
Rotation keeps the replaced token for 5 minutes; if that replaced token is presented again inside the
window the browser is simply re-rotated (it never got the response), but if it appears AFTER the window
it can only be a copy, so the registration is revoked (both tokens), the cookie cleared,
`device.reuse_detected` written, and the owner's Settings and the Management Devices page show a
"possible stolen sign-in" notice. Approving requests and granting books to a kid need a real session,
never a trusted browser or a reader token. Signing out on a browser revokes whatever trusted device its
cookie names, whoever owns it. Over https the cookies are written as `__Host-bookdoe_session` and
`__Host-bookdoe_device` (Secure, Path=/, no Domain); the old unprefixed session cookie is still read
so the deploy signs nobody out, but only as a transition: it is honoured only when no prefixed
session cookie is present, only until `auth.legacy_cookie_until` (set to 1 day after the first start
with this code), and is re-issued as `__Host-` and deleted on first use. A legacy (unprefixed) device cookie
is never honoured, and a legacy cookie planted from a sibling subdomain cannot override a prefixed one.
Sign-out deletes both names (the `__Host-` deletion carries Secure and Path=/). Over http the plain names
are used. A replaced device token may re-rotate once per rotation; a second use of a replaced token inside
the window (two parties taking turns) counts as reuse and revokes the device. Changing the Kindle address
needs a real session; a trusted browser can still send to the saved address.
The per-person limit is checked and the row inserted in one transaction. Only a user-agent summary and an IP /24 (/48) prefix are kept per device.
Known limits: sessions are stateless, so signing out does not invalidate a copy of a remembered session
cookie (only "sign out everywhere" or another `session_version` bump ends it). A password change keeps
trusted browsers by default (the owner's decision; tick "revoke trusted devices" to end them). Offboarding a
person in Authentik does not reach their remembered bookdoe sessions or trusted browsers: remove or
disable the account in Management (which revokes their devices) or revoke the devices there. The legacy
(pre-`__Host-`) session cookie is honoured for one day after first start only, and only in the exact old
token format; the replacement is a fresh 24 h session (`session.legacy_migrated` is audited).
Not verified here: how iOS treats cookie storage for the installed home-screen app; test it
on the device.

## Account types, Management and the Player

Four account types (`admin`, `adult`, `kid`, `pending`) are derived from `users.is_admin`
and `users.access_scope` in one place (`auth.account_type`). Management (`/manage` and
every library-organising `/api/v1` route behind `auth.require_full_access`) is
administrators only; a kid or adult who types the URL gets a 403 page and 403 from every
management API. Personal routes stay open to placed accounts and apply the kid
visibility predicate (`tagging.kids_visibility_clause`: `kids` tag, book grant or
collection grant; everything else 404). `pending` identities can only sign in and out.
Authentik `groups` -> type mapping, precedence and the last-admin guard are in
`PLAYER_MANAGEMENT.md`; only the validated ID token's claim is ever read. Every type
change, approver assignment, grant, request decision and device registration or
revocation is a History event naming the actor.

## Device tokens (Player API)

A trusted-browser cookie (`bookdoe_device`) rides the same lane, plus the exact personal routes listed in `PLAYER_API.md`; see Staying signed in above.

`alxd_...` tokens are minted by bookdoe's own device-code pairing (`PLAYER_API.md`):
a signed-in browser session approves a short-lived code shown on the device; the
device polls for the token; the code is single-use and expires in 15 minutes; pairing
requests are rate limited per IP. Only the SHA-256 of a token is stored. A device
token reaches the Player API and the media byte routes it points at and nothing else:
`/api/v1` management and personal routes, `/manage`, credential management and
`require_admin` all refuse it, whatever the owner's type. Tokens are revocable by the
owner (Player Settings) and by an administrator (Management), rotatable by the device,
and locked out when the owner becomes `pending`. No IdP secret, password or admin
token is ever placed on a device.

## Discover chat

The Player's AI book-recommendation chat (`/api/player/v1/discover`, `PLAYER_MANAGEMENT.md` > Discover chat) treats the
model as untrusted. Reader text, library titles and the profile are delimited data; control characters and the
delimiter characters are stripped; the model's JSON is validated and bounded and its titles are grounded against the
library (visible works only) and the metadata provider before display. For kid accounts the server re-checks every
candidate with the kids-gate audience evidence, drops adult signals, screens input and output with a keyword pass and
never sends the library title list. The prompt carries only the asker's own history and profile, never other
accounts' data, paths, e-mail addresses, user names or secrets. Conversations and profiles are per account and are not
readable by administrators for adults; a kid's are readable by assigned approvers and administrators while
`discover.kid_chats_visible_to_approvers` is on (the kid is told). API keys are refused, paired device tokens are accepted (chats stay tied to the account) but approver views of a kid's chats need a normal sign-in;
writes need CSRF. Calls are bounded: one generation per account, a hard deadline, a burst limit, a daily cap, a turn
cap and a token cap. The burst limit and daily cap are counted in an append-only usage ledger (`discover_usage`) that
deleting a chat does not reset (pruned after the retention window, never under two days); a cancelled or timed-out turn
keeps its account "in flight" until its worker thread has actually exited, so it cannot be used to fill the shared
two-worker pool. For kids, titles named in the assistant's prose are not grounded, so prose (and each card's "why")
that names a quoted, italic or title-like span that is not one of the grounded cards is replaced with a fixed sentence
(a "why" is blanked); a kid's "remember that?"
question is always the server's own wording; a work hidden from kids (or with an explicit kids-shelf "off" decision) is
never recommended through its provider record. Each conversation records the account type it was created under:
approvers and administrators can read only conversations created while the account was a kid's, so switching an adult
to a kid does not expose their earlier chats, and those chats are also hidden from the account itself (not listed, `404`),
because their cards and prose were never screened for the new audience; a turn that finishes after the type changed is
discarded. Residual risk: the taste profile is one row per account; it is labelled
with the account type that last wrote it and is not readable for approvers while that label is not "kid", but once a
switched account (adult to kid) saves its profile again, the existing contents (screened as a kid's) become readable.
Residual risk: a keyword pass is not a classifier; the unverified-kid-candidate default is to drop.

## API keys

Keys (`alex_…`) authenticate as the account that created them, in every auth mode including OIDC, and are created under Settings → API keys. Each key carries one access level that is enforced on every request: `read` (GET/HEAD only), `write` (any non-admin change) or `admin` (user and system management; never more than the owning account already has). New keys default to `read`; a key may have an expiry. A key cannot create, list or revoke keys - that needs a signed-in person - so a leaked key cannot escalate itself. Only a SHA-256 hash and a short display prefix are stored; the secret is shown once. Creation and revocation are written to History. `last_used_at` is updated at most once a minute per key. Keys written before levels existed carry `*` and are treated as admin-level (still capped by their owner's rights); revoke and re-issue them at a narrower level. OPDS readers may send a key as the HTTP Basic password with any username.

## Secrets

Integration secrets are encrypted at rest with a key derived from the application secret. API keys are hashed in the database. Authorization headers and credentials should never be written to application logs.

## Filesystem safety

The scanner/adopter is observational. File mutation occurs only in the managed-import path or explicit future maintenance actions. Import path generation sanitizes unsafe names and refuses silent overwrite.

## Network integrations

Indexer/download-client integrations accept HTTP/HTTPS URLs and reject embedded URL credentials. Keep bookdoe and OpenReads on a private Docker network and avoid publishing OpenReads unless there is a concrete operational requirement.

## Migration

Readarr databases are opened with SQLite `mode=ro`. The migration test suite verifies that synthetic source-database and source-media bytes remain unchanged.

## Backups

bookdoe's online backup uses SQLite's backup API rather than copying a live database file. The generated backup is integrity-checked before publication and receives a SHA-256 manifest.

## Threat-model limitations

The project has unit/integration security-oriented controls, but this packaged build has not undergone an independent penetration test or third-party supply-chain audit. Before Internet exposure, review dependency/container scan results and your NPM/Authen­tik configuration. There is currently no architectural need to expose the application publicly for the described use case.
