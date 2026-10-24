import { create } from "zustand";

import type { ChatMessage } from "@/utils/api";
import { useChatStore } from "@/store/useChatStore";
import { postSseStream } from "@/utils/sse";
import type { HarnessV3Event, HarnessV3Frame, HarnessV3InputRequest } from "@/types/harnessV3";

type V3ConversationState = {
  activeAssistantMessageId: string;
  connection: "idle" | "streaming" | "waiting_for_user" | "timeout" | "completed" | "failed";
  versions: Record<string, number>;
  set: (patch: Partial<Omit<V3ConversationState, "set">>) => void;
};

export const useHarnessV3State = create<V3ConversationState>((set) => ({
  activeAssistantMessageId: "",
  connection: "idle",
  versions: {},
  set,
}));

function makeId(prefix: string) {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return `${prefix}-${crypto.randomUUID()}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function workbenchSessionId(sessionId: string) {
  const debugId = new URLSearchParams(window.location.search).get("workbench_debug_session_id")?.trim();
  return debugId ? `workbench_${debugId}` : sessionId;
}

function contextData() {
  const store = useChatStore.getState();
  const identity = store.debugIdentity;
  if (!identity) throw new Error("缺少本地调试身份");
  const profileId = store.selectedProfileId || store.activeProfileId || undefined;
  const profileData = profileId
    ? {
        name: identity.display_name,
        ...(identity.grade ? { grade: identity.grade } : {}),
        ...(identity.school_year ? { school_year: identity.school_year } : {}),
      }
    : undefined;
  return {
    user_id: identity.user_id,
    ...(profileId ? { profile_id: profileId } : {}),
    ...(profileData ? { profile_data: profileData } : {}),
  };
}

function selectedTeamId() {
  const store = useChatStore.getState();
  const explicit = store.pendingExpertTeamId.trim();
  if (explicit) return explicit;
  const active = store.activeExpertTeam?.team_id.trim();
  if (active) return active;
  if (store.expertTeamCatalog.length === 1) return store.expertTeamCatalog[0].team_id;
  if (store.expertTeamCatalog.length === 0) throw new Error("当前没有可用的已发布专家团，无法发起 V3 对话。");
  throw new Error("请先选择一个专家团。");
}

function upsertFrame(frame: HarnessV3Frame, event: HarnessV3Event["event"]) {
  const state = useHarnessV3State.getState();
  const knownVersion = state.versions[frame.assistant_message_id] ?? -1;
  if (frame.record_version < knownVersion) return;

  const store = useChatStore.getState();
  if (frame.superseded_assistant_message_id) {
    store.updateMessage(frame.superseded_assistant_message_id, (message) => ({
      ...message,
      generationStatus: "superseded",
      streamingStatus: "completed",
    }));
  }

  const terminal = frame.persistence.terminal;
  const failed = frame.state.status === "failed" || frame.outcome === "failed";
  const next: Partial<ChatMessage> = {
    id: frame.assistant_message_id,
    messageId: frame.assistant_message_id,
    role: "assistant",
    content: frame.state.result || "",
    createdAt: frame.state.created_at || new Date().toISOString(),
    profileId: frame.profile_id ?? undefined,
    profileName: frame.profile_name ?? undefined,
    agentLabel: "主编排器",
    messageType: "harness_v3",
    blocks: [{ type: "task_progress_v3", payload: { frame } }],
    generationStatus: frame.state.status === "cancelled" ? "cancelled" : terminal ? frame.state.status : "running",
    streamingStatus: failed ? "failed" : terminal ? "completed" : "streaming",
    errorMessage: frame.state.error || undefined,
  };
  const exists = store.messages.some((message) => message.id === frame.assistant_message_id);
  if (exists) store.updateMessage(frame.assistant_message_id, (message) => ({ ...message, ...next }));
  else store.appendMessage(next as ChatMessage);

  const connection = terminal
    ? (failed ? "failed" : "completed")
    : event === "done_suspended"
      ? "waiting_for_user"
      : event === "done_timeout"
        ? "timeout"
        : "streaming";
  useHarnessV3State.getState().set({
    activeAssistantMessageId: terminal ? "" : frame.assistant_message_id,
    connection,
    versions: { ...useHarnessV3State.getState().versions, [frame.assistant_message_id]: frame.record_version },
  });
  store.setActiveProfileId(frame.profile_id ?? "");
  store.setSelectedProfileId(frame.profile_id ?? "");
  store.setActiveProfileName(frame.profile_name ?? "");
  if (frame.expert_team_id) {
    const team = store.expertTeamCatalog.find((item) => item.team_id === frame.expert_team_id);
    if (team) {
      store.setActiveExpertTeam({
        team_id: team.team_id,
        name: team.name,
        brief: team.brief,
        coordinator_expert_id: team.coordinator_expert_id,
        coordinator_mention_name: team.members.find((member) => member.is_coordinator)?.mention_name ?? team.coordinator_name,
        active_expert_id: team.coordinator_expert_id,
        active_mention_name: team.members.find((member) => member.is_coordinator)?.mention_name ?? team.coordinator_name,
        members: team.members,
      });
    }
  }
}

async function stream(input: Record<string, unknown>, options: { appendUser?: string } = {}) {
  const store = useChatStore.getState();
  if (!store.sessionId) throw new Error("请先创建会话");
  const controller = new AbortController();
  const previous = store.streamAbortController;
  if (previous) previous.abort();
  if (options.appendUser) {
    store.appendMessage({
      id: makeId("user"), role: "user", content: options.appendUser, createdAt: new Date().toISOString(), blocks: [],
      profileId: store.selectedProfileId || store.activeProfileId || undefined,
      profileName: store.activeProfileName || undefined,
    });
    store.setComposerValue("");
  }
  store.setSending(true);
  store.setErrorMessage("");
  store.setStreamAbortController(controller);
  store.setCurrentRunId(makeId("run"));
  useHarnessV3State.getState().set({ connection: "streaming" });
  const requestRunId = useChatStore.getState().currentRunId;
  try {
    await postSseStream({
      url: `${store.apiBaseUrl}/api/v3/sessions/chat/stream`,
      body: {
        session_id: workbenchSessionId(store.sessionId),
        run_id: requestRunId,
        input: JSON.stringify(input),
        context_data: contextData(),
      },
      signal: controller.signal,
      protocol: "hailiang.sse.v3",
      onEvent: (event) => {
        if (["state", "done_suspended", "done_timeout", "done"].includes(event.event)
          && (event.data as HarnessV3Frame).protocol === "hailiang.sse.v3") {
          upsertFrame(event.data as HarnessV3Frame, event.event as HarnessV3Event["event"]);
        }
      },
    });
  } catch (error) {
    if (!controller.signal.aborted) {
      store.setErrorMessage(error instanceof Error ? error.message : "V3 对话请求失败");
      useHarnessV3State.getState().set({ connection: "failed" });
    }
    throw error;
  } finally {
    if (useChatStore.getState().streamAbortController === controller) {
      store.setStreamAbortController(null);
      store.setSending(false);
      store.setCurrentRunId("");
    }
  }
}

export async function sendHarnessV3Chat(content: string) {
  const teamId = selectedTeamId();
  await stream({ action: "chat", content, expert_team_id: [teamId] }, { appendUser: content });
}

export async function submitHarnessV3Answer(frame: HarnessV3Frame, request: HarnessV3InputRequest, answers: Record<string, unknown>, skipped: boolean) {
  const teamId = frame.expert_team_id || selectedTeamId();
  await stream({
    action: "answer_input_request",
    assistant_message_id: frame.assistant_message_id,
    request_id: request.id,
    expert_team_id: [teamId],
    submission_id: makeId("harness-answer"),
    expected_version: request.request_version,
    answers,
    skipped,
  });
}

export async function resumeHarnessV3(frame: HarnessV3Frame) {
  await stream({ action: "resume", assistant_message_id: frame.assistant_message_id, expert_team_id: [frame.expert_team_id || selectedTeamId()] });
}

export async function stopHarnessV3() {
  await stream({ action: "stop", source: "composer" });
}
