"""One look, read by every engine -- and a lint that keeps it that way.

Four engines draw graphics here and each used to carry its own idea of the
brand. The three Python ones agreed by coincidence, not construction: change
highlight_color and devices.py followed while Remotion and Hyperframes stayed
yellow. edit_engine said corner 28 against retelling's radius 18, which is
visible as two cards in one video with different corners. SHELF.md stated the
rule in prose and nothing enforced it.

The tests below are that enforcement.
"""
import json
import re
from pathlib import Path

import pytest

import tokens

BURNER = Path(__file__).resolve().parent.parent
REMOTION = BURNER.parent / "remotion-overlays"
HYPERFRAMES = BURNER.parent / "hyperframes-overlays"

HEX = re.compile(r"#[0-9a-fA-F]{6}\b")

# Files allowed to hold a brand literal anywhere, each with the reason. Not a
# dumping ground: a new entry has to say why that colour is not a brand mark.
ALLOWED_FILES = {
    # The RGB split IS the glitch. The cyan here is half of a red/cyan
    # aberration pair, not the brand accent, and swapping it for the brand
    # would break the effect rather than brand it.
    "compositions/components/caption-glitch-rgb.html",
}

# A brand colour may be WRITTEN only where a default is DECLARED -- a CSS
# custom property, or a Hyperframes variable declaration. Everywhere else it
# has to arrive as a prop or a var, because that is the difference between a
# look the editor can change and one it cannot. This is the rule SHELF.md
# stated in prose and nothing enforced.
_DECLARATION = re.compile(r"(^\s*--[A-Za-z0-9_-]+\s*:)|(\"default\"\s*:)")


def test_every_python_engine_reads_the_same_document():
    """devices.py, retelling.py and edit_engine.py each declared their own."""
    import devices
    import edit_engine
    import retelling

    assert devices.DEFAULT_PALETTE == tokens.palette()
    assert retelling.DEFAULT_TOKENS["colors"]["accent"] == tokens.COLORS["brand"]
    assert retelling.DEFAULT_TOKENS["radius"] == tokens.GEOMETRY["radius"]

    st = edit_engine.style_from_pack({"label": "p", "style": {}})
    assert st["devices"]["palette"] == tokens.palette()
    # The disagreement that was visible on screen.
    assert st["devices"]["corner"] == retelling.DEFAULT_TOKENS["radius"] == 18


def test_a_project_colour_reaches_every_engine_the_same_way():
    """One pink highlight, and all four answers move together."""
    import edit_engine

    tl = {"style": {"highlight_color": "#FF3B6B", "font_name": "Bebas Neue"}}
    t = tokens.resolve(tl)
    assert t["colors"]["brand"] == "#FF3B6B"
    assert t["font"]["display"] == "Bebas Neue"
    # devices.py / motion.py take the list, in contract order
    assert tokens.palette(t)[0] == "#FF3B6B"
    # React takes props
    assert tokens.as_props(t)["brandColor"] == "#FF3B6B"
    # the HTML engines take custom properties
    assert "--brand:#FF3B6B" in tokens.as_css_vars(t)
    # and the pack path agrees
    assert edit_engine.style_from_pack(
        {"label": "p", "style": {"highlight": "#FF3B6B"}})["devices"]["palette"][0] == "#FF3B6B"


def test_the_look_the_prompt_carries_is_the_resolved_one(app_mod):
    """_device_look is what motion.py's brief and devices.py both read."""
    pal, font = app_mod._device_look({"style": {"highlight_color": "#FF3B6B"}})
    assert pal[0] == "#FF3B6B" and len(pal) == 4
    assert font == tokens.FONTS["display"]
    pal, font = app_mod._device_look(None, {"palette": ["#112233"], "font": "Impact"})
    assert pal[0] == "#112233" and font == "Impact"


def test_remotion_and_hyperframes_are_handed_the_brand_rather_than_a_literal(app_mod):
    """The one function both non-Python engines come through.

    It answered "#FFD60A" three times as a literal. That matched the default,
    so every render looked right -- and a project that set a pink highlight got
    pink devices and gold Remotion in the same video.
    """
    tl = {"style": {"highlight_color": "#FF3B6B"}}
    for rec in ({"id": "SplitText", "props": "text"},
                {"id": "NumberCounter", "props": "count"},
                {"id": "Words", "props": "words"}):
        props = app_mod._remotion_look_props(rec, 'say "HELLO"', tl)
        assert props["color"] == "#FF3B6B", rec["id"]
        assert props["brandColor"] == "#FF3B6B", rec["id"]
    # a look with no content props still gets them: the caption looks drew in
    # Tailwind blue because nothing ever handed them a colour
    bare = app_mod._remotion_look_props({"id": "WordHighlightCaptions"}, "captions", tl)
    assert bare["brandColor"] == "#FF3B6B" and bare["displayFont"]
    # and with no project style it is the document's default, not a literal
    assert app_mod._remotion_look_props({"id": "X"}, "", None)["brandColor"] == tokens.COLORS["brand"]


def test_the_typescript_side_holds_the_same_numbers():
    """src/tokens.ts carries Studio fallbacks. They have to match, or a look
    previews in one brand and renders in another."""
    src = (REMOTION / "src" / "tokens.ts").read_text(encoding="utf-8")
    for key, want in (("brandColor", tokens.COLORS["brand"]),
                      ("accentColor", tokens.COLORS["accent"]),
                      ("inkColor", tokens.COLORS["ink"]),
                      ("surfaceColor", tokens.COLORS["surface"]),
                      ("displayFont", tokens.FONTS["display"]),
                      ("uiFont", tokens.FONTS["ui"])):
        assert f'{key}: "{want}"' in src, f"{key} drifted from tokens.py"
    assert f'radius: {tokens.GEOMETRY["radius"]},' in src
    assert f'fadeOutS: {tokens.TIMING["out_s"]},' in src


def _offenders(root: Path, patterns: tuple, allowed: set) -> list:
    """Brand colours written as literals in a USAGE position."""
    out = []
    for pat in patterns:
        for f in sorted(root.rglob(pat)):
            rel = f.relative_to(root).as_posix()
            if rel in allowed or "node_modules" in rel:
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if _DECLARATION.search(line):
                    continue          # declaring the default is the one place allowed
                for hit in HEX.findall(line):
                    if tokens.is_settable_colour(hit):
                        out.append(f"{rel}:{n}: {hit}")
    return out


def test_no_composition_writes_a_settable_brand_colour_as_a_literal():
    """The rule, enforced.

    `brand` and `accent` are what the editor lets a project change, so they
    have to arrive as props. A decorative grey may be a literal -- a token
    there would be a lie about how configurable it is -- which is why this
    checks only the two that are settable.

    src/tokens.ts is exempt: it IS the declaration.
    """
    bad = _offenders(REMOTION / "src", ("*.ts", "*.tsx"), {"tokens.ts"})
    assert not bad, "brand colours hardcoded instead of passed as props:\n" + "\n".join(bad)


def test_no_hyperframes_composition_writes_one_either():
    if not HYPERFRAMES.is_dir():
        pytest.skip("hyperframes-overlays not in this checkout")
    bad = _offenders(HYPERFRAMES, ("compositions/**/*.html", "index.html"), ALLOWED_FILES)
    assert not bad, "brand colours hardcoded instead of declared:\n" + "\n".join(bad)


def test_the_hyperframes_root_variables_track_the_document():
    """index.html declares the brand as a CSS custom property. If tokens.py
    moves and this does not, the HTML engine renders the old brand."""
    f = HYPERFRAMES / "index.html"
    if not f.is_file():
        pytest.skip("hyperframes-overlays not in this checkout")
    decls = {m.group(1): m.group(2).lower() for m in
             re.finditer(r"^\s*(--[A-Za-z0-9_-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;",
                         f.read_text(encoding="utf-8"), re.M)}
    brand = {k: v for k, v in decls.items() if tokens.is_settable_colour(v)}
    assert brand, "no brand custom property found -- did it become a literal?"
    for name, value in brand.items():
        assert value in {tokens.COLORS["brand"].lower(), tokens.COLORS["accent"].lower()}, \
            f"{name} is {value}, which is in neither tokens.py mark"


def test_the_hyperframes_declaration_is_valid_and_matches_the_document():
    """A malformed data-composition-variables is a lint error, and a default
    that drifts means the composition previews in the wrong brand."""
    f = HYPERFRAMES / "compositions" / "components" / "caption-kinetic-slam.html"
    if not f.is_file():
        pytest.skip("composition not in this checkout")
    raw = f.read_text(encoding="utf-8")
    m = re.search(r"data-composition-variables='(\[.*?\])'", raw, re.S)
    assert m, "the variable is not declared, so the CLI cannot override it"
    decls = json.loads(m.group(1))
    brand = next(d for d in decls if d["id"] == "brandColor")
    assert brand["type"] == "color"
    assert brand["default"] == tokens.COLORS["brand"], "default drifted from tokens.py"
    assert "getVariables()" in raw, "declared but never read"


def test_retelling_can_rebuild_its_tokens_after_import():
    """retelling.py has a public `tokens()` function of its own, which shadows
    the module name from its def onwards. Importing the module unaliased
    worked only because DEFAULT_TOKENS is computed above that def -- an
    accident of line order, and the kind that breaks on the next edit."""
    import retelling
    assert retelling._default_tokens() == retelling.DEFAULT_TOKENS
    assert callable(retelling.tokens), "the public API changed shape"
