type ModelTrace = {
  purpose?: string;
  diagnostics?: {
    context_policy_version?: string;
    estimated_tokens?: number;
    components?: Record<string, number>;
    fact_versions?: Record<string, number>;
    compactions?: Array<{ reason: string; before: number; after: number }>;
  };
  usage?: { input_tokens?: number; output_tokens?: number; prompt_cache_hit_tokens?: number; prompt_cache_miss_tokens?: number; duration_ms?: number };
  measurement_source?: string;
};

export function HarnessContextDiagnostics({ trace }: { trace: string }) {
  let tasks: Array<{ task_id: string; kind: string; trace: ModelTrace[] }> = [];
  try {
    const data = JSON.parse(trace);
    if (Array.isArray(data.tasks)) tasks = data.tasks;
  } catch { return null; }
  const labels: Record<string, string> = { rules: "规则", task: "任务书", facts: "事实", questions: "问答", summary: "摘要", history: "历史", tool_evidence: "工具证据", tool_schemas: "工具定义" };
  return <div className="space-y-2" aria-label="上下文用量诊断">
    {tasks.map(task => task.trace?.map((entry, i) => entry.diagnostics?.components ? (
      <details key={`${task.task_id}-${i}`} className="rounded-lg border border-white/10 p-3 text-xs text-slate-300">
        <summary>{task.kind} · {task.task_id} · 第 {i + 1} 次 · {entry.purpose || "task"} · {entry.diagnostics.context_policy_version || "v1"}</summary>
        <p className="my-2">估算输入 {entry.diagnostics.estimated_tokens ?? "未知"} / 实际输入 {entry.usage?.input_tokens ?? "未提供"} / 输出 {entry.usage?.output_tokens ?? "未提供"}</p>
        <p>缓存命中 {entry.usage?.prompt_cache_hit_tokens ?? "未提供"} / 未命中 {entry.usage?.prompt_cache_miss_tokens ?? "未提供"} / 耗时 {entry.usage?.duration_ms ?? "未知"} ms</p>
        <dl className="my-2 grid grid-cols-2 gap-1">{Object.entries(entry.diagnostics.components).map(([key, value]) => <div key={key}><dt className="inline">{labels[key] || key}：</dt><dd className="inline">{value}</dd></div>)}</dl>
        <p>已选事实：{Object.entries(entry.diagnostics.fact_versions || {}).map(([key, version]) => `${key}@${version}`).join("、") || "无"}</p>
        {entry.diagnostics.compactions?.map((c, j) => <p key={j}>整理 {c.reason}：{c.before} → {c.after}</p>)}
        <p className="mt-2 text-slate-500">分项为估算；总输入另含消息封装。缓存不抵扣逻辑上下文预算。</p>
      </details>
    ) : null))}
  </div>;
}
