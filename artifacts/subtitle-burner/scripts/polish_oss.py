"""Open-source engine adapters for polish_cut.

Wires real third-party tools (not FFmpeg re-implementations alone):

  1) Auto-Editor — silence / loudness rough-cut + native Kdenlive & Shotcut export
  2) MoviePy — code-driven B-roll + lower-third composite
  3) Kdenlive / Shotcut — project files produced by Auto-Editor (--export)

Remotion is a React/Node framework and is not invoked from this Python Studio
path; MoviePy covers the same “code-driven overlays” role here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def which_auto_editor() -> str | None:
    return shutil.which("auto-editor")


def moviepy_available() -> bool:
    try:
        import moviepy  # noqa: F401
        return True
    except Exception:
        return False


def _tc_to_seconds(tc: str) -> float:
    """Parse MLT/Kdenlive timecode: HH:MM:SS.mmm or HH:MM:SS:FF."""
    tc = (tc or "").strip().replace(",", ".")
    if not tc:
        return 0.0
    parts = tc.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 4:
            h, m, s, fr = parts
            # Prefer fractional-second style if last part looks like frames < 100
            return int(h) * 3600 + int(m) * 60 + int(float(s)) + (float(fr) / 60.0)
        return float(tc)
    except (TypeError, ValueError):
        return 0.0


def parse_mlt_keep_ranges(path: Path) -> list[tuple[float, float]]:
    """Extract source keep (in,out) spans from a Kdenlive/Shotcut MLT XML."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []
    best: list[tuple[float, float]] = []
    for playlist in root.findall("playlist"):
        entries = playlist.findall("entry")
        if len(entries) < 1:
            continue
        spans: list[tuple[float, float]] = []
        for e in entries:
            inn, out = e.get("in"), e.get("out")
            if not inn or not out:
                continue
            a, b = _tc_to_seconds(inn), _tc_to_seconds(out)
            if b > a + 0.04:
                spans.append((a, b))
        if len(spans) > len(best):
            best = spans
    return best


def run_auto_editor(
    video: Path,
    *,
    work: Path,
    noise_db: float = -30.0,
    margin_s: float = 0.2,
    fps: int = 60,
    width: int = 1920,
    height: int = 1080,
    cut_outs: list[tuple[float, float]] | None = None,
    render_mp4: bool = True,
    export_kdenlive: bool = True,
    export_shotcut: bool = True,
) -> dict[str, Any]:
    """Run Auto-Editor for silence cut + NLE project exports.

    Returns dict with keys: binary, mp4, kdenlive, shotcut, keeps, cmd_log
    """
    ae = which_auto_editor()
    if not ae:
        raise RuntimeError(
            "auto-editor not found on PATH. Install with: pip install auto-editor"
        )
    video = Path(video)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    stem = video.stem
    out: dict[str, Any] = {
        "binary": ae,
        "mp4": None,
        "kdenlive": None,
        "shotcut": None,
        "keeps": [],
        "cmd_log": [],
    }

    def _base_cmd() -> list[str]:
        cmd = [
            ae, str(video),
            "--edit", f"audio:{noise_db:g}dB",
            "--margin", f"{margin_s:g}s",
            "--frame-rate", str(int(fps)),
            "--resolution", f"{int(width)},{int(height)}",
            "--no-open", "-q",
        ]
        if cut_outs:
            cmd.append("--cut-out")
            for a, b in cut_outs:
                if b > a:
                    cmd.append(f"{a:.3f}s,{b:.3f}s")
        return cmd

    # Native Kdenlive project (also yields keep ranges we can parse)
    if export_kdenlive:
        cmd = _base_cmd() + ["--export", "kdenlive"]
        out["cmd_log"].append(cmd)
        subprocess.run(cmd, cwd=str(work), check=False, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # Auto-Editor writes next to the source by default — also check cwd.
        candidates = list(video.parent.glob(f"{stem}*ALTERED*.kdenlive")) + \
            list(work.glob(f"{stem}*ALTERED*.kdenlive")) + \
            list(video.parent.glob(f"{stem}*.kdenlive")) + \
            list(work.glob("*.kdenlive"))
        if candidates:
            dest = work / f"{stem}_polish.kdenlive"
            src = max(candidates, key=lambda p: p.stat().st_mtime)
            if src.resolve() != dest.resolve():
                shutil.copy2(src, dest)
            out["kdenlive"] = str(dest)
            out["keeps"] = parse_mlt_keep_ranges(dest)

    if export_shotcut:
        cmd = _base_cmd() + ["--export", "shotcut"]
        out["cmd_log"].append(cmd)
        subprocess.run(cmd, cwd=str(work), check=False, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        candidates = list(video.parent.glob(f"{stem}*ALTERED*.mlt")) + \
            list(work.glob(f"{stem}*ALTERED*.mlt")) + \
            list(work.glob("*.mlt"))
        if candidates:
            dest = work / f"{stem}_polish.mlt"
            src = max(candidates, key=lambda p: p.stat().st_mtime)
            if src.resolve() != dest.resolve():
                shutil.copy2(src, dest)
            out["shotcut"] = str(dest)
            if not out["keeps"]:
                out["keeps"] = parse_mlt_keep_ranges(dest)

    if render_mp4:
        mp4 = work / f"{stem}_ae_silence.mp4"
        cmd = _base_cmd() + ["-o", str(mp4)]
        out["cmd_log"].append(cmd)
        cp = subprocess.run(cmd, cwd=str(work), check=False, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if mp4.exists() and mp4.stat().st_size > 0:
            out["mp4"] = str(mp4)
        else:
            raise RuntimeError(
                f"auto-editor render failed: {(cp.stdout or '')[-800:]}"
            )

    return out


def composite_with_moviepy(
    *,
    rough: Path,
    out: Path,
    overlays: list[tuple[float, Any]],
    broll_mode: str = "pip",
    lower_thirds: bool = True,
    width: int = 1920,
    height: int = 1080,
    fps: int = 60,
    music: Path | None = None,
    music_duck_db: float = -18.0,
) -> Path:
    """Composite B-roll + lower-thirds with MoviePy (code-driven overlays)."""
    try:
        from moviepy import (
            AudioFileClip,
            CompositeAudioClip,
            CompositeVideoClip,
            ImageClip,
            TextClip,
            VideoFileClip,
            concatenate_videoclips,
        )
    except Exception as e:
        raise RuntimeError(f"MoviePy not available: {e}") from e

    rough = Path(rough)
    out = Path(out)
    base = VideoFileClip(str(rough))
    # Resize canvas if needed
    if abs(base.w - width) > 2 or abs(base.h - height) > 2:
        try:
            base = base.resized((width, height))
        except Exception:
            pass

    layers = [base]
    for ct, hit in overlays:
        asset = Path(getattr(hit, "asset", hit.get("asset") if isinstance(hit, dict) else ""))
        if not asset or not asset.exists():
            continue
        dur = float(getattr(hit, "duration", 2.5) or 2.5)
        keyword = str(getattr(hit, "keyword", "") or "")
        try:
            if asset.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                clip = ImageClip(str(asset)).with_duration(dur)
            else:
                clip = VideoFileClip(str(asset)).subclipped(0, min(dur, 8.0))
            target_w = int(width * (0.36 if broll_mode == "pip" else 0.84))
            clip = clip.resized(width=target_w)
            if broll_mode == "pip":
                clip = clip.with_position((width - clip.w - 48, height - clip.h - 48))
            else:
                clip = clip.with_position(("center", "center"))
            clip = clip.with_start(ct).with_opacity(0.92 if broll_mode == "pip" else 0.88)
            layers.append(clip)
            if lower_thirds and keyword:
                try:
                    txt = TextClip(
                        text=keyword.strip().title()[:48],
                        font_size=max(28, int(width * 0.028)),
                        color="white",
                        bg_color="black",
                        method="label",
                    ).with_duration(min(2.2, dur)).with_start(ct).with_position((56, height - 140))
                    layers.append(txt)
                except Exception:
                    # TextClip needs a system font; skip rather than fail polish.
                    pass
        except Exception as e:
            print(f"[polish_oss] MoviePy overlay skip {asset.name}: {e}")

    final = CompositeVideoClip(layers, size=(width, height)).with_duration(base.duration)

    # Audio: keep speech from rough; optionally duck-mix music (simple volume)
    audio = base.audio
    if music and Path(music).exists():
        try:
            mclip = AudioFileClip(str(music)).subclipped(0, base.duration)
            duck = 10 ** (float(music_duck_db) / 20.0)
            mclip = mclip.with_volume_scaled(max(0.05, min(1.0, duck * 0.55)))
            if audio is not None:
                audio = CompositeAudioClip([audio, mclip])
            else:
                audio = mclip
        except Exception as e:
            print(f"[polish_oss] MoviePy music mix skipped: {e}")
    if audio is not None:
        final = final.with_audio(audio)

    out.parent.mkdir(parents=True, exist_ok=True)
    final.write_videofile(
        str(out),
        fps=fps,
        codec="libx264",
        audio_codec="aac",
        bitrate="8M",
        audio_bitrate="192k",
        logger=None,
    )
    try:
        base.close()
        final.close()
    except Exception:
        pass
    return out
