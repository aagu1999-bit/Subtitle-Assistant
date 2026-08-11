/* ============================================================
   Timeline Editor — multi-track video editor (front-end)
   Talks to the /timeline/* + /upload-asset + /source-info routes.
   Self-contained: manages its own state, fetches its own data.
   ============================================================ */
(function () {
  "use strict";

  const TL_BUILD = "studio-editor-build-28-caption-look-split-place";
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
    { id: "split_screen", label: "Split-screen", icon: "⬓" },
    { id: "punch_zoom", label: "Punch zoom", icon: "⚡" },
    { id: "ken_burns", label: "Ken Burns", icon: "🔍" },
    { id: "color", label: "Color grade", icon: "🎨" },
  ];

  // ---- State ----
  let tl = null;           // { job_id, label, canvas, fit, fps, tracks }
  let selected = null;     // { track, id }
  let sources = [];        // [{job_id, filename, ...}]
  let assets = [];         // [{asset_id, kind, duration, ext}]
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
    if (!tl.tracks.main.length) { c.anchor = null; return; }
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
        if (!c.anchor) return;
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
    if (!res.ok) throw new Error((data && data.error) || res.statusText);
    return data;
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
        o.textContent = `${(p.filename || "Untitled").replace(/\.mp4$/, "")} (${p.clip_count} clips)`;
        if (tl && p.job_id === tl.job_id) o.selected = true;
        sel.appendChild(o);
      });
    } catch (e) { /* ignore */ }
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
  function renderSourceList() {
    const wrap = $("tlSourceList");
    if (!wrap) return;
    wrap.innerHTML = "";
    if (!sources.length) {
      wrap.innerHTML = '<p class="muted tl-hint">Upload &amp; transcribe a video first — it\'ll show up here.</p>';
      return;
    }
    sources.forEach((s) => {
      const div = document.createElement("div");
      div.className = "tl-source-item";
      div.innerHTML =
        `<span class="tl-source-name" title="${esc(s.filename || "")}">${esc(s.filename || s.job_id.slice(0, 8))}</span>`;
      const add = document.createElement("button");
      add.className = "tl-chip-btn";
      add.textContent = "➕ Main";
      add.title = "Add full clip to the main track";
      add.onclick = () => addMainClip(s.job_id);
      const ov = document.createElement("button");
      ov.className = "tl-chip-btn";
      ov.textContent = "🎬";
      ov.title = "Add as video overlay / picture-in-picture";
      ov.onclick = () => addOverlayClip({ source_job_id: s.job_id });
      const del = document.createElement("button");
      del.className = "tl-chip-btn tl-chip-danger";
      del.textContent = "✕";
      del.title = "Remove from Media list (and drop timeline clips that use it)";
      del.onclick = (e) => {
        e.stopPropagation();
        removeSourceFromMedia(s);
      };
      div.appendChild(add);
      div.appendChild(ov);
      div.appendChild(del);
      wrap.appendChild(div);
    });
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

  function renderAssetList() {
    const wrap = $("tlAssetList");
    if (!wrap) return;
    wrap.innerHTML = "";
    if (!assets.length) {
      wrap.innerHTML = '<p class="muted tl-hint">No assets yet. Upload a file or Suggest B-roll overlays.</p>';
      return;
    }
    assets.forEach((a) => {
      const div = document.createElement("div");
      div.className = "tl-source-item";
      const icon = a.kind === "audio" ? "🎵" : a.kind === "image" ? "🖼" : "🎞";
      const label = a.filename || a.keyword || `${a.ext || a.kind} ${String(a.asset_id || "").slice(0, 6)}`;
      const thumb = (a.kind === "image" || a.kind === "video")
        ? `<img class="tl-asset-thumb" src="/asset/${a.asset_id}" alt="" loading="lazy">`
        : `<span class="tl-asset-thumb tl-asset-thumb-ph" aria-hidden="true">${icon}</span>`;
      div.innerHTML =
        `${thumb}<span class="tl-source-name" title="${esc(label)}">${esc(label)}${a.duration ? " · " + fmtTime(a.duration) : ""}</span>`;
      if (a.kind === "audio") {
        const m = document.createElement("button");
        m.className = "tl-chip-btn";
        m.textContent = "🎵 Music";
        m.onclick = () => addMusicClip(a);
        div.appendChild(m);
      } else if (a.kind === "video") {
        const o = document.createElement("button");
        o.className = "tl-chip-btn";
        o.textContent = "🎬 Video";
        o.title = "Add as video overlay (PiP / full-bleed)";
        o.onclick = () => addOverlayClip({ asset_id: a.asset_id }, a);
        div.appendChild(o);
      } else {
        const o = document.createElement("button");
        o.className = "tl-chip-btn";
        o.textContent = "🖼 Image";
        o.title = "Add as image overlay / B-roll";
        o.onclick = () => addOverlayClip({ asset_id: a.asset_id }, a);
        div.appendChild(o);
      }
      const del = document.createElement("button");
      del.className = "tl-chip-btn tl-chip-danger";
      del.textContent = "✕";
      del.title = "Delete this asset";
      del.onclick = (e) => {
        e.stopPropagation();
        removeAssetFromMedia(a);
      };
      div.appendChild(del);
      // Clicking the row itself selects/highlights — also jump to Media tab.
      div.addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        setLeftTab("media", { pin: true });
      });
      wrap.appendChild(div);
    });
  }

  function esc(s) {
    return String(s || "").replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // ---- Left column tabs (Transcript / Media) ----
  let leftTabPinned = null; // "media" | "transcript" | null — user choice wins over auto-switch

  function setLeftTab(name, opts) {
    opts = opts || {};
    leftTab = name;
    if (opts.pin) leftTabPinned = name;
    document.querySelectorAll(".tl-lefttab").forEach((b) =>
      b.classList.toggle("active", b.dataset.ltab === name));
    document.querySelectorAll(".tl-leftpanel").forEach((p) =>
      p.classList.toggle("hidden", p.dataset.lpanel !== name));
  }

  function wireLeftTabs() {
    document.querySelectorAll(".tl-lefttab").forEach((b) => {
      b.onclick = () => setLeftTab(b.dataset.ltab, { pin: true });
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
    const src = "/raw-upload/" + clip.source_job_id;
    if (v.getAttribute("src") !== src) {
      v.src = src;
      previewingOutput = false;
      const wrap = v.closest(".tl-preview");
      if (wrap) wrap.classList.add("has-video");
    }
    const t = Math.max(clip.in || 0, Math.min(clip.out || 1e9, Number(w.start) || 0));
    try { v.currentTime = t; } catch (e) { /* ignore */ }
    highlightTranscriptAt(t);
    updatePlayhead();
    updateStageCompositor();
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
        "Click a word to seek · Shift+click to cut/restore · Double-click to rename captions.";
    }
    renderTranscriptWords(clip);
    updateTranscriptToolbar(clip);
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
    transcriptWords.forEach((w, i) => {
      const sp = document.createElement("span");
      const cut = isWordCut(clip, w);
      const renamed = !!(clip.word_overrides && clip.word_overrides[wordOverrideKey(w)]);
      sp.className = "tl-tword"
        + (cut ? " cut" : "")
        + (fillers.has(i) && !cut ? " filler" : "")
        + (renamed ? " renamed" : "");
      sp.textContent = displayWordText(clip, w) + " ";
      sp.dataset.start = w.start;
      sp.dataset.idx = String(i);
      const col = _spkColor(sc, w.speaker);
      if (col && !cut) sp.style.color = col;
      const bits = [];
      if (w.speaker) bits.push(_spkLabel(w.speaker));
      if (renamed) bits.push("renamed");
      if (fillers.has(i)) bits.push("filler");
      bits.push("click seek · ⇧ cut · dbl-click rename");
      sp.title = bits.join(" · ");
      sp.addEventListener("click", (e) => {
        e.preventDefault();
        if (e.shiftKey) toggleWordCut(clip, w);
        else seekTranscriptWord(clip, w);
      });
      sp.addEventListener("dblclick", (e) => {
        e.preventDefault();
        e.stopPropagation();
        beginWordRename(clip, w, sp);
      });
      doc.appendChild(sp);
    });
    updateTranscriptToolbar(clip);
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
      h: ref.h != null ? Number(ref.h) : null,
      opacity: ref.opacity != null ? Number(ref.opacity) : 1.0,
      fit: ref.fit || "cover",
      fade_in: ref.fade_in != null ? Number(ref.fade_in) : 0.15,
      fade_out: ref.fade_out != null ? Number(ref.fade_out) : 0.2,
      border_px: ref.border_px != null ? Number(ref.border_px) : 0,
      layout: ref.layout || null,
      keyword: ref.keyword || null,
      source: ref.source || null,
      ken_burns: ref.ken_burns
        ? Object.assign({}, ref.ken_burns)
        : (ref.source === "photo" || (asset && asset.kind === "image")
          ? { enabled: true, direction: "in", intensity: "med" }
          : null),
    };
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
    const start = opts.start != null ? Number(opts.start) : (v ? (v.currentTime || 0) : 0);
    const dur = opts.out != null ? Number(opts.out) : (
      ftype === "punch_zoom" ? 1.2 : ftype === "ken_burns" ? 4 : ftype === "split_screen" ? 4 : 3
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

  // Inject Effects lane + toolbar button if the HTML template is stale
  // (common when JS cache-busts ahead of a Flask restart / old index.html).
  function ensureEffectsChrome() {
    const tracks = $("tlTracks");
    if (tracks && !document.querySelector('.tl-track[data-track="effects"]')) {
      const row = document.createElement("div");
      row.className = "tl-track";
      row.dataset.track = "effects";
      row.innerHTML =
        '<div class="tl-track-label">✨ Effects</div>' +
        '<div class="tl-track-lane" data-lane="effects"></div>';
      const overlay = tracks.querySelector('.tl-track[data-track="overlay"]');
      const text = tracks.querySelector('.tl-track[data-track="text"]');
      if (overlay && overlay.nextSibling) tracks.insertBefore(row, overlay.nextSibling);
      else if (text) tracks.insertBefore(row, text);
      else tracks.appendChild(row);
      console.log("[timeline] injected missing Effects track into DOM");
    }
    if (!$("tlAddEffectBtn")) {
      const titleBtn = $("tlAddTitleBtn");
      if (titleBtn && titleBtn.parentNode) {
        const btn = document.createElement("button");
        btn.id = "tlAddEffectBtn";
        btn.className = "tl-chip-btn";
        btn.title = "Add a timed effect on the Effects lane (split-screen, punch, Ken Burns, color)";
        btn.textContent = "+ Effect";
        titleBtn.parentNode.insertBefore(btn, titleBtn.nextSibling);
        console.log("[timeline] injected missing + Effect button into DOM");
      }
    }
    const fxBtn = $("tlAddEffectBtn");
    if (fxBtn && !fxBtn.dataset.wired) {
      fxBtn.dataset.wired = "1";
      fxBtn.onclick = () => addEffectClip("punch_zoom");
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
      if (c.shot_index != null) badges += ` S${Number(c.shot_index) + 1}`;
      if (c.ken_burns && c.ken_burns.enabled) badges += " 🔍";
      if (c.punch_zoom && c.punch_zoom.enabled) badges += " ⚡";
      if (c.split && c.split.enabled) badges += " ⬓";
      if (c.cuts && c.cuts.length) badges += " ✂️";
      return `${idx + 1}. ${name.replace(/\.[^.]+$/, "")}${badges}`;
    }
    if (track === "effects") {
      const meta = EFFECT_TYPES.find((t) => t.id === c.type);
      const icon = meta ? meta.icon : "✨";
      const name = meta ? meta.label : (c.type || "Effect");
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

  // ---- Selection + properties ----
  function selectClip(track, id) {
    selected = { track, id };
    const c = findClip(track, id);
    // Choose what the preview shows: the clip's own video, or — for titles /
    // image overlays with no video — the first Main clip as a backdrop so you
    // can still position boxes against real framing.
    let src = null, seekTo = null;
    if (c && c.source_job_id) { src = "/raw-upload/" + c.source_job_id; seekTo = c.in || 0; }
    else if (c && c.asset_id && track !== "music") src = "/asset/" + c.asset_id;
    if (!src) {
      const fm = tl.tracks.main.find((m) => m.source_job_id);
      if (fm) src = "/raw-upload/" + fm.source_job_id;
    }
    if (src) {
      const v = $("tlPreviewVideo");
      const wrap = v.closest(".tl-preview");
      if (v.getAttribute("src") !== src) { v.src = src; previewingOutput = false; }
      wrap.classList.add("has-video");
      if (seekTo != null) { try { v.currentTime = seekTo; } catch (e) {} }
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
    if (!v) return null;
    const t = v.currentTime || 0;
    if (previewingOutput) return t;
    if (selected && selected.track === "main") {
      const idx = tl.tracks.main.findIndex((c) => c.id === selected.id);
      if (idx < 0) return null;
      const c = tl.tracks.main[idx];
      return mainStart(idx) + sourceTimeToLocalOutput(c, t);
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
        if (!c.source_job_id) continue;
        selected = { track: "main", id: c.id };
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
          const steps = soft ? 12 : 7;
          const stepMs = soft ? 40 : 35;
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
      setRenderStatus(cancelled ? "Preview stopped" : "Preview done — Render for exact xfade / captions burn");
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
  const PUNCH_PEAK = { low: 1.15, med: 1.25, high: 1.40, strong: 1.40 };
  const PUNCH_DECAY = 0.45;

  function punchScaleAt(cfg, tRel) {
    const peak = PUNCH_PEAK[cfg.intensity] || PUNCH_PEAK.med;
    const amp = peak - 1;
    if (amp <= 0) return 1;
    const hit = Math.max(0, Number(cfg.hit) || 0);
    if (tRel < hit) return 1;
    const decay = Math.max(0.05, Number(cfg.decay) || PUNCH_DECAY);
    const u = Math.min(1, Math.max(0, (tRel - hit) / decay));
    return 1 + amp * Math.pow(1 - u, 3);
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
    const lanePunch = activeEffectOfType(ot, "punch_zoom");
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

    rafId = (!v.paused && !v.ended) ? requestAnimationFrame(previewFrame) : null;
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
        if (item.h != null) wrap.style.height = (item.h * 100) + "%";
        else wrap.style.aspectRatio = "16 / 9";
        wrap.style.opacity = baseOp * fadeMul;
        wrap.style.overflow = "hidden";
        wrap.style.pointerEvents = "auto";
        wrap.style.cursor = "move";
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

    if (tl.logo && tl.logo.asset_id) {
      const lg = tl.logo;
      const el = document.createElement("img");
      el.style.position = "absolute";
      el.style.left = (lg.x != null ? lg.x : 0.04) * 100 + "%";
      el.style.top = (lg.y != null ? lg.y : 0.04) * 100 + "%";
      el.style.width = (lg.w != null ? lg.w : 0.18) * 100 + "%";
      el.style.opacity = lg.opacity != null ? lg.opacity : 0.9;
      el.style.pointerEvents = "auto";
      el.style.cursor = "move";
      el.src = "/asset/" + lg.asset_id;

      el.addEventListener("pointerdown", (e) => {
        startBoxDrag(e, "logo", lg, el);
      });

      layer.appendChild(el);
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
      box.style.left = (obj.x != null ? obj.x : 0.5) * 100 + "%";
      box.style.top = (obj.y != null ? obj.y : 0.1) * 100 + "%";
      box.style.width = (obj.w != null ? obj.w : 0.3) * 100 + "%";
      if (obj.h != null) box.style.height = (obj.h * 100) + "%";
      else box.style.aspectRatio = "16 / 9";
      const lbl = document.createElement("div");
      lbl.className = "tl-pbox-label";
      lbl.textContent = labelText;
      box.appendChild(lbl);
      const h = document.createElement("div");
      h.className = "tl-pbox-handle";
      box.appendChild(h);
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

  let boxDrag = null;
  function startBoxDrag(e, kind, obj, box) {
    pushHistory();
    const rect = $("tlStage").getBoundingClientRect();
    boxDrag = {
      kind, obj, box, rect,
      isHandle: e.target.classList.contains("tl-pbox-handle"),
      sx: e.clientX, sy: e.clientY,
      ox: obj.x != null ? obj.x : 0.5, oy: obj.y != null ? obj.y : 0.5,
      ow: obj.w != null ? obj.w : 0.3,
    };
    try { box.setPointerCapture(e.pointerId); } catch (err) {}
    e.preventDefault();
    e.stopPropagation();
  }
  function onBoxMove(e) {
    if (!boxDrag) return;
    const { rect, obj, box, isHandle } = boxDrag;
    if (isHandle) {
      const w = boxDrag.ow + (e.clientX - boxDrag.sx) / rect.width;
      obj.w = Math.min(1, Math.max(0.05, w));
      box.style.width = obj.w * 100 + "%";
    } else {
      obj.x = Math.min(1, Math.max(0, boxDrag.ox + (e.clientX - boxDrag.sx) / rect.width));
      obj.y = Math.min(1, Math.max(0, boxDrag.oy + (e.clientY - boxDrag.sy) / rect.height));
      box.style.left = obj.x * 100 + "%";
      box.style.top = obj.y * 100 + "%";
    }
  }
  function onBoxUp() {
    if (!boxDrag) return;
    boxDrag = null;
    renderProps();   // refresh the X/Y/size sliders to the dragged values
    scheduleSave();
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
        html += `<div class="tl-prop-grid">${propSelect("intensity", "Strength", c.intensity || "med", [["low", "Low"], ["med", "Medium"], ["high", "Strong"], ["strong", "Strong"]])}</div>`;
      } else if (c.type === "ken_burns") {
        html += `<div class="tl-prop-grid">${propSelect("direction", "Direction", c.direction || "in", [["in", "Zoom in"], ["out", "Zoom out"]])}${propSelect("intensity", "Strength", c.intensity || "med", [["low", "Subtle"], ["med", "Medium"], ["high", "Strong"]])}</div>`;
      } else if (c.type === "split_screen") {
        const splitOpts = [["", "— pick second video —"]].concat(
          sources.map((s) => [s.job_id, (s.filename || s.job_id.slice(0, 8)).replace(/\.[^.]+$/, "")]));
        html += propSelect("source_job_id", "Second video", c.source_job_id || "", splitOpts);
        html += `<div class="tl-prop-grid">${propSelect("layout", "Layout", c.layout || "stack", [["auto", "Auto"], ["side", "Side by side"], ["stack", "Top / bottom"]])}${propNum("in", "2nd start (s)", c.in || 0, 0, 99999, 0.1)}</div>`;
        const lay = c.layout || "stack";
        const place = c.placement || (lay === "side" ? "second_right" : "second_bottom");
        if (lay === "side") {
          html += propSelect("placement", "Second video goes…", place,
            [["second_left", "Left"], ["second_right", "Right (default)"]]);
        } else {
          html += propSelect("placement", "Second video goes…", place,
            [["second_top", "Top"], ["second_bottom", "Bottom (default)"]]);
        }
        html += `<p class="muted" style="font-size:.72rem">Main stays the other half. Audio always comes from Main. This is two sources side-by-side — different from Ingest <strong>Analyze</strong> (same video, speaker crops).</p>`;
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
      html += `<div class="tl-prop-grid">${propNum("start", "Start (s)", c.start, 0, 99999, 0.1)}${propRange("opacity", "Opacity", c.opacity != null ? c.opacity : 1, 0, 1, 0.05)}</div>`;
      html += `<div class="tl-prop-grid">${propNum("in", "Trim in (s)", c.in, 0, c._max || 99999, 0.1)}${propNum("out", "Trim out (s)", c.out, 0.1, c._max || 99999, 0.1)}</div>`;
      html += `<label class="tl-prop-sectlabel">Layout presets</label>`;
      html += `<div class="tl-layout-presets">`;
      Object.keys(OVERLAY_LAYOUTS).forEach((id) => {
        const L = OVERLAY_LAYOUTS[id];
        const active = c.layout === id ? " active" : "";
        html += `<button type="button" class="tl-chip-btn${active}" data-act="ovlayout" data-layout="${id}">${L.label}</button>`;
      });
      html += `</div>`;
      html += `<div class="tl-prop-grid">${propRange("x", "Position X", c.x != null ? c.x : 0.5, 0, 1, 0.01)}${propRange("y", "Position Y", c.y != null ? c.y : 0.1, 0, 1, 0.01)}</div>`;
      html += `<div class="tl-prop-grid">${propRange("w", "Width", c.w != null ? c.w : 0.34, 0.05, 1.0, 0.01)}${propRange("h", "Height", c.h != null ? c.h : 0.22, 0.05, 1.0, 0.01)}</div>`;
      const fitOpts = [["cover", "Cover / Crop"], ["contain", "Contain / Fit"], ["fill", "Stretch"]];
      html += propSelect("fit", "Fit mode", c.fit || "cover", fitOpts);
      html += `<div class="tl-prop-grid">${propRange("fade_in", "Fade in (s)", c.fade_in != null ? c.fade_in : 0.15, 0, 1.5, 0.05)}${propRange("fade_out", "Fade out (s)", c.fade_out != null ? c.fade_out : 0.2, 0, 1.5, 0.05)}</div>`;
      html += propRange("border_px", "White border (px)", c.border_px != null ? c.border_px : 0, 0, 16, 1);
      // Ken Burns on B-roll / photo overlays (moment inserts — not whole Main).
      const ovKb = c.ken_burns || {};
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">🔍 Ken Burns (B-roll motion)</label>`;
      html += propCheck("ken_burns.enabled", "Slow zoom on this overlay moment", ovKb.enabled);
      if (ovKb.enabled) {
        html += `<div class="tl-prop-grid">${propSelect("ken_burns.direction", "Direction", ovKb.direction || "in", [["in", "Zoom in (push)"], ["out", "Zoom out (pull)"]])}${propSelect("ken_burns.intensity", "Strength", ovKb.intensity || "med", [["low", "Subtle"], ["med", "Medium"], ["high", "Strong"]])}</div>`;
        html += `<p class="muted" style="font-size:.72rem">Best on photo B-roll and short inserts — keeps still frames alive for the beat.</p>`;
      }
    } else { // main
      html += `<div class="tl-prop-grid">${propNum("in", "Trim in (s)", c.in, 0, c._max || 99999, 0.1)}${propNum("out", "Trim out (s)", c.out, 0.1, c._max || 99999, 0.1)}</div>`;
      html += `<div class="tl-prop-inline" style="gap:6px;margin-bottom:10px"><button class="btn btn-secondary" data-act="setin" style="flex:1;font-size:.78rem">⤓ Set IN here</button><button class="btn btn-secondary" data-act="setout" style="flex:1;font-size:.78rem">Set OUT here ⤓</button></div>`;
      html += propCheck("burn_captions", "Burn word-by-word captions (from transcript)", c.burn_captions !== false);
      const trType = (c.transition && c.transition.type) || "";
      html += propSelect("__transition", "Transition into this clip", trType, TRANSITION_OPTS);
      html += `<p class="muted" style="font-size:.72rem">Crossfade from the previous clip. The first clip ignores this.</p>`;

      // --- Text-based editing ---
      const cutCount = (c.cuts || []).length;
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">✂️ Text-based editing</label>`;
      html += `<button class="btn btn-secondary btn-block" data-act="edittext">Edit transcript${cutCount ? ` (${cutCount} cut${cutCount > 1 ? "s" : ""})` : ""}</button>`;
      html += `<p class="muted" style="font-size:.72rem">Strike out words to delete them from the video.</p>`;

      // --- AI effect placement ---
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">✨ AI camera moves</label>`;
      html += `<button class="btn btn-secondary btn-block" data-act="suggestfx">Suggest camera moves</button>`;
      html += `<p class="muted" style="font-size:.72rem">Reads this clip's transcript and proposes timed moves. Apply places them on the <strong>Effects</strong> lane (resize / move freely).</p>`;
      html += `<div id="tlFxList" style="margin-top:8px"></div>`;

      // --- Per-shot AI restyle (Captions shot restyle) ---
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">🎨 AI Edit this shot</label>`;
      html += `<div class="tl-prop-grid">
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
      html += `<button class="btn btn-secondary btn-block" data-act="restyle" style="margin-top:6px">Restyle shot</button>`;
      html += `<p class="muted" style="font-size:.72rem">Re-runs the AI Edit recipe on this shot only (Captions per-shot restyle).</p>`;

      // --- Active Speaker Reframe (per clip) ---
      const ref = c.reframe || {};
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">📱 9:16 Active Speaker Reframe</label>`;
      html += propCheck("reframe.enabled", "Enable on this clip", ref.enabled);
      if (ref.enabled) {
        const pOpts = [["active", "Active Speaker"], ["left", "Left Person"], ["right", "Right Person"], ["full", "Wide Shot"]];
        html += `<div class="tl-prop-grid">${propSelect("reframe.top_panel", "Top panel", ref.top_panel || "active", pOpts)}${propSelect("reframe.bottom_panel", "Bottom panel", ref.bottom_panel || "full", pOpts)}</div>`;
      }

      html += `<hr class="tl-sep"><p class="muted" style="font-size:.72rem">Prefer the <strong>Effects</strong> lane for timed punch / Ken Burns / split / color. Clip-level toggles below still work for whole-shot looks.</p>`;

      // --- Ken Burns (per clip) ---
      const kb = c.ken_burns || {};
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">🔍 Ken Burns (whole clip)</label>`;
      html += propCheck("ken_burns.enabled", "Enable on this clip", kb.enabled);
      if (kb.enabled) {
        html += `<div class="tl-prop-grid">${propSelect("ken_burns.direction", "Direction", kb.direction || "in", [["in", "Zoom in (push)"], ["out", "Zoom out (pull)"]])}${propSelect("ken_burns.intensity", "Strength", kb.intensity || "med", [["low", "Subtle"], ["med", "Medium"], ["high", "Strong"]])}</div>`;
      }

      // --- Punch Zoom (per clip) ---
      const pz = c.punch_zoom || {};
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">⚡ Punch Zoom (whole clip)</label>`;
      html += propCheck("punch_zoom.enabled", "Enable Punch Zoom on this clip", pz.enabled);
      if (pz.enabled) {
        html += `<div class="tl-prop-grid">${propSelect("punch_zoom.intensity", "Strength", pz.intensity || "med", [["low", "Low (1.15x)"], ["med", "Medium (1.25x)"], ["strong", "Strong (1.40x)"]])}</div>`;
      }

      // --- Split-screen (per clip) ---
      const sp = c.split || {};
      const splitOpts = [["", "— pick second video —"]].concat(
        sources.filter((s) => s.job_id !== c.source_job_id).map((s) => [s.job_id, (s.filename || s.job_id.slice(0, 8)).replace(/\.[^.]+$/, "")]));
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">⬓ Split-screen (whole clip)</label>`;
      html += propCheck("split.enabled", "Enable on this clip", sp.enabled);
      if (sp.enabled) {
        html += propSelect("split.source_job_id", "Second video", sp.source_job_id || "", splitOpts);
        html += `<div class="tl-prop-grid">${propSelect("split.layout", "Layout", sp.layout || "stack", [["auto", "Auto"], ["side", "Side by side"], ["stack", "Top / bottom"]])}${propNum("split.in", "2nd start (s)", sp.in || 0, 0, 99999, 0.1)}</div>`;
        const lay = sp.layout || "stack";
        const place = sp.placement || (lay === "side" ? "second_right" : "second_bottom");
        if (lay === "side") {
          html += propSelect("split.placement", "Second video goes…", place,
            [["second_left", "Left"], ["second_right", "Right (default)"]]);
        } else {
          html += propSelect("split.placement", "Second video goes…", place,
            [["second_top", "Top"], ["second_bottom", "Bottom (default)"]]);
        }
      }

      // --- Color grade (per clip) ---
      const col = c.color || {};
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">🎨 Color (whole clip)</label>`;
      html += `<div class="tl-swatches" id="tlSwatches">` +
        COLOR_PRESETS.map(([v, t2]) =>
          `<div class="tl-swatch tl-swatch-${v} ${ (col.preset || "none") === v ? "active" : ""}" data-preset="${v}" title="${t2}">${t2}</div>`
        ).join("") + `</div>`;
      html += `<div class="tl-prop-grid" style="margin-top:8px">${propRange("color.brightness", "Brightness", col.brightness != null ? col.brightness : 0, -0.3, 0.3, 0.02)}${propRange("color.contrast", "Contrast", col.contrast != null ? col.contrast : 1, 0.5, 1.5, 0.02)}</div>`;
      html += propRange("color.saturation", "Saturation", col.saturation != null ? col.saturation : 1, 0, 2, 0.05);
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
    const opts = [["", "— no logo —"]].concat(imgVid.map((a) => [a.asset_id, `${a.kind} · ${a.ext}`]));
    let html = `<h3>⚙ Project</h3>`;
    html += `<p class="muted" style="font-size:.74rem">Select a clip for its settings, or set a logo that stays on the whole video.</p>`;
    html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">🏷 Persistent logo / watermark</label>`;
    if (!imgVid.length) {
      html += `<p class="muted" style="font-size:.74rem">Upload a logo image first (Assets → Upload asset).</p>`;
    }
    html += propSelect("__logo_asset", "Logo image", lg.asset_id || "", opts);
    if (lg.asset_id) {
      html += `<div class="tl-prop-grid">${propRange("__logo_x", "Position X", lg.x != null ? lg.x : 0.04, 0, 1, 0.01)}${propRange("__logo_y", "Position Y", lg.y != null ? lg.y : 0.04, 0, 1, 0.01)}</div>`;
      html += `<div class="tl-prop-grid">${propRange("__logo_w", "Size (width %)", lg.w != null ? lg.w : 0.18, 0.03, 0.6, 0.01)}${propRange("__logo_opacity", "Opacity", lg.opacity != null ? lg.opacity : 0.9, 0.1, 1, 0.05)}</div>`;
    }
    const sc = tl.speaker_colors || {};
    const hb = tl.headline_banner;
    const hbText = typeof hb === "string" ? hb : (hb && hb.text) || "";
    const st = normalizeTlStyle(tl.style || {});
    const primary = st.primary_color || st.primary || "#FFFFFF";
    const highlight = st.highlight_color || st.highlight || "#FFD60A";
    const fontName = st.font_name || st.font || "Anton";
    const fontSize = st.font_size != null ? st.font_size : (st.size != null ? st.size : 64);
    html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">🎨 Captions (from Caption look)</label>`;
    html += `<p class="muted" style="font-size:.74rem;line-height:1.4"><strong>Caption look</strong> is the source of truth for karaoke style (white base + yellow active word, fonts, etc.). Timeline does not keep a second style editor — transfer it over.</p>`;
    html += `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:8px 0;padding:10px;background:#12151e;border:1px solid #2a2f3a;border-radius:8px">
      <span style="font-family:${esc(fontName)},sans-serif;font-weight:900;font-size:1.05rem;letter-spacing:.02em">
        <span style="color:${esc(primary)}">here with.</span>
        <span style="color:${esc(highlight)}"> Vanessa,</span>
      </span>
      <span class="muted" style="font-size:.7rem">${esc(fontName)} · ${fontSize}px</span>
    </div>`;
    html += `<div class="tl-prop-inline" style="gap:6px;margin-bottom:8px;flex-wrap:wrap">
      <button class="btn btn-secondary" data-act="open-caption-look" style="flex:1;font-size:.78rem">🎨 Open Caption look</button>
      <button class="btn btn-primary" data-act="pull-caption-look" style="flex:1;font-size:.78rem;background:linear-gradient(135deg,#9785ff,#6c5cff);color:#fff">⬇ Pull into Timeline</button>
    </div>`;
    if (tl.ai_edit) {
      html += `<p class="muted" style="font-size:.72rem;margin-top:6px">AI Edit: ${esc(tl.ai_edit.style_pack || "")} · ${esc(tl.ai_edit.intensity || "med")}</p>`;
    }
    const spkKeys = Object.keys(sc).filter((k) => /^SPEAKER_\d+$/i.test(k)).sort();
    if (!spkKeys.length) spkKeys.push("SPEAKER_00", "SPEAKER_01");
    html += `<p class="muted" style="font-size:.72rem;margin:8px 0 4px">Speaker tints (Ingest → Analyze). Inactive words use these; the active karaoke word stays Highlight yellow.</p>`;
    html += `<div class="tl-prop-grid">`;
    spkKeys.forEach((key) => {
      const label = key === "SPEAKER_00" ? "Host" : (key === "SPEAKER_01" ? "Guest" : key.replace("SPEAKER_", "Spk "));
      html += `<label>${label} <input type="color" data-key="__sc:${key}" value="${sc[key] || _spkColor({}, key) || "#FFD700"}"></label>`;
    });
    html += `</div>`;
    html += `<label class="tl-prop">Headline<input type="text" data-key="__headline" value="${(hbText || "").replace(/"/g, "&quot;")}" placeholder="Optional banner"></label>`;
    wrap.innerHTML = html;

    const openCap = wrap.querySelector('[data-act="open-caption-look"]');
    if (openCap) openCap.onclick = () => {
      if (typeof window.setActiveTab === "function") window.setActiveTab("branding");
      else {
        const btn = document.querySelector('.main-tab[data-tab="branding"]');
        if (btn) btn.click();
      }
    };
    const pullCap = wrap.querySelector('[data-act="pull-caption-look"]');
    if (pullCap) pullCap.onclick = () => {
      pushHistory();
      if (!pullCaptionLookOntoTimeline(null, { quiet: false })) {
        alert("Open Caption look first and set fonts/colors (Hormozi = white + yellow karaoke), then Pull again.");
      }
    };

    wrap.querySelectorAll("[data-key]").forEach((inp) => {
      const key = inp.dataset.key;
      const ev = inp.tagName === "SELECT" || inp.type === "checkbox" || inp.type === "color" ? "change" : "input";
      inp.addEventListener(ev, () => {
        if (key === "__logo_asset") {
          if (inp.value) tl.logo = Object.assign({ x: 0.04, y: 0.04, w: 0.18, opacity: 0.9 }, tl.logo || {}, { asset_id: inp.value });
          else tl.logo = null;
          renderProps();
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
        } else if (key === "__transition") {
          c.transition = v ? { type: v } : null;
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
  }

  // ---- AI camera moves -------------------------------------------------
  const FX_LABEL = {
    punch_zoom: "🔍 Punch zoom",
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
    if (!clipEl) return;
    const track = clipEl.dataset.track;
    const id = clipEl.dataset.id;
    selectClip(track, id);
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
  async function newProject() {
    try {
      const data = await api("/timeline/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: "Timeline edit" }),
      });
      await openProject(data.job_id);
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
      return;
    }
    const btn = $("tlRenderBtn");
    btn.disabled = true;
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
      pollRender();
    } catch (e) {
      setRenderStatus("Error: " + e.message);
      btn.disabled = false;
    }
  }

  function pollRender() {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        const s = await api("/status/" + tl.job_id);
        if (s.status === "done" && s.output) {
          clearInterval(pollTimer);
          setRenderStatus("Done ✓");
          $("tlRenderBtn").disabled = false;
          const v = $("tlPreviewVideo");
          v.src = "/preview/" + s.output + "?t=" + Date.now();
          previewingOutput = true;   // preview is now the full output (1:1 playhead)
          v.closest(".tl-preview").classList.add("has-video");
          v.load();
          v.play().catch(() => {});
        } else if (s.status === "error") {
          clearInterval(pollTimer);
          setRenderStatus("Error: " + (s.error || "render failed"));
          $("tlRenderBtn").disabled = false;
        } else {
          setRenderStatus(`${s.status || "working"}… ${s.progress || 0}%`);
        }
      } catch (e) {
        clearInterval(pollTimer);
        setRenderStatus("Error: " + e.message);
        $("tlRenderBtn").disabled = false;
      }
    }, 1500);
  }

  function setRenderStatus(s) {
    const el = $("tlRenderStatus");
    if (el) el.textContent = s;
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
    } catch (e) {
      alert("Asset upload failed: " + e.message);
      setSaveState("");
    }
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
      if (v.paused) v.play().catch(() => {});
      else v.pause();
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
        on("tlNewBtn", "onclick", newProject);
        on("tlProjectSelect", "onchange", (e) => { if (e.target.value) openProject(e.target.value); });
        on("tlLabel", "oninput", (e) => { if (tl) { tl.label = e.target.value; scheduleSave(); } });
        on("tlCanvas", "onchange", (e) => { if (tl) { pushHistory(); tl.canvas = e.target.value; applyStage(); updateStageCompositor(); scheduleSave(); } });
        on("tlFit", "onchange", (e) => { if (tl) { pushHistory(); tl.fit = e.target.value; applyStage(); scheduleSave(); } });
        on("tlRenderBtn", "onclick", renderTimelineVideo);
        on("tlAddTitleBtn", "onclick", () => addTitle());
        ensureEffectsChrome();
        on("tlAddEffectBtn", "onclick", () => addEffectClip("punch_zoom"));
        on("tlPlaySeqBtn", "onclick", () => playSequencePreview());
        on("tlSplitBtn", "onclick", () => splitAtPlayhead());
        on("tlCopyBtn", "onclick", () => copySelectedClip());
        on("tlPasteBtn", "onclick", () => pasteClip());
        on("tlDupBtn", "onclick", () => duplicateSelectedClip());
        wireCaptionsToolbar();
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

        // Click / scrub anywhere on ruler to seek playhead
        const ruler = $("tlRuler");
        if (ruler) {
          ruler.addEventListener("pointerdown", (e) => {
            const rect = ruler.getBoundingClientRect();
            const clickX = e.clientX - rect.left;
            const t = Math.max(0, clickX / PPS);
            const v = $("tlPreviewVideo");
            if (v) v.currentTime = t;
            updatePlayhead();
            updateStageCompositor();
          });
        }

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
          const data = await api("/timeline/list");
          if (data.timelines.length) await openProject(data.timelines[0].job_id);
          else await newProject();
        } catch (e) {
          try { await newProject(); } catch (e2) { console.error("[timeline] project init failed", e2); }
        }
      }

      await loadSources();
      await loadAssets();
      await loadProjects();
      console.log("[timeline] " + TL_BUILD + " ready; tl=", !!tl);
    })();

    return _initPromise;
  }

  // Expose for setActiveTab("editor") — both header + main nav entry points.
  window.ensureTimelineInit = ensureInit;
  window.addOverlayClip = addOverlayClip;
  window.addEffectClip = addEffectClip;

  async function suggestKeywordOverlays() {
    if (!(await ensureProject())) return;
    const btn = $("tlAutoOverlaysBtn");
    const modeEl = $("tlBrollMode");
    const placeEl = $("tlBrollPlacement");
    const mode = modeEl ? modeEl.value : "auto";
    const placement = placeEl ? placeEl.value : "pip";
    if (btn) { btn.disabled = true; btn.textContent = mode === "badge" ? "Making badges…" : "Fetching B-roll…"; }
    try {
      let jobId = null;
      if (selected && selected.track === "main") {
        const c = findClip("main", selected.id);
        jobId = c && c.source_job_id;
      }
      if (!jobId && tl.tracks.main[0]) jobId = tl.tracks.main[0].source_job_id;
      if (!jobId && typeof window.currentJobId !== "undefined") jobId = window.currentJobId;
      const body = { budget: 5, mode, placement };
      if (jobId) body.job_id = jobId;
      const data = await api("/fetch-auto-overlays", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const list = data.overlays || [];
      if (!list.length) {
        alert(mode === "photo"
          ? "No photo B-roll found. Check API keys / try Auto or Badges."
          : "No keyword overlay moments found in this transcript.");
        return;
      }
      for (const ov of list) {
        await addOverlayClip(ov);
      }
      await loadAssets();
      setLeftTab("media", { pin: true });
      renderTimeline();
      const st = data.stats || {};
      const bits = [];
      if (st.photo) bits.push(`${st.photo} photo`);
      if (st.badge) bits.push(`${st.badge} badge`);
      setSaveState(`Added ${list.length} B-roll overlay${list.length === 1 ? "" : "s"}` + (bits.length ? ` (${bits.join(", ")})` : "") + " — see Media");
      if (window.StudioLogger) StudioLogger.clip("auto_overlays", `${list.length}:${mode}`);
    } catch (e) {
      alert("Could not suggest overlays: " + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "✨ Suggest B-roll overlays"; }
    }
  }

  // Branding tab → Timeline: caption style, speaker colors, headline, logo.
  window.applyTimelineBranding = function (style, opts) {
    if (!tl) {
      alert("Open or create a Timeline project first.");
      return false;
    }
    pushHistory();
    style = normalizeTlStyle(style || {});
    opts = opts || {};
    tl.style = Object.assign({}, tl.style || {}, style);
    const sc = style.speaker_colors || {};
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
    selected = null;
    renderTimeline();
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

      if (opts.newProject || (seeding && !tl) || opts.seedTimeline) {
        await newProject();
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

      // Single-job seed (Edit range / job row).
      if (seedJobId && !sources.find((s) => s.job_id === seedJobId)) await loadSources();
      if (seedJobId && tl) {
        if (opts.replace) {
          pushHistory();
          tl.tracks.main = [];
          selected = null;
        }
        await addMainClip(seedJobId, opts.in, opts.out);
        seedStyleFromCaptionLook(seedJobId);
        renderTimeline();
        scheduleSave();
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
      `• Overlays / titles keep their timing on the new clip\n\n` +
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
      // Prefer grade from first main clip if any.
      const firstFx = tl.tracks.main.find((c) => c.color || c.color_grade);
      if (firstFx) {
        merged.color = firstFx.color || firstFx.color_grade;
        merged.color_grade = merged.color;
      }
      oldIds.forEach((oid) => reanchorFromClip(oid, merged.id));
      tl.tracks.main = [merged];
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
      pushHistory();
      const idx = tl.tracks.main.findIndex((c) => c.id === clip.id);
      const pieces = shots.map((s, i) => {
        const piece = {
          id: uid(),
          source_job_id: clip.source_job_id,
          in: s.start,
          out: s.end,
          _max: clip._max,
          transition: i < shots.length - 1 ? { type: "crossfade", duration: 0.25 } : null,
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
      appendCoMsg("bot", "I can change caption colors/fonts, speaker colors, canvas, transitions, punch zoom / Ken Burns (Main or Overlay), color grade, titles, cuts, merge/reorder shots. Ask in plain language — then click ▶ Render to bake it in.");
    }
    try { if ($("coEditorInput")) $("coEditorInput").focus(); } catch (e) {}
  }
  function closeCoEditor() {
    const drawer = $("coEditorDrawer");
    if (!drawer) return;
    drawer.classList.add("hidden");
    drawer.style.display = "";
    drawer.setAttribute("aria-hidden", "true");
    const btn = $("tlCoEditorBtn");
    if (btn) btn.classList.remove("active-co");
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
    if (!tl || !Array.isArray(ops) || !ops.length) return 0;
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
          tl.style = normalizeTlStyle(Object.assign({}, prev, patch));
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
          const i = Number(op.index);
          if (i >= 0 && i < tl.tracks.main.length) {
            const id = tl.tracks.main[i].id;
            deleteClip("main", id);
            notes.push(`deleted shot ${i}`);
            applied++;
          }
        } else if (name === "set_transition") {
          const i = Number(op.index);
          if (i >= 0 && i < tl.tracks.main.length) {
            tl.tracks.main[i].transition = {
              type: op.type || "crossfade",
              duration: Number(op.duration) || 0.3,
            };
            applied++;
          }
        } else if (name === "enable_punch_zoom") {
          const i = Number(op.index);
          if (i >= 0 && i < tl.tracks.main.length) {
            tl.tracks.main[i].punch_zoom = { enabled: true, intensity: op.intensity || "med" };
            applied++;
          }
        } else if (name === "enable_ken_burns") {
          const track = (op.track === "overlay") ? "overlay" : "main";
          const i = Number(op.index);
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
          const track = (op.track === "overlay") ? "overlay" : "main";
          const i = Number(op.index);
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
        } else if (name === "set_color_grade") {
          const i = Number(op.index);
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
        } else if (name === "apply_recommended_cuts") {
          const i = Number(op.index);
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
          const i = Number(op.index);
          if (i >= 0 && i < tl.tracks.main.length - 1) {
            selectClip("main", tl.tracks.main[i].id);
            mergeSelectedWithNext();
            applied++;
          }
        } else if (name === "reorder_shot") {
          const from = Number(op.from);
          const to = Number(op.to);
          if (from >= 0 && from < tl.tracks.main.length && to >= 0 && to < tl.tracks.main.length && from !== to) {
            const [item] = tl.tracks.main.splice(from, 1);
            tl.tracks.main.splice(to, 0, item);
            applied++;
          }
        }
      } catch (e) {
        console.warn("[co-editor] op failed", name, e);
      }
    }
    } finally {
      historySuspended = false;
    }
    renderTimeline();
    updateStageCompositor();
    try {
      const v = $("tlPreviewVideo");
      const ot = playheadOutputTime();
      updateLiveCaptions(ot != null ? ot : (v ? v.currentTime || 0 : 0));
    } catch (e) {}
    scheduleSave();
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
      else if (!(data.ops || []).length) msg += " (no ops returned)";
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
  window.restyleSelectedShot = restyleSelectedShot;
})();
