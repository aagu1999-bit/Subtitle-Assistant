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
import struct
import subprocess
import sys
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
