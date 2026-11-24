# remotion-overlays

Sidecar Remotion project. `motion.py` still draws most co-editor animations.
Rotating / orbiting / carousel requests render through OrbitCarousel and land
on the timeline as a normal overlay (`_make_remotion_orbit`).

On Replit, port 3000 is already the canvas preview. Use **8000**.

```bash
cd artifacts/remotion-overlays
pnpm install
pnpm studio                 # http://localhost:8000 — open this in a NEW TAB, not the Preview iframe
pnpm render                 # out/orbit.webm (concurrency 1, fits Replit RAM)
```

The Preview pane is an iframe on another origin, so Remotion Studio's
API calls fail with "Request from different origin not allowed". Open
the Public URL for port 8000 in its own browser tab.

Do not copy React Bits into this repo as a library. Port one look onto
`useCurrentFrame()` when you want that style (see OrbitCarousel.tsx).
