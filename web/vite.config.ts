/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` proxies the API to a running `hollis serve` (default port 7878). The proxy
// presents the server's own origin, which cookie-authenticated requests must carry.
const api = process.env.HOLLIS_DEV_API ?? "http://127.0.0.1:7878";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/hollis/server/static",
    emptyOutDir: true,
    assetsInlineLimit: 0, // fonts and images as files: the CSP allows data: only for images
    sourcemap: false,
  },
  server: {
    proxy: {
      "/v1": {
        target: api,
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (request) => request.setHeader("origin", api));
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
