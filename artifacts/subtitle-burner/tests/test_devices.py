"""Graphics devices: transparent renders, caching, and the overlay composite."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BURNER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BURNER))

import devices  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")


def _frame_rgb(path: Path, t: float, w: int, h: int) -> bytes:
    return subprocess.run(
        [FFMPEG, "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-"],
        capture_output=True, check=True).stdout


@pytest.mark.parametrize("kind", sorted(devices.CATALOG))
def test_every_device_draws_something_and_leaves_the_rest_clear(kind):
    box = devices.box_for(kind, 1080, 1920)
    size = (box["px_w"] // 3, box["px_h"] // 3)
    dur = devices.CATALOG[kind]["dur"]
    img = devices.frame_at(kind, [], dur * 0.75, size)
    alpha = img.getchannel("A")
    hist = alpha.histogram()
    opaque = sum(hist[200:])
    clear = hist[0]
    assert opaque > 0, f"{kind} drew nothing"
    if devices.CATALOG[kind]["family"] in ("ui", "text"):
        assert clear > 0, f"{kind} should not fill its whole box"
    # The tail fades to nothing so devices never pop off.
    end = devices.frame_at(kind, [], dur - 0.001, size)
    assert end.getchannel("A").getextrema()[1] < 10


def test_labels_reach_the_pixels():
    a = devices.frame_at("venn", ["Cats", "Dogs", "Pets"], 2.5, (400, 200))
    b = devices.frame_at("venn", ["Money", "Time", "Life"], 2.5, (400, 200))
    assert a.tobytes() != b.tobytes()


def test_subscribe_button_is_clicked_and_turns_grey():
    size = (600, 190)

    def red_pixels(t):
        img = devices.frame_at("subscribe_button", ["Alex", "1.2M"], t, size).convert("RGB")
        return sum(1 for r, g, b in img.getdata() if r > 180 and g < 40 and b < 40)

    assert red_pixels(0.6) > 2000, "no red Subscribe button before the click"
    assert red_pixels(2.4) == 0, "button still red after it was clicked"

    import edit_engine
    assert edit_engine.suggest_device("don't forget to subscribe and hit the bell")["kind"] \
        == "subscribe_button"


def test_cache_key_covers_every_input():
    base = ("counter", ["40%"], 2.8, (400, 200), 30, ["#FFFFFF"], "Anton")
    k = devices.cache_key(*base)
    assert k == devices.cache_key(*base)
    for i, alt in enumerate([("venn",), (["41%"],), (3.0,), ((402, 200),), (24,),
                              (["#000000"],), ("DM Sans",)]):
        changed = list(base)
        changed[i] = alt[0]
        assert devices.cache_key(*changed) != k


@needs_ffmpeg
def test_render_is_transparent_vp9_and_cached(tmp_path):
    p, cached = devices.render_cached("step_tracker", ["Plan", "Build", "Ship"], 1.5,
                                      (320, 96), 24, tmp_path)
    assert not cached
    tags = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name:stream_tags=alpha_mode",
         "-of", "csv=p=0", str(p)], capture_output=True, text=True).stdout
    assert "vp9" in tags and "1" in tags
    mtime = p.stat().st_mtime
    p2, cached2 = devices.render_cached("step_tracker", ["Plan", "Build", "Ship"], 1.5,
                                        (320, 96), 24, tmp_path)
    assert cached2 and p2 == p and p2.stat().st_mtime == mtime


@needs_ffmpeg
def test_device_composites_with_alpha_onto_the_video(app_mod, clip, tmp_path):
    info = app_mod._render_device_asset("counter", ["99%", "uptime"], 1.6, "9x16",
                                        ["#FF0000", "#00FF00"], None, 30)
    ov = app_mod._device_overlay_clip(info, 0.2)
    assert app_mod._is_alpha_video(app_mod._find_asset_path(info["asset_id"]))
    out = tmp_path / "dev.mp4"
    app_mod._tl_composite_overlays(clip, [ov], 1080, 1920, out, fps=30)
    W, H = 108, 192
    before = _frame_rgb(clip, 1.4, W, H)
    after = _frame_rgb(out, 1.4, W, H)

    def px(buf, x, y):
        i = (y * W + x) * 3
        return buf[i:i + 3]

    # Inside the device box the counter panel covers the bars...
    bx = int((ov["x"] + ov["w"] / 2) * W)
    by = int((ov["y"] + ov["h"] / 2) * H)
    assert px(before, bx, by) != px(after, bx, by)
    # ...and well outside it the video shows through untouched (no black box).
    ox, oy = W // 2, int(H * 0.8)
    assert all(abs(a - b) < 24 for a, b in zip(px(before, ox, oy), px(after, ox, oy)))
    # Same request, same asset.
    again = app_mod._render_device_asset("counter", ["99%", "uptime"], 1.6, "9x16",
                                         ["#FF0000", "#00FF00"], None, 30)
    assert again["asset_id"] == info["asset_id"] and again["cached"]


def test_device_routes(app_mod):
    c = app_mod.app.test_client()
    cat = c.get("/devices/catalog").get_json()
    assert {d["kind"] for d in cat["devices"]} == set(devices.CATALOG)
    assert cat["pack_budgets"]["film"]["per_minute"] == 1
    s = c.post("/devices/suggest", json={"text": "she texted me at 2am"}).get_json()
    assert s["kind"] == "chat_bubbles"
    s = c.post("/devices/suggest", json={"text": "revenue grew 40% last year"}).get_json()
    assert s["kind"] == "counter" and s["labels"][0].startswith("40")
    r = c.get("/devices/preview.png?kind=venn&labels=%5B%22A%22%2C%22B%22%5D")
    assert r.status_code == 200 and r.data[:4] == b"\x89PNG"
    assert c.post("/devices/render", json={"kind": "nope"}).status_code == 400


def test_style_pack_budget_limits_and_filters_devices():
    import edit_engine as ee
    words = []
    t = 0.0
    for i in range(12):
        for tok in f"Revenue grew {i + 1}0% and she texted me.".split():
            words.append({"word": tok, "start": t, "end": t + 0.25})
            t += 0.3
        t += 2.0
    film = {"graphics_budget": {"per_minute": 1, "allow": ["venn", "timeline_scrubber"]}}
    plan = ee.plan_edit(words, 0, t, intensity="med", style_pack=film)
    kinds = [r["device"]["kind"] for r in plan["structure"]["spans"] if "device" in r]
    assert all(k in ("venn", "timeline_scrubber") for k in kinds)
    pulse = {"graphics_budget": {"per_minute": 6, "allow": ["ui", "stat"]}}
    plan = ee.plan_edit(words, 0, t, intensity="high", style_pack=pulse)
    kinds = [r["device"]["kind"] for r in plan["structure"]["spans"] if "device" in r]
    assert kinds and all(k in ("counter", "chat_bubbles", "notification", "search_bar",
                               "toggle_meter") for k in kinds)
