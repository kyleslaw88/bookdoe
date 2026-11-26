from app import db


def test_defaults_are_created():
    assert db.one("SELECT COUNT(*) n FROM profiles")["n"] == 2
    assert db.one("SELECT COUNT(*) n FROM naming_templates")["n"] == 2
    assert db.one("SELECT COUNT(*) n FROM jobs")["n"] >= 5
    assert db.one("SELECT config_json FROM profiles WHERE id='audio-default'")
