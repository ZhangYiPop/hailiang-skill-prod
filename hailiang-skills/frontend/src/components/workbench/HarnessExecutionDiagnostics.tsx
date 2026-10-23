export type HarnessExecutionPhase = {
  id: string;
  kind: string;
  title: string;
  status: string;
  created_at?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  duration_ms?: number | null;
  queue_reason?: string | null;
};

export type HarnessExecutionDiagnosticsData = {
  total_elapsed_ms?: number | null;
  worker_claimed_at?: string | null;
  worker_wait_ms?: number | null;
  first_model_phase?: { purpose?: string; duration_ms?: number | null } | null;
  first_output_at?: string | null;
  first_output_ms?: number | null;
  max_parallel?: number;
  running_task_count?: number;
  phases?: HarnessExecutionPhase[];
};

const queueLabels: Record<string, string> = {
  waiting_for_dependency: "等待前置任务",
  waiting_for_user_input: "等待用户补充",
  run_parallelism_limit: "达到本任务并行上限",
  awaiting_worker_claim: "等待 worker 领取",
  waiting_for_parent_task: "等待所属任务执行",
};

function elapsed(value?: number | null): string {
  if (value == null) return "未完成";
  if (value < 1000) return `${value}ms`;
  return `${(value / 1000).toFixed(value >= 10_000 ? 1 : 2)}s`;
}

export function HarnessExecutionDiagnostics({ diagnostics }: { diagnostics?: HarnessExecutionDiagnosticsData | null }) {
  if (!diagnostics) return null;
  const phases = diagnostics.phases || [];
  return <section aria-label="执行耗时与排队诊断" className="space-y-3 rounded-xl border border-sky-300/20 bg-sky-950/20 p-3 text-xs text-slate-300">
    <div>
      <p className="font-medium text-sky-100">执行耗时与排队诊断</p>
      <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-slate-400">
        <div><dt className="inline">流程总耗时：</dt><dd className="inline text-slate-200">{elapsed(diagnostics.total_elapsed_ms)}</dd></div>
        <div><dt className="inline">worker 领取等待：</dt><dd className="inline text-slate-200">{elapsed(diagnostics.worker_wait_ms)}</dd></div>
        <div><dt className="inline">首个模型阶段：</dt><dd className="inline text-slate-200">{diagnostics.first_model_phase?.purpose || "尚未开始"}{diagnostics.first_model_phase?.duration_ms != null ? ` · ${elapsed(diagnostics.first_model_phase.duration_ms)}` : ""}</dd></div>
        <div><dt className="inline">首个可见输出：</dt><dd className="inline text-slate-200">{diagnostics.first_output_at ? elapsed(diagnostics.first_output_ms) : "尚未输出"}</dd></div>
      </dl>
    </div>
    {phases.length ? <div className="space-y-1 border-t border-white/10 pt-2">
      {phases.map((phase) => <div key={phase.id} className="rounded-lg bg-black/15 px-2 py-1.5">
        <p className="text-slate-200">{phase.title} <span className="text-slate-500">· {phase.kind} · {phase.status} · {elapsed(phase.duration_ms)}</span></p>
        {phase.queue_reason ? <p className="mt-0.5 text-amber-200">{queueLabels[phase.queue_reason] || phase.queue_reason}{phase.queue_reason === "awaiting_worker_claim" ? "（可能是 worker 离线或全局执行槽位繁忙）" : ""}</p> : null}
      </div>)}
    </div> : null}
  </section>;
}
