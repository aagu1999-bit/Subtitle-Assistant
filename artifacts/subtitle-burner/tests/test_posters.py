"""The picker shows a real still, not a drawing of a category.

What the slash menu used to show was _look_poster_image: a PNG drawn with
PIL.ImageDraw, picked by _poster_kind from a handful of categories. A category
is not a composition, so the thumbnail could never resemble the render --
_poster_kind('remotion', XFollowCard) is 'card', and the card previewed as a
generic rectangle.

Two things make the honest version affordable, and both are load-bearing here:

  * a still of the slowest composition costs ~4s once Remotion's bundle is
    warm, against 500s for its full render; and
  * nothing renders on a request. The route serves a warmed still if one
    exists and otherwise serves the old sketch, so the picker never waits and
    a look with no still is not an error.

The last point is the one worth a test with teeth: if _look_poster_png ever
learns to render, a picker opening 43 rows pays three minutes.
"""
import importlib
import sys
from pathlib import Path

import pytest
from PIL import Image

BURNER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BURNER))

app = importlib.import_module("app")

REC = {"id": "ZZTestLook", "words": ("test look",), "dur": 4.0, "place": "full"}


@pytest.fixture()
def posters(tmp_path, monkeypatch):
    """Point the poster cache at a scratch directory."""
    monkeypatch.setattr(app, "POSTER_DIR", tmp_path)
    return tmp_path


def _still(path: Path, size=(132, 234), colour=(255, 0, 0, 255)) -> bytes:
    Image.new("RGBA", size, colour).save(path, "PNG")
    return path.read_bytes()


# ---- the still wins over the sketch -----------------------------------------

def test_warmed_still_is_served_instead_of_the_sketch(posters):
    want = _still(app._look_still_path("remotion", REC))
    got = app._look_poster_png("remotion", REC)
    assert got == want, "a warmed still exists and the sketch was served anyway"


def test_sketch_is_served_when_no_still_has_been_warmed(posters):
    got = app._look_poster_png("remotion", REC)
    assert got[:8] == b"\x89PNG\r\n\x1a\n"
    # The sketch is the drawn 168x300, not a still.
    assert Image.open(__import__("io").BytesIO(got)).size == (168, 300)


def test_a_look_with_no_composition_keeps_its_sketch(posters):
    """motion/effects looks have nothing to render; they must not regress."""
    got = app._look_poster_png("effects", {"id": "lean-in", "words": ("lean in",)})
    assert Image.open(__import__("io").BytesIO(got)).size == (168, 300)


# ---- the request path must never render -------------------------------------

def test_serving_a_poster_never_invokes_remotion(posters, monkeypatch):
    """The wire that matters. Rendering here costs ~4s per row.

    Proved by breaking it: make _render_remotion_still explode if touched.
    """
    def boom(*a, **k):
        raise AssertionError("_look_poster_png rendered on the request path")

    monkeypatch.setattr(app, "_render_remotion_still", boom)
    app._look_poster_png("remotion", REC)          # no still warmed
    _still(app._look_still_path("remotion", REC))
    app._look_poster_png("remotion", REC)          # still warmed


# ---- blank frames are retried, not shipped ----------------------------------

def test_still_has_ink_rejects_a_fully_transparent_frame():
    assert not app._still_has_ink(Image.new("RGBA", (8, 8), (0, 0, 0, 0)))
    assert app._still_has_ink(Image.new("RGBA", (8, 8), (0, 0, 0, 255)))
    one = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    one.putpixel((3, 3), (255, 255, 255, 255))
    assert app._still_has_ink(one), "one opaque pixel is still a drawn look"


def test_frame_candidates_are_several_and_inside_the_composition():
    frames = app._look_still_frames(REC)
    assert len(frames) > 1, "a WebGL look that fails to paint needs another frame"
    assert len(set(frames)) == len(frames)
    assert all(1 <= f < int(REC["dur"] * 30) for f in frames)


def test_a_blank_first_frame_is_retried(posters, monkeypatch):
    """Lightning measured 0% opaque at frame 72 and 100% at 60. Retry or lose it."""
    seen = []

    def fake_render(comp, props, out, *, frame=0, **k):
        seen.append(frame)
        blank = frame == app._look_still_frames(REC)[0]
        Image.new("RGBA", (16, 28), (0, 0, 0, 0 if blank else 255)).save(out, "PNG")

    monkeypatch.setattr(app, "_render_remotion_still", fake_render)
    monkeypatch.setattr(app, "_remotion_look_props", lambda *a, **k: {})
    assert app._warm_look_poster(REC) == "ok"
    assert len(seen) == 2, f"expected one retry after the blank frame, got {seen}"


def test_all_frames_blank_is_reported_not_shipped(posters, monkeypatch):
    def always_blank(comp, props, out, *, frame=0, **k):
        Image.new("RGBA", (16, 28), (0, 0, 0, 0)).save(out, "PNG")

    monkeypatch.setattr(app, "_render_remotion_still", always_blank)
    monkeypatch.setattr(app, "_remotion_look_props", lambda *a, **k: {})
    assert app._warm_look_poster(REC).startswith("failed")
    assert not app._look_still_path("remotion", REC).exists()


# ---- the overlay stays an overlay -------------------------------------------

def test_a_warmed_poster_keeps_its_alpha(posters, monkeypatch):
    """SplitText is 0.6% opaque. Flattened to RGB it became a solid block."""
    def fake_render(comp, props, out, *, frame=0, **k):
        im = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
        im.paste((255, 255, 255, 255), (40, 900, 1040, 1000))
        im.save(out, "PNG")

    monkeypatch.setattr(app, "_render_remotion_still", fake_render)
    monkeypatch.setattr(app, "_remotion_look_props", lambda *a, **k: {})
    assert app._warm_look_poster(REC) == "ok"
    out = Image.open(app._look_still_path("remotion", REC))
    assert out.mode == "RGBA", "the picker composites it over its own backdrop"
    lo, hi = out.getchannel("A").getextrema()
    assert lo == 0 and hi > 8, "a transparent overlay must stay transparent"
    assert out.size[0] <= app.POSTER_W and out.size[1] <= app.POSTER_H


def test_warm_skips_a_look_that_already_has_a_still(posters, monkeypatch):
    _still(app._look_still_path("remotion", REC))

    def boom(*a, **k):
        raise AssertionError("re-rendered a look that already had a still")

    monkeypatch.setattr(app, "_render_remotion_still", boom)
    assert app._warm_look_poster(REC) == "cached"
