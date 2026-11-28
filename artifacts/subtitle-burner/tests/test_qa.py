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
    assert ok["engine"] == "model" and ok["issues"] == [
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
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(qa, "local_face_boxes", lambda p: [])
        v = c.post(f"/qa/{jid}/run", json={"checks": False, "vision": True}).get_json()
        assert v["vision"]["engine"] == "local" and v["vision"]["frames"] == 5
    finally:
        app_mod.jobs.pop(sid, None)
        app_mod.jobs.pop(jid, None)
        dest.unlink(missing_ok=True)


def test_vertical_packs_keep_captions_out_of_the_feed_ui(app_mod):
    for pid, pack in app_mod.AI_EDIT_STYLE_PACKS.items():
        if pack.get("canvas", "9x16") == "9x16":
            assert pack["style"]["position_y"] <= 75, pid


# ---- the report reaching the model, and the two wires that used to drop it ----

def test_the_report_keeps_findings_and_names_the_op_that_fixes_each(app_mod):
    """What _qa_for_prompt hands the model, and what it deliberately does not.

    A clean check is not an instruction, and a defect the model has no op for
    is worse than silence -- told about loudness it cannot change, it invents
    an op. So severity decides inclusion and `you_can_fix_this` decides
    whether acting is allowed.
    """
    app_mod.jobs["qaP"] = {"qa": {
        "status": "fail",
        "ran_at": __import__("time").time() - 42,
        "checks": [
            {"id": "resolution", "status": "pass", "detail": "1080x1920"},
            {"id": "caption_safe", "status": "warn", "detail": "1 caption sits under the vertical feed UI",
             "items": [{"start": 7.25, "text": "down low"}]},
            {"id": "caption_frame", "status": "fail", "detail": "1 caption runs off the frame",
             "items": [{"start": 2.0, "text": "too long"}]},
            {"id": "loudness", "status": "warn", "detail": "-20.5 LUFS, quieter than the -16..-12 target"},
            {"id": "caption_safe_skipped", "status": "skip", "detail": "nothing burned"},
        ],
        "vision": {"engine": "model+local", "issues": [
            {"t": 3.5, "severity": "warn", "problem": "Venn covers a face -- try Behind me or move it"}]},
    }}
    try:
        got = app_mod._qa_for_prompt("qaP")
    finally:
        app_mod.jobs.pop("qaP", None)

    by = {f["check"]: f for f in got["findings"]}
    assert "resolution" not in by, "a passing check is not an instruction"
    assert not any(f["severity"] == "skip" for f in got["findings"])
    assert by["caption_safe"]["at_seconds"] == [7.25], "the model is not told when"
    assert by["caption_frame"]["severity"] == "fail"
    assert "position_y" in by["caption_safe"]["you_can_fix_this"]
    assert "set_behind" in by["vision"]["you_can_fix_this"]
    # the one it must NOT act on
    assert "loudness" in by and "you_can_fix_this" not in by["loudness"]
    assert got["status"] == "fail"
    assert 40 <= got["render_age_seconds"] <= 60, "staleness is not reported"


def test_a_report_still_running_or_absent_says_nothing_at_all(app_mod):
    """An in-flight render must not read as a verdict."""
    app_mod.jobs["qaR"] = {"qa": {"status": "running"}}
    app_mod.jobs["qaE"] = {"qa": {"status": "error", "error": "ffprobe died"}}
    app_mod.jobs["qaN"] = {}
    try:
        assert app_mod._qa_for_prompt("qaR") is None
        assert app_mod._qa_for_prompt("qaE") is None
        assert app_mod._qa_for_prompt("qaN") is None
        assert app_mod._qa_for_prompt("nosuchjob") is None
    finally:
        for k in ("qaR", "qaE", "qaN"):
            app_mod.jobs.pop(k, None)


def test_a_clean_render_is_one_line_not_fifty(app_mod):
    app_mod.jobs["qaC"] = {"qa": {"status": "pass", "checks": [
        {"id": "resolution", "status": "pass", "detail": "1080x1920"},
        {"id": "fps", "status": "pass", "detail": "30 fps"}]}}
    try:
        got = app_mod._qa_for_prompt("qaC")
    finally:
        app_mod.jobs.pop("qaC", None)
    assert got["status"] == "pass" and got["findings"] == []


def test_the_model_and_the_geometry_check_both_report(tmp_path):
    """run_vision used to `return` on the model path, so with a key configured
    the caption/graphic boxes were computed for this call and thrown away.

    The two see different things: the local pass is handed the exact boxes so
    it knows for certain, the model only ever sees pixels but is the only one
    that can call text unreadable.
    """
    f = tmp_path / "f.jpg"
    f.write_bytes(b"x")
    boxes = [{"start": 0, "end": 1, "text": "hello", "x0": 300, "y0": 500, "x1": 800, "y1": 700}]

    res = qa.run_vision(
        [{"t": 0.5, "path": f}],
        lambda p, imgs: {"issues": [{"t": 0.5, "severity": "warn", "problem": "Text is unreadable"}]},
        boxes=boxes, W=1080, H=1920, graphics=[],
        face_fn=lambda p: [{"x0": 0.35, "y0": 0.25, "x1": 0.6, "y1": 0.38}])

    problems = [i["problem"] for i in res["issues"]]
    assert any("unreadable" in p for p in problems), "the model's finding was lost"
    assert any("covers a face" in p and "hello" in p for p in problems), \
        "the geometry check was computed and dropped"
    assert res["engine"] == "model+local"


def test_the_same_finding_from_both_engines_is_reported_once(tmp_path):
    f = tmp_path / "f.jpg"
    f.write_bytes(b"x")
    boxes = [{"start": 0, "end": 1, "text": "hello", "x0": 300, "y0": 500, "x1": 800, "y1": 700}]
    face = lambda p: [{"x0": 0.35, "y0": 0.25, "x1": 0.6, "y1": 0.38}]  # noqa: E731
    local = qa.local_vision([{"t": 0.5, "path": f}], boxes, 1080, 1920, [], face_fn=face)
    dup = dict(local["issues"][0])

    res = qa.run_vision([{"t": 0.5, "path": f}], lambda p, imgs: {"issues": [dup]},
                        boxes=boxes, W=1080, H=1920, graphics=[], face_fn=face)
    same = [i for i in res["issues"] if "covers a face" in i["problem"]]
    assert len(same) == 1, f"reported {len(same)} times: {same}"


@needs_ffmpeg
def test_re_check_does_not_lose_a_caption_that_runs_off_the_frame(app_mod, clip, temp_db):
    """Re-check used to turn a hard fail into a skip.

    qa_run built its kwargs only for timeline jobs and never passed ass_text
    at all, so caption_frame was not emitted and caption_safe answered "skip".
    A caption running off the frame -- a **fail** on the first render -- simply
    vanished when you pressed the button, which is the worst direction for a
    defect to move in.
    """
    jid = "qaRecheck" + uuid.uuid4().hex[:6]
    out_name = f"{jid}.mp4"
    shutil.copyfile(clip, app_mod.OUTPUT_DIR / out_name)
    (app_mod.UPLOAD_DIR / f"{jid}.ass").write_text(ASS, encoding="utf-8")
    app_mod.jobs[jid] = {"status": "done", "output": out_name, "is_timeline": False}
    c = app_mod.app.test_client()
    try:
        rep = c.post(f"/qa/{jid}/run", json={"checks": True, "vision": False}).get_json()
        got = {ch["id"]: ch["status"] for ch in rep["checks"]}
        assert got["caption_frame"] == "fail", f"the fail was lost on re-check: {got}"
        assert got["caption_safe"] == "warn"
        assert rep["status"] == "fail"

        # And this is the bug itself, kept as a test so the guard cannot
        # quietly stop guarding: starve the call of the script and the fail
        # disappears rather than becoming a pass.
        import pytest as _pt
        with _pt.MonkeyPatch.context() as mp:
            mp.setattr(app_mod, "_qa_ass_text", lambda *a, **k: None)
            blind = c.post(f"/qa/{jid}/run", json={"checks": True, "vision": False}).get_json()
        ids = {ch["id"]: ch["status"] for ch in blind["checks"]}
        assert "caption_frame" not in ids and ids["caption_safe"] == "skip"
        assert blind["status"] != "fail", "the old behaviour was worse than this"
    finally:
        app_mod.jobs.pop(jid, None)
        (app_mod.OUTPUT_DIR / out_name).unlink(missing_ok=True)
        (app_mod.UPLOAD_DIR / f"{jid}.ass").unlink(missing_ok=True)


def test_a_timeline_job_is_read_from_its_own_subtitle_script(app_mod):
    """The two render paths write to different names; pick by the job kind."""
    jid = "qaPick" + uuid.uuid4().hex[:6]
    (app_mod.UPLOAD_DIR / f"{jid}.ass").write_text("BURN", encoding="utf-8")
    (app_mod.UPLOAD_DIR / f"{jid}_tltext.ass").write_text("TIMELINE", encoding="utf-8")
    try:
        assert app_mod._qa_ass_text(jid, is_timeline=True) == "TIMELINE"
        assert app_mod._qa_ass_text(jid, is_timeline=False) == "BURN"
        assert app_mod._qa_ass_text("no-such-job-at-all") is None
    finally:
        (app_mod.UPLOAD_DIR / f"{jid}.ass").unlink(missing_ok=True)
        (app_mod.UPLOAD_DIR / f"{jid}_tltext.ass").unlink(missing_ok=True)


def test_the_report_survives_a_restart(app_mod, temp_db):
    """_db_save_job is called straight after the report is written and had no
    column to put it in, so reopening a checked job showed "status: none"."""
    jid = "qaDb" + uuid.uuid4().hex[:6]
    rep = {"status": "warn", "checks": [{"id": "loudness", "status": "warn", "detail": "-20.5 LUFS"}],
           "keyframes": [{"t": 1.0, "file": "x_qa0.jpg"}], "ran_at": 1700000000.0}
    app_mod.jobs[jid] = {"status": "done", "output": "x.mp4", "qa": rep, "created_at": 1.0}
    try:
        app_mod._db_save_job(jid)
        app_mod.jobs.pop(jid)
        app_mod._load_jobs_from_db()
        assert app_mod.jobs[jid]["qa"] == rep, "the verdict did not survive"
        # an in-flight state is not worth persisting: after a restart the
        # thread that would finish it is gone, so it could only be a lie
        app_mod.jobs[jid]["qa"] = {"status": "running"}
        app_mod._db_save_job(jid)
        app_mod.jobs.pop(jid)
        app_mod._load_jobs_from_db()
        assert app_mod.jobs[jid]["qa"] is None
    finally:
        app_mod.jobs.pop(jid, None)


def test_the_vision_pass_runs_on_whichever_provider_has_a_key(app_mod, monkeypatch):
    """It gated on GEMINI_API_KEY while the call itself dispatches to Claude,
    so an Anthropic-only deploy quietly got no vision pass at all."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert app_mod._vision_llm_available() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert app_mod._vision_llm_available() is True, "Claude-only deploy gets no vision"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    assert app_mod._vision_llm_available() is True, "Gemini-only deploy regressed"
