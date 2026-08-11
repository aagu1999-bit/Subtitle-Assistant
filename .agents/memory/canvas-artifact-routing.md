---
name: Canvas artifact routing & preview ports
description: Durable routing decisions for the Subtitle Burner preview and Canvas (design) artifact
---

## Decisions
- The Flask app must run from a **standalone** workflow, never as a service of the Canvas (design) artifact. **Why:** the workspace proxy gives design-kind artifacts no web routing, and a duplicate artifact service fights over the app's port, hanging the preview.
- The preview pane may open either the plain domain URL or `:8080` (the API server). Both must serve the app, so the API server keeps a catch-all proxy for non-`/api` paths to the Flask app. The proxy sits before body parsers and the server disables `requestTimeout` so large video uploads stream through.
- **How to apply:** after any branch switch or pull, check that the app's workflow still targets the externally-mapped port, the Canvas artifact declares no app service, and the API-server proxy still exists — remote branches have reintroduced stale config before.

## Platform quirks
- `removeWorkflow` refuses managed artifact workflows even after the service is dropped from artifact.toml; strip the stale block by writing a full `.replit` and calling `verifyAndReplaceDotReplit`.
- `verifyAndReplaceArtifactToml` refuses version or kind changes.
- Managed artifact run commands need absolute paths (relative `cd` fails from the artifact's own directory).
