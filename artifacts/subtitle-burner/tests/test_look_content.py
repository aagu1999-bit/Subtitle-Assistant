"""Looks that receive what the user actually said.

The whole channel from the model to a Remotion look was _quoted_look_text:
one regex, the first quoted phrase, capped at 48 characters. So

    a profile card for "Monita Kai", she runs a studio in Lagos,
    400k revenue, @monitakai

reached the component as {"text": "Monita Kai"} and everything else was
discarded. The component then had to fake the rest -- and the numbers it
faked went on screen looking like data.
"""
import re
from pathlib import Path

BURNER = Path(__file__).resolve().parent.parent
LOOKS = BURNER.parent / "remotion-overlays" / "src" / "looks"

PROFILE = {"id": "ProfileCard", "props": "text", "fields": {
    "name": {"type": "text", "max": 40},
    "handle": {"type": "text", "max": 30},
    "views": {"type": "text", "max": 12},
    "likes": {"type": "text", "max": 12},
    "avatar": {"type": "picture"},
}}


def _content(app_mod, props):
    """Just the look's own keys, without the shared design tokens."""
    return {k: v for k, v in props.items() if k not in app_mod.tokens.as_props()}


def test_a_look_with_no_fields_is_completely_unchanged(app_mod):
    """Forty-two recipes go through this function. Only the ones that opt in
    may behave differently, or this change is a rewrite of all of them."""
    rec = {"id": "SplitText", "props": "text"}
    before = app_mod._remotion_look_props(rec, 'say "HELLO"', None)
    after = app_mod._remotion_look_props(rec, 'say "HELLO"', None,
                                         {"name": "ignored", "anything": 1})
    assert before == after, "a recipe without fields was affected by content"


def test_the_named_content_reaches_the_look(app_mod):
    got = _content(app_mod, app_mod._remotion_look_props(
        PROFILE, "a profile card", None,
        {"name": "Monita Kai", "handle": "@monitakai", "views": "412k"}))
    assert got["name"] == "Monita Kai"
    assert got["handle"] == "@monitakai"
    assert got["views"] == "412k"


def test_a_key_the_recipe_never_declared_is_dropped(app_mod):
    """Model output is rendered into a video the user publishes, so this is
    a gate and not a merge."""
    got = _content(app_mod, app_mod._remotion_look_props(
        PROFILE, "a profile card", None,
        {"name": "Ada", "bio": "not declared", "onclick": "alert(1)"}))
    assert "bio" not in got and "onclick" not in got
    assert got["name"] == "Ada"


def test_text_is_cut_to_the_length_the_recipe_asked_for(app_mod):
    got = _content(app_mod, app_mod._remotion_look_props(
        PROFILE, "x", None, {"name": "A" * 500, "views": "9" * 50}))
    assert len(got["name"]) == 40
    assert len(got["views"]) == 12


def test_a_picture_field_never_takes_a_web_address(app_mod, monkeypatch):
    """A raw URL from model output must never reach <Img src=...>. The value
    is a subject or an asset id, resolved through our own staging."""
    seen = {}

    def fake_stage(queries, want=3, **kw):
        seen["queries"] = list(queries)
        seen["generate"] = kw.get("generate")
        return ([{"id": "a1", "src": "staged/a1.png", "kind": "image"}], [])

    monkeypatch.setattr(app_mod, "_remotion_stage_photos", fake_stage)
    got = _content(app_mod, app_mod._remotion_look_props(
        PROFILE, "x", None, {"avatar": "https://evil.example/pic.png"}))
    assert got["avatar"] == "staged/a1.png", "the model's string reached the component"
    assert seen["queries"] == ["https://evil.example/pic.png"], "it was not staged at all"
    assert seen["generate"] is False, "a missing picture would be invented"


def test_a_picture_that_cannot_be_found_is_simply_absent(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "_remotion_stage_photos", lambda q, **k: ([], list(q)))
    got = _content(app_mod, app_mod._remotion_look_props(
        PROFILE, "x", None, {"name": "Ada", "avatar": "a logo nobody has"}))
    assert "avatar" not in got and got["name"] == "Ada"


def test_staging_blowing_up_does_not_lose_the_rest(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "_remotion_stage_photos",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no provider")))
    got = _content(app_mod, app_mod._remotion_look_props(
        PROFILE, "x", None, {"name": "Ada", "avatar": "x"}))
    assert got["name"] == "Ada" and "avatar" not in got


def test_the_op_carries_content_through_to_the_look(app_mod, monkeypatch):
    """Same shape as the `photos` array, which was the only precedent."""
    seen = {}
    monkeypatch.setattr(app_mod, "_make_animation",
                        lambda *a, **k: seen.update(k) or (_ for _ in ()).throw(
                            RuntimeError("stop here")))
    app_mod._resolve_add_animation(
        {"op": "add_animation", "description": "a profile card",
         "content": {"name": "Ada"}, "start": 1.0}, {"canvas": "9x16"})
    assert seen.get("content") == {"name": "Ada"}, "the op dropped it"


def test_a_non_dict_content_is_ignored_rather_than_crashing(app_mod):
    for junk in ("a string", 42, ["a", "list"], None):
        got = app_mod._remotion_look_props(PROFILE, "x", None, junk)
        assert "name" not in _content(app_mod, got)


def test_the_model_is_told_which_looks_take_content(app_mod):
    block = app_mod._look_fields_prompt()
    assert "ProfileCard: name, handle, views, likes, avatar(picture)" in block
    assert "NEVER invent a number" in block, "nothing stops it filling the gaps"
    assert "never a web address" in block
    # generated from the recipes, so it cannot advertise a field the gate drops
    declared = {r["id"] for r in app_mod.REMOTION_RECIPES if r.get("fields")}
    for rid in declared:
        assert rid in block, f"{rid} declares fields and is not in the prompt"


def test_the_block_actually_reaches_the_prompt(app_mod, temp_db, monkeypatch):
    """Calling _look_fields_prompt() proves the function works, not that it is
    wired in -- an earlier version of the test above passed with the block
    deleted from the prompt entirely."""
    seen = {}
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions",
                        lambda p: seen.update(prompt=p) or {"ops": [], "message": "ok"})
    tl = {"canvas": "9x16", "fps": 30, "style": {}, "speaker_colors": {},
          "tracks": {"main": [], "overlay": [], "text": [], "effects": [], "music": []}}
    app_mod.app.test_client().post("/co-editor", json={
        "prompt": "add a profile card", "timeline": tl})
    p = seen["prompt"]
    assert "LOOKS THAT TAKE NAMED CONTENT" in p, "the model never sees the fields"
    assert "ProfileCard: name, handle" in p
    assert "NEVER invent a number" in p
    # and an example it can copy the shape from
    assert '"content": {"name": "Monita Kai"' in p


def test_no_look_renders_a_statistic_it_was_not_given():
    """ProfileCard drew the literals "124k views" and "8.2k likes" as if they
    were this person's real figures, in a video they publish. A number on
    screen has to come from a prop or not be there."""
    metric = re.compile(r'"[\d.]+[kKmM]"')
    bad = []
    for f in sorted(LOOKS.rglob("*.tsx")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("//"):
                continue
            for hit in metric.findall(line):
                bad.append(f"{f.relative_to(LOOKS)}:{n}: {hit}")
    assert not bad, "invented statistics rendered as if real:\n" + "\n".join(bad)


def test_the_card_draws_nothing_it_was_not_given():
    src = (LOOKS / "bits" / "cards.tsx").read_text(encoding="utf-8")
    body = src[src.index("ProfileCardLook"):][:2600]
    assert "124k" not in body and "8.2k" not in body
    assert "stats.length ?" in body, "the stats row is drawn even when empty"
    assert "handle ?" in body, "a handle is still being derived from the name"
    assert 'text.split(" ")[0]' not in body, "the fake handle is still there"
    assert "avatar ?" in body, "a real avatar cannot be used"


def test_the_op_examples_are_valid_json(app_mod, temp_db, monkeypatch):
    """The one place the exact output format is shown was showing the wrong
    one. `_op_examples` doubled its braces for an f-string, but f-string
    escaping applies to the literal template and never to an interpolated
    value -- so every example reached the model as `{{"op": ...}}`.

    It still produced valid ops, which is why nobody noticed.
    """
    import json as _json
    seen = {}
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions",
                        lambda p: seen.update(prompt=p) or {"ops": [], "message": "ok"})
    tl = {"canvas": "9x16", "fps": 30, "style": {}, "speaker_colors": {},
          "tracks": {"main": [], "overlay": [], "text": [], "effects": [], "music": []}}
    app_mod.app.test_client().post("/co-editor", json={"prompt": "add something", "timeline": tl})
    p = seen["prompt"]
    assert "{{" not in p and "}}" not in p, "the prompt still carries doubled braces"

    # The examples list itself, not lines that merely look like one: the
    # template also carries inline prose examples that legitimately end in a
    # full stop, and an earlier version of this test tripped over one.
    assert len(app_mod.CO_EDITOR_OP_EXAMPLES) >= 10
    for ex in app_mod.CO_EDITOR_OP_EXAMPLES:
        _json.loads(ex)              # the source must be valid JSON
        assert ex in p, f"an example did not survive into the prompt: {ex[:60]}"


# ---- a bug in our own call is not an engine being unavailable ---------------

def test_a_signature_mismatch_is_recorded_as_a_bug_not_as_unavailable(app_mod, monkeypatch, caplog):
    """_make_animation catches Exception around every engine call, so a
    TypeError from our own signature drift became the same silent walk down
    to motion.py as a missing Chrome -- which is how the content change in
    #66 broke three stubs at once and sent me looking in the wrong place.

    It still falls back: the person keeps their video. What changed is that
    the data says which of the two it was.
    """
    import logging
    monkeypatch.setattr(app_mod, "_match_named_look",
                        lambda d: ("remotion", {"id": "GlassSurface", "dur": 3.0, "place": "full"}))
    monkeypatch.setattr(app_mod, "_design_animation", lambda *a, **k: {"duration": 1.0})
    monkeypatch.setattr(app_mod, "_render_motion_asset",
                        lambda spec, desc, canvas, fps=30, fell_back_from=None: {
                            "asset_id": "m", "kind": "video", "cached": False,
                            "duration": 1.0, "box": {},
                            "device": {"engine": "motion", "fell_back_from": fell_back_from}})

    # an engine that genuinely cannot run
    monkeypatch.setattr(app_mod, "_make_remotion_look",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no browser")))
    out = app_mod._make_animation("a glass surface", {"canvas": "9x16"})
    assert out["device"]["fell_back_from"]["kind"] == "unavailable"

    # a bug in our own call, which must not read the same
    def wrong_signature(rec, desc, tl):          # one argument short
        return {}
    monkeypatch.setattr(app_mod, "_make_remotion_look", wrong_signature)
    with caplog.at_level(logging.ERROR):
        out = app_mod._make_animation("a glass surface", {"canvas": "9x16"})
    fb = out["device"]["fell_back_from"]
    assert fb["kind"] == "bug", "a signature mismatch still reads as 'engine unavailable'"
    assert "TypeError" in fb["why"]
    # and it is loud, with a traceback, not a one-line warning
    assert any(r.levelno >= logging.ERROR and "BUG" in r.message for r in caplog.records), \
        "a bug in our own code was logged no louder than a missing browser"
    assert any(r.exc_info for r in caplog.records), "logged without a traceback"
    # the person still gets their video
    assert out["device"]["engine"] == "motion"
