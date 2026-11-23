#!/usr/bin/env python3
"""Generate the final index.html with the Alice catalog embedded.

Usage: python3 generate_site.py [--output index.html]
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
INDEX = REPO / "index.html"
CATALOG = REPO / "movies_catalog.json"


def main() -> None:
    if not CATALOG.exists():
        print("No movies_catalog.json found. Run crawler.py first.")
        sys.exit(1)
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    catalog_json = json.dumps(catalog, ensure_ascii=False)

    html = INDEX.read_text(encoding="utf-8")
    if "<script>CONFIG_PLACEHOLDER</script>" in html:
        html = html.replace("<script>CONFIG_PLACEHOLDER</script>", f"<script>window.__MOVIES_CONFIG = {{ catalog: {catalog_json}, baseUrl: '/movies/' }};</script>")
        INDEX.write_text(html, encoding="utf-8")
        print(f"Generated index.html with {len(catalog)} movies")
        return

    # Inject CONFIG before the main <script> tag
    marker = "<script>"
    config = f"<script>window.__MOVIES_CONFIG = {{ catalog: {catalog_json}, baseUrl: '/movies/' }};</script>\n\n"
    idx = html.find(marker)
    if idx == -1:
        print("ERROR: no <script> tag found")
        sys.exit(1)
    html = html[:idx] + config + html[idx:]
    INDEX.write_text(html, encoding="utf-8")
    print(f"Generated index.html with {len(catalog)} movies")


if __name__ == "__main__":
    main()
