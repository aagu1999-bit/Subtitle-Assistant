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

  root.KeyframeMotion = {
    SWIPE_FROM: SWIPE_FROM,
    MOTION_EASE: MOTION_EASE,
    motionTracks: motionTracks,
    motionOffsetAt: motionOffsetAt,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KeyframeMotion;
  }
})(typeof window !== "undefined" ? window : globalThis);
