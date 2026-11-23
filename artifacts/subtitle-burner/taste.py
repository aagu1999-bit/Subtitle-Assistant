"""Taste memory: what this editor likes, learned from what they keep and undo.

Two kinds of entries live here. Rules and bad examples the user writes
themselves ("never zoom on my co-host", "no stock photos of handshakes") are
permanent until deleted. Learned entries come from the suggestions table:
an accepted move nudges its treatment up, a dismissed one down, an edit
records how it was corrected. Learned entries decay and are pruned, so a
habit from three months ago stops steering today's edit.

Retrieval is deliberately small: a bag-of-words cosine plus recency and
weight, top-k into the prompt. A vector store would be overkill for a few
hundred lines of text per user.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

KINDS = ("rule", "bad_example", "prefer", "avoid", "example")
USER_KINDS = ("rule", "bad_example")
DAY = 86400.0

# Effect / op types in the app -> the planner's treatment keys.
TREATMENT_KEY = {
    "punch_zoom": "punch", "ken_burns": "clean_zoom", "split_screen": "split",
    "speed": "speed", "broll": "broll", "add_broll": "broll", "kinetic_text": "kinetic_text",
}

_STOP = set("a an the and or of to in on for with at by is are was be it this that my your "
            "i you we they he she them our me us not no do does did".split())


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP]


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b.get(k, 0) for k in a)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def treatment_key(kind_or_type: str, device_kind: str | None = None) -> str:
    if device_kind:
        return f"device:{device_kind}"
    return TREATMENT_KEY.get(kind_or_type, kind_or_type or "")


class TasteStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    # ---- storage ----
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "items": []}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def items(self) -> list[dict]:
        with self._lock:
            return list(self._load()["items"])

    # ---- writes ----
    def add(self, kind: str, text: str, tags=None, key: str | None = None,
            source: str = "user", weight: float = 1.0, now: float | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:300]
        if not text:
            raise ValueError("text is required")
        now = now or time.time()
        with self._lock:
            data = self._load()
            norm = " ".join(_tokens(text))
            for it in data["items"]:
                if it["kind"] == kind and " ".join(_tokens(it["text"])) == norm:
                    # Seen again: strengthen rather than duplicate.
                    it["weight"] = round(min(5.0, it.get("weight", 1.0) + 0.5 * weight), 3)
                    it["updated_at"] = now
                    it["count"] = it.get("count", 1) + 1
                    self._save(data)
                    return it
            item = {"id": uuid.uuid4().hex[:10], "kind": kind, "text": text,
                    "tags": [str(t)[:30] for t in (tags or [])][:8], "key": key or None,
                    "source": source, "weight": round(float(weight), 3), "count": 1,
                    "created_at": now, "updated_at": now, "last_used": None}
            data["items"].append(item)
            self._save(data)
            return item

    def remove(self, item_id: str) -> bool:
        with self._lock:
            data = self._load()
            before = len(data["items"])
            data["items"] = [i for i in data["items"] if i["id"] != item_id]
            if len(data["items"]) != before:
                self._save(data)
                return True
            return False

    def reset(self, scope: str = "learned") -> int:
        """Forget learned entries (default) or everything."""
        with self._lock:
            data = self._load()
            before = len(data["items"])
            if scope == "all":
                data["items"] = []
            else:
                data["items"] = [i for i in data["items"] if i["source"] == "user"]
            self._save(data)
            return before - len(data["items"])

    def prune(self, ttl_days: float = 120, max_items: int = 300, min_weight: float = 0.15,
              now: float | None = None) -> dict:
        """Context hygiene: decay and drop stale learned entries, cap the store.

        User-written rules are never dropped by pruning; only deleting them
        removes them.
        """
        now = now or time.time()
        with self._lock:
            data = self._load()
            kept, dropped = [], 0
            for it in data["items"]:
                if it["source"] == "user":
                    kept.append(it)
                    continue
                age_days = (now - max(it.get("updated_at") or 0, it.get("last_used") or 0)) / DAY
                if age_days > ttl_days:
                    dropped += 1
                    continue
                # Half-life of 45 days on learned weight.
                eff = it.get("weight", 1.0) * (0.5 ** (age_days / 45.0))
                if eff < min_weight:
                    dropped += 1
                    continue
                kept.append(it)
            learned = [i for i in kept if i["source"] != "user"]
            if len(kept) > max_items and learned:
                learned.sort(key=lambda i: (i.get("weight", 1.0), i.get("updated_at") or 0))
                cut = {i["id"] for i in learned[: len(kept) - max_items]}
                dropped += len(cut)
                kept = [i for i in kept if i["id"] not in cut]
            data["items"] = kept
            self._save(data)
            return {"dropped": dropped, "kept": len(kept)}

    # ---- reads ----
    def retrieve(self, query: str, k: int = 6, now: float | None = None,
                 touch: bool = True) -> list[dict]:
        """Top-k entries for *query*. User rules always come first."""
        now = now or time.time()
        with self._lock:
            data = self._load()
            q = Counter(_tokens(query))
            scored = []
            for it in data["items"]:
                sim = _cosine(q, Counter(_tokens(it["text"] + " " + " ".join(it.get("tags") or []))))
                age_days = (now - (it.get("updated_at") or now)) / DAY
                recency = 0.5 ** (age_days / 30.0)
                score = sim + 0.15 * recency + 0.08 * min(5.0, it.get("weight", 1.0))
                if it["source"] == "user":
                    score += 1.0
                scored.append((score, it))
            scored.sort(key=lambda p: p[0], reverse=True)
            top = [it for _, it in scored[:k]]
            if touch and top:
                ids = {i["id"] for i in top}
                for it in data["items"]:
                    if it["id"] in ids:
                        it["last_used"] = now
                self._save(data)
            return top

    def prompt_block(self, query: str, k: int = 6) -> str:
        top = self.retrieve(query, k)
        if not top:
            return ""
        label = {"rule": "Rule", "bad_example": "Never like this", "prefer": "Tends to keep",
                 "avoid": "Tends to remove", "example": "Liked"}
        lines = [f"- {label.get(i['kind'], i['kind'])}: {i['text']}" for i in top]
        return ("\nTHIS EDITOR'S TASTE (from their rules and past corrections; follow it unless "
                "the request says otherwise):\n" + "\n".join(lines) + "\n")

    def plan_hints(self, threshold: float = 0.75) -> dict:
        """{"avoid": [...], "prefer": [...]} treatment keys for the planner."""
        score: dict[str, float] = {}
        for it in self.items():
            key = it.get("key")
            if not key:
                continue
            w = it.get("weight", 1.0)
            if it["kind"] in ("avoid", "bad_example"):
                score[key] = score.get(key, 0.0) - w
            elif it["kind"] in ("prefer", "example"):
                score[key] = score.get(key, 0.0) + w
        return {"avoid": sorted(k for k, v in score.items() if v <= -threshold),
                "prefer": sorted(k for k, v in score.items() if v >= threshold)}

    # ---- learning ----
    def learn_from_suggestion(self, kind: str, proposed, outcome: str, final=None) -> list[dict]:
        """Turn one resolved suggestion into taste entries."""
        if outcome not in ("accepted", "edited", "rejected") or not isinstance(proposed, dict):
            return []
        out = []
        if kind == "clip":
            title = str(proposed.get("title") or proposed.get("hook") or "")[:80]
            if not title:
                return []
            if outcome == "rejected":
                out.append(self.add("avoid", f"Clip idea like “{title}”", tags=["clip"],
                                    source="learned"))
            else:
                out.append(self.add("example", f"Clip idea like “{title}”", tags=["clip"],
                                    source="learned", weight=0.6))
            return out
        ftype = str(proposed.get("type") or "")
        key = treatment_key(ftype, (proposed.get("device") or {}).get("kind")
                            if isinstance(proposed.get("device"), dict) else None)
        pretty = ftype.replace("_", " ") or "effect"
        tags = [ftype] if ftype else []
        if outcome == "rejected":
            inten = proposed.get("intensity")
            text = f"Removes suggested {pretty}" + (f" at {inten} intensity" if inten else "")
            out.append(self.add("avoid", text, tags=tags, key=key, source="learned"))
        elif outcome == "accepted":
            out.append(self.add("prefer", f"Keeps suggested {pretty}", tags=tags, key=key,
                                source="learned", weight=0.5))
        elif outcome == "edited" and isinstance(final, dict):
            a, b = proposed.get("intensity"), final.get("intensity")
            if a and b and a != b:
                out.append(self.add("prefer", f"Prefers {pretty} at {b} intensity, not {a}",
                                    tags=tags + ["intensity"], source="learned"))
            if isinstance(final.get("anchor") or final.get("aim"), dict) and not (
                    isinstance(proposed.get("anchor") or proposed.get("aim"), dict)):
                out.append(self.add("prefer", f"Aims {pretty} at the speaker's face",
                                    tags=tags + ["aim"], source="learned"))
            def length(d):
                if d.get("dur") is not None:
                    return float(d["dur"])
                return float(d.get("end", 0)) - float(d.get("start", 0))
            try:
                pd, fd = length(proposed), length(final)
            except (TypeError, ValueError):
                pd = fd = 0
            if pd > 0 and fd > 0 and abs(fd - pd) / pd > 0.3:
                out.append(self.add("prefer", f"Prefers {'shorter' if fd < pd else 'longer'} "
                                    f"{pretty if pretty.endswith('s') else pretty + 's'}", tags=tags + ["duration"], source="learned"))
            if not out:
                out.append(self.add("prefer", f"Keeps suggested {pretty} with tweaks", tags=tags,
                                    key=key, source="learned", weight=0.3))
        return out
