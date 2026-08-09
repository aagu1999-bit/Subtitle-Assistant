/* ============================================================
   Timeline Editor — multi-track video editor (front-end)
   Talks to the /timeline/* + /upload-asset + /source-info routes.
   Self-contained: manages its own state, fetches its own data.
   ============================================================ */
(function () {
  "use strict";

  const TL_BUILD = "editor-build-11";
  console.log("[timeline] " + TL_BUILD + " script loaded");

  const $ = (id) => document.getElementById(id);
  let PPS = 14;            // pixels per second (mutable: timeline zoom)
  const MIN_TL_SECONDS = 30;
  const LANE_OFFSET = 0;   // lanes start at x=0 within their container
  const MAX_UNDO = 50;
  const SNAP_PX = 10;      // magnetic snap threshold in screen pixels
  const KB_INTENSITY = { low: 0.08, med: 0.14, high: 0.22 };

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
  let seqPreview = null;   // { running, cancel } for main-track cut preview
  let magnetic = true;     // snap clip edges to nearby cuts / playhead
  let liveComposite = true; // show timed overlays/titles while previewing
  let lastCompositeOt = null;
  let musicPlayers = [];   // [{ id, audio, clip }] active during Preview cut
  let liveGradeClipId = null;

  const uid = () => Math.random().toString(36).slice(2, 10);

  // ---- Undo / redo ----
  function snapshotState() {
    return JSON.stringify({
      label: tl.label,
      canvas: tl.canvas,
      fit: tl.fit,
      fps: tl.fps,
      bg: tl.bg || "#000000",
      logo: tl.logo || null,
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
    tl.tracks = d.tracks || { main: [], overlay: [], text: [], music: [] };
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
    return Math.max(0.1, (c.out || 0) - (c.in || 0));
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
    ["overlay", "text", "music"].forEach((k) => {
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
    ["overlay", "text", "music"].forEach((k) => {
      tl.tracks[k].forEach((c) => {
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
      tracks: {
        main: tl.tracks.main.map((c) => ({
          id: c.id, source_job_id: c.source_job_id, asset_id: c.asset_id,
          in: c.in, out: c.out, transition: c.transition || null,
          cuts: c.cuts || [], ken_burns: c.ken_burns || null, split: c.split || null,
          color: c.color || null, burn_captions: c.burn_captions,
        })),
        overlay: tl.tracks.overlay.map((c) => ({ ...c })),
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
      ov.textContent = "🖼";
      ov.title = "Add as overlay / picture-in-picture";
      ov.onclick = () => addOverlayClip({ source_job_id: s.job_id });
      div.appendChild(add);
      div.appendChild(ov);
      wrap.appendChild(div);
    });
  }

  function renderAssetList() {
    const wrap = $("tlAssetList");
    if (!wrap) return;
    wrap.innerHTML = "";
    if (!assets.length) {
      wrap.innerHTML = '<p class="muted tl-hint">No assets yet.</p>';
      return;
    }
    assets.forEach((a) => {
      const div = document.createElement("div");
      div.className = "tl-source-item";
      const icon = a.kind === "audio" ? "🎵" : a.kind === "image" ? "🖼" : "🎞";
      div.innerHTML =
        `<span class="tl-source-name">${icon} ${esc(a.ext)} ${a.duration ? fmtTime(a.duration) : ""}</span>`;
      if (a.kind === "audio") {
        const m = document.createElement("button");
        m.className = "tl-chip-btn";
        m.textContent = "🎵 Music";
        m.onclick = () => addMusicClip(a);
        div.appendChild(m);
      } else {
        const o = document.createElement("button");
        o.className = "tl-chip-btn";
        o.textContent = "🖼 Overlay";
        o.onclick = () => addOverlayClip({ asset_id: a.asset_id }, a);
        div.appendChild(o);
      }
      wrap.appendChild(div);
    });
  }

  function esc(s) {
    return String(s || "").replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // ---- Left column tabs (Transcript / Media) ----
  function setLeftTab(name) {
    leftTab = name;
    document.querySelectorAll(".tl-lefttab").forEach((b) =>
      b.classList.toggle("active", b.dataset.ltab === name));
    document.querySelectorAll(".tl-leftpanel").forEach((p) =>
      p.classList.toggle("hidden", p.dataset.lpanel !== name));
  }

  // ---- Transcript-first editing (inline strike-to-cut) ----
  async function renderTranscript(clip) {
    const doc = $("tlTranscriptDoc");
    const hint = $("tlTranscriptHint");
    if (!doc) return;
    transcriptWords = null;
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
    if (hint) hint.textContent = "Click a word to strike it (cut from video). Click again to restore.";
    renderTranscriptWords(clip);
  }

  function isWordCut(clip, w) {
    return (clip.cuts || []).some(([cs, ce]) =>
      Number(w.start) >= cs - 0.01 && Number(w.end) <= ce + 0.01);
  }

  function renderTranscriptWords(clip) {
    const doc = $("tlTranscriptDoc");
    if (!doc || !transcriptWords) return;
    doc.innerHTML = "";
    transcriptWords.forEach((w) => {
      const sp = document.createElement("span");
      sp.className = "tl-tword" + (isWordCut(clip, w) ? " cut" : "");
      sp.textContent = w.word + " ";
      sp.dataset.start = w.start;
      sp.onclick = () => toggleWordCut(clip, w);
      doc.appendChild(sp);
    });
  }

  function highlightTranscriptAt(t) {
    const doc = $("tlTranscriptDoc");
    if (!doc) return;
    const spans = doc.querySelectorAll(".tl-tword");
    let best = -1;
    spans.forEach((sp, i) => { if (parseFloat(sp.dataset.start) <= t) best = i; });
    spans.forEach((sp, i) => sp.classList.toggle("playing", i === best));
  }

  function toggleWordCut(clip, w) {
    pushHistory();
    const cuts = (clip.cuts || []).slice();
    if (isWordCut(clip, w)) {
      // Remove any cut covering this word.
      clip.cuts = cuts.filter(([cs, ce]) =>
        !(Number(w.start) >= cs - 0.01 && Number(w.end) <= ce + 0.01));
    } else {
      cuts.push([Number(w.start), Number(w.end)]);
      // Merge overlapping/adjacent cuts.
      cuts.sort((a, b) => a[0] - b[0]);
      const merged = [];
      cuts.forEach((r) => {
        const last = merged[merged.length - 1];
        if (last && r[0] <= last[1] + 0.05) last[1] = Math.max(last[1], r[1]);
        else merged.push(r.slice());
      });
      clip.cuts = merged;
    }
    renderTranscriptWords(clip);
    applyAnchors();   // cuts change Main duration → reflow anchored items
    renderTracks();
    scheduleSave();
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
    if (!(await ensureProject())) return;
    pushHistory();
    let max = 4;
    if (ref.source_job_id) {
      try { max = await getSourceDuration(ref.source_job_id); } catch (e) {}
    } else if (asset && asset.duration) {
      max = asset.duration || 4;
    }
    const out = ref.asset_id && asset && asset.kind === "image" ? 4 : Math.min(max, 5);
    const oc = {
      id: uid(), ...ref, in: 0, out, _max: max,
      start: 0, x: 0.62, y: 0.06, w: 0.34, opacity: 1.0,
    };
    reanchor(oc);
    tl.tracks.overlay.push(oc);
    selectClip("overlay", oc.id);
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

  // ---- Timeline rendering ----
  // renderTimeline = redraw lanes AND rebuild the props panel (use on
  // selection / add / delete). renderTracks = redraw only the lanes (use during
  // slider/number edits so the props panel keeps focus).
  function renderTimeline() {
    applyAnchors();   // keep anchored overlays/titles/music attached to Main
    renderTracks();
    renderProps();
  }

  function renderTracks() {
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

    ["main", "overlay", "text", "music"].forEach((track) => {
      const lane = document.querySelector(`.tl-track-lane[data-lane="${track}"]`);
      if (!lane) return;
      lane.innerHTML = "";
      lane.style.minWidth = width + "px";
      tl.tracks[track].forEach((c, idx) => {
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
    }
  }

  function clipLabel(track, c, idx) {
    if (track === "main") {
      const s = sources.find((x) => x.job_id === c.source_job_id);
      const name = s ? (s.filename || "clip") : "clip";
      let badges = "";
      if (c.ken_burns && c.ken_burns.enabled) badges += " 🔍";
      if (c.split && c.split.enabled) badges += " ⬓";
      if (c.cuts && c.cuts.length) badges += " ✂️";
      return `${idx + 1}. ${name.replace(/\.[^.]+$/, "")}${badges}`;
    }
    if (track === "text") return (c.text || "Title").split("\n")[0];
    if (track === "music") return `🎵 music ${fmtTime(clipDuration(c))}`;
    return `🖼 overlay ${fmtTime(clipDuration(c))}`;
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
    // Selecting a Main clip surfaces its transcript for text-based editing.
    if (track === "main" && c) {
      setLeftTab("transcript");
      renderTranscript(c);
      if (!(seqPreview && seqPreview.running)) applyLiveGrade(c);
    } else if (!(seqPreview && seqPreview.running) && c && c.color) {
      applyLiveGrade(c);
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
      return mainStart(idx) + Math.max(0, Math.min(clipDuration(c), t - (c.in || 0)));
    }
    return null;
  }

  function updatePlayhead() {
    const ph = $("tlPlayhead");
    if (!ph) return;
    const ot = playheadOutputTime();
    if (ot == null) { ph.style.display = "none"; return; }
    ph.style.display = "block";
    ph.style.left = (70 + 8 + ot * PPS) + "px";  // 70 label + 8 padding
    const lab = $("tlPlayheadTime");
    if (lab) lab.textContent = fmtTime(ot);
  }

  // ---- Split the selected Main clip at the playhead ----
  function splitAtPlayhead() {
    if (!tl) return;
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

  // Keep-ranges after text cuts — used by Preview cut so struck words are skipped.
  function keepRangesForClip(clip) {
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
    return ranges.length ? ranges : [[cin, cout]];
  }

  function titleDuration(c) {
    // Titles store duration in `out` (in is unused / 0).
    if (c.in == null || c.in === 0) return Math.max(0.2, c.out || 4);
    return clipDuration(c);
  }

  function assetKind(assetId) {
    const a = assets.find((x) => x.asset_id === assetId);
    return a ? a.kind : null;
  }

  // Timed multi-track composite on the preview stage (overlays / titles / logo).
  // Not a full FFmpeg substitute (no grades/transitions/audio duck), but enough
  // to judge placement and timing while editing.
  function updateLiveComposite(ot) {
    const layer = $("tlOverlayLayer");
    if (!layer || !tl || ot == null || !liveComposite) return;
    lastCompositeOt = ot;
    layer.innerHTML = "";

    if (tl.logo && tl.logo.asset_id) {
      addLiveLayerItem("logo", tl.logo, "Logo", { interactive: !(seqPreview && seqPreview.running) });
    }

    (tl.tracks.overlay || []).forEach((c) => {
      const start = c.start || 0;
      if (ot >= start - 0.001 && ot < start + clipDuration(c)) {
        addLiveLayerItem("overlay", c, "Overlay", { interactive: !(seqPreview && seqPreview.running), media: true, mediaTime: (c.in || 0) + (ot - start) });
      }
    });

    (tl.tracks.text || []).forEach((c) => {
      const start = c.start || 0;
      if (ot >= start - 0.001 && ot < start + titleDuration(c)) {
        addLiveTitleItem(c, { interactive: !(seqPreview && seqPreview.running) });
      }
    });

    // Music presence chip (audio itself is mixed during Preview cut).
    const musicOn = (tl.tracks.music || []).some((c) => {
      const start = c.start || 0;
      return ot >= start && ot < start + clipDuration(c);
    });
    if (musicOn) {
      const chip = document.createElement("div");
      chip.className = "tl-music-chip";
      chip.textContent = (seqPreview && seqPreview.running) ? "🎵 music playing" : "🎵 music";
      layer.appendChild(chip);
    }
  }

  function addLiveLayerItem(kind, obj, labelText, opts) {
    opts = opts || {};
    const layer = $("tlOverlayLayer");
    if (!layer) return;
    const box = document.createElement("div");
    box.className = "tl-pbox tl-live-box";
    box.style.left = (obj.x != null ? obj.x : 0.5) * 100 + "%";
    box.style.top = (obj.y != null ? obj.y : 0.1) * 100 + "%";
    box.style.width = (obj.w != null ? obj.w : 0.3) * 100 + "%";
    box.style.aspectRatio = "16 / 9";
    if (obj.opacity != null) box.style.opacity = String(obj.opacity);

    if (opts.media) {
      let mediaEl = null;
      if (obj.asset_id) {
        const kindA = assetKind(obj.asset_id);
        if (kindA === "image") {
          mediaEl = document.createElement("img");
          mediaEl.src = "/asset/" + obj.asset_id;
        } else {
          mediaEl = document.createElement("video");
          mediaEl.src = "/asset/" + obj.asset_id;
          mediaEl.muted = true;
          mediaEl.playsInline = true;
        }
      } else if (obj.source_job_id) {
        mediaEl = document.createElement("video");
        mediaEl.src = "/raw-upload/" + obj.source_job_id;
        mediaEl.muted = true;
        mediaEl.playsInline = true;
      }
      if (mediaEl) {
        mediaEl.className = "tl-live-media";
        box.appendChild(mediaEl);
        if (mediaEl.tagName === "VIDEO" && opts.mediaTime != null) {
          const seek = () => {
            try { mediaEl.currentTime = Math.max(0, opts.mediaTime); } catch (e) {}
          };
          if (mediaEl.readyState >= 1) seek();
          else mediaEl.addEventListener("loadedmetadata", seek, { once: true });
        }
      }
    }

    const lbl = document.createElement("div");
    lbl.className = "tl-pbox-label";
    lbl.textContent = labelText;
    box.appendChild(lbl);

    if (opts.interactive !== false) {
      const h = document.createElement("div");
      h.className = "tl-pbox-handle";
      box.appendChild(h);
      box.addEventListener("pointerdown", (e) => startBoxDrag(e, kind, obj, box));
    } else {
      box.style.pointerEvents = "none";
    }
    layer.appendChild(box);
  }

  function addLiveTitleItem(c, opts) {
    opts = opts || {};
    const layer = $("tlOverlayLayer");
    if (!layer) return;
    const box = document.createElement("div");
    box.className = "tl-pbox title tl-live-title";
    box.style.left = (c.x != null ? c.x : 0.5) * 100 + "%";
    box.style.top = (c.y != null ? c.y : 0.82) * 100 + "%";
    box.style.color = c.color || "#FFFFFF";
    box.style.fontFamily = c.font || "Anton, sans-serif";
    box.style.fontSize = Math.max(12, Math.min(42, (c.size || 56) * 0.35)) + "px";
    box.style.fontWeight = c.bold === false ? "500" : "700";
    if (c.bg_enabled) {
      const op = c.bg_opacity != null ? c.bg_opacity : 0.55;
      box.style.background = hexToRgba(c.bg_color || "#000000", op);
    }
    box.textContent = c.text || "Title";
    if (opts.interactive !== false) {
      box.addEventListener("pointerdown", (e) => startBoxDrag(e, "title", c, box));
    } else {
      box.style.pointerEvents = "none";
    }
    layer.appendChild(box);
  }

  function hexToRgba(hex, alpha) {
    const h = String(hex || "#000000").replace("#", "");
    const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
    const n = parseInt(full, 16);
    if (!Number.isFinite(n)) return `rgba(0,0,0,${alpha})`;
    const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    return `rgba(${r},${g},${b},${alpha})`;
  }

  function refreshCompositeFromPreview() {
    if (!liveComposite || !tl) return;
    if (previewingOutput) {
      const v = $("tlPreviewVideo");
      if (v) updateLiveComposite(v.currentTime || 0);
      return;
    }
    const ot = playheadOutputTime();
    if (ot != null) updateLiveComposite(ot);
  }

  // ---- Live grades / Ken Burns / transitions / music ----
  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function dbToLinear(db) {
    const lin = Math.pow(10, (Number(db) || 0) / 20);
    return Math.max(0, Math.min(1, lin));
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

  function applyLiveGrade(clip) {
    const v = $("tlPreviewVideo");
    if (!v) return;
    v.style.filter = cssFilterForColor(clip && clip.color);
    liveGradeClipId = clip ? clip.id : null;
  }

  function updateKenBurnsProgress(clip, progress01) {
    const v = $("tlPreviewVideo");
    if (!v) return;
    const kb = clip && clip.ken_burns;
    if (!kb || !kb.enabled) {
      // Don't clear transform if a slide transition class is active.
      const stage = $("tlStage");
      if (stage && (stage.classList.contains("tl-slide-left") || stage.classList.contains("tl-slide-right"))) return;
      v.style.transform = "";
      return;
    }
    const intensity = KB_INTENSITY[kb.intensity || "med"] || KB_INTENSITY.med;
    const p = Math.max(0, Math.min(1, progress01));
    const scale = (kb.direction === "out")
      ? (1 + intensity) * (1 - p) + 1 * p
      : 1 * (1 - p) + (1 + intensity) * p;
    v.style.transition = "none";
    v.style.transform = `scale(${scale})`;
  }

  function clearLiveVideoFx() {
    const v = $("tlPreviewVideo");
    const stage = $("tlStage");
    const tr = $("tlTransitionLayer");
    if (v) {
      v.style.filter = "";
      v.style.opacity = "";
      v.style.transform = "";
      v.style.transition = "";
    }
    if (stage) stage.classList.remove("tl-slide-left", "tl-slide-right");
    if (tr) {
      tr.className = "tl-transition-layer";
      tr.style.opacity = "";
      tr.style.clipPath = "";
    }
    liveGradeClipId = null;
  }

  async function liveTransitionOut(type) {
    const v = $("tlPreviewVideo");
    const stage = $("tlStage");
    const tr = $("tlTransitionLayer");
    if (!v || !type) return;
    const dur = 0.38;
    if (type === "fade" || type === "dissolve") {
      v.style.transition = `opacity ${dur}s linear`;
      v.style.opacity = "0";
      await sleep(dur * 1000);
      return;
    }
    if (type === "fadeblack") {
      if (tr) {
        tr.className = "tl-transition-layer active";
        await sleep(dur * 1000);
      }
      return;
    }
    if (type === "slideleft" || type === "slideright") {
      if (stage) stage.classList.add(type === "slideleft" ? "tl-slide-left" : "tl-slide-right");
      v.style.transition = `transform ${dur}s ease, opacity ${dur}s ease`;
      await sleep(dur * 1000);
      return;
    }
    if (type === "wipeleft" || type === "circleopen" || type === "radial") {
      if (!tr) return;
      const cls = type === "wipeleft" ? "tl-tr-wipeleft" : (type === "radial" ? "tl-tr-radial" : "tl-tr-circleopen");
      tr.className = "tl-transition-layer " + cls;
      // Force reflow then activate.
      void tr.offsetWidth;
      tr.classList.add("active");
      await sleep(450);
      return;
    }
    // Unknown → short fade
    v.style.transition = `opacity ${dur}s linear`;
    v.style.opacity = "0";
    await sleep(dur * 1000);
  }

  async function liveTransitionIn(type) {
    const v = $("tlPreviewVideo");
    const stage = $("tlStage");
    const tr = $("tlTransitionLayer");
    if (!v) return;
    const dur = 0.38;
    if (stage) stage.classList.remove("tl-slide-left", "tl-slide-right");
    if (type === "fadeblack" || type === "wipeleft" || type === "circleopen" || type === "radial") {
      if (tr) {
        tr.className = "tl-transition-layer active";
        v.style.opacity = "1";
        void tr.offsetWidth;
        tr.classList.remove("active");
        await sleep(dur * 1000);
        tr.className = "tl-transition-layer";
      }
      return;
    }
    // fade / dissolve / slide / default: fade video back in
    v.style.opacity = "0";
    v.style.transition = `opacity ${dur}s linear`;
    void v.offsetWidth;
    v.style.opacity = "1";
    await sleep(dur * 1000);
    if (tr) tr.className = "tl-transition-layer";
  }

  function stopMusicPreview() {
    musicPlayers.forEach((p) => {
      try { p.audio.pause(); } catch (e) {}
      try { p.audio.src = ""; } catch (e) {}
    });
    musicPlayers = [];
  }

  function syncMusicAt(ot) {
    if (!tl) return;
    const activeIds = new Set();
    (tl.tracks.music || []).forEach((c) => {
      if (!c.asset_id) return;
      const start = c.start || 0;
      const end = start + clipDuration(c);
      if (ot < start || ot >= end) return;
      activeIds.add(c.id);
      let player = musicPlayers.find((p) => p.id === c.id);
      if (!player) {
        const audio = new Audio("/asset/" + c.asset_id);
        audio.preload = "auto";
        const gain = c.gain_db != null ? Number(c.gain_db) : -18;
        // Duck ≈ extra attenuation under dialogue during live preview.
        audio.volume = dbToLinear(gain + (c.duck ? -8 : 0));
        player = { id: c.id, audio, clip: c };
        musicPlayers.push(player);
      }
      const srcT = (c.in || 0) + (ot - start);
      if (Math.abs((player.audio.currentTime || 0) - srcT) > 0.4) {
        try { player.audio.currentTime = Math.max(0, srcT); } catch (e) {}
      }
      if (player.audio.paused) player.audio.play().catch(() => {});
    });
    // Pause players for clips that are no longer under the playhead.
    musicPlayers.forEach((p) => {
      if (!activeIds.has(p.id) && !p.audio.paused) {
        try { p.audio.pause(); } catch (e) {}
      }
    });
  }

  // Lightweight multi-track cut preview — keep-ranges + overlays/titles +
  // approximate grades / Ken Burns / transitions / music bed.
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
      cancel: () => {
        cancelled = true;
        try { v.pause(); } catch (e) {}
        stopMusicPreview();
      },
    };
    if (btn) btn.textContent = "⏹ Stop";
    setRenderStatus("Previewing cut + grades / transitions / music…");
    previewingOutput = false;
    v.closest(".tl-preview").classList.add("has-video");

    const waitEvent = (el, ev, timeoutMs) => new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        el.removeEventListener(ev, finish);
        resolve();
      };
      el.addEventListener(ev, finish);
      if (timeoutMs) setTimeout(finish, timeoutMs);
    });

    try {
      for (let i = 0; i < tl.tracks.main.length; i++) {
        if (cancelled) break;
        const c = tl.tracks.main[i];
        if (!c.source_job_id) continue;

        const trType = (i > 0 && c.transition && c.transition.type) ? c.transition.type : "";
        if (trType && !cancelled) await liveTransitionOut(trType);

        selected = { track: "main", id: c.id };
        setLeftTab("transcript");
        renderTranscript(c);
        const src = "/raw-upload/" + c.source_job_id;
        if (v.getAttribute("src") !== src) {
          v.src = src;
          await waitEvent(v, "loadedmetadata", 8000);
        }
        if (cancelled) break;

        applyLiveGrade(c);
        if (trType && !cancelled) await liveTransitionIn(trType);
        else {
          v.style.opacity = "1";
          const tr = $("tlTransitionLayer");
          if (tr) tr.className = "tl-transition-layer";
        }

        const ranges = keepRangesForClip(c);
        const totalKeep = ranges.reduce((a, [s, e]) => a + Math.max(0, e - s), 0) || clipDuration(c);
        const baseOut = mainStart(i);
        let played = 0;
        for (let r = 0; r < ranges.length; r++) {
          if (cancelled) break;
          const [start, end] = ranges[r];
          try { v.currentTime = start; } catch (e) {}
          await waitEvent(v, "seeked", 2000);
          if (cancelled) break;
          const playP = v.play();
          if (playP && playP.catch) playP.catch(() => {});
          await new Promise((resolve) => {
            const onTime = () => {
              const srcT = v.currentTime || 0;
              const into = played + Math.max(0, srcT - start);
              const ot = baseOut + into;
              const ph = $("tlPlayhead");
              if (ph) {
                ph.style.display = "block";
                ph.style.left = (70 + 8 + ot * PPS) + "px";
              }
              const lab = $("tlPlayheadTime");
              if (lab) lab.textContent = fmtTime(ot);
              highlightTranscriptAt(srcT);
              updateLiveComposite(ot);
              updateKenBurnsProgress(c, into / totalKeep);
              syncMusicAt(ot);
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
      }
    } finally {
      stopMusicPreview();
      clearLiveVideoFx();
      seqPreview = null;
      if (btn) btn.textContent = "▶ Preview cut";
      setRenderStatus(cancelled ? "Preview stopped" : "Preview done — Render for final captions burn / exact xfade");
      renderTimeline();
    }
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
    ["overlay", "text", "music"].forEach((k) => {
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

  function setZoom(delta) {
    PPS = Math.max(4, Math.min(60, PPS + delta));
    renderTracks();
    updatePlayhead();
  }

  // ---- Live preview boxes (drag to position titles / overlays / logo) ----
  function renderPreviewBoxes() {
    const layer = $("tlOverlayLayer");
    if (!layer || !tl) return;
    // Prefer timed composite whenever we can map a preview time → output time.
    if (liveComposite && !(seqPreview && seqPreview.running)) {
      const ot = previewingOutput
        ? (($("tlPreviewVideo") && $("tlPreviewVideo").currentTime) || 0)
        : playheadOutputTime();
      if (ot != null) {
        updateLiveComposite(ot);
        if (selected && selected.track === "main") {
          const c = findClip("main", selected.id);
          if (c && c.split && c.split.enabled) addSplitGuide(c);
        }
        return;
      }
    }
    layer.innerHTML = "";
    if (!selected) {
      if (tl.logo && tl.logo.asset_id) addPreviewBox("logo", tl.logo, "Logo");
      return;
    }
    const c = findClip(selected.track, selected.id);
    if (!c) return;
    if (selected.track === "text") addPreviewBox("title", c, (c.text || "Title").split("\n")[0]);
    else if (selected.track === "overlay") addPreviewBox("overlay", c, "Overlay");
    else if (selected.track === "main" && c.split && c.split.enabled) addSplitGuide(c);
  }

  function addPreviewBox(kind, obj, labelText) {
    const layer = $("tlOverlayLayer");
    const box = document.createElement("div");
    if (kind === "title") {
      box.className = "tl-pbox title";
      box.style.left = (obj.x != null ? obj.x : 0.5) * 100 + "%";
      box.style.top = (obj.y != null ? obj.y : 0.85) * 100 + "%";
      box.textContent = labelText;
    } else {
      box.className = "tl-pbox";
      box.style.left = (obj.x != null ? obj.x : 0.5) * 100 + "%";
      box.style.top = (obj.y != null ? obj.y : 0.1) * 100 + "%";
      box.style.width = (obj.w != null ? obj.w : 0.3) * 100 + "%";
      box.style.aspectRatio = "16 / 9";
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

  function addSplitGuide(c) {
    const layer = $("tlOverlayLayer");
    let layout = (c.split && c.split.layout) || "auto";
    if (layout === "auto") layout = (tl.canvas === "16x9") ? "side" : "stack";
    const line = document.createElement("div");
    line.style.position = "absolute";
    line.style.background = "rgba(255,255,255,.6)";
    if (layout === "side") { line.style.left = "50%"; line.style.top = "0"; line.style.bottom = "0"; line.style.width = "2px"; }
    else { line.style.top = "50%"; line.style.left = "0"; line.style.right = "0"; line.style.height = "2px"; }
    layer.appendChild(line);
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
      // Highlight the word under the playhead in the transcript doc.
      if (leftTab === "transcript" && transcriptWords) highlightTranscriptAt(v.currentTime);
      if (!(seqPreview && seqPreview.running)) refreshCompositeFromPreview();
    };
    v.addEventListener("timeupdate", upd);
    v.addEventListener("loadedmetadata", upd);
    v.addEventListener("seeked", upd);
    v.addEventListener("play", upd);
    v.addEventListener("pause", upd);
  }

  function findClip(track, id) {
    return tl.tracks[track].find((c) => c.id === id);
  }

  function renderProps() {
    const wrap = $("tlProps");
    if (!wrap) return;
    if (!selected) {
      renderProjectProps(wrap);
      renderPreviewBoxes();
      return;
    }
    const c = findClip(selected.track, selected.id);
    if (!c) {
      wrap.innerHTML = '<div class="tl-props-empty muted">Clip removed.</div>';
      return;
    }
    const t = selected.track;
    let html = `<h3>${({ main: "🎬 Main clip", overlay: "🖼 Overlay", text: "🔤 Title", music: "🎵 Music" })[t]}</h3>`;

    if (t === "text") {
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
      html += `<div class="tl-prop-grid">${propNum("start", "Start (s)", c.start, 0, 99999, 0.1)}${propRange("opacity", "Opacity", c.opacity, 0, 1, 0.05)}</div>`;
      html += `<div class="tl-prop-grid">${propNum("in", "Trim in (s)", c.in, 0, c._max || 99999, 0.1)}${propNum("out", "Trim out (s)", c.out, 0.1, c._max || 99999, 0.1)}</div>`;
      html += `<div class="tl-prop-grid">${propRange("x", "Position X", c.x, 0, 1, 0.01)}${propRange("y", "Position Y", c.y, 0, 1, 0.01)}</div>`;
      html += propRange("w", "Size (width %)", c.w, 0.05, 1, 0.01);
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

      // --- Ken Burns (per clip) ---
      const kb = c.ken_burns || {};
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">🔍 Ken Burns (motion)</label>`;
      html += propCheck("ken_burns.enabled", "Enable on this clip", kb.enabled);
      if (kb.enabled) {
        html += `<div class="tl-prop-grid">${propSelect("ken_burns.direction", "Direction", kb.direction || "in", [["in", "Zoom in (push)"], ["out", "Zoom out (pull)"]])}${propSelect("ken_burns.intensity", "Strength", kb.intensity || "med", [["low", "Subtle"], ["med", "Medium"], ["high", "Strong"]])}</div>`;
      }

      // --- Split-screen (per clip) ---
      const sp = c.split || {};
      const splitOpts = [["", "— pick second video —"]].concat(
        sources.filter((s) => s.job_id !== c.source_job_id).map((s) => [s.job_id, (s.filename || s.job_id.slice(0, 8)).replace(/\.[^.]+$/, "")]));
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">⬓ Split-screen</label>`;
      html += propCheck("split.enabled", "Enable on this clip", sp.enabled);
      if (sp.enabled) {
        html += propSelect("split.source_job_id", "Second video", sp.source_job_id || "", splitOpts);
        html += `<div class="tl-prop-grid">${propSelect("split.layout", "Layout", sp.layout || "auto", [["auto", "Auto"], ["side", "Side by side"], ["stack", "Top / bottom"]])}${propNum("split.in", "2nd start (s)", sp.in || 0, 0, 99999, 0.1)}</div>`;
      }

      // --- Color grade (per clip) ---
      const col = c.color || {};
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">🎨 Color</label>`;
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
        if (!c.color) c.color = {};
        c.color.preset = sw.dataset.preset;
        renderProps(); renderTracks(); scheduleSave();
      };
    });
    renderPreviewBoxes();
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
    wrap.innerHTML = html;

    wrap.querySelectorAll("[data-key]").forEach((inp) => {
      const key = inp.dataset.key;
      const ev = inp.tagName === "SELECT" ? "change" : "input";
      inp.addEventListener(ev, () => {
        if (key === "__logo_asset") {
          if (inp.value) tl.logo = Object.assign({ x: 0.04, y: 0.04, w: 0.18, opacity: 0.9 }, tl.logo || {}, { asset_id: inp.value });
          else tl.logo = null;
          renderProps();
        } else {
          if (!tl.logo) return;
          const field = key.replace("__logo_", "");
          tl.logo[field] = parseFloat(inp.value);
          const outSpan = wrap.querySelector(`[data-out="${key}"]`);
          if (outSpan) outSpan.textContent = (+inp.value).toFixed(2);
        }
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
          c.out = (c.in || 0) + Math.max(0.2, v);
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
      ["overlay", "text", "music"].forEach((k) => {
        tl.tracks[k] = (tl.tracks[k] || []).filter((c) => c.anchor !== id);
      });
    }
    tl.tracks[track] = tl.tracks[track].filter((c) => c.id !== id);
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
      } else if (drag.track === "text") {
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
    refreshCompositeFromPreview();
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
      else if (c) reanchor(c);             // overlay/title/music → re-pin to where it landed
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
      tracks: {
        main: (d.tracks && d.tracks.main) || [],
        overlay: (d.tracks && d.tracks.overlay) || [],
        text: (d.tracks && d.tracks.text) || [],
        music: (d.tracks && d.tracks.music) || [],
      },
    };
    // Restore _max trims by probing main/overlay sources lazily.
    selected = null;
    clearHistory();
    if (seqPreview && seqPreview.running) seqPreview.cancel();
    stopMusicPreview();
    clearLiveVideoFx();
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

  function editorTabActive() {
    const btn = document.querySelector('.main-tab[data-tab="editor"]');
    return !!(btn && btn.classList.contains("active"));
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
  async function ensureInit() {
    if (initialized) {
      loadSources(); loadAssets(); loadProjects();
      return;
    }
    initialized = true;
    console.log("[timeline] " + TL_BUILD + " initializing");

    try {
      on("tlNewBtn", "onclick", newProject);
      on("tlProjectSelect", "onchange", (e) => { if (e.target.value) openProject(e.target.value); });
      on("tlLabel", "oninput", (e) => { if (tl) { tl.label = e.target.value; scheduleSave(); } });
      on("tlCanvas", "onchange", (e) => { if (tl) { pushHistory(); tl.canvas = e.target.value; applyStage(); renderPreviewBoxes(); scheduleSave(); } });
      on("tlFit", "onchange", (e) => { if (tl) { pushHistory(); tl.fit = e.target.value; applyStage(); scheduleSave(); } });
      on("tlRenderBtn", "onclick", renderTimelineVideo);
      on("tlPlaySeqBtn", "onclick", playSequencePreview);
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
      on("tlAddTitleBtn", "onclick", () => addTitle());
      on("tlProjectBtn", "onclick", () => { selected = null; renderTimeline(); });
      on("tlAssetBtn", "onclick", () => { const f = $("tlAssetFile"); if (f) f.click(); });
      on("tlAssetFile", "onchange", (e) => { if (e.target.files[0]) uploadAsset(e.target.files[0]); e.target.value = ""; });
      on("tlSplitBtn", "onclick", splitAtPlayhead);
      on("tlZoomIn", "onclick", () => setZoom(4));
      on("tlZoomOut", "onclick", () => setZoom(-4));
      document.querySelectorAll(".tl-lefttab").forEach((b) =>
        b.onclick = () => setLeftTab(b.dataset.ltab));

      const timeline = $("tlTimeline");
      if (timeline) timeline.addEventListener("pointerdown", onTimelineMouseDown);
      document.addEventListener("pointermove", onMouseMove);
      document.addEventListener("pointerup", onMouseUp);
      document.addEventListener("pointermove", onBoxMove);
      document.addEventListener("pointerup", onBoxUp);
      document.addEventListener("keydown", onEditorKeyDown);
      wireScrub();
      setLeftTab("media");
      setSaveState(TL_BUILD);
      updateHistoryButtons();
    } catch (e) {
      console.error("[timeline] wiring failed", e);
      alert("Editor failed to start (" + TL_BUILD + "): " + e.message + "\nTry a hard refresh (Cmd/Ctrl+Shift+R).");
    }

    // Open/create the project FIRST so `tl` exists before the source list
    // (with its + buttons) renders — otherwise an early click races a null tl.
    try {
      const data = await api("/timeline/list");
      if (data.timelines.length) await openProject(data.timelines[0].job_id);
      else await newProject();
    } catch (e) {
      try { await newProject(); } catch (e2) { console.error("[timeline] project init failed", e2); }
    }

    await loadSources();
    await loadAssets();
    await loadProjects();
    console.log("[timeline] " + TL_BUILD + " ready; tl=", !!tl);
  }

  // Hook the Editor tab button so we init on first open.
  document.addEventListener("DOMContentLoaded", () => {
    const tabBtn = document.querySelector('.main-tab[data-tab="editor"]');
    if (tabBtn) tabBtn.addEventListener("click", ensureInit);
  });

  // Entry point used by Edit / Highlights / Compilation handoffs.
  // opts: { in, out, clips:[{job_id|source_job_id, in|start_time, out|end_time}], replace, newProject }
  window.openTimelineEditor = async function (seedJobId, opts) {
    opts = opts || {};
    const tabBtn = document.querySelector('.main-tab[data-tab="editor"]');
    if (tabBtn) tabBtn.click();
    await ensureInit();
    await loadSources();

    if (opts.newProject) {
      await newProject();
    }

    if (opts.replace && tl) {
      pushHistory();
      tl.tracks.main = [];
      selected = null;
    }

    if (Array.isArray(opts.clips) && opts.clips.length) {
      pushHistory();
      for (const c of opts.clips) {
        const jid = c.job_id || c.source_job_id;
        if (!jid) continue;
        const cin = c.in != null ? c.in : c.start_time;
        const cout = c.out != null ? c.out : c.end_time;
        await addMainClip(jid, cin, cout, { skipHistory: true, skipRender: true });
      }
      renderTimeline();
      scheduleSave();
      setRenderStatus(`Added ${opts.clips.length} clip${opts.clips.length === 1 ? "" : "s"} — ▶ Preview cut to review`);
      return;
    }

    if (seedJobId && tl) await addMainClip(seedJobId, opts.in, opts.out);
  };
})();
