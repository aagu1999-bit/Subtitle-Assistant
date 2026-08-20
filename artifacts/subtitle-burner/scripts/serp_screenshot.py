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
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Larger viewport + device_scale_factor=2 → sharper SERP crops (thumbnail grids
# alone look soft; we also open the Images detail pane / download murl when possible).
DEFAULT_VIEWPORT = {"width": 1440, "height": 960}
DEVICE_SCALE_FACTOR = 2


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
                "width": min(900, hero["width"] + 2 * pad),
                "height": min(700, hero["height"] + 2 * pad),
            }
        except Exception:
            continue
    return None


def _bing_image_murls(page, *, limit: int = 8) -> list[str]:
    """Parse Bing Images tile metadata for full-size media URLs (murl)."""
    urls: list[str] = []
    try:
        raw = page.eval_on_selector_all(
            "a.iusc",
            """(els) => els.slice(0, 24).map((el) => {
              try {
                const m = el.getAttribute('m');
                if (!m) return null;
                const j = JSON.parse(m);
                return j.murl || j.purl || null;
              } catch (e) { return null; }
            }).filter(Boolean)""",
        )
        for u in raw or []:
            if isinstance(u, str) and u.startswith("http") and u not in urls:
                urls.append(u)
            if len(urls) >= limit:
                break
    except Exception:
        pass
    return urls


def _crop_box_bing_detail(page) -> dict | None:
    """Large preview after clicking a Bing Images tile (much clearer than grid thumbs)."""
    selectors = [
        "#mainImageWindow img",
        "#iol_im",
        ".overlayDetail img",
        "#detailCanvas img",
        "div.imgContainer img",
        "#ivc_fullimg",
        "img.nofocus",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = min(loc.count(), 6)
            boxes = []
            for i in range(n):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if not box:
                        continue
                    if box.get("width", 0) < 280 or box.get("height", 0) < 220:
                        continue
                    boxes.append(box)
                except Exception:
                    continue
            if not boxes:
                continue
            hero = max(boxes, key=lambda b: b["width"] * b["height"])
            pad = 8
            return {
                "x": max(0, hero["x"] - pad),
                "y": max(0, hero["y"] - pad),
                "width": min(1100, hero["width"] + 2 * pad),
                "height": min(820, hero["height"] + 2 * pad),
            }
        except Exception:
            continue
    return None


def _open_bing_image_detail(page) -> bool:
    """Click the first solid Bing Images tile so the detail pane loads."""
    for sel in ("a.iusc", ".imgpt a", ".imgpt img", "#mmComponent_images_1 a"):
        try:
            loc = page.locator(sel)
            n = min(loc.count(), 10)
            for i in range(n):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if not box or box.get("width", 0) < 80:
                        continue
                    el.click(timeout=2500)
                    page.wait_for_timeout(1100)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _download_url_to_path(url: str, out_path: Path, *, timeout_s: float = 25.0) -> bool:
    """Fetch a full-resolution image URL (Bing murl) when SERP thumbs are soft."""
    import urllib.request

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": "https://www.bing.com/",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
        if len(data) < 4000:
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Prefer PNG for Timeline; convert JPEG via Pillow when available.
        if "png" in ctype or url.lower().endswith(".png"):
            out_path.write_bytes(data)
        elif "jpeg" in ctype or "jpg" in ctype or url.lower().endswith((".jpg", ".jpeg")):
            try:
                from io import BytesIO
                from PIL import Image

                img = Image.open(BytesIO(data)).convert("RGB")
                img.save(out_path, format="PNG", optimize=True)
            except Exception:
                out_path.write_bytes(data)
        else:
            try:
                from io import BytesIO
                from PIL import Image

                img = Image.open(BytesIO(data)).convert("RGB")
                img.save(out_path, format="PNG", optimize=True)
            except Exception:
                out_path.write_bytes(data)
        return out_path.exists() and out_path.stat().st_size >= 4000
    except Exception:
        return False


def _open_google_image_detail(page) -> bool:
    for sel in ("div.isv-r a", "div[data-id] a", "img.rg_i"):
        try:
            loc = page.locator(sel)
            n = min(loc.count(), 8)
            for i in range(n):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if not box or box.get("width", 0) < 90:
                        continue
                    el.click(timeout=2500)
                    page.wait_for_timeout(1000)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _crop_box_google_detail(page) -> dict | None:
    selectors = [
        "img.sFlh5c",
        "a.YsLeY img",
        "#SvaFOb img",
        "div[data-ved] img.n3VNCb",
        "img.n3VNCb",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = min(loc.count(), 6)
            boxes = []
            for i in range(n):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if not box or box.get("width", 0) < 260 or box.get("height", 0) < 200:
                        continue
                    boxes.append(box)
                except Exception:
                    continue
            if not boxes:
                continue
            hero = max(boxes, key=lambda b: b["width"] * b["height"])
            pad = 8
            return {
                "x": max(0, hero["x"] - pad),
                "y": max(0, hero["y"] - pad),
                "width": min(1100, hero["width"] + 2 * pad),
                "height": min(820, hero["height"] + 2 * pad),
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


def _short_pw_error(exc: object) -> str:
    """Strip Playwright's huge 'Browser logs:' dump so UI alerts stay readable."""
    s = str(exc or "").strip() or "unknown_error"
    if "Browser logs:" in s:
        s = s.split("Browser logs:", 1)[0].strip()
    # Keep first meaningful line / sentence
    for sep in ("\n", "Call log:"):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) > 280:
        s = s[:277] + "…"
    return s


def _chromium_launch_args() -> list[str]:
    """Flags that keep Chromium alive in Replit / Docker / Nix sandboxes."""
    return [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--mute-audio",
        "--disable-blink-features=AutomationControlled",
        "--disable-extensions",
        "--disable-background-networking",
        "--font-render-hinting=none",
    ]


def _launch_chromium(p):
    """Try several Chromium launch strategies until one stays open.

    Replit often kills default ``chromium_headless_shell`` (sandbox / shm).
    Prefer full Chromium with ``chromium_sandbox=False`` + no-sandbox args.
    """
    # Prefer full Chromium over headless_shell when the env allows it.
    os.environ.setdefault("PLAYWRIGHT_CHROMIUM_USE_HEADLESS_SHELL", "0")

    attempts = [
        {
            "headless": True,
            "chromium_sandbox": False,
            "args": _chromium_launch_args(),
        },
        {
            "headless": True,
            "chromium_sandbox": False,
            "args": _chromium_launch_args() + ["--single-process", "--no-zygote"],
        },
        {
            # Last resort: headed mode behind a virtual display if present
            "headless": False,
            "chromium_sandbox": False,
            "args": _chromium_launch_args(),
        },
    ]
    errors: list[str] = []
    for opts in attempts:
        browser = None
        try:
            browser = p.chromium.launch(**opts)
            # Smoke-check: create a page so we fail fast if the process dies.
            ctx = browser.new_context(viewport={"width": 800, "height": 600})
            page = ctx.new_page()
            page.goto("about:blank", wait_until="domcontentloaded", timeout=8000)
            page.close()
            ctx.close()
            return browser
        except Exception as exc:
            errors.append(_short_pw_error(exc))
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            continue
    hint = (
        "Chromium failed to start on this host. In Replit Shell run: "
        "`python -m playwright install chromium` then Stop + Run. "
        "If it still fails: `playwright install-deps chromium` (needs packages)."
    )
    raise RuntimeError(hint + " Last errors: " + " | ".join(errors[:3]))


def capture_serp(
    query: str,
    *,
    tab: str = "images",
    out_path: Path,
    engine: str = "auto",
    timeout_ms: int = 45000,
    clarity: bool = True,
) -> dict:
    """Capture a cropped SERP screenshot. Returns metadata dict.

    When *clarity* is True (default):
      - Chromium device_scale_factor=2 (sharper PNG)
      - Images tab: open detail pane for a large preview crop
      - Bing Images: if detail is still soft, download full-size ``murl``
    """
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
        try:
            browser = _launch_chromium(p)
        except Exception as exc:
            return {
                "ok": False,
                "query": query,
                "tab": tab,
                "error": _short_pw_error(exc),
                "path": str(out_path),
                "hint": (
                    "Replit: python -m playwright install chromium && Stop+Run. "
                    "Needs --no-sandbox (bundled in this worker)."
                ),
            }

        try:
            context = browser.new_context(
                viewport=DEFAULT_VIEWPORT,
                device_scale_factor=DEVICE_SCALE_FACTOR if clarity else 1,
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
                    clarity_mode = "grid"
                    murls: list[str] = []

                    if eng == "google":
                        if tab == "images":
                            if clarity:
                                _open_google_image_detail(page)
                                clip = _crop_box_google_detail(page) or _crop_box_images_google(page)
                                clarity_mode = "detail" if clip else "grid"
                            else:
                                clip = _crop_box_images_google(page)
                        elif tab == "maps":
                            clip = _crop_box_maps_google(page)
                        else:
                            clip = _crop_box_web_google(page)
                    else:
                        if tab == "images":
                            murls = _bing_image_murls(page) if clarity else []
                            if clarity:
                                opened = _open_bing_image_detail(page)
                                clip = _crop_box_bing_detail(page) if opened else None
                                if clip:
                                    clarity_mode = "detail"
                                else:
                                    clip = _crop_box_bing_images(page)
                                    clarity_mode = "grid"
                            else:
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
                    bytes_n = out_path.stat().st_size if out_path.exists() else 0

                    # Bing Images: prefer full-size murl when the SERP crop is still a soft thumb.
                    crop_soft = (
                        float(clip.get("width") or 0) < 520
                        or float(clip.get("height") or 0) < 360
                        or bytes_n < 220_000
                    )
                    if (
                        clarity
                        and eng == "bing"
                        and tab == "images"
                        and murls
                        and crop_soft
                    ):
                        for murl in murls[:4]:
                            if _download_url_to_path(murl, out_path):
                                clarity_mode = "murl"
                                bytes_n = out_path.stat().st_size
                                break

                    meta = {
                        "ok": True,
                        "query": query,
                        "tab": tab,
                        "engine": eng,
                        "path": str(out_path),
                        "clip": clip if clarity_mode != "murl" else None,
                        "url": page.url,
                        "bytes": bytes_n,
                        "clarity": clarity_mode,
                        "device_scale_factor": DEVICE_SCALE_FACTOR if clarity else 1,
                    }
                    return meta
                except Exception as exc:
                    last_err = _short_pw_error(exc)
                    continue
        finally:
            try:
                browser.close()
            except Exception:
                pass

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
    ap.add_argument("--no-clarity", action="store_true", help="Disable 2x DPR / detail / murl clarity path")
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
    meta = capture_serp(
        args.query,
        tab=args.tab,
        out_path=Path(args.out),
        engine=args.engine,
        clarity=not args.no_clarity,
    )
    print(json.dumps(meta, indent=2))
    return 0 if meta.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
