// vite.config.ts
import react from "file:///<PROJECT_DIR>/web/node_modules/@vitejs/plugin-react/dist/index.js";
import { defineConfig } from "file:///<PROJECT_DIR>/web/node_modules/vite/dist/node/index.js";
var vite_config_default = defineConfig({
  plugins: [react()],
  server: {
    port: 5180,
    proxy: {
      "/api": "http://localhost:8099",
      "/artifacts": "http://localhost:8099"
    }
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
          if (id.includes("vidstack") || id.includes("media-icons")) return "vendor-media";
          if (id.includes("recharts") || id.includes("victory-vendor") || /[\\/]d3-/.test(id))
            return "vendor-charts";
          return;
        }
      }
    },
    chunkSizeWarningLimit: 1200
  }
});
export {
  vite_config_default as default
};
//# sourceMappingURL=data:application/json;base64,ewogICJ2ZXJzaW9uIjogMywKICAic291cmNlcyI6IFsidml0ZS5jb25maWcudHMiXSwKICAic291cmNlc0NvbnRlbnQiOiBbImNvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9kaXJuYW1lID0gXCJEOlxcXFx6a3Jfd29ya1xcXFxQb3RhdG9fVGVzdFxcXFx3ZWJcIjtjb25zdCBfX3ZpdGVfaW5qZWN0ZWRfb3JpZ2luYWxfZmlsZW5hbWUgPSBcIkQ6XFxcXHprcl93b3JrXFxcXFBvdGF0b19UZXN0XFxcXHdlYlxcXFx2aXRlLmNvbmZpZy50c1wiO2NvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9pbXBvcnRfbWV0YV91cmwgPSBcImZpbGU6Ly8vRDovemtyX3dvcmsvUG90YXRvX1Rlc3Qvd2ViL3ZpdGUuY29uZmlnLnRzXCI7aW1wb3J0IHJlYWN0IGZyb20gXCJAdml0ZWpzL3BsdWdpbi1yZWFjdFwiO1xyXG5pbXBvcnQgeyBkZWZpbmVDb25maWcgfSBmcm9tIFwidml0ZVwiO1xyXG5cclxuLy8gRGV2IHByb3h5OiB0aGUgU1BBIGNhbGxzIC9hcGkgYW5kIC9hcnRpZmFjdHMgb24gaXRzIG93biBvcmlnaW47IFZpdGUgZm9yd2FyZHNcclxuLy8gdGhlbSB0byB0aGUgRmFzdEFQSSBiYWNrZW5kIG9uIDo4MDk5LlxyXG5leHBvcnQgZGVmYXVsdCBkZWZpbmVDb25maWcoe1xyXG4gIHBsdWdpbnM6IFtyZWFjdCgpXSxcclxuICBzZXJ2ZXI6IHtcclxuICAgIHBvcnQ6IDUxODAsXHJcbiAgICBwcm94eToge1xyXG4gICAgICBcIi9hcGlcIjogXCJodHRwOi8vbG9jYWxob3N0OjgwOTlcIixcclxuICAgICAgXCIvYXJ0aWZhY3RzXCI6IFwiaHR0cDovL2xvY2FsaG9zdDo4MDk5XCIsXHJcbiAgICB9LFxyXG4gIH0sXHJcbiAgYnVpbGQ6IHtcclxuICAgIC8vIEtlZXAgdGhlIGFwcCBzaGVsbCBzbWFsbDogcm91dGVzIGFyZSBsYXp5IChzZWUgbWFpbi50c3gpLCBhbmQgdmVuZG9yIGxpYnNcclxuICAgIC8vIGFyZSBzcGxpdCBzbyBhIGNoYW5nZSB0byBhcHAgY29kZSBuZXZlciBpbnZhbGlkYXRlcyB0aGUgdmVuZG9yIGNhY2hlLlxyXG4gICAgLy9cclxuICAgIC8vIE9ubHkgc2VsZi1jb250YWluZWQgaGVhdnl3ZWlnaHQgbGlicyBnZXQgdGhlaXIgb3duIGNodW5rLiBFdmVyeXRoaW5nIGVsc2VcclxuICAgIC8vIChyZWFjdCwgcmVhY3QtZG9tLCBzY2hlZHVsZXIsIHJhZGl4LCByb3V0ZXIsIGkxOG4sIGljb25zIFx1MjAyNikgaXMgbGVmdCB0b1xyXG4gICAgLy8gUm9sbHVwJ3Mgb3duIGdyYXBoOiBSZWFjdCBhbmQgcmVhY3QtZG9tIE1VU1QgbGFuZCBpbiB0aGUgc2FtZSBjaHVuayBhcyB0aGVcclxuICAgIC8vIHBhY2thZ2VzIHRoYXQgaW1wb3J0IHRoZW0sIG90aGVyd2lzZSBhIGZvcmNlLXNwbGl0IGNhbiBlbWl0IGEgY2h1bmsgdGhhdFxyXG4gICAgLy8gcnVucyBiZWZvcmUgUmVhY3QgaXMgaW5pdGlhbGlzZWQgYW5kIHRocm93c1xyXG4gICAgLy8gXCJDYW5ub3QgcmVhZCBwcm9wZXJ0aWVzIG9mIHVuZGVmaW5lZCAocmVhZGluZyAndXNlU3RhdGUnKVwiLlxyXG4gICAgcm9sbHVwT3B0aW9uczoge1xyXG4gICAgICBvdXRwdXQ6IHtcclxuICAgICAgICBtYW51YWxDaHVua3MoaWQpIHtcclxuICAgICAgICAgIGlmICghaWQuaW5jbHVkZXMoXCJub2RlX21vZHVsZXNcIikpIHJldHVybjtcclxuICAgICAgICAgIC8vIG1lZGlhL3BsYXllciBzdGFjayBcdTIwMTQgcHVsbHMgaW4gbm8gUmVhY3QsIHNhZmUgdG8gaXNvbGF0ZVxyXG4gICAgICAgICAgaWYgKGlkLmluY2x1ZGVzKFwidmlkc3RhY2tcIikgfHwgaWQuaW5jbHVkZXMoXCJtZWRpYS1pY29uc1wiKSkgcmV0dXJuIFwidmVuZG9yLW1lZGlhXCI7XHJcbiAgICAgICAgICAvLyBjaGFydHM6IG9ubHkgT3ZlcnZpZXcgYW5kIFJ1blJlcG9ydCB1c2UgaXQsIGFuZCBpdCBpcyBieSBmYXIgdGhlXHJcbiAgICAgICAgICAvLyBoZWF2aWVzdCBkZXBlbmRlbmN5IGluIHRoZSBhcHAgKH4zNjgga0IpLiBJc29sYXRpbmcgaXQga2VlcHMgaXQgb2ZmXHJcbiAgICAgICAgICAvLyBldmVyeSBvdGhlciByb3V0ZSBhbmQgbGV0cyB0aGUgYnJvd3NlciBjYWNoZSBpdCBhY3Jvc3MgZGVwbG95cy5cclxuICAgICAgICAgIGlmIChpZC5pbmNsdWRlcyhcInJlY2hhcnRzXCIpIHx8IGlkLmluY2x1ZGVzKFwidmljdG9yeS12ZW5kb3JcIikgfHwgL1tcXFxcL11kMy0vLnRlc3QoaWQpKVxyXG4gICAgICAgICAgICByZXR1cm4gXCJ2ZW5kb3ItY2hhcnRzXCI7XHJcbiAgICAgICAgICByZXR1cm47XHJcbiAgICAgICAgfSxcclxuICAgICAgfSxcclxuICAgIH0sXHJcbiAgICBjaHVua1NpemVXYXJuaW5nTGltaXQ6IDEyMDAsXHJcbiAgfSxcclxufSk7XHJcbiJdLAogICJtYXBwaW5ncyI6ICI7QUFBMlEsT0FBTyxXQUFXO0FBQzdSLFNBQVMsb0JBQW9CO0FBSTdCLElBQU8sc0JBQVEsYUFBYTtBQUFBLEVBQzFCLFNBQVMsQ0FBQyxNQUFNLENBQUM7QUFBQSxFQUNqQixRQUFRO0FBQUEsSUFDTixNQUFNO0FBQUEsSUFDTixPQUFPO0FBQUEsTUFDTCxRQUFRO0FBQUEsTUFDUixjQUFjO0FBQUEsSUFDaEI7QUFBQSxFQUNGO0FBQUEsRUFDQSxPQUFPO0FBQUE7QUFBQTtBQUFBO0FBQUE7QUFBQTtBQUFBO0FBQUE7QUFBQTtBQUFBO0FBQUEsSUFVTCxlQUFlO0FBQUEsTUFDYixRQUFRO0FBQUEsUUFDTixhQUFhLElBQUk7QUFDZixjQUFJLENBQUMsR0FBRyxTQUFTLGNBQWMsRUFBRztBQUVsQyxjQUFJLEdBQUcsU0FBUyxVQUFVLEtBQUssR0FBRyxTQUFTLGFBQWEsRUFBRyxRQUFPO0FBSWxFLGNBQUksR0FBRyxTQUFTLFVBQVUsS0FBSyxHQUFHLFNBQVMsZ0JBQWdCLEtBQUssV0FBVyxLQUFLLEVBQUU7QUFDaEYsbUJBQU87QUFDVDtBQUFBLFFBQ0Y7QUFBQSxNQUNGO0FBQUEsSUFDRjtBQUFBLElBQ0EsdUJBQXVCO0FBQUEsRUFDekI7QUFDRixDQUFDOyIsCiAgIm5hbWVzIjogW10KfQo=
