/* ============================================================
   Timeline Editor — multi-track video editor (front-end)
   Talks to the /timeline/* + /upload-asset + /source-info routes.
   Self-contained: manages its own state, fetches its own data.
   ============================================================ */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PPS = 14;          // pixels per second on the timeline
  const MIN_TL_SECONDS = 30;
  const LANE_OFFSET = 0;   // lanes start at x=0 within their container

  // ---- State ----
  let tl = null;           // { job_id, label, canvas, fit, fps, tracks }
  let selected = null;     // { track, id }
  let sources = [];        // [{job_id, filename, ...}]
  let assets = [];         // [{asset_id, kind, duration, ext}]
  const srcDur = {};       // job_id -> duration cache
  let saveTimer = null;
  let pollTimer = null;
  let initialized = false;

  const uid = () => Math.random().toString(36).slice(2, 10);

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

  // ---- Add clips ----
  async function addMainClip(jobId, inS, outS) {
    try {
      const dur = await getSourceDuration(jobId);
      const ci = inS != null ? Math.max(0, inS) : 0;
      const co = outS != null ? Math.min(dur, outS) : dur;
      tl.tracks.main.push({
        id: uid(), source_job_id: jobId, in: ci, out: co > ci ? co : dur,
        _max: dur, transition: null, burn_captions: true,
      });
      renderTimeline();
      scheduleSave();
    } catch (e) {
      alert("Couldn't add clip: " + e.message);
    }
  }

  async function addOverlayClip(ref, asset) {
    let max = 4;
    if (ref.source_job_id) {
      try { max = await getSourceDuration(ref.source_job_id); } catch (e) {}
    } else if (asset && asset.duration) {
      max = asset.duration || 4;
    }
    const out = ref.asset_id && asset && asset.kind === "image" ? 4 : Math.min(max, 5);
    tl.tracks.overlay.push({
      id: uid(), ...ref, in: 0, out, _max: max,
      start: 0, x: 0.62, y: 0.06, w: 0.34, opacity: 1.0,
    });
    selectClip("overlay", tl.tracks.overlay[tl.tracks.overlay.length - 1].id);
    renderTimeline();
    scheduleSave();
  }

  function addMusicClip(asset) {
    const max = asset.duration || 60;
    tl.tracks.music.push({
      id: uid(), asset_id: asset.asset_id, in: 0, out: max, _max: max,
      start: 0, gain_db: -18, duck: true,
    });
    selectClip("music", tl.tracks.music[tl.tracks.music.length - 1].id);
    renderTimeline();
    scheduleSave();
  }

  function addTitle() {
    tl.tracks.text.push({
      id: uid(), text: "Lower third\nName · Title", start: 0, out: 4,
      x: 0.5, y: 0.82, size: 56, color: "#FFFFFF", font: "Anton",
      bg_enabled: true, bg_color: "#000000", bg_opacity: 0.55,
      outline_color: "#000000", outline_width: 0, shadow: 0,
      bold: true, align: 2, anim: "fade",
    });
    selectClip("text", tl.tracks.text[tl.tracks.text.length - 1].id);
    renderTimeline();
    scheduleSave();
  }

  // ---- Timeline rendering ----
  // renderTimeline = redraw lanes AND rebuild the props panel (use on
  // selection / add / delete). renderTracks = redraw only the lanes (use during
  // slider/number edits so the props panel keeps focus).
  function renderTimeline() {
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
      if (v.getAttribute("src") !== src) v.src = src;
      wrap.classList.add("has-video");
      if (seekTo != null) { try { v.currentTime = seekTo; } catch (e) {} }
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

  // ---- Live preview boxes (drag to position titles / overlays / logo) ----
  function renderPreviewBoxes() {
    const layer = $("tlOverlayLayer");
    if (!layer || !tl) return;
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

  // ---- Scrub bar ----
  function wireScrub() {
    const v = $("tlPreviewVideo");
    const scrub = $("tlScrub");
    const playBtn = $("tlPlayBtn");
    if (!v) return;
    playBtn.onclick = () => { v.paused ? v.play().catch(() => {}) : v.pause(); };
    const upd = () => {
      const d = v.duration || 0;
      if (scrub && document.activeElement !== scrub) scrub.value = d ? (v.currentTime / d * 100) : 0;
      $("tlScrubTime").textContent = `${fmtTime(v.currentTime)} / ${fmtTime(d)}`;
      playBtn.textContent = v.paused ? "▶" : "❚❚";
    };
    v.addEventListener("timeupdate", upd);
    v.addEventListener("loadedmetadata", upd);
    v.addEventListener("play", upd);
    v.addEventListener("pause", upd);
    scrub.addEventListener("input", (e) => { if (v.duration) v.currentTime = (e.target.value / 100) * v.duration; });
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
    }

    html += `<button class="tl-del-btn" data-act="del">🗑 Delete clip</button>`;
    wrap.innerHTML = html;
    wireProps(wrap, t, c);
    renderPreviewBoxes();
  }

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
      const handler = () => {
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
    if (et) et.onclick = () => openTextEditor(c);
    const setin = wrap.querySelector('[data-act="setin"]');
    if (setin) setin.onclick = () => {
      const t = $("tlPreviewVideo").currentTime || 0;
      c.in = Math.max(0, Math.min(t, (c.out || 0) - 0.2));
      renderTimeline(); scheduleSave();
    };
    const setout = wrap.querySelector('[data-act="setout"]');
    if (setout) setout.onclick = () => {
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
      clip.cuts = ranges;
      close();
      renderTimeline();
      scheduleSave();
    };
  }

  function deleteClip(track, id) {
    tl.tracks[track] = tl.tracks[track].filter((c) => c.id !== id);
    if (selected && selected.id === id) selected = null;
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
    drag = {
      track, id, c,
      startX: e.clientX,
      mode: handle ? ("resize-" + handle.dataset.side) : "move",
      origIn: c.in || 0, origOut: c.out || 0, origStart: c.start || 0,
      origLeft: parseFloat(clipEl.style.left) || 0,
    };
    e.preventDefault();
  }

  function onMouseMove(e) {
    if (!drag) return;
    const dx = e.clientX - drag.startX;
    const dt = dx / PPS;
    const c = drag.c;
    const max = c._max || 1e9;

    if (drag.mode === "move") {
      if (drag.track === "main") {
        // Reorder by where the cursor lands among main clips.
        reorderMainByX(drag.id, drag.origLeft + dx);
      } else {
        c.start = Math.max(0, drag.origStart + dt);
      }
    } else if (drag.mode === "resize-left") {
      let ni = Math.min(Math.max(0, drag.origIn + dt), drag.origOut - 0.2);
      if (ni < 0) ni = 0;
      c.in = ni;
      if (drag.track !== "main" && drag.track !== "text") {
        // overlay/music: trimming the head shifts visible start too
        c.start = Math.max(0, drag.origStart + (ni - drag.origIn));
      }
    } else if (drag.mode === "resize-right") {
      let no = Math.max(drag.origIn + 0.2, drag.origOut + dt);
      if (drag.track !== "text") no = Math.min(no, max);
      c.out = no;
    }
    renderTracks();
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
      drag = null;
      renderProps();   // reflect final trim/position values in the panel
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
          v.src = "/preview/" + s.output;
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

  // ---- Init (lazy, when the Editor tab is first opened) ----
  async function ensureInit() {
    if (initialized) {
      loadSources();
      loadAssets();
      loadProjects();
      return;
    }
    initialized = true;

    $("tlNewBtn").onclick = newProject;
    $("tlProjectSelect").onchange = (e) => { if (e.target.value) openProject(e.target.value); };
    $("tlLabel").oninput = (e) => { if (tl) { tl.label = e.target.value; scheduleSave(); } };
    $("tlCanvas").onchange = (e) => { if (tl) { tl.canvas = e.target.value; applyStage(); renderPreviewBoxes(); scheduleSave(); } };
    $("tlFit").onchange = (e) => { if (tl) { tl.fit = e.target.value; applyStage(); scheduleSave(); } };
    $("tlRenderBtn").onclick = renderTimelineVideo;
    $("tlAddTitleBtn").onclick = () => { if (tl) addTitle(); else alert("Create or open a project first."); };
    $("tlProjectBtn").onclick = () => { selected = null; renderTimeline(); };
    $("tlAssetBtn").onclick = () => $("tlAssetFile").click();
    $("tlAssetFile").onchange = (e) => { if (e.target.files[0]) uploadAsset(e.target.files[0]); e.target.value = ""; };

    const timeline = $("tlTimeline");
    timeline.addEventListener("mousedown", onTimelineMouseDown);
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    document.addEventListener("pointermove", onBoxMove);
    document.addEventListener("pointerup", onBoxUp);
    wireScrub();

    await loadSources();
    await loadAssets();
    await loadProjects();

    // Open the most recent project, or start a fresh one.
    try {
      const data = await api("/timeline/list");
      if (data.timelines.length) await openProject(data.timelines[0].job_id);
      else await newProject();
    } catch (e) {
      await newProject();
    }
  }

  // Hook the Editor tab button so we init on first open.
  document.addEventListener("DOMContentLoaded", () => {
    const tabBtn = document.querySelector('.main-tab[data-tab="editor"]');
    if (tabBtn) tabBtn.addEventListener("click", ensureInit);
  });

  // Expose an entry point so other parts of the app could open the editor
  // seeded from a specific job in the future.
  window.openTimelineEditor = async function (seedJobId, opts) {
    opts = opts || {};
    const tabBtn = document.querySelector('.main-tab[data-tab="editor"]');
    if (tabBtn) tabBtn.click();
    await ensureInit();
    // Make sure this newly-added source is in the library list.
    if (seedJobId && !sources.find((s) => s.job_id === seedJobId)) await loadSources();
    if (seedJobId && tl) await addMainClip(seedJobId, opts.in, opts.out);
  };
})();
