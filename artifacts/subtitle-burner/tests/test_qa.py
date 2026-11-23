"""Post-render QA checks and their wiring into renders."""
import shutil
import subprocess
import uuid

import pytest

import qa

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")

ASS = """[Script Info]
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DM Sans,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,4,0,2,40,40,600,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,hello there
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\pos(540,1850)}down in the buttons
Dialogue: 0,0:00:02.00,0:00:03.00,Default,,0,0,0,,this caption is far far far too long to ever fit on one line of a phone
"""


def test_caption_boxes_follow_alignment_pos_and_margins():
    from devices import _font
    boxes = qa.caption_boxes(ASS, 1080, 1920, _font)
    assert len(boxes) == 3
    a, b, c = boxes
    assert a["y1"] == pytest.approx(1920 - 600, abs=1)
    assert abs((a["x0"] + a["x1"]) / 2 - 540) < 1
    assert b["y1"] == pytest.approx(1850, abs=1)
    assert c["x1"] - c["x0"] > 1080


def test_caption_checks_flag_overflow_and_the_platform_ui():
    from devices import _font
    checks = {c["id"]: c for c in qa.check_captions(qa.caption_boxes(ASS, 1080, 1920, _font), 1080, 1920)}
    assert checks["caption_frame"]["status"] == "fail"
    assert checks["caption_frame"]["items"][0]["start"] == 2.0
    assert checks["caption_safe"]["status"] == "warn"
    assert checks["caption_safe"]["items"][0]["start"] == 1.0


def test_format_checks():
    info = {"width": 1080, "height": 1920, "fps": 30.0, "duration": 9.0, "has_audio": True}
    got = {c["id"]: c["status"] for c in qa.check_format(info, 1080, 1920, 30, 10.0)}
    assert got == {"resolution": "pass", "fps": "pass", "duration": "warn"}
    got = {c["id"]: c["status"] for c in qa.check_format(dict(info, width=720), 1080, 1920, 30, 9.0)}
    assert got["resolution"] == "fail"


def test_loudness_and_black_and_dead_air_rules():
    av = {"lufs": -20.5, "peak": -0.2, "black": [{"start": 1.0, "end": 1.2}, {"start": 4, "end": 5}],
          "silence": [{"start": 6.0, "end": 9.0}]}
    got = {c["id"]: c for c in qa.check_av(av, 10.0)}
    assert got["loudness"]["status"] == "warn" and "quieter" in got["loudness"]["detail"]
    assert got["peak"]["status"] == "warn"
    assert got["black_frames"]["status"] == "warn" and len(got["black_frames"]["items"]) == 1
    assert got["dead_air"]["status"] == "warn"


@needs_ffmpeg
def test_analyze_finds_black_silence_and_loudness(tmp_path):
    src = tmp_path / "t.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=black:s=320x240:d=1.5,format=yuv420p",
                    "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1.5",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=2.5",
                    "-f", "lavfi", "-i", "sine=frequency=440:d=0.5:sample_rate=44100",
                    "-filter_complex",
                    "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a][3:a]concat=n=2:v=0:a=1[a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-c:a", "aac", str(src)],
                   check=True)
    av = qa.analyze_av(src, FFMPEG)
    assert av["black"] and av["black"][0]["start"] == 0
    assert av["silence"] and av["silence"][0]["start"] == 0
    assert av["lufs"] is not None


def test_local_vision_spots_a_caption_over_a_face(tmp_path):
    frames = [{"t": 0.5, "path": tmp_path / "f.jpg"}]
    boxes = [{"start": 0, "end": 1, "text": "hello", "x0": 300, "y0": 500, "x1": 800, "y1": 700}]
    graphics = [{"start": 0, "end": 1, "label": "Venn", "x0": 0, "y0": 0, "x1": 1080, "y1": 900}]
    res = qa.local_vision(frames, boxes, 1080, 1920, graphics,
                          face_fn=lambda p: [{"x0": 0.35, "y0": 0.25, "x1": 0.6, "y1": 0.38}])
    problems = [i["problem"] for i in res["issues"]]
    assert any("covers a face" in p and "hello" in p for p in problems)
    assert any(p.startswith("Venn covers a face") for p in problems)


def test_vision_falls_back_locally_when_the_model_fails(tmp_path):
    f = tmp_path / "f.jpg"
    f.write_bytes(b"x")

    def boom(prompt, images):
        raise RuntimeError("quota")

    res = qa.run_vision([{"t": 0.0, "path": f}], boom, boxes=[], W=1080, H=1920,
                        face_fn=lambda p: [])
    assert res["engine"] == "local" and "quota" in res["note"]
    ok = qa.run_vision([{"t": 1.0, "path": f}],
                       lambda p, imgs: {"issues": [{"t": 1, "severity": "bad", "problem": "Cropped head"},
                                                   {"nope": 1}]})
    assert ok["engine"] == "gemini" and ok["issues"] == [
        {"t": 1.0, "severity": "warn", "problem": "Cropped head"}]


@needs_ffmpeg
def test_a_timeline_render_comes_back_with_a_qa_report(app_mod, clip, monkeypatch):
    sid = uuid.uuid4().hex
    dest = app_mod.UPLOAD_DIR / f"{sid}.mp4"
    shutil.copyfile(clip, dest)
    app_mod.jobs[sid] = {"status": "done", "words": [{"word": "hi", "start": 0.2, "end": 0.6}],
                         "style": {}, "emoji_rules": {}}
    jid = uuid.uuid4().hex
    tl = {"canvas": "9x16", "fps": 30, "tracks": {"main": [
        {"id": "m", "source_job_id": sid, "in": 0, "out": 2}], "overlay": [], "text": [],
        "effects": [], "music": []}}
    app_mod.jobs[jid] = {"status": "queued", "is_timeline": True, "timeline": tl}
    try:
        app_mod.render_timeline_job(jid, tl)
        rep = app_mod.jobs[jid]["qa"]
        ids = {c["id"]: c["status"] for c in rep["checks"]}
        assert ids["resolution"] == "pass" and ids["duration"] == "pass"
        assert ids["caption_frame"] == "pass"
        assert "loudness" in ids and len(rep["keyframes"]) == 5
        assert rep["render_stats"]["hits"] + rep["render_stats"]["misses"] == 1
        c = app_mod.app.test_client()
        got = c.get(f"/qa/{jid}").get_json()
        assert got["keyframes"][0]["url"].startswith("/preview/")
        assert c.get(got["keyframes"][0]["url"]).status_code == 200
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setattr(qa, "local_face_boxes", lambda p: [])
        v = c.post(f"/qa/{jid}/run", json={"checks": False, "vision": True}).get_json()
        assert v["vision"]["engine"] == "local" and v["vision"]["frames"] == 5
    finally:
        app_mod.jobs.pop(sid, None)
        app_mod.jobs.pop(jid, None)
        dest.unlink(missing_ok=True)
