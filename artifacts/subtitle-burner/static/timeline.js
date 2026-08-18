/* ============================================================
   Timeline Editor — multi-track video editor (front-end)
   Talks to the /timeline/* + /upload-asset + /source-info routes.
   Self-contained: manages its own state, fetches its own data.
   ============================================================ */
(function () {
  "use strict";

  const TL_BUILD = "studio-editor-build-78-midform-arc-regen";
  console.log("[timeline] " + TL_BUILD + " script loaded");

  const $ = (id) => document.getElementById(id);
  let PPS = 14;            // pixels per second (mutable: timeline zoom)
  const MIN_TL_SECONDS = 30;
  const TRACK_LABEL_W = 78; // must match .tl-track-label width in timeline.css
  const LANE_OFFSET = 0;   // lanes start at x=0 within their container
  const MAX_UNDO = 50;
  const SNAP_PX = 10;      // magnetic snap threshold in screen pixels
  const TRACK_KEYS = ["main", "overlay", "effects", "text", "music"];
  const EFFECT_TYPES = [
    { id: "punch_zoom", label: "Punch zoom", icon: "⚡" },
    { id: "zoom_1_5", label: "1.5× Zoom hold", icon: "🔎" },
    { id: "zoom_2x", label: "2× Zoom hold", icon: "🔍" },
    { id: "ken_burns", label: "Ken Burns", icon: "🎞" },
    { id: "color", label: "Color grade", icon: "🎨" },
    { id: "split_screen", label: "Split-screen", icon: "⬓" },
  ];

  // ---- State ----
  let tl = null;           // { job_id, label, canvas, fit, fps, tracks }
  let selected = null;     // { track, id }
  let logoSelected = false; // project logo selected for on-stage resize
  let pendingBroll = [];   // suggested overlays awaiting Accept / Skip / Replace
  let _overlayReplaceTargetId = null; // sticky Overlay id during Replace media flow
  let sources = [];        // [{job_id, filename, ...}]
  let assets = [];         // [{asset_id, kind, duration, ext}]
  const selectedAssetIds = new Set(); // multi-select checkboxes in the Library panel
  const srcDur = {};       // job_id -> duration cache
  let saveTimer = null;
  let pollTimer = null;
  let initialized = false;
  let leftTab = "media";   // "media" | "transcript"
  let previewingOutput = false;  // true while the rendered result is in preview
  let transcriptWords = null;    // cached words for the open transcript clip
  let undoStack = [];
  let redoStack = [];
  let historySuspended = false;
  let seqPreview = null;         // { running, cancel } for Preview cut
  let magnetic = true;           // snap clip edges to nearby cuts / playhead
  let musicPlayers = [];         // music bed during Preview cut
  let clipAdvanceLock = false;   // guards against re-entrant clip-to-clip advance

  const uid = () => Math.random().toString(36).slice(2, 10);

  /** Caption look is canonical; accept Timeline/AI short aliases too. */
  function normalizeTlStyle(style) {
    if (typeof window.normalizeCaptionStyle === "function") {
      return window.normalizeCaptionStyle(style || {});
    }
    if (!style || typeof style !== "object") return {};
    const out = Object.assign({}, style);
    const map = [
      ["font", "font_name"], ["size", "font_size"],
      ["primary", "primary_color"], ["highlight", "highlight_color"],
      ["accent", "accent_color"], ["outline", "outline_color"],
      ["group", "group_size"],
    ];
    for (const [short, long] of map) {
      if ((out[long] == null || out[long] === "") && out[short] != null && out[short] !== "") out[long] = out[short];
      if ((out[short] == null || out[short] === "") && out[long] != null && out[long] !== "") out[short] = out[long];
    }
    return out;
  }

  function styleIsEmpty(st) {
    if (typeof window.styleHasCaptionFields === "function") {
      return !window.styleHasCaptionFields(st);
    }
    if (!st || typeof st !== "object") return true;
    return !(st.font_name || st.font || st.primary_color || st.primary || st.font_size || st.size);
  }

  /** If Timeline has no caption style yet, seed from Caption look / source job. */
  function seedStyleFromCaptionLook(preferredJobId) {
    if (!tl || !styleIsEmpty(tl.style)) {
      if (tl && tl.style) tl.style = normalizeTlStyle(tl.style);
      return false;
    }
    return pullCaptionLookOntoTimeline(preferredJobId, { quiet: true });
  }

  /** Caption look is canonical — transfer fonts/colors/speakers onto this project. */
  function pullCaptionLookOntoTimeline(preferredJobId, opts) {
    opts = opts || {};
    if (!tl) return false;
    let style = null;
    if (typeof window.captionLookStyle === "function") {
      style = window.captionLookStyle();
    } else if (typeof window.getStyle === "function") {
      style = window.getStyle();
    }
    if (styleIsEmpty(style)) {
      const jid = preferredJobId
        || (tl.tracks && tl.tracks.main[0] && tl.tracks.main[0].source_job_id)
        || null;
      const meta = jid && window.jobsById && window.jobsById[jid];
      if (meta && !styleIsEmpty(meta.style)) style = meta.style;
    }
    if (styleIsEmpty(style)) {
      if (!opts.quiet) setSaveState("Caption look empty — open Caption look first");
      return false;
    }
    tl.style = normalizeTlStyle(style);
    const sc = (style && style.speaker_colors) || {};
    if (sc && Object.keys(sc).length) {
      tl.speaker_colors = Object.assign({}, tl.speaker_colors || {}, sc);
    }
    const banner = style && style.headline_banner;
    if (banner) {
      tl.headline_banner = typeof banner === "string" ? { text: banner } : banner;
    }
    try {
      if (typeof window.getAudio === "function") tl.audio = window.getAudio();
    } catch (_) { /* optional */ }
    if (!opts.quiet) {
      setSaveState("Pulled Caption look → Timeline");
      scheduleSave();
      renderProps();
      updateStageCompositor();
    }
    return true;
  }

  // ---- Undo / redo ----
  function snapshotState() {
    return JSON.stringify({
      label: tl.label,
      canvas: tl.canvas,
      fit: tl.fit,
      fps: tl.fps,
      bg: tl.bg || "#000000",
      logo: tl.logo || null,
      style: tl.style || null,
      audio: tl.audio || null,
      ai_edit: tl.ai_edit || null,
      speaker_colors: tl.speaker_colors || null,
      headline_banner: tl.headline_banner || null,
      track_states: tl.track_states || null,
      tracks: tl.tracks,
      selected,
    });
  }

  function pushHistory() {
    if (!tl || historySuspended) return;
    undoStack.push(snapshotState());
    if (undoStack.length > MAX_UNDO) undoStack.shift();
    redoStack = [];
    updateHistoryButtons();
  }

  function clearHistory() {
    undoStack = [];
    redoStack = [];
    updateHistoryButtons();
  }

  function restoreSnapshot(snap) {
    if (!tl || !snap) return;
    const d = JSON.parse(snap);
    historySuspended = true;
    tl.label = d.label;
    tl.canvas = d.canvas || "9x16";
    tl.fit = d.fit || "cover";
    tl.fps = d.fps || 30;
    tl.bg = d.bg || "#000000";
    tl.logo = d.logo || null;
    if (d.style !== undefined) tl.style = d.style;
    if (d.audio !== undefined) tl.audio = d.audio;
    if (d.ai_edit !== undefined) tl.ai_edit = d.ai_edit;
    if (d.speaker_colors !== undefined) tl.speaker_colors = d.speaker_colors;
    if (d.headline_banner !== undefined) tl.headline_banner = d.headline_banner;
    if (d.track_states !== undefined) tl.track_states = d.track_states;
    tl.tracks = d.tracks || { main: [], overlay: [], effects: [], text: [], music: [] };
    if (!tl.tracks.effects) tl.tracks.effects = [];
    selected = d.selected || null;
    if ($("tlLabel")) $("tlLabel").value = tl.label;
    if ($("tlCanvas")) $("tlCanvas").value = tl.canvas;
    if ($("tlFit")) $("tlFit").value = tl.fit;
    applyStage();
    renderTimeline();
    if (selected && selected.track === "main") {
      const c = findClip(selected.track, selected.id);
      if (c) renderTranscript(c);
    }
    historySuspended = false;
    scheduleSave();
    updateHistoryButtons();
  }

  function undo() {
    if (!tl || !undoStack.length) return;
    redoStack.push(snapshotState());
    restoreSnapshot(undoStack.pop());
  }

  function redo() {
    if (!tl || !redoStack.length) return;
    undoStack.push(snapshotState());
    restoreSnapshot(redoStack.pop());
  }

  function updateHistoryButtons() {
    const u = $("tlUndoBtn");
    const r = $("tlRedoBtn");
    if (u) u.disabled = !undoStack.length;
    if (r) r.disabled = !redoStack.length;
  }

  // ---- Helpers ----
  function fmtTime(s) {
    s = Math.max(0, s || 0);
    const m = Math.floor(s / 60);
    const sec = (s % 60).toFixed(1);
    return `${m}:${sec.padStart(4, "0")}`;
  }

  function clipDuration(c) {
    if (!c) return 0.1;
    // Effect-lane clips + titles: `out` is duration (no source trim).
    if (c.type && EFFECT_TYPES.some((t) => t.id === c.type)) {
      return Math.max(0.2, Number(c.out) || 2);
    }
    // Main clips have no absolute `start` — honor text-edit keep-ranges so
    // lane widths / anchors match Preview cut + Render.
    if (c.start == null && c.source_job_id) {
      return mainClipVisibleDuration(c);
    }
    return Math.max(0.1, (c.out || 0) - (c.in || 0));
  }

  function mainClipVisibleDuration(c) {
    const ranges = keepRangesForClip(c, { allowEmpty: true });
    if (!ranges.length) return 0.05;
    const sum = ranges.reduce((acc, [a, b]) => acc + Math.max(0, b - a), 0);
    return Math.max(0.05, sum);
  }

  /** Map a source-time into the clip's output-local time (after cuts). */
  function sourceTimeToLocalOutput(clip, srcT) {
    const ranges = keepRangesForClip(clip, { allowEmpty: true });
    let played = 0;
    const t = Number(srcT) || 0;
    for (let i = 0; i < ranges.length; i++) {
      const [a, b] = ranges[i];
      if (t < a) return played;
      if (t <= b) return played + (t - a);
      played += Math.max(0, b - a);
    }
    return played;
  }

  /** Inverse of sourceTimeToLocalOutput — map output-local seconds back into source time. */
  function localOutputToSourceTime(clip, localOt) {
    const ranges = keepRangesForClip(clip, { allowEmpty: true });
    let played = 0;
    let remain = Math.max(0, Number(localOt) || 0);
    for (let i = 0; i < ranges.length; i++) {
      const [a, b] = ranges[i];
      const d = Math.max(0, b - a);
      if (remain <= d + 0.0001) return a + Math.min(d, remain);
      remain -= d;
      played += d;
    }
    return Number(clip.out) || Number(clip.in) || 0;
  }

  // Main clips are sequential; compute each clip's start by cumulative duration.
  function mainStart(idx) {
    let t = 0;
    for (let i = 0; i < idx; i++) t += clipDuration(tl.tracks.main[i]);
    return t;
  }

  function totalDuration() {
    let max = MIN_TL_SECONDS;
    let mainTotal = 0;
    tl.tracks.main.forEach((c) => (mainTotal += clipDuration(c)));
    max = Math.max(max, mainTotal);
    ["overlay", "effects", "text", "music"].forEach((k) => {
      tl.tracks[k].forEach((c) => {
        max = Math.max(max, (c.start || 0) + clipDuration(c));
      });
    });
    return max;
  }

  // ---- Anchoring (Phase 3) ----
  // Overlays / titles / music remember which Main clip they sit under (anchor)
  // and how far into it (anchor_offset), so reordering/retrimming Main carries
  // them along instead of leaving them stranded at an absolute second.
  function reanchor(c) {
    if (!tl.tracks.main.length) {
      if (typeof c.anchor === "string") c.anchor = null;
      return;
    }
    // Preserve face-coordinate anchors (objects from AI zoom) — those are not
    // Main clip ids and must not be overwritten by timeline reanchor.
    if (c.anchor && typeof c.anchor === "object") return;
    let idx = 0;
    for (let i = 0; i < tl.tracks.main.length; i++) {
      if ((c.start || 0) >= mainStart(i) - 0.001) idx = i;
    }
    c.anchor = tl.tracks.main[idx].id;
    c.anchor_offset = Math.max(0, (c.start || 0) - mainStart(idx));
  }

  function applyAnchors() {
    ["overlay", "effects", "text", "music"].forEach((k) => {
      (tl.tracks[k] || []).forEach((c) => {
        // Face-coord anchors are objects; only Main-clip id strings retime.
        if (typeof c.anchor !== "string" || !c.anchor) return;
        const idx = tl.tracks.main.findIndex((m) => m.id === c.anchor);
        if (idx >= 0) c.start = Math.max(0, mainStart(idx) + (c.anchor_offset || 0));
      });
    });
  }

  // ---- Networking ----
  async function api(url, opts) {
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-json */ }
    if (!res.ok) {
      const msg = (data && (data.error || data.message)) || res.statusText || ("HTTP " + res.status);
      if (res.status === 404) {
        throw new Error(msg + " (404 — route missing on this host; pull latest code and Stop+Run / Redeploy)");
      }
      if (res.status === 405) {
        throw new Error(
          msg + " (405 — Flask is still on an old process, or wrong method. " +
          "In Replit: Stop the Project workflow, Start it again, hard-refresh. " +
          "Polish must POST /polish/run.)"
        );
      }
      throw new Error(msg);
    }
    return data;
  }

  function isAlwaysPhotoMatchSession() {
    if (window._activeCapcutKey === "capcut_always") return true;
    const pending = window._pendingCapcutTemplate;
    if (pending && (pending.photo_match || pending.ai_edit_pack === "always")) return true;
    const ae = tl && tl.ai_edit;
    if (!ae) return false;
    const id = String(ae.style_pack_id || "").toLowerCase();
    const label = String(ae.style_pack || "").toLowerCase();
    return !!(ae.photo_match || id === "always" || label === "always" || label.includes("always"));
  }

  function syncAlwaysBrollDefaults(opts) {
    opts = opts || {};
    const ae = (tl && tl.ai_edit) || {};
    const pack = window._pendingCapcutTemplate || {};
    const wantPhoto = !!(opts.photo_match || ae.photo_match || pack.photo_match || isAlwaysPhotoMatchSession());
    if (!wantPhoto) return false;

    const modeEl = $("tlBrollMode");
    const placeEl = $("tlBrollPlacement");
    const scopeEl = $("tlBrollScope");
    const aiEl = $("tlBrollAiPhotos");
    const mode = opts.broll_mode || ae.broll_mode || pack.broll_mode || "photo";
    const place = opts.broll_placement || ae.broll_placement || pack.broll_placement || "center";
    const scope = opts.broll_scope || ae.broll_scope || pack.broll_scope || "full";
    if (modeEl) {
      const opt = modeEl.querySelector(`option[value="${mode}"]`);
      if (opt && !opt.disabled) modeEl.value = mode;
      else if (modeEl.querySelector('option[value="auto"]:not([disabled])')) modeEl.value = "auto";
    }
    if (placeEl) placeEl.value = place;
    if (scopeEl) scopeEl.value = scope;
    // Only set the AI checkbox when explicitly requested (e.g. Always pack apply).
    // Never re-force it on Suggest / status refresh — that blocked stock CSE/Pexels.
    if (aiEl && !aiEl.disabled && opts.use_ai_photos != null) {
      aiEl.checked = !!opts.use_ai_photos;
    }
    return true;
  }

  function alwaysKenBurnsDefault() {
    const pack = window._pendingCapcutTemplate || {};
    const kb = pack.ken_burns || {};
    return {
      enabled: true,
      direction: kb.direction || "in",
      intensity: kb.intensity || "med",
    };
  }

  /** Resolve Always / placement into a concrete overlay layout recipe. */
  function resolveBrollLayoutId(ref) {
    const placeEl = $("tlBrollPlacement");
    const place = (placeEl && placeEl.value) || (ref && ref.layout) || "center";
    if (place === "pip" || place === "pip_tr") return "pip_tr";
    if (place === "pip_tl" || place === "pip_br" || place === "pip_bl") return place;
    if (place === "lower") return "lower";
    if (place === "full" || place === "full_bleed") return "full";
    // Always photo-match + default Suggest placement "center"
    if (place === "center" || place === "center_match" || isAlwaysPhotoMatchSession()) return "center";
    return "center";
  }

  function applyBrollLayoutRecipe(ref) {
    if (!ref || ref.source === "badge") return ref;
    const layoutId = resolveBrollLayoutId(ref);
    const L = OVERLAY_LAYOUTS[layoutId];
    if (!L) return ref;
    ref.layout = layoutId;
    ref.x = L.x; ref.y = L.y; ref.w = L.w; ref.h = L.h; ref.fit = L.fit;
    return ref;
  }

  /** Strip queue fields + Always Ken Burns + layout recipe before place. */
  function prepareBrollAcceptRef(p) {
    const { id: _drop, _status, ...ref } = p;
    if (isAlwaysPhotoMatchSession() && ref.source !== "gif" && !(ref.ken_burns && ref.ken_burns.enabled)) {
      ref.ken_burns = alwaysKenBurnsDefault();
    }
    if (ref.source !== "badge" && ref.source !== "gif") {
      // Keep explicit pip/full if already set; otherwise apply recipe.
      if (!ref.layout || ref.layout === "pip_auto" || ref.layout === "center" || isAlwaysPhotoMatchSession()) {
        applyBrollLayoutRecipe(ref);
      }
    }
    return ref;
  }

  /** Move seeded Always overlays into the Suggest review queue (Accept / Skip). */
  function promoteOverlaysToPendingReview(opts) {
    opts = opts || {};
    if (!tl) return 0;
    const list = (tl.tracks.overlay || []).slice();
    if (!list.length) return 0;
    pendingBroll = list.map((ov) => {
      const ken = ov.ken_burns && ov.ken_burns.enabled
        ? Object.assign({}, ov.ken_burns)
        : (opts.kenBurns !== false ? alwaysKenBurnsDefault() : null);
      return Object.assign({}, ov, {
        id: uid(),
        _status: "pending",
        keyword: ov.keyword || "B-roll",
        source: ov.source || "photo",
        ken_burns: ken,
      });
    });
    tl.tracks.overlay = [];
    renderPendingBroll();
    return pendingBroll.length;
  }

  async function finishAlwaysSeedHandoff(seed, mediaHints) {
    const hints = mediaHints || (seed && seed.media_hints) || {};
    const always = !!(hints.photo_match || (seed && seed.ai_edit && seed.ai_edit.photo_match)
      || isAlwaysPhotoMatchSession());
    if (!always) return;

    // Brand kit: merge Caption Look logo/colors into the Always project.
    try {
      let lookStyle = null;
      if (typeof window.captionLookStyle === "function") lookStyle = window.captionLookStyle();
      else if (typeof window.getStyle === "function") lookStyle = window.getStyle();
      const brandOpts = {
        broll_mode: "photo",
        broll_placement: "center",
        broll_scope: "full",
      };
      if (window._brandLogoAssetId) {
        brandOpts.logo = {
          asset_id: window._brandLogoAssetId,
          x: 0.04, y: 0.04, w: 0.18, opacity: 0.9,
        };
      }
      if (lookStyle && typeof window.applyTimelineBranding === "function") {
        window.applyTimelineBranding(lookStyle, brandOpts);
      } else if (brandOpts.logo && tl) {
        tl.logo = Object.assign({}, tl.logo || {}, brandOpts.logo);
      }
    } catch (e) { /* best-effort */ }

    syncAlwaysBrollDefaults({
      photo_match: true,
      use_ai_photos: !!(seed && seed.ai_edit && seed.ai_edit.use_ai_photos),
      broll_mode: (seed && seed.ai_edit && seed.ai_edit.broll_mode) || "photo",
      broll_placement: (seed && seed.ai_edit && seed.ai_edit.broll_placement) || "center",
      broll_scope: (seed && seed.ai_edit && seed.ai_edit.broll_scope) || "full",
    });
    await loadAssets().catch(() => {});
    setLeftTab("media", { pin: true });

    const n = promoteOverlaysToPendingReview({ kenBurns: true });
    const photos = hints.photo_count || 0;
    const badges = hints.badge_count || 0;
    const gemini = hints.gemini_count || 0;

    if (n) {
      const bits = [];
      if (gemini) bits.push(gemini + " AI");
      if (photos - gemini > 0) bits.push((photos - gemini) + " photo");
      if (badges) bits.push(badges + " badge");
      setSaveState(
        `Always · ${n} still${n === 1 ? "" : "s"} ready to review`
        + (bits.length ? ` (${bits.join(", ")})` : "")
        + " — Accept / As Main / Skip"
      );
      return;
    }

    // No overlays seeded (no providers / no keywords) — run Suggest once with Always defaults.
    setSaveState("Always · no stills seeded — running Suggest B-roll…");
    try {
      await suggestKeywordOverlays();
    } catch (e) {
      setSaveState("Always · open Media and Suggest B-roll (check Generate AI photos)");
    }
  }

  window.syncAlwaysBrollDefaults = syncAlwaysBrollDefaults;
  window.isAlwaysPhotoMatchSession = isAlwaysPhotoMatchSession;
  window.applyAlwaysPackToTimeline = async function applyAlwaysPackToTimeline() {
    if (!(await ensureProject())) return false;
    if (typeof window.applyCapcutTemplateToUi === "function") {
      window.applyCapcutTemplateToUi("capcut_always");
    }
    window._activeCapcutKey = "capcut_always";
    const t = (typeof window.CAPCUT_TEMPLATES === "object" && window.CAPCUT_TEMPLATES.capcut_always)
      || window._pendingCapcutTemplate
      || {};
    const brandKit = t.brand_kit || { apply_logo: true, apply_colors: true, caption_preset: "hormozi" };

    // Compose Caption Look / brand kit into the Always session.
    let lookStyle = null;
    try {
      if (typeof window.flushCaptionLookToJob === "function") {
        lookStyle = await window.flushCaptionLookToJob();
      } else if (typeof window.captionLookStyle === "function") {
        lookStyle = window.captionLookStyle();
      } else if (typeof window.getStyle === "function") {
        lookStyle = window.getStyle();
      }
    } catch (e) { /* best-effort */ }

    const brandOpts = {
      canvas: t.canvas || "9x16",
      ken_burns: null, // stills get KB on Accept; don't auto on Main A-roll
      color_grade: t.color_grade || "warm",
      broll_mode: "photo",
      broll_placement: "center",
      broll_scope: "full",
    };
    if (brandKit.apply_logo && window._brandLogoAssetId) {
      brandOpts.logo = {
        asset_id: window._brandLogoAssetId,
        x: 0.04, y: 0.04, w: 0.18, opacity: 0.9,
      };
    }
    // Merge Always pack colors with Caption Look when brand kit asks for colors.
    let styleForTl = lookStyle || tl.style || {};
    if (brandKit.apply_colors) {
      styleForTl = Object.assign({}, styleForTl, {
        font_name: t.font || styleForTl.font_name || styleForTl.font,
        font: t.font || styleForTl.font || styleForTl.font_name,
        font_size: t.size != null ? t.size : styleForTl.font_size,
        primary_color: t.primary || styleForTl.primary_color,
        highlight_color: t.highlight || styleForTl.highlight_color,
        accent_color: t.accent || styleForTl.accent_color,
        group_size: t.group != null ? t.group : styleForTl.group_size,
      });
      if (t.headline) styleForTl.headline_banner = t.headline;
    }
    if (typeof window.applyTimelineBranding === "function") {
      window.applyTimelineBranding(styleForTl, brandOpts);
    }

    if (!tl.ai_edit) tl.ai_edit = {};
    tl.ai_edit = Object.assign({}, tl.ai_edit, {
      style_pack: "Always",
      style_pack_id: "always",
      photo_match: true,
      // Stock first by default — user opts into Gemini via the checkbox.
      use_ai_photos: false,
      broll_mode: "photo",
      broll_placement: "center",
      broll_scope: "full",
      ken_burns_on_accept: true,
      brand_kit: brandKit,
      caption_preset: brandKit.caption_preset || t.viral_preset || "hormozi",
    });
    syncAlwaysBrollDefaults({ photo_match: true, use_ai_photos: false });
    await refreshBrollStatus().catch(() => {});
    setLeftTab("media", { pin: true });
    setSaveState("Always pack + brand kit — Suggest B-roll to match stills");
    scheduleSave();
    return true;
  };

  /** Live-refresh B-roll mode dropdown from /broll/status (avoids stale HTML). */
  async function refreshBrollStatus(opts) {
    opts = opts || {};
    const statusEl = $("tlBrollStatus");
    const hintEl = $("tlBrollHint");
    const modeEl = $("tlBrollMode");
    const aiEl = $("tlBrollAiPhotos");
    const preferEl = $("tlBrollPrefer");
    try {
      let q = "";
      if (opts.probe === "cse" || opts.probe === "google_cse") q = "?probe=cse";
      else if (opts.probe === "serpapi" || opts.probe === "serp") q = "?probe=serpapi";
      else if (opts.probe === "all") q = "?probe=all";
      else if (opts.probe) q = "?probe=1";
      const data = await api("/broll/status" + q);
      const st = data.providers || {};
      const photoReady = !!data.photo_ready;
      const gifReady = !!(data.gif_ready || st.serpapi || st.google_cse);
      const geminiReady = !!(data.gemini_image_ready || st.gemini_image);
      window._brollGeminiReady = geminiReady;
      const bits = [];
      if (st.serpapi) bits.push("SerpAPI ✓");
      else bits.push("SerpAPI ✗");
      if (st.pexels) bits.push("Pexels ✓");
      else bits.push("Pexels ✗");
      if (st.unsplash) bits.push("Unsplash ✓");
      if (st.google_cse) bits.push("CSE ✓");
      else bits.push("CSE ✗");
      bits.push(geminiReady ? "Gemini ✓" : "Gemini ✗");
      if (statusEl) {
        statusEl.textContent = bits.join(" · ") + (data.build ? " · " + data.build : "");
        statusEl.style.color = (photoReady || geminiReady) ? "#7ddea0" : "#f0c674";
      }
      if (aiEl) {
        aiEl.disabled = !geminiReady;
        if (!geminiReady) aiEl.checked = false;
      }
      if (preferEl) {
        try {
          const saved = localStorage.getItem("tl_broll_prefer");
          if (saved && preferEl.querySelector('option[value="' + saved + '"]') && !preferEl.dataset.userSet) {
            preferEl.value = saved;
          }
        } catch (e) { /* ignore */ }
        // Disable unavailable prefer targets (still allow Auto).
        const serpOpt = preferEl.querySelector('option[value="serpapi"]');
        const cseOpt = preferEl.querySelector('option[value="google_cse"]');
        const pexOpt = preferEl.querySelector('option[value="pexels"]');
        const unsOpt = preferEl.querySelector('option[value="unsplash"]');
        if (serpOpt) serpOpt.disabled = !st.serpapi;
        if (cseOpt) cseOpt.disabled = !st.google_cse;
        if (pexOpt) pexOpt.disabled = !st.pexels;
        if (unsOpt) unsOpt.disabled = !st.unsplash;
        if (preferEl.value === "serpapi" && !st.serpapi) preferEl.value = "auto";
        if (preferEl.value === "google_cse" && !st.google_cse) preferEl.value = "auto";
        if (preferEl.value === "pexels" && !st.pexels) preferEl.value = "auto";
        if (preferEl.value === "unsplash" && !st.unsplash) preferEl.value = "auto";
      }
      if (modeEl) {
        const photoOpt = modeEl.querySelector('option[value="photo"]');
        const gifOpt = modeEl.querySelector('option[value="gif"]');
        const autoOpt = modeEl.querySelector('option[value="auto"]');
        if (photoOpt) photoOpt.disabled = !(photoReady || geminiReady);
        if (gifOpt) gifOpt.disabled = !gifReady;
        // If photos just became available and UI was stuck on badges, flip to Auto.
        if ((photoReady || geminiReady) && (modeEl.value === "badge" || !modeEl.value) && autoOpt) {
          modeEl.value = "auto";
        }
        if (!(photoReady || geminiReady) && modeEl.value === "photo" && autoOpt) {
          modeEl.value = "auto";
        }
        if (!gifReady && modeEl.value === "gif" && autoOpt) {
          modeEl.value = "auto";
        }
        // Legacy badge option removed from the DOM — clear if still selected.
        if (modeEl.value === "badge" && autoOpt) modeEl.value = "auto";
      }
      // Always session: re-apply photo/AI defaults after providers unlock.
      if (!opts.probe && isAlwaysPhotoMatchSession()) {
        syncAlwaysBrollDefaults({ photo_match: true });
      }
      if (hintEl) {
        if (isAlwaysPhotoMatchSession()) {
          hintEl.innerHTML = "<strong>Always · Photo Match</strong> — Suggest stills for keywords, then Accept. "
            + (geminiReady ? "AI photos preferred when checked. " : "Set <code>GEMINI_API_KEY</code> for AI stills. ")
            + "Ken Burns applies on Accept.";
        } else if (photoReady || geminiReady) {
          hintEl.innerHTML = "Photo providers ready. Suggest → <strong>Overlay</strong> or <strong>As Main</strong>. "
            + "Each Suggest returns up to ~4–5 clips (max 12)."
            + " <em>Prefer talker screenshots</em> uses Analyze speakers faces when available; stock/AI people get likeness bias."
            + (st.serpapi ? " SerpAPI covers photos + GIFs." : "")
            + (geminiReady ? " Check <em>Generate AI photos</em> to prefer Gemini stills." : "");
        } else {
          const aliases = (data.pexels_env && data.pexels_env.alias_names) || [];
          const aliasNote = aliases.length
            ? " Found env names: " + aliases.join(", ") + "."
            : "";
          hintEl.innerHTML = "No photo API key in <em>this</em> Studio process — keyword text badges are off. "
            + "On Replit: Tools → Secrets → <code>SERPAPI_API_KEY</code> "
            + "(or <code>PEXELS_API_KEY</code>), then <strong>Stop + Run</strong> "
            + "(Cursor secrets do not sync)." + aliasNote
            + " Optional: <code>GEMINI_API_KEY</code> + check Generate AI photos.";
        }
      }
      if (opts.probe === "serpapi" || opts.probe === "serp") {
        const p = data.serpapi_probe;
        if (statusEl && p) {
          statusEl.textContent = (p.ok ? "SerpAPI OK" : "SerpAPI fail") + ": " + (p.message || "")
            + (p.http_status != null ? " (HTTP " + p.http_status + ")" : "");
          statusEl.style.color = p.ok ? "#7ddea0" : "#e07070";
        }
        return p || null;
      }
      if (opts.probe === "cse" || opts.probe === "google_cse") {
        const p = data.cse_probe;
        if (statusEl && p) {
          const softOk = !!p.ok;
          statusEl.textContent = (softOk ? "CSE OK" : "CSE fail") + ": " + (p.message || "")
            + (p.http_status != null ? " (HTTP " + p.http_status + ")" : "");
          statusEl.style.color = softOk ? "#7ddea0" : "#e07070";
        }
        return p || null;
      }
      if (opts.probe && data.pexels_probe) {
        const p = data.pexels_probe;
        if (statusEl) {
          statusEl.textContent = (p.ok ? "Pexels OK" : "Pexels fail") + ": " + (p.message || "")
            + (p.http_status != null ? " (HTTP " + p.http_status + ")" : "");
          statusEl.style.color = p.ok ? "#7ddea0" : "#e07070";
        }
        return p;
      }
      return data;
    } catch (e) {
      if (statusEl) {
        statusEl.textContent = "Could not read /broll/status: " + e.message;
        statusEl.style.color = "#e07070";
      }
      return null;
    }
  }

  function currentBrollPrefer() {
    const el = $("tlBrollPrefer");
    const v = el ? el.value : "auto";
    return v || "auto";
  }

  async function testPexelsKey() {
    const btn = $("tlBrollTestBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Testing…"; }
    try {
      const p = await refreshBrollStatus({ probe: true });
      if (p && !p.ok) {
        alert((p.message || "Pexels check failed") + "\n\nReplit: set PEXELS_API_KEY in Tools → Secrets, then Stop + Run. Cursor secrets do not sync to Replit.");
      } else if (p && p.ok) {
        alert("Pexels accepted the key. Switch mode to Auto (or Photos) and click Suggest B-roll again.");
      }
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Test Pexels"; }
    }
  }

  async function testSerpapiKey() {
    const btn = $("tlBrollTestSerpBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Testing…"; }
    try {
      const p = await refreshBrollStatus({ probe: "serpapi" });
      if (!p) {
        alert("Could not reach /broll/status?probe=serpapi");
      } else if (!p.ok) {
        alert((p.message || "SerpAPI check failed")
          + "\n\nReplit Secrets → SERPAPI_API_KEY (exact name, no quotes) → Stop + Run."
          + "\nGet a key at https://serpapi.com/manage-api-key");
      } else if (p.hits === 0) {
        alert((p.message || "SerpAPI OK but 0 images.") + "\n\nTry Suggest B-roll anyway.");
      } else {
        alert("SerpAPI is good to go. Prefer source → SerpAPI first (or Auto), then Suggest B-roll.");
      }
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Test SerpAPI"; }
    }
  }

  async function testCseKey() {
    const btn = $("tlBrollTestCseBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Testing…"; }
    try {
      const p = await refreshBrollStatus({ probe: "cse" });
      if (!p) {
        alert("Could not reach /broll/status?probe=cse");
      } else if (!p.ok) {
        const msg = p.message || "CSE check failed";
        const locked = /does not have the access|permission_denied|closed to \*new\*/i.test(msg);
        alert(msg
          + (locked
            ? "\n\nYour keys are loaded — Google is rejecting the *project*. "
              + "Use PEXELS_API_KEY (photos) or Generate AI photos instead. GIF mode needs CSE."
            : "\n\nAlso check: API key Application restrictions = None (Replit is not a browser referrer), "
              + "secrets have no quotes, Stop+Run after editing Secrets."));
      } else if (p.hits === 0) {
        alert((p.message || "CSE responded but returned 0 images.")
          + "\n\nTurn on Image search, and for GIFs include giphy.com / tenor.com / imgur.com.");
      } else {
        alert("Google CSE is good to go (Image search works). "
          + "Set Prefer source → Google CSE first, then Suggest B-roll.");
      }
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Test CSE"; }
    }
  }

  function scheduleSave() {
    setSaveState("Editing…");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 800);
  }

  async function saveNow() {
    if (!tl) return;
    try {
      await api("/timeline/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: tl.job_id, timeline: serialize(), label: tl.label }),
      });
      setSaveState("Saved ✓");
    } catch (e) {
      setSaveState("Save failed");
    }
  }

  function setSaveState(s) {
    const el = $("tlSaveState");
    if (el) el.textContent = s;
  }

  // Build the timeline doc to persist/render. Main clips carry no `start`
  // (it's derived); everything else keeps its explicit start.
  function serialize() {
    return {
      canvas: tl.canvas,
      fit: tl.fit,
      fps: tl.fps,
      bg: tl.bg || "#000000",
      logo: tl.logo || null,
      style: tl.style || null,
      audio: tl.audio || null,
      sfx_overlays: tl.sfx_overlays !== false,
      ai_edit: tl.ai_edit || null,
      speaker_colors: tl.speaker_colors || { SPEAKER_00: "#FFD700", SPEAKER_01: "#00E5FF" },
      headline_banner: tl.headline_banner || null,
      track_states: tl.track_states || null,
      tracks: {
        main: tl.tracks.main.map((c) => ({
          id: c.id, source_job_id: c.source_job_id, asset_id: c.asset_id,
          in: c.in, out: c.out, transition: c.transition || null,
          cuts: c.cuts || [],
          word_overrides: c.word_overrides || null,
          ken_burns: c.ken_burns || null, punch_zoom: c.punch_zoom || null, split: c.split || null,
          color: c.color || null, color_grade: c.color_grade || null,
          reframe: c.reframe || null, shot_index: c.shot_index != null ? c.shot_index : null,
          burn_captions: c.burn_captions,
        })),
        overlay: tl.tracks.overlay.map((c) => ({ ...c })),
        effects: (tl.tracks.effects || []).map((c) => ({ ...c })),
        text: tl.tracks.text.map((c) => ({ ...c })),
        music: tl.tracks.music.map((c) => ({ ...c })),
      },
    };
  }

  // ---- Loaders ----
  async function loadProjects() {
    try {
      const data = await api("/timeline/list");
      const sel = $("tlProjectSelect");
      if (!sel) return;
      sel.innerHTML = "";
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = data.timelines.length ? "— Open project —" : "No projects yet";
      sel.appendChild(blank);
      data.timelines.forEach((p) => {
        const o = document.createElement("option");
        o.value = p.job_id;
        const name = (p.filename || "Untitled").replace(/\.mp4$/, "");
        o.textContent = `${name} (${p.clip_count} clip${p.clip_count === 1 ? "" : "s"})`;
        if (tl && p.job_id === tl.job_id) o.selected = true;
        sel.appendChild(o);
      });
    } catch (e) { /* ignore */ }
  }

  async function deleteCurrentProject() {
    if (!tl || !tl.job_id) {
      alert("No project open to delete.");
      return;
    }
    const nMain = (tl.tracks.main || []).length;
    const label = tl.label || "this project";
    const msg = nMain
      ? `Delete timeline "${label}" (${nMain} main clip${nMain === 1 ? "" : "s"})? Source videos stay in Media.`
      : `Delete empty timeline "${label}"?`;
    if (!confirm(msg)) return;
    const doomed = tl.job_id;
    try {
      await api("/timeline/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: doomed }),
      });
    } catch (e) {
      alert("Could not delete project: " + e.message);
      return;
    }
    tl = null;
    selected = null;
    const data = await api("/timeline/list").catch(() => ({ timelines: [] }));
    if (data.timelines && data.timelines.length) {
      await openProject(data.timelines[0].job_id);
    } else {
      const labelEl = $("tlLabel");
      if (labelEl) labelEl.value = "";
      renderTimeline();
      setSaveState("Project deleted");
    }
    await loadProjects();
  }

  async function loadSources() {
    try {
      const data = await api("/jobs");
      sources = (data.jobs || []).filter((j) => j.video_available);
      renderSourceList();
    } catch (e) { /* ignore */ }
  }

  async function loadAssets() {
    try {
      const data = await api("/list-assets");
      assets = data.assets || [];
      renderAssetList();
    } catch (e) { /* ignore */ }
  }

  async function getSourceDuration(jobId) {
    if (srcDur[jobId] != null) return srcDur[jobId];
    const info = await api("/source-info/" + jobId);
    srcDur[jobId] = info.duration || 10;
    return srcDur[jobId];
  }

  // ---- Library rendering ----
  let libraryView = "list"; // "list" | "icons"

  function applyLibraryViewClass() {
    ["tlSourceList", "tlAssetList"].forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.classList.toggle("tl-view-list", libraryView === "list");
      el.classList.toggle("tl-view-icons", libraryView === "icons");
    });
    document.querySelectorAll(".tl-view-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === libraryView);
    });
  }

  function wireLibraryViewToggle() {
    document.querySelectorAll(".tl-view-btn").forEach((b) => {
      b.onclick = () => {
        libraryView = b.dataset.view === "icons" ? "icons" : "list";
        applyLibraryViewClass();
        renderSourceList();
        renderAssetList();
      };
    });
  }

  function _bindLibraryDrag(el, payload) {
    el.draggable = true;
    el.addEventListener("dragstart", (e) => {
      el.classList.add("dragging");
      try {
        e.dataTransfer.setData("application/x-tl-lib", JSON.stringify(payload));
        e.dataTransfer.setData("text/plain", payload.kind + ":" + (payload.job_id || payload.asset_id || ""));
        e.dataTransfer.effectAllowed = "copy";
      } catch (_) { /* ignore */ }
    });
    el.addEventListener("dragend", () => el.classList.remove("dragging"));
  }

  function renderSourceList() {
    const wrap = $("tlSourceList");
    if (!wrap) return;
    wrap.innerHTML = "";
    applyLibraryViewClass();
    if (!sources.length) {
      wrap.innerHTML = '<p class="muted tl-hint">Upload &amp; transcribe a video first — it\'ll show up here.</p>';
      return;
    }
    sources.forEach((s) => {
      const div = document.createElement("div");
      div.className = "tl-source-item";
      const thumb = `<span class="tl-asset-thumb tl-asset-thumb-ph" aria-hidden="true">🎬</span>`;
      div.innerHTML =
        `${thumb}<span class="tl-source-name" title="${esc(s.filename || "")}">${esc(s.filename || s.job_id.slice(0, 8))}</span>`;
      const actions = document.createElement("div");
      actions.className = "tl-source-actions";
      const add = document.createElement("button");
      add.className = "tl-chip-btn";
      add.textContent = "➕";
      add.title = "Add to Main";
      add.onclick = (e) => { e.stopPropagation(); addMainClip(s.job_id); };
      const ov = document.createElement("button");
      ov.className = "tl-chip-btn";
      ov.textContent = "🖼";
      ov.title = "Add as overlay";
      ov.onclick = (e) => { e.stopPropagation(); addOverlayClip({ source_job_id: s.job_id }); };
      const fx = document.createElement("button");
      fx.className = "tl-chip-btn";
      fx.textContent = "✨";
      fx.title = "Add as Effects split second video";
      fx.onclick = (e) => {
        e.stopPropagation();
        addEffectClip("split_screen", { source_job_id: s.job_id, placement: "second_bottom" });
      };
      const ren = document.createElement("button");
      ren.className = "tl-chip-btn";
      ren.textContent = "✎";
      ren.title = "Rename this video";
      ren.onclick = (e) => {
        e.stopPropagation();
        renameSourceJob(s);
      };
      const del = document.createElement("button");
      del.className = "tl-chip-btn tl-chip-danger";
      del.textContent = "✕";
      del.title = "Remove from Media list";
      del.onclick = (e) => {
        e.stopPropagation();
        removeSourceFromMedia(s);
      };
      actions.appendChild(add);
      actions.appendChild(ov);
      actions.appendChild(fx);
      actions.appendChild(ren);
      actions.appendChild(del);
      div.appendChild(actions);
      _bindLibraryDrag(div, { kind: "source", job_id: s.job_id });
      wrap.appendChild(div);
    });
  }

  async function renameSourceJob(s) {
    if (!s || !s.job_id) return;
    const current = s.filename || "";
    const next = prompt("Rename this video:", current);
    if (next === null) return;
    const trimmed = next.trim();
    if (!trimmed || trimmed === current) return;
    try {
      const res = await fetch("/rename-job", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: s.job_id, filename: trimmed }),
      });
      const j = await res.json();
      if (j.error) throw new Error(j.error);
      s.filename = trimmed;
      renderSourceList();
      renderTimeline(); // refreshes clip labels / logo dropdown that show source filenames
      if (typeof window.renderJobsList === "function") window.renderJobsList();
      setSaveState("Renamed ✓");
    } catch (e) {
      alert("Rename failed: " + e.message);
    }
  }

  async function renameAsset(a) {
    if (!a || !a.asset_id) return;
    const current = a.filename || a.keyword || "";
    const next = prompt("Rename this asset:", current);
    if (next === null) return;
    const trimmed = next.trim();
    if (!trimmed || trimmed === current) return;
    try {
      const j = await api("/rename-asset/" + a.asset_id, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: trimmed }),
      });
      if (j.error) throw new Error(j.error);
      a.filename = trimmed;
      renderAssetList();
      renderTimeline(); // logo dropdown + overlay/music labels use asset filenames
      setSaveState("Renamed ✓");
    } catch (e) {
      alert("Rename failed: " + e.message);
    }
  }

  function removeSourceFromMedia(s) {
    if (!s || !s.job_id) return;
    const used = tl && ["main", "overlay", "effects"].some((k) =>
      (tl.tracks[k] || []).some((c) => c.source_job_id === s.job_id));
    const msg = used
      ? `Remove "${s.filename || s.job_id.slice(0, 8)}" from Media and delete timeline clips that use it?`
      : `Remove "${s.filename || s.job_id.slice(0, 8)}" from Media?`;
    if (!confirm(msg)) return;
    if (tl && used) {
      pushHistory();
      ["main", "overlay"].forEach((k) => {
        const kept = [];
        (tl.tracks[k] || []).forEach((c) => {
          if (c.source_job_id === s.job_id) {
            if (k === "main") {
              ["overlay", "effects", "text", "music"].forEach((tk) => {
                tl.tracks[tk] = (tl.tracks[tk] || []).filter((x) => x.anchor !== c.id);
              });
            }
          } else kept.push(c);
        });
        tl.tracks[k] = kept;
      });
      tl.tracks.effects = (tl.tracks.effects || []).filter((c) => c.source_job_id !== s.job_id);
      if (selected && findClip(selected.track, selected.id) == null) selected = null;
      renderTimeline();
      scheduleSave();
    }
    sources = sources.filter((x) => x.job_id !== s.job_id);
    renderSourceList();
    if (typeof window.removeJobFromList === "function") {
      window.removeJobFromList(s.job_id);
    }
    setSaveState("Removed from Media");
  }

  async function removeAssetFromMedia(a) {
    if (!a || !a.asset_id) return;
    const used = tl && ["overlay", "music"].some((k) =>
      (tl.tracks[k] || []).some((c) => c.asset_id === a.asset_id));
    const label = a.filename || a.keyword || a.asset_id.slice(0, 6);
    const msg = used
      ? `Delete asset "${label}" and remove timeline clips that use it?`
      : `Delete asset "${label}"?`;
    if (!confirm(msg)) return;
    try {
      await api("/delete-asset/" + a.asset_id, { method: "POST" });
    } catch (e) {
      alert("Could not delete asset: " + e.message);
      return;
    }
    if (tl && used) {
      pushHistory();
      ["overlay", "music"].forEach((k) => {
        tl.tracks[k] = (tl.tracks[k] || []).filter((c) => c.asset_id !== a.asset_id);
      });
      if (tl.logo && tl.logo.asset_id === a.asset_id) tl.logo = null;
      if (selected && findClip(selected.track, selected.id) == null) selected = null;
      renderTimeline();
      scheduleSave();
    }
    await loadAssets();
    setSaveState("Asset deleted");
  }

  /** Bulk-delete every asset checked in the Library panel (uses /delete-asset). */
  async function deleteSelectedAssets() {
    const ids = Array.from(selectedAssetIds).filter((id) => assets.some((a) => a.asset_id === id));
    if (!ids.length) return;
    const plural = ids.length > 1 ? "s" : "";
    if (!confirm(`Delete ${ids.length} selected asset${plural}? This also removes any timeline clips that use them.`)) return;

    const usedAny = tl && ids.some((id) =>
      ["overlay", "music"].some((k) => (tl.tracks[k] || []).some((c) => c.asset_id === id)));
    if (usedAny) pushHistory();

    for (const id of ids) {
      try { await api("/delete-asset/" + id, { method: "POST" }); } catch (e) { /* keep going */ }
      if (tl) {
        ["overlay", "music"].forEach((k) => {
          tl.tracks[k] = (tl.tracks[k] || []).filter((c) => c.asset_id !== id);
        });
        if (tl.logo && tl.logo.asset_id === id) tl.logo = null;
      }
    }
    selectedAssetIds.clear();
    if (tl) {
      if (selected && findClip(selected.track, selected.id) == null) selected = null;
      renderTimeline();
      scheduleSave();
    }
    await loadAssets();
    setSaveState(`Deleted ${ids.length} asset${plural}`);
  }

  function renderAssetList() {
    const wrap = $("tlAssetList");
    if (!wrap) return;
    const replacing = !!_overlayReplaceTargetId;
    document.body.classList.toggle("tl-replace-mode", replacing);
    const banner = $("tlReplaceModeBanner");
    if (banner) {
      banner.hidden = !replacing;
      if (replacing) {
        const clip = findClip("overlay", _overlayReplaceTargetId);
        const label = (clip && (clip.keyword || clip.source)) || "selected overlay";
        banner.innerHTML = `Replace mode · tap <strong>Use</strong> on an asset for “${esc(label)}”. `
          + `<button type="button" class="tl-chip-btn" id="tlReplaceModeCancel">Cancel</button>`;
        const cancel = $("tlReplaceModeCancel");
        if (cancel) cancel.onclick = () => endOverlayReplaceMode();
      }
    }
    wrap.innerHTML = "";
    applyLibraryViewClass();
    if (!assets.length) {
      wrap.innerHTML = '<p class="muted tl-hint">No assets yet. Upload a file or Suggest B-roll overlays.</p>';
      return;
    }
    // Drop selections for assets that no longer exist.
    const liveIds = new Set(assets.map((a) => a.asset_id));
    Array.from(selectedAssetIds).forEach((id) => { if (!liveIds.has(id)) selectedAssetIds.delete(id); });

    if (!replacing) {
      const bulk = document.createElement("div");
      bulk.className = "tl-asset-bulkbar";
      const allChecked = assets.length > 0 && assets.every((a) => selectedAssetIds.has(a.asset_id));
      bulk.innerHTML =
        `<label class="tl-asset-bulk-all"><input type="checkbox" id="tlAssetSelectAll"${allChecked ? " checked" : ""}> Select all</label>` +
        `<button type="button" id="tlAssetDeleteSelected" class="tl-chip-btn tl-chip-danger" ${selectedAssetIds.size ? "" : "disabled"}>🗑 Delete selected (${selectedAssetIds.size})</button>`;
      wrap.appendChild(bulk);
      const allBox = bulk.querySelector("#tlAssetSelectAll");
      if (allBox) {
        allBox.onchange = () => {
          if (allBox.checked) assets.forEach((a) => selectedAssetIds.add(a.asset_id));
          else selectedAssetIds.clear();
          renderAssetList();
        };
      }
      const delBtn = bulk.querySelector("#tlAssetDeleteSelected");
      if (delBtn) delBtn.onclick = () => deleteSelectedAssets();
    }

    assets.forEach((a) => {
      const div = document.createElement("div");
      div.className = "tl-source-item" + (replacing ? " tl-replace-candidate" : "");
      const icon = a.kind === "audio" ? "🎵" : a.kind === "image" ? "🖼" : "🎞";
      const label = a.filename || a.keyword || `${a.ext || a.kind} ${String(a.asset_id || "").slice(0, 6)}`;
      const thumb = (a.kind === "image" || a.kind === "video")
        ? `<img class="tl-asset-thumb" src="/asset/${a.asset_id}" alt="" loading="lazy">`
        : `<span class="tl-asset-thumb tl-asset-thumb-ph" aria-hidden="true">${icon}</span>`;
      const checkbox = replacing ? "" :
        `<input type="checkbox" class="tl-asset-check" data-asset-id="${esc(a.asset_id)}"${selectedAssetIds.has(a.asset_id) ? " checked" : ""} title="Select for bulk delete">`;
      div.innerHTML =
        `${checkbox}${thumb}<span class="tl-source-name" title="${esc(label)}">${esc(label)}${a.duration ? " · " + fmtTime(a.duration) : ""}</span>`;
      const check = div.querySelector(".tl-asset-check");
      if (check) {
        check.onclick = (e) => e.stopPropagation();
        check.onchange = () => {
          if (check.checked) selectedAssetIds.add(a.asset_id);
          else selectedAssetIds.delete(a.asset_id);
          renderAssetList();
        };
      }
      const actions = document.createElement("div");
      actions.className = "tl-source-actions";
      if (a.kind === "audio") {
        const m = document.createElement("button");
        m.className = "tl-chip-btn";
        m.textContent = "🎵";
        m.title = "Add to Music";
        m.onclick = (e) => { e.stopPropagation(); addMusicClip(a); };
        actions.appendChild(m);
      } else if (replacing) {
        const use = document.createElement("button");
        use.className = "tl-chip-btn tl-chip-primary";
        use.textContent = "Use";
        use.title = "Replace selected overlay with this asset";
        use.onclick = (e) => {
          e.stopPropagation();
          replaceSelectedOverlayAsset(a.asset_id, {
            source: a.source || (a.kind === "gif" ? "gif" : (a.kind === "image" ? "photo" : null)),
            keyword: a.keyword || a.filename || null,
          });
        };
        actions.appendChild(use);
      } else {
        const o = document.createElement("button");
        o.className = "tl-chip-btn";
        o.textContent = a.kind === "video" ? "🎬" : "🖼";
        o.title = "Add as overlay (PiP — floats over A-roll, does not take Main time)";
        o.onclick = (e) => { e.stopPropagation(); addOverlayClip({ asset_id: a.asset_id }, a); };
        actions.appendChild(o);
        // Cutaway on Main (takes timeline time). Ken Burns is opt-in via clip props / + Effect.
        const asMain = document.createElement("button");
        asMain.className = "tl-chip-btn";
        asMain.textContent = "✂";
        asMain.title = "Add as Main cutaway (takes time on Main — stills ~2.4s)";
        asMain.onclick = (e) => {
          e.stopPropagation();
          addMainCutawayClip({
            asset_id: a.asset_id,
            start: playheadOutputTime() || 0,
            out: a.kind === "image" ? 2.4 : Math.min(a.duration || 4, 5),
            keyword: a.keyword || a.filename || null,
            source: a.source || (a.kind === "gif" ? "gif" : null),
          }, a);
        };
        actions.appendChild(asMain);
        if (a.kind === "image" || a.kind === "gif" || a.kind === "video") {
          const fx = document.createElement("button");
          fx.className = "tl-chip-btn";
          fx.textContent = "✨";
          fx.title = "Add as Effects split-screen second panel (image/video OK)";
          fx.onclick = (e) => {
            e.stopPropagation();
            addEffectClip("split_screen", {
              asset_id: a.asset_id,
              placement: "second_bottom",
            });
          };
          actions.appendChild(fx);
        }
      }
      if (!replacing) {
        const ren = document.createElement("button");
        ren.className = "tl-chip-btn";
        ren.textContent = "✎";
        ren.title = "Rename this asset";
        ren.onclick = (e) => {
          e.stopPropagation();
          renameAsset(a);
        };
        actions.appendChild(ren);
      }
      const del = document.createElement("button");
      del.className = "tl-chip-btn tl-chip-danger";
      del.textContent = "✕";
      del.title = "Delete this asset";
      del.onclick = (e) => {
        e.stopPropagation();
        removeAssetFromMedia(a);
      };
      actions.appendChild(del);
      div.appendChild(actions);
      _bindLibraryDrag(div, { kind: "asset", asset_id: a.asset_id, asset_kind: a.kind });
      wrap.appendChild(div);
    });
  }

  function beginOverlayReplaceMode(overlayId, opts) {
    opts = opts || {};
    const id = overlayId || (selected && selected.track === "overlay" ? selected.id : null);
    if (!id || !findClip("overlay", id)) {
      alert("Select an Overlay clip first.");
      return false;
    }
    _overlayReplaceTargetId = id;
    selected = { track: "overlay", id };
    if (opts.openMedia !== false) setLeftTab("media", { pin: true });
    renderAssetList();
    setSaveState("Replace mode — pick an asset or generate AI");
    if (typeof window.refreshMobileContextTools === "function") window.refreshMobileContextTools();
    return true;
  }

  function endOverlayReplaceMode() {
    _overlayReplaceTargetId = null;
    document.body.classList.remove("tl-replace-mode");
    const banner = $("tlReplaceModeBanner");
    if (banner) { banner.hidden = true; banner.innerHTML = ""; }
    renderAssetList();
    if (typeof window.refreshMobileContextTools === "function") window.refreshMobileContextTools();
  }

  window.beginOverlayReplaceMode = beginOverlayReplaceMode;
  window.endOverlayReplaceMode = endOverlayReplaceMode;
  window.getOverlayReplaceTargetId = function () { return _overlayReplaceTargetId; };
  window.getSelectedOverlayKeyword = function () {
    const id = _overlayReplaceTargetId
      || (selected && selected.track === "overlay" ? selected.id : null);
    const c = id ? findClip("overlay", id) : null;
    return (c && (c.keyword || "")) || "";
  };

  function esc(s) {
    return String(s || "").replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // ---- Left column tabs (Transcript / Media / Look) ----
  let leftTabPinned = null; // "media" | "transcript" | "look" | null

  function setLeftTab(name, opts) {
    opts = opts || {};
    leftTab = name;
    if (opts.pin) leftTabPinned = name;
    document.querySelectorAll(".tl-lefttab").forEach((b) =>
      b.classList.toggle("active", b.dataset.ltab === name));
    document.querySelectorAll(".tl-leftpanel").forEach((p) =>
      p.classList.toggle("hidden", p.dataset.lpanel !== name));
    if (name === "look") mountCaptionLookIntoTimeline();
    // Phone: keep the bottom sheet open when switching Transcript / Media / Look.
    if (opts.openSheet !== false && document.body.classList.contains("is-phone") &&
        typeof window.openMobileTimelinePanel === "function") {
      if (!document.body.classList.contains("tl-mobile-panel-open") && opts.pin) {
        window.openMobileTimelinePanel(name);
      }
    }
  }
  window.setTimelineLeftTab = setLeftTab;

  function jumpLookSection(which) {
    setLeftTab("look", { pin: true });
    mountCaptionLookIntoTimeline();
    const caps = document.getElementById("captionLookCaptionsSection");
    const audio = document.getElementById("captionLookAudioSection");
    const bg = document.getElementById("bgMusicPanel");
    // Focus one panel so options (fonts / audio checkboxes) are actually visible.
    if (caps && typeof caps.open === "boolean") caps.open = (which !== "audio");
    if (audio && typeof audio.open === "boolean") audio.open = (which === "audio");
    if (bg && typeof bg.open === "boolean" && which === "audio") bg.open = false;
    // Desktop workspace-focus hides the side columns permanently — pop the
    // Look panel into the workspace modal instead so Captions/Audio are reachable.
    if (isWorkspaceFocusDesktop()) {
      openWorkspacePanel(which === "audio" ? "🔊 Audio" : "🎨 Captions", { mode: "left" });
    }
    const id = which === "audio" ? "captionLookAudioSection" : "captionLookCaptionsSection";
    const el = document.getElementById(id);
    if (el) {
      try { el.scrollIntoView({ behavior: "smooth", block: "nearest" }); } catch (_) {}
    }
  }

  function wireLeftTabs() {
    document.querySelectorAll(".tl-lefttab").forEach((b) => {
      b.onclick = () => setLeftTab(b.dataset.ltab, { pin: true });
    });
    document.querySelectorAll("[data-look-jump]").forEach((b) => {
      b.onclick = () => jumpLookSection(b.dataset.lookJump);
    });
  }

  // ---- Workspace-focus modal (desktop): Library / Captions / Audio / Clip
  // props open here instead of living in permanent side columns, so preview +
  // toolbar + timeline stay visible at all times. ----
  function isWorkspaceFocusDesktop() {
    return document.body.classList.contains("tl-workspace-focus") &&
      !document.body.classList.contains("is-phone");
  }

  let _workspaceModalReturn = null; // { node, parent, next } — where to put a moved panel back

  function restoreWorkspaceNode() {
    if (!_workspaceModalReturn) return;
    const { node, parent, next } = _workspaceModalReturn;
    if (parent) {
      if (next && next.parentElement === parent) parent.insertBefore(node, next);
      else parent.appendChild(node);
    }
    _workspaceModalReturn = null;
  }

  /**
   * Open the workspace modal. opts.mode "left" moves #tlLeftPanel (Library /
   * Transcript / Look) into the modal body; "props" moves #tlProps (clip /
   * project properties). opts.fillFn(bodyEl) can render arbitrary content
   * instead. Moving actual DOM nodes (rather than cloning) keeps every
   * existing event listener / render() target working unmodified.
   */
  function openWorkspacePanel(title, opts) {
    opts = opts || {};
    const modal = $("tlWorkspaceModal");
    const body = $("tlWorkspaceModalBody");
    const titleEl = $("tlWorkspaceModalTitle");
    if (!modal || !body) return;
    if (titleEl) titleEl.textContent = title || "Panel";

    let node = null;
    if (opts.mode === "left") node = $("tlLeftPanel");
    else if (opts.mode === "props") node = $("tlProps");

    // Never orphan whatever was previously parked in the modal.
    if (_workspaceModalReturn && _workspaceModalReturn.node !== node) restoreWorkspaceNode();

    if (node) {
      if (node.parentElement !== body) {
        _workspaceModalReturn = { node, parent: node.parentElement, next: node.nextSibling };
        body.innerHTML = "";
        body.appendChild(node);
      }
      node.classList.remove("hidden");
    } else if (typeof opts.fillFn === "function") {
      body.innerHTML = "";
      opts.fillFn(body);
    }

    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    if (typeof opts.onOpen === "function") opts.onOpen(body);
  }

  function closeWorkspacePanel() {
    const modal = $("tlWorkspaceModal");
    const body = $("tlWorkspaceModalBody");
    if (!modal) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    restoreWorkspaceNode();
    if (body) body.innerHTML = "";
  }

  let _captionLookMounted = false;
  function mountCaptionLookIntoTimeline() {
    const mount = $("tlLookMount");
    const root = $("captionLookRoot");
    if (!mount || !root) return;
    if (root.parentElement !== mount) {
      mount.appendChild(root);
      root.classList.add("tl-embedded");
      const back = $("brandingBackIngestBtn");
      if (back) back.classList.add("hidden");
      _captionLookMounted = true;
      wireCaptionLookAutoSync();
    }
    // Keep Captions + Audio Enhancement expanded and reachable in Timeline Look.
    const audioSec = document.getElementById("captionLookAudioSection");
    if (audioSec) {
      if (typeof audioSec.open === "boolean") audioSec.open = true;
      audioSec.classList.remove("hidden");
      audioSec.style.display = "";
    }
    const capsSec = document.getElementById("captionLookCaptionsSection");
    if (capsSec && typeof capsSec.open === "boolean") capsSec.open = true;
  }

  let _lookSyncWired = false;
  function wireCaptionLookAutoSync() {
    if (_lookSyncWired) return;
    const root = $("captionLookRoot");
    if (!root) return;
    _lookSyncWired = true;
    const sync = () => {
      if (!tl) return;
      pullCaptionLookOntoTimeline(null, { quiet: true });
      // Also stash audio settings onto the timeline job when available.
      try {
        if (typeof window.getAudio === "function") {
          tl.audio = window.getAudio();
        } else if (typeof getAudio === "function") {
          tl.audio = getAudio();
        }
      } catch (_) { /* optional */ }
      scheduleSave();
      if (!selected) renderProps();
    };
    root.addEventListener("input", sync);
    root.addEventListener("change", sync);
    root.addEventListener("click", (e) => {
      if (e.target && e.target.closest && e.target.closest("#viralPresets .theme, #themes .theme")) {
        setTimeout(sync, 0);
      }
    });
  }

  async function dropLibraryOnLane(lane, payload) {
    if (!payload || !lane) return;
    if (!(await ensureProject())) return;
    if (payload.kind === "source" && payload.job_id) {
      if (lane === "main") return addMainClip(payload.job_id);
      if (lane === "overlay") return addOverlayClip({ source_job_id: payload.job_id });
      if (lane === "effects") {
        return addEffectClip("split_screen", {
          source_job_id: payload.job_id,
          placement: "second_bottom",
        });
      }
      if (lane === "music") {
        alert("Transcribed videos go on Main / Overlay / Effects — not Music. Upload an audio asset for the music lane.");
        return;
      }
    }
    if (payload.kind === "asset" && payload.asset_id) {
      const asset = assets.find((a) => a.asset_id === payload.asset_id);
      if (!asset) return;
      if (lane === "music" || asset.kind === "audio") {
        if (asset.kind !== "audio") {
          alert("Only audio assets can go on the Music lane.");
          return;
        }
        return addMusicClip(asset);
      }
      if (lane === "main") {
        // Video/image/GIF assets can land as Main cutaways (long-form B-roll).
        if (asset.kind === "image" || asset.kind === "gif" || asset.kind === "video") {
          return addMainCutawayClip({
            asset_id: asset.asset_id,
            start: playheadOutputTime() || 0,
            out: asset.kind === "image" ? 2.4 : Math.min(asset.duration || 4, 5),
            keyword: asset.keyword || asset.filename || null,
            source: asset.source || (asset.kind === "gif" ? "gif" : null),
          }, asset);
        }
        return addOverlayClip({ asset_id: asset.asset_id }, asset);
      }
      if (lane === "overlay") return addOverlayClip({ asset_id: asset.asset_id }, asset);
      if (lane === "effects") {
        // Split-screen second panel can be a still, GIF, or video asset.
        if (asset.kind === "image" || asset.kind === "gif" || asset.kind === "video") {
          return addEffectClip("split_screen", {
            asset_id: asset.asset_id,
            placement: "second_bottom",
          });
        }
        alert("Drop images / GIFs / videos on Effects for split-screen. Use + Effect for Ken Burns / punch zoom.");
        return;
      }
    }
  }

  function wireLibraryLaneDrop() {
    document.querySelectorAll(".tl-track-lane[data-lane]").forEach((laneEl) => {
      const lane = laneEl.dataset.lane;
      if (!lane || lane === "text") return;
      laneEl.addEventListener("dragover", (e) => {
        if (!e.dataTransfer) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
        laneEl.classList.add("drag-over");
      });
      laneEl.addEventListener("dragleave", () => laneEl.classList.remove("drag-over"));
      laneEl.addEventListener("drop", async (e) => {
        e.preventDefault();
        laneEl.classList.remove("drag-over");
        let payload = null;
        try {
          payload = JSON.parse(e.dataTransfer.getData("application/x-tl-lib") || "null");
        } catch (_) { payload = null; }
        if (!payload) return;
        await dropLibraryOnLane(lane, payload);
      });
    });
  }

  // ---- Transcript-first editing (Phase 4: strike + rename + fillers) ----
  const _TL_FILLER_SINGLE = new Set([
    "um", "uh", "uhh", "uhm", "umm", "er", "erm",
    "ah", "ahh", "hm", "hmm", "mm", "mhm",
    "like", "basically", "literally", "actually",
    "kinda", "sorta", "anyway", "anyways",
    "okay", "ok", "right", "well",
  ]);
  const _TL_FILLER_PAIRS = [
    ["you", "know"], ["i", "mean"], ["sort", "of"], ["kind", "of"],
  ];

  function _tlStripWord(s) {
    return String(s || "").toLowerCase().replace(/[^a-z']/g, "");
  }

  function wordOverrideKey(w) {
    return Number(w.start || 0).toFixed(3);
  }

  function displayWordText(clip, w) {
    const ov = clip && clip.word_overrides;
    const key = wordOverrideKey(w);
    if (ov && ov[key] != null && String(ov[key]).length) return String(ov[key]);
    return String(w.word || "");
  }

  function setWordOverride(clip, w, text) {
    const key = wordOverrideKey(w);
    const cleaned = String(text || "").trim();
    if (!cleaned || cleaned === String(w.word || "").trim()) {
      if (clip.word_overrides) {
        delete clip.word_overrides[key];
        if (!Object.keys(clip.word_overrides).length) clip.word_overrides = null;
      }
      return;
    }
    clip.word_overrides = Object.assign({}, clip.word_overrides || {});
    clip.word_overrides[key] = cleaned;
  }

  function cutStats(clip) {
    const words = transcriptWords || [];
    let cutWords = 0;
    let cutSec = 0;
    const ranges = keepRangesForClip(clip, { allowEmpty: true });
    const full = Math.max(0, (clip.out || 0) - (clip.in || 0));
    const kept = ranges.reduce((a, [s, e]) => a + Math.max(0, e - s), 0);
    cutSec = Math.max(0, full - kept);
    words.forEach((w) => { if (isWordCut(clip, w)) cutWords++; });
    return {
      total: words.length,
      cutWords,
      cutSec,
      keptSec: kept,
      overrides: clip.word_overrides ? Object.keys(clip.word_overrides).length : 0,
    };
  }

  function updateTranscriptToolbar(clip) {
    const bar = $("tlTranscriptToolbar");
    const meta = $("tlTranscriptMeta");
    if (!bar) return;
    if (!clip || !transcriptWords || !transcriptWords.length) {
      bar.classList.add("hidden");
      return;
    }
    bar.classList.remove("hidden");
    const s = cutStats(clip);
    if (meta) {
      meta.textContent =
        `${s.total} words` +
        (s.cutWords ? ` · ${s.cutWords} cut (${s.cutSec.toFixed(1)}s)` : "") +
        (s.overrides ? ` · ${s.overrides} renames` : "");
    }
  }

  function seekTranscriptWord(clip, w) {
    if (!clip || !w) return;
    const v = $("tlPreviewVideo");
    if (!v) return;
    // Keep this Main clip selected so the playhead maps source → output correctly.
    if (clip.id && tl && tl.tracks && tl.tracks.main) {
      const exists = tl.tracks.main.some((c) => c.id === clip.id);
      if (exists) {
        selected = { track: "main", id: clip.id };
        logoSelected = false;
      }
    }
    const src = clip.source_job_id ? ("/raw-upload/" + clip.source_job_id) : null;
    if (src && v.getAttribute("src") !== src) {
      v.src = src;
      previewingOutput = false;
      const wrap = v.closest(".tl-preview");
      if (wrap) wrap.classList.add("has-video");
    } else {
      previewingOutput = false;
    }
    const t = Math.max(clip.in || 0, Math.min(clip.out || 1e9, Number(w.start) || 0));
    const apply = () => {
      try { v.currentTime = t; } catch (e) { /* ignore */ }
      highlightTranscriptAt(t);
      updatePlayhead();
      updateStageCompositor();
      renderTracks();
      renderProps();
    };
    if (src && v.readyState < 1) {
      v.addEventListener("loadedmetadata", apply, { once: true });
      v.load();
    } else {
      apply();
    }
  }

  async function renderTranscript(clip) {
    const doc = $("tlTranscriptDoc");
    const hint = $("tlTranscriptHint");
    if (!doc) return;
    transcriptWords = null;
    updateTranscriptToolbar(null);
    if (!clip || !clip.source_job_id) {
      doc.innerHTML = "";
      if (hint) hint.textContent = "Select a Main clip to edit its words.";
      return;
    }
    doc.innerHTML = '<p class="muted">Loading transcript…</p>';
    let words = [];
    try {
      const s = await api("/status/" + clip.source_job_id);
      words = (s.words || []).filter((w) =>
        Number(w.end) > (clip.in || 0) && Number(w.start) < (clip.out || 1e9));
    } catch (e) { doc.innerHTML = '<p class="muted">Could not load transcript.</p>'; return; }
    if (!words.length) {
      doc.innerHTML = '<p class="muted">No transcript words in this clip\'s range. Transcribe the source video to enable text editing.</p>';
      return;
    }
    transcriptWords = words;
    if (hint) {
      hint.textContent =
        "Click a phrase to seek · edit text · × cuts the phrase · Shift+click a word to cut one · Double-click a word to rename.";
    }
    renderTranscriptWords(clip);
    updateTranscriptToolbar(clip);
    refreshTlAnalyzeStatus(clip.source_job_id);
  }

  function _fmtTlTime(sec) {
    const s = Math.max(0, Number(sec) || 0);
    const m = Math.floor(s / 60);
    const r = s - m * 60;
    return m + ":" + r.toFixed(1).padStart(4, "0");
  }

  function refreshTlAnalyzeStatus(jobId) {
    const el = $("tlAnalyzeStatus");
    const btn = $("tlAnalyzeBtn");
    if (!el || !jobId) return;
    fetch("/reframe-status/" + jobId)
      .then((r) => r.json())
      .then((data) => {
        if (data.ready && data.stats) {
          const faceN = data.stats.face_samples || 0;
          const faceNote = data.stats.faces_skipped
            ? " · faces skipped"
            : (faceN ? `, ${faceN} faces` : "");
          el.textContent = `✓ ${data.stats.speaker_count} speakers${faceNote}`;
          if (btn) btn.textContent = "Re-analyze";
        } else if (data.error) {
          el.textContent = data.error;
        } else {
          el.textContent = "Speakers optional — Analyze when you want colors / 9:16 reframe.";
          if (btn) btn.textContent = "Analyze speakers";
        }
      })
      .catch(() => { /* silent */ });
  }

  function isWordCut(clip, w) {
    return (clip.cuts || []).some(([cs, ce]) =>
      Number(w.start) >= cs - 0.01 && Number(w.end) <= ce + 0.01);
  }

  function _spkColor(sc, speaker) {
    if (!speaker) return null;
    if (sc[speaker]) return sc[speaker];
    if (speaker === "SPEAKER_00" && sc.Host) return sc.Host;
    if (speaker === "SPEAKER_01" && sc.Guest) return sc.Guest;
    const m = /SPEAKER_(\d+)/i.exec(speaker);
    if (!m) return null;
    const palette = [
      "#FFD700", "#00E5FF", "#a3be8c", "#b48ead", "#d08770",
      "#88c0d0", "#bf616a", "#5e81ac", "#ebcb8b", "#c084fc",
    ];
    return palette[parseInt(m[1], 10) % palette.length];
  }

  function _spkLabel(speaker) {
    if (speaker === "SPEAKER_00") return "Host";
    if (speaker === "SPEAKER_01") return "Guest";
    const m = /SPEAKER_(\d+)/i.exec(speaker || "");
    if (m) return "Speaker " + (parseInt(m[1], 10) + 1);
    return speaker || "";
  }

  function _detectTlFillerIndices(words, clip) {
    const flagged = new Set();
    if (!words || !words.length) return flagged;
    for (let i = 0; i < words.length; i++) {
      const tok = _tlStripWord(displayWordText(clip, words[i]));
      if (!tok) continue;
      if (_TL_FILLER_SINGLE.has(tok)) { flagged.add(i); continue; }
      if (i + 1 < words.length) {
        const next = _tlStripWord(displayWordText(clip, words[i + 1]));
        for (const [a, b] of _TL_FILLER_PAIRS) {
          if (tok === a && next === b) { flagged.add(i); flagged.add(i + 1); }
        }
      }
    }
    return flagged;
  }

  function renderTranscriptWords(clip) {
    const doc = $("tlTranscriptDoc");
    if (!doc || !transcriptWords) return;
    doc.innerHTML = "";
    const sc = (tl && tl.speaker_colors) || {};
    const fillers = _detectTlFillerIndices(transcriptWords, clip);
    const style = normalizeTlStyle((tl && tl.style) || {});
    const groupSize = Math.max(1, Math.min(5, Number(style.group_size) || 3));

    for (let i = 0; i < transcriptWords.length; i += groupSize) {
      const group = transcriptWords.slice(i, i + groupSize);
      if (!group.length) continue;

      const row = document.createElement("div");
      row.className = "tl-phrase-row";
      row.dataset.start = String(group[0].start);
      row.dataset.end = String(group[group.length - 1].end);
      row.dataset.from = String(i);

      const spCounts = {};
      group.forEach((w) => {
        if (w.speaker) spCounts[w.speaker] = (spCounts[w.speaker] || 0) + 1;
      });
      const dominantSp = Object.keys(spCounts).sort((a, b) => spCounts[b] - spCounts[a])[0] || "";
      if (dominantSp) {
        row.classList.add("has-speaker");
        const rail = _spkColor(sc, dominantSp) || "#FFD700";
        row.style.setProperty("--tl-spk", rail);
      }

      const timeEl = document.createElement("span");
      timeEl.className = "tl-phrase-time";
      timeEl.textContent = _fmtTlTime(group[0].start);
      timeEl.title = "Seek to " + _fmtTlTime(group[0].start)
        + (dominantSp ? " · " + _spkLabel(dominantSp) : "");

      const textEl = document.createElement("div");
      textEl.className = "tl-phrase-text";

      group.forEach((w, j) => {
        const abs = i + j;
        const sp = document.createElement("span");
        const cut = isWordCut(clip, w);
        const renamed = !!(clip.word_overrides && clip.word_overrides[wordOverrideKey(w)]);
        sp.className = "tl-tword"
          + (cut ? " cut" : "")
          + (fillers.has(abs) && !cut ? " filler" : "")
          + (renamed ? " renamed" : "");
        sp.textContent = displayWordText(clip, w) + " ";
        sp.dataset.start = w.start;
        sp.dataset.idx = String(abs);
        const col = _spkColor(sc, w.speaker);
        if (col && !cut) sp.style.color = col;
        const bits = [];
        if (w.speaker) bits.push(_spkLabel(w.speaker));
        if (renamed) bits.push("renamed");
        if (fillers.has(abs)) bits.push("filler");
        bits.push("⇧ cut · dbl-click rename");
        sp.title = bits.join(" · ");
        sp.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          if (e.shiftKey) toggleWordCut(clip, w);
          else seekTranscriptWord(clip, w);
        });
        sp.addEventListener("dblclick", (e) => {
          e.preventDefault();
          e.stopPropagation();
          beginWordRename(clip, w, sp);
        });
        textEl.appendChild(sp);
      });

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "tl-phrase-del";
      delBtn.textContent = "×";
      delBtn.title = "Cut this whole phrase from the clip";
      delBtn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        cutPhraseWords(clip, group);
      });

      row.addEventListener("click", (e) => {
        if (delBtn.contains(e.target)) return;
        // Word spans handle their own seek; phrase/time click seeks to phrase start.
        if (e.target && e.target.closest && e.target.closest(".tl-tword")) return;
        seekTranscriptWord(clip, group[0]);
        doc.querySelectorAll(".tl-phrase-row").forEach((r) => r.classList.remove("active"));
        row.classList.add("active");
      });

      row.appendChild(timeEl);
      row.appendChild(textEl);
      row.appendChild(delBtn);
      doc.appendChild(row);
    }
    updateTranscriptToolbar(clip);
  }

  function cutPhraseWords(clip, group) {
    if (!clip || !group || !group.length) return;
    pushHistory();
    const cuts = (clip.cuts || []).slice();
    group.forEach((w) => {
      if (!w || isWordCut(clip, w)) return;
      cuts.push([Number(w.start), Number(w.end)]);
    });
    clip.cuts = mergeCuts(cuts);
    renderTranscriptWords(clip);
    applyAnchors();
    renderTracks();
    scheduleSave();
  }

  function beginWordRename(clip, w, spanEl) {
    if (!clip || !w || !spanEl) return;
    if (spanEl.querySelector("input")) return;
    pushHistory();
    const current = displayWordText(clip, w);
    const input = document.createElement("input");
    input.type = "text";
    input.className = "tl-tword-edit";
    input.value = current;
    spanEl.textContent = "";
    spanEl.appendChild(input);
    input.focus();
    input.select();
    let done = false;
    const finish = (commit) => {
      if (done) return;
      done = true;
      if (commit) setWordOverride(clip, w, input.value);
      renderTranscriptWords(clip);
      scheduleSave();
      updateLiveCaptions(playheadOutputTime() || 0);
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
      e.stopPropagation();
    });
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("click", (e) => e.stopPropagation());
  }

  function highlightTranscriptAt(t) {
    const doc = $("tlTranscriptDoc");
    if (!doc) return;
    const spans = doc.querySelectorAll(".tl-tword");
    let best = -1;
    spans.forEach((sp, i) => { if (parseFloat(sp.dataset.start) <= t) best = i; });
    spans.forEach((sp, i) => sp.classList.toggle("playing", i === best));
    const rows = doc.querySelectorAll(".tl-phrase-row");
    rows.forEach((row) => {
      const a = parseFloat(row.dataset.start);
      const b = parseFloat(row.dataset.end);
      row.classList.toggle("active", Number.isFinite(a) && Number.isFinite(b) && t >= a && t < b + 0.05);
    });
  }

  function mergeCuts(cuts) {
    const sorted = cuts.slice().sort((a, b) => a[0] - b[0]);
    const merged = [];
    sorted.forEach((r) => {
      const last = merged[merged.length - 1];
      if (last && r[0] <= last[1] + 0.05) last[1] = Math.max(last[1], r[1]);
      else merged.push([r[0], r[1]]);
    });
    return merged;
  }

  function toggleWordCut(clip, w) {
    pushHistory();
    const cuts = (clip.cuts || []).slice();
    if (isWordCut(clip, w)) {
      clip.cuts = cuts.filter(([cs, ce]) =>
        !(Number(w.start) >= cs - 0.01 && Number(w.end) <= ce + 0.01));
    } else {
      cuts.push([Number(w.start), Number(w.end)]);
      clip.cuts = mergeCuts(cuts);
    }
    renderTranscriptWords(clip);
    applyAnchors();   // cuts change Main duration → reflow anchored items
    renderTracks();
    scheduleSave();
  }

  function restoreAllCuts(clip) {
    if (!clip || !(clip.cuts || []).length) return;
    pushHistory();
    clip.cuts = [];
    renderTranscriptWords(clip);
    applyAnchors();
    renderTracks();
    scheduleSave();
  }

  function cutFillerWords(clip) {
    if (!clip || !transcriptWords || !transcriptWords.length) return;
    const idxs = _detectTlFillerIndices(transcriptWords, clip);
    if (!idxs.size) {
      alert("No filler words detected in this clip.");
      return;
    }
    pushHistory();
    const cuts = (clip.cuts || []).slice();
    idxs.forEach((i) => {
      const w = transcriptWords[i];
      if (!w || isWordCut(clip, w)) return;
      cuts.push([Number(w.start), Number(w.end)]);
    });
    clip.cuts = mergeCuts(cuts);
    renderTranscriptWords(clip);
    applyAnchors();
    renderTracks();
    scheduleSave();
  }

  function wireTranscriptToolbar() {
    const restoreBtn = $("tlTranscriptRestoreBtn");
    const fillerBtn = $("tlTranscriptFillersBtn");
    const analyzeBtn = $("tlAnalyzeBtn");
    if (restoreBtn) {
      restoreBtn.onclick = () => {
        if (!selected || selected.track !== "main") return;
        const clip = findClip("main", selected.id);
        if (clip) restoreAllCuts(clip);
      };
    }
    if (fillerBtn) {
      fillerBtn.onclick = () => {
        if (!selected || selected.track !== "main") return;
        const clip = findClip("main", selected.id);
        if (clip) cutFillerWords(clip);
      };
    }
    if (analyzeBtn) {
      analyzeBtn.onclick = () => {
        const clip = (selected && selected.track === "main")
          ? findClip("main", selected.id)
          : ((tl && tl.tracks && tl.tracks.main && tl.tracks.main[0]) || null);
        if (!clip || !clip.source_job_id) {
          alert("Select a Main clip with a transcribed source, then Analyze speakers.");
          return;
        }
        const statusEl = $("tlAnalyzeStatus");
        if (typeof window.startReframeAnalyze === "function") {
          window.startReframeAnalyze(analyzeBtn, {
            jobId: clip.source_job_id,
            onStatus: (msg) => { if (statusEl) statusEl.textContent = msg || ""; },
          });
        } else {
          alert("Analyze is still loading — try again in a second.");
        }
      };
    }
    window.onTimelineAnalyzeReady = (jobId) => {
      // After diarization, stamp colors onto project + refresh phrase rails.
      fetch("/stamp-speakers/" + jobId, { method: "POST" }).catch(() => {});
      fetch("/reframe-status/" + jobId)
        .then((r) => r.json())
        .then((data) => {
          if (!tl || !data.ready || !data.stats) return;
          const breakdown = (data.stats.speaker_breakdown) || [];
          tl.speaker_colors = tl.speaker_colors || {};
          breakdown.forEach((spk, i) => {
            if (!spk || !spk.id) return;
            if (!tl.speaker_colors[spk.id]) {
              tl.speaker_colors[spk.id] = _spkColor({}, spk.id) ||
                ["#FFD700", "#00E5FF", "#a3be8c", "#b48ead", "#d08770"][i % 5];
            }
          });
          scheduleSave();
          renderProps();
          const clip = (selected && selected.track === "main")
            ? findClip("main", selected.id)
            : null;
          if (clip && clip.source_job_id === jobId) renderTranscript(clip);
          else if (clip) renderTranscriptWords(clip);
          refreshTlAnalyzeStatus(jobId);
        })
        .catch(() => {});
    };
  }

  // Make sure a project exists before adding anything (guards the race where
  // the source list renders its + buttons before init finishes creating one).
  async function ensureProject() {
    if (tl) return true;
    await newProject();
    return !!tl;
  }

  // ---- Add clips ----
  async function addMainClip(jobId, inS, outS, opts) {
    opts = opts || {};
    try {
      if (!(await ensureProject())) return;
      if (!opts.skipHistory) pushHistory();
      const dur = await getSourceDuration(jobId);
      const ci = inS != null ? Math.max(0, inS) : 0;
      const co = outS != null ? Math.min(dur, outS) : dur;
      tl.tracks.main.push({
        id: uid(), source_job_id: jobId, in: ci, out: co > ci ? co : dur,
        _max: dur, transition: null, burn_captions: true,
      });
      if (!opts.skipRender) {
        renderTimeline();
        scheduleSave();
      }
    } catch (e) {
      alert("Couldn't add clip: " + e.message);
    }
  }

  /** Insert a still/video asset as a Main cutaway, splitting A-roll at *start* (output time). */
  async function addMainCutawayClip(ref, asset) {
    if (!(await ensureProject())) return null;
    if (!tl.tracks.main.length) {
      alert("Add an A-roll clip on Main first, then accept B-roll as a cutaway.");
      return null;
    }
    pushHistory();
    const dur = Math.max(
      0.8,
      ref.out != null
        ? Number(ref.out) - Number(ref.in || 0)
        : (asset && asset.kind === "image" ? 2.4 : Math.min((asset && asset.duration) || 4, 5)),
    );
    const tStart = Math.max(0, Number(ref.start != null ? ref.start : 0));
    const cut = {
      id: uid(),
      source_job_id: null,
      asset_id: ref.asset_id || (asset && asset.asset_id) || null,
      in: 0,
      out: dur,
      _max: dur,
      burn_captions: false,
      cutaway: true,
      keyword: ref.keyword || null,
      source: ref.source || null,
      // Ken Burns is opt-in (inspector / + Effect) — never auto-enable on place.
      ken_burns: ref.ken_burns ? Object.assign({}, ref.ken_burns) : null,
      transition: null,
    };
    if (ref.source === "gif" || (asset && asset.kind === "gif")) {
      cut.ken_burns = null;
    }
    if (!cut.asset_id) {
      alert("Cutaway needs an asset.");
      return null;
    }
    insertCutawayIntoMain(tStart, cut);
    selected = { track: "main", id: cut.id };
    renderTimeline();
    scheduleSave();
    return cut;
  }

  function insertCutawayIntoMain(tStart, cutClip) {
    const cutDur = clipDuration(cutClip);
    let cursor = 0;
    for (let i = 0; i < tl.tracks.main.length; i++) {
      const c = tl.tracks.main[i];
      const cDur = clipDuration(c);
      if (tStart >= cursor + cDur - 0.02) {
        cursor += cDur;
        continue;
      }
      // Asset cutaways: insert before this clip if we're at its leading edge,
      // otherwise replace a slice of A-roll (source_job) or splice between assets.
      if (c.asset_id && !c.source_job_id) {
        if (tStart <= cursor + 0.05) {
          tl.tracks.main.splice(i, 0, cutClip);
        } else {
          tl.tracks.main.splice(i + 1, 0, cutClip);
        }
        return;
      }
      const local = Math.max(0, tStart - cursor);
      const srcIn = Number(c.in) || 0;
      const midStart = srcIn + local;
      const midEnd = Math.min(Number(c.out) || midStart + cutDur, midStart + cutDur);
      const slice = Math.max(0.8, midEnd - midStart);
      cutClip.out = slice;
      cutClip._max = slice;
      const parts = [];
      if (midStart > srcIn + 0.08) {
        const before = Object.assign({}, c, { id: uid(), out: midStart, transition: null });
        before.cuts = (c.cuts || []).filter((r) => r[0] < midStart);
        parts.push(before);
      }
      parts.push(cutClip);
      if (midEnd < (Number(c.out) || 0) - 0.08) {
        const after = Object.assign({}, c, {
          id: uid(), in: midEnd, transition: null,
        });
        after.cuts = (c.cuts || []).filter((r) => r[1] > midEnd);
        parts.push(after);
      }
      tl.tracks.main.splice(i, 1, ...parts);
      return;
    }
    tl.tracks.main.push(cutClip);
  }

  async function addOverlayClip(ref, asset) {
    if (!(await ensureProject())) return null;
    pushHistory();
    let max = 4;
    if (ref.source_job_id) {
      try { max = await getSourceDuration(ref.source_job_id); } catch (e) {}
    } else if (asset && asset.duration) {
      max = asset.duration || 4;
    } else if (ref.out != null && ref.in != null) {
      max = Math.max(4, Number(ref.out) - Number(ref.in) || 4);
    }
    const out = ref.out != null
      ? Number(ref.out)
      : (ref.asset_id && asset && asset.kind === "image" ? 4 : Math.min(max, 5));
    const oc = {
      id: uid(),
      source_job_id: ref.source_job_id || null,
      asset_id: ref.asset_id || null,
      in: ref.in != null ? Number(ref.in) : 0,
      out: out > 0 ? out : Math.min(max, 5),
      _max: max,
      start: ref.start != null ? Number(ref.start) : 0,
      x: ref.x != null ? Number(ref.x) : 0.58,
      y: ref.y != null ? Number(ref.y) : 0.06,
      w: ref.w != null ? Number(ref.w) : 0.34,
      h: ref.h != null ? Number(ref.h) : 0.22,
      opacity: ref.opacity != null ? Number(ref.opacity) : 1.0,
      fit: ref.fit || "cover",
      fade_in: ref.fade_in != null ? Number(ref.fade_in) : 0.15,
      fade_out: ref.fade_out != null ? Number(ref.fade_out) : 0.2,
      border_px: ref.border_px != null ? Number(ref.border_px) : 0,
      layout: ref.layout || null,
      keyword: ref.keyword || null,
      source: ref.source || null,
      // Ken Burns is opt-in via clip props — do not auto-apply on place.
      ken_burns: ref.ken_burns ? Object.assign({}, ref.ken_burns) : null,
    };
    // GIFs keep their own animation — no Ken Burns.
    if (ref.source === "gif" || (asset && asset.kind === "gif") ||
        (ref.asset_id && /\.gif$/i.test(String((asset && asset.filename) || "")))) {
      oc.ken_burns = null;
    }
    // Legacy auto-fetch used percent coords / scale — normalize if needed.
    if (oc.x > 1.5) oc.x = Math.min(1, oc.x / 100);
    if (oc.y > 1.5) oc.y = Math.min(1, oc.y / 100);
    if (ref.scale != null && ref.w == null) oc.w = Math.min(1, Number(ref.scale) / 100);
    reanchor(oc);
    tl.tracks.overlay.push(oc);
    selectClip("overlay", oc.id);
    renderTimeline();
    scheduleSave();
    return oc;
  }

  const OVERLAY_LAYOUTS = {
    pip_tr: { x: 0.58, y: 0.06, w: 0.36, h: 0.22, fit: "cover", label: "PiP TR" },
    pip_tl: { x: 0.04, y: 0.06, w: 0.36, h: 0.22, fit: "cover", label: "PiP TL" },
    pip_br: { x: 0.58, y: 0.68, w: 0.36, h: 0.22, fit: "cover", label: "PiP BR" },
    pip_bl: { x: 0.04, y: 0.68, w: 0.36, h: 0.22, fit: "cover", label: "PiP BL" },
    full:   { x: 0, y: 0, w: 1, h: 1, fit: "cover", label: "Full-bleed" },
    // Always · Photo Match default — large centered still (not edge-to-edge).
    center: { x: 0.08, y: 0.12, w: 0.84, h: 0.55, fit: "cover", label: "Center match" },
    lower:  { x: 0.08, y: 0.62, w: 0.84, h: 0.28, fit: "cover", label: "Lower media" },
  };

  function applyOverlayLayout(clip, layoutId) {
    const L = OVERLAY_LAYOUTS[layoutId];
    if (!clip || !L) return;
    pushHistory();
    clip.layout = layoutId;
    clip.x = L.x; clip.y = L.y; clip.w = L.w; clip.h = L.h; clip.fit = L.fit;
    renderTimeline();
    scheduleSave();
  }

  async function addMusicClip(asset) {
    if (!(await ensureProject())) return;
    pushHistory();
    const max = asset.duration || 60;
    const mc = {
      id: uid(), asset_id: asset.asset_id, in: 0, out: max, _max: max,
      start: 0, gain_db: -18, duck: true,
    };
    reanchor(mc);
    tl.tracks.music.push(mc);
    selectClip("music", mc.id);
    renderTimeline();
    scheduleSave();
  }

  async function addTitle() {
    if (!(await ensureProject())) return;
    pushHistory();
    const tc = {
      id: uid(), text: "Lower third\nName · Title", start: 0, out: 4,
      x: 0.5, y: 0.82, size: 56, color: "#FFFFFF", font: "Anton",
      bg_enabled: true, bg_color: "#000000", bg_opacity: 0.55,
      outline_color: "#000000", outline_width: 0, shadow: 0,
      bold: true, align: 2, anim: "fade",
    };
    reanchor(tc);
    tl.tracks.text.push(tc);
    selectClip("text", tc.id);
    renderTimeline();
    scheduleSave();
  }

  async function addEffectClip(type, opts) {
    opts = opts || {};
    if (!(await ensureProject())) return null;
    if (!tl.tracks.effects) tl.tracks.effects = [];
    pushHistory();
    const meta = EFFECT_TYPES.find((t) => t.id === type) || EFFECT_TYPES[0];
    const ftype = meta.id;
    const v = $("tlPreviewVideo");
    // Anchor new effect blocks to the *output* playhead (where the red
    // playhead / grade currently applies) rather than raw source time —
    // otherwise Effects lane blocks land in the wrong spot whenever the
    // preview is showing a trimmed/reordered Main clip.
    const start = opts.start != null ? Number(opts.start) : (playheadOutputTime() ?? (v ? (v.currentTime || 0) : 0));
    const dur = opts.out != null ? Number(opts.out) : (
      ftype === "punch_zoom" ? 1.2 :
      (ftype === "zoom_1_5" || ftype === "zoom_2x") ? 2.5 :
      ftype === "ken_burns" ? 4 : ftype === "split_screen" ? 4 : 3
    );
    const ec = {
      id: uid(),
      type: ftype,
      start: Math.max(0, start),
      out: Math.max(0.2, dur),
      intensity: opts.intensity || "med",
      direction: opts.direction || "in",
      layout: opts.layout || "auto",
      source_job_id: opts.source_job_id || null,
      asset_id: opts.asset_id || null,
      in: opts.in != null ? Number(opts.in) : 0,
      preset: opts.preset || "none",
      brightness: opts.brightness != null ? Number(opts.brightness) : 0,
      contrast: opts.contrast != null ? Number(opts.contrast) : 1,
      saturation: opts.saturation != null ? Number(opts.saturation) : 1,
      hit: opts.hit != null ? Number(opts.hit) : 0,
      decay: opts.decay != null ? Number(opts.decay) : 0.45,
      anchor: opts.anchor || null,
      placement: opts.placement || (ftype === "split_screen" ? "second_bottom" : null),
    };
    if (opts.quote) ec.quote = opts.quote;
    if (opts.reason) ec.reason = opts.reason;
    reanchor(ec);
    tl.tracks.effects.push(ec);
    selectClip("effects", ec.id);
    renderTimeline();
    scheduleSave();
    return ec;
  }

  // Small modal listing EFFECT_TYPES as buttons. Reuses #tlWorkspaceModal so
  // it works on both desktop and mobile without any extra markup.
  function openEffectPicker() {
    openWorkspacePanel("✨ Add effect", {
      fillFn: (body) => {
        const wrap = document.createElement("div");
        wrap.className = "tl-effect-picker";
        wrap.style.cssText = "display:flex;flex-direction:column;gap:8px;padding:4px 2px";
        EFFECT_TYPES.forEach((t) => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "tl-chip-btn";
          btn.style.cssText = "text-align:left;justify-content:flex-start;font-size:14px;padding:10px 12px";
          btn.textContent = `${t.icon}  ${t.label}`;
          btn.onclick = async () => {
            closeWorkspacePanel();
            const ec = await addEffectClip(t.id);
            if (ec && t.id === "split_screen") {
              // Split-screen needs a second source + timeframe picked right away.
              openWorkspacePanel("✎ Clip properties", { mode: "props" });
            }
          };
          wrap.appendChild(btn);
        });
        body.innerHTML = "";
        body.appendChild(wrap);
      },
    });
  }

  // Mini dialog for picking the split-screen second video's IN point by
  // scrubbing the actual footage instead of guessing a number.
  function openSplitScrubDialog(c) {
    if (!c || !c.source_job_id) {
      alert("Scrub works on a second *video* source. Stills don't need an IN point.");
      return;
    }
    const overlay = document.createElement("div");
    overlay.className = "tl-split-scrub-overlay";
    overlay.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:9999;display:flex;align-items:center;justify-content:center;padding:16px";
    const box = document.createElement("div");
    box.style.cssText =
      "background:#161a24;border:1px solid #333c4d;border-radius:12px;padding:16px;max-width:480px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,.5)";
    box.innerHTML =
      `<h3 style="margin:0 0 10px;font-size:15px;color:#fff">Scrub 2nd video</h3>` +
      `<video id="tlSplitScrubVideo" src="/raw-upload/${encodeURIComponent(c.source_job_id)}" controls playsinline ` +
      `style="width:100%;max-height:60vh;border-radius:8px;background:#000;display:block"></video>` +
      `<div style="display:flex;align-items:center;gap:8px;margin-top:12px">` +
      `<span class="muted" id="tlSplitScrubTime" style="font-size:.78rem;margin-right:auto">0.0s</span>` +
      `<button type="button" class="tl-chip-btn" id="tlSplitScrubCancel">Cancel</button>` +
      `<button type="button" class="btn btn-primary" id="tlSplitScrubUse">Use current time as 2nd IN</button>` +
      `</div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    const video = box.querySelector("#tlSplitScrubVideo");
    const timeEl = box.querySelector("#tlSplitScrubTime");
    const updTime = () => { timeEl.textContent = `${(video.currentTime || 0).toFixed(1)}s`; };
    video.addEventListener("timeupdate", updTime);
    video.addEventListener("loadedmetadata", () => {
      if (c.in) { try { video.currentTime = Math.min(c.in, video.duration || c.in); } catch (e) { /* ignore */ } }
      updTime();
    }, { once: true });

    const close = () => overlay.remove();
    box.querySelector("#tlSplitScrubCancel").onclick = close;
    box.querySelector("#tlSplitScrubUse").onclick = () => {
      pushHistory();
      c.in = Math.max(0, video.currentTime || 0);
      close();
      renderProps();
      scheduleSave();
    };
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  }

  // Inject Effects lane + toolbar button if the HTML template is stale
  // (common when JS cache-busts ahead of a Flask restart / old index.html).
  function ensureEffectsChrome() {
    const tracks = $("tlTracks");
    // Titles lane is intentionally removed from the UI — don't re-inject it,
    // and don't use it as an insertion anchor for the Effects lane anymore.
    if (tracks && !document.querySelector('.tl-track[data-track="effects"]')) {
      const row = document.createElement("div");
      row.className = "tl-track";
      row.dataset.track = "effects";
      row.innerHTML =
        '<div class="tl-track-label">✨ Effects</div>' +
        '<div class="tl-track-lane" data-lane="effects"></div>';
      const overlay = tracks.querySelector('.tl-track[data-track="overlay"]');
      const music = tracks.querySelector('.tl-track[data-track="music"]');
      if (overlay && overlay.nextSibling) tracks.insertBefore(row, overlay.nextSibling);
      else if (music) tracks.insertBefore(row, music);
      else tracks.appendChild(row);
      console.log("[timeline] injected missing Effects track into DOM");
    }
    if (!$("tlAddEffectBtn")) {
      const anchorBtn = $("tlDupBtn") || $("tlPasteBtn") || $("tlCopyBtn") || $("tlDeleteBtn");
      if (anchorBtn && anchorBtn.parentNode) {
        const btn = document.createElement("button");
        btn.id = "tlAddEffectBtn";
        btn.className = "tl-chip-btn";
        btn.title = "Add a timed effect on the Effects lane (split-screen, punch, Ken Burns, color)";
        btn.textContent = "+ Effect";
        anchorBtn.parentNode.insertBefore(btn, anchorBtn.nextSibling);
        console.log("[timeline] injected missing + Effect button into DOM");
      }
    }
    const fxBtn = $("tlAddEffectBtn");
    if (fxBtn && !fxBtn.dataset.wired) {
      fxBtn.dataset.wired = "1";
      fxBtn.onclick = () => openEffectPicker();
    }
  }

  // ---- Timeline rendering ----
  // renderTimeline = redraw lanes AND rebuild the props panel (use on
  // selection / add / delete). renderTracks = redraw only the lanes (use during
  // slider/number edits so the props panel keeps focus).
  function renderTimeline() {
    ensureEffectsChrome();
    applyAnchors();   // keep anchored overlays/titles/music attached to Main
    renderTracks();
    renderProps();
  }

  function renderTracks() {
    ensureEffectsChrome();
    const total = totalDuration();
    const width = Math.max(300, total * PPS);

    // Ruler
    const ruler = $("tlRuler");
    if (ruler) {
      ruler.innerHTML = "";
      ruler.style.width = width + "px";
      const step = total > 120 ? 30 : total > 60 ? 15 : total > 20 ? 5 : 2;
      for (let t = 0; t <= total; t += step) {
        const tick = document.createElement("span");
        tick.className = "tl-tick";
        tick.style.left = t * PPS + "px";
        tick.textContent = fmtTime(t);
        ruler.appendChild(tick);
      }
    }

    if (!tl.track_states) {
      tl.track_states = {
        main: { mute: false, solo: false, lock: false },
        overlay: { mute: false, solo: false, lock: false },
        effects: { mute: false, solo: false, lock: false },
        text: { mute: false, solo: false, lock: false },
        music: { mute: false, solo: false, lock: false }
      };
    }
    if (!tl.track_states.effects) {
      tl.track_states.effects = { mute: false, solo: false, lock: false };
    }
    if (!tl.tracks.effects) tl.tracks.effects = [];

    TRACK_KEYS.forEach((track) => {
      const lane = document.querySelector(`.tl-track-lane[data-lane="${track}"]`);
      if (!lane) return;

      const st = tl.track_states[track] || { mute: false, solo: false, lock: false };
      const label = document.querySelector(`.tl-track[data-track="${track}"] .tl-track-label`);
      if (label) {
        let controls = label.querySelector(".tl-track-controls");
        if (!controls) {
          controls = document.createElement("div");
          controls.className = "tl-track-controls";
          controls.style.display = "flex";
          controls.style.gap = "2px";
          controls.style.marginTop = "0";
          label.appendChild(controls);
        }
        controls.innerHTML = `
          <button class="tl-chip-btn ${st.mute ? 'active' : ''}" style="padding:2px 6px; ${st.mute ? 'background:#ff4444;color:#fff;' : ''}" data-act="mute" title="Mute track">M</button>
          <button class="tl-chip-btn ${st.solo ? 'active' : ''}" style="padding:2px 6px; ${st.solo ? 'background:#fbbf24;color:#000;' : ''}" data-act="solo" title="Solo track">S</button>
          <button class="tl-chip-btn ${st.lock ? 'active' : ''}" style="padding:2px 6px; ${st.lock ? 'background:#555;color:#fff;' : ''}" data-act="lock" title="Lock track">${st.lock ? '🔒' : '🔓'}</button>
        `;
        controls.querySelectorAll("button").forEach(b => {
          b.onclick = (e) => {
            e.stopPropagation();
            const act = b.dataset.act;
            if (act === "solo") {
               const val = !st.solo;
               TRACK_KEYS.forEach(t => {
                 if (tl.track_states[t]) tl.track_states[t].solo = false;
               });
               st.solo = val;
            } else {
               st[act] = !st[act];
            }
            renderTracks();
            scheduleSave();
          };
        });
      }

      lane.innerHTML = "";
      lane.style.minWidth = width + "px";
      lane.style.pointerEvents = st.lock ? "none" : "auto";

      const anySolo = TRACK_KEYS.some(t => tl.track_states[t] && tl.track_states[t].solo);
      const isMuted = st.mute || (anySolo && !st.solo);
      lane.style.opacity = isMuted ? "0.4" : "1";

      (tl.tracks[track] || []).forEach((c, idx) => {
        const start = track === "main" ? mainStart(idx) : (c.start || 0);
        const dur = clipDuration(c);
        const el = document.createElement("div");
        el.className = `tl-clip tl-clip-${track}` +
          (selected && selected.track === track && selected.id === c.id ? " selected" : "");
        el.style.left = (LANE_OFFSET + start * PPS) + "px";
        el.style.width = Math.max(20, dur * PPS) + "px";
        el.dataset.track = track;
        el.dataset.id = c.id;

        // Filmstrip + waveform backgrounds (sliced to the clip's in/out).
        // Skip while dragging so we don't rebuild image layers ~60x/second.
        if (!drag) addClipMedia(el, track, c);

        const label = document.createElement("div");
        label.className = "tl-clip-label";
        label.textContent = clipLabel(track, c, idx);
        el.appendChild(label);
        el.title = clipHoverDetail(track, c, idx);

        // Resize handles
        ["left", "right"].forEach((side) => {
          const h = document.createElement("div");
          h.className = "tl-clip-handle " + side;
          h.dataset.side = side;
          el.appendChild(h);
        });

        lane.appendChild(el);

        // Shot boundary marker at the start of each Main shot after the first.
        if (track === "main" && idx > 0) {
          const mark = document.createElement("div");
          mark.className = "tl-shot-marker";
          mark.style.left = (LANE_OFFSET + start * PPS) + "px";
          mark.dataset.label = `S${idx + 1}`;
          mark.title = `Shot ${idx + 1} boundary`;
          lane.appendChild(mark);
        }
      });

      // Ghost pending B-roll on the Overlay lane (not in the project until Accept).
      if (track === "overlay" && pendingBroll.length) {
        pendingBroll.forEach((p) => {
          const start = p.start || 0;
          const dur = Math.max(0.4, (p.out != null ? Number(p.out) : 1.8) - (p.in || 0));
          const el = document.createElement("div");
          el.className = "tl-clip tl-clip-overlay tl-clip-pending";
          el.style.left = (LANE_OFFSET + start * PPS) + "px";
          el.style.width = Math.max(20, dur * PPS) + "px";
          el.title = `Pending B-roll: ${p.keyword || "overlay"} — Accept in Media`;
          el.addEventListener("click", (e) => {
            e.stopPropagation();
            setLeftTab("media", { pin: true });
            renderPendingBroll();
            const card = document.querySelector(`.tl-broll-card[data-pending-id="${p.id}"]`);
            if (card) {
              try { card.scrollIntoView({ behavior: "smooth", block: "nearest" }); } catch (_) {}
              card.classList.add("flash");
              setTimeout(() => card.classList.remove("flash"), 800);
            }
          });
          const label = document.createElement("div");
          label.className = "tl-clip-label";
          label.textContent = p.keyword || "B-roll";
          el.appendChild(label);
          if (p.asset_id) {
            const thumb = document.createElement("div");
            thumb.className = "tl-clip-film";
            thumb.style.backgroundImage = `url("/asset/${p.asset_id}")`;
            thumb.style.backgroundSize = "cover";
            thumb.style.opacity = "0.45";
            el.appendChild(thumb);
          }
          lane.appendChild(el);
        });
      }
    });
    updatePlayhead();
  }

  // Slice a full-source filmstrip/waveform image to the clip's [in,out] window
  // via background-size + position (see CSS comment in the render engine notes).
  function bgSlice(el, cls, url, srcMax, cIn, cDur) {
    const node = document.createElement("div");
    node.className = cls;
    node.style.backgroundImage = `url("${url}")`;
    if (srcMax && srcMax > cDur + 0.01) {
      node.style.backgroundSize = `${(srcMax / cDur) * 100}% 100%`;
      const overflow = srcMax - cDur;
      node.style.backgroundPositionX = `${(cIn / overflow) * 100}%`;
    } else {
      node.style.backgroundSize = "100% 100%";
      node.style.backgroundPositionX = "0%";
    }
    el.appendChild(node);
  }

  function addClipMedia(el, track, c) {
    const cIn = c.in || 0, cDur = clipDuration(c), srcMax = c._max || 0;
    if (track === "main" && c.source_job_id) {
      bgSlice(el, "tl-clip-film", "/filmstrip/" + c.source_job_id + ".jpg", srcMax, cIn, cDur);
      bgSlice(el, "tl-clip-wave", "/waveform/" + c.source_job_id + ".png", srcMax, cIn, cDur);
    } else if (track === "music" && c.asset_id) {
      bgSlice(el, "tl-clip-wave", "/asset-waveform/" + c.asset_id + ".png", srcMax, cIn, cDur);
    } else if (track === "overlay" && c.source_job_id) {
      bgSlice(el, "tl-clip-film", "/filmstrip/" + c.source_job_id + ".jpg", srcMax, cIn, cDur);
    } else if (track === "overlay" && c.asset_id) {
      const node = document.createElement("div");
      node.className = "tl-clip-film tl-clip-asset-thumb";
      node.style.backgroundImage = `url("/asset/${c.asset_id}?t=1")`;
      node.style.backgroundSize = "cover";
      node.style.backgroundPosition = "center";
      el.appendChild(node);
    }
  }

  function clipLabel(track, c, idx) {
    if (track === "main") {
      const s = sources.find((x) => x.job_id === c.source_job_id);
      const name = s ? (s.filename || "clip") : "clip";
      let badges = "";
      // S# matches co-editor seq (timeline order). Detect-shots shot_index is
      // separate metadata and must not override what the editor sees on the lane.
      badges += ` S${idx + 1}`;
      if (c.ken_burns && c.ken_burns.enabled) badges += " 🔍";
      if (c.punch_zoom && c.punch_zoom.enabled) badges += " ⚡";
      if (c.split && c.split.enabled) badges += " ⬓";
      if (c.cuts && c.cuts.length) badges += " ✂️";
      return `${idx + 1}. ${name.replace(/\.[^.]+$/, "")}${badges}`;
    }
    if (track === "effects") {
      const meta = EFFECT_TYPES.find((t) => t.id === c.type);
      const icon = meta ? meta.icon : "✨";
      // Zoom holds: lead with the multiplier so it reads at a glance even on
      // a narrow clip — "1.5×" / "2×" matters more here than the full label.
      let name = meta ? meta.label : (c.type || "Effect");
      if (c.type === "zoom_1_5") name = "1.5× hold";
      else if (c.type === "zoom_2x") name = "2× hold";
      return `${icon} ${name} ${fmtTime(clipDuration(c))}`;
    }
    if (track === "text") return (c.text || "Title").split("\n")[0];
    if (track === "music") {
      const a = assets.find((x) => x.asset_id === c.asset_id);
      const name = a ? (a.filename || a.ext || "music") : "music";
      return `🎵 ${String(name).replace(/\.[^.]+$/, "")} ${fmtTime(clipDuration(c))}`;
    }
    const isVid = !!(c.source_job_id || (c.asset_id && (assets.find((x) => x.asset_id === c.asset_id) || {}).kind === "video"));
    const icon = isVid ? "🎬" : "🖼";
    let ovBadge = (c.ken_burns && c.ken_burns.enabled) ? " 🔍" : "";
    if (track === "main" && c.cutaway) ovBadge = " ✂" + ovBadge;
    if (c.keyword) return `${icon} ${c.keyword}${ovBadge}`;
    if (c.asset_id) {
      const a = assets.find((x) => x.asset_id === c.asset_id);
      if (a && a.filename) return `${icon} ${String(a.filename).replace(/\.[^.]+$/, "")}${ovBadge}`;
      return `${icon} asset ${String(c.asset_id).slice(0, 6)}${ovBadge}`;
    }
    if (c.source_job_id) {
      const s = sources.find((x) => x.job_id === c.source_job_id);
      if (s && s.filename) return `${icon} ${String(s.filename).replace(/\.[^.]+$/, "")}${ovBadge}`;
    }
    return `${icon} overlay ${fmtTime(clipDuration(c))}${ovBadge}`;
  }

  /** Multi-line tooltip (native title="") with the details clipLabel has no
   * room to show — trim points, which FX are on, quote/reason, etc. */
  function clipHoverDetail(track, c, idx) {
    const lines = [];
    if (track === "main") {
      const s = sources.find((x) => x.job_id === c.source_job_id);
      lines.push(`Main clip ${idx + 1}: ${s && s.filename ? s.filename : (c.source_job_id ? "clip" : "no source")}`);
      const cin = c.in || 0, cout = c.out != null ? c.out : cin;
      lines.push(`Trim: ${fmtTime(cin)} – ${fmtTime(cout)} (source) · ${fmtTime(clipDuration(c))} on timeline`);
      const fx = [];
      if (c.punch_zoom && c.punch_zoom.enabled) fx.push(`Punch zoom (${c.punch_zoom.intensity || "med"})`);
      if (c.ken_burns && c.ken_burns.enabled) fx.push(`Ken Burns (${c.ken_burns.direction || "in"}, ${c.ken_burns.intensity || "med"})`);
      if (c.split && c.split.enabled) fx.push("Split-screen");
      if (c.cuts && c.cuts.length) fx.push(`${c.cuts.length} cut${c.cuts.length === 1 ? "" : "s"} removed`);
      const grade = ((c.color || c.color_grade || {}).preset) || "none";
      if (grade !== "none") fx.push(`Color grade: ${grade}`);
      lines.push(fx.length ? fx.join(" · ") : "No camera FX / color grade on this clip");
      lines.push(c.burn_captions === false ? "Captions: off" : "Captions: burned in");
      if (c.transition && c.transition.type) lines.push(`Transition out: ${c.transition.type}`);
      return lines.join("\n");
    }
    if (track === "effects") {
      const meta = EFFECT_TYPES.find((t) => t.id === c.type);
      lines.push(meta ? meta.label : (c.type || "Effect"));
      const bits = [];
      if (c.type === "zoom_1_5") bits.push("constant 1.5× hold");
      else if (c.type === "zoom_2x") bits.push("constant 2× hold");
      else if (c.intensity) bits.push(`intensity: ${c.intensity}`);
      if (c.type === "ken_burns" && c.direction) bits.push(`direction: ${c.direction}`);
      if (c.type === "split_screen" && c.layout) bits.push(`layout: ${c.layout}`);
      if (bits.length) lines.push(bits.join(" · "));
      lines.push(`Start ${fmtTime(c.start || 0)} · Duration ${fmtTime(clipDuration(c))}`);
      if (c.quote) lines.push(`“${c.quote}”`);
      if (c.reason) lines.push(c.reason);
      return lines.join("\n");
    }
    if (track === "overlay") {
      if (c.keyword) lines.push(`Keyword: ${c.keyword}`);
      else if (c.asset_id) {
        const a = assets.find((x) => x.asset_id === c.asset_id);
        lines.push(a && a.filename ? a.filename : "Overlay asset");
      } else if (c.source_job_id) {
        const s = sources.find((x) => x.job_id === c.source_job_id);
        lines.push(s && s.filename ? s.filename : "Overlay clip");
      } else {
        lines.push("Overlay");
      }
      lines.push(`Layout: ${c.layout || "auto"}`);
      lines.push(c.ken_burns && c.ken_burns.enabled
        ? `Ken Burns: ${c.ken_burns.direction || "in"} (${c.ken_burns.intensity || "med"})`
        : "Ken Burns: off");
      lines.push(`Opacity: ${c.opacity != null ? Math.round(Number(c.opacity) * 100) + "%" : "100%"}`);
      lines.push(`Start ${fmtTime(c.start || 0)} · Duration ${fmtTime(clipDuration(c))}`);
      return lines.join("\n");
    }
    if (track === "text") {
      lines.push((c.text || "Title").split("\n")[0]);
      lines.push(`Start ${fmtTime(c.start || 0)} · Duration ${fmtTime(clipDuration(c))}`);
      if (c.font || c.color) lines.push(`Font: ${c.font || "default"} · Color: ${c.color || "default"}`);
      return lines.join("\n");
    }
    if (track === "music") {
      const a = assets.find((x) => x.asset_id === c.asset_id);
      lines.push(`Music: ${a ? (a.filename || a.ext || "track") : "track"}`);
      lines.push(`Gain: ${c.gain_db != null ? Number(c.gain_db).toFixed(1) + " dB" : "0 dB"}`);
      lines.push(`Ducking under speech: ${c.duck === false ? "off" : "on"}`);
      lines.push(`Start ${fmtTime(c.start || 0)} · Duration ${fmtTime(clipDuration(c))}`);
      return lines.join("\n");
    }
    return "";
  }

  // ---- Selection + properties ----
  function selectClip(track, id, opts) {
    opts = opts || {};
    selected = { track, id };
    logoSelected = false;
    const c = findClip(track, id);
    // Choose what the preview shows: the clip's own video, or — for titles /
    // image overlays with no video — the first Main clip as a backdrop so you
    // can still position boxes against real framing.
    let src = null, seekTo = null;
    if (c && c.source_job_id) { src = "/raw-upload/" + c.source_job_id; seekTo = c.in || 0; }
    else if (c && c.asset_id && track !== "music") {
      const asset = assets.find((x) => x.asset_id === c.asset_id);
      if (asset && asset.kind === "video") src = "/asset/" + c.asset_id;
      else if (track !== "main") src = "/asset/" + c.asset_id;
      // Main still cutaways: keep A-roll under the compositor full-bleed image.
    }
    if (!src) {
      const fm = tl.tracks.main.find((m) => m.source_job_id);
      if (fm) src = "/raw-upload/" + fm.source_job_id;
    }
    if (src) {
      const v = $("tlPreviewVideo");
      const wrap = v.closest(".tl-preview");
      if (v.getAttribute("src") !== src) { v.src = src; previewingOutput = false; }
      wrap.classList.add("has-video");
      if (seekTo != null && !opts.preserveSeek) {
        try { v.currentTime = seekTo; } catch (e) {}
      }
    }
    // Selecting a Main clip surfaces its transcript — unless the user pinned Media.
    if (track === "main" && c && leftTabPinned !== "media") {
      setLeftTab("transcript");
      renderTranscript(c);
    } else if (track === "main" && c && leftTab === "transcript") {
      renderTranscript(c);
    }
    // Selecting overlay/music/text keeps Media open so you can see the library.
    if ((track === "overlay" || track === "music" || track === "text") && leftTabPinned !== "transcript") {
      setLeftTab("media");
    }
    applyStage();
    renderTimeline();   // renderProps() (inside) redraws the preview boxes
    if (typeof window.refreshMobileContextTools === "function") {
      try { window.refreshMobileContextTools(); } catch (e) { /* optional */ }
    }
  }

  const CANVAS_AR = { "9x16": "9 / 16", "16x9": "16 / 9", "1x1": "1 / 1", "4x5": "4 / 5" };
  function applyStage() {
    const st = $("tlStage");
    if (!st || !tl) return;
    st.style.aspectRatio = CANVAS_AR[tl.canvas] || "9 / 16";
    st.classList.toggle("fit-contain", tl.fit === "contain");
  }

  // ---- Playhead ----
  // Map the preview's current time onto the output timeline. After a full
  // render the preview IS the output (1:1). While editing a single clip, the
  // preview is that source clip, so map source-time into the clip's slot.
  function playheadOutputTime() {
    const v = $("tlPreviewVideo");
    if (!v || !tl) return null;
    const t = v.currentTime || 0;
    if (previewingOutput) return t;
    if (selected && selected.track === "main") {
      const idx = tl.tracks.main.findIndex((c) => c.id === selected.id);
      if (idx >= 0) {
        const c = tl.tracks.main[idx];
        // Still / asset Main cutaways have no A-roll source clock.
        if (c.asset_id && !c.source_job_id) return mainStart(idx);
        return mainStart(idx) + sourceTimeToLocalOutput(c, t);
      }
    }
    // Map preview source time → output even when an Overlay/Effect is selected
    // (common while scrubbing). Prefer the Main clip whose [in,out] contains t
    // so split same-source clips (IMG_0022 ×2) land on the right playhead slot.
    for (let i = 0; i < tl.tracks.main.length; i++) {
      const c = tl.tracks.main[i];
      if (!c.source_job_id) continue;
      if (v.getAttribute("src") !== "/raw-upload/" + c.source_job_id) continue;
      const cin = c.in || 0;
      const cout = c.out != null ? c.out : 1e9;
      if (t >= cin - 0.05 && t <= cout + 0.05) {
        return mainStart(i) + sourceTimeToLocalOutput(c, t);
      }
    }
    for (let i = 0; i < tl.tracks.main.length; i++) {
      const c = tl.tracks.main[i];
      if (!c.source_job_id) continue;
      if (v.getAttribute("src") === "/raw-upload/" + c.source_job_id) {
        return mainStart(i) + sourceTimeToLocalOutput(c, t);
      }
    }
    return null;
  }

  function updatePlayhead() {
    const ph = $("tlPlayhead");
    if (!ph) return;
    const ot = playheadOutputTime();
    if (ot == null) { ph.style.display = "none"; return; }
    ph.style.display = "block";
    ph.style.left = (TRACK_LABEL_W + ot * PPS) + "px";
    const lab = $("tlPlayheadTime");
    if (lab) lab.textContent = fmtTime(ot);
  }

  /** Convert a pointer X inside #tlTimeline into output timeline seconds. */
  function outputTimeFromClientX(clientX) {
    const root = $("tlTimeline");
    if (!root) return 0;
    const rect = root.getBoundingClientRect();
    const pad = 8; // .tl-timeline padding
    const x = clientX - rect.left + (root.scrollLeft || 0) - pad;
    return Math.max(0, (x - TRACK_LABEL_W) / PPS);
  }

  /**
   * Seek the preview + playhead to an output-timeline time.
   * Loads the covering Main clip when not viewing a full render.
   */
  function seekToOutputTime(ot, opts) {
    opts = opts || {};
    if (!tl) return;
    ot = Math.max(0, Number(ot) || 0);
    const v = $("tlPreviewVideo");
    if (!v) return;

    let idx = -1;
    for (let i = 0; i < tl.tracks.main.length; i++) {
      const s = mainStart(i);
      const e = s + clipDuration(tl.tracks.main[i]);
      if (ot >= s - 0.0001 && ot < e + 0.0001) { idx = i; break; }
    }
    if (idx < 0 && tl.tracks.main.length) {
      idx = tl.tracks.main.length - 1;
      const end = mainStart(idx) + clipDuration(tl.tracks.main[idx]);
      ot = Math.max(mainStart(idx), end - 0.05);
    }

    if (previewingOutput && !opts.forceSource) {
      try { v.currentTime = ot; } catch (e) { /* ignore */ }
      updatePlayhead();
      updateStageCompositor();
      if (idx >= 0 && leftTab === "transcript") {
        const c = tl.tracks.main[idx];
        const local = ot - mainStart(idx);
        highlightTranscriptAt(localOutputToSourceTime(c, local));
      }
      return;
    }

    if (idx < 0) {
      updatePlayhead();
      return;
    }

    const c = tl.tracks.main[idx];
    const local = ot - mainStart(idx);
    const srcT = localOutputToSourceTime(c, local);
    if (!opts.keepSelection) {
      selected = { track: "main", id: c.id };
      logoSelected = false;
    }

    const finish = () => {
      try { v.currentTime = srcT; } catch (e) { /* ignore */ }
      if (leftTab === "transcript") {
        const trClip = (opts.keepSelection && selected && selected.track === "main")
          ? findClip("main", selected.id)
          : c;
        if (trClip && trClip.source_job_id && trClip.source_job_id === c.source_job_id) {
          if (!transcriptWords) renderTranscript(trClip);
          else highlightTranscriptAt(srcT);
        }
      }
      updatePlayhead();
      updateStageCompositor();
      renderTracks();
      if (!opts.quietProps && !opts.keepSelection) renderProps();
    };

    if (c.source_job_id) {
      const src = "/raw-upload/" + c.source_job_id;
      if (v.getAttribute("src") !== src) {
        v.src = src;
        previewingOutput = false;
        const wrap = v.closest(".tl-preview");
        if (wrap) wrap.classList.add("has-video");
        v.addEventListener("loadedmetadata", finish, { once: true });
        try { v.load(); } catch (e) { finish(); }
        return;
      }
    } else if (c.asset_id) {
      // Still cutaways: keep last A-roll frame under a full-bleed compositor image.
      // Video assets: load into the preview element.
      const asset = assets.find((x) => x.asset_id === c.asset_id);
      if (asset && asset.kind === "video") {
        const src = "/asset/" + c.asset_id;
        if (v.getAttribute("src") !== src) {
          v.src = src;
          previewingOutput = false;
          const wrap = v.closest(".tl-preview");
          if (wrap) wrap.classList.add("has-video");
          v.addEventListener("loadedmetadata", finish, { once: true });
          try { v.load(); } catch (e) { finish(); }
          return;
        }
      }
      previewingOutput = false;
      finish();
      return;
    }
    previewingOutput = false;
    finish();
  }

  function wireTimelineSeek() {
    const root = $("tlTimeline");
    const ruler = $("tlRuler");
    if (!root || root._seekWired) return;
    root._seekWired = true;

    const onSeekPointer = (e) => {
      if (e.button != null && e.button !== 0) return;
      // Don't steal clip drag / resize handles.
      if (e.target.closest && (
        e.target.closest(".tl-clip-handle") ||
        e.target.closest(".tl-track-controls") ||
        e.target.closest("button")
      )) return;
      // Clicking a real clip still selects+drags via onTimelineMouseDown —
      // but also seek to that X so the cursor jumps under the click.
      const ot = outputTimeFromClientX(e.clientX);
      // Empty lane / ruler / pending ghost: seek only (no drag).
      const onClip = e.target.closest && e.target.closest(".tl-clip") && !e.target.closest(".tl-clip-pending");
      if (!onClip || e.target.closest(".tl-ruler") || e.target === root || e.target.classList.contains("tl-track-lane")) {
        seekToOutputTime(ot, { quietProps: !!onClip });
      } else if (onClip) {
        // Selecting a clip already jumps preview to clip.in — override to click time.
        seekToOutputTime(ot, { quietProps: true });
      }
    };

    if (ruler) {
      ruler.style.cursor = "ew-resize";
      ruler.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        e.stopPropagation();
        seekToOutputTime(outputTimeFromClientX(e.clientX));
        const move = (ev) => seekToOutputTime(outputTimeFromClientX(ev.clientX), { quietProps: true });
        const up = () => {
          document.removeEventListener("pointermove", move);
          document.removeEventListener("pointerup", up);
          renderProps();
        };
        document.addEventListener("pointermove", move);
        document.addEventListener("pointerup", up);
      });
    }

    // Empty areas of lanes / timeline chrome (not starting a clip drag).
    root.addEventListener("pointerdown", (e) => {
      if (e.target.closest && e.target.closest(".tl-clip") && !e.target.closest(".tl-clip-pending")) return;
      if (e.target.closest && e.target.closest(".tl-ruler")) return;
      if (e.target.closest && e.target.closest(".tl-track-controls")) return;
      onSeekPointer(e);
    });
  }

  // ---- Split the selected Main clip at the playhead ----
  function splitAtPlayhead() {
    const cur = ($("tlPreviewVideo").currentTime) || 0;
    let idx, t;
    if (previewingOutput) {
      // Preview is the full render: map output time -> which Main clip + source time.
      idx = -1;
      for (let i = 0; i < tl.tracks.main.length; i++) {
        const s = mainStart(i), e = s + clipDuration(tl.tracks.main[i]);
        if (cur >= s && cur < e) { idx = i; t = (tl.tracks.main[i].in || 0) + (cur - s); break; }
      }
      if (idx < 0) { alert("Move the playhead over a Main clip first."); return; }
      selectClip("main", tl.tracks.main[idx].id);
    } else {
      if (!selected || selected.track !== "main") { alert("Select a Main clip first."); return; }
      idx = tl.tracks.main.findIndex((c) => c.id === selected.id);
      if (idx < 0) return;
      t = cur;  // source time of the selected clip's preview
    }
    const c = tl.tracks.main[idx];
    if (t <= (c.in || 0) + 0.1 || t >= (c.out || 0) - 0.1) {
      alert("Move the playhead to somewhere inside the clip first.");
      return;
    }
    pushHistory();
    const second = JSON.parse(JSON.stringify(c));
    second.id = uid();
    c.out = t;
    second.in = t;
    second.transition = null;  // internal split is a hard cut
    // Split cuts between the two halves by source time.
    c.cuts = (c.cuts || []).filter((r) => r[0] < t);
    second.cuts = (second.cuts || []).filter((r) => r[1] > t);
    tl.tracks.main.splice(idx + 1, 0, second);
    renderTimeline();
    scheduleSave();
  }

  function setZoom(delta) {
    PPS = Math.max(4, Math.min(60, PPS + delta));
    renderTracks();
    updatePlayhead();
  }

  function titleDuration(c) {
    // Titles store duration in `out` (in is unused / 0).
    if (c.in == null || c.in === 0) return Math.max(0.2, c.out || 4);
    return clipDuration(c);
  }

  // ---- Magnetic snap ----
  function collectSnapTimes(exclude) {
    const times = [0];
    if (!tl) return times;
    (tl.tracks.main || []).forEach((c, i) => {
      if (exclude && exclude.track === "main" && exclude.id === c.id) return;
      const s = mainStart(i);
      times.push(s, s + clipDuration(c));
    });
    ["overlay", "effects", "text", "music"].forEach((k) => {
      (tl.tracks[k] || []).forEach((c) => {
        if (exclude && exclude.track === k && exclude.id === c.id) return;
        const s = c.start || 0;
        const d = k === "text" ? titleDuration(c) : clipDuration(c);
        times.push(s, s + d);
      });
    });
    const ot = playheadOutputTime();
    if (ot != null) times.push(ot);
    return times;
  }

  function snapTime(t, exclude) {
    if (!magnetic || !tl) return t;
    const thresh = SNAP_PX / PPS;
    let best = t, bestD = thresh;
    collectSnapTimes(exclude).forEach((s) => {
      const d = Math.abs(s - t);
      if (d <= bestD) { bestD = d; best = s; }
    });
    return Math.max(0, best);
  }

  // ---- Preview cut (Main keep-ranges + grade/music approx, no full Render) ----
  function keepRangesForClip(clip, opts) {
    opts = opts || {};
    const cin = clip.in || 0;
    const cout = Math.max(cin + 0.05, clip.out || cin + 0.05);
    const cuts = (clip.cuts || [])
      .map(([a, b]) => [Math.max(cin, Number(a)), Math.min(cout, Number(b))])
      .filter(([a, b]) => b - a > 0.02)
      .sort((a, b) => a[0] - b[0]);
    const ranges = [];
    let cursor = cin;
    cuts.forEach(([a, b]) => {
      if (a > cursor + 0.02) ranges.push([cursor, a]);
      cursor = Math.max(cursor, b);
    });
    if (cout > cursor + 0.02) ranges.push([cursor, cout]);
    if (ranges.length) return ranges;
    return opts.allowEmpty ? [] : [[cin, cout]];
  }

  function cssFilterForColor(color) {
    if (!color) return "";
    const parts = [];
    switch (color.preset || "none") {
      case "neutral": parts.push("contrast(1.04)", "saturate(1.06)"); break;
      case "warm": parts.push("sepia(0.2)", "saturate(1.18)", "hue-rotate(-10deg)"); break;
      case "cool": parts.push("saturate(1.06)", "hue-rotate(14deg)", "contrast(1.03)"); break;
      case "vivid": parts.push("contrast(1.12)", "saturate(1.32)"); break;
      case "bw": parts.push("grayscale(1)", "contrast(1.05)"); break;
      default: break;
    }
    const b = Number(color.brightness || 0);
    const c = color.contrast != null ? Number(color.contrast) : 1;
    const s = color.saturation != null ? Number(color.saturation) : 1;
    if (Math.abs(b) > 0.001) parts.push(`brightness(${Math.max(0.2, Math.min(1.8, 1 + b))})`);
    if (Math.abs(c - 1) > 0.001) parts.push(`contrast(${Math.max(0.2, Math.min(2.5, c))})`);
    if (Math.abs(s - 1) > 0.001) parts.push(`saturate(${Math.max(0, Math.min(2.5, s))})`);
    return parts.join(" ");
  }

  function applyLiveGrade(clip, ot) {
    const v = $("tlPreviewVideo");
    if (!v) return;
    const laneColor = activeEffectOfType(ot != null ? ot : playheadOutputTime(), "color");
    let color = null;
    if (laneColor) {
      color = {
        preset: laneColor.preset || "none",
        brightness: laneColor.brightness,
        contrast: laneColor.contrast,
        saturation: laneColor.saturation,
      };
    } else {
      color = clip && (clip.color || clip.color_grade);
    }
    v.style.filter = cssFilterForColor(color);
  }

  function activeEffectsAt(ot) {
    if (!tl || ot == null) return [];
    return (tl.tracks.effects || []).filter((c) => {
      const s = c.start || 0;
      return ot >= s && ot < s + clipDuration(c);
    });
  }

  function activeEffectOfType(ot, type) {
    const list = activeEffectsAt(ot);
    for (let i = list.length - 1; i >= 0; i--) {
      if (list[i].type === type) return list[i];
    }
    return null;
  }

  function stopMusicPreview() {
    musicPlayers.forEach((p) => {
      try { p.audio.pause(); } catch (e) {}
    });
    musicPlayers = [];
  }

  function dbToLinear(db) {
    return Math.max(0, Math.min(1, Math.pow(10, (Number(db) || 0) / 20)));
  }

  function syncMusicAt(ot) {
    if (!tl) return;
    const active = new Set();
    (tl.tracks.music || []).forEach((c) => {
      if (!c.asset_id) return;
      const start = c.start || 0;
      const end = start + clipDuration(c);
      if (ot < start || ot >= end) return;
      active.add(c.id);
      let player = musicPlayers.find((p) => p.id === c.id);
      if (!player) {
        const audio = new Audio("/asset/" + c.asset_id);
        audio.volume = dbToLinear((c.gain_db != null ? c.gain_db : -18) + (c.duck ? -8 : 0));
        player = { id: c.id, audio };
        musicPlayers.push(player);
      }
      const srcT = (c.in || 0) + (ot - start);
      if (Math.abs((player.audio.currentTime || 0) - srcT) > 0.4) {
        try { player.audio.currentTime = Math.max(0, srcT); } catch (e) {}
      }
      if (player.audio.paused) player.audio.play().catch(() => {});
    });
    musicPlayers.forEach((p) => {
      if (!active.has(p.id) && !p.audio.paused) {
        try { p.audio.pause(); } catch (e) {}
      }
    });
  }

  async function playSequencePreview() {
    if (seqPreview && seqPreview.running) {
      seqPreview.cancel();
      return;
    }
    if (!tl || !tl.tracks.main.length) {
      alert("Add at least one clip to the Main track first.");
      return;
    }
    const v = $("tlPreviewVideo");
    const btn = $("tlPlaySeqBtn");
    if (!v) return;
    let cancelled = false;
    seqPreview = {
      running: true,
      cancel: () => { cancelled = true; try { v.pause(); } catch (e) {} stopMusicPreview(); },
    };
    if (btn) btn.textContent = "⏹ Stop";
    setRenderStatus("Previewing cut (keep-ranges + grades/music)…");
    previewingOutput = false;
    v.closest(".tl-preview").classList.add("has-video");

    const waitEvent = (el, ev, timeoutMs) => new Promise((resolve) => {
      let done = false;
      const finish = () => { if (done) return; done = true; el.removeEventListener(ev, finish); resolve(); };
      el.addEventListener(ev, finish);
      if (timeoutMs) setTimeout(finish, timeoutMs);
    });

    try {
      for (let i = 0; i < tl.tracks.main.length; i++) {
        if (cancelled) break;
        const c = tl.tracks.main[i];
        selected = { track: "main", id: c.id };
        // Still / asset cutaways: hold on the compositor for their duration.
        if (c.asset_id && !c.source_job_id) {
          const hold = clipDuration(c);
          const baseOut = mainStart(i);
          const asset = assets.find((x) => x.asset_id === c.asset_id);
          if (asset && asset.kind === "video") {
            const src = "/asset/" + c.asset_id;
            if (v.getAttribute("src") !== src) {
              v.src = src;
              await waitEvent(v, "loadedmetadata", 8000);
            }
            try { v.currentTime = c.in || 0; } catch (e) {}
            v.play().catch(() => {});
            await new Promise((resolve) => {
              const t0 = performance.now();
              const tick = () => {
                const elapsed = (performance.now() - t0) / 1000;
                const ot = baseOut + Math.min(hold, elapsed);
                const ph = $("tlPlayhead");
                if (ph) { ph.style.display = "block"; ph.style.left = (TRACK_LABEL_W + ot * PPS) + "px"; }
                syncMusicAt(ot);
                updateStageCompositor();
                if (cancelled || elapsed >= hold - 0.03) {
                  try { v.pause(); } catch (e) {}
                  resolve();
                  return;
                }
                requestAnimationFrame(tick);
              };
              requestAnimationFrame(tick);
            });
          } else {
            await new Promise((resolve) => {
              const t0 = performance.now();
              const tick = () => {
                const elapsed = (performance.now() - t0) / 1000;
                const ot = baseOut + Math.min(hold, elapsed);
                const ph = $("tlPlayhead");
                if (ph) { ph.style.display = "block"; ph.style.left = (TRACK_LABEL_W + ot * PPS) + "px"; }
                syncMusicAt(ot);
                updateStageCompositor();
                if (cancelled || elapsed >= hold - 0.03) {
                  resolve();
                  return;
                }
                requestAnimationFrame(tick);
              };
              updateStageCompositor();
              requestAnimationFrame(tick);
            });
          }
          continue;
        }
        if (!c.source_job_id) continue;
        if (leftTabPinned !== "media") setLeftTab("transcript");
        renderTranscript(c);
        applyLiveGrade(c, mainStart(i));
        const src = "/raw-upload/" + c.source_job_id;
        if (v.getAttribute("src") !== src) {
          v.src = src;
          await waitEvent(v, "loadedmetadata", 8000);
        }
        // Fade in after soft dissolve from previous clip.
        v.style.opacity = "0";
        const fadeInSteps = 5;
        for (let s = 1; s <= fadeInSteps; s++) {
          if (cancelled) break;
          v.style.opacity = String(s / fadeInSteps);
          await new Promise((r) => setTimeout(r, 30));
        }
        v.style.opacity = "1";
        if (cancelled) break;
        const ranges = keepRangesForClip(c);
        const baseOut = mainStart(i);
        let played = 0;
        for (let r = 0; r < ranges.length; r++) {
          if (cancelled) break;
          const [start, end] = ranges[r];
          try { v.currentTime = start; } catch (e) {}
          await waitEvent(v, "seeked", 2000);
          if (cancelled) break;
          v.play().catch(() => {});
          await new Promise((resolve) => {
            const onTime = () => {
              const srcT = v.currentTime || 0;
              const ot = baseOut + played + Math.max(0, srcT - start);
              const ph = $("tlPlayhead");
              if (ph) { ph.style.display = "block"; ph.style.left = (TRACK_LABEL_W + ot * PPS) + "px"; }
              const lab = $("tlPlayheadTime");
              if (lab) lab.textContent = fmtTime(ot);
              highlightTranscriptAt(srcT);
              syncMusicAt(ot);
              updateLiveCaptions(ot);
              if (typeof updateStageCompositor === "function") updateStageCompositor();
              if (cancelled || v.ended || srcT >= end - 0.03) {
                v.removeEventListener("timeupdate", onTime);
                try { v.pause(); } catch (e) {}
                played += Math.max(0, end - start);
                resolve();
              }
            };
            v.addEventListener("timeupdate", onTime);
          });
        }
        // Soft opacity dissolve into the next Main clip (approx — not ffmpeg xfade).
        // Longer (~0.45s) when the outgoing clip has a fade-style transition.
        if (!cancelled && i < tl.tracks.main.length - 1) {
          const tr = (c.transition && c.transition.type) || c.transition || "";
          const soft = /fade|dissolve/i.test(String(tr));
          // Hard cuts (transition null/"cut"): keep the preview dissolve
          // near-instant so it reads as a cut, not a mystery fade.
          const steps = soft ? 12 : 2;
          const stepMs = soft ? 40 : 20;
          for (let s = 1; s <= steps; s++) {
            if (cancelled) break;
            v.style.opacity = String(1 - s / steps);
            await new Promise((r) => setTimeout(r, stepMs));
          }
          v.style.opacity = "0";
        }
      }
    } finally {
      stopMusicPreview();
      if (v) {
        v.style.filter = "";
        v.style.opacity = "1";
      }
      seqPreview = null;
      if (btn) btn.textContent = "▶ Preview cut";
      setRenderStatus(cancelled ? "Preview stopped" : "Preview done — Render for exact transitions / captions");
      renderTimeline();
    }
  }

  // ---- Preview animation parity ----------------------------------------
  // The renderer animates titles with ASS tags (app.py _tl_build_titles_ass):
  // \fad(300,300) and, for slideup, \move(x, y+60, x, y, 0, 400). Mirror those
  // exact numbers here so what plays in the preview is what gets burned in.
  const FADE_MS = 300;
  const SLIDE_MS = 400;
  const SLIDE_PX = 60;   // in *output* pixels, as in the \move tag
  const CANVAS_DIMS = {  // mirrors TIMELINE_CANVASES in app.py
    "9x16": [1080, 1920], "16x9": [1920, 1080],
    "1x1": [1080, 1080], "4x5": [1080, 1350],
  };

  // Titles currently on screen, so the rAF loop can animate them in place
  // instead of rebuilding the layer (rebuilding would restart overlay <video>
  // playback and re-request every <img> each frame).
  let animEntries = [];
  let ovEntries = [];
  let visSig = null;
  let rafId = null;
  let liveCaptionEl = null;

  // Keep B-roll <video> overlays running with the main preview. Only correct
  // the time on real drift — reassigning currentTime every frame re-seeks the
  // decoder and makes the overlay stutter.
  function syncOverlayVideos(ot, playing) {
    for (const o of ovEntries) {
      if (!o.isVideo) continue;
      const want = o.srcIn + Math.max(0, ot - o.start);
      if (Math.abs((o.el.currentTime || 0) - want) > 0.30) {
        try { o.el.currentTime = want; } catch (e) {}
      }
      if (playing && o.el.paused) { o.el.play().catch(() => {}); }
      else if (!playing && !o.el.paused) { o.el.pause(); }
    }
  }

  function syncOverlayKenBurns(ot) {
    for (const o of ovEntries) {
      if (!o.el) continue;
      const tRel = Math.max(0, ot - o.start);
      const scale = kenBurnsScaleAt(o.ken_burns, tRel, o.dur || 1);
      o.el.style.transformOrigin = "50% 50%";
      if (Math.abs(scale - 1) > 0.001) {
        o.el.style.transform = `scale(${scale.toFixed(4)})`;
      } else {
        o.el.style.transform = "";
      }
    }
  }

  // Mirrors _PUNCH_PEAK / PUNCH_DECAY_SECONDS and the zoompan curve in app.py.
  // The renderer snaps to the peak on the hit and eases out on a cubic, so the
  // preview has to use the same numbers or the move previews wrong.
  const PUNCH_PEAK = {
    low: 1.15, med: 1.25, high: 1.40, strong: 1.40,
    "1.5x": 1.5, "2x": 2.0, hold_1_5: 1.5, hold_2: 2.0,
  };
  const PUNCH_DECAY = 0.45;
  // "Hold" intensities snap to their target scale at the hit and stay there
  // for the whole duration instead of easing back to 1.0 — see
  // _PUNCH_HOLD_INTENSITIES in app.py.
  const PUNCH_HOLD_INTENSITIES = new Set(["1.5x", "2x", "hold_1_5", "hold_2"]);
  // Effects-lane fx types that behave like punch_zoom for preview purposes,
  // mapped to the intensity they should render at.
  const ZOOM_HOLD_LANE_TYPES = { zoom_1_5: "1.5x", zoom_2x: "2x" };

  function punchScaleAt(cfg, tRel) {
    const peak = PUNCH_PEAK[cfg.intensity] || PUNCH_PEAK.med;
    const amp = peak - 1;
    if (amp <= 0) return 1;
    const hit = Math.max(0, Number(cfg.hit) || 0);
    if (tRel < hit) return 1;
    if (PUNCH_HOLD_INTENSITIES.has(cfg.intensity)) return peak;
    const decay = Math.max(0.05, Number(cfg.decay) || PUNCH_DECAY);
    const u = Math.min(1, Math.max(0, (tRel - hit) / decay));
    return 1 + amp * Math.pow(1 - u, 3);
  }

  // Effects-lane lookup that also matches zoom_1_5 / zoom_2x "hold" clips,
  // returning a punch_zoom-shaped object (with `.intensity` normalised) so
  // callers can treat them exactly like a punch_zoom lane effect.
  function activePunchLikeEffect(ot) {
    const list = activeEffectsAt(ot);
    for (let i = list.length - 1; i >= 0; i--) {
      const fx = list[i];
      if (fx.type === "punch_zoom") return fx;
      const heldIntensity = ZOOM_HOLD_LANE_TYPES[fx.type];
      if (heldIntensity) return Object.assign({}, fx, { intensity: heldIntensity });
    }
    return null;
  }

  // Mirrors _KENBURNS_INTENSITY in app.py — slow zoom ramp over the clip.
  const KENBURNS_AMOUNT = { low: 0.12, med: 0.22, high: 0.35 };

  function kenBurnsScaleAt(kb, tRel, dur) {
    if (!kb || !kb.enabled) return 1;
    const amount = KENBURNS_AMOUNT[kb.intensity] || KENBURNS_AMOUNT.med;
    const u = Math.min(1, Math.max(0, tRel / Math.max(0.05, dur)));
    if ((kb.direction || "in") === "out") return (1 + amount) - amount * u;
    return 1 + amount * u;
  }

  // Runs every frame so punch + Ken Burns animate; compose both scales.
  // Effects-lane clips override Main-clip props when active at the playhead.
  function applyPunchZoom(v, ot) {
    if (!v || !tl) return;
    const lanePunch = activePunchLikeEffect(ot);
    const laneKen = activeEffectOfType(ot, "ken_burns");
    for (let i = 0; i < tl.tracks.main.length; i++) {
      const c = tl.tracks.main[i];
      const start = mainStart(i);
      const dur = clipDuration(c);
      if (ot < start || ot >= start + dur) continue;
      const tRel = ot - start;
      let scale = 1;
      if (laneKen) {
        scale = kenBurnsScaleAt(
          { enabled: true, intensity: laneKen.intensity || "med", direction: laneKen.direction || "in" },
          Math.max(0, ot - (laneKen.start || 0)),
          clipDuration(laneKen)
        );
      } else {
        scale = kenBurnsScaleAt(c.ken_burns, tRel, dur);
      }
      let pz = null;
      let punchRel = tRel;
      if (lanePunch) {
        pz = {
          enabled: true,
          intensity: lanePunch.intensity || "med",
          hit: lanePunch.hit != null ? lanePunch.hit : 0,
          decay: lanePunch.decay != null ? lanePunch.decay : PUNCH_DECAY,
          anchor: lanePunch.anchor || null,
        };
        punchRel = Math.max(0, ot - (lanePunch.start || 0));
      } else if (c.punch_zoom && c.punch_zoom.enabled) {
        pz = c.punch_zoom;
      }
      if (pz && pz.enabled) scale *= punchScaleAt(pz, punchRel);
      const a = (pz && pz.anchor) || {};
      const ax = Math.min(1, Math.max(0, Number(a.x != null ? a.x : 0.5)));
      const ay = Math.min(1, Math.max(0, Number(a.y != null ? a.y : 0.5)));
      v.style.transformOrigin = `${(ax * 100).toFixed(2)}% ${(ay * 100).toFixed(2)}%`;
      if (Math.abs(scale - 1) > 0.001) {
        v.style.transform = `scale(${scale.toFixed(4)})`;
      } else {
        v.style.transform = "";
        v.style.transformOrigin = "";
      }
      return;
    }
    v.style.transform = "";
    v.style.transformOrigin = "";
  }

  function outputHeight() {
    const d = CANVAS_DIMS[(tl && tl.canvas) || "9x16"] || CANVAS_DIMS["9x16"];
    return d[1];
  }

  // Which items are on screen at `ot`. When this changes the layer needs a
  // structural rebuild; while it holds steady we only touch styles.
  function visibleSignature(ot) {
    if (!tl) return "";
    const ids = [];
    const scan = (arr) => (arr || []).forEach((it) => {
      const s = it.start || 0;
      if (ot >= s && ot <= s + clipDuration(it)) ids.push(it.id);
    });
    scan(tl.tracks.text);
    scan(tl.tracks.overlay);
    scan(tl.tracks.effects);
    let mainId = "";
    for (let i = 0; i < tl.tracks.main.length; i++) {
      const s = mainStart(i);
      if (ot >= s && ot < s + clipDuration(tl.tracks.main[i])) {
        mainId = tl.tracks.main[i].id; break;
      }
    }
    return ids.join(",") + "|" + mainId + "|" + (selected ? selected.id : "");
  }

  function applyTitleAnim(entry, ot) {
    const t = ot - entry.start;                  // seconds since it appeared
    const remain = (entry.start + entry.dur) - ot;
    let opacity = 1, dy = 0;

    if (entry.anim !== "none") {
      const fade = FADE_MS / 1000;
      if (t < fade) opacity = t / fade;
      else if (remain < fade) opacity = remain / fade;
      opacity = Math.max(0, Math.min(1, opacity));
    }
    if (entry.anim === "slideup") {
      const slide = SLIDE_MS / 1000;
      if (t < slide) dy = SLIDE_PX * (1 - Math.max(0, Math.min(1, t / slide)));
    }

    entry.el.style.opacity = opacity;
    // Scale the output-space slide into stage pixels so it reads the same at
    // any preview size.
    const stagePx = dy * ((entry.el.parentNode ? entry.el.parentNode.clientHeight : 0) || 640) / outputHeight();
    entry.el.style.transform = assAnchorTransform(entry.align) +
      (stagePx ? ` translateY(${stagePx.toFixed(2)}px)` : "");
  }

  // Drive the preview from requestAnimationFrame while the video plays.
  // `timeupdate` only fires ~4x/second, which is why placed elements used to
  // lag and stutter behind the picture. Editing stays live: dragging updates
  // the model and the next frame reflects it, with nothing paused.
  function activeMainAt(ot) {
    if (!tl) return null;
    for (let i = 0; i < tl.tracks.main.length; i++) {
      const start = mainStart(i);
      const dur = clipDuration(tl.tracks.main[i]);
      if (ot >= start && ot < start + dur) return tl.tracks.main[i];
    }
    return null;
  }

  function previewFrame() {
    const v = $("tlPreviewVideo");
    if (!v || !tl) { rafId = null; return; }

    let ot = playheadOutputTime();
    if (ot == null) ot = v.currentTime || 0;

    const sig = visibleSignature(ot);
    if (sig !== visSig) {
      updateStageCompositor();          // items entered/left: rebuild
    } else {
      for (const e of animEntries) applyTitleAnim(e, ot);
      // Caption text changes every word — refresh karaoke layer cheaply.
      updateLiveCaptions(ot);
    }
    const active = activeMainAt(ot);
    if (!(seqPreview && seqPreview.running)) applyLiveGrade(active, ot);
    applyPunchZoom(v, ot);   // must run every frame, not only on rebuild
    syncOverlayVideos(ot, !v.paused && !v.ended);
    syncOverlayKenBurns(ot);
    updatePlayhead();
    if (leftTab === "transcript" && transcriptWords) highlightTranscriptAt(v.currentTime);
    if (!v.paused && !v.ended) maybeAdvancePastClipEnd();

    rafId = (!v.paused && !v.ended) ? requestAnimationFrame(previewFrame) : null;
  }

  /**
   * Space/native play scrubs the raw <video> source, which has no concept of
   * clip boundaries — left alone it keeps playing straight through clip.out
   * into whatever footage follows in that same source file. Watch every
   * frame while playing and, once we cross the current Main clip's out point,
   * hop to the next Main clip (or stop) instead of leaking into raw source.
   */
  function maybeAdvancePastClipEnd() {
    if (!tl || previewingOutput || (seqPreview && seqPreview.running) || clipAdvanceLock) return;
    const v = $("tlPreviewVideo");
    if (!v || v.paused || v.ended) return;

    const src = v.getAttribute("src") || "";
    const t = v.currentTime || 0;
    let idx = -1;
    for (let i = 0; i < tl.tracks.main.length; i++) {
      const c = tl.tracks.main[i];
      if (!c.source_job_id) continue;
      if (src !== "/raw-upload/" + c.source_job_id) continue;
      const cin = c.in || 0;
      const cout = c.out != null ? c.out : cin + 1e9;
      if (t >= cin - 0.15 && t <= cout + 0.15) { idx = i; break; }
    }
    if (idx < 0 && selected && selected.track === "main") {
      idx = tl.tracks.main.findIndex((c) => c.id === selected.id);
    }
    if (idx < 0) return;

    const c = tl.tracks.main[idx];
    if (!c || !c.source_job_id) return;
    const ranges = keepRangesForClip(c, { allowEmpty: true });
    const end = ranges.length ? ranges[ranges.length - 1][1] : (c.out != null ? c.out : t);
    if (t < end - 0.04) return;

    const nextIdx = idx + 1;
    const nextClip = tl.tracks.main[nextIdx];
    if (nextClip) {
      clipAdvanceLock = true;
      try { v.pause(); } catch (e) { /* ignore */ }
      selectClip("main", nextClip.id);
      seekToOutputTime(mainStart(nextIdx) + 0.01);
      const resumePlay = () => {
        v.play().catch(() => {});
        startPreviewLoop();
        clipAdvanceLock = false;
      };
      // Same-source clips seek synchronously (readyState unchanged); a
      // different source just called v.load(), which resets readyState to 0
      // — wait for it so play() doesn't briefly start from t=0.
      if (v.readyState >= 1) requestAnimationFrame(resumePlay);
      else v.addEventListener("loadedmetadata", resumePlay, { once: true });
    } else {
      try { v.pause(); } catch (e) { /* ignore */ }
    }
  }

  function startPreviewLoop() {
    if (rafId == null) rafId = requestAnimationFrame(previewFrame);
  }
  function stopPreviewLoop() {
    if (rafId != null) { cancelAnimationFrame(rafId); rafId = null; }
  }

  // ---- Live preview boxes (drag to position titles / overlays / logo) ----
  function updateStageCompositor() {
    const layer = $("tlOverlayLayer");
    const v = $("tlPreviewVideo");
    if (!layer || !tl || !v) return;

    if (boxDrag) return;

    layer.innerHTML = "";
    animEntries = [];
    ovEntries = [];

    let ot = playheadOutputTime();
    if (ot == null) ot = v.currentTime || 0;
    visSig = visibleSignature(ot);

    const activeMainClip = activeMainAt(ot);
    if (!(seqPreview && seqPreview.running)) applyLiveGrade(activeMainClip, ot);

    applyPunchZoom(v, ot);

    const stHeight = layer.clientHeight || 640;

    // Full-bleed Main cutaway (still / photo) sits under overlays.
    if (activeMainClip && activeMainClip.asset_id && !activeMainClip.source_job_id) {
      const asset = assets.find((x) => x.asset_id === activeMainClip.asset_id);
      if (!asset || asset.kind !== "video") {
        const cut = document.createElement("img");
        cut.className = "tl-main-cutaway";
        cut.src = "/asset/" + activeMainClip.asset_id;
        cut.alt = activeMainClip.keyword || "Cutaway";
        cut.style.cssText = "position:absolute;inset:0;width:100%;height:100%;object-fit:cover;pointer-events:none;z-index:1";
        layer.appendChild(cut);
      }
    }

    tl.tracks.text.forEach(item => {
      const start = item.start || 0;
      const dur = clipDuration(item);
      if (ot >= start && ot <= start + dur) {
        const el = document.createElement("div");
        el.style.position = "absolute";
        el.style.left = (item.x != null ? item.x : 0.5) * 100 + "%";
        el.style.top = (item.y != null ? item.y : 0.85) * 100 + "%";
        el.style.transform = assAnchorTransform(item.align);
        el.style.textAlign = item.align === 1 ? "left" : (item.align === 3 ? "right" : "center");
        el.style.color = item.color || "#FFFFFF";
        el.style.fontFamily = item.font || "Anton";
        el.style.fontWeight = item.bold ? "bold" : "normal";
        el.style.fontSize = ((item.size || 56) / 1000 * stHeight) + "px";
        el.style.whiteSpace = "pre-wrap";
        el.style.pointerEvents = "none";
        
        if (item.bg_enabled) {
           const bg = document.createElement("div");
           bg.style.position = "absolute";
           bg.style.inset = "0";
           bg.style.backgroundColor = item.bg_color || "#000000";
           bg.style.opacity = item.bg_opacity != null ? item.bg_opacity : 0.55;
           bg.style.zIndex = "-1";
           el.appendChild(bg);
           
           const txt = document.createElement("div");
           txt.innerText = item.text || "Title";
           txt.style.padding = "0.2em 0.4em";
           el.appendChild(txt);
        } else {
           el.innerText = item.text || "Title";
        }

        layer.appendChild(el);
        // Register for the rAF loop and apply this frame's state immediately,
        // so a title that is mid-fade doesn't flash at full opacity on rebuild.
        const entry = { el, start, dur, anim: item.anim || "fade", align: item.align };
        animEntries.push(entry);
        applyTitleAnim(entry, ot);
      }
    });

    tl.tracks.overlay.forEach(item => {
      const start = item.start || 0;
      const dur = clipDuration(item);
      if (ot >= start && ot <= start + dur) {
        const wrap = document.createElement("div");
        let media;
        const asset = item.asset_id
          ? (assets.find((x) => x.asset_id === item.asset_id)
            || (window.ASSETS || []).find((x) => x.id === item.asset_id || x.asset_id === item.asset_id))
          : null;
        const assetIsVideo = !!(asset && asset.kind === "video");
        if (item.source_job_id || assetIsVideo) {
          media = document.createElement("video");
          if (item.source_job_id) media.src = "/raw-upload/" + item.source_job_id;
          else media.src = "/asset/" + item.asset_id;
          media.muted = true;
          media.playsInline = true;
          try { media.currentTime = Math.max(0, (ot - start) + (item.in || 0)); } catch (e) {}
        } else {
          media = document.createElement("img");
          if (item.asset_id) media.src = "/asset/" + item.asset_id;
          else if (item.src) media.src = item.src;
        }
        const localT = Math.max(0, ot - start);
        const fadeIn = Number(item.fade_in) || 0;
        const fadeOut = Number(item.fade_out) || 0;
        let fadeMul = 1;
        if (fadeIn > 0 && localT < fadeIn) fadeMul = Math.max(0, localT / fadeIn);
        if (fadeOut > 0 && localT > dur - fadeOut) fadeMul = Math.min(fadeMul, Math.max(0, (dur - localT) / fadeOut));
        const baseOp = item.opacity != null ? item.opacity : 1.0;
        wrap.style.position = "absolute";
        wrap.style.left = (item.x != null ? item.x : 0.5) * 100 + "%";
        wrap.style.top = (item.y != null ? item.y : 0.1) * 100 + "%";
        wrap.style.width = (item.w != null ? item.w : 0.3) * 100 + "%";
        if (item.h != null) {
          wrap.style.height = (item.h * 100) + "%";
          wrap.style.aspectRatio = "auto";
        } else {
          wrap.style.aspectRatio = "16 / 9";
        }
        wrap.style.opacity = baseOp * fadeMul;
        wrap.style.overflow = "hidden";
        wrap.style.pointerEvents = "auto";
        wrap.style.cursor = "move";
        wrap.dataset.ovId = item.id;
        if (item.border_px) {
          wrap.style.boxShadow = `0 0 0 ${item.border_px}px #fff`;
        }
        media.style.position = "absolute";
        media.style.inset = "0";
        media.style.width = "100%";
        media.style.height = "100%";
        media.style.objectFit = item.fit || "cover";
        media.style.pointerEvents = "none";
        wrap.appendChild(media);
        wrap.addEventListener("pointerdown", (e) => {
          logoSelected = false;
          selectClip("overlay", item.id);
          startBoxDrag(e, "overlay", item, wrap);
        });

        layer.appendChild(wrap);
        ovEntries.push({
          el: media,
          start,
          srcIn: item.in || 0,
          dur,
          ken_burns: item.ken_burns || null,
          isVideo: media.tagName === "VIDEO",
        });
        // Apply this frame's Ken Burns immediately so rebuild doesn't flash at 1x.
        const kbScale = kenBurnsScaleAt(item.ken_burns, localT, dur);
        media.style.transformOrigin = "50% 50%";
        if (Math.abs(kbScale - 1) > 0.001) {
          media.style.transform = `scale(${kbScale.toFixed(4)})`;
        }
      }
    });

    // Ghost pending B-roll suggestions on the stage (dimmed, not interactive resize).
    pendingBroll.forEach((item) => {
      const start = item.start || 0;
      const dur = Math.max(0.4, (item.out != null ? Number(item.out) : 1.8) - (item.in || 0));
      if (ot < start || ot > start + dur) return;
      const wrap = document.createElement("div");
      wrap.className = "tl-pending-ghost";
      wrap.style.position = "absolute";
      wrap.style.left = (item.x != null ? item.x : 0.5) * 100 + "%";
      wrap.style.top = (item.y != null ? item.y : 0.1) * 100 + "%";
      wrap.style.width = (item.w != null ? item.w : 0.3) * 100 + "%";
      wrap.style.height = ((item.h != null ? item.h : 0.22) * 100) + "%";
      wrap.style.opacity = "0.72";
      wrap.style.overflow = "hidden";
      wrap.style.pointerEvents = "auto";
      wrap.style.cursor = "pointer";
      wrap.title = "Pending B-roll — click to review in Media";
      const media = document.createElement("img");
      media.src = item.asset_id ? ("/asset/" + item.asset_id) : "";
      media.style.position = "absolute";
      media.style.inset = "0";
      media.style.width = "100%";
      media.style.height = "100%";
      media.style.objectFit = item.fit || "cover";
      media.style.pointerEvents = "none";
      wrap.appendChild(media);
      wrap.addEventListener("click", (e) => {
        e.stopPropagation();
        setLeftTab("media", { pin: true });
        renderPendingBroll();
      });
      layer.appendChild(wrap);
    });

    if (tl.logo && tl.logo.asset_id) {
      const lg = tl.logo;
      const el = document.createElement("img");
      el.dataset.logoEl = "1";
      el.style.position = "absolute";
      el.style.left = (lg.x != null ? lg.x : 0.04) * 100 + "%";
      el.style.top = (lg.y != null ? lg.y : 0.04) * 100 + "%";
      el.style.width = (lg.w != null ? lg.w : 0.18) * 100 + "%";
      if (lg.h != null) {
        el.style.height = (lg.h * 100) + "%";
        el.style.objectFit = "fill";
      } else {
        el.style.height = "auto";
        el.style.objectFit = "contain";
      }
      el.style.opacity = lg.opacity != null ? lg.opacity : 0.9;
      el.style.pointerEvents = "auto";
      el.style.cursor = "move";
      el.src = "/asset/" + lg.asset_id;

      el.addEventListener("pointerdown", (e) => {
        selected = null;
        logoSelected = true;
        renderProps();
        startBoxDrag(e, "logo", lg, el);
      });

      layer.appendChild(el);
      if (logoSelected) addPreviewBox("logo", lg, "Logo");
    }

    // Render interactive bounding boxes & resize handles for selected elements
    if (selected) {
      const c = findClip(selected.track, selected.id);
      if (c) {
        if (selected.track === "text") addPreviewBox("title", c, (c.text || "Title").split("\n")[0]);
        else if (selected.track === "overlay") addPreviewBox("overlay", c, "Overlay");
        else if (selected.track === "main" && c.split && c.split.enabled) addSplitGuide(c.split);
        else if (selected.track === "effects" && c.type === "split_screen") addSplitGuide(c);
      }
    }
    // Split-screen guide whenever an effects-lane split is active at playhead
    const liveSplit = activeEffectOfType(ot, "split_screen");
    if (liveSplit && !(selected && selected.track === "effects" && selected.id === liveSplit.id)) {
      addSplitGuide(liveSplit);
    } else if (!liveSplit && activeMainClip && activeMainClip.split && activeMainClip.split.enabled
      && !(selected && selected.track === "main" && selected.id === activeMainClip.id)) {
      addSplitGuide(activeMainClip.split);
    }

    // Karaoke caption approx from the open transcript + branding style.
    liveCaptionEl = null;
    updateLiveCaptions(ot);
  }

  function sourceTimeFromKeepRanges(ot, clip) {
    const idx = tl.tracks.main.findIndex((c) => c.id === clip.id);
    if (idx < 0) return null;
    let local = ot - mainStart(idx);
    const ranges = keepRangesForClip(clip);
    let played = 0;
    for (let r = 0; r < ranges.length; r++) {
      const [a, b] = ranges[r];
      const d = Math.max(0, b - a);
      if (local <= played + d + 0.001) return a + Math.max(0, local - played);
      played += d;
    }
    return null;
  }

  function updateLiveCaptions(ot) {
    const layer = $("tlOverlayLayer");
    if (!layer || !tl) return;
    if (!liveCaptionEl || !liveCaptionEl.isConnected) {
      liveCaptionEl = document.createElement("div");
      liveCaptionEl.id = "tlLiveCaptions";
      layer.appendChild(liveCaptionEl);
    }
    const clip = activeMainAt(ot);
    if (!clip || clip.burn_captions === false || !transcriptWords || !transcriptWords.length) {
      liveCaptionEl.style.display = "none";
      return;
    }
    let srcT = null;
    if (previewingOutput || (seqPreview && seqPreview.running)) {
      srcT = sourceTimeFromKeepRanges(ot, clip);
    } else {
      const v = $("tlPreviewVideo");
      srcT = v ? (v.currentTime || 0) : null;
    }
    if (srcT == null) {
      liveCaptionEl.style.display = "none";
      return;
    }
    const style = tl.style || {};
    const groupSize = Math.max(1, Math.min(5, Number(style.group_size) || 3));
    const ranges = keepRangesForClip(clip);
    // Prefer visible (non-cut) words with clip-local renames applied.
    const visible = [];
    for (let i = 0; i < transcriptWords.length; i++) {
      const w = transcriptWords[i];
      if (isWordCut(clip, w)) continue;
      const mid = (Number(w.start || 0) + Number(w.end || 0)) / 2;
      if (!ranges.some(([a, b]) => mid >= a && mid <= b)) continue;
      visible.push({
        word: displayWordText(clip, w),
        start: w.start,
        end: w.end,
        speaker: w.speaker,
        _srcIdx: i,
      });
    }
    if (!visible.length) {
      liveCaptionEl.style.display = "none";
      return;
    }
    let wi = -1;
    for (let i = 0; i < visible.length; i++) {
      const w = visible[i];
      const a = Number(w.start || 0);
      const b = Number(w.end || a);
      if (srcT >= a && srcT <= b + 0.08) { wi = i; break; }
    }
    if (wi < 0) {
      for (let i = visible.length - 1; i >= 0; i--) {
        if (Number(visible[i].start || 0) <= srcT) { wi = i; break; }
      }
    }
    if (wi < 0) {
      liveCaptionEl.style.display = "none";
      return;
    }
    const g0 = Math.floor(wi / groupSize) * groupSize;
    const group = visible.slice(g0, g0 + groupSize);
    const primary = style.primary_color || style.primary || "#FFFFFF";
    const highlight = style.highlight_color || style.highlight || "#FFE566";
    const font = style.font_name || style.font || "Anton";
    const size = Number(style.font_size || style.size) || 64;
    const posY = (style.position_y != null ? Number(style.position_y) : 75) / 100;
    const stHeight = layer.clientHeight || 640;
    const sc = tl.speaker_colors || style.speaker_colors || {};
    liveCaptionEl.style.cssText =
      `position:absolute;left:50%;top:${posY * 100}%;transform:translate(-50%,-50%);` +
      `pointer-events:none;text-align:center;z-index:6;font-family:"${font}",sans-serif;` +
      `font-weight:800;font-size:${(size / 1000) * stHeight}px;line-height:1.15;` +
      `text-shadow:0 2px 8px rgba(0,0,0,.8);width:92%;display:block`;
    liveCaptionEl.innerHTML = group.map((w, j) => {
      const idx = g0 + j;
      let col = idx === wi ? highlight : primary;
      if (idx !== wi && w.speaker) {
        const scCol = _spkColor(sc, w.speaker);
        if (scCol) col = scCol;
      }
      let t = String(w.word || "").replace(/[<>&]/g, "");
      if (style.all_caps) t = t.toUpperCase();
      return `<span style="color:${col};margin:0 .12em">${t}</span>`;
    }).join("");
  }

  // libass anchors a title at its numpad-alignment point, not its centre: with
  // the default \an2 the stored (x, y) is the text's bottom-centre. The preview
  // used a fixed translate(-50%, -50%), so titles previewed half a text-height
  // above where they rendered (and half a box-width off for left/right aligns).
  // Map the alignment to the matching CSS transform so both agree.
  function assAnchorTransform(align) {
    const a = (align >= 1 && align <= 9) ? align : 2;
    const col = (a - 1) % 3;             // 0 left, 1 centre, 2 right
    const row = Math.floor((a - 1) / 3); // 0 bottom, 1 middle, 2 top
    const tx = col === 0 ? "0%" : (col === 1 ? "-50%" : "-100%");
    const ty = row === 0 ? "-100%" : (row === 1 ? "-50%" : "0%");
    return `translate(${tx}, ${ty})`;
  }

  function addPreviewBox(kind, obj, labelText) {
    const layer = $("tlOverlayLayer");
    const box = document.createElement("div");
    if (kind === "title") {
      box.className = "tl-pbox title";
      box.style.left = (obj.x != null ? obj.x : 0.5) * 100 + "%";
      box.style.top = (obj.y != null ? obj.y : 0.85) * 100 + "%";
      // Override the stylesheet's fixed centre transform to match libass.
      box.style.transform = assAnchorTransform(obj.align);
      box.textContent = labelText;
    } else {
      box.className = "tl-pbox";
      box.dataset.pboxKind = kind;
      if (obj.id) box.dataset.pboxId = obj.id;
      box.style.left = (obj.x != null ? obj.x : (kind === "logo" ? 0.04 : 0.5)) * 100 + "%";
      box.style.top = (obj.y != null ? obj.y : (kind === "logo" ? 0.04 : 0.1)) * 100 + "%";
      box.style.width = (obj.w != null ? obj.w : (kind === "logo" ? 0.18 : 0.3)) * 100 + "%";
      if (obj.h != null) {
        box.style.height = (obj.h * 100) + "%";
        box.style.aspectRatio = "auto";
      } else if (kind === "logo") {
        box.style.height = "auto";
        box.style.minHeight = "36px";
      } else {
        box.style.aspectRatio = "16 / 9";
      }
      const lbl = document.createElement("div");
      lbl.className = "tl-pbox-label";
      lbl.textContent = labelText;
      box.appendChild(lbl);
      // Corner + edge handles: free width / height (not locked aspect).
      [
        ["se", "nwse-resize"],
        ["e", "ew-resize"],
        ["s", "ns-resize"],
      ].forEach(([dir, cur]) => {
        const h = document.createElement("div");
        h.className = "tl-pbox-handle tl-pbox-handle-" + dir;
        h.dataset.handle = dir;
        h.style.cursor = cur;
        h.title = dir === "e" ? "Drag to change width" : (dir === "s" ? "Drag to change height" : "Drag to change width & height");
        box.appendChild(h);
      });
    }
    box.addEventListener("pointerdown", (e) => startBoxDrag(e, kind, obj, box));
    layer.appendChild(box);
  }

  function addSplitGuide(splitCfg) {
    const layer = $("tlOverlayLayer");
    if (!layer || !splitCfg) return;
    let layout = splitCfg.layout || "auto";
    if (layout === "auto") layout = (tl.canvas === "16x9") ? "side" : "stack";
    const place = (splitCfg.placement || "").toLowerCase();
    const line = document.createElement("div");
    line.style.position = "absolute";
    line.style.background = "rgba(255,255,255,.6)";
    line.style.pointerEvents = "none";
    if (layout === "side") { line.style.left = "50%"; line.style.top = "0"; line.style.bottom = "0"; line.style.width = "2px"; }
    else { line.style.top = "50%"; line.style.left = "0"; line.style.right = "0"; line.style.height = "2px"; }
    layer.appendChild(line);
    const tag = document.createElement("div");
    tag.style.cssText = "position:absolute;padding:2px 6px;font-size:10px;font-weight:700;background:rgba(0,0,0,.55);color:#fff;border-radius:4px;pointer-events:none;";
    let secondLabel = "2nd";
    if (layout === "side") {
      const secondLeft = place === "second_left" || place === "left" || place === "main_right";
      tag.textContent = secondLeft ? "2nd ←" : "→ 2nd";
      tag.style.top = "8px";
      tag.style.left = secondLeft ? "8px" : "54%";
    } else {
      const secondTop = place === "second_top" || place === "top" || place === "main_bottom";
      tag.textContent = secondTop ? "2nd ↑" : "2nd ↓";
      tag.style.left = "8px";
      tag.style.top = secondTop ? "8px" : "54%";
    }
    layer.appendChild(tag);
  }

  function _liveBoxTarget(kind, obj) {
    const layer = $("tlOverlayLayer");
    if (!layer) return null;
    if (kind === "logo") return layer.querySelector("[data-logo-el]");
    if (kind === "overlay" && obj && obj.id) return layer.querySelector(`[data-ov-id="${obj.id}"]`);
    return null;
  }

  function _applyBoxGeom(el, obj, kind) {
    if (!el || !obj) return;
    const dx = (obj.x != null ? obj.x : (kind === "logo" ? 0.04 : 0.5));
    const dy = (obj.y != null ? obj.y : (kind === "logo" ? 0.04 : 0.1));
    const dw = (obj.w != null ? obj.w : (kind === "logo" ? 0.18 : 0.3));
    el.style.left = (dx * 100) + "%";
    el.style.top = (dy * 100) + "%";
    el.style.width = (dw * 100) + "%";
    if (obj.h != null) {
      el.style.height = (obj.h * 100) + "%";
      el.style.aspectRatio = "auto";
      if (kind === "logo") el.style.objectFit = "fill";
    }
  }

  let boxDrag = null;
  function startBoxDrag(e, kind, obj, box) {
    pushHistory();
    const rect = $("tlStage").getBoundingClientRect();
    const handleEl = e.target.classList && e.target.classList.contains("tl-pbox-handle")
      ? e.target
      : (e.target.closest && e.target.closest(".tl-pbox-handle"));
    const measuredH = (box && box.offsetHeight && rect.height)
      ? (box.offsetHeight / rect.height)
      : (kind === "logo" ? 0.1 : 0.22);
    boxDrag = {
      kind, obj, box, rect,
      isHandle: !!handleEl,
      handle: (handleEl && handleEl.dataset.handle) || "se",
      sx: e.clientX, sy: e.clientY,
      ox: obj.x != null ? obj.x : (kind === "logo" ? 0.04 : 0.5),
      oy: obj.y != null ? obj.y : (kind === "logo" ? 0.04 : 0.1),
      ow: obj.w != null ? obj.w : (kind === "logo" ? 0.18 : 0.3),
      oh: obj.h != null ? obj.h : measuredH,
    };
    try { box.setPointerCapture(e.pointerId); } catch (err) {}
    e.preventDefault();
    e.stopPropagation();
  }
  function onBoxMove(e) {
    if (!boxDrag) return;
    const { rect, obj, box, isHandle, handle, kind } = boxDrag;
    if (isHandle) {
      const dx = (e.clientX - boxDrag.sx) / Math.max(1, rect.width);
      const dy = (e.clientY - boxDrag.sy) / Math.max(1, rect.height);
      if (handle === "se" || handle === "e") {
        obj.w = Math.min(1, Math.max(0.05, boxDrag.ow + dx));
      }
      if (handle === "se" || handle === "s") {
        obj.h = Math.min(1, Math.max(0.05, boxDrag.oh + dy));
      } else if (obj.h == null && handle === "e") {
        // Keep existing height if any was measured so width-only doesn't snap aspect.
        obj.h = boxDrag.oh;
      }
      _applyBoxGeom(box, obj, kind);
      _applyBoxGeom(_liveBoxTarget(kind, obj), obj, kind);
    } else {
      obj.x = Math.min(1, Math.max(0, boxDrag.ox + (e.clientX - boxDrag.sx) / Math.max(1, rect.width)));
      obj.y = Math.min(1, Math.max(0, boxDrag.oy + (e.clientY - boxDrag.sy) / Math.max(1, rect.height)));
      _applyBoxGeom(box, obj, kind);
      _applyBoxGeom(_liveBoxTarget(kind, obj), obj, kind);
    }
  }
  function onBoxUp() {
    if (!boxDrag) return;
    boxDrag = null;
    renderProps();   // refresh the X/Y/size sliders to the dragged values
    scheduleSave();
    updateStageCompositor();
  }

  // ---- Preview <-> playhead sync (native <video controls> does the seeking) ----
  function wireScrub() {
    const v = $("tlPreviewVideo");
    if (!v) return;
    const upd = () => {
      updatePlayhead();
      updateStageCompositor();
      // Highlight the word under the playhead in the transcript doc.
      if (leftTab === "transcript" && transcriptWords) highlightTranscriptAt(v.currentTime);
    };
    // timeupdate stays as a coarse fallback (~4 Hz) for browsers that throttle
    // rAF in background tabs; the rAF loop is what drives smooth playback.
    v.addEventListener("timeupdate", () => { if (rafId == null) upd(); });
    v.addEventListener("loadedmetadata", upd);
    v.addEventListener("seeked", upd);
    v.addEventListener("play", () => { upd(); startPreviewLoop(); });
    v.addEventListener("playing", startPreviewLoop);
    v.addEventListener("pause", () => { stopPreviewLoop(); upd(); });
    v.addEventListener("ended", () => { stopPreviewLoop(); upd(); });
  }

  function findClip(track, id) {
    if (!tl || !tl.tracks[track]) return null;
    return tl.tracks[track].find((c) => c.id === id);
  }

  function renderProps() {
    const wrap = $("tlProps");
    if (!wrap) return;
    if (!selected) {
      renderProjectProps(wrap);
      updateStageCompositor();
      return;
    }
    const c = findClip(selected.track, selected.id);
    if (!c) {
      wrap.innerHTML = '<div class="tl-props-empty muted">Clip removed.</div>';
      return;
    }
    const t = selected.track;
    let html = `<h3>${({ main: "🎬 Main clip", overlay: "🖼 Overlay", effects: "✨ Effect", text: "🔤 Title", music: "🎵 Music" })[t]}</h3>`;

    if (t === "effects") {
      const typeOpts = EFFECT_TYPES.map((x) => [x.id, `${x.icon} ${x.label}`]);
      html += propSelect("type", "Effect", c.type || "punch_zoom", typeOpts);
      html += `<div class="tl-prop-grid">${propNum("start", "Start (s)", c.start || 0, 0, 99999, 0.1)}${propNum("dur", "Duration (s)", clipDuration(c), 0.2, 120, 0.1)}</div>`;
      html += `<p class="muted" style="font-size:.72rem">Drag the clip on the Effects lane to change when it runs. Main-clip effects still work; lane effects win while they overlap.</p>`;
      if (c.type === "punch_zoom") {
        html += `<div class="tl-prop-grid">${propSelect("intensity", "Strength", c.intensity || "med", [["1.5x", "1.5× hold"], ["2x", "2× hold"], ["low", "Low punch"], ["med", "Medium punch"], ["high", "Strong punch"], ["strong", "Strong punch"]])}</div>`;
      } else if (c.type === "zoom_1_5" || c.type === "zoom_2x") {
        html += `<p class="muted" style="font-size:.72rem">Constant ${c.type === "zoom_1_5" ? "1.5×" : "2×"} zoom held for the whole effect window — no ease-out.</p>`;
      } else if (c.type === "ken_burns") {
        html += `<div class="tl-prop-grid">${propSelect("direction", "Direction", c.direction || "in", [["in", "Zoom in"], ["out", "Zoom out"]])}${propSelect("intensity", "Strength", c.intensity || "med", [["low", "Subtle"], ["med", "Medium"], ["high", "Strong"]])}</div>`;
      } else if (c.type === "split_screen") {
        const splitOpts = [["", "— pick second media —"]].concat(
          sources.map((s) => [
            `job:${s.job_id}`,
            `🎬 ${(s.filename || s.job_id.slice(0, 8)).replace(/\.[^.]+$/, "")}`,
          ]),
          assets
            .filter((a) => a.kind === "image" || a.kind === "gif" || a.kind === "video")
            .map((a) => {
              const icon = a.kind === "image" ? "🖼" : (a.kind === "gif" ? "🎞" : "🎬");
              const label = (a.filename || a.keyword || a.asset_id.slice(0, 8)).replace(/\.[^.]+$/, "");
              return [`asset:${a.asset_id}`, `${icon} ${label}`];
            })
        );
        const curMedia = c.source_job_id
          ? `job:${c.source_job_id}`
          : (c.asset_id ? `asset:${c.asset_id}` : "");
        html += propSelect("__split_media", "Second media", curMedia, splitOpts);
        const splitAsset = c.asset_id
          ? assets.find((a) => a.asset_id === c.asset_id)
          : null;
        const isStill = !!(splitAsset && (splitAsset.kind === "image" || splitAsset.kind === "gif"));
        html += `<div class="tl-prop-grid">${propSelect("layout", "Layout", c.layout || "stack", [["auto", "Auto"], ["side", "Side by side"], ["stack", "Top / bottom"]])}`;
        if (!isStill) {
          html += propNum("in", "2nd start (s)", c.in || 0, 0, 99999, 0.1);
        }
        html += `</div>`;
        if (!isStill) {
          html += `<button type="button" class="btn btn-secondary btn-block" data-act="split-scrub" ${c.source_job_id ? "" : "disabled"} style="margin:2px 0 8px">🎬 Scrub 2nd video…</button>`;
        } else {
          html += `<p class="muted" style="font-size:.72rem;margin:2px 0 8px">Still / GIF is looped for the effect duration — no IN scrub needed.</p>`;
        }
        const lay = c.layout || "stack";
        const place = c.placement || (lay === "side" ? "second_right" : "second_bottom");
        if (lay === "side") {
          html += propSelect("placement", "Second media goes…", place,
            [["second_left", "Left"], ["second_right", "Right (default)"]]);
        } else {
          html += propSelect("placement", "Second media goes…", place,
            [["second_top", "Top"], ["second_bottom", "Bottom (default)"]]);
        }
        html += `<p class="muted" style="font-size:.72rem">Main stays the other half. Audio always comes from Main. Videos, stills, and GIFs from Media all work as the second panel.</p>`;
      } else if (c.type === "color") {
        html += `<div class="tl-swatches" id="tlFxSwatches">` +
          COLOR_PRESETS.map(([v, t2]) =>
            `<div class="tl-swatch tl-swatch-${v} ${(c.preset || "none") === v ? "active" : ""}" data-preset="${v}" title="${t2}">${t2}</div>`
          ).join("") + `</div>`;
        html += `<div class="tl-prop-grid" style="margin-top:8px">${propRange("brightness", "Brightness", c.brightness != null ? c.brightness : 0, -0.3, 0.3, 0.02)}${propRange("contrast", "Contrast", c.contrast != null ? c.contrast : 1, 0.5, 1.5, 0.02)}</div>`;
        html += propRange("saturation", "Saturation", c.saturation != null ? c.saturation : 1, 0, 2, 0.05);
      }
    } else if (t === "text") {
      html += propTextarea("text", "Text (use line breaks)", c.text);
      html += `<div class="tl-prop-grid">${propNum("size", "Font size", c.size, 10, 200)}${propSelect("font", "Font", c.font, FONT_OPTS)}</div>`;
      html += `<div class="tl-prop-grid">${propColor("color", "Text color", c.color)}${propSelect("anim", "Animation", c.anim, [["fade", "Fade"], ["slideup", "Slide up"], ["none", "None"]])}</div>`;
      html += propCheck("bg_enabled", "Background box (lower-third)", c.bg_enabled);
      html += `<div class="tl-prop-grid">${propColor("bg_color", "Box color", c.bg_color)}${propRange("bg_opacity", "Box opacity", c.bg_opacity, 0, 1, 0.05)}</div>`;
      html += `<div class="tl-prop-grid">${propRange("x", "Position X", c.x, 0, 1, 0.01)}${propRange("y", "Position Y", c.y, 0, 1, 0.01)}</div>`;
      html += `<div class="tl-prop-grid">${propNum("start", "Start (s)", c.start, 0, 99999, 0.1)}${propNum("dur", "Duration (s)", clipDuration(c), 0.2, 99999, 0.1)}</div>`;
    } else if (t === "music") {
      html += `<div class="tl-prop-grid">${propNum("start", "Start (s)", c.start, 0, 99999, 0.1)}${propNum("gain_db", "Volume (dB)", c.gain_db, -40, 10, 1)}</div>`;
      html += `<div class="tl-prop-grid">${propNum("in", "Trim in (s)", c.in, 0, c._max || 99999, 0.1)}${propNum("out", "Trim out (s)", c.out, 0.1, c._max || 99999, 0.1)}</div>`;
      html += propCheck("duck", "Duck under voice (auto-lower during speech)", c.duck);
    } else if (t === "overlay") {
      let ovBody = "";
      ovBody += `<div class="tl-prop-grid">${propNum("start", "Start (s)", c.start, 0, 99999, 0.1)}${propRange("opacity", "Opacity", c.opacity != null ? c.opacity : 1, 0, 1, 0.05)}</div>`;
      ovBody += `<div class="tl-prop-grid">${propNum("in", "Trim in (s)", c.in, 0, c._max || 99999, 0.1)}${propNum("out", "Trim out (s)", c.out, 0.1, c._max || 99999, 0.1)}</div>`;
      ovBody += `<label class="tl-prop-sectlabel">Layout presets</label>`;
      ovBody += `<div class="tl-layout-presets">`;
      Object.keys(OVERLAY_LAYOUTS).forEach((id) => {
        const L = OVERLAY_LAYOUTS[id];
        const active = c.layout === id ? " active" : "";
        ovBody += `<button type="button" class="tl-chip-btn${active}" data-act="ovlayout" data-layout="${id}">${L.label}</button>`;
      });
      ovBody += `</div>`;
      ovBody += `<div class="tl-prop-grid">${propRange("x", "Position X", c.x != null ? c.x : 0.5, 0, 1, 0.01)}${propRange("y", "Position Y", c.y != null ? c.y : 0.1, 0, 1, 0.01)}</div>`;
      ovBody += `<div class="tl-prop-grid">${propRange("w", "Width", c.w != null ? c.w : 0.34, 0.05, 1.0, 0.01)}${propRange("h", "Height", c.h != null ? c.h : 0.22, 0.05, 1.0, 0.01)}</div>`;
      const fitOpts = [["cover", "Cover / Crop"], ["contain", "Contain / Fit"], ["fill", "Stretch"]];
      ovBody += propSelect("fit", "Fit mode", c.fit || "cover", fitOpts);
      ovBody += `<div class="tl-prop-grid">${propRange("fade_in", "Fade in (s)", c.fade_in != null ? c.fade_in : 0.15, 0, 1.5, 0.05)}${propRange("fade_out", "Fade out (s)", c.fade_out != null ? c.fade_out : 0.2, 0, 1.5, 0.05)}</div>`;
      ovBody += propRange("border_px", "White border (px)", c.border_px != null ? c.border_px : 0, 0, 16, 1);
      html += propSection("🖼 Overlay layout", ovBody, true);
      const ovKb = c.ken_burns || {};
      let kbBody = propCheck("ken_burns.enabled", "Slow zoom on this overlay moment", ovKb.enabled);
      if (ovKb.enabled) {
        kbBody += `<div class="tl-prop-grid">${propSelect("ken_burns.direction", "Direction", ovKb.direction || "in", [["in", "Zoom in (push)"], ["out", "Zoom out (pull)"]])}${propSelect("ken_burns.intensity", "Strength", ovKb.intensity || "med", [["low", "Subtle"], ["med", "Medium"], ["high", "Strong"]])}</div>`;
        kbBody += `<p class="muted" style="font-size:.72rem">Best on photo B-roll and short inserts — keeps still frames alive for the beat.</p>`;
      }
      html += propSection("🔍 Ken Burns (B-roll motion)", kbBody, !!ovKb.enabled);
      const geminiOk = !!window._brollGeminiReady;
      let replaceBody = `<p class="muted" style="font-size:.72rem;line-height:1.4;margin:0 0 8px">CapCut Replace — swap this overlay’s media without moving timing/layout.</p>`;
      replaceBody += `<label class="muted" style="font-size:.74rem;display:block;margin-bottom:6px">AI prompt`
        + `<textarea data-ov-ai-prompt rows="2" style="display:block;width:100%;margin-top:4px;padding:8px;background:#10131d;border:1px solid #3b4252;color:#fff;border-radius:8px;resize:vertical" placeholder="e.g. festival crowd bokeh lights, photo">${esc(c.keyword || "")}</textarea></label>`;
      replaceBody += `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">`
        + `<button type="button" class="btn btn-primary btn-sm" data-act="ov-ai-gen" ${geminiOk ? "" : "disabled"}>${geminiOk ? "✨ Generate + replace" : "Set GEMINI_API_KEY"}</button>`
        + `<button type="button" class="btn btn-secondary btn-sm" data-act="ov-lib-replace">📚 Pick from Library</button>`
        + `</div>`
        + `<p class="muted" data-ov-ai-status style="font-size:.72rem;margin:8px 0 0"></p>`;
      html += propSection("↻ Replace media", replaceBody, false);
    } else { // main
      const st = normalizeTlStyle(tl.style || {});
      const primary = st.primary_color || st.primary || "#FFFFFF";
      const highlight = st.highlight_color || st.highlight || "#FFD60A";
      const fontName = st.font_name || st.font || "Anton";
      const fontSize = st.font_size != null ? st.font_size : (st.size != null ? st.size : 64);

      let trimBody = "";
      trimBody += `<div class="tl-prop-grid">${propNum("in", "Trim in (s)", c.in, 0, c._max || 99999, 0.1)}${propNum("out", "Trim out (s)", c.out, 0.1, c._max || 99999, 0.1)}</div>`;
      trimBody += `<div class="tl-prop-inline" style="gap:6px;margin-bottom:10px"><button class="btn btn-secondary" data-act="setin" style="flex:1;font-size:.78rem">⤓ Set IN here</button><button class="btn btn-secondary" data-act="setout" style="flex:1;font-size:.78rem">Set OUT here ⤓</button></div>`;
      const trType = (c.transition && c.transition.type) || "";
      trimBody += propSelect("__transition", "Transition into this clip", trType, TRANSITION_OPTS);
      trimBody += `<p class="muted" style="font-size:.72rem">Crossfade from the previous clip. The first clip ignores this. Edit words in the left <strong>Transcript</strong> panel.</p>`;
      html += propSection("✂ Trim", trimBody, true);

      let capBody = propCheck("burn_captions", "Burn word-by-word captions (from transcript)", c.burn_captions !== false);
      capBody += `<p class="muted" style="font-size:.72rem;line-height:1.4;margin:6px 0 8px">Caption words come from this clip’s Whisper transcript. Look / branding below is what they render as.</p>`;
      capBody += `<div class="tl-cap-preview">
        <span style="font-family:${esc(fontName)},sans-serif;font-weight:900;font-size:1.1rem;letter-spacing:.02em">
          <span style="color:${esc(primary)}">here with.</span>
          <span style="color:${esc(highlight)}"> Vanessa,</span>
        </span>
        <span class="muted" style="font-size:.7rem;display:block;margin-top:4px">${esc(fontName)} · ${fontSize}px</span>
      </div>`;
      capBody += `<div class="tl-prop-grid">${propSelect("__style_font", "Font", fontName, FONT_OPTS)}${propNum("__style_size", "Size", fontSize, 24, 140, 2)}</div>`;
      capBody += `<div class="tl-prop-grid">${propColor("__style_primary", "Base color", primary)}${propColor("__style_highlight", "Karaoke highlight", highlight)}</div>`;
      capBody += `<button class="btn btn-secondary btn-block" data-act="open-captions" style="margin-top:6px">🎨 Open Captions (full Look)</button>`;
      html += propSection("💬 Captions style", capBody, true);

      let aiBody = `<button class="btn btn-secondary btn-block" data-act="suggestfx">Suggest camera moves</button>`;
      aiBody += `<p class="muted" style="font-size:.72rem">Reads this clip’s transcript and proposes timed moves on the Effects lane.</p>`;
      aiBody += `<div id="tlFxList" style="margin-top:8px"></div>`;
      aiBody += `<hr class="tl-sep">`;
      aiBody += `<div class="tl-prop-grid">
        <label class="tl-prop">Style<select data-restyle-pack>
          <option value="pulse">Pulse</option>
          <option value="clarity">Clarity</option>
          <option value="magazine">Magazine</option>
          <option value="velocity">Velocity</option>
          <option value="film">Film</option>
        </select></label>
        <label class="tl-prop">Intensity<select data-restyle-intensity>
          <option value="low">Low</option>
          <option value="med" selected>Med</option>
          <option value="high">High</option>
        </select></label>
      </div>`;
      aiBody += `<button class="btn btn-secondary btn-block" data-act="restyle" style="margin-top:6px">Restyle this shot</button>`;
      html += propSection("✨ AI", aiBody, false);

      const kb = c.ken_burns || {};
      const pz = c.punch_zoom || {};
      const sp = c.split || {};
      const col = c.color || {};
      const ref = c.reframe || {};
      const splitOpts = [["", "— pick second video —"]].concat(
        sources.filter((s) => s.job_id !== c.source_job_id).map((s) => [s.job_id, (s.filename || s.job_id.slice(0, 8)).replace(/\.[^.]+$/, "")]));

      let fxBody = `<p class="muted" style="font-size:.72rem;margin:0 0 8px;line-height:1.4">Whole-clip looks. Prefer the <strong>Effects</strong> lane when you want timed stretches.</p>`;

      fxBody += `<div class="tl-fx-block"><strong>🔍 Ken Burns</strong>`;
      fxBody += propCheck("ken_burns.enabled", "Enable", kb.enabled);
      if (kb.enabled) {
        fxBody += `<div class="tl-prop-grid">${propSelect("ken_burns.direction", "Direction", kb.direction || "in", [["in", "Zoom in"], ["out", "Zoom out"]])}${propSelect("ken_burns.intensity", "Strength", kb.intensity || "med", [["low", "Subtle"], ["med", "Medium"], ["high", "Strong"]])}</div>`;
      }
      fxBody += `</div>`;

      fxBody += `<div class="tl-fx-block"><strong>⚡ Punch zoom</strong>`;
      fxBody += propCheck("punch_zoom.enabled", "Enable", pz.enabled);
      if (pz.enabled) {
        fxBody += `<div class="tl-prop-grid">${propSelect("punch_zoom.intensity", "Strength", pz.intensity || "med", [["1.5x", "1.5× hold"], ["2x", "2× hold"], ["low", "Low (1.15x)"], ["med", "Medium (1.25x)"], ["strong", "Strong (1.40x)"]])}</div>`;
      }
      fxBody += `</div>`;

      fxBody += `<div class="tl-fx-block"><strong>⬓ Split-screen</strong>`;
      fxBody += propCheck("split.enabled", "Enable", sp.enabled);
      if (sp.enabled) {
        fxBody += propSelect("split.source_job_id", "Second video", sp.source_job_id || "", splitOpts);
        fxBody += `<div class="tl-prop-grid">${propSelect("split.layout", "Layout", sp.layout || "stack", [["auto", "Auto"], ["side", "Side by side"], ["stack", "Top / bottom"]])}${propNum("split.in", "2nd start (s)", sp.in || 0, 0, 99999, 0.1)}</div>`;
        const lay = sp.layout || "stack";
        const place = sp.placement || (lay === "side" ? "second_right" : "second_bottom");
        if (lay === "side") {
          fxBody += propSelect("split.placement", "Second video goes…", place,
            [["second_left", "Left"], ["second_right", "Right (default)"]]);
        } else {
          fxBody += propSelect("split.placement", "Second video goes…", place,
            [["second_top", "Top"], ["second_bottom", "Bottom (default)"]]);
        }
      }
      fxBody += `</div>`;

      fxBody += `<div class="tl-fx-block"><strong>🎨 Color</strong>`;
      fxBody += `<div class="tl-swatches" id="tlSwatches">` +
        COLOR_PRESETS.map(([v, t2]) =>
          `<div class="tl-swatch tl-swatch-${v} ${ (col.preset || "none") === v ? "active" : ""}" data-preset="${v}" title="${t2}">${t2}</div>`
        ).join("") + `</div>`;
      fxBody += `<div class="tl-prop-grid" style="margin-top:8px">${propRange("color.brightness", "Brightness", col.brightness != null ? col.brightness : 0, -0.3, 0.3, 0.02)}${propRange("color.contrast", "Contrast", col.contrast != null ? col.contrast : 1, 0.5, 1.5, 0.02)}</div>`;
      fxBody += propRange("color.saturation", "Saturation", col.saturation != null ? col.saturation : 1, 0, 2, 0.05);
      fxBody += `</div>`;

      fxBody += `<div class="tl-fx-block"><strong>📱 9:16 speaker reframe</strong>`;
      fxBody += `<p class="muted" style="font-size:.72rem;line-height:1.4;margin:4px 0 8px">After <strong>Analyze speakers</strong>, crops a vertical stack (e.g. active speaker + wide) for Reels/TikTok. Needs Analyze first.</p>`;
      fxBody += propCheck("reframe.enabled", "Enable on this clip", ref.enabled);
      if (ref.enabled) {
        const pOpts = [["active", "Active Speaker"], ["left", "Left Person"], ["right", "Right Person"], ["full", "Wide Shot"]];
        fxBody += `<div class="tl-prop-grid">${propSelect("reframe.top_panel", "Top panel", ref.top_panel || "active", pOpts)}${propSelect("reframe.bottom_panel", "Bottom panel", ref.bottom_panel || "full", pOpts)}</div>`;
      }
      fxBody += `</div>`;

      html += propSection("✨ Effects (whole clip)", fxBody, false);
    }

    html += `<button class="tl-del-btn" data-act="del">🗑 Delete clip</button>`;
    wrap.innerHTML = html;
    wireProps(wrap, t, c);
    // Color swatch clicks (not a generic data-key control).
    wrap.querySelectorAll(".tl-swatch").forEach((sw) => {
      sw.onclick = () => {
        pushHistory();
        if (t === "effects") {
          c.preset = sw.dataset.preset;
        } else {
          if (!c.color) c.color = {};
          c.color.preset = sw.dataset.preset;
        }
        renderProps(); renderTracks(); scheduleSave();
      };
    });
    updateStageCompositor();
    if (typeof window.refreshMobileContextTools === "function") {
      try { window.refreshMobileContextTools(); } catch (e) { /* optional */ }
    }
  }

  const COLOR_PRESETS = [
    ["none", "None"], ["neutral", "Neutral"], ["warm", "Warm"],
    ["cool", "Cool"], ["vivid", "Vivid"], ["bw", "B&W"],
  ];

  // Project-level settings (shown when no clip is selected): persistent logo.
  function renderProjectProps(wrap) {
    if (!tl) {
      wrap.innerHTML = '<div class="tl-props-empty muted">Create or open a project to start.</div>';
      return;
    }
    const imgVid = assets.filter((a) => a.kind === "image" || a.kind === "video");
    const lg = tl.logo || {};
    const opts = [["", "— no logo —"]].concat(imgVid.map((a) =>
      [a.asset_id, a.filename || a.keyword || `${a.kind} · ${a.ext}`]));
    let html = `<h3>⚙ Project</h3>`;
    html += `<p class="muted" style="font-size:.74rem;line-height:1.4">No clip selected. Drag media onto lanes, or open Captions / Audio below.</p>`;
    html += `<p class="muted" style="font-size:.72rem;line-height:1.4">Project name is the field in the top toolbar; it auto-saves while typing, or press <strong>Save name</strong> to commit it right away.</p>`;
    if (!(tl.tracks.main || []).length) {
      html += `<p class="muted" style="font-size:.74rem;padding:8px 10px;background:#181c28;border:1px solid #2a2f3a;border-radius:8px;line-height:1.45">This project has <strong>0 Main clips</strong>. Drag a video from <strong>Media</strong> onto the Main lane to start.</p>`;
    }

    html += `<div class="tl-prop-stack">
      <button class="btn btn-primary btn-block" data-act="open-captions" type="button" style="background:linear-gradient(135deg,#9785ff,#6c5cff);color:#fff">🎨 Captions</button>
      <button class="btn btn-secondary btn-block" data-act="open-audio" type="button">🔊 Audio Enhancement</button>
      <p class="muted" style="font-size:.72rem;line-height:1.4;margin:0">Captions = fonts/colors/karaoke. Audio = noise/voice/loudness (applies on <strong>▶ Render</strong>).</p>
    </div>`;

    let logoBody = "";
    if (!imgVid.length) {
      logoBody += `<p class="muted" style="font-size:.74rem">Upload a logo image first (Media → Assets).</p>`;
    }
    logoBody += propSelect("__logo_asset", "Logo image", lg.asset_id || "", opts);
    if (lg.asset_id) {
      logoBody += `<p class="muted" style="font-size:.72rem;line-height:1.4">Click the logo on the preview, then drag the white circles to stretch width/height. Sliders below are fine-tune.</p>`;
      logoBody += `<div class="tl-prop-grid">${propRange("__logo_x", "Position X", lg.x != null ? lg.x : 0.04, 0, 1, 0.01)}${propRange("__logo_y", "Position Y", lg.y != null ? lg.y : 0.04, 0, 1, 0.01)}</div>`;
      logoBody += `<div class="tl-prop-grid">${propRange("__logo_w", "Width", lg.w != null ? lg.w : 0.18, 0.03, 0.8, 0.01)}${propRange("__logo_h", "Height", lg.h != null ? lg.h : 0.1, 0.03, 0.8, 0.01)}</div>`;
      logoBody += propRange("__logo_opacity", "Opacity", lg.opacity != null ? lg.opacity : 0.9, 0.1, 1, 0.05);
      logoBody += `<button class="btn btn-secondary btn-block" data-act="select-logo" type="button" style="margin-top:6px">Select logo on preview</button>`;
    }
    html += propSection("🏷 Logo / watermark", logoBody, !!lg.asset_id);

    let sfxBody = propCheck("__sfx_overlays", "Auto whoosh / click when overlays & zooms land", tl.sfx_overlays !== false);
    sfxBody += `<p class="muted" style="font-size:.72rem;line-height:1.4;margin:4px 0 0">Baked on ▶ Render — tiny accents under speech (Captions-style). Uncheck to silence.</p>`;
    html += propSection("🔊 Overlay SFX", sfxBody, true);

    const sc = tl.speaker_colors || {};
    const hb = tl.headline_banner;
    const hbText = typeof hb === "string" ? hb : (hb && hb.text) || "";
    const mainClip = (tl.tracks.main || [])[0] || null;
    const spkKeys = Object.keys(sc).filter((k) => /^SPEAKER_\d+$/i.test(k)).sort();
    if (!spkKeys.length) spkKeys.push("SPEAKER_00", "SPEAKER_01");

    let spkBody = `<div class="tl-spk-panel">`;
    if (!mainClip) {
      spkBody += `<p class="muted" style="font-size:.72rem;margin:0 0 10px;line-height:1.4">Add a Main clip first, then Analyze.</p>`;
    } else {
      spkBody += `<button class="btn btn-secondary btn-block" data-act="analyze-speakers" type="button">Analyze speakers</button>`;
      spkBody += `<p class="muted" style="font-size:.72rem;margin:6px 0 12px;line-height:1.4">Runs diarization (+ faces when available) for speaker colors and 9:16 reframe.</p>`;
    }
    spkBody += `<div class="tl-spk-colors">`;
    spkKeys.forEach((key) => {
      const label = key === "SPEAKER_00" ? "Host" : (key === "SPEAKER_01" ? "Guest" : key.replace("SPEAKER_", "Spk "));
      spkBody += `<label class="tl-spk-swatch"><span>${label}</span><input type="color" data-key="__sc:${key}" value="${sc[key] || _spkColor({}, key) || "#FFD700"}"></label>`;
    });
    spkBody += `</div>`;
    spkBody += `<label class="tl-prop tl-headline-row"><span>Headline banner</span><input type="text" data-key="__headline" value="${(hbText || "").replace(/"/g, "&quot;")}" placeholder="Optional lower-third / banner text"></label>`;
    spkBody += `</div>`;
    html += propSection("👥 Speakers", spkBody, false);

    if (tl.ai_edit) {
      html += `<p class="muted" style="font-size:.72rem;margin-top:8px">AI Edit seed: ${esc(tl.ai_edit.style_pack || "")} · ${esc(tl.ai_edit.intensity || "med")}</p>`;
    }

    html += `<button class="tl-del-btn" data-act="delete-project" type="button" style="margin-top:12px">🗑 Delete project</button>`;
    wrap.innerHTML = html;

    const openCap = wrap.querySelector('[data-act="open-captions"]');
    if (openCap) openCap.onclick = () => jumpLookSection("captions");
    const openAud = wrap.querySelector('[data-act="open-audio"]');
    if (openAud) openAud.onclick = () => jumpLookSection("audio");
    const selLogo = wrap.querySelector('[data-act="select-logo"]');
    if (selLogo) selLogo.onclick = () => {
      selected = null;
      logoSelected = true;
      updateStageCompositor();
      renderProps();
    };
    const delProj = wrap.querySelector('[data-act="delete-project"]');
    if (delProj) delProj.onclick = () => deleteCurrentProject();
    const analyzeProj = wrap.querySelector('[data-act="analyze-speakers"]');
    if (analyzeProj) {
      analyzeProj.onclick = () => {
        const clip = (selected && selected.track === "main")
          ? findClip("main", selected.id)
          : ((tl && tl.tracks && tl.tracks.main && tl.tracks.main[0]) || null);
        if (!clip || !clip.source_job_id) {
          alert("Add a Main clip first, then Analyze speakers.");
          return;
        }
        const statusEl = $("tlAnalyzeStatus");
        if (typeof window.startReframeAnalyze === "function") {
          window.startReframeAnalyze(analyzeProj, {
            jobId: clip.source_job_id,
            onStatus: (msg) => { if (statusEl) statusEl.textContent = msg || ""; },
          });
        } else {
          alert("Analyze is still loading — try again in a second.");
        }
      };
    }

    wrap.querySelectorAll("[data-key]").forEach((inp) => {
      const key = inp.dataset.key;
      const ev = inp.tagName === "SELECT" || inp.type === "checkbox" || inp.type === "color" ? "change" : "input";
      inp.addEventListener(ev, () => {
        if (key === "__logo_asset") {
          if (inp.value) tl.logo = Object.assign({ x: 0.04, y: 0.04, w: 0.18, opacity: 0.9 }, tl.logo || {}, { asset_id: inp.value });
          else tl.logo = null;
          renderProps();
        } else if (key === "__sfx_overlays") {
          tl.sfx_overlays = !!inp.checked;
        } else if (key && key.startsWith("__sc:")) {
          const spk = key.slice(5);
          tl.speaker_colors = tl.speaker_colors || {};
          tl.speaker_colors[spk] = inp.value;
        } else if (key === "__sc0" || key === "__sc1") {
          tl.speaker_colors = tl.speaker_colors || {};
          if (key === "__sc0") tl.speaker_colors.SPEAKER_00 = inp.value;
          else tl.speaker_colors.SPEAKER_01 = inp.value;
        } else if (key === "__headline") {
          const t = inp.value.trim();
          tl.headline_banner = t ? { text: t } : null;
          if (tl.style) tl.style.headline_banner = t;
        } else {
          if (!tl.logo) return;
          const field = key.replace("__logo_", "");
          tl.logo[field] = parseFloat(inp.value);
          const outSpan = wrap.querySelector(`[data-out="${key}"]`);
          if (outSpan) outSpan.textContent = (+inp.value).toFixed(2);
        }
        updateStageCompositor();
        scheduleSave();
      });
    });
    if (typeof window.refreshMobileContextTools === "function") {
      try { window.refreshMobileContextTools(); } catch (e) { /* optional */ }
    }
  }

  const FONT_OPTS = [
    ["Anton", "Anton"], ["Bebas Neue", "Bebas Neue"], ["Oswald", "Oswald"],
    ["Archivo Black", "Archivo Black"], ["Montserrat Thin Black", "Montserrat Black"],
    ["Alfa Slab One", "Alfa Slab One"], ["Staatliches", "Staatliches"],
    ["Passion One", "Passion One"], ["DM Sans", "DM Sans"],
  ];
  const TRANSITION_OPTS = [
    ["", "Hard cut"], ["fade", "Crossfade"], ["fadeblack", "Fade through black"],
    ["dissolve", "Dissolve"], ["slideleft", "Slide left"], ["slideright", "Slide right"],
    ["wipeleft", "Wipe left"], ["circleopen", "Circle open"], ["radial", "Radial"],
  ];

  // ---- Property control builders ----
  function propText(key, label, val) {
    return `<div class="tl-prop-row"><label>${label}</label><input type="text" data-key="${key}" value="${esc(val)}"></div>`;
  }
  function propTextarea(key, label, val) {
    return `<div class="tl-prop-row"><label>${label}</label><textarea data-key="${key}">${esc(val)}</textarea></div>`;
  }
  function propNum(key, label, val, min, max, step) {
    return `<div class="tl-prop-row"><label>${label}</label><input type="number" data-key="${key}" value="${val}" min="${min}" max="${max}" step="${step || 1}"></div>`;
  }
  function propRange(key, label, val, min, max, step) {
    return `<div class="tl-prop-row"><label>${label}: <span data-out="${key}">${(+val).toFixed(2)}</span></label><input type="range" data-key="${key}" value="${val}" min="${min}" max="${max}" step="${step}"></div>`;
  }
  function propColor(key, label, val) {
    return `<div class="tl-prop-row"><label>${label}</label><input type="color" data-key="${key}" value="${val}"></div>`;
  }
  function propCheck(key, label, val) {
    return `<div class="tl-prop-row"><label class="tl-prop-inline"><input type="checkbox" data-key="${key}" ${val ? "checked" : ""}> ${label}</label></div>`;
  }
  function propSelect(key, label, val, opts) {
    const o = opts.map(([v, t]) => `<option value="${v}" ${v === val ? "selected" : ""}>${t}</option>`).join("");
    return `<div class="tl-prop-row"><label>${label}</label><select data-key="${key}">${o}</select></div>`;
  }
  /** Collapsible right-rail section (restored details/summary). */
  function propSection(title, bodyHtml, open) {
    return `<details class="tl-prop-details"${open ? " open" : ""}>` +
      `<summary class="tl-prop-summary">${title}</summary>` +
      `<div class="tl-prop-body">${bodyHtml}</div></details>`;
  }

  function wireProps(wrap, track, c) {
    wrap.querySelectorAll("[data-key]").forEach((inp) => {
      const key = inp.dataset.key;
      let histPushed = false;
      inp.addEventListener("focus", () => { histPushed = false; });
      const handler = () => {
        if (!histPushed) { pushHistory(); histPushed = true; }
        let v;
        if (inp.type === "checkbox") v = inp.checked;
        else if (inp.type === "number" || inp.type === "range") v = parseFloat(inp.value);
        else v = inp.value;

        if (key === "dur") {
          // Titles + Effects lane: `out` stores duration (in is unused / 0).
          if (track === "effects" || track === "text") {
            c.out = Math.max(0.2, v);
            if (c.in) c.in = 0;
          } else {
            c.out = (c.in || 0) + Math.max(0.2, v);
          }
        } else if (key === "__split_media") {
          if (typeof v === "string" && v.startsWith("job:")) {
            c.source_job_id = v.slice(4);
            c.asset_id = null;
          } else if (typeof v === "string" && v.startsWith("asset:")) {
            c.asset_id = v.slice(6);
            c.source_job_id = null;
            c.in = 0;
          } else {
            c.source_job_id = null;
            c.asset_id = null;
          }
          renderProps();
          renderTimeline();
          updateStageCompositor();
          scheduleSave();
          return;
        } else if (key === "__transition") {
          c.transition = v ? { type: v } : null;
        } else if (key === "__style_font" || key === "__style_size" || key === "__style_primary" || key === "__style_highlight") {
          tl.style = normalizeTlStyle(Object.assign({}, tl.style || {}));
          if (key === "__style_font") {
            tl.style.font_name = v; tl.style.font = v;
          } else if (key === "__style_size") {
            tl.style.font_size = v; tl.style.size = v;
          } else if (key === "__style_primary") {
            tl.style.primary_color = v; tl.style.primary = v;
          } else if (key === "__style_highlight") {
            tl.style.highlight_color = v; tl.style.highlight = v;
          }
          syncStyleToCaptionLook(tl.style);
          // Rebuild so the live preview chip updates.
          renderProps();
          updateStageCompositor();
          scheduleSave();
          return;
        } else if (key.indexOf(".") >= 0) {
          // Nested key like "ken_burns.enabled" — create the object if needed.
          const [obj, field] = key.split(".");
          if (!c[obj] || typeof c[obj] !== "object") c[obj] = {};
          c[obj][field] = v;
        } else {
          c[key] = v;
        }
        // Keep trims sane.
        if (key === "in" && c.out <= c.in) c.out = c.in + 0.2;
        if (key === "out" && c.out <= (c.in || 0)) c.out = (c.in || 0) + 0.2;

        const outSpan = wrap.querySelector(`[data-out="${key}"]`);
        if (outSpan) outSpan.textContent = (+v).toFixed(2);
        // Checkboxes / selects can add or remove sub-controls, so rebuild the
        // whole panel. Text/number/range edits only redraw the lanes so the
        // focused control isn't torn out mid-edit.
        const structural = inp.type === "checkbox" || inp.tagName === "SELECT";
        if (structural) renderTimeline(); else renderTracks();
        updateStageCompositor();
        scheduleSave();
      };
      inp.addEventListener(inp.tagName === "SELECT" || inp.type === "checkbox" || inp.type === "color" ? "change" : "input", handler);
    });
    const del = wrap.querySelector('[data-act="del"]');
    if (del) del.onclick = () => deleteClip(track, c.id);
    const openCap = wrap.querySelector('[data-act="open-captions"]');
    if (openCap) openCap.onclick = () => jumpLookSection("captions");
    const et = wrap.querySelector('[data-act="edittext"]');
    if (et) et.onclick = () => { setLeftTab("transcript"); renderTranscript(c); };
    const setin = wrap.querySelector('[data-act="setin"]');
    if (setin) setin.onclick = () => {
      pushHistory();
      const t = $("tlPreviewVideo").currentTime || 0;
      c.in = Math.max(0, Math.min(t, (c.out || 0) - 0.2));
      renderTimeline(); scheduleSave();
    };
    const setout = wrap.querySelector('[data-act="setout"]');
    if (setout) setout.onclick = () => {
      pushHistory();
      const t = $("tlPreviewVideo").currentTime || 0;
      c.out = Math.min(c._max || 1e9, Math.max(t, (c.in || 0) + 0.2));
      renderTimeline(); scheduleSave();
    };
    const scrub2 = wrap.querySelector('[data-act="split-scrub"]');
    if (scrub2) scrub2.onclick = () => openSplitScrubDialog(c);
    const sfx = wrap.querySelector('[data-act="suggestfx"]');
    if (sfx) sfx.onclick = () => suggestEffectsFor(c, sfx);
    const restyle = wrap.querySelector('[data-act="restyle"]');
    if (restyle) restyle.onclick = () => {
      const pack = (wrap.querySelector("[data-restyle-pack]") || {}).value || "pulse";
      const intensity = (wrap.querySelector("[data-restyle-intensity]") || {}).value || "med";
      restyleSelectedShot(pack, intensity);
    };
    wrap.querySelectorAll('[data-act="ovlayout"]').forEach((btn) => {
      btn.onclick = () => applyOverlayLayout(c, btn.dataset.layout);
    });
    const ovLibReplace = wrap.querySelector('[data-act="ov-lib-replace"]');
    if (ovLibReplace) {
      ovLibReplace.onclick = () => {
        beginOverlayReplaceMode(c.id);
        if (typeof window.openMobileTimelinePanel === "function") {
          try { window.openMobileTimelinePanel("media"); } catch (e) { /* optional */ }
        }
      };
    }
    const ovAiGen = wrap.querySelector('[data-act="ov-ai-gen"]');
    if (ovAiGen) {
      ovAiGen.onclick = async () => {
        const promptEl = wrap.querySelector("[data-ov-ai-prompt]");
        const statusEl = wrap.querySelector("[data-ov-ai-status]");
        const prompt = ((promptEl && promptEl.value) || c.keyword || "").trim();
        if (prompt.length < 2) {
          if (statusEl) statusEl.textContent = "Enter a short prompt first.";
          return;
        }
        beginOverlayReplaceMode(c.id, { openMedia: false });
        ovAiGen.disabled = true;
        ovAiGen.textContent = "Generating…";
        if (statusEl) statusEl.textContent = "Calling Gemini…";
        try {
          const res = await fetch("/broll/generate-ai", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt, keyword: prompt.slice(0, 40) }),
          });
          const data = await res.json();
          if (!res.ok || data.error) throw new Error(data.error || ("HTTP " + res.status));
          const ok = replaceSelectedOverlayAsset(data.asset_id, {
            source: "gemini",
            keyword: data.keyword || prompt.slice(0, 40),
          });
          if (statusEl) statusEl.textContent = ok ? "Replaced ✓" : "Replace failed.";
        } catch (e) {
          endOverlayReplaceMode();
          if (statusEl) statusEl.textContent = "Failed: " + (e.message || e);
        } finally {
          ovAiGen.disabled = !window._brollGeminiReady;
          ovAiGen.textContent = window._brollGeminiReady ? "✨ Generate + replace" : "Set GEMINI_API_KEY";
        }
      };
    }
  }

  /** Push Timeline style knobs into the Look form so both stay canonical. */
  function syncStyleToCaptionLook(style) {
    style = normalizeTlStyle(style || {});
    try {
      if (typeof window.applyStyle === "function") window.applyStyle(style);
      else if (typeof applyStyle === "function") applyStyle(style);
    } catch (_) { /* optional */ }
    try {
      if (typeof window.updateFontPreview === "function") window.updateFontPreview();
    } catch (_) { /* optional */ }
  }

  // ---- AI camera moves -------------------------------------------------
  const FX_LABEL = {
    punch_zoom: "⚡ Punch zoom",
    zoom_1_5: "🔎 1.5× zoom hold",
    zoom_2x: "🔍 2× zoom hold",
    ken_burns: "🎞 Ken Burns",
    split_screen: "⬓ Split screen",
  };

  async function suggestEffectsFor(clip, btn) {
    const list = $("tlFxList");
    if (!clip.source_job_id) {
      if (list) list.innerHTML = `<p class="muted" style="font-size:.72rem">This clip has no transcribed source to read.</p>`;
      return;
    }
    btn.disabled = true;
    if (list) list.innerHTML = `<p class="muted" style="font-size:.72rem">Reading the transcript…</p>`;
    try {
      const res = await fetch("/suggest-effects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: clip.source_job_id, max_effects: 6 }),
      });
      // A Flask error/404 page is HTML, and res.json() then fails on "<" with
      // a parse error that says nothing about what went wrong.
      if (!(res.headers.get("content-type") || "").includes("application/json")) {
        throw new Error(res.status === 404
          ? "The server doesn't have /suggest-effects — restart the app after pulling."
          : `Server returned ${res.status} instead of JSON.`);
      }
      const data = await res.json();
      if (data.error) throw new Error(data.error);

      // Suggestions are in source time; only those inside this clip's trim can
      // be applied to it.
      const cin = clip.in || 0, cout = clip.out || 0;
      const usable = (data.effects || []).filter(
        (e) => e.start_time >= cin - 0.01 && e.end_time <= cout + 0.01
      );
      renderFxSuggestions(clip, data.effects || [], usable);
    } catch (e) {
      if (list) list.innerHTML = `<p class="muted" style="font-size:.72rem;color:#ff8a8a">${e.message}</p>`;
    } finally {
      btn.disabled = false;
    }
  }

  function renderFxSuggestions(clip, all, usable) {
    const list = $("tlFxList");
    if (!list) return;
    if (!all.length) {
      list.innerHTML = `<p class="muted" style="font-size:.72rem">No moments stood out — the transcript may be too short.</p>`;
      return;
    }
    const outside = all.length - usable.length;
    let html = "";
    usable.forEach((e, i) => {
      const dur = (e.end_time - e.start_time).toFixed(1);
      html += `<div class="tl-fx-sug" style="border:1px solid #2c3240;border-radius:8px;padding:8px;margin-bottom:6px">
        <div style="display:flex;align-items:center;gap:6px;justify-content:space-between">
          <strong style="font-size:.78rem">${FX_LABEL[e.type] || e.type}</strong>
          <span class="muted" style="font-size:.7rem">${e.start_time.toFixed(1)}s · ${dur}s</span>
        </div>
        ${e.quote ? `<p class="muted" style="font-size:.7rem;margin:4px 0 0">“${e.quote}”</p>` : ""}
        <p class="muted" style="font-size:.7rem;margin:4px 0 6px">${e.reason || ""}</p>
        <button class="btn btn-secondary" data-fx="${i}" style="font-size:.74rem;padding:3px 8px">Apply</button>
      </div>`;
    });
    if (outside) {
      html += `<p class="muted" style="font-size:.7rem">${outside} more fall outside this clip's trim.</p>`;
    }
    list.innerHTML = html;
    list.querySelectorAll("[data-fx]").forEach((b) => {
      b.onclick = () => applyEffectSuggestion(clip, usable[parseInt(b.dataset.fx, 10)]);
    });
  }

  // Timed suggestions land on the Effects lane (no Main split required).
  function applyEffectSuggestion(clip, fx) {
    if (!fx) return;
    const idx = tl.tracks.main.findIndex((m) => m.id === clip.id);
    if (idx < 0) return;
    const clipStart = mainStart(idx);
    const outStart = clipStart + sourceTimeToLocalOutput(clip, fx.start_time);
    const outEnd = clipStart + sourceTimeToLocalOutput(clip, fx.end_time);
    const dur = Math.max(0.3, outEnd - outStart);
    addEffectClip(fx.type || "punch_zoom", {
      start: outStart,
      out: dur,
      intensity: fx.intensity || "med",
      direction: fx.direction || "in",
      anchor: fx.anchor || null,
      quote: fx.quote || null,
      reason: fx.reason || null,
    });
  }

  // ---- Text-based editing: strike out words to cut them from the clip ----
  async function openTextEditor(clip) {
    if (!clip.source_job_id) {
      alert("Text editing only works on clips from a transcribed video.");
      return;
    }
    let words = [];
    try {
      const s = await api("/status/" + clip.source_job_id);
      words = (s.words || []).filter((w) =>
        Number(w.end) > (clip.in || 0) && Number(w.start) < (clip.out || 1e9));
    } catch (e) {
      alert("Couldn't load transcript: " + e.message);
      return;
    }
    if (!words.length) {
      alert("No transcript words found in this clip's range. Transcribe the source video first.");
      return;
    }

    // Which words are currently cut, derived from existing clip.cuts.
    const cut = new Set();
    (clip.cuts || []).forEach(([cs, ce]) => {
      words.forEach((w, i) => {
        if (Number(w.start) >= cs - 0.01 && Number(w.end) <= ce + 0.01) cut.add(i);
      });
    });

    const back = document.createElement("div");
    back.className = "tl-modal-back";
    back.innerHTML = `
      <div class="tl-modal">
        <div class="tl-modal-head">
          <strong>✂️ Edit transcript</strong>
          <span class="muted">Click a word to strike it out — struck words are removed from the video.</span>
        </div>
        <div class="tl-modal-words" id="tlWords"></div>
        <div class="tl-modal-foot">
          <span class="muted" id="tlCutInfo"></span>
          <span style="flex:1"></span>
          <button class="btn btn-secondary" data-x="cancel">Cancel</button>
          <button class="btn btn-primary" data-x="apply" style="background:linear-gradient(135deg,#9785ff,#6c5cff);color:#fff">Apply cuts</button>
        </div>
      </div>`;
    document.body.appendChild(back);
    const wordsEl = back.querySelector("#tlWords");
    const info = back.querySelector("#tlCutInfo");

    function draw() {
      wordsEl.innerHTML = "";
      words.forEach((w, i) => {
        const sp = document.createElement("span");
        sp.className = "tl-word" + (cut.has(i) ? " cut" : "");
        sp.textContent = w.word;
        sp.onclick = () => { cut.has(i) ? cut.delete(i) : cut.add(i); draw(); };
        wordsEl.appendChild(sp);
      });
      const secs = [...cut].reduce((a, i) => a + (Number(words[i].end) - Number(words[i].start)), 0);
      info.textContent = `${cut.size} word(s) struck · ~${secs.toFixed(1)}s removed`;
    }
    draw();

    function close() { back.remove(); }
    back.querySelector('[data-x="cancel"]').onclick = close;
    back.onclick = (e) => { if (e.target === back) close(); };
    back.querySelector('[data-x="apply"]').onclick = () => {
      // Merge contiguous struck words into [start,end] cut ranges.
      const ranges = [];
      let run = null;
      words.forEach((w, i) => {
        if (cut.has(i)) {
          if (!run) run = [Number(w.start), Number(w.end)];
          else run[1] = Number(w.end);
        } else if (run) { ranges.push(run); run = null; }
      });
      if (run) ranges.push(run);
      pushHistory();
      clip.cuts = ranges;
      close();
      renderTimeline();
      scheduleSave();
    };
  }

  function deleteClip(track, id) {
    if (!tl) return;
    pushHistory();
    // Ripple: dropping a Main clip also drops items anchored exclusively to it.
    // Remaining Main clips close the gap automatically (starts are cumulative).
    if (track === "main") {
      ["overlay", "effects", "text", "music"].forEach((k) => {
        tl.tracks[k] = (tl.tracks[k] || []).filter((c) => c.anchor !== id);
      });
    }
    tl.tracks[track] = (tl.tracks[track] || []).filter((c) => c.id !== id);
    if (selected && selected.id === id) selected = null;
    applyAnchors();
    renderTimeline();
    scheduleSave();
  }

  // ---- Drag (move + resize) on the timeline ----
  let drag = null;

  function onTimelineMouseDown(e) {
    const clipEl = e.target.closest(".tl-clip");
    if (!clipEl || clipEl.classList.contains("tl-clip-pending")) return;
    const track = clipEl.dataset.track;
    const id = clipEl.dataset.id;
    selectClip(track, id, { preserveSeek: true });
    // Jump playhead to the clicked time on the clip (not always clip.in).
    seekToOutputTime(outputTimeFromClientX(e.clientX), { quietProps: true, keepSelection: true });
    const handle = e.target.closest(".tl-clip-handle");
    const c = findClip(track, id);
    if (!c) return;
    pushHistory();
    const mainIdx = track === "main" ? tl.tracks.main.findIndex((x) => x.id === id) : -1;
    drag = {
      track, id, c,
      startX: e.clientX,
      mode: handle ? ("resize-" + handle.dataset.side) : "move",
      origIn: c.in || 0, origOut: c.out || 0, origStart: c.start || 0,
      origLeft: parseFloat(clipEl.style.left) || 0,
      mainIdx,
      mainStart0: mainIdx >= 0 ? mainStart(mainIdx) : 0,
    };
    e.preventDefault();
  }

  function onMouseMove(e) {
    if (!drag) return;
    const dx = e.clientX - drag.startX;
    const dt = dx / PPS;
    const c = drag.c;
    const max = c._max || 1e9;
    const exclude = { track: drag.track, id: drag.id };

    if (drag.mode === "move") {
      if (drag.track === "main") {
        // Reorder by where the cursor lands among main clips.
        reorderMainByX(drag.id, drag.origLeft + dx);
      } else {
        c.start = snapTime(Math.max(0, drag.origStart + dt), exclude);
      }
    } else if (drag.mode === "resize-left") {
      if (drag.track === "main" && drag.mainIdx >= 0) {
        // Snap the output start edge; Main clips always begin at mainStart.
        // Left-resize changes source `in` (and thus duration) — later clips ripple.
        let ni = Math.min(Math.max(0, drag.origIn + dt), drag.origOut - 0.2);
        const newDur = drag.origOut - ni;
        const endT = drag.mainStart0 + newDur;
        const snappedEnd = snapTime(endT, exclude);
        ni = Math.min(Math.max(0, drag.origOut - Math.max(0.2, snappedEnd - drag.mainStart0)), drag.origOut - 0.2);
        c.in = ni;
      } else if (drag.track === "text" || drag.track === "effects") {
        // Duration-based clips: `out` is duration. Left edge moves start + shortens.
        const origDur = Math.max(0.2, drag.origOut || clipDuration(c));
        const endT = drag.origStart + origDur;
        let newStart = snapTime(Math.max(0, drag.origStart + dt), exclude);
        newStart = Math.min(newStart, endT - 0.2);
        c.start = newStart;
        c.out = Math.max(0.2, endT - newStart);
        if (c.in) c.in = 0;
      } else {
        let ni = Math.min(Math.max(0, drag.origIn + dt), drag.origOut - 0.2);
        if (ni < 0) ni = 0;
        c.in = ni;
        if (drag.track !== "text") {
          // overlay/music: trimming the head shifts visible start too
          c.start = snapTime(Math.max(0, drag.origStart + (ni - drag.origIn)), exclude);
        }
      }
    } else if (drag.mode === "resize-right") {
      if (drag.track === "main" && drag.mainIdx >= 0) {
        let no = Math.max(drag.origIn + 0.2, drag.origOut + dt);
        no = Math.min(no, max);
        const endT = drag.mainStart0 + (no - drag.origIn);
        const snappedEnd = snapTime(endT, exclude);
        no = Math.min(max, Math.max(drag.origIn + 0.2, drag.origIn + (snappedEnd - drag.mainStart0)));
        c.out = no;
      } else if (drag.track === "text" || drag.track === "effects") {
        let no = Math.max(0.2, drag.origOut + dt);
        const endT = (c.start || 0) + no;
        const snappedEnd = snapTime(endT, exclude);
        c.out = Math.max(0.2, snappedEnd - (c.start || 0));
      } else {
        let no = Math.max(drag.origIn + 0.2, drag.origOut + dt);
        no = Math.min(no, max);
        const endT = (c.start || 0) + (no - (c.in || 0));
        const snappedEnd = snapTime(endT, exclude);
        no = Math.min(max, Math.max((c.in || 0) + 0.2, (c.in || 0) + (snappedEnd - (c.start || 0))));
        c.out = no;
      }
    }
    // Live ripple for anchored overlays/titles/music while Main duration changes.
    if (drag.track === "main") applyAnchors();
    renderTracks();
    updateStageCompositor();
  }

  function reorderMainByX(id, leftPx) {
    const arr = tl.tracks.main;
    const idx = arr.findIndex((c) => c.id === id);
    if (idx < 0) return;
    const centerT = (leftPx + clipDuration(arr[idx]) * PPS / 2) / PPS;
    // Determine target index by cumulative durations.
    let acc = 0, target = 0;
    for (let i = 0; i < arr.length; i++) {
      if (i === idx) continue;
      const d = clipDuration(arr[i]);
      if (centerT > acc + d / 2) target = (i < idx ? i + 1 : i);
      acc += d;
    }
    if (target !== idx) {
      const [moved] = arr.splice(idx, 1);
      arr.splice(target, 0, moved);
    }
  }

  function onMouseUp() {
    if (drag) {
      const tr = drag.track, c = drag.c;
      drag = null;
      if (tr === "main") applyAnchors();   // Main moved/trimmed → reflow anchored items
      else if (c) reanchor(c);             // overlay/title/music/effects → re-pin to where it landed
      renderTimeline();                    // redraw lanes + props with final values
      scheduleSave();
    }
  }

  // ---- Project lifecycle ----
  async function newProject(opts) {
    opts = opts || {};
    try {
      const body = {
        label: opts.label || (opts.canvas === "16x9" ? "Long-form edit" : "Timeline edit"),
      };
      if (opts.canvas) body.canvas = opts.canvas;
      const data = await api("/timeline/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await openProject(data.job_id);
      if (opts.canvas && tl) {
        tl.canvas = opts.canvas;
        if ($("tlCanvas")) $("tlCanvas").value = tl.canvas;
        applyStage();
      }
      await loadProjects();
    } catch (e) {
      alert("Could not create project: " + e.message);
    }
  }

  async function openProject(jobId) {
    const data = await api("/timeline/" + jobId);
    const d = data.timeline || {};
    tl = {
      job_id: jobId,
      label: (data.filename || "Timeline edit").replace(/\.mp4$/, ""),
      canvas: d.canvas || "9x16",
      fit: d.fit || "cover",
      fps: d.fps || 30,
      bg: d.bg || "#000000",
      logo: d.logo || null,
      style: d.style ? normalizeTlStyle(d.style) : null,
      audio: d.audio || null,
      sfx_overlays: d.sfx_overlays !== false,
      ai_edit: d.ai_edit || null,
      speaker_colors: (() => {
        const sc = d.speaker_colors || {};
        return {
          SPEAKER_00: sc.SPEAKER_00 || sc.Host || "#FFD700",
          SPEAKER_01: sc.SPEAKER_01 || sc.Guest || "#00E5FF",
          ...sc,
        };
      })(),
      headline_banner: d.headline_banner || null,
      track_states: d.track_states || {
        main: { mute: false, solo: false, lock: false },
        overlay: { mute: false, solo: false, lock: false },
        effects: { mute: false, solo: false, lock: false },
        text: { mute: false, solo: false, lock: false },
        music: { mute: false, solo: false, lock: false },
      },
      tracks: {
        main: (d.tracks && d.tracks.main) || [],
        overlay: (d.tracks && d.tracks.overlay) || [],
        effects: (d.tracks && d.tracks.effects) || [],
        text: (d.tracks && d.tracks.text) || [],
        music: (d.tracks && d.tracks.music) || [],
      },
    };
    // Restore _max trims by probing main/overlay sources lazily.
    selected = null;
    clearHistory();
    $("tlLabel").value = tl.label;
    $("tlCanvas").value = tl.canvas;
    $("tlFit").value = tl.fit;
    setSaveState("Saved ✓");
    applyStage();
    renderTimeline();
    refreshMaxTrims();
  }

  async function refreshMaxTrims() {
    const jobs = new Set();
    tl.tracks.main.forEach((c) => c.source_job_id && jobs.add(c.source_job_id));
    tl.tracks.overlay.forEach((c) => c.source_job_id && jobs.add(c.source_job_id));
    for (const j of jobs) {
      try {
        const d = await getSourceDuration(j);
        tl.tracks.main.forEach((c) => { if (c.source_job_id === j && !c._max) c._max = d; });
        tl.tracks.overlay.forEach((c) => { if (c.source_job_id === j && !c._max) c._max = d; });
      } catch (e) {}
    }
    // Music clips: fill _max from the asset list so the waveform slices right.
    tl.tracks.music.forEach((c) => {
      if (c._max || !c.asset_id) return;
      const a = assets.find((x) => x.asset_id === c.asset_id);
      if (a && a.duration) c._max = a.duration;
    });
    renderTimeline();
  }

  // ---- Render ----
  async function renderTimelineVideo() {
    if (!tl || !tl.tracks.main.length) {
      alert("Add at least one clip to the Main track first.");
      return null;
    }
    const btn = $("tlRenderBtn");
    if (btn) btn.disabled = true;
    setRenderStatus("Queued…");
    try {
      // Caption look wins: refresh Timeline style from it before baking ASS.
      pullCaptionLookOntoTimeline(null, { quiet: true });
      await saveNow();
      await api("/timeline/render", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: tl.job_id, timeline: serialize() }),
      });
      return await pollRender();
    } catch (e) {
      setRenderStatus("Error: " + e.message);
      if (btn) btn.disabled = false;
      alert("Render failed: " + (e.message || e));
      return null;
    }
  }
  window.renderTimelineVideo = renderTimelineVideo;
  window.timelineHasMainClips = function () {
    return !!(tl && tl.tracks && tl.tracks.main && tl.tracks.main.length);
  };

  function overlayKeywordsCsv() {
    if (!tl) return "";
    const kws = [];
    (tl.tracks.overlay || []).forEach((c) => {
      const k = String(c.keyword || "").trim();
      if (!k) return;
      if (!kws.some((x) => x.toLowerCase() === k.toLowerCase())) kws.push(k);
    });
    return kws.join(", ");
  }

  function parsePolishRes(val) {
    // "1920x1080@60" → { width, height, fps }
    const m = String(val || "1920x1080@60").match(/^(\d+)x(\d+)@(\d+)$/);
    if (!m) return { width: 1920, height: 1080, fps: 60 };
    return { width: +m[1], height: +m[2], fps: +m[3] };
  }

  function collectPolishOptsFromForm() {
    const res = parsePolishRes(($("tlPolishRes") && $("tlPolishRes").value) || "1920x1080@60");
    const kwRaw = ($("tlPolishKeywords") && $("tlPolishKeywords").value || "").trim();
    const keywords = kwRaw
      ? kwRaw.split(/[,;]+/).map((s) => s.trim()).filter(Boolean)
      : undefined;
    return {
      pacing: ($("tlPolishPacing") && $("tlPolishPacing").value) || "fast",
      broll_mode: ($("tlPolishBrollMode") && $("tlPolishBrollMode").value) || "pip",
      silence_engine: ($("tlPolishSilenceEngine") && $("tlPolishSilenceEngine").value) || "auto",
      composite_engine: ($("tlPolishComposite") && $("tlPolishComposite").value) || "ffmpeg",
      export_nle: !($("tlPolishNle") && !$("tlPolishNle").checked),
      face_reframe: !($("tlPolishFace") && !$("tlPolishFace").checked),
      cut_stumbles: !($("tlPolishStumbles") && !$("tlPolishStumbles").checked),
      lower_thirds: !!($("tlPolishLowerThirds") && $("tlPolishLowerThirds").checked),
      export_edl: !($("tlPolishEdl") && !$("tlPolishEdl").checked),
      keywords,
      width: res.width,
      height: res.height,
      fps: res.fps,
    };
  }

  function openPolishSheet(prefill) {
    const modal = $("tlPolishModal");
    if (!modal) {
      // Fallback if DOM missing — run with defaults / prefill
      return runTimelinePolish(prefill || {});
    }
    if ($("tlPolishKeywords") && !$("tlPolishKeywords").value) {
      $("tlPolishKeywords").value = overlayKeywordsCsv();
    }
    if (prefill && typeof prefill === "object") {
      if (prefill.pacing && $("tlPolishPacing")) $("tlPolishPacing").value = prefill.pacing;
      if (prefill.broll_mode && $("tlPolishBrollMode")) $("tlPolishBrollMode").value = prefill.broll_mode;
      if (prefill.face_reframe != null && $("tlPolishFace")) {
        $("tlPolishFace").checked = !!prefill.face_reframe;
      }
      if (prefill.cut_stumbles != null && $("tlPolishStumbles")) {
        $("tlPolishStumbles").checked = !!prefill.cut_stumbles;
      }
      if (prefill.lower_thirds != null && $("tlPolishLowerThirds")) {
        $("tlPolishLowerThirds").checked = !!prefill.lower_thirds;
      }
      if (prefill.export_edl != null && $("tlPolishEdl")) {
        $("tlPolishEdl").checked = !!prefill.export_edl;
      }
      if (prefill.keywords && $("tlPolishKeywords")) {
        $("tlPolishKeywords").value = Array.isArray(prefill.keywords)
          ? prefill.keywords.join(", ")
          : String(prefill.keywords);
      }
      if (prefill.width && prefill.height && prefill.fps && $("tlPolishRes")) {
        const key = `${prefill.width}x${prefill.height}@${prefill.fps}`;
        const opt = $("tlPolishRes").querySelector(`option[value="${key}"]`);
        if (opt) $("tlPolishRes").value = key;
      }
    }
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
  }

  function closePolishSheet() {
    const modal = $("tlPolishModal");
    if (!modal) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  async function runTimelinePolish(opts) {
    opts = opts || {};
    if (!tl || !tl.tracks.main.length) {
      alert("Add at least one Main clip (transcribed source) first.");
      return null;
    }
    const btn = $("tlPolishBtn");
    const runBtn = $("tlPolishRun");
    if (btn) btn.disabled = true;
    if (runBtn) runBtn.disabled = true;
    setRenderStatus("Polish queued…");
    try {
      await saveNow();
      const body = {
        job_id: tl.job_id,
        timeline: serialize(),
        pacing: opts.pacing || "fast",
        broll_mode: opts.broll_mode || "pip",
        face_reframe: opts.face_reframe !== false,
        cut_stumbles: opts.cut_stumbles !== false,
        lower_thirds: opts.lower_thirds === true,
        export_edl: opts.export_edl !== false,
        silence_engine: opts.silence_engine || "auto",
        composite_engine: opts.composite_engine || "ffmpeg",
        export_nle: opts.export_nle !== false,
        width: opts.width || 1920,
        height: opts.height || 1080,
        fps: opts.fps || 60,
      };
      if (opts.keywords && opts.keywords.length) body.keywords = opts.keywords;

      const endpoints = ["/polish/run", "/timeline/polish"];
      let res = null;
      let lastErr = null;
      for (const ep of endpoints) {
        try {
          res = await api(ep, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          if (ep !== endpoints[0]) {
            console.warn("[timeline] Polish started via legacy", ep);
          }
          break;
        } catch (err) {
          lastErr = err;
          const m = String((err && err.message) || err || "");
          // Try next endpoint only on routing failures.
          if (!/\b(404|405)\b/.test(m) && !/Method Not Allowed/i.test(m) && !/route missing/i.test(m)) {
            throw err;
          }
          console.warn("[timeline] Polish POST failed at", ep, m);
        }
      }
      if (!res) {
        throw new Error(
          (lastErr && lastErr.message ? lastErr.message + " — " : "") +
          "Polish backend routes are missing on the running Flask process. " +
          "In Replit: open Shell and run:  pkill -f 'python app.py'; pkill -f gunicorn; " +
          "then Stop+Start the Project workflow, hard-refresh, confirm console shows build-56+."
        );
      }

      closePolishSheet();
      const polishId = res.polish_id;
      setRenderStatus(
        `Polish running (${res.pacing || body.pacing})…`
        + (res.keywords && res.keywords.length ? ` · ${res.keywords.length} B-roll keys` : "")
        + (res.has_music ? " · music duck" : "")
      );
      return await pollPolish(polishId);
    } catch (e) {
      setRenderStatus("Polish error: " + e.message);
      if (btn) btn.disabled = false;
      if (runBtn) runBtn.disabled = false;
      throw e;
    }
  }
  window.runTimelinePolish = runTimelinePolish;
  window.openPolishSheet = openPolishSheet;
  window.closePolishSheet = closePolishSheet;

  function pollPolish(polishId) {
    return new Promise((resolve, reject) => {
      clearInterval(pollTimer);
      let failStreak = 0;
      pollTimer = setInterval(async () => {
        try {
          const s = await api("/polish/status/" + polishId);
          failStreak = 0;
          if (s.status === "done" && s.output) {
            clearInterval(pollTimer);
            const stats = s.stats || {};
            const srcDur = stats.source_duration_s;
            const cutDur = stats.cut_duration_s;
            const durNote = (srcDur != null && cutDur != null)
              ? ` · ${Number(srcDur).toFixed(1)}s → ${Number(cutDur).toFixed(1)}s`
              : "";
            setRenderStatus("Polish done ✓ — playing polished output (not timeline source)" + durNote);
            if ($("tlPolishBtn")) $("tlPolishBtn").disabled = false;
            if ($("tlPolishRun")) $("tlPolishRun").disabled = false;
            const v = $("tlPreviewVideo");
            if (v) {
              v.src = "/preview/" + s.output + "?t=" + Date.now();
              previewingOutput = true;
              const wrap = v.closest(".tl-preview");
              if (wrap) wrap.classList.add("has-video");
              v.load();
              v.play().catch(() => {});
            }
            // Do NOT call showExportDone() — that jumps to Ingest and makes
            // Timeline play the unmodified Main source again ("nothing changed").
            alert(
              "Polish finished.\n\n" +
              "The Timeline preview is now the polished MP4 (not your Main source).\n" +
              (durNote ? ("Length change:" + durNote + "\n") : "") +
              "Download: /download/" + s.output + "\n\n" +
              "Note: scrubbing the timeline or Preview cut switches back to the original source. " +
              "Captions still need ▶ Render."
            );
            resolve(s);
          } else if (s.status === "error") {
            clearInterval(pollTimer);
            setRenderStatus("Polish error: " + (s.error || "failed"));
            if ($("tlPolishBtn")) $("tlPolishBtn").disabled = false;
            if ($("tlPolishRun")) $("tlPolishRun").disabled = false;
            reject(new Error(s.error || "polish failed"));
          } else {
            setRenderStatus(`Polish ${s.status || "working"}… ${s.progress || 0}%`);
          }
        } catch (e) {
          failStreak += 1;
          setRenderStatus("Polish reconnecting… (" + failStreak + ")");
          if (failStreak >= 12) {
            clearInterval(pollTimer);
            setRenderStatus("Polish error: " + e.message);
            if ($("tlPolishBtn")) $("tlPolishBtn").disabled = false;
            if ($("tlPolishRun")) $("tlPolishRun").disabled = false;
            reject(e);
          }
        }
      }, 1200);
    });
  }

  function pollRender() {
    return new Promise((resolve, reject) => {
      clearInterval(pollTimer);
      let failStreak = 0;
      pollTimer = setInterval(async () => {
        try {
          const s = await api("/status/" + tl.job_id);
          failStreak = 0;
          if (typeof window.showExportProgressUpdate === "function") {
            window.showExportProgressUpdate(s);
          }
          if (s.status === "done" && s.output) {
            clearInterval(pollTimer);
            setRenderStatus("Done ✓ — download ready on Ingest");
            if ($("tlRenderBtn")) $("tlRenderBtn").disabled = false;
            const v = $("tlPreviewVideo");
            if (v) {
              v.src = "/preview/" + s.output + "?t=" + Date.now();
              previewingOutput = true;   // preview is now the full output (1:1 playhead)
              const wrap = v.closest(".tl-preview");
              if (wrap) wrap.classList.add("has-video");
              v.load();
              v.play().catch(() => {});
            }
            if (typeof window.showExportDone === "function") {
              window.showExportDone(s.output, {
                jobId: tl.job_id,
                stayOnTab: true,
                timeline: true,
              });
            } else if (typeof window.triggerVideoDownload === "function") {
              window.triggerVideoDownload(s.output, { timeline: true });
            }
            resolve(s);
          } else if (s.status === "error") {
            clearInterval(pollTimer);
            setRenderStatus("Error: " + (s.error || "render failed"));
            if ($("tlRenderBtn")) $("tlRenderBtn").disabled = false;
            reject(new Error(s.error || "render failed"));
          } else {
            setRenderStatus(`${s.status || "working"}… ${s.progress || 0}%`);
          }
        } catch (e) {
          failStreak += 1;
          setRenderStatus("Reconnecting… (" + failStreak + ")");
          // Long encodes often hitch the proxy briefly — retry before giving up.
          if (failStreak >= 12) {
            clearInterval(pollTimer);
            setRenderStatus("Error: " + e.message);
            if ($("tlRenderBtn")) $("tlRenderBtn").disabled = false;
            reject(e);
          }
        }
      }, 1500);
    });
  }

  let progressHideTimer = null;
  function updateProgressBar(s) {
    const bar = $("tlProgressBar");
    const fill = $("tlProgressFill");
    const label = $("tlProgressLabel");
    if (!bar || !fill || !label) return;
    if (progressHideTimer) { clearTimeout(progressHideTimer); progressHideTimer = null; }

    const msg = String(s || "");
    label.textContent = msg;

    const pctMatch = msg.match(/(\d+)\s*%/);
    if (pctMatch) {
      fill.style.width = Math.max(0, Math.min(100, Number(pctMatch[1]))) + "%";
    }

    if (!msg) {
      bar.classList.add("hidden");
      fill.style.width = "0%";
      fill.classList.remove("is-error");
      return;
    }

    const isDone = /done|✓/i.test(msg);
    const isError = /error|fail/i.test(msg);
    if (isDone || isError) {
      bar.classList.remove("hidden");
      fill.classList.toggle("is-error", isError && !isDone);
      if (isDone && !pctMatch) fill.style.width = "100%";
      progressHideTimer = setTimeout(() => {
        bar.classList.add("hidden");
        fill.style.width = "0%";
        fill.classList.remove("is-error");
        progressHideTimer = null;
      }, 2200);
      return;
    }

    if (/%|queued|working|polish|render|encoding|stitch/i.test(msg)) {
      bar.classList.remove("hidden");
      fill.classList.remove("is-error");
      if (!pctMatch) fill.style.width = fill.style.width && fill.style.width !== "0%" ? fill.style.width : "6%";
      return;
    }

    bar.classList.add("hidden");
  }

  function setRenderStatus(s) {
    const el = $("tlRenderStatus");
    if (el) el.textContent = s;
    updateProgressBar(s);
  }

  // ---- Asset upload ----
  async function uploadAsset(file) {
    const fd = new FormData();
    fd.append("file", file);
    setSaveState("Uploading asset…");
    try {
      await api("/upload-asset", { method: "POST", body: fd });
      await loadAssets();
      setSaveState("Saved ✓");
      try {
        if (typeof window.refreshSoundSheetIfOpen === "function") {
          window.refreshSoundSheetIfOpen();
        }
      } catch (e) {}
    } catch (e) {
      alert("Asset upload failed: " + e.message);
      setSaveState("");
    }
  }

  // If Replit served a stale index.html (new JS, old DOM), inject Polish UI
  // so the feature still appears without requiring a perfect hard-refresh.
  function ensurePolishDom() {
    const renderBtn = $("tlRenderBtn");
    if (!$("tlPolishBtn") && renderBtn && renderBtn.parentNode) {
      const btn = document.createElement("button");
      btn.id = "tlPolishBtn";
      btn.type = "button";
      btn.className = "btn btn-secondary tl-tipbtn tl-polish-btn";
      btn.title = "Open Polish options — silence cut, jump-cut, B-roll, audio master";
      btn.textContent = "✨ Polish";
      renderBtn.parentNode.insertBefore(btn, renderBtn);
      console.warn("[timeline] injected #tlPolishBtn (HTML was stale — restart Flask after git pull)");
    }
    const co = $("tlCoEditorBtn");
    if (!$("tlPolishChip") && co && co.parentNode) {
      const chip = document.createElement("button");
      chip.id = "tlPolishChip";
      chip.type = "button";
      chip.className = "tl-chip-btn tl-chip-polish";
      chip.title = "Polish cut — silence, jump-cut, B-roll, audio master";
      chip.textContent = "✨ Polish";
      if (co.nextSibling) co.parentNode.insertBefore(chip, co.nextSibling);
      else co.parentNode.appendChild(chip);
      console.warn("[timeline] injected #tlPolishChip (HTML was stale)");
    }
    if ($("tlPolishModal")) return;
    const modal = document.createElement("div");
    modal.id = "tlPolishModal";
    modal.className = "ai-edit-modal hidden";
    modal.setAttribute("aria-hidden", "true");
    modal.innerHTML = `
      <div class="ai-edit-modal-card" style="max-width:520px">
        <div class="ai-edit-modal-head">
          <strong>✨ Polish cut</strong>
          <span class="muted" style="font-size:.78rem">Silence · jump-cut · B-roll · audio master</span>
          <button type="button" id="tlPolishClose" class="tl-chip-btn" style="margin-left:auto" aria-label="Close">✕</button>
        </div>
        <div class="ai-edit-modal-body">
          <p class="muted" style="font-size:.78rem;line-height:1.4;margin:0 0 12px">
            Rough polish on the primary Main source — jump zooms every ~3s, face punch, grade.
            Does not replace Timeline Render.
          </p>
          <div class="row"><label style="min-width:110px" for="tlPolishPacing">Pacing</label>
            <select id="tlPolishPacing" style="flex:1">
              <option value="fast" selected>Fast — punchy zooms</option>
              <option value="informative">Informative</option>
              <option value="cinematic">Cinematic</option>
            </select></div>
          <div class="row"><label style="min-width:110px" for="tlPolishBrollMode">B-roll</label>
            <select id="tlPolishBrollMode" style="flex:1">
              <option value="pip" selected>PiP</option>
              <option value="center">Center</option>
            </select></div>
          <div class="row"><label style="min-width:110px" for="tlPolishKeywords">Keywords</label>
            <input type="text" id="tlPolishKeywords" maxlength="400" style="flex:1;padding:8px 10px;background:#10131d;border:1px solid #3b4252;color:#fff;border-radius:8px"></div>
          <div class="row"><label style="min-width:110px" for="tlPolishRes">Output</label>
            <select id="tlPolishRes" style="flex:1">
              <option value="1920x1080@60" selected>1080p60</option>
              <option value="1920x1080@30">1080p30</option>
              <option value="1080x1920@60">9:16 1080</option>
            </select></div>
          <div class="row"><label style="min-width:110px" for="tlPolishSilenceEngine">Silence</label>
            <select id="tlPolishSilenceEngine" style="flex:1">
              <option value="auto" selected>Auto (Auto-Editor / FFmpeg)</option>
              <option value="auto-editor">Auto-Editor</option>
              <option value="ffmpeg">FFmpeg</option>
            </select></div>
          <div class="row"><label style="min-width:110px" for="tlPolishComposite">Overlays</label>
            <select id="tlPolishComposite" style="flex:1">
              <option value="ffmpeg" selected>FFmpeg</option>
              <option value="moviepy">MoviePy</option>
            </select></div>
          <div class="row"><label style="min-width:110px">Options</label>
            <div style="display:flex;flex-direction:column;gap:6px;font-size:.86rem">
              <label><input type="checkbox" id="tlPolishNle" checked> Kdenlive + Shotcut projects</label>
              <label><input type="checkbox" id="tlPolishFace" checked> Face reframe</label>
              <label><input type="checkbox" id="tlPolishStumbles" checked> Cut stumbles / retakes</label>
              <label><input type="checkbox" id="tlPolishLowerThirds"> Lower-thirds</label>
              <label><input type="checkbox" id="tlPolishEdl" checked> EDL export</label>
            </div></div>
        </div>
        <div class="ai-edit-modal-foot">
          <button type="button" class="btn btn-secondary" id="tlPolishCancel">Cancel</button>
          <button type="button" class="btn btn-primary" id="tlPolishRun"
            style="background:linear-gradient(135deg,#9785ff,#6c5cff);color:#fff">Run Polish</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
    console.warn("[timeline] injected #tlPolishModal (HTML was stale — restart the Replit workflow)");
  }

  // Null-safe event binding so one missing/stale element can never abort the
  // rest of the wiring (the old code threw on the first null and left the whole
  // editor dead — no drag, no buttons, nothing).
  function on(id, ev, fn) {
    const el = $(id);
    if (el) el[ev] = fn;
    else console.warn("[timeline] missing element:", id, "(stale index.html? hard-refresh)");
  }

  // ---- Clipboard & Copy / Paste / Duplicate ----
  let tlClipboard = null; // { track, data }

  function copySelectedClip() {
    if (!selected || !tl) return;
    const c = findClip(selected.track, selected.id);
    if (!c) return;
    tlClipboard = { track: selected.track, data: JSON.parse(JSON.stringify(c)) };
    setSaveState("Copied clip");
    if (window.StudioLogger) StudioLogger.clip("copied", `${selected.track}:${selected.id}`);
  }

  function pasteClip() {
    if (!tlClipboard || !tl) return;
    pushHistory();
    const track = tlClipboard.track;
    const c = JSON.parse(JSON.stringify(tlClipboard.data));
    c.id = uid();

    // Paste position: at playhead time
    const v = $("tlPreviewVideo");
    const pTime = v ? (v.currentTime || 0) : 0;

    if (track === "main") {
      tl.tracks.main.push(c);
    } else {
      c.start = pTime;
      reanchor(c);
      tl.tracks[track].push(c);
    }

    selectClip(track, c.id);
    renderTimeline();
    scheduleSave();
    if (window.StudioLogger) StudioLogger.clip("pasted", `${track}:${c.id}`);
  }

  function duplicateSelectedClip() {
    if (!selected || !tl) return;
    const c = findClip(selected.track, selected.id);
    if (!c) return;
    pushHistory();
    const track = selected.track;
    const dup = JSON.parse(JSON.stringify(c));
    dup.id = uid();

    if (track === "main") {
      const idx = tl.tracks.main.findIndex((m) => m.id === selected.id);
      if (idx >= 0) tl.tracks.main.splice(idx + 1, 0, dup);
      else tl.tracks.main.push(dup);
    } else {
      dup.start = (c.start || 0) + clipDuration(c) + 0.3;
      reanchor(dup);
      tl.tracks[track].push(dup);
    }

    selectClip(track, dup.id);
    renderTimeline();
    scheduleSave();
    if (window.StudioLogger) StudioLogger.clip("duplicated", `${track}:${dup.id}`);
  }

  // ---- Keyboard (Editor tab) ----
  function editorTabActive() {
    return !!document.querySelector('.main-tab.active[data-tab="editor"]');
  }

  function isTypingTarget(el) {
    if (!el) return false;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return true;
    if (el.isContentEditable) return true;
    return !!el.closest("[contenteditable='true']");
  }

  function nudgeSelected(dt) {
    if (!tl || !selected) return;
    const c = findClip(selected.track, selected.id);
    if (!c) return;
    pushHistory();
    if (selected.track === "main") {
      const max = c._max || 1e9;
      const dur = clipDuration(c);
      let ni = Math.max(0, Math.min((c.in || 0) + dt, max - dur));
      c.in = ni;
      c.out = ni + dur;
    } else {
      c.start = Math.max(0, (c.start || 0) + dt);
      reanchor(c);
    }
    renderTimeline();
    scheduleSave();
  }

  function onEditorKeyDown(e) {
    if (!initialized || !editorTabActive() || !tl) return;
    if (isTypingTarget(e.target)) return;
    const mod = e.metaKey || e.ctrlKey;
    const key = e.key;

    if (mod && key.toLowerCase() === "z") {
      e.preventDefault();
      if (e.shiftKey) redo(); else undo();
      return;
    }
    if (mod && key.toLowerCase() === "y") {
      e.preventDefault();
      redo();
      return;
    }
    if (mod && key.toLowerCase() === "c") {
      e.preventDefault();
      copySelectedClip();
      return;
    }
    if (mod && key.toLowerCase() === "x") {
      if (selected) {
        e.preventDefault();
        copySelectedClip();
        deleteClip(selected.track, selected.id);
        setSaveState("Cut clip");
      }
      return;
    }
    if (mod && key.toLowerCase() === "v") {
      e.preventDefault();
      pasteClip();
      return;
    }
    if (mod && key.toLowerCase() === "d") {
      e.preventDefault();
      duplicateSelectedClip();
      return;
    }
    if (key === " " || key === "Spacebar") {
      e.preventDefault();
      const v = $("tlPreviewVideo");
      if (!v) return;
      if (v.paused) {
        v.play().catch(() => {});
        startPreviewLoop();
      } else {
        v.pause();
      }
      return;
    }
    if (key === "Delete" || key === "Backspace") {
      if (selected) {
        e.preventDefault();
        deleteClip(selected.track, selected.id);
      }
      return;
    }
    if (key === "s" || key === "S") {
      e.preventDefault();
      splitAtPlayhead();
      return;
    }
    if (key === "+" || key === "=") {
      e.preventDefault();
      setZoom(4);
      return;
    }
    if (key === "-" || key === "_") {
      e.preventDefault();
      setZoom(-4);
      return;
    }
    if (key === "ArrowLeft" || key === "ArrowRight") {
      e.preventDefault();
      const dir = key === "ArrowLeft" ? -1 : 1;
      const step = e.shiftKey ? 1 : 0.1;
      if (selected) nudgeSelected(dir * step);
      else {
        const v = $("tlPreviewVideo");
        if (v) {
          try { v.currentTime = Math.max(0, (v.currentTime || 0) + dir * step); } catch (err) {}
          updatePlayhead();
        }
      }
      return;
    }
  }

  // ---- Init (lazy, when the Editor tab is first opened) ----
  let _initPromise = null;
  let _skipAutoOpenOnce = false;

  async function ensureInit(opts) {
    opts = opts || {};
    if (opts.skipAutoOpen) _skipAutoOpenOnce = true;
    if (initialized) {
      loadSources(); loadAssets(); loadProjects();
      return _initPromise || Promise.resolve();
    }
    if (_initPromise) return _initPromise;

    _initPromise = (async () => {
      initialized = true;
      console.log("[timeline] " + TL_BUILD + " initializing");

      try {
        ensurePolishDom();
        on("tlNewBtn", "onclick", newProject);
        on("tlDeleteProjectBtn", "onclick", () => deleteCurrentProject());
        on("tlProjectSelect", "onchange", (e) => { if (e.target.value) openProject(e.target.value); });
        on("tlLabel", "oninput", (e) => { if (tl) { tl.label = e.target.value; scheduleSave(); } });
        on("tlRenameSaveBtn", "onclick", async () => {
          if (!tl) return;
          const input = $("tlLabel");
          if (input) tl.label = input.value;
          clearTimeout(saveTimer);
          await saveNow();
          setSaveState("Name saved ✓");
        });
        on("tlCanvas", "onchange", (e) => { if (tl) { pushHistory(); tl.canvas = e.target.value; applyStage(); updateStageCompositor(); scheduleSave(); } });
        on("tlFit", "onchange", (e) => { if (tl) { pushHistory(); tl.fit = e.target.value; applyStage(); scheduleSave(); } });
        on("tlRenderBtn", "onclick", renderTimelineVideo);
        on("tlPolishBtn", "onclick", () => openPolishSheet());
        on("tlPolishChip", "onclick", () => openPolishSheet());
        on("tlPolishClose", "onclick", () => closePolishSheet());
        on("tlPolishCancel", "onclick", () => closePolishSheet());
        on("tlPolishRun", "onclick", () => {
          runTimelinePolish(collectPolishOptsFromForm()).catch(() => {});
        });
        const polishModal = $("tlPolishModal");
        if (polishModal) {
          polishModal.addEventListener("click", (e) => {
            if (e.target === polishModal) closePolishSheet();
          });
        }
        on("tlWorkspaceModalClose", "onclick", () => closeWorkspacePanel());
        const workspaceModal = $("tlWorkspaceModal");
        if (workspaceModal) {
          workspaceModal.addEventListener("click", (e) => {
            if (e.target === workspaceModal) closeWorkspacePanel();
          });
        }
        // Desktop default: keep preview + toolbar + timeline always visible;
        // Library / Captions / Audio / Clip props open as workspace-modal windows.
        if (!document.body.classList.contains("is-phone")) {
          document.body.classList.add("tl-workspace-focus");
        }
        on("tlMobilePanelBtn", "onclick", () => {
          // Phone tap is already handled by mobile.js's own listener (bottom
          // sheet); this covers the desktop workspace-focus case only.
          if (document.body.classList.contains("is-phone")) return;
          openWorkspacePanel("📚 Library", { mode: "left" });
          setLeftTab("media", { pin: true, openSheet: false });
        });
        on("tlPropsBtn", "onclick", () => {
          openWorkspacePanel(selected ? "✎ Clip properties" : "⚙ Project properties", { mode: "props" });
        });
        // Titles lane removed from the UI — + Title button no longer exists;
        // addTitle() is kept unwired so existing projects with text clips
        // still render correctly (see _tl_build_titles_ass).
        ensureEffectsChrome();
        on("tlAddEffectBtn", "onclick", () => openEffectPicker());
        on("tlPlaySeqBtn", "onclick", () => playSequencePreview());
        on("tlSplitBtn", "onclick", () => splitAtPlayhead());
        on("tlCopyBtn", "onclick", () => copySelectedClip());
        on("tlPasteBtn", "onclick", () => pasteClip());
        on("tlDupBtn", "onclick", () => duplicateSelectedClip());
        wireCaptionsToolbar();
        wireLibraryViewToggle();
        wireLibraryLaneDrop();
        mountCaptionLookIntoTimeline();
        // Keep Look available even if user never opens the tab yet — mount once.
        window.openTimelineLook = () => {
          if (typeof window.setActiveTab === "function") window.setActiveTab("editor");
          jumpLookSection("captions");
          if (typeof window.openMobileTimelinePanel === "function") {
            window.openMobileTimelinePanel("look");
          }
          selected = null;
          logoSelected = false;
          renderProps();
        };
        window.jumpTimelineLook = (which) => {
          if (typeof window.setActiveTab === "function") window.setActiveTab("editor");
          jumpLookSection(which === "audio" ? "audio" : "captions");
          if (typeof window.openMobileTimelinePanel === "function") {
            window.openMobileTimelinePanel("look");
          }
          selected = null;
          logoSelected = false;
          renderProps();
        };
        on("tlCaptionsBtn", "onclick", () => window.jumpTimelineLook("captions"));
        on("tlAudioBtn", "onclick", () => window.jumpTimelineLook("audio"));
        on("tlUndoBtn", "onclick", undo);
        on("tlRedoBtn", "onclick", redo);
        on("tlMagneticBtn", "onclick", () => {
          magnetic = !magnetic;
          const b = $("tlMagneticBtn");
          if (b) {
            b.classList.toggle("active", magnetic);
            b.textContent = magnetic ? "🧲 Snap on" : "🧲 Snap off";
            b.title = magnetic
              ? "Magnetic snap on — clip edges snap to nearby cuts / playhead"
              : "Magnetic snap off";
          }
        });
        on("tlZoomIn", "onclick", () => setZoom(4));
        on("tlZoomOut", "onclick", () => setZoom(-4));
        document.addEventListener("keydown", onEditorKeyDown);

        // Click / scrub ruler + empty lanes to seek playhead (output time).
        wireTimelineSeek();

        on("tlProjectBtn", "onclick", () => {
          // Logo / project panel — clear selection so renderProps shows project settings.
          selected = null;
          renderTimeline();
        });

        const timeline = $("tlTimeline");
        if (timeline) timeline.addEventListener("pointerdown", onTimelineMouseDown);
        document.addEventListener("pointermove", onMouseMove);
        document.addEventListener("pointerup", onMouseUp);
        document.addEventListener("pointermove", onBoxMove);
        document.addEventListener("pointerup", onBoxUp);
        wireScrub();
        wireTranscriptToolbar();
        wireLeftTabs();
        on("tlAutoOverlaysBtn", "onclick", () => suggestKeywordOverlays());
        on("tlAlwaysPackBtn", "onclick", () => {
          if (typeof window.applyAlwaysPackToTimeline === "function") {
            window.applyAlwaysPackToTimeline().then((ok) => {
              if (ok) {
                const go = confirm("Always pack applied. Run Suggest B-roll now?");
                if (go) suggestKeywordOverlays();
              }
            });
          }
        });
        on("tlBrollTestSerpBtn", "onclick", () => testSerpapiKey());
        on("tlBrollTestBtn", "onclick", () => testPexelsKey());
        on("tlBrollTestCseBtn", "onclick", () => testCseKey());
        on("tlBrollPrefer", "onchange", (e) => {
          const el = e && e.target;
          if (el) {
            el.dataset.userSet = "1";
            try { localStorage.setItem("tl_broll_prefer", el.value || "auto"); } catch (err) { /* ignore */ }
          }
        });
        on("tlBrollAiPhotos", "onchange", (e) => {
          const on = !!(e && e.target && e.target.checked);
          if (tl) {
            if (!tl.ai_edit) tl.ai_edit = {};
            tl.ai_edit.use_ai_photos = on;
            scheduleSave();
          }
          try { localStorage.setItem("tl_broll_use_ai", on ? "1" : "0"); } catch (err) { /* ignore */ }
        });
        on("tlAssetBtn", "onclick", () => $("tlAssetFile")?.click());
        on("tlAssetFile", "onchange", async (e) => {
          const f = e.target.files && e.target.files[0];
          if (!f) return;
          await uploadAsset(f);
          e.target.value = "";
          setLeftTab("media", { pin: true });
        });
        setLeftTab("media", { pin: true });
        setSaveState(TL_BUILD);
        updateHistoryButtons();
      } catch (e) {
        console.error("[timeline] wiring failed", e);
        alert("Editor failed to start (" + TL_BUILD + "): " + e.message + "\nTry a hard refresh (Cmd/Ctrl+Shift+R).");
      }

      // Open/create the project FIRST so `tl` exists before the source list
      // (with its + buttons) renders — otherwise an early click races a null tl.
      // When Shorts/Compilation is seeding clips, skip auto-opening the last
      // project so it can't overwrite the multi-clip seed.
      const skipAuto = _skipAutoOpenOnce;
      _skipAutoOpenOnce = false;
      if (!skipAuto) {
        try {
          // Prefer projects that already have clips; avoid auto-creating empty clutter.
          const data = await api("/timeline/list?include_empty=1");
          const all = data.timelines || [];
          const withClips = all.filter((p) => (p.clip_count || 0) > 0);
          if (withClips.length) await openProject(withClips[0].job_id);
          else if (all.length) await openProject(all[0].job_id);
          // else: wait until user clicks + New or drops media (ensureProject)
        } catch (e) {
          console.warn("[timeline] project init skipped", e);
        }
      }

      await loadSources();
      await loadAssets();
      await loadProjects();
      refreshBrollStatus().catch(() => {});
      console.log("[timeline] " + TL_BUILD + " ready; tl=", !!tl);
    })();

    return _initPromise;
  }

  // Expose for setActiveTab("editor") — both header + main nav entry points.
  window.ensureTimelineInit = ensureInit;
  window.addOverlayClip = addOverlayClip;
  window.addEffectClip = addEffectClip;
  window.openEffectPicker = openEffectPicker;
  window.EFFECT_TYPES = EFFECT_TYPES;
  window.getTimelineSelection = function () {
    return selected ? { track: selected.track, id: selected.id } : null;
  };
  window.refreshTimelineProps = function () {
    try { renderProps(); } catch (e) { /* ignore */ }
  };
  window.bindTimelinePropsHost = function (host) {
    if (!host || !selected) return;
    const c = findClip(selected.track, selected.id);
    if (!c) {
      // Project props panel — re-run renderProjectProps wiring by cloning from #tlProps.
      return;
    }
    wireProps(host, selected.track, c);
    host.querySelectorAll(".tl-swatch").forEach((sw) => {
      sw.onclick = () => {
        pushHistory();
        if (selected.track === "effects") c.preset = sw.dataset.preset;
        else {
          if (!c.color) c.color = {};
          c.color.preset = sw.dataset.preset;
        }
        renderProps();
        renderTracks();
        scheduleSave();
        if (typeof window.refreshMobileContextTools === "function") window.refreshMobileContextTools();
      };
    });
  };

  const CLIP_STYLES = [
    { id: "talking_head", label: "Talking head", blurb: "Punch zoom on the beat — CapCut social default" },
    { id: "split_stack", label: "Split / stack", blurb: "Top/bottom second video on Effects lane" },
    { id: "pip_corner", label: "Corner PiP", blurb: "Selected overlay → top-right PiP (or B-roll default)" },
    { id: "center_overlay", label: "Center overlay", blurb: "Selected overlay → center media (Always look)" },
    { id: "word_emphasis", label: "Word emphasis", blurb: "Punchwords on + tighter caption groups" },
    { id: "hook_broll", label: "Hook · B-roll density", blurb: "Center placement + denser Suggest near playhead" },
    { id: "cinematic", label: "Cinematic push", blurb: "Subtle Ken Burns on Main (you chose this style)" },
    { id: "clarity", label: "Clarity", blurb: "Clean talking-head — no punch, Hormozi-style captions" },
  ];

  window.listClipStyles = function () { return CLIP_STYLES.slice(); };

  window.replaceSelectedOverlayAsset = function (assetId, opts) {
    opts = opts || {};
    if (!tl || !assetId) return false;
    const targetId = _overlayReplaceTargetId
      || (selected && selected.track === "overlay" ? selected.id : null);
    if (!targetId) {
      alert("Select an Overlay clip first.");
      return false;
    }
    const c = findClip("overlay", targetId);
    if (!c) {
      endOverlayReplaceMode();
      alert("That Overlay clip is gone — select another and try Replace again.");
      return false;
    }
    pushHistory();
    c.asset_id = assetId;
    c.source_job_id = null;
    if (opts.source) c.source = opts.source;
    if (opts.keyword) c.keyword = opts.keyword;
    if (opts.source === "gif" || (opts.keyword && /\.gif$/i.test(String(opts.keyword)))) {
      c.ken_burns = null;
    } else if (opts.source === "gemini" || opts.source === "photo") {
      if (!(c.ken_burns && c.ken_burns.enabled) && isAlwaysPhotoMatchSession()) {
        c.ken_burns = alwaysKenBurnsDefault();
      }
    }
    selected = { track: "overlay", id: c.id };
    endOverlayReplaceMode();
    loadAssets().catch(() => {});
    renderTimeline();
    scheduleSave();
    setSaveState("Replaced overlay media");
    if (typeof window.refreshMobileContextTools === "function") window.refreshMobileContextTools();
    return true;
  };

  window.applyClipStyle = function (styleId) {
    if (!tl) {
      alert("Open a Timeline project first.");
      return false;
    }
    const mainSel = selected && selected.track === "main"
      ? findClip("main", selected.id)
      : (tl.tracks.main[0] || null);
    const ovSel = selected && selected.track === "overlay"
      ? findClip("overlay", selected.id)
      : null;

    pushHistory();
    if (styleId === "talking_head") {
      if (!mainSel) { alert("Select a Main clip first."); return false; }
      mainSel.punch_zoom = { enabled: true, intensity: "med" };
      if (mainSel.ken_burns) mainSel.ken_burns = null;
    } else if (styleId === "split_stack") {
      addEffectClip("split_screen", { layout: "stack", placement: "second_bottom" });
    } else if (styleId === "pip_corner") {
      if (ovSel) applyOverlayLayout(ovSel, "pip_tr");
      else {
        const placeEl = $("tlBrollPlacement");
        if (placeEl) placeEl.value = "pip";
        alert("PiP default set for B-roll. Select an Overlay clip to reposition it, or Suggest B-roll.");
      }
    } else if (styleId === "center_overlay") {
      if (ovSel) applyOverlayLayout(ovSel, "full");
      else {
        const placeEl = $("tlBrollPlacement");
        if (placeEl) placeEl.value = "center";
        alert("Center overlay default set. Select an Overlay or Suggest B-roll.");
      }
    } else if (styleId === "word_emphasis") {
      tl.style = normalizeTlStyle(Object.assign({}, tl.style || {}, {
        punchword_emphasis: true,
        group_size: 2,
        group: 2,
      }));
      syncStyleToCaptionLook(tl.style);
    } else if (styleId === "hook_broll") {
      const placeEl = $("tlBrollPlacement");
      const scopeEl = $("tlBrollScope");
      const modeEl = $("tlBrollMode");
      if (placeEl) placeEl.value = "center";
      if (scopeEl) scopeEl.value = "playhead";
      if (modeEl) {
        const photoOpt = modeEl.querySelector('option[value="photo"]:not([disabled])');
        const autoOpt = modeEl.querySelector('option[value="auto"]:not([disabled])');
        if (photoOpt) modeEl.value = "photo";
        else if (autoOpt) modeEl.value = "auto";
      }
      setLeftTab("media", { pin: true });
      setSaveState("Hook · B-roll — Suggest near playhead (center)");
      if (typeof window.openMobileTimelinePanel === "function") {
        try { window.openMobileTimelinePanel("media"); } catch (e) { /* optional */ }
      }
    } else if (styleId === "cinematic") {
      if (!mainSel) { alert("Select a Main clip first."); return false; }
      mainSel.ken_burns = { enabled: true, direction: "in", intensity: "low" };
      if (mainSel.punch_zoom) mainSel.punch_zoom = { enabled: false };
    } else if (styleId === "clarity") {
      if (mainSel) {
        mainSel.punch_zoom = null;
        mainSel.ken_burns = null;
      }
      tl.style = normalizeTlStyle(Object.assign({}, tl.style || {}, {
        font_name: "Montserrat Thin Black",
        font: "Montserrat Thin Black",
        primary_color: "#FFFFFF",
        highlight_color: "#FFD60A",
        group_size: 2,
        group: 2,
        punchword_emphasis: true,
      }));
      syncStyleToCaptionLook(tl.style);
    } else {
      return false;
    }
    renderTimeline();
    scheduleSave();
    setSaveState("Clip style · " + styleId);
    if (typeof window.refreshMobileContextTools === "function") window.refreshMobileContextTools();
    return true;
  };

  async function suggestKeywordOverlays() {
    if (!(await ensureProject())) return;
    const btn = $("tlAutoOverlaysBtn");
    const modeEl = $("tlBrollMode");
    const placeEl = $("tlBrollPlacement");
    const scopeEl = $("tlBrollScope");
    const aiEl = $("tlBrollAiPhotos");
    // Always session: sync photo mode/placement only — do NOT re-check AI photos.
    if (isAlwaysPhotoMatchSession()) {
      syncAlwaysBrollDefaults({ photo_match: true });
      await refreshBrollStatus().catch(() => {});
      syncAlwaysBrollDefaults({ photo_match: true });
    }
    const mode = modeEl ? modeEl.value : "auto";
    const placement = placeEl ? placeEl.value : "pip";
    const scope = scopeEl ? scopeEl.value : "full";
    // Opt-in only: unchecked means stock providers (CSE / Pexels / Unsplash).
    const useAiPhotos = !!(aiEl && aiEl.checked && !aiEl.disabled);
    const isLong = tl && tl.canvas === "16x9";
    if (btn) {
      btn.disabled = true;
      btn.textContent = useAiPhotos && mode !== "badge" && mode !== "gif"
        ? "Generating AI photos…"
        : (mode === "gif" ? "Fetching GIFs…" : "Fetching B-roll…");
    }
    try {
      let jobId = null;
      let winStart = null;
      let winEnd = null;
      if (selected && selected.track === "main") {
        const c = findClip("main", selected.id);
        jobId = c && c.source_job_id;
        if (scope === "selected" && c && c.source_job_id) {
          winStart = Number(c.in) || 0;
          winEnd = Number(c.out) != null ? Number(c.out) : winStart + 60;
        }
      }
      if (!jobId && tl.tracks.main[0]) jobId = tl.tracks.main[0].source_job_id;
      if (!jobId && typeof window.currentJobId !== "undefined") jobId = window.currentJobId;

      if (scope === "playhead") {
        const ot = playheadOutputTime();
        const center = ot != null ? ot : 0;
        const pad = isLong ? 45 : 30;
        winStart = Math.max(0, center - pad);
        winEnd = center + pad;
        // Map output → source when the active Main is an A-roll source clip.
        const active = activeMainAt(center);
        if (active && active.source_job_id) {
          const idx = tl.tracks.main.findIndex((x) => x.id === active.id);
          const local = center - mainStart(idx);
          const srcT = localOutputToSourceTime(active, local);
          winStart = Math.max(0, srcT - pad);
          winEnd = srcT + pad;
          jobId = active.source_job_id;
        }
      } else if (scope === "selected" && winStart == null) {
        // Fall back: first Main A-roll window.
        const c = (tl.tracks.main || []).find((x) => x.source_job_id);
        if (c) {
          winStart = Number(c.in) || 0;
          winEnd = Number(c.out) != null ? Number(c.out) : winStart + 120;
          jobId = c.source_job_id;
        }
      }

      // Long-form: quieter density (fewer suggestions per pass).
      // Cap 12 per Suggest click — run Suggest again (or change Scope) for more.
      let budget = isLong ? 4 : 5;
      if (scope === "playhead") budget = Math.min(budget, 3);
      if (scope === "selected") budget = isLong ? 4 : 5;
      if (mode === "gif") budget = Math.min(budget, 4);
      if (useAiPhotos) budget = Math.min(budget, 3);

      const body = { budget, mode, placement, use_ai_photos: useAiPhotos };
      if (jobId) body.job_id = jobId;
      if (winStart != null) body.start = winStart;
      if (winEnd != null) body.end = winEnd;
      const prefer = currentBrollPrefer();
      if (prefer && prefer !== "auto") body.prefer_provider = prefer;
      const spkEl = $("tlBrollSpeakerStills");
      body.prefer_speaker_stills = !(spkEl && !spkEl.checked);
      body.appearance_bias = true;
      const data = await api("/fetch-auto-overlays", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const list = (data.overlays || []).filter((ov) => ov && ov.source !== "badge");
      if (!list.length) {
        alert(mode === "gif"
          ? "No GIFs found. Needs SERPAPI_API_KEY (or Google CSE with Image search)."
          : mode === "photo"
          ? (useAiPhotos
            ? "No AI/stock photo B-roll found. Check GEMINI_API_KEY / SERPAPI_API_KEY / PEXELS_API_KEY."
            : "No photo B-roll found. Set SERPAPI_API_KEY or PEXELS_API_KEY, or enable Generate AI photos.")
          : (data.hint || "No photo B-roll moments found in this window. Configure SerpAPI / Pexels or enable AI photos."));
        return;
      }
      // Review queue — do NOT auto-place on Overlay until Accept.
      const always = isAlwaysPhotoMatchSession();
      pendingBroll = list.map((ov) => {
        const item = Object.assign({}, ov, { id: uid(), _status: "pending" });
        if (always && ov.source !== "gif" && !(item.ken_burns && item.ken_burns.enabled)) {
          item.ken_burns = alwaysKenBurnsDefault();
        }
        if ((!item.layout || item.layout === "pip_auto") && item.source !== "gif") {
          applyBrollLayoutRecipe(item);
        }
        return item;
      });
      await loadAssets();
      setLeftTab("media", { pin: true });
      renderPendingBroll();
      renderTracks();
      updateStageCompositor();
      const st = data.stats || {};
      const bits = [];
      if (st.gemini) bits.push(`${st.gemini} AI`);
      if (st.photo) bits.push(`${st.photo} photo`);
      if (st.gif) bits.push(`${st.gif} gif`);
      if (st.speaker_still) bits.push(`${st.speaker_still} talker`);
      const by = st.by_provider || {};
      const provBits = [];
      if (by.speaker_still) provBits.push(`Talker ${by.speaker_still}`);
      if (by.serpapi) provBits.push(`SerpAPI ${by.serpapi}`);
      if (by.google_cse) provBits.push(`CSE ${by.google_cse}`);
      if (by.pexels) provBits.push(`Pexels ${by.pexels}`);
      if (by.unsplash) provBits.push(`Unsplash ${by.unsplash}`);
      if (by.gemini) provBits.push(`Gemini ${by.gemini}`);
      const scopeLabel = scope === "playhead" ? "near playhead" : (scope === "selected" ? "selected clip" : "full transcript");
      setSaveState(
        (always ? "Always · " : "")
        + `${list.length} B-roll suggestion${list.length === 1 ? "" : "s"} (${scopeLabel}) — Accept / As Main / Skip`
        + (bits.length ? ` · ${bits.join(", ")}` : "")
        + (provBits.length ? ` · ${provBits.join(", ")}` : "")
      );
      if (!data.photo_ready && !data.gemini_image_ready && !st.photo && !st.gemini && !st.gif) {
        const hint = data.hint || "No photo API key in this Studio process. On Replit set PEXELS_API_KEY then Stop+Run.";
        const statusEl = $("tlBrollStatus");
        if (statusEl) {
          statusEl.textContent = "Photos unavailable — text badges disabled. " + hint;
          statusEl.style.color = "#f0c674";
        }
        refreshBrollStatus().catch(() => {});
      }
      if (window.StudioLogger) StudioLogger.clip("auto_overlays_pending", `${list.length}:${mode}:${scope}:ai=${useAiPhotos ? 1 : 0}`);
    } catch (e) {
      alert("Could not suggest overlays: " + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "✨ Suggest B-roll"; }
    }
  }

  function renderPendingBroll() {
    const host = $("tlBrollPending");
    if (!host) return;
    if (!pendingBroll.length) {
      host.classList.add("hidden");
      host.innerHTML = "";
      return;
    }
    host.classList.remove("hidden");
    const asMainDefault = tl && tl.canvas === "16x9";
    const always = isAlwaysPhotoMatchSession();
    const recipe = always ? "Always · center match + Ken Burns" : null;
    let html = `<div class="tl-broll-pending-head">
      <strong>Review B-roll (${pendingBroll.length})${recipe ? ` · ${recipe}` : ""}</strong>
      <div class="tl-broll-pending-actions">
        <button type="button" class="tl-chip-btn" data-broll-act="accept-all">${asMainDefault ? "Accept all → Overlay" : "Accept all"}</button>
        <button type="button" class="tl-chip-btn" data-broll-act="accept-all-main">Accept all → Main</button>
        <button type="button" class="tl-chip-btn tl-chip-danger" data-broll-act="skip-all">Skip all</button>
      </div>
    </div>`;
    pendingBroll.forEach((p) => {
      const start = Number(p.start || 0);
      const dur = Math.max(0.4, (p.out != null ? Number(p.out) : 1.8) - (p.in || 0));
      const srcLabel = p.source === "gif" ? "GIF"
        : (p.source === "gemini" ? "AI photo"
        : (p.source === "speaker_still" ? "Talker still"
        : (p.source === "photo" ? "Photo"
        : (p.source === "badge" ? "Badge" : (p.source || "Asset")))));
      html += `<div class="tl-broll-card" data-pending-id="${p.id}">
        <img class="tl-broll-thumb" src="/asset/${esc(p.asset_id)}" alt="" loading="lazy">
        <div class="tl-broll-meta">
          <div class="kw">${esc(p.keyword || "B-roll")}</div>
          <div class="when">${fmtTime(start)} · ${dur.toFixed(1)}s</div>
          <div class="src">${esc(srcLabel)}</div>
          <div class="tl-broll-btns">
            <button type="button" class="btn btn-primary" data-broll-act="accept" data-id="${p.id}">Overlay</button>
            <button type="button" class="btn btn-secondary" data-broll-act="accept-main" data-id="${p.id}">As Main</button>
            <button type="button" class="btn btn-secondary" data-broll-act="skip" data-id="${p.id}">Skip</button>
            <button type="button" class="btn btn-secondary" data-broll-act="replace" data-id="${p.id}">Replace</button>
            <button type="button" class="tl-chip-btn" data-broll-act="seek" data-id="${p.id}" title="Seek preview to this moment">↗ Seek</button>
          </div>
        </div>
      </div>`;
    });
    host.innerHTML = html;
    host.querySelectorAll("[data-broll-act]").forEach((btn) => {
      btn.onclick = () => {
        const act = btn.dataset.brollAct;
        const id = btn.dataset.id;
        if (act === "accept-all") return acceptAllPendingBroll();
        if (act === "accept-all-main") return acceptAllPendingBrollAsMain();
        if (act === "skip-all") return skipAllPendingBroll();
        if (act === "accept") return acceptPendingBroll(id);
        if (act === "accept-main") return acceptPendingBrollAsMain(id);
        if (act === "skip") return skipPendingBroll(id);
        if (act === "replace") return replacePendingBroll(id, btn);
        if (act === "seek") return seekPendingBroll(id);
      };
    });
  }

  function _pendingById(id) {
    return pendingBroll.find((p) => p.id === id) || null;
  }

  function seekPendingBroll(id) {
    const p = _pendingById(id);
    if (!p) return;
    const t = Number(p.start || 0);
    seekToOutputTime(t, { keepSelection: true, quietProps: true });
  }

  async function acceptPendingBroll(id) {
    const p = _pendingById(id);
    if (!p) return;
    pendingBroll = pendingBroll.filter((x) => x.id !== id);
    renderPendingBroll();
    const ref = prepareBrollAcceptRef(p);
    await addOverlayClip(ref);
    renderTracks();
    updateStageCompositor();
    setSaveState(`Accepted “${p.keyword || "B-roll"}” → Overlay`);
  }

  async function acceptPendingBrollAsMain(id) {
    const p = _pendingById(id);
    if (!p) return;
    pendingBroll = pendingBroll.filter((x) => x.id !== id);
    renderPendingBroll();
    const ref = prepareBrollAcceptRef(p);
    const asset = assets.find((x) => x.asset_id === p.asset_id) || null;
    await addMainCutawayClip(ref, asset);
    renderTracks();
    updateStageCompositor();
    setSaveState(`Accepted “${p.keyword || "B-roll"}” → Main cutaway`);
  }

  function skipPendingBroll(id) {
    const p = _pendingById(id);
    if (!p) return;
    pendingBroll = pendingBroll.filter((x) => x.id !== id);
    // Best-effort cleanup of unused suggestion asset (ignore failures).
    if (p.asset_id) {
      api("/delete-asset/" + p.asset_id, { method: "POST" }).catch(() => {});
    }
    renderPendingBroll();
    renderTracks();
    updateStageCompositor();
    loadAssets().catch(() => {});
    setSaveState(`Skipped “${p.keyword || "B-roll"}”`);
  }

  async function acceptAllPendingBroll() {
    const list = pendingBroll.slice();
    if (!list.length) return;
    pendingBroll = [];
    renderPendingBroll();
    const ownHistory = !historySuspended;
    if (ownHistory) pushHistory();
    const prevSuspend = historySuspended;
    historySuspended = true;
    setSaveState(`Accepting ${list.length} overlays…`);
    try {
      for (const p of list) {
        const ref = prepareBrollAcceptRef(p);
        await addOverlayClip(ref);
      }
    } finally {
      historySuspended = prevSuspend;
    }
    renderTracks();
    updateStageCompositor();
    const always = isAlwaysPhotoMatchSession();
    setSaveState(
      `Accepted ${list.length} B-roll overlay${list.length === 1 ? "" : "s"}`
      + (always ? " · Always layout + Ken Burns" : "")
      + (ownHistory ? " (one Undo)" : "")
    );
  }

  async function acceptAllPendingBrollAsMain() {
    const list = pendingBroll.slice();
    if (!list.length) return;
    pendingBroll = [];
    renderPendingBroll();
    // Later cutaways shift Main — accept in reverse chronological order so
    // earlier `start` times stay valid as we splice.
    list.sort((a, b) => Number(b.start || 0) - Number(a.start || 0));
    const ownHistory = !historySuspended;
    if (ownHistory) pushHistory();
    const prevSuspend = historySuspended;
    historySuspended = true;
    setSaveState(`Accepting ${list.length} Main cutaways…`);
    try {
      for (const p of list) {
        const ref = prepareBrollAcceptRef(p);
        const asset = assets.find((x) => x.asset_id === p.asset_id) || null;
        await addMainCutawayClip(ref, asset);
      }
    } finally {
      historySuspended = prevSuspend;
    }
    renderTracks();
    updateStageCompositor();
    const always = isAlwaysPhotoMatchSession();
    setSaveState(
      `Accepted ${list.length} Main cutaway${list.length === 1 ? "" : "s"}`
      + (always ? " · Always Ken Burns" : "")
      + (ownHistory ? " (one Undo)" : "")
    );
  }

  function skipAllPendingBroll() {
    const list = pendingBroll.slice();
    pendingBroll = [];
    list.forEach((p) => {
      if (p.asset_id) api("/delete-asset/" + p.asset_id, { method: "POST" }).catch(() => {});
    });
    renderPendingBroll();
    renderTracks();
    updateStageCompositor();
    loadAssets().catch(() => {});
    setSaveState(`Skipped ${list.length} suggestion${list.length === 1 ? "" : "s"}`);
  }

  async function replacePendingBroll(id, btn) {
    const p = _pendingById(id);
    if (!p) return;
    const card = document.querySelector(`.tl-broll-card[data-pending-id="${id}"]`);
    if (card) card.classList.add("is-replacing");
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    const modeEl = $("tlBrollMode");
    const placeEl = $("tlBrollPlacement");
    const aiEl = $("tlBrollAiPhotos");
    const mode = modeEl ? modeEl.value : "auto";
    const placement = placeEl ? placeEl.value : "pip";
    const useAiPhotos = !!(aiEl && aiEl.checked && !aiEl.disabled);
    let jobId = null;
    if (tl && tl.tracks.main[0]) jobId = tl.tracks.main[0].source_job_id;
    if (!jobId && typeof window.currentJobId !== "undefined") jobId = window.currentJobId;
    try {
      const body = {
        budget: 1,
        mode,
        placement,
        use_ai_photos: useAiPhotos,
        keywords: [{
          text: p.keyword || "B-roll",
          start: p.start || 0,
          duration: Math.max(1.2, (p.out != null ? Number(p.out) : 1.8) - (p.in || 0)),
        }],
      };
      if (jobId) body.job_id = jobId;
      const prefer = currentBrollPrefer();
      if (prefer && prefer !== "auto") body.prefer_provider = prefer;
      const spkEl = $("tlBrollSpeakerStills");
      body.prefer_speaker_stills = !(spkEl && !spkEl.checked);
      body.appearance_bias = true;
      const data = await api("/fetch-auto-overlays", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const next = (data.overlays || [])[0];
      if (!next || !next.asset_id) {
        alert("No replacement found for “" + (p.keyword || "B-roll") + "”. Try another mode.");
        return;
      }
      const oldAsset = p.asset_id;
      Object.assign(p, next, {
        id: p.id,
        start: p.start,
        keyword: p.keyword || next.keyword,
        _status: "pending",
      });
      if (oldAsset && oldAsset !== p.asset_id) {
        api("/delete-asset/" + oldAsset, { method: "POST" }).catch(() => {});
      }
      await loadAssets();
      renderPendingBroll();
      renderTracks();
      updateStageCompositor();
      setSaveState(`Replaced “${p.keyword}” candidate`);
    } catch (e) {
      alert("Replace failed: " + e.message);
    } finally {
      if (card) card.classList.remove("is-replacing");
    }
  }

  // Branding tab → Timeline: caption style, speaker colors, headline, logo,
  // plus CapCut pack extras (canvas, punch, ken burns, color grade, B-roll defaults).
  window.applyTimelineBranding = function (style, opts) {
    if (!tl) {
      alert("Open or create a Timeline project first.");
      return false;
    }
    pushHistory();
    style = normalizeTlStyle(style || {});
    opts = opts || {};
    tl.style = Object.assign({}, tl.style || {}, style);
    const sc = style.speaker_colors || opts.speaker_color_map || {};
    const merged = Object.assign({}, tl.speaker_colors || {});
    Object.keys(sc).forEach((k) => { if (sc[k]) merged[k] = sc[k]; });
    if (sc.Host && !merged.SPEAKER_00) merged.SPEAKER_00 = sc.Host;
    if (sc.Guest && !merged.SPEAKER_01) merged.SPEAKER_01 = sc.Guest;
    if (!merged.SPEAKER_00) merged.SPEAKER_00 = "#FFD700";
    if (!merged.SPEAKER_01) merged.SPEAKER_01 = "#00E5FF";
    tl.speaker_colors = merged;
    const banner = style.headline_banner;
    if (banner) tl.headline_banner = { text: String(banner) };
    else if (banner === "") tl.headline_banner = null;
    if (opts.logo && opts.logo.asset_id) {
      tl.logo = Object.assign(
        { x: 0.04, y: 0.04, w: 0.18, opacity: 0.9 },
        tl.logo || {},
        opts.logo,
      );
    }
    if (opts.canvas) {
      const c = String(opts.canvas).replace(":", "x");
      if (c === "9x16" || c === "16x9" || c === "1x1") {
        tl.canvas = c;
        if ($("tlCanvas")) $("tlCanvas").value = tl.canvas;
      }
    }
    // Punch zoom / Ken Burns / color grade on Main clips (opt-in from CapCut packs).
    if (opts.punch_zoom && opts.punch_zoom.enabled && tl.tracks.main.length) {
      tl.tracks.main.forEach((clip) => {
        clip.punch_zoom = {
          enabled: true,
          intensity: opts.punch_zoom.intensity || "med",
        };
      });
    }
    if (opts.ken_burns && opts.ken_burns.enabled && tl.tracks.main.length) {
      tl.tracks.main.forEach((clip) => {
        if (clip.cutaway) return; // still cutaways stay static unless user enables
        clip.ken_burns = {
          enabled: true,
          direction: opts.ken_burns.direction || "in",
          intensity: opts.ken_burns.intensity || "low",
        };
      });
    }
    if (opts.color_grade) {
      tl.tracks.main.forEach((clip) => {
        clip.color_grade = opts.color_grade;
      });
    }
    if (opts.broll_mode && $("tlBrollMode")) {
      const modeEl = $("tlBrollMode");
      const opt = modeEl.querySelector(`option[value="${opts.broll_mode}"]`);
      if (opt && !opt.disabled) modeEl.value = opts.broll_mode;
    }
    if (opts.broll_placement && $("tlBrollPlacement")) {
      $("tlBrollPlacement").value = opts.broll_placement;
    }
    if (opts.broll_scope && $("tlBrollScope")) {
      $("tlBrollScope").value = opts.broll_scope;
    }
    selected = null;
    renderTimeline();
    applyStage();
    scheduleSave();
    setSaveState("Branding applied ✓");
    // Refresh media library so the new logo asset appears.
    try { loadAssets(); } catch (e) {}
    return true;
  };

  // Hook every Editor tab button (header workflow + mainTabs) so either works.
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll('.main-tab[data-tab="editor"]').forEach((tabBtn) => {
      tabBtn.addEventListener("click", ensureInit);
    });
  });

  // Expose an entry point so Shorts / Assembly / Compilation can open the
  // editor seeded from one job or a multi-clip queue.
  window.openTimelineEditor = async function (seedJobId, opts) {
    opts = opts || {};
    const seeding = !!(opts.newProject || (opts.clips && opts.clips.length) || seedJobId || opts.seedTimeline);
    if (seeding) {
      window._tlDeferAutoOpen = true;
      _skipAutoOpenOnce = true;
    }
    try {
      // Flush Caption look so job.style matches the Branding panel before we seed.
      if (typeof window.flushCaptionLookToJob === "function" && (seedJobId || (typeof window.currentJobId !== "undefined" && window.currentJobId))) {
        try { await window.flushCaptionLookToJob(); } catch (e) { /* best-effort */ }
      }
      if (typeof window.setActiveTab === "function") {
        window.setActiveTab("editor");
      } else {
        const tabBtn = document.querySelector('.main-tab[data-tab="editor"]');
        if (tabBtn) tabBtn.click();
      }
      await ensureInit({ skipAutoOpen: seeding });

      const wantCanvas = opts.canvas || (opts.longForm ? "16x9" : null);
      const forceNew = !!(opts.newProject || opts.longForm || opts.seedTimeline);

      if (forceNew || (seeding && !tl)) {
        await newProject({
          canvas: wantCanvas || undefined,
          label: opts.label || (opts.longForm ? "Long-form edit" : undefined),
        });
      }

      if (wantCanvas && tl) {
        pushHistory();
        tl.canvas = wantCanvas;
        if ($("tlCanvas")) $("tlCanvas").value = tl.canvas;
        // Long-form defaults: quieter B-roll placement (center is less shouty on 16:9).
        if (opts.longForm) {
          const placeEl = $("tlBrollPlacement");
          const scopeEl = $("tlBrollScope");
          if (placeEl && placeEl.value === "pip") placeEl.value = "center";
          if (scopeEl) scopeEl.value = "playhead";
        }
        applyStage();
      }

      // Captions-style AI Edit seed: apply full timeline JSON onto the project.
      if (opts.seedTimeline && tl) {
        pushHistory();
        applySeedTimeline(opts.seedTimeline, opts.label);
        // AI packs use short keys — normalize; if still empty, Caption look.
        if (tl.style) tl.style = normalizeTlStyle(tl.style);
        seedStyleFromCaptionLook(opts.seedTimeline.source_job_id || seedJobId);
        await loadSources();
        await refreshMaxTrims();
        renderTimeline();
        scheduleSave();
        try {
          await finishAlwaysSeedHandoff(opts.seedTimeline, opts.mediaHints || opts.seedTimeline.media_hints);
        } catch (e) {
          console.warn("[timeline] Always handoff:", e);
        }
        return;
      }

      const clips = Array.isArray(opts.clips) ? opts.clips : null;
      if (clips && clips.length) {
        if (!(await ensureProject())) return;
        pushHistory();
        if (opts.replace) {
          tl.tracks.main = [];
          selected = null;
        }
        await loadSources();
        for (const item of clips) {
          const jid = item && (item.source_job_id || item.job_id);
          if (!jid) continue;
          const inS = item.start_time != null ? Number(item.start_time)
            : (item.in != null ? Number(item.in) : null);
          const outS = item.end_time != null ? Number(item.end_time)
            : (item.out != null ? Number(item.out) : null);
          await addMainClip(jid, inS, outS, { skipRender: true, skipHistory: true });
        }
        seedStyleFromCaptionLook(clips[0] && (clips[0].source_job_id || clips[0].job_id));
        renderTimeline();
        scheduleSave();
        return;
      }

      // Single-job seed (Edit range / job row / long-form ingest).
      if (seedJobId && !sources.find((s) => s.job_id === seedJobId)) await loadSources();
      if (seedJobId && tl) {
        if (opts.replace || opts.longForm) {
          pushHistory();
          tl.tracks.main = [];
          selected = null;
        }
        await addMainClip(seedJobId, opts.in, opts.out);
        seedStyleFromCaptionLook(seedJobId);
        renderTimeline();
        scheduleSave();
        if (opts.longForm) {
          setLeftTab("media", { pin: true });
          setSaveState("Long-form 16:9 — Suggest B-roll near playhead, Accept as Main cutaway");
        }
      }
    } finally {
      window._tlDeferAutoOpen = false;
    }
  };

  function applySeedTimeline(seed, label) {
    if (!tl || !seed) return;
    const tracks = seed.tracks || {};
    tl.canvas = seed.canvas || tl.canvas || "9x16";
    tl.fit = seed.fit || tl.fit || "cover";
    tl.fps = seed.fps || tl.fps || 30;
    tl.bg = seed.bg || tl.bg || "#000000";
    if (seed.style) tl.style = normalizeTlStyle(seed.style);
    if (seed.ai_edit) tl.ai_edit = seed.ai_edit;
    if (label) {
      tl.label = label;
      if ($("tlLabel")) $("tlLabel").value = label;
    }
    if ($("tlCanvas")) $("tlCanvas").value = tl.canvas;
    if ($("tlFit")) $("tlFit").value = tl.fit;
    tl.tracks.main = Array.isArray(tracks.main) ? tracks.main.map((c) => ({
      ...c,
      id: c.id || uid(),
      burn_captions: c.burn_captions !== false,
    })) : [];
    tl.tracks.overlay = Array.isArray(tracks.overlay) ? tracks.overlay.map((c) => ({ ...c, id: c.id || uid() })) : [];
    tl.tracks.effects = Array.isArray(tracks.effects) ? tracks.effects.map((c) => ({ ...c, id: c.id || uid() })) : [];
    tl.tracks.text = Array.isArray(tracks.text) ? tracks.text.map((c) => ({ ...c, id: c.id || uid() })) : [];
    tl.tracks.music = Array.isArray(tracks.music) ? tracks.music.map((c) => ({ ...c, id: c.id || uid() })) : [];
    selected = tl.tracks.main[0] ? { track: "main", id: tl.tracks.main[0].id } : null;
    applyStage();
  }

  // ---- Merge adjacent Main shots ----
  // Same source: union in/out; source gaps become cuts (skip middle).
  // Different sources: stitch via /compile-clips into a new job, replace A+B
  // with one Main clip pointing at that stitched source.
  async function mergeSelectedWithNext() {
    if (!tl || !selected || selected.track !== "main") {
      alert("Select a Main clip to merge with the next shot.");
      return;
    }
    const idx = tl.tracks.main.findIndex((c) => c.id === selected.id);
    if (idx < 0 || idx >= tl.tracks.main.length - 1) {
      alert("Nothing to merge — pick a clip that has a following shot.");
      return;
    }
    const a = tl.tracks.main[idx];
    const b = tl.tracks.main[idx + 1];
    if (!a.source_job_id || !b.source_job_id) {
      alert("Both shots need a source video to merge.");
      return;
    }

    if (a.source_job_id !== b.source_job_id) {
      await mergeDifferentSources(idx, a, b);
      return;
    }

    const aIn = Number(a.in) || 0;
    const aOut = Number(a.out) || aIn;
    const bIn = Number(b.in) || 0;
    const bOut = Number(b.out) || bIn;
    if (bOut <= bIn + 0.05) {
      alert("Next shot has no usable duration.");
      return;
    }

    const earlyIn = Math.min(aIn, bIn);
    const earlyOut = aIn <= bIn ? aOut : bOut;
    const lateIn = aIn <= bIn ? bIn : aIn;
    const lateOut = Math.max(aOut, bOut);
    const gap = lateIn - earlyOut;
    const beforeDur = clipDuration(a) + clipDuration(b);

    if (bIn >= aIn - 0.05 && bOut <= aOut + 0.05) {
      pushHistory();
      reanchorFromClip(b.id, a.id);
      tl.tracks.main.splice(idx + 1, 1);
      selectClip("main", a.id);
      renderTimeline();
      scheduleSave();
      setRenderStatus("Removed next shot — it was already inside this clip’s source range");
      return;
    }

    pushHistory();
    a.in = earlyIn;
    a.out = lateOut;
    const cuts = [...(a.cuts || []), ...(b.cuts || [])];
    let gapCut = null;
    if (gap > 0.05) {
      gapCut = [earlyOut, lateIn];
      cuts.push(gapCut);
    }
    a.cuts = mergeCuts(cuts);
    a.word_overrides = Object.assign({}, b.word_overrides || {}, a.word_overrides || {});
    a.transition = b.transition || a.transition || null;
    if (!a.punch_zoom && b.punch_zoom) a.punch_zoom = b.punch_zoom;
    if (!a.ken_burns && b.ken_burns) a.ken_burns = b.ken_burns;
    if (!(a.color || a.color_grade) && (b.color || b.color_grade)) {
      a.color = b.color || b.color_grade;
      a.color_grade = a.color;
    }
    a._max = Math.max(Number(a._max) || 0, Number(b._max) || 0, a.out);
    reanchorFromClip(b.id, a.id);
    tl.tracks.main.splice(idx + 1, 1);
    selectClip("main", a.id);
    renderTimeline();
    scheduleSave();
    const afterDur = clipDuration(a);
    if (gapCut) {
      setRenderStatus(
        `Merged with ${fmtTime(gap)} gap skipped as cut · ` +
        `${fmtTime(a.in)}–${fmtTime(a.out)} · ${fmtTime(afterDur)} visible`
      );
    } else {
      setRenderStatus(
        `Merged continuous stretch → ${fmtTime(a.in)}–${fmtTime(a.out)} · ${fmtTime(afterDur)} visible`
      );
    }
  }

  function reanchorFromClip(fromId, toId) {
    ["overlay", "effects", "text", "music"].forEach((k) => {
      (tl.tracks[k] || []).forEach((c) => {
        if (c.anchor === fromId) c.anchor = toId;
      });
    });
  }

  function sourceNameForJob(jobId) {
    const s = sources.find((x) => x.job_id === jobId);
    return (s && s.filename) || String(jobId || "").slice(0, 8);
  }

  /** Expand a Main clip into compile-clips segments (honors word-cut keep-ranges). */
  function mainClipToCompileSegments(c) {
    if (!c || !c.source_job_id) return [];
    const ranges = keepRangesForClip(c, { allowEmpty: true });
    if (!ranges.length) return [];
    const fname = sourceNameForJob(c.source_job_id);
    return ranges.map(([a, b]) => ({
      source_job_id: c.source_job_id,
      start_time: a,
      end_time: b,
      source_filename: fname,
    }));
  }

  async function stitchCompileClips(clips, label) {
    const res = await fetch("/compile-clips", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: label || "stitched", clips }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "Stitch failed");
    return data;
  }

  async function resolveStitchedDuration(jobId, fallback) {
    try {
      const info = await api("/source-info/" + jobId);
      if (info && info.duration) return Number(info.duration);
    } catch (e) {}
    return fallback || 0;
  }

  async function mergeDifferentSources(idx, a, b) {
    const btn = $("tlMergeBtn");
    const aName = sourceNameForJob(a.source_job_id);
    const bName = sourceNameForJob(b.source_job_id);
    if (!confirm(
      `These are different source videos.\n\n` +
      `Merge will stitch them into a new combined clip:\n` +
      `• ${aName}\n• ${bName}\n\nContinue?`
    )) return;

    if (btn) { btn.disabled = true; btn.textContent = "⛓ Stitching…"; }
    setRenderStatus("Stitching different sources into one clip…");
    try {
      const label = `merged ${String(aName).replace(/\.[^.]+$/, "").slice(0, 24)}+${String(bName).replace(/\.[^.]+$/, "").slice(0, 24)}`;
      const segs = [
        ...mainClipToCompileSegments(a),
        ...mainClipToCompileSegments(b),
      ];
      if (segs.length < 2) throw new Error("Nothing to stitch (clips may be fully cut away).");
      const data = await stitchCompileClips(segs, label);
      const newId = data.job_id;
      const dur = await resolveStitchedDuration(newId, clipDuration(a) + clipDuration(b));
      srcDur[newId] = dur;
      pushHistory();
      const merged = {
        id: uid(),
        source_job_id: newId,
        in: 0,
        out: dur,
        _max: dur,
        transition: b.transition || a.transition || null,
        burn_captions: a.burn_captions !== false,
        cuts: [],
        color: a.color || a.color_grade || b.color || b.color_grade || null,
        color_grade: a.color || a.color_grade || b.color || b.color_grade || null,
        punch_zoom: a.punch_zoom || null,
        ken_burns: a.ken_burns || null,
      };
      reanchorFromClip(a.id, merged.id);
      reanchorFromClip(b.id, merged.id);
      tl.tracks.main.splice(idx, 2, merged);
      applyAnchors();
      if (typeof window.addJobToList === "function") window.addJobToList(newId);
      await loadSources();
      selectClip("main", merged.id);
      renderTimeline();
      scheduleSave();
      setRenderStatus(`Stitched different sources → ${fmtTime(dur)} · ${data.filename || "merged clip"}`);
    } catch (e) {
      alert("Merge/stitch failed: " + (e.message || e));
      setRenderStatus("Merge failed");
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "⛓ Merge"; }
    }
  }

  /**
   * Bake the whole Main track into one stitched source (Captions-style compile
   * inside Timeline). Word cuts become omitted segments. Overlays/titles keep
   * absolute times and re-anchor to the new single Main clip.
   */
  async function stitchMainTrack() {
    if (!tl || !tl.tracks.main.length) {
      alert("Add at least one Main clip first.");
      return;
    }
    const segs = [];
    tl.tracks.main.forEach((c) => {
      mainClipToCompileSegments(c).forEach((s) => segs.push(s));
    });
    if (!segs.length) {
      alert("Nothing to stitch — Main clips are empty or fully cut away.");
      return;
    }
    const nClips = tl.tracks.main.length;
    if (!confirm(
      `Stitch Main into one combined clip?\n\n` +
      `• ${nClips} Main shot${nClips === 1 ? "" : "s"} → ${segs.length} segment${segs.length === 1 ? "" : "s"}\n` +
      `• Word cuts are baked out (skipped)\n` +
      `• Transitions between Main shots are baked away (become hard joins in the new file)\n` +
      `• Effects / overlays / titles keep absolute timing and re-anchor to the new clip\n` +
      `• Per-clip punch / Ken Burns on Main copies from the first shot that has them\n\n` +
      `Then Preview cut / Render as usual.`
    )) return;

    const btn = $("tlStitchMainBtn");
    if (btn) { btn.disabled = true; btn.textContent = "🎞 Stitching…"; }
    setRenderStatus("Stitching Main track…");
    try {
      const label = (tl.label && String(tl.label).trim())
        ? `${String(tl.label).trim()} — stitched`
        : "timeline stitch";
      const data = await stitchCompileClips(segs, label);
      const newId = data.job_id;
      const beforeDur = totalDuration();
      const dur = await resolveStitchedDuration(newId, beforeDur);
      srcDur[newId] = dur;
      pushHistory();
      const oldIds = tl.tracks.main.map((c) => c.id);
      const merged = {
        id: uid(),
        source_job_id: newId,
        in: 0,
        out: dur,
        _max: dur,
        transition: null,
        burn_captions: true,
        cuts: [],
      };
      // Prefer grade / camera FX from the first Main clip that has them.
      const firstFx = tl.tracks.main.find((c) => c.color || c.color_grade);
      if (firstFx) {
        merged.color = firstFx.color || firstFx.color_grade;
        merged.color_grade = merged.color;
      }
      const firstPunch = tl.tracks.main.find((c) => c.punch_zoom && c.punch_zoom.enabled);
      if (firstPunch) merged.punch_zoom = JSON.parse(JSON.stringify(firstPunch.punch_zoom));
      const firstKen = tl.tracks.main.find((c) => c.ken_burns && c.ken_burns.enabled);
      if (firstKen) merged.ken_burns = JSON.parse(JSON.stringify(firstKen.ken_burns));
      oldIds.forEach((oid) => reanchorFromClip(oid, merged.id));
      tl.tracks.main = [merged];
      applyAnchors();
      if (typeof window.addJobToList === "function") window.addJobToList(newId);
      await loadSources();
      selectClip("main", merged.id);
      renderTimeline();
      scheduleSave();
      setRenderStatus(
        `Main stitched → ${fmtTime(dur)} · ${data.filename || "stitched"} · Preview cut or Render next`
      );
    } catch (e) {
      alert("Stitch Main failed: " + (e.message || e));
      setRenderStatus("Stitch failed");
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "🎞 Stitch Main"; }
    }
  }

  async function detectShotsOnSelected() {
    if (!tl) return;
    let clip = null;
    if (selected && selected.track === "main") {
      clip = tl.tracks.main.find((c) => c.id === selected.id);
    } else if (tl.tracks.main.length === 1) {
      clip = tl.tracks.main[0];
    }
    if (!clip || !clip.source_job_id) {
      alert("Select a Main clip with a source video first.");
      return;
    }
    const btn = $("tlDetectShotsBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Detecting…"; }
    try {
      const res = await fetch("/detect-shots", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: clip.source_job_id,
          in: clip.in || 0,
          out: clip.out,
          threshold: 0.35,
        }),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      const shots = data.shots || [];
      if (shots.length <= 1) {
        alert("No scene changes detected in this clip.");
        return;
      }
      if (!confirm(
        `Split this Main clip into ${shots.length} shots at scene changes?\n\n` +
        `This is the opposite of Compile — it breaks one clip into S1…S${shots.length} on the timeline.\n` +
        `To join AI Shorts highlights into one video, use Compilation → Compile instead.`
      )) return;
      pushHistory();
      const idx = tl.tracks.main.findIndex((c) => c.id === clip.id);
      const pieces = shots.map((s, i) => {
        const piece = {
          id: uid(),
          source_job_id: clip.source_job_id,
          in: s.start,
          out: s.end,
          _max: clip._max,
          transition: null,
          burn_captions: clip.burn_captions !== false,
          cuts: (clip.cuts || []).filter(([cs, ce]) => ce > s.start && cs < s.end)
            .map(([cs, ce]) => [Math.max(cs, s.start), Math.min(ce, s.end)]),
          color_grade: clip.color_grade ? JSON.parse(JSON.stringify(clip.color_grade)) : null,
          shot_index: s.index,
        };
        return piece;
      });
      // Preserve first-piece effects if whole-clip had them.
      if (clip.punch_zoom) pieces[0].punch_zoom = clip.punch_zoom;
      if (clip.ken_burns) pieces[0].ken_burns = clip.ken_burns;
      tl.tracks.main.splice(idx, 1, ...pieces);
      selectClip("main", pieces[0].id);
      renderTimeline();
      scheduleSave();
      setRenderStatus(`Split into ${pieces.length} shots`);
    } catch (e) {
      alert("Shot detection failed: " + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "🎞 Detect shots"; }
    }
  }

  async function restyleSelectedShot(packId, intensity) {
    if (!tl || !selected || selected.track !== "main") {
      alert("Select a Main shot to restyle.");
      return;
    }
    const clip = tl.tracks.main.find((c) => c.id === selected.id);
    if (!clip || !clip.source_job_id) return;
    try {
      const res = await fetch("/ai-edit-seed", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_job_id: clip.source_job_id,
          start_time: clip.in || 0,
          end_time: clip.out,
          style_pack: packId || "pulse",
          intensity: intensity || "med",
          apply_cuts: false,
          create_clip: false,
          label: "Shot restyle",
        }),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      const seeded = ((data.timeline || {}).tracks || {}).main || [];
      if (!seeded.length) throw new Error("No restyle result");
      pushHistory();
      const idx = tl.tracks.main.findIndex((c) => c.id === clip.id);
      // Replace this one shot with the seeded pieces; shift later clips.
      const pieces = seeded.map((c) => ({ ...c, id: uid() }));
      tl.tracks.main.splice(idx, 1, ...pieces);
      if (data.timeline && data.timeline.style) tl.style = data.timeline.style;
      selectClip("main", pieces[0].id);
      renderTimeline();
      scheduleSave();
    } catch (e) {
      alert("Shot restyle failed: " + e.message);
    }
  }

  // ---- Co-editor (chat mutates timeline) ----
  function openCoEditor() {
    const drawer = $("coEditorDrawer");
    if (!drawer) {
      console.warn("[timeline] coEditorDrawer missing from DOM");
      return;
    }
    drawer.classList.remove("hidden");
    drawer.style.display = "flex";
    drawer.setAttribute("aria-hidden", "false");
    const btn = $("tlCoEditorBtn");
    if (btn) btn.classList.add("active-co");
    const log = $("coEditorLog");
    if (log && !log.dataset.booted) {
      log.dataset.booted = "1";
      appendCoMsg("bot",
        "I'm your Timeline remote — describe the edit you want on this project. " +
        "I apply structured ops now (captions, shots, overlays, music, styles). " +
        "I can't invent new features, rewrite transcript words, or finish the export — you still ▶ Render. " +
        "Select a clip and say “this” to target it."
      );
      renderCoEditorChips();
    }
    try { if ($("coEditorInput")) $("coEditorInput").focus(); } catch (e) {}
    try {
      if (document.body.classList.contains("is-phone")) {
        document.body.classList.add("tl-coeditor-open");
      }
    } catch (e) {}
  }
  function closeCoEditor() {
    const drawer = $("coEditorDrawer");
    if (!drawer) return;
    drawer.classList.add("hidden");
    drawer.style.display = "";
    drawer.setAttribute("aria-hidden", "true");
    const btn = $("tlCoEditorBtn");
    if (btn) btn.classList.remove("active-co");
    try {
      if (typeof window.onCoEditorClosed === "function") window.onCoEditorClosed();
    } catch (e) {}
  }
  function appendCoMsg(role, text) {
    const log = $("coEditorLog");
    if (!log) return;
    const div = document.createElement("div");
    div.className = "co-editor-msg " + role;
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  const CO_EDITOR_CHIPS = [
    { label: "Bigger captions", prompt: "Make the captions bigger and punchier" },
    { label: "Duck music", prompt: "Duck the music bed under speech" },
    { label: "PiP this overlay", prompt: "Make the selected overlay a top-right PiP" },
    { label: "Cinematic look", prompt: "Apply the cinematic clip style to this shot" },
    { label: "Suggest B-roll", prompt: "Suggest photo B-roll near the playhead for review" },
    { label: "Accept all B-roll", prompt: "Accept all pending B-roll suggestions onto Overlay" },
    { label: "Run Polish", prompt: "Run polish cut on the Main source with fast pacing and PiP B-roll" },
    { label: "What can you do?", prompt: "What can you change on this timeline, and what can't you do?" },
  ];

  function renderCoEditorChips() {
    const host = $("coEditorChips");
    if (!host || host.dataset.ready) return;
    host.dataset.ready = "1";
    host.innerHTML = CO_EDITOR_CHIPS.map((c) =>
      `<button type="button" class="co-editor-chip" data-co-prompt="${String(c.prompt).replace(/"/g, "&quot;")}">${c.label}</button>`
    ).join("");
    host.addEventListener("click", (e) => {
      const btn = e.target && e.target.closest && e.target.closest("[data-co-prompt]");
      if (!btn) return;
      const input = $("coEditorInput");
      if (input) input.value = btn.dataset.coPrompt || "";
      sendCoEditorPrompt();
    });
  }

  function transcriptNearPlayhead() {
    const words = transcriptWords || [];
    if (!words.length) return null;
    const t = playheadOutputTime();
    if (t == null) return null;
    // Approximate: words use source time; near current transcript cache is fine.
    const nearby = words.filter((w) => {
      const s = Number(w.start != null ? w.start : w.begin) || 0;
      const e = Number(w.end) || (s + 0.3);
      return e >= t - 4 && s <= t + 4;
    }).slice(0, 28);
    if (!nearby.length) return null;
    return nearby.map((w) => w.word || w.text || "").filter(Boolean).join(" ").slice(0, 220);
  }

  function coEditorClientContext() {
    const sel = selected
      ? {
          track: selected.track,
          id: selected.id,
          index: (tl && tl.tracks[selected.track])
            ? tl.tracks[selected.track].findIndex((c) => c.id === selected.id)
            : -1,
        }
      : null;
    let playhead = null;
    try {
      playhead = playheadOutputTime();
      if (playhead == null) {
        const v = $("tlPreviewVideo");
        playhead = v ? (v.currentTime || 0) : null;
      }
    } catch (e) {}
    return {
      selection: sel,
      playhead_s: playhead,
      transcript_near_playhead: transcriptNearPlayhead(),
      pending_broll_count: (typeof pendingBroll !== "undefined" && pendingBroll) ? pendingBroll.length : 0,
      always_photo_match: !!isAlwaysPhotoMatchSession(),
    };
  }

  function timelinePayloadForCoEditor() {
    if (!tl) return null;
    return {
      canvas: tl.canvas,
      fit: tl.fit,
      style: tl.style || {},
      speaker_colors: tl.speaker_colors || {},
      tracks: {
        main: tl.tracks.main,
        overlay: tl.tracks.overlay,
        effects: tl.tracks.effects || [],
        text: tl.tracks.text,
        music: tl.tracks.music,
      },
    };
  }

  function resolveShotIndex(val) {
    if (val == null || val === "") return -1;
    if (typeof val === "number" && Number.isFinite(val)) return Math.trunc(val);
    const s = String(val).trim();
    const m = /^s(\d+)$/i.exec(s);
    if (m) return Math.max(0, Number(m[1]) - 1); // S1 → 0
    const n = Number(s);
    return Number.isFinite(n) ? Math.trunc(n) : -1;
  }

  function resolveOpTrackIndex(op, defaultTrack) {
    let track = op.track || defaultTrack || "main";
    const wantsSelected = op.target === "selected"
      || op.index === "selected"
      || op.index === -1
      || op.use_selection === true;
    if (wantsSelected) {
      if (!selected) return { track, index: -1 };
      if (op.track && selected.track !== op.track) return { track: op.track, index: -1 };
      track = selected.track;
      const arr = (tl.tracks[track] || []);
      return { track, index: arr.findIndex((c) => c.id === selected.id) };
    }
    return { track, index: Number(op.index) };
  }

  function coerceHexColor(val, fallback) {
    if (val == null || val === "") return fallback;
    let s = String(val).trim();
    if (/^#[0-9a-fA-F]{6}$/.test(s)) return s.toUpperCase();
    if (/^[0-9a-fA-F]{6}$/.test(s)) return ("#" + s).toUpperCase();
    const named = {
      white: "#FFFFFF", black: "#000000", yellow: "#FFD60A", gold: "#FFD700",
      cyan: "#00E5FF", red: "#FF0055", green: "#00FF88", blue: "#3B82F6",
      pink: "#EC4899", orange: "#F97316", purple: "#8B5CF6",
    };
    const key = s.toLowerCase().replace(/\s+/g, "");
    if (named[key]) return named[key];
    return fallback;
  }

  async function applyCoEditorOps(ops) {
    if (!tl || !Array.isArray(ops) || !ops.length) return { applied: 0, notes: [] };
    pushHistory();
    historySuspended = true;
    let applied = 0;
    const notes = [];
    try {
    for (const op of ops) {
      const name = op && op.op;
      if (!name) continue;
      try {
        if (name === "set_caption_style") {
          const prev = tl.style || {};
          const patch = {
            font: op.font || op.font_name || prev.font || prev.font_name,
            size: op.size != null ? Number(op.size)
              : (op.font_size != null ? Number(op.font_size) : (prev.size != null ? prev.size : prev.font_size)),
            primary: coerceHexColor(
              op.primary || op.primary_color,
              prev.primary || prev.primary_color || "#FFFFFF"
            ),
            highlight: coerceHexColor(
              op.highlight || op.highlight_color,
              prev.highlight || prev.highlight_color || "#FFD60A"
            ),
            accent: coerceHexColor(
              op.accent || op.accent_color,
              prev.accent || prev.accent_color || "#00FF88"
            ),
          };
          if (op.group != null || op.group_size != null) {
            patch.group = Number(op.group != null ? op.group : op.group_size) || 3;
          }
          if (op.position_y != null) patch.position_y = Number(op.position_y);
          if (op.punchword_emphasis != null) patch.punchword_emphasis = !!op.punchword_emphasis;
          tl.style = normalizeTlStyle(Object.assign({}, prev, patch));
          try { if (typeof syncStyleToCaptionLook === "function") syncStyleToCaptionLook(tl.style); } catch (e) {}
          notes.push("caption style");
          applied++;
        } else if (name === "set_speaker_colors") {
          tl.speaker_colors = Object.assign({}, tl.speaker_colors || {});
          const host = coerceHexColor(op.SPEAKER_00 || op.host || op.Host, null);
          const guest = coerceHexColor(op.SPEAKER_01 || op.guest || op.Guest, null);
          if (host) tl.speaker_colors.SPEAKER_00 = host;
          if (guest) tl.speaker_colors.SPEAKER_01 = guest;
          if (op.colors && typeof op.colors === "object") {
            Object.keys(op.colors).forEach((k) => {
              const hx = coerceHexColor(op.colors[k], null);
              if (hx) tl.speaker_colors[k] = hx;
            });
          }
          notes.push("speaker colors");
          applied++;
        } else if (name === "delete_shot") {
          const { index: i } = resolveOpTrackIndex(op, "main");
          if (i >= 0 && i < tl.tracks.main.length) {
            const id = tl.tracks.main[i].id;
            deleteClip("main", id);
            applyAnchors();
            notes.push(`deleted shot ${i}`);
            applied++;
          }
        } else if (name === "delete_clip") {
          const { track, index: i } = resolveOpTrackIndex(op, op.track || "overlay");
          const arr = tl.tracks[track] || [];
          if (i >= 0 && i < arr.length && ["main", "overlay", "music", "text", "effects"].includes(track)) {
            deleteClip(track, arr[i].id);
            notes.push(`deleted ${track}[${i}]`);
            applied++;
          }
        } else if (name === "set_transition") {
          const { index: i } = resolveOpTrackIndex(op, "main");
          if (i >= 0 && i < tl.tracks.main.length) {
            tl.tracks.main[i].transition = {
              type: op.type || "crossfade",
              duration: Number(op.duration) || 0.3,
            };
            applied++;
          }
        } else if (name === "enable_punch_zoom") {
          const { index: i } = resolveOpTrackIndex(op, "main");
          if (i >= 0 && i < tl.tracks.main.length) {
            tl.tracks.main[i].punch_zoom = { enabled: true, intensity: op.intensity || "med" };
            applied++;
          }
        } else if (name === "enable_ken_burns") {
          const resolved = resolveOpTrackIndex(op, op.track === "overlay" ? "overlay" : "main");
          const track = resolved.track === "overlay" ? "overlay" : "main";
          const i = resolved.index;
          const arr = tl.tracks[track] || [];
          if (i >= 0 && i < arr.length) {
            arr[i].ken_burns = {
              enabled: true,
              intensity: op.intensity || "med",
              direction: op.direction || "in",
            };
            notes.push(`Ken Burns on ${track}[${i}]`);
            applied++;
          }
        } else if (name === "clear_effects") {
          const resolved = resolveOpTrackIndex(op, op.track === "overlay" ? "overlay" : "main");
          const track = resolved.track === "overlay" ? "overlay" : "main";
          const i = resolved.index;
          const arr = tl.tracks[track] || [];
          if (i >= 0 && i < arr.length) {
            if (track === "main") {
              arr[i].punch_zoom = null;
              arr[i].ken_burns = null;
            } else {
              arr[i].ken_burns = null;
            }
            applied++;
          }
        } else if (name === "set_canvas") {
          const c = String(op.canvas || "").replace(":", "x");
          if (["9x16", "16x9", "1x1", "4x5"].includes(c)) {
            tl.canvas = c;
            if ($("tlCanvas")) $("tlCanvas").value = c;
            applyStage();
            applied++;
          }
        } else if (name === "set_fit") {
          const fit = String(op.fit || "").toLowerCase();
          if (fit === "cover" || fit === "contain") {
            tl.fit = fit;
            if ($("tlFit")) $("tlFit").value = fit;
            applyStage();
            notes.push("fit " + fit);
            applied++;
          }
        } else if (name === "set_color_grade") {
          const { index: i } = resolveOpTrackIndex(op, "main");
          if (i >= 0 && i < tl.tracks.main.length) {
            // Render + preview read `color` (not the legacy `color_grade` alias).
            const grade = { preset: op.preset || "warm" };
            tl.tracks.main[i].color = grade;
            tl.tracks.main[i].color_grade = grade;
            notes.push(`color grade shot ${i}`);
            applied++;
          }
        } else if (name === "add_title") {
          const tc = {
            id: uid(),
            text: String(op.text || "Title").slice(0, 120),
            start: Number(op.start) || 0,
            out: Number(op.duration) || 3,
            x: 0.5, y: 0.15, size: 60, color: "#FFFFFF", font: "Anton",
            bg_enabled: true, bg_color: "#000000", bg_opacity: 0.5,
            outline_color: "#000000", outline_width: 0, shadow: 0,
            bold: true, align: 2, anim: "fade",
          };
          reanchor(tc);
          tl.tracks.text.push(tc);
          applied++;
        } else if (name === "set_text") {
          const { index: i } = resolveOpTrackIndex(op, "text");
          const arr = tl.tracks.text || [];
          if (i >= 0 && i < arr.length && op.text != null) {
            arr[i].text = String(op.text).slice(0, 200);
            notes.push("title text");
            applied++;
          }
        } else if (name === "apply_recommended_cuts") {
          const { index: i } = resolveOpTrackIndex(op, "main");
          if (i >= 0 && i < tl.tracks.main.length) {
            const clip = tl.tracks.main[i];
            const res = await fetch("/recommended-cuts", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                job_id: clip.source_job_id,
                in: clip.in || 0,
                out: clip.out,
              }),
            });
            const data = await res.json();
            if (!data.error) {
              clip.cuts = data.cuts || [];
              applied++;
            }
          }
        } else if (name === "merge_shots") {
          const { index: i } = resolveOpTrackIndex(op, "main");
          if (i >= 0 && i < tl.tracks.main.length - 1) {
            selectClip("main", tl.tracks.main[i].id);
            mergeSelectedWithNext();
            applyAnchors();
            applied++;
          }
        } else if (name === "reorder_shot") {
          const from = resolveShotIndex(op.from != null ? op.from : op.from_index);
          const to = resolveShotIndex(op.to != null ? op.to : op.to_index);
          if (from >= 0 && from < tl.tracks.main.length && to >= 0 && to < tl.tracks.main.length && from !== to) {
            const [item] = tl.tracks.main.splice(from, 1);
            tl.tracks.main.splice(to, 0, item);
            applyAnchors();
            notes.push(`reordered S${from + 1} → pos ${to + 1}`);
            applied++;
          }
        } else if (name === "swap_shot") {
          const a = resolveShotIndex(op.a != null ? op.a : op.from);
          const b = resolveShotIndex(op.b != null ? op.b : op.to);
          if (a >= 0 && b >= 0 && a < tl.tracks.main.length && b < tl.tracks.main.length && a !== b) {
            const tmp = tl.tracks.main[a];
            tl.tracks.main[a] = tl.tracks.main[b];
            tl.tracks.main[b] = tmp;
            applyAnchors();
            notes.push(`swapped S${a + 1} ↔ S${b + 1}`);
            applied++;
          }
        } else if (name === "set_overlay_layout") {
          const { index: i } = resolveOpTrackIndex(op, "overlay");
          const arr = tl.tracks.overlay || [];
          let layout = String(op.layout || op.layout_id || "pip_tr");
          if (layout === "full_bleed") layout = "full";
          if (layout === "center_match") layout = "center";
          if (i >= 0 && i < arr.length && OVERLAY_LAYOUTS[layout]) {
            // applyOverlayLayout pushes history — suspended so skip via direct set
            const L = OVERLAY_LAYOUTS[layout];
            const clip = arr[i];
            clip.layout = layout;
            clip.x = L.x; clip.y = L.y; clip.w = L.w; clip.h = L.h; clip.fit = L.fit;
            notes.push("overlay " + layout);
            applied++;
          }
        } else if (name === "set_music") {
          const { index: i } = resolveOpTrackIndex(op, "music");
          const arr = tl.tracks.music || [];
          if (i >= 0 && i < arr.length) {
            if (op.gain_db != null) arr[i].gain_db = Number(op.gain_db);
            if (op.duck != null) arr[i].duck = !!op.duck;
            notes.push("music");
            applied++;
          }
        } else if (name === "apply_clip_style") {
          const styleId = op.style || op.style_id || op.id;
          if (styleId && typeof window.applyClipStyle === "function") {
            const ok = window.applyClipStyle(String(styleId));
            if (ok) {
              notes.push("clip style " + styleId);
              applied++;
            }
          }
        } else if (name === "suggest_broll") {
          if (op.mode && $("tlBrollMode")) {
            const modeEl = $("tlBrollMode");
            const opt = modeEl.querySelector(`option[value="${op.mode}"]:not([disabled])`);
            if (opt) modeEl.value = op.mode;
          }
          if (op.placement && $("tlBrollPlacement")) $("tlBrollPlacement").value = op.placement;
          if (op.scope && $("tlBrollScope")) $("tlBrollScope").value = op.scope;
          if (op.use_ai != null && $("tlBrollAiPhotos")) $("tlBrollAiPhotos").checked = !!op.use_ai;
          await suggestKeywordOverlays();
          notes.push("B-roll suggestions queued");
          applied++;
        } else if (name === "accept_all_broll") {
          const asMain = op.as_main === true || op.lane === "main" || op.track === "main";
          const n = pendingBroll.length;
          if (n) {
            if (asMain) await acceptAllPendingBrollAsMain();
            else await acceptAllPendingBroll();
            notes.push(asMain ? `accepted ${n} → Main` : `accepted ${n} → Overlay`);
            applied++;
          } else {
            notes.push("no pending B-roll");
          }
        } else if (name === "skip_all_broll") {
          const n = pendingBroll.length;
          if (n) {
            skipAllPendingBroll();
            notes.push(`skipped ${n} B-roll`);
            applied++;
          }
        } else if (name === "split_at_playhead") {
          splitAtPlayhead();
          notes.push("split at playhead");
          applied++;
        } else if (name === "run_polish") {
          const polishOpts = {
            pacing: op.pacing || "fast",
            broll_mode: op.broll_mode || op.broll || "pip",
            face_reframe: op.face_reframe !== false,
            cut_stumbles: op.cut_stumbles !== false,
            lower_thirds: op.lower_thirds === true,
            export_edl: op.export_edl !== false,
          };
          if (op.keywords) {
            polishOpts.keywords = Array.isArray(op.keywords)
              ? op.keywords
              : String(op.keywords).split(/[,;]+/).map((s) => s.trim()).filter(Boolean);
          }
          if (op.width) polishOpts.width = Number(op.width) || 1920;
          if (op.height) polishOpts.height = Number(op.height) || 1080;
          if (op.fps) polishOpts.fps = Number(op.fps) || 60;
          // Fire-and-poll; don't block the rest of ops on encode finish.
          runTimelinePolish(polishOpts).catch((e) => {
            console.warn("[co-editor] polish failed", e);
          });
          notes.push("Polish started (" + polishOpts.pacing + ")");
          applied++;
        }
      } catch (e) {
        console.warn("[co-editor] op failed", name, e);
      }
    }
    } finally {
      historySuspended = false;
    }
    applyAnchors();
    renderTimeline();
    updateStageCompositor();
    try {
      const v = $("tlPreviewVideo");
      const ot = playheadOutputTime();
      updateLiveCaptions(ot != null ? ot : (v ? v.currentTime || 0 : 0));
    } catch (e) {}
    scheduleSave();
    if (typeof window.refreshMobileContextTools === "function") window.refreshMobileContextTools();
    return { applied, notes };
  }

  async function sendCoEditorPrompt() {
    if (!tl) {
      alert("Open a timeline project first.");
      return;
    }
    const input = $("coEditorInput");
    const prompt = (input && input.value || "").trim();
    if (!prompt) return;
    if (input) input.value = "";
    appendCoMsg("user", prompt);
    appendCoMsg("bot", "Thinking…");
    const log = $("coEditorLog");
    const thinking = log && log.lastChild;
    try {
      const res = await fetch("/co-editor", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt,
          timeline: timelinePayloadForCoEditor(),
          context: coEditorClientContext(),
        }),
      });
      const data = await res.json();
      if (thinking) thinking.remove();
      if (data.error) throw new Error(data.error);
      const result = await applyCoEditorOps(data.ops || []);
      const n = typeof result === "number" ? result : (result && result.applied) || 0;
      const notes = (result && result.notes) || [];
      let msg = data.message || (n ? `Applied ${n} edit(s).` : "No edits applied.");
      if (n && notes.length) msg += " · " + notes.join(", ");
      if (n) msg += " · Click ▶ Render to burn into the export.";
      appendCoMsg("bot", msg);
    } catch (e) {
      if (thinking) thinking.remove();
      appendCoMsg("bot", "Error: " + e.message);
    }
  }

  // Wire new toolbar buttons once DOM is ready (ensureInit also calls wireToolbar).
  function wireCaptionsToolbar() {
    const mergeBtn = $("tlMergeBtn");
    if (mergeBtn && !mergeBtn._wired) {
      mergeBtn._wired = true;
      mergeBtn.onclick = () => mergeSelectedWithNext();
    }
    const stitchBtn = $("tlStitchMainBtn");
    if (stitchBtn && !stitchBtn._wired) {
      stitchBtn._wired = true;
      stitchBtn.onclick = () => stitchMainTrack();
    }
    const delBtn = $("tlDeleteBtn");
    if (delBtn && !delBtn._wired) {
      delBtn._wired = true;
      delBtn.onclick = () => {
        if (!selected) { alert("Select a clip to delete."); return; }
        deleteClip(selected.track, selected.id);
      };
    }
    const shotsBtn = $("tlDetectShotsBtn");
    if (shotsBtn && !shotsBtn._wired) {
      shotsBtn._wired = true;
      shotsBtn.onclick = () => detectShotsOnSelected();
    }
    const coBtn = $("tlCoEditorBtn");
    if (coBtn && !coBtn._wired) {
      coBtn._wired = true;
      coBtn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        const drawer = $("coEditorDrawer");
        const open = drawer && !drawer.classList.contains("hidden") && drawer.style.display !== "none";
        if (open) closeCoEditor();
        else openCoEditor();
      };
    }
    if ($("coEditorClose") && !$("coEditorClose")._wired) {
      $("coEditorClose")._wired = true;
      $("coEditorClose").onclick = () => closeCoEditor();
    }
    if ($("coEditorSend") && !$("coEditorSend")._wired) {
      $("coEditorSend")._wired = true;
      $("coEditorSend").onclick = () => sendCoEditorPrompt();
    }
    if ($("coEditorInput") && !$("coEditorInput")._wired) {
      $("coEditorInput")._wired = true;
      $("coEditorInput").addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); sendCoEditorPrompt(); }
      });
    }
  }
  // Belt-and-suspenders: delegated click in case early wiring missed the button.
  if (!window._tlCoEditorDelegated) {
    window._tlCoEditorDelegated = true;
    document.addEventListener("click", (e) => {
      const t = e.target && e.target.closest && e.target.closest("#tlCoEditorBtn");
      if (!t) return;
      // If direct onclick already handled it, skip; otherwise open.
      if (t._wired) return;
      e.preventDefault();
      openCoEditor();
    });
  }
  wireCaptionsToolbar();
  window.openCoEditor = openCoEditor;
  window.closeCoEditor = closeCoEditor;

  window.listTimelineAudioAssets = function () {
    return (assets || []).filter((a) => a.kind === "audio");
  };
  window.getTimelineMusicClips = function () {
    if (!tl || !tl.tracks) return [];
    return (tl.tracks.music || []).map((c) => {
      const a = assets.find((x) => x.asset_id === c.asset_id);
      return {
        id: c.id,
        asset_id: c.asset_id,
        gain_db: c.gain_db != null ? c.gain_db : -18,
        duck: !!c.duck,
        label: (a && (a.filename || a.keyword)) || c.asset_id || "Music",
      };
    });
  };
  window.addMusicAssetToTimeline = async function (assetId) {
    const a = (assets || []).find((x) => x.asset_id === assetId);
    if (!a) {
      alert("Audio asset not found — upload first.");
      return false;
    }
    await addMusicClip(a);
    if (typeof window.refreshMobileContextTools === "function") window.refreshMobileContextTools();
    return true;
  };
  window.setMusicClipDuck = function (clipId, duckOn) {
    if (!tl) return false;
    const c = findClip("music", clipId);
    if (!c) return false;
    pushHistory();
    c.duck = !!duckOn;
    renderTimeline();
    scheduleSave();
    return true;
  };
  window.triggerTimelineAssetUpload = function (accept) {
    const input = $("tlAssetFile");
    if (!input) return false;
    if (accept) input.setAttribute("accept", accept);
    else input.setAttribute("accept", "video/*,image/*,audio/*");
    input.click();
    // Restore broad accept after pick so Media upload still works for all kinds.
    setTimeout(() => {
      try { input.setAttribute("accept", "video/*,image/*,audio/*"); } catch (e) {}
    }, 1500);
    return true;
  };
  window.restyleSelectedShot = restyleSelectedShot;
})();
