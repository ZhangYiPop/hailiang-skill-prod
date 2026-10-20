import { legacyView, subscribeV3, type V3Snapshot, type V3Event } from "@/utils/harnessV3";
import { createUuid } from "@/utils/uuid";
import { useEffect } from "react";
import { create } from "zustand";
import { useChatStore } from "@/store/useChatStore";
import type { ChatMessage } from "@/utils/api";
import type { HarnessRun, HarnessSnapshot } from "@/types/harness";
import { isHarnessTerminal } from "@/types/harness";

export const useHarnessState = create<{
  key: string; mode: string; activeRunId: string; connection: string; runs: Record<string, HarnessRun>; cursor: number;
  set: (patch: Partial<{ key: string; mode: string; activeRunId: string; connection: string; runs: Record<string, HarnessRun>; cursor: number }>) => void;
}>((set) => ({ key: "", mode: "", activeRunId: "", connection: "connected", runs: {}, cursor: 0, set }));

function identity() {
  const state = useChatStore.getState();
  return { user_id: state.userId, profile_id: state.selectedProfileId || state.activeProfileId || null };
}
function identityQuery() {
  const who = identity();
  return new URLSearchParams({ user_id: who.user_id, ...(who.profile_id ? { profile_id: who.profile_id } : {}) });
}
function key() {
  const state = useChatStore.getState();
  return `${state.apiBaseUrl}|${state.sessionId}|${state.userId}|${identity().profile_id ?? ""}`;
}
const v3 = import.meta.env.VITE_HAILIANG_SSE_V3 === 'true';
export const harnessV3Enabled = v3;
let v3Snapshot: V3Snapshot | undefined;
export async function harnessRequest<T>(path: string, body?: unknown): Promise<T> {
  const submissionKey = `harness-start:${key()}`;
  if (v3 && body && path.endsWith('/harness/runs')) {
    const value = body as Record<string, unknown>;
    const fingerprint = JSON.stringify({...value, submission_id: undefined});
    let cached: {fingerprint: string; id: string} | null = null;
    try { cached = JSON.parse(sessionStorage.getItem(submissionKey) || 'null'); } catch { /* empty */ }
    value.submission_id = cached?.fingerprint === fingerprint ? cached.id : createUuid();
    sessionStorage.setItem(submissionKey, JSON.stringify({fingerprint, id: value.submission_id}));
  }
  const response = await fetch(`${useChatStore.getState().apiBaseUrl}/api/${v3 ? "v3" : "v2"}${path}`, {
    method: body === undefined ? "GET" : "POST", headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || data.code || (typeof data.detail === "object" ? data.detail?.message || data.detail?.code : data.detail) || "请求失败");
  if (v3 && body && path.endsWith('/harness/runs')) sessionStorage.removeItem(submissionKey);
  return data as T;
}
function runMessages(run: HarnessRun): ChatMessage[] {
  const result = run.result || (run.status === "failed" ? "本次任务未能完成，请重新发起任务。" : "");
  return [
    { id: `harness-user-${run.run_id}`, role: "user", content: run.goal, createdAt: run.created_at, blocks: [], messageType: "harness" },
    { id: run.assistant_message_id || `harness-work-${run.run_id}`, role: "assistant", content: "", createdAt: run.created_at,
      agentLabel: "主 agent", blocks: [{ type: "task_progress", payload: { run } }, ...(result ? [{ type: "markdown" as const, payload: { content: result } }] : [])],
      streamingStatus: run.message_status === "streaming" ? "streaming" : run.status === "failed" ? "failed" : "completed", messageType: "harness" },
  ];
}
export async function refreshHarness(): Promise<HarnessSnapshot> {
  const state = useChatStore.getState();
  const currentKey = key();
  const raw = await harnessRequest<HarnessSnapshot | V3Snapshot>(`/sessions/${encodeURIComponent(state.sessionId)}/harness?${identityQuery()}`);
  const snapshot = v3 ? legacyView(raw as V3Snapshot) : raw as HarnessSnapshot;
  if (key() !== currentKey) return snapshot;
  if (v3) {
    if (v3Snapshot?.session_id === state.sessionId && v3Snapshot.profile_id === identity().profile_id && v3Snapshot.cursor > snapshot.cursor) return legacyView(v3Snapshot);
    v3Snapshot = raw as V3Snapshot;
  }
  return applySnapshot(snapshot, currentKey);
}
function applySnapshot(snapshot: HarnessSnapshot, currentKey: string): HarnessSnapshot {
  const previous = useHarnessState.getState();
  if (v3 && previous.key === currentKey && snapshot.cursor < previous.cursor) return snapshot;
  const runs = previous.key === currentKey ? { ...previous.runs } : {};
  for (const run of snapshot.runs) {
    if (!runs[run.run_id] || runs[run.run_id].version <= run.version) runs[run.run_id] = run;
  }
  snapshot.runs = Object.values(runs);
  snapshot.cursor = Math.max(previous.key === currentKey ? previous.cursor : 0, snapshot.cursor);
  const active = snapshot.runs.find((run) => !isHarnessTerminal(run.status));
  useHarnessState.getState().set({ key: currentKey, mode: snapshot.runtime_mode, activeRunId: active?.run_id ?? "", runs, cursor: snapshot.cursor });
  if (snapshot.runtime_mode === "harness_v1") {
    const current = useChatStore.getState();
    const otherMessages = current.messages.filter((m) => m.messageType !== "harness");
    current.setMessages([...otherMessages, ...[...snapshot.runs].sort((a, b) => a.created_at.localeCompare(b.created_at)).flatMap(runMessages)]);
    current.setSending(Boolean(active));
  }
  return snapshot;
}
export async function sendHarnessIfEnabled(content: string): Promise<boolean> {
  const state = useChatStore.getState();
  let snapshot: HarnessSnapshot;
  try { snapshot = await refreshHarness(); }
  catch (error) {
    // An older backend has no harness endpoint. Do not swallow other failures.
    if (String(error).includes("Not Found")) return false;
    throw error;
  }
  if (snapshot.runtime_mode !== "harness_v1") return false;
  if (!v3 && snapshot.runs.some((run) => !isHarnessTerminal(run.status))) throw new Error("任务进行中，请回答卡片或停止任务。");
  const who = identity();
  state.setSending(true);
  try {
    const response = await harnessRequest<{ run_id: string }>(`/sessions/${encodeURIComponent(state.sessionId)}/harness/runs`, {
      ...(v3 ? { submission_id: createUuid() } : {}),
      goal: content, context_data: { ...who, ...(who.profile_id ? {
        student_name: state.debugIdentity.display_name, grade: state.debugIdentity.grade || undefined,
        school_year: state.debugIdentity.school_year || undefined,
      } : {}) },
      expert_team_id: state.pendingExpertTeamId || null, expert_id: state.pendingExpertId || null,
      clear_experts: state.pendingExpertClear,
      debug_session_id: new URLSearchParams(window.location.search).get("workbench_debug_session_id") || undefined,
    });
    useHarnessState.getState().set({ activeRunId: response.run_id });
    if (state.pendingExpertTeamId) {
      const team = state.expertTeamCatalog.find((item) => item.team_id === state.pendingExpertTeamId);
      if (team) state.setActiveExpertTeam({ ...team, coordinator_mention_name: team.coordinator_name,
        active_expert_id: state.pendingExpertId || team.coordinator_expert_id,
        active_mention_name: team.members.find((member) => member.expert_id === (state.pendingExpertId || team.coordinator_expert_id))?.mention_name || team.coordinator_name });
    }
    if (state.pendingExpertId) state.setActiveExpertId(state.pendingExpertId);
    if (state.pendingExpertClear) { state.setActiveExpertTeam(null); state.setActiveExpertId(""); }
    state.setPendingExpertId(""); state.setPendingExpertTeamId(""); state.setPendingExpertClear(false);
    await refreshHarness();
  } catch (error) { state.setSending(false); throw error; }
  return true;
}
export async function submitHarnessAnswer(sessionId: string, requestId: string, body: unknown): Promise<void> {
  const state = useChatStore.getState();
  const response = await fetch(`${state.apiBaseUrl}/api/v3/sessions/${encodeURIComponent(sessionId)}/input-requests/${encodeURIComponent(requestId)}/answers`, {
    method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}, body: JSON.stringify(body),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.message || data.code || data.detail || '提交失败');
  }
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
    await refreshHarness(); return;
  }
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let pending = '';
  while (true) {
    const chunk = await reader.read(); pending += decoder.decode(chunk.value || new Uint8Array(), {stream: !chunk.done});
    let boundary: number;
    while ((boundary = pending.indexOf('\n\n')) >= 0) {
      const frame = pending.slice(0, boundary); pending = pending.slice(boundary + 2);
      const data = frame.split('\n').find(line => line.startsWith('data: '))?.slice(6);
      if (!data) continue;
      const event = JSON.parse(data) as V3Event;
      if (event.snapshot && event.session_id === state.sessionId) {
        v3Snapshot = event.snapshot;
        applySnapshot(legacyView(event.snapshot), key());
      }
    }
    if (chunk.done) break;
  }
}

export async function cancelHarness(): Promise<boolean> {
  const runId = useHarnessState.getState().activeRunId;
  if (!runId) return false;
  const session = useChatStore.getState().sessionId;
  await harnessRequest(`/sessions/${encodeURIComponent(session)}/harness/runs/${encodeURIComponent(runId)}/cancel`, identity());
  await refreshHarness();
  return true;
}
export function useHarnessConversation() {
  const apiBaseUrl = useChatStore((s) => s.apiBaseUrl);
  const sessionId = useChatStore((s) => s.sessionId);
  const userId = useChatStore((s) => s.userId);
  const profileId = useChatStore((s) => s.selectedProfileId || s.activeProfileId);
  useEffect(() => {
    let stopped = false;
    let source: EventSource | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    useHarnessState.getState().set({ key: key(), mode: "", activeRunId: "", connection: "connected", runs: {}, cursor: 0 });
    if (!sessionId || !userId) return;
    if (v3) {
      const currentKey = key();
      return subscribeV3(
        () => harnessRequest<V3Snapshot>(`/sessions/${encodeURIComponent(sessionId)}/harness?${identityQuery()}`),
        `${apiBaseUrl}/api/v3/sessions/${encodeURIComponent(sessionId)}/harness/events?${identityQuery()}`,
        snapshot => { if (key() === currentKey) { v3Snapshot = snapshot; applySnapshot(legacyView(snapshot), currentKey); } },
        connection => { if (key() === currentKey) useHarnessState.getState().set({connection}); });
    }
    void refreshHarness().then((snapshot) => {
      if (stopped || snapshot.runtime_mode !== "harness_v1") return;
      const query = identityQuery(); query.set("after", String(snapshot.cursor));
      source = new EventSource(`${apiBaseUrl}/api/${v3 ? "v3" : "v2"}/sessions/${encodeURIComponent(sessionId)}/harness/events?${query}`);
      const update = () => {
        if (timer || stopped) return;
        timer = setTimeout(() => { timer = undefined; void refreshHarness().catch(() => useHarnessState.getState().set({ connection: "reconnecting" })); }, 80);
      };
      source.addEventListener("harness", update);
      source.onopen = () => { if (stopped) return; useHarnessState.getState().set({ connection: "connected" }); if (!v3) update(); };
      source.onerror = () => { if (!stopped) useHarnessState.getState().set({ connection: "reconnecting" }); };
    }).catch(() => { /* Legacy/older server keeps its existing conversation flow. */ });
    return () => { stopped = true; source?.close(); if (timer) clearTimeout(timer); };
  }, [apiBaseUrl, sessionId, userId, profileId]);
}
