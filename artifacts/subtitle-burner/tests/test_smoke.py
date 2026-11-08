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

    monkeypatch.setattr(app_mod, "_face_anchor_at", lambda j, t: {"x": 0.61, "y": 0.27})
    got = app_mod._resolve_punch_aim({"op": "set_punch_aim", "index": 0, "at": "speaker"},
                                     main, effects)
    assert got == {"op": "set_punch_aim", "index": 0, "x": 0.61, "y": 0.27}

    # No face data: drop the op rather than ship one that aims at nothing.
    monkeypatch.setattr(app_mod, "_face_anchor_at", lambda j, t: None)
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
        py = re.sub(r"\bt\b", repr(float(t)), expr)
        return eval(py, {"min": min, "max": max, "pow": pow})

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

    It happened with the punch zoom (the preview kept its own 1.40 peaks) and
    with the caption font. This compares the browser's easing against the
    renderer's expression at the same instants rather than trusting that two
    copies of a cubic stay equal.
    """
    import re
    import shutil
    import subprocess as sp

    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")

    js_src = (BURNER / "static" / "timeline.js").read_text(encoding="utf-8")
    start = js_src.index("const OVERLAY_SWIPE_FROM")
    end = js_src.index("// Runs every frame so punch + Ken Burns animate")
    block = js_src[start:end]
    block = re.sub(r"^\s*const ", "var ", block, flags=re.M)
    block = re.sub(r"^\s*function ", "function ", block, flags=re.M)

    times = [0, 0.0875, 0.175, 0.2625, 0.35, 0.5]
    script = (
        block
        + "const out=[];"
        + f"for (const lt of {times}) "
        + 'out.push(overlayMotionOffset({motion:{type:"swipe_left",in:0.35}}, lt).dx.toFixed(6));'
        + "console.log(out.join(' '));"
    )
    js = sp.run([node, "-e", script], capture_output=True, text=True, check=True)
    js_vals = [float(v) for v in js.stdout.split()]

    W = 1000
    expr, _ = app_mod._tl_overlay_motion_expr(
        {"type": "swipe_left", "in": 0.35}, 0, 0, 0.0, W, 2000)
    py_vals = [
        eval(re.sub(r"\bt\b", repr(float(t)), expr),
             {"min": min, "max": max, "pow": pow}) / W
        for t in times
    ]

    for t, a, b in zip(times, js_vals, py_vals):
        assert abs(a - b) < 1e-6, (
            f"at {t}s the preview offsets by {a} and the render by {b}")
    assert js_vals[0] == 1.0 and js_vals[-1] == 0.0, "the curve does not run 1->0"


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
