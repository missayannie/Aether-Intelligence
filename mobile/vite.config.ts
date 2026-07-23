import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base: "./" — Capacitor serves the built app from file:// inside the native
// shell, so asset URLs must be relative, not absolute.
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: { port: 5180, host: true },
});
