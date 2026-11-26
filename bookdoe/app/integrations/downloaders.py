from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .. import db
from ..services import acquisition
from ..crypto import decrypt_secret, scrub_secrets


def _safe_url(url: str) -> str:
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Integration URL must be http(s), contain a hostname, and not embed credentials")
    return url.rstrip("/")


def _join_download_path(base: str | None, name: str | None) -> str | None:
    """Narrow a client's shared download directory to one download's own payload.

    Clients report the directory they place completed items in, which is shared by
    every download. Importing that directory imports everyone else's files too, so
    the item's own name must always be appended.
    """
    if not base:
        return None
    if not name:
        return base
    return str(Path(base) / name)


def test(client: dict[str, Any]) -> dict[str, Any]:
    acquisition.ensure_enabled()
    try:
        kind = client["kind"]
        if kind == "sabnzbd":
            return _sab_test(client)
        if kind == "qbittorrent":
            return _qbit_test(client)
        if kind == "nzbget":
            return _nzbget_test(client)
        if kind == "transmission":
            return _transmission_test(client)
        return {"ok": False, "message": f"Unsupported client: {kind}"}
    except Exception as exc:
        return {"ok": False, "message": scrub_secrets(exc, *_client_secrets(client))}


def _client_secrets(client: dict[str, Any]) -> list[str | None]:
    return [decrypt_secret(client.get(k)) for k in ("api_key_enc", "password_enc")]


def add(client: dict[str, Any], release: dict[str, Any]) -> dict[str, Any]:
    acquisition.ensure_enabled()
    kind = client["kind"]
    if kind == "sabnzbd":
        return _sab_add(client, release)
    if kind == "qbittorrent":
        return _qbit_add(client, release)
    if kind == "nzbget":
        return _nzbget_add(client, release)
    if kind == "transmission":
        return _transmission_add(client, release)
    raise ValueError(f"Unsupported download client: {kind}")


def poll(client: dict[str, Any]) -> list[dict[str, Any]]:
    acquisition.ensure_enabled()
    kind = client["kind"]
    if kind == "sabnzbd":
        return _sab_poll(client)
    if kind == "qbittorrent":
        return _qbit_poll(client)
    if kind == "nzbget":
        return _nzbget_poll(client)
    if kind == "transmission":
        return _transmission_poll(client)
    return []


def remove(client: dict[str, Any], job_id: str | None, name: str | None = None) -> None:
    """Ask the client to drop one job and its downloaded data; raises if it cannot."""
    acquisition.ensure_enabled()
    kind = client["kind"]
    if kind == "sabnzbd":
        return _sab_remove(client, job_id)
    if kind == "qbittorrent":
        return _qbit_remove(client, job_id, name)
    if kind == "nzbget":
        return _nzbget_remove(client, job_id)
    if kind == "transmission":
        return _transmission_remove(client, job_id)
    raise ValueError(f"Unsupported download client: {kind}")


def _sab_params(c: dict[str, Any]) -> dict[str, Any]:
    return {"apikey": decrypt_secret(c.get("api_key_enc")), "output": "json"}


def _sab_test(c):
    params = {**_sab_params(c), "mode": "version"}
    r = httpx.get(_safe_url(c["base_url"]) + "/api", params=params, timeout=10, follow_redirects=False)
    r.raise_for_status()
    return {"ok": True, "message": f"SABnzbd {r.json().get('version','reachable')}"}


def _sab_add(c, rel):
    params = {**_sab_params(c), "mode": "addurl", "name": rel["download_url"]}
    if c.get("category"):
        params["cat"] = c["category"]
    r = httpx.get(_safe_url(c["base_url"]) + "/api", params=params, timeout=20, follow_redirects=False)
    r.raise_for_status()
    data = r.json()
    ids = data.get("nzo_ids") or []
    return {"ok": bool(data.get("status", True)), "id": ids[0] if ids else None, "raw": data}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _hms_seconds(v):
    """SABnzbd timeleft 'H:MM:SS' -> seconds, or None."""
    try:
        h, m, sec = (int(p) for p in str(v).split(":"))
        return h * 3600 + m * 60 + sec
    except ValueError:
        return None


def _sab_stats(x, speed_kb):
    total, left = _num(x.get("mb")), _num(x.get("mbleft"))
    stats = {}
    if total is not None:
        stats["size_bytes"] = int(total * 1048576)
        if left is not None:
            stats["downloaded_bytes"] = int(max(0.0, total - left) * 1048576)
    if speed_kb is not None:
        stats["speed_bps"] = int(speed_kb * 1024)
    eta = _hms_seconds(x.get("timeleft"))
    if eta is not None:
        stats["eta_seconds"] = eta
    return stats


def _sab_poll(c):
    base = _safe_url(c["base_url"]) + "/api"
    out = []
    r = httpx.get(base, params={**_sab_params(c), "mode": "queue", "limit": 200}, timeout=15, follow_redirects=False)
    r.raise_for_status()
    q = r.json().get("queue") or {}
    speed_kb = _num(q.get("kbpersec"))
    for x in q.get("slots") or []:
        out.append({"id": x.get("nzo_id"), "name": x.get("filename"), "progress": float(x.get("percentage") or 0) / 100, "state": "downloading", "path": None, **_sab_stats(x, speed_kb)})
    # A finished job leaves the queue entirely; only history carries the final
    # location (`storage`), so without this a completed usenet grab can never import.
    h = httpx.get(base, params={**_sab_params(c), "mode": "history", "limit": 200}, timeout=15, follow_redirects=False)
    h.raise_for_status()
    for x in (h.json().get("history") or {}).get("slots") or []:
        status = str(x.get("status") or "").lower()
        out.append({
            "id": x.get("nzo_id"),
            "name": x.get("name"),
            "progress": 1.0 if status == "completed" else 0.0,
            "state": "completed" if status == "completed" else ("failed" if status == "failed" else "downloading"),
            "path": x.get("storage") or None,
        })
    return out


def _sab_remove(c, job_id):
    if not job_id:
        raise ValueError("No client job id recorded for this download")
    base = _safe_url(c["base_url"]) + "/api"
    # A job is in the queue while active and in history once finished; SABnzbd answers
    # status=true for an unknown id, so clearing both is the only way to be sure.
    for mode in ("queue", "history"):
        r = httpx.get(base, params={**_sab_params(c), "mode": mode, "name": "delete", "value": job_id, "del_files": 1}, timeout=15, follow_redirects=False)
        r.raise_for_status()


@contextmanager
def _qbit_session(c):
    """A logged-in qBittorrent client. A context manager because httpx refuses to
    re-enter a client that has already sent the login request."""
    with httpx.Client(base_url=_safe_url(c["base_url"]), timeout=20, follow_redirects=False) as client:
        user = decrypt_secret(c.get("username_enc")) or "admin"
        password = decrypt_secret(c.get("password_enc")) or ""
        r = client.post("/api/v2/auth/login", data={"username": user, "password": password})
        r.raise_for_status()
        if r.text.strip() not in {"Ok.", "Ok"}:
            raise ValueError("qBittorrent authentication failed")
        yield client


def _qbit_test(c):
    with _qbit_session(c) as s:
        r = s.get("/api/v2/app/version")
        r.raise_for_status()
        return {"ok": True, "message": f"qBittorrent {r.text.strip()}"}


def _qbit_add(c, rel):
    with _qbit_session(c) as s:
        data = {"urls": rel["download_url"]}
        if c.get("category"):
            data["category"] = c["category"]
        r = s.post("/api/v2/torrents/add", data=data)
        r.raise_for_status()
        # qBittorrent doesn't return the hash for URL adds. Storing the indexer guid here
        # (it is usually a URL) made poll_all believe the row already had an id and skip
        # its name correlation, so the download never completed. None lets poll bind the hash.
        return {"ok": r.text.strip().lower().startswith("ok"), "id": None, "raw": r.text}


def _qbit_remove(c, job_id, name):
    # Deletion removes the torrent's files, so it only ever acts on an exact hash. A name
    # match is not good enough: two torrents can share one, and a wrong delete is unrecoverable.
    if not job_id:
        raise ValueError("No client job id recorded for this download")
    with _qbit_session(c) as s:
        r = s.get("/api/v2/torrents/info", params={"filter": "all"})
        r.raise_for_status()
        hit = [x for x in r.json() if x.get("hash") == job_id]
        if len(hit) != 1:
            raise ValueError("Torrent not found in qBittorrent (the stored id is not a torrent hash)")
        r = s.post("/api/v2/torrents/delete", data={"hashes": hit[0]["hash"], "deleteFiles": "true"})
        r.raise_for_status()


def _qbit_poll(c):
    with _qbit_session(c) as s:
        r = s.get("/api/v2/torrents/info", params={"filter": "all"})
        r.raise_for_status()
        out = []
        for x in r.json():
            state = str(x.get("state") or "")
            done = state in {"uploading", "stalledUP", "pausedUP", "queuedUP", "forcedUP"} or float(x.get("progress") or 0) >= 1
            out.append({"id": x.get("hash"), "name": x.get("name"), "progress": float(x.get("progress") or 0), "state": "completed" if done else "downloading", "path": x.get("content_path") or _join_download_path(x.get("save_path"), x.get("name")),
                        "size_bytes": x.get("size") or x.get("total_size"), "downloaded_bytes": x.get("downloaded"),
                        "speed_bps": x.get("dlspeed"), "eta_seconds": x.get("eta") if 0 <= int(x.get("eta") or -1) < 8640000 else None})
        return out


def _nzbget_rpc(c, method: str, params: list[Any] | None = None):
    user = decrypt_secret(c.get("username_enc")) or "nzbget"
    pw = decrypt_secret(c.get("password_enc")) or ""
    auth = (user, pw)
    r = httpx.post(_safe_url(c["base_url"]) + "/jsonrpc", auth=auth, json={"method": method, "params": params or [], "id": 1}, timeout=15, follow_redirects=False)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise ValueError(str(data["error"]))
    return data.get("result")


def _nzbget_test(c):
    version = _nzbget_rpc(c, "version")
    return {"ok": True, "message": f"NZBGet {version}"}


def _nzbget_add(c, rel):
    # appendurl is supported by modern NZBGet. Arguments: name, category, priority, addtop, paused, url.
    name = rel.get("raw_title") or rel.get("title") or "book"
    result = _nzbget_rpc(c, "appendurl", [name, c.get("category") or "", 0, False, False, rel["download_url"]])
    return {"ok": bool(result), "id": str(result) if result else None}


def _nzbget_remove(c, job_id):
    if not job_id:
        raise ValueError("No client job id recorded for this download")
    nzb_id = int(job_id)
    # editqueue returns false when no such id exists in that list: try the active
    # queue first, then the history of finished jobs.
    if _nzbget_rpc(c, "editqueue", ["GroupFinalDelete", "", [nzb_id]]):
        return
    if not _nzbget_rpc(c, "editqueue", ["HistoryFinalDelete", "", [nzb_id]]):
        raise ValueError("Job not found in NZBGet")


def _nzbget_poll(c):
    groups = _nzbget_rpc(c, "listgroups") or []
    out = [{"id": str(x.get("NZBID")), "name": x.get("NZBName"), "progress": 1 - (float(x.get("RemainingSizeMB") or 0) / max(0.001, float(x.get("FileSizeMB") or 1))), "state": "downloading", "path": x.get("DestDir"),
            "size_bytes": int(float(x.get("FileSizeMB") or 0) * 1048576) or None,
            "downloaded_bytes": int(max(0.0, float(x.get("FileSizeMB") or 0) - float(x.get("RemainingSizeMB") or 0)) * 1048576)} for x in groups]
    # A finished job leaves listgroups and only `history` records how it ended and where
    # the files are, so without this an NZBGet grab could never complete or import.
    for x in _nzbget_rpc(c, "history") or []:
        status = str(x.get("Status") or "").upper()
        if status.startswith("SUCCESS"):
            state = "completed"
        elif status.startswith("FAILURE"):
            state = "failed"
        else:
            continue  # DELETED/*, WARNING/* and the like are left for an operator
        out.append({"id": str(x.get("NZBID")), "name": x.get("Name") or x.get("NZBName"), "progress": 1.0 if state == "completed" else 0.0, "state": state, "path": x.get("FinalDir") or x.get("DestDir") or None})
    return out


# torrent-add asks the client to fetch the .torrent itself, which can mean a slow
# round trip to a private tracker (often via a VPN). Status calls stay snappy.
_TRANSMISSION_TIMEOUTS = {"torrent-add": 120.0}
_TRANSMISSION_DEFAULT_TIMEOUT = 15.0


def _transmission_request(c, method: str, arguments: dict[str, Any] | None = None):
    url = _safe_url(c["base_url"]) + "/transmission/rpc"
    auth = None
    user = decrypt_secret(c.get("username_enc"))
    pw = decrypt_secret(c.get("password_enc"))
    if user:
        auth = (user, pw or "")
    headers = {}
    timeout = _TRANSMISSION_TIMEOUTS.get(method, _TRANSMISSION_DEFAULT_TIMEOUT)
    payload = {"method": method, "arguments": arguments or {}}
    r = httpx.post(url, json=payload, auth=auth, headers=headers, timeout=timeout, follow_redirects=False)
    if r.status_code == 409:
        sid = r.headers.get("X-Transmission-Session-Id")
        r = httpx.post(url, json=payload, auth=auth, headers={"X-Transmission-Session-Id": sid or ""}, timeout=timeout, follow_redirects=False)
    r.raise_for_status()
    data = r.json()
    if data.get("result") != "success":
        raise ValueError(data.get("result") or "Transmission error")
    return data.get("arguments") or {}


def _transmission_test(c):
    data = _transmission_request(c, "session-get", {"fields": ["version"]})
    return {"ok": True, "message": f"Transmission {data.get('version','reachable')}"}


def _fetch_torrent_metainfo(url: str) -> str | None:
    """Download a .torrent and return it base64-encoded, or None if it is not one.

    Handing Transmission a URL makes *it* responsible for reaching the indexer. That
    fails whenever the client is network-isolated from the indexer -- a VPN-confined
    torrent client being the common case -- and it fails as a hang rather than an
    error, because the RPC call blocks until the client's own fetch gives up.
    bookdoe can already reach the indexer (it just searched it), so fetching the
    metainfo here and passing the bytes is both faster and far more robust.
    """
    try:
        r = httpx.get(url, timeout=60, follow_redirects=True)
        r.raise_for_status()
    except Exception:
        return None
    body = r.content
    # A bencoded torrent starts with 'd'. Anything else (an HTML error page, a magnet
    # redirect landing page) must fall through to the client rather than be sent on.
    if not body[:1] == b"d":
        return None
    return base64.b64encode(body).decode("ascii")


def _transmission_add(c, rel):
    url = rel["download_url"]
    metainfo = None
    if url and not url.startswith("magnet:"):
        metainfo = _fetch_torrent_metainfo(url)
    args = {"metainfo": metainfo} if metainfo else {"filename": url}
    result = _transmission_request(c, "torrent-add", args)
    item = result.get("torrent-added") or result.get("torrent-duplicate") or {}
    return {"ok": True, "id": str(item.get("hashString") or item.get("id") or ""), "raw": result}


def _transmission_remove(c, job_id):
    if not job_id:
        raise ValueError("No client job id recorded for this download")
    _transmission_request(c, "torrent-remove", {"ids": [job_id], "delete-local-data": True})


def _transmission_poll(c):
    data = _transmission_request(c, "torrent-get", {"fields": ["id", "hashString", "name", "percentDone", "status", "downloadDir", "isFinished", "totalSize", "sizeWhenDone", "rateDownload", "eta"]})
    out = []
    for x in data.get("torrents") or []:
        done = bool(x.get("isFinished")) or float(x.get("percentDone") or 0) >= 1
        # downloadDir is shared by every torrent in the client. Importing it would
        # sweep up every other completed download, so always narrow to this
        # torrent's own file/folder inside it.
        out.append({"id": x.get("hashString") or str(x.get("id")), "name": x.get("name"), "progress": float(x.get("percentDone") or 0), "state": "completed" if done else "downloading", "path": _join_download_path(x.get("downloadDir"), x.get("name")),
                    "size_bytes": x.get("totalSize") or x.get("sizeWhenDone"), "downloaded_bytes": int((x.get("sizeWhenDone") or 0) * float(x.get("percentDone") or 0)) or None,
                    "speed_bps": x.get("rateDownload"), "eta_seconds": x.get("eta") if int(x.get("eta") or -1) >= 0 else None})
    return out
