# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use the repository's
private vulnerability reporting (GitHub: Security tab, "Report a vulnerability") or
contact the maintainer directly through the address on the project page. Include the
version (`/api/player/v1/capabilities` reports it), the steps to reproduce and the
impact you see.

You can expect an acknowledgement within a week. Fixes for confirmed issues are
released as a new version with a note in the release description; credit is given
unless you prefer otherwise.

## Supported versions

Only the latest release on `main` receives security fixes.

## Scope and expectations

bookdoe is designed for a private network behind a reverse proxy. What is in
scope: authentication and session handling, device and API key tokens, the kids
content boundary, path safety for library roots and imports, secret handling and
logging, the OPDS and Player APIs, the iOS app's credential storage and transport.

Things that are documented limitations rather than vulnerabilities:

- `BOOKDOE_AUTH_MODE=none` disables authentication and is for testing only.
- `proxy` mode trusts the identity header from the configured proxies; the
  `--forwarded-allow-ips` caveat is described in `docs/SECURITY.md`.
- The iOS app allows plain HTTP so it can reach LAN servers; use HTTPS when the
  server is reachable from the Internet.

## Hardening checklist for operators

- Generate `BOOKDOE_SECRET_KEY` (the installer does) and keep `bookdoe/.env`
  and the config volume private; both hold credentials.
- Put the service behind HTTPS and do not publish OpenReads.
- Prefer OIDC with `BOOKDOE_OIDC_ALLOWED_GROUPS` set, or local accounts with long
  passwords (12 characters minimum is enforced).
- Rotate device tokens and API keys from Management when a device is lost.
- Keep backups off the application volume (`docs/OPERATIONS.md`).

The threat model and the controls in place are described in `docs/SECURITY.md`.
