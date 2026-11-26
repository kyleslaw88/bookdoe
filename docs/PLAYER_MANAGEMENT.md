# Management, Player, accounts and Authentik

How the two experiences are served, how an Authentik identity becomes an bookdoe
account type, and what to configure in Authentik. The device API is in
`PLAYER_API.md`.

## Two shells, one backend

| URL | Shell | Who |
| --- | --- | --- |
| `/app`, `/app/...`, `/` and any other non-API path | Player (`player.html`, `player.js`, `player.css`) | every placed account; the installed PWA starts here (`start_url` `/app#home`) |
| `/manage`, `/manage/...` | Management (`index.html`, `app.js`) | administrators. Anyone else signed in gets a 403 page that links to the Player; a signed-out visitor gets the shell so its sign-in screen can run |

The shells are not the boundary. Every `/api/v1` route that organises the library
(acquisition, metadata, settings, users, jobs, Inbox, history, search providers) is
behind `auth.require_full_access`, which since this change means administrators only.
Personal routes (library browsing, book detail, cover, reader manifest, file
streaming, progress, rating, read state, authors, series, tags) stay open to every
placed account and keep the kid visibility rules. The reader and audio player are one
shared module, `consume.js`, loaded by both shells.

Old links of the form `/#library/<id>` still open (the Player). Management links are
`/manage#...`. The Playwright UI scripts under `scripts/` drive `/manage#...`.

## Account types

| Type | Stored as | Can |
| --- | --- | --- |
| `admin` | `is_admin=1` | Management and Player, every API, approvals |
| `adult` | `is_admin=0, access_scope='full'` | Player, personal APIs; requests auto-approved (phase 3); may be made an approver |
| `kid` | `access_scope='kids'` | Player over approved content only; requests need approval |
| `pending` | `access_scope='pending'` | Sign in and sign out. Every other route answers 403 `Your account is awaiting approval by an administrator`; the Player shows a waiting screen |

`auth.account_type(row)` is the single derivation; `/api/v1/auth/me` returns it as
`user.account_type` together with `user.management` (true only for an admin session that
is not a device token). Management sets it with `PATCH /api/v1/users/{id}`
`{"account_type": "adult"}` (the old `is_admin`/`access_scope` fields still work). Every
change writes `auth.account_type_changed` to History naming who did it; a loss of
privilege (admin to anything, anything to kid or pending) bumps `session_version`, which
signs that person out everywhere.

## Authentik: what bookdoe reads

bookdoe uses the Authorization Code flow with PKCE against the provider named by
`BOOKDOE_OIDC_ISSUER`. After signature (JWKS), issuer, audience and nonce
validation it reads from the ID token:

- `sub` (identity; stored in `users.auth_subject`, matched first on every sign-in),
- `preferred_username`, `email`, `email_verified`, `name` (display and linking, see `SECURITY.md`),
- `groups` (a list of group names) for the account type.

Nothing a browser or device sends can set a type: only the validated token's `groups`
claim is read, and only inside the callback.

### Authentik configuration (for the owner to apply; nothing here was changed by this work)

1. **Groups.** Create `bookdoe-admin`, `bookdoe-adult`, `bookdoe-kid` (Directory
   -> Groups). Other names work; set `BOOKDOE_OIDC_GROUP_*` to match. Put each person
   in exactly one of them. A user in `bookdoe-kid` and another bookdoe group is a
   contradiction and is treated as the most restrictive case (see below).
2. **Scope mapping for groups.** Authentik ships a scope mapping that emits `groups`
   (Customisation -> Property Mappings, "authentik default OAuth Mapping: OpenID
   'profile'" already includes `groups` in recent versions; otherwise add a Scope
   Mapping with scope name `groups` and expression
   `return {"groups": [g.name for g in request.user.ak_groups.all()]}`).
3. **Provider.** Applications -> Providers -> the bookdoe OAuth2/OpenID provider:
   client type Confidential, redirect URI `https://<BOOKDOE_PUBLIC_URL>/auth/callback`,
   signing key set, and under Advanced -> Scopes include `openid`, `email`, `profile`
   and the groups mapping. If `groups` lives in its own scope, add it to
   `BOOKDOE_OIDC_SCOPES` (`openid profile email groups`).
4. **Application.** Applications -> Applications -> bookdoe: bind the provider. If
   access should be limited at the door, bind a policy (or group) to the application,
   or set `BOOKDOE_OIDC_ALLOWED_GROUPS` to a group everyone in the household has.
5. **Verify** by signing in once and checking History for `auth.account_provisioned`
   (new account) or `auth.role_claim_*` (existing account in `groups` mode). If the
   event says "no mapped group", the claim is not reaching bookdoe: re-check step 2
   and the scopes in step 3.
6. **Device Authorization Grant** (optional, future): Authentik supports it per brand
   (System -> Brands -> Flow settings -> Device code flow). bookdoe's device pairing
   does not depend on it (see `PLAYER_API.md`); it is listed so the option is known.

### Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `BOOKDOE_OIDC_GROUP_ADMIN` | `bookdoe-admin` | group that maps to `admin`; empty disables that mapping |
| `BOOKDOE_OIDC_GROUP_ADULT` | `bookdoe-adult` | group that maps to `adult` |
| `BOOKDOE_OIDC_GROUP_KID` | `bookdoe-kid` | group that maps to `kid` |
| `BOOKDOE_OIDC_ROLE_SOURCE` | `provision` | `provision`: the claim decides the type of a NEW account only; administrators own it afterwards. `groups`: the claim is authoritative on every sign-in |
| `BOOKDOE_OIDC_BOOTSTRAP_ADMINS` | empty | comma list of `preferred_username` or `sub` values that become admin on their first sign-in. Setting it also turns off the "first account on an empty install is the admin" rule |
| `BOOKDOE_OIDC_ALLOWED_GROUPS` | empty | unchanged: when set, a sign-in that creates or links an account must carry one of these groups |

### The default, stated plainly

Out of the box Authentik groups are NOT authoritative. A new Authentik identity is
auto-created as `pending` (no access) unless it is in a mapped group or in
`BOOKDOE_OIDC_BOOTSTRAP_ADMINS`; an administrator then assigns the type in
Management -> Settings -> Accounts. Existing accounts are never changed by a sign-in.
Group mapping on existing accounts is opt-in (`BOOKDOE_OIDC_ROLE_SOURCE=groups`) and
stays off unless the owner sets it.

### Upgrading an existing install

No manual step. Types derive from the columns production already has:
`is_admin=1` -> admin (the owner's existing account, identified by its own row, keeps
Management); `access_scope='full'` -> adult; `access_scope='kids'` -> kid. `pending`
is only ever written when a NEW identity is provisioned, so no existing user lands
there. An existing administrator signing in through Authentik with no `groups` claim
(or an unmapped one) is matched on the stored `sub`, email or username and stays
admin in both role-source modes; in `groups` mode the last administrator cannot be
demoted by any claim. `bookdoe/tests/test_upgrade_accounts.py` proves each of these
against rows in the pre-change shape with migrations 17 to 20 run as an upgrade.

### Precedence, provisioning and role changes

New identity (no row for the `sub`, no verified-email or username link):

1. listed in `BOOKDOE_OIDC_BOOTSTRAP_ADMINS` -> `admin`;
2. mapped group -> that type (`admin` wins over `adult`; `kid` together with `admin` or
   `adult` -> `pending`, flagged `auth.role_claim_inconsistent`);
3. first account on an empty install and no bootstrap list -> `admin` (so a fresh
   install always has an administrator; name the bootstrap admins to disable it);
4. otherwise `pending`. Nobody becomes an adult or admin just by being able to sign in.

Existing account:

- `provision` (default): the claim is ignored; what an administrator set in Management
  stands. Safe when the `groups` claim is not yet confirmed to arrive.
- `groups`: the type follows the claim; a demotion revokes sessions and is written to
  History. A missing or unmapped claim changes nothing and writes
  `auth.role_claim_ignored`; a contradictory claim changes nothing and writes
  `auth.role_claim_inconsistent`; the last administrator is never demoted
  (`auth.role_claim_refused`).

Local (family) accounts created in Management are not touched by any of this.

### Initial administrator without self-registration

Any one of: `BOOKDOE_BOOTSTRAP_PASSWORD` (local admin at first start, existing
behaviour), the first-run setup form while the install is empty and
`BOOKDOE_AUTH_MODE=local`, membership of the admin group on first sign-in, or
`BOOKDOE_OIDC_BOOTSTRAP_ADMINS`. There is no route that lets a visitor create an
account with any access.

## Staying signed in, and Authentik settings that reduce prompts

Two optional features keep a family member signed in. **Neither needs any change in
Authentik**, and Authentik remains the only place passwords and MFA live.

| | Keep me signed in | Trust this device |
| --- | --- | --- |
| What the person ticks | a checkbox on the sign-in gates (Player, Management, family account form) | Player Settings > Trusted devices (and a one-time dismissible offer on Home) |
| Lasts | 90 days (setting), renewed while in use, never beyond 180 days from sign-in | until revoked (the token rotates itself every 30 days of use) |
| Reaches | what the account may normally reach, Management included for an administrator | the Player only, administrators too. Management still asks you to sign in |
| Ended by | password change, demotion, sign out everywhere, expiry | revoking it, signing out in that browser, demotion to pending, deleting the account, optional idle expiry |
| Wording shown | "Keep me signed in for 90 days" | "This device stays signed in until you revoke it. Use only on your own devices." |

Administrators control them under Management > Settings > Accounts > Staying signed in:
`auth.remember_days` (0 turns it off), `auth.remember_allowed_types`,
`auth.remember_absolute_days`, `auth.trust_device_allowed_types`, `auth.trusted_devices_max`
(per person; one more is refused with a message), `auth.trusted_device_idle_days` (0 = never).
Management > Settings > Devices lists every trusted browser (owner, browser, last prefix,
last seen) and revokes any. Editing an account has "Also revoke this person's trusted
devices"; without it a password or role change leaves them in place (demotion to Pending
always revokes). Player Settings has "Sign out everywhere" with the same opt-in checkbox.

Signing out in a trusted browser also forgets it, so the cookie cannot sign the person
straight back in. A kid can trust their own tablet, and a parent revokes it from
Management if it is lost. Over http (no TLS) the cookie is not marked Secure; use https.

### Authentik settings that reduce prompts (optional appendix; not required for the above)

Nothing here was applied by this change, and the features above work without any of it. If
the owner wants fewer Authentik prompts for the people who sign in through Authentik, these are
the steps in the Authentik admin interface (menu names as of 2025.x; wording may differ slightly):

1. **Longer Authentik session.** Flows and Stages > Stages > open the *User Login* stage
   used by the default authentication flow (often `default-authentication-login`) > Edit.
   Set *Session duration* to e.g. `days=30` (0 means a browser-session cookie). Set
   *Stay signed in offset* (the "remember me" prompt) to e.g. `days=90`; once set, the
   login page offers "Stay signed in?" and uses that lifetime when the person accepts. Save.
2. **Skip MFA for a while.** Flows and Stages > Stages > the *Authenticator Validation*
   stage in that flow > Edit > *Last validation threshold* = e.g. `days=30`. Authentik then
   skips the second factor if the same browser validated within that window. Save.
3. **Passkeys / WebAuthn.** Flows and Stages > Stages > Create > *WebAuthn Authenticator
   Setup Stage* (user verification: preferred), add it to the user settings flow (Flows and
   Stages > Flows > the user settings flow > Stage bindings), then in the *Authenticator
   Validation* stage allow *WebAuthn* among the device classes. People register a passkey
   at Authentik's user settings > MFA Devices. With a passkey, sign-in is one tap.
4. **Verify** by signing out and in again from a phone: the "Stay signed in" choice should
   appear and there should be no MFA prompt inside the threshold.

iOS standalone PWA limits: the home-screen app runs in its own WebKit container. Third-party
password manager extensions and Safari-based autofill are generally not offered inside it, and
the redirect to Authentik happens in that same container. That is why a long session (Keep me
signed in), a trusted device (no password at all) or a passkey (Face ID, built into iOS) matter
more here than a better password manager. Not tested against the production Authentik; try it
on the device.

## Kid content controls

A kid sees a work when any of these holds; everything else is 404 on every surface
(library, detail, cover, reader manifest, file bytes, OPDS, authors, series, Player
API, device tokens):

1. the work carries the `kids` system tag (the household-wide shelf; the LLM
   classifier only proposes, an administrator approves, unchanged);
2. the kid holds a book grant (`content_grants`), made by an administrator or by one of
   the kid's approvers, or created automatically when the kid's request was approved;
3. the kid holds a collection grant (`tag_grants`) on a tag the work carries.
   Collection grants are administrator-only because one click widens the shelf by
   many books, now and later.

Manual decisions beat the classifier. `kids_decisions` (migration 20) records, per
work, whether a person put it on or off the shelf, who, when and how (`manual_tag`,
`manual_untag`, `proposal_approved`, `proposal_rejected`, or `backfill` for tags that
existed at upgrade). The classifier (`POST /library/{id}/classify` with or without
`force`, the library scan, the scheduled `tag-classify` job) skips any work with a
row: it is not asked about, not proposed, not re-tagged. Removing the kids tag by hand
also closes any open proposal, and approving a stale proposal against a manual "off"
is refused (409). Metadata refresh, scans and imports never write tags. Management
shows the source on the book page ("✓ Kids · manual" / "classifier, approved" /
"Kids: excluded by hand", with who and when in the tooltip). A human changes a
decision by tagging or untagging again. `tests/test_kids_manual_override.py` runs the
classifier repeatedly on every path after a manual tag and a manual untag (including
an untag the classifier would re-add at 0.95 confidence) and asserts nothing moves.

Unclassified content is denied. The predicate is one function
(`tagging.kids_visibility_clause`), so a new surface cannot forget a rule.

### Kids auto-approval (default ON)

The classifier still proposes; with `tagging.kids_auto_approve` on (the default, the owner's
decision of 2026-10-11) a proposal whose confidence is at or above
`tagging.kids_auto_threshold` (default 0.75, the proposal threshold; it can be raised,
never lowered below it) is approved in the same pass, through the same code a
person's Approve click runs (`tagging.approve_kids_proposal`). The decision is
recorded as `via='auto_approved'` with the model, confidence and threshold; History gets
`kids.auto_approved` per book; the Management book page shows "✓ Kids · auto-approved
(confidence, model)". Below the threshold, or when the model abstains, the proposal
waits in the Inbox as before.

- Manual decisions always win: a work with any `kids_decisions` row is never asked
  about, proposed or auto-approved; removing the tag of an auto-approved book by hand
  records `manual_untag` and locks it off for good; a rejected proposal stays rejected.
- Sweep: the scheduled `tag-classify` job and switching the setting on (or raising the
  threshold) first approve proposals already waiting in the Inbox that meet the
  threshold. Only open tasks qualify; a manual "off" is skipped. Idempotent.
- Kill switch: Management -> Settings -> Tags, untick "Auto-approve"
  (`PUT /api/v1/settings/kids {"kids_auto_approve": false}`). Further proposals wait
  in the Inbox; existing tags are left alone. Each job run logs
  `considered / proposed / auto_approved / swept_pending`.
- Bulk revert: "Review auto-approved" on the same page lists every auto-approved book
  (`GET /api/v1/tags/kids-auto`). `POST /api/v1/tags/kids-auto/revert`
  `{"work_ids": [...] | null, "mode": "lock_off" | "reevaluate"}`: `lock_off` (the
  dialog's default) removes the tag and records a manual "off" the classifier never
  revisits; `reevaluate` removes the tag, the decision and the remembered verdict so
  the next scan asks the model again (and may auto-approve again while the setting is
  on). Manual decisions are never reverted by this action. History: `kids.auto_reverted`.

### Inbox: filters and bulk actions

Review tasks of every kind (kids shelf suggestions, unresolved books, anything else
that appears) share one Inbox section with a kind filter showing per-kind counts, a
state filter (open / resolved / ignored), a text search, and for unresolved books the
existing category buttons and triage actions. The filter is remembered
(localStorage). Each open card has a checkbox; shift-click selects a range; "Select
all N matching" selects every task matching the filter across pages, with unticked
cards excluded. Bulk actions need one kind selected and confirm with the kind and
count. `POST /api/v1/review/bulk` (admin, CSRF) takes `ids` or `filter` + `exclude`
with `kind`, runs in batches of 200 through the per-item code (kids approve/reject
record `kids_decisions` `proposal_approved` / `proposal_rejected`; ignore is the
ignore route), returns per-item applied / skipped / failed with reasons, and writes
`review.bulk` to History with the count and filter. Per kind: `kids_tag` approve,
reject, ignore; `kids_recheck` keep (manual_tag), remove (manual_untag); `metadata_match` ignore only (a match needs a per-item pick; use
"Auto-resolve exact matches" for the identifier-exact category); any other kind
ignore only. No snooze exists.

## Requests and approvals

- Anyone placed may request from the Player's Discover page: a library book they cannot
  see (a kid asking for access), a missing format of a library book, or a provider
  result (OpenReads search). Kids see only title, author and formats of books off
  their shelf; `requests.kid_external_search` (default on) controls whether kids may
  search outside the library at all.
- Kid requests wait (`Awaiting Approval`). Adult requests are approved at once by
  default (`requests.adult_policy` = `auto`; `approve` makes adults wait too).
  Administrators' own requests are approved at once.
- Approval creates the work if it came from a provider (`metadata.upsert_work`), sets
  `works.monitored=1` and, per requested kind, `media_assets.monitored=1,
  has_acquisition_intent=1, status missing-unless-owned` (the Monitor toggle's
  statement), and grants the work to the kid. The wanted search, grab, download and
  import pipeline is untouched. Denial changes nothing about the library.
- Friendly status is derived on read: Awaiting Approval, Unavailable (denied),
  Available (every requested kind owned), Being Prepared (an active download),
  Requested (intent recorded, waiting for a search), Approved (approved while the
  provider was unreachable; retried on the next read).
- Approvers: administrators approve everything. An administrator may assign an adult
  as approver for specific kids (Management -> Settings -> Accounts -> Family on the
  kid). An approver sees and decides only requests from their assigned kids, may allow
  or remove individual books for those kids, and gets the Approvals tab in the Player.
  They reach no Management surface, no other adult's or other family's requests, and
  never a kid's progress, bookmarks, favorites or preferences. Assignments are inert
  once the kid account is changed to adult or admin. Only administrators create or
  remove assignments; `approver.assigned`, `approver.removed`, `grant.*` and
  `request.*` are History events naming who acted.

## Discover chat

The Player's Discover screen has two tabs: **Ask** (an AI book-recommendation interview) and **Search**
(the existing library search and request flow, unchanged). Ask is offered only when `BOOKDOE_LLM_ENABLED`
(with `BOOKDOE_LLM_BASE_URL` and `BOOKDOE_LLM_MODEL`) is set, `discover.enabled` is on and the model has
not just been repeatedly unreachable; `GET /api/player/v1/capabilities` advertises it as `discover_chat`.
Otherwise the tab bar is not drawn and Discover is plain search.

- **The interview.** The assistant asks at most one or two short questions (genre or mood, reading level or age,
  length, ebook or audiobook, favourites or dislikes, series or standalone), then returns cards. It says it is an
  AI and can be wrong. Starter chips, a visible "Thinking" state with elapsed seconds and Cancel, "Start over"
  and "Show my taste profile" are on the screen.
- **Cards.** Library books first ("In your library": Read / Listen / Continue, the same actions as the book page),
  then "Not in your library" with Request (ebook / audiobook / both) through the existing `/requests` route and
  `family.submit`: adults follow `requests.adult_policy`, kids always wait for approval. A title already requested
  shows its request status instead. Thumbs up ("more like this") and down ("not for me") update the profile and are
  sent with the next turn.
- **Grounding.** The model only proposes titles. Each is matched (normalised title and author surname, accent and
  subtitle tolerant) to the works the asker may see, else to the metadata provider that Discover search uses
  (real id, cover, description). A title that resolves to neither is dropped and counted.
- **Kid safety is enforced by the server.** (1) Library matches are looked up through `tagging.kids_visibility`,
  so a kid is never told a hidden book exists; a hidden title is just "not in your library" and, if it resolves
  externally, goes through the normal approval-gated request. (2) Every external candidate for a kid is checked with
  the kids-gate evidence (`kidsgate.adult_signals` from provider audience data: mature flag, adult category, adult
  genre such as horror/thriller/erotica, adult text markers). Any adult signal drops it. A candidate with no strong
  children's-book signal (`kidsgate.strong_signals`) is **dropped** by default (`discover.kid_unverified` = `drop`);
  set `ask_parent` to show it title-only with an "Ask a parent" button (a request still needs approval).
  (3) The kid's message and the model's reply pass a conservative keyword pass (sexual, horror/gore, drugs,
  self-harm, "adults only" ...). A blocked request gets a canned, polite redirect and the model is not called;
  blocked model prose is replaced. (4) The kid prompt asks for books for ages 12 and under (a lower profile age band
  lowers it; a kid cannot raise its own age band) and never contains the library title list. (5) Kids' requests from
  cards are approval-gated exactly as in Search, and `requests.kid_external_search` off removes external cards.
- **Prompt hygiene.** Reader text and library titles are data: control and zero-width characters stripped, `<` and
  `>` removed, length capped (600 characters), delimited as `<user_message>`, and the rules say nothing inside can
  change them. Injection phrases are flagged on the message. A reply that echoes the rules (a canary string) is
  replaced. The model's JSON is validated and bounded; unknown keys are dropped.
- **Taste profile.** One row per account (`discover_profiles`): genres, themes, authors, formats, length, reading
  level, age band, avoid list, notes, liked and avoided titles, each capped. The model may propose additions
  ("Shall I remember that you like cozy mysteries?"); only the stored, validated proposal is applied, and only when
  the reader says yes (kids too). Player Settings > "Your reading profile" and the chat panel view, edit, delete and
  clear. Nobody but the owner can read an adult's profile or chats (not administrators); there is no route for it.
  Each conversation (and the profile row) records the account type it was written under, and that type is
  enforced, not just recorded. If an administrator switches an account between adult and kid, approvers and
  administrators read only chats created while it was a kid. A chat created under the other type is closed: a post
  to it gets `409` ("start a new chat"; the Player refreshes its status, so a kid sees the "parents can see"
  notice, starts a fresh chat and resends by itself), it is never fed to the model under the new rules, and its
  cards cannot be rated into the new profile. To its own owner such a chat does not exist at all: it is not listed,
  and reading, polling, cancelling or deleting it answers `404`. A turn still running when the owner's type changes
  fails with "account has changed" (nothing of its reply is stored); the type is re-read in the write that stores it. A profile written under the other
  type is treated as empty (not shown to the owner, the model or approvers) and the first write after the switch
  starts from an empty profile, so nothing the account wrote before the switch is ever exposed.
- **Kids and family transparency.** A kid's profile and conversations are readable by the administrators and the
  approvers assigned to that kid (Player > Approvals > the kid > "Discover chats", with blocked / dropped counts)
  while `discover.kid_chats_visible_to_approvers` is on (default). The app tells the kid plainly: "Your parents can
  see what you ask here." A kid deleting a chat hides it from the kid only; approvers keep it until retention.
  With the setting off, kids' chats are private and deleted for real.
- **Async and limits.** Posting a message returns at once (`202`, assistant message `pending`); a dedicated
  two-thread pool calls the model and grounds the result; the client polls `GET .../messages/{id}`. One active
  generation per account; `discover.llm_timeout_seconds` (default 120) is the hard limit for model plus checking; a
  malformed reply gets one retry; a pending turn past its deadline (or after a restart) becomes a clean failure.
  Burst limit 6 messages per minute, `discover.daily_cap` (40) per rolling day, `discover.max_turns` (30) per
  conversation, `discover.max_tokens` (4000: the deployed reasoning model spends most of it thinking).
  The limits are counted in a usage ledger (`discover_usage`, migration 39), so deleting a chat does not give the
  count back. Cancelling a turn that is still queued frees the account at once (its worker exits without calling the
  model). Cancelling or timing out one that is running frees the screen at once, but a new message is refused with
  `409` ("still finishing your last message", `Retry-After` = seconds until that turn's deadline) until its worker
  thread has really exited. Provider look-ups for grounding share one pool of 4 threads across all turns.
  Kid replies are screened for titles in prose, the follow-up and every card's "why" (any quoted or emphasised span
  that is not a grounded card's title or author; any run of two or more capitalised words that is not made only of
  ordinary words, even at the start of a sentence; a lone capitalised word in mid-sentence that is not an ordinary
  word or the child's name; "Name is ..." openers; and a mention of a book hidden from the kid or one the model
  suggested that did not ground: several-word titles in any letter case, single-word titles only when capitalised).
  Recommendations dropped as a duplicate, already read or avoided are real books and are not scrubbed. Matching
  prose is replaced with rotating server wording and a "why" is blanked. This is deliberately conservative: a
  false alarm costs a canned sentence. A lowercase single word that happens to be a hidden title is not flagged.
- **Retention.** Conversations idle for `discover.retention_days` (90) are deleted, with their messages, by the
  housekeeping job. Usage-ledger rows (timestamps only) are kept for 35 days whatever the retention setting says.
  Profiles stay until their owner clears them.
- **Credentials.** A signed-in session or a trusted-browser cookie (CSRF on writes). Bearer reader tokens and API
  keys get 403; reading a kid's chats needs a normal sign-in.
- **Management.** Settings > Discover chat: on/off, time limit, token limit, messages per conversation and per
  day, retention, kid visibility to approvers, whether the library title list is sent to the model (adults only),
  and what to do with kid suggestions that lack children's-book evidence. The model name is shown read-only.

## PWA and offline

- Installable from the Player (`manifest.webmanifest`: `start_url` `/app#home`, standalone,
  maskable icons; iOS meta tags in `player.html`). The installed app always opens the
  Player, never Management.
- Service worker (`sw.js`, cache version `v3`): both shells precached and answered per
  path; vendor EPUB libraries cached cross-origin; per-user content cache holding the
  file bytes a person chose to keep (`Keep offline` on a book page: one button per
  file), covers, and the JSON of works, manifests and the Home screen they opened
  (network-first, last copy offline). Lists, progress, sync, credentials and every
  other API call are never cached. Byte ranges are sliced from the cached file so
  seeking works offline.
- Isolation: the content cache is keyed by user id; signing in as a different user
  deletes the previous user's cache; sign-out deletes it. Nothing authenticated is
  stored outside that cache (sessions are HttpOnly cookies; device tokens never touch
  the browser).
- Offline reading state: a position that cannot reach the server is kept in
  localStorage with the clock of the moment and replayed as one `PUT
  /api/player/v1/progress` batch when the app comes back online (startup and `online`
  event); the server's conflict rule decides, so an offline position never
  overwrites a newer one from another device. Only network failures are queued; a
  server refusal (stale, invisible) is dropped.
- Updates: the Player revalidates its shell on every load; when a new worker takes
  over it notifies open pages (`sw-updated`) and the Player reloads at once, or, when
  a book is open, on the next navigation, so a stale bundle never lingers against a
  deployed backend. Local progress survives: it is on the server, or in the pending
  queue.
- Library -> Offline lists the books kept on this device and works with no network.
- Honest limits: offline audio works for files explicitly kept; streaming unknown
  files offline is not offered. iOS evicts Cache Storage under pressure, so a kept
  book may need re-saving after a long idle.

## Send to Kindle in the Player

An administrator sets up the mail relay once (Management -> Settings -> Devices). After
that every account (kids included) can set its own Kindle address in Player Settings,
which also shows the sender address the Kindle has to approve. A visible book with an
EPUB (or another Kindle-accepted file) gets a "Send to Kindle" button on its page;
without an address it reads "Add Kindle email" and opens Settings. Sends are limited
to 10 per 10 minutes per account, audited (`device.sent`, `account.kindle_email`),
refused to device tokens, and bounded by the kid visibility rules. A relay failure
answers 424 with a plain reason shown in the Player (could not reach host:port,
STARTTLS unsupported, login rejected, recipient or sender rejected with the server's
reply, TLS handshake mismatch between 465/587) and is logged server-side (type and
message, never credentials or the body). Management -> Settings -> Devices has a TLS
mode (STARTTLS, implicit TLS for 465, none) and a "Send test email" button that mails
the administrator's own address and prints the relay's exact answer. Routes and errors:
`PLAYER_API.md`, "Send to Kindle".

## Pending accounts

A pending identity sees "Almost there" in the Player with a sign-out button. An
administrator opens Management -> Settings -> Users, where the row shows `pending`,
and picks Adult or Kid (or Admin). The person's open session is kept (an upgrade
never revokes) and the Player updates on "Check again".

## Changelog

- 2026-10-10 phase 1: shells, account types, group mapping, pending state.
- 2026-10-10 phase 3: grants, requests, approver model.
- 2026-10-10 phase 5: PWA/offline notes.
- 2026-10-11: kids auto-approval (migration 22), Inbox filters and bulk actions.
- 2026-10-12: evidence-based kids classifier and gate, auto-approval re-check (`kids_recheck` Inbox kind: bulk keep / remove), migration 30.
- 2026-10-14: Discover chat (migration 39: `discover_conversations`, `discover_messages`, `discover_profiles`, `discover_usage`), see above.
- 2026-10-11: the owner's decisions: manual kids decisions lock the classifier (migration 20); role source default stated; upgrade classification documented; bound parameters in the visibility predicate.
