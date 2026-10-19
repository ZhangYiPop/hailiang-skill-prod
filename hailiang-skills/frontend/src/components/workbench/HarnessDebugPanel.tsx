import { legacyView, subscribeV3, type V3Snapshot } from "@/utils/harnessV3";
import { useEffect, useMemo, useRef, useState } from "react";
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
    || "Harness 会话";
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
  historySessions = [],
  onHistoryChange,
}: {
  baseUrl: string;
  revisionId: string;
  actorId: string;
  soulRevisionId?: string | null;
  historySessions?: HarnessHistorySession[];
  onHistoryChange?: () => void;
}) {
  const v3 = import.meta.env.VITE_HAILIANG_SSE_V3 === 'true';
  const api = v3 ? '/api/v3' : '/api/v2';
  const v3Cursor = useRef({session: '', cursor: -1});
  const acceptV3 = (snapshot: V3Snapshot): HarnessSnapshot => {
    if (v3Cursor.current.session === snapshot.session_id && snapshot.cursor < v3Cursor.current.cursor) return {runtime_mode: snapshot.runtime_mode, cursor: snapshot.cursor, runs: []};
    v3Cursor.current = {session: snapshot.session_id, cursor: snapshot.cursor};
    return legacyView(snapshot);
  };
  const [goal, setGoal] = useState("");
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
    const startKey = `${persistenceKey}:submission`;
    if (v3 && body && path.endsWith('/harness/runs')) {
      const value = body as Record<string, unknown>;
      const fingerprint = JSON.stringify({...value, submission_id: undefined});
      let cached: {fingerprint: string; id: string} | null = null;
      try { cached = JSON.parse(sessionStorage.getItem(startKey) || 'null'); } catch { /* empty */ }
      value.submission_id = cached?.fingerprint === fingerprint ? cached.id : crypto.randomUUID();
      sessionStorage.setItem(startKey, JSON.stringify({fingerprint, id: value.submission_id}));
    }
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
    if (v3 && body && path.endsWith('/harness/runs')) sessionStorage.removeItem(startKey);
    return (v3 && result.protocol === 'hailiang.sse.v3' && Array.isArray(result.runs) ? acceptV3(result as V3Snapshot) : result) as T;
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
          conclusion: id === debugId ? "当前 Harness 会话" : "本地 Harness 会话",
        });
      }
    }
    if (debugId && !byId.has(debugId)) {
      byId.set(debugId, {
        debug_session_id: debugId,
        created_at: "",
        status: evidenceSaved ? "completed" : "active",
        conclusion: "当前 Harness 会话",
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
    if (v3) return subscribeV3(
      async () => {
        const response = await fetch(`${baseUrl}${api}/sessions/${sessionId}/harness?user_id=${encodeURIComponent(userId)}`);
        if (!response.ok) throw new Error('快照读取失败');
        return response.json() as Promise<V3Snapshot>;
      },
      `${baseUrl}${api}/sessions/${sessionId}/harness/events?user_id=${encodeURIComponent(userId)}`,
      snapshot => mergeRuns(acceptV3(snapshot).runs), setConnection);
    let disposed = false;
    let refreshing = false;
    let pendingRefresh = false;
    const query = new URLSearchParams({ user_id: userId });
    const refresh = async () => {
      if (refreshing) { pendingRefresh = true; return; }
      refreshing = true;
      try {
        const snapshot = await request<HarnessSnapshot>(`${api}/sessions/${sessionId}/harness?${query}`);
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
      const data = await request(`${api}/sessions/${sessionId}/harness/runs/${runs[runs.length - 1].run_id}/trace?user_id=${encodeURIComponent(userId)}`);
      setTrace(JSON.stringify(data, null, 2));
    } catch (e) {
      setError(String(e));
    }
  };

  const start = async () => {
    if (active || !goal.trim()) return;
    setBusy(true); setError("");
    try {
      const id = (!evidenceSaved && debugId) || (await request<{ debug_session_id: string }>("/workbench/v1/revision-tests", { revision_id: revisionId, actor_id: actorId, soul_revision_id: soulRevisionId || null })).debug_session_id;
      if (id !== debugId) setRuns([]);
      await request(`${api}/sessions/harness-${id}/harness/runs`, { ...(v3 ? {submission_id: crypto.randomUUID()} : {}), goal, context_data: { user_id: userId }, debug_session_id: id });
      setDebugId(id);
      sessionStorage.setItem(persistenceKey, id);
      rememberSession(id);
      setEvidenceSaved(false);
      const snapshot = await request<HarnessSnapshot>(`${api}/sessions/harness-${id}/harness?user_id=${encodeURIComponent(userId)}`);
      mergeRuns(snapshot.runs); setGoal("");
      onHistoryChange?.();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  const submit: SubmitHarnessAnswer = async (input, answers, skipped, submissionId) => {
    await request(`${api}/sessions/${sessionId}/input-requests/${input.id}/answers`, { user_id: userId, profile_id: null, submission_id: submissionId, expected_version: input.version, skipped, answers: skipped ? {} : answers });
    const snapshot = await request<HarnessSnapshot>(`${api}/sessions/${sessionId}/harness?user_id=${encodeURIComponent(userId)}`);
    mergeRuns(snapshot.runs);
  };

  const saveEvidence = async () => {
    if (active || !conclusion.trim() || !debugId) return;
    setBusy(true); setError("");
    try {
      for (const run of runs) await request(`/workbench/v1/debug-sessions/${debugId}/turns`, {
        actor_id: actorId, user_message: run.goal, assistant_message: run.result || `任务状态：${run.status}，原因：${run.error || "未返回结论"}`,
        trace: [{ runtime_mode: "harness_v1", run_id: run.run_id, version: run.version, status: run.status, nodes: run.nodes }],
      });
      await request(`/workbench/v1/debug-sessions/${debugId}/complete`, { actor_id: actorId, conclusion });
      setEvidenceSaved(true);
      onHistoryChange?.();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <label className="block shrink-0 text-xs text-slate-400">
        历史会话
        <select
          value={debugId}
          onChange={(event) => selectSession(event.target.value)}
          className="mt-1 w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-400/40"
        >
          <option value="">新建 Harness 会话</option>
          {sessionOptions.map((item) => (
            <option key={item.debug_session_id} value={item.debug_session_id}>
              {sessionLabel(item)}
            </option>
          ))}
        </select>
      </label>

      <div className="flex min-h-0 flex-1 gap-3">
        <div className="flex w-[42%] min-w-[200px] shrink-0 flex-col gap-3">
          <p className="shrink-0 text-sm leading-6 text-slate-400">
            用完整任务目标验证并行分派、工具追问与恢复；补充表单、回复与轨迹都在右侧。
          </p>
          <textarea
            aria-label="Harness 测试目标"
            value={goal}
            disabled={Boolean(active) && !v3 || busy || !revisionId}
            onChange={(e) => setGoal(e.target.value)}
            className="min-h-[8rem] w-full flex-1 rounded-lg border border-white/10 bg-slate-950 p-3 text-sm outline-none focus:border-cyan-400/40 disabled:opacity-40"
            placeholder={revisionId ? "输入完整任务，测试专家分派、追问与恢复" : "请先选择要测试的修订"}
            rows={6}
          />
          {error ? <p role="alert" className="shrink-0 text-xs text-rose-300">{error}</p> : null}
          <div className="flex shrink-0 flex-wrap gap-3">
            <button
              type="button"
              disabled={Boolean(active) && !v3 || busy || !revisionId || !goal.trim()}
              onClick={() => void start()}
              className="rounded-lg bg-cyan-400/15 px-3 py-2 text-xs text-cyan-100 disabled:opacity-40"
            >
              {active && v3 ? "发送并终止上一轮" : "开始 Harness 测试"}
            </button>
            {active ? (
              <button
                type="button"
                onClick={() => void request(`${api}/sessions/${sessionId}/harness/runs/${active.run_id}/cancel`, { user_id: userId, profile_id: null }).catch((e) => setError(String(e)))}
                className="text-xs text-rose-200"
              >
                停止任务
              </button>
            ) : null}
            {runs.length > 0 ? (
              <button
                type="button"
                onClick={() => {
                  setRightPane("trace");
                  void loadTrace();
                }}
                className="text-xs text-slate-300"
              >
                查看调用追踪
              </button>
            ) : null}
          </div>
          {runs.length > 0 && !active ? (
            <div className="shrink-0 space-y-2 border-t border-white/10 pt-3">
              <textarea
                aria-label="Harness 调试结论"
                value={conclusion}
                disabled={busy || evidenceSaved}
                onChange={(e) => setConclusion(e.target.value)}
                className="w-full rounded-lg border border-white/10 bg-slate-950 p-2 text-sm outline-none focus:border-violet-400/40 disabled:opacity-40"
                placeholder="记录人工核查结论，说明通过或发现的问题"
                rows={2}
              />
              <button
                type="button"
                disabled={busy || evidenceSaved || !conclusion.trim()}
                onClick={() => void saveEvidence()}
                className="rounded-lg bg-violet-400/15 px-3 py-2 text-xs text-violet-100 disabled:opacity-40"
              >
                保存为调试依据
              </button>
              {evidenceSaved ? (
                <p className="text-xs text-slate-300">已保存调试依据：{debugId}。</p>
              ) : null}
            </div>
          ) : null}
        </div>

        <div className="flex min-h-0 min-w-0 flex-1 flex-col rounded-2xl border border-cyan-300/15 bg-slate-950/50">
          <div className="flex shrink-0 gap-1 border-b border-white/10 p-1">
            <button
              type="button"
              onClick={() => setRightPane("reply")}
              className={`flex-1 rounded-lg px-3 py-1.5 text-xs transition ${rightPane === "reply" ? "bg-cyan-400/15 text-cyan-100" : "text-slate-400 hover:text-slate-200"}`}
            >
              对话
            </button>
            <button
              type="button"
              onClick={() => {
                setRightPane("trace");
                if (!trace && runs.length) void loadTrace();
              }}
              className={`flex-1 rounded-lg px-3 py-1.5 text-xs transition ${rightPane === "trace" ? "bg-cyan-400/15 text-cyan-100" : "text-slate-400 hover:text-slate-200"}`}
            >
              轨迹过程
            </button>
          </div>
          <div className="min-h-0 flex-1 space-y-4 overflow-auto p-4">
            {rightPane === "reply" ? (
              <div className="space-y-4">
                {runs.length ? (
                  runs.map((run) => {
                    const pending = run.inputs.filter((item) => item.status === "pending");
                    return (
                      <div key={`chat-${run.run_id}`} className="space-y-3 rounded-2xl border border-cyan-300/15 bg-cyan-950/10 p-4">
                        <p className="text-sm text-cyan-100">
                          {run.goal} · {isHarnessTerminal(run.status) ? "已结束" : run.status === "waiting_for_user" ? "等待补充" : "进行中"}
                        </p>
                        {run.phase && !isHarnessTerminal(run.status) ? (
                          <p className="whitespace-pre-wrap text-sm text-slate-300">{run.phase}</p>
                        ) : null}
                        <p className="text-xs text-slate-500">
                          已完成 {run.completed_count} / {run.task_count} · 等待补充 {run.waiting_count}
                          {connection === "reconnecting" && !isHarnessTerminal(run.status) ? " · 连接恢复中" : ""}
                        </p>
                        {pending.length ? (
                          pending.map((item) => {
                            const owner = run.nodes.find((node) => node.id === item.bindings[0]?.task_id);
                            return (
                              <div key={item.id}>
                                <p className="mb-1 text-xs text-violet-200">{owner?.actor || "主 agent"} · {owner?.title || "补充信息"}</p>
                                <AgentInputRequest request={item} sessionId={run.session_id} onSubmitAnswer={submit} />
                              </div>
                            );
                          })
                        ) : !run.result ? (
                          <p className="text-xs text-slate-500">任务树与调用详情见「轨迹过程」。</p>
                        ) : null}
                        {run.result ? (
                          <div className="space-y-2 border-t border-white/10 pt-3">
                            <p className="text-[11px] text-slate-500">结论</p>
                            <MarkdownContent content={run.result} />
                          </div>
                        ) : null}
                      </div>
                    );
                  })
                ) : (
                  <p className="py-10 text-center text-sm text-slate-600">补充表单与结论会显示在这里；任务树在「轨迹过程」。</p>
                )}
              </div>
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
                    {runs.length ? "正在加载或尚未拉取调用追踪；也可点左侧「查看调用追踪」。" : "开始任务后可在此查看轨迹过程。"}
                  </p>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
