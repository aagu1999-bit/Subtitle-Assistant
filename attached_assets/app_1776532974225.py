"""
Subtitle Burner - A Flask web app that adds word-by-word highlighted
captions to videos using Whisper for transcription and FFmpeg for rendering.
"""
import os
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
jobs = {}  # job_id -> {"status": "...", "progress": int, "output": str, "error": str}


def allowed_file(name: str) -> bool:
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED_EXT


# ---- Whisper transcription with word-level timestamps ----
def transcribe(video_path: Path):
    """Return a list of word dicts: [{'word': str, 'start': float, 'end': float}, ...]"""
    # Imported lazily so the server starts even if the model isn't downloaded yet
    from faster_whisper import WhisperModel

    # "base" is a good speed/accuracy trade-off for Replit.
    # Use "small" or "medium" for better accuracy if you have the resources.
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        str(video_path),
        word_timestamps=True,
        vad_filter=True,  # skip silent sections
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


def build_ass(words, style: dict, video_w: int, video_h: int) -> str:
    """
    Build an .ass subtitle file as a string.

    style keys:
      font_name, font_size, primary_color (#hex), highlight_color (#hex),
      outline_color (#hex), outline_width (int), position_y (0-100 %),
      all_caps (bool), group_size (int)
    """
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

    # ASS alignment: 2 = bottom-center. We use \pos to place precisely.
    pos_x = video_w // 2
    pos_y = int(video_h * (pos_y_pct / 100.0))

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
        group_start = group[0]["start"]
        group_end = group[-1]["end"]

        # For each word in the group, emit one Dialogue line in which that word
        # is rendered in the highlight color while the others use the primary color.
        for idx, active in enumerate(group):
            pieces = []
            for j, w in enumerate(group):
                word_text = fmt(w["word"])
                # ASS needs some escaping for braces; words rarely contain them
                word_text = word_text.replace("{", "").replace("}", "")
                if j == idx:
                    pieces.append(f"{{\\c{highlight}}}{word_text}{{\\c{primary}}}")
                else:
                    pieces.append(word_text)
            text = " ".join(pieces)

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
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    out = subprocess.check_output(cmd).decode().strip()
    w, h = out.split(",")
    return int(w), int(h)


def burn_subtitles(video_path: Path, ass_path: Path, output_path: Path):
    """Burn the ASS file into the video using FFmpeg."""
    # fontsdir points at our local /fonts folder so bundled TTFs are found
    fonts_arg = str(FONT_DIR).replace("\\", "/").replace(":", r"\:")
    ass_arg = str(ass_path).replace("\\", "/").replace(":", r"\:")

    vf = f"subtitles='{ass_arg}':fontsdir='{fonts_arg}'"

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "copy",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {proc.stderr[-2000:]}")


# ---- Background worker ----
def process_job(job_id: str, video_path: Path, style: dict):
    try:
        jobs[job_id]["status"] = "transcribing"
        jobs[job_id]["progress"] = 10
        words = transcribe(video_path)

        if not words:
            raise RuntimeError("No speech detected in the video.")

        jobs[job_id]["status"] = "building subtitles"
        jobs[job_id]["progress"] = 60
        w, h = get_video_dimensions(video_path)
        ass_content = build_ass(words, style, w, h)

        ass_path = UPLOAD_DIR / f"{job_id}.ass"
        ass_path.write_text(ass_content, encoding="utf-8")

        jobs[job_id]["status"] = "rendering video"
        jobs[job_id]["progress"] = 75
        output_path = OUTPUT_DIR / f"{job_id}.mp4"
        burn_subtitles(video_path, ass_path, output_path)

        jobs[job_id]["status"] = "done"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["output"] = output_path.name
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


# ---- Routes ----
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "video" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["video"]
    if f.filename == "" or not allowed_file(f.filename):
        return jsonify({"error": "Invalid file type"}), 400

    job_id = uuid.uuid4().hex
    ext = f.filename.rsplit(".", 1)[1].lower()
    video_path = UPLOAD_DIR / f"{job_id}.{ext}"
    f.save(str(video_path))

    # Read style JSON from the form
    import json

    style = json.loads(request.form.get("style", "{}"))

    jobs[job_id] = {"status": "queued", "progress": 0, "output": None, "error": None}
    t = threading.Thread(target=process_job, args=(job_id, video_path, style))
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
    # 0.0.0.0 so Replit's proxy can reach it
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
