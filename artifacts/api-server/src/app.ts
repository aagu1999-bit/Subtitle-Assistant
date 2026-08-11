import express, { type Express, type Request, type Response } from "express";
import cors from "cors";
import pinoHttp from "pino-http";
import http from "node:http";
import router from "./routes";
import { logger } from "./lib/logger";

const app: Express = express();

// ---- Catch-all proxy to the Subtitle Burner Flask app (port 8081) ----
// The workspace preview often opens replit.dev:8080 (this server). Anything
// that is not an /api route is forwarded to the Flask app so the preview
// shows the real UI regardless of which port the preview pane picks.
// Registered BEFORE the body parsers so request bodies (JSON posts, video
// uploads) stream through untouched.
const FLASK_PORT = Number(process.env["FLASK_APP_PORT"] ?? 8081);

// Hop-by-hop headers must not be forwarded by a proxy (RFC 7230 §6.1).
const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

function sanitizeHeaders(
  headers: http.IncomingHttpHeaders,
): http.IncomingHttpHeaders {
  const out: http.IncomingHttpHeaders = {};
  // Headers named in the Connection header are also hop-by-hop.
  const connectionListed = new Set(
    String(headers["connection"] ?? "")
      .split(",")
      .map((h) => h.trim().toLowerCase())
      .filter(Boolean),
  );
  for (const [key, value] of Object.entries(headers)) {
    const lower = key.toLowerCase();
    if (HOP_BY_HOP.has(lower) || connectionListed.has(lower)) continue;
    out[key] = value;
  }
  return out;
}

function proxyToFlask(req: Request, res: Response): void {
  const upstream = http.request(
    {
      host: "127.0.0.1",
      port: FLASK_PORT,
      path: req.originalUrl,
      method: req.method,
      headers: {
        ...sanitizeHeaders(req.headers),
        host: `127.0.0.1:${FLASK_PORT}`,
      },
    },
    (upstreamRes) => {
      res.writeHead(
        upstreamRes.statusCode ?? 502,
        sanitizeHeaders(upstreamRes.headers),
      );
      upstreamRes.pipe(res);
      // If the upstream response dies mid-stream, destroy the client response
      // instead of cleanly ending a truncated body.
      upstreamRes.on("error", (err) => {
        logger.error({ err }, "Flask proxy upstream response error");
        res.destroy(err);
      });
      // Client went away — stop reading from Flask.
      res.on("close", () => {
        upstreamRes.destroy();
      });
    },
  );
  upstream.on("error", (err) => {
    logger.error({ err }, "Flask proxy error");
    if (!res.headersSent) {
      res
        .status(502)
        .json({ error: "Subtitle Burner app is not reachable on port 8081" });
    } else {
      res.destroy(err);
    }
  });
  // Client aborted the upload — tear down the upstream request too.
  req.on("aborted", () => {
    upstream.destroy();
  });
  req.on("error", (err) => {
    logger.error({ err }, "Flask proxy client request error");
    upstream.destroy(err);
  });
  req.pipe(upstream);
}

app.use((req, res, next) => {
  if (req.path === "/api" || req.path.startsWith("/api/")) {
    next();
    return;
  }
  proxyToFlask(req, res);
});

app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return {
          id: req.id,
          method: req.method,
          url: req.url?.split("?")[0],
        };
      },
      res(res) {
        return {
          statusCode: res.statusCode,
        };
      },
    },
  }),
);
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.use("/api", router);

export default app;
