import { useEffect, useMemo, useState } from "react";

import type { HarnessV3Frame, HarnessV3InputRequest, HarnessV3Node } from "@/types/harnessV3";
import { resumeHarnessV3, submitHarnessV3Answer, useHarnessV3State } from "@/hooks/useHarnessV3Conversation";

const labels: Record<string, string> = {
  queued: "排队中", running: "执行中", waiting_for_dependency: "等待依赖", waiting_for_user: "等待你补充",
  completed: "已完成", failed: "执行失败", cancelled: "已停止", skipped: "未补充",
};

function AnswerCard({ frame, request }: { frame: HarnessV3Frame; request: HarnessV3InputRequest }) {
  const [drafts, setDrafts] = useState<Record<string, unknown>>(() => {
    try { return JSON.parse(sessionStorage.getItem(`harness-v3-draft:${request.id}`) || "{}"); } catch { return {}; }
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (request.status === "pending") sessionStorage.setItem(`harness-v3-draft:${request.id}`, JSON.stringify(drafts));
    else sessionStorage.removeItem(`harness-v3-draft:${request.id}`);
  }, [drafts, request.id, request.status]);
  if (request.status !== "pending") return null;
  const submit = async (skipped: boolean) => {
    setSubmitting(true); setError("");
    const answers = skipped ? {} : Object.fromEntries(Object.entries(drafts).filter(([, value]) => value !== "" && value != null && (!Array.isArray(value) || value.length > 0)));
    try { await submitHarnessV3Answer(frame, request, answers, skipped); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "提交失败，请重试"); }
    finally { setSubmitting(false); }
  };
  return <form aria-label={request.title} className="mt-3 space-y-3 rounded-xl border border-amber-300/30 bg-amber-300/5 p-3" onSubmit={(event) => { event.preventDefault(); void submit(false); }}>
    <p className="text-sm text-amber-100">{request.actor || "主编排器"} 向你追问 · {request.title} · 第 {request.round} 轮</p>
    {request.clarification_reason ? <p className="text-xs text-slate-300">{request.clarification_reason}</p> : null}
    {request.fields.map((field) => <label key={field.key} className="block text-sm text-slate-200">
      {field.label}{field.required ? "（必填，可跳过）" : ""}
      {field.input_type === "single_select" ? <select aria-label={field.label} disabled={submitting} value={String(drafts[field.key] ?? "")} onChange={(event) => setDrafts({ ...drafts, [field.key]: event.target.value })} className="mt-1 block w-full rounded-lg bg-slate-900 p-2">
        <option value="">暂不填写</option>{field.options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select> :
      field.input_type === "multi_select" ? <span className="mt-1 flex flex-wrap gap-2">{field.options.map((option) => <label key={option.value} className="text-xs">
        <input aria-label={`${field.label}：${option.label}`} type="checkbox" disabled={submitting} checked={Array.isArray(drafts[field.key]) && (drafts[field.key] as string[]).includes(option.value)} onChange={(event) => {
          const current = Array.isArray(drafts[field.key]) ? drafts[field.key] as string[] : [];
          setDrafts({ ...drafts, [field.key]: event.target.checked ? [...current, option.value] : current.filter((value) => value !== option.value) });
        }} /> {option.label}</label>)}</span> :
      field.input_type === "text" ? <textarea aria-label={field.label} disabled={submitting} value={String(drafts[field.key] ?? "")} onChange={(event) => setDrafts({ ...drafts, [field.key]: event.target.value })} className="mt-1 block w-full rounded-lg bg-slate-900 p-2" /> :
      <input aria-label={field.label} type="number" step={field.input_type === "integer" ? "1" : "any"} min={field.min ?? undefined} max={field.max ?? undefined} disabled={submitting} value={String(drafts[field.key] ?? "")} onChange={(event) => setDrafts({ ...drafts, [field.key]: event.target.value === "" ? "" : Number(event.target.value) })} className="mt-1 block w-full rounded-lg bg-slate-900 p-2" />}
    </label>)}
    {error ? <p role="alert" className="text-xs text-rose-300">{error}</p> : null}
    <div className="flex gap-3"><button disabled={submitting} className="rounded-lg bg-cyan-400/20 px-3 py-2 text-xs">{submitting ? "提交中" : "提交已填写内容"}</button>
      <button type="button" disabled={submitting} onClick={() => void submit(true)} className="px-3 py-2 text-xs text-slate-300">全部跳过</button></div>
  </form>;
}

function ResultCard({ node }: { node: HarnessV3Node }) {
  if (!node.result?.conclusion) return null;
  return <div className="mt-2 rounded-lg border border-violet-300/20 bg-violet-300/[0.05] p-3 text-sm text-slate-200">
    <p className="text-xs text-violet-200">{node.actor || node.title} 的结论</p><p className="mt-1 whitespace-pre-wrap">{node.result.conclusion}</p>
    {node.result.findings?.length ? <p className="mt-2 text-xs text-slate-300">依据：{node.result.findings.join("；")}</p> : null}
    {node.result.limitations?.length ? <p className="mt-1 text-xs text-amber-200">限制：{node.result.limitations.join("；")}</p> : null}
  </div>;
}

export function HarnessV3ProgressBlock({ frame }: { frame: HarnessV3Frame }) {
  const connection = useHarnessV3State((state) => state.connection);
  const nodes = frame.state.nodes;
  const nodeById = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const render = (node: HarnessV3Node, depth = 0): JSX.Element => {
    const children = nodes.filter((child) => child.parent_id === node.id);
    return <div key={node.id} className={depth ? "ml-3 border-l border-white/10 pl-3" : ""}>
      <details open={node.status !== "completed" && node.status !== "cancelled"}>
        <summary className="cursor-pointer py-2 text-sm text-slate-200"><span>{node.title}</span>{node.actor ? <span className="ml-2 text-xs text-slate-400">{node.actor}</span> : null}
          <span className={`ml-2 text-xs ${node.status === "failed" ? "text-rose-300" : node.status === "waiting_for_user" ? "text-amber-200" : "text-cyan-200"}`}>{labels[node.status] || node.status}</span></summary>
        {node.action ? <p className="pb-2 text-xs text-slate-400">{node.action}</p> : null}
        {node.waiting_on?.length ? <p className="pb-2 text-xs text-slate-400">等待：{node.waiting_on.map((id) => nodeById.get(id)?.title || id).join("、")}</p> : null}
        {node.error ? <p className="pb-2 text-xs text-rose-300">失败原因：{node.error}</p> : null}
        <ResultCard node={node} />
        {children.map((child) => render(child, depth + 1))}
      </details>
    </div>;
  };
  const dispatch = frame.state.dispatch;
  // A snapshot may retain older pending-looking requests while a group is
  // being advanced. Only the server-designated active request is editable.
  const activeRequestId = frame.state.input_queue?.active_request_id;
  const pending = activeRequestId
    ? frame.state.inputs.find((item) => item.id === activeRequestId && item.status === "pending")
    : frame.state.inputs.find((item) => item.status === "pending");
  const queued = frame.state.inputs.filter((item) => item.status === "queued");
  const roots = nodes.filter((node) => !node.parent_id);
  const canResume = !frame.persistence.terminal && connection === "timeout";
  return <section aria-label="任务动态状态" className="rounded-2xl border border-cyan-300/20 bg-cyan-950/20 p-4">
    <p className="text-sm text-cyan-100">{frame.state.goal} · {frame.state.action || labels[frame.state.status] || "处理中"}</p>
    {dispatch?.status === "completed" ? <div className="mt-3 rounded-xl border border-white/10 p-3 text-xs text-slate-300"><p>已拆分为 {dispatch.planned_expert_count ?? 0} 个专家任务 · 可并行 {dispatch.parallel_task_count ?? 0} 个</p>
      {dispatch.experts?.map((expert) => <p key={expert.node_id} className="mt-1">{expert.name}：{expert.brief || expert.agent_brief || "等待执行"}</p>)}</div> : null}
    <p className="mt-2 text-xs text-slate-400">已完成 {frame.state.completed_count ?? 0} / {frame.state.task_count ?? 0} 项任务 · {frame.state.waiting_count ?? 0} 项等待补充</p>
    <div className="mt-2">{roots.map((node) => render(node))}</div>
    {pending ? <AnswerCard frame={frame} request={pending} /> : null}
    {queued.map((request, index) => <div key={request.id} className="mt-2 rounded-lg border border-white/10 p-2 text-xs text-slate-400">后续问题 {index + 1} · {request.actor || "主编排器"}：{request.title}</div>)}
    {canResume ? <button type="button" onClick={() => void resumeHarnessV3(frame)} className="mt-3 rounded-lg border border-cyan-300/30 bg-cyan-300/10 px-3 py-2 text-xs text-cyan-100">继续处理</button> : null}
    {frame.state.recommendations_status === "completed" && frame.state.jump_cards?.length ? <div className="mt-3 space-y-2">{frame.state.jump_cards.map((card, index) => <div key={String(card.id || index)} className="rounded-lg border border-white/10 p-3 text-xs text-slate-300"><b>{String(card.title || "相关推荐")}</b><p>{String(card.description || card.reason || "")}</p></div>)}</div> : null}
  </section>;
}
