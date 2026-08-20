#!/usr/bin/env python3
"""Playwright SERP screenshot + crop worker for Timeline overlays.

Stays on the search engine results page (no click-through into articles).
Prefers Google; falls back to Bing when Google CAPTCHA/consent blocks.

Usage:
  python scripts/serp_screenshot.py --query "Asbury Park concert" --tab images --out /tmp/a.png
  python scripts/serp_screenshot.py --demo-nj   # capture the 3 NJ examples
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_VIEWPORT = {"width": 1280, "height": 900}


def _slug(q: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (q or "").strip().lower()).strip("-")
    return (s[:48] or "query")


def _detect_block(page) -> str | None:
    """Return a short reason if the page is a CAPTCHA / consent wall."""
    try:
        url = (page.url or "").lower()
        title = (page.title() or "").lower()
        body = ""
        try:
            body = (page.locator("body").inner_text(timeout=2000) or "")[:2000].lower()
        except Exception:
            pass
        blob = f"{url}\n{title}\n{body}"
        if "sorry/index" in url or "/sorry/" in url:
            return "google_sorry"
        if "captcha" in blob or "unusual traffic" in blob or "not a robot" in blob:
            return "captcha"
        if "before you continue" in blob and "google" in blob:
            return "consent"
        return None
    except Exception:
        return None


def _try_dismiss_consent(page) -> None:
    for label in (
        "Accept all",
        "Accept All",
        "I agree",
        "Agree",
        "Reject all",
        "Reject All",
    ):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=1500)
                page.wait_for_timeout(600)
                return
        except Exception:
            continue
    # Google consent iframe (EU)
    try:
        for frame in page.frames:
            if "consent" in (frame.url or "").lower():
                for label in ("Accept all", "Reject all", "I agree"):
                    try:
                        b = frame.get_by_role("button", name=re.compile(label, re.I))
                        if b.count():
                            b.first.click(timeout=1500)
                            page.wait_for_timeout(600)
                            return
                    except Exception:
                        continue
    except Exception:
        pass


def _crop_box_images_google(page) -> dict | None:
    """Prefer one large Images tile (pleasing overlay), else first-row strip."""
    selectors = [
        "div[data-id] a",
        "div.isv-r",
        "#rso img",
        "img.rg_i",
        "div[jsname] img",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = min(loc.count(), 12)
            if n < 1:
                continue
            boxes = []
            for i in range(n):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if not box or box.get("width", 0) < 100 or box.get("height", 0) < 100:
                        continue
                    boxes.append(box)
                except Exception:
                    continue
            if not boxes:
                continue
            # Largest single tile first — cleaner as a video overlay.
            hero = max(boxes, key=lambda b: b["width"] * b["height"])
            if hero["width"] >= 180 and hero["height"] >= 160:
                pad = 10
                return {
                    "x": max(0, hero["x"] - pad),
                    "y": max(0, hero["y"] - pad),
                    "width": min(page.viewport_size["width"] - 8, hero["width"] + 2 * pad),
                    "height": min(560, hero["height"] + 2 * pad),
                }
            left = min(b["x"] for b in boxes[:4])
            top = min(b["y"] for b in boxes[:4])
            right = max(b["x"] + b["width"] for b in boxes[:4])
            bottom = max(b["y"] + b["height"] for b in boxes[:4])
            pad = 12
            return {
                "x": max(0, left - pad),
                "y": max(0, top - pad),
                "width": min(page.viewport_size["width"] - 8, right - left + 2 * pad),
                "height": min(520, bottom - top + 2 * pad),
            }
        except Exception:
            continue
    return None


def _crop_box_web_google(page) -> dict | None:
    """Top organic result / AI overview / local pack — whichever is visible first."""
    selectors = [
        "div[data-attrid='wa:/description']",  # featured
        "#rso .g",
        "#search .g",
        "div.MjjYud",
        "div.VkpGBb",  # local pack
        "div.ULSxyf",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if not loc.count():
                continue
            if not loc.is_visible():
                continue
            box = loc.bounding_box()
            if not box or box.get("height", 0) < 40:
                continue
            # Cap height for a pleasing overlay card
            h = min(max(box["height"], 140), 420)
            w = min(max(box["width"], 320), 720)
            return {
                "x": max(0, box["x"] - 8),
                "y": max(0, box["y"] - 8),
                "width": w + 16,
                "height": h + 16,
            }
        except Exception:
            continue
    return None


def _crop_box_maps_google(page) -> dict | None:
    selectors = [
        "div[role='main'] div[data-result-index='0']",
        "a.hfpxzc",  # place result
        "div.Nv2PK",
        "div.VkpGBb",
        "#search .g",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if not loc.count() or not loc.is_visible():
                continue
            box = loc.bounding_box()
            if not box:
                continue
            return {
                "x": max(0, box["x"] - 10),
                "y": max(0, box["y"] - 10),
                "width": min(560, box["width"] + 20),
                "height": min(480, max(box["height"] + 20, 220)),
            }
        except Exception:
            continue
    return _crop_box_web_google(page)


def _crop_box_bing_images(page) -> dict | None:
    selectors = [
        ".imgpt img",
        "#mmComponent_images_1 img",
        ".img_cont img",
        "ul.iusc",
        ".imgpt",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = min(loc.count(), 12)
            boxes = []
            for i in range(n):
                try:
                    box = loc.nth(i).bounding_box()
                    if box and box.get("width", 0) > 120 and box.get("height", 0) > 120:
                        boxes.append(box)
                except Exception:
                    continue
            if not boxes:
                continue
            hero = max(boxes, key=lambda b: b["width"] * b["height"])
            pad = 10
            return {
                "x": max(0, hero["x"] - pad),
                "y": max(0, hero["y"] - pad),
                "width": min(720, hero["width"] + 2 * pad),
                "height": min(560, hero["height"] + 2 * pad),
            }
        except Exception:
            continue
    return None


def _crop_box_bing_web(page) -> dict | None:
    selectors = ["li.b_algo", "#b_results .b_algo", ".b_ans"]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if not loc.count() or not loc.is_visible():
                continue
            box = loc.bounding_box()
            if not box:
                continue
            return {
                "x": max(0, box["x"] - 8),
                "y": max(0, box["y"] - 8),
                "width": min(700, box["width"] + 16),
                "height": min(360, max(box["height"] + 16, 140)),
            }
        except Exception:
            continue
    return None


def _default_center_crop(page, *, w=720, h=420) -> dict:
    vw = page.viewport_size["width"]
    vh = page.viewport_size["height"]
    return {
        "x": max(0, (vw - w) // 2),
        "y": max(80, int(vh * 0.18)),
        "width": min(w, vw - 20),
        "height": min(h, vh - 100),
    }


def capture_serp(
    query: str,
    *,
    tab: str = "images",
    out_path: Path,
    engine: str = "auto",
    timeout_ms: int = 45000,
) -> dict:
    """Capture a cropped SERP screenshot. Returns metadata dict."""
    from playwright.sync_api import sync_playwright

    tab = (tab or "images").lower().strip()
    if tab not in ("images", "web", "maps", "flights", "ai_overview"):
        tab = "images"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    engines = []
    if engine == "google":
        engines = ["google"]
    elif engine == "bing":
        engines = ["bing"]
    else:
        engines = ["google", "bing"]

    last_err = None
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport=DEFAULT_VIEWPORT,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        for eng in engines:
            try:
                if eng == "google":
                    if tab == "images":
                        url = f"https://www.google.com/search?tbm=isch&q={_quote(query)}&hl=en&gl=us"
                    elif tab == "maps":
                        url = f"https://www.google.com/maps/search/{_quote(query)}"
                    elif tab == "flights":
                        url = f"https://www.google.com/travel/flights?q={_quote(query)}"
                    else:
                        url = f"https://www.google.com/search?q={_quote(query)}&hl=en&gl=us"
                else:
                    if tab == "images":
                        url = f"https://www.bing.com/images/search?q={_quote(query)}"
                    elif tab == "maps":
                        url = f"https://www.bing.com/maps?q={_quote(query)}"
                    else:
                        url = f"https://www.bing.com/search?q={_quote(query)}"

                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(900)
                _try_dismiss_consent(page)
                page.wait_for_timeout(700)
                block = _detect_block(page)
                if block and eng == "google":
                    last_err = block
                    continue

                # Extra settle for images/maps
                page.wait_for_timeout(1200)
                if eng == "google":
                    if tab == "images":
                        clip = _crop_box_images_google(page)
                    elif tab == "maps":
                        clip = _crop_box_maps_google(page)
                    else:
                        clip = _crop_box_web_google(page)
                else:
                    if tab == "images":
                        clip = _crop_box_bing_images(page)
                    else:
                        clip = _crop_box_bing_web(page)

                if not clip:
                    clip = _default_center_crop(page)

                # Clamp to viewport
                vw, vh = page.viewport_size["width"], page.viewport_size["height"]
                clip["x"] = max(0, min(clip["x"], vw - 40))
                clip["y"] = max(0, min(clip["y"], vh - 40))
                clip["width"] = max(120, min(clip["width"], vw - clip["x"]))
                clip["height"] = max(100, min(clip["height"], vh - clip["y"]))

                page.screenshot(path=str(out_path), clip=clip, type="png")
                meta = {
                    "ok": True,
                    "query": query,
                    "tab": tab,
                    "engine": eng,
                    "path": str(out_path),
                    "clip": clip,
                    "url": page.url,
                    "bytes": out_path.stat().st_size if out_path.exists() else 0,
                }
                browser.close()
                return meta
            except Exception as exc:
                last_err = str(exc)
                continue

        browser.close()

    return {
        "ok": False,
        "query": query,
        "tab": tab,
        "error": last_err or "capture_failed",
        "path": str(out_path),
    }


def _quote(q: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(q)


DEMO_NJ = [
    {
        "id": "juneteenth-nj-2026",
        "query": "Juneteenth 2026 New Jersey events",
        "tab": "images",
        "note": "Celebration / event atmosphere",
    },
    {
        "id": "asbury-concert",
        "query": "concert Asbury Park New Jersey live music",
        "tab": "images",
        "note": "Boardwalk / venue concert energy",
    },
    {
        "id": "exchange-place-jersey-city",
        "query": "Exchange Place Jersey City waterfront",
        "tab": "images",
        "note": "Skyline / PATH plaza (Hoboken–JC waterfront)",
    },
]


def run_demo_nj(out_dir: Path) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for item in DEMO_NJ:
        dest = out_dir / f"serp-{item['id']}.png"
        print(f"[serp] {item['id']}: {item['query']} ({item['tab']})", flush=True)
        meta = capture_serp(item["query"], tab=item["tab"], out_path=dest)
        meta["id"] = item["id"]
        meta["note"] = item["note"]
        results.append(meta)
        print(json.dumps({k: meta.get(k) for k in ("ok", "engine", "bytes", "error", "path")}, indent=2), flush=True)
        time.sleep(1.2)
    (out_dir / "serp-demo-nj.json").write_text(json.dumps(results, indent=2))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SERP screenshot + crop")
    ap.add_argument("--query", help="Search query")
    ap.add_argument("--tab", default="images", help="images|web|maps|flights|ai_overview")
    ap.add_argument("--out", help="Output PNG path")
    ap.add_argument("--engine", default="auto", help="auto|google|bing")
    ap.add_argument("--demo-nj", action="store_true", help="Capture 3 NJ examples")
    ap.add_argument(
        "--out-dir",
        default=str(ROOT / "static" / "mockups" / "google-overlays" / "live"),
        help="Demo output directory",
    )
    args = ap.parse_args(argv)

    if args.demo_nj:
        results = run_demo_nj(Path(args.out_dir))
        ok_n = sum(1 for r in results if r.get("ok"))
        print(f"[serp] done {ok_n}/{len(results)} ok", flush=True)
        return 0 if ok_n else 1

    if not args.query or not args.out:
        ap.error("--query and --out required (or use --demo-nj)")
    meta = capture_serp(args.query, tab=args.tab, out_path=Path(args.out), engine=args.engine)
    print(json.dumps(meta, indent=2))
    return 0 if meta.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
