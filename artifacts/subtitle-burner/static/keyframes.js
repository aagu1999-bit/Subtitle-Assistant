/* The preview half of keyframes.py. Read that file first -- it carries the
 * reasoning; this is the same spec evaluated per frame instead of compiled
 * into an ffmpeg expression.
 *
 * A track is [[localSeconds, value], ...]. Local seconds means measured from
 * the effect's own start, which is what lets one spec drive filters whose
 * clocks disagree.
 *
 * The easing names and the interpolation have to match keyframes.py exactly.
 * They are checked against it by a parity test over random tracks rather than
 * by hoping two hand-written cubics stay equal -- that hope is what produced
 * a Ken Burns that accumulates in the renderer and is linear here.
 */
(function (root) {
  "use strict";

  var EASINGS = ["linear", "out_cubic", "in_cubic", "in_out_cubic", "hold"];
  var DEFAULT_EASE = "linear";

  // Mirrors _ease() in keyframes.py.
  function ease(name, u) {
    if (name === "out_cubic") return 1 - Math.pow(1 - u, 3);
    if (name === "in_cubic") return Math.pow(u, 3);
    if (name === "in_out_cubic") {
      return u < 0.5 ? 4 * Math.pow(u, 3)
                     : 1 - Math.pow(-2 * u + 2, 3) / 2;
    }
    if (name === "hold") return 0;
    return u;
  }

  // Mirrors normalize_track(). Drops unusable keyframes rather than throwing:
  // a malformed track should cost its own effect, not the whole preview.
  function normalizeTrack(track) {
    var out = [];
    for (var i = 0; i < (track || []).length; i++) {
      var p = track[i];
      if (!p || p.length < 2) continue;
      var t = Number(p[0]), v = Number(p[1]);
      if (!isFinite(t) || !isFinite(v)) continue;
      out.push([Math.max(0, t), v]);
    }
    out.sort(function (a, b) { return a[0] - b[0]; });
    return out;
  }

  // Mirrors eval_track(). null for an empty track, so a caller can tell
  // "not animated" from "animated to zero".
  function evalTrack(track, t, easeName) {
    var pts = normalizeTrack(track);
    if (!pts.length) return null;
    easeName = easeName || DEFAULT_EASE;
    if (pts.length === 1 || t <= pts[0][0]) return pts[0][1];
    if (t >= pts[pts.length - 1][0]) return pts[pts.length - 1][1];
    for (var i = 0; i < pts.length - 1; i++) {
      var t0 = pts[i][0], v0 = pts[i][1];
      var t1 = pts[i + 1][0], v1 = pts[i + 1][1];
      if (t0 <= t && t < t1) {
        var span = t1 - t0;
        if (span <= 0) return v1;
        return v0 + (v1 - v0) * ease(easeName, (t - t0) / span);
      }
    }
    return pts[pts.length - 1][1];
  }

  root.Keyframes = {
    EASINGS: EASINGS,
    DEFAULT_EASE: DEFAULT_EASE,
    ease: ease,
    normalizeTrack: normalizeTrack,
    evalTrack: evalTrack,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.Keyframes;   // so the parity test can require it
  }
})(typeof window !== "undefined" ? window : globalThis);
