"""Taste memory: learning, retrieval, hygiene, and where it reaches prompts."""
import sys
import time
from pathlib import Path

import pytest

import taste

DAY = 86400


@pytest.fixture
def store(tmp_path):
    return taste.TasteStore(tmp_path / "taste.json")


def test_user_rules_always_lead_retrieval(store):
    store.add("rule", "Never zoom on my co-host", tags=["punch"])
    for i in range(10):
        store.add("prefer", f"Keeps suggested thing number {i} for cooking videos",
                  source="learned")
    top = store.retrieve("cooking video edit", k=3)
    assert top[0]["text"] == "Never zoom on my co-host"
    assert "cooking" in top[1]["text"]


def test_repeats_strengthen_instead_of_duplicating(store):
    a = store.add("avoid", "Removes suggested punch zoom", key="punch", source="learned")
    b = store.add("avoid", "removes suggested  punch zoom", key="punch", source="learned")
    assert a["id"] == b["id"] and b["weight"] == 1.5 and len(store.items()) == 1


def test_learning_from_corrections(store):
    p = {"type": "punch_zoom", "start": 1, "end": 3, "intensity": "strong"}
    store.learn_from_suggestion("effect", p, "rejected")
    store.learn_from_suggestion("effect", p, "rejected")
    got = store.learn_from_suggestion("effect", p, "edited",
                                      dict(p, intensity="low", end=1.8, aim={"x": .4, "y": .3}))
    texts = [g["text"] for g in got]
    assert "Prefers punch zoom at low intensity, not strong" in texts
    assert "Aims punch zoom at the speaker's face" in texts
    assert "Prefers shorter punch zooms" in texts
    assert store.plan_hints()["avoid"] == ["punch"]
    assert store.learn_from_suggestion("effect", p, "proposed") == []


def test_prune_decays_learned_but_keeps_rules(store):
    old = time.time() - 200 * DAY
    store.add("rule", "Brand yellow is #FFD60A", now=old)
    store.add("avoid", "Removes suggested ken burns", source="learned", now=old)
    store.add("prefer", "Keeps suggested punch zoom", source="learned")
    res = store.prune(ttl_days=120)
    assert res == {"dropped": 1, "kept": 2}
    kinds = sorted(i["kind"] for i in store.items())
    assert kinds == ["prefer", "rule"]
    assert store.reset() == 1 and [i["kind"] for i in store.items()] == ["rule"]
    assert store.reset("all") == 1 and store.items() == []


def test_prompt_block_is_empty_without_memory(store):
    assert store.prompt_block("anything") == ""
    store.add("bad_example", "Stock photos of people shaking hands")
    assert "Never like this: Stock photos of people shaking hands" in store.prompt_block("b-roll")


def test_feedback_route_teaches_and_prompts_listen(app_mod, temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "TASTE", taste.TasteStore(tmp_path / "t.json"))
    c = app_mod.app.test_client()
    proposed = {"type": "ken_burns", "start": 2, "end": 8, "intensity": "med"}
    sid = c.post("/suggestion-feedback", json={"job_id": "j", "kind": "effect",
                                               "outcome": "proposed", "proposed": proposed}
                 ).get_json()["suggestion_id"]
    # Resolve by id without resending the proposal: it is read back from the row.
    r = c.post("/suggestion-feedback", json={"job_id": "j", "kind": "effect",
                                             "outcome": "rejected", "suggestion_id": sid}).get_json()
    assert r["learned"] == ["Removes suggested ken burns at med intensity"]
    assert c.post("/taste", json={"kind": "rule", "text": "Keep cuts on the beat"}).status_code == 200
    assert c.post("/taste", json={"kind": "avoid", "text": "x"}).status_code == 400
    listing = c.get("/taste").get_json()
    assert listing["items"][0]["text"] == "Keep cuts on the beat"
    prompt = app_mod._build_effect_suggestion_prompt("[00:01] hi", 10.0, 3)
    assert "THIS EDITOR'S TASTE" in prompt and "Keep cuts on the beat" in prompt
    rid = listing["items"][0]["id"]
    assert c.delete(f"/taste/{rid}").get_json()["ok"]
    assert c.post("/taste/reset", json={}).get_json()["removed"] == 1


def test_memory_lint_finds_rot(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import prune_context
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("- [Good](good.md)\n- [Gone](gone.md)\n")
    (mem / "good.md").write_text("---\nname: g\ndescription: d\n---\nsee [x](missing.md)\n")
    (mem / "loose.md").write_text("no header\n")
    rep = prune_context.lint_memory(mem, stale_days=-1)
    assert {d["target"] for d in rep["dead_links"]} == {"gone.md", "missing.md"}
    assert rep["orphans"] == ["loose.md"]
    assert rep["missing_front_matter"] == ["loose.md"]
    assert {s["note"] for s in rep["stale"]} == {"good.md", "loose.md"}
    assert prune_context.main(["--memory", str(mem), "--taste", str(tmp_path / "none.json")]) == 1


def test_the_repo_memory_index_has_no_dead_links():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import prune_context
    rep = prune_context.lint_memory(prune_context.REPO / ".agents" / "memory")
    assert not rep["dead_links"], rep


def test_learning_reads_the_editor_state_shape(store):
    proposed = {"type": "ken_burns", "start": 2.0, "dur": 6.0, "intensity": "low", "direction": "in"}
    got = store.learn_from_suggestion("effect", proposed, "edited", dict(proposed, dur=2.0))
    assert [g["text"] for g in got] == ["Prefers shorter ken burns"]
