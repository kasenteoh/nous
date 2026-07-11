// W-C.2: a missing/partial Supabase config must fail LOUDLY where it is a
// deployment mistake (Vercel) and degrade to "not configured" everywhere else
// (secret-free CI, local dev). The policy lives in createSupabaseServerClient.
import { afterEach, describe, expect, it, vi } from "vitest";

import { createSupabaseServerClient, SupabaseConfigError } from "@/lib/db";

afterEach(() => {
  vi.unstubAllEnvs();
});

function stubEnv(vars: Record<string, string | undefined>): void {
  for (const key of ["VERCEL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"]) {
    vi.stubEnv(key, vars[key] ?? "");
  }
}

describe("createSupabaseServerClient config policy", () => {
  it("throws a benign Error off-Vercel when env is missing (CI/dev degrade path)", () => {
    stubEnv({});
    expect(() => createSupabaseServerClient()).toThrowError(Error);
    expect(() => createSupabaseServerClient()).not.toThrowError(
      SupabaseConfigError,
    );
  });

  it("stays benign off-Vercel on PARTIAL config (lint.yml plants a canary key without a URL)", () => {
    stubEnv({ SUPABASE_SERVICE_ROLE_KEY: "canary-key-not-real" });
    expect(() => createSupabaseServerClient()).not.toThrowError(
      SupabaseConfigError,
    );
  });

  it("throws SupabaseConfigError on Vercel when env is missing", () => {
    stubEnv({ VERCEL: "1" });
    expect(() => createSupabaseServerClient()).toThrowError(
      SupabaseConfigError,
    );
  });

  it("throws SupabaseConfigError on Vercel on partial config, naming the missing var", () => {
    stubEnv({ VERCEL: "1", SUPABASE_URL: "https://example.supabase.co" });
    expect(() => createSupabaseServerClient()).toThrowError(
      /SUPABASE_SERVICE_ROLE_KEY/,
    );
  });

  it("constructs a client when both vars are present, on or off Vercel", () => {
    stubEnv({
      VERCEL: "1",
      SUPABASE_URL: "https://example.supabase.co",
      SUPABASE_SERVICE_ROLE_KEY: "not-a-real-key-but-long-enough",
    });
    expect(createSupabaseServerClient()).toBeTruthy();
  });
});
