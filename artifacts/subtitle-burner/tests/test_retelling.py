"""The retelling pass: one default look, a spine, and a refusal-first graphic gate."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import retelling as rt  # noqa: E402


DELETED = ("pulse", "clarity", "magazine", "film", "velocity", "always")


def words(*lines):
    """(speaker, "sentence") lines, 0.3s apart, stamped with ids."""
    out, t = [], 0.0
    for speaker, line in lines:
        for tok in line.split():
            out.append({
                "word": tok, "start": round(t, 3), "end": round(t + 0.25, 3),
                "speaker": speaker,
            })
            t += 0.3
    return rt.assign_word_ids(out)


TAPE = words(
    ("HOST", "So tell me about the jars."),
    ("GUEST", "I only bring forty jars to the market."),
    ("GUEST", "That is why I sell out by two."),
    ("GUEST", "She texted me google it sometime."),
)


def _ids(role_words):
    """First and last id of an exact phrase in TAPE."""
    text = " ".join(w["word"] for w in TAPE)
    phrase = " ".join(role_words)
    start = text.index(phrase)
    # Map character offset back by walking words.
    acc = ""
    first = last = None
    for w in TAPE:
        if acc:
            acc += " "
        begin = len(acc)
        acc += w["word"]
        if first is None and begin >= start:
            first = w["id"]
        if len(acc) >= start + len(phrase):
            last = w["id"]
            break
    return first, last


def test_one_default_look_and_every_template_is_available():
    look = rt.tokens()
    assert look["id"] == "default"
    assert look["motion"] == ["hold", "rise", "count", "snap"]
    assert look["templates"] == [
        "search_bar", "message_card", "counter", "quote_card", "name_card", "media_card",
    ]
    assert "per_minute" not in look
    assert "drawers" not in look
    for name in DELETED:
        assert name not in look["id"]
    assert rt.DELETED_STYLE_PACKS == DELETED
    # The constant itself is not the object handed to callers.
    look["motion"].append("bounce")
    assert "bounce" not in rt.tokens()["motion"]


def test_word_ids_are_the_index():
    stamped = rt.assign_word_ids([{"word": "She", "start": 0, "end": 0.2}])
    assert stamped[0]["id"] == "w0000"
    assert rt.word_index("w0000") == 0
    with pytest.raises(ValueError):
        rt.word_index("word-1")


def test_picker_drops_style_pack_and_refuses_a_third_sentence():
    raw = {
        "style_pack": "pulse",
        "candidates": [
            {
                "id": "r1",
                "style_pack": "clarity",
                "audience": "other market vendors",
                "retelling": "She sells out by two because she only brings forty jars.",
                "lead_with_turn": False,
                "spine_dynamic": "claim_proof",
                "beats": {
                    "situation": "She brings a small batch.",
                    "turn": "She only brings forty jars.",
                    "consequence": "She sells out by two.",
                },
            },
            {
                "audience": "shoppers at the stall",
                "retelling": "The hot sauce is gone by two.",
                "lead_with_turn": True,
                "spine_dynamic": "story_arc",
                "beats": {
                    "situation": "She makes hot sauce.",
                    "turn": "It is gone by two.",
                    "consequence": "The stall is empty.",
                },
            },
        ],
    }
    got = rt.normalize_picker(raw)
    assert "style_pack" not in got
    assert "style_pack" not in got["candidates"][0]
    assert got["candidates"][1]["id"] == "r2"
    assert got["candidates"][1]["lead_with_turn"] is True
    assert got["candidates"][0]["spine_dynamic"] == "claim_proof"
    assert got["candidates"][1]["spine_dynamic"] == "story_arc"
    missing = {
        "candidates": [{k: v for k, v in raw["candidates"][0].items() if k != "spine_dynamic"},
                       raw["candidates"][1]],
    }
    with pytest.raises(ValueError, match="spine_dynamic"):
        rt.normalize_picker(missing)
    raw["candidates"][0]["retelling"] = "One. Two. Three."
    with pytest.raises(ValueError, match="one or two"):
        rt.normalize_picker(raw)
    raw["candidates"] = raw["candidates"][:1]
    with pytest.raises(ValueError, match="two or three"):
        rt.normalize_picker(raw)


def _spine(lead=False, graphic=None, dynamic="claim_proof", detours=None):
    sit = _ids("I only bring forty jars to the market.".split())
    turn = _ids("That is why I sell out by two.".split())
    con = _ids("She texted me google it sometime.".split())
    # Consequence in this fixture is just the leftover line, used only so the
    # spine has three spans. Graphics tests put the fact on the turn.
    beats = [
        {"role": "situation", "start_word_id": sit[0], "end_word_id": sit[1],
         "text": "I only bring forty jars to the market.", "punch_in": False},
        {"role": "turn", "start_word_id": turn[0], "end_word_id": turn[1],
         "text": "That is why I sell out by two.",
         "protect_pause_before_ms": 400, "protect_pause_after_ms": 200,
         "punch_in": True},
        {"role": "consequence", "start_word_id": con[0], "end_word_id": con[1],
         "text": "She texted me google it sometime.", "punch_in": False,
         "protect_pause_before_ms": 900},
    ]
    body = {
        "style_pack": "velocity",
        "audience": "other market vendors",
        "retelling": "She sells out by two because she only brings forty jars.",
        "lead_with_turn": lead,
        "spine_dynamic": dynamic,
        "beats": beats,
        "detours": detours or [],
        "graphics": [graphic] if graphic else [],
    }
    return body


def test_spine_orders_the_turn_first_only_when_it_stands_alone():
    chrono = rt.normalize_spine(_spine(False), TAPE)
    assert "style_pack" not in chrono
    assert [b["role"] for b in chrono["beats"]] == ["situation", "turn", "consequence"]
    assert chrono["order"] == ["situation", "turn", "consequence"]
    # A pause marked on the consequence is not kept. Only the turn holds air.
    consequence = chrono["beats"][-1]
    assert consequence["protect_pause_before_ms"] == 0
    turn = chrono["beats"][1]
    assert turn["protect_pause_before_ms"] == 400
    assert turn["punch_in"] is True

    led = rt.normalize_spine(_spine(True), TAPE)
    assert [b["role"] for b in led["beats"]] == ["turn", "situation", "consequence"]


def test_a_punch_on_the_situation_is_refused_unless_the_speaker_changes():
    body = _spine(False)
    start = body["beats"][0]["start_word_id"]
    end = body["beats"][0]["end_word_id"]
    # One word in, the speaker has not changed, so a punch is a timer in disguise.
    nxt = f"w{rt.word_index(start) + 1:04d}"
    body["beats"][0]["start_word_id"] = nxt
    body["beats"][0]["text"] = rt.span_text(TAPE, nxt, end)
    body["beats"][0]["punch_in"] = True
    with pytest.raises(ValueError, match="punch-in"):
        rt.normalize_spine(body, TAPE)
    # The guest's first word, right after the host, is a real speaker change.
    body["beats"][0]["start_word_id"] = start
    body["beats"][0]["text"] = rt.span_text(TAPE, start, end)
    got = rt.normalize_spine(body, TAPE)
    assert got["beats"][0]["punch_in"] is True


def test_google_and_texted_do_not_open_a_graphic():
    body = _spine()
    con = body["beats"][2]
    for word, template in (("google", "counter"), ("texted", "quote_card")):
        graphic = {
            "id": "g1", "beat_role": "consequence",
            "start_word_id": con["start_word_id"], "end_word_id": con["end_word_id"],
            "template": template,
            "justification": {
                "kind": "ear_blind", "fact": "name", "text": word,
                "why": "Someone said the word.",
            },
        }
        with pytest.raises(ValueError, match="template_forbidden"):
            rt.normalize_spine(_spine(graphic=graphic), TAPE)


def test_a_number_may_be_a_counter_and_a_verb_may_not():
    turn = _spine()["beats"][1]
    # "two" is the number in the turn. "forty" lives in the situation.
    graphic = {
        "id": "g1", "beat_role": "turn",
        "start_word_id": turn["start_word_id"], "end_word_id": turn["end_word_id"],
        "template": "counter",
        "source_word_ids": [turn["end_word_id"]],
        "justification": {
            "kind": "ear_blind", "fact": "number", "text": "two",
            "function": "scale",
            "why": "The retelling is the hour she sells out.",
        },
    }
    got = rt.normalize_spine(_spine(graphic=graphic), TAPE)
    assert got["graphics"][0]["template"] == "counter"
    assert got["graphics"][0]["use_person_matte"] is False
    assert got["graphics"][0]["on_asset_miss"] == "raw_speaker"
    assert got["graphics"][0]["presentation_mode"] == "framed_card"

    graphic["justification"]["text"] = "eleven"
    with pytest.raises(ValueError, match="fact_not_in_span"):
        rt.normalize_spine(_spine(graphic=graphic), TAPE)

    graphic["justification"]["text"] = "two"
    graphic["presentation_mode"] = "fullscreen_replace"
    with pytest.raises(ValueError, match="template_forbidden"):
        rt.normalize_spine(_spine(graphic=graphic), TAPE)


def test_visual_proof_is_a_media_card_and_a_callback_is_not_a_bare_verb():
    turn = _spine()["beats"][1]
    proof = {
        "id": "g1", "beat_role": "turn",
        "start_word_id": turn["start_word_id"], "end_word_id": turn["end_word_id"],
        "template": "media_card",
        "presentation_mode": "fullscreen_replace",
        "justification": {
            "kind": "visual_proof", "fact": "ui", "text": "sell out",
            "function": "ui",
            "why": "The stall emptying is the thing the viewer has to see.",
        },
    }
    got = rt.normalize_spine(_spine(graphic=proof), TAPE)
    assert got["graphics"][0]["presentation_mode"] == "fullscreen_replace"
    # Mid-turn, the guest is already on screen, so nothing waits.
    assert got["graphics"][0]["reaction_delay_s"] == 0

    proof["template"] = "counter"
    proof["presentation_mode"] = "framed_card"
    with pytest.raises(ValueError, match="template_forbidden"):
        rt.normalize_spine(_spine(graphic=proof), TAPE)

    con = _spine()["beats"][2]
    bare = {
        "id": "g2", "beat_role": "consequence",
        "start_word_id": con["start_word_id"], "end_word_id": con["end_word_id"],
        "template": "message_card",
        "justification": {
            "kind": "ear_blind", "fact": "receipt", "text": "texted",
            "function": "contradiction",
            "why": "Someone said the word.",
        },
    }
    with pytest.raises(ValueError, match="template_forbidden"):
        rt.normalize_spine(_spine(graphic=bare), TAPE)
    msg = {
        "id": "g2", "beat_role": "consequence",
        "start_word_id": con["start_word_id"], "end_word_id": con["end_word_id"],
        "template": "message_card",
        "presentation_mode": "split_screen",
        "justification": {
            "kind": "ear_blind", "fact": "receipt", "text": "texted me google it",
            "function": "contradiction",
            "why": "The wording is the contradiction.",
        },
    }
    got = rt.normalize_spine(_spine(graphic=msg), TAPE)
    assert got["graphics"][0]["template"] == "message_card"
    assert got["graphics"][0]["presentation_mode"] == "split_screen"
    assert "reading_aloud" not in got["graphics"][0]["justification"]
    msg["justification"]["reading_aloud"] = True
    got = rt.normalize_spine(_spine(graphic=msg), TAPE)
    assert got["graphics"][0]["justification"]["reading_aloud"] is True


def test_a_graphic_moves_above_the_face_and_drops_when_nothing_is_clear():
    face = {"x": 0.28, "y": 0.32, "w": 0.44, "h": 0.40}
    card = {"x": 0.10, "y": 0.36, "w": 0.80, "h": 0.18}
    band = {"top": 0.80}
    moved = rt.place_off_face(card, [face], band)
    assert moved["dropped"] is False
    assert moved["face_collision"] is True
    assert moved["placement_zone"] == "top_band"
    assert moved["use_person_matte"] is False
    assert moved["rect"]["y"] + moved["rect"]["h"] <= face["y"]

    # The forehead is against the top of the frame, so the card sits under the chin.
    high = {"x": 0.2, "y": 0.02, "w": 0.6, "h": 0.20}
    covering = {"x": 0.10, "y": 0.04, "w": 0.80, "h": 0.18}
    under = rt.place_off_face(covering, [high], band)
    assert under["dropped"] is False
    assert under["placement_zone"] == "above_captions_band"
    assert under["rect"]["y"] >= high["y"] + high["h"]

    wall = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.70}
    dropped = rt.place_off_face(card, [wall], {"top": 0.72})
    assert dropped["dropped"] is True
    assert dropped["reason"] == "covers_face_no_band"
    assert dropped["use_person_matte"] is False


def _tone(sr, spans, total):
    samples = [0.0] * int(total * sr)
    for a, b in spans:
        for i in range(int(a * sr), min(int(b * sr), len(samples))):
            samples[i] = 0.5
    return samples


def test_snap_finds_the_speech_edge_and_does_not_cross_a_pause():
    sr = 100
    samples = _tone(sr, [(1.0, 2.0)], total=3.0)
    assert rt.snap_boundary(samples, sr, 1.08, "in") == pytest.approx(1.0, abs=0.02)
    assert rt.snap_boundary(samples, sr, 1.90, "out") == pytest.approx(2.0, abs=0.02)
    # Already on the edge: do not invent a move.
    assert rt.snap_boundary(samples, sr, 1.0, "in") == pytest.approx(1.0, abs=0.02)
    # The rise is before a protected pause. The cut may enter the pause
    # and must not come out the other side into the previous sentence.
    early = _tone(sr, [(0.40, 2.0)], total=3.0)
    held = rt.snap_boundary(early, sr, 1.05, "in", cap_s=0.9, protect=(0.55, 0.98))
    assert held == pytest.approx(0.55, abs=0.02)
    # An out-point must not walk through the pause into the next sentence.
    long = _tone(sr, [(1.0, 2.8)], total=3.0)
    stopped = rt.snap_boundary(long, sr, 1.95, "out", cap_s=0.9, protect=(2.02, 2.50))
    assert stopped == pytest.approx(2.50, abs=0.02)
    assert rt.snap_boundary([], sr, 1.5, "in") == 1.5
    # Whisper was early: the onset is later, still inside ±200ms.
    assert rt.snap_boundary(samples, sr, 0.88, "in") == pytest.approx(1.0, abs=0.02)
    # The onset is outside the ±250ms window, so the boundary stays.
    assert rt.snap_boundary(samples, sr, 0.60, "in") == pytest.approx(0.60, abs=0.02)


def test_the_checker_drops_a_bad_graphic_and_redraws_a_fixed_word():
    turn = _spine()["beats"][1]
    graphic = {
        "id": "g1", "beat_role": "turn",
        "start_word_id": turn["start_word_id"], "end_word_id": turn["end_word_id"],
        "template": "counter",
        "rect": {"x": 0.1, "y": 0.36, "w": 0.8, "h": 0.18},
        "source_word_ids": [turn["end_word_id"]],
        "justification": {
            "kind": "ear_blind", "fact": "number", "text": "two",
            "function": "scale",
            "why": "The hour is the retelling.",
        },
    }
    plan = rt.normalize_spine(_spine(graphic=graphic), TAPE)
    g = plan["graphics"][0]
    g["rect"] = {"x": 0.1, "y": 0.36, "w": 0.8, "h": 0.18}
    face = {"x": 0.28, "y": 0.32, "w": 0.44, "h": 0.40}
    wid = g["source_word_ids"][0]
    report = rt.check_graphics(
        [g], TAPE,
        face_boxes={"g1": [face]},
        overrides={wid: "Apify"},
        caption_band={"top": 0.80},
    )
    actions = {f["action"] for f in report["fixes"]}
    assert "move" in actions
    assert "redraw" in actions
    assert report["ok"] is False

    con = _spine()["beats"][2]
    bare = {
        "id": "g1",
        "start_word_id": con["start_word_id"],
        "end_word_id": con["end_word_id"],
        "template": "name_card",
        "source_word_ids": [con["start_word_id"]],
        "justification": {
            "kind": "ear_blind", "fact": "name", "text": "google",
            "why": "Someone said the word.",
        },
    }
    report = rt.check_graphics([bare], TAPE)
    assert report["fixes"][0]["action"] == "drop"
    assert report["fixes"][0]["reason"] == "template_forbidden"

    missed = dict(plan["graphics"][0])
    missed["asset_id"] = "missing"
    missed["rect"] = None
    report = rt.check_graphics([missed], TAPE, missing_asset_ids=["missing"])
    assert report["fixes"][0] == {"id": "g1", "action": "drop", "reason": "asset_miss"}


def test_prompts_do_not_ask_for_a_pack_or_a_timer():
    picker = rt.picker_prompt(TAPE)
    spine = rt.spine_prompt(TAPE, "other market vendors", "She sells out by two.", False)
    for prompt in (picker, spine):
        lowered = prompt.lower()
        for name in DELETED:
            assert name not in lowered
        assert "7-10" not in lowered
        assert "every 7" not in lowered
        assert "style pack" not in lowered or "do not return a style pack" in lowered
    assert "w0000" in picker
    assert "mute test" in spine
    assert "raw_speaker" in spine
    for name in ("claim_proof", "story_arc", "escalation"):
        assert name in picker and name in spine
    assert "format rubric" in picker
    assert "corner_note" in spine and "split_screen" in spine
    assert "search_bar" in spine
    assert "lucide" in spine.lower()
    assert "typing" in spine
    assert "gradient" in spine and "blur" in spine
    assert "1.5" in spine


def test_a_picked_story_still_cuts_when_its_lines_are_not_in_the_tape():
    got = rt.spine_from_candidate({
        "audience": "an event organizer",
        "retelling": "Rain did not stop the meetup. The crowd still showed up.",
        "lead_with_turn": False,
        "spine_dynamic": "story_arc",
        "beats": {
            "situation": "An organizer feared the weather would cancel everything.",
            "turn": "A huge crowd showed up anyway.",
            "consequence": "The meetup still worked.",
        },
    }, TAPE)
    assert [b["role"] for b in got["beats"]] == ["situation", "turn", "consequence"]
    assert all(b["start_word_id"] and b["end_word_id"] for b in got["beats"])
    assert got["graphics"] == []


def test_the_director_frames_a_figure_a_question_and_a_mechanism():
    spine = rt.normalize_spine(_spine(), TAPE)
    directed = rt.direct_visuals(TAPE, spine)
    cards = {g["justification"]["text"]: g["template"] for g in directed["graphics"]}
    assert cards.get("forty") == "counter"
    assert cards.get("two") == "counter"
    assert "google" not in cards
    assert "texted" not in cards
    assert all(g["on_asset_miss"] == "raw_speaker" for g in directed["graphics"])

    asked = words(
        ("HOST", "So tell me about the stall."),
        ("GUEST", "Why do the jars sell out?"),
        ("GUEST", "That is how it works on a small table."),
        ("GUEST", "Consistency wins."),
    )

    def span_ids(phrase):
        text = " ".join(w["word"] for w in asked)
        start = text.index(phrase)
        cursor = 0
        first = last = None
        for w in asked:
            word = w["word"]
            if cursor >= start and first is None:
                first = w["id"]
            cursor += len(word) + 1
            if first and cursor >= start + len(phrase):
                last = w["id"]
                break
        return first, last

    q0, q1 = span_ids("Why do the jars sell out?")
    m0, m1 = span_ids("That is how it works on a small table.")
    c0, c1 = span_ids("Consistency wins.")
    question = rt.normalize_spine({
        "audience": "other vendors",
        "retelling": "The jars sell out because of how the table works.",
        "lead_with_turn": False,
        "spine_dynamic": "story_arc",
        "beats": [
            {"role": "situation", "start_word_id": m0, "end_word_id": m1},
            {"role": "turn", "start_word_id": q0, "end_word_id": q1},
            {"role": "consequence", "start_word_id": c0, "end_word_id": c1},
        ],
    }, asked)
    directed = rt.direct_visuals(asked, {**question, "graphics": []})
    by_role = {g["beat_role"]: g for g in directed["graphics"]}
    assert by_role["situation"]["template"] == "media_card"
    assert by_role["situation"]["justification"]["fact"] == "mechanism"
    assert by_role["turn"]["template"] == "search_bar"
    assert by_role["turn"]["justification"]["function"] == "question"
    assert by_role["consequence"]["template"] == "quote_card"
    assert all(g["presentation_mode"] == "framed_card" for g in directed["graphics"])


def test_a_prose_beat_map_is_cut_back_to_the_words():
    # The model often returns the picker's shape: a sentence per beat, no word ids.
    got = rt.normalize_spine({
        "audience": "other market vendors",
        "retelling": "She sells out by two because she only brings forty jars.",
        "lead_with_turn": False,
        "spine_dynamic": "claim_proof",
        "beats": {
            "situation": "She only brings forty jars to the market.",
            "turn": "That is why she sells out by two.",
            "consequence": "She texted me to google it sometime.",
        },
    }, TAPE)
    assert [b["role"] for b in got["beats"]] == ["situation", "turn", "consequence"]
    assert "forty" in got["beats"][0]["text"]
    assert "two" in got["beats"][1]["text"]
    assert "texted" in got["beats"][2]["text"]
    assert "style_pack" not in got


def test_story_arc_keeps_a_detour_that_pays_off_and_claim_proof_does_not():
    tape = words(
        ("GUEST", "I started with a small stall."),
        ("GUEST", "The secret recipe took years."),
        ("GUEST", "Then the line went around the block."),
        ("GUEST", "That secret recipe is why we sell out."),
    )
    # I started with a small stall. / The secret recipe took years. /
    # Then the line went around the block. / That secret recipe is why we sell out.
    sit = (tape[0]["id"], tape[5]["id"])
    detour = (tape[6]["id"], tape[10]["id"])
    turn = (tape[11]["id"], tape[17]["id"])
    con = (tape[18]["id"], tape[-1]["id"])

    def body(dynamic, with_detour=True):
        return {
            "audience": "home cooks",
            "retelling": "The secret recipe is why the stall sells out.",
            "lead_with_turn": False,
            "spine_dynamic": dynamic,
            "beats": [
                {"role": "situation", "start_word_id": sit[0], "end_word_id": sit[1]},
                {"role": "turn", "start_word_id": turn[0], "end_word_id": turn[1]},
                {"role": "consequence", "start_word_id": con[0], "end_word_id": con[1]},
            ],
            "detours": [{
                "start_word_id": detour[0], "end_word_id": detour[1],
            }] if with_detour else [],
        }

    kept = rt.normalize_spine(body("story_arc"), tape)
    assert kept["detours"][0]["pays_off"] is True
    assert "recipe" in kept["detours"][0]["text"]
    with pytest.raises(ValueError, match="story arc"):
        rt.normalize_spine(body("claim_proof"), tape)
    orphan = body("story_arc")
    # The block-line tangent never comes back in the callback.
    orphan["beats"][1] = {
        "role": "turn", "start_word_id": detour[0], "end_word_id": detour[1],
    }
    orphan["detours"] = [{
        "start_word_id": turn[0], "end_word_id": turn[1],
    }]
    with pytest.raises(ValueError, match="pay off"):
        rt.normalize_spine(orphan, tape)


def test_escalation_protects_a_deadpan_drop_inside_a_kept_beat():
    body = _spine(dynamic="escalation")
    body["beats"][2]["deadpan"] = True
    body["beats"][2]["protect_pause_after_ms"] = 350
    kept = rt.normalize_spine(body, TAPE)
    assert kept["spine_dynamic"] == "escalation"
    assert kept["beats"][-1]["protect_pause_after_ms"] == 350
    tight = rt.normalize_spine(_spine(dynamic="claim_proof"), TAPE)
    assert tight["beats"][-1]["protect_pause_after_ms"] == 0

    sr = 100
    samples = _tone(sr, [(0.0, 0.40)], total=1.2)
    drops = rt.find_deadpan_drops(samples, sr, 0.0, 1.0)
    assert drops
    assert drops[0]["start"] == pytest.approx(0.40, abs=0.03)
    assert drops[0]["end"] > drops[0]["start"] + 0.18
    assert rt.find_deadpan_drops(_tone(sr, [(0.0, 1.0)], total=1.2), sr, 0.0, 1.0) == []

    loud = _tone(sr, [(0.0, 3.0)], total=3.0)
    plan = rt.apply_prosody(TAPE, kept, loud, sr)
    consequence = plan["beats"][-1]
    assert consequence["deadpan_pauses"] == []
    assert consequence["protect_pause_after_ms"] == 0
    assert "snap_in" in plan["beats"][0]


def test_guest_reaction_host_bias_and_cross_talk():
    sit = _ids("I only bring forty jars to the market.".split())
    opening = {
        "id": "g-open", "beat_role": "situation",
        "start_word_id": sit[0], "end_word_id": sit[1],
        "template": "media_card",
        "presentation_mode": "fullscreen_replace",
        "justification": {
            "kind": "visual_proof", "fact": "ui", "text": "forty jars",
            "function": "ui",
            "why": "The batch is the thing you have to see.",
        },
    }
    got = rt.normalize_spine(_spine(graphic=opening), TAPE)
    assert got["graphics"][0]["reaction_delay_s"] == 1.5
    opening["presentation_mode"] = "corner_note"
    with pytest.raises(ValueError, match="template_forbidden"):
        rt.normalize_spine(_spine(graphic=opening), TAPE)
    opening["presentation_mode"] = "split_screen"
    held = rt.normalize_spine(_spine(graphic=opening), TAPE)
    assert held["graphics"][0]["reaction_delay_s"] == 0

    raw = dict(got["graphics"][0])
    raw["reaction_delay_s"] = 0
    raw["rect"] = None
    report = rt.check_graphics([raw], TAPE)
    assert report["fixes"][0]["action"] == "delay"
    assert report["fixes"][0]["delay_s"] == 1.5

    host = words(
        ("HOST", "Maya brought forty jars."),
        ("GUEST", "That is the whole batch."),
        ("GUEST", "We sell out by two."),
    )
    name = {
        "id": "g-name", "beat_role": "situation",
        "start_word_id": host[0]["id"], "end_word_id": host[3]["id"],
        "template": "name_card",
        "icon": "store",
        "justification": {
            "kind": "ear_blind", "fact": "name", "text": "Maya",
            "function": "proper_noun",
            "why": "The ear will not keep the name.",
        },
    }
    spine = {
        "audience": "shoppers",
        "retelling": "Maya sells out by two.",
        "lead_with_turn": False,
        "spine_dynamic": "claim_proof",
        "beats": [
            {"role": "situation", "start_word_id": host[0]["id"], "end_word_id": host[3]["id"]},
            {"role": "turn", "start_word_id": host[4]["id"], "end_word_id": host[8]["id"]},
            {"role": "consequence", "start_word_id": host[9]["id"], "end_word_id": host[-1]["id"]},
        ],
        "graphics": [name],
    }
    named = rt.normalize_spine(spine, host)
    assert named["graphics"][0]["icon"] == "store"
    heavy = dict(name)
    heavy["template"] = "counter"
    heavy["justification"] = {
        "kind": "ear_blind", "fact": "number", "text": "forty",
        "function": "scale",
        "why": "The count is the premise.",
    }
    spine["graphics"] = [heavy]
    with pytest.raises(ValueError, match="host_setup"):
        rt.normalize_spine(spine, host)

    overlap = rt.assign_word_ids([
        {"word": "Hold", "start": 0.0, "end": 0.5, "speaker": "HOST"},
        {"word": "No", "start": 0.2, "end": 0.7, "speaker": "GUEST"},
        {"word": "way", "start": 0.7, "end": 1.0, "speaker": "GUEST"},
        {"word": "The", "start": 1.4, "end": 1.6, "speaker": "GUEST"},
        {"word": "batch", "start": 1.6, "end": 1.9, "speaker": "GUEST"},
        {"word": "sells", "start": 2.2, "end": 2.5, "speaker": "GUEST"},
        {"word": "out", "start": 2.5, "end": 2.8, "speaker": "GUEST"},
    ])
    crossed = {
        "audience": "listeners",
        "retelling": "The batch sells out.",
        "lead_with_turn": False,
        "spine_dynamic": "story_arc",
        "beats": [
            {"role": "situation", "start_word_id": "w0001", "end_word_id": "w0002",
             "punch_in": True},
            {"role": "turn", "start_word_id": "w0003", "end_word_id": "w0004", "punch_in": True},
            {"role": "consequence", "start_word_id": "w0005", "end_word_id": "w0006"},
        ],
        "graphics": [{
            "id": "g-over", "beat_role": "situation",
            "start_word_id": "w0001", "end_word_id": "w0002",
            "template": "search_bar",
            "justification": {
                "function": "question", "text": "No way",
                "why": "An overlay would cover the overlap.",
            },
        }],
    }
    with pytest.raises(ValueError, match="cross_talk"):
        rt.normalize_spine(crossed, overlap)
    crossed["graphics"] = []
    held_wide = rt.normalize_spine(crossed, overlap)
    situation = next(b for b in held_wide["beats"] if b["role"] == "situation")
    turn_beat = next(b for b in held_wide["beats"] if b["role"] == "turn")
    assert situation["punch_in"] is False
    assert turn_beat["punch_in"] is True


def test_manual_overlays_are_not_dropped_for_a_missing_reason():
    face = {"x": 0.28, "y": 0.32, "w": 0.44, "h": 0.40}
    hand = {
        "id": "hand",
        "origin": "co_editor",
        "template": "quote_card",
        "rect": {"x": 0.1, "y": 0.36, "w": 0.8, "h": 0.18},
        "use_person_matte": True,
    }
    report = rt.check_graphics([hand], TAPE, face_boxes={"hand": [face]}, caption_band={"top": 0.80})
    assert {f["action"] for f in report["fixes"]} == {"move"}
    assert all(f.get("reason") != "no_justification" for f in report["fixes"])

    gone = dict(hand)
    gone["rect"] = None
    gone["asset_id"] = "missing-still"
    report = rt.check_graphics([gone], TAPE, missing_asset_ids=["missing-still"])
    assert report["fixes"] == [{"id": "hand", "action": "drop", "reason": "asset_miss"}]


def test_a_timestamp_override_dirties_the_graphic():
    turn = _spine()["beats"][1]
    graphic = {
        "id": "g1", "beat_role": "turn",
        "start_word_id": turn["start_word_id"], "end_word_id": turn["end_word_id"],
        "template": "counter",
        "source_word_ids": [turn["end_word_id"]],
        "justification": {
            "kind": "ear_blind", "fact": "number", "text": "two",
            "function": "scale",
            "why": "The hour is the retelling.",
        },
    }
    plan = rt.normalize_spine(_spine(graphic=graphic), TAPE)
    g = plan["graphics"][0]
    word = next(w for w in TAPE if w["id"] == g["source_word_ids"][0])
    stamp = f"{word['start']:.3f}"
    report = rt.check_graphics([g], TAPE, overrides={stamp: "2"})
    assert any(f["action"] == "redraw" and f["reason"] == "word_override" for f in report["fixes"])
    same = rt.check_graphics([g], TAPE, overrides={stamp: word["word"]})
    assert not any(f["action"] == "redraw" for f in same["fixes"])


def test_action_stock_is_allowed_on_a_story_and_refused_on_a_proof():
    turn = _spine()["beats"][1]
    action = {
        "id": "g-act", "beat_role": "turn",
        "start_word_id": turn["start_word_id"], "end_word_id": turn["end_word_id"],
        "template": None,
        "presentation_mode": "fullscreen_replace",
        "justification": {
            "kind": "visual_proof", "fact": "action", "text": "sell out",
            "function": "action",
            "why": "Setting the stall up is the visual the line earns.",
        },
    }
    with pytest.raises(ValueError, match="fact, a metric, or a receipt"):
        rt.normalize_spine(_spine(graphic=action), TAPE)
    got = rt.normalize_spine(_spine(graphic=action, dynamic="story_arc"), TAPE)
    assert got["graphics"][0]["template"] is None
    assert got["graphics"][0]["on_asset_miss"] == "raw_speaker"


def test_snap_lands_on_the_kept_word_and_records_a_real_drop():
    sr = 100
    samples = _tone(sr, [(1.0, 1.95), (2.2, 2.8), (3.2, 3.9)], total=4.2)
    tape = rt.assign_word_ids([
        {"word": "Go", "start": 1.08, "end": 1.30, "speaker": "GUEST"},
        {"word": "now", "start": 1.40, "end": 1.90, "speaker": "GUEST"},
        {"word": "Hold", "start": 2.25, "end": 2.50, "speaker": "GUEST"},
        {"word": "there", "start": 2.55, "end": 2.75, "speaker": "GUEST"},
        {"word": "Done", "start": 3.25, "end": 3.50, "speaker": "GUEST"},
        {"word": "today", "start": 3.55, "end": 3.85, "speaker": "GUEST"},
    ])
    spine = {
        "audience": "someone walking past",
        "retelling": "Go now and it is done today.",
        "lead_with_turn": False,
        "spine_dynamic": "escalation",
        "beats": [
            {"role": "situation", "start_word_id": "w0000", "end_word_id": "w0001"},
            {"role": "turn", "start_word_id": "w0002", "end_word_id": "w0003", "punch_in": True},
            {"role": "consequence", "start_word_id": "w0004", "end_word_id": "w0005"},
        ],
    }
    plan = rt.normalize_spine(spine, tape)
    snapped = rt.apply_prosody(tape, plan, samples, sr)
    assert snapped["beats"][0]["snap_in"] == pytest.approx(1.0, abs=0.02)
    assert snapped["beats"][0]["snap_out"] == pytest.approx(1.95, abs=0.03)
