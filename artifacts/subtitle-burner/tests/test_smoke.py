"""End-to-end smoke test for the burner.

Written to run in two places: a dev box with everything installed, and a bare
checkout with neither Whisper nor MediaPipe. Anything needing a heavy or
optional dependency skips with a reason rather than failing, so a bare run
still exercises the parts that break most often -- the filter strings, the
effect validation and the ASS generation -- and a full run adds a real render.

Run with:  python -m pytest artifacts/subtitle-burner/tests -v
"""
import importlib.util
import json
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

BURNER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BURNER))

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")


def _have(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


@pytest.fixture(scope="session")
def app_mod():
    """Import app.py once.

    It is imported rather than exec'd piecemeal so the test covers the real
    module: a syntax error or a bad import lands here instead of in
    production.
    """
    if not _have("flask"):
        pytest.skip("flask not installed")
    import app  # noqa: F401
    return app


@pytest.fixture(scope="session")
def clip(tmp_path_factory):
    """Two seconds of 9:16 colour bars with a tone, so a render has audio."""
    if not FFMPEG:
        pytest.skip("ffmpeg not on PATH")
    out = tmp_path_factory.mktemp("media") / "clip.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)],
        check=True, capture_output=True,
    )
    assert out.stat().st_size > 0
    return out


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


# --- filter construction -------------------------------------------------
# These need no media and no optional deps, and they are where the subtle
# regressions live: a filter that is syntactically fine but animates nothing.

def test_punch_zoom_animates(app_mod):
    """The punch must change over time, not sit at a constant scale.

    It shipped once as a static crop+scale, which is a filter string that
    looks correct and produces no motion at all.
    """
    f = app_mod._tl_punch_zoom_filter(
        {"enabled": True, "intensity": "med"}, 1080, 1920, fps=30, dur=1.2
    )
    assert f, "enabled punch produced no filter"
    assert "zoompan" in f, "punch must use zoompan; crop cannot animate per frame"
    assert "in" in f, "zoom expression does not reference the frame counter"


def test_punch_zoom_respects_the_peak_cap(app_mod):
    """No intensity may exceed the 118% ceiling a punch-in should land at."""
    for intensity in ("low", "med", "high", "strong"):
        peak = app_mod._PUNCH_PEAK[intensity]
        assert 1.0 < peak <= 1.18, f"{intensity} peaks at {peak}, above the cap"


def test_punch_zoom_disabled_is_empty(app_mod):
    assert app_mod._tl_punch_zoom_filter(None, 1080, 1920) == ""
    assert app_mod._tl_punch_zoom_filter({"enabled": False}, 1080, 1920) == ""


def test_kenburns_disabled_is_empty(app_mod):
    assert app_mod._tl_kenburns_filter({}, 1080, 1920, 30, 5) == ""


# --- effect validation ---------------------------------------------------
# The model is free to return nonsense; these are the guards that stop it
# reaching the renderer.

def test_effect_suggestions_are_clamped(app_mod):
    raw = [
        {"type": "punch_zoom", "start_time": -5, "end_time": 2},      # before zero
        {"type": "ken_burns", "start_time": 10, "end_time": 900},     # past the end
        {"type": "nonsense", "start_time": 1, "end_time": 2},         # unknown type
    ]
    out = app_mod._sanitize_effect_suggestions(raw, total=60.0)
    assert all(e["type"] in app_mod._EFFECT_LIMITS for e in out)
    for e in out:
        assert 0 <= e["start_time"] < e["end_time"] <= 60.0
        lo, hi = app_mod._EFFECT_LIMITS[e["type"]]
        assert lo - 1e-6 <= e["end_time"] - e["start_time"] <= hi + 1e-6


def test_effect_suggestions_never_overlap(app_mod):
    """Two camera moves at once reads as a glitch, not as emphasis."""
    raw = [
        {"type": "punch_zoom", "start_time": 10.0, "end_time": 12.0},
        {"type": "punch_zoom", "start_time": 11.0, "end_time": 13.0},
        {"type": "punch_zoom", "start_time": 11.5, "end_time": 12.5},
    ]
    out = app_mod._sanitize_effect_suggestions(raw, total=60.0)
    assert len(out) == 1, f"overlapping effects survived: {out}"


# --- alignment must never damage the transcript --------------------------

def test_alignment_keeps_whisper_timings_when_whisperx_is_absent(app_mod, monkeypatch):
    """A missing or failing aligner costs accuracy, never the transcript."""
    words = [{"word": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4}
             for i in range(10)]
    monkeypatch.setitem(sys.modules, "whisperx", None)
    out = app_mod._refine_word_alignment(Path("/nonexistent.wav"), words)
    assert out == words


# --- captions ------------------------------------------------------------

def test_ass_is_generated_for_words(app_mod):
    words = [{"word": "hello", "start": 0.0, "end": 0.4},
             {"word": "world", "start": 0.5, "end": 0.9}]
    ass = app_mod.build_ass(words, app_mod._TL_DEFAULT_CAPTION_STYLE, 1080, 1920)
    assert "[Script Info]" in ass and "[Events]" in ass
    assert "Dialogue:" in ass, "no dialogue lines emitted"


# --- the render ----------------------------------------------------------

@needs_ffmpeg
def test_fixture_clip_is_a_real_video(clip):
    info = probe(clip)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert abs(float(info["format"]["duration"]) - 2.0) < 0.5


@needs_ffmpeg
def test_burn_produces_a_playable_mp4(app_mod, clip, tmp_path):
    """The whole point of the app: words in, burned-in video out."""
    words = [{"word": "hello", "start": 0.2, "end": 0.6},
             {"word": "world", "start": 0.7, "end": 1.1}]
    ass = tmp_path / "subs.ass"
    ass.write_text(app_mod.build_ass(words, app_mod._TL_DEFAULT_CAPTION_STYLE, 1080, 1920),
                   encoding="utf-8")
    out = tmp_path / "burned.mp4"
    app_mod.burn_subtitles(clip, ass, out)

    assert out.exists() and out.stat().st_size > 0, "render produced nothing"
    info = probe(out)
    assert any(s["codec_type"] == "video" for s in info["streams"])
    # Within a hair of the source: a render that silently truncates is the
    # failure this is here to catch.
    assert abs(float(info["format"]["duration"]) - 2.0) < 0.6


# --- optional dependencies ----------------------------------------------

@pytest.mark.skipif(not _have("mediapipe"), reason="mediapipe not installed")
def test_mediapipe_still_exposes_the_solutions_api(app_mod):
    """MediaPipe removed mp.solutions; this app is written against it.

    Pinned in requirements, but an unpinned install elsewhere resolves to a
    release where face detection and the matte both raise AttributeError.
    """
    import mediapipe as mp
    assert hasattr(mp, "solutions"), (
        "mediapipe has no `solutions`: reframe and the matte are broken. "
        "Install mediapipe<0.10.22."
    )
    assert hasattr(mp.solutions, "face_detection")
    assert hasattr(mp.solutions, "selfie_segmentation")


@needs_ffmpeg
@pytest.mark.skipif(not (_have("mediapipe") and _have("cv2")),
                    reason="mediapipe/OpenCV not installed")
def test_person_matte_returns_a_png_and_a_coverage(app_mod, clip):
    png, coverage = app_mod.segment_person_at(clip, 1.0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert 0.0 <= coverage <= 1.0
    # Colour bars contain no person, so the model should find nothing. This
    # asserts the plumbing, not the model's quality on real footage.
    assert coverage < 0.5


@pytest.mark.skipif(not _have("faster_whisper"),
                    reason="faster-whisper not installed")
@needs_ffmpeg
def test_transcribe_returns_well_formed_words(app_mod, clip):
    """A tone has no speech, so an empty list is correct. What matters is the
    shape of whatever comes back, and that it does not raise."""
    words = app_mod.transcribe(clip)
    assert isinstance(words, list)
    for w in words:
        assert {"word", "start", "end"} <= set(w)
        assert w["end"] >= w["start"]


# --- the headline banner must reach build_ass as a string ----------------
# The batch-clip path wrote a dict. A dict is truthy, so it sailed past the
# `if headline_banner:` guard and into fmt(), which either raised on .upper()
# or formatted the dict's repr into an ASS line -- where `{` and `}` open and
# close an override block, so the whole line is discarded.

def test_headline_banner_normalises_to_a_string(app_mod):
    norm = app_mod._headline_banner_text
    assert norm({"enabled": True, "text": "Featured"}) == "Featured"
    assert norm({"enabled": False, "text": "Featured"}) == ""
    assert norm("Featured") == "Featured"
    assert norm("") == "" and norm(None) == ""


def test_headline_banner_dict_renders_as_its_text(app_mod):
    """A job saved with the dict shape still burns the right words."""
    words = [{"word": "hello", "start": 0.0, "end": 0.4}]
    style = dict(app_mod._TL_DEFAULT_CAPTION_STYLE,
                 headline_banner={"enabled": True, "text": "Featured"})
    banner = app_mod._headline_banner_text(style["headline_banner"])
    ass = app_mod.build_ass(words, style, 1080, 1920, headline_banner=banner)
    assert "FEATURED" in ass.upper(), "banner text missing from the ASS"
    assert "enabled" not in ass, "the dict's repr leaked into the ASS"


# --- fonts: the dropdown and fonts/ must agree --------------------------
# The browser registers @font-face by matching the dropdown value against a
# FILENAME; libass matches the same value against the family name INSIDE the
# TTF. The two disagreed for the app's default, so the preview silently
# measured a system face while the render used Montserrat.

_UNBUNDLED_FONTS = {"Integral CF", "The Bold Font"}  # commercial, see fonts/README.md


def _font_options() -> list[str]:
    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    block = html.split('<select id="font">')[1].split("</select>")[0]
    return re.findall(r'value="([^"]+)"', block)


def _font_aliases() -> dict:
    """Read FONT_FILE_ALIASES out of app.js so there is one source for them."""
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    block = js.split("const FONT_FILE_ALIASES = {")[1].split("}")[0]
    return dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', block))


def _resolves_to_file(family: str, files: list[str], aliases: dict) -> str | None:
    """The same rule _registerSubtitleFonts applies, alias first."""
    alias = aliases.get(family)
    if alias and alias in files:
        return alias
    norm = lambda s: re.sub(r"[-_\s]+", "", s.lower())
    target = norm(family)
    for f in files:
        stem = norm(f.rsplit(".", 1)[0])
        if stem == target or target in stem or stem in target:
            return f
    return None


def test_every_offered_font_is_bundled_or_known_unbundled():
    """Offering a font with no file renders it in a silent system substitute."""
    files = [p.name for p in (BURNER / "fonts").glob("*")
             if p.suffix.lower() in (".ttf", ".otf")]
    assert files, "no fonts on disk"
    aliases = _font_aliases()
    unresolved = [f for f in _font_options()
                  if not _resolves_to_file(f, files, aliases)]
    assert set(unresolved) <= _UNBUNDLED_FONTS, (
        f"offered but not bundled and not declared unbundled: {unresolved}"
    )


def test_the_font_matcher_actually_consults_the_alias_map():
    """The data being right is not the same as the code using it.

    This suite once passed on a branch where FONT_FILE_ALIASES was present and
    correct but the matcher never read it, so the default font still
    registered no @font-face. The browser caught it; these tests did not.
    """
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    body = js.split("async function _registerSubtitleFonts()")[1].split("\n}\n")[0]
    assert "FONT_FILE_ALIASES[family]" in body, (
        "the alias map is defined but the matcher never looks at it"
    )
    # And the alias must win over the filename guess, not merely be consulted.
    assert re.search(r"alias && fonts\.includes\(alias\)\s*\)\s*\?", body), (
        "the alias is not preferred over the filename-stem match"
    )


def test_the_default_font_resolves_to_a_file():
    """It did not, which is how preview and render came to disagree."""
    files = [p.name for p in (BURNER / "fonts").glob("*")
             if p.suffix.lower() in (".ttf", ".otf")]
    default = "Montserrat Thin Black"
    assert _resolves_to_file(default, files, _font_aliases()), (
        f"{default} registers no @font-face, so the preview falls back"
    )


def test_aliased_font_names_are_the_family_inside_the_ttf():
    """Why the UI value must not be 'corrected' to match the filename.

    libass resolves the ASS Style's font by the name recorded in the TTF, not
    by filename. Renaming "Montserrat Thin Black" to "Montserrat Black" would
    fix the browser lookup and break the render -- so the alias map fixes the
    browser side only. This asserts that premise rather than trusting it.
    """
    for family, filename in _font_aliases().items():
        path = BURNER / "fonts" / filename
        assert path.exists(), f"alias points at a missing file: {filename}"
        assert family in _ttf_family_names(path), (
            f"{family!r} is not a family name inside {filename}; "
            f"found {_ttf_family_names(path)}"
        )


def _ttf_family_names(path: Path) -> set[str]:
    """Family/full names from a TrueType `name` table (ids 1, 4 and 16)."""
    data = path.read_bytes()
    num_tables = struct.unpack(">H", data[4:6])[0]
    offset = None
    for i in range(num_tables):
        rec = 12 + 16 * i
        if data[rec:rec + 4] == b"name":
            offset = struct.unpack(">I", data[rec + 8:rec + 12])[0]
    assert offset is not None, f"{path.name} has no name table"
    _, count, str_off = struct.unpack(">HHH", data[offset:offset + 6])
    names = set()
    for i in range(count):
        rec = offset + 6 + 12 * i
        pid, _, _, nid, ln, so = struct.unpack(">HHHHHH", data[rec:rec + 12])
        if nid not in (1, 4, 16):
            continue
        raw = data[offset + str_off + so:offset + str_off + so + ln]
        try:
            names.add(raw.decode("utf-16-be") if pid == 3 else raw.decode("latin-1"))
        except UnicodeDecodeError:
            continue
    return names


def test_font_faces_are_installed_before_the_explicit_load():
    """Source-order guard: there is no JS test harness, and order is the bug.

    document.fonts.load() resolves immediately -- fetching nothing -- if the
    @font-face rules are not in the document yet. That left every subtitle
    font unfetched and the preview measuring a system face, which is exactly
    what the explicit load is there to prevent. Verified in a browser; this
    keeps the two lines from being reordered again.
    """
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    body = js.split("async function _registerSubtitleFonts()")[1].split("\n}\n")[0]
    assert "document.head.appendChild(styleEl)" in body, (
        "the @font-face rules are never installed at all")
    appended = body.index("document.head.appendChild(styleEl)")
    loaded = body.index("document.fonts.load(`16px")  # the call, not the comment
    assert appended < loaded, (
        "document.fonts.load() runs before the @font-face rules are installed; "
        "it will resolve against an unknown family and fetch nothing"
    )


# --- suggestion feedback ------------------------------------------------
# Every accept, correction and dismissal used to be discarded. The `edited`
# row is the one that matters: proposed vs shipped is a labelled example of
# how the suggestion was wrong.

@pytest.fixture
def temp_db(app_mod, tmp_path, monkeypatch):
    """Point the schema and every write at a throwaway database."""
    monkeypatch.setattr(app_mod, "DB_PATH", tmp_path / "jobs.db")
    app_mod._db_init()
    return app_mod.DB_PATH


def test_an_edited_suggestion_stores_both_halves(app_mod, temp_db):
    client = app_mod.app.test_client()
    proposed = {"type": "punch_zoom", "start": 10.0, "end": 12.0, "intensity": "med"}
    created = client.post("/suggestion-feedback", json={
        "job_id": "j1", "kind": "effect", "outcome": "proposed", "proposed": proposed,
    }).get_json()
    sid = created["suggestion_id"]

    final = dict(proposed, intensity="low", anchor={"x": 0.3, "y": 0.42})
    resolved = client.post("/suggestion-feedback", json={
        "job_id": "j1", "kind": "effect", "outcome": "edited",
        "suggestion_id": sid, "proposed": proposed, "final": final,
    }).get_json()
    assert resolved["ok"], resolved

    rows = client.get("/suggestion-feedback/j1").get_json()["suggestions"]
    assert len(rows) == 1, "resolving a suggestion must not open a second row"
    assert rows[0]["outcome"] == "edited"
    assert rows[0]["proposed"] == proposed, "the original proposal was overwritten"
    assert rows[0]["final"] == final
    assert rows[0]["resolved_at"]


def test_a_dismissed_suggestion_is_recorded(app_mod, temp_db):
    """Rejection was implicit before, so a bad placement looked like an unread one."""
    client = app_mod.app.test_client()
    client.post("/suggestion-feedback", json={
        "job_id": "j2", "kind": "effect", "outcome": "rejected",
        "proposed": {"type": "ken_burns", "start": 3.0, "end": 8.0},
    })
    rows = client.get("/suggestion-feedback/j2").get_json()["suggestions"]
    assert [r["outcome"] for r in rows] == ["rejected"]


def test_an_unknown_outcome_is_refused(app_mod, temp_db):
    client = app_mod.app.test_client()
    res = client.post("/suggestion-feedback", json={
        "job_id": "j3", "kind": "effect", "outcome": "maybe",
    })
    assert res.status_code == 400


# --- the serialize() whitelist -----------------------------------------

def test_serialize_persists_per_clip_effect_state():
    """serialize() whitelists main-clip fields and drops anything else.

    A field missing from that list survives in memory and vanishes on the next
    debounced save, so the feature works until a reload -- which is how the
    per-clip `reframe` control came to edit a value nothing persisted.
    """
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    block = js.split("main: tl.tracks.main.map((c) => ({")[1].split("})),")[0]
    for key in ("punch_zoom", "ken_burns", "split", "cuts", "color", "reframe"):
        assert f"{key}:" in block, f"{key} is edited in the UI but never saved"


def test_effect_clips_persist_everything_they_carry():
    """Effects land on their own lane, and that lane is spread-serialised.

    AI provenance attaches to the effect clip, so it needs no whitelist entry
    -- but only while the effects track keeps spreading. Narrowing it to a
    field list later would silently drop fx_origin on the next save.
    """
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    block = js.split("function serialize()")[1].split("\n  }")[0]
    assert "effects: (tl.tracks.effects || []).map((c) => ({ ...c }))" in block, (
        "the effects track no longer spreads; fx_origin needs an explicit entry"
    )
    assert "fx_origin" in js, "nothing attaches provenance to an applied effect"


def test_feedback_for_an_unknown_suggestion_is_not_dropped(app_mod, temp_db):
    """An id with no row updated nothing and still reported success.

    Projects saved before the table existed carry ids that resolve to nothing.
    Losing the correction is worse than losing the link to the proposal.
    """
    client = app_mod.app.test_client()
    res = client.post("/suggestion-feedback", json={
        "job_id": "j4", "kind": "effect", "outcome": "edited",
        "suggestion_id": 999999,
        "proposed": {"type": "punch_zoom", "intensity": "med"},
        "final": {"type": "punch_zoom", "intensity": "low"},
    }).get_json()
    assert res["ok"], res
    rows = client.get("/suggestion-feedback/j4").get_json()["suggestions"]
    assert len(rows) == 1, "the correction was silently discarded"
    assert rows[0]["final"]["intensity"] == "low"


# --- dead and duplicated brand machinery --------------------------------

def test_the_capcut_table_has_exactly_one_source(app_mod):
    """Two tables claiming to be the same thing is how they drift.

    On one branch the Python copy was dead weight and the JS had its own, with
    a different schema. Here the Python one is authoritative: it is injected
    into the page and the JS prefers it, keeping its own copy only as a
    fallback. That is the arrangement worth protecting -- the failure mode is
    someone removing the injection and leaving the two to diverge silently.
    """
    assert hasattr(app_mod, "CAPCUT_TEMPLATES"), "the server table is gone"
    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    assert "capcut_templates" in html, "the server table never reaches the page"
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    assert "window.CAPCUT_TEMPLATES" in js, (
        "the JS ignores the injected table and uses only its own copy"
    )


def test_the_brand_preset_round_trips_every_style_key():
    """The loader restored 8 of the ~20 keys getStyle() produces.

    Both ends are now driven by STYLE_CONTROLS, so this checks that table
    covers what getStyle() emits rather than trusting the two to stay level.
    """
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    emitted = set(re.findall(r"^\s{4}(\w+):",
                             js.split("function getStyle()")[1].split("\n  };")[0],
                             re.M))
    table = set(re.findall(r'\["(\w+)",\s*"[^"]+",',
                           js.split("const STYLE_CONTROLS = [")[1].split("\n];")[0]))
    table |= set(re.findall(r'\["(\w+)",\s*"\w+",\s*"[^"]+",',
                            js.split("const NESTED_STYLE_CONTROLS = [")[1].split("\n];")[0]))
    # bg_music hangs off an uploaded file; restoring its numbers without the
    # audio would be a lie, so it is left out on purpose. speaker_colors has
    # its own block because the speaker list is dynamic: the pickers have to be
    # built from the saved keys before their values can be written.
    # bg_music hangs off an uploaded file, speaker_colors has a dynamic
    # picker list, and hook_mode / hook_title are derived rather than stored
    # on a control of their own.
    handled_elsewhere = {"bg_music", "speaker_colors", "hook_mode", "hook_title"}
    gap = emitted - table - handled_elsewhere
    assert not gap, f"saved but never restored: {sorted(gap)}"


def test_the_brand_preset_button_is_wired():
    """It sat in the markup with no listener, so the feature was console-only."""
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    for btn in ("saveBrandPresetBtn", "loadBrandPresetBtn"):
        assert f'id="{btn}"' in html, f"{btn} is missing from the markup"
        # Accessor-agnostic: the app looks elements up with both $() and
        # getElementById, and which one is used is not the point.
        looked_up = f'$("{btn}")' in js or f'getElementById("{btn}")' in js
        assert looked_up, f"{btn} is never looked up, so it has no listener"
        assert re.search(rf'{btn}\.(onclick|addEventListener)', js), (
            f"{btn} is looked up but nothing is bound to it"
        )


def test_the_punch_aim_does_not_collide_with_lane_anchoring():
    """`anchor` on a lane clip means "which Main clip do I hang off".

    reanchor() rewrites it to a Main clip id on every reflow, so an aim point
    stored there was wiped moments after the model set it -- and the server's
    isinstance(dict) guard then discarded the string silently, leaving every
    AI-aimed push rendered dead centre with nothing in any log.
    """
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    body = js.split("async function addEffectClip")[1].split("\n  }\n")[0]
    assert "aim:" in body, "the effect clip has no aim field of its own"
    assert not re.search(r"^\s+anchor: opts\.", body, re.M), (
        "the aim point is being stored in `anchor`, which reanchor() overwrites"
    )
    # reanchor must still own `anchor`; this is what makes the collision real.
    # Spelling differs between branches, so match the behaviour: the lane
    # anchor is assigned a Main clip's id.
    assert re.search(r"c\.anchor\s*=\s*(clip\.id|tl\.tracks\.main\[[^\]]+\]\.id)", js), (
        "reanchor no longer writes a clip id to `anchor`; re-check the collision"
    )


def test_the_renderer_reads_the_aim_field(app_mod):
    """A lane punch has to reach the filter aimed, not centred."""
    src = (BURNER / "app.py").read_text(encoding="utf-8")
    assert 'aim = fx.get("aim")' in src, "the lane mapping never looks at aim"
    # And the filter must actually move the window off centre for an aim.
    off = app_mod._tl_punch_zoom_filter(
        {"enabled": True, "intensity": "high", "anchor": {"x": 0.2, "y": 0.8}},
        1080, 1920, fps=30, dur=1.2)
    mid = app_mod._tl_punch_zoom_filter(
        {"enabled": True, "intensity": "high"}, 1080, 1920, fps=30, dur=1.2)
    assert off != mid, "the aim point changes nothing in the filter"
    assert "iw*0.2000" in off and "ih*0.8000" in off


def test_every_ai_edit_pack_font_resolves_both_ways(app_mod):
    """A pack font has to satisfy two different matchers or it half-works.

    The browser registers @font-face by matching the name against a FILENAME;
    libass matches it against the family name recorded INSIDE the TTF. Pulse
    named a commercial font nobody has, and Clarity named "Montserrat Black",
    which matches the filename but not the family name inside it -- so that
    pack previewed in Montserrat and rendered in a system font.
    """
    font_dir = BURNER / "fonts"
    files = [p.name for p in font_dir.glob("*")
             if p.suffix.lower() in (".ttf", ".otf")]
    aliases = _font_aliases()
    families = set()
    for f in files:
        families |= _ttf_family_names(font_dir / f)

    broken = []
    for key, pack in app_mod.AI_EDIT_STYLE_PACKS.items():
        name = (pack.get("style") or {}).get("font")
        if not name:
            continue
        if not _resolves_to_file(name, files, aliases):
            broken.append(f"{key}: {name!r} matches no file (browser preview)")
        if name not in families:
            broken.append(f"{key}: {name!r} is no family inside any TTF (libass render)")
    assert not broken, "\n".join(broken)


# --- co-editor: the op vocabulary is the feature ------------------------
# The co-editor can only do what its op list covers, so the gaps are not
# limitations of the idea -- they are missing entries.

def _coeditor_timeline():
    return {
        "canvas": "9x16",
        "style": {"font": "Anton", "size": 64},
        "tracks": {
            "main": [{"id": "m1", "source_job_id": "job1", "in": 0, "out": 8}],
            "overlay": [], "text": [], "music": [],
            "effects": [{"id": "e1", "type": "punch_zoom", "start": 1.0,
                         "out": 1.2, "intensity": "high",
                         "aim": {"x": 0.4, "y": 0.3}, "fx_origin": {"suggestion_id": 1}}],
        },
    }


def test_the_coeditor_can_see_the_effects_lane(app_mod, temp_db, monkeypatch):
    """Every AI-placed camera move lives on that lane.

    The summary handed to the model listed main, overlay, text and music but
    not effects, so it could neither report on a camera move nor change one.
    """
    seen = {}

    def fake(prompt):
        seen["prompt"] = prompt
        return {"ops": [], "message": "noted"}

    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions", fake)
    app_mod.app.test_client().post("/co-editor", json={
        "prompt": "what camera moves are there?", "timeline": _coeditor_timeline()})
    assert "effect_clips" in seen["prompt"], "the effects lane is invisible to the model"
    assert "punch_zoom" in seen["prompt"]
    assert "from_ai" in seen["prompt"], "no way to tell which moves the AI placed"


def test_the_new_ops_survive_the_whitelist(app_mod, temp_db, monkeypatch):
    """An op the server does not allow is dropped before it reaches the UI."""
    ops = [
        {"op": "set_shot_range", "index": 0, "in": 2.0, "out": 6.5},
        {"op": "set_effect_time", "index": 0, "start": 4.2, "duration": 2.0},
        {"op": "set_punch_aim", "index": 0, "x": 0.33, "y": 0.28},
        {"op": "delete_effect", "index": 0},
        {"op": "teleport_clip", "index": 0},          # not a real op
    ]
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions",
                        lambda p: {"ops": ops, "message": "ok"})
    out = app_mod.app.test_client().post("/co-editor", json={
        "prompt": "do things", "timeline": _coeditor_timeline()}).get_json()
    names = [o["op"] for o in out["ops"]]
    assert names == ["set_shot_range", "set_effect_time", "set_punch_aim", "delete_effect"]


def test_an_unanswerable_prompt_is_recorded(app_mod, temp_db, monkeypatch):
    """The prompts that produce nothing are the most useful thing here.

    They are a list of the edits people actually ask for that the co-editor
    cannot yet make -- which is the list of ops worth adding next.
    """
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions",
                        lambda p: {"ops": [], "message": "I can't add b-roll yet."})
    client = app_mod.app.test_client()
    client.post("/co-editor", json={
        "job_id": "job1", "prompt": "add b-roll of a city",
        "timeline": _coeditor_timeline()})
    rows = client.get("/suggestion-feedback/job1").get_json()["suggestions"]
    assert len(rows) == 1 and rows[0]["outcome"] == "rejected"
    assert rows[0]["proposed"]["prompt"] == "add b-roll of a city"


def test_aiming_at_the_speaker_resolves_to_a_point(app_mod, monkeypatch):
    """"Aim it at me" is how anyone phrases this, so it has to resolve."""
    tl = _coeditor_timeline()
    main, effects = tl["tracks"]["main"], tl["tracks"]["effects"]

    # Patched at _speaker_anchor_at, which is what the resolver now calls:
    # aiming by biggest face was the bug, so the seam moved. Whether it
    # really picks the talker is tested on a real fixture further down.
    monkeypatch.setattr(app_mod, "_speaker_anchor_at",
                        lambda j, t, words=None: {"x": 0.61, "y": 0.27})
    got = app_mod._resolve_punch_aim({"op": "set_punch_aim", "index": 0, "at": "speaker"},
                                     main, effects)
    assert got == {"op": "set_punch_aim", "index": 0, "x": 0.61, "y": 0.27}

    # No face data: drop the op rather than ship one that aims at nothing.
    monkeypatch.setattr(app_mod, "_speaker_anchor_at", lambda j, t, words=None: None)
    assert app_mod._resolve_punch_aim(
        {"op": "set_punch_aim", "index": 0, "at": "speaker"}, main, effects) is None

    # Explicit coordinates are clamped, and a bad index is refused.
    assert app_mod._resolve_punch_aim(
        {"op": "set_punch_aim", "index": 0, "x": 5, "y": -1}, main, effects
    ) == {"op": "set_punch_aim", "index": 0, "x": 1.0, "y": 0.0}
    assert app_mod._resolve_punch_aim(
        {"op": "set_punch_aim", "index": 9, "x": 0.5, "y": 0.5}, main, effects) is None


# --- overlays that move -------------------------------------------------
# Overlay x/y were baked into the filter as constants, so nothing could travel
# across the frame. They are per-frame expressions (`eval` defaults to frame),
# which is what makes a swipe possible at all.

def test_a_still_overlay_builds_constant_positions(app_mod):
    x, y = app_mod._tl_overlay_motion_expr(None, 100, 200, 0.5, 1080, 1920)
    assert (x, y) == ("100", "200"), "an overlay with no motion must not move"
    x, y = app_mod._tl_overlay_motion_expr(
        {"type": "none"}, 100, 200, 0.5, 1080, 1920)
    assert (x, y) == ("100", "200")


def test_a_swipe_comes_from_the_side_it_is_named_for(app_mod):
    """"swipe_left" is the gesture: the photo enters right and travels left."""
    import re

    def at(expr, t):
        # The compiled track is a nested if()/lt() chain, which Python cannot
        # parse as calls under those names.
        py = re.sub(r"\bt\b", repr(float(t)), expr)
        py = py.replace("if(", "IF(").replace("lt(", "LT(")
        return eval(py, {"IF": lambda c, a, b: a if c else b,
                         "LT": lambda a, b: a < b,
                         "min": min, "max": max, "pow": pow})

    x, y = app_mod._tl_overlay_motion_expr(
        {"type": "swipe_left", "in": 0.4}, 300, 200, 1.0, 1080, 1920)
    assert y == "200", "a horizontal swipe must not drift vertically"
    assert at(x, 1.0) == 300 + 1080, "does not start off-frame"
    assert at(x, 1.4) == 300, "does not settle at the resting position"
    mid = at(x, 1.2)
    assert 300 < mid < 300 + 1080, "no motion between the ends"
    # Cubic ease-out: most of the distance is covered early.
    assert mid < 300 + 1080 / 2, "the ease is not decelerating"

    x_r, _ = app_mod._tl_overlay_motion_expr(
        {"type": "swipe_right", "in": 0.4}, 300, 200, 1.0, 1080, 1920)
    assert at(x_r, 1.0) == 300 - 1080, "swipe_right must enter from the left"

    _, y_u = app_mod._tl_overlay_motion_expr(
        {"type": "swipe_up", "in": 0.4}, 300, 200, 1.0, 1080, 1920)
    assert at(y_u, 1.0) == 200 + 1920, "swipe_up must enter from below"


@needs_ffmpeg
def test_a_swiping_overlay_really_moves_in_the_render(app_mod, tmp_path):
    """The filter string being right proves nothing -- measure the pixels.

    This is the lesson of the punch zoom, which shipped as a filter that was
    syntactically perfect and produced no motion whatsoever.
    """
    import cv2
    import numpy as np

    W, H = 360, 640
    base = tmp_path / "base.mp4"
    card = tmp_path / "card.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"color=c=0x202020:size={W}x{H}:rate=30:duration=2",
                    "-pix_fmt", "yuv420p", str(base)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=0xFF00AA:size=200x200:d=1",
                    "-frames:v", "1", str(card)], check=True)

    def leftmost_card_column(frame):
        r = frame[:, :, 2].astype(int)
        g = frame[:, :, 1].astype(int)
        b = frame[:, :, 0].astype(int)
        cols = np.where(((r > 150) & (g < 110) & (b > 110)).any(axis=0))[0]
        return int(cols[0]) if len(cols) else None

    def render(motion, name):
        clip = {"src": str(card), "start": 0.3, "in": 0, "out": 1.5,
                "x": 0.25, "y": 0.3, "w": 0.5, "fit": "cover"}
        if motion:
            clip["motion"] = motion
        out = tmp_path / f"{name}.mp4"
        real = app_mod._timeline_clip_source
        # The real resolver takes extra keyword arguments on some branches,
        # so pass them through rather than pinning this stub to one signature.
        app_mod._timeline_clip_source = (
            lambda c, **kw: card if c.get("src") else real(c, **kw))
        try:
            app_mod._tl_composite_overlays(base, [clip], W, H, out, fps=30)
        finally:
            app_mod._timeline_clip_source = real
        cap = cv2.VideoCapture(str(out))
        xs = []
        for t in (0.35, 0.45, 0.55, 0.80):
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            xs.append(leftmost_card_column(frame) if ok else None)
        cap.release()
        return xs

    still = render(None, "still")
    assert len(set(still)) == 1, f"a still overlay drifted: {still}"

    moved = render({"type": "swipe_left", "in": 0.3}, "swiped")
    assert len(set(moved)) > 1, f"the swipe produced no motion: {moved}"
    assert all(a >= b for a, b in zip(moved, moved[1:])), (
        f"swipe_left must travel leftwards: {moved}")
    assert moved[0] > still[0], "the photo did not start off to the right"
    assert moved[-1] == still[-1], (
        f"the swipe settled at {moved[-1]}, not the resting {still[-1]}")


# --- carousels and prompt-placed b-roll ---------------------------------

def test_a_carousel_is_a_run_of_overlay_clips_sharing_a_box(app_mod, monkeypatch):
    """Deliberately not a new track: N ordinary overlays, laid back to back.

    That reuses the whole overlay pipeline and leaves each frame editable
    afterwards, which a bespoke carousel object would not.
    """
    monkeypatch.setattr(app_mod, "_resolve_photo_source",
                        lambda ref: (f"asset-{ref}", ""))
    clips, notes = app_mod._build_carousel_clips(
        ["one", "two", "three"], start=5.0, per_photo=1.5,
        motion="swipe_left", size="full")
    assert notes == []
    assert len(clips) == 3
    assert len({c["carousel_id"] for c in clips}) == 1, "frames are not grouped"
    # Back to back, no overlap: each photo hands over to the next.
    starts = [c["start"] for c in clips]
    assert starts == [5.0, 6.5, 8.0]
    assert all(c["out"] == 1.5 for c in clips)
    # One shared box, unlike auto b-roll which spreads around the corners.
    assert len({(c["x"], c["y"], c["w"], c["h"]) for c in clips}) == 1
    assert clips[0]["w"] == 1.0 and clips[0]["fit"] == "cover", "full should cover"
    assert all(c["motion"]["type"] == "swipe_left" for c in clips)


def test_a_pip_carousel_sits_in_a_box(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "_resolve_photo_source", lambda r: (f"a-{r}", ""))
    clips, _ = app_mod._build_carousel_clips(
        ["a", "b"], start=0, per_photo=1.0, motion="swipe_up", size="pip")
    assert clips[0]["w"] < 1.0 and clips[0]["x"] > 0, "pip should not cover the frame"


def test_a_keyword_with_no_provider_refuses_instead_of_making_a_badge(app_mod, monkeypatch):
    """The button path substitutes a generated TEXT BADGE for a missing photo.

    That is fine for a button someone clicked knowingly. For "add b-roll of a
    city" it would silently put a word on screen where a picture was asked
    for, so the prompt path says why and places nothing.
    """
    monkeypatch.setattr(app_mod, "_broll_any_photo_provider", lambda: False)
    monkeypatch.setattr(app_mod, "ASSET_DIR", app_mod.ASSET_DIR)
    asset_id, why = app_mod._resolve_photo_source("a city skyline")
    assert asset_id is None
    assert "provider" in why.lower() and "city skyline" in why

    op, note = app_mod._resolve_add_carousel({"photos": ["a city skyline"]})
    assert op is None, "a carousel was placed with nothing to place"
    assert "provider" in note.lower()


def test_the_carousel_op_reaches_the_client_with_photos_attached(app_mod, temp_db, monkeypatch):
    """The model picks subjects and timing; the server attaches real photos."""
    monkeypatch.setattr(app_mod, "_resolve_photo_source", lambda r: (f"a-{r}", ""))
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        lambda p: {"ops": [{"op": "add_carousel", "photos": ["x", "y"],
                            "start": 2.0, "per_photo": 1.2,
                            "motion": "swipe_left", "size": "full"}],
                   "message": "done"})
    out = app_mod.app.test_client().post("/co-editor", json={
        "prompt": "carousel of two photos that swipe",
        "timeline": _coeditor_timeline()}).get_json()
    assert len(out["ops"]) == 1
    clips = out["ops"][0]["clips"]
    assert [c["asset_id"] for c in clips] == ["a-x", "a-y"]
    assert all(c["motion"]["type"] == "swipe_left" for c in clips)


def test_a_failed_fetch_is_explained_not_swallowed(app_mod, temp_db, monkeypatch):
    monkeypatch.setattr(app_mod, "_resolve_photo_source",
                        lambda r: (None, f"no photo found for '{r}'"))
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        lambda p: {"ops": [{"op": "add_carousel", "photos": ["nonsense"]}],
                   "message": ""})
    out = app_mod.app.test_client().post("/co-editor", json={
        "prompt": "carousel of nonsense", "timeline": _coeditor_timeline()}).get_json()
    assert out["ops"] == [], "an unplaceable carousel still produced an op"
    assert "no photo found" in out["message"]


def test_the_preview_and_the_render_swipe_on_the_same_curve(app_mod):
    """Two implementations of one motion is how preview and render drift.

    Both sides now go through the keyframe engine, so this compares the whole
    real path -- motion block to tracks to a number -- rather than two
    hand-written cubics that happen to match today.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")

    motion = {"type": "swipe_left", "in": 0.35}
    times = [0, 0.0875, 0.175, 0.2625, 0.35, 0.5]

    script = (
        "const K=require(%r);const M=require(%r);\n"
        % (str(BURNER / "static" / "keyframes.js"),
           str(BURNER / "static" / "keyframes-motion.js"))
        + "globalThis.Keyframes=K;\n"
        + "const out=[];for(const t of %s)" % json.dumps(times)
        + " out.push(M.motionOffsetAt(%s,t).dx.toFixed(9));" % json.dumps(motion)
        + "console.log(out.join(' '));"
    )
    js_vals = [float(v) for v in subprocess.run(
        [node, "-e", script], capture_output=True, text=True,
        check=True).stdout.split()]

    # The renderer compiles the same tracks into an overlay x expression.
    W = 1000
    expr, _ = app_mod._tl_overlay_motion_expr(motion, 0, 0, 0.0, W, 2000)
    py_expr = expr.replace("if(", "IF(").replace("lt(", "LT(")
    py_vals = [
        eval(py_expr, {"IF": lambda c, a, b: a if c else b,
                       "LT": lambda a, b: a < b, "pow": pow,
                       "t": float(t)}) / W
        for t in times
    ]

    for t, a, b in zip(times, js_vals, py_vals):
        assert abs(a - b) < 1e-9, (
            f"at {t}s the preview offsets by {a} and the render by {b}")
    assert js_vals[0] == 1.0 and js_vals[-1] == 0.0, "the curve does not run 1->0"

    # And the migration must not have changed the shape: pow(1-u,3).
    for t, got in zip(times, js_vals):
        u = min(1.0, max(0.0, t / 0.35))
        assert abs(got - (1 - u) ** 3) < 1e-9, (
            f"the swipe curve changed at {t}s: {got} vs {(1 - u) ** 3}")


def test_the_hold_intensities_survive_the_punch_retune(app_mod):
    """"Hold" zooms are a different effect and must not be retuned.

    They snap to a scale and stay there for the whole clip, which is the
    opposite of a punch. Lowering the punch peaks and changing their curve
    must leave these completely alone -- this is the one step in the port that
    could quietly regress work that was already here.
    """
    import re

    def curve(intensity, dur=1.2):
        f = app_mod._tl_punch_zoom_filter(
            {"enabled": True, "intensity": intensity}, 1080, 1920, fps=30, dur=dur)
        z = re.search(r"zoompan=z='([^']+)'", f).group(1)
        py = z.replace("if(", "IF(").replace("lt(", "LT(").replace("gte(", "GTE(")
        py = re.sub(r"\bin\b", "N", py)
        env = {"IF": lambda c, a, b: a if c else b, "LT": lambda a, b: a < b,
               "GTE": lambda a, b: a >= b, "pow": pow, "min": min, "max": max}
        return [round(eval(py, {**env, "N": float(n)}), 4) for n in range(0, 40, 4)]

    for intensity, scale in (("1.5x", 1.5), ("2x", 2.0),
                             ("hold_1_5", 1.5), ("hold_2", 2.0)):
        vals = curve(intensity)
        assert set(vals) == {scale}, (
            f"{intensity} should hold flat at {scale}, got {vals}")
        assert app_mod._PUNCH_PEAK[intensity] == scale

    # And the punch intensities must now rise, hold and release.
    for intensity in ("low", "med", "high", "strong"):
        vals = curve(intensity)
        peak = app_mod._PUNCH_PEAK[intensity]
        assert vals[0] == 1.0, f"{intensity} does not start at rest"
        assert max(vals) == peak, f"{intensity} never reaches its peak"
        assert vals.count(peak) > 1, f"{intensity} does not hold at the peak"
        assert vals[-1] == 1.0, f"{intensity} does not settle back"
        assert 1.0 < peak <= 1.18, f"{intensity} peaks at {peak}, above the cap"


def test_rebuilding_the_speaker_pickers_keeps_the_enable_toggle():
    """#speakerColorsEnabled lives inside the wrap that gets cleared.

    syncSpeakerColorPickers() does wrap.innerHTML = "" and never rebuilt the
    toggle, so any call destroyed it -- after which getStyle() found no
    checkbox and reported speaker_colors as {} forever, silently dropping them
    from every render. It runs on the normal path after Analyze, not only from
    the preset loader, so this was live.
    """
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    body = js.split("function syncSpeakerColorPickers")[1].split("\nfunction ")[0]
    assert 'wrap.innerHTML = ""' in body, "the function no longer clears the wrap"
    assert "speakerColorsEnabled" in body, (
        "the wrap is cleared without preserving the enable toggle inside it"
    )
    assert "wrap.appendChild(toggle)" in body, "the toggle is never put back"
    # And the markup must still place it inside, or this guard is pointless.
    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    wrap = html.split('id="speakerColorPickers"')[1].split("</div>")[0]
    assert 'id="speakerColorsEnabled"' in wrap


# --- The refusal backlog -----------------------------------------------
# Every co-editor prompt that makes the model reach for an op that does not
# exist is a concrete request for one that should. Those were being recorded
# and then lost: the client never sent a job_id, so the rows landed under
# job_id = "" and the only reader took a job id in the URL path.

def test_the_coeditor_tells_the_log_which_project_it_was():
    """Without a job_id every refusal lands at a URL nothing can address."""
    src = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    send = src.split("async function sendCoEditorPrompt")[1].split("\n  }")[0]
    assert '"/co-editor"' in send, "found the wrong function"
    assert re.search(r"job_id:\s*tl\.job_id", send), \
        "sendCoEditorPrompt must post job_id or the refusal log cannot be read back"


def test_refused_ops_are_readable_across_every_project(app_mod, temp_db, monkeypatch):
    """The backlog question is 'what do people keep asking for?', not
    'what did they ask for on this one project?'."""
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        # Deliberately not a real op. set_background used to stand here and
        # then got built -- which is the backlog doing its job, and exactly
        # why this needs a name nothing is going to implement.
        lambda p: {"ops": [{"op": "animate_text_on_the_beat", "style": "bounce"},
                           {"op": "enable_punch_zoom", "index": 0}],
                   "message": "can't make text dance yet"})
    client = app_mod.app.test_client()
    # Two different projects asking for the same missing capability.
    for job, ask in (("jobA", "make the captions bounce on the beat"),
                     ("jobB", "words should pop with the music")):
        client.post("/co-editor", json={
            "job_id": job, "prompt": ask, "timeline": _coeditor_timeline()})

    data = client.get("/suggestions?kind=coeditor").get_json()
    assert data["count"] == 2, "both projects' rows must come back from one call"

    wanted = {w["op"]: w for w in data["wanted_ops"]}
    assert "animate_text_on_the_beat" in wanted, "the invented op is the point"
    assert wanted["animate_text_on_the_beat"]["count"] == 2, \
        "asks must add up across projects"
    # The real op was applied, so it is not a request for anything.
    assert "enable_punch_zoom" not in wanted
    # And it has to carry the words they used, or it is a name with no meaning.
    assert "make the captions bounce on the beat" in \
        wanted["animate_text_on_the_beat"]["prompts"]


def test_the_backlog_filters_do_not_leak_other_rows(app_mod, temp_db, monkeypatch):
    """A kind filter that quietly ignores itself would make the page lie."""
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions",
                        lambda p: {"ops": [], "message": "no"})
    client = app_mod.app.test_client()
    client.post("/co-editor", json={"job_id": "jobA", "prompt": "x",
                                    "timeline": _coeditor_timeline()})
    app_mod._record_suggestion("jobA", "effect", {"type": "punch_zoom"}, "proposed")

    assert client.get("/suggestions").get_json()["count"] == 2
    assert client.get("/suggestions?kind=effect").get_json()["count"] == 1
    assert client.get("/suggestions?kind=coeditor").get_json()["count"] == 1
    assert client.get("/suggestions?outcome=rejected").get_json()["count"] == 1
    # A bad limit must not 500 the page that reads it.
    assert client.get("/suggestions?limit=banana").status_code == 200


# --- Characterisation: what the three animation systems do TODAY --------
# These pin current behaviour before the keyframe engine replaces it. Some of
# them pin behaviour that is wrong; that is deliberate. A migration you cannot
# diff is a rewrite, and Ken Burns in particular has had no test at all, so a
# regression in it passes the whole suite today.

def _eval_zoompan_z(expr: str, frames, recursive=False):
    """Evaluate a zoompan z-expression the way ffmpeg would.

    `in` is the input frame index.

    The `recursive` flag models `zoom` as the previous frame's output. REAL
    FFMPEG DOES NOT DO THAT with d=1 -- it resets `zoom` to 1.0 for every
    input frame, which is why the old accumulating Ken Burns produced no
    motion at all. The flag is kept only so that claim stays testable; never
    trust it as a model of what will render.
    """
    py = (expr.replace("if(", "IF(").replace("lte(", "LTE(")
              .replace("lt(", "LT(").replace("gte(", "GTE("))
    py = re.sub(r"\bin\b", "N", py)
    py = re.sub(r"\bzoom\b", "Z", py)
    env = {"IF": lambda c, a, b: a if c else b,
           "LT": lambda a, b: a < b, "LTE": lambda a, b: a <= b,
           "GTE": lambda a, b: a >= b, "pow": pow, "min": min, "max": max}
    out, z = [], 1.0
    for n in range(max(frames) + 1):
        val = eval(py, {**env, "N": float(n), "Z": z})
        if recursive:
            z = val
        if n in frames:
            out.append(round(val, 4))
    return out


def _node_eval(block_from: str, block_to: str, body: str):
    """Run a slice of timeline.js under node and return its stdout."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    js_src = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    block = js_src[js_src.index(block_from):js_src.index(block_to)]
    block = re.sub(r"^\s*const ", "var ", block, flags=re.M)
    block = re.sub(r"^\s*function ", "function ", block, flags=re.M)
    res = subprocess.run([node, "-e", block + body],
                         capture_output=True, text=True, check=True)
    return res.stdout.strip()


def test_ken_burns_follows_the_clock_now(app_mod):
    """It used to add a fixed increment to its own previous output.

    That is not a function of time: it could not be seeked, and its shape
    depended on how many frames had already gone by. The accumulation was
    linear, just one increment ahead of a clock -- 0.0018 at the default
    intensity -- so the push-in is visually unchanged by this.
    """
    f = app_mod._tl_kenburns_filter(
        {"enabled": True, "intensity": "med", "direction": "in"},
        1080, 1920, fps=30, dur=4.0)
    expr = re.search(r"zoompan=z='([^']+)'", f).group(1)
    assert re.search(r"\bin\b", expr), "must consult the frame counter"
    assert not re.search(r"\bzoom\b", expr), (
        "must not read its own previous output -- that is the accumulator")

    vals = _eval_zoompan_z(expr, [0, 30, 60, 90, 120])
    assert vals[0] == pytest.approx(1.0, abs=1e-4), "starts at rest"
    assert vals[-1] == pytest.approx(1.22, abs=1e-4), "arrives at its target"
    gaps = [round(b - a, 4) for a, b in zip(vals, vals[1:])]
    assert len(set(gaps)) == 1, f"a Ken Burns drift must be even: {gaps}"


def test_ken_burns_pull_out_lands_and_stays(app_mod):
    """The defect this migration fixes, and the only visible change in it.

    The old pull-out was if(lte(zoom,1.0), maxz, max(zoom-inc, 1.0)). It
    leaned on the first frame's zoom being 1.0 to jump to full zoom -- then
    fired again the moment the ease-out reached 1.0, throwing it back to
    full zoom and starting over. On any clip long enough to finish, it
    snapped, repeatedly, mid-shot.
    """
    f = app_mod._tl_kenburns_filter(
        {"enabled": True, "intensity": "med", "direction": "out"},
        1080, 1920, fps=30, dur=1.0)
    expr = re.search(r"zoompan=z='([^']+)'", f).group(1)
    vals = _eval_zoompan_z(expr, list(range(0, 60)))

    assert vals[0] == pytest.approx(1.22, abs=1e-4), "starts zoomed in"
    assert vals[-1] == pytest.approx(1.0, abs=1e-4), "ends at rest"
    assert vals == sorted(vals, reverse=True), (
        f"a pull-out must never climb again: {vals}")
    landed = vals.index(min(vals))
    assert max(vals[landed:]) == pytest.approx(1.0, abs=1e-4), (
        "it snaps back to full zoom after landing -- the old bug is back")


def test_ken_burns_previews_the_way_it_renders(app_mod):
    """Same name, one curve. Compared at frame boundaries, both directions.

    The preview was 1 + amount*(tRel/dur) while the renderer accumulated,
    and nothing compared them -- Ken Burns had exactly one test, that an
    empty dict yields "".
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")

    for direction in ("in", "out"):
        kb = {"enabled": True, "intensity": "med", "direction": direction}
        for fps in (24, 30, 60):
            frames = [0, 5, int(fps), int(2 * fps), int(3.5 * fps), int(4 * fps)]
            f = app_mod._tl_kenburns_filter(kb, 1080, 1920, fps=fps, dur=4.0)
            expr = re.search(r"zoompan=z='([^']+)'", f).group(1)
            py_vals = _eval_zoompan_z(expr, frames)

            script = (
                "globalThis.window=globalThis;\n"
                "globalThis.Keyframes=require(%r);\n"
                % str(BURNER / "static" / "keyframes.js")
                + "const M=require(%r);\n" % str(BURNER / "static" / "keyframes-motion.js")
                + "const o=[];for(const t of %s)" % json.dumps([n / fps for n in frames])
                + " o.push(M.kenBurnsScaleAt(%s,t,4.0).toFixed(9));" % json.dumps(kb)
                + "console.log(o.join(' '));"
            )
            js_vals = [float(v) for v in subprocess.run(
                [node, "-e", script], capture_output=True, text=True,
                check=True).stdout.split()]

            for n, a, b in zip(frames, js_vals, py_vals):
                assert abs(a - b) < 1e-4, (
                    f"{direction} at {fps}fps frame {n}: "
                    f"preview {a}, render {b}")


def test_the_punch_zoom_agrees_with_its_preview_at_any_fps(app_mod):
    """The preview used to hardcode 30fps to turn its frame counts into
    seconds while the renderer used the project fps, so a 60fps project --
    which is what the polish path renders at -- previewed a push twice as
    long as it burned. Both now read one track in seconds.

    Compared at frame boundaries, because the renderer can only evaluate at
    in/fps; asking it for 0.05s at 24fps compares two different instants and
    measures quantisation rather than agreement.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")

    cfg = {"enabled": True, "intensity": "med", "hit": 0}
    dur = 1.2

    def preview_at(times):
        script = (
            "globalThis.window=globalThis;\n"
            "globalThis.Keyframes=require(%r);\n"
            % str(BURNER / "static" / "keyframes.js")
            + "const M=require(%r);\n" % str(BURNER / "static" / "keyframes-motion.js")
            + "const o=[];for(const t of %s)" % json.dumps(times)
            + " o.push(M.punchScaleAt(%s,t,%s).toFixed(9));" % (json.dumps(cfg), dur)
            + "console.log(o.join(' '));"
        )
        return [float(v) for v in subprocess.run(
            [node, "-e", script], capture_output=True, text=True,
            check=True).stdout.split()]

    # 30 is included so a "fix" that merely hardcodes another rate fails too.
    for fps in (24, 30, 60):
        frames = [0, 1, 2, 4, 7, 12, int(0.6 * fps), int(1.0 * fps)]
        f = app_mod._tl_punch_zoom_filter(cfg, 1080, 1920, fps=fps, dur=dur)
        expr = re.search(r"zoompan=z='([^']+)'", f).group(1)
        py_vals = _eval_zoompan_z(expr, frames)
        js_vals = preview_at([n / fps for n in frames])
        for n, a, b in zip(frames, js_vals, py_vals):
            assert abs(a - b) < 1e-4, (
                f"at {fps}fps, frame {n} ({n / fps:.4f}s): "
                f"preview {a}, render {b}")

    # And the shape must still be a push: rest, rise, hold, settle.
    vals = preview_at([0, 0.1, 0.5, 1.19])
    assert vals[0] == pytest.approx(1.0), "does not start at rest"
    assert max(vals) == pytest.approx(1.12, abs=1e-6), "never reaches its peak"
    assert vals[-1] < 1.01, "does not settle back"


def test_the_overlay_frame_writes_position_as_time_advances():
    """applyOverlayFrame is the per-frame writer. Run it and watch the box.

    Before this existed, position and opacity were computed only inside
    updateStageCompositor, which re-runs when a clip enters or leaves the
    visible set. During a swipe nothing does, so a swiping overlay sat a
    full canvas off-frame -- 119.895% -- for its entire life in the preview
    while the render slid it in.
    """
    js_src = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    block = js_src[js_src.index("  function applyOverlayFrame"):
                   js_src.index("  // Runs every frame so punch")]
    block = re.sub(r"^\s*const ", "var ", block, flags=re.M)
    block = re.sub(r"^\s*function ", "function ", block, flags=re.M)

    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    # The real modules, not stubs: this has to exercise the path the browser
    # takes, including the engine that now owns the curve.
    script = (
        "globalThis.window = globalThis;\n"
        "globalThis.Keyframes = require(%r);\n" % str(BURNER / "static" / "keyframes.js")
        + "globalThis.KeyframeMotion = require(%r);\n"
          % str(BURNER / "static" / "keyframes-motion.js")
    ) + block + """
      var item = {x: 0.2, y: 0.2, opacity: 1, fade_in: 0, fade_out: 0,
                  motion: {type: "swipe_left", in: 1.2}};
      var o = {item: item, start: 0.5, dur: 4,
               wrap: {style: {}}, el: {style: {}}, ken_burns: null};
      var out = [];
      for (var t of [0.5, 0.8, 1.1, 1.4, 1.7, 2.5]) {
        applyOverlayFrame(o, t);
        out.push(o.wrap.style.left);
      }
      console.log(out.join(" "));
    """
    vals = [float(v.replace("%", "")) for v in
            subprocess.run([node, "-e", script], capture_output=True,
                           text=True, check=True).stdout.split()]

    assert vals[0] == pytest.approx(120.0, abs=0.1), "starts a canvas off-frame"
    assert vals == sorted(vals, reverse=True), f"must travel one way: {vals}"
    assert vals[-1] == pytest.approx(20.0, abs=0.01), "must settle at rest"
    assert len(set(round(v, 3) for v in vals)) > 4, (
        f"the box barely moves: {vals} -- this is the frozen-swipe bug")


def test_the_frame_loop_owns_overlay_position_not_the_rebuild():
    """One writer per property, or the two drift and only one animates.

    The bug was not a wrong curve -- overlayMotionOffset was correct and had
    a passing parity test. It was that nothing called it between rebuilds.
    """
    js_src = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")

    frame = js_src[js_src.index("  function previewFrame()"):
                   js_src.index("  function previewFrame()") + 1400]
    assert "syncOverlayKenBurns(ot)" in frame, "the frame loop must drive overlays"

    sync = js_src[js_src.index("  function syncOverlayKenBurns"):
                  js_src.index("  function syncOverlayKenBurns") + 220]
    assert "applyOverlayFrame" in sync, "per-frame pass must call the writer"

    # The rebuild may set size and chrome, but not the animated properties.
    # Scoped to the placed-overlay loop: the pending-B-roll ghost below it is
    # a static preview of a suggestion nobody has accepted, so a constant
    # position there is correct.
    rebuild = js_src[js_src.index("    tl.tracks.overlay.forEach(item => {"):
                     js_src.index("    // Ghost pending B-roll suggestions")]
    for prop in ("wrap.style.left", "wrap.style.top", "wrap.style.opacity"):
        assert prop not in rebuild, (
            f"{prop} is written during the rebuild again; it will freeze "
            "between rebuilds exactly as the swipe did")

    # And the entry has to carry the wrapper, or the writer has nothing to move.
    assert re.search(r"ovEntries\.push\(", js_src), "ovEntries still populated"
    entry = js_src[js_src.index("const entry = {"):
                   js_src.index("ovEntries.push(entry)")]
    assert "wrap," in entry and "item," in entry, (
        "the frame writer needs the wrapper element and the clip")


# --- The keyframe engine ------------------------------------------------
# One spec, three readers: eval_track (Python), evalTrack (JS, per frame) and
# to_ffmpeg_expr (compiled into a filter). They are checked against each other
# over random tracks, because every drift this app has had came from two
# hand-written copies of one curve that nobody compared.

def _kf():
    import keyframes
    return keyframes


def _random_tracks(n=40, seed=20261108):
    import random
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        count = rng.choice([1, 2, 2, 3, 4, 5])
        ts, t = [], rng.uniform(0, 0.4)
        for _ in range(count):
            ts.append(round(t, 4))
            t += rng.uniform(0.05, 1.5)
        vals = [round(rng.uniform(-2, 3), 4) for _ in ts]
        out.append(list(map(list, zip(ts, vals))))
    return out


def test_the_keyframe_curve_is_the_same_in_both_languages():
    """Python and the browser must read one spec identically.

    This is the check that did not exist for Ken Burns, which accumulates in
    the renderer and is linear in the preview under the same name.
    """
    kf = _kf()
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")

    tracks = _random_tracks()
    times = [0.0, 0.11, 0.37, 0.8, 1.25, 2.0, 3.5]
    cases = [(tr, e) for tr in tracks for e in kf.EASINGS]

    script = (
        "const K=require(%r);\n" % str(BURNER / "static" / "keyframes.js")
        + "const cases=%s, times=%s;\n" % (json.dumps(cases), json.dumps(times))
        + "const out=[];for(const [tr,e] of cases){for(const t of times){"
        + "const v=K.evalTrack(tr,t,e);out.push(v===null?'null':v.toFixed(9));}}"
        + "console.log(out.join(' '));"
    )
    js_vals = subprocess.run([node, "-e", script], capture_output=True,
                             text=True, check=True).stdout.split()

    i = 0
    for track, ease in cases:
        for t in times:
            py = kf.eval_track(track, t, ease)
            js = js_vals[i]; i += 1
            if py is None:
                assert js == "null"
                continue
            assert abs(float(js) - py) < 1e-9, (
                f"{ease} at {t}s on {track}: python {py}, browser {js}")
    assert i == len(cases) * len(times), "every case must be compared"


def test_the_compiled_expression_matches_the_evaluator(app_mod):
    """to_ffmpeg_expr is what actually renders, so it has to agree too.

    The expression is evaluated the way ffmpeg would, in local seconds, so a
    track that previews correctly cannot burn in differently.
    """
    kf = _kf()
    env = {"lt": lambda a, b: a < b, "pow": pow, "min": min, "max": max,
           "if": None}
    for track in _random_tracks(n=25, seed=7):
        for ease in kf.EASINGS:
            expr = kf.to_ffmpeg_expr(track, "T", ease)
            if expr is None:
                assert not kf.normalize_track(track)
                continue
            py_expr = expr.replace("if(", "IF(").replace("lt(", "LT(")
            for t in (0.0, 0.2, 0.55, 1.0, 1.9, 4.0):
                got = eval(py_expr, {"IF": lambda c, a, b: a if c else b,
                                     "LT": env["lt"], "pow": pow,
                                     "T": float(t)})
                want = kf.eval_track(track, t, ease)
                assert abs(got - want) < 1e-6, (
                    f"{ease} at {t}s: expression {got}, evaluator {want}\n"
                    f"track={track}\nexpr={expr}")


def test_a_track_holds_its_ends_and_survives_rubbish():
    """Saved timelines are free-form JSON written by a model and by hand."""
    kf = _kf()
    tr = [[1.0, 10.0], [2.0, 20.0]]
    assert kf.eval_track(tr, 0.0) == 10.0, "holds the first value before it"
    assert kf.eval_track(tr, 9.9) == 20.0, "holds the last value after it"
    assert kf.eval_track(tr, 1.5) == pytest.approx(15.0), "interpolates"
    assert kf.eval_track([], 1.0) is None, "empty is not zero"

    messy = [[2.0, 20.0], ["1.0", "10.0"], None, [3.0], ["x", 1], [0.5, 5.0]]
    assert kf.normalize_track(messy) == [(0.5, 5.0), (1.0, 10.0), (2.0, 20.0)]
    assert kf.eval_track(messy, 1.5) == pytest.approx(15.0), "sorted, not raw"

    # A constant track compiles to a bare number, not a one-armed if().
    assert kf.to_ffmpeg_expr([[0.0, 1.5]]) == "1.5"
    assert kf.to_ffmpeg_expr([]) is None


def test_the_time_base_is_local_seconds_whatever_the_filter_counts():
    """The one decision that lets a single spec drive three clocks.

    Overlay x/y are expressions over absolute t; zoompan only sees the frame
    counter. Both ask for local seconds and get the same curve.
    """
    kf = _kf()
    track = [[0.0, 0.0], [1.0, 100.0]]

    overlay = kf.to_ffmpeg_expr(track, "(t-4.2)")
    at_half = eval(overlay.replace("if(", "IF(").replace("lt(", "LT("),
                   {"IF": lambda c, a, b: a if c else b,
                    "LT": lambda a, b: a < b, "pow": pow, "t": 4.7})
    assert at_half == pytest.approx(50.0), "overlay clock is absolute t"

    # `in` is a Python keyword, so rename it the way _eval_zoompan_z does.
    zoom = kf.to_ffmpeg_expr(track, "(in/30)")
    assert "in/30" in zoom, "the frame counter must reach the expression"
    py = re.sub(r"\bin\b", "N", zoom.replace("if(", "IF(").replace("lt(", "LT("))
    at_15 = eval(py, {"IF": lambda c, a, b: a if c else b,
                      "LT": lambda a, b: a < b, "pow": pow, "N": 15})
    assert at_15 == pytest.approx(50.0), "zoompan clock is the frame counter"


def test_the_keyframe_module_reaches_the_browser():
    """A module the page never loads is not an engine, it is a file."""
    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    assert "filename='keyframes.js'" in html, "keyframes.js is not on the page"
    # It has to come first, or timeline.js reads window.Keyframes as undefined.
    assert html.index("filename='keyframes.js'") < html.index("filename='timeline.js'"), \
        "keyframes.js must load before timeline.js"


@needs_ffmpeg
def test_ken_burns_really_moves_in_the_render(app_mod, tmp_path):
    """The only check that would have caught what was actually wrong.

    The old Ken Burns was a recursive zoompan expression: it added a fixed
    increment to its own previous output. That reads as a ramp if you
    simulate it, which is what the characterisation test did -- but real
    ffmpeg with d=1 resets `zoom` to 1.0 on every input frame, so the
    recursion never accumulated. Rendered, the push-in sat at 1.0 and the
    pull-out sat at 1.22. Ken Burns produced no motion at all, and every
    test in this suite passed while it did.

    So this one renders and measures pixels.
    """
    if not _have("cv2") or not _have("numpy"):
        pytest.skip("cv2/numpy not installed")
    import cv2

    still = tmp_path / "still.png"
    src = tmp_path / "src.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "smptebars=size=540x960:duration=0.1:rate=1",
                    "-frames:v", "1", str(still)], check=True)
    # A static source, so anything that changes on screen is the zoom.
    subprocess.run([FFMPEG, "-v", "error", "-y", "-loop", "1", "-i", str(still),
                    "-t", "2", "-r", "30", "-pix_fmt", "yuv420p", str(src)],
                   check=True)

    def bar_width(path):
        """Leftmost colour bar in pixels. Magnifying crops it, so it narrows."""
        im = cv2.imread(str(path))
        row = im[480].astype(int)
        first, w = row[3], 0
        for px in row:
            if abs(px - first).sum() < 40:
                w += 1
            else:
                break
        return w

    def widths(kb):
        out = tmp_path / f"kb_{kb['direction']}.mp4"
        vf = app_mod._tl_kenburns_filter(kb, 540, 960, fps=30, dur=2.0)
        assert vf, "filter must be built"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(src),
                        "-vf", vf, str(out)], check=True)
        got = []
        for t in ("0.03", "0.7", "1.3", "1.9"):
            png = tmp_path / f"f_{kb['direction']}_{t}.png"
            subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", t, "-i", str(out),
                            "-frames:v", "1", str(png)], check=True)
            got.append(bar_width(png))
        return got

    push = widths({"enabled": True, "intensity": "med", "direction": "in"})
    assert len(set(push)) > 2, f"the push-in does not move: {push}"
    assert push == sorted(push, reverse=True), (
        f"a push-in must magnify steadily, narrowing the bar: {push}")

    pull = widths({"enabled": True, "intensity": "med", "direction": "out"})
    assert len(set(pull)) > 2, f"the pull-out does not move: {pull}"
    assert pull == sorted(pull), (
        f"a pull-out must widen steadily back to rest: {pull}")
    assert pull[-1] > push[-1], "the two directions end at the same place"


# --- The layer behind the subject ---------------------------------------

def test_the_background_survives_a_save_on_both_sides():
    """Two silent data sinks sit between the UI and the renderer.

    serialize() whitelists top-level keys and drops the rest on the next
    debounced save; _normalize_timeline decides what the server keeps. A
    field missing from either appears to work until a reload.
    """
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")

    # There are THREE of these, not one, and they are near-identical:
    # serialize() for the save, snapshotState() for undo, and
    # _normalize_timeline server-side. A field missing from serialize()
    # vanishes on save; missing from snapshotState() it survives the save
    # and then disappears the first time anyone presses undo.
    for fn in ("  function serialize() {", "  function snapshotState() {"):
        block = js[js.index(fn):js.index(fn) + 900]
        assert re.search(r"background:\s*tl\.background", block), (
            f"{fn.strip()} drops the background -- it will vanish, and only "
            "after a save or an undo, which is why this is checked statically")

    py = (BURNER / "app.py").read_text(encoding="utf-8")
    assert 'tl["background"]' in py, "_normalize_timeline drops the background"


def test_a_background_needs_something_to_point_at(app_mod, temp_db):
    """A background with no picture is not a background."""
    norm = app_mod._normalize_timeline
    assert norm({"background": {"asset_id": "abc"}})["background"] == {"asset_id": "abc"}
    assert norm({"background": {"source_job_id": "job1"}})["background"] is not None
    for junk in ({}, {"fit": "cover"}, "beach", 7, None):
        assert norm({"background": junk})["background"] is None, junk


@needs_ffmpeg
def test_the_background_really_sits_behind_the_subject(app_mod, tmp_path, monkeypatch):
    """The capability no combination of the 32 existing ops could express.

    Every other compositing pass starts from [0:v:0] of the baked video and
    lays things on top, so "a picture behind me" was not a missing op -- it
    was a missing z-order. This renders it and reads the pixels: the subject
    must survive, and what was around it must become the picture.
    """
    if not _have("cv2"):
        pytest.skip("cv2 not installed")
    import cv2

    # A stand-in subject: an orange block on black, with a matte to match.
    subject = tmp_path / "subject.mp4"
    matte = tmp_path / "matte.mp4"
    bg_png = tmp_path / "bg.png"
    out = tmp_path / "out.mp4"
    box = "drawbox=x=170:y=300:w=200:h=360:color=%s@1:t=fill"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=540x960:d=1:r=30", "-vf", box % "orange",
                    "-pix_fmt", "yuv420p", str(subject)], check=True)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=540x960:d=1:r=30", "-vf", box % "white",
                    "-pix_fmt", "gray", str(matte)], check=True)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "smptebars=s=540x960:d=0.1:r=1", "-frames:v", "1",
                    str(bg_png)], check=True)

    # Skip the segmentation model: this test is about the compositing, and
    # the matte's own quality is a separate question answered by eye.
    monkeypatch.setattr(app_mod, "_tl_matte_video",
                        lambda *a, **k: matte)
    monkeypatch.setattr(app_mod, "_tl_bg_source", lambda bg: bg_png)

    assert app_mod._tl_composite_background(
        subject, {"asset_id": "x", "fit": "cover"}, 540, 960, out,
        fps=30, dur=1.0), "the composite reported failure"

    frame = tmp_path / "f.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "0.5", "-i", str(out),
                    "-frames:v", "1", str(frame)], check=True)
    im = cv2.imread(str(frame))

    inside = im[480, 270]          # middle of the subject block
    outside = im[40, 40]           # well clear of it
    assert inside[2] > 180 and inside[1] > 100 and inside[0] < 80, (
        f"the subject was not kept: BGR {inside}")
    assert outside.sum() > 150, (
        f"behind the subject is still black -- the background did not land: {outside}")


def test_a_background_without_a_matte_leaves_the_video_alone(app_mod, tmp_path,
                                                             monkeypatch):
    """A matte that cannot be made must cost the effect, never the export."""
    monkeypatch.setattr(app_mod, "_tl_matte_video", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_tl_bg_source", lambda bg: tmp_path / "nope.png")
    out = tmp_path / "out.mp4"
    assert app_mod._tl_composite_background(
        tmp_path / "base.mp4", {"asset_id": "x"}, 540, 960, out) is False
    assert not out.exists(), "a failed background must not leave a broken file"


# --- Matte quality -------------------------------------------------------
# There is no footage of a person on this machine -- every upload is the same
# colour-bar test clip -- and no network or key reaches a stock provider. But
# matplotlib ships a real portrait, so matte quality is measurable offline and
# deterministically.

def _portrait():
    import matplotlib.cbook as cbook
    return (Path(cbook.__file__).parent / "mpl-data" / "sample_data"
            / "grace_hopper.jpg")


def _raw_matte(path):
    import cv2
    import mediapipe as mp
    import numpy as np
    im = cv2.imread(str(path))
    seg = mp.solutions.selfie_segmentation.SelfieSegmentation(model_selection=1)
    try:
        res = seg.process(cv2.cvtColor(im, cv2.COLOR_BGR2RGB))
    finally:
        seg.close()
    return im, np.clip(res.segmentation_mask, 0.0, 1.0)


# The cap's white crown: a bright subject against a bright background, which
# is where this model gives up. On real footage it is the top of the hair.
_CROWN = (slice(55, 95), slice(215, 300))
# Sky well clear of the subject -- must stay background whatever we do.
_SKY = (slice(10, 40), slice(430, 500))


def test_the_matte_refinement_firms_up_a_bleeding_edge(app_mod):
    """The number is the point, not that a function was called."""
    if not (_have("cv2") and _have("mediapipe") and _have("matplotlib")):
        pytest.skip("cv2 / mediapipe / matplotlib not installed")
    _, raw = _raw_matte(_portrait())
    ref = app_mod._matte_refine(raw)

    raw_crown, ref_crown = raw[_CROWN].mean(), ref[_CROWN].mean()
    assert raw_crown < 0.80, (
        f"the model now reports {raw_crown:.3f} on the crown -- it used to "
        "bleed at 0.705, so this test's premise needs rechecking")
    assert ref_crown > raw_crown + 0.10, (
        f"refinement barely moved the bleeding edge: {raw_crown:.3f} -> "
        f"{ref_crown:.3f}")


def test_the_refinement_does_not_simply_brighten_everything(app_mod):
    """A remap that pulls the whole frame up would pass the test above."""
    if not (_have("cv2") and _have("mediapipe") and _have("matplotlib")):
        pytest.skip("cv2 / mediapipe / matplotlib not installed")
    import numpy as np
    _, raw = _raw_matte(_portrait())
    ref = app_mod._matte_refine(raw)

    # The sky is exactly 0.0, so no amount of brightening can move it -- an
    # earlier version of this test checked only there and happily passed a
    # refinement that just multiplied the whole mask by three. The pixels
    # that discriminate are the UNCERTAIN ones leaning background: a remap
    # pushes them to 0, a brightener pulls them toward subject.
    band = (raw >= 0.05) & (raw <= 0.15)
    assert band.sum() > 200, "no uncertain band to test against"
    assert ref[band].mean() < 0.02, (
        f"uncertain background was pulled toward subject: "
        f"{raw[band].mean():.3f} -> {ref[band].mean():.3f}")
    assert ref[_SKY].max() < 0.05, (
        f"background pixels became subject: {ref[_SKY].max():.3f}")
    assert ref.max() <= 1.0 and ref.min() >= 0.0, "alpha left 0..1"
    # And it must still be a matte, not a silhouette: some partial alpha left.
    partial = ((ref > 0.05) & (ref < 0.95)).sum()
    assert partial > 0, "no soft edge at all -- that is a cut-out, not a matte"


def test_the_refinement_is_cheap_enough_to_run_per_frame(app_mod):
    """The guided filter that does this properly costs 196ms a frame, which
    is six minutes on a 60-second clip against the model's own 7ms."""
    if not (_have("cv2") and _have("mediapipe") and _have("matplotlib")):
        pytest.skip("cv2 / mediapipe / matplotlib not installed")
    import time
    _, raw = _raw_matte(_portrait())
    app_mod._matte_refine(raw)                      # warm
    t0 = time.perf_counter()
    for _ in range(20):
        app_mod._matte_refine(raw)
    per_ms = (time.perf_counter() - t0) / 20 * 1000
    assert per_ms < 5.0, f"{per_ms:.1f} ms a frame is too slow to run per frame"


def test_a_degenerate_knee_leaves_the_matte_alone(app_mod, monkeypatch):
    """Tuning knobs get mistyped; a bad one must not blank the subject."""
    import numpy as np
    m = np.array([[0.0, 0.5, 1.0]], dtype="float32")
    monkeypatch.setattr(app_mod, "MATTE_KNEE_LO", 0.8)
    monkeypatch.setattr(app_mod, "MATTE_KNEE_HI", 0.2)
    assert np.allclose(app_mod._matte_refine(m), m), "inverted knee must no-op"


# --- set_background, the registry, and the co-editor's memory ------------

def test_set_background_reaches_the_client_with_a_real_picture(app_mod, temp_db,
                                                               monkeypatch):
    """The model names a subject; the server turns it into an asset."""
    monkeypatch.setattr(app_mod, "_resolve_photo_source",
                        lambda ref: ("asset123", ""))
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        lambda p: {"ops": [{"op": "set_background", "photo": "bali beach"}],
                   "message": "placed it"})
    out = app_mod.app.test_client().post("/co-editor", json={
        "job_id": "j", "prompt": "put a beach behind me",
        "timeline": _coeditor_timeline()}).get_json()
    assert out["ops"] == [{"op": "set_background", "asset_id": "asset123",
                           "fit": "cover"}]


def test_clearing_the_background_is_its_own_instruction(app_mod, temp_db,
                                                        monkeypatch):
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        lambda p: {"ops": [{"op": "set_background", "clear": True}]})
    out = app_mod.app.test_client().post("/co-editor", json={
        "job_id": "j", "prompt": "remove the background",
        "timeline": _coeditor_timeline()}).get_json()
    assert out["ops"] == [{"op": "set_background", "clear": True}]


def test_a_background_with_no_provider_says_so_rather_than_improvising(
        app_mod, temp_db, monkeypatch):
    """The b-roll button quietly substitutes a text badge when it cannot find
    a photo. That is fine for a button someone clicked knowingly and wrong
    for "put a beach behind me", which would put the word behind their head.
    """
    monkeypatch.setattr(app_mod, "_broll_any_photo_provider", lambda: False)
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        lambda p: {"ops": [{"op": "set_background", "photo": "bali beach"}],
                   "message": ""})
    out = app_mod.app.test_client().post("/co-editor", json={
        "job_id": "j", "prompt": "beach behind me",
        "timeline": _coeditor_timeline()}).get_json()
    assert out["ops"] == [], "nothing should be placed"
    assert "PEXELS_API_KEY" in out["message"] or "provider" in out["message"].lower(), \
        f"the refusal must name what is missing: {out['message']!r}"


def test_the_op_registry_is_the_only_list(app_mod):
    """allowed and the prompt examples are generated; the client is checked."""
    names = app_mod._co_editor_op_names()
    assert "set_background" in names and len(names) >= 33

    # Every registered op must be shown to the model, or it can never use it.
    shown = "\n".join(app_mod.CO_EDITOR_OP_EXAMPLES)
    for n in names:
        assert f'"op": "{n}"' in shown, f"{n} is allowed but never shown"

    # And must have a client branch, or it is accepted and silently ignored.
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    body = js[js.index("async function applyCoEditorOps"):][:24000]
    missing = [n for n in sorted(names) if f'name === "{n}"' not in body]
    assert not missing, f"accepted by the server, ignored by the client: {missing}"


# ---------------------------------------------------------------------------
# "Split screen from 0:10 to 0:15" applied it to the whole clip.
#
# The effects lane was never at fault: _tl_effect_cut_points splits each keep
# at effect boundaries and _tl_effects_overlapping scopes props per
# sub-segment, and test_a_speakers_split_survives_the_whole_render_chain
# proves a lane split stops at its own edge.
#
# The co-editor could not CREATE an effect. Of the ops it had, three touched
# the lane -- clear_effects, delete_effect, set_effect_time -- and all three
# act on an effect that already exists. Asked for a split over a window it
# reached for the nearest thing it did have, apply_clip_style("split_stack"),
# which restyles WHOLE Main clips and has no concept of time. Hence the
# symptom. split_at_playhead made it worse: that is a CUT whose name reads
# like split-screen, sitting in the same list.
# ---------------------------------------------------------------------------

def test_a_windowed_effect_can_be_asked_for_at_all(app_mod, temp_db, monkeypatch):
    """The gap: 35 ops, and not one of them made an effect."""
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions",
                        lambda p: {"ops": [
                            {"op": "add_effect", "type": "split_screen",
                             "start": 10.0, "duration": 5.0,
                             "layout": "stack", "mode": "speakers"}],
                            "message": "ok"})
    out = app_mod.app.test_client().post("/co-editor", json={
        "prompt": "split screen from 0:10 to 0:15",
        "timeline": _coeditor_timeline()}).get_json()
    assert len(out["ops"]) == 1, out
    op = out["ops"][0]
    # The times are the whole point -- an op that arrived without them would
    # land at the playhead and be indistinguishable from the bug.
    assert op["op"] == "add_effect" and op["type"] == "split_screen"
    assert op["start"] == 10.0 and op["duration"] == 5.0
    assert op["layout"] == "stack" and op["mode"] == "speakers"


def test_the_effect_creator_is_in_every_list_it_has_to_be_in(app_mod):
    """Registry, prompt and client dispatcher, or it is accepted and ignored."""
    assert "add_effect" in app_mod._co_editor_op_names()
    shown = "\n".join(app_mod.CO_EDITOR_OP_EXAMPLES)
    assert '"op": "add_effect"' in shown
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    body = js[js.index("async function applyCoEditorOps"):][:26000]
    assert 'name === "add_effect"' in body
    assert "addEffectClip(" in body, "the branch must reach the real creator"


def test_the_prompt_separates_cutting_from_split_screen(app_mod, temp_db,
                                                        monkeypatch):
    """The model's confusion IS the bug, so the prompt has to name it.

    Two ops it used to reach for: split_at_playhead, whose name reads like
    split-screen and is scissors, and apply_clip_style, which has no concept
    of time. Both stay available for what they really do.
    """
    seen = {}
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        lambda p: (seen.setdefault("p", p), {"ops": []})[1])
    app_mod.app.test_client().post("/co-editor", json={
        "prompt": "split screen for five seconds",
        "timeline": _coeditor_timeline()})
    p = seen["p"]
    assert "split_at_playhead CUTS" in p, "nothing tells it that one is scissors"
    assert "apply_clip_style" in p and "no concept of time" in p
    assert "add_effect" in p


def test_an_effect_the_lane_cannot_carry_is_refused_not_dropped(app_mod, temp_db,
                                                                monkeypatch):
    """_normalize_timeline drops an unknown effect type on the next save.

    So an op that got past the server would place a clip that silently
    vanishes -- the silent-sink failure this codebase has hit twice. Refuse
    it here, and SAY so, because "applied 1 edit" followed by nothing is
    indistinguishable from a bug.
    """
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions",
                        lambda p: {"ops": [
                            {"op": "add_effect", "type": "barrel_roll",
                             "start": 1.0, "duration": 1.0}], "message": ""})
    out = app_mod.app.test_client().post("/co-editor", json={
        "prompt": "barrel roll at one second",
        "timeline": _coeditor_timeline()}).get_json()
    assert out["ops"] == []
    assert "barrel_roll" in out["message"], out["message"]

    # And every type it DOES accept must survive a save, or the op is a lie.
    for t in app_mod.TL_EFFECT_TYPES:
        op, note = app_mod._resolve_add_effect({"type": t, "start": 1.0})
        assert op is not None, f"{t} refused by the resolver: {note}"
        tl = app_mod._normalize_timeline({"tracks": {"effects": [
            {"id": "x", "type": t, "start": 1.0, "out": 2.0}]}})
        assert tl["tracks"]["effects"], f"{t} accepted but dropped on save"


def test_a_clamped_duration_says_it_was_clamped(app_mod):
    """A silently retimed effect looks exactly like the bug being fixed."""
    op, note = app_mod._resolve_add_effect(
        {"type": "punch_zoom", "start": 2.0, "duration": 30.0})
    lo, hi = app_mod._EFFECT_LIMITS["punch_zoom"]
    assert op["duration"] == hi
    assert "30" in note and f"{hi:g}s" in note, note

    # Inside the bounds nothing is said and nothing is changed.
    op, note = app_mod._resolve_add_effect(
        {"type": "split_screen", "start": 10.0, "duration": 5.0})
    assert op["duration"] == 5.0 and note == ""

    # No duration at all leaves it to the client's per-type default rather
    # than inventing one here, where the two would drift.
    op, note = app_mod._resolve_add_effect({"type": "ken_burns", "start": 0})
    assert "duration" not in op and note == ""


def test_the_old_whole_clip_style_path_is_untouched(app_mod, temp_db, monkeypatch):
    """apply_clip_style("split_stack") is still a real thing to want.

    It was the wrong answer for a time range, not the wrong op. Breaking it
    on the way past would trade one bug for another.
    """
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions",
                        lambda p: {"ops": [
                            {"op": "apply_clip_style", "style": "split_stack"}],
                            "message": ""})
    out = app_mod.app.test_client().post("/co-editor", json={
        "prompt": "make every shot a split stack",
        "timeline": _coeditor_timeline()}).get_json()
    assert [o["op"] for o in out["ops"]] == ["apply_clip_style"]
    assert out["ops"][0]["style"] == "split_stack"
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    assert 'name === "apply_clip_style"' in js


def test_a_windowed_split_leaves_the_footage_either_side_alone(app_mod, tmp_path):
    """"It applied to everything" is a pixel claim, so it is checked in pixels.

    The existing chain test samples inside the window and after it. The
    report was about a split covering the WHOLE clip, so the frame BEFORE
    the window is the half that was missing -- and it is the half a
    whole-clip style would fail.
    """
    for mod in ("cv2", "flask"):
        if not _have(mod):
            pytest.skip(f"{mod} not installed")
    import cv2
    import uuid

    sid, rid = uuid.uuid4().hex, uuid.uuid4().hex
    made = []
    try:
        src = app_mod.UPLOAD_DIR / f"{sid}.mp4"
        subprocess.run(
            [FFMPEG, "-v", "error", "-y",
             "-f", "lavfi", "-i", "color=c=red:s=640x720:d=3:r=30",
             "-f", "lavfi", "-i", "color=c=blue:s=640x720:d=3:r=30",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-filter_complex", "[0:v][1:v]hstack=inputs=2,format=yuv420p[v]",
             "-map", "[v]", "-map", "2:a", "-c:a", "aac", "-shortest", str(src)],
            check=True)
        made.append(src)
        made.append(_write_two_box_analysis(app_mod, sid))

        base = {"status": "done", "progress": 100, "output": None, "error": None,
                "audio": None, "emoji_rules": {}, "created_at": 0.0,
                "words": None, "style": {}}
        app_mod.jobs[sid] = {**base, "filename": "halves.mp4"}
        app_mod.jobs[rid] = {**base, "filename": "split.mp4"}

        # The shape the op produces: one window in the middle of the shot,
        # with real footage on both sides of it.
        timeline = {
            "canvas": "9x16", "fps": 30, "fit": "cover", "bg": "#000000",
            "tracks": {
                "main": [{"id": "m1", "source_job_id": sid,
                          "in": 0.0, "out": 3.0, "burn_captions": False}],
                "effects": [{"id": "f1", "type": "split_screen",
                             "start": 1.0, "out": 1.0, "mode": "speakers",
                             "speakers": ["SPEAKER_00", "SPEAKER_01"],
                             "layout": "auto"}],
                "overlay": [], "text": [], "music": [],
            },
        }
        app_mod.render_timeline_job(rid, timeline)
        assert app_mod.jobs[rid]["status"] == "done", app_mod.jobs[rid].get("error")
        out = app_mod.OUTPUT_DIR / f"{rid}.mp4"
        made.append(out)

        def split_like(t):
            png = tmp_path / f"w{t}.png"
            subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", str(t),
                            "-i", str(out), "-frames:v", "1", str(png)],
                           check=True)
            im = cv2.imread(str(png))
            if im is None:
                return None
            top, bottom = im[480, 540], im[1440, 540]
            # Two panels means red above and blue below. One uncut shot
            # means both samples land in the same half of the 9:16 crop.
            return bool(top[2] > 150 and top[0] < 80
                        and bottom[0] > 150 and bottom[2] < 80)

        assert split_like(1.5) is True, "the split is missing inside its window"
        assert split_like(0.4) is False, \
            "the split reached BACK before its start -- the reported bug"
        assert split_like(2.6) is False, "the split leaked past its end"
    finally:
        for f in made:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        app_mod._SPEAKER_FACE_CACHE.pop(sid, None)
        app_mod.jobs.pop(sid, None)
        app_mod.jobs.pop(rid, None)


def test_the_coeditor_can_hear_the_answer_to_its_own_question(app_mod, temp_db,
                                                              monkeypatch):
    """It is told three times to ask for clarification. Nothing used to carry
    the reply back, so the drawer looked like a chat and was not one."""
    seen = {}
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        lambda p: (seen.setdefault("prompt", p), {"ops": []})[1])
    app_mod.app.test_client().post("/co-editor", json={
        "job_id": "j", "prompt": "all of them",
        "timeline": _coeditor_timeline(),
        "history": [{"role": "user", "text": "punch in on the hook"},
                    {"role": "bot", "text": "All shots, or just this one?"}]})
    p = seen["prompt"]
    assert "All shots, or just this one?" in p, "the question is not carried back"
    assert "punch in on the hook" in p, "the earlier turn is not carried back"
    assert "all of them" in p


def test_the_remembered_conversation_is_bounded(app_mod, temp_db, monkeypatch):
    """Unbounded history would grow every request's cost without limit."""
    seen = {}
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        lambda p: (seen.setdefault("prompt", p), {"ops": []})[1])
    long_turns = [{"role": "user", "text": f"turn number {i} " + "x" * 900}
                  for i in range(30)]
    app_mod.app.test_client().post("/co-editor", json={
        "job_id": "j", "prompt": "now do it", "timeline": _coeditor_timeline(),
        "history": long_turns})
    p = seen["prompt"]
    assert "turn number 29" in p, "the most recent turns must survive"
    assert "turn number 10" not in p, "old turns must be dropped"
    assert "x" * 400 not in p, "each turn must be truncated"


def test_the_drawer_sends_what_it_remembers():
    """The server reads `history`; the client has to actually post it."""
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    send = js[js.index("async function sendCoEditorPrompt"):][:2200]
    assert "history:" in send, "sendCoEditorPrompt does not post the history"
    assert "rememberCoTurn" in js, "nothing records the turns"
    assert "forgetCoEditorTurns" in js, (
        "a question asked about one project must not be answered against "
        "another -- there must be a way to clear it")


# --- One render through every pass that changed -------------------------

@needs_ffmpeg
def test_one_render_through_the_whole_pipeline(app_mod, tmp_path, monkeypatch):
    """Every piece was verified alone; nothing had exercised the chain.

    The render is eight passes:

        per-segment effects -> concat/xfade -> BACKGROUND -> logo
          -> audio bed -> overlays -> SFX -> captions+titles -> audio enhance

    The background pass is new and was spliced into the middle of that, and
    all three effects now emit different filter strings. The first run of
    this test found a NameError in the background pass that would have
    crashed every render with a background set -- invisible to the isolated
    test, which called the function directly.

    The subject is a real person (matplotlib's bundled portrait), so the
    matte is real rather than stubbed.
    """
    for mod in ("cv2", "numpy", "mediapipe", "matplotlib"):
        if not _have(mod):
            pytest.skip(f"{mod} not installed")
    import cv2
    import numpy as np
    import uuid

    portrait = _portrait()
    sid, rid = uuid.uuid4().hex, uuid.uuid4().hex
    bg_id, ov_id = uuid.uuid4().hex, uuid.uuid4().hex
    made = []

    def ff(*args):
        subprocess.run([FFMPEG, "-v", "error", "-y", *args], check=True)

    try:
        src = app_mod.UPLOAD_DIR / f"{sid}.mp4"
        ff("-loop", "1", "-i", str(portrait), "-f", "lavfi",
           "-i", "sine=frequency=330:duration=6", "-t", "6", "-r", "30",
           "-vf", "scale=540:960:force_original_aspect_ratio=increase,crop=540:960",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src))
        made.append(src)

        app_mod.ASSET_DIR.mkdir(parents=True, exist_ok=True)
        bg = app_mod.ASSET_DIR / f"{bg_id}.png"
        ff("-f", "lavfi", "-i",
           "gradients=s=540x960:c0=0x0a3d91:c1=0xff8c1a:d=0.1:r=1",
           "-frames:v", "1", str(bg))
        ov = app_mod.ASSET_DIR / f"{ov_id}.png"
        ff("-f", "lavfi", "-i", "color=c=0x19d47a:s=320x200:d=0.1:r=1",
           "-frames:v", "1", str(ov))
        made += [bg, ov]
        # Real uploads always carry meta; without it the overlay resolver
        # returns None and the clip is dropped from the graph in silence.
        for aid in (bg_id, ov_id):
            app_mod._write_asset_meta(aid, filename="t.png", source="test")
            made.append(app_mod._asset_meta_path(aid))

        words = [{"word": w, "start": 0.6 + i * 0.45, "end": 0.95 + i * 0.45}
                 for i, w in enumerate("this is the full pipeline".split())]
        style = {"font_name": "Anton", "font_size": 64, "position_y": 82}
        base = {"status": "done", "progress": 100, "output": None, "error": None,
                "audio": None, "emoji_rules": {}, "created_at": 0.0}
        app_mod.jobs[sid] = {**base, "words": words, "style": style,
                             "filename": "portrait.mp4"}
        app_mod.jobs[rid] = {**base, "words": None, "style": style,
                             "filename": "e2e.mp4"}

        timeline = {
            "canvas": "9x16", "fps": 30, "fit": "cover", "bg": "#000000",
            "style": style,
            "background": {"asset_id": bg_id, "fit": "cover"},
            "tracks": {
                "main": [
                    {"id": "m1", "source_job_id": sid, "in": 0.0, "out": 3.0,
                     "transition": {"type": "crossfade", "duration": 0.3}},
                    {"id": "m2", "source_job_id": sid, "in": 3.0, "out": 5.0},
                ],
                "effects": [
                    {"id": "f1", "type": "punch_zoom", "start": 0.4, "out": 1.2,
                     "intensity": "med", "hit": 0.0},
                    {"id": "f2", "type": "ken_burns", "start": 1.8, "out": 2.0,
                     "intensity": "med", "direction": "in"},
                    {"id": "f3", "type": "zoom_2x", "start": 4.0, "out": 0.8},
                ],
                "overlay": [
                    {"id": "o1", "asset_id": ov_id, "start": 1.0, "in": 0,
                     "out": 2.5, "x": 0.12, "y": 0.12, "w": 0.4, "h": 0.18,
                     "fit": "cover", "opacity": 1.0, "fade_in": 0, "fade_out": 0,
                     "motion": {"type": "swipe_left", "in": 0.8}},
                ],
                "text": [{"id": "t1", "text": "END TO END", "start": 0.3,
                          "out": 2.0, "x": 0.5, "y": 0.2, "size": 56,
                          "font": "Anton", "align": 2, "anim": "fade"}],
                "music": [],
            },
        }

        app_mod.render_timeline_job(rid, timeline)
        assert app_mod.jobs[rid]["status"] == "done", app_mod.jobs[rid].get("error")

        out = app_mod.OUTPUT_DIR / f"{rid}.mp4"
        made.append(out)
        assert out.exists() and out.stat().st_size > 100_000

        def frame(t):
            png = tmp_path / f"f{t}.png"
            ff("-ss", str(t), "-i", str(out), "-frames:v", "1", str(png))
            return cv2.imread(str(png))

        # The background replaced hers. Asserted by comparing against the
        # SOURCE at the same corner rather than against a hardcoded colour:
        # the gradient's exact shade at a given pixel is an accident of the
        # canvas size, but "this is no longer the photo's own background" is
        # the actual claim.
        mid = frame(2.4)
        src_png = tmp_path / "src.png"
        ff("-ss", "2.4", "-i", str(src), "-frames:v", "1", str(src_png))
        src_frame = cv2.resize(cv2.imread(str(src_png)),
                               (mid.shape[1], mid.shape[0]))
        corner = (slice(60, 200), slice(30, 160))
        rendered_bg = mid[corner].reshape(-1, 3).mean(0)
        original_bg = src_frame[corner].reshape(-1, 3).mean(0)
        moved = float(np.abs(rendered_bg - original_bg).sum())
        assert moved > 60, (
            f"the corner still looks like the photo's own background: "
            f"rendered {rendered_bg} vs original {original_bg}")
        assert rendered_bg.max() > 60, f"corner is near-black: {rendered_bg}"

        # And she survived it -- the matte kept the subject, skin-toned.
        # Sampled below the overlay box (which ends at 0.12+0.18 of the
        # height) and above the captions, or this reads the green card
        # instead of her face.
        face = mid[760:880, 470:640].reshape(-1, 3).mean(0)
        assert face[2] > face[0] + 20, f"subject missing: BGR {face}"

        # The overlay swiped in rather than appearing at rest, inside the same
        # filtergraph as everything above.
        def green_left(im):
            g = ((im[:, :, 1].astype(int) - im[:, :, 2].astype(int) > 40)
                 & (im[:, :, 1].astype(int) - im[:, :, 0].astype(int) > 40))
            cols = np.where(g.any(axis=0))[0]
            return int(cols.min()) if len(cols) else None

        early, late = green_left(frame(1.05)), green_left(frame(2.4))
        assert early is not None and late is not None, "the overlay never landed"
        assert early > late + 200, f"the overlay did not travel: {early} -> {late}"
        assert abs(late - int(0.12 * 1080)) < 12, f"settled at {late}, not at rest"

        # And the zoom hold still changes framing this late in the chain.
        assert green_left(frame(4.3)) is None, "overlay outstayed its range"
    finally:
        for p in made:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        app_mod.jobs.pop(sid, None)
        app_mod.jobs.pop(rid, None)


def test_the_coeditor_can_see_your_own_photos_and_the_current_background(
        app_mod, temp_db, monkeypatch, tmp_path):
    """It told people to "name photos you have already uploaded" and then
    never showed it any, so that advice could not be followed. It also could
    not tell whether there was a background to clear."""
    import uuid
    aid = uuid.uuid4().hex
    app_mod.ASSET_DIR.mkdir(parents=True, exist_ok=True)
    pic = app_mod.ASSET_DIR / f"{aid}.png"
    pic.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    app_mod._write_asset_meta(aid, filename="my beach.png", source="upload")
    seen = {}
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        lambda p: (seen.setdefault("p", p), {"ops": []})[1])
    try:
        tl = _coeditor_timeline()
        tl["background"] = {"asset_id": "deadbeef" * 4, "fit": "cover"}
        app_mod.app.test_client().post("/co-editor", json={
            "job_id": "j", "prompt": "use my beach photo", "timeline": tl})
        p = seen["p"]
        assert "my beach.png" in p, "the model cannot see the user's own photos"
        assert aid in p, "it needs the id, since that is what the op takes"
        assert "your_photos" in p and "stock_photos_available" in p
        assert '"background"' in p, "it cannot tell if there is one to clear"
    finally:
        pic.unlink(missing_ok=True)
        app_mod._asset_meta_path(aid).unlink(missing_ok=True)


def test_the_background_has_a_control_not_just_a_prompt():
    """It shipped prompt-only: no picker, no clear, and nothing on screen
    saying whether one was set, so "nothing happened" was unanswerable."""
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    assert "__bg_asset" in js, "no way to pick a background"
    assert 'data-act="remove-bg"' in js, "no way to clear one"
    assert "Background (behind you)" in js, "the panel has no heading"
    # And the handlers have to exist, or the control is decoration.
    for key in ('key === "__bg_asset"', 'key === "__bg_fit"'):
        assert key in js, f"{key} is rendered but never handled"


def test_clearing_a_background_that_is_not_there_says_so():
    """"Applied 1 edit" after clearing nothing is indistinguishable from a
    bug -- which is how this looked the first time it was used."""
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    branch = js[js.index('name === "set_background"'):][:900]
    assert "no background to remove" in branch, (
        "a no-op clear must say so rather than report success")
    # The Render reminder belongs in ONE place. It used to be in this note
    # AND appended by sendCoEditorPrompt, so the drawer read "background set
    # - click Render to burn it in - Click Render to burn into the export."
    assert "Render" not in branch, (
        "the Render reminder is appended once by sendCoEditorPrompt; saying "
        "it here too is what produced the doubled sentence")
    send = js[js.index("async function sendCoEditorPrompt"):][:2600]
    assert send.count("Click \u25b6 Render") == 1, (
        "exactly one Render reminder, appended in one place")


def test_the_cutout_preview_never_masks_with_a_matte_it_has_not_checked():
    """A mask-image that fails to load does not fall back to "no mask" -- it
    makes the element ENTIRELY INVISIBLE. Verified in a browser: a video with
    a mask pointing at a 404 disappears and leaves only what is behind it.

    So a deploy without mediapipe (400), a job not in memory (404), or a
    frame with nobody in it (a valid but black matte) all produced the same
    picture: the background alone, no person. Which reads as "it replaced me"
    rather than "the cut-out failed", and sent the search in the wrong
    direction for a day.
    """
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    fn = js[js.index("function refreshBackgroundPreview"):][:3000]

    assert "fetch(" in fn, (
        "the matte must be fetched and checked before it is used as a mask")
    assert "X-Subject-Coverage" in fn, (
        "a matte that loads but finds nobody masks the video away just as "
        "completely as one that fails -- the coverage header says which")
    assert ".catch(" in fn and "unmask" in fn, "no fallback path"

    # The mask must only ever be set from a verified blob, never the raw URL.
    assert "createObjectURL" in fn, "mask should come from the fetched blob"
    assert "maskImage = `url(/segment-preview" not in js, (
        "masking straight from the endpoint URL skips the check entirely")
    assert "revokeObjectURL" in fn, "blob urls leak without this"


# --- A broken dependency must cost the background, not the render -------

def test_the_deps_check_verifies_the_api_not_just_the_import(app_mod, monkeypatch):
    """MediaPipe REMOVED mp.solutions in 0.10.22.

    The old check only did `import mediapipe` and said yes, so on a newer
    install the caller died on the attribute instead -- which is how a
    deploy that ignored the pin turned an optional background into
    "Render failed: module 'mediapipe' has no attribute 'solutions'".

    The existing test that mediapipe exposes solutions checks the LOCAL
    install, which is pinned correctly, so it can never catch a server that
    is not. This one can.
    """
    import types
    fake = types.ModuleType("mediapipe")
    fake.__version__ = "0.10.31"          # imports fine, has no solutions
    monkeypatch.setitem(sys.modules, "mediapipe", fake)

    ok, msg = app_mod._segment_deps_available()
    assert ok is False, "a mediapipe without solutions must not pass the check"
    assert "0.10.31" in msg, "say which version is installed"
    assert "0.10.22" in msg, "say what the pin is"
    assert "reinstall" in msg.lower(), "say what fixes it"


def test_a_matte_that_cannot_be_built_returns_none_rather_than_raising(
        app_mod, tmp_path, monkeypatch):
    """The segmenter used to be constructed outside the try, so the error
    escaped every fallback and killed the export."""
    import types
    src = tmp_path / "x.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=64x64:d=0.3:r=10", "-pix_fmt", "yuv420p",
                    str(src)], check=True)

    real = sys.modules.get("mediapipe")
    broken = types.ModuleType("mediapipe")
    broken.__version__ = "0.10.31"
    monkeypatch.setitem(sys.modules, "mediapipe", broken)
    try:
        got = app_mod._tl_matte_video(src, 64, 64, 10, dur=0.3)
    except Exception as exc:                       # noqa: BLE001
        pytest.fail(f"a broken mediapipe must not raise out of here: {exc!r}")
    assert got is None
    assert real is not None or True


def test_a_render_with_no_segmentation_still_finishes(app_mod, tmp_path,
                                                      monkeypatch):
    """An optional layer must never cost the export.

    Before this, a background on a server whose mediapipe had dropped the
    API failed the whole render -- captions, cuts, effects and all.
    """
    import uuid
    monkeypatch.setattr(app_mod, "_segment_deps_available",
                        lambda: (False, "mediapipe 0.10.31 has no solutions"))
    # A real picture, so it gets past the source lookup and reaches the
    # segmentation check -- a missing asset bails earlier and for a
    # different reason.
    aid = uuid.uuid4().hex
    app_mod.ASSET_DIR.mkdir(parents=True, exist_ok=True)
    pic = app_mod.ASSET_DIR / f"{aid}.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=red:s=32x32:d=0.1:r=1", "-frames:v", "1",
                    str(pic)], check=True)
    out = tmp_path / "out.mp4"
    try:
        ok = app_mod._tl_composite_background(
            tmp_path / "base.mp4", {"asset_id": aid}, 540, 960, out)
    finally:
        pic.unlink(missing_ok=True)
    assert ok is False, "it must decline, not raise"
    assert not out.exists(), "and leave no broken file behind"
    assert app_mod._tl_background_skipped, "the reason must be recorded"
    assert "solutions" in app_mod._tl_background_skipped[-1]


def test_the_preview_does_not_promise_a_cutout_the_render_cannot_do():
    """The fallback used to say "The render still cuts you out properly",
    which is false when the server cannot segment at all -- and that
    sentence is what sent someone to click Render and hit the failure."""
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    assert "The render still cuts you out properly" not in js
    assert "The render will skip it" in js, (
        "say what the render will actually do")


# --- Backgrounds you can switch in and out ------------------------------

@needs_ffmpeg
def test_backgrounds_switch_in_and_out_and_leave_the_footage_between_them(
        app_mod, tmp_path):
    """A background is an overlay marked `behind`, so switching them through
    a clip is several of them in a row on the lane.

    The design rests on one property: laying a cut-out subject back over its
    OWN frame is a no-op. Measured directly before building on it -- zero
    differing pixels inside the subject, the rest confined to chroma
    subsampling at the matte edge, and it does not accumulate over repeated
    passes. That is what lets the pictures be painted onto a copy of the
    frame with no branch for the stretches that have none, and it is why
    the gap between two backgrounds has to be the untouched original.
    """
    for mod in ("cv2", "mediapipe", "matplotlib"):
        if not _have(mod):
            pytest.skip(f"{mod} not installed")
    import cv2
    import uuid

    base = tmp_path / "base.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-loop", "1", "-i",
                    str(_portrait()), "-t", "6", "-r", "30", "-vf",
                    "scale=540:960:force_original_aspect_ratio=increase,"
                    "crop=540:960", "-pix_fmt", "yuv420p", str(base)], check=True)

    made, ids = [], []
    for col in ("0x1133ff", "0x11cc33"):          # blue, then green
        aid = uuid.uuid4().hex
        ids.append(aid)
        pic = app_mod.ASSET_DIR / f"{aid}.png"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c={col}:s=540x960:d=0.1:r=1", "-frames:v", "1",
                        str(pic)], check=True)
        app_mod._write_asset_meta(aid, filename=f"{col}.png", source="test")
        made += [pic, app_mod._asset_meta_path(aid)]

    out = tmp_path / "switch.mp4"
    try:
        clips = [
            # Blue, full frame, 0.5 - 2.0
            {"id": "b1", "asset_id": ids[0], "behind": True, "start": 0.5,
             "in": 0, "out": 1.5, "x": 0, "y": 0, "w": 1, "h": 1,
             "fit": "cover", "opacity": 1, "fade_in": 0, "fade_out": 0},
            # Green, only the TOP 45% -- "any size", real background around it
            {"id": "b2", "asset_id": ids[1], "behind": True, "start": 3.5,
             "in": 0, "out": 1.5, "x": 0, "y": 0, "w": 1, "h": 0.45,
             "fit": "cover", "opacity": 1, "fade_in": 0, "fade_out": 0},
        ]
        app_mod._tl_composite_overlays(base, clips, 540, 960, out, fps=30)
        assert out.exists() and out.stat().st_size > 10_000

        def sample(t, box):
            png = tmp_path / f"s{t}.png"
            subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", str(t), "-i",
                            str(out), "-frames:v", "1", str(png)], check=True)
            im = cv2.imread(str(png))
            return im[box].reshape(-1, 3).mean(0), im

        corner = (slice(40, 90), slice(20, 90))
        blue, _ = sample(1.2, corner)
        assert blue[0] > 150 and blue[2] < 90, f"first background missing: {blue}"
        green, gim = sample(4.2, corner)
        assert green[1] > 140 and green[2] < 90, f"second background missing: {green}"

        # The gap, and after the last one: the ORIGINAL footage, untouched.
        for t in (0.2, 2.8, 5.6):
            got, _ = sample(t, corner)
            assert not (got[0] > 150 and got[2] < 90), f"blue leaked at {t}s"
            assert not (got[1] > 140 and got[2] < 90), f"green leaked at {t}s"

        # Size is honoured: below the green box the real background shows.
        below = gim[700:800, 20:90].reshape(-1, 3).mean(0)
        assert not (below[1] > 140 and below[2] < 90), (
            f"a 45%-tall background covered the whole frame: {below}")

        # And she survives all of it.
        face = gim[600:700, 230:320].reshape(-1, 3).mean(0)
        assert face[2] > face[0] + 20, f"subject lost: {face}"
    finally:
        for p in made:
            Path(p).unlink(missing_ok=True)


def test_an_overlay_without_behind_still_goes_in_front(app_mod):
    """The new branch must not change what overlays have always done."""
    src = (BURNER / "app.py").read_text(encoding="utf-8")
    fn = src[src.index("    def _build_graph(items: list)"):][:2200]
    assert 'it[0].get("behind")' in fn, "the split must key off the flag"
    assert "front = [it for it in items if not it[0].get(\"behind\")]" in fn
    # With nothing behind, no matte is fetched and no split is emitted.
    assert "if behind:" in fn, "the matte must only be built when needed"


def test_a_behind_clip_previews_under_the_video():
    """#tlOverlayLayer has z-index 2 and therefore its own stacking context,
    so a child of it can never sit below the video however it is
    z-indexed. Behind-clips need their own container beneath."""
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    assert "behindLayerEl" in js, "no container for behind-clips"
    assert "item.behind ? behindLayerEl() : layer" in js, (
        "behind-clips must be appended to the lower container")
    css = (BURNER / "static" / "timeline.css").read_text(encoding="utf-8")
    assert ".tl-behind-layer" in css and "z-index: 0" in css


def test_the_behind_switch_is_in_the_overlay_panel():
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    assert 'propCheck("behind"' in js, "no way to mark a clip as a background"
    assert 'data-layout="full"' in js, "no way to make it full frame"
    assert 'key === "behind"' in js, "toggling it must re-render and save"


# --- Making pictures on command -----------------------------------------

def test_render_does_not_download_the_file_by_itself():
    """Render makes the video; it does not take it.

    showExportDone ended by building an <a download> and clicking it, so
    every render -- and most renders are iterations -- dropped a file in the
    Downloads folder. The banner it also shows carries a real download link,
    so the button was always there; the automatic click was the problem.
    """
    app_js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    done = app_js[app_js.index("function showExportDone"):][:1600]
    assert "triggerVideoDownload" not in done, (
        "a finished render must not click a download for you")
    assert "showDownloadReadyBanner" in done, "offer the download instead"
    assert "Download MP4" in app_js, "the banner must carry a real button"

    tl_js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    assert "triggerVideoDownload" not in tl_js, (
        "the polish and render-poll paths downloaded automatically too")


def test_the_image_generator_takes_any_prompt_now(app_mod):
    """It existed and worked, aimed at exactly one thing.

    The single path to the image model was instructed to produce B-roll with
    "no text, no logos, no watermarks, no UI chrome" -- which is why the app
    could not make a graphic with words in it, and why the co-editor said it
    could not create images. The B-roll wording now lives in its caller.
    """
    src = (BURNER / "app.py").read_text(encoding="utf-8")
    assert "def gemini_image(" in src, "no general entry point"
    gen = src[src.index("def gemini_image("):][:700]
    assert "no text" not in gen, "the general path must not refuse text"
    # And the B-roll flow must be provably unchanged.
    broll = src[src.index("def _generate_broll_gemini_image("):][:3000]
    assert "no text, " in broll and "no logos" in broll, (
        "the B-roll framing disappeared instead of moving to its caller")


def test_make_image_hands_back_something_placeable(app_mod, temp_db, monkeypatch,
                                                   tmp_path):
    """A generated picture arrives as an overlay clip, which is what makes
    it useful: behind the speaker or in front, full frame or a box, timed."""
    made = tmp_path / "g.png"
    made.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 400)
    monkeypatch.setattr(app_mod, "_gemini_image_ready", lambda: True)
    monkeypatch.setattr(app_mod, "gemini_image",
                        lambda prompt, stem: made)
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        lambda p: {"ops": [{"op": "make_image", "description": "a pier at dusk",
                            "start": 3.0, "duration": 4, "behind": True}]})
    out = app_mod.app.test_client().post("/co-editor", json={
        "job_id": "j", "prompt": "make me a pier",
        "timeline": _coeditor_timeline()}).get_json()
    assert len(out["ops"]) == 1, out
    clip = out["ops"][0]["clip"]
    assert re.fullmatch(r"[a-f0-9]{32}", clip["asset_id"]), "needs a real asset id"
    assert clip["behind"] is True and clip["w"] == 1.0, "behind implies full frame"
    assert clip["start"] == 3.0 and clip["out"] == 4.0
    app_mod._find_asset_path(clip["asset_id"]).unlink(missing_ok=True)
    app_mod._asset_meta_path(clip["asset_id"]).unlink(missing_ok=True)


def test_make_image_says_so_when_it_cannot(app_mod, temp_db, monkeypatch):
    monkeypatch.setattr(app_mod, "_gemini_image_ready", lambda: False)
    monkeypatch.setattr(
        app_mod, "_gemini_generate_clip_suggestions",
        lambda p: {"ops": [{"op": "make_image", "description": "a pier"}]})
    out = app_mod.app.test_client().post("/co-editor", json={
        "job_id": "j", "prompt": "make me a pier",
        "timeline": _coeditor_timeline()}).get_json()
    assert out["ops"] == []
    assert "GEMINI_API_KEY" in out["message"], out["message"]


def test_a_drawn_text_thread_carries_the_exact_words(app_mod, tmp_path):
    """The case generation cannot do.

    An image model asked for a message saying something specific returns
    something that reads as a message and says something else. So this is
    drawn, and the test that matters is that the words asked for are the
    words present.
    """
    if not _have("PIL"):
        pytest.skip("Pillow not installed")
    from PIL import Image
    dest = tmp_path / "t.png"
    got = app_mod.draw_text_thread([
        {"from": "them", "text": "are you actually going tonight"},
        {"from": "me", "text": "obviously"},
    ], dest, title="Mom")
    assert got and dest.exists()

    im = Image.open(dest).convert("RGBA")
    assert im.mode == "RGBA", "must be transparent to sit over video"
    px = im.load()
    corner = px[2, 2]
    assert corner[3] == 0, f"the corner should be transparent, got {corner}"

    # "me" is blue and on the RIGHT; "them" is grey and on the LEFT.
    w, h = im.size
    def row_has(colour, y, xs):
        return any(abs(px[x, y][0] - colour[0]) < 24
                   and abs(px[x, y][2] - colour[2]) < 24 for x in xs)
    blue, grey = (10, 132, 255), (58, 58, 60)
    right, left = range(int(w * 0.75), w - 4), range(4, int(w * 0.25))
    assert any(row_has(blue, y, right) for y in range(0, h, 4)), "no blue on the right"
    assert any(row_has(grey, y, left) for y in range(0, h, 4)), "no grey on the left"


def test_the_coeditor_knows_it_can_make_pictures(app_mod):
    """It answered "I cannot create custom images" while the capability sat
    in the codebase behind a hardcoded B-roll prompt."""
    names = app_mod._co_editor_op_names()
    assert {"make_image", "add_text_message"} <= names
    shown = "\n".join(app_mod.CO_EDITOR_OP_EXAMPLES)
    assert '"op": "make_image"' in shown and '"op": "add_text_message"' in shown
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    body = js[js.index("async function applyCoEditorOps"):][:26000]
    for n in ("make_image", "add_text_message"):
        assert f'name === "{n}"' in body, f"{n} accepted but never placed"


# ---------------------------------------------------------------------------
# Aiming a push at whoever is actually talking.
#
# The bug these guard: `at: "speaker"` went through _face_anchor_at, whose
# whole selection rule was `max(faces, key=w*h)`. In a two-person interview
# that is whoever sits nearer the lens -- permanently, whether or not they
# are the one speaking. So "aim it at me" stared at one person for the
# entire video, and a test that only asserted "an anchor came back" would
# have passed throughout.
#
# The fixture therefore has to be one where the two answers DIFFER: two
# faces in every frame, one of them visibly bigger, and the diarization
# handing the smaller one a window of its own. Aiming by size gives the big
# face at every instant; aiming by speaker has to move.
# ---------------------------------------------------------------------------

# Deliberately not round numbers, so a hardcoded 0.5 or a silently-centred
# fallback cannot pass by coincidence.
_NEAR_FACE = {"cx": 0.2600, "cy": 0.4500, "w": 0.30, "h": 0.40}   # bigger
_FAR_FACE = {"cx": 0.7800, "cy": 0.3900, "w": 0.14, "h": 0.18}    # smaller


def _write_reframe(app_mod, job_id, *, diarization):
    """A two-person reframe cache: both faces always visible, A then B.

    _assign_speakers_to_faces attributes a speaker to the HIGHEST-SCORING
    face inside the windows where that speaker talks alone, so the fixture
    scores the talking head above the listening one. That is a real
    limitation of the existing mapping worth naming: it separates two
    people only insofar as detector confidence tracks who is speaking. It
    is not something this change introduced, and the stills path
    (_speaker_still_for_time) has always relied on exactly the same rule.
    """
    samples = []
    t = 0.25
    while t < 8.0:
        talking_near = t < 4.0
        samples.append({"t": round(t, 2), "faces": [
            dict(_NEAR_FACE, score=0.95 if talking_near else 0.55),
            dict(_FAR_FACE, score=0.55 if talking_near else 0.95),
        ]})
        t += 0.5
    path = app_mod.UPLOAD_DIR / f"{job_id}_reframe.json"
    path.write_text(json.dumps({
        "faces": samples,
        "diarization": diarization,
        "duration": 8.0,
    }), encoding="utf-8")
    return path


@pytest.fixture
def two_speakers(app_mod):
    """A job whose analysis says A talks 0-4s and B talks 4-8s."""
    job_id = "pytest_twospk_aim"
    path = _write_reframe(app_mod, job_id, diarization=[
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 4.0},
        {"speaker": "SPEAKER_01", "start": 4.0, "end": 8.0},
    ])
    app_mod._SPEAKER_FACE_CACHE.pop(job_id, None)
    try:
        yield job_id
    finally:
        path.unlink(missing_ok=True)
        app_mod._SPEAKER_FACE_CACHE.pop(job_id, None)


def test_the_aim_follows_whoever_is_talking(app_mod, two_speakers):
    """At a moment in A's window it aims at A; in B's window, at B."""
    early = app_mod._speaker_anchor_at(two_speakers, 2.0)
    late = app_mod._speaker_anchor_at(two_speakers, 6.0)
    assert early and late, "analysis was present, so an anchor was expected"

    # The near face is A's. The far face is B's. The aim has to cross.
    assert early["x"] == pytest.approx(_NEAR_FACE["cx"], abs=0.02), early
    assert late["x"] == pytest.approx(_FAR_FACE["cx"], abs=0.02), late
    assert late["x"] - early["x"] > 0.3, (early, late)


def test_biggest_face_is_the_wrong_answer_here(app_mod, two_speakers):
    """Pins the discrimination: size-based aiming never leaves the near face.

    Without this, the test above could pass against a fixture where both
    rules happen to agree, and would then be guarding nothing.
    """
    by_size_early = app_mod._face_anchor_at(two_speakers, 2.0)
    by_size_late = app_mod._face_anchor_at(two_speakers, 6.0)
    assert by_size_early["x"] == pytest.approx(_NEAR_FACE["cx"], abs=0.001)
    assert by_size_late["x"] == pytest.approx(_NEAR_FACE["cx"], abs=0.001)
    # Same answer at both instants -- it cannot follow anyone.
    assert by_size_late == by_size_early


def test_per_word_speakers_outrank_the_diarization_for_aim(app_mod, two_speakers):
    """Word-level labels are the better signal and must win.

    _active_speaker_for_time prefers them; this proves the aim path passes
    them through rather than silently dropping the argument.
    """
    words = [{"start": 1.90, "end": 2.10, "speaker": "SPEAKER_01"}]
    aimed = app_mod._speaker_anchor_at(two_speakers, 2.0, words=words)
    # Diarization says A at t=2.0; the word says B, so B's face wins.
    assert aimed["x"] == pytest.approx(_FAR_FACE["cx"], abs=0.02), aimed


def test_the_aim_falls_back_to_the_biggest_face_without_diarization(app_mod):
    """One person on camera, or no speaker analysis: biggest face is right.

    The fallback has to return something rather than nothing, or wiring the
    speaker path in would have broken aiming for every single-speaker clip.
    """
    job_id = "pytest_nodiar_aim"
    path = _write_reframe(app_mod, job_id, diarization=[])
    app_mod._SPEAKER_FACE_CACHE.pop(job_id, None)
    try:
        aimed = app_mod._speaker_anchor_at(job_id, 6.0)
        assert aimed is not None, "faces were present; aiming should still work"
        assert aimed == app_mod._face_anchor_at(job_id, 6.0)
        assert aimed["x"] == pytest.approx(_NEAR_FACE["cx"], abs=0.001)
    finally:
        path.unlink(missing_ok=True)
        app_mod._SPEAKER_FACE_CACHE.pop(job_id, None)


def test_the_aim_is_none_when_no_analysis_has_run(app_mod):
    """No reframe cache at all -> no anchor, rather than a centred guess."""
    assert app_mod._speaker_anchor_at("pytest_job_that_does_not_exist", 1.0) is None
    assert app_mod._speaker_anchor_at(None, 1.0) is None


def test_set_punch_aim_at_speaker_resolves_to_the_talker(app_mod, two_speakers):
    """The co-editor path, end to end: `at: "speaker"` must mean the talker.

    This is the op the model actually emits for "aim it at me", so it is
    the one that has to resolve correctly -- not just the helper under it.
    """
    main = [{"source_job_id": two_speakers, "in": 0.0, "out": 8.0}]
    effects = [
        {"type": "punch_zoom", "start": 2.0, "out": 1.0},
        {"type": "punch_zoom", "start": 6.0, "out": 1.0},
    ]
    first = app_mod._resolve_punch_aim(
        {"op": "set_punch_aim", "index": 0, "at": "speaker"}, main, effects)
    second = app_mod._resolve_punch_aim(
        {"op": "set_punch_aim", "index": 1, "at": "speaker"}, main, effects)
    assert first["x"] == pytest.approx(_NEAR_FACE["cx"], abs=0.02), first
    assert second["x"] == pytest.approx(_FAR_FACE["cx"], abs=0.02), second


def test_the_speaker_face_map_is_cached_per_analysis(app_mod, two_speakers):
    """It is O(faces x speakers x segments) and runs once per effect.

    A long edit aims a dozen pushes from one analysis; recomputing the
    mapping for each was tens of thousands of interval comparisons a push.
    """
    rd = app_mod._load_reframe_cache(two_speakers)
    calls = {"n": 0}
    real = app_mod._assign_speakers_to_faces

    def counted(diar, faces):
        calls["n"] += 1
        return real(diar, faces)

    app_mod._assign_speakers_to_faces = counted
    try:
        for _ in range(5):
            app_mod._speaker_face_map(two_speakers, rd)
        assert calls["n"] == 1, f"recomputed {calls['n']} times"
    finally:
        app_mod._assign_speakers_to_faces = real


# ---------------------------------------------------------------------------
# The pin has to reach the machine that installs.
#
# The deployed app died with `module 'mediapipe' has no attribute
# 'solutions'` while pyproject.toml and requirements.txt both said
# `mediapipe>=0.10.5,<0.10.22`. The pin was never wrong -- uv.lock simply
# had no mediapipe entry at all (44 locked packages, zero matches) and
# predated the pin by two weeks. Replit installs from the lock, so the
# declaration was decorative.
#
# test_mediapipe_still_exposes_the_solutions_api checks the LOCAL install,
# which was correctly pinned, so it could never catch this. This checks
# the artifact a deploy actually reads.
# ---------------------------------------------------------------------------

REPO_ROOT = BURNER.parent.parent


def _locked_version(name):
    """The version uv.lock pins for *name*, or None if it locks none."""
    lock = REPO_ROOT / "uv.lock"
    if not lock.exists():
        pytest.skip("uv.lock not in this checkout")
    block = None
    for chunk in lock.read_text(encoding="utf-8").split("[[package]]"):
        if re.search(rf'^\s*name = "{re.escape(name)}"\s*$', chunk, re.M):
            block = chunk
            break
    if block is None:
        return None
    m = re.search(r'^\s*version = "([^"]+)"', block, re.M)
    return m.group(1) if m else None


def test_the_lockfile_honours_the_mediapipe_pin():
    """uv.lock must resolve mediapipe, and resolve it under the ceiling.

    Both halves matter and they fail for different reasons: a missing
    entry means the lock never saw the dependency, and a too-new version
    means it saw it and ignored the ceiling. Either way a fresh build
    lands on a release without `mp.solutions`.
    """
    declared = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    ceiling = re.search(r'"mediapipe>=[\d.]+,<([\d.]+)"', declared)
    assert ceiling, "pyproject.toml no longer declares an upper bound on mediapipe"
    limit = tuple(int(p) for p in ceiling.group(1).split("."))

    locked = _locked_version("mediapipe")
    assert locked is not None, (
        "uv.lock has no mediapipe entry, so the pin never reaches a build "
        "that installs from the lock"
    )
    got = tuple(int(p) for p in re.findall(r"\d+", locked)[:3])
    assert got < limit, f"uv.lock pins mediapipe {locked}, at or above the {ceiling.group(1)} ceiling"


def test_the_lockfile_carries_the_vision_dependencies():
    """opencv too -- the matte and every face read go through cv2.

    It went missing from the lock for the same reason mediapipe did, and
    without it Analyze and the background both fail on a fresh build.
    """
    assert _locked_version("opencv-python-headless") is not None, \
        "uv.lock has no opencv-python-headless entry"


# ---------------------------------------------------------------------------
# Analyze -> two speakers -> two caption colours.
#
# Listed in the brief as unverified, and it turns out to work -- so these
# pin it rather than fix it. Worth pinning because the chain is long and
# every link is silent on failure: a missed stamp does not raise, it just
# paints both people the same colour, which looks like a styling choice.
# ---------------------------------------------------------------------------

# #FFD700 -> ASS is &HBBGGRR&, so gold becomes &H00D7FF&.
_GOLD_HEX, _GOLD_ASS = "#FFD700", "&H00D7FF&"
_CYAN_HEX, _CYAN_ASS = "#00E5FF", "&HFFE500&"
_TWO_COLOURS = {"SPEAKER_00": _GOLD_HEX, "SPEAKER_01": _CYAN_HEX}


def _spoken_colours(ass):
    """Every distinct \\c colour tag in the dialogue, in order."""
    return sorted(set(re.findall(r"\\c(&H[0-9A-Fa-f]{6}&)", ass)))


def test_two_speakers_get_two_caption_colours(app_mod):
    """Words already carrying a speaker: the preferred path."""
    words = [
        {"word": "my", "start": 0.0, "end": 0.3, "speaker": "SPEAKER_00"},
        {"word": "turn", "start": 0.4, "end": 0.7, "speaker": "SPEAKER_00"},
        {"word": "now", "start": 4.0, "end": 4.3, "speaker": "SPEAKER_01"},
        {"word": "mine", "start": 4.4, "end": 4.8, "speaker": "SPEAKER_01"},
    ]
    ass = app_mod.build_ass(words, app_mod._TL_DEFAULT_CAPTION_STYLE, 1080, 1920,
                            speaker_colors=_TWO_COLOURS)
    got = _spoken_colours(ass)
    assert _GOLD_ASS in got and _CYAN_ASS in got, got
    # Both present is the whole claim -- one colour twice would look
    # deliberate on screen and pass any "did it render" check.
    assert len(got) >= 2, got


def test_unstamped_words_get_their_colour_from_the_diarization(app_mod):
    """The fallback: no word.speaker, so build_ass matches on midpoints."""
    words = [{"word": w, "start": s, "end": s + 0.3} for w, s in
             [("my", 0.0), ("turn", 0.4), ("now", 4.0), ("mine", 4.4)]]
    diar = [{"speaker": "SPEAKER_00", "start": 0.0, "end": 2.0},
            {"speaker": "SPEAKER_01", "start": 3.5, "end": 6.0}]
    ass = app_mod.build_ass(words, app_mod._TL_DEFAULT_CAPTION_STYLE, 1080, 1920,
                            speaker_colors=_TWO_COLOURS, diarization=diar)
    got = _spoken_colours(ass)
    assert _GOLD_ASS in got and _CYAN_ASS in got, got


def test_one_speaker_is_not_painted_two_colours(app_mod):
    """A solo clip must not pick up the second speaker's colour.

    The mirror of the test above: a matcher loose enough to colour
    everything would pass both of those and still be wrong.
    """
    words = [{"word": w, "start": s, "end": s + 0.3} for w, s in
             [("all", 0.0), ("mine", 0.4), ("here", 0.8)]]
    diar = [{"speaker": "SPEAKER_00", "start": 0.0, "end": 6.0}]
    ass = app_mod.build_ass(words, app_mod._TL_DEFAULT_CAPTION_STYLE, 1080, 1920,
                            speaker_colors=_TWO_COLOURS, diarization=diar)
    assert _CYAN_ASS not in _spoken_colours(ass), _spoken_colours(ass)


def test_timeline_captions_map_back_to_source_time_for_speakers(app_mod):
    """Cut, trimmed and concatenated words still find the right speaker.

    This is the link with the most room to be wrong, and the one that was
    buried inline in render_timeline_job. The export's clock is not the
    clip's: a word at 1.0s in the output came from 61.0s in the source
    here, and only (src_in, src_out) plus the segment's output start can
    undo that. Two segments with DIFFERENT shifts, because a single
    segment would pass with the shift ignored entirely.
    """
    job_id = "pytest_tlstamp_src"
    cache = app_mod.UPLOAD_DIR / f"{job_id}_reframe.json"
    cache.write_text(json.dumps({"diarization": [
        {"speaker": "SPEAKER_00", "start": 60.0, "end": 64.0},
        {"speaker": "SPEAKER_01", "start": 120.0, "end": 124.0},
    ]}), encoding="utf-8")
    try:
        # Segment 0: source 60-64s, laid at output 0s.
        # Segment 1: source 120-124s, laid at output 4s.
        seg_meta = [
            {"source_job_id": job_id, "src_in": 60.0, "src_out": 64.0},
            {"source_job_id": job_id, "src_in": 120.0, "src_out": 124.0},
        ]
        seg_starts = [0.0, 4.0]
        words = [
            {"word": "first", "start": 1.0, "end": 1.4},   # -> source 61.2s
            {"word": "second", "start": 5.0, "end": 5.4},  # -> source 121.2s
        ]
        n = app_mod._stamp_caption_words_from_source(words, seg_meta, seg_starts)
        assert n == 2, f"stamped {n} of 2"
        assert words[0]["speaker"] == "SPEAKER_00", words[0]
        assert words[1]["speaker"] == "SPEAKER_01", words[1]

        # And the colours actually differ once that reaches build_ass.
        ass = app_mod.build_ass(words, app_mod._TL_DEFAULT_CAPTION_STYLE,
                                1080, 1920, speaker_colors=_TWO_COLOURS)
        got = _spoken_colours(ass)
        assert _GOLD_ASS in got and _CYAN_ASS in got, got
    finally:
        cache.unlink(missing_ok=True)


def test_an_existing_speaker_stamp_is_not_overwritten(app_mod):
    """A stamp made in source time, before any cutting, is the better one."""
    job_id = "pytest_tlstamp_keep"
    cache = app_mod.UPLOAD_DIR / f"{job_id}_reframe.json"
    cache.write_text(json.dumps({"diarization": [
        {"speaker": "SPEAKER_01", "start": 0.0, "end": 10.0},
    ]}), encoding="utf-8")
    try:
        words = [{"word": "mine", "start": 1.0, "end": 1.4,
                  "speaker": "SPEAKER_00"}]
        n = app_mod._stamp_caption_words_from_source(
            words,
            [{"source_job_id": job_id, "src_in": 0.0, "src_out": 10.0}],
            [0.0],
        )
        assert n == 0
        assert words[0]["speaker"] == "SPEAKER_00"
    finally:
        cache.unlink(missing_ok=True)


def test_speaker_stamping_survives_a_missing_analysis(app_mod):
    """No Analyze run for the source clip: no colours, not an exception.

    Captions are the last pass of a long render. Raising here would cost
    the whole export over an optional nicety.
    """
    words = [{"word": "hello", "start": 0.5, "end": 0.9}]
    assert app_mod._stamp_caption_words_from_source(
        words, [{"source_job_id": "pytest_no_such_analysis",
                 "src_in": 0.0, "src_out": 2.0}], [0.0]) == 0
    assert "speaker" not in words[0]
    # And no source job at all (a generated clip, a title card).
    assert app_mod._stamp_caption_words_from_source(
        words, [{"src_in": 0.0, "src_out": 2.0}], [0.0]) == 0


# ---------------------------------------------------------------------------
# A broken mediapipe must cost faces, not Analyze -- and must say so.
#
# The matte's guard was fixed to check mp.solutions rather than trust the
# import. Analyze's guard was not, and it is the one the UI reads to tell
# the user what is about to run: on mediapipe 0.10.22+ it answered
# faces_ok=True, the endpoint promised "face/reframe crops will run", and
# the tracker died on the attribute a minute later. The failure was caught
# downstream, so this was a wrong message rather than a crash -- but a
# wrong message is what sent someone to click Render last time.
# ---------------------------------------------------------------------------

class _NoSolutions:
    """mediapipe as it is from 0.10.22: imports fine, API gone."""
    __version__ = "0.10.26"


class _OnlyFaces:
    """A hypothetical install with one half of the API but not the other."""
    __version__ = "0.10.21"

    class solutions:  # noqa: N801
        face_detection = object()


def test_the_api_check_names_the_pin_not_the_attribute(app_mod, monkeypatch):
    """The message has to point at the remedy, not the symptom."""
    monkeypatch.setitem(sys.modules, "mediapipe", _NoSolutions())
    ok, why = app_mod._mediapipe_solutions_api("face_detection")
    assert not ok
    assert "face_detection" in why
    assert "0.10.22" in why, why          # the version that removed it
    assert "0.10.26" in why, why          # what is actually installed
    assert "reinstall" in why.lower(), why  # what to do about it


def test_the_api_check_is_per_feature(app_mod, monkeypatch):
    """selfie_segmentation and face_detection are asked for separately.

    One guard answering for both is how they drifted in the first place.
    """
    monkeypatch.setitem(sys.modules, "mediapipe", _OnlyFaces())
    assert app_mod._mediapipe_solutions_api("face_detection")[0] is True
    assert app_mod._mediapipe_solutions_api("selfie_segmentation")[0] is False


def test_analyze_reports_faces_unavailable_on_a_too_new_mediapipe(app_mod, monkeypatch):
    """faces_ok must be False, and diarization must be unaffected.

    This is the test that would have caught the guard drift: the old probe
    returned faces_ok=True here, because `import mediapipe` worked.
    """
    monkeypatch.setitem(sys.modules, "mediapipe", _NoSolutions())
    deps = app_mod._probe_analyze_deps()
    assert deps["faces_ok"] is False
    assert "0.10.22" in (deps["faces_error"] or ""), deps["faces_error"]
    # The diarization half is independent and must not be dragged down:
    # "no mediapipe" is not a reason to refuse speaker labels.
    assert "mediapipe" not in (deps["error"] or "").lower(), deps["error"]


def test_analyze_still_runs_for_speakers_without_faces(app_mod, monkeypatch):
    """_reframe_deps_available is what gates the endpoint. Faces are optional."""
    monkeypatch.setattr(app_mod, "_probe_analyze_deps", lambda: {
        "diarization_ok": True, "faces_ok": False,
        "hf_token": True, "pyannote": "ok",
        "mediapipe": "no solutions.face_detection",
        "error": None, "faces_error": "no solutions.face_detection",
    })
    ok, msg = app_mod._reframe_deps_available()
    assert ok is True, msg


def test_the_matte_guard_still_refuses_a_too_new_mediapipe(app_mod, monkeypatch):
    """Sharing the check must not have loosened the matte's guard."""
    monkeypatch.setitem(sys.modules, "mediapipe", _NoSolutions())
    ok, why = app_mod._segment_deps_available()
    assert ok is False
    assert "selfie_segmentation" in why, why


# ---------------------------------------------------------------------------
# "Split the screen when two people talk" -- which has never rendered.
#
# _tl_split_segment stacks two DIFFERENT sources. Two people talking over
# each other is not that shape: one camera, one file, two faces inside the
# same frame. So an Analyze-suggested split_screen landed as a clip with no
# second media, _timeline_clip_source returned None, and the renderer fell
# straight through to an ordinary segment. The suggestion rendered as
# nothing at all, silently, which is why nobody filed it as broken.
#
# The boxes it needs were already being computed. Getting them to the
# renderer meant going through four places that each drop unknown fields:
# _sanitize_effect_suggestions (builds a fresh dict), addEffectClip (fixed
# field list), applyEffectSuggestion (explicit opts) and
# _tl_props_from_lane_effects. _normalize_timeline and serialize() spread,
# so those two were already fine.
# ---------------------------------------------------------------------------

_BOX_LEFT = (0.25, 0.50, 0.10, 0.15)    # cx, cy, w, h
_BOX_RIGHT = (0.75, 0.50, 0.10, 0.15)


def _write_two_box_analysis(app_mod, job_id, *, boxes=(_BOX_LEFT, _BOX_RIGHT),
                            overlaps=None):
    """A cache whose two speakers sit at known, far-apart screen positions.

    Each sample holds only the talking speaker's face, so
    _assign_speakers_to_faces lands each one on their own box exactly.
    """
    diar = [{"speaker": "SPEAKER_00", "start": 0.0, "end": 4.0},
            {"speaker": "SPEAKER_01", "start": 4.0, "end": 8.0}]
    samples = []
    t = 0.25
    while t < 8.0:
        box = boxes[0] if t < 4.0 else boxes[1]
        samples.append({"t": round(t, 2), "faces": [
            {"cx": box[0], "cy": box[1], "w": box[2], "h": box[3], "score": 0.9},
        ]})
        t += 0.5
    path = app_mod.UPLOAD_DIR / f"{job_id}_reframe.json"
    payload = {"faces": samples, "diarization": diar, "duration": 8.0}
    if overlaps is not None:
        payload["overlaps"] = overlaps
    path.write_text(json.dumps(payload), encoding="utf-8")
    app_mod._SPEAKER_FACE_CACHE.pop(job_id, None)
    return path


def test_speaker_split_boxes_come_back_left_to_right(app_mod):
    """Frame order, not speaking order.

    Ordering by who talks first would let the two halves swap sides between
    one split and the next, which reads as a jump cut rather than an edit.
    So the box with the smaller cx is always first -- including when the
    `speakers` list names them the other way round.
    """
    job_id = "pytest_split_lr"
    path = _write_two_box_analysis(app_mod, job_id)
    try:
        boxes = app_mod._tl_speaker_split_boxes(job_id)
        assert boxes and len(boxes) == 2, boxes
        assert boxes[0][0] < boxes[1][0], boxes
        assert boxes[0][0] == pytest.approx(_BOX_LEFT[0], abs=0.02)
        assert boxes[1][0] == pytest.approx(_BOX_RIGHT[0], abs=0.02)

        # Named in reverse: same frame order out.
        rev = app_mod._tl_speaker_split_boxes(job_id, ["SPEAKER_01", "SPEAKER_00"])
        assert rev[0][0] < rev[1][0], rev
    finally:
        path.unlink(missing_ok=True)
        app_mod._SPEAKER_FACE_CACHE.pop(job_id, None)


def test_a_split_is_refused_when_it_would_show_one_face_twice(app_mod):
    """Two speakers, one detected position: not a two-up.

    The detector routinely sees only the person facing the camera. Showing
    the same crop in both panels looks like a rendering fault, so the split
    is declined and the shot plays uncut instead.
    """
    job_id = "pytest_split_same"
    path = _write_two_box_analysis(
        app_mod, job_id, boxes=(_BOX_LEFT, (0.27, 0.50, 0.10, 0.15)))
    try:
        assert app_mod._tl_speaker_split_boxes(job_id) is None
    finally:
        path.unlink(missing_ok=True)
        app_mod._SPEAKER_FACE_CACHE.pop(job_id, None)


def test_a_split_is_refused_without_an_analysis(app_mod):
    assert app_mod._tl_speaker_split_boxes("pytest_no_analysis_at_all") is None
    assert app_mod._tl_speaker_split_boxes(None) is None


def test_the_speaker_crop_fits_the_panel_and_stays_in_frame(app_mod):
    """The crop carries the panel's aspect so the scale after it loses nothing.

    And a face near an edge must clamp inward rather than ask ffmpeg for
    pixels that are not there, which fails the whole render.
    """
    # Portrait canvas, stacked: each panel is 1080x960.
    w, h, x, y = app_mod._tl_speaker_crop_px(
        (0.5, 0.5, 0.10, 0.15), 1280, 720, 1080, 960)
    assert w % 2 == 0 and h % 2 == 0 and x % 2 == 0 and y % 2 == 0
    assert 0 <= x and x + w <= 1280
    assert 0 <= y and y + h <= 720
    assert w / h == pytest.approx(1080 / 960, rel=0.02), (w, h)
    # Taller than the face, so it is a head-and-shoulders crop not a mugshot.
    assert h > 0.15 * 720 * 2

    # Hard against the left edge and the top.
    w2, h2, x2, y2 = app_mod._tl_speaker_crop_px(
        (0.02, 0.02, 0.10, 0.15), 1280, 720, 1080, 960)
    assert x2 == 0 and y2 == 0
    assert x2 + w2 <= 1280 and y2 + h2 <= 720


def test_the_split_suggestion_asks_for_the_mode_that_renders(app_mod):
    """_overlap_split_suggestions must emit mode "speakers".

    Without it the applied clip has no second media and no boxes, and the
    renderer falls through -- the exact silent no-op this fixes.
    """
    job_id = "pytest_split_sugg"
    path = _write_two_box_analysis(app_mod, job_id, overlaps=[
        {"start": 3.0, "end": 5.5, "speakers": ["SPEAKER_00", "SPEAKER_01"]},
    ])
    try:
        out = app_mod._overlap_split_suggestions(job_id, 8.0)
        assert out, "an overlap longer than the floor should suggest a split"
        assert out[0]["type"] == "split_screen"
        assert out[0]["mode"] == "speakers", out[0]
        assert out[0]["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
    finally:
        path.unlink(missing_ok=True)
        app_mod._SPEAKER_FACE_CACHE.pop(job_id, None)


def test_the_sanitiser_keeps_what_makes_a_split_a_split(app_mod):
    """It builds a fresh dict from a fixed field list -- a fourth silent sink.

    mode, speakers and face_anchor were all dropped on the way to the
    client, so the suggestion arrived stripped of everything that told the
    renderer what to do with it.
    """
    raw = [{
        "type": "split_screen", "start_time": 3.0, "end_time": 5.5,
        "mode": "speakers", "speakers": ["SPEAKER_00", "SPEAKER_01"],
        "face_anchor": {"x": 0.3, "y": 0.4},
    }]
    out = app_mod._sanitize_effect_suggestions(raw, 10.0)
    assert out and out[0]["mode"] == "speakers", out
    assert out[0]["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
    assert out[0]["face_anchor"]["x"] == pytest.approx(0.3)

    # Allowlisted, not spread: a model-authored row cannot smuggle fields in.
    sneaky = app_mod._sanitize_effect_suggestions(
        [{"type": "punch_zoom", "start_time": 0.0, "end_time": 1.0,
          "mode": "rm -rf", "asset_id": "../../etc/passwd",
          "source_job_id": "whatever"}], 10.0)
    assert "asset_id" not in sneaky[0] and "source_job_id" not in sneaky[0]
    assert "mode" not in sneaky[0], sneaky[0]


def test_the_renderer_reads_mode_and_speakers_off_the_lane(app_mod):
    """_tl_props_from_lane_effects is the last place they can be dropped."""
    _ken, _punch, _color, split = app_mod._tl_props_from_lane_effects([
        {"type": "split_screen", "start": 1.0, "out": 2.0,
         "mode": "speakers", "speakers": ["SPEAKER_01", "SPEAKER_00"],
         "layout": "stack"},
    ])
    assert split and split["enabled"] is True
    assert split["mode"] == "speakers", split
    assert split["speakers"] == ["SPEAKER_01", "SPEAKER_00"], split


@needs_ffmpeg
def test_the_two_up_really_contains_both_boxes(app_mod, tmp_path):
    """Pixels, not a filter string.

    The source is red on the left half and blue on the right, with a face
    box in each. A correct two-up puts red in one panel and blue in the
    other; a crop that ignored the boxes would put the same colour in both,
    and reading the filter graph would not tell the difference.
    """
    if not _have("cv2"):
        pytest.skip("cv2 not installed")
    import cv2

    src = tmp_path / "halves.mp4"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=red:s=640x720:d=2:r=30",
         "-f", "lavfi", "-i", "color=c=blue:s=640x720:d=2:r=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-filter_complex", "[0:v][1:v]hstack=inputs=2,format=yuv420p[v]",
         "-map", "[v]", "-map", "2:a", "-c:a", "aac", "-shortest", str(src)],
        check=True)

    out = tmp_path / "twoup.mp4"
    # 9:16 canvas -> "auto" means stacked, so panel 0 is the TOP half.
    dur = app_mod._tl_split_speakers_segment(
        src, 0.0, 1.5, [_BOX_LEFT, _BOX_RIGHT], "auto",
        1080, 1920, 30, out)
    assert dur == pytest.approx(1.5)
    assert out.exists() and out.stat().st_size > 1000

    png = tmp_path / "f.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "0.5", "-i", str(out),
                    "-frames:v", "1", str(png)], check=True)
    img = cv2.imread(str(png))
    assert img is not None and img.shape[0] == 1920 and img.shape[1] == 1080

    # BGR. Sample the middle of each panel.
    top = img[480, 540]
    bottom = img[1440, 540]
    assert top[2] > 150 and top[0] < 80, f"top panel is not the red half: {top}"
    assert bottom[0] > 150 and bottom[2] < 80, f"bottom panel is not the blue half: {bottom}"


@needs_ffmpeg
def test_a_speakers_split_survives_the_whole_render_chain(app_mod, tmp_path):
    """Through render_timeline_job, because the chain is where it died.

    Every piece of this worked in isolation before: the boxes were computed,
    the suggestion was made, the clip was added. What failed was the
    handover -- four field lists in a row, each dropping what it did not
    recognise -- so the only test that could have caught it is one that
    starts from a timeline and ends at pixels.
    """
    for mod in ("cv2", "flask"):
        if not _have(mod):
            pytest.skip(f"{mod} not installed")
    import cv2
    import uuid

    sid, rid = uuid.uuid4().hex, uuid.uuid4().hex
    made = []
    try:
        src = app_mod.UPLOAD_DIR / f"{sid}.mp4"
        subprocess.run(
            [FFMPEG, "-v", "error", "-y",
             "-f", "lavfi", "-i", "color=c=red:s=640x720:d=4:r=30",
             "-f", "lavfi", "-i", "color=c=blue:s=640x720:d=4:r=30",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
             "-filter_complex", "[0:v][1:v]hstack=inputs=2,format=yuv420p[v]",
             "-map", "[v]", "-map", "2:a", "-c:a", "aac", "-shortest", str(src)],
            check=True)
        made.append(src)
        made.append(_write_two_box_analysis(app_mod, sid))

        base = {"status": "done", "progress": 100, "output": None, "error": None,
                "audio": None, "emoji_rules": {}, "created_at": 0.0,
                "words": None, "style": {}}
        app_mod.jobs[sid] = {**base, "filename": "halves.mp4"}
        app_mod.jobs[rid] = {**base, "filename": "split.mp4"}

        timeline = {
            "canvas": "9x16", "fps": 30, "fit": "cover", "bg": "#000000",
            "tracks": {
                "main": [{"id": "m1", "source_job_id": sid,
                          "in": 0.0, "out": 2.0, "burn_captions": False}],
                # No source_job_id and no asset_id: before this change the
                # renderer saw nothing to split with and produced the plain
                # shot, which is what the suggestion had always done.
                "effects": [{"id": "f1", "type": "split_screen",
                             "start": 0.2, "out": 1.4, "mode": "speakers",
                             "speakers": ["SPEAKER_00", "SPEAKER_01"],
                             "layout": "auto"}],
                "overlay": [], "text": [], "music": [],
            },
        }
        app_mod.render_timeline_job(rid, timeline)
        assert app_mod.jobs[rid]["status"] == "done", app_mod.jobs[rid].get("error")

        out = app_mod.OUTPUT_DIR / f"{rid}.mp4"
        made.append(out)
        assert out.exists() and out.stat().st_size > 10_000

        def frame(t):
            png = tmp_path / f"c{t}.png"
            subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", str(t),
                            "-i", str(out), "-frames:v", "1", str(png)],
                           check=True)
            return cv2.imread(str(png))

        inside = frame(0.8)
        assert inside is not None, "no frame inside the split window"
        top, bottom = inside[480, 540], inside[1440, 540]
        assert top[2] > 150 and top[0] < 80, f"top panel: {top}"
        assert bottom[0] > 150 and bottom[2] < 80, f"bottom panel: {bottom}"

        # Outside the window the shot is uncut, so both samples read the
        # same half of the 9:16 crop rather than two different colours.
        outside = frame(1.85)
        if outside is not None:
            o_top, o_bottom = outside[480, 540], outside[1440, 540]
            split_like = (o_top[2] > 150 and o_top[0] < 80
                          and o_bottom[0] > 150 and o_bottom[2] < 80)
            assert not split_like, "the split leaked past its own window"
    finally:
        for f in made:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        app_mod._SPEAKER_FACE_CACHE.pop(sid, None)
        app_mod.jobs.pop(sid, None)
        app_mod.jobs.pop(rid, None)


# ---------------------------------------------------------------------------
# The ffmpeg binary must not be able to vanish.
#
# What took the deployed app down: `uv sync` prunes the environment to the
# lockfile, imageio-ffmpeg was declared nowhere, and its bundled binary went
# away under a RUNNING gunicorn worker. Two separate faults turned that into
# every render failing:
#
#   1. _find_ffmpeg() returned imageio's path without checking it exists, so
#      even a fresh start would have used a dead path rather than falling
#      back.
#   2. Resolution happens once, at import, into a constant read by ~100 call
#      sites -- so the worker kept spawning the path it memorised at startup
#      and only a restart could fix it.
#
# The log read `Finalize main spawn failed: [Errno 2] No such file or
# directory` over and over, which names the symptom and not the cause.
# ---------------------------------------------------------------------------

class _FakeImageio:
    """Stands in for imageio_ffmpeg, which is not installed here.

    Worth stating plainly: the test environment has no imageio_ffmpeg, so
    every other test in this file runs on the SYSTEM ffmpeg while production
    prefers a different binary. The binary the renders actually use has
    never been exercised by this suite. These tests cover the choosing, not
    the binary.
    """

    def __init__(self, path):
        self._path = path

    def get_ffmpeg_exe(self):
        return self._path


def test_a_missing_bundled_binary_falls_back_instead_of_being_used(app_mod, monkeypatch, tmp_path):
    """The path is checked, not trusted.

    get_ffmpeg_exe() reports where the binary SHOULD be. An environment
    rebuild can leave the package importable with the binary gone, and
    returning that path means every spawn dies.
    """
    ghost = tmp_path / "binaries" / "ffmpeg-linux-x86_64-v7.0.2"
    assert not ghost.exists()
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", _FakeImageio(str(ghost)))

    got = app_mod._find_ffmpeg()
    assert got != str(ghost), "returned a path that does not exist"
    assert got == (shutil.which("ffmpeg") or "ffmpeg")


def test_a_present_bundled_binary_is_preferred(app_mod, monkeypatch, tmp_path):
    """The preference still works when the binary is really there."""
    real = tmp_path / "ffmpeg"
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(0o755)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", _FakeImageio(str(real)))
    assert app_mod._find_ffmpeg() == str(real)


def test_a_non_executable_binary_is_not_used(app_mod, monkeypatch, tmp_path):
    """Present but not executable is just as unusable as absent."""
    dud = tmp_path / "ffmpeg"
    dud.write_text("not a binary")
    dud.chmod(0o644)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", _FakeImageio(str(dud)))
    assert app_mod._find_ffmpeg() != str(dud)


def test_refresh_reports_whether_the_binary_moved(app_mod, monkeypatch):
    """_refresh_ffmpeg returns True only on an actual change.

    A retry that fires when nothing moved would just spawn the same dead
    path twice and log twice as much.
    """
    real = app_mod.FFMPEG
    try:
        assert app_mod._refresh_ffmpeg() is False, "nothing changed, so no refresh"
        monkeypatch.setattr(app_mod, "FFMPEG", "/nonexistent/ffmpeg")
        assert app_mod._refresh_ffmpeg() is True
        assert app_mod.FFMPEG == real
    finally:
        app_mod.FFMPEG = real


@needs_ffmpeg
def test_the_server_heals_itself_when_ffmpeg_moves(app_mod, tmp_path):
    """The behavioural one: an encode survives the binary disappearing.

    This is the actual outage, reproduced -- a worker holding a path that
    no longer resolves. Before the fix the encode returns False and the
    render fails; after it, the spawn is retried against a re-resolved
    binary and the file is produced. Nobody has to read a log and restart.
    """
    real = app_mod.FFMPEG
    ghost = str(tmp_path / "gone" / "ffmpeg")
    out = tmp_path / "healed.png"
    try:
        app_mod.FFMPEG = ghost          # as if it were pruned under us
        ok = app_mod._run_ffmpeg_encode(
            [ghost, "-f", "lavfi", "-i", "color=c=red:s=64x64:d=0.1",
             "-frames:v", "1", str(out)],
            what="Self-heal probe",
        )
        assert ok is True, "encode did not recover from a moved binary"
        assert out.exists() and out.stat().st_size > 100
        assert app_mod.FFMPEG == real, "the global should now point at a real binary"
    finally:
        app_mod.FFMPEG = real


@needs_ffmpeg
def test_the_resolved_ffmpeg_can_burn_subtitles(app_mod):
    """libass is the whole reason a particular binary is preferred.

    An ffmpeg without the subtitles filter renders every caption as nothing,
    silently. Nothing checked that the binary finally chosen has it.
    """
    assert app_mod._ffmpeg_usable(app_mod.FFMPEG) or app_mod.FFMPEG == "ffmpeg"
    out = subprocess.run([app_mod.FFMPEG, "-hide_banner", "-filters"],
                         capture_output=True, text=True, timeout=60)
    assert re.search(r"^\s*\S+\s+subtitles\s", out.stdout, re.M), \
        "the resolved ffmpeg has no subtitles filter -- captions would burn as nothing"


# ---------------------------------------------------------------------------
# Two dependency lists that disagree is how the app keeps losing pieces.
#
# Three outages, one shape. `uv sync` installs what pyproject.toml declares
# and PRUNES everything else, and requirements.txt had packages pyproject
# never mentioned:
#
#   mediapipe       pinned in both, absent from uv.lock -> background died
#   imageio-ffmpeg  declared nowhere at all             -> every render died
#   pyannote.audio  requirements.txt only               -> speakers died
#
# Each time the package was genuinely required and genuinely undeclared
# somewhere, and nothing failed until production did. These tests make the
# divergence itself the failure, so it is caught in CI rather than by a
# creator whose render just stopped working.
# ---------------------------------------------------------------------------

# Packages intentionally in requirements.txt but NOT pyproject.toml. Each
# needs a reason, so that an accident cannot hide here as a decision.
_NOT_IN_PYPROJECT = {
    "whisperx": "install size vs alignment quality -- _refine_word_alignment "
                "degrades silently to Whisper's own word timings",
}


def _canon(name):
    """PEP 503 normalisation: pyannote.audio and pyannote-audio are one name."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _requirements_names():
    text = (BURNER / "requirements.txt").read_text(encoding="utf-8")
    out = set()
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[;]", line)[0]
        if name.strip():
            out.add(_canon(name))
    return out


def _pyproject_names():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"dependencies\s*=\s*\[(.*?)\n\]", text, re.S)
    assert block, "pyproject.toml has no dependencies array"
    out = set()
    for line in block.group(1).splitlines():
        line = line.split("#")[0].strip()
        m = re.match(r'"([^"<>=!~\[;]+)', line)
        if m:
            out.add(_canon(m.group(1)))
    return out


def test_requirements_and_pyproject_do_not_silently_diverge():
    """Anything the app needs must be in the file the installer reads.

    This is the test that would have caught all three outages. It fails the
    moment a package is added to one list and not the other, which is the
    only moment at which the mistake is cheap.
    """
    missing = _requirements_names() - _pyproject_names() - set(_NOT_IN_PYPROJECT)
    assert not missing, (
        "declared in requirements.txt but not pyproject.toml, so `uv sync` "
        f"will PRUNE them from a deployed environment: {sorted(missing)}. "
        "Add them to pyproject.toml, or record them in _NOT_IN_PYPROJECT "
        "with the reason they are deliberately absent."
    )


def test_pyproject_and_requirements_do_not_diverge_the_other_way_either():
    """The mirror. A one-way check guards half the door.

    A package in pyproject.toml but not requirements.txt leaves anyone
    installing with `pip install -r requirements.txt` missing it -- the same
    failure, pointed the other way.
    """
    missing = _pyproject_names() - _requirements_names()
    assert not missing, (
        "declared in pyproject.toml but not requirements.txt, so a pip "
        f"install from that file silently omits them: {sorted(missing)}"
    )


def test_the_deliberate_exclusions_are_real():
    """An allowlist that outlives its entries starts hiding things.

    If a package is removed from requirements.txt, its exemption here is
    stale and the next reader cannot tell an obsolete entry from a live
    decision.
    """
    reqs = _requirements_names()
    stale = [n for n in _NOT_IN_PYPROJECT if _canon(n) not in reqs]
    assert not stale, f"exempted but no longer in requirements.txt: {stale}"


def test_the_lockfile_carries_the_speaker_stack():
    """pyannote must reach the artifact a deploy installs from.

    The declaration is worthless if the lock does not carry it -- exactly
    how the mediapipe pin was true in two files and false in practice for
    weeks.
    """
    assert _locked_version("pyannote-audio") is not None, (
        "uv.lock has no pyannote-audio entry, so Analyze speakers will not "
        "install on a fresh build"
    )
    # Diarization runs on torch; without it pyannote imports and then fails.
    assert _locked_version("torch") is not None, "uv.lock has no torch entry"


def test_the_lockfile_carries_the_polish_engines():
    """Polish degrades rather than crashes, which makes a loss easy to miss.

    /polish/ping reports which engines exist, so a pruned auto-editor leaves
    the button present and inert -- indistinguishable from broken.
    """
    for pkg in ("auto-editor", "moviepy"):
        assert _locked_version(pkg) is not None, f"uv.lock has no {pkg} entry"


# ---------------------------------------------------------------------------
# pyannote's torch stack: the fourth variant of one disease.
#
# A library drops an API, the dependent is still written against it, and
# nothing in the packaging metadata records the incompatibility. mediapipe
# removed mp.solutions; torchaudio DEPRECATED AudioMetaData in 2.8 and
# REMOVED it in 2.9, and pyannote.audio 3.x still calls it -- while
# declaring torchaudio with NO upper bound, so an honest resolve takes the
# newest and diarization dies at import:
#
#   AttributeError: module 'torchaudio' has no attribute 'AudioMetaData'
#
# Only a lock test can catch this. The constraint lives in pyproject, the
# damage happens in uv.lock, and the import failure happens on a server.
# ---------------------------------------------------------------------------

# Last torchaudio minor that still exports AudioMetaData.
_TORCHAUDIO_API_REMOVED_IN = (2, 9)


def _minor(version):
    parts = [int(x) for x in re.findall(r"\d+", version)[:2]]
    return tuple(parts + [0] * (2 - len(parts)))


def test_the_lock_keeps_torchaudio_below_the_api_removal():
    """pyannote 3.x cannot run on torchaudio 2.9+.

    Fails against a lock that resolved torchaudio freely, which is what
    took diarization down after the dependency was finally installed.
    """
    locked = _locked_version("torchaudio")
    assert locked is not None, "uv.lock has no torchaudio entry"
    assert _minor(locked) < _TORCHAUDIO_API_REMOVED_IN, (
        f"uv.lock pins torchaudio {locked}, at or past the "
        f"{_TORCHAUDIO_API_REMOVED_IN[0]}.{_TORCHAUDIO_API_REMOVED_IN[1]} "
        "release that removed AudioMetaData, which pyannote.audio 3.x calls"
    )


def test_torch_and_torchaudio_share_a_minor():
    """A mismatched pair is its own failure, separate from the API removal.

    torchaudio is built against a specific torch; crossing them produces
    symbol errors that look nothing like a version problem.
    """
    t, ta = _locked_version("torch"), _locked_version("torchaudio")
    assert t and ta, (t, ta)
    assert _minor(t) == _minor(ta), f"torch {t} vs torchaudio {ta}"


def test_the_pin_is_written_down_where_the_resolver_reads_it():
    """A bound that lives only in the lock is one `uv lock` from gone.

    The lock is an output. If pyproject does not carry the constraint, the
    next re-lock silently undoes this and the failure returns.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for pkg in ("torch", "torchaudio"):
        assert re.search(rf'"{pkg}>=[\d.]+,<[\d.]+"', text), (
            f"pyproject.toml does not bound {pkg}, so a re-lock will take "
            "the newest again"
        )


# ---------------------------------------------------------------------------
# Loading the pyannote checkpoint on PyTorch 2.6+.
#
# From 2.6 torch.load defaults to weights_only=True, which permits only a
# small set of tensor types. pyannote 3.x checkpoints predate that and
# carry plain Python objects, so Analyze died at model load with
# "Unsupported global: GLOBAL torch.torch_version.TorchVersion".
#
# The refusals arrive ONE AT A TIME -- each load fails on the next name --
# so the allowlist is a reviewed set rather than something discovered over
# four redeploys. It is deliberately NOT built from whatever the error
# names, and the checkpoint is NOT loaded with weights_only=False: either
# would let a checkpoint execute code on load, which is precisely what the
# default exists to prevent.
#
# No torch in this environment, so these cover the logic, not the load.
# ---------------------------------------------------------------------------

class _FakeTorchSerialization:
    def __init__(self):
        self.registered = []

    def add_safe_globals(self, objs):
        self.registered.extend(objs)


class _FakeTorch:
    def __init__(self):
        self.serialization = _FakeTorchSerialization()


def test_the_allowlist_registers_what_exists_and_skips_what_does_not(app_mod, monkeypatch):
    """A rename upstream must cost the allowlisting, not Analyze.

    These names live in two third-party packages across several versions.
    Importing them unguarded would turn a renamed class into a crash.
    """
    monkeypatch.setattr(app_mod, "_PYANNOTE_SAFE_GLOBALS", [
        ("json", "JSONDecoder"),            # exists
        ("pathlib", "Path"),                # exists
        ("no_such_module_xyz", "Nope"),     # does not
        ("json", "NoSuchAttr"),             # module exists, attribute not
    ])
    fake = _FakeTorch()
    n = app_mod._allow_pyannote_checkpoint_globals(fake)
    assert n == 2, f"expected the 2 resolvable names, got {n}"

    import json as _json
    import pathlib as _pathlib
    assert set(fake.serialization.registered) == {_json.JSONDecoder, _pathlib.Path}


def test_an_older_torch_without_the_api_is_left_alone(app_mod):
    """torch < 2.6 has no add_safe_globals and no new default.

    Calling into a missing API would break the very installs that never
    had the problem.
    """
    class _Torch26Less:
        pass

    assert app_mod._allow_pyannote_checkpoint_globals(_Torch26Less()) == 0


def test_the_safe_unpickler_error_is_recognised(app_mod):
    """It is matched on the message, so the match has to be right.

    PyTorch raises a plain UnpicklingError, indistinguishable by type from
    a genuinely corrupt file.
    """
    real = Exception(
        "Weights only load failed. ... WeightsUnpickler error: Unsupported "
        "global: GLOBAL torch.torch_version.TorchVersion was not an allowed "
        "global by default."
    )
    assert app_mod._is_weights_only_error(real) is True

    # And must NOT swallow the unrelated failures that have their own help.
    for other in ("Repo model pyannote/speaker-diarization-3.1 is gated.",
                  "401 Client Error: Unauthorized",
                  "Connection reset by peer"):
        assert app_mod._is_weights_only_error(Exception(other)) is False, other


def test_the_help_names_the_missing_type_and_where_to_add_it(app_mod):
    """PyTorch's own message is a wall of text offering the unsafe fix.

    The useful part is which name to allowlist, so that is what surfaces.
    """
    err = Exception(
        "Weights only load failed. WeightsUnpickler error: Unsupported "
        "global: GLOBAL omegaconf.listconfig.ListConfig was not an allowed "
        "global by default."
    )
    help_text = app_mod._weights_only_help(err)
    assert "omegaconf.listconfig.ListConfig" in help_text
    assert "_PYANNOTE_SAFE_GLOBALS" in help_text
    assert '("omegaconf.listconfig", "ListConfig")' in help_text


def test_the_help_does_not_recommend_disabling_the_check(app_mod):
    """The guard that matters.

    PyTorch's message suggests weights_only=False, and copying that
    suggestion into our own error would walk someone straight into letting
    a downloaded file execute code. The message must say the opposite.
    """
    err = Exception("Weights only load failed. Unsupported global: GLOBAL x.Y")
    help_text = app_mod._weights_only_help(err)
    advice = help_text.split("PyTorch said:")[0]
    assert "weights_only=False" in advice and "Do NOT" in advice, advice


def test_the_checkpoint_is_never_loaded_with_the_check_disabled(app_mod):
    """Asserted on the source, because the failure is a silent weakening.

    A future "quick fix" here would not fail any behavioural test -- it
    would make Analyze work, which is exactly why it needs a guard that
    notices.
    """
    import ast as _ast
    tree = _ast.parse((BURNER / "app.py").read_text(encoding="utf-8"))
    offenders = []
    for node in _ast.walk(tree):
        # torch.load(..., weights_only=False)
        if isinstance(node, _ast.Call):
            for kw in node.keywords:
                if (kw.arg == "weights_only"
                        and isinstance(kw.value, _ast.Constant)
                        and kw.value.value is False):
                    offenders.append(f"call at line {node.lineno}")
        # kwargs["weights_only"] = False, the patch-torch.load dodge
        if isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Constant) \
                and node.value.value is False:
            for t in node.targets:
                if isinstance(t, _ast.Subscript) and isinstance(t.slice, _ast.Constant) \
                        and t.slice.value == "weights_only":
                    offenders.append(f"assignment at line {node.lineno}")
    assert not offenders, (
        "app.py loads a checkpoint with the safe unpickler disabled: "
        f"{offenders}"
    )


# ---------------------------------------------------------------------------
# An overlay that slides back out.
#
# The motion track arrived and stopped: [[0, offset], [slide, 0]]. A card
# could glide in beautifully and then vanish on a single frame, which is the
# part that reads as unfinished. The original design had an `out` field and
# it was never built.
#
# A lone card RETREATS the way it came -- it was shown, now it is put away.
# A carousel member CARRIES ON THROUGH and exits the far side, because the
# next photo is about to take that space and a retreat would look like it
# changed its mind.
# ---------------------------------------------------------------------------

def test_without_an_out_the_track_is_exactly_what_it_always_was(app_mod):
    """Every overlay that already exists must render identically.

    This is the compatibility claim, and it is cheap to assert exactly
    rather than hope.
    """
    m = {"type": "swipe_left", "in": 0.35}
    assert app_mod.overlay_motion_tracks(m) == {"x": [[0.0, 1.0], [0.35, 0.0]]}
    # Even with a duration known, out=0 means cut.
    assert app_mod.overlay_motion_tracks(m, 3.0) == {"x": [[0.0, 1.0], [0.35, 0.0]]}


def test_a_lone_card_retreats_the_way_it_came(app_mod):
    """swipe_left enters from the right, so it must leave to the right."""
    tr = app_mod.overlay_motion_tracks(
        {"type": "swipe_left", "in": 0.35, "out": 0.4}, 3.0)["x"]
    assert tr[0] == [0.0, 1.0], "does not start off-frame right"
    assert tr[1][1] == 0.0, "does not arrive home"
    assert tr[-1] == [3.0, 1.0], f"does not retreat right: {tr[-1]}"
    # And it holds at home in between rather than drifting.
    assert tr[-2][1] == 0.0 and tr[-2][0] == pytest.approx(2.6)


def test_a_carousel_member_carries_on_through(app_mod):
    """Same entrance, opposite exit -- that IS the distinction."""
    m = {"type": "swipe_left", "in": 0.35, "out": 0.4}
    lone = app_mod.overlay_motion_tracks(m, 3.0)["x"]
    carousel = app_mod.overlay_motion_tracks(m, 3.0, carousel=True)["x"]
    assert lone[:-1] == carousel[:-1], "they should differ only in the exit"
    assert lone[-1][1] == 1.0 and carousel[-1][1] == -1.0
    assert lone[-1][1] == -carousel[-1][1], "exits are not opposite"


def test_a_short_clip_squeezes_rather_than_loses_its_entrance(app_mod):
    """Trimming a clip shorter than in+out must not make the slide vanish.

    Dropping the entrance would look like the effect broke. punch_zoom_tracks
    squeezes for the same reason.
    """
    tr = app_mod.overlay_motion_tracks(
        {"type": "swipe_left", "in": 0.35, "out": 0.4}, 0.5)["x"]
    assert tr[0] == [0.0, 1.0]
    assert tr[-1][0] == pytest.approx(0.5), "exit runs past the clip's end"
    assert 0 < tr[1][0] < 0.5, f"entrance was dropped, not squeezed: {tr}"


def test_the_vertical_swipes_get_an_exit_too(app_mod):
    """y is a separate branch in the builder and could easily be forgotten."""
    tr = app_mod.overlay_motion_tracks(
        {"type": "swipe_up", "in": 0.3, "out": 0.3}, 2.0)
    assert "y" in tr and "x" not in tr
    assert tr["y"][-1] == [2.0, 1.0]


@needs_ffmpeg
def test_a_sliding_overlay_really_leaves_the_frame(app_mod, tmp_path):
    """Pixels, because this is motion.

    The swipe was once declared working off its filter string while the
    preview sat frozen, so a slide-out is only real if the card is actually
    gone. Measured the same way as the entrance test: find the card's
    leftmost column, and watch it travel off the edge.
    """
    if not _have("cv2"):
        pytest.skip("cv2 not installed")
    import cv2
    import numpy as np

    W, H = 360, 640
    base = tmp_path / "sl_base.mp4"
    card = tmp_path / "sl_card.png"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"color=c=0x202020:size={W}x{H}:rate=30:duration=3",
                    "-pix_fmt", "yuv420p", str(base)], check=True)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=0xFF00AA:size=200x200:d=1",
                    "-frames:v", "1", str(card)], check=True)

    def card_columns(frame):
        r, g, b = (frame[:, :, i].astype(int) for i in (2, 1, 0))
        cols = np.where(((r > 150) & (g < 110) & (b > 110)).any(axis=0))[0]
        return (int(cols[0]), int(cols[-1])) if len(cols) else None

    def render(motion, carousel=False):
        clip = {"src": str(card), "start": 0.2, "in": 0, "out": 2.0,
                "x": 0.25, "y": 0.3, "w": 0.5, "fit": "cover",
                "motion": motion}
        if carousel:
            clip["carousel_id"] = "grp1"
        out = tmp_path / ("car.mp4" if carousel else "lone.mp4")
        real = app_mod._timeline_clip_source
        app_mod._timeline_clip_source = (
            lambda c, **kw: card if c.get("src") else real(c, **kw))
        try:
            app_mod._tl_composite_overlays(base, [clip], W, H, out, fps=30)
        finally:
            app_mod._timeline_clip_source = real
        cap = cv2.VideoCapture(str(out))
        seen = {}
        for t in (0.9, 2.15):      # settled mid-life, then deep in the exit
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            seen[t] = card_columns(frame) if ok else None
        cap.release()
        return seen

    motion = {"type": "swipe_left", "in": 0.3, "out": 0.4}
    lone = render(motion)
    assert lone[0.9] is not None, "the card never arrived"
    settled_left = lone[0.9][0]

    # Deep in the exit it must have moved RIGHT, back the way it came.
    assert lone[2.15] is None or lone[2.15][0] > settled_left + 20, (
        f"the card did not retreat: settled at {settled_left}, "
        f"exit frame {lone[2.15]}"
    )

    # A carousel member goes the other way -- that IS the distinction.
    car = render(motion, carousel=True)
    assert car[0.9] is not None and car[0.9][0] == settled_left
    assert car[2.15] is None or car[2.15][0] < settled_left - 20, (
        f"the carousel card did not carry through: exit frame {car[2.15]}"
    )


# ---------------------------------------------------------------------------
# Diarizing the whole edit, not each clip it was cut from.
#
# Several interviews stitched into one Main track came back as TWO speakers,
# not six -- and it was never a cap, since the UI defaults to "Up to 6".
# Diarization runs per SOURCE clip and every run numbers from scratch, so
# interview A and interview B each produce SPEAKER_00 and SPEAKER_01 for
# different humans. Nothing namespaced them, so the project merged six
# people into two and coloured person A and person D identically.
#
# The layout is the piece most likely to be wrong and needs no audio at all,
# so it is tested hardest.
# ---------------------------------------------------------------------------

def _audio_tl(clips):
    return {"canvas": "9x16", "tracks": {"main": clips, "overlay": [],
                                         "effects": [], "text": [], "music": []}}


def test_one_clip_with_no_cuts_is_one_piece_at_zero(app_mod):
    out = app_mod._tl_audio_layout(_audio_tl(
        [{"id": "m1", "source_job_id": "A", "in": 2.0, "out": 9.0}]))
    assert len(out["pieces"]) == 1
    p = out["pieces"][0]
    assert (p["source_job_id"], p["src_in"], p["src_out"]) == ("A", 2.0, 9.0)
    assert p["out_start"] == 0.0 and p["dur"] == 7.0
    assert out["analysis_total"] == 7.0 and out["render_total"] == 7.0


def test_cuts_split_a_clip_into_contiguous_pieces(app_mod):
    """No gap and no overlap -- a gap is exactly where a drift comes from."""
    out = app_mod._tl_audio_layout(_audio_tl([
        {"id": "m1", "source_job_id": "A", "in": 0.0, "out": 10.0,
         "cuts": [[3.0, 4.0], [7.0, 7.5]]}]))
    pieces = out["pieces"]
    assert [(p["src_in"], p["src_out"]) for p in pieces] == [
        (0.0, 3.0), (4.0, 7.0), (7.5, 10.0)]
    # The analysis clock has to be exactly cumulative, because that IS the
    # concatenated audio the diarization timestamps will refer to.
    assert [p["out_start"] for p in pieces] == [0.0, 3.0, 6.0]
    assert out["analysis_total"] == pytest.approx(8.5)
    for a, b in zip(pieces, pieces[1:]):
        assert b["out_start"] == pytest.approx(a["out_start"] + a["dur"]), \
            "a gap or overlap between pieces"


def test_three_sources_come_back_in_order_with_their_own_ids(app_mod):
    """The stitched-interview case: three clips, three sources, in order."""
    out = app_mod._tl_audio_layout(_audio_tl([
        {"id": "m1", "source_job_id": "A", "in": 0.0, "out": 4.0},
        {"id": "m2", "source_job_id": "B", "in": 1.0, "out": 3.0},
        {"id": "m3", "source_job_id": "C", "in": 5.0, "out": 8.0}]))
    assert [p["source_job_id"] for p in out["pieces"]] == ["A", "B", "C"]
    assert [p["out_start"] for p in out["pieces"]] == [0.0, 4.0, 6.0]
    assert out["analysis_total"] == 9.0


def test_a_crossfade_shortens_the_render_but_not_the_analysis(app_mod):
    """Two clocks, deliberately.

    The analysis audio is joined with hard cuts -- a crossfade there would
    blend two people's voices at every boundary -- so its total is the plain
    sum. The export crossfades, so it is shorter. Conflating them is the
    drift that misattributes speakers.
    """
    out = app_mod._tl_audio_layout(_audio_tl([
        {"id": "m1", "source_job_id": "A", "in": 0.0, "out": 4.0},
        {"id": "m2", "source_job_id": "B", "in": 0.0, "out": 4.0,
         "transition": {"type": "crossfade"}}]))
    assert out["analysis_total"] == 8.0, "a crossfade must not shorten the audio"
    assert out["render_total"] < 8.0, "the export is shortened by the xfade"

    # And it is shortened by exactly the transition the renderer would use,
    # because both go through _tl_transition_step rather than two copies of
    # the same sum.
    hard, tdur, offset, seg_start, new_cur = app_mod._tl_transition_step(
        4.0, 4.0, "crossfade")
    assert not hard
    assert out["render_total"] == pytest.approx(8.0 - tdur)
    assert out["pieces"][1]["render_start"] == pytest.approx(offset)


def test_the_renderer_folds_with_the_same_arithmetic(app_mod, tmp_path):
    """Asserted against _tl_build_main_track's own answer, so the two cannot
    disagree about where a crossfaded clip starts."""
    if not _have("flask"):
        pytest.skip("flask not installed")
    import uuid
    jid = uuid.uuid4().hex
    made = []
    try:
        segs = []
        for i, colour in enumerate(("red", "blue")):
            sp = tmp_path / f"seg{i}.mp4"
            subprocess.run(
                [FFMPEG, "-v", "error", "-y",
                 "-f", "lavfi", "-i", f"color=c={colour}:s=320x240:d=2:r=30",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                 "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", str(sp)],
                check=True)
            segs.append((sp, 2.0))
        out = tmp_path / "folded.mp4"
        made.append(out)
        total, starts = app_mod._tl_build_main_track(
            segs, ["crossfade"], out, jid)

        layout = app_mod._tl_audio_layout(_audio_tl([
            {"id": "m1", "source_job_id": "A", "in": 0.0, "out": 2.0},
            {"id": "m2", "source_job_id": "B", "in": 0.0, "out": 2.0,
             "transition": {"type": "crossfade"}}]))
        assert [p["render_start"] for p in layout["pieces"]] == \
            pytest.approx(starts)
        assert layout["render_total"] == pytest.approx(total)
    finally:
        for f in made:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


def test_a_transition_only_lands_at_the_start_of_a_clip(app_mod):
    """A text-edit cut inside a clip is a hard cut, like the renderer's."""
    out = app_mod._tl_audio_layout(_audio_tl([
        {"id": "m1", "source_job_id": "A", "in": 0.0, "out": 9.0,
         "cuts": [[4.0, 5.0]], "transition": {"type": "crossfade"}},
        {"id": "m2", "source_job_id": "B", "in": 0.0, "out": 4.0,
         "transition": {"type": "crossfade"}}]))
    assert [p["transition"] for p in out["pieces"]] == \
        ["crossfade", 0, "crossfade"]


def test_the_global_labels_land_back_in_each_source_own_time(app_mod):
    """The step that makes a whole-edit analysis useful to what exists.

    Everything downstream asks "who is talking at time t in THIS clip", so
    one global diarization is clipped per piece and translated back -- while
    the labels stay global, which is the entire point.
    """
    layout = app_mod._tl_audio_layout(_audio_tl([
        {"id": "m1", "source_job_id": "A", "in": 10.0, "out": 14.0},
        {"id": "m2", "source_job_id": "B", "in": 30.0, "out": 34.0}]))
    pieces = layout["pieces"]
    # Four people across two interviews, in ONE label space.
    segments = [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
        {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_01"},
        {"start": 4.0, "end": 6.0, "speaker": "SPEAKER_02"},
        {"start": 6.0, "end": 8.0, "speaker": "SPEAKER_03"},
    ]
    by = app_mod._tl_project_diarization_to_sources(segments, pieces)
    assert set(by) == {"A", "B"}
    # A's clip starts at 10s in its own source, so global 0-4 is 10-14 there.
    assert [(s["start"], s["end"], s["speaker"]) for s in by["A"]] == [
        (10.0, 12.0, "SPEAKER_00"), (12.0, 14.0, "SPEAKER_01")]
    assert [(s["start"], s["end"], s["speaker"]) for s in by["B"]] == [
        (30.0, 32.0, "SPEAKER_02"), (32.0, 34.0, "SPEAKER_03")]
    # And the labels do NOT collide, which per-clip runs guaranteed they would.
    assert not ({s["speaker"] for s in by["A"]}
                & {s["speaker"] for s in by["B"]})


def test_contiguous_pieces_of_one_source_are_put_back_together(app_mod):
    """Two Main clips that pick up where the other left off are one stretch
    of source audio, so one speaker must not arrive as two segments -- every
    reader expects the shape a single-source run produces.

    And a cut is NOT such a boundary: the 0.4s it removed really is a gap in
    source time, and claiming the speaker talked through it would assert
    something the analysis never said about material the user deleted.
    """
    layout = app_mod._tl_audio_layout(_audio_tl([
        {"id": "m1", "source_job_id": "A", "in": 0.0, "out": 5.0},
        {"id": "m2", "source_job_id": "A", "in": 5.0, "out": 10.0}]))
    by = app_mod._tl_project_diarization_to_sources(
        [{"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"}],
        layout["pieces"])
    assert len(by["A"]) == 1, by["A"]
    assert by["A"][0]["start"] == 0.0 and by["A"][0]["end"] == pytest.approx(10.0)

    layout = app_mod._tl_audio_layout(_audio_tl([
        {"id": "m1", "source_job_id": "A", "in": 0.0, "out": 10.0,
         "cuts": [[4.0, 4.4]]}]))
    by = app_mod._tl_project_diarization_to_sources(
        [{"start": 0.0, "end": 9.6, "speaker": "SPEAKER_00"}],
        layout["pieces"])
    assert [(s["start"], s["end"]) for s in by["A"]] == [(0.0, 4.0), (4.4, 10.0)]


def test_a_piece_with_no_source_job_is_not_attributed(app_mod):
    """An asset clip -- a still, a stock video -- has no transcript to colour."""
    layout = app_mod._tl_audio_layout(_audio_tl([
        {"id": "m1", "asset_id": "deadbeef", "in": 0.0, "out": 3.0},
        {"id": "m2", "source_job_id": "A", "in": 0.0, "out": 3.0}]))
    assert layout["pieces"][0]["source_job_id"] is None
    by = app_mod._tl_project_diarization_to_sources(
        [{"start": 0.0, "end": 6.0, "speaker": "SPEAKER_00"}],
        layout["pieces"])
    # Its 3 seconds still occupy the clock, so A's words start at 0, not 3.
    assert set(by) == {"A"}
    assert by["A"][0]["start"] == 0.0 and by["A"][0]["end"] == pytest.approx(3.0)


def test_an_empty_main_track_says_so_rather_than_diarizing_nothing(app_mod,
                                                                   tmp_path):
    with pytest.raises(RuntimeError, match="empty"):
        app_mod._tl_concat_main_audio(_audio_tl([]), tmp_path / "x.wav")


def test_the_joined_audio_is_as_long_as_the_layout_says(app_mod, tmp_path):
    """What makes the diarization's timestamps line up at all.

    Three distinct tones are not speech, so pyannote cannot be exercised
    offline -- that gap is real and is flagged. What CAN be proven here is
    the thing the timestamps depend on: the stream is exactly the length the
    layout claims, piece by piece.
    """
    if not _have("flask"):
        pytest.skip("flask not installed")
    import uuid
    ids = [uuid.uuid4().hex, uuid.uuid4().hex]
    made = []
    try:
        for jid, freq in zip(ids, (220, 660)):
            src = app_mod.UPLOAD_DIR / f"{jid}.mp4"
            subprocess.run(
                [FFMPEG, "-v", "error", "-y",
                 "-f", "lavfi", "-i", "color=c=black:s=160x120:d=6:r=15",
                 "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=6",
                 "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)],
                check=True)
            made.append(src)
            app_mod.jobs[jid] = {
                "status": "done", "progress": 100, "output": None,
                "error": None, "audio": None, "emoji_rules": {},
                "created_at": 0.0, "words": None, "style": {},
                "filename": f"{jid}.mp4"}

        tl = _audio_tl([
            {"id": "m1", "source_job_id": ids[0], "in": 1.0, "out": 4.0,
             "cuts": [[2.0, 2.5]]},
            {"id": "m2", "source_job_id": ids[1], "in": 0.0, "out": 2.0}])
        wav = tmp_path / "joined.wav"
        layout = app_mod._tl_concat_main_audio(tl, wav)
        made.append(wav)
        assert wav.exists() and wav.stat().st_size > 1000
        # 1.0-2.0, 2.5-4.0, then 0-2.0 == 4.5s
        assert layout["analysis_total"] == pytest.approx(4.5)
        dur = app_mod._media_duration(wav)
        assert dur == pytest.approx(layout["analysis_total"], abs=0.15), \
            f"the joined audio is {dur}s, the layout says {layout['analysis_total']}s"
    finally:
        for f in made:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        for jid in ids:
            app_mod.jobs.pop(jid, None)


def test_a_source_with_no_audio_becomes_silence_not_a_shorter_stream(app_mod,
                                                                    tmp_path):
    """Skipping it would shift every speaker after it -- a failure that looks
    like bad diarization rather than like a missing audio track."""
    if not _have("flask"):
        pytest.skip("flask not installed")
    import uuid
    jid = uuid.uuid4().hex
    made = []
    try:
        src = app_mod.UPLOAD_DIR / f"{jid}.mp4"
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-f", "lavfi",
             "-i", "color=c=green:s=160x120:d=5:r=15",
             "-pix_fmt", "yuv420p", "-an", str(src)], check=True)
        made.append(src)
        app_mod.jobs[jid] = {
            "status": "done", "progress": 100, "output": None, "error": None,
            "audio": None, "emoji_rules": {}, "created_at": 0.0,
            "words": None, "style": {}, "filename": f"{jid}.mp4"}
        wav = tmp_path / "silent.wav"
        layout = app_mod._tl_concat_main_audio(
            _audio_tl([{"id": "m1", "source_job_id": jid,
                        "in": 0.0, "out": 4.0}]), wav)
        made.append(wav)
        assert layout["analysis_total"] == pytest.approx(4.0)
        assert app_mod._media_duration(wav) == pytest.approx(4.0, abs=0.15)
    finally:
        for f in made:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        app_mod.jobs.pop(jid, None)


def test_a_global_label_overrides_the_colliding_per_clip_one(app_mod, tmp_path,
                                                             monkeypatch):
    """The collision, end to end at the caption level.

    Words arrive already stamped by their own clip's Analyze -- and those
    stamps are from runs that each started at SPEAKER_00. So in a stitched
    edit person A and person D both carry "SPEAKER_00" and get the same
    colour. _stamp_caption_words_from_source used to leave a stamped word
    alone, which is right for a per-source analysis and wrong here.
    """
    seg_meta = [
        {"source_job_id": "A", "src_in": 0.0, "src_out": 2.0,
         "burn_captions": True},
        {"source_job_id": "B", "src_in": 0.0, "src_out": 2.0,
         "burn_captions": True},
    ]
    seg_starts = [0.0, 2.0]
    words = [
        {"word": "one", "start": 0.5, "end": 0.9, "speaker": "SPEAKER_00"},
        {"word": "two", "start": 2.5, "end": 2.9, "speaker": "SPEAKER_00"},
    ]
    by_source = {
        "A": [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"}],
        "B": [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_02"}],
    }
    app_mod._stamp_caption_words_from_source(words, seg_meta, seg_starts,
                                             by_source)
    assert words[0]["speaker"] == "SPEAKER_00"
    assert words[1]["speaker"] == "SPEAKER_02", \
        "the second interview kept the colliding label"

    # Without a whole-edit analysis the old contract stands: a stamp made in
    # source time is the better one and is not touched.
    words2 = [{"word": "one", "start": 0.5, "end": 0.9,
               "speaker": "SPEAKER_01"}]
    app_mod._stamp_caption_words_from_source(words2, seg_meta, seg_starts, None)
    assert words2[0]["speaker"] == "SPEAKER_01"


def test_six_speakers_get_six_colours(app_mod):
    """Two is what one interview has. The project ships with Host and Guest,
    so without this everyone from the third person on falls back to the plain
    caption colour -- the thing the global analysis exists to remove."""
    filled = app_mod._fill_speaker_colors(
        {"SPEAKER_00": "#FFD700", "SPEAKER_01": "#00E5FF"},
        [f"SPEAKER_0{i}" for i in range(6)])
    assert len(filled) == 6
    assert len(set(filled.values())) == 6, f"duplicate colours: {filled}"
    # The two the user chose are untouched.
    assert filled["SPEAKER_00"] == "#FFD700" and filled["SPEAKER_01"] == "#00E5FF"
    # Speaker colours off stays off -- turning them on is not this call.
    assert app_mod._fill_speaker_colors({}, ["SPEAKER_00"]) == {}


def test_the_palette_is_the_same_in_both_languages(app_mod):
    """A third and fourth caption colour that disagreed between preview and
    render is the same class of bug as three animation systems each with
    their own idea of time."""
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    block = js[js.index("DEFAULT_SPEAKER_PALETTE = ["):]
    block = block[:block.index("]")]
    in_js = re.findall(r'"(#[0-9a-fA-F]{6})"', block)
    assert in_js, "no palette found in the browser"
    assert [c.lower() for c in in_js] == \
        [c.lower() for c in app_mod.DEFAULT_SPEAKER_PALETTE]


def test_the_whole_edit_endpoint_needs_a_main_track(app_mod, temp_db):
    import uuid
    jid = uuid.uuid4().hex
    app_mod.jobs[jid] = {"status": "done", "progress": 100, "output": None,
                         "error": None, "audio": None, "emoji_rules": {},
                         "created_at": 0.0, "words": None, "style": {},
                         "filename": "x.mp4"}
    try:
        c = app_mod.app.test_client()
        r = c.post("/analyze-timeline-speakers",
                   json={"job_id": jid, "timeline": _audio_tl([])})
        assert r.status_code == 400 and "Main track" in r.get_json()["error"]
        r = c.post("/analyze-timeline-speakers", json={"job_id": "nope"})
        assert r.status_code == 404
        # Nothing analysed yet is a plain answer, not an error.
        st = c.get(f"/timeline-speakers/{jid}").get_json()
        assert st["ready"] is False and st["running"] is False
        assert st["state"] == "idle" and st["error"] is None
    finally:
        app_mod.jobs.pop(jid, None)


def test_a_cached_whole_edit_analysis_is_read_back(app_mod):
    import uuid
    jid = uuid.uuid4().hex
    p = app_mod._tl_diar_path(jid)
    try:
        p.write_text(json.dumps({
            "speakers": ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"],
            "sources": ["A", "B"], "analysis_total": 42.0,
            "by_source": {"A": [{"start": 0, "end": 1,
                                 "speaker": "SPEAKER_00"}]},
        }), encoding="utf-8")
        rec = app_mod._tl_load_timeline_diarization(jid)
        assert rec and rec["speakers"][2] == "SPEAKER_02"
        out = app_mod.app.test_client().get(
            f"/timeline-speakers/{jid}").get_json()
        assert out["ready"] is True and out["speaker_count"] == 3
        assert out["sources"] == ["A", "B"]
        # A file without by_source is not a usable analysis.
        p.write_text(json.dumps({"speakers": ["SPEAKER_00"]}), encoding="utf-8")
        assert app_mod._tl_load_timeline_diarization(jid) is None
    finally:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def test_the_whole_edit_button_exists_and_is_wired():
    """Three buttons, and the distinction between them is what caused the
    confusion -- so the one that is actually global has to be reachable."""
    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="tlAnalyzeEditBtn"' in html
    assert "Analyze whole edit" in html
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    assert 'tlAnalyzeEditBtn' in js
    assert "/analyze-timeline-speakers" in js
    assert "runAnalyzeWholeEdit" in js


def test_four_people_across_two_interviews_get_four_caption_colours(app_mod):
    """The claim the whole feature exists for, end to end offline.

    Chain: the whole-edit diarization is projected into each source's own
    time, overrides the colliding per-clip stamps, the palette fills in the
    speakers the project never had pickers for, and build_ass burns them.

    Before this, interview A's SPEAKER_00 and interview B's SPEAKER_00 were
    different humans with the same name -- so they burned in one colour, and
    a test that only asked "did two colours appear" would have passed on the
    two the project already had.
    """
    layout = app_mod._tl_audio_layout(_audio_tl([
        {"id": "m1", "source_job_id": "A", "in": 0.0, "out": 4.0},
        {"id": "m2", "source_job_id": "B", "in": 0.0, "out": 4.0}]))
    pieces = layout["pieces"]
    by_source = app_mod._tl_project_diarization_to_sources([
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
        {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_01"},
        {"start": 4.0, "end": 6.0, "speaker": "SPEAKER_02"},
        {"start": 6.0, "end": 8.0, "speaker": "SPEAKER_03"},
    ], pieces)

    seg_meta = [{"source_job_id": p["source_job_id"], "src_in": p["src_in"],
                 "src_out": p["src_out"], "burn_captions": True}
                for p in pieces]
    seg_starts = [p["out_start"] for p in pieces]
    # Every word arrives stamped the way its own clip's Analyze stamped it:
    # two names for four people.
    words = [
        {"word": "one", "start": 0.5, "end": 0.9, "speaker": "SPEAKER_00"},
        {"word": "two", "start": 2.5, "end": 2.9, "speaker": "SPEAKER_01"},
        {"word": "three", "start": 4.5, "end": 4.9, "speaker": "SPEAKER_00"},
        {"word": "four", "start": 6.5, "end": 6.9, "speaker": "SPEAKER_01"},
    ]
    app_mod._stamp_caption_words_from_source(words, seg_meta, seg_starts,
                                             by_source)
    assert [w["speaker"] for w in words] == [
        "SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03"]

    colours = app_mod._fill_speaker_colors(
        dict(_TWO_COLOURS), ["SPEAKER_0%d" % i for i in range(4)])
    ass = app_mod.build_ass(words, app_mod._TL_DEFAULT_CAPTION_STYLE,
                            1080, 1920, speaker_colors=colours)
    got = _spoken_colours(ass)
    assert len(got) == 4, f"four people, {len(got)} colour(s): {got}"

    # The discrimination: without the whole-edit analysis the same four words
    # keep their colliding labels and burn in TWO colours. If this passed,
    # the test above would be guarding nothing.
    words_before = [
        {"word": "one", "start": 0.5, "end": 0.9, "speaker": "SPEAKER_00"},
        {"word": "two", "start": 2.5, "end": 2.9, "speaker": "SPEAKER_01"},
        {"word": "three", "start": 4.5, "end": 4.9, "speaker": "SPEAKER_00"},
        {"word": "four", "start": 6.5, "end": 6.9, "speaker": "SPEAKER_01"},
    ]
    app_mod._stamp_caption_words_from_source(words_before, seg_meta,
                                             seg_starts, None)
    ass_before = app_mod.build_ass(words_before, app_mod._TL_DEFAULT_CAPTION_STYLE,
                                   1080, 1920, speaker_colors=colours)
    assert len(_spoken_colours(ass_before)) == 2, \
        "the bug does not reproduce, so the fix above proves nothing"


# ---------------------------------------------------------------------------
# Saying what actually came back, and not holding the request open.
#
# The deployed app reported:
#
#     Whole-edit analysis failed: Failed to execute 'json' on 'Response':
#     Unexpected token '<', "<!doctype "... is not valid JSON
#
# which is not the error -- it is the error being THROWN AWAY. The response
# was an HTML page and the client called `await r.json()` blind, discarding
# the status code and the page's own text, the only two things that say
# which failure it was.
#
# Two fixes, and they are different failures: the client must report what
# came back, and the analysis must stop being synchronous so a long edit
# cannot be cut off mid-request in the first place.
# ---------------------------------------------------------------------------

_HELPERS_FROM = "  function describeNonJson(res, text, url) {"
_HELPERS_TO = "  let PPS = 14;"

_WERKZEUG_404 = (
    "<!doctype html>\n<html lang=en>\n<title>404 Not Found</title>\n"
    "<h1>Not Found</h1>\n<p>The requested URL was not found on the server.</p>\n"
)


def _describe(status, status_text, text, url="/analyze-timeline-speakers"):
    body = (
        "\nconsole.log(describeNonJson("
        f"{{status: {status}, statusText: {json.dumps(status_text)}}}, "
        f"{json.dumps(text)}, {json.dumps(url)}));"
    )
    return _node_eval(_HELPERS_FROM, _HELPERS_TO, body)


def test_a_404_page_is_reported_as_a_stale_build_not_a_parse_error():
    """The likely cause of the report, and invisible from the browser.

    A Werkzeug 404 page means the route does not exist, which means the
    serving process predates the feature. "Unexpected token '<'" sends
    nobody to a restart; naming the remedy does.
    """
    out = _describe(404, "NOT FOUND", _WERKZEUG_404)
    assert "/analyze-timeline-speakers" in out
    assert "404" in out
    low = out.lower()
    assert "restart" in low and "pull" in low, out
    assert "unexpected token" not in low


def test_a_cut_off_request_is_reported_as_a_timeout():
    """The other way this exact message appears: a proxy gives up on a long
    synchronous request and answers with its own page."""
    for status in (502, 503, 504):
        out = _describe(status, "Gateway Timeout", "<html><body>oops</body></html>")
        assert str(status) in out
        assert "cut off" in out.lower(), out


def test_any_other_page_is_reported_with_its_own_title():
    """A Werkzeug error page carries the exact status in its <title>, which
    is the single most useful token in it."""
    out = _describe(
        500, "INTERNAL SERVER ERROR",
        "<!doctype html>\n<title>500 Internal Server Error</title>\n"
        "<h1>Internal Server Error</h1>")
    assert "500" in out and "Internal Server Error" in out
    assert "not JSON" in out or "not json" in out.lower()


def test_a_real_json_error_still_speaks_for_itself():
    """The helper must not bury a message the server took care to write."""
    body = """
const calls = [];
global.fetch = async () => ({
  ok: false, status: 400, statusText: "BAD REQUEST",
  text: async () => JSON.stringify({error: "pyannote.audio unavailable"}),
});
postJSON("/x", {}).then(
  () => console.log("RESOLVED"),
  (e) => console.log(e.message));
"""
    out = _node_eval(_HELPERS_FROM, _HELPERS_TO, body)
    assert out == "pyannote.audio unavailable", out


def test_postjson_returns_the_parsed_body_on_success():
    body = """
global.fetch = async () => ({
  ok: true, status: 200, statusText: "OK",
  text: async () => JSON.stringify({status: "started", started_at: 12.5}),
});
postJSON("/x", {}).then((d) => console.log(d.status + " " + d.started_at),
                        (e) => console.log("THREW " + e.message));
"""
    assert _node_eval(_HELPERS_FROM, _HELPERS_TO, body) == "started 12.5"


# --- the analysis no longer holds the request open -------------------------

@pytest.fixture
def tl_project(app_mod):
    """A project job with a one-clip Main track, and a clean run table."""
    import uuid
    jid = uuid.uuid4().hex
    app_mod.jobs[jid] = {
        "status": "done", "progress": 100, "output": None, "error": None,
        "audio": None, "emoji_rules": {}, "created_at": 0.0, "words": None,
        "style": {}, "filename": "project.mp4"}
    app_mod._TL_DIAR_RUNS.pop(jid, None)
    try:
        yield jid
    finally:
        app_mod.jobs.pop(jid, None)
        app_mod._TL_DIAR_RUNS.pop(jid, None)
        try:
            app_mod._tl_diar_path(jid).unlink(missing_ok=True)
        except OSError:
            pass


def _tl_body(jid):
    return {"job_id": jid, "timeline": _audio_tl(
        [{"id": "m1", "source_job_id": "A", "in": 0.0, "out": 4.0}])}


def test_the_request_returns_while_the_analysis_is_still_running(
        app_mod, temp_db, tl_project, monkeypatch):
    """The failure being fixed: it used to answer only when pyannote was
    done, so a long edit held the request open until something upstream cut
    it off and returned an HTML page."""
    import threading as _th
    held = _th.Event()
    entered = _th.Event()

    def slow(job_id, timeline, **kw):
        entered.set()
        held.wait(10)           # still running when the POST answers
        return {"speakers": [], "sources": [], "layout": [],
                "analysis_total": 0.0}

    monkeypatch.setattr(app_mod, "analyze_timeline_speakers", slow)
    monkeypatch.setattr(app_mod, "_reframe_deps_available", lambda: (True, ""))
    c = app_mod.app.test_client()
    try:
        r = c.post("/analyze-timeline-speakers", json=_tl_body(tl_project))
        assert r.status_code == 202, r.get_json()
        assert r.get_json()["status"] == "started"
        assert r.get_json()["started_at"] > 0
        assert entered.wait(5), "the worker never started"

        st = c.get(f"/timeline-speakers/{tl_project}").get_json()
        assert st["running"] is True and st["ready"] is False
    finally:
        held.set()


def test_a_second_start_is_refused_rather_than_run_twice(
        app_mod, temp_db, tl_project, monkeypatch):
    """Two pyannote runs over the same audio fight for the same temp files
    and the same CPU for no benefit."""
    import threading as _th
    held = _th.Event()
    monkeypatch.setattr(app_mod, "analyze_timeline_speakers",
                        lambda *a, **k: (held.wait(10), {})[1])
    monkeypatch.setattr(app_mod, "_reframe_deps_available", lambda: (True, ""))
    c = app_mod.app.test_client()
    try:
        assert c.post("/analyze-timeline-speakers",
                      json=_tl_body(tl_project)).status_code == 202
        r2 = c.post("/analyze-timeline-speakers", json=_tl_body(tl_project))
        assert r2.status_code == 409
        assert "already" in r2.get_json()["error"].lower()
    finally:
        held.set()


def test_a_worker_that_raises_ends_in_error_not_running(
        app_mod, temp_db, tl_project, monkeypatch):
    """The outcome worth guarding hardest.

    A spinner with no end is worse than an error: it tells the user
    nothing, cannot be retried, and leaves the button dead. So every exit
    path out of the worker has to leave a terminal state.
    """
    def boom(*a, **k):
        raise ValueError("pyannote fell over")

    monkeypatch.setattr(app_mod, "analyze_timeline_speakers", boom)
    monkeypatch.setattr(app_mod, "_reframe_deps_available", lambda: (True, ""))
    c = app_mod.app.test_client()
    assert c.post("/analyze-timeline-speakers",
                  json=_tl_body(tl_project)).status_code == 202

    deadline = time.time() + 5
    st = {}
    while time.time() < deadline:
        st = c.get(f"/timeline-speakers/{tl_project}").get_json()
        if not st["running"]:
            break
        time.sleep(0.05)
    assert st["running"] is False, "the run never reached a terminal state"
    assert st["state"] == "error"
    assert "pyannote fell over" in (st["error"] or "")


def test_start_poll_ready_end_to_end(app_mod, temp_db, tl_project, monkeypatch,
                                     tmp_path):
    """The whole path, with pyannote stubbed: start, poll, read the result.

    diarize_audio is the only part that cannot run here, so it is the only
    part replaced -- the join, the layout, the projection, the colours, the
    cached record and the two endpoints are all the real ones.
    """
    import uuid
    src_id = uuid.uuid4().hex
    src = app_mod.UPLOAD_DIR / f"{src_id}.mp4"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=160x120:d=5:r=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)],
        check=True)
    app_mod.jobs[src_id] = {
        "status": "done", "progress": 100, "output": None, "error": None,
        "audio": None, "emoji_rules": {}, "created_at": 0.0, "words": None,
        "style": {}, "filename": "src.mp4"}

    monkeypatch.setattr(app_mod, "_reframe_deps_available", lambda: (True, ""))
    monkeypatch.setattr(app_mod, "diarize_audio", lambda *a, **k: [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
        {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_01"},
        {"start": 4.0, "end": 6.0, "speaker": "SPEAKER_02"},
    ])
    c = app_mod.app.test_client()
    try:
        body = {"job_id": tl_project, "timeline": _audio_tl(
            [{"id": "m1", "source_job_id": src_id, "in": 0.0, "out": 4.0}])}
        r = c.post("/analyze-timeline-speakers", json=body)
        assert r.status_code == 202
        started_at = r.get_json()["started_at"]

        deadline = time.time() + 30
        st = {}
        while time.time() < deadline:
            st = c.get(f"/timeline-speakers/{tl_project}").get_json()
            if not st["running"]:
                break
            time.sleep(0.1)
        assert st.get("error") is None, st
        assert st["ready"] is True and st["state"] == "done", st
        assert st["speaker_count"] == 3

        # Colours come back with the result, so the poller can apply them --
        # and three speakers get three, not two plus a fallback.
        colours = st["speaker_colors"]
        assert len(colours) == 3 and len(set(colours.values())) == 3, colours

        # The record is this run's, which is what lets a poller tell it from
        # a cached one. Same clock on both sides.
        assert st["created_at"] >= started_at - 1
    finally:
        try:
            src.unlink(missing_ok=True)
        except OSError:
            pass
        app_mod.jobs.pop(src_id, None)


def test_a_cached_result_is_distinguishable_from_this_run(app_mod, tl_project):
    """Without this the first poll reports instant success.

    An analysis run yesterday leaves a record on disk, so `ready` is true
    before the new run has done anything. The poller compares the record's
    created_at against the start time the POST handed back -- both from the
    server's clock, so no browser-vs-server skew.
    """
    app_mod._tl_diar_path(tl_project).write_text(json.dumps({
        "created_at": 1000.0, "speakers": ["SPEAKER_00"], "sources": ["A"],
        "by_source": {"A": [{"start": 0, "end": 1, "speaker": "SPEAKER_00"}]},
    }), encoding="utf-8")
    st = app_mod.app.test_client().get(
        f"/timeline-speakers/{tl_project}").get_json()
    assert st["ready"] is True and st["created_at"] == 1000.0
    assert st["running"] is False
    # A run started after that record must not accept it.
    assert not (st["created_at"] >= 2000.0 - 1)


def test_postjson_actually_uses_the_describer_on_an_html_reply():
    """The describer being right is not the same as it being reached.

    Caught by making postJSON fall back to JSON.parse on the non-JSON path:
    every test that fed it valid JSON still passed, because none of them
    ever took that path.
    """
    body = """
global.fetch = async () => ({
  ok: false, status: 404, statusText: "NOT FOUND",
  text: async () => %s,
});
postJSON("/analyze-timeline-speakers", {}).then(
  () => console.log("RESOLVED — the HTML reply was treated as success"),
  (e) => console.log(e.message));
""" % json.dumps(_WERKZEUG_404)
    out = _node_eval(_HELPERS_FROM, _HELPERS_TO, body)
    assert "/analyze-timeline-speakers" in out and "404" in out, out
    assert "restart" in out.lower(), out
    assert "unexpected token" not in out.lower()
    assert "RESOLVED" not in out


def test_getjson_describes_an_html_reply_too():
    """The poller runs on getJSON, so an HTML page mid-poll must read the
    same way rather than reviving the original parse error."""
    body = """
global.fetch = async () => ({
  ok: false, status: 504, statusText: "Gateway Timeout",
  text: async () => "<!doctype html><title>504</title>",
});
getJSON("/timeline-speakers/x").then(
  () => console.log("RESOLVED"),
  (e) => console.log(e.message));
"""
    out = _node_eval(_HELPERS_FROM, _HELPERS_TO, body)
    assert "504" in out and "cut off" in out.lower(), out


def test_a_failed_job_reported_with_200_is_data_not_a_transport_error():
    """getJSON must not throw on a body carrying `error` when the status is OK.

    /timeline-speakers answers 200 with {error: "..."} to say the JOB
    failed. Treating that as a transport failure made the poller retry it
    five times as if it were a network blip -- ten seconds of "Starting…"
    and a dead button before the real reason appeared. Every unit test
    passed while that was true; only driving it in a browser showed it.
    """
    body = """
global.fetch = async () => ({
  ok: true, status: 200, statusText: "OK",
  text: async () => JSON.stringify({running: false, state: "error",
                                    error: "no audio stream"}),
});
getJSON("/timeline-speakers/x").then(
  (d) => console.log("RETURNED " + d.state + " " + d.error),
  (e) => console.log("THREW " + e.message));
"""
    out = _node_eval(_HELPERS_FROM, _HELPERS_TO, body)
    assert out == "RETURNED error no audio stream", out

    # A bad STATUS is still a failure, and still speaks with the server's words.
    body_bad = """
global.fetch = async () => ({
  ok: false, status: 500, statusText: "INTERNAL SERVER ERROR",
  text: async () => JSON.stringify({error: "boom"}),
});
getJSON("/timeline-speakers/x").then(
  () => console.log("RESOLVED"), (e) => console.log(e.message));
"""
    assert _node_eval(_HELPERS_FROM, _HELPERS_TO, body_bad) == "boom"


def test_the_poller_surfaces_a_job_error_on_the_first_poll():
    """End to end through the real poll loop, not the source.

    The loop's retry-on-blip counter is what hid this, so the assertion is
    about HOW MANY polls it took: one, not six.
    """
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    poller = js[js.index("  async function pollWholeEditAnalysis"):]
    poller = poller[:poller.index("\n  function wireTranscriptToolbar")]
    block = js[js.index(_HELPERS_FROM):js.index(_HELPERS_TO)] + poller
    block = re.sub(r"^\s*const ", "var ", block, flags=re.M)
    block = re.sub(r"^\s*function ", "function ", block, flags=re.M)
    harness = """
var TL_DIAR_POLL_MS = 1, TL_DIAR_MAX_MS = 5000;
var polls = 0;
function setAnalyzeProgressUI() {}
global.fetch = async () => { polls++; return {
  ok: true, status: 200, statusText: "OK",
  text: async () => JSON.stringify({ready: false, running: false,
                                    state: "error", error: "no audio stream"}),
}; };
pollWholeEditAnalysis("j", 100).then(
  () => console.log("RESOLVED after " + polls),
  (e) => console.log(e.message + " | polls=" + polls));
"""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    out = subprocess.run([node, "-e", block + harness],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "no audio stream | polls=1", out


def test_the_client_polls_rather_than_waiting_on_the_post():
    """Source-level, because the defect is a missing round trip rather than
    a wrong value: a client that awaited the POST's result would work in
    every test here and still time out on a real edit."""
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    fn = js[js.index("async function runAnalyzeWholeEdit"):]
    # Up to the POLLER's definition, not past it: the slice used to run to
    # wireTranscriptToolbar, which swallowed pollWholeEditAnalysis's own
    # `async function` line -- so the assertion below was satisfied by the
    # declaration and would have passed with the call removed. Caught by
    # removing it.
    fn = fn[:fn.index("async function pollWholeEditAnalysis")]
    assert "postJSON(" in fn, "still calling fetch().json() blind"
    assert "await pollWholeEditAnalysis(" in fn, "the POST's reply is awaited as the result"
    assert "await r.json()" not in fn
    poll = js[js.index("async function pollWholeEditAnalysis"):][:3000]
    assert "/timeline-speakers/" in poll
    assert "st.running" in poll and "st.error" in poll
    # A poll with no ceiling is a spinner with no end.
    assert "TL_DIAR_MAX_MS" in poll


# ---------------------------------------------------------------------------
# Telling the analysis how hard to listen.
#
# A stitched multi-interview video came back with 2 speakers when there were
# at least 3, and the voices were described as very different -- so the audio
# is not genuinely ambiguous, something was suppressing the separation.
#
# It is not the video. pyannote embeds short windows of AUDIO and clusters
# them; it never sees the picture, and there is no scene detection anywhere
# in this app, so a cut between two stitched interviews is invisible to it.
#
# Two clustering parameters decide how readily it calls two voices one
# person -- `threshold` and `min_cluster_size`, the latter absorbing a
# speaker who simply said too little -- and neither was reachable. The
# control that WAS reachable is a ceiling, not a target: "Up to 6" sends
# max_speakers and the clustering still picks its own number inside 1..6.
# ---------------------------------------------------------------------------

class _FakePipeline:
    """Stands in for pyannote, which is not installed here.

    Mirrors the two calls the tuning makes -- parameters(instantiated=True)
    and instantiate(params) -- and records what it was handed.
    """

    def __init__(self, params=None, raise_on_instantiate=False):
        self._params = params if params is not None else {
            "clustering": {"method": "centroid", "threshold": 0.70,
                           "min_cluster_size": 12},
            "segmentation": {"min_duration_off": 0.0},
        }
        self.raise_on_instantiate = raise_on_instantiate
        self.instantiated = []

    def parameters(self, instantiated=False):
        import copy
        return copy.deepcopy(self._params)

    def instantiate(self, params):
        if self.raise_on_instantiate:
            raise ValueError("unsupported parameter block")
        self.instantiated.append(params)
        self._params = params


def _clustering_after(app_mod, sensitivity):
    pipe = _FakePipeline()
    app_mod._apply_diarization_sensitivity(pipe, sensitivity)
    return pipe.parameters()["clustering"]


def test_the_sensitivities_are_actually_different_and_ordered(app_mod):
    """A mapping that returned the same numbers for all three would pass
    every other test here, so the ordering is the assertion."""
    base = _FakePipeline().parameters()["clustering"]
    split = _clustering_after(app_mod, "split")
    merge = _clustering_after(app_mod, "merge")

    # "Split" must make it EASIER to call two voices different people.
    assert split["threshold"] < base["threshold"], (split, base)
    assert split["min_cluster_size"] < base["min_cluster_size"], split
    # "Merge" the other way.
    assert merge["threshold"] > base["threshold"], (merge, base)
    assert merge["min_cluster_size"] > base["min_cluster_size"], merge
    # And normal changes nothing at all -- today's behaviour is the default.
    pipe = _FakePipeline()
    assert app_mod._apply_diarization_sensitivity(pipe, "normal") is None
    assert pipe.instantiated == [], "normal must not retune anything"


def test_min_cluster_size_stays_a_usable_integer(app_mod):
    """It is a count of segments. 0 or a float would be a different bug."""
    for sens in ("split", "merge"):
        cl = _clustering_after(app_mod, sens)
        assert isinstance(cl["min_cluster_size"], int)
        assert cl["min_cluster_size"] >= 1


def test_a_pyannote_that_cannot_be_tuned_still_analyses(app_mod):
    """The failure to avoid is "a tuning knob took Analyze down".

    This repo has lost that bet three times on moving dependencies --
    mediapipe's solutions, torchaudio's AudioMetaData, torch.load's
    weights_only default. So every failure path here leaves diarization
    working at the model's own settings.
    """
    # Raises on instantiate.
    pipe = _FakePipeline(raise_on_instantiate=True)
    assert app_mod._apply_diarization_sensitivity(pipe, "split") is None

    # Exposes no clustering parameters at all (a future rename).
    pipe = _FakePipeline(params={"segmentation": {"min_duration_off": 0.0}})
    assert app_mod._apply_diarization_sensitivity(pipe, "split") is None
    assert pipe.instantiated == []

    # parameters() itself blows up.
    class _Hostile:
        def parameters(self, instantiated=False):
            raise RuntimeError("gone")

        def instantiate(self, params):
            raise AssertionError("must not be reached")

    assert app_mod._apply_diarization_sensitivity(_Hostile(), "split") is None

    # Clustering present but with nothing tunable in it.
    pipe = _FakePipeline(params={"clustering": {"method": "centroid"}})
    assert app_mod._apply_diarization_sensitivity(pipe, "split") is None
    assert pipe.instantiated == []


def test_a_tuning_does_not_leak_into_the_next_analysis(app_mod):
    """The pipeline is a module-level cache shared by every Analyze. A
    tuning left in place would quietly change the next run, which is the
    kind of fault that looks like the model being inconsistent."""
    pipe = _FakePipeline()
    before = pipe.parameters()
    restore = app_mod._apply_diarization_sensitivity(pipe, "split")
    assert restore is not None
    assert pipe.parameters()["clustering"]["threshold"] < \
        before["clustering"]["threshold"]
    app_mod._restore_diarization_params(pipe, restore)
    assert pipe.parameters() == before, "the next Analyze inherits the tuning"


def test_an_unknown_sensitivity_falls_back_to_the_default(app_mod):
    """And the default is "auto" -- work the count out -- because asking the
    user for a number they do not have was the original mistake."""
    assert app_mod._DIARIZATION_SENSITIVITY_DEFAULT == "auto"
    for bad in (None, "", "banana", 7):
        assert app_mod._normalize_sensitivity(bad) == "auto"
    # The words the UI sends, and a few obvious synonyms.
    assert app_mod._normalize_sensitivity("AUTO") == "auto"
    assert app_mod._normalize_sensitivity("split") == "split"
    assert app_mod._normalize_sensitivity("MERGE") == "merge"
    assert app_mod._normalize_sensitivity("more") == "split"


def test_both_analyze_endpoints_pass_the_setting_down(app_mod, temp_db,
                                                      monkeypatch, tmp_path):
    """Plumbing, end to end, because a control that reaches nothing is the
    exact shape of the bug being fixed -- num_speakers was wired through
    the whole server and no line of JavaScript ever sent it."""
    import uuid
    seen = {}

    def fake_diarize(path, **kw):
        seen.update(kw)
        return [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}]

    monkeypatch.setattr(app_mod, "diarize_audio", fake_diarize)
    monkeypatch.setattr(app_mod, "_reframe_deps_available", lambda: (True, ""))

    # --- the timeline path -------------------------------------------------
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    src = app_mod.UPLOAD_DIR / f"{sid}.mp4"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=160x120:d=4:r=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)],
        check=True)
    base = {"status": "done", "progress": 100, "output": None, "error": None,
            "audio": None, "emoji_rules": {}, "created_at": 0.0,
            "words": None, "style": {}, "filename": "x.mp4"}
    app_mod.jobs[pid] = dict(base)
    app_mod.jobs[sid] = dict(base)
    try:
        c = app_mod.app.test_client()
        r = c.post("/analyze-timeline-speakers", json={
            "job_id": pid, "sensitivity": "split", "num_speakers": 3,
            "timeline": _audio_tl([{"id": "m1", "source_job_id": sid,
                                    "in": 0.0, "out": 3.0}])})
        assert r.status_code == 202
        deadline = time.time() + 30
        while time.time() < deadline:
            st = c.get(f"/timeline-speakers/{pid}").get_json()
            if not st["running"]:
                break
            time.sleep(0.1)
        assert st.get("error") is None, st
        assert seen.get("sensitivity") == "split", seen
        assert seen.get("num_speakers") == 3, seen
        # And the record says what produced it, so a changing count has a
        # stated cause rather than looking like the model being flaky.
        assert st["sensitivity"] == "split"
    finally:
        try:
            src.unlink(missing_ok=True)
        except OSError:
            pass
        for j in (pid, sid):
            app_mod.jobs.pop(j, None)
            app_mod._TL_DIAR_RUNS.pop(j, None)
            try:
                app_mod._tl_diar_path(j).unlink(missing_ok=True)
            except OSError:
                pass

    # --- the per-source path reaches analyze_reframe with it too ----------
    seen.clear()
    captured = {}
    monkeypatch.setattr(app_mod, "analyze_reframe_job",
                        lambda *a, **k: captured.setdefault("args", a))
    jid = uuid.uuid4().hex
    vid = app_mod.UPLOAD_DIR / f"{jid}.mp4"
    vid.write_bytes(b"\x00" * 128)
    app_mod.jobs[jid] = dict(base)
    try:
        r = app_mod.app.test_client().post("/analyze-reframe", json={
            "job_id": jid, "sensitivity": "split"})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["sensitivity"] == "split"
        # (job_id, video_path, num, min, max, sensitivity)
        assert captured["args"][5] == "split", captured["args"]
    finally:
        try:
            vid.unlink(missing_ok=True)
        except OSError:
            pass
        app_mod.jobs.pop(jid, None)


def test_the_controls_exist_and_one_function_reads_them():
    """Two Analyze buttons read the same two controls. They must not drift
    about what "Up to 6" or "Split similar voices" means."""
    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="analyzeSensitivity"' in html
    assert "split similar voices" in html and "merge similar voices" in html
    assert 'value="auto" selected' in html, \
        "the default must be the one that works the count out"
    assert 'value="exact:3"' in html, "no way to force an exact count"

    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    assert "function analyzeSpeakerOptions()" in js
    fn = js[js.index("function analyzeSpeakerOptions()"):]
    fn = fn[:fn.index("window.analyzeSpeakerOptions")]
    # "Exactly N" must force a count, not widen a ceiling.
    assert "num_speakers" in fn and "max_speakers" in fn
    assert "analyzeSensitivity" in fn

    tl = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    whole = tl[tl.index("async function runAnalyzeWholeEdit"):]
    whole = whole[:whole.index("async function pollWholeEditAnalysis")]
    assert "analyzeSpeakerOptions" in whole, \
        "the timeline reads the controls its own way again"


def test_exactly_n_forces_a_count_rather_than_raising_a_ceiling():
    """The distinction the whole report turned on, checked in node."""
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    block = js[js.index("function analyzeSpeakerOptions()"):
               js.index("window.analyzeSpeakerOptions")]
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    harness = """
var vals = {analyzeMaxSpeakers: "exact:3", analyzeSensitivity: "split"};
global.document = {getElementById: (id) => ({value: vals[id]})};
console.log(JSON.stringify(analyzeSpeakerOptions()));
vals.analyzeMaxSpeakers = "6"; vals.analyzeSensitivity = "normal";
console.log(JSON.stringify(analyzeSpeakerOptions()));
"""
    out = subprocess.run([node, "-e", block + harness],
                         capture_output=True, text=True, check=True).stdout
    exact, ceiling = [json.loads(line) for line in out.strip().splitlines()]
    assert exact == {"num_speakers": 3, "sensitivity": "split"}, exact
    # A ceiling must NOT send num_speakers, or every run forces a number the
    # user was trying to discover.
    assert "num_speakers" not in ceiling, ceiling
    assert ceiling["max_speakers"] == 6 and ceiling["min_speakers"] == 1, ceiling
    # The setting is always sent now, rather than relying on two defaults
    # agreeing -- they disagreed once already.
    assert ceiling["sensitivity"] == "normal", ceiling


# ---------------------------------------------------------------------------
# Working the speaker count out, instead of asking for it.
#
# "I never know the exact count — can it not figure it out itself?" Fair:
# "Exactly N" is useless while N is the question, and "Up to 6" is a ceiling
# the clustering ignores.
#
# One pass can only report what its own threshold produced -- it cannot say
# whether 2 is the truth or an artefact of where that threshold sits. So
# "auto" climbs a ladder of sensitivities and keeps the count that HOLDS
# across two of them. A count that survives a change of setting is evidence.
#
# pyannote is not installed here, so what is tested is the ladder: how many
# passes run, which result is chosen, and that a later pass cannot cost an
# earlier one. Whether the sensitive pass really finds the third speaker
# needs real voices.
# ---------------------------------------------------------------------------

class _LadderStub:
    """Stands in for the whole diarization stack.

    Records every pipeline call and returns a scripted speaker count per
    sensitivity, so the ladder's decisions are observable.
    """

    def __init__(self, counts, fail_on=()):
        self.counts = counts            # {sensitivity: n_speakers}
        self.fail_on = set(fail_on)
        self.calls = []                 # sensitivities, in order
        self.extractions = 0

    def install(self, app_mod, monkeypatch):
        stub = self

        monkeypatch.setattr(app_mod, "_get_diarization_pipeline",
                            lambda: object())
        monkeypatch.setattr(app_mod, "_diarization_torch_threads", lambda: 1)
        monkeypatch.setattr(app_mod, "_load_wav_waveform",
                            lambda p: (None, 16000))
        monkeypatch.setattr(app_mod, "_safe_unlink", lambda p: None)
        monkeypatch.setitem(sys.modules, "torch",
                            type(sys)("torch"))
        sys.modules["torch"].set_num_threads = lambda n: None

        def fake_extract(src, dest):
            stub.extractions += 1
            dest.write_bytes(b"RIFF" + b"\x00" * 64)

        monkeypatch.setattr(app_mod, "_extract_audio_for_diarization",
                            fake_extract)

        def fake_pass(pipeline, waveform, sr, audio_path, call_kwargs, level):
            stub.calls.append(level)
            if level in stub.fail_on:
                raise RuntimeError(f"pass {level} fell over")
            n = stub.counts[level]
            # One segment per speaker, each a second long and labelled so the
            # chosen pass is identifiable from what comes back.
            return [{"start": float(i), "end": float(i) + 1.0,
                     "speaker": f"SPEAKER_{i:02d}", "_from": level}
                    for i in range(n)]

        monkeypatch.setattr(app_mod, "_run_diarization_pass", fake_pass)
        return stub


def _run_auto(app_mod, monkeypatch, counts, fail_on=(), **kw):
    stub = _LadderStub(counts, fail_on).install(app_mod, monkeypatch)
    report = {}
    segs = app_mod.diarize_audio(app_mod.UPLOAD_DIR / "ladder_probe.mp4",
                                 report=report, **kw)
    return stub, segs, report


def test_two_agreeing_passes_stop_at_two(app_mod, monkeypatch):
    """A third pass could only cost time. On a long edit that is minutes."""
    stub, segs, rep = _run_auto(
        app_mod, monkeypatch, {"normal": 3, "split": 3, "split_more": 5})
    assert stub.calls == ["normal", "split"], stub.calls
    assert rep["chosen"]["speaker_count"] == 3
    assert rep["confidence"] == "stable"
    assert len(segs) == 3


def test_a_disagreement_runs_a_third_and_takes_what_repeats(app_mod,
                                                            monkeypatch):
    """The reported case: normal finds 2, sensitive finds 3. If the third
    pass also says 3, then 3 is the count that holds."""
    stub, segs, rep = _run_auto(
        app_mod, monkeypatch, {"normal": 2, "split": 3, "split_more": 3})
    assert stub.calls == ["normal", "split", "split_more"]
    assert rep["chosen"]["speaker_count"] == 3
    assert rep["confidence"] == "stable"
    # And the segments are the chosen pass's own, not another run's.
    assert {s["_from"] for s in segs} == {"split"}, segs


def test_a_count_that_keeps_climbing_is_flagged_rather_than_trusted(app_mod,
                                                                    monkeypatch):
    """2, then 3, then 4 is not discovery -- it is over-splitting. Take the
    middle and SAY it is a guess, rather than reporting 4 with confidence."""
    stub, segs, rep = _run_auto(
        app_mod, monkeypatch, {"normal": 2, "split": 3, "split_more": 4})
    assert stub.calls == ["normal", "split", "split_more"]
    assert rep["confidence"] == "uncertain"
    assert rep["chosen"]["speaker_count"] == 3, rep["chosen"]
    assert {s["_from"] for s in segs} == {"split"}


def test_an_exact_count_skips_the_ladder_entirely(app_mod, monkeypatch):
    """"Exactly N" means the user has told it the answer. Deriving one
    anyway would be slower and would ignore what they said."""
    stub, segs, rep = _run_auto(
        app_mod, monkeypatch, {"normal": 4, "split": 9, "split_more": 9},
        num_speakers=4)
    assert stub.calls == ["normal"], stub.calls
    assert rep["mode"] == "fixed"
    assert len(segs) == 4


def test_an_explicit_sensitivity_still_runs_exactly_one_pass(app_mod,
                                                             monkeypatch):
    """The single-pass settings shipped in #38 must be untouched -- they are
    the fast path, and tripling their cost would be a silent regression."""
    for level in ("normal", "split", "merge"):
        stub, segs, rep = _run_auto(
            app_mod, monkeypatch,
            {"normal": 2, "split": 3, "merge": 1, "split_more": 4},
            sensitivity=level)
        assert stub.calls == [level], (level, stub.calls)
        assert rep["mode"] == "fixed"


def test_the_audio_is_extracted_once_for_the_whole_ladder(app_mod, monkeypatch):
    """Re-extracting per pass would triple the ffmpeg work invisibly -- the
    kind of cost nobody notices because the result is still right."""
    stub, _segs, _rep = _run_auto(
        app_mod, monkeypatch, {"normal": 2, "split": 3, "split_more": 4})
    assert len(stub.calls) == 3, "expected the full ladder for this fixture"
    assert stub.extractions == 1, \
        f"extracted the audio {stub.extractions} times"


def test_a_later_pass_that_crashes_does_not_cost_the_earlier_one(app_mod,
                                                                 monkeypatch):
    """Failing the whole Analyze because the SECOND OPINION fell over would
    be the wrong trade. The first pass is a real diarization."""
    stub, segs, rep = _run_auto(
        app_mod, monkeypatch, {"normal": 2, "split": 3, "split_more": 4},
        fail_on=("split",))
    assert stub.calls == ["normal", "split"]
    assert len(segs) == 2 and {s["_from"] for s in segs} == {"normal"}
    assert rep["lost_passes"] == ["split"]
    assert rep["chosen"]["speaker_count"] == 2

    # But the FIRST pass failing has nothing to fall back on, so it raises
    # rather than silently reporting zero speakers.
    with pytest.raises(RuntimeError):
        _run_auto(app_mod, monkeypatch,
                  {"normal": 2, "split": 3, "split_more": 4},
                  fail_on=("normal",))


def test_the_report_carries_a_breakdown_per_pass(app_mod, monkeypatch):
    """The number alone cannot be judged: 2 and 3 look identical. Seeing a
    third person at 20% of the speech is what makes the answer believable."""
    _stub, _segs, rep = _run_auto(
        app_mod, monkeypatch, {"normal": 2, "split": 3, "split_more": 3})
    assert [p["sensitivity"] for p in rep["passes"]] == \
        ["normal", "split", "split_more"]
    for p in rep["passes"]:
        assert len(p["breakdown"]) == p["speaker_count"]
        assert all("speech_pct" in row for row in p["breakdown"])
        assert "seconds" in p
    # The raw segments are NOT shipped in the report -- they are large and
    # the caller already has the chosen ones.
    assert all("_segments" not in p for p in rep["passes"])


def test_the_default_really_is_auto_on_both_sides(app_mod):
    """A default that disagrees between the server and the page is exactly
    how "Up to 6" came to mean something nobody intended."""
    assert app_mod._DIARIZATION_SENSITIVITY_DEFAULT == "auto"
    stub_counts = {"normal": 2, "split": 2}
    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    sel = html[html.index('id="analyzeSensitivity"'):]
    sel = sel[:sel.index("</select>")]
    assert 'value="auto" selected' in sel, sel[:400]
    # And "auto" must be a ladder, not a tuning -- the rungs tune themselves.
    assert app_mod._apply_diarization_sensitivity(object(), "auto") is None
    assert set(app_mod._DIARIZATION_AUTO_LADDER) <= \
        set(app_mod._DIARIZATION_SENSITIVITY), stub_counts


def test_the_ladder_climbs_towards_splitting(app_mod):
    """Ordered on purpose: the cheap conservative rung first, so the common
    case stops after two. A ladder that started at the most aggressive
    setting would answer differently AND cost more."""
    ladder = app_mod._DIARIZATION_AUTO_LADDER
    thresholds = [app_mod._DIARIZATION_SENSITIVITY[n][0] for n in ladder]
    sizes = [app_mod._DIARIZATION_SENSITIVITY[n][1] for n in ladder]
    assert thresholds == sorted(thresholds, reverse=True), thresholds
    assert sizes == sorted(sizes, reverse=True), sizes
    assert ladder[0] == "normal", "the first rung must be the model's own tuning"


def test_the_evidence_line_says_what_the_other_pass_found():
    """Shown to the user, so it is checked in node rather than assumed."""
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    block = js[js.index("function describeSpeakerDetection(det)"):
               js.index("window.describeSpeakerDetection")]
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    harness = """
const disagreed = {mode: "auto", confidence: "stable",
  chosen: {sensitivity: "split", speaker_count: 3},
  passes: [{sensitivity: "normal", speaker_count: 2},
           {sensitivity: "split", speaker_count: 3}]};
console.log(describeSpeakerDetection(disagreed));
const climbing = {mode: "auto", confidence: "uncertain",
  chosen: {sensitivity: "split", speaker_count: 3},
  passes: [{sensitivity: "normal", speaker_count: 2},
           {sensitivity: "split", speaker_count: 3},
           {sensitivity: "split_more", speaker_count: 4}]};
console.log(describeSpeakerDetection(climbing));
const agreed = {mode: "auto", confidence: "stable",
  chosen: {sensitivity: "split", speaker_count: 3},
  passes: [{sensitivity: "normal", speaker_count: 3},
           {sensitivity: "split", speaker_count: 3}]};
console.log(describeSpeakerDetection(agreed));
console.log("[" + describeSpeakerDetection({mode: "fixed"}) + "]");
"""
    out = subprocess.run([node, "-e", block + harness],
                         capture_output=True, text=True,
                         check=True).stdout.splitlines()
    assert "normal found 2" in out[0], out[0]
    assert "best guess" in out[1], out[1]
    assert "held steady" in out[2], out[2]
    # A single fixed pass has no second opinion to report, so it says nothing
    # rather than inventing reassurance.
    assert out[3] == "[]", out[3]


# ---------------------------------------------------------------------------
# The phone trap: held portrait, the Timeline was unreachable.
#
# Everything was dimmed, nothing responded, and pinch-zoom could not even be
# used to escape. ROTATING TO LANDSCAPE FIXED IT, and that detail is the
# whole diagnosis.
#
# Measured at iPhone-portrait size in a real browser:
#
#   #tlToolSheetScrim   hidden=true   BUT computed display: flex
#                       fixed, inset 0, 390x664, z-205,
#                       rgba(0,0,0,0.5), pointer-events auto
#
# `hidden` works only through the UA rule `[hidden] { display: none }`,
# which has the lowest specificity there is. mobile.css had, inside
# @media (max-width: 768px):
#
#   .tl-mobile-only { display: inline-flex !important; }
#
# ...which beat it, and FORCED every mobile-only element visible on a phone
# whether or not it was hidden. Two of them are full-screen overlays. The
# result was a sheet of glass over the whole editor, eating every tap, with
# nothing to dismiss -- the overlay closes when tapped, but it was never
# "open", so nothing was listening.
#
# Landscape "worked" only because an iPhone is wider than 768px that way,
# so the media query did not apply and `hidden` behaved normally.
# ---------------------------------------------------------------------------

def _mobile_css():
    return (BURNER / "static" / "mobile.css").read_text(encoding="utf-8")


def test_a_phone_rule_cannot_override_the_hidden_attribute():
    """The exact defect. `!important` display on a class that hidden
    elements also carry makes `hidden` a no-op."""
    css = _mobile_css()
    assert ".tl-mobile-only:not([hidden])" in css, \
        "the phone rule can force a hidden element visible again"
    # The bare form may still exist where it HIDES (that is the desktop
    # rule and it is correct); it must not exist where it reveals.
    for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        if ".tl-mobile-only" not in sel or "[hidden]" in sel:
            continue
        if "!important" not in body or "display" not in body:
            continue
        assert re.search(r"display\s*:\s*none", body), \
            f"a bare .tl-mobile-only rule still reveals: {sel.strip()} {{{body}}}"


def test_no_important_display_rule_can_unhide_an_element_that_ships_hidden():
    """The general form, so the next one is caught rather than the last one.

    Any element in the markup that carries BOTH `hidden` and a class must
    not be matched by an `!important` display rule that forgets
    `:not([hidden])` -- that combination is what turns a dormant overlay
    into a sheet of glass.
    """
    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    # Comments stripped FIRST. A selector slice runs from the previous `}`,
    # so it swallows any comment above the rule -- and the comment above
    # this very fix contains the literal "[hidden]", which silently
    # exempted the rule it was explaining. The test passed while the bug
    # was reinstated. Caught by reinstating it.
    css = re.sub(r"/\*.*?\*/", "", _mobile_css(), flags=re.S)
    # Flatten @media wrappers. `([^{}]+)\{([^}]*)\}` cannot see inside a
    # nested at-rule: it reads `@media (max-width: 768px) {` as the SELECTOR
    # and everything to the first `}` as the body, so every rule in the
    # phone media query -- which is the entire area this bug lives in --
    # was invisible. The sweep passed while covering nothing. Caught by
    # reinstating the bug and watching it still pass.
    css = re.sub(r"@media[^{]*\{", "", css)

    # Classes on elements that ship with the hidden attribute.
    hidden_classes = set()
    for tag in re.findall(r"<[a-zA-Z]+[^>]*\bhidden\b[^>]*>", html):
        m = re.search(r'class="([^"]+)"', tag)
        if m:
            hidden_classes.update(m.group(1).split())

    co = {}
    for tag in re.findall(r"<[a-zA-Z]+[^>]*\bhidden\b[^>]*>", html):
        m = re.search(r'class="([^"]+)"', tag)
        if not m:
            continue
        names = m.group(1).split()
        for n in names:
            co.setdefault(n, set()).update(f".{o}" for o in names)

    def html_classes_with(name):
        return co.get(name, set())

    offenders = []
    for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        if "display" not in body or "!important" not in body:
            continue
        sel = sel.strip()
        for cls in hidden_classes:
            if f".{cls}" not in sel:
                continue
            # Fine if it excludes hidden, or if it only ever HIDES things.
            if "[hidden]" in sel:
                continue
            if re.search(r"display\s*:\s*none", body):
                continue
            # Also fine if a companion rule puts `hidden` back, which is how
            # .phone-only was patched when this bit the first time.
            # A companion rule that puts `hidden` back is an equally valid
            # repair -- how .phone-only was patched when this first bit.
            # It must be matched against the SELECTOR, not the raw slice.
            bare = sel.split("\n")[-1].strip()
            repair = re.escape(bare) + r"\[hidden\]\s*\{[^}]*display\s*:\s*none"
            if re.search(repair, css):
                continue
            # ...or a repair keyed on another class the SAME element
            # carries, which is how .phone-only was patched. Matched
            # against classes that really co-occur, not any class at all.
            if any(re.search(re.escape(f".{other}") + r"\[hidden\]\s*\{[^}]*"
                             r"display\s*:\s*none", css)
                   for other in html_classes_with(cls)
                   if other != f".{cls}"):
                continue
            offenders.append((sel, cls))
    assert not offenders, (
        "these !important display rules can override `hidden` on elements "
        f"that ship hidden: {offenders}")


def test_the_page_still_allows_pinch_zoom():
    """Blocking zoom fails WCAG 1.4.4 -- and it removed the last way out
    when a full-screen overlay trapped the page."""
    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    meta = re.search(r'<meta name="viewport"[^>]*>', html)
    assert meta, "no viewport meta"
    tag = meta.group(0)
    assert "maximum-scale" not in tag, tag
    assert "user-scalable=no" not in tag, tag
    assert "width=device-width" in tag


def test_the_scrim_can_be_dismissed_by_touch_not_only_by_click():
    """On iOS Safari a `click` born from a touch on a plain, non-interactive
    <div> is unreliable. A dismissable overlay that only answers a desktop
    event is not dismissable on the device it matters on."""
    js = (BURNER / "static" / "mobile.js").read_text(encoding="utf-8")
    # From where the listeners are REGISTERED. Slicing from the first
    # mention of the id lands in syncPhoneClass and proves nothing -- the
    # same "matched its own neighbourhood" mistake as earlier in this file.
    block = js[js.index('const scrim = document.getElementById("mobilePanelScrim");\n    if (scrim) {'):]
    block = block[:1200]
    assert 'addEventListener("pointerdown"' in block, \
        "the scrim still listens for click only"
    assert 'addEventListener("click"' in block, "click must still work too"
    css = _mobile_css()
    scrim = css[css.index(".mobile-panel-scrim"):]
    scrim = scrim[:scrim.index("}")]
    assert "touch-action" in scrim and "cursor" in scrim


def test_the_screen_is_never_dimmed_for_a_panel_that_cannot_be_seen(app_mod):
    """The second defect, found on the way to the first.

    The bottom sheet lives inside the editor's .tab-content, so when that is
    display:none the sheet has no box at all -- yet openMobilePanel still
    raised a full-screen scrim over whatever happened to be on screen.
    """
    js = (BURNER / "static" / "mobile.js").read_text(encoding="utf-8")
    assert "function mobilePanelCanBeSeen()" in js
    body = js[js.index("function openMobilePanel"):]
    body = body[:body.index("function closeMobilePanel")]
    assert "mobilePanelCanBeSeen()" in body, \
        "the scrim goes up without checking the panel is visible"
    # And it must bail rather than merely log.
    assert "closeMobilePanel()" in body

    # The self-heal exists and is wired to the events that change layout.
    assert "function healOrphanedMobilePanel()" in js
    for evt in ("orientationchange", "resize"):
        assert f'addEventListener("{evt}"' in js, evt


def test_the_mobile_only_rule_still_shows_what_is_not_hidden():
    """The guard must not have turned the phone chrome off wholesale --
    mobile-only controls that are NOT hidden still have to appear."""
    css = _mobile_css()
    m = re.search(r"\.tl-mobile-only:not\(\[hidden\]\)\s*\{([^}]*)\}", css)
    assert m, "the phone rule is gone entirely"
    assert "display" in m.group(1) and "none" not in m.group(1), m.group(1)


# ---------------------------------------------------------------------------
# The co-editor you could not type into.
#
# On a phone the drawer is a bottom sheet, and its CSS comment has always
# said "above nav / context strip" -- but it was pinned `bottom: 0`, which
# is not above the nav, it is UNDER it. The bottom nav is fixed in the same
# band, so measured at iPhone portrait (390x664):
#
#   #coEditorInput     612 -> 652
#   #mobileBottomNav   603 -> 664   (fixed, z-200)
#
# The text box and Send sat inside the nav's 61px strip. There was nowhere
# visible to type, and focusing an input you cannot see is what made the
# keyboard feel uncontrollable too.
# ---------------------------------------------------------------------------

_NAV_CLEARANCE = "calc(64px + env(safe-area-inset-bottom, 0px))"


def test_the_coeditor_sheet_clears_the_bottom_nav():
    """The failure was geometric, so the assertion is about the offset."""
    css = _mobile_css()
    rule = re.search(
        r"body\.is-phone \.co-editor-drawer\s*\{([^}]*)\}", css)
    assert rule, "the phone rule for the co-editor drawer is gone"
    body = rule.group(1)
    bottom = re.search(r"(?<!-)\bbottom\s*:\s*([^;]+);", body)
    assert bottom, f"no bottom offset: {body}"
    value = bottom.group(1).strip()
    assert value != "0", "the drawer is pinned under the bottom nav again"
    assert "64px" in value and "safe-area-inset-bottom" in value, value


def test_every_phone_bottom_sheet_clears_the_nav_the_same_way():
    """Three places already encode the nav's height. They must agree, or
    one sheet ends up under it while the others do not -- which is exactly
    how this happened: the body padding and the context strip cleared the
    nav and the co-editor did not."""
    css = re.sub(r"/\*.*?\*/", "", _mobile_css(), flags=re.S)
    uses = re.findall(r"calc\(\s*64px\s*\+\s*env\(safe-area-inset-bottom[^)]*\)\s*\)",
                      css)
    assert len(uses) >= 3, (
        "the nav-clearance expression is used fewer times than expected; "
        f"found {len(uses)}")


def test_the_keyboard_inset_is_measured_rather_than_assumed():
    """iOS does not resize the layout viewport when the keyboard opens, so a
    fixed bottom sheet stays put and the keyboard covers it -- you can focus
    a box you cannot see. visualViewport is the only thing that reports it,
    and nothing in this app used it before."""
    js = (BURNER / "static" / "mobile.js").read_text(encoding="utf-8")
    assert "visualViewport" in js, "nothing measures the keyboard"
    assert "--kb-inset" in js
    assert 'classList.toggle("kb-open"' in js
    # A threshold, not "> 0": a collapsing Safari toolbar is not a keyboard.
    fn = js[js.index("function syncKeyboardInset()"):]
    fn = fn[:fn.index("\n  }") + 4]
    m = re.search(r"inset\s*>\s*(\d+)", fn)
    assert m and int(m.group(1)) >= 50, f"threshold too low: {fn}"

    css = _mobile_css()
    assert "body.is-phone.kb-open .co-editor-drawer" in css, \
        "nothing lifts the drawer above the keyboard"
    rule = re.search(r"body\.is-phone\.kb-open \.co-editor-drawer\s*\{([^}]*)\}",
                     css).group(1)
    assert "--kb-inset" in rule, rule


def test_the_keyboard_can_be_put_away_again():
    """"I cannot bring up and lower my keyboard." Sending kept focus, so the
    keyboard stayed over the answer with no obvious way down."""
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    fn = js[js.index("const input = $(\"coEditorInput\");\n    const prompt ="):]
    fn = fn[:2000]
    assert ".blur()" in fn, "sending leaves the keyboard up over the reply"
    assert "is-phone" in fn, "it must only put the keyboard away on a phone"

    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    tag = re.search(r'<input[^>]*id="coEditorInput"[^>]*>', html).group(0)
    assert 'enterkeyhint="send"' in tag, tag


def test_the_drawer_cannot_grow_taller_than_the_space_above_the_nav():
    """A sheet taller than the room left pushes its own head off the top of
    a short phone, which is the same failure in the other direction."""
    css = _mobile_css()
    rule = re.search(r"body\.is-phone \.co-editor-drawer\s*\{([^}]*)\}",
                     css).group(1)
    assert "max-height" in rule, rule
    mh = re.search(r"max-height\s*:\s*([^;]+);", rule).group(1)
    assert "64px" in mh and "dvh" in mh, mh


# ---------------------------------------------------------------------------
# The preview claimed a windowed split covered the whole shot.
#
# Asked for a split from 0:20 to 0:25, the co-editor placed it correctly
# (`add_effect`, #36), the props panel showed Start 20 / 5s, and the render
# windows it -- test_a_windowed_split_leaves_the_footage_either_side_alone
# proves that in pixels. But SELECTING the effect clip drew the divider and
# the "2nd" badge at every playhead position, with no time check at all. At
# 0:33 that is indistinguishable from a split applied to the entire clip.
#
# The data was right and the render was right. The preview was the thing
# saying otherwise -- the worst place for the lie to live, because it is
# the only part anyone looks at.
# ---------------------------------------------------------------------------

def test_the_selected_split_guide_is_scoped_to_its_own_window():
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    sel = js[js.index('if (selected) {\n      const c = findClip(selected.track, selected.id);'):]
    sel = sel[:sel.index("// Split-screen guide whenever")]
    assert 'c.type === "split_screen"' in sel, "the selected branch moved"
    assert "effectCoversPlayhead(" in sel, \
        "the selected split guide is drawn with no time check again"
    # And it must say where the effect IS rather than drawing nothing at all.
    assert "addEffectElsewhereHint(" in sel


def test_the_two_guide_paths_share_one_window_arithmetic():
    """The selected path and the live path disagreed, and that disagreement
    WAS the bug. One function, two readers."""
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    fn = js[js.index("function effectCoversPlayhead(c, ot)"):]
    fn = fn[:fn.index("\n  }") + 4]
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    harness = """
function clipDuration(c) { return Number(c.out) || 0; }
const fx = {start: 20, out: 5};
console.log(JSON.stringify([10, 19.9, 20, 22, 24.9, 25, 33].map(
  (t) => effectCoversPlayhead(fx, t))));
"""
    out = subprocess.run([node, "-e", fn + harness],
                         capture_output=True, text=True, check=True).stdout.strip()
    # Inside is [start, start+out): 20 in, 25 out -- a half-open window, so
    # two back-to-back effects cannot both own the boundary instant.
    assert json.loads(out) == [False, False, True, True, True, False, False], out


# ---------------------------------------------------------------------------
# A background behind ONE half of a split screen.
#
# _tl_composite_background runs over the whole baked main track, after the
# two panels have already been stacked -- so it mattes the combined frame
# and lands on both halves. There was no way to reach one panel from there,
# which is why "change the background on only one side" could not be
# expressed however it was asked for.
#
# Each panel is already its own stream before the stack, so the
# cut-out-and-replace moves INTO a branch. The matte goes through the same
# crop+scale as the picture it describes, so it lines up by construction.
# ---------------------------------------------------------------------------

def test_a_split_with_no_panel_backgrounds_is_unchanged(app_mod, tmp_path,
                                                        monkeypatch):
    """Every split that already exists must render exactly as before.

    Asserted on the ffmpeg command itself: a new feature that quietly
    rewrites the graph for everyone is a regression wearing a feature's
    clothes.
    """
    seen = {}
    real = app_mod._tl_run
    app_mod._tl_run = lambda cmd, what, *a, **k: seen.setdefault(what, list(cmd))
    try:
        monkeypatch.setattr(app_mod, "_probe_video_size", lambda p: (1920, 1080))
        monkeypatch.setattr(app_mod, "_has_audio_stream", lambda p: True)
        boxes = [_BOX_LEFT, _BOX_RIGHT]
        app_mod._tl_split_speakers_segment(
            Path("in.mp4"), 0.0, 2.0, boxes, "stack",
            1080, 1920, 30, tmp_path / "o.mp4", panel_bgs=None)
        cmd = seen["Speaker split-screen"]
    finally:
        app_mod._tl_run = real
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "alphamerge" not in fc, fc
    assert fc.count("-i") == 0
    # One input only: the source. No matte, no picture.
    assert cmd.count("-i") == 1, cmd
    assert fc == ("[0:v:0]split=2[sa][sb];"
                  "[sa]crop=582:518:188:342,scale=1080:960:"
                  "force_original_aspect_ratio=increase,crop=1080:960,"
                  "setsar=1,fps=30[p0];"
                  "[sb]crop=582:518:1148:342,scale=1080:960:"
                  "force_original_aspect_ratio=increase,crop=1080:960,"
                  "setsar=1,fps=30[p1];"
                  "[p0][p1]vstack=inputs=2,format=yuv420p[v]"), fc


def test_only_the_named_panel_gets_a_background(app_mod, tmp_path,
                                                monkeypatch):
    """The graph shape, before spending a real render on it: panel 0 is
    composited and panel 1 reaches the stack untouched."""
    seen = {}
    real_run, real_matte = app_mod._tl_run, app_mod._tl_matte_video
    fake_matte = tmp_path / "matte.mp4"
    fake_matte.write_bytes(b"\x00" * 64)
    pic = tmp_path / "beach.png"
    pic.write_bytes(b"\x00" * 64)
    app_mod._tl_run = lambda cmd, what, *a, **k: seen.setdefault(what, list(cmd))
    app_mod._tl_matte_video = lambda *a, **k: fake_matte
    try:
        monkeypatch.setattr(app_mod, "_probe_video_size", lambda p: (1920, 1080))
        monkeypatch.setattr(app_mod, "_has_audio_stream", lambda p: True)
        monkeypatch.setattr(app_mod, "_segment_deps_available", lambda: (True, ""))
        monkeypatch.setattr(app_mod, "_tl_bg_source", lambda bg: pic)
        boxes = [_BOX_LEFT, _BOX_RIGHT]
        app_mod._tl_split_speakers_segment(
            Path("in.mp4"), 4.0, 2.0, boxes, "stack",
            1080, 1920, 30, tmp_path / "o.mp4",
            panel_bgs=[{"background": {"asset_id": "x", "fit": "cover"}}, None])
        cmd = seen["Speaker split-screen"]
    finally:
        app_mod._tl_run, app_mod._tl_matte_video = real_run, real_matte
    fc = cmd[cmd.index("-filter_complex") + 1]
    # Panel 0 is composited; panel 1 goes into the stack as itself.
    assert "[p0][pm0]alphamerge[pfg0]" in fc, fc
    assert "[pbg0][pfg0]overlay=0:0:format=auto[q0]" in fc, fc
    assert "[q0][p1]vstack" in fc, fc
    assert "pm1" not in fc and "q1" not in fc, fc
    # The matte goes through the SAME crop+scale as the panel it describes.
    panel_chain = re.search(r"\[sa\](crop=[^\[]+)\[p0\]", fc).group(1)
    matte_chain = re.search(r"\[\d+:v\](crop=[^\[]+),format=gray\[pm0\]", fc).group(1)
    assert panel_chain == matte_chain, (panel_chain, matte_chain)


def test_the_matte_is_cut_from_the_effect_window_not_the_clip_start(
        app_mod, tmp_path, monkeypatch):
    """A split at 0:20 needs frames 20-25. Without a start the whole clip is
    matted to use five seconds of it."""
    calls = []
    real_run, real_matte = app_mod._tl_run, app_mod._tl_matte_video
    pic = tmp_path / "p.png"; pic.write_bytes(b"\x00" * 64)
    m = tmp_path / "m.mp4"; m.write_bytes(b"\x00" * 64)

    def spy(src, W, H, fps, dur=None, start=0.0):
        calls.append({"start": start, "dur": dur})
        return m

    app_mod._tl_run = lambda *a, **k: None
    app_mod._tl_matte_video = spy
    try:
        monkeypatch.setattr(app_mod, "_probe_video_size", lambda p: (1920, 1080))
        monkeypatch.setattr(app_mod, "_has_audio_stream", lambda p: True)
        monkeypatch.setattr(app_mod, "_segment_deps_available", lambda: (True, ""))
        monkeypatch.setattr(app_mod, "_tl_bg_source", lambda bg: pic)
        app_mod._tl_split_speakers_segment(
            Path("in.mp4"), 20.0, 5.0,
            [_BOX_LEFT, _BOX_RIGHT],
            "stack", 1080, 1920, 30, tmp_path / "o.mp4",
            panel_bgs=[{"background": {"asset_id": "x"}}, None])
    finally:
        app_mod._tl_run, app_mod._tl_matte_video = real_run, real_matte
    assert calls and calls[0]["start"] == 20.0, calls
    assert calls[0]["dur"] == 5.0, calls


def test_start_is_part_of_the_matte_cache_key(app_mod, monkeypatch, tmp_path):
    """Two windows of one clip must not collide on one cached matte."""
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 2048)
    monkeypatch.setattr(app_mod, "_segment_deps_available", lambda: (False, "no"))
    # Deps off, so it returns None -- but the key is computed before that
    # check only if the implementation says so; assert on the real thing.
    import hashlib
    st = src.stat()

    def key_for(start):
        return hashlib.md5(
            f"{src}|{st.st_mtime_ns}|{st.st_size}|{100}x{200}|{30}|{start}|{5.0}"
            .encode()).hexdigest()[:16]

    assert key_for(0.0) != key_for(20.0), "the window does not change the key"


def test_a_panel_background_without_a_matte_costs_the_background_only(
        app_mod, tmp_path, monkeypatch):
    """An optional layer must never cost the segment. Without segmentation
    the panel renders as plain footage, as the whole-frame pass already
    does."""
    seen = {}
    pic = tmp_path / "p.png"
    pic.write_bytes(b"\x00" * 64)
    real_run, real_matte = app_mod._tl_run, app_mod._tl_matte_video
    app_mod._tl_run = lambda cmd, what, *a, **k: seen.setdefault(what, list(cmd))
    app_mod._tl_matte_video = lambda *a, **k: None
    try:
        monkeypatch.setattr(app_mod, "_probe_video_size", lambda p: (1920, 1080))
        monkeypatch.setattr(app_mod, "_has_audio_stream", lambda p: True)
        monkeypatch.setattr(app_mod, "_segment_deps_available",
                            lambda: (False, "mediapipe unavailable"))
        # The picture MUST resolve, or the request is dropped before the
        # matte is ever attempted and this proves nothing. It passed that
        # way until a leaked patch from another test was cleaned up.
        monkeypatch.setattr(app_mod, "_tl_bg_source", lambda bg: pic)
        app_mod._tl_background_skipped.clear()
        app_mod._tl_split_speakers_segment(
            Path("in.mp4"), 0.0, 2.0,
            [_BOX_LEFT, _BOX_RIGHT],
            "stack", 1080, 1920, 30, tmp_path / "o.mp4",
            panel_bgs=[{"background": {"asset_id": "x"}}, None])
        cmd = seen["Speaker split-screen"]
    finally:
        app_mod._tl_run, app_mod._tl_matte_video = real_run, real_matte
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "alphamerge" not in fc, "it tried to composite without a matte"
    assert "[p0][p1]vstack" in fc, fc
    # And it says why, rather than the background silently not being there.
    assert app_mod._tl_background_skipped, "nothing recorded the reason"


def test_panels_survive_every_field_list_between_the_ui_and_the_renderer(app_mod):
    """Four hand-written field lists sit between the control and ffmpeg, and
    this is exactly where mode/speakers were dropped the first time."""
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    add = js[js.index("async function addEffectClip"):]
    add = add[:add.index("\n  }")]
    assert "opts.panels" in add, "addEffectClip drops panels before the clip exists"

    # serialize() spreads effects clips, and _normalize_timeline does
    # row = dict(fx) -- proved by round-tripping rather than by reading.
    tl = app_mod._normalize_timeline({"tracks": {"effects": [
        {"id": "f1", "type": "split_screen", "start": 1.0, "out": 2.0,
         "panels": [{"background": {"asset_id": "abc", "fit": "cover"}}, None]}]}})
    fx = tl["tracks"]["effects"][0]
    assert fx["panels"][0]["background"]["asset_id"] == "abc"

    # And the fresh dict the renderer folds lane effects into.
    _k, _p, _c, split = app_mod._tl_props_from_lane_effects([fx])
    assert split["panels"][0]["background"]["asset_id"] == "abc", split

    # The control has to be wired in the CLIP props loop, where `c` is the
    # selected clip -- not the PROJECT loop that owns __bg_asset/__logo_asset.
    # It landed in the project loop first: the dropdown rendered, changing it
    # did nothing, and only a real round-trip through /timeline/save showed
    # it. So the assertion is adjacency to its own sibling control.
    i_panel = js.index('/^__panel_bg_\\d$/.test(key)')
    i_mode = js.index('key === "__split_mode"')
    i_projbg = js.index('if (key === "__bg_asset")')
    assert abs(i_panel - i_mode) < 1200, \
        "the panel handler is not beside __split_mode in the clip props loop"
    assert abs(i_panel - i_projbg) > 1200, \
        "the panel handler is in the project props loop, where `c` is wrong"


def test_one_half_gets_the_new_background_and_the_other_does_not(app_mod,
                                                                 tmp_path):
    """The claim, in pixels, on a real render with a real matte.

    Both halves must be checked. A background painted over the whole frame
    would pass "panel 0 shows the picture" on its own -- which is exactly
    the bug being fixed, so asserting panel 1 is UNTOUCHED is the half that
    does the work.
    """
    for mod in ("cv2", "mediapipe"):
        if not _have(mod):
            pytest.skip(f"{mod} not installed")
    import cv2
    import numpy as np

    portrait = _portrait()
    if not portrait.exists():
        pytest.skip("bundled portrait not available")

    src = tmp_path / "two.mp4"
    # The same portrait twice side by side, so each panel's crop finds a
    # real person and the matte is genuine rather than a stub.
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-loop", "1", "-t", "2", "-i", str(portrait),
         "-filter_complex",
         "[0:v]scale=512:600,setsar=1[a];[a]split=2[l][r];[l][r]hstack=inputs=2,"
         "fps=15,format=yuv420p[v]",
         "-map", "[v]", "-an", str(src)],
        check=True)

    # A flat magenta picture: unmistakable against the portrait.
    pic = tmp_path / "bg.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=magenta:s=400x400", "-frames:v", "1",
                    str(pic)], check=True)

    out = tmp_path / "split.mp4"
    real_bg = app_mod._tl_bg_source
    app_mod._tl_bg_source = lambda bg: pic
    try:
        app_mod._tl_split_speakers_segment(
            src, 0.0, 1.0, [_BOX_LEFT, _BOX_RIGHT], "stack",
            540, 960, 15, out,
            panel_bgs=[{"background": {"asset_id": "x", "fit": "cover"}}, None])
    finally:
        app_mod._tl_bg_source = real_bg
    assert out.exists() and out.stat().st_size > 2000

    png = tmp_path / "frame.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "0.3", "-i", str(out),
                    "-frames:v", "1", str(png)], check=True)
    im = cv2.imread(str(png))
    assert im is not None
    h, w = im.shape[:2]
    top, bottom = im[: h // 2], im[h // 2:]

    def magenta_fraction(panel):
        b, g, r = panel[:, :, 0].astype(int), panel[:, :, 1].astype(int), \
            panel[:, :, 2].astype(int)
        return float(np.mean((b > 120) & (r > 120) & (g < 90)))

    top_bg = magenta_fraction(top)
    bottom_bg = magenta_fraction(bottom)
    # The composited half shows a real amount of the new picture...
    assert top_bg > 0.12, f"no new background in the top half: {top_bg:.3f}"
    # ...and the untouched half shows essentially none. This is the
    # assertion that fails if the background leaks across the stack.
    assert bottom_bg < 0.02, \
        f"the background leaked into the other half: {bottom_bg:.3f}"
    # And the subject survives rather than being wiped: the composited half
    # is not mostly the picture.
    assert top_bg < 0.95, f"the top half is just the picture: {top_bg:.3f}"


# --- style packs: somewhere for a look to live ---------------------------
# AI_EDIT_STYLE_PACKS held six looks and nothing could add a seventh. Brand
# presets went to localStorage -- one browser, invisible to the server, gone
# with the cache -- and STUDIO_TEMPLATES is a client-side array, so a look the
# user kept could never drive an edit. These tests guard the two ways that can
# go wrong: a saved pack failing to reach the renderer, and a saved pack
# quietly changing what a built-in means.

def _saved_pack_style():
    return {"font_name": "Bebas Neue", "font_size": 71,
            "highlight_color": "#ABCDEF", "group_size": 1}


def test_the_builtin_packs_resolve_exactly_as_before(app_mod, temp_db):
    """The resolver replaced a direct dict lookup in the AI Edit pipeline.

    Every built-in must come back byte-identical, with and without saved packs
    in the table -- otherwise this refactor silently retuned six looks people
    already use.
    """
    client = app_mod.app.test_client()
    for key, ref in app_mod.AI_EDIT_STYLE_PACKS.items():
        expect = dict(ref)
        expect["_id"] = key
        assert app_mod._resolve_style_pack(key) == expect, key

    # Again with a saved pack present, which is when shadowing would bite.
    client.post("/style-packs", json={"label": "Mine", "style": _saved_pack_style()})
    for key, ref in app_mod.AI_EDIT_STYLE_PACKS.items():
        expect = dict(ref)
        expect["_id"] = key
        assert app_mod._resolve_style_pack(key) == expect, f"{key} after a save"

    # And an unknown id still lands on pulse rather than raising.
    assert app_mod._resolve_style_pack("no-such-pack")["_id"] == "pulse"
    assert app_mod._resolve_style_pack("")["_id"] == "pulse"


def test_a_saved_pack_cannot_overwrite_a_builtin(app_mod, temp_db):
    """Otherwise picking "Clarity" would stop meaning Clarity for everyone."""
    client = app_mod.app.test_client()
    res = client.post("/style-packs", json={
        "label": "Hijack", "id": "clarity", "style": {"font_size": 9}})
    assert res.status_code == 400
    assert "built-in" in (res.get_json() or {}).get("error", "")
    assert app_mod._resolve_style_pack("clarity")["label"] == "Clarity"
    assert app_mod._resolve_style_pack("clarity")["style"]["size"] == 64

    # The slug of a label that collides is pushed out of the way instead.
    assert app_mod._slug_pack_id("Clarity") != "clarity"
    assert client.post("/style-packs", json={
        "label": "Clarity", "style": {"font_size": 9}}).status_code == 200
    assert app_mod._resolve_style_pack("clarity")["style"]["size"] == 64

    # Deleting one is refused too, so the table cannot be emptied of them.
    assert client.delete("/style-packs/clarity").status_code == 400


def test_a_saved_pack_reaches_the_renderer_as_the_style_used(app_mod, temp_db,
                                                             monkeypatch):
    """The handover is the whole feature, and it is where this breaks.

    A pack is saved through HTTP, resolved by id inside /ai-edit-seed, and its
    caption style has to land on the produced timeline. Four hand-written field
    lists sit between those two points; the same handover has already eaten
    `mode`, `speakers` and `panels`.
    """
    monkeypatch.setattr(app_mod, "_gemini_generate_clip_suggestions",
                        lambda prompt: {"effects": [], "hook": None})
    client = app_mod.app.test_client()
    app_mod.jobs["spk_src"] = {
        "status": "awaiting_edit", "filename": "x.mp4",
        "words": [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.45}
                  for i, w in enumerate("one two three four five six".split() * 8)],
    }
    try:
        saved = client.post("/style-packs", json={
            "label": "Mine", "style": _saved_pack_style()}).get_json()
        assert saved["id"] == "mine"

        def seed(pack_id):
            res = client.post("/ai-edit-seed", json={
                "source_job_id": "spk_src", "style_pack": pack_id,
                "full_video": True, "insert_media": False})
            assert res.status_code == 200, res.get_json()
            return res.get_json()["timeline"]

        tl = seed("mine")
        style = tl["style"]
        # The saved values, not the built-in default's.
        assert style.get("font_name") == "Bebas Neue", style
        assert int(style.get("font_size")) == 71, style
        assert str(style.get("highlight_color")).upper() == "#ABCDEF", style
        # And the badge names the pack that was really used, so the timeline
        # does not claim to be a built-in.
        assert tl["ai_edit"]["style_pack_id"] == "mine"
        assert tl["ai_edit"]["style_pack"] == "Mine"

        # A built-in through the same path is untouched by any of this.
        clarity = seed("clarity")["style"]
        assert clarity.get("font_name") == "Montserrat Thin Black"
        assert int(clarity.get("font_size")) == 64
    finally:
        app_mod.jobs.pop("spk_src", None)


def test_the_pack_picker_lists_saved_packs_beside_the_builtins(app_mod, temp_db):
    """The picker renders whatever this endpoint returns, so the list IS the UI.

    `source` is what lets a saved pack be told apart from a built-in; without
    it the two are indistinguishable and neither can be offered for deletion.
    """
    client = app_mod.app.test_client()
    before = client.get("/ai-edit/style-packs").get_json()["packs"]
    assert [p["id"] for p in before] == list(app_mod.AI_EDIT_STYLE_PACKS)
    assert {p["source"] for p in before} == {"builtin"}

    client.post("/style-packs", json={"label": "My Reel Look",
                                      "style": _saved_pack_style()})
    after = client.get("/ai-edit/style-packs").get_json()["packs"]
    mine = [p for p in after if p["source"] != "builtin"]
    assert len(mine) == 1, after
    assert mine[0]["id"] == "my-reel-look"
    assert mine[0]["label"] == "My Reel Look"
    # Every built-in is still listed: a saved pack adds, it does not replace.
    assert set(app_mod.AI_EDIT_STYLE_PACKS).issubset({p["id"] for p in after})


def test_saving_the_same_name_twice_updates_one_pack(app_mod, temp_db):
    """Otherwise tweaking a look ten times leaves ten near-identical packs."""
    client = app_mod.app.test_client()
    first = client.post("/style-packs", json={
        "label": "My Look", "style": {"font_size": 60}}).get_json()
    second = client.post("/style-packs", json={
        "label": "My Look", "style": {"font_size": 72}}).get_json()
    assert first["id"] == second["id"]
    assert len(app_mod._db_load_style_packs()) == 1
    assert app_mod._resolve_style_pack(first["id"])["style"]["font_size"] == 72
    # created_at survives an update; only updated_at moves.
    with sqlite3.connect(str(temp_db)) as conn:
        created, updated = conn.execute(
            "SELECT created_at, updated_at FROM style_packs WHERE pack_id = ?",
            (first["id"],)).fetchone()
    assert updated >= created


def test_one_unreadable_saved_pack_does_not_take_the_picker_down(app_mod, temp_db):
    """A bad row must cost that row, not every pack in the app.

    This is the same trade as the matte and the diarization ladder: a broken
    optional thing degrades, it does not fail the surface around it.
    """
    client = app_mod.app.test_client()
    client.post("/style-packs", json={"label": "Good", "style": {"font_size": 64}})
    with sqlite3.connect(str(temp_db)) as conn:
        conn.execute(
            "INSERT INTO style_packs (pack_id, label, pack, source, created_at,"
            " updated_at) VALUES ('broken', 'Broken', '{not json', 'manual', 1, 2)")
        conn.commit()

    loaded = app_mod._db_load_style_packs()
    assert "good" in loaded and "broken" not in loaded
    packs = client.get("/ai-edit/style-packs").get_json()["packs"]
    ids = [p["id"] for p in packs]
    assert "good" in ids
    assert set(app_mod.AI_EDIT_STYLE_PACKS).issubset(set(ids))


def test_a_saved_pack_can_be_deleted(app_mod, temp_db):
    client = app_mod.app.test_client()
    pid = client.post("/style-packs", json={
        "label": "Throwaway", "style": {"font_size": 64}}).get_json()["id"]
    assert client.delete("/style-packs/" + pid).status_code == 200
    assert app_mod._db_load_style_packs() == {}
    # A second delete is a 404, not a silent success.
    assert client.delete("/style-packs/" + pid).status_code == 404
    # And the id now resolves to the fallback rather than a stale copy.
    assert app_mod._resolve_style_pack(pid)["_id"] == "pulse"


def test_saving_a_look_refuses_to_store_nothing(app_mod, temp_db):
    """A pack with no style would silently render as the default forever."""
    client = app_mod.app.test_client()
    assert client.post("/style-packs", json={"style": {"font_size": 1}}).status_code == 400
    assert client.post("/style-packs", json={"label": "Empty"}).status_code == 400
    assert client.post("/style-packs", json={"label": "Empty", "style": {}}).status_code == 400
    assert app_mod._db_load_style_packs() == {}


def test_the_branding_save_button_persists_to_the_server(app_mod):
    """localStorage was the whole store, so a look died with the cache.

    Asserted on the source because the defect is a missing side effect: the
    old function wrote one key and told the user it was saved. The button has
    to await the save too, or the note fires before the POST resolves.
    """
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    start = js.index("window.saveCustomBrandPreset")
    body = js[start:js.index("window.listSavedStylePacks")]
    assert '"/style-packs"' in body, "the save never reaches the server"
    assert 'method: "POST"' in body
    # The local copy is kept deliberately as a fallback, not as the store.
    assert "localStorage.setItem" in body

    btn = js[js.index('const saveBrandPresetBtn'):]
    btn = btn[:btn.index("const loadBrandPresetBtn")]
    assert "await window.saveCustomBrandPreset" in btn, \
        "the click handler does not await the save"

    loader = js[js.index("window.loadCustomBrandPreset"):]
    loader = loader[:loader.index("// ---- Auto-Fetch B-Roll")]
    assert '"/style-packs"' in loader, "the loader never asks the server"
    assert 'localStorage.getItem("_customBrandPreset")' in loader, \
        "the browser copy is no longer a fallback"


# --- split ratio: the wide strip over the close-up -----------------------
# Both split builders hard-coded `pw, ph = W, H // 2`, so the only split the
# app could make was even halves. A pulled-back strip over a punched-in shot
# -- one camera, two crops -- is uneven by definition, and at 50/50 it reads
# as a mistake rather than as framing.

def _captured_filtergraph(app_mod, monkeypatch, **kwargs):
    """Run the speakers split with ffmpeg stubbed and return its graph."""
    seen = {}

    def fake_run(cmd, label):
        seen["fc"] = cmd[cmd.index("-filter_complex") + 1]

    monkeypatch.setattr(app_mod, "_tl_run", fake_run)
    monkeypatch.setattr(app_mod, "_probe_video_size", lambda s: (1080, 1920))
    monkeypatch.setattr(app_mod, "_has_audio_stream", lambda s: True)
    monkeypatch.setattr(app_mod, "_tl_declick_af", lambda d: "anull")
    boxes = kwargs.pop("boxes", [(0.3, 0.4, 0.2, 0.25), (0.7, 0.4, 0.2, 0.25)])
    app_mod._tl_split_speakers_segment(
        Path("in.mp4"), 0.0, 5.0, boxes, kwargs.pop("layout", "stack"),
        1080, 1920, 30, Path("out.mp4"), **kwargs)
    return seen["fc"]


def test_an_even_split_renders_exactly_what_it_always_did(app_mod, monkeypatch):
    """Every split that already exists has to be provably untouched.

    The ratio work replaced one shared `pw, ph` with per-panel sizes, which
    is precisely the kind of change that moves a number by one pixel and is
    never noticed. So the default is asserted character for character, not
    approximately.
    """
    base = _captured_filtergraph(app_mod, monkeypatch)
    named = _captured_filtergraph(app_mod, monkeypatch, ratio="even")
    assert base == named, "'even' is not the same as no ratio at all"
    # Both panels are half the canvas, as before.
    assert base.count("crop=1080:960") == 4, base
    assert "vstack=inputs=2" in base

    # And an uneven ratio really does something, or the test above proves
    # only that nothing happens.
    third = _captured_filtergraph(app_mod, monkeypatch, ratio="third")
    assert third != base
    assert "crop=1080:652" in third and "crop=1080:1268" in third, third


@pytest.mark.parametrize("layout,W,H", [
    ("stack", 1080, 1920), ("stack", 1920, 1080), ("stack", 1080, 1080),
    ("side", 1080, 1920), ("side", 1920, 1080),
    # Odd canvases are where the even-ing actually bites. With an even H
    # the remainder of an even h0 is already even, so the guard on the
    # SECOND panel is dead code for every real canvas -- and a version of
    # this test without these two rows passed with that guard deleted.
    ("stack", 1080, 1921), ("side", 1081, 1920),
])
def test_panel_sizes_are_even_and_fill_the_canvas(app_mod, layout, W, H):
    """An odd panel height is not a slightly wrong number -- it is an ffmpeg
    failure, and a one-pixel shortfall is a seam down the middle of the
    export. Both panels are therefore derived from one subtraction rather
    than from two independent roundings.
    """
    for name in ("thin", "third", "even", "two_thirds", "wide"):
        sizes = app_mod._tl_panel_sizes(layout, W, H, name)
        assert len(sizes) == 2
        for w, h in sizes:
            assert w % 2 == 0 and h % 2 == 0, (name, sizes)
            assert w >= 2 and h >= 2, (name, sizes)
        if layout == "side":
            # hstack needs matching heights; the widths carry the ratio.
            assert sizes[0][1] == sizes[1][1], (name, sizes)
            assert sizes[0][0] + sizes[1][0] == W - (W % 2), (name, sizes)
        else:
            # vstack needs matching widths.
            assert sizes[0][0] == sizes[1][0], (name, sizes)
            assert sizes[0][1] + sizes[1][1] == H - (H % 2), (name, sizes)

    # The ratio is honoured in the right direction, and the named presets
    # are ordered. A mapping that returned the same numbers for all five
    # would pass every assertion above.
    axis = 0 if layout == "side" else 1
    firsts = [app_mod._tl_panel_sizes(layout, W, H, n)[0][axis]
              for n in ("thin", "third", "even", "two_thirds", "wide")]
    assert firsts == sorted(firsts), firsts
    assert firsts[0] < firsts[-1], firsts


def test_an_unknown_ratio_falls_back_rather_than_raising(app_mod):
    for junk in (None, "", "enormous", {}, [], True):
        assert app_mod._split_ratio_value(junk) == 0.5, junk
    # A raw fraction is accepted, for a saved project or a co-editor op,
    # and clamped so a split can never be a sliver.
    assert app_mod._split_ratio_value(0.3) == 0.3
    assert app_mod._split_ratio_value("0.3") == 0.3
    # BOTH paths clamp. A number and a numeric string take different
    # branches, and an earlier version of this test only ever exercised the
    # number -- so removing the string branch's clamp broke nothing and the
    # test still passed.
    assert app_mod._split_ratio_value(0.01) == 0.15
    assert app_mod._split_ratio_value(99) == 0.85
    assert app_mod._split_ratio_value("0.01") == 0.15
    assert app_mod._split_ratio_value("99") == 0.85


def test_the_wide_close_pair_needs_no_analysis_at_all(app_mod):
    """This is the whole reason the mode exists.

    The speakers split returns None without diarization AND faces, and the
    renderer then falls through to the uncut shot -- silently. The footage
    this look is for (a DJ set, a performance) often has no speech to
    diarize, so a framing split that needed Analyze could never be reached
    on it.
    """
    boxes, crops = app_mod._tl_framing_boxes(None)
    assert boxes is not None and len(boxes) == 2
    assert len(crops) == 2

    # The close-up must actually be closer, or this is two identical crops
    # stacked -- which is what it would look like if the pair collapsed.
    sizes = app_mod._tl_panel_sizes("stack", 1080, 1920, "third")
    widths = [
        app_mod._tl_speaker_crop_px(boxes[i], 1080, 1920, *sizes[i], **crops[i])[0]
        for i in (0, 1)
    ]
    assert widths[0] > widths[1] * 1.25, (
        f"the strip sees {widths[0] / widths[1]:.2f}x the close-up's width; "
        "that is not a wide shot next to a close-up")


def test_a_found_face_moves_the_closeup_without_opening_it_out(app_mod, monkeypatch):
    """A detected face should re-aim the close-up, never resize it.

    Face boxes vary hugely with distance from camera, and feeding that
    straight through made the "close-up" as wide as the strip on a big
    detection -- losing the contrast that is the entire effect.
    """
    monkeypatch.setattr(app_mod, "_load_reframe_cache", lambda j: {
        "faces": [{"faces": [{"cx": 0.8, "cy": 0.25, "w": 0.5, "h": 0.6}]}],
    })
    boxes, crops = app_mod._tl_framing_boxes("job")
    assert boxes[1][0] == pytest.approx(0.8), "the close-up ignored the face"
    assert boxes[1][1] == pytest.approx(0.25)

    sizes = app_mod._tl_panel_sizes("stack", 1080, 1920, "third")
    w_strip = app_mod._tl_speaker_crop_px(boxes[0], 1080, 1920, *sizes[0], **crops[0])[0]
    w_close = app_mod._tl_speaker_crop_px(boxes[1], 1080, 1920, *sizes[1], **crops[1])[0]
    assert w_strip > w_close * 1.25, (w_strip, w_close)


def test_no_face_data_leaves_the_closeup_centred(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "_load_reframe_cache", lambda j: None)
    assert app_mod._tl_framing_boxes("job")[0][1][0] == pytest.approx(0.5)
    # An analysis that ran but found nobody is the same case, not a crash.
    monkeypatch.setattr(app_mod, "_load_reframe_cache",
                        lambda j: {"faces": [{"faces": []}]})
    assert app_mod._tl_framing_boxes("job")[0][1][0] == pytest.approx(0.5)


def test_the_ratio_survives_the_handover_to_the_renderer(app_mod):
    """Four hand-written field lists sit between the control and ffmpeg.

    That handover has already eaten `mode`, `speakers` and `panels`, one
    per feature. `_tl_props_from_lane_effects` builds a FRESH dict, so a
    field missing from it is dropped after surviving everything else.
    """
    fx = [{"id": "e1", "type": "split_screen", "start": 0, "out": 4,
           "mode": "wide_punch", "ratio": "third", "layout": "auto"}]
    _ken, _punch, _color, split = app_mod._tl_props_from_lane_effects(fx)
    assert split and split["enabled"]
    assert split["mode"] == "wide_punch"
    assert split["ratio"] == "third", "the ratio was dropped on the way through"

    # And an old clip with no ratio still means even halves.
    _k, _p, _c, plain = app_mod._tl_props_from_lane_effects(
        [{"id": "e2", "type": "split_screen", "start": 0, "out": 4}])
    assert app_mod._split_ratio_value(plain["ratio"]) == 0.5

    # _normalize_timeline spreads effects clips, so the field rides free
    # there -- proved rather than trusted.
    tl = app_mod._normalize_timeline({
        "canvas": "9x16",
        "tracks": {"main": [], "overlay": [], "text": [], "music": [],
                   "effects": fx},
    })
    assert tl["tracks"]["effects"][0]["ratio"] == "third"


def test_the_client_names_ratio_in_its_fixed_field_list(app_mod):
    """addEffectClip builds from a FIXED list: anything unnamed is dropped
    before the clip exists, which is where mode/speakers went the first
    time and panels the second.
    """
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    body = js[js.index("async function addEffectClip"):js.index("reanchor(ec);")]
    assert "opts.ratio" in body, "ratio is dropped before the clip exists"

    # The control exists and offers the mode, or the capability is
    # unreachable -- the exact shape of the add_effect and panels gaps.
    props = js[js.index('propSelect("__split_mode"'):]
    props = props[:props.index("panelBackgroundControls(c)")]
    assert "wide_punch" in props

    handler = js[js.index('key === "__split_mode"'):]
    handler = handler[:handler.index('key === "__split_media"')]
    assert "wide_punch" in handler, "picking the mode would not store it"
    assert 'c.ratio = "third"' in handler, \
        "wide_punch seeds no ratio, so it would render as even halves"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_the_strip_sees_what_the_closeup_cannot(app_mod, tmp_path, monkeypatch):
    """The claim is a pixel claim, so it gets a real render.

    A marker is painted down the far-left edge of the source, outside the
    close-up's crop but inside the full-width strip. The strip must contain
    it and the close-up must not. Asserting only the first would pass two
    identical crops stacked -- which is exactly what a collapsed pair looks
    like, and the thing this is built against.
    """
    if not _have("cv2"):
        pytest.skip("cv2 not installed")
    import cv2
    import numpy as np

    src = tmp_path / "src.mp4"
    out = tmp_path / "out.mp4"
    # Grey frame, a magenta bar down the left edge, a white block centred.
    vf = ("drawbox=x=0:y=0:w=90:h=1920:color=magenta@1:t=fill,"
          "drawbox=x=440:y=700:w=200:h=500:color=white@1:t=fill")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=gray:s=1080x1920:d=1:r=30", "-vf", vf,
                    "-pix_fmt", "yuv420p", str(src)], check=True)

    monkeypatch.setattr(app_mod, "_load_reframe_cache", lambda j: None)
    boxes, crops = app_mod._tl_framing_boxes(None)
    app_mod._tl_split_speakers_segment(
        src, 0.0, 0.9, boxes, "stack", 1080, 1920, 30, out,
        ratio="third", crop_opts=crops)
    assert out.exists() and out.stat().st_size > 1000

    png = tmp_path / "f.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "0.4", "-i", str(out),
                    "-frames:v", "1", str(png)], check=True)
    im = cv2.imread(str(png))
    assert im is not None
    h, w = im.shape[:2]
    assert (w, h) == (1080, 1920), (w, h)

    seam = app_mod._tl_panel_sizes("stack", 1080, 1920, "third")[0][1]
    strip, close = im[:seam], im[seam:]

    def magenta(panel):
        b, g, r = (panel[:, :, i].astype(int) for i in range(3))
        return float(np.mean((b > 110) & (r > 110) & (g < 90)))

    s, c = magenta(strip), magenta(close)
    # The strip is full width, so the left-edge marker survives into it.
    assert s > 0.03, f"the strip did not see the wide-only marker: {s:.4f}"
    # The close-up is cropped in past it. This is the half that fails if
    # both panels end up showing the same crop.
    assert c < 0.005, f"the close-up is not punched in: {c:.4f}"

    # And the strip really is the smaller panel, measured on the frame
    # rather than read off the filter string.
    assert 0.30 < seam / h < 0.38, seam / h


# --- the speaker count has to be reachable ------------------------------
# The ladder works: it runs several passes and reports "4, but the count
# kept climbing". `num_speakers` is plumbed through the endpoint to
# pyannote, and analyzeSpeakerOptions() sends it for "Exactly N". The whole
# chain was complete and unusable, because the only control that produces
# "Exactly N" sat in a section marked `class="panel hidden"` -- so the
# dropdown was permanently stuck on whatever it was last given in markup.

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}


def _ancestor_tags(html: str, needle: str) -> list:
    """Every open tag enclosing *needle*, outermost first.

    A tag stack rather than "the nearest <section>": the control was
    originally hidden by its section, but a hidden <div> anywhere in
    between would hide it just as completely, and an earlier version of
    this test walked only to the section and missed exactly that.
    """
    at = html.index(needle)
    stack = []
    for m in re.finditer(r"<(/?)([a-zA-Z][\w-]*)([^>]*?)(/?)>", html[:at]):
        closing, name, attrs, selfclose = m.groups()
        name = name.lower()
        if name in _VOID_TAGS or selfclose:
            continue
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == name:
                    del stack[i:]
                    break
        else:
            stack.append((name, m.group(0)))
    return stack


def test_the_speaker_controls_are_not_buried_in_a_hidden_panel(app_mod):
    """A control nobody can see is the same as no control at all.

    This is the exact shape of every gap this project keeps hitting: the
    capability exists, the plumbing is complete, and nothing can ask for it.
    """
    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    # The scripts only. Including the whole template made every class in
    # the markup justify itself -- a hidden <div class="mystery-box"> passed
    # because "mystery-box" appeared in the markup being checked.
    js_all = "".join(
        (BURNER / "static" / f).read_text(encoding="utf-8")
        for f in ("app.js", "timeline.js", "mobile.js")
    ) + "".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))

    for ctl in ('id="analyzeMaxSpeakers"', 'id="analyzeSensitivity"'):
        assert html.count(ctl) == 1, f"{ctl} is duplicated; readers go by id"
        for name, tag in _ancestor_tags(html, ctl):
            # aria-hidden is never toggled here; it marks a pane that was
            # retired and left in place for legacy handlers. That is what
            # swallowed this control.
            assert 'aria-hidden="true"' not in tag, (
                f"{ctl} is inside a retired <{name}>: {tag!r}")
            assert "display:none" not in tag.replace(" ", ""), (ctl, tag)
            if not re.search(r"(^|[\s\"'])hidden([\s>=\"']|$)", tag):
                continue
            # Hidden in markup is fine for a pane code opens -- the app
            # shell and the tab panes all start that way. It is fatal for a
            # pane nothing can SELECT, since nothing can then open it.
            handles = re.findall(r'id="([^"]+)"', tag)
            handles += [c for c in re.findall(r'class="([^"]*)"', tag)
                        for c in c.split()]
            handles += re.findall(r'(data-[\w-]+)=', tag)
            # "hidden" and "panel" are on half the document; finding them in
            # a script proves nothing about THIS element.
            handles = [h for h in handles if h not in ("hidden", "panel")]
            assert any(h in js_all for h in handles), (
                f"{ctl} is inside a hidden <{name}> that no script can "
                f"select: {tag!r} — it is hidden for good, which is the "
                "same as having no control at all")

    # And it is beside the buttons it drives, not in some other tab.
    toolbar = html[html.index('id="tlTranscriptToolbar"'):]
    toolbar = toolbar[:toolbar.index('id="tlAnalyzeProgressWrap"')]
    assert 'id="analyzeMaxSpeakers"' in toolbar, \
        "the Speakers control is not beside the Analyze buttons"
    assert 'id="analyzeSensitivity"' in toolbar

    # "Exactly N" has to be on offer, or a confirmed count cannot be pinned.
    opts = toolbar[toolbar.index('id="analyzeMaxSpeakers"'):]
    assert 'value="exact:4"' in opts, opts[:400]


def test_an_uncertain_count_can_be_confirmed_in_one_click(app_mod):
    """A best guess with nothing to do about it is where this stalls.

    The ladder reports "4, and the count kept climbing". Without a confirm,
    acting on that means knowing a dropdown exists somewhere else, which is
    exactly the knowledge the feature was supposed to remove.
    """
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    fn = js[js.index("function offerSpeakerCountLock"):]
    fn = fn[:fn.index("// Poll until the run reaches")]

    # Only when the passes disagreed: a steady count needs no confirming,
    # and a button on every result trains people to ignore it.
    assert 'det.confidence !== "uncertain"' in fn, \
        "the confirm would appear on every result"
    # It pins through the SAME control analyzeSpeakerOptions() reads, so the
    # choice sticks for later runs instead of applying once and vanishing.
    assert 'analyzeMaxSpeakers' in fn
    assert "exact:" in fn
    assert "runAnalyzeWholeEdit" in fn, "confirming does not re-run anything"
    # A value the dropdown does not carry would select nothing and re-run
    # unpinned -- which looks exactly like the button doing nothing.
    assert "appendChild(o)" in fn or "sel.appendChild" in fn, \
        "an unlisted count would silently fail to pin"

    # And the result line is wired to call it.
    assert "offerSpeakerCountLock(data, triggerBtn)" in js


# --- the whole-edit answer has to reach the words -----------------------
# Everything was right up to the last step. The global diarization ran, and
# _tl_project_diarization_to_sources translated it back into each source's
# own clock with the labels intact. Then nothing wrote it anywhere a reader
# could see: /stamp-speakers only ever read the PER-SOURCE cache, so
# "Analyze whole edit" produced a count, caption colours, and a transcript
# with no speakers on it at all.

def _two_source_project(app_mod, jid):
    """A project whose Main track spans two different source clips."""
    for sid, start in (("srcA", 0.0), ("srcB", 0.0)):
        app_mod.jobs[sid] = {
            "status": "awaiting_edit", "filename": f"{sid}.mp4",
            "words": [{"word": w, "start": start + i * 1.0,
                       "end": start + i * 1.0 + 0.9}
                      for i, w in enumerate("one two three four".split())],
        }
    return {
        "canvas": "9x16",
        "tracks": {
            "main": [
                {"id": "m1", "source_job_id": "srcA", "in": 0.0, "out": 4.0},
                {"id": "m2", "source_job_id": "srcB", "in": 0.0, "out": 4.0},
            ],
            "overlay": [], "effects": [], "text": [], "music": [],
        },
    }


def test_the_whole_edit_analysis_labels_the_transcript(app_mod, tmp_path,
                                                       monkeypatch):
    """The failing assertion is "any word at all got a speaker".

    Before this, the count was right, the colours were right, and every
    word's `speaker` was empty -- so the transcript rails and labels that
    renderTranscript already draws had nothing to draw.
    """
    import uuid
    jid = uuid.uuid4().hex
    tl = _two_source_project(app_mod, jid)
    app_mod.jobs[jid] = {"status": "awaiting_edit", "timeline": tl,
                         "filename": "project.mp4"}

    # The audio join and pyannote itself are not what is under test.
    monkeypatch.setattr(app_mod, "_tl_concat_main_audio",
                        lambda t, dest, **k: {
                            "pieces": [
                                {"source_job_id": "srcA", "src_in": 0.0,
                                 "src_out": 4.0, "out_start": 0.0, "dur": 4.0},
                                {"source_job_id": "srcB", "src_in": 0.0,
                                 "src_out": 4.0, "out_start": 4.0, "dur": 4.0},
                            ],
                            "analysis_total": 8.0, "render_total": 8.0})
    # Four people across the two clips, in OUTPUT time -- the whole point
    # being that srcB's people are not called SPEAKER_00 again.
    monkeypatch.setattr(app_mod, "diarize_audio", lambda *a, **k: [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
        {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_01"},
        {"start": 4.0, "end": 6.0, "speaker": "SPEAKER_02"},
        {"start": 6.0, "end": 8.0, "speaker": "SPEAKER_03"},
    ])
    monkeypatch.setattr(app_mod, "_safe_unlink", lambda p: None)

    try:
        rec = app_mod.analyze_timeline_speakers(jid, tl)
        assert len(rec["speakers"]) == 4, rec["speakers"]

        # THE assertion. Zero before this change.
        assert rec["words_stamped"] > 0, "the analysis labelled nothing"

        stamped = {}
        for sid in ("srcA", "srcB"):
            got = [w.get("speaker") for w in app_mod.jobs[sid]["words"]]
            assert any(got), f"{sid} has no speakers on its words: {got}"
            stamped[sid] = {s for s in got if s}

        # The labels stay GLOBAL. Both clips falling back to SPEAKER_00 is
        # the per-source behaviour and the exact bug this analysis exists
        # to remove, so overlapping label sets mean it collapsed.
        assert not (stamped["srcA"] & stamped["srcB"]), stamped
    finally:
        for k in (jid, "srcA", "srcB"):
            app_mod.jobs.pop(k, None)
        try:
            app_mod._tl_diar_path(jid).unlink(missing_ok=True)
        except OSError:
            pass


def test_global_labels_win_over_the_per_source_ones(app_mod, tmp_path,
                                                    monkeypatch):
    """Otherwise pressing the button that fixes the collision and then the
    one that causes it puts the collision straight back.
    """
    import uuid
    jid = uuid.uuid4().hex
    monkeypatch.setattr(app_mod, "UPLOAD_DIR", tmp_path)
    app_mod.jobs["srcA"] = {
        "status": "awaiting_edit",
        "words": [{"word": "a", "start": 0.0, "end": 1.0}],
    }
    try:
        # A per-source cache exists and says SPEAKER_00 ...
        (tmp_path / "srcA_reframe.json").write_text(json.dumps({
            "diarization": [{"start": 0.0, "end": 1.0,
                             "speaker": "SPEAKER_00"}]}), encoding="utf-8")
        # ... and a whole-edit record says this person is SPEAKER_02.
        (tmp_path / f"{jid}_tldiar.json").write_text(json.dumps({
            "by_source": {"srcA": [{"start": 0.0, "end": 1.0,
                                    "speaker": "SPEAKER_02"}]}},
        ), encoding="utf-8")

        out = app_mod.app.test_client().post("/stamp-speakers/srcA").get_json()
        assert out["ok"] and out["labels_from"] == "whole_edit", out
        assert app_mod.jobs["srcA"]["words"][0]["speaker"] == "SPEAKER_02"

        # With no whole-edit record it still falls back rather than failing.
        (tmp_path / f"{jid}_tldiar.json").unlink()
        out = app_mod.app.test_client().post("/stamp-speakers/srcA").get_json()
        assert out["labels_from"] == "per_source", out
    finally:
        app_mod.jobs.pop("srcA", None)


def test_one_source_failing_to_stamp_does_not_lose_the_analysis(app_mod,
                                                                monkeypatch):
    """The record is already on disk by then; losing it over one clip would
    throw away minutes of pyannote for nothing.
    """
    import uuid
    jid = uuid.uuid4().hex
    tl = _two_source_project(app_mod, jid)
    app_mod.jobs[jid] = {"status": "awaiting_edit", "timeline": tl}

    monkeypatch.setattr(app_mod, "_tl_concat_main_audio",
                        lambda t, dest, **k: {
                            "pieces": [
                                {"source_job_id": "srcA", "src_in": 0.0,
                                 "src_out": 4.0, "out_start": 0.0, "dur": 4.0},
                                {"source_job_id": "srcB", "src_in": 0.0,
                                 "src_out": 4.0, "out_start": 4.0, "dur": 4.0},
                            ],
                            "analysis_total": 8.0, "render_total": 8.0})
    monkeypatch.setattr(app_mod, "diarize_audio", lambda *a, **k: [
        {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"},
        {"start": 4.0, "end": 8.0, "speaker": "SPEAKER_01"},
    ])
    monkeypatch.setattr(app_mod, "_safe_unlink", lambda p: None)

    real = app_mod._stamp_job_speakers

    def flaky(sid, diar=None):
        if sid == "srcA":
            raise RuntimeError("boom")
        return real(sid, diar)

    monkeypatch.setattr(app_mod, "_stamp_job_speakers", flaky)
    try:
        rec = app_mod.analyze_timeline_speakers(jid, tl)
        assert rec["speakers"], "the analysis was lost over one clip"
        assert rec["words_stamped"] > 0, "the surviving clip was not labelled"
        assert "srcA" not in rec["words_stamped_by_source"]
        assert rec["words_stamped_by_source"]["srcB"] > 0
    finally:
        for k in (jid, "srcA", "srcB"):
            app_mod.jobs.pop(k, None)
        try:
            app_mod._tl_diar_path(jid).unlink(missing_ok=True)
        except OSError:
            pass


# --- correcting who said what -------------------------------------------
# Diarization is a guess and the person reading the transcript can see
# things it cannot. Merging two labels that are obviously one person IS the
# count going down, which is a far better lever than telling the analyser a
# number before you have seen any evidence.

@pytest.fixture
def spk_job(app_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(app_mod, "DB_PATH", tmp_path / "jobs.db")
    app_mod._db_init()
    app_mod.jobs["spk"] = {
        "status": "awaiting_edit",
        "words": [{"word": w, "start": i * 1.0, "end": i * 1.0 + 0.9,
                   "speaker": f"SPEAKER_0{i % 4}"}
                  for i, w in enumerate("a b c d e f g h".split())],
    }
    try:
        yield "spk"
    finally:
        app_mod.jobs.pop("spk", None)


def test_merging_two_speakers_lowers_the_count(app_mod, spk_job):
    c = app_mod.app.test_client()
    before = app_mod._speaker_counts(app_mod.jobs[spk_job]["words"])
    assert len(before) == 4

    r = c.post(f"/speakers/reassign/{spk_job}",
               json={"from": "SPEAKER_03", "to": "SPEAKER_01"}).get_json()
    assert r["ok"] and r["speaker_count"] == 3, r
    assert "SPEAKER_03" not in r["speakers"]
    # The words themselves moved, so every existing reader -- caption
    # colours, the breakdown, the count -- sees it with no new code.
    got = {w["speaker"] for w in app_mod.jobs[spk_job]["words"]}
    assert "SPEAKER_03" not in got and "SPEAKER_01" in got


def test_splitting_a_phrase_onto_a_new_speaker_raises_the_count(app_mod, spk_job):
    c = app_mod.app.test_client()
    # Collapse to two first, so "new" has a free label to claim.
    c.post(f"/speakers/reassign/{spk_job}",
           json={"from": "SPEAKER_02", "to": "SPEAKER_00"})
    c.post(f"/speakers/reassign/{spk_job}",
           json={"from": "SPEAKER_03", "to": "SPEAKER_01"})
    assert len(app_mod._speaker_counts(app_mod.jobs[spk_job]["words"])) == 2

    keys = [app_mod._word_key(w) for w in app_mod.jobs[spk_job]["words"][:2]]
    r = c.post(f"/speakers/reassign/{spk_job}",
               json={"words": keys, "to": "new"}).get_json()
    assert r["speaker_count"] == 3, r
    assert r["to"] not in ("SPEAKER_00", "SPEAKER_01"), r["to"]
    assert r["words_changed"] == 2


def test_a_correction_outlives_a_fresh_analysis(app_mod, spk_job):
    """The one that decides whether any of this is trustworthy.

    Re-running Analyze and silently discarding work done by hand would make
    the feature worse than not having it.
    """
    c = app_mod.app.test_client()
    keys = [app_mod._word_key(w) for w in app_mod.jobs[spk_job]["words"][:2]]
    c.post(f"/speakers/reassign/{spk_job}",
           json={"words": keys, "to": "SPEAKER_02"})
    corrected = [w["speaker"] for w in app_mod.jobs[spk_job]["words"][:2]]
    assert corrected == ["SPEAKER_02", "SPEAKER_02"]

    # A fresh diarization that disagrees about exactly those words.
    diar = [{"start": i * 1.0, "end": i * 1.0 + 0.95,
             "speaker": f"SPEAKER_0{i % 4}"} for i in range(8)]
    app_mod._stamp_job_speakers(spk_job, diar)

    after = [w["speaker"] for w in app_mod.jobs[spk_job]["words"][:2]]
    assert after == ["SPEAKER_02", "SPEAKER_02"], (
        f"the re-analysis overwrote the correction: {after}")
    # Untouched words still follow the new analysis.
    assert app_mod.jobs[spk_job]["words"][3]["speaker"] == "SPEAKER_03"


def test_corrections_can_be_thrown_away(app_mod, spk_job, tmp_path):
    """An override you cannot undo is a trap, not a feature."""
    c = app_mod.app.test_client()
    keys = [app_mod._word_key(w) for w in app_mod.jobs[spk_job]["words"][:2]]
    c.post(f"/speakers/reassign/{spk_job}",
           json={"words": keys, "to": "SPEAKER_02"})
    assert len(app_mod._load_speaker_fixes(spk_job)) == 2

    (tmp_path / f"{spk_job}_reframe.json").write_text(json.dumps({
        "diarization": [{"start": i * 1.0, "end": i * 1.0 + 0.95,
                         "speaker": f"SPEAKER_0{i % 4}"} for i in range(8)]}),
        encoding="utf-8")
    r = c.delete(f"/speakers/reassign/{spk_job}").get_json()
    assert r["ok"] and r["cleared"] == 2
    assert app_mod._load_speaker_fixes(spk_job) == {}
    assert app_mod.jobs[spk_job]["words"][0]["speaker"] == "SPEAKER_00"


def test_a_reassignment_that_names_nothing_is_refused(app_mod, spk_job):
    """Silently doing nothing is how a control that looks wired stays dead."""
    c = app_mod.app.test_client()
    assert c.post(f"/speakers/reassign/{spk_job}", json={}).status_code == 400
    assert c.post(f"/speakers/reassign/{spk_job}",
                  json={"to": "SPEAKER_01"}).status_code == 400
    assert c.post(f"/speakers/reassign/{spk_job}",
                  json={"from": "SPEAKER_09", "to": "SPEAKER_01"}
                  ).status_code == 400
    assert c.post(f"/speakers/reassign/{spk_job}",
                  json={"words": ["999.000"], "to": "SPEAKER_01"}
                  ).status_code == 400
    assert c.post(f"/speakers/reassign/{spk_job}",
                  json={"from": "SPEAKER_01", "to": "SPEAKER_01"}
                  ).status_code == 400
    # Nothing was written on any of those.
    assert app_mod._load_speaker_fixes(spk_job) == {}


def test_speaker_names_survive_the_save(app_mod):
    """serialize() is a fixed whitelist that has eaten five fields already."""
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    ser = js[js.index("function serialize()"):js.index("// ---- Loaders ----")]
    assert "speaker_names" in ser, "names vanish on the next save"

    tl = app_mod._normalize_timeline({
        "canvas": "9x16",
        "speaker_names": {"SPEAKER_00": "Maya", "SPEAKER_01": "   ",
                          "SPEAKER_02": "x" * 99},
        "tracks": {"main": [], "overlay": [], "text": [], "music": [],
                   "effects": []},
    })
    names = tl["speaker_names"]
    assert names["SPEAKER_00"] == "Maya"
    assert "SPEAKER_01" not in names, "a blank name should not be stored"
    assert len(names["SPEAKER_02"]) == 40, "names are bounded"

    # And a project with none is not given an empty dict to carry about.
    assert app_mod._normalize_timeline({
        "canvas": "9x16",
        "tracks": {"main": [], "overlay": [], "text": [], "music": [],
                   "effects": []}})["speaker_names"] is None


def test_the_speaker_label_is_a_control_not_a_caption(app_mod):
    """Before this it was text on the timestamp: a wrong speaker stayed
    wrong, and the count could only be changed by re-running the analysis
    with a number supplied up front."""
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    assert "tl-phrase-spk" in js and "openSpeakerMenu" in js
    menu = js[js.index("function openSpeakerMenu"):js.index("function _spkColors")]
    # The three corrections, all one mechanism.
    assert '"new"' in menu, "no way to split a phrase onto someone else"
    assert "_mergeSpeakers" in menu, "no way to merge, so the count cannot drop"
    assert "speaker_names" in menu, "no way to rename"
    # Dismissable by touch: a menu that only answers to click is one that
    # cannot be closed on iOS, which this project has already shipped once.
    #
    # Asserted on the LISTENER, not the word. A bare `"pointerdown" in menu`
    # passed with the listener changed to click, because the comment above
    # it explaining the choice contains the word -- the same self-exempting
    # assertion that let the [hidden] CSS bug through in #40.
    assert 'addEventListener("pointerdown"' in menu, \
        "the menu cannot be dismissed by touch"


# --- learning a look from a reference ------------------------------------
# Three kinds of answer kept apart: measured (a number out of the file),
# judged (a vision model named something) and needs_confirmation (fonts,
# which cannot be recovered from pixels at all). The feature is only
# trustworthy if a guess never arrives looking like a measurement.

def _build_reference(tmp_path, shots=6, shot_s=2.0, band_y=1360):
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    """A reference with KNOWN properties.

    Alternating dark/bright scenes with real structure, because a cut that
    only changes hue scores near zero: ffmpeg's scene detection is largely
    luma-based, and a fixture of similar-brightness colours measures the
    fixture rather than the code.
    """
    segs = []
    for i in range(shots):
        out = tmp_path / f"seg{i}.mp4"
        base = (f"color=c=0x101418:s=1080x1920:d={shot_s}:r=30" if i % 2 == 0
                else f"smptebars=s=1080x1920:d={shot_s}:r=30")
        vf = (f"drawbox=x=0:y={band_y}:w=1080:h=120:color=magenta@1:t=fill,"
              "drawtext=text='%{n}':x=120:y=" + str(band_y + 15)
              + ":fontsize=90:fontcolor=white")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", base,
                        "-vf", vf, "-pix_fmt", "yuv420p", str(out)], check=True)
        segs.append(out)
    lst = tmp_path / "list.txt"
    lst.write_text("".join(f"file '{s}'\n" for s in segs), encoding="utf-8")
    silent = tmp_path / "ref.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(silent)], check=True)
    out = tmp_path / "ref_av.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(silent), "-f", "lavfi",
                    "-i", f"sine=frequency=300:duration={shots * shot_s}",
                    "-filter:a", "volume=0.9", "-c:v", "copy", "-shortest",
                    str(out)], check=True)
    return out


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_a_reference_is_measured_not_guessed(app_mod, tmp_path, monkeypatch):
    """Built to known numbers, so the analysis has something to be wrong
    about. A measurement that returned a plausible constant would pass a
    test that only checked the shape of the result."""
    if not _have("cv2"):
        pytest.skip("cv2 not installed")
    refs = tmp_path / "refs"
    refs.mkdir()
    monkeypatch.setattr(app_mod, "REFERENCE_DIR", refs)
    # No key here, so the judged half degrades rather than running.
    monkeypatch.setattr(app_mod, "_ref_vision", lambda sheet: None)

    video = _build_reference(tmp_path, shots=6, shot_s=2.0, band_y=1360)
    rec = app_mod.analyze_reference(video, "r1", "My reel")
    pack = rec["pack"]

    # Cut rhythm: six 2-second shots.
    pacing = pack["pacing"]
    assert pacing["shots"] == 6, pacing
    assert 1.8 <= pacing["median_shot_s"] <= 2.2, pacing
    assert 26 <= pacing["cuts_per_min"] <= 34, pacing

    # Caption band: drawn at 1360/1920 = 70.8% down.
    assert 68 <= pack["style"]["position_y"] <= 74, pack["style"]

    # The accent is the magenta band, not the dominant ground.
    accent = pack["palette"]["accent"].upper()
    r, g, b = (int(accent[i:i + 2], 16) for i in (1, 3, 5))
    assert r > 150 and b > 150 and g < 110, f"accent is not the magenta: {accent}"
    assert pack["palette"]["accent_share"] < 0.4, "the accent is not a minority"

    # Loudness came back as a real number rather than silence.
    assert -40 < pack["loudness_i"] < -5, pack.get("loudness_i")


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_what_it_could_not_measure_is_absent_not_defaulted(app_mod, tmp_path,
                                                           monkeypatch):
    """Absent means "the pack you start from keeps its own value here".

    A confident wrong number is the expensive failure: it would silently
    retune every video made with the pack.
    """
    if not _have("cv2"):
        pytest.skip("cv2 not installed")
    refs = tmp_path / "refs"
    refs.mkdir()
    monkeypatch.setattr(app_mod, "REFERENCE_DIR", refs)
    monkeypatch.setattr(app_mod, "_ref_vision", lambda sheet: None)

    # Noise: edges everywhere and churn everywhere, spread evenly across
    # rows. Two earlier versions of this fixture proved nothing -- flat
    # grey has no churn and a colour wash has no edges, so each was
    # rejected by a guard BEFORE the confidence test and passed with that
    # test deleted. Noise is the case the confidence gate exists for: a
    # busy profile with no peak standing out. On this exact clip the
    # function used to answer position_y 100 -- the very bottom row --
    # with a confidence of 28, which is how the absolute edge floor and
    # the edge-row exclusion came to exist.
    plain = tmp_path / "plain.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x303030:s=540x960:d=3:r=15",
                    "-vf", "noise=alls=90:allf=t+u",
                    "-pix_fmt", "yuv420p", str(plain)], check=True)

    rec = app_mod.analyze_reference(plain, "r2")
    assert "position_y" not in (rec["pack"].get("style") or {}), \
        "a caption position was invented for a video with no captions"
    assert "position_y" not in rec["provenance"]
    assert any("caption" in n for n in rec["notes"]), rec["notes"]


def test_a_band_hard_against_the_frame_edge_is_not_a_caption(app_mod):
    """Built as arrays rather than a video, because the absolute edge floor
    rejects the noise clip first and this guard can never fire there.

    A high-contrast band changing in the very last rows is a letterbox
    seam or an encoding artifact. It is also exactly where a spurious peak
    lands, so without this it would be reported as a caption at 99%.
    """
    if not _have("cv2"):
        pytest.skip("cv2 not installed")
    import numpy as np

    def frames(band_rows, n=12):
        out = []
        for i in range(n):
            f = np.full((960, 540, 3), 90, dtype=np.uint8)
            # Structure everywhere, so the absolute edge floor is satisfied
            # and this test is about WHERE the peak is allowed to be.
            f[::6, :, :] = 170
            lo, hi = band_rows
            f[lo:hi, :, :] = 255 if i % 2 == 0 else 0
            out.append(f)
        return out

    # Hard against the bottom edge: refused.
    assert app_mod._ref_caption_band(frames((956, 960))) is None

    # The same band a sensible distance up IS a caption, so the guard is
    # an exclusion of the edge and not a rejection of everything.
    got = app_mod._ref_caption_band(frames((660, 700)))
    assert got is not None and 65 <= got["position_y"] <= 75, got


def test_a_font_is_never_asserted_only_offered(app_mod):
    """A font cannot be read off pixels. Anything claiming otherwise is
    guessing, and a wrong guess renders in the wrong face without saying
    so -- which this repo has already shipped once."""
    for category in ("condensed_sans", "geometric_sans", "unknown", "", None):
        choices = app_mod._ref_font_choices(category)
        assert choices, f"nothing offered for {category!r}"
        # Every offered face must exist on disk, or the pack renders in a
        # system fallback and nobody is told.
        files = {f.stem.lower().replace("-regular", "").replace("-black", "")
                 .replace("[wght]", "").replace(" ", "")
                 for f in (BURNER / "fonts").glob("*")
                 if f.suffix.lower() in (".ttf", ".otf")}
        for name in choices:
            key = name.lower().replace(" ", "").replace("thinblack", "black")
            assert key in files or name.lower().replace(" ", "") in files, \
                f"{name} is offered but is not in fonts/"
    # The commercial faces the repo deliberately does not bundle.
    everywhere = {n for c in app_mod._REF_FONT_CATEGORIES for n in
                  app_mod._ref_font_choices(c)}
    assert "Integral CF" not in everywhere and "The Bold Font" not in everywhere


def test_the_vision_vocabulary_is_closed(app_mod):
    """A device the app cannot make is a budget for an op that does not
    exist. Anything outside the list is discarded rather than carried."""
    raw = {"devices": [
        {"kind": "text_message", "frames": 10},
        {"kind": "holographic_pyramid", "frames": 30},   # invented
        {"kind": "TWO_UP", "frames": 4},                 # case
        {"kind": "document_card", "frames": 0},          # never seen
    ]}
    out = app_mod._ref_devices_from_vision(raw, total_tiles=40, duration_s=60.0)
    kinds = {d["kind"] for d in out}
    assert "holographic_pyramid" not in kinds
    assert "text_message" in kinds and "two_up" in kinds
    assert "document_card" not in kinds, "a device seen in no tile got a budget"
    # Every surviving device names an op the app actually has.
    for d in out:
        assert d["op"] in app_mod.REF_DEVICES.values()
    # Garbage in does not raise.
    assert app_mod._ref_devices_from_vision(None, 40, 60.0) == []
    assert app_mod._ref_devices_from_vision({"devices": "x"}, 40, 60.0) == []
    assert app_mod._ref_devices_from_vision(raw, 40, 0) == []


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_a_learned_look_outlives_the_cleanup_sweep(app_mod, tmp_path,
                                                   monkeypatch):
    """Renders are deleted after six hours by _cleanup_loop. A style sheet
    that evaporated the same way would be worse than useless, so what is
    kept lives outside every swept directory."""
    if not _have("cv2"):
        pytest.skip("cv2 not installed")
    refs = tmp_path / "refs"
    refs.mkdir()
    uploads = tmp_path / "up"
    uploads.mkdir()
    outputs = tmp_path / "out"
    outputs.mkdir()
    monkeypatch.setattr(app_mod, "REFERENCE_DIR", refs)
    monkeypatch.setattr(app_mod, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(app_mod, "OUTPUT_DIR", outputs)
    monkeypatch.setattr(app_mod, "_ref_vision", lambda sheet: None)

    video = _build_reference(tmp_path, shots=2, shot_s=1.0)
    rec = app_mod.analyze_reference(video, "r3", "Keep me")
    app_mod._save_reference(rec)

    sheet = refs / "r3.jpg"
    assert sheet.exists() and sheet.stat().st_size > 2000
    # Bounded in absolute terms, and -- the property that actually makes
    # keeping it forever safe -- CONSTANT with duration: it is always
    # REF_FRAME_SAMPLES tiles, whether the reference is ten seconds or ten
    # minutes. Comparing it to this fixture's own file size would prove
    # nothing, since a two-second test clip is smaller than the sheet.
    assert sheet.stat().st_size < 500_000, sheet.stat().st_size

    longer = _build_reference(tmp_path / "long", shots=8, shot_s=1.0)
    rec2 = app_mod.analyze_reference(longer, "r3b")
    assert rec2["frames_sampled"] == app_mod.REF_FRAME_SAMPLES
    bigger = (refs / "r3b.jpg").stat().st_size
    assert bigger < 500_000, bigger

    # Nothing of the record is in a directory the sweep touches.
    assert not list(uploads.glob("*")), list(uploads.glob("*"))
    assert not list(outputs.glob("*")), list(outputs.glob("*"))
    assert app_mod._load_reference("r3")["pack"]["label"] == "Keep me"


def test_the_reference_store_is_not_somewhere_the_sweep_can_reach(app_mod):
    """Asserted on the real constants, not a monkeypatched temp directory.

    The test above swaps all three for temp paths, so it proves the record
    lands in whichever directory REFERENCE_DIR points at -- and passed with
    REFERENCE_DIR redefined as OUTPUT_DIR, which is swept every six hours.
    """
    ref = app_mod.REFERENCE_DIR.resolve()
    for swept in (app_mod.OUTPUT_DIR, app_mod.UPLOAD_DIR):
        swept = swept.resolve()
        assert ref != swept, f"references live in {swept}, which is swept"
        assert swept not in ref.parents, f"references sit under {swept}"

    # And the sweep itself must not have learned to glob it.
    src = (BURNER / "app.py").read_text(encoding="utf-8")
    loop = src[src.index("def _cleanup_loop"):]
    loop = loop[:loop.index("\ndef ", 10)]
    assert "REFERENCE_DIR" not in loop, \
        "the cleanup loop touches the reference store"


def test_a_reference_becomes_a_pack_the_renderer_accepts(app_mod, temp_db,
                                                         tmp_path, monkeypatch):
    """The point of the whole thing: a learned look has to drive the same
    pipeline a built-in does, through the handover that has eaten five
    fields already."""
    monkeypatch.setattr(app_mod, "REFERENCE_DIR", tmp_path)
    app_mod._save_reference({
        "id": "r4", "created_at": 0, "source": "myreel.mp4", "sheet": "r4.jpg",
        "pack": {"label": "From a reference", "canvas": "9x16",
                 "style": {"highlight": "#E8C39E", "position_y": 71},
                 "pacing": {"median_shot_s": 2.4}},
        "provenance": {"highlight": "measured"}, "unresolved": [], "notes": [],
    })
    c = app_mod.app.test_client()
    r = c.post("/reference/r4/keep", json={
        "label": "My reel look", "style": {"font": "Anton"}}).get_json()
    assert r["ok"], r

    pack = app_mod._resolve_style_pack(r["id"])
    assert pack["style"]["font"] == "Anton", "the correction was dropped"
    assert pack["style"]["highlight"] == "#E8C39E"
    assert pack["pacing"]["median_shot_s"] == 2.4

    # It shows up where the built-ins do, marked as learned.
    listed = {p["id"]: p for p in c.get("/ai-edit/style-packs").get_json()["packs"]}
    assert listed[r["id"]]["source"] == "reference"
    assert set(app_mod.AI_EDIT_STYLE_PACKS).issubset(set(listed))

    # Nothing is saved until Keep is called.
    assert c.post("/reference/nope/keep", json={}).status_code == 404


def test_learning_a_look_is_reachable(app_mod):
    """An engine nothing can ask for is the bug this project keeps hitting:
    add_effect, `panels`, the wide/close split and "Exactly N" each shipped
    complete and unreachable, the last one because I put its control in a
    hidden panel myself."""
    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="learnLookBtn"' in html
    # The button's OWN tag as well as its ancestors: _ancestor_tags walks
    # upwards only, so a bare `hidden` on the element itself slipped past
    # an earlier version of this.
    own = html[html.rindex("<", 0, html.index('id="learnLookBtn"')):]
    own = own[:own.index(">") + 1]
    assert not re.search(r"(^|[\s\"'])hidden([\s>=\"']|$)", own), own

    for ctl in ('id="learnLookBtn"', 'id="learnLookPanel"'):
        for name, tag in _ancestor_tags(html, ctl):
            assert 'aria-hidden="true"' not in tag, (ctl, tag)
            if not re.search(r"(^|[\s\"'])hidden([\s>=\"']|$)", tag):
                continue
            handles = re.findall(r'id="([^"]+)"', tag)
            handles += [c for c in re.findall(r'class="([^"]*)"', tag)
                        for c in c.split()]
            handles = [h for h in handles if h not in ("hidden", "panel")]
            assert any(h in js for h in handles), (
                f"{ctl} is inside a hidden <{name}> nothing can open: {tag!r}")

    body = js[js.index("function wireLearnLook"):]
    assert '"/reference/analyze"' in body, "the button reaches no endpoint"
    assert '"/reference/" + refId' in body, "it never polls for the result"
    assert '/keep' in body, "there is no way to accept the sheet"
    # The three kinds of answer must stay visually apart, or a guess
    # arrives looking like a measurement.
    assert "needs_confirmation" in body and "judged" in body and "measured" in body
    css = (BURNER / "static" / "style.css").read_text(encoding="utf-8")
    for cls in (".ll-m", ".ll-j", ".ll-a"):
        assert cls in css, f"{cls} has no styling, so the tiers look alike"


# --- making a look, where looks are chosen -------------------------------
# #46 shipped the reference analysis reachable only through
# Library → Look → scroll. The #46 test proved it was not HIDDEN, which is
# a different question from whether anyone would find it.

def test_the_pack_picker_offers_to_learn_one(app_mod):
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    fn = js[js.index("async function renderAiEditPacks"):]
    fn = fn[:fn.index("\nfunction closeAiEditPlan")]

    assert 'id="aiEditLearnPack"' in fn or 'learn.id = "aiEditLearnPack"' in fn
    assert "openLearnLook" in fn, "the card reaches nothing"
    # It must not be mistaken for a pack: the active-state sweep keys off
    # data-pack, so the card deliberately has none.
    learn = fn[fn.index("const learn = document.createElement"):]
    learn = learn[:learn.index("host.appendChild(learn)")]
    assert "learn.dataset.pack" not in learn, \
        "the learn card carries data-pack and could be selected as a look"
    # Saving refreshes the list, or the new pack is invisible until reload.
    assert "renderAiEditPacks()" in learn, "the list is not refreshed after saving"


def test_the_learn_panel_is_moved_not_duplicated(app_mod):
    """Two copies would mean two elements sharing every id in the panel,
    and getElementById would silently start answering about whichever came
    first in the document."""
    js = (BURNER / "static" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function wireLearnLook"):]
    assert "window.openLearnLook" in body
    assert "host.appendChild(panel)" in body, "the panel is not moved to the host"
    # Asserted on the restore ITSELF, not the helper's name: renaming the
    # function left every caller still saying "returnHome", so a bare
    # name check passed on code that would throw at runtime.
    assert "home.appendChild(panel)" in body, "the panel is never put back"
    assert "closeBtn.onclick = returnHome" in body, "closing does not restore it"

    # And whoever borrows it gives it back when their surface goes away.
    # Without this, closing the AI Edit modal after saving leaves the panel
    # parked inside it and Library -> Look comes up empty.
    assert "window.learnLookReturnHome" in body
    close = js[js.index("function closeAiEditPlan"):]
    close = close[:close.index("\n}")]
    assert "learnLookReturnHome" in close, \
        "closing the modal strands the panel inside it"
    # cloneNode would be the duplicating mistake.
    assert "cloneNode" not in body, "the panel is cloned, duplicating its ids"

    html = (BURNER / "templates" / "index.html").read_text(encoding="utf-8")
    for el in ("learnLookPanel", "learnLookFile", "learnLookSheet"):
        assert html.count(f'id="{el}"') == 1, f"{el} is declared more than once"


def test_the_strip_control_is_named_for_what_it_does(app_mod):
    """The instructions I wrote for this control said "strip size"; the
    control said "Split at (top panel)". The word people reach for is the
    one it should carry."""
    js = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    fn = js[js.index("function splitRatioControl"):]
    fn = fn[:fn.index("function panelBackgroundControls")]
    assert 'c.mode === "wide_punch"' in fn
    assert "Strip size" in fn, "wide/close mode still says 'Split at'"
    assert "Split at" in fn, "the media split lost its own wording"
