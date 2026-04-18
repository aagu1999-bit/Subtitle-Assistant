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

let currentFile = null;

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
groupEl.oninput = () => groupVal.textContent = groupEl.value;

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

// ---- Submit ----
go.onclick = async () => {
  if (!currentFile) return;
  result.classList.add("hidden");
  progress.classList.remove("hidden");
  go.disabled = true;
  barFill.style.width = "5%";
  statusText.textContent = "Uploading…";

  const style = {
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

  const fd = new FormData();
  fd.append("video", currentFile);
  fd.append("style", JSON.stringify(style));

  let job;
  try {
    const res = await fetch("/upload", { method: "POST", body: fd });
    job = await res.json();
    if (job.error) throw new Error(job.error);
  } catch (e) {
    statusText.textContent = "Upload failed: " + e.message;
    go.disabled = false;
    return;
  }

  poll(job.job_id);
};

async function poll(jobId) {
  const res = await fetch("/status/" + jobId);
  const s = await res.json();
  barFill.style.width = (s.progress || 10) + "%";
  statusText.textContent = s.status.charAt(0).toUpperCase() + s.status.slice(1) + "…";

  if (s.status === "done") {
    barFill.style.width = "100%";
    progress.classList.add("hidden");
    result.classList.remove("hidden");
    player.src = "/preview/" + s.output;
    dl.href = "/download/" + s.output;
    go.disabled = false;
    return;
  }
  if (s.status === "error") {
    statusText.textContent = "Error: " + s.error;
    go.disabled = false;
    return;
  }
  setTimeout(() => poll(jobId), 2000);
}
