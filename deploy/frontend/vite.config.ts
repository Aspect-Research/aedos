import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server on :5173 (matches the backend's default AEDOS_ALLOWED_ORIGINS).
// For sub-path production hosting (e.g. aspectresearch.org/aedos) set
// `base: "/aedos/"` here (or via a build env) — left as "/" for local dev.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
});
