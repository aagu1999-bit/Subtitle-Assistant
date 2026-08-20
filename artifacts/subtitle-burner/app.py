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
from werkzeug.exceptions import RequestEntityTooLarge
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
# Always re-read templates after git pull. Without this, Replit's long-lived
# Flask/gunicorn process keeps a stale compiled index.html while ?v=-busted
# JS reloads — exactly the "missing tlPolishBtn / stale index.html" skew.
app.config["TEMPLATES_AUTO_RELOAD"] = True
try:
    app.jinja_env.auto_reload = True
except Exception:
    pass

CAPCUT_TEMPLATES = {
    # Rich packs applied by Caption look → CapCut Template (and Apply → Timeline).
    # Keys mirror the UI <select id="capcutTemplate"> values.
    "podcast_interview": {
        "label": "Podcast / Interview 9:16",
        "ai_edit_pack": "clarity",
        "canvas": "9x16",
        "font": "Montserrat Thin Black",
        "size": 64,
        "primary": "#FFFFFF",
        "highlight": "#FFD60A",
        "accent": "#00FF88",
        "group": 2,
        "headline": "Mind-Blowing Secret",
        "viral_preset": "hormozi",
        "speaker_colors": True,
        "speaker_color_map": {"SPEAKER_00": "#FFD700", "SPEAKER_01": "#00E5FF"},
        "reframe": {"enabled": True, "top_panel": "active", "bottom_panel": "full"},
        "punch_zoom": {"enabled": False, "intensity": "med"},
        "ken_burns": None,
        "color_grade": None,
        "auto_overlays": False,
        "broll_mode": "auto",
        "broll_placement": "pip",
        "broll_scope": "full",
    },
    "capcut_reels": {
        "label": "CapCut High-Energy Reel",
        "ai_edit_pack": "pulse",
        "canvas": "9x16",
        "font": "Integral CF",
        "size": 68,
        "primary": "#FFFFFF",
        "highlight": "#00F2EA",
        "accent": "#FF0055",
        "group": 1,
        "headline": "",
        "viral_preset": "mrbeast",
        "speaker_colors": False,
        "reframe": {"enabled": False},
        "punch_zoom": {"enabled": True, "intensity": "med"},
        "ken_burns": None,
        "color_grade": None,
        "auto_overlays": True,
        "broll_mode": "auto",
        "broll_placement": "pip",
        "broll_scope": "full",
    },
    "product_spotlight": {
        "label": "Product Spotlight",
        "ai_edit_pack": "velocity",
        "canvas": "9x16",
        "font": "Anton",
        "size": 72,
        "primary": "#00FF88",
        "highlight": "#FF00FF",
        "accent": "#00CFFF",
        "group": 3,
        "headline": "📍 Featured Product",
        "viral_preset": "neon",
        "speaker_colors": False,
        "reframe": {"enabled": False},
        "punch_zoom": {"enabled": True, "intensity": "high"},
        "ken_burns": None,
        "color_grade": "warm",
        "auto_overlays": True,
        "broll_mode": "auto",
        "broll_placement": "center",
        "broll_scope": "selected",
    },
    "cinematic_vlog": {
        "label": "Cinematic Festival Vlog",
        "ai_edit_pack": "film",
        "canvas": "16x9",
        "font": "DM Sans",
        "size": 56,
        "primary": "#F8FAFC",
        "highlight": "#6366F1",
        "accent": "#EC4899",
        "group": 4,
        "headline": "",
        "viral_preset": "karaoke",
        "speaker_colors": False,
        "reframe": {"enabled": False},
        "punch_zoom": {"enabled": False, "intensity": "med"},
        "ken_burns": {"enabled": True, "direction": "in", "intensity": "low"},
        "color_grade": "cinematic",
        "auto_overlays": False,
        "broll_mode": "auto",
        "broll_placement": "center",
        "broll_scope": "playhead",
    },
    # CapCut Always–style: match stills to spoken keywords (photos / AI photos).
    "capcut_always": {
        "label": "Always · Photo Match",
        "ai_edit_pack": "always",
        "canvas": "9x16",
        "font": "Montserrat Thin Black",
        "size": 62,
        "primary": "#FFFFFF",
        "highlight": "#FFE566",
        "accent": "#FF6B9A",
        "group": 2,
        "headline": "",
        "viral_preset": "hormozi",
        "speaker_colors": False,
        "reframe": {"enabled": False},
        "punch_zoom": {"enabled": False, "intensity": "med"},
        "ken_burns": {"enabled": True, "direction": "in", "intensity": "med"},
        "color_grade": "warm",
        "auto_overlays": True,
        "photo_match": True,
        "use_ai_photos": False,
        "broll_mode": "photo",
        "broll_placement": "center",
        "broll_scope": "full",
        # Brand kit composition — Caption Look logo/colors/preset merge on apply.
        "brand_kit": {
            "apply_logo": True,
            "apply_colors": True,
            "caption_preset": "hormozi",
        },
    },
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
        "transition": None,
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
        "transition": None,
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
        "transition": None,
        "caption_preset": "karaoke",
        "style": {
            "font": "DM Sans", "size": 56, "primary": "#F8FAFC",
            "highlight": "#6366F1", "accent": "#EC4899", "group": 3,
        },
        "color_grade": {"preset": "warm", "brightness": 0.02, "contrast": 0.05, "saturation": 0.08},
        # AI Edit no longer seeds text overlays (see _build_ai_edit_timeline) —
        # kept False rather than removed in case older saved projects read it.
        "add_title": False,
    },
    "velocity": {
        "label": "Velocity",
        "blurb": "High intensity — dense zooms, silence cuts, energetic captions",
        "canvas": "9x16",
        "transition": None,
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
        "transition": None,
        "caption_preset": "karaoke",
        "style": {
            "font": "DM Sans", "size": 52, "primary": "#F5F0E8",
            "highlight": "#E8C39E", "accent": "#8B7355", "group": 4,
        },
        "color_grade": {"preset": "cool", "brightness": -0.02, "contrast": 0.08, "saturation": -0.05},
    },
    "always": {
        "label": "Always",
        "blurb": "Photo-match B-roll — keyword stills (stock/AI) with soft Ken Burns",
        "canvas": "9x16",
        "transition": None,
        "caption_preset": "hormozi",
        "photo_match": True,
        "use_ai_photos": False,
        "style": {
            "font": "Montserrat Thin Black", "size": 62, "primary": "#FFFFFF",
            "highlight": "#FFE566", "accent": "#FF6B9A", "group": 2,
        },
        "color_grade": {"preset": "warm", "brightness": 0.03, "contrast": 0.06, "saturation": 0.1},
        "ken_burns": {"enabled": True, "direction": "in", "intensity": "med"},
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

# Default 2 GB — a "500 MB" phone export is often 480–560 MB, and multipart
# framing pushes Content-Length over a hard 500 MB cap. Override with MAX_UPLOAD_MB.
try:
    _max_upload_mb = max(100, int(os.environ.get("MAX_UPLOAD_MB", "2048")))
except (ValueError, TypeError):
    _max_upload_mb = 2048
app.config["MAX_CONTENT_LENGTH"] = _max_upload_mb * 1024 * 1024
ALLOWED_EXT = {"mp4", "mov", "mkv", "webm", "avi", "m4v"}

# Chunked / resumable ingest — each HTTP request stays small so Replit / CDN
# proxies don't kill a multi-minute single POST with a generic "network error".
try:
    UPLOAD_CHUNK_SIZE = max(1_048_576, int(os.environ.get("UPLOAD_CHUNK_SIZE", str(8 * 1024 * 1024))))
except (ValueError, TypeError):
    UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
_pending_uploads: dict[str, dict] = {}
_pending_uploads_lock = threading.Lock()

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
        status = row["status"]
        error = row["error"]
        # Daemon render/transcribe threads die with the process. Mark any
        # in-progress work as error so the UI does not poll forever after
        # Stop+Run / gunicorn restart (common on long >2–3 min encodes).
        if _is_stale_in_progress_status(status):
            status = "error"
            if "analys" in (row["status"] or "").lower() or "speaker" in (row["status"] or "").lower():
                error = (
                    error
                    or "Analyze interrupted by server restart. Click Analyze speakers again."
                )
            else:
                error = (
                    error
                    or "Interrupted by server restart. Click Instant Export / Render again."
                )
        jobs[row["job_id"]] = {
            "status": status,
            "progress": row["progress"],
            "output": row["output"],
            "error": error,
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


def _is_stale_in_progress_status(status: str | None) -> bool:
    """True if this status needed a live worker that no longer exists after restart."""
    s = (status or "").strip().lower()
    if not s or s in ("done", "error", "awaiting_edit"):
        return False
    if s in ("queued", "transcribing", "uploading", "analyzing", "processing"):
        return True
    # British spelling "analysing speakers" / face tracking — common hang after Stop+Run.
    if "analys" in s or "diariz" in s or "speaker" in s or "tracking face" in s:
        return True
    hints = (
        "render", "remux", "mix", "build", "encod", "compos", "burn",
        "final", "mux", "stitch", "overlay", "export", "writing",
    )
    return any(h in s for h in hints)


# Initialise DB and reload persisted jobs before the cleanup thread starts.
_db_init()
_load_jobs_from_db()
# Persist abandoned-job fixes so /status stays honest after reload.
try:
    for _jid, _job in list(jobs.items()):
        if (_job.get("error") or "").startswith("Interrupted by server restart"):
            _db_save_job(_jid)
except Exception:
    pass


def allowed_file(name: str) -> bool:
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED_EXT


@app.errorhandler(RequestEntityTooLarge)
def _handle_too_large(_exc):
    limit_mb = int(app.config.get("MAX_CONTENT_LENGTH", 0) / (1024 * 1024))
    return jsonify({
        "error": (
            f"File too large for a single upload (limit ~{limit_mb} MB). "
            "Compress to H.264 MP4, or use the chunked uploader (auto for large files)."
        ),
    }), 413


def _pending_upload_dir(upload_id: str) -> Path:
    return UPLOAD_DIR / f".chunked_{upload_id}"


def _prune_stale_pending_uploads(max_age_sec: float = 6 * 3600) -> None:
    """Drop abandoned chunked sessions so disk doesn't fill with part files."""
    now = time.time()
    dead: list[str] = []
    with _pending_uploads_lock:
        for uid, meta in list(_pending_uploads.items()):
            if now - float(meta.get("created_at", now)) > max_age_sec:
                dead.append(uid)
        for uid in dead:
            _pending_uploads.pop(uid, None)
    for uid in dead:
        d = _pending_upload_dir(uid)
        try:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _start_transcribe_job(
    video_path: Path,
    filename: str,
    pre_clean: bool = False,
    expected_bytes: int | None = None,
    job_id: str | None = None,
) -> tuple[str, dict]:
    """Validate media, register job, start Whisper thread. Shared by single + chunked upload."""
    job_id = job_id or uuid.uuid4().hex
    pre_probe = _probe_media_streams(video_path)
    if pre_probe.get("error") and not pre_probe.get("has_video") and not pre_probe.get("has_audio"):
        repaired = _repair_uploaded_media(video_path)
        if repaired:
            video_path = repaired
            expected_bytes = None
    probe = _validate_uploaded_media(video_path, expected_bytes=expected_bytes)
    print(
        f"[upload] {job_id} ok size={video_path.stat().st_size} "
        f"audio={probe.get('has_audio')} video={probe.get('has_video')} "
        f"dur={probe.get('duration'):.1f}s name={filename!r}",
        flush=True,
    )
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
        "filename": filename,
        "media_info": probe,
    }
    _db_save_job(job_id)
    t = threading.Thread(target=transcribe_job, args=(job_id, video_path, pre_clean))
    t.daemon = True
    t.start()
    return job_id, {
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
    }


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
# 200M covers a full 10-minute 4K iPhone MOV without truncating the probe.
_FFPROBE_DEEP = ["-analyzeduration", "200M", "-probesize", "200M"]


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


def _repair_uploaded_media(path: Path) -> Path | None:
    """Best-effort container repair for uploads ffprobe can't read at all.

    Some phone exports (long MOVs especially) land with a container flavor
    or moov-atom layout that FFmpeg's demuxer chokes on even at a deep probe.
    A plain stream copy into a fresh MP4 fixes most of these without
    touching a single frame; only re-encode if the copy itself fails (rare,
    usually a genuinely damaged stream).

    On success, replaces *path* on disk with the fixed file — same job_id
    stem, `.mp4` extension — and returns the new Path. Returns None (leaving
    the original file untouched) if nothing could be salvaged.
    """
    tmp = path.with_name(f".{path.stem}.repair.mp4")
    _safe_unlink(tmp)
    base = [FFMPEG, "-y", "-analyzeduration", "200M", "-probesize", "200M", "-i", str(path)]
    attempts = [
        ("remux copy", base + ["-c", "copy", "-movflags", "+faststart", str(tmp)]),
        ("re-encode", base + [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(tmp),
        ]),
    ]
    for label, cmd in attempts:
        _safe_unlink(tmp)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"[repair] {path.name} {label} attempt errored: {e}", flush=True)
            continue
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 8_192:
            _safe_unlink(tmp)
            continue
        probe = _probe_media_streams(tmp)
        if probe.get("error") or (not probe.get("has_video") and not probe.get("has_audio")):
            _safe_unlink(tmp)
            continue
        final = path if path.suffix.lower() == ".mp4" else path.with_suffix(".mp4")
        try:
            if final != path:
                _safe_unlink(path)
            elif final.exists():
                _safe_unlink(final)
            try:
                tmp.replace(final)
            except OSError:
                shutil.move(str(tmp), str(final))
        except OSError as e:
            print(f"[repair] {path.name} could not swap in fixed file: {e}", flush=True)
            _safe_unlink(tmp)
            return None
        print(f"[repair] {path.name} -> {final.name} via {label}", flush=True)
        return final
    _safe_unlink(tmp)
    return None


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
# Slightly denser than 2 fps so scene cuts / quick talker swaps still catch faces.
REFRAME_FACE_SAMPLE_FPS = float(os.environ.get("REFRAME_FACE_SAMPLE_FPS", "3.5"))
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


def _hf_gated_repo_from_error(err_str: str) -> str | None:
    """Pull the gated Hugging Face repo id out of a download error, if present."""
    m = re.search(
        r"huggingface\.co/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
        err_str or "",
    )
    return m.group(1) if m else None


def _pyannote_gate_help(err_str: str = "", *, token: str | None = None) -> str:
    """User-facing checklist when HF rejects a gated pyannote download."""
    blocked = _hf_gated_repo_from_error(err_str)
    # Newer pyannote.audio pulls community-1 (and its PLDA assets) in addition
    # to the classic 3.1 / segmentation-3.0 pair — accept ALL of them.
    must_accept = [
        "https://hf.co/pyannote/speaker-diarization-3.1",
        "https://hf.co/pyannote/segmentation-3.0",
        "https://hf.co/pyannote/speaker-diarization-community-1",
    ]
    if blocked and f"https://hf.co/{blocked}" not in must_accept:
        must_accept.append(f"https://hf.co/{blocked}")
    numbered = " ".join(
        f"({i}) Accept {url}" for i, url in enumerate(must_accept, start=1)
    )
    n = len(must_accept)
    token_hint = (
        f"HF_TOKEN is set in this process ({len(token)} chars)."
        if token else
        "HF_TOKEN is NOT set in the running Studio process."
    )
    blocked_note = (
        f" Blocked download was from `{blocked}`."
        if blocked else ""
    )
    return (
        "Hugging Face rejected the token for pyannote models. "
        f"{token_hint}{blocked_note} "
        "Do ALL of these with the SAME HF account: "
        f"{numbered} "
        f"(you need every gated repo — accepting only diarization-3.1 is not enough) "
        f"({n + 1}) Create a token at https://huggingface.co/settings/tokens "
        "— classic Read token, OR fine-grained with "
        "“Read access to contents of all public gated repos you can access” enabled — "
        f"({n + 2}) Put it in .env.local / host Secrets as HF_TOKEN=hf_... and "
        "RESTART the Studio server. "
        f"Raw error: {(err_str or '')[:280]}"
    )


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
                or "unauthorized" in err_str.lower()
            ):
                raise RuntimeError(
                    _pyannote_gate_help(err_str, token=token)
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


def _hf_token_present() -> bool:
    return bool(
        os.environ.get(HUGGINGFACE_TOKEN_ENV)
        or os.environ.get("HUGGINGFACE_TOKEN")
        or os.environ.get("HF_TOKEN")
    )


def _warm_diarization_pipeline() -> None:
    """Optional background warm. Disabled by default — model download/load
    steals CPU/disk from uploads + Whisper. Set DIARIZATION_WARM=1 to enable.
    """
    try:
        if os.environ.get("DIARIZATION_WARM", "").strip() not in ("1", "true", "True", "yes"):
            return
        if not _hf_token_present():
            return
        _get_diarization_pipeline()
    except Exception as e:
        print(f"[diarize] warm skipped: {e}", flush=True)


# Only runs when DIARIZATION_WARM=1 (off by default so upload stays fast).
threading.Thread(target=_warm_diarization_pipeline, daemon=True).start()


def _probe_analyze_deps() -> dict:
    """Structured Analyze dependency probe (diarization vs faces are independent).

    Speaker colors / diarization only need pyannote + HF token.
    9:16 reframe crops also need mediapipe (often broken on headless Linux
    without libGL) — that must not block Analyze for speakers.
    """
    out = {
        "diarization_ok": False,
        "faces_ok": False,
        "hf_token": _hf_token_present(),
        "pyannote": None,
        "mediapipe": None,
        "error": None,
        "faces_error": None,
    }
    try:
        import pyannote.audio  # noqa: F401
        out["pyannote"] = "ok"
    except ImportError as e:
        out["pyannote"] = f"import failed: {e}"
    except Exception as e:
        out["pyannote"] = f"{type(e).__name__}: {e}"

    try:
        import mediapipe  # noqa: F401
        out["mediapipe"] = "ok"
        out["faces_ok"] = True
    except ImportError as e:
        out["mediapipe"] = f"import failed: {e}"
        out["faces_error"] = out["mediapipe"]
    except Exception as e:
        out["mediapipe"] = f"{type(e).__name__}: {e}"
        out["faces_error"] = out["mediapipe"]

    if out["pyannote"] != "ok":
        out["error"] = f"pyannote.audio unavailable ({out['pyannote']})"
    elif not out["hf_token"]:
        out["error"] = (
            f"{HUGGINGFACE_TOKEN_ENV} env var missing in the running Studio process. "
            f"Set it in the host env / .env.local and restart the server "
            f"(accept licences at https://huggingface.co/pyannote/speaker-diarization-3.1 , "
            f"https://huggingface.co/pyannote/segmentation-3.0 , and "
            f"https://huggingface.co/pyannote/speaker-diarization-community-1)."
        )
    else:
        out["diarization_ok"] = True
    return out


def _reframe_deps_available() -> tuple[bool, str]:
    """Return (ok, msg). Speakers-only Analyze is ok without mediapipe."""
    d = _probe_analyze_deps()
    if d["diarization_ok"]:
        return True, ""
    return False, d["error"] or "Analyze dependencies unavailable"


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


def _diarization_annotation(diar_out):
    """Normalize pyannote 3.x Annotation vs 4.x DiarizeOutput → Annotation.

    Newer community-1 / pyannote.audio 4.x returns DiarizeOutput with
    ``.speaker_diarization``; calling ``.itertracks`` on the wrapper raises
    AttributeError (what users hear as Analyze failed).
    """
    if diar_out is None:
        raise RuntimeError("Diarization pipeline returned empty output")
    if hasattr(diar_out, "itertracks"):
        return diar_out
    for attr in ("speaker_diarization", "exclusive_speaker_diarization", "annotation"):
        ann = getattr(diar_out, attr, None)
        if ann is not None and hasattr(ann, "itertracks"):
            return ann
    # Some builds expose serialize() with a plain list
    if hasattr(diar_out, "serialize"):
        try:
            payload = diar_out.serialize() or {}
            rows = payload.get("diarization") or payload.get("exclusive_diarization") or []
            if isinstance(rows, list) and rows:
                return rows  # handled specially by caller
        except Exception:
            pass
    raise RuntimeError(
        f"Unrecognized diarization output type: {type(diar_out).__name__}. "
        "Update Studio or pin pyannote.audio; expected Annotation or DiarizeOutput."
    )


def _segments_from_diar_annotation(ann) -> list[dict]:
    """Convert Annotation (or serialized list) → [{start,end,speaker}]."""
    segments: list[dict] = []
    if isinstance(ann, list):
        for row in ann:
            if not isinstance(row, dict):
                continue
            try:
                segments.append({
                    "start": round(float(row.get("start", 0)), 3),
                    "end": round(float(row.get("end", 0)), 3),
                    "speaker": str(row.get("speaker") or "SPEAKER_00"),
                })
            except (TypeError, ValueError):
                continue
        segments.sort(key=lambda s: s["start"])
        return segments
    for turn, _, speaker in ann.itertracks(yield_label=True):
        segments.append({
            "start": round(float(turn.start), 3),
            "end": round(float(turn.end), 3),
            "speaker": str(speaker),
        })
    segments.sort(key=lambda s: s["start"])
    return segments


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
        ann = _diarization_annotation(diar)
        return _segments_from_diar_annotation(ann)
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

    # MediaPipe: good for frontal faces (slightly lower threshold so
    # soft lighting / partial faces still register for split crops).
    detector = mp.solutions.face_detection.FaceDetection(
        model_selection=1, min_detection_confidence=0.28,
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

    Face tracking is best-effort: if mediapipe/OpenCV isn't available (common
    on headless Linux without libGL), we still return speaker diarization so
    Ingest Analyze / speaker colors work.

    Returns a JSON-serializable payload that's cached per job for the
    compositor to consume. Caller is responsible for spawning this in a
    background thread — both passes are CPU-heavy.
    """
    deps = _probe_analyze_deps()
    if not deps["diarization_ok"]:
        raise RuntimeError(deps["error"] or "Diarization dependencies unavailable")

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
    faces_warning: str | None = None
    errors: list[BaseException] = []
    # Heartbeat so the UI progress bar moves during long pyannote loads.
    stop_hb = threading.Event()
    hb_pct = {"n": 15}

    def _heartbeat() -> None:
        while not stop_hb.wait(7.0):
            hb_pct["n"] = min(52, hb_pct["n"] + 2)
            _progress(hb_pct["n"], "analysing speakers (diarization running…)")

    hb_thread = threading.Thread(target=_heartbeat, name="reframe-hb", daemon=True)
    hb_thread.start()

    def _run_diar():
        return diarize_audio(
            video_path,
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
        )

    def _run_faces():
        if not deps["faces_ok"]:
            raise RuntimeError(deps.get("faces_error") or "Face tracking unavailable")
        return detect_face_tracks(video_path, sample_fps=fps)

    try:
        if deps["faces_ok"]:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="reframe") as pool:
                fut_diar = pool.submit(_run_diar)
                fut_faces = pool.submit(_run_faces)
                for fut in as_completed([fut_diar, fut_faces]):
                    try:
                        if fut is fut_diar:
                            diar = fut.result()
                            stop_hb.set()
                            _progress(55, "speakers labelled — tracking faces")
                        else:
                            faces = fut.result()
                            _progress(70, "faces sampled — finishing diarization")
                    except BaseException as e:
                        if fut is fut_diar:
                            errors.append(e)
                        else:
                            faces_warning = str(e)
                            faces = []
                            _progress(70, "speakers labelled — faces skipped")
            if errors:
                raise errors[0]
        else:
            # Speakers-only path (no mediapipe / libGL).
            faces_warning = deps.get("faces_error") or "Face tracking unavailable"
            try:
                diar = _run_diar()
            except BaseException:
                raise
            stop_hb.set()
            _progress(70, "speakers labelled — faces skipped (no mediapipe)")
    finally:
        stop_hb.set()

    overlaps = find_overlap_regions(diar)
    speakers = sorted({s["speaker"] for s in diar}, key=_speaker_sort_key)
    face_hit_frames = sum(1 for s in faces if (s.get("faces") or []))
    face_box_total = sum(len(s.get("faces") or []) for s in faces)
    # Distinguish "detector never ran" from "ran but saw nobody".
    if faces_warning:
        faces_skipped = True
        faces_status = "skipped"
    elif not faces:
        faces_skipped = True
        faces_status = "empty"
        faces_warning = faces_warning or "No face samples produced"
    elif face_hit_frames == 0:
        faces_skipped = False
        faces_status = "no_detections"
        faces_warning = (
            "Face detector ran but found 0 faces — try better lighting / "
            "frontal angles, or Re-analyze after a closer crop."
        )
    else:
        faces_skipped = False
        faces_status = "ok"
    _progress(90, "caching speaker map")
    return {
        "diarization": diar,
        "overlaps": overlaps,
        "faces": faces,
        "stats": {
            "speakers": speakers,
            "speaker_count": len(speakers),
            "face_samples": len(faces),
            "face_hit_frames": face_hit_frames,
            "face_box_total": face_box_total,
            "overlap_seconds": round(sum(o["end"] - o["start"] for o in overlaps), 2),
            "diarization_device": _diarization_device_resolved,
            "speaker_breakdown": _speaker_breakdown(diar),
            "face_sample_fps": fps,
            "faces_skipped": faces_skipped,
            "faces_status": faces_status,
            "faces_warning": faces_warning,
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
    # ---- Viral hook / headline (opening card) ----
    hook_text = ""
    hook_font = "Bebas Neue"
    hook_dur = 2.5
    hook_mode = "hook"  # hook = timed open card; banner = full-duration strip
    ht = style.get("hook_title") if isinstance(style.get("hook_title"), dict) else None
    if ht and str(ht.get("text") or "").strip():
        hook_text = str(ht.get("text") or "").strip()[:120]
        hook_font = str(ht.get("font") or style.get("hook_font") or "Bebas Neue").strip() or "Bebas Neue"
        try:
            hook_dur = float(ht.get("duration_sec") or style.get("hook_duration") or 2.5)
        except (TypeError, ValueError):
            hook_dur = 2.5
        hook_mode = str(ht.get("mode") or "hook").lower()
    else:
        raw_banner = headline_banner if headline_banner is not None else style.get("headline_banner")
        if isinstance(raw_banner, dict):
            hook_text = str(raw_banner.get("text") or "").strip()[:120]
            hook_font = str(raw_banner.get("font") or style.get("hook_font") or "Bebas Neue").strip() or "Bebas Neue"
            try:
                hook_dur = float(raw_banner.get("duration_sec") or 2.5)
            except (TypeError, ValueError):
                hook_dur = 2.5
            hook_mode = str(raw_banner.get("mode") or "hook").lower()
        elif raw_banner:
            hook_text = str(raw_banner).strip()[:120]
            hook_font = str(style.get("hook_font") or "Bebas Neue").strip() or "Bebas Neue"
            try:
                hook_dur = float(style.get("hook_duration") or 2.5)
            except (TypeError, ValueError):
                hook_dur = 2.5
            hook_mode = str(style.get("hook_mode") or "hook").lower()
    hook_dur = max(0.8, min(8.0, hook_dur))
    if hook_text:
        banner_size = max(28, int(font_size * 0.9))
        banner_style = (
            f"Style: Banner,{hook_font},{banner_size},{primary},{primary},"
            f"{outline},&H80000000,-1,0,0,0,100,100,0,0,3,0,0,2,20,20,20,1\n"
        )

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
                # Prefer stamped word.speaker (Timeline remaps to output time);
                # fall back to diarization mid-point match (source-time jobs).
                spk = w.get("speaker") if isinstance(w, dict) else None
                if not spk and diarization and speaker_colors:
                    word_mid = (float(w.get('start', 0)) + float(w.get('end', 0))) / 2
                    for seg in diarization:
                        if seg['start'] <= word_mid <= seg['end']:
                            spk = seg.get('speaker')
                            break
                if spk and speaker_colors and spk in speaker_colors:
                    hc = speaker_colors[spk]
                    if isinstance(hc, str) and hc.startswith('#'):
                        hc = hc[1:]
                    if isinstance(hc, str) and len(hc) == 6:
                        speaker_color = f"&H{hc[4:6]}{hc[2:4]}{hc[0:2]}&"

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

    if hook_text:
        banner_y = int(video_h * 0.08)
        last_end = float(words[-1].get("end", 0)) if words else hook_dur
        if hook_mode == "banner":
            banner_end = max(hook_dur, last_end)
        else:
            banner_end = min(hook_dur, max(hook_dur, 0.8))
        banner_end_ts = ass_timestamp(banner_end)
        # Uppercase viral hooks read more like TikTok/Reels opens.
        hook_draw = fmt(hook_text) if all_caps else hook_text
        lines.append(
            f"Dialogue: 0,0:00:00.00,{banner_end_ts},Banner,,0,0,0,,"
            f"{{\\pos({pos_x},{banner_y})\\bord0\\shad0\\3c&H000000&\\3a&H80&}}{hook_draw}"
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
        # Match output_path's container (.m4a for the Timeline Render caller)
        # so the rename below doesn't leave a raw-ADTS/MP4 mismatch on disk.
        rebuilt = output_path.with_suffix(f".rebuilt{output_path.suffix or '.m4a'}")
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

# QuickTime / iOS / Safari-friendly H.264 MP4 trailer args. Append on *final*
# outputs (not every intermediate). Non-yuv420p, missing faststart, or odd
# profiles are the usual reason macOS QuickTime says "media isn't compatible".
_QT_SAFE_MP4_ARGS = [
    "-pix_fmt", "yuv420p",
    "-profile:v", "high",
    "-level", "4.1",
    "-movflags", "+faststart",
]


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

    # Match enhanced_path's container so the final rename doesn't leave a
    # raw-ADTS/MP4 mismatch on disk (matters for the .m4a path used by
    # Timeline Render's _tl_apply_project_audio).
    out_tmp = enhanced_path.with_suffix(f".post{enhanced_path.suffix or '.m4a'}")
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
    job_id: str | None = None,
):
    """Burn the ASS file into the video using FFmpeg.

    If *silent* is True, the output has no audio track at all (-an). This is
    used by the burn cache: the silent video can be cached and remuxed with
    different audio later without re-encoding the (slow) video.
    Otherwise: if *audio_path* is supplied, it replaces the audio stream;
    otherwise the source video's audio is copied through.
    If *quality_boost* is True, the source is upscaled (lanczos) so the short
    edge is at least 1080 px, with a light unsharp pass.
    Optional *job_id* streams encode progress into jobs[job_id]["progress"].
    """
    # FFmpeg will silently ignore -vf / libx264 when the input has no video
    # stream and happily write an audio-only MP4 — catch that early.
    if not _probe_media_streams(video_path).get("has_video"):
        raise RuntimeError(
            "Caption burn: input has no video track (audio-only). "
            "Re-render from Timeline → ▶ Render."
        )

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
        cmd += ["-map", "0:v:0", "-vf", vf, *_VIDEO_ENC_ARGS, "-an"]
    else:
        if audio_path:
            cmd += ["-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0"]
        else:
            # Force video mapping so an audio-only source cannot "succeed".
            cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
        # Re-encode audio to AAC-LC (not stream-copy) so QuickTime / iOS
        # don't reject exotic source codecs after caption burn.
        cmd += [
            "-vf", vf, *_VIDEO_ENC_ARGS,
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        ]
        if audio_path:
            # Cap to the picture length — never let short replacement audio
            # truncate the video via -shortest.
            burn_dur = _media_duration(video_path)
            if burn_dur > 0.05:
                cmd += ["-t", f"{burn_dur:.3f}"]
    cmd += [*_QT_SAFE_MP4_ARGS, str(output_path)]

    duration_hint = _media_duration(video_path) or None
    ok = _run_ffmpeg_encode(
        cmd,
        what="caption burn",
        job_id=job_id,
        progress_lo=80,
        progress_hi=94,
        duration_hint=duration_hint,
    )
    if not ok and VIDEO_ENC_NAME == "h264_videotoolbox":
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
            fallback += [
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            ]
            if audio_path and duration_hint and duration_hint > 0.05:
                fallback += ["-t", f"{duration_hint:.3f}"]
        fallback += [*_QT_SAFE_MP4_ARGS, str(output_path)]
        ok = _run_ffmpeg_encode(
            fallback,
            what="caption burn (libx264 fallback)",
            job_id=job_id,
            progress_lo=80,
            progress_hi=94,
            duration_hint=duration_hint,
        )

    if not ok:
        raise RuntimeError("FFmpeg caption burn failed (see ffmpeg_render.log)")


def _run_ffmpeg_encode(
    cmd: list,
    what: str = "FFmpeg",
    job_id: str | None = None,
    progress_lo: int | None = None,
    progress_hi: int | None = None,
    duration_hint: float | None = None,
) -> bool:
    """Run an ffmpeg encode without buffering all stderr in memory.

    When *job_id* + progress range are set, streams `-progress` so long
    encodes (>2–3 min) keep updating /status instead of freezing at 80%.
    Returns True on exit code 0.
    """
    if not cmd or cmd[0] != FFMPEG:
        raise ValueError(f"{what}: command must start with FFMPEG binary")

    run_cmd = [FFMPEG, "-hide_banner", "-nostats"]
    rest = cmd[1:]
    track_progress = bool(job_id and progress_lo is not None and progress_hi is not None)
    if track_progress:
        run_cmd += ["-progress", "pipe:1"]
    run_cmd += rest

    try:
        proc = subprocess.Popen(
            run_cmd,
            stdout=subprocess.PIPE if track_progress else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as e:
        ffmpeg_logger.error(f"{what} spawn failed: {e}")
        return False

    last_pct = int(progress_lo) if progress_lo is not None else 0
    last_db = 0.0
    stderr_tail: list[str] = []

    def _drain_stderr():
        if not proc.stderr:
            return
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            stderr_tail.append(line)
            if len(stderr_tail) > 40:
                stderr_tail.pop(0)

    # Drain stderr on a side thread so a full pipe can't deadlock.
    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    try:
        if track_progress and proc.stdout:
            for raw in proc.stdout:
                line = (raw or "").strip()
                if not line:
                    continue
                if line.startswith("out_time_ms=") and duration_hint and duration_hint > 0.5:
                    try:
                        ms = int(line.split("=", 1)[1].strip() or "0")
                    except ValueError:
                        continue
                    frac = min(1.0, max(0.0, (ms / 1000.0) / duration_hint))
                    pct = int(progress_lo + (progress_hi - progress_lo) * frac)
                    if pct > last_pct and job_id in jobs:
                        last_pct = pct
                        jobs[job_id]["progress"] = pct
                        # Throttle SQLite writes during long encodes.
                        now = time.time()
                        if now - last_db >= 2.5:
                            last_db = now
                            try:
                                _db_save_job(job_id)
                            except Exception:
                                pass
                elif line == "progress=end":
                    break
        proc.wait()
    finally:
        err_thread.join(timeout=5)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    if proc.returncode != 0:
        tail = "\n".join(stderr_tail)[-2000:]
        ffmpeg_logger.error(f"{what} failed (code {proc.returncode}): {tail}")
        return False
    return True


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
    ]
    # Cap to picture length. Do NOT use -shortest: short/corrupt audio can
    # stop the mux before any video packets are copied (audio-only MP4).
    vid_dur = _media_duration(silent_video)
    if vid_dur > 0.05:
        cmd += ["-t", f"{vid_dur:.3f}"]
    cmd += [
        "-movflags", "+faststart",
        str(output_path),
    ]
    if not _run_ffmpeg_encode(cmd, what="audio remux"):
        raise RuntimeError("Audio remux failed (see ffmpeg_render.log)")
    _assert_mp4_has_video(output_path, "audio remux")


def _assert_mp4_has_video(path: Path, what: str = "export") -> None:
    """Reject audio-only / empty MP4s so downloads never ship a soundtrack without picture."""
    if not path or not path.exists() or path.stat().st_size < 1024:
        raise RuntimeError(f"{what} produced an empty or missing file.")
    probe = _probe_media_streams(path)
    if not probe.get("has_video"):
        raise RuntimeError(
            f"{what} produced an audio-only file (no video track). "
            "Try Timeline → ▶ Render again. If it keeps failing, turn off Look → "
            "Audio Enhancement and Project → Auto whoosh/click, then re-export."
        )


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
# Native image model for optional Suggest B-roll AI photos (opt-in).
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
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
    "punch_zoom": (0.6, 3.0),    # fast push-in — structural beats only
    "zoom_1_5": (1.2, 4.0),      # 1.5x hold — light mid-video emphasis
    "zoom_2x": (1.0, 3.5),       # 2x hold — strong mid-video emphasis
    "ken_burns": (3.0, 12.0),    # slow drift over a longer stretch
    "split_screen": (1.0, 15.0),  # both speakers framed together
}


def _build_effect_suggestion_prompt(transcript: str, total: float,
                                    max_effects: int,
                                    purpose: str | None = None) -> str:
    """Ask Gemini where camera moves would earn their keep."""
    purpose = (purpose or "").strip()
    purpose_block = ""
    if purpose:
        purpose_block = f"""
Editor's stated purpose for this edit (honor this above generic social defaults):
\"{purpose[:500]}\"
Bias effect choices toward that goal. If the purpose asks for calm / documentary /
cinematic pacing, prefer ken_burns and fewer punch_zooms. If it asks for viral /
hooks / energy, prefer punch_zoom on strongest lines.
"""
    long_note = ""
    if total >= 60:
        third = total / 3.0
        long_note = f"""
SPREAD REQUIREMENT — this edit is {total:.0f}s long, split it into thirds for
planning purposes: hook [0s-{third:.0f}s], mid [{third:.0f}s-{2 * third:.0f}s],
late [{2 * third:.0f}s-{total:.0f}s]. Place AT LEAST ONE accent in each third —
energy has to last the whole runtime, not just the opening.
Do NOT cluster all moves in the first 45-60 seconds; that is the single most
common mistake and it makes the back half of the edit feel dead.
For videos longer than 2 minutes, target roughly one accent every 20-35s of
kept runtime (not source runtime) all the way to the end, not just the start.
"""
    if total >= 240:
        long_note += f"""
This is a LONGER cut (~{total / 60:.0f} min). Still prefer fewer, stronger
moments than accents everywhere (max {max_effects}), but they must be spread
the full length — a hook-heavy first minute followed by 3+ silent minutes is
a failed edit even if the moves themselves are good.
"""
    hook_block = ""
    hook_shape = ""
    if total >= 45:
        hook_block = """
This edit covers a FULL VIDEO (not just a short clip). Also identify the strongest
HOOK moment (3-8s) that should OPEN the finished edit even if it occurs mid-transcript.
Prefer surprising claims, open loops, or quotable lines over polite introductions.
Report it in "structure" below — this is separate from the camera-move effects.
"""
        hook_shape = """,
  "structure": {
    "hook_start_time": 42.1,
    "hook_end_time": 47.8,
    "hook_quote": "the exact words that make this the hook",
    "hook_reason": "why this should open the edit"
  }"""
    return f"""You are an expert video editor deciding where camera moves belong in a talking-head edit. The transcript below has [mm:ss] timestamps. The video is {total:.1f} seconds long.
{purpose_block}{long_note}{hook_block}
Choose at most {max_effects} moments. Fewer is better — a move that isn't motivated is worse than no move at all. Never cover the whole video; these are accents.

Effects you may place (pick the RIGHT tool — do not overuse punch_zoom):
- "punch_zoom": FAST push-in for a STRUCTURAL beat only — scene/scenery change, hook→intro pivot, major topic shift, section boundary. Use sparingly (typically 1–3 in a full video). Duration {_EFFECT_LIMITS['punch_zoom'][0]}-{_EFFECT_LIMITS['punch_zoom'][1]}s.
- "zoom_1_5": HOLD at 1.5x while someone says something interesting, answers a question, or lightly emphasizes a phrase. Duration {_EFFECT_LIMITS['zoom_1_5'][0]}-{_EFFECT_LIMITS['zoom_1_5'][1]}s. This should be the MOST COMMON mid-video accent.
- "zoom_2x": HOLD at 2x for stronger emphasis — funny line, blunt take, hot take, big claim. Duration {_EFFECT_LIMITS['zoom_2x'][0]}-{_EFFECT_LIMITS['zoom_2x'][1]}s. Less common than zoom_1_5.
- "ken_burns": slow drift on longer explanation stretches. Duration {_EFFECT_LIMITS['ken_burns'][0]}-{_EFFECT_LIMITS['ken_burns'][1]}s.

Rules:
- Effects must not overlap each other.
- Anchor each one to what is actually said at that timestamp; quote it.
- Keep every time within 0 and {total:.1f} seconds.
- Prefer zoom_1_5 / zoom_2x for mid-sentence emphasis. Do NOT use punch_zoom for those — save punch_zoom for real structural beats (scenery/topic/section changes).
- At most ~20% of accents should be punch_zoom on videos longer than 90s.
- intensity is "low", "med" or "strong". Reserve "strong" for the single biggest beat.
- For ken_burns, direction is "in" (push in) or "out" (pull back).
- Semantic stress cues (Captions-style): list markers ("first", "number one", "second"), secrets/reveals ("secret", "the truth is"), contrasts ("but", "however"), punchlines, and new-sentence pivots are strong candidates for zoom_1_5 / zoom_2x — place the hold ON the emphasized word when the transcript makes that clear.

Return JSON in exactly this shape:
{{
  "effects": [
    {{
      "type": "punch_zoom",
      "start_time": 4.0,
      "end_time": 5.4,
      "intensity": "strong",
      "direction": "in",
      "quote": "the exact words at the section/topic change",
      "reason": "hook pivots into the introduction — structural beat"
    }},
    {{
      "type": "zoom_1_5",
      "start_time": 12.4,
      "end_time": 14.9,
      "intensity": "med",
      "direction": "in",
      "quote": "the exact words being lightly emphasised",
      "reason": "interesting claim worth leaning into"
    }},
    {{
      "type": "zoom_2x",
      "start_time": 38.1,
      "end_time": 40.0,
      "intensity": "strong",
      "direction": "in",
      "quote": "the exact words of the hot take",
      "reason": "blunt hot take — the strongest line in this stretch"
    }}
  ]{hook_shape}
}}

TRANSCRIPT:
{transcript}
"""


def _overlap_split_suggestions(job_id: str, total: float) -> list[dict]:
    """Split-screen ranges taken straight from diarization overlaps.

    Manifests as Effects-lane ``split_screen`` blocks (not a separate track).
    Even when a video has 6+ identified speakers, each overlap only frames the
    **up to two** voices active in that window — faces (when Analyze found them)
    steer the crop toward those talkers at burn/reframe time.
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
        spk_list = ov.get("speakers") or []
        if isinstance(spk_list, (list, tuple)):
            # Cap at two concurrent talkers for the split layout.
            spk_pair = [str(x) for x in list(spk_list)[:2]]
        else:
            spk_pair = []
        mid = (s + e) / 2.0
        face_a = _face_anchor_at(job_id, mid)
        out.append({
            "type": "split_screen",
            "start_time": s,
            "end_time": min(e, s + hi),
            "intensity": "med",
            "direction": "in",
            "quote": "",
            "reason": (
                "Two people talking at once — Effects → Split-screen (max 2 panels). "
                "Other speakers stay labelled; they get the frame when they talk alone."
            ),
            "source": "diarization",
            "speakers": spk_pair,
            "face_anchor": face_a,
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
    """Clamp to the video, enforce per-type durations, drop overlaps.

    Accepts punch_zoom, zoom_1_5, zoom_2x, ken_burns, split_screen (anything
    else is dropped) — the membership check is just "is it in
    _EFFECT_LIMITS". intensity stays a free "low"/"med"/"strong" label here
    even for the zoom holds; the effects-lane renderer maps zoom_1_5/zoom_2x
    to their fixed scale factor later (intensity is cosmetic for those two).
    """
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

    # Gemini has a habit of front-loading energy into the first minute and
    # going quiet for the rest of a long edit. Flag it loudly so it's easy to
    # spot in logs — the caller (ai_edit_seed) runs _ensure_effects_span_timeline
    # afterward to backfill sparse late regions.
    if total > 90 and kept:
        early_cutoff = total * 0.4
        early_count = sum(1 for c in kept if c["start_time"] < early_cutoff)
        if early_count / len(kept) > 0.7:
            ai_logger.warning(
                f"[ai-edit] effect suggestions are front-loaded: "
                f"{early_count}/{len(kept)} start before {early_cutoff:.0f}s "
                f"of a {total:.0f}s timeline"
            )
    return kept


def _ensure_effects_span_timeline(effects: list, t_in: float, t_out: float,
                                   words: list | None, max_fx: int) -> list[dict]:
    """Backfill sparse later regions of a long edit with light zoom_1_5 accents.

    Gemini's effect suggestions tend to cluster in the first minute of a long
    edit and go quiet after that, which reads as the energy dying halfway
    through. This scans for gaps wider than ~30s with no effect coverage and
    drops a short, low-intensity zoom_1_5 hold near the gap's midpoint —
    snapped to the nearest transcript word so it lands on speech instead of
    dead air. Uses zoom_1_5 (not punch_zoom) because these are mid-video
    emphasis fills, not structural beats. Never exceeds max_fx and never
    overlaps an existing effect.
    """
    span = t_out - t_in
    if span < 90:
        return effects

    max_fx = max(1, int(max_fx or 4))
    effects = list(effects or [])

    # If the budget is already full but everything is stuck in the first ~40%,
    # drop the weakest/latest-of-early accents to free slots for the back half.
    if effects and span >= 90:
        cutoff = t_in + span * 0.4
        early = [e for e in effects if e["start_time"] < cutoff]
        late = [e for e in effects if e["start_time"] >= cutoff]
        if len(early) >= max(2, int(0.7 * len(effects))) and len(late) < 2:
            keep_early = max(1, max_fx // 3)
            early_sorted = sorted(early, key=lambda e: e["start_time"])
            effects = early_sorted[:keep_early] + late
            print(
                f"[ai-edit] Front-loaded effects trimmed "
                f"({len(early)} early → {keep_early}) to free budget for later accents",
                flush=True,
            )

    if len(effects) >= max_fx:
        return effects

    target_gap = 30.0
    sorted_fx = sorted(effects, key=lambda e: e["start_time"])

    gaps: list[tuple[float, float]] = []
    cursor = t_in
    for e in sorted_fx:
        if e["start_time"] - cursor > target_gap:
            gaps.append((cursor, e["start_time"]))
        cursor = max(cursor, e["end_time"])
    if t_out - cursor > target_gap:
        gaps.append((cursor, t_out))
    if not gaps:
        return effects

    word_starts = sorted({
        round(float(w.get("start", 0) or 0), 2)
        for w in (words or [])
        if t_in <= float(w.get("start", 0) or 0) <= t_out
    })

    lo, hi = _EFFECT_LIMITS["zoom_1_5"]
    dur = max(lo, min(hi, 1.8))
    added: list[dict] = []
    for gs, ge in gaps:
        if len(effects) + len(added) >= max_fx:
            break
        if ge - gs < dur + 1.0:
            continue  # gap too tight to fit an accent with breathing room
        mid = (gs + ge) / 2.0
        # Prefer gaps in the back half of the cut
        if mid < t_in + span * 0.35 and any(g[0] >= t_in + span * 0.4 for g in gaps):
            continue
        anchor_t = min(word_starts, key=lambda w: abs(w - mid)) if word_starts else mid
        start = min(max(anchor_t, gs + 0.5), ge - dur - 0.5)
        end = start + dur
        overlaps = any(start < e["end_time"] and e["start_time"] < end for e in effects + added)
        if overlaps:
            continue
        quote = ""
        for w in (words or []):
            try:
                ws = float(w.get("start", 0) or 0)
            except (TypeError, ValueError):
                continue
            if abs(ws - start) < 1.5:
                quote = str(w.get("word") or w.get("text") or "").strip()
                if quote:
                    break
        added.append({
            "type": "zoom_1_5",
            "start_time": round(start, 2),
            "end_time": round(end, 2),
            "intensity": "low",
            "direction": "in",
            "quote": quote[:120],
            "reason": "auto energy accent — mid-video emphasis hold",
            "auto_spread": True,
        })
    if added:
        print(f"[ai-edit] Added {len(added)} late-timeline energy accent(s)", flush=True)
        effects = sorted(effects + added, key=lambda e: e["start_time"])
    return effects


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
                job_id=job_id,
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

        # Verify the final mp4 is real video (not audio-only) before announcing done.
        if not output_path.exists() or output_path.stat().st_size < 1024:
            raise RuntimeError(
                "Render produced an empty or missing output file."
            )
        _assert_mp4_has_video(output_path, "Instant Export")
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
                    # Duck MUSIC under speech (sidechain key = voice).
                    filter_complex = (
                        f"[1:a]volume={vol}dB[mus];"
                        f"[mus][0:a]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350:makeup=1[musicduck];"
                        f"[0:a][musicduck]amix=inputs=2:duration=first:dropout_transition=2[outa]"
                    )
                else:
                    filter_complex = (
                        f"[1:a]volume={vol}dB[mus];"
                        f"[0:a][mus]amix=inputs=2:duration=first:dropout_transition=2[outa]"
                    )
                    
                cmd = [
                    FFMPEG, "-y",
                    "-i", str(output_path),
                    "-i", str(bg_music_path),
                    "-filter_complex", filter_complex,
                    "-map", "0:v:0", "-map", "[outa]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                ]
                ie_dur = _media_duration(output_path)
                if ie_dur > 0.05:
                    cmd += ["-t", f"{ie_dur:.3f}"]
                cmd += [str(mixed_output)]
                ffmpeg_logger.info(f"[{job_id}] Mixing bg music: {' '.join(cmd)}")
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode == 0:
                    try:
                        _assert_mp4_has_video(mixed_output, "Instant Export music mix")
                        shutil.move(mixed_output, output_path)
                    except RuntimeError as ve:
                        ffmpeg_logger.error(f"[{job_id}] Mixing bg music dropped video: {ve}")
                        _safe_unlink(mixed_output)
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
    key, cx = _google_cse_creds()
    return {
        "serpapi": bool(_serpapi_api_key()),
        "google_cse": bool(key and cx),
        "pexels": bool(_pexels_api_key()),
        "unsplash": bool((os.environ.get("UNSPLASH_ACCESS_KEY") or "").strip()),
        "gemini_image": bool((os.environ.get("GEMINI_API_KEY") or "").strip()),
        "badge": True,
    }


def _serpapi_api_key() -> str:
    """SerpAPI key — Replit secret name: SERPAPI_API_KEY."""
    for name in ("SERPAPI_API_KEY", "SERP_API_KEY", "SERPAPI_KEY"):
        raw = os.environ.get(name)
        if raw is None:
            continue
        val = raw.strip().strip('"').strip("'")
        if val:
            return val
    return ""


def _pexels_api_key() -> str:
    """Return trimmed Pexels key (Replit/UI pastes often include trailing spaces).

    Accepts common alias names so a mistyped Replit secret still works.
    """
    for name in ("PEXELS_API_KEY", "PEXELS_KEY", "PEXELS_API"):
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return ""


def _pexels_env_aliases_present() -> list:
    """Secret *names* present in the process that look like Pexels (no values)."""
    out = []
    for k in os.environ:
        ku = k.upper()
        if "PEXEL" in ku:
            out.append(k)
    return sorted(out)


def _probe_pexels_key() -> dict:
    """Live check against Pexels — never returns the key value."""
    key = _pexels_api_key()
    if not key:
        return {
            "configured": False,
            "ok": False,
            "http_status": None,
            "message": "PEXELS_API_KEY is missing in this process. "
                       "Set the secret name exactly PEXELS_API_KEY, then restart Studio.",
        }
    try:
        import requests as _req
        r = _req.get(
            "https://api.pexels.com/v1/search",
            params={"query": "nature", "per_page": 1},
            headers={"Authorization": key},
            timeout=15,
        )
        if r.status_code == 200:
            photos = (r.json() or {}).get("photos") or []
            return {
                "configured": True,
                "ok": True,
                "http_status": 200,
                "key_len": len(key),
                "key_prefix": key[:4] + "…",
                "hits": len(photos),
                "message": "Pexels accepted the key.",
            }
        if r.status_code in (401, 403):
            return {
                "configured": True,
                "ok": False,
                "http_status": r.status_code,
                "key_len": len(key),
                "key_prefix": key[:4] + "…",
                "message": "Pexels rejected the key (unauthorized). "
                           "Regenerate at https://www.pexels.com/api/ and update the secret.",
            }
        return {
            "configured": True,
            "ok": False,
            "http_status": r.status_code,
            "key_len": len(key),
            "key_prefix": key[:4] + "…",
            "message": f"Pexels returned HTTP {r.status_code}. Try again in a minute.",
        }
    except Exception as e:
        return {
            "configured": True,
            "ok": False,
            "http_status": None,
            "key_len": len(key),
            "message": f"Could not reach Pexels: {e}",
        }


def _google_cse_creds() -> tuple[str, str]:
    """Return (api_key, cx). Strips whitespace/quotes; accepts common secret aliases."""
    key = ""
    for name in (
        "GOOGLE_CSE_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_CUSTOM_SEARCH_API_KEY",
        "CSE_API_KEY",
    ):
        raw = os.environ.get(name)
        if raw is None:
            continue
        val = raw.strip().strip('"').strip("'")
        if val:
            key = val
            break
    cx = ""
    for name in (
        "GOOGLE_CSE_CX",
        "GOOGLE_CX",
        "GOOGLE_SEARCH_ENGINE_ID",
        "CSE_CX",
        "CX",
    ):
        raw = os.environ.get(name)
        if raw is None:
            continue
        val = raw.strip().strip('"').strip("'")
        if val:
            cx = val
            break
    return key, cx


def _cse_error_tip(status_code: int, err: dict | None, err_msg: str) -> str:
    """Actionable tip from Google CSE JSON error (no secrets)."""
    reason = ""
    status = ""
    if isinstance(err, dict):
        status = str(err.get("status") or "")
        errors = err.get("errors") or []
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = str(errors[0].get("reason") or "")
    blob = f"{err_msg} {status} {reason}".lower()

    # Most common 2026 failure: API closed to new Cloud projects.
    if (
        "does not have the access" in blob
        or "permission_denied" in blob
        or (status_code == 403 and reason in ("forbidden", "accessNotConfigured"))
    ):
        return (
            " Google closed Custom Search JSON API to *new* Cloud projects "
            "(even if the API toggle is ON). Existing customers only until Jan 2027. "
            "Fix for Studio: use PEXELS_API_KEY and/or Unsplash / Generate AI photos — "
            "those work without CSE. Or try an older GCP project that already had CSE quota."
        )
    if "referer" in blob or "referrer" in blob or "ip" in blob and "block" in blob:
        return (
            " API key restrictions are blocking Replit servers. "
            "Google Cloud → Credentials → your key → Application restrictions → "
            "set to None (or allow server IPs), and API restrictions → Custom Search API."
        )
    if "billing" in blob:
        return " Link a billing account on the same GCP project as the API key."
    if "image" in blob or "searchtype" in blob:
        return " Enable Image search on the Programmable Search Engine (cx)."
    if "api key" in blob or "keyinvalid" in blob.replace(" ", "") or "keyexpired" in blob:
        return " Check GOOGLE_CSE_API_KEY (no quotes/spaces) and that it belongs to this project."
    if "cx" in blob or "invalid argument" in blob:
        return " Check GOOGLE_CSE_CX is the Search engine ID (not the API key)."
    if status_code == 403:
        return (
            " Typical 403 causes: (1) CSE JSON API not entitled for this project "
            "(closed to new customers), (2) API key HTTP-referrer/IP restrictions, "
            "(3) wrong project. Prefer Pexels for photos on Replit."
        )
    return ""


def _probe_google_cse() -> dict:
    """Live check against Google Custom Search (image) — never returns secrets."""
    key, cx = _google_cse_creds()
    missing = []
    if not key:
        missing.append("GOOGLE_CSE_API_KEY")
    if not cx:
        missing.append("GOOGLE_CSE_CX")
    if missing:
        return {
            "configured": False,
            "ok": False,
            "http_status": None,
            "message": (
                "Missing " + " + ".join(missing) + " in this process. "
                "Set both secrets, enable Image search on the Programmable Search Engine, "
                "then restart Studio (Replit: Stop + Run)."
            ),
        }
    try:
        import requests as _req
        r = _req.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": key,
                "cx": cx,
                "q": "nature landscape",
                "searchType": "image",
                "num": 1,
                "safe": "active",
            },
            timeout=15,
        )
        body = {}
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        err = body.get("error") if isinstance(body, dict) else None
        err_msg = ""
        err_status = ""
        err_reason = ""
        if isinstance(err, dict):
            err_msg = str(err.get("message") or "")[:280]
            err_status = str(err.get("status") or "")
            errors = err.get("errors") or []
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                err_reason = str(errors[0].get("reason") or "")
        if r.status_code == 200:
            items = body.get("items") or []
            return {
                "configured": True,
                "ok": True,
                "http_status": 200,
                "key_len": len(key),
                "key_prefix": key[:4] + "…",
                "cx_len": len(cx),
                "cx_prefix": cx[:6] + "…",
                "hits": len(items),
                "message": (
                    "Google CSE accepted the key + cx (Image search OK)."
                    if items
                    else "CSE responded OK but returned 0 images for a test query — "
                         "confirm Image search is ON and the engine can reach the web / GIF sites."
                ),
                "ok_strict": bool(items),
            }
        tip = _cse_error_tip(r.status_code, err if isinstance(err, dict) else None, err_msg)
        return {
            "configured": True,
            "ok": False,
            "http_status": r.status_code,
            "error_status": err_status or None,
            "error_reason": err_reason or None,
            "key_len": len(key),
            "key_prefix": key[:4] + "…",
            "cx_len": len(cx),
            "cx_prefix": cx[:6] + "…",
            "message": (
                f"Google CSE HTTP {r.status_code}"
                + (f" ({err_status})" if err_status else "")
                + (f": {err_msg}" if err_msg else ".")
                + tip
            ).strip(),
        }
    except Exception as e:
        return {
            "configured": True,
            "ok": False,
            "http_status": None,
            "key_len": len(key),
            "cx_len": len(cx),
            "message": f"Could not reach Google CSE: {e}",
        }


def _normalize_broll_prefer_provider(raw) -> str:
    v = str(raw or "auto").lower().strip().replace("-", "_").replace(" ", "_")
    aliases = {
        "auto": "auto",
        "default": "auto",
        "serp": "serpapi",
        "serpapi": "serpapi",
        "serp_api": "serpapi",
        "cse": "google_cse",
        "google": "google_cse",
        "google_cse": "google_cse",
        "googlesearch": "google_cse",
        "pexels": "pexels",
        "unsplash": "unsplash",
    }
    return aliases.get(v, "auto")


def _broll_stock_provider_order(prefer: str = "auto") -> list[str]:
    """Ordered stock photo providers. Prefer moves that source to the front; others remain fallbacks."""
    # SerpAPI first in auto — CSE is often blocked for new Google Cloud projects.
    base = ["serpapi", "pexels", "unsplash", "google_cse"]
    pref = _normalize_broll_prefer_provider(prefer)
    if pref in base:
        return [pref] + [p for p in base if p != pref]
    return list(base)


def _broll_any_photo_provider() -> bool:
    """Stock photo providers only — Gemini AI photos are opt-in separately."""
    st = _broll_provider_status()
    return bool(
        st.get("serpapi") or st["google_cse"] or st["pexels"] or st["unsplash"]
    )


def _broll_gif_provider_ready() -> bool:
    st = _broll_provider_status()
    return bool(st.get("serpapi") or st.get("google_cse"))


def _probe_serpapi() -> dict:
    """Live SerpAPI check — never returns the key value."""
    key = _serpapi_api_key()
    if not key:
        return {
            "configured": False,
            "ok": False,
            "http_status": None,
            "message": "SERPAPI_API_KEY is missing in this process. "
                       "Replit Secrets → name exactly SERPAPI_API_KEY → Stop + Run.",
        }
    try:
        import requests as _req
        r = _req.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google_images",
                "q": "nature landscape",
                "api_key": key,
                "safe": "active",
            },
            timeout=25,
        )
        body = {}
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        if r.status_code == 200:
            hits = body.get("images_results") or []
            return {
                "configured": True,
                "ok": True,
                "http_status": 200,
                "key_len": len(key),
                "key_prefix": key[:4] + "…",
                "hits": len(hits),
                "message": (
                    "SerpAPI accepted the key (Google Images OK)."
                    if hits
                    else "SerpAPI OK but 0 image hits for the test query — try Suggest again."
                ),
                "ok_strict": bool(hits),
            }
        err = str(body.get("error") or "")[:240]
        return {
            "configured": True,
            "ok": False,
            "http_status": r.status_code,
            "key_len": len(key),
            "key_prefix": key[:4] + "…",
            "message": (
                f"SerpAPI HTTP {r.status_code}"
                + (f": {err}" if err else ".")
                + (" Check the key at https://serpapi.com/manage-api-key" if r.status_code in (401, 403) else "")
            ),
        }
    except Exception as e:
        return {
            "configured": True,
            "ok": False,
            "http_status": None,
            "key_len": len(key),
            "message": f"Could not reach SerpAPI: {e}",
        }


def _gemini_image_ready() -> bool:
    return bool((os.environ.get("GEMINI_API_KEY") or "").strip())


_PERSON_QUERY_RE = re.compile(
    r"\b("
    r"person|people|man|woman|men|women|guy|girl|boy|human|face|portrait|"
    r"speaker|host|guest|interviewer|interviewee|talent|couple|crowd|audience|"
    r"friend|family|coworker|colleague|doctor|teacher|athlete|ceo|founder|"
    r"talking|speaking|interview|reaction|selfie|headshot"
    r")\b",
    re.I,
)


def _load_reframe_cache(job_id: str | None) -> dict | None:
    if not job_id:
        return None
    cache_path = UPLOAD_DIR / f"{job_id}_reframe.json"
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _reframe_faces_ready(job_id: str | None) -> bool:
    rd = _load_reframe_cache(job_id)
    if not rd:
        return False
    faces = rd.get("faces") or []
    if not faces:
        return False
    stats = rd.get("stats") or {}
    if stats.get("faces_skipped"):
        return False
    return True


def _query_implies_person(query: str) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    if _PERSON_QUERY_RE.search(q):
        return True
    # Short name-like tokens often mean people in interview B-roll.
    tokens = [t for t in re.split(r"\W+", q) if t]
    if 1 <= len(tokens) <= 3 and all(t[:1].isupper() for t in tokens if t.isalpha()):
        return True
    return False


def _active_speaker_for_time(
    reframe_data: dict | None,
    t: float,
    words: list | None = None,
) -> str | None:
    if words:
        best = None
        best_d = 1e9
        for w in words:
            try:
                ws = float(w.get("start", 0))
                we = float(w.get("end", ws))
            except (TypeError, ValueError):
                continue
            if ws - 0.15 <= t <= we + 0.15 and w.get("speaker"):
                d = abs(((ws + we) / 2.0) - t)
                if d < best_d:
                    best_d = d
                    best = str(w.get("speaker"))
        if best:
            return best
    if not reframe_data:
        return None
    return _active_speaker_at(reframe_data.get("diarization") or [], t)


def _extract_frame_crop_jpg(
    video_path: Path,
    t: float,
    bbox: tuple[float, float, float, float],
    dest: Path,
    *,
    pad: float = 1.65,
    max_side: int = 960,
) -> Path | None:
    """Grab a frame at *t* and crop around normalized face bbox → JPEG."""
    try:
        import cv2
    except ImportError:
        return None
    try:
        cap = cv2.VideoCapture(str(video_path))
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0, int(float(t) * 1000)))
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return None
        fh, fw = frame.shape[:2]
        cx, cy, w, h = bbox
        # Prefer upper-body / headroom: expand downward slightly.
        x1 = max(0, min(fw - 1, int((cx - w * pad) * fw)))
        x2 = max(x1 + 1, min(fw, int((cx + w * pad) * fw)))
        y1 = max(0, min(fh - 1, int((cy - h * pad * 0.95) * fh)))
        y2 = max(y1 + 1, min(fh, int((cy + h * pad * 1.35) * fh)))
        cropped = frame[y1:y2, x1:x2]
        if cropped.size == 0:
            return None
        ch, cw = cropped.shape[:2]
        scale = min(1.0, float(max_side) / float(max(ch, cw, 1)))
        if scale < 0.999:
            cropped = cv2.resize(
                cropped,
                (max(1, int(cw * scale)), max(1, int(ch * scale))),
                interpolation=cv2.INTER_AREA,
            )
        dest = dest.with_suffix(".jpg")
        ok, buf = cv2.imencode(".jpg", cropped, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            return None
        dest.write_bytes(buf.tobytes())
        if dest.exists() and dest.stat().st_size > 800:
            return dest
        _safe_unlink(dest)
        return None
    except Exception as e:
        ai_logger.warning(f"Speaker still extract failed: {e}")
        return None


def _speaker_still_for_time(
    job_id: str | None,
    t: float,
    dest_stem: Path,
    *,
    speaker: str | None = None,
    words: list | None = None,
) -> Path | None:
    """Screenshot / face-crop of who is talking near *t* (needs Analyze speakers)."""
    if not job_id:
        return None
    rd = _load_reframe_cache(job_id)
    if not rd:
        return None
    faces = rd.get("faces") or []
    if not faces:
        return None
    video_path = find_video_path(job_id)
    if not video_path:
        return None
    diar = rd.get("diarization") or []
    spk = speaker or _active_speaker_for_time(rd, t, words=words)
    positions, bboxes = _assign_speakers_to_faces(diar, faces)
    bbox = None
    if spk and spk in bboxes:
        bbox = bboxes[spk]
    else:
        found = _face_samples_at(faces, t)
        if found:
            face = max(found, key=lambda f: float(f.get("w", 0)) * float(f.get("h", 0)))
            try:
                bbox = (
                    float(face["cx"]), float(face["cy"]),
                    float(face.get("w", 0.2)), float(face.get("h", 0.25)),
                )
            except (KeyError, TypeError, ValueError):
                bbox = None
        if bbox is None and positions:
            # Fall back to any known speaker position.
            spk0 = next(iter(bboxes), None) or next(iter(positions), None)
            if spk0 and spk0 in bboxes:
                bbox = bboxes[spk0]
            elif spk0 and spk0 in positions:
                cx, cy = positions[spk0]
                bbox = (cx, cy, 0.22, 0.28)
    if not bbox:
        return None
    return _extract_frame_crop_jpg(video_path, t, bbox, dest_stem)


def _describe_face_appearance(face_jpg: Path) -> str | None:
    """Short appearance line from a face crop via Gemini vision (for stock/AI bias)."""
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key or not face_jpg or not face_jpg.exists():
        return None
    try:
        blob = face_jpg.read_bytes()
        if len(blob) < 400:
            return None
        b64 = base64.b64encode(blob).decode("ascii")
        model = (os.environ.get("GEMINI_MODEL") or GEMINI_MODEL or "gemini-2.0-flash").strip()
        # Prefer a cheap text model for description.
        for cand in (model, "gemini-2.0-flash", "gemini-2.5-flash"):
            if not cand:
                continue
            url = f"{_GEMINI_BASE_URL}/{cand}:generateContent?key={api_key}"
            body = {
                "contents": [{
                    "role": "user",
                    "parts": [
                        {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                        {"text": (
                            "Describe this person's appearance for stock-photo search in one short phrase "
                            "(age range, gender presentation, hair, skin tone, clothing style). "
                            "No name. No speculation about identity. Max 16 words."
                        )},
                    ],
                }],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 60},
            }
            import requests as _req
            resp = _req.post(url, json=body, timeout=40)
            if resp.status_code >= 400:
                continue
            data = resp.json() or {}
            parts = (
                ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")
                or []
            )
            text = " ".join(
                str(p.get("text") or "").strip()
                for p in parts if isinstance(p, dict)
            ).strip()
            text = re.sub(r"\s+", " ", text).strip(" .")
            if 8 <= len(text) <= 160:
                return text
        return None
    except Exception as e:
        ai_logger.warning(f"Speaker appearance describe failed: {e}")
        return None


def _ensure_speaker_looks(job_id: str | None) -> dict:
    """Cache SPEAKER_id → appearance phrase on reframe JSON."""
    if not job_id:
        return {}
    rd = _load_reframe_cache(job_id)
    if not rd:
        return {}
    cached = rd.get("speaker_looks")
    if isinstance(cached, dict) and cached:
        return {str(k): str(v) for k, v in cached.items() if v}
    if not _gemini_image_ready():
        return {}
    diar = rd.get("diarization") or []
    faces = rd.get("faces") or []
    if not diar or not faces:
        return {}
    video_path = find_video_path(job_id)
    if not video_path:
        return {}
    _, bboxes = _assign_speakers_to_faces(diar, faces)
    looks: dict[str, str] = {}
    tmp_dir = UPLOAD_DIR / f"{job_id}_looks_tmp"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for spk, bbox in list(bboxes.items())[:4]:
            segs = [s for s in diar if s.get("speaker") == spk]
            if not segs:
                continue
            sample_t = (float(segs[0]["start"]) + float(segs[0]["end"])) / 2.0
            crop = _extract_frame_crop_jpg(
                video_path, sample_t, bbox, tmp_dir / f"{spk}.jpg", pad=1.25, max_side=512,
            )
            if not crop:
                continue
            desc = _describe_face_appearance(crop)
            if desc:
                looks[spk] = desc
    finally:
        try:
            for p in tmp_dir.glob("*"):
                _safe_unlink(p)
            tmp_dir.rmdir()
        except OSError:
            pass
    if looks:
        rd["speaker_looks"] = looks
        try:
            (UPLOAD_DIR / f"{job_id}_reframe.json").write_text(
                json.dumps(rd, ensure_ascii=False), encoding="utf-8",
            )
        except OSError:
            pass
    return looks


def _appearance_hint_for_time(
    job_id: str | None,
    t: float,
    words: list | None = None,
) -> str:
    looks = _ensure_speaker_looks(job_id)
    if not looks:
        return ""
    rd = _load_reframe_cache(job_id)
    spk = _active_speaker_for_time(rd, t, words=words)
    if spk and looks.get(spk):
        return looks[spk]
    # Blend all known looks (multi-person interview).
    return " / ".join(looks.values())


def _speaker_reference_face(
    job_id: str | None,
    t: float,
    dest_stem: Path,
    words: list | None = None,
) -> Path | None:
    """Small face crop used as Gemini likeness reference."""
    return _speaker_still_for_time(
        job_id, t, dest_stem, words=words,
    )


def _with_appearance_bias(query: str, appearance_hint: str) -> str:
    q = (query or "").strip()
    hint = (appearance_hint or "").strip()
    if not q or not hint:
        return q
    if not _query_implies_person(q):
        # Soft: still nudge people-ish compositions when we have on-screen talent.
        return f"{q}, person who looks like: {hint}"
    return f"{q}, person resembling: {hint}"


def _generate_broll_gemini_image(
    query: str,
    dest_stem: Path,
    *,
    appearance_hint: str = "",
    reference_face: Path | None = None,
) -> Path | None:
    """Generate a B-roll still via Gemini image model. Returns saved Path or None."""
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return None
    q = (query or "").strip()
    if not q:
        return None
    hint = (appearance_hint or "").strip()
    likeness = ""
    if hint:
        likeness = (
            f" If people appear, they should resemble: {hint}. "
            "Match age range, hair, skin tone, and clothing vibe — do not copy a real identity."
        )
    prompt = (
        "Generate a single realistic photo suitable as video B-roll (no text, "
        "no logos, no watermarks, no UI chrome). Landscape-friendly composition. "
        f"Subject: {q}.{likeness}"
    )
    if reference_face and reference_face.exists():
        prompt += (
            " Use the attached reference face only for likeness of any people in the new photo. "
            "Create a NEW scene (not a copy of the reference frame)."
        )
    model = (GEMINI_IMAGE_MODEL or "gemini-2.5-flash-image").strip()
    url = f"{_GEMINI_BASE_URL}/{model}:generateContent?key={api_key}"
    parts: list[dict] = []
    if reference_face and reference_face.exists():
        try:
            raw = reference_face.read_bytes()
            if len(raw) > 400:
                mime = "image/jpeg"
                suf = reference_face.suffix.lower()
                if suf == ".png":
                    mime = "image/png"
                elif suf == ".webp":
                    mime = "image/webp"
                parts.append({
                    "inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(raw).decode("ascii"),
                    }
                })
        except OSError:
            pass
    parts.append({"text": prompt})
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
        },
    }
    try:
        import requests as _req
        resp = _req.post(url, json=body, timeout=90)
        if resp.status_code >= 400:
            ai_logger.warning(
                f"Gemini image B-roll HTTP {resp.status_code}: {resp.text[:300]}"
            )
            return None
        data = resp.json() or {}
        parts_out = (
            ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")
            or []
        )
        for part in parts_out:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data") or {}
            raw_b64 = inline.get("data")
            if not raw_b64:
                continue
            mime = (inline.get("mimeType") or inline.get("mime_type") or "image/png").lower()
            ext = "png"
            if "jpeg" in mime or "jpg" in mime:
                ext = "jpg"
            elif "webp" in mime:
                ext = "webp"
            elif "gif" in mime:
                ext = "gif"
            try:
                blob = base64.b64decode(raw_b64)
            except Exception as e:
                ai_logger.warning(f"Gemini image base64 decode failed: {e}")
                continue
            if len(blob) < 800:
                continue
            dest = dest_stem.with_suffix(f".{ext}")
            dest.write_bytes(blob)
            if dest.exists() and dest.stat().st_size > 800:
                return dest
            _safe_unlink(dest)
        return None
    except Exception as e:
        ai_logger.warning(f"Gemini image B-roll failed: {e}")
        return None


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


def _search_broll_google_cse(query: str, file_type: str | None = None) -> str | None:
    key, cx = _google_cse_creds()
    if not key or not cx:
        return None
    try:
        import requests as _req
        params = {
            "key": key, "cx": cx, "q": query,
            "searchType": "image", "num": 1, "safe": "active",
        }
        ft = (file_type or "").lower().strip().lstrip(".")
        if ft in ("gif", "png", "jpg", "jpeg", "webp", "bmp"):
            params["fileType"] = "gif" if ft == "gif" else ft
        r = _req.get(
            "https://www.googleapis.com/customsearch/v1",
            params=params,
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


def _search_broll_serpapi(query: str, prefer_gif: bool = False) -> str | None:
    """Google Images via SerpAPI — CSE replacement for photos + GIFs."""
    key = _serpapi_api_key()
    if not key:
        return None
    q = (query or "").strip()
    if not q:
        return None
    if prefer_gif:
        # Bias toward animated GIFs / meme hosts without requiring CSE.
        if "gif" not in q.lower():
            q = f"{q} gif"
    try:
        import requests as _req
        r = _req.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google_images",
                "q": q,
                "api_key": key,
                "safe": "active",
            },
            timeout=25,
        )
        if r.status_code >= 400:
            err = ""
            try:
                err = str((r.json() or {}).get("error") or "")[:160]
            except Exception:
                pass
            ai_logger.warning(f"SerpAPI B-roll HTTP {r.status_code}: {err}")
            return None
        items = (r.json() or {}).get("images_results") or []
        if not items:
            return None
        # Prefer a real .gif when requested; otherwise first original URL.
        if prefer_gif:
            for it in items:
                if not isinstance(it, dict):
                    continue
                for field in ("original", "link", "thumbnail"):
                    url = it.get(field)
                    if url and ".gif" in str(url).lower().split("?")[0]:
                        return str(url)
        for it in items:
            if not isinstance(it, dict):
                continue
            url = it.get("original") or it.get("link") or it.get("thumbnail")
            if url:
                return str(url)
        return None
    except Exception as e:
        ai_logger.warning(f"SerpAPI B-roll search failed: {e}")
        return None


def _search_broll_stock_url(
    query: str,
    provider: str,
    *,
    prefer_gif: bool = False,
) -> str | None:
    """Single-provider stock URL lookup."""
    p = (provider or "").lower().strip()
    if p == "serpapi":
        return _search_broll_serpapi(query, prefer_gif=prefer_gif)
    if p == "google_cse":
        if prefer_gif:
            url = _search_broll_google_cse(query, file_type="gif")
            if url:
                return url
            url = _search_broll_google_cse(query)
            if url and ".gif" not in url.lower().split("?")[0]:
                return None
            return url
        return _search_broll_google_cse(query)
    if p == "pexels":
        return None if prefer_gif else _search_broll_pexels(query)
    if p == "unsplash":
        return None if prefer_gif else _search_broll_unsplash(query)
    return None


def _search_broll_pexels(query: str) -> str | None:
    key = _pexels_api_key()
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
            ai_logger.warning(f"Pexels B-roll search HTTP {r.status_code}")
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


def _fetch_broll_image_for_keyword(
    query: str,
    dest_stem: Path,
    prefer_gif: bool = False,
    use_ai: bool = False,
    prefer_provider: str = "auto",
    appearance_hint: str = "",
    reference_face: Path | None = None,
) -> Path | None:
    """Search providers and download the first hit. Prefer Gemini when use_ai."""
    path, _src = _fetch_broll_image_for_keyword_ex(
        query, dest_stem,
        prefer_gif=prefer_gif, use_ai=use_ai, prefer_provider=prefer_provider,
        appearance_hint=appearance_hint, reference_face=reference_face,
    )
    return path


def _fetch_broll_image_for_keyword_ex(
    query: str,
    dest_stem: Path,
    prefer_gif: bool = False,
    use_ai: bool = False,
    prefer_provider: str = "auto",
    appearance_hint: str = "",
    reference_face: Path | None = None,
) -> tuple:
    """Returns (Path|None, source) where source is
    'gemini'|'gif'|'photo'|None.
    """
    path, src, _prov = _fetch_broll_image_for_keyword_detail(
        query, dest_stem,
        prefer_gif=prefer_gif, use_ai=use_ai, prefer_provider=prefer_provider,
        appearance_hint=appearance_hint, reference_face=reference_face,
    )
    return path, src


def _fetch_broll_image_for_keyword_detail(
    query: str,
    dest_stem: Path,
    prefer_gif: bool = False,
    use_ai: bool = False,
    prefer_provider: str = "auto",
    appearance_hint: str = "",
    reference_face: Path | None = None,
) -> tuple:
    """Returns (Path|None, source, provider) with provider in
    gemini|google_cse|pexels|unsplash|None and source gemini|gif|photo|None.
    """
    q = (query or "").strip()
    if not q:
        return None, None, None

    hint = (appearance_hint or "").strip()
    stock_q = _with_appearance_bias(q, hint) if hint else q

    if use_ai and not prefer_gif and _gemini_image_ready():
        ai_path = _generate_broll_gemini_image(
            q, dest_stem,
            appearance_hint=hint,
            reference_face=reference_face,
        )
        if ai_path:
            return ai_path, "gemini", "gemini"

    order = _broll_stock_provider_order(prefer_provider)
    if prefer_gif:
        # GIF mode: SerpAPI (preferred) or legacy Google CSE.
        order = [p for p in _broll_stock_provider_order(prefer_provider)
                 if p in ("serpapi", "google_cse")]
        if not order:
            order = ["serpapi", "google_cse"]

    for prov in order:
        url = _search_broll_stock_url(stock_q, prov, prefer_gif=prefer_gif)
        if not url and stock_q != q:
            url = _search_broll_stock_url(q, prov, prefer_gif=prefer_gif)
        if not url:
            continue
        path = _download_url_to_asset(url, dest_stem)
        if not path:
            continue
        if prefer_gif or _is_gif_path(path):
            return path, "gif", prov
        return path, "photo", prov
    return None, None, None


def _overlay_layout_for_index(i: int, placement: str = "pip") -> dict:
    return _caption_aware_overlay_layout(i, placement)


def _caption_aware_overlay_layout(
    i: int,
    placement: str = "pip",
    *,
    face_cx: float | None = None,
    face_cy: float | None = None,
    caption_y_pct: float = 82.0,
) -> dict:
    """Place PiP / center plates in free real estate (away from face + captions).

    Captions usually sit in the lower third (position_y ~75–85). Prefer top
    corners / top plate so overlays don't cover karaoke or the talking head.
    """
    placement = (placement or "pip").lower()
    try:
        cap_y = float(caption_y_pct)
    except (TypeError, ValueError):
        cap_y = 82.0
    # Fraction of frame height where the caption band begins (with padding).
    caption_band_top = max(0.48, min(0.88, (cap_y / 100.0) - 0.16))

    if placement in ("center", "centre", "full"):
        # If the face is high, drop the plate mid-frame but still above captions.
        if face_cy is not None and face_cy < 0.32:
            y = min(0.40, caption_band_top - 0.30)
            return {
                "x": 0.10, "y": max(0.34, y), "w": 0.80, "h": 0.28,
                "fit": "contain", "layout": "center",
            }
        # Default: top half plate — never into the caption band.
        h = min(0.36, max(0.22, caption_band_top - 0.08))
        return {
            "x": 0.10, "y": 0.05, "w": 0.80, "h": h,
            "fit": "contain", "layout": "center",
        }

    candidates = [
        {"x": 0.58, "y": 0.04, "w": 0.36, "h": 0.22, "fit": "cover", "layout": "pip_tr", "score": 0.0},
        {"x": 0.04, "y": 0.04, "w": 0.36, "h": 0.22, "fit": "cover", "layout": "pip_tl", "score": 0.0},
        {"x": 0.58, "y": 0.30, "w": 0.36, "h": 0.22, "fit": "cover", "layout": "pip_mr", "score": 0.0},
        {"x": 0.04, "y": 0.30, "w": 0.36, "h": 0.22, "fit": "cover", "layout": "pip_ml", "score": 0.0},
    ]
    for c in candidates:
        bottom = c["y"] + c["h"]
        if bottom > caption_band_top:
            c["score"] -= 6.0
        cx = c["x"] + c["w"] / 2.0
        cy = c["y"] + c["h"] / 2.0
        if face_cx is not None and face_cy is not None:
            if abs(cx - float(face_cx)) < 0.28 and abs(cy - float(face_cy)) < 0.28:
                c["score"] -= 10.0
            # Prefer the side opposite the face.
            if (float(face_cx) < 0.5 and c["x"] >= 0.5) or (float(face_cx) >= 0.5 and c["x"] < 0.5):
                c["score"] += 2.5
            if float(face_cy) > 0.45 and c["y"] < 0.2:
                c["score"] += 1.5
        # Prefer top slots generally (Captions-style).
        if c["y"] < 0.15:
            c["score"] += 1.0
    candidates.sort(key=lambda c: (-c["score"], c["y"], c["x"]))
    top_score = candidates[0]["score"]
    best = [c for c in candidates if c["score"] >= top_score - 0.5] or candidates
    pick = best[int(i) % len(best)]
    return {k: pick[k] for k in ("x", "y", "w", "h", "fit", "layout")}


def _face_hint_for_job(job_id: str | None) -> tuple[float | None, float | None]:
    """Best-effort face center from reframe cache (normalized 0–1)."""
    if not job_id:
        return None, None
    rd = _load_reframe_cache(job_id)
    if not rd:
        return None, None
    faces = rd.get("faces") or []
    # Samples are {t, faces:[{cx,cy,...}]} — pick the largest face from the
    # earliest non-empty sample (good enough for overlay placement).
    if isinstance(faces, list):
        for sample in faces:
            if not isinstance(sample, dict):
                continue
            fl = sample.get("faces") if "faces" in sample else None
            if isinstance(fl, list) and fl:
                face = max(fl, key=lambda f: float(f.get("w", 0)) * float(f.get("h", 0)))
                try:
                    return float(face["cx"]), float(face.get("cy", 0.4))
                except (KeyError, TypeError, ValueError):
                    pass
            # Legacy flat face dict
            if "cx" in sample or "x" in sample:
                try:
                    return float(sample.get("cx", sample.get("x", 0.5))), float(
                        sample.get("cy", sample.get("y", 0.4))
                    )
                except (TypeError, ValueError):
                    pass
    for key in ("face_cx", "cx", "center_x"):
        if key in rd:
            try:
                cx = float(rd.get(key))
                cy = float(rd.get("face_cy") or rd.get("cy") or rd.get("center_y") or 0.4)
                return cx, cy
            except (TypeError, ValueError):
                break
    return None, None


def _make_keyword_badge_png(text: str, dest: Path) -> bool:
    """Deprecated — keyword text badges are disabled (dated look / caption clash).

    Kept as a no-op so old call sites fail soft instead of drawing purple boxes.
    """
    return False


@app.route('/fetch-auto-overlays', methods=['POST'])
def fetch_auto_overlays():
    """Suggest timed B-roll overlays from transcript keywords.

    Body: {
      words?: [...], job_id?: str, budget?: int,
      mode?: "auto"|"photo"|"badge"|"gif",
      placement?: "pip"|"center",
      start?: float, end?: float,   # optional source-time window (long-form)
      use_ai_photos?: bool,         # opt-in Gemini stills before stock
      prefer_provider?: "auto"|"serpapi"|"google_cse"|"pexels"|"unsplash",
      prefer_speaker_stills?: bool,  # default true — screenshot who is talking
      appearance_bias?: bool,       # default true — likeness-match stock/AI people
      semantic?: bool
    }
    """
    data = request.get_json(force=True) or {}
    words = data.get("words") or []
    job_id = data.get("job_id")
    if (not words) and job_id and job_id in jobs:
        words = jobs[job_id].get("words") or []
    try:
        budget = max(1, min(12, int(data.get("budget") or 5)))
    except (TypeError, ValueError):
        budget = 5
    mode = str(data.get("mode") or "auto").lower().strip()
    placement = str(data.get("placement") or "pip").lower().strip()
    use_ai_photos = bool(data.get("use_ai_photos")) and _gemini_image_ready()
    prefer_provider = _normalize_broll_prefer_provider(
        data.get("prefer_provider") or data.get("provider") or "auto"
    )
    jid = job_id if isinstance(job_id, str) else None
    speaker_ready = _reframe_faces_ready(jid)
    prefer_speaker_stills = data.get("prefer_speaker_stills", True) is not False
    appearance_bias = data.get("appearance_bias", True) is not False
    # Keyword text badges are retired — map legacy "badge" → photo when possible.
    if mode == "badge":
        mode = "photo" if (
            _broll_any_photo_provider() or use_ai_photos or speaker_ready
        ) else "auto"
    if mode not in ("auto", "photo", "gif"):
        mode = "auto"

    face_cx, face_cy = _face_hint_for_job(jid)
    caption_y = 82.0
    if jid and jid in jobs:
        st = (jobs[jid].get("style") or {})
        try:
            caption_y = float(st.get("position_y") or caption_y)
        except (TypeError, ValueError):
            pass

    try:
        win_start = float(data["start"]) if data.get("start") is not None else 0.0
    except (TypeError, ValueError):
        win_start = 0.0
    try:
        win_end = float(data["end"]) if data.get("end") is not None else 1e9
    except (TypeError, ValueError):
        win_end = 1e9
    if win_end <= win_start:
        win_start, win_end = 0.0, 1e9
    win_start = max(0.0, win_start)
    win_end = max(win_start + 0.5, win_end)

    semantic = data.get("semantic", True) is not False
    callouts = _broll_callouts_for_window(
        words, win_start, win_end, budget, semantic=semantic,
    )
    # Optional: replace/refetch specific keywords (approval UI "Replace").
    raw_kw = data.get("keywords")
    if isinstance(raw_kw, list) and raw_kw:
        forced = []
        for item in raw_kw[:budget]:
            if isinstance(item, str) and item.strip():
                forced.append({"text": item.strip(), "start": 0.0, "duration": 1.8})
            elif isinstance(item, dict) and str(item.get("text") or "").strip():
                try:
                    start = float(item.get("start") or 0)
                except (TypeError, ValueError):
                    start = 0.0
                try:
                    dur = float(item.get("duration") or 1.8)
                except (TypeError, ValueError):
                    dur = 1.8
                forced.append({
                    "text": str(item.get("text")).strip(),
                    "start": max(0.0, start),
                    "duration": max(1.0, dur),
                })
        if forced:
            callouts = forced

    overlays = []
    providers = _broll_provider_status()
    used_photo = 0
    used_badge = 0
    used_gif = 0
    used_gemini = 0
    used_speaker = 0
    used_by_provider: dict[str, int] = {}
    ASSET_DIR.mkdir(parents=True, exist_ok=True)

    # Warm appearance cache once per Suggest (Gemini vision) when biasing stock/AI.
    if appearance_bias and speaker_ready and jid:
        try:
            _ensure_speaker_looks(jid)
        except Exception as e:
            ai_logger.warning(f"speaker looks warm failed: {e}")

    for co in callouts:
        label = str(co.get("text") or "B-roll").strip() or "B-roll"
        start = float(co.get("start") or 0)
        dur = float(co.get("duration") or 1.8)
        asset_id = uuid.uuid4().hex
        asset_path = None
        source = "badge"
        stock_provider = None

        want_gif = mode == "gif" and _broll_gif_provider_ready()
        want_photo = mode in ("photo", "auto") and (
            _broll_any_photo_provider() or use_ai_photos
            or (prefer_speaker_stills and speaker_ready)
        )
        if want_gif:
            hint = _appearance_hint_for_time(jid, start, words=words) if appearance_bias else ""
            asset_path, src_tag, stock_provider = _fetch_broll_image_for_keyword_detail(
                label, ASSET_DIR / asset_id, prefer_gif=True,
                prefer_provider=prefer_provider,
                appearance_hint=hint,
            )
            if asset_path:
                source = "gif"
                used_gif += 1
                used_by_provider[stock_provider or "serpapi"] = (
                    used_by_provider.get(stock_provider or "serpapi", 0) + 1
                )
                final = ASSET_DIR / f"{asset_id}{asset_path.suffix.lower()}"
                if asset_path.resolve() != final.resolve():
                    try:
                        if final.exists():
                            _safe_unlink(final)
                        asset_path.replace(final)
                        asset_path = final
                    except OSError:
                        pass
        elif want_photo:
            # 1) Prefer a screenshot of who is talking (Analyze speakers + faces).
            if prefer_speaker_stills and speaker_ready and jid:
                still = _speaker_still_for_time(
                    jid, start, ASSET_DIR / asset_id, words=words,
                )
                if still:
                    asset_path = still
                    source = "speaker_still"
                    stock_provider = "speaker_still"
                    used_speaker += 1
                    used_by_provider["speaker_still"] = (
                        used_by_provider.get("speaker_still", 0) + 1
                    )
            # 2) Else stock / AI — bias people toward on-screen likeness.
            if asset_path is None:
                hint = ""
                ref_face = None
                if appearance_bias and speaker_ready and jid:
                    hint = _appearance_hint_for_time(jid, start, words=words)
                    if use_ai_photos or _query_implies_person(label):
                        ref_face = _speaker_still_for_time(
                            jid, start,
                            ASSET_DIR / f"{asset_id}_ref",
                            words=words,
                        )
                asset_path, src_tag, stock_provider = _fetch_broll_image_for_keyword_detail(
                    label, ASSET_DIR / asset_id,
                    use_ai=use_ai_photos,
                    prefer_provider=prefer_provider,
                    appearance_hint=hint,
                    reference_face=ref_face,
                )
                if ref_face is not None:
                    try:
                        _safe_unlink(ref_face)
                    except OSError:
                        pass
                if asset_path:
                    if src_tag == "gemini":
                        source = "gemini"
                        used_gemini += 1
                        used_by_provider["gemini"] = used_by_provider.get("gemini", 0) + 1
                    else:
                        is_gif = _is_gif_path(asset_path)
                        source = "gif" if is_gif else "photo"
                        if is_gif:
                            used_gif += 1
                        else:
                            used_photo += 1
                        if stock_provider:
                            used_by_provider[stock_provider] = (
                                used_by_provider.get(stock_provider, 0) + 1
                            )
            if asset_path is not None:
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
            # No more purple keyword badges — skip until a real photo/GIF is found.
            continue

        stem = asset_path.stem
        if stem != asset_id:
            asset_id = stem

        display_name = f"{label}{asset_path.suffix.lower()}"
        _write_asset_meta(
            asset_id,
            filename=display_name,
            keyword=label,
            source=source,
            provider=stock_provider,
        )

        pos = _caption_aware_overlay_layout(
            len(overlays), placement,
            face_cx=face_cx, face_cy=face_cy, caption_y_pct=caption_y,
        )
        overlays.append({
            "asset_id": asset_id,
            "keyword": label,
            "source": source,
            "provider": stock_provider,
            "in": 0,
            "out": max(1.2, dur),
            "start": max(0.0, start),
            "x": pos["x"],
            "y": pos["y"],
            "w": pos["w"],
            "h": pos["h"],
            "opacity": 1.0,
            "fit": pos["fit"],
            "fade_in": 0.15,
            "fade_out": 0.25,
            "border_px": 2 if source in ("photo", "gif", "gemini", "speaker_still") else 0,
            "layout": pos["layout"],
            # Ken Burns is opt-in in the Timeline inspector / Effects lane.
            "ken_burns": None,
            "quote": co.get("quote") or "",
            "reason": co.get("reason") or "",
            "story_role": co.get("story_role"),
            "google_tab": co.get("google_tab"),
            "worthiness_score": co.get("worthiness_score"),
            "worthiness": co.get("worthiness"),
        })

    return jsonify({
        "ok": True,
        "overlays": overlays,
        "count": len(overlays),
        "mode": mode,
        "placement": placement,
        "prefer_provider": prefer_provider,
        "prefer_speaker_stills": prefer_speaker_stills,
        "appearance_bias": appearance_bias,
        "speaker_faces_ready": speaker_ready,
        "use_ai_photos": use_ai_photos,
        "semantic": semantic,
        "window": {"start": win_start, "end": win_end if win_end < 1e8 else None},
        "providers": providers,
        "photo_ready": _broll_any_photo_provider() or speaker_ready,
        "gemini_image_ready": _gemini_image_ready(),
        "worthiness": {
            "threshold": int(os.environ.get("OVERLAY_WORTHINESS_THRESHOLD", "55") or 55),
            "min_gap_s": 18.0,
            "candidates": [
                {
                    "text": c.get("text"),
                    "start": c.get("start"),
                    "score": c.get("worthiness_score"),
                    "google_tab": c.get("google_tab"),
                    "story_role": c.get("story_role"),
                    "worthiness": c.get("worthiness"),
                }
                for c in callouts
            ],
        },
        "stats": {
            "photo": used_photo,
            "badge": used_badge,
            "gif": used_gif,
            "gemini": used_gemini,
            "speaker_still": used_speaker,
            "by_provider": used_by_provider,
        },
        "hint": (
            None if _broll_any_photo_provider() or use_ai_photos or speaker_ready
            else "No photo API key and no speaker faces yet. "
                 "Analyze speakers (faces) for talker screenshots, or set "
                 "SERPAPI_API_KEY / PEXELS_API_KEY / GEMINI_API_KEY."
        ),
    })


@app.route("/overlay/worthiness-preview", methods=["POST"])
def overlay_worthiness_preview():
    """Checkpoint A: score overlay candidates without fetching images.

    Body: { job_id?, words?, start?, end?, budget?, semantic?, threshold? }
    Returns ranked worthy candidates + rejected list with reasons.
    """
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    words = data.get("words")
    if (not words) and job_id and job_id in jobs:
        words = jobs[job_id].get("words") or []
    if not words:
        return jsonify({"error": "Need words or a transcribed job_id"}), 400
    try:
        budget = max(1, min(12, int(data.get("budget") or 4)))
    except (TypeError, ValueError):
        budget = 4
    try:
        win_start = float(data["start"]) if data.get("start") is not None else 0.0
    except (TypeError, ValueError):
        win_start = 0.0
    try:
        win_end = float(data["end"]) if data.get("end") is not None else None
    except (TypeError, ValueError):
        win_end = None
    if win_end is None:
        try:
            win_end = float((words[-1] or {}).get("end") or 90)
        except (TypeError, ValueError, IndexError):
            win_end = 90.0
    semantic = data.get("semantic", True) is not False
    try:
        threshold = int(data["threshold"]) if data.get("threshold") is not None else None
    except (TypeError, ValueError):
        threshold = None

    raw_budget = min(12, budget * 2)
    if semantic:
        raw = _semantic_broll_callouts(words, win_start, win_end, raw_budget)
    else:
        raw = _keyword_callouts_for_window(words, win_start, win_end, raw_budget)

    # Score everyone for the rejected list too
    all_scored = []
    for c in raw:
        meta = _score_overlay_worthiness(
            c, window_start=win_start, window_end=win_end,
            story_role=c.get("story_role"),
        )
        row = dict(c)
        row["worthiness"] = meta
        row["worthiness_score"] = meta["score"]
        row["google_tab"] = meta["google_tab"]
        all_scored.append(row)

    kept = _rank_overlay_candidates(
        raw,
        window_start=win_start,
        window_end=win_end,
        budget=budget,
        threshold=threshold,
    )
    kept_keys = {
        (round(float(k.get("start") or 0), 2), str(k.get("text") or "").lower())
        for k in kept
    }
    rejected = [
        r for r in all_scored
        if (round(float(r.get("start") or 0), 2), str(r.get("text") or "").lower())
        not in kept_keys
    ]
    thr = threshold
    if thr is None:
        try:
            thr = int(os.environ.get("OVERLAY_WORTHINESS_THRESHOLD", "55") or 55)
        except (TypeError, ValueError):
            thr = 55

    return jsonify({
        "ok": True,
        "threshold": thr,
        "min_gap_s": 18.0,
        "window": {"start": win_start, "end": win_end},
        "candidates": kept,
        "rejected": rejected,
        "candidate_count": len(kept),
        "rejected_count": len(rejected),
        "formula": "concrete/proof/place + story_role − abstract − spacing; threshold default 55",
    })


@app.route("/overlay/serp-capture", methods=["POST"])
def overlay_serp_capture():
    """Playwright SERP screenshot → crop → Timeline asset (Checkpoint B input).

    Body: { query: str, tab?: images|web|maps|flights|ai_overview, engine?: auto|google|bing }
    Returns: { ok, asset_id, path, engine, tab, query, clip }

    Google often returns a sorry/CAPTCHA page from datacenter IPs — engine=auto
    falls back to Bing Images/Web which still yields real SERP crops.
    Set SERP_CAPTURE=0 to disable.
    """
    if os.environ.get("SERP_CAPTURE", "1").strip() in ("0", "false", "False", "no"):
        return jsonify({"error": "SERP capture disabled (SERP_CAPTURE=0)"}), 403
    data = request.get_json(force=True) or {}
    query = str(data.get("query") or data.get("text") or "").strip()
    if len(query) < 3:
        return jsonify({"error": "query required"}), 400
    tab = str(data.get("tab") or data.get("google_tab") or "images").strip().lower()
    engine = str(data.get("engine") or os.environ.get("SERP_ENGINE") or "auto").strip().lower()

    try:
        from scripts.serp_screenshot import capture_serp
    except Exception as exc:
        return jsonify({
            "error": f"Playwright SERP worker unavailable: {exc}",
            "hint": "pip install playwright && python -m playwright install chromium",
        }), 501

    asset_id = uuid.uuid4().hex
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    dest = ASSET_DIR / f"{asset_id}.png"
    try:
        meta = capture_serp(query, tab=tab, out_path=dest, engine=engine)
    except Exception as exc:
        from scripts.serp_screenshot import _short_pw_error
        return jsonify({
            "error": f"capture failed: {_short_pw_error(exc)}",
            "hint": (
                "Replit: in Shell run `python -m playwright install chromium`, then Stop + Run. "
                "Chromium needs --no-sandbox on Replit (now default in the SERP worker)."
            ),
        }), 500

    if not meta.get("ok") or not dest.exists() or dest.stat().st_size < 800:
        return jsonify({
            "ok": False,
            "error": meta.get("error") or "empty_capture",
            "hint": meta.get("hint"),
            "query": query,
            "tab": tab,
            "engine": meta.get("engine"),
        }), 502

    _write_asset_meta(
        asset_id,
        filename=f"serp-{tab}-{query[:40].replace(' ', '-')}.png",
        keyword=query[:80],
        source="serp_screenshot",
        provider=meta.get("engine") or "serp",
    )
    return jsonify({
        "ok": True,
        "asset_id": asset_id,
        "query": query,
        "tab": tab,
        "engine": meta.get("engine"),
        "url": meta.get("url"),
        "clip": meta.get("clip"),
        "bytes": meta.get("bytes"),
        "clarity": meta.get("clarity"),
        "device_scale_factor": meta.get("device_scale_factor"),
        "overlay": {
            "asset_id": asset_id,
            "keyword": query[:80],
            "source": "serp_screenshot",
            "provider": meta.get("engine"),
            "in": 0,
            "out": 2.2,
            "start": 0,
            "google_tab": tab,
        },
    })


@app.route("/overlay/serp-demo-nj", methods=["POST", "GET"])
def overlay_serp_demo_nj():
    """Capture the 3 NJ reference SERP crops (Juneteenth / Asbury / Exchange Place)."""
    if os.environ.get("SERP_CAPTURE", "1").strip() in ("0", "false", "False", "no"):
        return jsonify({"error": "SERP capture disabled"}), 403
    try:
        from scripts.serp_screenshot import run_demo_nj
    except Exception as exc:
        return jsonify({"error": str(exc)}), 501
    out_dir = BASE_DIR / "static" / "mockups" / "google-overlays" / "live"
    try:
        results = run_demo_nj(out_dir)
    except Exception as exc:
        return jsonify({"error": f"demo failed: {exc}"}), 500
    return jsonify({
        "ok": all(r.get("ok") for r in results),
        "results": results,
        "catalog": "/static/mockups/google-overlays/live/",
    })


@app.route("/broll/status", methods=["GET"])
def broll_status():
    """Which B-roll image providers are configured.

    Add ?probe=1 (Pexels), ?probe=cse, or ?probe=serpapi (does not return secrets).
    """
    st = _broll_provider_status()
    raw = os.environ.get("PEXELS_API_KEY")
    aliases = _pexels_env_aliases_present()
    cse_key, cse_cx = _google_cse_creds()
    serp_key = _serpapi_api_key()
    out = {
        "providers": st,
        "photo_ready": _broll_any_photo_provider(),
        "gif_ready": _broll_gif_provider_ready(),
        "gemini_image_ready": _gemini_image_ready(),
        "build": "broll-status-v6-serpapi",
        "prefer_provider_options": [
            "auto", "serpapi", "pexels", "unsplash", "google_cse",
        ],
        # Diagnostics only — never includes the key value.
        "pexels_env": {
            "present": bool(_pexels_api_key()) or (raw is not None),
            "nonempty_after_strip": bool(_pexels_api_key()),
            "length": len(_pexels_api_key()),
            "had_surrounding_whitespace": bool(raw is not None and raw != raw.strip()),
            "alias_names": aliases,
            "expected_name": "PEXELS_API_KEY",
        },
        "serpapi_env": {
            "present": bool(serp_key),
            "length": len(serp_key),
            "ready": bool(serp_key),
            "expected_name": "SERPAPI_API_KEY",
        },
        "cse_env": {
            "api_key_present": bool(cse_key),
            "api_key_length": len(cse_key),
            "cx_present": bool(cse_cx),
            "cx_length": len(cse_cx),
            "ready": bool(cse_key and cse_cx),
            "expected_names": ["GOOGLE_CSE_API_KEY", "GOOGLE_CSE_CX"],
        },
        "hint": (
            "Preferred: SERPAPI_API_KEY (photos + GIFs). Also PEXELS_API_KEY / UNSPLASH_ACCESS_KEY. "
            "Replit Tools→Secrets, then Stop+Run — Cursor secrets do not sync. "
            "Optional: GEMINI_API_KEY for Generate AI photos. "
            "Probe: /broll/status?probe=serpapi | ?probe=1 | ?probe=cse"
        ),
    }
    probe = str(request.args.get("probe") or "").strip().lower()
    if probe in ("1", "true", "yes", "pexels"):
        out["pexels_probe"] = _probe_pexels_key()
    if probe in ("cse", "google", "google_cse", "all"):
        out["cse_probe"] = _probe_google_cse()
    if probe in ("serp", "serpapi", "serp_api", "all"):
        out["serpapi_probe"] = _probe_serpapi()
    if probe == "all":
        out["pexels_probe"] = _probe_pexels_key()
    return jsonify(out)


@app.route("/broll/generate-ai", methods=["POST"])
def broll_generate_ai():
    """Generate one still via Gemini for Replace-media / explicit Insert.

    Body: { prompt: str, keyword?: str }
    Returns asset_id for Timeline overlay insert — never auto-places.
    """
    if not _gemini_image_ready():
        return jsonify({
            "error": "GEMINI_API_KEY is not set in this Studio process.",
            "gemini_image_ready": False,
        }), 400
    data = request.get_json(force=True) or {}
    prompt = str(data.get("prompt") or data.get("text") or "").strip()
    keyword = str(data.get("keyword") or prompt or "AI photo").strip()[:80]
    if len(prompt) < 2:
        return jsonify({"error": "Prompt is required"}), 400
    if len(prompt) > 500:
        prompt = prompt[:500]
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    asset_id = uuid.uuid4().hex
    path = _generate_broll_gemini_image(prompt, ASSET_DIR / asset_id)
    if not path:
        return jsonify({"error": "Gemini did not return an image. Try a simpler prompt."}), 502
    final = ASSET_DIR / f"{asset_id}{path.suffix.lower()}"
    if path.resolve() != final.resolve():
        try:
            if final.exists():
                _safe_unlink(final)
            path.replace(final)
            path = final
        except OSError:
            pass
    _write_asset_meta(
        asset_id,
        filename=f"{keyword}{path.suffix.lower()}",
        keyword=keyword,
        source="gemini",
        prompt=prompt[:200],
    )
    return jsonify({
        "ok": True,
        "asset_id": asset_id,
        "source": "gemini",
        "keyword": keyword,
        "url": f"/asset/{asset_id}",
    })


@app.route("/broll/test-pexels", methods=["GET"])
def broll_test_pexels():
    """Live Pexels key check — never echoes the full key."""
    return jsonify(_probe_pexels_key())


@app.route("/broll/test-cse", methods=["GET"])
def broll_test_cse():
    """Live Google CSE key+cx check — never echoes secrets."""
    return jsonify(_probe_google_cse())


@app.route("/broll/test-serpapi", methods=["GET"])
def broll_test_serpapi():
    """Live SerpAPI key check — never echoes the full key."""
    return jsonify(_probe_serpapi())


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
    cleaned = _ensure_effects_span_timeline(cleaned, 0.0, total, words, max_effects)
    # Aim each push at the speaker rather than the centre of the frame, when
    # the reframe analysis has face data to aim with.
    for fx in cleaned:
        if fx["type"] in ("punch_zoom", "zoom_1_5", "zoom_2x"):
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


def _normalize_highlight_clip(c: dict) -> dict | None:
    """Coerce a Shorts / suggestion dict into a mid-form segment candidate."""
    if not isinstance(c, dict):
        return None
    try:
        start = float(c.get("start_time", c.get("start", 0)) or 0)
        end = float(c.get("end_time", c.get("end", 0)) or 0)
    except (TypeError, ValueError):
        return None
    if end <= start + 2.5:
        return None
    return {
        "start_time": round(start, 3),
        "end_time": round(end, 3),
        "duration": round(end - start, 3),
        "title": str(c.get("title") or "Beat")[:120],
        "hook_quote": str(c.get("hook_quote") or "")[:300],
        "reason": str(c.get("reason") or "")[:400],
        "viral_score": int(c.get("viral_score") or 0),
        "category": str(c.get("category") or "")[:80],
        "hook_start_time": float(c.get("hook_start_time", start) or start),
        "hook_end_time": float(c.get("hook_end_time", min(end, start + 5)) or min(end, start + 5)),
    }


def _pack_midform_segments(ordered: list, target_sec: float,
                            tolerance: float = 15.0,
                            hook_max_sec: float = 35.0) -> tuple[list, float]:
    """Greedy pack arc-ordered beats toward target (±tolerance).

    Arc roles (preferred): hook → grand_intro → body* → closer.
    Hook is always first and soft-capped to ``hook_max_sec`` (default 35s).
    """
    if not ordered:
        return [], 0.0
    lo = max(20.0, float(target_sec) - float(tolerance))
    hi = float(target_sec) + float(tolerance)
    hook_max_sec = max(8.0, min(45.0, float(hook_max_sec)))
    packed: list[dict] = []
    total = 0.0
    for i, raw in enumerate(ordered):
        seg = dict(raw)
        try:
            dur = float(seg["end_time"]) - float(seg["start_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if dur < 2.5:
            continue
        role = str(seg.get("role") or ("hook" if not packed else "body")).lower()
        # Skip heavy overlap with already-packed material (same source timeline).
        overlap_bad = False
        for p in packed:
            ov = min(float(seg["end_time"]), float(p["end_time"])) - max(
                float(seg["start_time"]), float(p["start_time"])
            )
            shorter = min(dur, float(p["end_time"]) - float(p["start_time"]))
            if shorter > 0 and ov / shorter > 0.55:
                overlap_bad = True
                break
        if overlap_bad and packed:
            continue
        if not packed:
            # Hook always included; cap to top-of-funnel length (≤ ~35s).
            if dur > hook_max_sec:
                seg["end_time"] = round(float(seg["start_time"]) + hook_max_sec, 3)
                seg["trimmed"] = True
                dur = hook_max_sec
            elif dur > hi and dur > target_sec * 1.35:
                seg["end_time"] = round(float(seg["start_time"]) + hi, 3)
                seg["trimmed"] = True
                dur = hi
            seg["role"] = "hook"
            packed.append(seg)
            total = dur
            continue
        if total >= lo and total >= target_sec - 5 and len(packed) >= 2:
            # Still allow a closer if we don't have one yet and it fits small.
            if role == "closer" and total + min(dur, 20) <= hi + 5:
                pass
            else:
                break
        if total + dur <= hi:
            packed.append(seg)
            total += dur
            if total >= target_sec and len(packed) >= 2 and role != "grand_intro":
                # Prefer stopping once we have hook + content; keep going for closer only if under.
                if any(str(p.get("role")) == "closer" for p in packed) or total >= target_sec + 5:
                    break
            continue
        remain = hi - total
        if remain >= 10:
            seg = dict(seg)
            seg["end_time"] = round(float(seg["start_time"]) + remain, 3)
            seg["trimmed"] = True
            packed.append(seg)
            total += remain
        break
    for i, seg in enumerate(packed):
        seg["duration"] = round(float(seg["end_time"]) - float(seg["start_time"]), 3)
        if not seg.get("role"):
            if i == 0:
                seg["role"] = "hook"
            elif i == len(packed) - 1 and len(packed) >= 3:
                seg["role"] = "closer"
            else:
                seg["role"] = "body"
    return packed, round(total, 2)


def _midform_fingerprint(segs: list) -> list:
    """Stable fingerprint of a mid-form pack for regeneration avoid lists."""
    out = []
    for s in segs or []:
        try:
            out.append([
                round(float(s.get("start_time", s.get("start", 0))), 1),
                round(float(s.get("end_time", s.get("end", 0))), 1),
            ])
        except (TypeError, ValueError):
            continue
    return out


def _looks_like_intro_clip(c: dict) -> bool:
    """Cheap transcript cue that a beat is a name/role introduction."""
    blob = " ".join([
        str(c.get("title") or ""),
        str(c.get("hook_quote") or ""),
        str(c.get("reason") or ""),
    ]).lower()
    cues = (
        "my name", "i'm ", "i am ", "this is ", "introduce", "introduction",
        "welcome", "thanks for having", "founder of", "ceo of", "host of",
        "here with", "joined by",
    )
    return any(x in blob for x in cues)


def _heuristic_midform_order(clips: list, target_sec: float,
                              avoid_fps: list | None = None) -> list:
    """Fallback arc: hook → optional grand_intro → body → closer."""
    if not clips:
        return []
    avoid = {
        (round(float(a[0]), 1), round(float(a[1]), 1))
        for a in (avoid_fps or [])
        if isinstance(a, (list, tuple)) and len(a) >= 2
    }
    ranked = sorted(clips, key=lambda c: (-int(c.get("viral_score") or 0), c["start_time"]))
    hook = None
    for c in ranked:
        sig = (round(c["start_time"], 1), round(c["end_time"], 1))
        if sig in avoid:
            continue
        hook = c
        break
    if hook is None:
        hook = ranked[0]
    rest = [
        c for c in clips
        if not (
            abs(c["start_time"] - hook["start_time"]) < 0.05
            and abs(c["end_time"] - hook["end_time"]) < 0.05
        )
    ]
    intros = [c for c in rest if _looks_like_intro_clip(c)]
    non_intro = [c for c in rest if c not in intros]
    after = sorted(
        [c for c in non_intro if c["start_time"] >= hook["end_time"] - 1.0],
        key=lambda c: c["start_time"],
    )
    before = sorted(
        [c for c in non_intro if c not in after],
        key=lambda c: c["start_time"],
    )
    body_pool = after + before
    ordered = [dict(hook, role="hook")]
    if intros:
        # Prefer an intro that isn't the hook itself.
        gi = sorted(intros, key=lambda c: c["start_time"])[0]
        ordered.append(dict(gi, role="grand_intro"))
        body_pool = [c for c in body_pool if c is not gi and c not in intros]
    # Body then closer (last remaining high-score / late beat)
    if len(body_pool) >= 2:
        closer = body_pool[-1]
        body = body_pool[:-1]
        ordered.extend(dict(c, role="body") for c in body[:4])
        ordered.append(dict(closer, role="closer"))
    else:
        ordered.extend(dict(c, role="body") for c in body_pool[:4])
    packed, _ = _pack_midform_segments(ordered, target_sec)
    return packed


def _gemini_midform_plan(words: list, clips: list, target_sec: float,
                          format_type: str = "interview",
                          variant: str = "story",
                          avoid_hook_indices: list | None = None,
                          avoid_fingerprints: list | None = None) -> dict:
    """Ask Gemini to assemble a mid-form arc toward target_sec.

    Canonical formula (Studio mid-form strategy):
      1) HOOK (required, top priority) — most outlandish / shocking / emotional /
         tonal-reaction beat. Top-of-funnel; ideally 10–35s (never a long chapter).
      2) GRAND INTRO (optional) — host or guest name/role introduce themselves
         *after* the hook when the transcript supports it.
      3) BODY — substance that advances the same through-line.
      4) CLOSER (preferred) — conclusive, thought-provoking, or emotional payoff;
         guest self-intro can live here only if it wasn't used as grand_intro.

    Regeneration: ``avoid_fingerprints`` lists prior packs so a re-click swaps
    beats while keeping the same arc roles.
    """
    pool = []
    for i, c in enumerate(clips):
        pool.append({
            "index": i,
            "start_time": c["start_time"],
            "end_time": c["end_time"],
            "duration": c["duration"],
            "title": c["title"],
            "hook_quote": c.get("hook_quote") or "",
            "reason": c.get("reason") or "",
            "viral_score": c.get("viral_score") or 0,
        })
    transcript = _format_transcript_for_llm(words)
    if len(transcript) > 12000:
        transcript = transcript[:12000] + "\n…"
    variant = (variant or "story").lower().strip()
    if variant not in ("punchy", "story", "deep"):
        variant = "story"
    avoid_hook_indices = list(avoid_hook_indices or [])
    avoid_fingerprints = list(avoid_fingerprints or [])
    variant_rules = {
        "punchy": (
            "VARIANT=punchy: shortest cut. Prefer hook + 1 body (+ optional closer). "
            "Alternate hook. Lean low on duration."
        ),
        "story": (
            "VARIANT=story: fullest coherent arc — hook → grand_intro(if any) → body → closer."
        ),
        "deep": (
            "VARIANT=deep: same through-line with richer body proof; still open with a "
            "shocking/emotional hook ≤35s."
        ),
    }[variant]
    avoid_block = ""
    if avoid_hook_indices:
        avoid_block += (
            f"Prefer a DIFFERENT hook than candidate index(es) {avoid_hook_indices}.\n"
        )
    if avoid_fingerprints:
        avoid_block += (
            "REGENERATION — do NOT rebuild these prior packs (same start/end pairs). "
            f"Swap in different candidates while keeping the arc roles: {json.dumps(avoid_fingerprints[:6])}\n"
        )
    prompt = f"""You are a YouTube mid-form editor. Build ONE mini-episode from highlight candidates.

Goal runtime: ~{target_sec:.0f}s (soft {max(20, target_sec - 15):.0f}–{target_sec + 15:.0f}s).
Format hint: {format_type}.
{variant_rules}
{avoid_block}
CANONICAL ARC (stick to this script — only skip optional beats if absent in the source):
1) HOOK (required, TOP PRIORITY): the most outlandish, shocking, emotional, or intense tonal
   reaction line. Top-of-funnel. Prefer 10–35 seconds total for this beat — never open with a
   polite intro. Facial-reaction moments are nice-to-have, not required.
2) GRAND INTRO (optional): AFTER the hook, if someone introduces their name/role or the host
   brings the guest on — place that next so viewers know who is speaking.
3) BODY: the substance that continues the same through-line (proof, story, explanation).
4) CLOSER (preferred when available): conclusive / thought-provoking / emotional payoff.
   If a guest self-intro was not used as grand_intro, it may close instead.

Rules:
- One through-line only. Drop high-score clips from a different chapter.
- Use only candidate indices below (each once). Do not invent times.
- roles MUST use: hook | grand_intro | body | closer
- First role MUST be hook. Prefer ending on closer when material exists.
- Aim for 2–5 clips so packed duration lands near {target_sec:.0f}s.

Candidates:
{json.dumps(pool, ensure_ascii=False)}

Transcript (context):
{transcript}

Return STRICT JSON:
{{
  "throughline": "one sentence describing the mini-episode story",
  "title": "3-8 word episode title",
  "ordered_indices": [2, 0, 5, 1],
  "roles": ["hook", "grand_intro", "body", "closer"],
  "why": "why this order follows the arc"
}}
"""
    result = _gemini_generate_clip_suggestions(prompt)
    if not isinstance(result, dict):
        raise RuntimeError("Gemini mid-form returned non-object")
    idxs = result.get("ordered_indices") or result.get("indices") or []
    if not isinstance(idxs, list) or not idxs:
        raise RuntimeError("Gemini mid-form returned no ordered_indices")
    roles = result.get("roles") if isinstance(result.get("roles"), list) else []
    allowed_roles = ("hook", "grand_intro", "body", "closer", "button")
    ordered = []
    seen = set()
    for n, raw_i in enumerate(idxs):
        try:
            i = int(raw_i)
        except (TypeError, ValueError):
            continue
        if i < 0 or i >= len(clips) or i in seen:
            continue
        seen.add(i)
        seg = dict(clips[i])
        role = str(roles[n] if n < len(roles) else ("hook" if not ordered else "body")).lower()
        if role not in allowed_roles:
            role = "hook" if not ordered else "body"
        if role == "button":
            role = "closer"
        seg["role"] = role
        ordered.append(seg)
    if not ordered:
        raise RuntimeError("Gemini mid-form indices invalid")
    ordered[0]["role"] = "hook"
    # If a closer wasn't labeled but we have 3+ beats, mark the last as closer.
    if len(ordered) >= 3 and ordered[-1].get("role") in ("body", "button"):
        if not any(s.get("role") == "closer" for s in ordered):
            ordered[-1]["role"] = "closer"
    packed, total = _pack_midform_segments(ordered, target_sec)
    return {
        "segments": packed,
        "packed_duration": total,
        "throughline": str(result.get("throughline") or "")[:240],
        "title": str(result.get("title") or "Mini-episode")[:120],
        "why": str(result.get("why") or "")[:400],
        "engine": "gemini",
        "variant": variant,
        "formula": "hook>grand_intro?>body*>closer?",
        "hook_index": next(
            (i for i, c in enumerate(clips)
             if packed and abs(float(c["start_time"]) - float(packed[0]["start_time"])) < 0.05
             and abs(float(c["end_time"]) - float(packed[0]["end_time"])) < 0.05),
            None,
        ),
    }


def _midform_variant_targets(base_goal: float) -> dict:
    """A/B variant duration goals from a shared base (e.g. 90 → 60/90/110)."""
    g = float(base_goal)
    return {
        "punchy": max(45.0, min(90.0, round(g * 0.67))),
        "story": max(60.0, min(150.0, g)),
        "deep": max(75.0, min(180.0, round(g * 1.22))),
    }


def _heuristic_midform_variants(clips: list, base_goal: float) -> list:
    """Build Punchy / Story / Deep without Gemini — different hooks + budgets."""
    if not clips:
        return []
    targets = _midform_variant_targets(base_goal)
    ranked = sorted(clips, key=lambda c: (-int(c.get("viral_score") or 0), c["start_time"]))
    versions = []
    used_hooks = set()
    specs = [
        ("punchy", "Punchy", "Shortest alternate cut for A/B testing"),
        ("story", "Story", "Most coherent mini-episode"),
        ("deep", "Deep", "Richer proof version of the same story"),
    ]
    for key, label, blurb in specs:
        # Pick hook: prefer unused high-score hooks
        hook = None
        for c in ranked:
            sig = (round(c["start_time"], 1), round(c["end_time"], 1))
            if sig in used_hooks:
                continue
            hook = c
            used_hooks.add(sig)
            break
        if hook is None:
            hook = ranked[0]
        rest = sorted(
            [c for c in clips if not (
                abs(c["start_time"] - hook["start_time"]) < 0.05
                and abs(c["end_time"] - hook["end_time"]) < 0.05
            )],
            key=lambda c: c["start_time"],
        )
        after = [c for c in rest if c["start_time"] >= hook["end_time"] - 1.0]
        before = [c for c in rest if c not in after]
        # Punchy: fewer after-hook beats; Deep: allow more
        if key == "punchy":
            body = (after + before)[:3]
        elif key == "deep":
            body = (after + before)[:6]
        else:
            body = (after + before)[:5]
        ordered = [dict(hook, role="hook")] + [dict(c, role="body") for c in body]
        packed, total = _pack_midform_segments(ordered, targets[key])
        if not packed:
            continue
        versions.append({
            "id": key,
            "label": label,
            "blurb": blurb,
            "goal_duration": targets[key],
            "packed_duration": total,
            "delta_sec": round(total - targets[key], 1),
            "title": f"{label}: {packed[0].get('title') or 'Mini-episode'}"[:120],
            "throughline": blurb,
            "why": f"Heuristic {label} pack",
            "engine": "heuristic",
            "variant": key,
            "segment_count": len(packed),
            "segments": packed,
        })
    return versions


def _ensure_midform_clip_pool(job_id: str, *, format_type: str,
                               refresh_shorts: bool, num_pool: int) -> list:
    """Return normalized highlight pool, refreshing Shorts when sparse."""
    job = jobs[job_id]
    words = job.get("words") or []
    pool_raw = list(job.get("clip_suggestions") or [])
    if refresh_shorts or len(pool_raw) < 3:
        transcript_text = _format_transcript_for_llm(words)
        durations = [15, 30, 45, 60]
        prompt = _build_clip_suggestion_prompt(
            transcript_text, format_type, durations, max(5, min(12, int(num_pool))),
        )
        result = _gemini_generate_clip_suggestions(prompt)
        clips_raw = result.get("clips", []) if isinstance(result, dict) else []
        cleaned = []
        for c in clips_raw:
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
        _snap_clip_to_target_durations(cleaned, words, durations)
        cleaned = [c for c in cleaned if (c["end_time"] - c["start_time"]) >= 3]
        cleaned.sort(key=lambda c: c["start_time"])
        _detect_overlap_groups(cleaned, threshold=0.90)
        if cleaned:
            pool_raw = cleaned
            job["clip_suggestions"] = cleaned
            job["clip_format"] = format_type
            _db_save_job(job_id)
    clips = []
    for c in pool_raw:
        n = _normalize_highlight_clip(c)
        if n:
            clips.append(n)
    return clips


def _segments_payload(job_id: str, segs: list) -> list:
    out = []
    for s in segs or []:
        out.append({
            "source_job_id": job_id,
            "start_time": s["start_time"],
            "end_time": s["end_time"],
            "duration": s.get("duration") or round(float(s["end_time"]) - float(s["start_time"]), 3),
            "title": s.get("title") or "",
            "hook_quote": s.get("hook_quote") or "",
            "role": s.get("role") or "body",
            "trimmed": bool(s.get("trimmed")),
        })
    return out


def _build_midform_episode(job_id: str, *, target_sec: float = 90.0,
                            format_type: str = "interview",
                            refresh_shorts: bool = False,
                            num_pool: int = 8,
                            avoid_fingerprints: list | None = None,
                            regenerate: bool = False) -> dict:
    """Plan a ~target_sec mini-episode from AI Shorts for one source job.

    Formula: hook (shock/emotion ≤35s) → grand_intro? → body* → closer?
    When ``regenerate`` is true, prior packs on the job are avoided so a
    re-click swaps beats while keeping the same arc roles.
    """
    if job_id not in jobs:
        raise ValueError("Unknown job")
    job = jobs[job_id]
    words = job.get("words") or []
    if not words:
        raise ValueError("Transcript not ready yet")
    target_sec = max(45.0, min(180.0, float(target_sec)))
    format_type = (format_type or "interview").lower()
    if format_type not in _FORMAT_RUBRICS:
        format_type = "interview"

    # Prior packs for regeneration (client may also send avoid_fingerprints).
    history = list(job.get("midform_history") or [])
    avoid_fps = list(avoid_fingerprints or [])
    if regenerate and not avoid_fps:
        for prev in history[-6:]:
            if isinstance(prev, list):
                avoid_fps.append(prev)
            elif isinstance(prev, dict) and prev.get("fingerprint"):
                avoid_fps.append(prev["fingerprint"])
    # Also avoid the latest saved plan if regenerating.
    if regenerate:
        last = job.get("midform_plan") or {}
        fp = _midform_fingerprint(last.get("segments") or [])
        if fp and fp not in avoid_fps:
            avoid_fps.append(fp)

    pool_raw = list(job.get("clip_suggestions") or [])
    if refresh_shorts or len(pool_raw) < 3:
        transcript_text = _format_transcript_for_llm(words)
        # Mid-form wants a diverse pool of short beats to stitch.
        durations = [15, 30, 45, 60]
        prompt = _build_clip_suggestion_prompt(
            transcript_text, format_type, durations, max(5, min(12, int(num_pool))),
        )
        result = _gemini_generate_clip_suggestions(prompt)
        clips_raw = result.get("clips", []) if isinstance(result, dict) else []
        cleaned = []
        for c in clips_raw:
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
        _snap_clip_to_target_durations(cleaned, words, durations)
        cleaned = [c for c in cleaned if (c["end_time"] - c["start_time"]) >= 3]
        cleaned.sort(key=lambda c: c["start_time"])
        _detect_overlap_groups(cleaned, threshold=0.90)
        if cleaned:
            pool_raw = cleaned
            job["clip_suggestions"] = cleaned
            job["clip_format"] = format_type
            _db_save_job(job_id)

    clips = []
    for c in pool_raw:
        n = _normalize_highlight_clip(c)
        if n:
            clips.append(n)
    if len(clips) < 2:
        raise ValueError(
            "Need at least 2 AI Shorts moments to stitch a mid-form episode. "
            "Run Find highlights first, or try again with refresh."
        )

    plan = None
    gemini_err = None
    try:
        plan = _gemini_midform_plan(
            words, clips, target_sec,
            format_type=format_type,
            avoid_fingerprints=avoid_fps or None,
        )
    except Exception as exc:
        gemini_err = str(exc)
        ai_logger.warning(f"[midform] Gemini plan failed ({exc}); heuristic fallback")
        packed = _heuristic_midform_order(clips, target_sec, avoid_fps=avoid_fps)
        plan = {
            "segments": packed,
            "packed_duration": round(sum(s["duration"] for s in packed), 2),
            "throughline": "Heuristic arc: shock hook → intro? → body → closer",
            "title": (packed[0]["title"] if packed else "Mini-episode")[:120],
            "why": "Gemini unavailable or failed — used viral hook + arc packing",
            "engine": "heuristic",
            "formula": "hook>grand_intro?>body*>closer?",
        }

    segs = plan.get("segments") or []
    if not segs:
        raise ValueError("Could not pack a mid-form episode from these highlights")

    packed_dur = float(plan.get("packed_duration") or sum(s["duration"] for s in segs))
    delta = packed_dur - target_sec
    fp = _midform_fingerprint(segs)
    payload = {
        "ok": True,
        "job_id": job_id,
        "goal_duration": round(target_sec, 1),
        "packed_duration": round(packed_dur, 1),
        "delta_sec": round(delta, 1),
        "within_tolerance": abs(delta) <= 15.0,
        "title": plan.get("title") or "Mini-episode",
        "throughline": plan.get("throughline") or "",
        "why": plan.get("why") or "",
        "engine": plan.get("engine") or "unknown",
        "formula": plan.get("formula") or "hook>grand_intro?>body*>closer?",
        "regenerated": bool(regenerate or avoid_fps),
        "segment_count": len(segs),
        "segments": [
            {
                "source_job_id": job_id,
                "start_time": s["start_time"],
                "end_time": s["end_time"],
                "duration": s["duration"],
                "title": s.get("title") or "",
                "hook_quote": s.get("hook_quote") or "",
                "role": s.get("role") or "body",
                "trimmed": bool(s.get("trimmed")),
            }
            for s in segs
        ],
        "fingerprint": fp,
        "pool_size": len(clips),
        "gemini_warning": gemini_err,
        "shorts_refreshed": bool(refresh_shorts or len(pool_raw) >= 3),
    }
    # Remember packs so the next click can regenerate differently.
    history = list(job.get("midform_history") or [])
    history.append(fp)
    job["midform_history"] = history[-12:]
    job["midform_plan"] = payload
    _db_save_job(job_id)
    print(
        f"[midform] {job_id} goal={target_sec:.0f}s packed={packed_dur:.1f}s "
        f"segs={len(segs)} engine={payload['engine']} regen={payload['regenerated']}",
        flush=True,
    )
    return payload


def _build_midform_versions(job_id: str, *, base_goal: float = 90.0,
                             format_type: str = "interview",
                             refresh_shorts: bool = False,
                             num_pool: int = 10) -> dict:
    """A/B: 2–3 alternate mid-form cuts (Punchy / Story / Deep) of the same story."""
    if job_id not in jobs:
        raise ValueError("Unknown job")
    job = jobs[job_id]
    words = job.get("words") or []
    if not words:
        raise ValueError("Transcript not ready yet")
    base_goal = max(45.0, min(180.0, float(base_goal)))
    format_type = (format_type or "interview").lower()
    if format_type not in _FORMAT_RUBRICS:
        format_type = "interview"

    clips = _ensure_midform_clip_pool(
        job_id, format_type=format_type, refresh_shorts=refresh_shorts, num_pool=num_pool,
    )
    if len(clips) < 2:
        raise ValueError(
            "Need at least 2 AI Shorts moments to build A/B mid-form versions. "
            "Run Find highlights first."
        )

    targets = _midform_variant_targets(base_goal)
    versions: list[dict] = []
    gemini_err = None
    throughline = ""
    used_hook_idxs: list[int] = []

    for key, label, blurb in (
        ("story", "Story", "Most coherent mini-episode"),
        ("punchy", "Punchy", "Shortest alternate cut for A/B"),
        ("deep", "Deep", "Richer proof version of the same story"),
    ):
        plan = None
        try:
            plan = _gemini_midform_plan(
                words, clips, targets[key],
                format_type=format_type,
                variant=key,
                avoid_hook_indices=used_hook_idxs if key != "story" else None,
            )
            if plan.get("throughline") and not throughline:
                throughline = plan["throughline"]
            elif throughline and not plan.get("throughline"):
                plan["throughline"] = throughline
            hi = plan.get("hook_index")
            if isinstance(hi, int) and hi not in used_hook_idxs:
                used_hook_idxs.append(hi)
        except Exception as exc:
            gemini_err = str(exc)
            ai_logger.warning(f"[midform] variant {key} Gemini failed: {exc}")
            plan = None
        if not plan or not plan.get("segments"):
            continue
        segs = plan["segments"]
        packed = float(plan.get("packed_duration") or sum(s["duration"] for s in segs))
        versions.append({
            "id": key,
            "label": label,
            "blurb": blurb,
            "goal_duration": targets[key],
            "packed_duration": round(packed, 1),
            "delta_sec": round(packed - targets[key], 1),
            "within_tolerance": abs(packed - targets[key]) <= 18.0,
            "title": str(plan.get("title") or f"{label} mini-episode")[:120],
            "throughline": str(plan.get("throughline") or throughline or "")[:240],
            "why": str(plan.get("why") or blurb)[:400],
            "engine": plan.get("engine") or "gemini",
            "variant": key,
            "formula": plan.get("formula") or "hook>grand_intro?>body*>closer?",
            "segment_count": len(segs),
            "segments": _segments_payload(job_id, segs),
        })

    if len(versions) < 2:
        # Full heuristic set when Gemini only produced 0–1 usable variants.
        heur = _heuristic_midform_variants(clips, base_goal)
        for h in heur:
            h["segments"] = _segments_payload(job_id, h.get("segments") or [])
            h["job_id"] = job_id
        if heur:
            versions = heur
            if gemini_err is None:
                gemini_err = "Fell back to heuristic A/B packs"

    if not versions:
        raise ValueError("Could not build mid-form A/B versions")

    # Deduplicate near-identical packs (same segment fingerprint)
    def _fp(v: dict) -> tuple:
        return tuple(
            (round(float(s["start_time"]), 1), round(float(s["end_time"]), 1))
            for s in (v.get("segments") or [])
        )
    unique = []
    seen_fp = set()
    for v in versions:
        fp = _fp(v)
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        unique.append(v)
    # Prefer keeping punchy/story/deep labels even if we had to drop one
    versions = unique[:3]
    if len(versions) < 2 and len(clips) >= 3:
        # Force heuristic diversity
        versions = _heuristic_midform_variants(clips, base_goal)
        for h in versions:
            h["segments"] = _segments_payload(job_id, h.get("segments") or [])

    payload = {
        "ok": True,
        "job_id": job_id,
        "base_goal": round(base_goal, 1),
        "throughline": throughline or (versions[0].get("throughline") if versions else ""),
        "version_count": len(versions),
        "versions": versions,
        "pool_size": len(clips),
        "gemini_warning": gemini_err,
        "engine": versions[0].get("engine") if versions else "unknown",
    }
    job["midform_versions"] = payload
    _db_save_job(job_id)
    print(
        f"[midform] versions {job_id} base={base_goal:.0f}s n={len(versions)} "
        f"durs={[v.get('packed_duration') for v in versions]}",
        flush=True,
    )
    return payload


@app.route("/midform-episode", methods=["POST"])
def midform_episode():
    """Build one mid-form mini-episode, or 2–3 A/B versions (Punchy/Story/Deep).

    Body: {
      job_id,
      target_duration?: 60|90|120 (default 90),
      format?: shorts format,
      refresh_shorts?: bool,
      versions?: bool | int,
      regenerate?: bool,  # avoid prior packs; keep hook→intro→body→closer arc
      avoid_fingerprints?: [[[start,end], ...], ...]
    }

    Arc formula: HOOK (shock/emotion ≤35s) → GRAND INTRO? → BODY* → CLOSER?
    """
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job — select a transcribed video first"}), 404
    try:
        target = float(data.get("target_duration") or data.get("goal_duration") or 90)
    except (TypeError, ValueError):
        target = 90.0
    format_type = (data.get("format") or jobs[job_id].get("clip_format") or "interview")
    refresh = bool(data.get("refresh_shorts"))
    regenerate = bool(data.get("regenerate"))
    avoid_fps = data.get("avoid_fingerprints") if isinstance(data.get("avoid_fingerprints"), list) else None
    want_versions = data.get("versions")
    multi = False
    if want_versions is True or str(want_versions).lower() in ("1", "true", "yes", "ab", "all"):
        multi = True
    else:
        try:
            multi = int(want_versions) >= 2
        except (TypeError, ValueError):
            multi = False
    try:
        if multi:
            plan = _build_midform_versions(
                job_id,
                base_goal=target,
                format_type=str(format_type),
                refresh_shorts=refresh,
            )
        else:
            plan = _build_midform_episode(
                job_id,
                target_sec=target,
                format_type=str(format_type),
                refresh_shorts=refresh,
                regenerate=regenerate,
                avoid_fingerprints=avoid_fps,
            )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        ai_logger.exception(f"[midform] failed for {job_id}: {e}")
        return jsonify({"error": str(e)}), 500
    return jsonify(plan)


@app.route("/midform-versions", methods=["POST"])
def midform_versions():
    """Alias: always returns Punchy / Story / Deep A/B packs."""
    data = request.get_json(force=True) or {}
    data["versions"] = True
    # Reuse handler logic via internal call shape
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job — select a transcribed video first"}), 404
    try:
        target = float(data.get("target_duration") or data.get("goal_duration") or 90)
    except (TypeError, ValueError):
        target = 90.0
    format_type = (data.get("format") or jobs[job_id].get("clip_format") or "interview")
    refresh = bool(data.get("refresh_shorts"))
    try:
        plan = _build_midform_versions(
            job_id,
            base_goal=target,
            format_type=str(format_type),
            refresh_shorts=refresh,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        ai_logger.exception(f"[midform] versions failed for {job_id}: {e}")
        return jsonify({"error": str(e)}), 500
    return jsonify(plan)


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


def _compile_target_canvas(src_paths: list[Path]) -> tuple[int, int]:
    """Fixed even canvas for stitching AI Shorts — majority portrait → 9:16."""
    dims: list[tuple[int, int]] = []
    for p in src_paths:
        try:
            dims.append(get_video_dimensions(p))
        except Exception:
            continue
    if not dims:
        return 1080, 1920
    portrait = sum(1 for w, h in dims if h >= w)
    if portrait >= (len(dims) + 1) // 2:
        return 1080, 1920
    return 1920, 1080


def _compile_trim_segment(
    src: Path, start: float, end: float, out_path: Path, W: int, H: int
) -> None:
    """Trim one highlight into a uniform H.264/AAC segment for concat demuxer.

    All segments must share identical size/fps/pix_fmt/audio layout or
    ``-c copy`` concat silently produces broken A/V (the classic "compile
    doesn't stitch properly" symptom when mixing 9:16 + 16:9 sources).
    """
    dur = max(0.05, float(end) - float(start))
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps=30,format=yuv420p,setsar=1"
    )
    has_a = _has_audio_stream(src)
    # Soft loudness match across interviews so volume doesn't jump on stitch.
    af = (
        "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
        "loudnorm=I=-14:TP=-1.5:LRA=11"
    )
    # Accurate seek (-ss after -i) so highlight IN/OUT match the AI Shorts card.
    cmd = [FFMPEG, "-y", "-i", str(src), "-ss", f"{start:.3f}", "-t", f"{dur:.3f}"]
    if has_a:
        cmd += [
            "-vf", vf,
            "-af", af,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-shortest", str(out_path),
        ]
    else:
        cmd += [
            "-f", "lavfi", "-t", f"{dur:.3f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-vf", vf,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-shortest", str(out_path),
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 512:
        raise RuntimeError((proc.stderr or proc.stdout or "trim failed")[-500:])


@app.route("/compile-clips", methods=["POST"])
def compile_clips():
    """Stitch multiple clip ranges into one composite job.

    Body: {clips: [{source_job_id, start_time, end_time}], label}
    Returns: {job_id, filename}

    Each segment is trimmed and re-encoded (libx264 / aac) onto a *shared*
    canvas so the concat demuxer can copy-stream them into one mp4.
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
            "source_label": str(item.get("source_label") or "")[:80],
            "theme": str(item.get("theme") or "")[:120],
            "arc_role": str(item.get("arc_role") or "")[:40],
        })

    new_job_id = uuid.uuid4().hex
    composite_path = UPLOAD_DIR / f"{new_job_id}.mp4"
    W, H = _compile_target_canvas([v["src_video"] for v in validated])

    # 1. Trim + normalize each segment to a uniform format so concat is clean.
    seg_paths: list[Path] = []
    list_path: Path | None = None
    try:
        for i, v in enumerate(validated):
            seg_path = UPLOAD_DIR / f"{new_job_id}_seg{i:03d}.mp4"
            try:
                _compile_trim_segment(
                    v["src_video"], v["start"], v["end"], seg_path, W, H
                )
            except RuntimeError as exc:
                return jsonify({"error": f"Trim failed on clip {i + 1}: {exc}"}), 500
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
            # Remux-reencode fallback if copy concat chokes on edge cases.
            cmd_fb = [
                FFMPEG, "-y", "-f", "concat", "-safe", "0",
                "-i", str(list_path),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                *_QT_SAFE_MP4_ARGS,
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                str(composite_path),
            ]
            proc2 = subprocess.run(cmd_fb, capture_output=True, text=True)
            if proc2.returncode != 0:
                return jsonify({
                    "error": f"Concat failed: {(proc2.stderr or proc.stderr or '')[-500:]}"
                }), 500
    finally:
        for p in seg_paths:
            _safe_unlink(p)
        if list_path:
            _safe_unlink(list_path)

    if not composite_path.exists() or composite_path.stat().st_size < 1024:
        return jsonify({"error": "Compile produced an empty file"}), 500

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
        "canvas": f"{W}x{H}",
        "clips": [
            {
                "source_job_id": v["source_job_id"],
                "start_time": v["start"],
                "end_time": v["end"],
                "title": v["title"],
                "hook_quote": v["hook_quote"],
                "source_filename": v["source_filename"],
                "source_label": v.get("source_label") or "",
                "theme": v.get("theme") or "",
                "arc_role": v.get("arc_role") or "",
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
        "duration": cumulative,
    }
    _db_save_job(new_job_id)
    return jsonify({
        "job_id": new_job_id,
        "filename": new_filename,
        "segments": len(validated),
        "duration": cumulative,
        "canvas": f"{W}x{H}",
    })


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
            "archived": bool(recipe.get("archived")),
        })
    out.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return jsonify({"compilations": out})


@app.route("/archive-compilation/<job_id>", methods=["POST"])
def archive_compilation(job_id: str):
    """Hide or unhide a past compilation without deleting sources or the bake.

    Body: { archived?: bool } — default true (archive). Sets compile_recipe.archived.
    Does not delete the job, MP4, or any source interviews.
    """
    if job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    recipe = jobs[job_id].get("compile_recipe")
    if not isinstance(recipe, dict):
        return jsonify({"error": "This job is not a compilation."}), 400
    data = request.get_json(silent=True) or {}
    if "archived" in data:
        archived = bool(data.get("archived"))
    else:
        archived = True
    recipe = dict(recipe)
    recipe["archived"] = archived
    jobs[job_id]["compile_recipe"] = recipe
    _db_save_job(job_id)
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "archived": archived,
        "label": recipe.get("label") or jobs[job_id].get("filename"),
    })


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


@app.route("/multi-interview/plan", methods=["POST"])
def multi_interview_plan():
    """Plan a cross-interview reel from multiple uploaded / transcribed jobs.

    Body: {
      job_ids: [...],
      format?: "interview",
      goal_sec?: 90,
      max_themes?: 6,
      clips_per_theme?: 4,
      avoid_fingerprints?: [...]   # prior arc fingerprints for regenerate
    }
    Returns themes (browse) + arc (ordered story beats) for the compile queue.
    """
    data = request.get_json(force=True) or {}
    raw_ids = data.get("job_ids") or data.get("jobs") or []
    if not isinstance(raw_ids, list) or len(raw_ids) < 2:
        return jsonify({"error": "Pick at least 2 transcribed videos (job_ids)."}), 400
    job_ids = []
    for jid in raw_ids[:12]:
        jid = str(jid or "").strip()
        if jid and jid in jobs and jid not in job_ids:
            job_ids.append(jid)
    if len(job_ids) < 2:
        return jsonify({"error": "Need at least 2 valid transcribed jobs."}), 400

    try:
        max_themes = max(2, min(10, int(data.get("max_themes") or 6)))
    except (TypeError, ValueError):
        max_themes = 6
    try:
        clips_per = max(2, min(8, int(data.get("clips_per_theme") or 4)))
    except (TypeError, ValueError):
        clips_per = 4
    try:
        goal_sec = float(data.get("goal_sec") or data.get("target_sec") or 90)
    except (TypeError, ValueError):
        goal_sec = 90.0
    goal_sec = max(45.0, min(180.0, goal_sec))
    format_type = str(data.get("format") or "interview")
    avoid = data.get("avoid_fingerprints") or []
    if not isinstance(avoid, list):
        avoid = []
    avoid = [str(a)[:200] for a in avoid[-8:] if a]

    blocks = []
    meta = []
    for jid in job_ids:
        j = jobs[jid]
        words = j.get("words") or []
        if not words:
            continue
        fname = j.get("filename") or jid[:8]
        transcript = _format_transcript_for_llm(words)
        if not transcript.strip():
            continue
        # Cap each transcript so multi-job prompts stay within context.
        if len(transcript) > 6000:
            transcript = transcript[:6000] + "\n…"
        blocks.append(f"=== SOURCE {jid} | {fname} ===\n{transcript}")
        meta.append({
            "job_id": jid,
            "filename": fname,
            "word_count": len(words),
            "duration": float(words[-1].get("end") or 0) if words else 0,
        })
    if len(blocks) < 2:
        return jsonify({"error": "At least 2 jobs need transcripts (words)."}), 400

    avoid_note = ""
    if avoid:
        avoid_note = (
            "\nREGENERATE: pick DIFFERENT clip ranges / source order than these "
            "prior fingerprints (same arc roles OK):\n- "
            + "\n- ".join(avoid)
            + "\n"
        )

    prompt = f"""You are editing a MULTI-INTERVIEW reel across DIFFERENT speakers/sources.
Build a mini story arc (not just theme clusters), aiming for ~{goal_sec:.0f}s total.

Format hint: {format_type}
{avoid_note}
Return TWO things:
1) "arc" — ordered story beats: hook → answer_a → answer_b (more answers OK) → closer.
   Each beat is ONE clip from ONE source. Prefer DIFFERENT sources for consecutive beats.
   Hook = strongest quotable / emotional open (any source). Closer = button/payoff.
2) "themes" — up to {max_themes} shared questions with up to {clips_per} clips each (browse/add later).

Rules:
- Use ONLY the provided source_job_id values.
- start_time / end_time must be inside that source's transcript timestamps.
- Prefer answer-led hooks (lead with the quotable line, not the question).
- Arc clip length 8–40s each; total arc duration near {goal_sec:.0f}s (±25s OK).
- roles MUST use: hook | answer | closer  (use answer for all middle beats)
- Skip weak single-source themes unless uniquely strong.

Return STRICT JSON:
{{
  "throughline": "one-sentence shared question / thesis",
  "arc": [
    {{
      "role": "hook",
      "source_job_id": "abc123",
      "start_time": 40.2,
      "end_time": 55.0,
      "title": "Shock open",
      "hook_quote": "exact spoken line",
      "reason": "why this opens the reel",
      "theme": "Finding local events"
    }}
  ],
  "themes": [
    {{
      "theme": "Finding local events",
      "question": "How do you discover events in your city?",
      "clips": [
        {{
          "source_job_id": "abc123",
          "start_time": 40.2,
          "end_time": 72.5,
          "title": "App calendars + friends",
          "hook_quote": "exact spoken line",
          "reason": "why this answers the shared question"
        }}
      ]
    }}
  ]
}}

Sources:
{chr(10).join(blocks)}
"""
    try:
        result = _gemini_generate_clip_suggestions(prompt)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    allowed = set(job_ids)

    def _normalize_clip(c, *, role="", theme=""):
        if not isinstance(c, dict):
            return None
        sid = str(c.get("source_job_id") or "").strip()
        if sid not in allowed:
            return None
        try:
            st = float(c.get("start_time", c.get("start", 0)))
            en = float(c.get("end_time", c.get("end", 0)))
        except (TypeError, ValueError):
            return None
        if en - st < 3.0:
            return None
        words = jobs[sid].get("words") or []
        if words:
            tmax = float(words[-1].get("end") or en)
            st = max(0.0, min(st, tmax - 3.0))
            en = max(st + 3.0, min(en, tmax))
        fname = jobs[sid].get("filename") or ""
        label = str(fname).rsplit(".", 1)[0][:60] if fname else sid[:8]
        clip_role = str(c.get("role") or role or "").strip().lower()
        if clip_role not in ("hook", "answer", "closer", "grand_intro", "body"):
            clip_role = role or "answer"
        if clip_role in ("grand_intro", "body"):
            clip_role = "answer"
        return {
            "source_job_id": sid,
            "source_filename": fname,
            "source_label": label,
            "start_time": round(st, 3),
            "end_time": round(en, 3),
            "title": str(c.get("title") or theme or clip_role or "clip")[:120],
            "hook_quote": str(c.get("hook_quote") or "")[:300],
            "reason": str(c.get("reason") or "")[:300],
            "theme": str(c.get("theme") or theme or "")[:120],
            "arc_role": clip_role,
        }

    # ---- Arc beats ----
    arc_raw = result.get("arc") if isinstance(result, dict) else None
    arc = []
    if isinstance(arc_raw, list):
        for i, beat in enumerate(arc_raw[:10]):
            default_role = "hook" if i == 0 else ("closer" if i == len(arc_raw) - 1 else "answer")
            norm = _normalize_clip(beat, role=default_role)
            if norm:
                arc.append(norm)

    # Fallback: synthesize arc from first theme if Gemini omitted arc.
    themes_raw = result.get("themes") if isinstance(result, dict) else None
    if not arc and isinstance(themes_raw, list) and themes_raw:
        first = themes_raw[0] if isinstance(themes_raw[0], dict) else {}
        theme_name = str(first.get("theme") or "Theme")[:120]
        for i, c in enumerate((first.get("clips") or [])[:4]):
            role = "hook" if i == 0 else ("closer" if i == min(3, len(first.get("clips") or []) - 1) else "answer")
            norm = _normalize_clip(c, role=role, theme=theme_name)
            if norm:
                arc.append(norm)

    # Pack toward goal_sec (prefer keeping hook + closer).
    if arc:
        packed = []
        total = 0.0
        for i, beat in enumerate(arc):
            dur = float(beat["end_time"]) - float(beat["start_time"])
            is_edge = beat.get("arc_role") in ("hook", "closer") or i == 0 or i == len(arc) - 1
            if packed and total + dur > goal_sec * 1.35 and not is_edge:
                continue
            if total >= goal_sec and not is_edge and len(packed) >= 3:
                continue
            packed.append(beat)
            total += dur
        if packed:
            arc = packed

    # ---- Themes (browse) ----
    themes = []
    if isinstance(themes_raw, list):
        for th in themes_raw[:max_themes]:
            if not isinstance(th, dict):
                continue
            theme_name = str(th.get("theme") or "Theme")[:120]
            clips_out = []
            for c in (th.get("clips") or [])[:clips_per]:
                norm = _normalize_clip(c, theme=theme_name)
                if norm:
                    clips_out.append(norm)
            if clips_out:
                themes.append({
                    "theme": theme_name,
                    "question": str(th.get("question") or "")[:240],
                    "clips": clips_out,
                })

    arc_total = sum(float(b["end_time"]) - float(b["start_time"]) for b in arc)
    fingerprint = "|".join(
        f"{b['arc_role']}:{b['source_job_id'][:8]}:{b['start_time']:.1f}-{b['end_time']:.1f}"
        for b in arc
    )

    if not arc and not themes:
        return jsonify({"error": "Gemini returned no arc or themes", "raw": result}), 500

    return jsonify({
        "ok": True,
        "sources": meta,
        "throughline": str((result or {}).get("throughline") or "")[:240] if isinstance(result, dict) else "",
        "arc": arc,
        "arc_total_s": round(arc_total, 1),
        "goal_sec": goal_sec,
        "fingerprint": fingerprint,
        "formula": "hook>answer*>closer",
        "themes": themes,
        "theme_count": len(themes),
        "clip_count": sum(len(t["clips"]) for t in themes),
    })


@app.route("/preview-tightening", methods=["POST"])
def preview_tightening():
    """Compute silence-compression stats and per-gap details for a job.

    Body: {job_id, max_gap, target_gap, preserved_gap_starts?, taste_protect?}
    Returns: {stats, gaps, taste?}

    Identification (gap scan) stays separate from taste: when taste_protect is
    true, humor/shock pauses are auto-marked preserved before execution.
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
    preserved = list(data.get("preserved_gap_starts") or [])
    taste_protect = data.get("taste_protect") is True or str(
        data.get("taste_protect") or ""
    ).lower() in ("1", "true", "yes")

    # Identify gaps first (no taste yet) so we can score them.
    base = compute_silence_compression(
        words,
        max_gap=max_gap,
        target_gap=target_gap,
        preserved_gap_starts=preserved,
    )
    taste = {"protected_starts": [], "decisions": [], "engine": "none"}
    if taste_protect and base.get("gaps"):
        try:
            taste = _taste_protect_silence_gaps(words, base["gaps"])
        except Exception as exc:
            ai_logger.warning(f"[taste] preview protect failed: {exc}")
            taste = {"protected_starts": [], "decisions": [], "engine": "error"}
        # Merge LLM/heuristic protects into preserved, then recompute execution.
        merged_preserved = sorted({
            round(float(t), 1) for t in (preserved + list(taste.get("protected_starts") or []))
        })
        comp = compute_silence_compression(
            words,
            max_gap=max_gap,
            target_gap=target_gap,
            preserved_gap_starts=merged_preserved,
        )
        gaps = _apply_taste_to_gaps(comp.get("gaps") or [], taste)
        stats = dict(comp["stats"])
        stats["taste_protected"] = len(taste.get("protected_starts") or [])
        stats["taste_engine"] = taste.get("engine")
        return jsonify({
            "stats": stats,
            "gaps": gaps,
            "taste": taste,
            "preserved_gap_starts": merged_preserved,
        })

    return jsonify({"stats": base["stats"], "gaps": base["gaps"], "taste": taste})


@app.route("/taste-protect-gaps", methods=["POST"])
def taste_protect_gaps():
    """Run taste scoring on current silence gaps and return Protected starts.

    Body: {job_id, max_gap?, target_gap?, preserved_gap_starts?}
    Does not cut anything — only returns which gaps editing taste would keep.
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
    base = compute_silence_compression(
        words, max_gap=max_gap, target_gap=target_gap,
        preserved_gap_starts=preserved,
    )
    taste = _taste_protect_silence_gaps(words, base.get("gaps") or [])
    gaps = _apply_taste_to_gaps(base.get("gaps") or [], taste)
    return jsonify({
        "taste": taste,
        "gaps": gaps,
        "protected_gap_starts": taste.get("protected_starts") or [],
    })


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
    templates_dir = BASE_DIR / "templates"
    try:
        mtimes = []
        for d in (static_dir, templates_dir):
            if not d.is_dir():
                continue
            for p in d.rglob("*"):
                if p.is_file():
                    try:
                        mtimes.append(p.stat().st_mtime)
                    except OSError:
                        pass
        asset_version = str(int(max(mtimes) if mtimes else time.time()))
    except OSError:
        asset_version = str(int(time.time()))
    html = render_template(
        "index.html",
        auphonic_enabled=auphonic_enabled,
        elevenlabs_enabled=elevenlabs_enabled,
        dolby_enabled=dolby_enabled,
        gemini_enabled=gemini_enabled,
        broll_photo_ready=broll_photo_ready,
        broll_google_cse=bool(broll_providers.get("google_cse")),
        broll_serpapi=bool(broll_providers.get("serpapi")),
        broll_gif_ready=_broll_gif_provider_ready(),
        broll_providers=broll_providers,
        capcut_templates=CAPCUT_TEMPLATES,
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


@app.route("/upload/init", methods=["POST"])
def upload_init():
    """Start a chunked ingest session (resumable; survives proxy timeouts).

    JSON body: {filename, size, pre_clean?}
    Returns: {upload_id, chunk_size, received[]}
    """
    _prune_stale_pending_uploads()
    data = request.get_json(silent=True) or {}
    filename = str(data.get("filename") or "").strip()
    if not filename or not allowed_file(filename):
        return jsonify({
            "error": f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXT))}",
        }), 400
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size < 1:
        return jsonify({"error": "Missing or invalid file size"}), 400
    limit = int(app.config.get("MAX_CONTENT_LENGTH") or 0)
    if limit and size > limit:
        limit_mb = limit // (1024 * 1024)
        return jsonify({
            "error": (
                f"File is {size // (1024 * 1024)} MB; max is ~{limit_mb} MB. "
                "Export a smaller H.264 MP4 (1080p is usually enough)."
            ),
        }), 413

    ext = filename.rsplit(".", 1)[1].lower()
    upload_id = uuid.uuid4().hex
    dest = _pending_upload_dir(upload_id)
    dest.mkdir(parents=True, exist_ok=True)
    pre_clean = str(data.get("pre_clean") or "").lower() in ("1", "true", "yes")
    meta = {
        "upload_id": upload_id,
        "filename": filename,
        "ext": ext,
        "size": size,
        "pre_clean": pre_clean,
        "chunk_size": UPLOAD_CHUNK_SIZE,
        "created_at": time.time(),
        "received": {},  # index -> bytes written
    }
    with _pending_uploads_lock:
        _pending_uploads[upload_id] = meta
    (dest / "meta.json").write_text(json.dumps({
        k: v for k, v in meta.items() if k != "received"
    }), encoding="utf-8")
    print(
        f"[upload] init {upload_id} size={size} name={filename!r} chunk={UPLOAD_CHUNK_SIZE}",
        flush=True,
    )
    return jsonify({
        "upload_id": upload_id,
        "chunk_size": UPLOAD_CHUNK_SIZE,
        "received": [],
    })


@app.route("/upload/chunk/<upload_id>", methods=["PUT", "POST"])
def upload_chunk(upload_id: str):
    """Accept one binary chunk. Headers: X-Chunk-Index, X-Chunk-Bytes (optional)."""
    with _pending_uploads_lock:
        meta = _pending_uploads.get(upload_id)
    if not meta:
        # Recover from process restart via meta.json on disk
        dest = _pending_upload_dir(upload_id)
        meta_path = dest / "meta.json"
        if not meta_path.exists():
            return jsonify({"error": "Unknown or expired upload session — start again"}), 404
        try:
            disk = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return jsonify({"error": "Corrupt upload session — start again"}), 400
        meta = {
            **disk,
            "received": {},
            "created_at": float(disk.get("created_at") or time.time()),
        }
        for part in dest.glob("part_*"):
            try:
                idx = int(part.name.split("_", 1)[1])
                meta["received"][idx] = part.stat().st_size
            except (ValueError, OSError):
                continue
        with _pending_uploads_lock:
            _pending_uploads[upload_id] = meta

    try:
        index = int(request.headers.get("X-Chunk-Index", request.args.get("index", -1)))
    except (TypeError, ValueError):
        index = -1
    if index < 0:
        return jsonify({"error": "Missing X-Chunk-Index"}), 400

    body = request.get_data(cache=False)
    if not body:
        return jsonify({"error": "Empty chunk body"}), 400

    expected = request.headers.get("X-Chunk-Bytes") or request.args.get("bytes")
    if expected is not None:
        try:
            if len(body) != int(expected):
                return jsonify({
                    "error": f"Chunk size mismatch: got {len(body)} expected {expected}",
                }), 400
        except (TypeError, ValueError):
            pass

    dest = _pending_upload_dir(upload_id)
    dest.mkdir(parents=True, exist_ok=True)
    part_path = dest / f"part_{index:05d}"
    part_path.write_bytes(body)
    with _pending_uploads_lock:
        meta = _pending_uploads.get(upload_id) or meta
        meta.setdefault("received", {})[index] = len(body)
        _pending_uploads[upload_id] = meta
        received_bytes = sum(meta["received"].values())
        total = int(meta.get("size") or 0)
    return jsonify({
        "ok": True,
        "index": index,
        "received_bytes": received_bytes,
        "total_bytes": total,
        "pct": round(100.0 * received_bytes / total, 1) if total else None,
    })


@app.route("/upload/finish/<upload_id>", methods=["POST"])
def upload_finish(upload_id: str):
    """Assemble chunks → validate → start transcription (same payload as /transcribe-only)."""
    with _pending_uploads_lock:
        meta = _pending_uploads.get(upload_id)
    dest = _pending_upload_dir(upload_id)
    if not meta:
        meta_path = dest / "meta.json"
        if meta_path.exists():
            try:
                disk = json.loads(meta_path.read_text(encoding="utf-8"))
                meta = {**disk, "received": {}}
                for part in dest.glob("part_*"):
                    try:
                        idx = int(part.name.split("_", 1)[1])
                        meta["received"][idx] = part.stat().st_size
                    except (ValueError, OSError):
                        continue
            except Exception:
                meta = None
    if not meta:
        return jsonify({"error": "Unknown or expired upload session — start again"}), 404

    size = int(meta.get("size") or 0)
    chunk_size = int(meta.get("chunk_size") or UPLOAD_CHUNK_SIZE)
    n_chunks = max(1, (size + chunk_size - 1) // chunk_size)
    received = dict(meta.get("received") or {})
    for part in dest.glob("part_*"):
        try:
            idx = int(part.name.split("_", 1)[1])
            received[idx] = part.stat().st_size
        except (ValueError, OSError):
            continue

    missing = [i for i in range(n_chunks) if i not in received]
    if missing:
        return jsonify({
            "error": f"Missing {len(missing)} chunk(s) — resume upload",
            "missing": missing[:40],
            "received": sorted(received.keys()),
        }), 400

    got = sum(received.values())
    if size and got != size:
        return jsonify({
            "error": f"Assembled size {got} != declared {size}. Re-upload.",
        }), 400

    ext = str(meta.get("ext") or "mp4").lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"error": "Unsupported extension"}), 400
    job_id = uuid.uuid4().hex
    video_path = UPLOAD_DIR / f"{job_id}.{ext}"
    try:
        with open(video_path, "wb") as out:
            for i in range(n_chunks):
                part = dest / f"part_{i:05d}"
                out.write(part.read_bytes())
    except OSError as e:
        _safe_unlink(video_path)
        return jsonify({"error": f"Could not assemble upload: {e}"}), 500

    try:
        shutil.rmtree(dest, ignore_errors=True)
    except OSError:
        pass
    with _pending_uploads_lock:
        _pending_uploads.pop(upload_id, None)

    filename = str(meta.get("filename") or f"upload.{ext}")
    pre_clean = bool(meta.get("pre_clean"))
    try:
        job_id, payload = _start_transcribe_job(
            video_path, filename, pre_clean=pre_clean,
            expected_bytes=size, job_id=job_id,
        )
    except Exception as e:
        _safe_unlink(video_path)
        print(f"[upload] finish {upload_id} rejected: {e}", flush=True)
        return jsonify({"error": str(e)}), 400
    payload["chunked"] = True
    return jsonify(payload)


@app.route("/upload/status/<upload_id>", methods=["GET"])
def upload_status(upload_id: str):
    """Which chunks are already on the server (for resume after network blip)."""
    with _pending_uploads_lock:
        meta = _pending_uploads.get(upload_id)
    dest = _pending_upload_dir(upload_id)
    if not meta and not dest.exists():
        return jsonify({"error": "Unknown upload"}), 404
    received = dict((meta or {}).get("received") or {})
    for part in dest.glob("part_*"):
        try:
            idx = int(part.name.split("_", 1)[1])
            received[idx] = part.stat().st_size
        except (ValueError, OSError):
            continue
    size = int((meta or {}).get("size") or 0)
    got = sum(received.values())
    return jsonify({
        "upload_id": upload_id,
        "received": sorted(int(k) for k in received.keys()),
        "received_bytes": got,
        "total_bytes": size,
        "chunk_size": int((meta or {}).get("chunk_size") or UPLOAD_CHUNK_SIZE),
        "filename": (meta or {}).get("filename"),
    })


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
    pre_clean = request.form.get("pre_clean", "").lower() in ("1", "true", "yes")
    try:
        job_id, payload = _start_transcribe_job(
            video_path, f.filename, pre_clean=pre_clean,
            expected_bytes=expected_bytes, job_id=job_id,
        )
    except Exception as e:
        _safe_unlink(video_path)
        print(f"[upload] {job_id} rejected after save: {e}", flush=True)
        return jsonify({"error": str(e)}), 400
    return jsonify(payload)


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
        deps = _probe_analyze_deps()
        return jsonify({
            "error": msg,
            "deps": {
                "hf_token": deps["hf_token"],
                "pyannote": deps["pyannote"],
                "mediapipe": deps["mediapipe"],
                "diarization_ok": deps["diarization_ok"],
                "faces_ok": deps["faces_ok"],
            },
        }), 400

    deps = _probe_analyze_deps()
    faces_note = None
    if not deps["faces_ok"]:
        faces_note = (
            "Speaker diarization will run; face/reframe crops skipped "
            f"({deps.get('faces_error') or 'mediapipe unavailable'})."
        )

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
        "faces_ok": deps["faces_ok"],
        "faces_note": faces_note,
    })


@app.route("/analyze-deps", methods=["GET"])
def analyze_deps_endpoint():
    """Diagnostic: what Analyze can run in this process (HF / pyannote / mediapipe)."""
    deps = _probe_analyze_deps()
    return jsonify({
        "ok": deps["diarization_ok"],
        "hf_token_present": deps["hf_token"],
        "diarization_ok": deps["diarization_ok"],
        "faces_ok": deps["faces_ok"],
        "pyannote": deps["pyannote"],
        "mediapipe": deps["mediapipe"],
        "error": deps["error"],
        "faces_error": deps["faces_error"],
        "diarization_device": _diarization_device_resolved or DIARIZATION_DEVICE,
        "diarization_model": DIARIZATION_MODEL,
    })


def _speaker_label_for_id(spk: str, index_fallback: int = 0) -> str:
    """Stable Host/Guest labels by SPEAKER_NN id (not sort order)."""
    m = re.match(r"SPEAKER_(\d+)$", str(spk or "").strip(), re.I)
    if m:
        n = int(m.group(1))
        if n == 0:
            return "Host"
        if n == 1:
            return "Guest"
        return f"Speaker {n + 1}"
    labels = ("Host", "Guest", "Speaker 3", "Speaker 4", "Speaker 5")
    if 0 <= index_fallback < len(labels):
        return labels[index_fallback]
    return f"Speaker {index_fallback + 1}"


def _speaker_sort_key(spk: str) -> tuple:
    m = re.match(r"SPEAKER_(\d+)$", str(spk or "").strip(), re.I)
    if m:
        return (0, int(m.group(1)))
    return (1, str(spk or ""))


def _speaker_breakdown(diar: list) -> list:
    """Per-speaker speech seconds + % for Ingest / Timeline cards.

    Labels are tied to SPEAKER_00 → Host, SPEAKER_01 → Guest, etc., so caption
    colors stay consistent across multi-interview Main clips.
    """
    totals: dict[str, float] = {}
    for seg in diar or []:
        spk = seg.get("speaker") or "UNKNOWN"
        try:
            dur = max(0.0, float(seg.get("end", 0)) - float(seg.get("start", 0)))
        except (TypeError, ValueError):
            dur = 0.0
        totals[spk] = totals.get(spk, 0.0) + dur
    total = sum(totals.values()) or 1.0
    out = []
    for i, spk in enumerate(sorted(totals.keys(), key=_speaker_sort_key)):
        sec = totals[spk]
        out.append({
            "id": spk,
            "label": _speaker_label_for_id(spk, i),
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
        # Same repair-before-reject path as /transcribe-only — a container
        # ffprobe can't read at all is often salvageable with a remux.
        pre_probe = _probe_media_streams(staging)
        if pre_probe.get("error") and not pre_probe.get("has_video") and not pre_probe.get("has_audio"):
            repaired = _repair_uploaded_media(staging)
            if repaired:
                staging = repaired
                ext = staging.suffix.lstrip(".").lower()
                expected_bytes = None
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
    """Force an attachment download (helps mobile browsers that otherwise
    inline-play and never offer Save)."""
    safe = Path(filename).name
    resp = send_from_directory(OUTPUT_DIR, safe, as_attachment=True, download_name=safe)
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
ASSET_EXT_STILL = {"jpg", "jpeg", "png", "webp", "bmp"}
ASSET_EXT_GIF = {"gif"}
ASSET_EXT_IMAGE = ASSET_EXT_STILL | ASSET_EXT_GIF
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
    if ext in ASSET_EXT_GIF:
        return "gif"
    if ext in ASSET_EXT_STILL:
        return "image"
    if ext in ASSET_EXT_AUDIO:
        return "audio"
    return None


def _is_gif_path(path: Path | None) -> bool:
    return bool(path) and path.suffix.lower().lstrip(".") in ASSET_EXT_GIF


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


def _timeline_clip_source(clip: dict, prefer_proxy: bool = True) -> Path | None:
    """Resolve a timeline clip's media: either a source job's video or an asset.

    For transcribed jobs, prefer the H.264 edit proxy when present — iPhone MOV
    originals often carry mebx metadata streams that break filtergraphs using
    `[N:v]` during overlay composite.
    """
    sid = clip.get("source_job_id")
    if sid and sid in jobs:
        if prefer_proxy:
            proxy = _edit_proxy_path(sid)
            try:
                if proxy.exists() and proxy.stat().st_size > 64:
                    return proxy
            except OSError:
                pass
        return find_video_path(sid)
    aid = clip.get("asset_id")
    if aid:
        return _find_asset_path(aid)
    return None


def _tl_ensure_overlay_video(path: Path, job_id: str | None = None) -> Path | None:
    """Return a path FFmpeg can read as a real video stream for overlays.

    If *path* has no video (or is a brittle phone MOV / HEVC), fall back to the
    job's edit proxy or a short one-off H.264 remux in CACHE_DIR.

    iPhone QuickTime files often carry ``mebx`` timed-metadata streams that make
    filtergraph inputs like ``[N:v]`` fail even when a video stream exists.
    """
    if not path or not path.exists():
        return None
    # Already a cleaned overlay/proxy mp4 — trust it.
    name = path.name.lower()
    if "_editproxy" in name or name.startswith("ovsrc_"):
        probe_fast = _probe_media_streams(path)
        return path if probe_fast.get("has_video") else None

    probe = _probe_media_streams(path)
    fmt = (probe.get("format_name") or "").lower()
    is_qt = path.suffix.lower() in (".mov", ".qt") or "mov" in fmt or "quicktime" in fmt
    needs_safe = (
        not probe.get("has_video")
        or probe.get("is_hevc")
        or is_qt
    )
    if probe.get("has_video") and not needs_safe:
        return path
    if job_id:
        proxy = _edit_proxy_path(job_id)
        try:
            if proxy.exists() and proxy.stat().st_size > 64:
                return proxy
        except OSError:
            pass
    # One-off remux/transcode for overlay use (small PiP — speed over quality).
    try:
        digest = hashlib.md5(f"{path.resolve()}:{path.stat().st_mtime}".encode()).hexdigest()[:16]
    except OSError:
        return path if probe.get("has_video") else None
    out = CACHE_DIR / f"ovsrc_{digest}.mp4"
    if out.exists() and out.stat().st_size > 64:
        return out
    proc = subprocess.run(
        [
            FFMPEG, "-y",
            "-dn", "-sn",
            "-i", str(path),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            str(out),
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 64:
        _safe_unlink(out)
        return path if probe.get("has_video") else None
    return out


def _tl_audio_wants_enhance(audio: dict | None) -> bool:
    """True when Look → Audio Enhancement should run on Timeline Render."""
    if not isinstance(audio, dict) or not audio:
        return False
    provider = str(audio.get("provider") or "ffmpeg").lower()
    try:
        offset = abs(float(audio.get("offset_seconds") or 0))
    except (TypeError, ValueError):
        offset = 0.0
    if offset >= 0.01:
        return True
    if provider in ("auphonic", "elevenlabs", "dolby"):
        return True
    return bool(build_audio_filter_chain(audio))


def _tl_passthrough_copy(video: Path, out_path: Path) -> bool:
    """Best-effort copy of `video` to `out_path` so a render can still finish.

    Tries a plain filesystem copy first (fastest, preserves container as-is),
    then falls back to an ffmpeg stream copy if the destination needs a
    different filename/container to satisfy downstream expectations.
    """
    try:
        if out_path.resolve() == video.resolve():
            return True
    except OSError:
        pass
    try:
        shutil.copyfile(video, out_path)
        return out_path.exists() and out_path.stat().st_size > 0
    except OSError:
        pass
    try:
        proc = subprocess.run(
            [FFMPEG, "-y", "-i", str(video), "-c", "copy", "-movflags", "+faststart", str(out_path)],
            capture_output=True, text=True,
        )
        return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0
    except OSError:
        return False


def _tl_apply_project_audio(video: Path, audio: dict, out_path: Path, job_id: str) -> None:
    """Apply Caption-look audio enhancement onto a finished timeline video.

    This must never blow up Timeline Render: any failure along the way is
    logged and the function falls back to passing the original (pre-audio)
    video straight through to `out_path` so the render can still complete.
    """
    provider = str((audio or {}).get("provider") or "ffmpeg").lower()
    enhanced = UPLOAD_DIR / f"{job_id}_tlaudio.m4a"
    try:
        try:
            if provider == "auphonic":
                if not os.environ.get("AUPHONIC_API_KEY"):
                    raise RuntimeError("Auphonic is not configured (AUPHONIC_API_KEY not set).")
                enhance_with_auphonic(video, enhanced, audio)
            elif provider == "elevenlabs":
                if not os.environ.get("ELEVENLABS_API_KEY"):
                    raise RuntimeError("ElevenLabs is not configured (ELEVENLABS_API_KEY not set).")
                enhance_with_elevenlabs(video, enhanced, audio)
            elif provider == "dolby":
                if not os.environ.get("DOLBY_API_KEY"):
                    raise RuntimeError("Dolby is not configured (DOLBY_API_KEY not set).")
                enhance_with_dolby(video, enhanced, audio)
            else:
                af = build_audio_filter_chain(audio)
                if af:
                    apply_audio_enhancements(video, enhanced, af)
                else:
                    # Offset-only: extract clean AAC (in an mp4/m4a container) then shift below.
                    proc = subprocess.run(
                        [FFMPEG, "-y", "-i", str(video), "-vn", "-c:a", "aac", "-b:a", "192k",
                         str(enhanced)],
                        capture_output=True, text=True,
                    )
                    if proc.returncode != 0 or not enhanced.exists():
                        raise RuntimeError(f"Audio extract failed: {(proc.stderr or '')[-400:]}")

            if provider in ("auphonic", "elevenlabs"):
                _apply_isolation_postprocess(enhanced, video, audio)

            try:
                offset_sec = float(audio.get("offset_seconds", 0) or 0)
            except (TypeError, ValueError):
                offset_sec = 0.0
            if abs(offset_sec) >= 0.01 and enhanced.exists():
                adjusted = enhanced.with_suffix(".offset.m4a")
                if offset_sec > 0:
                    cmd = [FFMPEG, "-y", "-i", str(enhanced), "-ss", f"{offset_sec:.3f}",
                           "-c:a", "aac", "-b:a", "192k", str(adjusted)]
                else:
                    delay_ms = int(round(abs(offset_sec) * 1000))
                    cmd = [FFMPEG, "-y", "-i", str(enhanced),
                           "-af", f"adelay={delay_ms}|{delay_ms}",
                           "-c:a", "aac", "-b:a", "192k", str(adjusted)]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode == 0 and adjusted.exists() and adjusted.stat().st_size > 0:
                    _safe_unlink(enhanced)
                    adjusted.rename(enhanced)
                else:
                    _safe_unlink(adjusted)

            if not enhanced.exists() or enhanced.stat().st_size < 64:
                raise RuntimeError("Audio enhancement produced no usable audio.")

            # Prefer an explicit video duration over -shortest: short enhanced
            # audio + -shortest + -c:v copy can drop every video packet.
            try:
                vid_dur = float(_media_duration(video) or _ffprobe_duration(video) or 0.0)
            except Exception:
                vid_dur = 0.0

            mux_attempts = [
                [FFMPEG, "-y", "-i", str(video), "-i", str(enhanced),
                 "-map", "0:v:0", "-map", "1:a:0?",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 *([ "-t", f"{vid_dur:.3f}" ] if vid_dur > 0.05 else []),
                 "-movflags", "+faststart", str(out_path)],
                [FFMPEG, "-y", "-i", str(video), "-i", str(enhanced),
                 "-map", "0:v:0", "-map", "1:a:0?",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", str(out_path)],
                [FFMPEG, "-y", "-i", str(video), "-i", str(enhanced),
                 "-map", "0:v:0", "-map", "1:a:0?",
                 *_VIDEO_ENC_ARGS, "-c:a", "aac", "-b:a", "192k",
                 *([ "-t", f"{vid_dur:.3f}" ] if vid_dur > 0.05 else []),
                 "-movflags", "+faststart", str(out_path)],
            ]
            mux_err = ""
            muxed = False
            for cmd in mux_attempts:
                if out_path.exists():
                    _safe_unlink(out_path)
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1024:
                    try:
                        _assert_mp4_has_video(out_path, "audio enhance mux")
                        muxed = True
                        break
                    except RuntimeError as ve:
                        mux_err = str(ve)
                        _safe_unlink(out_path)
                        continue
                mux_err = (proc.stderr or "")[-800:]
            if not muxed:
                raise RuntimeError(f"Mux enhanced audio failed: {mux_err}")
        except Exception as err:
            print(f"[timeline] Audio enhance skipped: {err}")
            if out_path.exists():
                _safe_unlink(out_path)
            if not _tl_passthrough_copy(video, out_path):
                raise
    finally:
        _safe_unlink(enhanced)
        _safe_unlink(enhanced.with_suffix(".offset.m4a"))


def _tl_run(cmd: list, what: str, job_id: str | None = None,
            progress_lo: int | None = None, progress_hi: int | None = None,
            duration_hint: float | None = None) -> None:
    """Run an ffmpeg command, raising a trimmed error on failure."""
    if not _run_ffmpeg_encode(
        cmd,
        what=what,
        job_id=job_id,
        progress_lo=progress_lo,
        progress_hi=progress_hi,
        duration_hint=duration_hint,
    ):
        raise RuntimeError(f"{what} failed (see ffmpeg_render.log)")


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


_PUNCH_PEAK = {
    "low": 1.15, "med": 1.25, "high": 1.40, "strong": 1.40,
    # "Hold" zooms: snap to the target scale and stay there for the whole
    # clip/effect duration instead of decaying back to 1.0 (see
    # _PUNCH_HOLD_INTENSITIES below).
    "1.5x": 1.5, "2x": 2.0, "hold_1_5": 1.5, "hold_2": 2.0,
}
PUNCH_DECAY_SECONDS = 0.45
# Intensities that hold at their target scale for the whole duration rather
# than easing back down to 1.0 — used by the 1.5x / 2x "Zoom hold" effects.
_PUNCH_HOLD_INTENSITIES = {"1.5x", "2x", "hold_1_5", "hold_2"}


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
    if punch_cfg.get("intensity") in _PUNCH_HOLD_INTENSITIES:
        # Hold zoom: snap to the target scale at the hit and stay there for
        # the rest of the clip — no ease-back-to-1.0 decay.
        z = f"if(gte(in,{f0:.3f}),{peak:.4f},1)"
    else:
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

    Still-image assets (Main cutaways) are looped for the requested duration.
    Uses an MD5 hash cache to skip FFmpeg re-encoding for unchanged segments,
    speeding up timeline updates by up to 30x.
    """
    dur = max(0.05, t_out - t_in)
    ext = src.suffix.lower().lstrip(".")
    is_gif = ext in ASSET_EXT_GIF
    is_image = ext in ASSET_EXT_STILL

    # Calculate unique segment cache key
    mtime = src.stat().st_mtime if src.exists() else 0
    cache_raw = (
        f"{src.resolve()}_{mtime}_{t_in:.3f}_{t_out:.3f}_{W}_{H}_{fps}_{fit}_{bg}_"
        f"img={int(is_image)}_gif={int(is_gif)}_"
        f"{json.dumps(ken, sort_keys=True) if ken else ''}_"
        f"{json.dumps(color, sort_keys=True) if color else ''}_"
        f"{json.dumps(punch, sort_keys=True) if punch else ''}"
    )
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
    # Skip Ken Burns on GIFs — motion lives in the animation itself.
    kb = None if is_gif else _tl_kenburns_filter(ken, W, H, fps, dur)
    if kb:
        vf += "," + kb
    pz = _tl_punch_zoom_filter(punch, W, H, fps)
    if pz:
        vf += "," + pz
    cf = _tl_color_filter(color)
    if cf:
        vf += "," + cf
    vf += ",format=yuv420p"

    if is_gif:
        cmd = [
            FFMPEG, "-y",
            "-ignore_loop", "0", "-stream_loop", "-1",
            "-i", str(src),
            "-f", "lavfi", "-t", f"{dur:.3f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", f"{dur:.3f}", "-vf", vf, *_VIDEO_ENC_ARGS,
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest",
            str(out_path),
        ]
        _tl_run(cmd, "GIF cutaway")
    elif is_image:
        # Main cutaway from a still: loop the frame, silent audio bed.
        cmd = [
            FFMPEG, "-y",
            "-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", str(src),
            "-f", "lavfi", "-t", f"{dur:.3f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-vf", vf, *_VIDEO_ENC_ARGS,
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest",
            "-t", f"{dur:.3f}", str(out_path),
        ]
        _tl_run(cmd, "Still cutaway")
    else:
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

    srcB may be a still image or GIF — stills are looped for *dur*; GIFs are
    stream-looped. Audio is taken from source A. Each half is center-cropped
    to fill its panel. An optional *color* grade is applied to the combined frame.
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

    ext_b = srcB.suffix.lower().lstrip(".")
    is_still = ext_b in ASSET_EXT_STILL
    is_gif = ext_b in ASSET_EXT_GIF

    def panel(idx):
        return (f"[{idx}:v:0]scale={pw}:{ph}:force_original_aspect_ratio=increase,"
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
           "-ss", f"{inA:.3f}", "-i", str(srcA)]
    if is_still:
        # Loop the still for the full segment — seeking into a photo is a no-op.
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", str(srcB)]
    elif is_gif:
        cmd += ["-ignore_loop", "0", "-stream_loop", "-1", "-i", str(srcB)]
    else:
        cmd += ["-ss", f"{inB:.3f}", "-i", str(srcB)]
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
    # Optional free height (on-stage stretch). Otherwise keep aspect via -2.
    lh = None
    if logo.get("h") is not None:
        try:
            hfrac = min(1.0, max(0.03, float(logo.get("h"))))
            lh = max(2, int(H * hfrac) // 2 * 2)
        except (TypeError, ValueError):
            lh = None
    if lh:
        scale = f"scale={lw}:{lh}:force_original_aspect_ratio=disable"
    else:
        scale = f"scale={lw}:-2"
    prep = f"[1:v:0]{scale},setsar=1,format=yuva420p,colorchannelmixer=aa={opacity:.3f}[lg]"
    fc = f"{prep};[0:v:0][lg]overlay=x={x}:y={y}:shortest=1[v]"
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
            max_tdur = min(0.8, cur_dur * 0.45, nxt_dur * 0.45)
            is_hard_cut = not tname
            if tname and (max_tdur < 0.12 or cur_dur < 0.28 or nxt_dur < 0.28):
                # Segments too short for a real crossfade — fall back to a
                # hard cut instead of forcing a degenerate xfade duration.
                print(f"[timeline] Transition {i}: segments too short — hard cut", flush=True)
                is_hard_cut = True
            if not is_hard_cut:
                xfade = TIMELINE_TRANSITIONS.get(str(tname), "fade")
                tdur = max(0.12, max_tdur)
                offset = max(0.0, cur_dur - tdur)
                fc = (
                    f"[0:v][1:v]xfade=transition={xfade}:duration={tdur:.3f}:"
                    f"offset={offset:.3f},format=yuv420p[v];"
                    f"[0:a][1:a]acrossfade=d={tdur:.3f}[a]"
                )
                new_seg_start = offset
                new_cur_dur = cur_dur + nxt_dur - tdur
            else:
                fc = "[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[v][a]"
                new_seg_start = cur_dur
                new_cur_dur = cur_dur + nxt_dur
            try:
                _tl_run(
                    [FFMPEG, "-y", "-i", str(cur_path), "-i", str(nxt_path),
                     "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                     *_VIDEO_ENC_ARGS, "-c:a", "aac", "-b:a", "192k", str(step_out)],
                    f"Transition {i}",
                )
            except RuntimeError:
                if is_hard_cut:
                    raise
                print(f"[timeline] Transition {i} failed — retrying hard cut", flush=True)
                fc = "[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[v][a]"
                new_seg_start = cur_dur
                new_cur_dur = cur_dur + nxt_dur
                _tl_run(
                    [FFMPEG, "-y", "-i", str(cur_path), "-i", str(nxt_path),
                     "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                     *_VIDEO_ENC_ARGS, "-c:a", "aac", "-b:a", "192k", str(step_out)],
                    f"Transition {i} (hard cut retry)",
                )
            seg_starts.append(new_seg_start)
            cur_dur = new_cur_dur
            intermediates.append(step_out)
            cur_path = step_out
        _tl_run([FFMPEG, "-y", "-i", str(cur_path), "-c", "copy",
                 "-movflags", "+faststart", str(out_path)], "Finalize main")
        return cur_dur, seg_starts
    finally:
        for p in intermediates:
            _safe_unlink(p)


def _ensure_ui_sfx_assets() -> dict[str, Path]:
    """Generate short click/whoosh WAVs once (no binary assets in git)."""
    sfx_dir = CACHE_DIR / "sfx"
    sfx_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    specs = {
        # Soft high click — pairs with punch / badge-like pops
        "click": (
            ["-f", "lavfi", "-i", "sine=frequency=1650:duration=0.06"],
            "afade=t=out:st=0.015:d=0.045,volume=0.85",
        ),
        # Pink-noise whoosh — pairs with B-roll / split / Ken Burns arrivals
        "whoosh": (
            ["-f", "lavfi", "-i", "anoisesrc=d=0.38:color=pink:sample_rate=44100"],
            "afade=t=in:st=0:d=0.035,afade=t=out:st=0.22:d=0.14,"
            "highpass=f=220,lowpass=f=7200,volume=0.72",
        ),
    }
    for name, (src, af) in specs.items():
        # Bump generation stamp so louder assets replace quiet cached WAVs.
        path = sfx_dir / f"{name}_v2.wav"
        if path.exists() and path.stat().st_size > 200:
            out[name] = path
            continue
        cmd = [FFMPEG, "-y", *src, "-af", af, "-ar", "44100", "-ac", "2", str(path)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and path.exists():
            out[name] = path
        else:
            print(f"[sfx] failed to build {name}: {(proc.stderr or '')[-200:]}", flush=True)
    return out


def _tl_input_has_audio(path: Path) -> bool:
    try:
        return _has_audio_stream(path)
    except Exception:
        return False


def _tl_require_video(path: Path, what: str) -> float:
    """Ensure *path* has a video track; return its duration (seconds)."""
    probe = _probe_media_streams(path)
    if not probe.get("has_video"):
        raise RuntimeError(f"{what}: input has no video track.")
    dur = float(probe.get("duration") or 0.0)
    if dur <= 0.05:
        dur = float(_media_duration(path) or 0.0)
    if dur <= 0.05:
        try:
            dur = float(_ffprobe_duration(path) or 0.0)
        except Exception:
            dur = 0.0
    if dur <= 0.05:
        raise RuntimeError(f"{what}: could not read video duration.")
    return dur


def _tl_mux_filtered_audio(
    base: Path,
    inputs: list[str],
    filter_complex: str,
    out_path: Path,
    what: str,
    duration: float,
) -> None:
    """Map filtered audio onto *base* while keeping the full video track.

    Avoids `-shortest` with `-c:v copy`, which can emit audio-only MP4s when
    the filtered audio ends before the first video packet is copied.
    """
    t_args = ["-t", f"{duration:.3f}"]
    copy_cmd = [
        FFMPEG, "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        *t_args,
        "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        _tl_run(copy_cmd, what)
        _assert_mp4_has_video(out_path, what)
        return
    except Exception as copy_err:
        print(f"[{what}] stream-copy mux failed ({copy_err}); re-encoding video", flush=True)
        _safe_unlink(out_path)

    reenc_cmd = [
        FFMPEG, "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "0:v:0", "-map", "[aout]",
        *_VIDEO_ENC_ARGS,
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        *t_args,
        "-movflags", "+faststart",
        str(out_path),
    ]
    _tl_run(reenc_cmd, f"{what} (re-encode)")
    _assert_mp4_has_video(out_path, what)


def _tl_mix_overlay_sfx(base: Path, overlay_clips: list, effect_clips: list,
                        out_path: Path) -> None:
    """Mix auto click/whoosh one-shots at overlay / effect onsets.

    Captions-style sensory sync: every visual pop gets a tiny audio accent.
    Voice stays primary; SFX are short but clearly audible on Render.
    """
    assets = _ensure_ui_sfx_assets()
    if not assets:
        _tl_run([FFMPEG, "-y", "-i", str(base), "-c", "copy", str(out_path)],
                "SFX passthrough")
        return

    hits: list[tuple[float, str]] = []
    for ov in overlay_clips or []:
        if not isinstance(ov, dict):
            continue
        try:
            st = float(ov.get("start", 0))
        except (TypeError, ValueError):
            continue
        kind = "whoosh"
        src = str(ov.get("source") or "").lower()
        if src in ("badge",):
            kind = "click"
        hits.append((max(0.0, st), kind))
    for fx in effect_clips or []:
        if not isinstance(fx, dict):
            continue
        ftype = str(fx.get("type") or "")
        try:
            st = float(fx.get("start", 0))
        except (TypeError, ValueError):
            continue
        if ftype in ("punch_zoom", "zoom_1_5", "zoom_2x", "split_screen"):
            hits.append((max(0.0, st), "click" if ftype.startswith("zoom") or ftype == "punch_zoom" else "whoosh"))
        elif ftype == "ken_burns":
            hits.append((max(0.0, st), "whoosh"))

    # Dedupe near-simultaneous hits (keep first)
    hits.sort(key=lambda h: h[0])
    cleaned: list[tuple[float, str]] = []
    for t, k in hits:
        if cleaned and abs(t - cleaned[-1][0]) < 0.12:
            continue
        cleaned.append((t, k))
    cleaned = cleaned[:48]
    if not cleaned:
        _tl_run([FFMPEG, "-y", "-i", str(base), "-c", "copy", str(out_path)],
                "SFX passthrough")
        return

    duration = _tl_require_video(base, "Overlay SFX mix")

    # Base with no audio track → silent bed so adelay mix still works.
    inputs: list[str] = ["-i", str(base)]
    if _tl_input_has_audio(base):
        filt = ["[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=1.0[voice]"]
        next_i = 1
    else:
        inputs += [
            "-f", "lavfi", "-t", f"{duration:.3f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
        filt = ["[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=0.01[voice]"]
        next_i = 2

    labels = []
    for t, kind in cleaned:
        path = assets.get(kind) or assets.get("whoosh") or assets.get("click")
        if not path:
            continue
        inputs += ["-i", str(path)]
        delay = int(t * 1000)
        # Bound apad to the video length — bare apad is infinite and pairs
        # badly with -shortest / stream-copy muxes.
        filt.append(
            f"[{next_i}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"volume=1.35,adelay={delay}|{delay},apad=whole_dur={duration:.3f}[s{next_i}]"
        )
        labels.append(f"[s{next_i}]")
        next_i += 1
    if not labels:
        _tl_run([FFMPEG, "-y", "-i", str(base), "-c", "copy", str(out_path)],
                "SFX passthrough")
        return
    if len(labels) == 1:
        filt.append(f"{labels[0]}anull[sfxall]")
    else:
        filt.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0:dropout_transition=0[sfxall]"
        )
    # Prefer hearing the accent; voice still dominates after amix.
    filt.append("[sfxall]volume=1.25[sfxboost]")
    filt.append(
        "[voice][sfxboost]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
    )
    print(f"[sfx] mixing {len(labels)} hits onto {base.name}", flush=True)
    _tl_mux_filtered_audio(
        base, inputs, ";".join(filt), out_path, "Overlay SFX mix", duration,
    )


def _tl_mix_music(base: Path, music_clips: list, out_path: Path) -> None:
    """Mix background music tracks into *base*'s audio (video stream preserved).

    Each clip is trimmed, gain-staged and time-shifted to its start. If any
    clip requests ducking, all music is side-chain compressed against the main
    voice so speech stays on top.
    """
    resolved = []
    for m in music_clips:
        # Direct path (Caption Look / Instant Export bg music fallback)
        raw = m.get("_path") or m.get("path")
        if raw:
            path = Path(str(raw))
            if path.exists():
                resolved.append((m, path))
                continue
        path = _timeline_clip_source(m)
        if path:
            resolved.append((m, path))
    if not resolved:
        # Nothing usable — just pass the base through.
        _tl_run([FFMPEG, "-y", "-i", str(base), "-c", "copy", str(out_path)],
                "Music passthrough")
        return

    duration = _tl_require_video(base, "Music mix")

    inputs: list[str] = ["-i", str(base)]
    if _tl_input_has_audio(base):
        filt = ["[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,asplit=2[voice][key]"]
        next_i = 1
    else:
        inputs += [
            "-f", "lavfi", "-t", f"{duration:.3f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
        filt = ["[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,asplit=2[voice][key]"]
        next_i = 2

    music_labels = []
    duck = False
    for m, path in resolved:
        inputs += ["-i", str(path)]
        m_in = max(0.0, float(m.get("in", 0)))
        try:
            m_out = float(m.get("out", m_in + 30))
        except (TypeError, ValueError):
            m_out = m_in + 30
        if m_out <= m_in:
            # Probe real duration when out was missing / zero.
            clip_dur = _media_duration(path)
            m_out = m_in + (clip_dur if clip_dur > 0.5 else 60.0)
        start = max(0.0, float(m.get("start", 0)))
        try:
            gain = float(m.get("gain_db", -12))
        except (TypeError, ValueError):
            gain = -12.0
        # Floor so music is actually audible under speech.
        gain = max(-24.0, min(0.0, gain))
        delay = int(start * 1000)
        if m.get("duck", True):
            duck = True
        filt.append(
            f"[{next_i}:a]aformat=sample_fmts=fltp:sample_rates=44100:"
            f"channel_layouts=stereo,"
            f"atrim=start={m_in:.3f}:end={m_out:.3f},"
            f"asetpts=PTS-STARTPTS,volume={gain:.2f}dB,"
            f"adelay={delay}|{delay},apad=whole_dur={duration:.3f}[m{next_i}]"
        )
        music_labels.append(f"[m{next_i}]")
        next_i += 1

    if len(music_labels) == 1:
        filt.append(f"{music_labels[0]}anull[musicall]")
    else:
        filt.append(f"{''.join(music_labels)}amix=inputs={len(music_labels)}:"
                    f"duration=longest:normalize=0:dropout_transition=0[musicall]")

    if duck:
        # Compress MUSIC keyed by speech (not the other way around).
        filt.append(
            "[musicall][key]sidechaincompress=threshold=0.02:ratio=6:"
            "attack=15:release=350:makeup=1[musicfinal]"
        )
    else:
        filt.append("[musicall]anull[musicfinal]")

    filt.append(
        "[voice][musicfinal]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]"
    )
    print(f"[music] mixing {len(music_labels)} bed(s) duck={duck}", flush=True)

    _tl_mux_filtered_audio(
        base, inputs, ";".join(filt), out_path, "Music mix", duration,
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
    # Best-effort duration probe — used to drop/clamp overlays that would
    # otherwise land past the end of the base clip (which needs a huge tpad
    # and can make ffmpeg choke on some inputs).
    try:
        base_dur = _ffprobe_duration(base)
    except Exception:
        base_dur = 0.0

    resolved = []
    for ov in overlay_clips:
        if base_dur > 0:
            try:
                ov_start = max(0.0, float(ov.get("start", 0)))
            except (TypeError, ValueError):
                ov_start = 0.0
            if ov_start >= base_dur - 0.05:
                print(f"[overlay] skip: start {ov_start:.2f}s is past base duration "
                      f"{base_dur:.2f}s", flush=True)
                continue
        path = _timeline_clip_source(ov, prefer_proxy=True)
        if not path:
            continue
        ext = path.suffix.lower().lstrip(".")
        if ext in ASSET_EXT_GIF:
            kind = "gif"
        elif ext in ASSET_EXT_STILL:
            kind = "image"
        else:
            kind = "video"
        if kind == "video":
            sid = ov.get("source_job_id")
            safe = _tl_ensure_overlay_video(path, sid if isinstance(sid, str) else None)
            if not safe:
                print(f"[overlay] skip {path.name}: no video stream", flush=True)
                continue
            path = safe
        resolved.append((ov, path, kind))
    if not resolved:
        _tl_run([FFMPEG, "-y", "-i", str(base), "-c", "copy", str(out_path)],
                "Overlay passthrough")
        return

    def _build_graph(items: list) -> tuple[list, str, str]:
        inputs = ["-i", str(base)]
        filt = []
        cur = "[0:v:0]"
        in_idx = 1
        for n, (ov, path, kind) in enumerate(items, start=1):
            start = max(0.0, float(ov.get("start", 0)))
            o_in = max(0.0, float(ov.get("in", 0)))
            o_out = float(ov.get("out", o_in + 4))
            length = max(0.2, o_out - o_in)
            if base_dur > 0:
                # Clamp so the overlay never needs to extend past the base
                # clip's end — an absurdly long tpad/overlay window is both
                # wasteful and a common source of filtergraph failures.
                length = min(length, max(0.2, base_dur - start + 0.5))
            length = min(length, 60.0)
            inputs, filt, cur, in_idx = _tl_composite_overlay_step(
                ov, path, kind, n, inputs, filt, cur, in_idx, start, o_in,
                length, W, H, fps,
            )
        return inputs, filt, cur

    fps = max(15, min(60, int(fps or 30)))
    try:
        inputs, filt, cur = _build_graph(resolved)
        _tl_run(
            [FFMPEG, "-y", *inputs, "-filter_complex", ";".join(filt),
             "-map", cur, "-map", "0:a?", *_VIDEO_ENC_ARGS,
             "-c:a", "aac", "-b:a", "192k", str(out_path)],
            "Overlay composite",
        )
        return
    except RuntimeError as exc:
        first_err = str(exc)
        print(f"[overlay] Overlay composite failed: {first_err}", flush=True)

    # Retry once, dropping overlays that start in the back half of the clip —
    # those are the ones most likely to be driving a bad tpad/duration edge
    # case, and losing them still keeps the earlier overlays on screen.
    if base_dur > 0:
        early = [r for r in resolved if float(r[0].get("start", 0) or 0) <= 0.5 * base_dur]
    else:
        early = []
    if early and len(early) < len(resolved):
        try:
            inputs, filt, cur = _build_graph(early)
            _tl_run(
                [FFMPEG, "-y", *inputs, "-filter_complex", ";".join(filt),
                 "-map", cur, "-map", "0:a?", *_VIDEO_ENC_ARGS,
                 "-c:a", "aac", "-b:a", "192k", str(out_path)],
                "Overlay composite retry",
            )
            print(f"[overlay] retry without late overlays succeeded "
                  f"(dropped {len(resolved) - len(early)} overlay(s))", flush=True)
            return
        except RuntimeError as exc:
            print(f"[overlay] retry also failed: {exc}", flush=True)

    print(f"[overlay] Overlays skipped due to ffmpeg error — passing base "
          f"through unchanged. Original error: {first_err}", flush=True)
    _tl_run([FFMPEG, "-y", "-i", str(base), "-c", "copy", str(out_path)],
            "Overlay passthrough")


def _tl_composite_overlay_step(ov: dict, path: Path, kind: str, n: int,
                                inputs: list, filt: list, cur: str, in_idx: int,
                                start: float, o_in: float, length: float,
                                W: int, H: int, fps: int) -> tuple:
    """Append one overlay's input(s) + filter chain onto an in-progress graph.

    Returns the updated (inputs, filt, cur, in_idx).
    """
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
    if kind == "gif":
        # Animated GIF: decode frames (ignore_loop 0 = honor file loop;
        # stream_loop -1 keeps it playing for the overlay window).
        inputs += [
            "-ignore_loop", "0", "-stream_loop", "-1",
            "-i", str(path),
        ]
        prep = (
            f"[{in_idx}:v:0]trim=duration={length:.3f},"
            f"setpts=PTS-STARTPTS,fps={fps},{scale}"
        )
    elif kind == "image":
        inputs += ["-loop", "1", "-t", f"{length:.3f}", "-i", str(path)]
        # Explicit :0 — phone containers often mix mebx data streams.
        prep = f"[{in_idx}:v:0]{scale}"
    else:
        # Open full file, trim in-graph (avoids -ss-before--i dropping :v on MOV/mebx).
        inputs += ["-i", str(path)]
        prep = (
            f"[{in_idx}:v:0]trim=start={o_in:.3f}:duration={length:.3f},"
            f"setpts=PTS-STARTPTS,{scale}"
        )
    # Ken Burns on the PiP box (photo / short B-roll moments) — skip for GIFs.
    kb = None if kind == "gif" else _tl_kenburns_filter(ov.get("ken_burns"), ow, oh, fps, length)
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
    return inputs, filt, cur, in_idx


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
    audio = tl.get("audio")
    tl["audio"] = audio if isinstance(audio, dict) else None
    # Auto whoosh/click on overlay & camera-effect onsets (Captions-style).
    tl["sfx_overlays"] = tl.get("sfx_overlays", True) is not False
    ts = tl.get("track_states")
    tl["track_states"] = ts if isinstance(ts, dict) else None
    tracks = tl.get("tracks")
    if not isinstance(tracks, dict):
        tracks = {}
    for key in ("main", "overlay", "effects", "text", "music"):
        clips = tracks.get(key)
        tracks[key] = clips if isinstance(clips, list) else []
    # Effect-lane clips: type + start + out(duration). Coerce common shapes.
    _fx_types = {"split_screen", "punch_zoom", "zoom_1_5", "zoom_2x", "ken_burns", "color"}
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
        elif ftype in ("punch_zoom", "zoom_1_5", "zoom_2x"):
            # zoom_1_5 / zoom_2x are "hold" punch zooms: constant scale for
            # the whole effect window rather than the punch-and-decay curve.
            if ftype == "zoom_1_5":
                intensity = "1.5x"
            elif ftype == "zoom_2x":
                intensity = "2x"
            else:
                intensity = fx.get("intensity") or "med"
            punch = {
                "enabled": True,
                "intensity": intensity,
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
                        # Second-source in-point advances with source time on A
                        # for video; stills/GIFs stay pinned at 0 (looped).
                        base_in = max(0.0, float((split or {}).get("in", 0)))
                        ext_b = split_src.suffix.lower().lstrip(".")
                        if ext_b in ASSET_EXT_STILL or ext_b in ASSET_EXT_GIF:
                            s_in = 0.0
                        else:
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
        _assert_mp4_has_video(base, "Main track bake")

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
                    "speaker": w.get("speaker"),
                })

        # Stamp remapped caption words from each segment's Analyze diarization
        # (source time → output time). Prefer already-stamped word.speaker.
        for idx, meta in enumerate(seg_meta):
            sjid = meta.get("source_job_id")
            if not sjid:
                continue
            cache_path = UPLOAD_DIR / f"{sjid}_reframe.json"
            if not cache_path.exists():
                continue
            try:
                diar_src = (json.loads(cache_path.read_text(encoding="utf-8")).get("diarization") or [])
            except (json.JSONDecodeError, OSError):
                continue
            if not diar_src:
                continue
            seg_start = seg_starts[idx] if idx < len(seg_starts) else 0.0
            src_in = float(meta.get("src_in") or 0)
            src_out = float(meta.get("src_out") or 0)
            for w in caption_words:
                if w.get("speaker"):
                    continue
                try:
                    mid_out = (float(w["start"]) + float(w["end"])) / 2.0
                except (TypeError, ValueError, KeyError):
                    continue
                local = mid_out - seg_start
                if local < -0.05 or local > (src_out - src_in) + 0.05:
                    continue
                src_mid = src_in + max(0.0, local)
                for seg in diar_src:
                    try:
                        if float(seg.get("start", 0)) <= src_mid <= float(seg.get("end", 0)):
                            w["speaker"] = seg.get("speaker")
                            break
                    except (TypeError, ValueError):
                        continue

        # ---- Persistent logo / watermark (whole-video overlay) ----
        if tl.get("logo"):
            _stage("adding logo", 48)
            logod = UPLOAD_DIR / f"{job_id}_tllogo.mp4"
            work.append(logod)
            _tl_apply_logo(base, tl["logo"], W, H, logod)
            base = logod

        # ---- Pass 2: background music ----
        music_clips = list(tracks.get("music") or [])
        if not music_clips:
            # Caption Look / Instant Export upload lands as {source}_bgmusic.* —
            # pick it up so Timeline ▶ Render still hears the bed.
            for c in main_clips:
                sjid = c.get("source_job_id")
                if not sjid:
                    continue
                bg_files = sorted(UPLOAD_DIR.glob(f"{sjid}_bgmusic.*"), key=lambda p: p.stat().st_mtime, reverse=True)
                bg_files = [p for p in bg_files if p.is_file() and p.suffix.lower() not in (".json",)]
                if bg_files:
                    style_bg = (tl.get("style") or {}).get("bg_music") or {}
                    try:
                        vol = float(style_bg.get("volume_db", -12))
                    except (TypeError, ValueError):
                        vol = -12.0
                    duck = style_bg.get("duck", True) is not False
                    music_clips = [{
                        "_path": str(bg_files[0]),
                        "in": 0,
                        "out": 0,  # probe in mixer
                        "start": 0,
                        "gain_db": vol,
                        "duck": duck,
                    }]
                    print(f"[timeline] using Caption Look bg music {bg_files[0].name}", flush=True)
                    break
        if music_clips:
            _stage("mixing music", 55)
            mixed = UPLOAD_DIR / f"{job_id}_tlmusic.mp4"
            work.append(mixed)
            try:
                _tl_mix_music(base, music_clips, mixed)
                base = mixed
            except Exception as music_err:
                # Never fail the whole Timeline Render over a music-bed mux
                # hiccup — keep the pre-music picture and continue.
                print(f"[timeline] Music mix skipped: {music_err}", flush=True)
                _safe_unlink(mixed)

        # ---- Pass 3: overlays / B-roll / PiP ----
        if tracks["overlay"]:
            _stage("compositing overlays", 70)
            comp = UPLOAD_DIR / f"{job_id}_tlovl.mp4"
            work.append(comp)
            _tl_composite_overlays(base, tracks["overlay"], W, H, comp, fps=fps)
            base = comp

        # ---- Pass 3b: auto whoosh/click SFX synced to overlay / effect starts ----
        sfx_on = tl.get("sfx_overlays", True) is not False
        if sfx_on and (tracks.get("overlay") or tracks.get("effects")):
            _stage("mixing overlay SFX", 74)
            sfxed = UPLOAD_DIR / f"{job_id}_tlsfx.mp4"
            work.append(sfxed)
            try:
                _tl_mix_overlay_sfx(
                    base,
                    tracks.get("overlay") or [],
                    tracks.get("effects") or [],
                    sfxed,
                )
                base = sfxed
            except Exception as sfx_err:
                print(f"[timeline] overlay SFX skipped: {sfx_err}", flush=True)

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
            hook_title = None
            if isinstance(hb, dict):
                hook_title = hb
                headline = hb.get("text") or ""
            elif isinstance(hb, str):
                headline = hb
            else:
                headline = (caption_style or {}).get("headline_banner") or ""
            style_for_ass = dict(caption_style or _TL_DEFAULT_CAPTION_STYLE)
            if hook_title:
                style_for_ass["hook_title"] = hook_title
            elif headline:
                # Prefer Look hook_* knobs when banner is a plain string.
                if (caption_style or {}).get("hook_title"):
                    style_for_ass["hook_title"] = caption_style["hook_title"]
                else:
                    style_for_ass["headline_banner"] = headline
                    if (caption_style or {}).get("hook_font"):
                        style_for_ass["hook_font"] = caption_style["hook_font"]
                    if (caption_style or {}).get("hook_duration") is not None:
                        style_for_ass["hook_duration"] = caption_style["hook_duration"]
                    if (caption_style or {}).get("hook_mode"):
                        style_for_ass["hook_mode"] = caption_style["hook_mode"]
            caption_ass = build_ass(
                caption_words,
                style_for_ass,
                W, H,
                caption_emoji or {},
                speaker_colors=speaker_colors,
                diarization=diar,
                headline_banner=headline if not hook_title else None,
            )
        titles_ass = _tl_build_titles_ass(tracks["text"], W, H) if tracks["text"] else None

        if caption_ass or titles_ass:
            _stage("adding captions & titles", 85)
            ass_text = _tl_compose_ass(W, H, caption_ass or "", titles_ass or "")
            ass_path = UPLOAD_DIR / f"{job_id}_tltext.ass"
            ass_path.write_text(ass_text, encoding="utf-8")
            work.append(ass_path)
            burn_subtitles(base, ass_path, output_path)
            _assert_mp4_has_video(output_path, "Caption burn")
        else:
            # Nothing to burn — just finalize (stream copy is enough).
            _stage("finalizing", 90)
            _tl_run([FFMPEG, "-y", "-i", str(base),
                     "-map", "0:v:0", "-map", "0:a:0?",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                     *_QT_SAFE_MP4_ARGS,
                     "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                     str(output_path)], "Finalize")
            _assert_mp4_has_video(output_path, "Finalize")

        if not output_path.exists() or output_path.stat().st_size < 1024:
            raise RuntimeError("Render produced an empty or missing output file.")

        # ---- Pass 5: Audio Enhancement (Look → Audio) — exact FFmpeg / AI ----
        audio = tl.get("audio") if isinstance(tl.get("audio"), dict) else None
        if not audio:
            audio = jobs[job_id].get("audio") if isinstance(jobs[job_id].get("audio"), dict) else None
        if _tl_audio_wants_enhance(audio):
            _stage("enhancing audio", 93)
            pre_audio = UPLOAD_DIR / f"{job_id}_tlpreaudio.mp4"
            if pre_audio.exists():
                _safe_unlink(pre_audio)
            output_path.replace(pre_audio)
            work.append(pre_audio)
            jobs[job_id]["audio"] = audio
            try:
                _tl_apply_project_audio(pre_audio, audio, output_path, job_id)
            except Exception as audio_err:
                # Never fail the whole Timeline Render over an audio-enhance
                # hiccup — fall back to the pre-enhancement video untouched.
                print(f"[timeline] Audio enhance skipped: {audio_err}")
                if output_path.exists():
                    _safe_unlink(output_path)
                if not _tl_passthrough_copy(pre_audio, output_path):
                    shutil.copyfile(pre_audio, output_path)
                jobs[job_id]["status"] = "finalizing (audio enhance skipped)"
                _db_save_job(job_id)

        if not output_path.exists() or output_path.stat().st_size < 1024:
            raise RuntimeError("Render produced an empty or missing output file.")
        _assert_mp4_has_video(output_path, "Timeline Render")
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
    project_id = (request.form.get("project_id") or request.form.get("timeline_job_id") or "").strip()
    if project_id and re.fullmatch(r"[a-f0-9]{32}", project_id):
        _write_asset_meta(asset_id, project_id=project_id)
    return jsonify({
        "asset_id": asset_id,
        "kind": kind,
        "filename": f.filename,
        "project_id": project_id or None,
        "duration": _media_duration(dest) if kind in ("video", "audio", "gif") else 0.0,
    })


@app.route("/list-assets", methods=["GET"])
def list_assets():
    """List uploaded timeline assets, newest first.

    Optional query: ``project_id`` — when set, prefer assets tagged to that
    timeline project (also returns ``in_project`` flags for client filtering).
    """
    project_id = (request.args.get("project_id") or "").strip()
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
        aid_project = meta.get("project_id") or None
        out.append({
            "asset_id": p.stem,
            "kind": kind,
            "ext": p.suffix.lstrip("."),
            "filename": filename,
            "keyword": meta.get("keyword"),
            "source": meta.get("source"),
            "project_id": aid_project,
            "in_project": bool(project_id and aid_project == project_id),
            "duration": _media_duration(p) if kind in ("video", "audio", "gif") else 0.0,
            "mtime": p.stat().st_mtime,
        })
    out.sort(key=lambda a: a["mtime"], reverse=True)
    return jsonify({"assets": out, "project_id": project_id or None})


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
        # Idempotent cleanup (Skip / Replace) — not a hard client error.
        return jsonify({"ok": True, "asset_id": asset_id, "removed": 0, "missing": True})
    return jsonify({"ok": True, "asset_id": asset_id, "removed": removed})


@app.route("/rename-asset/<asset_id>", methods=["POST"])
def rename_asset(asset_id: str):
    """Set a human-friendly filename label for an uploaded asset.

    Body: {filename}
    """
    if not asset_id or not re.fullmatch(r"[a-f0-9]{32}", asset_id):
        return jsonify({"error": "Invalid asset id"}), 400
    if not _find_asset_path(asset_id):
        return jsonify({"error": "Unknown asset"}), 404
    data = request.get_json(force=True) or {}
    new_name = (data.get("filename") or "").strip()[:200]
    if not new_name:
        return jsonify({"error": "Filename cannot be empty"}), 400
    _write_asset_meta(asset_id, filename=new_name)
    return jsonify({"asset_id": asset_id, "filename": new_name})


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
    {seed_job_id}. Optional canvas (e.g. 16x9 for long-form). Returns {job_id}.
    """
    data = request.get_json(force=True) or {}
    seed_job_id = data.get("seed_job_id")
    label = (data.get("label") or "Timeline edit").strip()[:80] or "Timeline edit"
    canvas = data.get("canvas") or "9x16"
    if canvas not in TIMELINE_CANVASES:
        canvas = "9x16"

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
    timeline = _normalize_timeline({"tracks": tracks, "canvas": canvas})

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


_TIMELINE_RESERVED_IDS = frozenset({
    "create", "save", "render", "list", "delete", "polish", "new", "export",
})


@app.route("/timeline/<job_id>", methods=["GET"])
def timeline_get(job_id: str):
    # Prevent POST-to-/timeline/polish hitting this GET rule as job_id="polish"
    # when an old process lacked the dedicated polish route (Flask → 405).
    if job_id in _TIMELINE_RESERVED_IDS:
        return jsonify({
            "error": f"/{job_id} is a Timeline action path, not a project id",
            "hint": "For Polish use POST /polish/run (restart the Replit app after git pull)",
        }), 404
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
    # Keep style/audio mirrors on the job so renders and other tabs stay in sync.
    if timeline.get("style") is not None:
        jobs[job_id]["style"] = timeline.get("style")
    if timeline.get("audio") is not None:
        jobs[job_id]["audio"] = timeline.get("audio")
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
    audio = data.get("audio")
    if audio is None:
        audio = timeline.get("audio")
    if audio is None:
        audio = jobs[job_id].get("audio")
    if isinstance(audio, dict):
        timeline["audio"] = audio
        jobs[job_id]["audio"] = audio
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
    """List timeline editor jobs, newest first.

    Query:
      include_empty=1 — keep 0-clip projects (default hides them so empty
      autosaves don't clutter the picker).
    """
    include_empty = str(request.args.get("include_empty") or "").lower() in ("1", "true", "yes")
    out = []
    for jid, job in jobs.items():
        if not job.get("is_timeline"):
            continue
        clip_count = len((job.get("timeline") or {}).get("tracks", {}).get("main", []))
        if not include_empty and clip_count <= 0:
            continue
        out.append({
            "job_id": jid,
            "filename": job.get("filename"),
            "status": job.get("status"),
            "output": job.get("output"),
            "created_at": job.get("created_at"),
            "clip_count": clip_count,
        })
    out.sort(key=lambda a: a.get("created_at") or 0, reverse=True)
    return jsonify({"timelines": out})


@app.route("/timeline/delete", methods=["POST"])
def timeline_delete():
    """Delete a timeline project job (and its DB row). Does not delete source videos."""
    data = request.get_json(force=True) or {}
    job_id = (data.get("job_id") or "").strip()
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown job"}), 404
    job = jobs.get(job_id) or {}
    if not job.get("is_timeline"):
        return jsonify({"error": "Not a timeline project"}), 400
    # Best-effort: remove rendered output files for this timeline job.
    try:
        for folder in (OUTPUT_DIR, UPLOAD_DIR):
            for p in Path(folder).glob(f"{job_id}*"):
                try:
                    p.unlink()
                except OSError:
                    pass
    except Exception:
        pass
    jobs.pop(job_id, None)
    try:
        _db_delete_job(job_id)
    except Exception as e:
        return jsonify({"error": f"Deleted from memory but DB failed: {e}"}), 500
    return jsonify({"ok": True, "job_id": job_id})


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



def _preceding_sentence_for_gap(words: list, gap_start: float, max_words: int = 28) -> str:
    """Words spoken immediately before a silence gap (for taste scoring)."""
    before = []
    for w in words or []:
        try:
            we = float(w.get("end", 0) or 0)
        except (TypeError, ValueError):
            continue
        if we <= gap_start + 0.05:
            before.append(str(w.get("word", "") or "").strip())
    before = [t for t in before if t]
    if not before:
        return ""
    # Prefer the last clause after ., !, ? — else last N words.
    joined = " ".join(before[-max_words:])
    for sep in ("! ", "? ", ". "):
        if sep in joined:
            joined = joined.rsplit(sep, 1)[-1]
            if sep.startswith("!"):
                joined = joined  # already stripped
            break
    return joined.strip()


_TASTE_PROTECT_SENTIMENTS = frozenset({
    "humor", "comedy", "joke", "punchline", "shock", "surprise",
    "dramatic", "reveal", "emotional", "rhetorical", "suspense", "beat",
})

_TASTE_PROTECT_PHRASES = (
    "wait for it", "get this", "here's the thing", "heres the thing",
    "plot twist", "no joke", "dead serious", "you won't believe",
    "you wont believe", "check this out", "listen to this",
)


def _heuristic_taste_decisions(gaps: list) -> list[dict]:
    """Offline taste: protect pauses after punchy / shocked delivery without Gemini."""
    out = []
    for g in gaps or []:
        try:
            start = float(g.get("start", 0) or 0)
            dur = float(g.get("duration") or (float(g.get("end", 0)) - start))
        except (TypeError, ValueError):
            continue
        before = str(g.get("context_before") or g.get("sentence_before") or "").strip()
        low = before.lower()
        sentiment = None
        reason = None
        if any(p in low for p in _TASTE_PROTECT_PHRASES) and dur >= 0.7:
            sentiment = "suspense"
            reason = "Setup phrase before pause — keep the beat"
        elif before.endswith("!") and dur >= 0.9:
            sentiment = "shock" if dur >= 1.2 else "punchline"
            reason = "Exclamation before silence — protect the reaction beat"
        elif before.endswith("?") and dur >= 1.0:
            sentiment = "rhetorical"
            reason = "Question hang — keep space for the answer beat"
        elif dur >= 1.4 and len(before.split()) <= 8 and before.endswith((".", "…", "...")):
            # Short declarative line then long pause → often intentional.
            sentiment = "dramatic"
            reason = "Short line + long pause — likely intentional beat"
        if sentiment:
            out.append({
                "start": round(start, 1),
                "decision": "protect",
                "sentiment": sentiment,
                "reason": reason,
                "source": "heuristic",
            })
        else:
            out.append({
                "start": round(start, 1),
                "decision": "cut",
                "sentiment": "dead_air",
                "reason": "No taste cue — safe to tighten",
                "source": "heuristic",
            })
    return out


def _taste_protect_silence_gaps(words: list, gaps: list) -> dict:
    """Score already-identified silence gaps; return which to Protect.

    Separation of concerns (editing taste):
      1) Identification — silencedetect / word-gap scan finds candidate pauses
      2) Taste — this function only decides protect vs cut (never invents gaps)
      3) Execution — caller skips Protected starts when building cut ranges

    Humor / shock / dramatic beats → Protected. Dead air / breath → Cut.
    Uses Gemini when configured; always runs a heuristic pass as baseline.
    """
    enriched = []
    for g in gaps or []:
        if not isinstance(g, dict):
            continue
        try:
            start = float(g.get("start", 0) or 0)
        except (TypeError, ValueError):
            continue
        item = dict(g)
        item["start"] = start
        if not item.get("sentence_before"):
            item["sentence_before"] = _preceding_sentence_for_gap(words, start)
        if not item.get("context_before") and item.get("sentence_before"):
            item["context_before"] = item["sentence_before"]
        enriched.append(item)

    if not enriched:
        return {
            "protected_starts": [],
            "decisions": [],
            "engine": "none",
        }

    decisions = _heuristic_taste_decisions(enriched)
    engine = "heuristic"
    by_start = {round(float(d["start"]), 1): d for d in decisions}

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if api_key and len(enriched) > 0:
        # Cap payload — long interviews can have dozens of gaps.
        sample = enriched[:40]
        lines = []
        for i, g in enumerate(sample):
            sent = (g.get("sentence_before") or g.get("context_before") or "").strip()[:180]
            after = (g.get("context_after") or "").strip()[:80]
            try:
                dur = float(g.get("duration") or 0)
            except (TypeError, ValueError):
                dur = 0.0
            lines.append(
                f"{i}. t={float(g['start']):.1f}s dur={dur:.1f}s "
                f"| BEFORE: \"{sent}\" | AFTER: \"{after}\""
            )
        prompt = f"""You are an expert dialogue editor deciding which SILENCE GAPS to protect.

Each gap was already IDENTIFIED by audio/transcript timing. Your only job is TASTE —
do NOT invent new gaps. For each gap, decide:
- "protect" if the pause after the preceding line carries comedy, shock, drama, a punchline,
  rhetorical hang, or emotional beat (cutting it would kill the timing).
- "cut" if it is dead air, a breath, an um-pause, or unmotivated lag.

Sentiment tags when protecting: humor, punchline, shock, dramatic, rhetorical, suspense, emotional.
Be conservative: prefer cut for ordinary breaths; protect when the silence IS the joke or the reaction.

Gaps:
{chr(10).join(lines)}

Return STRICT JSON:
{{
  "gaps": [
    {{"index": 0, "decision": "protect", "sentiment": "humor", "reason": "short punchline needs hang"}},
    {{"index": 1, "decision": "cut", "sentiment": "dead_air", "reason": "unmotivated breath"}}
  ]
}}
"""
        try:
            result = _gemini_generate_clip_suggestions(prompt)
            raw = result.get("gaps") if isinstance(result, dict) else None
            if isinstance(raw, list):
                engine = "gemini+heuristic"
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    try:
                        idx = int(item.get("index"))
                    except (TypeError, ValueError):
                        continue
                    if idx < 0 or idx >= len(sample):
                        continue
                    start_key = round(float(sample[idx]["start"]), 1)
                    decision = str(item.get("decision") or "cut").strip().lower()
                    if decision not in ("protect", "cut"):
                        decision = "cut"
                    sentiment = str(item.get("sentiment") or "").strip().lower() or (
                        "beat" if decision == "protect" else "dead_air"
                    )
                    # Sentiment override: humor/shock etc. force protect even if model said cut.
                    if decision == "cut" and sentiment in _TASTE_PROTECT_SENTIMENTS:
                        decision = "protect"
                    reason = str(item.get("reason") or "")[:200]
                    by_start[start_key] = {
                        "start": start_key,
                        "decision": decision,
                        "sentiment": sentiment,
                        "reason": reason or (
                            "LLM taste protect" if decision == "protect" else "LLM: safe to cut"
                        ),
                        "source": "gemini",
                    }
        except Exception as exc:
            ai_logger.warning(f"[taste] Gemini silence taste failed; heuristic only: {exc}")

    decisions = [by_start[k] for k in sorted(by_start.keys())]
    protected = [
        d["start"] for d in decisions
        if d.get("decision") == "protect"
        or str(d.get("sentiment") or "").lower() in _TASTE_PROTECT_SENTIMENTS
    ]
    # De-dupe while preserving order
    seen = set()
    protected_starts = []
    for t in protected:
        key = round(float(t), 1)
        if key in seen:
            continue
        seen.add(key)
        protected_starts.append(key)

    print(
        f"[taste] silence protect {len(protected_starts)}/{len(enriched)} "
        f"gaps engine={engine}",
        flush=True,
    )
    return {
        "protected_starts": protected_starts,
        "decisions": decisions,
        "engine": engine,
    }


def _apply_taste_to_gaps(gaps: list, taste: dict | None) -> list:
    """Annotate gap dicts with taste fields and flip preserved for Protected starts."""
    if not gaps:
        return []
    taste = taste or {}
    decisions = {
        round(float(d.get("start", 0)), 1): d
        for d in (taste.get("decisions") or [])
        if isinstance(d, dict)
    }
    protected = {round(float(t), 1) for t in (taste.get("protected_starts") or [])}
    out = []
    for g in gaps:
        if not isinstance(g, dict):
            continue
        item = dict(g)
        try:
            key = round(float(item.get("start", 0)), 1)
        except (TypeError, ValueError):
            out.append(item)
            continue
        dec = decisions.get(key) or {}
        if key in protected or dec.get("decision") == "protect":
            item["preserved"] = True
            item["taste_protected"] = True
        item["taste_decision"] = dec.get("decision") or (
            "protect" if item.get("preserved") else "cut"
        )
        item["taste_sentiment"] = dec.get("sentiment") or ""
        item["taste_reason"] = dec.get("reason") or ""
        item["taste_source"] = dec.get("source") or taste.get("engine") or ""
        out.append(item)
    return out


def _recommended_cuts_for_words(words: list, t_in: float, t_out: float,
                                max_gap: float = 1.0,
                                include_fillers: bool = True,
                                include_silence: bool = True,
                                taste_protect: bool = True) -> dict:
    """Build filler + silence cut ranges (source seconds) for AI Trim parity.

    Silence identification (word gaps) is separate from execution: when
    ``taste_protect`` is on, humor/shock/dramatic pauses are Protected and
    never become cut ranges.
    """
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
    taste_meta = {"protected_starts": [], "decisions": [], "engine": "none"}
    if include_silence and len(window) >= 2:
        silence = compute_silence_compression(window, max_gap=max_gap, target_gap=0.25)
        raw_gaps = silence.get("gaps") or []
        if taste_protect and raw_gaps:
            try:
                taste_meta = _taste_protect_silence_gaps(window, raw_gaps)
            except Exception as exc:
                ai_logger.warning(f"[taste] recommended-cuts protect failed: {exc}")
                taste_meta = {"protected_starts": [], "decisions": [], "engine": "error"}
            raw_gaps = _apply_taste_to_gaps(raw_gaps, taste_meta)
        protected = {
            round(float(t), 1) for t in (taste_meta.get("protected_starts") or [])
        }
        for g in raw_gaps:
            if g.get("preserved") or g.get("taste_protected"):
                silence_gaps.append({
                    "start": float(g["start"]),
                    "end": float(g["end"]),
                    "duration": float(g.get("duration") or 0),
                    "preserved": True,
                    "taste_protected": True,
                    "taste_sentiment": g.get("taste_sentiment") or "",
                    "taste_reason": g.get("taste_reason") or "",
                    "context_before": g.get("context_before", ""),
                    "context_after": g.get("context_after", ""),
                })
                continue
            try:
                gs = max(t_in, float(g["start"]))
                ge = min(t_out, float(g["end"]))
            except (KeyError, TypeError, ValueError):
                continue
            if round(gs, 1) in protected:
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
                    "preserved": False,
                    "taste_protected": False,
                    "taste_sentiment": g.get("taste_sentiment") or "",
                    "taste_reason": g.get("taste_reason") or "",
                    "context_before": g.get("context_before", ""),
                    "context_after": g.get("context_after", ""),
                })
    merged = _merge_cut_ranges(cuts)
    cut_total = sum(b - a for a, b in merged)
    labeled = []
    for a, b in merged:
        kind = "silence"
        for g in silence_gaps:
            if a < g["end"] and b > g["start"] and not g.get("preserved"):
                kind = "silence"
                labeled.append({
                    "start": a, "end": b,
                    "kind": kind,
                    "context_before": g.get("context_before", ""),
                    "context_after": g.get("context_after", ""),
                    "taste_sentiment": g.get("taste_sentiment", ""),
                    "taste_reason": g.get("taste_reason", ""),
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
        "taste": taste_meta,
        "stats": {
            "cut_count": len(merged),
            "seconds_removed": round(cut_total, 2),
            "window_in": t_in,
            "window_out": t_out,
            "taste_protected": len(taste_meta.get("protected_starts") or []),
            "taste_engine": taste_meta.get("engine"),
        },
    }


def _intensity_effect_budget(intensity: str, duration_sec: float | None = None) -> int:
    base = {"low": 2, "med": 4, "high": 8}.get((intensity or "med").lower(), 4)
    # Full-video / long-form edits need a few more accents without going wild —
    # otherwise energy dies off a cliff past the first minute. Cap at 24.
    try:
        dur = float(duration_sec or 0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur >= 240:
        return min(24, base + 10)
    if dur >= 120:
        return min(18, base + 6)
    if dur >= 60:
        return min(14, base + 3)
    return base


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
                            insert_media: bool = True,
                            hook: dict | None = None) -> dict:
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
    # Camera moves live on the Effects lane now, not as Main splits — splitting
    # Main into a micro-clip per punch/zoom used to make every mid-video accent
    # read as a hard cut into a punch zoom, which is the opposite of "hold a
    # zoom while someone talks". Main stays one continuous piece (plus cuts)
    # except for split_screen, which really does need its own Main segment
    # since it changes how that stretch is framed/composited.
    #
    # Pass the window duration so long edits keep a full accent budget — without
    # it we used to keep only the base ~4 earliest moves and the back half died.
    budget = _intensity_effect_budget(intensity, t_out - t_in)
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
    usable.sort(key=lambda e: float(e["start_time"]))
    if len(usable) > budget:
        # Prefer a spread across the timeline over earliest-N (Gemini + backfill
        # already tried to span; don't throw away the late half here).
        if budget <= 1:
            usable = usable[:1]
        else:
            idxs = sorted({
                int(round(i * (len(usable) - 1) / (budget - 1)))
                for i in range(budget)
            })
            usable = [usable[i] for i in idxs]

    split_fx = [fx for fx in usable if fx.get("type") == "split_screen"]
    lane_fx = [fx for fx in usable if fx.get("type") != "split_screen"]

    pieces = [main_clip]
    if split_fx:
        pieces = []
        cursor = t_in
        for fx in split_fx:
            fs = max(t_in, float(fx["start_time"]))
            fe = min(t_out, float(fx["end_time"]))
            if fs - cursor > 0.08:
                pieces.append({
                    "id": _uid_short(), "source_job_id": job_id,
                    "in": cursor, "out": fs, "transition": None,
                    "burn_captions": True, "cuts": [],
                    "color": grade, "color_grade": grade,
                })
            pieces.append({
                "id": _uid_short(), "source_job_id": job_id,
                "in": fs, "out": fe, "transition": None,
                "burn_captions": True, "cuts": [],
                "color": grade, "color_grade": grade,
                "split": {"enabled": True},
            })
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
        # Transitions between pieces — hard cuts unless the pack opts in.
        tr = pack.get("transition")
        if tr:
            for i, p in enumerate(pieces[:-1]):
                p["transition"] = {"type": tr, "duration": 0.25 if intensity != "high" else 0.15}

    # punch_zoom / zoom_1_5 / zoom_2x / ken_burns → Effects lane, in output
    # time. Main is one continuous source window [t_in, t_out] (plus the
    # split_screen pieces above), so output time is just source time offset
    # from t_in — a good seed the editor can nudge by hand once cuts compress
    # the timeline.
    effects_track: list[dict] = []
    for fx in lane_fx:
        fs = max(t_in, float(fx["start_time"]))
        fe = min(t_out, float(fx["end_time"]))
        entry = {
            "id": _uid_short(),
            "type": fx.get("type"),
            "start": round(fs - t_in, 3),
            "out": round(max(0.2, fe - fs), 3),
            "intensity": fx.get("intensity") or "med",
            "direction": fx.get("direction") or "in",
        }
        if isinstance(fx.get("anchor"), dict):
            entry["anchor"] = fx["anchor"]
        if fx.get("quote"):
            entry["quote"] = fx["quote"]
        if fx.get("reason"):
            entry["reason"] = fx["reason"]
        effects_track.append(entry)

    # Hook-pull: for full-video edits, reorder so the strongest moment opens
    # the edit even though it happens mid-transcript (classic hook-first cut).
    hook_pulled = False
    hook_quote = None
    if hook:
        try:
            hook_s = max(t_in, float(hook.get("start_time")))
            hook_e = min(t_out, float(hook.get("end_time")))
        except (TypeError, ValueError):
            hook_s = hook_e = None
        if (
            hook_s is not None and hook_e is not None
            and hook_e > hook_s
            and 2.5 <= (hook_e - hook_s) <= 12.0
            and hook_s > t_in + 2.0
        ):
            # Reorder as three source ranges: the hook itself, then whatever
            # came before it, then whatever came after — all hard cuts.
            ranges = [(hook_s, hook_e), (t_in, hook_s), (hook_e, t_out)]
            new_pieces = []
            for rs, re_ in ranges:
                if re_ - rs < 0.1:
                    continue
                new_pieces.append({
                    "id": _uid_short(), "source_job_id": job_id,
                    "in": rs, "out": re_, "transition": None,
                    "burn_captions": True, "cuts": [],
                    "color": grade, "color_grade": grade,
                })
            if new_pieces:
                # Redistribute original cuts onto the new ranges by intersection.
                if cuts:
                    for p in new_pieces:
                        p["cuts"] = [
                            [max(p["in"], c[0]), min(p["out"], c[1])]
                            for c in cuts
                            if c[1] > p["in"] and c[0] < p["out"]
                            and min(p["out"], c[1]) - max(p["in"], c[0]) > 0.05
                        ]
                # Re-apply split_screen onto whichever new piece it now falls
                # inside (best-effort, by midpoint).
                for fx in split_fx:
                    try:
                        fmid = (float(fx["start_time"]) + float(fx["end_time"])) / 2
                    except (TypeError, ValueError):
                        continue
                    target = next(
                        (p for p in new_pieces if p["in"] - 0.05 <= fmid <= p["out"] + 0.05),
                        None,
                    )
                    if target is not None:
                        target["split"] = {"enabled": True}
                pieces = new_pieces
                hook_pulled = True
                hook_quote = str(hook.get("hook_quote") or hook.get("quote") or "")[:200] or None

                # Effects-lane entries were seeded in *pre-pull* output time
                # (offset from the old t_in). Remap each one's start through
                # the same three-window reorder so it still lands on the beat
                # it was anchored to instead of drifting into whatever now
                # plays at that raw offset.
                def _map_through_ranges(t: float, rngs: list[tuple[float, float]]) -> float | None:
                    cursor = 0.0
                    for rs, re__ in rngs:
                        span = re__ - rs
                        if span < 0.1:
                            continue
                        if rs - 0.05 <= t <= re__ + 0.05:
                            return cursor + max(0.0, min(t, re__) - rs)
                        cursor += span
                    return None

                remapped = []
                for entry in effects_track:
                    fs = entry["start"] + t_in
                    new_start = _map_through_ranges(fs, ranges)
                    if new_start is None:
                        continue  # fell in a range too short to keep (<0.1s)
                    entry["start"] = round(new_start, 3)
                    remapped.append(entry)
                effects_track = remapped

    # AI Edit never seeds text overlays — captions already carry the words,
    # and auto-placed titles/keyword callouts read as clutter more often than
    # they help. Pack "add_title" and keyword text callouts are ignored here;
    # the editor's manual "Add title" button still works for hand-placed text.
    text_track: list[dict] = []

    # Keyword callouts still drive photo-match / badge overlays (Phase 5/3) —
    # just without the on-screen text label that used to ride along with them.
    overlay_track = []
    if insert_media and words:
        photo_match = bool(pack.get("photo_match"))
        use_ai = bool(pack.get("use_ai_photos")) and _gemini_image_ready()
        media_budget = {"low": 2, "med": 4, "high": 6}.get((intensity or "med").lower(), 4) if photo_match else {
            "low": 1, "med": 3, "high": 5
        }.get((intensity or "med").lower(), 3)
        callouts = _broll_callouts_for_window(words, t_in, t_out, media_budget, semantic=True)
        ken_default = pack.get("ken_burns") if isinstance(pack.get("ken_burns"), dict) else None
        for i, co in enumerate(callouts):
            # Map source time into output time roughly as offset from t_in
            # (before cut compression — good enough for seed placement).
            start = max(0.0, float(co["start"]) - t_in)
            dur = min(2.8, max(1.4, float(co.get("duration") or 1.8))) if photo_match else min(
                2.2, max(1.2, float(co.get("duration") or 1.8))
            )
            # Photo-match: prefer real stills (Gemini / stock). Never fall back
            # to keyword text badges (dated purple boxes that fight captions).
            try:
                asset_id = uuid.uuid4().hex
                dest_stem = ASSET_DIR / asset_id
                asset_path = None
                source = "photo"
                stock_provider = None
                # Prefer talker screenshot when Analyze speakers+faces is ready.
                if photo_match and _reframe_faces_ready(job_id):
                    still = _speaker_still_for_time(
                        job_id, float(co["start"]), dest_stem, words=words,
                    )
                    if still:
                        asset_path = still
                        source = "speaker_still"
                        stock_provider = "speaker_still"
                if asset_path is None and photo_match and (
                    _broll_any_photo_provider() or use_ai
                ):
                    hint = _appearance_hint_for_time(
                        job_id, float(co["start"]), words=words,
                    )
                    ref = None
                    if use_ai or _query_implies_person(str(co["text"])):
                        ref = _speaker_still_for_time(
                            job_id, float(co["start"]),
                            ASSET_DIR / f"{asset_id}_ref", words=words,
                        )
                    asset_path, src_tag = _fetch_broll_image_for_keyword_ex(
                        str(co["text"]), dest_stem, use_ai=use_ai,
                        appearance_hint=hint, reference_face=ref,
                    )
                    if ref is not None:
                        try:
                            _safe_unlink(ref)
                        except OSError:
                            pass
                    if asset_path:
                        source = src_tag or "photo"
                if asset_path:
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
                    continue
                _write_asset_meta(
                    asset_id,
                    filename=f"{co['text']}{asset_path.suffix.lower()}",
                    keyword=str(co["text"]),
                    source=source,
                    provider=stock_provider,
                )
                face_cx, face_cy = _face_hint_for_job(job_id)
                cap_y = 82.0
                try:
                    cap_y = float((pack.get("style") or {}).get("position_y") or cap_y)
                except (TypeError, ValueError):
                    pass
                if photo_match and source in ("photo", "gemini"):
                    pos = _caption_aware_overlay_layout(
                        i, "center",
                        face_cx=face_cx, face_cy=face_cy, caption_y_pct=cap_y,
                    )
                    layout = pos.get("layout") or "center"
                    opacity = 1.0
                    border = 0
                    if ken_default and ken_default.get("enabled"):
                        ken = {
                            "enabled": True,
                            "direction": ken_default.get("direction") or "in",
                            "intensity": ken_default.get("intensity") or "med",
                        }
                    else:
                        ken = {"enabled": True, "direction": "in", "intensity": "med"}
                else:
                    pos = _caption_aware_overlay_layout(
                        i, "pip",
                        face_cx=face_cx, face_cy=face_cy, caption_y_pct=cap_y,
                    )
                    layout = pos.get("layout") or "pip_auto"
                    opacity = 1.0
                    border = 0
                    ken = None
                overlay_track.append({
                    "id": _uid_short(),
                    "asset_id": asset_id,
                    "keyword": str(co["text"])[:40],
                    "source": source,
                    "in": 0,
                    "out": dur,
                    "start": start,
                    "x": pos["x"], "y": pos["y"],
                    "w": pos["w"], "h": pos["h"],
                    "opacity": opacity,
                    "fit": pos.get("fit") or ("cover" if str(layout).startswith("pip") else "contain"),
                    "fade_in": 0.15, "fade_out": 0.25,
                    "border_px": border,
                    "layout": layout,
                    "ken_burns": ken,
                    "anchor": pieces[0]["id"] if pieces else None,
                    "anchor_offset": start,
                })
            except Exception:
                pass

    music_track = []
    # Auto-attach previously uploaded bg music for this job, if present.
    bg_music_files = list(UPLOAD_DIR.glob(f"{job_id}_bgmusic.*"))
    if bg_music_files and intensity != "low":
        # Music assets on the timeline use asset_id from /upload-asset.
        # For job-scoped bg music we keep a hint the UI can surface.
        pass

    photo_n = sum(1 for o in overlay_track if o.get("source") in ("photo", "gemini"))
    badge_n = sum(1 for o in overlay_track if o.get("source") == "badge")
    gemini_n = sum(1 for o in overlay_track if o.get("source") == "gemini")

    cut_removed = 0.0
    for c in (cuts or []):
        try:
            cut_removed += max(0.0, float(c[1]) - float(c[0]))
        except (TypeError, ValueError, IndexError):
            continue
    before_s = max(0.0, float(t_out) - float(t_in))
    after_s = max(0.0, before_s - cut_removed)
    edit_receipt = {
        "kind": "ai_edit",
        "at": time.time(),
        "label": (label or pack.get("label") or "AI Edit")[:80],
        "style_pack": pack.get("label"),
        "intensity": intensity,
        "scope": "full" if (t_in <= 0.05 and before_s > 30) else "window",
        "duration": {
            "before_s": round(before_s, 2),
            "after_s": round(after_s, 2),
            "removed_s": round(cut_removed, 2),
        },
        "cuts": {
            "count": len(cuts or []),
            "seconds_removed": round(cut_removed, 2),
            "summary": (
                f"Applied {len(cuts or [])} cut range(s) (−{cut_removed:.1f}s)"
                if cuts else "No silence/stumble cuts applied"
            ),
        },
        "effects": [
            {
                "id": e.get("id"),
                "type": e.get("type"),
                "start": e.get("start"),
                "duration": e.get("out"),
                "intensity": e.get("intensity"),
                "quote": e.get("quote") or "",
                "track": "effects",
            }
            for e in effects_track
        ],
        "overlays": [
            {
                "id": o.get("id"),
                "start": o.get("start"),
                "keyword": o.get("keyword") or "",
                "source": o.get("source") or "",
                "track": "overlay",
            }
            for o in overlay_track
        ],
        "audio": {
            "summary": "Captions Audio Enhancement applies on ▶ Render (loudnorm / noise / duck)",
        },
        "hook": {
            "pulled": bool(hook_pulled),
            "quote": hook_quote or ((hook or {}).get("quote") if isinstance(hook, dict) else None),
        },
        "rows": [],
    }
    # Flat clickable rows for Timeline UI (duration → cuts → zooms → B-roll).
    edit_receipt["rows"] = [
        {
            "id": "dur",
            "kind": "duration",
            "label": (
                f"Length {edit_receipt['duration']['before_s']:.0f}s → "
                f"{edit_receipt['duration']['after_s']:.0f}s"
                f" (−{edit_receipt['duration']['removed_s']:.0f}s)"
            ),
        },
        {
            "id": "cuts",
            "kind": "cuts",
            "label": edit_receipt["cuts"]["summary"],
        },
    ]
    for e in edit_receipt["effects"]:
        tlabel = {
            "punch_zoom": "Punch zoom",
            "zoom_1_5": "1.5× zoom",
            "zoom_2x": "2× zoom",
            "ken_burns": "Ken Burns",
        }.get(e.get("type"), e.get("type") or "Effect")
        st = float(e.get("start") or 0)
        mm, ss = divmod(int(st), 60)
        edit_receipt["rows"].append({
            "id": e.get("id"),
            "kind": "effect",
            "track": "effects",
            "clip_id": e.get("id"),
            "label": f"{tlabel} @{mm}:{ss:02d}"
                     + (f" — “{(e.get('quote') or '')[:40]}”" if e.get("quote") else ""),
        })
    for o in edit_receipt["overlays"]:
        st = float(o.get("start") or 0)
        mm, ss = divmod(int(st), 60)
        kw = o.get("keyword") or o.get("source") or "B-roll"
        edit_receipt["rows"].append({
            "id": o.get("id"),
            "kind": "overlay",
            "track": "overlay",
            "clip_id": o.get("id"),
            "label": f"B-roll “{kw}” @{mm}:{ss:02d}",
        })
    if hook_pulled and hook_quote:
        edit_receipt["rows"].append({
            "id": "hook",
            "kind": "hook",
            "label": f"Hook pulled forward — “{str(hook_quote)[:60]}”",
        })

    return {
        "canvas": pack.get("canvas") or "9x16",
        "fit": "cover",
        "fps": 30,
        "bg": "#000000",
        "style": _normalize_caption_style(pack.get("style") or {}),
        "caption_preset": pack.get("caption_preset"),
        "ai_edit": {
            "style_pack": pack.get("label"),
            "style_pack_id": (pack.get("_id") or (pack.get("label") or "").lower()),
            "intensity": intensity,
            "insert_media": bool(insert_media),
            "photo_match": bool(pack.get("photo_match")),
            "use_ai_photos": bool(pack.get("use_ai_photos")),
            "broll_mode": "photo" if pack.get("photo_match") else "auto",
            "broll_placement": "center" if pack.get("photo_match") else "pip",
            "broll_scope": "full",
            "ken_burns_on_accept": bool(pack.get("photo_match")),
            "hook_pulled": hook_pulled,
            "hook_quote": hook_quote,
        },
        "edit_receipt": edit_receipt,
        "tracks": {
            "main": pieces,
            "overlay": overlay_track,
            "effects": effects_track,
            "text": text_track,
            "music": music_track,
        },
        "media_hints": {
            "bg_music_available": bool(bg_music_files),
            "callout_count": 0,
            "overlay_count": len(overlay_track),
            "photo_count": photo_n,
            "badge_count": badge_n,
            "gemini_count": gemini_n,
            "photo_match": bool(pack.get("photo_match")),
            "review_overlays": bool(pack.get("photo_match")),
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
    "event", "events", "nightlife", "concert", "venue", "park", "street",
    "influencer", "influencers", "social", "media", "content", "creator",
}


# ---- Overlay worthiness (Checkpoint A) ------------------------------------
# Score candidates BEFORE fetching stock/Google assets. Threshold + spacing
# keep talking-head edits from spraying B-roll on every noun.

_OVERLAY_ABSTRACT = {
    "feel", "feeling", "felt", "stress", "stressed", "energy", "vibe", "vibes",
    "thing", "things", "stuff", "basically", "literally", "actually", "really",
    "honestly", "whatever", "something", "someone", "everything", "nothing",
    "moment", "journey", "mindset", "passion", "grateful", "blessed",
}
_OVERLAY_PROOF = {
    "flight", "flights", "airport", "ticket", "price", "cost", "dollar", "dollars",
    "percent", "score", "rating", "approved", "vote", "headline", "article",
    "news", "report", "chart", "map", "route", "schedule", "calendar", "event",
    "festival", "concert", "restaurant", "hotel", "product", "app", "website",
}
_OVERLAY_PLACE = {
    "city", "park", "street", "beach", "boardwalk", "venue", "stadium", "airport",
    "restaurant", "hotel", "market", "store", "shop", "club", "bar", "museum",
}


def _overlay_google_tab_for_query(text: str, reason: str = "") -> str:
    """Suggest which Google surface fits a cue (SERP screenshot router hint)."""
    blob = f"{text} {reason}".lower()
    if re.search(r"\b(flight|flights|ewr|jfk|lga|lax|airport|nonstop)\b", blob):
        return "flights"
    if re.search(r"\b(restaurant|cafe|hotel|address|near me|directions|map)\b", blob):
        return "maps"
    if re.search(r"\b(headline|article|approved|news|report|announced)\b", blob):
        return "web"
    if re.search(r"\b(overview|summary|explained|what is|how does)\b", blob):
        return "ai_overview"
    if re.search(r"\b(festival|concert|crowd|skyline|photo|looks like|wearing)\b", blob):
        return "images"
    return "images"


def _score_overlay_worthiness(
    callout: dict,
    *,
    window_start: float,
    window_end: float,
    story_role: str | None = None,
) -> dict:
    """Return score 0–100 + reasons for whether a cue deserves an overlay.

    Rubric (matches product brainstorm):
      + concrete entity / proof / place
      + story joint (hook / turn / closer)
      + visualizable search query
      − abstract feeling / filler
      − too early pile-up / weak quote
    """
    text = str(callout.get("text") or callout.get("search_query") or "").strip()
    quote = str(callout.get("quote") or "").strip()
    reason = str(callout.get("reason") or "").strip()
    try:
        start = float(callout.get("start") or callout.get("start_time") or window_start)
    except (TypeError, ValueError):
        start = float(window_start)

    toks = [t for t in re.sub(r"[^a-z0-9\s]+", " ", text.lower()).split() if t]
    reasons_pos: list[str] = []
    reasons_neg: list[str] = []
    score = 35  # baseline: needs evidence to rise

    # Concrete visual query length
    if 3 <= len(toks) <= 10:
        score += 12
        reasons_pos.append("concrete multi-word visual query")
    elif len(toks) <= 1:
        score -= 18
        reasons_neg.append("single-token / weak query")

    proof_hits = [t for t in toks if t in _OVERLAY_PROOF]
    place_hits = [t for t in toks if t in _OVERLAY_PLACE or t in _VISUAL_KEYWORDS]
    abstract_hits = [t for t in toks if t in _OVERLAY_ABSTRACT]

    if proof_hits:
        score += 18
        reasons_pos.append(f"proof/entity ({', '.join(proof_hits[:3])})")
    if place_hits:
        score += 10
        reasons_pos.append(f"place/visual noun ({', '.join(place_hits[:3])})")
    if abstract_hits and not proof_hits:
        score -= 22
        reasons_neg.append(f"abstract/filler ({', '.join(abstract_hits[:3])})")

    # Numbers / money / dates in query or quote → proof overlay
    if re.search(r"(\$\d|\d+%|\b\d{4}\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b)", f"{text} {quote}", re.I):
        score += 14
        reasons_pos.append("numeric/date proof")

    role = (story_role or callout.get("story_role") or "").strip().lower()
    if role in ("hook", "opener"):
        score += 12
        reasons_pos.append("hook / opener beat")
    elif role in ("closer", "payoff", "button"):
        score += 10
        reasons_pos.append("closer / payoff beat")
    elif role in ("turn", "conflict", "proof"):
        score += 8
        reasons_pos.append(f"story joint ({role})")

    rel = 0.0
    span = max(0.5, float(window_end) - float(window_start))
    if span > 0:
        rel = (start - float(window_start)) / span
    if rel <= 0.12:
        # Early is fine for ONE hook image; don't over-reward pile-up here.
        score += 4
        reasons_pos.append("early-window candidate")
    elif 0.35 <= rel <= 0.85:
        score += 6
        reasons_pos.append("mid/body placement room")

    if quote and len(quote.split()) >= 3:
        score += 6
        reasons_pos.append("anchored to a real quote")
    elif not quote and callout.get("source") == "semantic":
        score -= 4
        reasons_neg.append("no quote anchor")

    if reason and len(reason) > 12:
        score += 4

    # LLM may already send a self-score 0–1 or 0–100
    raw_self = callout.get("worthiness") or callout.get("worthiness_score")
    try:
        self_s = float(raw_self)
        if self_s <= 1.0:
            self_s *= 100.0
        # Blend lightly so heuristics still dominate v1
        score = int(round(0.7 * score + 0.3 * self_s))
        reasons_pos.append(f"model self-score {self_s:.0f}")
    except (TypeError, ValueError):
        pass

    score = max(0, min(100, int(score)))
    google_tab = callout.get("google_tab") or _overlay_google_tab_for_query(text, reason)
    worthy = score >= int(os.environ.get("OVERLAY_WORTHINESS_THRESHOLD", "55") or 55)

    return {
        "score": score,
        "worthy": worthy,
        "google_tab": google_tab,
        "reasons_for": reasons_pos[:6],
        "reasons_against": reasons_neg[:6],
        "start": round(start, 3),
        "text": text[:80],
        "quote": quote[:160],
        "story_role": role or None,
    }


def _rank_overlay_candidates(
    callouts: list,
    *,
    window_start: float,
    window_end: float,
    budget: int,
    min_gap_s: float = 18.0,
    threshold: int | None = None,
) -> list:
    """Score → filter by threshold → enforce spacing → keep top *budget*.

    Returns callouts enriched with worthiness fields. Unworthy cues are dropped
    (Checkpoint A). Spacing stops "every noun gets a sticker."
    """
    if threshold is None:
        try:
            threshold = int(os.environ.get("OVERLAY_WORTHINESS_THRESHOLD", "55") or 55)
        except (TypeError, ValueError):
            threshold = 55
    budget = max(0, min(12, int(budget or 0)))
    if budget <= 0 or not callouts:
        return []

    scored = []
    for c in callouts:
        if not isinstance(c, dict):
            continue
        meta = _score_overlay_worthiness(
            c, window_start=window_start, window_end=window_end,
            story_role=c.get("story_role"),
        )
        row = dict(c)
        row["worthiness"] = meta
        row["worthiness_score"] = meta["score"]
        row["google_tab"] = meta["google_tab"]
        scored.append(row)

    scored.sort(key=lambda r: (-int(r.get("worthiness_score") or 0), float(r.get("start") or 0)))

    kept: list[dict] = []
    for row in scored:
        score = int(row.get("worthiness_score") or 0)
        if score < threshold:
            continue
        st = float(row.get("start") or 0)
        if any(abs(st - float(k.get("start") or 0)) < min_gap_s for k in kept):
            # Too close to a stronger (already kept) overlay
            w = row.get("worthiness") or {}
            against = list(w.get("reasons_against") or [])
            against.append(f"too close to another overlay (<{min_gap_s:.0f}s)")
            w["reasons_against"] = against
            w["worthy"] = False
            row["worthiness"] = w
            continue
        kept.append(row)
        if len(kept) >= budget:
            break

    kept.sort(key=lambda r: float(r.get("start") or 0))
    return kept


def _keyword_callouts_for_window(words: list, t_in: float, t_out: float,
                                 budget: int) -> list:
    """Pick visual keywords inside [t_in, t_out] from the STT transcript only.

    Strict allow-list — never promote arbitrary long tokens (names like
    \"Anthony\", place fragments like \"Central\") into B-roll search. That
    was the \"metadata bleed / phonetic guess\" failure mode.
    """
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
        if not clean or clean in seen:
            continue
        if clean not in _VISUAL_KEYWORDS:
            continue
        seen.add(clean)
        out.append({
            "text": clean,
            "start": ws,
            "duration": max(1.4, min(2.4, we - ws + 1.2)),
            "source": "keyword",
        })
    return out


def _semantic_broll_callouts(words: list, t_in: float, t_out: float,
                             budget: int) -> list:
    """Sentence-level LLM → stock-search queries (Captions-style semantic B-roll).

    Reads full sentences in the window, extracts the *concept* (not a single
    token), and returns timed search phrases bound to word-level start times.
    Falls back to the allow-list keyword picker when Gemini is unavailable.
    """
    budget = max(1, min(12, int(budget or 4)))
    win_words = []
    for w in words or []:
        try:
            ws = float(w.get("start", 0))
            we = float(w.get("end", 0))
        except (TypeError, ValueError):
            continue
        if we <= t_in or ws >= t_out:
            continue
        win_words.append(w)
    if not win_words:
        return []

    # Prefer Gemini when configured.
    if not (os.environ.get("GEMINI_API_KEY") or "").strip():
        return _keyword_callouts_for_window(words, t_in, t_out, budget)

    transcript = _format_transcript_for_llm(win_words)
    if not transcript.strip():
        return _keyword_callouts_for_window(words, t_in, t_out, budget)

    span = max(1.0, float(t_out) - float(t_in))
    prompt = f"""You are a video editor choosing B-roll search queries for a talking-head cut.

Window: {t_in:.1f}s → {t_out:.1f}s ({span:.0f}s). Transcript has [mm:ss] stamps relative to the FULL video.

Worthiness rules (ONLY propose overlays that pass):
- Viewer must NEED to see it to believe/understand the line (proof, place, product, event, number).
- Prefer story joints: hook, turn/proof, closer — not every mid-sentence noun.
- Skip abstract feelings, filler, and anything already obvious on camera.
- Prefer concrete visual concepts: "local nightlife events flyer collage", "urban park meetup", "phone calendar app".
- When the beat is about people / reactions / the speakers themselves, use a people-oriented query
  (e.g. "two people talking over coffee") — our pipeline may use a talker screenshot.
- Never invent moments that aren't in the transcript.
- Propose up to {min(budget * 2, 10)} candidates (we will score + thin to ~{budget}).
- start_time on the emphasized words; duration 1.6–2.8s.
- search_query is what we type into stock/Google (3–8 words, no quotes).
- google_tab one of: images | web | flights | maps | ai_overview
- story_role one of: hook | proof | turn | body | closer | null
- worthiness 0–100 (your confidence this beat deserves an overlay)

Return STRICT JSON:
{{
  "overlays": [
    {{
      "start_time": 12.4,
      "duration": 2.0,
      "search_query": "city nightlife event crowd evening",
      "quote": "exact words near this beat",
      "reason": "why this visual matches the sentence meaning",
      "story_role": "proof",
      "google_tab": "images",
      "worthiness": 78
    }}
  ]
}}

Transcript:
{transcript}
"""
    try:
        result = _gemini_generate_clip_suggestions(prompt)
    except Exception as exc:
        ai_logger.warning(f"[broll] semantic callouts fell back to keywords: {exc}")
        return _keyword_callouts_for_window(words, t_in, t_out, budget)

    raw = result.get("overlays") if isinstance(result, dict) else None
    if not isinstance(raw, list):
        return _keyword_callouts_for_window(words, t_in, t_out, budget)

    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        q = str(item.get("search_query") or item.get("query") or item.get("text") or "").strip()
        if len(q) < 3:
            continue
        # Reject obvious name-only / metadata-ish queries.
        ql = re.sub(r"[^a-z0-9\s]+", " ", q.lower()).strip()
        toks = [t for t in ql.split() if t]
        if len(toks) == 1 and toks[0] not in _VISUAL_KEYWORDS:
            continue
        try:
            st = float(item.get("start_time", item.get("start", t_in)))
        except (TypeError, ValueError):
            continue
        try:
            dur = float(item.get("duration") or 2.0)
        except (TypeError, ValueError):
            dur = 2.0
        st = max(float(t_in), min(float(t_out) - 0.3, st))
        dur = max(1.4, min(2.8, dur))
        role = str(item.get("story_role") or "").strip().lower() or None
        if role in ("null", "none", "n/a"):
            role = None
        tab = str(item.get("google_tab") or "").strip().lower() or None
        if tab not in ("images", "web", "flights", "maps", "ai_overview"):
            tab = None
        entry = {
            "text": q[:80],
            "start": round(st, 3),
            "duration": round(dur, 3),
            "quote": str(item.get("quote") or "")[:160],
            "reason": str(item.get("reason") or "")[:200],
            "source": "semantic",
            "story_role": role,
            "google_tab": tab,
        }
        if item.get("worthiness") is not None:
            entry["worthiness"] = item.get("worthiness")
        out.append(entry)
        if len(out) >= max(budget * 2, budget):
            break

    if not out:
        return _keyword_callouts_for_window(words, t_in, t_out, budget)
    out.sort(key=lambda c: c["start"])
    return out


def _broll_callouts_for_window(words: list, t_in: float, t_out: float,
                               budget: int, *, semantic: bool = True,
                               rank: bool = True) -> list:
    """Public picker: semantic LLM when possible, else keyword allow-list.

    When *rank* is True (default), apply worthiness scoring + spacing so only
    Checkpoint-A-worthy cues survive (see ``_rank_overlay_candidates``).
    """
    # Pull a wider candidate pool, then thin with the scorer.
    raw_budget = max(budget, min(12, budget * 2)) if rank else budget
    if semantic:
        raw = _semantic_broll_callouts(words, t_in, t_out, raw_budget)
    else:
        raw = _keyword_callouts_for_window(words, t_in, t_out, raw_budget)
    if not rank:
        return raw[:budget]
    return _rank_overlay_candidates(
        raw,
        window_start=t_in,
        window_end=t_out,
        budget=budget,
    )


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
      label?, purpose?, apply_cuts?, create_clip?, max_effects?
    }

    Omit end_time (or pass full transcript end) + create_clip=false to edit the
    entire uploaded video — not only a Shorts chop.
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
    pack = dict(AI_EDIT_STYLE_PACKS.get(pack_id) or AI_EDIT_STYLE_PACKS["pulse"])
    pack["_id"] = pack_id
    intensity = (data.get("intensity") or "med").lower()
    if intensity not in ("low", "med", "high"):
        intensity = "med"
    label = (data.get("label") or src.get("filename") or "AI Edit")[:120]
    purpose = str(data.get("purpose") or data.get("edit_purpose") or "").strip()[:500]

    try:
        t_in = float(data.get("start_time", data.get("in", 0)) or 0)
        default_out = float(words[-1].get("end", 0) or 0)
        # Explicit full-video flag, or missing end → use full transcript.
        if data.get("full_video") or (
            data.get("end_time") is None and data.get("out") is None
            and data.get("create_clip") is not True
        ):
            t_out = default_out
            t_in = 0.0
        else:
            t_out = float(data.get("end_time", data.get("out", default_out)) or default_out)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid start/end"}), 400
    t_in = max(0.0, t_in)
    if t_out <= t_in + 0.5:
        return jsonify({"error": "Clip window too short"}), 400

    apply_cuts = bool(data.get("apply_cuts", True))
    create_clip = bool(data.get("create_clip", False))
    insert_media = bool(data.get("insert_media", True))
    window_dur = t_out - t_in

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
    hook = None
    gemini_warning = None
    try:
        max_fx = int(data.get("max_effects") or _intensity_effect_budget(intensity, window_dur))
    except (TypeError, ValueError):
        max_fx = _intensity_effect_budget(intensity, window_dur)
    try:
        total = float(words[-1].get("end", 0) or work_out)
        # Windowed words for the LLM when editing a range of the parent job.
        win_words = [
            w for w in words
            if float(w.get("end", 0) or 0) > work_in and float(w.get("start", 0) or 0) < work_out
        ] or words
        result = _gemini_generate_clip_suggestions(
            _build_effect_suggestion_prompt(
                _format_transcript_for_llm(win_words),
                max(1.0, work_out - work_in if create_clip else total),
                max_fx,
                purpose=purpose or None,
            )
        )
        effects = _sanitize_effect_suggestions(result.get("effects") or [], total)
        # Long edits from Gemini tend to front-load all the energy into the
        # first minute — backfill quiet late stretches with light accents so
        # the back half of the video isn't dead.
        effects = _ensure_effects_span_timeline(effects, 0.0, total, win_words, max_fx)
        for fx in effects:
            if fx.get("type") in ("punch_zoom", "zoom_1_5", "zoom_2x"):
                anchor = _face_anchor_at(work_job_id, fx["start_time"])
                if anchor:
                    fx["anchor"] = anchor
        structure = result.get("structure") or {}
        if isinstance(structure, dict) and structure.get("hook_start_time") is not None \
                and structure.get("hook_end_time") is not None:
            try:
                hook = {
                    "start_time": float(structure["hook_start_time"]),
                    "end_time": float(structure["hook_end_time"]),
                    "hook_quote": str(structure.get("hook_quote") or "")[:200],
                    "hook_reason": str(structure.get("hook_reason") or "")[:300],
                }
            except (TypeError, ValueError):
                hook = None
    except RuntimeError as exc:
        gemini_warning = str(exc)
    except Exception as exc:
        gemini_warning = str(exc)

    timeline = _build_ai_edit_timeline(
        work_job_id, work_in, work_out, pack, intensity, cuts, effects,
        label=label, words=words, insert_media=insert_media, hook=hook,
    )
    if purpose:
        timeline["ai_edit"] = dict(timeline.get("ai_edit") or {})
        timeline["ai_edit"]["purpose"] = purpose
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
        "purpose": purpose or None,
        "full_video": bool(data.get("full_video")) or (t_in <= 0.05 and abs(t_out - float(words[-1].get("end", 0) or 0)) < 0.5),
        "label": label,
        "in": work_in,
        "out": work_out,
        "recommended_cuts": rec,
        "applied_cuts": cuts,
        "effects": effects,
        "hook": hook,
        "timeline": timeline,
        "edit_receipt": (timeline or {}).get("edit_receipt"),
        "media_hints": (timeline or {}).get("media_hints") or {},
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
    """Natural-language → validated timeline mutation ops (Captions Co-editor).

    Purpose: a remote control for THIS Timeline project — not a general chatbot.
    The model may only emit allowlisted ops the Studio UI can apply. It cannot
    invent features, rewrite the renderer, or finish an export (user still Renders).
    """
    data = request.get_json(force=True) or {}
    prompt = (data.get("prompt") or "").strip()
    timeline = data.get("timeline") or {}
    client_ctx = data.get("context") or {}
    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400
    if not timeline:
        return jsonify({"error": "Timeline is required"}), 400

    main = (timeline.get("tracks") or {}).get("main") or []
    overlays = (timeline.get("tracks") or {}).get("overlay") or []
    music = (timeline.get("tracks") or {}).get("music") or []
    texts = (timeline.get("tracks") or {}).get("text") or []
    effects = (timeline.get("tracks") or {}).get("effects") or []
    style = timeline.get("style") or {}
    speaker_colors = timeline.get("speaker_colors") or {}
    summary = {
        "canvas": timeline.get("canvas"),
        "fit": timeline.get("fit"),
        "selection": client_ctx.get("selection"),
        "playhead_s": client_ctx.get("playhead_s"),
        "transcript_near_playhead": client_ctx.get("transcript_near_playhead"),
        "main_clip_count": len(main),
        "main_clips": [
            {
                "index": i,
                "seq": f"S{i + 1}",
                "shot_index": c.get("shot_index"),
                "id": c.get("id"),
                "in": c.get("in"),
                "out": c.get("out"),
                "has_punch_zoom": bool((c.get("punch_zoom") or {}).get("enabled")),
                "has_ken_burns": bool((c.get("ken_burns") or {}).get("enabled")),
                "color_preset": ((c.get("color") or c.get("color_grade") or {}).get("preset")),
                "cut_count": len(c.get("cuts") or []),
                "transition": (c.get("transition") or {}).get("type") if isinstance(c.get("transition"), dict) else c.get("transition"),
                "burn_captions": c.get("burn_captions"),
            }
            for i, c in enumerate(main[:24])
        ],
        "overlay_count": len(overlays),
        "overlay_clips": [
            {
                "index": i,
                "keyword": c.get("keyword"),
                "source": c.get("source"),
                "layout": c.get("layout"),
                "has_ken_burns": bool((c.get("ken_burns") or {}).get("enabled")),
                "start": c.get("start"),
                "duration": (float(c.get("out") or 0) - float(c.get("in") or 0)) if c.get("out") is not None else None,
            }
            for i, c in enumerate(overlays[:16])
        ],
        "music_clips": [
            {
                "index": i,
                "gain_db": c.get("gain_db", -18),
                "duck": bool(c.get("duck")),
                "start": c.get("start"),
            }
            for i, c in enumerate(music[:8])
        ],
        "text_clips": [
            {
                "index": i,
                "text": str(c.get("text") or "")[:80],
                "start": c.get("start"),
            }
            for i, c in enumerate(texts[:8])
        ],
        "effects_count": len(effects),
        "caption_style": {
            "font": style.get("font") or style.get("font_name"),
            "size": style.get("size") or style.get("font_size"),
            "primary": style.get("primary") or style.get("primary_color"),
            "highlight": style.get("highlight") or style.get("highlight_color"),
            "accent": style.get("accent") or style.get("accent_color"),
            "group": style.get("group") or style.get("group_size"),
            "position_y": style.get("position_y"),
            "punchword_emphasis": style.get("punchword_emphasis"),
        },
        "speaker_colors": speaker_colors,
        "clip_styles": [
            "talking_head", "split_stack", "pip_corner", "center_overlay",
            "word_emphasis", "hook_broll", "cinematic", "clarity",
        ],
        "overlay_layouts": ["pip_tr", "pip_tl", "pip_br", "pip_bl", "full", "center", "lower"],
    }

    system_prompt = f"""You are the Studio Timeline co-editor — a natural-language REMOTE CONTROL for this project.

PURPOSE
- User describes an edit in plain language → you return structured ops the UI applies now.
- You mutate project state only (captions look, shots, overlays, music, titles, styles).
- You are NOT a general assistant, NOT a video generator, and you cannot finish export.
- After ops apply, the user still clicks ▶ Render / Instant Export to bake the MP4.
- If they ask what you can do / how something works, return ops: [] and explain clearly in message.
- STORY / FEEL requests (hook → body → conclusion, "make Main feel punchy", "tighten the open"):
  brainstorm in message (ask 1–2 clarifying questions if needed), then emit concrete ops you CAN apply
  now (reorder_shot, apply_recommended_cuts, enable_punch_zoom, set_color_grade, suggest_broll,
  run_polish, set_caption_style, set_music). Do not pretend you rewrote the transcript.

HARD LIMITS (say no in message, ops: [])
- Cannot invent ops outside the allowlist.
- Cannot rewrite individual transcript words, approve pending B-roll one-by-one, or download files.
- Cannot change secrets/API keys or run arbitrary code.
- suggest_broll queues suggestions for review. Only use accept_all_broll when pending_broll_count > 0 and the user explicitly asks to accept/place them.
- run_polish starts an async FFmpeg polish job on the Main source (not Timeline Render). Say so in message; do not claim captions were burned.

Current timeline + selection:
{json.dumps(summary, indent=2)}

Return ONLY JSON:
{{
  "ops": [
    {{"op": "set_caption_style", "font": "Anton", "size": 64, "primary": "#FFFFFF", "highlight": "#FFD60A", "accent": "#00FF88", "group": 2, "position_y": 75, "punchword_emphasis": true}},
    {{"op": "set_speaker_colors", "SPEAKER_00": "#FFD700", "SPEAKER_01": "#00E5FF"}},
    {{"op": "delete_shot", "index": 2}},
    {{"op": "delete_clip", "track": "overlay", "index": 0}},
    {{"op": "set_transition", "index": 0, "type": "crossfade", "duration": 0.3}},
    {{"op": "enable_punch_zoom", "index": 1, "intensity": "med"}},
    {{"op": "enable_ken_burns", "index": 0, "intensity": "med", "direction": "in"}},
    {{"op": "enable_ken_burns", "track": "overlay", "index": 0, "intensity": "med", "direction": "in"}},
    {{"op": "clear_effects", "index": 0}},
    {{"op": "clear_effects", "track": "overlay", "index": 0}},
    {{"op": "set_canvas", "canvas": "9x16"}},
    {{"op": "set_fit", "fit": "cover"}},
    {{"op": "set_color_grade", "index": 0, "preset": "warm"}},
    {{"op": "add_title", "text": "Hello", "start": 0, "duration": 3}},
    {{"op": "set_text", "index": 0, "text": "New lower third"}},
    {{"op": "apply_recommended_cuts", "index": 0}},
    {{"op": "merge_shots", "index": 0}},
    {{"op": "reorder_shot", "from": 2, "to": 0}},
    {{"op": "reorder_shot", "from": "S3", "to": "S1"}},
    {{"op": "swap_shot", "a": "S1", "b": "S2"}},
    {{"op": "set_overlay_layout", "index": 0, "layout": "pip_tr"}},
    {{"op": "set_music", "index": 0, "gain_db": -20, "duck": true}},
    {{"op": "apply_clip_style", "style": "cinematic"}},
    {{"op": "suggest_broll", "mode": "photo", "placement": "center", "scope": "playhead", "use_ai": false}},
    {{"op": "accept_all_broll", "as_main": false}},
    {{"op": "skip_all_broll"}},
    {{"op": "split_at_playhead"}},
    {{"op": "enable_punch_zoom", "target": "selected", "intensity": "med"}},
    {{"op": "run_polish", "pacing": "fast", "broll_mode": "pip", "face_reframe": true}}
  ],
  "message": "Short confirmation (or explanation if ops empty). Mention Render when burn/style changed."
}}

Rules:
- Use only the ops listed above (examples show shapes; emit only what the user asked for).
- Indexes are 0-based. Main shots also carry seq like "S1", "S2" (timeline order, matching the S# badge on clips). For "put S2 first / swap S1 and S3", prefer reorder_shot or swap_shot with "S2"-style labels (or 0-based from/to).
- For "this / selected / current clip", set "target":"selected" (and track if needed).
- Prefer 1-8 ops. If unclear or out of scope, ops: [] + honest message.
- Colors #RRGGBB. Canvas: 9x16, 16x9, 1x1, 4x5. Fit: cover|contain. Color presets: none,neutral,warm,cool,vivid,bw.
- Multi-interview / multiple Main shots: prefer ONE shared grade (neutral or warm) on every Main index. Do not mix warm/cool/vivid across interviews for comedy. Only use bw when the user explicitly asks for black-and-white / stylized B&W.
- Caption color requests → set_caption_style. Host/Guest → set_speaker_colors.
- Overlay motion → enable_ken_burns track:"overlay". PiP / full-bleed → set_overlay_layout.
- Music volume/duck → set_music. CapCut clip looks → apply_clip_style.
- "find B-roll / suggest photos" → suggest_broll (review queue; do not claim clips were placed).
- "accept all B-roll / place suggestions" → accept_all_broll (only if pending_broll_count > 0). as_main:true for Main cutaways.
- "skip all suggestions" → skip_all_broll.
- "split here" → split_at_playhead (uses current playhead).
- "polish / polish cut / run polish" → run_polish. pacing: fast|informative|cinematic. broll_mode: pip|center. Optional keywords array. Not the same as ▶ Render.
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
        message = str(result.get("message") or "")[:500]

    allowed = {
        "set_caption_style", "set_speaker_colors", "delete_shot", "delete_clip",
        "set_transition", "enable_punch_zoom", "enable_ken_burns", "clear_effects",
        "set_canvas", "set_fit", "set_color_grade", "add_title", "set_text",
        "apply_recommended_cuts", "merge_shots", "reorder_shot", "swap_shot",
        "set_overlay_layout", "set_music", "apply_clip_style", "suggest_broll",
        "accept_all_broll", "skip_all_broll", "split_at_playhead", "run_polish",
    }
    ops = []
    for op in raw_ops:
        if not isinstance(op, dict):
            continue
        name = str(op.get("op") or "")
        if name not in allowed:
            continue
        ops.append(op)
        if len(ops) >= 12:
            break

    return jsonify({"ops": ops, "message": message or f"Applied {len(ops)} edit(s)."})


# ---- Polish cut (silence / jump-cut / B-roll master via scripts/polish_cut.py) ----
_polish_jobs: dict = {}


def _load_polish_cut_module():
    import importlib.util
    import sys
    path = BASE_DIR / "scripts" / "polish_cut.py"
    if not path.exists():
        raise RuntimeError("scripts/polish_cut.py missing")
    name = "studio_polish_cut"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _timeline_primary_source_job(timeline: dict) -> str | None:
    for c in ((timeline or {}).get("tracks") or {}).get("main") or []:
        sj = c.get("source_job_id")
        if sj and sj in jobs and find_video_path(sj):
            return sj
    return None


def _timeline_music_path(timeline: dict) -> Path | None:
    for c in ((timeline or {}).get("tracks") or {}).get("music") or []:
        aid = c.get("asset_id")
        if not aid:
            continue
        p = _find_asset_path(aid)
        if p and p.exists():
            return p
    return None


def _timeline_broll_keywords(timeline: dict, limit: int = 12) -> list[str]:
    kws = []
    for c in ((timeline or {}).get("tracks") or {}).get("overlay") or []:
        kw = (c.get("keyword") or "").strip()
        if kw and kw.lower() not in {x.lower() for x in kws}:
            kws.append(kw)
        if len(kws) >= limit:
            break
    return kws


def _run_polish_job(polish_id: str, opts: dict) -> None:
    job = _polish_jobs.get(polish_id)
    if not job:
        return
    stop_heartbeat = threading.Event()

    def _heartbeat():
        # UI was stuck at 25% for minutes during long FFmpeg — nudge while working.
        p = 25
        while not stop_heartbeat.wait(8.0):
            j = _polish_jobs.get(polish_id)
            if not j or j.get("status") != "running":
                return
            p = min(90, p + 5)
            j["progress"] = p

    try:
        job["status"] = "running"
        job["progress"] = 10
        # Prefer Studio's FFmpeg binary for polish_cut subprocesses.
        os.environ["FFMPEG"] = FFMPEG
        # Drop cached module so script fixes apply after pull without full mental model of importlib cache.
        import sys as _sys
        _sys.modules.pop("studio_polish_cut", None)
        mod = _load_polish_cut_module()
        job["progress"] = 25
        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()
        result = mod.run_polish(
            video=Path(opts["video"]),
            out=Path(opts["out"]),
            pacing_name=opts.get("pacing") or "fast",
            audio=Path(opts["audio"]) if opts.get("audio") else None,
            music=Path(opts["music"]) if opts.get("music") else None,
            words=opts.get("words") or [],
            keywords=opts.get("keywords") or [],
            assets_dir=Path(opts["assets_dir"]) if opts.get("assets_dir") else None,
            broll_mode=opts.get("broll_mode") or "pip",
            width=int(opts.get("width") or 1920),
            height=int(opts.get("height") or 1080),
            fps=int(opts.get("fps") or 60),
            face_reframe=bool(opts.get("face_reframe", True)),
            cut_stumbles=bool(opts.get("cut_stumbles", True)),
            lower_thirds=bool(opts.get("lower_thirds", False)),
            export_edl=bool(opts.get("export_edl", True)),
            silence_engine=str(opts.get("silence_engine") or "auto"),
            composite_engine=str(opts.get("composite_engine") or "ffmpeg"),
            export_nle=bool(opts.get("export_nle", True)),
            dry_run=False,
            report_path=Path(opts["report"]) if opts.get("report") else None,
        )
        stop_heartbeat.set()
        out_name = Path(result["output"]).name if result.get("output") else None
        job["status"] = "done"
        job["progress"] = 100
        job["output"] = out_name
        job["stats"] = (result.get("eval") or {}).get("stats") or {}
        job["warnings"] = (result.get("eval") or {}).get("warnings") or []
    except Exception as e:
        stop_heartbeat.set()
        job["status"] = "error"
        job["error"] = str(e)
        job["progress"] = 100
        print(f"[polish] {polish_id} failed: {e}", flush=True)


@app.route("/polish/ping", methods=["GET"])
def polish_ping():
    """Capability probe — UI can detect a stale Flask process after git pull."""
    ae = False
    mp = False
    try:
        import shutil
        ae = bool(shutil.which("auto-editor"))
    except Exception:
        pass
    try:
        import importlib.util
        mp = importlib.util.find_spec("moviepy") is not None
    except Exception:
        pass
    return jsonify({
        "ok": True,
        "polish": True,
        "endpoints": {"run": "/polish/run", "status": "/polish/status/<id>"},
        "engines": {"auto_editor": ae, "moviepy": mp},
        "note": "Polish is a source rough-cut; Timeline Render still burns captions.",
    })


@app.route("/polish/run", methods=["POST"])
@app.route("/timeline/polish", methods=["POST"])
def timeline_polish():
    """Kick off polish_cut on the Timeline's primary source take.

    Prefer POST /polish/run (avoids clashing with GET /timeline/<job_id>).
    Legacy alias: POST /timeline/polish.
    Poll `/polish/status/<polish_id>`; download via `/download/<output>`.
    """
    data = request.get_json(force=True) or {}
    job_id = data.get("job_id")
    if not job_id or job_id not in jobs:
        return jsonify({"error": "Unknown timeline job"}), 404
    timeline = _normalize_timeline(data.get("timeline") or jobs[job_id].get("timeline") or {})
    source_id = _timeline_primary_source_job(timeline)
    if not source_id:
        return jsonify({"error": "Add a Main clip with a transcribed source video first."}), 400
    video = find_video_path(source_id)
    if not video:
        return jsonify({"error": "Source video file missing on disk."}), 404

    pacing = str(data.get("pacing") or "fast").lower().strip()
    if pacing not in ("fast", "fast-paced", "cinematic", "informative"):
        pacing = "fast"
    broll_mode = str(data.get("broll_mode") or "pip").lower()
    if broll_mode not in ("pip", "center"):
        broll_mode = "pip"

    keywords = data.get("keywords")
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]
    if not keywords:
        keywords = _timeline_broll_keywords(timeline)
    words = list(jobs.get(source_id, {}).get("words") or [])
    music = _timeline_music_path(timeline)

    polish_id = uuid.uuid4().hex[:16]
    out_name = f"polish_{polish_id}.mp4"
    out_path = OUTPUT_DIR / out_name
    report_path = OUTPUT_DIR / f"polish_{polish_id}_report.json"

    _polish_jobs[polish_id] = {
        "status": "queued",
        "progress": 0,
        "output": None,
        "error": None,
        "timeline_job_id": job_id,
        "source_job_id": source_id,
        "pacing": pacing,
        "created_at": time.time(),
    }
    opts = {
        "video": str(video),
        "out": str(out_path),
        "report": str(report_path),
        "pacing": pacing,
        "words": words,
        "keywords": keywords,
        "assets_dir": str(ASSET_DIR),
        "broll_mode": broll_mode,
        "music": str(music) if music else None,
        "width": int(data.get("width") or 1920),
        "height": int(data.get("height") or 1080),
        "fps": int(data.get("fps") or 60),
        "face_reframe": data.get("face_reframe", True) is not False,
        "cut_stumbles": data.get("cut_stumbles", True) is not False,
        "lower_thirds": bool(data.get("lower_thirds") is True),
        "export_edl": data.get("export_edl", True) is not False,
        "silence_engine": str(data.get("silence_engine") or "auto"),
        "composite_engine": str(data.get("composite_engine") or "ffmpeg"),
        "export_nle": data.get("export_nle", True) is not False,
    }
    t = threading.Thread(target=_run_polish_job, args=(polish_id, opts), daemon=True)
    t.start()
    return jsonify({
        "polish_id": polish_id,
        "source_job_id": source_id,
        "pacing": pacing,
        "keywords": keywords,
        "has_music": bool(music),
        "word_count": len(words),
    })


@app.route("/timeline/polish", methods=["GET"])
def timeline_polish_get_hint():
    return jsonify({
        "error": "Use POST /polish/run to start a polish job",
        "hint": "If you saw HTTP 405 before, Stop+Run the Replit workflow after git pull",
    }), 405


@app.route("/polish/status/<polish_id>", methods=["GET"])
def polish_status(polish_id: str):
    job = _polish_jobs.get(polish_id)
    if not job:
        return jsonify({"error": "Unknown polish job"}), 404
    return jsonify({
        "polish_id": polish_id,
        "status": job.get("status"),
        "progress": job.get("progress") or 0,
        "output": job.get("output"),
        "error": job.get("error"),
        "stats": job.get("stats") or {},
        "warnings": job.get("warnings") or [],
        "pacing": job.get("pacing"),
        "source_job_id": job.get("source_job_id"),
    })


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
    polish_rules = sorted({str(r) for r in app.url_map.iter_rules() if "polish" in str(r)})
    print(f"[polish] registered routes: {polish_rules}", flush=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8081)),
            debug=False, threaded=True)
