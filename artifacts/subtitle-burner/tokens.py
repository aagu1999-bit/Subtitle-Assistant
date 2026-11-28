"""The one look. Every renderer reads this, nothing re-declares it.

Four engines draw graphics in this app -- devices.py, motion.py, Remotion and
Hyperframes -- and until now each carried its own idea of the brand:

    devices.DEFAULT_PALETTE        ["#FFD60A", "#00E5FF", "#FFFFFF", "#0F1220"]
    retelling.DEFAULT_TOKENS       radius 18, ui DM Sans, display Anton
    edit_engine.style_from_pack    corner 28          <- disagreed with radius
    _remotion_look_props           "#FFD60A" hardcoded, three times
    the two caption looks          "#2563eb", Tailwind's default blue

The first three agreed by coincidence, not by construction: change
highlight_color in the editor and devices.py followed it while Remotion and
Hyperframes did not. `SHELF.md` stated the rule in prose and nothing enforced
it. So the same plan rendered in two style regimes depending on which phrase
matched in _place_plan.

This module is the document. It holds the defaults, and `resolve()` layers the
project's own style over them, so there is exactly one place where "what
colour is the brand" is answered and exactly one place to change it.

Nothing here imports app, devices, motion, retelling or edit_engine -- they
import this. Keeping it leaf-level is what stops the next duplicate appearing.
"""
from __future__ import annotations

import re

# ---- the document -----------------------------------------------------------

# The brand marks. `brand` is the one the eye goes to, `accent` supports it;
# `ink` is type on video, `surface` is a card behind type. These four, in this
# order, are what devices.py and motion.py take as a palette, so the order is
# part of the contract.
COLORS = {
    "brand": "#FFD60A",
    "accent": "#00E5FF",
    # Off-white, not pure white. The two engines genuinely disagreed here --
    # devices.py said #FFFFFF, retelling.py said #F8FAFC -- and unifying had
    # to pick one. retelling's value wins because its card system is where
    # this colour is most of the type on screen, and because the difference
    # going the other way is 2% of luminance on a device mockup against a
    # visible change to every retelling card.
    "ink": "#F8FAFC",
    "surface": "#0F1220",
}

# Anton for anything big enough to read as a shape, DM Sans for the rest.
FONTS = {"display": "Anton", "ui": "DM Sans"}

# One corner radius. It was 18 in retelling and 28 in edit_engine, which is
# visible: two cards in one video with different corners.
GEOMETRY = {"radius": 18, "stroke": 2, "card_pad": 28, "card_max_w": 0.86}

# How a graphic leaves. devices.py always faded over 0.3s and Remotion hard
# cut, which is the single most noticeable inconsistency between them.
TIMING = {"in_s": 0.25, "out_s": 0.3}

_HEX = re.compile(r"#[0-9a-fA-F]{6}$")


def tokens() -> dict:
    """The defaults, as a fresh dict a caller may edit freely."""
    return {
        "id": "default",
        "colors": dict(COLORS),
        "font": dict(FONTS),
        "radius": GEOMETRY["radius"],
        "stroke": GEOMETRY["stroke"],
        "card": {"pad": GEOMETRY["card_pad"], "max_w": GEOMETRY["card_max_w"]},
        "timing": dict(TIMING),
    }


def palette(t: dict | None = None) -> list[str]:
    """The four colours, in contract order, for devices.py and motion.py."""
    c = (t or tokens())["colors"]
    return [c["brand"], c["accent"], c["ink"], c["surface"]]


# ---- the project's own style, layered over the defaults ---------------------

def _hex_or_none(v) -> str | None:
    s = str(v or "").strip()
    return s if _HEX.match(s) else None


def resolve(timeline: dict | None = None, data: dict | None = None) -> dict:
    """The live tokens for one project.

    Precedence, strongest first: what this request asked for, the timeline's
    style_doc.devices, the caption style the user set in the editor, then the
    defaults above. That order is lifted straight out of the old
    `_device_look`, which was the only function that got this right -- it just
    had four call sites and none of them were Remotion or Hyperframes.
    """
    t = tokens()
    tl = timeline or {}
    data = data or {}
    doc = (tl.get("style_doc") or {}).get("devices") or {}
    st = tl.get("style") or {}

    # An explicit palette wins whole: a caller passing one has already decided.
    pal = data.get("palette") or doc.get("palette")
    pal = [c for c in (_hex_or_none(x) for x in (pal or [])) if c]
    if pal:
        names = ("brand", "accent", "ink", "surface")
        for name, colour in zip(names, pal):
            t["colors"][name] = colour
    else:
        brand = (_hex_or_none(st.get("highlight_color"))
                 or _hex_or_none(st.get("highlight")))
        accent = (_hex_or_none(st.get("accent_color"))
                  or _hex_or_none(st.get("accent")))
        if brand:
            t["colors"]["brand"] = brand
        if accent:
            t["colors"]["accent"] = accent

    font = data.get("font") or doc.get("font") or st.get("font_name")
    if font and str(font).strip():
        t["font"]["display"] = str(font).strip()

    radius = doc.get("corner", doc.get("radius"))
    try:
        if radius is not None:
            t["radius"] = max(0, min(64, int(radius)))
    except (TypeError, ValueError):
        pass
    return t


# ---- handing the same look to engines that are not Python -------------------

def as_props(t: dict | None = None) -> dict:
    """Tokens as Remotion props: camelCase, flat, JSON-safe.

    Remotion compositions are the reason this exists. They are React, so they
    cannot import this module -- the values have to arrive as props, which is
    why `_remotion_look_props` is the chokepoint worth fixing. Hyperframes
    reads the same dict, which is how two engines are fixed in one place.
    """
    t = t or tokens()
    c = t["colors"]
    return {
        "brandColor": c["brand"],
        "accentColor": c["accent"],
        "inkColor": c["ink"],
        "surfaceColor": c["surface"],
        "displayFont": t["font"]["display"],
        "uiFont": t["font"]["ui"],
        "radius": t["radius"],
        "fadeOutS": t["timing"]["out_s"],
    }


def as_css_vars(t: dict | None = None) -> str:
    """The same look as custom properties, for the HTML engines."""
    p = as_props(t)
    pairs = {
        "--brand": p["brandColor"],
        "--accent": p["accentColor"],
        "--ink": p["inkColor"],
        "--surface": p["surfaceColor"],
        "--display-font": p["displayFont"],
        "--ui-font": p["uiFont"],
        "--radius": f"{p['radius']}px",
    }
    return ";".join(f"{k}:{v}" for k, v in pairs.items())


# The two a project can actually change (see `resolve`). Ink and surface are
# structural -- nothing in the editor sets them -- so the rule below is exactly
# as strong as the configurability, and no stronger. Requiring every white in
# every composition to arrive as a prop would be churn that protects nothing.
SETTABLE_COLORS = ("brand", "accent")


def is_settable_colour(value: str) -> bool:
    """True if this hex is a mark a project can change, in any letter case.

    Used by the test that forbids a composition writing one as a literal. A
    decorative grey may be a literal; `brand` and `accent` must arrive as
    props, or changing highlight_color silently stops reaching that engine --
    which is exactly what was happening, invisibly, because the literal and
    the default were the same colour.
    """
    s = str(value or "").strip().lower()
    return s in {COLORS[k].lower() for k in SETTABLE_COLORS}
