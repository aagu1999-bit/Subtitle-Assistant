/* ============================================================
   Timeline Editor — multi-track video editor (front-end)
   Talks to the /timeline/* + /upload-asset + /source-info routes.
   Self-contained: manages its own state, fetches its own data.
   ============================================================ */
(function () {
  "use strict";

  const TL_BUILD = "editor-build-8";
  console.log("[timeline] " + TL_BUILD + " script loaded");

  const $ = (id) => document.getElementById(id);
  let PPS = 14;            // pixels per second (mutable: timeline zoom)
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
  let leftTab = "media";   // "media" | "transcript"
  let previewingOutput = false;  // true while the rendered result is in preview
  let transcriptWords = null;    // cached words for the open transcript clip

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
      speaker_colors: tl.speaker_colors || { Host: "#FFD700", Guest: "#00E5FF" },
      headline_banner: tl.headline_banner || null,
      track_states: tl.track_states || null,
      tracks: {
        main: tl.tracks.main.map((c) => ({
          id: c.id, source_job_id: c.source_job_id, asset_id: c.asset_id,
          in: c.in, out: c.out, transition: c.transition || null,
          cuts: c.cuts || [], ken_burns: c.ken_burns || null, punch_zoom: c.punch_zoom || null, split: c.split || null,
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
  async function addMainClip(jobId, inS, outS) {
    try {
      if (!(await ensureProject())) return;
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
    if (!(await ensureProject())) return;
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

    if (!tl.track_states) {
      tl.track_states = {
        main: { mute: false, solo: false, lock: false },
        overlay: { mute: false, solo: false, lock: false },
        text: { mute: false, solo: false, lock: false },
        music: { mute: false, solo: false, lock: false }
      };
    }

    ["main", "overlay", "text", "music"].forEach((track) => {
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
          controls.style.gap = "4px";
          controls.style.marginTop = "4px";
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
               ["main", "overlay", "text", "music"].forEach(t => {
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

      const anySolo = ["main", "overlay", "text", "music"].some(t => tl.track_states[t] && tl.track_states[t].solo);
      const isMuted = st.mute || (anySolo && !st.solo);
      lane.style.opacity = isMuted ? "0.4" : "1";

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
    if (track === "main" && c) { setLeftTab("transcript"); renderTranscript(c); }
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

  // Keep B-roll <video> overlays running with the main preview. Only correct
  // the time on real drift — reassigning currentTime every frame re-seeks the
  // decoder and makes the overlay stutter.
  function syncOverlayVideos(ot, playing) {
    for (const o of ovEntries) {
      const want = o.srcIn + Math.max(0, ot - o.start);
      if (Math.abs((o.el.currentTime || 0) - want) > 0.30) {
        try { o.el.currentTime = want; } catch (e) {}
      }
      if (playing && o.el.paused) { o.el.play().catch(() => {}); }
      else if (!playing && !o.el.paused) { o.el.pause(); }
    }
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
    }
    syncOverlayVideos(ot, !v.paused && !v.ended);
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

    let activeMainClip = null;
    for (let i = 0; i < tl.tracks.main.length; i++) {
      const start = mainStart(i);
      const dur = clipDuration(tl.tracks.main[i]);
      if (ot >= start && ot < start + dur) {
        activeMainClip = tl.tracks.main[i];
        break;
      }
    }

    if (activeMainClip && activeMainClip.punch_zoom && activeMainClip.punch_zoom.enabled) {
      const intensity = activeMainClip.punch_zoom.intensity || "med";
      const scale = intensity === "low" ? 1.15 : (intensity === "strong" ? 1.40 : 1.25);
      v.style.transform = `scale(${scale})`;
    } else {
      v.style.transform = "";
    }

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
        let el;
        if (item.source_job_id) {
          el = document.createElement("video");
          el.src = "/raw-upload/" + item.source_job_id;
          el.muted = true;
          el.playsInline = true;
          try { el.currentTime = Math.max(0, (ot - start) + (item.in || 0)); } catch (e) {}
        } else {
          el = document.createElement("img");
          if (item.asset_id) el.src = "/asset/" + item.asset_id;
          else if (item.src) el.src = item.src;
        }
        el.style.position = "absolute";
        el.style.left = (item.x != null ? item.x : 0.5) * 100 + "%";
        el.style.top = (item.y != null ? item.y : 0.1) * 100 + "%";
        el.style.width = (item.w != null ? item.w : 0.3) * 100 + "%";
        el.style.opacity = item.opacity != null ? item.opacity : 1.0;
        el.style.objectFit = "cover";
        el.style.pointerEvents = "auto";
        el.style.cursor = "move";
        
        el.addEventListener("pointerdown", (e) => {
          selectClip("overlay", item.id);
          startBoxDrag(e, "overlay", item, el);
        });

        layer.appendChild(el);
        // B-roll video: the layer is no longer rebuilt every frame, so keep the
        // element playing in step with the main preview instead of freezing on
        // the frame it was created at.
        if (el.tagName === "VIDEO") {
          ovEntries.push({ el, start, srcIn: item.in || 0 });
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
        else if (selected.track === "main" && c.split && c.split.enabled) addSplitGuide(c);
      }
    }
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
      html += propRange("w", "Size (width %)", c.w, 0.05, 2.0, 0.01);
      const fitOpts = [["cover", "Cover / Crop Fill"], ["contain", "Contain / Fit Aspect"], ["fill", "Stretch / Custom Box"]];
      html += propSelect("fit", "Crop & Fit Mode", c.fit || "cover", fitOpts);
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

      // --- Active Speaker Reframe (per clip) ---
      const ref = c.reframe || {};
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">📱 9:16 Active Speaker Reframe</label>`;
      html += propCheck("reframe.enabled", "Enable on this clip", ref.enabled);
      if (ref.enabled) {
        const pOpts = [["active", "Active Speaker"], ["left", "Left Person"], ["right", "Right Person"], ["full", "Wide Shot"]];
        html += `<div class="tl-prop-grid">${propSelect("reframe.top_panel", "Top panel", ref.top_panel || "active", pOpts)}${propSelect("reframe.bottom_panel", "Bottom panel", ref.bottom_panel || "full", pOpts)}</div>`;
      }

      // --- Ken Burns (per clip) ---
      const kb = c.ken_burns || {};
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">🔍 Ken Burns (motion)</label>`;
      html += propCheck("ken_burns.enabled", "Enable on this clip", kb.enabled);
      if (kb.enabled) {
        html += `<div class="tl-prop-grid">${propSelect("ken_burns.direction", "Direction", kb.direction || "in", [["in", "Zoom in (push)"], ["out", "Zoom out (pull)"]])}${propSelect("ken_burns.intensity", "Strength", kb.intensity || "med", [["low", "Subtle"], ["med", "Medium"], ["high", "Strong"]])}</div>`;
      }

      // --- Punch Zoom (per clip) ---
      const pz = c.punch_zoom || {};
      html += `<hr class="tl-sep"><label class="tl-prop-sectlabel">⚡ Punch Zoom</label>`;
      html += propCheck("punch_zoom.enabled", "Enable Punch Zoom on this clip", pz.enabled);
      if (pz.enabled) {
        html += `<div class="tl-prop-grid">${propSelect("punch_zoom.intensity", "Strength", pz.intensity || "med", [["low", "Low (1.15x)"], ["med", "Medium (1.25x)"], ["strong", "Strong (1.40x)"]])}</div>`;
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
        if (!c.color) c.color = {};
        c.color.preset = sw.dataset.preset;
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
      speaker_colors: d.speaker_colors || { Host: "#FFD700", Guest: "#00E5FF" },
      headline_banner: d.headline_banner || null,
      track_states: d.track_states || {
        main: { mute: false, solo: false, lock: false },
        overlay: { mute: false, solo: false, lock: false },
        text: { mute: false, solo: false, lock: false },
        music: { mute: false, solo: false, lock: false },
      },
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
      on("tlCanvas", "onchange", (e) => { if (tl) { tl.canvas = e.target.value; applyStage(); updateStageCompositor(); scheduleSave(); } });
      on("tlFit", "onchange", (e) => { if (tl) { tl.fit = e.target.value; applyStage(); scheduleSave(); } });
      on("tlRenderBtn", "onclick", renderTimelineVideo);
      on("tlAddTitleBtn", "onclick", () => addTitle());
      // Global keyboard shortcuts for timeline (Copy, Cut, Paste, Duplicate, Delete)
      document.addEventListener("keydown", (e) => {
        // Only trigger shortcuts if the Editor tab is active and focus is not inside a text input/textarea
        const isEditorTab = document.querySelector('.main-tab.active[data-tab="editor"]');
        if (!isEditorTab || !tl) return;
        const tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : "";
        if (tag === "input" || tag === "textarea" || e.target.isContentEditable) return;

        const isCmdOrCtrl = e.metaKey || e.ctrlKey;
        if (isCmdOrCtrl && e.key.toLowerCase() === "c") {
          e.preventDefault(); copySelectedClip();
        } else if (isCmdOrCtrl && e.key.toLowerCase() === "x") {
          if (selected) {
            e.preventDefault();
            copySelectedClip();
            deleteClip(selected.track, selected.id);
            setSaveState("Cut clip");
          }
        } else if (isCmdOrCtrl && e.key.toLowerCase() === "v") {
          e.preventDefault(); pasteClip();
        } else if (isCmdOrCtrl && e.key.toLowerCase() === "d") {
          e.preventDefault(); duplicateSelectedClip();
        } else if (e.key === "Delete" || e.key === "Backspace") {
          if (selected) {
            e.preventDefault();
            deleteClip(selected.track, selected.id);
          }
        }
      });

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
        const f = $("tlAssetFile");
        if (f) f.click();
      });

      const timeline = $("tlTimeline");
      if (timeline) timeline.addEventListener("pointerdown", onTimelineMouseDown);
      document.addEventListener("pointermove", onMouseMove);
      document.addEventListener("pointerup", onMouseUp);
      document.addEventListener("pointermove", onBoxMove);
      document.addEventListener("pointerup", onBoxUp);
      wireScrub();
      setLeftTab("media");
      setSaveState(TL_BUILD);
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
