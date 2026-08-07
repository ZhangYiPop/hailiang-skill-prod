import { useEffect, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";

import type { StatusTimelineBlock as StatusTimelineBlockType } from "@/types/messageBlocks";
import { useChatStore } from "@/store/useChatStore";

type StatusTimelineBlockProps = {
  messageId: string;
  block: StatusTimelineBlockType;
};

const statusToneClasses: Record<string, string> = {
  completed: "border-emerald-400/20 bg-emerald-400/10 text-emerald-100",
  active: "border-cyan-400/30 bg-cyan-400/10 text-cyan-100",
  pending: "border-white/10 bg-white/[0.04] text-slate-300",
  failed: "border-rose-400/20 bg-rose-400/10 text-rose-100",
};

export function StatusTimelineBlock({ messageId, block }: StatusTimelineBlockProps) {
  const message = useChatStore((state) => state.messages.find((item) => item.id === messageId));
  const toggleReasoningExpanded = useChatStore((state) => state.toggleReasoningExpanded);
  const items = block.payload.items ?? [];
  const [timelineExpanded, setTimelineExpanded] = useState(!block.payload.collapsed);
  const reasoningContent = message?.reasoningContent ?? "";
  const reasoningStatus = message?.reasoningStatus ?? "idle";
  const reasoningExpanded = message?.reasoningExpanded ?? false;
  const shouldShowThinking = Boolean(reasoningContent) || reasoningStatus === "streaming";
  useEffect(() => {
    if (block.payload.collapsed) {
      setTimelineExpanded(false);
    }
  }, [block.payload.collapsed]);
  if (!items.length && !shouldShowThinking) {
    return null;
  }
  const shouldCollapseTimeline = Boolean(block.payload.collapsed) && items.length > 3;
  const visibleItems = shouldCollapseTimeline && !timelineExpanded ? items.slice(0, 3) : items;

  return (
    <div className="rounded-2xl border border-cyan-400/15 bg-cyan-950/20 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.18em] text-cyan-200">
            {block.payload.title ?? "推理进度"}
          </p>
          {block.payload.summary ? (
            <p className="mt-1 text-sm font-medium text-cyan-50">当前步骤：{block.payload.summary}</p>
          ) : null}
        </div>
        {shouldCollapseTimeline ? (
          <button
            type="button"
            onClick={() => setTimelineExpanded((value) => !value)}
            className="inline-flex shrink-0 items-center gap-1 rounded-full border border-cyan-300/15 px-2 py-1 text-xs text-slate-300"
          >
            {timelineExpanded ? "收起" : `共 ${items.length} 步`}
            {timelineExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          </button>
        ) : null}
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        {visibleItems.map((item, index) => (
          <div
            key={`${item.stage}-${index}`}
            className={[
              "rounded-full border px-3 py-1.5 text-xs",
              statusToneClasses[item.status ?? "pending"] ?? statusToneClasses.pending,
            ].join(" ")}
          >
            {item.label}
            {typeof item.elapsedMs === "number" ? (
              <span className="ml-1 opacity-60">{(item.elapsedMs / 1000).toFixed(1)}s</span>
            ) : null}
          </div>
        ))}
      </div>
      {shouldShowThinking ? (
        <div className="mt-3 overflow-hidden rounded-2xl border border-cyan-300/15 bg-cyan-300/[0.06]">
          <button
            type="button"
            onClick={() => toggleReasoningExpanded(messageId)}
            className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left text-xs text-cyan-100"
          >
            <span className="font-medium">
              {reasoningStatus === "streaming" ? "Thinking..." : "Thinking 已完成"}
            </span>
            <span className="inline-flex items-center gap-2 text-slate-400">
              {reasoningExpanded ? "收起" : "展开"}
              {reasoningExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </span>
          </button>
          {reasoningExpanded && reasoningContent ? (
            <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words border-t border-cyan-300/10 px-4 py-3 text-xs leading-6 text-slate-300">
              {reasoningContent}
            </pre>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
