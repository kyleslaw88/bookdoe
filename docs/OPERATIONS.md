# Operations

## Health

Both applications expose health endpoints. bookdoe's authenticated system health view also reports OpenReads, indexer, downloader and root state.

bookdoe has two unauthenticated probes. `/health` only proves the process answers. `/readyz` is the one Docker's HEALTHCHECK and `scripts/deploy.sh` use. It returns 200, or 503 when a check fails:

- the database accepts a write (a `BEGIN IMMEDIATE` plus a rolled-back header write on a fresh connection);
- the scheduler loop is alive: it writes a heartbeat (`settings` key `scheduler.heartbeat`) every 15 s tick, whether or not a job is busy, and the probe fails when that heartbeat is older than twice the shortest enabled schedule (floor 300 s). A long job no longer counts against it: jobs run in a worker pool off the loop (`scheduler.tick`), one run per kind at a time, and the check reports them under `scheduler_heartbeat.running` (`kind`, `age_seconds`) for operators. Databases without a heartbeat yet fall back to the newest run time, as before. Skipped when `BOOKDOE_SCHEDULER_ENABLED=0`;
- OpenReads answers. This is reported as `"status": "degraded"` with a 200, not a 503: bookdoe still serves and manages the existing library without OpenReads, and failing the probe would mark the container unhealthy and make a deploy roll back over a sibling service's outage.

The body also carries informational counts: `import_failed_downloads` and `failed_job_runs_24h`. It never contains secrets, paths or upstream error text.

## Build stamp

The running build is shown in Management (nav drawer footer, Settings > About) and in Player Settings, and is returned by `GET /api/v1/version`, `/readyz` (`build`) and the Player API `capabilities`. The image gets it from two build args, `GIT_SHA` and `BUILD_TIME`, which `docker-compose.yml` passes through (`GIT_SHA: ${GIT_SHA:-dev}`, `BUILD_TIME: ${BUILD_TIME:-}`). `scripts/deploy.sh` exports both. A manual deploy on the server, from the compose checkout:

```sh
GIT_SHA=$(git rev-parse --short=12 HEAD) docker compose up -d --build
```

(`BUILD_TIME` is optional; add `BUILD_TIME=$(date -u +%Y-%m-%dT%H:%MZ)` if `date` works.) With neither set the build reports `dev`. Each deploy also changes the service worker's cache name and the asset URLs, so installed apps pick the new build up once; offline books and saved positions are kept. `GET /api/v1/version/details` (administrators) adds the version, process start time, API version and database migration step.

## Jobs

How they run (changed 2026-10-11 after `/readyz` went 503 live: a `tag-classify` batch of
20 LLM calls at up to 300 s each ran inside the scheduler loop and stalled every job
for 20 minutes). The loop only ticks: heartbeat, then dispatch due jobs to a thread
pool. One run per job kind at a time; a kind still running when it is due again is
skipped for that tick (not queued), a manual run of it answers 409, and the Management
classify scan is refused (409) while the scheduled batch runs (and the batch skips
while a scan runs). A run that outlives `BOOKDOE_JOB_LEASE_SECONDS` (default 3600)
is presumed stuck: its lease is released so the kind can run again and its row is
marked failed. Management -> Settings -> System shows the loop's last tick and the
running jobs with their ages.

The kids classifier batch is bounded: `BOOKDOE_CLASSIFY_BATCH` books per run
(default 5), `BOOKDOE_CLASSIFY_BUDGET_SECONDS` of wall clock after which no new book
is started (default 120; the call in flight finishes), and
`BOOKDOE_CLASSIFY_LLM_TIMEOUT` per call (default 120; other LLM callers keep
`BOOKDOE_LLM_TIMEOUT`). The job result records `considered`, `proposed`,
`auto_approved`, `abstained`, `errors`, `swept_pending`, `budget_stopped`,
`elapsed_seconds` and `remaining`.

Kids auto-approval: the `tag-classify` job now applies confident kids-shelf proposals
itself (default on). To stop it: Management -> Settings -> Tags, untick Auto-approve, or
`PUT /api/v1/settings/kids {"kids_auto_approve": false}`; existing tags stay, and
"Review auto-approved" on the same page reverts them in bulk. Details in
`PLAYER_MANAGEMENT.md`.

Persistent scheduled jobs include:

- download queue polling;
- wanted automatic search;
- metadata refresh;
- author bibliography discovery;
- library file health;
- daily database backup;
- housekeeping;
- `convert-audio` / `convert-ebook`: drain the conversion queue (below);
- `managed-library-build`: manual only, never scheduled (see "Building the managed library").

Job runs record outcome, error, duration and result data. Jobs can be forced from Settings.

### Format conversion (M4B, EPUB)

Settings -> Conversion, the book page's "Convert to M4B / EPUB" button and the library select mode's "Convert selected to preferred formats" queue conversions in the `conversions` table; the `convert-audio` and `convert-ebook` jobs (every 60 s, and started at once by a request) work it off, one run per kind, `audio_concurrency` / `ebook_concurrency` conversions at a time (default 1 + 1), niced. Originals are never touched: the result is a new file next to them (`<root>/<author>/<title>/<title>.m4b`, written as a hidden `.partial` file and published without ever replacing a file) and a derived asset. Only books in a **managed** root are converted. Before starting, a conversion needs its estimated output plus `disk_margin_mb` (default 1024) free, else it waits and retries. A failed attempt is retried after `retry_backoff_minutes` (15, doubling) up to `max_attempts` (3), then parked as `failed` with the reason until someone presses Retry; a DRM-protected or unsupported file is `skipped` at once. Audio is encoded with ffmpeg (AAC, 64 kbps mono / 96 kbps stereo, chapters from file boundaries); ebooks are unpacked with KindleUnpack (`mobi`, GPL-3.0, run as a subprocess) and need no Calibre. After a restart a conversion that was running goes back to the queue and its partial file is removed. "Convert automatically on import" is off by default.

### Building the managed library

The managed library build copies every book from the **adopted** (read-only) roots into the **managed** roots, in the
preferred formats, and switches the library over to the copies one book at a time. The originals are never written,
renamed, moved, linked into or deleted; they stay exactly where they are as *preserved sources* (see
`DOMAIN_MODEL.md`). Code: `services/managedbuild.py`; tables `build_plans` / `build_plan_items` / `build_runs` /
`managed_copies` (migration step 41; 39 and 40 belong to the discover-chat and bulk-duplicates steps). It is a manual-only job (`managed-library-build`): it never runs on a schedule and
nothing starts without a confirmed dry run.

**The run switch (`managed_build.run_enabled`, default OFF).** Running a build needs an explicit admin setting: the
"Allow builds to run" switch at the top of the build panel (`PUT /api/v1/managed-build/settings {"run_enabled": true}`,
audited as `managed_build.run_enabled`). While it is off, Start and Resume answer **409** with "Running a managed
library build is switched off..." (also the v1 `/jobs/managed-library-build/run`); the dry run, the plan and its file
list, Confirm, Pause, Cancel and **Undo of finished work stay available**. **Turning it off pauses a running build
after the current file** (the workers check the switch between files; the build becomes *paused* with the reason
"Paused because "Allow builds to run" was switched off"); switch it on again and press Resume to continue. Switch it on
for the pilot and the full run, and off again afterwards.

**Roots must be stored at their real path.** The preflight (Start and Resume) refuses when a managed or source root's
stored path is not its real path (a symbolic link anywhere in it, e.g. `/data/books` where `/data` is a link to
`/mnt/user/data`): every safety check compares paths under the stored root. The message names the real location;
edit the root in Settings -> Library roots to that path and run a new dry run.

**Where:** Management -> Settings -> Library roots. The banner at the top says "N of M books have a verified managed
copy (X of Y)" with **Build managed library...**; every adopted/observe root has **Copy into managed library...**
(the same panel, limited to that folder). Settings -> Jobs, the Overview card and a book page ("Copy to managed
library..." for that one book) open the same panel. Admins only; kids and other accounts see none of it.

What each book gets (option `mode`, default `preferred`):

- **Copy**: already EPUB or M4B. Every file is copied.
- **Convert**: MOBI/AZW/AZW3 -> EPUB, an MP3/M4A/FLAC/Ogg/Opus set -> one chaptered M4B, with the conversion engine's
  tools (KindleUnpack, ffmpeg), written straight into the managed root and recorded as a *derived asset*, exactly like
  a normal conversion. The source asset stays the owned asset.
- **Copy as it is** (`non_convertible: copy`, default) or **Skip** (`skip`): PDF, CBZ, DRM-protected and anything else
  with no preferred target. A book that already has a real EPUB/M4B asset copies that one; its MOBI is copied as it is
  or skipped.
- **Skip** with a reason: missing or unreadable file (one missing track holds back the whole set), a symlink (never
  followed), already copied or converted, no managed root of that kind, path too long. `mode: as_is` copies everything
  unchanged (no conversion).

Single files are named by the naming template (`{Author}/{Title}/{Title}.{Ext}` by default). A multi-file book keeps its
own file names inside the templated book folder, so its part order and the track titles the Player shows stay the same.
A name already taken is never overwritten: an identical file nobody else uses is reused (nothing copied), anything else
gets " (2)", case-insensitively.

**Run a pilot first.** In the panel pick a folder, a kind and "First N books" (say 5), press **Dry run**, read the
summary (books, files and size by action, skip reasons, destinations with free space against what is needed plus a
5% / at least 1 GB margin, an estimated time, warnings, the file list with filters), type the number of books, press
**Confirm**, then **Start build**. Check a few of those books in the Player, then run the whole library the same way.
A plan expires after 24 hours and runs once; only the newest dry run can be confirmed; one build at a time.

Options: `verify` `full` (default: every copy is re-read from the disk and its sha256 compared with the source
stream's; `size` compares sizes only; `none` warns loudly), `throttle_mb_s` (default 50; 0 = no limit, shared by all
workers) and `concurrency` (books at a time, 1 or 2) keep Plex and Immich responsive on the same NAS, `stop_on_error`.
The API is `POST /api/v1/managed-build/plans` (options), `GET .../plans/<id>` and `.../items?action=&state=&kind=&
root_id=&q=&collision=&limit=&offset=`, `POST .../plans/<id>/confirm {"book_count": N}`, `POST .../plans/<id>/start`,
`GET /api/v1/managed-build/status`, `POST .../runs/<id>/pause|resume|cancel`, `.../items/<id>/retry|skip`,
`.../runs/<id>/undo {"confirm_count": N}`, `GET .../runs/<id>/journal`; per book `GET|POST /api/v1/works/<id>/managed-copy[/undo]`.

**How a file is copied:** the source is re-checked against the signature the dry run recorded (size, mtime, inode,
device; a changed source is refused, re-plan it), then streamed in 8 MiB chunks to
`.alx-build-<run>-<item>-<random>.part` (an unpredictable name) inside the destination folder while hashing, fsynced,
verified, and published with a hard link that fails if the name exists (or `renameat2`
RENAME_NOREPLACE where links are unavailable), then the source is checked again (changed during the copy: discarded and
retried). When every file of the book is in place, ONE database transaction re-points the book's file records at the
copies; until then the original is what the library uses.

Every write goes through directory handles: the destination folders are created with mkdirat and opened with
O_NOFOLLOW|O_DIRECTORY one level at a time from the managed root (a symlink or file on the way is refused, never
followed; each new folder's parent is fsynced), temp files are created with O_CREAT|O_EXCL|O_NOFOLLOW through the
folder handle, and removals (temps, unrecorded copies, the crash-recovery sweep) open the folder from its managed root
the same way and refuse anything outside a managed root. **Conversions** write into a temp file bookdoe created that
way: ffmpeg and ffprobe are handed the open file as `/proc/self/fd/N` (where `/proc` is missing the encode goes to a
private 0700 scratch folder and is copied in), KindleUnpack works in a private 0700 temp folder and the EPUB is written
into the open file. No tool ever opens a name inside the managed root, so a symlink planted there cannot redirect a write.

**Expected duration and space for production (361 GB, about 3,825 adopted files):** copying alone is bound by the speed
limit: about 2 hours at 50 MB/s, about 3 hours with `full` verification (the re-read). Conversions dominate a
`preferred` run: an MP3 audiobook is re-encoded to AAC at roughly 20-40x real time on the NAS CPU, so a library of
hundreds of MP3 sets takes days, not hours; the dry run's estimate (marked rough) is computed from the real durations,
and a pilot run measures the real throughput for the next estimate. `mode: as_is` (no conversion) is the quick
alternative. Space: what the dry run shows per destination; for copies about the source size (361 GB) plus the margin,
converted M4Bs at 64/96 kbps are usually smaller than their MP3s.

**Pause, resume, cancel:** Pause and Cancel take effect at the next safe point (between files). Paused, stopped and
interrupted builds continue with **Resume**. Cancel keeps the books already switched (Undo removes them) and removes
copies that were not recorded yet. A restart of the app turns a running build into *interrupted*: at startup its
journal is settled (temp files removed, a publish that happened kept, the rest re-queued) and it waits for Resume;
nothing restarts by itself.

**Failures:** a book that fails (verification mismatch, I/O error) is retried up to 3 attempts in total with a backoff
(every attempt of a book counts, whatever step failed), then it is marked failed with the reason and its unrecorded
copies are removed; the build goes on (or stops, with `stop_on_error`). A copy that disappears or changes after it was
published but before its book was recorded is copied again (within the same 3 attempts). An unexpected error (a bug,
a database error) never leaves a build "running" with nothing working on it: the book in hand is marked failed, the
build stops with the error as its reason and its claims are released, so **Resume**, **Cancel** and **Undo** work.
A transient "database is locked" inside a book is not a failure: the book waits and is tried again later, without
using one of its 3 attempts. A run-level stop (out of space, no safe publish on the managed root) is not an attempt
of the book in hand either: after Resume it has its full budget. The panel lists failures with **Retry** and **Skip**. A full disk (ENOSPC, or free space below the
margin before the next book) stops the build cleanly, nothing half-written; free space and Resume. A permission error
names the folder.

**Unraid / FUSE:** publishing needs hard links (or `renameat2` NOREPLACE) on the managed share. The preflight probes
each managed root once (a throwaway file in its `.bookdoe-trash` folder). If the share cannot give a no-clobber
publish, the build refuses to start with a clear message, and a build whose links stop working stops: bookdoe never
falls back to an exists-check plus rename, which could overwrite. Fix: Settings -> Global Share Settings -> Tunable
(support Hard Links) = Yes, or point the managed root at a disk path (`/mnt/diskN/...`) or a pool.

**FUSE page cache (Unraid `shfs`):** `verify: full` re-reads every copy after an fsync and a POSIX_FADV_DONTNEED hint,
but on a FUSE mount such as Unraid's `/mnt/user` the kernel may still answer that re-read from its page cache, so it
proves the bytes the kernel holds, not necessarily the bytes on the disk. For the strongest check point the managed
root at a disk or pool path (`/mnt/diskN/...`, `/mnt/cache/...`, not `/mnt/user`), or verify later: after a reboot (or
`echo 1 > /proc/sys/vm/drop_caches` on the host) a deep file verification (`POST /api/v1/files/<id>/verify?deep=true`,
the book page's file verify) re-reads a copy and compares it with the sha256 the build recorded. There is no
whole-library "verify later" pass for the build yet; spot-check a sample of books after a large run.

**Verify afterwards:** the banner and the Overview card count books with a verified managed copy; each book page says
"Managed copy: verified / converted / Not copied yet / Copying n%" and lists the originals under "Source copies"
(admin). A library rescan after a build is a no-op for these books: the scanner leaves preserved originals and managed
copies alone. History has one `managed_build.copied` entry per book plus the run's start and end.

**Undo build:** per run (Build history -> type the number of books -> Undo build) or per book (book page -> Undo
managed copy). The copies move into the managed root's `.bookdoe-trash/<date>/` (kept for the trash retention, listed
in Settings -> System; Restore is refused for them, run the build again instead) and the records point at the originals
again, one transaction per book. Refused, with nothing changed for that book and the reason in the result
(`source_gone`): a book whose original is gone or changed (it would be left without a file); for a **converted** book
every source file of the conversion is checked against the signature saved at planning, and if any is gone the
converted file stays the book's file. The trash never purges (and Restore explains) an Undo-build copy whose original
has gone since: it may be the book's only file; move it back by hand and rescan. Also refused: a book whose managed copy was renamed or moved since the
build (its record no longer points at the copy the build made; move it back or leave it, the original stays
preserved). **Converted books** (the derived EPUB/M4B) are only deleted when this build created that asset AND it has
no listening/reading history: an asset with saved progress, bookmarks, a download record or a book converted from it,
or one that existed before the build, is **kept** with its file and listed as "kept: has listening/reading history"
(or "existed before this build"); both confirm screens say so. A trash name already taken gets " (2)"; a copy that
cannot be moved is reported (it stays in place and scans keep ignoring it). The move into the trash goes through
directory handles opened from the managed root with O_NOFOLLOW (trash folders made with mkdirat), checks that the file
is the copy the build made (device and inode) right before it moves, and never replaces anything: a hard link then
unlink, or `renameat2` RENAME_NOREPLACE where links are unsupported; with neither, the move is refused and the copy
stays in place. An undo cut short by a restart is finished at startup.

### Metadata refresh: batch size and provider traffic

`metadata-refresh` is disabled by default on purpose. Each run (1) re-syncs up to 50 works that already carry an OpenReads id (one `get_work` each) and (2) tries to match up to `batch` works that have no OpenReads id (one OpenReads `/api/v2/resolve` call each, which fans out to Open Library and Google Books). `batch` is read from the job's `config_json` (`{"batch": 200}`), default 25, clamped to 1-1000; `schedule_seconds` is unchanged (default daily).

What enabling it with `batch` 200 hourly means: up to 200 resolve calls plus up to 50 syncs per hour, i.e. about 6,000 provider-bound requests per day, enough to cover the ~5,900 unmatched works in roughly a day, after which only new imports cost anything. Rate-limited answers are recorded and retried later rather than counted as "no match". Start with `batch` 50 and look at the job result (`newly_linked`, `needs_review`, `retrying`) before raising it. Matches below 0.95 confidence become review tasks, so a large batch also grows the Inbox.

### Inbox triage

Open `metadata_match` review tasks are grouped into identifier-exact, has-owned-files, hopeless and other (`GET /api/v1/review/triage`). Admins can run `POST /api/v1/review/triage` with `{"action": "auto_resolve" | "ignore_hopeless", "limit": N}`. `auto_resolve` links each work to the stored provider candidate that shares an exact identifier with it (same path as picking a match by hand; one provider fetch per task, so keep `limit` modest, default 100). `ignore_hopeless` dismisses tasks whose work has no identifiers, no owned file and no stored candidate; it changes nothing about the books.

## Acquisition flag

Indexers, download clients, wanted search, the download queue and their scheduler jobs (`download-poll`, `wanted-search`, `upgrade-search`, `author-discovery`) sit behind one flag, the `acquisition_enabled` setting. While it is off, the acquisition routes answer 404 as if absent and those jobs do nothing. For what the integration does and your responsibilities when using it, see [INTEGRATIONS.md](INTEGRATIONS.md).

| Install type | Default |
| --- | --- |
| New install | off |
| Existing install, on upgrade | on if it already has an indexer or a download client, otherwise off (migration step 48) |

A value already stored is never overwritten by the migration.

Env override: `BOOKDOE_ACQUISITION_ENABLED` wins over the stored setting in both directions. `1`, `true`, `yes` and `on` turn it on; `0`, `false`, `no` and `off` turn it off. Unset or any other value falls back to the stored setting.

A change takes effect after a restart: restart the container after editing the env var or the stored setting (`docker compose restart bookdoe`).

## Deploy

`sh scripts/deploy.sh` on a server checkout: refuses on a dirty tree, fast-forwards
to `origin/main`, takes a backup (the same in-process `create_backup()` the daily job
and "Back up now" use, tagged `predeploy-<sha>`), builds and tags both images
`bookdoe:<sha>` / `openreads:<sha>` (also re-tagged `:local`), restarts, waits for
`/readyz`, then checks OpenReads specifically: `/readyz` reports an unreachable
OpenReads as `"degraded"` (200, not 503) day to day so an unrelated sibling outage
never rolls back a healthy bookdoe, but this deploy just rebuilt and restarted
OpenReads too, so a degraded OpenReads right after it is the deploy failing, not an
unrelated outage -- `scripts/deploy.sh` fails on it where a plain `/readyz` poll would
not. `sh scripts/deploy.sh --check` shows the pending commits and changes nothing.

Both services are single-container (no replicas), so there is no drain on/off toggle.
What stands in for one: the in-process job scheduler (download polling, conversions,
metadata refresh, search, the backup job itself) already waits for its running jobs
to finish on SIGTERM (`app/main.py` lifespan -> `scheduler.shutdown_pool(wait=True)`).
`deploy.sh` backs that up on both sides: it polls `/readyz`'s reported running jobs
and waits (bounded, `DRAIN_WAIT_MAX_S`, default 300s) for them to clear before
restarting, and passes a generous `--timeout` (`STOP_GRACE_S`, default 60s) to
`docker compose up` so the container is not SIGKILLed before that in-app wait
finishes if something is still running when the wait gives up.

Roll back without a rebuild:

```sh
sh scripts/deploy.sh --rollback          # back to the sha the last deploy/rollback replaced
sh scripts/deploy.sh --rollback <sha>    # a specific sha, if bookdoe:<sha> and openreads:<sha> are still on this host
```

It checks both tagged images exist locally before touching anything running, then
restarts (no build, no migration, no backup) and runs the same health and OpenReads
checks. `.deploy/current` and `.deploy/previous` (host-local, git-excluded) track
what is live and what a bare `--rollback` goes back to; `.deploy/history.log` records
every attempt (`OK` / `FAILED` / `ROLLED_BACK` / `FAILED_ROLLBACK`).

## Backup and recovery

bookdoe writes verified backups under `/config/backups` by default. The default retention is 14 backups and is configurable with `BOOKDOE_BACKUP_RETENTION`.

The easy way is the script, run from the compose checkout on the server:

```sh
sh scripts/restore.sh $BOOKDOE_CONFIG_DIR/backups/bookdoe-<stamp>.db --yes
```

It refuses to run without `--yes`, verifies the SHA-256 against the sidecar `.json` (aborting with nothing changed on a mismatch), stops the container, moves the live database and its `-wal`/`-shm` to `bookdoe.db.bak-<timestamp>`, copies the backup into place, starts the container and waits for `/readyz`. `LIVE_DIR` (default: `BOOKDOE_CONFIG_DIR` from the root `.env`, else `./data/bookdoe`) and `HEALTH_URL` (default: port `BOOKDOE_PORT` from the root `.env`) can be overridden. The manual procedure it automates is:

A restore should be performed while bookdoe is stopped:

1. stop bookdoe;
2. preserve the current database as an additional rollback copy;
3. verify the selected backup's SHA-256 against its JSON sidecar;
4. replace `/config/bookdoe.db` with the selected backup;
5. ensure ownership matches PUID/PGID;
6. start bookdoe and inspect `/health` and the System page.

Backups live on the same volume as the database. Keep an off-volume copy too (the Unraid appdata backup, or an rsync of the backups directory to the array).

### Rollback after a destructive migration

A normal code rollback (`git reset --hard <prev>` and rebuild) is only safe while every migration is additive. If a migration changed or dropped data, rolling back the code is not enough: restore the `premigrate-*.db` backup that the migration took first (kept apart from the daily backups, newest 5):

```sh
sh scripts/restore.sh $BOOKDOE_CONFIG_DIR/backups/premigrate-<stamp>.db --yes
```

Roll the code back to the matching commit first, then restore. Anything written after the migration is lost.

### OpenReads backup

OpenReads' field locks, manual metadata and mappings are not disposable (its upstream provider data is). bookdoe's daily `database-backup` job also backs up OpenReads' database when it can see it, to the same backups directory with prefix `openreads-` and its own retention (`BOOKDOE_BACKUP_RETENTION`). An OpenReads failure is reported in the job result and does not fail the bookdoe backup. To enable it, mount the OpenReads data directory read-only into the bookdoe service in `docker-compose.yml` and set the env:

```yaml
    volumes:
      - ${OPENREADS_DATA_DIR:-./data/openreads}:/openreads-data:ro
    environment:
      BOOKDOE_OPENREADS_DB_PATH: /openreads-data/openreads.db
```

Restoring it is a manual copy into the OpenReads data directory with OpenReads stopped. The Unraid appdata backup should still cover that path.

## Logs

Use container logs for server diagnostics. Avoid enabling overly verbose HTTP client logging in production because integrations carry credentials.

## Upgrade strategy

Back up before schema/application upgrades. The Player/Management release adds
migrations 17 to 20 (sync columns on `media_progress`; `bookmarks`, `favorites`,
`user_preferences`; `content_grants`, `tag_grants`, `requests`, `approver_assignments`;
`devices`, `device_codes`; `kids_decisions`, backfilled from existing kids tags; 22 rebuilds `kids_decisions` to allow `auto_approved`, rows copied and counted). All are additive: a code rollback to the previous image
leaves the new tables unused and loses nothing already there, and the framework still
writes its `premigrate-*` copy first. New environment variables are optional
(`BOOKDOE_OIDC_GROUP_*`, `BOOKDOE_OIDC_ROLE_SOURCE`, `BOOKDOE_OIDC_BOOTSTRAP_ADMINS`;
see `PLAYER_MANAGEMENT.md`). Behaviour changes to know before deploying: adults no
longer reach Management (administrators only); a NEW Authentik identity not in a mapped
group lands as `pending` instead of a full account (existing accounts keep their
type; the existing administrator keeps Management, see `PLAYER_MANAGEMENT.md`
"Upgrading an existing install"); `/` now serves the Player and
Management lives at `/manage` (Nginx Proxy Manager needs no change: same host, same
container port). The installed PWA picks up the new shell on its next load. Keep acquisition automation disabled during any migration that materially changes matching/scoring semantics, then validate manual searches before re-enabling the wanted-search job.

## Performance budgets

`bookdoe/tests/test_large_library_perf.py` (marked `slow`) seeds 100,000 works and times each real route. The budgets below are the wall-clock limits that file asserts for one request; `tests/test_perf_docs_budgets.py` fails if this table drifts from them.

| Request | Budget |
| --- | --- |
| Library text search (`/api/v1/library?q=`) | 1.25s |
| Global search (`/api/v1/search?q=`) | 1.5s |

Search is still an unindexed `LIKE '%q%'` scan, so a budget failure there is a real regression, not CI noise. Run with `PYTHONPATH=. pytest -o addopts= -m slow tests/test_large_library_perf.py`.
