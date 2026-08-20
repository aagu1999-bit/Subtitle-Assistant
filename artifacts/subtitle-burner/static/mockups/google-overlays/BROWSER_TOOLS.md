# Browser capture tools (SERP screenshot overlays)

Goal: stay on Google (Web / Images / Flights / Maps / AI Overview) → screenshot →
crop the useful card → Timeline overlay asset. No click-through into articles.

## Do we have Chrome DevTools MCP here?
**No.** This cloud agent environment does not expose a Chrome DevTools MCP server.
What we *do* have:

| Tool | What it is | Fit for SERP screenshots |
|---|---|---|
| **Cursor `computerUse` agent** | Full desktop browser control (click, type, screenshot) | Works for demos; Google often CAPTCHA-blocks automation |
| **Playwright / Puppeteer** | Headless Chromium via CDP (Chrome DevTools Protocol) | Best engineering fit for a Studio worker |
| **Selenium + ChromeDriver** | Classic WebDriver | Same idea as Playwright; heavier |
| **Chrome DevTools Protocol (raw)** | Low-level CDP over WebSocket | Power users; Playwright wraps this |
| **Browserless / Playwright cloud** | Hosted Chromium with CDP | Scale without managing browsers on Replit |
| **SerpAPI / Google CSE** | JSON search APIs (not screenshots) | Fast fallback for *images*; cannot capture Flights UI chrome |

Advertised “AI browser agents” are optional wrappers — underneath they still use
**CDP / Playwright / Puppeteer**. Prefer those primitives.

## Recommended architecture for Studio
1. **Worthiness scorer** (Checkpoint A) — already drafting in app.py  
   `POST /overlay/worthiness-preview` → candidates with scores + `google_tab`
2. **Provider router**
   - library first
   - SerpAPI/CSE for plain Images
   - **Playwright SERP worker** only when `google_tab` is flights / ai_overview / maps / web card
3. **Checkpoint B** — human Accept/Reject before place
4. Cache by `(query, google_tab)` so re-runs don’t re-hit Google

## Env knobs
- `OVERLAY_WORTHINESS_THRESHOLD` (default `55`)
- Spacing: 18s min gap between kept overlays
