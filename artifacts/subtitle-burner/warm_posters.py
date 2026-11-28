#!/usr/bin/env python3
"""Render the picker's poster stills.

The co-editor's slash menu used to show hand-drawn PIL sketches chosen by
category, so a row never resembled what the look actually rendered. This
renders one real still per Remotion composition into .look-posters/ (gitignored
-- these are build artifacts), and app.py serves them in place of the sketches.

Run it after a deploy, or whenever a composition changes:

    python warm_posters.py              # only looks with no still yet
    python warm_posters.py --force      # re-render every look
    python warm_posters.py NumberCounter LineChart

Cost: ~30s for Remotion's first bundle, then ~4s per look. The whole catalogue
is about three minutes, once. Nothing here runs on a web request.

A look that fails is reported and skipped -- it keeps its sketch, which is what
the picker showed before, so a failure here never breaks the menu.
"""
from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    force = "--force" in argv
    ids = [a for a in argv if not a.startswith("-")]

    import app

    if not app._remotion_ready():
        missing = app._remotion_status()["missing"]
        print("Remotion cannot render here, so there are no stills to warm.")
        for m in missing:
            print(f"  - {m}")
        print("\nThe picker keeps its drawn sketches. Nothing is broken.")
        return 1

    report = app._warm_look_posters(ids or None, force=force)
    if not report:
        print(f"No Remotion look matched {ids!r}.")
        return 1

    ok = sorted(k for k, v in report.items() if v == "ok")
    cached = sorted(k for k, v in report.items() if v == "cached")
    bad = sorted((k, v) for k, v in report.items() if v.startswith("failed"))

    for look, why in bad:
        print(f"  FAILED  {look}: {why[8:]}")
    print(f"\nrendered {len(ok)}, already had {len(cached)}, failed {len(bad)}")
    if ok:
        print("rendered: " + ", ".join(ok))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
