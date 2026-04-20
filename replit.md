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
- Runs on port 5000 (local), served at `/` via external port 80 through path-based routing

### Structure
```
artifacts/subtitle-burner/
├── app.py                 # Flask server, Whisper, ASS generation, FFmpeg
├── templates/index.html   # Frontend UI
├── static/style.css       # Styles
├── static/app.js          # Frontend JavaScript
├── fonts/                 # Add .ttf/.otf font files here
├── uploads/               # Temp uploads (auto-created)
└── outputs/               # Rendered videos (auto-created)
```

### Fonts
Add font files to `artifacts/subtitle-burner/fonts/`:
- `Montserrat-Black.ttf` — free from Google Fonts
- `TheBoldFont.ttf` — thebold.net (commercial)
- `IntegralCF-Bold.otf` — fontfabric.com (commercial)

See `references/server.md` for workspace structure details.
