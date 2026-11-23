#!/usr/bin/env python3
"""Build the authoritative movie database — PARALLEL edition.

Merges:
  1. youtube_free_movies.json   — 497 official "Free with ads" titles + URLs
                                  scraped from YouTube's own storefront DOM
  2. Wikipedia                  — real title, year, genre, runtime, poster
  3. votes.json                 — viewer ranking (OK lifts, FAKE sinks)

Output:
  georgeanton_movies.db   SQLite: title, url, genre, year, rating, duration,
                          availability, date_added, score, poster, status
  verified_catalog.json   site-ready shelf built only from verified data
"""
from __future__ import annotations

import difflib
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import curator  # noqa: E402

RESOLVED = HERE / "youtube_movies_resolved.json"
CATALOG = HERE / "movies_catalog.json"
VOTES = HERE / "votes.json"
DB = HERE / "georgeanton_movies.db"
OUT = HERE / "verified_catalog.json"


def norm(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def main() -> None:
    resolved = json.loads(RESOLVED.read_text(encoding="utf-8")) if RESOLVED.exists() else []
    old = json.loads(CATALOG.read_text(encoding="utf-8")) if CATALOG.exists() else []
    votes = json.loads(VOTES.read_text(encoding="utf-8")) if VOTES.exists() else {}
    by_title = {norm(m.get("title")): m for m in old}

    # dedupe resolved by video id
    unique, seen = [], set()
    for r in resolved:
        url = r.get("url")
        if not url:
            continue
        vid = url.split("v=")[-1].split("&")[0]
        if vid in seen:
            continue
        seen.add(vid)
        unique.append((vid, r))
    print(f"{len(unique)} unique official films · {len(old)} old catalog rows", flush=True)

    # parallel Wikipedia enrichment
    enriched = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(curator.wiki_lookup, r["title"]): (vid, r)
                   for vid, r in unique}
        for n, fut in enumerate(as_completed(futures), 1):
            vid, r = futures[fut]
            try:
                w = fut.result() or {}
                # Guard against Wikipedia matching the wrong film (e.g.
                # "E.M.P. 333 Days" → "Haridas"). The page title must actually
                # resemble what YouTube's storefront called it.
                if w:
                    a, b = norm(w.get("title") or ""), norm(futures[fut][1]["title"])
                    ratio = difflib.SequenceMatcher(None, a, b).ratio()
                    if ratio < 0.45:
                        w = {}
                enriched[vid] = w
            except Exception:  # noqa: BLE001
                enriched[vid] = {}
            if n % 50 == 0:
                print(f"  enriched {n}/{len(unique)}", flush=True)

    rows = []
    for vid, r in unique:
        title = r["title"]
        w = enriched.get(vid) or {}
        prev = by_title.get(norm(title), {})
        v = (votes.get("votes") or {}).get(vid, {})
        ok, fake = v.get("ok", 0), v.get("fake", 0)
        rows.append({
            "title": w.get("title") or title,
            "youtube_url": f"https://www.youtube.com/watch?v={vid}",
            "video_id": vid,
            "genre": ", ".join(w.get("genres") or []) or prev.get("source", ""),
            "year": w.get("year") or r.get("year") or prev.get("year"),
            "rating": ok - fake,
            "duration": w.get("runtime") or prev.get("duration") or 0,
            "availability": "public" if w.get("url") else "unknown",
            "date_added": int(time.time()),
            "match_score": r.get("score", 0),
            "match_reason": r.get("why", "official-storefront"),
            "poster": w.get("poster") or "",
            "status": "fake" if fake > ok else "ok",
            "source": r.get("channel") or "YouTube Movies & TV",
            "wiki": w.get("url") or "",
        })

    con = sqlite3.connect(DB)
    con.execute("DROP TABLE IF EXISTS movies")
    con.execute("""CREATE TABLE movies (
        title TEXT NOT NULL, youtube_url TEXT PRIMARY KEY, video_id TEXT,
        genre TEXT, year INTEGER, rating INTEGER DEFAULT 0, duration INTEGER,
        availability TEXT, date_added INTEGER, match_score REAL, match_reason TEXT,
        poster TEXT, status TEXT, source TEXT, wiki TEXT)""")
    con.executemany("""INSERT INTO movies
      (title,youtube_url,video_id,genre,year,rating,duration,availability,
       date_added,match_score,match_reason,poster,status,source,wiki)
      VALUES (:title,:youtube_url,:video_id,:genre,:year,:rating,:duration,
              :availability,:date_added,:match_score,:match_reason,:poster,
              :status,:source,:wiki)""", rows)
    con.commit()

    # stats
    n_year = con.execute("SELECT COUNT(*) FROM movies WHERE year IS NOT NULL").fetchone()[0]
    n_genre = con.execute("SELECT COUNT(*) FROM movies WHERE genre != ''").fetchone()[0]
    n_poster = con.execute("SELECT COUNT(*) FROM movies WHERE poster != ''").fetchone()[0]
    con.close()

    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ DB: {len(rows)} films · {n_year} with year · {n_genre} with genre · "
          f"{n_poster} with poster → {DB.name}", flush=True)


if __name__ == "__main__":
    main()
