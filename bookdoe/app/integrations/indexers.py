from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import logging
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
from defusedxml.ElementTree import fromstring as safe_fromstring

import httpx

from .. import db
from ..services import acquisition
from ..crypto import decrypt_secret, scrub_secrets

log = logging.getLogger(__name__)


def test(indexer: dict[str, Any]) -> dict[str, Any]:
    acquisition.ensure_enabled()
    key = decrypt_secret(indexer.get("api_key_enc"))
    try:
        if indexer["kind"] == "prowlarr":
            with _client(indexer, 10) as c:
                r = c.get("/api/v1/health")
                r.raise_for_status()
            return {"ok": True, "message": "Prowlarr API reachable"}
        params = {"t": "caps", "o": "xml"}
        if key:
            params["apikey"] = key
        r = httpx.get(indexer["base_url"].rstrip("/") + "/api", params=params, timeout=10, follow_redirects=False)
        r.raise_for_status()
        safe_fromstring(r.content)
        return {"ok": True, "message": "Indexer capabilities reachable"}
    except Exception as exc:
        return {"ok": False, "message": scrub_secrets(exc, key)}


def search(indexer: dict[str, Any], query: dict[str, Any]) -> list[dict[str, Any]]:
    acquisition.ensure_enabled()
    try:
        if indexer["kind"] == "prowlarr":
            return _search_prowlarr(indexer, query)
        return _search_newznab(indexer, query)
    except Exception as exc:
        # The release search stores str(exc) per indexer; never let the URL's key through.
        raise IndexerError(scrub_secrets(exc, decrypt_secret(indexer.get("api_key_enc")))) from None


class IndexerError(RuntimeError):
    """An indexer call failed; the message is already scrubbed of credentials."""


def _client(indexer: dict[str, Any], timeout=30) -> httpx.Client:
    headers = {"Accept": "application/json", "User-Agent": "bookdoe/0.1"}
    key = decrypt_secret(indexer.get("api_key_enc"))
    if key:
        headers["X-Api-Key"] = key
    return httpx.Client(base_url=indexer["base_url"].rstrip("/"), headers=headers, timeout=timeout, follow_redirects=False)


def _search_prowlarr(indexer: dict[str, Any], query: dict[str, Any]) -> list[dict[str, Any]]:
    params: list[tuple[str, Any]] = [("query", query.get("q") or "")]
    cats = query.get("categories") or db.json_load(indexer.get("categories_json"), [])
    for c in cats:
        params.append(("categories", int(c)))
    if query.get("indexer_ids"):
        for i in query["indexer_ids"]:
            params.append(("indexerIds", int(i)))
    with _client(indexer) as c:
        r = c.get("/api/v1/search", params=params)
        r.raise_for_status()
        data = r.json()
    out = []
    for x in data if isinstance(data, list) else []:
        out.append({
            "guid": str(x.get("guid") or x.get("id") or _hash(x.get("title", ""))),
            "title": x.get("title") or "",
            "download_url": x.get("downloadUrl") or x.get("magnetUrl"),
            "info_url": x.get("infoUrl") or x.get("indexer"),
            "size": x.get("size"),
            "publish_date": x.get("publishDate"),
            "seeders": x.get("seeders"),
            "leechers": x.get("leechers"),
            "grabs": x.get("grabs"),
            "protocol": str(x.get("protocol") or ("torrent" if x.get("magnetUrl") else "usenet")).lower(),
            "categories": x.get("categories") or [],
            "indexer_name": x.get("indexer"),
            "raw": x,
        })
    return out


def _search_newznab(indexer: dict[str, Any], query: dict[str, Any]) -> list[dict[str, Any]]:
    key = decrypt_secret(indexer.get("api_key_enc"))
    media = query.get("media_kind")
    # Book search when supported; generic search is a resilient fallback.
    params = {"t": "book" if media == "ebook" else "search", "o": "xml", "q": query.get("q") or ""}
    if key:
        params["apikey"] = key
    cats = query.get("categories") or db.json_load(indexer.get("categories_json"), [])
    if cats:
        params["cat"] = ",".join(str(x) for x in cats)
    if query.get("author") and params["t"] == "book":
        params["author"] = query["author"]
    if query.get("title") and params["t"] == "book":
        params["title"] = query["title"]
    if query.get("isbn") and params["t"] == "book":
        params["isbn"] = query["isbn"]
    base = indexer["base_url"].rstrip("/")
    url = base if base.endswith("/api") else base + "/api"
    r = httpx.get(url, params=params, timeout=30, follow_redirects=False, headers={"User-Agent": "bookdoe/0.1"})
    r.raise_for_status()
    root = safe_fromstring(r.content)
    ns = {"newznab": "http://www.newznab.com/DTD/2010/feeds/attributes/"}
    out = []
    for item in root.findall(".//item"):
        attrs = {}
        for a in item.findall("newznab:attr", ns):
            attrs[a.attrib.get("name")] = a.attrib.get("value")
        enclosure = item.find("enclosure")
        link = _text(item, "link")
        title = _text(item, "title") or ""
        pub = _text(item, "pubDate")
        protocol = "torrent" if indexer["kind"] == "torznab" or (link or "").startswith("magnet:") else "usenet"
        out.append({
            "guid": _text(item, "guid") or _hash(title + (link or "")),
            "title": title,
            "download_url": (enclosure.attrib.get("url") if enclosure is not None else None) or link,
            "info_url": _text(item, "comments") or link,
            "size": _int((enclosure.attrib.get("length") if enclosure is not None else None) or attrs.get("size")),
            "publish_date": _iso_date(pub),
            "seeders": _int(attrs.get("seeders")),
            "leechers": _int(attrs.get("peers")),
            "grabs": _int(attrs.get("grabs")),
            "protocol": protocol,
            "categories": [a.attrib.get("value") for a in item.findall("category") if a.attrib.get("value")],
            "raw": attrs,
        })
    return out


def _text(el: ET.Element, tag: str) -> str | None:
    x = el.find(tag)
    return x.text.strip() if x is not None and x.text else None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _iso_date(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return email.utils.parsedate_to_datetime(v).isoformat()
    except Exception:
        return v


def _hash(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()
