// A minimal chainable mock of the supabase-js query builder, for testing
// lib/queries.ts at its observable boundary: which filters were applied to
// which table, and what the code does with the scripted {data, error, count}
// response. Injected by mocking createSupabaseServerClient (lib/db.ts).
//
// Every filter/modifier method records itself and returns `this`, exactly like
// PostgrestFilterBuilder; awaiting the builder (it is a thenable, like the real
// one) resolves the response produced by the test's `respond` callback, which
// receives the fully-chained builder so it can dispatch on table + calls.

import type { SupabaseClient } from "@supabase/supabase-js";

/** Scripted response for one awaited query. */
export interface QueryResult {
  data?: unknown;
  error?: { message: string; code?: string } | null;
  count?: number | null;
}

interface ResolvedResult {
  data: unknown;
  error: { message: string; code?: string } | null;
  count: number | null;
}

export interface RecordedCall {
  method: string;
  args: unknown[];
}

export type Responder = (builder: MockQueryBuilder) => QueryResult;

function sameArgs(recorded: unknown[], expected: unknown[]): boolean {
  return expected.every(
    (arg, i) => JSON.stringify(recorded[i]) === JSON.stringify(arg),
  );
}

export class MockQueryBuilder implements PromiseLike<ResolvedResult> {
  readonly table: string;
  readonly calls: RecordedCall[] = [];
  private readonly respond: Responder;

  constructor(table: string, respond: Responder) {
    this.table = table;
    this.respond = respond;
  }

  private chain(method: string, args: unknown[]): this {
    this.calls.push({ method, args });
    return this;
  }

  select(...args: unknown[]): this {
    return this.chain("select", args);
  }
  or(...args: unknown[]): this {
    return this.chain("or", args);
  }
  is(...args: unknown[]): this {
    return this.chain("is", args);
  }
  eq(...args: unknown[]): this {
    return this.chain("eq", args);
  }
  neq(...args: unknown[]): this {
    return this.chain("neq", args);
  }
  gt(...args: unknown[]): this {
    return this.chain("gt", args);
  }
  gte(...args: unknown[]): this {
    return this.chain("gte", args);
  }
  lt(...args: unknown[]): this {
    return this.chain("lt", args);
  }
  lte(...args: unknown[]): this {
    return this.chain("lte", args);
  }
  not(...args: unknown[]): this {
    return this.chain("not", args);
  }
  in(...args: unknown[]): this {
    return this.chain("in", args);
  }
  contains(...args: unknown[]): this {
    return this.chain("contains", args);
  }
  ilike(...args: unknown[]): this {
    return this.chain("ilike", args);
  }
  order(...args: unknown[]): this {
    return this.chain("order", args);
  }
  limit(...args: unknown[]): this {
    return this.chain("limit", args);
  }
  range(...args: unknown[]): this {
    return this.chain("range", args);
  }
  single(...args: unknown[]): this {
    return this.chain("single", args);
  }

  /** True when a call to `method` was recorded whose leading args match. */
  has(method: string, ...args: unknown[]): boolean {
    return this.calls.some(
      (c) => c.method === method && sameArgs(c.args, args),
    );
  }

  /** All recorded arg lists for `method`. */
  argsOf(method: string): unknown[][] {
    return this.calls.filter((c) => c.method === method).map((c) => c.args);
  }

  /** First recorded arg for `method` at position `index`, or undefined. */
  firstArg(method: string, index = 0): unknown {
    return this.argsOf(method)[0]?.[index];
  }

  then<TResult1 = ResolvedResult, TResult2 = never>(
    onfulfilled?:
      | ((value: ResolvedResult) => TResult1 | PromiseLike<TResult1>)
      | null,
    onrejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null,
  ): PromiseLike<TResult1 | TResult2> {
    const r = this.respond(this);
    return Promise.resolve({
      data: r.data ?? null,
      error: r.error ?? null,
      count: r.count ?? null,
    }).then(onfulfilled, onrejected);
  }
}

export interface MockSupabase {
  /** Pass to vi.mocked(createSupabaseServerClient).mockReturnValue(...). */
  client: SupabaseClient;
  /** Every builder created via from(), in creation order. */
  builders: MockQueryBuilder[];
  /** Builders for one table, in creation order. */
  buildersFor(table: string): MockQueryBuilder[];
}

export function createMockSupabase(respond: Responder): MockSupabase {
  const builders: MockQueryBuilder[] = [];
  const from = (table: string): MockQueryBuilder => {
    const b = new MockQueryBuilder(table, respond);
    builders.push(b);
    return b;
  };
  // Only `.from()` is exercised by lib/queries.ts; the cast confines the test
  // double to the SupabaseClient surface the code under test actually uses.
  const client = { from } as unknown as SupabaseClient;
  return {
    client,
    builders,
    buildersFor: (table: string) => builders.filter((b) => b.table === table),
  };
}
