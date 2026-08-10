---
name: Canvas artifact owns Flask app routing
description: How the subtitle-burner Flask app is served externally and why duplicate workflows break it
---
External/mobile traffic to `/` is routed via the Canvas artifact's managed service `artifacts/mockup-sandbox: Start application`, not the raw port map.

**Rules:**
- The Flask app (artifacts/subtitle-burner, port 8081) must be run ONLY by the managed artifact workflow. A standalone workflow running the same app steals port 8081 → managed service fails "Address already in use" → external 502.
- Managed artifact service run commands execute from the artifact's own dir, so use absolute paths (`cd /home/runner/workspace/artifacts/subtitle-burner`), not workspace-relative ones.

**Why:** Two outages (Aug 2026) came from (1) a relative `cd` failing in the managed workflow, (2) a duplicate standalone "Start application" workflow holding 8081.

**How to apply:** If mobile/external preview 502s, check the managed workflow's logs first; kill any stray `python app.py` process holding 8081; never re-create a standalone workflow for this app.
