/* Mobile shell helpers — bottom nav, Timeline sheet, toolbar More */
(function () {
  "use strict";

  const MQ = window.matchMedia("(max-width: 768px)");

  function isPhone() {
    return MQ.matches;
  }

  function syncPhoneClass() {
    document.body.classList.toggle("is-phone", isPhone());
    if (!isPhone()) {
      document.body.classList.remove("tl-mobile-panel-open", "tl-toolbar-expanded");
      const scrim = document.getElementById("mobilePanelScrim");
      if (scrim) scrim.hidden = true;
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
  }

  function openMobilePanel(ltab) {
    if (!isPhone()) return;
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
        if (typeof window.setActiveTab === "function") window.setActiveTab(tab);
        else {
          const t = document.querySelector(`.main-tab[data-tab="${tab}"]`);
          if (t) t.click();
        }
        if (tab === "editor" && typeof window.ensureTimelineInit === "function") {
          window.ensureTimelineInit();
        }
      });
    });

    // Keep bottom nav in sync when desktop/header tabs change.
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

    // Expose for timeline.js when Accept B-roll / Look jumps need the sheet.
    window.openMobileTimelinePanel = openMobilePanel;
    window.closeMobileTimelinePanel = closeMobilePanel;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
