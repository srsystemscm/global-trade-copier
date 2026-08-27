import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Proxies API + WebSocket calls straight through to the FastAPI hub during
// development, so the browser never needs to know the hub's actual port.
// Override with VITE_HUB_TARGET if the hub isn't on its default port.
const HUB_TARGET = process.env.VITE_HUB_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/slaves": HUB_TARGET,
      "/trades": HUB_TARGET,
      "/config": HUB_TARGET,
      "/status": HUB_TARGET,
      "/logs": HUB_TARGET,
      "/risk": HUB_TARGET,
      "/schwab": HUB_TARGET,
      "/health": HUB_TARGET,
      "/ws": { target: HUB_TARGET.replace("http", "ws"), ws: true },
    },
  },
});
