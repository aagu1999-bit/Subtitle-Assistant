# reel-automation

Headless renderer for 2.5D floating screenshots with word-level captions,
built to be driven by an n8n webhook.

```
web element ──screenshot──▶ transparent PNG ──Pillow──▶ depth shadow
                                                            │
base video ──┐                                              │
captions   ──┼──────────── CompositeVideoClip ◀──eased pop──┘
             └──▶ final_render.mp4
```

## Run it

```bash
python main.py                                  # defaults, mocked transcript
python main.py --payload job.json               # n8n body from disk
cat job.json | python main.py --payload -       # or from stdin
python main.py --screenshot shot.png            # skip the capture step
```

Every field on `RenderConfig` is settable from the payload. Unknown keys
are ignored, so n8n routing metadata in the same body is harmless:

```json
{
  "url": "https://news.ycombinator.com",
  "selector": ".athing",
  "duration": 6.0,
  "pop_peak": 1.08,
  "active_color": "#FFD700",
  "out_path": "render_042.mp4"
}
```

## Things that will bite you

**MoviePy 2.x renamed everything.** `resized` / `with_position` /
`with_duration` / `with_start`, and `TextClip(text=...)`. The 1.x spellings
(`resize`, `set_position`, `TextClip(txt=...)`) do not exist. Most MoviePy
code you will find online is 1.x and will not run here.

**`font=` is a file path, not a family name.** MoviePy 2.x draws text with
Pillow, so `font="Poppins-Regular"` raises. `resolve_font()` takes a
candidate list, searches the repo's `fonts/` then the system, and prints
what it substituted. **Poppins is not bundled** — drop `Poppins-Regular.ttf`
into `artifacts/subtitle-burner/fonts/` and it is picked up automatically.
Bebas Neue is bundled.

**Scale factors that round to zero pixels crash mid-render.** MoviePy
resizes to whole pixels, so a factor small enough to round to 0 raises
`height and width must be > 0` from deep inside Pillow. The screenshot clip
is scheduled with `with_start()` so it is absent before the pop rather than
present at size zero, and the scale has a floor.

**A blurred, offset shadow needs a bigger canvas.** Pillow composites
within the source bounds, so `add_drop_shadow` grows the canvas by
`offset + 3×blur` first. Without that the shadow is sliced flat along two
edges — which reads as "the design looks a bit off", not as a bug.

**`omit_background=True` is not enough on its own.** Most pages paint an
opaque background; the capture injects `html, body { background:
transparent }` so the element's rounded corners survive into the alpha,
which is what the shadow then cuts around.

## The easing

`ease_out_back` overshoots past 1.0 and settles — that is what makes the
pop read as physical rather than as a linear zoom. Its peak has a closed
form:

```
peak = 1 + 4·c1³ / 27·(c1+1)²
```

The textbook `c1 = 1.70158` peaks at exactly **1.10**, not the 1.05 a spec
usually asks for. So `back_constant_for_peak(1.05)` solves for the constant
instead: set the overshoot in human terms and the maths follows, rather
than pasting a number nobody can re-derive.

## Dependencies

`playwright` and `moviepy` are declared in `pyproject.toml` and
`requirements.txt`. The pip install of Playwright does **not** include the
browser:

```bash
playwright install chromium
```

## Captions

This renders captions as MoviePy `TextClip` layers — one white base layer
per word plus a gold active layer windowed to that word's timestamps, over
a semi-transparent `#1A1C1D` slab.

The sibling app has a faster, more capable path for the same idea:
`build_ass()` in `artifacts/subtitle-burner/app.py` generates libass
subtitles with per-word highlighting, grouping, wrapping, speaker colours
and emoji, burned in one ffmpeg pass. If this pipeline grows past a
prototype, that is where to look before writing more compositing code here.

`mock_whisper_words()` returns `{word, start, end}` dicts shaped like
faster-whisper's output — which is what the app actually uses — so
swapping the mock for a real transcript is a one-line change.
