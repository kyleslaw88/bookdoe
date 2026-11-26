"""Tiny generated fixtures for the conversion tests (no binary files are checked in).

- ``make_mp3`` / ``make_audio``: short sine tones through ffmpeg with ID3 title/track tags.
- ``make_mobi``: a hand-assembled MOBI 6 file (PalmDB header, PalmDOC + MOBI + EXTH header,
  uncompressed text records, one JPEG cover record). ``drm=True`` sets the PalmDOC
  encryption field the way a Mobipocket-DRM book does; the text is then not real, which is
  fine: conversion must refuse such a file before reading it.
- ``managed_root`` / ``adopted_root`` / ``add_book``: a library root of either mode and an
  owned asset with real files in it.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

from app import db

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

# 1x1 JPEG
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfcffd9"
)


def make_audio(path: Path, *, seconds: float = 2.0, freq: int = 440, rate: int = 22050, channels: int = 1,
               title: str | None = None, track: int | None = None, codec_args: list[str] | None = None) -> Path:
    """A sine tone; the container follows the file extension."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate={rate}",
           "-ac", str(channels)]
    if title:
        cmd += ["-metadata", f"title={title}"]
    if track is not None:
        cmd += ["-metadata", f"track={track}"]
    cmd += (codec_args or []) + [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def make_mp3(path: Path, **kw) -> Path:
    return make_audio(path, codec_args=["-b:a", "32k"], **kw)


def make_mobi(path: Path, *, title: str = "Tiny Book", author: str = "Anne Author", drm: bool = False,
              paragraphs: int = 30, cover: bool = True) -> Path:
    """A valid, minimal, uncompressed MOBI 6 book KindleUnpack can read."""
    body = "".join(f"<p>Paragraph {i}. The quick brown fox jumps over the lazy dog.</p>" for i in range(paragraphs))
    html = (f"<html><head><guide></guide></head><body><h1>{title}</h1>"
            f"<mbp:pagebreak/><h2>Chapter Two</h2>{body}<br><p>The end &amp; more.</p></body></html>").encode("utf-8")
    text_records = [html[i:i + 4096] for i in range(0, len(html), 4096)]
    exth_records = [(100, author.encode()), (503, title.encode()), (106, b"2020-01-01")]
    if cover:
        exth_records += [(201, struct.pack(">I", 0)), (203, struct.pack(">I", 0))]
    exth_body = b"".join(struct.pack(">II", t, 8 + len(d)) + d for t, d in exth_records)
    exth = b"EXTH" + struct.pack(">II", 12 + len(exth_body), len(exth_records)) + exth_body
    exth += b"\x00" * ((4 - len(exth) % 4) % 4)
    full_name = title.encode()
    n_text = len(text_records)
    head = bytearray(16 + 232)               # PalmDOC header (16) + MOBI header (232); offsets below are from record 0
    struct.pack_into(">HHIHHH", head, 0, 1, 0, len(html), n_text, 4096, 2 if drm else 0)
    head[0x10:0x14] = b"MOBI"
    struct.pack_into(">III", head, 0x14, 232, 2, 65001)          # header length, type (book), utf-8
    struct.pack_into(">II", head, 0x20, 0x1234, 6)               # unique id, file version 6
    for off in range(0x28, 0x50, 4):
        struct.pack_into(">I", head, off, 0xFFFFFFFF)            # no index records
    struct.pack_into(">I", head, 0x50, n_text + 1 + (1 if cover else 0))   # first non-text record
    struct.pack_into(">I", head, 0x54, len(head) + len(exth))    # full name offset
    struct.pack_into(">II", head, 0x58, len(full_name), 9)       # full name length, locale en
    struct.pack_into(">I", head, 0x68, 6)                        # min version
    struct.pack_into(">I", head, 0x6C, n_text + 1 if cover else 0xFFFFFFFF)   # first image record
    struct.pack_into(">I", head, 0x80, 0x40)                     # EXTH present
    struct.pack_into(">I", head, 0xA8, 0xFFFFFFFF)               # no DRM record offset
    struct.pack_into(">HH", head, 0xC0, 1, n_text)               # first/last content record
    for off in (0xC8, 0xD0, 0xE0, 0xF4):                         # fcis, flis, srcs, ncx index: none
        struct.pack_into(">I", head, off, 0xFFFFFFFF)
    record0 = bytes(head) + exth + full_name + b"\x00" * 4
    records = [record0, *text_records]
    if cover:
        records.append(JPEG)
    records.append(b"\xe9\x8e\r\n")                      # end-of-file record
    n = len(records)
    offset = 78 + 8 * n + 2
    table = b""
    for i, r in enumerate(records):
        table += struct.pack(">II", offset, 2 * i)
        offset += len(r)
    db_header = (title[:31].encode().ljust(32, b"\x00") + struct.pack(">HHIIIIII", 0, 0, 0, 0, 0, 0, 0, 0)
                 + b"BOOKMOBI" + struct.pack(">IIH", 2 * n, 0, n))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(db_header + table + b"\x00\x00" + b"".join(records))
    return path


def root(tmp_path: Path, mode: str = "managed", kind: str = "mixed", name: str | None = None) -> dict:
    base = tmp_path / (name or f"{mode}-{kind}")
    base.mkdir(parents=True, exist_ok=True)
    rid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at) VALUES (?,?,?,?,?,1,1,?,?)",
            (rid, base.name, str(base), kind, mode, db.now(), db.now()))
    return {"id": rid, "path": base}


def add_book(rt: dict, title: str, author: str, kind: str, files: list[Path], *, fmt: str | None = None,
             monitored: int = 0, intent: int = 0) -> tuple[str, str]:
    """A work with one owned asset whose files are already on disk. Returns (work_id, asset_id)."""
    from app.services import migration, scanner

    work_id = migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})
    ext = fmt or files[0].suffix.lower().lstrip(".")
    asset_id = db.uid()
    with db.tx() as conn:
        conn.execute("DELETE FROM media_assets WHERE work_id=? AND kind=? AND format IS NULL AND status='none'", (work_id, kind))
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,root_folder_id,added_at,updated_at) VALUES (?,?,?,?,'owned',?,?,?,?,?)",
            (asset_id, work_id, kind, ext, monitored, intent, rt["id"], db.now(), db.now()))
        for i, f in enumerate(sorted(files), 1):
            st = f.stat()
            conn.execute(
                "INSERT INTO media_files(id,asset_id,path,relative_path,part_number,size_bytes,mtime,container,managed,adopted,missing,discovered_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
                (db.uid(), asset_id, str(f), str(f.relative_to(rt["path"])), i if len(files) > 1 else None, st.st_size, st.st_mtime,
                 f.suffix.lower().lstrip("."), 1, 0, db.now()))
    return work_id, asset_id


def add_derived(source_asset_id: str, fmt: str, path: Path, *, derived_by: str = "test 1") -> str:
    """A converted-copy asset (+ one file row) as services/conversion._register would leave it."""
    src = db.one("SELECT * FROM media_assets WHERE id=?", (source_asset_id,))
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"derived bytes " * 40)
    aid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,root_folder_id,added_at,updated_at,
                                        derived_from_asset_id,derived_by,derived_at) VALUES (?,?,?,?,'owned',0,0,?,?,?,?,?,?)""",
            (aid, src["work_id"], src["kind"], fmt, src["root_folder_id"], t, t, source_asset_id, derived_by, t))
        conn.execute(
            "INSERT INTO media_files(id,asset_id,path,size_bytes,mtime,container,managed,adopted,missing,discovered_at) VALUES (?,?,?,?,?,?,1,0,0,?)",
            (db.uid(), aid, str(path), path.stat().st_size, path.stat().st_mtime, fmt, t))
    return aid
