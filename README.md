# Georgeanton Flicks

A Netflix-shaped shelf over the full movies that already live on YouTube — curated by Alice,
not by hand.

Live shelf: `http://127.0.0.1:8099` (and `http://192.168.1.153:8099` on the LAN).

## What it is, honestly

YouTube is full of uploaders who publish 90–150 minute films for free: Pelis Center, PAIN EDIXX,
Mundo Droid, Hollywood English Collection, Underground Cut, Lionsgate Movies, Movie Storm,
Nouvelle Vague, We Got Movies, and a long tail of channels nobody has ever heard of. Nobody
curates that pile. That is the whole idea: Alice walks it, names the films, throws out the junk,
and puts what is left on a shelf you can actually browse.

The site does not host or proxy video. Every film plays as a YouTube embed, so the bandwidth and
the takedown risk stay with YouTube and the shelf stays up.

## The pieces

| file | job |
|---|---|
| `curator.py` | the librarian — ingests channels, cleans titles, identifies films, health-checks the library |
| `discover.py` | walks the YouTube recommendation graph ("up next") to find new uploaders and films |
| `server.py` | serves the site + `/api/catalog`, `/api/health`, `/api/refresh` |
| `index.html` | the shelf itself (no framework, no CDN) |
| `movies_catalog.json` | the library the site reads |
| `channels.json` | uploaders discovered while walking recommendations |

## How a film gets identified

Titles on these uploads are useless marketing: *"THEY WERE SENTENCED TO DEATH… NOW THEY MUST
FIGHT UNTIL ONLY ONE SURVIVES"*. So identification runs in layers:

1. **Bracket titles** — `【SHELTER】` in the title is usually the real film.
2. **youtube-dlp metadata** — duration, availability, upload date, and the description
   (uploaders often write *"Movie Name (2015)"* below the fold).
3. **Wikipedia** — title, year, runtime, and genres from the article's categories.
4. **Local model** (`nemotron-3.5-bolt:e4b` on Ollama) — last resort, only allowed to tidy a
   string, never to invent a film.
5. Anything still under 0.65 confidence is kept as the cleaned upload title and marked
   **unverified** rather than being given a guess.

Junk is filtered by duration (under 20 minutes is not a feature film) and a blocklist
(gameplay, IPTV tutorials, trailers, DVD menus, compilations, livestreams).

## Keeping it true over time

YouTube purges this supply constantly. `curator.py --no-ingest` re-checks every film:

- `ok` — plays
- `restricted` — age- or region-gated
- `blocked` / `removed` — gone; shown greyed out with a red flag, not silently deleted

Duplicates (same film, two uploads) are collapsed on save, keeping the most-confident,
longest copy.

## Running it

```bash
# the shelf
/Users/ioanganton/.local/share/autoclip/venv/bin/python server.py       # :8099

# pull new films from the recommendation graph
... python discover.py --seed <videoId> --depth 2 --max 60

# re-check the whole library (removals, region blocks)
... python curator.py --no-ingest

# re-run the librarian over titles already in the library
... python curator.py --reidentify
```

`PORT=8099` overrides the port. The site's *"Ask her to crawl now"* button POSTs
`/api/refresh`, which runs the curator in the background.

## Status, 2026-11-22

- Working: shelf, catalog API, recommendation walk, channel ingest, Wikipedia identification,
  health checks, dedupe, background refresh from the UI.
- Working but shallow: region detection (YouTube reports availability, but per-country checks
  are not implemented yet — that needs a `--geo-bypass-country` sweep).
- Not done: `georgeanton.com` DNS. The domain lives in a Cloudflare account this machine has no
  API token for, so the shelf is served locally/LAN until that token exists.
- Known ugliness: a lot of this supply is AI-generated fake movies with real-film titles
  ("Jurassic World 6"). They are kept — George asked for the new stuff — but they are the reason
  the *unverified* flag exists.
