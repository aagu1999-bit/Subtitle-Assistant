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
  el.addEventListener("input", scheduleDraftSave);
  el.addEventListener("change", scheduleDraftSave);
});

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
drop.onclick = () => fileInput.click();
["dragenter", "dragover"].forEach(ev =>
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("hover"); }));
["dragleave", "drop"].forEach(ev =>
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("hover"); }));
drop.addEventListener("drop", e => {
  if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
});
fileInput.onchange = () => { if (fileInput.files.length) handleFiles(fileInput.files); };

function handleFiles(files) {
  const videos = Array.from(files).filter(f => f.type.startsWith("video/"));
  if (!videos.length) { alert("Please select video files."); return; }

  // In the empty state the Transcribe button isn't visible (it's in the
  // sidebar which is hidden). Auto-kick everything immediately and activate
  // the first one so the user lands directly in the editor for it.
  const isEmpty = (typeof _loadJobIds === "function") && _loadJobIds().length === 0;

  if (videos.length === 1 && !isEmpty) {
    // Existing behaviour: stage the file, let user click Transcribe.
    currentFile = videos[0];
    fn.textContent = videos[0].name + "  (" + (videos[0].size / 1048576).toFixed(1) + " MB)";
    go.disabled = false;
  } else {
    currentFile = null;
    if (fn) {
      fn.textContent = videos.length === 1
        ? `Queueing ${videos[0].name}…`
        : `Queueing ${videos.length} videos…`;
    }
    if (go) go.disabled = true;
    videos.forEach((f, idx) => uploadAndTranscribe(f, getPreCleanFlag(), idx === 0));
  }
}

function getPreCleanFlag() {
  return $("preCleanForTranscribe") && $("preCleanForTranscribe").checked;
}

// ---- Helpers: collect style / audio ----
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
    speaker_colors: speakerColorsEnabled ? {
      SPEAKER_00: $("hostColor") ? $("hostColor").value : "#FFD700",
      SPEAKER_01: $("guestColor") ? $("guestColor").value : "#00E5FF",
    } : {},
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
    row.remove();
    scheduleDraftSave();
  };

  row.appendChild(kwInput);
  row.appendChild(emojiInput);
  row.appendChild(removeBtn);
  emojiRulesList.appendChild(row);

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
        editedWords.push({ word, start: origTimes[i].s, end: origTimes[i].e });
      });
    } else {
      const duration = Math.max(0, end - start);
      const wordDur  = duration / words.length;
      words.forEach((word, i) => {
        editedWords.push({
          word,
          start: start + i * wordDur,
          end:   start + (i + 1) * wordDur,
        });
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
      addEmojiRule(p.keyword, p.emoji);
      scheduleDraftSave();
    }
  };
  emojiPresetsDiv.appendChild(btn);
});

addRuleBtn.onclick = () => addEmojiRule();

// ---- Phase 1: Transcribe ----
async function uploadAndTranscribe(file, preClean, makeActive = false) {
  const fd = new FormData();
  fd.append("video", file);
  if (preClean) fd.append("pre_clean", "true");

  try {
    const res = await fetch("/transcribe-only", { method: "POST", body: fd });
    const job = await res.json();
    if (job.error) throw new Error(job.error);

    addJobToList(job.job_id);
    if (makeActive) {
      currentJobId = job.job_id;
      result.classList.add("hidden");
      editor.classList.add("hidden");
      progress.classList.remove("hidden");
      barFill.style.width = "5%";
      statusText.textContent = "Uploading…";
      pollTranscription(job.job_id);
    }
    refreshJobsList();
    return job.job_id;
  } catch (e) {
    if (makeActive) {
      showError("Upload failed: " + e.message);
      go.disabled = false;
    } else {
      console.error("Upload failed for", file.name, e);
    }
    return null;
  }
}

go.onclick = async () => {
  if (!currentFile) return;
  go.disabled = true;
  await uploadAndTranscribe(currentFile, getPreCleanFlag(), true);
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

  barFill.style.width = (s.progress || 10) + "%";

  if (s.status === "awaiting_edit") {
    barFill.style.width = "100%";
    statusText.textContent = "Transcription complete!";
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
    showError("Transcription error: " + s.error);
    go.disabled = false;
    return;
  }

  statusText.textContent = capitalize(s.status) + "…";
  setTimeout(() => pollTranscription(jobId), 2000);
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
    row.dataset.words = JSON.stringify(group.map(w => ({ s: w.start, e: w.end })));

    // Timestamp label
    const timeEl = document.createElement("span");
    timeEl.className = "phrase-time";
    timeEl.textContent = fmtTime(group[0].start);
    timeEl.title = "Seek to " + fmtTime(group[0].start);

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
      return safe;
    }).join(" ");
    textEl.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); textEl.blur(); }
    });
    textEl.addEventListener("input", scheduleDraftSave);

    // Delete row button
    const delBtn = document.createElement("button");
    delBtn.className = "phrase-del";
    delBtn.textContent = "×";
    delBtn.title = "Remove this phrase";
    delBtn.onclick = (e) => {
      e.stopPropagation();
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
// re-render, and persist. Pure caption edit — the underlying audio still
// says "um", we just no longer caption it. If the user wants the audio
// trimmed too, that's a follow-up (silence-tightening per-word).
const fillerCleanBtn = $("fillerCleanBtn");
if (fillerCleanBtn) {
  fillerCleanBtn.onclick = () => {
    if (!currentWords || !currentWords.length) return;
    const flagged = _detectFillerIndices(currentWords);
    if (flagged.size === 0) return;
    if (!confirm(
      `Remove ${flagged.size} filler word${flagged.size === 1 ? "" : "s"} from captions? ` +
      `The audio in the video stays untouched; this just drops them from the burned subtitles.`
    )) return;
    currentWords = currentWords.filter((_, i) => !flagged.has(i));
    renderPhraseList(currentWords);
    updateRowCount();
    if (typeof scheduleDraftSave === "function") scheduleDraftSave();
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
  textEl.textContent = g.preserved
    ? `⏸ Pause kept (${_gapRange})`
    : `✂ Cutting silence (${_gapRange})`;

  const label = document.createElement("label");
  label.className = "gap-toggle";
  label.title = "Tick to PRESERVE this pause";
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
      ? `⏸ Pause kept (${_gapRange})`
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

function showEditor(words, saved = {}) {
  if (retranscribeBtn) retranscribeBtn.disabled = false;
  if (saved.style) applyStyle(saved.style);
  if (saved.audio) applyAudio(saved.audio);
  if (saved.emoji_rules) applyEmojiRules(saved.emoji_rules);

  // Reveal the Result tab if this job already has a finished render.
  if (saved && saved.output) {
    const resultBtn = document.getElementById("tabResult");
    if (resultBtn) resultBtn.classList.remove("hidden");
  }

  // Load the original uploaded video into the source player
  sourcePlayer.src = "/raw-upload/" + currentJobId;

  // Populate the editable subtitle list
  renderPhraseList(words);
  updateRowCount();

  // Show audio preview panel if any enhancement is enabled
  updateAudioPreviewVisibility();

  editor.classList.remove("hidden");
  editor.scrollIntoView({ behavior: "smooth", block: "start" });
  localStorage.setItem("subtitleBurner:lastJobId", currentJobId);
}

// ---- Phase 2: Render ----
renderBtn.onclick = async () => {
  const editedWords = collectEditedWords();

  if (!editedWords.length) {
    return;
  }

  const emojiRules = getEmojiRules();
  currentWords = editedWords;
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
        words: editedWords,
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
      showError("Job is no longer available on the server. Please re-upload.");
      renderBtn.disabled = false;
      return;
    }
    s = await res.json();
  } catch (e) {
    setTimeout(() => pollRender(jobId), 3000);
    return;
  }

  // Drop UI updates if user switched jobs mid-render.
  if (currentJobId && currentJobId !== jobId) return;

  barFill.style.width = (s.progress || 10) + "%";
  statusText.textContent = capitalize(s.status) + "…";

  if (s.status === "done") {
    barFill.style.width = "100%";
    progress.classList.add("hidden");
    result.classList.remove("hidden");
    player.src = "/preview/" + s.output;
    dl.href = "/download/" + s.output;
    renderBtn.disabled = false;
    result.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }

  if (s.status === "error") {
    showError("Render error: " + s.error);
    renderBtn.disabled = false;
    return;
  }

  setTimeout(() => pollRender(jobId), 2000);
}

// "Edit transcript again" — scroll back up to editor
reEditBtn.onclick = () => {
  result.classList.add("hidden");
  editor.scrollIntoView({ behavior: "smooth", block: "start" });
};

// "Re-transcribe" — discard current words and re-run Whisper on the video.
// Useful when timestamps have drifted from the actual frames (typically when
// the job's video file is itself the output of a previous render/clip op).
if (retranscribeBtn) {
  retranscribeBtn.onclick = async () => {
    if (!currentJobId) return;
    const ok = confirm(
      "Re-transcribe this video? Your current transcript edits will be discarded and replaced with a fresh Whisper pass. This usually takes 20–60 seconds."
    );
    if (!ok) return;
    retranscribeBtn.disabled = true;
    try {
      const res = await fetch("/retranscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: currentJobId }),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      // Swap the editor for the progress UI and let pollTranscription drive
      // the rest. When it completes, showEditor() repopulates the new words.
      editor.classList.add("hidden");
      result.classList.add("hidden");
      progress.classList.remove("hidden");
      barFill.style.width = "30%";
      statusText.textContent = "Re-transcribing…";
      pollTranscription(currentJobId);
    } catch (e) {
      alert("Re-transcribe failed: " + e.message);
      retranscribeBtn.disabled = false;
    }
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

async function refreshReframeStatus() {
  if (!currentJobId || !reframeStatus) return;
  try {
    const res = await fetch(`/reframe-status/${currentJobId}`);
    const data = await res.json();
    if (data.ready && data.stats) {
      reframeStatus.textContent =
        `✓ ${data.stats.speaker_count} speakers, ${data.stats.face_samples} face samples`;
      if (reframeEnabled) {
        reframeEnabled.disabled = false;
        reframeEnabled.checked = true; // Auto-check when analysis is ready!
      }
      if (reframeAnalyzeBtn) reframeAnalyzeBtn.textContent = "Re-analyze";
      refreshReframePreview();
      refreshSpeakerAvatars();
    } else {
      reframeStatus.textContent = "Not analysed yet";
      if (reframeEnabled) {
        reframeEnabled.disabled = true;
        reframeEnabled.checked = false;
      }
      if (reframeAnalyzeBtn) reframeAnalyzeBtn.textContent = "Analyze speakers + faces";
      const box = $("reframePreviewBox");
      if (box) box.style.display = "none";
      const spkBox = $("reframeSpeakerBox");
      if (spkBox) spkBox.style.display = "none";
    }
  } catch { /* offline — silent */ }
}

if (reframeAnalyzeBtn) {
  reframeAnalyzeBtn.onclick = async () => {
    if (!currentJobId) { alert("Open a transcribed video first."); return; }
    reframeAnalyzeBtn.disabled = true;
    if (reframeSwapBtn) reframeSwapBtn.style.display = "none";
    reframeStatus.textContent = "Starting…";
    try {
      const res = await fetch("/analyze-reframe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: currentJobId }),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      reframeStatus.textContent = "Analysing… (1–3 min for 1 minute of video)";
      if (_reframePollTimer) clearInterval(_reframePollTimer);
      _reframePollTimer = setInterval(async () => {
        const r = await fetch(`/reframe-status/${currentJobId}`).then(r => r.json());
        if (r.ready) {
          clearInterval(_reframePollTimer);
          _reframePollTimer = null;
          refreshReframeStatus();
          reframeAnalyzeBtn.disabled = false;
          if (reframeEnabled) {
            reframeEnabled.disabled = false;
            reframeEnabled.checked = true;
          }
        } else if (r.error) {
          // Worker died — stop polling and tell the user what went wrong.
          clearInterval(_reframePollTimer);
          _reframePollTimer = null;
          reframeStatus.textContent = `❌ ${r.error}`;
          reframeStatus.style.color = "#ff8a8a";
          reframeAnalyzeBtn.disabled = false;
        }
      }, 3000);
    } catch (e) {
      reframeStatus.textContent = "Error: " + e.message;
      reframeAnalyzeBtn.disabled = false;
    }
  };
}

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

function showError(msg) {
  progress.classList.add("hidden");
  statusText.textContent = msg;
  progress.classList.remove("hidden");
  barFill.style.width = "0%";
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
  const emptyEl = document.getElementById("emptyState");
  const shellEl = document.getElementById("appShell");
  if (emptyEl) emptyEl.classList.toggle("hidden", ids.length > 0);
  if (shellEl) shellEl.classList.toggle("hidden", ids.length === 0);
  if (!ids.length) {
    jobsPanel.classList.add("hidden");
    return;
  }
  jobsPanel.classList.remove("hidden");
  jobsCountEl.textContent = ids.length === 1 ? "1 video" : `${ids.length} videos`;
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
      toTl.className = "job-rename";
      toTl.textContent = "🎬";
      toTl.title = "Edit in timeline (open the multi-track editor with this clip)";
      toTl.onclick = (e) => {
        e.stopPropagation();
        // timeline.js loads after app.js, so guard at click time, not render.
        if (typeof window.openTimelineEditor === "function") window.openTimelineEditor(jobId);
        else alert("Editor is still loading — try again in a second.");
      };
      div.appendChild(toTl);
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

async function switchToJob(jobId) {
  if (currentJobId === jobId) return;
  if (saveDraftNow) await saveDraftNow();  // persist current job's edits first
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
      showEditor(currentWords, s);
      if (s.output && s.status === "done") {
        result.classList.remove("hidden");
        player.src = "/preview/" + s.output;
        dl.href = "/download/" + s.output;
      } else {
        result.classList.add("hidden");
      }
    } else {
      // Still transcribing — show the progress UI for this job
      editor.classList.add("hidden");
      result.classList.add("hidden");
      progress.classList.remove("hidden");
      barFill.style.width = (s.progress || 10) + "%";
      statusText.textContent = capitalize(s.status || "loading") + "…";
      pollTranscription(jobId);
    }
    renderJobsList();
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
  // Auto-restore the most recently active job into the editor (if it's
  // still in a usable state). Skip errored jobs and abandoned uploads.
  const lastId = localStorage.getItem("subtitleBurner:lastJobId");
  if (lastId && jobsById[lastId]) {
    const meta = jobsById[lastId];
    if (meta.status !== "error" && (meta.status === "done" || meta.video_available !== false)) {
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

    // ---- Action buttons ----
    const actions = document.createElement("div");
    actions.className = "hl-actions";

    const previewBtn = document.createElement("button");
    previewBtn.textContent = "▶ Preview";
    previewBtn.onclick = () => {
      // Open the preview-edit panel on the Edit tab. onUpdate is called on
      // every input change in the panel (live sync back to this card) plus
      // on Save / Make-a-clip / Add-to-compilation actions.
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

    const addSegBtn = document.createElement("button");
    addSegBtn.textContent = "🔗 Add to assembly";
    addSegBtn.title = "Add this clip as a segment in the assembly bar";
    addSegBtn.onclick = () => {
      if (editedEnd <= editedStart) { alert("End time must be greater than start time."); return; }
      _addClipToAssembly({ title: editedTitle, start: editedStart, end: editedEnd, quote: editedQuote });
      addSegBtn.textContent = "✓ Added";
      addSegBtn.disabled = true;
      setTimeout(() => { addSegBtn.disabled = false; addSegBtn.textContent = "🔗 Add to assembly"; }, 1500);
      if (window.StudioLogger) StudioLogger.clip("segment_added", `"${editedTitle}" ${editedStart.toFixed(1)}-${editedEnd.toFixed(1)}s`);
    };
    actions.appendChild(addSegBtn);

    const addBtn = document.createElement("button");
    addBtn.textContent = "+ Add to compilation";
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
        addBtn.textContent = "+ Add to compilation";
      }, 1500);
    };
    actions.appendChild(addBtn);

    const makeBtn = document.createElement("button");
    makeBtn.className = "primary";
    makeBtn.textContent = "Make a clip";
    makeBtn.onclick = async () => {
      if (editedEnd <= editedStart) {
        alert("End time must be greater than start time.");
        return;
      }
      makeBtn.disabled = true;
      makeBtn.textContent = "Trimming…";
      try {
        const res = await fetch("/clip-from-job", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            source_job_id: currentJobId,
            start_time: editedStart,
            end_time: editedEnd,
            label: editedTitle || "highlight",
          }),
        });
        const j = await res.json();
        if (j.error) throw new Error(j.error);
        addJobToList(j.job_id);
        await refreshJobsList();
        await switchToJob(j.job_id);
      } catch (e) {
        alert("Could not create clip: " + e.message);
        makeBtn.disabled = false;
        makeBtn.textContent = "Make a clip";
      }
    };
    actions.appendChild(makeBtn);

    const tlBtn = document.createElement("button");
    tlBtn.textContent = "🎬 Open in Timeline";
    tlBtn.title = "Open the timeline editor with this highlight as a clip";
    tlBtn.onclick = () => {
      if (editedEnd <= editedStart) {
        alert("End time must be greater than start time.");
        return;
      }
      if (typeof window.openTimelineEditor === "function") {
        window.openTimelineEditor(currentJobId, { in: editedStart, out: editedEnd });
      } else {
        alert("Timeline Editor is not available.");
      }
    };
    actions.appendChild(tlBtn);

    card.appendChild(actions);
    hlResults.appendChild(card);
  });
}

// Ranges already shown to the user — fed back as avoid_ranges so "More
// options" returns a different batch instead of the same top picks.
let _hlAvoidRanges = [];
const hlMoreBtn = $("hlMoreBtn");

async function _runFindHighlights({ avoid }) {
  if (!currentJobId) {
    alert("No active job. Transcribe a video first.");
    return;
  }
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
    const data = await res.json();
    if (data.error) throw new Error(data.error);
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

  compileListEl.innerHTML = "";
  if (!q.length) {
    const hint = document.createElement("p");
    hint.className = "muted";
    hint.style.cssText = "text-align:center;padding:24px 12px;font-size:.9rem";
    hint.innerHTML = "Nothing queued yet. Open the <strong>✨ Highlights</strong> tab on any video and click <strong>+ Add to compilation</strong> on the suggestions you want to stitch together.";
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
  setActiveTab("edit");
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
      compileStatus.textContent = `✓ Compiled ${j.segments} clip${j.segments === 1 ? "" : "s"} into a new job.`;
      // Clear the queue and switch to the new compilation job.
      saveCompileQueue([]);
      renderCompileQueue();
      compileLabelEl.value = "";
      addJobToList(j.job_id);
      await refreshJobsList();
      refreshPastCompiles();
      await switchToJob(j.job_id);
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
    openBtn.onclick = async () => { await switchToJob(c.job_id); };
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

function setActiveTab(tab) {
  const stepMap = {
    ingest: "1",
    transcript: "2",
    highlights: "3",
    branding: "4",
    editor: "5"
  };
  const step = stepMap[tab];

  document.querySelectorAll(".main-tab").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === tab);
  });

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
        // Ensure inner panels & editor views within the matching tab are visible
        c.querySelectorAll(".panel, section, #editor, #transcriptEditor, #highlightsPanel").forEach(p => {
          if (p.id !== "jobsPanel") p.classList.remove("hidden");
        });
      }
    }
  });

  if (tab === "editor" && window.ensureTimelineInit) {
    window.ensureTimelineInit();
  }
}

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
    const badge = e.target.closest(".step-badge");
    if (!badge || !badge.dataset.step) return;
    const stepToTab = {
      "1": "ingest",
      "2": "transcript",
      "3": "highlights",
      "4": "branding",
      "5": "editor"
    };
    const tab = stepToTab[badge.dataset.step];
    if (tab) setActiveTab(tab);
  });
}

// Trigger the file input from the empty-state hero button.
if (emptyDropBtn) {
  emptyDropBtn.onclick = () => fileInput.click();
}

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

// Reveal Result tab when a render completes; only auto-switch to it when
// the user is currently on Edit (so render output gets surfaced) — leave
// them alone if they're on Highlights or Compilation.
if (result && tabResultBtn) {
  const obs = new MutationObserver(() => {
    if (result.classList.contains("hidden")) return;
    tabResultBtn.classList.remove("hidden");
    const activeTabBtn = mainTabs && mainTabs.querySelector(".main-tab.active");
    if (activeTabBtn && activeTabBtn.dataset.tab === "edit") {
      setActiveTab("result");
    }
  });
  obs.observe(result, { attributes: true, attributeFilter: ["class"] });
}

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
// editable Start/End (mm:ss.s), Add to compilation, Make a clip — plus
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

  setActiveTab("edit");
  // Defer play+scroll until the tab switch's display change has applied.
  requestAnimationFrame(() => _pePlay());
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
    _peSetStatus("Trimming clip…");
    try {
      const res = await fetch("/clip-from-job", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_job_id: currentJobId,
          start_time: t.s,
          end_time: t.e,
          label: _activePreview.title || "highlight",
        }),
      });
      const j = await res.json();
      if (j.error) throw new Error(j.error);
      addJobToList(j.job_id);
      await refreshJobsList();
      closePreviewEditor();
      await switchToJob(j.job_id);
    } catch (err) {
      _peSetStatus("Could not create clip: " + err.message, false);
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
const _tPreviewSummary = $("tightenPreviewSummary");
const _tGapListEl = $("tightenGapList");
let _tLastGaps = [];

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
  const preservedNote = preserved > 0 ? `, ${preserved} preserved` : "";
  _tPreviewSummary.style.color = "#7cd98a";
  _tPreviewSummary.textContent =
    `${stats.gaps_cut} cut${preservedNote} → ${cut.toFixed(1)}s removed (${pct}% tighter). New length ≈ ${newd.toFixed(1)}s (was ${orig.toFixed(1)}s).`;
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

async function _tFetchPreview(showLoading) {
  if (!currentJobId) {
    if (_tPreviewSummary) _tPreviewSummary.textContent = "Open a transcribed video first.";
    return;
  }
  if (showLoading && _tPreviewBtn) {
    _tPreviewBtn.disabled = true;
    if (_tPreviewSummary) _tPreviewSummary.textContent = "Scanning gaps…";
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
      }),
    });
    const j = await res.json();
    if (j.error) throw new Error(j.error);
    _tLastGaps = j.gaps || [];
    _tRenderSummary(j.stats);
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
  }
}

if (_tPreviewBtn) _tPreviewBtn.onclick = () => _tFetchPreview(true);

// Re-scan automatically if the user changes thresholds AFTER an initial scan.
function _tMaybeReScan() {
  if (Array.isArray(_tLastGaps) && _tLastGaps.length >= 0 &&
      _tPreviewSummary && _tPreviewSummary.textContent.trim() !== "") {
    _tFetchPreview(false);
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
  _clipAssembly.forEach((seg, i) => {
    const block = document.createElement("div");
    const dur = (seg.end - seg.start).toFixed(1);
    const bg = segColors[i % segColors.length];
    block.style.cssText = `display:flex;align-items:center;gap:6px;padding:6px 10px;background:${bg}22;border:1px solid ${bg};border-radius:6px;font-size:.78rem;color:${bg};cursor:grab;user-select:none;white-space:nowrap;flex-shrink:0`;
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

    const label = document.createElement("span");
    label.innerHTML = `<strong>${String.fromCharCode(65 + i)}</strong> ${_fmtTimeFine(seg.start)}–${_fmtTimeFine(seg.end)} <span style="opacity:0.6">(${dur}s)</span>`;
    block.appendChild(label);

    // Remove button
    const removeBtn = document.createElement("button");
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

// Clip Assembly: Preview sequence (virtual playlist)
(function() {
  const previewBtn = $("clipAssemblyPreview");
  const clearBtn = $("clipAssemblyClear");
  const exportBtn = $("clipAssemblyExport");
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

    _playingAssembly = true;
    previewBtn.textContent = "⏸ Stop preview";
    let segIdx = 0;
    video.currentTime = _clipAssembly[0].start;
    video.play();

    function tick() {
      if (!_playingAssembly) return;
      const seg = _clipAssembly[segIdx];
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

// ---- Smart Export Engine (Header Controller) ----
(function() {
  const headerBtn = $("headerExportBtn");
  if (!headerBtn) return;

  headerBtn.addEventListener("click", () => {
    // Determine active tab
    const activeTabBtn = document.querySelector(".main-tab.active");
    const tabName = activeTabBtn ? activeTabBtn.getAttribute("data-tab") : "edit";

    if (window.StudioLogger) StudioLogger.action("headerExportBtn", "click", `tab:${tabName}`);

    if (tabName === "highlights") {
      const batchBtn = $("batchExportBtn");
      if (batchBtn) { batchBtn.click(); return; }
    }
    if (tabName === "editor") {
      const tlBtn = $("tlRenderBtn");
      if (tlBtn) { tlBtn.click(); return; }
    }
    if ($("clipAssemblyBar") && !$("clipAssemblyBar").classList.contains("hidden")) {
      const expAss = $("clipAssemblyExport");
      if (expAss) { expAss.click(); return; }
    }

    // Default to main Edit Studio render
    const rBtn = $("renderBtn");
    const gBtn = $("go");
    if (rBtn && !rBtn.disabled && rBtn.offsetParent !== null) {
      rBtn.click();
    } else if (gBtn && !gBtn.disabled) {
      gBtn.click();
    } else {
      alert("No active video or edit to render. Upload a video to begin!");
    }
  });
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

      const style = getStyle();
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

// ---- CapCut Viral Templates ----
const CAPCUT_TEMPLATES = {
  podcast_interview: { font: "Montserrat Thin Black", size: 64, primary: "#FFFFFF", highlight: "#FFD60A", accent: "#00FF88", group: 2, headline: "Mind-Blowing Secret", speakerColors: true },
  capcut_reels: { font: "Integral CF", size: 68, primary: "#FFFFFF", highlight: "#00F2EA", accent: "#FF0055", group: 1, headline: "", speakerColors: false, punch_zoom: { enabled: true, intensity: "med" } },
  product_spotlight: { font: "Bebas Neue", size: 72, primary: "#00FF88", highlight: "#FF00FF", accent: "#00CFFF", group: 3, headline: "Must Have Product!", speakerColors: false },
  cinematic_vlog: { font: "DM Sans", size: 56, primary: "#F8FAFC", highlight: "#6366F1", accent: "#EC4899", group: 4, headline: "", speakerColors: false },
};

const capcutTemplateEl = document.getElementById("capcutTemplate");
if (capcutTemplateEl) {
  capcutTemplateEl.addEventListener("change", () => {
    const tKey = capcutTemplateEl.value;
    const t = CAPCUT_TEMPLATES[tKey];
    if (!t) return;
    
    if (document.getElementById("font")) document.getElementById("font").value = t.font;
    if (document.getElementById("group")) { document.getElementById("group").value = t.group; if (document.getElementById("groupVal")) document.getElementById("groupVal").textContent = t.group; }
    if (document.getElementById("headlineBanner")) document.getElementById("headlineBanner").value = t.headline;
    if (document.getElementById("speakerColorsEnabled")) document.getElementById("speakerColorsEnabled").checked = t.speakerColors;
    
    if (t.punch_zoom) {
      if (document.getElementById("punchZoomEnabled")) document.getElementById("punchZoomEnabled").checked = t.punch_zoom.enabled;
      if (document.getElementById("punchZoomIntensity")) document.getElementById("punchZoomIntensity").value = t.punch_zoom.intensity;
    } else {
      if (document.getElementById("punchZoomEnabled")) document.getElementById("punchZoomEnabled").checked = false;
    }
    
    const presetMap = {
      podcast_interview: "hormozi",
      capcut_reels: "mrbeast",
      product_spotlight: "neon",
      cinematic_vlog: "karaoke"
    };
    const presetBtn = document.querySelector(`#viralPresets .theme[data-preset="${presetMap[tKey]}"]`);
    if (presetBtn) presetBtn.click();
    else {
      if (document.getElementById("size")) document.getElementById("size").value = t.size;
      if (document.getElementById("primary")) document.getElementById("primary").value = t.primary;
      if (document.getElementById("highlight")) document.getElementById("highlight").value = t.highlight;
      if (document.getElementById("accent")) document.getElementById("accent").value = t.accent;
      if (typeof scheduleDraftSave === "function") scheduleDraftSave();
      if (typeof updateFontPreview === "function") updateFontPreview();
    }
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
      if (style.speaker_colors.SPEAKER_00 && document.getElementById("hostColor")) document.getElementById("hostColor").value = style.speaker_colors.SPEAKER_00;
      if (style.speaker_colors.SPEAKER_01 && document.getElementById("guestColor")) document.getElementById("guestColor").value = style.speaker_colors.SPEAKER_01;
    }
    
    if (typeof scheduleDraftSave === "function") scheduleDraftSave();
    if (typeof updateFontPreview === "function") updateFontPreview();
    
    alert("Brand preset loaded successfully!");
  } catch (e) {
    console.error("Failed to load preset", e);
    alert("Failed to load brand preset.");
  }
};

// ---- Auto-Fetch B-Roll & Overlays Button Handler ----
const autoFetchOverlaysBtn = document.getElementById("autoFetchOverlaysBtn");
if (autoFetchOverlaysBtn) {
  autoFetchOverlaysBtn.addEventListener("click", async () => {
    try {
      autoFetchOverlaysBtn.disabled = true;
      autoFetchOverlaysBtn.textContent = "Fetching...";
      
      let wordsToUse = [];
      if (typeof currentWords !== "undefined" && currentWords.length > 0) {
        wordsToUse = currentWords;
      }
      
      const res = await fetch("/fetch-auto-overlays", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ words: wordsToUse }),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      
      if (data.overlays && Array.isArray(data.overlays)) {
        if (typeof window.addOverlayClip === "function") {
          data.overlays.forEach(overlay => window.addOverlayClip(overlay));
        } else if (typeof window.populateOverlaysList === "function") {
          window.populateOverlaysList(data.overlays);
        } else {
          console.log("Fetched overlays:", data.overlays);
          alert("Overlays fetched but no handler found to display them. Check console.");
        }
      }
    } catch (e) {
      alert("Failed to fetch overlays: " + e.message);
    } finally {
      autoFetchOverlaysBtn.disabled = false;
      autoFetchOverlaysBtn.textContent = "Auto-Fetch B-Roll & Overlays";
    }
  });
}

