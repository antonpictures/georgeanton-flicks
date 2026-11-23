#!/usr/bin/env python3
"""Georgeanton Flicks server — the shelf that georgeanton.com serves.

    python3 server.py            # http://127.0.0.1:8099
    PORT=8099 python3 server.py

Routes:
    GET  /                 the site
    GET  /api/catalog      {"movies":[…], "built":ts, "sources":n}
    GET  /api/health       quick liveness + library counts
    POST /api/refresh      run the curator in the background (Alice's hands)
    GET  /<file>           static files from this directory

The server never proxies video: playback is a YouTube embed, so the bandwidth
and the takedown risk stay with YouTube, and the shelf stays up.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG = HERE / "movies_catalog.json"
VOTES = HERE / "votes.json"
PORT = int(os.environ.get("PORT", "8099"))
PY = "/Users/ioanganton/.local/share/autoclip/venv/bin/python"

_refresh_lock = threading.Lock()
_refresh_state: dict = {"running": False, "started": 0, "finished": 0, "last_log": ""}


def load_votes() -> dict:
    if VOTES.exists():
        try:
            return json.loads(VOTES.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"votes": {}, "rejected_ids": [], "rejected_channels": [], "voters": {}}


def save_votes(v: dict) -> None:
    VOTES.write_text(json.dumps(v, indent=2), encoding="utf-8")


def catalog_payload() -> dict:
    try:
        rows = json.loads(CATALOG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        rows = []
    rows.sort(key=lambda m: (-(m.get("year") or 0), -(m.get("added_at") or 0)))
    sources = len({m.get("source") for m in rows if m.get("source")})
    return {
        "movies": rows,
        "count": len(rows),
        "playable": sum(1 for m in rows if m.get("status") == "ok"),
        "sources": sources,
        "built": int(CATALOG.stat().st_mtime) if CATALOG.exists() else 0,
    }


def run_curator() -> None:
    with _refresh_lock:
        if _refresh_state["running"]:
            return
        _refresh_state.update(running=True, started=time.time())
    try:
        p = subprocess.run([PY, str(HERE / "curator.py")], cwd=str(HERE),
                           capture_output=True, text=True, timeout=3600)
        _refresh_state["last_log"] = (p.stdout or "")[-4000:]
    except Exception as e:  # noqa: BLE001
        _refresh_state["last_log"] = f"curator failed: {e}"
    finally:
        _refresh_state.update(running=False, finished=time.time())


def client_ip(self_handler) -> str:
    for h in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
        v = self_handler.headers.get(h)
        if v:
            return v.split(",")[0].strip()
    return self_handler.client_address[0] if self_handler.client_address else "unknown"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def log_message(self, fmt, *args):  # keep the log quiet but present
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/catalog":
            return self._json(catalog_payload())
        if path == "/api/health":
            p = catalog_payload()
            return self._json({"ok": True, "count": p["count"], "playable": p["playable"],
                               "refresh": _refresh_state})
        if path == "/api/votes":
            return self._json(load_votes())
        if path == "/api/refresh":
            return self._json({"refresh": _refresh_state})
        if path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/vote":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                body = {}
            vid = body.get("id")
            vote = body.get("vote")
            if vote not in ("ok", "fake"):
                return self._json({"ok": False, "reason": "bad vote"}, 400)
            v = load_votes()
            entry = v["votes"].setdefault(vid, {"ok": 0, "fake": 0})
            # No dedup — pure crowd algorithm. Every click counts.
            entry[vote] = entry.get(vote, 0) + 1
            # A FAKE vote is a ranking signal, not a deletion: the film stays
            # in the library but sinks to the bottom of the shelf. Alice also
            # learns the uploader so she stops adding more from it.
            if vote == "fake":
                if vid not in v["rejected_ids"]:
                    v["rejected_ids"].append(vid)
                ch = None
                try:
                    rows = json.loads(CATALOG.read_text(encoding="utf-8"))
                    for m in rows:
                        if m.get("id") == vid:
                            ch = m.get("source")
                            break
                except Exception:  # noqa: BLE001
                    pass
                if ch and ch not in v["rejected_channels"]:
                    v["rejected_channels"].append(ch)
            save_votes(v)
            return self._json({"ok": True, "votes": v["votes"].get(vid, {})})
        if path == "/api/refresh":
            if _refresh_state["running"]:
                return self._json({"started": False, "reason": "already running",
                                   "refresh": _refresh_state}, 202)
            threading.Thread(target=run_curator, daemon=True).start()
            return self._json({"started": True})
        self.send_error(404)


def main() -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Georgeanton Flicks → http://127.0.0.1:{PORT}  ({HERE})", flush=True)
    if CATALOG.exists():
        p = catalog_payload()
        print(f"library: {p['count']} films · {p['playable']} playable · "
              f"{p['sources']} uploaders", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
