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
    r = c.post("/edit-plan", json={"job_id": talk_job, "style_pack": "velocity",
                                   "intensity": "high"}).get_json()
    roles = [s["role"] for s in r["structure"]["spans"]]
    assert roles[0] == "hook" and roles[-1] == "thesis"
    assert r["style"]["pack"] == "Velocity"
    assert "spans" not in r["style"] and "caption" not in r["structure"]


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
    assert tl["style_doc"]["devices"]["per_minute"] == 8
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
    assert any(fx.get("fx_origin") == "plan" for fx in tl["tracks"]["effects"]) or not (
        treatments & {"punch", "clean_zoom"})
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
