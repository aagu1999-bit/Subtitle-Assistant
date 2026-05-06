// ---- Color themes ----
const THEMES = [
  { name: "Classic",   primary: "#FFFFFF", highlight: "#FFD60A", outline: "#000000" },
  { name: "TikTok",    primary: "#FFFFFF", highlight: "#00F2EA", outline: "#000000" },
  { name: "Fire",      primary: "#FFFFFF", highlight: "#FF4D4D", outline: "#1a0000" },
  { name: "Neon",      primary: "#00FF88", highlight: "#FF00FF", outline: "#000000" },
  { name: "Mint",      primary: "#FFFFFF", highlight: "#7CFFB2", outline: "#0a1f14" },
  { name: "Sunset",    primary: "#FFE4B5", highlight: "#FF6B35", outline: "#2a0f00" },
  { name: "Mono",      primary: "#FFFFFF", highlight: "#AAAAAA", outline: "#000000" },
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
const go = $("go"), progress = $("progress"), barFill = $("barFill"), statusText = $("statusText");
const result = $("result"), player = $("player"), dl = $("dl");
const editor = $("editor"), rowCount = $("rowCount");
const renderBtn = $("renderBtn"), reEditBtn = $("reEditBtn");
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
const audioCheckboxes = ["noiseReduction", "voiceBoost", "loudnessNorm", "voiceClarity"].map($);

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
    scheduleDraftSave();
  };
  themesDiv.appendChild(b);
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

["font", "primary", "highlight", "outlineColor", "allCaps", "shadow"].forEach(id => {
  const el = $(id);
  if (!el) return;
  el.addEventListener("input", scheduleDraftSave);
  el.addEventListener("change", scheduleDraftSave);
});

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
  if (videos.length === 1) {
    // Preserve the existing single-file path for the "Transcribe" button.
    currentFile = videos[0];
    fn.textContent = videos[0].name + "  (" + (videos[0].size / 1048576).toFixed(1) + " MB)";
    go.disabled = false;
  } else {
    // Multi-file mode: kick all of them off immediately, no need for the button.
    currentFile = null;
    fn.textContent = `Queueing ${videos.length} videos…`;
    go.disabled = true;
    videos.forEach(f => uploadAndTranscribe(f, getPreCleanFlag()));
  }
}

function getPreCleanFlag() {
  return $("preCleanForTranscribe") && $("preCleanForTranscribe").checked;
}

// ---- Helpers: collect style / audio ----
function getStyle() {
  return {
    font_name:       $("font").value,
    font_size:       parseInt(sizeEl.value, 10),
    primary_color:   primaryEl.value,
    highlight_color: highlightEl.value,
    outline_color:   outlineEl.value,
    outline_width:   parseInt(owEl.value, 10),
    shadow:          $("shadow").checked ? 1 : 0,
    position_y:      parseInt(posEl.value, 10),
    all_caps:        $("allCaps").checked,
    group_size:      parseInt(groupEl.value, 10),
    quality_boost:   $("qualityBoost") ? $("qualityBoost").checked : false,
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
  if (style.outline_color) outlineEl.value = style.outline_color;
  if (style.outline_width !== undefined) owEl.value = style.outline_width;
  if (style.position_y !== undefined) posEl.value = style.position_y;
  if (style.all_caps !== undefined) $("allCaps").checked = !!style.all_caps;
  if (style.shadow !== undefined) $("shadow").checked = !!style.shadow;
  if (style.group_size) groupEl.value = style.group_size;
  if (style.quality_boost !== undefined && $("qualityBoost")) {
    $("qualityBoost").checked = !!style.quality_boost;
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

function collectEditedWords(options = {}) {
  const rows = phraseListEl.querySelectorAll(".phrase-row");
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
    currentWords = s.words || [];
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
  if (!words || !words.length) return;

  const groupSize = parseInt(groupEl.value, 10) || 3;
  for (let i = 0; i < words.length; i += groupSize) {
    const group = words.slice(i, i + groupSize);
    if (!group.length) continue;

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

    // Editable text
    const textEl = document.createElement("span");
    textEl.className = "phrase-text";
    textEl.contentEditable = "true";
    textEl.spellcheck = false;
    textEl.textContent = group.map(w => w.word).join(" ");
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
}

function updateRowCount() {
  const n = phraseListEl.querySelectorAll(".phrase-row").length;
  rowCount.textContent = n + " phrase" + (n !== 1 ? "s" : "");
}

function highlightPhraseRow(target) {
  phraseListEl.querySelectorAll(".phrase-row").forEach(r => r.classList.remove("active"));
  if (target) target.classList.add("active");
}

// Sync phrase list highlight as the source video plays
sourcePlayer.addEventListener("timeupdate", () => {
  const t = sourcePlayer.currentTime;
  const rows = Array.from(phraseListEl.querySelectorAll(".phrase-row"));
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
});

function fmtTime(sec) {
  const m = Math.floor(sec / 60);
  const s = (sec % 60).toFixed(1).padStart(4, "0");
  return `${m}:${s}`;
}

function showEditor(words, saved = {}) {
  if (saved.style) applyStyle(saved.style);
  if (saved.audio) applyAudio(saved.audio);
  if (saved.emoji_rules) applyEmojiRules(saved.emoji_rules);

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
    if (s.words && s.words.length) {
      currentWords = s.words;
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
