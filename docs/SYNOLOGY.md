# Synology (DSM 7.2+)

bookdoe runs on any Synology that supports Container Manager (the Docker package).
Support is best-effort; this is a hobby project.

1. Install **Container Manager** from Package Center and enable SSH (or use File Station).
2. Create shared folders or pick existing ones, for example `/volume1/docker/bookdoe`
   (config), `/volume1/books` and `/volume1/audiobooks`.
3. Copy the repository to `/volume1/docker/bookdoe-src`, then in `.env` set
   `BOOKDOE_CONFIG_DIR`, `OPENREADS_DATA_DIR`, `BOOKS_DIR`, `AUDIOBOOKS_DIR`, and
   `PUID`/`PGID` to the ids of a DSM user (`id youruser` over SSH) that can read the media.
   `sh install.sh` does this interactively.
4. In Container Manager choose Project, Create, point it at the folder holding
   `docker-compose.yml`, and start it. Or over SSH: `sudo docker compose up -d --build`.
5. Open `http://<nas>:8795/setup` and finish the wizard.

DSM already uses ports 5000 and 5001; the default 8795 does not clash.

## HTTPS

Use **Control Panel, Login Portal, Advanced, Reverse Proxy**: source HTTPS, your
hostname, port 443; destination HTTP, `localhost`, port 8795. Add a certificate under
Security, Certificate (Let's Encrypt works). Then set `BOOKDOE_PUBLIC_URL` to the
HTTPS URL. See the HTTPS section of [INSTALL.md](INSTALL.md) for why the iOS app needs it.
