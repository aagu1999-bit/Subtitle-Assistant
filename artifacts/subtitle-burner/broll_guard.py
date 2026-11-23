"""B-roll guardrails: a source ladder, A-roll colour matching, safe generation.

The ladder tries the user's own library first (their footage always looks
like their video), then stock search on *concept* queries rather than the
literal noun, and only then generates an abstract, people-free, text-free
shot tinted with the A-roll's own palette. Whatever is chosen gets a colour
match against the A-roll -- exposure, saturation, tint and a touch of grain --
so cutaways stop looking pasted in.
"""
from __future__ import annotations

import hashlib
import io
import re
import subprocess
from pathlib import Path

import edit_engine

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "my", "your",
         "our", "img", "image", "photo", "video", "clip", "final", "copy", "jpg", "png", "mp4",
         "mov", "webm", "stock", "broll", "b", "roll"}


def _stem(tok: str) -> str:
    tok = tok.lower()
    for suf in ("ing", "ers", "er", "es", "s"):
        if len(tok) > len(suf) + 2 and tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


def _terms(text: str) -> set[str]:
    toks = re.findall(r"[a-zA-Z]{3,}", text or "")
    return {_stem(t) for t in toks if t.lower() not in _STOP}


# ---- 1. Library ---------------------------------------------------------------

def library_match(queries: list[str], assets: list[dict], exclude_sources=("device", "generated")):
    """Best user asset for the queries by shared terms, or None.

    *assets* rows carry filename/keyword/source/kind. Graphics devices and
    our own generated fills never count as "your footage".
    """
    want = set()
    for q in queries or []:
        want |= _terms(q)
    if not want:
        return None
    best, best_score = None, 0.0
    for a in assets or []:
        if a.get("kind") not in ("video", "image"):
            continue
        if a.get("source") in exclude_sources:
            continue
        have = _terms(" ".join(str(a.get(k) or "") for k in ("filename", "keyword", "tags")))
        shared = want & have
        if not shared:
            continue
        score = len(shared) / (len(want) ** 0.5) + (0.25 if a.get("kind") == "video" else 0)
        if score > best_score:
            best, best_score = a, score
    return best


# ---- 2. A-roll look -------------------------------------------------------------

def frame_image(path: Path, t: float = 0.0, ffmpeg: str = "ffmpeg", width: int = 160):
    """A small RGB PIL frame from a video or image (None on failure)."""
    from PIL import Image
    p = Path(path)
    if p.suffix.lower().lstrip(".") in ("jpg", "jpeg", "png", "webp", "bmp", "gif"):
        try:
            im = Image.open(p).convert("RGB")
            im.thumbnail((width, width * 4))
            return im
        except OSError:
            return None
    try:
        out = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(p), "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=30).stdout
        return Image.open(io.BytesIO(out)).convert("RGB") if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def image_stats(img) -> dict:
    """Mean luma, chroma offsets, saturation and a grain estimate (0-255 scales)."""
    from PIL import ImageFilter, ImageStat
    ycc = img.convert("YCbCr")
    y, cb, cr = ImageStat.Stat(ycc).mean
    sat = ImageStat.Stat(img.convert("HSV")).mean[1]
    gray = img.convert("L")
    diff = ImageStat.Stat(_absdiff(gray, gray.filter(ImageFilter.GaussianBlur(1.2)))).mean[0]
    return {"y": round(y, 2), "cb": round(cb, 2), "cr": round(cr, 2), "sat": round(sat, 2),
            "grain": round(diff, 2)}


def _absdiff(a, b):
    from PIL import ImageChops
    return ImageChops.difference(a, b)


def palette(img, n: int = 3) -> list[str]:
    """Dominant colours, most common first, nudged to be usable as tints."""
    q = img.convert("RGB").resize((64, 64)).quantize(colors=max(2, n + 2), method=0)
    pal = q.getpalette() or []
    counts = sorted(q.getcolors() or [], reverse=True)
    out = []
    for _, idx in counts:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        if lum < 12 or lum > 246:
            continue  # pure black bars / blown highlights are not the look
        out.append(f"#{r:02X}{g:02X}{b:02X}")
        if len(out) >= n:
            break
    return out or ["#334155", "#64748B", "#0F172A"]


def color_match_params(a_roll: dict, b_roll: dict | None = None) -> dict:
    """Filter parameters that pull B-roll toward the A-roll's look.

    Without B-roll stats the B-roll is assumed neutral (mid exposure, moderate
    saturation, no tint, clean), which is what stock photos mostly are.
    """
    b = b_roll or {"y": 118.0, "cb": 128.0, "cr": 128.0, "sat": 95.0, "grain": 1.0}

    def clamp(v, lo, hi):
        return max(lo, min(hi, v))

    brightness = clamp((a_roll["y"] - b["y"]) / 255.0 * 0.7, -0.15, 0.15)
    saturation = clamp((a_roll["sat"] + 8) / max(8.0, b["sat"] + 8), 0.6, 1.35)
    # Cr is red-difference, Cb blue-difference. Shift midtones by the gap.
    rm = clamp((a_roll["cr"] - b["cr"]) / 128.0 * 0.8, -0.2, 0.2)
    bm = clamp((a_roll["cb"] - b["cb"]) / 128.0 * 0.8, -0.2, 0.2)
    gm = clamp(-(rm + bm) / 2, -0.2, 0.2)
    grain = int(clamp((a_roll["grain"] - b["grain"]) * 2.2, 0, 18))
    return {"brightness": round(brightness, 3), "saturation": round(saturation, 3),
            "rm": round(rm, 3), "gm": round(gm, 3), "bm": round(bm, 3), "grain": grain}


def color_match_filter(params) -> str:
    """FFmpeg chain for color_match params ('' when there is nothing to do)."""
    if not isinstance(params, dict):
        return ""
    try:
        br = float(params.get("brightness", 0) or 0)
        sat = float(params.get("saturation", 1) or 1)
        rm = float(params.get("rm", 0) or 0)
        gm = float(params.get("gm", 0) or 0)
        bm = float(params.get("bm", 0) or 0)
        grain = int(params.get("grain", 0) or 0)
    except (TypeError, ValueError):
        return ""
    parts = []
    if abs(br) > 0.004 or abs(sat - 1) > 0.01:
        parts.append(f"eq=brightness={br:.3f}:saturation={sat:.3f}")
    if max(abs(rm), abs(gm), abs(bm)) > 0.004:
        parts.append(f"colorbalance=rm={rm:.3f}:gm={gm:.3f}:bm={bm:.3f}")
    if grain > 0:
        parts.append(f"noise=alls={min(30, grain)}:allf=t")
    return ",".join(parts)


# ---- 3. Generative abstract fallback --------------------------------------------

# Abstract looks keyed by the feeling a concept carries. Every one is pure
# colour and light: nothing here can draw a person, a hand or a word.
_LOOKS = {
    "orbit": {"type": "spiral", "speed": 0.006, "n": 3, "blur": 40, "pulse": 0.0},
    "drift": {"type": "linear", "speed": 0.010, "n": 3, "blur": 30, "pulse": 0.0},
    "glow": {"type": "radial", "speed": 0.012, "n": 2, "blur": 26, "pulse": 0.05},
    "tide": {"type": "circular", "speed": 0.008, "n": 3, "blur": 34, "pulse": 0.03},
}
_MOOD_LOOK = [
    (re.compile(r"overwhelm|pressure|chaos|stress|anxi|tension|clock|deadline|grind", re.I), "orbit"),
    (re.compile(r"growth|momentum|energy|launch|win|rise|sunrise|bright", re.I), "glow"),
    (re.compile(r"calm|clarity|focus|quiet|still|rest|ocean|water|horizon", re.I), "tide"),
]


def pick_look(text: str) -> str:
    for rx, look in _MOOD_LOOK:
        if rx.search(text or ""):
            return look
    keys = sorted(_LOOKS)
    return keys[int(hashlib.md5((text or "").encode()).hexdigest(), 16) % len(keys)]


def sanitize_prompt(text: str, pal: list[str]) -> dict:
    """Generative policy: abstract, people-free, text-free, palette-anchored."""
    prompt = edit_engine.abstract_prompt(text, pal)
    ok = edit_engine.generative_prompt_ok(prompt)
    return {"prompt": prompt, "ok": ok,
            "policy": ["abstract or atmospheric only", "no people, faces or hands",
                       "no legible text or logos", "palette anchored to the A-roll"]}


def abstract_lavfi(look: str, pal: list[str], dur: float, W: int, H: int, fps: int = 30,
                   seed: int = 7) -> str:
    """A lavfi source graph for an abstract shot (rendered at half size)."""
    spec = _LOOKS.get(look) or _LOOKS["drift"]
    cols = [(c if re.fullmatch(r"#[0-9a-fA-F]{6}", c or "") else "#334155").replace("#", "0x")
            for c in (list(pal) + ["#0F172A", "#334155", "#64748B"])[:3]]
    w2, h2 = max(64, W // 4 * 2), max(64, H // 4 * 2)
    g = (f"gradients=s={w2}x{h2}:r={fps}:d={dur:.3f}:type={spec['type']}:n={spec['n']}"
         f":c0={cols[0]}:c1={cols[1]}:c2={cols[2]}:speed={spec['speed']}:seed={seed}")
    chain = [g, f"gblur=sigma={spec['blur'] / 2:.1f}"]
    if spec["pulse"]:
        chain.append(f"eq=brightness='{spec['pulse']}*sin(2*PI*t/3)':eval=frame")
    chain += [f"scale={W}:{H}:flags=bicubic", "noise=alls=7:allf=t", "vignette=PI/5",
              "format=yuv420p"]
    return ",".join(chain)


def generate_abstract(text: str, pal: list[str], dur: float, W: int, H: int,
                      cache_dir: Path, fps: int = 30, ffmpeg: str = "ffmpeg") -> dict:
    """Render (or reuse) an abstract fallback shot. Returns path + provenance."""
    look = pick_look(text)
    policy = sanitize_prompt(text, pal)
    seed = int(hashlib.md5((text or "").encode()).hexdigest()[:6], 16)
    graph = abstract_lavfi(look, pal, dur, W, H, fps, seed)
    key = hashlib.sha1(f"v1|{graph}".encode()).hexdigest()[:20]
    out = Path(cache_dir) / f"genabs_{key}.mp4"
    cached = out.exists() and out.stat().st_size > 1000
    if not cached:
        tmp = out.with_suffix(".part.mp4")
        proc = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", graph, "-t", f"{dur:.3f}",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-an", str(tmp)],
            capture_output=True, text=True)
        if proc.returncode != 0 or not tmp.exists():
            raise RuntimeError(f"abstract fill failed: {proc.stderr[-300:]}")
        tmp.replace(out)
    return {"path": out, "cached": cached, "look": look, **policy}


# ---- The ladder --------------------------------------------------------------------

def smart_broll(text: str, *, assets: list[dict], stock=None, generate=None,
                allow_stock: bool = True, allow_generate: bool = True) -> dict:
    """Walk library -> stock -> generated for one phrase.

    *stock(query)* returns an asset dict or None; *generate(text)* returns an
    asset dict. Every rung is recorded so the UI can say why it landed where
    it did.
    """
    cq = edit_engine.concept_queries(text)
    queries = cq["queries"] or [text]
    ladder = []
    hit = library_match(queries + [" ".join(cq.get("literal") or [])], assets)
    ladder.append({"step": "library", "ok": bool(hit),
                   "detail": (hit or {}).get("filename") or "no matching footage in your library"})
    if hit:
        return {"source": "library", "asset": hit, "query": queries[0], "concepts": cq["concepts"],
                "ladder": ladder}
    if allow_stock and stock:
        for q in queries:
            got = stock(q)
            ladder.append({"step": "stock", "ok": bool(got), "detail": q})
            if got:
                return {"source": "stock", "asset": got, "query": q, "concepts": cq["concepts"],
                        "ladder": ladder}
    elif allow_stock:
        ladder.append({"step": "stock", "ok": False, "detail": "no stock provider configured"})
    if allow_generate and generate:
        got = generate(text)
        ladder.append({"step": "generated", "ok": bool(got),
                       "detail": (got or {}).get("look") or "abstract fill"})
        if got:
            return {"source": "generated", "asset": got, "query": queries[0],
                    "concepts": cq["concepts"], "ladder": ladder}
    return {"source": None, "asset": None, "query": queries[0], "concepts": cq["concepts"],
            "ladder": ladder}
