import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

/**
 * Two shapes, one app:
 *
 *  - `npm run dev`  → next dev on :3000, proxying the engine's routes to the
 *                     Python engine on :7070 (started by scripts/dev.mjs).
 *  - `npm run build` → a static export in ./out that the engine serves itself,
 *                     so day to day only `python3 engine/server.py` runs.
 */
const ENGINE = process.env.ENGINE_URL ?? "http://127.0.0.1:7070";

// Everything the engine answers. Keep in sync with engine/server.py routes.
const ENGINE_ROUTES = [
  "/api",
  "/page",
  "/pdf",
  "/raw",
  "/cover",
  "/thumbnail",
  "/screenshot",
  "/handwriting",
];

export default function config(phase: string): NextConfig {
  if (phase === PHASE_DEVELOPMENT_SERVER) {
    return {
      // next dev otherwise writes an AGENTS.md into the project.
      agentRules: false,
      experimental: {
        // Syncing a full Kindle can take minutes; don't cut it off.
        proxyTimeout: 30 * 60 * 1000,
        // Push-to-Kindle uploads go through the proxy too.
        proxyClientMaxBodySize: "110mb",
      },
      async rewrites() {
        return ENGINE_ROUTES.map((prefix) => ({
          source: `${prefix}/:path*`,
          destination: `${ENGINE}${prefix}/:path*`,
        }));
      },
    };
  }
  return {
    output: "export",
    images: { unoptimized: true },
  };
}
