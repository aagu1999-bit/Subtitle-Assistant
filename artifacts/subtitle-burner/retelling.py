"""Retelling-first edit decisions.

Pure functions over a word list. No Flask, no FFmpeg, no style packs.
The caller loads audio samples and face boxes and passes them in.

A graphic stays off unless a kept line fails by ear, fails the mute test,
or is doing one of the six rhetorical jobs. The template follows that job.
Nothing is gated by a pack allowlist or a format rubric.
"""
from __future__ import annotations

import math
import re

# The six pacing packs. They are not a choice anymore. Callers that still
# send one of these ids are resolved to the single default look elsewhere.
DELETED_STYLE_PACKS = ("pulse", "clarity", "magazine", "film", "velocity", "always")

MOTIONS = ("hold", "rise", "count", "snap")
TEMPLATES = (
    "search_bar", "message_card", "counter", "quote_card", "name_card", "media_card",
)
EAR_BLIND = ("number", "price", "name", "receipt")
VISUAL_PROOF = ("mechanism", "ui", "before_after", "story_texture", "action")
BEATS = ("situation", "turn", "consequence")
PRESENTATIONS = ("framed_card", "fullscreen_replace", "corner_note", "split_screen")
SPINE_DYNAMICS = ("claim_proof", "story_arc", "escalation")
REACTION_DELAY_S = 1.5

# Rhetorical job, not the verb the speaker happened to say.
FUNCTIONS = {
    "search_bar": ("question", "curiosity", "cultural_reference"),
    "message_card": ("objection", "monologue", "callback", "contradiction"),
    "counter": ("stakes", "scale", "delta", "listing"),
    "quote_card": ("thesis", "mic_drop", "deadpan_echo"),
    "name_card": ("proper_noun", "price", "tool", "venue"),
    "media_card": ("mechanism", "ui", "before_after", "story_texture", "action"),
}
# A null template is a stock or library asset with no card chrome.
_MEDIA_FUNCTIONS = FUNCTIONS["media_card"]
_MODES = {
    "search_bar": ("framed_card", "corner_note"),
    "message_card": ("framed_card", "split_screen"),
    "counter": ("framed_card",),
    "quote_card": ("framed_card", "corner_note"),
    "name_card": ("framed_card", "corner_note"),
    "media_card": ("framed_card", "fullscreen_replace", "split_screen"),
}

# Facts a claim_proof graphic is allowed to anchor. Story texture and
# literal action belong on a story arc or an escalation, not a tight proof cut.
_CLAIM_FACTS = ("number", "price", "name", "receipt", "mechanism", "ui", "before_after")

# A bare verb is not a fact. These used to open a search bar or a chat thread.
_BARE = frozenset({
    "google", "googled", "search", "searched", "searching",
    "text", "texted", "texting", "message", "messaged", "messages",
    "chat", "chatted", "dm", "dms",
})
_STOP = frozenset(
    "a an the and or to of i you he she it we they me my is was be "
    "for on in at by that this with from as".split()
)
_HOSTS = frozenset({"host", "interviewer"})
_GUESTS = frozenset({"guest", "respondent"})

_PRICE_RE = re.compile(
    r"(?:\$|£|€)\s?\d|\b\d[\d,.]*(?:\s?(?:dollars|dollar|bucks|usd|euros|euro|pounds|pound))\b",
    re.I,
)
_NUMBER_RE = re.compile(
    r"\b\d[\d,.]*\s?(?:%|percent|x|k|m)?\b|"
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundred|thousand|million|billion)\b",
    re.I,
)
_ICON_RE = re.compile(r"[a-z0-9_]+")

DEFAULT_TOKENS = {
    "id": "default",
    "colors": {
        "ink": "#F8FAFC",
        "accent": "#FFD60A",
        "card": "#0F1220",
        "card_ink": "#F8FAFC",
        "stroke": "#F8FAFC",
    },
    "font": {"ui": "DM Sans", "display": "Anton"},
    "radius": 18,
    "stroke": 2,
    "card": {"pad": 28, "max_w": 0.86},
    "motion": list(MOTIONS),
    "templates": list(TEMPLATES),
}


def tokens() -> dict:
    """The one look. A copy, so a caller cannot edit the constant."""
    return {
        "id": DEFAULT_TOKENS["id"],
        "colors": dict(DEFAULT_TOKENS["colors"]),
        "font": dict(DEFAULT_TOKENS["font"]),
        "radius": DEFAULT_TOKENS["radius"],
        "stroke": DEFAULT_TOKENS["stroke"],
        "card": dict(DEFAULT_TOKENS["card"]),
        "motion": list(DEFAULT_TOKENS["motion"]),
        "templates": list(DEFAULT_TOKENS["templates"]),
    }


# ---- words ---------------------------------------------------------------

def assign_word_ids(words: list) -> list[dict]:
    """Stamp `w0000` ids. The index is the position in this list."""
    out = []
    for i, w in enumerate(words or []):
        row = dict(w)
        row["id"] = f"w{i:04d}"
        out.append(row)
    return out


def word_index(word_id: str) -> int:
    m = re.fullmatch(r"w(\d{4})", str(word_id or ""))
    if not m:
        raise ValueError(f"bad word id {word_id!r}")
    return int(m.group(1))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def span_text(words: list, start_id: str, end_id: str) -> str:
    a, b = word_index(start_id), word_index(end_id)
    if a > b or a < 0 or b >= len(words):
        raise ValueError("word span is outside the transcript")
    return _norm(" ".join(str(words[i].get("word") or "") for i in range(a, b + 1)))


def _sentence_count(text: str) -> int:
    parts = [p for p in re.split(r"[.!?]+", _norm(text)) if p.strip()]
    return len(parts)


def _word_times(words: list, start_id: str, end_id: str) -> tuple[float, float]:
    a, b = word_index(start_id), word_index(end_id)
    return float(words[a]["start"]), float(words[b]["end"])


# ---- speakers ------------------------------------------------------------

def _labels(words: list) -> list[str]:
    found = []
    for w in words or []:
        sp = str(w.get("speaker") or "").strip()
        if sp and sp not in found:
            found.append(sp)
    return found


def _multi(words: list) -> bool:
    return len(_labels(words)) > 1


def _is_host(label: str) -> bool:
    return str(label or "").strip().lower() in _HOSTS


def _is_guest(label: str, labels: list[str]) -> bool:
    low = str(label or "").strip().lower()
    if low in _GUESTS:
        return True
    if any(_is_host(s) for s in labels) and label and not _is_host(label):
        return True
    named = any(_is_host(s) or s.lower() in _GUESTS for s in labels)
    return bool(label) and not named and len(labels) > 1


def _guest_opening(words: list, start_id: str) -> bool:
    """True when this word is the first word of a guest (or incoming) turn."""
    if not _multi(words):
        return False
    idx = word_index(start_id)
    labels = _labels(words)
    label = str(words[idx].get("speaker") or "")
    if not _is_guest(label, labels):
        return False
    if idx == 0:
        return label.strip().lower() in _GUESTS
    prev = str(words[idx - 1].get("speaker") or "")
    return prev != label


def _span_is_host(words: list, start_id: str, end_id: str) -> bool:
    if not _multi(words) or not any(_is_host(s) for s in _labels(words)):
        return False
    a, b = word_index(start_id), word_index(end_id)
    speakers = [str(words[i].get("speaker") or "") for i in range(a, b + 1)]
    return bool(speakers) and all(_is_host(s) for s in speakers)


def _times_overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def _cross_talk(words: list, start_id: str, end_id: str) -> bool:
    """True when a word in the span is spoken over a different speaker."""
    if not _multi(words):
        return False
    a, b = word_index(start_id), word_index(end_id)
    for i in range(a, b + 1):
        w = words[i]
        try:
            ws, we = float(w["start"]), float(w["end"])
        except (TypeError, ValueError, KeyError):
            continue
        sp = str(w.get("speaker") or "")
        if not sp:
            continue
        for other in words:
            osp = str(other.get("speaker") or "")
            if not osp or osp == sp:
                continue
            try:
                os_, oe = float(other["start"]), float(other["end"])
            except (TypeError, ValueError, KeyError):
                continue
            if _times_overlap(ws, we, os_, oe):
                return True
    return False


# ---- picker --------------------------------------------------------------

def _candidate(raw: dict, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"candidate {index} is not an object")
    audience = _norm(raw.get("audience"))
    retelling = _norm(raw.get("retelling"))
    if not audience:
        raise ValueError(f"candidate {index} has no audience")
    if not retelling:
        raise ValueError(f"candidate {index} has no retelling")
    n = _sentence_count(retelling)
    if n < 1 or n > 2:
        raise ValueError(f"candidate {index} retelling must be one or two sentences")
    if not isinstance(raw.get("lead_with_turn"), bool):
        raise ValueError(f"candidate {index} lead_with_turn must be true or false")
    dynamic = raw.get("spine_dynamic")
    if dynamic not in SPINE_DYNAMICS:
        raise ValueError(
            f"candidate {index} spine_dynamic must be claim_proof, story_arc, or escalation"
        )
    beats = raw.get("beats") or {}
    if not isinstance(beats, dict):
        raise ValueError(f"candidate {index} beats must be an object")
    cleaned = {}
    for role in BEATS:
        line = _norm(beats.get(role))
        if not line:
            raise ValueError(f"candidate {index} is missing the {role}")
        cleaned[role] = line
    cid = _norm(raw.get("id")) or f"r{index + 1}"
    return {
        "id": cid,
        "audience": audience,
        "retelling": retelling,
        "lead_with_turn": raw["lead_with_turn"],
        "spine_dynamic": dynamic,
        "beats": cleaned,
    }


def normalize_picker(payload: dict) -> dict:
    """A picker response. `style_pack` is not part of the schema and is dropped."""
    if not isinstance(payload, dict):
        raise ValueError("picker response must be an object")
    raw = payload.get("candidates")
    if not isinstance(raw, list) or not 2 <= len(raw) <= 3:
        raise ValueError("propose two or three retellings")
    return {"version": 2, "candidates": [_candidate(c, i) for i, c in enumerate(raw)]}


def _transcript_lines(words: list) -> str:
    lines = []
    for w in assign_word_ids(words):
        sp = w.get("speaker") or ""
        who = f" {sp}" if sp else ""
        lines.append(f"{w['id']}{who} {float(w.get('start', 0)):.2f} {w.get('word', '')}")
    return "\n".join(lines)


def picker_prompt(words: list) -> str:
    return (
        "You are an editorial story cutter. Find the retellings a specific "
        "stranger would repeat. Do not pace the video. Do not decorate lines. "
        "Do not pick a format rubric for an interview, a hang, or a rant.\n\n"
        "Propose two or three retellings this tape actually supports. Do not "
        "invent a comedic, contrarian, or tutorial angle the words do not contain.\n"
        "Each candidate:\n"
        "- audience: the specific stranger\n"
        "- retelling: one or two sentences, using what the speaker said\n"
        "- lead_with_turn: true only when the turn is understandable with no "
        "situation in front of it. Otherwise false.\n"
        "- spine_dynamic: claim_proof, story_arc, or escalation, whichever matches "
        "the tape.\n"
        "  claim_proof: a tight cut. Graphics anchor facts, metrics, and receipts.\n"
        "  story_arc: keep a tangent that pays off at the callback. Do not trim "
        "that detour as off-topic.\n"
        "  escalation: keep a rapid-fire buildup. A sudden silence after a loud "
        "stretch is a deadpan pause, not dead air.\n"
        "- beats.situation, beats.turn, beats.consequence: one line each\n\n"
        'Return JSON: {"version": 2, "candidates": [...]}\n'
        "Do not return a style pack, a format, a score, or a graphic.\n\n"
        "Transcript:\n" + _transcript_lines(words)
    )


# ---- graphics gate -------------------------------------------------------

def _bare(text: str) -> bool:
    tok = re.sub(r"[^a-z0-9']", "", _norm(text).lower())
    return tok in _BARE


def fact_in_span(span: str, fact: str) -> bool:
    needle = _norm(fact).lower()
    return bool(needle) and needle in _norm(span).lower()


def _matches_fact(fact: str, text: str) -> bool:
    if fact == "price":
        return bool(_PRICE_RE.search(text))
    if fact == "number":
        return bool(_NUMBER_RE.search(text))
    return True


def _functions_for(template) -> tuple:
    if template is None or template == "media_card":
        return _MEDIA_FUNCTIONS
    return FUNCTIONS.get(template, ())


def gate_justification(span: str, justification: dict, template) -> str | None:
    """None when the graphic may exist. Otherwise a drop reason.

    The template follows the rhetorical job. A bare "google" or "texted"
    is still not a graphic. Visual proof has to be a media card or no card.
    """
    if not isinstance(justification, dict):
        return "no_justification"
    kind = str(justification.get("kind") or "")
    fact = str(justification.get("fact") or "")
    text = _norm(justification.get("text"))
    why = _norm(justification.get("why"))
    function = str(justification.get("function") or "")
    if not text or not why:
        return "no_justification"
    if not fact_in_span(span, text):
        return "fact_not_in_span"
    if _bare(text):
        return "template_forbidden"
    if template not in TEMPLATES and template is not None:
        return "template_forbidden"
    if function not in _functions_for(template):
        return "template_forbidden"
    if kind == "visual_proof":
        if fact not in VISUAL_PROOF:
            return "no_justification"
        if template not in ("media_card", None):
            return "template_forbidden"
        return None
    if kind == "ear_blind":
        if fact not in EAR_BLIND:
            return "no_justification"
        if not _matches_fact(fact, text):
            return "fact_not_in_span"
        if fact == "number" and template != "counter":
            return "template_forbidden"
        if fact == "price" and template not in ("counter", "name_card"):
            return "template_forbidden"
        if fact == "name" and template != "name_card":
            return "template_forbidden"
        if fact == "receipt" and template not in ("message_card", "quote_card"):
            return "template_forbidden"
        return None
    if template in ("media_card", None):
        return "no_justification"
    if template == "counter" and function != "listing":
        if not (_NUMBER_RE.search(text) or _PRICE_RE.search(text)):
            return "fact_not_in_span"
    if template == "name_card" and function == "price" and not _PRICE_RE.search(text):
        return "fact_not_in_span"
    return None


def _presentation(template, mode: str) -> str | None:
    if mode not in PRESENTATIONS:
        return "template_forbidden"
    key = "media_card" if template is None else template
    if mode not in _MODES.get(key, ()):
        return "template_forbidden"
    return None


def _anchors_claim(justification: dict) -> bool:
    kind = str(justification.get("kind") or "")
    fact = str(justification.get("fact") or "")
    return kind in ("ear_blind", "visual_proof") and fact in _CLAIM_FACTS


# ---- face collision ------------------------------------------------------

def _rect(box: dict) -> tuple[float, float, float, float]:
    return (
        float(box.get("x", 0)),
        float(box.get("y", 0)),
        float(box.get("w", box.get("width", 0)) or 0),
        float(box.get("h", box.get("height", 0)) or 0),
    )


def _overlaps(a, b, pad: float = 0.0) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ax, ay = ax - pad, ay - pad
    aw, ah = aw + pad * 2, ah + pad * 2
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def _band_top(caption_band: dict | None) -> float:
    if not isinstance(caption_band, dict):
        return 0.72
    if caption_band.get("top") is not None:
        return float(caption_band["top"])
    return float(caption_band.get("y", 0.72))


def place_off_face(rect: dict, face_boxes: list | None,
                   caption_band: dict | None = None, pad: float = 0.02) -> dict:
    """Move a graphic to the clearer of top_band and above_captions_band.

    top_band sits above the head. above_captions_band sits between the chin
    and the captions. If neither is clear, the graphic is dropped. The
    behind-the-speaker matte is never turned on.
    """
    box = _rect(rect or {})
    faces = [_rect(f) for f in (face_boxes or []) if isinstance(f, dict)]

    def pack(zone, cand, collision):
        return {
            "face_collision": collision,
            "placement_zone": zone,
            "rect": {"x": cand[0], "y": cand[1], "w": cand[2], "h": cand[3]},
            "dropped": False,
            "use_person_matte": False,
        }

    hit = any(_overlaps(box, f, pad) for f in faces)
    if not hit:
        return pack((rect or {}).get("placement_zone") or "clear", box, False)

    x, _y, w, h = box
    caption_top = _band_top(caption_band)
    face_top = min(f[1] for f in faces)
    face_bot = max(f[1] + f[3] for f in faces)
    widths = ((x, w), (0.04, min(w, 0.42)), (0.54, min(w, 0.42)))
    found = []

    def consider(zone, cand, clearance):
        if cand[1] < 0.02 - 1e-6 or cand[1] + cand[3] > caption_top - 0.01:
            return
        if any(_overlaps(cand, f, pad) for f in faces):
            return
        found.append((clearance, 0 if zone == "top_band" else 1, zone, cand))

    max_bottom = face_top - pad
    if max_bottom - h >= 0.02:
        for ytry in (max(0.04, max_bottom - h), 0.04):
            if ytry + h > max_bottom + 1e-6:
                continue
            for xtry, wtry in widths:
                cand = (xtry, ytry, wtry, h)
                consider("top_band", cand, face_top - (cand[1] + cand[3]))

    min_top = face_bot + pad
    if min_top + h <= caption_top - 0.01:
        for xtry, wtry in widths:
            cand = (xtry, min_top, wtry, h)
            gap = min(cand[1] - face_bot, caption_top - (cand[1] + cand[3]))
            consider("above_captions_band", cand, gap)

    if not found:
        return {
            "face_collision": True,
            "placement_zone": None,
            "rect": None,
            "dropped": True,
            "reason": "covers_face_no_band",
            "use_person_matte": False,
        }
    found.sort(key=lambda item: (-item[0], item[1]))
    _clear, _tie, zone, cand = found[0]
    return pack(zone, cand, True)


# ---- waveform snap -------------------------------------------------------

def _rms_track(samples, sr: int, hop: float = 0.01) -> list[tuple[float, float]]:
    n = max(1, int(sr * hop))
    out = []
    step = n
    for i in range(0, len(samples), step):
        chunk = samples[i:i + n]
        if len(chunk) == 0:
            break
        acc = 0.0
        for v in chunk:
            acc += float(v) * float(v)
        out.append((i / float(sr), math.sqrt(acc / len(chunk))))
    return out


def _speech_edges(track, thresh: float) -> tuple[list[float], list[float]]:
    onsets, decays = [], []
    was = False
    for ts, rms in track:
        now = rms >= thresh
        if now and not was:
            onsets.append(ts)
        elif was and not now:
            decays.append(ts)
        was = now
    if was and track:
        decays.append(track[-1][0])
    return onsets, decays


def _clamp_pause(t: float, snapped: float, protect) -> float:
    """Stay on this side of a protected pause, or inside it. Do not exit the far side."""
    if not protect:
        return snapped
    try:
        a, b = float(protect[0]), float(protect[1])
    except (TypeError, ValueError, IndexError):
        return snapped
    if b < a:
        a, b = b, a
    if t >= b:
        return max(snapped, a)
    if t <= a:
        return min(snapped, b)
    return min(max(snapped, a), b)


def snap_boundary(samples, sr: int, t: float, side: str, *,
                  cap_s: float = 0.20, window_s: float = 0.25,
                  protect=None) -> float:
    """Snap to the nearest speech onset or decay inside a ±250ms window.

    The move is at most ±200ms, either earlier or later. A protected pause
    may be entered and must not be crossed. Empty audio leaves `t` alone.
    An edge you cannot reach without passing the cap is left alone.
    """
    t = float(t)
    if samples is None or sr <= 0 or len(samples) == 0:
        return t
    if side not in ("in", "out"):
        raise ValueError("side must be 'in' or 'out'")
    track = _rms_track(samples, sr)
    if not track:
        return t
    hi = max(r for _, r in track)
    if hi < 1e-6:
        return t
    onsets, decays = _speech_edges(track, hi * 0.25)
    edges = onsets if side == "in" else decays
    search = max(float(window_s), float(cap_s))
    windowed = [e for e in edges if abs(e - t) <= search + 1e-6]
    if not windowed:
        snapped = t
    else:
        nearest = min(windowed, key=lambda e: (abs(e - t), e))
        snapped = nearest if abs(nearest - t) <= float(cap_s) + 1e-6 else t
    return round(_clamp_pause(t, snapped, protect), 3)


def find_deadpan_drops(samples, sr: int, start: float, end: float) -> list[dict]:
    """Silence that follows a loud stretch inside a kept beat.

    A drop is quiet for at least ~180ms after the energy was high. Used to
    protect a deadpan pause on an escalation, not to choose which lines stay.
    """
    if samples is None or sr <= 0 or end <= start:
        return []
    track = [(ts, rms) for ts, rms in _rms_track(samples, sr)
             if start - 1e-6 <= ts <= end + 1e-6]
    if len(track) < 3:
        return []
    hi = max(rms for _, rms in track)
    if hi < 1e-6:
        return []
    loud, quiet = hi * 0.55, hi * 0.20
    drops = []
    seen_loud = False
    i = 0
    while i < len(track):
        ts, rms = track[i]
        if rms >= loud:
            seen_loud = True
            i += 1
            continue
        if seen_loud and rms < quiet:
            j = i
            while j < len(track) and track[j][1] < quiet:
                j += 1
            a, b = track[i][0], track[j - 1][0]
            if b - a >= 0.18:
                drops.append({"start": round(a, 3), "end": round(b, 3)})
            seen_loud = False
            i = j
            continue
        i += 1
    return drops


# ---- spine ---------------------------------------------------------------

def _order(lead: bool) -> list[str]:
    if lead:
        return ["turn", "situation", "consequence"]
    return ["situation", "turn", "consequence"]


def _speaker_change(words: list, start_id: str) -> bool:
    idx = word_index(start_id)
    if idx <= 0:
        return False
    prev = words[idx - 1].get("speaker")
    cur = words[idx].get("speaker")
    return bool(prev and cur and str(prev) != str(cur))


def _content_words(text: str) -> set[str]:
    out = set()
    for tok in re.findall(r"[a-z0-9']+", _norm(text).lower()):
        if len(tok) > 3 and tok not in _STOP:
            out.add(tok)
    return out


def _beat(raw: dict, words: list, dynamic: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("a beat must be an object")
    role = str(raw.get("role") or "")
    if role not in BEATS:
        raise ValueError(f"unknown beat {role!r}")
    text = span_text(words, raw.get("start_word_id"), raw.get("end_word_id"))
    claimed = _norm(raw.get("text"))
    if claimed and claimed.lower() not in text.lower() and text.lower() not in claimed.lower():
        raise ValueError(f"{role} text is not the words in that span")
    punch = raw.get("punch_in") is True
    if punch and role == "situation" and not _speaker_change(words, raw["start_word_id"]):
        raise ValueError("a punch-in is only allowed on the turn, the consequence, or a speaker change")
    if punch and _cross_talk(words, raw["start_word_id"], raw["end_word_id"]):
        punch = False
    before = int(raw.get("protect_pause_before_ms") or 0)
    after = int(raw.get("protect_pause_after_ms") or 0)
    deadpan = raw.get("deadpan") is True
    if role != "turn" and not (dynamic == "escalation" and deadpan):
        before = 0
        after = 0
        deadpan = False
    if role == "turn":
        deadpan = deadpan or before > 0 or after > 0
    if before < 0 or after < 0:
        raise ValueError("a protected pause cannot be negative")
    return {
        "role": role,
        "start_word_id": raw["start_word_id"],
        "end_word_id": raw["end_word_id"],
        "text": text,
        "protect_pause_before_ms": before,
        "protect_pause_after_ms": after,
        "deadpan": deadpan if role != "turn" else bool(raw.get("deadpan") is True),
        "punch_in": punch,
    }


def _ranges_overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 <= b1 and b0 <= a1


def _detour(raw: dict, words: list, beats: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("a detour must be an object")
    text = span_text(words, raw.get("start_word_id"), raw.get("end_word_id"))
    a, b = word_index(raw["start_word_id"]), word_index(raw["end_word_id"])
    for beat in beats.values():
        if _ranges_overlap(a, b, word_index(beat["start_word_id"]), word_index(beat["end_word_id"])):
            raise ValueError("a detour has to sit outside the three beats")
    shared = _content_words(text) & _content_words(beats["consequence"]["text"])
    if not shared:
        raise ValueError("a detour has to pay off in the callback")
    return {
        "start_word_id": raw["start_word_id"],
        "end_word_id": raw["end_word_id"],
        "text": text,
        "pays_off": True,
    }


def _clean_justification(just: dict) -> dict:
    cleaned = {
        "function": str(just.get("function") or ""),
        "text": _norm(just.get("text")),
        "why": _norm(just.get("why")),
    }
    if just.get("kind"):
        cleaned["kind"] = str(just["kind"])
    if just.get("fact"):
        cleaned["fact"] = str(just["fact"])
    if just.get("reading_aloud") is True:
        cleaned["reading_aloud"] = True
    return cleaned


def _graphic(raw: dict, words: list, beats: dict, dynamic: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("a graphic must be an object")
    role = str(raw.get("beat_role") or "")
    if role not in beats:
        raise ValueError("a graphic must sit on a kept beat")
    template = raw.get("template")
    if template not in TEMPLATES and template is not None:
        raise ValueError(f"unknown template {template!r}")
    span = span_text(words, raw.get("start_word_id"), raw.get("end_word_id"))
    beat = beats[role]
    if word_index(raw["start_word_id"]) < word_index(beat["start_word_id"]):
        raise ValueError("graphic starts before its beat")
    if word_index(raw["end_word_id"]) > word_index(beat["end_word_id"]):
        raise ValueError("graphic ends after its beat")
    why = gate_justification(span, raw.get("justification"), template)
    if why:
        raise ValueError(why)
    if dynamic == "claim_proof" and not _anchors_claim(raw.get("justification") or {}):
        raise ValueError("claim_proof graphics anchor a fact, a metric, or a receipt")
    mode = raw.get("presentation_mode") or "framed_card"
    bad = _presentation(template, mode)
    if bad:
        raise ValueError(bad)
    if raw.get("use_person_matte") is True:
        raise ValueError("person_matte")
    miss = raw.get("on_asset_miss") or "raw_speaker"
    if miss != "raw_speaker":
        raise ValueError("on_asset_miss")
    if _span_is_host(words, raw["start_word_id"], raw["end_word_id"]):
        if template not in ("name_card", "search_bar"):
            raise ValueError("host_setup")
    if _cross_talk(words, raw["start_word_id"], raw["end_word_id"]):
        raise ValueError("cross_talk")
    icon = None
    if template == "name_card" and raw.get("icon"):
        icon = str(raw["icon"])
        if not _ICON_RE.fullmatch(icon):
            raise ValueError("icon must be a Lucide name")
    delay = 0.0
    if mode == "fullscreen_replace" and _guest_opening(words, raw["start_word_id"]):
        try:
            asked = float(raw.get("reaction_delay_s") or 0)
        except (TypeError, ValueError):
            asked = 0.0
        delay = max(asked, REACTION_DELAY_S)
    ids = raw.get("source_word_ids")
    if not isinstance(ids, list) or not ids:
        ids = [raw["start_word_id"]]
    for wid in ids:
        word_index(wid)
    out = {
        "id": _norm(raw.get("id")) or "g1",
        "origin": "retelling",
        "beat_role": role,
        "start_word_id": raw["start_word_id"],
        "end_word_id": raw["end_word_id"],
        "justification": _clean_justification(raw["justification"]),
        "template": template,
        "presentation_mode": mode,
        "placement_zone": raw.get("placement_zone") or "above_captions_band",
        "face_collision": bool(raw.get("face_collision")),
        "use_person_matte": False,
        "on_asset_miss": "raw_speaker",
        "reaction_delay_s": delay,
        "spine_dynamic": dynamic,
        "source_word_ids": [str(i) for i in ids],
    }
    if icon:
        out["icon"] = icon
    return out


def _unwrap_spine(payload: dict) -> dict:
    """A model sometimes nests the cut, or sends the picker object back."""
    beats = payload.get("beats")
    if isinstance(beats, (list, dict)) and beats:
        return payload
    for key in ("spine", "plan", "cut"):
        inner = payload.get(key)
        if isinstance(inner, dict) and inner.get("beats"):
            merged = dict(inner)
            for field in ("audience", "retelling", "lead_with_turn", "spine_dynamic"):
                if field not in merged and field in payload:
                    merged[field] = payload[field]
            return merged
    cands = payload.get("candidates")
    if isinstance(cands, list) and cands and isinstance(cands[0], dict) and cands[0].get("beats"):
        merged = dict(cands[0])
        for field in ("audience", "retelling", "lead_with_turn", "spine_dynamic"):
            if field not in merged and field in payload:
                merged[field] = payload[field]
        return merged
    return payload


def _word_token(word) -> str:
    return re.sub(r"[^a-z0-9']", "", str(word or "").lower())


def _line_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", _norm(text).lower())


def _range_blocked(start: int, end: int, avoid: list) -> bool:
    mid = (start + end) / 2
    return any(a <= mid <= b for a, b in avoid)


def _best_span(words: list, line: str, avoid: list) -> tuple[int, int] | None:
    """Word indexes of the transcript span a prose line is talking about."""
    full = _line_tokens(line)
    if not full:
        return None
    toks = [_word_token(w.get("word")) for w in words]
    n = len(full)
    if n <= 40:
        for i in range(0, len(toks) - n + 1):
            if toks[i:i + n] == full and not _range_blocked(i, i + n - 1, avoid):
                return i, i + n - 1
    needles = {t for t in full if len(t) > 2 and t not in _STOP} or set(full)
    if not needles:
        return None
    need = min(len(needles), max(2, (len(needles) + 1) // 2)) if len(needles) > 1 else 1
    max_w = min(len(words), max(8, len(needles) * 6))
    best = None
    for i in range(len(words)):
        seen = set()
        limit = min(len(words), i + max_w)
        for j in range(i, limit):
            if toks[j] in needles:
                seen.add(toks[j])
            if len(seen) < need or _range_blocked(i, j, avoid):
                continue
            cand = (len(seen), -(j - i), -i, i, j)
            if best is None or cand > best:
                best = cand
    if best is None:
        return None
    return best[3], best[4]


def _beat_spec_list(raw_beats, words: list) -> list:
    """Accept word-id spans, or the prose map the picker already uses.

    Models often send ``{"situation": "...", "turn": "...", "consequence": "..."}``
    instead of an array of word ids. Those sentences are matched back onto
    the transcript. The paraphrase itself is not stored as the beat text.
    """
    if isinstance(raw_beats, list) and raw_beats and all(
        isinstance(b, dict) and b.get("start_word_id") and b.get("end_word_id")
        for b in raw_beats
    ):
        return raw_beats
    specs = []
    if isinstance(raw_beats, dict):
        for role in ("turn", "situation", "consequence"):
            if role in raw_beats:
                specs.append((role, raw_beats[role]))
    elif isinstance(raw_beats, list):
        ordered = []
        for item in raw_beats:
            if isinstance(item, dict):
                ordered.append((str(item.get("role") or ""), item))
        # The turn is matched first so a later summary cannot sit on its words.
        specs = sorted(ordered, key=lambda pair: {"turn": 0, "situation": 1, "consequence": 2}.get(pair[0], 9))
    else:
        return []
    avoid = []
    out = []
    for role, spec in specs:
        if role not in BEATS:
            continue
        if isinstance(spec, dict) and spec.get("start_word_id") and spec.get("end_word_id"):
            a, b = word_index(spec["start_word_id"]), word_index(spec["end_word_id"])
            avoid.append((a, b))
            out.append(spec)
            continue
        text = spec if isinstance(spec, str) else _norm((spec or {}).get("text") or (spec or {}).get("line"))
        span = _best_span(words, text, avoid)
        if span is None:
            raise ValueError(f"could not find the {role} in the transcript")
        a, b = span
        avoid.append((a, b))
        beat = {
            "role": role,
            "start_word_id": words[a]["id"],
            "end_word_id": words[b]["id"],
        }
        if isinstance(spec, dict):
            for key in ("punch_in", "protect_pause_before_ms", "protect_pause_after_ms", "deadpan"):
                if key in spec:
                    beat[key] = spec[key]
        out.append(beat)
    return out


def normalize_spine(payload: dict, words: list) -> dict:
    """A spine plan. `style_pack` is dropped. Word text is taken from `words`."""
    if not isinstance(payload, dict):
        raise ValueError("spine plan must be an object")
    if not words:
        raise ValueError("no words")
    words = assign_word_ids(words) if "id" not in (words[0] or {}) else list(words)
    payload = _unwrap_spine(payload)
    audience = _norm(payload.get("audience"))
    retelling = _norm(payload.get("retelling"))
    if not audience or not retelling:
        raise ValueError("a spine needs an audience and a retelling")
    if not isinstance(payload.get("lead_with_turn"), bool):
        raise ValueError("lead_with_turn must be true or false")
    dynamic = payload.get("spine_dynamic")
    if dynamic not in SPINE_DYNAMICS:
        raise ValueError("spine_dynamic must be claim_proof, story_arc, or escalation")
    raw_beats = _beat_spec_list(payload.get("beats"), words)
    if not raw_beats:
        raise ValueError("beats must list the kept spans")
    beats = [_beat(b, words, dynamic) for b in raw_beats]
    by = {b["role"]: b for b in beats}
    if set(by) != set(BEATS) or len(beats) != 3:
        raise ValueError("the spine is situation, turn, and consequence, each once")
    raw_detours = payload.get("detours") or []
    if raw_detours and dynamic != "story_arc":
        raise ValueError("a detour is only kept on a story arc")
    detours = [_detour(d, words, by) for d in raw_detours]
    graphics = []
    for g in payload.get("graphics") or []:
        graphics.append(_graphic(g, words, by, dynamic))
    out = {
        "version": 2,
        "audience": audience,
        "retelling": retelling,
        "lead_with_turn": payload["lead_with_turn"],
        "spine_dynamic": dynamic,
        "order": _order(payload["lead_with_turn"]),
        "beats": [by[role] for role in _order(payload["lead_with_turn"])],
        "detours": detours,
        "graphics": graphics,
    }
    if payload.get("job_id"):
        out["job_id"] = str(payload["job_id"])
    return out


def _dynamic_rules(dynamic: str) -> str:
    if dynamic == "story_arc":
        return (
            "This cut is a story_arc. Keep a tangent whose words come back in "
            "the consequence, as a detour outside the three beats. Do not trim "
            "that detour as off-topic."
        )
    if dynamic == "escalation":
        return (
            "This cut is an escalation. Keep the rapid-fire buildup. Set deadpan "
            "true and protect_pause_after_ms on a kept beat when a loud stretch "
            "drops into silence. A counter may tick a list, 1 then 2 then 3."
        )
    return (
        "This cut is a claim_proof. Keep the spans tight. Do not add a detour. "
        "A graphic has to anchor a fact, a metric, or a receipt."
    )


def spine_prompt(words: list, audience: str, retelling: str, lead_with_turn: bool,
                 spine_dynamic: str = "claim_proof") -> str:
    body = picker_prompt(words).split("Transcript:\n", 1)[1]
    dynamic = spine_dynamic if spine_dynamic in SPINE_DYNAMICS else "claim_proof"
    lead = "Lead with the turn." if lead_with_turn else "Keep situation, then turn, then consequence."
    return (
        "You are an editorial story cutter. Cut this transcript to one retelling.\n"
        f"Audience: {audience}\n"
        f"Retelling: {retelling}\n"
        f"Spine dynamic: {dynamic}\n"
        f"{lead} Do not pull an unrelated line forward because it sounds like a hook.\n"
        "The only shapes are claim_proof, story_arc, and escalation.\n"
        f"{_dynamic_rules(dynamic)}\n\n"
        "Keep only the word-id spans that deliver the situation, the turn, and the "
        "consequence. Set punch_in true only on the turn, the consequence, or a "
        "speaker change. Never set punch_in while two speakers overlap. Set "
        "protect_pause_before_ms and protect_pause_after_ms on the turn, from the "
        "real pause touching it.\n\n"
        "Graphics default to none. Add one only when a kept line fails by ear "
        "(number, price, name, receipt) or fails the mute test "
        "(mechanism, ui, before_after, story_texture), or when the line is doing "
        "one rhetorical job below. Copy the on-screen text from the words. "
        "Choose the template by that job, not by the verb that was said:\n"
        "- search_bar: a central question, curiosity, or a cultural reference. "
        "Not because someone said google.\n"
        "- message_card: an objection, an inner monologue, a dialogue callback, "
        "or a contradiction receipt. Not because someone said texted.\n"
        "- counter: stakes, scale, a before-and-after delta, or a rapid-fire list.\n"
        "- quote_card: the thesis, a mic-drop line, or a 2 to 4 word deadpan echo.\n"
        "- name_card: an ear-blind proper noun, price, tool, or venue. Set icon "
        "to a Lucide name. Stroke it with tokens.stroke and tokens.colors.stroke.\n"
        "- media_card, or template null: library or stock for visual proof or "
        "story texture. Literal action is allowed when the beat earns it, "
        "including typing, working, or setting up a space.\n\n"
        "presentation_mode is one of:\n"
        "- framed_card: a rounded card over the speaker. No unframed stock rectangle.\n"
        "- fullscreen_replace: a full-screen cutaway. At the start of a guest turn, "
        "wait 1.5s so the reaction lands, or use corner_note or split_screen instead.\n"
        "- corner_note: a compact upper corner for a cultural reference or a short "
        "deadpan echo, so a relaxed conversation is not covered.\n"
        "- split_screen: a 50/50 for a two-speaker reaction or a contradiction receipt.\n\n"
        "When the tape has a host and a guest, keep host turns on the speaker "
        "except a name_card or a search_bar. Put the heavier card on the guest. "
        "Hold a wide shot during overlapping speech: no punch, no overlay.\n"
        "use_person_matte is false. on_asset_miss is raw_speaker. "
        "Do not invent a gradient or a blur fill when an asset is missing.\n"
        "Do not return a style pack.\n\n"
        "Return JSON. beats is an array of word-id spans copied from the transcript, "
        "not a summary object:\n"
        '{"audience":"...","retelling":"...","lead_with_turn":false,'
        '"spine_dynamic":"' + dynamic + '",'
        '"beats":['
        '{"role":"situation","start_word_id":"w0000","end_word_id":"w0004"},'
        '{"role":"turn","start_word_id":"w0008","end_word_id":"w0012","punch_in":true},'
        '{"role":"consequence","start_word_id":"w0016","end_word_id":"w0020"}'
        '],"graphics":[]}\n'
        "Use ids that appear in the transcript.\n\n"
        "Transcript:\n" + body
    )


def apply_prosody(words: list, spine: dict, samples, sr: int) -> dict:
    """Snap kept edges and, on an escalation, record deadpan drops.

    Silence here only protects a pause inside a span that is already kept.
    It does not decide which sentences stay.
    """
    if words and "id" not in (words[0] or {}):
        words = assign_word_ids(words)
    dynamic = spine.get("spine_dynamic")
    beats = []
    for beat in spine.get("beats") or []:
        row = dict(beat)
        start, end = _word_times(words, row["start_word_id"], row["end_word_id"])
        before = int(row.get("protect_pause_before_ms") or 0) / 1000.0
        after = int(row.get("protect_pause_after_ms") or 0) / 1000.0
        protect_in = (start - before, start) if before else None
        protect_out = (end, end + after) if after else None
        row["snap_in"] = snap_boundary(samples, sr, start, "in", protect=protect_in)
        row["snap_out"] = snap_boundary(samples, sr, end, "out", protect=protect_out)
        drops = []
        if dynamic == "escalation":
            drops = find_deadpan_drops(samples, sr, start, end)
            if row.get("deadpan") and not drops and row.get("role") != "turn":
                row["protect_pause_before_ms"] = 0
                row["protect_pause_after_ms"] = 0
                row["deadpan"] = False
            elif drops and row.get("role") != "turn":
                tail = drops[-1]
                row["deadpan"] = True
                row["protect_pause_after_ms"] = max(
                    int(row.get("protect_pause_after_ms") or 0),
                    int(round((tail["end"] - tail["start"]) * 1000)),
                )
        row["deadpan_pauses"] = drops
        beats.append(row)
    out = dict(spine)
    out["beats"] = beats
    return out


# ---- checker -------------------------------------------------------------

_MANUAL = frozenset({"manual", "co_editor", "co-editor"})


def _is_manual(graphic: dict) -> bool:
    return str(graphic.get("origin") or "") in _MANUAL


def override_for_word(word: dict, overrides: dict):
    """A caption override keyed by word id or by the word's start time."""
    if not overrides or not isinstance(word, dict):
        return None
    wid = word.get("id")
    if wid is not None and wid in overrides:
        return overrides[wid]
    try:
        start = float(word.get("start"))
    except (TypeError, ValueError):
        return None
    for key in (f"{start:.3f}", f"{start:.2f}", str(start), str(round(start, 3))):
        if key in overrides:
            return overrides[key]
    return None


def _redraw(graphic: dict, words: list, overrides: dict) -> bool:
    by_id = {w.get("id"): w for w in words}
    for wid in graphic.get("source_word_ids") or []:
        word = by_id.get(wid)
        if not word:
            continue
        replacement = override_for_word(word, overrides)
        if replacement is None:
            continue
        if _norm(replacement) != _norm(word.get("word")):
            return True
    return False


def check_graphics(graphics: list, words: list, *,
                   face_boxes: dict | None = None,
                   overrides: dict | None = None,
                   missing_asset_ids: list | None = None,
                   caption_band: dict | None = None) -> dict:
    """Grade overlays without having planned them.

    A retelling graphic is dropped for a failed gate, a bad template or mode,
    a host-setup card, cross-talk, a matte, or a missing asset. A guest-turn
    fullscreen cutaway is delayed, not dropped. A hand-placed overlay is only
    moved off a face or dropped when its asset is missing.

    `overrides` may be keyed by word id or by the word's start timestamp.
    Either one dirties a graphic that copied that word.
    """
    if words and "id" not in (words[0] or {}):
        words = assign_word_ids(words)
    else:
        words = list(words or [])
    overrides = overrides or {}
    missing = set(missing_asset_ids or [])
    fixes = []
    for g in graphics or []:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "")
        manual = _is_manual(g)
        if not manual:
            try:
                span = span_text(words, g.get("start_word_id"), g.get("end_word_id"))
            except (ValueError, TypeError):
                fixes.append({"id": gid, "action": "drop", "reason": "fact_not_in_span"})
                continue
            template = g.get("template")
            reason = gate_justification(span, g.get("justification"), template)
            if reason:
                fixes.append({"id": gid, "action": "drop", "reason": reason})
                continue
            mode = g.get("presentation_mode") or "framed_card"
            if _presentation(template, mode):
                fixes.append({"id": gid, "action": "drop", "reason": "template_forbidden"})
                continue
            if g.get("spine_dynamic") == "claim_proof" and not _anchors_claim(g.get("justification") or {}):
                fixes.append({
                    "id": gid, "action": "drop",
                    "reason": "claim_proof graphics anchor a fact, a metric, or a receipt",
                })
                continue
            if g.get("use_person_matte") is True:
                fixes.append({"id": gid, "action": "drop", "reason": "person_matte"})
                continue
            try:
                if _span_is_host(words, g.get("start_word_id"), g.get("end_word_id")):
                    if template not in ("name_card", "search_bar"):
                        fixes.append({"id": gid, "action": "drop", "reason": "host_setup"})
                        continue
                if _cross_talk(words, g.get("start_word_id"), g.get("end_word_id")):
                    fixes.append({"id": gid, "action": "drop", "reason": "cross_talk"})
                    continue
                if (mode == "fullscreen_replace"
                        and _guest_opening(words, g.get("start_word_id"))
                        and float(g.get("reaction_delay_s") or 0) < REACTION_DELAY_S):
                    fixes.append({
                        "id": gid, "action": "delay", "delay_s": REACTION_DELAY_S,
                        "reason": "reaction_hold",
                    })
            except (ValueError, TypeError):
                fixes.append({"id": gid, "action": "drop", "reason": "fact_not_in_span"})
                continue
        if g.get("asset_id") and g["asset_id"] in missing:
            fixes.append({"id": gid, "action": "drop", "reason": "asset_miss"})
            continue
        if g.get("asset_missing") is True:
            fixes.append({"id": gid, "action": "drop", "reason": "asset_miss"})
            continue
        faces = (face_boxes or {}).get(gid) or []
        if g.get("rect") and faces:
            placed = place_off_face(g["rect"], faces, caption_band)
            if placed["dropped"]:
                fixes.append({"id": gid, "action": "drop", "reason": "covers_face_no_band"})
                continue
            if placed["face_collision"]:
                fixes.append({
                    "id": gid, "action": "move",
                    "placement_zone": placed["placement_zone"],
                    "rect": placed["rect"],
                })
        if _redraw(g, words, overrides):
            fixes.append({"id": gid, "action": "redraw", "reason": "word_override"})
    return {"ok": not fixes, "fixes": fixes}
