"""Seeded large-library generator for the F-09 performance tests.

Nothing here is committed data: ``seed_large_library`` builds the library in the
test database from a fixed random seed, so every run measures the same shape.
Rows go in with ``executemany`` on one connection; going through the import
services for 100,000 works would measure the importer, not the read paths.
"""
from __future__ import annotations

import random

from app import db

WORKS = 100_000
AUTHORS = 20_000
SERIES = 8_000
OWNED_EVERY = 2  # every 2nd work has an owned ebook with one file (50,000 owned)

_WORDS = [
    "Silent", "Crimson", "Dead", "World", "Iron", "Garden", "Winter", "Hollow", "Star", "River",
    "Shadow", "Glass", "Last", "Ember", "Harbor", "Lost", "Orbit", "Sparrow", "Salt", "Thunder",
    "Paper", "Wolf", "Tide", "Cinder", "Lantern", "Empire", "Atlas", "Maple", "Quiet", "Vault",
]
_FIRST = ["Ada", "Ben", "Cora", "Dev", "Elin", "Farid", "Gwen", "Hugo", "Iris", "Jon", "Kira", "Leo"]
_LAST = ["Alanson", "Brook", "Castell", "Dunmore", "Ekström", "Faulk", "Garrow", "Hale", "Imrie", "Jarrah"]

# Titles/authors the tests search for.  Both occur a known, small number of times.
NEEDLE_TITLE = "Zanzibar Meridian"
NEEDLE_AUTHOR = "Quillon Vantongeren"


def seed_large_library(works: int = WORKS, seed: int = 20261004) -> dict[str, int]:
    rng = random.Random(seed)
    t = db.now()
    authors = max(1, works * AUTHORS // WORKS)
    n_series = max(1, works * SERIES // WORKS)
    author_ids = [f"a{i:06d}" for i in range(authors)]
    series_ids = [f"s{i:05d}" for i in range(n_series)]
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",
            [
                (
                    aid,
                    NEEDLE_AUTHOR if i == 0 else f"{rng.choice(_FIRST)} {rng.choice(_LAST)} {i}",
                    None,
                    t,
                    t,
                )
                for i, aid in enumerate(author_ids)
            ],
        )
        conn.executemany(
            "INSERT INTO series(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",
            [(sid, f"{rng.choice(_WORDS)} Cycle {i}", None, t, t) for i, sid in enumerate(series_ids)],
        )
        work_rows, wc_rows, sm_rows, asset_rows, file_rows = [], [], [], [], []
        for i in range(works):
            wid = f"w{i:06d}"
            title = NEEDLE_TITLE if i == 2 else f"{rng.choice(_WORDS)} {rng.choice(_WORDS)} {i}"
            work_rows.append((wid, title, title.lower(), t, t))
            wc_rows.append((wid, author_ids[i % authors], "author", 0))
            if i % 5 == 0:
                sm_rows.append((wid, series_ids[i % n_series], str(i % 7 + 1)))
            if i % OWNED_EVERY == 0:
                aid = f"x{i:06d}"
                asset_rows.append((aid, wid, "ebook", "epub", "owned", 0, 0, t, t))
                file_rows.append((f"f{i:06d}", aid, f"/seed/{i:06d}.epub", 1, 1024, "epub", 0, t))
            elif i % 10 == 1:
                asset_rows.append((f"x{i:06d}", wid, "audiobook", None, "missing", 1, 1, t, t))
        conn.executemany(
            "INSERT INTO works(id,title,sort_title,added_at,updated_at) VALUES (?,?,?,?,?)", work_rows
        )
        conn.executemany(
            "INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,?)", wc_rows
        )
        conn.executemany(
            "INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", sm_rows
        )
        conn.executemany(
            "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,added_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            asset_rows,
        )
        conn.executemany(
            "INSERT INTO media_files(id,asset_id,path,part_number,size_bytes,container,missing,discovered_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            file_rows,
        )
    return {"works": works, "authors": authors, "series": n_series, "owned": len(file_rows)}
