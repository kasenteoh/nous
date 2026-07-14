// A subtle "RSS" feed-discovery link, shown near an entity page header so a
// reader can subscribe to that entity's funding + news feed without an account.
// Pure server component. `href` is the entity's feed.xml route — a plain <a>
// (not next/link), since a route handler isn't a client-navigable page, mirror-
// ing the footer's /feed.xml link.

export function RssLink({
  href,
  label = "RSS",
  title = "Subscribe to this feed (RSS)",
}: {
  href: string;
  label?: string;
  title?: string;
}) {
  return (
    <a
      href={href}
      title={title}
      className="inline-flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink transition-colors"
    >
      {/* Standard RSS glyph — inherits currentColor so it tracks the link tone
          in both themes. */}
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        width="12"
        height="12"
        fill="currentColor"
      >
        <path d="M6.18 15.64a2.18 2.18 0 1 1 0 4.36 2.18 2.18 0 0 1 0-4.36zM4 4.44A15.56 15.56 0 0 1 19.56 20h-2.83A12.73 12.73 0 0 0 4 7.27V4.44zM4 10.1A9.9 9.9 0 0 1 13.9 20h-2.83A7.07 7.07 0 0 0 4 12.93V10.1z" />
      </svg>
      {label}
    </a>
  );
}
