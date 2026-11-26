# Public demo deployment

Reproducible setup for the isolated demo instance. It holds only public-domain or openly
licensed media (Project Gutenberg, LibriVox); never mount a real library into it.

1. Create `demo.env` next to the compose file with `BOOKDOE_BOOTSTRAP_USER`,
   `BOOKDOE_BOOTSTRAP_PASSWORD` and `BOOKDOE_SECRET` (throwaway values; do not commit it).
2. Create `DEMO_DATA` (default `./data`) with `seed/<name>.db` (exactly one database file; it is copied to `bookdoe.db` at start; the script refuses to start otherwise), optionally `seed/covers/`,
   `media/books`, `media/audiobooks` and `podcasts`.
3. `BOOKDOE_IMAGE=bookdoe:<tag> DEMO_PUBLIC_URL=https://your-demo-host docker compose up -d`.

`demo-entry.sh` wipes `/config` and reloads it from `/seed` at every start, and exits at the
next 11:00 UTC so Docker restarts it: the demo resets daily. Set `SEED_MODE=1` to skip the
reset while preparing a new seed (then copy the database file and `covers/` from `config/` into `seed/`).
