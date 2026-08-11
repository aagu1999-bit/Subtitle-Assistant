"""
Subtitle Burner - A Flask web app that adds word-by-word highlighted
captions to videos using Whisper for transcription and FFmpeg for rendering.
"""
import os
import sys
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
# OpenMP multi-thread + Flask background workers can segfault on macOS.
# Keep the single-thread clamp on Darwin only — on Linux/server CPUs it
# makes Whisper and pyannote diarization several× slower for no benefit.
if sys.platform == "darwin":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, Response, make_response
import logging
from logging.handlers import RotatingFileHandler

# ---- Local env auto-load ----
# Loads KEY=VALUE pairs from .env / .env.local in the project root so Flask
# always boots with the same set of API keys regardless of which terminal
# launched it. Without this, restarting Flask from a fresh shell silently
# disabled every `*_API_KEY`-gated panel (Gemini, Auphonic, Dolby, etc.) —
# fix is to keep keys on disk in .env.local (gitignored) and load them here.
def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # Strip optional surrounding quotes; do NOT overwrite if already
            # set in the real environment, so an explicit `export` always wins.
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass

# Project root holds `.env.local`; the burner directory may also have its own.
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_load_env_file(_PROJECT_ROOT / ".env.local")
_load_env_file(_PROJECT_ROOT / ".env")
_load_env_file(Path(__file__).parent / ".env.local")
_load_env_file(Path(__file__).parent / ".env")

# ---- Hugging Face compatibility patch for pyannote.audio ----
def _patch_huggingface_hub() -> None:
    """pyannote.audio passes legacy `use_auth_token` kwarg to `hf_hub_download`,
    which huggingface_hub v0.28+ deprecated in favor of `token`.
    This patch translates `use_auth_token` -> `token` across huggingface_hub and
    any loaded pyannote/speechbrain submodules.
    """
    try:
        import sys
        import huggingface_hub
        _orig = getattr(huggingface_hub, "_raw_hf_hub_download", None)
        if not _orig:
            _orig = getattr(huggingface_hub, "hf_hub_download", None)
            setattr(huggingface_hub, "_raw_hf_hub_download", _orig)

        if not _orig:
            return

        def _patched_hf_hub_download(*args, **kwargs):
            if "use_auth_token" in kwargs:
                t = kwargs.pop("use_auth_token")
                if "token" not in kwargs and t is not None:
                    kwargs["token"] = t
            return _orig(*args, **kwargs)

        huggingface_hub.hf_hub_download = _patched_hf_hub_download

        # Always sweep sys.modules to patch lazily-imported pyannote/speechbrain modules
        import warnings
        for mod_name, mod in list(sys.modules.items()):
            # Skip deprecated speechbrain.pretrained to avoid triggering import warnings
            if "pretrained" in mod_name:
                continue
            if mod and ("pyannote" in mod_name or "speechbrain" in mod_name or "huggingface_hub" in mod_name):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    if hasattr(mod, "hf_hub_download"):
                        setattr(mod, "hf_hub_download", _patched_hf_hub_download)
    except Exception:
        pass

_patch_huggingface_hub()

app = Flask(__name__)

CAPCUT_TEMPLATES = {
    "podcast_interview": {
        "reframe": "9:16",
        "font": "Montserrat Black",
        "speaker_colors": {"SPEAKER_00": "#FFD700", "SPEAKER_01": "#00E5FF"},
        "headline_banner": True
    },
    "capcut_reels": {
        "crop": "9:16",
        "font": "Bebas Neue",
        "animation": "MrBeast Vibrant Bounce",
        "words_per_group": 2,
        "punchwords": True,
        "auto_overlays": True
    },
    "product_spotlight": {
        "aspect_ratio": ["1:1", "9:16"],
        "font": "Anton",
        "headline_banner": "📍 Featured Product",
        "color_grade": "warm"
    },
    "cinematic_vlog": {
        "aspect_ratio": "16:9",
        "font": "DM Sans",
        "animation": "Karaoke Flow",
        "camera": "subtle slow pan/zoom",
        "audio": "ambient background music ducking"
    }
}

# Canonical Caption look schema used by build_ass / Instant Export / Shorts.
# Timeline UI and AI Edit packs historically used short aliases (font, size,
# primary, …); _normalize_caption_style merges those into this shape.
_DEFAULT_CAPTION_STYLE = {
    "font_name": "Montserrat Thin Black",
    "font_size": 64,
    "primary_color": "#FFFFFF",
    "highlight_color": "#FFD60A",
    "accent_color": "#FF6B35",
    "outline_color": "#000000",
    "outline_width": 3,
    "shadow": 1,
    "position_y": 82,
    "all_caps": True,
    "group_size": 3,
    "smooth_timings": True,
    "punchword_emphasis": True,
}

_STYLE_SHORT_TO_LONG = {
    "font": "font_name",
    "size": "font_size",
    "primary": "primary_color",
    "highlight": "highlight_color",
    "accent": "accent_color",
    "outline": "outline_color",
    "group": "group_size",
}


def _normalize_caption_style(style) -> dict:
    """Return Caption look keys so burns never miss AI-pack / Timeline aliases."""
    if not isinstance(style, dict):
        return dict(_DEFAULT_CAPTION_STYLE)
    out = dict(style)
    for short, long in _STYLE_SHORT_TO_LONG.items():
        if out.get(long) in (None, "") and out.get(short) not in (None, ""):
            out[long] = out[short]
    # Mirror canonical → short so Timeline props / co-editor keep working.
    for short, long in _STYLE_SHORT_TO_LONG.items():
        if out.get(long) not in (None, "") and out.get(short) in (None, ""):
            out[short] = out[long]
    for k, v in _DEFAULT_CAPTION_STYLE.items():
        if out.get(k) in (None, ""):
            out[k] = v
    return out


def _style_has_caption_fields(style) -> bool:
    if not isinstance(style, dict) or not style:
        return False
    return any(
        style.get(k) not in (None, "")
        for k in (
            "font_name", "font", "primary_color", "primary",
            "font_size", "size", "highlight_color", "highlight",
        )
    )


# Captions-style AI Edit recipes. Intensity scales cut/zoom/B-roll density.
# These seed timeline JSON — they are not full generative Mirage styles.
AI_EDIT_STYLE_PACKS = {
    "pulse": {
        "label": "Pulse",
        "blurb": "Fast social pacing — punch zooms, hard cuts, bold captions",
        "canvas": "9x16",
        "transition": "crossfade",
        "caption_preset": "mrbeast",
        "style": {
            "font": "Integral CF", "size": 68, "primary": "#FFFFFF",
            "highlight": "#00F2EA", "accent": "#FF0055", "group": 1,
        },
        "color_grade": {"preset": "vivid", "brightness": 0.05, "contrast": 0.1, "saturation": 0.15},
    },
    "clarity": {
        "label": "Clarity",
        "blurb": "Clean talking-head — light trim, subtle zoom, readable captions",
        "canvas": "9x16",
        "transition": "dissolve",
        "caption_preset": "hormozi",
        "style": {
            "font": "Montserrat Black", "size": 64, "primary": "#FFFFFF",
            "highlight": "#FFD60A", "accent": "#00FF88", "group": 2,
        },
        "color_grade": {"preset": "neutral", "brightness": 0.0, "contrast": 0.05, "saturation": 0.0},
    },
    "magazine": {
        "label": "Magazine",
        "blurb": "Editorial polish — soft Ken Burns, warm grade, lower-third titles",
        "canvas": "9x16",
        "transition": "fade_black",
        "caption_preset": "karaoke",
        "style": {
            "font": "DM Sans", "size": 56, "primary": "#F8FAFC",
            "highlight": "#6366F1", "accent": "#EC4899", "group": 3,
        },
        "color_grade": {"preset": "warm", "brightness": 0.02, "contrast": 0.05, "saturation": 0.08},
        "add_title": True,
    },
    "velocity": {
        "label": "Velocity",
        "blurb": "High intensity — dense zooms, silence cuts, energetic captions",
        "canvas": "9x16",
        "transition": "slide",
        "caption_preset": "neon",
        "style": {
            "font": "Bebas Neue", "size": 72, "primary": "#00FF88",
            "highlight": "#FF00FF", "accent": "#00CFFF", "group": 2,
        },
        "color_grade": {"preset": "vivid", "brightness": 0.08, "contrast": 0.15, "saturation": 0.2},
    },
    "film": {
        "label": "Film",
        "blurb": "Cinematic slow push — muted grade, sparse cuts, elegant type",
        "canvas": "16x9",
        "transition": "dissolve",
        "caption_preset": "karaoke",
        "style": {
            "font": "DM Sans", "size": 52, "primary": "#F5F0E8",
            "highlight": "#E8C39E", "accent": "#8B7355", "group": 4,
        },
        "color_grade": {"preset": "cool", "brightness": -0.02, "contrast": 0.08, "saturation": -0.05},
    },
}

_FILLER_SINGLE_WORDS = {
    "um", "uh", "uhh", "uhm", "umm", "er", "erm",
    "ah", "ahh", "hm", "hmm", "mm", "mhm",
    "like", "basically", "literally", "actually",
    "kinda", "sorta", "anyway", "anyways",
    "okay", "ok", "right", "well",
}
_FILLER_PAIR_WORDS = [
    ("you", "know"), ("i", "mean"), ("sort", "of"), ("kind", "of"),
]

# Long-form threshold matching Captions AI Shorts (4 minutes).
LONG_FORM_SECONDS = 240.0


# ---- Config ----
BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

def _setup_logger(name, filename):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(LOGS_DIR / filename, maxBytes=10*1024*1024, backupCount=3)
    formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

api_logger = _setup_logger('api_logger', 'api_requests.log')
ai_logger = _setup_logger('ai_logger', 'ai_audit.log')
ffmpeg_logger = _setup_logger('ffmpeg_logger', 'ffmpeg_render.log')
cache_logger = _setup_logger('cache_logger', 'cache_perf.log')
speaker_logger = _setup_logger('speaker_logger', 'speaker_tracking.log')

@app.before_request
def log_request_info():
    request.start_time = time.time()

@app.after_request
def log_response_info(response):
    if hasattr(request, 'start_time'):
        duration = time.time() - request.start_time
        api_logger.info(f"{request.method} {request.path} {response.status_code} {duration:.3f}s")
    return response

UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
FONT_DIR = BASE_DIR / "fonts"
CACHE_DIR = BASE_DIR / "cache"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
FONT_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

# ---- FFmpeg binary selection ----
# Prefer imageio-ffmpeg's pre-built binary which includes libass (subtitles filter).
# Fall back to system ffmpeg if imageio-ffmpeg is not installed.
def _find_ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        if ff:
            return ff
    except Exception:
        pass
    return "ffmpeg"

FFMPEG = _find_ffmpeg()

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
            for col in ("style", "audio", "emoji_rules", "audio_cache_key", "burn_cache_key", "filename", "compile_recipe", "timeline"):
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
    compile_recipe_json = json.dumps(job.get("compile_recipe")) if job.get("compile_recipe") is not None else None
    timeline_json = json.dumps(job.get("timeline")) if job.get("timeline") is not None else None
    with _db_lock:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO jobs
                    (job_id, status, progress, output, error, words, style, audio, emoji_rules, created_at, completed_at, audio_cache_key, burn_cache_key, filename, compile_recipe, timeline)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    compile_recipe_json,
                    timeline_json,
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
        compile_recipe = None
        try:
            if row["compile_recipe"]:
                compile_recipe = json.loads(row["compile_recipe"])
        except (IndexError, KeyError, json.JSONDecodeError, TypeError):
            compile_recipe = None
        timeline = None
        try:
            if row["timeline"]:
                timeline = json.loads(row["timeline"])
        except (IndexError, KeyError, json.JSONDecodeError, TypeError):
            timeline = None
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
            "compile_recipe": compile_recipe,
            "timeline": timeline,
            # is_timeline isn't its own column — a job is a timeline editor
            # project iff it carries a timeline doc. Derive it on load so the
            # editor list + cleanup exemption survive a server restart.
            "is_timeline": timeline is not None,
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


def _timeline_referenced_job_ids() -> set:
    """Source job_ids referenced by any timeline project (main/overlay/split).

    These must NOT be pruned while a timeline still points at them, otherwise the
    editor loses the footage/transcript it needs to render or text-edit.
    """
    refs: set = set()
    for job in list(jobs.values()):
        tl = job.get("timeline")
        if not tl:
            continue
        tracks = (tl.get("tracks") or {})
        for key in ("main", "overlay"):
            for c in (tracks.get(key) or []):
                if c.get("source_job_id"):
                    refs.add(c["source_job_id"])
                sp = c.get("split") or {}
                if sp.get("source_job_id"):
                    refs.add(sp["source_job_id"])
    return refs


def _cleanup_loop() -> None:
    """Background thread: delete old output MP4s and prune the jobs dict."""
    while True:
        time.sleep(_CLEANUP_INTERVAL)
        now = time.time()
        output_cutoff = now - OUTPUT_TTL_SECONDS
        upload_cutoff = now - UPLOAD_TTL_SECONDS
        referenced = _timeline_referenced_job_ids()

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
            # Timeline editor jobs are re-editable drafts — keep them so the
            # user can tweak and re-render even after the output MP4 expires.
            and not job.get("is_timeline")
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
            # Keep sources a timeline still points at (footage + transcript).
            and jid not in referenced
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
        active_ids = set(jobs.keys()) | referenced
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
# Defaults tuned for interactive Studio use on CPU (Replit / laptops).
# Override with WHISPER_MODEL=small|medium only if you need accuracy over speed.
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
# Cap OpenMP threads — on small VMs "use all cores" often thrashes and is slower.
try:
    WHISPER_CPU_THREADS = max(1, int(os.environ.get("WHISPER_CPU_THREADS", "4")))
except ValueError:
    WHISPER_CPU_THREADS = 4

_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper_model():
    """Lazy-init a single shared WhisperModel. Re-used across all transcriptions."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                try:
                    from faster_whisper import WhisperModel
                    kwargs = dict(
                        device=WHISPER_DEVICE,
                        compute_type=WHISPER_COMPUTE_TYPE,
                    )
                    # cpu_threads is ignored on CUDA builds; safe to pass on CPU.
                    if WHISPER_DEVICE == "cpu":
                        kwargs["cpu_threads"] = WHISPER_CPU_THREADS
                    _whisper_model = WhisperModel(WHISPER_MODEL_NAME, **kwargs)
                    print(
                        f"[Whisper] Loaded model={WHISPER_MODEL_NAME!r} "
                        f"device={WHISPER_DEVICE} compute={WHISPER_COMPUTE_TYPE} "
                        f"cpu_threads={WHISPER_CPU_THREADS}",
                        flush=True,
                    )
                except Exception as err:
                    print(f"[Whisper] Note: faster_whisper model not loaded yet ({err})")
                    return None
    return _whisper_model


# Warm up the Whisper model in the background if installed.
threading.Thread(target=_get_whisper_model, daemon=True).start()


# Phone MOVs / large MP4s often put the moov atom at the end. Default ffprobe
# probesize can miss streams and look like "no audio" even when sound exists.
_FFPROBE_DEEP = ["-analyzeduration", "100M", "-probesize", "100M"]


def _probe_media_streams(path: Path) -> dict:
    """Return stream/format info from a deep ffprobe.

    Never treats probe failure as "no audio" — callers decide. Truncated
    uploads and odd phone containers commonly fail a shallow probe.

    Also reports video_codec / audio_codec so we can tip users about iPhone
    HEVC (plays on Drive after Drive re-encodes; Windows often needs codecs).
    """
    info = {
        "has_audio": False,
        "has_video": False,
        "duration": 0.0,
        "error": None,
        "size": 0,
        "video_codec": None,
        "audio_codec": None,
        "is_hevc": False,
        "format_name": None,
    }
    try:
        info["size"] = path.stat().st_size if path.exists() else 0
    except OSError as e:
        info["error"] = f"cannot read file: {e}"
        return info
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                *_FFPROBE_DEEP,
                "-show_entries",
                "stream=codec_type,codec_name:format=duration,format_name",
                "-of", "json",
                str(path),
            ],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
        data = json.loads(out or "{}")
        for stream in data.get("streams") or []:
            ctype = (stream.get("codec_type") or "").lower()
            cname = (stream.get("codec_name") or "").lower()
            if ctype == "audio":
                info["has_audio"] = True
                if not info["audio_codec"]:
                    info["audio_codec"] = cname
            elif ctype == "video":
                info["has_video"] = True
                if not info["video_codec"]:
                    info["video_codec"] = cname
                if cname in ("hevc", "h265", "hev1", "hvc1"):
                    info["is_hevc"] = True
        fmt = data.get("format") or {}
        info["format_name"] = fmt.get("format_name")
        try:
            info["duration"] = float(fmt.get("duration") or 0)
        except (TypeError, ValueError):
            info["duration"] = 0.0
        # Tag-only HEVC sometimes reports codec_name oddly — also sniff format.
        if not info["is_hevc"] and info.get("video_codec") in ("hevc", "h265"):
            info["is_hevc"] = True
    except subprocess.TimeoutExpired:
        info["error"] = "ffprobe timed out (file may still be writing or corrupt)"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, json.JSONDecodeError) as e:
        info["error"] = str(e) or e.__class__.__name__
    return info


def _media_has_audio(path: Path) -> bool:
    """True if ffprobe finds at least one audio stream (deep probe)."""
    return bool(_probe_media_streams(path).get("has_audio"))


def _validate_uploaded_media(path: Path, expected_bytes: int | None = None) -> dict:
    """Reject truncated / empty / video-only uploads before Whisper starts.

    Returns the probe dict on success; raises RuntimeError with a user-facing
    message on failure.
    """
    try:
        size = path.stat().st_size
    except OSError as e:
        raise RuntimeError(f"Upload could not be read on the server: {e}") from e

    # Empty MP4 shells from aborted transfers are often a few hundred bytes.
    if size < 8_192:
        raise RuntimeError(
            "Upload looks incomplete (file is nearly empty on the server). "
            "Your connection may have dropped mid-transfer — try uploading again."
        )
    if expected_bytes and expected_bytes > 0 and size < int(expected_bytes * 0.95):
        raise RuntimeError(
            f"Upload incomplete: server received {size // 1024} KB but the browser "
            f"sent ~{expected_bytes // 1024} KB. Re-upload on a stable connection."
        )

    probe = _probe_media_streams(path)
    if probe.get("error") and not probe.get("has_video") and not probe.get("has_audio"):
        raise RuntimeError(
            "Upload could not be read as a media file (corrupt or truncated). "
            f"Re-upload the original export. ({probe['error']})"
        )
    if not probe.get("has_video") and not probe.get("has_audio"):
        raise RuntimeError(
            "Upload has no video or audio streams — the file is likely truncated "
            "or not a real media export. Re-upload and try again."
        )
    return probe


def _extract_whisper_wav(video_path: Path, pre_clean: bool = False) -> Path:
    """Extract 16 kHz mono PCM for Whisper.

    Always go through FFmpeg — feeding the raw container to faster-whisper/PyAv
    crashes on video-only files and some phone MOV variants.

    Tries several extract strategies (iPhone .MOV / HE-AAC are picky). Probe is
    advisory only — we never short-circuit to "no audio" before attempting
    extract. Truncated uploads get a different message than true silent video.
    """
    probe = _probe_media_streams(video_path)
    wav = video_path.with_name(f".{video_path.stem}.whisper.wav")
    af = (
        "afftdn=nf=-25,dynaudnorm=p=0.95:m=12:s=12"
        if pre_clean else "anull"
    )

    def _wav_ok() -> bool:
        try:
            return wav.exists() and wav.stat().st_size >= 64
        except OSError:
            return False

    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True)

    # Strategy list: phone MOVs often fail optional -map tricks but succeed
    # when FFmpeg picks the default audio stream (no -map).
    attempts: list[list[str]] = [
        # 1) Default audio stream (best for IMG_*.MOV / QuickTime)
        [
            FFMPEG, "-y", *_FFPROBE_DEEP, "-i", str(video_path),
            "-vn", "-sn", "-dn",
            "-ac", "1", "-ar", "16000", "-af", af,
            "-c:a", "pcm_s16le", str(wav),
        ],
        # 2) Explicit first audio stream
        [
            FFMPEG, "-y", *_FFPROBE_DEEP, "-i", str(video_path),
            "-vn", "-sn", "-dn", "-map", "0:a:0",
            "-ac", "1", "-ar", "16000", "-af", af,
            "-c:a", "pcm_s16le", str(wav),
        ],
        # 3) Any audio stream(s), take first via -ac 1
        [
            FFMPEG, "-y", *_FFPROBE_DEEP, "-i", str(video_path),
            "-vn", "-sn", "-dn", "-map", "0:a",
            "-ac", "1", "-ar", "16000", "-af", af,
            "-c:a", "pcm_s16le", str(wav),
        ],
    ]

    last: subprocess.CompletedProcess | None = None
    for cmd in attempts:
        _safe_unlink(wav)
        last = _run(cmd)
        if last.returncode == 0 and _wav_ok():
            return wav

    # 4) Remux to a clean MP4 then extract — recovers some QuickTime layouts
    # where direct PCM extract fails but streams are present.
    remux = video_path.with_name(f".{video_path.stem}.audioremux.mp4")
    try:
        _safe_unlink(remux)
        remux_proc = _run([
            FFMPEG, "-y", *_FFPROBE_DEEP, "-i", str(video_path),
            "-c", "copy", "-movflags", "+faststart", str(remux),
        ])
        if remux_proc.returncode == 0 and remux.exists() and remux.stat().st_size > 64:
            _safe_unlink(wav)
            last = _run([
                FFMPEG, "-y", *_FFPROBE_DEEP, "-i", str(remux),
                "-vn", "-sn", "-dn",
                "-ac", "1", "-ar", "16000", "-af", af,
                "-c:a", "pcm_s16le", str(wav),
            ])
            if last.returncode == 0 and _wav_ok():
                return wav
    finally:
        _safe_unlink(remux)

    _safe_unlink(wav)
    err_blob = ""
    if last is not None:
        err_blob = ((last.stderr or "") + "\n" + (last.stdout or "")).lower()
    no_stream = any(
        needle in err_blob
        for needle in (
            "does not contain any stream",
            "output file does not contain any stream",
            "matches no streams",
            "stream map '0:a",
        )
    )
    size_kb = int((probe.get("size") or 0) / 1024)
    dur = float(probe.get("duration") or 0)

    # Truncated / unreadable upload — common on large iPhone MOVs mid-transfer.
    if probe.get("error") or (not probe.get("has_audio") and not probe.get("has_video")):
        raise RuntimeError(
            "Could not read audio from this upload — the file on the server looks "
            f"incomplete or unreadable ({size_kb} KB). Re-upload IMG/MOV and try again."
            + (f" Detail: {probe.get('error')}" if probe.get("error") else "")
        )
    if not probe.get("has_audio") and probe.get("has_video") and dur <= 0:
        raise RuntimeError(
            "Upload looks incomplete (video stream found but no usable audio / duration). "
            f"Server file is {size_kb} KB — re-upload the full original recording."
        )
    if not probe.get("has_audio") and (probe.get("has_video") or no_stream):
        tip = ""
        if probe.get("is_hevc"):
            tip = (
                " Note: this looks like iPhone HEVC — Google Drive can still play it "
                "because Drive re-encodes for streaming; upload the original file "
                "(or export Most Compatible / H.264 MP4) and wait for 100% transfer."
            )
        raise RuntimeError(
            "This video has no audio track Whisper can read "
            f"({size_kb} KB, duration {dur:.1f}s"
            + (f", video={probe.get('video_codec')}" if probe.get("video_codec") else "")
            + (f", audio={probe.get('audio_codec') or 'none'}")
            + "). If you hear sound on your phone, re-upload the original recording "
            "or tap Re-drop after a full transfer."
            + tip
        )
    err = (last.stderr or last.stdout or "").strip().splitlines() if last else []
    tail = err[-1] if err else "ffmpeg extract failed"
    raise RuntimeError(f"Could not extract audio for transcription: {tail}")

def _edit_proxy_path(job_id: str) -> Path:
    return UPLOAD_DIR / f"{job_id}_editproxy.mp4"


def build_edit_proxy(job_id: str, video_path: Path) -> None:
    """Background: small H.264 proxy so Transcript Cut seeks fast on phone MOVs.

    Burns / renders always use the original upload — this is editor playback only.
    Started AFTER Whisper finishes so encode does not steal CPU from transcription.
    """
    out = _edit_proxy_path(job_id)
    try:
        # ultrafast + downscale — playback aid only, not on the Whisper critical path.
        proc = subprocess.run(
            [
                FFMPEG, "-y",
                *_FFPROBE_DEEP,
                "-i", str(video_path),
                "-vf", "scale=-2:'min(720,ih)'",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart",
                str(out),
            ],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not out.exists() or out.stat().st_size < 64:
            _safe_unlink(out)
            print(f"[proxy] {job_id} failed: {(proc.stderr or '')[-200:]}", flush=True)
            return
        if job_id in jobs:
            jobs[job_id]["edit_proxy"] = True
            _db_save_job(job_id)
        print(f"[proxy] {job_id} ready ({out.stat().st_size // 1024} KB)", flush=True)
    except Exception as e:
        _safe_unlink(out)
        print(f"[proxy] {job_id} error: {e}", flush=True)


def transcribe(video_path: Path, pre_clean: bool = False, job_id: str | None = None):
    """Return a list of word dicts: [{'word': str, 'start': float, 'end': float}, ...]

    When *pre_clean* is True, run a fast local FFmpeg cleanup pass before
    Whisper. The filters chosen (afftdn for spectral noise gate, dynaudnorm
    for soft-voice boost) don't shift transients, so word-level timestamps
    stay aligned with the original video.

    If *job_id* is set, bump jobs[job_id]['progress'] while segments stream so
    the UI doesn't sit frozen at 30% for the whole pass.
    """
    wav: Path | None = None
    t0 = time.time()
    try:
        model = _get_whisper_model()
        if model is None:
            raise RuntimeError(
                "Whisper model is not loaded. Check that faster-whisper is installed "
                "and WHISPER_MODEL is reachable."
            )
        if job_id and job_id in jobs:
            jobs[job_id]["progress"] = 35
            jobs[job_id]["status"] = "extracting audio"
            _db_save_job(job_id)
        wav = _extract_whisper_wav(video_path, pre_clean=pre_clean)
        extract_s = time.time() - t0
        if job_id and job_id in jobs:
            jobs[job_id]["progress"] = 45
            jobs[job_id]["status"] = "transcribing"
            _db_save_job(job_id)

        beam = max(1, int(os.environ.get("WHISPER_BEAM_SIZE", "1")))
        result = model.transcribe(
            str(wav),
            word_timestamps=True,
            vad_filter=True,
            beam_size=beam,
            best_of=beam,
            # Huge win on CPU: don't re-decode conditioned on prior text.
            condition_on_previous_text=False,
        )
        # faster-whisper returns (segments_generator, info). Guard odd returns.
        if isinstance(result, tuple):
            if len(result) < 1:
                raise RuntimeError("Whisper returned an empty result tuple")
            segments = result[0]
            info = result[1] if len(result) > 1 else None
        else:
            segments = result
            info = None

        duration = float(getattr(info, "duration", 0) or 0) if info is not None else 0.0
        words = []
        last_pct_save = 45
        for seg in segments:
            seg_words = getattr(seg, "words", None) or []
            for w in seg_words:
                text = (getattr(w, "word", None) or "").strip()
                if not text:
                    continue
                try:
                    start = float(getattr(w, "start", 0.0) or 0.0)
                    end = float(getattr(w, "end", start) or start)
                except (TypeError, ValueError):
                    continue
                if end < start:
                    end = start
                words.append({"word": text, "start": start, "end": end})
            # Heartbeat progress from segment end time.
            if job_id and job_id in jobs and duration > 0:
                seg_end = float(getattr(seg, "end", 0) or 0)
                pct = 45 + int(min(50, max(0, (seg_end / duration) * 50)))
                if pct >= last_pct_save + 5:
                    jobs[job_id]["progress"] = pct
                    last_pct_save = pct
                    _db_save_job(job_id)
        elapsed = time.time() - t0
        print(
            f"[Whisper] {video_path.name}: {len(words)} words in {elapsed:.1f}s "
            f"(extract {extract_s:.1f}s, model={WHISPER_MODEL_NAME})",
            flush=True,
        )
        return words
    except IndexError as e:
        # PyAV's cryptic failure mode for missing/broken audio streams.
        raise RuntimeError(
            "Could not read audio from this video (no usable audio stream). "
            "Re-export with audio and try again."
        ) from e
    finally:
        if wav:
            _safe_unlink(wav)


# ---- Interview reframe: speaker diarization + face tracking ----
#
# Two-pass analysis that lets the burner produce a 9:16 vertical edit where
# the active speaker fills the frame. All heavyweight imports (pyannote,
# mediapipe, torch) are lazy so the app boots even if the user hasn't
# installed the optional deps. Results are cached to JSON per job so a
# subsequent render reuses them without re-running the expensive analysis.

# Frames-per-second we sample the source video at for face detection.
# Face boxes change slowly (people don't teleport) so 2 fps is plenty —
# we interpolate between samples at burn time. Lower fps = faster analysis.
REFRAME_FACE_SAMPLE_FPS = float(os.environ.get("REFRAME_FACE_SAMPLE_FPS", "2"))
HUGGINGFACE_TOKEN_ENV = "HF_TOKEN"
DIARIZATION_MODEL = os.environ.get(
    "DIARIZATION_MODEL", "pyannote/speaker-diarization-3.1"
)
# auto | cuda | mps | cpu — auto picks CUDA, then Apple MPS, then CPU.
DIARIZATION_DEVICE = (os.environ.get("DIARIZATION_DEVICE") or "auto").strip().lower()

_diarization_pipeline = None
_diarization_pipeline_lock = threading.Lock()
_diarization_device_resolved: str | None = None


def _diarization_torch_threads() -> int:
    """Thread budget for torch/pyannote inference.

    Darwin stays at 1 (OpenMP + Flask worker segfaults). Linux/server uses
    up to 8 cores unless DIARIZATION_TORCH_THREADS overrides.
    """
    override = (os.environ.get("DIARIZATION_TORCH_THREADS") or "").strip()
    if override.isdigit():
        return max(1, int(override))
    if sys.platform == "darwin":
        return 1
    return max(1, min(8, os.cpu_count() or 4))


def _resolve_diarization_device(torch_mod) -> str:
    pref = DIARIZATION_DEVICE
    if pref in ("cuda", "gpu"):
        if torch_mod.cuda.is_available():
            return "cuda"
        raise RuntimeError("DIARIZATION_DEVICE=cuda but CUDA is not available")
    if pref == "mps":
        mps = getattr(torch_mod.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
        raise RuntimeError("DIARIZATION_DEVICE=mps but MPS is not available")
    if pref == "cpu":
        return "cpu"
    # auto
    if torch_mod.cuda.is_available():
        return "cuda"
    mps = getattr(torch_mod.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def _get_diarization_pipeline():
    """Load pyannote once and reuse across Analyze runs (model load is slow)."""
    global _diarization_pipeline, _diarization_device_resolved
    if _diarization_pipeline is not None:
        return _diarization_pipeline
    with _diarization_pipeline_lock:
        if _diarization_pipeline is not None:
            return _diarization_pipeline
        from pyannote.audio import Pipeline  # heavy, lazy import
        import torch

        torch.set_num_threads(_diarization_torch_threads())
        _patch_huggingface_hub()
        token = (
            os.environ.get(HUGGINGFACE_TOKEN_ENV)
            or os.environ.get("HUGGINGFACE_TOKEN")
            or os.environ.get("HF_TOKEN")
        )
        try:
            # huggingface_hub / pyannote recently renamed use_auth_token → token.
            try:
                pipeline = Pipeline.from_pretrained(DIARIZATION_MODEL, token=token)
            except TypeError as kw_err:
                if "token" not in str(kw_err) and "use_auth_token" not in str(kw_err):
                    raise
                pipeline = Pipeline.from_pretrained(
                    DIARIZATION_MODEL, use_auth_token=token
                )
        except Exception as err:
            err_str = str(err)
            if (
                "gated" in err_str.lower()
                or "401" in err_str
                or "403" in err_str
                or "private" in err_str.lower()
            ):
                raise RuntimeError(
                    "Hugging Face Access Error: Please verify you have accepted the free model "
                    "agreements at https://hf.co/pyannote/speaker-diarization-3.1 and "
                    "https://hf.co/pyannote/segmentation-3.0."
                ) from err
            raise
        if pipeline is None:
            raise RuntimeError(f"Failed to load {DIARIZATION_MODEL} pipeline.")

        device_name = _resolve_diarization_device(torch)
        try:
            pipeline.to(torch.device(device_name))
        except Exception as move_err:
            print(
                f"[diarize] could not move pipeline to {device_name}: {move_err}; using CPU",
                flush=True,
            )
            device_name = "cpu"
            try:
                pipeline.to(torch.device("cpu"))
            except Exception:
                pass

        _diarization_device_resolved = device_name
        _diarization_pipeline = pipeline
        print(
            f"[diarize] pipeline ready model={DIARIZATION_MODEL} "
            f"device={device_name} torch_threads={_diarization_torch_threads()}",
            flush=True,
        )
        return _diarization_pipeline


def _warm_diarization_pipeline() -> None:
    """Background warm so the first Analyze doesn't pay full model-download cost."""
    try:
        if not (
            os.environ.get(HUGGINGFACE_TOKEN_ENV)
            or os.environ.get("HUGGINGFACE_TOKEN")
            or os.environ.get("HF_TOKEN")
        ):
            return
        _get_diarization_pipeline()
    except Exception as e:
        print(f"[diarize] warm skipped: {e}", flush=True)


threading.Thread(target=_warm_diarization_pipeline, daemon=True).start()


def _reframe_deps_available() -> tuple[bool, str]:
    """Return (ok, msg). Tries to import the heavy deps without crashing the
    app if they're missing — the reframe feature is opt-in.

    Reports the actual ImportError reason (e.g. missing libGL.so.1) instead
    of pretending the package isn't installed. mediapipe in particular
    installs cleanly but fails to import on minimal Linux sandboxes (Replit,
    Docker slim) because its native module links against libGL at runtime.
    """
    try:
        import mediapipe  # noqa: F401
    except ImportError as e:
        return False, f"mediapipe failed to import: {e}"
    except Exception as e:
        return False, f"mediapipe import raised {type(e).__name__}: {e}"
    try:
        import pyannote.audio  # noqa: F401
    except ImportError as e:
        return False, f"pyannote.audio failed to import: {e}"
    except Exception as e:
        return False, f"pyannote.audio import raised {type(e).__name__}: {e}"
    if not (
        os.environ.get(HUGGINGFACE_TOKEN_ENV)
        or os.environ.get("HUGGINGFACE_TOKEN")
        or os.environ.get("HF_TOKEN")
    ):
        return False, (
            f"{HUGGINGFACE_TOKEN_ENV} env var missing. Get a free token at "
            f"https://huggingface.co/settings/tokens and accept the model "
            f"licence at https://huggingface.co/pyannote/speaker-diarization-3.1"
        )
    return True, ""


def _extract_audio_for_diarization(video_path: Path, out_path: Path) -> None:
    """16 kHz mono WAV is the standard input for pyannote."""
    proc = subprocess.run(
        [FFMPEG, "-y", "-i", str(video_path),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
         str(out_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Audio extract for diarization failed: {proc.stderr[-400:]}"
        )


def _load_wav_waveform(audio_path: Path):
    """Load mono/stereo PCM WAV as float32 waveform + sample rate.

    In-memory input is faster than re-decoding the file path inside pyannote.
    Returns (None, sample_rate) if the WAV layout isn't a simple PCM we handle.
    """
    import wave
    import numpy as np
    import torch

    with wave.open(str(audio_path), "rb") as w:
        sr = int(w.getframerate())
        n_ch = int(w.getnchannels())
        n_frames = int(w.getnframes())
        sampwidth = int(w.getsampwidth())
        raw = w.readframes(n_frames)
    if sampwidth == 2:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        audio = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        return None, sr
    if n_ch > 1:
        audio = audio.reshape(-1, n_ch).mean(axis=1)
    waveform = torch.from_numpy(np.ascontiguousarray(audio)).unsqueeze(0)
    return waveform, sr


def diarize_audio(
    video_path: Path,
    *,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> list[dict]:
    """Run pyannote speaker diarization and return a list of segments:
    [{start, end, speaker}]  where speaker is "SPEAKER_00", "SPEAKER_01", ...

    Sorted by start. Skipping overlapped regions inside pyannote's output —
    those are surfaced separately by ``find_overlap_regions``.

    Speeds vs the old path:
      - cached pipeline (no per-run HF download / weight load)
      - CUDA/MPS when available (DIARIZATION_DEVICE=auto)
      - multi-thread torch on Linux
      - in-memory waveform when possible
      - optional num_speakers hint shrinks clustering work
    """
    import torch

    torch.set_num_threads(_diarization_torch_threads())

    # Use a stable temp path inside UPLOAD_DIR — avoid .with_suffix() on paths
    # whose stem contains dots (e.g. "file.backup.mp4" → wrong suffix stripping).
    job_stem = video_path.stem
    audio_path = UPLOAD_DIR / f"{job_stem}.diar.wav"
    try:
        _extract_audio_for_diarization(video_path, audio_path)
        # Sanity-check: if FFmpeg silently failed, surface a clear error now.
        if not audio_path.exists() or audio_path.stat().st_size == 0:
            raise RuntimeError(
                f"Audio extraction produced no output at {audio_path}. "
                "Check that the video has an audio track."
            )
        pipeline = _get_diarization_pipeline()
        call_kwargs: dict = {}
        if num_speakers is not None and int(num_speakers) > 0:
            call_kwargs["num_speakers"] = int(num_speakers)
        if min_speakers is not None and int(min_speakers) > 0:
            call_kwargs["min_speakers"] = int(min_speakers)
        if max_speakers is not None and int(max_speakers) > 0:
            call_kwargs["max_speakers"] = int(max_speakers)

        t0 = time.time()
        waveform, sr = _load_wav_waveform(audio_path)
        if waveform is not None:
            diar = pipeline(
                {"waveform": waveform, "sample_rate": sr},
                **call_kwargs,
            )
        else:
            diar = pipeline(str(audio_path), **call_kwargs)
        elapsed = time.time() - t0
        device = _diarization_device_resolved or "unknown"
        print(
            f"[diarize] inference {elapsed:.1f}s device={device} "
            f"model={DIARIZATION_MODEL}",
            flush=True,
        )
        segments = []
        for turn, _, speaker in diar.itertracks(yield_label=True):
            segments.append({
                "start": round(float(turn.start), 3),
                "end": round(float(turn.end), 3),
                "speaker": str(speaker),
            })
        segments.sort(key=lambda s: s["start"])
        return segments
    finally:
        _safe_unlink(audio_path)


def find_overlap_regions(diar: list[dict]) -> list[dict]:
    """Pairwise scan of diarization segments to surface windows where two or
    more speakers are talking simultaneously — these are the moments the
    compositor will render as a vertical split instead of single-speaker
    crop.

    Returns [{start, end, speakers: [labels]}] merged so adjacent overlap
    windows with the same speaker set become one segment.
    """
    events: list[tuple[float, int, str]] = []  # (time, +1/-1, speaker)
    for s in diar:
        events.append((s["start"], +1, s["speaker"]))
        events.append((s["end"], -1, s["speaker"]))
    events.sort(key=lambda e: (e[0], -e[1]))

    active: dict[str, int] = {}
    overlaps: list[dict] = []
    last_t = None
    for t, delta, spk in events:
        if last_t is not None and t > last_t and sum(active.values()) >= 2:
            overlaps.append({
                "start": last_t,
                "end": t,
                "speakers": sorted(k for k, v in active.items() if v > 0),
            })
        active[spk] = active.get(spk, 0) + delta
        last_t = t

    # Merge adjacent overlaps with identical speaker sets.
    merged: list[dict] = []
    for o in overlaps:
        if merged and merged[-1]["end"] >= o["start"] - 0.01 and \
                merged[-1]["speakers"] == o["speakers"]:
            merged[-1]["end"] = o["end"]
        else:
            merged.append(o)
    return merged


def detect_face_tracks(video_path: Path,
                        sample_fps: float = REFRAME_FACE_SAMPLE_FPS) -> list[dict]:
    """Sample frames at *sample_fps* and run MediaPipe Face Detection.
    Returns one entry per sampled frame:
        {t: seconds, faces: [{cx, cy, w, h, score}, ...]}
    Coordinates are normalised to [0..1] of frame width/height.
    """
    import cv2
    import mediapipe as mp

    sample_fps = float(sample_fps) if sample_fps else float(REFRAME_FACE_SAMPLE_FPS)
    sample_fps = max(0.5, sample_fps)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video for face detection: {video_path}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, int(round(src_fps / sample_fps)))

    # MediaPipe: good for frontal faces
    detector = mp.solutions.face_detection.FaceDetection(
        model_selection=1, min_detection_confidence=0.35,
    )
    # OpenCV cascades: good for profile/side faces that MediaPipe misses
    cv_frontal = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml")
    cv_profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")

    samples: list[dict] = []
    try:
        for fi in range(0, total_frames, step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap.read()
            if not ok:
                continue
            fh, fw = frame.shape[:2]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            faces: list[dict] = []
            seen_cx: set = set()  # deduplicate overlapping detections

            # --- MediaPipe detections ---
            res = detector.process(rgb)
            for det in (res.detections or []):
                bb = det.location_data.relative_bounding_box
                cx = bb.xmin + bb.width / 2
                cy = bb.ymin + bb.height / 2
                key = round(cx, 1)
                seen_cx.add(key)
                faces.append({
                    "cx": round(float(cx), 4),
                    "cy": round(float(cy), 4),
                    "w":  round(float(bb.width), 4),
                    "h":  round(float(bb.height), 4),
                    "score": round(float(det.score[0]) if det.score else 0.0, 3),
                    "source": "mediapipe",
                })

            # --- OpenCV frontal + profile cascade (catches what MediaPipe misses) ---
            for cascade, flip in [(cv_frontal, False), (cv_profile, False), (cv_profile, True)]:
                img = cv2.flip(gray, 1) if flip else gray
                rects = cascade.detectMultiScale(
                    img, scaleFactor=1.1, minNeighbors=4,
                    minSize=(int(fw * 0.05), int(fh * 0.05)),
                )
                for (x, y, w, h) in (rects if len(rects) else []):
                    if flip:
                        x = fw - x - w  # un-mirror
                    cx = (x + w / 2) / fw
                    cy = (y + h / 2) / fh
                    # Skip if MediaPipe already found a face in this region
                    key = round(cx, 1)
                    if key in seen_cx:
                        continue
                    seen_cx.add(key)
                    faces.append({
                        "cx": round(float(cx), 4),
                        "cy": round(float(cy), 4),
                        "w":  round(float(w / fw), 4),
                        "h":  round(float(h / fh), 4),
                        "score": 0.6,
                        "source": "opencv",
                    })

            samples.append({"t": round(fi / src_fps, 3), "faces": faces})
    finally:
        cap.release()
        detector.close()
    return samples


def analyze_reframe(
    video_path: Path,
    *,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    face_sample_fps: float | None = None,
    progress_cb=None,
) -> dict:
    """End-to-end analysis: diarization + face tracking + overlap detection.

    Diarization (torch) and face sampling (MediaPipe/OpenCV) run in parallel
    so wall-clock time is closer to max(diar, faces) instead of sum.

    Returns a JSON-serializable payload that's cached per job for the
    compositor to consume. Caller is responsible for spawning this in a
    background thread — both passes are CPU-heavy.
    """
    ok, msg = _reframe_deps_available()
    if not ok:
        raise RuntimeError(msg)

    def _progress(pct: int, status: str) -> None:
        if progress_cb:
            try:
                progress_cb(pct, status)
            except Exception:
                pass

    fps = float(face_sample_fps) if face_sample_fps is not None else float(REFRAME_FACE_SAMPLE_FPS)
    _progress(15, "analysing speakers")

    diar: list[dict] = []
    faces: list[dict] = []
    errors: list[BaseException] = []

    def _run_diar():
        return diarize_audio(
            video_path,
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
        )

    def _run_faces():
        return detect_face_tracks(video_path, sample_fps=fps)

    # Overlap wall time: pyannote (GIL-heavy but releases in native ops) +
    # OpenCV/MediaPipe face pass on another thread.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="reframe") as pool:
        fut_diar = pool.submit(_run_diar)
        fut_faces = pool.submit(_run_faces)
        for fut in as_completed([fut_diar, fut_faces]):
            try:
                if fut is fut_diar:
                    diar = fut.result()
                    _progress(55, "speakers labelled — tracking faces")
                else:
                    faces = fut.result()
                    _progress(70, "faces sampled — finishing diarization")
            except BaseException as e:
                errors.append(e)

    if errors:
        # Prefer the first failure; cancel isn't needed — both already done/failed.
        raise errors[0]

    overlaps = find_overlap_regions(diar)
    speakers = sorted({s["speaker"] for s in diar})
    _progress(90, "caching speaker map")
    return {
        "diarization": diar,
        "overlaps": overlaps,
        "faces": faces,
        "stats": {
            "speakers": speakers,
            "speaker_count": len(speakers),
            "face_samples": len(faces),
            "overlap_seconds": round(sum(o["end"] - o["start"] for o in overlaps), 2),
            "diarization_device": _diarization_device_resolved,
            "face_sample_fps": fps,
        },
    }


# ---- Reframe compositor ----
#
# Takes the analysis payload from analyze_reframe + the source video, and
# produces a 9:16 vertical edit where each segment is cropped onto the
# active speaker's face. Overlap segments render as a vertical-stack split.
#
# Approach mirrors compile-clips / silence-tightening: write each segment
# to its own file at uniform encoder params, then concat-demuxer them.
# Single-pass filter_complex with conditional per-time crops works in
# theory but is fragile in ffmpeg 8.x — the per-segment pattern has been
# proven reliable in this codebase.

REFRAME_TARGET_W = 1080
REFRAME_TARGET_H = 1920  # 9:16 short-form canvas


def _face_samples_at(faces: list[dict], t: float) -> list[dict]:
    """Nearest-in-time face sample to *t*. Returns the `faces` array
    (possibly empty) from that sample."""
    if not faces:
        return []
    # Binary-search would be O(log n) — for typical short clips a linear
    # scan is fine and avoids importing bisect.
    nearest = min(faces, key=lambda s: abs(s.get("t", 0) - t))
    return nearest.get("faces", []) or []


def _assign_speakers_to_faces(diar: list[dict],
                               faces: list[dict]) -> tuple[dict, dict]:
    """Best-effort mapping speaker_label → (cx, cy) typical position + avg face bbox.

    Returns (positions_dict, bboxes_dict) where bboxes_dict maps speaker
    to (cx, cy, w, h) average face bounding box in normalised coords.
    """
    by_speaker: dict[str, list[tuple[float, float, float, float]]] = {}  # cx,cy,w,h
    # Build a per-speaker exclusivity timeline: only counts windows
    # where this speaker alone is talking (so we don't double-attribute
    # overlap moments).
    intervals_by_speaker: dict[str, list[tuple[float, float]]] = {}
    for seg in diar:
        intervals_by_speaker.setdefault(seg["speaker"], []).append(
            (seg["start"], seg["end"])
        )

    def speaker_alone_at(t: float, spk: str) -> bool:
        for s in diar:
            if s["speaker"] == spk:
                if s["start"] <= t <= s["end"]:
                    return True
                continue
            if s["start"] <= t <= s["end"]:
                return False
        return False

    for sample in faces:
        t = float(sample.get("t", 0))
        face_list = sample.get("faces") or []
        if not face_list:
            continue
        for spk in intervals_by_speaker:
            if speaker_alone_at(t, spk):
                    best = max(face_list, key=lambda f: f.get("score", 0))
                    by_speaker.setdefault(spk, []).append((
                        float(best["cx"]), float(best["cy"]),
                        float(best.get("w", 0.15)), float(best.get("h", 0.25)),
                    ))

    out: dict[str, tuple[float, float]] = {}
    out_bbox: dict[str, tuple[float, float, float, float]] = {}  # speaker -> avg (cx,cy,w,h)
    all_positions: list[tuple[float, float]] = []
    all_bboxes: list[tuple[float, float, float, float]] = []
    for sample in faces:
        for f in (sample.get("faces") or []):
            all_positions.append((float(f["cx"]), float(f["cy"])))
            all_bboxes.append((float(f["cx"]), float(f["cy"]),
                               float(f.get("w", 0.15)), float(f.get("h", 0.25))))
    overall_centre = (
        (sum(p[0] for p in all_positions) / len(all_positions),
         sum(p[1] for p in all_positions) / len(all_positions))
        if all_positions else (0.5, 0.5)
    )
    overall_bbox = (
        (sum(b[0] for b in all_bboxes) / len(all_bboxes),
         sum(b[1] for b in all_bboxes) / len(all_bboxes),
         sum(b[2] for b in all_bboxes) / len(all_bboxes),
         sum(b[3] for b in all_bboxes) / len(all_bboxes))
        if all_bboxes else (0.5, 0.5, 0.15, 0.25)
    )

    for spk in intervals_by_speaker:
        positions = by_speaker.get(spk) or []
        if positions:
            out[spk] = (
                sum(p[0] for p in positions) / len(positions),
                sum(p[1] for p in positions) / len(positions),
            )
            out_bbox[spk] = (
                sum(p[0] for p in positions) / len(positions),
                sum(p[1] for p in positions) / len(positions),
                sum(p[2] for p in positions) / len(positions),
                sum(p[3] for p in positions) / len(positions),
            )
        else:
            out[spk] = overall_centre
            out_bbox[spk] = overall_bbox

    # If two or more speakers landed on the same cluster (face detector
    # only saw one person), nudge them apart along x so they don't
    # all crop to the same spot.
    if len(out) >= 2:
        xs = sorted(out.values(), key=lambda p: p[0])
        if xs[-1][0] - xs[0][0] < 0.05:
            # Order speakers by FIRST APPEARANCE in diarization timeline so
            # speaker swapping (reframe-swap-speakers) flips positions!
            speakers_ordered: list[str] = []
            for seg in diar:
                spk = seg.get("speaker")
                if spk and spk in out and spk not in speakers_ordered:
                    speakers_ordered.append(spk)
            for spk in sorted(out.keys()):
                if spk not in speakers_ordered:
                    speakers_ordered.append(spk)

            for i, spk in enumerate(speakers_ordered):
                cx, cy = out[spk]
                offset = (i / max(1, len(speakers_ordered) - 1) - 0.5) * 0.3
                out[spk] = (min(1.0, max(0.0, cx + offset)), cy)
                out_bbox[spk] = (min(1.0, max(0.0, cx + offset)), cy,
                                 out_bbox[spk][2], out_bbox[spk][3])
                                 
    speaker_logger.info(f"Assigned speakers to faces: positions={out}, bboxes={out_bbox}")
    return out, out_bbox


def _active_speaker_at(diar: list[dict], t: float) -> str | None:
    """First speaker whose interval contains *t*. Used during solo
    segments — overlap windows are handled separately."""
    for seg in diar:
        if seg["start"] <= t < seg["end"]:
            return seg["speaker"]
    return None


def _flatten_reframe_timeline(diar: list[dict], overlaps: list[dict],
                               total_duration: float) -> list[dict]:
    """Walk the source timeline and produce a contiguous list of segments:
        [{start, end, mode: 'solo'|'split'|'empty', speakers: [...]}]

    'empty' segments (no speaker active) hold the previous active
    speaker's framing so the camera doesn't snap to centre during
    pauses — they're rewritten in the caller to inherit framing.
    """
    # Time-boundary points: all diar starts/ends + overlap starts/ends.
    points = {0.0, total_duration}
    for s in diar:
        points.add(s["start"]); points.add(s["end"])
    for o in overlaps:
        points.add(o["start"]); points.add(o["end"])
    pts = sorted(p for p in points if 0 <= p <= total_duration)

    overlap_set = sorted(overlaps, key=lambda o: o["start"])
    segs: list[dict] = []
    for a, b in zip(pts, pts[1:]):
        if b - a < 0.04:  # below 1 frame at 24fps — skip
            continue
        mid = (a + b) / 2
        # Overlap?
        for o in overlap_set:
            if o["start"] <= mid < o["end"]:
                segs.append({
                    "start": a, "end": b,
                    "mode": "split", "speakers": list(o["speakers"]),
                })
                break
        else:
            spk = _active_speaker_at(diar, mid)
            if spk is None:
                segs.append({"start": a, "end": b, "mode": "empty", "speakers": []})
            else:
                segs.append({"start": a, "end": b, "mode": "solo", "speakers": [spk]})

    # Merge adjacent same-mode segments with same speakers.
    merged: list[dict] = []
    for s in segs:
        if merged and merged[-1]["mode"] == s["mode"] and \
                merged[-1]["speakers"] == s["speakers"]:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(dict(s))
    return merged


def _solo_crop(src_w: int, src_h: int,
               speaker_pos: tuple[float, float],
               face_bbox: tuple[float, float, float, float] | None = None,
               ) -> tuple[int, int, int, int]:
    """Compute (x, y, w, h) for a 9:16 crop tightly zoomed on the speaker.

    If *face_bbox* (cx, cy, fw, fh) normalised is available, zoom in so the
    face fills roughly the upper third of the crop (cinematic talking-head
    style). Without bbox data, fall back to a full-frame 9:16 pan.
    """
    src_aspect = src_w / src_h
    target_aspect = REFRAME_TARGET_W / REFRAME_TARGET_H  # 9/16

    if face_bbox is not None:
        # Zoom in so the face is roughly 35% of frame height (tight head shot).
        # face_bbox is (cx, cy, w, h) all normalised to [0..1].
        fcx, fcy, fw, fh = face_bbox
        # Target: face occupies ~35% of output height → crop height = face_h / 0.35
        face_h_px = fh * src_h
        crop_h = min(src_h, max(int(face_h_px / 0.30), int(src_h * 0.25)))
        crop_w = int(round(crop_h * target_aspect))
        if crop_w > src_w:
            crop_w = src_w
            crop_h = int(round(crop_w / target_aspect))
        # Anchor: face centre in upper-third of crop
        cx_px = fcx * src_w
        cy_px = fcy * src_h
        x = int(round(cx_px - crop_w / 2))
        # Place face in upper 40% of crop
        y = int(round(cy_px - crop_h * 0.35))
        x = max(0, min(src_w - crop_w, x))
        y = max(0, min(src_h - crop_h, y))
        return x, y, crop_w, crop_h

    # Fallback: no face bbox — pan to speaker position, 9:16 full crop
    if src_aspect > target_aspect:
        crop_h = src_h
        crop_w = int(round(crop_h * target_aspect))
        cx_px = speaker_pos[0] * src_w
        x = int(round(cx_px - crop_w / 2))
        x = max(0, min(src_w - crop_w, x))
        return x, 0, crop_w, crop_h
    crop_w = src_w
    crop_h = int(round(crop_w / target_aspect))
    cy_px = speaker_pos[1] * src_h
    y = int(round(cy_px - crop_h * 0.4))
    y = max(0, min(src_h - crop_h, y))
    return 0, y, crop_w, crop_h


def _split_crops(src_w: int, src_h: int,
                  positions: list[tuple[float, float]],
                  bboxes: list[tuple | None] | None = None,
                  ) -> list[tuple[int, int, int, int]]:
    """2-way zoomed crops for a vertical-stack overlap segment.

    Each crop fills half the 9:16 canvas height (1080x960). When a face bbox is
    available we zoom in tight — same logic as _solo_crop.
    Without bbox data we fall back to a horizontal pan."""
    panel_h = REFRAME_TARGET_H // 2  # 960 px per half (50/50 split)
    target_aspect = REFRAME_TARGET_W / panel_h  # 1.125 (9:8)
    crops = []
    for i in range(2):
        cx, cy = positions[i] if i < len(positions) else (0.5, 0.5)
        bbox = (bboxes[i] if bboxes and i < len(bboxes) else None)
        if bbox is not None:
            fcx, fcy, fw, fh = bbox
            face_h_px = fh * src_h
            zoom_frac = 0.38
            crop_h = min(src_h, max(int(face_h_px / zoom_frac), int(src_h * 0.2)))
            crop_w = int(round(crop_h * target_aspect))
            if crop_w > src_w:
                crop_w = src_w
                crop_h = int(round(crop_w / target_aspect))
            cx_px = fcx * src_w
            cy_px = fcy * src_h
            x = int(round(cx_px - crop_w / 2))
            y = int(round(cy_px - crop_h * 0.35))
            x = max(0, min(src_w - crop_w, x))
            y = max(0, min(src_h - crop_h, y))
            crops.append((x, y, crop_w, crop_h))
        else:
            src_aspect = src_w / src_h
            if src_aspect > target_aspect:
                crop_h = src_h
                crop_w = int(round(crop_h * target_aspect))
                cx_px = cx * src_w
                x = int(round(cx_px - crop_w / 2))
                x = max(0, min(src_w - crop_w, x))
                crops.append((x, 0, crop_w, crop_h))
            else:
                crop_w = src_w
                crop_h = int(round(crop_w / target_aspect))
                cy_px = cy * src_h
                y = int(round(cy_px - crop_h * 0.4))
                y = max(0, min(src_h - crop_h, y))
                crops.append((0, y, crop_w, crop_h))
    return crops


def compute_reframe_plan(reframe_data: dict, src_w: int, src_h: int,
                          total_duration: float,
                          top_panel: str = "active",
                          bottom_panel: str = "full") -> list[dict]:
    """Turn the analysis JSON into a list of compositor instructions.

    *top_panel* and *bottom_panel* options:
      - 'active': Zoom on currently active speaker.
      - 'left': Zoom on Speaker 1 (Left face).
      - 'right': Zoom on Speaker 2 (Right face).
      - 'full': Original wide video shot.
    """
    diar = reframe_data.get("diarization") or []
    overlaps = reframe_data.get("overlaps") or []
    faces = reframe_data.get("faces") or []
    speaker_positions, speaker_bboxes = _assign_speakers_to_faces(diar, faces)
    segs = _flatten_reframe_timeline(diar, overlaps, total_duration)

    spks_by_cx = sorted(speaker_positions.keys(), key=lambda s: speaker_positions[s][0])
    left_spk = spks_by_cx[0] if spks_by_cx else "SPEAKER_00"
    right_spk = spks_by_cx[1] if len(spks_by_cx) > 1 else left_spk

    is_swapped = bool(reframe_data.get("swap_speaker_voices"))

    # Full video (wide shot) crop for 9:8 half panel
    target_aspect = REFRAME_TARGET_W / (REFRAME_TARGET_H // 2)  # 1.125 (9:8)
    src_aspect = src_w / src_h
    if src_aspect > target_aspect:
        full_crop_h = src_h
        full_crop_w = int(round(src_h * target_aspect))
        full_x = max(0, (src_w - full_crop_w) // 2)
        full_y = 0
    else:
        full_crop_w = src_w
        full_crop_h = int(round(src_w / target_aspect))
        full_x = 0
        full_y = max(0, (src_h - full_crop_h) // 2)
    full_video_crop = (full_x, full_y, full_crop_w, full_crop_h)

    def crop_for_setting(setting_name: str, active_spk: str) -> tuple[int, int, int, int]:
        if setting_name == "full":
            return full_video_crop
        if setting_name == "active":
            spk = active_spk
            if is_swapped:
                spk = right_spk if active_spk == left_spk else (left_spk if active_spk == right_spk else active_spk)
        elif setting_name == "left":
            spk = right_spk if is_swapped else left_spk
        else:
            spk = left_spk if is_swapped else right_spk
        pos = speaker_positions.get(spk, (0.5, 0.5))
        bbox = speaker_bboxes.get(spk)
        crops = _split_crops(src_w, src_h, [pos], bboxes=[bbox])
        return crops[0]

    plan: list[dict] = []
    last_active_spk = left_spk

    for s in segs:
        if s["speakers"]:
            last_active_spk = s["speakers"][0]

        top_crop = crop_for_setting(top_panel, last_active_spk)
        bottom_crop = crop_for_setting(bottom_panel, last_active_spk)

        plan.append({
            "start": s["start"], "end": s["end"], "mode": "split", "crops": [top_crop, bottom_crop],
        })
        
    speaker_logger.info(f"Computed reframe plan: {len(plan)} segments, is_swapped={is_swapped}")
    return plan


def apply_reframe(video_path: Path, plan: list[dict], output_path: Path) -> None:
    """Render the 9:16 reframed video. Per-segment encode + concat-demuxer
    stream-copy — same pattern as compile_clips and silence-tightening."""
    if not plan:
        raise RuntimeError("Empty reframe plan — nothing to compose.")
    seg_paths: list[Path] = []
    list_path: Path | None = None
    try:
        for i, seg in enumerate(plan):
            seg_path = output_path.with_name(
                f"{output_path.stem}_rf{i:03d}.mp4"
            )
            duration = seg["end"] - seg["start"]
            if duration <= 0:
                continue
            if seg["mode"] == "solo":
                x, y, w, h = seg["crop"]
                vf = (
                    f"crop={w}:{h}:{x}:{y},"
                    f"scale={REFRAME_TARGET_W}:{REFRAME_TARGET_H}:flags=lanczos,"
                    f"fps=30,format=yuv420p,setsar=1"
                )
                cmd = [
                    FFMPEG, "-y",
                    "-ss", f"{seg['start']:.3f}",
                    "-i", str(video_path),
                    "-t", f"{duration:.3f}",
                    "-vf", vf,
                    "-af", "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo",
                    *_VIDEO_ENC_ARGS,
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                    "-avoid_negative_ts", "make_zero",
                    str(seg_path),
                ]
            else:  # split — 2-way vertical stack via filter_complex
                (x1, y1, w1, h1), (x2, y2, w2, h2) = seg["crops"][:2]
                half_h = REFRAME_TARGET_H // 2
                fc = (
                    f"[0:v]split=2[v1in][v2in];"
                    f"[v1in]crop={w1}:{h1}:{x1}:{y1},"
                    f"scale={REFRAME_TARGET_W}:{half_h}:flags=lanczos,"
                    f"fps=30,format=yuv420p,setsar=1[v1];"
                    f"[v2in]crop={w2}:{h2}:{x2}:{y2},"
                    f"scale={REFRAME_TARGET_W}:{half_h}:flags=lanczos,"
                    f"fps=30,format=yuv420p,setsar=1[v2];"
                    f"[v1][v2]vstack=inputs=2[outv]"
                )
                cmd = [
                    FFMPEG, "-y",
                    "-ss", f"{seg['start']:.3f}",
                    "-i", str(video_path),
                    "-t", f"{duration:.3f}",
                    "-filter_complex", fc,
                    "-map", "[outv]", "-map", "0:a?",
                    "-af", "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo",
                    *_VIDEO_ENC_ARGS,
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                    "-avoid_negative_ts", "make_zero",
                    str(seg_path),
                ]
            ffmpeg_logger.info(f"Applying reframe segment {i}: {' '.join(cmd)}")
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0 and VIDEO_ENC_NAME == "h264_videotoolbox":
                fallback = list(cmd)
                fb_v_idx = fallback.index("-c:v")
                fallback[fb_v_idx:fb_v_idx + 4] = [
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                ]
                proc = subprocess.run(fallback, capture_output=True, text=True)
            if proc.returncode != 0:
                err = proc.stderr or ""
                diag = [
                    ln for ln in err.splitlines()
                    if any(t in ln for t in ("Error", "error", "Invalid", "failed", "Failed"))
                ]
                tail = "\n".join(diag[-12:]) if diag else err[-1500:]
                raise RuntimeError(
                    f"Reframe segment {i + 1}/{len(plan)} ({seg['mode']}) failed:\n{tail}"
                )
            seg_paths.append(seg_path)

        list_path = output_path.with_name(f"{output_path.stem}_rf_concat.txt")
        list_path.write_text(
            "\n".join(f"file '{p.absolute()}'" for p in seg_paths) + "\n"
        )
        proc = subprocess.run([
            FFMPEG, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            str(output_path),
        ], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Reframe concat-demuxer failed: {proc.stderr[-1500:]}"
            )
    finally:
        for p in seg_paths:
            _safe_unlink(p)
        if list_path is not None:
            _safe_unlink(list_path)


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


# Words/lines this length or shorter never get scaled or hyphen-broken —
# the user explicitly asked that medium-length words like "CONTEMPLATING"
# (13) be rendered at their natural size even if they brush the safe area.
NO_SHRINK_MAX_CHARS = 15


def _fit_line_uniform(line: str, font_size: int, video_w: int,
                       char_factor: float = 0.76, safe_pct: float = 0.84) -> str:
    """Return *line* with a uniform \\fscx/\\fscy scale that fits the safe area.

    Older logic only squished horizontally (\\fscx) which made long words like
    "contemplating" look stretched-thin. Uniform scaling preserves letter
    proportions: a long word just becomes smaller, not deformed. The scale
    floor of 40 % keeps text legible if a word is absurdly long.

    Lines whose visible length is ≤ NO_SHRINK_MAX_CHARS are left at full
    size even if our estimator says they overflow. If the user picked a
    font/size that genuinely runs off-screen, lowering the size is the
    correct fix — silent shrinking just hides it.
    """
    raw_len = _visible_len(line)
    if raw_len == 0 or raw_len <= NO_SHRINK_MAX_CHARS:
        return line
    est_px = raw_len * font_size * char_factor
    max_px = video_w * safe_pct
    if est_px <= max_px:
        return line  # Fits already.
    scale_pct = max(40, int((max_px / est_px) * 100))
    return f"{{\\fscx{scale_pct}\\fscy{scale_pct}}}{line}"


# Back-compat alias — old name kept so external imports / debug grep still work.
_clamp_line_width = _fit_line_uniform


def _hyphenate_oversized_word(part: str, max_chars: int,
                                no_break_max_chars: int = NO_SHRINK_MAX_CHARS) -> str:
    """Split a word that's longer than *max_chars* across multiple lines
    with a trailing hyphen on each non-last fragment.

    Preserves any leading/trailing ASS override tags wrapping the word —
    e.g. ``{\\c&H...}CONTEMPLATING{\\c&H...}`` becomes
    ``{\\c&H...}CONTEM-{\\c&H...}\\N{\\c&H...}PLATING{\\c&H...}`` so the
    karaoke highlight colour stays consistent across both halves.

    If the visible region contains inline tags (rare — happens only when the
    middle of a word changes colour mid-letter), we leave the word alone and
    let the uniform-fit step shrink it as a fallback. That's safer than
    splitting through a tag and corrupting the ASS stream.
    """
    visible_chars = _visible_len(part)
    if visible_chars <= max_chars or visible_chars <= no_break_max_chars:
        return part
    m = re.match(r"^((?:\{[^}]*\})*)(.*?)((?:\{[^}]*\})*)$", part, re.DOTALL)
    if not m:
        return part
    pre, middle, post = m.group(1), m.group(2), m.group(3)
    if re.search(r"\{[^}]*\}", middle):
        return part
    if not middle:
        return part
    # Balanced split: find the minimum line count N where every roughly-even
    # chunk fits within max_chars (allowing 1 char for the trailing hyphen).
    # Naive greedy packing tends to leave a tiny orphan last line.
    word_len = len(middle)
    n = 2
    def chunk_fits(count: int) -> bool:
        for k in range(count):
            start = (word_len * k) // count
            end = (word_len * (k + 1)) // count
            chars = end - start + (1 if k < count - 1 else 0)  # +hyphen
            if chars > max_chars:
                return False
        return True
    while n < word_len and not chunk_fits(n):
        n += 1
    pieces = []
    for k in range(n):
        start = (word_len * k) // n
        end = (word_len * (k + 1)) // n
        chunk = middle[start:end]
        if k < n - 1:
            chunk += "-"
        pieces.append(pre + chunk + post)
    return r"\N".join(pieces)


def _wrap_ass_text(text: str, max_chars: int) -> str:
    """Split ASS-tagged subtitle text at word boundaries to fit max_chars per line.

    Inline ASS tags (e.g. {\\cXXX}) are counted as zero-width so they don't
    trigger early wrapping. Words longer than *max_chars* on their own get
    hyphen-split across lines instead of running off-screen or being shrunk.
    Lines are joined with \\N (hard newline in ASS).
    """
    parts = text.split(" ")
    # Pre-pass: any single word that exceeds max_chars gets hyphenated. We
    # expand the resulting \N segments into separate "parts" so the wrap
    # loop below treats each fragment as its own line candidate.
    expanded: list[str] = []
    for p in parts:
        if _visible_len(p) > max_chars:
            broken = _hyphenate_oversized_word(p, max_chars)
            expanded.extend(broken.split(r"\N"))
        else:
            expanded.append(p)
    parts = expanded
    if all(_visible_len(p) <= max_chars for p in parts) and \
            _visible_len(text) <= max_chars:
        return text
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


_PUNCHWORD_STOPLIST = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "so",
    "because", "as", "at", "by", "for", "from", "in", "into", "of", "off",
    "on", "onto", "out", "over", "to", "up", "with", "without", "is", "am",
    "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "done", "have", "has", "had", "having", "i", "me", "my", "we", "us",
    "our", "you", "your", "he", "him", "his", "she", "her", "it", "its",
    "they", "them", "their", "this", "that", "these", "those", "what",
    "which", "who", "whom", "whose", "when", "where", "why", "how", "not",
    "no", "yes", "there", "here", "than", "too", "very", "just", "also",
    "only", "any", "all", "some", "each", "every", "other", "another",
    "again", "once", "more", "most", "such", "much", "many", "few", "like",
    "go", "goes", "get", "got", "make", "made", "know", "see", "say",
    "said", "can", "could", "will", "would", "should", "may", "might",
    "must", "shall", "let", "yeah", "ok", "okay", "um", "uh", "oh", "well",
})


def _is_punchword_candidate(raw: str) -> bool:
    """Heuristic: would emphasising *raw* feel right? Catches content words
    (long, rare, numerals, proper nouns) and rejects function words.

    The heuristic deliberately runs against the ORIGINAL casing because
    proper nouns lose their cue once we apply ALL CAPS at render time.
    """
    raw = (raw or "").strip()
    if not raw:
        return False
    if any(ch.isdigit() for ch in raw):
        return True
    clean = re.sub(r"[^A-Za-z']", "", raw)
    if not clean:
        return False
    if clean.lower() in _PUNCHWORD_STOPLIST:
        return False
    if raw[:1].isupper() and not raw.isupper():  # mid-sentence proper noun
        return True
    return len(clean) >= 6


def _select_group_punchword_indices(group: list, max_per_group: int = 2) -> set:
    """Pick at most *max_per_group* punchword positions inside *group*.

    Without a per-group cap every other word would light up and the effect
    burns out — emphasising 1–2 words per group is what reads as deliberate.
    Tie-break: longest visible-letter count wins; positional order breaks
    further ties so earlier candidates win.
    """
    candidates = []
    for i, w in enumerate(group):
        text = w.get("word", "")
        if _is_punchword_candidate(text):
            visible_len = len(re.sub(r"[^A-Za-z0-9']", "", text))
            candidates.append((-visible_len, i))
    candidates.sort()  # most-emphatic first
    return {idx for _, idx in candidates[:max_per_group]}


def _smooth_word_timings(words: list,
                          min_active: float = 0.18,
                          max_active: float = 0.90) -> list:
    """Clamp each word's [start, end] highlight window so karaoke moves at a
    comfortable read speed (~150–180 WPM).

    Whisper sometimes emits word durations as short as 60 ms or as long as
    1.2 s — the highlight either flickers or lingers past the natural beat.
    Returns a NEW list (originals untouched):

      - duration < *min_active*: extend the window forward but never past
        the next word's onset. Audio onset is preserved either way.
      - duration > *max_active*: cap the window. Audio still plays past the
        cap; the highlight just resolves earlier so the eye moves on.
    """
    out = []
    for i, w in enumerate(words):
        try:
            s = float(w.get("start", 0))
            e = float(w.get("end", s))
        except (TypeError, ValueError):
            out.append(dict(w))
            continue
        dur = e - s
        if dur < min_active:
            next_start = None
            if i + 1 < len(words):
                try:
                    next_start = float(words[i + 1].get("start"))
                except (TypeError, ValueError):
                    next_start = None
            target = s + min_active
            if next_start is not None:
                target = min(target, next_start)
            if target > e:
                e = target
        elif dur > max_active:
            e = s + max_active
        out.append({**w, "start": s, "end": e})
    return out


def build_ass(words, style: dict, video_w: int, video_h: int, emoji_rules: dict = None, speaker_colors: dict = None, diarization: list = None, headline_banner: str = None) -> str:
    style = _normalize_caption_style(style)
    font = style.get("font_name", "Montserrat Thin Black")
    font_size = int(style.get("font_size", 72))
    primary = hex_to_ass_color(style.get("primary_color", "#FFFFFF"))
    highlight = hex_to_ass_color(style.get("highlight_color", "#FFD60A"))
    accent = hex_to_ass_color(style.get("accent_color", "#FF6B35"))
    outline = hex_to_ass_color(style.get("outline_color", "#000000"))
    outline_w = int(style.get("outline_width", 3))
    shadow = int(style.get("shadow", 1))
    pos_y_pct = float(style.get("position_y", 85))
    all_caps = bool(style.get("all_caps", True))
    group_size = int(style.get("group_size", 3))
    smoothing_on = style.get("smooth_timings", True)
    punchword_on = style.get("punchword_emphasis", True)

    # Smooth karaoke pacing: keep every word's highlight window in a
    # comfortable read band (~180–900 ms). Skipped if the user explicitly
    # turned it off in case they want raw Whisper timings for some reason.
    if smoothing_on:
        words = _smooth_word_timings(words)

    pos_x = video_w // 2
    pos_y = int(video_h * (pos_y_pct / 100.0))

    # Two char-factor estimates, tuned for different jobs:
    #   - WRAP factor (0.62): optimistic — lets more words land on one line.
    #     Used to decide where to insert \N line breaks.
    #   - CLAMP factor: conservative — bumped per font for slabby/wide
    #     typefaces (Alfa Slab One, Bagel Fat One, Sigmar). Used by the
    #     uniform-fit step to shrink any line that would overflow.
    # Safe area is 78% of video width (11% margins each side) — was 84%,
    # but a) wide display fonts were pushing right up against the frame
    # and b) the extra room makes captions less claustrophobic on phones.
    _wrap_char_factor = 0.62
    _wide_fonts = {
        "alfa slab one", "bagel fat one", "sigmar", "passion one",
        "rubik mono one", "archivo black", "bowlby one",
    }
    _clamp_char_factor = 0.96 if font.lower().strip() in _wide_fonts else 0.88
    _safe_pct = 0.78
    # Floor matches NO_SHRINK_MAX_CHARS: phrases of 15 or fewer visible
    # characters never break across lines, regardless of font size or safe
    # area math. Otherwise short groups like "WHY DO I HAVE TO" were getting
    # split unnecessarily when the per-char estimate ran tight.
    max_chars_per_line = max(NO_SHRINK_MAX_CHARS, int((video_w * _safe_pct) / (font_size * _wrap_char_factor)))

    # Normalise emoji rule keys to lowercase alpha-only for robust matching
    normalised_emoji: dict[str, str] = {}
    if emoji_rules:
        for k, v in emoji_rules.items():
            clean_key = re.sub(r"[^a-z]", "", k.lower().strip())
            if clean_key:
                normalised_emoji[clean_key] = v

    banner_style = ""
    if headline_banner:
        banner_size = int(font_size * 0.6)
        banner_style = f"Style: Banner,{font},{banner_size},{primary},{primary},{outline},&H80000000,-1,0,0,0,100,100,0,0,3,0,0,2,20,20,20,1\n"

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
{banner_style}
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

        # Pick at most 2 words per group to wear the accent color. Only
        # affects non-active words — the active (karaoke) word always uses
        # the highlight color so the moving focus stays unambiguous.
        punch_idxs = _select_group_punchword_indices(group) if punchword_on else set()

        for idx, active in enumerate(group):
            pieces = []
            for j, w in enumerate(group):
                word_text = fmt(w["word"])
                word_text = word_text.replace("{", "").replace("}", "")
                
                speaker_color = None
                if diarization and speaker_colors:
                    word_mid = (float(w.get('start', 0)) + float(w.get('end', 0))) / 2
                    for seg in diarization:
                        if seg['start'] <= word_mid <= seg['end']:
                            spk = seg['speaker']
                            if spk in speaker_colors:
                                hc = speaker_colors[spk]
                                if hc.startswith('#'):
                                    hc = hc[1:]
                                if len(hc) == 6:
                                    speaker_color = f"&H{hc[4:6]}{hc[2:4]}{hc[0:2]}&"
                            break

                base_col = speaker_color if speaker_color else primary
                word_prefix = f"{{\\c{speaker_color}}}" if speaker_color else ""
                word_suffix = f"{{\\c{primary}}}" if speaker_color else ""
                
                if j == idx:
                    pieces.append(f"{{\\c{highlight}}}{word_text}{{\\c{base_col}}}")
                elif j in punch_idxs:
                    pieces.append(f"{{\\c{accent}}}{word_text}{{\\c{base_col}}}")
                else:
                    pieces.append(f"{word_prefix}{word_text}{word_suffix}")

            text = " ".join(pieces)

            # Append the group emoji to every dialogue line so it's visible
            # for the full group duration, not just one word's moment.
            if group_emoji:
                text = text + " " + group_emoji

            # Wrap long lines, then uniformly scale each line to fit the
            # safe area. Letters keep their natural proportions — only words
            # that actually overflow get scaled down.
            text = _wrap_ass_text(text, max_chars_per_line)
            text = r"\N".join(
                _fit_line_uniform(ln, font_size, video_w,
                                  _clamp_char_factor, _safe_pct)
                for ln in text.split(r"\N")
            )

            start_ts = ass_timestamp(active["start"])
            end_ts = ass_timestamp(active["end"])

            line = (
                f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,"
                f"{{\\pos({pos_x},{pos_y})}}{text}"
            )
            lines.append(line)

    if headline_banner:
        banner_y = int(video_h * 0.05)
        last_end = float(words[-1].get("end", 0)) if words else 0
        banner_end_ts = _ts_to_ass(last_end)
        lines.append(
            f"Dialogue: 0,0:00:00.00,{banner_end_ts},Banner,,0,0,0,,"
            f"{{\\pos({pos_x},{banner_y})\\bord0\\shad0\\3c&H000000&\\3a&H80&}}{fmt(headline_banner)}"
        )

    return header + "\n".join(lines) + "\n"


def compute_silence_compression(words: list, max_gap: float = 1.0,
                                 target_gap: float = 0.3,
                                 preserved_gap_starts: list | None = None) -> dict:
    """Compute keep-ranges and remapped word timestamps for silence-tightening.

    Walks consecutive word pairs in the transcript. Whenever the gap between
    two words exceeds ``max_gap``, the gap is compressed to ``target_gap``
    by cutting (gap - target_gap) seconds out of the middle, splitting the
    breath room evenly between the two words.

    Gaps whose start time (the previous word's `end`, rounded to 0.1s) is in
    ``preserved_gap_starts`` are NOT cut — they're listed in the returned
    `gaps` payload with `preserved=True` so the UI can show them as opted-out.
    Use this to keep specific dramatic pauses or comedic beats intact.

    A 0.05s safety buffer protects each word boundary so a slightly-late
    Whisper timestamp can't lop off a trailing consonant.

    Returns:
        {
          "ranges": [(src_start, src_end), …]  segments of source to keep,
          "words":  [{word, start, end}, …]    remapped to compressed timeline,
          "gaps":   [ { index, start, end, duration, preserved,
                        context_before, context_after }, … ]   one per
                    above-threshold gap, in source-timeline order,
          "stats":  {original_duration, new_duration, gaps_cut, gaps_total,
                     total_cut},
        }
    """
    BUFFER = 0.05  # protect word boundaries from imprecise timestamps
    CONTEXT_WORDS = 4

    preserved = set()
    if preserved_gap_starts:
        for t in preserved_gap_starts:
            try:
                preserved.add(round(float(t), 1))
            except (TypeError, ValueError):
                continue

    empty_stats = {
        "original_duration": 0.0, "new_duration": 0.0,
        "gaps_cut": 0, "gaps_total": 0, "total_cut": 0.0,
    }
    if not words:
        return {"ranges": [], "words": [], "gaps": [], "stats": empty_stats}

    src_start = max(0.0, float(words[0].get("start", 0)) - BUFFER)
    src_end = float(words[-1].get("end", 0)) + BUFFER

    ranges: list[tuple[float, float]] = []
    new_words: list[dict] = []
    gaps_detail: list[dict] = []
    keep_start = src_start
    # cumulative_drop counts EVERY second of the original timeline that
    # doesn't make it into the tightened output — which includes the
    # initial src_start offset (any silence before the first spoken word
    # that's outside the BUFFER), not just the gap-cuts that come later.
    # Without seeding it here, every remapped word lands `src_start`
    # seconds too late and the burned captions drift behind the video.
    cumulative_drop = src_start
    gaps_cut = 0
    half = target_gap / 2.0

    def _ctx_before(i: int) -> str:
        start = max(0, i - CONTEXT_WORDS)
        return " ".join(str(words[j].get("word", "")).strip()
                        for j in range(start, i)).strip()

    def _ctx_after(i: int) -> str:
        end = min(len(words), i + CONTEXT_WORDS)
        return " ".join(str(words[j].get("word", "")).strip()
                        for j in range(i, end)).strip()

    for i, w in enumerate(words):
        try:
            ws = float(w.get("start", 0))
            we = float(w.get("end", 0))
        except (TypeError, ValueError):
            continue
        if i > 0:
            prev = words[i - 1]
            try:
                pe = float(prev.get("end", 0))
            except (TypeError, ValueError):
                pe = ws
            gap = ws - pe
            if gap > max_gap:
                gap_start_key = round(pe, 1)
                is_preserved = gap_start_key in preserved
                gaps_detail.append({
                    "index": len(gaps_detail),
                    "start": pe,
                    "end": ws,
                    "duration": gap,
                    "preserved": is_preserved,
                    "context_before": _ctx_before(i),
                    "context_after": _ctx_after(i),
                })
                if not is_preserved:
                    # Cut the middle of the gap; leave half of target_gap on each side.
                    keep_end = max(pe + BUFFER, pe + half)
                    next_start = min(ws - BUFFER, ws - half)
                    if next_start > keep_end + 0.2:
                        ranges.append((keep_start, keep_end))
                        cumulative_drop += (next_start - keep_end)
                        keep_start = next_start
                        gaps_cut += 1
        new_words.append({
            "word": w.get("word", ""),
            "start": ws - cumulative_drop,
            "end": we - cumulative_drop,
        })

    ranges.append((keep_start, src_end))

    original_duration = src_end - src_start
    new_duration = sum(b - a for a, b in ranges)

    return {
        "ranges": ranges,
        "words": new_words,
        "gaps": gaps_detail,
        "stats": {
            "original_duration": original_duration,
            "new_duration": new_duration,
            "gaps_cut": gaps_cut,
            "gaps_total": len(gaps_detail),
            "total_cut": original_duration - new_duration,
        },
    }


def apply_silence_tightening(video_path: Path, ranges: list,
                               output_path: Path,
                               crossfade_ms: int = 0) -> None:
    """Trim source video to the given keep-ranges and concatenate them.

    Single ffmpeg pass via filter_complex (atrim/trim + concat) so the
    cuts land at frame-accurate positions. Re-encodes for accurate seek;
    stream-copy concat would snap cuts to keyframes and shift the audio.

    If *crossfade_ms* > 0, applies a brief fade-out at the end of each
    segment (except the last) and a fade-in at the start of each segment
    (except the first). Total length is preserved (unlike acrossfade,
    which overlaps segments) so audio stays in lockstep with the
    hard-cut video. Practical range: 20–60 ms.
    """
    if not ranges:
        raise RuntimeError("No keep-ranges supplied to silence-tightening.")

    if len(ranges) == 1:
        a, b = ranges[0]
        cmd = [
            FFMPEG, "-y",
            "-ss", f"{a:.3f}",
            "-i", str(video_path),
            "-t", f"{(b - a):.3f}",
            "-fflags", "+genpts",
            *_VIDEO_ENC_ARGS,
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-vsync", "cfr",
            "-avoid_negative_ts", "make_zero",
            # No +faststart here: this is an intermediate file consumed by
            # the burn pipeline. ffmpeg 8.x's faststart rewrite was failing
            # on the second pass and killing the whole render.
            str(output_path),
        ]
    else:
        # Multi-range tightening. We *used* to do this in a single ffmpeg pass
        # via filter_complex (trim+concat filter), but ffmpeg 8.x's concat
        # filter aborts with "Failed to configure output pad" / EINVAL on
        # multi-segment trims even when every output param is normalised.
        # The reliable pattern is the same one /compile-clips uses: render
        # each kept range to its own file at uniform encoder params, then
        # stitch them with the concat *demuxer* (stream copy, no re-encode).
        fade_dur = max(0.0, crossfade_ms / 1000.0)
        seg_paths: list[Path] = []
        list_path: Path | None = None
        n = len(ranges)
        try:
            for i, (a, b) in enumerate(ranges):
                seg_dur = b - a
                seg_path = output_path.with_name(
                    f"{output_path.stem}_seg{i:03d}.mp4"
                )
                # Per-segment audio chain — we apply the crossfades here so
                # the concat demuxer never has to touch audio samples.
                a_filter = (
                    "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
                )
                if fade_dur > 0 and seg_dur > 2 * fade_dur:
                    fades = []
                    if i > 0:
                        fades.append(f"afade=t=in:st=0:d={fade_dur:.3f}")
                    if i < n - 1:
                        fades.append(
                            f"afade=t=out:st={max(0.0, seg_dur - fade_dur):.3f}:d={fade_dur:.3f}"
                        )
                    if fades:
                        a_filter = ",".join(fades) + "," + a_filter
                seg_cmd = [
                    FFMPEG, "-y",
                    "-ss", f"{a:.3f}",
                    "-i", str(video_path),
                    "-t", f"{seg_dur:.3f}",
                    "-vf", "fps=30,format=yuv420p,setsar=1",
                    "-af", a_filter,
                    *_VIDEO_ENC_ARGS,
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                    "-avoid_negative_ts", "make_zero",
                    str(seg_path),
                ]
                proc = subprocess.run(seg_cmd, capture_output=True, text=True)
                if proc.returncode != 0 and VIDEO_ENC_NAME == "h264_videotoolbox":
                    fallback = list(seg_cmd)
                    fb_v_idx = fallback.index("-c:v")
                    fallback[fb_v_idx:fb_v_idx + 4] = [
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20"
                    ]
                    proc = subprocess.run(fallback, capture_output=True, text=True)
                if proc.returncode != 0:
                    err = proc.stderr or ""
                    diag = [
                        ln for ln in err.splitlines()
                        if any(t in ln for t in ("Error", "error", "Invalid", "failed", "Failed"))
                    ]
                    details = "\n".join(diag[-15:]) if diag else err[-2000:]
                    raise RuntimeError(
                        f"Silence-tightening segment {i + 1}/{n} failed:\n{details}"
                    )
                seg_paths.append(seg_path)

            # Stitch with the demuxer (stream copy). All segments share
            # identical encoder params so this is fast and lossless.
            list_path = output_path.with_name(f"{output_path.stem}_concat.txt")
            list_path.write_text(
                "\n".join(f"file '{p.absolute()}'" for p in seg_paths) + "\n"
            )
            concat_cmd = [
                FFMPEG, "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_path),
                "-c", "copy",
                str(output_path),
            ]
            proc = subprocess.run(concat_cmd, capture_output=True, text=True)
        finally:
            for p in seg_paths:
                _safe_unlink(p)
            if list_path is not None:
                _safe_unlink(list_path)
        if proc.returncode != 0:
            err = proc.stderr or ""
            diag = [
                ln for ln in err.splitlines()
                if any(t in ln for t in ("Error", "error", "Invalid", "failed", "Failed"))
            ]
            details = "\n".join(diag[-15:]) if diag else err[-2000:]
            raise RuntimeError(f"Silence-tightening concat-demuxer failed:\n{details}")
        return

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and VIDEO_ENC_NAME == "h264_videotoolbox":
        # Hardware encoder can choke on certain inputs; retry libx264.
        fallback = [a for a in cmd]
        # Replace the videotoolbox args with libx264 args
        fb_v_idx = fallback.index("-c:v") if "-c:v" in fallback else None
        if fb_v_idx is not None:
            fallback[fb_v_idx + 1] = "libx264"
            # Also tweak preset/crf to reasonable defaults
            try:
                fallback[fb_v_idx:fb_v_idx + 4] = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
            except Exception:
                pass
        proc = subprocess.run(fallback, capture_output=True, text=True)
    if proc.returncode != 0:
        # Pull out the lines that actually describe the failure: anything
        # tagged with [error], "Error", "Invalid", or "failed" — trailing
        # libx264 stats are noise. Falls back to the last 3000 chars if
        # nothing matches so we never throw away the real reason.
        err = proc.stderr or ""
        signal_hint = ""
        if proc.returncode < 0:
            signal_hint = f" (process killed by signal {-proc.returncode})"
        diag = [
            ln for ln in err.splitlines()
            if any(t in ln for t in ("Error", "error", "Invalid", "failed", "Failed"))
        ]
        details = "\n".join(diag[-25:]) if diag else err[-3000:]
        raise RuntimeError(
            f"Silence-tightening ffmpeg failed (returncode={proc.returncode}{signal_hint}).\n"
            f"Command: ffmpeg ... {output_path.name}\n"
            f"Diagnostics:\n{details}"
        )


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
    cmd = [FFMPEG, "-y", "-i", str(video_path)]
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
            [FFMPEG, "-y",
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
        [FFMPEG, "-y", "-i", str(video_path),
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
            [FFMPEG, "-y", "-i", str(cleaned),
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
            [FFMPEG, "-hide_banner", "-encoders"],
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
        [FFMPEG, "-y", "-i", str(video_path),
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
            [FFMPEG, "-y", "-i", str(cleaned_path),
             "-c:a", "aac", "-b:a", "192k", str(output_path)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"AAC re-encode failed: {proc.stderr[-1500:]}")
    finally:
        _safe_unlink(wav_path)
        _safe_unlink(cleaned_path)


_POSTPROCESS_KEYS = {"offset_seconds", "wet_mix", "output_gain_db", "post_filters"}


def _audio_cache_key(audio: dict, style: dict | None = None) -> str:
    """Stable hash of the audio settings that REQUIRE re-running the AI provider.

    Local post-process knobs (wet/dry blend, output gain, post-filters, sync
    offset) are excluded so the user can iterate on them without burning
    credits — they get re-applied on every render via _apply_isolation_postprocess.

    If silence-tightening is enabled in *style*, its parameters are folded
    into the key. Tightening produces a different source audio (cuts removed,
    timeline compressed) so cached audio from a non-tightened or differently-
    tightened render must be invalidated.
    """
    if not audio or not audio.get("provider"):
        return ""
    payload = {k: v for k, v in audio.items() if k not in _POSTPROCESS_KEYS}
    if style and style.get("tighten_silences", {}).get("enabled"):
        ts = style["tighten_silences"]
        payload["__tighten"] = {
            "max_gap": ts.get("max_gap"),
            "target_gap": ts.get("target_gap"),
            "crossfade": bool(ts.get("crossfade")),
            "preserved": sorted(ts.get("preserved_gap_starts") or []),
        }
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
    cmd: list[str] = [FFMPEG, "-y"]

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
    punch_cfg: dict | None = None,
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

    if punch_cfg and punch_cfg.get("enabled"):
        try:
            src_w, src_h = get_video_dimensions(video_path)
            pz = _tl_punch_zoom_filter(punch_cfg, src_w, src_h)
            if pz:
                vf_parts.append(pz)
        except Exception:
            pass

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

    cmd: list[str] = [FFMPEG, "-y", "-i", str(video_path)]
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
        FFMPEG, "-y",
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


def _burn_cache_key(style: dict, words: list, emoji_rules: dict, video_path: Path, job_id: str = "") -> str:
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
    if job_id:
        reframe_cache = UPLOAD_DIR / f"{job_id}_reframe.json"
        if reframe_cache.exists():
            try:
                rdata = json.loads(reframe_cache.read_text(encoding="utf-8"))
                payload["swap_speaker_voices"] = bool(rdata.get("swap_speaker_voices"))
                payload["reframe_mtime"] = reframe_cache.stat().st_mtime
            except Exception:
                pass
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---- AI clip suggestions (Gemini) ----

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _format_transcript_for_llm(words: list, max_chars_per_chunk: int = 220) -> str:
    """Group word-timestamp records into readable [mm:ss] lines.

    Breaks at natural pauses (>0.7s gap) or when a chunk reaches
    ``max_chars_per_chunk``. The LLM uses these timestamps to choose clip
    boundaries that fall on sentence-ish edges.
    """
    if not words:
        return ""
    chunks: list[str] = []
    current_words: list[str] = []
    current_start = float(words[0].get("start", 0))

    def _ts(t: float) -> str:
        mm = int(t // 60)
        ss = int(t % 60)
        return f"[{mm:02d}:{ss:02d}]"

    for i, w in enumerate(words):
        current_words.append(str(w.get("word", "")).strip())
        end = float(w.get("end", 0))
        next_gap = (
            float(words[i + 1].get("start", end)) - end
            if i + 1 < len(words) else 999.0
        )
        text_so_far = " ".join(current_words).strip()
        if (len(text_so_far) >= max_chars_per_chunk or next_gap > 0.7) and text_so_far:
            chunks.append(f"{_ts(current_start)} {text_so_far}")
            current_words = []
            if i + 1 < len(words):
                current_start = float(words[i + 1].get("start", end))
    if current_words:
        chunks.append(f"{_ts(current_start)} {' '.join(current_words).strip()}")
    return "\n".join(chunks)


_FORMAT_RUBRICS = {
    "comedy": (
        "Format: COMEDY.\n"
        "- Look for setup → tension → punchline. The punchline is the payoff.\n"
        "- Rule of three: two normal items then a twist. Identify these patterns.\n"
        "- Callbacks: a line that pays off something earlier. These travel well as clips.\n"
        "- Reaction beats matter — the line right after the joke can land harder than the joke.\n"
        "- AVOID slow setups; if a joke needs 30s of context, don't pick it."
    ),
    "interview": (
        "Format: INTERVIEW.\n"
        "- Lead with the ANSWER, not the question. Hook should be the strongest, most quotable line.\n"
        "- Vulnerability beats: moments of admission, hesitation, surprise, raw honesty.\n"
        "- Quote-worthiness: a line that stands alone without setup.\n"
        "- A whole arc inside 60s: one strong claim, evidence/example, takeaway."
    ),
    "event_recap": (
        "Format: EVENT RECAP.\n"
        "- Front-load the peak. Don't save the best moment — open with it.\n"
        "- Three-beat arc: anticipation → climax → reaction. The reaction often beats the climax.\n"
        "- Audience energy: cheers, gasps, laughter spikes are gold. Note when the transcript shows audience response.\n"
        "- Visual/spatial language ('I couldn't believe what I saw') is good for hooks even without B-roll."
    ),
    "vendor_interview": (
        "This is a vendor interview at a festival or market. "
        "Find clips that showcase: (1) The vendor's origin/founder story — why they started their business, "
        "(2) Product spotlight — what makes their product unique or how it's made, "
        "(3) Business advice or hustle tips for aspiring vendors, "
        "(4) Authentic festival vibe or funny banter moments. "
        "Each clip should be self-contained and feel like a mini-story. "
        "Prioritize emotional resonance and shareability."
    ),
    "auto": (
        "Format: AUTO. Detect the dominant content type from the transcript "
        "(comedy / interview / event recap / monologue / tutorial) and apply "
        "the relevant rubric. State your detected type in the reason field."
    ),
}


def _build_clip_suggestion_prompt(transcript: str, format_type: str,
                                   target_durations: list, num_clips: int,
                                   avoid_ranges: list | None = None) -> str:
    rubric = _FORMAT_RUBRICS.get(format_type, _FORMAT_RUBRICS["auto"])
    # 3s is the hard floor — anything shorter isn't a clip, it's a flicker.
    sorted_durations = sorted(set(int(d) for d in target_durations if int(d) >= 3))
    if not sorted_durations:
        sorted_durations = [60]
    durations_str = ", ".join(f"{d}s" for d in sorted_durations)
    longest = max(sorted_durations)
    avoid_block = ""
    if avoid_ranges:
        formatted = "; ".join(
            f"[{float(r[0]):.1f}s–{float(r[1]):.1f}s]"
            for r in avoid_ranges if len(r) == 2
        )
        if formatted:
            avoid_block = (
                f"\nAVOID these source-time ranges — they were already shown to "
                f"the user and rejected. Pick DIFFERENT moments that don't "
                f"materially overlap any of: {formatted}\n"
            )
    return f"""You are an expert short-form video editor. Identify the most engaging segments in the transcript below for clipping into stand-alone short-form videos.{avoid_block}

{rubric}

Universal rubric (applies to every format):
- HOOK (first 3-5s of the clip): a strong opener — quotable line, surprising claim, open loop, or pattern interrupt. Within 5 seconds the viewer must know why they should keep watching. The hook can be the literal start of the clip OR a line from later you'd want pulled forward.
- BODY: retention engine — something interesting (new angle, escalation, surprise, emotional shift) every 7-10s.
- PAYOFF (last 5-10s): punchline, conclusion, emotional peak, or callback. Don't trail off mid-thought.

Length targets — STRICT REQUIREMENT: every clip's total duration
(end_time − start_time) MUST match one of these values: {durations_str}.
Tolerance is ±2 seconds; do NOT return clips outside these lengths.
Pick the target duration that best fits each moment's content arc —
shorter for hooks and punchlines, longer for stories and explanations.
You may reuse the same duration multiple times if the source has several
strong moments at that length.

Pick clips that BEGIN AND END at natural sentence boundaries — match the
[mm:ss] timestamps on the transcript lines.

Overlapping clips are explicitly allowed and useful: a 5s hook can live
INSIDE a 30s clip of the same moment if both stand alone. The user wants
to compare and pick.

Return UP TO {num_clips} clips, ranked by engagement potential (best first).
Quality > quantity: if the source only has 4 truly strong moments, return
4 — do NOT pad to {num_clips}. The longest clip should not exceed {longest + 10}s.

For each clip output:
- start_time (float seconds, must equal the start of one of the transcript lines)
- end_time (float seconds)
- hook_start_time, hook_end_time (3-5s window inside the clip; the strongest opening moment)
- hook_quote (the literal line of dialogue serving as the hook)
- title (3-7 word descriptive title)
- reason (1-2 sentences: WHY this segment works under the rubric above)
- viral_score (int 0-100)
- category (string: founder_story/product_spotlight/business_advice/festival_vibe)
- suggested_headline (string)

Output STRICT JSON only, no prose, no markdown fences. Schema:
{{
  "clips": [
    {{
      "start_time": <float>,
      "end_time": <float>,
      "hook_start_time": <float>,
      "hook_end_time": <float>,
      "hook_quote": "<string>",
      "title": "<string>",
      "reason": "<string>",
      "viral_score": <int>,
      "category": "<string>",
      "suggested_headline": "<string>"
    }}
  ]
}}

Transcript (each line begins with [mm:ss] indicating the start time of that sentence):
{transcript}
"""


def _detect_overlap_groups(clips: list, threshold: float = 0.90) -> list:
    """Mark clips that substantially overlap each other with a shared group_id.

    Two clips are in the same group if the overlap region covers at least
    *threshold* of the SHORTER clip's duration. Singleton clips get
    ``group_id = None`` so the UI knows to leave them un-tinted.
    Operates on the list in place and returns it.
    """
    n = len(clips)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        si = float(clips[i]["start_time"])
        ei = float(clips[i]["end_time"])
        for j in range(i + 1, n):
            sj = float(clips[j]["start_time"])
            ej = float(clips[j]["end_time"])
            ov_start = max(si, sj)
            ov_end = min(ei, ej)
            if ov_end <= ov_start:
                continue
            overlap = ov_end - ov_start
            shorter = min(ei - si, ej - sj)
            if shorter > 0 and (overlap / shorter) >= threshold:
                union(i, j)

    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(i)

    next_group = 0
    for members in buckets.values():
        if len(members) >= 2:
            for idx in members:
                clips[idx]["group_id"] = next_group
            next_group += 1
        else:
            clips[members[0]]["group_id"] = None
    return clips


def _snap_clip_to_target_durations(clips: list, words: list,
                                    target_durations: list[int]) -> list:
    """Force each clip's duration to one of *target_durations* (seconds).

    For each clip we pick the target closest to its current duration, then
    snap end_time to the nearest word boundary near ``start + target``. The
    hook range is clamped to fit inside the new clip window. start_time is
    left alone so Gemini's chosen hook moment stays anchored.
    """
    if not clips or not words or not target_durations:
        return clips
    valid_targets = [int(d) for d in target_durations if int(d) >= 3]
    if not valid_targets:
        return clips
    boundaries = sorted({float(w.get("end", 0)) for w in words if w.get("end") is not None})
    if not boundaries:
        return clips
    src_max_t = boundaries[-1]
    for c in clips:
        start = float(c["start_time"])
        end = float(c["end_time"])
        actual = end - start
        target = min(valid_targets, key=lambda d: abs(d - actual))
        ideal_end = min(start + target, src_max_t)
        snapped_end = min(boundaries, key=lambda b: abs(b - ideal_end))
        if snapped_end - start < 3:
            continue
        c["end_time"] = snapped_end
        hs = float(c.get("hook_start_time", start))
        he = float(c.get("hook_end_time", min(snapped_end, start + 5)))
        if hs < start:
            hs = start
        if he > snapped_end:
            he = snapped_end
        if he <= hs:
            he = min(snapped_end, hs + 5)
        c["hook_start_time"] = hs
        c["hook_end_time"] = he
    return clips


# ---- AI effect placement -------------------------------------------------
# Effects are currently whole-clip flags, but a suggestion is only useful if it
# says *when*. These carry in/out times so the UI can split a clip at those
# boundaries and enable the effect on the middle piece — the same split the
# editor already supports by hand.
_EFFECT_LIMITS = {
    # type: (min duration, max duration)
    "punch_zoom": (0.6, 3.0),    # quick emphasis push-in
    "ken_burns": (3.0, 12.0),    # slow drift over a longer stretch
    "split_screen": (1.0, 15.0),  # both speakers framed together
}


def _build_effect_suggestion_prompt(transcript: str, total: float,
                                    max_effects: int) -> str:
    """Ask Gemini where camera moves would earn their keep."""
    return f"""You are an expert short-form video editor deciding where camera moves belong in a talking-head edit. The transcript below has [mm:ss] timestamps. The video is {total:.1f} seconds long.

Choose at most {max_effects} moments. Fewer is better — a move that isn't motivated is worse than no move at all. Never cover the whole video; these are accents.

Effects you may place:
- "punch_zoom": a fast push-in for emphasis. Use on a punchline, a strong claim, a reaction, a name drop, or an emotional beat. Duration {_EFFECT_LIMITS['punch_zoom'][0]}-{_EFFECT_LIMITS['punch_zoom'][1]}s, tight around the line itself.
- "ken_burns": a slow drift that keeps a static shot alive. Use on longer explanation or storytelling stretches where nothing else is moving. Duration {_EFFECT_LIMITS['ken_burns'][0]}-{_EFFECT_LIMITS['ken_burns'][1]}s.

Rules:
- Effects must not overlap each other.
- Anchor each one to what is actually said at that timestamp; quote it.
- Keep every time within 0 and {total:.1f} seconds.
- intensity is "low", "med" or "strong". Reserve "strong" for the single biggest beat.
- For ken_burns, direction is "in" (push in) or "out" (pull back).

Return JSON in exactly this shape:
{{
  "effects": [
    {{
      "type": "punch_zoom",
      "start_time": 12.4,
      "end_time": 13.9,
      "intensity": "med",
      "direction": "in",
      "quote": "the exact words being emphasised",
      "reason": "why this moment earns a camera move"
    }}
  ]
}}

TRANSCRIPT:
{transcript}
"""


def _overlap_split_suggestions(job_id: str, total: float) -> list[dict]:
    """Split-screen ranges taken straight from diarization.

    When two speakers talk over each other, framing both is the obvious call,
    and the reframe analysis already computed exactly those windows — so this
    needs no model, just the cached overlaps.
    """
    cache = UPLOAD_DIR / f"{job_id}_reframe.json"
    if not cache.exists():
        return []
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    lo, hi = _EFFECT_LIMITS["split_screen"]
    out: list[dict] = []
    for ov in (data.get("overlaps") or []):
        try:
            s = max(0.0, float(ov.get("start")))
            e = min(total, float(ov.get("end")))
        except (TypeError, ValueError):
            continue
        if e - s < lo:
            continue
        out.append({
            "type": "split_screen",
            "start_time": s,
            "end_time": min(e, s + hi),
            "intensity": "med",
            "direction": "in",
            "quote": "",
            "reason": "Both speakers talking at once — frame them together.",
            "source": "diarization",
        })
    return out


def _face_anchor_at(job_id: str, t: float) -> dict | None:
    """Normalised (x, y) of the most prominent face nearest time *t*.

    Zooming into the middle of the frame is only right by accident; the
    reframe analysis already tracked faces, so a punch can push toward the
    person actually talking. Returns None when no analysis has been run.
    """
    cache = UPLOAD_DIR / f"{job_id}_reframe.json"
    if not cache.exists():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    found = _face_samples_at(data.get("faces") or [], t)
    if not found:
        return None
    # Biggest face wins — the speaker is normally nearest the camera.
    face = max(found, key=lambda f: float(f.get("w", 0)) * float(f.get("h", 0)))
    try:
        return {"x": round(float(face["cx"]), 4), "y": round(float(face["cy"]), 4)}
    except (KeyError, TypeError, ValueError):
        return None


def _sanitize_effect_suggestions(raw: list, total: float) -> list[dict]:
    """Clamp to the video, enforce per-type durations, drop overlaps."""
    cleaned: list[dict] = []
    for e in raw or []:
        kind = str(e.get("type", "")).strip()
        if kind not in _EFFECT_LIMITS:
            continue
        try:
            s = float(e.get("start_time"))
            t = float(e.get("end_time"))
        except (TypeError, ValueError):
            continue
        s = max(0.0, min(s, total))
        t = max(0.0, min(t, total))
        if t <= s:
            continue
        lo, hi = _EFFECT_LIMITS[kind]
        if t - s < lo:
            t = min(total, s + lo)      # too short to read: widen to the floor
            if t - s < lo:
                continue                 # not enough video left
        if t - s > hi:
            t = s + hi                   # too long to be an accent: trim
        intensity = str(e.get("intensity", "med")).lower()
        direction = str(e.get("direction", "in")).lower()
        cleaned.append({
            "type": kind,
            "start_time": round(s, 3),
            "end_time": round(t, 3),
            "intensity": intensity if intensity in ("low", "med", "strong") else "med",
            "direction": direction if direction in ("in", "out") else "in",
            "quote": str(e.get("quote", ""))[:300],
            "reason": str(e.get("reason", ""))[:500],
            "source": str(e.get("source", "gemini")),
        })

    # Earliest first, then drop anything that collides with a kept effect —
    # two camera moves running at once read as a glitch, not as emphasis.
    cleaned.sort(key=lambda c: c["start_time"])
    kept: list[dict] = []
    for c in cleaned:
        if any(c["start_time"] < k["end_time"] and k["start_time"] < c["end_time"]
               for k in kept):
            continue
        kept.append(c)
    return kept


def _gemini_generate_clip_suggestions(prompt: str) -> dict:
    """Call Gemini and parse a JSON-mode response. Raises on failure."""
    import requests

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("Gemini is not configured (GEMINI_API_KEY not set).")

    url = f"{_GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent?key={api_key}"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.6,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(url, json=body, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini API failed ({resp.status_code}): {resp.text[:500]}")
    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Gemini returned an unexpected response shape: {e}")
    try:
        parsed = json.loads(text)
        ai_logger.info(f"Gemini prompt len: {len(prompt)}, response len: {len(text)}")
        return parsed
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini returned non-JSON output: {e}; first 500 chars: {text[:500]}")


# ---- Background workers ----

def transcribe_job(job_id: str, video_path: Path, pre_clean: bool = False):
    """Transcribe only — stores words and sets status to awaiting_edit."""
    try:
        jobs[job_id]["status"] = "transcribing"
        jobs[job_id]["progress"] = 30
        jobs[job_id]["error"] = None
        probe = _probe_media_streams(video_path)
        jobs[job_id]["media_info"] = probe
        _db_save_job(job_id)
        # iPhone HEVC / HDR MOVs often black-screen in Chrome on Windows until we
        # have an H.264 edit proxy. Start that early for HEVC only; otherwise wait
        # until after Whisper so we don't steal CPU from transcription.
        proxy_started_early = False
        if probe.get("is_hevc") or (video_path.suffix.lower() == ".mov" and probe.get("has_video")):
            threading.Thread(
                target=build_edit_proxy, args=(job_id, video_path), daemon=True
            ).start()
            proxy_started_early = True
            print(f"[proxy] {job_id} early start (hevc/mov preview)", flush=True)
        words = transcribe(video_path, pre_clean=pre_clean, job_id=job_id)
        if not words:
            raise RuntimeError("No speech detected in the video.")
        jobs[job_id]["words"] = words
        jobs[job_id]["status"] = "awaiting_edit"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["error"] = None
        _db_save_job(job_id)
        if not proxy_started_early:
            threading.Thread(
                target=build_edit_proxy, args=(job_id, video_path), daemon=True
            ).start()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[transcribe_job] {job_id} failed:\n{tb}", flush=True)
        jobs[job_id]["status"] = "error"
        # Keep the short message for the UI; full traceback goes to the server log.
        jobs[job_id]["error"] = str(e) or e.__class__.__name__
        jobs[job_id]["completed_at"] = time.time()
        try:
            jobs[job_id]["media_info"] = _probe_media_streams(video_path)
        except Exception:
            pass
        _db_save_job(job_id)
        # Keep the upload on disk so the user can retry Re-transcribe without
        # re-uploading. Orphan cleanup can happen later via job delete.
        # (Previously we deleted the source here, which made retries impossible.)


def analyze_reframe_job(
    job_id: str,
    video_path: Path,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> None:
    """Background worker that runs diarization + face tracking and caches
    the result to uploads/<job>_reframe.json. Job status is reported via
    the existing in-memory job dict so the UI's status poll picks it up.
    """
    def _progress(pct: int, status: str) -> None:
        jobs[job_id]["status"] = status
        jobs[job_id]["progress"] = int(pct)
        try:
            _db_save_job(job_id)
        except Exception:
            pass

    try:
        jobs[job_id]["reframe_error"] = None
        jobs[job_id]["reframe_ready"] = False
        prev_err = jobs[job_id].get("error") or ""
        if "Reframe analysis failed" in prev_err or "reframe" in prev_err.lower():
            jobs[job_id]["error"] = None
        _progress(10, "analysing speakers")
        result = analyze_reframe(
            video_path,
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            progress_cb=_progress,
        )
        cache_path = UPLOAD_DIR / f"{job_id}_reframe.json"
        cache_path.write_text(json.dumps(result), encoding="utf-8")
        n_spk = _stamp_job_speakers(job_id, result.get("diarization") or [])
        device = (result.get("stats") or {}).get("diarization_device") or "?"
        ai_logger.info(
            f"[{job_id}] Reframe analysis done: {result['stats']['speaker_count']} speakers, "
            f"{result['stats']['face_samples']} face samples, {n_spk} words labeled "
            f"(device={device})"
        )
        jobs[job_id]["status"] = "awaiting_edit"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["reframe_ready"] = True
        jobs[job_id]["reframe_error"] = None
        # Only clear job.error if it was a prior reframe failure.
        prev_err = jobs[job_id].get("error") or ""
        if "Reframe analysis failed" in prev_err or "reframe" in prev_err.lower():
            jobs[job_id]["error"] = None
        _db_save_job(job_id)
    except Exception as e:
        jobs[job_id]["status"] = "awaiting_edit"
        jobs[job_id]["reframe_ready"] = False
        jobs[job_id]["reframe_error"] = f"Reframe analysis failed: {e}"
        # Keep transcription/job.error intact — Analyze failures are separate.
        _db_save_job(job_id)
        ai_logger.exception(f"[{job_id}] Reframe analysis failed: {e}")


def retranscribe_job(job_id: str, video_path: Path, pre_clean: bool = False):
    """Re-run Whisper on an existing job's video and overwrite its words.

    Used by /retranscribe — handy when a job's stored words drift out of sync
    with its video file (e.g. the job was a clip of a previously-rendered
    video, so the original timestamps don't match the current frames).

    Differences vs transcribe_job:
      - Existing words/style/audio/emoji_rules are preserved (not cleared).
      - On failure we DON'T delete the source video — the user's intent is to
        recover, not start over.
      - burn_cache_key is invalidated so the next render rebuilds against
        the fresh transcript.
    """
    try:
        jobs[job_id]["status"] = "re-transcribing"
        jobs[job_id]["progress"] = 30
        jobs[job_id]["error"] = None
        _db_save_job(job_id)
        words = transcribe(video_path, pre_clean=pre_clean, job_id=job_id)
        if not words:
            raise RuntimeError("No speech detected in the video.")
        jobs[job_id]["words"] = words
        jobs[job_id]["burn_cache_key"] = None
        jobs[job_id]["status"] = "awaiting_edit"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["error"] = None
        _db_save_job(job_id)
        threading.Thread(
            target=build_edit_proxy, args=(job_id, video_path), daemon=True
        ).start()
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = f"Re-transcribe failed: {e}"
        try:
            jobs[job_id]["media_info"] = _probe_media_streams(video_path)
        except Exception:
            pass
        _db_save_job(job_id)


def render_job(job_id: str, video_path: Path, words: list, style: dict, audio: dict, emoji_rules: dict):
    """Build ASS, optionally enhance audio, then burn subtitles."""
    ass_path: Path | None = None
    enhanced_audio_path: Path | None = None
    tight_video_path: Path | None = None
    try:
        # ---- Optional silence-tightening pre-step ----
        # If style.tighten_silences is enabled, compress long pauses in the
        # source video FIRST. The downstream pipeline (subtitle burn, audio
        # enhancement) then operates on the tightened video and remapped
        # word timestamps so subtitles stay in sync with the cuts.
        tight_settings = (style or {}).get("tighten_silences") or {}
        if tight_settings.get("enabled"):
            try:
                max_gap = float(tight_settings.get("max_gap", 1.0))
            except (TypeError, ValueError):
                max_gap = 1.0
            try:
                target_gap = float(tight_settings.get("target_gap", 0.3))
            except (TypeError, ValueError):
                target_gap = 0.3
            preserved_starts = tight_settings.get("preserved_gap_starts") or []
            comp = compute_silence_compression(
                words,
                max_gap=max_gap,
                target_gap=target_gap,
                preserved_gap_starts=preserved_starts,
            )
            if comp["stats"]["gaps_cut"] > 0:
                jobs[job_id]["status"] = "tightening silences"
                jobs[job_id]["progress"] = 50
                _db_save_job(job_id)
                tight_video_path = UPLOAD_DIR / f"{job_id}_tight.mp4"
                # Default 30ms crossfade if user enabled it on the panel.
                crossfade_ms = 30 if tight_settings.get("crossfade") else 0
                apply_silence_tightening(
                    video_path, comp["ranges"], tight_video_path,
                    crossfade_ms=crossfade_ms,
                )
                # Replace the source so everything downstream uses the
                # tightened version. Words are also remapped to the new
                # timeline so subtitle timing stays correct.
                video_path = tight_video_path
                words = comp["words"]

        # ---- Optional interview-reframe pre-step ----
        # If style.reframe.enabled and the analysis cache exists, produce a
        # 9:16 reframed video and feed that downstream. Word timestamps are
        # NOT remapped (timeline is unchanged), only the frame composition.
        reframe_video_path: Path | None = None
        reframe_settings = (style or {}).get("reframe") or {}
        if reframe_settings.get("enabled"):
            cache_path = UPLOAD_DIR / f"{job_id}_reframe.json"
            if not cache_path.exists():
                jobs[job_id]["error"] = (
                    "Reframe enabled but no analysis cache found — "
                    "click 'Analyze speakers + faces' first."
                )
                _db_save_job(job_id)
            else:
                jobs[job_id]["status"] = "reframing video"
                jobs[job_id]["progress"] = 52
                _db_save_job(job_id)
                try:
                    reframe_data = json.loads(cache_path.read_text(encoding="utf-8"))
                    diarization_data = reframe_data.get('diarization')
                    src_w, src_h = get_video_dimensions(video_path)
                    src_duration = float(words[-1].get("end", 0)) if words else 0
                    if src_duration <= 0:
                        # Probe the actual file as a fallback.
                        proc = subprocess.run(
                            ["ffprobe", "-v", "error", "-show_entries",
                             "format=duration", "-of", "csv=p=0",
                             str(video_path)],
                            capture_output=True, text=True,
                        )
                        try:
                            src_duration = float((proc.stdout or "0").strip())
                        except ValueError:
                            src_duration = 0
                    top_panel = reframe_settings.get("top_panel") or "active"
                    bottom_panel = reframe_settings.get("bottom_panel") or "full"
                    plan = compute_reframe_plan(
                        reframe_data, src_w, src_h, src_duration,
                        top_panel=top_panel, bottom_panel=bottom_panel
                    )
                    reframe_video_path = UPLOAD_DIR / f"{job_id}_reframed.mp4"
                    apply_reframe(video_path, plan, reframe_video_path)
                    video_path = reframe_video_path
                except Exception as e:
                    # Don't abort the whole render — fall through with the
                    # un-reframed source and surface the error to the UI.
                    jobs[job_id]["error"] = f"Reframe failed: {e}"
                    _db_save_job(job_id)
                    if reframe_video_path:
                        _safe_unlink(reframe_video_path)
                        reframe_video_path = None

        jobs[job_id]["status"] = "building subtitles"
        jobs[job_id]["progress"] = 55
        _db_save_job(job_id)
        w, h = get_video_dimensions(video_path)
        # If Quality Boost is enabled, the burn step will upscale the frame
        # before the subtitles filter runs. Build the ASS layout against the
        # POST-upscale dimensions so libass scales fonts correctly.
        if style.get("quality_boost"):
            w, h, _ = _quality_boost_scale(w, h)
            
        speaker_colors = style.get('speaker_colors', {}) or {}
        headline_banner = style.get('headline_banner', '')
        # Color captions by speaker whenever colors are set — don't require
        # the 9:16 reframe compositor to be on.
        diar = diarization_data if (reframe_settings.get("enabled") or speaker_colors) else None

        ass_content = build_ass(
            words, style, w, h,
            emoji_rules=emoji_rules,
            speaker_colors=speaker_colors,
            diarization=diar,
            headline_banner=headline_banner
        )

        ass_path = UPLOAD_DIR / f"{job_id}.ass"
        ass_path.write_text(ass_content, encoding="utf-8")

        provider = audio.get("provider", "ffmpeg") if audio else "ffmpeg"
        cache_path = UPLOAD_DIR / f"{job_id}_audiocache.aac"
        cache_key = _audio_cache_key(audio, style) if audio else ""
        cached_key = jobs[job_id].get("audio_cache_key") or ""
        cache_hit = (
            provider in ("auphonic", "elevenlabs", "dolby")
            and cache_key
            and cache_key == cached_key
            and cache_path.exists()
            and cache_path.stat().st_size > 0
        )

        if cache_hit:
            cache_logger.info(f"[{job_id}] Audio cache HIT (key: {cache_key})")
            # Re-use the previously enhanced audio. No API call, no credits used.
            jobs[job_id]["status"] = "using cached enhanced audio"
            jobs[job_id]["progress"] = 75
            _db_save_job(job_id)
            enhanced_audio_path = UPLOAD_DIR / f"{job_id}_enhanced.aac"
            shutil.copy(cache_path, enhanced_audio_path)
        elif provider == "auphonic":
            cache_logger.info(f"[{job_id}] Audio cache MISS (key: {cache_key}) - Processing with {provider}")
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
            cache_logger.info(f"[{job_id}] Audio cache MISS (key: {cache_key}) - Processing with {provider}")
            if not os.environ.get("ELEVENLABS_API_KEY"):
                raise RuntimeError(
                    "ElevenLabs is not configured on this server (ELEVENLABS_API_KEY not set)."
                )

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
                cmd = [FFMPEG, "-y", "-i", str(enhanced_audio_path),
                       "-ss", f"{offset_sec:.3f}",
                       "-c:a", "aac", "-b:a", "192k", str(adjusted)]
            else:
                delay_ms = int(round(abs(offset_sec) * 1000))
                cmd = [FFMPEG, "-y", "-i", str(enhanced_audio_path),
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
        burn_key = _burn_cache_key(style, words, emoji_rules or {}, video_path, job_id=job_id)
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
                punch_cfg=style.get("punch_zoom"),
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

        # ---- Optional background music with ducking ----
        bg_music = style.get('bg_music', {})
        if bg_music.get('enabled'):
            bg_music_files = list(UPLOAD_DIR.glob(f"{job_id}_bgmusic.*"))
            if bg_music_files:
                bg_music_path = bg_music_files[0]
                jobs[job_id]["status"] = "mixing background music"
                _db_save_job(job_id)
                vol = float(bg_music.get('volume_db', -12))
                duck = bool(bg_music.get('duck'))
                
                mixed_output = OUTPUT_DIR / f"{job_id}_mixed.mp4"
                
                if duck:
                    filter_complex = f"[1:a]volume={vol}dB[mus]; [0:a][mus]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[outa]"
                else:
                    filter_complex = f"[1:a]volume={vol}dB[mus]; [0:a][mus]amix=inputs=2:duration=first:dropout_transition=2[outa]"
                    
                cmd = [
                    FFMPEG, "-y",
                    "-i", str(output_path),
                    "-i", str(bg_music_path),
                    "-filter_complex", filter_complex,
                    "-map", "0:v", "-map", "[outa]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    str(mixed_output)
                ]
                ffmpeg_logger.info(f"[{job_id}] Mixing bg music: {' '.join(cmd)}")
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode == 0:
                    shutil.move(mixed_output, output_path)
                else:
                    ffmpeg_logger.error(f"[{job_id}] Mixing bg music failed: {proc.stderr}")
                    _safe_unlink(mixed_output)

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
        # The tightened-source intermediate isn't cached — burn / audio caches
        # already reflect the tighten settings via their cache keys, so the
        # tightened mp4 only needs to live for the duration of this render.
        _safe_unlink(tight_video_path) if tight_video_path else None
        _safe_unlink(reframe_video_path) if reframe_video_path else None


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

# ---- B-roll / photo overlay helpers (Phase 5+) ----
# Pipeline: transcript keywords → image provider → ASSET_DIR → Timeline overlay
# track → existing FFmpeg composite. We deliberately do NOT use Playwright to
# scrape Google (ToS/fragile) or MoviePy (duplicates the FFmpeg render path).

def _broll_provider_status() -> dict:
    return {
        "google_cse": bool(os.environ.get("GOOGLE_CSE_API_KEY") and os.environ.get("GOOGLE_CSE_CX")),
        "pexels": bool(os.environ.get("PEXELS_API_KEY")),
        "unsplash": bool(os.environ.get("UNSPLASH_ACCESS_KEY")),
        "badge": True,
    }


def _broll_any_photo_provider() -> bool:
    st = _broll_provider_status()
    return bool(st["google_cse"] or st["pexels"] or st["unsplash"])


def _download_url_to_asset(url: str, dest_stem: Path, timeout: int = 25) -> Path | None:
    """Download an image URL next to dest_stem. Returns the saved Path or None."""
    if not url:
        return None
    try:
        import requests as _req
        headers = {
            "User-Agent": "SubtitleAssistantBroll/1.0",
            "Accept": "image/*,*/*",
        }
        r = _req.get(url, headers=headers, timeout=timeout, stream=True)
        if r.status_code >= 400:
            return None
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = "jpg"
        if "png" in ctype:
            ext = "png"
        elif "webp" in ctype:
            ext = "webp"
        elif "gif" in ctype:
            ext = "gif"
        dest = dest_stem.with_suffix(f".{ext}")
        written = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                written += len(chunk)
                if written > 12 * 1024 * 1024:
                    break
        if dest.exists() and dest.stat().st_size > 800:
            return dest
        _safe_unlink(dest)
        return None
    except Exception as e:
        ai_logger.warning(f"B-roll download failed: {e}")
        return None


def _search_broll_google_cse(query: str) -> str | None:
    key = os.environ.get("GOOGLE_CSE_API_KEY", "")
    cx = os.environ.get("GOOGLE_CSE_CX", "")
    if not key or not cx:
        return None
    try:
        import requests as _req
        r = _req.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": key, "cx": cx, "q": query,
                "searchType": "image", "num": 1, "safe": "active",
            },
            timeout=20,
        )
        if r.status_code >= 400:
            return None
        items = (r.json() or {}).get("items") or []
        if not items:
            return None
        return items[0].get("link")
    except Exception as e:
        ai_logger.warning(f"Google CSE B-roll search failed: {e}")
        return None


def _search_broll_pexels(query: str) -> str | None:
    key = os.environ.get("PEXELS_API_KEY", "")
    if not key:
        return None
    try:
        import requests as _req
        r = _req.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": 1, "orientation": "landscape"},
            headers={"Authorization": key},
            timeout=20,
        )
        if r.status_code >= 400:
            return None
        photos = (r.json() or {}).get("photos") or []
        if not photos:
            return None
        src = photos[0].get("src") or {}
        return src.get("large") or src.get("medium") or src.get("original")
    except Exception as e:
        ai_logger.warning(f"Pexels B-roll search failed: {e}")
        return None


def _search_broll_unsplash(query: str) -> str | None:
    key = os.environ.get("UNSPLASH_ACCESS_KEY", "")
    if not key:
        return None
    try:
        import requests as _req
        r = _req.get(
            "https://api.unsplash.com/search/photos",
            params={"query": query, "per_page": 1, "orientation": "landscape"},
            headers={"Authorization": f"Client-ID {key}", "Accept-Version": "v1"},
            timeout=20,
        )
        if r.status_code >= 400:
            return None
        results = (r.json() or {}).get("results") or []
        if not results:
            return None
        urls = results[0].get("urls") or {}
        return urls.get("regular") or urls.get("small") or urls.get("full")
    except Exception as e:
        ai_logger.warning(f"Unsplash B-roll search failed: {e}")
        return None


def _fetch_broll_image_for_keyword(query: str, dest_stem: Path) -> Path | None:
    """Search providers in order and download the first hit next to dest_stem."""
    q = (query or "").strip()
    if not q:
        return None
    url = (
        _search_broll_google_cse(q)
        or _search_broll_pexels(q)
        or _search_broll_unsplash(q)
    )
    if not url:
        return None
    return _download_url_to_asset(url, dest_stem)


def _overlay_layout_for_index(i: int, placement: str = "pip") -> dict:
    placement = (placement or "pip").lower()
    if placement in ("center", "centre", "full"):
        return {
            "x": 0.12, "y": 0.18, "w": 0.76, "h": 0.52,
            "fit": "contain", "layout": "center",
        }
    corners = [
        {"x": 0.58, "y": 0.06},
        {"x": 0.04, "y": 0.06},
        {"x": 0.58, "y": 0.62},
        {"x": 0.04, "y": 0.62},
    ]
    pos = corners[i % 4]
    return {
        "x": pos["x"], "y": pos["y"], "w": 0.38, "h": 0.24,
        "fit": "cover", "layout": "pip_auto",
    }


def _make_keyword_badge_png(text: str, dest: Path) -> bool:
    """Render a simple keyword badge PNG via ffmpeg (no Pillow required)."""
    label = re.sub(r"[^\w\s\-']", "", str(text or "")).strip()[:28] or "B-roll"
    safe = label.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    vf = (
        f"drawbox=x=0:y=0:w=iw:h=ih:color=0x10131d@1:t=fill,"
        f"drawbox=x=16:y=16:w=iw-32:h=ih-32:color=0x6c5cff@1:t=6,"
        f"drawtext=text='{safe}':fontcolor=white:fontsize=54:"
        f"x=(w-text_w)/2:y=(h-text_h)/2:font=Sans"
    )
    cmd = [
        FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=0x10131d:s=640x360:d=0.1",
        "-frames:v", "1", "-update", "1", "-vf", vf, str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0 and dest.exists() and dest.stat().st_size > 100


@app.route('/fetch-auto-overlays', methods=['POST'])
def fetch_auto_overlays():
    """Suggest timed B-roll overlays from transcript keywords.

    Body: {
      words?: [...], job_id?: str, budget?: int,
      mode?: "auto"|"photo"|"badge",
      placement?: "pip"|"center"
    }
    """
    data = request.get_json(force=True) or {}
    words = data.get("words") or []
    job_id = data.get("job_id")
    if (not words) and job_id and job_id in jobs:
        words = jobs[job_id].get("words") or []
    try:
        budget = max(1, min(8, int(data.get("budget") or 5)))
    except (TypeError, ValueError):
        budget = 5
    mode = str(data.get("mode") or "auto").lower().strip()
    if mode not in ("auto", "photo", "badge"):
        mode = "auto"
    placement = str(data.get("placement") or "pip").lower().strip()

    callouts = _keyword_callouts_for_window(words, 0.0, 1e9, budget)
    overlays = []
    providers = _broll_provider_status()
    used_photo = 0
    used_badge = 0
    ASSET_DIR.mkdir(parents=True, exist_ok=True)

    for co in callouts:
        label = str(co.get("text") or "B-roll").strip() or "B-roll"
        start = float(co.get("start") or 0)
        dur = float(co.get("duration") or 1.8)
        asset_id = uuid.uuid4().hex
        asset_path = None
        source = "badge"

        want_photo = mode in ("photo", "auto") and _broll_any_photo_provider()
        if want_photo:
            asset_path = _fetch_broll_image_for_keyword(label, ASSET_DIR / asset_id)
            if asset_path:
                source = "photo"
                used_photo += 1
                final = ASSET_DIR / f"{asset_id}{asset_path.suffix.lower()}"
                if asset_path.resolve() != final.resolve():
                    try:
                        if final.exists():
                            _safe_unlink(final)
                        asset_path.replace(final)
                        asset_path = final
                    except OSError:
                        pass

        if asset_path is None:
            if mode == "photo":
                continue
            dest = ASSET_DIR / f"{asset_id}.png"
            if not _make_keyword_badge_png(label, dest):
                _safe_unlink(dest)
                continue
            asset_path = dest
            source = "badge"
            used_badge += 1

        stem = asset_path.stem
        if stem != asset_id:
            asset_id = stem

        display_name = f"{label}{asset_path.suffix.lower()}"
        _write_asset_meta(
            asset_id,
            filename=display_name,
            keyword=label,
            source=source,
        )

        pos = _overlay_layout_for_index(len(overlays), placement)
        overlays.append({
            "asset_id": asset_id,
            "keyword": label,
            "source": source,
            "in": 0,
            "out": max(1.2, dur),
            "start": max(0.0, start),
            "x": pos["x"],
            "y": pos["y"],
            "w": pos["w"],
            "h": pos["h"],
            "opacity": 0.95 if source == "badge" else 1.0,
            "fit": pos["fit"],
            "fade_in": 0.15,
            "fade_out": 0.25,
            "border_px": 2 if source == "photo" else 0,
            "layout": pos["layout"],
            "ken_burns": (
                {"enabled": True, "direction": "in", "intensity": "med"}
                if source == "photo" else None
            ),
        })

    return jsonify({
        "ok": True,
        "overlays": overlays,
        "count": len(overlays),
        "mode": mode,
        "placement": placement,
        "providers": providers,
        "stats": {"photo": used_photo, "badge": used_badge},
    })


@app.route("/broll/status", methods=["GET"])
def broll_status():
    """Which B-roll image providers are configured."""
    st = _broll_provider_status()
    return jsonify({
        "providers": st,
        "photo_ready": _broll_any_photo_provider(),
        "hint": (
            "Set PEXELS_API_KEY and/or UNSPLASH_ACCESS_KEY and/or "
            "GOOGLE_CSE_API_KEY+GOOGLE_CSE_CX for photo B-roll."
        ),
    })


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


@app.route('/upload-bg-music', methods=['POST'])
def upload_bg_music():
    job_id = request.form.get('job_id')
    music_file = request.files.get('music')
    if not job_id or not music_file:
        return jsonify({"error": "Missing job_id or music"}), 400
    
    ext = music_file.filename.rsplit('.', 1)[-1] if '.' in music_file.filename else 'mp3'
    filename = f"{job_id}_bgmusic.{ext}"
    path = UPLOAD_DIR / filename
    music_file.save(str(path))
    return jsonify({"ok": True, "path": filename})


@app.route("/suggest-clips", methods=["POST"])
def suggest_clips():
    """LLM-driven clip suggestions for a job's transcript.

    Body: {job_id, format, target_durations, num_clips}
      format: "comedy" | "interview" | "event_recap" | "auto"
      target_durations: list of ints in seconds (e.g. [5, 15, 30, 60]).
                        Falls back to [60] if missing/empty. Single-int
                        legacy `target_duration` is also accepted.
      num_clips: int (default 5, max 12). Quality > quantity — if Gemini
                 returns fewer, that's fine.

    Response: {clips, format}. Clips are sorted by start_time (earliest
    first) and each carries a group_id grouping any clips that overlap
    by ≥90% of the shorter clip — so the UI can tint same-content variants
    with a shared hue.
    """
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    format_type = (data.get("format") or "auto").lower()
    if format_type not in _FORMAT_RUBRICS:
        format_type = "auto"

    raw_durations = data.get("target_durations")
    if raw_durations is None and "target_duration" in data:
        raw_durations = [data.get("target_duration")]
    durations: list[int] = []
    for d in (raw_durations or []):
        try:
            v = int(d)
            if 5 <= v <= 180:
                durations.append(v)
        except (TypeError, ValueError):
            continue
    if not durations:
        durations = [60]

    try:
        num_clips = int(data.get("num_clips") or 5)
    except (TypeError, ValueError):
        num_clips = 5
    num_clips = max(1, min(12, num_clips))

    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    job = jobs[job_id]
    words = job.get("words")
    if not words:
        return jsonify({"error": "Transcript not available for this job yet."}), 400

    raw_avoid = data.get("avoid_ranges") or []
    avoid_ranges: list[tuple[float, float]] = []
    for r in raw_avoid:
        try:
            s, e = float(r[0]), float(r[1])
            if e > s >= 0:
                avoid_ranges.append((s, e))
        except (TypeError, ValueError, IndexError):
            continue

    transcript = _format_transcript_for_llm(words)
    prompt = _build_clip_suggestion_prompt(
        transcript, format_type, durations, num_clips, avoid_ranges
    )

    try:
        result = _gemini_generate_clip_suggestions(prompt)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    clips = result.get("clips") or []
    src_max_t = float(words[-1].get("end", 0)) if words else 0
    cleaned: list[dict] = []
    for c in clips:
        try:
            start = float(c.get("start_time"))
            end = float(c.get("end_time"))
            if end <= start or start < 0 or end > src_max_t + 1:
                continue
            cleaned.append({
                "start_time": start,
                "end_time": end,
                "hook_start_time": float(c.get("hook_start_time", start)),
                "hook_end_time": float(c.get("hook_end_time", min(end, start + 5))),
                "hook_quote": str(c.get("hook_quote", ""))[:300],
                "title": str(c.get("title", ""))[:120],
                "reason": str(c.get("reason", ""))[:500],
                "viral_score": int(c.get("viral_score", 0)),
                "category": str(c.get("category", "")),
                "suggested_headline": str(c.get("suggested_headline", "")),
            })
        except (TypeError, ValueError):
            continue

    # Force every clip's length to one of the user's selected targets — Gemini
    # treats durations as suggestions even when told they're strict, so we snap
    # end_time to the nearest word boundary at start + nearest_target.
    _snap_clip_to_target_durations(cleaned, words, durations)
    # Hard 3s floor — drop anything that ended up too short to be a real clip.
    cleaned = [c for c in cleaned if (c["end_time"] - c["start_time"]) >= 3]
    # Drop clips that materially overlap any avoid_range (≥50% of clip inside).
    if avoid_ranges:
        kept: list[dict] = []
        for c in cleaned:
            cs, ce = c["start_time"], c["end_time"]
            cdur = ce - cs
            overlap_ratio = 0.0
            for (a_s, a_e) in avoid_ranges:
                ov = max(0.0, min(ce, a_e) - max(cs, a_s))
                if cdur > 0 and ov / cdur > overlap_ratio:
                    overlap_ratio = ov / cdur
            if overlap_ratio < 0.5:
                kept.append(c)
        cleaned = kept
    # Sort by source-timeline start (earliest first) so the UI reads in order.
    cleaned.sort(key=lambda c: c["start_time"])
    # Tag overlap groups so the UI can tint same-content variants together.
    _detect_overlap_groups(cleaned, threshold=0.90)

    return jsonify({"clips": cleaned, "format": format_type})


def _create_clip_from_job(source_job_id: str, start: float, end: float, label: str, style=None) -> str:
    if not source_job_id or source_job_id not in jobs:
        raise ValueError("Source job not found")
    src_video = find_video_path(source_job_id)
    if not src_video:
        raise ValueError("Source video no longer available on the server")
    if end <= start:
        raise ValueError("end_time must be greater than start_time")

    src_job = jobs[source_job_id]
    new_job_id = uuid.uuid4().hex
    ext = src_video.suffix.lstrip(".") or "mp4"
    new_video_path = UPLOAD_DIR / f"{new_job_id}.{ext}"

    duration = end - start
    cmd = [
        FFMPEG, "-y",
        "-ss", f"{start:.3f}",
        "-i", str(src_video),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(new_video_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        _safe_unlink(new_video_path)
        raise RuntimeError(f"Trim failed: {proc.stderr[-500:]}")

    src_words = src_job.get("words") or []
    new_words = []
    for w in src_words:
        try:
            ws = float(w.get("start", 0))
            we = float(w.get("end", 0))
        except (TypeError, ValueError):
            continue
        if we <= start or ws >= end:
            continue
        new_words.append({
            "word": w.get("word", ""),
            "start": max(0.0, ws - start),
            "end": max(0.0, min(end, we) - start),
        })

    src_filename = src_job.get("filename") or "clip"
    base_stem = Path(src_filename).stem if src_filename else "clip"
    new_filename = f"{base_stem} — {label or 'highlight'}.{ext}"

    # Caption look: prefer explicit style (UI flush), else source job.style.
    inherited = style if _style_has_caption_fields(style) else src_job.get("style")
    jobs[new_job_id] = {
        "status": "awaiting_edit",
        "progress": 100,
        "output": None,
        "error": None,
        "words": new_words,
        "style": _normalize_caption_style(inherited) if inherited else None,
        "audio": None,
        "emoji_rules": src_job.get("emoji_rules"),
        "created_at": time.time(),
        "filename": new_filename,
    }
    _db_save_job(new_job_id)
    return new_job_id

@app.route("/selftest")
def selftest():
    """A dependency-free page for isolating file-picker problems on a device.

    The app's own picker sits behind its stylesheet, its scripts and whatever
    frame it is being viewed in. This page has none of that: three ways of
    opening a picker, side by side, so a device that fails can say which
    mechanism failed rather than just "nothing happens".
    """
    return Response(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Picker self-test</title></head>
<body style="font:16px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:16px;background:#111;color:#eee">
<h2 style="margin:0 0 4px">Picker self-test</h2>
<p style="color:#9aa0a6;margin:0 0 16px;font-size:13px">
  Server time {time.strftime('%H:%M:%S')} · app.py build {int(Path(__file__).stat().st_mtime)}
</p>

<div style="border:1px solid #333;border-radius:10px;padding:14px;margin-bottom:12px">
  <b>1. Plain visible input</b><br>
  <input id="a" type="file" accept="video/*" style="margin-top:8px">
</div>

<div style="border:1px solid #333;border-radius:10px;padding:14px;margin-bottom:12px">
  <b>2. Label + hidden-but-rendered input</b> (what the app now uses)<br>
  <input id="b" type="file" accept="video/*"
         style="position:absolute;width:1px;height:1px;opacity:0">
  <label for="b" style="display:inline-block;margin-top:8px;padding:10px 16px;
         background:#6c5cff;color:#fff;border-radius:8px;cursor:pointer">Tap to choose</label>
</div>

<div style="border:1px solid #333;border-radius:10px;padding:14px;margin-bottom:12px">
  <b>3. Scripted .click()</b> (what the app used before)<br>
  <input id="c" type="file" accept="video/*" style="display:none">
  <button onclick="document.getElementById('c').click()"
          style="margin-top:8px;padding:10px 16px;background:#333;color:#fff;
          border:1px solid #555;border-radius:8px">Tap to choose</button>
</div>

<pre id="out" style="background:#000;border:1px solid #333;border-radius:8px;
     padding:12px;white-space:pre-wrap;word-break:break-all;font-size:12px">No file chosen yet.</pre>
<p id="ua" style="color:#666;font-size:11px;word-break:break-all"></p>

<script>
var out = document.getElementById("out");
document.getElementById("ua").textContent = navigator.userAgent;
["a","b","c"].forEach(function (id) {{
  document.getElementById(id).addEventListener("change", function (e) {{
    var f = e.target.files && e.target.files[0];
    out.textContent = f
      ? ("WORKED via #" + id + "\\nname: " + f.name + "\\ntype: " + (f.type || "(none)") +
         "\\nsize: " + (f.size / 1048576).toFixed(1) + " MB")
      : ("#" + id + " fired but no file");
  }});
}});
window.addEventListener("error", function (e) {{
  out.textContent = "JS error: " + e.message;
}});
</script>
</body></html>""", mimetype="text/html")


@app.route("/suggest-effects", methods=["POST"])
def suggest_effects():
    """Where camera moves belong in a job, as timed ranges.

    Punch-zoom and Ken Burns placements come from the transcript via Gemini;
    split-screen comes free from the diarization overlaps the reframe analysis
    already cached, so those are returned even when Gemini is unavailable.
    """
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    words = jobs[job_id].get("words")
    if not words:
        return jsonify({"error": "Transcript not available for this job yet."}), 400

    try:
        max_effects = int(data.get("max_effects") or 6)
    except (TypeError, ValueError):
        max_effects = 6
    max_effects = max(1, min(12, max_effects))

    total = float(words[-1].get("end", 0) or 0)
    if total <= 0:
        return jsonify({"error": "Could not determine the video's length."}), 400

    # Diarization overlaps need no model, so they stand on their own if the
    # Gemini call fails or isn't configured.
    suggestions = _overlap_split_suggestions(job_id, total)
    gemini_error = None
    try:
        result = _gemini_generate_clip_suggestions(
            _build_effect_suggestion_prompt(
                _format_transcript_for_llm(words), total, max_effects
            )
        )
        suggestions += (result.get("effects") or [])
    except RuntimeError as exc:
        gemini_error = str(exc)

    cleaned = _sanitize_effect_suggestions(suggestions, total)[:max_effects]
    # Aim each push at the speaker rather than the centre of the frame, when
    # the reframe analysis has face data to aim with.
    for fx in cleaned:
        if fx["type"] == "punch_zoom":
            anchor = _face_anchor_at(job_id, fx["start_time"])
            if anchor:
                fx["anchor"] = anchor
    if not cleaned and gemini_error:
        return jsonify({"error": gemini_error}), 500
    return jsonify({
        "effects": cleaned,
        "duration": total,
        "warning": gemini_error,   # partial results still worth returning
    })


@app.route("/clip-from-job", methods=["POST"])
def clip_from_job():
    """Spawn a new job that's a trimmed slice of an existing job's source video.

    Body: {source_job_id, start_time, end_time, label, style?}
    Inherits Caption look (explicit style or source job.style), emoji_rules,
    and transcript words (filtered + offset to the new range). The new job
    lands at awaiting_edit so the user can immediately tweak and render.
    """
    data = request.get_json(force=True) or {}
    source_job_id = data.get("source_job_id")
    try:
        start = max(0.0, float(data.get("start_time", 0)))
        end = float(data.get("end_time", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid start/end time"}), 400
    label = (data.get("label") or "").strip()[:80]
    style = data.get("style")

    try:
        new_job_id = _create_clip_from_job(source_job_id, start, end, label, style=style)
        return jsonify({"job_id": new_job_id, "filename": jobs[new_job_id]["filename"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/auto-process-job", methods=["POST"])
def auto_process_job():
    """Generate AI Shorts for a job.

    Fast path: Gemini on the existing transcript only.
    Diarization / 9:16 reframe is kicked off in the background (optional) —
    it must NEVER block Shorts or make upload feel like a 15‑minute hang.
    """
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    format_type = data.get("format", "vendor_interview")
    num_clips = int(data.get("num_clips", 5))
    # Opt-in only — Analyze Speakers button is the explicit path.
    run_diarization = bool(data.get("run_diarization"))

    if not job_id or job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    job = jobs[job_id]
    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Video missing"}), 404

    # Accept the same target_durations shape as /suggest-clips (UI checkboxes).
    raw_durations = data.get("target_durations")
    if raw_durations is None and "target_duration" in data:
        raw_durations = [data.get("target_duration")]
    durations: list[int] = []
    for d in (raw_durations or []):
        try:
            v = int(d)
            if 5 <= v <= 180:
                durations.append(v)
        except (TypeError, ValueError):
            continue
    if not durations:
        durations = [30, 60]

    try:
        # a. Transcript must already exist (normal upload path). Do not
        # re-run Whisper here — that doubled wait when Auto-Shorts was on.
        if not job.get("words"):
            return jsonify({
                "error": "Transcript not ready yet — wait for Whisper to finish, then retry."
            }), 409

        # b. Optional diarization — background only, never block this request.
        cache_path = UPLOAD_DIR / f"{job_id}_reframe.json"
        if run_diarization and not cache_path.exists():
            ok, _msg = _reframe_deps_available()
            if ok:
                threading.Thread(
                    target=analyze_reframe_job, args=(job_id, video_path), daemon=True
                ).start()

        # c. Gemini clip suggestions (the actual Shorts work).
        transcript_text = _format_transcript_for_llm(job["words"])
        prompt = _build_clip_suggestion_prompt(transcript_text, format_type, durations, num_clips)
        result = _gemini_generate_clip_suggestions(prompt)
        clips = result.get("clips", [])

        cleaned = []
        for c in clips:
            try:
                start = float(c.get("start_time"))
                end = float(c.get("end_time"))
                if end <= start or start < 0:
                    continue
                cleaned.append({
                    "start_time": start,
                    "end_time": end,
                    "hook_start_time": float(c.get("hook_start_time", start)),
                    "hook_end_time": float(c.get("hook_end_time", min(end, start + 5))),
                    "hook_quote": str(c.get("hook_quote", ""))[:300],
                    "title": str(c.get("title", ""))[:120],
                    "reason": str(c.get("reason", ""))[:500],
                    "viral_score": int(c.get("viral_score", 0)),
                    "category": str(c.get("category", "")),
                    "suggested_headline": str(c.get("suggested_headline", "")),
                })
            except (TypeError, ValueError):
                continue
        _snap_clip_to_target_durations(cleaned, job["words"], durations)
        cleaned = [c for c in cleaned if (c["end_time"] - c["start_time"]) >= 3]
        cleaned.sort(key=lambda c: c["start_time"])
        _detect_overlap_groups(cleaned, threshold=0.90)

        job["clip_suggestions"] = cleaned
        job["clip_format"] = format_type
        _db_save_job(job_id)

        return jsonify({"ok": True, "job_id": job_id, "clips": cleaned, "format": format_type})
    except Exception as e:
        ai_logger.error(f"[{job_id}] Auto-process failed: {e}")
        return jsonify({"error": str(e)}), 500

import zipfile
@app.route("/batch-render-clips", methods=["POST"])
def batch_render_clips():
    data = request.get_json(force=True) or {}
    source_job_id = data.get("source_job_id")
    clips = data.get("clips", [])
    style = data.get("style", {})
    format_zip = data.get("format_zip", False)

    if not source_job_id or source_job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
        
    static_renders = BASE_DIR / "static" / "renders"
    static_renders.mkdir(parents=True, exist_ok=True)
    
    rendered_clips = []
    clip_paths = []
    
    try:
        for i, c in enumerate(clips):
            start = float(c.get("start_time", 0))
            end = float(c.get("end_time", 0))
            title = c.get("title", f"clip_{i}")
            headline = c.get("headline", "")
            
            new_job_id = _create_clip_from_job(source_job_id, start, end, title, style=style)
            
            job = jobs[new_job_id]
            job_style = _normalize_caption_style(style if _style_has_caption_fields(style) else job.get("style"))
            if headline:
                job_style["headline_banner"] = str(headline).strip()
            job["style"] = job_style
            _db_save_job(new_job_id)
            
            video_path = find_video_path(new_job_id)
            render_job(new_job_id, video_path, job["words"], job_style, None, job.get("emoji_rules", {}))
            
            job_output = jobs[new_job_id].get("output")
            if not job_output:
                ffmpeg_logger.error(f"Render failed for clip {title}")
                continue
                
            out_file = OUTPUT_DIR / job_output
            dest_file = static_renders / job_output
            shutil.copy(out_file, dest_file)
            
            rendered_clips.append({
                "title": title,
                "url": f"/static/renders/{job_output}"
            })
            clip_paths.append((title, dest_file))
            
        if format_zip:
            timestamp = int(time.time())
            zip_filename = f"batch_{source_job_id}_{timestamp}.zip"
            zip_path = static_renders / zip_filename
            with zipfile.ZipFile(zip_path, 'w') as zf:
                for title, p in clip_paths:
                    zf.write(p, arcname=f"{title}.mp4")
            
            return jsonify({
                "ok": True, 
                "download_url": f"/static/renders/{zip_filename}", 
                "zip_filename": zip_filename, 
                "clips": rendered_clips
            })
        else:
            return jsonify({"ok": True, "clips": rendered_clips})
            
    except Exception as e:
        ffmpeg_logger.error(f"Batch render failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/compile-clips", methods=["POST"])
def compile_clips():
    """Stitch multiple clip ranges into one composite job.

    Body: {clips: [{source_job_id, start_time, end_time}], label}
    Returns: {job_id, filename}

    Each segment is trimmed and re-encoded (libx264 / aac) at 1080p / 30fps
    so the concat demuxer can copy-stream them into one mp4 without re-encode.
    The merged transcript is built by filtering each source's words to the
    requested range and offsetting them by cumulative segment duration.
    """
    data = request.get_json(force=True) or {}
    items = data.get("clips") or []
    if not items:
        return jsonify({"error": "No clips provided"}), 400
    label = (data.get("label") or "compilation").strip()[:80] or "compilation"

    validated = []
    for idx, item in enumerate(items):
        sid = item.get("source_job_id")
        if not sid or sid not in jobs:
            return jsonify({"error": f"Source job not found: {sid}"}), 404
        src_video = find_video_path(sid)
        if not src_video:
            return jsonify({"error": f"Source video missing for clip {idx + 1}"}), 404
        try:
            start = max(0.0, float(item.get("start_time", 0)))
            end = float(item.get("end_time", 0))
        except (TypeError, ValueError):
            return jsonify({"error": f"Invalid time on clip {idx + 1}"}), 400
        if end <= start:
            return jsonify({"error": f"end_time must be > start_time on clip {idx + 1}"}), 400
        validated.append({
            "source_job_id": sid,
            "src_video": src_video,
            "start": start,
            "end": end,
            "title": str(item.get("title") or "")[:120],
            "hook_quote": str(item.get("hook_quote") or "")[:300],
            "source_filename": str(item.get("source_filename") or "")[:200],
        })

    new_job_id = uuid.uuid4().hex
    composite_path = UPLOAD_DIR / f"{new_job_id}.mp4"

    # 1. Trim + normalize each segment to a uniform format so concat is clean.
    seg_paths: list[Path] = []
    list_path: Path | None = None
    try:
        for i, v in enumerate(validated):
            seg_path = UPLOAD_DIR / f"{new_job_id}_seg{i:03d}.mp4"
            duration = v["end"] - v["start"]
            cmd = [
                FFMPEG, "-y",
                "-ss", f"{v['start']:.3f}",
                "-i", str(v["src_video"]),
                "-t", f"{duration:.3f}",
                "-vf", "scale=1080:-2:flags=lanczos,fps=30",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                str(seg_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                return jsonify({"error": f"Trim failed on clip {i + 1}: {proc.stderr[-400:]}"}), 500
            seg_paths.append(seg_path)

        # 2. Concat with the demuxer (cheap stream copy now that all segs match).
        list_path = UPLOAD_DIR / f"{new_job_id}_concat.txt"
        list_path.write_text(
            "\n".join(f"file '{p.absolute()}'" for p in seg_paths) + "\n"
        )
        cmd = [
            FFMPEG, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(composite_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return jsonify({"error": f"Concat failed: {proc.stderr[-500:]}"}), 500
    finally:
        for p in seg_paths:
            _safe_unlink(p)
        if list_path:
            _safe_unlink(list_path)

    # 3. Stitch the merged transcript with cumulative offsets.
    merged_words: list[dict] = []
    cumulative = 0.0
    inherited_style = None
    inherited_emoji = None
    for v in validated:
        src_job = jobs[v["source_job_id"]]
        if inherited_style is None and src_job.get("style"):
            inherited_style = src_job.get("style")
        if inherited_emoji is None and src_job.get("emoji_rules"):
            inherited_emoji = src_job.get("emoji_rules")
        for w in (src_job.get("words") or []):
            try:
                ws = float(w.get("start", 0))
                we = float(w.get("end", 0))
            except (TypeError, ValueError):
                continue
            if we <= v["start"] or ws >= v["end"]:
                continue
            merged_words.append({
                "word": w.get("word", ""),
                "start": cumulative + max(0.0, ws - v["start"]),
                "end": cumulative + max(0.0, min(v["end"], we) - v["start"]),
            })
        cumulative += (v["end"] - v["start"])

    new_filename = f"{label}.mp4"
    recipe = {
        "label": label,
        "created_at": time.time(),
        "clips": [
            {
                "source_job_id": v["source_job_id"],
                "start_time": v["start"],
                "end_time": v["end"],
                "title": v["title"],
                "hook_quote": v["hook_quote"],
                "source_filename": v["source_filename"],
            }
            for v in validated
        ],
    }
    jobs[new_job_id] = {
        "status": "awaiting_edit",
        "progress": 100,
        "output": None,
        "error": None,
        "words": merged_words,
        "style": inherited_style,
        "audio": None,
        "emoji_rules": inherited_emoji,
        "created_at": time.time(),
        "filename": new_filename,
        "compile_recipe": recipe,
    }
    _db_save_job(new_job_id)
    return jsonify({"job_id": new_job_id, "filename": new_filename, "segments": len(validated)})


@app.route("/list-compilations", methods=["GET"])
def list_compilations():
    """Return every job that was produced by /compile-clips, newest first.

    Each entry carries enough metadata for the UI to render a card without
    a follow-up call: label, segment count, total duration, and a list of
    source_job_ids so the frontend can flag missing-source clips up front.
    """
    out = []
    for jid, j in jobs.items():
        recipe = j.get("compile_recipe")
        if not recipe:
            continue
        clips = recipe.get("clips") or []
        total = sum(
            max(0.0, float(c.get("end_time", 0)) - float(c.get("start_time", 0)))
            for c in clips
        )
        out.append({
            "job_id": jid,
            "label": recipe.get("label") or j.get("filename") or "compilation",
            "filename": j.get("filename"),
            "created_at": recipe.get("created_at") or j.get("created_at"),
            "segment_count": len(clips),
            "total_duration": total,
            "source_job_ids": list({c.get("source_job_id") for c in clips if c.get("source_job_id")}),
        })
    out.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return jsonify({"compilations": out})


@app.route("/load-compilation/<job_id>", methods=["GET"])
def load_compilation(job_id: str):
    """Return the recipe for a previously-compiled job, with availability flags.

    For each clip we report whether its source job still exists (and has a
    video on disk) so the UI can grey out / let the user remove unavailable
    segments before re-rendering.
    """
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    recipe = jobs[job_id].get("compile_recipe")
    if not recipe:
        return jsonify({"error": "This job is not a compilation."}), 400
    annotated = []
    for c in (recipe.get("clips") or []):
        sid = c.get("source_job_id")
        source_ok = bool(sid and sid in jobs and find_video_path(sid))
        annotated.append({
            **c,
            "source_available": source_ok,
        })
    return jsonify({
        "job_id": job_id,
        "label": recipe.get("label"),
        "created_at": recipe.get("created_at"),
        "clips": annotated,
    })


@app.route("/preview-tightening", methods=["POST"])
def preview_tightening():
    """Compute silence-compression stats and per-gap details for a job.

    Body: {job_id, max_gap, target_gap, preserved_gap_starts?}
    Returns: {stats: {...}, gaps: [{index, start, end, duration,
                                    preserved, context_before, context_after}, …]}
    """
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    words = jobs[job_id].get("words")
    if not words:
        return jsonify({"error": "Transcript not available for this job."}), 400
    try:
        max_gap = float(data.get("max_gap", 1.0))
        target_gap = float(data.get("target_gap", 0.3))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid threshold values"}), 400
    preserved = data.get("preserved_gap_starts") or []
    comp = compute_silence_compression(
        words,
        max_gap=max_gap,
        target_gap=target_gap,
        preserved_gap_starts=preserved,
    )
    return jsonify({"stats": comp["stats"], "gaps": comp["gaps"]})


@app.route("/rename-job", methods=["POST"])
def rename_job():
    """Set a human-friendly filename label for a job (sidebar display only).

    Body: {job_id, filename}
    """
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    new_name = (data.get("filename") or "").strip()[:200]
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    if not new_name:
        return jsonify({"error": "Filename cannot be empty"}), 400
    jobs[job_id]["filename"] = new_name
    _db_save_job(job_id)
    return jsonify({"job_id": job_id, "filename": new_name})


@app.route("/")
def index():
    auphonic_enabled = bool(os.environ.get("AUPHONIC_API_KEY"))
    elevenlabs_enabled = bool(os.environ.get("ELEVENLABS_API_KEY"))
    dolby_enabled = bool(os.environ.get("DOLBY_API_KEY"))
    gemini_enabled = bool(os.environ.get("GEMINI_API_KEY"))
    broll_providers = _broll_provider_status()
    broll_photo_ready = _broll_any_photo_provider()
    # Cache-bust static assets whenever they change on disk (e.g. after a
    # `git pull`). Browsers caching old app.js/style.css was producing
    # phantom layout bugs (e.g. tab content appearing blank).
    static_dir = BASE_DIR / "static"
    try:
        latest_mtime = max(
            (p.stat().st_mtime for p in static_dir.iterdir() if p.is_file()),
            default=0,
        )
        asset_version = str(int(latest_mtime))
    except OSError:
        asset_version = str(int(time.time()))
    html = render_template(
        "index.html",
        auphonic_enabled=auphonic_enabled,
        elevenlabs_enabled=elevenlabs_enabled,
        dolby_enabled=dolby_enabled,
        gemini_enabled=gemini_enabled,
        broll_photo_ready=broll_photo_ready,
        broll_providers=broll_providers,
        asset_version=asset_version,
    )
    # Never let the browser cache the HTML shell. The ?v=asset_version on the
    # CSS/JS only busts those files if the *page* itself is fresh — a cached
    # index.html keeps pointing at old ?v= and re-loads stale JS, which is how
    # version skew (new JS wiring buttons a stale page lacks) crept in.
    resp = make_response(html)
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/favicon.ico")
def favicon():
    """Tiny SVG favicon so browsers stop 404-spamming the console."""
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' rx='8' fill='#6c5cff'/>"
        "<text x='16' y='22' text-anchor='middle' font-size='16' "
        "font-family='system-ui,sans-serif' fill='white'>S</text></svg>"
    )
    return Response(svg, mimetype="image/svg+xml")


@app.route("/transcribe-only", methods=["POST"])
def transcribe_only():
    """Phase 1: upload video and transcribe. Returns job_id; poll /status for words."""
    if "video" not in request.files:
        # Behind a proxy this usually means the multipart body never arrived
        # intact (size limit, or a stripped Content-Type) rather than a UI
        # mistake, so log what did arrive instead of returning a bare 400.
        print(f"[upload] rejected: no 'video' part. "
              f"form_keys={list(request.form.keys())} "
              f"file_keys={list(request.files.keys())} "
              f"content_length={request.content_length} "
              f"content_type={request.content_type!r}", flush=True)
        return jsonify({"error": "No file uploaded (it did not reach the server intact)"}), 400
    f = request.files["video"]
    if f.filename == "":
        print("[upload] rejected: empty filename", flush=True)
        return jsonify({"error": "No file selected"}), 400
    if not allowed_file(f.filename):
        print(f"[upload] rejected: unsupported extension for {f.filename!r}", flush=True)
        return jsonify({
            "error": f"Unsupported file type: {f.filename}. Allowed: {', '.join(sorted(ALLOWED_EXT))}"
        }), 400

    job_id = uuid.uuid4().hex
    ext = f.filename.rsplit(".", 1)[1].lower()
    video_path = UPLOAD_DIR / f"{job_id}.{ext}"
    # Prefer the part's declared size; Content-Length is the whole multipart body.
    expected_bytes = None
    try:
        if getattr(f, "content_length", None):
            expected_bytes = int(f.content_length)
    except (TypeError, ValueError):
        expected_bytes = None
    f.save(str(video_path))
    try:
        probe = _validate_uploaded_media(video_path, expected_bytes=expected_bytes)
        print(
            f"[upload] {job_id} ok size={video_path.stat().st_size} "
            f"audio={probe.get('has_audio')} video={probe.get('has_video')} "
            f"dur={probe.get('duration'):.1f}s name={f.filename!r}",
            flush=True,
        )
        # Do NOT reject on !has_audio here — shallow/odd probes false-negative on
        # phone MOVs. Whisper extract is the ground truth (with a clear error).
    except Exception as e:
        _safe_unlink(video_path)
        print(f"[upload] {job_id} rejected after save: {e}", flush=True)
        return jsonify({"error": str(e)}), 400

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
        "media_info": probe,
    }
    _db_save_job(job_id)
    pre_clean = request.form.get("pre_clean", "").lower() in ("1", "true", "yes")
    t = threading.Thread(target=transcribe_job, args=(job_id, video_path, pre_clean))
    t.daemon = True
    t.start()

    return jsonify({
        "job_id": job_id,
        "media_info": {
            "size": probe.get("size"),
            "duration": probe.get("duration"),
            "has_audio": probe.get("has_audio"),
            "has_video": probe.get("has_video"),
            "video_codec": probe.get("video_codec"),
            "audio_codec": probe.get("audio_codec"),
            "is_hevc": probe.get("is_hevc"),
        },
    })


@app.route("/analyze-reframe", methods=["POST"])
def analyze_reframe_endpoint():
    """Kick off diarization + face tracking for *job_id*. Returns 200 with
    {status: 'started'} immediately; poll /reframe-status/<job_id> for completion.
    On completion the result lives in uploads/<job>_reframe.json and the
    job's `reframe_ready` flag flips True.

    Optional JSON body keys (speed / quality knobs):
      num_speakers, min_speakers, max_speakers — passed to pyannote clustering

    Returns 400 with a helpful message if the optional reframe deps aren't
    installed yet — better than a stack trace in the user's face.
    """
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job — select a transcribed video first"}), 404
    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Source video missing for this job"}), 404
    ok, msg = _reframe_deps_available()
    if not ok:
        return jsonify({"error": msg}), 400

    def _opt_int(key: str) -> int | None:
        raw = data.get(key)
        if raw is None or raw == "":
            return None
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    num_speakers = _opt_int("num_speakers")
    min_speakers = _opt_int("min_speakers")
    max_speakers = _opt_int("max_speakers")

    # Clear stale Analyze errors so polling doesn't immediately short-circuit
    # on a previous failure while the new worker is still starting.
    job = jobs[job_id]
    job["reframe_error"] = None
    job["reframe_ready"] = False
    # Don't clobber a real transcription error with reframe leftovers.
    prev_err = job.get("error") or ""
    if "Reframe analysis failed" in prev_err or "reframe" in prev_err.lower():
        job["error"] = None
    job["status"] = "analysing speakers"
    job["progress"] = 5
    try:
        _db_save_job(job_id)
    except Exception:
        pass

    t = threading.Thread(
        target=analyze_reframe_job,
        args=(job_id, video_path, num_speakers, min_speakers, max_speakers),
    )
    t.daemon = True
    t.start()
    return jsonify({
        "job_id": job_id,
        "status": "started",
        "diarization_device": _diarization_device_resolved or DIARIZATION_DEVICE,
        "diarization_model": DIARIZATION_MODEL,
    })


def _speaker_breakdown(diar: list) -> list:
    """Per-speaker speech seconds + % for Ingest cards."""
    totals: dict[str, float] = {}
    for seg in diar or []:
        spk = seg.get("speaker") or "UNKNOWN"
        try:
            dur = max(0.0, float(seg.get("end", 0)) - float(seg.get("start", 0)))
        except (TypeError, ValueError):
            dur = 0.0
        totals[spk] = totals.get(spk, 0.0) + dur
    total = sum(totals.values()) or 1.0
    labels = ("Host", "Guest", "Speaker 3", "Speaker 4", "Speaker 5")
    out = []
    for i, spk in enumerate(sorted(totals.keys())):
        sec = totals[spk]
        out.append({
            "id": spk,
            "label": labels[i] if i < len(labels) else f"Speaker {i + 1}",
            "speech_sec": round(sec, 1),
            "speech_pct": round(100.0 * sec / total),
        })
    return out


def assign_speakers_to_words(words: list, diarization: list) -> list:
    """Stamp each word with the diarization speaker at its midpoint."""
    if not words or not diarization:
        return words or []
    segs = sorted(
        (s for s in diarization if s.get("speaker") is not None),
        key=lambda s: float(s.get("start", 0) or 0),
    )
    if not segs:
        return words
    out = []
    for w in words:
        nw = dict(w) if isinstance(w, dict) else {"word": str(w)}
        try:
            mid = (float(nw.get("start", 0)) + float(nw.get("end", 0))) / 2.0
        except (TypeError, ValueError):
            out.append(nw)
            continue
        spk = None
        for seg in segs:
            try:
                a = float(seg.get("start", 0))
                b = float(seg.get("end", 0))
            except (TypeError, ValueError):
                continue
            if a <= mid <= b:
                spk = seg.get("speaker")
                break
        if spk:
            nw["speaker"] = spk
        out.append(nw)
    return out


def _stamp_job_speakers(job_id: str, diarization: list | None = None) -> int:
    """Persist speaker labels onto the job's words. Returns # words stamped."""
    job = jobs.get(job_id)
    if not job:
        return 0
    diar = diarization
    if diar is None:
        cache_path = UPLOAD_DIR / f"{job_id}_reframe.json"
        if not cache_path.exists():
            return 0
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            diar = _effective_diar_for_words(data)
        except (json.JSONDecodeError, OSError):
            return 0
    words = job.get("words") or []
    if not words or not diar:
        return 0
    stamped = assign_speakers_to_words(words, diar)
    job["words"] = stamped
    _db_save_job(job_id)
    return sum(1 for w in stamped if w.get("speaker"))


@app.route("/reframe-status/<job_id>")
def reframe_status(job_id: str):
    """Return cached reframe analysis summary if it exists, plus any
    worker error so the UI can stop polling on a silent crash. The full
    faces array can get large so we surface just the stats here; full
    payload is fetched at burn time, not over the wire."""
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    job = jobs[job_id]
    cache_path = UPLOAD_DIR / f"{job_id}_reframe.json"
    if cache_path.exists() and job.get("reframe_ready", True):
        # Prefer cache when ready. During a re-analyze, reframe_ready is False
        # so we keep reporting progress instead of the stale cache.
        try:
            # If a re-analyze is in flight, don't claim ready from old cache.
            status = (job.get("status") or "").lower()
            analysing = (
                "analys" in status or "speaker" in status or "face" in status
            ) and not job.get("reframe_ready")
            if not analysing:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                stats = dict(data.get("stats") or {})
                diar = data.get("diarization") or []
                breakdown = _speaker_breakdown(diar)
                if breakdown:
                    stats["speaker_breakdown"] = breakdown
                    stats["speakers"] = [s["id"] for s in breakdown]
                    stats["speaker_count"] = len(breakdown)
                return jsonify({"ready": True, "stats": stats})
        except (json.JSONDecodeError, OSError) as e:
            return jsonify({"ready": False, "error": str(e)}), 200

    status = job.get("status") or ""
    analysing = (
        "analys" in status.lower()
        or "speaker" in status.lower()
        or "face" in status.lower()
    ) and not job.get("reframe_ready")

    # While a run is in flight, never surface a stale reframe_error.
    if analysing:
        return jsonify({
            "ready": False,
            "status": status,
            "progress": job.get("progress"),
            "diarization_device": _diarization_device_resolved,
        }), 200

    err = job.get("reframe_error") or ""
    if not err:
        # Back-compat for older jobs that stuffed Analyze failures into error.
        legacy = job.get("error") or ""
        if "Reframe analysis failed" in legacy or "reframe analysis" in legacy.lower():
            err = legacy
    if err:
        return jsonify({"ready": False, "error": err}), 200

    return jsonify({
        "ready": False,
        "status": None,
        "progress": None,
        "diarization_device": _diarization_device_resolved,
    }), 200


@app.route("/reframe-swap-speakers", methods=["POST"])
def reframe_swap_speakers():
    """Swap the SPEAKER_00 / SPEAKER_01 labels in the cached reframe JSON.

    This corrects misassigned speaker-to-face mapping without re-running
    the full 3–7 minute diarization + face-detection pipeline.
    The next render will use the swapped assignment.
    """
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404

    cache_path = UPLOAD_DIR / f"{job_id}_reframe.json"
    if not cache_path.exists():
        return jsonify({"error": "No analysis cache found — run Analyze first"}), 400

    try:
        reframe_data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return jsonify({"error": f"Could not read cache: {e}"}), 500

    reframe_data["swap_speaker_voices"] = not reframe_data.get("swap_speaker_voices", False)
    cache_path.write_text(json.dumps(reframe_data, ensure_ascii=False), encoding="utf-8")
    # Re-stamp words using the effective (post-swap) speaker mapping.
    stamped = _stamp_job_speakers(job_id, _effective_diar_for_words(reframe_data))
    return jsonify({"ok": True, "swapped": reframe_data["swap_speaker_voices"], "words_stamped": stamped})


def _effective_diar_for_words(reframe_data: dict) -> list:
    """Diarization segments with Host/Guest swap applied for word labeling."""
    diar = reframe_data.get("diarization") or []
    if not reframe_data.get("swap_speaker_voices"):
        return diar
    out = []
    for seg in diar:
        ns = dict(seg)
        spk = ns.get("speaker")
        if spk == "SPEAKER_00":
            ns["speaker"] = "SPEAKER_01"
        elif spk == "SPEAKER_01":
            ns["speaker"] = "SPEAKER_00"
        out.append(ns)
    return out


@app.route("/stamp-speakers/<job_id>", methods=["POST"])
def stamp_speakers(job_id: str):
    """Label each transcript word with its diarization speaker (idempotent)."""
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    cache_path = UPLOAD_DIR / f"{job_id}_reframe.json"
    if not cache_path.exists():
        return jsonify({"error": "No diarization cache — run Analyze first"}), 400
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return jsonify({"error": str(e)}), 500
    n = _stamp_job_speakers(job_id, _effective_diar_for_words(data))
    return jsonify({"ok": True, "words_stamped": n})


@app.route("/reframe-speaker-avatar/<job_id>/<speaker_id>")
def reframe_speaker_avatar(job_id: str, speaker_id: str):
    """Return a cropped JPEG avatar face of a specific speaker."""
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    cache_path = UPLOAD_DIR / f"{job_id}_reframe.json"
    if not cache_path.exists():
        return jsonify({"error": "No reframe cache"}), 404
    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Source video missing"}), 404

    try:
        reframe_data = json.loads(cache_path.read_text(encoding="utf-8"))
        diar = reframe_data.get("diarization") or []
        faces = reframe_data.get("faces") or []
        speaker_positions, speaker_bboxes = _assign_speakers_to_faces(diar, faces)

        pos = speaker_positions.get(speaker_id, (0.5, 0.5))
        bbox = speaker_bboxes.get(speaker_id, (pos[0], pos[1], 0.2, 0.2))

        # Find sample time
        segs = [s for s in diar if s["speaker"] == speaker_id]
        sample_t = (segs[0]["start"] + segs[0]["end"]) / 2 if segs else 1.0

        import cv2
        cap = cv2.VideoCapture(str(video_path))
        cap.set(cv2.CAP_PROP_POS_MSEC, int(sample_t * 1000))
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return jsonify({"error": "Frame read failed"}), 500

        fh, fw = frame.shape[:2]
        cx, cy, w, h = bbox
        x1 = max(0, min(fw - 1, int((cx - w * 1.2) * fw)))
        x2 = max(x1 + 1, min(fw, int((cx + w * 1.2) * fw)))
        y1 = max(0, min(fh - 1, int((cy - h * 1.2) * fh)))
        y2 = max(y1 + 1, min(fh, int((cy + h * 1.2) * fh)))

        cropped = frame[y1:y2, x1:x2]
        resized = cv2.resize(cropped, (120, 120))
        ok, buf = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            return jsonify({"error": "JPEG encode failed"}), 500
        return Response(buf.tobytes(), mimetype="image/jpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/reframe-preview-crop/<job_id>/<panel>")
def reframe_preview_crop(job_id: str, panel: str):
    """Generate a quick JPEG crop thumbnail of top or bottom panel for live UI preview."""
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    cache_path = UPLOAD_DIR / f"{job_id}_reframe.json"
    if not cache_path.exists():
        return jsonify({"error": "No reframe cache"}), 404
    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Source video missing"}), 404

    top_panel = request.args.get("top") or "active"
    bottom_panel = request.args.get("bottom") or "full"
    try:
        reframe_data = json.loads(cache_path.read_text(encoding="utf-8"))
        src_w, src_h = get_video_dimensions(video_path)
        plan = compute_reframe_plan(reframe_data, src_w, src_h, 30.0, top_panel=top_panel, bottom_panel=bottom_panel)
        if not plan:
            return jsonify({"error": "Empty plan"}), 400

        seg = None
        for s in plan:
            if s.get("crops") and len(s["crops"]) >= 2:
                seg = s
                break
        if not seg:
            seg = plan[0]
            crops = [seg.get("crop", (0, 0, src_w, src_h)), seg.get("crop", (0, 0, src_w, src_h))]
        else:
            crops = seg["crops"]

        crop_idx = 0 if panel == "top" else 1
        x, y, w, h = crops[crop_idx]

        import cv2
        cap = cv2.VideoCapture(str(video_path))
        sample_t = seg.get("start", 0) + 0.5
        cap.set(cv2.CAP_PROP_POS_MSEC, int(sample_t * 1000))
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return jsonify({"error": "Frame read failed"}), 500

        fh, fw = frame.shape[:2]
        crop_y1 = max(0, min(fh - 1, y))
        crop_y2 = max(crop_y1 + 1, min(fh, y + h))
        crop_x1 = max(0, min(fw - 1, x))
        crop_x2 = max(crop_x1 + 1, min(fw, x + w))

        cropped = frame[crop_y1:crop_y2, crop_x1:crop_x2]
        resized = cv2.resize(cropped, (270, 240))
        ok, buf = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            return jsonify({"error": "JPEG encode failed"}), 500
        return Response(buf.tobytes(), mimetype="image/jpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/retranscribe", methods=["POST"])
def retranscribe():
    """Re-run Whisper on an existing job's video file. Returns 200 immediately;
    poll /status/<job_id> to see when the new words land.

    Body: {job_id, pre_clean?}. The pre_clean flag mirrors the upload-time
    option (extra denoise pass before Whisper).
    """
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({
            "error": "Source video missing. The original upload was deleted "
                     "after the previous render — re-upload to transcribe."
        }), 404
    pre_clean = bool(data.get("pre_clean"))
    t = threading.Thread(
        target=retranscribe_job, args=(job_id, video_path, pre_clean)
    )
    t.daemon = True
    t.start()
    return jsonify({"job_id": job_id, "status": "re-transcribing"})


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
    jobs[job_id]["style"] = _normalize_caption_style(style)
    jobs[job_id]["audio"] = audio
    jobs[job_id]["emoji_rules"] = emoji_rules
    jobs[job_id]["status"] = "queued"
    jobs[job_id]["progress"] = 0
    jobs[job_id]["output"] = None
    jobs[job_id]["error"] = None
    _db_save_job(job_id)

    t = threading.Thread(
        target=render_job,
        args=(job_id, video_path, words, jobs[job_id]["style"], audio, emoji_rules),
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
        # Recover from a prior transcription miss (e.g. silent upload) once
        # a usable transcript exists — needed for demo/UI workflows.
        if words and jobs[job_id].get("status") == "error":
            jobs[job_id]["status"] = "awaiting_edit"
            jobs[job_id]["error"] = None
            jobs[job_id]["progress"] = 100

    for key in ("style", "audio", "emoji_rules"):
        if key in data:
            val = data.get(key) or {}
            if key == "style":
                val = _normalize_caption_style(val) if val else {}
            jobs[job_id][key] = val

    if "clip_suggestions" in data and isinstance(data.get("clip_suggestions"), list):
        jobs[job_id]["clip_suggestions"] = data.get("clip_suggestions") or []
        if data.get("clip_format"):
            jobs[job_id]["clip_format"] = data.get("clip_format")

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
    # When stuck in error, attach a fresh probe so the UI can say "Retry" vs
    # "re-drop the full file" instead of a dead-end Transcribe alert.
    if payload.get("status") == "error" and payload["video_available"]:
        try:
            path = find_video_path(job_id)
            if path:
                payload["media_info"] = _probe_media_streams(path)
        except Exception:
            pass
    return jsonify(payload)


@app.route("/replace-and-transcribe", methods=["POST"])
def replace_and_transcribe():
    """Replace the source file on an existing (usually failed) job and re-run Whisper.

    Use this when Retry keeps saying "no audio" — the file on disk is often a
    truncated iPhone MOV; re-dropping onto the same job clears that dead-end.
    Form: job_id, video, optional pre_clean.

    Saves the new file to a temp path and validates BEFORE deleting the old
    source, so a bad second drop cannot leave the job with zero media.
    """
    job_id = (request.form.get("job_id") or "").strip()
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    if "video" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["video"]
    if not f.filename or not allowed_file(f.filename):
        return jsonify({"error": "Unsupported or empty file"}), 400

    ext = f.filename.rsplit(".", 1)[1].lower()
    staging = UPLOAD_DIR / f".{job_id}.replace_staging.{ext}"
    _safe_unlink(staging)
    expected_bytes = None
    try:
        if getattr(f, "content_length", None):
            expected_bytes = int(f.content_length)
    except (TypeError, ValueError):
        expected_bytes = None
    f.save(str(staging))
    try:
        probe = _validate_uploaded_media(staging, expected_bytes=expected_bytes)
        print(
            f"[replace] {job_id} ok size={staging.stat().st_size} "
            f"audio={probe.get('has_audio')} video={probe.get('has_video')} "
            f"name={f.filename!r}",
            flush=True,
        )
    except Exception as e:
        _safe_unlink(staging)
        return jsonify({"error": str(e)}), 400

    # Validated — now swap in as the job source.
    old = find_video_path(job_id)
    if old:
        _safe_unlink(old)
    for extra in UPLOAD_DIR.glob(f".{job_id}.*"):
        # Keep the staging file until we rename it.
        if extra == staging:
            continue
        _safe_unlink(extra)
    _safe_unlink(_edit_proxy_path(job_id))

    video_path = UPLOAD_DIR / f"{job_id}.{ext}"
    try:
        staging.replace(video_path)
    except OSError:
        shutil.move(str(staging), str(video_path))
    _safe_unlink(staging)

    jobs[job_id]["filename"] = f.filename
    jobs[job_id]["status"] = "queued"
    jobs[job_id]["progress"] = 0
    jobs[job_id]["error"] = None
    jobs[job_id]["words"] = None
    jobs[job_id]["media_info"] = probe
    jobs[job_id]["edit_proxy"] = False
    _db_save_job(job_id)
    pre_clean = request.form.get("pre_clean", "").lower() in ("1", "true", "yes")
    t = threading.Thread(target=retranscribe_job, args=(job_id, video_path, pre_clean))
    t.daemon = True
    t.start()
    return jsonify({"job_id": job_id, "status": "re-transcribing", "replaced": True})


def _format_srt_timestamp(seconds: float) -> str:
    """SRT format: HH:MM:SS,mmm (comma decimal separator)."""
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:  # rounding edge: 999.6 → 1000
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _format_vtt_timestamp(seconds: float) -> str:
    """VTT format: HH:MM:SS.mmm (period decimal separator)."""
    return _format_srt_timestamp(seconds).replace(",", ".")


def _words_to_caption_text(words: list, group_size: int, fmt: str) -> str:
    """Build SRT or VTT text from the job's words list.

    Groups follow the same chunking the renderer uses so the exported file
    matches the burned video's caption blocks 1:1. *fmt* is 'srt' or 'vtt'.
    """
    if fmt not in ("srt", "vtt"):
        raise ValueError(f"Unsupported caption format: {fmt}")
    is_vtt = (fmt == "vtt")
    ts = _format_vtt_timestamp if is_vtt else _format_srt_timestamp

    out: list[str] = []
    if is_vtt:
        out.append("WEBVTT\n")

    groups = group_words(words, group_size=max(1, group_size))
    cue_num = 0
    for group in groups:
        if not group:
            continue
        try:
            start = float(group[0]["start"])
            end = float(group[-1]["end"])
        except (TypeError, ValueError, KeyError):
            continue
        if end <= start:
            continue
        text = " ".join(str(w.get("word", "")).strip() for w in group if w.get("word"))
        if not text:
            continue
        cue_num += 1
        block = []
        if not is_vtt:
            block.append(str(cue_num))
        block.append(f"{ts(start)} --> {ts(end)}")
        block.append(text)
        out.append("\n".join(block))
    return ("\n\n".join(out) + "\n") if out else (("WEBVTT\n" if is_vtt else ""))


@app.route("/export-captions/<job_id>.<ext>")
def export_captions(job_id: str, ext: str):
    """Stream the active transcript as SRT or VTT.

    Groups are built from the job's stored *words* using the current style's
    group_size (default 3) so the exported file matches the burned video's
    caption cadence. The download filename uses the job's display label
    when present so users get sensible defaults.
    """
    ext = (ext or "").lower()
    if ext not in ("srt", "vtt"):
        return jsonify({"error": "Format must be srt or vtt"}), 400
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    job = jobs[job_id]
    words = job.get("words") or []
    if not words:
        return jsonify({"error": "Job has no transcript yet"}), 400
    style = job.get("style") or {}
    group_size = int(style.get("group_size", 3) or 3)
    body = _words_to_caption_text(words, group_size, ext)
    label = (job.get("filename") or job_id).rsplit(".", 1)[0]
    download_name = f"{label}.{ext}"
    mimetype = "text/vtt" if ext == "vtt" else "application/x-subrip"
    return Response(
        body,
        mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename=\"{download_name}\""},
    )


@app.route("/download/<path:filename>")
def download(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


@app.route("/preview/<path:filename>")
def preview(filename):
    return send_from_directory(OUTPUT_DIR, filename)


@app.route("/job-poster/<job_id>.jpg")
def job_poster(job_id: str):
    """Serve a single representative frame from the source video so the font
    preview can sit on top of real footage instead of a checkerboard.

    The frame is grabbed at ~0.5s (skipping black openings) and downscaled
    so it stays cheap to fetch. Cached on disk per-job; cleared if the
    source mtime is newer than the poster.
    """
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Source video missing"}), 404
    poster_path = UPLOAD_DIR / f"{job_id}_poster.jpg"
    if (
        not poster_path.exists()
        or poster_path.stat().st_mtime < video_path.stat().st_mtime
    ):
        cmd = [
            FFMPEG, "-y",
            # Grab frame 0 — if the user is editing a job that was itself
            # rendered from a previously-burned video, t=0.5s would already
            # show baked-in subtitles. Frame 0 is usually before any
            # subtitle event fires.
            "-i", str(video_path),
            "-vf", (
                "select=eq(n\\,0),"
                "scale='if(gt(iw,ih),min(720,iw),-2)':'if(gt(iw,ih),-2,min(720,ih))'"
            ),
            "-frames:v", "1",
            "-q:v", "4",
            str(poster_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not poster_path.exists():
            return jsonify({"error": "Could not extract poster frame"}), 500
    return send_from_directory(UPLOAD_DIR, poster_path.name)


@app.route("/job-canvas/<job_id>")
def job_canvas(job_id: str):
    """Return the burn-canvas dimensions for *job_id* so the font preview
    can mirror the actual output: aspect ratio, the size the renderer will
    use for libass (post-quality-boost), plus the source dims as a fallback."""
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Source video missing"}), 404
    try:
        src_w, src_h = get_video_dimensions(video_path)
    except Exception as exc:
        return jsonify({"error": f"Could not probe video: {exc}"}), 500
    quality_boost = bool((jobs[job_id].get("style") or {}).get("quality_boost"))
    burn_w, burn_h = src_w, src_h
    if quality_boost:
        burn_w, burn_h, _ = _quality_boost_scale(src_w, src_h)
    return jsonify({
        "source_width": src_w,
        "source_height": src_h,
        "burn_width": burn_w,
        "burn_height": burn_h,
        "quality_boost": quality_boost,
    })


@app.route("/fonts/<path:filename>")
def serve_font(filename):
    """Serve TTF/OTF files so the browser can preview subtitles in the same
    typeface libass will use at burn time."""
    return send_from_directory(FONT_DIR, filename)


@app.route("/list-fonts")
def list_fonts():
    """List font files available in FONT_DIR so the UI can register
    @font-face for everything that's actually on disk."""
    out = []
    for p in sorted(FONT_DIR.glob("*")):
        if p.suffix.lower() in (".ttf", ".otf"):
            out.append(p.name)
    return jsonify({"fonts": out})


@app.route("/raw-upload/<job_id>")
def raw_upload(job_id):
    """Stream video for the editor. Prefer the lightweight edit proxy when ready
    so large phone MOVs seek quickly; burns still use the original file."""
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    proxy = _edit_proxy_path(job_id)
    if proxy.exists() and proxy.stat().st_size > 64:
        return send_from_directory(UPLOAD_DIR, proxy.name)
    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Video not found"}), 404
    return send_from_directory(UPLOAD_DIR, video_path.name)


@app.route("/source-video/<job_id>")
def source_video(job_id):
    """Always the original upload (for burns / downloads that need full quality)."""
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


# ============================================================================
# TIMELINE EDITOR
# ----------------------------------------------------------------------------
# A multi-track video compositor built on top of the same FFmpeg primitives the
# rest of the app uses. A "timeline" is a JSON document with four track kinds:
#
#   main    — sequential video clips (trimmed slices of existing jobs/assets).
#             Each boundary may carry a crossfade transition.
#   overlay — B-roll / picture-in-picture / image overlays composited on top of
#             the main track at a position + size, gated to a time window.
#   text    — titles / lower-thirds, burned via libass (reuses the caption path).
#   music   — background audio tracks, gain-staged, with optional ducking under
#             the main voice.
#
# Rendering runs in four passes (base → music → overlays → titles) rather than
# one mega filter_complex. Passes are slower (re-encodes) but far easier to
# reason about and debug, and interview/red-carpet edits are rarely more than a
# handful of clips. Output lands in OUTPUT_DIR/{job_id}.mp4 like every other
# render, so the existing Result tab / preview / download all work unchanged.
# ============================================================================

ASSET_DIR = BASE_DIR / "assets"
ASSET_DIR.mkdir(exist_ok=True)

ASSET_EXT_VIDEO = {"mp4", "mov", "mkv", "webm", "avi", "m4v"}
ASSET_EXT_IMAGE = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}
ASSET_EXT_AUDIO = {"mp3", "wav", "m4a", "aac", "ogg", "flac"}

# Canvas presets the UI offers. Keys are sent by the client.
TIMELINE_CANVASES = {
    "9x16": (1080, 1920),
    "16x9": (1920, 1080),
    "1x1": (1080, 1080),
    "4x5": (1080, 1350),
}

# xfade transition names we expose in the UI -> the ffmpeg `transition=` value.
TIMELINE_TRANSITIONS = {
    "fade": "fade",
    "fadeblack": "fadeblack",
    "dissolve": "dissolve",
    "slideleft": "slideleft",
    "slideright": "slideright",
    "slideup": "slideup",
    "slidedown": "slidedown",
    "wipeleft": "wipeleft",
    "wiperight": "wiperight",
    "circleopen": "circleopen",
    "radial": "radial",
}


def _asset_kind(ext: str) -> str | None:
    ext = ext.lower().lstrip(".")
    if ext in ASSET_EXT_VIDEO:
        return "video"
    if ext in ASSET_EXT_IMAGE:
        return "image"
    if ext in ASSET_EXT_AUDIO:
        return "audio"
    return None


def _asset_meta_path(asset_id: str) -> Path:
    return ASSET_DIR / f"{asset_id}.meta.json"


def _write_asset_meta(asset_id: str, **fields) -> None:
    """Persist display metadata (original filename, keyword, source) beside an asset."""
    if not asset_id:
        return
    path = _asset_meta_path(asset_id)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    for k, v in fields.items():
        if v is not None and v != "":
            data[k] = v
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _read_asset_meta(asset_id: str) -> dict:
    path = _asset_meta_path(asset_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _find_asset_path(asset_id: str) -> Path | None:
    """Resolve an uploaded asset id to its file on disk (extension agnostic)."""
    if not asset_id or not re.fullmatch(r"[a-f0-9]{32}", asset_id):
        return None
    for p in ASSET_DIR.glob(f"{asset_id}.*"):
        if not p.is_file():
            continue
        # Skip sidecar metadata / non-media companions.
        if p.name.endswith(".meta.json") or p.suffix.lower() == ".json":
            continue
        if _asset_kind(p.suffix):
            return p
    return None


def _ff_color(hex_color: str, default: str = "black") -> str:
    """Convert '#RRGGBB' to ffmpeg's '0xRRGGBB' color form for filter args."""
    if not hex_color:
        return default
    s = hex_color.strip().lstrip("#")
    if len(s) == 6 and re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return "0x" + s.upper()
    return default


def _has_audio_stream(path: Path) -> bool:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return bool(out)
    except (OSError, subprocess.SubprocessError):
        return False


def _media_duration(path: Path) -> float:
    """Best-effort container duration in seconds (0.0 on failure)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return max(0.0, float(out)) if out and out != "N/A" else 0.0
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


def _timeline_clip_source(clip: dict) -> Path | None:
    """Resolve a timeline clip's media: either a source job's video or an asset."""
    sid = clip.get("source_job_id")
    if sid and sid in jobs:
        return find_video_path(sid)
    aid = clip.get("asset_id")
    if aid:
        return _find_asset_path(aid)
    return None


def _tl_run(cmd: list, what: str) -> None:
    """Run an ffmpeg command, raising a trimmed error on failure."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed: {proc.stderr[-800:]}")


# Color-grade presets (ffmpeg filter fragments). Mirrors the Descript-style
# None / Neutral / Warm / Cool swatches, plus a couple extras.
_TL_COLOR_PRESETS = {
    "none": "",
    "neutral": "eq=contrast=1.04:saturation=1.06",
    "warm": "colorbalance=rs=0.06:rm=0.04:gs=0.01:bs=-0.06:bm=-0.05,eq=saturation=1.08",
    "cool": "colorbalance=rs=-0.05:rm=-0.03:bs=0.07:bm=0.05,eq=saturation=1.04",
    "vivid": "eq=contrast=1.10:saturation=1.28",
    "bw": "hue=s=0,eq=contrast=1.05",
}


def _tl_color_filter(color: dict | None) -> str:
    """Build a color-grade filter chain from a clip's color settings.

    {preset, brightness(-0.3..0.3), contrast(0.5..1.5), saturation(0..2)}.
    Returns "" when nothing is set.
    """
    if not color:
        return ""
    parts = []
    preset = _TL_COLOR_PRESETS.get(color.get("preset", "none"), "")
    if preset:
        parts.append(preset)
    try:
        b = float(color.get("brightness", 0) or 0)
        c = float(color.get("contrast", 1) or 1)
        s = float(color.get("saturation", 1) or 1)
    except (TypeError, ValueError):
        b, c, s = 0.0, 1.0, 1.0
    if abs(b) > 1e-3 or abs(c - 1) > 1e-3 or abs(s - 1) > 1e-3:
        b = max(-1.0, min(1.0, b))
        c = max(0.0, min(3.0, c))
        s = max(0.0, min(3.0, s))
        parts.append(f"eq=brightness={b:.3f}:contrast={c:.3f}:saturation={s:.3f}")
    return ",".join(parts)


_KENBURNS_INTENSITY = {"low": 0.12, "med": 0.22, "high": 0.35}


def _tl_kenburns_filter(ken: dict, W: int, H: int, fps: int, dur: float) -> str:
    """Build a zoompan expression for a slow Ken Burns push-in / pull-out.

    Returns "" when disabled. The zoom ramps over the clip's frame count so the
    move always finishes on time regardless of clip length. Centered crop.
    """
    if not ken or not ken.get("enabled"):
        return ""
    amount = _KENBURNS_INTENSITY.get(ken.get("intensity", "med"), 0.22)
    direction = ken.get("direction", "in")
    frames = max(2, int(dur * fps))
    inc = amount / frames
    maxz = 1.0 + amount
    if direction == "out":
        # Start zoomed in (first frame, zoom var still 1.0), then ease back out.
        zexpr = f"if(lte(zoom,1.0),{maxz:.4f},max(zoom-{inc:.6f},1.0))"
    else:  # in
        zexpr = f"min(zoom+{inc:.6f},{maxz:.4f})"
    return (
        f"zoompan=z='{zexpr}':d=1:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={W}x{H}:fps={fps}"
    )


_PUNCH_PEAK = {"low": 1.15, "med": 1.25, "high": 1.40, "strong": 1.40}
PUNCH_DECAY_SECONDS = 0.45


def _tl_punch_zoom_filter(punch_cfg: dict | None, W: int, H: int,
                          fps: int = 30) -> str:
    """Build an animated punch zoom: hit the peak on the beat, ease back out.

    Returns "" when disabled.

    The zoom snaps to its peak at `hit` seconds and decays to 1.0 over `decay`
    seconds on a cubic ease-out, so the move lands hard and settles softly. A
    linear decay reads mechanical, which is the whole difference between a
    punch and a slow zoom.

    `anchor` is a normalised (x, y) to zoom toward — pass the speaker's face
    from the reframe analysis and the push lands on them rather than on the
    middle of the frame. Defaults to centre.

    crop can't do this: its w/h expressions are evaluated once at
    configuration, so the window size cannot change per frame. zoompan
    re-evaluates z every frame, which is why the zoom is built there.
    """
    if not punch_cfg or not punch_cfg.get("enabled"):
        return ""
    peak = _PUNCH_PEAK.get(punch_cfg.get("intensity", "med"), 1.25)
    amp = peak - 1.0
    if amp <= 0:
        return ""

    try:
        hit = max(0.0, float(punch_cfg.get("hit", 0.0)))
    except (TypeError, ValueError):
        hit = 0.0
    try:
        decay = max(0.05, float(punch_cfg.get("decay", PUNCH_DECAY_SECONDS)))
    except (TypeError, ValueError):
        decay = PUNCH_DECAY_SECONDS

    anchor = punch_cfg.get("anchor") or {}
    try:
        ax = min(1.0, max(0.0, float(anchor.get("x", 0.5))))
        ay = min(1.0, max(0.0, float(anchor.get("y", 0.5))))
    except (TypeError, ValueError):
        ax = ay = 0.5

    f0 = hit * max(1, fps)
    fd = max(1.0, decay * max(1, fps))
    # Progress through the decay, clamped so the frames before the hit read 0
    # and everything after the settle reads 1.
    u = f"min(1,max(0,(in-{f0:.3f})/{fd:.3f}))"
    # gte() holds the zoom at 1.0 until the hit; without it the clamp above
    # would park the frame at full zoom for the whole run-up.
    z = f"1+{amp:.4f}*pow(1-{u},3)*gte(in,{f0:.3f})"
    return (
        f"zoompan=z='{z}':"
        f"x='clip(iw*{ax:.4f}-(iw/zoom)/2,0,iw-iw/zoom)':"
        f"y='clip(ih*{ay:.4f}-(ih/zoom)/2,0,ih-ih/zoom)':"
        f"d=1:s={W}x{H}:fps={fps}"
    )


def _tl_normalize_segment(src: Path, t_in: float, t_out: float,
                          W: int, H: int, fps: int, fit: str, bg: str,
                          out_path: Path, ken: dict | None = None,
                          color: dict | None = None,
                          punch: dict | None = None) -> float:
    """Trim [t_in, t_out] of *src* and conform it to the WxH/fps canvas.

    Uses an MD5 hash cache to skip FFmpeg re-encoding for unchanged segments,
    speeding up timeline updates by up to 30x.
    """
    dur = max(0.05, t_out - t_in)
    
    # Calculate unique segment cache key
    mtime = src.stat().st_mtime if src.exists() else 0
    cache_raw = f"{src.resolve()}_{mtime}_{t_in:.3f}_{t_out:.3f}_{W}_{H}_{fps}_{fit}_{bg}_{json.dumps(ken, sort_keys=True) if ken else ''}_{json.dumps(color, sort_keys=True) if color else ''}_{json.dumps(punch, sort_keys=True) if punch else ''}"
    cache_hash = hashlib.md5(cache_raw.encode("utf-8")).hexdigest()
    cached_segment = CACHE_DIR / f"norm_{cache_hash}.mp4"

    if cached_segment.exists() and cached_segment.stat().st_size > 0:
        # Cache hit: copy cached segment directly without FFmpeg render!
        shutil.copy(cached_segment, out_path)
        return dur

    if fit == "contain":
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={_ff_color(bg)},setsar=1")
    else:  # cover
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H},setsar=1")
    vf += f",fps={fps}"
    kb = _tl_kenburns_filter(ken, W, H, fps, dur)
    if kb:
        vf += "," + kb
    pz = _tl_punch_zoom_filter(punch, W, H, fps)
    if pz:
        vf += "," + pz
    cf = _tl_color_filter(color)
    if cf:
        vf += "," + cf
    vf += ",format=yuv420p"

    has_audio = _has_audio_stream(src)
    cmd = [FFMPEG, "-y", "-ss", f"{t_in:.3f}", "-i", str(src)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd += ["-t", f"{dur:.3f}", "-vf", vf,
            *_VIDEO_ENC_ARGS, "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]
    if has_audio:
        cmd += ["-af", _tl_declick_af(dur)]
    if not has_audio:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    cmd += [str(out_path)]
    _tl_run(cmd, "Clip trim")

    # Store in cache for future instant re-renders
    try:
        shutil.copy(out_path, cached_segment)
    except Exception:
        pass
    return dur


def _tl_declick_af(dur: float, d: float = 0.02) -> str:
    """Short fade-in + fade-out so segment joins don't pop."""
    d = min(d, max(0.005, dur / 4))
    return f"afade=t=in:st=0:d={d:.3f},afade=t=out:st={max(0.0, dur - d):.3f}:d={d:.3f}"


def _tl_split_segment(srcA: Path, inA: float, srcB: Path, inB: float,
                      dur: float, layout: str, W: int, H: int, fps: int,
                      out_path: Path, color: dict | None = None,
                      placement: str | None = None) -> float:
    """Render a split-screen segment: two sources shown at once.

    layout 'side' = left/right halves; 'stack' = top/bottom halves; 'auto'
    picks stack for portrait/square canvases and side for landscape.

    placement controls where the *second* video (srcB) sits:
      stack: 'second_top' | 'second_bottom' (default bottom)
      side:  'second_left' | 'second_right' (default right)
    Legacy: placement 'swap' flips A/B panels.

    Audio is taken from source A. Each half is center-cropped to fill its panel.
    An optional *color* grade is applied to the combined frame.
    """
    if layout == "auto":
        layout = "stack" if H >= W else "side"
    place = (placement or "").strip().lower()
    if place in ("swap", "flipped", "second_first"):
        # Legacy swap flag → concrete placement
        place = "second_top" if layout != "side" else "second_left"
    if layout == "side":
        pw, ph = W // 2, H
        stack = "hstack"
        # Default: Main left, second right
        second_first = place in ("second_left", "left", "main_right")
    else:
        pw, ph = W, H // 2
        stack = "vstack"
        # Default: Main top, second bottom
        second_first = place in ("second_top", "top", "main_bottom")
    pw -= pw % 2
    ph -= ph % 2

    def panel(idx):
        return (f"[{idx}:v]scale={pw}:{ph}:force_original_aspect_ratio=increase,"
                f"crop={pw}:{ph},setsar=1,fps={fps}[p{idx}]")

    hasA = _has_audio_stream(srcA)
    cf = _tl_color_filter(color)
    post = ("," + cf) if cf else ""
    if second_first:
        order = "[p1][p0]"
    else:
        order = "[p0][p1]"
    fc = (f"{panel(0)};{panel(1)};"
          f"{order}{stack}=inputs=2{post},format=yuv420p[v]")
    cmd = [FFMPEG, "-y",
           "-ss", f"{inA:.3f}", "-i", str(srcA),
           "-ss", f"{inB:.3f}", "-i", str(srcB)]
    if not hasA:
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    audio_idx = "2:a:0" if not hasA else "0:a:0"
    cmd += ["-t", f"{dur:.3f}", "-filter_complex", fc,
            "-map", "[v]", "-map", audio_idx,
            *_VIDEO_ENC_ARGS, "-c:a", "aac", "-b:a", "192k",
            "-ar", "44100", "-ac", "2"]
    if hasA:
        cmd += ["-af", _tl_declick_af(dur)]
    cmd += ["-shortest", str(out_path)]
    _tl_run(cmd, "Split-screen")
    return dur


def _tl_apply_logo(base: Path, logo: dict, W: int, H: int, out_path: Path) -> None:
    """Overlay a persistent logo/watermark across the whole video."""
    path = _find_asset_path(logo.get("asset_id", "")) if logo else None
    if not path:
        _tl_run([FFMPEG, "-y", "-i", str(base), "-c", "copy", str(out_path)],
                "Logo passthrough")
        return
    wfrac = min(1.0, max(0.03, float(logo.get("w", 0.18))))
    lw = max(2, int(W * wfrac) // 2 * 2)
    x = int(W * float(logo.get("x", 0.04)))
    y = int(H * float(logo.get("y", 0.04)))
    opacity = min(1.0, max(0.0, float(logo.get("opacity", 0.9))))
    is_video = path.suffix.lower().lstrip(".") in ASSET_EXT_VIDEO
    inputs = ["-i", str(base)]
    if is_video:
        inputs += ["-stream_loop", "-1", "-i", str(path)]
    else:
        inputs += ["-i", str(path)]
    prep = f"[1:v]scale={lw}:-2,setsar=1,format=yuva420p,colorchannelmixer=aa={opacity:.3f}[lg]"
    fc = f"{prep};[0:v][lg]overlay=x={x}:y={y}:shortest=1[v]"
    _tl_run(
        [FFMPEG, "-y", *inputs, "-filter_complex", fc,
         "-map", "[v]", "-map", "0:a?", *_VIDEO_ENC_ARGS,
         "-c:a", "copy", str(out_path)],
        "Logo overlay",
    )


def _tl_concat(paths: list, out_path: Path) -> None:
    """Concatenate identically-formatted segments with the concat demuxer."""
    list_path = out_path.with_suffix(".concat.txt")
    list_path.write_text("\n".join(f"file '{p.absolute()}'" for p in paths) + "\n")
    try:
        _tl_run(
            [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
             "-c", "copy", "-movflags", "+faststart", str(out_path)],
            "Concat",
        )
    finally:
        _safe_unlink(list_path)


def _tl_build_main_track(segments: list, transitions: list,
                         out_path: Path, job_id: str) -> tuple[float, list]:
    """Stitch normalized *segments* into one clip.

    *segments* is [(path, duration)]; *transitions[i]* is the crossfade
    duration (seconds) between segment i and i+1, or 0 for a hard cut. When no
    crossfades are requested we use the cheap concat demuxer; otherwise we fold
    the clips together pairwise (xfade for crossfades, filter-concat for cuts).
    Returns (total_duration, seg_output_starts) where seg_output_starts[i] is
    the time on the final timeline where segment i begins — used to place
    captions through the same trims/cuts/transitions.
    """
    if not segments:
        raise RuntimeError("Main track has no clips")
    if len(segments) == 1:
        seg_path, dur = segments[0]
        # Copy the lone segment to the expected output name.
        _tl_run([FFMPEG, "-y", "-i", str(seg_path), "-c", "copy",
                 "-movflags", "+faststart", str(out_path)], "Finalize main")
        return dur, [0.0]

    # transitions[i] is a transition-type string (truthy) or 0/None for a cut.
    if not any(transitions):
        _tl_concat([p for p, _ in segments], out_path)
        starts, acc = [], 0.0
        for _, d in segments:
            starts.append(acc)
            acc += d
        return acc, starts

    intermediates: list[Path] = []
    try:
        cur_path, cur_dur = segments[0]
        seg_starts = [0.0]
        for i in range(1, len(segments)):
            nxt_path, nxt_dur = segments[i]
            tname = transitions[i - 1] if i - 1 < len(transitions) else 0
            step_out = UPLOAD_DIR / f"{job_id}_tlmix{i:03d}.mp4"
            if tname:
                xfade = TIMELINE_TRANSITIONS.get(str(tname), "fade")
                tdur = min(0.8, cur_dur * 0.45, nxt_dur * 0.45)
                tdur = max(0.2, tdur)
                offset = max(0.0, cur_dur - tdur)
                fc = (
                    f"[0:v][1:v]xfade=transition={xfade}:duration={tdur:.3f}:"
                    f"offset={offset:.3f},format=yuv420p[v];"
                    f"[0:a][1:a]acrossfade=d={tdur:.3f}[a]"
                )
                seg_starts.append(offset)
                cur_dur = cur_dur + nxt_dur - tdur
            else:
                fc = "[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[v][a]"
                seg_starts.append(cur_dur)
                cur_dur = cur_dur + nxt_dur
            _tl_run(
                [FFMPEG, "-y", "-i", str(cur_path), "-i", str(nxt_path),
                 "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                 *_VIDEO_ENC_ARGS, "-c:a", "aac", "-b:a", "192k", str(step_out)],
                f"Transition {i}",
            )
            intermediates.append(step_out)
            cur_path = step_out
        _tl_run([FFMPEG, "-y", "-i", str(cur_path), "-c", "copy",
                 "-movflags", "+faststart", str(out_path)], "Finalize main")
        return cur_dur, seg_starts
    finally:
        for p in intermediates:
            _safe_unlink(p)


def _tl_mix_music(base: Path, music_clips: list, out_path: Path) -> None:
    """Mix background music tracks into *base*'s audio (video stream copied).

    Each clip is trimmed, gain-staged and time-shifted to its start. If any
    clip requests ducking, all music is side-chain compressed against the main
    voice so speech stays on top.
    """
    resolved = []
    for m in music_clips:
        path = _timeline_clip_source(m)
        if path:
            resolved.append((m, path))
    if not resolved:
        # Nothing usable — just pass the base through.
        _tl_run([FFMPEG, "-y", "-i", str(base), "-c", "copy", str(out_path)],
                "Music passthrough")
        return

    inputs = ["-i", str(base)]
    filt = ["[0:a]asplit=2[voice][key]"]
    music_labels = []
    duck = False
    for idx, (m, path) in enumerate(resolved, start=1):
        inputs += ["-i", str(path)]
        m_in = max(0.0, float(m.get("in", 0)))
        m_out = float(m.get("out", m_in + 30))
        if m_out <= m_in:
            m_out = m_in + 30
        start = max(0.0, float(m.get("start", 0)))
        gain = float(m.get("gain_db", -18))
        delay = int(start * 1000)
        if m.get("duck"):
            duck = True
        filt.append(
            f"[{idx}:a]aformat=sample_fmts=fltp:sample_rates=44100:"
            f"channel_layouts=stereo,"
            f"atrim=start={m_in:.3f}:end={m_out:.3f},"
            f"asetpts=PTS-STARTPTS,volume={gain:.2f}dB,"
            f"adelay={delay}|{delay}[m{idx}]"
        )
        music_labels.append(f"[m{idx}]")

    if len(music_labels) == 1:
        filt.append(f"{music_labels[0]}anull[musicall]")
    else:
        filt.append(f"{''.join(music_labels)}amix=inputs={len(music_labels)}:"
                    f"duration=longest:normalize=0[musicall]")

    if duck:
        filt.append("[musicall][key]sidechaincompress=threshold=0.03:ratio=8:"
                    "attack=20:release=400[musicfinal]")
    else:
        filt.append("[musicall]anull[musicfinal]")

    filt.append("[voice][musicfinal]amix=inputs=2:duration=first:normalize=0[aout]")

    _tl_run(
        [FFMPEG, "-y", *inputs, "-filter_complex", ";".join(filt),
         "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-shortest", str(out_path)],
        "Music mix",
    )


def _tl_overlay_scale_filter(ow: int, oh: int, fit: str) -> str:
    """FFmpeg scale/crop/pad chain for overlay fit modes."""
    fit = (fit or "cover").lower()
    if fit == "contain":
        return (
            f"scale={ow}:{oh}:force_original_aspect_ratio=decrease,"
            f"pad={ow}:{oh}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        )
    if fit == "fill":
        return f"scale={ow}:{oh},setsar=1"
    # cover (default): fill box, crop overflow
    return (
        f"scale={ow}:{oh}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{oh},setsar=1"
    )


def _tl_composite_overlays(base: Path, overlay_clips: list,
                           W: int, H: int, out_path: Path,
                           fps: int = 30) -> None:
    """Composite B-roll / PiP / image overlays onto *base* (audio copied).

    Honors: start, in/out, x, y, w, h, opacity, fit (cover|contain|fill),
    fade_in / fade_out, border_px, ken_burns (slow zoom on the PiP box).
    """
    resolved = []
    for ov in overlay_clips:
        path = _timeline_clip_source(ov)
        if not path:
            continue
        kind = "image" if path.suffix.lower().lstrip(".") in ASSET_EXT_IMAGE else "video"
        resolved.append((ov, path, kind))
    if not resolved:
        _tl_run([FFMPEG, "-y", "-i", str(base), "-c", "copy", str(out_path)],
                "Overlay passthrough")
        return

    inputs = ["-i", str(base)]
    filt = []
    cur = "[0:v]"
    in_idx = 1
    fps = max(15, min(60, int(fps or 30)))
    for n, (ov, path, kind) in enumerate(resolved, start=1):
        start = max(0.0, float(ov.get("start", 0)))
        o_in = max(0.0, float(ov.get("in", 0)))
        o_out = float(ov.get("out", o_in + 4))
        length = max(0.2, o_out - o_in)
        wfrac = min(1.0, max(0.05, float(ov.get("w", 0.4))))
        if ov.get("h") is not None:
            try:
                hfrac = min(1.0, max(0.05, float(ov.get("h"))))
            except (TypeError, ValueError):
                hfrac = wfrac * 9 / 16
        else:
            # Default PiP box ≈ 16:9 relative to canvas width.
            hfrac = min(1.0, max(0.05, (wfrac * W * 9 / 16) / max(1, H)))
        ow = max(2, int(W * wfrac) // 2 * 2)
        oh = max(2, int(H * hfrac) // 2 * 2)
        x = int(W * float(ov.get("x", 0.5)))
        y = int(H * float(ov.get("y", 0.5)))
        # Keep overlay on-canvas
        x = max(0, min(W - ow, x))
        y = max(0, min(H - oh, y))
        opacity = min(1.0, max(0.0, float(ov.get("opacity", 1.0))))
        fit = str(ov.get("fit") or "cover")
        try:
            fade_in = max(0.0, min(length / 2, float(ov.get("fade_in") or 0)))
        except (TypeError, ValueError):
            fade_in = 0.0
        try:
            fade_out = max(0.0, min(length / 2, float(ov.get("fade_out") or 0)))
        except (TypeError, ValueError):
            fade_out = 0.0
        try:
            border = max(0, min(24, int(ov.get("border_px") or 0)))
        except (TypeError, ValueError):
            border = 0

        scale = _tl_overlay_scale_filter(ow, oh, fit)
        if kind == "image":
            inputs += ["-loop", "1", "-t", f"{length:.3f}", "-i", str(path)]
            prep = f"[{in_idx}:v]{scale}"
        else:
            inputs += ["-ss", f"{o_in:.3f}", "-t", f"{length:.3f}", "-i", str(path)]
            prep = f"[{in_idx}:v]{scale},setpts=PTS-STARTPTS"
        # Ken Burns on the PiP box (photo / short B-roll moments).
        kb = _tl_kenburns_filter(ov.get("ken_burns"), ow, oh, fps, length)
        if kb:
            prep += "," + kb
        if border > 0:
            prep += (
                f",pad={ow + border * 2}:{oh + border * 2}:{border}:{border}:white"
            )
            # Re-clamp position for padded size
            x = max(0, min(W - (ow + border * 2), x - border))
            y = max(0, min(H - (oh + border * 2), y - border))
        need_alpha = opacity < 1.0 or fade_in > 0 or fade_out > 0
        if need_alpha:
            prep += ",format=yuva420p"
            if opacity < 1.0:
                prep += f",colorchannelmixer=aa={opacity:.3f}"
            if fade_in > 0:
                prep += f",fade=t=in:st=0:d={fade_in:.3f}:alpha=1"
            if fade_out > 0:
                st = max(0.0, length - fade_out)
                prep += f",fade=t=out:st={st:.3f}:d={fade_out:.3f}:alpha=1"
        # Delay the overlay so it lands at `start` on the timeline.
        prep += f",tpad=start_duration={start:.3f}"
        prep += f"[ov{n}]"
        filt.append(prep)
        filt.append(
            f"{cur}[ov{n}]overlay=x={x}:y={y}:"
            f"enable='between(t,{start:.3f},{start + length:.3f})'[bg{n}]"
        )
        cur = f"[bg{n}]"
        in_idx += 1

    _tl_run(
        [FFMPEG, "-y", *inputs, "-filter_complex", ";".join(filt),
         "-map", cur, "-map", "0:a?", *_VIDEO_ENC_ARGS,
         "-c:a", "copy", str(out_path)],
        "Overlay composite",
    )


def _tl_build_titles_ass(text_clips: list, W: int, H: int) -> str:
    """Build an ASS file for the text track (titles / lower-thirds).

    Each clip gets its own [V4+] style so per-clip font/size/color/box settings
    can coexist. Positioning is via \\pos; lower-thirds use an opaque box
    (BorderStyle 4). Animation is a fade by default, optional slide-up via
    \\move.
    """
    styles = []
    events = []
    for i, c in enumerate(text_clips):
        text = str(c.get("text", "")).strip()
        if not text:
            continue
        try:
            start = float(c.get("start", 0))
            end = float(c.get("out", start + 3))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue

        font = c.get("font", "Anton")
        size = int(c.get("size", 64))
        primary = hex_to_ass_color(c.get("color", "#FFFFFF"))
        outline_col = hex_to_ass_color(c.get("outline_color", "#000000"))
        has_box = bool(c.get("bg_enabled"))
        back = hex_to_ass_color(c.get("bg_color", "#000000"))
        if has_box:
            # Apply box opacity (0..1) to the BackColour alpha byte.
            try:
                op = min(1.0, max(0.0, float(c.get("bg_opacity", 0.6))))
            except (TypeError, ValueError):
                op = 0.6
            alpha = int((1.0 - op) * 255)
            back = f"&H{alpha:02X}{back[4:]}"  # splice alpha into &HAABBGGRR
        # BorderStyle 3 = opaque box (libass-standard); the Outline field then
        # controls the box padding around the text. BorderStyle 1 = outline.
        border_style = 3 if has_box else 1
        outline_w = 10 if has_box else int(c.get("outline_width", 3))
        shadow = int(c.get("shadow", 0))
        bold = 1 if c.get("bold", True) else 0
        align = int(c.get("align", 2))  # numpad alignment, 2 = bottom-center

        styles.append(
            f"Style: T{i},{font},{size},{primary},{primary},{outline_col},{back},"
            f"{bold},0,0,0,100,100,0,0,{border_style},{outline_w},{shadow},"
            f"{align},40,40,40,1"
        )

        px = int(W * float(c.get("x", 0.5)))
        py = int(H * float(c.get("y", 0.85)))
        anim = c.get("anim", "fade")
        if anim == "slideup":
            pos = f"\\move({px},{py + 60},{px},{py},0,400)"
        elif anim == "none":
            pos = f"\\pos({px},{py})"
        else:
            pos = f"\\pos({px},{py})"
        fade = "" if anim == "none" else "\\fad(300,300)"
        body = text.replace("\n", "\\N").replace("{", "").replace("}", "")
        events.append(
            f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},T{i},,0,0,0,,"
            f"{{{pos}{fade}}}{body}"
        )

    header = (
        "[Script Info]\n"
        "Title: Timeline Titles\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {W}\n"
        f"PlayResY: {H}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(styles) + "\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    return header + "\n".join(events) + "\n"


_TL_DEFAULT_CAPTION_STYLE = _DEFAULT_CAPTION_STYLE


def _tl_compose_ass(W: int, H: int, *ass_docs: str) -> str:
    """Merge several ASS docs into one (single libass burn pass).

    Pulls every ``Style:`` and ``Dialogue:`` line out of each input doc and
    rebuilds a single document with one header. Robust to ordering because it
    matches on line prefixes, not positions. Style names are assumed unique
    across docs (captions use ``Default``; titles use ``T0..Tn``).
    """
    styles, events = [], []
    for doc in ass_docs:
        if not doc:
            continue
        for line in doc.splitlines():
            if line.startswith("Style:"):
                styles.append(line)
            elif line.startswith("Dialogue:"):
                events.append(line)
    header = (
        "[Script Info]\n"
        "Title: Timeline Captions+Titles\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {W}\n"
        f"PlayResY: {H}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(styles) + "\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    return header + "\n".join(events) + "\n"


def _tl_keep_ranges(t_in: float, t_out: float, cuts: list) -> list:
    """Subtract *cuts* from [t_in, t_out], returning the kept (start, end) spans.

    Used by text-based editing: cut spans (in source seconds) are removed and
    the surviving pieces are stitched back together. Tiny (<0.1s) survivors are
    dropped so we don't emit degenerate segments.
    """
    if not cuts:
        return [(t_in, t_out)]
    # Clamp + sort cuts that intersect the clip window.
    norm = []
    for cut in cuts:
        try:
            cs = max(t_in, float(cut[0]))
            ce = min(t_out, float(cut[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if ce > cs:
            norm.append((cs, ce))
    if not norm:
        return [(t_in, t_out)]
    norm.sort()
    keep = []
    cursor = t_in
    for cs, ce in norm:
        if cs > cursor:
            keep.append((cursor, cs))
        cursor = max(cursor, ce)
    if cursor < t_out:
        keep.append((cursor, t_out))
    return [(s, e) for s, e in keep if e - s >= 0.1]


def _normalize_timeline(timeline: dict) -> dict:
    """Validate + coerce a timeline doc to a safe canonical form."""
    tl = dict(timeline or {})
    canvas = tl.get("canvas", "9x16")
    if canvas not in TIMELINE_CANVASES:
        canvas = "9x16"
    tl["canvas"] = canvas
    try:
        tl["fps"] = max(15, min(60, int(tl.get("fps", 30))))
    except (TypeError, ValueError):
        tl["fps"] = 30
    if tl.get("fit") not in ("cover", "contain"):
        tl["fit"] = "cover"
    if not isinstance(tl.get("bg"), str):
        tl["bg"] = "#000000"
    # Persistent logo / watermark (applies across the whole render).
    logo = tl.get("logo")
    tl["logo"] = logo if (isinstance(logo, dict) and logo.get("asset_id")) else None
    # Branding kit — keep these through save/render (were previously stripped).
    sc = tl.get("speaker_colors")
    if isinstance(sc, dict):
        # Accept Host/Guest aliases from older docs / CapCut stubs.
        normalized = dict(sc)
        if "SPEAKER_00" not in normalized and "Host" in normalized:
            normalized["SPEAKER_00"] = normalized["Host"]
        if "SPEAKER_01" not in normalized and "Guest" in normalized:
            normalized["SPEAKER_01"] = normalized["Guest"]
        tl["speaker_colors"] = normalized
    else:
        tl["speaker_colors"] = {}
    hb = tl.get("headline_banner")
    if isinstance(hb, dict) or isinstance(hb, str) or hb is None:
        tl["headline_banner"] = hb
    else:
        tl["headline_banner"] = None
    style = tl.get("style")
    if isinstance(style, dict) and style:
        tl["style"] = _normalize_caption_style(style)
    else:
        tl["style"] = {}
    ts = tl.get("track_states")
    tl["track_states"] = ts if isinstance(ts, dict) else None
    tracks = tl.get("tracks")
    if not isinstance(tracks, dict):
        tracks = {}
    for key in ("main", "overlay", "effects", "text", "music"):
        clips = tracks.get(key)
        tracks[key] = clips if isinstance(clips, list) else []
    # Effect-lane clips: type + start + out(duration). Coerce common shapes.
    _fx_types = {"split_screen", "punch_zoom", "ken_burns", "color"}
    cleaned_fx = []
    for fx in tracks["effects"]:
        if not isinstance(fx, dict):
            continue
        ftype = str(fx.get("type") or "")
        if ftype not in _fx_types:
            continue
        try:
            start = max(0.0, float(fx.get("start", 0)))
        except (TypeError, ValueError):
            start = 0.0
        try:
            # `out` is duration on the effects lane (same convention as titles).
            dur = float(fx.get("out", fx.get("duration", 2)))
        except (TypeError, ValueError):
            dur = 2.0
        dur = max(0.2, min(120.0, dur))
        row = dict(fx)
        row["type"] = ftype
        row["start"] = start
        row["out"] = dur
        if "id" not in row or not row["id"]:
            row["id"] = uuid.uuid4().hex[:8]
        cleaned_fx.append(row)
    tracks["effects"] = cleaned_fx
    tl["tracks"] = tracks
    return tl


def _tl_effect_span(fx: dict) -> tuple[float, float]:
    """Return (start, end) in output time for an effects-lane clip."""
    try:
        start = max(0.0, float(fx.get("start", 0)))
    except (TypeError, ValueError):
        start = 0.0
    try:
        dur = max(0.2, float(fx.get("out", fx.get("duration", 2))))
    except (TypeError, ValueError):
        dur = 2.0
    return start, start + dur


def _tl_effects_overlapping(effects: list, t0: float, t1: float) -> list:
    """Effects whose [start, end) overlaps output interval [t0, t1)."""
    out = []
    for fx in effects or []:
        if not isinstance(fx, dict):
            continue
        a, b = _tl_effect_span(fx)
        if b > t0 + 0.001 and a < t1 - 0.001:
            out.append(fx)
    return out


def _tl_effect_cut_points(effects: list, t0: float, t1: float) -> list[float]:
    """Sorted unique boundaries inside (t0, t1) where an effect starts or ends."""
    pts = {round(t0, 4), round(t1, 4)}
    for fx in effects or []:
        if not isinstance(fx, dict):
            continue
        a, b = _tl_effect_span(fx)
        if t0 < a < t1:
            pts.add(round(a, 4))
        if t0 < b < t1:
            pts.add(round(b, 4))
    return sorted(pts)


def _tl_props_from_lane_effects(lane_fxs: list) -> tuple:
    """Fold overlapping effects-lane clips into ken/punch/color/split props.

    Later clips of the same type win. Returns (ken, punch, color, split).
    """
    ken = None
    punch = None
    color = None
    split = None
    for fx in lane_fxs or []:
        ftype = fx.get("type")
        if ftype == "ken_burns":
            ken = {
                "enabled": True,
                "intensity": fx.get("intensity") or "med",
                "direction": fx.get("direction") or "in",
            }
        elif ftype == "punch_zoom":
            punch = {
                "enabled": True,
                "intensity": fx.get("intensity") or "med",
                "hit": float(fx.get("hit") or 0),
                "decay": float(fx.get("decay") or 0.45),
            }
            if isinstance(fx.get("anchor"), dict):
                punch["anchor"] = fx["anchor"]
        elif ftype == "color":
            color = {
                "preset": fx.get("preset") or "none",
                "brightness": fx.get("brightness", 0),
                "contrast": fx.get("contrast", 1),
                "saturation": fx.get("saturation", 1),
            }
        elif ftype == "split_screen":
            split = {
                "enabled": True,
                "source_job_id": fx.get("source_job_id"),
                "asset_id": fx.get("asset_id"),
                "in": float(fx.get("in") or 0),
                "layout": fx.get("layout") or "auto",
                "placement": fx.get("placement") or "second_bottom",
            }
    return ken, punch, color, split


def render_timeline_job(job_id: str, timeline: dict) -> None:
    """Background worker: composite a timeline into OUTPUT_DIR/{job_id}.mp4."""
    work: list[Path] = []

    def _stage(label: str, pct: int) -> None:
        jobs[job_id]["status"] = label
        jobs[job_id]["progress"] = pct
        _db_save_job(job_id)

    try:
        tl = _normalize_timeline(timeline)
        W, H = TIMELINE_CANVASES[tl["canvas"]]
        fps = tl["fps"]
        fit = tl["fit"]
        bg = tl["bg"]
        tracks = tl["tracks"]
        style = tl.get("style") or jobs[job_id].get("style") or {}
        if not _style_has_caption_fields(style):
            # Fall back to Caption look on the first main source job.
            for c in (tracks.get("main") or []):
                sjid = c.get("source_job_id")
                if sjid and jobs.get(sjid) and _style_has_caption_fields(jobs[sjid].get("style")):
                    style = jobs[sjid]["style"]
                    break
        style = _normalize_caption_style(style) if _style_has_caption_fields(style) else dict(_DEFAULT_CAPTION_STYLE)
        tl["style"] = style

        main_clips = tracks["main"]
        if not main_clips:
            raise RuntimeError("Add at least one clip to the main track before rendering.")

        lane_effects = tracks.get("effects") or []

        # ---- Pass 1: normalize + stitch the main video track ----
        # Each main clip may expand into several segments: text-based editing
        # cuts split it into keep-ranges, and split-screen / Ken Burns change how
        # each piece is rendered. Effects-lane clips further subdivide by time.
        # Transitions live on clip *boundaries* only; cut-internal boundaries
        # are hard cuts.
        _stage("building main track", 10)
        segments = []        # [(path, dur)]
        transitions = []     # len == len(segments) - 1
        seg_meta = []        # per-segment caption source info, aligned to segments
        seg_counter = 0
        out_cursor = 0.0     # output time so far (before xfade), matches Effects lane
        for i, c in enumerate(main_clips):
            src = _timeline_clip_source(c)
            if not src:
                raise RuntimeError(f"Main clip {i + 1}: source video is no longer available.")
            
            # Apply reframe if enabled in style
            if style.get("reframe", {}).get("enabled"):
                source_job_id = c.get("source_job_id")
                if source_job_id:
                    cache_path = UPLOAD_DIR / f"{source_job_id}_reframe.json"
                    if cache_path.exists():
                        reframe_video_path = UPLOAD_DIR / f"{source_job_id}_reframe.mp4"
                        if not reframe_video_path.exists():
                            reframe_data = json.loads(cache_path.read_text(encoding="utf-8"))
                            src_w, src_h = get_video_dimensions(src)
                            src_duration = _media_duration(src)
                            plan = compute_reframe_plan(reframe_data, src_w, src_h, src_duration)
                            apply_reframe(src, plan, reframe_video_path)
                        src = reframe_video_path

            t_in = max(0.0, float(c.get("in", 0)))
            t_out = float(c.get("out", t_in + 2))
            if t_out <= t_in:
                raise RuntimeError(f"Main clip {i + 1}: end must be after start.")

            keeps = _tl_keep_ranges(t_in, t_out, c.get("cuts") or [])
            if not keeps:
                continue  # entire clip was cut away via text editing

            # Clip-level FX (legacy / inspector) — Effects lane overrides when set.
            clip_ken = c.get("ken_burns")
            clip_punch = c.get("punch_zoom")
            clip_color = c.get("color") or c.get("color_grade")
            clip_split = c.get("split") or {}

            clip_tr = c.get("transition") or {}
            clip_tr = clip_tr.get("type") if isinstance(clip_tr, dict) and clip_tr.get("type") else 0

            # Captions default ON; the source job supplies the words + style.
            burn_caps = c.get("burn_captions", True) and bool(c.get("source_job_id"))

            for k, (ks, ke) in enumerate(keeps):
                kdur = ke - ks
                keep_ot0 = out_cursor
                keep_ot1 = out_cursor + kdur
                cuts = _tl_effect_cut_points(lane_effects, keep_ot0, keep_ot1)
                for bi in range(len(cuts) - 1):
                    ot0, ot1 = cuts[bi], cuts[bi + 1]
                    sub_dur = ot1 - ot0
                    if sub_dur < 0.04:
                        continue
                    src_off = ot0 - keep_ot0
                    sub_ks = ks + src_off
                    sub_ke = sub_ks + sub_dur

                    lane_fxs = _tl_effects_overlapping(lane_effects, ot0, ot1)
                    fx_ken, fx_punch, fx_color, fx_split = _tl_props_from_lane_effects(lane_fxs)

                    ken = fx_ken if fx_ken else clip_ken
                    punch = fx_punch if fx_punch else clip_punch
                    color = fx_color if fx_color else clip_color
                    split = fx_split if fx_split else (clip_split if clip_split.get("enabled") else None)
                    split_src = _timeline_clip_source(split) if (split and split.get("enabled")) else None

                    seg_path = UPLOAD_DIR / f"{job_id}_tlseg{seg_counter:03d}.mp4"
                    if split_src:
                        # Second-source in-point advances with source time on A.
                        base_in = max(0.0, float((split or {}).get("in", 0)))
                        s_in = base_in + (sub_ks - t_in)
                        dur = _tl_split_segment(src, sub_ks, split_src, s_in, sub_dur,
                                                (split or {}).get("layout", "auto"),
                                                W, H, fps, seg_path, color=color,
                                                placement=(split or {}).get("placement"))
                    else:
                        dur = _tl_normalize_segment(src, sub_ks, sub_ke, W, H, fps, fit, bg,
                                                    seg_path, ken=ken, color=color, punch=punch)
                    segments.append((seg_path, dur))
                    seg_meta.append({
                        "source_job_id": c.get("source_job_id"),
                        "src_in": sub_ks, "src_out": sub_ke, "burn_captions": burn_caps,
                        "word_overrides": c.get("word_overrides") if isinstance(c.get("word_overrides"), dict) else {},
                    })
                    work.append(seg_path)
                    # Boundary transition only at the START of a new clip's first
                    # kept piece (not the very first segment overall).
                    if segments and len(segments) > 1:
                        transitions.append(clip_tr if (k == 0 and bi == 0) else 0)
                    seg_counter += 1

                out_cursor += kdur

            jobs[job_id]["progress"] = 10 + int(25 * (i + 1) / len(main_clips))
            _db_save_job(job_id)

        if not segments:
            raise RuntimeError("Every main clip was cut away — nothing left to render.")

        base = UPLOAD_DIR / f"{job_id}_tlbase.mp4"
        work.append(base)
        _total_dur, seg_starts = _tl_build_main_track(segments, transitions, base, job_id)

        # ---- Remap each captioned segment's words onto the output timeline ----
        caption_words = []
        caption_style = style
        caption_emoji = None
        for idx, meta in enumerate(seg_meta):
            if not meta["burn_captions"]:
                continue
            sjob = jobs.get(meta["source_job_id"]) or {}
            words = sjob.get("words") or []
            if not words:
                continue
            if not _style_has_caption_fields(caption_style):
                caption_style = _normalize_caption_style(
                    sjob.get("style") or _TL_DEFAULT_CAPTION_STYLE
                )
            if caption_emoji is None:
                caption_emoji = sjob.get("emoji_rules") or {}
            seg_start = seg_starts[idx] if idx < len(seg_starts) else 0.0
            overrides = meta.get("word_overrides") or {}
            for w in words:
                try:
                    ws = float(w.get("start", 0))
                    we = float(w.get("end", 0))
                except (TypeError, ValueError):
                    continue
                if we <= meta["src_in"] or ws >= meta["src_out"]:
                    continue
                local_s = max(0.0, ws - meta["src_in"])
                local_e = max(local_s, min(meta["src_out"], we) - meta["src_in"])
                text = w.get("word", "")
                # Clip-local caption renames (Phase 4) keyed by source start time.
                key = f"{ws:.3f}"
                if key in overrides and str(overrides[key]).strip():
                    text = str(overrides[key]).strip()
                caption_words.append({
                    "word": text,
                    "start": seg_start + local_s,
                    "end": seg_start + local_e,
                })

        # ---- Persistent logo / watermark (whole-video overlay) ----
        if tl.get("logo"):
            _stage("adding logo", 48)
            logod = UPLOAD_DIR / f"{job_id}_tllogo.mp4"
            work.append(logod)
            _tl_apply_logo(base, tl["logo"], W, H, logod)
            base = logod

        # ---- Pass 2: background music ----
        if tracks["music"]:
            _stage("mixing music", 55)
            mixed = UPLOAD_DIR / f"{job_id}_tlmusic.mp4"
            work.append(mixed)
            _tl_mix_music(base, tracks["music"], mixed)
            base = mixed

        # ---- Pass 3: overlays / B-roll / PiP ----
        if tracks["overlay"]:
            _stage("compositing overlays", 70)
            comp = UPLOAD_DIR / f"{job_id}_tlovl.mp4"
            work.append(comp)
            _tl_composite_overlays(base, tracks["overlay"], W, H, comp, fps=fps)
            base = comp

        # ---- Pass 4: captions + titles / lower-thirds (single libass burn) ----
        output_path = OUTPUT_DIR / f"{job_id}.mp4"
        caption_ass = None
        if caption_words:
            # Prefer timeline branding; fall back to caption style colors.
            speaker_colors = tl.get("speaker_colors") or (caption_style or {}).get("speaker_colors") or {}
            if isinstance(speaker_colors, dict):
                speaker_colors = dict(speaker_colors)
                if "SPEAKER_00" not in speaker_colors and "Host" in speaker_colors:
                    speaker_colors["SPEAKER_00"] = speaker_colors["Host"]
                if "SPEAKER_01" not in speaker_colors and "Guest" in speaker_colors:
                    speaker_colors["SPEAKER_01"] = speaker_colors["Guest"]
            else:
                speaker_colors = {}
            diar = None
            if speaker_colors:
                for meta in seg_meta:
                    if not meta.get("source_job_id"):
                        continue
                    cache_path = UPLOAD_DIR / f"{meta['source_job_id']}_reframe.json"
                    if not cache_path.exists():
                        continue
                    try:
                        rd = json.loads(cache_path.read_text(encoding="utf-8"))
                        diar = rd.get("diarization") or []
                        if diar:
                            break
                    except (json.JSONDecodeError, OSError):
                        continue
            hb = tl.get("headline_banner")
            if isinstance(hb, dict):
                headline = hb.get("text") or ""
            elif isinstance(hb, str):
                headline = hb
            else:
                headline = (caption_style or {}).get("headline_banner") or ""
            caption_ass = build_ass(
                caption_words,
                caption_style or _TL_DEFAULT_CAPTION_STYLE,
                W, H,
                caption_emoji or {},
                speaker_colors=speaker_colors,
                diarization=diar,
                headline_banner=headline,
            )
        titles_ass = _tl_build_titles_ass(tracks["text"], W, H) if tracks["text"] else None

        if caption_ass or titles_ass:
            _stage("adding captions & titles", 85)
            ass_text = _tl_compose_ass(W, H, caption_ass or "", titles_ass or "")
            ass_path = UPLOAD_DIR / f"{job_id}_tltext.ass"
            ass_path.write_text(ass_text, encoding="utf-8")
            work.append(ass_path)
            burn_subtitles(base, ass_path, output_path)
        else:
            # Nothing to burn — just finalize (stream copy is enough).
            _stage("finalizing", 90)
            _tl_run([FFMPEG, "-y", "-i", str(base), "-c", "copy",
                     "-movflags", "+faststart", str(output_path)], "Finalize")

        if not output_path.exists() or output_path.stat().st_size < 1024:
            raise RuntimeError("Render produced an empty or missing output file.")
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
        for p in work:
            _safe_unlink(p)


# ---- Timeline routes ----

@app.route("/upload-asset", methods=["POST"])
def upload_asset():
    """Store an uploaded B-roll / image / music asset for use on the timeline."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    kind = _asset_kind(ext)
    if not kind:
        return jsonify({"error": f"Unsupported asset type: .{ext}"}), 400
    asset_id = uuid.uuid4().hex
    dest = ASSET_DIR / f"{asset_id}.{ext}"
    f.save(str(dest))
    _write_asset_meta(asset_id, filename=f.filename, source="upload")
    return jsonify({
        "asset_id": asset_id,
        "kind": kind,
        "filename": f.filename,
        "duration": _media_duration(dest) if kind in ("video", "audio") else 0.0,
    })


@app.route("/list-assets", methods=["GET"])
def list_assets():
    """List uploaded timeline assets, newest first."""
    out = []
    for p in ASSET_DIR.glob("*"):
        if not p.is_file():
            continue
        if p.name.endswith(".meta.json") or p.suffix.lower() == ".json":
            continue
        kind = _asset_kind(p.suffix)
        if not kind:
            continue
        meta = _read_asset_meta(p.stem)
        filename = meta.get("filename") or meta.get("keyword") or None
        if not filename and meta.get("keyword"):
            filename = f"{meta['keyword']}.{p.suffix.lstrip('.')}"
        out.append({
            "asset_id": p.stem,
            "kind": kind,
            "ext": p.suffix.lstrip("."),
            "filename": filename,
            "keyword": meta.get("keyword"),
            "source": meta.get("source"),
            "duration": _media_duration(p) if kind in ("video", "audio") else 0.0,
            "mtime": p.stat().st_mtime,
        })
    out.sort(key=lambda a: a["mtime"], reverse=True)
    return jsonify({"assets": out})


@app.route("/delete-asset/<asset_id>", methods=["POST", "DELETE"])
def delete_asset(asset_id: str):
    """Remove an uploaded timeline asset and its metadata sidecar."""
    if not asset_id or not re.fullmatch(r"[a-f0-9]{32}", asset_id):
        return jsonify({"error": "Invalid asset id"}), 400
    removed = 0
    for p in list(ASSET_DIR.glob(f"{asset_id}.*")):
        if not p.is_file():
            continue
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    # Waveform companion uses a different stem pattern.
    wave = ASSET_DIR / f"{asset_id}_wave.png"
    if wave.exists():
        _safe_unlink(wave)
    if removed == 0:
        return jsonify({"error": "Asset not found"}), 404
    return jsonify({"ok": True, "asset_id": asset_id, "removed": removed})


@app.route("/asset/<asset_id>")
def serve_asset(asset_id: str):
    """Serve an asset file for in-browser preview."""
    path = _find_asset_path(asset_id)
    if not path:
        return jsonify({"error": "Asset not found"}), 404
    return send_from_directory(ASSET_DIR, path.name)


def _make_waveform(src: Path, out: Path) -> bool:
    """Render a waveform PNG of *src*'s audio (cached). False if no audio."""
    if not _has_audio_stream(src):
        return False
    proc = subprocess.run(
        [FFMPEG, "-y", "-i", str(src), "-filter_complex",
         "showwavespic=s=1600x120:colors=#8b7bff|#6c5cff", "-frames:v", "1", str(out)],
        capture_output=True, text=True,
    )
    return proc.returncode == 0 and out.exists()


def _make_filmstrip(src: Path, out: Path, frames: int = 12) -> bool:
    """Render a horizontal filmstrip JPG (N evenly-spaced frames) of *src*."""
    dur = _media_duration(src)
    if dur <= 0:
        return False
    rate = max(0.01, frames / dur)
    proc = subprocess.run(
        [FFMPEG, "-y", "-i", str(src), "-an", "-vf",
         f"fps={rate:.5f},scale=-1:64,tile={frames}x1", "-frames:v", "1",
         "-qscale:v", "6", str(out)],
        capture_output=True, text=True,
    )
    return proc.returncode == 0 and out.exists()


def _cached_render(cache: Path, src: Path, maker) -> Path | None:
    """Return *cache*, (re)building it via *maker* if missing/stale."""
    if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
        return cache
    try:
        if maker(src, cache):
            return cache
    except (OSError, subprocess.SubprocessError):
        pass
    return None


@app.route("/waveform/<job_id>.png")
def waveform(job_id: str):
    """Audio waveform strip for a source job (cached)."""
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    src = find_video_path(job_id)
    if not src:
        return jsonify({"error": "Source missing"}), 404
    cache = _cached_render(UPLOAD_DIR / f"{job_id}_wave.png", src, _make_waveform)
    if not cache:
        return jsonify({"error": "No audio / waveform unavailable"}), 404
    return send_from_directory(cache.parent, cache.name)


@app.route("/filmstrip/<job_id>.jpg")
def filmstrip(job_id: str):
    """Filmstrip thumbnail strip for a source job (cached)."""
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    src = find_video_path(job_id)
    if not src:
        return jsonify({"error": "Source missing"}), 404
    cache = _cached_render(UPLOAD_DIR / f"{job_id}_strip.jpg", src, _make_filmstrip)
    if not cache:
        return jsonify({"error": "Filmstrip unavailable"}), 404
    return send_from_directory(cache.parent, cache.name)


@app.route("/asset-waveform/<asset_id>.png")
def asset_waveform(asset_id: str):
    """Audio waveform strip for an uploaded asset (music/video)."""
    src = _find_asset_path(asset_id)
    if not src:
        return jsonify({"error": "Asset not found"}), 404
    cache = _cached_render(ASSET_DIR / f"{asset_id}_wave.png", src, _make_waveform)
    if not cache:
        return jsonify({"error": "No audio"}), 404
    return send_from_directory(cache.parent, cache.name)


@app.route("/source-info/<job_id>")
def source_info(job_id: str):
    """Return a source job's video duration + display dimensions so the editor
    can place and trim clips against a real length."""
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Source video missing"}), 404
    try:
        w, h = get_video_dimensions(video_path)
    except Exception as exc:
        w, h = 0, 0
    return jsonify({
        "duration": _media_duration(video_path),
        "width": w,
        "height": h,
        "filename": jobs[job_id].get("filename"),
    })


@app.route("/timeline/create", methods=["POST"])
def timeline_create():
    """Create a new (empty) timeline editor job.

    Optionally seed the main track with the calling job's full clip via
    {seed_job_id}. Returns {job_id}.
    """
    data = request.get_json(force=True) or {}
    seed_job_id = data.get("seed_job_id")
    label = (data.get("label") or "Timeline edit").strip()[:80] or "Timeline edit"

    job_id = uuid.uuid4().hex
    tracks = {"main": [], "overlay": [], "text": [], "music": []}
    if seed_job_id and seed_job_id in jobs:
        seed_video = find_video_path(seed_job_id)
        if seed_video:
            dur = _media_duration(seed_video)
            tracks["main"].append({
                "id": uuid.uuid4().hex[:8],
                "source_job_id": seed_job_id,
                "in": 0.0,
                "out": dur or 10.0,
            })
    timeline = _normalize_timeline({"tracks": tracks})

    jobs[job_id] = {
        "status": "timeline_edit",
        "progress": 0,
        "output": None,
        "error": None,
        "words": None,
        "style": None,
        "audio": None,
        "emoji_rules": None,
        "created_at": time.time(),
        "filename": f"{label}.mp4",
        "timeline": timeline,
        "is_timeline": True,
    }
    _db_save_job(job_id)
    return jsonify({"job_id": job_id, "timeline": timeline})


@app.route("/timeline/<job_id>", methods=["GET"])
def timeline_get(job_id: str):
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    job = jobs[job_id]
    return jsonify({
        "job_id": job_id,
        "timeline": job.get("timeline") or _normalize_timeline({}),
        "filename": job.get("filename"),
        "status": job.get("status"),
        "output": job.get("output"),
    })


@app.route("/timeline/save", methods=["POST"])
def timeline_save():
    """Persist a timeline document (autosave from the editor)."""
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    timeline = _normalize_timeline(data.get("timeline") or {})
    jobs[job_id]["timeline"] = timeline
    if data.get("label"):
        jobs[job_id]["filename"] = f"{str(data['label']).strip()[:80]}.mp4"
    _db_save_job(job_id)
    return jsonify({"ok": True})


@app.route("/timeline/render", methods=["POST"])
def timeline_render():
    """Kick off a background timeline render. Poll progress via /status."""
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    timeline = _normalize_timeline(data.get("timeline") or jobs[job_id].get("timeline") or {})
    if not timeline["tracks"]["main"]:
        return jsonify({"error": "Add at least one clip to the main track first."}), 400

    style = data.get("style") or timeline.get("style") or jobs[job_id].get("style") or {}
    # Timeline project may have empty style — seed from first main source's Caption look.
    if not _style_has_caption_fields(style):
        for c in timeline["tracks"]["main"]:
            sjid = c.get("source_job_id")
            if sjid and jobs.get(sjid) and _style_has_caption_fields(jobs[sjid].get("style")):
                style = jobs[sjid]["style"]
                break
    style = _normalize_caption_style(style) if _style_has_caption_fields(style) else _normalize_caption_style({})
    timeline["style"] = style
    jobs[job_id]["style"] = style
    jobs[job_id]["timeline"] = timeline
    jobs[job_id]["status"] = "queued"
    jobs[job_id]["progress"] = 0
    jobs[job_id]["output"] = None
    jobs[job_id]["error"] = None
    _db_save_job(job_id)

    t = threading.Thread(target=render_timeline_job, args=(job_id, timeline))
    t.daemon = True
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/timeline/list", methods=["GET"])
def timeline_list():
    """List timeline editor jobs, newest first."""
    out = []
    for jid, job in jobs.items():
        if not job.get("is_timeline"):
            continue
        out.append({
            "job_id": jid,
            "filename": job.get("filename"),
            "status": job.get("status"),
            "output": job.get("output"),
            "created_at": job.get("created_at"),
            "clip_count": len((job.get("timeline") or {}).get("tracks", {}).get("main", [])),
        })
    out.sort(key=lambda a: a.get("created_at") or 0, reverse=True)
    return jsonify({"timelines": out})


# =====================================================================
# Captions-aligned pipeline: AI Edit seed, recommended cuts, shots, co-editor
# =====================================================================

def _strip_filler_token(s: str) -> str:
    return re.sub(r"[^a-z']", "", (s or "").lower())


def _filler_indices(words: list) -> set:
    flagged = set()
    if not words:
        return flagged
    for i, w in enumerate(words):
        tok = _strip_filler_token(str(w.get("word", "")))
        if not tok:
            continue
        if tok in _FILLER_SINGLE_WORDS:
            flagged.add(i)
            continue
        if i + 1 < len(words):
            nxt = _strip_filler_token(str(words[i + 1].get("word", "")))
            for a, b in _FILLER_PAIR_WORDS:
                if tok == a and nxt == b:
                    flagged.add(i)
                    flagged.add(i + 1)
                    break
    return flagged


def _merge_cut_ranges(ranges: list) -> list:
    if not ranges:
        return []
    norm = []
    for r in ranges:
        try:
            a, b = float(r[0]), float(r[1])
        except (TypeError, ValueError, IndexError):
            continue
        if b > a:
            norm.append([a, b])
    if not norm:
        return []
    norm.sort(key=lambda x: x[0])
    out = [norm[0]]
    for a, b in norm[1:]:
        if a <= out[-1][1] + 0.05:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _recommended_cuts_for_words(words: list, t_in: float, t_out: float,
                                max_gap: float = 1.0,
                                include_fillers: bool = True,
                                include_silence: bool = True) -> dict:
    """Build filler + silence cut ranges (source seconds) for AI Trim parity."""
    window = [w for w in (words or [])
              if float(w.get("end", 0) or 0) > t_in
              and float(w.get("start", 0) or 0) < t_out]
    cuts = []
    filler_count = 0
    if include_fillers and window:
        flagged = _filler_indices(window)
        for i in flagged:
            try:
                ws = max(t_in, float(window[i].get("start", 0)))
                we = min(t_out, float(window[i].get("end", 0)))
            except (TypeError, ValueError):
                continue
            if we > ws:
                cuts.append([ws, we])
                filler_count += 1
    silence_gaps = []
    if include_silence and len(window) >= 2:
        silence = compute_silence_compression(window, max_gap=max_gap, target_gap=0.25)
        for g in silence.get("gaps") or []:
            if g.get("preserved"):
                continue
            try:
                gs = max(t_in, float(g["start"]))
                ge = min(t_out, float(g["end"]))
            except (KeyError, TypeError, ValueError):
                continue
            # Cut the middle of the gap, leave target_gap/2 on each side.
            cut_len = (ge - gs) - 0.25
            if cut_len < 0.15:
                continue
            mid_start = gs + 0.125
            mid_end = ge - 0.125
            if mid_end > mid_start:
                cuts.append([mid_start, mid_end])
                silence_gaps.append({
                    "start": gs, "end": ge, "duration": ge - gs,
                    "context_before": g.get("context_before", ""),
                    "context_after": g.get("context_after", ""),
                })
    merged = _merge_cut_ranges(cuts)
    cut_total = sum(b - a for a, b in merged)
    labeled = []
    for a, b in merged:
        kind = "silence"
        for g in silence_gaps:
            if a < g["end"] and b > g["start"]:
                kind = "silence"
                labeled.append({
                    "start": a, "end": b,
                    "kind": kind,
                    "context_before": g.get("context_before", ""),
                    "context_after": g.get("context_after", ""),
                })
                break
        else:
            labeled.append({
                "start": a, "end": b,
                "kind": "filler",
                "context_before": "",
                "context_after": "",
            })
    return {
        "cuts": merged,
        "cut_details": labeled,
        "filler_count": filler_count,
        "silence_gaps": silence_gaps,
        "stats": {
            "cut_count": len(merged),
            "seconds_removed": round(cut_total, 2),
            "window_in": t_in,
            "window_out": t_out,
        },
    }


def _intensity_effect_budget(intensity: str) -> int:
    return {"low": 2, "med": 4, "high": 8}.get((intensity or "med").lower(), 4)


def _uid_short() -> str:
    return uuid.uuid4().hex[:8]


def _clip_color_from_pack(pack: dict) -> dict:
    """Normalize pack color_grade into the `color` shape Render actually reads."""
    raw = pack.get("color_grade") if isinstance(pack, dict) else None
    if not isinstance(raw, dict):
        return {"preset": "none"}
    preset = str(raw.get("preset") or "none")
    if preset not in _TL_COLOR_PRESETS:
        preset = "none"
    # Older packs stored tiny deltas (contrast: 0.1) that destroy the image if
    # treated as absolute ffmpeg eq values — keep preset only.
    return {"preset": preset}


def _build_ai_edit_timeline(job_id: str, t_in: float, t_out: float,
                            pack: dict, intensity: str,
                            cuts: list, effects: list,
                            label: str = "",
                            words: list | None = None,
                            insert_media: bool = True) -> dict:
    """Assemble a Captions-style seeded timeline project from a style pack."""
    main_id = _uid_short()
    grade = _clip_color_from_pack(pack)
    main_clip = {
        "id": main_id,
        "source_job_id": job_id,
        "in": t_in,
        "out": t_out,
        "transition": None,
        "burn_captions": True,
        "cuts": cuts or [],
        "color": grade,
        "color_grade": grade,
    }
    # Apply up to budget punch/Ken Burns by splitting the main clip.
    budget = _intensity_effect_budget(intensity)
    pieces = [main_clip]
    usable = []
    for fx in effects or []:
        try:
            fs = float(fx.get("start_time"))
            fe = float(fx.get("end_time"))
        except (TypeError, ValueError):
            continue
        if fe <= fs or fs < t_in - 0.05 or fe > t_out + 0.05:
            continue
        usable.append(fx)
        if len(usable) >= budget:
            break

    if usable:
        # Split chronologically into effect mid-segments.
        usable.sort(key=lambda e: float(e["start_time"]))
        pieces = []
        cursor = t_in
        for fx in usable:
            fs = max(t_in, float(fx["start_time"]))
            fe = min(t_out, float(fx["end_time"]))
            if fs - cursor > 0.08:
                pieces.append({
                    "id": _uid_short(), "source_job_id": job_id,
                    "in": cursor, "out": fs, "transition": None,
                    "burn_captions": True, "cuts": [],
                    "color": grade, "color_grade": grade,
                })
            mid = {
                "id": _uid_short(), "source_job_id": job_id,
                "in": fs, "out": fe, "transition": None,
                "burn_captions": True, "cuts": [],
                "color": grade, "color_grade": grade,
            }
            ftype = fx.get("type")
            if ftype == "punch_zoom":
                mid["punch_zoom"] = {
                    "enabled": True,
                    "intensity": fx.get("intensity") or ("high" if intensity == "high" else "med"),
                }
                if fx.get("anchor"):
                    mid["punch_zoom"]["anchor"] = fx["anchor"]
            elif ftype == "ken_burns":
                mid["ken_burns"] = {
                    "enabled": True,
                    "intensity": fx.get("intensity") or "med",
                    "direction": fx.get("direction") or "in",
                }
            elif ftype == "split_screen":
                mid["split"] = {"enabled": True}
            pieces.append(mid)
            cursor = fe
        if t_out - cursor > 0.08:
            pieces.append({
                "id": _uid_short(), "source_job_id": job_id,
                "in": cursor, "out": t_out, "transition": None,
                "burn_captions": True, "cuts": [],
                "color": grade, "color_grade": grade,
            })
        # Redistribute original cuts onto pieces by intersection.
        if cuts:
            for p in pieces:
                p["cuts"] = [
                    [max(p["in"], c[0]), min(p["out"], c[1])]
                    for c in cuts
                    if c[1] > p["in"] and c[0] < p["out"] and min(p["out"], c[1]) - max(p["in"], c[0]) > 0.05
                ]
        # Transitions between pieces
        tr = pack.get("transition") or "crossfade"
        for i, p in enumerate(pieces[:-1]):
            p["transition"] = {"type": tr, "duration": 0.25 if intensity != "high" else 0.15}

    text_track = []
    if pack.get("add_title") and label:
        text_track.append({
            "id": _uid_short(),
            "text": label[:80],
            "start": 0,
            "out": 2.5,
            "x": 0.5, "y": 0.12, "size": 64,
            "color": "#FFFFFF", "font": pack.get("style", {}).get("font") or "Anton",
            "bg_enabled": True, "bg_color": "#000000", "bg_opacity": 0.45,
            "outline_color": "#000000", "outline_width": 0, "shadow": 0,
            "bold": True, "align": 2, "anim": "fade",
            "anchor": pieces[0]["id"] if pieces else None,
        })

    # Keyword callouts → text track + optional badge overlays (Phase 5).
    overlay_track = []
    if insert_media and words:
        media_budget = {"low": 1, "med": 3, "high": 5}.get((intensity or "med").lower(), 3)
        callouts = _keyword_callouts_for_window(words, t_in, t_out, media_budget)
        style = pack.get("style") or {}
        for i, co in enumerate(callouts):
            # Map source time into output time roughly as offset from t_in
            # (before cut compression — good enough for seed placement).
            start = max(0.0, float(co["start"]) - t_in)
            dur = min(2.2, max(1.2, float(co.get("duration") or 1.8)))
            text_track.append({
                "id": _uid_short(),
                "text": str(co["text"])[:40],
                "start": start,
                "out": start + dur,  # absolute end (ASS builder expects end, not duration)
                "x": 0.72, "y": 0.18, "size": int(style.get("size") or style.get("font_size") or 56) - 8,
                "color": style.get("highlight") or style.get("highlight_color") or "#FFD60A",
                "font": style.get("font") or style.get("font_name") or "Anton",
                "bg_enabled": True, "bg_color": "#000000", "bg_opacity": 0.55,
                "outline_color": "#000000", "outline_width": 0, "shadow": 0,
                "bold": True, "align": 2, "anim": "slideup",
                "anchor": pieces[0]["id"] if pieces else None,
            })
            # Also seed a PiP keyword badge on the overlay track when possible.
            try:
                asset_id = uuid.uuid4().hex
                dest = ASSET_DIR / f"{asset_id}.png"
                if _make_keyword_badge_png(str(co["text"]), dest):
                    corners = [
                        {"x": 0.58, "y": 0.06}, {"x": 0.04, "y": 0.06},
                        {"x": 0.58, "y": 0.62}, {"x": 0.04, "y": 0.62},
                    ]
                    pos = corners[i % 4]
                    overlay_track.append({
                        "id": _uid_short(),
                        "asset_id": asset_id,
                        "in": 0,
                        "out": dur,
                        "start": start,
                        "x": pos["x"], "y": pos["y"],
                        "w": 0.36, "h": 0.20,
                        "opacity": 0.92,
                        "fit": "contain",
                        "fade_in": 0.15, "fade_out": 0.2,
                        "border_px": 0,
                        "layout": "pip_auto",
                        "anchor": pieces[0]["id"] if pieces else None,
                        "anchor_offset": start,
                    })
                else:
                    _safe_unlink(dest)
            except Exception:
                pass

    music_track = []
    # Auto-attach previously uploaded bg music for this job, if present.
    bg_music_files = list(UPLOAD_DIR.glob(f"{job_id}_bgmusic.*"))
    if bg_music_files and intensity != "low":
        # Music assets on the timeline use asset_id from /upload-asset.
        # For job-scoped bg music we keep a hint the UI can surface.
        pass

    return {
        "canvas": pack.get("canvas") or "9x16",
        "fit": "cover",
        "fps": 30,
        "bg": "#000000",
        "style": _normalize_caption_style(pack.get("style") or {}),
        "caption_preset": pack.get("caption_preset"),
        "ai_edit": {
            "style_pack": pack.get("label"),
            "intensity": intensity,
            "insert_media": bool(insert_media),
        },
        "tracks": {
            "main": pieces,
            "overlay": overlay_track,
            "text": text_track,
            "music": music_track,
        },
        "media_hints": {
            "bg_music_available": bool(bg_music_files),
            "callout_count": max(0, len(text_track) - (1 if pack.get("add_title") and label else 0)),
            "overlay_count": len(overlay_track),
        },
    }


_VISUAL_KEYWORDS = {
    "coffee", "candle", "festival", "money", "growth", "craft", "food", "music",
    "car", "house", "dog", "cat", "computer", "phone", "book", "water", "fire",
    "earth", "sky", "sun", "moon", "star", "city", "tree", "flower", "people",
    "business", "love", "happy", "sad", "angry", "time", "day", "night", "world",
    "life", "school", "family", "friend", "party", "game", "sport", "art",
    "nature", "technology", "health", "travel", "work", "home",
    "success", "power", "brand", "product", "design", "video", "photo",
    "founder", "vendor", "market", "customer", "sale", "shop",
}


def _keyword_callouts_for_window(words: list, t_in: float, t_out: float,
                                 budget: int) -> list:
    """Pick visual keywords inside [t_in, t_out] for text-track callouts."""
    stop = {
        "the", "and", "a", "to", "of", "in", "i", "is", "that", "it", "on", "you",
        "this", "for", "but", "with", "are", "have", "be", "at", "or", "as", "was",
        "so", "if", "out", "not", "we", "my", "they", "your", "all", "do", "can",
        "will", "about", "which", "up", "one", "there", "what", "would", "when",
        "an", "she", "he", "their", "her", "his", "has", "who", "from", "by",
        "some", "me", "how", "like", "just", "know", "then", "them", "now", "well",
        "think", "um", "uh", "okay", "ok", "right",
    }
    out = []
    seen = set()
    for w in words or []:
        if len(out) >= max(0, budget):
            break
        try:
            ws = float(w.get("start", 0))
            we = float(w.get("end", 0))
        except (TypeError, ValueError):
            continue
        if we <= t_in or ws >= t_out:
            continue
        text = str(w.get("word", "")).strip()
        clean = re.sub(r"[^a-zA-Z0-9]", "", text).lower()
        if not clean or clean in stop or clean in seen:
            continue
        if clean in _VISUAL_KEYWORDS or len(clean) > 5:
            seen.add(clean)
            out.append({
                "text": text.strip(".,!?").capitalize(),
                "start": ws,
                "duration": 1.8,
            })
    return out


def _detect_shots_ffmpeg(video_path: Path, threshold: float = 0.35,
                         t_in: float | None = None,
                         t_out: float | None = None) -> list:
    """Scene-change timestamps via ffmpeg select=gt(scene,N)."""
    cmd = [
        FFMPEG, "-hide_banner",
    ]
    if t_in is not None and t_in > 0:
        cmd += ["-ss", f"{float(t_in):.3f}"]
    cmd += ["-i", str(video_path)]
    if t_out is not None and t_in is not None and t_out > t_in:
        cmd += ["-t", f"{float(t_out - (t_in or 0)):.3f}"]
    cmd += [
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    # showinfo lands on stderr
    times = []
    for line in (proc.stderr or "").splitlines():
        # pts_time:12.345
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            t = float(m.group(1))
            if t_in:
                t += float(t_in)
            times.append(round(t, 3))
    # Dedupe near-duplicates
    cleaned = []
    for t in times:
        if not cleaned or t - cleaned[-1] >= 0.35:
            cleaned.append(t)
    return cleaned


@app.route("/ai-edit/style-packs", methods=["GET"])
def ai_edit_style_packs():
    packs = []
    for key, pack in AI_EDIT_STYLE_PACKS.items():
        packs.append({
            "id": key,
            "label": pack.get("label", key),
            "blurb": pack.get("blurb", ""),
            "canvas": pack.get("canvas", "9x16"),
        })
    return jsonify({"packs": packs})


@app.route("/recommended-cuts", methods=["POST"])
def recommended_cuts():
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    words = jobs[job_id].get("words") or []
    if not words:
        return jsonify({"error": "Transcript not available"}), 400
    try:
        t_in = float(data.get("in", data.get("start_time", 0)) or 0)
        t_out = float(data.get("out", data.get("end_time", words[-1].get("end", 0))) or 0)
        max_gap = float(data.get("max_gap", 1.0) or 1.0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid in/out"}), 400
    if t_out <= t_in:
        t_out = float(words[-1].get("end", 0) or 0)
    result = _recommended_cuts_for_words(
        words, t_in, t_out, max_gap=max_gap,
        include_fillers=bool(data.get("include_fillers", True)),
        include_silence=bool(data.get("include_silence", True)),
    )
    return jsonify(result)


@app.route("/ai-edit-seed", methods=["POST"])
def ai_edit_seed():
    """Captions-style AI Edit: style pack + intensity → seeded timeline JSON.

    Body: {
      source_job_id, start_time?, end_time?, style_pack, intensity,
      label?, apply_cuts?, create_clip?, max_effects?
    }
    """
    data = request.get_json(force=True) or {}
    source_job_id = data.get("source_job_id") or data.get("job_id")
    if not source_job_id or source_job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    src = jobs[source_job_id]
    words = src.get("words") or []
    if not words:
        return jsonify({"error": "Transcript not available"}), 400

    pack_id = (data.get("style_pack") or "pulse").lower()
    pack = AI_EDIT_STYLE_PACKS.get(pack_id) or AI_EDIT_STYLE_PACKS["pulse"]
    intensity = (data.get("intensity") or "med").lower()
    if intensity not in ("low", "med", "high"):
        intensity = "med"
    label = (data.get("label") or src.get("filename") or "AI Edit")[:120]

    try:
        t_in = float(data.get("start_time", data.get("in", 0)) or 0)
        default_out = float(words[-1].get("end", 0) or 0)
        t_out = float(data.get("end_time", data.get("out", default_out)) or default_out)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid start/end"}), 400
    t_in = max(0.0, t_in)
    if t_out <= t_in + 0.5:
        return jsonify({"error": "Clip window too short"}), 400

    apply_cuts = bool(data.get("apply_cuts", True))
    create_clip = bool(data.get("create_clip", False))
    insert_media = bool(data.get("insert_media", True))

    # Optional: materialize a chopped child job (Captions "individual clip").
    work_job_id = source_job_id
    work_in, work_out = t_in, t_out
    if create_clip:
        try:
            work_job_id = _create_clip_from_job(source_job_id, t_in, t_out, label)
            # Child job words are remapped to 0..duration
            work_in, work_out = 0.0, t_out - t_in
            words = jobs[work_job_id].get("words") or []
        except Exception as e:
            return jsonify({"error": f"Could not create clip: {e}"}), 400

    rec = _recommended_cuts_for_words(words, work_in, work_out)
    # Client may send an outline-reviewed subset of cuts (Captions "revert" flow).
    raw_cuts = data.get("cuts")
    if isinstance(raw_cuts, list):
        cuts = _merge_cut_ranges(raw_cuts) if apply_cuts else []
    else:
        cuts = rec["cuts"] if apply_cuts else []

    # Effect suggestions (Gemini when available; empty otherwise).
    effects = []
    gemini_warning = None
    try:
        max_fx = int(data.get("max_effects") or _intensity_effect_budget(intensity))
    except (TypeError, ValueError):
        max_fx = _intensity_effect_budget(intensity)
    try:
        total = float(words[-1].get("end", 0) or work_out)
        result = _gemini_generate_clip_suggestions(
            _build_effect_suggestion_prompt(
                _format_transcript_for_llm(words), total, max_fx
            )
        )
        effects = _sanitize_effect_suggestions(result.get("effects") or [], total)
        for fx in effects:
            if fx.get("type") == "punch_zoom":
                anchor = _face_anchor_at(work_job_id, fx["start_time"])
                if anchor:
                    fx["anchor"] = anchor
    except RuntimeError as exc:
        gemini_warning = str(exc)
    except Exception as exc:
        gemini_warning = str(exc)

    timeline = _build_ai_edit_timeline(
        work_job_id, work_in, work_out, pack, intensity, cuts, effects,
        label=label, words=words, insert_media=insert_media,
    )
    # Persist style onto the working job for caption burns (canonical Caption look).
    if pack.get("style"):
        jobs[work_job_id]["style"] = _normalize_caption_style(pack["style"])
        _db_save_job(work_job_id)

    return jsonify({
        "ok": True,
        "source_job_id": source_job_id,
        "clip_job_id": work_job_id if create_clip else None,
        "job_id": work_job_id,
        "style_pack": pack_id,
        "intensity": intensity,
        "label": label,
        "in": work_in,
        "out": work_out,
        "recommended_cuts": rec,
        "applied_cuts": cuts,
        "effects": effects,
        "timeline": timeline,
        "warning": gemini_warning,
    })


@app.route("/detect-shots", methods=["POST"])
def detect_shots():
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Video missing"}), 404
    try:
        threshold = float(data.get("threshold", 0.35) or 0.35)
        t_in = data.get("in")
        t_out = data.get("out")
        t_in = float(t_in) if t_in is not None else None
        t_out = float(t_out) if t_out is not None else None
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parameters"}), 400
    try:
        times = _detect_shots_ffmpeg(video_path, threshold=threshold, t_in=t_in, t_out=t_out)
    except Exception as e:
        return jsonify({"error": f"Shot detection failed: {e}"}), 500

    # Build shot spans covering [t_in,t_out] or full duration.
    if t_in is None:
        t_in = 0.0
    if t_out is None:
        words = jobs[job_id].get("words") or []
        t_out = float(words[-1].get("end", 0) or 0) if words else _ffprobe_duration(video_path)
    bounds = [t_in] + [t for t in times if t_in < t < t_out] + [t_out]
    shots = []
    for i in range(len(bounds) - 1):
        if bounds[i + 1] - bounds[i] < 0.2:
            continue
        shots.append({
            "index": len(shots),
            "start": bounds[i],
            "end": bounds[i + 1],
            "duration": round(bounds[i + 1] - bounds[i], 3),
        })
    return jsonify({"shots": shots, "cut_points": times, "in": t_in, "out": t_out})


@app.route("/co-editor", methods=["POST"])
def co_editor():
    """Natural-language → validated timeline mutation ops (Captions Co-editor)."""
    data = request.get_json(force=True) or {}
    prompt = (data.get("prompt") or "").strip()
    timeline = data.get("timeline") or {}
    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400
    if not timeline:
        return jsonify({"error": "Timeline is required"}), 400

    main = (timeline.get("tracks") or {}).get("main") or []
    overlays = (timeline.get("tracks") or {}).get("overlay") or []
    style = timeline.get("style") or {}
    speaker_colors = timeline.get("speaker_colors") or {}
    summary = {
        "canvas": timeline.get("canvas"),
        "main_clip_count": len(main),
        "main_clips": [
            {
                "index": i,
                "id": c.get("id"),
                "in": c.get("in"),
                "out": c.get("out"),
                "has_punch_zoom": bool((c.get("punch_zoom") or {}).get("enabled")),
                "has_ken_burns": bool((c.get("ken_burns") or {}).get("enabled")),
                "color_preset": ((c.get("color") or c.get("color_grade") or {}).get("preset")),
                "cut_count": len(c.get("cuts") or []),
                "transition": (c.get("transition") or {}).get("type") if isinstance(c.get("transition"), dict) else c.get("transition"),
            }
            for i, c in enumerate(main[:24])
        ],
        "overlay_count": len(overlays),
        "overlay_clips": [
            {
                "index": i,
                "keyword": c.get("keyword"),
                "source": c.get("source"),
                "has_ken_burns": bool((c.get("ken_burns") or {}).get("enabled")),
                "start": c.get("start"),
                "duration": (float(c.get("out") or 0) - float(c.get("in") or 0)) if c.get("out") is not None else None,
            }
            for i, c in enumerate(overlays[:16])
        ],
        "text_count": len((timeline.get("tracks") or {}).get("text") or []),
        "music_count": len((timeline.get("tracks") or {}).get("music") or []),
        "caption_style": {
            "font": style.get("font") or style.get("font_name"),
            "size": style.get("size") or style.get("font_size"),
            "primary": style.get("primary") or style.get("primary_color"),
            "highlight": style.get("highlight") or style.get("highlight_color"),
            "accent": style.get("accent") or style.get("accent_color"),
        },
        "speaker_colors": speaker_colors,
    }

    system_prompt = f"""You are a Timeline co-editor. Convert the user's request into JSON ops that mutate THIS timeline.

You can ONLY change timeline project state via the ops below. You cannot invent new features.
Caption style changes update the Timeline project's burn style (primary/highlight/accent/font/size).
Speaker colors (Host/Guest) are separate — use set_speaker_colors for those.
Ken Burns on Overlay is preferred for photo B-roll moments; punch zoom stays on Main.
Color grades must use presets: none, neutral, warm, cool, vivid, bw.
After ops apply in the UI, the user still must click Render to bake captions/effects into the export MP4.

Current timeline summary:
{json.dumps(summary, indent=2)}

Return ONLY JSON:
{{
  "ops": [
    {{"op": "set_caption_style", "font": "Anton", "size": 64, "primary": "#FFFFFF", "highlight": "#FFD60A", "accent": "#00FF88"}},
    {{"op": "set_speaker_colors", "SPEAKER_00": "#FFD700", "SPEAKER_01": "#00E5FF"}},
    {{"op": "delete_shot", "index": 2}},
    {{"op": "set_transition", "index": 0, "type": "crossfade", "duration": 0.3}},
    {{"op": "enable_punch_zoom", "index": 1, "intensity": "med"}},
    {{"op": "enable_ken_burns", "index": 0, "intensity": "med", "direction": "in"}},
    {{"op": "enable_ken_burns", "track": "overlay", "index": 0, "intensity": "med", "direction": "in"}},
    {{"op": "clear_effects", "index": 0}},
    {{"op": "clear_effects", "track": "overlay", "index": 0}},
    {{"op": "set_canvas", "canvas": "9x16"}},
    {{"op": "set_color_grade", "index": 0, "preset": "warm"}},
    {{"op": "add_title", "text": "Hello", "start": 0, "duration": 3}},
    {{"op": "apply_recommended_cuts", "index": 0}},
    {{"op": "merge_shots", "index": 0}},
    {{"op": "reorder_shot", "from": 2, "to": 0}}
  ],
  "message": "Short confirmation of what you changed (mention Render if captions/styles changed)"
}}

Rules:
- Use only the ops listed above.
- Shot indexes refer to main track clips (0-based) unless track is "overlay".
- Prefer 1-5 ops. If the request is unclear, return ops: [] and explain in message.
- Colors must be #RRGGBB. Canvas must be one of 9x16, 16x9, 1x1, 4x5.
- For "make captions yellow/blue/…" prefer set_caption_style primary/highlight. For Host/Guest colors use set_speaker_colors.
- For B-roll / overlay motion requests, use enable_ken_burns with track:"overlay".
"""
    try:
        result = _gemini_generate_clip_suggestions(system_prompt + "\n\nUser request: " + prompt)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    raw_ops = result.get("ops") if isinstance(result, dict) else None
    if raw_ops is None and isinstance(result, list):
        raw_ops = result
    raw_ops = raw_ops or []
    message = ""
    if isinstance(result, dict):
        message = str(result.get("message") or "")[:400]

    allowed = {
        "set_caption_style", "set_speaker_colors", "delete_shot", "set_transition",
        "enable_punch_zoom", "enable_ken_burns", "clear_effects", "set_canvas",
        "set_color_grade", "add_title", "apply_recommended_cuts", "merge_shots",
        "reorder_shot",
    }
    ops = []
    for op in raw_ops:
        if not isinstance(op, dict):
            continue
        name = str(op.get("op") or "")
        if name not in allowed:
            continue
        ops.append(op)
        if len(ops) >= 8:
            break

    return jsonify({"ops": ops, "message": message or f"Applied {len(ops)} edit(s)."})


@app.route("/job-duration/<job_id>", methods=["GET"])
def job_duration(job_id):
    """Duration helper for long-form routing (words end, else ffprobe)."""
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    words = jobs[job_id].get("words") or []
    dur = 0.0
    if words:
        try:
            dur = float(words[-1].get("end", 0) or 0)
        except (TypeError, ValueError):
            dur = 0.0
    if dur <= 0:
        path = find_video_path(job_id)
        if path:
            dur = _ffprobe_duration(path)
    return jsonify({
        "job_id": job_id,
        "duration": dur,
        "is_long_form": dur >= LONG_FORM_SECONDS,
        "long_form_threshold": LONG_FORM_SECONDS,
    })


if __name__ == "__main__":
    # threaded=True is essential: the preview streams large source videos and
    # ffmpeg builds filmstrips/waveforms on demand. Single-threaded, those long
    # requests block seeking, saving, and status polling — making the editor
    # feel frozen. Concurrent workers keep the UI responsive.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8081)),
            debug=False, threaded=True)
