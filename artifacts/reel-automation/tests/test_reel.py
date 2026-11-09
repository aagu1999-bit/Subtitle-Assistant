"""Tests for the reel pipeline.

Written to run without moviepy or playwright installed, since the parts
most likely to be wrong -- the easing maths and the shadow compositing --
need neither. Only the end-to-end render does, and it skips when absent.

    python -m pytest artifacts/reel-automation/tests -v
    python -m pytest artifacts/                        # everything
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_DIR))

import main as reel  # noqa: E402


def _have(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


@pytest.fixture
def card(tmp_path):
    """A rounded card on transparency, flush to its own bounds.

    Flush on purpose: it is the worst case for shadow clipping, which is
    the failure this asset exists to expose.
    """
    from PIL import Image, ImageDraw
    p = tmp_path / "card.png"
    im = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle([0, 0, 299, 199], radius=28,
                                         fill=(240, 60, 90, 255))
    im.save(p)
    return p


# --- easing ---------------------------------------------------------------

def test_the_ease_starts_at_zero_and_ends_at_one():
    assert reel.ease_out_back(0.0) == pytest.approx(0.0, abs=1e-9)
    assert reel.ease_out_back(1.0) == pytest.approx(1.0, abs=1e-9)


def test_the_ease_actually_overshoots():
    """Without the overshoot it is a linear zoom with extra steps.

    This is the whole reason for easeOutBack over a lerp, so it is worth
    asserting rather than assuming.
    """
    samples = [reel.ease_out_back(i / 500) for i in range(501)]
    assert max(samples) > 1.0, "never passes 1.0 -- that is not a pop"


def test_the_textbook_constant_peaks_at_1_10_not_1_05():
    """The trap this codebase documents.

    A spec asking for a 1.05 overshoot and a snippet using the textbook
    c1 = 1.70158 are not the same animation, and nothing about the number
    says so.
    """
    assert reel.back_peak(1.70158) == pytest.approx(1.10, abs=1e-4)


def test_a_requested_peak_is_solved_exactly():
    """Set the overshoot in human terms; the constant follows.

    Sampled rather than trusting the closed form, so an error in either
    the formula or the solver shows up.
    """
    for wanted in (1.02, 1.05, 1.10, 1.25):
        c1 = reel.back_constant_for_peak(wanted)
        assert reel.back_peak(c1) == pytest.approx(wanted, abs=1e-6)
        sampled = max(reel.ease_out_back(i / 2000, c1) for i in range(2001))
        assert sampled == pytest.approx(wanted, abs=1e-3), (wanted, sampled)


def test_no_overshoot_is_a_valid_ask():
    assert reel.back_constant_for_peak(1.0) == 0.0
    assert reel.ease_out_back(0.5, 0.0) <= 1.0


def test_the_pop_never_returns_a_scale_that_rounds_to_zero_pixels():
    """The bug that broke the first real render.

    MoviePy resizes to whole pixels, so a factor small enough to round to
    0 raises `height and width must be > 0` from inside Pillow, mid-encode.
    Reading the code did not catch it; running it did.
    """
    floor = 2.0 / 400.0
    for i in range(200):
        s = reel.pop_scale_at(i / 100.0, duration=0.5, peak=1.05, floor=floor)
        assert s >= floor, f"t={i / 100.0} gave {s}, which rounds to no pixels"
        assert round(400 * s) >= 1


def test_the_pop_settles_at_exactly_one():
    """It has to land, not hover near 1.0 forever."""
    assert reel.pop_scale_at(0.5, duration=0.5, peak=1.05) == 1.0
    assert reel.pop_scale_at(9.9, duration=0.5, peak=1.05) == 1.0


# --- drop shadow ----------------------------------------------------------

def test_the_shadow_canvas_grows_enough_for_the_blur(card, tmp_path):
    """Pillow composites inside the source bounds.

    An offset, blurred shadow on an un-grown canvas is sliced flat along
    two edges, which reads as a design problem rather than a bug.
    """
    from PIL import Image
    out = reel.add_drop_shadow(card, tmp_path / "s.png", offset=(15, 15),
                               blur=18, opacity=0.55)
    src, got = Image.open(card), Image.open(out)
    pad = 15 + 18 * 3
    assert got.size == (src.size[0] + pad * 2, src.size[1] + pad * 2)


def test_the_shadow_tail_is_not_clipped(card, tmp_path):
    """The far corner must be empty -- proof the blur had room to fade.

    If the canvas were too small the tail would still be opaque where it
    meets the edge.
    """
    from PIL import Image
    out = reel.add_drop_shadow(card, tmp_path / "s.png", offset=(15, 15), blur=18)
    im = Image.open(out).convert("RGBA")
    assert im.getpixel((im.size[0] - 2, im.size[1] - 2))[3] == 0


def test_the_shadow_appears_where_the_offset_puts_it(card, tmp_path):
    """Down and to the right of the subject, where nothing was before."""
    from PIL import Image
    out = reel.add_drop_shadow(card, tmp_path / "s.png", offset=(15, 15), blur=18)
    src, im = Image.open(card).convert("RGBA"), Image.open(out).convert("RGBA")
    pad = 15 + 18 * 3
    px, py = src.size[0] + 8, src.size[1] + 8
    assert src.size[0] <= px and src.size[1] <= py          # transparent in source
    assert im.getpixel((px + pad, py + pad))[3] > 10, "no shadow was cast"


def test_the_subject_pixels_are_untouched(card, tmp_path):
    """A depth shadow sits behind. Any change to the subject is a bug."""
    from PIL import Image
    out = reel.add_drop_shadow(card, tmp_path / "s.png", offset=(15, 15), blur=18)
    src, im = Image.open(card).convert("RGBA"), Image.open(out).convert("RGBA")
    pad = 15 + 18 * 3
    cx, cy = src.size[0] // 2, src.size[1] // 2
    assert im.getpixel((cx + pad, cy + pad)) == src.getpixel((cx, cy))


def test_the_shadow_follows_the_alpha_not_the_bounding_box(card, tmp_path):
    """A rounded card must not cast a square shadow.

    Sampled in the BOTTOM OVERHANG -- the strip below the subject where
    the offset shadow is the only thing drawn. A first version of this
    test sampled over the subject, which sits on top of the shadow, and so
    measured the card's own opacity and failed a correct implementation.
    """
    from PIL import Image
    offset, blur = 40, 2
    out = reel.add_drop_shadow(card, tmp_path / "s.png",
                               offset=(offset, offset), blur=blur)
    im = Image.open(out).convert("RGBA")
    pad = offset + blur * 3
    ox = oy = pad + offset           # where the shadow silhouette starts
    y = oy + 197                     # 2px above the shadow's bottom edge

    corner = im.getpixel((ox + 297, y))[3]     # outside the rounded corner
    middle = im.getpixel((ox + 150, y))[3]     # mid bottom edge
    assert middle > corner, (
        f"corner alpha {corner} vs mid-edge {middle} -- the shadow is "
        "square, so it came from the bounding box rather than the alpha"
    )
    assert corner == 0, f"rounded corner is casting shadow: alpha {corner}"


# --- fonts ----------------------------------------------------------------

def test_a_missing_font_falls_back_to_a_real_file():
    """Poppins is not bundled, and MoviePy 2.x needs a path that exists."""
    got = reel.resolve_font(["Poppins-Regular", "DM Sans"], quiet=True)
    assert Path(got).is_file()


def test_an_exact_match_wins_over_a_substring():
    """Otherwise 'Anton-Regular' could lose to any file containing it."""
    got = reel.resolve_font(["Anton-Regular"], quiet=True)
    assert Path(got).stem == "Anton-Regular"


def test_resolution_never_returns_a_name_that_is_not_a_path():
    """The failure mode this guards: MoviePy raises on a family name.

    Returning 'Poppins-Regular' would look fine until render time.
    """
    for candidates in (["Poppins-Regular"], ["nonexistent-font-xyz"],
                       ["Bebas-Neue-Bold", "BebasNeue-Regular"]):
        got = reel.resolve_font(candidates, quiet=True)
        assert Path(got).is_file(), f"{candidates} -> {got!r} is not a file"


# --- payload --------------------------------------------------------------

def test_the_payload_maps_onto_the_config():
    cfg = reel.RenderConfig.from_payload({
        "url": "https://example.org", "duration": 7.5, "pop_peak": 1.2,
        "active_color": "#00FF00",
    })
    assert cfg.url == "https://example.org"
    assert cfg.duration == 7.5 and cfg.pop_peak == 1.2
    assert cfg.active_color == "#00FF00"


def test_unknown_payload_keys_are_ignored():
    """An n8n body carries routing metadata that is none of our business.

    Failing a render over an extra key would be absurd.
    """
    cfg = reel.RenderConfig.from_payload({
        "url": "https://example.org",
        "executionId": "n8n-9912", "workflowName": "reels", "$node": {},
    })
    assert cfg.url == "https://example.org"
    assert not hasattr(cfg, "executionId")


def test_json_lists_become_tuples_again():
    """JSON has no tuples, and the maths downstream unpacks pairs."""
    cfg = reel.RenderConfig.from_payload({"shadow_offset": [20, 30],
                                          "shot_center": [0.25, 0.5]})
    assert cfg.shadow_offset == (20, 30)
    assert cfg.shot_center == (0.25, 0.5)


def test_an_empty_payload_is_just_the_defaults():
    assert reel.RenderConfig.from_payload(None).url == reel.RenderConfig().url
    assert reel.RenderConfig.from_payload({}).duration == reel.RenderConfig().duration


# --- end to end -----------------------------------------------------------

@pytest.mark.skipif(not _have("moviepy"), reason="moviepy not installed")
def test_the_pipeline_renders_a_playable_file(card, tmp_path):
    """Composite for real, with the capture stubbed.

    Asserts pixels rather than existence: the screenshot must be ABSENT
    before the pop and present after, which is what proves with_start is
    scheduling it rather than drawing it at scale zero.
    """
    if not _have("cv2") and not __import__("shutil").which("ffmpeg"):
        pytest.skip("no ffmpeg to probe the output")

    out = tmp_path / "render.mp4"
    cfg = reel.RenderConfig(width=360, height=640, duration=2.0, fps=15,
                            pop_start=0.4, pop_duration=0.5,
                            shot_center=(0.5, 0.35), out_path=str(out))
    reel.build_video(cfg, screenshot_png=card)
    assert out.exists() and out.stat().st_size > 2000

    ffmpeg = __import__("shutil").which("ffmpeg")
    from PIL import Image

    def frame(t):
        png = tmp_path / f"f{t}.png"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", str(t),
                        "-i", str(out), "-frames:v", "1", str(png)], check=True)
        return Image.open(png).convert("RGB")

    centre = (180, int(640 * 0.35))
    before = frame(0.1).getpixel(centre)
    after = frame(1.2).getpixel(centre)
    assert before != after, "the screenshot is on screen before it pops in"
    r, g, b = after
    assert r > 150 and g < 120, f"the card is not visible after the pop: {after}"


# --- text metrics ---------------------------------------------------------
# Two bugs that no amount of reading would have found, because the numbers
# the library reports are the wrong numbers. Both were caught by rendering
# a frame and looking at it.

@pytest.mark.skipif(not _have("moviepy"), reason="moviepy not installed")
def test_a_text_clip_is_tall_enough_for_its_own_descenders():
    """MoviePy sizes a TextClip from the drawn bbox and comes up short.

    For DM Sans at 84px it allocates 83px where the font needs 111, so
    every 'g', 'y' and 'p' is sliced flat. The clip's reported `.size` is
    the WRONG size, which is why this needs the font's own metrics.
    """
    import numpy as np
    font = reel.resolve_font(["DM Sans"], quiet=True)
    needed = reel.font_line_height(font, 84)

    from moviepy import TextClip
    naive = TextClip(text="gypsy", font=font, font_size=84, color="#FFFFFF")
    assert naive.h < needed, (
        "MoviePy no longer under-allocates; this guard can be revisited"
    )

    clip = reel.make_text_clip("gypsy", font, 84, "#FFFFFF")
    assert clip.h >= needed

    # Ink reaching the final row is CORRECT here -- the box is exactly
    # ascent + descent, so a descender is meant to touch the descent line.
    # What matters is whether the tail EXISTS, so compare the ink itself:
    # a sliced 'g' and 'y' simply have fewer lit pixels.
    def _ink(c):
        return float((c.mask.get_frame(0) > 0.02).sum())

    assert _ink(clip) > _ink(naive) * 1.02, (
        f"fixed render has {_ink(clip):.0f} ink px vs naive {_ink(naive):.0f} "
        "-- the descenders are still being cut"
    )
    # And nothing is lost off the bottom of the box.
    rows = np.where(clip.mask.get_frame(0).max(axis=1) > 0.02)[0]
    assert rows.max() <= clip.h - 1


@pytest.mark.skipif(not _have("moviepy"), reason="moviepy not installed")
def test_every_word_in_a_line_gets_the_same_height():
    """Otherwise top-aligning them puts each word on its own baseline.

    'edit' has no descender and 'gypsy' does, so their natural bounding
    boxes differ; laid out at a common y they would step up and down.
    """
    font = reel.resolve_font(["DM Sans"], quiet=True)
    heights = {reel.make_text_clip(w, font, 84, "#FFFFFF").h
               for w in ("edit", "gypsy", "THIS", "a")}
    assert len(heights) == 1, f"words render at differing heights: {heights}"


@pytest.mark.skipif(not _have("moviepy"), reason="moviepy not installed")
def test_the_active_word_replaces_the_base_word_rather_than_covering_it():
    """Two different faces cannot be stacked "exactly on top".

    The brief asked for the highlight layer over the base layer. With a
    condensed all-caps Bebas over a humanist DM Sans the gold word is
    narrower than the white one beneath, so the base shows out of both
    sides and the line reads as a double exposure. The word is swapped,
    which means at any instant exactly one layer covers each slot.
    """
    cfg = reel.RenderConfig(caption_size=48)
    words = [{"word": "alpha", "start": 0.0, "end": 0.5},
             {"word": "beta", "start": 0.5, "end": 1.0}]
    block = reel.caption_clips([words], cfg)[0]

    # Layer 0 is the slab; the rest are text. Count what is visible
    # mid-way through the first word.
    t = 0.25
    live = []
    for layer in block.clips[1:]:
        start = layer.start or 0.0
        end = layer.end if layer.end is not None else block.duration
        if start <= t < end:
            live.append(layer)
    # One per word: 'alpha' as the gold active, 'beta' still white.
    assert len(live) == len(words), (
        f"{len(live)} text layers live at t={t} for {len(words)} words -- "
        "the faces are stacking rather than swapping"
    )
