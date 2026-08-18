// ---- Filler-word detection ----
// Single-word fillers + the start tokens of two-word fillers ("you" needs a
// trailing "know", "i" needs a trailing "mean"). The detector handles the
// multi-word cases by peeking at the next word.
const _FILLER_SINGLE = new Set([
  "um", "uh", "uhh", "uhm", "umm", "er", "erm",
  "ah", "ahh", "hm", "hmm", "mm", "mhm",
  "like", "basically", "literally", "actually",
  "kinda", "sorta", "anyway", "anyways",
  "okay", "ok", "right", "well",
]);
const _FILLER_PAIRS = [
  // [first, second]  → "you know", "i mean", "sort of", "kind of"
  ["you", "know"],
  ["i", "mean"],
  ["sort", "of"],
  ["kind", "of"],
];
function _stripWord(s) {
  return (s || "").toLowerCase().replace(/[^a-z']/g, "");
}
function _detectFillerIndices(words) {
  // Returns a Set of indices into *words* that the user could safely cut.
  // Two-word fillers contribute BOTH indices.
  const flagged = new Set();
  if (!words || !words.length) return flagged;
  for (let i = 0; i < words.length; i++) {
    const w = _stripWord(words[i].word);
    if (!w) continue;
    if (_FILLER_SINGLE.has(w)) {
      // Filler-only when it stands on its own — guard against false positives
      // like "I like that" by checking word duration and surrounding gap. A
      // real filler "like" usually clocks <300 ms; semantic "like" is longer.
      // For an MVP we just flag all matches and let the user review.
      flagged.add(i);
      continue;
    }
    if (i + 1 < words.length) {
      const next = _stripWord(words[i + 1].word);
      for (const [a, b] of _FILLER_PAIRS) {
        if (w === a && next === b) {
          flagged.add(i);
          flagged.add(i + 1);
          break;
        }
      }
    }
  }
  return flagged;
}
function _updateFillerBanner(words) {
  const banner = $("fillerBanner");
  const countEl = $("fillerCount");
  if (!banner || !countEl) return;
  const flagged = _detectFillerIndices(words);
  if (flagged.size === 0) {
    banner.classList.add("hidden");
    return;
  }
  banner.classList.remove("hidden");
  countEl.textContent = `${flagged.size} filler word${flagged.size === 1 ? "" : "s"} detected`;
}

// Hook the filler banner update into the existing phrase-list render path.
// The button itself is wired below renderPhraseList.

// ---- Punchword heuristic (mirrors _is_punchword_candidate in app.py) ----
// Used by the transcript editor to mark which words will be auto-emphasised
// in the burn. Keep this list in sync with _PUNCHWORD_STOPLIST in app.py.
const _PUNCHWORD_STOPLIST = new Set([
  "a","an","the","and","or","but","if","then","else","so","because",
  "as","at","by","for","from","in","into","of","off","on","onto","out",
  "over","to","up","with","without","is","am","are","was","were","be",
  "been","being","do","does","did","done","have","has","had","having",
  "i","me","my","we","us","our","you","your","he","him","his","she",
  "her","it","its","they","them","their","this","that","these","those",
  "what","which","who","whom","whose","when","where","why","how","not",
  "no","yes","there","here","than","too","very","just","also","only",
  "any","all","some","each","every","other","another","again","once",
  "more","most","such","much","many","few","like","go","goes","get","got",
  "make","made","know","see","say","said","can","could","will","would",
  "should","may","might","must","shall","let","yeah","ok","okay","um",
  "uh","oh","well",
]);

function _isPunchwordCandidate(raw) {
  raw = (raw || "").trim();
  if (!raw) return false;
  if (/\d/.test(raw)) return true;
  const clean = raw.replace(/[^A-Za-z']/g, "");
  if (!clean) return false;
  if (_PUNCHWORD_STOPLIST.has(clean.toLowerCase())) return false;
  if (raw[0] === raw[0].toUpperCase() && raw !== raw.toUpperCase() &&
      /[a-z]/.test(raw)) {
    return true; // mid-sentence proper-noun cue
  }
  return clean.length >= 6;
}

function _selectGroupPunchwordIndices(group, maxPerGroup = 2) {
  const candidates = [];
  for (let i = 0; i < group.length; i++) {
    const text = (group[i].word || "").toString();
    if (_isPunchwordCandidate(text)) {
      const visibleLen = text.replace(/[^A-Za-z0-9']/g, "").length;
      candidates.push([-visibleLen, i]);
    }
  }
  candidates.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  return new Set(candidates.slice(0, maxPerGroup).map(c => c[1]));
}

// ---- Color themes ----
// Each theme also defines an *accent* used for non-active "punchword"
// emphasis (long words, numbers, proper nouns). Picked to contrast with
// both the primary and highlight without colliding with either.
const THEMES = [
  { name: "Classic",   primary: "#FFFFFF", highlight: "#FFD60A", outline: "#000000", accent: "#FF6B35" },
  { name: "TikTok",    primary: "#FFFFFF", highlight: "#00F2EA", outline: "#000000", accent: "#FF4D8F" },
  { name: "Fire",      primary: "#FFFFFF", highlight: "#FF4D4D", outline: "#1a0000", accent: "#FFD60A" },
  { name: "Neon",      primary: "#00FF88", highlight: "#FF00FF", outline: "#000000", accent: "#00CFFF" },
  { name: "Mint",      primary: "#FFFFFF", highlight: "#7CFFB2", outline: "#0a1f14", accent: "#FFB347" },
  { name: "Sunset",    primary: "#FFE4B5", highlight: "#FF6B35", outline: "#2a0f00", accent: "#FFD60A" },
  { name: "Mono",      primary: "#FFFFFF", highlight: "#AAAAAA", outline: "#000000", accent: "#666666" },
];

// Preset emoji rules — keyword and matching emoji
const EMOJI_PRESETS = [
  { keyword: "fire",    emoji: "🔥" },
  { keyword: "love",    emoji: "❤️" },
  { keyword: "money",   emoji: "💰" },
  { keyword: "laugh",   emoji: "😂" },
  { keyword: "wow",     emoji: "😮" },
  { keyword: "win",     emoji: "🏆" },
  { keyword: "sad",     emoji: "😢" },
  { keyword: "cool",    emoji: "😎" },
  { keyword: "crazy",   emoji: "🤯" },
  { keyword: "go",      emoji: "🚀" },
];

// ---- Element refs ----
const $ = (id) => document.getElementById(id);
const drop = $("drop"), fileInput = $("file"), fn = $("filename");
const sizeEl = $("size"), sizeVal = $("sizeVal");
const owEl = $("outlineWidth"), owVal = $("owVal");
const posEl = $("posY"), posVal = $("posVal");
const groupEl = $("group"), groupVal = $("groupVal");
const primaryEl = $("primary"), highlightEl = $("highlight"), outlineEl = $("outlineColor");
const accentEl = $("accent");
const go = $("go"), progress = $("progress"), barFill = $("barFill"), statusText = $("statusText");
const retryTranscribeBtn = $("retryTranscribeBtn");
const redropVideoBtn = $("redropVideoBtn");
const retryTranscribeHint = $("retryTranscribeHint");
const result = $("result"), player = $("player"), dl = $("dl");
const editor = $("editor"), rowCount = $("rowCount");
const renderBtn = $("renderBtn"), reEditBtn = $("reEditBtn");
const retranscribeBtn = $("retranscribeBtn");
const exportSrtBtn = $("exportSrtBtn");
const exportVttBtn = $("exportVttBtn");
const emojiRulesList = $("emojiRulesList"), addRuleBtn = $("addRuleBtn");
const emojiPresetsDiv = $("emojiPresets");
const previewWrap = $("audioPreviewWrap"), previewBtn = $("previewAudio");
const audioPreviewArea = $("audioPreviewArea"), audioPlayer = $("audioPlayer");
const audioPreviewStatus = $("audioPreviewStatus");
const sourcePlayer = $("sourcePlayer");
const phraseListEl = $("phraseList");

let currentFile = null;
let currentJobId = null;
let currentWords = []; // original words from transcription [{word, start, end}]
let audioBlobUrl = null;
let draftSaveTimer = null;
// While an upload/transcribe is in flight, never bounce back to the empty
// landing page — refreshJobsList used to see 0 localStorage IDs mid-upload
// and hide the progress UI (looked like 1–5% then "Choose a video to start").
let _ingestBusy = 0;
let _progressPhase = null; // "upload" | "transcribe" | null

// ---- Edit-tab undo / redo (transcript + style + emoji rules) ----
const EDIT_MAX_UNDO = 40;
let editUndoStack = [];
let editRedoStack = [];
let editHistSuspended = false;
let editHistoryArmed = false;

function editTabActive() {
  const btn = document.querySelector('.main-tab[data-tab="edit"]');
  return !!(btn && btn.classList.contains("active"));
}

function captureEditSnapshot() {
  const words = (typeof collectEditedWords === "function")
    ? collectEditedWords({ silent: true })
    : [];
  return JSON.stringify({
    words: words.length ? words : (currentWords || []),
    style: (typeof getStyle === "function") ? getStyle() : {},
    emoji_rules: (typeof getEmojiRules === "function") ? getEmojiRules() : {},
  });
}

function clearEditHistory() {
  editUndoStack = [];
  editRedoStack = [];
  editHistoryArmed = false;
  updateEditHistoryButtons();
}

function pushEditHistory() {
  if (editHistSuspended || !currentJobId) return;
  const snap = captureEditSnapshot();
  if (editUndoStack.length && editUndoStack[editUndoStack.length - 1] === snap) return;
  editUndoStack.push(snap);
  if (editUndoStack.length > EDIT_MAX_UNDO) editUndoStack.shift();
  editRedoStack = [];
  updateEditHistoryButtons();
}

function armEditHistory() {
  if (editHistSuspended || editHistoryArmed || !currentJobId) return;
  pushEditHistory();
  editHistoryArmed = true;
}

function disarmEditHistory() {
  editHistoryArmed = false;
}

function restoreEditSnapshot(snap) {
  if (!snap) return;
  const d = JSON.parse(snap);
  editHistSuspended = true;
  currentWords = _sanitizeWords(d.words || []);
  if (d.style && typeof applyStyle === "function") applyStyle(d.style);
  if (d.emoji_rules && typeof applyEmojiRules === "function") applyEmojiRules(d.emoji_rules);
  renderPhraseList(currentWords);
  updateRowCount();
  if (typeof updateFontPreview === "function") updateFontPreview();
  editHistSuspended = false;
  scheduleDraftSave();
  updateEditHistoryButtons();
}

function undoEdit() {
  if (!editUndoStack.length) return;
  editRedoStack.push(captureEditSnapshot());
  restoreEditSnapshot(editUndoStack.pop());
}

function redoEdit() {
  if (!editRedoStack.length) return;
  editUndoStack.push(captureEditSnapshot());
  restoreEditSnapshot(editRedoStack.pop());
}

function updateEditHistoryButtons() {
  const u = $("editUndoBtn");
  const r = $("editRedoBtn");
  if (u) u.disabled = !editUndoStack.length;
  if (r) r.disabled = !editRedoStack.length;
}

function bindEditHistoryArming(el) {
  if (!el) return;
  el.addEventListener("pointerdown", armEditHistory);
  el.addEventListener("focus", armEditHistory);
  el.addEventListener("pointerup", disarmEditHistory);
  el.addEventListener("blur", disarmEditHistory);
}

document.addEventListener("DOMContentLoaded", () => {
  const u = $("editUndoBtn");
  const r = $("editRedoBtn");
  if (u) u.onclick = () => undoEdit();
  if (r) r.onclick = () => redoEdit();
  updateEditHistoryButtons();
});

document.addEventListener("keydown", (e) => {
  if (!editTabActive() || !currentJobId) return;
  const mod = e.metaKey || e.ctrlKey;
  if (!mod) return;
  const key = e.key.toLowerCase();
  if (key === "z") {
    e.preventDefault();
    if (e.shiftKey) redoEdit();
    else undoEdit();
  } else if (key === "y") {
    e.preventDefault();
    redoEdit();
  }
});

// ---- Audio engine tab state ----
// "ffmpeg" or "auphonic". Defaults to ffmpeg; only switches if tabs are present.
let activeAudioTab = "ffmpeg";

const ffmpegTabContent     = $("ffmpegTabContent");
const auphonicTabContent   = $("auphonicTabContent");    // null when auphonic_enabled=false
const elevenlabsTabContent = $("elevenlabsTabContent");  // null when elevenlabs_enabled=false
const dolbyTabContent      = $("dolbyTabContent");       // null when dolby_enabled=false
const audioTabsEl          = $("audioTabs");             // null when no AI provider enabled

const audioTabContents = {
  ffmpeg: ffmpegTabContent,
  auphonic: auphonicTabContent,
  elevenlabs: elevenlabsTabContent,
  dolby: dolbyTabContent,
};

function showAudioTabContent(name) {
  for (const [key, el] of Object.entries(audioTabContents)) {
    if (!el) continue;
    el.classList.toggle("hidden", key !== name);
  }
}

if (audioTabsEl) {
  audioTabsEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".audio-tab");
    if (!btn) return;
    const tab = btn.dataset.tab;
    if (tab === activeAudioTab) return;
    activeAudioTab = tab;
    audioTabsEl.querySelectorAll(".audio-tab").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    showAudioTabContent(tab);
    updateAudioPreviewVisibility();
    scheduleDraftSave();
  });
}

// ---- Auphonic presets ----
const AUPHONIC_PRESETS = {
  interview:    { speech_isolation: true,  adaptive_leveler: true,  noise_hum_reduction: true,  highpass: true,  loudness_lufs: -16 },
  podcast:      { speech_isolation: true,  adaptive_leveler: true,  noise_hum_reduction: true,  highpass: true,  loudness_lufs: -16 },
  storytelling: { speech_isolation: true,  adaptive_leveler: true,  noise_hum_reduction: false, highpass: true,  loudness_lufs: -14 },
  cleanroom:    { speech_isolation: false, adaptive_leveler: true,  noise_hum_reduction: false, highpass: false, loudness_lufs: -16 },
};

// State for preset-only fields (no direct toggles in UI)
let auphonicNoiseHumReduction = true;
let auphonicHighpass = true;

function applyAuphonicPreset(name) {
  const p = AUPHONIC_PRESETS[name];
  if (!p) return;
  const si  = $("auphonicSpeechIsolation");
  const al  = $("auphonicAdaptiveLeveler");
  const ld  = $("auphonicLoudness");
  if (si)  si.checked  = p.speech_isolation;
  if (al)  al.checked  = p.adaptive_leveler;
  if (ld)  ld.value    = p.loudness_lufs !== null ? String(p.loudness_lufs) : "";
  auphonicNoiseHumReduction = p.noise_hum_reduction;
  auphonicHighpass          = p.highpass;
  // Mark active preset button
  if (auphonicTabContent) {
    auphonicTabContent.querySelectorAll(".auphonic-preset-btn").forEach(b => {
      b.classList.toggle("active", b.dataset.preset === name);
    });
  }
  scheduleDraftSave();
}

// Apply default preset on load (Interview) — only if elements exist
if ($("auphonicSpeechIsolation")) {
  applyAuphonicPreset("interview");
  auphonicTabContent.querySelectorAll(".auphonic-preset-btn").forEach(btn => {
    btn.addEventListener("click", () => applyAuphonicPreset(btn.dataset.preset));
  });
}

// ---- Audio toggle visibility ----
// The audio preview panel lives inside the editor section (available after transcription).
// Filter null in case any checkbox is omitted from the rendered HTML
// (e.g. by a Jinja conditional). Without the filter a missing element would
// throw on .addEventListener / .checked and crash the rest of init.
const audioCheckboxes = ["noiseReduction", "voiceBoost", "loudnessNorm", "voiceClarity"]
  .map($)
  .filter(Boolean);

function updateAudioPreviewVisibility() {
  if (activeAudioTab !== "ffmpeg") {
    // Auphonic mode: no preview panel
    previewWrap.classList.add("hidden");
    audioPreviewArea.classList.add("hidden");
    return;
  }
  const anyOn = audioCheckboxes.some(cb => cb.checked);
  if (anyOn && currentJobId) {
    previewWrap.classList.remove("hidden");
  } else {
    previewWrap.classList.add("hidden");
    audioPreviewArea.classList.add("hidden");
  }
}

audioCheckboxes.forEach(cb => cb.addEventListener("change", updateAudioPreviewVisibility));

// ---- Instagram Pro preset ----
$("instagramPreset").onclick = () => {
  pushEditHistory();
  $("noiseReduction").checked = true;
  $("voiceBoost").checked = true;
  $("loudnessNorm").checked = true;
  $("voiceClarity").checked = true;
  updateAudioPreviewVisibility();
  scheduleDraftSave();
};

// ---- Themes ----
const themesDiv = $("themes");
THEMES.forEach((t, i) => {
  const b = document.createElement("div");
  b.className = "theme" + (i === 0 ? " active" : "");
  b.textContent = t.name;
  b.onclick = () => {
    pushEditHistory();
    document.querySelectorAll(".theme").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    primaryEl.value = t.primary;
    highlightEl.value = t.highlight;
    outlineEl.value = t.outline;
    if (accentEl && t.accent) accentEl.value = t.accent;
    scheduleDraftSave();
  };
  themesDiv.appendChild(b);
});

// ---- Viral Presets click handler ----
const VIRAL_PRESETS = {
  hormozi: { font: "Montserrat Thin Black", size: 64, primary: "#FFFFFF", highlight: "#FFD60A", accent: "#00FF88", outline: "#000000", outlineWidth: 4, allCaps: true, shadow: 2 },
  mrbeast: { font: "Integral CF", size: 68, primary: "#FFFFFF", highlight: "#00F2EA", accent: "#FF0055", outline: "#000000", outlineWidth: 5, allCaps: true, shadow: 3 },
  neon:    { font: "Bebas Neue", size: 72, primary: "#00FF88", highlight: "#FF00FF", accent: "#00CFFF", outline: "#110022", outlineWidth: 3, allCaps: true, shadow: 2 },
  karaoke: { font: "DM Sans", size: 56, primary: "#F8FAFC", highlight: "#6366F1", accent: "#EC4899", outline: "#0F172A", outlineWidth: 2, allCaps: false, shadow: 1 },
};

document.querySelectorAll("#viralPresets .theme").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#viralPresets .theme").forEach(x => x.classList.remove("active"));
    btn.classList.add("active");
    const presetKey = btn.dataset.preset;
    const p = VIRAL_PRESETS[presetKey];
    if (!p) return;
    const fontEl = $("font");
    if (fontEl) fontEl.value = p.font;
    if (sizeEl) { sizeEl.value = p.size; sizeVal.textContent = p.size; }
    if (primaryEl) primaryEl.value = p.primary;
    if (highlightEl) highlightEl.value = p.highlight;
    if (accentEl) accentEl.value = p.accent;
    if (outlineEl) outlineEl.value = p.outline;
    if (owEl) { owEl.value = p.outlineWidth; owVal.textContent = p.outlineWidth; }
    const capsEl = $("allCaps");
    if (capsEl) capsEl.checked = p.allCaps;
    const shadowEl = $("shadow");
    if (shadowEl) shadowEl.value = p.shadow;
    scheduleDraftSave();
  });
});

// ---- Live labels ----
sizeEl.oninput  = () => { sizeVal.textContent = sizeEl.value; scheduleDraftSave(); };
owEl.oninput    = () => { owVal.textContent = owEl.value; scheduleDraftSave(); };
posEl.oninput   = () => { posVal.textContent = posEl.value + "%"; scheduleDraftSave(); };
groupEl.oninput = () => {
  pushEditHistory();
  const edited = collectEditedWords({ silent: true });
  if (edited.length) currentWords = edited;
  groupVal.textContent = groupEl.value;
  renderPhraseList(currentWords);
  updateRowCount();
  scheduleDraftSave();
};

["font", "primary", "highlight", "accent", "outlineColor", "allCaps", "shadow", "smoothTimings", "punchwordEmphasis", "reframeEnabled"].forEach(id => {
  const el = $(id);
  if (!el) return;
  bindEditHistoryArming(el);
  el.addEventListener("input", scheduleDraftSave);
  el.addEventListener("change", scheduleDraftSave);
});
[sizeEl, owEl, posEl].forEach(bindEditHistoryArming);

// ---- Font preview ----
// Pull a list of TTF/OTF files from the backend and inject @font-face rules
// so the dropdown values render in their actual typeface inside the preview.
// Filename → CSS font-family is best-effort: we strip the extension, replace
// dashes/underscores with spaces, and let the browser pick by exact match
// against the dropdown's option values. Anything that doesn't match falls
// back to the platform sans (still useful for size/color/outline checks).
// Track which subtitle families have actually finished downloading. The
// preview's canvas.measureText() silently falls back to the system default
// (much wider) until the real TTF lands, so we kick off explicit loads and
// re-render the preview the moment the active family becomes available.
const _loadedSubtitleFonts = new Set();

async function _registerSubtitleFonts() {
  let fonts = [];
  try {
    const res = await fetch("/list-fonts");
    fonts = (await res.json()).fonts || [];
  } catch { return; }
  const wanted = Array.from(document.querySelectorAll("#font option"))
    .map(o => o.value);
  const norm = s => s.toLowerCase().replace(/[-_\s]+/g, "");
  const styleEl = document.createElement("style");
  let css = "";
  const loadPromises = [];
  for (const family of wanted) {
    const target = norm(family);
    const match = fonts.find(f => {
      const stem = f.replace(/\.[^.]+$/, "");
      return norm(stem) === target || norm(stem).includes(target) || target.includes(norm(stem));
    });
    if (!match) continue;
    const url = `/fonts/${encodeURIComponent(match)}`;
    css += `@font-face { font-family: ${JSON.stringify(family)}; src: url("${url}"); font-display: swap; }\n`;
    // Trigger an explicit download. Without this, the font isn't fetched
    // until the first DOM node references it, which races the preview's
    // measureText call.
    if (document.fonts && document.fonts.load) {
      loadPromises.push(
        document.fonts.load(`16px "${family}"`).then(() => {
          _loadedSubtitleFonts.add(family);
          // If the user is currently looking at this family, re-measure.
          if ($("font") && $("font").value === family) {
            updateFontPreview();
          }
        }).catch(() => { /* missing TTF → silent fallback */ })
      );
    }
  }
  styleEl.textContent = css;
  document.head.appendChild(styleEl);
  await Promise.all(loadPromises);
}
_registerSubtitleFonts();

const fontPreviewWord = $("fontPreviewWord");
const fontPreviewText = $("fontPreviewText");
const fontPreviewStage = $("fontPreviewStage");
let _fontPreviewMeasureCtx = null;

// Burn-canvas dimensions for the active job. Defaults to 1080×1920 (9:16
// short-form) until the backend reports the real ones; updated on every job
// switch and whenever Quality Boost toggles. The preview frame's aspect ratio
// and font scaling key off these so the preview matches the actual render.
let _previewCanvas = { w: 1080, h: 1920, qualityBoost: false };

async function refreshPreviewCanvas(jobId) {
  if (!jobId) return;
  try {
    const res = await fetch(`/job-canvas/${jobId}`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.error) return;
    _previewCanvas = {
      w: data.burn_width || 1080,
      h: data.burn_height || 1920,
      qualityBoost: !!data.quality_boost,
    };
    if (fontPreviewStage) {
      fontPreviewStage.style.aspectRatio = `${_previewCanvas.w} / ${_previewCanvas.h}`;
      // Lock the long-edge cap so wide videos don't blow out the panel.
      fontPreviewStage.style.maxWidth =
        _previewCanvas.w >= _previewCanvas.h ? "320px" : "220px";
      // Drop in a representative frame from the source so the user sees the
      // subtitle landing on actual footage. Cache-bust per session only —
      // the backend regenerates when the source mtime changes.
      fontPreviewStage.style.backgroundImage = `url(/job-poster/${jobId}.jpg)`;
      fontPreviewStage.style.backgroundSize = "cover";
      fontPreviewStage.style.backgroundPosition = "center";
      fontPreviewStage.classList.add("has-poster");
    }
    updateFontPreview();
  } catch { /* offline / unknown job — keep defaults */ }
}

function _hexToRgb(hex) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
  return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [0, 0, 0];
}

function updateFontPreview() {
  if (!fontPreviewWord) return;
  const family = $("font").value;
  const size = parseInt(sizeEl.value, 10) || 56;
  const primary = primaryEl.value;
  const outlineColor = outlineEl.value;
  const outlineWidth = parseInt(owEl.value, 10) || 0;
  const allCaps = $("allCaps").checked;
  const shadow = $("shadow").checked;
  const posY = parseInt(posEl.value, 10) || 85;
  const text = (fontPreviewText && fontPreviewText.value.trim()) || "Wait for it…";

  // Scale font-size from the burn canvas (after any Quality Boost upscale)
  // to the preview frame's pixel height so size/position look proportionally
  // honest for THIS job's actual output, not a hard-coded 1080-tall canvas.
  const stageH = fontPreviewStage ? fontPreviewStage.clientHeight : 240;
  const stageW = fontPreviewStage ? fontPreviewStage.clientWidth : 135;
  const canvasH = _previewCanvas.h || 1080;
  const previewSize = Math.max(10, Math.round(size * (stageH / canvasH)));
  // Mirror the renderer's behaviour: words wider than the 78% safe area get
  // hyphen-broken across lines instead of being shrunk. Use canvas
  // measureText so the break math matches the actual font's pixel width
  // (heavy slab fonts are way wider than a flat 0.55-per-char estimate).
  const safeW = stageW * 0.78;
  const measure = (() => {
    if (!_fontPreviewMeasureCtx) {
      _fontPreviewMeasureCtx = document
        .createElement("canvas")
        .getContext("2d");
    }
    const ctx = _fontPreviewMeasureCtx;
    const allCapsBool = $("allCaps").checked;
    // No weight override here — see CSS comment on .font-preview-word. Most
    // display fonts ship one weight; faking 900 makes measureText report
    // 25–30% wider than the actual ASS render produces.
    ctx.font = `${previewSize}px "${family}", "Arial Black", sans-serif`;
    return (s) => ctx.measureText(allCapsBool ? s.toUpperCase() : s).width;
  })();
  // Mirrors NO_SHRINK_MAX_CHARS in app.py — words this length or shorter
  // are never broken or shrunk even if they brush the safe area.
  const NO_BREAK_MAX_CHARS = 15;
  function hyphenateWord(w) {
    if (w.length <= NO_BREAK_MAX_CHARS) return w;
    if (measure(w) <= safeW) return w;
    // Find the minimum number of lines N such that the word can be split
    // into N roughly-even chunks where every chunk (with its trailing
    // hyphen, except the last) fits the safe area. Greedy max-pack would
    // give "INTERNAT-/IONALIZ-/ATION" (3 lines, last too short); even
    // splits at N=2 gives "INTERNATI-/ONALIZATION" if that fits.
    function fitsAt(n) {
      for (let k = 0; k < n; k++) {
        const start = Math.floor((w.length * k) / n);
        const end = Math.floor((w.length * (k + 1)) / n);
        const piece = w.slice(start, end) + (k < n - 1 ? "-" : "");
        if (measure(piece) > safeW) return false;
      }
      return true;
    }
    let n = 2;
    while (n < w.length && !fitsAt(n)) n += 1;
    const out = [];
    for (let k = 0; k < n; k++) {
      const start = Math.floor((w.length * k) / n);
      const end = Math.floor((w.length * (k + 1)) / n);
      out.push(w.slice(start, end) + (k < n - 1 ? "-" : ""));
    }
    return out.join("\n");
  }
  const wrapped = (text || "")
    .split(/\s+/)
    .map(hyphenateWord)
    .join(" ");

  fontPreviewWord.style.fontFamily = `"${family}", "Arial Black", sans-serif`;
  fontPreviewWord.style.fontSize = `${previewSize}px`;
  fontPreviewWord.style.color = primary;
  fontPreviewWord.style.textTransform = allCaps ? "uppercase" : "none";
  fontPreviewWord.style.top = `${posY}%`;
  // Push lines apart so a thick text-shadow outline on one line doesn't
  // overlap the outline of the next line and merge into a black blob.
  // Each pixel of outline needs roughly 2× the room (top + bottom).
  const previewOutline = Math.round(outlineWidth * (stageH / canvasH));
  const extraGap = (previewOutline * 2) / Math.max(1, previewSize);
  fontPreviewWord.style.lineHeight = (1.1 + extraGap).toFixed(3);

  // Build a multi-direction text-shadow stack to mimic libass's outline +
  // optional drop shadow. Browsers have no true text-stroke, and the outline
  // width has to be scaled from 1080-canvas pixels to preview pixels or it
  // visually overwhelms the smaller preview text.
  const layers = [];
  if (previewOutline > 0) {
    const w = previewOutline;
    for (let dx = -w; dx <= w; dx++) {
      for (let dy = -w; dy <= w; dy++) {
        if (dx === 0 && dy === 0) continue;
        layers.push(`${dx}px ${dy}px 0 ${outlineColor}`);
      }
    }
  }
  if (shadow) {
    const [r, g, b] = _hexToRgb(outlineColor);
    layers.push(`3px 4px 6px rgba(${r}, ${g}, ${b}, 0.65)`);
  }
  fontPreviewWord.style.textShadow = layers.join(", ");
  fontPreviewWord.style.whiteSpace = "pre-line";
  fontPreviewWord.textContent = wrapped;
}

// Wire every style control to update the preview live, in addition to the
// draft-save path that's already attached.
["font", "size", "primary", "outlineColor", "outlineWidth", "allCaps", "shadow", "posY"].forEach(id => {
  const el = $(id);
  if (!el) return;
  el.addEventListener("input", updateFontPreview);
  el.addEventListener("change", updateFontPreview);
});
// Repaint the transcript when the accent color or punchword toggle change
// — those decide which words get tinted.
if (accentEl) {
  accentEl.addEventListener("input", () => {
    if (currentWords && currentWords.length) renderPhraseList(currentWords);
  });
}
if ($("punchwordEmphasis")) {
  $("punchwordEmphasis").addEventListener("change", () => {
    if (currentWords && currentWords.length) renderPhraseList(currentWords);
  });
}

if ($("font")) {
  // When the dropdown switches families, force a font load before measuring.
  $("font").addEventListener("change", async () => {
    const fam = $("font").value;
    if (document.fonts && document.fonts.load && !_loadedSubtitleFonts.has(fam)) {
      try {
        await document.fonts.load(`16px "${fam}"`);
        _loadedSubtitleFonts.add(fam);
      } catch { /* fallback */ }
      updateFontPreview();
    }
  });
}
if (fontPreviewText) fontPreviewText.addEventListener("input", updateFontPreview);
// Quality Boost upscales the burn canvas — re-fetch dimensions when toggled.
if ($("qualityBoost")) {
  $("qualityBoost").addEventListener("change", () => {
    if (currentJobId) refreshPreviewCanvas(currentJobId);
  });
}
// Theme buttons mutate primary/outline directly via .value = …, which doesn't
// fire input events — so refresh after click.
document.addEventListener("click", (e) => {
  if (e.target && e.target.classList && e.target.classList.contains("theme")) {
    setTimeout(updateFontPreview, 0);
  }
});
updateFontPreview();

// Wire the ElevenLabs sliders so their pill labels update live.
const elevenWet = $("elevenWetMix");
const elevenGain = $("elevenGain");
if (elevenWet) {
  elevenWet.oninput = () => {
    $("elevenWetMixVal").textContent = elevenWet.value + "%";
    scheduleDraftSave();
  };
}
if (elevenGain) {
  elevenGain.oninput = () => {
    const v = parseFloat(elevenGain.value);
    $("elevenGainVal").textContent = (v > 0 ? "+" : "") + v + " dB";
    scheduleDraftSave();
  };
}

const dolbySpeechIso = $("dolbySpeechIsolation");
if (dolbySpeechIso) {
  dolbySpeechIso.oninput = () => {
    $("dolbySpeechIsolationVal").textContent = dolbySpeechIso.value;
    scheduleDraftSave();
  };
}

const auphonicWetEl = $("auphonicWetMix");
if (auphonicWetEl) {
  auphonicWetEl.oninput = () => {
    $("auphonicWetMixVal").textContent = auphonicWetEl.value + "%";
    scheduleDraftSave();
  };
}
const auphonicGainEl = $("auphonicGain");
if (auphonicGainEl) {
  auphonicGainEl.oninput = () => {
    const v = parseFloat(auphonicGainEl.value);
    $("auphonicGainVal").textContent = (v > 0 ? "+" : "") + v + " dB";
    scheduleDraftSave();
  };
}

const audioOffsetEl = $("audioOffset");
function _updateAudioOffsetLabel() {
  if (!audioOffsetEl) return;
  const v = parseFloat(audioOffsetEl.value);
  const sign = v > 0 ? "+" : "";
  $("audioOffsetVal").textContent = sign + v.toFixed(2) + "s";
}
if (audioOffsetEl) {
  audioOffsetEl.oninput = () => {
    _updateAudioOffsetLabel();
    scheduleDraftSave();
  };
}

["noiseReduction", "voiceBoost", "loudnessNorm", "voiceClarity", "auphonicSpeechIsolation", "auphonicAdaptiveLeveler", "auphonicLoudness", "elevenVoiceBoost", "elevenLoudnessNorm", "elevenVoiceClarity", "dolbyContentType", "dolbyNoiseReduction", "dolbyDynamics", "dolbyLoudness"].forEach(id => {
  const el = $(id);
  if (!el) return;
  el.addEventListener("change", scheduleDraftSave);
});

// ---- Drag & drop ----
// No click handler here: the drop zone's inner element is a <label for="file">,
// so the browser opens the picker itself. Calling fileInput.click() as well
// would fire the picker twice.
if (drop) {
  ["dragenter", "dragover"].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("hover"); }));
  ["dragleave", "drop"].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("hover"); }));
  drop.addEventListener("drop", e => {
    if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
  });
}
if (fileInput) {
  fileInput.onchange = () => { if (fileInput.files.length) handleFiles(fileInput.files); };
}

// Mirrors ALLOWED_EXT in app.py. The server rejects anything else with a 400,
// so screen for it here and say so rather than letting the upload fail.
const ACCEPTED_VIDEO_EXT = ["mp4", "mov", "mkv", "webm", "avi", "m4v"];

// The server identifies uploads by extension too, so a phone file that
// arrives without a usable one is refused at both ends. When the browser
// tells us the MIME type we can supply the matching extension ourselves.
const MIME_TO_EXT = {
  "video/mp4": "mp4",
  "video/quicktime": "mov",
  "video/x-quicktime": "mov",
  "video/x-matroska": "mkv",
  "video/webm": "webm",
  "video/avi": "avi",
  "video/x-msvideo": "avi",
  "video/x-m4v": "m4v",
  "video/m4v": "m4v",
};

function videoExtOf(f) {
  const parts = (f.name || "").split(".");
  return parts.length > 1 ? parts.pop().toLowerCase() : "";
}

function isAcceptedVideo(f) {
  if (ACCEPTED_VIDEO_EXT.includes(videoExtOf(f))) return true;
  // iOS and some Android pickers hand over names the server won't recognise
  // ("capturedvideo", "image.mov" variants, or no extension at all), so trust
  // the MIME type when it maps onto a format we support.
  return !!MIME_TO_EXT[(f.type || "").toLowerCase()];
}

// Give the file a name the server will accept, without touching its bytes.
function normalizeVideoFile(f) {
  if (ACCEPTED_VIDEO_EXT.includes(videoExtOf(f))) return f;
  const ext = MIME_TO_EXT[(f.type || "").toLowerCase()];
  if (!ext) return f;
  const base = (f.name || "video").replace(/\.[^.]*$/, "") || "video";
  try {
    return new File([f], `${base}.${ext}`, { type: f.type });
  } catch (e) {
    return f;   // very old browsers: send it as-is and let the server answer
  }
}

function handleFiles(files) {
  const all = Array.from(files);
  const videos = all.filter(isAcceptedVideo).map(normalizeVideoFile);
  if (!videos.length) {
    const names = all.map(f => f.name).join(", ");
    alert(all.length
      ? `Can't use ${names}.\n\nSupported formats: ${ACCEPTED_VIDEO_EXT.join(", ")}.`
      : "Please select video files.");
    return;
  }
  const skipped = all.filter(f => !isAcceptedVideo(f));
  if (skipped.length) {
    alert(`Skipping ${skipped.map(f => f.name).join(", ")} — supported formats are ${ACCEPTED_VIDEO_EXT.join(", ")}.`);
  }

  // Large iPhone MOVs often truncate mid-upload on slow links — warn up front.
  const bigMov = videos.find((f) => {
    const name = (f.name || "").toLowerCase();
    return (name.endsWith(".mov") || name.endsWith(".m4v")) && f.size > 80 * 1024 * 1024;
  });
  if (bigMov) {
    const mb = Math.round(bigMov.size / (1024 * 1024));
    const ok = confirm(
      `"${bigMov.name}" is ~${mb} MB (common for iPhone HEVC).\n\n` +
      "Keep this tab open until upload shows 100%. If Windows can't play it, that's normal — Drive re-encodes for streaming.\n\n" +
      "OK = upload now.\nCancel = export Most Compatible / H.264 MP4 on the phone first (smaller + more reliable)."
    );
    if (!ok) return;
  }

  // Leave the empty hero immediately so the user sees Ingest progress.
  _ingestBusy += 1;
  const emptyEl = document.getElementById("emptyState");
  const shellEl = document.getElementById("appShell");
  const headerEl = document.getElementById("appHeader");
  if (emptyEl) emptyEl.classList.add("hidden");
  if (shellEl) shellEl.classList.remove("hidden");
  if (headerEl) headerEl.classList.remove("hidden");

  // Always auto-start upload + transcription on drop/pick. Staging a file and
  // waiting for "Transcribe" looked broken once the user already had jobs
  // (filename appeared, nothing happened).
  currentFile = null;
  if (fn) {
    fn.textContent = videos.length === 1
      ? `Uploading ${videos[0].name}…`
      : `Uploading ${videos.length} videos…`;
  }
  const emptyStatus = $("emptyPickStatus");
  if (emptyStatus) {
    emptyStatus.textContent = videos.length === 1
      ? `Uploading ${videos[0].name}…`
      : `Uploading ${videos.length} videos…`;
  }
  if (go) go.disabled = true;
  // Show Ingest + progress bar right away (don't wait for XHR to start).
  if (typeof setActiveTab === "function") setActiveTab("ingest");
  if (result) result.classList.add("hidden");
  if (editor) editor.classList.add("hidden");
  if (progress) {
    progress.classList.remove("hidden");
    if (barFill) barFill.style.width = "3%";
    if (statusText) {
      statusText.textContent = videos.length === 1
        ? `Uploading ${videos[0].name}…`
        : `Uploading ${videos.length} videos…`;
    }
  }

  Promise.all(videos.map((f, idx) => uploadAndTranscribe(f, getPreCleanFlag(), idx === 0)))
    .then(ids => {
      const ok = ids.filter(Boolean).length;
      const failed = ids.length - ok;
      if (fn) {
        if (!ok) fn.textContent = "Upload failed — see the error above.";
        else if (failed) fn.textContent = `${ok} uploaded, ${failed} failed — transcribing…`;
        else fn.textContent = ids.length === 1
          ? `${videos[0].name} — transcribing…`
          : `${ok} videos uploaded — transcribing…`;
      }
      if (go) go.disabled = false;
    })
    .finally(() => {
      _ingestBusy = Math.max(0, _ingestBusy - 1);
      // Re-evaluate empty vs shell now that the in-flight gate dropped.
      renderJobsList();
    });
}
// Expose for the early empty-state script in index.html.
window.handleFiles = handleFiles;

function getPreCleanFlag() {
  // Pre-clean checkbox removed from Ingest — never denoise on upload/retranscribe
  // from this UI. Backend still accepts pre_clean if a client sends it.
  return false;
}

// Stable palette for SPEAKER_00…N (Host/Guest first, then extras).
const DEFAULT_SPEAKER_PALETTE = [
  "#FFD700", "#00E5FF", "#a3be8c", "#b48ead", "#d08770",
  "#88c0d0", "#bf616a", "#5e81ac", "#ebcb8b", "#c084fc",
];

function defaultSpeakerColor(speakerId, index) {
  const m = /SPEAKER_(\d+)/i.exec(String(speakerId || ""));
  const i = m ? parseInt(m[1], 10) : (Number.isFinite(index) ? index : 0);
  return DEFAULT_SPEAKER_PALETTE[Math.abs(i) % DEFAULT_SPEAKER_PALETTE.length];
}

function speakerLabel(speakerId) {
  if (speakerId === "SPEAKER_00") return "Host";
  if (speakerId === "SPEAKER_01") return "Guest";
  const m = /SPEAKER_(\d+)/i.exec(String(speakerId || ""));
  if (m) return `Speaker ${parseInt(m[1], 10) + 1}`;
  return speakerId || "Speaker";
}

function collectSpeakerColors() {
  const out = {};
  document.querySelectorAll("[data-speaker-color]").forEach((inp) => {
    const id = inp.dataset.speakerColor;
    if (id && inp.value) out[id] = inp.value;
  });
  // Legacy Host/Guest ids (kept in sync with SPEAKER_00/01).
  if ($("hostColor") && !out.SPEAKER_00) out.SPEAKER_00 = $("hostColor").value;
  if ($("guestColor") && !out.SPEAKER_01) out.SPEAKER_01 = $("guestColor").value;
  return out;
}

function colorForSpeaker(speakerId, index) {
  if (!speakerId) return defaultSpeakerColor("SPEAKER_00", index);
  const map = collectSpeakerColors();
  if (map[speakerId]) return map[speakerId];
  if (speakerId === "SPEAKER_00" && map.Host) return map.Host;
  if (speakerId === "SPEAKER_01" && map.Guest) return map.Guest;
  return defaultSpeakerColor(speakerId, index);
}

function syncSpeakerColorPickers(speakerIds) {
  const wrap = $("speakerColorPickers");
  if (!wrap) return;
  const ids = (speakerIds || []).filter(Boolean);
  // Always keep at least Host + Guest slots so branding works before Analyze.
  const ensured = ids.length ? ids.slice() : ["SPEAKER_00", "SPEAKER_01"];
  if (!ensured.includes("SPEAKER_00")) ensured.unshift("SPEAKER_00");
  if (!ensured.includes("SPEAKER_01") && ensured.length === 1) ensured.push("SPEAKER_01");
  const existing = collectSpeakerColors();
  wrap.innerHTML = "";
  ensured.forEach((id, i) => {
    const label = document.createElement("label");
    label.style.cssText = "display:flex;align-items:center;gap:6px;font-size:.84rem";
    const name = document.createElement("span");
    name.textContent = speakerLabel(id);
    const inp = document.createElement("input");
    inp.type = "color";
    inp.dataset.speakerColor = id;
    if (id === "SPEAKER_00") inp.id = "hostColor";
    if (id === "SPEAKER_01") inp.id = "guestColor";
    inp.value = existing[id] || defaultSpeakerColor(id, i);
    inp.style.cssText =
      "width:32px;height:28px;padding:0;border:1px solid #3b4252;border-radius:6px;background:transparent;cursor:pointer";
    inp.addEventListener("input", () => {
      const cards = document.querySelectorAll("#ingestSpeakerCards .speaker-card");
      cards.forEach((card) => {
        const sid = card.dataset.speakerId;
        const color = colorForSpeaker(sid);
        const pill = card.querySelector(".speaker-pill");
        if (pill) {
          pill.style.background = color + "33";
          pill.style.color = color;
          pill.style.borderColor = color + "88";
        }
      });
      if (currentWords && currentWords.some((w) => w.speaker) && phraseListEl) {
        renderPhraseList(currentWords);
      }
    });
    label.appendChild(name);
    label.appendChild(inp);
    wrap.appendChild(label);
  });
}

// ---- Helpers: collect style / audio ----
/** Caption look is canonical (font_name, primary_color, …). Timeline / AI packs
 *  may still send short aliases (font, primary). Merge so burns + seeds agree. */
function normalizeCaptionStyle(style) {
  if (!style || typeof style !== "object") return {};
  const out = Object.assign({}, style);
  const map = [
    ["font", "font_name"],
    ["size", "font_size"],
    ["primary", "primary_color"],
    ["highlight", "highlight_color"],
    ["accent", "accent_color"],
    ["outline", "outline_color"],
    ["group", "group_size"],
  ];
  for (const [short, long] of map) {
    if ((out[long] == null || out[long] === "") && out[short] != null && out[short] !== "") {
      out[long] = out[short];
    }
    if ((out[short] == null || out[short] === "") && out[long] != null && out[long] !== "") {
      out[short] = out[long];
    }
  }
  return out;
}

function styleHasCaptionFields(style) {
  if (!style || typeof style !== "object") return false;
  return !!(
    style.font_name || style.font || style.primary_color || style.primary ||
    style.font_size || style.size || style.highlight_color || style.highlight
  );
}

/** Current Caption look from the Branding panel (always the export source). */
function captionLookStyle() {
  return normalizeCaptionStyle(typeof getStyle === "function" ? getStyle() : {});
}

async function flushCaptionLookToJob() {
  if (!currentJobId || typeof saveDraftNow !== "function") return captionLookStyle();
  try { await saveDraftNow(); } catch { /* best-effort */ }
  return captionLookStyle();
}

function getStyle() {
  const speakerColorsEnabled = $("speakerColorsEnabled") && $("speakerColorsEnabled").checked;
  const bgMusicDuck = $("bgMusicDuck") ? $("bgMusicDuck").checked : false;
  const bgMusicIntensity = $("bgMusicDuckIntensity") ? $("bgMusicDuckIntensity").value : "medium";
  const duckRatioMap = { gentle: 4, medium: 8, aggressive: 16 };
  return {
    font_name:       $("font").value,
    font_size:       parseInt(sizeEl.value, 10),
    primary_color:   primaryEl.value,
    highlight_color: highlightEl.value,
    accent_color:    accentEl ? accentEl.value : "#FF6B35",
    outline_color:   outlineEl.value,
    outline_width:   parseInt(owEl.value, 10),
    shadow:          $("shadow").checked ? 1 : 0,
    position_y:      parseInt(posEl.value, 10),
    all_caps:        $("allCaps").checked,
    group_size:      parseInt(groupEl.value, 10),
    smooth_timings:  $("smoothTimings") ? $("smoothTimings").checked : true,
    punchword_emphasis: $("punchwordEmphasis") ? $("punchwordEmphasis").checked : true,
    quality_boost:   $("qualityBoost") ? $("qualityBoost").checked : false,
    headline_banner: $("headlineBanner") ? $("headlineBanner").value.trim() : "",
    speaker_colors: speakerColorsEnabled ? collectSpeakerColors() : {},
    reframe: {
      enabled:      $("reframeEnabled") ? $("reframeEnabled").checked : false,
      top_panel:    $("reframeTopSelect") ? $("reframeTopSelect").value : "active",
      bottom_panel: $("reframeBottomSelect") ? $("reframeBottomSelect").value : "full",
    },
    punch_zoom: {
      enabled: $("punchZoomEnabled") ? $("punchZoomEnabled").checked : false,
      intensity: $("punchZoomIntensity") ? $("punchZoomIntensity").value : "med",
    },
    tighten_silences: {
      enabled:    $("tightenEnabled") ? $("tightenEnabled").checked : false,
      max_gap:    $("tightenMaxGap") ? parseFloat($("tightenMaxGap").value) : 1.0,
      target_gap: $("tightenTargetGap") ? parseFloat($("tightenTargetGap").value) : 0.3,
      crossfade:  $("tightenCrossfade") ? $("tightenCrossfade").checked : false,
      preserved_gap_starts: (typeof _tPreservedCache !== "undefined") ? _tPreservedCache.slice() : [],
    },
    bg_music: {
      enabled:   !!window._bgMusicUploaded,
      volume_db: $("bgMusicVolume") ? parseInt($("bgMusicVolume").value, 10) : -12,
      duck:      bgMusicDuck,
      duck_ratio: duckRatioMap[bgMusicIntensity] || 8,
    },
  };
}

function _audioOffsetSec() {
  const el = $("audioOffset");
  return el ? parseFloat(el.value) : 0;
}

function getAudio() {
  const offset = _audioOffsetSec();
  if (activeAudioTab === "auphonic") {
    const loudnessVal = $("auphonicLoudness") ? $("auphonicLoudness").value : "-16";
    const wet = $("auphonicWetMix");
    const gain = $("auphonicGain");
    return {
      provider:            "auphonic",
      speech_isolation:    $("auphonicSpeechIsolation") ? $("auphonicSpeechIsolation").checked : false,
      adaptive_leveler:    $("auphonicAdaptiveLeveler") ? $("auphonicAdaptiveLeveler").checked : true,
      noise_hum_reduction: auphonicNoiseHumReduction,
      highpass:            auphonicHighpass,
      loudness_lufs:       loudnessVal !== "" ? parseInt(loudnessVal, 10) : null,
      // Post-process knobs (excluded from cache key — adjust freely):
      wet_mix:             wet ? parseInt(wet.value, 10) : 100,
      output_gain_db:      gain ? parseFloat(gain.value) : 0,
      offset_seconds:      offset,
    };
  }
  if (activeAudioTab === "elevenlabs") {
    const wet = $("elevenWetMix");
    const gain = $("elevenGain");
    return {
      provider:       "elevenlabs",
      wet_mix:        wet ? parseInt(wet.value, 10) : 100,
      output_gain_db: gain ? parseFloat(gain.value) : 0,
      post_filters: {
        voice_boost:   $("elevenVoiceBoost") ? $("elevenVoiceBoost").checked : false,
        loudness_norm: $("elevenLoudnessNorm") ? $("elevenLoudnessNorm").checked : true,
        voice_clarity: $("elevenVoiceClarity") ? $("elevenVoiceClarity").checked : false,
      },
      offset_seconds: offset,
    };
  }
  if (activeAudioTab === "dolby") {
    const lufsRaw = $("dolbyLoudness") ? $("dolbyLoudness").value : "-14";
    return {
      provider:             "dolby",
      content_type:         $("dolbyContentType") ? $("dolbyContentType").value : "social_media",
      speech_isolation:     $("dolbySpeechIsolation") ? parseInt($("dolbySpeechIsolation").value, 10) : 50,
      noise_reduction:      $("dolbyNoiseReduction") ? $("dolbyNoiseReduction").value : "medium",
      dynamics:             $("dolbyDynamics") ? $("dolbyDynamics").value : "low",
      loudness_target_lufs: lufsRaw !== "" ? parseInt(lufsRaw, 10) : null,
      offset_seconds:       offset,
    };
  }
  return {
    provider:        "ffmpeg",
    noise_reduction: $("noiseReduction").checked,
    voice_boost:     $("voiceBoost").checked,
    loudness_norm:   $("loudnessNorm").checked,
    voice_clarity:   $("voiceClarity").checked,
    offset_seconds:  offset,
  };
}

// ---- Preview Audio (uses server-side job file — no re-upload) ----
previewBtn.onclick = async () => {
  if (!currentJobId) return;
  const audio = getAudio();

  previewBtn.disabled = true;
  previewBtn.textContent = "Processing…";
  audioPreviewArea.classList.add("hidden");
  audioPreviewStatus.textContent = "";

  try {
    const res = await fetch("/preview-audio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: currentJobId, audio }),
    });
    if (!res.ok) {
      const err = await res.json();
      audioPreviewStatus.textContent = "Error: " + (err.error || "Unknown error");
      audioPreviewArea.classList.remove("hidden");
      return;
    }
    const blob = await res.blob();
    if (audioBlobUrl) { URL.revokeObjectURL(audioBlobUrl); }
    audioBlobUrl = URL.createObjectURL(blob);
    audioPlayer.src = audioBlobUrl;
    audioPreviewStatus.textContent = "First 30 seconds of enhanced audio";
    audioPreviewArea.classList.remove("hidden");
  } catch (e) {
    audioPreviewStatus.textContent = "Request failed: " + e.message;
    audioPreviewArea.classList.remove("hidden");
  } finally {
    previewBtn.disabled = false;
    previewBtn.innerHTML = "&#9654; Preview first 30 s";
  }
};

// ---- Emoji rules ----
function addEmojiRule(keyword = "", emoji = "") {
  const row = document.createElement("div");
  row.className = "emoji-rule-row";

  const kwInput = document.createElement("input");
  kwInput.type = "text";
  kwInput.className = "emoji-input keyword";
  kwInput.placeholder = "keyword (e.g. fire)";
  kwInput.value = keyword;

  const emojiInput = document.createElement("input");
  emojiInput.type = "text";
  emojiInput.className = "emoji-input emoji-field";
  emojiInput.placeholder = "emoji";
  emojiInput.value = emoji;

  const removeBtn = document.createElement("button");
  removeBtn.className = "emoji-remove-btn";
  removeBtn.title = "Remove rule";
  removeBtn.textContent = "×";
  removeBtn.onclick = () => {
    pushEditHistory();
    row.remove();
    scheduleDraftSave();
  };

  row.appendChild(kwInput);
  row.appendChild(emojiInput);
  row.appendChild(removeBtn);
  emojiRulesList.appendChild(row);

  bindEditHistoryArming(kwInput);
  bindEditHistoryArming(emojiInput);
  kwInput.addEventListener("input", scheduleDraftSave);
  emojiInput.addEventListener("input", scheduleDraftSave);
  kwInput.focus();
}

function getEmojiRules() {
  const rules = {};
  emojiRulesList.querySelectorAll(".emoji-rule-row").forEach(row => {
    const kw = row.querySelector(".keyword").value.trim().toLowerCase();
    const em = row.querySelector(".emoji-field").value.trim();
    if (kw && em) rules[kw] = em;
  });
  return rules;
}

function applyStyle(style = {}) {
  style = normalizeCaptionStyle(style);
  if (!style || typeof style !== "object") return;
  if (style.font_name) $("font").value = style.font_name;
  if (style.font_size) sizeEl.value = style.font_size;
  if (style.primary_color) primaryEl.value = style.primary_color;
  if (style.highlight_color) highlightEl.value = style.highlight_color;
  if (style.accent_color && accentEl) accentEl.value = style.accent_color;
  if (style.outline_color) outlineEl.value = style.outline_color;
  if (style.outline_width !== undefined) owEl.value = style.outline_width;
  if (style.position_y !== undefined) posEl.value = style.position_y;
  if (style.all_caps !== undefined) $("allCaps").checked = !!style.all_caps;
  if (style.shadow !== undefined) $("shadow").checked = !!style.shadow;
  if (style.group_size) groupEl.value = style.group_size;
  if (style.smooth_timings !== undefined && $("smoothTimings")) {
    $("smoothTimings").checked = !!style.smooth_timings;
  }
  if (style.punchword_emphasis !== undefined && $("punchwordEmphasis")) {
    $("punchwordEmphasis").checked = !!style.punchword_emphasis;
  }
  if (style.reframe && $("reframeEnabled")) {
    // Only honor a saved enabled=true if the analysis cache still exists —
    // refreshReframeStatus() may flip it off if /reframe-status returns
    // not-ready. Until then, restore the user's previous choice.
    $("reframeEnabled").checked = !!style.reframe.enabled;
  }
  if (style.quality_boost !== undefined && $("qualityBoost")) {
    $("qualityBoost").checked = !!style.quality_boost;
  }
  if (style.tighten_silences && $("tightenEnabled")) {
    const ts = style.tighten_silences;
    $("tightenEnabled").checked = !!ts.enabled;
    if ($("tightenMaxGap") && ts.max_gap !== undefined) {
      $("tightenMaxGap").value = ts.max_gap;
      const lbl = $("tightenMaxGapVal");
      if (lbl) lbl.textContent = parseFloat(ts.max_gap).toFixed(1) + "s";
    }
    if ($("tightenTargetGap") && ts.target_gap !== undefined) {
      $("tightenTargetGap").value = ts.target_gap;
      const lbl = $("tightenTargetGapVal");
      if (lbl) lbl.textContent = parseFloat(ts.target_gap).toFixed(2) + "s";
    }
    const ctrls = $("tightenControls");
    if (ctrls) ctrls.classList.toggle("hidden", !ts.enabled);
    if (Array.isArray(ts.preserved_gap_starts)) {
      _tPreservedCache = ts.preserved_gap_starts.slice();
    }
    if ($("tightenCrossfade") && ts.crossfade !== undefined) {
      $("tightenCrossfade").checked = !!ts.crossfade;
    }
  }
  sizeVal.textContent = sizeEl.value;
  owVal.textContent = owEl.value;
  posVal.textContent = posEl.value + "%";
  groupVal.textContent = groupEl.value;
  document.querySelectorAll(".theme").forEach(x => x.classList.remove("active"));
}

function applyAudio(audio = {}) {
  if (!audio || typeof audio !== "object") return;
  let restoredTab = "ffmpeg";
  if (audio.provider === "auphonic" && auphonicTabContent) restoredTab = "auphonic";
  else if (audio.provider === "elevenlabs" && elevenlabsTabContent) restoredTab = "elevenlabs";
  else if (audio.provider === "dolby" && dolbyTabContent) restoredTab = "dolby";
  activeAudioTab = restoredTab;
  if (audioTabsEl) {
    audioTabsEl.querySelectorAll(".audio-tab").forEach(b => {
      b.classList.toggle("active", b.dataset.tab === activeAudioTab);
    });
    showAudioTabContent(activeAudioTab);
  }
  $("noiseReduction").checked = !!audio.noise_reduction;
  $("voiceBoost").checked = !!audio.voice_boost;
  $("loudnessNorm").checked = !!audio.loudness_norm;
  $("voiceClarity").checked = !!audio.voice_clarity;
  if ($("elevenWetMix") && audio.provider === "elevenlabs") {
    const wm = audio.wet_mix !== undefined ? audio.wet_mix : 100;
    $("elevenWetMix").value = wm;
    $("elevenWetMixVal").textContent = wm + "%";
    const gn = audio.output_gain_db !== undefined ? audio.output_gain_db : 0;
    $("elevenGain").value = gn;
    $("elevenGainVal").textContent = (gn > 0 ? "+" : "") + gn + " dB";
    const pf = audio.post_filters || {};
    $("elevenVoiceBoost").checked = !!pf.voice_boost;
    $("elevenLoudnessNorm").checked = pf.loudness_norm !== false;
    $("elevenVoiceClarity").checked = !!pf.voice_clarity;
  }
  if (audioOffsetEl && audio.offset_seconds !== undefined) {
    audioOffsetEl.value = audio.offset_seconds;
    _updateAudioOffsetLabel();
  }
  if ($("dolbyContentType") && audio.provider === "dolby") {
    if (audio.content_type) $("dolbyContentType").value = audio.content_type;
    if (audio.speech_isolation !== undefined) {
      $("dolbySpeechIsolation").value = audio.speech_isolation;
      $("dolbySpeechIsolationVal").textContent = audio.speech_isolation;
    }
    if (audio.noise_reduction) $("dolbyNoiseReduction").value = audio.noise_reduction;
    if (audio.dynamics) $("dolbyDynamics").value = audio.dynamics;
    $("dolbyLoudness").value = audio.loudness_target_lufs !== null && audio.loudness_target_lufs !== undefined
      ? String(audio.loudness_target_lufs) : "";
  }
  if ($("auphonicSpeechIsolation") && audio.provider === "auphonic") {
    $("auphonicSpeechIsolation").checked = !!audio.speech_isolation;
    $("auphonicAdaptiveLeveler").checked = audio.adaptive_leveler !== false;
    $("auphonicLoudness").value = audio.loudness_lufs !== null && audio.loudness_lufs !== undefined ? String(audio.loudness_lufs) : "";
    auphonicNoiseHumReduction = !!audio.noise_hum_reduction;
    auphonicHighpass = !!audio.highpass;
    if ($("auphonicWetMix")) {
      const wm = audio.wet_mix !== undefined ? audio.wet_mix : 100;
      $("auphonicWetMix").value = wm;
      $("auphonicWetMixVal").textContent = wm + "%";
    }
    if ($("auphonicGain")) {
      const gn = audio.output_gain_db !== undefined ? audio.output_gain_db : 0;
      $("auphonicGain").value = gn;
      $("auphonicGainVal").textContent = (gn > 0 ? "+" : "") + gn + " dB";
    }
  }
  updateAudioPreviewVisibility();
}

function applyEmojiRules(rules = {}) {
  if (!rules || typeof rules !== "object") return;
  emojiRulesList.innerHTML = "";
  Object.entries(rules).forEach(([keyword, emoji]) => addEmojiRule(keyword, emoji));
}

function _sanitizeWords(words) {
  // Strip any phantom gap-marker entries that may have leaked into a saved
  // draft from earlier builds (before .gap-row was excluded from
  // collectEditedWords). A real word has a non-empty word string and a
  // numeric start/end. Gap markers start with ✂ or ⏸.
  if (!Array.isArray(words)) return [];
  return words.filter(w => {
    if (!w || typeof w !== "object") return false;
    const text = String(w.word || "").trim();
    if (!text) return false;
    if (text.startsWith("✂") || text.startsWith("⏸")) return false;
    if (text.includes("Cutting silence") || text.includes("Pause kept")) return false;
    const s = parseFloat(w.start), e = parseFloat(w.end);
    if (!Number.isFinite(s) || !Number.isFinite(e)) return false;
    return true;
  });
}

function collectEditedWords(options = {}) {
  // Only iterate REAL transcript phrase rows. Gap-marker rows share the
  // .phrase-row class for layout but must NOT be saved as words.
  const rows = phraseListEl.querySelectorAll(".phrase-row:not(.gap-row)");
  const editedWords = [];
  rows.forEach(row => {
    const text = row.querySelector(".phrase-text").textContent.trim();
    if (!text) return;
    const start     = parseFloat(row.dataset.start);
    const end       = parseFloat(row.dataset.end);
    const origTimes = JSON.parse(row.dataset.words || "[]");
    const words     = text.split(/\s+/).filter(Boolean);
    if (!words.length) return;

    if (words.length === origTimes.length) {
      words.forEach((word, i) => {
        const entry = { word, start: origTimes[i].s, end: origTimes[i].e };
        if (origTimes[i].sp) entry.speaker = origTimes[i].sp;
        editedWords.push(entry);
      });
    } else {
      const duration = Math.max(0, end - start);
      const wordDur  = duration / words.length;
      // Preserve dominant speaker for the row when word count changes.
      const rowSp = row.dataset.speaker || null;
      words.forEach((word, i) => {
        const entry = {
          word,
          start: start + i * wordDur,
          end:   start + (i + 1) * wordDur,
        };
        if (rowSp) entry.speaker = rowSp;
        else if (origTimes[i] && origTimes[i].sp) entry.speaker = origTimes[i].sp;
        editedWords.push(entry);
      });
    }
  });

  if (!editedWords.length && !options.silent) {
    alert("No subtitle phrases to render — please keep at least one row.");
  }
  return editedWords;
}

function scheduleDraftSave() {
  if (!currentJobId) return;
  clearTimeout(draftSaveTimer);
  draftSaveTimer = setTimeout(saveDraftNow, 500);
}

async function saveDraftNow() {
  if (!currentJobId) return;
  const editedWords = collectEditedWords({ silent: true });
  if (editedWords.length) currentWords = editedWords;
  localStorage.setItem("subtitleBurner:lastJobId", currentJobId);
  try {
    await fetch("/save-draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: currentJobId,
        words: editedWords.length ? editedWords : currentWords,
        style: getStyle(),
        audio: getAudio(),
        emoji_rules: getEmojiRules(),
      }),
    });
  } catch (e) {
  }
}

// Populate preset chips
EMOJI_PRESETS.forEach(p => {
  const btn = document.createElement("button");
  btn.className = "emoji-preset";
  btn.textContent = `${p.emoji} ${p.keyword}`;
  btn.title = `Add rule: "${p.keyword}" → ${p.emoji}`;
  btn.onclick = () => {
    const existing = Array.from(emojiRulesList.querySelectorAll(".keyword"))
      .map(el => el.value.trim().toLowerCase());
    if (!existing.includes(p.keyword)) {
      pushEditHistory();
      addEmojiRule(p.keyword, p.emoji);
      scheduleDraftSave();
    }
  };
  emojiPresetsDiv.appendChild(btn);
});

addRuleBtn.onclick = () => {
  pushEditHistory();
  addEmojiRule();
};

// XHR rather than fetch purely for upload progress — fetch cannot report how
// much of a request body has been sent, and phone uploads are slow enough
// that the difference between "working" and "frozen" has to be visible.
function _uploadWithProgress(fd, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/transcribe-only");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch (err) {
        // Flask serves HTML for errors; surface the status, not a parse error.
        reject(new Error(`Server returned ${xhr.status} instead of JSON.`));
        return;
      }
      if (xhr.status >= 400 || (data && data.error)) {
        reject(new Error((data && data.error) || `Upload failed (${xhr.status}).`));
      } else {
        resolve(data);
      }
    };
    xhr.onerror = () => reject(new Error("Network error during upload — check the connection."));
    xhr.onabort = () => reject(new Error("Upload cancelled."));
    xhr.send(fd);
  });
}

// ---- Phase 1: Transcribe ----
async function uploadAndTranscribe(file, preClean, makeActive = false) {
  const fd = new FormData();
  fd.append("video", file);
  if (preClean) fd.append("pre_clean", "true");

  // Show the bar before the request starts. fetch() reports no upload
  // progress, so a phone video used to sit on a blank screen for the whole
  // transfer with nothing to show it was working.
  if (makeActive) {
    result.classList.add("hidden");
    editor.classList.add("hidden");
    progress.classList.remove("hidden");
    _progressPhase = "upload";
    // Upload phase uses 0–40% of the bar; transcription takes 40–100%.
    barFill.style.width = "3%";
    statusText.textContent = "Uploading " + (file.name || "video") + "…";
    if (typeof setActiveTab === "function") setActiveTab("ingest");
  }

  try {
    const job = await _uploadWithProgress(fd, (frac) => {
      if (!makeActive || _progressPhase !== "upload") return;
      const pct = 3 + Math.round(Math.max(0, Math.min(1, frac)) * 37); // 3→40
      barFill.style.width = pct + "%";
      statusText.textContent = frac >= 1
        ? "Upload complete — extracting audio & transcribing…"
        : `Uploading… ${Math.round(frac * 100)}%`;
    });

    addJobToList(job.job_id);
    if (makeActive) {
      currentJobId = job.job_id;
      currentFile = null; // consumed — don't re-upload on accidental Transcribe click
      _progressPhase = "transcribe";
      barFill.style.width = "42%";
      const mi = job.media_info || {};
      const bits = [
        mi.is_hevc ? "HEVC" : (mi.video_codec || null),
        mi.audio_codec ? `audio:${mi.audio_codec}` : (mi.has_audio === false ? "audio:none" : null),
        mi.size ? `${Math.round(mi.size / 1024)} KB on server` : null,
        mi.duration ? `${Number(mi.duration).toFixed(1)}s` : null,
      ].filter(Boolean);
      statusText.textContent = bits.length
        ? `Server received file (${bits.join(" · ")}) — extracting audio & transcribing…`
        : "Starting transcription…";
      if (mi.has_audio === false) {
        statusText.textContent += " Warning: no audio stream detected — if this fails, Re-drop the original.";
      }
      pollTranscription(job.job_id);
    }
    refreshJobsList();
    return job.job_id;
  } catch (e) {
    if (makeActive) {
      _progressPhase = null;
      showError("Upload failed: " + e.message);
      go.disabled = false;
    } else {
      console.error("Upload failed for", file.name, e);
      if (window.__studioError) {
        window.__studioError(`Upload failed for ${file.name}: ${e.message}`);
      }
    }
    return null;
  }
}

go.onclick = async () => {
  // No staged file: either a job is already in flight / ready / failed.
  // Never claim "already transcribed" for an error job — that dead-ends the flow.
  if (!currentFile) {
    const meta = currentJobId ? (jobsById[currentJobId] || {}) : null;
    const st = meta && meta.status;
    if (currentJobId && st === "error") {
      const redo = confirm(
        "This video failed transcription (it is NOT ready to edit).\n\n" +
        "OK = Retry Whisper on the file already on the server.\n" +
        "Cancel = close this, then use “Re-drop video” or drop the file again."
      );
      if (redo) {
        go.disabled = true;
        try {
          await startRetranscribe(currentJobId, { label: "Retrying transcription…" });
        } finally {
          go.disabled = false;
        }
      }
      return;
    }
    if (currentJobId && (st === "transcribing" || st === "queued" || st === "re-transcribing" || st === "extracting audio")) {
      alert("Still working on this video — wait for transcription to finish (or pick it under Your videos).");
      return;
    }
    if (currentJobId && (meta.has_words || st === "awaiting_edit" || st === "done" || st === "timeline_edit")) {
      const again = confirm(
        "This video is already transcribed.\n\n" +
        "OK = open it for editing.\n" +
        "Cancel = stay here (drop a new file to upload another, or use Re-transcribe in the editor)."
      );
      if (again) switchToJob(currentJobId, { force: true, tab: "ingest" });
      return;
    }
    alert("Drop a video first, then click Transcribe.");
    return;
  }
  go.disabled = true;
  _ingestBusy += 1;
  const emptyEl = document.getElementById("emptyState");
  const shellEl = document.getElementById("appShell");
  const headerEl = document.getElementById("appHeader");
  if (emptyEl) emptyEl.classList.add("hidden");
  if (shellEl) shellEl.classList.remove("hidden");
  if (headerEl) headerEl.classList.remove("hidden");
  try {
    await uploadAndTranscribe(currentFile, getPreCleanFlag(), true);
  } finally {
    _ingestBusy = Math.max(0, _ingestBusy - 1);
    renderJobsList();
  }
};

async function pollTranscription(jobId) {
  let s;
  try {
    const res = await fetch("/status/" + jobId);
    if (res.status === 404) {
      showError("Job is no longer available on the server. Please re-upload.");
      go.disabled = false;
      return;
    }
    s = await res.json();
  } catch (e) {
    showError("Connection error — retrying…");
    setTimeout(() => pollTranscription(jobId), 3000);
    return;
  }

  // If the user switched to another job while we were polling, drop this
  // poller — the new job's poller (or the periodic /jobs refresh) takes over.
  if (currentJobId && currentJobId !== jobId) return;

  _progressPhase = "transcribe";
  // Map server 0–100 onto the remaining bar (40–100) so we never jump backwards
  // into the upload band (which looked like 1%↔5% thrashing).
  const serverPct = Math.max(0, Math.min(100, Number(s.progress) || 0));
  const uiPct = 40 + Math.round(serverPct * 0.6);
  barFill.style.width = uiPct + "%";

  if (s.status === "awaiting_edit") {
    _progressPhase = null;
    barFill.style.width = "100%";
    statusText.textContent = "Transcription complete!";
    if (retryTranscribeBtn) retryTranscribeBtn.classList.add("hidden");
    if (redropVideoBtn) redropVideoBtn.classList.add("hidden");
    if (retryTranscribeHint) retryTranscribeHint.classList.add("hidden");
    if (fn) fn.textContent = "";   // clear the "…transcribing" upload label
    currentWords = _sanitizeWords(s.words);
    setTimeout(() => {
      if (currentJobId !== jobId) return;
      progress.classList.add("hidden");
      go.disabled = false;
      showEditor(currentWords, s);
    }, 600);
    return;
  }

  if (s.status === "error") {
    _progressPhase = null;
    if (typeof setActiveTab === "function") setActiveTab("ingest");
    showError("Transcription error: " + s.error, {
      allowRetry: true,
      jobId: jobId,
      mediaInfo: s.media_info || null,
    });
    go.disabled = false;
    return;
  }

  // Live status: "extracting audio" / "transcribing" with % so a 2‑min clip
  // never looks frozen at 30% for minutes.
  const label = capitalize(s.status || "transcribing");
  statusText.textContent = `${label}… ${serverPct}%`;
  if (retryTranscribeBtn) retryTranscribeBtn.classList.add("hidden");
  if (redropVideoBtn) redropVideoBtn.classList.add("hidden");
  if (retryTranscribeHint) retryTranscribeHint.classList.add("hidden");
  setTimeout(() => pollTranscription(jobId), 1500);
}

// ---- Phrase timeline / editable subtitle list ----
function renderPhraseList(words) {
  phraseListEl.innerHTML = "";
  _updateFillerBanner(words);
  if (!words || !words.length) return;

  // Pull the latest scan results so we can interleave gap markers inline.
  // Empty array if the user hasn't scanned yet — in that case the list
  // renders identically to before.
  const gaps = Array.isArray(_tLastGaps) ? _tLastGaps.slice() : [];
  let gapIdx = 0;

  const groupSize = parseInt(groupEl.value, 10) || 3;
  const fillerIdxs = _detectFillerIndices(words);
  // Re-key fillerIdxs from word-index → group-relative index per-group so the
  // inner render loop can mark them cheaply without recomputing the heuristic.

  const insertGapsUpTo = (nextPhraseStart) => {
    // Insert every gap whose start time falls before this phrase begins.
    // We do NOT skip intra-phrase gaps: the user wants to see all detected
    // pauses regardless of where they fall relative to phrase grouping.
    // A gap inside a phrase (between word 1 and word 2 of the same phrase)
    // appears just before the NEXT phrase starts — close enough to the
    // actual position for visual scanning.
    while (gapIdx < gaps.length && gaps[gapIdx].start < nextPhraseStart) {
      phraseListEl.appendChild(_buildInlineGapRow(gaps[gapIdx++]));
    }
  };

  for (let i = 0; i < words.length; i += groupSize) {
    const group = words.slice(i, i + groupSize);
    if (!group.length) continue;

    insertGapsUpTo(group[0].start);

    const row = document.createElement("div");
    row.className = "phrase-row";
    row.dataset.start = group[0].start;
    row.dataset.end   = group[group.length - 1].end;
    row.dataset.words = JSON.stringify(group.map(w => ({
      s: w.start, e: w.end, sp: w.speaker || null,
    })));

    // Dominant speaker for this phrase → left rail color (Host/Guest).
    const spCounts = {};
    group.forEach((w) => {
      if (w.speaker) spCounts[w.speaker] = (spCounts[w.speaker] || 0) + 1;
    });
    const dominantSp = Object.keys(spCounts).sort((a, b) => spCounts[b] - spCounts[a])[0] || "";
    if (dominantSp) {
      row.dataset.speaker = dominantSp;
      row.classList.add("has-speaker");
      const rail = colorForSpeaker(dominantSp);
      row.style.borderLeftColor = rail;
      row.style.boxShadow = `inset 3px 0 0 ${rail}`;
    }

    // Timestamp label
    const timeEl = document.createElement("span");
    timeEl.className = "phrase-time";
    timeEl.textContent = fmtTime(group[0].start);
    timeEl.title = "Seek to " + fmtTime(group[0].start)
      + (dominantSp ? ` · ${speakerLabel(dominantSp)}` : "");

    // Editable text — punchwords (long words / numbers / proper nouns)
    // are wrapped in a span tinted with the user's accent color so the
    // user can see at a glance which words will be auto-emphasised in the
    // burn. The wrap is purely visual; textContent strips it, so editing
    // and word-collection behave identically to plain text.
    const textEl = document.createElement("span");
    textEl.className = "phrase-text";
    textEl.contentEditable = "true";
    textEl.spellcheck = false;
    const punchEnabled = $("punchwordEmphasis") ? $("punchwordEmphasis").checked : true;
    const punchIdxs = punchEnabled ? _selectGroupPunchwordIndices(group) : new Set();
    const accentColor = accentEl ? accentEl.value : "#FF6B35";
    const escapeHtml = (s) => s.replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
    }[c]));
    textEl.innerHTML = group.map((w, j) => {
      const safe = escapeHtml(w.word || "");
      const absoluteIdx = i + j;
      const isFiller = fillerIdxs.has(absoluteIdx);
      if (isFiller) {
        return `<span class="filler-word" title="Filler word — click 'Remove all' above to strip">${safe}</span>`;
      }
      if (punchIdxs.has(j)) {
        return `<span class="punchword" style="color:${accentColor}">${safe}</span>`;
      }
      if (w.speaker) {
        const sc = colorForSpeaker(w.speaker);
        return `<span class="speaker-word" style="color:${sc}" title="${speakerLabel(w.speaker)}">${safe}</span>`;
      }
      return safe;
    }).join(" ");
    textEl.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); textEl.blur(); }
    });
    bindEditHistoryArming(textEl);
    textEl.addEventListener("input", scheduleDraftSave);

    // Delete row button
    const delBtn = document.createElement("button");
    delBtn.className = "phrase-del";
    delBtn.textContent = "×";
    delBtn.title = "Remove this phrase";
    delBtn.onclick = (e) => {
      e.stopPropagation();
      pushEditHistory();
      row.remove();
      updateRowCount();
      scheduleDraftSave();
    };

    // Row click → seek video; skip when clicking text (for editing) or delete button
    row.onclick = (e) => {
      if (textEl.contains(e.target) || delBtn.contains(e.target)) return;
      sourcePlayer.currentTime = parseFloat(row.dataset.start);
      if (sourcePlayer.paused) sourcePlayer.play();
      highlightPhraseRow(row);
    };

    row.appendChild(timeEl);
    row.appendChild(textEl);
    row.appendChild(delBtn);

    phraseListEl.appendChild(row);
  }

  // Trailing gaps that fall after the last phrase (rare but possible).
  while (gapIdx < gaps.length) {
    phraseListEl.appendChild(_buildInlineGapRow(gaps[gapIdx++]));
  }
}

// One-click filler cleanup: drop every flagged word from currentWords,
// re-render, and persist. Optionally apply AI Trim (real keep-range cuts)
// via the timeline — Captions AI Trim parity.
const fillerCleanBtn = $("fillerCleanBtn");
if (fillerCleanBtn) {
  fillerCleanBtn.onclick = async () => {
    if (!currentWords || !currentWords.length) return;
    const flagged = _detectFillerIndices(currentWords);
    if (flagged.size === 0) return;
    const cutVideo = confirm(
      `Remove ${flagged.size} filler word${flagged.size === 1 ? "" : "s"}?\n\n` +
      `OK = remove from captions AND open AI Edit with AI Trim (cuts audio/video).\n` +
      `Cancel = captions only (audio unchanged).`
    );
    pushEditHistory();
    // Always strip from captions first.
    const cutRanges = [];
    flagged.forEach((i) => {
      const w = currentWords[i];
      if (!w) return;
      cutRanges.push([Number(w.start), Number(w.end)]);
    });
    currentWords = currentWords.filter((_, i) => !flagged.has(i));
    renderPhraseList(currentWords);
    updateRowCount();
    if (typeof scheduleDraftSave === "function") scheduleDraftSave();
    if (cutVideo && currentJobId && typeof window.openAiEditPlanForJob === "function") {
      window.openAiEditPlanForJob(currentJobId, {
        label: (jobsById[currentJobId] && jobsById[currentJobId].filename) || "AI Trim",
      });
    }
  };
}

function _buildInlineGapRow(g) {
  const row = document.createElement("div");
  row.className = "phrase-row gap-row" + (g.preserved ? " preserved" : "");

  const timeEl = document.createElement("span");
  timeEl.className = "phrase-time gap-time";
  timeEl.textContent = `${g.duration.toFixed(1)}s`;
  timeEl.title = `Gap at ${fmtTime(g.start)} — click to listen`;

  const textEl = document.createElement("span");
  textEl.className = "phrase-text gap-label";
  const _gapRange = `${fmtTime(g.start)} → ${fmtTime(g.end)}`;
  const tasteBit = g.taste_sentiment
    ? ` · ${g.taste_sentiment}`
    : "";
  textEl.textContent = g.preserved
    ? `⏸ Pause kept${tasteBit} (${_gapRange})`
    : `✂ Cutting silence (${_gapRange})`;
  if (g.taste_reason) {
    textEl.title = g.taste_reason;
  }

  const label = document.createElement("label");
  label.className = "gap-toggle";
  label.title = g.taste_reason
    ? `Taste: ${g.taste_reason}`
    : "Tick to PRESERVE this pause";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = !!g.preserved;
  cb.dataset.gapStart = g.start.toFixed(1);
  cb.onclick = (e) => e.stopPropagation();
  cb.onchange = () => {
    const set = _tGetPreservedSet();
    const key = Math.round(parseFloat(cb.dataset.gapStart) * 10) / 10;
    if (cb.checked) set.add(key); else set.delete(key);
    _tCommitPreservedSet(set);
    g.preserved = cb.checked;
    row.classList.toggle("preserved", cb.checked);
    textEl.textContent = cb.checked
      ? `⏸ Pause kept${tasteBit} (${_gapRange})`
      : `✂ Cutting silence (${_gapRange})`;
    // Refresh summary numbers from server.
    if (typeof _tFetchPreview === "function") _tFetchPreview(false);
  };
  label.appendChild(cb);
  const lblTxt = document.createElement("span");
  lblTxt.textContent = "Keep";
  label.appendChild(lblTxt);

  row.appendChild(timeEl);
  row.appendChild(textEl);
  row.appendChild(label);

  // Click on the row body (not the checkbox) → seek source player to the gap
  row.onclick = (e) => {
    if (e.target.closest("input,label")) return;
    if (sourcePlayer && sourcePlayer.src) {
      try {
        sourcePlayer.currentTime = Math.max(0, g.start - 0.5);
        sourcePlayer.play().catch(() => {});
      } catch (_) {}
    }
  };

  return row;
}

function updateRowCount() {
  // Count real phrases only — gap markers share .phrase-row for layout.
  const n = phraseListEl.querySelectorAll(".phrase-row:not(.gap-row)").length;
  rowCount.textContent = n + " phrase" + (n !== 1 ? "s" : "");
}

function highlightPhraseRow(target) {
  phraseListEl.querySelectorAll(".phrase-row").forEach(r => r.classList.remove("active"));
  if (target) target.classList.add("active");
}

// Sync phrase list highlight and live WYSIWYG overlay as the source video plays
sourcePlayer.addEventListener("timeupdate", () => {
  const t = sourcePlayer.currentTime;
  const rows = Array.from(phraseListEl.querySelectorAll(".phrase-row:not(.gap-row)"));
  let activeRow = null;
  for (let i = 0; i < rows.length; i++) {
    const start = parseFloat(rows[i].dataset.start);
    const nextStart = i < rows.length - 1 ? parseFloat(rows[i + 1].dataset.start) : Infinity;
    if (t >= start && t < nextStart) { activeRow = rows[i]; break; }
  }
  if (activeRow && !activeRow.classList.contains("active")) {
    highlightPhraseRow(activeRow);
    activeRow.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // Render Live WYSIWYG Subtitle Overlay on video stage
  const overlay = $("liveCaptionOverlay");
  if (overlay && currentWords && currentWords.length) {
    const currentWord = currentWords.find(w => t >= w.start && t <= w.end);
    if (currentWord) {
      const activeText = currentWord.word;
      const highlightColor = highlightEl ? highlightEl.value : "#FFD60A";
      const fontVal = $("font") ? $("font").value : "Outfit";
      overlay.style.fontFamily = `'${fontVal}', sans-serif`;
      overlay.innerHTML = `<span>${activeText.replace(/</g, "&lt;")}</span>`;
      overlay.style.display = "block";
      const span = overlay.querySelector("span");
      if (span) {
        span.className = "word-active";
        span.style.color = highlightColor;
      }
    } else {
      overlay.style.display = "none";
    }
  }
});

function fmtTime(sec) {
  const m = Math.floor(sec / 60);
  const s = (sec % 60).toFixed(1).padStart(4, "0");
  return `${m}:${s}`;
}

let _proxyWatchTimer = null;
/** Reload #sourcePlayer once the H.264 edit proxy is ready (HEVC → playable). */
function _watchEditProxy(jobId) {
  if (_proxyWatchTimer) {
    clearInterval(_proxyWatchTimer);
    _proxyWatchTimer = null;
  }
  if (!jobId || !sourcePlayer) return;
  let tries = 0;
  _proxyWatchTimer = setInterval(async () => {
    tries += 1;
    if (tries > 40 || currentJobId !== jobId) {
      clearInterval(_proxyWatchTimer);
      _proxyWatchTimer = null;
      return;
    }
    try {
      const res = await fetch("/status/" + jobId);
      if (!res.ok) return;
      const s = await res.json();
      if (s.edit_proxy) {
        const t = sourcePlayer.currentTime || 0;
        sourcePlayer.src = "/raw-upload/" + jobId + "?proxy=1&t=" + Date.now();
        sourcePlayer.addEventListener("loadedmetadata", () => {
          try { sourcePlayer.currentTime = t; } catch { /* ignore */ }
        }, { once: true });
        clearInterval(_proxyWatchTimer);
        _proxyWatchTimer = null;
      }
    } catch { /* ignore */ }
  }, 3000);
}

function showEditor(words, saved = {}) {
  // Never present an empty Transcript Cut as "ready" — that produced the
  // black player + "0 phrases" + "No active job" dead-end.
  if (!currentJobId || !Array.isArray(words) || !words.length) {
    if (editor) editor.classList.add("hidden");
    if (typeof setActiveTab === "function") setActiveTab("ingest");
    const meta = currentJobId ? (jobsById[currentJobId] || {}) : null;
    if (meta && meta.status === "error") {
      showError("Transcription error: " + (meta.error || "unknown"), {
        allowRetry: true,
        jobId: currentJobId,
        mediaInfo: meta.media_info || null,
      });
    } else {
      showError(
        "No transcript yet. Drop a video on Ingest and wait for Whisper to finish.",
        { allowRetry: false }
      );
    }
    return;
  }

  if (retranscribeBtn) retranscribeBtn.disabled = false;
  if (saved.style) applyStyle(saved.style);
  if (saved.audio) applyAudio(saved.audio);
  if (saved.emoji_rules) applyEmojiRules(saved.emoji_rules);

  // Reveal the Result tab if this job already has a finished render.
  if (saved && saved.output) {
    const resultBtn = document.getElementById("tabResult");
    if (resultBtn) resultBtn.classList.remove("hidden");
  }

  // Load the original uploaded video into the source player.
  // For HEVC iPhone MOVs Chrome-on-Windows is often black until the H.264
  // edit proxy lands — keep refreshing briefly until /raw-upload serves it.
  sourcePlayer.src = "/raw-upload/" + currentJobId + "?t=" + Date.now();
  if (typeof _watchEditProxy === "function") _watchEditProxy(currentJobId);

  // Populate the editable subtitle list
  currentWords = words;
  renderPhraseList(words);
  updateRowCount();
  clearEditHistory();

  // Show audio preview panel if any enhancement is enabled
  updateAudioPreviewVisibility();

  editor.classList.remove("hidden");
  localStorage.setItem("subtitleBurner:lastJobId", currentJobId);

  // After first transcription, stay on Ingest with ready CTAs (Phase 1 shell).
  // Edit words / Caption look / Shorts / Timeline are explicit next actions.
  const jobDur = _jobDurationFromWords(words);
  const isLongForm = jobDur >= 240;
  const longBadge = $("hlLongFormBadge");
  if (longBadge) longBadge.style.display = isLongForm ? "" : "none";

  const landOn = (saved && saved._landOn) || "ingest";
  // Legacy "transcript" / Edit Words landings redirect to Timeline.
  if (landOn === "transcript" || landOn === "editor") {
    if (typeof window.openTimelineEditor === "function" && currentJobId) {
      window.openTimelineEditor(currentJobId);
    } else {
      setActiveTab("editor");
    }
  } else {
    setActiveTab("ingest");
  }
  if (typeof updateAiEditNudge === "function") updateAiEditNudge(false);
  if (landOn !== "transcript" && landOn !== "editor" && typeof updateReadyActions === "function") {
    updateReadyActions();
    const ready = $("readyActions");
    if (ready) ready.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // Restore prior AI Shorts suggestions if the job already has them.
  if (Array.isArray(saved.clip_suggestions) && saved.clip_suggestions.length && typeof renderHighlights === "function") {
    renderHighlights(saved.clip_suggestions, saved.clip_format || "auto");
    if (hlStatus) {
      hlStatus.textContent = `${saved.clip_suggestions.length} short${saved.clip_suggestions.length === 1 ? "" : "s"} ready — Open in Timeline or Export clip.`;
    }
  }

  // Auto-Generate Shorts after upload removed from Ingest — use AI Shorts tab /
  // ready "Find Shorts" only.
}

function _jobDurationFromWords(words) {
  if (!words || !words.length) return 0;
  try {
    return Math.max(0, Number(words[words.length - 1].end) || 0);
  } catch {
    return 0;
  }
}

async function maybeAutoGenerateShorts(jobId, opts) {
  opts = opts || {};
  const cb = $("autoGenerateShorts");
  const force = !!opts.force;
  if (!force && (!cb || !cb.checked)) return;
  if (!jobId) return;
  if ($("hlGeminiDisabled")) {
    if (hlStatus) hlStatus.textContent = "Auto-Generate Shorts needs GEMINI_API_KEY.";
    return;
  }
  // Already have suggestions (restored from job).
  if (hlResults && hlResults.children.length) {
    if (hlStatus) {
      hlStatus.textContent = `⚡ ${hlResults.children.length} short${hlResults.children.length === 1 ? "" : "s"} ready — open AI Shorts when you want them.`;
    }
    if (opts.switchTab) setActiveTab("highlights");
    return;
  }

  if (hlStatus) hlStatus.textContent = "⚡ Auto-generating shorts…";
  if (hlFindBtn) hlFindBtn.disabled = true;
  try {
    const format = (hlFormatEl && hlFormatEl.value) || "auto";
    const num = (hlCountEl && parseInt(hlCountEl.value, 10)) || 5;
    const durations = Array.from(document.querySelectorAll(".hl-dur:checked"))
      .map((el) => parseInt(el.value, 10))
      .filter((n) => !isNaN(n));
    const res = await fetch("/auto-process-job", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: jobId,
        format,
        num_clips: num,
        target_durations: durations.length ? durations : [30, 60],
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || res.statusText);
    const clips = data.clips || [];
    renderHighlights(clips, format);
    if (hlStatus) {
      hlStatus.textContent = clips.length
        ? `⚡ Auto-generated ${clips.length} short${clips.length === 1 ? "" : "s"}. Open in Timeline or Export clip.`
        : "Auto-generate returned no clips — try Find highlights with different lengths.";
    }
    const badge = document.querySelector('.main-tab[data-tab="highlights"]');
    if (badge && clips.length) {
      badge.title = `${clips.length} shorts ready`;
    }
    if (opts.switchTab && clips.length) setActiveTab("highlights");
    try { await refreshReframeStatus(); } catch { /* optional */ }
  } catch (e) {
    if (hlStatus) hlStatus.textContent = "Auto-generate failed: " + e.message;
  } finally {
    if (hlFindBtn && !$("hlGeminiDisabled")) hlFindBtn.disabled = false;
  }
}

// ---- Phase 2: Render ----
// (legacy maybeAutoGenerateShorts replaced above; keep render wiring)
renderBtn.onclick = async () => {
  if (typeof window.runInstantExport === "function") {
    return window.runInstantExport({ forceJobRender: true });
  }
  const editedWords = collectEditedWords({ silent: true });
  const words = editedWords.length ? editedWords : (currentWords || []);
  if (!words.length) {
    alert("No subtitle phrases to render — upload and wait for transcript ready.");
    return;
  }

  const emojiRules = getEmojiRules();
  currentWords = words;
  saveDraftNow();

  result.classList.add("hidden");
  progress.classList.remove("hidden");
  renderBtn.disabled = true;
  barFill.style.width = "5%";
  statusText.textContent = "Sending to renderer…";

  try {
    const res = await fetch("/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: currentJobId,
        words,
        style: getStyle(),
        audio: getAudio(),
        emoji_rules: emojiRules,
      }),
    });
    const job = await res.json();
    if (job.error) throw new Error(job.error);
    pollRender(job.job_id);
  } catch (e) {
    showError("Render failed: " + e.message);
    renderBtn.disabled = false;
  }
};

async function pollRender(jobId) {
  let s;
  try {
    const res = await fetch("/status/" + jobId);
    if (res.status === 404) {
      // Only surface if this is still the active job.
      if (!currentJobId || currentJobId === jobId) {
        showError("Job is no longer available on the server. Please re-upload.");
        renderBtn.disabled = false;
      }
      return;
    }
    s = await res.json();
  } catch (e) {
    setTimeout(() => pollRender(jobId), 3000);
    return;
  }

  const isActive = !currentJobId || currentJobId === jobId;
  if (isActive) {
    barFill.style.width = (s.progress || 10) + "%";
    statusText.textContent = capitalize(s.status) + "… " + (s.progress != null ? (s.progress + "%") : "");
  }

  if (s.status === "done") {
    // Always wire download for the finished job — long renders often finish
    // after the user browsed away; still surface the file when they return
    // or when Instant Export left Ingest waiting.
    if (typeof window.showExportDone === "function") {
      window.showExportDone(s.output, { jobId, force: isActive });
    } else if (isActive) {
      barFill.style.width = "100%";
      progress.classList.add("hidden");
      result.classList.remove("hidden");
      player.src = "/preview/" + s.output;
      dl.href = "/download/" + s.output;
      renderBtn.disabled = false;
      result.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    if (renderBtn) renderBtn.disabled = false;
    return;
  }

  if (s.status === "error") {
    if (isActive) {
      showError("Render error: " + s.error);
      renderBtn.disabled = false;
    }
    return;
  }

  // Keep polling even if the user switched jobs — long (>2.5 min) encodes
  // must not be abandoned by the UI.
  setTimeout(() => pollRender(jobId), 2000);
}

// "Edit transcript again" — open Edit words panel
reEditBtn.onclick = () => {
  result.classList.add("hidden");
  if (typeof openEditWords === "function") openEditWords();
  else {
    setActiveTab("transcript");
    editor.scrollIntoView({ behavior: "smooth", block: "start" });
  }
};

// "Re-transcribe" — discard current words and re-run Whisper on the video.
// Useful when timestamps have drifted from the actual frames (typically when
// the job's video file is itself the output of a previous render/clip op).
if (retranscribeBtn) {
  retranscribeBtn.onclick = async () => {
    if (!currentJobId) return;
    const ok = confirm(
      "Re-transcribe this video? Your current transcript edits will be discarded and replaced with a fresh Whisper pass."
    );
    if (!ok) return;
    retranscribeBtn.disabled = true;
    const okRun = await startRetranscribe(currentJobId);
    if (!okRun) retranscribeBtn.disabled = false;
  };
}

// Caption export — backend builds the SRT/VTT from the latest stored words
// in jobs.db. If the user has uncommitted transcript edits in the editor,
// flush them via saveDraftNow first so the export reflects what's on
// screen, not the last persisted state.
async function _downloadCaptions(ext) {
  if (!currentJobId) {
    alert("Open a transcribed video first.");
    return;
  }
  if (typeof saveDraftNow === "function") {
    try { await saveDraftNow(); } catch { /* best-effort */ }
  }
  const url = `/export-captions/${currentJobId}.${ext}`;
  // Use a hidden anchor click so the Content-Disposition header drives the
  // filename instead of forcing one we'd guess wrong.
  const a = document.createElement("a");
  a.href = url;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}
if (exportSrtBtn) exportSrtBtn.onclick = () => _downloadCaptions("srt");
if (exportVttBtn) exportVttBtn.onclick = () => _downloadCaptions("vtt");

// Interview-reframe analysis. POSTs to /analyze-reframe, then polls the
// dedicated /reframe-status endpoint until ready. Once ready, the
// "Apply 9:16 reframe on next render" checkbox enables.
const reframeAnalyzeBtn = $("reframeAnalyzeBtn");
const reframeSwapBtn    = $("reframeSwapBtn");
const reframeEnabled    = $("reframeEnabled");
const reframeStatus     = $("reframeStatus");
let _reframePollTimer = null;

function renderIngestSpeakerCards(stats) {
  const wrap = $("ingestSpeakerCards");
  if (!wrap) return;
  const breakdown = (stats && stats.speaker_breakdown) || [];
  if (!breakdown.length) {
    wrap.innerHTML =
      `<p id="ingestSpeakerEmpty" class="muted" style="font-size:.78rem;line-height:1.5;margin:0 0 8px;padding:10px;background:rgba(30,41,59,0.4);border-radius:8px;border:1px solid rgba(255,255,255,0.06)">` +
      `No speakers yet. Click <strong>Analyze</strong> (or Analyze on Transcript Cut) — live cards appear after diarization. Auto-Generate Shorts is Gemini-only and does not run speakers.` +
      `</p>`;
    return;
  }
  const ts = Date.now();
  syncSpeakerColorPickers(breakdown.map((s) => s.id));
  wrap.innerHTML = "";
  breakdown.forEach((spk, i) => {
    const color = colorForSpeaker(spk.id, i);
    const card = document.createElement("div");
    card.className = "speaker-card";
    card.dataset.speakerId = spk.id;
    const avatar = document.createElement("img");
    avatar.className = "speaker-avatar";
    avatar.alt = spk.label || spk.id;
    avatar.src = currentJobId
      ? `/reframe-speaker-avatar/${currentJobId}/${encodeURIComponent(spk.id)}?t=${ts}`
      : "";
    avatar.onerror = () => { avatar.style.display = "none"; };

    const meta = document.createElement("div");
    meta.style.cssText = "flex:1;min-width:0";
    meta.innerHTML =
      `<div style="font-weight:600;font-size:0.85rem">${spk.label || speakerLabel(spk.id)}</div>` +
      `<div style="font-size:0.74rem;color:#94a3b8">${spk.speech_pct}% speech · ${spk.speech_sec}s · ${spk.id}</div>`;

    const pill = document.createElement("span");
    pill.className = "speaker-pill";
    pill.textContent = spk.label || speakerLabel(spk.id);
    pill.style.cssText =
      `background:${color}33;color:${color};border:1px solid ${color}88`;

    card.appendChild(avatar);
    card.appendChild(meta);
    card.appendChild(pill);
    wrap.appendChild(card);
  });
}

async function refreshReframeStatus() {
  if (!currentJobId) return;
  try {
    const res = await fetch(`/reframe-status/${currentJobId}`);
    const data = await res.json();
    if (data.ready && data.stats) {
      if (reframeStatus) {
        const faceN = data.stats.face_samples || 0;
        const faceNote = data.stats.faces_skipped
          ? " · faces skipped (speakers still OK)"
          : `, ${faceN} face samples`;
        reframeStatus.textContent =
          `✓ ${data.stats.speaker_count} speakers${faceNote}`;
        reframeStatus.style.color = "";
      }
      if (reframeEnabled) {
        reframeEnabled.disabled = false;
        reframeEnabled.checked = true; // Auto-check when analysis is ready!
      }
      if (reframeAnalyzeBtn) reframeAnalyzeBtn.textContent = "Re-analyze";
      refreshReframePreview();
      refreshSpeakerAvatars();
      renderIngestSpeakerCards(data.stats);
      const ingestBtn = $("ingestAnalyzeBtn");
      if (ingestBtn) ingestBtn.textContent = "Re-analyze";
      // Stamp word.speaker + refresh Transcript Cut coloring.
      try {
        await fetch(`/stamp-speakers/${currentJobId}`, { method: "POST" });
        const st = await fetch(`/status/${currentJobId}`).then((r) => r.json());
        if (Array.isArray(st.words) && st.words.length) {
          currentWords = _sanitizeWords(st.words);
          if (phraseListEl && !phraseListEl.closest(".hidden")) {
            renderPhraseList(currentWords);
            updateRowCount();
          }
        }
      } catch { /* optional */ }
    } else {
      if (reframeStatus) reframeStatus.textContent = "Not analysed yet";
      if (reframeEnabled) {
        reframeEnabled.disabled = true;
        reframeEnabled.checked = false;
      }
      if (reframeAnalyzeBtn) reframeAnalyzeBtn.textContent = "Analyze speakers + faces";
      const box = $("reframePreviewBox");
      if (box) box.style.display = "none";
      const spkBox = $("reframeSpeakerBox");
      if (spkBox) spkBox.style.display = "none";
      renderIngestSpeakerCards(null);
      const ingestBtn = $("ingestAnalyzeBtn");
      if (ingestBtn) ingestBtn.textContent = "Analyze";
    }
  } catch { /* offline — silent */ }
}

async function startReframeAnalyze(triggerBtn, opts) {
  opts = opts || {};
  const jobId = opts.jobId || currentJobId;
  const onStatus = typeof opts.onStatus === "function" ? opts.onStatus : null;
  if (!jobId) {
    alert("Select a Main clip in Timeline (or a transcribed video), then click Analyze speakers.");
    return;
  }
  // Prefer the Timeline-selected source when Analyze is launched from Timeline.
  if (opts.jobId) currentJobId = opts.jobId;
  if (triggerBtn) triggerBtn.disabled = true;
  if (reframeAnalyzeBtn) reframeAnalyzeBtn.disabled = true;
  const ingestBtn = $("ingestAnalyzeBtn");
  const tlBtn = $("tlAnalyzeBtn");
  if (ingestBtn) {
    ingestBtn.disabled = true;
    ingestBtn.textContent = "Analysing…";
  }
  if (tlBtn && tlBtn !== triggerBtn) {
    tlBtn.disabled = true;
    tlBtn.textContent = "Analysing…";
  }
  if (reframeAnalyzeBtn) reframeAnalyzeBtn.textContent = "Analysing…";
  if (reframeSwapBtn) reframeSwapBtn.style.display = "none";
  if (reframeStatus) {
    reframeStatus.style.color = "";
    reframeStatus.textContent = "Starting…";
  }
  if (onStatus) onStatus("Starting…");
  // Ensure the empty/status node exists even after cards were rendered.
  let empty = $("ingestSpeakerEmpty");
  const cardsWrap = $("ingestSpeakerCards");
  if (!empty && cardsWrap) {
    cardsWrap.innerHTML =
      `<p id="ingestSpeakerEmpty" class="muted" style="font-size:.78rem;line-height:1.5;margin:0 0 8px;padding:10px;background:rgba(30,41,59,0.4);border-radius:8px;border:1px solid rgba(255,255,255,0.06)"></p>`;
    empty = $("ingestSpeakerEmpty");
  }
  if (empty) empty.textContent = "Analysing speakers… (GPU if available; CPU uses multi-core on Linux)";

  const unlockAnalyzeBtns = () => {
    if (reframeAnalyzeBtn) reframeAnalyzeBtn.disabled = false;
    if (ingestBtn) ingestBtn.disabled = false;
    if (tlBtn) {
      tlBtn.disabled = false;
      if (!tlBtn.textContent || /analys/i.test(tlBtn.textContent)) tlBtn.textContent = "Analyze speakers";
    }
  };

  if (_reframePollTimer) {
    clearInterval(_reframePollTimer);
    _reframePollTimer = null;
  }

  try {
    const res = await fetch("/analyze-reframe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId }),
    });
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      throw new Error(
        res.status === 404
          ? "Analyze endpoint missing — restart the app after pulling."
          : `Server returned ${res.status} (not JSON). Restart the Studio server.`
      );
    }
    if (!res.ok || (data && data.error)) {
      let msg = (data && data.error) || `Analyze failed (${res.status})`;
      if (data && data.deps) {
        const d = data.deps;
        msg += `\n\nDeps in this process:\n` +
          `• HF_TOKEN: ${d.hf_token ? "present" : "MISSING"}\n` +
          `• pyannote: ${d.pyannote}\n` +
          `• mediapipe: ${d.mediapipe}` +
          (d.faces_ok === false ? "\n(Face crops optional — speakers should still work after update.)" : "");
      }
      throw new Error(msg);
    }
    const deviceHint = data.diarization_device ? ` · ${data.diarization_device}` : "";
    if (data.faces_note) {
      if (reframeStatus) reframeStatus.textContent = data.faces_note;
      if (empty) empty.textContent = data.faces_note;
      if (onStatus) onStatus(data.faces_note);
    }
    const startMsg = `Analysing speakers + faces${deviceHint}…`;
    if (reframeStatus) reframeStatus.textContent = startMsg;
    if (empty) empty.textContent = startMsg;
    if (onStatus) onStatus(startMsg);
    if (data.faces_note && empty) {
      empty.textContent = `Analysing speakers${deviceHint}… (${data.faces_note})`;
    }

    let pollFails = 0;
    _reframePollTimer = setInterval(async () => {
      try {
        const statusRes = await fetch(`/reframe-status/${jobId}`);
        let r = null;
        try {
          r = await statusRes.json();
        } catch (_) {
          pollFails += 1;
          if (pollFails >= 5) {
            clearInterval(_reframePollTimer);
            _reframePollTimer = null;
            unlockAnalyzeBtns();
            if (ingestBtn) ingestBtn.textContent = "Analyze";
            if (reframeAnalyzeBtn) reframeAnalyzeBtn.textContent = "Analyze speakers + faces";
            if (empty) empty.textContent = "Lost contact with server while analysing — try Analyze again.";
            if (onStatus) onStatus("Lost contact — try Analyze again.");
          }
          return;
        }
        pollFails = 0;
        if (r.ready) {
          clearInterval(_reframePollTimer);
          _reframePollTimer = null;
          await refreshReframeStatus();
          unlockAnalyzeBtns();
          if (tlBtn) tlBtn.textContent = "Re-analyze";
          if (reframeEnabled) {
            reframeEnabled.disabled = false;
            reframeEnabled.checked = true;
          }
          if (onStatus) {
            const n = (r.stats && r.stats.speaker_count) || "?";
            onStatus(`✓ ${n} speakers — colors apply in transcript`);
          }
          if (typeof window.onTimelineAnalyzeReady === "function") {
            try { window.onTimelineAnalyzeReady(jobId, r); } catch (_) { /* optional */ }
          }
        } else if (r.error) {
          clearInterval(_reframePollTimer);
          _reframePollTimer = null;
          if (reframeStatus) {
            reframeStatus.textContent = `❌ ${r.error}`;
            reframeStatus.style.color = "#ff8a8a";
          }
          if (empty) empty.textContent = "Analyze failed: " + r.error;
          if (onStatus) onStatus("❌ " + r.error);
          unlockAnalyzeBtns();
          if (ingestBtn) ingestBtn.textContent = "Analyze";
          if (reframeAnalyzeBtn) reframeAnalyzeBtn.textContent = "Analyze speakers + faces";
        } else if (r.status) {
          const pct = r.progress != null ? ` (${r.progress}%)` : "";
          const msg = `${r.status}${pct}`;
          if (reframeStatus) reframeStatus.textContent = msg;
          if (empty) empty.textContent = msg;
          if (onStatus) onStatus(msg);
        }
      } catch (pollErr) {
        pollFails += 1;
        if (pollFails >= 5) {
          clearInterval(_reframePollTimer);
          _reframePollTimer = null;
          unlockAnalyzeBtns();
          if (ingestBtn) ingestBtn.textContent = "Analyze";
          if (empty) empty.textContent = "Analyze poll failed: " + (pollErr.message || pollErr);
          if (onStatus) onStatus("Analyze poll failed: " + (pollErr.message || pollErr));
        }
      }
    }, 1500);
  } catch (e) {
    if (reframeStatus) {
      reframeStatus.textContent = "Error: " + e.message;
      reframeStatus.style.color = "#ff8a8a";
    }
    unlockAnalyzeBtns();
    if (ingestBtn) ingestBtn.textContent = "Analyze";
    if (reframeAnalyzeBtn) reframeAnalyzeBtn.textContent = "Analyze speakers + faces";
    if (empty) empty.textContent = "Analyze failed: " + e.message;
    if (onStatus) onStatus("Error: " + e.message);
    alert("Analyze failed:\n\n" + e.message);
  }
}

window.startReframeAnalyze = startReframeAnalyze;

if (reframeAnalyzeBtn) {
  reframeAnalyzeBtn.onclick = () => startReframeAnalyze(reframeAnalyzeBtn);
}
const ingestAnalyzeBtn = $("ingestAnalyzeBtn");
if (ingestAnalyzeBtn) {
  ingestAnalyzeBtn.onclick = () => startReframeAnalyze(ingestAnalyzeBtn);
}
// Backup: event delegation in case the Ingest button node is recreated.
document.addEventListener("click", (e) => {
  const btn = e.target && e.target.closest && e.target.closest("#ingestAnalyzeBtn");
  if (!btn) return;
  // Prefer the direct handler; this catches cases where onclick was wiped.
  if (btn.onclick) return;
  e.preventDefault();
  startReframeAnalyze(btn);
});

// Live-tint Ingest speaker pills + Transcript phrase colors when pickers change.
// (Dynamic pickers wire their own input handlers in syncSpeakerColorPickers.)
document.addEventListener("input", (e) => {
  const t = e.target;
  if (!t || !t.dataset || !t.dataset.speakerColor) return;
  if (t.id !== "hostColor" && t.id !== "guestColor") return;
  // Host/Guest legacy fields may still exist before first Analyze expand.
  const cards = document.querySelectorAll("#ingestSpeakerCards .speaker-card");
  cards.forEach((card) => {
    const sid = card.dataset.speakerId;
    const color = colorForSpeaker(sid);
    const pill = card.querySelector(".speaker-pill");
    if (pill) {
      pill.style.background = color + "33";
      pill.style.color = color;
      pill.style.borderColor = color + "88";
    }
  });
  if (currentWords && currentWords.some((w) => w.speaker) && phraseListEl) {
    renderPhraseList(currentWords);
  }
});

function refreshSpeakerAvatars() {
  const spkBox = $("reframeSpeakerBox");
  if (!currentJobId || !spkBox) return;
  const ts = Date.now();
  const spk0 = $("spk0Avatar");
  const spk1 = $("spk1Avatar");
  if (spk0) spk0.src = `/reframe-speaker-avatar/${currentJobId}/SPEAKER_00?t=${ts}`;
  if (spk1) spk1.src = `/reframe-speaker-avatar/${currentJobId}/SPEAKER_01?t=${ts}`;
  spkBox.style.display = "block";
}

async function handleSwapSpeakerVoices() {
  if (!currentJobId) return;
  const btn = $("reframeSwapDiarBtn");
  if (btn) { btn.disabled = true; btn.textContent = "Swapping…"; }
  try {
    const res = await fetch("/reframe-swap-speakers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: currentJobId }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    if (reframeStatus) reframeStatus.textContent = "✓ Speaker voices swapped";
    refreshSpeakerAvatars();
    refreshReframePreview();
    refreshReframeStatus();
  } catch (e) {
    if (reframeStatus) reframeStatus.textContent = "❌ Swap failed: " + e.message;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "⇄ Swap Speaker Voices"; }
  }
}

const reframeSwapDiarBtn = $("reframeSwapDiarBtn");
if (reframeSwapDiarBtn) reframeSwapDiarBtn.onclick = handleSwapSpeakerVoices;

function refreshReframePreview() {
  const box = $("reframePreviewBox");
  if (!currentJobId || !box) return;
  const topVal = $("reframeTopSelect") ? $("reframeTopSelect").value : "active";
  const botVal = $("reframeBottomSelect") ? $("reframeBottomSelect").value : "full";
  const ts = Date.now();
  const topImg = $("reframeTopImg");
  const botImg = $("reframeBottomImg");
  if (topImg) topImg.src = `/reframe-preview-crop/${currentJobId}/top?top=${topVal}&bottom=${botVal}&t=${ts}`;
  if (botImg) botImg.src = `/reframe-preview-crop/${currentJobId}/bottom?top=${topVal}&bottom=${botVal}&t=${ts}`;
  
  const labels = {
    active: "🗣️ Active Speaker (Auto Zoom)",
    left:   "👤 Speaker 1 (Left Person)",
    right:  "👤 Speaker 2 (Right Person)",
    full:   "📹 Original Wide Video",
  };
  const topDesc = $("reframeTopDesc");
  const botDesc = $("reframeBottomDesc");
  if (topDesc) topDesc.textContent = labels[topVal] || topVal;
  if (botDesc) botDesc.textContent = labels[botVal] || botVal;
  box.style.display = "block";
}

function handleReframeSwap() {
  const topEl = $("reframeTopSelect");
  const botEl = $("reframeBottomSelect");
  if (topEl && botEl) {
    const tmp = topEl.value;
    topEl.value = botEl.value;
    botEl.value = tmp;
    refreshReframePreview();
  }
}

const reframeSwapBtnCard = $("reframeSwapBtnCard");
if (reframeSwapBtnCard) reframeSwapBtnCard.onclick = handleReframeSwap;

const reframeTopSelect = $("reframeTopSelect");
if (reframeTopSelect) reframeTopSelect.onchange = () => refreshReframePreview();

const reframeBottomSelect = $("reframeBottomSelect");
if (reframeBottomSelect) reframeBottomSelect.onchange = () => refreshReframePreview();

// Refresh reframe panel state whenever a job is switched in / out.
if (typeof window !== "undefined") {
  window.addEventListener("subtitleBurner:jobChanged", refreshReframeStatus);
}

// ---- Utilities ----
function capitalize(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

function _mediaNeedsRedrop(mediaInfo, errMsg) {
  if (!mediaInfo && !errMsg) return false;
  const msg = String(errMsg || "").toLowerCase();
  if (
    msg.includes("incomplete") ||
    msg.includes("truncated") ||
    msg.includes("nearly empty") ||
    msg.includes("unreadable") ||
    msg.includes("could not extract audio") ||
    msg.includes("could not read audio") ||
    msg.includes("source video missing")
  ) {
    return true;
  }
  if (mediaInfo && mediaInfo.has_audio === false) {
    return true;
  }
  if (msg.includes("no audio")) return true;
  return false;
}

function showError(msg, opts) {
  opts = opts || {};
  progress.classList.remove("hidden");
  const readyBox = $("readyActions");
  if (readyBox) readyBox.classList.add("hidden");
  const media = opts.mediaInfo || null;
  let text = msg;
  if (media) {
    const kb = Math.round((media.size || 0) / 1024);
    const bits = [
      kb ? `${kb} KB on server` : null,
      media.has_audio ? "audio:yes" : "audio:no",
      media.has_video ? "video:yes" : "video:no",
      media.video_codec ? `v:${media.video_codec}` : null,
      media.audio_codec ? `a:${media.audio_codec}` : (media.has_audio === false ? "a:none" : null),
      media.is_hevc ? "HEVC" : null,
      media.duration ? `${Number(media.duration).toFixed(1)}s` : null,
    ].filter(Boolean);
    if (bits.length) text += `  [${bits.join(" · ")}]`;
    if (media.is_hevc && !media.has_audio) {
      text += " — HEVC iPhone files often play on Drive (re-encoded) but Windows needs codecs; Re-drop the full original if transfer was cut off.";
    }
  }
  statusText.textContent = text;
  barFill.style.width = "0%";
  const jobId = opts.jobId || currentJobId;
  const showRetry = !!opts.allowRetry && !!jobId;
  const needsRedrop = showRetry && _mediaNeedsRedrop(media, msg);
  if (retryTranscribeBtn) {
    // Prefer re-drop when server file has no audio — Retry will fail the same way.
    retryTranscribeBtn.classList.toggle("hidden", !showRetry || needsRedrop);
    retryTranscribeBtn.disabled = false;
    if (showRetry) retryTranscribeBtn.dataset.jobId = jobId;
  }
  if (redropVideoBtn) {
    redropVideoBtn.classList.toggle("hidden", !showRetry);
    redropVideoBtn.disabled = false;
    if (showRetry) redropVideoBtn.dataset.jobId = jobId;
    if (needsRedrop) {
      redropVideoBtn.classList.remove("btn-secondary");
      redropVideoBtn.classList.add("btn-primary");
      retryTranscribeBtn && retryTranscribeBtn.classList.add("hidden");
    } else {
      redropVideoBtn.classList.add("btn-secondary");
      redropVideoBtn.classList.remove("btn-primary");
    }
  }
  if (retryTranscribeHint) {
    retryTranscribeHint.classList.toggle("hidden", !showRetry);
    if (needsRedrop) {
      retryTranscribeHint.textContent =
        "The file on the server has no readable audio (often a cut-off iPhone .MOV upload). Use Re-drop video and wait until upload reaches 100%.";
    } else {
      retryTranscribeHint.textContent =
        "Retry re-runs Whisper on the file already uploaded. If it fails again with “no audio,” use Re-drop video.";
    }
  }
}

/** Re-run Whisper on a job already on the server (clears the error tag). */
async function startRetranscribe(jobId, opts) {
  opts = opts || {};
  if (!jobId) return false;
  try {
    const res = await fetch("/retranscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: jobId,
        pre_clean: typeof getPreCleanFlag === "function" ? getPreCleanFlag() : false,
      }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    currentJobId = jobId;
    localStorage.setItem("subtitleBurner:lastJobId", jobId);
    if (jobsById[jobId]) {
      jobsById[jobId].status = "re-transcribing";
      jobsById[jobId].error = null;
    }
    renderJobsList();
    editor.classList.add("hidden");
    result.classList.add("hidden");
    progress.classList.remove("hidden");
    if (retryTranscribeBtn) retryTranscribeBtn.classList.add("hidden");
    if (redropVideoBtn) redropVideoBtn.classList.add("hidden");
    if (retryTranscribeHint) retryTranscribeHint.classList.add("hidden");
    barFill.style.width = "30%";
    statusText.textContent = opts.label || "Re-transcribing…";
    pollTranscription(jobId);
    return true;
  } catch (e) {
    alert("Re-transcribe failed: " + e.message);
    return false;
  }
}

if (retryTranscribeBtn) {
  retryTranscribeBtn.onclick = async () => {
    const jobId = retryTranscribeBtn.dataset.jobId || currentJobId;
    if (!jobId) return;
    retryTranscribeBtn.disabled = true;
    await startRetranscribe(jobId, { label: "Retrying transcription…" });
    retryTranscribeBtn.disabled = false;
  };
}

/** Replace the broken server file on an error job, then re-run Whisper. */
async function replaceAndTranscribe(jobId, file) {
  if (!jobId || !file) return null;
  const fd = new FormData();
  fd.append("job_id", jobId);
  fd.append("video", file);
  if (getPreCleanFlag()) fd.append("pre_clean", "true");

  progress.classList.remove("hidden");
  if (retryTranscribeBtn) retryTranscribeBtn.classList.add("hidden");
  if (redropVideoBtn) redropVideoBtn.classList.add("hidden");
  if (retryTranscribeHint) retryTranscribeHint.classList.add("hidden");
  _progressPhase = "upload";
  barFill.style.width = "3%";
  statusText.textContent = "Re-uploading " + (file.name || "video") + "…";

  const job = await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/replace-and-transcribe");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable || _progressPhase !== "upload") return;
      const pct = 3 + Math.round(Math.max(0, Math.min(1, e.loaded / e.total)) * 37);
      barFill.style.width = pct + "%";
      statusText.textContent = e.loaded / e.total >= 1
        ? "Upload complete — starting transcription…"
        : `Re-uploading… ${Math.round((e.loaded / e.total) * 100)}%`;
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); }
      catch (err) { reject(new Error(`Server returned ${xhr.status}`)); return; }
      if (xhr.status >= 400 || (data && data.error)) {
        reject(new Error((data && data.error) || `Replace failed (${xhr.status})`));
      } else resolve(data);
    };
    xhr.onerror = () => reject(new Error("Network error during re-upload."));
    xhr.send(fd);
  });

  currentJobId = jobId;
  localStorage.setItem("subtitleBurner:lastJobId", jobId);
  if (jobsById[jobId]) {
    jobsById[jobId].status = "re-transcribing";
    jobsById[jobId].error = null;
    jobsById[jobId].filename = file.name;
  }
  renderJobsList();
  _progressPhase = "transcribe";
  barFill.style.width = "42%";
  statusText.textContent = "Starting transcription…";
  pollTranscription(jobId);
  return jobId;
}

if (redropVideoBtn) {
  redropVideoBtn.onclick = () => {
    const jobId = redropVideoBtn.dataset.jobId || currentJobId;
    if (!jobId) return;
    pickFileAndReplace(jobId);
  };
}

/** Open a file picker and replace the source on *jobId*, then re-transcribe. */
function pickFileAndReplace(jobId) {
  if (!jobId) return;
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ACCEPTED_VIDEO_EXT.map((e) => "." + e).join(",");
  input.onchange = async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    if (redropVideoBtn) redropVideoBtn.disabled = true;
    _ingestBusy += 1;
    if (typeof setActiveTab === "function") setActiveTab("ingest");
    try {
      await replaceAndTranscribe(jobId, file);
    } catch (e) {
      showError("Re-drop failed: " + e.message, {
        allowRetry: true,
        jobId: jobId,
      });
    } finally {
      if (redropVideoBtn) redropVideoBtn.disabled = false;
      _ingestBusy = Math.max(0, _ingestBusy - 1);
      renderJobsList();
    }
  };
  input.click();
}

// =====================================================================
// Multi-video sidebar
// =====================================================================
//
// State:
//   localStorage["subtitleBurner:jobIds"]      JSON array, newest first
//   localStorage["subtitleBurner:lastJobId"]   single id (kept for backward-compat)
//
// In-memory:
//   jobsById      { jobId -> last /jobs summary record } for sidebar rendering

const jobsPanel = $("jobsPanel");
const jobsListEl = $("jobsList");
const jobsCountEl = $("jobsCount");
let jobsById = {};

// Caption look helpers for Timeline + Instant Export (Phase 3 plumbing).
window.normalizeCaptionStyle = normalizeCaptionStyle;
window.styleHasCaptionFields = styleHasCaptionFields;
window.captionLookStyle = captionLookStyle;
window.applyStyle = applyStyle;
window.updateFontPreview = updateFontPreview;
window.flushCaptionLookToJob = flushCaptionLookToJob;
window.getStyle = getStyle;
window.getAudio = getAudio;
Object.defineProperty(window, "currentJobId", {
  get() { return currentJobId; },
  configurable: true,
});
Object.defineProperty(window, "jobsById", {
  get() { return jobsById; },
  configurable: true,
});

function _loadJobIds() {
  try {
    const raw = localStorage.getItem("subtitleBurner:jobIds");
    if (raw) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) return arr.filter(x => typeof x === "string");
    }
  } catch (e) {}
  // Fall back to legacy single-id key
  const legacy = localStorage.getItem("subtitleBurner:lastJobId");
  return legacy ? [legacy] : [];
}

function _saveJobIds(ids) {
  localStorage.setItem("subtitleBurner:jobIds", JSON.stringify(ids));
}

function addJobToList(jobId) {
  if (!jobId) return;
  const ids = _loadJobIds();
  if (!ids.includes(jobId)) {
    ids.unshift(jobId);
    _saveJobIds(ids);
  }
}

function removeJobFromList(jobId) {
  const ids = _loadJobIds().filter(x => x !== jobId);
  _saveJobIds(ids);
  delete jobsById[jobId];
  if (currentJobId === jobId) {
    currentJobId = null;
    localStorage.removeItem("subtitleBurner:lastJobId");
    editor.classList.add("hidden");
    result.classList.add("hidden");
  }
  renderJobsList();
}
window.removeJobFromList = removeJobFromList;
window.addJobToList = addJobToList;

function _statusBadgeClass(status) {
  if (!status) return "";
  if (status === "done") return "done";
  if (status === "error") return "error";
  if (status.includes("rendering") || status.includes("burn") || status.includes("remuxing")) return "rendering";
  if (status === "transcribing" || status === "queued") return status;
  return "transcribing";
}

function renderJobsList() {
  const ids = _loadJobIds();
  // Toggle empty-state vs app-shell here — single source of truth.
  // Keep the shell visible while an upload is in flight even before the
  // job id lands in localStorage (otherwise the 4s /jobs poll bounces the
  // user back to "Choose a video to start").
  const showShell = ids.length > 0 || _ingestBusy > 0;
  const emptyEl = document.getElementById("emptyState");
  const shellEl = document.getElementById("appShell");
  const headerEl = document.getElementById("appHeader");
  if (emptyEl) emptyEl.classList.toggle("hidden", showShell);
  if (shellEl) shellEl.classList.toggle("hidden", !showShell);
  // Hide sticky Studio header on welcome screen so it can't cover the CTA.
  if (headerEl) headerEl.classList.toggle("hidden", !showShell);
  if (!ids.length) {
    if (jobsPanel) jobsPanel.classList.add("hidden");
    return;
  }
  if (jobsPanel) jobsPanel.classList.remove("hidden");
  if (jobsCountEl) jobsCountEl.textContent = ids.length === 1 ? "1 video" : `${ids.length} videos`;
  if (!jobsListEl) return;
  jobsListEl.innerHTML = "";
  ids.forEach(jobId => {
    const meta = jobsById[jobId] || {};
    const div = document.createElement("div");
    div.className = "job-item" + (jobId === currentJobId ? " active" : "");
    div.dataset.jobId = jobId;

    const name = document.createElement("div");
    name.className = "job-name";
    name.textContent = meta.filename || jobId.slice(0, 8) + "…";
    div.appendChild(name);

    const status = document.createElement("span");
    status.className = "job-status " + _statusBadgeClass(meta.status);
    let label = meta.status || "loading";
    if (label === "awaiting_edit") label = "ready";
    status.textContent = label;
    div.appendChild(status);

    const rename = document.createElement("button");
    rename.className = "job-rename";
    rename.textContent = "✎";
    rename.title = "Rename";
    rename.onclick = async (e) => {
      e.stopPropagation();
      const current = meta.filename || "";
      const next = prompt("Rename this video:", current);
      if (next === null) return;
      const trimmed = next.trim();
      if (!trimmed || trimmed === current) return;
      try {
        const res = await fetch("/rename-job", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ job_id: jobId, filename: trimmed }),
        });
        const j = await res.json();
        if (j.error) throw new Error(j.error);
        if (jobsById[jobId]) jobsById[jobId].filename = trimmed;
        renderJobsList();
      } catch (err) {
        alert("Rename failed: " + err.message);
      }
    };
    div.appendChild(rename);

    if (meta.video_available) {
      const toTl = document.createElement("button");
      toTl.className = "job-rename job-to-timeline";
      toTl.textContent = "Open in Timeline";
      toTl.title = "Open in Timeline with this clip";
      toTl.onclick = (e) => {
        e.stopPropagation();
        // timeline.js loads after app.js, so guard at click time, not render.
        if (typeof window.openTimelineEditor === "function") window.openTimelineEditor(jobId);
        else alert("Editor is still loading — try again in a second.");
      };
      div.appendChild(toTl);
    }

    if (meta.has_words || meta.status === "awaiting_edit" || meta.status === "done") {
      const editWords = document.createElement("button");
      editWords.className = "job-rename job-edit-words";
      editWords.textContent = "Edit in Timeline";
      editWords.title = "Open Timeline transcript — phrases, fillers, speakers";
      editWords.onclick = (e) => {
        e.stopPropagation();
        if (typeof window.openTimelineEditor === "function") window.openTimelineEditor(jobId);
        else switchToJob(jobId, { force: true, tab: "editor" });
      };
      div.appendChild(editWords);
    }

    if (meta.status === "error") {
      const errTitle = meta.error || "Transcription failed";
      const needsDrop = _mediaNeedsRedrop(null, errTitle) || !meta.video_available;
      if (meta.video_available && !needsDrop) {
        const retry = document.createElement("button");
        retry.className = "job-rename job-retry";
        retry.textContent = "↻ Retry";
        retry.title = errTitle + " — re-run Whisper without re-uploading";
        retry.onclick = async (e) => {
          e.stopPropagation();
          retry.disabled = true;
          await startRetranscribe(jobId, { label: "Retrying transcription…" });
        };
        div.appendChild(retry);
      }
      const redrop = document.createElement("button");
      redrop.className = "job-rename job-redrop";
      redrop.textContent = "↑ Re-drop";
      redrop.title = needsDrop
        ? errTitle + " — replace the server file with a full re-upload"
        : "Replace this upload and re-transcribe";
      redrop.onclick = (e) => {
        e.stopPropagation();
        pickFileAndReplace(jobId);
      };
      div.appendChild(redrop);
    }

    const del = document.createElement("button");
    del.className = "job-delete";
    del.textContent = "✕";
    del.title = "Remove from list";
    del.onclick = (e) => {
      e.stopPropagation();
      if (confirm(`Remove "${meta.filename || jobId.slice(0, 8)}" from your list?`)) {
        removeJobFromList(jobId);
      }
    };
    div.appendChild(del);

    div.onclick = () => switchToJob(jobId);
    jobsListEl.appendChild(div);
  });
}

async function switchToJob(jobId, opts) {
  opts = opts || {};
  // Allow re-opening the same job (e.g. Compilation "Open job") so the UI
  // still navigates to Transcript even when currentJobId already matches.
  if (currentJobId === jobId && !opts.force) {
    if (opts.tab) setActiveTab(opts.tab);
    return;
  }
  if (saveDraftNow && currentJobId && currentJobId !== jobId) await saveDraftNow();
  try {
    const res = await fetch("/status/" + jobId);
    if (res.status === 404) {
      removeJobFromList(jobId);
      return;
    }
    if (!res.ok) return;
    const s = await res.json();
    currentJobId = jobId;
    localStorage.setItem("subtitleBurner:lastJobId", jobId);
    refreshPreviewCanvas(jobId);
    window.dispatchEvent(new CustomEvent("subtitleBurner:jobChanged"));
    if (s.words && s.words.length) {
      currentWords = _sanitizeWords(s.words);
      const landOn = opts.tab === "transcript" || opts.tab === "branding" || opts.tab === "highlights" || opts.tab === "editor"
        ? opts.tab
        : "ingest";
      // showEditor only understands ingest vs transcript for landing; other tabs set after.
      showEditor(currentWords, Object.assign({}, s, {
        _landOn: (landOn === "transcript") ? "transcript" : "ingest",
      }));
      if (s.output && s.status === "done") {
        result.classList.remove("hidden");
        player.src = "/preview/" + s.output;
        dl.href = "/download/" + s.output;
      } else {
        result.classList.add("hidden");
      }
      if (landOn !== "ingest" && landOn !== "transcript") {
        setActiveTab(landOn);
      }
    } else if (s.status === "error") {
      editor.classList.add("hidden");
      result.classList.add("hidden");
      if (typeof setActiveTab === "function") setActiveTab(opts.tab || "ingest");
      showError(
        "Transcription error: " + (s.error || "unknown"),
        {
          // Always allow recovery UI — Re-drop works even if the prior file is gone.
          allowRetry: true,
          jobId: jobId,
          mediaInfo: s.media_info || null,
        }
      );
    } else {
      // Still transcribing — show the progress UI for this job
      editor.classList.add("hidden");
      result.classList.add("hidden");
      progress.classList.remove("hidden");
      if (retryTranscribeBtn) retryTranscribeBtn.classList.add("hidden");
      if (redropVideoBtn) redropVideoBtn.classList.add("hidden");
      if (retryTranscribeHint) retryTranscribeHint.classList.add("hidden");
      barFill.style.width = (s.progress || 10) + "%";
      statusText.textContent = capitalize(s.status || "loading") + "…";
      pollTranscription(jobId);
      setActiveTab(opts.tab || "ingest");
    }
    renderJobsList();
    if (typeof updateReadyActions === "function") updateReadyActions();
  } catch (e) {
    console.error("switchToJob failed", e);
  }
}

async function refreshJobsList() {
  try {
    const res = await fetch("/jobs");
    if (!res.ok) return;
    const data = await res.json();
    const validIds = new Set();
    (data.jobs || []).forEach(j => {
      jobsById[j.job_id] = j;
      validIds.add(j.job_id);
    });
    // Drop any localStorage IDs the server no longer knows about.
    const ids = _loadJobIds();
    const filtered = ids.filter(id => validIds.has(id));
    if (filtered.length !== ids.length) {
      _saveJobIds(filtered);
      if (currentJobId && !validIds.has(currentJobId)) {
        currentJobId = null;
        localStorage.removeItem("subtitleBurner:lastJobId");
      }
    }
    renderJobsList();
  } catch (e) {
    console.error("refreshJobsList failed", e);
  }
}

async function initJobs() {
  await refreshJobsList();
  // Auto-restore the most recently active job. Error jobs open Ingest with
  // Retry / Re-drop — skipping them left users staring at a red badge only.
  const lastId = localStorage.getItem("subtitleBurner:lastJobId");
  if (lastId && jobsById[lastId]) {
    const meta = jobsById[lastId];
    if (meta.status === "error") {
      await switchToJob(lastId, { force: true, tab: "ingest" });
    } else if (meta.status === "done" || meta.video_available !== false) {
      await switchToJob(lastId);
    }
  }
  // Poll for status updates of in-progress jobs every 4 seconds.
  setInterval(refreshJobsList, 4000);
}

initJobs();

// =====================================================================
// AI Highlights — Gemini-driven clip suggestions
// =====================================================================

const hlFindBtn = $("hlFindBtn");
const hlResults = $("hlResults");
const hlStatus = $("hlStatus");
const hlFormatEl = $("hlFormat");
const hlDurationEl = $("hlDuration");
const hlCountEl = $("hlCount");

function _fmtTime(t) {
  // mm:ss (no decimals) — used for status displays and queue rows
  const tt = Math.max(0, Math.floor(t || 0));
  const mm = Math.floor(tt / 60).toString().padStart(2, "0");
  const ss = (tt % 60).toString().padStart(2, "0");
  return `${mm}:${ss}`;
}

function _fmtTimeFine(t) {
  // m:ss.s — used for editable time inputs where 0.1s precision matters
  const sign = (t || 0) < 0 ? "-" : "";
  let v = Math.abs(t || 0);
  const m = Math.floor(v / 60);
  const s = v - m * 60;
  return `${sign}${m}:${s.toFixed(1).padStart(4, "0")}`;
}

function _parseTime(str) {
  // Accept either "m:ss.s" / "m:ss" or plain seconds. Return null if invalid.
  if (str === null || str === undefined) return null;
  const s = String(str).trim();
  if (!s) return null;
  if (s.includes(":")) {
    const parts = s.split(":");
    if (parts.length !== 2) return null;
    const m = parseFloat(parts[0]);
    const sec = parseFloat(parts[1]);
    if (isNaN(m) || isNaN(sec) || sec < 0) return null;
    return m * 60 + sec;
  }
  const v = parseFloat(s);
  return isNaN(v) || v < 0 ? null : v;
}

// Tint palette for clips that overlap each other (≥90% by shorter clip).
// Singleton clips (group_id null) keep the default card background.
const HL_GROUP_TINTS = [
  "rgba(108, 92, 255, 0.16)",   // purple
  "rgba(255, 149, 85, 0.16)",   // orange
  "rgba(124, 217, 138, 0.14)",  // green
  "rgba(255, 215, 102, 0.14)",  // gold
  "rgba(102, 204, 220, 0.14)",  // cyan
  "rgba(255, 130, 180, 0.14)",  // pink
];

function renderHighlights(clips, format) {
  hlResults.innerHTML = "";
  if (!clips.length) {
    hlStatus.textContent = "No suggestions returned. Try a different format or longer transcript.";
    return;
  }
  // Count groups that have ≥2 members for a friendlier summary.
  const groupSet = new Set(clips.filter(c => c.group_id !== null && c.group_id !== undefined).map(c => c.group_id));
  const overlapNote = groupSet.size > 0
    ? ` · ${groupSet.size} overlap group${groupSet.size === 1 ? "" : "s"}`
    : "";
  hlStatus.textContent = `${clips.length} suggestion${clips.length === 1 ? "" : "s"} (${format})${overlapNote} · earliest first`;

  clips.forEach((c, idx) => {
    const card = document.createElement("div");
    card.className = "hl-card";
    if (c.group_id !== null && c.group_id !== undefined) {
      card.style.background = HL_GROUP_TINTS[c.group_id % HL_GROUP_TINTS.length];
      card.dataset.groupId = c.group_id;
    }

    // ---- Mutable per-card state so the user can nudge boundaries ----
    let editedStart = c.start_time;
    let editedEnd = c.end_time;
    let editedTitle = c.title || "Untitled clip";
    let editedQuote = c.hook_quote || "";
    let previewStopHandler = null;

    const title = document.createElement("div");
    title.className = "hl-title";
    // Index prefix sits outside the editable element so renumbering on
    // re-renders doesn't get baked into the user's edit.
    const indexLabel = document.createElement("span");
    indexLabel.textContent = `${idx + 1}. `;
    indexLabel.style.opacity = "0.7";
    title.appendChild(indexLabel);
    const titleText = document.createElement("strong");
    titleText.textContent = editedTitle;
    titleText.contentEditable = "true";
    titleText.spellcheck = false;
    titleText.title = "Click to rename";
    titleText.style.cursor = "text";
    titleText.style.outline = "none";
    titleText.style.borderBottom = "1px dashed rgba(255,255,255,0.18)";
    titleText.addEventListener("input", () => {
      editedTitle = titleText.textContent.trim() || "Untitled clip";
    });
    titleText.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); titleText.blur(); }
    });
    title.appendChild(titleText);
    const dur = document.createElement("span");
    dur.className = "hl-time";
    const updateDurLabel = () =>
      dur.textContent = `${(editedEnd - editedStart).toFixed(1)}s`;
    updateDurLabel();
    title.appendChild(dur);
    if (c.group_id !== null && c.group_id !== undefined) {
      const badge = document.createElement("span");
      badge.className = "hl-overlap-badge";
      badge.style.background = HL_GROUP_TINTS[c.group_id % HL_GROUP_TINTS.length];
      badge.textContent = `Overlap #${c.group_id + 1}`;
      badge.title = "This clip shares ≥90% of its content with another clip flagged with the same color/number.";
      title.appendChild(badge);
    }
    card.appendChild(title);

    // ---- Viral Score Badge + Category Tag (from enhanced Gemini response) ----
    if (c.viral_score || c.category || c.suggested_headline) {
      const metaRow = document.createElement("div");
      metaRow.style.cssText = "display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0 2px";
      if (c.viral_score != null) {
        const scoreBadge = document.createElement("span");
        const score = parseInt(c.viral_score, 10);
        const scoreColor = score >= 80 ? "#ff4444" : score >= 60 ? "#ff9500" : score >= 40 ? "#ffd60a" : "#8892b0";
        scoreBadge.style.cssText = `display:inline-flex;align-items:center;gap:4px;font-size:.76rem;font-weight:700;color:${scoreColor};background:${scoreColor}18;padding:2px 8px;border-radius:12px;border:1px solid ${scoreColor}44`;
        scoreBadge.textContent = `🔥 ${score}`;
        scoreBadge.title = "Viral potential score (0-100)";
        metaRow.appendChild(scoreBadge);
      }
      if (c.category) {
        const catBadge = document.createElement("span");
        const catIcons = { founder_story: "📖", product_spotlight: "🎯", business_advice: "💡", festival_vibe: "🎪" };
        const catLabels = { founder_story: "Founder Story", product_spotlight: "Product Spotlight", business_advice: "Business Advice", festival_vibe: "Festival Vibe" };
        catBadge.style.cssText = "display:inline-flex;align-items:center;gap:4px;font-size:.76rem;color:#a0b0c8;background:#1e2535;padding:2px 8px;border-radius:12px;border:1px solid #2d3a50";
        catBadge.textContent = `${catIcons[c.category] || "🏷"} ${catLabels[c.category] || c.category}`;
        metaRow.appendChild(catBadge);
      }
      if (c.suggested_headline) {
        const headlineBadge = document.createElement("span");
        headlineBadge.style.cssText = "font-size:.76rem;color:#8892b0;font-style:italic";
        headlineBadge.textContent = `💬 "${c.suggested_headline}"`;
        headlineBadge.title = "AI-suggested headline for this clip";
        metaRow.appendChild(headlineBadge);
      }
      card.appendChild(metaRow);
    }

    // Hook quote — editable too. Always render the row so the user can add
    // a quote even when Gemini didn't supply one.
    const quote = document.createElement("div");
    quote.className = "hl-quote";
    quote.contentEditable = "true";
    quote.spellcheck = false;
    quote.title = "Click to edit the hook quote";
    quote.style.cursor = "text";
    quote.style.outline = "none";
    quote.dataset.placeholder = "(no hook quote — click to add)";
    quote.textContent = editedQuote ? `"${editedQuote}"` : "";
    quote.addEventListener("focus", () => {
      // Strip surrounding quotes while editing so the user only types the
      // content, not the punctuation. Re-applied on blur.
      const t = quote.textContent.trim();
      if (t.startsWith("\"") && t.endsWith("\"")) {
        quote.textContent = t.slice(1, -1);
      }
    });
    quote.addEventListener("input", () => {
      editedQuote = quote.textContent.trim();
    });
    quote.addEventListener("blur", () => {
      const t = quote.textContent.trim();
      editedQuote = t;
      quote.textContent = t ? `"${t}"` : "";
    });
    quote.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); quote.blur(); }
    });
    card.appendChild(quote);

    if (c.reason) {
      const reason = document.createElement("div");
      reason.className = "hl-reason";
      reason.textContent = c.reason;
      card.appendChild(reason);
    }

    // ---- Editable start/end time inputs ----
    const timeRow = document.createElement("div");
    timeRow.className = "hl-time-edit";

    // Text inputs that accept either "m:ss.s" or plain seconds. Internal
    // state stays in seconds so downstream code (FFmpeg, /clip-from-job)
    // doesn't change.
    const mkInput = (val, onChange) => {
      const i = document.createElement("input");
      i.type = "text";
      i.value = _fmtTimeFine(val);
      i.style.fontFamily = "ui-monospace, monospace";
      i.oninput = () => {
        const v = _parseTime(i.value);
        if (v !== null) onChange(v);
      };
      return i;
    };

    const startLabel = document.createElement("label");
    startLabel.textContent = "Start ";
    const startInput = mkInput(editedStart, v => {
      editedStart = v;
      updateDurLabel();
    });
    startLabel.appendChild(startInput);
    timeRow.appendChild(startLabel);

    const endLabel = document.createElement("label");
    endLabel.textContent = "End ";
    const endInput = mkInput(editedEnd, v => {
      editedEnd = v;
      updateDurLabel();
    });
    endLabel.appendChild(endInput);
    timeRow.appendChild(endLabel);

    card.appendChild(timeRow);

    // ---- Action buttons (Phase 2 verb set, fixed order) ----
    // Preview → Open in Timeline → AI Edit… → Export clip → Add to compilation
    const actions = document.createElement("div");
    actions.className = "hl-actions";

    const previewBtn = document.createElement("button");
    previewBtn.textContent = "Preview";
    previewBtn.title = "Play and nudge this range";
    previewBtn.onclick = () => {
      openPreviewEditor({
        title: editedTitle,
        hookQuote: editedQuote,
        start: editedStart,
        end: editedEnd,
        onUpdate: (newStart, newEnd) => {
          editedStart = newStart;
          editedEnd = newEnd;
          startInput.value = _fmtTimeFine(newStart);
          endInput.value = _fmtTimeFine(newEnd);
          updateDurLabel();
        },
      });
    };
    actions.appendChild(previewBtn);

    const openTlBtn = document.createElement("button");
    openTlBtn.className = "primary hl-open-timeline";
    openTlBtn.textContent = "Open in Timeline";
    openTlBtn.title = "Put this range on the Timeline (Main track)";
    openTlBtn.onclick = () => {
      if (editedEnd <= editedStart) {
        alert("End time must be greater than start time.");
        return;
      }
      if (typeof window.openTimelineEditor === "function") {
        window.openTimelineEditor(currentJobId, {
          in: editedStart,
          out: editedEnd,
          newProject: true,
          replace: true,
        });
      } else {
        alert("Timeline is not available yet — try again in a second.");
      }
    };
    actions.appendChild(openTlBtn);

    const aiEditBtn = document.createElement("button");
    aiEditBtn.className = "hl-ai-edit";
    aiEditBtn.textContent = "AI Edit…";
    aiEditBtn.title = "Advanced: style + intensity → seeded Timeline project";
    aiEditBtn.onclick = () => {
      if (editedEnd <= editedStart) {
        alert("End time must be greater than start time.");
        return;
      }
      openAiEditPlan({
        source_job_id: currentJobId,
        start_time: editedStart,
        end_time: editedEnd,
        label: editedTitle || "highlight",
      });
    };
    actions.appendChild(aiEditBtn);

    const exportBtn = document.createElement("button");
    exportBtn.textContent = "Export clip";
    exportBtn.title = "Trim this range into a standalone video job";
    exportBtn.onclick = async () => {
      if (editedEnd <= editedStart) {
        alert("End time must be greater than start time.");
        return;
      }
      exportBtn.disabled = true;
      exportBtn.textContent = "Exporting…";
      try {
        const style = await flushCaptionLookToJob();
        const res = await fetch("/clip-from-job", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            source_job_id: currentJobId,
            start_time: editedStart,
            end_time: editedEnd,
            label: editedTitle || "highlight",
            style,
          }),
        });
        const j = await res.json();
        if (j.error) throw new Error(j.error);
        addJobToList(j.job_id);
        await refreshJobsList();
        await switchToJob(j.job_id, { force: true, tab: "ingest" });
      } catch (e) {
        alert("Could not export clip: " + e.message);
        exportBtn.disabled = false;
        exportBtn.textContent = "Export clip";
      }
    };
    actions.appendChild(exportBtn);

    const addBtn = document.createElement("button");
    addBtn.textContent = "Add to compilation";
    addBtn.title = "Queue this clip for the Compilation tab";
    addBtn.onclick = () => {
      if (editedEnd <= editedStart) {
        alert("End time must be greater than start time.");
        return;
      }
      addToCompileQueue({
        source_job_id: currentJobId,
        source_filename: (jobsById[currentJobId] && jobsById[currentJobId].filename) || "",
        start_time: editedStart,
        end_time: editedEnd,
        hook_quote: editedQuote,
        title: editedTitle,
      });
      addBtn.textContent = "✓ Added";
      addBtn.disabled = true;
      setTimeout(() => {
        addBtn.disabled = false;
        addBtn.textContent = "Add to compilation";
      }, 1500);
    };
    actions.appendChild(addBtn);

    card.appendChild(actions);
    hlResults.appendChild(card);
  });
}

// Ranges already shown to the user — fed back as avoid_ranges so "More
// options" returns a different batch instead of the same top picks.
let _hlAvoidRanges = [];
const hlMoreBtn = $("hlMoreBtn");

async function _runFindHighlights({ avoid }) {
  if (!requireReadyTranscript("AI Shorts")) return;
  const durations = Array.from(document.querySelectorAll(".hl-dur:checked"))
    .map(el => parseInt(el.value, 10));
  if (!durations.length) {
    alert("Pick at least one target length.");
    return;
  }
  hlFindBtn.disabled = true;
  if (hlMoreBtn) hlMoreBtn.disabled = true;
  hlStatus.textContent = avoid && avoid.length ? "Looking for different moments…" : "Asking Gemini…";
  hlResults.innerHTML = "";
  try {
    const res = await fetch("/suggest-clips", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: currentJobId,
        format: hlFormatEl.value,
        target_durations: durations,
        num_clips: parseInt(hlCountEl.value, 10),
        avoid_ranges: avoid || [],
      }),
    });
    const raw = await res.text();
    let data = null;
    try {
      data = JSON.parse(raw);
    } catch {
      throw new Error(
        res.status === 404
          ? "Shorts API not found — restart the app after pulling the latest branch."
          : res.status >= 500
            ? "Server error while finding highlights (often missing GEMINI_API_KEY or Gemini timeout). Check Replit secrets."
            : `Shorts response was not JSON (HTTP ${res.status}).`
      );
    }
    if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
    const clips = data.clips || [];
    renderHighlights(clips, data.format || hlFormatEl.value);
    // Accumulate every range we've shown so the next "More options" excludes them all.
    for (const c of clips) {
      _hlAvoidRanges.push([c.start_time, c.end_time]);
    }
    if (hlMoreBtn) hlMoreBtn.style.display = clips.length ? "" : "none";
    if (clips.length === 0 && avoid && avoid.length) {
      hlStatus.textContent = "No more distinct moments — Gemini ran out of fresh material.";
    }
  } catch (e) {
    hlStatus.textContent = "Error: " + e.message;
  } finally {
    hlFindBtn.disabled = false;
    if (hlMoreBtn) hlMoreBtn.disabled = false;
  }
}

if (hlFindBtn) {
  hlFindBtn.onclick = () => {
    _hlAvoidRanges = [];
    _runFindHighlights({ avoid: [] });
  };
}
if (hlMoreBtn) {
  hlMoreBtn.onclick = () => _runFindHighlights({ avoid: _hlAvoidRanges.slice() });
}

// =====================================================================
// AI Edit plan modal — style + intensity → seeded timeline project
// =====================================================================
let _aiEditCtx = null;
let _aiEditPackId = "pulse";
let _aiEditRecCuts = []; // full recommended cuts from preview

function _syncAiEditScopeUi() {
  const fullEl = $("aiEditScopeFull");
  const clipEl = $("aiEditScopeClip");
  const createClipEl = $("aiEditCreateClip");
  const scopeLabel = $("aiEditScopeLabel");
  const isFull = !!(fullEl && fullEl.checked) || !!( _aiEditCtx && _aiEditCtx.full_video );
  if (fullEl && clipEl && _aiEditCtx) {
    // If opened from a highlight window, allow clip scope; otherwise force full.
    const hasWindow = _aiEditCtx.end_time != null && _aiEditCtx.start_time != null
      && Number(_aiEditCtx.end_time) > Number(_aiEditCtx.start_time);
    clipEl.disabled = !hasWindow && !_aiEditCtx.allow_clip_scope;
    if (_aiEditCtx.full_video || !hasWindow) {
      fullEl.checked = true;
      if (clipEl) clipEl.checked = false;
    }
  }
  const useFull = !!(fullEl && fullEl.checked);
  if (createClipEl) {
    if (useFull) createClipEl.checked = false;
  }
  if (scopeLabel) {
    scopeLabel.textContent = useFull
      ? "Full video → cuts, zooms, captions → Timeline (no Shorts chop required)"
      : "Highlight window → cuts, zooms, captions → Timeline";
  }
}

async function openAiEditPlan(ctx) {
  _aiEditCtx = ctx || null;
  _aiEditRecCuts = [];
  const modal = $("aiEditModal");
  if (!modal || !_aiEditCtx) return;
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");

  const purposeEl = $("aiEditPurpose");
  if (purposeEl) purposeEl.value = _aiEditCtx.purpose || purposeEl.value || "";

  const createClipEl = $("aiEditCreateClip");
  if (createClipEl) {
    // Full-video edits stay on the source job. Short highlight chops can create a child job.
    const preferClip = !_aiEditCtx.full_video
      && _aiEditCtx.create_clip !== false
      && (_aiEditCtx.start_time != null && _aiEditCtx.end_time != null
          && Number(_aiEditCtx.end_time) - Number(_aiEditCtx.start_time) < 180);
    createClipEl.checked = preferClip;
  }
  const fullEl = $("aiEditScopeFull");
  const clipEl = $("aiEditScopeClip");
  if (fullEl && clipEl) {
    if (_aiEditCtx.full_video || _aiEditCtx.end_time == null) {
      fullEl.checked = true;
      clipEl.checked = false;
    } else {
      clipEl.checked = true;
      fullEl.checked = false;
    }
  }
  _syncAiEditScopeUi();

  const status = $("aiEditCutsPreview");
  if (status) status.textContent = "Loading style packs…";
  const outline = $("aiEditOutline");
  if (outline) {
    outline.innerHTML = "";
    outline.classList.remove("has-cuts");
  }
  // Prefer CapCut → AI Edit pack mapping when set on AI Shorts.
  const fromCapcut = window._preferredAiEditPack
    || (window._pendingCapcutTemplate && window._pendingCapcutTemplate.ai_edit_pack)
    || null;
  if (fromCapcut) _aiEditPackId = fromCapcut;

  try {
    const res = await fetch("/ai-edit/style-packs");
    const data = await res.json();
    const packs = data.packs || [];
    const host = $("aiEditPacks");
    if (host) {
      host.innerHTML = "";
      packs.forEach((p) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ai-edit-pack" + (p.id === _aiEditPackId ? " active" : "");
        btn.dataset.pack = p.id;
        btn.innerHTML = `<strong>${p.label}</strong><span>${p.blurb || ""}</span>`;
        btn.onclick = () => {
          _aiEditPackId = p.id;
          host.querySelectorAll(".ai-edit-pack").forEach((el) => el.classList.toggle("active", el.dataset.pack === p.id));
          previewAiEditCuts();
        };
        host.appendChild(btn);
      });
      if (!packs.find((p) => p.id === _aiEditPackId) && packs[0]) {
        _aiEditPackId = packs[0].id;
      }
      host.querySelectorAll(".ai-edit-pack").forEach((el) => el.classList.toggle("active", el.dataset.pack === _aiEditPackId));
    }
    await previewAiEditCuts();
  } catch (e) {
    if (status) status.textContent = "Could not load style packs: " + e.message;
  }
}

function closeAiEditPlan() {
  const modal = $("aiEditModal");
  if (!modal) return;
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
  _aiEditCtx = null;
  _aiEditRecCuts = [];
}

function _fmtCutTime(t) {
  const v = Math.max(0, Number(t) || 0);
  const m = Math.floor(v / 60);
  const s = (v - m * 60).toFixed(1);
  return `${m}:${s.padStart(4, "0")}`;
}

function renderAiEditOutline(rec) {
  const outline = $("aiEditOutline");
  if (!outline) return;
  const cuts = (rec && rec.cuts) || [];
  _aiEditRecCuts = cuts.map((c) => [Number(c[0]), Number(c[1])]);
  if (!_aiEditRecCuts.length) {
    outline.innerHTML = "";
    outline.classList.remove("has-cuts");
    return;
  }
  outline.classList.add("has-cuts");
  const details = (rec && rec.cut_details) || [];
  let html = `<div class="ai-edit-outline-head">
    <span>Outline · recommended cuts (uncheck to keep)</span>
    <button type="button" class="tl-chip-btn" id="aiEditRestoreCuts">Restore all</button>
  </div>`;
  _aiEditRecCuts.forEach((c, i) => {
    const dur = (c[1] - c[0]).toFixed(1);
    const d = details[i] || {};
    const kind = d.kind === "silence" ? "silence" : "filler";
    const ctx = (d.context_before || d.context_after)
      ? `${d.context_before || ""} … ${d.context_after || ""}`.trim()
      : kind;
    html += `<label class="ai-edit-cut-row">
      <input type="checkbox" class="ai-edit-cut-check" data-idx="${i}" checked>
      <span><span class="cut-meta">${_fmtCutTime(c[0])}–${_fmtCutTime(c[1])} · ${dur}s · ${kind}</span><br>${ctx}</span>
    </label>`;
  });
  outline.innerHTML = html;
  const restore = $("aiEditRestoreCuts");
  if (restore) {
    restore.onclick = () => {
      outline.querySelectorAll(".ai-edit-cut-check").forEach((cb) => { cb.checked = true; });
    };
  }
}

function selectedAiEditCuts() {
  const outline = $("aiEditOutline");
  if (!outline || !_aiEditRecCuts.length) return _aiEditRecCuts.slice();
  const selected = [];
  outline.querySelectorAll(".ai-edit-cut-check").forEach((cb) => {
    if (!cb.checked) return;
    const i = parseInt(cb.dataset.idx, 10);
    if (!isNaN(i) && _aiEditRecCuts[i]) selected.push(_aiEditRecCuts[i]);
  });
  return selected;
}

function _aiEditIsFullScope() {
  const fullEl = $("aiEditScopeFull");
  if (fullEl) return !!fullEl.checked;
  return !!(_aiEditCtx && _aiEditCtx.full_video);
}

async function previewAiEditCuts() {
  if (!_aiEditCtx) return;
  const el = $("aiEditCutsPreview");
  if (!el) return;
  el.textContent = "Estimating recommended cuts…";
  try {
    const full = _aiEditIsFullScope();
    const body = {
      job_id: _aiEditCtx.source_job_id,
      start_time: full ? 0 : _aiEditCtx.start_time,
      end_time: full ? undefined : _aiEditCtx.end_time,
    };
    const res = await fetch("/recommended-cuts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    const st = data.stats || {};
    el.textContent = (full ? "Full video · " : "Window · ")
      + `Recommended AI Trim: ${st.cut_count || 0} cut(s) · ~${st.seconds_removed || 0}s removed`
      + (data.filler_count ? ` · ${data.filler_count} filler word(s)` : "")
      + ((data.silence_gaps || []).length ? ` · ${(data.silence_gaps || []).length} silence gap(s)` : "");
    renderAiEditOutline(data);
  } catch (e) {
    el.textContent = "Cut preview unavailable: " + e.message;
    renderAiEditOutline({ cuts: [] });
  }
}

async function runAiEditGenerate() {
  if (!_aiEditCtx) return;
  const btn = $("aiEditGenerate");
  const status = $("aiEditStatus");
  if (btn) { btn.disabled = true; btn.textContent = "Creating…"; }
  if (status) status.textContent = "Building AI Edit project…";
  try {
    const intensity = ($("aiEditIntensity") && $("aiEditIntensity").value) || "med";
    const applyCuts = !($("aiEditApplyCuts") && !$("aiEditApplyCuts").checked);
    const full = _aiEditIsFullScope();
    const createClip = !full && !($("aiEditCreateClip") && !$("aiEditCreateClip").checked);
    const insertMedia = !($("aiEditInsertMedia") && !$("aiEditInsertMedia").checked);
    const purpose = ($("aiEditPurpose") && $("aiEditPurpose").value || "").trim();
    const cuts = applyCuts ? selectedAiEditCuts() : [];
    const res = await fetch("/ai-edit-seed", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_job_id: _aiEditCtx.source_job_id,
        start_time: full ? 0 : _aiEditCtx.start_time,
        end_time: full ? undefined : _aiEditCtx.end_time,
        full_video: full,
        label: _aiEditCtx.label || (full ? "Full-video AI Edit" : "AI Edit"),
        purpose: purpose || undefined,
        style_pack: _aiEditPackId,
        intensity,
        apply_cuts: applyCuts,
        create_clip: createClip,
        insert_media: insertMedia,
        cuts,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || res.statusText);
    if (data.clip_job_id) {
      try {
        addJobToList(data.clip_job_id);
        await refreshJobsList();
      } catch { /* non-fatal */ }
    }
    closeAiEditPlan();
    if (typeof window.openTimelineEditor === "function") {
      await window.openTimelineEditor(data.job_id, {
        newProject: true,
        replace: true,
        seedTimeline: data.timeline,
        mediaHints: data.media_hints || (data.timeline && data.timeline.media_hints),
        label: data.label || "AI Edit",
      });
    } else {
      alert("Timeline Editor is not available.");
    }
    if (data.warning) console.warn("[ai-edit]", data.warning);
  } catch (e) {
    if (status) status.textContent = "Error: " + e.message;
    alert("AI Edit failed: " + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Create project"; }
  }
}

(function wireAiEditModal() {
  const close = () => closeAiEditPlan();
  if ($("aiEditClose")) $("aiEditClose").onclick = close;
  if ($("aiEditCancel")) $("aiEditCancel").onclick = close;
  if ($("aiEditGenerate")) $("aiEditGenerate").onclick = () => runAiEditGenerate();
  const modal = $("aiEditModal");
  if (modal) {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) close();
    });
  }
  const applyCutsEl = $("aiEditApplyCuts");
  if (applyCutsEl) {
    applyCutsEl.addEventListener("change", () => {
      const outline = $("aiEditOutline");
      if (!outline) return;
      outline.style.opacity = applyCutsEl.checked ? "1" : "0.45";
      outline.querySelectorAll(".ai-edit-cut-check").forEach((cb) => {
        cb.disabled = !applyCutsEl.checked;
      });
    });
  }
  // AI Edit whole current job (full video) — ready panel / AI Shorts.
  window.openAiEditPlanForJob = function (jobId, opts) {
    opts = opts || {};
    const full = opts.full_video !== false && opts.end_time == null;
    openAiEditPlan({
      source_job_id: jobId || currentJobId,
      start_time: full ? 0 : (opts.start_time != null ? opts.start_time : 0),
      end_time: full ? null : opts.end_time,
      full_video: full,
      label: opts.label || (full ? "Full-video AI Edit" : "AI Edit"),
      purpose: opts.purpose || "",
      create_clip: opts.create_clip === true ? true : false,
      allow_clip_scope: !!opts.allow_clip_scope,
    });
  };

  const scopeFull = $("aiEditScopeFull");
  const scopeClip = $("aiEditScopeClip");
  const onScopeChange = () => {
    _syncAiEditScopeUi();
    previewAiEditCuts();
  };
  if (scopeFull) scopeFull.addEventListener("change", onScopeChange);
  if (scopeClip) scopeClip.addEventListener("change", onScopeChange);
})();

function openAiEditFullFromReady() {
  if (!requireReadyTranscript("AI Edit full video")) return;
  if (typeof window.openAiEditPlanForJob !== "function") {
    alert("AI Edit is still loading — try again in a second.");
    return;
  }
  window.openAiEditPlanForJob(currentJobId, {
    full_video: true,
    label: (jobsById[currentJobId] && jobsById[currentJobId].filename) || "Full-video AI Edit",
  });
}

function updateAiEditNudge(_isLongForm) {
  // AI Edit nudge stays on AI Shorts only — never on Edit Words / ready panel.
  const nudge = $("aiEditNudge");
  if (nudge) nudge.classList.add("hidden");
}
const aiEditNudgeBtn = $("aiEditNudgeBtn");
if (aiEditNudgeBtn) {
  aiEditNudgeBtn.onclick = () => {
    if (!currentJobId) return;
    if (typeof window.openAiEditPlanForJob === "function") {
      window.openAiEditPlanForJob(currentJobId, {
        full_video: true,
        label: (jobsById[currentJobId] && jobsById[currentJobId].filename) || "Full-video AI Edit",
      });
    }
  };
}

// =====================================================================
// Compilation queue
// =====================================================================

const COMPILE_QUEUE_KEY = "subtitleBurner:compilationQueue";
const compilePanel = $("compilePanel");
const compileListEl = $("compileList");
const compileCountEl = $("compileCount");
const compileGoBtn = $("compileGoBtn");
const compileClearBtn = $("compileClearBtn");
const compileLabelEl = $("compileLabel");
const compileStatus = $("compileStatus");

function loadCompileQueue() {
  try {
    const raw = localStorage.getItem(COMPILE_QUEUE_KEY);
    if (raw) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) return arr;
    }
  } catch (e) {}
  return [];
}

function saveCompileQueue(q) {
  localStorage.setItem(COMPILE_QUEUE_KEY, JSON.stringify(q));
}

function addToCompileQueue(item) {
  const q = loadCompileQueue();
  q.push(item);
  saveCompileQueue(q);
  renderCompileQueue();
}

function removeFromCompileQueue(idx) {
  const q = loadCompileQueue();
  q.splice(idx, 1);
  saveCompileQueue(q);
  renderCompileQueue();
}

function renderCompileQueue() {
  if (!compilePanel) return;
  // Panel itself always stays visible inside the Compilation tab; only the
  // list contents and the action buttons reflect the current queue state.
  compilePanel.classList.remove("hidden");
  const q = loadCompileQueue();
  // Update the tab nav badge in lockstep.
  const badge = document.getElementById("compileBadge");
  if (badge) {
    if (q.length > 0) {
      badge.textContent = String(q.length);
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  }
  const totalDur = q.reduce((s, c) => s + (c.end_time - c.start_time), 0);
  compileCountEl.textContent = q.length
    ? `${q.length} clip${q.length === 1 ? "" : "s"} · ~${Math.round(totalDur)}s total`
    : "0 clips";
  if (compileGoBtn) compileGoBtn.disabled = !q.length;
  if (compileClearBtn) compileClearBtn.disabled = !q.length;
  const compileToTl = $("compileToTimelineBtn");
  if (compileToTl) compileToTl.disabled = !q.length;

  compileListEl.innerHTML = "";
  if (!q.length) {
    const hint = document.createElement("p");
    hint.className = "muted";
    hint.style.cssText = "text-align:center;padding:24px 12px;font-size:.9rem";
    hint.innerHTML = "Nothing queued yet. On <strong>AI Shorts</strong>, click <strong>Add to compilation</strong> on the clips you want to stitch together.";
    compileListEl.appendChild(hint);
    return;
  }
  q.forEach((item, idx) => {
    const row = document.createElement("div");
    row.className = "compile-item";
    if (item.source_available === false) row.classList.add("compile-missing");
    row.draggable = true;
    row.dataset.idx = String(idx);

    const grip = document.createElement("span");
    grip.className = "compile-grip";
    grip.title = "Drag to reorder";
    grip.textContent = "⋮⋮";
    row.appendChild(grip);

    const meta = document.createElement("div");
    meta.className = "compile-meta";
    const src = document.createElement("div");
    src.className = "compile-source";

    // Static prefix: `<idx>. <filename> · ` — index renumbers on reorder so
    // it lives outside the editable element, otherwise the edit cursor
    // would land in dead text.
    const prefix = document.createElement("span");
    prefix.textContent = `${idx + 1}. ${item.source_filename || item.source_job_id.slice(0, 8) + "…"} · `;
    src.appendChild(prefix);

    // Editable title — persisted to localStorage immediately so the new
    // value flows into /compile-clips and is restored on page reload.
    const titleSpan = document.createElement("span");
    titleSpan.className = "compile-title-edit";
    titleSpan.contentEditable = "true";
    titleSpan.spellcheck = false;
    titleSpan.draggable = false;
    titleSpan.title = "Click to rename this clip";
    titleSpan.textContent = item.title || "highlight";
    titleSpan.addEventListener("input", () => {
      const q = loadCompileQueue();
      if (q[idx]) {
        q[idx] = { ...q[idx], title: titleSpan.textContent.trim() || "highlight" };
        saveCompileQueue(q);
      }
    });
    titleSpan.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); titleSpan.blur(); }
    });
    src.appendChild(titleSpan);

    if (item.source_available === false) {
      const missing = document.createElement("span");
      missing.textContent = " ⚠ source missing";
      missing.style.color = "#ff8a8a";
      src.appendChild(missing);
    }
    meta.appendChild(src);

    // Editable hook quote — always shown so the user can add one even when
    // the highlight didn't have one.
    const quote = document.createElement("div");
    quote.className = "compile-quote";
    quote.contentEditable = "true";
    quote.spellcheck = false;
    quote.draggable = false;
    quote.title = "Click to edit the hook quote";
    quote.dataset.placeholder = "(no hook quote — click to add)";
    quote.textContent = item.hook_quote ? `"${item.hook_quote}"` : "";
    quote.addEventListener("focus", () => {
      const t = quote.textContent.trim();
      if (t.startsWith("\"") && t.endsWith("\"")) quote.textContent = t.slice(1, -1);
    });
    quote.addEventListener("input", () => {
      const q = loadCompileQueue();
      if (q[idx]) {
        q[idx] = { ...q[idx], hook_quote: quote.textContent.trim() };
        saveCompileQueue(q);
      }
    });
    quote.addEventListener("blur", () => {
      const t = quote.textContent.trim();
      quote.textContent = t ? `"${t}"` : "";
    });
    quote.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); quote.blur(); }
    });
    meta.appendChild(quote);
    row.appendChild(meta);

    // Inline editable start/end. Persists to localStorage on every input
    // so re-renders, drag-reorders, and the eventual /compile-clips POST
    // all see the latest times. Format is m:ss.s — same as the highlight
    // cards — so muscle memory carries over.
    const time = document.createElement("span");
    time.className = "compile-time";
    const durLabel = document.createElement("span");
    durLabel.className = "compile-duration";
    const refreshDur = () => {
      const cur = loadCompileQueue()[idx];
      if (!cur) return;
      const dur = cur.end_time - cur.start_time;
      durLabel.textContent = `${dur.toFixed(1)}s`;
      // Flag invalid ranges (end ≤ start) on the row so the user notices
      // before hitting Compile, where the backend would reject the clip.
      const invalid = !(dur > 0);
      row.classList.toggle("compile-invalid", invalid);
      durLabel.title = invalid ? "End must be greater than start" : "";
    };
    const mkTimeInput = (val, onChange) => {
      const i = document.createElement("input");
      i.type = "text";
      i.value = _fmtTimeFine(val);
      i.className = "compile-time-input";
      i.draggable = false;
      i.addEventListener("click", (ev) => ev.stopPropagation());
      i.addEventListener("input", () => {
        const v = _parseTime(i.value);
        if (v === null) return;
        onChange(v);
      });
      return i;
    };
    const startIn = mkTimeInput(item.start_time, (v) => {
      const cur = loadCompileQueue();
      if (!cur[idx]) return;
      cur[idx] = { ...cur[idx], start_time: v };
      saveCompileQueue(cur);
      refreshDur();
    });
    const endIn = mkTimeInput(item.end_time, (v) => {
      const cur = loadCompileQueue();
      if (!cur[idx]) return;
      cur[idx] = { ...cur[idx], end_time: v };
      saveCompileQueue(cur);
      refreshDur();
    });
    time.appendChild(startIn);
    const arrow = document.createElement("span");
    arrow.textContent = "→";
    arrow.style.opacity = "0.6";
    arrow.style.padding = "0 4px";
    time.appendChild(arrow);
    time.appendChild(endIn);
    time.appendChild(durLabel);
    refreshDur();
    row.appendChild(time);

    const previewBtn = document.createElement("button");
    previewBtn.textContent = "▶";
    previewBtn.className = "compile-preview";
    previewBtn.title = "Preview just this clip";
    previewBtn.disabled = item.source_available === false;
    previewBtn.onclick = (e) => {
      e.stopPropagation();
      previewCompileItem(idx);
    };
    row.appendChild(previewBtn);

    const rm = document.createElement("button");
    rm.textContent = "✕";
    rm.className = "compile-remove";
    rm.title = "Remove from queue";
    rm.onclick = () => removeFromCompileQueue(idx);
    row.appendChild(rm);

    row.addEventListener("dragstart", (e) => {
      row.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      // Firefox refuses to start a drag without setData.
      e.dataTransfer.setData("text/plain", String(idx));
    });
    row.addEventListener("dragend", () => {
      row.classList.remove("dragging");
      compileListEl.querySelectorAll(".compile-item.drop-target")
        .forEach(el => el.classList.remove("drop-target", "drop-above", "drop-below"));
    });

    row.addEventListener("dragover", (e) => {
      const dragging = compileListEl.querySelector(".compile-item.dragging");
      if (!dragging || dragging === row) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const r = row.getBoundingClientRect();
      const above = (e.clientY - r.top) < r.height / 2;
      row.classList.add("drop-target");
      row.classList.toggle("drop-above", above);
      row.classList.toggle("drop-below", !above);
    });
    row.addEventListener("dragleave", () => {
      row.classList.remove("drop-target", "drop-above", "drop-below");
    });
    row.addEventListener("drop", (e) => {
      e.preventDefault();
      const fromIdx = parseInt(e.dataTransfer.getData("text/plain"), 10);
      if (Number.isNaN(fromIdx) || fromIdx === idx) return;
      const r = row.getBoundingClientRect();
      const above = (e.clientY - r.top) < r.height / 2;
      let toIdx = above ? idx : idx + 1;
      if (fromIdx < toIdx) toIdx -= 1;
      reorderCompileQueue(fromIdx, toIdx);
    });

    compileListEl.appendChild(row);
  });
}

function reorderCompileQueue(fromIdx, toIdx) {
  const q = loadCompileQueue();
  if (fromIdx < 0 || fromIdx >= q.length) return;
  if (toIdx < 0) toIdx = 0;
  if (toIdx > q.length - 1) toIdx = q.length - 1;
  if (fromIdx === toIdx) return;
  const [moved] = q.splice(fromIdx, 1);
  q.splice(toIdx, 0, moved);
  saveCompileQueue(q);
  renderCompileQueue();
}

// Preview a single queued item by reusing the Edit-tab preview-editor
// (sourcePlayer + start/end stop-handler). Switches the player's source if
// the clip belongs to a different job than the one currently loaded, and
// wires onUpdate so any timing nudges in the panel persist back to the
// compilation queue's localStorage entry.
function previewCompileItem(idx) {
  const q = loadCompileQueue();
  const item = q[idx];
  if (!item) return;
  if (item.source_available === false) {
    alert("This clip's source video is missing — can't preview.");
    return;
  }
  const targetSrc = `/raw-upload/${item.source_job_id}`;
  const sameSrc = sourcePlayer && sourcePlayer.src.endsWith(targetSrc);
  if (!sameSrc && sourcePlayer) sourcePlayer.src = targetSrc;
  openPreviewEditor({
    title: item.title || "highlight",
    hookQuote: item.hook_quote || "",
    start: item.start_time,
    end: item.end_time,
    onUpdate: (newStart, newEnd) => {
      const cur = loadCompileQueue();
      if (!cur[idx]) return;
      cur[idx] = { ...cur[idx], start_time: newStart, end_time: newEnd };
      saveCompileQueue(cur);
      renderCompileQueue();
    },
  });
}

// Sequence-play every queued clip in order through the source player. Skips
// items whose source is missing. Uses the Edit-tab player so the user gets
// the standard scrubber + audio. Clips from different jobs work because we
// switch the player's src between segments.
let _compileSequenceActive = false;
let _compileSequenceCancel = null;

async function previewCompileAll() {
  if (_compileSequenceActive) {
    if (_compileSequenceCancel) _compileSequenceCancel();
    return;
  }
  const q = loadCompileQueue();
  const playable = q.filter(it => it.source_available !== false);
  if (!playable.length) {
    alert("Queue is empty (or all clips have missing sources).");
    return;
  }
  setActiveTab("compilation");
  await new Promise(r => requestAnimationFrame(r));
  _compileSequenceActive = true;
  const btn = $("compilePreviewAllBtn");
  if (btn) btn.textContent = "⏹ Stop preview";

  let cancelled = false;
  _compileSequenceCancel = () => { cancelled = true; sourcePlayer.pause(); };

  try {
    for (let i = 0; i < playable.length; i++) {
      if (cancelled) break;
      const it = playable[i];
      const src = `/raw-upload/${it.source_job_id}`;
      if (!sourcePlayer.src.endsWith(src)) {
        sourcePlayer.src = src;
        // Wait for the new source to be seekable.
        await new Promise(resolve => {
          const ready = () => {
            sourcePlayer.removeEventListener("loadedmetadata", ready);
            resolve();
          };
          sourcePlayer.addEventListener("loadedmetadata", ready);
        });
      }
      sourcePlayer.currentTime = it.start_time;
      try { await sourcePlayer.play(); } catch { /* autoplay blocked? */ }
      // Wait for end of segment OR cancellation.
      await new Promise(resolve => {
        const onTime = () => {
          if (cancelled || sourcePlayer.currentTime >= it.end_time) {
            sourcePlayer.pause();
            sourcePlayer.removeEventListener("timeupdate", onTime);
            resolve();
          }
        };
        sourcePlayer.addEventListener("timeupdate", onTime);
      });
    }
  } finally {
    _compileSequenceActive = false;
    _compileSequenceCancel = null;
    if (btn) btn.textContent = "▶ Preview all";
  }
}

const compilePreviewAllBtn = $("compilePreviewAllBtn");
if (compilePreviewAllBtn) {
  compilePreviewAllBtn.onclick = previewCompileAll;
}

function sendCompileQueueToTimeline() {
  const q = loadCompileQueue().filter((it) => it.source_available !== false);
  if (!q.length) {
    alert("Queue is empty (or all clips have missing sources).");
    return;
  }
  if (typeof window.openTimelineEditor !== "function") {
    alert("Editor is still loading — try again in a second.");
    return;
  }
  window.openTimelineEditor(null, {
    clips: q.map((it) => ({
      source_job_id: it.source_job_id,
      start_time: it.start_time,
      end_time: it.end_time,
    })),
    replace: true,
    newProject: true,
  });
}

const compileToTimelineBtn = $("compileToTimelineBtn");
if (compileToTimelineBtn) {
  compileToTimelineBtn.onclick = sendCompileQueueToTimeline;
}

if (compileGoBtn) {
  compileGoBtn.onclick = async () => {
    const q = loadCompileQueue();
    if (!q.length) return;
    compileGoBtn.disabled = true;
    compileStatus.textContent = "Stitching clips…";
    try {
      const res = await fetch("/compile-clips", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          clips: q.map(c => ({
            source_job_id: c.source_job_id,
            start_time: c.start_time,
            end_time: c.end_time,
            title: c.title || "",
            hook_quote: c.hook_quote || "",
            source_filename: c.source_filename || "",
          })),
          label: (compileLabelEl.value || "compilation").trim(),
        }),
      });
      const j = await res.json();
      if (j.error) throw new Error(j.error);
      compileStatus.textContent = `✓ Compiled ${j.segments} clip${j.segments === 1 ? "" : "s"} into one MP4` +
        (j.duration ? ` (${Number(j.duration).toFixed(1)}s)` : "") +
        (j.canvas ? ` · ${j.canvas}` : "") + ".";
      // Clear the queue and switch to the new compilation job.
      saveCompileQueue([]);
      renderCompileQueue();
      compileLabelEl.value = "";
      addJobToList(j.job_id);
      await refreshJobsList();
      refreshPastCompiles();
      await switchToJob(j.job_id);
      // Close the Captions-style loop: offer Timeline as the edit surface.
      if (typeof window.openTimelineEditor === "function") {
        const goTl = confirm(
          `Compiled ${j.segments} highlight${j.segments === 1 ? "" : "s"} into ONE video.\n\n` +
          `Open in Timeline to edit captions / effects / Render?\n\n` +
          `Tip: leave Detect shots alone unless you want to split the compilation back into scenes.`
        );
        if (goTl) {
          window.openTimelineEditor(j.job_id, { replace: true, newProject: true });
        }
      }
    } catch (e) {
      compileStatus.textContent = "Error: " + e.message;
    } finally {
      compileGoBtn.disabled = false;
    }
  };
}

if (compileClearBtn) {
  compileClearBtn.onclick = () => {
    if (!loadCompileQueue().length) return;
    if (confirm("Clear all clips from the compilation queue?")) {
      saveCompileQueue([]);
      renderCompileQueue();
    }
  };
}

renderCompileQueue();

// ---- Multi-interview: shared themes across uploaded jobs ----
const multiJobListEl = $("multiInterviewJobList");
const multiThemesEl = $("multiInterviewThemes");
const multiStatusEl = $("multiInterviewStatus");

function _jobReadyForMulti(j) {
  if (!j || !j.job_id) return false;
  const st = String(j.status || "");
  return st === "awaiting_edit" || st === "done" || st === "ready" || !!j.has_words || (j.word_count || 0) > 0;
}

function renderMultiInterviewJobs() {
  if (!multiJobListEl) return;
  const list = Object.values(jobsById || {}).filter(_jobReadyForMulti);
  multiJobListEl.innerHTML = "";
  if (!list.length) {
    multiJobListEl.innerHTML = `<p class="muted" style="font-size:.84rem;margin:0">No transcribed videos yet. Upload 2+ interviews on <strong>Ingest</strong>, wait until ready, then refresh.</p>`;
    return;
  }
  list.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  list.forEach((j) => {
    const row = document.createElement("label");
    row.style.cssText = "display:flex;align-items:center;gap:8px;padding:6px 8px;background:#12151f;border:1px solid #2a2f3a;border-radius:8px;font-size:.84rem;cursor:pointer";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "multi-interview-job";
    cb.value = j.job_id;
    const name = document.createElement("span");
    name.style.flex = "1";
    name.textContent = j.filename || j.job_id.slice(0, 8);
    const meta = document.createElement("span");
    meta.className = "muted";
    meta.style.fontSize = ".72rem";
    meta.textContent = j.status || "";
    row.appendChild(cb);
    row.appendChild(name);
    row.appendChild(meta);
    multiJobListEl.appendChild(row);
  });
}

function renderMultiInterviewThemes(themes) {
  if (!multiThemesEl) return;
  multiThemesEl.innerHTML = "";
  if (!themes || !themes.length) {
    multiThemesEl.innerHTML = `<p class="muted" style="font-size:.84rem">No shared themes found. Try more interviews or a broader topic set.</p>`;
    return;
  }
  themes.forEach((th, idx) => {
    const card = document.createElement("div");
    card.style.cssText = "padding:12px;background:#12151f;border:1px solid #2a2f3a;border-radius:10px";
    const head = document.createElement("div");
    head.style.cssText = "display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:flex-start";
    const title = document.createElement("div");
    title.innerHTML = `<strong>${escHtml(th.theme || ("Theme " + (idx + 1)))}</strong>`
      + (th.question ? `<div class="muted" style="font-size:.78rem;margin-top:4px">${escHtml(th.question)}</div>` : "");
    const addBtn = document.createElement("button");
    addBtn.className = "btn btn-secondary";
    addBtn.type = "button";
    addBtn.textContent = `＋ Add ${ (th.clips || []).length } clip(s) to queue`;
    addBtn.onclick = () => {
      (th.clips || []).forEach((c) => {
        addToCompileQueue({
          source_job_id: c.source_job_id,
          source_filename: c.source_filename || "",
          start_time: c.start_time,
          end_time: c.end_time,
          title: c.title || th.theme || "",
          hook_quote: c.hook_quote || "",
        });
      });
      addBtn.textContent = "✓ Added";
      addBtn.disabled = true;
      if (multiStatusEl) multiStatusEl.textContent = `Queued theme “${th.theme || ""}”.`;
    };
    head.appendChild(title);
    head.appendChild(addBtn);
    card.appendChild(head);
    const list = document.createElement("ul");
    list.style.cssText = "margin:10px 0 0;padding-left:18px;font-size:.8rem;color:#a8b0c0";
    (th.clips || []).forEach((c) => {
      const li = document.createElement("li");
      const fname = c.source_filename || String(c.source_job_id || "").slice(0, 8);
      li.textContent = `${fname}: ${Number(c.start_time).toFixed(1)}s–${Number(c.end_time).toFixed(1)}s`
        + (c.hook_quote ? ` — “${c.hook_quote}”` : "");
      list.appendChild(li);
    });
    card.appendChild(list);
    multiThemesEl.appendChild(card);
  });
}

function escHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

const multiRefreshBtn = $("multiInterviewRefreshBtn");
if (multiRefreshBtn) {
  multiRefreshBtn.onclick = async () => {
    await refreshJobsList();
    renderMultiInterviewJobs();
  };
}
const multiPlanBtn = $("multiInterviewPlanBtn");
if (multiPlanBtn) {
  multiPlanBtn.onclick = async () => {
    const ids = Array.from(document.querySelectorAll(".multi-interview-job:checked")).map((el) => el.value);
    if (ids.length < 2) {
      alert("Select at least 2 transcribed interviews.");
      return;
    }
    multiPlanBtn.disabled = true;
    if (multiStatusEl) multiStatusEl.textContent = "Planning with Gemini…";
    try {
      const res = await fetch("/multi-interview/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_ids: ids, format: "interview" }),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      renderMultiInterviewThemes(data.themes || []);
      if (multiStatusEl) {
        multiStatusEl.textContent = `Found ${data.theme_count || 0} theme(s), ${data.clip_count || 0} clip(s).`;
      }
    } catch (e) {
      if (multiStatusEl) multiStatusEl.textContent = "Error: " + e.message;
      alert("Multi-interview plan failed: " + e.message);
    } finally {
      multiPlanBtn.disabled = false;
    }
  };
}

renderMultiInterviewJobs();
// Keep the checklist fresh when the jobs list refreshes.
const _origRefreshJobsList = refreshJobsList;
refreshJobsList = async function () {
  const r = await _origRefreshJobsList.apply(this, arguments);
  try { renderMultiInterviewJobs(); } catch (e) { /* ignore */ }
  return r;
};

// ---- Past compilations: list, load-back-into-queue ----
const pastCompilesListEl = $("pastCompilesList");
const pastCompilesCountEl = $("pastCompilesCount");

function _fmtCompileDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}

async function refreshPastCompiles() {
  if (!pastCompilesListEl) return;
  let comps = [];
  try {
    const res = await fetch("/list-compilations");
    const data = await res.json();
    comps = data.compilations || [];
  } catch (e) {
    pastCompilesListEl.innerHTML = `<div class="muted" style="padding:12px">Couldn't load: ${e.message}</div>`;
    return;
  }
  if (pastCompilesCountEl) {
    pastCompilesCountEl.textContent = comps.length
      ? `${comps.length} saved`
      : "";
  }
  pastCompilesListEl.innerHTML = "";
  if (!comps.length) {
    const hint = document.createElement("div");
    hint.className = "muted";
    hint.style.cssText = "text-align:center;padding:20px 12px;font-size:.88rem";
    hint.textContent = "No past compilations yet. After your first Compile, it'll show up here so you can edit and re-render.";
    pastCompilesListEl.appendChild(hint);
    return;
  }
  comps.forEach(c => {
    const card = document.createElement("div");
    card.className = "past-compile-card";
    const head = document.createElement("div");
    head.className = "past-compile-head";
    const label = document.createElement("strong");
    label.textContent = c.label || c.filename || "compilation";
    head.appendChild(label);
    const meta = document.createElement("span");
    meta.className = "muted";
    meta.style.fontSize = ".78rem";
    meta.textContent = `${c.segment_count} clip${c.segment_count === 1 ? "" : "s"} · ${c.total_duration.toFixed(1)}s · ${_fmtCompileDate(c.created_at)}`;
    head.appendChild(meta);
    card.appendChild(head);

    const actions = document.createElement("div");
    actions.className = "past-compile-actions";

    const editBtn = document.createElement("button");
    editBtn.textContent = "✎ Edit clips";
    editBtn.className = "btn btn-secondary";
    editBtn.onclick = () => loadCompilationIntoQueue(c.job_id);
    actions.appendChild(editBtn);

    const openBtn = document.createElement("button");
    openBtn.textContent = "Open job";
    openBtn.className = "btn";
    openBtn.title = "Open this compiled job on Transcript Cut";
    openBtn.onclick = async () => {
      await switchToJob(c.job_id, { force: true, tab: "transcript" });
    };
    actions.appendChild(openBtn);

    card.appendChild(actions);
    pastCompilesListEl.appendChild(card);
  });
}

async function loadCompilationIntoQueue(jobId) {
  let data;
  try {
    const res = await fetch(`/load-compilation/${jobId}`);
    data = await res.json();
    if (data.error) throw new Error(data.error);
  } catch (e) {
    alert("Couldn't load: " + e.message);
    return;
  }
  const existing = loadCompileQueue();
  if (existing.length) {
    const ok = confirm(
      `The compilation queue has ${existing.length} clip${existing.length === 1 ? "" : "s"} in it. Replace with the ${data.clips.length}-clip recipe from "${data.label}"?`
    );
    if (!ok) return;
  }
  const missing = data.clips.filter(c => !c.source_available).length;
  const restored = data.clips.map(c => ({
    source_job_id: c.source_job_id,
    source_filename: c.source_filename || "",
    start_time: c.start_time,
    end_time: c.end_time,
    title: c.title || "",
    hook_quote: c.hook_quote || "",
    source_available: c.source_available,
  }));
  saveCompileQueue(restored);
  renderCompileQueue();
  if (compileLabelEl && data.label) compileLabelEl.value = data.label;
  if (compileStatus) {
    compileStatus.textContent = missing
      ? `Loaded ${restored.length} clips. ⚠️ ${missing} clip${missing === 1 ? "" : "s"} can't be re-rendered (source video missing) — remove them before compiling.`
      : `Loaded ${restored.length} clips from "${data.label}". Edit and hit Compile to render a new version.`;
  }
}

refreshPastCompiles();

// =====================================================================
// Layout shell: empty state, tab switching, compilation badge
// =====================================================================

const emptyState = $("emptyState");
const appShell = $("appShell");
const mainTabs = $("mainTabs");
const tabContents = document.querySelectorAll("[data-tab-group]");
const tabResultBtn = $("tabResult");
const compileBadge = $("compileBadge");
const emptyDropBtn = $("emptyDropBtn");

function hasReadyTranscript() {
  return !!(currentJobId && Array.isArray(currentWords) && currentWords.length);
}

function requireReadyTranscript(actionLabel) {
  if (hasReadyTranscript()) return true;
  const meta = currentJobId ? (jobsById[currentJobId] || {}) : null;
  if (meta && meta.status === "error") {
    alert(
      (actionLabel || "That step") +
      " needs a finished transcript.\n\nThis video failed transcription — go to Ingest and use Re-drop video (or Retry)."
    );
    if (typeof setActiveTab === "function") setActiveTab("ingest");
    switchToJob(currentJobId, { force: true, tab: "ingest" });
    return false;
  }
  alert(
    (actionLabel || "That step") +
    " needs a transcribed video first.\n\nDrop a video on Ingest and wait until it shows ready (not error)."
  );
  if (typeof setActiveTab === "function") setActiveTab("ingest");
  return false;
}

function setActiveTab(tab) {
  // Tabs that only make sense after Whisper produced words.
  const needsTranscript = {
    transcript: "Edit words",
    highlights: "AI Shorts",
    branding: "Caption look",
    compilation: "Compilation",
    editor: "Timeline",
    result: "Result",
  };
  if (needsTranscript[tab] && !hasReadyTranscript()) {
    const wanted = needsTranscript[tab];
    tab = "ingest";
    const statusEl = $("statusText");
    const prog = $("progress");
    if (prog) prog.classList.remove("hidden");
    if (statusEl) {
      const errMeta = currentJobId ? (jobsById[currentJobId] || {}) : null;
      statusEl.textContent = (errMeta && errMeta.status === "error")
        ? `${wanted} is locked — this video failed transcription. Use Re-drop video below.`
        : `${wanted} unlocks after Whisper finishes. Drop a video on Ingest (wait for ready, not error).`;
    }
  }

  // Primary story steps: 1 Ingest → 2 Shorts → 3 Timeline.
  // transcript/branding are secondary panels (Edit words / Caption look).
  const stepMap = {
    ingest: "1",
    highlights: "2",
    compilation: "2",
    editor: "3",
    result: "3",
    transcript: "1",
    branding: "1",
  };
  const step = stepMap[tab];

  document.querySelectorAll(".main-tab").forEach(b => {
    const isSecondary = b.classList.contains("main-tab-secondary");
    const isMatch = b.dataset.tab === tab;
    b.classList.toggle("active", isMatch);
    // Reveal secondary nav chips only while that panel is open.
    if (isSecondary) {
      b.classList.toggle("hidden", !isMatch);
    }
  });

  document.body.classList.toggle("tab-editor-active", tab === "editor");
  if (typeof window.refreshMobileContextTools === "function") {
    try { window.refreshMobileContextTools(); } catch (e) { /* optional */ }
  }

  document.querySelectorAll(".step-badge").forEach(b => {
    b.classList.toggle("active", b.dataset.step === step);
  });

  document.querySelectorAll(".tab-content, [data-tab-group]").forEach(c => {
    if (c.dataset.tabGroup) {
      const isMatch = c.dataset.tabGroup === tab;
      c.classList.toggle("hidden", !isMatch);
      c.classList.toggle("active", isMatch);
      c.style.display = isMatch ? "block" : "none";
      if (isMatch) {
        // Ensure core shells inside the tab are visible. Leave ephemeral
        // panels (preview editor, filler banner, jobs empty) alone.
        ["editor", "transcriptEditor", "highlightsPanel", "compilePanel"].forEach((id) => {
          const el = c.querySelector("#" + id);
          if (el) el.classList.remove("hidden");
        });
      }
    }
  });

  if (tab === "editor" && typeof window.ensureTimelineInit === "function") {
    // Skip auto-open when Shorts/Compilation is about to seed a fresh project;
    // otherwise ensureInit races and reloads an older single-clip timeline.
    if (!window._tlDeferAutoOpen) {
      window.ensureTimelineInit();
    }
  }

  if (typeof updateReadyActions === "function") updateReadyActions();
}

// Used by timeline.js openTimelineEditor / other modules.
window.setActiveTab = setActiveTab;

function updateReadyActions() {
  const box = $("readyActions");
  if (!box) return;
  const ready = hasReadyTranscript();
  box.classList.toggle("hidden", !ready);
  const hint = $("readyActionsHint");
  const longBtn = $("readyOpenLongFormBtn");
  const words = (typeof currentWords !== "undefined" && currentWords) ||
    (jobsById[currentJobId] && jobsById[currentJobId].words) || [];
  const dur = typeof _jobDurationFromWords === "function" ? _jobDurationFromWords(words) : 0;
  const isLong = dur >= 240;
  if (longBtn) {
    longBtn.classList.toggle("hidden", !ready || !isLong);
    if (isLong) longBtn.classList.remove("btn-secondary");
  }
  if (hint && ready) {
    const name = (jobsById[currentJobId] && jobsById[currentJobId].filename) || "This video";
    hint.textContent = isLong
      ? `${name} looks long-form (${Math.round(dur / 60)} min). Prefer Edit as Long-form (16:9), or open Timeline / Find Shorts.`
      : `${name} is transcribed. Edit phrases in Timeline, set caption look, or find shorts.`;
  }
}

function openEditWords() {
  // Edit Words page retired as the primary surface — phrase editing lives in Timeline.
  openTimelineFromReady();
}

function openCaptionLook() {
  // Caption look + audio live in Timeline → Look now.
  if (typeof window.openTimelineLook === "function") {
    window.openTimelineLook();
    return;
  }
  if (typeof window.openTimelineEditor === "function" && currentJobId) {
    window.openTimelineEditor(currentJobId);
  } else if (typeof setActiveTab === "function") {
    setActiveTab("editor");
  }
  setTimeout(() => {
    if (typeof window.openTimelineLook === "function") window.openTimelineLook();
  }, 50);
}

function openFindShorts() {
  if (!requireReadyTranscript("AI Shorts")) return;
  setActiveTab("highlights");
}

function openTimelineFromReady() {
  if (!requireReadyTranscript("Timeline")) return;
  if (typeof window.openTimelineEditor === "function" && currentJobId) {
    window.openTimelineEditor(currentJobId);
  } else {
    setActiveTab("editor");
  }
}

function openLongFormFromReady() {
  if (!requireReadyTranscript("Long-form Timeline")) return;
  if (typeof window.openTimelineEditor === "function" && currentJobId) {
    window.openTimelineEditor(currentJobId, {
      longForm: true,
      canvas: "16x9",
      newProject: true,
      replace: true,
      label: "Long-form edit",
    });
  } else {
    setActiveTab("editor");
  }
}

window.openEditWords = openEditWords;
window.openCaptionLook = openCaptionLook;
window.openLongFormFromReady = openLongFormFromReady;

[
  ["readyCaptionLookBtn", openCaptionLook],
  ["readyFindShortsBtn", openFindShorts],
  ["readyOpenTimelineBtn", openTimelineFromReady],
  ["readyOpenLongFormBtn", openLongFormFromReady],
  ["readyAiEditFullBtn", openAiEditFullFromReady],
  ["hlAiEditFullBtn", openAiEditFullFromReady],
  ["transcriptBackIngestBtn", () => setActiveTab("ingest")],
  ["transcriptCaptionLookBtn", openCaptionLook],
  ["brandingBackIngestBtn", () => setActiveTab("ingest")],
].forEach(([id, fn]) => {
  const el = $(id);
  if (el) el.onclick = fn;
});

// Timeline toolbar Captions / Audio / Look (elements may appear after timeline.js mounts).
document.addEventListener("click", (e) => {
  const caps = e.target && e.target.closest && e.target.closest("#tlCaptionsBtn");
  const aud = e.target && e.target.closest && e.target.closest("#tlAudioBtn");
  const look = e.target && e.target.closest && e.target.closest("#tlCaptionLookBtn");
  if (!caps && !aud && !look) return;
  if (typeof window.setActiveTab === "function") window.setActiveTab("editor");
  if (aud && typeof window.jumpTimelineLook === "function") {
    window.jumpTimelineLook("audio");
    return;
  }
  if (typeof window.jumpTimelineLook === "function") {
    window.jumpTimelineLook("captions");
    return;
  }
  openCaptionLook();
});

if (mainTabs) {
  mainTabs.addEventListener("click", (e) => {
    const btn = e.target.closest(".main-tab");
    if (!btn || btn.classList.contains("hidden")) return;
    if (btn.dataset.tab) setActiveTab(btn.dataset.tab);
  });
}

const workflowSteps = document.getElementById("workflowSteps");
if (workflowSteps) {
  workflowSteps.addEventListener("click", (e) => {
    // The header row is built from .main-tab buttons (data-tab); an older build
    // used .step-badge (data-step). Support both so the top nav actually works.
    const tabBtn = e.target.closest(".main-tab");
    if (tabBtn && tabBtn.dataset.tab) { setActiveTab(tabBtn.dataset.tab); return; }
    const badge = e.target.closest(".step-badge");
    if (!badge || !badge.dataset.step) return;
    const stepToTab = {
      "1": "ingest",
      "2": "highlights",
      "3": "editor",
    };
    const tab = stepToTab[badge.dataset.step];
    if (tab) setActiveTab(tab);
  });
}

// Empty-state #emptyFile is wired by an early inline script in index.html
// (so the picker works even if something later in this file throws).
// It calls window.handleFiles — do not attach a second change listener here.

// Allow drag-and-drop anywhere on the page in the empty state.
document.addEventListener("dragover", (e) => {
  if (!emptyState || emptyState.classList.contains("hidden")) return;
  e.preventDefault();
});
document.addEventListener("drop", (e) => {
  if (!emptyState || emptyState.classList.contains("hidden")) return;
  if (e.dataTransfer && e.dataTransfer.files.length) {
    e.preventDefault();
    handleFiles(e.dataTransfer.files);
  }
});

// Reveal Result tab when a render completes; auto-switch when the user is on
// Transcript (caption burn) so output surfaces. Leave them alone on Shorts /
// Compilation / Timeline.
if (result && tabResultBtn) {
  const obs = new MutationObserver(() => {
    if (result.classList.contains("hidden")) return;
    tabResultBtn.classList.remove("hidden");
    const activeTabBtn = mainTabs && mainTabs.querySelector(".main-tab.active");
    const tab = activeTabBtn && activeTabBtn.dataset.tab;
    if (tab === "transcript" || tab === "branding") {
      setActiveTab("result");
    }
  });
  obs.observe(result, { attributes: true, attributeFilter: ["class"] });
}

// Ensure only Ingest is visible on first paint (other tabs ship with content
// that used to stack because they lacked `.hidden`).
setActiveTab("ingest");

// Initial render — hooks now live inline inside renderJobsList /
// renderCompileQueue / showEditor instead of via function-wrapping.
renderJobsList();
renderCompileQueue();

// =====================================================================
// Highlight preview-edit panel (lives below the source player on Edit tab)
// =====================================================================
//
// User clicks ▶ Preview on a Highlights card → switches to the Edit tab
// and opens this panel under the source video. The panel mirrors the card:
// editable Start/End (mm:ss.s), Add to compilation, Export clip — plus
// Replay and Save & back. Edits live-sync to the Highlights card so both
// representations stay aligned.

let _activePreview = null;       // { title, hookQuote, start, end, onUpdate }
let _previewStopHandler = null;  // timeupdate listener for auto-pause

function _peClearStopHandler() {
  if (_previewStopHandler && sourcePlayer) {
    sourcePlayer.removeEventListener("timeupdate", _previewStopHandler);
  }
  _previewStopHandler = null;
}

function _peGetTimes() {
  const s = _parseTime($("previewEditStart").value);
  const e = _parseTime($("previewEditEnd").value);
  return { s, e, valid: s !== null && e !== null && e > s };
}

function _peUpdateDur() {
  const dur = $("previewEditDur");
  if (!dur) return;
  const t = _peGetTimes();
  dur.textContent = t.valid ? (t.e - t.s).toFixed(1) + "s" : "—";
}

function _peSyncToCard() {
  // Live-propagate panel changes back to the Highlights card so both
  // representations stay aligned no matter where the user edits.
  if (!_activePreview || typeof _activePreview.onUpdate !== "function") return;
  const t = _peGetTimes();
  if (!t.valid) return;
  _activePreview.onUpdate(t.s, t.e);
}

function _peSetStatus(msg, ok = true) {
  const el = $("previewEditStatus");
  if (!el) return;
  el.textContent = msg || "";
  el.style.color = ok ? "#9aa0a6" : "#ff8a8a";
}

function _pePlay() {
  const target = sourcePlayer;
  if (!target || !target.src) return;
  const t = _peGetTimes();
  if (!t.valid) return;
  _peClearStopHandler();
  try {
    target.currentTime = t.s;
    target.play();
    _previewStopHandler = () => {
      if (target.currentTime >= t.e) {
        target.pause();
        _peClearStopHandler();
      }
    };
    target.addEventListener("timeupdate", _previewStopHandler);
    target.scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (err) { /* ignore play() rejections */ }
}

function openPreviewEditor(clip) {
  const panel = $("previewEditPanel");
  if (!panel) return;
  _activePreview = clip;
  $("previewEditTitle").textContent = clip.title || "Untitled clip";
  $("previewEditHook").textContent = clip.hookQuote ? `"${clip.hookQuote}"` : "";
  $("previewEditStart").value = _fmtTimeFine(clip.start);
  $("previewEditEnd").value = _fmtTimeFine(clip.end);
  _peSetStatus("");
  _peUpdateDur();
  panel.classList.remove("hidden");

  // Preview panel + #sourcePlayer live on the Transcript tab — not Shorts.
  setActiveTab("transcript");
  // Defer play+scroll until the tab switch's display change has applied.
  requestAnimationFrame(() => {
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    _pePlay();
  });
}

function closePreviewEditor() {
  _peClearStopHandler();
  _activePreview = null;
  const panel = $("previewEditPanel");
  if (panel) panel.classList.add("hidden");
}

// Inputs: live duration update + live sync back to the Highlights card.
const _peStart = $("previewEditStart");
const _peEnd = $("previewEditEnd");
function _peOnInput() {
  _peUpdateDur();
  _peSyncToCard();
}
if (_peStart) _peStart.oninput = _peOnInput;
if (_peEnd) _peEnd.oninput = _peOnInput;

// Buttons.
const _peReplay = $("previewEditReplay");
if (_peReplay) _peReplay.onclick = _pePlay;

const _peClose = $("previewEditClose");
if (_peClose) _peClose.onclick = closePreviewEditor;

const _peSave = $("previewEditSave");
if (_peSave) {
  _peSave.onclick = () => {
    const t = _peGetTimes();
    if (!t.valid) {
      _peSetStatus("Invalid times. End must be greater than start.", false);
      return;
    }
    _peSyncToCard();
    closePreviewEditor();
    setActiveTab("highlights");
  };
}

const _peAdd = $("previewEditAdd");
if (_peAdd) {
  _peAdd.onclick = () => {
    if (!_activePreview || !currentJobId) return;
    const t = _peGetTimes();
    if (!t.valid) {
      _peSetStatus("Invalid times. End must be greater than start.", false);
      return;
    }
    _peSyncToCard();
    addToCompileQueue({
      source_job_id: currentJobId,
      source_filename: (jobsById[currentJobId] && jobsById[currentJobId].filename) || "",
      start_time: t.s,
      end_time: t.e,
      hook_quote: _activePreview.hookQuote || "",
      title: _activePreview.title || "",
    });
    _peSetStatus(`✓ Added to compilation queue (${(t.e - t.s).toFixed(1)}s).`);
  };
}

const _peToTimeline = $("previewEditToTimeline");
if (_peToTimeline) {
  _peToTimeline.onclick = () => {
    if (!_activePreview || !currentJobId) return;
    const t = _peGetTimes();
    if (!t.valid) {
      _peSetStatus("Invalid times. End must be greater than start.", false);
      return;
    }
    _peSyncToCard();
    if (typeof window.openTimelineEditor !== "function") {
      alert("Timeline is still loading — try again in a second.");
      return;
    }
    window.openTimelineEditor(currentJobId, {
      in: t.s,
      out: t.e,
      newProject: true,
      replace: true,
    });
  };
}

const _peAiEdit = $("previewEditAiEdit");
if (_peAiEdit) {
  _peAiEdit.onclick = () => {
    if (!_activePreview || !currentJobId) return;
    const t = _peGetTimes();
    if (!t.valid) {
      _peSetStatus("Invalid times. End must be greater than start.", false);
      return;
    }
    _peSyncToCard();
    openAiEditPlan({
      source_job_id: currentJobId,
      start_time: t.s,
      end_time: t.e,
      label: (_activePreview && _activePreview.title) || "highlight",
    });
  };
}

const editToTimelineBtn = $("editToTimelineBtn");
if (editToTimelineBtn) {
  editToTimelineBtn.onclick = () => {
    if (!currentJobId) {
      alert("No active video. Transcribe a video first.");
      return;
    }
    if (typeof window.openTimelineEditor !== "function") {
      alert("Editor is still loading — try again in a second.");
      return;
    }
    window.openTimelineEditor(currentJobId);
  };
}

const aiEditJobBtn = $("aiEditJobBtn");
if (aiEditJobBtn) {
  aiEditJobBtn.onclick = () => {
    if (!currentJobId) {
      alert("No active video. Transcribe a video first.");
      return;
    }
    if (typeof window.openAiEditPlanForJob === "function") {
      window.openAiEditPlanForJob(currentJobId, {
        label: (jobsById[currentJobId] && jobsById[currentJobId].filename) || "AI Edit",
      });
    } else {
      alert("AI Edit is still loading — try again in a second.");
    }
  };
}

const _peMakeClip = $("previewEditMakeClip");
if (_peMakeClip) {
  _peMakeClip.onclick = async () => {
    if (!_activePreview || !currentJobId) return;
    const t = _peGetTimes();
    if (!t.valid) {
      _peSetStatus("Invalid times. End must be greater than start.", false);
      return;
    }
    _peSyncToCard();
    _peMakeClip.disabled = true;
    _peSetStatus("Exporting clip…");
    try {
      const style = await flushCaptionLookToJob();
      const res = await fetch("/clip-from-job", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_job_id: currentJobId,
          start_time: t.s,
          end_time: t.e,
          label: _activePreview.title || "highlight",
          style,
        }),
      });
      const j = await res.json();
      if (j.error) throw new Error(j.error);
      addJobToList(j.job_id);
      await refreshJobsList();
      closePreviewEditor();
      await switchToJob(j.job_id, { force: true, tab: "ingest" });
    } catch (err) {
      _peSetStatus("Could not export clip: " + err.message, false);
    } finally {
      _peMakeClip.disabled = false;
    }
  };
}

// =====================================================================
// Tighten silences (Style panel) — UI wiring
// =====================================================================

const _tEnabled = $("tightenEnabled");
const _tControls = $("tightenControls");
const _tMaxGap = $("tightenMaxGap");
const _tTargetGap = $("tightenTargetGap");
const _tMaxGapLbl = $("tightenMaxGapVal");
const _tTargetGapLbl = $("tightenTargetGapVal");
const _tPreviewBtn = $("tightenPreviewBtn");
const _tTasteBtn = $("tightenTasteBtn");
const _tPreviewSummary = $("tightenPreviewSummary");
const _tGapListEl = $("tightenGapList");
let _tLastGaps = [];
let _tTasteOn = false;

if (_tEnabled && _tControls) {
  _tEnabled.addEventListener("change", () => {
    _tControls.classList.toggle("hidden", !_tEnabled.checked);
    scheduleDraftSave();
  });
}
if (_tMaxGap && _tMaxGapLbl) {
  _tMaxGap.oninput = () => {
    _tMaxGapLbl.textContent = parseFloat(_tMaxGap.value).toFixed(1) + "s";
    scheduleDraftSave();
  };
}
if (_tTargetGap && _tTargetGapLbl) {
  _tTargetGap.oninput = () => {
    _tTargetGapLbl.textContent = parseFloat(_tTargetGap.value).toFixed(2) + "s";
    scheduleDraftSave();
  };
}

const _tCrossfade = $("tightenCrossfade");
if (_tCrossfade) {
  _tCrossfade.addEventListener("change", () => {
    if (typeof scheduleDraftSave === "function") scheduleDraftSave();
  });
}

function _tFmtTime(t) {
  const tt = Math.max(0, t || 0);
  const m = Math.floor(tt / 60);
  const s = (tt - m * 60).toFixed(1);
  return `${m}:${s.padStart(4, "0")}`;
}

function _tEscapeHTML(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

function _tGetPreservedSet() {
  const arr = (getStyle().tighten_silences || {}).preserved_gap_starts || [];
  return new Set(arr.map(x => Math.round(parseFloat(x) * 10) / 10));
}

function _tCommitPreservedSet(set) {
  // Round-trip through getStyle() / setter — we shadow into a global the
  // getStyle() reader honours, then schedule a draft save.
  _tPreservedCache = Array.from(set);
  if (typeof scheduleDraftSave === "function") scheduleDraftSave();
}

let _tPreservedCache = [];

function _tRenderSummary(stats) {
  if (!_tPreviewSummary) return;
  if (!stats || stats.gaps_total === 0) {
    _tPreviewSummary.style.color = "#9aa0a6";
    _tPreviewSummary.textContent =
      `No gaps longer than ${_tMaxGap.value}s found. Try lowering the threshold.`;
    return;
  }
  const cut = stats.total_cut || 0;
  const orig = stats.original_duration || 0;
  const newd = stats.new_duration || 0;
  const pct = orig > 0 ? Math.round((cut / orig) * 100) : 0;
  const preserved = stats.gaps_total - stats.gaps_cut;
  const tasteN = stats.taste_protected || 0;
  const preservedNote = preserved > 0 ? `, ${preserved} preserved` : "";
  const tasteNote = tasteN > 0 ? ` · ${tasteN} taste-protected` : "";
  _tPreviewSummary.style.color = "#7cd98a";
  _tPreviewSummary.textContent =
    `${stats.gaps_cut} cut${preservedNote}${tasteNote} → ${cut.toFixed(1)}s removed (${pct}% tighter). New length ≈ ${newd.toFixed(1)}s (was ${orig.toFixed(1)}s).`;
}

function _tRenderGapList(gaps) {
  if (!_tGapListEl) return;
  _tGapListEl.innerHTML = "";
  if (!gaps || !gaps.length) return;

  const helper = document.createElement("p");
  helper.className = "muted";
  helper.style.cssText = "font-size:.78rem;margin:0 0 6px";
  helper.textContent = "Each row is a gap that would be cut. Tick to PRESERVE the pause (e.g. a comedic beat). Click any row to seek the source player to that moment.";
  _tGapListEl.appendChild(helper);

  gaps.forEach(g => {
    const row = document.createElement("div");
    row.className = "tighten-gap-row" + (g.preserved ? " preserved" : "");

    const left = document.createElement("div");
    left.className = "tighten-gap-meta";
    const time = document.createElement("div");
    time.className = "tighten-gap-time";
    time.textContent = `${_tFmtTime(g.start)} · ${g.duration.toFixed(1)}s gap`;
    left.appendChild(time);
    const ctx = document.createElement("div");
    ctx.className = "tighten-gap-ctx";
    const before = (g.context_before || "").trim();
    const after = (g.context_after || "").trim();
    ctx.innerHTML = before
      ? `<span>“…${_tEscapeHTML(before)}”</span> <span class="tighten-gap-arrow">→</span> <span>“${_tEscapeHTML(after)}…”</span>`
      : `<span>“${_tEscapeHTML(after)}…”</span>`;
    left.appendChild(ctx);
    if (g.taste_reason || g.taste_sentiment) {
      const tasteEl = document.createElement("div");
      tasteEl.className = "muted";
      tasteEl.style.cssText = "font-size:.72rem;margin-top:2px";
      const tag = g.taste_sentiment ? `[${g.taste_sentiment}] ` : "";
      tasteEl.textContent = tag + (g.taste_reason || (g.preserved ? "Protected" : "Cut"));
      left.appendChild(tasteEl);
    }
    row.appendChild(left);

    const label = document.createElement("label");
    label.className = "tighten-gap-toggle";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!g.preserved;
    cb.dataset.gapStart = g.start.toFixed(1);
    cb.onchange = (e) => {
      e.stopPropagation();
      const set = _tGetPreservedSet();
      const key = Math.round(parseFloat(cb.dataset.gapStart) * 10) / 10;
      if (cb.checked) set.add(key); else set.delete(key);
      _tCommitPreservedSet(set);
      row.classList.toggle("preserved", cb.checked);
      _tFetchPreview(false);
    };
    label.appendChild(cb);
    const lblTxt = document.createElement("span");
    lblTxt.textContent = "Preserve";
    label.appendChild(lblTxt);
    row.appendChild(label);

    row.onclick = (e) => {
      if (e.target.closest("input,label")) return;
      if (sourcePlayer && sourcePlayer.src) {
        try {
          sourcePlayer.currentTime = Math.max(0, g.start - 0.5);
          sourcePlayer.play().catch(() => {});
          sourcePlayer.scrollIntoView({ behavior: "smooth", block: "center" });
        } catch (_) {}
      }
    };

    _tGapListEl.appendChild(row);
  });
}

async function _tFetchPreview(showLoading, opts) {
  opts = opts || {};
  const useTaste = opts.taste_protect === true || _tTasteOn;
  if (!currentJobId) {
    if (_tPreviewSummary) _tPreviewSummary.textContent = "Open a transcribed video first.";
    return;
  }
  if (showLoading && _tPreviewBtn) {
    _tPreviewBtn.disabled = true;
    if (_tTasteBtn) _tTasteBtn.disabled = true;
    if (_tPreviewSummary) {
      _tPreviewSummary.textContent = useTaste
        ? "Scanning gaps + scoring taste beats…"
        : "Scanning gaps…";
    }
  }
  try {
    const preserved = (getStyle().tighten_silences || {}).preserved_gap_starts || [];
    const res = await fetch("/preview-tightening", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: currentJobId,
        max_gap: parseFloat(_tMaxGap.value),
        target_gap: parseFloat(_tTargetGap.value),
        preserved_gap_starts: preserved,
        taste_protect: !!useTaste,
      }),
    });
    const j = await res.json();
    if (j.error) throw new Error(j.error);
    _tLastGaps = j.gaps || [];
    // Persist auto-protected starts so Enable on render keeps them.
    if (useTaste && Array.isArray(j.preserved_gap_starts)) {
      const set = new Set(
        (preserved || []).map((x) => Math.round(parseFloat(x) * 10) / 10)
      );
      j.preserved_gap_starts.forEach((t) => {
        set.add(Math.round(parseFloat(t) * 10) / 10);
      });
      _tCommitPreservedSet(set);
    }
    _tRenderSummary(j.stats);
    if (_tGapListEl && typeof _tRenderGapList === "function") {
      _tRenderGapList(_tLastGaps);
    }
    // Inline: re-render the transcript phrase list so gap markers appear
    // between phrases at their actual time positions.
    if (Array.isArray(currentWords) && currentWords.length) {
      renderPhraseList(currentWords);
    }
  } catch (e) {
    if (_tPreviewSummary) {
      _tPreviewSummary.style.color = "#ff8a8a";
      _tPreviewSummary.textContent = "Error: " + e.message;
    }
  } finally {
    if (showLoading && _tPreviewBtn) _tPreviewBtn.disabled = false;
    if (_tTasteBtn) _tTasteBtn.disabled = false;
  }
}

if (_tPreviewBtn) _tPreviewBtn.onclick = () => {
  _tTasteOn = false;
  _tFetchPreview(true, { taste_protect: false });
};
if (_tTasteBtn) {
  _tTasteBtn.onclick = () => {
    _tTasteOn = true;
    _tFetchPreview(true, { taste_protect: true });
  };
}

// Re-scan automatically if the user changes thresholds AFTER an initial scan.
function _tMaybeReScan() {
  if (Array.isArray(_tLastGaps) && _tLastGaps.length >= 0 &&
      _tPreviewSummary && _tPreviewSummary.textContent.trim() !== "") {
    _tFetchPreview(false, { taste_protect: _tTasteOn });
  }
}
if (_tMaxGap) _tMaxGap.addEventListener("change", _tMaybeReScan);
if (_tTargetGap) _tTargetGap.addEventListener("change", _tMaybeReScan);

// ===========================================================================
//  NEW FEATURES — Studio Logger, Bg Music, Speaker Colors, Clip Assembly
// ===========================================================================

// ---- StudioLogger initialisation ----
if (window.StudioLogger) {
  StudioLogger.init();
  StudioLogger.enableFetchLogging();
  // Attach to the source video player once the DOM is ready
  const _slVideo = $("sourcePlayer");
  if (_slVideo) StudioLogger.attachMediaElement(_slVideo);
}

// ---- Speaker Color Row: show when reframe analysis completes ----
(function() {
  const _scRow = $("speakerColorRow");
  if (!_scRow) return;
  // Watch for the reframe checkbox becoming enabled (analysis completed)
  const _reframeCheck = $("reframeEnabled");
  if (_reframeCheck) {
    const _scObserver = new MutationObserver(() => {
      if (!_reframeCheck.disabled) { _scRow.style.display = "flex"; }
    });
    _scObserver.observe(_reframeCheck, { attributes: true, attributeFilter: ["disabled"] });
  }
})();

// ---- Headline Banner: live WYSIWYG preview ----
(function() {
  const bannerInput = $("headlineBanner");
  const overlay = $("liveCaptionOverlay");
  if (!bannerInput || !overlay) return;
  let bannerDiv = document.createElement("div");
  bannerDiv.id = "headlineBannerOverlay";
  bannerDiv.style.cssText = "position:absolute;top:5%;left:50%;transform:translateX(-50%);background:rgba(0,0,0,0.65);color:#fff;font-weight:700;font-size:.78rem;padding:4px 14px;border-radius:14px;white-space:nowrap;display:none;z-index:20;backdrop-filter:blur(6px);letter-spacing:.02em";
  overlay.parentElement.appendChild(bannerDiv);
  bannerInput.addEventListener("input", () => {
    const txt = bannerInput.value.trim();
    if (txt) { bannerDiv.textContent = "📍 " + txt; bannerDiv.style.display = "block"; }
    else { bannerDiv.style.display = "none"; }
  });
})();

// ---- Background Music upload handler ----
window._bgMusicUploaded = false;
(function() {
  const fileInput = $("bgMusicFile");
  const uploadBtn = $("bgMusicUploadBtn");
  const statusEl = $("bgMusicStatus");
  const nameEl = $("bgMusicFileName");
  const volSlider = $("bgMusicVolume");
  const volVal = $("bgMusicVolVal");
  if (!fileInput || !uploadBtn) return;

  fileInput.addEventListener("change", () => {
    const f = fileInput.files[0];
    if (f) {
      uploadBtn.disabled = false;
      if (nameEl) nameEl.textContent = f.name;
      if (window.StudioLogger) StudioLogger.action("bgMusicFile", "selected", f.name);
    } else {
      uploadBtn.disabled = true;
      if (nameEl) nameEl.textContent = "";
    }
  });

  if (volSlider && volVal) {
    volSlider.addEventListener("input", () => { volVal.textContent = volSlider.value + " dB"; });
  }

  uploadBtn.addEventListener("click", async () => {
    if (!currentJobId) { alert("No active job. Upload a video first."); return; }
    const f = fileInput.files[0];
    if (!f) return;
    uploadBtn.disabled = true;
    uploadBtn.textContent = "Uploading…";
    if (statusEl) statusEl.textContent = "";
    try {
      const fd = new FormData();
      fd.append("job_id", currentJobId);
      fd.append("music", f);
      const res = await fetch("/upload-bg-music", { method: "POST", body: fd });
      const j = await res.json();
      if (j.error) throw new Error(j.error);
      window._bgMusicUploaded = true;
      uploadBtn.textContent = "✓ Uploaded";
      if (statusEl) statusEl.textContent = `Ready: ${j.path}`;
      if (window.StudioLogger) StudioLogger.action("bgMusicUpload", "success", j.path);
    } catch (e) {
      uploadBtn.textContent = "⬆ Upload music";
      uploadBtn.disabled = false;
      if (statusEl) { statusEl.textContent = "Error: " + e.message; statusEl.style.color = "#ff8a8a"; }
      if (window.StudioLogger) StudioLogger.error("bgMusicUpload", e, 0, "Music upload failed");
    }
  });
})();

// ---- Clip Assembly Bar (multi-segment editor) ----
const _clipAssembly = [];

function _addClipToAssembly(seg) {
  _clipAssembly.push({ ...seg, id: Date.now() + Math.random() });
  _renderAssemblyBar();
}

function _nudgeAssemblySeg(i, which, delta) {
  const seg = _clipAssembly[i];
  if (!seg) return;
  const minDur = 0.5;
  if (which === "start") {
    const next = Math.max(0, seg.start + delta);
    if (seg.end - next < minDur) return;
    seg.start = Math.round(next * 10) / 10;
  } else {
    const next = Math.max(seg.start + minDur, seg.end + delta);
    seg.end = Math.round(next * 10) / 10;
  }
  _renderAssemblyBar();
  if (window.StudioLogger) {
    StudioLogger.clip("segment_trimmed", `${String.fromCharCode(65 + i)} ${seg.start.toFixed(1)}–${seg.end.toFixed(1)}s`);
  }
}

function _renderAssemblyBar() {
  const bar = $("clipAssemblyBar");
  const track = $("clipSegmentTrack");
  const countEl = $("clipAssemblyCount");
  if (!bar || !track) return;

  if (_clipAssembly.length === 0) {
    bar.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");
  if (countEl) countEl.textContent = _clipAssembly.length + " segment" + (_clipAssembly.length !== 1 ? "s" : "");

  track.innerHTML = "";
  const segColors = ["#5e81ac", "#a3be8c", "#b48ead", "#d08770", "#88c0d0", "#ebcb8b"];
  const chipCss = "background:transparent;border:1px solid currentColor;border-radius:4px;color:inherit;font-size:.68rem;cursor:pointer;padding:1px 4px;line-height:1.2;opacity:0.85";
  _clipAssembly.forEach((seg, i) => {
    const block = document.createElement("div");
    const dur = (seg.end - seg.start).toFixed(1);
    const bg = segColors[i % segColors.length];
    block.style.cssText = `display:flex;align-items:center;gap:5px;padding:6px 8px;background:${bg}22;border:1px solid ${bg};border-radius:6px;font-size:.78rem;color:${bg};cursor:grab;user-select:none;white-space:nowrap;flex-shrink:0`;
    block.draggable = true;
    block.dataset.idx = i;

    // Drag-to-reorder
    block.addEventListener("dragstart", e => { e.dataTransfer.setData("text/plain", String(i)); block.style.opacity = "0.5"; });
    block.addEventListener("dragend", () => { block.style.opacity = "1"; });
    block.addEventListener("dragover", e => e.preventDefault());
    block.addEventListener("drop", e => {
      e.preventDefault();
      const fromIdx = parseInt(e.dataTransfer.getData("text/plain"), 10);
      if (isNaN(fromIdx) || fromIdx === i) return;
      const [moved] = _clipAssembly.splice(fromIdx, 1);
      _clipAssembly.splice(i, 0, moved);
      _renderAssemblyBar();
      if (window.StudioLogger) StudioLogger.clip("reordered", `Moved segment ${fromIdx} → ${i}`);
    });

    const mkTrim = (label, title, which, delta) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.title = title;
      b.style.cssText = chipCss;
      b.onclick = (e) => { e.stopPropagation(); _nudgeAssemblySeg(i, which, delta); };
      // Don't start a drag from trim chips.
      b.addEventListener("mousedown", (e) => e.stopPropagation());
      b.draggable = false;
      return b;
    };

    block.appendChild(mkTrim("−2s", "Start 2s earlier", "start", -2));
    block.appendChild(mkTrim("+2s", "Start 2s later", "start", 2));

    const label = document.createElement("span");
    label.innerHTML = `<strong>${String.fromCharCode(65 + i)}</strong> ${_fmtTimeFine(seg.start)}–${_fmtTimeFine(seg.end)} <span style="opacity:0.6">(${dur}s)</span>`;
    block.appendChild(label);

    block.appendChild(mkTrim("−2s", "End 2s earlier", "end", -2));
    block.appendChild(mkTrim("+2s", "End 2s later", "end", 2));

    // Remove button
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.textContent = "✕";
    removeBtn.title = "Remove this segment";
    removeBtn.style.cssText = "background:transparent;border:none;color:#bf616a;font-size:.82rem;cursor:pointer;padding:0 2px;line-height:1";
    removeBtn.onclick = (e) => {
      e.stopPropagation();
      _clipAssembly.splice(i, 1);
      _renderAssemblyBar();
      if (window.StudioLogger) StudioLogger.clip("segment_removed", `Segment ${String.fromCharCode(65 + i)} removed`);
    };
    block.appendChild(removeBtn);

    track.appendChild(block);
  });
}

// Clip Assembly: Preview sequence (virtual playlist) + Timeline handoff
(function() {
  const previewBtn = $("clipAssemblyPreview");
  const clearBtn = $("clipAssemblyClear");
  const exportBtn = $("clipAssemblyExport");
  const toTlBtn = $("clipAssemblyToTimeline");
  if (!previewBtn || !clearBtn) return;

  let _playingAssembly = false;
  let _assemblyRaf = null;

  previewBtn.addEventListener("click", () => {
    const video = $("sourcePlayer");
    if (!video || _clipAssembly.length === 0) return;
    if (_playingAssembly) {
      _playingAssembly = false;
      video.pause();
      if (_assemblyRaf) cancelAnimationFrame(_assemblyRaf);
      previewBtn.textContent = "▶ Preview sequence";
      return;
    }

    // Player lives on Transcript — switch there so preview is visible.
    setActiveTab("transcript");
    _playingAssembly = true;
    previewBtn.textContent = "⏸ Stop preview";
    let segIdx = 0;
    requestAnimationFrame(() => {
      video.currentTime = _clipAssembly[0].start;
      video.play().catch(() => {});
    });

    function tick() {
      if (!_playingAssembly) return;
      const seg = _clipAssembly[segIdx];
      if (!seg) { _playingAssembly = false; previewBtn.textContent = "▶ Preview sequence"; return; }
      if (video.currentTime >= seg.end) {
        segIdx++;
        if (segIdx >= _clipAssembly.length) {
          _playingAssembly = false;
          video.pause();
          previewBtn.textContent = "▶ Preview sequence";
          return;
        }
        video.currentTime = _clipAssembly[segIdx].start;
      }
      _assemblyRaf = requestAnimationFrame(tick);
    }
    tick();
    if (window.StudioLogger) StudioLogger.clip("assembly_preview", `${_clipAssembly.length} segments`);
  });

  clearBtn.addEventListener("click", () => {
    _clipAssembly.length = 0;
    _renderAssemblyBar();
    if (window.StudioLogger) StudioLogger.clip("assembly_cleared", "all segments removed");
  });

  if (toTlBtn) {
    toTlBtn.addEventListener("click", () => {
      if (_clipAssembly.length === 0) return;
      if (!currentJobId) { alert("No active job."); return; }
      if (typeof window.openTimelineEditor !== "function") {
        alert("Timeline Editor is not available.");
        return;
      }
      const clips = _clipAssembly.map((s) => ({
        source_job_id: currentJobId,
        start_time: s.start,
        end_time: s.end,
      }));
      window.openTimelineEditor(null, { clips, replace: true, newProject: true });
      if (window.StudioLogger) StudioLogger.clip("assembly_to_timeline", `${clips.length} segments`);
    });
  }

  if (exportBtn) {
    exportBtn.addEventListener("click", async () => {
      if (_clipAssembly.length === 0) return;
      if (!currentJobId) { alert("No active job."); return; }
      exportBtn.disabled = true;
      exportBtn.textContent = "Exporting…";
      try {
        const segments = _clipAssembly.map(s => ({ start_time: s.start, end_time: s.end, title: s.title }));
        const res = await fetch("/compile-clips", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            clips: segments.map(s => ({
              source_job_id: currentJobId,
              source_filename: (jobsById[currentJobId] && jobsById[currentJobId].filename) || "",
              start_time: s.start_time,
              end_time: s.end_time,
              title: s.title,
            })),
            label: "Assembled Short",
          }),
        });
        const j = await res.json();
        if (j.error) throw new Error(j.error);
        await refreshJobsList();
        await switchToJob(j.job_id);
        if (window.StudioLogger) StudioLogger.clip("assembly_exported", `job: ${j.job_id}`);
      } catch (e) {
        alert("Export failed: " + e.message);
        if (window.StudioLogger) StudioLogger.error("assembly_export", e, 0, "Export failed");
      } finally {
        exportBtn.disabled = false;
        exportBtn.textContent = "🚀 Export assembled short";
      }
    });
  }
})();

// ---- Smart Export Engine (Header + mobile Export) ----
(function() {
  let _exportBusy = false;

  function activeStudioTab() {
    const activeTabBtn = document.querySelector("#mainTabs .main-tab.active")
      || document.querySelector(".workflow-steps .main-tab.active")
      || document.querySelector(".main-tab.active");
    return activeTabBtn ? activeTabBtn.getAttribute("data-tab") : "ingest";
  }

  function showExportProgress(msg, opts) {
    opts = opts || {};
    // Timeline exports stay on the editor — switching to Ingest mid-render made
    // people download the wrong / older Instant Export MP4 (or audio remux).
    if (!opts.stayOnTab && typeof setActiveTab === "function") setActiveTab("ingest");
    if (progress) progress.classList.remove("hidden");
    if (result) result.classList.add("hidden");
    if (barFill) barFill.style.width = "8%";
    if (statusText) statusText.textContent = msg || "Exporting…";
    try {
      (progress || statusText)?.scrollIntoView?.({ behavior: "smooth", block: "center" });
    } catch (e) { /* ignore */ }
  }

  function showExportProgressUpdate(s) {
    if (!s) return;
    if (progress) progress.classList.remove("hidden");
    if (barFill) barFill.style.width = Math.max(8, Number(s.progress) || 10) + "%";
    if (statusText) {
      statusText.textContent = capitalize(s.status || "Rendering") + "… "
        + (s.progress != null ? (s.progress + "%") : "");
    }
  }

  function showDownloadReadyBanner(output, opts) {
    if (!output) return;
    opts = opts || {};
    const id = "studioDownloadBanner";
    let ban = document.getElementById(id);
    if (!ban) {
      ban = document.createElement("div");
      ban.id = id;
      ban.style.cssText =
        "position:fixed;left:12px;right:12px;bottom:72px;z-index:10050;" +
        "background:#161a24;border:1px solid #3b82f6;border-radius:12px;" +
        "padding:12px 14px;box-shadow:0 12px 40px rgba(0,0,0,.45);" +
        "display:flex;flex-wrap:wrap;gap:10px;align-items:center;" +
        "font-size:14px;color:#fff";
      document.body.appendChild(ban);
    }
    const url = "/download/" + encodeURIComponent(String(output).replace(/^\/+/, ""))
      + "?t=" + Date.now();
    const label = opts.timeline
      ? "Timeline export ready — full edited video (not Instant Export / audio-only)."
      : "Export ready — tap Download to save the MP4.";
    ban.innerHTML =
      `<span style="flex:1;min-width:140px;line-height:1.35">${label}</span>` +
      `<a class="btn btn-primary" href="${url}" download style="text-decoration:none;white-space:nowrap">⬇ Download MP4</a>` +
      `<button type="button" class="btn btn-secondary" data-dismiss style="white-space:nowrap">Dismiss</button>`;
    const dismiss = ban.querySelector("[data-dismiss]");
    if (dismiss) dismiss.onclick = () => ban.remove();
  }

  function triggerVideoDownload(output, opts) {
    if (!output) return;
    opts = opts || {};
    const name = String(output).split("/").pop() || "export.mp4";
    const url = "/download/" + encodeURIComponent(name) + "?t=" + Date.now();
    try {
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.rel = "noopener";
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      setTimeout(() => a.remove(), 500);
    } catch (e) { /* iOS may block programmatic click after async */ }
    showDownloadReadyBanner(output, opts);
  }
  window.triggerVideoDownload = triggerVideoDownload;
  window.showDownloadReadyBanner = showDownloadReadyBanner;

  function showExportDone(output, opts) {
    opts = opts || {};
    if (!output) return;
    const isMobile = !!(window.matchMedia && window.matchMedia("(max-width: 900px)").matches);
    // Desktop Instant Export lands on Ingest where the classic download link lives.
    // Timeline / mobile stay put so the user downloads the correct file.
    if (!opts.stayOnTab && !opts.timeline && !isMobile && typeof setActiveTab === "function") {
      setActiveTab("ingest");
    }
    if (barFill) barFill.style.width = "100%";
    if (progress) progress.classList.add("hidden");
    if (result) result.classList.remove("hidden");
    if (player) {
      player.src = "/preview/" + output + "?t=" + Date.now();
      try { player.load(); } catch (e) { /* ignore */ }
    }
    if (dl) {
      dl.href = "/download/" + output + "?t=" + Date.now();
      dl.setAttribute("download", String(output).split("/").pop() || "export.mp4");
    }
    if (renderBtn) renderBtn.disabled = false;
    if (statusText) {
      statusText.textContent = opts.timeline
        ? "Done — Timeline MP4 ready"
        : "Done — download ready";
    }
    try {
      result?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    } catch (e) { /* ignore */ }
    triggerVideoDownload(output, { timeline: !!opts.timeline });
  }

  window.showExportProgressUpdate = showExportProgressUpdate;
  window.showExportDone = showExportDone;

  async function renderCurrentJobDirect() {
    if (!currentJobId) throw new Error("No active video.");
    const edited = (typeof collectEditedWords === "function")
      ? collectEditedWords({ silent: true })
      : [];
    const words = (edited && edited.length) ? edited : (currentWords || []);
    if (!words.length) {
      throw new Error("No transcript words to burn. Wait until Ingest shows ready.");
    }
    currentWords = words;
    if (typeof flushCaptionLookToJob === "function") {
      try { await flushCaptionLookToJob(); } catch (e) { /* best-effort */ }
    }
    const style = typeof getStyle === "function" ? getStyle() : {};
    const audio = typeof getAudio === "function" ? getAudio() : {};
    const emojiRules = typeof getEmojiRules === "function" ? getEmojiRules() : [];
    showExportProgress("Sending Instant Export…");
    if (renderBtn) renderBtn.disabled = true;
    const res = await fetch("/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: currentJobId,
        words,
        style,
        audio,
        emoji_rules: emojiRules,
      }),
    });
    const job = await res.json();
    if (!res.ok || job.error) throw new Error(job.error || ("HTTP " + res.status));
    pollRender(job.job_id);
  }

  window.runInstantExport = async function runInstantExport(opts) {
    opts = opts || {};
    if (_exportBusy) return;
    _exportBusy = true;
    const headerBtn = $("headerExportBtn");
    const readyBtn = $("readyInstantExportBtn");
    const mobileBtn = $("mobileExportBtn");
    const prevHeader = headerBtn ? headerBtn.textContent : "";
    try {
      if (headerBtn) { headerBtn.disabled = true; headerBtn.textContent = "Exporting…"; }
      if (readyBtn) readyBtn.disabled = true;
      if (mobileBtn) mobileBtn.disabled = true;

      if (typeof hasReadyTranscript === "function" && !hasReadyTranscript()) {
        alert("Instant Export needs a transcribed video first.\n\nGo to Ingest, drop your video, and wait until it shows ready.");
        if (typeof setActiveTab === "function") setActiveTab("ingest");
        return;
      }

      if (window.StudioLogger) StudioLogger.action("instantExport", "click", activeStudioTab());

      if (typeof flushCaptionLookToJob === "function" && currentJobId) {
        try { await flushCaptionLookToJob(); } catch (e) { /* best-effort */ }
      }

      const tabName = activeStudioTab();

      // Prefer Timeline bake whenever a project has Main clips — even if the
      // user is on Caption look / Ingest. Instant Export of the source job is
      // captions-only and is NOT the edited Timeline.
      const hasMain = !!(window.timelineHasMainClips && window.timelineHasMainClips());
      if (!opts.forceJobRender && hasMain && typeof window.renderTimelineVideo === "function") {
        if (typeof setActiveTab === "function" && tabName !== "editor") {
          setActiveTab("editor");
        }
        showExportProgress("Rendering Timeline… (full edited video)", { stayOnTab: true });
        const s = await window.renderTimelineVideo();
        if (s && s.output) {
          showExportDone(s.output, {
            jobId: s.job_id || (window.tl && window.tl.job_id),
            stayOnTab: true,
            timeline: true,
          });
        }
        return;
      }

      if (tabName === "highlights") {
        const batchBtn = $("batchExportBtn");
        if (batchBtn && !batchBtn.disabled) {
          batchBtn.click();
          return;
        }
      }

      if ($("clipAssemblyBar") && !$("clipAssemblyBar").classList.contains("hidden")) {
        const expAss = $("clipAssemblyExport");
        if (expAss && !expAss.disabled) {
          expAss.click();
          return;
        }
      }

      // Default: burn current job captions → MP4 (no Timeline Main clips).
      await renderCurrentJobDirect();
    } catch (e) {
      alert("Instant Export failed: " + (e && e.message ? e.message : e));
      if (renderBtn) renderBtn.disabled = false;
      if (progress) progress.classList.add("hidden");
    } finally {
      _exportBusy = false;
      if (headerBtn) { headerBtn.disabled = false; headerBtn.textContent = prevHeader || "⚡ Instant Export MP4"; }
      if (readyBtn) readyBtn.disabled = false;
      if (mobileBtn) mobileBtn.disabled = false;
    }
  };

  const headerBtn = $("headerExportBtn");
  if (headerBtn) {
    headerBtn.addEventListener("click", () => window.runInstantExport());
  }
  const readyExport = $("readyInstantExportBtn");
  if (readyExport) {
    readyExport.addEventListener("click", () => window.runInstantExport({ forceJobRender: true }));
  }
})();

// ---- Batch Export All Shorts Handler ----
(function() {
  const batchBtn = $("batchExportBtn");
  if (!batchBtn) return;

  batchBtn.addEventListener("click", async () => {
    if (!currentJobId) { alert("No active job. Upload a video first."); return; }
    
    // Collect highlight cards from DOM or state
    const cards = document.querySelectorAll(".hl-card");
    if (!cards || cards.length === 0) {
      alert("No clip suggestions available yet. Click 'Find highlights' first!");
      return;
    }

    batchBtn.disabled = true;
    batchBtn.textContent = "⏳ Generating ZIP Batch…";

    try {
      const clipsToRender = [];
      cards.forEach(card => {
        const titleEl = card.querySelector(".hl-title strong");
        const inputs = card.querySelectorAll(".hl-time-edit input");
        let start = 0, end = 0;
        if (inputs.length >= 2) {
          start = _parseTime(inputs[0].value) || 0;
          end = _parseTime(inputs[1].value) || 0;
        }
        if (end > start) {
          clipsToRender.push({
            start_time: start,
            end_time: end,
            title: titleEl ? titleEl.textContent.trim() : "Viral Short",
            headline: $("headlineBanner") ? $("headlineBanner").value.trim() : "",
          });
        }
      });

      if (clipsToRender.length === 0) throw new Error("No valid clips found to export.");

      const style = await flushCaptionLookToJob();
      const res = await fetch("/batch-render-clips", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_job_id: currentJobId,
          clips: clipsToRender,
          style: style,
          format_zip: true,
        }),
      });

      const j = await res.json();
      if (j.error) throw new Error(j.error);

      // Trigger automatic browser download of ZIP
      if (j.download_url) {
        const a = document.createElement("a");
        a.href = j.download_url;
        a.download = j.zip_filename || "viral_shorts_batch.zip";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      }

      batchBtn.textContent = "✓ Batch Exported (ZIP)!";
      if (window.StudioLogger) StudioLogger.net("POST", "/batch-render-clips", 200, 0, clipsToRender.length);
      setTimeout(() => {
        batchBtn.disabled = false;
        batchBtn.textContent = "🚀 Batch Export All Shorts (ZIP / MP4)";
      }, 3000);
    } catch (e) {
      alert("Batch export failed: " + e.message);
      batchBtn.disabled = false;
      batchBtn.textContent = "🚀 Batch Export All Shorts (ZIP / MP4)";
      if (window.StudioLogger) StudioLogger.error("batchExport", e, 0, "Batch export failed");
    }
  });
})();

// ---- CapCut Viral Templates (rich packs from server CAPCUT_TEMPLATES) ----
const CAPCUT_TEMPLATES = (typeof window !== "undefined" && window.CAPCUT_TEMPLATES && Object.keys(window.CAPCUT_TEMPLATES).length)
  ? window.CAPCUT_TEMPLATES
  : {
      podcast_interview: {
        canvas: "9x16", font: "Montserrat Thin Black", size: 64,
        primary: "#FFFFFF", highlight: "#FFD60A", accent: "#00FF88", group: 2,
        headline: "Mind-Blowing Secret", viral_preset: "hormozi", speaker_colors: true,
        speaker_color_map: { SPEAKER_00: "#FFD700", SPEAKER_01: "#00E5FF" },
        reframe: { enabled: true, top_panel: "active", bottom_panel: "full" },
        punch_zoom: { enabled: false }, auto_overlays: false, broll_mode: "auto",
        broll_placement: "pip", broll_scope: "full",
      },
      capcut_reels: {
        canvas: "9x16", font: "Integral CF", size: 68,
        primary: "#FFFFFF", highlight: "#00F2EA", accent: "#FF0055", group: 1,
        headline: "", viral_preset: "mrbeast", speaker_colors: false,
        punch_zoom: { enabled: true, intensity: "med" }, auto_overlays: true,
        broll_mode: "auto", broll_placement: "pip", broll_scope: "full",
      },
      product_spotlight: {
        canvas: "9x16", font: "Anton", size: 72,
        primary: "#00FF88", highlight: "#FF00FF", accent: "#00CFFF", group: 3,
        headline: "📍 Featured Product", viral_preset: "neon", speaker_colors: false,
        punch_zoom: { enabled: true, intensity: "high" }, color_grade: "warm",
        auto_overlays: true, broll_mode: "auto", broll_placement: "center",
        broll_scope: "selected",
      },
      cinematic_vlog: {
        canvas: "16x9", font: "DM Sans", size: 56,
        primary: "#F8FAFC", highlight: "#6366F1", accent: "#EC4899", group: 4,
        headline: "", viral_preset: "karaoke", speaker_colors: false,
        punch_zoom: { enabled: false },
        ken_burns: { enabled: true, direction: "in", intensity: "low" },
        color_grade: "cinematic", auto_overlays: false, broll_mode: "auto",
        broll_placement: "center", broll_scope: "playhead",
      },
      capcut_always: {
        label: "Always · Photo Match",
        ai_edit_pack: "always",
        canvas: "9x16", font: "Montserrat Thin Black", size: 62,
        primary: "#FFFFFF", highlight: "#FFE566", accent: "#FF6B9A", group: 2,
        headline: "", viral_preset: "hormozi", speaker_colors: false,
        punch_zoom: { enabled: false },
        ken_burns: { enabled: true, direction: "in", intensity: "med" },
        color_grade: "warm", auto_overlays: true, photo_match: true,
        use_ai_photos: false, broll_mode: "photo", broll_placement: "center",
        broll_scope: "full",
        brand_kit: { apply_logo: true, apply_colors: true, caption_preset: "hormozi" },
      },
    };

window._activeCapcutKey = window._activeCapcutKey || "podcast_interview";

function _setSelectOptionValue(sel, value) {
  if (!sel || value == null) return;
  const font = String(value);
  if ([...sel.options].some((o) => o.value === font)) {
    sel.value = font;
    return;
  }
  const soft = [...sel.options].find((o) =>
    o.value.toLowerCase().includes(font.split(" ")[0].toLowerCase())
  );
  if (soft) sel.value = soft.value;
}

function applyCapcutTemplateToUi(tKey, opts) {
  opts = opts || {};
  const t = CAPCUT_TEMPLATES[tKey];
  if (!t) return null;
  window._activeCapcutKey = tKey;
  window._pendingCapcutTemplate = t;

  // Map CapCut-style edit pack → AI Edit recipe (storytelling lives there).
  const packMap = {
    podcast_interview: "clarity",
    capcut_reels: "pulse",
    product_spotlight: "velocity",
    cinematic_vlog: "film",
    capcut_always: "always",
  };
  const aiPack = t.ai_edit_pack || packMap[tKey] || "pulse";
  t.ai_edit_pack = aiPack;
  if (typeof _aiEditPackId !== "undefined") {
    try { _aiEditPackId = aiPack; } catch (e) { /* optional */ }
  }
  window._preferredAiEditPack = aiPack;

  if ($("font")) _setSelectOptionValue($("font"), t.font);
  if ($("group") && t.group != null) {
    $("group").value = t.group;
    if ($("groupVal")) $("groupVal").textContent = t.group;
  }
  if ($("size") && t.size != null) {
    $("size").value = t.size;
    if ($("sizeVal")) $("sizeVal").textContent = t.size;
  }
  if ($("headlineBanner") && t.headline != null) $("headlineBanner").value = t.headline;
  if ($("speakerColorsEnabled")) $("speakerColorsEnabled").checked = !!t.speaker_colors;
  if (t.speaker_color_map && typeof syncSpeakerColorPickers === "function") {
    try {
      syncSpeakerColorPickers(Object.keys(t.speaker_color_map));
      Object.entries(t.speaker_color_map).forEach(([id, color]) => {
        const inp = document.querySelector(`[data-speaker-color="${id}"]`);
        if (inp && color) inp.value = color;
      });
    } catch (e) { /* optional */ }
  }

  if (t.punch_zoom) {
    if ($("punchZoomEnabled")) $("punchZoomEnabled").checked = !!t.punch_zoom.enabled;
    if ($("punchZoomIntensity") && t.punch_zoom.intensity) {
      $("punchZoomIntensity").value = t.punch_zoom.intensity;
    }
  } else if ($("punchZoomEnabled")) {
    $("punchZoomEnabled").checked = false;
  }

  if (t.reframe) {
    if ($("reframeEnabled")) $("reframeEnabled").checked = !!t.reframe.enabled;
    if ($("reframeTopSelect") && t.reframe.top_panel) $("reframeTopSelect").value = t.reframe.top_panel;
    if ($("reframeBottomSelect") && t.reframe.bottom_panel) $("reframeBottomSelect").value = t.reframe.bottom_panel;
  }

  const preset = t.viral_preset || ({
    podcast_interview: "hormozi",
    capcut_reels: "mrbeast",
    product_spotlight: "neon",
    cinematic_vlog: "karaoke",
    capcut_always: "hormozi",
  })[tKey];
  const presetBtn = preset
    ? document.querySelector(`#viralPresets .theme[data-preset="${preset}"]`)
    : null;
  if (presetBtn && !opts.skipPresetClick) {
    presetBtn.click();
  } else {
    if ($("primary") && t.primary) $("primary").value = t.primary;
    if ($("highlight") && t.highlight) $("highlight").value = t.highlight;
    if ($("accent") && t.accent) $("accent").value = t.accent;
  }

  // Re-apply pack-specific colors/fonts after viral preset may overwrite them.
  if ($("primary") && t.primary) $("primary").value = t.primary;
  if ($("highlight") && t.highlight) $("highlight").value = t.highlight;
  if ($("accent") && t.accent) $("accent").value = t.accent;
  if ($("font")) _setSelectOptionValue($("font"), t.font);
  if ($("group") && t.group != null) {
    $("group").value = t.group;
    if ($("groupVal")) $("groupVal").textContent = t.group;
  }

  const hint = $("capcutTemplateHint");
  if (hint) {
    if (tKey === "capcut_always" || t.photo_match) {
      hint.innerHTML = "<strong>" + (t.label || tKey) + "</strong> → AI Edit pack <strong>"
        + aiPack + "</strong>. "
        + "Matches stills to spoken keywords (stock or Gemini AI photos) with Ken Burns. "
        + "Run <strong>Find highlights</strong>, then <strong>AI Edit…</strong>.";
    } else {
      hint.innerHTML = "<strong>" + (t.label || tKey) + "</strong> → AI Edit pack <strong>"
        + aiPack + "</strong>. "
        + "Find highlights, then <strong>AI Edit…</strong> on a card for cuts/zooms. "
        + "Caption Look stays for fonts/colors/brand only.";
    }
  }

  if (typeof scheduleDraftSave === "function") scheduleDraftSave();
  if (typeof updateFontPreview === "function") updateFontPreview();
  return t;
}

window.applyCapcutTemplateToUi = applyCapcutTemplateToUi;

const capcutTemplateEl = document.getElementById("capcutTemplate");
if (capcutTemplateEl) {
  capcutTemplateEl.addEventListener("change", () => {
    applyCapcutTemplateToUi(capcutTemplateEl.value);
  });
}

// ---- Custom Brand Preset Saver ----
window.saveCustomBrandPreset = function(name = "custom") {
  try {
    const style = typeof getStyle === "function" ? getStyle() : {};
    localStorage.setItem("_customBrandPreset", JSON.stringify(style));
    console.log("Custom brand preset saved:", name);
    alert("Brand preset saved successfully!");
  } catch (e) {
    console.error("Failed to save preset", e);
    alert("Failed to save brand preset.");
  }
};

const saveBrandPresetBtn = $("saveBrandPresetBtn");
if (saveBrandPresetBtn) {
  saveBrandPresetBtn.onclick = () => window.saveCustomBrandPreset("custom");
}
const loadBrandPresetBtn = $("loadBrandPresetBtn");
if (loadBrandPresetBtn) {
  loadBrandPresetBtn.onclick = () => window.loadCustomBrandPreset();
}

// Brand logo (Branding tab) — upload once, apply with Apply → Timeline.
window._brandLogoAssetId = window._brandLogoAssetId || null;

(function wireBrandLogo() {
  const input = $("brandLogoInput");
  const clearBtn = $("brandLogoClearBtn");
  const status = $("brandLogoStatus");
  const preview = $("brandLogoPreview");
  if (!input) return;

  const setUi = (assetId, note) => {
    window._brandLogoAssetId = assetId || null;
    if (status) status.textContent = note || (assetId ? `Logo ready (${assetId.slice(0, 8)}…)` : "Optional — applied when you hit Apply → Timeline");
    if (preview) {
      if (assetId) {
        preview.src = "/asset/" + assetId + "?t=" + Date.now();
        preview.style.display = "inline-block";
      } else {
        preview.removeAttribute("src");
        preview.style.display = "none";
      }
    }
  };

  input.addEventListener("change", async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    if (status) status.textContent = "Uploading logo…";
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch("/upload-asset", { method: "POST", body: fd });
      const j = await res.json();
      if (!res.ok || j.error) throw new Error(j.error || res.statusText);
      setUi(j.asset_id, "Logo uploaded — Apply → Timeline to place it");
      if (window.StudioLogger) StudioLogger.clip("brand_logo_upload", j.asset_id);
    } catch (e) {
      setUi(null, "Logo upload failed: " + e.message);
      alert("Logo upload failed: " + e.message);
    } finally {
      input.value = "";
    }
  });

  if (clearBtn) {
    clearBtn.onclick = () => setUi(null, "Logo cleared");
  }
})();

async function applyBrandingToTimeline() {
  // Caption Look → Timeline: look/brand only (not storytelling cuts).
  const style = typeof flushCaptionLookToJob === "function"
    ? await flushCaptionLookToJob()
    : (typeof captionLookStyle === "function" ? captionLookStyle() : getStyle());
  if (typeof window.ensureTimelineInit === "function") {
    await window.ensureTimelineInit();
  }
  if (typeof window.openTimelineEditor === "function" && currentJobId) {
    const needOpen = !(window.timelineHasMainClips && window.timelineHasMainClips());
    if (needOpen) {
      await window.openTimelineEditor(currentJobId);
    }
  }
  if (typeof window.applyTimelineBranding !== "function") {
    alert("Timeline Editor is not available.");
    return;
  }
  const opts = {};
  if (window._brandLogoAssetId) {
    opts.logo = {
      asset_id: window._brandLogoAssetId,
      x: 0.04, y: 0.04, w: 0.18, opacity: 0.9,
    };
  }
  window.applyTimelineBranding(style, opts);
  setActiveTab("editor");
  if (window.StudioLogger) {
    StudioLogger.clip("branding_to_timeline", "look only" + (opts.logo ? " + logo" : ""));
  }
}

const applyBrandingToTimelineBtn = $("applyBrandingToTimelineBtn");
if (applyBrandingToTimelineBtn) {
  applyBrandingToTimelineBtn.onclick = () => {
    applyBrandingToTimeline().catch((e) => alert("Could not apply branding: " + e.message));
  };
}

window.loadCustomBrandPreset = function() {
  try {
    const data = localStorage.getItem("_customBrandPreset");
    if (!data) {
      alert("No custom brand preset found.");
      return;
    }
    const style = JSON.parse(data);
    
    if (style.font_name && document.getElementById("font")) document.getElementById("font").value = style.font_name;
    if (style.font_size && document.getElementById("size")) { document.getElementById("size").value = style.font_size; if (document.getElementById("sizeVal")) document.getElementById("sizeVal").textContent = style.font_size; }
    if (style.primary_color && document.getElementById("primary")) document.getElementById("primary").value = style.primary_color;
    if (style.highlight_color && document.getElementById("highlight")) document.getElementById("highlight").value = style.highlight_color;
    if (style.accent_color && document.getElementById("accent")) document.getElementById("accent").value = style.accent_color;
    if (style.outline_color && document.getElementById("outlineColor")) document.getElementById("outlineColor").value = style.outline_color;
    if (style.outline_width !== undefined && document.getElementById("outlineWidth")) { document.getElementById("outlineWidth").value = style.outline_width; if (document.getElementById("owVal")) document.getElementById("owVal").textContent = style.outline_width; }
    
    if (style.headline_banner !== undefined && document.getElementById("headlineBanner")) document.getElementById("headlineBanner").value = style.headline_banner;
    
    if (style.speaker_colors && Object.keys(style.speaker_colors).length > 0 && document.getElementById("speakerColorsEnabled")) {
      document.getElementById("speakerColorsEnabled").checked = true;
      syncSpeakerColorPickers(Object.keys(style.speaker_colors));
      Object.entries(style.speaker_colors).forEach(([id, color]) => {
        const inp = document.querySelector(`[data-speaker-color="${id}"]`);
        if (inp && color) inp.value = color;
      });
    }
    
    if (typeof scheduleDraftSave === "function") scheduleDraftSave();
    if (typeof updateFontPreview === "function") updateFontPreview();
    
    alert("Brand preset loaded successfully!");
  } catch (e) {
    console.error("Failed to load preset", e);
    alert("Failed to load brand preset.");
  }
};

// ---- Auto-Fetch B-Roll & Overlays (legacy branding button, if present) ----
const autoFetchOverlaysBtn = document.getElementById("autoFetchOverlaysBtn");
if (autoFetchOverlaysBtn) {
  autoFetchOverlaysBtn.addEventListener("click", async () => {
    try {
      autoFetchOverlaysBtn.disabled = true;
      autoFetchOverlaysBtn.textContent = "Fetching...";
      if (typeof window.ensureTimelineInit === "function") {
        await window.ensureTimelineInit();
      }
      if (typeof window.addOverlayClip !== "function") {
        throw new Error("Timeline overlays are not available yet.");
      }
      const body = {
        job_id: currentJobId || undefined,
        words: (typeof currentWords !== "undefined" && currentWords.length) ? currentWords : undefined,
        budget: 5,
        mode: "auto",
        placement: "pip",
      };
      const res = await fetch("/fetch-auto-overlays", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      const list = data.overlays || [];
      for (const overlay of list) {
        await window.addOverlayClip(overlay);
      }
      if (typeof setActiveTab === "function") setActiveTab("editor");
      alert(list.length
        ? `Added ${list.length} keyword overlay${list.length === 1 ? "" : "s"} to the Timeline.`
        : "No keyword moments found to overlay.");
    } catch (e) {
      alert("Failed to fetch overlays: " + e.message);
    } finally {
      autoFetchOverlaysBtn.disabled = false;
      autoFetchOverlaysBtn.textContent = "Auto-Fetch B-Roll & Overlays";
    }
  });
}

