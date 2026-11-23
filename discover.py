#!/usr/bin/env python3
"""Alice's recommendation walker — the supply graph.

George's observation (2026-11-22): open one full movie on YouTube and the
"up next" column is a wall of full movies from channels nobody curates:
Mundo Droid, Hollywood English Collection, We Got Movies, Underground Cut,
Nouvelle Vague, Movie Storm, Iron Crest Films, Nik, EviL, Sara Fayruz, ...
That column *is* the map of the market. So we walk it.

How it works:
  1. Fetch a watch page, read `ytInitialData`, pull `compactVideoRenderer`
     nodes out of `secondaryResults` (the recommendations column).
  2. Keep only feature-length items (>= 20 min).
  3. Harvest the owning channel handle too, so the uploader network grows
     itself instead of being hardcoded.
  4. Breadth-first: every accepted video becomes a new seed, up to --depth.
     Dedupe against the catalog so a run is cheap.

Usage:
    python3 discover.py --seed XYDe_6RuiKQ            # one seed, depth 2
    python3 discover.py --seed XYDe_6RuiKQ --depth 3 --max 120
    python3 discover.py --seeds seeds.txt             # one video id per line
    python3 discover.py --from-catalog                # seed from the library

Writes:
    movies_catalog.json   (merged through curator.add_movie)
    channels.json         discovered uploaders: handle, name, seen count
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from collections import Counter, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import curator  # noqa: E402

HERE = Path(__file__).resolve().parent
CHANNELS = HERE / "channels.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")


def watch_page(vid: str) -> dict | None:
    url = f"https://www.youtube.com/watch?v={vid}&hl=en&gl=US"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "CONSENT=YES+1; SOCS=CAI",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", html, re.S)
    if not m:
        m = re.search(r'window\["ytInitialData"\]\s*=\s*(\{.*?\});', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _text(node) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    if "runs" in node:
        return "".join(r.get("text", "") for r in node["runs"])
    return ""


def _seconds(txt: str) -> int:
    parts = [p for p in txt.strip().split(":") if p.isdigit()]
    if not parts:
        return 0
    s = 0
    for p in parts:
        s = s * 60 + int(p)
    return s


TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def _lockup(v: dict) -> dict | None:
    """New YouTube UI (2026) puts the recommendations column in lockupViewModel."""
    vid = v.get("contentId") or ""
    title = ((v.get("metadata") or {}).get("lockupMetadataViewModel") or {}).get("title", {}) or {}
    title = title.get("content", "") if isinstance(title, dict) else str(title)
    img = json.dumps(v.get("contentImage") or {})
    if not vid:
        m = re.search(r"/vi/([\w-]{11})/", img)
        if m:
            vid = m.group(1)
    if not vid:
        return None

    # duration badge
    dur_txt = ""
    for badge in re.findall(r'"thumbnailBadgeViewModel":\s*\{"text":\s*"([^"]+)"', img):
        if TIME_RE.match(badge):
            dur_txt = badge
            break
    if not dur_txt:
        m = re.search(r'"text":\s*"(\d{1,2}:\d{2}(?::\d{2})?)"', img)
        if m:
            dur_txt = m.group(1)

    # channel name + handle
    blob = json.dumps(v)
    handle = ""
    m = re.search(r'"canonicalBaseUrl":\s*"(/@[\w.\-]+)"', blob)
    if m:
        handle = m.group(1)
    channel = ""
    for label in ("accessibilityContext",):
        for m in re.finditer(r'"%s":\s*\{\s*"label":\s*"([^"]{3,80})"' % label, blob):
            cand = m.group(1)
            if " - " in cand:
                channel = cand.split(" - ")[0].strip()
                break
        if channel:
            break
    if not channel:
        # metadata rows: "Channel • 1.2M views • 3 weeks ago"
        for m in re.finditer(r'"content":\s*"([^"]{2,60})"', blob):
            t = m.group(1)
            if ("views" in t or "ago" in t or TIME_RE.match(t)
                    or t.lower() in ("new", "live")):
                continue
            channel = t
            break

    channel = (channel or "").split("|")[0].strip()
    if not channel or channel.lower() == (title or "").lower():
        channel = handle.lstrip("@") or "recommended"
    return {"id": vid, "title": title, "duration": _seconds(dur_txt),
            "channel": channel, "handle": handle}


def recommendations(data: dict) -> list[dict]:
    """Pull the 'up next' column out of ytInitialData (legacy + 2026 layouts)."""
    found: list[dict] = []
    seen: set[str] = set()

    def push(row: dict | None):
        if not row or not row.get("id") or row["id"] in seen:
            return
        seen.add(row["id"])
        found.append(row)

    def walk(node):
        if isinstance(node, dict):
            if "lockupViewModel" in node and isinstance(node["lockupViewModel"], dict):
                push(_lockup(node["lockupViewModel"]))
            for key in ("compactVideoRenderer", "videoRenderer", "gridVideoRenderer"):
                if key in node and isinstance(node[key], dict):
                    v = node[key]
                    vid = v.get("videoId")
                    if vid:
                        length = _text(v.get("lengthText"))
                        if not length:
                            ov = v.get("thumbnailOverlayTimeStatusRenderer") or {}
                            length = _text(ov.get("text")) if isinstance(ov, dict) else ""
                        handle = ""
                        for holder in (v.get("shortBylineText"), v.get("longBylineText"),
                                       v.get("ownerText")):
                            if not isinstance(holder, dict):
                                continue
                            for run in holder.get("runs", []):
                                url = (run.get("navigationEndpoint", {})
                                          .get("browseEndpoint", {})
                                          .get("canonicalBaseUrl", ""))
                                if url.startswith("/@"):
                                    handle = url
                                    break
                            if handle:
                                break
                        push({"id": vid,
                              "title": _text(v.get("title")) or _text(v.get("headline")),
                              "duration": _seconds(length),
                              "channel": (_text(v.get("shortBylineText"))
                                          or _text(v.get("ownerText"))),
                              "handle": handle})
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    # Only the recommendations column, when we can find it — otherwise all.
    recs_root = None
    try:
        walk(data)
    except RecursionError:
        pass
    return found



def load_channels() -> dict:
    if CHANNELS.exists():
        try:
            return json.loads(CHANNELS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_channels(ch: dict) -> None:
    rows = sorted(ch.values(), key=lambda c: -c["seen"])
    CHANNELS.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def walk(seeds: list[str], depth: int = 2, max_videos: int = 80,
         delete_junk: bool = True) -> None:
    cat = curator.load()
    channels = load_channels()
    queue = deque((vid, 0) for vid in seeds)
    visited: set[str] = set(cat.keys())
    added = 0
    pages = 0

    print(f"walking recommendations — {len(seeds)} seeds, depth {depth}, cap {max_videos}",
          flush=True)

    # The seeds themselves are films too.
    for vid in seeds:
        if vid in cat:
            continue
        out, _ = curator.run([curator.YTDLP, "--no-warnings", "--no-update",
                              "--skip-download", "--print", "%(title)s",
                              "--print", "%(duration)s",
                              f"https://www.youtube.com/watch?v={vid}"], 60)
        lines = out.splitlines()
        if len(lines) >= 2 and lines[0].strip():
            title, dur = lines[0].strip(), lines[1].strip()
            if curator.add_movie(cat, vid, title,
                                 "seed" if not cat else "seed",
                                 int(dur) if dur.isdigit() else 0):
                m = cat[vid]
                added += 1
                print(f"    + seed {vid} · {m.title} ({m.year or '?'})", flush=True)

    while queue and added < max_videos:
        vid, level = queue.popleft()
        if vid in visited:
            continue
        visited.add(vid)
        data = watch_page(vid)
        pages += 1
        if not data:
            print(f"  ! {vid}: page read failed", flush=True)
            continue
        recs = recommendations(data)
        print(f"  · {vid} → {len(recs)} recommendations (level {level})", flush=True)

        for r in recs:
            h = r.get("handle") or ""
            if h:
                name = r.get("channel") or h
                row = channels.setdefault(h, {"handle": h, "name": name, "seen": 0,
                                              "first_seen": time.time()})
                row["seen"] += 1
                row["name"] = row.get("name") or name

            if not r["duration"] or r["duration"] < curator.MIN_SECONDS:
                continue
            if delete_junk and curator.JUNK.search(r["title"] or ""):
                continue
            if r["id"] in visited and r["id"] in cat:
                continue

            if r["id"] not in cat:
                if curator.add_movie(cat, r["id"], r["title"], r.get("channel") or "recommended",
                                     r["duration"], deep=False):
                    m = cat[r["id"]]
                    added += 1
                    print(f"    + {r['id']} · {m.title} ({m.year or '?'}) "
                          f"[{r['duration']//60}m · {m.source}]", flush=True)
                    if added % 5 == 0:
                        curator.save(cat)
                        save_channels(channels)
                    if added >= max_videos:
                        break

            if level < depth and r["id"] not in visited:
                queue.append((r["id"], level + 1))

        if pages % 10 == 0:
            curator.save(cat)
            save_channels(channels)

    curator.save(cat)
    save_channels(channels)
    print(f"\n✓ walked {pages} pages · +{added} films · "
          f"{len(channels)} uploaders mapped · library {len(cat)}", flush=True)


def main() -> None:
    argv = sys.argv[1:]
    seeds: list[str] = []
    depth = 2
    cap = 80

    for i, a in enumerate(argv):
        if a == "--seed" and i + 1 < len(argv):
            seeds.append(argv[i + 1])
        elif a == "--seeds" and i + 1 < len(argv):
            seeds += [l.strip() for l in Path(argv[i + 1]).read_text().splitlines()
                      if l.strip() and not l.startswith("#")]
        elif a == "--depth" and i + 1 < len(argv):
            depth = int(argv[i + 1])
        elif a == "--max" and i + 1 < len(argv):
            cap = int(argv[i + 1])

    if "--from-catalog" in argv:
        cat = curator.load()
        seeds += [m.id for m in cat.values() if m.status == "ok"][:10]

    if not seeds:
        seeds = ["XYDe_6RuiKQ"]  # Escape Plan, Pelis Center — George's seed

    walk(list(dict.fromkeys(seeds)), depth=depth, max_videos=cap)


if __name__ == "__main__":
    main()
