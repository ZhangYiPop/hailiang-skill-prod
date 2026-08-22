import { create } from "zustand";

import {
  getAutoDetectedApiBaseUrl,
  getRuntimeApiBaseUrl,
  getRuntimeUserId,
  normalizeBaseUrl,
} from "@/config/runtime";
import type {
  CandidatePath,
  ChatMessage,
  DebugIdentity,
  ExpertCatalogItem,
  ExpertTeamCatalogItem,
  SelectedExpertTeam,
  FactMap,
  MessageResponse,
  ProfileSummary,
  SkillCatalogItem,
  SessionListItem,
  SkillEvent,
} from "@/utils/api";
import type { MessageBlock, RuntimeStatusItem } from "@/types/messageBlocks";

const DEBUG_IDENTITY_STORAGE_KEY = "hailiang.debug_identity";

function readStoredDebugIdentity(): DebugIdentity | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.localStorage.getItem(DEBUG_IDENTITY_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const value = JSON.parse(raw) as Partial<DebugIdentity>;
    if (typeof value.user_id !== "string" || !value.user_id.trim() || typeof value.display_name !== "string") {
      return null;
    }
    return {
      user_id: value.user_id,
      display_name: value.display_name,
      profile_id: typeof value.profile_id === "string" ? value.profile_id : "",
      session_id: typeof value.session_id === "string" ? value.session_id : "",
      school_year: typeof value.school_year === "string" ? value.school_year : "",
      grade: typeof value.grade === "string" ? value.grade : "",
    };
  } catch {
    return null;
  }
}

function persistDebugIdentity(value: DebugIdentity | null): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    if (value) {
      window.localStorage.setItem(DEBUG_IDENTITY_STORAGE_KEY, JSON.stringify(value));
    } else {
      window.localStorage.removeItem(DEBUG_IDENTITY_STORAGE_KEY);
    }
  } catch {
    // Local storage can be disabled in private browsing; the in-memory store
    // remains the source for the current page in that case.
  }
}

type FactsSnapshotPayload = {
  userFacts?: FactMap;
  sharedFacts?: FactMap;
  profileFacts?: FactMap;
  sessionFacts?: FactMap;
  effectiveFacts?: FactMap;
};

type ChatStore = {
  apiBaseUrl: string;
  debugIdentity: DebugIdentity | null;
  userId: string;
  currentScenario: string;
  viewMode: "chat" | "debug";
  themeMode: "dark" | "light";
  enableThinking: boolean;
  returnReasoning: boolean;
  profiles: ProfileSummary[];
  activeProfileId: string;
  sessionList: SessionListItem[];
  sessionId: string;
  sessionTitle: string;
  activeProfileName: string;
  activeSkill: string;
  skillCatalog: SkillCatalogItem[];
  expertCatalog: ExpertCatalogItem[];
  expertTeamCatalog: ExpertTeamCatalogItem[];
  activeExpertId: string;
  activeExpertTeam: SelectedExpertTeam | null;
  messages: ChatMessage[];
  candidatePaths: CandidatePath[];
  events: SkillEvent[];
  userFacts: FactMap;
  sharedFacts: FactMap;
  profileFacts: FactMap;
  sessionFacts: FactMap;
  effectiveFacts: FactMap;
  skillStates: Record<string, Record<string, unknown>>;
  lastResponse: MessageResponse | null;
  composerValue: string;
  formDrafts: Record<string, Record<string, unknown>>;
  isCreatingSession: boolean;
  isSending: boolean;
  isLoadingEvents: boolean;
  isLoadingProfiles: boolean;
  isLoadingSessions: boolean;
  isSwitchingSession: boolean;
  errorMessage: string;
  streamAbortController: AbortController | null;
  currentRunId: string;
  isCancellingRun: boolean;
  setApiBaseUrl: (value: string) => void;
  resetApiBaseUrl: () => void;
  setDebugIdentity: (value: DebugIdentity | null) => void;
  setUserId: (value: string) => void;
  setCurrentScenario: (value: string) => void;
  setViewMode: (value: "chat" | "debug") => void;
  setThemeMode: (value: "dark" | "light") => void;
  setEnableThinking: (value: boolean) => void;
  setReturnReasoning: (value: boolean) => void;
  setProfiles: (profiles: ProfileSummary[]) => void;
  setActiveProfileId: (value: string) => void;
  setSessionList: (value: SessionListItem[]) => void;
  setSessionId: (value: string) => void;
  setSessionTitle: (value: string) => void;
  setActiveProfileName: (value: string) => void;
  setActiveSkill: (value: string) => void;
  setSkillCatalog: (value: SkillCatalogItem[]) => void;
  setExpertCatalog: (value: ExpertCatalogItem[]) => void;
  setExpertTeamCatalog: (value: ExpertTeamCatalogItem[]) => void;
  setActiveExpertId: (value: string) => void;
  setActiveExpertTeam: (value: SelectedExpertTeam | null) => void;
  setMessages: (messages: ChatMessage[]) => void;
  appendMessage: (message: ChatMessage) => void;
  updateMessage: (messageId: string, updater: (message: ChatMessage) => ChatMessage) => void;
  createAssistantPlaceholder: () => string;
  appendAssistantDelta: (messageId: string, delta: string) => void;
  appendReasoningDelta: (messageId: string, delta: string) => void;
  completeReasoning: (messageId: string) => void;
  toggleReasoningExpanded: (messageId: string) => void;
  upsertMessageBlock: (messageId: string, block: MessageBlock) => void;
  selectRouteSuggestion: (messageId: string, targetSkillId: string) => void;
  pushRuntimeStatus: (
    messageId: string,
    item: {
      stage: string;
      label: string;
      detail?: string;
      summary?: string;
      source?: string;
      seq?: number;
      elapsedMs?: number;
      timestamp?: string;
    },
  ) => void;
  completeRuntimeStatus: (messageId: string) => void;
  failStreamingMessage: (messageId: string, errorMessage: string) => void;
  setCandidatePaths: (items: CandidatePath[]) => void;
  setEvents: (items: SkillEvent[]) => void;
  setFactsSnapshot: (payload: FactsSnapshotPayload) => void;
  setSkillStates: (value: Record<string, Record<string, unknown>>) => void;
  setLastResponse: (response: MessageResponse | null) => void;
  setComposerValue: (value: string) => void;
  setFormDraftValue: (formId: string, fieldKey: string, value: unknown) => void;
  clearFormDraft: (formId: string) => void;
  setCreatingSession: (value: boolean) => void;
  setSending: (value: boolean) => void;
  setLoadingEvents: (value: boolean) => void;
  setLoadingProfiles: (value: boolean) => void;
  setLoadingSessions: (value: boolean) => void;
  setSwitchingSession: (value: boolean) => void;
  setStreamAbortController: (value: AbortController | null) => void;
  setCurrentRunId: (value: string) => void;
  setCancellingRun: (value: boolean) => void;
  setErrorMessage: (value: string) => void;
  resetConversation: () => void;
};

function makeMessageId(role: "user" | "assistant"): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${role}-${crypto.randomUUID()}`;
  }
  return `${role}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function makeStatusTimelineBlock(
  items: RuntimeStatusItem[] = [],
  summary?: string,
  collapsed: boolean = false,
): MessageBlock {
  return {
    type: "status_timeline",
    payload: {
      title: "推理进度",
      summary,
      collapsed,
      items,
    },
  };
}

function getBlockKey(block: MessageBlock): string {
  if (block.type === "fact_form") {
    const formId = (block.payload as { form_id?: string }).form_id;
    return `fact_form:${formId ?? "default"}`;
  }
  return block.type;
}

function upsertBlocks(blocks: MessageBlock[], nextBlock: MessageBlock): MessageBlock[] {
  const blockKey = getBlockKey(nextBlock);
  const nextBlocks = [...blocks];
  const index = nextBlocks.findIndex((item) => getBlockKey(item) === blockKey);
  if (index >= 0) {
    nextBlocks[index] = nextBlock;
    return nextBlocks;
  }
  return [...nextBlocks, nextBlock];
}

function updateTimelineItems(
  currentItems: RuntimeStatusItem[],
  nextItem?: {
    stage: string;
    label: string;
    detail?: string;
    source?: string;
    seq?: number;
    elapsedMs?: number;
    timestamp?: string;
  },
  failureMessage?: string,
): RuntimeStatusItem[] {
  if (!nextItem) {
    if (failureMessage) {
      return currentItems.map((item, index) => ({
        ...item,
        status: index === currentItems.length - 1 ? "failed" : "completed",
        label: index === currentItems.length - 1 ? failureMessage : item.label,
      }));
    }
    return currentItems.map((item) => ({
      ...item,
      status: item.status === "active" ? "completed" : item.status ?? "completed",
    }));
  }

  const existingIndex = currentItems.findIndex((item) => item.stage === nextItem.stage);
  if (existingIndex >= 0) {
    return currentItems.map((item, index) => {
      if (index < existingIndex) {
        return { ...item, status: "completed" };
      }
      if (index === existingIndex) {
        return {
          ...item,
          label: nextItem.label,
          detail: nextItem.detail,
          source: nextItem.source,
          seq: nextItem.seq,
          elapsedMs: nextItem.elapsedMs,
          timestamp: nextItem.timestamp,
          status: "active",
        };
      }
      return item;
    });
  }

  return [
    ...currentItems.map<RuntimeStatusItem>((item) => ({
      ...item,
      status: item.status === "failed" ? "failed" : "completed",
    })),
    {
      stage: nextItem.stage,
      label: nextItem.label,
      detail: nextItem.detail,
      source: nextItem.source,
      seq: nextItem.seq,
      elapsedMs: nextItem.elapsedMs,
      timestamp: nextItem.timestamp,
      status: "active" as const,
    },
  ];
}

function updateStatusBlock(
  blocks: MessageBlock[],
  nextItem?: {
    stage: string;
    label: string;
    detail?: string;
    summary?: string;
    source?: string;
    seq?: number;
    elapsedMs?: number;
    timestamp?: string;
  },
  failureMessage?: string,
): MessageBlock[] {
  const statusBlock =
    blocks.find((block) => block.type === "status_timeline") ?? makeStatusTimelineBlock();
  const payload = (statusBlock.payload ?? {}) as {
    items?: RuntimeStatusItem[];
    summary?: string;
    collapsed?: boolean;
  };
  const items = updateTimelineItems(payload.items ?? [], nextItem, failureMessage);
  const summary = nextItem?.summary ?? payload.summary;
  const collapsed = nextItem ? false : items.length > 3 || Boolean(payload.collapsed);
  return upsertBlocks(blocks, makeStatusTimelineBlock(items, summary, collapsed));
}

function mergeFactRecordMap(
  current: FactMap,
  next: FactMap | undefined,
): FactMap {
  return next ?? current;
}

function buildRecordMap(payload: FactMap | undefined): FactMap {
  return payload ?? {};
}

export const useChatStore = create<ChatStore>((set) => ({
  apiBaseUrl: getRuntimeApiBaseUrl(),
  debugIdentity: readStoredDebugIdentity(),
  userId: readStoredDebugIdentity()?.user_id ?? getRuntimeUserId(),
  currentScenario: "",
  viewMode: "debug",
  themeMode: "dark",
  enableThinking: false,
  returnReasoning: false,
  profiles: [],
  activeProfileId: "",
  sessionList: [],
  sessionId: "",
  sessionTitle: "",
  activeProfileName: "",
  activeSkill: "",
  skillCatalog: [],
  expertCatalog: [],
  expertTeamCatalog: [],
  activeExpertId: "",
  activeExpertTeam: null,
  messages: [],
  candidatePaths: [],
  events: [],
  userFacts: {},
  sharedFacts: {},
  profileFacts: {},
  sessionFacts: {},
  effectiveFacts: {},
  skillStates: {},
  lastResponse: null,
  composerValue: "",
  formDrafts: {},
  isCreatingSession: false,
  isSending: false,
  isLoadingEvents: false,
  isLoadingProfiles: false,
  isLoadingSessions: false,
  isSwitchingSession: false,
  errorMessage: "",
  streamAbortController: null,
  currentRunId: "",
  isCancellingRun: false,
  setApiBaseUrl: (value) => set({ apiBaseUrl: normalizeBaseUrl(value) }),
  // The button is an explicit local-browser recovery action: ignore a stale
  // Vite-injected hostname and use the current page hostname instead.
  resetApiBaseUrl: () => set({ apiBaseUrl: getAutoDetectedApiBaseUrl() }),
  setDebugIdentity: (value) => {
    persistDebugIdentity(value);
    set({
      debugIdentity: value,
      userId: value?.user_id ?? "",
    });
  },
  setUserId: (value) => set({ userId: value }),
  setCurrentScenario: (value) => set({ currentScenario: value }),
  setViewMode: (value) => set({ viewMode: value }),
  setThemeMode: (value) => set({ themeMode: value }),
  setEnableThinking: (value) => {
    set({
      enableThinking: value,
      returnReasoning: value,
    });
  },
  setReturnReasoning: (value) => {
    set((state) => ({
      returnReasoning: value,
      enableThinking: value ? true : state.enableThinking,
    }));
  },
  setProfiles: (profiles) => set({ profiles }),
  setActiveProfileId: (value) => set({ activeProfileId: value }),
  setSessionList: (value) => set({ sessionList: value }),
  setSessionId: (value) => set({ sessionId: value }),
  setSessionTitle: (value) => set({ sessionTitle: value }),
  setActiveProfileName: (value) => set({ activeProfileName: value }),
  setActiveSkill: (value) => set({ activeSkill: value }),
  setSkillCatalog: (value) => set({ skillCatalog: value }),
  setExpertCatalog: (value) => set({ expertCatalog: value }),
  setExpertTeamCatalog: (value) => set({ expertTeamCatalog: value }),
  setActiveExpertId: (value) => set({ activeExpertId: value }),
  setActiveExpertTeam: (value) => set({ activeExpertTeam: value }),
  setMessages: (messages) => set({ messages }),
  appendMessage: (message) => set((state) => ({ messages: [...state.messages, message] })),
  updateMessage: (messageId, updater) =>
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === messageId ? updater(message) : message,
      ),
    })),
  createAssistantPlaceholder: () => {
    const id = makeMessageId("assistant");
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id,
          role: "assistant",
          content: "",
          createdAt: new Date().toISOString(),
          blocks: [makeStatusTimelineBlock()],
          streamingStatus: "streaming",
          reasoningContent: "",
          reasoningStatus: "idle",
          reasoningExpanded: true,
        },
      ],
    }));
    return id;
  },
  appendAssistantDelta: (messageId, delta) =>
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === messageId
          ? {
              ...message,
              content: `${message.content}${delta}`,
              streamingStatus: "streaming",
            }
          : message,
      ),
    })),
  appendReasoningDelta: (messageId, delta) =>
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === messageId
          ? {
              ...message,
              reasoningContent: `${message.reasoningContent ?? ""}${delta}`,
              reasoningStatus: "streaming",
              reasoningExpanded: true,
              streamingStatus: "streaming",
            }
          : message,
      ),
    })),
  completeReasoning: (messageId) =>
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === messageId && message.reasoningContent
          ? {
              ...message,
              reasoningStatus: "completed",
              reasoningExpanded: false,
            }
          : message,
      ),
    })),
  toggleReasoningExpanded: (messageId) =>
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === messageId
          ? {
              ...message,
              reasoningExpanded: !message.reasoningExpanded,
            }
          : message,
      ),
    })),
  upsertMessageBlock: (messageId, block) =>
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === messageId
          ? {
              ...message,
              blocks: upsertBlocks(message.blocks, block),
            }
          : message,
      ),
    })),
  selectRouteSuggestion: (messageId, targetSkillId) =>
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === messageId
          ? {
              ...message,
              selectedRouteSuggestion: targetSkillId,
            }
          : message,
      ),
    })),
  pushRuntimeStatus: (messageId, item) =>
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === messageId
          ? {
              ...message,
              blocks: updateStatusBlock(message.blocks, item),
              streamingStatus: "streaming",
            }
          : message,
      ),
    })),
  completeRuntimeStatus: (messageId) =>
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === messageId
          ? {
              ...message,
              blocks: updateStatusBlock(message.blocks),
              streamingStatus: "completed",
            }
          : message,
      ),
    })),
  failStreamingMessage: (messageId, errorMessage) =>
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === messageId
          ? {
              ...message,
              errorMessage,
              blocks: updateStatusBlock(message.blocks, undefined, errorMessage),
              streamingStatus: "failed",
            }
          : message,
      ),
    })),
  setCandidatePaths: (items) => set({ candidatePaths: items }),
  setEvents: (items) => set({ events: items }),
  setFactsSnapshot: (payload) =>
    set((state) => ({
      userFacts: mergeFactRecordMap(state.userFacts, payload.userFacts ?? payload.sharedFacts),
      sharedFacts: mergeFactRecordMap(state.sharedFacts, payload.sharedFacts ?? payload.userFacts),
      profileFacts: mergeFactRecordMap(state.profileFacts, payload.profileFacts),
      sessionFacts: mergeFactRecordMap(state.sessionFacts, payload.sessionFacts),
      effectiveFacts: mergeFactRecordMap(state.effectiveFacts, payload.effectiveFacts),
    })),
  setSkillStates: (value) => set({ skillStates: value }),
  setLastResponse: (response) => set({ lastResponse: response }),
  setComposerValue: (value) => set({ composerValue: value }),
  setFormDraftValue: (formId, fieldKey, value) =>
    set((state) => ({
      formDrafts: {
        ...state.formDrafts,
        [formId]: {
          ...(state.formDrafts[formId] ?? {}),
          [fieldKey]: value,
        },
      },
    })),
  clearFormDraft: (formId) =>
    set((state) => {
      const nextDrafts = { ...state.formDrafts };
      delete nextDrafts[formId];
      return { formDrafts: nextDrafts };
    }),
  setCreatingSession: (value) => set({ isCreatingSession: value }),
  setSending: (value) => set({ isSending: value }),
  setLoadingEvents: (value) => set({ isLoadingEvents: value }),
  setLoadingProfiles: (value) => set({ isLoadingProfiles: value }),
  setLoadingSessions: (value) => set({ isLoadingSessions: value }),
  setSwitchingSession: (value) => set({ isSwitchingSession: value }),
  setStreamAbortController: (value) => set({ streamAbortController: value }),
  setCurrentRunId: (value) => set({ currentRunId: value }),
  setCancellingRun: (value) => set({ isCancellingRun: value }),
  setErrorMessage: (value) => set({ errorMessage: value }),
  resetConversation: () =>
    set({
      sessionId: "",
      sessionTitle: "",
      activeSkill: "",
      activeExpertId: "",
      activeExpertTeam: null,
      currentScenario: "",
      messages: [],
      candidatePaths: [],
      events: [],
      userFacts: buildRecordMap(undefined),
      sharedFacts: buildRecordMap(undefined),
      profileFacts: buildRecordMap(undefined),
      sessionFacts: buildRecordMap(undefined),
      effectiveFacts: buildRecordMap(undefined),
      skillStates: {},
      lastResponse: null,
      composerValue: "",
      formDrafts: {},
      streamAbortController: null,
      currentRunId: "",
      isCancellingRun: false,
      errorMessage: "",
    }),
}));
