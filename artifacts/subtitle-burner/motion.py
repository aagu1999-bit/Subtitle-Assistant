"""Motion graphics from a scene description.

The fixed devices each need a hand-written painter, so every new idea (a
subscribe button, a like counter, a loading bar) meant new code. Here the
model writes a small scene instead -- shapes, text, icons, lines, a mouse
pointer -- each with keyframes, and one renderer draws any of them. The
scene is plain data: it is validated and clamped before anything is drawn,
and nothing from the model is ever executed.

Coordinates are design units: the scene declares its own size (say 1000 x
300) and everything is placed in that space, centred on (x, y). The scene
is then scaled to its slot on the canvas, so the model never has to know the
output resolution.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from PIL import Image, ImageDraw

import devices
from devices import (_bold, _circle, _clamp, _font, _pointer, _rounded, _text_w,
                     _with_alpha, encode_alpha_webm, UI_FONT)

MOTION_VERSION = 1
MAX_LAYERS = 48
MAX_KEYS = 24
LAYER_TYPES = ("rect", "circle", "text", "icon", "line", "ring", "cursor")
ICONS = ("bell", "heart", "star", "check", "play", "pause", "plus", "close", "arrow_right",
         "arrow_up", "arrow_down", "search", "user", "lock", "mail", "chat", "thumbs_up",
         "bolt", "fire", "share", "bookmark", "eye")
EASES = ("linear", "in", "out", "in_out", "back", "bounce", "elastic", "step")
KEYED = ("x", "y", "w", "h", "r", "scale", "opacity", "rotation", "progress", "fill", "color")
PLACEMENTS = {"top": 0.16, "upper": 0.32, "center": 0.5, "lower": 0.66, "bottom": 0.8}


# ---- validation --------------------------------------------------------------

def _num(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return max(lo, min(hi, f))


def _color(v, default="#FFFFFF"):
    s = str(v or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", s):
        return s.upper()
    return default


def _rgba(hex_color: str, alpha_mul: float = 1.0) -> tuple:
    s = hex_color.lstrip("#")
    a = int(s[6:8], 16) if len(s) == 8 else 255
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(a * _clamp(alpha_mul)))


def normalize(spec: dict) -> dict:
    """Clamp a model-written scene into something safe to draw. Raises ValueError."""
    if not isinstance(spec, dict):
        raise ValueError("animation spec must be an object")
    dur = _num(spec.get("duration"), 0.8, 12.0, 3.0)
    size = spec.get("size") or [1000, 400]
    try:
        sw = _num(size[0], 100, 4000, 1000)
        sh = _num(size[1], 60, 4000, 400)
    except (TypeError, IndexError, KeyError):
        sw, sh = 1000.0, 400.0
    place = str(spec.get("placement") or "center").lower()
    out = {
        "duration": dur, "size": [sw, sh],
        "placement": place if place in PLACEMENTS or place == "full" else "center",
        "width": _num(spec.get("width"), 0.55, 1.0, 0.9),
        "layers": [],
    }
    ids = set()
    by_id: dict = {}
    for raw in (spec.get("layers") or [])[:MAX_LAYERS]:
        if not isinstance(raw, dict):
            continue
        typ = str(raw.get("type") or "")
        if typ not in LAYER_TYPES:
            continue
        lay = {"type": typ, "id": str(raw.get("id") or f"l{len(out['layers'])}")[:24]}
        lay["x"] = _num(raw.get("x"), -sw, sw * 2, sw / 2)
        lay["y"] = _num(raw.get("y"), -sh, sh * 2, sh / 2)
        lay["w"] = _num(raw.get("w"), 1, sw * 2, sw * 0.3)
        lay["h"] = _num(raw.get("h"), 1, sh * 2, sh * 0.2)
        lay["r"] = _num(raw.get("r"), 1, max(sw, sh), min(sw, sh) * 0.1)
        lay["radius"] = _num(raw.get("radius"), 0, max(sw, sh), 0)
        lay["scale"] = _num(raw.get("scale"), 0, 8, 1)
        lay["opacity"] = _num(raw.get("opacity"), 0, 1, 1)
        lay["rotation"] = _num(raw.get("rotation"), -3600, 3600, 0)
        lay["progress"] = _num(raw.get("progress"), 0, 1, 1)
        lay["fill"] = _color(raw.get("fill"), "#FFFFFF" if typ != "ring" else "#FFFFFF")
        lay["color"] = _color(raw.get("color"), lay["fill"])
        lay["stroke"] = _color(raw.get("stroke"), "#00000000")
        lay["stroke_width"] = _num(raw.get("stroke_width"), 0, 60, 0)
        lay["width_px"] = _num(raw.get("line_width", raw.get("width_px")), 1, 120, 8)
        lay["anchor"] = raw.get("anchor") if raw.get("anchor") in ("left", "right") else "center"
        if typ == "text":
            lay["text"] = str(raw.get("text") or "")[:80]
            lay["size"] = _num(raw.get("size"), 6, sh * 2, sh * 0.2)
            f = str(raw.get("font") or "bold")
            lay["font"] = f[:40]
            lay["align"] = raw.get("align") if raw.get("align") in ("left", "center", "right") else "center"
            lay["max_width"] = _num(raw.get("max_width"), 10, sw * 2, sw)
            cnt = raw.get("count")
            if isinstance(cnt, dict):
                lay["count"] = {
                    "from": _num(cnt.get("from"), -1e12, 1e12, 0),
                    "to": _num(cnt.get("to"), -1e12, 1e12, 100),
                    "t0": _num(cnt.get("t0"), 0, dur, 0),
                    "t1": _num(cnt.get("t1"), 0, dur, dur * 0.6),
                    "decimals": int(_num(cnt.get("decimals"), 0, 3, 0)),
                    "prefix": str(cnt.get("prefix") or "")[:8],
                    "suffix": str(cnt.get("suffix") or "")[:12],
                }
            ty = raw.get("type_on")
            if isinstance(ty, list) and len(ty) == 2:
                lay["type_on"] = [_num(ty[0], 0, dur, 0), _num(ty[1], 0, dur, 1)]
        if typ == "icon":
            name = str(raw.get("name") or "star")
            lay["name"] = name if name in ICONS else "star"
            lay["size"] = _num(raw.get("size"), 4, max(sw, sh), sh * 0.2)
        if typ == "line":
            pts = []
            for p in (raw.get("points") or [])[:24]:
                try:
                    pts.append([_num(p[0], -sw, sw * 2, 0), _num(p[1], -sh, sh * 2, 0)])
                except (TypeError, IndexError, KeyError):
                    continue
            if len(pts) < 2:
                continue
            lay["points"] = pts
            lay["arrow"] = bool(raw.get("arrow"))
        if typ == "cursor":
            lay["size"] = _num(raw.get("size"), 10, sh * 2, sh * 0.3)
            lay["clicks"] = sorted(_num(c, 0, dur, 0) for c in (raw.get("clicks") or [])[:8])
        parent = raw.get("parent")
        if parent and str(parent) in ids and typ != "line":
            lay["parent"] = str(parent)
            par = by_id[str(parent)]
            if typ == "text" and par["type"] in ("rect", "circle"):
                # Text that belongs to a shape stays inside it. Models judge
                # glyph widths badly, and "SUBSCRIBED" spilling off its pill
                # is the commonest flaw a viewer notices.
                inner = (par["w"] if par["type"] == "rect" else par["r"] * 2) * 0.86
                if lay["align"] == "left":
                    room = inner / 2 - lay["x"]
                elif lay["align"] == "right":
                    room = inner / 2 + lay["x"]
                else:
                    room = inner - 2 * abs(lay["x"])
                lay["max_width"] = max(10.0, min(lay["max_width"], room))
        keys = []
        for k in (raw.get("keys") or [])[:MAX_KEYS]:
            if not isinstance(k, dict):
                continue
            key = {"t": _num(k.get("t"), 0, dur, 0)}
            ease = str(k.get("ease") or "in_out")
            key["ease"] = ease if ease in EASES else "in_out"
            for prop in KEYED:
                if prop not in k:
                    continue
                if prop in ("fill", "color"):
                    key[prop] = _color(k[prop], lay[prop])
                elif prop == "opacity":
                    key[prop] = _num(k[prop], 0, 1, 1)
                elif prop == "progress":
                    key[prop] = _num(k[prop], 0, 1, 1)
                elif prop == "scale":
                    key[prop] = _num(k[prop], 0, 8, 1)
                elif prop == "rotation":
                    key[prop] = _num(k[prop], -3600, 3600, 0)
                else:
                    key[prop] = _num(k[prop], -max(sw, sh) * 2, max(sw, sh) * 3, lay.get(prop, 0))
            keys.append(key)
        keys.sort(key=lambda k: k["t"])
        lay["keys"] = keys
        ids.add(lay["id"])
        by_id[lay["id"]] = lay
        out["layers"].append(lay)
    if not out["layers"]:
        raise ValueError("animation has no drawable layers")
    return out


# ---- placement ---------------------------------------------------------------

def box_for(spec: dict, canvas_w: int, canvas_h: int) -> dict:
    """Where the scene sits on the canvas, as fractions plus the pixel size."""
    sw, sh = spec["size"]
    if spec["placement"] == "full":
        bw = 1.0
        px_w = canvas_w
        px_h = min(canvas_h, int(px_w * sh / sw))
        if px_h == canvas_h:
            px_w = int(px_h * sw / sh)
            bw = px_w / canvas_w
    else:
        bw = spec["width"] if canvas_h >= canvas_w else min(spec["width"], 0.62)
        px_w = int(canvas_w * bw)
        px_h = int(px_w * sh / sw)
        if px_h > canvas_h * 0.9:
            px_h = int(canvas_h * 0.9)
            px_w = int(px_h * sw / sh)
            bw = px_w / canvas_w
    px_w, px_h = max(64, px_w // 2 * 2), max(48, px_h // 2 * 2)
    bh = px_h / canvas_h
    cy = 0.5 if spec["placement"] == "full" else PLACEMENTS.get(spec["placement"], 0.5)
    y = _clamp(cy - bh / 2, 0.0, max(0.0, 1 - bh))
    return {"x": round((1 - bw) / 2, 4), "y": round(y, 4), "w": round(bw, 4), "h": round(bh, 4),
            "px_w": px_w, "px_h": px_h}


# ---- animation ---------------------------------------------------------------

def _ease(name: str, u: float) -> float:
    u = _clamp(u)
    if name == "linear":
        return u
    if name == "in":
        return u * u * u
    if name == "out":
        return 1 - (1 - u) ** 3
    if name == "back":
        return devices.ease_out_back(u)
    if name == "step":
        return 1.0 if u >= 1 else 0.0
    if name == "bounce":
        n, d = 7.5625, 2.75
        if u < 1 / d:
            return n * u * u
        if u < 2 / d:
            u -= 1.5 / d
            return n * u * u + 0.75
        if u < 2.5 / d:
            u -= 2.25 / d
            return n * u * u + 0.9375
        u -= 2.625 / d
        return n * u * u + 0.984375
    if name == "elastic":
        if u in (0.0, 1.0):
            return u
        return 2 ** (-10 * u) * math.sin((u * 10 - 0.75) * (2 * math.pi / 3)) + 1
    return devices.ease_in_out(u)


def _mix_color(a: str, b: str, u: float) -> str:
    ca, cb = _rgba(a), _rgba(b)
    m = [round(ca[i] + (cb[i] - ca[i]) * u) for i in range(4)]
    return "#" + "".join(f"{max(0, min(255, v)):02X}" for v in m)


def state_at(lay: dict, t: float) -> dict:
    """Every keyed property of *lay* at time t (holds before the first key)."""
    st = {p: lay[p] for p in KEYED}
    for prop in KEYED:
        ks = [k for k in lay["keys"] if prop in k]
        if not ks:
            continue
        if t <= ks[0]["t"]:
            st[prop] = ks[0][prop]
            continue
        prev = ks[0]
        val = prev[prop]
        for k in ks[1:]:
            if t < k["t"]:
                span = max(1e-6, k["t"] - prev["t"])
                u = _ease(k["ease"], (t - prev["t"]) / span)
                if prop in ("fill", "color"):
                    val = _mix_color(prev[prop], k[prop], u)
                else:
                    val = prev[prop] + (k[prop] - prev[prop]) * u
                break
            prev = k
            val = k[prop]
        st[prop] = val
    return st


# ---- icons --------------------------------------------------------------------

def _icon(name: str, size: int, color: tuple, ss: int = 4) -> Image.Image:
    s = max(4, int(size)) * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lw = max(2, int(s * 0.11))

    def P(*pts):
        return [(x * s, y * s) for x, y in pts]

    if name == "bell":
        return devices._bell(size, color)
    if name == "heart":
        d.ellipse(P((0.06, 0.12), (0.54, 0.6)), fill=color)
        d.ellipse(P((0.46, 0.12), (0.94, 0.6)), fill=color)
        d.polygon(P((0.09, 0.45), (0.91, 0.45), (0.5, 0.92)), fill=color)
    elif name == "star":
        pts = []
        for i in range(10):
            a = -math.pi / 2 + i * math.pi / 5
            rr = 0.48 if i % 2 == 0 else 0.2
            pts.append((0.5 + rr * math.cos(a), 0.53 + rr * math.sin(a)))
        d.polygon(P(*pts), fill=color)
    elif name == "check":
        d.line(P((0.14, 0.52), (0.4, 0.78), (0.88, 0.24)), fill=color, width=lw * 2, joint="curve")
    elif name == "play":
        d.polygon(P((0.24, 0.12), (0.88, 0.5), (0.24, 0.88)), fill=color)
    elif name == "pause":
        d.rectangle(P((0.2, 0.14), (0.4, 0.86)), fill=color)
        d.rectangle(P((0.6, 0.14), (0.8, 0.86)), fill=color)
    elif name == "plus":
        d.rectangle(P((0.42, 0.1), (0.58, 0.9)), fill=color)
        d.rectangle(P((0.1, 0.42), (0.9, 0.58)), fill=color)
    elif name == "close":
        d.line(P((0.16, 0.16), (0.84, 0.84)), fill=color, width=lw * 2)
        d.line(P((0.84, 0.16), (0.16, 0.84)), fill=color, width=lw * 2)
    elif name in ("arrow_right", "arrow_up", "arrow_down"):
        d.rectangle(P((0.1, 0.4), (0.6, 0.6)), fill=color)
        d.polygon(P((0.52, 0.14), (0.92, 0.5), (0.52, 0.86)), fill=color)
        if name != "arrow_right":
            img = img.rotate(90 if name == "arrow_up" else -90, resample=Image.BICUBIC)
    elif name == "search":
        d.ellipse(P((0.08, 0.08), (0.66, 0.66)), outline=color, width=lw)
        d.line(P((0.58, 0.58), (0.9, 0.9)), fill=color, width=int(lw * 1.4))
    elif name == "user":
        d.ellipse(P((0.3, 0.06), (0.7, 0.46)), fill=color)
        d.pieslice(P((0.1, 0.52), (0.9, 1.3)), 180, 360, fill=color)
    elif name == "lock":
        d.arc(P((0.26, 0.06), (0.74, 0.58)), 180, 360, fill=color, width=lw)
        d.line(P((0.26, 0.32), (0.26, 0.46)), fill=color, width=lw)
        d.line(P((0.74, 0.32), (0.74, 0.46)), fill=color, width=lw)
        d.rounded_rectangle(P((0.14, 0.44), (0.86, 0.94)), radius=s * 0.08, fill=color)
    elif name == "mail":
        d.rounded_rectangle(P((0.06, 0.18), (0.94, 0.82)), radius=s * 0.06, outline=color, width=lw)
        d.line(P((0.08, 0.22), (0.5, 0.56), (0.92, 0.22)), fill=color, width=lw, joint="curve")
    elif name == "chat":
        d.rounded_rectangle(P((0.06, 0.1), (0.94, 0.72)), radius=s * 0.16, fill=color)
        d.polygon(P((0.22, 0.66), (0.42, 0.68), (0.18, 0.92)), fill=color)
    elif name == "thumbs_up":
        d.rounded_rectangle(P((0.06, 0.42), (0.26, 0.92)), radius=s * 0.04, fill=color)
        d.rounded_rectangle(P((0.32, 0.4), (0.9, 0.92)), radius=s * 0.1, fill=color)
        d.polygon(P((0.32, 0.46), (0.5, 0.08), (0.64, 0.12), (0.6, 0.44)), fill=color)
    elif name == "bolt":
        d.polygon(P((0.58, 0.04), (0.16, 0.56), (0.46, 0.56), (0.38, 0.96), (0.84, 0.4),
                    (0.54, 0.4)), fill=color)
    elif name == "fire":
        d.ellipse(P((0.16, 0.38), (0.84, 0.96)), fill=color)
        d.polygon(P((0.18, 0.62), (0.3, 0.3), (0.42, 0.44), (0.5, 0.04), (0.68, 0.36),
                    (0.74, 0.24), (0.84, 0.62)), fill=color)
    elif name == "share":
        for cx, cy in ((0.76, 0.2), (0.24, 0.5), (0.76, 0.8)):
            d.ellipse(P((cx - 0.14, cy - 0.14), (cx + 0.14, cy + 0.14)), fill=color)
        d.line(P((0.76, 0.2), (0.24, 0.5), (0.76, 0.8)), fill=color, width=lw)
    elif name == "bookmark":
        d.polygon(P((0.2, 0.06), (0.8, 0.06), (0.8, 0.94), (0.5, 0.7), (0.2, 0.94)), fill=color)
    elif name == "eye":
        d.chord(P((0.04, 0.2), (0.96, 1.1)), 200, 340, fill=color)
        d.chord(P((0.04, -0.1), (0.96, 0.8)), 20, 160, fill=color)
        d.ellipse(P((0.36, 0.31), (0.64, 0.59)), fill=(0, 0, 0, 0))
    return img.resize((max(4, int(size)), max(4, int(size))), Image.LANCZOS)


# ---- drawing --------------------------------------------------------------------

def _font_for(name: str, px: int):
    n = (name or "").lower()
    if n in ("bold", "heavy", "black"):
        return _bold(px)
    if n in ("regular", "ui", "sans", "body"):
        return _font(UI_FONT, px)
    if n in ("display", "headline"):
        return _font("Anton", px)
    return _font(name, px)


def _fmt_count(c: dict, t: float) -> str:
    u = devices.ease_out_cubic((t - c["t0"]) / max(1e-6, c["t1"] - c["t0"]))
    v = c["from"] + (c["to"] - c["from"]) * u
    return f"{c['prefix']}{v:,.{c['decimals']}f}{c['suffix']}"


class _Sprites:
    def __init__(self, limit: int = 600):
        self.cache: dict = {}
        self.limit = limit

    def get(self, key, make):
        img = self.cache.get(key)
        if img is None:
            if len(self.cache) > self.limit:
                self.cache.clear()
            img = make()
            self.cache[key] = img
        return img


def _place(frame: Image.Image, sprite: Image.Image, cx: float, cy: float,
           scale: float, rotation: float, alpha: float, anchor=(0.5, 0.5)) -> None:
    if alpha <= 0.003 or scale <= 0.01:
        return
    s = sprite
    if abs(scale - 1) > 0.003:
        s = s.resize((max(1, int(s.width * scale)), max(1, int(s.height * scale))), Image.BILINEAR)
    ax, ay = anchor
    if abs(rotation) > 0.05:
        s = s.rotate(-rotation, resample=Image.BICUBIC, expand=True)
        ax, ay = 0.5, 0.5
    s = _with_alpha(s, alpha)
    x, y = int(round(cx - s.width * ax)), int(round(cy - s.height * ay))
    sx, sy = max(0, -x), max(0, -y)
    ex, ey = min(s.width, frame.width - x), min(s.height, frame.height - y)
    if ex > sx and ey > sy:
        frame.alpha_composite(s.crop((sx, sy, ex, ey)), (x + sx, y + sy))


def painter(spec: dict, px_w: int, px_h: int):
    """draw(frame, t) for a normalized scene at the given pixel size."""
    k = px_w / spec["size"][0]
    layers = spec["layers"]
    by_id = {l["id"]: l for l in layers}
    sprites = _Sprites()

    def resolved(lay, t, memo):
        if lay["id"] in memo:
            return memo[lay["id"]]
        st = state_at(lay, t)
        par = by_id.get(lay.get("parent"))
        if par is not None and par is not lay:
            ps = resolved(par, t, memo)
            st["x"] = ps["x"] + st["x"] * ps["scale"]
            st["y"] = ps["y"] + st["y"] * ps["scale"]
            st["scale"] *= ps["scale"]
            st["opacity"] *= ps["opacity"]
        memo[lay["id"]] = st
        return st

    def draw(frame, t):
        memo: dict = {}
        for lay in layers:
            st = resolved(lay, t, memo)
            op = st["opacity"]
            if op <= 0.003:
                continue
            typ = lay["type"]
            cx, cy = st["x"] * k, st["y"] * k
            if typ == "rect":
                w, h = max(2, int(st["w"] * k)), max(2, int(st["h"] * k))
                rad = int(min(lay["radius"] * k, min(w, h) / 2))
                sw_ = int(lay["stroke_width"] * k)
                spr = sprites.get(("rect", w, h, rad, st["fill"], lay["stroke"], sw_),
                                  lambda: _rounded((w, h), rad, fill=_rgba(st["fill"]),
                                                   outline=_rgba(lay["stroke"]) if sw_ else None,
                                                   width=sw_))
                anchor = {"left": (0, 0.5), "right": (1, 0.5)}.get(lay["anchor"], (0.5, 0.5))
                _place(frame, spr, cx, cy, st["scale"], st["rotation"], op, anchor)
            elif typ == "circle":
                dm = max(2, int(st["r"] * 2 * k))
                sw_ = int(lay["stroke_width"] * k)
                spr = sprites.get(("circ", dm, st["fill"], lay["stroke"], sw_),
                                  lambda: _circle(dm, _rgba(st["fill"]),
                                                  outline=_rgba(lay["stroke"]) if sw_ else None,
                                                  width=sw_))
                _place(frame, spr, cx, cy, st["scale"], st["rotation"], op)
            elif typ == "ring":
                dm = max(4, int(st["r"] * 2 * k))
                lw = max(1, int(lay["width_px"] * k))
                prog = round(_clamp(st["progress"]), 3)

                def mk():
                    ss = 2
                    big = Image.new("RGBA", (dm * ss, dm * ss), (0, 0, 0, 0))
                    d = ImageDraw.Draw(big)
                    d.ellipse([0, 0, dm * ss - 1, dm * ss - 1], outline=_rgba(lay["stroke"]),
                              width=lw * ss)
                    if prog > 0:
                        d.arc([0, 0, dm * ss - 1, dm * ss - 1], -90, -90 + 360 * prog,
                              fill=_rgba(st["color"]), width=lw * ss)
                    return big.resize((dm, dm), Image.LANCZOS)
                spr = sprites.get(("ring", dm, lw, prog, st["color"], lay["stroke"]), mk)
                _place(frame, spr, cx, cy, st["scale"], st["rotation"], op)
            elif typ == "text":
                txt = lay["text"]
                if "count" in lay:
                    txt = _fmt_count(lay["count"], t)
                if "type_on" in lay:
                    t0, t1 = lay["type_on"]
                    n = int(len(txt) * _clamp((t - t0) / max(1e-6, t1 - t0)))
                    txt = txt[:n]
                if not txt:
                    continue
                px = max(6, int(lay["size"] * k))
                maxw = lay["max_width"] * k

                def mk(txt=txt, px=px):
                    f = _font_for(lay["font"], px)
                    while px > 8 and _text_w(f, txt) > maxw:
                        px = int(px * 0.92)
                        f = _font_for(lay["font"], px)
                    sw_ = int(lay["stroke_width"] * k)
                    return devices._text_sprite(txt, f, _rgba(st["color"]), stroke=sw_,
                                                stroke_fill=_rgba(lay["stroke"]))
                spr = sprites.get(("text", txt, px, st["color"], lay["font"], lay["stroke"]), mk)
                anchor = {"left": (0, 0.5), "right": (1, 0.5)}.get(lay["align"], (0.5, 0.5))
                _place(frame, spr, cx, cy, st["scale"], st["rotation"], op, anchor)
            elif typ == "icon":
                sz = max(4, int(lay["size"] * k))
                spr = sprites.get(("icon", lay["name"], sz, st["color"]),
                                  lambda: _icon(lay["name"], sz, _rgba(st["color"])))
                _place(frame, spr, cx, cy, st["scale"], st["rotation"], op)
            elif typ == "line":
                pts = [(x * k, y * k) for x, y in lay["points"]]
                prog = _clamp(st["progress"])
                if prog <= 0:
                    continue
                segs = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
                target = sum(segs) * prog
                drawn, acc = [pts[0]], 0.0
                for i, sl in enumerate(segs):
                    if acc + sl >= target:
                        u = (target - acc) / sl if sl else 1
                        drawn.append((pts[i][0] + (pts[i + 1][0] - pts[i][0]) * u,
                                      pts[i][1] + (pts[i + 1][1] - pts[i][1]) * u))
                        break
                    acc += sl
                    drawn.append(pts[i + 1])
                layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
                d = ImageDraw.Draw(layer)
                lw = max(1, int(lay["width_px"] * k))
                d.line(drawn, fill=_rgba(st["color"]), width=lw, joint="curve")
                if lay["arrow"] and len(drawn) >= 2:
                    (x0, y0), (x1, y1) = drawn[-2], drawn[-1]
                    a = math.atan2(y1 - y0, x1 - x0)
                    L = lw * 3
                    d.polygon([(x1 + math.cos(a) * L * 0.6, y1 + math.sin(a) * L * 0.6),
                               (x1 + math.cos(a + 2.5) * L, y1 + math.sin(a + 2.5) * L),
                               (x1 + math.cos(a - 2.5) * L, y1 + math.sin(a - 2.5) * L)],
                              fill=_rgba(st["color"]))
                frame.alpha_composite(_with_alpha(layer, op))
            elif typ == "cursor":
                sz = max(10, int(lay["size"] * k))
                ptr, (hx, hy) = sprites.get(("cursor", sz), lambda: _pointer(sz))
                press = 1.0
                for c in lay["clicks"]:
                    if c <= t < c + 0.45:
                        u = (t - c) / 0.45
                        rd = max(4, int(sz * 0.9))
                        ring = sprites.get(("ripple", rd), lambda: _circle(
                            rd * 2, (255, 255, 255, 0), outline=(255, 255, 255, 230),
                            width=max(2, rd // 8)))
                        _place(frame, ring, cx, cy, 0.2 + 0.8 * devices.ease_out_cubic(u),
                               0, op * (1 - u))
                    if c <= t < c + 0.12:
                        press = 0.85
                _place(frame, ptr, cx, cy, st["scale"] * press, 0, op, anchor=(hx, hy))
    return draw


def fit_to_content(spec: dict, margin: float = 0.05) -> dict:
    """Crop the scene to what is actually drawn, so its width is the graphic's width.

    Models lay a button out in a 1000-wide scene and leave most of it empty;
    the slot on the canvas is then mostly air and the button reads tiny. The
    mouse pointer is left out of the measurement because it enters from off
    the edge on purpose.
    """
    sw, sh = spec["size"]
    probe = {**spec, "layers": [l for l in spec["layers"] if l["type"] != "cursor"]}
    if not probe["layers"]:
        return spec
    pw = 400
    ph = max(8, int(pw * sh / sw))
    # Measure on a canvas with room around the scene, so content placed
    # outside 0..W/0..H is seen rather than clipped.
    pad_x, pad_y = pw, ph
    union = None
    draw = painter({**probe, "size": [sw * 3, sh * 3], "layers": [
        {**l, "x": l["x"] + (sw if "parent" not in l else 0),
         "y": l["y"] + (sh if "parent" not in l else 0),
         "keys": [{**k, **({"x": k["x"] + sw} if "x" in k and "parent" not in l else {}),
                   **({"y": k["y"] + sh} if "y" in k and "parent" not in l else {})}
                  for k in l["keys"]],
         **({"points": [[x + sw, y + sh] for x, y in l["points"]]} if l["type"] == "line" else {})}
        for l in probe["layers"]]}, pw + 2 * pad_x, ph + 2 * pad_y)
    for i in range(12):
        t = spec["duration"] * (0.05 + 0.85 * i / 11)
        frame = Image.new("RGBA", (pw + 2 * pad_x, ph + 2 * pad_y), (0, 0, 0, 0))
        draw(frame, t)
        bb = frame.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
        if bb:
            union = bb if union is None else (min(union[0], bb[0]), min(union[1], bb[1]),
                                              max(union[2], bb[2]), max(union[3], bb[3]))
    if not union:
        return spec
    k = pw / sw
    x0, y0 = (union[0] - pad_x) / k, (union[1] - pad_y) / k
    x1, y1 = (union[2] - pad_x) / k, (union[3] - pad_y) / k
    m = margin * max(x1 - x0, y1 - y0)
    x0, y0, x1, y1 = x0 - m, y0 - m, x1 + m, y1 + m
    if (x1 - x0) > sw * 0.95 and (y1 - y0) > sh * 0.95 and x0 >= -sw * 0.02 and y0 >= -sh * 0.02:
        return spec

    def shift(l):
        if "parent" in l:
            return l
        out = {**l, "x": l["x"] - x0, "y": l["y"] - y0,
               "keys": [{**kk, **({"x": kk["x"] - x0} if "x" in kk else {}),
                         **({"y": kk["y"] - y0} if "y" in kk else {})} for kk in l["keys"]]}
        if l["type"] == "line":
            out["points"] = [[x - x0, y - y0] for x, y in l["points"]]
        return out

    return {**spec, "size": [round(x1 - x0, 2), round(y1 - y0, 2)],
            "layers": [shift(l) for l in spec["layers"]]}


def cache_key(spec: dict, size: tuple, fps: int) -> str:
    raw = json.dumps({"v": MOTION_VERSION, "s": spec, "px": list(size), "f": int(fps)},
                     sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def frame_at(spec: dict, t: float, size: tuple) -> Image.Image:
    w, h = int(size[0]), int(size[1])
    frame = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    painter(spec, w, h)(frame, t)
    return devices._fade_tail(frame, t, spec["duration"])


def render_cached(spec: dict, size: tuple, fps: int, cache_dir: Path,
                  ffmpeg: str = "ffmpeg") -> tuple[Path, bool]:
    w, h = int(size[0]) // 2 * 2, int(size[1]) // 2 * 2
    fps = max(12, min(60, int(fps or 30)))
    path = Path(cache_dir) / f"motion_{cache_key(spec, (w, h), fps)}.webm"
    if path.exists() and path.stat().st_size > 200:
        return path, True
    draw = painter(spec, w, h)
    encode_alpha_webm(draw, w, h, fps, spec["duration"], path, ffmpeg)
    return path, False


# ---- the model's brief ------------------------------------------------------------

EXAMPLE = {
    "duration": 3.6, "size": [1000, 320], "placement": "lower", "width": 0.92,
    "layers": [
        {"id": "card", "type": "rect", "x": 500, "y": 130, "w": 980, "h": 200, "radius": 44,
         "fill": "#0F0F0FEE", "stroke": "#FFFFFF22", "stroke_width": 2,
         "keys": [{"t": 0, "scale": 0.6, "opacity": 0}, {"t": 0.35, "scale": 1, "opacity": 1, "ease": "back"}]},
        {"id": "avatar", "type": "circle", "parent": "card", "x": -400, "y": 0, "r": 62, "fill": "#FFD60A"},
        {"id": "initial", "type": "text", "parent": "avatar", "text": "A", "x": 0, "y": 0, "size": 64,
         "font": "bold", "color": "#0F0F0F"},
        {"id": "name", "type": "text", "parent": "card", "text": "Alex Talks Money", "x": -310, "y": -22,
         "size": 46, "font": "bold", "color": "#FFFFFF", "align": "left", "max_width": 380},
        {"id": "subs", "type": "text", "parent": "card", "text": "1.2M subscribers", "x": -310, "y": 30,
         "size": 32, "font": "regular", "color": "#AAAAAA", "align": "left"},
        {"id": "btn", "type": "rect", "parent": "card", "x": 290, "y": 0, "w": 300, "h": 100, "radius": 50,
         "fill": "#FF0000",
         "keys": [{"t": 1.15, "fill": "#FF0000", "scale": 1}, {"t": 1.3, "fill": "#CC0000", "scale": 1.04},
                  {"t": 1.6, "scale": 1.04}, {"t": 1.66, "scale": 0.94, "ease": "out"},
                  {"t": 1.72, "fill": "#CC0000"}, {"t": 1.8, "fill": "#3A3A3A", "scale": 1, "ease": "back"}]},
        {"id": "btn_txt", "type": "text", "parent": "btn", "text": "SUBSCRIBE", "x": 0, "y": 0, "size": 40,
         "font": "bold", "color": "#FFFFFF",
         "keys": [{"t": 1.7, "opacity": 1}, {"t": 1.76, "opacity": 0}]},
        {"id": "done_txt", "type": "text", "parent": "btn", "text": "SUBSCRIBED", "x": 32, "y": 0, "size": 31,
         "font": "bold", "color": "#F1F1F1",
         "keys": [{"t": 1.74, "opacity": 0}, {"t": 1.84, "opacity": 1}]},
        {"id": "bell", "type": "icon", "parent": "btn", "name": "bell", "x": -116, "y": 0, "size": 40,
         "color": "#F1F1F1",
         "keys": [{"t": 1.74, "opacity": 0, "rotation": 0}, {"t": 1.84, "opacity": 1},
                  {"t": 1.95, "rotation": -22}, {"t": 2.07, "rotation": 18}, {"t": 2.19, "rotation": -12},
                  {"t": 2.31, "rotation": 6}, {"t": 2.45, "rotation": 0}]},
        {"id": "mouse", "type": "cursor", "x": 1040, "y": 340, "size": 90, "clicks": [1.6],
         "keys": [{"t": 0.35, "x": 1040, "y": 340}, {"t": 1.25, "x": 830, "y": 150, "ease": "in_out"},
                  {"t": 2.3, "x": 830, "y": 150}, {"t": 2.9, "x": 900, "y": 300}]},
    ],
}

EXAMPLE_BAR = {
    "duration": 3.0, "size": [1000, 220], "placement": "upper", "width": 0.86,
    "layers": [
        {"id": "track", "type": "rect", "x": 500, "y": 140, "w": 900, "h": 44, "radius": 22,
         "fill": "#FFFFFF33", "keys": [{"t": 0, "opacity": 0}, {"t": 0.3, "opacity": 1}]},
        {"id": "fill", "type": "rect", "parent": "track", "anchor": "left", "x": -450, "y": 0,
         "w": 44, "h": 44, "radius": 22, "fill": "#00E5FF",
         "keys": [{"t": 0.3, "w": 44}, {"t": 2.2, "w": 900, "ease": "in_out"}]},
        {"id": "pct", "type": "text", "x": 950, "y": 60, "size": 48, "font": "bold",
         "color": "#FFFFFF", "align": "right",
         "count": {"from": 0, "to": 100, "t0": 0.3, "t1": 2.2, "suffix": "%"}},
        {"id": "label", "type": "text", "x": 50, "y": 60, "size": 44, "font": "bold",
         "color": "#FFFFFF", "align": "left", "text": "Uploading..."},
    ],
}


def brief(description: str, palette: list, font: str | None, duration: float | None,
          canvas: str) -> str:
    return f"""You are a motion designer. Design a short transparent animation that is
composited over a {canvas} talking-head video. Return ONLY a JSON scene.

THE REQUEST: {description}

SCENE FORMAT
- duration (seconds, 0.8-12){f" -- use {duration:.1f}" if duration else ""}; size [W, H] in design
  units (W 1000 is typical; pick H for the shape of the graphic); placement: top | upper |
  center | lower | bottom | full; width = fraction of the frame width (0.55-1; 0.85-0.95
  for anything with text -- it has to read on a phone).
- Fill the scene: the main element should span most of W. Size H to the content, so a
  single button row is about W x 0.3W, not a tall empty box.
- layers: drawn in order (later = on top). Every layer has id, type, x, y, optional
  parent, opacity, scale, rotation (degrees), and keys.
- POSITIONS: x, y is the layer's CENTRE. With no parent it is in scene units (0..W,
  0..H, y down). With a parent (an EARLIER layer id) x, y are RELATIVE TO THE PARENT'S
  CENTRE -- (0, 0) is dead centre of the parent -- and the layer moves, scales and fades
  with it. Put a button's label on the button with parent = the button and x = y = 0.
- keys: [{{"t": seconds, "<prop>": value, "ease": linear|in|out|in_out|back|bounce|elastic|step}}].
  Keyable props: x, y, w, h, r, scale, opacity, rotation, progress, fill, color. Before
  the first key a prop holds its first key's value; ease applies on the way INTO a key.
- types:
  rect: w, h, radius, fill, stroke, stroke_width, anchor (left|right makes x the left or
        right edge, so keying w grows it one way: progress bars use anchor left)
  circle: r, fill, stroke, stroke_width
  text: text (<=80 chars), size (cap height-ish, design units), font (bold | regular |
        display | a font name), color, align (left|center|right -- x is then the left/right
        edge), max_width, stroke, stroke_width, optional count {{from, to, t0, t1, decimals,
        prefix, suffix}} for a number that counts, optional type_on [t0, t1] to type it out
  icon: name ({", ".join(ICONS)}), size, color
  line: points [[x,y],...], color, line_width, arrow (bool); key progress 0->1 to draw it on
  ring: r, line_width, color (the arc), stroke (the track), key progress 0->1
  cursor: a mouse pointer whose TIP is at (x, y); size; clicks [seconds] (press + ripple)
- Colours "#RRGGBB" or "#RRGGBBAA".

DESIGN RULES
- Recreate the real thing faithfully when the request names one (a YouTube button is red
  #FF0000 with white text, Instagram's like is a red heart, an iPhone toggle is green).
  Otherwise use this palette: {", ".join(palette)}; text font: {font or "bold"}.
- Make it move: an entrance in the first 0.4s, the main action in the middle, hold the end
  state; a fade-out is added for you at the end.
- Keep everything inside 0..W and 0..H except a cursor entering from off-edge.
- Legible on a phone: main text at least H*0.12.
- Everything a thing is made of goes together: card -> children on the card; button ->
  its label and icon on the button. Check text fits inside its shape (a bold letter is
  about 0.62 x size wide) and that nothing overlaps unless it should.
- Rich, not sparse: real UI has a container, an icon or avatar, a title, a detail line.
- 3 to 30 layers. No placeholder text -- use real words from the request.

EXAMPLE (a YouTube subscribe button a mouse clicks):
{json.dumps(EXAMPLE)}

EXAMPLE (a progress bar that fills while a percentage counts):
{json.dumps(EXAMPLE_BAR)}
"""
