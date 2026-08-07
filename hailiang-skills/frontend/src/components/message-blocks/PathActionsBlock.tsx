import type { PathActionsBlock as PathActionsBlockType } from "@/types/messageBlocks";
import type { MessageInteractionState } from "@/utils/api";

type PathActionsBlockProps = {
  block: PathActionsBlockType;
  onSelect: (pathName: string, description?: string) => void;
  interactionState?: MessageInteractionState;
};

export function PathActionsBlock({ block, onSelect, interactionState }: PathActionsBlockProps) {
  const actions = block.payload.actions ?? [];
  if (!actions.length) {
    return null;
  }
  const expired = interactionState?.status === "expired";

  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
      <p className="text-xs uppercase tracking-[0.18em] text-slate-500">可继续展开的路径</p>
      {expired ? <p className="mt-2 text-xs text-slate-400">已失效，请以最新回复为准。</p> : null}
      <div className="mt-3 flex gap-3 overflow-x-auto pb-1">
        {actions.map((action) => (
          <button
            key={`${action.path_id ?? action.path_name}`}
            type="button"
            disabled={expired}
            onClick={() => onSelect(action.path_name, action.description)}
            className="min-w-[220px] rounded-2xl border border-white/10 bg-slate-950/70 p-4 text-left transition hover:border-cyan-300/30 hover:bg-cyan-300/10 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <p className="text-sm font-semibold text-white">{action.path_name}</p>
            <p className="mt-2 text-xs leading-6 text-slate-300">
              {action.description || "点击后继续展开这条路径。"}
            </p>
            {action.source?.record_id || action.source?.sheet ? (
              <p className="mt-3 text-[11px] text-slate-500">
                {action.source?.record_id ? `ID ${action.source.record_id}` : ""}
                {action.source?.sheet ? ` · ${action.source.sheet}` : ""}
              </p>
            ) : null}
          </button>
        ))}
      </div>
    </div>
  );
}
