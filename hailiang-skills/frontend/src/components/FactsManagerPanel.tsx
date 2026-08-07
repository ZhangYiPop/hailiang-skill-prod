import { useEffect, useMemo, useState } from "react";

import {
  clearUserFactsBySource,
  getFactFormConfig,
  upsertProfileFacts,
  upsertSessionFacts,
  upsertUserFacts,
  type FactFormFieldConfig,
  type FactMap,
  type FactSourcePayload,
} from "@/utils/api";

type FactsManagerPanelProps = {
  apiBaseUrl: string;
  userId: string;
  profileId: string;
  profileName: string;
  sessionId: string;
  sharedFacts: FactMap;
  profileFacts: FactMap;
  sessionFacts: FactMap;
  onSaved: () => Promise<void>;
  onCleared: (source: FactSourcePayload) => Promise<void>;
};

function getFactRecordValue(facts: FactMap, factKey: string): unknown {
  return facts[factKey]?.value;
}

function buildDraftValuesFromFacts(
  fields: FactFormFieldConfig[],
  sharedFacts: FactMap,
  profileFacts: FactMap,
  sessionFacts: FactMap,
): Record<string, unknown> {
  return fields.reduce<Record<string, unknown>>((acc, field) => {
    if (field.scope === "shared") {
      const value = getFactRecordValue(sharedFacts, field.fact_key);
      if (value !== undefined) {
        acc[field.fact_key] = value;
      }
      return acc;
    }
    if (field.scope === "profile") {
      const value = getFactRecordValue(profileFacts, field.fact_key);
      if (value !== undefined) {
        acc[field.fact_key] = value;
      }
      return acc;
    }
    if (field.scope === "session") {
      const value = getFactRecordValue(sessionFacts, field.fact_key);
      if (value !== undefined) {
        acc[field.fact_key] = value;
      }
      return acc;
    }
    return acc;
  }, {});
}

function buildSourceGroups(sharedFacts: FactMap) {
  const grouped = new Map<
    string,
    {
      key: string;
      label: string;
      source: FactSourcePayload;
      factKeys: string[];
    }
  >();
  for (const [factKey, value] of Object.entries(sharedFacts)) {
    const source: FactSourcePayload = {
      type: value.source_type ?? "unknown",
      source_id: value.source_id ?? null,
      source_label: value.source_label ?? null,
    };
    const mapKey = `${source.type ?? ""}::${source.source_id ?? ""}::${source.source_label ?? ""}`;
    const current = grouped.get(mapKey) ?? {
      key: mapKey,
      label: source.source_label ?? source.source_id ?? source.type ?? "unknown",
      source,
      factKeys: [],
    };
    current.factKeys.push(factKey);
    grouped.set(mapKey, current);
  }
  return Array.from(grouped.values());
}

export function FactsManagerPanel({
  apiBaseUrl,
  userId,
  profileId,
  profileName,
  sessionId,
  sharedFacts,
  profileFacts,
  sessionFacts,
  onSaved,
  onCleared,
}: FactsManagerPanelProps) {
  const [open, setOpen] = useState(false);
  const [fields, setFields] = useState<FactFormFieldConfig[]>([]);
  const [draftValues, setDraftValues] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [clearingSourceKey, setClearingSourceKey] = useState("");
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    if (!open || fields.length) {
      return;
    }
    setLoading(true);
    setLocalError("");
    void getFactFormConfig(apiBaseUrl)
      .then((response) => setFields(response.fields ?? []))
      .catch((error) => setLocalError(error instanceof Error ? error.message : "facts 配置加载失败"))
      .finally(() => setLoading(false));
  }, [apiBaseUrl, fields.length, open]);

  useEffect(() => {
    if (!open || !fields.length) {
      return;
    }
    setDraftValues(buildDraftValuesFromFacts(fields, sharedFacts, profileFacts, sessionFacts));
  }, [fields, open, profileFacts, sessionFacts, sharedFacts]);

  const sourceGroups = useMemo(() => buildSourceGroups(sharedFacts), [sharedFacts]);

  const handleValueChange = (factKey: string, value: unknown) => {
    setDraftValues((current) => ({
      ...current,
      [factKey]: value,
    }));
  };

  const handleSave = async () => {
    if (!userId) {
      setLocalError("请先填写 User ID");
      return;
    }
    const sharedUpdates = fields
      .filter((field) => field.scope === "shared")
      .map((field) => ({
        key: field.fact_key,
        value: draftValues[field.fact_key],
      }))
      .filter((item) => item.value != null && item.value !== "" && (!Array.isArray(item.value) || item.value.length));
    const profileUpdates = fields
      .filter((field) => field.scope === "profile")
      .map((field) => ({
        key: field.fact_key,
        value: draftValues[field.fact_key],
      }))
      .filter((item) => item.value != null && item.value !== "" && (!Array.isArray(item.value) || item.value.length));
    const sessionUpdates = fields
      .filter((field) => field.scope === "session")
      .map((field) => ({
        key: field.fact_key,
        value: draftValues[field.fact_key],
      }))
      .filter((item) => item.value != null && item.value !== "" && (!Array.isArray(item.value) || item.value.length));

    if (!sharedUpdates.length && !profileUpdates.length && !sessionUpdates.length) {
      setLocalError("请至少填写一项信息后再保存");
      return;
    }

    setSaving(true);
    setLocalError("");
    try {
      if (sharedUpdates.length) {
        await upsertUserFacts(apiBaseUrl, userId, {
          scope: "shared",
          source: {
            type: "user_form",
            source_id: "facts_manager_modal",
            source_label: "Facts Manager Modal",
          },
          updates: sharedUpdates,
        });
      }
      if (profileUpdates.length) {
        if (!profileId) {
          throw new Error("请先选择孩子档案后再保存 profile 级 facts");
        }
        await upsertProfileFacts(apiBaseUrl, userId, profileId, {
          scope: "profile",
          source: {
            type: "user_form",
            source_id: "facts_manager_modal",
            source_label: "Facts Manager Modal",
          },
          updates: profileUpdates,
        });
      }
      if (sessionUpdates.length) {
        if (!sessionId) {
          throw new Error("请先创建会话后再保存 session 级 facts");
        }
        await upsertSessionFacts(apiBaseUrl, sessionId, {
          scope: "session",
          source: {
            type: "user_form",
            source_id: "facts_manager_modal",
            source_label: "Facts Manager Modal",
          },
          updates: sessionUpdates,
        });
      }
      setDraftValues({});
      await onSaved();
      setOpen(false);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "facts 保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <section className="rounded-[28px] border border-white/10 bg-white/[0.04] p-5">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Facts 管理</p>
            <h2 className="mt-1 text-lg font-semibold text-white">家庭 / 孩子 / 会话 Facts 管理</h2>
            <p className="mt-2 text-sm leading-6 text-slate-400">
              字段来源于后端 `facts_schema.yml` 的全部 `facts`；已配置交互规则的按规则渲染，未配置的默认使用填空。
            </p>
          </div>
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="rounded-2xl border border-cyan-300/40 bg-cyan-300/15 px-5 py-3 text-sm font-medium text-cyan-50 transition hover:bg-cyan-300/25"
          >
            填写 / 更新 Facts
          </button>
        </div>

        <div className="mt-4 flex flex-wrap gap-3">
          <div className="rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3 text-sm text-slate-200">
            家庭共享 facts：{Object.keys(sharedFacts).length} 项
          </div>
          <div className="rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3 text-sm text-slate-200">
            当前孩子 facts：{Object.keys(profileFacts).length} 项
          </div>
          <div className="rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3 text-sm text-slate-200">
            当前会话 facts：{Object.keys(sessionFacts).length} 项
          </div>
          <div className="rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3 text-sm text-slate-200">
            共享 facts 可按来源清理：{sourceGroups.length} 组
          </div>
        </div>

        <div className="mt-4 rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3 text-sm text-slate-200">
          当前孩子：{profileName || "未选择"}
        </div>

        <div className="mt-4 space-y-3">
          {sourceGroups.length ? (
            sourceGroups.map((item) => (
              <div
                key={item.key}
                className="rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3"
              >
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-semibold text-white">{item.label}</p>
                    <p className="mt-1 text-xs text-slate-500">
                      {item.source.type ?? "unknown"}
                      {item.source.source_id ? ` · ${item.source.source_id}` : ""}
                      {` · ${item.factKeys.length} 项`}
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={clearingSourceKey === item.key}
                    onClick={async () => {
                      setClearingSourceKey(item.key);
                      try {
                        await clearUserFactsBySource(apiBaseUrl, userId, { source: item.source });
                        await onCleared(item.source);
                      } finally {
                        setClearingSourceKey("");
                      }
                    }}
                    className="rounded-full border border-rose-300/30 bg-rose-300/10 px-3 py-2 text-xs text-rose-100 transition hover:bg-rose-300/20 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {clearingSourceKey === item.key ? "清理中..." : "一键清除此来源"}
                  </button>
                </div>
                <p className="mt-2 text-xs leading-6 text-slate-400">{item.factKeys.join("、")}</p>
              </div>
            ))
          ) : (
            <div className="rounded-2xl border border-dashed border-white/10 p-4 text-sm text-slate-400">
              当前没有可按来源清理的共享 facts。
            </div>
          )}
        </div>
      </section>

      {open ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4">
          <div className="max-h-[88vh] w-full max-w-4xl overflow-auto rounded-[28px] border border-white/10 bg-[#07111f] p-6 shadow-[0_30px_120px_rgba(0,0,0,0.45)]">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Facts 管理</p>
                <h3 className="mt-2 text-2xl font-semibold text-white">填写或更新 Facts</h3>
                <p className="mt-2 text-sm text-slate-400">
                  共享信息写入家庭层，孩子信息写入当前 profile，临时意图写入当前 session。
                </p>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded-full border border-white/10 px-3 py-2 text-xs text-slate-300 transition hover:border-cyan-300/30 hover:text-cyan-100"
              >
                关闭
              </button>
            </div>

            {localError ? (
              <div className="mt-4 rounded-2xl border border-amber-400/20 bg-amber-400/10 px-4 py-3 text-sm text-amber-100">
                {localError}
              </div>
            ) : null}

            {loading ? (
              <div className="mt-6 rounded-2xl border border-white/10 bg-slate-950/70 p-4 text-sm text-slate-300">
                正在加载 facts 配置...
              </div>
            ) : (
              <div className="mt-6 grid gap-4 md:grid-cols-2">
                {fields.map((field) => {
                  const currentValue = draftValues[field.fact_key];
                  return (
                    <div
                      key={field.fact_key}
                      className="rounded-2xl border border-white/10 bg-slate-950/70 p-4"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="text-sm font-semibold text-white">{field.label}</p>
                          <p className="mt-1 text-xs leading-5 text-slate-500">
                            {field.scope} · {field.input_type}
                            {field.example ? ` · ${field.example}` : ""}
                          </p>
                        </div>
                      </div>

                      {field.input_type === "single_select" ? (
                        <div className="mt-3 flex flex-wrap gap-2">
                          {(field.options ?? []).map((option) => {
                            const active = currentValue === option.value;
                            return (
                              <button
                                key={`${field.fact_key}-${option.value}`}
                                type="button"
                                onClick={() => handleValueChange(field.fact_key, option.value)}
                                className={[
                                  "rounded-full border px-3 py-2 text-xs transition",
                                  active
                                    ? "border-cyan-300/40 bg-cyan-300/20 text-cyan-50"
                                    : "border-white/10 bg-white/[0.04] text-slate-300 hover:border-cyan-300/25 hover:bg-cyan-300/10",
                                ].join(" ")}
                              >
                                {option.label}
                              </button>
                            );
                          })}
                        </div>
                      ) : null}

                      {field.input_type === "multi_select" ? (
                        <div className="mt-3 flex flex-wrap gap-2">
                          {(field.options ?? []).map((option) => {
                            const selectedValues = Array.isArray(currentValue) ? currentValue : [];
                            const active = selectedValues.includes(option.value);
                            return (
                              <button
                                key={`${field.fact_key}-${option.value}`}
                                type="button"
                                onClick={() => {
                                  const nextValue = active
                                    ? selectedValues.filter((item) => item !== option.value)
                                    : [...selectedValues, option.value];
                                  handleValueChange(field.fact_key, nextValue);
                                }}
                                className={[
                                  "rounded-full border px-3 py-2 text-xs transition",
                                  active
                                    ? "border-cyan-300/40 bg-cyan-300/20 text-cyan-50"
                                    : "border-white/10 bg-white/[0.04] text-slate-300 hover:border-cyan-300/25 hover:bg-cyan-300/10",
                                ].join(" ")}
                              >
                                {option.label}
                              </button>
                            );
                          })}
                        </div>
                      ) : null}

                      {field.input_type === "text" ? (
                        <textarea
                          value={
                            Array.isArray(currentValue)
                              ? currentValue.join("\n")
                              : typeof currentValue === "string"
                                ? currentValue
                                : currentValue == null
                                  ? ""
                                  : String(currentValue)
                          }
                          onChange={(event) =>
                            handleValueChange(
                              field.fact_key,
                              field.value_type === "string_list"
                                ? event.target.value
                                    .split(/\n|,|，/)
                                    .map((item) => item.trim())
                                    .filter(Boolean)
                                : event.target.value,
                            )
                          }
                          placeholder={
                            field.placeholder ||
                            field.example ||
                            (field.value_type === "string_list"
                              ? "请输入，多项可换行或用逗号分隔"
                              : "请输入")
                          }
                          rows={4}
                          className="mt-3 w-full rounded-2xl border border-white/10 bg-slate-950/80 px-4 py-3 text-sm text-white outline-none transition focus:border-cyan-300/40"
                        />
                      ) : null}
                    </div>
                  );
                })}
              </div>
            )}

            <div className="mt-6 flex justify-end">
              <button
                type="button"
                disabled={saving || loading}
                onClick={() => {
                  void handleSave();
                }}
                className="rounded-2xl border border-cyan-300/40 bg-cyan-300/15 px-5 py-3 text-sm font-medium text-cyan-50 transition hover:bg-cyan-300/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {saving ? "保存中..." : "保存 Facts"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
