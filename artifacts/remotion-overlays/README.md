# remotion-overlays

Sidecar Remotion project. `motion.py` still draws most co-editor animations.
Rotating / orbiting / carousel requests render through OrbitCarousel and land
on the timeline as a normal overlay (`_make_remotion_orbit`).

On Replit, port 3000 is the canvas preview. Studio is **8000**.

```bash
cd artifacts/remotion-overlays
pnpm install
pnpm studio
```

Wait for `Server ready` on 8000. Open **port 8000 in a new browser tab**
(Tools → Ports → 8000 → Public / open). Do not use the Preview iframe
(CORS: "Request from different origin not allowed").

In Studio: Actions → Browse Elements → Remotion Elements (charts, maps,
captions). Catalog: `SHELF.md`. `pnpm render` writes `out/orbit.webm`.

Do not copy React Bits into this repo as a library. Port one look onto
`useCurrentFrame()` when you want that style (see OrbitCarousel.tsx).
