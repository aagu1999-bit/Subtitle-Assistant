---
name: Canvas artifact owns Flask app routing
description: How the subtitle-burner Flask app is served externally and why duplicate workflows break it
---
External/mobile traffic to `/` is routed via the Canvas artifact's managed service `artifacts/mockup-sandbox: Start application`, not the raw port map.

**Rules:**
- The Flask app (artifacts/subtitle-burner, port 8081) must be run ONLY by the managed artifact workflow. A standalone workflow running the same app steals port 8081 → managed service fails "Address already in use" → external 502.
- Managed artifact service run commands execute from the artifact's own dir, so use absolute paths (`cd /home/runner/workspace/artifacts/subtitle-burner`), not workspace-relative ones.

**Why:** Two outages (Aug 2026) came from (1) a relative `cd` failing in the managed workflow, (2) a duplicate standalone "Start application" workflow holding 8081.

**How to apply (superseded Aug 2026):** Final working setup is the opposite of the old rule: the Flask app is served by a STANDALONE "Start application" workflow (port 8081 → external 80), because the workspace proxy does not route web traffic to a `kind = "design"` artifact's services. The Canvas artifact was moved to previewPath `/__canvas` and no longer owns `/`. Do not re-add the Flask app as a Canvas artifact service.

**Port gotcha:** `replit.dev:8080` is the API server (external 8080 → local 8080), NOT the app. The app is at the plain domain URL (external 80 → local 8081). A preview tab pinned to `:8080` shows 502 whenever the API server is down — check which port the preview URL uses before debugging the app.
