# Secrets and configuration sources

bookdoe never ships a secret in its image, its Compose file or its defaults. Every
credential is read at startup through one provider layer
(`bookdoe/app/secrets_provider.py`) with a fixed precedence:

1. **Process environment.** Whatever Docker, systemd or your shell exported.
2. **Env file.** `BOOKDOE_ENV_FILE` (default `<data dir>/setup.env`, which the
   setup wizard writes inside the `/config` volume). A variable already present in
   the environment is never overridden by the file.
3. **Secrets backend** (optional). `BOOKDOE_SECRETS_BACKEND=infisical` makes
   bookdoe ask Infisical for any credential the two sources above do not hold.

The settings that go through this layer are `BOOKDOE_SECRET_KEY`,
`BOOKDOE_BOOTSTRAP_PASSWORD`, `BOOKDOE_OIDC_CLIENT_SECRET`, `OPENREADS_API_KEY`,
`BOOKDOE_GOOGLE_BOOKS_API_KEY`, `BOOKDOE_HARDCOVER_API_KEY`,
`BOOKDOE_AUDIOBOOKDB_API_KEY` and `BOOKDOE_LLM_API_KEY`. Non-secret settings
(ports, paths, auth mode) are plain environment variables and also benefit from the
env file.

## References

Any of those variables may hold a reference instead of a literal value:

| Form | Meaning |
| --- | --- |
| `infisical://NAME` | the secret `NAME` at `INFISICAL_SECRET_PATH` |
| `infisical:///folder/sub/NAME` | the secret `NAME` in that folder |
| `file:///run/secrets/name` | the contents of a file (Docker or Kubernetes secrets); trailing whitespace is stripped |
| `env://OTHER` | the value of another variable (which may itself be a reference) |

Example, Docker secrets without Infisical:

```env
BOOKDOE_SECRET_KEY=file:///run/secrets/bookdoe_secret_key
```

## Infisical

Create a machine identity with universal auth and give it read access to the project
and environment that hold the secrets. Then set:

```env
BOOKDOE_SECRETS_BACKEND=infisical
INFISICAL_SITE_URL=https://app.infisical.com     # or your self-hosted URL
INFISICAL_CLIENT_ID=<machine identity client id>
INFISICAL_CLIENT_SECRET=<machine identity client secret>
INFISICAL_PROJECT_ID=<project id>
INFISICAL_ENVIRONMENT=prod                       # default prod
INFISICAL_SECRET_PATH=/                          # default /
INFISICAL_TIMEOUT=10                             # seconds, default 10
```

The two Infisical credentials are the only secrets that must still live in the
environment or the env file; they bootstrap everything else. Secrets with the same
name as the bookdoe variable (for example `BOOKDOE_SECRET_KEY`) are picked up
with no further configuration; anything else is wired with an `infisical://`
reference.

Behaviour:

- Login is `POST /api/v1/auth/universal-auth/login`; the access token is cached until
  shortly before `expiresIn` and renewed once on a 401.
- Secrets are read with `GET /api/v3/secrets/raw/{name}` and cached for 60 seconds.
- Values are never logged and never appear in error messages. A failure names the
  variable, the backend, the HTTP status and the first 8 characters of the client id.
- If Infisical is unreachable or the identity is rejected, startup fails with that
  message. bookdoe does not fall back to a weaker secret.

These API shapes follow Infisical's public documentation and are verified only
against a fake server in `bookdoe/tests/test_secrets_provider.py`. If your Infisical
version differs, the client is small and lives in one class (`InfisicalProvider`).

## Checking the configuration

- Setup wizard: the "Secrets" step runs a connectivity check and shows backend,
  site, project, environment and whether login works (no values).
- CLI: `python -m app.cli secrets-check` from `bookdoe/` (or
  `docker compose exec bookdoe python -m app.cli secrets-check`).
- Health: `/readyz` does not contact the backend; a misconfiguration is a startup
  failure, not a runtime degradation.

## OpenReads

OpenReads reads its few settings (`GOOGLE_BOOKS_API_KEY`, `OPENREADS_V2_API_KEY`,
LLM settings) straight from its environment or `openreads/.env`. The setup wizard
writes that file for you; `file://` and `infisical://` references are not expanded by
OpenReads itself.
