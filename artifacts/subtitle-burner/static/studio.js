/* ============================================================
   Studio flow — Style tab, phrase toolbar, graphic picker,
   Export / QA, dirty-segment bands, structure panel, taste memory.
   Talks to timeline.js only through window.tlAPI.
   ============================================================ */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const T = () => window.tlAPI;

  async function api(url, opts) {
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-json */ }
    if (!res.ok || (data && data.error && !data.clip && !data.items)) {
      throw new Error((data && data.error) || res.statusText);
    }
    return data;
  }
  const post = (url, body) => api(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
  });
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (s) => {
    s = Math.max(0, Number(s) || 0);
    const m = Math.floor(s / 60);
    return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
  };
  function toast(msg) {
    let el = $("studioToast");
    if (!el) {
      el = document.createElement("div");
      el.id = "studioToast";
      el.className = "studio-toast";
      el.setAttribute("role", "status");
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), 3200);
  }

  const ROLE_LABEL = {
    hook: "Hook", data: "Data", list: "List", comparison: "Compare",
    story: "Story", thesis: "Thesis", body: "Body",
  };
  const FAMILY_LABEL = { ui: "UI", mental: "diagrams", stat: "stats", text: "type" };

  function treatmentLabel(tr) {
    if (!tr || tr === "none") return { icon: "·", text: "Talking head" };
    if (tr === "punch") return { icon: "⚡", text: "Punch zoom" };
    if (tr === "clean_zoom") return { icon: "🔍", text: "Slow push" };
    if (tr === "broll") return { icon: "🖼", text: "B-roll" };
    if (tr === "kinetic_text") return { icon: "🔤", text: "Kinetic type" };
    if (tr.startsWith("device:")) {
      const kind = tr.slice(7);
      const c = catalog.byKind[kind];
      return { icon: "🧩", text: c ? c.label : kind.replace(/_/g, " ") };
    }
    return { icon: "✨", text: tr };
  }

  // ------------------------------------------------------------------
  // Catalog (devices) + packs, fetched once
  // ------------------------------------------------------------------
  const catalog = { list: [], byKind: {}, budgets: {}, loaded: null };
  function loadCatalog() {
    if (!catalog.loaded) {
      catalog.loaded = api("/devices/catalog").then((d) => {
        catalog.list = d.devices || [];
        catalog.byKind = {};
        catalog.list.forEach((c) => { catalog.byKind[c.kind] = c; });
        catalog.budgets = d.pack_budgets || {};
      }).catch((e) => { catalog.loaded = null; throw e; });
    }
    return catalog.loaded;
  }

  function previewUrl(kind, labels, canvas) {
    const q = new URLSearchParams({ kind, labels: JSON.stringify(labels || []), canvas: canvas || "9x16" });
    const tl = T() && T().tl;
    if (tl && tl.job_id) q.set("job_id", tl.job_id);
    return "/devices/preview.png?" + q.toString();
  }

  // ------------------------------------------------------------------
  // Style tab
  // ------------------------------------------------------------------
  const style = { pack: "pulse", intensity: "med", packs: [], plan: null, jobId: null, busy: false };

  function currentJob() { return window.currentJobId || null; }

  async function enterStyle() {
    const jid = currentJob();
    try {
      await loadCatalog();
      if (!style.packs.length) {
        const d = await api("/ai-edit/style-packs");
        style.packs = d.packs || [];
      }
    } catch (e) {
      $("styleStatus").textContent = "Could not load styles: " + e.message;
      return;
    }
    renderPacks();
    if (jid && jid !== style.jobId) {
      style.jobId = jid;
      style.plan = null;
      $("styleStoryboard").innerHTML = "";
      $("styleSummary").classList.add("hidden");
      try {
        const st = await api("/status/" + jid);
        const words = st.words || [];
        const end = words.length ? Number(words[words.length - 1].end) || 0 : 0;
        $("styleStart").value = 0;
        $("styleEnd").value = end ? end.toFixed(1) : "";
        $("styleStatus").textContent = `${st.filename || "This video"} · ${fmt(end)} of speech. Pick a style, then Preview or Build.`;
      } catch (e) { /* status text stays */ }
    }
  }

  function budgetLine(p) {
    const own = p.graphics_budget || {};
    const d = own.per_minute != null ? own : (catalog.budgets[p.id] || {});
    if (d.per_minute == null) return "No graphics cap";
    const per = d.per_minute;
    const allow = d.allow || [];
    const fam = (!allow.length || allow.includes("all")) ? "any graphic" : allow.map((a) =>
      FAMILY_LABEL[a] || (catalog.byKind[a] ? catalog.byKind[a].label : a.replace(/_/g, " "))).join(", ");
    return `≤${per} graphics/min · ${fam}`;
  }

  function renderPacks() {
    const host = $("stylePacks");
    if (!host) return;
    host.innerHTML = "";
    style.packs.forEach((p) => {
      const st = p.style || {};
      const card = document.createElement("button");
      card.type = "button";
      card.className = "style-pack" + (p.id === style.pack ? " active" : "");
      card.setAttribute("role", "radio");
      card.setAttribute("aria-checked", p.id === style.pack ? "true" : "false");
      card.dataset.pack = p.id;
      const sw = [st.primary, st.highlight, st.accent].filter(Boolean)
        .map((c) => `<i style="background:${esc(c)}"></i>`).join("");
      card.innerHTML = `
        <div class="style-pack-sample" style="font-family:'${esc(st.font || "Anton")}',sans-serif">
          <span style="color:${esc(st.primary || "#fff")}">Say it</span>
          <span style="color:${esc(st.highlight || "#FFD60A")}">loud</span>
        </div>
        <strong>${esc(p.label)}</strong>
        <span class="style-pack-blurb">${esc(p.blurb || "")}</span>
        <span class="style-pack-meta"><span class="style-swatches">${sw}</span>${esc(budgetLine(p))}</span>`;
      card.onclick = () => {
        style.pack = p.id;
        renderPacks();
        if (style.plan) previewPlan();
      };
      host.appendChild(card);
    });
  }

  function styleRequest() {
    const start = parseFloat($("styleStart").value) || 0;
    const endRaw = parseFloat($("styleEnd").value);
    const body = {
      job_id: currentJob(), source_job_id: currentJob(),
      style_pack: style.pack, intensity: style.intensity,
      start_time: start, use_llm: $("styleLlm").checked,
      graphics: $("styleGraphics").checked, broll: $("styleBroll").checked,
      moves: $("styleMoves").checked,
    };
    if (endRaw > start) body.end_time = endRaw;
    return body;
  }

  async function previewPlan() {
    if (!currentJob()) { toast("Drop a video on Ingest first."); return; }
    if (style.busy) return;
    style.busy = true;
    const status = $("styleStatus");
    status.textContent = "Planning the edit…";
    try {
      await loadCatalog();
      const plan = await post("/edit-plan", styleRequest());
      style.plan = plan;
      renderStoryboard(plan);
      status.textContent = "Storyboard ready. Build edit turns it into a timeline.";
    } catch (e) {
      status.textContent = "Plan failed: " + e.message;
    } finally {
      style.busy = false;
    }
  }

  function renderStoryboard(plan) {
    const spans = (plan.structure && plan.structure.spans) || [];
    const counts = {};
    let graphics = 0, broll = 0, moves = 0;
    spans.forEach((s) => {
      counts[s.role] = (counts[s.role] || 0) + 1;
      if (s.treatment.startsWith("device:") || s.treatment === "kinetic_text") graphics++;
      else if (s.treatment === "broll") broll++;
      else if (s.treatment === "punch" || s.treatment === "clean_zoom") moves++;
    });
    const dur = Math.max(1, (plan.structure.out || 0) - (plan.structure.in || 0));
    const dev = (plan.style && plan.style.devices) || {};
    const cap = dev.per_minute != null ? Math.max(1, Math.round(dev.per_minute * dur / 60)) : null;
    const sum = $("styleSummary");
    sum.classList.remove("hidden");
    sum.innerHTML = `
      <span><strong>${spans.length}</strong> beats</span>
      <span>${esc(plan.structure.pace || "")} pace · ${Math.round(plan.structure.wpm || 0)} wpm</span>
      <span>🧩 <strong>${graphics}</strong>${cap != null ? ` / ${cap} budget` : ""}</span>
      <span>🖼 <strong>${broll}</strong> B-roll</span>
      <span>⚡ <strong>${moves}</strong> moves</span>
      <span class="muted">${Object.keys(counts).map((r) => `${ROLE_LABEL[r] || r} ${counts[r]}`).join(" · ")}</span>`;

    const host = $("styleStoryboard");
    host.innerHTML = "";
    const canvas = (style.packs.find((p) => p.id === style.pack) || {}).canvas || "9x16";
    spans.forEach((s) => {
      const tr = treatmentLabel(s.treatment);
      const card = document.createElement("div");
      card.className = `sb-card role-${s.role} tr-${s.treatment.split(":")[0]}`;
      let visual = `<div class="sb-visual sb-visual-${esc(s.treatment.split(":")[0])}"><span>${tr.icon}</span></div>`;
      if (s.device) {
        visual = `<div class="sb-visual sb-visual-device"><img loading="lazy" alt="${esc(tr.text)} preview" src="${esc(previewUrl(s.device.kind, s.device.labels, canvas))}"></div>`;
      } else if (s.treatment === "broll" && s.broll) {
        const q = (s.broll.concepts && s.broll.concepts[0]) || (s.broll.literal && s.broll.literal[0]) || "";
        visual = `<div class="sb-visual sb-visual-broll"><span>🖼</span><em>${esc(q)}</em></div>`;
      }
      card.innerHTML = `
        ${visual}
        <div class="sb-meta">
          <span class="sb-time">${fmt(s.start - (plan.structure.in || 0))}</span>
          <span class="sb-role">${esc(ROLE_LABEL[s.role] || s.role)}</span>
          <span class="sb-tr">${tr.icon} ${esc(tr.text)}</span>
        </div>
        <p class="sb-text">“${esc((s.text || "").slice(0, 140))}${(s.text || "").length > 140 ? "…" : ""}”</p>
        <p class="sb-reason muted">${esc(s.reason || "")}</p>`;
      host.appendChild(card);
    });
    if (!spans.length) host.innerHTML = '<p class="muted">No speech in this window.</p>';
  }

  async function buildEdit() {
    if (!currentJob()) { toast("Drop a video on Ingest first."); return; }
    if (style.busy) return;
    style.busy = true;
    const btn = $("styleBuildBtn");
    const status = $("styleStatus");
    btn.disabled = true;
    btn.textContent = "Building…";
    status.textContent = "Planning beats, rendering graphics, finding B-roll…";
    try {
      const body = Object.assign(styleRequest(), {
        smart: true, apply_cuts: $("styleCuts").checked, create_clip: false,
        allow_generate: $("styleGenerate").checked,
        label: (((window.jobsById || {})[currentJob()] || {}).filename || "AI Edit").replace(/\.[^.]+$/, "")
          + " · " + ((style.packs.find((p) => p.id === style.pack) || {}).label || style.pack),
      });
      const data = await post("/ai-edit-seed", body);
      const pl = data.placed || {};
      status.textContent = `Built: ${pl.devices || 0} graphics · ${pl.broll || 0} B-roll · ${pl.effects || 0} moves`
        + ((pl.notes || []).length ? ` · ${(pl.notes || []).length} note(s)` : "");
      // Render pulls captions from Caption look, so the picked style has to
      // land there too or its captions are replaced at export.
      if (data.timeline && data.timeline.style && typeof window.applyStyle === "function") {
        try { window.applyStyle(data.timeline.style); } catch (_) { /* Look form not mounted */ }
        if (typeof window.updateFontPreview === "function") window.updateFontPreview();
        if (typeof window.scheduleDraftSave === "function") window.scheduleDraftSave();
      }
      if (typeof window.openTimelineEditor === "function") {
        await window.openTimelineEditor(data.job_id, {
          newProject: true, replace: true, seedTimeline: data.timeline, label: data.label || "AI Edit",
        });
        const api_ = T();
        if (api_) {
          await api_.reloadAssets();
          const first = api_.tl && api_.tl.tracks.main[0];
          if (first) api_.selectClip("main", first.id);
        }
        toast("Edit built. Drag across transcript words to add more.");
      }
    } catch (e) {
      status.textContent = "Build failed: " + e.message;
    } finally {
      style.busy = false;
      btn.disabled = false;
      btn.textContent = "✨ Build edit";
    }
  }

  function wireStyle() {
    document.querySelectorAll(".style-intensity [data-intensity]").forEach((b) => {
      b.onclick = () => {
        style.intensity = b.dataset.intensity;
        document.querySelectorAll(".style-intensity [data-intensity]").forEach((x) =>
          x.classList.toggle("active", x === b));
        if (style.plan) previewPlan();
      };
    });
    ["styleGraphics", "styleBroll", "styleMoves"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("change", () => { if (style.plan) previewPlan(); });
    });
    if ($("stylePreviewBtn")) $("stylePreviewBtn").onclick = previewPlan;
    if ($("styleBuildBtn")) $("styleBuildBtn").onclick = buildEdit;
    if ($("styleTasteBtn")) $("styleTasteBtn").onclick = openTaste;
  }

  // ------------------------------------------------------------------
  // Phrase toolbar (drag across transcript words)
  // ------------------------------------------------------------------
  let phrase = null;

  function phraseFromSelection() {
    const doc = $("tlTranscriptDoc");
    const sel = window.getSelection();
    if (!doc || !sel || sel.isCollapsed || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    if (!doc.contains(range.commonAncestorContainer)) return null;
    const idxs = [];
    doc.querySelectorAll(".tl-tword").forEach((sp) => {
      if (range.intersectsNode(sp) && sp.dataset.idx != null) idxs.push(Number(sp.dataset.idx));
    });
    const api_ = T();
    const words = api_ && api_.transcriptWords;
    const clip = api_ && api_.transcriptClip;
    if (!idxs.length || !words || !clip) return null;
    const a = Math.min(...idxs), b = Math.max(...idxs);
    const ws = words.slice(a, b + 1);
    const text = ws.map((w) => String(w.word || "").trim()).join(" ").replace(/\s+/g, " ").trim();
    const srcStart = Number(ws[0].start) || 0;
    const srcEnd = Number(ws[ws.length - 1].end) || srcStart + 0.5;
    const start = api_.sourceToTimeline(clip, srcStart);
    const end = Math.max(start + 0.3, api_.sourceToTimeline(clip, srcEnd));
    return { text, start, end, dur: end - start, clip, rect: range.getBoundingClientRect() };
  }

  function ensureToolbar() {
    let bar = $("phraseToolbar");
    if (bar) return bar;
    bar = document.createElement("div");
    bar.id = "phraseToolbar";
    bar.className = "phrase-toolbar hidden";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Add to this phrase");
    bar.innerHTML = `
      <button type="button" data-ph="broll" title="Find B-roll for this phrase: your library, then stock, then a generated abstract fill">🖼 B-roll</button>
      <button type="button" data-ph="graphic" title="Add a UI mockup or diagram">🧩 Graphic</button>
      <button type="button" data-ph="behind" title="Big type behind you">👤 Behind me</button>
      <button type="button" data-ph="speed" title="Speed up or slow down this phrase">⏩ Speed</button>
      <button type="button" data-ph="punch" title="Punch-in zoom on this phrase">⚡ Punch</button>
      <div class="phrase-speed hidden">
        <button type="button" data-speed="0.5">0.5×</button>
        <button type="button" data-speed="0.75">0.75×</button>
        <button type="button" data-speed="1.5">1.5×</button>
        <button type="button" data-speed="2">2×</button>
      </div>
      <span class="phrase-busy hidden">…</span>`;
    bar.addEventListener("mousedown", (e) => e.preventDefault());   // keep the selection
    bar.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b || !phrase) return;
      if (b.dataset.speed) { phraseSpeed(Number(b.dataset.speed)); return; }
      const act = b.dataset.ph;
      if (act === "broll") phraseBroll(b);
      else if (act === "graphic") { hideToolbar(); openPicker(phrase); }
      else if (act === "behind") phraseBehind(b);
      else if (act === "speed") bar.querySelector(".phrase-speed").classList.toggle("hidden");
      else if (act === "punch") phrasePunch();
    });
    document.body.appendChild(bar);
    return bar;
  }

  function showToolbar(p) {
    const bar = ensureToolbar();
    phrase = p;
    bar.querySelector(".phrase-speed").classList.add("hidden");
    bar.classList.remove("hidden");
    const r = p.rect;
    const w = bar.offsetWidth || 420;
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2));
    const top = r.top - bar.offsetHeight - 8 > 8 ? r.top - bar.offsetHeight - 8 : r.bottom + 8;
    bar.style.left = left + window.scrollX + "px";
    bar.style.top = top + window.scrollY + "px";
  }
  function hideToolbar() {
    const bar = $("phraseToolbar");
    if (bar) bar.classList.add("hidden");
  }
  function busy(on) {
    const bar = $("phraseToolbar");
    if (!bar) return;
    bar.classList.toggle("is-busy", on);
    bar.querySelector(".phrase-busy").classList.toggle("hidden", !on);
  }
  function doneWithPhrase() {
    hideToolbar();
    try { window.getSelection().removeAllRanges(); } catch (e) {}
    phrase = null;
  }

  async function phraseBroll() {
    const p = phrase;
    const api_ = T();
    busy(true);
    try {
      const d = await post("/broll/smart", {
        text: p.text, start: p.start, duration: Math.min(4, Math.max(1.6, p.dur)),
        job_id: api_.tl.job_id, timeline: api_.serialize(),
      });
      if (!d.clip) { toast(d.error || "No suitable B-roll."); return; }
      await api_.reloadAssets();
      await api_.addOverlayClip(d.clip, { kind: d.source === "stock" ? "image" : "video", duration: d.clip.out });
      const src = { library: "your library", stock: "stock", generated: "a generated abstract fill" }[d.source] || d.source;
      toast(`B-roll from ${src}: “${d.query}”`);
      doneWithPhrase();
    } catch (e) {
      toast("B-roll failed: " + e.message);
    } finally { busy(false); }
  }

  async function addDevice(kind, labels, p, opts) {
    opts = opts || {};
    const api_ = T();
    const d = await post("/devices/render", {
      kind, labels, start: p.start, job_id: api_.tl.job_id, canvas: api_.tl.canvas,
      duration: opts.duration,
    });
    if (opts.behind) d.clip.behind = true;
    await api_.reloadAssets();
    return api_.addOverlayClip(d.clip, { kind: "video", duration: d.duration });
  }

  async function phraseBehind() {
    const p = phrase;
    busy(true);
    try {
      await addDevice("kinetic_text", [p.text.slice(0, 48)], p,
        { behind: true, duration: Math.max(1.4, Math.min(4.5, p.dur + 0.6)) });
      toast("Type placed behind you. The matte is cut at render.");
      doneWithPhrase();
    } catch (e) {
      toast("Behind-me failed: " + e.message);
    } finally { busy(false); }
  }

  async function phraseSpeed(speed) {
    const p = phrase;
    await T().addEffectClip("speed", { start: p.start, out: Math.max(0.3, p.dur), speed });
    toast(`${speed}× on “${p.text.slice(0, 40)}”`);
    doneWithPhrase();
  }

  async function phrasePunch() {
    const p = phrase;
    await T().addEffectClip("punch_zoom", { start: p.start, out: Math.min(1.2, Math.max(0.5, p.dur)), intensity: "med" });
    toast("Punch zoom added.");
    doneWithPhrase();
  }

  function wirePhraseToolbar() {
    document.addEventListener("mouseup", (e) => {
      if (e.target.closest && e.target.closest("#phraseToolbar, #devicePicker, #tasteModal")) return;
      setTimeout(() => {
        const p = phraseFromSelection();
        if (p && p.text) showToolbar(p);
        else hideToolbar();
      }, 0);
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideToolbar(); });
    const doc = $("tlTranscriptDoc");
    if (doc) doc.addEventListener("scroll", hideToolbar);
  }

  // ------------------------------------------------------------------
  // Graphic picker
  // ------------------------------------------------------------------
  const picker = { phrase: null, kind: null, labelsByKind: {}, suggestion: null };

  async function openPicker(p) {
    picker.phrase = p;
    const modal = $("devicePicker");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    $("devicePickerPhrase").textContent = `“${p.text.slice(0, 80)}” at ${fmt(p.start)}`;
    $("devicePickerStatus").textContent = "Reading the phrase…";
    $("devicePickerBehind").checked = false;
    hideToolbar();
    try {
      await loadCatalog();
      const sug = await post("/devices/suggest", { text: p.text });
      picker.suggestion = sug;
      picker.labelsByKind = {};
      picker.labelsByKind[sug.kind] = sug.labels || [];
      (sug.alternatives || []).forEach((a) => { picker.labelsByKind[a.kind] = a.labels || []; });
      picker.kind = sug.kind;
      $("devicePickerStatus").textContent = `Suggested: ${(catalog.byKind[sug.kind] || {}).label || sug.kind} — ${sug.reason}`;
      renderPickerGrid();
      renderPickerFields();
    } catch (e) {
      $("devicePickerStatus").textContent = "Could not suggest: " + e.message;
    }
  }

  function closePicker() {
    const modal = $("devicePicker");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  function pickerLabels(kind) {
    if (picker.labelsByKind[kind]) return picker.labelsByKind[kind];
    if (kind === "subscribe_button") return ["Your Channel", "1.2M"];
    const words = (picker.phrase.text || "").split(/\s+/).filter((w) => w.length > 3).slice(0, 4);
    return kind === "kinetic_text" ? [picker.phrase.text.slice(0, 48)] : words.map((w) => w[0].toUpperCase() + w.slice(1));
  }

  function renderPickerGrid() {
    const grid = $("devicePickerGrid");
    const canvas = (T() && T().tl && T().tl.canvas) || "9x16";
    const order = [picker.suggestion && picker.suggestion.kind]
      .concat(((picker.suggestion && picker.suggestion.alternatives) || []).map((a) => a.kind))
      .concat(catalog.list.map((c) => c.kind));
    const seen = new Set();
    grid.innerHTML = "";
    order.filter((k) => k && !seen.has(k) && seen.add(k)).forEach((kind) => {
      const c = catalog.byKind[kind];
      if (!c) return;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "device-card" + (kind === picker.kind ? " active" : "")
        + (picker.suggestion && kind === picker.suggestion.kind ? " suggested" : "");
      b.dataset.kind = kind;
      b.innerHTML = `<img loading="lazy" alt="" src="${esc(previewUrl(kind, pickerLabels(kind), canvas))}">
        <strong>${esc(c.label)}</strong><span>${esc(c.blurb)}</span>`;
      b.onclick = () => { picker.kind = kind; renderPickerGrid(); renderPickerFields(); };
      grid.appendChild(b);
    });
  }

  function renderPickerFields() {
    const host = $("devicePickerFields");
    const c = catalog.byKind[picker.kind];
    if (!c) { host.innerHTML = ""; return; }
    const labels = pickerLabels(picker.kind).slice();
    host.innerHTML = `<div class="device-fields-head">${esc(c.label)} text</div>` + c.fields.map((f, i) =>
      `<label class="tl-prop">${esc(f)}<input type="text" maxlength="120" data-fi="${i}" value="${esc(labels[i] || "")}"></label>`
    ).join("");
    let t = null;
    host.querySelectorAll("input").forEach((inp) => {
      inp.addEventListener("input", () => {
        const arr = pickerLabels(picker.kind).slice();
        arr[Number(inp.dataset.fi)] = inp.value;
        picker.labelsByKind[picker.kind] = arr;
        clearTimeout(t);
        t = setTimeout(() => {
          const img = $("devicePickerGrid").querySelector(`.device-card[data-kind="${picker.kind}"] img`);
          if (img) img.src = previewUrl(picker.kind, arr, (T().tl || {}).canvas);
        }, 350);
      });
    });
  }

  async function pickerAdd() {
    const btn = $("devicePickerAdd");
    btn.disabled = true;
    $("devicePickerStatus").textContent = "Rendering…";
    try {
      const labels = pickerLabels(picker.kind).filter((x) => String(x || "").trim());
      await addDevice(picker.kind, labels, picker.phrase, { behind: $("devicePickerBehind").checked });
      closePicker();
      doneWithPhrase();
      toast(`${(catalog.byKind[picker.kind] || {}).label || "Graphic"} added.`);
    } catch (e) {
      $("devicePickerStatus").textContent = "Render failed: " + e.message;
    } finally { btn.disabled = false; }
  }

  function wirePicker() {
    if ($("devicePickerClose")) $("devicePickerClose").onclick = closePicker;
    if ($("devicePickerCancel")) $("devicePickerCancel").onclick = closePicker;
    if ($("devicePickerAdd")) $("devicePickerAdd").onclick = pickerAdd;
    const m = $("devicePicker");
    if (m) m.addEventListener("click", (e) => { if (e.target === m) closePicker(); });
  }

  // ------------------------------------------------------------------
  // Props panel extras (colour match, device re-render)
  // ------------------------------------------------------------------
  window.studioWireProps = function (wrap, track, c) {
    if (track !== "overlay") return;
    const api_ = T();
    const cm = wrap.querySelector('[data-act="colormatch"]');
    if (cm) {
      cm.addEventListener("change", async () => {
        api_.pushHistory();
        if (!cm.checked) {
          delete c.color_match;
          api_.renderProps(); api_.scheduleSave();
          return;
        }
        cm.disabled = true;
        try {
          const d = await post("/broll/color-match", {
            timeline: api_.serialize(), asset_id: c.asset_id, start: c.start || 0,
          });
          c.color_match = d.color_match;
          toast("Graded toward the A-roll. Visible after Render.");
        } catch (e) {
          toast("Color match failed: " + e.message);
        }
        api_.renderProps(); api_.scheduleSave();
      });
    }
    const host = wrap.querySelector("#tlDeviceProps");
    if (!host || !c.device) return;
    loadCatalog().then(() => {
      const meta = catalog.byKind[c.device.kind];
      if (!meta) return;
      const labels = (c.device.labels || []).slice();
      host.innerHTML = `<hr class="tl-sep"><label class="tl-prop-sectlabel">🧩 ${esc(meta.label)}</label>
        <img class="device-prop-preview" alt="" src="${esc(previewUrl(c.device.kind, labels, api_.tl.canvas))}">`
        + meta.fields.map((f, i) =>
          `<label class="tl-prop">${esc(f)}<input type="text" maxlength="120" data-dfi="${i}" value="${esc(labels[i] || "")}"></label>`).join("")
        + `<button type="button" class="btn btn-secondary btn-block" data-act="devrerender">↻ Re-render graphic</button>`;
      host.querySelector('[data-act="devrerender"]').onclick = async (ev) => {
        const btn = ev.currentTarget;
        const next = Array.from(host.querySelectorAll("[data-dfi]")).map((i) => i.value).filter((v) => v.trim());
        btn.disabled = true;
        btn.textContent = "Rendering…";
        try {
          const d = await post("/devices/render", {
            kind: c.device.kind, labels: next, start: c.start || 0, job_id: api_.tl.job_id,
            canvas: api_.tl.canvas, duration: Math.max(0.8, (c.out || 0) - (c.in || 0)),
          });
          api_.pushHistory();
          c.asset_id = d.asset_id;
          c.device = d.device;
          c.in = 0;
          c.out = d.duration;
          await api_.reloadAssets();
          api_.renderTimeline();
          api_.scheduleSave();
          toast(d.cached ? "Reused a cached render." : "Graphic re-rendered.");
        } catch (e) {
          toast("Re-render failed: " + e.message);
          btn.disabled = false;
          btn.textContent = "↻ Re-render graphic";
        }
      };
    });
  };

  // ------------------------------------------------------------------
  // Dirty segments (amber = re-encode, green = reused from cache)
  // ------------------------------------------------------------------
  const dirty = { on: false, data: null, timer: null, inflight: false };

  async function refreshDirty() {
    const api_ = T();
    if (!api_ || !api_.tl || dirty.inflight) return null;
    dirty.inflight = true;
    try {
      dirty.data = await post("/timeline/dirty", { job_id: api_.tl.job_id, timeline: api_.serialize() });
    } catch (e) {
      dirty.data = null;
    } finally { dirty.inflight = false; }
    drawDirty();
    renderExportDirty();
    return dirty.data;
  }

  function drawDirty() {
    const lane = document.querySelector('.tl-track-lane[data-lane="main"]');
    if (!lane) return;
    lane.querySelectorAll(".tl-dirty-band").forEach((b) => b.remove());
    if (!dirty.on || !dirty.data) return;
    const pps = T().pps;
    (dirty.data.segments || []).forEach((s) => {
      const b = document.createElement("div");
      b.className = "tl-dirty-band " + (s.cached ? "cached" : "dirty");
      b.style.left = s.start * pps + "px";
      b.style.width = Math.max(2, (s.end - s.start) * pps) + "px";
      b.title = (s.cached ? "Reused from cache" : "Will re-encode") + ` · ${fmt(s.start)}–${fmt(s.end)}`
        + (s.speed && s.speed !== 1 ? ` · ${s.speed}×` : "");
      lane.appendChild(b);
    });
  }

  function scheduleDirty() {
    if (!dirty.on && !isExportTab()) return;
    clearTimeout(dirty.timer);
    dirty.timer = setTimeout(refreshDirty, 600);
  }

  function wireDirty() {
    const btn = $("tlDirtyBtn");
    if (btn) {
      btn.onclick = () => {
        dirty.on = !dirty.on;
        btn.classList.toggle("active", dirty.on);
        if (dirty.on) refreshDirty(); else drawDirty();
      };
    }
    const lane = document.querySelector('.tl-track-lane[data-lane="main"]');
    if (lane) {
      new MutationObserver((muts) => {
        if (!dirty.on) return;
        const rebuilt = muts.some((m) => Array.from(m.removedNodes).some((n) => n.classList && n.classList.contains("tl-clip")));
        if (rebuilt && !lane.querySelector(".tl-dirty-band")) drawDirty();
      }).observe(lane, { childList: true });
    }
    const save = $("tlSaveState");
    if (save) {
      new MutationObserver(() => { if (/Saved/.test(save.textContent)) scheduleDirty(); })
        .observe(save, { childList: true, characterData: true, subtree: true });
    }
  }

  // ------------------------------------------------------------------
  // Export tab: render, cache stats, QA report, interchange
  // ------------------------------------------------------------------
  const exp = { qaTimer: null, jobId: null };
  const isExportTab = () => {
    const el = document.querySelector('[data-tab-group="export"]');
    return !!(el && !el.classList.contains("hidden"));
  };

  async function enterExport() {
    const api_ = T();
    if (api_ && !api_.tl && typeof window.ensureTimelineInit === "function") {
      try { await window.ensureTimelineInit(); } catch (e) {}
    }
    const tl = api_ && api_.tl;
    const name = $("exportProjectName");
    const has = !!(tl && tl.tracks && tl.tracks.main.length);
    $("exportRenderBtn").disabled = !has;
    if (!tl) {
      name.textContent = "No timeline project open. Build one from Style, or open one in Edit.";
      return;
    }
    name.textContent = `${tl.label || "Timeline"} · ${tl.canvas} · ${fmt(mainLength(tl))}`;
    $("exportJsonBtn").href = `/timeline/${tl.job_id}/export.json`;
    $("exportOtioBtn").href = `/timeline/${tl.job_id}/export.otio`;
    exp.jobId = tl.job_id;
    refreshDirty();
    try {
      const s = await api("/status/" + tl.job_id);
      showRenderResult(s);
    } catch (e) { /* not rendered yet */ }
  }

  function mainLength(tl) {
    return (tl.tracks.main || []).reduce((a, c) => a + T().clipDuration(c), 0);
  }

  function renderExportDirty() {
    const el = $("exportDirty");
    if (!el) return;
    const d = dirty.data;
    if (!d || !d.segments) { el.textContent = ""; return; }
    const total = (d.dirty_seconds || 0) + (d.cached_seconds || 0);
    if (!total) { el.textContent = ""; return; }
    const pct = Math.round((d.dirty_seconds / total) * 100);
    el.innerHTML = `<div class="export-dirty-bar">${d.segments.map((s) =>
      `<i class="${s.cached ? "cached" : "dirty"}" style="flex:${Math.max(0.01, s.end - s.start)}" title="${s.cached ? "cached" : "changed"} ${fmt(s.start)}–${fmt(s.end)}"></i>`).join("")}</div>
      <span>${d.dirty_seconds ? `<strong>${d.dirty_seconds.toFixed(1)}s</strong> changed will re-encode (${pct}%)` : "Nothing changed since the last render"}
      · ${d.cached_seconds.toFixed(1)}s reused from cache. Render only encodes the amber spans.</span>`;
  }

  async function exportRender() {
    const api_ = T();
    if (!api_ || !api_.tl) return;
    $("exportStatus").textContent = "Queued…";
    $("exportRenderBtn").disabled = true;
    setQa(null);
    const result = await api_.render();
    if (!result && $("exportRenderBtn").disabled) {
      $("exportRenderBtn").disabled = false;
      $("exportStatus").textContent = "Render did not start — see the message above.";
    }
  }

  window.studioOnRenderDone = function (jobId, s) {
    $("exportRenderBtn").disabled = false;
    showRenderResult(s);
    refreshDirty();
    const stats = s.render_stats;
    const tlStats = $("tlCacheStats");
    if (tlStats && stats) {
      tlStats.textContent = `· ${stats.hits || 0} reused / ${stats.misses || 0} encoded segments`;
    }
  };

  function showRenderResult(s) {
    const status = $("exportStatus");
    if (s.status === "error") {
      status.textContent = "Render failed: " + (s.error || "unknown error");
      return;
    }
    if (s.status !== "done" || !s.output) {
      if (s.status && s.status !== "timeline_edit") status.textContent = `${s.status}… ${s.progress || 0}%`;
      return;
    }
    status.textContent = "Rendered ✓";
    const v = $("exportPlayer");
    v.classList.remove("hidden");
    const src = "/preview/" + s.output;
    if (!v.src.endsWith(src)) v.src = src + "?t=" + Date.now();
    const dl = $("exportDownload");
    dl.href = "/download/" + s.output;
    dl.classList.remove("hidden");
    const st = s.render_stats;
    const box = $("exportCache");
    if (st) {
      box.classList.remove("hidden");
      box.innerHTML = `⚡ <strong>${st.hits || 0}</strong> segment(s) reused (${(st.seconds_reused || 0).toFixed(1)}s) ·
        <strong>${st.misses || 0}</strong> encoded (${(st.seconds_rendered || 0).toFixed(1)}s)`
        + (st.split ? ` · ${st.split} split at speed/subject boundaries` : "");
    }
    pollQa(s.job_id || exp.jobId);
  }

  function pollQa(jobId) {
    clearTimeout(exp.qaTimer);
    if (!jobId) return;
    api("/qa/" + jobId).then((r) => {
      setQa(r);
      if (r.status === "running") exp.qaTimer = setTimeout(() => pollQa(jobId), 1500);
    }).catch(() => {});
  }

  const SEV_ICON = { pass: "✓", warn: "!", fail: "✕", skip: "–", info: "i" };

  function setQa(r) {
    const badge = $("qaBadge");
    const list = $("qaChecks");
    const kf = $("qaKeyframes");
    if (!r || r.status === "none") {
      badge.textContent = "—";
      badge.className = "qa-badge";
      list.innerHTML = '<li class="muted">Render to see the report.</li>';
      kf.innerHTML = "";
      return;
    }
    badge.textContent = { pass: "Pass", warn: "Check", fail: "Fix", running: "Checking…", error: "Error" }[r.status] || r.status;
    badge.className = "qa-badge qa-" + r.status;
    if (r.status === "running") { list.innerHTML = '<li class="muted">Running checks…</li>'; return; }
    if (r.status === "error") { list.innerHTML = `<li class="qa-fail">${esc(r.error || "QA failed")}</li>`; return; }
    const rows = (r.checks || []).map((c) =>
      `<li class="qa-${esc(c.status)}"><span class="qa-ico">${SEV_ICON[c.status] || "•"}</span>
        <strong>${esc(c.label)}</strong> <span class="muted">${esc(c.detail || "")}</span></li>`);
    const vis = r.vision;
    if (vis) {
      if (!vis.issues.length) {
        rows.push(`<li class="qa-pass"><span class="qa-ico">✓</span><strong>Vision spot-check</strong> <span class="muted">${vis.frames} frames, nothing found (${esc(vis.engine)})</span></li>`);
      }
      vis.issues.forEach((i) => rows.push(
        `<li class="qa-${esc(i.severity)}"><span class="qa-ico">${SEV_ICON[i.severity] || "!"}</span>
          <strong>Vision @ ${fmt(i.t)}</strong> <span class="muted">${esc(i.problem)}</span></li>`));
    }
    list.innerHTML = rows.join("") || '<li class="muted">No checks ran.</li>';
    const issuesAt = {};
    ((vis && vis.issues) || []).forEach((i) => { issuesAt[Number(i.t).toFixed(2)] = i; });
    kf.innerHTML = (r.keyframes || []).map((k) => {
      const issue = issuesAt[Number(k.t).toFixed(2)];
      return `<figure class="${issue ? "qa-" + esc(issue.severity) : ""}"><img loading="lazy" alt="Frame at ${fmt(k.t)}" src="${esc(k.url)}">
        <figcaption>${fmt(k.t)}${issue ? " · " + esc(issue.problem.slice(0, 60)) : ""}</figcaption></figure>`;
    }).join("");
  }

  async function qaRun(vision) {
    if (!exp.jobId) return;
    const btn = vision ? $("qaVisionBtn") : $("qaRerunBtn");
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = vision ? "Looking…" : "Checking…";
    try {
      setQa(await post(`/qa/${exp.jobId}/run`, { checks: !vision, vision: !!vision }));
    } catch (e) {
      toast("QA: " + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  async function importEdit(file) {
    const st = $("exportImportStatus");
    st.textContent = "Importing…";
    const fd = new FormData();
    fd.append("file", file);
    try {
      const d = await api("/timeline/import", { method: "POST", body: fd });
      const miss = (d.missing || []).length;
      st.textContent = `Imported ${d.format.toUpperCase()}` + (miss ? ` · ${miss} clip(s) need relinking` : "");
      if (typeof window.setActiveTab === "function") window.setActiveTab("editor");
      if (typeof window.ensureTimelineInit === "function") await window.ensureTimelineInit();
      await T().openProject(d.job_id);
    } catch (e) {
      st.textContent = "Import failed: " + e.message;
    }
  }

  function wireExport() {
    if ($("exportRenderBtn")) $("exportRenderBtn").onclick = exportRender;
    if ($("qaRerunBtn")) $("qaRerunBtn").onclick = () => qaRun(false);
    if ($("qaVisionBtn")) $("qaVisionBtn").onclick = () => qaRun(true);
    const f = $("exportImportFile");
    if (f) f.addEventListener("change", () => {
      if (f.files && f.files[0]) importEdit(f.files[0]);
      f.value = "";
    });
    const rs = $("tlRenderStatus");
    if (rs) {
      new MutationObserver(() => {
        if (isExportTab() && rs.textContent && !/Done/.test(rs.textContent)) $("exportStatus").textContent = rs.textContent;
      }).observe(rs, { childList: true, characterData: true, subtree: true });
    }
  }

  // ------------------------------------------------------------------
  // Structure panel (editor left column)
  // ------------------------------------------------------------------
  function renderStructure() {
    const host = $("tlStructure");
    if (!host) return;
    const api_ = T();
    const tl = api_ && api_.tl;
    const spans = (tl && tl.structure && tl.structure.spans) || [];
    if (!spans.length) {
      host.innerHTML = '<p class="muted" style="font-size:.78rem">No plan on this project. Build one from <a href="#" data-go-style>Style</a>.</p>';
      const a = host.querySelector("[data-go-style]");
      if (a) a.onclick = (e) => { e.preventDefault(); window.setActiveTab && window.setActiveTab("style"); };
      return;
    }
    const placedAt = {};
    ["overlay", "effects"].forEach((k) => (tl.tracks[k] || []).forEach((c) => {
      if (c.plan_ref != null && placedAt[c.plan_ref] == null) placedAt[c.plan_ref] = c.start || 0;
    }));
    const main0 = tl.tracks.main[0];
    const t0 = tl.structure.in || 0;
    host.innerHTML = "";
    spans.forEach((s) => {
      const t = placedAt[s.index] != null ? placedAt[s.index]
        : (main0 ? api_.sourceToTimeline(main0, s.start) : s.start - t0);
      const tr = treatmentLabel(s.treatment);
      const row = document.createElement("button");
      row.type = "button";
      row.className = `tl-structure-row role-${s.role}`;
      row.innerHTML = `<span class="sb-time">${fmt(t)}</span><span class="sb-role">${esc(ROLE_LABEL[s.role] || s.role)}</span>
        <span class="tl-structure-tr" title="${esc(s.reason || "")}">${tr.icon} ${esc(tr.text)}</span>
        <span class="tl-structure-text">${esc((s.text || "").slice(0, 90))}</span>`;
      row.onclick = () => api_.seek(t + 0.01);
      host.appendChild(row);
    });
  }

  function wireStructure() {
    const tab = document.querySelector('.tl-lefttab[data-ltab="structure"]');
    if (tab) tab.addEventListener("click", renderStructure);
  }

  // ------------------------------------------------------------------
  // Taste memory
  // ------------------------------------------------------------------
  async function openTaste() {
    const m = $("tasteModal");
    m.classList.remove("hidden");
    m.setAttribute("aria-hidden", "false");
    await loadTaste();
    $("tasteText").focus();
  }
  function closeTaste() {
    const m = $("tasteModal");
    m.classList.add("hidden");
    m.setAttribute("aria-hidden", "true");
  }
  const KIND_LABEL = { rule: "Rule", bad_example: "Never", prefer: "Prefers", avoid: "Avoids", brand: "Brand" };

  async function loadTaste() {
    const host = $("tasteList");
    try {
      const d = await api("/taste");
      const items = d.items || [];
      if (!items.length) {
        host.innerHTML = '<p class="muted" style="font-size:.8rem">Nothing yet. Add a rule above, or keep editing: removing or adjusting AI moves teaches it.</p>';
        return;
      }
      host.innerHTML = items.map((i) => `
        <div class="taste-item taste-${esc(i.source)}">
          <span class="taste-kind">${esc(KIND_LABEL[i.kind] || i.kind)}</span>
          <span class="taste-text">${esc(i.text)}</span>
          <span class="taste-meta muted">${i.source === "user" ? "yours" : `learned · ×${Number(i.weight || 0).toFixed(1)}`}</span>
          <button type="button" class="tl-chip-btn" data-del="${esc(i.id)}" aria-label="Forget">✕</button>
        </div>`).join("");
      host.querySelectorAll("[data-del]").forEach((b) => {
        b.onclick = async () => {
          await api("/taste/" + encodeURIComponent(b.dataset.del), { method: "DELETE" });
          loadTaste();
        };
      });
    } catch (e) {
      host.innerHTML = `<p class="muted">Could not load: ${esc(e.message)}</p>`;
    }
  }

  async function addTaste() {
    const text = $("tasteText").value.trim();
    if (!text) return;
    try {
      await post("/taste", { kind: $("tasteKind").value, text });
      $("tasteText").value = "";
      loadTaste();
    } catch (e) { toast(e.message); }
  }

  function wireTaste() {
    if ($("tasteClose")) $("tasteClose").onclick = closeTaste;
    if ($("tasteDone")) $("tasteDone").onclick = closeTaste;
    if ($("tasteAddBtn")) $("tasteAddBtn").onclick = addTaste;
    if ($("tasteText")) $("tasteText").addEventListener("keydown", (e) => { if (e.key === "Enter") addTaste(); });
    if ($("tastePruneBtn")) $("tastePruneBtn").onclick = async () => {
      const r = await post("/taste/prune", {});
      toast(`Pruned ${r.removed || 0} item(s).`);
      loadTaste();
    };
    if ($("tasteResetBtn")) $("tasteResetBtn").onclick = async () => {
      if (!confirm("Forget everything learned? Your own rules stay.")) return;
      await post("/taste/reset", { scope: "learned" });
      loadTaste();
    };
    if ($("tlTasteBtn")) $("tlTasteBtn").onclick = openTaste;
    const m = $("tasteModal");
    if (m) m.addEventListener("click", (e) => { if (e.target === m) closeTaste(); });
  }

  // ------------------------------------------------------------------
  document.addEventListener("studio:tab", (e) => {
    const tab = e.detail && e.detail.tab;
    hideToolbar();
    if (tab === "style") enterStyle();
    else if (tab === "export") enterExport();
    else if (tab === "editor") setTimeout(renderStructure, 300);
  });

  function boot() {
    wireStyle();
    wirePhraseToolbar();
    wirePicker();
    wireDirty();
    wireExport();
    wireStructure();
    wireTaste();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  window.studio = { refreshDirty, renderStructure, openTaste, openPicker };
})();
