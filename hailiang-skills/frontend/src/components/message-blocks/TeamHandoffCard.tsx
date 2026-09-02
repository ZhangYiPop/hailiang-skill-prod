import type { MessageInteractionState, TeamHandoff } from "@/utils/api";

type TeamHandoffCardProps = {
  handoff: TeamHandoff;
  interactionState?: MessageInteractionState;
  disabled?: boolean;
  onConfirm: (candidate: TeamHandoff["candidates"][number]) => void;
};

/** Shared, protocol-owned renderer for a coordinator's member handoff. */
export function TeamHandoffCard({
  handoff,
  interactionState,
  disabled = false,
  onConfirm,
}: TeamHandoffCardProps) {
  const status = interactionState?.status ?? handoff.status ?? "active";
  const active = status === "active";
  return (
    <div className="rounded-2xl border border-violet-300/20 bg-violet-300/[0.07] px-5 py-4">
      <p className="text-center text-xs uppercase tracking-[0.2em] text-violet-100">建议由以下专家接管</p>
      {handoff.reason ? <p className="mt-2 text-center text-xs leading-relaxed text-slate-300">{handoff.reason}</p> : null}
      <div className="mt-4 flex flex-wrap justify-center gap-3">
        {handoff.candidates.map((candidate) => {
          const selected = status === "selected" && interactionState?.selected_target_skill_id === candidate.expert_id;
          const locked = disabled || !active || selected;
          return (
            <div key={candidate.expert_id} className="flex min-w-[160px] flex-col items-center gap-1">
              <button
                type="button"
                disabled={locked}
                onClick={() => onConfirm(candidate)}
                className={[
                  "w-full rounded-full border px-5 py-2.5 text-sm font-medium transition",
                  selected
                    ? "border-violet-200/70 bg-violet-200/20 text-violet-50"
                    : "border-white/10 bg-white/[0.06] text-slate-200 hover:border-violet-300/40 hover:bg-violet-300/10 hover:text-violet-50",
                  locked ? "cursor-not-allowed opacity-40" : "",
                ].join(" ")}
              >
                {selected ? "已转交：" : "@"}{candidate.mention_name}
              </button>
              {candidate.brief ? <span className="text-center text-[11px] leading-relaxed text-slate-400">{candidate.brief}</span> : null}
            </div>
          );
        })}
      </div>
      {status === "expired" ? <p className="mt-3 text-center text-xs text-slate-400">该转交建议已失效，请以最新对话为准。</p> : null}
    </div>
  );
}
