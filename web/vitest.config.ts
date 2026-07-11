import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Vitest unit/component test harness (jsdom). The Playwright smoke suite lives
// in e2e/ and runs via `npm run test:e2e` — it is excluded here so `vitest run`
// never tries to execute Playwright specs.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // Mirror the tsconfig "@/*" path alias.
      "@": path.resolve(__dirname),
      // lib/db.ts / lib/queries.ts import "server-only" to enforce the server
      // boundary; that package throws outside a React Server environment, so
      // tests resolve it to an empty local stub instead.
      "server-only": path.resolve(__dirname, "test/stubs/server-only.ts"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./test/setup.ts"],
    include: ["test/**/*.test.{ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**"],
  },
});
