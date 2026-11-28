"""Telling someone why "behind me" is off, in words they can use.

The server worked out the exact reason in seven places and the person saw
"Cut-out unavailable on this server" -- which names an internal technique,
blames something they cannot see, and offers no action. The browser threw the
real reason away one line before it could be shown, and the render-time
explanation went into a job key that nothing read.
"""
import re
import unittest.mock as mock
from pathlib import Path

BURNER = Path(__file__).resolve().parent.parent
TIMELINE_JS = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
# Comments quote the code they replaced, so an absence check over the raw file
# matches its own explanation. Search the code.
TIMELINE_CODE = "\n".join(
    ln for ln in TIMELINE_JS.splitlines() if not ln.lstrip().startswith("//"))


def _flat(text: str) -> str:
    """Whitespace-normalised, so a line wrap in a prompt is not a failure."""
    return " ".join(text.split())

BROKEN = (False, "this mediapipe (0.10.26) has no solutions.selfie_segmentation -- "
                 "it was removed in 0.10.22.")


def _off(app_mod):
    return mock.patch.object(app_mod, "_segment_deps_available", lambda: BROKEN)


def test_the_message_has_no_jargon_and_tells_them_what_happens(app_mod):
    with _off(app_mod):
        st = app_mod._behind_me_status()
    msg = st["message"]
    assert st["ok"] is False
    for jargon in ("cut-out", "cutout", "matte", "segmentation", "mediapipe", "server error"):
        assert jargon not in msg.lower(), f"{jargon!r} means nothing to a non-technical user"
    assert "not" in msg.lower() and "your" in msg.lower(), "it does not say this is not their fault"
    assert "render" in msg.lower(), "it does not say what happens to their export"
    # and the engineering sentence is kept for whoever has to fix the box
    assert "mediapipe" in st["detail"]
    assert st["fixable_by_user"] is False


def test_a_working_server_says_nothing_at_all(app_mod):
    st = app_mod._behind_me_status()
    assert st["ok"] is True and st["message"] == ""


def test_the_endpoint_answers_before_anything_is_tried(app_mod):
    c = app_mod.app.test_client()
    assert c.get("/can/behind-me").get_json()["ok"] is True
    with _off(app_mod):
        body = c.get("/can/behind-me").get_json()
    assert body["ok"] is False and body["message"] and body["detail"]


def test_the_preview_endpoint_sends_the_sentence_not_just_a_status(app_mod):
    """timeline.js used to `throw new Error("matte " + res.status)`, so the
    body was discarded one line before it could be read."""
    app_mod.jobs["bmJob"] = {"status": "done"}
    try:
        with _off(app_mod):
            r = app_mod.app.test_client().get("/segment-preview/bmJob.png")
        assert r.status_code == 400
        body = r.get_json()
        assert body["reason"] == "unavailable"
        assert "switched off" in body["message"]
        assert "mediapipe" in body["detail"], "the fixer's reason was dropped"
    finally:
        app_mod.jobs.pop("bmJob", None)


def test_the_browser_reads_the_body_and_separates_the_causes():
    assert 'throw new Error("matte ' not in TIMELINE_CODE, "the reason is still discarded"
    # and it positively reads the body before saying anything
    handler = TIMELINE_CODE[TIMELINE_CODE.index("segment-preview"):][:1200]
    assert "res.json()" in handler, "the body is never parsed"
    assert "backgroundFailureMessage" in TIMELINE_JS
    fn = TIMELINE_JS[TIMELINE_JS.index("function backgroundFailureMessage"):][:1400]
    assert "body.message" in fn, "the server's own words are not preferred"
    assert "404" in fn and "500" in fn, "causes are not separated"
    # the three outcomes must not all read the same
    assert "no longer on the server" in fn        # the clip is gone
    assert "temporary" in TIMELINE_CODE           # a blip is retryable
    assert "Cut-out unavailable on this server" not in TIMELINE_CODE


def test_a_render_that_dropped_the_background_says_so(app_mod):
    """background_note went to the client inside /status all along with
    nothing reading it, so the background silently was not in the file."""
    src = (BURNER / "app.py").read_text(encoding="utf-8")
    assert "background_note_plain" in src, "no plain sentence is recorded"
    assert "s.background_note_plain || s.background_note" in TIMELINE_CODE, \
        "the render-time reason is still unread"
    # and it is shown where someone is actually looking: the Done message
    done = TIMELINE_CODE[TIMELINE_CODE.index("Done ✓"):][:400]
    assert "setBackgroundNote(bgMissing)" in done


def test_the_controls_are_not_offered_when_they_cannot_work():
    assert "/can/behind-me" in TIMELINE_CODE, "the client never asks"
    assert "devicePickerBehind" in TIMELINE_CODE and "disabled = true" in TIMELINE_CODE
    pc = TIMELINE_CODE[TIMELINE_CODE.index("function propCheck"):][:700]
    assert 'key === "behind"' in pc, "the overlay checkbox is still offered"
    assert "disabled" in pc
    # an unreachable capability endpoint must not disable a working feature
    assert re.search(r"catch\(\(\) => \{\}\);\s*//.*never block", TIMELINE_JS), \
        "a network blip would switch the feature off"


def test_the_coeditor_stops_promising_it(app_mod, temp_db, monkeypatch):
    seen = {}
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions",
                        lambda p: seen.update(prompt=p) or {"ops": [], "message": "ok"})
    tl = {"canvas": "9x16", "fps": 30, "style": {}, "speaker_colors": {}, "tracks": {
        "main": [], "overlay": [], "text": [], "effects": [], "music": []}}

    app_mod.app.test_client().post("/co-editor", json={"prompt": "put it behind me", "timeline": tl})
    assert "BEHIND THE SPEAKER IS UNAVAILABLE" not in _flat(seen["prompt"]), \
        "it warns even when the feature works"

    seen.clear()
    with _off(app_mod):
        app_mod.app.test_client().post("/co-editor", json={"prompt": "put it behind me", "timeline": tl})
    p = _flat(seen["prompt"])
    assert "BEHIND THE SPEAKER IS UNAVAILABLE" in p
    assert 'Never set "behind": true' in p and "never emit set_behind" in p
    assert "not their fault" in p and "in FRONT instead" in p


def test_the_capability_answer_stops_claiming_it(app_mod):
    assert "Put anything behind you" in app_mod._co_editor_capability_message()
    with _off(app_mod):
        msg = app_mod._co_editor_capability_message()
    assert "Put anything behind you" not in msg
    assert "switched off on this server" in msg
    assert "in front" in msg, "it does not say what still works"
