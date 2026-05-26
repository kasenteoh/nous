// Server-renderable Markdown renderer using react-markdown + remark-gfm.
// No "use client" directive — react-markdown works fine in RSC.
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose prose-zinc dark:prose-invert prose-sm max-w-none prose-headings:font-semibold prose-a:underline">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}
