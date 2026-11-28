"""The cuts and the pauses, reaching the model that places graphics.

Both were already measured and neither arrived. _detect_shots_ffmpeg existed
for the reference analyser and a UI button; the pause pass scanned word gaps
for its own purposes. The prompt that decides where a graphic goes saw
neither, so the model could not land a move on a cut -- it did not know where
the cuts were.
"""
import re
import uuid


def _words(text: str, *, gap_after: dict | None = None, step: float = 0.4) -> list:
    out, t = [], 0.0
    for i, w in enumerate(text.split()):
        out.append({"word": w, "start": round(t, 3), "end": round(t + 0.3, 3)})
        t += step + (gap_after or {}).get(i, 0.0)
    return out


def test_pauses_come_out_of_the_word_timings_not_out_of_ffmpeg(app_mod):
    """No decode, no silencedetect: Whisper already knows."""
    w = _words("skill alone is not enough you also need demand",
               gap_after={4: 1.6, 7: 0.9})
    gaps = app_mod._speech_gaps(w)
    assert [g["start"] for g in gaps] == [1.9, 4.7]
    # Plain arithmetic rather than pytest.approx: this file can be the first
    # to touch numpy, and approx trips over a partially initialised one.
    assert abs(gaps[0]["seconds"] - 1.7) < 0.01
    # a steady reader has none, and gets no markers at all
    assert app_mod._speech_gaps(_words("one two three four five")) == []


def test_only_the_long_pauses_travel_and_they_travel_in_order(app_mod):
    many = {i: 0.5 + i * 0.1 for i in range(30)}
    gaps = app_mod._speech_gaps(_words(" ".join(f"w{i}" for i in range(40)), gap_after=many))
    assert len(gaps) == 14, "the prompt would be mostly pause markers"
    assert [g["start"] for g in gaps] == sorted(g["start"] for g in gaps)
    # the longest ones are the ones kept
    assert min(g["seconds"] for g in gaps) > 1.0


def test_a_transcript_with_nothing_to_mark_is_unchanged(app_mod):
    """Eleven call sites read this function. A legend or a blank marker line
    on a transcript with no cuts and no pauses would change every one."""
    flat = _words("one two three four")
    assert app_mod._format_transcript_for_llm(flat) == "[00:00] one two three four"
    assert app_mod._format_transcript_for_llm(flat, cuts=[]) == "[00:00] one two three four"


def test_a_cut_inside_a_pause_prints_inside_the_pause(app_mod):
    """Emitting markers as chunks were written put a cut that falls in a
    silent gap AFTER the chunk that follows the gap -- the wrong side of the
    pause, which is the one thing the model needs to get right."""
    w = _words("skill alone is not enough you also need demand", gap_after={4: 1.6})
    out = app_mod._format_transcript_for_llm(w, cuts=[2.6]).splitlines()
    body = [ln for ln in out if not ln.startswith("(")]
    assert body[0].startswith("[00:00] skill alone")
    assert "pause" in body[1]
    assert "cut at 2.6s" in body[2]
    assert body[3].startswith("[00:03] you also need")


def test_the_markers_are_explained_and_the_rule_is_stated(app_mod):
    out = app_mod._format_transcript_for_llm(
        _words("one two three four", gap_after={1: 1.5}))
    first = out.splitlines()[0]
    assert first.startswith("("), "the markers arrive with no legend"
    assert "silence" in first and "shot change" in first
    assert "never across" in first, "the rule the markers exist for is missing"


def test_a_marker_cannot_break_the_quote_snapping(app_mod):
    """Item 1 matches the model's quote against the word list. If the model
    folds a marker into a quote the match has to survive it."""
    w = _words("skill alone is not enough you also need demand", gap_after={4: 1.6})
    raw = [{"effect": "zoom", "quote": "~~ 1.7s pause ~~ you also need demand",
            "start_time": 0.0, "end_time": 1.0}]
    out, moved = app_mod._snap_suggestions_to_quotes(raw, w)
    assert moved == 1, "a quote carrying a marker stopped matching"
    assert abs(out[0]["start_time"] - 3.6) < 0.05


def test_the_cuts_are_computed_once_and_never_inside_a_turn(app_mod, monkeypatch):
    """A whole-file decode is ~20s of ffmpeg. The plan's own constraint was
    that none of these wires may make the tool slower."""
    jid = "cuts" + uuid.uuid4().hex[:6]
    calls = []
    monkeypatch.setattr(app_mod, "find_video_path", lambda j: "/tmp/x.mp4")
    monkeypatch.setattr(app_mod, "_detect_shots_ffmpeg",
                        lambda *a, **k: calls.append(1) or [1.0, 4.0])
    app_mod.jobs[jid] = {"status": "awaiting_edit"}
    try:
        assert app_mod._job_shot_cuts(jid) == [], "it decoded during the request"
        app_mod._warm_shot_cuts(jid)
        assert app_mod._job_shot_cuts(jid) == [1.0, 4.0]
        app_mod._warm_shot_cuts(jid)
        assert len(calls) == 1, "it decoded the same file twice"
    finally:
        app_mod.jobs.pop(jid, None)


def test_a_failed_scan_costs_nothing_and_says_nothing(app_mod, monkeypatch):
    jid = "cutsE" + uuid.uuid4().hex[:6]
    monkeypatch.setattr(app_mod, "find_video_path", lambda j: "/tmp/x.mp4")
    monkeypatch.setattr(app_mod, "_detect_shots_ffmpeg",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no ffmpeg")))
    app_mod.jobs[jid] = {"status": "awaiting_edit"}
    try:
        app_mod._warm_shot_cuts(jid)          # must not raise
        assert app_mod._job_shot_cuts(jid) == []
    finally:
        app_mod.jobs.pop(jid, None)


def test_the_coeditor_sees_cuts_in_output_time_including_the_joins(app_mod, temp_db, monkeypatch):
    """Two clips from one source, trimmed and butted together. A source cut
    has to move with the trim, a cut in discarded footage has to vanish, and
    the join between the two pieces is itself a cut nothing could detect."""
    seen = {}
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions",
                        lambda prompt: seen.update(prompt=prompt) or {"ops": [], "message": "ok"})
    words = [{"word": f"w{i}", "start": i * 1.0, "end": i * 1.0 + 0.5} for i in range(30)]
    monkeypatch.setitem(app_mod.jobs, "src1",
                        {"words": words, "shot_cuts": [3.0, 12.0, 25.0]})
    tl = {"canvas": "9x16", "fps": 30, "speaker_colors": {}, "style": {}, "tracks": {
        "main": [{"id": "a", "source_job_id": "src1", "in": 2, "out": 8},
                 {"id": "b", "source_job_id": "src1", "in": 20, "out": 26}],
        "overlay": [], "text": [], "effects": [], "music": []}}
    app_mod.app.test_client().post("/co-editor", json={
        "prompt": "put a card on a cut", "timeline": tl})
    p = seen["prompt"]
    # The exact set, not spot checks. A leaked cut is still put through the
    # mapping, so it surfaces at some OTHER number -- checking that the
    # source time is absent proves nothing, which is how an earlier version
    # of this test passed while trimmed-away cuts leaked through.
    got = sorted(float(m) for m in re.findall(r">\|< cut at (-?[\d.]+)s", p))
    assert got == [1.0, 6.0, 11.0], (
        "expected: 3.0 trimmed to 1.0, the join at 6.0, and 25.0 trimmed to "
        f"11.0 -- with 12.0 dropped as trimmed-away footage. got {got}")
    assert all(t >= 0 for t in got), "a cut mapped to before the video starts"
    assert "shot change" in p, "the legend did not travel"


def test_suggest_effects_is_handed_the_cuts_too(app_mod, temp_db, monkeypatch):
    jid = "sfx" + uuid.uuid4().hex[:6]
    seen = {}
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions",
                        lambda prompt: seen.update(prompt=prompt) or {"effects": []})
    app_mod.jobs[jid] = {
        "status": "done",
        "words": [{"word": f"w{i}", "start": i * 1.0, "end": i * 1.0 + 0.5} for i in range(20)],
        "shot_cuts": [7.5],
    }
    try:
        app_mod.app.test_client().post("/suggest-effects", json={"job_id": jid})
        assert "cut at 7.5s" in seen.get("prompt", ""), "placement still cannot see the cuts"
    finally:
        app_mod.jobs.pop(jid, None)


def test_the_detect_shots_button_leaves_its_answer_behind(app_mod, temp_db, monkeypatch):
    """It already paid for a whole-file decode; throwing the result away meant
    the next prompt paid for it again, or went without."""
    jid = "btn" + uuid.uuid4().hex[:6]
    monkeypatch.setattr(app_mod, "find_video_path", lambda j: "/tmp/x.mp4")
    monkeypatch.setattr(app_mod, "_detect_shots_ffmpeg", lambda *a, **k: [2.0, 9.0])
    app_mod.jobs[jid] = {"status": "done",
                         "words": [{"word": "a", "start": 0, "end": 12.0}]}
    try:
        app_mod.app.test_client().post("/detect-shots", json={"job_id": jid})
        assert app_mod._job_shot_cuts(jid) == [2.0, 9.0]
        # a windowed scan is NOT the whole file, so it must not pretend to be
        app_mod.jobs[jid].pop("shot_cuts")
        app_mod.app.test_client().post("/detect-shots",
                                       json={"job_id": jid, "in": 1, "out": 5})
        assert app_mod._job_shot_cuts(jid) == []
    finally:
        app_mod.jobs.pop(jid, None)
