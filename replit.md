# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.
Also contains a Python/Flask web app (Subtitle Burner) in `artifacts/subtitle-burner/`.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)
- **Python**: 3.11 (for Subtitle Burner)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

## Subtitle Burner App

Located in `artifacts/subtitle-burner/`. A Flask web app that:
- Accepts video uploads (mp4, mov, mkv, webm, avi, m4v)
- Transcribes speech using faster-whisper (base model, CPU)
- Generates ASS subtitle files with word-by-word highlighting
- Burns captions into video using FFmpeg
- Supports FFmpeg audio enhancement and optional Auphonic AI enhancement
- Persists job state, edited subtitle words, style settings, audio settings, and emoji rules in SQLite (`jobs.db`)
- Auto-saves subtitle/style drafts from the browser while editing and restores the latest job after reload
- **Timeline Editor** (Editor tab): a multi-track video editor built on the same
  FFmpeg backend. Tailored to interview / red-carpet edits:
  - **Main track** — trim & arrange sequential clips from any uploaded video,
    with per-boundary transitions (crossfade, fade-to-black, dissolve, slide,
    wipe, etc.).
  - **Overlay track** — B-roll / picture-in-picture / image overlays, positioned
    and sized over the main footage, gated to a time window.
  - **Titles track** — titles & lower-thirds burned via libass (fonts, colors,
    opaque box, fade / slide-up animation).
  - **Music track** — background music with per-clip gain and optional ducking
    (sidechain-compressed under the voice).
  - **Per-clip effects** (Main track): **Ken Burns** slow zoom (push-in /
    pull-out, 3 strengths, via zoompan); **split-screen** (two sources at once,
    side-by-side or top/bottom, via hstack/vstack); **text-based editing** —
    strike out transcript words and those spans are cut from the clip
    (keep-ranges stitched back together). Each effect is toggled per clip, with
    a badge on the timeline block (🔍 / ⬓ / ✂️).
  - **Persistent logo / watermark** — project-level image/video overlay across
    the whole render (set via the 🏷 Logo / project panel).
  - **Captions in the timeline** — each Main clip can burn its source
    transcript as word-by-word karaoke captions (per-clip toggle, default on).
    Word timestamps are remapped through trims / text-cuts / transitions onto
    the output timeline and merged with titles into a single libass pass.
  - **Send to timeline** — 🎬 button on each sidebar video and each Highlights
    result opens the editor with that clip (or highlight range) on the Main
    track (`window.openTimelineEditor(jobId, {in, out})`).
  - **Live preview** — canvas-aspect stage (matches the output ratio + fit) with
    a scrub bar; drag title / overlay / logo boxes directly on the frame to
    position them, and ⤓ Set IN/OUT buttons trim a clip at the playhead.
  - **Preview cut** — play Main-track keep-ranges (skips text cuts) and show
    timed overlays / titles / logo on the stage without a full FFmpeg render.
    Also approximates color grades, Ken Burns, inter-clip transitions, and a
    ducked music bed so edit → see no longer requires a full burn.
  - **Magnetic snap + ripple** — clip edges snap to nearby cuts / playhead;
    Main trims reflow anchored overlays/titles/music; deleting Main drops its
    anchored items and closes the gap.
  - **Undo / redo + keyboard** — Ctrl/⌘Z / Shift+Z, Space play/pause, S split,
    Del delete, ←/→ nudge (Shift = 1s), +/- zoom. Caption Edit tab also has
    transcript/style undo.
  - **Workflow handoffs** — Edit / Highlights / Compilation can send clips
    straight into the Editor (`openTimelineEditor` supports ranges + queues).
  - **Descript-style editing surface:** left column toggles **📝 Transcript**
    (click words to strike → cut from the video, with the playing word
    highlighted during preview) and **📁 Media**. Timeline has a **playhead**
    synced to the preview, **✂ Split** at the playhead, and **zoom**. Clips show
    **filmstrip thumbnails + audio waveforms** (`/filmstrip`, `/waveform`,
    `/asset-waveform` — cached ffmpeg). Per-clip **color grade** presets (None /
    Neutral / Warm / Cool / Vivid / B&W) + brightness/contrast/saturation
    (ffmpeg `eq`/`colorbalance`).
  - **Reliability:** overlays/titles/music **anchor** to a Main clip (+offset),
    so reordering/retrimming/cutting Main carries them along instead of
    desyncing; 20 ms audio fades on every segment de-click cuts; source videos
    referenced by a timeline are **exempt from cleanup** (won't be pruned while
    in use); Split works after a full render (maps output time → clip + source
    time); clip drag/resize works on touch (pointer events).
  - Canvas presets (9:16, 16:9, 1:1, 4:5) with cover/contain fit. Projects are
    persisted in the `timeline` column of `jobs.db` and survive restarts.
  - Renders in four passes (main → music → overlays → titles); output lands in
    `outputs/` and plays back in the Editor preview.
- Runs on port 8081 (local), served at `/` via external port 80 (no port suffix in URL)

### Structure
```
artifacts/subtitle-burner/
├── app.py                 # Flask server, Whisper, ASS generation, FFmpeg, Timeline engine
├── templates/index.html   # Frontend UI
├── static/style.css       # Styles
├── static/app.js          # Frontend JavaScript (caption editor)
├── static/timeline.css    # Timeline Editor styles
├── static/timeline.js     # Timeline Editor (multi-track) front-end
├── jobs.db                # SQLite job/draft/timeline persistence
├── fonts/                 # Add .ttf/.otf font files here
├── assets/                # Uploaded B-roll / images / music for the editor (auto-created)
├── uploads/               # Temp uploads (auto-created)
└── outputs/               # Rendered videos (auto-created)
```

### Fonts
Add font files to `artifacts/subtitle-burner/fonts/`:
- `Montserrat-Black.ttf` — free from Google Fonts
- `TheBoldFont.ttf` — thebold.net (commercial)
- `IntegralCF-Bold.otf` — fontfabric.com (commercial)

See `references/server.md` for workspace structure details.
