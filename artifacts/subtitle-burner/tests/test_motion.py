"""Animations from a description: the scene format, its renderer and the route."""
import shutil
import sys
from pathlib import Path

import pytest

BURNER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BURNER))

import motion  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def _px(img, x, y):
    return img.getpixel((int(x), int(y)))


def test_the_example_scenes_are_valid_and_draw():
    for ex in (motion.EXAMPLE, motion.EXAMPLE_BAR):
        spec = motion.normalize(ex)
        img = motion.frame_at(spec, spec["duration"] * 0.6, (500, 160))
        assert img.getchannel("A").getextrema()[1] > 200


def test_children_are_placed_relative_to_their_parent():
    spec = motion.normalize({"size": [100, 100], "duration": 2, "layers": [
        {"id": "box", "type": "rect", "x": 30, "y": 50, "w": 20, "h": 20, "fill": "#FF0000",
         "keys": [{"t": 0, "x": 30}, {"t": 1, "x": 70, "ease": "linear"}]},
        {"id": "dot", "type": "circle", "parent": "box", "x": 0, "y": 0, "r": 4, "fill": "#00FF00"},
    ]})
    early = motion.frame_at(spec, 0.0, (100, 100))
    late = motion.frame_at(spec, 1.2, (100, 100))
    assert _px(early, 30, 50)[:3] == (0, 255, 0), "child is not centred on its parent"
    assert _px(late, 70, 50)[:3] == (0, 255, 0), "child did not move with its parent"


def test_a_left_anchored_bar_grows_to_the_right():
    spec = motion.normalize({"size": [100, 20], "duration": 2, "layers": [
        {"id": "bar", "type": "rect", "anchor": "left", "x": 10, "y": 10, "w": 2, "h": 10,
         "fill": "#FFFFFF", "keys": [{"t": 0, "w": 2}, {"t": 1, "w": 80, "ease": "linear"}]},
    ]})
    img = motion.frame_at(spec, 1.2, (100, 20))
    assert _px(img, 12, 10)[3] > 200 and _px(img, 85, 10)[3] > 200
    assert _px(img, 5, 10)[3] == 0, "a left-anchored bar must not grow leftwards"


def test_text_on_a_shape_is_shrunk_to_fit_inside_it():
    spec = motion.normalize({"size": [400, 100], "duration": 2, "layers": [
        {"id": "pill", "type": "rect", "x": 200, "y": 50, "w": 120, "h": 50, "radius": 25,
         "fill": "#3A3A3A"},
        {"id": "label", "type": "text", "parent": "pill", "x": 0, "y": 0, "size": 40,
         "text": "SUBSCRIBED", "color": "#FFFFFF"},
    ]})
    img = motion.frame_at(spec, 1.0, (400, 100))
    white = [x for x in range(400) if img.getpixel((x, 50))[:3] == (255, 255, 255)
             or (img.getpixel((x, 50))[0] > 200 and img.getpixel((x, 50))[3] > 200)]
    assert white and min(white) >= 140 and max(white) <= 260, "label spills off its pill"


def test_empty_space_is_cropped_so_the_graphic_fills_its_slot():
    spec = motion.normalize({"size": [1000, 400], "duration": 2, "width": 0.9, "layers": [
        {"id": "btn", "type": "rect", "x": 300, "y": 200, "w": 200, "h": 80, "fill": "#FF0000",
         "keys": [{"t": 0, "scale": 0.5}, {"t": 0.5, "scale": 1}]},
        {"id": "mouse", "type": "cursor", "x": 1100, "y": 500, "size": 60,
         "keys": [{"t": 0, "x": 1100}, {"t": 1, "x": 300, "y": 200}]},
    ]})
    fit = motion.fit_to_content(spec)
    w, h = fit["size"]
    assert 200 <= w <= 240 and 80 <= h <= 110, fit["size"]
    img = motion.frame_at(fit, 1.0, (220, int(220 * h / w)))
    assert img.getpixel((40, int(110 * h / w)))[:3] == (255, 0, 0), "button not centred after crop"


def test_counting_text_and_colour_keys_animate():
    spec = motion.normalize({"size": [200, 100], "duration": 2, "layers": [
        {"id": "n", "type": "text", "x": 100, "y": 50, "size": 40, "text": "",
         "count": {"from": 0, "to": 1000, "t0": 0, "t1": 1}},
        {"id": "c", "type": "circle", "x": 20, "y": 20, "r": 8, "fill": "#FF0000",
         "keys": [{"t": 0, "fill": "#FF0000"}, {"t": 1, "fill": "#0000FF", "ease": "linear"}]},
    ]})
    a = motion.frame_at(spec, 0.05, (200, 100))
    b = motion.frame_at(spec, 1.5, (200, 100))
    assert a.crop((40, 20, 160, 80)).tobytes() != b.crop((40, 20, 160, 80)).tobytes()
    assert _px(a, 20, 20)[0] > 200 and _px(b, 20, 20)[2] > 200


def test_model_output_is_clamped_not_trusted():
    spec = motion.normalize({
        "duration": 999, "size": ["x", -5],
        "layers": [{"type": "script", "code": "import os"}] + [
            {"type": "rect", "w": 1e9, "fill": "red", "keys": [{"t": -4, "x": "nan"}]}] * 200,
    })
    assert spec["duration"] == 12.0
    assert len(spec["layers"]) <= motion.MAX_LAYERS
    assert all(l["type"] == "rect" for l in spec["layers"])
    assert spec["layers"][0]["fill"] == "#FFFFFF"
    with pytest.raises(ValueError):
        motion.normalize({"layers": [{"type": "nope"}]})


def test_every_icon_draws():
    for name in motion.ICONS:
        img = motion._icon(name, 40, (255, 255, 255, 255))
        assert img.getchannel("A").getextrema()[1] > 100, name


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")
def test_render_is_cached(tmp_path):
    spec = motion.normalize(motion.EXAMPLE_BAR)
    spec["duration"] = 1.0
    p1, cached1 = motion.render_cached(spec, (200, 44), 12, tmp_path, ffmpeg=FFMPEG)
    p2, cached2 = motion.render_cached(spec, (200, 44), 12, tmp_path, ffmpeg=FFMPEG)
    assert p1 == p2 and p1.exists() and not cached1 and cached2


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")
def test_the_coeditor_makes_any_animation(app_mod, temp_db, monkeypatch):
    calls = []

    def fake(prompt):
        calls.append(prompt)
        if "motion designer" in prompt:
            spec = dict(motion.EXAMPLE_BAR)
            spec["duration"] = 1.0
            return spec
        return {"ops": [{"op": "add_animation", "description": "a loading bar", "start": 3}],
                "message": "Added."}

    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions", fake)
    monkeypatch.setattr(app_mod, "MOTION_REVIEW", False)
    monkeypatch.setattr(app_mod, "FFMPEG", FFMPEG)
    tl = {"canvas": "9x16", "fps": 12, "style": {},
          "tracks": {"main": [], "overlay": [], "text": [], "music": [], "effects": []}}
    out = app_mod.app.test_client().post("/co-editor", json={
        "prompt": "make a loading bar animation", "timeline": tl}).get_json()
    assert [o["op"] for o in out["ops"]] == ["add_animation"]
    clip = out["ops"][0]["clip"]
    assert clip["device"]["kind"] == "animation" and clip["start"] == 3
    assert (app_mod.ASSET_DIR / f"{clip['asset_id']}.webm").exists()
    assert "ANY OTHER ANIMATION" in calls[0]
