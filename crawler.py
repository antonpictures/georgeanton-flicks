#!/usr/bin/env python3
"""Alice Movie Curator — crawls YouTube for full movies and maintains a catalog.

The "Alice bot": searches YouTube, uses Nemotron (local Ollama) to identify
movie titles from video titles, dedupes against the existing catalog, and
marks videos as unavailable when YouTube blocks them.

Usage:
    python3 crawler.py          # run once (add/update)
    python3 crawler.py --full   # full rescan (re-scrape, ~minutes)

Output: movies_catalog.json — [{id, title, duration_sec, found_at, blocked}]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

sys.path.insert(0, "/Users/ioanganton/.local/share/autoclip/venv/lib/python3.13/site-packages")
import httpx  # type: ignore

_REPO = Path(__file__).resolve().parent
CATALOG = _REPO / "movies_catalog.json"

# These uploader channels are the active "movie library" sources per George.
WATCH_CHANNELS = [
    "PAIN EDIXX",
    "moviez play",
    "TIGER MOVIEZ1",
    "Eliteplays-f4z",
    "Oggy PlayzLive",
    "jr annu 03",
]

# Genre/keyword searches for broader discovery
GENRE_QUERIES = [
    "ytsearch3:full movie 2025 action",
    "ytsearch3:full movie action english",
    "ytsearch3:horror movie full",
]

# Use Nemotron (local Ollama) as the "Alice" judge
OLLAMA = "http://127.0.0.1:11434/v1/chat/completions"
NEMOTRON = "nemotron-3.5-bolt:e4b"


@dataclass
class Movie:
    id: str
    title: str
    duration: int
    source: str = ""
    found_at: float = field(default_factory=time.time)
    blocked: bool = False
    tried: int = 0


def load_catalog() -> dict[str, Movie]:
    if CATALOG.exists():
        data = json.loads(CATALOG.read_text(encoding="utf-8"))
        return {m["id"]: Movie(**m) for m in data}
    return {}


def save_catalog(catalog: dict[str, Movie]) -> None:
    CATALOG.write_text(json.dumps(
        [asdict(m) for m in sorted(catalog.values(), key=lambda m: m.title)],
        ensure_ascii=False, indent=2), encoding="utf-8")


def _alice_identify(video_id: str, raw_title: str, source: str) -> dict:
    """Use Nemotron to extract the real movie title from a YouTube title."""
    prompt = f"""From this YouTube video title, extract the actual movie name and release year.
Video title: "{raw_title}"
Video channel: {source}
Reply with ONLY JSON: {{"title": "clean movie title", "year": 2025 or null, "is_movie": true/false, "confidence": 0.0-1.0}}
If this is not a full movie, say is_movie: false."""
    try:
        r = httpx.post(OLLAMA, json={
            "model": NEMOTRON, "prompt": prompt,
            "stream": False, "options": {"num_predict": 150},
            "chat_template_kwargs": {"reasoning_effort": 0},
        }, timeout=60)
        d = r.json()
        text = d.get("response", "").strip()
        # Extract JSON
        m = re.search(r'\{[^{}]*\}', text, re.S)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        print(f"  ⚠ identify failed: {e}", file=sys.stderr)
    return {"title": raw_title, "year": None, "is_movie": True, "confidence": 0.5}


def yt_search(query: str, count: int = 5) -> list[dict]:
    """Search YouTube via yt-dlp (no API key needed)."""
    try:
        out = subprocess.run(
            [
                "/Users/ioanganton/.local/bin/yt-dlp", "--no-warnings", "--no-update",
                "--skip-download", "--flat-playlist", "-J", query,
            ],
            capture_output=True, text=True, timeout=60,
        )
        d = json.loads(out.stdout)
        entries = d.get("entries", [d]) if isinstance(d, dict) else d
        return [e for e in entries if e.get("id")][:count]
    except Exception as e:
        print(f"  ⚠ search failed: {e}", file=sys.stderr)
        return []


def check_region(video_id: str) -> tuple[bool, str]:
    """Check if YouTube is serving this video (region check via yt-dlp)."""
    try:
        out = subprocess.run(
            ["/Users/ioanganton/.local/bin/yt-dlp", "--no-warnings", "--no-update",
             "--skip-download", "--flat-playlist", "--dump-single-json",
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=30,
        )
        d = json.loads(out.stdout)
        # Check for typical YouTube error messages
        error = d.get("_download_erro", None) or d.get("error", "")
        if error:
            return False, str(error)[:80]
        # Check "video unavailable in your region" pattern
        stderr = out.stderr.lower()
        if "region" in stderr or "unavailable" in stderr:
            return False, "regional restriction"
        return True, "ok"
    except Exception as e:
        return False, str(e)[:80]


def main(full: bool = False) -> None:
    catalog = load_catalog()

    # Collect candidates
    candidates: list[dict] = []
    seen: set[str] = set(catalog.keys())

    def add_candidate(vid: str, title: str, source: str):
        if vid not in seen:
            seen.add(vid)
            candidates.append({"id": vid, "title": title, "source": source})

    print(f"Starting catalog scan — {len(catalog)} existing movies")

    # 1. Search uploader channels (active movie library)
    for ch in WATCH_CHANNELS:
        print(f"\n→ Searching: {ch}")
        try:
            entries = yt_search(f"ytsearch20:{ch}", count=15)
            for e in entries:
                add_candidate(e.get("id", ""), e.get("title", "Unknown"), ch)
        except Exception as ex:
            print(f"  ⚠ channel search failed: {ex}")

    # 2. Genre searches for broader discovery
    if full:
        for q in GENRE_QUERIES:
            print(f"\n→ Genre search: {q}")
            try:
                entries = yt_search(q, count=5)
                for e in entries:
                    add_candidate(e.get("id", ""), e.get("title", "Unknown"), "discovery")
            except Exception as ex:
                print(f"  ⚠ search failed: {ex}")

    print(f"\n→ {len(candidates)} candidates to process")

    # 3. Alice identifies movies
    for i, cand in enumerate(candidates):
        vid, raw_title = cand["id"], cand["title"]
        src = cand.get("source", "")

        # Check region (YouTube availability)
        ok, reason = check_region(vid)
        if not ok:
            print(f"  ✗ [{i+1}/{len(candidates)}] {vid[:8]} — blocked: {reason[:40]} ({raw_title[:40]})")
            continue

        # Identify the movie
        try:
            info = _alice_identify(vid, raw_title, src)
            print(f"  + [{i+1}/{len(candidates)}] {vid[:8]} — '{info.get('title','')[:40]}' ({info.get('year','')}) ({src})")
            if info.get("is_movie", True) and info.get("title"):
                movie = Movie(
                    id=vid, title=info["title"], duration=int(info.get("duration") or 0),
                    source=src,
                )
                catalog[vid] = movie
                if i % 5 == 0:
                    save_catalog(catalog)
        except Exception as ex:
            print(f"  ⚠ [{i+1}/{len(candidates)}] {vid[:8]} — error: {ex}")

    # 4. Update: try blocked/updated movies
    print(f"\n→ Checking existing catalog ({len(catalog)} movies) for updates")
    for vid, movie in list(catalog.items()):
        if movie.tried >= 2:
            continue
        ok, _ = check_region(vid)
        if not ok and not movie.blocked:
            movie.blocked = True
            movie.tried += 1
            print(f"  ⚠ {vid[:8]} — now blocked")

    save_catalog(catalog)
    print(f"\n✓ Catalog saved: {len(catalog)} movies in {CATALOG}")
    if catalog:
        for m in sorted(catalog.values(), key=lambda m: m.title)[:10]:
            print(f"   - {m.title} ({m.duration//60 if m.duration else '?'}min)")


if __name__ == "__main__":
    full = "--full" in sys.argv
    main(full=full)
