/* Turning an effect's saved settings into keyframe tracks.
 *
 * The browser half of overlay_motion_tracks() in app.py. Offsets are in
 * CANVAS FRACTIONS, not pixels: the preview positions boxes in percentages
 * and the renderer in pixels, so the shared number stays unitless and each
 * side multiplies by its own canvas. That way the one quantity they both
 * compute cannot disagree.
 *
 * This file is where named effects become tracks. Keeping that translation
 * in one small place is what stops "swipe_left" meaning two slightly
 * different things on the two sides of the app, which is what happened to
 * Ken Burns.
 */
(function (root) {
  "use strict";

  // Which way a swipe comes FROM. "swipe_left" is the gesture: the photo
  // enters from the right and travels left, the way a carousel moves under
  // your thumb. Mirrors _OVERLAY_SWIPE_FROM in app.py.
  var SWIPE_FROM = {
    swipe_left: [1, 0], swipe_right: [-1, 0],
    swipe_up: [0, 1], swipe_down: [0, -1],
  };

  // A track running 1 -> 0 shaped by out_cubic is exactly pow(1-u,3), the
  // curve the swipe always had.
  var MOTION_EASE = "out_cubic";

  function motionTracks(motion) {
    if (!motion) return null;
    var dir = SWIPE_FROM[motion.type];
    if (!dir) return null;
    var slide = Math.max(0.05, Number(motion.in) || 0.35);
    var tracks = {};
    if (dir[0]) tracks.x = [[0, dir[0]], [slide, 0]];
    if (dir[1]) tracks.y = [[0, dir[1]], [slide, 0]];
    return tracks;
  }

  /** Offset in canvas fractions at localT seconds into the clip. */
  function motionOffsetAt(motion, localT) {
    var tracks = motionTracks(motion);
    if (!tracks) return { dx: 0, dy: 0 };
    var K = root.Keyframes;
    return {
      dx: tracks.x ? (K.evalTrack(tracks.x, localT, MOTION_EASE) || 0) : 0,
      dy: tracks.y ? (K.evalTrack(tracks.y, localT, MOTION_EASE) || 0) : 0,
    };
  }

  // ---- Punch zoom -----------------------------------------------------
  // Mirrors _PUNCH_PEAK / punch_zoom_tracks() in app.py.
  var PUNCH_PEAK = {
    low: 1.08, med: 1.12, high: 1.18, strong: 1.18,
    "1.5x": 1.5, "2x": 2.0, hold_1_5: 1.5, hold_2: 2.0,
  };
  var PUNCH_HOLD_INTENSITIES = ["1.5x", "2x", "hold_1_5", "hold_2"];
  var PUNCH_IN_FRAMES = 5;
  var PUNCH_OUT_FRAMES = 8;
  var PUNCH_HOLD_SECONDS = 0.30;
  // The frame counts are expressed at this rate, which is a duration, not a
  // frame count. This file used to turn them into seconds with a hardcoded
  // 30 while the renderer used the project fps, so a 60fps project previewed
  // a push twice as long as it burned.
  var PUNCH_REF_FPS = 30;
  var PUNCH_EASE = "out_cubic";

  function punchZoomTracks(cfg, dur) {
    if (!cfg || !cfg.enabled) return null;
    var intensity = cfg.intensity || "med";
    var peak = PUNCH_PEAK[intensity] || 1.25;
    if (peak - 1 <= 0) return null;
    var hit = Math.max(0, Number(cfg.hit) || 0);

    if (PUNCH_HOLD_INTENSITIES.indexOf(intensity) !== -1) {
      if (hit <= 0) return { track: [[0, peak]], ease: "hold" };
      return { track: [[0, 1], [hit, peak]], ease: "hold" };
    }

    var tIn = Math.max(1, Number(cfg.in_frames) || PUNCH_IN_FRAMES) / PUNCH_REF_FPS;
    var tOut = Math.max(1, Number(cfg.out_frames) || PUNCH_OUT_FRAMES) / PUNCH_REF_FPS;
    var total = (dur && dur > 0) ? Math.max(0.01, dur - hit)
                                 : tIn + PUNCH_HOLD_SECONDS + tOut;
    if (total < tIn + tOut) {
      var squeeze = total / (tIn + tOut);
      tIn *= squeeze; tOut *= squeeze;
    }
    var holdEnd = Math.max(tIn, total - tOut);
    return {
      track: [[hit, 1], [hit + tIn, peak], [hit + holdEnd, peak],
              [hit + total, 1]],
      ease: PUNCH_EASE,
    };
  }

  /** Scale at tRel seconds into the effect. 1 when there is nothing to do. */
  function punchScaleAt(cfg, tRel, dur) {
    var t = punchZoomTracks(cfg, dur);
    if (!t) return 1;
    var v = root.Keyframes.evalTrack(t.track, tRel, t.ease);
    return v === null ? 1 : v;
  }

  // ---- Ken Burns ------------------------------------------------------
  // Mirrors _KENBURNS_INTENSITY / kenburns_tracks() in app.py.
  var KENBURNS_AMOUNT = { low: 0.12, med: 0.22, high: 0.35 };
  var KENBURNS_EASE = "linear";

  function kenburnsTracks(kb, dur) {
    if (!kb || !kb.enabled) return null;
    var amount = KENBURNS_AMOUNT[kb.intensity] || KENBURNS_AMOUNT.med;
    var span = Math.max(0.05, Number(dur) || 0.05);
    var track = (kb.direction || "in") === "out"
      ? [[0, 1 + amount], [span, 1]]
      : [[0, 1], [span, 1 + amount]];
    return { track: track, ease: KENBURNS_EASE };
  }

  function kenBurnsScaleAt(kb, tRel, dur) {
    var t = kenburnsTracks(kb, dur);
    if (!t) return 1;
    var v = root.Keyframes.evalTrack(t.track, tRel, t.ease);
    return v === null ? 1 : v;
  }

  root.KeyframeMotion = {
    KENBURNS_AMOUNT: KENBURNS_AMOUNT,
    kenburnsTracks: kenburnsTracks,
    kenBurnsScaleAt: kenBurnsScaleAt,
    SWIPE_FROM: SWIPE_FROM,
    MOTION_EASE: MOTION_EASE,
    motionTracks: motionTracks,
    motionOffsetAt: motionOffsetAt,
    PUNCH_PEAK: PUNCH_PEAK,
    PUNCH_HOLD_INTENSITIES: PUNCH_HOLD_INTENSITIES,
    punchZoomTracks: punchZoomTracks,
    punchScaleAt: punchScaleAt,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KeyframeMotion;
  }
})(typeof window !== "undefined" ? window : globalThis);
