#!/usr/bin/env python3
"""Headless 2.5D screenshot + word-caption renderer.

A web element is screenshotted with a transparent background, given a
depth shadow, popped onto the frame with an eased scale, and composited
over a base video carrying word-level captions.

Built to be driven by an n8n webhook: every tunable lives on RenderConfig,
which `from_payload()` builds straight from the JSON body. Nothing reads
globals, so swapping a value is a payload change rather than an edit.

    python main.py                          # defaults, mocked transcript
    python main.py --payload job.json       # n8n body on disk
    cat job.json | python main.py --payload -

VERSION NOTES, because each of these silently breaks copied examples:

  * MoviePy 2.x renamed the mutation API. It is `resized`, `with_position`,
    `with_duration`, `with_start`, and `TextClip(text=...)`. The 1.x names
    (`resize`, `set_position`, `TextClip(txt=...)`) do not exist. Almost
    every MoviePy snippet online is 1.x.
  * MoviePy 2.x draws text with Pillow, not ImageMagick, so `font=` is a
    PATH TO A .ttf. `font="Poppins-Regular"` raises. resolve_font() exists
    for exactly this.
  * Pillow composites inside the source image's bounds, so a shadow that
    is offset and blurred without first growing the canvas gets clipped at
    the edges -- which reads as "slightly wrong" rather than "broken", the
    worst kind of bug to chase.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

from PIL import Image, ImageFilter

HERE = Path(__file__).resolve().parent
REPO_FONTS = HERE.parent / "subtitle-burner" / "fonts"
BUILD_DIR = HERE / "build"

# Where to look for a .ttf, in order. The repo's own fonts win so a render
# is reproducible on any machine rather than depending on what the host
# happens to have installed.
FONT_SEARCH_DIRS = [
    REPO_FONTS,
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".fonts",
    Path("/Library/Fonts"),            # macOS
    Path.home() / "Library" / "Fonts",
    Path("/System/Library/Fonts"),
]


# --------------------------------------------------------------------------
# Configuration -- the whole n8n contract is this object.
# --------------------------------------------------------------------------

@dataclass
class RenderConfig:
    """Everything the pipeline can be told to do.

    Font entries are CANDIDATE LISTS, not names: the first that resolves
    to a real file wins. Poppins is not bundled in this repo, so the
    fallback is the ordinary path rather than an error case.
    """

    # --- source ---
    url: str = "https://example.com"
    selector: str = "body > div"
    viewport: tuple[int, int] = (1280, 800)
    capture_timeout_ms: int = 15_000

    # --- canvas ---
    width: int = 1080
    height: int = 1920
    fps: int = 30
    duration: float = 4.0
    base_color: tuple[int, int, int] = (12, 12, 14)

    # --- screenshot layer ---
    shot_width_frac: float = 0.82          # of canvas width
    shot_center: tuple[float, float] = (0.5, 0.38)
    shadow_offset: tuple[int, int] = (15, 15)
    shadow_blur: int = 18
    shadow_opacity: float = 0.55
    shadow_color: tuple[int, int, int] = (0, 0, 0)

    # --- pop animation ---
    pop_start: float = 0.15
    pop_duration: float = 0.5
    pop_peak: float = 1.05                 # overshoot before settling at 1.0

    # --- captions ---
    caption_font: list[str] = field(
        default_factory=lambda: ["Poppins-Regular", "DM Sans", "Montserrat-Black"])
    active_font: list[str] = field(
        default_factory=lambda: ["Bebas-Neue-Bold", "BebasNeue-Regular", "Anton-Regular"])
    caption_size: int = 84
    caption_color: str = "#FFFFFF"
    active_color: str = "#FFD700"           # golden yellow
    block_color: str = "#1A1C1D"            # semi-transparent dark slab
    block_opacity: float = 0.72
    block_pad: tuple[int, int] = (28, 14)
    caption_y_frac: float = 0.74

    # --- output ---
    out_path: str = "final_render.mp4"

    @classmethod
    def from_payload(cls, payload: dict | None) -> "RenderConfig":
        """Build from an n8n JSON body, ignoring keys we do not know.

        Unknown keys are dropped rather than raising: a webhook body
        routinely carries routing metadata that is none of our business,
        and failing the render over it would be absurd.
        """
        cfg = cls()
        if not payload:
            return cfg
        known = {f for f in asdict(cfg)}
        for key, value in payload.items():
            if key not in known:
                continue
            current = getattr(cfg, key)
            # JSON has no tuples; restore them so downstream maths is safe.
            if isinstance(current, tuple) and isinstance(value, list):
                value = tuple(value)
            setattr(cfg, key, value)
        return cfg


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------

def _font_files() -> list[Path]:
    found: list[Path] = []
    for root in FONT_SEARCH_DIRS:
        if not root.exists():
            continue
        try:
            found.extend(p for p in root.rglob("*.tt[fc]") if p.is_file())
            found.extend(p for p in root.rglob("*.otf") if p.is_file())
        except (OSError, PermissionError):
            continue
    return found


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def resolve_font(candidates: list[str] | str, *, quiet: bool = False) -> str:
    """First candidate that resolves to a real font file on disk.

    MoviePy 2.x hands `font` to Pillow, which wants a path -- a family
    name like "Poppins-Regular" raises rather than falling back. So the
    fallback has to happen here, and has to say what it did: silently
    rendering in the wrong typeface is worse than a loud substitution.
    """
    if isinstance(candidates, str):
        candidates = [candidates]
    files = _font_files()
    if not files:
        raise RuntimeError(
            f"No font files found. Looked in: "
            f"{', '.join(str(d) for d in FONT_SEARCH_DIRS)}"
        )

    for wanted in candidates:
        target = _norm(wanted)
        # Exact stem match first, so "Anton-Regular" never loses to a file
        # that merely contains it.
        for f in files:
            if _norm(f.stem) == target:
                if not quiet and wanted != candidates[0]:
                    print(f"[font] '{candidates[0]}' unavailable; using {f.name}")
                return str(f)
        for f in files:
            if target and target in _norm(f.stem):
                if not quiet:
                    print(f"[font] '{wanted}' matched {f.name}")
                return str(f)

    fallback = sorted(files, key=lambda p: p.name)[0]
    if not quiet:
        print(f"[font] none of {candidates} found; falling back to {fallback.name}")
    return str(fallback)


# --------------------------------------------------------------------------
# 1. The screenshot engine (Playwright)
# --------------------------------------------------------------------------

def capture_element_png(url: str, selector: str, out_path: Path,
                        viewport: tuple[int, int] = (1280, 800),
                        timeout_ms: int = 15_000) -> Path:
    """Transparent PNG of one element on a page.

    `omit_background=True` only produces real transparency when the page
    itself is not painting an opaque background, which most sites are. The
    stylesheet injected below clears html/body so the element's own
    corners, shadows and rounded edges survive into the alpha channel --
    without it you get the element on a white rectangle and the depth
    shadow later has nothing to cut around.
    """
    from playwright.sync_api import sync_playwright

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(
                viewport={"width": viewport[0], "height": viewport[1]},
                device_scale_factor=2,      # retina capture; it gets scaled down
            )
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            page.add_style_tag(content="""
                html, body { background: transparent !important; }
            """)
            element = page.wait_for_selector(selector, timeout=timeout_ms)
            if element is None:
                raise RuntimeError(f"Selector not found: {selector!r} on {url}")
            # Wait for the element to settle rather than sleeping blind.
            element.scroll_into_view_if_needed()
            page.wait_for_timeout(250)
            element.screenshot(path=str(out_path), omit_background=True)
        finally:
            browser.close()

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"Screenshot produced nothing: {out_path}")
    return out_path


# --------------------------------------------------------------------------
# 2. The drop-shadow engine (Pillow)
# --------------------------------------------------------------------------

def add_drop_shadow(src_png: Path, out_png: Path,
                    offset: tuple[int, int] = (15, 15),
                    blur: int = 18,
                    opacity: float = 0.55,
                    color: tuple[int, int, int] = (0, 0, 0)) -> Path:
    """Depth shadow from the asset's own alpha channel.

    The silhouette comes from alpha, not from the bounding box, so a
    rounded card or an irregular shape casts a shadow of its actual shape.

    THE CANVAS IS GROWN FIRST. A blurred, offset shadow needs room that
    the source image does not have, and Pillow will happily clip it at the
    bounds -- giving a shadow with straight razor edges along two sides,
    which looks like a rendering artefact rather than an obvious bug.
    """
    src = Image.open(src_png).convert("RGBA")
    dx, dy = int(offset[0]), int(offset[1])
    # 3 sigma covers virtually all of a Gaussian; anything less clips the tail.
    pad = max(abs(dx), abs(dy)) + blur * 3
    w, h = src.size
    canvas_size = (w + pad * 2, h + pad * 2)

    alpha = src.getchannel("A")
    if opacity < 1.0:
        alpha = alpha.point(lambda a: int(a * opacity))

    # A solid block of the shadow colour, masked to the subject's silhouette.
    shadow = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    silhouette = Image.new("RGBA", (w, h), (*color, 255))
    silhouette.putalpha(alpha)
    shadow.paste(silhouette, (pad + dx, pad + dy), silhouette)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))

    out = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    out = Image.alpha_composite(out, shadow)
    subject = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    subject.paste(src, (pad, pad), src)
    out = Image.alpha_composite(out, subject)

    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_png)
    return out_png


# --------------------------------------------------------------------------
# 3. The animation engine (easing maths)
# --------------------------------------------------------------------------

def ease_out_back(t: float, c1: float = 1.70158) -> float:
    """Overshoot-and-settle easing. t and the result both run 0 -> 1.

    f(t) = 1 + c3(t-1)^3 + c1(t-1)^2,  c3 = c1 + 1

    It passes 1.0, peaks, and comes back down -- which is what makes a pop
    read as physical rather than as a linear zoom.
    """
    c3 = c1 + 1.0
    u = t - 1.0
    return 1.0 + c3 * u * u * u + c1 * u * u


def back_peak(c1: float) -> float:
    """How far ease_out_back overshoots, in closed form.

    Setting df/dt = 0 gives t* = 1 - 2c1/3c3, and substituting back:

        peak = 1 + 4c1^3 / (27 (c1 + 1)^2)

    The textbook c1 = 1.70158 gives exactly 1.10 -- which is worth knowing,
    because a spec asking for a 1.05 peak and a snippet using the textbook
    constant are not the same animation.
    """
    return 1.0 + (4.0 * c1 ** 3) / (27.0 * (c1 + 1.0) ** 2)


def back_constant_for_peak(peak: float, tol: float = 1e-9) -> float:
    """Invert back_peak: the constant that overshoots to exactly *peak*.

    Solved rather than hard-coded so the knob is in human terms. Asking
    for 1.05 should not require anyone to re-derive a magic number, and a
    magic number in the source cannot be checked by reading it.
    """
    if peak <= 1.0:
        return 0.0
    lo, hi = 0.0, 10.0
    while back_peak(hi) < peak:          # pathological asks still terminate
        hi *= 2.0
        if hi > 1e6:
            raise ValueError(f"peak {peak} is unreachable")
    while hi - lo > tol:
        mid = (lo + hi) / 2.0
        if back_peak(mid) < peak:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def pop_scale_at(t: float, *, duration: float, peak: float,
                 floor: float = 0.0) -> float:
    """Scale factor *t* seconds into the pop. Eased, then flat at 1.0.

    *t* is CLIP-LOCAL: the clip is scheduled with `with_start()` so it
    does not exist before the pop, rather than existing at scale zero.

    *floor* is not cosmetic. MoviePy rounds the scaled size to whole
    pixels, so any factor small enough to round to 0 raises
    "height and width must be > 0" from Pillow mid-render -- which is how
    this was found, by running it rather than by reading it.
    """
    if duration <= 0:
        return 1.0
    progress = t / duration
    if progress >= 1.0:
        return 1.0
    c1 = back_constant_for_peak(peak)
    return max(floor, ease_out_back(max(0.0, progress), c1))


# --------------------------------------------------------------------------
# 4. Mocked transcript -- shaped like the real thing
# --------------------------------------------------------------------------

def mock_whisper_words() -> list[dict]:
    """Word-level timestamps, in faster-whisper's shape.

    The repo transcribes with faster-whisper (not openai-whisper), whose
    words are {word, start, end}. Matching that shape means the real call
    replaces this function and nothing downstream changes.
    """
    script = [
        ("This", 0.42, 0.68), ("is", 0.70, 0.84), ("how", 0.86, 1.06),
        ("you", 1.08, 1.24), ("build", 1.26, 1.58), ("a", 1.60, 1.68),
        ("viral", 1.70, 2.06), ("edit", 2.08, 2.44),
        ("without", 2.60, 3.00), ("touching", 3.02, 3.42),
        ("a", 3.44, 3.52), ("timeline", 3.54, 3.98),
    ]
    return [{"word": w, "start": s, "end": e} for w, s, e in script]


def group_words(words: list[dict], per_group: int = 3) -> list[list[dict]]:
    """Chunk words into on-screen blocks.

    Captions read as phrases, not as a stream of single words; the active
    highlight is what moves within the block.
    """
    return [words[i:i + per_group] for i in range(0, len(words), per_group)]


# --------------------------------------------------------------------------
# 5. Clip builders (MoviePy 2.x)
# --------------------------------------------------------------------------

def animated_screenshot_clip(png: Path, cfg: RenderConfig):
    """The shadowed screenshot, popping in on an eased scale."""
    from moviepy import ImageClip

    base = ImageClip(str(png))
    target_w = cfg.width * cfg.shot_width_frac
    base_scale = target_w / base.w

    # Smallest factor that still rounds to a drawable frame. MoviePy
    # scales to whole pixels, so anything under this raises from Pillow.
    floor = 2.0 / (min(base.w, base.h) * base_scale)
    visible = max(0.05, cfg.duration - cfg.pop_start)

    def _scale(t: float) -> float:
        return base_scale * pop_scale_at(
            t, duration=cfg.pop_duration, peak=cfg.pop_peak, floor=floor)

    cx, cy = cfg.shot_center

    def _centered(t: float):
        # Re-centre every frame: the clip's size changes as it scales, and
        # a fixed top-left would make it grow out of its own corner.
        s = _scale(t)
        return (cfg.width * cx - (base.w * s) / 2,
                cfg.height * cy - (base.h * s) / 2)

    # Scheduled with with_start, so before the pop the clip is absent from
    # the composite rather than present at size zero.
    return (base
            .with_duration(visible)
            .resized(_scale)
            .with_position(_centered)
            .with_start(cfg.pop_start))


def font_line_height(font_path: str, font_size: int) -> int:
    """Real line height from the font's own metrics: ascent + descent.

    MoviePy 2.x sizes a TextClip from the drawn text's bounding box and
    comes up SHORT -- for DM Sans at 84px it allocates 83px where the font
    needs 111, so every descender is sliced flat. Found by rendering a word
    with a 'g' in it onto a magenta field and looking at it; no amount of
    reading the clip's reported `.size` would have shown it, because the
    number it reports is the wrong number.
    """
    from PIL import ImageFont
    f = ImageFont.truetype(font_path, font_size)
    ascent, descent = f.getmetrics()
    return ascent + descent


def make_text_clip(text: str, font_path: str, font_size: int, color: str,
                   box_h: int | None = None):
    """A TextClip tall enough for the glyphs it contains.

    Passing an explicit size also makes every word the SAME height, so
    top-aligning a row of them lands them on a shared baseline instead of
    stepping up and down with each word's ascenders.
    """
    from moviepy import TextClip

    probe = TextClip(text=text, font=font_path, font_size=font_size, color=color)
    needed = box_h or font_line_height(font_path, font_size)
    if probe.h >= needed:
        return probe
    return TextClip(text=text, font=font_path, font_size=font_size, color=color,
                    size=(probe.w + 2, needed), vertical_align="center",
                    horizontal_align="center")


def caption_clips(groups: list[list[dict]], cfg: RenderConfig) -> list:
    """Two layers per word block: white base, gold active word on a slab.

    The active layer is a separate clip windowed to one word's start/end
    and placed at that word's x offset inside the block, so the highlight
    travels along the phrase instead of the whole line changing colour.
    """
    from moviepy import ColorClip, CompositeVideoClip, TextClip

    base_font = resolve_font(cfg.caption_font)
    hot_font = resolve_font(cfg.active_font)
    pad_x, pad_y = cfg.block_pad
    # One box height for BOTH faces, so a gold Bebas word and the white
    # DM Sans words around it share a vertical centre instead of each
    # sitting wherever its own bounding box happens to fall.
    line_h = max(font_line_height(base_font, cfg.caption_size),
                 font_line_height(hot_font, cfg.caption_size))
    out = []

    for group in groups:
        if not group:
            continue
        g_start = float(group[0]["start"])
        g_end = float(group[-1]["end"])
        g_dur = max(0.25, g_end - g_start)

        # Render BOTH faces for every word and reserve the wider.
        #
        # The brief said to place the active layer "exactly on top" of the
        # base one. That only works when the two share metrics. With two
        # different faces -- a condensed all-caps Bebas over a humanist
        # DM Sans -- the gold word is narrower than the white one beneath
        # it, so the base text shows out of both sides and the line reads
        # as a double exposure. Seen by rendering it and looking.
        #
        # So the word is SWAPPED, not stacked, and each slot is as wide as
        # the wider of the two so nothing reflows as the highlight travels.
        base_pieces, hot_pieces, slots = [], [], []
        for w in group:
            b = make_text_clip(w["word"], base_font, cfg.caption_size,
                               cfg.caption_color, box_h=line_h)
            h = make_text_clip(w["word"], hot_font, cfg.caption_size,
                               cfg.active_color, box_h=line_h)
            base_pieces.append(b)
            hot_pieces.append(h)
            slots.append(max(b.w, h.w))
        space = cfg.caption_size * 0.30
        total_w = sum(slots) + space * (len(slots) - 1)

        block_w = int(total_w + pad_x * 2)
        block_h = int(line_h + pad_y * 2)
        slab = (ColorClip(size=(block_w, block_h),
                          color=_hex_to_rgb(cfg.block_color))
                .with_opacity(cfg.block_opacity)
                .with_duration(g_dur))

        layers = [slab]
        x = float(pad_x)
        for w, base_tc, hot_tc, slot in zip(group, base_pieces, hot_pieces, slots):
            hit_in = max(0.0, min(g_dur, float(w["start"]) - g_start))
            hit_out = max(hit_in + 0.01, min(g_dur, float(w["end"]) - g_start))

            # Each face centred in its slot, so a narrow condensed word does
            # not sit flush-left in a slot sized for a wider one.
            def _place(clip, at):
                return clip.with_position((at + (slot - clip.w) / 2.0, pad_y))

            # White, everywhere EXCEPT this word's own window -- which is
            # what stops the two faces overlapping.
            if hit_in > 0.01:
                layers.append(_place(base_tc.with_start(0).with_end(hit_in), x))
            if hit_out < g_dur - 0.01:
                layers.append(_place(
                    base_tc.copy().with_start(hit_out).with_end(g_dur), x))
            layers.append(_place(hot_tc.with_start(hit_in).with_end(hit_out), x))
            x += slot + space

        block = (CompositeVideoClip(layers, size=(block_w, block_h))
                 .with_start(g_start)
                 .with_duration(g_dur)
                 .with_position(("center", int(cfg.height * cfg.caption_y_frac))))
        out.append(block)

    return out


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


# --------------------------------------------------------------------------
# 6. Compositing
# --------------------------------------------------------------------------

def build_video(cfg: RenderConfig, *, screenshot_png: Path | None = None,
                words: list[dict] | None = None) -> Path:
    """Base video -> captions -> shadowed screenshot on top.

    *screenshot_png* lets a caller skip the Playwright step (tests, or a
    payload that supplies its own asset). *words* likewise replaces the
    mock with a real transcript.
    """
    from moviepy import ColorClip, CompositeVideoClip

    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    if screenshot_png is None:
        raw = capture_element_png(
            cfg.url, cfg.selector, BUILD_DIR / "shot_raw.png",
            viewport=cfg.viewport, timeout_ms=cfg.capture_timeout_ms)
    else:
        raw = Path(screenshot_png)

    shadowed = add_drop_shadow(
        raw, BUILD_DIR / "shot_shadow.png",
        offset=cfg.shadow_offset, blur=cfg.shadow_blur,
        opacity=cfg.shadow_opacity, color=cfg.shadow_color)

    # Stand-in for the raw footage. Swap for VideoFileClip(path) later --
    # nothing else in this function cares which it is.
    base = ColorClip(size=(cfg.width, cfg.height),
                     color=cfg.base_color).with_duration(cfg.duration)

    captions = caption_clips(
        group_words(words if words is not None else mock_whisper_words()), cfg)
    shot = animated_screenshot_clip(shadowed, cfg)

    final = CompositeVideoClip([base, *captions, shot],
                               size=(cfg.width, cfg.height))
    final = final.with_duration(cfg.duration)

    out = Path(cfg.out_path)
    if not out.is_absolute():
        out = HERE / out
    out.parent.mkdir(parents=True, exist_ok=True)
    final.write_videofile(
        str(out), fps=cfg.fps, codec="libx264", audio=False,
        preset="medium", logger=None)
    final.close()
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def load_payload(source: str | None) -> dict | None:
    if not source:
        return None
    if source == "-":
        return json.load(sys.stdin)
    return json.loads(Path(source).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--payload", help="JSON file with RenderConfig overrides, or - for stdin")
    ap.add_argument("--screenshot", help="Use this PNG instead of capturing one")
    ap.add_argument("--out", help="Override the output path")
    args = ap.parse_args(argv)

    cfg = RenderConfig.from_payload(load_payload(args.payload))
    if args.out:
        cfg.out_path = args.out

    out = build_video(cfg, screenshot_png=args.screenshot)
    print(f"[done] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
