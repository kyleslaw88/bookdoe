"""The web device id must come from the platform CSPRNG, never Math.random (CWE-327, issue #674)."""
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def test_device_id_does_not_use_math_random():
    lines = [l for l in (STATIC / "consume.js").read_text().splitlines() if "alx-device-id" in l and "setItem" in l]
    assert lines
    for line in lines:
        assert "Math.random" not in line
        assert "crypto.getRandomValues" in line or "crypto.randomUUID" in line
