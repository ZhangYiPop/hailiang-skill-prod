import { useEffect, useState } from "react";
import type { HarnessRun, HarnessNode, HarnessInput } from "@/types/harness";
import { isHarnessTerminal } from "@/types/harness";
import { harnessRequest, refreshHarness, submitHarnessAnswer, useHarnessState } from "@/hooks/useHarnessConversation";
import { useChatStore } from "@/store/useChatStore";
import { createUuid } from "@/utils/uuid";

const labels: Record<string, string> = { queued: "排队中", assigned: "已分派", running: "执行中", waiting_for_dependency: "等待前置任务",
  waiting_for_user: "等待你补充", completed: "已完成", failed: "执行失败", cancelled: "已停止", skipped: "未补充" };

function briefText(brief: HarnessNode["brief"]): string {
  if (!brief) return "";
  if (typeof brief === "string") return brief;
  return String(brief.objective || brief.goal || brief.completion || "");
}

export type SubmitHarnessAnswer = (request: HarnessInput, answers: Record<string, unknown>, skipped: boolean, submissionId: string) => Promise<void>;
export function AgentInputRequest({ request, sessionId, onSubmitAnswer, groupProgress }: { request: HarnessInput; sessionId: string; onSubmitAnswer?: SubmitHarnessAnswer; groupProgress?: string }) {
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
    body.submission_id = cached?.body === serialized ? cached.id : createUuid();
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
    <p className="text-sm text-amber-100">{request.actor || "主 agent"} 向你追问 · {request.title} · 第 {request.round} 轮{groupProgress ? ` · ${groupProgress}` : ""}</p>
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

export type StepIo = { input: unknown; output: unknown };

function formatIo(value: unknown): string {
  if (value == null || value === "") return "（无）";
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return "（无）";
    if (trimmed[0] === "{" || trimmed[0] === "[") {
      try { return JSON.stringify(JSON.parse(trimmed), null, 2); } catch { /* keep the original text */ }
    }
    return value;
  }
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function StepIoNodes({ io }: { io: StepIo }) {
  return (
    <>
      {([["输入", io.input], ["输出", io.output]] as const).map(([label, value]) => (
        <div key={label} className="ml-3 border-l border-white/10 pl-3">
          <details>
            <summary className="cursor-pointer py-1 text-xs text-slate-400">{label}</summary>
            <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-black/30 p-2 text-xs leading-5 text-slate-300">{formatIo(value)}</pre>
          </details>
        </div>
      ))}
    </>
  );
}

const ROUND_STATUS: Record<string, string> = {
  idle: "未分配",
  assigned: "已分配",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  partial: "部分完成",
};

function isDispatchStep(node: HarnessNode): boolean {
  return node.kind === "tool" && node.title === "分派专家任务";
}

function isHeadStep(node: HarnessNode): boolean {
  return node.kind === "tool" && node.title.startsWith("编排首");
}

function isRecommendStep(node: HarnessNode): boolean {
  return node.kind === "tool" && node.title === "推荐";
}

/** Prefer 分派专家任务; fall back to 编排首 when there is no dispatch node yet. */
function expertTransferAnchorId(nodes: HarnessNode[]): string | null {
  const dispatch = nodes.find(isDispatchStep);
  if (dispatch) return dispatch.id;
  return nodes.find(isHeadStep)?.id ?? null;
}

function RoundExpertsTransfer({ run }: { run: HarnessRun }) {
  const experts = run.round_experts;
  const assigned = experts?.assigned_expert_ids || [];
  const passed = assigned.length > 0;
  return (
    <div aria-label="SSE 专家 id" className="ml-3 mt-1 rounded-lg border border-violet-300/20 bg-violet-950/20 p-2 text-xs text-slate-300">
      <p className="text-slate-200">
        SSE · 专家 id · <span className={passed ? "text-emerald-300" : "text-slate-500"}>{passed ? "已传递" : "等待分配"}</span>
        {experts ? <span className="ml-2 text-slate-400">status={ROUND_STATUS[experts.status] || experts.status}</span> : null}
      </p>
      {passed ? (
        <dl className="mt-1 space-y-0.5 text-slate-400">
          <div><dt className="inline">assigned：</dt><dd className="inline text-slate-200">{assigned.join("、")}</dd></div>
          {(experts?.running_expert_ids || []).length > 0 ? (
            <div><dt className="inline">running：</dt><dd className="inline">{experts!.running_expert_ids.join("、")}</dd></div>
          ) : null}
          {(experts?.completed_expert_ids || []).length > 0 ? (
            <div><dt className="inline">completed：</dt><dd className="inline">{experts!.completed_expert_ids.join("、")}</dd></div>
          ) : null}
          {(experts?.failed_expert_ids || []).length > 0 ? (
            <div><dt className="inline">failed：</dt><dd className="inline text-rose-300">{experts!.failed_expert_ids.join("、")}</dd></div>
          ) : null}
        </dl>
      ) : null}
    </div>
  );
}

function JumpCardsTransfer({ run }: { run: HarnessRun }) {
  const cards = run.jump_cards || [];
  const passed = cards.length > 0;
  return (
    <div aria-label="SSE 推荐字段" className="ml-3 mt-1 rounded-lg border border-violet-300/20 bg-violet-950/20 p-2 text-xs text-slate-300">
      <p className="text-slate-200">
        SSE · jump_cards · <span className={passed ? "text-emerald-300" : "text-slate-500"}>{passed ? `已传递 · ${cards.length} 条` : "尚未产出"}</span>
      </p>
      {passed ? (
        <ul className="mt-1 space-y-2">
          {cards.map((card, index) => (
            <li key={index} className="rounded-lg border border-white/10 bg-black/20 p-2">
              <p className="text-slate-100">{card.title || "（无标题）"}</p>
              <p className="mt-0.5 text-slate-400">{card.description || "（无描述）"}</p>
              <p className="mt-1 text-slate-500">
                intent={card.intent || "—"} · object_type={card.object_type || "—"} · sub_type={card.sub_type || "—"}
              </p>
              {card.reason ? <p className="mt-0.5 text-slate-500">reason={card.reason}</p> : null}
              {card.filter ? (
                <p className="mt-0.5 text-slate-500">
                  filter: region={card.filter.region || "—"} · grade={card.filter.grade || "—"} · keyword={card.filter.keyword || "—"} · category={card.filter.category || "—"}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function TaskProgressBlock({ run, onSubmitAnswer, connection: suppliedConnection, steps }: { run: HarnessRun; onSubmitAnswer?: SubmitHarnessAnswer; connection?: string; steps?: Record<string, StepIo> }) {
  const [clock, setClock] = useState(Date.now());
  const chatConnection = useHarnessState((s) => s.connection);
  const connection = suppliedConnection || chatConnection;
  // A run can remain open while it waits for another task or for the user.
  // That wall-clock wait is not task execution, so it must not keep a UI
  // "running" timer growing after the scheduler has become idle.
  const activeExecutionCount = run.nodes.filter((node) => node.status === "running").length;
  const waitingForDependency = run.nodes.some((node) => node.status === "waiting_for_dependency");
  const waitingForUser = run.nodes.some((node) => node.status === "waiting_for_user");
  const tickElapsed = !isHarnessTerminal(run.status) && activeExecutionCount > 0;
  useEffect(() => {
    if (!tickElapsed) return;
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [tickElapsed]);
  const serverUpdatedAt = Date.parse(run.updated_at);
  const end = tickElapsed ? clock : (Number.isFinite(serverUpdatedAt) ? serverUpdatedAt : Date.now());
  const startedAt = Date.parse(run.created_at);
  const elapsedSeconds = Math.max(0, Math.floor((end - startedAt) / 1000));
  // Do not show created_at → updated_at while waiting: updated_at can be
  // refreshed by dependency/lease events long after active execution ended,
  // which makes a wall-clock wait look like a 100+ second task duration.
  // A run commonly has a parent waiting on dependencies while its child waits
  // on the user, so user input takes precedence in the public explanation.
  const elapsedLabel = tickElapsed
    ? `流程历时 ${elapsedSeconds}s · ${activeExecutionCount} 项任务执行中`
    : waitingForUser
      ? "任务计时已暂停 · 等待你补充"
      : waitingForDependency
        ? "任务计时已暂停 · 当前等待前置任务"
        : isHarnessTerminal(run.status)
          ? `本轮流程历时 ${elapsedSeconds}s`
          : "任务计时已暂停 · 等待调度";
  const expertAnchorId = expertTransferAnchorId(run.nodes);
  const render = (node: HarnessNode, depth = 0) => {
    const children = sortNodes(run.nodes.filter((n) => n.parent_id === node.id));
    const requests = run.inputs.filter((q) => q.bindings.some((b) => b.task_id === node.id));
    const io = steps?.[node.id];
    const showExperts = node.id === expertAnchorId;
    const showRecommend = isRecommendStep(node);
    return <div key={node.id} className={depth ? "ml-3 border-l border-white/10 pl-3" : ""}>
      <details open={!isHarnessTerminal(node.status) && node.status !== "skipped"}>
        <summary className="cursor-pointer py-2 text-sm text-slate-200"><span>{node.title}</span>{node.actor && <span className="ml-2 text-xs text-slate-400">{node.actor}</span>}
          <span className={`ml-2 text-xs ${node.status === "waiting_for_user" ? "text-amber-200" : node.status === "failed" ? "text-rose-300" : "text-cyan-200"}`}>{labels[node.status] || node.status}</span>
          {(node.attempts ?? 0) > 1 && <span className="ml-2 text-xs text-slate-400">第 {node.attempts} 次执行</span>}
        </summary>
        {(node.action || briefText(node.brief)) && <div className="pb-2 text-xs text-slate-400">
          {node.action ? <p>{node.action}</p> : null}
          {briefText(node.brief) ? <p className="mt-1">任务：{briefText(node.brief)}</p> : null}
          {node.waiting_on?.length ? <p className="mt-1 text-amber-200">等待：{node.waiting_on.join("、")}</p> : null}
        </div>}
        {io ? <StepIoNodes io={io} /> : null}
        {showExperts ? <RoundExpertsTransfer run={run} /> : null}
        {showRecommend ? <JumpCardsTransfer run={run} /> : null}
        {children.map((child) => render(child, depth + 1))}
        {node.result?.conclusion && <div className="mb-2 rounded-lg border border-emerald-300/20 bg-emerald-300/5 p-3 text-xs text-emerald-100">
          <p className="font-medium">专家结论</p><p className="mt-1 whitespace-pre-wrap">{node.result.conclusion}</p>
          {node.result.findings?.length ? <p className="mt-1 text-slate-300">依据：{node.result.findings.join("；")}</p> : null}
          {node.result.limitations?.length ? <p className="mt-1 text-amber-200">限制：{node.result.limitations.join("；")}</p> : null}
        </div>}
        {requests.filter((q) => !["pending", "queued"].includes(q.status)).map((q) => <AgentInputRequest key={q.id} request={q} sessionId={run.session_id} onSubmitAnswer={onSubmitAnswer} />)}
        {node.error && <p className="text-xs text-rose-300">失败原因：{node.error}</p>}
      </details>
    </div>;
  };
  return <section aria-label="任务动态状态" className="rounded-2xl border border-cyan-300/20 bg-cyan-950/20 p-4">
    <p className="text-sm text-cyan-100">{run.goal} · {labels[run.status] || "处理中"}</p>
    {(run.action || run.phase) && !isHarnessTerminal(run.status) && <p className="mt-2 whitespace-pre-wrap text-sm text-slate-300">{run.action || run.phase}</p>}
    <p className="mt-1 text-xs text-slate-400">已完成 {run.completed_count} / {run.task_count} 项任务 · {run.waiting_count} 项等待补充 · {elapsedLabel}</p>
    {run.input_token_limit && <p className="mt-1 text-xs text-slate-400">累计模型输入估算 {run.input_tokens_estimated?.toLocaleString() ?? 0} / {run.input_token_limit.toLocaleString()} token{run.input_tokens_actual != null ? ` · 已回报实际输入 ${run.input_tokens_actual.toLocaleString()} token` : ""}</p>}
    {connection === "reconnecting" && !isHarnessTerminal(run.status) && <p role="status" className="mt-2 text-xs text-amber-200">连接恢复中，后台任务继续执行</p>}
    {run.dispatch && <div className="mt-3 rounded-xl border border-violet-300/20 bg-violet-300/5 p-3">
      <p className="text-sm text-violet-100">任务拆解：已分派 {run.dispatch.planned_expert_count} 位专家、{run.dispatch.planned_task_count} 个任务</p>
      <p className="mt-1 text-xs text-slate-400">当前可执行 {run.dispatch.ready_task_count ?? run.dispatch.parallel_task_count} 个 · 等待依赖 {run.dispatch.blocked_task_count ?? 0} 个 · 最大并行 {run.dispatch.max_parallelism ?? "—"}</p>
      <div className="mt-2 space-y-2">{run.dispatch.experts.map((expert) => <div key={expert.subtask_id} className="rounded-lg border border-white/10 p-2 text-xs">
        <div className="flex items-center justify-between gap-2"><span className="text-slate-200">{expert.name}</span><span className="text-cyan-200">{labels[expert.status] || expert.status}</span></div>
        {expert.brief ? <p className="mt-1 text-slate-400">专家职责：{expert.brief}</p> : null}
        {expert.task_brief ? <p className="mt-1 text-slate-300">本次任务：{expert.task_brief}</p> : null}
        {expert.completion_criteria ? <p className="mt-1 text-slate-400">完成条件：{expert.completion_criteria}</p> : null}
        {expert.depends_on.length ? <p className="mt-1 text-amber-200">依赖：{expert.depends_on.join("、")}</p> : <p className="mt-1 text-slate-500">{expert.can_run_now ? "可并行执行" : "等待调度"}</p>}
      </div>)}</div>
    </div>}
    {sortNodes(run.nodes.filter((n) => !n.parent_id)).map((n) => render(n))}
    {run.synthesis_status === "running" && !isHarnessTerminal(run.status) && <p className="mt-3 rounded-lg border border-cyan-300/20 p-2 text-xs text-cyan-100">编排器正在汇总专家结论</p>}
    {run.recommendations_status === "running" && <p className="mt-2 rounded-lg border border-cyan-300/20 p-2 text-xs text-cyan-100">正在生成相关推荐</p>}
    {run.recommendations_status === "completed" && <div className="mt-2 rounded-lg border border-emerald-300/20 bg-emerald-300/5 p-3 text-xs text-emerald-100">
      <p className="font-medium">相关推荐已生成</p>
      {run.jump_cards?.map((card) => <div key={card.id || `${card.intent}:${card.object_type}:${card.title}`} className="mt-2 rounded-lg border border-white/10 p-2">
        <p className="text-slate-100">{card.title}</p><p className="mt-1 text-slate-300">{card.description}</p>
        {card.reason ? <p className="mt-1 text-slate-400">推荐原因：{card.reason}</p> : null}
      </div>)}
    </div>}
    {run.recommendations_status === "empty" && <p className="mt-2 text-xs text-slate-400">本轮没有匹配的相关推荐</p>}
    {run.recommendations_status === "failed" && <p className="mt-2 text-xs text-amber-200">相关推荐暂时未生成，已保留专家结论和最终回复</p>}
    {run.inputs.filter((q) => q.status === "pending").map((q) => {
      const owner = run.nodes.find((n) => n.id === q.owner_task_id);
      const group = run.input_groups?.find((item) => item.id === q.group_id);
      const progress = group ? `第 ${group.completed_count + 1}/${group.total_count} 题` : undefined;
      return <div key={q.id} className="mt-3">
        <p className="text-xs text-violet-200">{q.actor || owner?.actor || "主 agent"} · {owner?.title || "补充信息"}</p>
        <AgentInputRequest request={q} sessionId={run.session_id} onSubmitAnswer={onSubmitAnswer} groupProgress={progress} />
      </div>;
    })}
    {(run.input_groups || []).filter((group) => group.status === "active" && group.total_count > group.completed_count + 1).map((group) => <div key={group.id} className="mt-2 rounded-lg border border-white/10 p-2 text-xs text-slate-400">
      当前组 · 已完成 {group.completed_count}/{group.total_count} 题，下一题将在本题提交后显示
    </div>)}
    {(run.input_groups || []).filter((group) => group.status === "queued").map((group, index) => <div key={group.id} className="mt-2 rounded-lg border border-white/10 p-2 text-xs text-slate-400">
      后续第 {index + 1} 组 · {group.actors.join("、") || "主 agent"} · {group.total_count} 题
    </div>)}
  </section>;
}
