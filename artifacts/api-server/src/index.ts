import app from "./app";
import { logger } from "./lib/logger";

const rawPort = process.env["PORT"];

if (!rawPort) {
  throw new Error(
    "PORT environment variable is required but was not provided.",
  );
}

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

const server = app.listen(port, (err) => {
  if (err) {
    logger.error({ err }, "Error listening on port");
    process.exit(1);
  }

  logger.info({ port }, "Server listening");
});

// The Subtitle Burner proxy streams multi-hundred-MB video uploads and
// long-running render responses through this server. Node's default
// requestTimeout (300s) would kill slow uploads mid-stream, so disable it;
// keepAliveTimeout/headersTimeout stay at their defaults.
server.requestTimeout = 0;
