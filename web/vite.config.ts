import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev proxy: the SPA calls /api and /artifacts on its own origin; Vite forwards
// them to the FastAPI backend on :8099.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5180,
    proxy: {
      "/api": "http://localhost:8099",
      "/artifacts": "http://localhost:8099",
    },
  },
  build: {
    // Keep the app shell small: routes are lazy (see main.tsx), and vendor libs
    // are split so a change to app code never invalidates the vendor cache.
    //
    // Only self-contained heavyweight libs get their own chunk. Everything else
    // (react, react-dom, scheduler, radix, router, i18n, icons …) is left to
    // Rollup's own graph: React and react-dom MUST land in the same chunk as the
    // packages that import them, otherwise a force-split can emit a chunk that
    // runs before React is initialised and throws
    // "Cannot read properties of undefined (reading 'useState')".
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          // media/player stack — pulls in no React, safe to isolate
          if (id.includes("vidstack") || id.includes("media-icons")) return "vendor-media";
          // charts: only Overview and RunReport use it, and it is by far the
          // heaviest dependency in the app (~368 kB). Isolating it keeps it off
          // every other route and lets the browser cache it across deploys.
          if (id.includes("recharts") || id.includes("victory-vendor") || /[\\/]d3-/.test(id))
            return "vendor-charts";
          return;
        },
      },
    },
    chunkSizeWarningLimit: 1200,
  },
});
