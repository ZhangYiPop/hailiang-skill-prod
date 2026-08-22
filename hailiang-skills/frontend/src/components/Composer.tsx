import { useEffect, useState } from "react";

import { useChatStore } from "@/store/useChatStore";
import type { ExpertCatalogItem, ExpertTeamCatalogItem, SelectedExpertTeam } from "@/utils/api";

type ComposerProps = {
  disabled?: boolean;
  showQuickPrompts?: boolean;
  onSubmit: (value: string, options?: { teamMemberSwitch?: { targetExpertId: string } }) => Promise<void>;
  expertCatalog?: ExpertCatalogItem[];
  expertTeamCatalog?: ExpertTeamCatalogItem[];
  activeExpertId?: string;
  activeExpertTeam?: SelectedExpertTeam | null;
  onSelectExpert?: (expertId: string) => Promise<void>;
  onExitExpert?: () => Promise<void>;
  onSelectExpertTeam?: (teamId: string) => Promise<void>;
  onExitExpertTeam?: () => Promise<void>;
  isGenerating?: boolean;
  isCancelling?: boolean;
  onStopGeneration?: () => Promise<void>;
};

const quickPrompts = [
  "给孩子做一份生涯规划",
  "浙江物理类 580分，看看可以上哪些学校",
  "强基计划详细讲讲，我现在适合吗",
];

export function Composer({ disabled, showQuickPrompts = false, onSubmit, expertCatalog = [], expertTeamCatalog = [], activeExpertId = "", activeExpertTeam = null, onSelectExpert, onExitExpert, onSelectExpertTeam, onExitExpertTeam, isGenerating = false, isCancelling = false, onStopGeneration }: ComposerProps) {
  const { composerValue, setComposerValue } = useChatStore();
  const [toolbarTargetExpertId, setToolbarTargetExpertId] = useState("");
  const toolbarTarget = activeExpertTeam?.members.find((member) => member.expert_id === toolbarTargetExpertId);

  useEffect(() => {
    if (!activeExpertTeam?.members.some((member) => member.expert_id === toolbarTargetExpertId)) {
      setToolbarTargetExpertId("");
    }
  }, [activeExpertTeam, toolbarTargetExpertId]);

  const submit = async () => {
    const trimmed = composerValue.trim();
    if (!trimmed || disabled) {
      return;
    }
    await onSubmit(
      trimmed,
      toolbarTargetExpertId ? { teamMemberSwitch: { targetExpertId: toolbarTargetExpertId } } : undefined,
    );
    setToolbarTargetExpertId("");
  };

  return (
    <div className="space-y-4">
      {showQuickPrompts ? (
      <div className="flex flex-wrap gap-2">
        {quickPrompts.map((prompt) => (
          <button
            key={prompt}
            type="button"
            disabled={disabled}
            onClick={async () => {
              setComposerValue("");
              await onSubmit(
                prompt,
                toolbarTargetExpertId ? { teamMemberSwitch: { targetExpertId: toolbarTargetExpertId } } : undefined,
              );
              setToolbarTargetExpertId("");
            }}
            className="rounded-full border border-white/10 bg-white/[0.04] px-3 py-2 text-xs text-slate-300 transition hover:border-cyan-400/30 hover:bg-cyan-400/10 hover:text-cyan-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {prompt}
          </button>
        ))}
      </div>
      ) : null}

      {activeExpertTeam ? (
        <div className="space-y-2">
          <p className="px-1 text-xs text-violet-200">已进入专家团：{activeExpertTeam.name}</p>
          <div className="flex flex-wrap gap-2">
            {onExitExpertTeam ? (
              <button
                type="button"
                disabled={disabled}
                onClick={() => void onExitExpertTeam()}
                className="rounded-full border border-amber-300/30 bg-amber-300/10 px-3 py-2 text-xs text-amber-100 transition hover:bg-amber-300/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                退出专家团
              </button>
            ) : null}
            {activeExpertTeam.members.map((member) => {
              const selected = member.expert_id === activeExpertTeam.active_expert_id;
              return (
                <button
                  key={member.expert_id}
                  type="button"
                  disabled={disabled || selected}
                  title={member.routing_brief}
                  onClick={() => setToolbarTargetExpertId(member.expert_id)}
                  className={[
                    "rounded-full border px-3 py-2 text-xs transition",
                    selected || toolbarTargetExpertId === member.expert_id ? "border-violet-200/60 bg-violet-200/15 text-violet-100" : "border-white/10 bg-white/[0.04] text-slate-300 hover:border-violet-400/30 hover:bg-violet-400/10 hover:text-violet-100",
                    "disabled:cursor-not-allowed disabled:opacity-40",
                  ].join(" ")}
                >
                  {member.is_coordinator ? "主协调：" : "@"}{member.mention_name}
                </button>
              );
            })}
          </div>
          <p className="px-1 text-[11px] leading-5 text-slate-500">
            {toolbarTarget ? `下一条消息将由 @${toolbarTarget.mention_name} 接管。` : "点击团内专家后输入问题；当前成员不会推荐其他专家。"}
          </p>
        </div>
      ) : (
      expertCatalog.length || expertTeamCatalog.length || (activeExpertId && onExitExpert) ? (
        <div className="space-y-2">
          <p className="px-1 text-xs text-slate-500">选择专家或专家团</p>
          <div className="flex flex-wrap gap-2">
            {activeExpertId && onExitExpert ? (
              <button
                type="button"
                disabled={disabled}
                onClick={() => void onExitExpert()}
                className="rounded-full border border-amber-300/30 bg-amber-300/10 px-3 py-2 text-xs text-amber-100 transition hover:bg-amber-300/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                退出专家模式
              </button>
            ) : null}
            {expertTeamCatalog.map((team) => (
              <button
                key={team.team_id}
                type="button"
                disabled={disabled || !onSelectExpertTeam}
                title={team.description}
                onClick={() => void onSelectExpertTeam?.(team.team_id)}
                className="rounded-full border border-violet-300/25 bg-violet-300/10 px-3 py-2 text-xs text-violet-100 transition hover:bg-violet-300/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                专家团：{team.name}
              </button>
            ))}
            {expertCatalog.map((expert) => {
              const selected = activeExpertId === expert.expert_id;
              const skillLabels = expert.skills.map((skill) => skill.label).join("、");
              return (
                <button
                  key={expert.expert_id}
                  type="button"
                  disabled={disabled || selected || !onSelectExpert}
                  title={`${expert.description}\n已锁定 Skill：${skillLabels || expert.skill_ids.join("、")}`}
                  onClick={() => {
                    if (onSelectExpert) {
                      void onSelectExpert(expert.expert_id);
                    }
                  }}
                  className={[
                    "rounded-full border px-3 py-2 text-xs transition",
                    selected
                      ? "border-violet-200/60 bg-violet-200/15 text-violet-100"
                      : "border-white/10 bg-white/[0.04] text-slate-300 hover:border-violet-400/30 hover:bg-violet-400/10 hover:text-violet-100",
                    "disabled:cursor-not-allowed disabled:opacity-40",
                  ].join(" ")}
                >
                  {expert.name}
                </button>
              );
            })}
          </div>
          <p className="px-1 text-[11px] leading-5 text-slate-500">专家能力由专家包锁定；专家团由主协调专家决定是否建议转交。</p>
        </div>
      ) : null
      )}

      <div className="rounded-[28px] border border-white/10 bg-slate-950/80 p-3">
        <textarea
          value={composerValue}
          disabled={disabled}
          onChange={(event) => {
            setComposerValue(event.target.value);
          }}
          onKeyDown={async (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              await submit();
            }
          }}
          rows={4}
          placeholder="输入你要测试的对话..."
          className="min-h-[112px] w-full resize-none bg-transparent px-3 py-3 text-sm leading-7 text-slate-100 outline-none placeholder:text-slate-500"
        />
        <div className="flex items-center justify-between gap-3 border-t border-white/10 px-3 pt-3">
          <p className="text-xs text-slate-500">Enter 发送，Shift + Enter 换行</p>
          {isGenerating && onStopGeneration ? (
            <button
              type="button"
              disabled={isCancelling}
              onClick={() => void onStopGeneration()}
              className="rounded-full border border-rose-300/40 bg-rose-300/15 px-5 py-2 text-sm font-medium text-rose-50 transition hover:bg-rose-300/25 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {isCancelling ? "正在停止…" : "停止生成"}
            </button>
          ) : (
            <button
              type="button"
              disabled={disabled}
              onClick={submit}
              className="rounded-full border border-cyan-300/40 bg-cyan-300/15 px-5 py-2 text-sm font-medium text-cyan-50 transition hover:bg-cyan-300/25 disabled:cursor-not-allowed disabled:opacity-40"
            >
              发送消息
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
