"""auth_mode='none' answers every request with a fixed synthetic identity that has
no real row in `users` unless something creates one. Ratings and read-states carry
a NOT NULL foreign key to users(id), so the very first write in this mode failed
outright with an integrity error - found via a test that used the ordinary
TestClient(app) pattern with no session at all, which is exactly what 'none' mode is.
"""
from app import auth, db


def test_ensure_anonymous_user_creates_a_real_row():
    db.execute("DELETE FROM users WHERE id='anonymous'")
    assert db.one("SELECT id FROM users WHERE id='anonymous'") is None
    auth.ensure_anonymous_user()
    row = db.one("SELECT * FROM users WHERE id='anonymous'")
    assert row is not None
    assert row["access_scope"] == "full" and bool(row["is_admin"]) is True


def test_ensure_anonymous_user_is_idempotent():
    auth.ensure_anonymous_user()
    auth.ensure_anonymous_user()
    assert db.one("SELECT COUNT(*) n FROM users WHERE id='anonymous'")["n"] == 1


def test_current_user_none_mode_id_matches_a_real_row(monkeypatch):
    import dataclasses
    from app.config import settings

    replaced = dataclasses.replace(settings, auth_mode="none")
    monkeypatch.setattr(auth, "settings", replaced)
    auth.ensure_anonymous_user()
    user = auth.current_user(request=None)
    assert db.one("SELECT id FROM users WHERE id=?", (user["id"],)) is not None
