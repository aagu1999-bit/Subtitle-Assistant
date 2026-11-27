"""Speed ramps, behind-subject graphics and the segment cache, through real renders."""
import importlib.util
import inspect
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")


def _dur(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return float(out.strip())


@pytest.fixture
def source_job(app_mod, clip):
    sid = uuid.uuid4().hex
    dest = app_mod.UPLOAD_DIR / f"{sid}.mp4"
    shutil.copyfile(clip, dest)
    app_mod.jobs[sid] = {
        "status": "done", "style": {}, "emoji_rules": {},
        "words": [{"word": "one", "start": 0.1, "end": 0.4},
                  {"word": "two", "start": 1.2, "end": 1.5},
                  {"word": "three", "start": 1.6, "end": 1.9}],
    }
    yield sid
    app_mod.jobs.pop(sid, None)
    dest.unlink(missing_ok=True)


def _timeline(sid, effects=(), overlays=()):
    return {"canvas": "9x16", "fps": 30, "tracks": {
        "main": [{"id": "m1", "source_job_id": sid, "in": 0.0, "out": 2.0, "burn_captions": False}],
        "overlay": list(overlays), "effects": list(effects), "text": [], "music": []}}


def _render(app_mod, tl):
    jid = uuid.uuid4().hex
    app_mod.jobs[jid] = {"status": "queued", "is_timeline": True, "timeline": tl}
    app_mod.render_timeline_job(jid, tl)
    job = app_mod.jobs.pop(jid)
    assert job["status"] == "done", job.get("error")
    return app_mod.OUTPUT_DIR / job["output"], job


def test_time_map_moves_everything_after_a_ramp(app_mod):
    tm = app_mod._TimeMap()
    tm.add(0, 1, 1.0)
    tm.add(1, 3, 2.0)   # two seconds played in one
    tm.add(3, 5, 1.0)
    assert tm(0.5) == pytest.approx(0.5)
    assert tm(2.0) == pytest.approx(1.5)
    assert tm(4.0) == pytest.approx(3.0)
    assert tm(6.0) == pytest.approx(5.0)
    assert not tm.identity
    clip = app_mod._tl_remap_clip({"start": 4.0, "out": 4.5}, tm, out_is_end=True)
    assert clip == {"start": 3.0, "out": 3.5}


def test_atempo_chain_stays_in_range(app_mod):
    assert app_mod._tl_atempo_chain(1.0) == ""
    assert app_mod._tl_atempo_chain(4.0) == "atempo=2.0,atempo=2.0000"
    assert app_mod._tl_atempo_chain(0.25) == "atempo=0.5,atempo=0.5000"


def test_speed_is_a_lane_effect(app_mod):
    tl = app_mod._normalize_timeline({"tracks": {"effects": [
        {"type": "speed", "start": 1, "out": 2, "speed": 9}]}})
    fx = tl["tracks"]["effects"][0]
    assert fx["speed"] == 4.0 and fx["smooth"] is False


@needs_ffmpeg
def test_a_speed_ramp_shortens_the_render_and_the_cache_reuses_it(app_mod, source_job):
    fx = [{"id": "s1", "type": "speed", "start": 0.5, "out": 1.0, "speed": 2.0}]
    tl = _timeline(source_job, effects=fx)
    out, job = _render(app_mod, tl)
    assert _dur(out) == pytest.approx(1.5, abs=0.12)
    first = job["render_stats"]
    assert first["misses"] + first["hits"] == 3

    dirty = app_mod.app.test_client().post("/timeline/dirty", json={"timeline": tl}).get_json()
    assert dirty["dirty_seconds"] == 0 and len(dirty["segments"]) == 3
    assert [s["speed"] for s in dirty["segments"]] == [1.0, 2.0, 1.0]

    tl2 = _timeline(source_job, effects=[dict(fx[0], speed=1.5)])
    d2 = app_mod.app.test_client().post("/timeline/dirty", json={"timeline": tl2}).get_json()
    assert [s["cached"] for s in d2["segments"]] == [True, False, True]

    _out, job2 = _render(app_mod, tl)
    assert job2["render_stats"]["hits"] == 3 and job2["render_stats"]["misses"] == 0


@needs_ffmpeg
def test_captions_follow_a_speed_ramp(app_mod, source_job, monkeypatch):
    seen = {}

    def fake_build_ass(words, *a, **k):
        seen["words"] = words
        return ""

    monkeypatch.setattr(app_mod, "build_ass", fake_build_ass)
    tl = _timeline(source_job, effects=[{"type": "speed", "start": 1.0, "out": 1.0, "speed": 2.0}])
    tl["tracks"]["main"][0]["burn_captions"] = True
    _render(app_mod, tl)
    starts = [round(w["start"], 2) for w in seen["words"]]
    # "two" at 1.2s source sits 0.2s into a 2x piece starting at 1.0 -> 1.1
    assert starts == [0.1, 1.1, 1.3]


@needs_ffmpeg
def test_behind_me_device_goes_under_the_speaker_matte(app_mod, source_job, monkeypatch, tmp_path):
    """"Behind me" uses the overlay pass's matte, and legacy behind_subject still counts."""
    calls = []

    def fake_matte(src, W, H, fps, dur=None, start=0.0, spans=None):
        # `spans` says which stretches actually need the person cut out.
        # Recorded so the caller can be held to passing them.
        calls.append((W, H, spans))
        out = tmp_path / "matte.mp4"
        # Left half "person", right half background.
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                        f"color=black:s={W}x{H}:r={fps}:d={float(dur or 3):.3f}",
                        "-vf", f"drawbox=x=0:y=0:w={W // 2}:h={H}:color=white:t=fill",
                        "-pix_fmt", "yuv420p", str(out)], check=True)
        return out

    monkeypatch.setattr(app_mod, "_tl_matte_video", fake_matte)
    info = app_mod._render_device_asset("kinetic_text", ["BEHIND"], 1.5, "9x16",
                                        ["#FF0000"], None, 30)
    ov = app_mod._device_overlay_clip(info, 0.2)
    ov.update({"x": 0, "y": 0, "w": 1.0, "h": 1.0, "behind_subject": True})
    out, _job = _render(app_mod, _timeline(source_job, overlays=[ov]))
    assert calls, "the behind overlay never asked for a matte"
    assert _dur(out) == pytest.approx(2.0, abs=0.12)


@needs_ffmpeg
def test_stitched_short_segments_stay_constant_frame_rate(app_mod, clip, tmp_path):
    """Many tiny segments (speed ramps, behind-me splits) must not drift off 30 fps."""
    import subprocess
    spans = [(0.0, 0.53), (0.53, 0.87), (0.87, 1.6), (1.6, 1.93)]
    segs = []
    for i, (a, b) in enumerate(spans):
        out = tmp_path / f"s{i}.mp4"
        d = app_mod._tl_normalize_segment(clip, a, b, 360, 640, 30, "cover", "#000000", out,
                                          speed=1.5 if i == 2 else 1.0)
        assert abs(d * 30 - round(d * 30)) < 1e-6
        segs.append((out, d))
    final = tmp_path / "main.mp4"
    total, _ = app_mod._tl_build_main_track(segs, [0] * len(segs), final, "cfrtest")
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-count_frames",
                            "-show_entries", "stream=r_frame_rate,nb_read_frames", "-of", "csv=p=0",
                            str(final)], capture_output=True, text=True).stdout.strip()
    rate, frames = probe.split(",")
    assert rate == "30/1"
    assert int(frames) == round(total * 30)


def _have(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


# --- the overlay pass: stop paying for frames that get thrown away --------
# Each overlay did its timing twice. `tpad=start_duration` GENERATED
# start*fps real frames at the overlay's size, and the `enable=` on the
# overlay filter then discarded every one of them. Measured on the shipping
# pass (14 overlays with Ken Burns over a 90s base): 39.9s -> 30.7s, and the
# sampled frames are identical.

def _ovl_graph(app_mod, start=20.0, length=3.0, **extra):
    """The filter chain one overlay contributes to the graph."""
    ov = {"asset_id": "a" * 32, "start": start, "in": 0, "out": length,
          "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.3, "fit": "contain",
          "fade_in": 0.2, "fade_out": 0.2, "opacity": 1.0}
    ov.update(extra)
    _inputs, filt, _cur, _idx = app_mod._tl_composite_overlay_step(
        ov, Path("x.png"), "image", 1, ["-i", "base.mp4"], [], "[0:v:0]", 1,
        start, 0.0, length, 1080, 1920, 30,
    )
    return "\n".join(filt)


def test_an_overlay_is_delayed_without_generating_frames(app_mod):
    graph = _ovl_graph(app_mod, start=20.0, length=3.0)
    assert "tpad=start_duration" not in graph, \
        "still generating 20s of padding frames that enable= throws away"
    assert "setpts=PTS+20.000/TB" in graph, "the overlay is no longer delayed"
    # enable= is now the ONLY thing doing the timing, so losing it would
    # draw every overlay across the whole video.
    assert "enable='between(t,20.000,23.000)'" in graph
    # The stream has no frames before 20s now, so say what overlay should do
    # instead of leaning on framesync's defaults.
    assert "eof_action=pass" in graph and "repeatlast=0" in graph


def test_the_delay_comes_after_the_fades(app_mod):
    """fade is written against the clip's own 0..length clock. Shifting the
    timestamps first would move the fade outside its window, and it would
    simply never be seen — a silent wrong result, not an error."""
    graph = _ovl_graph(app_mod, start=20.0, length=3.0)
    assert "fade=t=in" in graph and "fade=t=out" in graph
    assert graph.index("fade=t=in") < graph.index("setpts=PTS+20.000/TB")
    assert graph.index("fade=t=out") < graph.index("setpts=PTS+20.000/TB")


def test_an_overlay_at_zero_still_builds(app_mod):
    """start=0 meant tpad=0, a no-op. setpts+0 has to be equally harmless."""
    graph = _ovl_graph(app_mod, start=0.0, length=3.0)
    assert "enable='between(t,0.000,3.000)'" in graph
    assert "tpad=start_duration" not in graph


# --- the matte: cut the person out only where something sits behind them --
# A behind-overlay on a 90s edit asked for 2,670 segmented frames, blew the
# 20s deadline, wrote short, and was thrown away by the `written < limit - 2`
# guard — so "behind me" rendered on nothing longer than a few seconds.
# Measured: whole video -> None (dropped) after 20.0s; spans -> a full 90s
# matte in 12.6s with 162 frames actually cut out of 2,700.

def _stub_matte_deps(app_mod, monkeypatch, tmp_path, calls, frames):
    """Point _tl_matte_video at fakes so the frame choice can be counted."""
    import cv2
    import mediapipe as mp
    import numpy as np

    class _FakeSeg:
        def process(self, _frame):
            calls["n"] += 1

            class R:
                segmentation_mask = np.ones((4, 4), dtype="float32")
            return R()

        def close(self):
            pass

    class _FakeCap:
        def isOpened(self):
            return True

        def set(self, *_a):
            return True

        def get(self, *_a):
            return 0.0

        def read(self):
            frames["i"] += 1
            return True, np.zeros((8, 8, 3), dtype="uint8")

        def grab(self):
            frames["i"] += 1
            return True

        def release(self):
            pass

    monkeypatch.setattr(app_mod, "_segment_deps_available", lambda: (True, ""))
    monkeypatch.setattr(app_mod, "MATTE_DIR", tmp_path)
    monkeypatch.setattr(app_mod, "_matte_work_size", lambda _w, _h: (4, 4))
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_a, **_k: _FakeCap())
    monkeypatch.setattr(cv2, "resize", lambda _img, size: np.zeros(
        (size[1], size[0], 3), dtype="uint8"))
    monkeypatch.setattr(mp.solutions.selfie_segmentation,
                        "SelfieSegmentation", lambda **_k: _FakeSeg())


@pytest.mark.skipif(not _have("cv2") or not _have("mediapipe"),
                    reason="cv2/mediapipe not installed")
def test_the_matte_only_runs_the_model_where_it_is_needed(app_mod, monkeypatch,
                                                          tmp_path):
    calls, frames = {"n": 0}, {"i": 0}
    _stub_matte_deps(app_mod, monkeypatch, tmp_path, calls, frames)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x" * 64)

    # 10s at 30fps = 300 frames; one 1-second span needs about 30 of them.
    out = app_mod._tl_matte_video(src, 64, 64, 30, 10.0, spans=[(4.0, 5.0)])
    assert out is not None, "the matte was written short and thrown away"
    assert 20 <= calls["n"] <= 45, \
        f"segmented {calls['n']} frames for a 1s span inside a 10s clip"
    # Still FULL LENGTH: alphamerge pairs it frame-for-frame with the whole
    # base, so a short matte would misalign every behind-overlay.
    assert frames["i"] >= 300

    # spans=None keeps the old behaviour — every frame through the model.
    calls["n"] = 0
    frames["i"] = 0
    assert app_mod._tl_matte_video(src, 64, 64, 30, 10.0, spans=None) is not None
    assert calls["n"] >= 290, "without spans it must still matte everything"


@pytest.mark.skipif(not _have("cv2") or not _have("mediapipe"),
                    reason="cv2/mediapipe not installed")
def test_two_span_sets_cannot_share_one_cached_matte(app_mod, monkeypatch,
                                                     tmp_path):
    """Same clip, different spans: serving one from the other's cache shows a
    cut-out taken from the wrong moment, and it looks entirely plausible.

    Driven through the real function — an earlier version of this test
    recomputed the hash itself, so it would have passed with the spans
    dropped from the key.
    """
    calls, frames = {"n": 0}, {"i": 0}
    _stub_matte_deps(app_mod, monkeypatch, tmp_path, calls, frames)
    src = tmp_path / "c.mp4"
    src.write_bytes(b"y" * 64)

    made = []
    for spans in (None, [(1.0, 2.0)], [(5.0, 6.0)]):
        out = app_mod._tl_matte_video(src, 64, 64, 30, 10.0, spans=spans)
        assert out is not None, f"no matte for spans={spans}"
        made.append(out.name)
    assert len(set(made)) == 3, f"two span sets share a cache file: {made}"


@needs_ffmpeg
def test_only_the_behind_overlays_decide_the_matte_spans(app_mod, monkeypatch,
                                                         tmp_path):
    """A front overlay must not drag the segmenter across its window too.

    Driven through the real function rather than grepped: an earlier version
    of this test matched the word "front" in a nearby comment and would have
    passed on a caller that matted everything.
    """
    seen = {}

    def fake_matte(src, W, H, fps, dur=None, start=0.0, spans=None):
        seen["spans"] = spans
        out = tmp_path / "m.mp4"
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                        f"color=white:s={W}x{H}:r={fps}:d={float(dur or 2):.3f}",
                        "-pix_fmt", "yuv420p", str(out)], check=True)
        return out

    monkeypatch.setattr(app_mod, "_tl_matte_video", fake_matte)
    monkeypatch.setattr(app_mod, "_segment_deps_available", lambda: (True, ""))

    base = tmp_path / "base.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=360x640:r=30:d=12", "-pix_fmt", "yuv420p",
                    str(base)], check=True)
    pic = app_mod.ASSET_DIR / f"{uuid.uuid4().hex}.png"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=red:s=120x120:d=1", "-frames:v", "1", str(pic)],
                   check=True)

    def _ov(start, behind):
        return {"asset_id": pic.stem, "start": start, "in": 0, "out": 2.0,
                "x": 0.1, "y": 0.1, "w": 0.4, "h": 0.2, "fit": "contain",
                "opacity": 1.0, "behind": behind}

    try:
        app_mod._tl_composite_overlays(
            base, [_ov(2.0, True), _ov(8.0, False)], 360, 640,
            tmp_path / "out.mp4", fps=30)
    finally:
        pic.unlink(missing_ok=True)

    spans = seen.get("spans")
    assert spans, "the caller still mattes the whole video"
    assert len(spans) == 1, f"a front overlay widened the matte: {spans}"
    s0, s1 = spans[0]
    assert abs(s0 - 2.0) < 0.01 and abs(s1 - 4.0) < 0.01, spans
    # The front overlay sits at 8s; nothing may reach it.
    assert all(e <= 8.0 for _s, e in spans)


# --- where did the time actually go -------------------------------------

def test_a_render_records_how_long_each_stage_took(app_mod):
    """Three fixes in a row went at "why is this render slow" by adding more
    status text, and none recorded a duration — so a render that sat for 52
    minutes still could not say which pass ate it."""
    src = inspect.getsource(app_mod.render_timeline_job)
    assert "stage_times" in src
    # A stage can end in two places: _stage(), and the main-piece loop, which
    # keeps its own clock. Both have to close their books.
    assert src.count("stage_times.append(") >= 3, \
        "a stage can end in more than one place and each has to be recorded"
    assert "slowest_stages" in src and "render_seconds" in src, \
        "the summary never reaches the job, so only the log has it"
    # In `finally`, so a render that FAILS still says where its time went —
    # which is the case that prompted this.
    assert "slowest_stages" in src[src.index("finally:"):]


def test_a_finished_render_is_not_reopened_by_the_heartbeat(app_mod):
    """The heartbeat writes status every 2s until told to stop, and it was
    told in `finally` — after "done" was written. A tick landing in that gap
    put a stage name and a climbing second count back over the result, so a
    finished render looked parked forever: indistinguishable from the hang
    this clock exists to report."""
    src = inspect.getsource(app_mod.render_timeline_job)
    done_at = src.index('jobs[job_id]["status"] = "done"')
    stop_before = src.rindex('clock["stop"] = True', 0, done_at)
    assert stop_before < done_at, "the clock is still stopped after 'done'"
    # And a tick already past that check must not reopen the job either.
    assert 'in ("done", "error")' in src, \
        "the heartbeat will still overwrite a terminal status"


# --- a stalled overlay pass has to end ------------------------------------
# Seen in the wild: "drawing overlays · no new frame for 191s" pinned at
# exactly 70%, climbing, with nothing able to stop it. `clock["armed"]` was
# set False immediately before the overlay call, so the 45s watchdog was
# switched off for the one stage that hangs.

def test_the_overlay_stage_is_watched(app_mod):
    src = inspect.getsource(app_mod.render_timeline_job)
    ovl = src[src.index('_stage("drawing overlays", 70)'):]
    ovl = ovl[:ovl.index("# ---- Pass 3b")]
    assert 'clock["armed"] = True' in ovl, \
        "the stage that hangs still has its watchdog switched off"
    # Armed before the call, disarmed after, or the next stage inherits it.
    assert ovl.index('clock["armed"] = True') < ovl.index("_tl_composite_overlays("), \
        "armed only after the call it is meant to watch"
    assert 'clock["armed"] = False' in ovl, "the next stage inherits the watchdog"
    # A fresh tick, or the time spent in the previous stage counts against
    # this one and it is killed the moment it starts.
    assert 'clock["tick"] = time.time()' in ovl


def test_a_stalled_overlay_pass_does_not_take_the_export_with_it(app_mod):
    """Everything that matters is already rendered by this point — the cuts,
    the grade, the captions. Losing all of it to a B-roll graph is the wrong
    trade, and waiting forever is worse."""
    src = inspect.getsource(app_mod.render_timeline_job)
    ovl = src[src.index('_stage("drawing overlays", 70)'):]
    ovl = ovl[:ovl.index("# ---- Pass 3b")]
    assert "except Exception as ovl_err:" in ovl, \
        "a failing overlay pass still fails the whole render"
    assert "_tl_overlay_skipped.append" in ovl
    # And the finished job says which layer went missing, rather than the
    # overlays just silently not being there.
    assert "overlay_note" in src


def test_the_cut_out_reports_its_own_frames(app_mod):
    """The heartbeat only sees ffmpeg's out_time_ms, and the matte writes to
    a pipe. Arming the watchdog without this would kill a cut-out that is
    working perfectly."""
    msrc = inspect.getsource(app_mod._tl_matte_video)
    assert msrc.count("_matte_tick(span, written)") >= 2, \
        "the matte goes silent on the path that skips the model, or on both"
    tick = inspect.getsource(app_mod._matte_tick)
    assert '"tick"' in tick and "render_beat" in tick


def test_the_overlay_inputs_are_named_in_the_log(app_mod):
    """"drawing overlays" covers fourteen inputs; when it stalls there was
    nothing to say which one."""
    src = inspect.getsource(app_mod._tl_composite_overlays)
    assert "compositing %d overlay(s)" in src
    assert "(behind)" in src, "a behind overlay is the expensive kind and is not marked"


@needs_ffmpeg
def test_a_killed_overlay_pass_still_produces_the_video(app_mod, source_job,
                                                        monkeypatch, tmp_path):
    """The end-to-end guarantee, driven through the real render.

    When the watchdog kills a stalled overlay encode, _tl_run raises — which
    is simulated here. The export must still land, with the overlays named
    as the thing that was lost. Before this, the raise propagated and the
    whole render failed; before the watchdog, it never raised at all and the
    render simply never ended.
    """
    def boom(*_a, **_k):
        raise RuntimeError("Overlay composite failed (see ffmpeg_render.log)")

    monkeypatch.setattr(app_mod, "_tl_composite_overlays", boom)

    pic = app_mod.ASSET_DIR / f"{uuid.uuid4().hex}.png"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=red:s=120x120:d=1", "-frames:v", "1", str(pic)],
                   check=True)
    ov = {"asset_id": pic.stem, "start": 0.2, "in": 0, "out": 1.0,
          "x": 0.1, "y": 0.1, "w": 0.4, "h": 0.2, "fit": "contain",
          "opacity": 1.0}
    try:
        out, job = _render(app_mod, _timeline(source_job, overlays=[ov]))
    finally:
        pic.unlink(missing_ok=True)

    assert out.exists() and out.stat().st_size > 0, "the export was lost"
    assert _dur(out) == pytest.approx(2.0, abs=0.2)
    # And it says so rather than the overlays just quietly not being there.
    assert "overlay" in (job.get("overlay_note") or "").lower(), job.get("overlay_note")
