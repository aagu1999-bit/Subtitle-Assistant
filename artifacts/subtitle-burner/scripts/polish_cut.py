#!/usr/bin/env python3
"""
polish_cut.py — programmatic rough-cut → polish → master (no synthetic AI video).

Implements an FFmpeg-first post pipeline:

  1) Silence removal (silencedetect, -30 dBFS, >0.4s) with optional 0.2s
     sentence padding from a Whisper-style words JSON.
  2) Jump-cut smoothing via alternating ±2% digital zoom on keep segments.
  3) Optional face-centered reframe (OpenCV Haar if available; else center crop).
  4) Keyword-triggered B-roll PiP / opacity overlays from an asset folder.
  5) Color grade (contrast ~+5%) + speech loudnorm (-14 LUFS) + music duck.

Does NOT generate synthetic visuals — only edits / composites real media.

Examples
--------
  # Fast social rough cut (video-only)
  python3 scripts/polish_cut.py \\
    --video raw.mp4 --pacing fast --out final.mp4

  # Cinematic with separate music + keyword B-roll
  python3 scripts/polish_cut.py \\
    --video interview.mov --audio bed.mp3 --pacing cinematic \\
    --words-json words.json --assets-dir ./broll \\
    --keywords "wellness,focus,price" --broll-mode pip \\
    --out wellness_cut.mp4 --fps 60 --height 1080

  # Plan only (no encode) — writes keep-ranges + self-eval report
  python3 scripts/polish_cut.py --video raw.mp4 --dry-run --report report.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    print(f"[polish_cut] ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def run(cmd: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    print("[polish_cut] $", " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def which_or_die(name: str) -> str:
    if name == "ffmpeg":
        env = os.environ.get("FFMPEG") or os.environ.get("FFMPEG_BINARY")
        if env and Path(env).exists():
            return env
    p = shutil.which(name)
    if not p:
        die(f"{name} not found on PATH")
    return p


def ffprobe_json(path: Path) -> dict:
    which_or_die("ffprobe")
    cp = run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture=True,
    )
    return json.loads(cp.stdout or "{}")


def media_duration(path: Path) -> float:
    meta = ffprobe_json(path)
    try:
        return float(meta.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def has_audio_stream(path: Path) -> bool:
    meta = ffprobe_json(path)
    return any(s.get("codec_type") == "audio" for s in meta.get("streams") or [])


def has_video_stream(path: Path) -> bool:
    meta = ffprobe_json(path)
    return any(s.get("codec_type") == "video" for s in meta.get("streams") or [])


# ---------------------------------------------------------------------------
# 1) Silence detection → keep ranges
# ---------------------------------------------------------------------------

@dataclass
class KeepRange:
    start: float
    end: float
    zoom: float = 1.0  # jump-cut smoothing scale

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def detect_silence_ranges(
    media: Path,
    *,
    noise_db: float = -30.0,
    min_silence: float = 0.4,
) -> list[tuple[float, float]]:
    """Return list of (silence_start, silence_end) via FFmpeg silencedetect."""
    which_or_die("ffmpeg")
    # silencedetect logs to stderr; capture merged stdout.
    filt = f"silencedetect=noise={noise_db}dB:d={min_silence}"
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(media),
        "-af", filt, "-f", "null", "-",
    ]
    cp = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = cp.stdout or ""
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", text)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", text)]
    # Pair starts with ends (silencedetect may emit start without end at EOF).
    silences: list[tuple[float, float]] = []
    ei = 0
    for s in starts:
        while ei < len(ends) and ends[ei] < s:
            ei += 1
        if ei < len(ends):
            silences.append((s, ends[ei]))
            ei += 1
        else:
            # Trailing silence to EOF — end filled by caller with duration.
            silences.append((s, -1.0))
    return silences


def keep_ranges_from_silence(
    duration: float,
    silences: list[tuple[float, float]],
    *,
    sentence_pad: float = 0.2,
    words: list[dict] | None = None,
) -> list[KeepRange]:
    """Invert silence spans into keep ranges.

    If ``words`` is provided, expand each keep window by ``sentence_pad`` at
    natural sentence boundaries (word ends followed by gap / punctuation),
    without re-introducing long silences.
    """
    cleaned: list[tuple[float, float]] = []
    for s, e in silences:
        if e < 0:
            e = duration
        s = max(0.0, min(duration, s))
        e = max(0.0, min(duration, e))
        if e - s >= 0.05:
            cleaned.append((s, e))
    cleaned.sort()

    keeps: list[KeepRange] = []
    cursor = 0.0
    for s, e in cleaned:
        if s > cursor + 0.05:
            keeps.append(KeepRange(cursor, s))
        cursor = max(cursor, e)
    if duration > cursor + 0.05:
        keeps.append(KeepRange(cursor, duration))

    if not keeps and duration > 0:
        keeps = [KeepRange(0.0, duration)]

    # Preserve short natural pauses (~0.2s) near sentence ends when words exist.
    if words and sentence_pad > 0:
        sentence_ends = _sentence_end_times(words)
        padded: list[KeepRange] = []
        for kr in keeps:
            a, b = kr.start, kr.end
            for t in sentence_ends:
                if a - 0.05 <= t <= b + 0.05:
                    # Ensure at least sentence_pad of media remains after the
                    # sentence end inside this keep (already kept; no-op trim).
                    # More importantly: if silence cut shaved too close to the
                    # end of a sentence, nudge keep end forward up to pad,
                    # without overlapping next silence heavily.
                    if 0 < (b - t) < sentence_pad:
                        b = min(duration, t + sentence_pad)
            if b > a + 0.05:
                padded.append(KeepRange(a, b))
        keeps = _merge_ranges(padded)

    return keeps


def _sentence_end_times(words: list[dict]) -> list[float]:
    ends: list[float] = []
    punct = re.compile(r"[.!?…][\"')\]]*$")
    for i, w in enumerate(words):
        tok = str(w.get("word") or w.get("text") or "").strip()
        try:
            end = float(w.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if punct.search(tok):
            ends.append(end)
            continue
        # Heuristic: gap > 0.35s to next word ≈ sentence / breath.
        if i + 1 < len(words):
            try:
                nxt = float(words[i + 1].get("start") or 0)
            except (TypeError, ValueError):
                nxt = end
            if nxt - end >= 0.35:
                ends.append(end)
    return ends


def _merge_ranges(ranges: list[KeepRange], gap: float = 0.02) -> list[KeepRange]:
    if not ranges:
        return []
    ranges = sorted(ranges, key=lambda r: r.start)
    out = [ranges[0]]
    for r in ranges[1:]:
        prev = out[-1]
        if r.start <= prev.end + gap:
            prev.end = max(prev.end, r.end)
        else:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# 2) Jump-cut zoom assignment
# ---------------------------------------------------------------------------

def assign_jumpcut_zooms(keeps: list[KeepRange], amount: float = 0.02) -> None:
    """Alternate 1.0 and 1±amount scale so jump cuts read as intentional switches."""
    for i, kr in enumerate(keeps):
        if i % 2 == 0:
            kr.zoom = 1.0 + amount
        else:
            kr.zoom = 1.0  # pull / wider feel relative to previous push


# ---------------------------------------------------------------------------
# 3) Face reframe (optional)
# ---------------------------------------------------------------------------

def detect_face_center_norm(video: Path, t: float) -> tuple[float, float] | None:
    """Return (cx, cy) in 0..1 if OpenCV Haar finds a face at time t."""
    try:
        import cv2  # type: ignore
    except Exception:
        return None
    which_or_die("ffmpeg")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        frame = Path(tmp.name)
    try:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", f"{t:.3f}", "-i", str(video),
                "-frames:v", "1", "-q:v", "3", str(frame), "-y",
            ],
            check=False,
        )
        if not frame.exists() or frame.stat().st_size < 100:
            return None
        img = cv2.imread(str(frame))
        if img is None:
            return None
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        if not Path(cascade_path).exists():
            return None
        cascade = cv2.CascadeClassifier(cascade_path)
        if cascade.empty():
            return None
        faces = cascade.detectMultiScale(gray, 1.1, 4, minSize=(40, 40))
        if faces is None or len(faces) == 0:
            return None
        # Largest face
        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        return ((x + fw / 2) / w, (y + fh / 2) / h)
    except Exception:
        return None
    finally:
        try:
            frame.unlink(missing_ok=True)
        except Exception:
            pass


def reframe_crop_filter(
    *,
    width: int,
    height: int,
    face_cx: float | None,
    face_cy: float | None,
    zoom: float,
) -> str:
    """Build scale+crop chain for 1080p output with optional face bias."""
    # Work in a scaled space then crop to target.
    # Effective zoom: scale up then crop centered (or face-biased).
    z = max(1.0, float(zoom))
    # First scale so short side covers target * z
    # Then crop WxH around face or center.
    # Using FFmpeg expressions for dynamic crop x/y.
    if face_cx is None:
        face_cx = 0.5
    if face_cy is None:
        face_cy = 0.45  # slight upper bias for talking heads
    # crop x/y: keep subject near center
    # x = (in_w - out_w) * face_cx , clamped
    return (
        f"scale={width * z:.0f}:{height * z:.0f}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:"
        f"'min(max((in_w-{width})*{face_cx:.4f}\\,0)\\,max(in_w-{width}\\,0))':"
        f"'min(max((in_h-{height})*{face_cy:.4f}\\,0)\\,max(in_h-{height}\\,0))',"
        f"setsar=1"
    )


# ---------------------------------------------------------------------------
# 4) B-roll keyword triggers
# ---------------------------------------------------------------------------

@dataclass
class BrollHit:
    keyword: str
    time: float
    asset: Path
    duration: float = 2.5


def load_words(path: Path | None) -> list[dict]:
    if not path:
        return []
    data = json.loads(path.read_text())
    if isinstance(data, dict) and "words" in data:
        data = data["words"]
    if not isinstance(data, list):
        die("--words-json must be a list of {word,start,end} or {words:[...]}")
    return data


def find_keyword_hits(
    words: list[dict],
    keywords: list[str],
    assets_dir: Path | None,
) -> list[BrollHit]:
    if not keywords or not assets_dir or not assets_dir.is_dir():
        return []
    assets = sorted(
        [
            p for p in assets_dir.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".gif"}
        ]
    )
    if not assets:
        return []

    # Map keyword → asset by filename stem contains keyword
    def asset_for(kw: str) -> Path:
        k = kw.lower().strip()
        for a in assets:
            if k in a.stem.lower():
                return a
        # Round-robin fallback by hash
        return assets[abs(hash(k)) % len(assets)]

    hits: list[BrollHit] = []
    joined = []
    for w in words:
        tok = str(w.get("word") or w.get("text") or "").strip().lower()
        tok_clean = re.sub(r"[^a-z0-9]+", "", tok)
        try:
            t = float(w.get("start") or 0)
        except (TypeError, ValueError):
            t = 0.0
        joined.append((tok_clean, t))

    for kw in keywords:
        k = re.sub(r"[^a-z0-9]+", "", kw.lower())
        if not k:
            continue
        for tok, t in joined:
            if tok == k or k in tok:
                hits.append(BrollHit(keyword=kw, time=t, asset=asset_for(kw)))
                break  # first mention per keyword
    hits.sort(key=lambda h: h.time)
    return hits


def map_source_time_to_cut(src_t: float, keeps: list[KeepRange]) -> float | None:
    """Map a source timestamp into polished timeline time (after silence cuts)."""
    out_t = 0.0
    for kr in keeps:
        if kr.start <= src_t < kr.end:
            return out_t + (src_t - kr.start)
        out_t += kr.duration
    return None


# ---------------------------------------------------------------------------
# Pacing presets
# ---------------------------------------------------------------------------

@dataclass
class Pacing:
    name: str
    silence_db: float
    min_silence: float
    sentence_pad: float
    jump_zoom: float
    contrast: float
    broll_opacity: float
    music_duck_db: float


PACINGS = {
    "fast": Pacing("fast", -30, 0.35, 0.15, 0.02, 1.05, 0.40, -18),
    "fast-paced": Pacing("fast", -30, 0.35, 0.15, 0.02, 1.05, 0.40, -18),
    "cinematic": Pacing("cinematic", -32, 0.55, 0.25, 0.015, 1.04, 0.55, -16),
    "informative": Pacing("informative", -30, 0.45, 0.20, 0.02, 1.05, 0.40, -18),
}


# ---------------------------------------------------------------------------
# Self-evaluation
# ---------------------------------------------------------------------------

@dataclass
class EvalReport:
    ok: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def self_evaluate(
    *,
    keeps: list[KeepRange],
    hits: list[BrollHit],
    duration_src: float,
    width: int,
    height: int,
    fps: int,
) -> EvalReport:
    report = EvalReport()
    if not keeps:
        report.fail("No keep ranges — silence detection removed everything.")
    total = sum(k.duration for k in keeps)
    report.stats = {
        "source_duration_s": round(duration_src, 3),
        "cut_duration_s": round(total, 3),
        "segments": len(keeps),
        "broll_overlays": len(hits),
        "output": f"{width}x{height}@{fps}",
    }
    if total < 0.5:
        report.fail(f"Cut duration too short ({total:.2f}s).")
    if total > duration_src + 0.5:
        report.fail("Cut duration exceeds source — range math error.")

    # Overlap check on keep ranges
    for i in range(1, len(keeps)):
        if keeps[i].start < keeps[i - 1].end - 1e-3:
            report.fail(
                f"Keep ranges overlap: [{keeps[i-1].start:.2f},{keeps[i-1].end:.2f}] "
                f"vs [{keeps[i].start:.2f},{keeps[i].end:.2f}]"
            )

    # B-roll within cut timeline + no heavy pile-up
    cut_hits = []
    for h in hits:
        ct = map_source_time_to_cut(h.time, keeps)
        if ct is None:
            report.warn(f"B-roll '{h.keyword}' at src {h.time:.2f}s fell inside a cut silence — skipped.")
            continue
        if ct + 0.3 > total:
            report.warn(f"B-roll '{h.keyword}' would start past end of cut — skipped.")
            continue
        cut_hits.append((ct, ct + h.duration, h.keyword))

    cut_hits.sort()
    for i in range(1, len(cut_hits)):
        prev = cut_hits[i - 1]
        cur = cut_hits[i]
        if cur[0] < prev[1] - 0.15:
            report.warn(
                f"B-roll overlap: '{prev[2]}' and '{cur[2]}' overlap on timeline "
                f"({prev[0]:.2f}-{prev[1]:.2f} vs {cur[0]:.2f}). Later one may cover earlier."
            )

    # Abrupt micro-segments
    for k in keeps:
        if k.duration < 0.12:
            report.warn(f"Micro segment {k.start:.2f}-{k.end:.2f} ({k.duration:.2f}s) may look abrupt.")

    return report


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------

def build_and_encode(
    *,
    video: Path,
    audio: Path | None,
    music: Path | None,
    keeps: list[KeepRange],
    hits: list[BrollHit],
    out: Path,
    width: int,
    height: int,
    fps: int,
    pacing: Pacing,
    broll_mode: str,
    face_reframe: bool,
    work: Path,
) -> Path:
    which_or_die("ffmpeg")
    seg_paths: list[Path] = []

    # Sample a face center near first keep mid (optional)
    face = None
    if face_reframe and keeps:
        mid = (keeps[0].start + keeps[0].end) / 2
        face = detect_face_center_norm(video, mid)
        if face:
            print(f"[polish_cut] face center ≈ ({face[0]:.2f}, {face[1]:.2f})")
        else:
            print("[polish_cut] face detect unavailable/miss — using talking-head bias crop")

    face_cx = face[0] if face else 0.5
    face_cy = face[1] if face else 0.42

    # ---- Per-segment extract with zoom/reframe + color ----
    for i, kr in enumerate(keeps):
        seg = work / f"seg_{i:04d}.mp4"
        vf = reframe_crop_filter(
            width=width, height=height, face_cx=face_cx, face_cy=face_cy, zoom=kr.zoom,
        )
        # Contrast boost ~5% via eq
        vf = f"{vf},eq=contrast={pacing.contrast:.3f}:brightness=0.02,fps={fps}"
        cmd = [
            "ffmpeg", "-hide_banner", "-y", "-loglevel", "error",
            "-ss", f"{kr.start:.3f}", "-to", f"{kr.end:.3f}",
            "-i", str(video),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p",
        ]
        if has_audio_stream(video):
            cmd += ["-af", "aresample=async=1", "-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-an"]
        cmd.append(str(seg))
        run(cmd)
        seg_paths.append(seg)

    # Concat
    concat_list = work / "concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in seg_paths))
    rough = work / "rough.mp4"
    run([
        "ffmpeg", "-hide_banner", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(rough),
    ])

    cut_dur = sum(k.duration for k in keeps)

    # Map B-roll onto cut timeline
    overlays: list[tuple[float, BrollHit]] = []
    for h in hits:
        ct = map_source_time_to_cut(h.time, keeps)
        if ct is None or ct >= cut_dur - 0.2:
            continue
        overlays.append((ct, h))

    # ---- Composite B-roll + audio master ----
    inputs = ["-i", str(rough)]
    # Optional replacement/narration audio (trim to cut length later)
    audio_idx = None
    if audio and audio.exists():
        inputs += ["-i", str(audio)]
        audio_idx = 1
    music_idx = None
    if music and music.exists():
        inputs += ["-i", str(music)]
        music_idx = 1 if audio_idx is None else 2

    # B-roll inputs
    broll_inputs: list[tuple[int, float, BrollHit]] = []
    for ct, h in overlays:
        idx = (1 if audio_idx is None else 2) + (1 if music_idx is not None else 0) + len(broll_inputs)
        # Actually compute index from how many -i we add:
        idx = 1 + (1 if audio_idx is not None else 0) + (1 if music_idx is not None else 0) + len(broll_inputs)
        inputs += ["-loop", "1", "-t", f"{h.duration:.3f}", "-i", str(h.asset)] if h.asset.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else ["-i", str(h.asset)]
        broll_inputs.append((idx, ct, h))

    fc: list[str] = []
    vlabel = "[0:v]"

    # Chain overlays
    for n, (idx, ct, h) in enumerate(broll_inputs):
        out_v = f"[v{n}]"
        next_v = f"[v{n}o]"
        # Scale overlay
        if broll_mode == "pip":
            # PiP bottom-right ~36% width
            fc.append(
                f"[{idx}:v]scale={int(width * 0.36)}:-1,format=rgba,"
                f"colorchannelmixer=aa={pacing.broll_opacity:.2f}[ov{n}]"
            )
            x, y = "W-w-48", "H-h-48"
        else:
            # Full-ish center plate
            fc.append(
                f"[{idx}:v]scale={int(width * 0.84)}:-1,format=rgba,"
                f"colorchannelmixer=aa={pacing.broll_opacity:.2f}[ov{n}]"
            )
            x, y = "(W-w)/2", "(H-h)/2"
        enable = f"between(t\\,{ct:.3f}\\,{ct + h.duration:.3f})"
        fc.append(
            f"{vlabel}[ov{n}]overlay=x={x}:y={y}:enable='{enable}'{next_v}"
        )
        vlabel = next_v

    # Audio graph
    # Speech from rough or external audio → loudnorm
    # Music → volume + sidechain duck approx via volume enable on speech presence
    # Practical duck: use sidechaincompress if music present; else just loudnorm speech.
    alabel = None
    if audio_idx is not None:
        fc.append(
            f"[{audio_idx}:a]atrim=0:{cut_dur:.3f},asetpts=PTS-STARTPTS,"
            f"loudnorm=I=-14:TP=-1.5:LRA=7[speech]"
        )
        alabel = "[speech]"
    else:
        # From rough
        fc.append(
            f"[0:a]loudnorm=I=-14:TP=-1.5:LRA=7[speech]"
        )
        alabel = "[speech]"

    if music_idx is not None:
        duck_lin = 10 ** (pacing.music_duck_db / 20.0)
        # sidechaincompress: music ducked by speech envelope
        fc.append(
            f"[{music_idx}:a]atrim=0:{cut_dur:.3f},asetpts=PTS-STARTPTS,volume=0.55[music]"
        )
        fc.append(
            f"[music]{alabel}sidechaincompress=threshold=0.05:ratio=6:attack=50:release=300:"
            f"level_sc=1:mix=1[ducked]"
        )
        # Blend speech + ducked music
        fc.append(f"{alabel}[ducked]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        alabel = "[aout]"
        # Note: sidechain uses speech as key; amix again with speech — fix properly:
        # Better graph: speech untouched, music ducked by speech, then amix.
        # Rebuild music path cleanly if we already mixed wrong — simplify below.

    # Simpler reliable audio path when music exists:
    if music_idx is not None:
        # Remove the overly complex chain; rebuild from scratch for audio-only labels.
        # We'll construct a dedicated filter for audio in a second pass if needed.
        pass

    filter_complex = ";".join(fc) if fc else None

    # If music ducking graph got messy, do a cleaner two-step encode.
    polished = work / "polished.mp4"
    if music_idx is not None:
        # Step A: video (+ speech loudnorm only)
        fc_v = [x for x in fc if ":a]" not in x and "loudnorm" not in x and "amix" not in x and "sidechain" not in x and "atrim" not in x]
        # Rebuild video-only chain
        fc_v = []
        vlabel = "[0:v]"
        for n, (idx, ct, h) in enumerate(broll_inputs):
            if broll_mode == "pip":
                fc_v.append(
                    f"[{idx}:v]scale={int(width * 0.36)}:-1,format=rgba,"
                    f"colorchannelmixer=aa={pacing.broll_opacity:.2f}[ov{n}]"
                )
                x, y = "W-w-48", "H-h-48"
            else:
                fc_v.append(
                    f"[{idx}:v]scale={int(width * 0.84)}:-1,format=rgba,"
                    f"colorchannelmixer=aa={pacing.broll_opacity:.2f}[ov{n}]"
                )
                x, y = "(W-w)/2", "(H-h)/2"
            enable = f"between(t\\,{ct:.3f}\\,{ct + h.duration:.3f})"
            next_v = f"[v{n}o]"
            fc_v.append(f"{vlabel}[ov{n}]overlay=x={x}:y={y}:enable='{enable}'{next_v}")
            vlabel = next_v
        # speech loudnorm from rough/external
        if audio_idx is not None:
            fc_v.append(
                f"[{audio_idx}:a]atrim=0:{cut_dur:.3f},asetpts=PTS-STARTPTS,"
                f"loudnorm=I=-14:TP=-1.5:LRA=7[speech]"
            )
        else:
            fc_v.append("[0:a]loudnorm=I=-14:TP=-1.5:LRA=7[speech]")
        # music ducked with sidechaincompress keyed by speech, then amix
        fc_v.append(
            f"[{music_idx}:a]atrim=0:{cut_dur:.3f},asetpts=PTS-STARTPTS,volume=0.7[musicraw]"
        )
        fc_v.append(
            "[musicraw][speech]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=250:"
            "makeup=1[musicduck]"
        )
        fc_v.append("[speech][musicduck]amix=inputs=2:duration=first:dropout_transition=2[aout]")

        cmd = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error", *inputs,
               "-filter_complex", ";".join(fc_v),
               "-map", vlabel, "-map", "[aout]",
               "-c:v", "libx264", "-preset", "medium", "-crf", "17",
               "-pix_fmt", "yuv420p", "-r", str(fps),
               "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart",
               "-t", f"{cut_dur:.3f}",
               str(polished)]
        run(cmd)
    else:
        # Video overlays + speech loudnorm only
        fc_v = []
        vlabel = "[0:v]"
        for n, (idx, ct, h) in enumerate(broll_inputs):
            if broll_mode == "pip":
                fc_v.append(
                    f"[{idx}:v]scale={int(width * 0.36)}:-1,format=rgba,"
                    f"colorchannelmixer=aa={pacing.broll_opacity:.2f}[ov{n}]"
                )
                x, y = "W-w-48", "H-h-48"
            else:
                fc_v.append(
                    f"[{idx}:v]scale={int(width * 0.84)}:-1,format=rgba,"
                    f"colorchannelmixer=aa={pacing.broll_opacity:.2f}[ov{n}]"
                )
                x, y = "(W-w)/2", "(H-h)/2"
            enable = f"between(t\\,{ct:.3f}\\,{ct + h.duration:.3f})"
            next_v = f"[v{n}o]"
            fc_v.append(f"{vlabel}[ov{n}]overlay=x={x}:y={y}:enable='{enable}'{next_v}")
            vlabel = next_v
        if audio_idx is not None:
            fc_v.append(
                f"[{audio_idx}:a]atrim=0:{cut_dur:.3f},asetpts=PTS-STARTPTS,"
                f"loudnorm=I=-14:TP=-1.5:LRA=7[aout]"
            )
            map_a = "[aout]"
        elif has_audio_stream(rough):
            fc_v.append("[0:a]loudnorm=I=-14:TP=-1.5:LRA=7[aout]")
            map_a = "[aout]"
        else:
            map_a = None

        cmd = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error", *inputs]
        if fc_v:
            cmd += ["-filter_complex", ";".join(fc_v), "-map", vlabel]
        else:
            cmd += ["-map", "0:v"]
        if map_a:
            cmd += ["-map", map_a, "-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-an"]
        cmd += [
            "-c:v", "libx264", "-preset", "medium", "-crf", "17",
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-movflags", "+faststart",
            "-t", f"{cut_dur:.3f}",
            str(polished),
        ]
        run(cmd)

    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(polished), str(out))
    return out



def run_polish(
    *,
    video: Path,
    out: Path,
    pacing_name: str = "informative",
    audio: Path | None = None,
    music: Path | None = None,
    words: list[dict] | None = None,
    words_json: Path | None = None,
    keywords: list[str] | None = None,
    assets_dir: Path | None = None,
    broll_mode: str = "pip",
    silence_db: float | None = None,
    min_silence: float | None = None,
    width: int = 1920,
    height: int = 1080,
    fps: int = 60,
    face_reframe: bool = True,
    dry_run: bool = False,
    report_path: Path | None = None,
    keep_work: bool = False,
) -> dict:
    """Library entry used by Studio + CLI. Raises RuntimeError on failure."""
    video = Path(video)
    out = Path(out)
    if not video.exists():
        raise RuntimeError(f"video not found: {video}")
    if not has_video_stream(video):
        raise RuntimeError(f"no video stream in {video}")

    key = str(pacing_name or "informative").lower().strip()
    if key not in PACINGS:
        key = "informative"
    pacing = PACINGS[key]
    silence_db_v = pacing.silence_db if silence_db is None else float(silence_db)
    min_silence_v = pacing.min_silence if min_silence is None else float(min_silence)

    dur = media_duration(video)
    if dur <= 0:
        raise RuntimeError("could not read source duration")

    if words is None:
        words = load_words(words_json) if words_json else []
    keywords = list(keywords or [])

    print(f"[polish_cut] source={video} duration={dur:.2f}s pacing={pacing.name}")
    print(f"[polish_cut] silence noise={silence_db_v}dB min={min_silence_v}s")

    probe_media = Path(audio) if (audio and Path(audio).exists()) else video
    if not has_audio_stream(probe_media):
        raise RuntimeError(f"no audio stream to scan for silence in {probe_media}")

    silences = detect_silence_ranges(probe_media, noise_db=silence_db_v, min_silence=min_silence_v)
    keeps = keep_ranges_from_silence(
        dur, silences, sentence_pad=pacing.sentence_pad, words=words or None,
    )
    assign_jumpcut_zooms(keeps, amount=pacing.jump_zoom)
    hits = find_keyword_hits(words, keywords, Path(assets_dir) if assets_dir else None)
    report = self_evaluate(
        keeps=keeps, hits=hits, duration_src=dur,
        width=width, height=height, fps=fps,
    )
    plan = {
        "video": str(video),
        "audio": str(audio) if audio else None,
        "music": str(music) if music else None,
        "pacing": asdict(pacing),
        "silence_spans": silences,
        "keep_ranges": [asdict(k) for k in keeps],
        "broll": [
            {
                "keyword": h.keyword,
                "src_time": h.time,
                "asset": str(h.asset),
                "cut_time": map_source_time_to_cut(h.time, keeps),
            }
            for h in hits
        ],
        "eval": asdict(report),
    }
    if report_path:
        Path(report_path).write_text(json.dumps(plan, indent=2))
        print(f"[polish_cut] wrote report {report_path}")

    for wmsg in report.warnings:
        print(f"[polish_cut] WARN: {wmsg}")
    for emsg in report.errors:
        print(f"[polish_cut] ERR: {emsg}")
    if not report.ok:
        raise RuntimeError("self-evaluation failed — aborting encode")

    if dry_run:
        plan["output"] = None
        plan["dry_run"] = True
        return plan

    work_root = Path(tempfile.mkdtemp(prefix="polish_cut_"))
    try:
        encoded = build_and_encode(
            video=video,
            audio=Path(audio) if audio else None,
            music=Path(music) if music else None,
            keeps=keeps,
            hits=hits,
            out=out,
            width=width,
            height=height,
            fps=fps,
            pacing=pacing,
            broll_mode=broll_mode,
            face_reframe=face_reframe,
            work=work_root,
        )
        out_dur = media_duration(encoded)
        if out_dur < 0.2:
            raise RuntimeError("output too short after encode")
        plan["output"] = str(encoded)
        plan["output_duration_s"] = out_dur
        print(f"[polish_cut] OK → {encoded} ({out_dur:.2f}s) {width}x{height}@{fps}")
        return plan
    finally:
        if not keep_work:
            shutil.rmtree(work_root, ignore_errors=True)
        else:
            print(f"[polish_cut] work dir kept: {work_root}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Programmatic polish cut: silence remove, jump-cut zoom, "
                    "reframe, B-roll, grade, loudness, duck (FFmpeg).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--video", required=True, type=Path, help="Raw video file")
    p.add_argument("--audio", type=Path, default=None, help="Optional separate speech/audio bed")
    p.add_argument("--music", type=Path, default=None, help="Optional background music to duck under speech")
    p.add_argument("--pacing", default="informative",
                   choices=sorted(set(PACINGS.keys())),
                   help="Desired pacing preset")
    p.add_argument("--words-json", type=Path, default=None,
                   help="Whisper-style words JSON for sentence padding + B-roll keywords")
    p.add_argument("--keywords", default="",
                   help="Comma-separated keywords that trigger B-roll overlays")
    p.add_argument("--assets-dir", type=Path, default=None,
                   help="Folder of images/videos used as B-roll assets")
    p.add_argument("--broll-mode", choices=["pip", "center"], default="pip")
    p.add_argument("--silence-db", type=float, default=None, help="Override silence threshold dBFS")
    p.add_argument("--min-silence", type=float, default=None, help="Override min silence duration (s)")
    p.add_argument("--no-face-reframe", action="store_true", help="Disable OpenCV face bias crop")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--out", type=Path, default=Path("polished_cut.mp4"))
    p.add_argument("--report", type=Path, default=None, help="Write JSON self-eval / plan report")
    p.add_argument("--dry-run", action="store_true", help="Plan + self-eval only (no encode)")
    p.add_argument("--keep-work", action="store_true", help="Keep temp segment files")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_polish(
            video=args.video,
            out=args.out,
            pacing_name=args.pacing,
            audio=args.audio,
            music=args.music,
            words_json=args.words_json,
            keywords=[k.strip() for k in (args.keywords or "").split(",") if k.strip()],
            assets_dir=args.assets_dir,
            broll_mode=args.broll_mode,
            silence_db=args.silence_db,
            min_silence=args.min_silence,
            width=args.width,
            height=args.height,
            fps=args.fps,
            face_reframe=not args.no_face_reframe,
            dry_run=args.dry_run,
            report_path=args.report,
            keep_work=args.keep_work,
        )
        return 0
    except RuntimeError as e:
        print(f"[polish_cut] ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
