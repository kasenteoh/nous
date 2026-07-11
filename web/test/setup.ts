// Global test setup: registers the jest-dom matchers (toBeInTheDocument,
// toHaveAttribute, …) on Vitest's expect, with their TypeScript augmentation.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// RTL auto-cleanup only self-registers when test globals are enabled; this
// project keeps globals off (explicit vitest imports), so unmount rendered
// trees between tests here.
afterEach(() => {
  cleanup();
});
