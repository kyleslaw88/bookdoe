# Domain model

The core architectural rule is that a **book is not a file and is not an edition**.

```text
Contributor
    |
    +-- WorkContributor -- Work -- SeriesMembership -- Series
                              |
                              +-- Edition -- ExternalIdentifiers
                              |
                              +-- MediaAsset (ebook)
                              |       +-- MediaFile (EPUB)
                              |       +-- MediaFile (PDF)
                              |
                              +-- MediaAsset (audiobook)
                                      +-- MediaFile (M4B)
                                      +-- MediaFile (part 01 MP3)
                                      +-- MediaFile (part 02 MP3)
```

## Work

A Work is the conceptual intellectual work. It owns monitoring state and can be associated with multiple contributors, editions, series memberships and media assets.

## Edition

An Edition describes a publication manifestation: publisher, release date, language, ISBN/ASIN/provider IDs, cover and other edition-specific metadata.

## MediaAsset

A MediaAsset is bookdoe's desired/owned representation of a Work. Ebook and audiobook state are independent. A user may own an EPUB but still be missing and monitoring the audiobook.

## MediaFile

A MediaFile is a physical path. Multipart audiobooks are therefore naturally modeled as one audiobook MediaAsset with several MediaFiles.

## Derived assets (converted copies)

A MediaAsset with `derived_from_asset_id` is a **converted copy** bookdoe made from another asset's files (an M4B from MP3s, an EPUB from a MOBI). It is a separate row so the original stays exactly as it was, but it is not a second owned copy: it never counts toward ownership, wanted or missing state, acquisition intent, duplicate detection, upgrade searches or quality evaluation, and an asset lookup that picks "the" asset of a kind for acquisition or monitoring skips it. It belongs to the same Work, so kid visibility and Player rules apply to it exactly as to the Work. A real asset of the same format (an import, a scan) supersedes it; a Work merge keeps its source link. `conversions` records the queue and history of the jobs that make them. A derived asset's files do not have to sit beside its source: when the source lives in a read-only (adopt/observe) root the converted file is written into the managed root of the same kind (`<root>/<Author>/<Title>/<Title>.<ext>`) and the derived asset's `root_folder_id` is that managed root while the source asset keeps pointing at the read-only one.

## Managed copies and preserved sources (managed library build)

A book in an **adopted** (read-only) root can get a verified copy in a **managed** root (services/managedbuild.py).
`media_assets` is unique per `(work, kind, format)`, so a same-format copy cannot be a second asset; and a second
`media_files` row on the same asset would be counted, listed, streamed, offered for removal and matched as a duplicate
by every query that reads the files of an asset. So the model is:

- the **active** `media_files` row keeps its id and is re-pointed at the managed copy (path, relative path,
  `managed=1`, `adopted=0`, the verified sha256). Everything that references the file or asset id (reading and
  listening progress, bookmarks, Player manifests and streaming, OPDS, Send to Kindle, merges, the redundant-files
  analysis, quality, library health) follows it without a remap, and nothing is counted twice;
- the **original** stays on disk, untouched, as a *preserved source*: a `managed_copies` row holds its path, root,
  size and signature and a snapshot of the original file row (for Undo), linked by `file_id` to the active row. It is
  not a library file: never listed, counted, edited, offered for removal or trash; the scanner, the managed-root
  reconcile and Inbox adoption skip it (`managedbuild.is_build_path`), so a rescan never re-imports it as a duplicate.
  Admins see it on the book page under "Source copies";
- a **converted** book (MP3 set -> M4B, MOBI -> EPUB) gets a derived asset in the managed root exactly as a normal
  conversion makes one (see Derived assets); its `managed_copies` row (kind `convert`) points at it, with
  `created_by_build` = 1 only when the build created that asset (0 when it re-used one that already existed). The
  source asset stays the owned asset. Undo deletes a derived asset only when the build created it and nothing refers to
  it (no progress, bookmarks, download or further derivative): history is never cascade-deleted by an undo;
- the switch happens per book in one transaction after every file is verified; until then the original is active.
  Undo reverses it from the snapshot and moves the copies to the managed trash.

## External identifiers

Provider IDs never define internal identity. UUIDs are canonical internal IDs; ISBN, ASIN, Open Library, Google Books and other identifiers are namespaced evidence attached to entities.

## Monitoring

Monitoring exists at multiple levels:

- Work: whether bookdoe is interested in the work at all.
- MediaAsset: independent ebook/audiobook desire.
- Author monitor: policy such as all/future/latest/none plus independent ebook/audiobook flags.
- Series monitor: policy plus media flags.

This model replaces the two-Readarr-instance workaround.

## People and their state

Users are Authentik identities (or local family accounts) with one account type:
admin, adult, kid or pending. Everything personal hangs off the user, never off the
work: `media_progress` (per user per asset: position, location, percent, device,
client clock), `bookmarks`, `favorites`, `user_preferences`, `requests`. Kid access is
`content_grants` (user, work) and `tag_grants` (user, tag) on top of the `kids` system
tag. `approver_assignments` (adult, kid) says who may decide a kid's requests.
`devices` are per-user credentials for the Player API. None of these change what a
Work, Edition, MediaAsset or MediaFile means; requests only ever record acquisition
intent on a MediaAsset.
