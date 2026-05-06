"""
Subtitle Burner - A Flask web app that adds word-by-word highlighted
captions to videos using Whisper for transcription and FFmpeg for rendering.
"""
import os
import re
import time
import uuid
import json
import base64
import shutil
import hashlib
import sqlite3
import subprocess
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, Response

app = Flask(__name__)

# ---- Config ----
BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
FONT_DIR = BASE_DIR / "fonts"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
FONT_DIR.mkdir(exist_ok=True)

app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB
ALLOWED_EXT = {"mp4", "mov", "mkv", "webm", "avi", "m4v"}

# How long (seconds) to keep finished output MP4s before deleting them.
# Override via the OUTPUT_TTL_SECONDS environment variable.
try:
    OUTPUT_TTL_SECONDS = max(60, int(os.environ.get("OUTPUT_TTL_SECONDS", 21600)))
except (ValueError, TypeError):
    OUTPUT_TTL_SECONDS = 21600

# How long (seconds) before an abandoned upload (awaiting_edit / queued /
# transcribing with no follow-up render) is removed from disk and memory.
# Defaults to the same value as OUTPUT_TTL_SECONDS.
try:
    UPLOAD_TTL_SECONDS = max(60, int(os.environ.get("UPLOAD_TTL_SECONDS", OUTPUT_TTL_SECONDS)))
except (ValueError, TypeError):
    UPLOAD_TTL_SECONDS = OUTPUT_TTL_SECONDS

# How often the cleanup loop wakes up (seconds).
_CLEANUP_INTERVAL = 300  # 5 minutes

# Statuses treated as "job still in progress / waiting for user action"
_PENDING_STATUSES = {
    "queued", "transcribing", "awaiting_edit", "building subtitles",
    "enhancing audio", "rendering video",
    "uploading to Auphonic", "processing audio", "downloading enhanced audio",
}

# ---- Job tracking (in-memory; fine for single-user Replit) ----
jobs = {}  # job_id -> {status, progress, output, error, words, completed_at}

# ---- SQLite persistence ----
DB_PATH = BASE_DIR / "jobs.db"
_db_lock = threading.Lock()


def _db_init() -> None:
    """Create the jobs table if it does not yet exist."""
    with _db_lock:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id       TEXT PRIMARY KEY,
                    status       TEXT,
                    progress     INTEGER,
                    output       TEXT,
                    error        TEXT,
                    words        TEXT,
                    style        TEXT,
                    audio        TEXT,
                    emoji_rules  TEXT,
                    created_at   REAL,
                    completed_at REAL
                )
            """)
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            for col in ("style", "audio", "emoji_rules", "audio_cache_key", "burn_cache_key", "filename"):
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
            conn.commit()


def _db_save_job(job_id: str) -> None:
    """Upsert the current in-memory state of *job_id* into SQLite."""
    job = jobs.get(job_id)
    if job is None:
        return
    words_json = json.dumps(job.get("words")) if job.get("words") is not None else None
    style_json = json.dumps(job.get("style")) if job.get("style") is not None else None
    audio_json = json.dumps(job.get("audio")) if job.get("audio") is not None else None
    emoji_rules_json = json.dumps(job.get("emoji_rules")) if job.get("emoji_rules") is not None else None
    with _db_lock:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO jobs
                    (job_id, status, progress, output, error, words, style, audio, emoji_rules, created_at, completed_at, audio_cache_key, burn_cache_key, filename)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job.get("status"),
                    job.get("progress"),
                    job.get("output"),
                    job.get("error"),
                    words_json,
                    style_json,
                    audio_json,
                    emoji_rules_json,
                    job.get("created_at"),
                    job.get("completed_at"),
                    job.get("audio_cache_key"),
                    job.get("burn_cache_key"),
                    job.get("filename"),
                ),
            )
            conn.commit()


def _db_delete_job(job_id: str) -> None:
    """Remove a job record from SQLite."""
    with _db_lock:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            conn.commit()


def _load_jobs_from_db() -> None:
    """Populate the in-memory jobs dict from persisted SQLite records."""
    if not DB_PATH.exists():
        return
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM jobs").fetchall()
    for row in rows:
        def _load_json_col(name: str):
            if row[name]:
                try:
                    return json.loads(row[name])
                except (json.JSONDecodeError, TypeError):
                    return None
            return None

        words = _load_json_col("words")
        style = _load_json_col("style")
        audio = _load_json_col("audio")
        emoji_rules = _load_json_col("emoji_rules")
        cache_key = None
        burn_key = None
        filename = None
        try:
            cache_key = row["audio_cache_key"]
        except (IndexError, KeyError):
            pass
        try:
            burn_key = row["burn_cache_key"]
        except (IndexError, KeyError):
            pass
        try:
            filename = row["filename"]
        except (IndexError, KeyError):
            pass
        jobs[row["job_id"]] = {
            "status": row["status"],
            "progress": row["progress"],
            "output": row["output"],
            "error": row["error"],
            "words": words,
            "style": style,
            "audio": audio,
            "emoji_rules": emoji_rules,
            "audio_cache_key": cache_key,
            "burn_cache_key": burn_key,
            "filename": filename,
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }


# Initialise DB and reload persisted jobs before the cleanup thread starts.
_db_init()
_load_jobs_from_db()


def allowed_file(name: str) -> bool:
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED_EXT


# ---- File / job cleanup helpers ----

def _safe_unlink(path: Path) -> None:
    """Delete a file if it exists, ignoring errors."""
    try:
        if path and path.exists():
            path.unlink()
    except OSError:
        pass


def _cleanup_temp_files(video_path: Path, ass_path: Path | None, audio_path: Path | None) -> None:
    """Remove all temporary files produced during a job once it has finished."""
    _safe_unlink(video_path)
    _safe_unlink(ass_path)
    _safe_unlink(audio_path)


def _cleanup_loop() -> None:
    """Background thread: delete old output MP4s and prune the jobs dict."""
    while True:
        time.sleep(_CLEANUP_INTERVAL)
        now = time.time()
        output_cutoff = now - OUTPUT_TTL_SECONDS
        upload_cutoff = now - UPLOAD_TTL_SECONDS

        # 1. Remove expired output files.
        for mp4 in list(OUTPUT_DIR.glob("*.mp4")):
            try:
                if mp4.stat().st_mtime < output_cutoff:
                    mp4.unlink()
            except OSError:
                pass

        # 2. Prune completed/errored job entries past the output TTL.
        stale_done = [
            jid for jid, job in list(jobs.items())
            if job.get("status") in ("done", "error")
            and job.get("completed_at", 0) < output_cutoff
        ]
        for jid in stale_done:
            jobs.pop(jid, None)
            _db_delete_job(jid)

        # 3. Prune abandoned jobs (user uploaded but never triggered render)
        #    that are older than the upload TTL.
        stale_pending = [
            jid for jid, job in list(jobs.items())
            if job.get("status") in _PENDING_STATUSES
            and job.get("created_at", 0) < upload_cutoff
        ]
        for jid in stale_pending:
            # Best-effort: delete any upload file that may still be on disk.
            video_path = find_video_path(jid)
            if video_path:
                _safe_unlink(video_path)
            # Also clean up any leftover .ass or _audio.aac for this job.
            _safe_unlink(UPLOAD_DIR / f"{jid}.ass")
            _safe_unlink(UPLOAD_DIR / f"{jid}_audio.aac")
            jobs.pop(jid, None)
            _db_delete_job(jid)

        # 4. Remove orphaned upload files (no matching job entry — e.g. after
        #    a server restart) that are older than the upload TTL.
        # We extract the leading job_id prefix (32-char hex) from each filename
        # so any file like {job}.mp4, {job}.ass, {job}_audio.aac,
        # {job}_audiocache.aac, etc. is correctly tied to its parent job.
        active_ids = set(jobs.keys())
        _job_id_re = re.compile(r"^([a-f0-9]{32})")
        for f in list(UPLOAD_DIR.glob("*")):
            if not f.is_file():
                continue
            try:
                if f.stat().st_mtime < upload_cutoff:
                    stem = f.stem
                    if stem.startswith("prev_"):
                        _safe_unlink(f)
                        continue
                    m = _job_id_re.match(stem)
                    if m and m.group(1) in active_ids:
                        # Belongs to an active job — keep it.
                        continue
                    _safe_unlink(f)
            except OSError:
                pass


# Start the cleanup thread once at import time.
_cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
_cleanup_thread.start()


def find_video_path(job_id: str) -> Path | None:
    """Find the uploaded video file for a job by trying all allowed extensions."""
    for ext in ALLOWED_EXT:
        candidate = UPLOAD_DIR / f"{job_id}.{ext}"
        if candidate.exists():
            return candidate
    return None


# ---- Whisper transcription with word-level timestamps ----
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")

_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper_model():
    """Lazy-init a single shared WhisperModel. Re-used across all transcriptions."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                _whisper_model = WhisperModel(
                    WHISPER_MODEL_NAME,
                    device=WHISPER_DEVICE,
                    compute_type=WHISPER_COMPUTE_TYPE,
                )
    return _whisper_model


# Warm up the Whisper model in the background so the first transcription
# doesn't pay the model-load cost.
threading.Thread(target=_get_whisper_model, daemon=True).start()


def transcribe(video_path: Path, pre_clean: bool = False):
    """Return a list of word dicts: [{'word': str, 'start': float, 'end': float}, ...]

    When *pre_clean* is True, run a fast local FFmpeg cleanup pass before
    Whisper. The filters chosen (afftdn for spectral noise gate, dynaudnorm
    for soft-voice boost) don't shift transients, so word-level timestamps
    stay aligned with the original video.
    """
    target = video_path
    cleaned: Path | None = None
    if pre_clean:
        cleaned = video_path.with_name(f".{video_path.stem}.preclean.wav")
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path),
             "-vn", "-ac", "1", "-ar", "16000",
             "-af", "afftdn=nf=-25,dynaudnorm=p=0.95:m=12:s=12",
             "-c:a", "pcm_s16le", str(cleaned)],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            target = cleaned
        else:
            # Pre-clean failure isn't fatal — fall back to the raw video.
            cleaned = None

    try:
        model = _get_whisper_model()
        segments, _info = model.transcribe(
            str(target),
            word_timestamps=True,
            vad_filter=True,
            beam_size=int(os.environ.get("WHISPER_BEAM_SIZE", "1")),
        )

        words = []
        for seg in segments:
            if not seg.words:
                continue
            for w in seg.words:
                text = w.word.strip()
                if not text:
                    continue
                words.append({"word": text, "start": float(w.start), "end": float(w.end)})
        return words
    finally:
        if cleaned:
            _safe_unlink(cleaned)


# ---- ASS subtitle generation ----
def hex_to_ass_color(hex_color: str) -> str:
    """Convert #RRGGBB to ASS &HBBGGRR& format (ASS uses BGR, not RGB)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def ass_timestamp(seconds: float) -> str:
    """Convert seconds -> H:MM:SS.cs (centiseconds) for ASS."""
    if seconds < 0:
        seconds = 0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:06.3f}"[:-1]  # drop last digit to get cs


def group_words(words, group_size=3):
    """Chunk the flat word list into groups of N."""
    return [words[i : i + group_size] for i in range(0, len(words), group_size)]


def _visible_len(ass_text: str) -> int:
    """Character count of ASS text, ignoring inline override tags like {\\pos(...)}."""
    return len(re.sub(r"\{[^}]*\}", "", ass_text))


def _clamp_line_width(line: str, font_size: int, video_w: int,
                      char_factor: float = 0.76, safe_pct: float = 0.84) -> str:
    """Return *line* with a \\fscx override that squishes it to fit the safe area.

    Uses an empirical pixel-width estimate (char_factor × font_size per visible
    character).  0.76 is conservative enough to cover wide fonts like Montserrat
    Black in all-caps mode.  The scale is floored at 40 % to keep text legible.
    """
    raw_len = _visible_len(line)
    if raw_len == 0:
        return line
    est_px = raw_len * font_size * char_factor
    max_px = video_w * safe_pct
    if est_px <= max_px:
        return line
    scale = max(40, int((max_px / est_px) * 100))
    return f"{{\\fscx{scale}}}{line}"


def _wrap_ass_text(text: str, max_chars: int) -> str:
    """Split ASS-tagged subtitle text at word boundaries to fit max_chars per line.

    Inline ASS tags (e.g. {\\cXXX}) are counted as zero-width so they don't
    trigger early wrapping.  Lines are joined with \\N (hard newline in ASS).
    """
    if _visible_len(text) <= max_chars:
        return text
    parts = text.split(" ")
    lines: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for part in parts:
        plen = _visible_len(part)
        gap = 1 if current else 0
        if current and current_len + gap + plen > max_chars:
            lines.append(current)
            current = [part]
            current_len = plen
        else:
            current.append(part)
            current_len += gap + plen
    if current:
        lines.append(current)
    return r"\N".join(" ".join(ln) for ln in lines)


def build_ass(words, style: dict, video_w: int, video_h: int, emoji_rules: dict = None) -> str:
    font = style.get("font_name", "Montserrat Thin Black")
    font_size = int(style.get("font_size", 72))
    primary = hex_to_ass_color(style.get("primary_color", "#FFFFFF"))
    highlight = hex_to_ass_color(style.get("highlight_color", "#FFD60A"))
    outline = hex_to_ass_color(style.get("outline_color", "#000000"))
    outline_w = int(style.get("outline_width", 3))
    shadow = int(style.get("shadow", 1))
    pos_y_pct = float(style.get("position_y", 85))
    all_caps = bool(style.get("all_caps", True))
    group_size = int(style.get("group_size", 3))

    pos_x = video_w // 2
    pos_y = int(video_h * (pos_y_pct / 100.0))

    # Two char-factor estimates, tuned for different jobs:
    #   - WRAP factor (0.62): optimistic — lets more words land on one line.
    #     Used to decide where to insert \N line breaks.
    #   - CLAMP factor (0.88): conservative — matches Montserrat Black in
    #     all-caps. Used by _clamp_line_width as a safety net to apply \fscx
    #     squish when a wrapped line still overflows the safe area.
    # Safe area is 84% of video width (8% margins each side).
    _wrap_char_factor = 0.62
    _clamp_char_factor = 0.88
    _safe_pct = 0.84
    max_chars_per_line = max(6, int((video_w * _safe_pct) / (font_size * _wrap_char_factor)))

    # Normalise emoji rule keys to lowercase alpha-only for robust matching
    normalised_emoji: dict[str, str] = {}
    if emoji_rules:
        for k, v in emoji_rules.items():
            clean_key = re.sub(r"[^a-z]", "", k.lower().strip())
            if clean_key:
                normalised_emoji[clean_key] = v

    header = f"""[Script Info]
Title: Generated Captions
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{font_size},{primary},{primary},{outline},&H00000000,0,0,0,0,100,100,0,0,1,{outline_w},{shadow},2,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def fmt(text: str) -> str:
        return text.upper() if all_caps else text

    lines = []
    groups = group_words(words, group_size=group_size)

    for group in groups:
        if not group:
            continue

        # Check for an emoji match within this group (first match wins)
        group_emoji = ""
        if normalised_emoji:
            for w in group:
                word_key = re.sub(r"[^a-z]", "", w["word"].lower())
                if word_key and word_key in normalised_emoji:
                    group_emoji = normalised_emoji[word_key]
                    break

        for idx, active in enumerate(group):
            pieces = []
            for j, w in enumerate(group):
                word_text = fmt(w["word"])
                word_text = word_text.replace("{", "").replace("}", "")
                if j == idx:
                    pieces.append(f"{{\\c{highlight}}}{word_text}{{\\c{primary}}}")
                else:
                    pieces.append(word_text)

            text = " ".join(pieces)

            # Append the group emoji to every dialogue line so it's visible
            # for the full group duration, not just one word's moment.
            if group_emoji:
                text = text + " " + group_emoji

            # Wrap long lines, then clamp any remaining overflow with \fscx
            text = _wrap_ass_text(text, max_chars_per_line)
            text = r"\N".join(
                _clamp_line_width(ln, font_size, video_w, _clamp_char_factor, _safe_pct)
                for ln in text.split(r"\N")
            )

            start_ts = ass_timestamp(active["start"])
            end_ts = ass_timestamp(active["end"])

            line = (
                f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,"
                f"{{\\pos({pos_x},{pos_y})}}{text}"
            )
            lines.append(line)

    return header + "\n".join(lines) + "\n"


def get_video_dimensions(video_path: Path):
    """Return (width, height) in *display* orientation.

    iPhone .mov files (and some Android recordings) store the frame in
    landscape but carry a rotation flag (±90°) so players present it as
    portrait. FFmpeg's default `autorotate` applies that flip during decode,
    so the subtitle filter sees the rotated frame. We must therefore feed the
    ASS layout the rotated dimensions, otherwise libass scales fonts by the
    wrong factor and our line-wrap math is computed against the wrong width.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
        "-of", "json",
        str(video_path),
    ]
    out = subprocess.check_output(cmd).decode()
    data = json.loads(out)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"No video stream in {video_path}")
    s = streams[0]
    w = int(s.get("width") or 0)
    h = int(s.get("height") or 0)

    rotation = 0
    tags = s.get("tags") or {}
    if tags.get("rotate"):
        try:
            rotation = int(tags["rotate"])
        except (TypeError, ValueError):
            pass
    for sd in s.get("side_data_list") or []:
        # Modern ffmpeg emits rotation via the Display Matrix side data.
        if sd.get("rotation") is not None:
            try:
                rotation = int(sd["rotation"])
            except (TypeError, ValueError):
                pass
            break

    if abs(rotation) % 180 == 90:
        w, h = h, w
    return w, h


def build_audio_filter_chain(audio: dict) -> str | None:
    """Build an FFmpeg audio filter string from the enhancement options.

    Returns None when no enhancements are requested.
    Supported keys (all bool):
      noise_reduction  – afftdn spectral noise gate
      voice_boost      – dynaudnorm: amplifies soft speech without crushing loud parts
      loudness_norm    – EBU R128 loudness normalisation (-14 LUFS, Instagram target)
      voice_clarity    – gentle presence EQ + de-esser for smooth, professional vocals
    """
    filters = []
    if audio.get("noise_reduction"):
        filters.append("afftdn=nf=-25")
    if audio.get("voice_boost"):
        # Dynamic audio normalisation: brings soft passages up to a consistent
        # level. p=0.95 target peak, m=15 max gain (dB), s=12 smoothing slope.
        # Runs before loudnorm so the integrated loudness target is hit cleanly.
        filters.append("dynaudnorm=p=0.95:m=15:s=12")
    if audio.get("voice_clarity"):
        # Gentle 2 kHz presence boost (wide Q, low gain) + 8 kHz de-esser cut
        # to keep vocals clear without harshness or sibilance
        filters.append("equalizer=f=2000:width_type=o:width=4:g=2")
        filters.append("equalizer=f=8000:width_type=o:width=2:g=-2")
    if audio.get("loudness_norm"):
        # -14 LUFS is the Instagram / social-media loudness target
        filters.append("loudnorm=I=-14:TP=-1:LRA=7")
    if filters:
        # Final brick-wall limiter prevents the stacked filters from clipping
        # peaks above 0 dBFS. -0.3 dBTP headroom keeps lossy encoders safe.
        filters.append("alimiter=limit=0.97:level=disabled")
    return ",".join(filters) if filters else None


def apply_audio_enhancements(video_path: Path, output_path: Path, af: str, duration: int | None = None):
    """Run FFmpeg with the given audio filter chain, outputting only the audio stream.

    Args:
        video_path: Source video file.
        output_path: Destination audio file (AAC).
        af: FFmpeg audio filter string built by build_audio_filter_chain().
        duration: If set, limit output to this many seconds (useful for previews).
    """
    cmd = ["ffmpeg", "-y", "-i", str(video_path)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += [
        "-vn",
        "-af", af,
        "-c:a", "aac",
        "-b:a", "192k",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg audio enhancement failed: {proc.stderr[-2000:]}")


def _auphonic_auth_headers() -> dict:
    """Build HTTP auth headers from AUPHONIC_API_KEY.

    If the value contains a colon it is treated as "user:password" (Basic Auth),
    otherwise it is used as a Bearer token.
    """
    key = os.environ.get("AUPHONIC_API_KEY", "")
    if ":" in key:
        token = base64.b64encode(key.encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {"Authorization": f"Bearer {key}"}


def enhance_with_auphonic(video_path: Path, output_path: Path, settings: dict, status_callback=None) -> None:
    """Upload video to Auphonic, apply AI audio enhancement, download the result.

    settings keys (all optional):
      speech_isolation   (bool)
      adaptive_leveler   (bool)
      noise_hum_reduction (bool)
      highpass           (bool)
      loudness_lufs      (int | None) — e.g. -16, -14, -23, or None for off
    """
    import requests

    headers = _auphonic_auth_headers()
    base_url = "https://auphonic.com/api"

    production_data: dict = {
        "output_files": [{"format": "aac"}],
        "speech_isolation": bool(settings.get("speech_isolation", False)),
        "adaptive_leveler": bool(settings.get("adaptive_leveler", True)),
        "noise_hum_reduction": bool(settings.get("noise_hum_reduction", False)),
    }

    if settings.get("highpass"):
        production_data["filtering"] = {"highpass": True}

    loudness = settings.get("loudness_lufs")
    if loudness is not None:
        production_data["loudness_target"] = int(loudness)

    if status_callback:
        status_callback("uploading to Auphonic")

    # Step 1: Create the production with JSON settings (no file yet).
    # Auphonic now requires application/json on /productions.json.
    json_headers = {**headers, "Content-Type": "application/json"}
    resp = requests.post(
        f"{base_url}/productions.json",
        headers=json_headers,
        data=json.dumps(production_data),
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Auphonic create production failed ({resp.status_code}): {resp.text[:500]}"
        )

    prod_uuid = resp.json()["data"]["uuid"]

    # Step 2: Upload the input file as multipart to the production's upload
    # endpoint. This endpoint stays multipart since it carries a file body.
    with open(video_path, "rb") as fh:
        up = requests.post(
            f"{base_url}/production/{prod_uuid}/upload.json",
            headers=headers,
            files={"input_file": (video_path.name, fh)},
            timeout=1800,
        )
    if up.status_code not in (200, 201):
        raise RuntimeError(
            f"Auphonic upload failed ({up.status_code}): {up.text[:500]}"
        )

    # Step 3: Kick off processing.
    resp = requests.post(
        f"{base_url}/production/{prod_uuid}/start.json",
        headers=headers,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Auphonic start production failed ({resp.status_code}): {resp.text[:500]}"
        )

    if status_callback:
        status_callback("processing audio")

    _MAX_POLL_SECONDS = 1800  # 30-minute hard deadline
    _poll_start = time.time()

    while True:
        time.sleep(3)
        if time.time() - _poll_start > _MAX_POLL_SECONDS:
            raise RuntimeError(
                "Auphonic processing timed out after 30 minutes. "
                "Check your Auphonic dashboard for production status."
            )
        resp = requests.get(
            f"{base_url}/production/{prod_uuid}.json",
            headers=headers,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Auphonic poll failed ({resp.status_code}): {resp.text[:300]}"
            )

        data = resp.json()["data"]
        status_string = data.get("status_string", "")

        if status_string == "Done":
            break
        if status_string in ("Error", "Encoding Failed"):
            msg = data.get("error_message") or status_string
            raise RuntimeError(f"Auphonic processing failed: {msg}")

    if status_callback:
        status_callback("downloading enhanced audio")

    output_files = data.get("output_files", [])
    audio_url = None
    for out in output_files:
        fmt = out.get("format", "")
        name = out.get("filename", "")
        if fmt == "aac" or name.endswith(".m4a") or name.endswith(".aac"):
            audio_url = out.get("download_url")
            break
    if not audio_url and output_files:
        audio_url = output_files[0].get("download_url")

    if not audio_url:
        raise RuntimeError("Auphonic: no output file URL found in production result")

    resp = requests.get(audio_url, headers=headers, timeout=600, stream=True)
    if resp.status_code != 200:
        raise RuntimeError(f"Auphonic download failed ({resp.status_code})")

    with open(output_path, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=65536):
            fh.write(chunk)

    # Auphonic free-tier productions prepend AND append a branded jingle
    # (~6.4s on each side, including silence/transition).
    #
    # The Auphonic output structure is:
    #   [ jingle (brand_trim s) | cleaned source audio (src_dur s) | jingle (brand_trim s) ]
    #
    # We rebuild the audio in a single ffmpeg pass:
    #   [ source[0:brand_trim] | auphonic[2*brand_trim : brand_trim+src_dur] | source[src_dur-brand_trim:src_dur] ]
    #
    # i.e. original audio at the boundaries, cleaned content in the middle.
    # Each branch is explicitly resampled to a common format (stereo / 44.1k /
    # fltp) BEFORE concat — without this, mismatched source formats (e.g. HE-AAC
    # mono 48 kbps phone audio) cause the concat filter to silently drop
    # branches or substitute silence. That was the real bug.
    #
    # Env knobs:
    #   AUPHONIC_BRAND_TRIM_SECONDS=0  -> disable everything (paid tier, no jingle)
    #   AUPHONIC_SPLICE_BOUNDARIES=0   -> use cleaned audio at boundaries instead of source
    try:
        brand_trim = float(os.environ.get("AUPHONIC_BRAND_TRIM_SECONDS", "6.409"))
    except (TypeError, ValueError):
        brand_trim = 6.409
    splice_boundaries = os.environ.get("AUPHONIC_SPLICE_BOUNDARIES", "1") not in ("0", "false", "False")

    out_dur = _ffprobe_duration(output_path)
    src_dur = _ffprobe_duration(video_path)

    if brand_trim > 0 and out_dur > 2 * brand_trim + 0.5 and src_dur > 2 * brand_trim + 0.5:
        rebuilt = output_path.with_suffix(".rebuilt.aac")
        afmt = "aformat=sample_rates=44100:channel_layouts=stereo:sample_fmts=fltp"

        if splice_boundaries:
            head_end = brand_trim
            tail_start = src_dur - brand_trim
            cleaned_mid_start = 2 * brand_trim                 # auphonic timeline
            cleaned_mid_end = brand_trim + tail_start          # = brand_trim + src_dur - brand_trim
            filter_complex = (
                f"[0:a]atrim=0:{head_end:.3f},asetpts=PTS-STARTPTS,{afmt}[head];"
                f"[1:a]atrim={cleaned_mid_start:.3f}:{cleaned_mid_end:.3f},asetpts=PTS-STARTPTS,{afmt}[mid];"
                f"[0:a]atrim={tail_start:.3f}:{src_dur:.3f},asetpts=PTS-STARTPTS,{afmt}[tail];"
                f"[head][mid][tail]concat=n=3:v=0:a=1[out]"
            )
        else:
            # No splice: just trim the jingles cleanly off the Auphonic output.
            cleaned_start = brand_trim
            cleaned_end = brand_trim + src_dur
            filter_complex = (
                f"[1:a]atrim={cleaned_start:.3f}:{cleaned_end:.3f},asetpts=PTS-STARTPTS,{afmt}[out]"
            )

        proc = subprocess.run(
            ["ffmpeg", "-y",
             "-i", str(video_path),
             "-i", str(output_path),
             "-filter_complex", filter_complex,
             "-map", "[out]",
             # Hard cap output length to the source video duration. Belt-and-
             # suspenders against any concat/atrim edge case that could append
             # extra audio past the video end.
             "-t", f"{src_dur:.3f}",
             "-c:a", "aac", "-b:a", "192k",
             str(rebuilt)],
            capture_output=True, text=True,
        )

        if proc.returncode == 0 and rebuilt.exists() and rebuilt.stat().st_size > 0:
            output_path.unlink()
            rebuilt.rename(output_path)
        else:
            _safe_unlink(rebuilt)
            # Surface the failure so we don't silently ship the un-trimmed jingled audio.
            stderr_tail = proc.stderr[-1500:] if proc.stderr else ""
            raise RuntimeError(
                f"Auphonic post-process (trim+splice) failed: {stderr_tail}"
            )


def _ffprobe_duration(path: Path) -> float:
    """Return media duration in seconds, or 0.0 if ffprobe fails."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            timeout=15,
        ).decode().strip()
        return float(out)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


def enhance_with_dolby(video_path: Path, output_path: Path, settings: dict, status_callback=None) -> None:
    """Run Dolby.io Media Enhance on the audio of *video_path*.

    settings keys (all optional, with sensible defaults):
      content_type         (str)  "social_media" | "podcast" | "voice_over" |
                                  "mobile_phone" | "interview"
      speech_isolation     (int)  0–100; 0 disables, higher = more aggressive.
      noise_reduction      (str)  "off" | "low" | "medium" | "high" | "max"
      dynamics             (str)  "off" | "low" | "medium" | "high"
      loudness_target_lufs (int)  e.g. -14, -16, -23. None to skip.
    """
    import requests

    api_key = os.environ.get("DOLBY_API_KEY", "")
    if not api_key:
        raise RuntimeError("Dolby is not configured (DOLBY_API_KEY not set).")

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    base = "https://api.dolby.com/media"

    if status_callback:
        status_callback("preparing audio for Dolby")

    # 1. Extract audio. Dolby accepts wav/mp3/aac/flac etc.; wav is simplest.
    wav_path = output_path.with_suffix(".wav")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path),
         "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(wav_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {proc.stderr[-1000:]}")

    # Per-job dlb:// URLs so concurrent jobs don't collide.
    dlb_in = f"dlb://in/{output_path.stem}.wav"
    dlb_out = f"dlb://out/{output_path.stem}.wav"

    try:
        # 2. Request a pre-signed upload URL, then PUT the WAV.
        if status_callback:
            status_callback("uploading to Dolby")
        resp = requests.post(
            f"{base}/input",
            headers=headers,
            json={"url": dlb_in},
            timeout=60,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Dolby /input failed ({resp.status_code}): {resp.text[:400]}")
        put_url = resp.json()["url"]

        with open(wav_path, "rb") as fh:
            up = requests.put(put_url, data=fh, timeout=1800)
        if up.status_code not in (200, 201):
            raise RuntimeError(f"Dolby upload PUT failed ({up.status_code}): {up.text[:400]}")

        # 3. Build the enhance request body from settings.
        content_type = settings.get("content_type", "social_media")
        body: dict = {
            "input": dlb_in,
            "output": dlb_out,
            "content": {"type": content_type},
            "audio": {},
        }

        nr = settings.get("noise_reduction", "medium")
        if nr and nr != "off":
            body["audio"]["noise"] = {"reduction": {"amount": nr}}

        si = int(settings.get("speech_isolation", 0) or 0)
        if si > 0:
            body["audio"].setdefault("speech", {})
            body["audio"]["speech"]["isolation"] = {"amount": si}

        dyn = settings.get("dynamics", "low")
        if dyn and dyn != "off":
            body["audio"]["dynamics"] = {"range_control": {"amount": dyn}}

        lufs = settings.get("loudness_target_lufs")
        if lufs is not None:
            body["audio"]["loudness"] = {"target_level": int(lufs)}

        # 4. Submit the enhance job.
        if status_callback:
            status_callback("processing with Dolby")
        resp = requests.post(f"{base}/enhance", headers=headers, json=body, timeout=60)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Dolby /enhance failed ({resp.status_code}): {resp.text[:400]}")
        job_id = resp.json()["job_id"]

        # 5. Poll until terminal.
        deadline = time.time() + 1800
        while True:
            if time.time() > deadline:
                raise RuntimeError("Dolby processing timed out after 30 minutes.")
            time.sleep(3)
            r = requests.get(
                f"{base}/enhance",
                headers={"x-api-key": api_key},
                params={"job_id": job_id},
                timeout=30,
            )
            if r.status_code != 200:
                raise RuntimeError(f"Dolby poll failed ({r.status_code}): {r.text[:300]}")
            data = r.json()
            status_str = data.get("status", "")
            if status_str == "Success":
                break
            if status_str in ("Failed", "InternalError"):
                err = data.get("error", {}).get("message") or status_str
                raise RuntimeError(f"Dolby job failed: {err}")

        # 6. Download the cleaned audio.
        if status_callback:
            status_callback("downloading enhanced audio")
        cleaned = output_path.with_suffix(".cleaned.wav")
        with requests.get(
            f"{base}/output",
            headers={"x-api-key": api_key},
            params={"url": dlb_out},
            stream=True,
            timeout=1800,
            allow_redirects=True,
        ) as r:
            if r.status_code != 200:
                raise RuntimeError(f"Dolby download failed ({r.status_code})")
            with open(cleaned, "wb") as out:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        out.write(chunk)

        # 7. Re-encode to AAC + brick-wall limiter so the burn step can copy.
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(cleaned),
             "-af", "alimiter=limit=0.97:level=disabled",
             "-c:a", "aac", "-b:a", "192k", str(output_path)],
            capture_output=True, text=True,
        )
        _safe_unlink(cleaned)
        if proc.returncode != 0:
            raise RuntimeError(f"AAC re-encode failed: {proc.stderr[-1000:]}")
    finally:
        _safe_unlink(wav_path)


def _detect_video_encoder() -> tuple[list[str], str]:
    """Pick the fastest H.264 encoder available.

    Returns (encoder_cli_args, encoder_name). On macOS we prefer Apple's
    hardware encoder (h264_videotoolbox), falling back to libx264 elsewhere
    or if videotoolbox isn't built into this ffmpeg.
    """
    forced = os.environ.get("VIDEO_ENCODER")
    if forced == "libx264":
        return (["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"], "libx264")
    if forced == "h264_videotoolbox":
        return (["-c:v", "h264_videotoolbox", "-b:v", "5M"], "h264_videotoolbox")

    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        if "h264_videotoolbox" in out:
            return (["-c:v", "h264_videotoolbox", "-b:v", "5M"], "h264_videotoolbox")
    except (OSError, subprocess.SubprocessError):
        pass
    return (["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"], "libx264")


_VIDEO_ENC_ARGS, VIDEO_ENC_NAME = _detect_video_encoder()


def enhance_with_elevenlabs(video_path: Path, output_path: Path, settings: dict, status_callback=None) -> None:
    """Run ElevenLabs Voice Isolator on the audio of *video_path*.

    Produces *output_path* as the RAW isolated voice (re-encoded to AAC).
    No blending, post-filters, or gain are applied here — those run later
    in _apply_isolation_postprocess so the user can iterate on them without
    re-billing the ElevenLabs API.
    """
    import requests

    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise RuntimeError("ElevenLabs is not configured (ELEVENLABS_API_KEY not set).")

    if status_callback:
        status_callback("preparing audio")

    # 1. Extract audio as WAV (mono / 44.1 kHz is plenty for voice isolation).
    wav_path = output_path.with_suffix(".wav")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path),
         "-vn", "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(wav_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {proc.stderr[-1000:]}")

    if status_callback:
        status_callback("isolating voice with ElevenLabs")

    # 2. POST to the ElevenLabs Audio Isolation API. Body is mp3 by default.
    cleaned_path = output_path.with_suffix(".cleaned.mp3")
    try:
        with open(wav_path, "rb") as fh:
            resp = requests.post(
                "https://api.elevenlabs.io/v1/audio-isolation",
                headers={"xi-api-key": api_key, "Accept": "audio/mpeg"},
                files={"audio": (wav_path.name, fh, "audio/wav")},
                timeout=1800,
                stream=True,
            )
            if resp.status_code != 200:
                err = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
                raise RuntimeError(f"ElevenLabs isolation failed ({resp.status_code}): {err}")
            with open(cleaned_path, "wb") as out:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        out.write(chunk)

        if status_callback:
            status_callback("encoding cleaned audio")

        # 3. Re-encode the cleaned mp3 to AAC. This is the cacheable raw output.
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(cleaned_path),
             "-c:a", "aac", "-b:a", "192k", str(output_path)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"AAC re-encode failed: {proc.stderr[-1500:]}")
    finally:
        _safe_unlink(wav_path)
        _safe_unlink(cleaned_path)


_POSTPROCESS_KEYS = {"offset_seconds", "wet_mix", "output_gain_db", "post_filters"}


def _audio_cache_key(audio: dict) -> str:
    """Stable hash of the audio settings that REQUIRE re-running the AI provider.

    Local post-process knobs (wet/dry blend, output gain, post-filters, sync
    offset) are excluded so the user can iterate on them without burning
    credits — they get re-applied on every render via _apply_isolation_postprocess.
    """
    if not audio or not audio.get("provider"):
        return ""
    payload = {k: v for k, v in audio.items() if k not in _POSTPROCESS_KEYS}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _apply_isolation_postprocess(
    enhanced_path: Path,
    video_path: Path,
    settings: dict,
) -> None:
    """Apply local FFmpeg post-process to *enhanced_path* in-place.

    Reads the AI-cleaned audio at *enhanced_path*, optionally blends it with
    the source video's original audio (wet/dry), runs any post-filters,
    applies output gain, and finishes with a brick-wall limiter so the chain
    can't clip. Result is written back to *enhanced_path*.

    Settings keys (all optional):
      wet_mix         (int 0-100, default 100 = pure cleaned audio)
      output_gain_db  (float dB, default 0)
      post_filters    (dict — same shape as build_audio_filter_chain input)
    """
    wet_mix = max(0, min(100, int(settings.get("wet_mix", 100))))
    try:
        gain_db = float(settings.get("output_gain_db", 0) or 0)
    except (TypeError, ValueError):
        gain_db = 0.0
    post_filters = settings.get("post_filters") or {}

    pf_chain = build_audio_filter_chain(post_filters)
    has_blend = wet_mix < 100
    has_gain = abs(gain_db) > 0.01
    if not has_blend and not pf_chain and not has_gain:
        # Nothing to do besides a final limiter — and even that's only useful
        # if there's any chance of clipping. Skip the re-encode entirely.
        return

    extras: list[str] = []
    if pf_chain:
        # build_audio_filter_chain already adds its own limiter, so don't
        # double it. Strip the trailing alimiter the helper appended.
        if pf_chain.endswith(",alimiter=limit=0.97:level=disabled"):
            pf_chain = pf_chain.rsplit(",", 1)[0]
        extras.append(pf_chain)
    if has_gain:
        extras.append(f"volume={gain_db}dB")
    extras.append("alimiter=limit=0.97:level=disabled")

    out_tmp = enhanced_path.with_suffix(".post.aac")
    cmd: list[str] = ["ffmpeg", "-y"]

    if has_blend:
        cleaned_w = wet_mix / 100.0
        original_w = 1.0 - cleaned_w
        cmd += ["-i", str(enhanced_path), "-i", str(video_path)]
        chain = [
            f"[0:a]volume={cleaned_w}[c]",
            f"[1:a]volume={original_w}[o]",
            "[c][o]amix=inputs=2:normalize=0[mixed]",
            f"[mixed]{','.join(extras)}[out]",
        ]
        cmd += ["-filter_complex", ";".join(chain), "-map", "[out]"]
    else:
        cmd += ["-i", str(enhanced_path), "-af", ",".join(extras)]

    cmd += ["-c:a", "aac", "-b:a", "192k", str(out_tmp)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0 and out_tmp.exists() and out_tmp.stat().st_size > 0:
        enhanced_path.unlink()
        out_tmp.rename(enhanced_path)
    else:
        _safe_unlink(out_tmp)
        raise RuntimeError(
            f"Audio post-process failed: {proc.stderr[-1500:] if proc.stderr else 'unknown'}"
        )


def _quality_boost_scale(w: int, h: int) -> tuple[int, int, str | None]:
    """Compute target dimensions and scale filter for Quality Boost.

    Returns (out_w, out_h, scale_filter_or_None). If the source already has
    a short edge >= 1080 px, no upscale is needed and we return the input
    dims with a None scale_filter.

    The output dims match what FFmpeg's `scale=…:-2` actually produces —
    important because the ASS file's PlayResX/Y must equal the dimensions
    the subtitles filter sees, otherwise libass mis-scales fonts.
    """
    short_edge = min(w, h)
    if short_edge >= 1080:
        return w, h, None
    if w <= h:
        ratio = 1080.0 / w
        new_w, new_h = 1080, int(round(h * ratio))
        scale_filter = "scale=1080:-2:flags=lanczos"
    else:
        ratio = 1080.0 / h
        new_w, new_h = int(round(w * ratio)), 1080
        scale_filter = "scale=-2:1080:flags=lanczos"
    # FFmpeg's :-2 forces even output; mirror that here.
    if new_h % 2:
        new_h -= 1
    if new_w % 2:
        new_w -= 1
    return new_w, new_h, scale_filter


def burn_subtitles(
    video_path: Path,
    ass_path: Path,
    output_path: Path,
    audio_path: Path | None = None,
    quality_boost: bool = False,
    silent: bool = False,
):
    """Burn the ASS file into the video using FFmpeg.

    If *silent* is True, the output has no audio track at all (-an). This is
    used by the burn cache: the silent video can be cached and remuxed with
    different audio later without re-encoding the (slow) video.
    Otherwise: if *audio_path* is supplied, it replaces the audio stream;
    otherwise the source video's audio is copied through.
    If *quality_boost* is True, the source is upscaled (lanczos) so the short
    edge is at least 1080 px, with a light unsharp pass.
    """
    fonts_arg = str(FONT_DIR).replace("\\", "/").replace(":", r"\:")
    ass_arg = str(ass_path).replace("\\", "/").replace(":", r"\:")

    vf_parts: list[str] = []
    if quality_boost:
        try:
            src_w, src_h = get_video_dimensions(video_path)
            _, _, scale_filter = _quality_boost_scale(src_w, src_h)
            if scale_filter:
                vf_parts.append(scale_filter)
        except Exception:
            pass
        # Mild sharpen — luma 5x5 kernel, gentle amount; chroma untouched.
        vf_parts.append("unsharp=5:5:0.6:5:5:0.0")

    # FFmpeg 8.x requires the explicit `filename=` form; the bare positional
    # first-arg syntax was removed and produces "No option name" errors.
    vf_parts.append(f"subtitles=filename={ass_arg}:fontsdir={fonts_arg}")
    vf = ",".join(vf_parts)

    cmd: list[str] = ["ffmpeg", "-y", "-i", str(video_path)]
    if silent:
        cmd += ["-vf", vf, *_VIDEO_ENC_ARGS, "-an"]
    else:
        if audio_path:
            cmd += ["-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0"]
        cmd += ["-vf", vf, *_VIDEO_ENC_ARGS, "-c:a", "copy"]
        if audio_path:
            cmd += ["-shortest"]
    cmd += [str(output_path)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and VIDEO_ENC_NAME == "h264_videotoolbox":
        # Hardware encoder can fail on unusual inputs (e.g. exotic pixel
        # formats). Retry once with libx264 so the user still gets a result.
        fb_cut = cmd.index("-vf") + 2
        fallback = (
            cmd[:fb_cut]
            + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
        )
        if silent:
            fallback += ["-an"]
        else:
            fallback += ["-c:a", "copy"]
            if audio_path:
                fallback += ["-shortest"]
        fallback += [str(output_path)]
        proc = subprocess.run(fallback, capture_output=True, text=True)

    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {proc.stderr[-2000:]}")


def mux_audio_into_video(silent_video: Path, audio_source: Path, output_path: Path) -> None:
    """Combine a pre-burned silent video with audio from *audio_source*.

    Video is stream-copied (no re-encode → fast). Audio is re-encoded to AAC
    so we get a clean stream regardless of the source format (.mov HE-AAC,
    cleaned mp3 from a provider, etc.).
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(silent_video),
        "-i", str(audio_source),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Audio remux failed: {proc.stderr[-2000:]}")


def _burn_cache_key(style: dict, words: list, emoji_rules: dict, video_path: Path) -> str:
    """Stable hash of everything that affects the BURNED VIDEO (subtitle render).

    Audio settings are NOT included — when only audio changes, we keep the
    cached silent video and just remux the new audio.
    """
    payload = {
        "style": style or {},
        "words": words or [],
        "emoji_rules": emoji_rules or {},
        "video": str(video_path),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---- Background workers ----

def transcribe_job(job_id: str, video_path: Path, pre_clean: bool = False):
    """Transcribe only — stores words and sets status to awaiting_edit."""
    try:
        jobs[job_id]["status"] = "transcribing"
        jobs[job_id]["progress"] = 30
        _db_save_job(job_id)
        words = transcribe(video_path, pre_clean=pre_clean)
        if not words:
            raise RuntimeError("No speech detected in the video.")
        jobs[job_id]["words"] = words
        jobs[job_id]["status"] = "awaiting_edit"
        jobs[job_id]["progress"] = 100
        _db_save_job(job_id)
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["completed_at"] = time.time()
        _db_save_job(job_id)
        # No render will follow — clean up the uploaded video now.
        _safe_unlink(video_path)


def render_job(job_id: str, video_path: Path, words: list, style: dict, audio: dict, emoji_rules: dict):
    """Build ASS, optionally enhance audio, then burn subtitles."""
    ass_path: Path | None = None
    enhanced_audio_path: Path | None = None
    try:
        jobs[job_id]["status"] = "building subtitles"
        jobs[job_id]["progress"] = 55
        _db_save_job(job_id)
        w, h = get_video_dimensions(video_path)
        # If Quality Boost is enabled, the burn step will upscale the frame
        # before the subtitles filter runs. Build the ASS layout against the
        # POST-upscale dimensions so libass scales fonts correctly.
        if style.get("quality_boost"):
            w, h, _ = _quality_boost_scale(w, h)
        ass_content = build_ass(words, style, w, h, emoji_rules=emoji_rules)

        ass_path = UPLOAD_DIR / f"{job_id}.ass"
        ass_path.write_text(ass_content, encoding="utf-8")

        provider = audio.get("provider", "ffmpeg") if audio else "ffmpeg"
        cache_path = UPLOAD_DIR / f"{job_id}_audiocache.aac"
        cache_key = _audio_cache_key(audio) if audio else ""
        cached_key = jobs[job_id].get("audio_cache_key") or ""
        cache_hit = (
            provider in ("auphonic", "elevenlabs", "dolby")
            and cache_key
            and cache_key == cached_key
            and cache_path.exists()
            and cache_path.stat().st_size > 0
        )

        if cache_hit:
            # Re-use the previously enhanced audio. No API call, no credits used.
            jobs[job_id]["status"] = "using cached enhanced audio"
            jobs[job_id]["progress"] = 75
            _db_save_job(job_id)
            enhanced_audio_path = UPLOAD_DIR / f"{job_id}_enhanced.aac"
            shutil.copy(cache_path, enhanced_audio_path)
        elif provider == "auphonic":
            if not os.environ.get("AUPHONIC_API_KEY"):
                raise RuntimeError(
                    "Auphonic is not configured on this server (AUPHONIC_API_KEY not set)."
                )

            def _set_status(s: str) -> None:
                jobs[job_id]["status"] = s
                jobs[job_id]["progress"] = {
                    "uploading to Auphonic": 60,
                    "processing audio": 68,
                    "downloading enhanced audio": 78,
                }.get(s, jobs[job_id]["progress"])
                _db_save_job(job_id)

            enhanced_audio_path = UPLOAD_DIR / f"{job_id}_auphonic.aac"
            enhance_with_auphonic(video_path, enhanced_audio_path, audio, status_callback=_set_status)
        elif provider == "elevenlabs":
            def _set_status_eleven(s: str) -> None:
                jobs[job_id]["status"] = s
                jobs[job_id]["progress"] = {
                    "preparing audio": 58,
                    "isolating voice with ElevenLabs": 65,
                    "encoding cleaned audio": 78,
                }.get(s, jobs[job_id]["progress"])
                _db_save_job(job_id)

            enhanced_audio_path = UPLOAD_DIR / f"{job_id}_eleven.aac"
            enhance_with_elevenlabs(video_path, enhanced_audio_path, audio, status_callback=_set_status_eleven)
        elif provider == "dolby":
            def _set_status_dolby(s: str) -> None:
                jobs[job_id]["status"] = s
                jobs[job_id]["progress"] = {
                    "preparing audio for Dolby": 58,
                    "uploading to Dolby": 62,
                    "processing with Dolby": 68,
                    "downloading enhanced audio": 78,
                }.get(s, jobs[job_id]["progress"])
                _db_save_job(job_id)

            enhanced_audio_path = UPLOAD_DIR / f"{job_id}_dolby.aac"
            enhance_with_dolby(video_path, enhanced_audio_path, audio, status_callback=_set_status_dolby)
        else:
            af = build_audio_filter_chain(audio)
            if af:
                jobs[job_id]["status"] = "enhancing audio"
                jobs[job_id]["progress"] = 68
                _db_save_job(job_id)
                enhanced_audio_path = UPLOAD_DIR / f"{job_id}_audio.aac"
                apply_audio_enhancements(video_path, enhanced_audio_path, af)

        # Persist freshly-enhanced AI audio (the RAW provider output, before
        # any local post-process) so future renders can skip the API round-trip
        # until the user changes audio settings that actually affect the API.
        if (not cache_hit
                and enhanced_audio_path
                and enhanced_audio_path.exists()
                and provider in ("auphonic", "elevenlabs", "dolby")
                and cache_key):
            try:
                shutil.copy(enhanced_audio_path, cache_path)
                jobs[job_id]["audio_cache_key"] = cache_key
                _db_save_job(job_id)
            except OSError:
                pass

        # Local post-process: wet/dry blend, post-filters, output gain, limiter.
        # Runs on every render (cached or fresh) so the user can iterate on
        # these knobs without re-billing the AI provider.
        if enhanced_audio_path and enhanced_audio_path.exists() and provider in ("auphonic", "elevenlabs"):
            jobs[job_id]["status"] = "applying post-process"
            jobs[job_id]["progress"] = 80
            _db_save_job(job_id)
            _apply_isolation_postprocess(enhanced_audio_path, video_path, audio)

        # Manual audio sync offset (slider). Positive = trim from audio start
        # (audio plays late). Negative = prepend silence (audio plays early).
        offset_sec = 0.0
        try:
            offset_sec = float(audio.get("offset_seconds", 0) or 0)
        except (TypeError, ValueError):
            offset_sec = 0.0
        if abs(offset_sec) >= 0.01 and enhanced_audio_path and enhanced_audio_path.exists():
            adjusted = enhanced_audio_path.with_suffix(".offset.aac")
            if offset_sec > 0:
                cmd = ["ffmpeg", "-y", "-i", str(enhanced_audio_path),
                       "-ss", f"{offset_sec:.3f}",
                       "-c:a", "aac", "-b:a", "192k", str(adjusted)]
            else:
                delay_ms = int(round(abs(offset_sec) * 1000))
                cmd = ["ffmpeg", "-y", "-i", str(enhanced_audio_path),
                       "-af", f"adelay={delay_ms}|{delay_ms}",
                       "-c:a", "aac", "-b:a", "192k", str(adjusted)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0 and adjusted.exists() and adjusted.stat().st_size > 0:
                enhanced_audio_path.unlink()
                adjusted.rename(enhanced_audio_path)
            else:
                _safe_unlink(adjusted)

        # ---- Burn cache: skip the slow video re-encode when only audio changed.
        burn_cache_path = UPLOAD_DIR / f"{job_id}_burncache.mp4"
        burn_key = _burn_cache_key(style, words, emoji_rules or {}, video_path)
        prev_burn_key = jobs[job_id].get("burn_cache_key") or ""
        burn_cache_hit = (
            burn_key
            and burn_key == prev_burn_key
            and burn_cache_path.exists()
            and burn_cache_path.stat().st_size > 1024
        )

        if burn_cache_hit:
            jobs[job_id]["status"] = "remuxing audio (burn cache hit)"
            jobs[job_id]["progress"] = 92
            _db_save_job(job_id)
            silent_path = burn_cache_path
            silent_is_cache = True
        else:
            jobs[job_id]["status"] = "rendering video"
            jobs[job_id]["progress"] = 80
            _db_save_job(job_id)
            silent_path = UPLOAD_DIR / f"{job_id}_silent.mp4"
            burn_subtitles(
                video_path, ass_path, silent_path,
                audio_path=None,
                quality_boost=bool(style.get("quality_boost")),
                silent=True,
            )
            # Cache the silent burn for next render.
            try:
                shutil.copy(silent_path, burn_cache_path)
                jobs[job_id]["burn_cache_key"] = burn_key
                _db_save_job(job_id)
            except OSError:
                pass
            silent_is_cache = False

        # Mux audio onto the silent video. Use enhanced audio if we produced
        # any, otherwise pull audio straight from the source video.
        output_path = OUTPUT_DIR / f"{job_id}.mp4"
        audio_source = enhanced_audio_path if (
            enhanced_audio_path and enhanced_audio_path.exists()
        ) else video_path
        mux_audio_into_video(silent_path, audio_source, output_path)

        # Clean up the throwaway silent file (the cache copy stays).
        if not silent_is_cache:
            _safe_unlink(silent_path)

        # Verify the final mp4 is real and non-empty before announcing done.
        # This catches the "render said done but nothing showed up" case.
        if not output_path.exists() or output_path.stat().st_size < 1024:
            raise RuntimeError(
                "Render produced an empty or missing output file."
            )
        # Touch mtime so the cleanup loop's TTL clock resets on completion.
        output_path.touch()

        jobs[job_id]["status"] = "done"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["output"] = output_path.name
        jobs[job_id]["completed_at"] = time.time()
        _db_save_job(job_id)
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["completed_at"] = time.time()
        _db_save_job(job_id)
    finally:
        # Keep the source upload around so the user can re-render with
        # different edits/style or retry after an error without re-uploading.
        # The background cleanup loop reclaims it after UPLOAD_TTL_SECONDS.
        _safe_unlink(ass_path) if ass_path else None
        _safe_unlink(enhanced_audio_path) if enhanced_audio_path else None


def process_job(job_id: str, video_path: Path, style: dict, audio: dict | None = None):
    """Legacy single-phase worker used by the /upload route."""
    audio = audio or {}
    try:
        jobs[job_id]["status"] = "transcribing"
        jobs[job_id]["progress"] = 10
        _db_save_job(job_id)
        words = transcribe(video_path)

        if not words:
            raise RuntimeError("No speech detected in the video.")

        render_job(job_id, video_path, words, style, audio, {})
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["completed_at"] = time.time()
        _db_save_job(job_id)
        # render_job handles its own cleanup; only clean up if we never reached it.
        _safe_unlink(video_path)


# ---- Routes ----
@app.route("/jobs")
def list_jobs():
    """Return summary metadata for every known job, newest first.

    Used by the multi-video sidebar. Excludes large fields (words, style,
    audio) — those are fetched on demand via /status/<job_id>.
    """
    out = []
    for jid, j in jobs.items():
        out.append({
            "job_id": jid,
            "filename": j.get("filename"),
            "status": j.get("status"),
            "progress": j.get("progress"),
            "output": j.get("output"),
            "error": (j.get("error") or "")[:200],
            "created_at": j.get("created_at"),
            "completed_at": j.get("completed_at"),
            "has_words": bool(j.get("words")),
            "video_available": find_video_path(jid) is not None,
        })
    out.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return jsonify({"jobs": out})


@app.route("/")
def index():
    auphonic_enabled = bool(os.environ.get("AUPHONIC_API_KEY"))
    elevenlabs_enabled = bool(os.environ.get("ELEVENLABS_API_KEY"))
    dolby_enabled = bool(os.environ.get("DOLBY_API_KEY"))
    return render_template(
        "index.html",
        auphonic_enabled=auphonic_enabled,
        elevenlabs_enabled=elevenlabs_enabled,
        dolby_enabled=dolby_enabled,
    )


@app.route("/transcribe-only", methods=["POST"])
def transcribe_only():
    """Phase 1: upload video and transcribe. Returns job_id; poll /status for words."""
    if "video" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["video"]
    if f.filename == "" or not allowed_file(f.filename):
        return jsonify({"error": "Invalid file type"}), 400

    job_id = uuid.uuid4().hex
    ext = f.filename.rsplit(".", 1)[1].lower()
    video_path = UPLOAD_DIR / f"{job_id}.{ext}"
    f.save(str(video_path))

    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "output": None,
        "error": None,
        "words": None,
        "style": None,
        "audio": None,
        "emoji_rules": None,
        "created_at": time.time(),
        "filename": f.filename,
    }
    _db_save_job(job_id)
    pre_clean = request.form.get("pre_clean", "").lower() in ("1", "true", "yes")
    t = threading.Thread(target=transcribe_job, args=(job_id, video_path, pre_clean))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/render", methods=["POST"])
def render():
    """Phase 2: take edited words + style + emoji rules, burn video."""
    data = request.get_json(force=True)
    job_id = data.get("job_id")
    words = data.get("words", [])
    style = data.get("style", {})
    audio = data.get("audio", {})
    emoji_rules = data.get("emoji_rules", {})

    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404

    if not words:
        return jsonify({"error": "No words provided"}), 400

    error = _validate_words(words)
    if error:
        return jsonify({"error": error}), 400

    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Original video not found on server"}), 404

    jobs[job_id]["words"] = words
    jobs[job_id]["style"] = style
    jobs[job_id]["audio"] = audio
    jobs[job_id]["emoji_rules"] = emoji_rules
    jobs[job_id]["status"] = "queued"
    jobs[job_id]["progress"] = 0
    jobs[job_id]["output"] = None
    jobs[job_id]["error"] = None
    _db_save_job(job_id)

    t = threading.Thread(
        target=render_job,
        args=(job_id, video_path, words, style, audio, emoji_rules),
    )
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


def _validate_words(words: list) -> str | None:
    for i, w in enumerate(words):
        if not isinstance(w, dict):
            return f"Word at index {i} is not an object"
        if not isinstance(w.get("word"), str) or not w["word"].strip():
            return f"Word at index {i} missing valid 'word' field"
        if not isinstance(w.get("start"), (int, float)):
            return f"Word at index {i} missing numeric 'start' field"
        if not isinstance(w.get("end"), (int, float)):
            return f"Word at index {i} missing numeric 'end' field"
    return None


@app.route("/save-draft", methods=["POST"])
def save_draft():
    data = request.get_json(force=True)
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404

    words = data.get("words")
    if words is not None:
        error = _validate_words(words)
        if error:
            return jsonify({"error": error}), 400
        jobs[job_id]["words"] = words

    for key in ("style", "audio", "emoji_rules"):
        if key in data:
            jobs[job_id][key] = data.get(key) or {}

    _db_save_job(job_id)
    return jsonify({"ok": True})


@app.route("/upload", methods=["POST"])
def upload():
    """Legacy single-phase upload (kept for compatibility)."""
    if "video" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["video"]
    if f.filename == "" or not allowed_file(f.filename):
        return jsonify({"error": "Invalid file type"}), 400

    job_id = uuid.uuid4().hex
    ext = f.filename.rsplit(".", 1)[1].lower()
    video_path = UPLOAD_DIR / f"{job_id}.{ext}"
    f.save(str(video_path))

    import json
    style = json.loads(request.form.get("style", "{}"))
    audio = json.loads(request.form.get("audio", "{}"))

    jobs[job_id] = {"status": "queued", "progress": 0, "output": None, "error": None, "words": None, "style": style, "audio": audio, "emoji_rules": {}, "created_at": time.time()}
    _db_save_job(job_id)
    t = threading.Thread(target=process_job, args=(job_id, video_path, style, audio))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    # Tell the client whether the source upload is still on disk. Used by the
    # resume-on-page-load flow to decide whether the editor session is still
    # actionable (after a render, the source is intentionally deleted).
    payload = dict(job)
    payload["video_available"] = find_video_path(job_id) is not None
    return jsonify(payload)


@app.route("/download/<path:filename>")
def download(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


@app.route("/preview/<path:filename>")
def preview(filename):
    return send_from_directory(OUTPUT_DIR, filename)


@app.route("/raw-upload/<job_id>")
def raw_upload(job_id):
    """Stream the original uploaded video so the editor can seek through it."""
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Video not found"}), 404
    return send_from_directory(UPLOAD_DIR, video_path.name)


@app.route("/preview-audio", methods=["POST"])
def preview_audio():
    """Preview the first 30 s of enhanced audio.

    Accepts JSON: { job_id: str, audio: { noise_reduction, voice_clarity, loudness_norm } }
    Reuses the already-uploaded video file so the full video is never re-sent.
    """
    data = request.get_json(force=True)
    job_id = data.get("job_id")
    audio_opts = data.get("audio", {})

    if not job_id or job_id not in jobs:
        return jsonify({"error": "Transcription not found — please transcribe the video first"}), 404

    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Original video no longer available on this server"}), 404

    af = build_audio_filter_chain(audio_opts)
    if not af:
        return jsonify({"error": "No audio enhancements selected"}), 400

    audio_path = UPLOAD_DIR / f"prev_{uuid.uuid4().hex}.aac"
    try:
        apply_audio_enhancements(video_path, audio_path, af, duration=30)
        audio_data = audio_path.read_bytes()
        return Response(audio_data, mimetype="audio/aac")
    except RuntimeError as exc:
        return jsonify({"error": str(exc)[:500]}), 500
    finally:
        _safe_unlink(audio_path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
