"""`bookdoe` command line: setup without a browser, secret bootstrap, checks.

    bookdoe setup [--admin-user U] [--admin-password-env VAR] [--public-url URL]
                     [--root NAME:PATH:KIND[:MODE]]... [--openreads-url URL]
                     [--secrets-backend env|infisical] [--non-interactive]
    bookdoe ensure-secret          # generate BOOKDOE_SECRET_KEY into the env file
    bookdoe secrets-check          # connectivity of the configured secrets backend
    bookdoe status                 # setup state, counts, env file

Run inside the container (`docker compose exec bookdoe bookdoe setup`) or from
`bookdoe/` with `PYTHONPATH=. python -m app.cli ...`. Passwords are read from an
environment variable or prompted, never taken on the command line.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any

from . import db, secrets_provider, setup
from .config import settings


def _root_arg(value: str) -> setup.RootSpec:
    parts = value.split(":")
    if len(parts) < 3:
        raise argparse.ArgumentTypeError("--root expects NAME:PATH:KIND[:MODE]")
    name, path, kind = parts[0], parts[1], parts[2]
    mode = parts[3] if len(parts) > 3 else "adopt"
    try:
        return setup.RootSpec(name=name, path=path, media_kind=kind, management_mode=mode)  # type: ignore[arg-type]
    except Exception as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _prompt(label: str, default: str | None = None, *, secret: bool = False, interactive: bool = True) -> str:
    if not interactive:
        return default or ""
    shown = f"{label} [{default}]: " if default else f"{label}: "
    value = getpass.getpass(shown) if secret else input(shown)
    return value.strip() or (default or "")


def cmd_ensure_secret(args) -> int:
    generated = setup.ensure_secret_key()
    print(f"{setup.SECRET_KEY_VAR}: {'generated into ' + str(setup.env_file_path()) if generated else 'already configured'}")
    return 0


def cmd_secrets_check(args) -> int:
    result = secrets_provider.reset().check()
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def cmd_status(args) -> int:
    db.init_db()
    print(json.dumps(setup.status(), indent=2, default=str))
    return 0


def cmd_setup(args) -> int:
    db.init_db()
    interactive = not args.non_interactive and sys.stdin.isatty()
    if settings.auth_mode != "local":
        print(f"setup: auth mode is {settings.auth_mode!r}; the administrator comes from the identity provider. Only roots and variables are applied.", file=sys.stderr)
    users = db.one("SELECT COUNT(*) n FROM users")["n"]

    password = os.environ.get(args.admin_password_env, "") if args.admin_password_env else ""
    if users == 0 and settings.auth_mode == "local":
        username = args.admin_user or _prompt("Administrator username", "admin", interactive=interactive)
        while not password or len(password) < 12:
            if not interactive:
                print(f"setup: an administrator password of at least 12 characters is required; pass --admin-password-env VAR", file=sys.stderr)
                return 2
            password = _prompt("Administrator password (12+ characters)", secret=True)
    else:
        username = args.admin_user or "admin"
        password = password or "x" * 12  # not used: an administrator already exists

    roots = list(args.root or [])
    if interactive and not roots:
        print("Library roots (paths inside the container; leave the name empty to stop).")
        while True:
            name = _prompt("Root name", "")
            if not name:
                break
            path = _prompt("Path", "/books")
            kind = _prompt("Kind (ebook/audiobook/mixed)", "ebook")
            mode = _prompt("Mode (adopt/observe/managed)", "adopt")
            try:
                roots.append(setup.RootSpec(name=name, path=path, media_kind=kind, management_mode=mode))  # type: ignore[arg-type]
            except Exception as exc:
                print(f"  rejected: {exc}")

    public_url = args.public_url or (_prompt("Public URL", settings.public_url) if interactive else None)
    openreads_url = args.openreads_url
    backend = args.secrets_backend or (_prompt("Secrets backend (env/infisical)", "env") if interactive else "env")
    infisical = None
    if backend == "infisical":
        env = os.environ
        infisical = setup.InfisicalSpec(
            site_url=env.get("INFISICAL_SITE_URL") or (_prompt("Infisical site URL", "https://app.infisical.com") if interactive else "https://app.infisical.com"),
            project_id=env.get("INFISICAL_PROJECT_ID") or _prompt("Infisical project id", interactive=interactive),
            client_id=env.get("INFISICAL_CLIENT_ID") or _prompt("Infisical client id", interactive=interactive),
            client_secret=env.get("INFISICAL_CLIENT_SECRET") or _prompt("Infisical client secret", secret=True, interactive=interactive),
            environment=env.get("INFISICAL_ENVIRONMENT") or (_prompt("Infisical environment", "prod") if interactive else "prod"),
            secret_path=env.get("INFISICAL_SECRET_PATH") or (_prompt("Infisical secret path", "/") if interactive else "/"),
        )

    req = setup.SetupRequest(
        admin=setup.AdminSpec(username=username, password=password, email=args.admin_email),
        public_url=public_url or None, roots=roots,
        metadata=setup.MetadataSpec(openreads_url=openreads_url),
        secrets=setup.SecretsSpec(backend=backend, infisical=infisical),  # type: ignore[arg-type]
    )
    checks = setup.run_checks(req)
    for p in checks["paths"]:
        print(f"  {'ok  ' if p['ok'] else 'FAIL'} root {p['name']} {p['path']}{': ' + p['error'] if p.get('error') else ''}")
    o = checks["openreads"]
    print(f"  {'ok  ' if o['ok'] else 'warn'} openreads {o['url']}{': ' + o['error'] if o.get('error') else ''}")
    s = checks["secrets"]
    print(f"  {'ok  ' if s.get('ok') else 'FAIL'} secrets backend {s.get('backend')}{': ' + s['error'] if s.get('error') else ''}")
    if not checks["ok"] and not args.force:
        print("setup: checks failed; fix them or re-run with --force", file=sys.stderr)
        return 1
    result = setup.apply(req)
    print(json.dumps({k: v for k, v in result.items() if k != "admin_user_id"} | {"admin_created": bool(result["admin_user_id"])}, indent=2))
    if result["restart_required"]:
        print("setup: restart the container to apply the new variables (docker compose restart bookdoe)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bookdoe", description="bookdoe setup and maintenance")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="first-run setup (interactive or with flags)")
    s.add_argument("--admin-user")
    s.add_argument("--admin-email")
    s.add_argument("--admin-password-env", metavar="VAR", help="name of the environment variable holding the password")
    s.add_argument("--public-url")
    s.add_argument("--openreads-url")
    s.add_argument("--root", action="append", type=_root_arg, metavar="NAME:PATH:KIND[:MODE]")
    s.add_argument("--secrets-backend", choices=secrets_provider.BACKENDS)
    s.add_argument("--non-interactive", action="store_true")
    s.add_argument("--force", action="store_true", help="apply even when a check fails")
    s.set_defaults(func=cmd_setup)
    e = sub.add_parser("ensure-secret", help="generate BOOKDOE_SECRET_KEY into the env file if missing")
    e.set_defaults(func=cmd_ensure_secret)
    c = sub.add_parser("secrets-check", help="check the configured secrets backend")
    c.set_defaults(func=cmd_secrets_check)
    st = sub.add_parser("status", help="setup state")
    st.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except secrets_provider.SecretError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
