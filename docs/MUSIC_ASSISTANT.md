# Music Assistant provider

A provider for [Music Assistant](https://music-assistant.io) (MA) that plays your bookdoe
audiobooks and podcasts. Home Assistant gets them through MA's own integration, so the same
items can be played from a HA dashboard, by voice (HA Assist: "play *Dune* in the kitchen"),
and sent to any MA speaker or speaker group.

Code: `bookdoe/integrations/music_assistant/bookdoe/`. `client.py` holds all the bookdoe logic
(no MA imports) and is tested against the real Player API in
`bookdoe/tests/test_music_assistant_provider.py`. `provider.py` is the thin MA-facing shim.

## What it does

- **Library**: audiobooks (`/library?kind=audiobook`) and podcast shows with their episodes.
- **Playback**: a one-file book (M4B) and podcast episodes stream straight from bookdoe with the
  token as a `Bearer` header, so ffmpeg can seek. A book split over several files is streamed
  track after track; a seek lands at the head of the track that holds it.
- **Resume**: starting a book or episode opens at the account's saved position, whatever device
  saved it.
- **Progress**: MA's played-position reports are written back (`PUT /progress/{asset}`,
  `POST /podcasts/episodes/{id}/progress`), at most one per 5 seconds and always at stop, so the
  book shows in bookdoe's Continue listening. A book's position is mapped onto its track
  (`file_id` + seconds into it).
- **Kids**: the provider uses a device token, which carries exactly its owner's account type.
  A kid's token lists only the kid's approved shelf; anything else answers 404.

## Install

1. In bookdoe (signed in as the person whose shelf and progress this is), pair a device named
   "Music Assistant" and keep its `alxd_...` token (Player API, "Device onboarding"). Use a
   separate account/token per person; revoking the device in Settings cuts the provider off.
2. Mount the provider folder into the MA container as a custom provider. Example compose volume
   (path of the MA providers directory depends on the image, check `music_assistant/providers`
   inside it):

   ```yaml
   volumes:
     - /path/to/bookdoe/bookdoe/integrations/music_assistant/bookdoe:/app/venv/lib/python3.12/site-packages/music_assistant/providers/bookdoe:ro
   ```

3. Restart Music Assistant (this is a family-facing service: do it at a quiet time), then
   Settings > Providers > Add > bookdoe, enter the server URL and the token.
4. In Home Assistant, the MA integration exposes the speakers; play the book or episode from
   the MA card on a dashboard or by voice through Assist.

## Install on Home Assistant OS (Music Assistant add-on)

When MA runs as the add-on (`d5369777_music_assistant`) there is no compose file to mount a
volume into. Copy the provider into the add-on container instead. This needs a shell on the HA
OS host with Docker access: the "Advanced SSH & Web Terminal" add-on (`a0d7b954_ssh`) with
protection mode off, key-only login. The official Terminal & SSH add-on has no `docker`.

```sh
# on the HA OS host, from the Advanced SSH add-on
docker cp bookdoe addon_d5369777_music_assistant:/app/venv/lib/python3.12/site-packages/music_assistant/providers/bookdoe
ha apps restart d5369777_music_assistant
```

`bookdoe` is the provider folder (`bookdoe/integrations/music_assistant/bookdoe`) copied to the
host first (for example into `/share`). Check the providers path inside the container first
(`docker exec addon_d5369777_music_assistant ls /app/venv/lib/python3.12/site-packages/music_assistant/providers`).
Then add the provider as in step 3 above.

**The copy is lost whenever the MA add-on is updated or rebuilt.** After every MA add-on update,
run the same two commands again (and re-check the providers path). Restarting MA is
family-facing: do it at a quiet time, and do not restart Home Assistant core for this.

## Caveats

- Custom providers are not supported by MA upstream: an MA upgrade can change the provider
  interface and break this until it is updated (or accepted upstream). Pin the MA image tag.
- `provider.py` needs a live MA to run; it was written against the MA 2.x provider interface and
  is not covered by the automated tests. Verify on your install: add the provider, play a book
  and an episode to a speaker, then check bookdoe's Continue listening.
- A seek inside a multi-file book restarts the track that holds it from its start.
- The token is sent to your own bookdoe server only; do not put the server on plain http across
  an untrusted network.
