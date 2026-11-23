"""Edit decision engine: decide WHAT a moment needs before HOW it looks.

Everything here is pure Python over transcript words (and optionally audio
samples) so it runs without Flask, ffmpeg or an API key, and is unit-tested
that way. app.py owns I/O: rendering devices, fetching B-roll, calling Gemini.

The output is two documents kept deliberately apart:

  structure -- spans of the transcript with a rhetorical role (hook, data,
               list, comparison, story, thesis, body) and the treatment the
               planner picked for each (device, b-roll, punch, clean zoom).
  style     -- how treatments look: caption font/colours, grade, zoom
               strength, b-roll and device density, device palette.

Swapping the style pack restyles the same structure; re-planning the
structure keeps the look. Neither knows about the other's fields.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

ROLES = ("hook", "data", "list", "comparison", "story", "thesis", "body")

# Treatments the planner can choose. "device:<kind>" names a graphics device.
TREATMENTS = ("device", "broll", "punch", "clean_zoom", "kinetic_text", "none")

_NUM_RE = re.compile(
    r"(\d|%|\$|£|€|\bpercent\b|\bmillion\b|\bbillion\b|\bthousand\b|\bhundred\b|"
    r"\bhalf\b|\bdouble[d]?\b|\btriple[d]?\b|\b\d+x\b|\btimes\b)", re.I)
_LIST_RE = re.compile(
    r"\b(first(ly)?|second(ly)?|third(ly)?|fourth|finally|step \w+|"
    r"(two|three|four|five|six|seven) (reasons|ways|things|steps|rules|tips|"
    r"mistakes|lessons|signs|types)|number (one|two|three))\b", re.I)
_COMPARE_RE = re.compile(
    r"\b(vs\.?|versus|compared to|compared with|better than|worse than|"
    r"instead of|rather than|on the other hand|the difference between|"
    r"while (the )?other|unlike|more than|less than)\b", re.I)
_STORY_RE = re.compile(
    r"\b(last (week|month|year|night|time)|yesterday|years ago|when i was|"
    r"i remember|one day|back (then|in)|i used to|i was|we were|"
    r"growing up|the first time i|i walked|i went|i got)\b", re.I)
_THESIS_RE = re.compile(
    r"\b(the (key|lesson|point|truth|secret|takeaway) is|here'?s the thing|"
    r"bottom line|that'?s why|the reason is|what (i|you) need to (know|understand)|"
    r"remember this|if you (take|remember) one thing|the real|this is why|"
    r"never forget|the most important)\b", re.I)

# Words that pull a span toward a specific graphics device rather than b-roll.
_DEVICE_CUES = [
    ("chat_bubbles", re.compile(r"\b(text(ed)?|messag(e|ed|es)|dm(ed)?|imessage|whatsapp|"
                                r"said to me|told me|replied|reply|chat)\b", re.I)),
    ("notification", re.compile(r"\b(notification|alert|ping(ed)?|email(ed)?|inbox|"
                                r"got a call|slack|phone (buzz|lit)|comment(s|ed)?)\b", re.I)),
    ("search_bar", re.compile(r"\b(google(d)?|search(ed)?|look(ed)? (it )?up|typed|"
                              r"browser|website|article|reddit|wikipedia)\b", re.I)),
    ("toggle_meter", re.compile(r"\b(switch(ed)?|turn(ed)? (it )?(on|off)|level(s)?|"
                                r"meter|progress|loading|battery|dial(ed)?|volume)\b", re.I)),
    ("node_map", re.compile(r"\b(leads to|which leads|because of|causes?|connect(s|ed)?|"
                            r"network|system|workflow|process|chain|ecosystem)\b", re.I)),
    ("timeline_scrubber", re.compile(r"\b(timeline|journey|milestone|from \w+ to \w+|"
                                     r"(19|20)\d\d|over the years|month (one|1)|day one|"
                                     r"phase|roadmap)\b", re.I)),
]

# Cues specific enough that the device beats a B-roll cutaway.
_STRONG_CUES = {"chat_bubbles", "notification", "search_bar"}

_ROLE_DEVICE = {
    "data": "counter",
    "list": "step_tracker",
    "comparison": "venn",
}

_STOP = {
    "the", "and", "a", "an", "to", "of", "in", "i", "is", "that", "it", "on", "you",
    "this", "for", "but", "with", "are", "have", "be", "at", "or", "as", "was",
    "so", "if", "out", "not", "we", "my", "they", "your", "all", "do", "can",
    "will", "about", "which", "up", "one", "there", "what", "would", "when",
    "she", "he", "their", "her", "his", "has", "who", "from", "by", "me", "how",
    "like", "just", "know", "then", "them", "now", "well", "think", "um", "uh",
    "okay", "ok", "right", "really", "very", "get", "got", "go", "going", "gonna",
    "thing", "things", "kind", "sort", "actually", "basically", "literally",
    "been", "had", "did", "does", "were", "our", "us", "its", "it's", "i'm",
    "don't", "that's", "because", "into", "than", "too", "more", "some", "any",
    "every", "also", "only", "even", "much", "many", "lot", "way", "make",
    "made", "see", "say", "said", "want", "need", "first", "second", "third",
}

# Literal phrases a naive search would turn into a cliche, mapped to the
# feeling a picture should carry instead.
_CONCEPTS = [
    (r"drown(ing)?|swamped|buried|overwhelm", ["late-night overwhelm", "cluttered desk at night", "tired at laptop"]),
    (r"burn(ed|t)? ?out|exhaust|tired|drained", ["burnout", "dim office after hours", "empty coffee cups"]),
    (r"money|cash|revenue|profit|income|paid|rich|wealth", ["financial growth", "rising chart on screen", "coins macro"]),
    (r"grow(th|ing)?|scale|scaling|bigger", ["upward momentum", "sunrise timelapse", "plant sprouting macro"]),
    (r"fail(ed|ure)?|lost|broke|mistake", ["setback", "rain on window", "crumpled paper"]),
    (r"win|won|success|made it|nailed", ["achievement", "confetti in light", "mountain summit"]),
    (r"stress|anxi|panic|pressure|deadline", ["pressure", "ticking clock macro", "racing city traffic"]),
    (r"calm|peace|relax|breathe|quiet", ["calm", "still lake at dawn", "soft morning light"]),
    (r"idea|think|realiz|figure(d)? out|clicked", ["insight", "light through window", "notebook sketching"]),
    (r"team|together|partner|cofounder|hire", ["collaboration", "hands on whiteboard", "busy workspace"]),
    (r"customer|client|user|audience|people", ["community", "busy street bokeh", "storefront at dusk"]),
    (r"speed|fast|quick|rush|hurry", ["momentum", "light trails", "fast moving clouds"]),
    (r"time|years|month|week|long", ["passage of time", "clock gears macro", "seasons changing"]),
    (r"home|family|kids|house", ["warm home", "kitchen morning light", "family table"]),
    (r"travel|trip|flight|abroad|city", ["wanderlust", "plane window clouds", "city skyline dusk"]),
    (r"food|cook|eat|coffee|restaurant", ["craft", "coffee pour macro", "kitchen steam"]),
    (r"tech|code|software|ai|app|computer", ["technology", "code on screen", "server lights"]),
    (r"health|gym|workout|run|sleep", ["vitality", "running shoes on track", "morning stretch"]),
]

# Subjects generative video gets wrong: anatomy, hands, faces, legible text.
GEN_BANNED = re.compile(
    r"\b(person|people|man|men|woman|women|guy|girl|boy|kid|child|children|"
    r"face|faces|hand|hands|finger|fingers|crowd|portrait|selfie|body|"
    r"text|word|words|letter|letters|sign|logo|caption|headline|label|"
    r"he|she|someone|somebody|customer|team|family)\b", re.I)


def _clean_tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-zA-Z][a-zA-Z'\-]+|\d+", (text or "").lower())
    return [t for t in toks if t not in _STOP and len(t) > 2]


# ---- Spans --------------------------------------------------------------

def segment_spans(words: list, t_in: float = 0.0, t_out: float = 1e9,
                  max_words: int = 16, pause_split: float = 0.6) -> list[dict]:
    """Group words into sentence-ish spans.

    Breaks on terminal punctuation, on a pause of *pause_split* seconds, or
    after *max_words*. Each span keeps its words so later passes can reach
    word-level prosody.
    """
    spans: list[dict] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        text = " ".join(str(w.get("word", "")).strip() for w in cur).strip()
        spans.append({
            "start": float(cur[0]["start"]),
            "end": float(cur[-1]["end"]),
            "text": re.sub(r"\s+", " ", text),
            "words": list(cur),
        })
        cur.clear()

    prev_end = None
    for w in words or []:
        try:
            ws, we = float(w.get("start", 0)), float(w.get("end", 0))
        except (TypeError, ValueError):
            continue
        if we <= t_in or ws >= t_out:
            continue
        if cur and prev_end is not None and ws - prev_end >= pause_split:
            flush()
        cur.append({**w, "start": ws, "end": we})
        prev_end = we
        token = str(w.get("word", "")).strip()
        if token.endswith((".", "!", "?")) or len(cur) >= max_words:
            flush()
    flush()
    for i, s in enumerate(spans):
        s["index"] = i
    return spans


def classify_role(text: str, start: float, window_start: float,
                  hook_seconds: float = 5.0) -> tuple[str, str]:
    """(role, reason) for one span. Heuristic and deterministic."""
    t = text or ""
    if start - window_start < hook_seconds:
        return "hook", "opens the video"
    if _LIST_RE.search(t):
        return "list", "enumerates points"
    if _COMPARE_RE.search(t):
        return "comparison", "contrasts two things"
    if _NUM_RE.search(t) and re.search(r"\d|percent|million|billion|thousand|double|triple|half", t, re.I):
        return "data", "states a number"
    if _THESIS_RE.search(t):
        return "thesis", "delivers the takeaway"
    if _STORY_RE.search(t):
        return "story", "tells an anecdote"
    return "body", "supporting talk"


def classify_spans(words: list, t_in: float = 0.0, t_out: float = 1e9) -> list[dict]:
    spans = segment_spans(words, t_in, t_out)
    for s in spans:
        role, why = classify_role(s["text"], s["start"], t_in)
        s["role"] = role
        s["reason"] = why
    # The last line of a clip is the payoff when nothing stronger claimed it.
    if len(spans) >= 3 and spans[-1]["role"] == "body":
        spans[-1]["role"] = "thesis"
        spans[-1]["reason"] = "closes the video"
    return spans


def refine_roles_with_llm(spans: list[dict], llm) -> list[dict]:
    """Optionally let a model relabel roles. *llm(prompt) -> dict*.

    The heuristic labels stay unless the model returns a valid role for an
    index, so a failed or partial answer costs nothing.
    """
    if not spans or llm is None:
        return spans
    lines = "\n".join(f"{s['index']}: {s['text'][:160]}" for s in spans[:80])
    prompt = (
        "Label each line of a short-form video transcript with its narrative role.\n"
        f"Roles: {', '.join(ROLES)}.\n"
        "hook = opening pattern interrupt; data = a number or statistic; list = "
        "enumerated points; comparison = two things contrasted; story = personal "
        "anecdote in past tense; thesis = the key lesson or punchline; body = the rest.\n"
        'Return JSON: {"roles": [{"index": 0, "role": "hook"}, ...]}\n\n' + lines
    )
    try:
        out = llm(prompt) or {}
    except Exception:
        return spans
    by_idx = {s["index"]: s for s in spans}
    for row in out.get("roles") or []:
        try:
            idx = int(row.get("index"))
        except (TypeError, ValueError, AttributeError):
            continue
        role = str(row.get("role") or "")
        if idx in by_idx and role in ROLES:
            if by_idx[idx]["role"] != role:
                by_idx[idx]["reason"] = "model relabelled"
            by_idx[idx]["role"] = role
    return spans


# ---- Prosody ------------------------------------------------------------

def prosody_from_samples(samples, sr: int, words: list) -> dict:
    """Per-word loudness, emphasis flags, speech rate and breathing pauses.

    *samples* is a mono float array in [-1, 1]. Emphasis is a word whose RMS
    sits well above the clip's typical level (z > 1.2) -- loudness rather than
    pitch, because it is cheap and it is what a punch-in should land on.
    """
    import numpy as np

    out = {"words": [], "emphasis": [], "pauses": [], "wpm": 0.0}
    if samples is None or not len(samples) or not words:
        return out
    x = np.asarray(samples, dtype="float32")
    levels = []
    for w in words:
        try:
            a = max(0, int(float(w["start"]) * sr))
            b = min(len(x), max(a + 1, int(float(w["end"]) * sr)))
        except (KeyError, TypeError, ValueError):
            levels.append(None)
            continue
        seg = x[a:b]
        rms = float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0
        levels.append(20 * math.log10(max(rms, 1e-6)))
    vals = [v for v in levels if v is not None]
    mu = float(np.mean(vals)) if vals else -40.0
    sd = float(np.std(vals)) if vals else 1.0
    sd = sd if sd > 1e-3 else 1.0
    for w, db in zip(words, levels):
        z = 0.0 if db is None else (db - mu) / sd
        row = {"start": float(w.get("start", 0)), "end": float(w.get("end", 0)),
               "word": w.get("word", ""), "db": None if db is None else round(db, 2),
               "z": round(z, 2)}
        out["words"].append(row)
        word = re.sub(r"[^a-z0-9]", "", str(w.get("word", "")).lower())
        if z > 1.2 and word and word not in _STOP:
            out["emphasis"].append(row)
    out["pauses"] = breathing_pauses(words)
    out["wpm"] = speech_rate(words)
    return out


def breathing_pauses(words: list, lo: float = 0.4, hi: float = 0.8) -> list[dict]:
    """Gaps between words long enough to cut on, short enough to be a breath."""
    res = []
    for a, b in zip(words or [], (words or [])[1:]):
        try:
            gap = float(b["start"]) - float(a["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo <= gap <= hi:
            res.append({"start": float(a["end"]), "end": float(b["start"]), "gap": round(gap, 3)})
    return res


def speech_rate(words: list) -> float:
    if not words or len(words) < 2:
        return 0.0
    try:
        dur = float(words[-1]["end"]) - float(words[0]["start"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    return round(len(words) / dur * 60.0, 1) if dur > 0.5 else 0.0


def pace_from_wpm(wpm: float) -> str:
    if wpm >= 185:
        return "fast"
    if wpm and wpm < 130:
        return "slow"
    return "normal"


def snap_to_pause(t: float, pauses: list[dict], window: float = 0.6) -> float:
    """Move an in/out point onto the middle of a nearby breathing pause."""
    best, dist = t, window
    for p in pauses or []:
        mid = (p["start"] + p["end"]) / 2
        if abs(mid - t) < dist:
            best, dist = mid, abs(mid - t)
    return best


# ---- Concept abstraction -------------------------------------------------

def concept_queries(text: str, limit: int = 3) -> dict:
    """Turn a spoken line into picture searches for the FEELING, not the noun.

    "I felt like I was drowning in spreadsheets" should not search for a
    person drowning. Returns {"concepts": [...], "queries": [...], "literal": [...]}.
    """
    t = (text or "").lower()
    concepts, queries = [], []
    for pat, qs in _CONCEPTS:
        if re.search(pat, t):
            concepts.append(qs[0])
            for q in qs[1:]:
                if q not in queries:
                    queries.append(q)
    literal = []
    for tok in _clean_tokens(text):
        if tok not in literal and not tok.isdigit():
            literal.append(tok)
    if not queries:
        # Nothing metaphorical matched: fall back to the most concrete nouns,
        # pushed toward atmosphere so stock search returns a mood, not a pose.
        for tok in literal[:2]:
            queries.append(f"{tok} atmospheric")
    return {"concepts": concepts[:limit], "queries": queries[:limit], "literal": literal[:6]}


def abstract_prompt(text: str, palette: list[str] | None = None) -> str:
    """A generative prompt restricted to textures, environments and macro detail."""
    cq = concept_queries(text)
    # Only abstracted concepts reach a generator. The speaker's literal nouns
    # ("my mom", "a sign saying SOLD") are exactly the people and text this
    # policy keeps out of generated frames.
    seed = cq["concepts"] or ["ambient light"]
    words = " ".join(seed)
    words = GEN_BANNED.sub("", words)
    words = re.sub(r"\s+", " ", words).strip() or "ambient light"
    tone = f", palette {' '.join(palette[:3])}" if palette else ""
    return (f"abstract atmospheric macro texture evoking {words}, soft depth of field, "
            f"slow drift, no people, no hands, no text{tone}")


def generative_prompt_ok(prompt: str) -> bool:
    """True when a prompt avoids anatomy, hands, faces and legible text."""
    stripped = re.sub(r"\bno (people|hands|text|faces?)\b", "", prompt or "", flags=re.I)
    return not GEN_BANNED.search(stripped)


# ---- Variety memory ------------------------------------------------------

@dataclass
class VarietyState:
    """What the edit just did, so it never repeats itself back to back."""
    window: float = 10.0
    history: list = field(default_factory=list)  # [(t, treatment_key)]

    def recent(self, t: float) -> list[str]:
        return [k for (ts, k) in self.history if t - ts <= self.window]

    def last(self) -> str | None:
        return self.history[-1][1] if self.history else None

    def allows(self, key: str, t: float) -> bool:
        if self.last() == key:
            return False
        family = key.split(":")[0]
        recent = self.recent(t)
        # Two of the same family inside the window reads as a template.
        return sum(1 for k in recent if k.split(":")[0] == family) < 2

    def push(self, key: str, t: float) -> None:
        self.history.append((t, key))


# ---- Planner -------------------------------------------------------------

DENSITY = {"low": 0.5, "med": 1.0, "high": 1.6}

DEVICE_FAMILY = {
    "chat_bubbles": "ui", "notification": "ui", "search_bar": "ui", "toggle_meter": "ui",
    "step_tracker": "mental", "venn": "mental", "node_map": "mental",
    "timeline_scrubber": "mental", "counter": "stat", "kinetic_text": "text",
}


def device_budget(pack: dict | None, seconds: float, intensity: str = "med") -> tuple[int, set]:
    """(max devices for this window, allowed kinds/families) from a style pack."""
    cfg = (pack or {}).get("graphics_budget") or {}
    allow = set(cfg.get("allow") or [])
    per_min = cfg.get("per_minute")
    if per_min is None:
        return 10 ** 6, allow
    density = DENSITY.get((intensity or "med").lower(), 1.0)
    n = float(per_min) * max(0.0, seconds) / 60.0 * density
    return (max(1, int(round(n))) if per_min > 0 else 0), allow


def device_allowed(kind: str, allow: set) -> bool:
    return not allow or kind in allow or DEVICE_FAMILY.get(kind) in allow


def _device_for(span: dict) -> str | None:
    text = span.get("text", "")
    for kind, rx in _DEVICE_CUES:
        if rx.search(text):
            return kind
    return _ROLE_DEVICE.get(span.get("role"))


def _cap(word: str) -> str:
    """Capitalise the first letter only; str.title() turns "here's" into "Here'S"."""
    word = str(word)
    return word[:1].upper() + word[1:]


def _labels_for(kind: str, span: dict) -> list[str]:
    """Best-effort labels for a device, pulled from the span itself."""
    text = span.get("text", "")
    toks = [t for t in _clean_tokens(text) if not t.isdigit()]
    if kind == "counter":
        m = re.search(r"(\$|£|€)?\s?(\d[\d,\.]*)\s?(%|percent|x|k|m|million|billion)?", text, re.I)
        if m:
            unit = (m.group(3) or "").lower()
            unit = {"percent": "%", "million": "M", "billion": "B"}.get(unit, unit)
            return [f"{m.group(1) or ''}{m.group(2)}{unit}", " ".join(toks[:4]).capitalize()]
        return ["2x", " ".join(toks[:4]).capitalize()]
    if kind == "step_tracker":
        return [_cap(w) for w in toks[:3]] or ["One", "Two", "Three"]
    if kind == "venn":
        m = re.search(r"(\w+)\s+(?:vs\.?|versus|compared (?:to|with)|instead of|rather than|than)\s+(\w+)", text, re.I)
        if m:
            return [_cap(m.group(1)), _cap(m.group(2)), "Overlap"]
        return [_cap(toks[0] if toks else "This"), _cap(toks[1] if len(toks) > 1 else "That"), "Both"]
    if kind == "chat_bubbles":
        return [text[:60], "wait what?"]
    if kind == "notification":
        return ["New message", text[:70]]
    if kind == "search_bar":
        return [" ".join(toks[:5]) or text[:40]]
    if kind == "toggle_meter":
        return [_cap(toks[0] if toks else "Focus"), "on"]
    if kind == "node_map":
        return [_cap(w) for w in toks[:4]] or ["Cause", "Effect"]
    if kind == "timeline_scrubber":
        years = re.findall(r"(?:19|20)\d\d", text)
        return years[:4] or [_cap(w) for w in toks[:3]] or ["Start", "Now"]
    if kind == "kinetic_text":
        return [text[:48]]
    return [text[:60]]


def suggest_device(text: str) -> dict:
    """Best device for a highlighted phrase, with labels and runners-up."""
    role, reason = classify_role(text, 999.0, 0.0)
    span = {"text": text, "role": role}
    best = _device_for(span)
    ranked = [best] if best else []
    for kind, rx in _DEVICE_CUES:
        if kind not in ranked and rx.search(text or ""):
            ranked.append(kind)
    for kind in ("kinetic_text", "counter", "step_tracker", "venn", "node_map"):
        if kind not in ranked:
            ranked.append(kind)
    kind = ranked[0]
    return {"kind": kind, "labels": _labels_for(kind, span), "role": role, "reason": reason,
            "alternatives": [{"kind": k, "labels": _labels_for(k, span)} for k in ranked[1:5]]}


def plan_edit(words: list, t_in: float, t_out: float, intensity: str = "med",
              style_pack: dict | None = None, prosody: dict | None = None,
              taste: dict | None = None, spans: list | None = None) -> dict:
    """Pick a treatment for every span. Returns {"structure", "style"}.

    *taste* may carry {"avoid": [treatment keys], "prefer": [...]} learned from
    the user's past corrections; avoided keys are skipped when an alternative
    exists.
    """
    density = DENSITY.get((intensity or "med").lower(), 1.0)
    spans = spans if spans is not None else classify_spans(words, t_in, t_out)
    prosody = prosody or {}
    emphasis = prosody.get("emphasis") or []
    pauses = prosody.get("pauses") or breathing_pauses(words)
    wpm = prosody.get("wpm") or speech_rate(
        [w for w in words or [] if t_in <= float(w.get("start", 0)) < t_out])
    pace = pace_from_wpm(wpm)
    avoid = set((taste or {}).get("avoid") or [])
    prefer = list((taste or {}).get("prefer") or [])
    state = VarietyState()
    total = max(0.5, t_out - t_in)
    # Treatment budget: about one visual beat every 7s at medium, scaled.
    budget = max(1, int(round(total / 7.0 * density)))
    if pace == "fast":
        budget += 1
    used = 0
    dev_budget, dev_allow = device_budget(style_pack, total, intensity)
    dev_used = 0
    decisions = []

    def candidates(span: dict) -> list[str]:
        role = span["role"]
        dev = _device_for(span)
        c: list[str] = []
        if role == "hook":
            c = ["punch", "kinetic_text"] + ([f"device:{dev}"] if dev else [])
        elif role in ("data", "list", "comparison"):
            c = [f"device:{dev or _ROLE_DEVICE[role]}", "punch"]
        elif role == "story":
            # "She texted me" is better shown as the text than as a stock
            # photo of a phone; looser cues still lose to B-roll.
            if dev in _STRONG_CUES:
                c = [f"device:{dev}", "broll", "punch"]
            else:
                c = ["broll"] + ([f"device:{dev}"] if dev else []) + ["punch"]
        elif role == "thesis":
            c = ["clean_zoom"]
        else:
            c = ([f"device:{dev}"] if dev else []) + ["broll", "punch"]
        for p in prefer:
            if p in c:
                c.remove(p)
                c.insert(0, p)
        return c

    for span in spans:
        dur = span["end"] - span["start"]
        cand = candidates(span)
        chosen = "none"
        must = span["role"] in ("hook", "thesis")
        if dur >= 0.6 and (used < budget or must):
            for key in cand:
                if key in avoid and len(cand) > 1:
                    continue
                dev_kind = key.split(":", 1)[1] if key.startswith("device:") else (
                    "kinetic_text" if key == "kinetic_text" else None)
                if dev_kind and (dev_used >= dev_budget or not device_allowed(dev_kind, dev_allow)):
                    continue
                if state.allows(key, span["start"]):
                    chosen = key
                    break
        hit = None
        if chosen in ("punch", "kinetic_text") or chosen.startswith("device:"):
            for e in emphasis:
                if span["start"] <= e["start"] <= span["end"]:
                    hit = e["start"]
                    break
        start = span["start"]
        end = span["end"]
        if chosen == "broll" or chosen.startswith("device:"):
            start = snap_to_pause(start, pauses)
            end = max(start + 1.2, snap_to_pause(end, pauses))
        row = {
            "index": span["index"], "role": span["role"], "reason": span["reason"],
            "text": span["text"], "start": round(start, 3), "end": round(end, 3),
            "treatment": chosen, "hit": round(hit, 3) if hit is not None else None,
        }
        if chosen.startswith("device:"):
            kind = chosen.split(":", 1)[1]
            row["device"] = {"kind": kind, "labels": _labels_for(kind, span)}
        elif chosen == "kinetic_text":
            row["device"] = {"kind": "kinetic_text", "labels": _labels_for("kinetic_text", span)}
        elif chosen == "broll":
            row["broll"] = concept_queries(span["text"])
        if chosen != "none":
            if "device" in row:
                dev_used += 1
            state.push(chosen, span["start"])
            if not must:
                used += 1
        decisions.append(row)

    style = style_from_pack(style_pack or {}, intensity)
    return {
        "structure": {
            "version": 1,
            "in": t_in, "out": t_out,
            "pace": pace, "wpm": wpm,
            "spans": decisions,
            "emphasis": [{"start": e["start"], "word": e["word"]} for e in emphasis[:40]],
            "pauses": pauses[:80],
        },
        "style": style,
    }


def style_from_pack(pack: dict, intensity: str = "med") -> dict:
    """The look half: nothing here says where anything goes."""
    st = dict(pack.get("style") or {})
    density = DENSITY.get((intensity or "med").lower(), 1.0)
    accent = st.get("highlight") or st.get("highlight_color") or "#FFD60A"
    second = st.get("accent") or st.get("accent_color") or "#00E5FF"
    return {
        "version": 1,
        "pack": pack.get("label") or "",
        "caption": st,
        "grade": pack.get("color_grade") or {"preset": "none"},
        "transition": pack.get("transition") or "crossfade",
        "zoom": {"punch_intensity": "high" if intensity == "high" else "med",
                 "clean_zoom": "low"},
        "density": {"devices": round(density, 2), "broll": round(density, 2)},
        "devices": {
            "palette": [accent, second, "#FFFFFF", "#0F1220"],
            "font": st.get("font") or st.get("font_name") or "DM Sans",
            "corner": 28,
            "per_minute": ((pack.get("graphics_budget") or {}).get("per_minute")),
            "allow": list((pack.get("graphics_budget") or {}).get("allow") or []),
        },
    }


# ---- Structure / style split on a timeline -------------------------------

_STYLE_KEYS = ("style", "speaker_colors", "headline_banner", "caption_preset", "logo",
               "style_doc")


def split_timeline(timeline: dict) -> tuple[dict, dict]:
    """(structure_doc, style_doc) for a timeline. merge_timeline undoes it."""
    tl = json.loads(json.dumps(timeline or {}))
    style = {k: tl.pop(k) for k in list(tl.keys()) if k in _STYLE_KEYS}
    return tl, style


def merge_timeline(structure: dict, style: dict) -> dict:
    out = json.loads(json.dumps(structure or {}))
    for k, v in (style or {}).items():
        if k in _STYLE_KEYS:
            out[k] = v
    return out


# ---- OpenTimelineIO (JSON, no library dependency) --------------------------

def _rt(seconds: float, rate: float) -> dict:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": float(rate),
            "value": round(float(seconds) * rate, 3)}


def _tr(start: float, dur: float, rate: float) -> dict:
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _rt(start, rate),
            "duration": _rt(dur, rate)}


def timeline_to_otio(timeline: dict, name: str = "Subtitle Burner edit",
                     source_paths: dict | None = None,
                     asset_paths: dict | None = None) -> dict:
    """Serialise a timeline doc as OTIO JSON (schema Timeline.1).

    Main clips become a video track of Clips with source ranges (cuts split a
    clip into its keep ranges); overlays become a second video track with Gaps
    between them; titles become markers. Every clip also carries its original
    dict under metadata["subtitle_burner"], so importing back is lossless.
    """
    rate = float((timeline or {}).get("fps") or 30)
    source_paths = source_paths or {}
    asset_paths = asset_paths or {}
    tracks = (timeline or {}).get("tracks") or {}

    def ref_for(c: dict) -> dict:
        path = None
        if c.get("source_job_id"):
            path = source_paths.get(c["source_job_id"])
        elif c.get("asset_id"):
            path = asset_paths.get(c["asset_id"])
        if path:
            return {"OTIO_SCHEMA": "ExternalReference.1", "target_url": f"file://{path}",
                    "available_range": None, "metadata": {}, "name": ""}
        return {"OTIO_SCHEMA": "MissingReference.1", "metadata": {}, "name": "",
                "available_range": None}

    main_children = []
    for c in tracks.get("main") or []:
        t_in, t_out = float(c.get("in", 0)), float(c.get("out", 0))
        keeps = _keep_ranges(t_in, t_out, c.get("cuts") or [])
        for k, (a, b) in enumerate(keeps):
            main_children.append({
                "OTIO_SCHEMA": "Clip.1",
                "name": f"{c.get('id', 'clip')}#{k}",
                "source_range": _tr(a, b - a, rate),
                "media_reference": ref_for(c),
                "effects": [], "markers": [], "enabled": True,
                "metadata": {"subtitle_burner": c if k == 0 else {"part_of": c.get("id")}},
            })

    ov_children, cursor = [], 0.0
    for c in sorted(tracks.get("overlay") or [], key=lambda x: float(x.get("start", 0))):
        start = float(c.get("start", 0))
        dur = max(0.04, float(c.get("out", 0)) - float(c.get("in", 0)))
        if start > cursor + 1e-3:
            ov_children.append({"OTIO_SCHEMA": "Gap.1", "name": "",
                                "source_range": _tr(0, start - cursor, rate),
                                "effects": [], "markers": [], "metadata": {}, "enabled": True})
        ov_children.append({
            "OTIO_SCHEMA": "Clip.1", "name": c.get("keyword") or (c.get("device") or {}).get("kind") or "overlay",
            "source_range": _tr(float(c.get("in", 0)), dur, rate),
            "media_reference": ref_for(c), "effects": [], "markers": [], "enabled": True,
            "metadata": {"subtitle_burner": c},
        })
        cursor = max(cursor, start + dur)

    markers = []
    for t in tracks.get("text") or []:
        ts = float(t.get("start", 0) or 0)
        te = float(t.get("out", 1) or 1)
        # Titles store `out` either as an end time or as a duration.
        dur = te - ts if te > ts else te
        markers.append({
            "OTIO_SCHEMA": "Marker.2", "name": str(t.get("text", ""))[:80], "color": "YELLOW",
            "marked_range": _tr(ts, max(0.04, dur), rate),
            "metadata": {"subtitle_burner": t}, "comment": "",
        })

    def track(name, kind, children, md):
        return {"OTIO_SCHEMA": "Track.1", "name": name, "kind": kind, "children": children,
                "source_range": None, "effects": [], "markers": [], "enabled": True,
                "metadata": md}

    rest = {k: v for k, v in (timeline or {}).items() if k != "tracks"}
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": name,
        "global_start_time": None,
        "metadata": {"subtitle_burner": {
            "timeline": rest,
            "effects": tracks.get("effects") or [],
            "music": tracks.get("music") or [],
        }},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1", "name": "tracks", "source_range": None,
            "effects": [], "markers": markers, "enabled": True, "metadata": {},
            "children": [
                track("Main", "Video", main_children, {}),
                track("Overlay", "Video", ov_children, {}),
            ],
        },
    }


def otio_to_timeline(doc: dict) -> dict:
    """Rebuild a timeline from OTIO JSON written by timeline_to_otio.

    Clips that carry our metadata round-trip exactly. Foreign OTIO files
    (from Resolve, Premiere...) import their main-track clips as source
    ranges with the file path kept for the user to relink.
    """
    if not isinstance(doc, dict) or doc.get("OTIO_SCHEMA", "").split(".")[0] != "Timeline":
        raise ValueError("not an OTIO timeline")
    md = ((doc.get("metadata") or {}).get("subtitle_burner") or {})
    tl = dict(md.get("timeline") or {})
    tracks = {"main": [], "overlay": [], "effects": list(md.get("effects") or []),
              "text": [], "music": list(md.get("music") or [])}
    stack = doc.get("tracks") or {}
    for i, trk in enumerate(stack.get("children") or []):
        key = "main" if i == 0 else "overlay"
        for ch in trk.get("children") or []:
            if not str(ch.get("OTIO_SCHEMA", "")).startswith("Clip"):
                continue
            own = (ch.get("metadata") or {}).get("subtitle_burner")
            if isinstance(own, dict) and own.get("part_of"):
                continue
            if isinstance(own, dict) and own:
                tracks[key].append(own)
                continue
            sr = ch.get("source_range") or {}
            st = sr.get("start_time") or {}
            du = sr.get("duration") or {}
            rate = float(st.get("rate") or 30) or 30
            a = float(st.get("value") or 0) / rate
            d = float(du.get("value") or 0) / rate
            url = ((ch.get("media_reference") or {}).get("target_url") or "")
            tracks[key].append({"id": (ch.get("name") or "clip")[:16], "in": a, "out": a + d,
                                "relink": url, "start": 0 if key == "main" else None})
    for m in stack.get("markers") or []:
        own = (m.get("metadata") or {}).get("subtitle_burner")
        if isinstance(own, dict):
            tracks["text"].append(own)
    tl["tracks"] = tracks
    return tl


def _keep_ranges(t_in: float, t_out: float, cuts: list) -> list:
    ranges = [[t_in, t_out]]
    for c in sorted(cuts or [], key=lambda r: float(r[0])):
        try:
            cs, ce = float(c[0]), float(c[1])
        except (TypeError, ValueError, IndexError):
            continue
        nxt = []
        for a, b in ranges:
            if ce <= a or cs >= b:
                nxt.append([a, b])
                continue
            if cs > a:
                nxt.append([a, cs])
            if ce < b:
                nxt.append([ce, b])
        ranges = nxt
    return [(a, b) for a, b in ranges if b - a > 0.04]
