import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type MarkdownContentProps = {
  content: string;
  className?: string;
};

export function MarkdownContent({ content, className }: MarkdownContentProps) {
  return (
    <div className={["markdown-content text-sm leading-7", className ?? ""].join(" ").trim()}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ ...props }) => (
            <a
              {...props}
              target="_blank"
              rel="noreferrer"
              className="text-cyan-200 underline decoration-cyan-400/40 underline-offset-4 hover:text-cyan-100"
            />
          ),
          code: ({ className: codeClassName, children, ...props }) => {
            const isBlock = Boolean(codeClassName);
            if (!isBlock) {
              return (
                <code
                  {...props}
                  className="rounded bg-white/10 px-1.5 py-0.5 font-mono text-[0.92em] text-cyan-100"
                >
                  {children}
                </code>
              );
            }
            return (
              <code {...props} className={codeClassName}>
                {children}
              </code>
            );
          },
          pre: ({ children, ...props }) => (
            <pre
              {...props}
              className="overflow-x-auto rounded-2xl border border-white/10 bg-slate-950/85 p-4 text-xs leading-6 text-slate-100"
            >
              {children}
            </pre>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
