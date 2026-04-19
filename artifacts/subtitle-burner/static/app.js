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
const editor = $("editor"), wordChips = $("wordChips"), wordCount = $("wordCount");
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

// ---- Audio toggle visibility ----
// The audio preview panel lives inside the editor section (available after transcription).
const audioCheckboxes = ["noiseReduction", "loudnessNorm", "voiceClarity"].map($);

function updateAudioPreviewVisibility() {
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
  $("loudnessNorm").checked = true;
  $("voiceClarity").checked = true;
  updateAudioPreviewVisibility();
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
  };
  themesDiv.appendChild(b);
});

// ---- Live labels ----
sizeEl.oninput  = () => sizeVal.textContent = sizeEl.value;
owEl.oninput    = () => owVal.textContent = owEl.value;
posEl.oninput   = () => posVal.textContent = posEl.value + "%";
groupEl.oninput = () => { groupVal.textContent = groupEl.value; renderPhraseList(currentWords); };

// ---- Drag & drop ----
drop.onclick = () => fileInput.click();
["dragenter", "dragover"].forEach(ev =>
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("hover"); }));
["dragleave", "drop"].forEach(ev =>
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("hover"); }));
drop.addEventListener("drop", e => {
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});
fileInput.onchange = () => { if (fileInput.files[0]) handleFile(fileInput.files[0]); };

function handleFile(f) {
  if (!f.type.startsWith("video/")) { alert("Please select a video file."); return; }
  currentFile = f;
  fn.textContent = f.name + "  (" + (f.size / 1048576).toFixed(1) + " MB)";
  go.disabled = false;
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
  };
}

function getAudio() {
  return {
    noise_reduction: $("noiseReduction").checked,
    loudness_norm:   $("loudnessNorm").checked,
    voice_clarity:   $("voiceClarity").checked,
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
  removeBtn.onclick = () => row.remove();

  row.appendChild(kwInput);
  row.appendChild(emojiInput);
  row.appendChild(removeBtn);
  emojiRulesList.appendChild(row);

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
    }
  };
  emojiPresetsDiv.appendChild(btn);
});

addRuleBtn.onclick = () => addEmojiRule();

// ---- Phase 1: Transcribe ----
go.onclick = async () => {
  if (!currentFile) return;

  result.classList.add("hidden");
  editor.classList.add("hidden");
  progress.classList.remove("hidden");
  go.disabled = true;
  barFill.style.width = "5%";
  statusText.textContent = "Uploading…";

  const fd = new FormData();
  fd.append("video", currentFile);

  let job;
  try {
    const res = await fetch("/transcribe-only", { method: "POST", body: fd });
    job = await res.json();
    if (job.error) throw new Error(job.error);
  } catch (e) {
    showError("Upload failed: " + e.message);
    go.disabled = false;
    return;
  }

  currentJobId = job.job_id;
  pollTranscription(job.job_id);
};

async function pollTranscription(jobId) {
  let s;
  try {
    const res = await fetch("/status/" + jobId);
    s = await res.json();
  } catch (e) {
    showError("Connection error — retrying…");
    setTimeout(() => pollTranscription(jobId), 3000);
    return;
  }

  barFill.style.width = (s.progress || 10) + "%";

  if (s.status === "awaiting_edit") {
    barFill.style.width = "100%";
    statusText.textContent = "Transcription complete!";
    currentWords = s.words || [];
    setTimeout(() => {
      progress.classList.add("hidden");
      go.disabled = false;
      showEditor(currentWords);
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

// ---- Phrase timeline ----
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

    const timeEl = document.createElement("span");
    timeEl.className = "phrase-time";
    timeEl.textContent = fmtTime(group[0].start);

    const textEl = document.createElement("span");
    textEl.className = "phrase-text";
    textEl.textContent = group.map(w => w.word).join(" ");

    row.appendChild(timeEl);
    row.appendChild(textEl);

    row.onclick = () => {
      sourcePlayer.currentTime = parseFloat(row.dataset.start);
      if (sourcePlayer.paused) sourcePlayer.play();
      highlightPhraseRow(row);
    };

    phraseListEl.appendChild(row);
  }
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

// ---- Word chip editor ----
function fmtTime(sec) {
  const m = Math.floor(sec / 60);
  const s = (sec % 60).toFixed(1).padStart(4, "0");
  return `${m}:${s}`;
}

function showEditor(words) {
  wordChips.innerHTML = "";
  words.forEach((w, i) => {
    wordChips.appendChild(makeChip(w.word, i, w.start));
  });
  updateWordCount();

  // Load the original uploaded video into the source player
  sourcePlayer.src = "/raw-upload/" + currentJobId;

  // Populate the phrase timeline
  renderPhraseList(words);

  // Show audio preview panel if any enhancement is enabled
  updateAudioPreviewVisibility();

  editor.classList.remove("hidden");
  editor.scrollIntoView({ behavior: "smooth", block: "start" });
}

function makeChip(word, originalIndex, startSec) {
  const chip = document.createElement("div");
  chip.className = "word-chip";
  chip.dataset.origIdx = originalIndex;

  const inner = document.createElement("div");
  inner.className = "word-chip-inner";

  const span = document.createElement("span");
  span.contentEditable = "true";
  span.textContent = word;
  span.spellcheck = false;

  // Prevent newlines; select all on focus for easy replacement
  span.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); span.blur(); }
  });
  span.addEventListener("focus", () => {
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(span);
    sel.removeAllRanges();
    sel.addRange(range);
  });

  const timeLabel = document.createElement("span");
  timeLabel.className = "word-chip-time";
  timeLabel.textContent = startSec != null ? fmtTime(startSec) : "";

  inner.appendChild(span);
  inner.appendChild(timeLabel);

  const del = document.createElement("button");
  del.className = "word-chip-del";
  del.textContent = "×";
  del.title = "Remove word";
  del.onclick = () => { chip.remove(); updateWordCount(); };

  chip.appendChild(inner);
  chip.appendChild(del);
  return chip;
}

function updateWordCount() {
  const n = wordChips.querySelectorAll(".word-chip").length;
  wordCount.textContent = n + " word" + (n !== 1 ? "s" : "");
}

// ---- Phase 2: Render ----
renderBtn.onclick = async () => {
  const chips = wordChips.querySelectorAll(".word-chip");
  const editedWords = [];
  chips.forEach(chip => {
    const text = chip.querySelector("span").textContent.trim();
    if (!text) return;
    const origIdx = parseInt(chip.dataset.origIdx, 10);
    const orig = currentWords[origIdx] || { start: 0, end: 0 };
    editedWords.push({ word: text, start: orig.start, end: orig.end });
  });

  if (!editedWords.length) {
    alert("No words to render.");
    return;
  }

  const emojiRules = getEmojiRules();

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
    s = await res.json();
  } catch (e) {
    setTimeout(() => pollRender(jobId), 3000);
    return;
  }

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
