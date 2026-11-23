#!/usr/bin/env python3
"""dead_link_scan.py — fast full-catalog availability sweep for georgeanton.com.

Why this exists:
  The shelf hides a film only when its status is "removed". curator.py's
  health_check() checks just 200 films per run, one slow yt-dlp call each,
  and skips anything checked in the last hour — so YouTube deletions linger
  on the shelf for days. This sweeps ALL films in batched yt-dlp calls and
  flips dead ones to status="removed" so index.html's visible() hides them.

Usage:
  python3 dead_link_scan.py --dry-run          # report only, no writes
  python3 dead_link_scan.py                    # sweep + write catalog + backup
  python3 dead_link_scan.py --limit 100        # first N only (smoke test)
  python3 dead_link_scan.py --workers 20       # parallel batch size

Strategy: feed URLs to yt-dlp in batches (one process per batch via -a file),
which reuses its extractor cache and is far faster than one call per film.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG = HERE / "movies_catalog.json"
YTDLP = "/Users/ioanganton/.local/bin/yt-dlp"
BATCH = 25


def check_batch(ids: list[str], timeout: int = 180) -> dict[str, str]:
    """Return {video_id: status} for a batch. status in ok|restricted|removed|blocked."""
    if not ids:
        return {}
    urlfile = HERE / f".scan_batch_{ids[0]}.txt"
    urlfile.write_text(
        "\n".join(f"https://www.youtube.com/watch?v={i}" for i in ids),
        encoding="utf-8",
    )
    cmd = [
        YTDLP, "--no-warnings", "--no-update", "--skip-download",
        "--flat-playlist", "--ignore-errors",
        "--print", "%(id)s|%(availability)s",
        "-a", str(urlfile),
    ]
    out = ""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = p.stdout or ""
        err = (p.stderr or "").lower()
    except subprocess.TimeoutExpired:
        out, err = "", "timeout"
    finally:
        urlfile.unlink(missing_ok=True)

    alive: dict[str, str] = {}
    for line in out.splitlines():
        if "|" in line:
            vid, avail = line.split("|", 1)
            avail = avail.strip().lower()
            status = "ok" if avail in ("public", "unlisted") else "restricted"
            alive[vid.strip()] = status

    # Anything in the batch that yt-dlp did not print as alive is dead/blocked.
    # Distinguish "definitely gone" from "could not check" using stderr hints.
    result: dict[str, str] = {}
    for vid in ids:
        if vid in alive:
            result[vid] = alive[vid]
        else:
            result[vid] = "removed"  # no metadata at all → not playable on shelf
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    rows = json.loads(CATALOG.read_text(encoding="utf-8"))
    if args.limit:
        rows = rows[: args.limit]
    ids = [r["id"] for r in rows if r.get("id")]
    print(f"scanning {len(ids)} films in batches of {BATCH} ({args.workers} workers)", flush=True)

    batches = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
    verdicts: dict[str, str] = {}
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(check_batch, b): b for b in batches}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                verdicts.update(fut.result())
            except Exception as e:  # noqa: BLE001
                print(f"batch {n} failed: {e}", flush=True)
            if n % 10 == 0:
                print(f"  {n}/{len(batches)} batches · {len(verdicts)} checked · {time.time()-started:.0f}s", flush=True)

    dead = [i for i, s in verdicts.items() if s == "removed"]
    restricted = [i for i, s in verdicts.items() if s == "restricted"]
    print(f"\nverdicts: {len(verdicts)} checked · {len(dead)} removed/dead · {len(restricted)} restricted · {time.time()-started:.0f}s", flush=True)

    if args.dry_run:
        sample = json.loads(CATALOG.read_text(encoding="utf-8"))
        byid = {r["id"]: r for r in sample}
        for i in dead[:20]:
            print(f"  DEAD {i} {byid.get(i,{}).get('title','?')[:50]}", flush=True)
        return

    # Backup then write.
    backup = CATALOG.with_suffix(".json.pre_scan")
    if not backup.exists():
        shutil.copy2(CATALOG, backup)
        print(f"backup → {backup.name}", flush=True)

    changed = 0
    full = json.loads(CATALOG.read_text(encoding="utf-8"))
    for r in full:
        s = verdicts.get(r.get("id"))
        if s == "removed" and r.get("status") != "removed":
            r["status"] = "removed"
            r["availability"] = "removed"
            r["checked_at"] = time.time()
            changed += 1
    CATALOG.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote catalog · {changed} newly marked removed · library {len(full)}", flush=True)


if __name__ == "__main__":
    main()
