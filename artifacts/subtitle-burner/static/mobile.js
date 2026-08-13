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
        { id: "library", ico: "📚", label: "Library" },
        { id: "export", ico: "⚡", label: "Export" },
      ];
    }
    if (track === "main") {
      return [
        { id: "clipstyle", ico: "✨", label: "Clip style" },
        { id: "split", ico: "✂", label: "Split" },
        { id: "captions", ico: "Aa", label: "Captions" },
        { id: "effects", ico: "🎞", label: "Effects" },
        { id: "edit", ico: "✎", label: "Edit" },
        { id: "delete", ico: "🗑", label: "Delete" },
      ];
    }
    if (track === "overlay") {
      return [
        { id: "layout", ico: "▢", label: "Layout" },
        { id: "edit", ico: "✎", label: "Edit" },
        { id: "library", ico: "📚", label: "Replace" },
        { id: "delete", ico: "🗑", label: "Delete" },
      ];
    }
    if (track === "effects") {
      return [
        { id: "edit", ico: "✎", label: "Edit" },
        { id: "effects", ico: "＋", label: "Add FX" },
        { id: "delete", ico: "🗑", label: "Delete" },
      ];
    }
    if (track === "text") {
      return [
        { id: "edit", ico: "✎", label: "Edit" },
        { id: "captions", ico: "Aa", label: "Look" },
        { id: "delete", ico: "🗑", label: "Delete" },
      ];
    }
    if (track === "music") {
      return [
        { id: "edit", ico: "✎", label: "Edit" },
        { id: "library", ico: "📚", label: "Library" },
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

  function onContextTool(toolId) {
    if (toolId === "export") {
      if (typeof window.runInstantExport === "function") window.runInstantExport();
      return;
    }
    if (toolId === "library") {
      openMobilePanel("media");
      return;
    }
    if (toolId === "add") {
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
    if (toolId === "layout") {
      openPropsInSheet("Overlay layout", "layout");
      return;
    }
    if (toolId === "edit") {
      openPropsInSheet("Edit", "edit");
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
