import { useState } from "react";

import type { SseV2PathOptions } from "@/types/streamEvents";

type PathOptionsBlockProps = {
  pathOptions: SseV2PathOptions;
  onSelect: (pathName: string, prompt?: string) => void;
};

export function PathOptionsBlock({ pathOptions, onSelect }: PathOptionsBlockProps) {
  const [selectedPathId, setSelectedPathId] = useState("");
  const options = pathOptions.options ?? [];
  if (!options.length) {
    return null;
  }

  const unavailable = pathOptions.status !== "active";
  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
      <p className="text-xs uppercase tracking-[0.18em] text-slate-500">可继续展开的路径</p>
      {unavailable ? <p className="mt-2 text-xs text-slate-400">已失效，请以最新回复为准。</p> : null}
      <div className="mt-3 flex gap-3 overflow-x-auto pb-1">
        {options.map((option) => {
          const selected = selectedPathId === option.path_id;
          const enabled = option.enabled && !unavailable && !selectedPathId;
          return (
            <button
              key={option.path_id || option.title}
              type="button"
              disabled={!enabled}
              onClick={() => {
                setSelectedPathId(option.path_id || option.title);
                onSelect(option.title, option.prompt);
              }}
              className="min-w-[220px] rounded-2xl border border-white/10 bg-slate-950/70 p-4 text-left transition hover:border-cyan-300/30 hover:bg-cyan-300/10 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <p className="text-sm font-semibold text-white">{option.title}</p>
              <p className="mt-2 text-xs leading-6 text-slate-300">
                {option.description || "点击后继续展开这条路径。"}
              </p>
              {selected ? <p className="mt-3 text-[11px] text-cyan-200">已选择，正在展开</p> : null}
            </button>
          );
        })}
      </div>
    </div>
  );
}
