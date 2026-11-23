#!/usr/bin/env python3
"""Alice Movie Curator — keeps a YouTube "full movie" library honest.

What it does (the Alice job George asked for):
  1. Walk the uploader channels that actually host full movies (Pelis Center,
     moviez play, TIGER MOVIEZ, ...) and every video they uploaded.
  2. Throw away everything that is not a feature film (games, trailers, IPTV
     tutorials, DVD menus) using duration + a blocklist.
  3. Fix the titles: clean the YouTube junk, then confirm the real film on
     Wikipedia (title / year / genre / runtime). The LLM is only the fallback.
  4. Health-check the whole catalog every run: a video YouTube removed or
     region-blocked gets marked, not silently deleted.
  5. Record availability (public / unlisted / region) so the player can skip
     what will not play.

Usage:
    python3 curator.py                 # full run: ingest + health-check
    python3 curator.py --no-ingest     # only health-check the catalog
    python3 curator.py --add "Escape Plan"   # force-add a search
    python3 curator.py --deep          # mine comments for unsure titles (slow)

Outputs:
    movies_catalog.json   the library (consumed by the site)
    curator_state.json    run log / stats
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

YTDLP = "/Users/ioanganton/.local/bin/yt-dlp"
OLLAMA = "http://127.0.0.1:11434"
LLM_MODEL = "nemotron-3.5-bolt:e4b"

REPO = Path(__file__).resolve().parent
CATALOG = REPO / "movies_catalog.json"
STATE = REPO / "curator_state.json"

# --- the uploader network -------------------------------------------------
# These are channels George pointed at: they publish full movies, often
# hours-long, and they keep publishing. That is the raw supply.
CHANNELS: list[dict] = [
    {"handle": "@peliscenterhd",  "name": "Pelis Center"},
    {"handle": "@PAIN-EDIXX",     "name": "PAIN EDIXX"},
    {"handle": "@moviez-play",    "name": "moviez play"},
    {"handle": "@TIGERMOVIEZ1",   "name": "TIGER MOVIEZ"},
    {"handle": "@jr_annu_03",     "name": "jr annu 03"},
    {"handle": "@Eliteplays-f4z", "name": "Elite plays"},
    {"handle": "@OggyPlayzLive",  "name": "Oggy playz"},
]

# Discovery queries — used when channels dry up (YouTube purges them).
DISCOVERY = [
    "ytsearch8:full movie english 2025",
    "ytsearch8:full movie action 2026",
]

MIN_SECONDS = 20 * 60          # shorter than this is not a feature film
MAX_SECONDS = 5 * 60 * 60      # longer than this is a loop / livestream

# Anything whose title smells like this is refused outright.
JUNK = re.compile(
    r"\b(gameplay|gta|minecraft|roblox|counter blox|counter-strike|fortnite|"
    r"iptv|android tv|install apps|smartphone|apk|vpn|tutorial|"
    r"dvd menu|menu play|play sequences|video game|walkthrough|"
    r"trailer|teaser|clip|behind the scenes|bloopers|interview|"
    r"full episode|episode \d|season \d|web ?series|music video|"
    r"lyrics|song|podcast|reaction|compilation|shorts|live ?stream|"
    r"highlights|goals|vs\.?|list of|episodes?|franchise|season \d|"
    r"s\d+e\d+|dvd|soundtrack|audiobook|asmr)\b",
    re.I,
)


@dataclass
class Movie:
    id: str
    title: str
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    duration: int = 0              # seconds, from YouTube metadata
    runtime: int | None = None     # minutes, from Wikipedia
    source: str = ""               # uploader channel
    raw_title: str = ""
    status: str = "ok"             # ok | blocked | restricted | removed
    availability: str = "public"
    checked_at: float = field(default_factory=time.time)
    added_at: float = field(default_factory=time.time)
    confidence: float = 0.0
    notes: str = ""
    regions: list[str] = field(default_factory=list)   # countries where it plays
    region_checked: float = 0.0
    poster: str | None = None


# === plumbing =============================================================
def run(cmd: list[str], timeout: int = 90) -> tuple[str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return "", "timeout"
    except Exception as e:  # noqa: BLE001
        return "", str(e)


def yt_json(args: list[str], timeout: int = 120) -> dict | list | None:
    out, _ = run([YTDLP, "--no-warnings", "--no-update", "--skip-download", *args], timeout)
    out = out.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # yt-dlp sometimes prints several JSON objects back to back
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
    return None


def llm(prompt: str, timeout: int = 90) -> str:
    try:
        req = urllib.request.Request(
            f"{OLLAMA}/api/generate",
            data=json.dumps({
                "model": LLM_MODEL, "prompt": prompt, "stream": False,
                "options": {"temperature": 0.1, "num_predict": 200},
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r).get("response", "").strip()
    except Exception:  # noqa: BLE001
        return ""


WIKI_UA = "AliceFlicks/1.0 (georgeanton.com; contact: george@georgeanton.com)"


def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": WIKI_UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def wiki_lookup(title: str) -> dict | None:
    """Ask Wikipedia if this is a real film. Free, no key, very reliable."""
    q = urllib.parse.quote(f"{title} film")
    url = ("https://en.wikipedia.org/w/api.php?action=query&list=search"
           f"&srsearch={q}&srlimit=3&format=json")
    try:
        hits = _get_json(url)["query"]["search"]
    except Exception:  # noqa: BLE001
        return None
    for hit in hits:
        page = hit.get("title", "")
        if not re.search(r"\(film\)|\(.*film\)|film$", page, re.I) and "film" not in hit.get("snippet", "").lower():
            continue
        data = wiki_page(page)
        if data:
            return data
    return None


GENRE_ORDER = ["Action", "Thriller", "Crime", "Adventure", "Horror", "Sci-Fi",
               "Mystery", "Drama", "Comedy", "Fantasy", "War", "Western",
               "Biography", "History", "Family", "Animation", "Romance",
               "Documentary", "Sport", "Musical", "Supernatural"]

GENRE_MAP = {
    "action": "Action", "adventure": "Adventure", "comedy": "Comedy",
    "crime": "Crime", "drama": "Drama", "fantasy": "Fantasy", "horror": "Horror",
    "mystery": "Mystery", "romance": "Romance", "sci-fi": "Sci-Fi",
    "science fiction": "Sci-Fi", "thriller": "Thriller", "war": "War",
    "western": "Western", "animation": "Animation", "animated": "Animation",
    "family": "Family", "documentary": "Documentary", "biography": "Biography",
    "biographical": "Biography", "history": "History", "musical": "Musical",
    "sport": "Sport", "supernatural": "Supernatural", "slasher": "Horror",
}


def wiki_infobox(page: str) -> dict:
    """Genre + runtime from the film's Wikipedia infobox."""
    q = urllib.parse.quote(page)
    url = (f"https://en.wikipedia.org/w/api.php?action=parse&page={q}"
           "&prop=wikitext&format=json&section=0&redirects=1")
    try:
        wt = _get_json(url)["parse"]["wikitext"]["*"]
    except Exception:  # noqa: BLE001
        return {}

    def field(name: str) -> str:
        m = re.search(rf"\|\s*{name}\s*=\s*(.*?)(?=\n\s*\||\n\}})", wt, re.S)
        return m.group(1).strip() if m else ""

    genres: list[str] = []
    raw = field("genre")
    # Links first: {{Plainlist|\n* [[Action film|Action]]\n* [[Thriller film|Thriller]]}}
    for label in re.findall(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", raw):
        key = label.strip().lower()
        key = re.sub(r"\s*film$", "", key)
        if key in GENRE_MAP:
            g = GENRE_MAP[key]
            if g not in genres:
                genres.append(g)
    if not genres:
        plain = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", raw)
        plain = re.sub(r"<ref.*?</ref>", "", plain, flags=re.S)
        for piece in re.split(r"[,/]|\band\b|&|[\n*{}|]", plain):
            key = piece.strip().lower()
            key = re.sub(r"\s*film$", "", key)
            if key in GENRE_MAP and GENRE_MAP[key] not in genres:
                genres.append(GENRE_MAP[key])

    runtime = None
    rm = re.search(r"(\d{2,3})\s*minutes", field("runtime"))
    if rm:
        runtime = int(rm.group(1))

    # Many film infoboxes omit |genre =. Wikipedia categories never do:
    # "2013 American action thriller films", "Films about prisons", ...
    if not genres:
        try:
            cats = _get_json(
                "https://en.wikipedia.org/w/api.php?action=query&prop=categories"
                f"&cllimit=100&clshow=!hidden&format=json&redirects=1&titles={q}"
            )
            pages = cats["query"]["pages"]
            blob = " ".join(c["title"].lower() for p in pages.values()
                            for c in p.get("categories", []))
            for rank, canon in enumerate(GENRE_ORDER):
                for key, mapped in GENRE_MAP.items():
                    if mapped == canon and re.search(rf"\b{re.escape(key)}\b", blob):
                        genres.append(canon)
                        break
            genres = genres[:4]
        except Exception:  # noqa: BLE001
            pass

    return {"genres": genres[:4], "runtime": runtime, "page": page}


def wiki_page(page: str) -> dict | None:
    q = urllib.parse.quote(page)
    url = (f"https://en.wikipedia.org/api/rest_v1/page/summary/{q}")
    try:
        d = _get_json(url)
    except Exception:  # noqa: BLE001
        return None
    title = d.get("title", page)
    extract = d.get("extract", "")
    if re.match(r"(list of|film series|.*\(franchise\))", title, re.I):
        return None
    if re.search(r"franchise|media franchise", title, re.I):
        return None
    # The opening sentence has to say it is a film. Otherwise we are looking at
    # an actor, a list, or a franchise page that merely mentions films.
    first = re.split(r"(?<!\b[A-Z])\.\s", extract, maxsplit=1)[0][:300]
    if not re.search(r"\b(film|movie)\b", first, re.I):
        return None
    # "...was an American actor who starred in the 1970 film..." is a person.
    if re.search(r"\b(actor|actress|television series|web series|born)\b", first, re.I):
        return None
    if re.search(r"\b(?:actor|actress|screenwriter|producer|director)\b\s*\(", title, re.I):
        return None
    year = None
    m = re.search(r"\b(19|20)\d{2}\b", extract)
    if m:
        year = int(m.group(0))
    box = wiki_infobox(d.get("title", page))
    poster = (d.get("originalimage") or {}).get("source") or (d.get("thumbnail") or {}).get("source")
    return {
        "title": re.sub(r"\s*\((?:19|20)\d{2}[^)]*film\)|\s*\(film\)$", "",
                        d.get("title", page)).strip(),
        "year": year,
        "extract": extract[:300],
        "genres": box.get("genres", []),
        "runtime": box.get("runtime"),
        "poster": poster,
        "url": d.get("content_urls", {}).get("desktop", {}).get("page", ""),
    }


# === title cleaning =======================================================
SMALL = {"a", "an", "the", "and", "or", "of", "in", "on", "to", "for", "with",
         "at", "by", "from", "vs", "vs.", "into", "as"}


def smart_title(s: str) -> str:
    """ALL-CAPS marketing titles look wrong on a shelf — make them readable."""
    if not s:
        return s
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return s
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    if upper_ratio < 0.75 or len(s) < 14:
        return s
    words = s.split()
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        if i and low in SMALL:
            out.append(low)
        else:
            out.append(w[:1].upper() + w[1:].lower() if w.isupper() else w)
    return " ".join(out)


# Marketing tails: "… Ultimate Action Movie Vin Diesel", "… Full Action Film 4K"
TAIL = re.compile(
    r"\s+(?:the\s+)?(?:(?:ultimate|best|new|epic|blockbuster|latest|complete)\s+)?"
    r"(?:action|horror|thriller|comedy|drama|sci-?fi|fantasy|adventure|crime|mystery|"
    r"romance|western|family|war|christmas|halloween)\s*"
    r"(?:movie|film)\b.*$",
    re.I,
)


BRACKET = re.compile(r"[【\[]([^】\]]{2,60})[】\]]")


def clean_title(raw: str) -> str:
    # 【SHELTER】 — uploaders hide the real title in brackets
    bracketed = [b.strip() for b in BRACKET.findall(raw)
                 if not re.search(r"full|hd|4k|official|trailer|sub|movie|film", b, re.I)]
    if bracketed:
        return smart_title(bracketed[-1])[:90]
    t = raw
    # Uploaders stack segments with pipes: "Film | Channel | Full Movie | Actor".
    # The first real segment is the film.
    if "|" in t:
        parts = [p.strip() for p in t.split("|") if p.strip()]
        if parts and len(parts[0]) >= 3:
            t = parts[0]
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", " ", t)      # emoji
    t = re.sub(r"#\w+", " ", t)                                        # hashtags
    t = re.sub(r"\b(full movie|full film|full hd|hd|4k|1080p|720p|"
               r"english subtitles|subtitles?|subbed|dubbed|"
               r"pelicula completa|película completa|peliculagratis|"
               r"watch online|free|new|latest|official)\b", " ", t, flags=re.I)
    t = re.sub(r"\(?(19|20)\d{2}\)?", " ", t)
    t = re.sub(r"[•·—–:]+", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -.,!")
    # Trim marketing tails ("… Ultimate Action Movie Vin Diesel")
    if len(t) > 34:
        t = TAIL.sub("", t).strip(" -.,!")
    t = smart_title(t)
    return t[:90]


def identify(raw_title: str, source: str) -> dict:
    """Return {title, year, confidence, notes}. Wikipedia first, LLM second."""
    guess = clean_title(raw_title)
    if not guess:
        guess = raw_title[:80]

    wiki = wiki_lookup(guess)
    if wiki:
        raw_words = {w for w in re.findall(r"[a-z]{3,}", raw_title.lower())}
        wiki_words = {w for w in re.findall(r"[a-z]{3,}", wiki["title"].lower())}
        overlap = len(raw_words & wiki_words)
        raw_year = None
        ym = re.search(r"(19|20)\d{2}", raw_title)
        if ym:
            raw_year = int(ym.group(0))
        # A real film has to be nameable from the upload. If the years disagree
        # (2026 upload vs. a 2025 film) or nothing overlaps, it is an AI fake
        # wearing a real film's name -> keep the cleaned upload title.
        if raw_year and wiki["year"] and abs(raw_year - wiki["year"]) > 1:
            return {"title": clean_title(raw_title), "year": raw_year,
                    "genres": [], "runtime": None, "poster": None,
                    "confidence": 0.35, "notes": "unverified"}
        # An upload that names a sequel number ("Jurassic World 6", "John Wick
        # Chapter 5") that the real film does not have is an AI fake wearing
        # a real franchise's name.
        # Only small numbers count as sequel markers; years never do.
        raw_digits = {d for d in re.findall(r"\b\d{1,3}\b", raw_title)}
        wiki_digits = {d for d in re.findall(r"\b\d{1,3}\b", wiki["title"])}
        if raw_digits and not (raw_digits & wiki_digits):
            return {"title": clean_title(raw_title), "year": raw_year,
                    "genres": [], "runtime": None, "poster": None,
                    "confidence": 0.35, "notes": "unverified"}
        if overlap < 2 and not (raw_words & wiki_words):
            return {"title": clean_title(raw_title), "year": raw_year,
                    "genres": [], "runtime": None, "poster": None,
                    "confidence": 0.35, "notes": "unverified"}
        return {"title": wiki["title"], "year": wiki["year"],
                "genres": wiki.get("genres", []), "runtime": wiki.get("runtime"),
                "poster": wiki.get("poster"), "confidence": 0.95,
                "notes": f"wiki: {wiki['url']}"}

    # LLM fallback: only trusted to tidy the string, never to invent a film.
    prompt = (
        "You are a film librarian. From this YouTube title, name the actual film.\n"
        f'YouTube title: "{raw_title}"\nChannel: {source}\n'
        'Reply with JSON only: {"title":"film name","year":2025|null,"is_film":true|false}'
    )
    raw = llm(prompt)
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if d.get("is_film") and d.get("title"):
                return {"title": str(d["title"])[:90],
                        "year": d.get("year") if isinstance(d.get("year"), int) else None,
                        "confidence": 0.6, "notes": "llm"}
        except json.JSONDecodeError:
            pass
    return {"title": guess, "year": None, "confidence": 0.3, "notes": "raw"}


# === YouTube access =======================================================
def channel_videos(handle: str, limit: int = 60) -> list[dict]:
    d = yt_json(["--flat-playlist", "-J", "--playlist-end", str(limit),
                 f"https://www.youtube.com/{handle}/videos"], timeout=180)
    if not isinstance(d, dict):
        return []
    out = []
    for e in d.get("entries") or []:
        if not e or not e.get("id"):
            continue
        out.append({
            "id": e["id"],
            "title": e.get("title") or "",
            "duration": int(e.get("duration") or 0),
        })
    return out


def probe(vid: str) -> dict:
    """One yt-dlp call: availability, duration, upload date, description.

    The description is the best film-identification signal we get for the
    all-caps marketing uploads ("THEY WERE SENTENCED TO DEATH..." — the film
    name is never in the title, but the uploader usually writes it below).
    """
    out, err = run([YTDLP, "--no-warnings", "--no-update", "--skip-download",
                    "--print", "%(availability)s",
                    "--print", "%(duration)s",
                    "--print", "%(upload_date)s",
                    "--print", "%(description)s",
                    f"https://www.youtube.com/watch?v={vid}"], timeout=60)
    blob = f"{out}\n{err}".lower()
    if not out.strip():
        if "private video" in blob:
            return {"status": "removed", "availability": "private", "description": ""}
        if "not available in your country" in blob or "region" in blob:
            return {"status": "restricted", "availability": "region", "description": ""}
        if "video unavailable" in blob or "has been removed" in blob:
            return {"status": "removed", "availability": "removed", "description": ""}
        if "age" in blob and "restrict" in blob:
            return {"status": "restricted", "availability": "age", "description": ""}
        return {"status": "blocked", "availability": "unknown", "description": ""}

    lines = out.splitlines()
    avail = (lines[0].strip() if lines else "public") or "public"
    duration = int(lines[1]) if len(lines) > 1 and lines[1].strip().isdigit() else 0
    upload = lines[2].strip() if len(lines) > 2 else ""
    desc = "\n".join(lines[3:])[:2500]
    status = "ok" if avail in ("public", "unlisted") else "restricted"
    return {"status": status, "availability": avail, "duration": duration,
            "upload_date": upload, "description": desc}


def video_health(vid: str) -> tuple[str, str]:
    p = probe(vid)
    return p["status"], p["availability"]


def identify_from_description(desc: str, raw_title: str) -> dict | None:
    """Look for an explicit film credit in the description."""
    if not desc:
        return None
    # "Movie Name (2025)" / "Film: Movie Name" / "Starring ... in Movie Name"
    patterns = [
        r"(?:movie|film|title)\s*[:\-–]\s*([A-Z][^.\n|]{2,60}?)\s*\(?((?:19|20)\d{2})\)?",
        r"([A-Z][A-Za-z0-9'’:,\- ]{2,60}?)\s*\((?:19|20)\d{2}\)\s*(?:full|movie|film)",
        r"^\s*([A-Z][A-Za-z0-9'’:,\- ]{2,60})\s*\((?:19|20)\d{2}\)",
    ]
    raw_words = {w for w in re.findall(r"[a-z]{3,}", raw_title.lower())}
    for idx, pat in enumerate(patterns):
        m = re.search(pat, desc, re.M | re.I)
        if m:
            name = m.group(1).strip(" -–:|")
            year = None
            ym = re.search(r"(19|20)\d{2}", m.group(0))
            if ym:
                year = int(ym.group(0))
            name_words = {w for w in re.findall(r"[a-z]{3,}", name.lower())}
            # A bare "X (2015) full movie" in the description is only believable
            # when the video title points at the same film (idx > 0).
            # The claimed title must be supported by the video title itself.
            if not (name_words & raw_words):
                continue
            if 2 < len(name) < 70:
                wiki = wiki_lookup(name)
                if wiki:
                    return {"title": wiki["title"], "year": wiki["year"] or year,
                            "confidence": 0.9, "notes": "description+wiki"}
                return {"title": name, "year": year, "confidence": 0.65,
                        "notes": "description"}
    return None



def top_comments(vid: str, n: int = 15) -> str:
    out, _ = run([YTDLP, "--no-warnings", "--no-update", "--skip-download",
                  "--write-comments", "--extractor-args",
                  f"youtube:comment_sort=top;max_comments={n},all,0",
                  "-J", f"https://www.youtube.com/watch?v={vid}"], timeout=120)
    try:
        d = json.loads(out)
    except Exception:  # noqa: BLE001
        return ""
    lines = [c.get("text", "")[:140] for c in (d.get("comments") or [])[:n]]
    return " / ".join(x for x in lines if x)[:1500]


# === catalog ==============================================================
def load() -> dict[str, Movie]:
    if not CATALOG.exists():
        return {}
    try:
        rows = json.loads(CATALOG.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: dict[str, Movie] = {}
    for r in rows:
        known = {k: v for k, v in r.items() if k in Movie.__dataclass_fields__}
        out[r["id"]] = Movie(**known)
    return out


def dedupe(cat: dict[str, Movie]) -> int:
    """Two uploads of the same film is shelf clutter. Keep the best one."""
    best: dict[tuple, Movie] = {}
    drop: list[str] = []
    for m in cat.values():
        key = (re.sub(r"[^a-z0-9]", "", m.title.lower()), m.year)
        cur = best.get(key)
        if cur is None:
            best[key] = m
            continue
        score = lambda x: (x.confidence, x.duration or 0)
        if score(m) > score(cur):
            best[key] = m
            drop.append(cur.id)
        else:
            drop.append(m.id)
    for vid in drop:
        cat.pop(vid, None)
    return len(drop)


def save(cat: dict[str, Movie]) -> None:
    dedupe(cat)
    rows = [asdict(m) for m in cat.values()]
    rows.sort(key=lambda m: (-(m.get("year") or 0), m["title"].lower()))
    CATALOG.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_source(src: str, title: str) -> str:
    """Uploader names come back dirty (whole titles, pipe stacks, emoji)."""
    s = (src or "").split("|")[0]
    s = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", s)
    s = re.sub(r"/@", "", s).strip(" -·.,!")
    if not s:
        return "YouTube"
    if len(s) > 30 or s.count("!") >= 3:
        return "YouTube"
    if title and s.lower()[:12] == title.lower()[:12]:
        return "YouTube"
    return s[:28]


def load_votes() -> dict:
    import json as _j
    vf = HERE / "votes.json" if "HERE" in globals() else __import__("pathlib").Path(__file__).resolve().parent / "votes.json"
    if vf.exists():
        try:
            return _j.loads(vf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"votes": {}, "rejected_ids": [], "rejected_channels": []}


def add_movie(cat: dict[str, Movie], vid: str, raw_title: str, source: str,
              duration: int, deep: bool = False) -> bool:
    if vid in cat:
        return False
    _v = load_votes()
    if vid in (_v.get("rejected_ids") or []):
        return False
    if source in (_v.get("rejected_channels") or []):
        return False
    if duration and not (MIN_SECONDS <= duration <= MAX_SECONDS):
        return False
    if JUNK.search(raw_title):
        return False

    info = identify(raw_title, source)

    probed = probe(vid)
    if probed["status"] == "removed":
        return False
    if not duration:
        duration = probed.get("duration") or 0
    if duration and not (MIN_SECONDS <= duration <= MAX_SECONDS):
        return False

    # The description beats the title for all-caps marketing uploads: the film
    # name is rarely in the title, but the uploader writes it below.
    if info["confidence"] < 0.9:
        better = identify_from_description(probed.get("description", ""), raw_title)
        if better and better["confidence"] > info["confidence"]:
            info = better

    if info["confidence"] < 0.65:
        info = {"title": clean_title(raw_title) or info["title"],
                "year": info.get("year"), "genres": info.get("genres") or [],
                "runtime": info.get("runtime"), "confidence": 0.35,
                "notes": "unverified"}

    if deep and info["confidence"] < 0.8:
        comments = top_comments(vid)
        if comments:
            raw = llm(
                "These are comments under a YouTube full-movie upload.\n"
                f'Title: "{raw_title}"\nComments: {comments}\n'
                'Which film is it? JSON only: {"title":"...","year":null,"is_film":true|false}'
            )
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    d = json.loads(m.group(0))
                    if d.get("is_film") and d.get("title"):
                        info = {"title": str(d["title"])[:90], "year": d.get("year"),
                                "confidence": 0.75, "notes": "comments"}
                except json.JSONDecodeError:
                    pass

    cat[vid] = Movie(
        id=vid, title=info["title"], year=info["year"],
        genres=info.get("genres") or [], duration=duration,
        runtime=info.get("runtime"), source=source, raw_title=raw_title[:180],
        status=probed["status"], availability=probed["availability"],
        confidence=info["confidence"], notes=info["notes"],
    )
    return True


def ingest_urls(cat: dict[str, Movie], path: str, deep: bool = False) -> int:
    """Feed Alice a list of YouTube URLs — e.g. "Copy link" from his iPhone
    YouTube app (Free Movies Daily catalog). One URL per line."""
    added = 0
    n = 0
    for raw in open(path, encoding="utf-8"):
        u = raw.strip()
        if not u or u.startswith("#"):
            continue
        m = re.search(r"v=([A-Za-z0-9_-]{11})", u)
        if not m:
            continue
        vid = m.group(1)
        d = yt_json(["--no-warnings", "--no-update", "--skip-download",
                     "--flat-playlist", "-J", "--playlist-end", "1", u], 120)
        title = (d or {}).get("title") or u
        dur = int((d or {}).get("duration") or 0)
        if dur and not (MIN_SECONDS <= dur <= MAX_SECONDS):
            continue
        src = "Free Movies Daily" if "FEstorefront" in u or "fmd" in u.lower() else "iPhone catalog"
        if add_movie(cat, vid, title, src, dur, deep=deep):
            m2 = cat[vid]
            print(f"  + {m2.title} ({m2.year or '?'}) [{dur//60}m]", flush=True)
            added += 1
        n += 1
    print(f"processed {n} URLs", flush=True)
    return added


def ingest_titles(cat: dict[str, Movie], path: str, deep: bool = False) -> int:
    """George's wishlist: a list of film titles, one per line. For each, search
    YouTube and add the best full-movie upload we can find."""
    added = 0
    lines = [l.strip() for l in open(path, encoding="utf-8") if l.strip()
             and not l.strip().startswith("#")]
    print(f"ingesting {len(lines)} requested titles", flush=True)
    for title in lines:
        # strip trailing year in parens for the search, keep it as a hint
        hint_year = None
        m = re.search(r"\((19|20)\d{2}\)", title)
        if m:
            hint_year = int(m.group(0).strip("()"))
        q = title
        d = yt_json(["--flat-playlist", "-J", f"ytsearch6:{q} full movie"], 120)
        entries = (d or {}).get("entries") if isinstance(d, dict) else []
        entries = [e for e in (entries or []) if e and e.get("id")]
        # prefer the longest upload that is at least 20 min
        entries.sort(key=lambda e: -(e.get("duration") or 0))
        for e in entries[:3]:
            vid = e.get("id"); raw = e.get("title") or title
            dur = int(e.get("duration") or 0)
            if not vid or dur < MIN_SECONDS:
                continue
            if add_movie(cat, vid, raw, "wishlist", dur, deep=deep):
                m2 = cat[vid]
                # honour the requested year if wiki didn't find one
                if hint_year and not m2.year:
                    m2.year = hint_year
                print(f"  + {vid} · {m2.title} ({m2.year or '?'}) [{dur//60}m]", flush=True)
                added += 1
                if added % 5 == 0:
                    save(cat)
                break
    return added


def ingest(cat: dict[str, Movie], deep: bool, force_search: str | None = None) -> int:
    added = 0
    sources: list[tuple[str, list[dict]]] = []

    if force_search:
        d = yt_json(["--flat-playlist", "-J", f"ytsearch6:{force_search} full movie"], 120)
        entries = (d or {}).get("entries") if isinstance(d, dict) else []
        sources.append(("search", [e for e in (entries or []) if e and e.get("id")]))
    else:
        for ch in CHANNELS:
            print(f"  → {ch['name']} ({ch['handle']})", flush=True)
            vids = channel_videos(ch["handle"])
            print(f"     {len(vids)} uploads", flush=True)
            sources.append((ch["name"], vids))
        for q in DISCOVERY:
            d = yt_json(["--flat-playlist", "-J", q], 120)
            entries = (d or {}).get("entries") if isinstance(d, dict) else []
            sources.append(("discovery", [e for e in (entries or []) if e and e.get("id")]))

    for name, vids in sources:
        for v in vids:
            vid = v.get("id")
            title = v.get("title") or ""
            dur = int(v.get("duration") or 0)
            if not vid:
                continue
            if dur and dur < MIN_SECONDS:
                continue
            if add_movie(cat, vid, title, name, dur, deep=deep):
                m = cat[vid]
                print(f"     + {vid} · {m.title} ({m.year or '?'}) "
                      f"[{dur//60}min, {m.confidence:.2f}]", flush=True)
                added += 1
                if added % 5 == 0:
                    save(cat)
    return added


REGION_PROBES = ["US", "GB", "CA", "DE", "IN", "BR", "MX", "AU", "JP", "FR"]


def region_scan(cat: dict[str, Movie], films: int = 6) -> int:
    """Which countries can actually play this video? YouTube answers per IP.

    yt-dlp's --geo-bypass-country makes YouTube believe the request comes from
    that country, so one sweep gives us a real allow-list per film.
    """
    targets = sorted((m for m in cat.values() if m.status == "ok"),
                     key=lambda m: m.region_checked)[:films]
    done = 0
    for m in targets:
        playable_in = []
        for cc in REGION_PROBES:
            out, err = run([YTDLP, "--no-warnings", "--no-update", "--skip-download",
                            f"--geo-bypass-country={cc}", "--print", "%(availability)s",
                            f"https://www.youtube.com/watch?v={m.id}"], timeout=45)
            blob = f"{out} {err}".lower()
            if out.strip() and "country" not in blob and "unavailable" not in blob:
                playable_in.append(cc)
        m.regions = playable_in
        m.region_checked = time.time()
        print(f"  🌍 {m.title[:38]:<38} {','.join(playable_in) or 'nowhere'}", flush=True)
        done += 1
        if done % 3 == 0:
            save(cat)
    return done


def health_check(cat: dict[str, Movie], max_checks: int = 200) -> int:
    """Mark the dead. YouTube purges this supply constantly — that is the job."""
    changed = 0
    for i, m in enumerate(cat.values()):
        if i >= max_checks:
            break
        age = time.time() - m.checked_at
        if m.status == "removed" and age < 86400 * 3:
            continue
        if age < 3600:          # already verified during this run
            continue
        status, avail = video_health(m.id)
        if status != m.status:
            print(f"  ⚠ {m.id} {m.title[:40]} — {m.status} → {status}", flush=True)
            m.status = status
            changed += 1
        m.availability = avail
        m.checked_at = time.time()
        if (i + 1) % 20 == 0:
            save(cat)
            print(f"     checked {i+1}/{min(len(cat), max_checks)}", flush=True)
    return changed


def reidentify(cat: dict[str, Movie]) -> int:
    """Re-run the librarian over what is already in the library."""
    changed = 0
    for i, m in enumerate(list(cat.values())):
        if m.id not in cat:
            continue
        raw = m.raw_title or m.title
        info = identify(raw, m.source)
        probed = probe(m.id)
        if info["confidence"] < 0.9:
            better = identify_from_description(probed.get("description", ""), raw)
            if better and better["confidence"] > info["confidence"]:
                info = better
        if info["confidence"] < 0.65:
            info = {"title": clean_title(raw) or info["title"],
                    "year": None, "genres": [], "runtime": None,
                    "confidence": 0.35, "notes": "unverified"}
        old = (m.title, m.year)
        # Never downgrade a confident identification to a shakier one.
        if info["confidence"] <= m.confidence + 0.001:
            if (i + 1) % 10 == 0:
                save(cat)
            continue
        m.title, m.year = info["title"], info["year"] or m.year
        m.genres = info.get("genres") or m.genres
        m.runtime = info.get("runtime") or m.runtime
        m.confidence, m.notes = info["confidence"], info["notes"]
        if old != (m.title, m.year):
            print(f"  ~ {m.id} {old[0][:34]!r} → {m.title[:44]!r} ({m.year})", flush=True)
            changed += 1
        if (i + 1) % 10 == 0:
            save(cat)
    return changed


def main() -> None:
    argv = sys.argv[1:]
    deep = "--deep" in argv
    force = None
    if "--add" in argv:
        i = argv.index("--add")
        force = argv[i + 1] if len(argv) > i + 1 else None

    cat = load()
    print(f"Alice curator — {len(cat)} films in library", flush=True)

    if "--regions" in argv:
        i = argv.index("--regions")
        n = int(argv[i + 1]) if len(argv) > i + 1 and argv[i + 1].isdigit() else 6
        done = region_scan(cat, n)
        save(cat)
        print(f"region-scan {done} films", flush=True)
        return

    if "--urls" in argv:
        i = argv.index("--urls")
        path = argv[i + 1] if len(argv) > i + 1 else None
        if not path:
            print("--urls needs a file"); return
        n = ingest_urls(cat, path, deep=deep)
        save(cat)
        print(f"added {n} from URL list · library {len(cat)}", flush=True)
        return

    if "--titles" in argv:
        i = argv.index("--titles")
        path = argv[i + 1] if len(argv) > i + 1 else None
        if not path:
            print("--titles needs a file"); return
        n = ingest_titles(cat, path, deep=deep)
        save(cat)
        print(f"added {n} from wishlist · library {len(cat)}", flush=True)
        return

    if "--reidentify" in argv:
        n = reidentify(cat)
        save(cat)
        print(f"reidentified {n} · library {len(cat)}", flush=True)
        return

    added = 0
    if force:
        added = ingest(cat, deep, force_search=force)
        save(cat)
        print(f"added {added}", flush=True)
    elif "--no-ingest" not in argv:
        print("· ingest", flush=True)
        added = ingest(cat, deep)
        save(cat)

    print("· health check", flush=True)
    changed = health_check(cat)
    save(cat)

    alive = sum(1 for m in cat.values() if m.status == "ok")
    STATE.write_text(json.dumps({
        "last_run": time.time(),
        "library": len(cat),
        "playable": alive,
        "added_this_run": added,
        "status_changes": changed,
    }, indent=2), encoding="utf-8")
    print(f"\n✓ library {len(cat)} · playable {alive} · +{added} · "
          f"{changed} status changes → {CATALOG.name}", flush=True)


if __name__ == "__main__":
    main()
