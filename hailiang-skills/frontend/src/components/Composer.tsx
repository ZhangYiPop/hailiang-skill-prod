import { useChatStore } from "@/store/useChatStore";
import type { SkillCatalogItem } from "@/utils/api";

type ComposerProps = {
  disabled?: boolean;
  showQuickPrompts?: boolean;
  onSubmit: (value: string) => Promise<void>;
  onSelectSkill?: (skill: SkillCatalogItem) => Promise<void>;
  onExitSkill?: () => Promise<void>;
  isGenerating?: boolean;
  isCancelling?: boolean;
  onStopGeneration?: () => Promise<void>;
};

const quickPrompts = [
  "给孩子做一份生涯规划",
  "浙江物理类 580分，看看可以上哪些学校",
  "强基计划详细讲讲，我现在适合吗",
];

export function Composer({ disabled, showQuickPrompts = false, onSubmit, onSelectSkill, onExitSkill, isGenerating = false, isCancelling = false, onStopGeneration }: ComposerProps) {
  const { composerValue, setComposerValue, skillCatalog, activeSkill } = useChatStore();

  const submit = async () => {
    const trimmed = composerValue.trim();
    if (!trimmed || disabled) {
      return;
    }
    await onSubmit(trimmed);
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
              await onSubmit(prompt);
            }}
            className="rounded-full border border-white/10 bg-white/[0.04] px-3 py-2 text-xs text-slate-300 transition hover:border-cyan-400/30 hover:bg-cyan-400/10 hover:text-cyan-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {prompt}
          </button>
        ))}
      </div>
      ) : null}

      {skillCatalog.length || (activeSkill && activeSkill !== "general_chat" && onExitSkill) ? (
        <div className="space-y-2">
          <p className="px-1 text-xs text-slate-500">进入专项 Skill</p>
          <div className="flex flex-wrap gap-2">
            {activeSkill && activeSkill !== "general_chat" && onExitSkill ? (
              <button
                type="button"
                disabled={disabled}
                onClick={() => void onExitSkill()}
                className="rounded-full border border-amber-300/30 bg-amber-300/10 px-3 py-2 text-xs text-amber-100 transition hover:bg-amber-300/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                退出顾问
              </button>
            ) : null}
            {skillCatalog.map((skill) => {
              const selected = activeSkill === skill.skill_id;
              return (
                <button
                  key={skill.skill_id}
                  type="button"
                  disabled={disabled || selected || !onSelectSkill}
                  title={skill.info || skill.brief || skill.scene_name || skill.label}
                  onClick={() => {
                    if (onSelectSkill) {
                      void onSelectSkill(skill);
                    }
                  }}
                  className={[
                    "rounded-full border px-3 py-2 text-xs transition",
                    selected
                      ? "border-cyan-200/60 bg-cyan-200/15 text-cyan-100"
                      : "border-white/10 bg-white/[0.04] text-slate-300 hover:border-cyan-400/30 hover:bg-cyan-400/10 hover:text-cyan-100",
                    "disabled:cursor-not-allowed disabled:opacity-40",
                  ].join(" ")}
                >
                  {skill.label}
                </button>
              );
            })}
          </div>
        </div>
      ) : null}

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
