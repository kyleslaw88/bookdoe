"""Evidence schema and pure ``evidence_for()`` function (Part XIX).

Defines the evidence signal type used to explain why a detected file matches
(or does not match) a Work.  Each signal carries a *state* drawn from the
shared vocabulary so that ``search_candidates._explain`` and the scanner
read the same semantics.

Pure function — no DB access, no network, no side effects.
"""
from __future__ import annotations

import re
from typing import Any

from . import search_candidates

# ---------------------------------------------------------------------------
# Signal type
# ---------------------------------------------------------------------------
# {signal: str, state: str, detail: str, weight: float}

# ---------------------------------------------------------------------------
# Signal names (Part XIX)
# ---------------------------------------------------------------------------
SIGNAL_IDENTIFIER = "identifier"
SIGNAL_TITLE = "title"
SIGNAL_AUTHOR = "author"
SIGNAL_SERIES = "series"
SIGNAL_SERIES_POSITION = "series_position"
SIGNAL_YEAR = "year"
SIGNAL_NARRATOR = "narrator"
SIGNAL_DURATION = "duration"
SIGNAL_EMBEDDED_METADATA = "embedded_metadata"
SIGNAL_DIRECTORY_STRUCTURE = "directory_structure"
SIGNAL_PROVIDER_AGREEMENT = "provider_agreement"

ALL_SIGNALS = (
    SIGNAL_IDENTIFIER,
    SIGNAL_TITLE,
    SIGNAL_AUTHOR,
    SIGNAL_SERIES,
    SIGNAL_SERIES_POSITION,
    SIGNAL_YEAR,
    SIGNAL_NARRATOR,
    SIGNAL_DURATION,
    SIGNAL_EMBEDDED_METADATA,
    SIGNAL_DIRECTORY_STRUCTURE,
    SIGNAL_PROVIDER_AGREEMENT,
)

# ---------------------------------------------------------------------------
# State vocabulary (shared with search_candidates._explain)
# ---------------------------------------------------------------------------
# Imported from search_candidates so there is a single source of truth.
STATE_EXACT = search_candidates.STATE_EXACT
STATE_SIMILAR = search_candidates.STATE_SIMILAR
STATE_CONFLICT = search_candidates.STATE_CONFLICT
STATE_UNAVAILABLE = search_candidates.STATE_UNAVAILABLE

VALID_STATES = search_candidates.VALID_STATES

# ---------------------------------------------------------------------------
# Default weights per signal
# ---------------------------------------------------------------------------
_SIGNAL_WEIGHTS: dict[str, float] = {
    SIGNAL_IDENTIFIER: 1.0,
    SIGNAL_TITLE: 0.3,
    SIGNAL_AUTHOR: 0.2,
    SIGNAL_SERIES: 0.15,
    SIGNAL_SERIES_POSITION: 0.1,
    SIGNAL_YEAR: 0.1,
    SIGNAL_NARRATOR: 0.1,
    SIGNAL_DURATION: 0.05,
    SIGNAL_EMBEDDED_METADATA: 0.05,
    SIGNAL_DIRECTORY_STRUCTURE: 0.05,
    SIGNAL_PROVIDER_AGREEMENT: 0.05,
}


def _weight(signal: str) -> float:
    """Return the default weight for a signal name."""
    return _SIGNAL_WEIGHTS.get(signal, 0.05)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _norm(text: str | None) -> str:
    """Lowercase, strip, collapse whitespace."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().lower())


def _year_from_date(date_str: str | None) -> int | None:
    """Extract a 4-digit year from a date string (YYYY or YYYY-MM-DD)."""
    if not date_str:
        return None
    m = re.match(r"(19\d{2}|20\d{2})", str(date_str))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evidence_for(
    detected: dict[str, Any],
    work_row: dict[str, Any],
) -> list[dict[str, str | float]]:
    """Produce a deterministic list of evidence signals comparing *detected*
    (from ``inspect_file`` / P5-04) against a *work_row* (from the ``works``
    table, possibly joined with ``editions`` and ``identifiers``).

    Returns a list of ``{signal, state, detail, weight}`` dicts.  The list
    always contains at least the ``identifier`` signal.  Narrator and
    duration signals are always included; when one side lacks data they
    return ``unavailable``.

    Pure function: identical inputs always produce identical outputs.
    """
    signals: list[dict[str, str | float]] = []

    # --- identifier --------------------------------------------------------
    signals.append(_signal_identifier(detected, work_row))

    # --- title -------------------------------------------------------------
    signals.append(_signal_title(detected, work_row))

    # --- author ------------------------------------------------------------
    signals.append(_signal_author(detected, work_row))

    # --- series ------------------------------------------------------------
    signals.append(_signal_series(detected, work_row))

    # --- series position ---------------------------------------------------
    signals.append(_signal_series_position(detected, work_row))

    # --- year --------------------------------------------------------------
    signals.append(_signal_year(detected, work_row))

    # --- narrator (only when both sides have data) -------------------------
    signals.append(_signal_narrator(detected, work_row))

    # --- duration (only when both sides have data) -------------------------
    signals.append(_signal_duration(detected, work_row))

    # --- embedded metadata -------------------------------------------------
    signals.append(_signal_embedded_metadata(detected, work_row))

    # --- directory structure -----------------------------------------------
    signals.append(_signal_directory_structure(detected, work_row))

    # --- provider agreement ------------------------------------------------
    signals.append(_signal_provider_agreement(detected, work_row))

    return signals


# ---------------------------------------------------------------------------
# Per-signal helpers
# ---------------------------------------------------------------------------

def _signal_identifier(
    detected: dict[str, Any],
    work_row: dict[str, Any],
) -> dict[str, str | float]:
    """Compare detected identifiers against the work's identifiers.

    Returns ``exact`` when any detected identifier namespace matches a
    work identifier value.  Returns ``conflict`` when the detected file
    carries identifiers that share a namespace with the work but differ
    in value.  Returns ``unavailable`` when the detected file carries
    no identifiers at all.
    """
    det_ids = detected.get("identifiers") or {}
    work_ids = work_row.get("identifiers") or []

    det_ns_values = {ns: str(v).strip().lower() for ns, v in det_ids.items() if v}

    if not det_ns_values:
        return {
            "signal": SIGNAL_IDENTIFIER,
            "state": STATE_UNAVAILABLE,
            "detail": "no identifiers in detected file",
            "weight": _weight(SIGNAL_IDENTIFIER),
        }

    # Check each detected identifier against work identifiers
    for ns, val in det_ns_values.items():
        for wid in work_ids:
            if isinstance(wid, dict):
                w_ns = wid.get("namespace", "")
                w_val = str(wid.get("value", "")).strip().lower()
            else:
                continue
            if ns == w_ns and val == w_val:
                return {
                    "signal": SIGNAL_IDENTIFIER,
                    "state": STATE_EXACT,
                    "detail": f"exact {ns} match",
                    "weight": _weight(SIGNAL_IDENTIFIER),
                }

    # Check if any detected namespace overlaps with a work namespace
    det_ns = set(det_ns_values.keys())
    work_ns = {
        wid.get("namespace", "")
        for wid in work_ids
        if isinstance(wid, dict) and wid.get("namespace")
    }
    if det_ns & work_ns:
        return {
            "signal": SIGNAL_IDENTIFIER,
            "state": STATE_CONFLICT,
            "detail": "detected identifiers share namespace with work but differ in value",
            "weight": _weight(SIGNAL_IDENTIFIER),
        }

    return {
        "signal": SIGNAL_IDENTIFIER,
        "state": STATE_UNAVAILABLE,
        "detail": "detected identifiers not found in work",
        "weight": _weight(SIGNAL_IDENTIFIER),
    }


def _signal_title(
    detected: dict[str, Any],
    work_row: dict[str, Any],
) -> dict[str, str | float]:
    """Compare detected title against the work's title."""
    det_title = _norm(detected.get("title"))
    work_title = _norm(work_row.get("title"))

    if not det_title:
        return {
            "signal": SIGNAL_TITLE,
            "state": STATE_UNAVAILABLE,
            "detail": "no title in detected file",
            "weight": _weight(SIGNAL_TITLE),
        }
    if not work_title:
        return {
            "signal": SIGNAL_TITLE,
            "state": STATE_UNAVAILABLE,
            "detail": "no title in work",
            "weight": _weight(SIGNAL_TITLE),
        }

    if det_title == work_title:
        return {
            "signal": SIGNAL_TITLE,
            "state": STATE_EXACT,
            "detail": f"title matches: {det_title}",
            "weight": _weight(SIGNAL_TITLE),
        }

    # Check for similarity (substring or high overlap)
    if det_title in work_title or work_title in det_title:
        return {
            "signal": SIGNAL_TITLE,
            "state": STATE_SIMILAR,
            "detail": f"title similar: '{det_title}' vs '{work_title}'",
            "weight": _weight(SIGNAL_TITLE),
        }

    return {
        "signal": SIGNAL_TITLE,
        "state": STATE_CONFLICT,
        "detail": f"title conflict: '{det_title}' vs '{work_title}'",
        "weight": _weight(SIGNAL_TITLE),
    }


def _signal_author(
    detected: dict[str, Any],
    work_row: dict[str, Any],
) -> dict[str, str | float]:
    """Compare detected authors against the work's authors.

    The work_row may carry authors as a list of dicts with 'name' keys,
    or as a plain list of strings.
    """
    det_authors = [a.strip() for a in (detected.get("authors") or []) if a]
    work_authors = _work_authors(work_row)

    if not det_authors:
        return {
            "signal": SIGNAL_AUTHOR,
            "state": STATE_UNAVAILABLE,
            "detail": "no authors in detected file",
            "weight": _weight(SIGNAL_AUTHOR),
        }
    if not work_authors:
        return {
            "signal": SIGNAL_AUTHOR,
            "state": STATE_UNAVAILABLE,
            "detail": "no authors in work",
            "weight": _weight(SIGNAL_AUTHOR),
        }

    det_norm = {_norm(a) for a in det_authors}
    work_norm = {_norm(a) for a in work_authors}

    if det_norm == work_norm:
        return {
            "signal": SIGNAL_AUTHOR,
            "state": STATE_EXACT,
            "detail": f"authors match: {', '.join(det_authors)}",
            "weight": _weight(SIGNAL_AUTHOR),
        }

    # Check partial overlap
    overlap = det_norm & work_norm
    if overlap:
        return {
            "signal": SIGNAL_AUTHOR,
            "state": STATE_SIMILAR,
            "detail": f"authors partially match: {', '.join(sorted(overlap))}",
            "weight": _weight(SIGNAL_AUTHOR),
        }

    return {
        "signal": SIGNAL_AUTHOR,
        "state": STATE_CONFLICT,
        "detail": f"authors conflict: '{', '.join(det_authors)}' vs '{', '.join(work_authors)}'",
        "weight": _weight(SIGNAL_AUTHOR),
    }


def _signal_series(
    detected: dict[str, Any],
    work_row: dict[str, Any],
) -> dict[str, str | float]:
    """Compare detected series against the work's series."""
    det_series = detected.get("series") or []
    work_series = work_row.get("series") or []

    if not det_series:
        return {
            "signal": SIGNAL_SERIES,
            "state": STATE_UNAVAILABLE,
            "detail": "no series in detected file",
            "weight": _weight(SIGNAL_SERIES),
        }
    if not work_series:
        return {
            "signal": SIGNAL_SERIES,
            "state": STATE_UNAVAILABLE,
            "detail": "no series in work",
            "weight": _weight(SIGNAL_SERIES),
        }

    det_norm = {_norm(s) for s in det_series}
    work_norm = {_norm(s) for s in work_series}

    if det_norm == work_norm:
        return {
            "signal": SIGNAL_SERIES,
            "state": STATE_EXACT,
            "detail": f"series match: {', '.join(det_series)}",
            "weight": _weight(SIGNAL_SERIES),
        }

    overlap = det_norm & work_norm
    if overlap:
        return {
            "signal": SIGNAL_SERIES,
            "state": STATE_SIMILAR,
            "detail": f"series partially match: {', '.join(sorted(overlap))}",
            "weight": _weight(SIGNAL_SERIES),
        }

    return {
        "signal": SIGNAL_SERIES,
        "state": STATE_CONFLICT,
        "detail": f"series conflict: '{', '.join(det_series)}' vs '{', '.join(work_series)}'",
        "weight": _weight(SIGNAL_SERIES),
    }


def _signal_series_position(
    detected: dict[str, Any],
    work_row: dict[str, Any],
) -> dict[str, str | float]:
    """Compare detected series position against the work's series position."""
    det_pos = detected.get("series_position")
    work_pos = work_row.get("series_position")

    if det_pos is None and work_pos is None:
        return {
            "signal": SIGNAL_SERIES_POSITION,
            "state": STATE_UNAVAILABLE,
            "detail": "no series position on either side",
            "weight": _weight(SIGNAL_SERIES_POSITION),
        }
    if det_pos is None:
        return {
            "signal": SIGNAL_SERIES_POSITION,
            "state": STATE_UNAVAILABLE,
            "detail": "no series position in detected file",
            "weight": _weight(SIGNAL_SERIES_POSITION),
        }
    if work_pos is None:
        return {
            "signal": SIGNAL_SERIES_POSITION,
            "state": STATE_UNAVAILABLE,
            "detail": "no series position in work",
            "weight": _weight(SIGNAL_SERIES_POSITION),
        }

    if str(det_pos) == str(work_pos):
        return {
            "signal": SIGNAL_SERIES_POSITION,
            "state": STATE_EXACT,
            "detail": f"series position matches: {det_pos}",
            "weight": _weight(SIGNAL_SERIES_POSITION),
        }

    return {
        "signal": SIGNAL_SERIES_POSITION,
        "state": STATE_CONFLICT,
        "detail": f"series position conflict: {det_pos} vs {work_pos}",
        "weight": _weight(SIGNAL_SERIES_POSITION),
    }


def _signal_year(
    detected: dict[str, Any],
    work_row: dict[str, Any],
) -> dict[str, str | float]:
    """Compare detected year against the work's publication year.

    Year is extracted from ``publication_date`` on the detected dict and
    ``first_publish_date`` on the work row.  When both are present and
    differ by more than 1 year, the state is ``conflict``.
    """
    det_year = _year_from_date(detected.get("publication_date"))
    work_year = _year_from_date(work_row.get("first_publish_date"))

    if det_year is None and work_year is None:
        return {
            "signal": SIGNAL_YEAR,
            "state": STATE_UNAVAILABLE,
            "detail": "no year on either side",
            "weight": _weight(SIGNAL_YEAR),
        }
    if det_year is None:
        return {
            "signal": SIGNAL_YEAR,
            "state": STATE_UNAVAILABLE,
            "detail": "no publication year in detected file",
            "weight": _weight(SIGNAL_YEAR),
        }
    if work_year is None:
        return {
            "signal": SIGNAL_YEAR,
            "state": STATE_UNAVAILABLE,
            "detail": "no publication year in work",
            "weight": _weight(SIGNAL_YEAR),
        }

    if det_year == work_year:
        return {
            "signal": SIGNAL_YEAR,
            "state": STATE_EXACT,
            "detail": f"year matches: {det_year}",
            "weight": _weight(SIGNAL_YEAR),
        }

    if abs(det_year - work_year) <= 1:
        return {
            "signal": SIGNAL_YEAR,
            "state": STATE_SIMILAR,
            "detail": f"year similar: {det_year} vs {work_year}",
            "weight": _weight(SIGNAL_YEAR),
        }

    return {
        "signal": SIGNAL_YEAR,
        "state": STATE_CONFLICT,
        "detail": f"year conflict: {det_year} vs {work_year}",
        "weight": _weight(SIGNAL_YEAR),
    }


def _signal_narrator(
    detected: dict[str, Any],
    work_row: dict[str, Any],
) -> dict[str, str | float]:
    """Compare detected narrators against the work's narrators.

    Only included when *both* sides have narrator data.
    """
    det_narrators = detected.get("narrators") or []
    work_narrators = work_row.get("narrators") or []

    if not det_narrators or not work_narrators:
        return {
            "signal": SIGNAL_NARRATOR,
            "state": STATE_UNAVAILABLE,
            "detail": "narrator data not available on both sides",
            "weight": _weight(SIGNAL_NARRATOR),
        }

    det_norm = {_norm(n) for n in det_narrators}
    work_norm = {_norm(n) for n in work_narrators}

    if det_norm == work_norm:
        return {
            "signal": SIGNAL_NARRATOR,
            "state": STATE_EXACT,
            "detail": f"narrators match: {', '.join(det_narrators)}",
            "weight": _weight(SIGNAL_NARRATOR),
        }

    overlap = det_norm & work_norm
    if overlap:
        return {
            "signal": SIGNAL_NARRATOR,
            "state": STATE_SIMILAR,
            "detail": f"narrators partially match: {', '.join(sorted(overlap))}",
            "weight": _weight(SIGNAL_NARRATOR),
        }

    return {
        "signal": SIGNAL_NARRATOR,
        "state": STATE_CONFLICT,
        "detail": f"narrators conflict: '{', '.join(det_narrators)}' vs '{', '.join(work_narrators)}'",
        "weight": _weight(SIGNAL_NARRATOR),
    }


def _signal_duration(
    detected: dict[str, Any],
    work_row: dict[str, Any],
) -> dict[str, str | float]:
    """Compare detected duration against the work's edition duration.

    Only included when *both* sides have duration data.
    """
    det_dur = detected.get("duration_seconds")
    work_dur = work_row.get("duration_seconds")

    if det_dur is None or work_dur is None:
        return {
            "signal": SIGNAL_DURATION,
            "state": STATE_UNAVAILABLE,
            "detail": "duration data not available on both sides",
            "weight": _weight(SIGNAL_DURATION),
        }

    if det_dur == work_dur:
        return {
            "signal": SIGNAL_DURATION,
            "state": STATE_EXACT,
            "detail": f"duration matches: {det_dur}s",
            "weight": _weight(SIGNAL_DURATION),
        }

    # Allow 10% tolerance
    ratio = min(det_dur, work_dur) / max(det_dur, work_dur)
    if ratio >= 0.9:
        return {
            "signal": SIGNAL_DURATION,
            "state": STATE_SIMILAR,
            "detail": f"duration similar: {det_dur}s vs {work_dur}s",
            "weight": _weight(SIGNAL_DURATION),
        }

    return {
        "signal": SIGNAL_DURATION,
        "state": STATE_CONFLICT,
        "detail": f"duration conflict: {det_dur}s vs {work_dur}s",
        "weight": _weight(SIGNAL_DURATION),
    }


def _signal_embedded_metadata(
    detected: dict[str, Any],
    work_row: dict[str, Any],
) -> dict[str, str | float]:
    """Signal whether embedded metadata was present in the detected file.

    Presence of embedded tags is a positive signal but not a definitive
    match against the work, so the state is ``similar`` rather than
    ``exact``.
    """
    if detected.get("embedded_tags"):
        return {
            "signal": SIGNAL_EMBEDDED_METADATA,
            "state": STATE_SIMILAR,
            "detail": "embedded metadata present",
            "weight": _weight(SIGNAL_EMBEDDED_METADATA),
        }
    return {
        "signal": SIGNAL_EMBEDDED_METADATA,
        "state": STATE_UNAVAILABLE,
        "detail": "no embedded metadata",
        "weight": _weight(SIGNAL_EMBEDDED_METADATA),
    }


def _signal_directory_structure(
    detected: dict[str, Any],
    work_row: dict[str, Any],
) -> dict[str, str | float]:
    """Signal whether directory structure evidence was available.

    Presence of directory structure evidence is a positive signal but not
    a definitive match against the work, so the state is ``similar``
    rather than ``exact``.
    """
    evidence = detected.get("evidence") or {}
    if evidence:
        return {
            "signal": SIGNAL_DIRECTORY_STRUCTURE,
            "state": STATE_SIMILAR,
            "detail": "directory structure evidence available",
            "weight": _weight(SIGNAL_DIRECTORY_STRUCTURE),
        }
    return {
        "signal": SIGNAL_DIRECTORY_STRUCTURE,
        "state": STATE_UNAVAILABLE,
        "detail": "no directory structure evidence",
        "weight": _weight(SIGNAL_DIRECTORY_STRUCTURE),
    }


def _signal_provider_agreement(
    detected: dict[str, Any],
    work_row: dict[str, Any],
) -> dict[str, str | float]:
    """Signal whether a provider agreed on this match.

    This is a placeholder for the provider-agreement signal; the actual
    provider evidence is carried in ``detected.get("provider_evidence")``
    and will be wired in the sibling issue.

    Presence of provider evidence is a positive signal but not a
    definitive match against the work, so the state is ``similar``
    rather than ``exact``.
    """
    provider_evidence = detected.get("provider_evidence")
    if provider_evidence:
        return {
            "signal": SIGNAL_PROVIDER_AGREEMENT,
            "state": STATE_SIMILAR,
            "detail": "provider evidence available",
            "weight": _weight(SIGNAL_PROVIDER_AGREEMENT),
        }
    return {
        "signal": SIGNAL_PROVIDER_AGREEMENT,
        "state": STATE_UNAVAILABLE,
        "detail": "no provider evidence",
        "weight": _weight(SIGNAL_PROVIDER_AGREEMENT),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _work_authors(work_row: dict[str, Any]) -> list[str]:
    """Extract author names from a work row.

    Handles both plain string lists and dicts with 'name' keys.
    """
    raw = work_row.get("authors") or work_row.get("contributors") or []
    if not raw:
        return []
    if isinstance(raw[0], dict):
        return [c.get("name", "") for c in raw if c.get("name")]
    return [str(a) for a in raw if a]
