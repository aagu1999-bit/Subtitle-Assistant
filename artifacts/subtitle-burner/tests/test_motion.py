"""Animations from a description: the scene format, its renderer and the route."""
import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image

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


def test_image_layers_draw_a_real_picture_and_never_take_a_path_from_the_model(tmp_path):
    from PIL import Image
    pic = tmp_path / "p.png"
    Image.new("RGB", (40, 40), (0, 200, 0)).save(pic)
    spec = motion.normalize({"size": [100, 100], "duration": 2, "layers": [
        {"id": "img", "type": "image", "photo": "dog", "file": "/etc/passwd",
         "x": 50, "y": 50, "w": 60, "h": 60, "shape": "circle"},
    ]})
    assert "file" not in spec["layers"][0], "a path from the model reached the renderer"
    assert motion.frame_at(spec, 1.0, (100, 100)).getchannel("A").getextrema()[1] == 0
    spec["layers"][0]["file"] = str(pic)
    img = motion.frame_at(spec, 1.0, (100, 100))
    assert img.getpixel((50, 50))[:3] == (0, 200, 0)
    assert img.getpixel((22, 22))[3] < 40, "circle mask not applied"


def test_pointer_clicks_get_click_sounds_unless_sounds_are_given():
    auto = motion.normalize(motion.EXAMPLE)
    assert auto["sounds"] == [{"t": 1.6, "name": "click"}]
    own = motion.normalize({**motion.EXAMPLE, "sounds": [{"t": 2.0, "name": "ding"},
                                                         {"t": 1, "name": "airhorn"}]})
    assert own["sounds"] == [{"t": 2.0, "name": "ding"}]


def test_sound_rules(app_mod):
    assert set(motion.SOUNDS) == set(app_mod.SFX_LIBRARY)
    hits = app_mod._tl_sound_hits([
        {"start": 1, "out": 3, "asset_id": "photo"},                       # B-roll: silent
        {"start": 2, "out": 3, "device": {"kind": "venn"}},               # graphic: default
        {"start": 4, "out": 3, "device": {"kind": "venn"}, "sfx": "none"},
        {"start": 5, "out": 3, "asset_id": "photo", "sfx": "whoosh"},     # chosen
        {"start": 6, "in": 0, "out": 3, "device": {"kind": "animation",
                                                    "spec": {"sounds": [{"t": 1.6, "name": "click"}]}}},
    ], [{"type": "punch_zoom", "start": 7}, {"type": "punch_zoom", "start": 8, "sfx": "thud"}],
        "pop")
    assert hits == [(2.0, "pop"), (5.0, "whoosh"), (7.6, "click"), (8.0, "thud")]
    assert app_mod._tl_sound_hits([{"start": 2, "device": {"kind": "venn"}}], [], "none") == []


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")
def test_sounds_mix_under_loud_speech_without_clipping(app_mod, tmp_path, monkeypatch):
    import json as _json
    import subprocess
    monkeypatch.setattr(app_mod, "FFMPEG", FFMPEG)
    base = tmp_path / "base.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x90:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=220:d=3,volume=0.95",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(base)], check=True)
    out = tmp_path / "out.mp4"
    app_mod._tl_mix_overlay_sfx(base, [{"start": 1.0, "device": {"kind": "venn"}},
                                       {"start": 1.0, "sfx": "thud"}], [], out, "pop")
    r = subprocess.run([FFMPEG, "-v", "info", "-i", str(out), "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    peak = float(r.split("max_volume:")[1].split("dB")[0])
    assert peak <= -0.5, f"mix clips at {peak} dB"
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
                           capture_output=True, text=True).stdout
    kinds = {s["codec_type"] for s in _json.loads(probe)["streams"]}
    assert kinds == {"video", "audio"}


def _carousel(by=-120, hold=0.3):
    orb = lambda ph: {"cx": 500, "cy": 170, "rx": 330, "ry": 70, "phase": ph, "depth": 0.45,
                      "steps": {"by": by, "move": 1.0, "hold": hold, "start": 0.4}}
    return motion.normalize({"duration": 5, "size": [1000, 340], "layers": [
        {"id": "sq", "type": "rect", "w": 150, "h": 150, "fill": "#FFD60A", "orbit": orb(0)},
        {"id": "tri", "type": "poly", "sides": 3, "r": 95, "fill": "#00E5FF", "orbit": orb(120)},
        {"id": "ci", "type": "circle", "r": 80, "fill": "#FF3B6B", "orbit": orb(240)},
    ]})


def test_an_orbit_carousel_turns_each_item_into_the_next_ones_place_and_holds():
    spec = _carousel()
    draw = motion.painter(spec, 1000, 340)
    memo_at = lambda t: {l["id"]: motion.state_at(l, t)["angle"] for l in spec["layers"]}

    def slot(t):
        out = {}
        for l in spec["layers"]:
            out[l["id"]] = round((l["orbit"]["phase"] + motion.state_at(l, t)["angle"]) % 360)
        return out

    assert slot(0.4) == {"sq": 0, "tri": 120, "ci": 240}
    # The square moves to where the circle was, the circle to the triangle's
    # place and the triangle to the front.
    assert slot(1.4) == {"sq": 240, "tri": 0, "ci": 120}
    assert slot(1.7) == slot(1.4), "the front item should rest for the hold"
    mid = slot(0.9)
    assert all(v % 120 for v in mid.values()), "items should be between places mid-turn"
    assert memo_at(5.0)["sq"] < memo_at(1.4)["sq"]

    frame = Image.new("RGBA", (1000, 340), (0, 0, 0, 0))
    draw(frame, 0.4)
    # The front square is drawn largest and at the bottom of the oval.
    assert _px(frame, 500, 240)[:3] == (255, 214, 10)
    assert _px(frame, 500, 170 - 70)[3] == 0


def test_the_front_orbit_item_is_drawn_on_top():
    spec = motion.normalize({"duration": 2, "size": [400, 200], "layers": [
        {"id": "front", "type": "rect", "w": 120, "h": 120, "fill": "#FF0000",
         "orbit": {"cx": 200, "cy": 100, "rx": 10, "ry": 10, "phase": 0}},
        {"id": "back", "type": "rect", "w": 120, "h": 120, "fill": "#0000FF",
         "orbit": {"cx": 200, "cy": 100, "rx": 10, "ry": 10, "phase": 180, "depth": 0}},
    ]})
    img = motion.frame_at(spec, 0.5, (400, 200))
    assert _px(img, 200, 100)[:3] == (255, 0, 0)


def test_orbits_survive_cropping():
    spec = motion.fit_to_content(_carousel())
    o = spec["layers"][0]["orbit"]
    w, h = spec["size"]
    assert w < 1000 and 0 < o["cx"] < w and 0 < o["cy"] < h


def test_a_triangle_polygon_points_up():
    spec = motion.normalize({"duration": 1, "size": [200, 200], "layers": [
        {"id": "t", "type": "poly", "sides": 3, "r": 90, "x": 100, "y": 100, "fill": "#00FF00"}]})
    img = motion.frame_at(spec, 0.5, (200, 200))
    assert _px(img, 100, 25)[3] > 0 and _px(img, 30, 25)[3] == 0
    assert _px(img, 40, 140)[3] > 0


def test_a_mistyped_photo_id_finds_the_users_photo_not_a_stock_search(app_mod, monkeypatch):
    real = "091eff0cee0645717898686e868a4e13"
    monkeypatch.setattr(app_mod, "_co_editor_asset_choices",
                        lambda: [{"asset_id": real, "name": "beach.jpg"}])
    monkeypatch.setattr(app_mod, "_fetch_broll_image_for_keyword",
                        lambda *a: pytest.fail("searched stock photos for an id"))
    monkeypatch.setattr(app_mod, "_broll_any_photo_provider", lambda: True)
    assert app_mod._resolve_photo_source(real[:-1] + "ee") == (real, "")
    aid, why = app_mod._resolve_photo_source("ffffffffffffffffffffffffffffffff")
    assert aid is None and why


def test_slow_coeditor_turns_run_in_the_background(app_mod, temp_db, monkeypatch):
    import threading
    import time as _t
    gate = threading.Event()

    def fake(prompt):
        gate.wait(5)
        return {"ops": [], "message": "Done thinking."}

    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions", fake)
    tl = {"canvas": "9x16", "tracks": {"main": [], "overlay": [], "text": [], "music": [], "effects": []}}
    c = app_mod.app.test_client()
    first = c.post("/co-editor", json={"prompt": "hello", "timeline": tl, "async": True}).get_json()
    token = first["pending"]
    assert c.get(f"/co-editor/result/{token}").get_json() == {"pending": token}
    gate.set()
    for _ in range(100):
        out = c.get(f"/co-editor/result/{token}").get_json()
        if "pending" not in out:
            break
        _t.sleep(0.05)
    assert out["message"] == "Done thinking."
    assert c.get(f"/co-editor/result/{token}").status_code == 404


def test_rotating_carousel_requests_are_sent_to_remotion(app_mod):
    assert app_mod._wants_remotion_orbit(
        "a mini carousel of a square a triangle and a circle rotating above the speakers")
    assert app_mod._wants_remotion_orbit("trade places around an oval")
    assert not app_mod._wants_remotion_orbit("a YouTube subscribe button")
    assert app_mod._orbit_hold("the front one rests 0.3 seconds") == 0.3
    assert app_mod._orbit_photo_queries("rotating carousel of three photos of bali beaches")
    assert not app_mod._orbit_photo_queries("rotating square triangle circle")
    assert app_mod._wants_remotion_orbit("replace the shapes with three photos of beaches in bali")


def test_make_animation_uses_remotion_for_an_orbit(app_mod, monkeypatch):
    seen = {}

    def fake(desc, tl, dur):
        seen["desc"] = desc
        return {"asset_id": "r1", "duration": 5, "box": {"x": 0, "y": 0.06, "w": 0.9, "h": 0.2},
                "device": {"kind": "animation", "engine": "remotion"}}

    monkeypatch.setattr(app_mod, "_make_remotion_orbit", fake)
    monkeypatch.setattr(app_mod, "_design_animation", lambda *a: pytest.fail("motion.py should not run"))
    out = app_mod._make_animation("rotating carousel of shapes above the heads", {"canvas": "9x16"}, 5)
    assert out["device"]["engine"] == "remotion" and seen["desc"].startswith("rotating")


def test_remotion_render_route_places_an_overlay(app_mod, temp_db, monkeypatch):
    monkeypatch.setattr(app_mod, "_make_remotion_orbit", lambda desc, tl, dur: {
        "asset_id": "r1", "kind": "video", "duration": 5.0,
        "box": {"x": 0.04, "y": 0.06, "w": 0.92, "h": 0.2, "px_w": 100, "px_h": 40},
        "device": {"kind": "animation", "engine": "remotion", "description": desc},
    })
    d = app_mod.app.test_client().post("/remotion/render", json={
        "description": "rotating carousel", "start": 5, "duration": 5}).get_json()
    assert d["clip"]["start"] == 5 and d["clip"]["device"]["engine"] == "remotion"


def test_photo_orbit_does_not_silently_become_shapes(app_mod, tmp_path, monkeypatch):
    from PIL import Image
    pics = []
    for i, c in enumerate([(20, 80, 180), (200, 140, 40), (40, 160, 90)]):
        p = tmp_path / f"b{i}.jpg"
        Image.new("RGB", (32, 32), c).save(p)
        pics.append(p)

    monkeypatch.setattr(app_mod, "_resolve_photo_source", lambda q: (None, "no stock"))
    monkeypatch.setattr(app_mod, "_gemini_image_ready", lambda: True)

    n = {"i": 0}

    def gen(prompt, stem):
        p = pics[n["i"] % 3]
        n["i"] += 1
        return p

    monkeypatch.setattr(app_mod, "gemini_image", gen)
    monkeypatch.setattr(app_mod, "REMOTION_DIR", tmp_path)
    items = app_mod._remotion_stage_photos(["bali beach"], want=3)
    assert [it["kind"] for it in items] == ["image", "image", "image"]
    assert {it["src"] for it in items}  # copied into public/
    # Asking for photos must not fall back to the square/triangle/circle.
    monkeypatch.setattr(app_mod, "_remotion_stage_photos", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no photos")))
    monkeypatch.setattr(app_mod, "_design_animation", lambda *a: pytest.fail("motion.py must not swallow a photo carousel"))
    with pytest.raises(RuntimeError, match="no photos"):
        app_mod._make_animation("replace the shapes with photos of bali beaches", {"canvas": "9x16"}, 5)


def test_wired_remotion_looks_match_their_phrases(app_mod):
    cases = {
        "a YouTube subscribe button": "YoutubeSubscribeNudge",
        "hit the bell": "YoutubeSubscribeNudge",
        "number counter to 24813": "NumberCounter",
        "a counter that goes to 10000": "NumberCounter",
        "vertical bar chart of views": "VerticalBarChart",
        "a line chart of the trend": "LineChart",
        "name lower third": "NameLowerThird",
        "word highlight captions": "WordHighlightCaptions",
        "popping word captions": "PoppingWordCaptions",
        "polaroid pictures of the trip": "PolaroidPictures",
        "on-screen messages look": "OnScreenMessages",
        'split text "WATCH THIS"': "SplitText",
        "youtube comment highlight": "YoutubeCommentHighlight",
        'blur text "HELLO"': "BlurText",
        "count up to 5000": "CountUp",
        "typewriter title": "TextType",
        "glitch text": "GlitchText",
        "rotating text": "RotatingText",
        "profile card": "ProfileCard",
        "circular carousel of cards": "CircularCarousel",
        "holo card": "HoloCard",
        "aurora background": "Aurora",
        "liquid chrome": "LiquidChrome",
        "looks like this": "LooksLikeThis",
        "screenshot tour": "ScreenshotTour",
    }
    for desc, cid in cases.items():
        rec = app_mod._match_remotion_recipe(desc)
        assert rec and rec["id"] == cid, desc
    assert app_mod._match_remotion_recipe("rotating carousel of shapes") is None
    assert app_mod._match_remotion_recipe("an encounter with a client") is None
    box = app_mod._remotion_overlay_box("9x16", "full")
    assert box["w"] == 1 and box["h"] == 1 and box["px_w"] == 1080


def test_make_animation_uses_remotion_for_a_wired_look(app_mod, monkeypatch):
    seen = {}

    def fake(rec, desc, tl, dur):
        seen["id"] = rec["id"]
        return {"asset_id": "r1", "duration": 4, "box": {"x": 0, "y": 0, "w": 1, "h": 1},
                "device": {"kind": "animation", "engine": "remotion", "composition": rec["id"]}}

    monkeypatch.setattr(app_mod, "_make_remotion_look", fake)
    monkeypatch.setattr(app_mod, "_design_animation", lambda *a: pytest.fail("motion.py should not run"))
    out = app_mod._make_animation("YouTube subscribe nudge on the end card", {"canvas": "9x16"}, 4)
    assert seen["id"] == "YoutubeSubscribeNudge" and out["device"]["engine"] == "remotion"


def test_split_text_passes_quoted_words(app_mod, monkeypatch, tmp_path):
    seen = {}

    def fake(comp, props, out):
        seen["comp"] = comp
        seen["props"] = props
        out.write_bytes(b"webm" + b"\0" * 400)

    monkeypatch.setattr(app_mod, "_render_remotion_webm", fake)
    monkeypatch.setattr(app_mod, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(app_mod, "ASSET_DIR", tmp_path)
    rec = app_mod._match_remotion_recipe('split text "WATCH THIS"')
    out = app_mod._make_remotion_look(rec, 'split text "WATCH THIS"', {"canvas": "9x16"}, 3)
    assert seen["comp"] == "SplitText" and seen["props"]["text"] == "WATCH THIS"
    assert seen["props"]["duration"] == 3 and out["box"]["w"] == 1


def test_gemini_json_with_quotes_in_the_message_is_salvaged(app_mod):
    broken = (
        '{\n"ops": [],\n"message": "I can help. Captions and "looks like this" '
        'and screenshot tour.\n"\n}'
    )
    out = app_mod._loads_model_json(broken)
    assert out["ops"] == []
    assert "looks like this" in out["message"] and "screenshot tour" in out["message"]


def test_the_prompt_names_wired_remotion_looks(app_mod, temp_db, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        lambda p: (seen.setdefault("p", p), {"ops": []})[1])
    tl = {"canvas": "9x16", "tracks": {"main": [], "overlay": [], "text": [], "music": [], "effects": []}}
    app_mod.app.test_client().post("/co-editor", json={
        "prompt": "what animations can you make", "timeline": tl})
    p = seen["p"]
    assert "YouTube subscribe nudge" in p
    assert "number counter" in p and "split text" in p
    assert "blur text" in p and "aurora background" in p
    assert "looks like this" in p and "screenshot tour" in p
    assert "WIRED REMOTION LOOKS" in p


def test_screenshot_tours_do_not_require_photos(app_mod):
    assert app_mod._shot_queries("screenshot tour") == []
    assert app_mod._shot_queries("looks like this") == []
    rec = app_mod._match_remotion_recipe("looks like this")
    props = app_mod._remotion_look_props(rec, "looks like this")
    assert props["caption"] == "looks like this" and props["items"] == []


def test_bits_backgrounds_sit_behind(app_mod):
    rec = app_mod._match_remotion_recipe("aurora background")
    assert rec["id"] == "Aurora" and rec.get("behind")
    clip = app_mod._device_overlay_clip({
        "asset_id": "a", "duration": 5, "box": {"x": 0, "y": 0, "w": 1, "h": 1},
        "device": {}, "behind": True,
    }, 0)
    assert clip["behind"] is True
