"""Small helpers for scripts/ui-player-seed.py (run with cwd=bookdoe/)."""
import struct
import zlib

from app import db


def own(work_id: str, kind: str = "ebook", fmt: str = "epub") -> str:
    aid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,added_at,updated_at) VALUES (?,?,?,?,'owned',0,0,?,?)",
            (aid, work_id, kind, fmt, t, t),
        )
    return aid


def png(seed: str, w: int = 40, h: int = 60) -> bytes:
    """A real, decodable PNG (solid colour from the seed) so <img> gets naturalWidth > 0."""
    r, g, b = (hash(seed) & 0xFF, (hash(seed) >> 8) & 0xFF, (hash(seed) >> 16) & 0xFF)
    raw = b"".join(b"\x00" + bytes([r, g, b]) * w for _ in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
