#!/usr/bin/env python3
"""Build the authoritative movie database.

Merges three sources into one clean SQLite DB + JSON for the site:
  1. youtube_movies_resolved.json  — official/studio YouTube URLs, scored matches
  2. Wikipedia (via curator)       — real title, year, genre, runtime, poster
  3. movies_catalog.json + votes   — viewer ranking (OK lifts, FAKE sinks)

Output:
  georgeanton_movies.db   SQLite — title, url, genre, year, rating, duration,
                          availability, date_added, score, why, poster, status
  verified_catalog.json   the site's shelf, rebuilt from verified data only
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
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

    print(f"resolver: {len(resolved)} titles · old catalog: {len(old)}", flush=True)

    rows = []
    seen = set()
    for i, r in enumerate(resolved):
        url = r.get("url")
        if not url:
            continue
        vid = url.split("v=")[-1].split("&")[0]
        if vid in seen:
            continue
        seen.add(vid)

        title = r["title"]
        key = norm(title)
        prev = by_title.get(key, {})

        # enrich with Wikipedia once per film (cheap: search + summary)
        w = {}
        try:
            w = curator.wiki_lookup(title) or {}
        except Exception:  # noqa: BLE001
            pass

        v = (votes.get("votes") or {}).get(vid, {})
        ok, fakev = v.get("ok", 0), v.get("fake", 0)
        rating = ok - fakev

        rows.append({
            "title": w.get("title") or title,
            "youtube_url": url,
            "video_id": vid,
            "genre": ", ".join(w.get("genres") or []) or prev.get("source", ""),
            "year": w.get("year") or r.get("year") or prev.get("year"),
            "rating": rating,
            "duration": w.get("runtime") or prev.get("duration") or 0,
            "availability": (w.get("url") and "public") or "unknown",
            "date_added": int(time.time()),
            "match_score": r.get("score", 0),
            "match_reason": r.get("why", ""),
            "poster": w.get("poster") or "",
            "status": "fake" if fakev > ok else "ok",
            "source": r.get("channel") or "YouTube Movies & TV",
            "wiki": w.get("url") or "",
        })
        if (i + 1) % 25 == 0:
            print(f"  enriched {i+1}/{len(resolved)}", flush=True)

    # --- SQLite ---
    con = sqlite3.connect(DB)
    con.execute("DROP TABLE IF EXISTS movies")
    con.execute("""
      CREATE TABLE movies (
        title TEXT NOT NULL,
        youtube_url TEXT PRIMARY KEY,
        video_id TEXT,
        genre TEXT,
        year INTEGER,
        rating INTEGER DEFAULT 0,
        duration INTEGER,
        availability TEXT,
        date_added INTEGER,
        match_score REAL,
        match_reason TEXT,
        poster TEXT,
        status TEXT,
        source TEXT,
        wiki TEXT
      )""")
    con.executemany("""INSERT INTO movies
      (title,youtube_url,video_id,genre,year,rating,duration,availability,
       date_added,match_score,match_reason,poster,status,source,wiki)
      VALUES (:title,:youtube_url,:video_id,:genre,:year,:rating,:duration,
              :availability,:date_added,:match_score,:match_reason,:poster,
              :status,:source,:wiki)""", rows)
    con.commit()
    con.close()

    # --- JSON for the site ---
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in rows if r["status"] == "ok")
    print(f"\n✓ database: {len(rows)} films ({ok} playable) → {DB.name} + {OUT.name}", flush=True)


if __name__ == "__main__":
    main()
