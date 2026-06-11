// Server-renderable Markdown renderer using react-markdown + remark-gfm.
// No "use client" directive — react-markdown works fine in RSC.
// Colors come from the semantic theme tokens via prose element modifiers, so
// the output follows the light/dark toggle without prose-invert.
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose prose-sm max-w-none text-ink-soft prose-headings:font-semibold prose-headings:text-ink prose-p:text-ink-soft prose-strong:text-ink prose-a:text-accent prose-a:underline prose-li:text-ink-soft prose-li:marker:text-ink-muted prose-code:text-ink prose-blockquote:text-ink-soft prose-blockquote:border-edge prose-hr:border-edge">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}
