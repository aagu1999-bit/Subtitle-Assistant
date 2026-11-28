"""The vendored remocn X Follow Card.

The first look in this repo whose content slots match what a person actually
says about themselves. Everything it draws was given to it.
"""
import json
import re
from pathlib import Path

BURNER = Path(__file__).resolve().parent.parent
REMOTION = BURNER.parent / "remotion-overlays"
VENDOR = REMOTION / "src" / "vendor" / "remocn"

_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_LINE = re.compile(r"^\s*//.*$", re.M)


def code(path: Path) -> str:
    """Source with comments removed.

    Three times this session a test asserting "X is not in this file"
    matched the comment explaining why X was removed. Strip them and the
    assertion is about the code.
    """
    return _LINE.sub("", _BLOCK.sub("", path.read_text(encoding="utf-8")))


def test_the_licence_travels_with_the_code():
    """MIT requires the notice to go with copies or substantial portions."""
    lic = (VENDOR / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in lic and "Remocn" in lic
    missing = [f.relative_to(VENDOR).as_posix()
               for f in sorted(list(VENDOR.rglob("*.ts")) + list(VENDOR.rglob("*.tsx")))
               if "Vendored from https://github.com/Remocn/remocn (MIT)" not in
               f.read_text(encoding="utf-8")[:900]]
    assert not missing, "vendored without the notice:\n" + "\n".join(missing)


def test_nothing_in_the_vendored_tree_reaches_the_network_or_evals():
    """Third-party code that renders into the user's video. Read before it
    went in; this keeps it read."""
    banned = re.compile(r"\beval\(|new Function|XMLHttpRequest|child_process|"
                        r"document\.cookie|dangerouslySetInnerHTML|WebSocket")
    bad = []
    for f in sorted(list(VENDOR.rglob("*.ts")) + list(VENDOR.rglob("*.tsx"))):
        for n, line in enumerate(code(f).splitlines(), 1):
            if banned.search(line):
                bad.append(f"{f.relative_to(VENDOR)}:{n}: {line.strip()[:70]}")
    assert not bad, "vendored code doing something it should not:\n" + "\n".join(bad)


def test_the_vendored_tree_adds_no_npm_dependency():
    """color.ts was the only file needing `culori`, and nothing the card
    reaches uses it -- so it is not vendored rather than pulling a package in
    for dead code."""
    pkg = json.loads((REMOTION / "package.json").read_text(encoding="utf-8"))
    deps = set(pkg.get("dependencies", {})) | set(pkg.get("devDependencies", {}))
    assert "culori" not in deps
    assert not (VENDOR / "core" / "color.ts").exists()
    imports = set()
    for f in sorted(list(VENDOR.rglob("*.ts")) + list(VENDOR.rglob("*.tsx"))):
        for m in re.findall(r'from "([^".][^"]*)"', f.read_text(encoding="utf-8")):
            imports.add(m.split("/")[0] if not m.startswith("@") else "/".join(m.split("/")[:2]))
    external = {i for i in imports if not i.startswith(".")}
    assert external <= {"react", "remotion", "@remotion/google-fonts"}, \
        f"the vendored tree wants {external - {'react', 'remotion', '@remotion/google-fonts'}}"


def test_no_alias_import_survived_the_vendoring():
    """remocn ships `@/lib/...`; this project has no `@/` alias, so every one
    was rewritten relative. A leftover would fail only at render time."""
    bad = [f.relative_to(VENDOR).as_posix()
           for f in sorted(list(VENDOR.rglob("*.ts")) + list(VENDOR.rglob("*.tsx")))
           if '"@/' in code(f)]
    assert not bad, f"unrewritten alias imports in {bad}"


def test_the_wrapper_forces_vertical_and_the_project_brand():
    """Horizontal lays out on 1280x720, which in a 1080x1920 frame scales to
    ~0.84 and leaves empty bands. Vertical is 720x1280 -- an exact 1.5x."""
    src = code(REMOTION / "src" / "looks" / "XFollowCard.tsx")
    assert 'orientation="vertical"' in src
    assert "accentColor={props.accentColor ?? t.brandColor}" in src, \
        "the Follow button would stay remocn's Twitter blue"
    # the vendored component scales itself; wrapping it in LookFrame would
    # scale it twice
    assert "LookFrame" not in src
    root = code(REMOTION / "src" / "Root.tsx")
    # The <Composition>, not just the name: the import line also contains
    # "XFollowCardLook", so a bare name check passes with the composition
    # deleted -- which is exactly what an earlier version of this did.
    assert re.search(r'<Composition\s+id="XFollowCard"\s+component=\{XFollowCardLook\}',
                     root), "the composition is not registered"
    assert "1080" in root or "NINE_BY_SIXTEEN" in root


def test_the_recipe_asks_for_what_the_component_can_use(app_mod):
    rec = next(r for r in app_mod.REMOTION_RECIPES if r["id"] == "XFollowCard")
    declared = set(rec["fields"])
    component = (VENDOR / "x-follow-card" / "index.tsx").read_text(encoding="utf-8")
    iface = component[component.index("export interface XFollowCardProps"):]
    iface = iface[:iface.index("}")]
    accepted = set(re.findall(r"^\s*(\w+)\??:", iface, re.M))
    unused = declared - accepted
    assert not unused, f"the recipe declares fields the component ignores: {unused}"


def test_asking_for_one_reaches_it_with_the_content(app_mod):
    eng, rec = app_mod._match_named_look("put up an x follow card")
    assert eng == "remotion" and rec["id"] == "XFollowCard"
    tok = set(app_mod.tokens.as_props())
    props = app_mod._remotion_look_props(rec, "an x follow card", None, {
        "name": "Monita Kai", "handle": "@monitakai", "verified": True,
        "bio": "Runs a studio in Lagos.", "joined": "March 2021"})
    got = {k: v for k, v in props.items() if k not in tok}
    assert got["name"] == "Monita Kai" and got["verified"] is True
    assert got["joined"] == "March 2021"


def test_the_model_is_told_this_look_takes_content(app_mod):
    assert "XFollowCard: name, handle, bio" in app_mod._look_fields_prompt()
