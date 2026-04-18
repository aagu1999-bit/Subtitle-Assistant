"""
Subtitle Burner - A Flask web app that adds word-by-word highlighted
captions to videos using Whisper for transcription and FFmpeg for rendering.
"""
import os
import re
import uuid
import subprocess
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory

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

# ---- Job tracking (in-memory; fine for single-user Replit) ----
jobs = {}  # job_id -> {status, progress, output, error, words, video_ext}


def allowed_file(name: str) -> bool:
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def find_video_path(job_id: str) -> Path | None:
    """Find the uploaded video file for a job by trying all allowed extensions."""
    for ext in ALLOWED_EXT:
        candidate = UPLOAD_DIR / f"{job_id}.{ext}"
        if candidate.exists():
            return candidate
    return None


# ---- Whisper transcription with word-level timestamps ----
def transcribe(video_path: Path):
    """Return a list of word dicts: [{'word': str, 'start': float, 'end': float}, ...]"""
    from faster_whisper import WhisperModel

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        str(video_path),
        word_timestamps=True,
        vad_filter=True,
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


def build_ass(words, style: dict, video_w: int, video_h: int, emoji_rules: dict = None) -> str:
    font = style.get("font_name", "Montserrat Black")
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
Style: Default,{font},{font_size},{primary},{primary},{outline},&H00000000,0,0,0,0,100,100,0,0,1,{outline_w},{shadow},5,20,20,20,1

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

            start_ts = ass_timestamp(active["start"])
            end_ts = ass_timestamp(active["end"])

            line = (
                f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,"
                f"{{\\pos({pos_x},{pos_y})}}{text}"
            )
            lines.append(line)

    return header + "\n".join(lines) + "\n"


def get_video_dimensions(video_path: Path):
    """Use ffprobe to read width/height."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        str(video_path),
    ]
    out = subprocess.check_output(cmd).decode().strip()
    first_line = next((ln for ln in out.splitlines() if ln.strip()), "")
    parts = first_line.split(",")
    w, h = parts[0], parts[1]
    return int(w), int(h)


def build_audio_filter_chain(audio: dict) -> str | None:
    """Build an FFmpeg audio filter string from the enhancement options.

    Returns None when no enhancements are requested.
    Supported keys (all bool):
      noise_reduction  – afftdn spectral noise gate
      loudness_norm    – EBU R128 loudness normalisation
      voice_clarity    – parametric EQ boost for the 1–4 kHz voice range
    """
    filters = []
    if audio.get("noise_reduction"):
        filters.append("afftdn=nf=-25")
    if audio.get("voice_clarity"):
        filters.append("equalizer=f=2500:width_type=o:width=2:g=4")
    if audio.get("loudness_norm"):
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return ",".join(filters) if filters else None


def apply_audio_enhancements(video_path: Path, output_path: Path, af: str):
    """Run FFmpeg with the given audio filter chain, copying the video stream."""
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-vn",
        "-af", af,
        "-c:a", "aac",
        "-b:a", "192k",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg audio enhancement failed: {proc.stderr[-2000:]}")


def burn_subtitles(video_path: Path, ass_path: Path, output_path: Path, audio_path: Path | None = None):
    """Burn the ASS file into the video using FFmpeg.

    If *audio_path* is supplied, it replaces the audio stream from *video_path*
    with the already-processed audio from that file.
    """
    fonts_arg = str(FONT_DIR).replace("\\", "/").replace(":", r"\:")
    ass_arg = str(ass_path).replace("\\", "/").replace(":", r"\:")

    vf = f"subtitles='{ass_arg}':fontsdir='{fonts_arg}'"

    if audio_path:
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-c:a", "copy",
            str(output_path),
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-c:a", "copy",
            str(output_path),
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {proc.stderr[-2000:]}")


# ---- Background workers ----

def transcribe_job(job_id: str, video_path: Path):
    """Transcribe only — stores words and sets status to awaiting_edit."""
    try:
        jobs[job_id]["status"] = "transcribing"
        jobs[job_id]["progress"] = 30
        words = transcribe(video_path)
        if not words:
            raise RuntimeError("No speech detected in the video.")
        jobs[job_id]["words"] = words
        jobs[job_id]["status"] = "awaiting_edit"
        jobs[job_id]["progress"] = 100
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


def render_job(job_id: str, video_path: Path, words: list, style: dict, audio: dict, emoji_rules: dict):
    """Build ASS, optionally enhance audio, then burn subtitles."""
    try:
        jobs[job_id]["status"] = "building subtitles"
        jobs[job_id]["progress"] = 55
        w, h = get_video_dimensions(video_path)
        ass_content = build_ass(words, style, w, h, emoji_rules=emoji_rules)

        ass_path = UPLOAD_DIR / f"{job_id}.ass"
        ass_path.write_text(ass_content, encoding="utf-8")

        af = build_audio_filter_chain(audio)
        enhanced_audio_path = None
        if af:
            jobs[job_id]["status"] = "enhancing audio"
            jobs[job_id]["progress"] = 68
            enhanced_audio_path = UPLOAD_DIR / f"{job_id}_audio.aac"
            apply_audio_enhancements(video_path, enhanced_audio_path, af)

        jobs[job_id]["status"] = "rendering video"
        jobs[job_id]["progress"] = 80
        output_path = OUTPUT_DIR / f"{job_id}.mp4"
        burn_subtitles(video_path, ass_path, output_path, audio_path=enhanced_audio_path)

        jobs[job_id]["status"] = "done"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["output"] = output_path.name
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


def process_job(job_id: str, video_path: Path, style: dict, audio: dict | None = None):
    """Legacy single-phase worker used by the /upload route."""
    audio = audio or {}
    try:
        jobs[job_id]["status"] = "transcribing"
        jobs[job_id]["progress"] = 10
        words = transcribe(video_path)

        if not words:
            raise RuntimeError("No speech detected in the video.")

        render_job(job_id, video_path, words, style, audio, {})
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


# ---- Routes ----
@app.route("/")
def index():
    return render_template("index.html")


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
    }
    t = threading.Thread(target=transcribe_job, args=(job_id, video_path))
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

    # Validate word objects — each must have word (str), start (num), end (num)
    for i, w in enumerate(words):
        if not isinstance(w, dict):
            return jsonify({"error": f"Word at index {i} is not an object"}), 400
        if not isinstance(w.get("word"), str) or not w["word"].strip():
            return jsonify({"error": f"Word at index {i} missing valid 'word' field"}), 400
        if not isinstance(w.get("start"), (int, float)):
            return jsonify({"error": f"Word at index {i} missing numeric 'start' field"}), 400
        if not isinstance(w.get("end"), (int, float)):
            return jsonify({"error": f"Word at index {i} missing numeric 'end' field"}), 400

    video_path = find_video_path(job_id)
    if not video_path:
        return jsonify({"error": "Original video not found on server"}), 404

    # Reset job state for the render phase (keep words for potential re-renders)
    jobs[job_id]["status"] = "queued"
    jobs[job_id]["progress"] = 0
    jobs[job_id]["output"] = None
    jobs[job_id]["error"] = None

    t = threading.Thread(
        target=render_job,
        args=(job_id, video_path, words, style, audio, emoji_rules),
    )
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


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

    jobs[job_id] = {"status": "queued", "progress": 0, "output": None, "error": None, "words": None}
    t = threading.Thread(target=process_job, args=(job_id, video_path, style, audio))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(job)


@app.route("/download/<path:filename>")
def download(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


@app.route("/preview/<path:filename>")
def preview(filename):
    return send_from_directory(OUTPUT_DIR, filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
