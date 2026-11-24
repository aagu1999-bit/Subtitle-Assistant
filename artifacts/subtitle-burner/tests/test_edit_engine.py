"""The edit decision engine: roles, prosody, concepts, variety, schemas, OTIO."""
import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import edit_engine as ee  # noqa: E402


def words_from(text_lines):
    """[(start, "sentence")] -> word dicts spaced 0.3s apart."""
    out = []
    for start, line in text_lines:
        t = start
        for tok in line.split():
            out.append({"word": tok, "start": round(t, 3), "end": round(t + 0.25, 3)})
            t += 0.3
    return out


TALK = words_from([
    (0.0, "Stop scrolling, this changed everything."),
    (6.0, "There are three reasons this works."),
    (10.0, "Revenue jumped 40% in a single month."),
    (14.0, "Last week when I walked into the office I was drowning in spreadsheets."),
    (20.0, "Freelancing versus a salary is the real choice."),
    (24.0, "She texted me at midnight asking for the file."),
    (28.0, "Here's the thing: consistency beats talent."),
])


def test_roles_cover_the_rhetorical_moves():
    spans = ee.classify_spans(TALK, 0, 40)
    roles = [s["role"] for s in spans]
    assert roles[0] == "hook"
    assert "list" in roles
    assert "data" in roles
    assert "story" in roles
    assert "comparison" in roles
    assert roles[-1] == "thesis"


def test_numbers_get_a_device_and_stories_get_broll():
    plan = ee.plan_edit(TALK, 0, 40, intensity="high")
    by_role = {}
    for row in plan["structure"]["spans"]:
        by_role.setdefault(row["role"], []).append(row)
    assert by_role["data"][0]["treatment"] == "device:counter"
    assert by_role["data"][0]["device"]["labels"][0].startswith("40")
    assert by_role["list"][0]["treatment"] == "device:step_tracker"
    assert by_role["comparison"][0]["treatment"] == "device:venn"
    assert by_role["story"][0]["treatment"] == "broll"
    assert by_role["thesis"][0]["treatment"] == "clean_zoom"


def test_device_cues_beat_generic_roles():
    plan = ee.plan_edit(TALK, 0, 40, intensity="high")
    texted = [r for r in plan["structure"]["spans"] if "texted" in r["text"]][0]
    assert texted["treatment"] == "device:chat_bubbles"


def test_story_broll_searches_the_feeling_not_the_noun():
    cq = ee.concept_queries("I was drowning in spreadsheets")
    assert not any("drown" in q for q in cq["queries"])
    assert "late-night overwhelm" in cq["concepts"]


def test_generative_prompts_refuse_people_hands_and_text():
    p = ee.abstract_prompt("my team shook hands and signed the contract", ["#112233"])
    assert ee.generative_prompt_ok(p), p
    assert not ee.generative_prompt_ok("a woman holding a sign")


def test_variety_never_repeats_back_to_back():
    st = ee.VarietyState()
    st.push("punch", 1.0)
    assert not st.allows("punch", 2.0)
    assert st.allows("broll", 2.0)
    st.push("device:venn", 3.0)
    st.push("broll", 4.0)
    # A second device inside the window is fine, a third of one family is not.
    st.push("device:counter", 5.0)
    assert not st.allows("device:step_tracker", 6.0)
    assert st.allows("device:step_tracker", 30.0)


def test_planner_never_places_the_same_treatment_twice_in_a_row():
    many = words_from([(i * 4.0, f"Revenue grew {i}0% that quarter.") for i in range(1, 9)])
    plan = ee.plan_edit(many, 0, 40, intensity="high")
    used = [r["treatment"] for r in plan["structure"]["spans"] if r["treatment"] != "none"]
    for a, b in zip(used, used[1:]):
        assert a != b, used


def test_taste_avoid_steers_the_choice():
    plan = ee.plan_edit(TALK, 0, 40, intensity="high", taste={"avoid": ["broll"]})
    story = [r for r in plan["structure"]["spans"] if r["role"] == "story"][0]
    assert story["treatment"] != "broll"


def test_prosody_finds_the_loud_word_and_the_breaths():
    np = pytest.importorskip("numpy")
    sr = 8000
    ws = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.3}
          for i, w in enumerate("this is the MOST important part of it".split())]
    ws[5]["start"] += 0.5  # a breath before "part"
    ws[5]["end"] += 0.5
    sig = np.zeros(int(sr * 5), dtype="float32")
    for i, w in enumerate(ws):
        amp = 0.8 if w["word"] == "MOST" else 0.1
        a, b = int(w["start"] * sr), int(w["end"] * sr)
        sig[a:b] = amp * np.sin(np.linspace(0, 400 * math.pi, b - a))
    pr = ee.prosody_from_samples(sig, sr, ws)
    assert [e["word"] for e in pr["emphasis"]] == ["MOST"]
    assert pr["pauses"] and 0.4 <= pr["pauses"][0]["gap"] <= 0.8
    assert pr["wpm"] > 0


def test_structure_and_style_are_separate_documents():
    plan = ee.plan_edit(TALK, 0, 40, style_pack={"label": "Pulse", "style": {"font": "Anton"}})
    assert "spans" in plan["structure"] and "caption" not in plan["structure"]
    assert plan["style"]["caption"]["font"] == "Anton" and "spans" not in plan["style"]
    tl = {"canvas": "9x16", "style": {"font": "Anton"}, "speaker_colors": {"A": "#fff"},
          "tracks": {"main": [{"id": "a", "in": 0, "out": 3}]}}
    structure, style = ee.split_timeline(tl)
    assert "style" not in structure and "tracks" not in style
    assert ee.merge_timeline(structure, style) == tl


def test_otio_round_trips_our_timeline():
    tl = {
        "canvas": "9x16", "fps": 30, "style": {"font": "Anton"},
        "tracks": {
            "main": [{"id": "m1", "source_job_id": "j1", "in": 1.0, "out": 9.0, "cuts": [[3.0, 4.0]]}],
            "overlay": [{"id": "o1", "asset_id": "a" * 32, "start": 2.0, "in": 0, "out": 2.5,
                         "device": {"kind": "venn"}}],
            "effects": [{"id": "e1", "type": "punch_zoom", "start": 1, "out": 1}],
            "text": [{"id": "t1", "text": "Hi", "start": 0, "out": 2}],
            "music": [],
        },
    }
    doc = ee.timeline_to_otio(tl, source_paths={"j1": "/tmp/j1.mp4"})
    json.dumps(doc)  # must be plain JSON
    assert doc["OTIO_SCHEMA"] == "Timeline.1"
    main = doc["tracks"]["children"][0]["children"]
    assert len(main) == 2, "the cut splits the clip into two keep ranges"
    assert main[0]["media_reference"]["target_url"] == "file:///tmp/j1.mp4"
    assert main[0]["source_range"]["duration"]["value"] == pytest.approx(60.0)
    back = ee.otio_to_timeline(doc)
    assert back["tracks"]["main"] == tl["tracks"]["main"]
    assert back["tracks"]["overlay"] == tl["tracks"]["overlay"]
    assert back["tracks"]["effects"] == tl["tracks"]["effects"]
    assert back["tracks"]["text"] == tl["tracks"]["text"]
    assert back["style"] == tl["style"]


def test_otio_imports_a_foreign_file_as_relinkable_clips():
    doc = {"OTIO_SCHEMA": "Timeline.1", "tracks": {"OTIO_SCHEMA": "Stack.1", "children": [
        {"OTIO_SCHEMA": "Track.1", "kind": "Video", "children": [
            {"OTIO_SCHEMA": "Clip.1", "name": "A001",
             "source_range": {"start_time": {"rate": 24, "value": 48},
                              "duration": {"rate": 24, "value": 96}},
             "media_reference": {"target_url": "file:///media/A001.mov"}}]}]}}
    tl = ee.otio_to_timeline(doc)
    c = tl["tracks"]["main"][0]
    assert c["in"] == pytest.approx(2.0) and c["out"] == pytest.approx(6.0)
    assert c["relink"].endswith("A001.mov")


def test_llm_role_refinement_ignores_garbage():
    spans = ee.classify_spans(TALK, 0, 40)
    before = [s["role"] for s in spans]
    ee.refine_roles_with_llm(spans, lambda p: {"roles": [{"index": 1, "role": "nonsense"},
                                                         {"index": "x"}]})
    assert [s["role"] for s in spans] == before
    ee.refine_roles_with_llm(spans, lambda p: {"roles": [{"index": 1, "role": "story"}]})
    assert spans[1]["role"] == "story"


def test_a_texting_story_shows_the_texts_not_a_stock_photo():
    words = words_from([(8.0, "Last night she texted me asking where the money went.")])
    plan = ee.plan_edit(words, 0, 20, intensity="high")
    row = plan["structure"]["spans"][0]
    assert row["role"] == "story"
    assert row["treatment"] == "device:chat_bubbles"


def test_spans_carry_a_visual_type():
    spans = ee.classify_spans(TALK, 0, 40)
    by_role = {}
    for s in spans:
        by_role.setdefault(s["role"], []).append(s)
        assert s["visual"] in ee.VISUAL_TYPES
    assert by_role["hook"][0]["visual"] == "quote"
    assert by_role["list"][0]["visual"] == "mechanism"
    assert by_role["data"][0]["visual"] == "transform"
    assert by_role["comparison"][0]["visual"] == "before_after"
    assert by_role["thesis"][0]["visual"] == "quote"
    assert by_role["story"][0]["visual"] == "none"


def test_interview_only_uses_its_motion_pack():
    plan = ee.plan_edit(TALK, 0, 40, intensity="high", format="interview")
    allowed = {"none", "punch", "look:lower-third", "look:backdrop-word", "look:huge-strike"}
    rows = plan["structure"]["spans"]
    assert plan["structure"]["format"] == "interview"
    assert {r["treatment"] for r in rows} <= allowed
    assert rows[0]["treatment"] == "look:lower-third"
    assert rows[0]["look"]["phrase"] == "name lower third"
    thesis = [r for r in rows if r["role"] == "thesis"][0]
    assert thesis["treatment"] == "look:backdrop-word"
    texted = [r for r in rows if "texted" in r["text"]][0]
    assert texted["treatment"] == "none"
    assert "device:chat_bubbles" not in {r["treatment"] for r in rows}


def test_recap_maps_roles_onto_named_looks():
    plan = ee.plan_edit(TALK, 0, 40, intensity="high", format="recap")
    by_role = {}
    for row in plan["structure"]["spans"]:
        by_role.setdefault(row["role"], []).append(row)
    assert plan["structure"]["format"] == "event_recap"
    assert by_role["hook"][0]["treatment"] == "look:kinetic-slam"
    assert by_role["list"][0]["treatment"] == "look:todo"
    assert by_role["data"][0]["treatment"] == "look:number-counter"
    assert by_role["data"][0]["look"]["text"].startswith("40")
    assert by_role["comparison"][0]["treatment"] == "look:screenshot-tour"
    assert by_role["story"][0]["treatment"] == "none"


def test_comedy_only_moves_on_the_punchline():
    plan = ee.plan_edit(TALK, 0, 40, intensity="high", format="comedy")
    rows = plan["structure"]["spans"]
    thesis = [r for r in rows if r["role"] == "thesis"][0]
    assert thesis["treatment"] in ("punch", "look:reject-stamp")
    assert all(r["treatment"] == "none" for r in rows if r["role"] != "thesis")


def test_auto_picks_a_motion_pack_instead_of_inventing_cards():
    plan = ee.plan_edit(TALK, 0, 40, intensity="high", format="auto")
    assert plan["structure"]["format"] == "interview"
    assert {r["treatment"] for r in plan["structure"]["spans"]} <= {
        "none", "punch", "look:lower-third", "look:backdrop-word", "look:huge-strike"}
    words = words_from([
        (0.0, "Okay so I have a joke."),
        (4.0, "The punchline is I still brought a spreadsheet."),
    ])
    comedy = ee.plan_edit(words, 0, 12, intensity="high", format="auto")
    assert comedy["structure"]["format"] == "comedy"
    recap_words = words_from([
        (0.0, "The crowd at the festival lost it."),
        (5.0, "On stage it looked like the whole venue cheered."),
    ])
    recap = ee.plan_edit(recap_words, 0, 12, intensity="high", format="auto")
    assert recap["structure"]["format"] == "event_recap"


def test_an_unknown_format_keeps_the_old_planner():
    plan = ee.plan_edit(TALK, 0, 40, intensity="high", format="documentary")
    data = [r for r in plan["structure"]["spans"] if r["role"] == "data"][0]
    assert plan["structure"]["format"] is None
    assert data["treatment"] == "device:counter"
    assert data["visual"] == "transform"


def test_labels_keep_apostrophes_lowercase():
    alts = {a["kind"]: a["labels"] for a in ee.suggest_device("here's the thing: consistency beats talent")["alternatives"]}
    assert alts["venn"][0] == "Here's"
