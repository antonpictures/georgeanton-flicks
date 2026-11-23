#!/usr/bin/env python3
"""Resolve a list of movie titles to official YouTube "Free with ads" URLs.

George's 498 titles from the YouTube Movies storefront → a two-column CSV:

    Title,YouTube URL
    Tremors,https://www.youtube.com/watch?v=...

Matching is scored, not "first result":
  + feature-length duration (60-240 min)
  + official movie channels ("Movies with subtitles", "YouTube Movies", studios)
  + title similarity to the requested name
  + correct release year in the title/description
  - review / clip / trailer / reaction / fan-edit keywords
  - obviously different runtime

Inputs:
  titles.txt              one "Title (YEAR)" per line (498 titles)
  youtube_free_movies.json  official URLs already scraped from the storefront DOM
Outputs:
  youtube_movies_with_links.csv   Title,YouTube URL
  youtube_movies_resolved.json    full detail for review
"""
from __future__ import annotations

import csv
import difflib
import json
import re
import subprocess
from pathlib import Path

YTDLP = "/Users/ioanganton/.local/bin/yt-dlp"
HERE = Path(__file__).resolve().parent
TITLES = HERE / "titles.txt"
OFFICIAL = HERE / "youtube_free_movies.json"
OUT_CSV = HERE / "youtube_movies_with_links.csv"
OUT_JSON = HERE / "youtube_movies_resolved.json"

# Channels that host YouTube's own "Free with ads" movie catalog
OFFICIAL_CHANNELS = (
    "movies with subtitles", "youtube movies", "movies & tv", "yt movies",
    "free movies", "movies anytime", "hollywood movies", "popcornflix",
    "universal pictures", "lionsgate movies", "paramount pictures",
    "warner bros", "sony pictures", "disney", "mgm", "shout! studios",
    "shout! factory", "welk", "gravitas", "vertical entertainment",
    "screen media", "cinedigm", "mubi", "criterion", "pbspicturehouse",
    "kino lorber", "millennium", "-starz", "amc", "bbc",
)
BAD = re.compile(
    r"review|reaction|recap|explained|clip|trailer|teaser|behind the scenes|"
    r"bloopers|spoiler|ending|facts|breakdown|tamil|telugu|hindi dubbed|"
    r"full movie in hindi|korean|subtitle|fan ?made|edit|compilation|shorts",
    re.I,
)


def search(title: str, count: int = 8) -> list[dict]:
    try:
        p = subprocess.run(
            [YTDLP, "--no-warnings", "--no-update", "--skip-download",
             "--flat-playlist", "-J", f"ytsearch{count}:{title} full movie"],
            capture_output=True, text=True, timeout=60,
        )
        d = json.loads(p.stdout)
        entries = d.get("entries") or []
        return [e for e in entries if e and e.get("id")]
    except Exception:  # noqa: BLE001
        return []


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def score(title: str, year: int | None, e: dict) -> tuple[float, str]:
    t = norm(title)
    cand = norm(e.get("title") or "")
    ch = (e.get("channel") or "").lower()
    dur = e.get("duration") or 0
    s = 0.0
    why = []

    # title similarity
    ratio = difflib.SequenceMatcher(None, t, cand).ratio()
    s += ratio * 40
    if t and t in cand:
        s += 25; why.append("title-exact")
    elif cand and cand in t:
        s += 20; why.append("title-contained")

    # duration: a feature film, not a clip
    if 3600 <= dur <= 14400:      # 60-240 min
        s += 25; why.append("feature")
    elif 1800 <= dur < 3600:
        s += 8
    elif dur and dur < 900:
        s -= 30; why.append("short")

    # official / studio channel
    if any(o in ch for o in OFFICIAL_CHANNELS):
        s += 35; why.append("official-channel")

    # year match
    if year:
        if str(year) in (e.get("title") or ""):
            s += 12; why.append("year")
        elif re.search(r"20[12]\d", e.get("title") or ""):
            s -= 5

    # bad keywords
    if BAD.search(e.get("title") or ""):
        s -= 35; why.append("bad-keyword")

    return s, ",".join(why)


def load_official() -> dict:
    if not OFFICIAL.exists():
        return {}
    data = json.loads(OFFICIAL.read_text(encoding="utf-8"))
    return {norm(m["title"]): m["url"] for m in data}


def main() -> None:
    official = load_official()
    rows = [l.strip() for l in TITLES.read_text(encoding="utf-8").splitlines() if l.strip()]
    results = []
    seen_ids, seen_titles = set(), set()

    print(f"resolving {len(rows)} titles ({len(official)} official URLs known)", flush=True)

    for i, line in enumerate(rows):
        m = re.match(r"^(.*?)\s*\(?((?:19|20)\d{2})\)?$", line)
        if m:
            title, year = m.group(1).strip(), int(m.group(2))
        else:
            title, year = line, None
        key = norm(title)
        if key in seen_titles:
            continue
        seen_titles.add(key)

        # exact official URL from the storefront DOM — best possible
        if key in official:
            url = official[key]
            vid = url.split("v=")[-1]
            results.append({"title": title, "year": year, "url": url,
                            "score": 999, "why": "official-storefront",
                            "channel": "YouTube Movies & TV"})
            seen_ids.add(vid)
            print(f"[{i+1}/{len(rows)}] {title[:40]:<40} → OFFICIAL", flush=True)
            continue

        cands = search(title)
        best, best_score, best_why = None, -999, ""
        for e in cands:
            vid = e.get("id")
            if not vid or vid in seen_ids:
                continue
            sc, why = score(title, year, e)
            if sc > best_score:
                best, best_score, best_why = e, sc, why
        if best is None:
            results.append({"title": title, "year": year, "url": "", "score": 0,
                            "why": "no-result", "channel": ""})
            print(f"[{i+1}/{len(rows)}] {title[:40]:<40} → NO RESULT", flush=True)
            continue

        vid = best["id"]
        seen_ids.add(vid)
        url = f"https://www.youtube.com/watch?v={vid}"
        results.append({"title": title, "year": year, "url": url,
                        "score": round(best_score, 1), "why": best_why,
                        "channel": best.get("channel") or "",
                        "found_title": best.get("title") or "",
                        "duration": best.get("duration") or 0})
        print(f"[{i+1}/{len(rows)}] {title[:38]:<38} → {vid} "
              f"({best_score:.0f} {best_why[:24]})", flush=True)

    OUT_CSV.write_text(
        "Title,YouTube URL\n" + "\n".join(
            f'"{r["title"].replace(chr(34), chr(34)*2)}","{r["url"]}"' for r in results
        ) + "\n", encoding="utf-8")
    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for r in results if r["url"])
    print(f"\n✓ {ok}/{len(results)} resolved → {OUT_CSV.name} + {OUT_JSON.name}", flush=True)


if __name__ == "__main__":
    main()
