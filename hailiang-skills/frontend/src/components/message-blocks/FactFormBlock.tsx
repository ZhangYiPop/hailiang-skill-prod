import { useState } from "react";

import { useChatStore } from "@/store/useChatStore";
import type { FactFormBlock as FactFormBlockType, FactFormField } from "@/types/messageBlocks";
import type { MessageInteractionState } from "@/utils/api";

type FactFormBlockProps = {
  messageId: string;
  block: FactFormBlockType;
  onSubmit: (
    messageId: string,
    formId: string,
    fields: FactFormField[],
    draftValues: Record<string, unknown>,
  ) => Promise<void>;
  interactionState?: MessageInteractionState;
};

export function FactFormBlock({ messageId, block, onSubmit, interactionState }: FactFormBlockProps) {
  const { formDrafts, setFormDraftValue } = useChatStore();
  const [submitting, setSubmitting] = useState(false);
  const formId = block.payload.form_id;
  const fields = block.payload.fields ?? [];
  const draftValues = formDrafts[formId] ?? {};
  // This is a cheap derived value rather than a Hook.  Keeping it outside the
  // conditional returns avoids changing Hook order when the form becomes
  // `submitted` after the user clicks an option.
  const canAutoSubmit = fields.length === 1 && fields[0]?.input_type === "single_select" && fields[0]?.submit_mode === "auto";

  if (interactionState?.status === "submitted") {
    return <div className="rounded-2xl border border-emerald-400/20 bg-emerald-400/5 p-4 text-sm text-emerald-100">已完成补充信息</div>;
  }
  if (interactionState?.status === "expired") {
    return <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4 text-sm text-slate-400">该表单已失效，请以最新回复为准。</div>;
  }

  if (!fields.length) {
    return null;
  }

  const submit = async (nextDrafts: Record<string, unknown>) => {
    setSubmitting(true);
    try {
      await onSubmit(messageId, formId, fields, nextDrafts);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="rounded-2xl border border-amber-400/20 bg-amber-400/5 p-4">
      <p className="text-xs uppercase tracking-[0.18em] text-amber-200">
        {block.payload.title ?? "补充信息"}
      </p>
      <div className="mt-3 space-y-4">
        {fields.map((field) => {
          const currentValue = draftValues[field.fact_key];
          const commonHint = field.example || field.placeholder;

          if (field.input_type === "single_select") {
            return (
              <div key={field.fact_key} className="space-y-2">
                <p className="text-sm font-medium text-white">{field.label}</p>
                {commonHint ? <p className="text-xs text-slate-400">{commonHint}</p> : null}
                <div className="flex flex-wrap gap-2">
                  {(field.options ?? []).map((option) => {
                    const active = currentValue === option.value;
                    return (
                      <button
                        key={`${field.fact_key}-${option.value}`}
                        type="button"
                        disabled={submitting}
                        onClick={() => {
                          const nextDrafts = {
                            ...draftValues,
                            [field.fact_key]: option.value,
                          };
                          setFormDraftValue(formId, field.fact_key, option.value);
                          if (canAutoSubmit) {
                            void submit(nextDrafts);
                          }
                        }}
                        className={[
                          "rounded-full border px-3 py-2 text-xs transition",
                          active
                            ? "border-cyan-300/40 bg-cyan-300/20 text-cyan-50"
                            : "border-white/10 bg-white/[0.04] text-slate-300 hover:border-cyan-300/30 hover:bg-cyan-300/10",
                        ].join(" ")}
                      >
                        {option.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          }

          if (field.input_type === "multi_select") {
            const selectedValues = Array.isArray(currentValue) ? currentValue : [];
            const maxSelections = field.max_selections;
            const selectionLimitReached = Boolean(maxSelections && selectedValues.length >= maxSelections);
            return (
              <div key={field.fact_key} className="space-y-2">
                <p className="text-sm font-medium text-white">{field.label}</p>
                {commonHint ? <p className="text-xs text-slate-400">{commonHint}</p> : null}
                {maxSelections ? <p className="text-xs text-slate-400">最多选择 {maxSelections} 项</p> : null}
                <div className="flex flex-wrap gap-2">
                  {(field.options ?? []).map((option) => {
                    const active = selectedValues.includes(option.value);
                    return (
                      <button
                        key={`${field.fact_key}-${option.value}`}
                        type="button"
                        disabled={submitting || (!active && selectionLimitReached)}
                        onClick={() => {
                          const nextValues = active
                            ? selectedValues.filter((item) => item !== option.value)
                            : [...selectedValues, option.value];
                          setFormDraftValue(formId, field.fact_key, nextValues);
                        }}
                        className={[
                          "rounded-full border px-3 py-2 text-xs transition",
                          active
                            ? "border-cyan-300/40 bg-cyan-300/20 text-cyan-50"
                            : "border-white/10 bg-white/[0.04] text-slate-300 hover:border-cyan-300/30 hover:bg-cyan-300/10",
                        ].join(" ")}
                      >
                        {option.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          }

          return (
            <label key={field.fact_key} className="block">
              <span className="text-sm font-medium text-white">{field.label}</span>
              {commonHint ? <span className="mt-1 block text-xs text-slate-400">{commonHint}</span> : null}
              <input
                value={typeof currentValue === "string" ? currentValue : ""}
                disabled={submitting}
                onChange={(event) => setFormDraftValue(formId, field.fact_key, event.target.value)}
                placeholder={field.placeholder || field.example || "请输入"}
                className="mt-2 w-full rounded-2xl border border-white/10 bg-slate-950/80 px-4 py-3 text-sm text-white outline-none transition focus:border-cyan-300/40"
              />
            </label>
          );
        })}
      </div>

      {!canAutoSubmit ? (
        <div className="sticky bottom-0 mt-4 border-t border-white/10 pt-4">
          <button
            type="button"
            disabled={submitting}
            onClick={() => {
              void submit(draftValues);
            }}
            className="w-full rounded-2xl border border-cyan-300/40 bg-cyan-300/15 px-4 py-3 text-sm font-medium text-cyan-50 transition hover:bg-cyan-300/25 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {submitting ? "提交中..." : "提交补充信息"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
