import { useEffect, useMemo, useState } from "react";
import { AgentInputRequest, TaskProgressBlock, type SubmitHarnessAnswer } from "@/components/message-blocks/TaskProgressBlock";
import { MarkdownContent } from "@/components/MarkdownContent";
import { HarnessContextDiagnostics } from "./HarnessContextDiagnostics";
import type { HarnessRun, HarnessSnapshot } from "@/types/harness";
import { isHarnessTerminal } from "@/types/harness";

export type HarnessHistorySession = {
  debug_session_id: string;
  created_at: string;
  status: string;
  conclusion?: string;
  transcript?: Array<{ role?: string; content?: string }>;
  trace?: Array<Record<string, unknown>>;
};

function formatSessionTime(value: string): string {
  try {
    return new Date(value).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch {
    return value;
  }
}

function sessionLabel(item: HarnessHistorySession): string {
  const first = item.transcript?.find((row) => row.role === "user")?.content
    || item.conclusion
    || "问答会话";
  const status = item.status === "completed" ? "已完成" : "进行中";
  return `${formatSessionTime(item.created_at)} · ${status} · ${first.slice(0, 28)}${first.length > 28 ? "…" : ""}`;
}

function isHarnessSession(item: HarnessHistorySession): boolean {
  return (item.trace || []).some((turn) => turn.runtime_mode === "harness_v1" || Boolean(turn.run_id));
}

export function HarnessDebugPanel({
  baseUrl,
  revisionId,
  actorId,
  soulRevisionId,
  objectType,
  historySessions = [],
  onHistoryChange,
  onEvidenceSaved,
}: {
  baseUrl: string;
  revisionId: string;
  actorId: string;
  soulRevisionId?: string | null;
  objectType?: string;
  historySessions?: HarnessHistorySession[];
  onHistoryChange?: () => void;
  onEvidenceSaved?: (debugSessionId: string) => void;
}) {
  const [goal, setGoal] = useState("");
  // 正式流程里身份、学段由平台前置收集；测试时在这里预置，避免专家被反复追问。
  const [userRole, setUserRole] = useState("家长");
  const [schoolStage, setSchoolStage] = useState("高中");
  const [gradeName, setGradeName] = useState("");
  const [debugId, setDebugId] = useState("");
  const [runs, setRuns] = useState<HarnessRun[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [trace, setTrace] = useState("");
  const [rightPane, setRightPane] = useState<"reply" | "trace">("reply");
  const [conclusion, setConclusion] = useState("");
  const [evidenceSaved, setEvidenceSaved] = useState(false);
  const [connection, setConnection] = useState("connected");
  const [knownSessionIds, setKnownSessionIds] = useState<string[]>([]);
  const userId = `workbench-harness:${actorId}`;
  const sessionId = `harness-${debugId}`;
  const active = runs.find((r) => !isHarnessTerminal(r.status));
  const persistenceKey = `harness-debug:${baseUrl}:${actorId}:${revisionId}:${soulRevisionId || ""}`;
  const knownKey = `${persistenceKey}:known`;

  const mergeRuns = (incoming: HarnessRun[]) => setRuns((previous) => {
    const runs = { ...Object.fromEntries(previous.map((run) => [run.run_id, run])) };
    for (const run of incoming) if (!runs[run.run_id] || runs[run.run_id].version <= run.version) runs[run.run_id] = run;
    return Object.values(runs).sort((a, b) => a.created_at.localeCompare(b.created_at));
  });

  const request = async <T,>(path: string, body?: unknown): Promise<T> => {
    let response: Response;
    try {
      response = await fetch(`${baseUrl}${path}`, {
        method: body ? "POST" : "GET",
        headers: { "Content-Type": "application/json" },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
    } catch (error) {
      const hint = baseUrl ? `（接口 ${baseUrl}${path}）` : "";
      throw new Error(`网络请求失败${hint}：${error instanceof Error ? error.message : String(error)}`);
    }
    const result = await response.json().catch(() => ({} as Record<string, unknown>));
    if (!response.ok) {
      const detail = result?.detail;
      const detailMessage =
        typeof detail === "string"
          ? detail
          : detail && typeof detail === "object" && typeof (detail as { message?: unknown }).message === "string"
            ? (detail as { message: string }).message
            : "";
      throw new Error(result?.message || detailMessage || result?.code || `HTTP ${response.status}`);
    }
    return result as T;
  };

  const rememberSession = (id: string) => {
    setKnownSessionIds((current) => {
      const next = [id, ...current.filter((item) => item !== id)].slice(0, 30);
      try { sessionStorage.setItem(knownKey, JSON.stringify(next)); } catch { /* ignore */ }
      return next;
    });
  };

  const sessionOptions = useMemo(() => {
    const fromHistory = historySessions.filter((item) => isHarnessSession(item) || knownSessionIds.includes(item.debug_session_id));
    const byId = new Map(fromHistory.map((item) => [item.debug_session_id, item]));
    for (const id of knownSessionIds) {
      if (!byId.has(id)) {
        byId.set(id, {
          debug_session_id: id,
          created_at: "",
          status: id === debugId ? "active" : "active",
          conclusion: id === debugId ? "当前问答会话" : "本地问答会话",
        });
      }
    }
    if (debugId && !byId.has(debugId)) {
      byId.set(debugId, {
        debug_session_id: debugId,
        created_at: "",
        status: evidenceSaved ? "completed" : "active",
        conclusion: "当前问答会话",
      });
    }
    return Array.from(byId.values()).sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  }, [historySessions, knownSessionIds, debugId, evidenceSaved]);

  useEffect(() => {
    let known: string[] = [];
    try { known = JSON.parse(sessionStorage.getItem(knownKey) || "[]"); } catch { known = []; }
    setKnownSessionIds(Array.isArray(known) ? known.filter((item) => typeof item === "string") : []);
    setDebugId(sessionStorage.getItem(persistenceKey) || "");
    setRuns([]);
    setTrace("");
    setEvidenceSaved(false);
    setConclusion("");
  }, [persistenceKey, knownKey]);

  useEffect(() => {
    if (!debugId) return;
    let disposed = false;
    let refreshing = false;
    let pendingRefresh = false;
    const query = new URLSearchParams({ user_id: userId });
    const refresh = async () => {
      if (refreshing) { pendingRefresh = true; return; }
      refreshing = true;
      try {
        const snapshot = await request<HarnessSnapshot>(`/api/v2/sessions/${sessionId}/harness?${query}`);
        if (!disposed) mergeRuns(snapshot.runs);
      } catch (e) { if (!disposed) setError(String(e)); }
      finally { refreshing = false; if (pendingRefresh && !disposed) { pendingRefresh = false; void refresh(); } }
    };
    const source = new EventSource(`${baseUrl}/api/v2/sessions/${sessionId}/harness/events?${query}`);
    source.addEventListener("harness", () => void refresh());
    source.onopen = () => { if (!disposed) { setConnection("connected"); void refresh(); } };
    source.onerror = () => { if (!disposed) setConnection("reconnecting"); };
    void refresh();
    return () => { disposed = true; source.close(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl, debugId, userId, sessionId]);

  const selectSession = (nextId: string) => {
    setError("");
    setTrace("");
    setRightPane("reply");
    setConclusion("");
    setEvidenceSaved(false);
    setRuns([]);
    setDebugId(nextId);
    if (nextId) {
      sessionStorage.setItem(persistenceKey, nextId);
      rememberSession(nextId);
    } else {
      sessionStorage.removeItem(persistenceKey);
    }
  };

  const loadTrace = async () => {
    if (!runs.length || !debugId) return;
    try {
      const data = await request(`/api/v2/sessions/${sessionId}/harness/runs/${runs[runs.length - 1].run_id}/trace?user_id=${encodeURIComponent(userId)}`);
      setTrace(JSON.stringify(data, null, 2));
    } catch (e) {
      setError(String(e));
    }
  };

  const start = async () => {
    if (active || busy || !goal.trim()) return;
    setBusy(true); setError(""); setTrace("");
    try {
      const id = (!evidenceSaved && debugId) || (await request<{ debug_session_id: string }>("/workbench/v1/revision-tests", { revision_id: revisionId, actor_id: actorId, soul_revision_id: soulRevisionId || null })).debug_session_id;
      if (id !== debugId) setRuns([]);
      const presetFacts = objectType === "skill" ? {} : { user_role: userRole, school_stage: schoolStage };
      await request(`/api/v2/sessions/harness-${id}/harness/runs`, {
        goal,
        context_data: { user_id: userId, facts: presetFacts, ...(gradeName && objectType !== "skill" ? { grade: gradeName } : {}) },
        debug_session_id: id,
      });
      setDebugId(id);
      sessionStorage.setItem(persistenceKey, id);
      rememberSession(id);
      setEvidenceSaved(false);
      const snapshot = await request<HarnessSnapshot>(`/api/v2/sessions/harness-${id}/harness?user_id=${encodeURIComponent(userId)}`);
      mergeRuns(snapshot.runs); setGoal("");
      onHistoryChange?.();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  const submit: SubmitHarnessAnswer = async (input, answers, skipped, submissionId) => {
    await request(`/api/v2/sessions/${sessionId}/input-requests/${input.id}/answers`, { user_id: userId, profile_id: null, submission_id: submissionId, expected_version: input.version, skipped, answers: skipped ? {} : answers });
    const snapshot = await request<HarnessSnapshot>(`/api/v2/sessions/${sessionId}/harness?user_id=${encodeURIComponent(userId)}`);
    mergeRuns(snapshot.runs);
  };

  const saveEvidence = async () => {
    if (active || !debugId) return;
    setBusy(true); setError("");
    try {
      for (const run of runs) await request(`/workbench/v1/debug-sessions/${debugId}/turns`, {
        actor_id: actorId, user_message: run.goal, assistant_message: run.result || `任务状态：${run.status}，原因：${run.error || "未返回结论"}`,
        trace: [{ runtime_mode: "harness_v1", run_id: run.run_id, version: run.version, status: run.status, nodes: run.nodes }],
      });
      await request(`/workbench/v1/debug-sessions/${debugId}/complete`, {
        actor_id: actorId,
        conclusion: conclusion.trim() || "业务人员已在候选修订测试台完成验证，并确认当前修订可进入发布流程。",
      });
      setEvidenceSaved(true);
      onHistoryChange?.();
      onEvidenceSaved?.(debugId);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  const canSend = Boolean(revisionId) && !active && !busy && !evidenceSaved && Boolean(goal.trim());

  const flowHint = objectType === "expert_team"
    ? "完整流程：身份学段检查 → 编排首 → 专家 → 编排尾 → 推荐"
    : objectType === "expert"
      ? "单专家：直接由该专家作答，不经过编排首和编排尾"
      : objectType === "skill"
        ? "单 Skill：通过临时测试专家调用，不经过编排"
        : "";

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      {flowHint ? <p className="shrink-0 rounded-lg border border-sky-300/15 bg-sky-400/5 px-3 py-2 text-xs text-sky-100">{flowHint}</p> : null}
      {objectType && objectType !== "skill" ? (
        <div className="grid shrink-0 grid-cols-3 gap-2 text-xs text-slate-400">
          {([
            ["身份", userRole, setUserRole, ["家长", "学生"]],
            ["学段", schoolStage, setSchoolStage, ["小学", "初中", "高中"]],
            ["年级", gradeName, setGradeName, ["", "初一", "初二", "初三", "高一", "高二", "高三"]],
          ] as Array<[string, string, (value: string) => void, string[]]>).map(([label, value, setter, options]) => (
            <label key={label} className="block">
              {label}
              <select
                value={value}
                disabled={Boolean(debugId)}
                onChange={(event) => setter(event.target.value)}
                title={debugId ? "预置信息只在新建会话时生效" : "测试预置信息，等同正式流程里平台前置收集的结果"}
                className="mt-1 w-full rounded-lg border border-white/10 bg-slate-950 px-2 py-1.5 text-sm text-slate-100 outline-none disabled:opacity-50"
              >
                {options.map((option) => <option key={option} value={option}>{option || "不预置"}</option>)}
              </select>
            </label>
          ))}
        </div>
      ) : null}
      <label className="block shrink-0 text-xs text-slate-400">
        历史会话
        <select
          value={debugId}
          onChange={(event) => selectSession(event.target.value)}
          className="mt-1 w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-sky-400/40"
        >
          <option value="">新建问答会话</option>
          {sessionOptions.map((item) => (
            <option key={item.debug_session_id} value={item.debug_session_id}>
              {sessionLabel(item)}
            </option>
          ))}
        </select>
      </label>

      <div className="flex min-h-[20rem] min-w-0 flex-1 flex-col rounded-2xl border border-sky-300/15 bg-slate-950/50">
        <div className="flex shrink-0 gap-1 border-b border-white/10 p-1">
          <button
            type="button"
            onClick={() => setRightPane("reply")}
            className={`flex-1 rounded-lg px-3 py-1.5 text-xs transition ${rightPane === "reply" ? "bg-sky-400/15 text-sky-100" : "text-slate-400 hover:text-slate-200"}`}
          >
            对话
          </button>
          <button
            type="button"
            onClick={() => {
              setRightPane("trace");
              if (!trace && runs.length) void loadTrace();
            }}
            className={`flex-1 rounded-lg px-3 py-1.5 text-xs transition ${rightPane === "trace" ? "bg-sky-400/15 text-sky-100" : "text-slate-400 hover:text-slate-200"}`}
          >
            轨迹过程{runs.length ? ` · ${runs.length}` : ""}
          </button>
        </div>
        <div className="min-h-0 flex-1 space-y-4 overflow-auto p-4">
          {rightPane === "reply" ? (
            runs.length ? (
              runs.map((run) => {
                const pending = run.inputs.filter((item) => item.status === "pending");
                const terminal = isHarnessTerminal(run.status);
                return (
                  <div key={`chat-${run.run_id}`} className="space-y-3">
                    <div className="text-right">
                      <div className="inline-block max-w-[95%] whitespace-pre-wrap rounded-2xl bg-sky-400 px-3 py-2 text-left text-sm leading-6 text-slate-950">
                        {run.goal}
                      </div>
                    </div>
                    <div className="text-left">
                      <div className="inline-block max-w-[95%] space-y-3 rounded-2xl bg-white/10 px-3 py-2 text-sm leading-6 text-slate-200">
                        {!terminal ? (
                          <div className="space-y-1">
                            <p className="text-xs text-sky-200">
                              {run.status === "waiting_for_user" ? "等待补充" : "进行中"}
                              {connection === "reconnecting" ? " · 连接恢复中" : ""}
                            </p>
                            {run.phase ? <p className="whitespace-pre-wrap text-sm text-slate-300">{run.phase}</p> : null}
                            <p className="text-xs text-slate-500">
                              已完成 {run.completed_count} / {run.task_count} · 等待补充 {run.waiting_count}
                            </p>
                          </div>
                        ) : null}
                        {pending.map((item) => {
                          const owner = run.nodes.find((node) => node.id === item.bindings[0]?.task_id);
                          return (
                            <div key={item.id}>
                              <p className="mb-1 text-xs text-violet-200">{owner?.actor || "主 agent"} · {owner?.title || "补充信息"}</p>
                              <AgentInputRequest request={item} sessionId={run.session_id} onSubmitAnswer={submit} />
                            </div>
                          );
                        })}
                        {run.result ? (
                          <MarkdownContent content={run.result} />
                        ) : null}
                        {run.jump_cards?.length ? (
                          <div className="flex flex-wrap gap-2 pt-1">
                            {run.jump_cards.map((card, index) => (
                              <span key={index} className="rounded-full border border-sky-300/30 px-3 py-1 text-xs text-sky-100">
                                {String((card as Record<string, unknown>).title || (card as Record<string, unknown>).id || "推荐")}
                              </span>
                            ))}
                          </div>
                        ) : null}
                        {!run.result && terminal ? (
                          <p className="text-sm text-slate-400">任务状态：{run.status}{run.error ? `，原因：${run.error}` : ""}</p>
                        ) : null}
                      </div>
                    </div>
                  </div>
                );
              })
            ) : (
              <p className="py-10 text-center text-sm text-slate-600">选择修订并提问后，回复会出现在这里。</p>
            )
          ) : (
            <div className="space-y-4">
              {runs.length ? (
                <div className="space-y-2 rounded-xl border border-white/10 bg-black/20 p-3">
                  <p className="text-xs font-medium text-slate-300">任务树与执行过程</p>
                  {runs.map((run) => (
                    <TaskProgressBlock key={`trace-${run.run_id}`} run={run} connection={connection} />
                  ))}
                </div>
              ) : null}
              {trace ? (
                <>
                  <HarnessContextDiagnostics trace={trace} />
                  <pre className="whitespace-pre-wrap rounded-xl border border-white/10 bg-black/20 p-3 text-xs text-slate-400">
                    {trace}
                  </pre>
                </>
              ) : (
                <p className="py-6 text-center text-sm text-slate-600">
                  {runs.length ? "正在加载调用追踪…" : "尚无调用轨迹；完成一轮问答后可在此查看。"}
                </p>
              )}
            </div>
          )}
        </div>
      </div>

      {error ? <p role="alert" className="shrink-0 text-xs text-rose-300">{error}</p> : null}

      {runs.length > 0 && !active ? (
        <div className="flex shrink-0 items-center gap-2">
          <input
            aria-label="测试结论"
            value={conclusion}
            disabled={busy || evidenceSaved}
            onChange={(e) => setConclusion(e.target.value)}
            className="min-w-0 flex-1 rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-xs outline-none focus:border-emerald-400/40 disabled:opacity-40"
            placeholder="测试结论（可选）：说明通过或发现的问题"
          />
          <button
            type="button"
            disabled={busy || evidenceSaved}
            onClick={() => void saveEvidence()}
            title="把本会话记成发布候选修订所需的测试证据"
            className="shrink-0 rounded-lg border border-emerald-400/25 bg-emerald-400/10 px-3 py-2 text-xs text-emerald-200 disabled:opacity-40"
          >
            {evidenceSaved ? "已记为测试证据" : "记为测试证据"}
          </button>
        </div>
      ) : null}

      <div className="flex shrink-0 items-end gap-2 border-t border-white/10 pt-3">
        <textarea
          aria-label="测试问题"
          value={goal}
          disabled={Boolean(active) || busy || !revisionId || evidenceSaved}
          onChange={(e) => setGoal(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              if (canSend) void start();
            }
          }}
          className="max-h-40 min-h-[3.25rem] w-full flex-1 resize-y rounded-lg border border-white/10 bg-slate-950 p-3 text-sm outline-none focus:border-sky-400/40 disabled:opacity-40"
          placeholder={
            !revisionId
              ? "请先选择要测试的修订"
              : evidenceSaved
                ? "该会话已记为测试证据，请新建问答会话继续测试"
                : "输入测试问题，Enter 发送，Shift+Enter 换行"
          }
          rows={2}
        />
        {active ? (
          <button
            type="button"
            onClick={() => void request(`/api/v2/sessions/${sessionId}/harness/runs/${active.run_id}/cancel`, { user_id: userId, profile_id: null }).catch((e) => setError(String(e)))}
            className="shrink-0 rounded-lg border border-rose-300/30 px-4 py-3 text-sm text-rose-100"
          >
            停止
          </button>
        ) : (
          <button
            type="button"
            disabled={!canSend}
            onClick={() => void start()}
            className="shrink-0 rounded-lg bg-sky-400 px-4 py-3 text-sm font-medium text-slate-950 disabled:opacity-40"
          >
            发送
          </button>
        )}
      </div>
      <p className="shrink-0 text-[11px] leading-5 text-slate-500">匿名会话：不读写孩子档案；任务树、分派与追问都在「轨迹过程」。</p>
    </div>
  );
}
