import { testerModelName, TESTER_MODEL_OPTIONS, useChatStore } from "@/store/useChatStore";
import { legacyView, subscribeV3, type V3Snapshot } from "@/utils/harnessV3";
import { createUuid } from "@/utils/uuid";
import { useEffect, useMemo, useRef, useState } from "react";
import { AgentInputRequest, TaskProgressBlock, type StepIo, type SubmitHarnessAnswer } from "@/components/message-blocks/TaskProgressBlock";

const GRADE_OPTIONS: Record<string, string[]> = {
  "小学": ["一年级", "二年级", "三年级", "四年级", "五年级", "六年级"],
  "初中": ["初一", "初二", "初三"],
  "高中": ["高一", "高二", "高三"],
};

const PROVINCES = [
  "北京", "天津", "上海", "重庆",
  "河北", "山西", "辽宁", "吉林", "黑龙江",
  "江苏", "浙江", "安徽", "福建", "江西", "山东",
  "河南", "湖北", "湖南", "广东", "海南",
  "四川", "贵州", "云南", "陕西", "甘肃", "青海",
  "广西", "内蒙古", "西藏", "宁夏", "新疆",
];

// 高考改革方案：传统/3+3/3+1+2
const PROVINCE_SCHEME: Record<string, "traditional" | "three_three" | "three_one_two"> = {
  "北京": "three_three", "天津": "three_three", "上海": "three_three",
  "浙江": "three_three", "山东": "three_three", "海南": "three_three",
  "河北": "three_one_two", "山西": "three_one_two", "辽宁": "three_one_two",
  "吉林": "three_one_two", "黑龙江": "three_one_two", "江苏": "three_one_two",
  "安徽": "three_one_two", "福建": "three_one_two", "江西": "three_one_two",
  "河南": "three_one_two", "湖北": "three_one_two", "湖南": "three_one_two",
  "广东": "three_one_two", "重庆": "three_one_two", "四川": "three_one_two",
  "贵州": "three_one_two", "云南": "three_one_two", "陕西": "three_one_two",
  "甘肃": "three_one_two", "青海": "three_one_two", "广西": "three_one_two",
  "内蒙古": "three_one_two", "西藏": "three_one_two", "宁夏": "three_one_two",
  "新疆": "three_one_two",
};

const SUBJECT_COMBINATIONS: Record<string, string[]> = {
  traditional: ["文科（政史地）", "理科（物化生）"],
  three_three: [
    "物理+化学+生物", "物理+化学+地理", "物理+化学+政治",
    "物理+生物+地理", "物理+生物+政治", "物理+地理+政治",
    "历史+化学+生物", "历史+化学+地理", "历史+化学+政治",
    "历史+生物+地理", "历史+生物+政治", "历史+地理+政治",
    "化学+生物+地理", "化学+生物+政治", "化学+地理+政治",
    "生物+地理+政治",
  ],
  three_one_two: [
    "物理+化学+生物", "物理+化学+地理", "物理+化学+政治",
    "物理+生物+地理", "物理+生物+政治", "物理+地理+政治",
    "历史+化学+生物", "历史+化学+地理", "历史+化学+政治",
    "历史+生物+地理", "历史+生物+政治", "历史+地理+政治",
  ],
};
import { MarkdownContent } from "@/components/MarkdownContent";
import { HarnessContextDiagnostics } from "./HarnessContextDiagnostics";
import { HarnessExecutionDiagnostics, type HarnessExecutionDiagnosticsData } from "./HarnessExecutionDiagnostics";
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

/** Derive a status dot for a session based on its runs and unread state. */
function sessionStatusDot(
  item: HarnessHistorySession,
  runsBySession: Record<string, HarnessRun[]>,
  unreadSessions: Set<string>,
  isCurrent: boolean,
): string {
  if (isCurrent) return "";
  const runs = runsBySession[item.debug_session_id] || [];
  if (!runs.length) return unreadSessions.has(item.debug_session_id) ? " 🟢" : "";
  const hasWaiting = runs.some((r) => !isHarnessTerminal(r.status));
  if (hasWaiting) return " 🟡";
  if (unreadSessions.has(item.debug_session_id)) return " 🟢";
  return "";
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
  const v3 = import.meta.env.VITE_HAILIANG_SSE_V3 === 'true';
  const api = v3 ? '/api/v3' : '/api/v2';
  const v3Cursor = useRef({session: '', cursor: -1});
  const acceptV3 = (snapshot: V3Snapshot): HarnessSnapshot => {
    if (v3Cursor.current.session === snapshot.session_id && snapshot.cursor < v3Cursor.current.cursor) return {runtime_mode: snapshot.runtime_mode, cursor: snapshot.cursor, runs: []};
    v3Cursor.current = {session: snapshot.session_id, cursor: snapshot.cursor};
    return legacyView(snapshot);
  };
  const [goal, setGoal] = useState("");
  const [userRole, setUserRole] = useState("家长");
  const [schoolStage, setSchoolStage] = useState("高中");
  const [gradeName, setGradeName] = useState("");
  const gradeOptions = useMemo(() => GRADE_OPTIONS[schoolStage] || Object.values(GRADE_OPTIONS).flat(), [schoolStage]);
  const [province, setProvince] = useState("");
  const [subjectCombination, setSubjectCombination] = useState("");
  const subjectOptions = useMemo(() => {
    if (!province) return [];
    const scheme = PROVINCE_SCHEME[province] || "three_one_two";
    return SUBJECT_COMBINATIONS[scheme] || [];
  }, [province]);
  const [debugId, setDebugId] = useState("");
  // Per-session state: runs, evidence, conclusion, trace.
  const [runsBySession, setRunsBySession] = useState<Record<string, HarnessRun[]>>({});
  const [evidenceBySession, setEvidenceBySession] = useState<Record<string, boolean>>({});
  const [conclusionBySession, setConclusionBySession] = useState<Record<string, string>>({});
  const [unreadSessions, setUnreadSessions] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [trace, setTrace] = useState("");
  const [stepsByRun, setStepsByRun] = useState<Record<string, Record<string, StepIo>>>({});
  const [executionDiagnosticsByRun, setExecutionDiagnosticsByRun] = useState<Record<string, HarnessExecutionDiagnosticsData>>({});
  const [rightPane, setRightPane] = useState<"reply" | "trace">("reply");
  const [connection, setConnection] = useState("connected");
  const [knownSessionIds, setKnownSessionIds] = useState<string[]>([]);
  const selectedModelId = useChatStore((state) => state.selectedModelId);
  const setSelectedModelId = useChatStore((state) => state.setSelectedModelId);
  const userId = `workbench-harness:${actorId}`;
  const sessionId = `harness-${debugId}`;
  const runs = runsBySession[debugId] || [];
  const evidenceSaved = evidenceBySession[debugId] || false;
  const conclusion = conclusionBySession[debugId] || "";
  // "active" only considers the CURRENT session's runs — other sessions can run in parallel.
  const active = runs.find((r) => !isHarnessTerminal(r.status));
  const persistenceKey = `harness-debug:${baseUrl}:${actorId}:${revisionId}:${soulRevisionId || ""}`;
  const knownKey = `${persistenceKey}:known`;
  const unreadKey = `${persistenceKey}:unread`;

  const mergeRuns = (incoming: HarnessRun[]) => {
    // Never store runs under an empty session key — that would leak content
    // into the "new session" placeholder view.
    const sid = debugId;
    if (!sid) return;
    setRunsBySession((prev) => {
      const current = { ...(prev[sid] ? Object.fromEntries(prev[sid].map((run) => [run.run_id, run])) : {}) };
      for (const run of incoming) if (!current[run.run_id] || current[run.run_id].version <= run.version) current[run.run_id] = run;
      const updated = Object.values(current).sort((a, b) => a.created_at.localeCompare(b.created_at));

      // Track unread: if any run just completed and this session is not the current one,
      // mark it as unread so a green dot appears.
      const hadTerminalRuns = (prev[sid] || []).every((r) => isHarnessTerminal(r.status));
      const allTerminal = updated.every((r) => isHarnessTerminal(r.status));
      if (!hadTerminalRuns && allTerminal && updated.length > 0) {
        setUnreadSessions((prev) => {
          const next = new Set(prev);
          next.add(sid);
          try { sessionStorage.setItem(unreadKey, JSON.stringify([...next])); } catch { /* ignore */ }
          return next;
        });
      }

      return { ...prev, [sid]: updated };
    });
  };

  const request = async <T,>(path: string, body?: unknown): Promise<T> => {
    let response: Response;
    const startKey = `${persistenceKey}:submission`;
    if (v3 && body && path.endsWith('/harness/runs')) {
      const value = body as Record<string, unknown>;
      const fingerprint = JSON.stringify({...value, submission_id: undefined});
      let cached: {fingerprint: string; id: string} | null = null;
      try { cached = JSON.parse(sessionStorage.getItem(startKey) || 'null'); } catch { /* empty */ }
      value.submission_id = cached?.fingerprint === fingerprint ? cached.id : createUuid();
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
    const fromHistory = historySessions.filter((item) => item.debug_session_id);
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
    setRunsBySession({});
    setTrace("");
    setStepsByRun({});
setEvidenceBySession({});
    setConclusionBySession({});
    // Restore unread state
    let unread: string[] = [];
    try { unread = JSON.parse(sessionStorage.getItem(unreadKey) || "[]"); } catch { unread = []; }
    setUnreadSessions(new Set(Array.isArray(unread) ? unread : []));
    setExecutionDiagnosticsByRun({});
  }, [persistenceKey, knownKey, unreadKey]);

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
    setStepsByRun({});
    setExecutionDiagnosticsByRun({});
    setRightPane("reply");
    setGoal("");
    setDebugId(nextId);
    if (nextId) {
      sessionStorage.setItem(persistenceKey, nextId);
      rememberSession(nextId);
      // Mark as read — remove green dot
      setUnreadSessions((prev) => {
        if (!prev.has(nextId)) return prev;
        const next = new Set(prev);
        next.delete(nextId);
        try { sessionStorage.setItem(unreadKey, JSON.stringify([...next])); } catch { /* ignore */ }
        return next;
      });
    } else {
      sessionStorage.removeItem(persistenceKey);
    }
  };

  const runKey = runs.map((run) => `${run.run_id}:${run.version}`).join("|");
  useEffect(() => {
    if (rightPane !== "trace" || !debugId || !runs.length) return;
    let cancelled = false;
    void (async () => {
      try {
        const loaded = await Promise.all(runs.map(async (run) => {
          const data = await request<{ steps?: Record<string, StepIo>; execution_diagnostics?: HarnessExecutionDiagnosticsData }>(`${api}/sessions/${sessionId}/harness/runs/${run.run_id}/trace?user_id=${encodeURIComponent(userId)}`);
          return [run.run_id, data] as const;
        }));
        if (cancelled) return;
        setStepsByRun(Object.fromEntries(loaded.map(([id, data]) => [id, data.steps || {}])));
        setExecutionDiagnosticsByRun(Object.fromEntries(loaded.map(([id, data]) => [id, data.execution_diagnostics]).filter(([, value]) => Boolean(value))));
        const latest = loaded[loaded.length - 1]?.[1];
        setTrace(latest ? JSON.stringify(latest, null, 2) : "");
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rightPane, debugId, runKey]);

  const start = async () => {
    // Only block if CURRENT session has an active run (can't send two questions
    // in the same session at once). Other sessions can run in parallel.
    const currentActive = (runsBySession[debugId] || []).find((r) => !isHarnessTerminal(r.status));
    if (currentActive || busy || !goal.trim()) return;
    setBusy(true); setError(""); setTrace("");
    try {
      // Always create a new debug session when the current one has completed runs
      // or has an active run in another session — never silently reuse.
      const hasCurrentRuns = (runsBySession[debugId] || []).length > 0;
      const hasAnyActive = Object.values(runsBySession).some((sessionRuns) =>
        sessionRuns.some((r) => !isHarnessTerminal(r.status))
      );
      const id = (hasCurrentRuns || hasAnyActive)
        ? (await request<{ debug_session_id: string }>("/workbench/v1/revision-tests", { revision_id: revisionId, actor_id: actorId, soul_revision_id: soulRevisionId || null })).debug_session_id
        : ((!evidenceSaved && debugId) || (await request<{ debug_session_id: string }>("/workbench/v1/revision-tests", { revision_id: revisionId, actor_id: actorId, soul_revision_id: soulRevisionId || null })).debug_session_id);
      const presetFacts = objectType === "skill" ? {} : {
        ...(userRole ? { user_role: userRole } : {}),
        ...(schoolStage ? { school_stage: schoolStage } : {}),
        ...(province ? { student_province: province, gaokao_province: province } : {}),
        ...(subjectCombination ? { subject_group: subjectCombination, subject_choice: subjectCombination } : {}),
      };
      await request(`${api}/sessions/harness-${id}/harness/runs`, {
        ...(v3 ? { submission_id: createUuid() } : {}),
        goal,
        model: selectedModelId,
        context_data: { user_id: userId, facts: presetFacts, ...(gradeName && objectType !== "skill" ? { grade: gradeName } : {}) },
        debug_session_id: id,
      });
      if (id !== debugId) {
        setDebugId(id);
        sessionStorage.setItem(persistenceKey, id);
        rememberSession(id);
        setEvidenceBySession((prev) => ({ ...prev, [id]: false }));
        setConclusionBySession((prev) => ({ ...prev, [id]: "" }));
      }
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
      setEvidenceBySession((prev) => ({ ...prev, [debugId]: true }));
      onHistoryChange?.();
      onEvidenceSaved?.(debugId);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };

  // Can send a new question: revision selected, no active run in current session,
  // not busy, current session not saved as evidence, and goal is non-empty.
  const canSend = Boolean(revisionId) && !active && !busy && !evidenceSaved && Boolean(goal.trim());

  const historyTitle = sessionOptions.find((item) => item.debug_session_id === debugId);
  const historyLabel = historyTitle ? sessionLabel(historyTitle) : "新建问答会话";

  const setConclusion = (value: string) => {
    setConclusionBySession((prev) => ({ ...prev, [debugId]: value }));
  };

  const stopActiveRun = async () => {
    if (!active) return;
    try {
      await request(`/api/v2/sessions/${sessionId}/harness/runs/${active.run_id}/cancel`, { user_id: userId, profile_id: null });
    } catch (e) { setError(String(e)); }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        <h2 className="text-lg font-semibold">候选修订手动测试</h2>
        <div className="flex shrink-0 items-center gap-1.5">
          <select
            aria-label="历史会话"
            title={historyLabel}
            value={debugId}
            onChange={(event) => selectSession(event.target.value)}
            className="h-7 max-w-[14rem] rounded-md border border-white/10 bg-slate-950 px-2 text-xs text-slate-100 outline-none"
          >
            <option value="" className="bg-slate-950 text-slate-100">
              {sessionOptions.length ? "历史会话" : "暂无历史会话"}
            </option>
            {sessionOptions.map((item) => (
              <option key={item.debug_session_id} value={item.debug_session_id} className="bg-slate-950 text-slate-100">
                {sessionStatusDot(item, runsBySession, unreadSessions, item.debug_session_id === debugId)}{sessionLabel(item)}
              </option>
            ))}
          </select>
          <select
            aria-label="问答模型"
            title={testerModelName(selectedModelId)}
            value={selectedModelId}
            onChange={(event) => setSelectedModelId(event.target.value === "pro" ? "pro" : "flash")}
            className="h-7 rounded-md border border-white/10 bg-slate-950 px-2 text-xs text-slate-100 outline-none"
          >
            {TESTER_MODEL_OPTIONS.map((item) => (
              <option key={item.id} value={item.id} className="bg-slate-950 text-slate-100">{item.model}</option>
            ))}
          </select>
        </div>
        <button
          type="button"
          disabled={busy}
          onClick={() => selectSession("")}
          className="ml-auto h-7 shrink-0 rounded-md border border-sky-300/30 px-2.5 text-xs text-sky-100 disabled:opacity-40"
        >
          新建会话
        </button>
      </div>
      {objectType && objectType !== "skill" ? (
        <div className="grid shrink-0 grid-cols-5 gap-2 text-xs text-slate-400">
          {([
            ["身份", userRole, setUserRole, ["", "家长", "学生"]],
            ["学段", schoolStage, (v: string) => { setSchoolStage(v); setGradeName(""); }, ["", "小学", "初中", "高中"]],
            ["年级", gradeName, setGradeName, ["", ...gradeOptions]],
            ["省份", province, (v: string) => { setProvince(v); setSubjectCombination(""); }, ["", ...PROVINCES]],
            ["选科组合", subjectCombination, setSubjectCombination, ["", ...subjectOptions]],
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
            onClick={() => setRightPane("trace")}
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
                    <div key={`trace-${run.run_id}`} className="space-y-2">
                      <TaskProgressBlock run={run} connection={connection} steps={stepsByRun[run.run_id]} />
                      <HarnessExecutionDiagnostics diagnostics={executionDiagnosticsByRun[run.run_id]} />
                    </div>
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
            onClick={() => void stopActiveRun()}
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
      <p className="shrink-0 text-[11px] leading-5 text-slate-500">匿名会话：不读写孩子档案；任务树、分派与追问都在「轨迹过程」。展开步骤可查看该步的输入和输出。</p>
    </div>
  );
}