import { Check, ChevronDown, ChevronUp, Copy, Download, RefreshCw } from "lucide-react";

import { StatusPill } from "@/components/StatusPill";
import { copyToClipboard } from "@/utils/clipboard";
import type { SkillEvent } from "@/utils/api";
import { useState } from "react";

type EventPanelProps = {
  events: SkillEvent[];
  loading?: boolean;
  onRefresh: () => Promise<void>;
  onDownloadLogs?: () => Promise<void>;
  canDownloadLogs?: boolean;
};

function formatEventTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

function CopyCardButton({
  content,
  className = "",
}: {
  content: string;
  className?: string;
}) {
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
      className={`inline-flex items-center gap-1 rounded-full border border-white/10 bg-slate-950/70 px-3 py-1 text-xs text-slate-300 transition hover:border-cyan-400/30 hover:text-cyan-100 ${className}`}
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
      {copied ? "已复制" : copyState === "failed" ? "复制失败" : "复制卡片"}
    </button>
  );
}

function formatPromptAssemblyPayload(payload: Record<string, unknown>) {
  return JSON.stringify(payload, null, 2);
}

function formatUnknown(value: unknown) {
  if (value === undefined || value === null || value === "") {
    return "(none)";
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value, null, 2);
}

/** Event payloads may be redacted into an object ({ length, sha256, preview }).
 * Never pass those values straight into JSX. */
function displayText(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (value && typeof value === "object" && "preview" in value) {
    const preview = (value as { preview?: unknown }).preview;
    if (typeof preview === "string") {
      return preview;
    }
  }
  return value == null ? "" : formatUnknown(value);
}

function displayTextList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(displayText).filter(Boolean) : [];
}

function formatEventCardContent(event: SkillEvent) {
  return JSON.stringify(
    {
      event_type: event.event_type,
      created_at: event.created_at,
      payload: event.payload,
    },
    null,
    2,
  );
}

function eventTurnId(event: SkillEvent) {
  const value = event.payload.turn_id;
  return typeof value === "string" && value ? value : "";
}

function eventTurnIndex(event: SkillEvent) {
  const value = event.payload.turn_index;
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function EventTurnPills({ event }: { event: SkillEvent }) {
  const turnIndex = eventTurnIndex(event);
  const turnId = eventTurnId(event);
  return (
    <>
      {turnIndex !== null && <StatusPill label={`第 ${turnIndex} 轮`} tone="info" />}
      {turnId && <StatusPill label={turnId} />}
    </>
  );
}

function buildRetrievalEventFromPrompt(event: SkillEvent): SkillEvent | null {
  const payload = event.payload;
  if (event.event_type !== "prompt_assembly" || payload.layer !== "retrieval") {
    return null;
  }
  const promptContent = String(payload.prompt_content ?? "");
  const retrievedCount = Number(payload.retrieved_count ?? 0);
  if (!promptContent || retrievedCount <= 0) {
    return null;
  }
  const sources = (payload.retrieved_sources as string[] | undefined) ?? [];
  const chunks = promptContent
    .split(/(?=## Supporting Snippet \d+)/g)
    .filter((chunk) => chunk.startsWith("## Supporting Snippet"));
  const items = chunks.map((chunk, index) => {
    const sourceKind = chunk.match(/source_kind=([^\n]+)/)?.[1]?.trim() ?? "retrieved";
    const score = Number(chunk.match(/score=([0-9.-]+)/)?.[1] ?? 0);
    const snippet = chunk
      .replace(/^## Supporting Snippet \d+\s*/g, "")
      .replace(/source_kind=[^\n]+\n?/g, "")
      .replace(/score=[0-9.-]+\n?/g, "")
      .trim();
    return {
      index: index + 1,
      source_type: sourceKind,
      source_path: sources[index] ?? "",
      title: sources[index] ?? `Supporting Snippet ${index + 1}`,
      score,
      snippet,
    };
  });
  if (!items.length) {
    return null;
  }
  return {
    ...event,
    event_id: `${event.event_id}_retrieval_context`,
    event_type: "retrieval_context",
    payload: {
      phase: payload.phase,
      skill_name: payload.skill_name,
      skill_id: payload.skill_id,
      skill_type: payload.skill_type,
      retrieved_count: retrievedCount,
      generated_asset_domains: payload.generated_asset_domains,
      local_asset_paths: payload.local_asset_paths,
      items,
      derived_from_event_id: event.event_id,
      turn_id: payload.turn_id,
      turn_index: payload.turn_index,
    },
  };
}

function CurrentTurnReferencePanel({ events }: { events: SkillEvent[] }) {
  const latestTurnId = [...events].reverse().map(eventTurnId).find(Boolean) ?? "";
  const latestTurnIndex =
    [...events]
      .reverse()
      .map(eventTurnIndex)
      .find((value): value is number => value !== null) ?? null;
  const referenceEvents = events.filter((event) => event.event_type === "reference_context");
  const currentReferenceEvents = latestTurnId
    ? referenceEvents.filter((event) => eventTurnId(event) === latestTurnId)
    : referenceEvents.slice(-1);
  const contextReferenceEvents = currentReferenceEvents.filter((event) => {
    const sourceType = String(event.payload.source_event_type ?? "");
    const phase = String(event.payload.phase ?? "");
    return sourceType === "retrieval_context" || phase === "runtime_final_response";
  });
  const loadedReferenceEvents = currentReferenceEvents.filter((event) => {
    const sourceType = String(event.payload.source_event_type ?? "");
    const phase = String(event.payload.phase ?? "");
    return sourceType === "ms_agent_runtime" || phase === "ms_agent_lazy_load";
  });
  const items = contextReferenceEvents.flatMap(
    (event) => (event.payload.items as Array<Record<string, unknown>> | undefined) ?? [],
  );
  const loadedItems = loadedReferenceEvents.flatMap(
    (event) => (event.payload.items as Array<Record<string, unknown>> | undefined) ?? [],
  );
  const skillNames = Array.from(
    new Set(
      currentReferenceEvents
        .map((event) => String(event.payload.skill_name ?? event.payload.skill_id ?? ""))
        .filter(Boolean),
    ),
  );

  return (
    <div className="mb-4 rounded-2xl border border-amber-400/20 bg-amber-950/10 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill label={`进入上下文 ${items.length}`} tone={items.length ? "success" : "default"} />
          {loadedItems.length > 0 && <StatusPill label={`ms-agent 已加载 ${loadedItems.length}`} tone="info" />}
          {latestTurnIndex !== null && <StatusPill label={`当前第 ${latestTurnIndex} 轮`} tone="info" />}
          {latestTurnId && <StatusPill label={latestTurnId} />}
          {skillNames.slice(0, 2).map((name) => (
            <StatusPill key={name} label={name} />
          ))}
        </div>
        {currentReferenceEvents.length > 0 && (
          <CopyCardButton
            content={formatUnknown({
              turn_id: latestTurnId,
              turn_index: latestTurnIndex,
              context_events: contextReferenceEvents.map((event) => event.payload),
              loaded_events: loadedReferenceEvents.map((event) => event.payload),
            })}
          />
        )}
      </div>
      <div className="mb-3 text-xs leading-5 text-slate-500">
        “进入上下文”是本轮最终 LLM Prompt 中的 reference snippet；“ms-agent 已加载”只表示
        SkillAnalyzer/lazy load 选中过这些文件，用于审计，不单独计入最终上下文。
      </div>
      {items.length ? (
        <div className="grid gap-2 md:grid-cols-2">
          {items.map((item, index) => (
            <div
              key={`${String(item.source_path ?? "")}-${index}`}
              className="rounded-xl border border-white/10 bg-slate-950/70 p-3"
            >
              <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                <span>#{String(item.index ?? index + 1)}</span>
                <span className="text-amber-200">{String(item.title ?? "reference")}</span>
                {item.score !== undefined && item.score !== null && (
                  <span>score={String(item.score)}</span>
                )}
              </div>
              <div className="mb-2 break-all text-xs text-slate-400">
                {String(item.source_path ?? "")}
              </div>
              <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-all text-xs leading-5 text-slate-300">
                {String(item.snippet ?? "")}
              </pre>
            </div>
          ))}
        </div>
      ) : (
        <div className="rounded-xl border border-dashed border-white/10 bg-slate-950/40 p-3 text-sm text-slate-400">
          当前轮暂未向最终 LLM 上下文注入 reference snippet。
        </div>
      )}
      {loadedItems.length > 0 && (
        <div className="mt-3 rounded-xl border border-white/10 bg-slate-950/40 p-3">
          <div className="mb-2 text-xs font-medium text-slate-400">ms-agent 已加载，仅作审计</div>
          <div className="flex flex-wrap gap-2">
            {loadedItems.map((item, index) => (
              <StatusPill
                key={`${String(item.source_path ?? "")}-${index}`}
                label={String(item.source_path ?? item.title ?? `reference ${index + 1}`)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function ToolResultCard({ payload }: { payload: Record<string, unknown> }) {
  const [contentExpanded, setContentExpanded] = useState(false);
  const [parsedExpanded, setParsedExpanded] = useState(false);
  const toolName = String(payload.tool_name ?? "");
  const ok = Boolean(payload.ok);
  const callId = String(payload.call_id ?? "");
  const sources = (payload.sources as string[] | undefined) ?? [];
  const argsText = formatUnknown(payload.arguments);
  const contentPreview = String(payload.content_preview ?? payload.content ?? "");
  const contentText = String(payload.content ?? "");
  const parsedContent = payload.parsed_content;
  const hasParsedContent = parsedContent !== undefined && parsedContent !== null;

  return (
    <div className="mt-2 rounded-xl border border-emerald-400/20 bg-emerald-950/20 p-3">
      <div className="mb-2 flex items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill label={`🛠 ${toolName || "tool"}`} tone={ok ? "success" : "danger"} />
          {callId && <StatusPill label={callId} />}
          <StatusPill label={ok ? "执行成功" : "执行失败"} tone={ok ? "success" : "danger"} />
        </div>
        <CopyCardButton content={formatUnknown(payload)} />
      </div>
      {payload.error ? (
        <div className="mb-2 rounded-lg border border-rose-400/20 bg-rose-950/30 p-2 text-xs text-rose-200">
          {String(payload.error)}
        </div>
      ) : null}
      {sources.length > 0 && (
        <div className="mb-2 text-xs text-slate-500">来源：{sources.join("、")}</div>
      )}
      <div className="mb-2">
        <div className="mb-1 text-xs font-medium text-slate-400">调用参数</div>
        <pre className="max-h-28 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-950/80 p-2 text-xs leading-5 text-slate-300">
          {argsText}
        </pre>
      </div>
      {contentPreview && (
        <div className="mb-2">
          <div className="mb-1 text-xs font-medium text-slate-400">命中 / 处理结果预览</div>
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-950/80 p-2 text-xs leading-5 text-slate-300">
            {contentPreview}
          </pre>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-3">
        {contentText && (
          <button
            type="button"
            onClick={() => setContentExpanded((e) => !e)}
            className="flex items-center gap-1 text-xs text-emerald-300 transition hover:text-emerald-200"
          >
            {contentExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            {contentExpanded ? "收起完整结果" : "展开完整结果"}
          </button>
        )}
        {hasParsedContent && (
          <button
            type="button"
            onClick={() => setParsedExpanded((e) => !e)}
            className="flex items-center gap-1 text-xs text-emerald-300 transition hover:text-emerald-200"
          >
            {parsedExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            {parsedExpanded ? "收起结构化 JSON" : "展开结构化 JSON"}
          </button>
        )}
      </div>
      {contentExpanded && (
        <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-950/80 p-3 text-xs leading-5 text-slate-300">
          {contentText}
        </pre>
      )}
      {parsedExpanded && hasParsedContent && (
        <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-950/80 p-3 text-xs leading-5 text-slate-300">
          {JSON.stringify(parsedContent, null, 2)}
        </pre>
      )}
    </div>
  );
}

function RetrievalContextCard({ payload }: { payload: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(false);
  const phase = String(payload.phase ?? "");
  const skillName = String(payload.skill_name ?? "");
  const items = (payload.items as Array<Record<string, unknown>> | undefined) ?? [];
  const generatedAssetDomains = (payload.generated_asset_domains as string[] | undefined) ?? [];
  const localAssetPaths = (payload.local_asset_paths as string[] | undefined) ?? [];

  return (
    <div className="mt-2 rounded-xl border border-violet-400/20 bg-violet-950/20 p-3">
      <div className="mb-2 flex items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill label={`🔎 检索命中 ${items.length}`} tone="info" />
          {phase && <StatusPill label={phase} />}
          {skillName && <StatusPill label={skillName} />}
        </div>
        <CopyCardButton content={formatUnknown(payload)} />
      </div>
      {generatedAssetDomains.length > 0 && (
        <div className="mb-1 text-xs text-slate-500">
          全局资产域：{generatedAssetDomains.join("、")}
        </div>
      )}
      {localAssetPaths.length > 0 && (
        <div className="mb-2 text-xs text-slate-500">
          本地资产：{localAssetPaths.join("、")}
        </div>
      )}
      <div className="space-y-2">
        {items.slice(0, expanded ? items.length : 3).map((item, index) => (
          <div key={`${item.source_path ?? index}`} className="rounded-lg bg-slate-950/70 p-2">
            <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
              <span>#{String(item.index ?? index + 1)}</span>
              <span>{String(item.source_type ?? "")}</span>
              <span>score={String(item.score ?? "")}</span>
              <span className="text-slate-400">{String(item.source_path ?? "")}</span>
            </div>
            <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-all text-xs leading-5 text-slate-300">
              {String(item.snippet ?? "")}
            </pre>
          </div>
        ))}
      </div>
      {items.length > 3 && (
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          className="mt-2 flex items-center gap-1 text-xs text-violet-300 transition hover:text-violet-200"
        >
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          {expanded ? "收起命中内容" : `展开全部 ${items.length} 条命中`}
        </button>
      )}
    </div>
  );
}

function PromptAssemblyCard({ payload }: { payload: Record<string, unknown> }) {
  const [promptExpanded, setPromptExpanded] = useState(false);
  const [reasoningExpanded, setReasoningExpanded] = useState(false);
  const [responseExpanded, setResponseExpanded] = useState(false);
  const phase = displayText(payload.phase);
  const skillName = displayText(payload.skill_name);
  const skillType = displayText(payload.skill_type);
  const layer = displayText(payload.layer);
  const referenceStrategy = displayText(payload.reference_strategy);
  const retrievedCount = Number(payload.retrieved_count ?? 0);
  const generatedAssetDomains = displayTextList(payload.generated_asset_domains);
  const retrievedSources = displayTextList(payload.retrieved_sources);
  const localAssetPaths = displayTextList(payload.local_asset_paths);
  const promptTitle = displayText(payload.prompt_title ?? payload.prompt_key);
  const assembledFrom = displayTextList(payload.assembled_from);
  const promptContent = formatUnknown(payload.prompt_content);
  const llmResponse = payload.llm_response;
  const hasLlmResponse = llmResponse !== undefined && llmResponse !== null;
  const formattedLlmResponse =
    typeof llmResponse === "string" ? llmResponse : JSON.stringify(llmResponse, null, 2);
  const llmReasoning = payload.llm_reasoning;
  const hasLlmReasoning = typeof llmReasoning === "string" && llmReasoning.trim().length > 0;

  return (
    <div className="mt-2 rounded-xl border border-cyan-400/20 bg-cyan-950/30 p-3">
      <div className="mb-2 flex items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill label={`📋 ${phase}`} tone="info" />
          <StatusPill label={skillName} />
          {layer && <StatusPill label={`layer:${layer}`} tone="info" />}
          {skillType && <StatusPill label={skillType} />}
          {promptTitle && (
            <span className="text-xs font-medium text-cyan-300">{promptTitle}</span>
          )}
        </div>
        <CopyCardButton content={formatPromptAssemblyPayload(payload)} />
      </div>
      <div className="mb-2 flex flex-wrap gap-3 text-xs text-slate-500">
        {referenceStrategy && <span>装载策略：{referenceStrategy}</span>}
        <span>检索命中：{retrievedCount}</span>
        {generatedAssetDomains.length > 0 && (
          <span>全局资产域：{generatedAssetDomains.join("、")}</span>
        )}
      </div>
      {assembledFrom.length > 0 && (
        <div className="mb-2 text-xs text-slate-500">
          来源：{assembledFrom.join(" → ")}
        </div>
      )}
      {(retrievedSources.length > 0 || localAssetPaths.length > 0) && (
        <div className="mb-2 space-y-1 text-xs text-slate-500">
          {retrievedSources.length > 0 && <div>检索来源：{retrievedSources.join("、")}</div>}
          {localAssetPaths.length > 0 && <div>本地 assets：{localAssetPaths.join("、")}</div>}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => setPromptExpanded((e) => !e)}
          className="flex items-center gap-1 text-xs text-cyan-400 transition hover:text-cyan-300"
        >
          {promptExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          {promptExpanded ? "收起 Prompt 内容" : "展开 Prompt 内容"}
        </button>
        {hasLlmReasoning && (
          <button
            type="button"
            onClick={() => setReasoningExpanded((e) => !e)}
            className="flex items-center gap-1 text-xs text-cyan-400 transition hover:text-cyan-300"
          >
            {reasoningExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            {reasoningExpanded ? "收起 Thinking" : "展开 Thinking"}
          </button>
        )}
        {hasLlmResponse && (
          <button
            type="button"
            onClick={() => setResponseExpanded((e) => !e)}
            className="flex items-center gap-1 text-xs text-cyan-400 transition hover:text-cyan-300"
          >
            {responseExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            {responseExpanded ? "收起 LLM Response" : "展开 LLM Response"}
          </button>
        )}
      </div>
      {promptExpanded && (
        <div className="mt-2">
          {payload.prompt_content !== undefined && payload.prompt_content !== null && (
            <div className="mb-2">
              <div className="mb-1 text-xs font-medium text-slate-400">Prompt 内容</div>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-950/80 p-3 text-xs leading-5 text-slate-300">
                {promptContent}
              </pre>
            </div>
          )}
          {payload.variables && (
            <div>
              <div className="mb-1 text-xs font-medium text-slate-400">变量值</div>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-950/80 p-3 text-xs leading-5 text-slate-300">
                {JSON.stringify(payload.variables, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
      {responseExpanded && hasLlmResponse && (
        <div className="mt-2">
          <div className="mb-1 text-xs font-medium text-slate-400">LLM Response</div>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-950/80 p-3 text-xs leading-5 text-slate-300">
            {formattedLlmResponse}
          </pre>
        </div>
      )}
      {reasoningExpanded && hasLlmReasoning && (
        <div className="mt-2">
          <div className="mb-1 text-xs font-medium text-slate-400">Thinking</div>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-950/80 p-3 text-xs leading-5 text-slate-300">
            {llmReasoning as string}
          </pre>
        </div>
      )}
    </div>
  );
}

export function EventPanel({
  events,
  loading,
  onRefresh,
  onDownloadLogs,
  canDownloadLogs = false,
}: EventPanelProps) {
  const [downloadingLogs, setDownloadingLogs] = useState(false);
  const isPromptAssembly = (event: SkillEvent) => event.event_type === "prompt_assembly";
  const isToolDebugEvent = (event: SkillEvent) =>
    event.event_type === "tool_result" || event.event_type === "retrieval_context";
  const isReferenceContextEvent = (event: SkillEvent) => event.event_type === "reference_context";
  const promptAssemblyEvents = events.filter(isPromptAssembly);
  const explicitToolDebugEvents = events.filter(isToolDebugEvent);
  const derivedRetrievalEvents =
    explicitToolDebugEvents.some((event) => event.event_type === "retrieval_context")
      ? []
      : promptAssemblyEvents
          .map(buildRetrievalEventFromPrompt)
          .filter((event): event is SkillEvent => event !== null);
  const toolDebugEvents = [...explicitToolDebugEvents, ...derivedRetrievalEvents];
  const otherEvents = events.filter(
    (e) => !isPromptAssembly(e) && !isToolDebugEvent(e) && !isReferenceContextEvent(e),
  );
  return (
    <section className="rounded-[28px] border border-white/10 bg-white/[0.04] p-5">
      <div className="mb-4 flex items-center justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-[0.22em] text-slate-500">事件日志</p>
          <h3 className="mt-2 text-lg font-semibold text-white">后端事件流</h3>
        </div>
        <div className="flex items-center gap-2">
          <StatusPill label={`${events.length} 条`} />
          {onDownloadLogs && (
            <button
              type="button"
              disabled={!canDownloadLogs || downloadingLogs}
              onClick={() => {
                setDownloadingLogs(true);
                void onDownloadLogs().finally(() => setDownloadingLogs(false));
              }}
              className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-slate-950/80 px-4 py-2 text-sm text-slate-200 transition hover:border-emerald-400/30 hover:text-emerald-100 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Download size={14} className={downloadingLogs ? "animate-pulse" : ""} />
              {downloadingLogs ? "下载中" : "下载日志"}
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              void onRefresh();
            }}
            className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-slate-950/80 px-4 py-2 text-sm text-slate-200 transition hover:border-cyan-400/30 hover:text-cyan-100"
          >
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
            刷新
          </button>
        </div>
      </div>

      <div className="max-h-[420px] space-y-3 overflow-y-auto pr-2">
        {events.length ? (
          <>
            <CurrentTurnReferencePanel events={events} />
            {promptAssemblyEvents.length > 0 && (
              <div className="mb-4">
                <div className="mb-3 flex items-center gap-2">
                  <StatusPill label={`📋 Prompt 调试（${promptAssemblyEvents.length}）`} tone="info" />
                  <span className="text-xs text-slate-500">
                    当前会话使用的 LLM Prompt，展开可查看完整内容
                  </span>
                </div>
                {promptAssemblyEvents
                  .slice()
                  .reverse()
                  .map((event) => (
                    <article
                      key={event.event_id}
                      className="mb-2 rounded-2xl border border-cyan-400/10 bg-slate-950/60 p-4"
                    >
                      <div className="mb-1 flex items-start justify-between gap-3">
                        <div className="flex items-center gap-2">
                          <StatusPill label={event.event_type} tone="info" />
                          <EventTurnPills event={event} />
                          <span className="text-xs text-slate-500">{formatEventTime(event.created_at)}</span>
                        </div>
                      </div>
                      <PromptAssemblyCard payload={event.payload} />
                    </article>
                  ))}
              </div>
            )}
            {toolDebugEvents.length > 0 && (
              <div className="mb-4">
                <div className="mb-3 flex items-center gap-2">
                  <StatusPill label={`🛠 工具 / 检索结果（${toolDebugEvents.length}）`} tone="success" />
                  <span className="text-xs text-slate-500">
                    展示 runtime 实际执行的工具结果，以及本轮自动检索命中的资产片段
                  </span>
                </div>
                {toolDebugEvents
                  .slice()
                  .reverse()
                  .map((event) => (
                    <article
                      key={event.event_id}
                      className="mb-2 rounded-2xl border border-emerald-400/10 bg-slate-950/60 p-4"
                    >
                      <div className="mb-1 flex items-start justify-between gap-3">
                        <div className="flex items-center gap-2">
                          <StatusPill
                            label={event.event_type}
                            tone={event.event_type === "tool_result" ? "success" : "info"}
                          />
                          <EventTurnPills event={event} />
                          <span className="text-xs text-slate-500">{formatEventTime(event.created_at)}</span>
                        </div>
                      </div>
                      {event.event_type === "tool_result" ? (
                        <ToolResultCard payload={event.payload} />
                      ) : (
                        <RetrievalContextCard payload={event.payload} />
                      )}
                    </article>
                  ))}
              </div>
            )}
            <div>
              <div className="mb-3 text-xs font-medium text-slate-500">
                其他事件（{otherEvents.length}）
              </div>
              {otherEvents
                .slice()
                .reverse()
                .map((event) => (
                  <article
                    key={event.event_id}
                    className="mb-2 rounded-2xl border border-white/10 bg-slate-950/80 p-4"
                  >
                    <div className="mb-1 flex items-start justify-between gap-3">
                      <div className="flex items-center gap-2">
                        <StatusPill label={event.event_type} tone="success" />
                        <EventTurnPills event={event} />
                        <span className="text-xs text-slate-500">{formatEventTime(event.created_at)}</span>
                      </div>
                      <CopyCardButton content={formatEventCardContent(event)} />
                    </div>
                    <pre className="mt-2 whitespace-pre-wrap break-all text-xs leading-5 text-slate-300">
                      {JSON.stringify(event.payload, null, 2)}
                    </pre>
                  </article>
                ))}
            </div>
          </>
        ) : (
          <div className="rounded-2xl border border-dashed border-white/10 p-4 text-sm text-slate-400">
            事件日志会在你发送消息后逐步累积。
          </div>
        )}
      </div>
    </section>
  );
}
