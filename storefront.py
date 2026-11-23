#!/usr/bin/env python3
"""Pull the titles out of a YouTube storefront page ("Free with ads" etc.)."""
import json
import re
import sys
import urllib.request

sys.path.insert(0, '/Users/ioanganton/Music/ANTON_SIFTA/scratch/movie-clone')
import discover, curator

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")


def storefront_videos(url, limit=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    dec = json.JSONDecoder()
    for m in re.finditer(r"ytInitialData\s*=\s*", html):
        try:
            data, _ = dec.raw_decode(html[m.end():])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data:
            recs = discover.recommendations(data)
            if recs:
                return recs[:limit]
    return []


if __name__ == '__main__':
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/feed/storefront?bp=ogUCKAY%3D"
    recs = storefront_videos(url)
    cat = curator.load()
    added = 0
    print(f"storefront: {len(recs)} videos")
    for r in recs:
        if curator.add_movie(cat, r["id"], r["title"], r.get("channel") or "storefront",
                             r["duration"]):
            m = cat[r["id"]]
            print(f"  + {m.title} ({m.year or '?'}) [{r['duration']//60}m]")
            added += 1
    curator.save(cat)
    print(f"added {added} · library {len(cat)}")
