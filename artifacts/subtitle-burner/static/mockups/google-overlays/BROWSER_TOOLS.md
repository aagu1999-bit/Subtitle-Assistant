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
1. **Worthiness scorer** (Checkpoint A) — `POST /overlay/worthiness-preview`
   scores cues + `google_tab`; Suggest B-roll already filters by threshold (~55) + spacing.
2. **Provider router**
   - library first
   - SerpAPI/CSE for plain Images
   - **Playwright SERP worker** when you want real search-page chrome (or Flights/Maps/AI Overview)
3. **Checkpoint B** — Review B-roll panel in Timeline Media:
   - shows **worthiness score** badge + suggested tab
   - **Capture SERP** → Playwright Chromium screenshots the results page, crops (or pulls Bing full-size `murl` when thumbs are soft), swaps the card image
   - Accept → Overlay / As Main / Skip (human gate before place)
4. Cache by `(query, google_tab)` so re-runs don’t re-hit Google

## Clarity (Playwright)
Default capture uses `device_scale_factor=2`, opens the Images **detail pane**, and for Bing
falls back to downloading the full-size `murl` when the SERP crop is still small.

## Env knobs
- `OVERLAY_WORTHINESS_THRESHOLD` (default `55`)
- Spacing: 18s min gap between kept overlays
- `SERP_CAPTURE=1` (default) enables Playwright capture API
- `SERP_ENGINE=auto|google|bing` — Google often CAPTCHA from cloud IPs; auto falls back to Bing
- `PLAYWRIGHT_CHROMIUM_USE_HEADLESS_SHELL=0` — prefer full Chromium (set by worker; helps Replit)

## Replit Chromium
Capture SERP needs a working Playwright browser. If you see
`BrowserType.launch: Target page, context or browser has been closed` (and a wall of
Chromium flags in the alert), Chromium crashed on start — usually sandbox/shm on Replit.

```bash
cd artifacts/subtitle-burner   # or your Studio root
pip install playwright
python -m playwright install chromium
# then Stop + Run the Project workflow
```

The SERP worker launches with `--no-sandbox`, `--disable-dev-shm-usage`, and retries
`--single-process` if the first launch dies.

## Live API
- `POST /overlay/serp-capture` `{ "query": "...", "tab": "images" }` → Timeline `asset_id`
- `GET/POST /overlay/serp-demo-nj` → captures the 3 NJ reference crops
- CLI: `python scripts/serp_screenshot.py --demo-nj`
