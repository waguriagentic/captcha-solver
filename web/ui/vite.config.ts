import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Builds to web/ui/dist, which web/routes.py serves as the SPA shell.
// Base is "/" because the dashboard owns its whole origin (dash.example.com) —
// the API lives on a separate host and never serves these assets.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    // `npm run dev` talks to a locally running server.py on :8877.
    proxy: {
      "/api": { target: "http://127.0.0.1:8877", changeOrigin: false },
    },
  },
});
