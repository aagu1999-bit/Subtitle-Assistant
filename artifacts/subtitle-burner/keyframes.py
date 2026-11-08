"""One way to say "this number changes over time".

The app grew three animation systems that each invented their own. They did
not agree about what time even is: the overlay swipe eased against `t`
(seconds), the punch zoom against `in` (the frame counter), and Ken Burns
against its own previous output, which is not a function of time at all and
cannot be seeked. Each was written twice, once in Python for ffmpeg and once
in JavaScript for the preview, and the pairs drifted -- Ken Burns accumulates
in the renderer and is linear in the browser, under the same name.

A track here is a list of [time, value] pairs in **local seconds**, measured
from whatever the effect's own start is. That single choice is what lets one
spec drive filters with different clocks: the emitter is handed an expression
for "local seconds in my clock" -- `(t-4.2)` for overlay, `(in/30)` for
zoompan -- and the curve above it never has to know the difference.

The shared thing is the data, not the code. Python turns a track into an
ffmpeg expression; keyframes.js evaluates the same track per frame. Two
mechanical readers of one spec, rather than two hand-written curves nobody
can diff.
"""

from __future__ import annotations

# Easing names are part of the saved format, so they are shared with
# keyframes.js verbatim. `hold` is a step -- it keeps the previous value
# until the next keyframe, which is how the "zoom hold" effects behave.
EASINGS = ("linear", "out_cubic", "in_cubic", "in_out_cubic", "hold")
DEFAULT_EASE = "linear"


def _ease(name: str, u: float) -> float:
    """Shape a 0..1 progress value. Mirrors ease() in keyframes.js."""
    if name == "out_cubic":
        return 1.0 - (1.0 - u) ** 3
    if name == "in_cubic":
        return u ** 3
    if name == "in_out_cubic":
        return 4.0 * u ** 3 if u < 0.5 else 1.0 - ((-2.0 * u + 2.0) ** 3) / 2.0
    if name == "hold":
        return 0.0
    return u


def _ease_expr(name: str, u: str) -> str:
    """The same shaping as an ffmpeg expression over an expression `u`."""
    if name == "out_cubic":
        return f"(1-pow(1-{u},3))"
    if name == "in_cubic":
        return f"pow({u},3)"
    if name == "in_out_cubic":
        return (f"if(lt({u},0.5),4*pow({u},3),"
                f"1-pow(-2*{u}+2,3)/2)")
    if name == "hold":
        return "0"
    return f"({u})"


def normalize_track(track) -> list[tuple[float, float]]:
    """Coerce a saved track into sorted [(t, value)] pairs.

    Saved timelines are free-form JSON written by a model and by hand, so
    this drops anything unusable rather than raising mid-render: a malformed
    keyframe should cost its own effect, never the export.
    """
    out = []
    for pair in track or []:
        try:
            t, v = pair[0], pair[1]
            out.append((max(0.0, float(t)), float(v)))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    out.sort(key=lambda p: p[0])
    return out


def eval_track(track, t: float, ease: str = DEFAULT_EASE) -> float | None:
    """The track's value at local time `t` seconds.

    Before the first keyframe it holds the first value, after the last it
    holds the last. Returns None for an empty track so a caller can tell
    "no animation" from "animated to zero".
    """
    pts = normalize_track(track)
    if not pts:
        return None
    if len(pts) == 1 or t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= t < t1:
            span = t1 - t0
            if span <= 0:
                return v1
            return v0 + (v1 - v0) * _ease(ease, (t - t0) / span)
    return pts[-1][1]


def to_ffmpeg_expr(track, time_expr: str = "t",
                   ease: str = DEFAULT_EASE) -> str | None:
    """Compile a track into an ffmpeg expression.

    `time_expr` is whatever evaluates to LOCAL SECONDS in the filter that
    will host this. Overlay x/y are expressions over absolute `t`, so an
    effect starting at 4.2s passes "(t-4.200)". zoompan only sees the frame
    counter, so it passes "(in/30)". The curve does not care which.

    Returns None for an empty track, and a bare number for a constant one --
    ffmpeg accepts a constant where an expression is wanted, and it keeps
    filter strings readable when nothing actually moves.
    """
    pts = normalize_track(track)
    if not pts:
        return None
    if len(pts) == 1:
        return _num(pts[0][1])

    # Build from the last segment backwards so each `if` nests in the else
    # of the one before it.
    expr = _num(pts[-1][1])
    for (t0, v0), (t1, v1) in reversed(list(zip(pts, pts[1:]))):
        span = t1 - t0
        if span <= 0:
            continue
        u = f"(({time_expr}-{_num(t0)})/{_num(span)})"
        shaped = _ease_expr(ease, u)
        seg = f"({_num(v0)}+{_num(v1 - v0)}*{shaped})"
        expr = f"if(lt({time_expr},{_num(t1)}),{seg},{expr})"
    # Before the first keyframe, hold the first value.
    return f"if(lt({time_expr},{_num(pts[0][0])}),{_num(pts[0][1])},{expr})"


def _num(v: float) -> str:
    """Format a number for an ffmpeg expression without exponent notation."""
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-") else "0"
