import { useEffect, useState } from "react";
import { TaskProgressBlock, type SubmitHarnessAnswer } from "@/components/message-blocks/TaskProgressBlock";
import { MarkdownContent } from "@/components/MarkdownContent";
import type { HarnessRun, HarnessSnapshot } from "@/types/harness";
import { isHarnessTerminal } from "@/types/harness";

export function HarnessDebugPanel({ baseUrl, revisionId, actorId, soulRevisionId }: { baseUrl: string; revisionId: string; actorId: string; soulRevisionId: string }) {
  const [goal, setGoal] = useState("");
  const [debugId, setDebugId] = useState("");
  const [runs, setRuns] = useState<HarnessRun[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [trace, setTrace] = useState("");
  const [conclusion, setConclusion] = useState("");
  const [evidenceSaved, setEvidenceSaved] = useState(false);
  const [connection, setConnection] = useState("connected");
  const userId = `workbench-harness:${actorId}`;
  const sessionId = `harness-${debugId}`;
  const active = runs.find((r) => !isHarnessTerminal(r.status));
  const persistenceKey = `harness-debug:${baseUrl}:${actorId}:${revisionId}:${soulRevisionId}`;
  const mergeRuns = (incoming: HarnessRun[]) => setRuns((previous) => {
    const runs = { ...Object.fromEntries(previous.map((run) => [run.run_id, run])) };
    for (const run of incoming) if (!runs[run.run_id] || runs[run.run_id].version <= run.version) runs[run.run_id] = run;
    return Object.values(runs).sort((a, b) => a.created_at.localeCompare(b.created_at));
  });
  const request = async <T,>(path: string, body?: unknown): Promise<T> => {
    const response = await fetch(`${baseUrl}${path}`, { method: body ? "POST" : "GET", headers: { "Content-Type": "application/json" }, ...(body ? { body: JSON.stringify(body) } : {}) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || result.code || String(result.detail));
    return result;
  };
  useEffect(() => { setDebugId(sessionStorage.getItem(persistenceKey) || ""); setRuns([]); setTrace(""); setEvidenceSaved(false); setConclusion(""); }, [persistenceKey]);
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
    // A candidate test owns its identity and does not modify the chat store.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl, debugId, userId, sessionId]);
  const start = async () => {
    if (active || !goal.trim()) return;
    setBusy(true); setError("");
    try {
      const id = (!evidenceSaved && debugId) || (await request<{ debug_session_id: string }>("/workbench/v1/revision-tests", { revision_id: revisionId, actor_id: actorId, soul_revision_id: soulRevisionId || null })).debug_session_id;
      if (id !== debugId) setRuns([]);
      await request(`/api/v2/sessions/harness-${id}/harness/runs`, { goal, context_data: { user_id: userId }, debug_session_id: id });
      setDebugId(id);
      sessionStorage.setItem(persistenceKey, id);
      setEvidenceSaved(false);
      const snapshot = await request<HarnessSnapshot>(`/api/v2/sessions/harness-${id}/harness?user_id=${encodeURIComponent(userId)}`);
      mergeRuns(snapshot.runs); setGoal("");
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const submit: SubmitHarnessAnswer = async (input, answers, skipped, submissionId) => {
    await request(`/api/v2/sessions/${sessionId}/input-requests/${input.id}/answers`, { user_id: userId, profile_id: null, submission_id: submissionId, expected_version: input.version, skipped, answers: skipped ? {} : answers });
    const snapshot = await request<HarnessSnapshot>(`/api/v2/sessions/${sessionId}/harness?user_id=${encodeURIComponent(userId)}`);
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
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  return <details className="mt-4 rounded-xl border border-cyan-300/20 p-3">
    <summary className="cursor-pointer text-sm text-cyan-100">Harness 候选测试（并行任务与工具恢复）</summary>
    <div className="mt-3 space-y-3">
      {runs.map((run) => <div key={run.run_id}><TaskProgressBlock run={run} onSubmitAnswer={submit} connection={connection} />{run.result && <MarkdownContent content={run.result} />}</div>)}
      <textarea aria-label="Harness 测试目标" value={goal} disabled={Boolean(active) || busy} onChange={(e) => setGoal(e.target.value)} className="w-full rounded-lg bg-slate-950 p-3 text-sm" placeholder="输入完整任务，测试专家分派、追问与恢复" />
      {error && <p role="alert" className="text-xs text-rose-300">{error}</p>}
      <div className="flex gap-3">
        <button disabled={Boolean(active) || busy || !revisionId} onClick={() => void start()} className="rounded-lg bg-cyan-400/15 px-3 py-2 text-xs">开始 Harness 测试</button>
        {active && <button onClick={() => void request(`/api/v2/sessions/${sessionId}/harness/runs/${active.run_id}/cancel`, { user_id: userId, profile_id: null }).catch((e) => setError(String(e)))} className="text-xs text-rose-200">停止任务</button>}
        {runs.length > 0 && <button onClick={() => void request(`/api/v2/sessions/${sessionId}/harness/runs/${runs[runs.length - 1].run_id}/trace?user_id=${encodeURIComponent(userId)}`).then((data) => setTrace(JSON.stringify(data, null, 2))).catch((e) => setError(String(e)))} className="text-xs text-slate-300">查看调用追踪</button>}
      </div>
      {trace && <pre className="max-h-80 overflow-auto whitespace-pre-wrap text-xs text-slate-400">{trace}</pre>}
      {runs.length > 0 && !active && <div className="space-y-2 border-t border-white/10 pt-3">
        <textarea aria-label="Harness 调试结论" value={conclusion} disabled={busy || evidenceSaved} onChange={(e) => setConclusion(e.target.value)} className="w-full rounded-lg bg-slate-950 p-2 text-sm" placeholder="记录人工核查结论，说明通过或发现的问题" />
        <button disabled={busy || evidenceSaved || !conclusion.trim()} onClick={() => void saveEvidence()} className="rounded-lg bg-violet-400/15 px-3 py-2 text-xs">保存为调试依据</button>
        {evidenceSaved && <p className="text-xs text-slate-300">已保存调试依据：{debugId}。可在发布流程中选择该记录。</p>}
      </div>}
    </div>
  </details>;
}
