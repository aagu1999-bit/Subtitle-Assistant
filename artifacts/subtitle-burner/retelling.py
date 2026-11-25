"""Retelling-first edit decisions.

Pure functions over a word list. No Flask, no FFmpeg, no style packs.
The caller loads audio samples and face boxes and passes them in.

A graphic is allowed only when a kept line fails by ear (a number, a price,
a name, a receipt) or fails the mute test (a mechanism, a UI, a before and
after that has to be seen). Every template is available. Nothing is gated
by a pack allowlist.
"""
from __future__ import annotations

import math
import re

# The six pacing packs. They are not a choice anymore. Callers that still
# send one of these ids are resolved to the single default look elsewhere.
DELETED_STYLE_PACKS = ("pulse", "clarity", "magazine", "film", "velocity", "always")

MOTIONS = ("hold", "rise", "count", "snap")
TEMPLATES = ("counter", "name_card", "quote_card", "message_card", "media_card")
EAR_BLIND = ("number", "price", "name", "receipt")
VISUAL_PROOF = ("mechanism", "ui", "before_after")
BEATS = ("situation", "turn", "consequence")
PRESENTATIONS = ("framed_card", "fullscreen_replace")

# A bare verb is not a fact. These used to open a search bar or a chat thread.
_BARE = frozenset({
    "google", "googled", "search", "searched", "searching",
    "text", "texted", "texting", "message", "messaged", "messages",
    "chat", "chatted", "dm", "dms",
})

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
        "beats": cleaned,
    }


def normalize_picker(payload: dict) -> dict:
    """A picker response. `style_pack` is not part of the schema and is dropped."""
    if not isinstance(payload, dict):
        raise ValueError("picker response must be an object")
    raw = payload.get("candidates")
    if not isinstance(raw, list) or not 2 <= len(raw) <= 3:
        raise ValueError("propose two or three retellings")
    return {"version": 1, "candidates": [_candidate(c, i) for i, c in enumerate(raw)]}


def picker_prompt(words: list) -> str:
    lines = []
    for w in assign_word_ids(words):
        sp = w.get("speaker") or ""
        who = f" {sp}" if sp else ""
        lines.append(f"{w['id']}{who} {float(w.get('start', 0)):.2f} {w.get('word', '')}")
    return (
        "You are an editorial story cutter. Find the retellings a specific "
        "stranger would repeat. Do not pace the video. Do not decorate lines.\n\n"
        "Propose two or three retellings this tape actually supports. Do not "
        "invent a comedic, contrarian, or tutorial angle the words do not contain.\n"
        "Each candidate:\n"
        "- audience: the specific stranger\n"
        "- retelling: one or two sentences, using what the speaker said\n"
        "- lead_with_turn: true only when the turn is understandable with no "
        "situation in front of it. Otherwise false.\n"
        "- beats.situation, beats.turn, beats.consequence: one line each\n\n"
        'Return JSON: {"version": 1, "candidates": [...]}\n'
        "Do not return a style pack, a format, a score, or a graphic.\n\n"
        "Transcript:\n" + "\n".join(lines)
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


def gate_justification(span: str, justification: dict, template: str) -> str | None:
    """None when the graphic may exist. Otherwise a drop reason.

    The template is part of the check: a number is a counter, a receipt is
    a quote card, and a mute-test fact is a media card. A search bar is not
    available here.
    """
    if not isinstance(justification, dict):
        return "no_justification"
    kind = str(justification.get("kind") or "")
    fact = str(justification.get("fact") or "")
    text = _norm(justification.get("text"))
    why = _norm(justification.get("why"))
    if not text or not why:
        return "no_justification"
    if not fact_in_span(span, text):
        return "fact_not_in_span"
    if _bare(text):
        return "template_forbidden"
    if kind == "ear_blind":
        if fact not in EAR_BLIND:
            return "no_justification"
        if not _matches_fact(fact, text):
            return "fact_not_in_span"
        if fact in ("number", "price") and template != "counter":
            return "template_forbidden"
        if fact == "name" and template != "name_card":
            return "template_forbidden"
        if fact == "receipt":
            if template == "message_card" and justification.get("reading_aloud") is True:
                return None
            if template == "quote_card":
                return None
            return "template_forbidden"
        return None
    if kind == "visual_proof":
        if fact not in VISUAL_PROOF:
            return "no_justification"
        if template != "media_card":
            return "template_forbidden"
        return None
    return "no_justification"


def _presentation(template: str, mode: str) -> str | None:
    if mode not in PRESENTATIONS:
        return "template_forbidden"
    if mode == "fullscreen_replace" and template != "media_card":
        return "template_forbidden"
    return None


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
    """Move a graphic off every face. Never covers one, never requests a matte.

    The open area is everything above the caption band. Search starts at the
    top of the frame, so a face in the middle does not keep the card. A left
    or right column is tried when a full-width card cannot sit clear. If
    nothing is clear, the graphic is dropped and the speaker stays.
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
    top = _band_top(caption_band)
    max_y = top - 0.03 - h
    widths = ((x, w), (0.04, min(w, 0.42)), (0.54, min(w, 0.42)))
    y = 0.04
    while max_y >= 0.04 and y <= max_y + 1e-6:
        for xtry, wtry in widths:
            cand = (xtry, y, wtry, h)
            if cand[1] + cand[3] > top - 0.01:
                continue
            if any(_overlaps(cand, f, pad) for f in faces):
                continue
            return pack("above_captions_band", cand, True)
        y += 0.04
    return {
        "face_collision": True,
        "placement_zone": None,
        "rect": None,
        "dropped": True,
        "reason": "covers_face_no_band",
        "use_person_matte": False,
    }


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
    """Move an in-point earlier or an out-point later, onto the speech edge.

    At most `cap_s` seconds. A protected pause may be entered and must not
    be crossed into the neighbouring sentence. Empty audio leaves `t` alone.
    """
    t = float(t)
    if samples is None or sr <= 0 or len(samples) == 0:
        return t
    track = _rms_track(samples, sr)
    if not track:
        return t
    hi = max(r for _, r in track)
    if hi < 1e-6:
        return t
    thresh = hi * 0.25
    look = max(cap_s, window_s)
    if side == "in":
        speech = [ts for ts, r in track
                  if t - look - 1e-6 <= ts <= t + 1e-6 and r >= thresh]
        snapped = min(speech) if speech else t
        snapped = min(t, max(t - cap_s, snapped))
    elif side == "out":
        speech = [ts for ts, r in track
                  if t - 1e-6 <= ts <= t + look + 1e-6 and r >= thresh]
        snapped = max(speech) if speech else t
        snapped = max(t, min(t + cap_s, snapped))
    else:
        raise ValueError("side must be 'in' or 'out'")
    return round(_clamp_pause(t, snapped, protect), 3)


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


def _beat(raw: dict, words: list) -> dict:
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
    before = int(raw.get("protect_pause_before_ms") or 0)
    after = int(raw.get("protect_pause_after_ms") or 0)
    if role != "turn":
        before = 0
        after = 0
    if before < 0 or after < 0:
        raise ValueError("a protected pause cannot be negative")
    return {
        "role": role,
        "start_word_id": raw["start_word_id"],
        "end_word_id": raw["end_word_id"],
        "text": text,
        "protect_pause_before_ms": before,
        "protect_pause_after_ms": after,
        "punch_in": punch,
    }


def _graphic(raw: dict, words: list, beats: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("a graphic must be an object")
    role = str(raw.get("beat_role") or "")
    if role not in beats:
        raise ValueError("a graphic must sit on a kept beat")
    template = raw.get("template")
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}")
    span = span_text(words, raw.get("start_word_id"), raw.get("end_word_id"))
    # The graphic's words have to lie inside the beat.
    beat = beats[role]
    if word_index(raw["start_word_id"]) < word_index(beat["start_word_id"]):
        raise ValueError("graphic starts before its beat")
    if word_index(raw["end_word_id"]) > word_index(beat["end_word_id"]):
        raise ValueError("graphic ends after its beat")
    why = gate_justification(span, raw.get("justification"), template)
    if why:
        raise ValueError(why)
    mode = raw.get("presentation_mode") or "framed_card"
    bad = _presentation(template, mode)
    if bad:
        raise ValueError(bad)
    if raw.get("use_person_matte") is True:
        raise ValueError("person_matte")
    miss = raw.get("on_asset_miss") or "raw_speaker"
    if miss != "raw_speaker":
        raise ValueError("on_asset_miss")
    just = raw["justification"]
    cleaned = {
        "kind": just["kind"],
        "fact": just["fact"],
        "text": _norm(just["text"]),
        "why": _norm(just["why"]),
    }
    if just.get("reading_aloud") is True:
        cleaned["reading_aloud"] = True
    ids = raw.get("source_word_ids")
    if not isinstance(ids, list) or not ids:
        ids = [raw["start_word_id"]]
    for wid in ids:
        word_index(wid)
    return {
        "id": _norm(raw.get("id")) or "g1",
        "beat_role": role,
        "start_word_id": raw["start_word_id"],
        "end_word_id": raw["end_word_id"],
        "justification": cleaned,
        "template": template,
        "presentation_mode": mode,
        "placement_zone": raw.get("placement_zone") or "above_captions_band",
        "face_collision": bool(raw.get("face_collision")),
        "use_person_matte": False,
        "on_asset_miss": "raw_speaker",
        "source_word_ids": [str(i) for i in ids],
    }


def normalize_spine(payload: dict, words: list) -> dict:
    """A spine plan. `style_pack` is dropped. Word text is taken from `words`."""
    if not isinstance(payload, dict):
        raise ValueError("spine plan must be an object")
    if not words:
        raise ValueError("no words")
    words = assign_word_ids(words) if "id" not in (words[0] or {}) else list(words)
    audience = _norm(payload.get("audience"))
    retelling = _norm(payload.get("retelling"))
    if not audience or not retelling:
        raise ValueError("a spine needs an audience and a retelling")
    if not isinstance(payload.get("lead_with_turn"), bool):
        raise ValueError("lead_with_turn must be true or false")
    raw_beats = payload.get("beats")
    if not isinstance(raw_beats, list) or not raw_beats:
        raise ValueError("beats must list the kept spans")
    beats = [_beat(b, words) for b in raw_beats]
    by = {b["role"]: b for b in beats}
    if set(by) != set(BEATS) or len(beats) != 3:
        raise ValueError("the spine is situation, turn, and consequence, each once")
    graphics = []
    for g in payload.get("graphics") or []:
        graphics.append(_graphic(g, words, by))
    out = {
        "version": 1,
        "audience": audience,
        "retelling": retelling,
        "lead_with_turn": payload["lead_with_turn"],
        "order": _order(payload["lead_with_turn"]),
        "beats": [by[role] for role in _order(payload["lead_with_turn"])],
        "graphics": graphics,
    }
    if payload.get("job_id"):
        out["job_id"] = str(payload["job_id"])
    return out


def spine_prompt(words: list, audience: str, retelling: str, lead_with_turn: bool) -> str:
    body = picker_prompt(words).split("Transcript:\n", 1)[1]
    lead = "Lead with the turn." if lead_with_turn else "Keep situation, then turn, then consequence."
    return (
        "You are an editorial story cutter. Cut this transcript to one retelling.\n"
        f"Audience: {audience}\n"
        f"Retelling: {retelling}\n"
        f"{lead} Do not pull an unrelated line forward because it sounds like a hook.\n\n"
        "Keep only the word-id spans that deliver the situation, the turn, and the "
        "consequence. Set punch_in true only on the turn, the consequence, or a "
        "speaker change. Set protect_pause_before_ms and protect_pause_after_ms "
        "only on the turn, from the real pause touching it.\n\n"
        "Graphics default to none. Add one only when a kept span has an ear-blind "
        "fact (number, price, name, receipt) or a visual proof (mechanism, ui, "
        "before_after) that fails the mute test. Copy the fact from the words. "
        "Templates: counter, name_card, quote_card, message_card, media_card. "
        "message_card only when the speaker is reading the message aloud "
        "(reading_aloud true). visual proof uses media_card. "
        "presentation_mode is framed_card or, for media_card only, fullscreen_replace. "
        "use_person_matte is false. on_asset_miss is raw_speaker. "
        "Do not open a search bar because someone said google, or chat bubbles "
        "because someone said texted.\n\n"
        "Transcript:\n" + body
    )


# ---- checker -------------------------------------------------------------

def check_graphics(graphics: list, words: list, *,
                   face_boxes: dict | None = None,
                   overrides: dict | None = None,
                   missing_asset_ids: list | None = None,
                   caption_band: dict | None = None) -> dict:
    """Grade a plan without having written it.

    `face_boxes` maps a graphic id to the face rects during that span.
    `overrides` maps a word id to the replacement caption. A hand edit of a
    word that was drawn on a graphic is a redraw, not a new plan.
    """
    if words and "id" not in (words[0] or {}):
        words = assign_word_ids(words)
    else:
        words = list(words or [])
    by_id = {w.get("id"): str(w.get("word") or "") for w in words}
    overrides = overrides or {}
    missing = set(missing_asset_ids or [])
    fixes = []
    for g in graphics or []:
        gid = str(g.get("id") or "")
        try:
            span = span_text(words, g.get("start_word_id"), g.get("end_word_id"))
        except (ValueError, TypeError):
            fixes.append({"id": gid, "action": "drop", "reason": "fact_not_in_span"})
            continue
        reason = gate_justification(span, g.get("justification"), g.get("template"))
        if reason:
            fixes.append({"id": gid, "action": "drop", "reason": reason})
            continue
        if g.get("use_person_matte") is True:
            fixes.append({"id": gid, "action": "drop", "reason": "person_matte"})
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
        redraw = False
        for wid in g.get("source_word_ids") or []:
            if wid in overrides and _norm(overrides[wid]) != _norm(by_id.get(wid)):
                redraw = True
                break
        if redraw:
            fixes.append({"id": gid, "action": "redraw", "reason": "word_override"})
    return {"ok": not fixes, "fixes": fixes}
