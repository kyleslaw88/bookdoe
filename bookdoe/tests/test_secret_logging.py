"""Regression tests for credential leakage into logs and for slow torrent adds.

Both found during the 2026-09-24 Unraid deployment at Gate 7:

CC-008  A live SABnzbd API key appeared in plaintext across 11 container log lines.
        bookdoe sets the root logger to INFO; httpx logs every request URL at
        INFO, and SABnzbd's API takes its key as a query parameter.
CC-009  torrent-add timed out after 15s. Transmission fetches the .torrent itself,
        which for a private tracker behind a VPN routinely exceeds that.
"""
import logging

from app.integrations import downloaders


def test_httpx_request_logging_is_suppressed():
    """httpx must not log request URLs - they carry apikey= query parameters."""
    import app.main  # noqa: F401  (import applies the logging configuration)
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level >= logging.WARNING, (
            f"{name} logger must be at WARNING or higher or request URLs "
            "containing credentials are written to logs"
        )


def test_bookdoe_own_logging_is_not_suppressed():
    """Only the noisy HTTP libraries are demoted - not the application's own logger.

    (Asserting the *explicit* level, not the effective one: pytest installs its own
    root handler at WARNING, which would make an effective-level check meaningless.)
    """
    import app.main  # noqa: F401
    assert logging.getLogger("bookdoe").level in (logging.NOTSET, logging.DEBUG, logging.INFO)


def test_torrent_add_gets_a_generous_timeout():
    """The client fetches the .torrent itself; a status-call timeout is too short."""
    assert downloaders._TRANSMISSION_TIMEOUTS["torrent-add"] >= 60


def test_status_calls_keep_a_short_timeout():
    assert downloaders._TRANSMISSION_DEFAULT_TIMEOUT <= 30
    assert "session-get" not in downloaders._TRANSMISSION_TIMEOUTS
    assert "torrent-get" not in downloaders._TRANSMISSION_TIMEOUTS
