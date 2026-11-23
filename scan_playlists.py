#!/usr/bin/env python3
"""Scan all of George's playlists for full-movie candidates."""
import json
import subprocess
import sys
import time
from pathlib import Path

YTDLP = "/Users/ioanganton/.local/bin/yt-dlp"
HERE = Path(__file__).resolve().parent
OUT = HERE / "anton_playlists_urls.txt"

# Fetch the channel's playlist list
print("Fetching playlist list...", flush=True)
out = subprocess.run(
    [YTDLP, "--no-warnings", "--no-update", "--flat-playlist", "-J",
     "https://www.youtube.com/@antonpictures/playlists"],
    capture_output=True, text=True, timeout=120,
)
playlists = json.loads(out.stdout).get("entries") or []
print(f"Found {len(playlists)} playlists", flush=True)

JUNK = ("shorts", "clip", "interview", "trailer", "teaser", "behind the scenes",
        "bloopers", "highlight", "compilation", "live stream", "podcast")

all_ids = []
seen = set()
for i, pl in enumerate(playlists):
    url = pl.get("url") or f"https://www.youtube.com/playlist?list={pl.get('id')}"
    title = pl.get("title") or "?"
    print(f"\n[{i+1}/{len(playlists)}] {title} ({url})", flush=True)
    try:
        r = subprocess.run(
            [YTDLP, "--no-warnings", "--no-update", "--flat-playlist", "-J",
             "--playlist-end", "200", url],
            capture_output=True, text=True, timeout=180,
        )
        d = json.loads(r.stdout)
    except Exception as e:  # noqa: BLE001
        print(f"  failed: {e}", flush=True)
        continue
    entries = d.get("entries") or []
    kept = 0
    for e in entries:
        if not e or not e.get("id"):
            continue
        vid = e["id"]
        if vid in seen:
            continue
        dur = int(e.get("duration") or 0)
        t = (e.get("title") or "").lower()
        if dur < 20 * 60:          # not a feature film
            continue
        if any(j in t for j in JUNK):
            continue
        seen.add(vid)
        all_ids.append(vid)
        kept += 1
    print(f"  {len(entries)} entries, kept {kept} full-movie candidates", flush=True)

OUT.write_text("".join(f"https://www.youtube.com/watch?v={v}\n" for v in all_ids))
print(f"\n✓ collected {len(all_ids)} unique full-movie candidates → {OUT}", flush=True)
PY