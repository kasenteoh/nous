// Empty stand-in for the "server-only" package under Vitest. The real package
// throws when imported outside a React Server Components environment (its
// whole job); tests import server modules like lib/queries.ts in a plain node
// process, so vitest.config.ts aliases "server-only" here.
export {};
