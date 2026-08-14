/* Mobile shell — bottom nav, Library sheet, CapCut-style context tools */
(function () {
  "use strict";

  const MQ = window.matchMedia("(max-width: 768px)");
  let _previewMode = "normal"; // normal | compact | collapsed
  let _activeTool = null;

  function isPhone() {
    return MQ.matches;
  }

  function syncPhoneClass() {
    document.body.classList.toggle("is-phone", isPhone());
    if (!isPhone()) {
      document.body.classList.remove(
        "tl-mobile-panel-open",
        "tl-toolbar-expanded",
        "tl-preview-compact",
        "tl-preview-collapsed",
        "tl-tool-sheet-open",
        "tab-editor-active"
      );
      const scrim = document.getElementById("mobilePanelScrim");
      if (scrim) scrim.hidden = true;
      closeToolSheet();
      const ctx = document.getElementById("tlContextTools");
      if (ctx) ctx.hidden = true;
    } else {
      refreshContextTools();
    }
  }

  function setMobileNavActive(tab) {
    document.querySelectorAll("#mobileBottomNav .mobile-nav-btn").forEach((btn) => {
      if (btn.dataset.action === "export") {
        btn.classList.remove("active");
        return;
      }
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
    document.body.classList.toggle("tab-editor-active", tab === "editor");
    if (tab === "editor") refreshContextTools();
    else {
      const ctx = document.getElementById("tlContextTools");
      if (ctx) ctx.hidden = true;
      closeToolSheet();
    }
  }

  function openMobilePanel(ltab) {
    if (!isPhone()) return;
    closeToolSheet();
    document.body.classList.add("tl-mobile-panel-open");
    const scrim = document.getElementById("mobilePanelScrim");
    if (scrim) scrim.hidden = false;
    if (ltab && typeof window.setTimelineLeftTab === "function") {
      window.setTimelineLeftTab(ltab, { pin: true });
    } else if (ltab) {
      const tab = document.querySelector(`.tl-lefttab[data-ltab="${ltab}"]`);
      if (tab) tab.click();
    }
  }

  function closeMobilePanel() {
    document.body.classList.remove("tl-mobile-panel-open");
    const scrim = document.getElementById("mobilePanelScrim");
    if (scrim) scrim.hidden = true;
  }

  function cyclePreviewSize() {
    if (!isPhone()) return;
    if (_previewMode === "normal") _previewMode = "compact";
    else if (_previewMode === "compact") _previewMode = "collapsed";
    else _previewMode = "normal";
    document.body.classList.toggle("tl-preview-compact", _previewMode === "compact");
    document.body.classList.toggle("tl-preview-collapsed", _previewMode === "collapsed");
    const btn = document.getElementById("tlPreviewCollapseBtn");
    if (btn) {
      btn.textContent = _previewMode === "collapsed" ? "▴" : (_previewMode === "compact" ? "▾" : "▾");
      btn.setAttribute("aria-expanded", _previewMode !== "collapsed" ? "true" : "false");
      btn.title = _previewMode === "collapsed"
        ? "Expand preview"
        : (_previewMode === "compact" ? "Collapse preview further" : "Shrink preview");
    }
  }

  function closeToolSheet() {
    document.body.classList.remove("tl-tool-sheet-open");
    const sheet = document.getElementById("tlToolSheet");
    const scrim = document.getElementById("tlToolSheetScrim");
    if (sheet) {
      sheet.hidden = true;
      sheet.setAttribute("aria-hidden", "true");
    }
    if (scrim) scrim.hidden = true;
    _activeTool = null;
    document.querySelectorAll("#tlContextTools .tl-ctx-btn").forEach((b) => b.classList.remove("active"));
  }

  function openToolSheet(title, html, toolId) {
    if (!isPhone()) return;
    closeMobilePanel();
    const sheet = document.getElementById("tlToolSheet");
    const scrim = document.getElementById("tlToolSheetScrim");
    const body = document.getElementById("tlToolSheetBody");
    const titleEl = document.getElementById("tlToolSheetTitle");
    if (!sheet || !body) return;
    if (titleEl) titleEl.textContent = title || "Tools";
    body.innerHTML = html || "";
    sheet.hidden = false;
    sheet.setAttribute("aria-hidden", "false");
    if (scrim) scrim.hidden = false;
    document.body.classList.add("tl-tool-sheet-open");
    _activeTool = toolId || null;
    document.querySelectorAll("#tlContextTools .tl-ctx-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tool === toolId);
    });
    // Auto-compact preview when a sheet is open (more room for tools).
    if (_previewMode === "normal") {
      _previewMode = "compact";
      document.body.classList.add("tl-preview-compact");
      document.body.classList.remove("tl-preview-collapsed");
    }
  }

  function toolsForSelection(sel) {
    const track = sel && sel.track;
    if (!track) {
      return [
        { id: "add", ico: "＋", label: "Add" },
        { id: "clipstyle", ico: "✨", label: "Clip style" },
        { id: "captions", ico: "Aa", label: "Captions" },
        { id: "sound", ico: "♪", label: "Sound" },
        { id: "chat", ico: "💬", label: "Chat" },
        { id: "export", ico: "⚡", label: "Export" },
      ];
    }
    if (track === "main") {
      return [
        { id: "clipstyle", ico: "✨", label: "Clip style" },
        { id: "split", ico: "✂", label: "Split" },
        { id: "captions", ico: "Aa", label: "Captions" },
        { id: "effects", ico: "🎞", label: "Effects" },
        { id: "delete", ico: "🗑", label: "Delete" },
      ];
    }
    if (track === "overlay") {
      return [
        { id: "layout", ico: "▢", label: "Layout" },
        { id: "size", ico: "⇔", label: "Size" },
        { id: "kenburns", ico: "🔍", label: "Ken Burns" },
        { id: "replace", ico: "↻", label: "Replace" },
        { id: "delete", ico: "🗑", label: "Delete" },
      ];
    }
    if (track === "effects") {
      return [
        { id: "type", ico: "🎞", label: "Type" },
        { id: "strength", ico: "↕", label: "Strength" },
        { id: "delete", ico: "🗑", label: "Delete" },
      ];
    }
    if (track === "text") {
      return [
        { id: "edit", ico: "✎", label: "Edit" },
        { id: "font", ico: "Aa", label: "Font" },
        { id: "timing", ico: "⏱", label: "Timing" },
        { id: "delete", ico: "🗑", label: "Delete" },
      ];
    }
    if (track === "music") {
      return [
        { id: "volume", ico: "🔊", label: "Volume" },
        { id: "duck", ico: "🔉", label: "Duck" },
        { id: "delete", ico: "🗑", label: "Delete" },
      ];
    }
    return [{ id: "edit", ico: "✎", label: "Edit" }];
  }

  function refreshContextTools() {
    const ctx = document.getElementById("tlContextTools");
    if (!ctx) return;
    if (!isPhone() || !document.body.classList.contains("tab-editor-active")) {
      ctx.hidden = true;
      return;
    }
    ctx.hidden = false;
    const sel = typeof window.getTimelineSelection === "function"
      ? window.getTimelineSelection()
      : null;
    const tools = toolsForSelection(sel);
    ctx.innerHTML = tools.map((t) =>
      `<button type="button" class="tl-ctx-btn${_activeTool === t.id ? " active" : ""}" data-tool="${t.id}">` +
      `<span class="ico">${t.ico}</span><span>${t.label}</span></button>`
    ).join("");
  }

  function clipStyleSheetHtml() {
    const styles = (typeof window.listClipStyles === "function")
      ? window.listClipStyles()
      : [];
    if (!styles.length) {
      return '<p class="muted">Clip styles are still loading — open Timeline again in a second.</p>';
    }
    return `<p class="muted" style="font-size:.78rem;margin:0 0 10px;line-height:1.4">Apply a layout/effect recipe to the <strong>selected Main clip</strong> (or project default). This uses tools you already have — not a new render engine.</p>` +
      `<div class="tl-clip-style-grid">` +
      styles.map((s) =>
        `<button type="button" class="tl-clip-style-card" data-clip-style="${s.id}">` +
        `<strong>${s.label}</strong><span>${s.blurb || ""}</span></button>`
      ).join("") +
      `</div>`;
  }

  function openPropsInSheet(title, toolId) {
    const props = document.getElementById("tlProps");
    if (!props) {
      openToolSheet(title, '<p class="muted">Nothing to edit yet.</p>', toolId);
      return;
    }
    // Ensure props are current, then mirror HTML into the sheet.
    if (typeof window.refreshTimelineProps === "function") window.refreshTimelineProps();
    openToolSheet(title, props.innerHTML, toolId);
    // Re-bind sheet interactions via timeline helper when available.
    if (typeof window.bindTimelinePropsHost === "function") {
      window.bindTimelinePropsHost(document.getElementById("tlToolSheetBody"));
    }
  }

  function replaceMediaSheetHtml(prefill) {
    const geminiOk = !!(window._brollGeminiReady);
    const seed = (prefill || "").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return `<p class="muted" style="font-size:.78rem;margin:0 0 10px;line-height:1.4">CapCut-style <strong>Replace media</strong> — generate an AI still or pick from Library. Updates the <em>same</em> overlay clip.</p>`
      + `<label class="muted" style="font-size:.74rem;display:block;margin-bottom:6px">AI prompt (9:16-friendly still)`
      + `<textarea id="tlReplaceAiPrompt" rows="3" style="display:block;width:100%;margin-top:4px;padding:8px;background:#10131d;border:1px solid #3b4252;color:#fff;border-radius:8px;resize:vertical" placeholder="e.g. warm coffee steam over a wooden table, photo">${seed}</textarea></label>`
      + `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">`
      + `<button type="button" class="btn btn-primary" id="tlReplaceAiGen" ${geminiOk ? "" : "disabled"}>${geminiOk ? "✨ Generate AI photo" : "Set GEMINI_API_KEY"}</button>`
      + `<button type="button" class="btn btn-secondary" id="tlReplaceBrowse">📚 Library</button>`
      + `</div>`
      + `<p id="tlReplaceAiStatus" class="muted" style="font-size:.72rem;margin:10px 0 0"></p>`
      + `<div id="tlReplaceAiPreview" style="margin-top:10px"></div>`;
  }

  async function generateReplaceAiPhoto() {
    const promptEl = document.getElementById("tlReplaceAiPrompt");
    const status = document.getElementById("tlReplaceAiStatus");
    const preview = document.getElementById("tlReplaceAiPreview");
    const btn = document.getElementById("tlReplaceAiGen");
    const prompt = (promptEl && promptEl.value || "").trim();
    if (prompt.length < 2) {
      if (status) status.textContent = "Enter a short prompt first.";
      return;
    }
    if (btn) { btn.disabled = true; btn.textContent = "Generating…"; }
    if (status) status.textContent = "Calling Gemini…";
    try {
      const res = await fetch("/broll/generate-ai", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, keyword: prompt.slice(0, 40) }),
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || ("HTTP " + res.status));
      if (status) status.textContent = "Ready — Insert to replace the same overlay.";
      if (preview) {
        preview.innerHTML =
          `<img src="${data.url}?t=${Date.now()}" alt="" style="max-width:100%;max-height:160px;border-radius:10px;border:1px solid #3b4252;display:block;margin-bottom:8px">`
          + `<button type="button" class="btn btn-primary" id="tlReplaceAiInsert">Insert</button>`;
        const insert = document.getElementById("tlReplaceAiInsert");
        if (insert) {
          insert.onclick = () => {
            if (typeof window.replaceSelectedOverlayAsset === "function") {
              const ok = window.replaceSelectedOverlayAsset(data.asset_id, {
                source: "gemini",
                keyword: data.keyword || prompt.slice(0, 40),
              });
              if (ok) {
                closeToolSheet();
                refreshContextTools();
              }
            } else {
              alert("Timeline replace helper not ready — try again.");
            }
          };
        }
      }
    } catch (e) {
      if (status) status.textContent = "Failed: " + (e.message || e);
    } finally {
      if (btn) {
        btn.disabled = !window._brollGeminiReady;
        btn.textContent = window._brollGeminiReady ? "✨ Generate AI photo" : "Set GEMINI_API_KEY";
      }
    }
  }

  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function soundSheetHtml() {
    const audioAssets = (typeof window.listTimelineAudioAssets === "function")
      ? window.listTimelineAudioAssets()
      : [];
    const musicClips = (typeof window.getTimelineMusicClips === "function")
      ? window.getTimelineMusicClips()
      : [];
    let html = `<p class="muted" style="font-size:.78rem;margin:0 0 10px;line-height:1.4">Add a music bed. Duck lowers the bed under speech (CapCut-style).</p>`;
    html += `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">`
      + `<button type="button" class="btn btn-primary" id="tlSoundUpload">⬆ Upload audio</button>`
      + `<button type="button" class="btn btn-secondary" id="tlSoundOpenMedia">📚 Media</button>`
      + `</div>`;

    html += `<h4 style="margin:0 0 6px;font-size:.82rem;color:#c8cdd8">On timeline</h4>`;
    if (!musicClips.length) {
      html += `<p class="muted" style="font-size:.74rem;margin:0 0 12px">No music clips yet.</p>`;
    } else {
      html += `<div class="tl-sound-list">`;
      musicClips.forEach((m) => {
        const id = escHtml(m.id);
        html += `<div class="tl-sound-row" data-music-id="${id}">`
          + `<div class="tl-sound-meta"><strong>${escHtml(m.label)}</strong><span>${escHtml(m.gain_db)} dB</span></div>`
          + `<label class="tl-sound-duck"><input type="checkbox" data-sound-duck="${id}" ${m.duck ? "checked" : ""}> Duck</label>`
          + `</div>`;
      });
      html += `</div>`;
    }

    html += `<h4 style="margin:14px 0 6px;font-size:.82rem;color:#c8cdd8">Audio library</h4>`;
    if (!audioAssets.length) {
      html += `<p class="muted" style="font-size:.74rem;margin:0">Upload an MP3/WAV/M4A to add a bed.</p>`;
    } else {
      html += `<div class="tl-sound-list">`;
      audioAssets.forEach((a) => {
        const label = a.filename || a.keyword || a.asset_id;
        const aid = escHtml(a.asset_id);
        html += `<div class="tl-sound-row">`
          + `<div class="tl-sound-meta"><strong>${escHtml(label)}</strong></div>`
          + `<button type="button" class="tl-chip-btn tl-chip-primary" data-sound-add="${aid}">Add</button>`
          + `</div>`;
      });
      html += `</div>`;
    }
    return html;
  }

  function refreshSoundSheetIfOpen() {
    if (_activeTool !== "sound") return;
    const sheet = document.getElementById("tlToolSheet");
    if (!sheet || sheet.hidden) return;
    openToolSheet("Sound", soundSheetHtml(), "sound");
    wireSoundSheet();
  }

  function wireSoundSheet() {
    const upload = document.getElementById("tlSoundUpload");
    const media = document.getElementById("tlSoundOpenMedia");
    if (upload) {
      upload.onclick = () => {
        if (typeof window.triggerTimelineAssetUpload === "function") {
          window.triggerTimelineAssetUpload("audio/*");
        } else {
          const input = document.getElementById("tlAssetFile");
          if (input) input.click();
        }
      };
    }
    if (media) {
      media.onclick = () => {
        closeToolSheet();
        openMobilePanel("media");
      };
    }
    document.querySelectorAll("[data-sound-add]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.soundAdd;
        btn.disabled = true;
        try {
          if (typeof window.addMusicAssetToTimeline === "function") {
            await window.addMusicAssetToTimeline(id);
            openToolSheet("Sound", soundSheetHtml(), "sound");
            wireSoundSheet();
          }
        } finally {
          btn.disabled = false;
        }
      });
    });
    document.querySelectorAll("[data-sound-duck]").forEach((cb) => {
      cb.addEventListener("change", () => {
        if (typeof window.setMusicClipDuck === "function") {
          window.setMusicClipDuck(cb.dataset.soundDuck, cb.checked);
        }
      });
    });
  }

  function onContextTool(toolId) {
    if (toolId === "export") {
      if (typeof window.runInstantExport === "function") window.runInstantExport();
      return;
    }
    if (toolId === "chat") {
      closeToolSheet();
      closeMobilePanel();
      _activeTool = "chat";
      refreshContextTools();
      document.body.classList.add("tl-coeditor-open");
      if (typeof window.openCoEditor === "function") window.openCoEditor();
      else alert("Co-editor is still loading — open Timeline again in a second.");
      return;
    }
    if (toolId === "sound") {
      openToolSheet("Sound", soundSheetHtml(), "sound");
      wireSoundSheet();
      return;
    }
    if (toolId === "library" || toolId === "add") {
      openMobilePanel("media");
      return;
    }
    if (toolId === "captions") {
      if (typeof window.jumpTimelineLook === "function") window.jumpTimelineLook("captions");
      else openMobilePanel("look");
      return;
    }
    if (toolId === "split") {
      const btn = document.getElementById("tlSplitBtn");
      if (btn) btn.click();
      return;
    }
    if (toolId === "delete") {
      const btn = document.getElementById("tlDeleteBtn");
      if (btn) btn.click();
      closeToolSheet();
      refreshContextTools();
      return;
    }
    if (toolId === "effects") {
      if (typeof window.addEffectClip === "function") window.addEffectClip("punch_zoom");
      refreshContextTools();
      openPropsInSheet("Effect", "edit");
      return;
    }
    if (toolId === "layout" || toolId === "size" || toolId === "kenburns"
        || toolId === "edit" || toolId === "type" || toolId === "strength"
        || toolId === "font" || toolId === "timing" || toolId === "volume" || toolId === "duck") {
      const titles = {
        layout: "Overlay layout",
        size: "Size & position",
        kenburns: "Ken Burns",
        edit: "Edit",
        type: "Effect type",
        strength: "Strength",
        font: "Font & look",
        timing: "Timing",
        volume: "Volume",
        duck: "Duck under voice",
      };
      openPropsInSheet(titles[toolId] || "Edit", toolId);
      return;
    }
    if (toolId === "replace") {
      const sel = typeof window.getTimelineSelection === "function"
        ? window.getTimelineSelection()
        : null;
      if (!sel || sel.track !== "overlay") {
        alert("Select an Overlay clip first.");
        return;
      }
      let prefill = "";
      if (typeof window.beginOverlayReplaceMode === "function") {
        window.beginOverlayReplaceMode(sel.id, { openMedia: false });
      }
      // Prefill from selected overlay keyword when available.
      try {
        const props = document.getElementById("tlProps");
        // Best-effort: keyword may be in save state; timeline exposes selection only.
        if (window.getOverlayReplaceTargetId && typeof window._lastOverlayKeyword === "string") {
          prefill = window._lastOverlayKeyword;
        }
      } catch (e) { /* ignore */ }
      // Ask timeline for keyword via a tiny helper if present.
      if (typeof window.getSelectedOverlayKeyword === "function") {
        prefill = window.getSelectedOverlayKeyword() || prefill;
      }
      openToolSheet("Replace media", replaceMediaSheetHtml(prefill), "replace");
      const gen = document.getElementById("tlReplaceAiGen");
      const browse = document.getElementById("tlReplaceBrowse");
      if (gen) gen.addEventListener("click", () => generateReplaceAiPhoto());
      if (browse) {
        browse.addEventListener("click", () => {
          closeToolSheet();
          openMobilePanel("media");
          if (typeof window.beginOverlayReplaceMode === "function") {
            window.beginOverlayReplaceMode(sel.id);
          }
        });
      }
      fetch("/broll/status").then((r) => r.json()).then((data) => {
        window._brollGeminiReady = !!(data.gemini_image_ready || (data.providers && data.providers.gemini_image));
        const g = document.getElementById("tlReplaceAiGen");
        if (g) {
          g.disabled = !window._brollGeminiReady;
          g.textContent = window._brollGeminiReady ? "✨ Generate AI photo" : "Set GEMINI_API_KEY";
        }
      }).catch(() => {});
      return;
    }
    if (toolId === "clipstyle") {
      openToolSheet("Clip style", clipStyleSheetHtml(), "clipstyle");
      const body = document.getElementById("tlToolSheetBody");
      if (body) {
        body.querySelectorAll("[data-clip-style]").forEach((card) => {
          card.addEventListener("click", () => {
            const id = card.dataset.clipStyle;
            if (typeof window.applyClipStyle === "function") {
              const ok = window.applyClipStyle(id);
              if (ok) {
                card.classList.add("active");
                setTimeout(() => closeToolSheet(), 350);
              }
            }
          });
        });
      }
    }
  }

  function wire() {
    syncPhoneClass();
    if (MQ.addEventListener) MQ.addEventListener("change", syncPhoneClass);
    else if (MQ.addListener) MQ.addListener(syncPhoneClass);

    document.querySelectorAll("#mobileBottomNav .mobile-nav-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.action === "export") {
          if (typeof window.runInstantExport === "function") window.runInstantExport();
          else alert("Export is still loading — wait a second and try again.");
          return;
        }
        const tab = btn.dataset.tab;
        setMobileNavActive(tab);
        closeMobilePanel();
        closeToolSheet();
        if (typeof window.setActiveTab === "function") window.setActiveTab(tab);
        else {
          const t = document.querySelector(`.main-tab[data-tab="${tab}"]`);
          if (t) t.click();
        }
        if (tab === "editor" && typeof window.ensureTimelineInit === "function") {
          window.ensureTimelineInit().then(() => refreshContextTools());
        }
      });
    });

    document.addEventListener("click", (e) => {
      const tabBtn = e.target && e.target.closest && e.target.closest(".main-tab[data-tab]");
      if (!tabBtn) return;
      setMobileNavActive(tabBtn.dataset.tab);
    });

    const libBtn = document.getElementById("tlMobilePanelBtn");
    if (libBtn) {
      libBtn.addEventListener("click", () => {
        if (document.body.classList.contains("tl-mobile-panel-open")) closeMobilePanel();
        else openMobilePanel("media");
      });
    }
    const closeBtn = document.getElementById("tlMobilePanelClose");
    if (closeBtn) closeBtn.addEventListener("click", closeMobilePanel);
    const scrim = document.getElementById("mobilePanelScrim");
    if (scrim) scrim.addEventListener("click", closeMobilePanel);

    const moreBtn = document.getElementById("tlToolbarMoreBtn");
    if (moreBtn) {
      moreBtn.addEventListener("click", () => {
        const open = document.body.classList.toggle("tl-toolbar-expanded");
        moreBtn.setAttribute("aria-expanded", open ? "true" : "false");
        moreBtn.textContent = open ? "▴ Less" : "⋯ More";
      });
    }

    const collapseBtn = document.getElementById("tlPreviewCollapseBtn");
    if (collapseBtn) collapseBtn.addEventListener("click", cyclePreviewSize);

    const ctx = document.getElementById("tlContextTools");
    if (ctx) {
      ctx.addEventListener("click", (e) => {
        const btn = e.target && e.target.closest && e.target.closest("[data-tool]");
        if (!btn) return;
        onContextTool(btn.dataset.tool);
      });
    }
    const sheetClose = document.getElementById("tlToolSheetClose");
    if (sheetClose) sheetClose.addEventListener("click", closeToolSheet);
    const sheetScrim = document.getElementById("tlToolSheetScrim");
    if (sheetScrim) sheetScrim.addEventListener("click", closeToolSheet);

    window.openMobileTimelinePanel = openMobilePanel;
    window.closeMobileTimelinePanel = closeMobilePanel;
    window.refreshMobileContextTools = refreshContextTools;
    window.closeMobileToolSheet = closeToolSheet;
    window.refreshSoundSheetIfOpen = refreshSoundSheetIfOpen;
    window.onCoEditorClosed = function () {
      document.body.classList.remove("tl-coeditor-open");
      if (_activeTool === "chat") {
        _activeTool = null;
        refreshContextTools();
      }
    };

    // If Timeline is already the active tab on load.
    const active = document.querySelector(".main-tab.active[data-tab]");
    if (active) setMobileNavActive(active.dataset.tab);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
