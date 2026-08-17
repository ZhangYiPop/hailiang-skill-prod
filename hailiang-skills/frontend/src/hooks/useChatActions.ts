import { useCallback, useRef } from "react";

import { useChatStore } from "@/store/useChatStore";
import {
  clearUserFactsBySource,
  createProfile,
  deleteSession,
  downloadSessionLogs,
  getEvents,
  getProfileFacts,
  getSession,
  getSessionContext,
  getUserFacts,
  listProfileSessions,
  listProfiles,
  listRuntimeSkills,
  normalizeMessageBlocks,
  updateSessionTitle,
  updateMessageFeedback,
  updateMessageInteraction,
  upsertProfileFacts,
  upsertSessionFacts,
  upsertUserFacts,
  type ChatRetryRequest,
  type ChatMessage,
  type DebugIdentity,
  type FactMap,
  type FactSourcePayload,
  type FactWriteResponse,
  type MessageResponse,
  type MessagePresentation,
  type RouteSuggestion,
  type SkillCatalogItem,
  type SkillTransition,
  type SessionContextMessage,
} from "@/utils/api";
import { postSseStream } from "@/utils/sse";
import { buildClientOpeningMessage } from "@/utils/opening";
import { isStatusTimelineBlock, type FactFormField, type MessageBlock } from "@/types/messageBlocks";
import type {
  FinalMessageData,
  SkillActionData,
  SkillContextData,
  SkillIntroData,
  StreamEvent,
  SseV2State,
} from "@/types/streamEvents";

const supersededStreamControllers = new WeakSet<AbortController>();

function makeMessageId(role: "user" | "assistant" | "session" | "run"): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${role}-${crypto.randomUUID()}`;
  }
  return `${role}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function makeMessage(
  role: "user" | "assistant",
  content: string = "",
  blocks: MessageBlock[] = [],
  metadata: Partial<ChatMessage> = {},
): ChatMessage {
  return {
    id: makeMessageId(role),
    role,
    content,
    createdAt: new Date().toISOString(),
    blocks,
    streamingStatus: role === "assistant" ? "completed" : "idle",
    ...metadata,
  };
}

function presentationFromState(state: SseV2State): MessagePresentation {
  return {
    assistant: state.assistant,
    intent: state.intent,
    form: state.form,
    path_options: state.path_options,
    skill_rooms: state.skill_rooms,
    skill_transition: state.skill_transition,
    session: state.session,
    risk: state.risk,
    error: state.error,
  };
}

function isTerminalState(status: SseV2State["status"]): boolean {
  return status !== "streaming";
}

function normalizeContextMessages(messages: SessionContextMessage[]): ChatMessage[] {
  return messages
  .filter((message) => !message.metadata?.hidden)
  .map((message, index) => {
    const metadata = message.metadata ?? {};
    const presentation = message.presentation ?? (metadata as { presentation?: MessagePresentation }).presentation;
    return {
      id: `${message.role}-${index}`,
      messageId: message.message_id,
      role: message.role,
      content: presentation?.assistant.content ?? message.content,
      createdAt: message.created_at ?? new Date().toISOString(),
      blocks: presentation ? [] : normalizeMessageBlocks(message.blocks ?? metadata.blocks ?? []),
      presentation,
      skillId: message.skill_id ?? metadata.skill_id,
      skillName: message.skill_name ?? metadata.skill_name,
      agentLabel: message.agent_label ?? metadata.agent_label,
      skillBrief: message.skill_brief ?? metadata.skill_brief ?? metadata.brief,
      skillInfo: message.skill_info ?? metadata.skill_info ?? metadata.info,
      sceneName: message.scene_name ?? metadata.scene_name,
      themeKey: message.theme_key ?? metadata.theme_key ?? metadata.skill_theme,
      conclusionSummary: message.conclusion_summary ?? metadata.conclusion_summary,
      contextCompression: message.context_compression ?? metadata.context_compression,
      routeSuggestions: message.route_suggestions ?? metadata.route_suggestions ?? [],
      selectedRouteSuggestion: message.selected_route_suggestion ?? metadata.selected_route_suggestion,
      interactionStates: message.interaction_states ?? metadata.interaction_states ?? {},
      feedback: message.feedback ?? metadata.feedback,
      feedbackUpdatedAt: message.feedback_updated_at ?? metadata.feedback_updated_at,
      messageType: message.message_type ?? metadata.message_type,
      skillIntro: message.skill_intro ?? metadata.skill_intro,
      skillTransition: message.skill_transition ?? metadata.skill_transition,
      generationStatus: (message.generation_status ?? metadata.generation_status) as string | undefined,
      streamingStatus: "completed",
    };
  });
}

function inferActiveSkill(
  skillStates: Record<string, Record<string, unknown>>,
  fallback?: string | null,
): string {
  const canonicalize = (value: unknown) => {
    const normalized = String(value ?? "").trim();
    return normalized === "main_planner" ? "career_plan_entity" : normalized;
  };
  const fallbackSkill = canonicalize(fallback);
  if (fallbackSkill) {
    return fallbackSkill;
  }
  const runtimeActive = canonicalize(skillStates.skill_runtime?.active_skill_id);
  if (runtimeActive) {
    return runtimeActive;
  }
  const preferredKeys = ["career_plan_entity", "general_chat", "planner", "router"];
  for (const key of preferredKeys) {
    if (skillStates[key] && Object.keys(skillStates[key]).length > 0) {
      const value = canonicalize(skillStates[key].target_skill ?? key);
      if (value) {
        return value;
      }
    }
  }
  const reservedKeys = new Set(["skill_runtime", "router", "facts_extractor", "planner", "admission", "school_intro", "main_planner"]);
  const matchedSkill = Object.entries(skillStates).find(
    ([key, value]) => !reservedKeys.has(key) && value && Object.keys(value).length > 0,
  );
  return canonicalize(matchedSkill?.[0] ?? "");
}


function inferCurrentScenario(
  interactionState?: Record<string, unknown>,
  plannerState?: Record<string, unknown>,
  routerState?: Record<string, unknown>,
  mainPlannerState?: Record<string, unknown>,
): string {
  const intentRoute = (mainPlannerState?.intent_route ?? {}) as Record<string, unknown>;
  const scenario =
    (intentRoute.scene_name as string | undefined) ??
    (mainPlannerState?.scene_name as string | undefined) ??
    (plannerState?.scene_name as string | undefined) ??
    (plannerState?.scenario_id as string | undefined) ??
    (routerState?.scene_name as string | undefined) ??
    (routerState?.scenario as string | undefined) ??
    (interactionState?.current_scenario as string | undefined) ??
    "";
  return scenario.trim();
}


function pickFactsPayload(currentFacts: FactWriteResponse["current_facts"]): {
  userFacts?: FactMap;
  sharedFacts?: FactMap;
  profileFacts?: FactMap;
  sessionFacts?: FactMap;
  effectiveFacts?: FactMap;
} {
  if (!currentFacts || Array.isArray(currentFacts)) {
    return {};
  }
  if ("user_facts" in currentFacts || "session_facts" in currentFacts || "effective_facts" in currentFacts) {
    const payload = currentFacts as {
      user_facts?: FactMap;
      shared_facts?: FactMap;
      profile_facts?: FactMap;
      session_facts?: FactMap;
      effective_facts?: FactMap;
    };
    return {
      userFacts: payload.user_facts,
      sharedFacts: payload.shared_facts ?? payload.user_facts,
      profileFacts: payload.profile_facts,
      sessionFacts: payload.session_facts,
      effectiveFacts: payload.effective_facts,
    };
  }
  return {
    userFacts: currentFacts as FactMap,
    sharedFacts: currentFacts as FactMap,
  };
}

function buildMessageResponseFromStream(data: FinalMessageData): MessageResponse {
  return {
    message_id: data.message_id,
    assistant_message: data.assistant_message,
    skill_intro: data.skill_intro ?? null,
    reasoning: data.reasoning,
    message_blocks: normalizeMessageBlocks(data.message_blocks),
    active_skill: (data.active_skill as string | null | undefined) ?? null,
    active_skill_label: data.active_skill_label ?? null,
    agent_label: data.agent_label ?? null,
    skill_brief: data.skill_brief ?? null,
    skill_info: data.skill_info ?? null,
    scene_name: data.scene_name ?? null,
    skill_theme: data.skill_theme ?? null,
    conclusion_summary: data.conclusion_summary ?? null,
    context_compression: data.context_compression ?? null,
    route_suggestions: data.route_suggestions ?? [],
    candidate_paths_brief: (data.candidate_paths_brief ?? []) as MessageResponse["candidate_paths_brief"],
    suggested_paths: (data.suggested_paths ?? []) as string[],
    facts_updated: [],
    risk_alerts: [],
    user_facts: (data.user_facts ?? data.shared_facts ?? {}) as FactMap,
    shared_facts: (data.shared_facts ?? data.user_facts ?? {}) as FactMap,
    profile_facts: (data.profile_facts ?? {}) as FactMap,
    session_facts: (data.session_facts ?? {}) as FactMap,
    effective_facts: (data.effective_facts ?? {}) as FactMap,
    profile_id: (data.profile_id as string | null | undefined) ?? null,
    profile_name: (data.profile_name as string | null | undefined) ?? null,
    router_state: (data.router_state ?? {}) as Record<string, unknown>,
    facts_extractor_state: (data.facts_extractor_state ?? {}) as Record<string, unknown>,
    planner_state: (data.planner_state ?? {}) as Record<string, unknown>,
    career_plan_state: ((data.career_plan_state ?? data.main_planner_state) ?? {}) as Record<string, unknown>,
    main_planner_state: ((data.career_plan_state ?? data.main_planner_state) ?? {}) as Record<string, unknown>,
    admission_state: {},
    school_intro_state: {},
    ranking_snapshot: {},
    conversation_state: data.conversation_state,
  };
}

function mergeFinalMessageBlocks(currentBlocks: MessageBlock[], incomingBlocks: MessageBlock[]): MessageBlock[] {
  const currentStatusBlocks = currentBlocks.filter((block) => isStatusTimelineBlock(block));
  const incomingStatusBlocks = incomingBlocks.filter((block) => isStatusTimelineBlock(block));
  const incomingOtherBlocks = incomingBlocks.filter((block) => !isStatusTimelineBlock(block));
  const statusBlocks = incomingStatusBlocks.length ? incomingStatusBlocks : currentStatusBlocks;
  return [...statusBlocks, ...incomingOtherBlocks];
}

function formatFactDraftValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.join("、");
  }
  if (value == null) {
    return "";
  }
  return String(value);
}

function buildFactSubmissionMessage(
  fields: FactFormField[],
  draftValues: Record<string, unknown>,
): string {
  return fields
    .map((field) => {
      const formattedValue = formatFactDraftValue(draftValues[field.fact_key]);
      if (!formattedValue) {
        return "";
      }
      return `${field.label}：${formattedValue}`;
    })
    .filter(Boolean)
    .join("；");
}

function textFactValue(facts: FactMap, key: string): string {
  const value = facts[key]?.value;
  return typeof value === "string" ? value : "";
}

function currentSchoolYear(facts: FactMap): string {
  const value = facts.profile_school_facts?.value;
  if (!Array.isArray(value) || value.length === 0) {
    return "";
  }
  const latest = value[value.length - 1];
  return typeof latest === "object" && latest && typeof (latest as Record<string, unknown>).school_year === "string"
    ? String((latest as Record<string, unknown>).school_year)
    : "";
}

type ApplyDebugIdentityOptions = {
  refreshProfileWorkspace?: boolean;
};

function buildContextData(identity: DebugIdentity): Record<string, string> {
  return {
    student_name: identity.display_name,
    user_id: identity.user_id,
    profile_id: identity.profile_id,
    ...(identity.school_year ? { school_year: identity.school_year } : {}),
    ...(identity.grade ? { grade: identity.grade } : {}),
  };
}

export function useChatActions() {
  const store = useChatStore();
  // State frame #1 carries the external run id. Bind a queued stop request to
  // its stream controller so it cannot affect a later run.
  const pendingStopStreamRef = useRef<AbortController | null>(null);
  const cancellingRunRef = useRef("");
  const identityApplySequenceRef = useRef(0);

  const cancelStreamRun = useCallback(async (stream: AbortController, runId: string) => {
    const state = useChatStore.getState();
    if (
      !runId ||
      state.streamAbortController !== stream ||
      state.currentRunId !== runId ||
      cancellingRunRef.current === runId
    ) {
      return;
    }
    cancellingRunRef.current = runId;
    state.setCancellingRun(true);
    const identity = state.debugIdentity;
    if (!identity) {
      throw new Error("缺少本地调试身份");
    }
    try {
      await postSseStream({
        url: `${state.apiBaseUrl}/api/v1/sessions/chat/stream`,
        body: {
          session_id: state.sessionId,
          run_id: runId,
          input: JSON.stringify({ action: "stop", source: "composer" }),
          context_data: buildContextData(identity),
        },
        onEvent: (event) => {
          if (event.event !== "state") {
            return;
          }
          const response = event.data as SseV2State;
          const target = [...useChatStore.getState().messages]
            .reverse()
            .find((message) => message.role === "assistant" && message.streamingStatus === "streaming");
          if (target) {
            state.updateMessage(target.id, (message) => ({
              ...message,
              content: response.assistant.content,
              presentation: presentationFromState(response),
              generationStatus: "cancelled",
              streamingStatus: "completed",
            }));
          }
        },
      });
    } finally {
      stream.abort();
      state.setStreamAbortController(null);
      state.setSending(false);
      state.setCancellingRun(false);
    }
  }, []);

  const abortCurrentStream = useCallback(() => {
    const currentAbortController = useChatStore.getState().streamAbortController;
    currentAbortController?.abort();
    if (pendingStopStreamRef.current === currentAbortController) {
      pendingStopStreamRef.current = null;
    }
    useChatStore.getState().setStreamAbortController(null);
  }, []);

  const refreshEventPanels = useCallback(async () => {
    const state = useChatStore.getState();
    if (!state.sessionId) {
      return;
    }
    const [sessionResult, eventsResult] = await Promise.all([
      getSession(state.apiBaseUrl, state.sessionId),
      getEvents(state.apiBaseUrl, state.sessionId),
    ]);
    store.setSessionTitle(sessionResult.title ?? "");
    store.setActiveProfileName(sessionResult.profile_name ?? state.activeProfileName);
    store.setCandidatePaths(sessionResult.candidate_paths);
    store.setSkillStates(sessionResult.skill_states);
    store.setEvents(eventsResult.events);
  }, [store]);

  const loadSkillCatalog = useCallback(async () => {
    const state = useChatStore.getState();
    try {
      const skills = await listRuntimeSkills(state.apiBaseUrl, state.debugIdentity?.grade ?? "");
      store.setSkillCatalog(skills);
      return skills;
    } catch {
      store.setSkillCatalog([]);
      return [];
    }
  }, [store]);

  const handleDownloadSessionLogs = useCallback(async () => {
    const state = useChatStore.getState();
    if (!state.sessionId) {
      store.setErrorMessage("当前没有可下载的会话日志");
      return;
    }
    try {
      const { blob, filename } = await downloadSessionLogs(state.apiBaseUrl, state.sessionId);
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      store.setErrorMessage(error instanceof Error ? error.message : "下载会话日志失败");
    }
  }, [store]);

  const refreshSessionData = useCallback(
    async (sessionIdOverride?: string) => {
      const state = useChatStore.getState();
      const targetSessionId = sessionIdOverride ?? state.sessionId;
      if (!targetSessionId) {
        return;
      }

      const [sessionResult, contextResult, eventsResult] = await Promise.all([
        getSession(state.apiBaseUrl, targetSessionId),
        getSessionContext(state.apiBaseUrl, targetSessionId),
        getEvents(state.apiBaseUrl, targetSessionId),
      ]);

      const nextProfileId = contextResult.profile_id ?? sessionResult.profile_id ?? "";
      const nextProfileName = contextResult.profile_name ?? sessionResult.profile_name ?? "";
      const nextUserId = contextResult.user_id ?? sessionResult.user_id ?? state.userId;
      if (contextResult.user_display_name || sessionResult.user_display_name) {
        store.setDebugIdentity({
          ...(useChatStore.getState().debugIdentity ?? { user_id: nextUserId, display_name: "", profile_id: "", session_id: "", school_year: "", grade: "" }),
          user_id: nextUserId,
          display_name: contextResult.user_display_name ?? sessionResult.user_display_name ?? "",
          profile_id: nextProfileId,
          session_id: targetSessionId,
        });
      }
      const nextActiveSkill =
        sessionResult.skill_display?.skill_id ||
        inferActiveSkill(
          sessionResult.skill_states,
          state.sessionList.find((item) => item.session_id === targetSessionId)?.active_skill ?? null,
        );
      const nextScenario =
        sessionResult.skill_display?.scene_name ||
        inferCurrentScenario(
          contextResult.interaction_state,
          (sessionResult.skill_states?.planner ?? {}) as Record<string, unknown>,
          (sessionResult.skill_states?.router ?? {}) as Record<string, unknown>,
          ((sessionResult.skill_states?.career_plan_entity ?? sessionResult.skill_states?.main_planner) ?? {}) as Record<string, unknown>,
        );
      store.setSessionId(targetSessionId);
      store.setSessionTitle(contextResult.title ?? sessionResult.title ?? "");
      store.setActiveProfileId(nextProfileId);
      store.setActiveProfileName(nextProfileName);
      store.setActiveSkill(nextActiveSkill);
      store.setCurrentScenario(nextScenario);
      const contextMessages = normalizeContextMessages(contextResult.messages);
      store.setMessages(
        contextMessages.some((message) => message.messageType === "client_opening")
          ? contextMessages
          : [
              makeMessage("assistant", buildClientOpeningMessage(sessionResult.recent_session_summary), [], {
                messageType: "client_opening",
              }),
              ...contextMessages,
            ],
      );
      store.setCandidatePaths(sessionResult.candidate_paths);
      store.setFactsSnapshot({
        userFacts: contextResult.user_facts,
        sharedFacts: contextResult.shared_facts ?? contextResult.user_facts,
        profileFacts: contextResult.profile_facts ?? {},
        sessionFacts: contextResult.session_facts,
        effectiveFacts: contextResult.effective_facts,
      });
      store.setSkillStates(sessionResult.skill_states);
      store.setEvents(eventsResult.events);
      store.setLastResponse(null);
    },
    [store],
  );

  const loadSessionList = useCallback(
    async (profileId: string, userIdOverride?: string) => {
      const state = useChatStore.getState();
      const userId = userIdOverride ?? state.userId;
      if (!userId || !profileId) {
        store.setSessionList([]);
        return [];
      }
      store.setLoadingSessions(true);
      try {
        const response = await listProfileSessions(state.apiBaseUrl, userId, profileId);
        store.setSessionList(response.sessions ?? []);
        const currentSessionId = useChatStore.getState().sessionId;
        if (currentSessionId) {
          const currentSession = (response.sessions ?? []).find((item) => item.session_id === currentSessionId);
          if (currentSession?.active_skill) {
            store.setActiveSkill(currentSession.active_skill);
          }
        }
        return response.sessions ?? [];
      } finally {
        store.setLoadingSessions(false);
      }
    },
    [store],
  );

  const loadSession = useCallback(
    async (sessionId: string) => {
      abortCurrentStream();
      store.setSwitchingSession(true);
      store.setErrorMessage("");
      try {
        await refreshSessionData(sessionId);
      } catch (error) {
        store.setErrorMessage(error instanceof Error ? error.message : "加载历史会话失败");
      } finally {
        store.setSwitchingSession(false);
      }
    },
    [abortCurrentStream, refreshSessionData, store],
  );

  const loadProfiles = useCallback(async (userIdOverride?: string) => {
    const state = useChatStore.getState();
    const userId = userIdOverride ?? state.userId;
    if (!userId) {
      store.setProfiles([]);
      return [];
    }
    store.setLoadingProfiles(true);
    try {
      const response = await listProfiles(state.apiBaseUrl, userId);
      const profiles = response.profiles ?? [];
      store.setProfiles(profiles);
      return profiles;
    } finally {
      store.setLoadingProfiles(false);
    }
  }, [store]);

  const applyDebugIdentity = useCallback(
    async (identity: DebugIdentity, options: ApplyDebugIdentityOptions = {}) => {
      const applySequence = ++identityApplySequenceRef.current;
      const isCurrentApply = () => applySequence === identityApplySequenceRef.current;
      abortCurrentStream();
      store.setDebugIdentity(identity);
      store.setProfiles([]);
      store.setSessionList([]);
      store.resetConversation();
      store.setActiveProfileId("");
      store.setActiveProfileName("");
      store.setErrorMessage("");
      store.setUserId(identity.user_id);

      if (!options.refreshProfileWorkspace) {
        // The single stream endpoint creates the session lazily on the first
        // message. Local debugging deliberately follows the BFF payload shape.
        store.setActiveProfileId(identity.profile_id);
        store.setActiveProfileName(identity.display_name);
        store.setSessionId(identity.session_id);
        await loadSkillCatalog();
        return;
      }

      const state = useChatStore.getState();
      const profilesResponse = await listProfiles(state.apiBaseUrl, identity.user_id);
      if (!isCurrentApply()) return;
      const profiles = profilesResponse.profiles ?? [];
      store.setProfiles(profiles);
      const profile = profiles.find((item) => item.profile_id === identity.profile_id) ?? profiles[0];

      if (!profile) {
        // Preserve the established forwarding fallback for a user whose first
        // request creates its profile lazily through the stream endpoint.
        store.setActiveProfileId(identity.profile_id);
        store.setActiveProfileName(identity.display_name);
        store.setSessionId(identity.session_id || makeMessageId("session"));
        await loadSkillCatalog();
        return;
      }

      const [profileFactsResponse, userFactsResponse, sessionsResponse] = await Promise.all([
        getProfileFacts(state.apiBaseUrl, identity.user_id, profile.profile_id),
        getUserFacts(state.apiBaseUrl, identity.user_id),
        listProfileSessions(state.apiBaseUrl, identity.user_id, profile.profile_id),
      ]);
      if (!isCurrentApply()) return;

      const profileFacts = profileFactsResponse.facts ?? {};
      const sharedFacts = userFactsResponse.shared_facts ?? userFactsResponse.facts ?? {};
      const sessions = sessionsResponse.sessions ?? [];
      const session = sessions.find((item) => item.session_id === identity.session_id) ?? sessions[0];
      const resolvedIdentity: DebugIdentity = {
        ...identity,
        display_name: profile.name || identity.display_name,
        profile_id: profile.profile_id,
        session_id: session?.session_id ?? makeMessageId("session"),
        school_year: currentSchoolYear(profileFacts),
        grade: textFactValue(profileFacts, "grade"),
      };
      store.setDebugIdentity(resolvedIdentity);
      store.setActiveProfileId(profile.profile_id);
      store.setActiveProfileName(profile.name);
      store.setSessionList(sessions);
      store.setFactsSnapshot({
        userFacts: userFactsResponse.facts,
        sharedFacts,
        profileFacts,
        effectiveFacts: { ...sharedFacts, ...profileFacts },
      });
      await loadSkillCatalog();
      if (!isCurrentApply()) return;

      if (session?.session_id) {
        await refreshSessionData(session.session_id);
        return;
      }
      store.setSessionId(resolvedIdentity.session_id);
      store.setSessionTitle("");
      store.setActiveSkill("");
    },
    [abortCurrentStream, loadSkillCatalog, refreshSessionData, store],
  );

  const resetDebugIdentity = useCallback(async () => {
    abortCurrentStream();
    store.setDebugIdentity(null);
    store.resetConversation();
    store.setProfiles([]);
    store.setSessionList([]);
    store.setActiveProfileId("");
    store.setActiveProfileName("");
  }, [abortCurrentStream, store]);

  const bootstrapDebugIdentity = useCallback(async () => {
    await loadSkillCatalog();
    const identity = useChatStore.getState().debugIdentity;
    if (!identity?.user_id) {
      return;
    }

    // Restore the last local request identity.  The first stream creates the
    // session if it does not exist yet.
    if (identity.profile_id && identity.session_id) {
      store.setUserId(identity.user_id);
      store.setActiveProfileId(identity.profile_id);
      store.setActiveProfileName(identity.display_name);
      store.setSessionId(identity.session_id);
      return;
    }

    // Older stored identities may not contain generated IDs.  Re-entering
    // through the normal adapter fills them in safely.
    await applyDebugIdentity(identity);
  }, [applyDebugIdentity, loadSkillCatalog, store]);

  const selectProfile = useCallback(
    async (profileId: string) => {
      if (!profileId) {
        return;
      }
      const state = useChatStore.getState();
      store.setActiveProfileId(profileId);
      const profile = state.profiles.find((item) => item.profile_id === profileId);
      store.setActiveProfileName(profile?.name ?? "");
      const sessions = await loadSessionList(profileId, state.userId);
      const preferredSession = sessions[0] ?? null;
      if (preferredSession?.session_id) {
        await loadSession(preferredSession.session_id);
        return;
      }
      // A newly created child may not have any session yet. Keep the workspace
      // usable by creating its first session immediately instead of leaving an
      // empty, disabled composer behind.
      store.resetConversation();
      store.setActiveProfileId(profileId);
      store.setActiveProfileName(profile?.name ?? "");
      store.setActiveSkill("");
      const sessionId = makeMessageId("session");
      store.setSessionId(sessionId);
      store.setSessionTitle("");
      const identity = useChatStore.getState().debugIdentity;
      if (identity) store.setDebugIdentity({ ...identity, profile_id: profileId, session_id: sessionId });
    },
    [loadSession, loadSessionList, store],
  );

  const handleCreateProfile = useCallback(
    async ({ name, schoolYear, grade }: { name: string; schoolYear: string; grade: string }) => {
      const state = useChatStore.getState();
      if (!state.userId) {
        throw new Error("缺少测试用户");
      }
      store.setErrorMessage("");
      try {
        const response = await createProfile(state.apiBaseUrl, state.userId, {
          name,
          initialize_from_shared_facts: false,
        });
        await upsertProfileFacts(state.apiBaseUrl, state.userId, response.profile.profile_id, {
          scope: "profile",
          source: {
            type: "demo_profile_creation",
            source_id: `profile:${response.profile.profile_id}`,
            source_label: "Demo 新建孩子",
          },
          updates: [
            { key: "profile_school_facts", value: [{ school_year: schoolYear, grade }] },
            { key: "grade", value: grade },
          ],
        });
        const identity = useChatStore.getState().debugIdentity;
        if (identity) {
          store.setDebugIdentity({
            ...identity,
            display_name: name,
            profile_id: response.profile.profile_id,
            session_id: "",
            school_year: schoolYear,
            grade,
          });
        }
        const nextProfiles = [...useChatStore.getState().profiles, response.profile];
        store.setProfiles(nextProfiles);
        await selectProfile(response.profile.profile_id);
      } catch (error) {
        store.setErrorMessage(error instanceof Error ? error.message : "创建孩子档案失败");
      }
    },
    [selectProfile, store],
  );

  const handleCreateSession = useCallback(async () => {
    const state = useChatStore.getState();
    if (!state.userId || !state.activeProfileId) {
      throw new Error("请先选择孩子档案");
    }
    store.setCreatingSession(true);
    store.setErrorMessage("");
    try {
      const sessionId = makeMessageId("session");
      store.resetConversation();
      store.setActiveProfileId(state.activeProfileId);
      store.setActiveProfileName(state.activeProfileName);
      store.setActiveSkill("");
      store.setSessionId(sessionId);
      store.setSessionTitle("");
      const identity = useChatStore.getState().debugIdentity;
      if (identity) store.setDebugIdentity({ ...identity, session_id: sessionId });
    } catch (error) {
      store.setErrorMessage(error instanceof Error ? error.message : "创建会话失败");
    } finally {
      store.setCreatingSession(false);
    }
  }, [loadSessionList, store]);

  const handleRenameSession = useCallback(
    async (sessionId: string, title: string) => {
      await updateSessionTitle(store.apiBaseUrl, sessionId, { title });
      const nextSessionList = useChatStore.getState().sessionList.map((item) =>
        item.session_id === sessionId ? { ...item, title } : item,
      );
      store.setSessionList(nextSessionList);
      if (useChatStore.getState().sessionId === sessionId) {
        store.setSessionTitle(title);
      }
    },
    [store],
  );

  const handleDeleteSession = useCallback(
    async (sessionId: string) => {
      const state = useChatStore.getState();
      if (!state.userId || !state.activeProfileId) {
        throw new Error("请先选择孩子档案");
      }
      try {
        await deleteSession(store.apiBaseUrl, sessionId, state.userId, state.activeProfileId);
        const remaining = useChatStore.getState().sessionList.filter((item) => item.session_id !== sessionId);
        store.setSessionList(remaining);
        if (useChatStore.getState().sessionId !== sessionId) {
          return;
        }
        const nextSession = remaining[0];
        if (nextSession) {
          await loadSession(nextSession.session_id);
          return;
        }
        await handleCreateSession();
      } catch (error) {
        store.setErrorMessage(error instanceof Error ? error.message : "删除会话失败");
        throw error;
      }
    },
    [handleCreateSession, loadSession, store],
  );

  const handleSendMessage = useCallback(
    async (
      content: string,
      options: {
        requestedTargetSkillId?: string;
        handoffContext?: Record<string, unknown>;
        reuseAssistantMessageId?: string;
        appendUserMessage?: boolean;
        enableThinkingOverride?: boolean;
        transition?: {
          action: "enter" | "exit";
          targetSkillId?: string;
          source: "toolbar" | "route_suggestion" | "exit_button";
          sourceMessageId?: string;
          sourceInteractionId?: string;
        };
      } = {},
    ) => {
      if (!store.sessionId) {
        throw new Error("请先创建会话");
      }

      const trimmed = content.trim();
      if (!trimmed) {
        return;
      }

      const currentAbortController = useChatStore.getState().streamAbortController;
      if (currentAbortController && !options.reuseAssistantMessageId) {
        supersededStreamControllers.add(currentAbortController);
        currentAbortController.abort();
        store.setStreamAbortController(null);
        store.setSending(false);
        const interruptedMessage = [...useChatStore.getState().messages]
          .reverse()
          .find((message) => message.role === "assistant" && message.streamingStatus === "streaming");
        if (interruptedMessage) {
          store.updateMessage(interruptedMessage.id, (message) => ({
            ...message,
            content: "",
            presentation: message.presentation
              ? {
                  ...message.presentation,
                  assistant: { content: "", status: "superseded" },
                  form: {},
                  path_options: {},
                  skill_rooms: [],
                }
              : message.presentation,
            generationStatus: "superseded",
            streamingStatus: "completed",
            errorMessage: undefined,
          }));
          store.completeReasoning(interruptedMessage.id);
          store.completeRuntimeStatus(interruptedMessage.id);
        }
      }

      store.setSending(true);
      store.setErrorMessage("");
      let assistantMessageId = options.reuseAssistantMessageId ?? "";
      const exitTransition = options.transition?.action === "exit";
      const thinkingEnabledForTurn = options.enableThinkingOverride ?? useChatStore.getState().enableThinking;
      const retryRequest: ChatRetryRequest = {
        content: trimmed,
        enableThinking: thinkingEnabledForTurn,
        requestedTargetSkillId: options.requestedTargetSkillId,
        handoffContext: options.handoffContext,
      };
      let streamFailed = false;
      let abortController: AbortController | null = null;
      let mainContentEnded = false;

      try {
        if (exitTransition) {
          assistantMessageId = store.createAssistantPlaceholder();
        } else if (options.appendUserMessage !== false) {
          store.setMessages(
            useChatStore.getState().messages.map((message) => {
              if (message.role !== "assistant" || !message.interactionStates) {
                return message;
              }
              const nextStates = Object.fromEntries(
                Object.entries(message.interactionStates).map(([key, state]) => [
                  key,
                  state.status === "active" ? { ...state, status: "expired" } : state,
                ]),
              );
              const pathOptions = message.presentation?.path_options;
              return {
                ...message,
                interactionStates: nextStates,
                presentation: message.presentation
                  ? {
                      ...message.presentation,
                      path_options:
                        pathOptions && "options" in pathOptions
                          ? {
                              ...pathOptions,
                              status: "expired",
                              options: pathOptions.options.map((option) => ({ ...option, enabled: false })),
                            }
                          : {},
                    }
                  : message.presentation,
              };
            }),
          );
          store.appendMessage(makeMessage("user", trimmed));
          assistantMessageId = store.createAssistantPlaceholder();
          store.setComposerValue("");
        } else if (assistantMessageId) {
          store.updateMessage(assistantMessageId, (message) => ({
            ...message,
            content: "",
            blocks: [],
            errorMessage: undefined,
            streamingStatus: "streaming",
            reasoningContent: "",
            reasoningStatus: "idle",
            reasoningExpanded: Boolean(thinkingEnabledForTurn),
            routeSuggestions: [],
            selectedRouteSuggestion: "",
            retryRequest,
          }));
        } else {
          assistantMessageId = store.createAssistantPlaceholder();
        }
        if (assistantMessageId) {
          store.updateMessage(assistantMessageId, (message) => ({
            ...message,
            retryRequest,
          }));
        }
        abortController = new AbortController();
        // A previous stream may have just completed; its run id must never be
        // used while this request is waiting for its own `run_started` event.
        pendingStopStreamRef.current = null;
        cancellingRunRef.current = "";
        store.setCurrentRunId("");
        store.setCancellingRun(false);
        store.setStreamAbortController(abortController);
        const releaseComposerForThisStream = () => {
          mainContentEnded = true;
          if (abortController && useChatStore.getState().streamAbortController === abortController) {
            store.setSending(false);
          }
        };

        const identity = useChatStore.getState().debugIdentity;
        if (!identity) {
          throw new Error("缺少本地调试身份");
        }
        const input = options.transition
          ? {
              action: options.transition.action === "enter" ? "enter_skill" : "quit_skill",
              target_skill_id: options.transition.targetSkillId ?? useChatStore.getState().activeSkill ?? "career_plan_entity",
              source: options.transition.source,
              source_message_id: options.transition.sourceMessageId,
              source_interaction_id: options.transition.sourceInteractionId,
              enable_thinking: thinkingEnabledForTurn,
              return_reasoning: thinkingEnabledForTurn,
            }
          : { action: "chat", content: trimmed, source: "chat", enable_thinking: thinkingEnabledForTurn, return_reasoning: thinkingEnabledForTurn };
        const runId = makeMessageId("run");
        let latestStateSeq = -1;
        await postSseStream({
            url: `${store.apiBaseUrl}/api/v1/sessions/chat/stream`,
            body: {
              session_id: store.sessionId,
              run_id: runId,
              input: JSON.stringify(input),
              context_data: buildContextData(identity),
            },
            signal: abortController.signal,
            onEvent: (event: StreamEvent) => {
              if (event.event === "state") {
                const state = event.data as SseV2State;
                if (state.run_id !== runId || state.seq <= latestStateSeq) {
                  return;
                }
                latestStateSeq = state.seq;
                store.setCurrentRunId(state.run_id);
                if (pendingStopStreamRef.current === abortController && abortController) {
                  void cancelStreamRun(abortController, state.run_id);
                } else {
                  store.setCancellingRun(false);
                }
                const activeSkill = state.session.active_skill.skill_id ?? "";
                if (activeSkill) {
                  store.setActiveSkill(activeSkill);
                  store.setCurrentScenario(state.session.active_skill.scene_name ?? "");
                }
                store.updateMessage(assistantMessageId, (message) => ({
                  ...message,
                  messageId: state.message_id ?? message.messageId,
                  content: state.assistant.content,
                  presentation: presentationFromState(state),
                  blocks: [],
                  routeSuggestions: [],
                  reasoningContent: "",
                  reasoningStatus: "idle",
                  generationStatus: state.status === "stopped" ? "cancelled" : state.status,
                  streamingStatus: isTerminalState(state.status) ? (state.status === "failed" || state.status === "blocked" ? "failed" : "completed") : "streaming",
                  errorMessage: state.risk.blocked
                    ? state.risk.message
                    : state.error.message
                      ? `${state.error.code ? `${state.error.code}：` : ""}${state.error.message}`
                      : state.status === "failed"
                        ? "流式消息执行失败"
                        : undefined,
                }));
                if (state.risk.blocked) {
                  store.setErrorMessage(state.risk.message);
                }
                if (isTerminalState(state.status)) {
                  store.setSending(false);
                  store.setCancellingRun(false);
                  releaseComposerForThisStream();
                }
                return;
              }
              switch (event.event) {
                case "skill_transition": {
                  const transition = event.data as SkillTransition & { message_id?: string };
                  if (transition.action === "exit" && transition.synthetic_user_message) {
                    const synthetic = transition.synthetic_user_message;
                    const exists = useChatStore.getState().messages.some(
                      (message) => message.messageId === synthetic.message_id,
                    );
                    if (!exists) {
                      store.appendMessage(
                        makeMessage("user", synthetic.content, [], {
                          messageId: synthetic.message_id,
                          createdAt: synthetic.created_at,
                          messageType: "skill_exit_command",
                        }),
                      );
                    }
                  }
                  const transitionMessage = makeMessage("assistant", "", [], {
                    messageId: transition.message_id,
                    messageType: "skill_transition",
                    skillTransition: transition,
                  });
                  const currentMessages = useChatStore.getState().messages;
                  const placeholderIndex = currentMessages.findIndex((message) => message.id === assistantMessageId);
                  if (placeholderIndex >= 0) {
                    store.setMessages([
                      ...currentMessages.slice(0, placeholderIndex),
                      transitionMessage,
                      ...currentMessages.slice(placeholderIndex),
                    ]);
                  } else {
                    store.appendMessage(transitionMessage);
                  }
                  if (transition.action === "exit") {
                    store.setActiveSkill("general_chat");
                    store.setCurrentScenario("");
                    store.setCandidatePaths([]);
                  }
                  break;
                }
                case "run_started":
                  if (event.data.run_id) {
                    const runId = String(event.data.run_id);
                    store.setCurrentRunId(runId);
                    if (pendingStopStreamRef.current === abortController && abortController) {
                      void cancelStreamRun(abortController, runId);
                    } else {
                      store.setCancellingRun(false);
                    }
                  }
                  break;
                case "skill_context": {
                  const contextData = event.data as SkillContextData;
                  const nextScenario = String(contextData.scene_name ?? "").trim();
                  store.updateMessage(assistantMessageId, (message) => ({
                    ...message,
                    skillId: (contextData.active_skill as string | null | undefined) ?? message.skillId,
                    skillName:
                      (contextData.active_skill_label as string | null | undefined) ?? message.skillName,
                    agentLabel: (contextData.agent_label as string | null | undefined) ?? message.agentLabel,
                    skillBrief: (contextData.skill_brief as string | null | undefined) ?? message.skillBrief,
                    skillInfo: (contextData.skill_info as string | null | undefined) ?? message.skillInfo,
                    sceneName: (contextData.scene_name as string | null | undefined) ?? message.sceneName,
                    themeKey: (contextData.skill_theme as string | null | undefined) ?? message.themeKey,
                  }));
                  if (contextData.active_skill) {
                    store.setActiveSkill(contextData.active_skill);
                  }
                  if (nextScenario) {
                    store.setCurrentScenario(nextScenario);
                  }
                  break;
                }
                case "skill_intro": {
                  const introData = event.data as SkillIntroData;
                  const introInfo = String(introData.info ?? introData.description ?? "");
                  const introMessage = makeMessage("assistant", introInfo, [], {
                    messageType: "skill_intro",
                    skillIntro: {
                      skill_id: String(introData.skill_id ?? ""),
                      skill_label: String(introData.skill_label ?? ""),
                      brief: String(introData.brief ?? ""),
                      info: introInfo,
                      description: String(introData.description ?? ""),
                      scene_name: String(introData.scene_name ?? ""),
                      skill_theme: String(introData.skill_theme ?? ""),
                    },
                    skillId: String(introData.skill_id ?? "") || undefined,
                    skillName: String(introData.skill_label ?? "") || undefined,
                    agentLabel: String(introData.skill_label ?? "") || undefined,
                    skillBrief: String(introData.brief ?? "") || undefined,
                    skillInfo: introInfo || undefined,
                    sceneName: String(introData.scene_name ?? "") || undefined,
                    themeKey: String(introData.skill_theme ?? "") || undefined,
                  });
                  const currentMessages = useChatStore.getState().messages;
                  const placeholderIndex = currentMessages.findIndex(
                    (message) => message.id === assistantMessageId,
                  );
                  if (placeholderIndex >= 0) {
                    store.setMessages([
                      ...currentMessages.slice(0, placeholderIndex),
                      introMessage,
                      ...currentMessages.slice(placeholderIndex),
                    ]);
                  } else {
                    store.appendMessage(introMessage);
                  }
                  break;
                }
                case "skill_status":
                  if ("stage" in event.data && "label" in event.data) {
                    const sseMeta = ((event.data as Record<string, unknown>)._sse ?? {}) as Record<string, unknown>;
                    store.pushRuntimeStatus(assistantMessageId, {
                      stage: String(event.data.stage ?? ""),
                      label: String(event.data.label ?? ""),
                      detail: typeof event.data.detail === "string" ? event.data.detail : undefined,
                      summary: typeof event.data.summary === "string" ? event.data.summary : undefined,
                      source: typeof event.data.source === "string" ? event.data.source : undefined,
                      seq: typeof sseMeta.seq === "number" ? sseMeta.seq : undefined,
                      elapsedMs: typeof sseMeta.elapsed_ms === "number" ? sseMeta.elapsed_ms : undefined,
                      timestamp: typeof sseMeta.ts === "string" ? sseMeta.ts : undefined,
                    });
                  }
                  break;
                case "message_block":
                  store.upsertMessageBlock(
                    assistantMessageId,
                    normalizeMessageBlocks([event.data])[0] ?? {
                      type: "unknown",
                      payload: event.data,
                    },
                  );
                  break;
                case "final_text_delta":
                  if ("delta" in event.data) {
                    store.appendAssistantDelta(assistantMessageId, String(event.data.delta ?? ""));
                  }
                  break;
                case "main_content_end": {
                  const mainContent = String(event.data.assistant_message ?? "");
                  store.updateMessage(assistantMessageId, (message) => ({
                    ...message,
                    messageId: (event.data.message_id as string | null | undefined) ?? message.messageId,
                    content: mainContent || message.content,
                    streamingStatus: "completed",
                  }));
                  store.completeReasoning(assistantMessageId);
                  store.completeRuntimeStatus(assistantMessageId);
                  releaseComposerForThisStream();
                  break;
                }
                case "reasoning_delta":
                  if (thinkingEnabledForTurn && "delta" in event.data) {
                    store.appendReasoningDelta(assistantMessageId, String(event.data.delta ?? ""));
                  }
                  break;
                case "skill_lifecycle":
                  if (event.data.type === "finalized") {
                    store.updateMessage(assistantMessageId, (message) => ({
                      ...message,
                      conclusionSummary:
                        (event.data.conclusion_summary as string | null | undefined) ?? message.conclusionSummary,
                      contextCompression:
                        (event.data.context_compression as typeof message.contextCompression | null | undefined) ??
                        message.contextCompression,
                      routeSuggestions:
                        (event.data.route_suggestions as RouteSuggestion[] | undefined) ?? message.routeSuggestions,
                    }));
                  }
                  break;
                case "skill_action": {
                  const actionData = event.data as SkillActionData;
                  const messageBlocks = normalizeMessageBlocks(actionData.message_blocks);
                  const nextScenario = String(actionData.scene_name ?? "").trim();
                  store.updateMessage(assistantMessageId, (message) => ({
                    ...message,
                    blocks: messageBlocks.length ? mergeFinalMessageBlocks(message.blocks, messageBlocks) : message.blocks,
                    skillId: (actionData.active_skill as string | null | undefined) ?? message.skillId,
                    skillName: (actionData.active_skill_label as string | null | undefined) ?? message.skillName,
                    agentLabel: (actionData.agent_label as string | null | undefined) ?? message.agentLabel,
                    skillBrief: (actionData.skill_brief as string | null | undefined) ?? message.skillBrief,
                    skillInfo: (actionData.skill_info as string | null | undefined) ?? message.skillInfo,
                    sceneName: (actionData.scene_name as string | null | undefined) ?? message.sceneName,
                    themeKey: (actionData.skill_theme as string | null | undefined) ?? message.themeKey,
                    conclusionSummary:
                      (actionData.conclusion_summary as string | null | undefined) ?? message.conclusionSummary,
                    contextCompression:
                      (actionData.context_compression as typeof message.contextCompression | null | undefined) ??
                      message.contextCompression,
                    routeSuggestions:
                      (actionData.route_suggestions as RouteSuggestion[] | undefined) ?? message.routeSuggestions,
                    streamingStatus: "completed",
                  }));
                  if (actionData.active_skill) {
                    store.setActiveSkill(actionData.active_skill);
                  }
                  if (nextScenario) {
                    store.setCurrentScenario(nextScenario);
                  }
                  break;
                }
                case "final_message": {
                  const finalData = event.data as FinalMessageData;
                  const messageBlocks = normalizeMessageBlocks(finalData.message_blocks);
                  const currentSkillStates = useChatStore.getState().skillStates;
                  const nextScenario =
                    String(finalData.scene_name ?? "").trim() ||
                    inferCurrentScenario(
                      undefined,
                      (finalData.planner_state ?? {}) as Record<string, unknown>,
                      (finalData.router_state ?? {}) as Record<string, unknown>,
                      ((finalData.career_plan_state ?? finalData.main_planner_state) ?? {}) as Record<string, unknown>,
                    );
                  store.updateMessage(assistantMessageId, (message) => ({
                    ...message,
                    messageId: finalData.message_id ?? message.messageId,
                    content: mainContentEnded
                      ? message.content || finalData.assistant_message || ""
                      : finalData.assistant_message ?? message.content,
                    reasoningContent:
                      thinkingEnabledForTurn
                        ? message.reasoningContent ||
                          String(
                            (finalData as unknown as { reasoning?: string }).reasoning ??
                              (((finalData.career_plan_state ?? finalData.main_planner_state) ?? {}) as { last_reasoning?: string })
                                .last_reasoning ??
                              "",
                          )
                        : "",
                    blocks: messageBlocks.length ? mergeFinalMessageBlocks(message.blocks, messageBlocks) : message.blocks,
                    skillId: (finalData.active_skill as string | null | undefined) ?? message.skillId,
                    skillName: (finalData.active_skill_label as string | null | undefined) ?? message.skillName,
                    agentLabel: (finalData.agent_label as string | null | undefined) ?? message.agentLabel,
                    skillBrief: (finalData as FinalMessageData & { skill_brief?: string }).skill_brief ?? message.skillBrief,
                    skillInfo: (finalData as FinalMessageData & { skill_info?: string }).skill_info ?? message.skillInfo,
                    sceneName: (finalData.scene_name as string | null | undefined) ?? nextScenario,
                    themeKey: (finalData.skill_theme as string | null | undefined) ?? message.themeKey,
                    conclusionSummary:
                      (finalData.conclusion_summary as string | null | undefined) ?? message.conclusionSummary,
                    contextCompression:
                      (finalData.context_compression as typeof message.contextCompression | null | undefined) ??
                      message.contextCompression,
                    routeSuggestions:
                      (finalData.route_suggestions as RouteSuggestion[] | undefined) ?? message.routeSuggestions,
                    streamingStatus: "completed",
                  }));
                  store.completeReasoning(assistantMessageId);
                  store.setLastResponse(buildMessageResponseFromStream(finalData));
                  store.setActiveSkill((finalData.active_skill as string | null | undefined) ?? "");
                  store.setCurrentScenario(nextScenario);
                  store.setCandidatePaths(
                    (finalData.candidate_paths_brief ?? []) as MessageResponse["candidate_paths_brief"],
                  );
                  store.setFactsSnapshot({
                    userFacts: (finalData.user_facts ?? finalData.shared_facts ?? {}) as FactMap,
                    sharedFacts: (finalData.shared_facts ?? finalData.user_facts ?? {}) as FactMap,
                    profileFacts: (finalData.profile_facts ?? {}) as FactMap,
                    sessionFacts: (finalData.session_facts ?? {}) as FactMap,
                    effectiveFacts: (finalData.effective_facts ?? {}) as FactMap,
                  });
                  store.setSkillStates({
                    ...currentSkillStates,
                    router: (finalData.router_state ?? {}) as Record<string, unknown>,
                    facts_extractor: (finalData.facts_extractor_state ?? {}) as Record<string, unknown>,
                    planner: (finalData.planner_state ?? {}) as Record<string, unknown>,
                    career_plan_entity: ((finalData.career_plan_state ?? finalData.main_planner_state) ?? {}) as Record<string, unknown>,
                  });
                  if (typeof finalData.profile_name === "string") {
                    store.setActiveProfileName(finalData.profile_name);
                  }
                  break;
                }
                case "run_failed":
                  if (!mainContentEnded) {
                    streamFailed = true;
                    store.failStreamingMessage(
                      assistantMessageId,
                      String(event.data.message ?? "流式消息执行失败"),
                    );
                    store.setErrorMessage(String(event.data.message ?? "流式消息执行失败"));
                  }
                  break;
                case "run_cancelled":
                  store.updateMessage(assistantMessageId, (message) => ({
                    ...message,
                    messageId: (event.data.message_id as string | undefined) || message.messageId,
                    generationStatus: "cancelled",
                    streamingStatus: "completed",
                    errorMessage: undefined,
                  }));
                  store.completeReasoning(assistantMessageId);
                  store.completeRuntimeStatus(assistantMessageId);
                  store.setCancellingRun(false);
                  break;
                case "moderation_blocked": {
                  const message = String(event.data.message ?? "该内容检测到非合规内容，当前对话中断，如果需要请重新输入");
                  store.updateMessage(assistantMessageId, (current) => ({
                    ...current,
                    content: "",
                    reasoningContent: "",
                    blocks: [],
                    streamingStatus: "failed",
                    errorMessage: message,
                  }));
                  store.completeReasoning(assistantMessageId);
                  store.completeRuntimeStatus(assistantMessageId);
                  store.setSending(false);
                  store.setErrorMessage(message);
                  break;
                }
                case "run_completed":
                  store.completeRuntimeStatus(assistantMessageId);
                  if (event.data.status === "cancelled") {
                    store.updateMessage(assistantMessageId, (message) => ({
                      ...message,
                      streamingStatus: "completed",
                    }));
                    store.setCancellingRun(false);
                  }
                  break;
                default:
                  break;
              }
            },
            onRetry: (attempt, error) => {
              store.updateMessage(assistantMessageId, (message) => ({
                ...message,
                errorMessage: `连接中断，正在自动重试第 ${attempt}/3 次：${error.message}`,
                streamingStatus: "streaming",
              }));
            },
        });

        if (streamFailed) {
          return;
        }

        await refreshEventPanels();
        await loadSessionList(useChatStore.getState().activeProfileId);
      } catch (error) {
        if (abortController && supersededStreamControllers.has(abortController)) {
          return;
        }
        if (assistantMessageId) {
          store.failStreamingMessage(
            assistantMessageId,
            error instanceof Error ? error.message : "发送消息失败",
          );
        }
        store.setErrorMessage(error instanceof Error ? error.message : "发送消息失败");
      } finally {
        if (abortController && useChatStore.getState().streamAbortController === abortController) {
          if (pendingStopStreamRef.current === abortController) {
            pendingStopStreamRef.current = null;
          }
          cancellingRunRef.current = "";
          store.setStreamAbortController(null);
          store.setCurrentRunId("");
          store.setCancellingRun(false);
          if (!mainContentEnded) {
            store.setSending(false);
          }
        }
      }
    },
    [cancelStreamRun, loadSessionList, refreshEventPanels, store],
  );

  const handleRetryMessage = useCallback(
    async (messageId: string) => {
      const message = useChatStore.getState().messages.find((item) => item.id === messageId);
      const retryRequest = message?.retryRequest;
      if (!retryRequest?.content?.trim()) {
        store.setErrorMessage("这条失败消息缺少可重试的请求信息");
        return;
      }
      await handleSendMessage(retryRequest.content, {
        requestedTargetSkillId: retryRequest.requestedTargetSkillId,
        handoffContext: retryRequest.handoffContext,
        reuseAssistantMessageId: messageId,
        appendUserMessage: false,
        enableThinkingOverride: retryRequest.enableThinking,
      });
    },
    [handleSendMessage, store],
  );

  const handleEnterSkill = useCallback(
    async (skill: SkillCatalogItem) => {
      await handleSendMessage(`进入${skill.label}`, {
        appendUserMessage: false,
        transition: { action: "enter", targetSkillId: skill.skill_id, source: "toolbar" },
      });
    },
    [handleSendMessage],
  );

  const handleRouteSuggestion = useCallback(
    async (messageId: string, suggestion: RouteSuggestion) => {
      const sourceMessageId = useChatStore.getState().messages.find((message) => message.id === messageId)?.messageId;
      if (!sourceMessageId) {
        throw new Error("当前建议消息尚未保存，无法切换 Skill");
      }
      const previousInteraction = useChatStore.getState().messages.find((message) => message.id === messageId)?.interactionStates?.route_suggestions;
      store.updateMessage(messageId, (message) => ({
        ...message,
        interactionStates: {
          ...(message.interactionStates ?? {}),
          route_suggestions: {
            kind: "route_suggestions",
            status: "selected",
            selected_target_skill_id: suggestion.target_skill_id,
          },
        },
      }));
      const agentLabel = suggestion.agent_label || suggestion.target_skill_id;
      try {
        await handleSendMessage(`继续到${agentLabel}`, {
          appendUserMessage: false,
          transition: {
            action: "enter",
            targetSkillId: suggestion.target_skill_id,
            source: "route_suggestion",
            sourceMessageId,
            sourceInteractionId: "route_suggestions",
          },
        });
      } catch (error) {
        store.updateMessage(messageId, (message) => ({
          ...message,
          interactionStates: {
            ...(message.interactionStates ?? {}),
            route_suggestions: previousInteraction ?? { kind: "route_suggestions", status: "active" },
          },
        }));
        throw error;
      }
    },
    [handleSendMessage, store],
  );

  const handleExitSkill = useCallback(async () => {
    await handleSendMessage("退出当前顾问", {
      appendUserMessage: false,
      transition: { action: "exit", source: "exit_button" },
    });
  }, [handleSendMessage]);

  const handleStopGeneration = useCallback(async () => {
    const state = useChatStore.getState();
    const stream = state.streamAbortController;
    if (!state.sessionId || !stream || state.isCancellingRun) {
      return;
    }
    pendingStopStreamRef.current = stream;
    state.setCancellingRun(true);
    if (state.currentRunId) {
      await cancelStreamRun(stream, state.currentRunId);
    }
  }, [cancelStreamRun]);

  const handleRefreshEvents = useCallback(async () => {
    if (!store.sessionId) {
      return;
    }
    store.setLoadingEvents(true);
    store.setErrorMessage("");
    try {
      await refreshSessionData();
      await loadSessionList(useChatStore.getState().activeProfileId);
    } catch (error) {
      store.setErrorMessage(error instanceof Error ? error.message : "刷新调试信息失败");
    } finally {
      store.setLoadingEvents(false);
    }
  }, [loadSessionList, refreshSessionData, store]);

  const handleSubmitFactForm = useCallback(
    async (
      messageId: string,
      formId: string,
      fields: FactFormField[],
      draftValues: Record<string, unknown>,
    ) => {
      if (!store.sessionId) {
        throw new Error("请先创建会话");
      }

      const updatesByScope = new Map<string, Array<{ key: string; value: unknown }>>();
      const submittedFields: FactFormField[] = [];
      for (const field of fields) {
        const rawValue = draftValues[field.fact_key];
        const isEmptyArray = Array.isArray(rawValue) && rawValue.length === 0;
        if (rawValue == null || rawValue === "" || isEmptyArray) {
          continue;
        }
        submittedFields.push(field);
        // Native questionnaires are owned by the active Skill.  The answer is
        // included in the existing follow-up message but must never be sent to
        // a generic Fact endpoint unless a server-side mapping promotes it.
        if (field.scope === "skill_session") {
          continue;
        }
        const bucket = updatesByScope.get(field.scope) ?? [];
        bucket.push({ key: field.fact_key, value: rawValue });
        updatesByScope.set(field.scope, bucket);
      }

      if (!submittedFields.length) {
        throw new Error("请先补充至少一项信息");
      }

      store.setErrorMessage("");
      const responses: FactWriteResponse[] = [];

      for (const [scope, updates] of updatesByScope.entries()) {
        if (scope === "session") {
          responses.push(
            await upsertSessionFacts(store.apiBaseUrl, store.sessionId, {
              scope,
              source: {
                type: "user_form",
                source_id: formId,
                source_label: "Assistant Fact Form",
              },
              updates,
            }),
          );
          continue;
        }
        if (scope === "profile") {
          if (!store.activeProfileId) {
            throw new Error("请先选择孩子档案");
          }
          responses.push(
            await upsertProfileFacts(store.apiBaseUrl, store.userId, store.activeProfileId, {
              scope,
              source: {
                type: "user_form",
                source_id: formId,
                source_label: "Assistant Fact Form",
              },
              updates,
            }),
          );
          continue;
        }
        responses.push(
          await upsertUserFacts(store.apiBaseUrl, store.userId, {
            scope: "shared",
            source: {
              type: "user_form",
              source_id: formId,
              source_label: "Assistant Fact Form",
            },
            updates,
          }),
        );
      }

      const mergedFacts = responses.reduce(
        (acc, response) => {
          const nextFacts = pickFactsPayload(response.current_facts);
          return {
            userFacts: nextFacts.userFacts ?? acc.userFacts,
            sharedFacts: nextFacts.sharedFacts ?? acc.sharedFacts,
            profileFacts: nextFacts.profileFacts ?? acc.profileFacts,
            sessionFacts: nextFacts.sessionFacts ?? acc.sessionFacts,
            effectiveFacts: nextFacts.effectiveFacts ?? acc.effectiveFacts,
          };
        },
        {
          userFacts: undefined as FactMap | undefined,
          sharedFacts: undefined as FactMap | undefined,
          profileFacts: undefined as FactMap | undefined,
          sessionFacts: undefined as FactMap | undefined,
          effectiveFacts: undefined as FactMap | undefined,
        },
      );

      store.setFactsSnapshot(mergedFacts);
      const serverMessageId = useChatStore.getState().messages.find((message) => message.id === messageId)?.messageId;
      if (!serverMessageId) {
        throw new Error("表单消息尚未保存，无法同步完成状态");
      }
      const interactionId = `fact_form:${formId}`;
      const interactionResponse = await updateMessageInteraction(
        store.apiBaseUrl,
        store.sessionId,
        serverMessageId,
        interactionId,
        {
          status: "submitted",
          submitted_fact_keys: submittedFields.map((field) => field.fact_key),
        },
      );
      store.clearFormDraft(formId);
      store.updateMessage(messageId, (message) => ({
        ...message,
        interactionStates: {
          ...(message.interactionStates ?? {}),
          [interactionId]: interactionResponse.state,
        },
      }));
      await refreshEventPanels();
      const followupMessage = buildFactSubmissionMessage(fields, draftValues);
      if (followupMessage) {
        await handleSendMessage(followupMessage);
      }
    },
    [handleSendMessage, refreshEventPanels, store],
  );

  const handlePathAction = useCallback(
    (pathName: string, prompt?: string) => {
      void handleSendMessage(prompt || `我想了解：${pathName} 路径`);
    },
    [handleSendMessage],
  );

  const handleClearUserFactsBySource = useCallback(
    async (source: FactSourcePayload) => {
      if (!store.userId) {
        throw new Error("请先填写 User ID");
      }
      store.setErrorMessage("");
      await clearUserFactsBySource(store.apiBaseUrl, store.userId, { source });
      await refreshSessionData();
    },
    [refreshSessionData, store],
  );

  const handleMessageFeedback = useCallback(
    async (messageId: string, feedback: "like" | "dislike") => {
      const state = useChatStore.getState();
      const message = state.messages.find((item) => item.id === messageId);
      const serverMessageId = message?.messageId;
      if (!state.sessionId || !serverMessageId) {
        store.setErrorMessage("当前消息还没有完成保存，暂时无法记录反馈");
        return;
      }
      const nextFeedback = message.feedback === feedback ? null : feedback;
      store.updateMessage(messageId, (item) => ({ ...item, feedback: nextFeedback }));
      try {
        const response = await updateMessageFeedback(
          state.apiBaseUrl,
          state.sessionId,
          serverMessageId,
          nextFeedback,
        );
        store.updateMessage(messageId, (item) => ({
          ...item,
          feedback: response.feedback,
          feedbackUpdatedAt: response.feedback_updated_at,
        }));
      } catch (error) {
        store.updateMessage(messageId, (item) => ({ ...item, feedback: message.feedback }));
        store.setErrorMessage(error instanceof Error ? error.message : "记录反馈失败");
      }
    },
    [store],
  );

  return {
    applyDebugIdentity,
    bootstrapDebugIdentity,
    loadProfiles,
    selectProfile,
    loadSessionList,
    loadSession,
    handleCreateProfile,
    handleCreateSession,
    resetDebugIdentity,
    handleRenameSession,
    handleDeleteSession,
    handleSendMessage,
    handleRefreshEvents,
    handleDownloadSessionLogs,
    handleSubmitFactForm,
    handlePathAction,
    handleRouteSuggestion,
    handleRetryMessage,
    handleEnterSkill,
    handleExitSkill,
    handleStopGeneration,
    handleClearUserFactsBySource,
    handleMessageFeedback,
  };
}
