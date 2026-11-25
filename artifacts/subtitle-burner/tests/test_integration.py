"""Smart AI Edit, the storyboard plan, export/import and the new co-editor ops."""
import json
import shutil
import uuid

import pytest

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")


def _words(lines):
    out = []
    for start, line in lines:
        t = start
        for tok in line.split():
            out.append({"word": tok, "start": round(t, 3), "end": round(t + 0.12, 3)})
            t += 0.15
    return out


@pytest.fixture
def talk_job(app_mod, clip, monkeypatch, tmp_path):
    import taste
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(app_mod, "TASTE", taste.TasteStore(tmp_path / "taste.json"))
    monkeypatch.setattr(app_mod, "_broll_any_photo_provider", lambda: False)
    sid = uuid.uuid4().hex
    dest = app_mod.UPLOAD_DIR / f"{sid}.mp4"
    shutil.copyfile(clip, dest)
    app_mod.jobs[sid] = {"status": "awaiting_edit", "style": {}, "emoji_rules": {},
                         "filename": "talk.mp4", "words": _words([
                             (0.0, "Stop scrolling right now."),
                             (6.0, "Revenue grew 40% last year."),
                             (10.0, "She texted me at midnight."),
                             (14.0, "Here's the thing: consistency wins."),
                         ])}
    yield sid
    app_mod.jobs.pop(sid, None)
    dest.unlink(missing_ok=True)


def test_edit_plan_is_a_storyboard_without_media(app_mod, talk_job):
    c = app_mod.app.test_client()
    r = c.post("/edit-plan", json={"job_id": talk_job, "style_pack": "default",
                                   "intensity": "high"}).get_json()
    roles = [s["role"] for s in r["structure"]["spans"]]
    assert roles[0] == "hook" and roles[-1] == "thesis"
    assert r["style"]["pack"] == "Default"
    assert r["style_pack"] == "default"
    assert "spans" not in r["style"] and "caption" not in r["structure"]


def test_edit_plan_format_locks_motion_to_the_lane(app_mod, talk_job):
    c = app_mod.app.test_client()
    r = c.post("/edit-plan", json={"job_id": talk_job, "style_pack": "pulse",
                                   "intensity": "high", "format": "interview"}).get_json()
    assert r["structure"]["format"] == "interview"
    allowed = {"none", "punch", "look:lower-third", "look:backdrop-word", "look:huge-strike"}
    assert {s["treatment"] for s in r["structure"]["spans"]} <= allowed
    texted = [s for s in r["structure"]["spans"] if "texted" in s["text"]][0]
    assert texted["treatment"] == "none"
    assert all(s.get("visual") for s in r["structure"]["spans"])


def test_src_to_timeline_respects_cuts(app_mod):
    assert app_mod._src_to_timeline(5.0, 1.0, [[2.0, 3.0]]) == pytest.approx(3.0)
    assert app_mod._src_to_timeline(2.5, 1.0, [[2.0, 3.0]]) == pytest.approx(1.0)


@needs_ffmpeg
def test_smart_seed_places_every_decision(app_mod, talk_job):
    c = app_mod.app.test_client()
    r = c.post("/ai-edit-seed", json={"source_job_id": talk_job, "style_pack": "velocity",
                                      "intensity": "high", "smart": True,
                                      "apply_cuts": False}).get_json()
    assert r["smart"] and r["plan"]["structure"]["spans"]
    tl = r["timeline"]
    assert tl["structure"] == r["plan"]["structure"]
    assert tl["style_doc"]["devices"]["per_minute"] is None
    treatments = {s["treatment"] for s in tl["structure"]["spans"]}
    overlays = tl["tracks"]["overlay"]
    kinds = {o["device"]["kind"] for o in overlays if o.get("device")}
    if "device:counter" in treatments:
        assert "counter" in kinds
    if "device:chat_bubbles" in treatments:
        assert "chat_bubbles" in kinds
    for o in overlays:
        assert app_mod._find_asset_path(o["asset_id"]) is not None
        assert "plan_ref" in o
    planned_fx = [fx for fx in tl["tracks"]["effects"]
                  if (fx.get("fx_origin") or {}).get("source") == "plan"]
    assert planned_fx or not (treatments & {"punch", "clean_zoom"})
    for fx in planned_fx:
        o = fx["fx_origin"]
        assert o["suggestion_id"] and o["proposed"]["type"] == fx["type"]
        assert o["proposed"]["dur"] == fx["out"]
    assert r["placed"]["devices"] == len([o for o in overlays if o.get("device")])


@needs_ffmpeg
def test_export_and_import_round_trip(app_mod, talk_job):
    c = app_mod.app.test_client()
    seeded = c.post("/ai-edit-seed", json={"source_job_id": talk_job, "smart": True,
                                           "style_pack": "pulse", "intensity": "med"}).get_json()
    created = c.post("/timeline/create", json={"label": "Round trip"}).get_json()
    jid = created["job_id"]
    assert c.post("/timeline/save", json={"job_id": jid, "timeline": seeded["timeline"]}).get_json()["ok"]
    try:
        r = c.get(f"/timeline/{jid}/export.json")
        assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
        doc = json.loads(r.data)
        assert "spans" in doc["structure"]["structure"] and "style_doc" in doc["style"]
        back = c.post("/timeline/import", json={"doc": doc}).get_json()
        assert back["format"] == "edit" and back["missing"] == []
        assert back["timeline"]["tracks"]["main"] == app_mod._normalize_timeline(
            seeded["timeline"])["tracks"]["main"]

        o = c.get(f"/timeline/{jid}/export.otio")
        otio = json.loads(o.data)
        assert otio["OTIO_SCHEMA"] == "Timeline.1"
        url = otio["tracks"]["children"][0]["children"][0]["media_reference"]["target_url"]
        assert url.startswith("file://") and talk_job in url
        via_otio = c.post("/timeline/import", json={"doc": otio}).get_json()
        assert via_otio["format"] == "otio" and via_otio["missing"] == []
        for j in (back["job_id"], via_otio["job_id"]):
            app_mod.jobs.pop(j, None)
    finally:
        app_mod.jobs.pop(jid, None)


def test_import_lists_media_it_cannot_find(app_mod):
    c = app_mod.app.test_client()
    doc = {"tracks": {"main": [{"id": "x", "source_job_id": "f" * 32, "in": 0, "out": 2}]}}
    r = c.post("/timeline/import", json={"doc": doc}).get_json()
    app_mod.jobs.pop(r["job_id"], None)
    assert r["missing"] == [{"track": "main", "id": "x", "relink": "f" * 32}]
    assert c.post("/timeline/import", json={"doc": "nope"}).status_code == 400


@needs_ffmpeg
def test_coeditor_can_add_a_device_speed_and_put_it_behind(app_mod, temp_db, monkeypatch):
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions", lambda p: {"ops": [
        {"op": "add_device", "kind": "venn", "labels": ["Skill", "Demand", "Money"], "start": 1},
        {"op": "add_device", "kind": "hologram"},
        {"op": "add_speed", "start": 2, "duration": 1, "speed": 10},
        {"op": "set_behind", "index": 0, "on": True},
    ], "message": "done"})
    c = app_mod.app.test_client()
    r = c.post("/co-editor", json={"prompt": "venn of skill and demand, speed up, behind me",
                                   "timeline": {"canvas": "9x16", "tracks": {"main": [{"id": "m"}]}}}
               ).get_json()
    names = [o["op"] for o in r["ops"]]
    assert names == ["add_device", "add_speed", "set_behind"]
    assert r["ops"][0]["clip"]["device"]["kind"] == "venn"
    assert r["ops"][1]["speed"] == 4.0
    assert "hologram" in r["message"]


def test_style_toggles_reach_the_plan(app_mod, talk_job):
    c = app_mod.app.test_client()
    base = {"job_id": talk_job, "style_pack": "velocity", "intensity": "high"}
    full = c.post("/edit-plan", json=base).get_json()
    trs = {s["treatment"] for s in full["structure"]["spans"]}
    assert any(t.startswith("device:") or t == "kinetic_text" for t in trs)

    bare = c.post("/edit-plan", json=dict(base, graphics=False, broll=False)).get_json()
    trs = [s["treatment"] for s in bare["structure"]["spans"]]
    assert not any(t.startswith("device:") or t in ("kinetic_text", "broll") for t in trs)
    assert all("device" not in s for s in bare["structure"]["spans"])
    assert set(trs) <= {"punch", "clean_zoom", "none"}

    still = c.post("/edit-plan", json=dict(base, graphics=False, broll=False, moves=False)).get_json()
    assert {s["treatment"] for s in still["structure"]["spans"]} == {"none"}


def test_style_packs_describe_the_look(app_mod):
    packs = app_mod.app.test_client().get("/ai-edit/style-packs").get_json()["packs"]
    by = {p["id"]: p for p in packs}
    assert "default" in by
    for gone in ("pulse", "clarity", "magazine", "film", "velocity", "always"):
        assert gone not in by
    assert by["default"]["style"]["highlight"].startswith("#")
    assert not (by["default"].get("graphics_budget") or {}).get("per_minute")


def test_page_ships_the_style_first_flow(app_mod):
    html = app_mod.app.test_client().get("/").get_data(as_text=True)
    for needle in ('data-tab="style"', 'data-tab="export"', 'data-tab-group="style"',
                   'data-tab-group="export"', "studio.js", 'id="devicePicker"',
                   'id="tasteModal"', 'data-ltab="structure"', 'id="captionLookToStyle"'):
        assert needle in html, needle
    assert "CapCut Template" not in html
    assert 'id="aiEditRetell"' in html
    assert 'id="readyFindStoryBtn"' in html


def _line_from(words, t):
    picked = []
    for w in words:
        if w["start"] + 0.01 < t:
            continue
        if picked and w["start"] - picked[-1]["end"] > 1:
            break
        picked.append(w)
    return picked


def test_retelling_apply_keeps_the_spine_and_skips_the_old_planner(app_mod, talk_job, monkeypatch):
    import retelling

    def boom(*_a, **_k):
        raise AssertionError("plan_edit should not run on the retelling path")

    monkeypatch.setattr(app_mod.edit_engine, "plan_edit", boom)
    words = retelling.assign_word_ids(app_mod.jobs[talk_job]["words"])
    stop, revenue, aside, thing = (_line_from(words, t) for t in (0, 6, 10, 14))
    forty = next(w for w in revenue if "40" in w["word"])
    spine = {
        "audience": "a founder who wants the number",
        "retelling": "Revenue grew 40% last year because they stayed consistent.",
        "lead_with_turn": False,
        "spine_dynamic": "claim_proof",
        "beats": [
            {"role": "situation", "start_word_id": stop[0]["id"], "end_word_id": stop[-1]["id"]},
            {"role": "turn", "start_word_id": revenue[0]["id"], "end_word_id": revenue[-1]["id"],
             "punch_in": True, "protect_pause_before_ms": 400},
            {"role": "consequence", "start_word_id": thing[0]["id"], "end_word_id": thing[-1]["id"]},
        ],
        "graphics": [
            {
                "id": "g-num",
                "beat_role": "turn",
                "start_word_id": forty["id"],
                "end_word_id": forty["id"],
                "template": "counter",
                "presentation_mode": "framed_card",
                "justification": {
                    "kind": "ear_blind", "fact": "number", "text": "40%",
                    "function": "scale",
                    "why": "the ear misses the figure",
                },
                "source_word_ids": [forty["id"]],
                "use_person_matte": False,
                "on_asset_miss": "raw_speaker",
            },
            {
                "id": "g-ui",
                "beat_role": "turn",
                "start_word_id": revenue[0]["id"],
                "end_word_id": revenue[-1]["id"],
                "template": "media_card",
                "presentation_mode": "framed_card",
                "justification": {
                    "kind": "visual_proof", "fact": "ui", "text": "Revenue grew",
                    "function": "ui",
                    "why": "the mute test needs the chart",
                },
                "source_word_ids": [revenue[0]["id"]],
                "use_person_matte": False,
                "on_asset_miss": "raw_speaker",
            },
        ],
    }
    c = app_mod.app.test_client()
    res = c.post("/retelling/apply", json={"job_id": talk_job, "spine": spine})
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    main = body["timeline"]["tracks"]["main"]
    assert [clip["retelling_role"] for clip in main] == ["situation", "turn", "consequence"]
    aside_at = aside[0]["start"]
    assert all(not (clip["in"] <= aside_at < clip["out"]) for clip in main)
    turn = main[1]
    assert turn["in"] <= revenue[0]["start"] - 0.35
    texts = body["timeline"]["tracks"]["text"]
    assert len(texts) == 1 and texts[0]["template"] == "counter" and "40%" in texts[0]["text"]
    assert "asset_miss" in [f["reason"] for f in body["checks"]["fixes"] if f["id"] == "g-ui"]
    assert body["timeline"]["tracks"]["effects"][0]["type"] == "punch_zoom"
    assert any(s["treatment"] == "Counter" for s in body["timeline"]["structure"]["spans"])

    refused = dict(spine)
    refused["graphics"] = [{
        "id": "g-chat",
        "beat_role": "consequence",
        "start_word_id": thing[0]["id"],
        "end_word_id": thing[-1]["id"],
        "template": "quote_card",
        "presentation_mode": "framed_card",
        "justification": {
            "kind": "ear_blind", "fact": "receipt", "text": "texted",
            "why": "they mentioned a text",
        },
        "use_person_matte": False,
        "on_asset_miss": "raw_speaker",
    }]
    bad = c.post("/retelling/apply", json={"job_id": talk_job, "spine": refused})
    assert bad.status_code == 400
    assert "fact_not_in_span" in bad.get_json()["error"]


def test_retellings_route_and_apply_from_a_candidate(app_mod, talk_job, monkeypatch):
    import retelling

    words = retelling.assign_word_ids(app_mod.jobs[talk_job]["words"])
    stop, revenue, thing = (_line_from(words, t) for t in (0, 6, 14))

    def fake(prompt):
        assert "pulse" not in prompt and "every 7" not in prompt
        if "Propose two or three" in prompt:
            return {"candidates": [
                {
                    "audience": "a founder",
                    "retelling": "Revenue grew forty percent.",
                    "lead_with_turn": False,
                    "spine_dynamic": "claim_proof",
                    "beats": {
                        "situation": "Stop scrolling.",
                        "turn": "Revenue grew.",
                        "consequence": "Consistency wins.",
                    },
                    "style_pack": "pulse",
                },
                {
                    "audience": "a skeptic",
                    "retelling": "They said stop, then showed the number.",
                    "lead_with_turn": True,
                    "spine_dynamic": "escalation",
                    "beats": {
                        "situation": "Stop scrolling.",
                        "turn": "Revenue grew.",
                        "consequence": "Consistency wins.",
                    },
                },
            ]}
        return {
            "lead_with_turn": True,
            "spine_dynamic": "escalation",
            "beats": [
                {"role": "situation", "start_word_id": stop[0]["id"], "end_word_id": stop[-1]["id"]},
                {"role": "turn", "start_word_id": revenue[0]["id"], "end_word_id": revenue[-1]["id"],
                 "punch_in": True},
                {"role": "consequence", "start_word_id": thing[0]["id"], "end_word_id": thing[-1]["id"]},
            ],
            "graphics": [],
        }

    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions", fake)
    monkeypatch.setattr(app_mod.edit_engine, "plan_edit", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("plan_edit")))
    c = app_mod.app.test_client()
    listed = c.post("/retellings", json={"job_id": talk_job}).get_json()
    assert [row["id"] for row in listed["candidates"]] == ["r1", "r2"]
    assert "style_pack" not in listed["candidates"][0]
    assert listed["candidates"][1]["lead_with_turn"] is True

    applied = c.post("/retelling/apply", json={
        "job_id": talk_job,
        "candidate": listed["candidates"][1],
    })
    assert applied.status_code == 200, applied.get_json()
    body = applied.get_json()
    assert [clip["retelling_role"] for clip in body["timeline"]["tracks"]["main"]] == [
        "turn", "situation", "consequence",
    ]
    cards = body["timeline"]["tracks"]["text"]
    assert cards
    assert {c["template"] for c in cards} <= {
        "search_bar", "message_card", "counter", "quote_card", "name_card", "media_card",
    }
    assert all(c["color"] == "#F8FAFC" and c["bg_color"] == "#0F1220" for c in cards)
    assert all(c["text"].lower() not in {"google", "texted", "subscribe"} for c in cards)
    assert body["timeline"]["structure"]["spans"]
    assert body["spine"]["lead_with_turn"] is True


def test_apply_still_cuts_when_the_model_omits_word_spans(app_mod, talk_job, monkeypatch):
    """The button was 400ing on 'beats must list the kept spans'."""
    def fake(prompt):
        if "Propose two or three" in prompt:
            return {"candidates": [
                {
                    "audience": "a founder",
                    "retelling": "Revenue grew forty percent.",
                    "lead_with_turn": False,
                    "spine_dynamic": "claim_proof",
                    "beats": {
                        "situation": "Stop scrolling.",
                        "turn": "Revenue grew.",
                        "consequence": "Consistency wins.",
                    },
                },
                {
                    "audience": "a skeptic",
                    "retelling": "They said stop, then showed the number.",
                    "lead_with_turn": False,
                    "spine_dynamic": "claim_proof",
                    "beats": {
                        "situation": "Stop scrolling.",
                        "turn": "Revenue grew.",
                        "consequence": "Consistency wins.",
                    },
                },
            ]}
        # The shape Gemini actually sent back: sentences, no word ids.
        return {
            "audience": "a founder",
            "retelling": "Revenue grew forty percent.",
            "lead_with_turn": False,
            "spine_dynamic": "claim_proof",
            "beats": {
                "situation": "Stop scrolling right now.",
                "turn": "Revenue grew last year.",
                "consequence": "Consistency wins.",
            },
        }

    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions", fake)
    c = app_mod.app.test_client()
    listed = c.post("/retellings", json={"job_id": talk_job}).get_json()
    applied = c.post("/retelling/apply", json={
        "job_id": talk_job,
        "candidate": listed["candidates"][0],
    })
    assert applied.status_code == 200, applied.get_json()
    body = applied.get_json()
    roles = [clip["retelling_role"] for clip in body["timeline"]["tracks"]["main"]]
    assert roles == ["situation", "turn", "consequence"]
    joined = " ".join(clip.get("retelling_role", "") for clip in body["timeline"]["tracks"]["main"])
    assert "situation" in joined
    text = " ".join(b["text"].lower() for b in body["spine"]["beats"])
    assert "revenue" in text or "scrolling" in text


def test_apply_cuts_from_the_picked_story_when_the_model_beats_are_empty(app_mod, talk_job, monkeypatch):
    def fake(prompt):
        if "Propose two or three" in prompt:
            return {"candidates": [
                {
                    "audience": "a founder",
                    "retelling": "Revenue grew forty percent.",
                    "lead_with_turn": False,
                    "spine_dynamic": "claim_proof",
                    "beats": {
                        "situation": "Stop scrolling.",
                        "turn": "Revenue grew.",
                        "consequence": "Consistency wins.",
                    },
                },
                {
                    "audience": "a skeptic",
                    "retelling": "They said stop, then showed the number.",
                    "lead_with_turn": False,
                    "spine_dynamic": "claim_proof",
                    "beats": {
                        "situation": "Stop scrolling.",
                        "turn": "Revenue grew.",
                        "consequence": "Consistency wins.",
                    },
                },
            ]}
        return {"beats": {"summary": "not three spans"}}

    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions", fake)
    c = app_mod.app.test_client()
    listed = c.post("/retellings", json={"job_id": talk_job}).get_json()
    applied = c.post("/retelling/apply", json={
        "job_id": talk_job,
        "candidate": listed["candidates"][0],
    })
    assert applied.status_code == 200, applied.get_json()
    body = applied.get_json()
    assert [b["role"] for b in body["spine"]["beats"]] == ["situation", "turn", "consequence"]
    assert "beats must list" not in (body.get("error") or "")
