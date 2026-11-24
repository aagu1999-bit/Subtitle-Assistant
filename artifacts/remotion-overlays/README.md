# remotion-overlays

Sidecar Remotion project. Studio still renders overlays with `motion.py`
(Pillow + ffmpeg). This folder is the trial renderer.

```bash
cd artifacts/remotion-overlays
pnpm install
pnpm remotion studio          # preview
pnpm render                   # transparent WebM
```

Do not copy React Bits into this repo as a library. Port one look onto
`useCurrentFrame()` when you want that style (see OrbitCarousel.tsx).
