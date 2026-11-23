#!/bin/zsh
# Alice's nightly shelf work: check every film, then pull a few new ones
# from the recommendation graph. Logs land in ~/.sifta-logs (never under
# ~/Music — launchd cannot write stdio there, TCC).
set -u
PY=/Users/ioanganton/.local/share/autoclip/venv/bin/python
DIR=/Users/ioanganton/Music/ANTON_SIFTA/scratch/movie-clone
LOG=/Users/ioanganton/.sifta-logs/flicks-curator.log

cd "$DIR" || exit 1
echo "=== $(date -u +%FT%TZ) nightly start ===" >> "$LOG"
"$PY" curator.py --no-ingest >> "$LOG" 2>&1
"$PY" curator.py --regions 6 >> "$LOG" 2>&1
"$PY" discover.py --from-catalog --depth 1 --max 25 >> "$LOG" 2>&1
echo "=== $(date -u +%FT%TZ) nightly done ===" >> "$LOG"
