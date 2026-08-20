# Google SERP overlay examples

Browse: open `index.html` in a browser (or via Studio static hosting).

## Status of images
The PNGs in this folder are **placeholders** (styled mock crops), not live Google captures.
Automated Google access from the cloud agent hits CAPTCHA, so real screenshots must be
dropped in by hand (or from a trusted SerpAPI/browser worker later).

### Replace with real SERP crops
Save over these filenames (same names keep `index.html` working):

| File | Google surface | Example spoken cue |
|---|---|---|
| `overlay-ex-images-festival.png` | Images tile | “We were at that Asbury festival…” |
| `overlay-ex-flights-card.png` | Flights card | “Newark to Miami that Friday…” |
| `overlay-ex-ai-overview.png` | AI Overview / featured box | “They’re reopening the boardwalk…” |
| `overlay-ex-web-article.png` | Web/News result | “City approved the waterfront park…” |
| `overlay-ex-maps-place.png` | Maps place card | “That Italian place in Hoboken…” |

**Crop rule:** stay on Google → screenshot the useful card/photo only → no click-through.

## Overlay worthiness (working notes)
See conversation / product notes: every video is a story; overlays should prove or
visualize a concrete beat, not decorate every sentence.
