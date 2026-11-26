from __future__ import annotations

import logging
import smtplib
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .. import db
from ..config import settings
from ..crypto import decrypt_secret, encrypt_secret
from . import kepub as kepub_service
from . import paths as path_guard

log = logging.getLogger(__name__)

# Kindle's Send-to-Kindle mail pipeline supports EPUB directly today, plus the
# original personal-document formats it has always accepted.
KINDLE_SUPPORTED_EXTS = {".epub", ".pdf", ".doc", ".docx", ".txt", ".mobi", ".azw3", ".azw"}
# Which file to send when a work has several: EPUB first (Kindle's native path now),
# then Amazon's own formats, then documents.
KINDLE_PREFERENCE = [".epub", ".azw3", ".mobi", ".azw", ".pdf", ".docx", ".doc", ".txt"]

SETTINGS_KEY = "device.smtp"
TLS_MODES = ("starttls", "ssl", "none")   # STARTTLS (587), implicit TLS (465), plain
SMTP_TIMEOUT = 30.0
# Mail goes through its own small pool so a hung relay can neither pin a request
# worker past the timeout nor be reached by anything but these calls. Created on first
# use and stoppable (app shutdown, test teardown).
_mail_pool: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    global _mail_pool
    if _mail_pool is None:
        _mail_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="alx-mail")
    return _mail_pool


def shutdown_mail_pool(wait: bool = True) -> None:
    global _mail_pool
    pool, _mail_pool = _mail_pool, None
    if pool is not None:
        pool.shutdown(wait=wait)


class MailError(Exception):
    """A delivery failure explained in words a person can act on (no credentials,
    no message body). `kind` is a stable tag for tests and logs."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


def tls_mode(cfg: dict[str, Any]) -> str:
    """Stored `tls_mode`, or the legacy boolean `use_tls` (True -> starttls)."""
    mode = cfg.get("tls_mode")
    if mode in TLS_MODES:
        return mode
    return "starttls" if cfg.get("use_tls", True) else "none"


def resolve_library_file(raw_path: str) -> Path | None:
    """The resolved path of a library file, or None if it is not inside an enabled root
    (``..`` or a symlink escaping it). Callers must read/serve the RETURNED path."""
    for root in db.all("SELECT path FROM root_folders WHERE enabled=1"):
        resolved = path_guard.resolve_safe(Path(root["path"]), Path(raw_path))
        if resolved is not None:
            return resolved
    return None


def get_smtp_config() -> dict[str, Any]:
    cfg = db.get_setting(SETTINGS_KEY, {}) or {}
    return {
        "host": cfg.get("host", ""),
        "port": cfg.get("port", 587),
        "username": cfg.get("username", ""),
        "from_email": cfg.get("from_email", ""),
        "use_tls": tls_mode(cfg) == "starttls",
        "tls_mode": tls_mode(cfg),
        "configured": bool(cfg.get("host") and cfg.get("from_email")),
        # password is deliberately never returned - see set_smtp_config().
    }


def set_smtp_config(*, host: str, port: int, username: str, password: str | None, from_email: str, use_tls: bool = True,
                    tls_mode: str | None = None) -> None:
    existing = db.get_setting(SETTINGS_KEY, {}) or {}
    # An empty password in the request means "keep the one already stored" - the
    # settings form never round-trips the real secret back to the browser to redisplay.
    password_enc = encrypt_secret(password) if password else existing.get("password_enc")
    mode = tls_mode if tls_mode in TLS_MODES else ("starttls" if use_tls else "none")
    db.set_setting(SETTINGS_KEY, {
        "host": host, "port": port, "username": username, "from_email": from_email,
        "use_tls": mode == "starttls", "tls_mode": mode, "password_enc": password_enc,
    })


def _smtp_password() -> str | None:
    cfg = db.get_setting(SETTINGS_KEY, {}) or {}
    return decrypt_secret(cfg.get("password_enc"))


def sendable_assets(work_id: str) -> list[dict[str, Any]]:
    """Ebook assets of the work with a present, Kindle-accepted file, best first."""
    rows = db.all(
        """SELECT a.id asset_id, a.format, f.path FROM media_assets a JOIN media_files f ON f.asset_id=a.id
           WHERE a.work_id=? AND a.kind='ebook' AND a.status='owned' AND f.missing=0 ORDER BY f.part_number, f.path""",
        (work_id,),
    )
    out, seen = [], set()
    for r in rows:
        ext = Path(r["path"]).suffix.lower()
        if ext not in KINDLE_SUPPORTED_EXTS or r["asset_id"] in seen:
            continue
        seen.add(r["asset_id"])
        out.append({"asset_id": r["asset_id"], "format": ext.lstrip("."), "filename": Path(r["path"]).name})
    out.sort(key=lambda x: KINDLE_PREFERENCE.index("." + x["format"]) if "." + x["format"] in KINDLE_PREFERENCE else 99)
    return out


def best_sendable(work_id: str) -> dict[str, Any] | None:
    items = sendable_assets(work_id)
    return items[0] if items else None


def _reply(exc: Exception) -> str:
    """The server's own words from an smtplib error, decoded and trimmed."""
    code = getattr(exc, "smtp_code", None)
    text = getattr(exc, "smtp_error", b"")
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    text = " ".join(str(text or "").split())[:200]
    return f"{code} {text}".strip() if code else text


def _deliver_now(cfg: dict[str, Any], msg: EmailMessage) -> None:
    host, port, mode = cfg["host"], int(cfg.get("port") or 587), tls_mode(cfg)
    where = f"{host}:{port}"
    try:
        if mode == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
    except ssl.SSLError as exc:
        raise MailError("tls", f"TLS handshake with {where} failed ({exc.reason or exc}); port 465 expects implicit TLS, 587 expects STARTTLS - check the TLS mode in Settings -> Devices") from exc
    except socket.gaierror as exc:
        raise MailError("unreachable", f"Could not reach the mail server {where}: hostname not found") from exc
    except (ConnectionRefusedError, socket.timeout, TimeoutError, OSError) as exc:
        reason = "connection refused" if isinstance(exc, ConnectionRefusedError) else "timed out" if isinstance(exc, (socket.timeout, TimeoutError)) else (exc.strerror or str(exc) or "network error")
        raise MailError("unreachable", f"Could not reach the mail server {where}: {reason}") from exc
    except smtplib.SMTPException as exc:
        raise MailError("protocol", f"The mail server {where} did not greet properly: {_reply(exc) or exc}") from exc
    try:
        with server:
            if mode == "starttls":
                try:
                    server.starttls()
                except smtplib.SMTPNotSupportedError as exc:
                    raise MailError("starttls_unsupported", f"The mail server {where} does not support TLS (STARTTLS); set TLS to 'None' (or 'Implicit TLS' with port 465) in Settings -> Devices") from exc
                except ssl.SSLError as exc:
                    raise MailError("tls", f"TLS handshake with {where} failed ({exc.reason or exc}); if the relay listens on 465 choose 'Implicit TLS', if it has no TLS choose 'None'") from exc
                except smtplib.SMTPResponseException as exc:
                    raise MailError("tls", f"The mail server {where} refused STARTTLS: {_reply(exc)}") from exc
            password = decrypt_secret(cfg.get("password_enc"))
            if cfg.get("username") and password:
                try:
                    server.login(cfg["username"], password)
                except smtplib.SMTPAuthenticationError as exc:
                    raise MailError("auth", f"The mail server {where} rejected the username or password: {_reply(exc)}") from exc
                except smtplib.SMTPNotSupportedError as exc:
                    raise MailError("auth", f"The mail server {where} does not offer login; clear the username and password if the relay needs none") from exc
            try:
                server.send_message(msg)
            except smtplib.SMTPRecipientsRefused as exc:
                detail = "; ".join(f"{addr}: {code} {(err.decode(errors='replace') if isinstance(err, bytes) else err)}" for addr, (code, err) in exc.recipients.items())
                raise MailError("recipient_refused", f"The mail server {where} rejected the recipient ({detail})") from exc
            except smtplib.SMTPSenderRefused as exc:
                raise MailError("sender_refused", f"The mail server {where} rejected the sender {exc.sender}: {_reply(exc)}") from exc
            except smtplib.SMTPDataError as exc:
                raise MailError("rejected", f"The mail server {where} rejected the message: {_reply(exc)}") from exc
    except MailError:
        raise
    except (smtplib.SMTPServerDisconnected, ConnectionError, socket.timeout, TimeoutError) as exc:
        raise MailError("disconnected", f"The mail server {where} dropped the connection ({exc or 'timed out'})") from exc
    except smtplib.SMTPException as exc:
        raise MailError("protocol", f"The mail server {where} answered with an error: {_reply(exc) or exc}") from exc
    except ssl.SSLError as exc:
        raise MailError("tls", f"TLS error talking to {where}: {exc.reason or exc}") from exc


def deliver(cfg: dict[str, Any], msg: EmailMessage) -> None:
    """Hand the message to the relay off the calling thread, bounded by SMTP_TIMEOUT.
    Raises MailError with a plain explanation; the exception type and message (never
    credentials or the body) are logged server-side so the container log has the cause."""
    future = _pool().submit(_deliver_now, cfg, msg)
    try:
        future.result(timeout=SMTP_TIMEOUT + 5)
    except FutureTimeout:
        log.warning("mail delivery via %s:%s timed out after %ss", cfg.get("host"), cfg.get("port"), SMTP_TIMEOUT)
        raise MailError("timeout", f"Could not reach the mail server {cfg.get('host')}:{cfg.get('port') or 587}: timed out after {int(SMTP_TIMEOUT)} s")
    except MailError as exc:
        cause = exc.__cause__
        log.warning("mail delivery failed [%s]: %s (%s: %s)", exc.kind, exc.message, type(cause).__name__ if cause else "-", cause or "")
        raise
    except Exception as exc:
        log.warning("mail delivery failed [unexpected]: %s: %s", type(exc).__name__, exc)
        raise MailError("unexpected", f"Sending failed: {type(exc).__name__}: {exc}") from exc


def send_test_email(to_email: str, *, by_user: str | None = None) -> dict[str, Any]:
    """A tiny message to the administrator's own address, so the relay can be checked
    without a book. Returns {ok, message}; a failure is a result, not an exception."""
    cfg = db.get_setting(SETTINGS_KEY, {}) or {}
    if not cfg.get("host") or not cfg.get("from_email"):
        return {"ok": False, "kind": "not_configured", "message": "Save an SMTP host and a From address first"}
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["from_email"], to_email, f"{settings.app_name} test message"
    msg.set_content(f"This is a test message from {settings.app_name}'s Send-to-Kindle settings. If you can read it, the mail relay works.")
    try:
        deliver(cfg, msg)
    except MailError as exc:
        return {"ok": False, "kind": exc.kind, "message": exc.message}
    db.add_history("device.smtp_test", f"Test email sent to {to_email} ({by_user or 'admin'})", data={"to": to_email})
    return {"ok": True, "kind": "sent", "message": f"Sent a test message from {cfg['from_email']} to {to_email} via {cfg['host']}:{cfg.get('port') or 587} ({tls_mode(cfg)})"}


def send_to_kindle(work_id: str, asset_id: str, to_email: str, *, by_user_id: str | None = None) -> dict[str, Any]:
    cfg = db.get_setting(SETTINGS_KEY, {}) or {}
    if not cfg.get("host") or not cfg.get("from_email"):
        raise ValueError("Send-to-Kindle is not configured (Settings -> Devices)")
    if not to_email:
        raise ValueError("No Kindle email address on file for this account")

    asset = db.one("SELECT * FROM media_assets WHERE id=? AND work_id=?", (asset_id, work_id))
    if not asset:
        raise ValueError("Asset not found")
    if asset["kind"] != "ebook":
        raise ValueError("Only ebook files can be sent to Kindle")
    files = db.all("SELECT * FROM media_files WHERE asset_id=? AND missing=0 ORDER BY part_number,path", (asset_id,))
    if not files:
        raise ValueError("No file available for this book")
    path = Path(files[0]["path"])
    if path.suffix.lower() not in KINDLE_SUPPORTED_EXTS:
        raise ValueError(f"Kindle does not accept .{path.suffix.lstrip('.')} files")
    path = resolve_library_file(files[0]["path"])
    if path is None:
        raise ValueError("File is outside the library roots")
    if not path.exists():
        raise ValueError("File is missing on disk")

    work = db.one("SELECT title FROM works WHERE id=?", (work_id,))
    msg = EmailMessage()
    msg["From"] = cfg["from_email"]
    msg["To"] = to_email
    msg["Subject"] = f"Convert" if path.suffix.lower() not in (".epub", ".pdf") else ""
    msg.set_content(f"Sent from {settings.app_name}: {work['title'] if work else path.stem}")
    msg.add_attachment(
        path.read_bytes(), maintype="application", subtype="octet-stream", filename=path.name,
    )

    deliver(cfg, msg)

    # Audit: who sent which file of which work. The address is a delivery detail of the
    # person's own account, never the mail body; nothing else about the message is kept.
    db.add_history("device.sent", f"Sent {path.name} to Kindle ({to_email})", work_id=work_id, asset_id=asset_id,
                   data={"user_id": by_user_id, "to": to_email, "filename": path.name})
    return {"sent": True, "to": to_email, "filename": path.name}


def kepub_for_asset(work_id: str, asset_id: str) -> tuple[bytes, str]:
    """Return (kepub bytes, filename) for the ebook asset's epub file.

    Kobo devices read plain EPUB natively; this exists for the per-paragraph reading
    position/highlight support that only a .kepub.epub-suffixed file gets. See kepub.py.
    """
    asset = db.one("SELECT * FROM media_assets WHERE id=? AND work_id=?", (asset_id, work_id))
    if not asset:
        raise ValueError("Asset not found")
    if asset["kind"] != "ebook" or (asset.get("format") or "").lower() != "epub":
        raise ValueError("Kobo conversion is only available for epub files")
    files = db.all("SELECT * FROM media_files WHERE asset_id=? AND missing=0 ORDER BY part_number,path", (asset_id,))
    if not files:
        raise ValueError("No file available for this book")
    path = resolve_library_file(files[0]["path"])
    if path is None:
        raise ValueError("File is outside the library roots")
    if not path.exists():
        raise ValueError("File is missing on disk")
    converted = kepub_service.convert(path.read_bytes())
    filename = path.stem + ".kepub.epub"
    db.add_history("device.kepub", f"Converted {path.name} for Kobo", work_id=work_id, asset_id=asset_id)
    return converted, filename
