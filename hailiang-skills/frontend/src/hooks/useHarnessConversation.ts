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
export async function harnessRequest<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${useChatStore.getState().apiBaseUrl}/api/v2${path}`, {
    method: body === undefined ? "GET" : "POST", headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || data.code || String(data.detail || "请求失败"));
  return data as T;
}
function runMessages(run: HarnessRun): ChatMessage[] {
  const result = run.result || (run.status === "failed" ? "本次任务未能完成，请重新发起任务。" : "");
  return [
    { id: `harness-user-${run.run_id}`, role: "user", content: run.goal, createdAt: run.created_at, blocks: [], messageType: "harness" },
    // Progress and final conclusion belong to the same assistant response.
    { id: run.assistant_message_id || `harness-work-${run.run_id}`, role: "assistant", content: "", createdAt: run.created_at, agentLabel: "主 agent",
      blocks: [{ type: "task_progress", payload: { run } }, ...(result ? [{ type: "markdown" as const, payload: { content: result } }] : [])],
      streamingStatus: run.status === "failed" ? "failed" : "completed", messageType: "harness" },
  ];
}
export async function refreshHarness(): Promise<HarnessSnapshot> {
  const state = useChatStore.getState();
  const currentKey = key();
  const snapshot = await harnessRequest<HarnessSnapshot>(`/sessions/${encodeURIComponent(state.sessionId)}/harness?${identityQuery()}`);
  if (key() !== currentKey) return snapshot;
  const previous = useHarnessState.getState();
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
  if (snapshot.runs.some((run) => !isHarnessTerminal(run.status))) throw new Error("任务进行中，请回答卡片或停止任务。");
  const who = identity();
  state.setSending(true);
  try {
    const response = await harnessRequest<{ run_id: string }>(`/sessions/${encodeURIComponent(state.sessionId)}/harness/runs`, {
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
    void refreshHarness().then((snapshot) => {
      if (stopped || snapshot.runtime_mode !== "harness_v1") return;
      const query = identityQuery(); query.set("after", String(snapshot.cursor));
      source = new EventSource(`${apiBaseUrl}/api/v2/sessions/${encodeURIComponent(sessionId)}/harness/events?${query}`);
      const update = () => {
        if (timer || stopped) return;
        timer = setTimeout(() => { timer = undefined; void refreshHarness().catch(() => useHarnessState.getState().set({ connection: "reconnecting" })); }, 80);
      };
      source.addEventListener("harness", update);
      source.onopen = () => { if (stopped) return; useHarnessState.getState().set({ connection: "connected" }); update(); };
      source.onerror = () => { if (!stopped) useHarnessState.getState().set({ connection: "reconnecting" }); };
    }).catch(() => { /* Legacy/older server keeps its existing conversation flow. */ });
    return () => { stopped = true; source?.close(); if (timer) clearTimeout(timer); };
  }, [apiBaseUrl, sessionId, userId, profileId]);
}
