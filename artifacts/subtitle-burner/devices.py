"""Graphics devices: animated UI mockups and mental-model diagrams.

Each device renders to a transparent VP9 WebM (yuva420p). One file serves
both ends: browsers play VP9 alpha natively for the timeline preview, and the
FFmpeg overlay pass decodes it with libvpx so the alpha survives into the
burn. Renders are cached by a hash of everything that affects the pixels, so
re-rendering a timeline never redraws an unchanged device.

Frames are drawn with Pillow. Static pieces (bubbles, cards, circles) are
drawn once as sprites at 2x and downscaled, then each frame only transforms
and composites them, which keeps a 3-second device to a second or two.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

import tokens

DEVICE_VERSION = 3
FONTS_DIR = Path(__file__).resolve().parent / "fonts"

# box: x, y, w, h as fractions of the canvas. fields: label prompts for the UI.
CATALOG = {
    "chat_bubbles": {
        "label": "Chat bubbles", "family": "ui", "dur": 3.2,
        "box": (0.06, 0.14, 0.88, 0.40),
        "fields": ["Message", "Reply", "Reply 2"],
        "blurb": "iMessage-style thread that pops in line by line",
    },
    "notification": {
        "label": "Notification", "family": "ui", "dur": 2.8,
        "box": (0.04, 0.05, 0.92, 0.15),
        "fields": ["App / title", "Body"],
        "blurb": "Banner that drops in from the top and slides away",
    },
    "search_bar": {
        "label": "Search typing", "family": "ui", "dur": 3.4,
        "box": (0.06, 0.16, 0.88, 0.30),
        "fields": ["Query", "Result 1", "Result 2"],
        "blurb": "A query typed into a search bar, then a highlighted result",
    },
    "subscribe_button": {
        "label": "Subscribe button", "family": "ui", "dur": 3.6,
        "box": (0.04, 0.60, 0.92, 0.16),
        "fields": ["Channel name", "Subscribers (1.2M)"],
        "blurb": "A mouse glides to a red Subscribe button, clicks, and the bell rings",
    },
    "toggle_meter": {
        "label": "Toggle + meter", "family": "ui", "dur": 2.8,
        "box": (0.10, 0.18, 0.80, 0.22),
        "fields": ["Label", "Value 0-100"],
        "blurb": "A switch flips on and a meter fills",
    },
    "step_tracker": {
        "label": "Step tracker", "family": "mental", "dur": 3.8,
        "box": (0.04, 0.10, 0.92, 0.20),
        "fields": ["Step 1", "Step 2", "Step 3", "Step 4"],
        "blurb": "Numbered steps that light up as you move through them",
    },
    "venn": {
        "label": "Venn", "family": "mental", "dur": 3.4,
        "box": (0.06, 0.12, 0.88, 0.36),
        "fields": ["Left", "Right", "Overlap"],
        "blurb": "Two ideas slide together; the overlap lights up",
    },
    "node_map": {
        "label": "Node map", "family": "mental", "dur": 3.8,
        "box": (0.04, 0.10, 0.92, 0.38),
        "fields": ["Node 1", "Node 2", "Node 3", "Node 4"],
        "blurb": "Glowing nodes wired together by animated curves",
    },
    "timeline_scrubber": {
        "label": "Timeline scrubber", "family": "mental", "dur": 3.8,
        "box": (0.04, 0.66, 0.92, 0.14),
        "fields": ["Milestone 1", "Milestone 2", "Milestone 3", "Milestone 4"],
        "blurb": "A playhead snaps from milestone to milestone",
    },
    "counter": {
        "label": "Stat counter", "family": "stat", "dur": 2.8,
        "box": (0.10, 0.14, 0.80, 0.22),
        "fields": ["Number (40%, $2M, 3x)", "Caption"],
        "blurb": "A number that counts up with a caption",
    },
    "kinetic_text": {
        "label": "Kinetic text", "family": "text", "dur": 2.4,
        "box": (0.04, 0.22, 0.92, 0.34),
        "fields": ["Text"],
        "blurb": "Big words that pop in one at a time (works behind you)",
    },
}

# Interface mockups only read as real apps in a neutral UI face, whatever
# display font the style pack uses for captions.
UI_FONT = "DM Sans"
# Read from tokens.py, which is the one place the brand is declared. This
# list agreed with it by coincidence, not by construction, so a change to
# the brand reached devices and left Remotion behind.
DEFAULT_PALETTE = tokens.palette()


# ---- small helpers ---------------------------------------------------------

def _rgb(hex_color: str, alpha: int = 255) -> tuple:
    s = (hex_color or "#FFFFFF").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        s = "FFFFFF"
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), alpha)


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def ease_out_cubic(u: float) -> float:
    u = _clamp(u)
    return 1 - (1 - u) ** 3


def ease_out_back(u: float, s: float = 1.70158) -> float:
    """Spring-ish overshoot, the 'pop' every UI animation leans on."""
    u = _clamp(u)
    c3 = s + 1
    return 1 + c3 * (u - 1) ** 3 + s * (u - 1) ** 2


def ease_in_out(u: float) -> float:
    u = _clamp(u)
    return 3 * u * u - 2 * u * u * u


def _font(name: str | None, size: int) -> ImageFont.FreeTypeFont:
    size = max(8, int(size))
    candidates = []
    if name:
        key = re.sub(r"[^a-z]", "", name.lower())
        for p in sorted(FONTS_DIR.glob("*.[ot]tf")):
            if key and key[:6] in re.sub(r"[^a-z]", "", p.stem.lower()):
                candidates.append(p)
    candidates += [FONTS_DIR / "DM Sans.ttf", FONTS_DIR / "Montserrat-Black.ttf"]
    for p in candidates:
        try:
            return ImageFont.truetype(str(p), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _bold(size: int) -> ImageFont.FreeTypeFont:
    for name in ("Montserrat-Black.ttf", "Anton-Regular.ttf", "DM Sans.ttf"):
        try:
            return ImageFont.truetype(str(FONTS_DIR / name), max(8, int(size)))
        except OSError:
            continue
    return ImageFont.load_default()


def _text_w(font, text: str) -> int:
    if not text:
        return 0
    b = font.getbbox(text)
    return b[2] - b[0]


def _wrap(font, text: str, max_w: int, max_lines: int = 3) -> list[str]:
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if _text_w(font, trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and len(" ".join(lines)) < len(text or ""):
        lines[-1] = lines[-1].rstrip(".,") + "…"
    return lines or [""]


def _rounded(size: tuple, radius: int, fill, outline=None, width=0, ss: int = 2) -> Image.Image:
    """Anti-aliased rounded rectangle sprite (drawn at ss x then downscaled)."""
    w, h = max(2, int(size[0])), max(2, int(size[1]))
    big = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=radius * ss, fill=fill,
                        outline=outline, width=width * ss)
    return big.resize((w, h), Image.LANCZOS)


def _circle(diam: int, fill, outline=None, width=0, ss: int = 2) -> Image.Image:
    d = max(2, int(diam))
    big = Image.new("RGBA", (d * ss, d * ss), (0, 0, 0, 0))
    dr = ImageDraw.Draw(big)
    dr.ellipse([0, 0, d * ss - 1, d * ss - 1], fill=fill, outline=outline, width=width * ss)
    return big.resize((d, d), Image.LANCZOS)


def _with_alpha(img: Image.Image, alpha: float) -> Image.Image:
    if alpha >= 0.999:
        return img
    a = img.getchannel("A").point(lambda v: int(v * _clamp(alpha)))
    out = img.copy()
    out.putalpha(a)
    return out


def _paste(frame: Image.Image, sprite: Image.Image, cx: float, cy: float,
           scale: float = 1.0, alpha: float = 1.0) -> None:
    """Composite *sprite* centred on (cx, cy) with a scale and opacity."""
    if alpha <= 0.003 or scale <= 0.01:
        return
    s = sprite
    if abs(scale - 1.0) > 0.003:
        nw, nh = max(1, int(s.width * scale)), max(1, int(s.height * scale))
        s = s.resize((nw, nh), Image.BILINEAR)
    s = _with_alpha(s, alpha)
    x, y = int(round(cx - s.width / 2)), int(round(cy - s.height / 2))
    sx, sy = max(0, -x), max(0, -y)
    ex, ey = min(s.width, frame.width - x), min(s.height, frame.height - y)
    if ex <= sx or ey <= sy:
        return
    frame.alpha_composite(s.crop((sx, sy, ex, ey)), (x + sx, y + sy))


def _pop(t: float, t0: float, d: float = 0.38) -> tuple[float, float]:
    """(scale, alpha) for an element that pops in at t0."""
    if t < t0:
        return 0.0, 0.0
    u = (t - t0) / d
    return 0.55 + 0.45 * ease_out_back(u), _clamp(u * 2.2)


def _text_sprite(text: str, font, fill, stroke=0, stroke_fill=(0, 0, 0, 200)) -> Image.Image:
    b = font.getbbox(text or " ", stroke_width=stroke)
    w, h = max(2, b[2] - b[0] + 4), max(2, b[3] - b[1] + 4)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((2 - b[0], 2 - b[1]), text or "", font=font, fill=fill,
                             stroke_width=stroke, stroke_fill=stroke_fill)
    return img


def _panel(w: int, h: int, palette: list, alpha: int = 190, radius: int = 28) -> Image.Image:
    dark = _rgb(palette[3] if len(palette) > 3 else "#0F1220", alpha)
    return _rounded((w, h), radius, fill=dark, outline=(255, 255, 255, 38), width=2)


def _labels(labels, n: int, defaults: list[str]) -> list[str]:
    out = [str(x).strip() for x in (labels or []) if str(x or "").strip()]
    if not out:
        out = list(defaults)
    return out[:n]


# ---- device painters ---------------------------------------------------------
# Each painter takes (w, h, labels, palette, font_name, dur) and returns a
# function draw(frame, t) that paints one frame onto a transparent canvas.

def _chat(w, h, labels, pal, font_name, dur):
    msgs = _labels(labels, 4, ["Did you see the numbers?", "wait what?!"])
    fs = max(18, int(h / 11))
    font = _font(UI_FONT, fs)
    pad = int(fs * 0.7)
    maxw = int(w * 0.72)
    sprites = []
    y = int(fs * 0.4)
    for i, m in enumerate(msgs):
        mine = i % 2 == 1
        lines = _wrap(font, m, maxw - pad * 2)
        tw = max(_text_w(font, ln) for ln in lines)
        lh = int(fs * 1.25)
        bw, bh = tw + pad * 2, lh * len(lines) + pad * 2 - int(fs * 0.25)
        fill = _rgb("#0A84FF") if mine else _rgb("#E9E9EB")
        txt = (255, 255, 255, 255) if mine else (17, 17, 17, 255)
        bub = _rounded((bw, bh), int(fs * 0.9), fill=fill)
        d = ImageDraw.Draw(bub)
        for k, ln in enumerate(lines):
            d.text((pad, pad - int(fs * 0.15) + k * lh), ln, font=font, fill=txt)
        cx = w - bw / 2 - int(w * 0.02) if mine else bw / 2 + int(w * 0.02)
        sprites.append((bub, cx, y + bh / 2, mine))
        y += bh + int(fs * 0.55)
    starts = [0.15 + i * min(0.85, (dur - 0.9) / max(1, len(sprites))) for i in range(len(sprites))]
    dot = _circle(int(fs * 0.42), (120, 120, 128, 255))
    typing = _rounded((int(fs * 2.6), int(fs * 1.5)), int(fs * 0.75), fill=_rgb("#E9E9EB"))

    def draw(frame, t):
        for i, (spr, cx, cy, mine) in enumerate(sprites):
            t0 = starts[i]
            if i > 0 and not mine and t0 - 0.45 <= t < t0:
                tx = typing.width / 2 + int(w * 0.02)
                _paste(frame, typing, tx, cy - spr.height / 2 + typing.height / 2)
                for k in range(3):
                    bob = math.sin((t * 9) - k * 0.9) * fs * 0.12
                    _paste(frame, dot, tx - fs * 0.6 + k * fs * 0.6,
                           cy - spr.height / 2 + typing.height / 2 + bob)
            sc, al = _pop(t, t0)
            rise = (1 - ease_out_cubic((t - t0) / 0.35)) * fs * 0.8 if t >= t0 else 0
            _paste(frame, spr, cx, cy + rise, sc, al)
    return draw


def _notification(w, h, labels, pal, font_name, dur):
    title, body = (_labels(labels, 2, ["Messages", "You have a new message"]) + [""])[:2]
    card = _rounded((w - 4, h - 4), int(h * 0.22), fill=(250, 250, 252, 238))
    d = ImageDraw.Draw(card)
    icon = int(h * 0.42)
    ic = _rounded((icon, icon), int(icon * 0.26), fill=_rgb(pal[0]))
    card.alpha_composite(ic, (int(h * 0.16), int((h - icon) / 2)))
    fs_t, fs_b = max(14, int(h * 0.2)), max(12, int(h * 0.18))
    ft, fb = _bold(fs_t), _font(UI_FONT, fs_b)
    tx = int(h * 0.16) + icon + int(h * 0.16)
    d.text((tx, int(h * 0.17)), title[:40], font=ft, fill=(20, 20, 24, 255))
    d.text((w - int(h * 0.2) - _text_w(fb, "now"), int(h * 0.2)), "now", font=fb, fill=(130, 130, 138, 255))
    for k, ln in enumerate(_wrap(fb, body, w - tx - int(h * 0.2), 2)):
        d.text((tx, int(h * 0.17) + fs_t + int(h * 0.08) + k * int(fs_b * 1.2)), ln, font=fb,
               fill=(60, 60, 68, 255))

    def draw(frame, t):
        u_in = ease_out_back((t - 0.1) / 0.45, 1.2) if t >= 0.1 else 0
        u_out = ease_in_out((t - (dur - 0.5)) / 0.4) if t > dur - 0.5 else 0
        y = -h + (h / 2 + h) * u_in - (h * 1.2) * u_out
        _paste(frame, card, w / 2, y, 1.0, 1.0 if t >= 0.1 else 0)
    return draw


def _search(w, h, labels, pal, font_name, dur):
    ls = _labels(labels, 3, ["how to grow on short form", "Post daily for 90 days", "Hooks matter most"])
    query = ls[0]
    results = ls[1:] or ["Top result"]
    bar_h = int(h * 0.30)
    fs = max(14, int(bar_h * 0.42))
    font = _font(UI_FONT, fs)
    room = w - int(bar_h * 0.95) - int(bar_h * 0.4)
    while fs > 14 and _text_w(font, query) > room:
        fs = int(fs * 0.92)
        font = _font(UI_FONT, fs)
    if _text_w(font, query) > room:
        while query and _text_w(font, query + "…") > room:
            query = query[:-1]
        query = query.rstrip() + "…"
    bar = _rounded((w - 4, bar_h), bar_h // 2, fill=(255, 255, 255, 245),
                   outline=(220, 220, 226, 255), width=2)
    d = ImageDraw.Draw(bar)
    r = int(bar_h * 0.2)
    cx0, cy0 = int(bar_h * 0.5), bar_h // 2 - int(r * 0.2)
    d.ellipse([cx0 - r, cy0 - r, cx0 + r, cy0 + r], outline=(120, 120, 130, 255), width=max(2, r // 4))
    d.line([cx0 + int(r * 0.7), cy0 + int(r * 0.7), cx0 + int(r * 1.5), cy0 + int(r * 1.5)],
           fill=(120, 120, 130, 255), width=max(2, r // 4))
    text_x = int(bar_h * 0.95)
    res_h = int(h * 0.19)
    fr = _font(UI_FONT, max(12, int(res_h * 0.36)))
    res_sprites = []
    for i, txt in enumerate(results[:2]):
        card = _rounded((w - int(w * 0.04), res_h), int(res_h * 0.25), fill=(255, 255, 255, 232))
        dd = ImageDraw.Draw(card)
        dd.text((int(res_h * 0.4), int(res_h * 0.18)), txt[:60], font=fr, fill=(26, 13, 171, 255))
        dd.rounded_rectangle([int(res_h * 0.4), int(res_h * 0.68), int(w * 0.6), int(res_h * 0.78)],
                             radius=4, fill=(205, 205, 212, 255))
        res_sprites.append(card)
    hl = _rgb(pal[0], 150)
    type_start, cps = 0.25, 15.0
    type_end = type_start + len(query) / cps

    def draw(frame, t):
        sc, al = _pop(t, 0.0, 0.3)
        _paste(frame, bar, w / 2, bar_h / 2 + 2, sc, al)
        n = int(_clamp((t - type_start) * cps, 0, len(query)))
        shown = query[:n]
        dd = ImageDraw.Draw(frame)
        dd.text((text_x, bar_h / 2 - fs * 0.62 + 2), shown, font=font, fill=(30, 30, 36, 255))
        if (t * 2) % 1 < 0.6 or n < len(query):
            cx = text_x + _text_w(font, shown) + 3
            dd.line([cx, bar_h * 0.27, cx, bar_h * 0.73], fill=_rgb(pal[1]), width=3)
        for i, spr in enumerate(res_sprites):
            t0 = type_end + 0.2 + i * 0.25
            if t < t0:
                continue
            u = ease_out_cubic((t - t0) / 0.35)
            cy = bar_h + int(h * 0.06) + i * (res_h + int(h * 0.04)) + res_h / 2
            _paste(frame, spr, w / 2, cy + (1 - u) * 20, 1.0, u)
            if i == 0 and t > t0 + 0.4:
                p = ease_out_cubic((t - t0 - 0.4) / 0.5)
                x0 = int(w * 0.02 + res_h * 0.35)
                x1 = x0 + int((_text_w(fr, results[0][:60]) + 8) * p)
                y0 = int(cy - res_h / 2 + res_h * 0.12)
                ov = Image.new("RGBA", frame.size, (0, 0, 0, 0))
                ImageDraw.Draw(ov).rectangle([x0, y0, x1, y0 + int(res_h * 0.46)], fill=hl)
                frame.alpha_composite(ov)
    return draw


def _toggle(w, h, labels, pal, font_name, dur):
    ls = _labels(labels, 2, ["Focus mode", "85"])
    label = ls[0]
    try:
        value = _clamp(float(re.sub(r"[^\d.]", "", ls[1] if len(ls) > 1 else "85") or 85) / 100.0)
    except ValueError:
        value = 0.85
    if len(ls) > 1 and ls[1].strip().lower() in ("on", "off"):
        value = 1.0 if ls[1].strip().lower() == "on" else 0.0
    panel = _panel(w, h, pal)
    fs = max(14, int(h * 0.2))
    font = _bold(fs)
    tw, th = int(h * 0.62), int(h * 0.34)
    track_off = _rounded((tw, th), th // 2, fill=(90, 90, 100, 255))
    track_on = _rounded((tw, th), th // 2, fill=_rgb(pal[0]))
    knob = _circle(int(th * 0.84), (255, 255, 255, 255))
    room = int(w * 0.84) - tw - int(w * 0.04)
    while fs > 12 and _text_w(font, label[:28]) > room:
        fs = int(fs * 0.9)
        font = _bold(fs)
    lbl = _text_sprite(label[:28], font, (255, 255, 255, 255))
    mw, mh = int(w * 0.84), int(h * 0.12)
    meter_bg = _rounded((mw, mh), mh // 2, fill=(255, 255, 255, 50))
    pct_font = _bold(max(12, int(h * 0.13)))

    def draw(frame, t):
        sc, al = _pop(t, 0.0, 0.3)
        _paste(frame, panel, w / 2, h / 2, sc, al)
        if t < 0.15:
            return
        row_y = h * 0.33
        _paste(frame, lbl, int(w * 0.08) + lbl.width / 2, row_y)
        u = ease_in_out((t - 0.6) / 0.35) if value > 0 else 0
        tx = w - int(w * 0.08) - tw / 2
        _paste(frame, track_off, tx, row_y)
        _paste(frame, track_on, tx, row_y, 1.0, u)
        kx = tx - tw / 2 + th / 2 + (tw - th) * u
        _paste(frame, knob, kx, row_y)
        my = h * 0.62
        _paste(frame, meter_bg, w / 2, my)
        p = value * ease_out_cubic((t - 0.8) / 1.2)
        if p > 0.01:
            fill = _rounded((max(mh, int(mw * p)), mh), mh // 2, fill=_rgb(pal[0]))
            _paste(frame, fill, (w - mw) / 2 + fill.width / 2, my)
        pct = _text_sprite(f"{int(round(p * 100))}%", pct_font, (255, 255, 255, 230))
        _paste(frame, pct, (w + mw) / 2 - pct.width / 2, my + mh * 1.7)
    return draw


def _steps(w, h, labels, pal, font_name, dur):
    ls = _labels(labels, 5, ["One", "Two", "Three"])
    n = max(2, len(ls))
    ls = (ls + ["", "", ""])[:n]
    panel = _panel(w, h, pal)
    r = int(min(h * 0.22, w / (n * 2.6)))
    fs = max(12, int(r * 0.95))
    nf, lf = _bold(fs), _font(font_name, max(12, int(h * 0.12)))
    xs = [w * 0.1 + i * (w * 0.8) / (n - 1) for i in range(n)]
    cy = h * 0.40
    idle = [_circle(r * 2, (255, 255, 255, 40), outline=(255, 255, 255, 140), width=3) for _ in range(n)]
    done = [_circle(r * 2, _rgb(pal[0])) for _ in range(n)]
    nums_idle = [_text_sprite(str(i + 1), nf, (255, 255, 255, 230)) for i in range(n)]
    nums_done = [_text_sprite(str(i + 1), nf, _rgb(pal[3])) for i in range(n)]
    lbls = [_text_sprite(ls[i][:16], lf, (255, 255, 255, 235)) for i in range(n)]
    seg = (dur - 1.0) / n

    def draw(frame, t):
        sc, al = _pop(t, 0.0, 0.3)
        _paste(frame, panel, w / 2, h / 2, sc, al)
        if t < 0.15:
            return
        d = ImageDraw.Draw(frame)
        d.line([xs[0], cy, xs[-1], cy], fill=(255, 255, 255, 70), width=max(3, r // 5))
        prog = _clamp((t - 0.35) / (seg * (n - 1) + 1e-6))
        xe = xs[0] + (xs[-1] - xs[0]) * ease_in_out(prog)
        d.line([xs[0], cy, xe, cy], fill=_rgb(pal[0]), width=max(3, r // 5))
        for i in range(n):
            reach = 0.35 + i * seg
            s_in, a_in = _pop(t, 0.15 + i * 0.08, 0.3)
            _paste(frame, idle[i], xs[i], cy, s_in, a_in)
            if t >= reach:
                s2, a2 = _pop(t, reach, 0.32)
                _paste(frame, done[i], xs[i], cy, s2, a2)
                _paste(frame, nums_done[i], xs[i], cy, s2, a2)
            else:
                _paste(frame, nums_idle[i], xs[i], cy, s_in, a_in)
            _paste(frame, lbls[i], xs[i], h * 0.78, 1.0, a_in)
    return draw


def _venn(w, h, labels, pal, font_name, dur):
    ls = (_labels(labels, 3, ["Skill", "Demand", "Money"]) + ["", "", ""])[:3]
    panel = _panel(w, h, pal)
    r = int(min(h * 0.42, w * 0.26))
    a = _circle(r * 2, _rgb(pal[0], 120), outline=_rgb(pal[0], 230), width=3)
    b = _circle(r * 2, _rgb(pal[1], 120), outline=_rgb(pal[1], 230), width=3)
    lf = _bold(max(14, int(r * 0.24)))
    la = _text_sprite(ls[0][:14], lf, (255, 255, 255, 255), stroke=2)
    lb = _text_sprite(ls[1][:14], lf, (255, 255, 255, 255), stroke=2)
    lo = _text_sprite(ls[2][:14], _bold(max(12, int(r * 0.2))), _rgb(pal[3]))
    chip = _rounded((lo.width + int(r * 0.25), lo.height + int(r * 0.12)), int(r * 0.12),
                    fill=(255, 255, 255, 240))
    cy = h / 2
    final_off = r * 0.62

    def lens(off):
        m1 = Image.new("L", (w, h), 0)
        m2 = Image.new("L", (w, h), 0)
        ImageDraw.Draw(m1).ellipse([w / 2 - off - r, cy - r, w / 2 - off + r, cy + r], fill=255)
        ImageDraw.Draw(m2).ellipse([w / 2 + off - r, cy - r, w / 2 + off + r, cy + r], fill=255)
        inter = ImageChops.multiply(m1, m2)
        glow = Image.new("RGBA", (w, h), _rgb(pal[2], 0))
        glow.putalpha(inter.point(lambda v: int(v * 0.55)))
        return glow

    final_lens = lens(final_off)

    def draw(frame, t):
        sc, al = _pop(t, 0.0, 0.3)
        _paste(frame, panel, w / 2, h / 2, sc, al)
        u = ease_out_back((t - 0.2) / 0.8, 1.1) if t > 0.2 else 0
        off = final_off + (w * 0.45 - final_off) * (1 - u)
        alpha = _clamp((t - 0.2) * 3)
        _paste(frame, a, w / 2 - off, cy, 1.0, alpha)
        _paste(frame, b, w / 2 + off, cy, 1.0, alpha)
        _paste(frame, la, w / 2 - off - r * 0.42, cy - r * 0.05, 1.0, alpha)
        _paste(frame, lb, w / 2 + off + r * 0.42, cy - r * 0.05, 1.0, alpha)
        if t > 1.15:
            pulse = 0.75 + 0.25 * math.sin((t - 1.15) * 5)
            frame.alpha_composite(_with_alpha(final_lens, _clamp((t - 1.15) * 3) * pulse))
            s2, a2 = _pop(t, 1.25, 0.35)
            _paste(frame, chip, w / 2, cy + r * 0.62, s2, a2)
            _paste(frame, lo, w / 2, cy + r * 0.62, s2, a2)
    return draw


def _nodes(w, h, labels, pal, font_name, dur):
    ls = _labels(labels, 5, ["Idea", "Build", "Ship", "Learn"])
    n = max(2, len(ls))
    panel = _panel(w, h, pal)
    r = int(min(h * 0.11, w * 0.06))
    pts = []
    for i in range(n):
        x = w * 0.12 + i * (w * 0.76) / (n - 1)
        y = h * (0.32 if i % 2 == 0 else 0.62)
        pts.append((x, y))
    glow = _circle(int(r * 3.4), _rgb(pal[0], 60))
    node = _circle(r * 2, _rgb(pal[0]), outline=(255, 255, 255, 230), width=3)
    lf = _font(font_name, max(12, int(h * 0.075)))
    lbls = [_text_sprite(s[:14], lf, (255, 255, 255, 240)) for s in ls[:n]]
    edges = list(zip(range(n - 1), range(1, n)))
    if n >= 4:
        edges.append((0, n - 1))
    seg = (dur - 1.0) / max(1, len(edges))

    def bez(p0, p1, u):
        mx, my = (p0[0] + p1[0]) / 2, min(p0[1], p1[1]) - h * 0.18
        a = (1 - u) ** 2
        return (a * p0[0] + 2 * (1 - u) * u * mx + u * u * p1[0],
                a * p0[1] + 2 * (1 - u) * u * my + u * u * p1[1])

    def draw(frame, t):
        sc, al = _pop(t, 0.0, 0.3)
        _paste(frame, panel, w / 2, h / 2, sc, al)
        d = ImageDraw.Draw(frame)
        for k, (i, j) in enumerate(edges):
            p = ease_in_out((t - 0.35 - k * seg) / (seg * 1.1))
            if p <= 0:
                continue
            steps = max(2, int(24 * p))
            line = [bez(pts[i], pts[j], p * s / steps) for s in range(steps + 1)]
            d.line(line, fill=_rgb(pal[1], 220), width=max(3, r // 4), joint="curve")
            hx, hy = line[-1]
            d.ellipse([hx - r * 0.25, hy - r * 0.25, hx + r * 0.25, hy + r * 0.25], fill=_rgb(pal[2]))
        for i, (x, y) in enumerate(pts):
            s, a = _pop(t, 0.2 + i * (dur - 1.2) / n, 0.35)
            _paste(frame, glow, x, y, s * (1 + 0.08 * math.sin(t * 6 + i)), a)
            _paste(frame, node, x, y, s, a)
            _paste(frame, lbls[i], x, y + r * 2.0, 1.0, a)
    return draw


def _scrubber(w, h, labels, pal, font_name, dur):
    ls = _labels(labels, 5, ["2019", "2021", "2023", "Now"])
    n = max(2, len(ls))
    panel = _panel(w, h, pal, radius=int(h * 0.2))
    xs = [w * 0.08 + i * (w * 0.84) / (n - 1) for i in range(n)]
    cy = h * 0.40
    dot_idle = _circle(int(h * 0.14), (255, 255, 255, 90))
    dot_done = _circle(int(h * 0.16), _rgb(pal[0]))
    knob = _circle(int(h * 0.26), (255, 255, 255, 255), outline=_rgb(pal[0]), width=4)
    lf = _font(font_name, max(12, int(h * 0.18)))
    lbls = [_text_sprite(s[:14], lf, (255, 255, 255, 235)) for s in ls[:n]]
    seg = (dur - 0.9) / (n - 1)

    def draw(frame, t):
        sc, al = _pop(t, 0.0, 0.3)
        _paste(frame, panel, w / 2, h / 2, 1.0, al)
        if t < 0.1:
            return
        d = ImageDraw.Draw(frame)
        d.line([xs[0], cy, xs[-1], cy], fill=(255, 255, 255, 70), width=max(3, int(h * 0.04)))
        k = int(_clamp((t - 0.3) / seg, 0, n - 1))
        u = ease_out_back(((t - 0.3) - k * seg) / (seg * 0.6), 1.3) if k < n - 1 else 1.0
        x = xs[k] + (xs[min(k + 1, n - 1)] - xs[k]) * (u if k < n - 1 and t > 0.3 else 0)
        x = min(x, xs[-1])
        d.line([xs[0], cy, x, cy], fill=_rgb(pal[0]), width=max(3, int(h * 0.04)))
        for i in range(n):
            reached = x >= xs[i] - 2
            _paste(frame, dot_done if reached else dot_idle, xs[i], cy)
            _paste(frame, lbls[i], xs[i], h * 0.76, 1.0 if reached else 0.85,
                   1.0 if reached else 0.55)
        _paste(frame, knob, x, cy)
    return draw


def _counter(w, h, labels, pal, font_name, dur):
    ls = (_labels(labels, 2, ["40%", "growth in 30 days"]) + [""])[:2]
    raw = ls[0]
    m = re.search(r"(-?\d[\d,]*\.?\d*)", raw)
    target = float(m.group(1).replace(",", "")) if m else 100.0
    prefix = raw[:m.start()] if m else ""
    suffix = raw[m.end():] if m else ""
    decimals = len(m.group(1).split(".")[1]) if m and "." in m.group(1) else 0
    panel = _panel(w, h, pal)
    nf = _bold(max(24, int(h * 0.46)))
    cf = _font(font_name, max(12, int(h * 0.15)))
    cap = _text_sprite(ls[1][:40], cf, (255, 255, 255, 230))

    def draw(frame, t):
        sc, al = _pop(t, 0.0, 0.3)
        _paste(frame, panel, w / 2, h / 2, sc, al)
        if t < 0.15:
            return
        u = ease_out_cubic((t - 0.2) / 1.3)
        val = target * u
        txt = f"{prefix}{val:,.{decimals}f}{suffix}"
        num = _text_sprite(txt, nf, _rgb(pal[0]), stroke=2, stroke_fill=(0, 0, 0, 120))
        bump = 1.0 + 0.06 * math.sin(_clamp((t - 1.4) / 0.3) * math.pi)
        _paste(frame, num, w / 2, h * 0.42, bump)
        bw = int(w * 0.5 * ease_out_cubic((t - 0.5) / 0.8))
        if bw > 4:
            ImageDraw.Draw(frame).rounded_rectangle(
                [w / 2 - bw / 2, h * 0.68, w / 2 + bw / 2, h * 0.68 + max(4, h * 0.025)],
                radius=3, fill=_rgb(pal[1]))
        _paste(frame, cap, w / 2, h * 0.82, 1.0, _clamp((t - 0.6) * 3))
    return draw


def _kinetic(w, h, labels, pal, font_name, dur):
    text = (_labels(labels, 1, ["THIS CHANGES EVERYTHING"]) or [""])[0].upper()
    words = text.split()[:10] or ["…"]
    fs = int(h * 0.30)
    font = _font(font_name or "Anton", fs)
    while fs > 20:
        font = _font(font_name or "Anton", fs)
        lines = _wrap(font, text, int(w * 0.94), 3)
        if len(lines) * fs * 1.08 <= h * 0.96:
            break
        fs = int(fs * 0.9)
    lines = _wrap(font, " ".join(words), int(w * 0.94), 3)
    longest = max(words, key=len)
    sprites = []
    y = (h - len(lines) * fs * 1.08) / 2 + fs * 0.54
    space = _text_w(font, " ")
    for ln in lines:
        lw = _text_w(font, ln)
        x = (w - lw) / 2
        for wd in ln.split():
            col = _rgb(pal[0]) if wd == longest else (255, 255, 255, 255)
            spr = _text_sprite(wd, font, col, stroke=max(2, fs // 18), stroke_fill=(0, 0, 0, 220))
            ww = _text_w(font, wd)
            sprites.append((spr, x + ww / 2, y))
            x += ww + space
        y += fs * 1.08
    step = min(0.22, (dur - 0.8) / max(1, len(sprites)))

    def draw(frame, t):
        for i, (spr, cx, cy) in enumerate(sprites):
            sc, al = _pop(t, 0.05 + i * step, 0.3)
            _paste(frame, spr, cx, cy, sc, al)
    return draw


def _pointer(size: int, ss: int = 4) -> tuple[Image.Image, tuple[float, float]]:
    """Arrow mouse pointer and its hotspot (the tip) as a fraction of the sprite."""
    s = size * ss
    pts = [(0.08, 0.04), (0.08, 0.80), (0.27, 0.63), (0.41, 0.95), (0.55, 0.89),
           (0.41, 0.58), (0.66, 0.58)]
    big = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    poly = [(x * s, y * s) for x, y in pts]
    shadow = [(x + s * 0.03, y + s * 0.03) for x, y in poly]
    d.polygon(shadow, fill=(0, 0, 0, 90))
    d.polygon(poly, fill=(255, 255, 255, 255))
    d.line(poly + [poly[0]], fill=(0, 0, 0, 255), width=max(2, int(s * 0.045)), joint="curve")
    return big.resize((size, size), Image.LANCZOS), (0.08, 0.04)


def _bell(size: int, color, ss: int = 4) -> Image.Image:
    s = size * ss
    big = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.ellipse([s * 0.44, s * 0.04, s * 0.56, s * 0.16], fill=color)
    d.pieslice([s * 0.2, s * 0.1, s * 0.8, s * 0.7], 180, 360, fill=color)
    d.polygon([(s * 0.2, s * 0.4), (s * 0.8, s * 0.4), (s * 0.92, s * 0.76), (s * 0.08, s * 0.76)],
              fill=color)
    d.ellipse([s * 0.38, s * 0.74, s * 0.62, s * 0.96], fill=color)
    return big.resize((size, size), Image.LANCZOS)


def _subscribe(w, h, labels, pal, font_name, dur):
    ls = (_labels(labels, 2, ["Your Channel", "1.2M"]) + [""])[:2]
    name = ls[0][:32]
    subs = ls[1].strip()
    if subs and "subscriber" not in subs.lower():
        subs += " subscribers"
    red, red_hover = _rgb("#FF0000"), _rgb("#CC0000")
    done_bg, done_fg = _rgb("#3A3A3A"), (241, 241, 241, 255)

    ch = int(h * 0.62)
    cw = int(w * 0.98)
    cx, cy = w / 2, h * 0.40
    card = _rounded((cw, ch), int(ch * 0.22), fill=(15, 15, 15, 236),
                    outline=(255, 255, 255, 30), width=2)
    d = ImageDraw.Draw(card)
    av = int(ch * 0.64)
    pad = int(ch * 0.18)
    card.alpha_composite(_circle(av, _rgb(pal[0] if pal else "#FFD60A")), (pad, (ch - av) // 2))
    ini = _text_sprite((name[:1] or "Y").upper(), _bold(int(av * 0.5)), (15, 15, 15, 255))
    card.alpha_composite(ini, (pad + (av - ini.width) // 2, (ch - ini.height) // 2))

    bh = int(ch * 0.50)
    bf = _bold(int(bh * 0.40))
    sub_txt = _text_sprite("SUBSCRIBE", bf, (255, 255, 255, 255))
    done_txt = _text_sprite("SUBSCRIBED", bf, done_fg)
    bell_sz = int(bh * 0.52)
    bw = max(sub_txt.width, done_txt.width + bell_sz + int(bh * 0.25)) + int(bh * 0.9)
    bx = cx + cw / 2 - pad - bw / 2
    btn_up = _rounded((bw, bh), bh // 2, fill=red)
    btn_hover = _rounded((bw, bh), bh // 2, fill=red_hover)
    btn_done = _rounded((bw, bh), bh // 2, fill=done_bg)
    bell = _bell(bell_sz, done_fg)

    tx = pad + av + int(pad * 0.8)
    room = int(cw - tx - bw - pad * 2)
    nfs = int(ch * 0.26)
    nf = _bold(nfs)
    while nfs > 12 and _text_w(nf, name) > room:
        nfs = int(nfs * 0.9)
        nf = _bold(nfs)
    sf = _font(UI_FONT, max(11, int(ch * 0.17)))
    name_h = nf.getbbox(name or " ")[3]
    top = (ch - name_h - (int(ch * 0.24) if subs else 0)) // 2
    d.text((tx, top), name, font=nf, fill=(255, 255, 255, 255))
    if subs:
        d.text((tx, top + name_h + int(ch * 0.06)), subs[:28], font=sf, fill=(170, 170, 170, 255))

    ptr_sz = int(h * 0.30)
    ptr, (hx, hy) = _pointer(ptr_sz)
    ring = _circle(bh * 2, (255, 255, 255, 0), outline=(255, 255, 255, 220), width=max(2, bh // 14))

    target = (bx + bw * 0.12, cy + bh * 0.12)
    start = (w * 0.98, h * 1.02)
    t_move, t_arrive, t_click, t_done = 0.35, 1.25, 1.6, 1.72

    def draw(frame, t):
        sc, al = _pop(t, 0.0, 0.35)
        _paste(frame, card, cx, cy, sc, al)
        if t < 0.2:
            return
        hovering = t_arrive - 0.1 <= t < t_done
        if t >= t_done:
            s2 = 0.92 + 0.08 * ease_out_back((t - t_done) / 0.3)
            _paste(frame, btn_done, bx, cy, s2)
            gap = int(bh * 0.18)
            total = bell_sz + gap + done_txt.width
            left = bx - total / 2
            ang = 0.0
            if t >= t_done + 0.15:
                k = t - t_done - 0.15
                ang = 24 * math.sin(k * 26) * math.exp(-k * 3.2)
            _paste(frame, bell.rotate(ang, resample=Image.BICUBIC), left + bell_sz / 2, cy, s2)
            _paste(frame, done_txt, left + bell_sz + gap + done_txt.width / 2, cy, s2)
        else:
            press = 0.94 if t_click <= t < t_done else (1.04 if hovering else 1.0)
            _paste(frame, btn_hover if hovering else btn_up, bx, cy, press)
            _paste(frame, sub_txt, bx, cy, press)
        if t_click <= t < t_click + 0.45:
            u = (t - t_click) / 0.45
            _paste(frame, ring, target[0], target[1], 0.2 + 0.8 * ease_out_cubic(u), 1 - u)

        if t < t_move:
            return
        if t < t_arrive:
            u = ease_in_out((t - t_move) / (t_arrive - t_move))
            px = start[0] + (target[0] - start[0]) * u
            py = start[1] + (target[1] - start[1]) * u - math.sin(u * math.pi) * h * 0.12
        elif t < t_done + 0.5:
            px, py = target
        else:
            u = ease_in_out((t - t_done - 0.5) / 0.6)
            px, py = target[0] + w * 0.05 * u, target[1] + h * 0.22 * u
        pscale = 0.85 if t_click <= t < t_click + 0.12 else 1.0
        sw = ptr.width * pscale
        _paste(frame, ptr, px - hx * sw + sw / 2, py - hy * sw + sw / 2, pscale)
    return draw


PAINTERS = {
    "chat_bubbles": _chat,
    "subscribe_button": _subscribe,
    "notification": _notification,
    "search_bar": _search,
    "toggle_meter": _toggle,
    "step_tracker": _steps,
    "venn": _venn,
    "node_map": _nodes,
    "timeline_scrubber": _scrubber,
    "counter": _counter,
    "kinetic_text": _kinetic,
}


# ---- public API ---------------------------------------------------------------

def box_for(kind: str, canvas_w: int, canvas_h: int) -> dict:
    """Default placement as canvas fractions plus the pixel size to render at."""
    meta = CATALOG.get(kind) or CATALOG["kinetic_text"]
    x, y, bw, bh = meta["box"]
    if canvas_w > canvas_h:
        # Landscape canvases: narrower boxes, taller relative height.
        bw = min(0.62, bw * 0.7)
        bh = min(0.7, bh * 1.6)
        x = (1 - bw) / 2
    pw = max(64, int(canvas_w * bw) // 2 * 2)
    ph = max(48, int(canvas_h * bh) // 2 * 2)
    return {"x": round(x, 4), "y": round(y, 4), "w": round(bw, 4), "h": round(bh, 4),
            "px_w": pw, "px_h": ph}


def cache_key(kind: str, labels: list, duration: float, size: tuple, fps: int,
              palette: list, font_name: str | None) -> str:
    raw = json.dumps({
        "v": DEVICE_VERSION, "k": kind, "l": [str(x) for x in (labels or [])],
        "d": round(float(duration), 2), "s": list(size), "f": int(fps),
        "p": list(palette or []), "font": font_name or "",
    }, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def frame_at(kind: str, labels: list, t: float, size: tuple, duration: float | None = None,
             palette: list | None = None, font_name: str | None = None) -> Image.Image:
    """One RGBA frame (used for picker thumbnails and tests)."""
    if kind not in PAINTERS:
        raise ValueError(f"unknown device: {kind}")
    dur = float(duration or CATALOG[kind]["dur"])
    pal = (list(palette or []) + DEFAULT_PALETTE)[:4]
    w, h = int(size[0]), int(size[1])
    draw = PAINTERS[kind](w, h, labels, pal, font_name, dur)
    frame = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw(frame, t)
    return _fade_tail(frame, t, dur)


def _fade_tail(frame: Image.Image, t: float, dur: float, tail: float = 0.3) -> Image.Image:
    if t <= dur - tail:
        return frame
    return _with_alpha(frame, _clamp((dur - t) / tail))


def render_device(kind: str, labels: list, duration: float, size: tuple, fps: int,
                  out_path: Path, palette: list | None = None,
                  font_name: str | None = None, ffmpeg: str = "ffmpeg") -> Path:
    """Render a device to a transparent VP9 WebM at *out_path*."""
    if kind not in PAINTERS:
        raise ValueError(f"unknown device: {kind}")
    w, h = int(size[0]) // 2 * 2, int(size[1]) // 2 * 2
    fps = max(12, min(60, int(fps or 30)))
    dur = max(0.6, min(20.0, float(duration or CATALOG[kind]["dur"])))
    pal = (list(palette or []) + DEFAULT_PALETTE)[:4]
    draw = PAINTERS[kind](w, h, labels, pal, font_name, dur)
    return encode_alpha_webm(draw, w, h, fps, dur, out_path, ffmpeg)


def encode_alpha_webm(draw, w: int, h: int, fps: int, dur: float, out_path: Path,
                      ffmpeg: str = "ffmpeg") -> Path:
    """Pipe draw(frame, t) frames into a transparent VP9 WebM."""
    n = int(round(dur * fps))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".part.webm")
    cmd = [ffmpeg, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
           "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "30",
           "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1",
           "-auto-alt-ref", "0", "-an", str(tmp)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i in range(n):
            t = i / fps
            frame = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw(frame, t)
            proc.stdin.write(_fade_tail(frame, t, dur).tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0 or not tmp.exists():
            raise RuntimeError(f"device encode failed: {err[-400:]}")
    except BrokenPipeError:
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.wait()
        raise RuntimeError(f"device encode failed: {err[-400:]}")
    tmp.replace(out_path)
    return out_path


def render_cached(kind: str, labels: list, duration: float, size: tuple, fps: int,
                  cache_dir: Path, palette: list | None = None,
                  font_name: str | None = None, ffmpeg: str = "ffmpeg") -> tuple[Path, bool]:
    """(path, was_cached). The cache key covers every input to the pixels."""
    key = cache_key(kind, labels, duration, size, fps, palette or DEFAULT_PALETTE, font_name)
    path = Path(cache_dir) / f"device_{kind}_{key}.webm"
    if path.exists() and path.stat().st_size > 200:
        return path, True
    render_device(kind, labels, duration, size, fps, path, palette, font_name, ffmpeg)
    return path, False


def catalog_public() -> list[dict]:
    return [{"kind": k, "label": v["label"], "family": v["family"], "duration": v["dur"],
             "fields": v["fields"], "blurb": v["blurb"], "box": v["box"]}
            for k, v in CATALOG.items()]
