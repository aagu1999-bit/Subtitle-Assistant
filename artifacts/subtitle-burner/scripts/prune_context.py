#!/usr/bin/env python3
"""Context hygiene: prune learned taste and lint the agent memory notes.

Stale context is worse than none: a learned habit from months ago, or a
memory note that points at a file that no longer exists, steers every later
prompt the wrong way. This script

  * decays and drops stale learned entries in the taste store (user-written
    rules are never touched), and
  * checks .agents/memory: index links that point nowhere, notes the index
    does not list, notes without name/description front matter, and notes
    not touched in a long time (flagged for re-verification, not deleted).

Usage:
  python scripts/prune_context.py                 # prune + lint, print a report
  python scripts/prune_context.py --dry-run       # report only
  python scripts/prune_context.py --reset-learned # forget all learned taste

Exits 1 when the memory index has dead links, so it can gate CI.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BURNER = HERE.parent
REPO = BURNER.parent.parent
sys.path.insert(0, str(BURNER))

import taste  # noqa: E402

LINK = re.compile(r"\[([^\]]+)\]\(([^)#\s]+)(?:#[^)]*)?\)")


def _last_touched(path: Path) -> float:
    """Last commit time for *path*, else its mtime."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%ct", "--", str(path)],
                             cwd=str(path.parent), capture_output=True, text=True,
                             timeout=10).stdout.strip()
        if out:
            return float(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return path.stat().st_mtime


def lint_memory(mem_dir: Path, stale_days: float = 180, now: float | None = None) -> dict:
    now = now or time.time()
    report = {"dir": str(mem_dir), "dead_links": [], "orphans": [], "missing_front_matter": [],
              "stale": [], "notes": 0}
    if not mem_dir.is_dir():
        report["error"] = "memory directory not found"
        return report
    index = mem_dir / "MEMORY.md"
    notes = sorted(p for p in mem_dir.glob("*.md") if p.name != "MEMORY.md")
    report["notes"] = len(notes)
    linked = set()
    if index.exists():
        for m in LINK.finditer(index.read_text(encoding="utf-8")):
            target = m.group(2)
            if re.match(r"^[a-z]+://", target):
                continue
            linked.add((mem_dir / target).resolve())
            if not (mem_dir / target).exists():
                report["dead_links"].append({"label": m.group(1), "target": target})
    for p in notes:
        if p.resolve() not in linked:
            report["orphans"].append(p.name)
        text = p.read_text(encoding="utf-8")
        fm = re.match(r"^---\n(.*?)\n---", text, re.S)
        if not fm or "name:" not in fm.group(1) or "description:" not in fm.group(1):
            report["missing_front_matter"].append(p.name)
        # Relative links inside a note can rot too.
        for m in LINK.finditer(text):
            target = m.group(2)
            if re.match(r"^[a-z]+://", target) or target.startswith("/"):
                continue
            if not (p.parent / target).exists():
                report["dead_links"].append({"label": m.group(1), "target": target, "in": p.name})
        age = (now - _last_touched(p)) / 86400.0
        if age > stale_days:
            report["stale"].append({"note": p.name, "days": int(age)})
    return report


def prune_taste(path: Path, ttl_days: float, dry_run: bool, reset_learned: bool) -> dict:
    if not path.exists():
        return {"path": str(path), "skipped": "no taste store yet"}
    target = path
    tmpdir = None
    if dry_run:
        tmpdir = Path(tempfile.mkdtemp())
        target = tmpdir / path.name
        shutil.copyfile(path, target)
    store = taste.TasteStore(target)
    try:
        if reset_learned:
            res = {"removed": store.reset("learned")}
        else:
            res = store.prune(ttl_days=ttl_days)
        res["path"] = str(path)
        res["dry_run"] = dry_run
        return res
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--taste", type=Path, default=BURNER / "data" / "taste.json")
    ap.add_argument("--memory", type=Path, default=REPO / ".agents" / "memory")
    ap.add_argument("--ttl-days", type=float, default=120)
    ap.add_argument("--stale-days", type=float, default=180)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset-learned", action="store_true")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    t = prune_taste(args.taste, args.ttl_days, args.dry_run, args.reset_learned)
    m = lint_memory(args.memory, args.stale_days)
    if args.json:
        print(json.dumps({"taste": t, "memory": m}, indent=2))
    else:
        print(f"Taste store ({t.get('path')}):", json.dumps({k: v for k, v in t.items() if k != "path"}))
        print(f"Memory notes in {m['dir']}: {m['notes']}")
        for d in m["dead_links"]:
            print(f"  DEAD LINK  {d['target']}" + (f" (in {d['in']})" if d.get("in") else " (index)"))
        for o in m["orphans"]:
            print(f"  ORPHAN     {o} is not listed in MEMORY.md")
        for f in m["missing_front_matter"]:
            print(f"  NO HEADER  {f} lacks name/description front matter")
        for s in m["stale"]:
            print(f"  STALE      {s['note']} untouched for {s['days']} days — re-verify")
        if not (m["dead_links"] or m["orphans"] or m["missing_front_matter"] or m["stale"]):
            print("  clean")
    return 1 if m["dead_links"] else 0


if __name__ == "__main__":
    sys.exit(main())
