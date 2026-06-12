// Server component that emits a JSON-LD structured-data block.
//
// Every "<" is replaced with its unicode escape (backslash-u003c) per the
// Next.js JSON-LD guide: JSON.stringify does not sanitize, and parts of the
// payload (company names, descriptions) are scraped text — without this a
// value containing a closing script tag could break out of the element. The
// escape is a no-op for JSON semantics.

export function JsonLd({ data }: { data: Record<string, unknown> }) {
  return (
    <script
      type="application/ld+json"
      dangerouslySetInnerHTML={{
        __html: JSON.stringify(data).replace(/</g, "\\u003c"),
      }}
    />
  );
}
