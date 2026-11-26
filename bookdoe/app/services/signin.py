"""Sign-in persistence settings: "Remember me" and "Trust this device".

Each value is its own row in the settings table under the key the administrator sees
(`auth.remember_days`, ...), so a bad value in one cannot hide the others. Reads are
tolerant (a malformed stored value falls back to the default); writes validate.
"""
from __future__ import annotations

from typing import Any

from .. import db

PLACED_TYPES = ("admin", "adult", "kid")

DEFAULTS: dict[str, Any] = {
    "auth.remember_days": 90,                       # 0 turns "remember me" off
    "auth.remember_allowed_types": list(PLACED_TYPES),
    "auth.remember_absolute_days": 180,             # a remembered session never outlives this, however often it renews
    "auth.trust_device_allowed_types": list(PLACED_TYPES),
    "auth.trusted_devices_max": 10,                 # per account
    "auth.trusted_device_idle_days": 0,             # 0 = a trusted device never expires from disuse
}
BOUNDS = {
    "auth.remember_days": (0, 365),
    "auth.remember_absolute_days": (1, 730),
    "auth.trusted_devices_max": (1, 50),
    "auth.trusted_device_idle_days": (0, 3650),
}
TYPE_LISTS = ("auth.remember_allowed_types", "auth.trust_device_allowed_types")


def _int(key: str) -> int:
    lo, hi = BOUNDS[key]
    value = db.get_setting(key, DEFAULTS[key])
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return DEFAULTS[key]


def _types(key: str) -> list[str]:
    value = db.get_setting(key, DEFAULTS[key])
    if not isinstance(value, list):
        return list(DEFAULTS[key])
    return [t for t in PLACED_TYPES if t in value]


def remember_days() -> int:
    return _int("auth.remember_days")


def remember_absolute_days() -> int:
    return _int("auth.remember_absolute_days")


def remember_allowed(account_type: str) -> bool:
    return remember_days() > 0 and account_type in _types("auth.remember_allowed_types")


def trust_allowed(account_type: str) -> bool:
    return account_type in _types("auth.trust_device_allowed_types")


def trusted_devices_max() -> int:
    return _int("auth.trusted_devices_max")


def trusted_device_idle_days() -> int:
    return _int("auth.trusted_device_idle_days")


def get_all() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in DEFAULTS:
        out[key] = _types(key) if key in TYPE_LISTS else _int(key)
    return out


def update(values: dict[str, Any]) -> dict[str, Any]:
    """Validate and store the given keys; raises ValueError naming the bad one."""
    clean: dict[str, Any] = {}
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError(f"Unknown setting {key}")
        if key in TYPE_LISTS:
            if not isinstance(value, list) or any(t not in PLACED_TYPES for t in value):
                raise ValueError(f"{key} must be a list drawn from {list(PLACED_TYPES)}")
            clean[key] = [t for t in PLACED_TYPES if t in value]
        else:
            lo, hi = BOUNDS[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{key} must be a whole number from {lo} to {hi}")
            clean[key] = value
    for key, value in clean.items():
        db.set_setting(key, value)
    return get_all()
