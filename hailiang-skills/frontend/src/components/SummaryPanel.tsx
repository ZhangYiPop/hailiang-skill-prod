import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { StatusPill } from "@/components/StatusPill";
import { copyToClipboard } from "@/utils/clipboard";
import type {
  AdmissionState,
  AssetSupport,
  CandidatePath,
  FactMap,
  MessageResponse,
  SchoolIntroState,
} from "@/utils/api";

type SummaryPanelProps = {
  lastResponse: MessageResponse | null;
  activeSkill: string;
  candidatePaths: CandidatePath[];
  userId: string;
  profileName: string;
  sessionTitle: string;
  sharedFacts: FactMap;
  profileFacts: FactMap;
  sessionFacts: FactMap;
  effectiveFacts: FactMap;
  skillStates: Record<string, Record<string, unknown>>;
};

function CopyCardButton({ content }: { content: string }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const copied = copyState === "copied";

  return (
    <button
      type="button"
      onClick={() => {
        void copyToClipboard(content).then((ok) => {
          setCopyState(ok ? "copied" : "failed");
          window.setTimeout(() => setCopyState("idle"), 1600);
        });
      }}
      className="inline-flex items-center gap-1 rounded-full border border-white/10 bg-slate-950/70 px-3 py-1 text-xs text-slate-300 transition hover:border-cyan-400/30 hover:text-cyan-100"
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
      {copied ? "已复制" : copyState === "failed" ? "复制失败" : "复制卡片"}
    </button>
  );
}

function FactsSnapshotSection({ title, facts }: { title: string; facts: FactMap }) {
  const factEntries = Object.entries(facts);
  return (
    <div className="rounded-2xl border border-white/10 bg-slate-950/40 p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs uppercase tracking-[0.18em] text-slate-500">{title}</p>
        <CopyCardButton content={JSON.stringify(facts, null, 2)} />
      </div>
      <div className="mt-3 space-y-3">
        {factEntries.length ? (
          factEntries.map(([key, value]) => (
            <div
              key={`${title}-${key}`}
              className="rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3"
            >
              <p className="font-mono text-xs uppercase tracking-[0.16em] text-cyan-200">{key}</p>
              <pre className="mt-2 whitespace-pre-wrap break-all text-xs leading-6 text-slate-300">
                {JSON.stringify(value, null, 2)}
              </pre>
            </div>
          ))
        ) : (
          <div className="rounded-2xl border border-dashed border-white/10 p-4 text-sm text-slate-400">
            暂无数据。
          </div>
        )}
      </div>
    </div>
  );
}

const missingSlotLabels: Record<string, string> = {
  student_region: "户籍地区",
  family_type: "家庭类型",
  ethnicity: "民族信息",
  hukou_years: "连续户籍年限",
  guardian_hukou_match: "监护人户籍一致性",
  school_status_years: "连续学籍年限",
  special_identity_tags: "竞赛/奖项身份",
  career_orientation: "职业兴趣",
  physical_requirements: "身高",
};

const blockingReasonLabels: Record<string, string> = {
  province_mismatch: "省份条件不匹配",
  region_mismatch: "户籍地区条件不匹配",
  subject_mismatch: "选科条件不匹配",
  score_band_mismatch: "分数段条件不匹配",
  ethnicity_mismatch: "民族条件不匹配",
  family_type_mismatch: "家庭类型条件不匹配",
  budget_mismatch: "预算条件不匹配",
};

export function SummaryPanel({
  lastResponse,
  activeSkill,
  candidatePaths,
  userId,
  profileName,
  sessionTitle,
  sharedFacts,
  profileFacts,
  sessionFacts,
  effectiveFacts,
  skillStates,
}: SummaryPanelProps) {
  const admissionState = (lastResponse?.admission_state ??
    (skillStates.admission as AdmissionState | undefined) ??
    {}) as AdmissionState;
  const schoolIntroState = (lastResponse?.school_intro_state ??
    (skillStates.school_intro as SchoolIntroState | undefined) ??
    {}) as SchoolIntroState;
  const currentAssetSupport = (
    lastResponse?.asset_support ??
    (activeSkill ? ((skillStates[activeSkill]?.asset_support as AssetSupport | undefined) ?? undefined) : undefined)
  ) as AssetSupport | undefined;
  const groupedCandidatePaths = {
    feasible: candidatePaths.filter((item) => item.feasibility_status === "feasible"),
    partial: candidatePaths.filter((item) => item.feasibility_status === "partial"),
    infeasible: candidatePaths.filter((item) => item.feasibility_status === "infeasible"),
  };
  const debugStates = [
    ["career_plan_entity", lastResponse?.career_plan_state ?? lastResponse?.main_planner_state ?? skillStates.career_plan_entity ?? skillStates.main_planner ?? {}],
    ["router", lastResponse?.router_state ?? skillStates.router ?? {}],
    ["facts_extractor", lastResponse?.facts_extractor_state ?? skillStates.facts_extractor ?? {}],
    ["planner", lastResponse?.planner_state ?? skillStates.planner ?? {}],
    ["ranking", lastResponse?.ranking_snapshot ?? skillStates.convergence?.ranking_snapshot ?? {}],
  ] as const;

  const sections = [
    {
      key: "feasible",
      title: "可行",
      tone: "success" as const,
      items: groupedCandidatePaths.feasible,
      emptyText: "当前没有明确可行的路径。",
    },
    {
      key: "partial",
      title: "部分条件满足",
      tone: "warning" as const,
      items: groupedCandidatePaths.partial,
      emptyText: "当前没有需要补充信息的路径。",
    },
    {
      key: "infeasible",
      title: "当前不满足",
      tone: "danger" as const,
      items: groupedCandidatePaths.infeasible,
      emptyText: "当前没有明确不满足的路径。",
    },
  ];
  return (
    <div className="space-y-4">
      <section className="rounded-[28px] border border-white/10 bg-white/[0.04] p-5">
        <div className="mb-4 flex items-center justify-between gap-4">
          <div>
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">响应摘要</p>
            <h3 className="mt-2 text-lg font-semibold text-white">最近一次返回</h3>
          </div>
          <StatusPill
            label={activeSkill || "未激活"}
            tone={activeSkill ? "success" : "default"}
          />
        </div>

        <div className="grid gap-3">
          <div className="rounded-2xl border border-white/10 bg-slate-950/70 p-4">
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">user_id / profile</p>
            <p className="mt-2 text-sm text-slate-200">
              {userId || "--"}
              {profileName ? ` · ${profileName}` : ""}
            </p>
          </div>
          <div className="rounded-2xl border border-white/10 bg-slate-950/70 p-4">
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">session_title</p>
            <p className="mt-2 text-sm text-slate-200">{sessionTitle || "发送第一条消息后自动生成"}</p>
          </div>
          <div className="rounded-2xl border border-white/10 bg-slate-950/70 p-4">
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">session_log_dir</p>
            <p className="mt-2 break-all text-sm text-slate-200">
              {lastResponse?.session_log_dir ?? "发送消息后显示本次会话日志目录"}
            </p>
          </div>
          <div className="rounded-2xl border border-white/10 bg-slate-950/70 p-4">
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">facts_updated</p>
            <p className="mt-2 text-sm text-slate-200">
              {lastResponse?.facts_updated?.length
                ? lastResponse.facts_updated.join("、")
                : "暂无事实字段更新"}
            </p>
          </div>
          <div className="rounded-2xl border border-white/10 bg-slate-950/70 p-4">
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">risk_alerts</p>
            <p className="mt-2 text-sm text-slate-200">
              {lastResponse?.risk_alerts?.length
                ? lastResponse.risk_alerts.join("、")
                : "当前没有风险提示"}
            </p>
          </div>
        </div>
      </section>

      <section className="rounded-[28px] border border-white/10 bg-white/[0.04] p-5">
        <div className="mb-4 flex items-center justify-between gap-4">
          <div>
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">资产支撑</p>
            <h3 className="mt-2 text-lg font-semibold text-white">当前 Skill 可用资产</h3>
          </div>
          <StatusPill label={activeSkill || "未激活"} tone={activeSkill ? "success" : "default"} />
        </div>

        {currentAssetSupport ? (
          <div className="space-y-3">
            <div className="rounded-2xl border border-white/10 bg-slate-950/70 p-4">
              <p className="text-xs uppercase tracking-[0.18em] text-slate-500">覆盖维度</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {currentAssetSupport.supported_dimensions?.length ? (
                  currentAssetSupport.supported_dimensions.map((dimension) => (
                    <StatusPill key={dimension} label={dimension} tone="success" />
                  ))
                ) : (
                  <p className="text-sm text-slate-300">当前没有可展示的覆盖维度。</p>
                )}
              </div>
            </div>

            <div className="rounded-2xl border border-white/10 bg-slate-950/70 p-4">
              <p className="text-xs uppercase tracking-[0.18em] text-slate-500">未覆盖维度</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {currentAssetSupport.dynamic_unavailable_dimensions?.length ? (
                  currentAssetSupport.dynamic_unavailable_dimensions.map((dimension) => (
                    <StatusPill key={dimension} label={dimension} tone="warning" />
                  ))
                ) : (
                  <p className="text-sm text-slate-300">当前没有明显缺失的动态维度。</p>
                )}
              </div>
            </div>

            <div className="rounded-2xl border border-white/10 bg-slate-950/70 p-4">
              <p className="text-xs uppercase tracking-[0.18em] text-slate-500">资产列表</p>
              <div className="mt-3 space-y-3">
                {currentAssetSupport.available_assets?.length ? (
                  currentAssetSupport.available_assets.map((asset) => (
                    <div
                      key={`${asset.path ?? asset.title ?? "asset"}`}
                      className="rounded-2xl border border-white/10 bg-slate-950/50 p-3"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="text-sm font-semibold text-white">{asset.title ?? asset.path}</p>
                          <p className="mt-1 break-all text-xs text-slate-500">{asset.path}</p>
                        </div>
                        <div className="flex flex-wrap justify-end gap-2">
                          <StatusPill
                            label={asset.enabled === false ? "disabled" : "enabled"}
                            tone={asset.enabled === false ? "warning" : "success"}
                          />
                          <StatusPill label={asset.count != null ? `${asset.count}` : "unknown"} />
                        </div>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-2">
                        {asset.supports?.map((dimension) => (
                          <StatusPill key={`${asset.path}-${dimension}`} label={dimension} />
                        ))}
                      </div>
                    </div>
                  ))
                ) : (
                  <p className="text-sm text-slate-300">当前没有可展示的资产清单。</p>
                )}
              </div>
            </div>
          </div>
        ) : (
          <div className="rounded-2xl border border-dashed border-white/10 p-4 text-sm text-slate-400">
            发送消息后，这里会展示当前 skill 的可用资产、覆盖维度和未覆盖维度。
          </div>
        )}
      </section>

      <section className="rounded-[28px] border border-white/10 bg-white/[0.04] p-5">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">模拟升学命中</p>
            <h3 className="mt-2 text-lg font-semibold text-white">学校与推荐路径</h3>
          </div>
          <StatusPill label={`${admissionState.matched_count ?? 0} 条`} />
        </div>

        <div className="space-y-3">
          {admissionState.matched_items_brief?.length ? (
            admissionState.matched_items_brief.map((item, index) => (
              <div
                key={`${item.region_variant ?? "unknown"}-${item.tier_name ?? "tier"}-${index}`}
                className="rounded-2xl border border-white/10 bg-slate-950/70 p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-sm font-semibold text-white">
                      {item.region_variant ?? admissionState.province ?? "未命名档位"}
                    </p>
                    <p className="mt-1 text-xs text-slate-500">
                      {item.tier_name ?? "未知层次"}
                      {item.subject_group ? ` · ${item.subject_group}` : ""}
                    </p>
                  </div>
                  <StatusPill
                    label={
                      item.score_range?.min_score != null && item.score_range?.max_score != null
                        ? `${item.score_range.min_score}-${item.score_range.max_score}`
                        : "分数待定"
                    }
                  />
                </div>

                {item.sample_schools?.length ? (
                  <div className="mt-3">
                    <p className="text-xs uppercase tracking-[0.16em] text-slate-500">代表院校</p>
                    <p className="mt-2 text-sm leading-6 text-slate-200">
                      {item.sample_schools.join("、")}
                    </p>
                  </div>
                ) : null}

                {item.recommended_paths?.length ? (
                  <div className="mt-3">
                    <p className="text-xs uppercase tracking-[0.16em] text-slate-500">推荐路径</p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {item.recommended_paths.map((pathName) => (
                        <StatusPill key={`${index}-${pathName}`} label={pathName} tone="success" />
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            ))
          ) : (
            <div className="rounded-2xl border border-dashed border-white/10 p-4 text-sm text-slate-400">
              进入模拟升学命中分数档后，这里会展示对应学校层次、代表院校和推荐路径。
            </div>
          )}
        </div>
      </section>

      <section className="rounded-[28px] border border-white/10 bg-white/[0.04] p-5">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">学校问询</p>
            <h3 className="mt-2 text-lg font-semibold text-white">命中学校</h3>
          </div>
          <StatusPill label={`${schoolIntroState.matched_count ?? 0} 条`} />
        </div>
        {schoolIntroState.matched_school_names?.length ? (
          <div className="flex flex-wrap gap-2">
            {schoolIntroState.matched_school_names.map((schoolName) => (
              <StatusPill key={schoolName} label={schoolName} tone="success" />
            ))}
          </div>
        ) : (
          <div className="rounded-2xl border border-dashed border-white/10 p-4 text-sm text-slate-400">
            命中具体学校后，这里会展示学校问询的结构化结果。
          </div>
        )}
      </section>

      <section className="rounded-[28px] border border-white/10 bg-white/[0.04] p-5">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">候选路径</p>
            <h3 className="mt-2 text-lg font-semibold text-white">收敛结果</h3>
          </div>
          <StatusPill label={`${candidatePaths.length} 条`} />
        </div>

        <div className="space-y-3">
          {candidatePaths.length ? (
            sections.map((section) => (
              <div key={section.key} className="rounded-2xl border border-white/10 bg-slate-950/40 p-4">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-semibold text-white">{section.title}</p>
                    <p className="mt-1 text-xs text-slate-500">{section.items.length} 条</p>
                  </div>
                  <StatusPill label={section.title} tone={section.tone} />
                </div>
                <div className="space-y-3">
                  {section.items.length ? (
                    section.items.map((item) => (
                      <div
                        key={`${section.key}-${item.path_id}-${item.primary_category}`}
                        className="rounded-2xl border border-white/10 bg-slate-950/70 p-4"
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div>
                            <p className="text-sm font-semibold text-white">
                              {item.primary_category ?? "未命名路径"}
                            </p>
                            <p className="mt-1 text-xs text-slate-500">
                              {item.path_id ?? "无 path_id"}
                              {item.sheet_group ? ` · ${item.sheet_group}` : ""}
                            </p>
                          </div>
                          <div className="flex flex-wrap items-center justify-end gap-2">
                            <StatusPill label={item.feasibility_label ?? section.title} tone={section.tone} />
                            <StatusPill
                              label={item.risk_level ?? "unknown"}
                              tone={
                                item.risk_level === "high"
                                  ? "danger"
                                  : item.risk_level === "medium"
                                    ? "warning"
                                    : "default"
                              }
                            />
                          </div>
                        </div>
                        <p className="mt-3 text-sm leading-6 text-slate-300">
                          {item.reasons?.[0] ?? "暂无推荐理由"}
                        </p>
                        {item.missing_slots?.length ? (
                          <p className="mt-2 text-xs leading-6 text-amber-200">
                            缺失信息：
                            {item.missing_slots
                              .map((slot) => missingSlotLabels[slot] ?? slot)
                              .join("、")}
                          </p>
                        ) : null}
                        {item.blocking_reasons?.length ? (
                          <p className="mt-2 text-xs leading-6 text-rose-200">
                            不满足原因：
                            {item.blocking_reasons
                              .map((reason) => blockingReasonLabels[reason] ?? reason)
                              .join("、")}
                          </p>
                        ) : null}
                        {item.target_users ? (
                          <p className="mt-2 text-xs leading-6 text-slate-400">
                            适用对象：{item.target_users}
                          </p>
                        ) : null}
                      </div>
                    ))
                  ) : (
                    <div className="rounded-2xl border border-dashed border-white/10 p-4 text-sm text-slate-400">
                      {section.emptyText}
                    </div>
                  )}
                </div>
              </div>
            ))
          ) : (
            <div className="rounded-2xl border border-dashed border-white/10 p-4 text-sm text-slate-400">
              发送多元路径相关消息后，这里会出现候选路径。
            </div>
          )}
        </div>
      </section>

      <section className="rounded-[28px] border border-white/10 bg-white/[0.04] p-5">
        <div className="mb-4">
          <p className="text-xs uppercase tracking-[0.22em] text-slate-500">事实快照</p>
          <h3 className="mt-2 text-lg font-semibold text-white">共享 / 孩子 / 会话 / 生效 facts</h3>
        </div>

        <div className="space-y-4">
          <FactsSnapshotSection title="家庭共享 facts" facts={sharedFacts} />
          <FactsSnapshotSection title="当前孩子 facts" facts={profileFacts} />
          <FactsSnapshotSection title="会话临时 facts" facts={sessionFacts} />
          <FactsSnapshotSection title="当前生效 facts" facts={effectiveFacts} />
        </div>
      </section>

      <section className="rounded-[28px] border border-white/10 bg-white/[0.04] p-5">
        <div className="mb-4">
          <p className="text-xs uppercase tracking-[0.22em] text-slate-500">决策链路</p>
          <h3 className="mt-2 text-lg font-semibold text-white">Main Planner / Router / Facts / Planner / Ranking</h3>
        </div>

        <div className="space-y-3">
          {debugStates.map(([key, value]) => (
            <div
              key={key}
              className="rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3"
            >
              <div className="flex items-start justify-between gap-3">
                <p className="font-mono text-xs uppercase tracking-[0.16em] text-emerald-200">{key}</p>
                <CopyCardButton content={JSON.stringify(value, null, 2)} />
              </div>
              <pre className="mt-2 whitespace-pre-wrap break-all text-xs leading-6 text-slate-300">
                {JSON.stringify(value, null, 2)}
              </pre>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
