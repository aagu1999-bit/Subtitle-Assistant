# Hyperframes overlays

Sidecar HTML-to-video renderer for Studio, next to `remotion-overlays`.
Does not replace `motion.py` or Remotion.

Write HTML + GSAP. `hyperframes render --format webm` makes a transparent
overlay. Preview on port 8001.

The co-editor sees every overlay installed here (captions, lower thirds,
follow cards, handwritten titles). `hyperframes.json` is the list; adding
another catalog item wires it. Full-scene films (WebGPU, 3D, transitions)
stay on https://hyperframes.heygen.com/catalog.

```bash
npx hyperframes preview --no-open --port 8001
npx hyperframes render -c index.html --format webm --workers 1 --low-memory-mode
```
