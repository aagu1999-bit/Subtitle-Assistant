"""Speed ramps, behind-subject graphics and the segment cache, through real renders."""
import shutil
import subprocess
import uuid

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

    def fake_matte(src, W, H, fps, dur=None, start=0.0):
        calls.append((W, H))
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
