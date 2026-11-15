import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In Docker the API is the "app" service; locally it is localhost:8000.
const target = process.env.VITE_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: { "/api": { target, changeOrigin: true } },
    watch: { usePolling: true }, // file events don't cross Docker bind mounts on Windows
  },
});
