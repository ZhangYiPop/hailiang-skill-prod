import { useEffect, useState } from "react";
import type { HarnessRun, HarnessNode, HarnessInput } from "@/types/harness";
import { isHarnessTerminal } from "@/types/harness";
import { harnessRequest, refreshHarness, submitHarnessAnswer, useHarnessState } from "@/hooks/useHarnessConversation";
import { useChatStore } from "@/store/useChatStore";

const labels: Record<string, string> = { queued: "排队中", running: "执行中", waiting_for_dependency: "等待任务结果",
  waiting_for_user: "等待你补充", completed: "已完成", failed: "执行失败", cancelled: "已停止", skipped: "未补充" };

export type SubmitHarnessAnswer = (request: HarnessInput, answers: Record<string, unknown>, skipped: boolean, submissionId: string) => Promise<void>;
export function AgentInputRequest({ request, sessionId, onSubmitAnswer }: { request: HarnessInput; sessionId: string; onSubmitAnswer?: SubmitHarnessAnswer }) {
  const [drafts, setDrafts] = useState<Record<string, unknown>>(() => {
    try { return JSON.parse(sessionStorage.getItem(`harness-draft:${request.id}`) || "{}"); } catch { return {}; }
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (request.status === "pending") sessionStorage.setItem(`harness-draft:${request.id}`, JSON.stringify(drafts));
    else sessionStorage.removeItem(`harness-draft:${request.id}`);
  }, [request.id, request.status, drafts]);
  const submit = async (skipped: boolean) => {
    setSubmitting(true); setError("");
    const store = useChatStore.getState();
    const body = { user_id: store.userId, profile_id: store.selectedProfileId || store.activeProfileId || null,
      submission_id: `${request.id}:${request.version}:${skipped ? "skip" : JSON.stringify(drafts)}`.slice(0, 100),
      expected_version: request.version, skipped, answers: skipped ? {} : drafts };
    // Reuse a generated ID on transport retry; the server checks the body too.
    const submissionKey = `harness-submission:${request.id}`;
    const serialized = JSON.stringify(body);
    let cached: { body: string; id: string } | null = null;
    try { cached = JSON.parse(sessionStorage.getItem(submissionKey) || "null"); } catch { /* empty */ }
    body.submission_id = cached?.body === serialized ? cached.id : crypto.randomUUID();
    sessionStorage.setItem(submissionKey, JSON.stringify({ body: serialized, id: body.submission_id }));
    try {
      if (onSubmitAnswer) await onSubmitAnswer(request, skipped ? {} : drafts, skipped, body.submission_id);
      else {
        await submitHarnessAnswer(sessionId, request.id, body);
      }
    } catch (e) { setError(e instanceof Error ? e.message : "提交失败，请重试"); }
    finally { setSubmitting(false); }
  };
  if (request.status !== "pending") return <div className="mt-2 rounded-xl border border-white/10 p-3 text-xs text-slate-400">
    {request.actor || "主 agent"} · {request.title} · 第 {request.round} 轮 · {request.status === "queued" ? "排队等待提问" : request.status === "answered" ? "已回答" : request.status === "skipped" ? "未补充" : "已失效"}
    {request.answer && !request.answer.skipped && <dl className="mt-2">{request.fields.map((f) => <div key={f.key}><dt className="inline">{f.label}：</dt><dd className="inline">{String(request.answer?.answers[f.key] ?? "本次跳过")}</dd></div>)}</dl>}
  </div>;
  return <form aria-label={request.title} className="mt-3 space-y-3 rounded-xl border border-amber-300/30 bg-amber-300/5 p-3" onSubmit={(e) => { e.preventDefault(); void submit(false); }}>
    <p className="text-sm text-amber-100">{request.actor || "主 agent"} 向你追问 · {request.title} · 第 {request.round} 轮</p>
    <p className="text-xs text-slate-400">可只填写部分，未填的问题本次跳过。</p>
    {request.clarification_reason && <p className="text-xs text-slate-300">{request.clarification_reason}</p>}
    {request.fields.map((field) => <div key={field.key} className="block text-sm text-slate-200">
      {field.label}
      {field.input_type === "single_select" ? <select aria-label={field.label} disabled={submitting} value={String(drafts[field.key] ?? "")} onChange={(e) => setDrafts({ ...drafts, [field.key]: e.target.value })} className="mt-1 block w-full rounded-lg bg-slate-900 p-2">
        <option value="">请选择</option>{field.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select> : field.input_type === "multi_select" ? <div className="mt-1 flex flex-wrap gap-2">{field.options.map((o) => <label key={o.value}>
        <input aria-label={`${field.label}：${o.label}`} type="checkbox" disabled={submitting} checked={Array.isArray(drafts[field.key]) && (drafts[field.key] as string[]).includes(o.value)} onChange={(e) => {
          const current = (drafts[field.key] as string[]) || []; setDrafts({ ...drafts, [field.key]: e.target.checked ? [...current, o.value] : current.filter((v) => v !== o.value) });
        }} /> {o.label}</label>)}</div> : field.input_type === "text" ? <textarea aria-label={field.label} disabled={submitting} value={String(drafts[field.key] ?? "")} onChange={(e) => setDrafts({ ...drafts, [field.key]: e.target.value })} className="mt-1 block w-full rounded-lg bg-slate-900 p-2" /> :
      <input aria-label={field.label} type="number" step={field.input_type === "integer" ? "1" : "any"} min={field.min} max={field.max} disabled={submitting} value={String(drafts[field.key] ?? "")} onChange={(e) => setDrafts({ ...drafts, [field.key]: e.target.value === "" ? "" : Number(e.target.value) })} className="mt-1 block w-full rounded-lg bg-slate-900 p-2" />}
    </div>)}
    {error && <p role="alert" className="text-xs text-rose-300">{error}</p>}
    <div className="flex gap-3"><button disabled={submitting} className="rounded-lg bg-cyan-400/20 px-3 py-2 text-xs">{submitting ? "提交中" : "提交并继续"}</button>
      <button type="button" disabled={submitting} onClick={() => void submit(true)} className="px-3 py-2 text-xs text-slate-300">全部跳过</button></div>
  </form>;
}

function nodeTime(node: HarnessNode): string {
  return node.created_at || node.started_at || "";
}

function sortNodes(nodes: HarnessNode[]): HarnessNode[] {
  return [...nodes].sort((a, b) => {
    const byTime = nodeTime(a).localeCompare(nodeTime(b));
    return byTime || a.id.localeCompare(b.id);
  });
}

export function TaskProgressBlock({ run, onSubmitAnswer, connection: suppliedConnection }: { run: HarnessRun; onSubmitAnswer?: SubmitHarnessAnswer; connection?: string }) {
  const [clock, setClock] = useState(Date.now());
  const chatConnection = useHarnessState((s) => s.connection);
  const connection = suppliedConnection || chatConnection;
  useEffect(() => { if (isHarnessTerminal(run.status)) return; const timer = setInterval(() => setClock(Date.now()), 1000); return () => clearInterval(timer); }, [run.status]);
  const end = isHarnessTerminal(run.status) ? Date.parse(run.updated_at) : clock;
  const render = (node: HarnessNode, depth = 0) => {
    const children = sortNodes(run.nodes.filter((n) => n.parent_id === node.id));
    const requests = run.inputs.filter((q) => q.bindings.some((b) => b.task_id === node.id));
    return <div key={node.id} className={depth ? "ml-3 border-l border-white/10 pl-3" : ""}>
      <details open={!isHarnessTerminal(node.status) && node.status !== "skipped"}>
        <summary className="cursor-pointer py-2 text-sm text-slate-200"><span>{node.title}</span>{node.actor && <span className="ml-2 text-xs text-slate-400">{node.actor}</span>}
          <span className={`ml-2 text-xs ${node.status === "waiting_for_user" ? "text-amber-200" : node.status === "failed" ? "text-rose-300" : "text-cyan-200"}`}>{labels[node.status] || node.status}</span>
          {(node.attempts ?? 0) > 1 && <span className="ml-2 text-xs text-slate-400">第 {node.attempts} 次执行</span>}
        </summary>
        {children.map((child) => render(child, depth + 1))}
        {requests.filter((q) => !["pending", "queued"].includes(q.status)).map((q) => <AgentInputRequest key={q.id} request={q} sessionId={run.session_id} onSubmitAnswer={onSubmitAnswer} />)}
        {node.error && <p className="text-xs text-rose-300">失败原因：{node.error}</p>}
      </details>
    </div>;
  };
  return <section aria-label="任务动态状态" className="rounded-2xl border border-cyan-300/20 bg-cyan-950/20 p-4">
    <p className="text-sm text-cyan-100">{run.goal} · {labels[run.status] || "处理中"}</p>
    {run.phase && !isHarnessTerminal(run.status) && <p className="mt-2 whitespace-pre-wrap text-sm text-slate-300">{run.phase}</p>}
    <p className="mt-1 text-xs text-slate-400">已完成 {run.completed_count} / {run.task_count} 项任务 · {run.waiting_count} 项等待补充 · {Math.max(0, Math.floor((end - Date.parse(run.created_at)) / 1000))}s</p>
    {run.input_token_limit && <p className="mt-1 text-xs text-slate-400">累计模型输入估算 {run.input_tokens_estimated?.toLocaleString() ?? 0} / {run.input_token_limit.toLocaleString()} token{run.input_tokens_actual != null ? ` · 已回报实际输入 ${run.input_tokens_actual.toLocaleString()} token` : ""}</p>}
    {connection === "reconnecting" && !isHarnessTerminal(run.status) && <p role="status" className="mt-2 text-xs text-amber-200">连接恢复中，后台任务继续执行</p>}
    {sortNodes(run.nodes.filter((n) => !n.parent_id)).map((n) => render(n))}
    {run.inputs.filter((q) => q.status === "pending").map((q) => {
      const owner = run.nodes.find((n) => n.id === q.owner_task_id);
      return <div key={q.id} className="mt-3">
        <p className="text-xs text-violet-200">{q.actor || owner?.actor || "主 agent"} · {owner?.title || "补充信息"}</p>
        <AgentInputRequest request={q} sessionId={run.session_id} onSubmitAnswer={onSubmitAnswer} />
      </div>;
    })}
    {run.inputs.filter((q) => q.status === "queued").map((q, index) => <div key={q.id} className="mt-2 rounded-lg border border-white/10 p-2 text-xs text-slate-400">
      排队第 {index + 1} 位 · {q.actor || "主 agent"}：{q.title}
    </div>)}
  </section>;
}
