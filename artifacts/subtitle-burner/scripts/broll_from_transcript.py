#!/usr/bin/env python3
"""
B-roll from transcript — thin CLI for the studio overlay pipeline.

This is the practical version of:
  transcript keywords → search photos → save assets → timed overlays

It deliberately does NOT:
  - scrape Google with Playwright (fragile / ToS), or
  - composite with MoviePy (the app already burns overlays via FFmpeg).

Instead it calls the running Subtitle Assistant API:

  POST /fetch-auto-overlays
    { job_id | words, mode: auto|photo|badge, placement: pip|center }

Providers (env):
  PEXELS_API_KEY
  UNSPLASH_ACCESS_KEY
  GOOGLE_CSE_API_KEY + GOOGLE_CSE_CX

Usage:
  python scripts/broll_from_transcript.py --job-id <id> --mode photo --placement pip
  python scripts/broll_from_transcript.py --words-json words.json --mode auto
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default=os.environ.get("STUDIO_URL", "http://127.0.0.1:5000"))
    p.add_argument("--job-id", default=None)
    p.add_argument("--words-json", default=None, help="JSON list of {word,start,end}")
    p.add_argument("--mode", choices=("auto", "photo", "badge"), default="auto")
    p.add_argument("--placement", choices=("pip", "center"), default="pip")
    p.add_argument("--budget", type=int, default=5)
    args = p.parse_args()

    body = {"mode": args.mode, "placement": args.placement, "budget": args.budget}
    if args.job_id:
        body["job_id"] = args.job_id
    if args.words_json:
        with open(args.words_json, encoding="utf-8") as f:
            body["words"] = json.load(f)
    if not body.get("job_id") and not body.get("words"):
        print("Provide --job-id or --words-json", file=sys.stderr)
        return 2

    # Status first
    status_url = args.base.rstrip("/") + "/broll/status"
    try:
        with urllib.request.urlopen(status_url, timeout=15) as r:
            status = json.loads(r.read().decode("utf-8"))
        print("providers:", json.dumps(status.get("providers"), indent=2))
    except Exception as e:
        print(f"Could not reach {status_url}: {e}", file=sys.stderr)
        return 1

    req = urllib.request.Request(
        args.base.rstrip("/") + "/fetch-auto-overlays",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))

    print(f"mode={data.get('mode')} count={data.get('count')} stats={data.get('stats')}")
    for ov in data.get("overlays") or []:
        print(
            f"  [{ov.get('source')}] {ov.get('keyword')!r} "
            f"@ {ov.get('start'):.1f}s → asset {ov.get('asset_id')}"
        )
    print("\nAdd these via Timeline → Suggest B-roll, or POST each asset_id into the overlay track.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
