"""Post-render QA: the boring checks a human editor does before posting.

Programmatic checks run on every render and cost one extra decode pass:
resolution / fps / duration against what was asked for, integrated loudness
and true peak against the -14 LUFS target, black frames, dead air, and
whether caption boxes stay on screen and out of the platform UI. A vision
spot-check over a handful of keyframes is opt-in: Gemini when a key is set,
otherwise a local face-vs-caption overlap test with MediaPipe.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

TARGET_LUFS = -14.0
LUFS_TOLERANCE = 2.0
PEAK_CEILING = -1.0


def _check(cid: str, label: str, status: str, detail: str, **extra) -> dict:
    return {"id": cid, "label": label, "status": status, "detail": detail, **extra}


# ---- probe ------------------------------------------------------------------------

def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration",
         "-of", "default=nw=0", str(path)], capture_output=True, text=True, timeout=60).stdout
    info = {"has_audio": False, "has_video": False}
    kind = None
    for line in out.splitlines():
        if line.startswith("[STREAM]"):
            kind = None
        k, _, v = line.partition("=")
        if k == "codec_type":
            kind = v
            info["has_audio" if v == "audio" else "has_video"] = True
        elif kind == "video" and k == "width":
            info["width"] = int(v)
        elif kind == "video" and k == "height":
            info["height"] = int(v)
        elif kind == "video" and k == "codec_name":
            info["vcodec"] = v
        elif kind == "video" and k == "r_frame_rate" and "/" in v:
            n, d = v.split("/")
            info["fps"] = round(float(n) / float(d), 3) if float(d) else 0.0
        elif k == "duration" and v not in ("", "N/A"):
            info["duration"] = round(float(v), 3)
    return info


def check_format(info: dict, W: int | None, H: int | None, fps: float | None,
                 duration: float | None) -> list[dict]:
    checks = []
    if W and H:
        got = (info.get("width"), info.get("height"))
        ok = got == (W, H)
        checks.append(_check("resolution", "Resolution", "pass" if ok else "fail",
                             f"{got[0]}x{got[1]}" + ("" if ok else f" (expected {W}x{H})"),
                             value=f"{got[0]}x{got[1]}"))
    if fps:
        got = info.get("fps") or 0
        ok = abs(got - fps) < 0.5
        checks.append(_check("fps", "Frame rate", "pass" if ok else "warn",
                             f"{got:g} fps" + ("" if ok else f" (expected {fps:g})"), value=got))
    if duration:
        got = info.get("duration") or 0
        off = got - duration
        ok = abs(off) <= 0.35
        checks.append(_check("duration", "Duration", "pass" if ok else "warn",
                             f"{got:.2f}s" + ("" if ok else f" ({off:+.2f}s vs the timeline)"),
                             value=got))
    if not info.get("has_audio"):
        checks.append(_check("audio_stream", "Audio track", "warn", "The export has no audio."))
    return checks


# ---- one decode pass: loudness, peak, black frames, dead air ---------------------

def analyze_av(path: Path, ffmpeg: str = "ffmpeg", has_audio: bool = True) -> dict:
    """Integrated LUFS, true peak, black ranges and silences from one ffmpeg run."""
    if has_audio:
        graph = ("[0:v]scale=320:-2,blackdetect=d=0.3:pix_th=0.10[v];"
                 "[0:a]ebur128=peak=true,silencedetect=n=-50dB:d=2.0[a]")
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        graph = "[0:v]scale=320:-2,blackdetect=d=0.3:pix_th=0.10[v]"
        maps = ["-map", "[v]"]
    proc = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
                           "-filter_complex", graph, *maps, "-f", "null", "-"],
                          capture_output=True, text=True, timeout=900)
    err = proc.stderr
    res = {"black": [], "silence": [], "lufs": None, "peak": None}
    for m in re.finditer(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)", err):
        res["black"].append({"start": float(m.group(1)), "end": float(m.group(2))})
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", err)]
    ends = re.findall(r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)", err)
    for i, s in enumerate(starts):
        e = float(ends[i][0]) if i < len(ends) else None
        res["silence"].append({"start": max(0.0, s), "end": e})
    summary = err[err.rfind("Summary:"):] if "Summary:" in err else ""
    m = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", summary)
    if m and m.group(1) != "-inf":
        res["lufs"] = float(m.group(1))
    m = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary)
    if m and m.group(1) != "-inf":
        res["peak"] = float(m.group(1))
    return res


def check_av(av: dict, duration: float | None, target: float = TARGET_LUFS) -> list[dict]:
    checks = []
    if av.get("lufs") is not None:
        off = av["lufs"] - target
        status = "pass" if abs(off) <= LUFS_TOLERANCE else "warn"
        hint = "" if status == "pass" else (" — quieter than feeds expect" if off < 0
                                            else " — louder than feeds normalise to")
        checks.append(_check("loudness", "Loudness", status,
                             f"{av['lufs']:.1f} LUFS (target {target:g}){hint}", value=av["lufs"]))
    if av.get("peak") is not None:
        ok = av["peak"] <= PEAK_CEILING + 0.05
        checks.append(_check("peak", "True peak", "pass" if ok else "warn",
                             f"{av['peak']:.1f} dBTP" + ("" if ok else " — may clip after upload"),
                             value=av["peak"]))
    # A fade through black at a transition is a choice; a long run is a bug.
    black = [b for b in av.get("black") or [] if b["end"] - b["start"] >= 0.5]
    checks.append(_check("black_frames", "Black frames", "warn" if black else "pass",
                         (f"{len(black)} black stretch(es), first at {black[0]['start']:.1f}s"
                          if black else "None longer than half a second"), items=black[:10]))
    dead = [s for s in av.get("silence") or []
            if (s["end"] or duration or s["start"]) - s["start"] >= 2.0]
    checks.append(_check("dead_air", "Dead air", "warn" if dead else "pass",
                         (f"{len(dead)} silent stretch(es) of 2s+, first at {dead[0]['start']:.1f}s"
                          if dead else "No silences of 2s or more"), items=dead[:10]))
    return checks


# ---- caption boxes ---------------------------------------------------------------------

def _ass_time(s: str) -> float:
    h, m, rest = s.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def caption_boxes(ass_text: str, W: int, H: int, font_loader=None) -> list[dict]:
    """Pixel boxes for every dialogue event in an ASS document."""
    styles, fmt = {}, None
    events = []
    for line in (ass_text or "").splitlines():
        if line.startswith("Format:") and fmt is None and "Fontname" in line:
            fmt = [x.strip() for x in line[7:].split(",")]
        elif line.startswith("Style:") and fmt:
            vals = [x.strip() for x in line[6:].split(",")]
            st = dict(zip(fmt, vals))
            styles[st.get("Name")] = st
        elif line.startswith("Dialogue:"):
            parts = line[9:].split(",", 9)
            if len(parts) == 10:
                events.append(parts)
    boxes = []
    cache = {}
    for parts in events:
        try:
            start, end = _ass_time(parts[1]), _ass_time(parts[2])
        except ValueError:
            continue
        st = styles.get(parts[3].strip()) or {}
        raw = parts[9]
        tags = "".join(re.findall(r"\{([^}]*)\}", raw))
        text = re.sub(r"\{[^}]*\}", "", raw)
        if not text.strip():
            continue
        try:
            size = float(st.get("Fontsize") or 48)
            align = int(st.get("Alignment") or 2)
            mv = int(parts[7]) or int(st.get("MarginV") or 0)
            ml = int(parts[5]) or int(st.get("MarginL") or 0)
            mr = int(parts[6]) or int(st.get("MarginR") or 0)
            outline = float(st.get("Outline") or 0)
        except ValueError:
            continue
        m = re.search(r"\\fs(\d+(?:\.\d+)?)", tags)
        if m:
            size = float(m.group(1))
        m = re.search(r"\\an(\d)", tags)
        if m:
            align = int(m.group(1))
        fscx = 1.0
        m = re.search(r"\\fscx(\d+(?:\.\d+)?)", tags)
        if m:
            fscx = float(m.group(1)) / 100.0
        lines = text.replace("\\n", "\\N").split("\\N")
        font_name = st.get("Fontname") or "Sans"
        key = (font_name, int(size))
        if key not in cache:
            cache[key] = font_loader(font_name, int(size)) if font_loader else None
        font = cache[key]
        widths = []
        for ln in lines:
            if font is not None:
                b = font.getbbox(ln)
                widths.append((b[2] - b[0]) * fscx)
            else:
                widths.append(len(ln) * size * 0.55 * fscx)
        bw = max(widths) + outline * 2
        bh = len(lines) * size * 1.15 + outline * 2
        m = re.search(r"\\(?:pos|move)\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)", tags)
        col = (align - 1) % 3          # 0 left, 1 centre, 2 right
        row = (align - 1) // 3         # 0 bottom, 1 middle, 2 top
        if m:
            ax, ay = float(m.group(1)), float(m.group(2))
        else:
            ax = ml if col == 0 else (W - mr if col == 2 else W / 2)
            ay = H - mv if row == 0 else (mv if row == 2 else H / 2)
        x0 = ax - (0 if col == 0 else (bw if col == 2 else bw / 2))
        y0 = ay - (bh if row == 0 else (0 if row == 2 else bh / 2))
        boxes.append({"start": start, "end": end, "text": " ".join(lines)[:60],
                      "x0": round(x0, 1), "y0": round(y0, 1),
                      "x1": round(x0 + bw, 1), "y1": round(y0 + bh, 1)})
    return boxes


def safe_zone(W: int, H: int) -> dict:
    """Where Shorts/Reels/TikTok chrome sits (portrait) or title-safe (landscape)."""
    if H > W:
        return {"left": 0.05 * W, "right": W - 0.12 * W, "top": 0.06 * H, "bottom": H - 0.20 * H,
                "name": "vertical feed UI"}
    return {"left": 0.05 * W, "right": 0.95 * W, "top": 0.05 * H, "bottom": 0.95 * H,
            "name": "title-safe"}


def check_captions(boxes: list[dict], W: int, H: int) -> list[dict]:
    if not boxes:
        return [_check("caption_safe", "Caption safe zone", "skip", "No captions or titles burned.")]
    z = safe_zone(W, H)
    off, ui = [], []
    for b in boxes:
        if b["x0"] < 0 or b["x1"] > W or b["y0"] < 0 or b["y1"] > H:
            off.append(b)
        elif b["x0"] < z["left"] or b["x1"] > z["right"] or b["y0"] < z["top"] or b["y1"] > z["bottom"]:
            ui.append(b)
    checks = []
    checks.append(_check(
        "caption_frame", "Captions on screen", "fail" if off else "pass",
        (f"{len(off)} caption(s) run off the frame, first at {off[0]['start']:.1f}s: "
         f"“{off[0]['text']}”" if off else f"All {len(boxes)} caption events fit the frame"),
        items=off[:10]))
    checks.append(_check(
        "caption_safe", "Caption safe zone", "warn" if ui else "pass",
        (f"{len(ui)} caption(s) sit under the {z['name']}, first at {ui[0]['start']:.1f}s"
         if ui else f"Clear of the {z['name']}"), items=ui[:10]))
    return checks


# ---- keyframes + vision ---------------------------------------------------------------

def keyframe_times(duration: float, n: int = 5) -> list[float]:
    n = max(1, n)
    return [round(duration * (i + 0.5) / n, 3) for i in range(n)]


def extract_keyframes(path: Path, times: list[float], out_dir: Path, stem: str,
                      ffmpeg: str = "ffmpeg", width: int = 360) -> list[dict]:
    frames = []
    for i, t in enumerate(times):
        dest = Path(out_dir) / f"{stem}_qa{i}.jpg"
        proc = subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
                               "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(dest)],
                              capture_output=True, timeout=60)
        if proc.returncode == 0 and dest.exists():
            frames.append({"t": t, "file": dest.name, "path": dest})
    return frames


VISION_PROMPT = (
    "You are a meticulous short-form video editor doing a final check. Each image is a "
    "frame from an export, labelled with its time. Report only real problems a viewer "
    "would notice: captions or graphics covering a face, unreadable or clipped text, "
    "awkward crops that cut off a head, frames that look broken or black, graphics that "
    "clash with the footage. Reply as JSON: {\"issues\": [{\"t\": seconds, "
    "\"severity\": \"warn\"|\"fail\", \"problem\": short sentence}]}. Empty list if it looks right."
)


def _overlap(a: dict, b: dict) -> float:
    w = max(0.0, min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]))
    h = max(0.0, min(a["y1"], b["y1"]) - max(a["y0"], b["y0"]))
    return w * h


def local_face_boxes(jpg_path: Path) -> list[dict]:
    """Normalised face boxes via MediaPipe face detection ([] if unavailable)."""
    try:
        import mediapipe as mp
        import numpy as np
        from PIL import Image
    except Exception:
        return []
    img = np.asarray(Image.open(jpg_path).convert("RGB"))
    with mp.solutions.face_detection.FaceDetection(model_selection=1,
                                                   min_detection_confidence=0.5) as fd:
        res = fd.process(img)
    out = []
    for d in (res.detections or []) if res else []:
        bb = d.location_data.relative_bounding_box
        out.append({"x0": bb.xmin, "y0": bb.ymin, "x1": bb.xmin + bb.width,
                    "y1": bb.ymin + bb.height})
    return out


def local_vision(frames: list[dict], boxes: list[dict], W: int, H: int,
                 graphics: list[dict] | None = None, face_fn=local_face_boxes) -> dict:
    """Faces vs captions/graphics on each keyframe, without any API."""
    issues = []
    for f in frames:
        faces = face_fn(f["path"])
        live = [b for b in boxes if b["start"] <= f["t"] < b["end"]]
        live_g = [g for g in graphics or [] if g["start"] <= f["t"] < g["end"]]
        for face in faces:
            fb = {"x0": face["x0"] * W, "y0": face["y0"] * H, "x1": face["x1"] * W,
                  "y1": face["y1"] * H}
            area = max(1.0, (fb["x1"] - fb["x0"]) * (fb["y1"] - fb["y0"]))
            for b in live:
                if _overlap(fb, b) / area > 0.15:
                    issues.append({"t": f["t"], "severity": "warn",
                                   "problem": f"Caption “{b['text'][:30]}” covers a face"})
                    break
            for g in live_g:
                if _overlap(fb, g) / area > 0.25:
                    issues.append({"t": f["t"], "severity": "warn",
                                   "problem": f"{g.get('label') or 'A graphic'} covers a face"
                                              " — try Behind me or move it"})
                    break
    return {"engine": "local", "issues": issues, "frames": len(frames)}


def _merge_issues(a: list, b: list) -> list:
    """Union of two issue lists, near-duplicates collapsed."""
    out, seen = [], set()
    for it in list(a or []) + list(b or []):
        key = (round(float(it.get("t") or 0), 1),
               str(it.get("problem") or "")[:40].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return sorted(out, key=lambda i: float(i.get("t") or 0))


def run_vision(frames: list[dict], llm_vision=None, **local_kwargs) -> dict:
    """The model over the keyframes AND the local geometry check, merged.

    These two see different things and neither subsumes the other. The
    local pass is handed the exact caption and graphic boxes, so it knows
    for certain when one lands on a face; the model only ever sees pixels,
    but it is the only thing that can call text unreadable or a crop bad.

    The model path used to `return` on success, so whenever a key was
    configured `local_kwargs` -- including the graphics boxes computed for
    exactly this purpose -- was built and then dropped on the floor.
    """
    if llm_vision and frames:
        try:
            images = [(f["t"], Path(f["path"]).read_bytes()) for f in frames]
            res = llm_vision(VISION_PROMPT, images) or {}
            issues = []
            for it in res.get("issues") or []:
                if not isinstance(it, dict) or not it.get("problem"):
                    continue
                sev = it.get("severity") if it.get("severity") in ("warn", "fail") else "warn"
                try:
                    t = float(it.get("t") or 0)
                except (TypeError, ValueError):
                    t = 0.0
                issues.append({"t": t, "severity": sev, "problem": str(it["problem"])[:200]})
            engine = "model"
            try:
                loc = local_vision(frames, **local_kwargs)
                if loc.get("issues"):
                    issues = _merge_issues(issues, loc["issues"])
                engine = "model+local"
            except Exception:
                pass          # the model's findings still stand on their own
            return {"engine": engine, "issues": issues, "frames": len(frames)}
        except Exception as e:  # fall through to the local check, but say so
            out = local_vision(frames, **local_kwargs)
            out["note"] = f"The vision model was unavailable ({str(e)[:120]}); used the local face check."
            return out
    return local_vision(frames, **local_kwargs)


# ---- the report ---------------------------------------------------------------------------

def run_qa(path: Path, *, W: int | None = None, H: int | None = None, fps: float | None = None,
           duration: float | None = None, ass_text: str | None = None, font_loader=None,
           keyframe_dir: Path | None = None, stem: str = "qa", ffmpeg: str = "ffmpeg") -> dict:
    t0 = time.time()
    info = probe(path)
    W = W or info.get("width")
    H = H or info.get("height")
    checks = check_format(info, W, H, fps, duration)
    av = analyze_av(path, ffmpeg, info.get("has_audio", False))
    checks += check_av(av, info.get("duration"))
    boxes = caption_boxes(ass_text or "", W or 1080, H or 1920, font_loader) if ass_text else []
    checks += check_captions(boxes, W or 1080, H or 1920)
    frames = []
    if keyframe_dir is not None and info.get("duration"):
        frames = extract_keyframes(path, keyframe_times(info["duration"]), keyframe_dir, stem, ffmpeg)
    worst = "pass"
    for c in checks:
        if c["status"] == "fail":
            worst = "fail"
        elif c["status"] == "warn" and worst == "pass":
            worst = "warn"
    return {
        "status": worst, "checks": checks, "probe": info,
        "loudness": {"lufs": av.get("lufs"), "peak": av.get("peak")},
        "keyframes": [{"t": f["t"], "file": f["file"]} for f in frames],
        "_frames": frames, "_boxes": boxes,
        "seconds": round(time.time() - t0, 2), "ran_at": time.time(),
    }


def public(report: dict) -> dict:
    """The report without internal paths (safe to JSON-serialise)."""
    return {k: v for k, v in (report or {}).items() if not k.startswith("_")}


def image_part(data: bytes) -> dict:
    import base64
    return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(data).decode()}}

