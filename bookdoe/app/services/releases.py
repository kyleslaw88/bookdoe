from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path
import re
import unicodedata
from typing import Any

from .. import db
from ..crypto import decrypt_secret
from ..integrations import indexers as ix
from . import acquisition

log = logging.getLogger(__name__)

AUDIO_FORMATS = ["m4b", "m4a", "mp3", "flac", "opus", "ogg", "aac"]
EBOOK_FORMATS = ["epub", "azw3", "mobi", "pdf", "azw", "fb2"]


def normalize(value: str | None) -> str:
    s = unicodedata.normalize("NFKD", value or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ISO 639-2/B style codes as they actually appear in release names, plus the English
# names scene groups use. Kept deliberately narrow: a token only counts as a language
# when it appears as a standalone word, because short codes like "ita" and "ger"
# collide with ordinary title words otherwise.
LANGUAGE_TOKENS: dict[str, tuple[str, ...]] = {
    "eng": ("eng", "english", "en"),
    "dut": ("dut", "nld", "dutch", "nederlands"),
    "ger": ("ger", "deu", "german", "deutsch"),
    "fre": ("fre", "fra", "french", "francais", "français"),
    "spa": ("spa", "esp", "spanish", "espanol", "español", "castellano"),
    "ita": ("ita", "italian", "italiano"),
    "por": ("por", "portuguese", "portugues", "português"),
    "rus": ("rus", "russian"),
    "pol": ("pol", "polish", "polski"),
    "swe": ("swe", "swedish", "svenska"),
    "nor": ("nor", "norwegian", "norsk"),
    "dan": ("dan", "danish", "dansk"),
    "fin": ("fin", "finnish", "suomi"),
    "cze": ("cze", "ces", "czech"),
    "hun": ("hun", "hungarian", "magyar"),
    "tur": ("tur", "turkish", "turkce", "türkçe"),
    "gre": ("gre", "ell", "greek"),
    "rom": ("rom", "ron", "romanian"),
    "ukr": ("ukr", "ukrainian"),
    "jpn": ("jpn", "japanese"),
    "chi": ("chi", "zho", "chinese", "mandarin"),
    "kor": ("kor", "korean"),
    "ara": ("ara", "arabic"),
    "heb": ("heb", "hebrew"),
    "hin": ("hin", "hindi"),
    "cat": ("cat", "catalan", "catala", "català"),
}

_LANG_LOOKUP: dict[str, str] = {tok: code for code, toks in LANGUAGE_TOKENS.items() for tok in toks}


def normalize_language(value: str | None) -> str | None:
    """Map any accepted spelling of a language onto its canonical code."""
    if not value:
        return None
    return _LANG_LOOKUP.get(str(value).strip().casefold())


def detect_language(normalized_title: str) -> str | None:
    """Return the language a release advertises, or None when it does not say.

    Most English releases carry no language tag at all, so "not stated" must stay
    distinct from "stated as something else" -- the scorer treats them differently.
    Two-letter codes are excluded here because they produce too many false hits in
    ordinary titles; only unambiguous tokens are honoured.
    """
    for code, tokens in LANGUAGE_TOKENS.items():
        for tok in tokens:
            if len(tok) < 3:
                continue
            if re.search(rf"\b{re.escape(tok)}\b", normalized_title):
                return code
    return None


def parse_release(title: str, kind: str) -> dict[str, Any]:
    n = normalize(title)
    formats = AUDIO_FORMATS if kind == "audiobook" else EBOOK_FORMATS
    fmt = next((f for f in formats if re.search(rf"\b{re.escape(f)}\b", n)), None)
    year_m = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    series_pos = None
    m = re.search(r"(?i)\b(?:book|bk|vol(?:ume)?)[ ._-]*(\d+(?:\.\d+)?)\b", title)
    if m:
        series_pos = m.group(1)
    return {
        "normalized": n,
        "format": fmt,
        "year": int(year_m.group(1)) if year_m else None,
        "abridged": True if "abridged" in n and "unabridged" not in n else False if "unabridged" in n else None,
        "unabridged_marker": "unabridged" in n,
        "series_position": series_pos,
        "language": detect_language(n),
        "retail": bool(re.search(r"\b(retail|audible|itunes)\b", n)),
    }


def _work_context(work_id: str) -> dict[str, Any]:
    work = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not work:
        raise ValueError("Work not found")
    authors = db.all(
        """SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id
           WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord""",
        (work_id,),
    )
    series = db.all(
        """SELECT s.name,sm.position FROM series_memberships sm JOIN series s ON s.id=sm.series_id WHERE sm.work_id=?""",
        (work_id,),
    )
    ids = db.all("SELECT namespace,value FROM identifiers WHERE entity_type='work' AND entity_id=?", (work_id,))
    ids += db.all(
        """SELECT i.namespace,i.value FROM editions e JOIN identifiers i ON i.entity_type='edition' AND i.entity_id=e.id WHERE e.work_id=?""",
        (work_id,),
    )
    return {
        "work": work,
        "authors": [a["name"] for a in authors],
        "series": series,
        "identifiers": {x["namespace"]: x["value"] for x in ids},
    }


def query_plan(work_id: str, kind: str) -> list[dict[str, Any]]:
    ctx = _work_context(work_id)
    title = ctx["work"]["title"]
    author = ctx["authors"][0] if ctx["authors"] else ""
    plans = []
    for ns in ("isbn13", "isbn10", "asin", "audible"):
        if ctx["identifiers"].get(ns):
            plans.append({"q": ctx["identifiers"][ns], "identifier": ns, "isbn": ctx["identifiers"][ns] if ns.startswith("isbn") else None})
    plans.extend([
        {"q": f"{author} {title}".strip(), "author": author, "title": title},
        {"q": title, "title": title},
    ])
    # Preserve order while dropping duplicates.
    seen, out = set(), []
    for p in plans:
        key = normalize(p["q"])
        if key and key not in seen:
            seen.add(key)
            p["media_kind"] = kind
            out.append(p)
    return out


def profile_formats(cfg: dict[str, Any], kind: str) -> tuple[list[str], list[str]]:
    """(allowed, preferred) format lists for a profile config, with the kind's defaults."""
    allowed = cfg.get("allowed_formats") or (AUDIO_FORMATS if kind == "audiobook" else EBOOK_FORMATS)
    return allowed, cfg.get("preferred_formats") or allowed


def format_rank(fmt: str, preferred: list[str]) -> int:
    """0 is best; a format the profile does not list ranks after every listed one."""
    return preferred.index(fmt) if fmt in preferred else len(preferred)


def abridged_rejected(cfg: dict[str, Any], abridged: bool | None) -> bool:
    """True only for a known-abridged audiobook under a profile that rejects them (unknown is not abridged)."""
    return cfg.get("abridged") == "reject" and abridged is True


def score_release(work_id: str, kind: str, release: dict[str, Any], profile: dict[str, Any] | None = None) -> tuple[int, list[dict[str, Any]], list[str], dict[str, Any]]:
    ctx = _work_context(work_id)
    p = parse_release(release["title"], kind)
    components: list[dict[str, Any]] = []
    rejected: list[str] = []
    nt = p["normalized"]
    title = normalize(ctx["work"]["title"])
    authors = [normalize(x) for x in ctx["authors"]]

    def add(label: str, score: int, evidence: str | None = None):
        components.append({"label": label, "score": score, "evidence": evidence})

    # Identifiers are strongest evidence.
    for ns, value in ctx["identifiers"].items():
        if value and normalize(value) in nt:
            add(f"Exact {ns.upper()} marker", 100, value)
            break
    if title and title in nt:
        add("Title exact/contained", 55, ctx["work"]["title"])
    else:
        title_tokens = set(title.split())
        rel_tokens = set(nt.split())
        overlap = len(title_tokens & rel_tokens) / max(1, len(title_tokens))
        if overlap >= 0.8:
            add("Title token match", 35, f"{overlap:.0%}")
        elif overlap < 0.45:
            rejected.append("Title does not sufficiently match")
            add("Weak title match", -60, f"{overlap:.0%}")
    if authors:
        if any(a and a in nt for a in authors):
            add("Author match", 45)
        else:
            # Do not hard-reject because many release titles omit author.
            add("Author not present", -5)
    for s in ctx["series"]:
        sn = normalize(s["name"])
        if sn and sn in nt:
            add("Series match", 20, s["name"])
            if s.get("position") and p.get("series_position") and normalize(str(s["position"])) == normalize(str(p["series_position"])):
                add("Series position match", 15, str(s["position"]))
    year = None
    if ctx["work"].get("first_publish_date"):
        m = re.search(r"\d{4}", str(ctx["work"]["first_publish_date"]))
        year = int(m.group()) if m else None
    if year and p["year"] == year:
        add("Publication year match", 10, str(year))

    cfg = profile or {}
    allowed, preferred = profile_formats(cfg, kind)
    if p["format"]:
        if p["format"] not in allowed:
            rejected.append(f"Format {p['format']} is not allowed")
            add("Disallowed format", -100, p["format"])
        else:
            rank = format_rank(p["format"], preferred)
            add("Preferred format", max(5, 35 - rank * 5), p["format"])
    else:
        add("Format not identified", -8)
    if kind == "audiobook":
        if abridged_rejected(cfg, p["abridged"]):
            rejected.append("Abridged audiobook rejected by profile")
            add("Abridged", -150)
        elif p["unabridged_marker"]:
            add("Unabridged marker", 20)
    size = release.get("size") or 0
    if size:
        mb = size / 1024 / 1024
        min_mb = float(cfg.get("min_size_mb", 0))
        max_mb = float(cfg.get("max_size_mb", 1e9))
        if mb < min_mb:
            rejected.append(f"Size {mb:.1f} MB is below profile minimum")
            add("Too small", -100, f"{mb:.1f} MB")
        elif mb > max_mb:
            rejected.append(f"Size {mb:.1f} MB exceeds profile maximum")
            add("Too large", -100, f"{mb:.1f} MB")
        else:
            add("Size within profile", 5, f"{mb:.1f} MB")
    if release.get("protocol") == "torrent":
        seeders = release.get("seeders")
        if seeders is not None:
            if int(seeders) == 0:
                add("No seeders", -35)
            elif int(seeders) >= 10:
                add("Healthy swarm", 10, str(seeders))
    # Language preference. "Not stated" is not evidence of the wrong language -- the
    # majority of English releases carry no tag - so only a release that explicitly
    # advertises a non-preferred language is penalised or rejected.
    preferred_langs = [normalize_language(x) or str(x).casefold() for x in (cfg.get("languages") or [])]
    policy = str(cfg.get("language_policy", "prefer")).casefold()
    rel_lang = p.get("language")
    if preferred_langs and policy != "ignore":
        if rel_lang is None:
            add("Language not stated", 0)
        elif rel_lang in preferred_langs:
            add("Preferred language", 25, rel_lang)
        elif policy == "reject":
            rejected.append(f"Language {rel_lang} is not in the preferred list")
            add("Non-preferred language", -150, rel_lang)
        else:
            add("Non-preferred language", -40, rel_lang)
    if p["retail"]:
        add("Retail-source marker", 8)
    blocked = db.one("SELECT id FROM blocklist WHERE (work_id=? OR work_id IS NULL) AND (indexer_guid=? OR release_title=?) AND (expires_at IS NULL OR expires_at>?) LIMIT 1", (work_id, release.get("guid"), release.get("title"), db.now()))
    if blocked:
        rejected.append("Release is blocklisted")
        add("Blocklisted", -1000)
    return sum(x["score"] for x in components), components, rejected, p


def search_releases(work_id: str, kind: str, *, automatic: bool = False) -> dict[str, Any]:
    acquisition.ensure_enabled()
    if kind not in {"ebook", "audiobook"}:
        raise ValueError("kind must be ebook or audiobook")
    search_id = db.uid()
    plans = query_plan(work_id, kind)
    with db.tx() as conn:
        conn.execute("INSERT INTO release_searches(id,work_id,asset_kind,query_json,automatic,started_at,status) VALUES (?,?,?,?,?,?,'running')", (search_id, work_id, kind, db.json_dump(plans), int(automatic), db.now()))
    indexers = db.all("SELECT * FROM indexers WHERE enabled=1 ORDER BY priority DESC,name")
    results: dict[tuple[str, str], dict[str, Any]] = {}
    errors = []

    def run_one(indexer: dict[str, Any], plan: dict[str, Any]):
        try:
            return indexer, plan, ix.search(indexer, plan), None
        except Exception as exc:
            return indexer, plan, [], str(exc)

    tasks = []
    # Query waterfall: identifier/title plans can yield duplicates; concurrent
    # execution keeps manual search responsive and dedupe happens below.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, max(1, len(indexers) * 2))) as pool:
        for indexer in indexers:
            for plan in plans[:4]:
                tasks.append(pool.submit(run_one, indexer, plan))
        for f in concurrent.futures.as_completed(tasks):
            indexer, plan, items, error = f.result()
            if error:
                errors.append({"indexer": indexer["name"], "query": plan["q"], "error": error})
                continue
            for item in items:
                key = (indexer["id"], str(item.get("guid") or item.get("download_url") or item.get("title")))
                item["indexer_id"] = indexer["id"]
                item["indexer_name"] = item.get("indexer_name") or indexer["name"]
                results.setdefault(key, item)

    profile_row = db.one(
        """SELECT p.config_json FROM media_assets a LEFT JOIN profiles p ON p.id=a.profile_id
           WHERE a.work_id=? AND a.kind=? AND a.derived_from_asset_id IS NULL ORDER BY a.added_at LIMIT 1""",
        (work_id, kind),
    )
    profile = db.json_load(profile_row["config_json"], {}) if profile_row else {}
    stored = []
    with db.tx() as conn:
        for item in results.values():
            score, components, rejected, parsed = score_release(work_id, kind, item, profile)
            rid = db.uid()
            conn.execute(
                """INSERT INTO releases(id,search_id,work_id,asset_kind,indexer_id,indexer_guid,protocol,raw_title,download_url,info_url,size_bytes,publish_date,seeders,leechers,grabs,parsed_json,score,score_json,rejected,rejection_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, search_id, work_id, kind, item["indexer_id"], item.get("guid"), item.get("protocol") or "unknown", item["title"], item.get("download_url"), item.get("info_url"), item.get("size"), item.get("publish_date"), item.get("seeders"), item.get("leechers"), item.get("grabs"), db.json_dump(parsed), score, db.json_dump(components), int(bool(rejected)), db.json_dump(rejected), db.now()),
            )
            stored.append({"id": rid, **item, "score": score, "score_components": components, "rejected": bool(rejected), "rejections": rejected, "parsed": parsed})
        conn.execute("UPDATE release_searches SET status='complete',finished_at=?,error=? WHERE id=?", (db.now(), db.json_dump(errors) if errors else None, search_id))
    stored.sort(key=lambda x: (x["rejected"], -x["score"], -(x.get("seeders") or 0)))
    return {"search_id": search_id, "items": stored, "errors": errors, "queries": plans}
