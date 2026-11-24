# Hyperframes overlays

Sidecar HTML-to-video renderer for Studio, next to `remotion-overlays`.
Does not replace `motion.py` or Remotion.

Write HTML + GSAP. `hyperframes render --format webm` makes a transparent
overlay. Preview on port 8001.

The co-editor only sees wired recipes (gold title, kinetic slam, glitch RGB
captions, pill karaoke). The rest of the catalog stays in Studio / on
https://hyperframes.heygen.com/catalog — star one, then we wire it.

```bash
npx hyperframes preview --no-open --port 8001
npx hyperframes render -c index.html --format webm --workers 1 --low-memory-mode
```
