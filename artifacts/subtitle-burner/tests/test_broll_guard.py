"""B-roll ladder, colour match and the generative policy."""
import shutil
import subprocess

import pytest
from PIL import Image

import broll_guard as bg

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")

LIB = [
    {"asset_id": "a" * 32, "kind": "video", "filename": "desk_late_night_laptop.mp4", "source": "upload"},
    {"asset_id": "b" * 32, "kind": "image", "filename": "beach.jpg", "source": "upload"},
    {"asset_id": "c" * 32, "kind": "video", "filename": "Counter: 40%", "source": "device"},
]


def test_concepts_find_your_footage_even_without_the_literal_words():
    res = bg.smart_broll("I was drowning in spreadsheets", assets=LIB)
    assert res["source"] == "library" and res["asset"]["asset_id"] == "a" * 32


def test_library_wins_when_your_footage_matches():
    calls = []
    res = bg.smart_broll("I was up all night at my desk", assets=LIB,
                         stock=lambda q: calls.append(q), generate=lambda t: calls.append(t))
    assert res["source"] == "library" and res["asset"]["asset_id"] == "a" * 32
    assert calls == [], "stock and generation are never consulted when the library hits"


def test_devices_never_count_as_library_footage():
    assert bg.library_match(["counter"], LIB) is None


def test_stock_searches_the_concept_and_generation_is_last():
    seen = []

    def stock(q):
        seen.append(q)
        return None

    # LIB's late-night desk clip would match the concept; leave it out.
    res = bg.smart_broll("honestly I was drowning in spreadsheets", assets=LIB[1:], stock=stock,
                         generate=lambda t: {"asset_id": "g" * 32, "look": "orbit"})
    assert seen and not any("drown" in q for q in seen)
    assert res["source"] == "generated"
    assert [s["step"] for s in res["ladder"]] == ["library"] + ["stock"] * len(seen) + ["generated"]


def test_generation_can_be_switched_off():
    res = bg.smart_broll("a quiet morning", assets=[], stock=None, generate=lambda t: {"x": 1},
                         allow_generate=False)
    assert res["source"] is None and res["asset"] is None


def test_prompts_are_held_to_the_policy():
    p = bg.sanitize_prompt("my mom holding up a sign that says SOLD", ["#112233"])
    assert p["ok"]
    low = p["prompt"].lower()
    assert "mom" not in low and "sign" not in low and "sold" not in low


def test_color_match_pulls_toward_the_a_roll():
    warm_dark = {"y": 70.0, "cb": 115.0, "cr": 145.0, "sat": 140.0, "grain": 6.0}
    clean = {"y": 150.0, "cb": 128.0, "cr": 128.0, "sat": 60.0, "grain": 0.5}
    p = bg.color_match_params(warm_dark, clean)
    assert p["brightness"] < 0 and p["saturation"] > 1
    assert p["rm"] > 0 and p["bm"] < 0 and p["grain"] > 0
    f = bg.color_match_filter(p)
    assert f.startswith("eq=") and "colorbalance=" in f and "noise=" in f
    assert bg.color_match_filter(bg.color_match_params(clean, clean)) == ""
    assert bg.color_match_filter({"brightness": "x"}) == ""


def test_palette_ignores_black_bars():
    img = Image.new("RGB", (100, 100), (0, 0, 0))
    img.paste((200, 80, 40), (0, 20, 100, 80))
    pal = bg.palette(img)
    assert pal[0] == "#C85028"


@needs_ffmpeg
def test_abstract_fill_renders_tinted_and_caches(tmp_path):
    g = bg.generate_abstract("pressure of the deadline", ["#AA2200", "#220000"], 1.0, 360, 640,
                             tmp_path, 24, FFMPEG)
    assert g["look"] == "orbit" and g["ok"] and g["path"].exists()
    img = bg.frame_image(g["path"], 0.5, FFMPEG)
    st = bg.image_stats(img)
    assert st["cr"] > 135, "the fill should carry the A-roll's red palette"
    again = bg.generate_abstract("pressure of the deadline", ["#AA2200", "#220000"], 1.0, 360,
                                 640, tmp_path, 24, FFMPEG)
    assert again["cached"] and again["path"] == g["path"]


@needs_ffmpeg
def test_smart_route_falls_back_to_a_color_matched_abstract(app_mod, clip, monkeypatch):
    monkeypatch.setattr(app_mod, "_broll_any_photo_provider", lambda: False)
    job_id = "t" * 32
    app_mod.jobs[job_id] = {"timeline": {"canvas": "9x16", "tracks": {"main": [
        {"id": "m", "asset_id": None, "in": 0, "out": 2}]}}}
    monkeypatch.setattr(app_mod, "_timeline_clip_source", lambda c: clip if c.get("id") == "m"
                        else app_mod._find_asset_path(c.get("asset_id")))
    c = app_mod.app.test_client()
    r = c.post("/broll/smart", json={"job_id": job_id, "text": "zxqv plorbing the wibble",
                                     "start": 0.5, "duration": 1.0}).get_json()
    app_mod.jobs.pop(job_id, None)
    assert r["source"] == "generated", r
    clip_ = r["clip"]
    assert clip_["w"] == 1.0 and clip_["broll"]["source"] == "generated"
    assert "color_match" in clip_
    meta = app_mod._read_asset_meta(clip_["asset_id"])
    assert meta["source"] == "generated" and "no people" in meta["prompt"]


@needs_ffmpeg
def test_color_match_reaches_the_composite(app_mod, clip, tmp_path):
    still = tmp_path / "gray.png"
    Image.new("RGB", (64, 64), (128, 128, 128)).save(still)
    asset_id = "e" * 32
    dest = app_mod.ASSET_DIR / f"{asset_id}.png"
    shutil.copyfile(still, dest)
    try:
        ov = {"asset_id": asset_id, "start": 0, "in": 0, "out": 2, "x": 0, "y": 0, "w": 1.0,
              "h": 1.0, "color_match": {"brightness": -0.3, "saturation": 1, "rm": 0.2,
                                        "gm": 0, "bm": -0.2, "grain": 0}}
        out = tmp_path / "cm.mp4"
        app_mod._tl_composite_overlays(clip, [ov], 1080, 1920, out, fps=30)
        raw = subprocess.run([FFMPEG, "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "4x4", "-"],
                             capture_output=True, check=True).stdout
        r, g, b = raw[0], raw[1], raw[2]
        assert r < 128 and b + 10 < r, (r, g, b)
    finally:
        dest.unlink(missing_ok=True)
