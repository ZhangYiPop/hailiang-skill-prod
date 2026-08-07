import type { MessageBlock } from "@/types/messageBlocks";
import type { SseV2PathOptions } from "@/types/streamEvents";

export type MessageRole = "user" | "assistant";

export type MessageInteractionState = {
  kind: "fact_form" | "route_suggestions" | "path_actions" | string;
  status: "active" | "submitted" | "selected" | "expired" | string;
  updated_at?: string;
  submitted_fact_keys?: string[];
  selected_target_skill_id?: string;
};

export type SkillTransition = {
  action: "enter" | "exit";
  from_skill_id: string;
  to_skill_id: string;
  source: "toolbar" | "route_suggestion" | "exit_button";
  created_at: string;
  context_mode?: "message_context" | "facts_only";
  context_source_message_id?: string;
  context_reset?: boolean;
  skill?: {
    skill_id: string;
    name: string;
    label: string;
    brief?: string;
    info?: string;
    description?: string;
    scene_name?: string;
    skill_theme?: string;
  };
  synthetic_user_message?: {
    message_id: string;
    content: string;
    created_at: string;
  };
};

export type MessagePresentation = {
  assistant: { content: string; status: string };
  intent: { status: string; steps: Array<{ id: string; label: string; status: string; detail?: string }> } | Record<string, never>;
  form: {
    form_id: string;
    title: string;
    description: string;
    status: string;
    interaction_id: string;
    fields: Array<{
      fact_key: string;
      label: string;
      input_type: string;
      required: boolean;
      placeholder: string;
      example: string;
      options: Array<{ label: string; value: string }>;
      submit_mode: string;
      scope: string;
      value_type: string;
      max_selections?: number;
    }>;
  } | Record<string, never>;
  path_options: SseV2PathOptions | Record<string, never>;
  skill_rooms: Array<{
    skill_id: string;
    title: string;
    brief?: string;
    info?: string;
    reason?: string;
    description: string;
    status: "enterable" | "entered";
    enabled: boolean;
    source_message_id: string;
    source_interaction_id: string;
  }>;
  skill_transition: Record<string, unknown>;
  session: { active_skill: { skill_id?: string; title?: string; brief?: string; info?: string; description?: string; scene_name?: string } | Record<string, never> };
  risk: { status: string; stage: string; blocked: boolean; message: string };
  error: { code: string; message: string; upstream_detail: string; retryable: boolean; terminal: boolean };
};

export type ChatMessage = {
  id: string;
  messageId?: string;
  role: MessageRole;
  content: string;
  createdAt: string;
  blocks: MessageBlock[];
  retryRequest?: ChatRetryRequest;
  skillId?: string;
  skillName?: string;
  agentLabel?: string;
  skillBrief?: string;
  skillInfo?: string;
  sceneName?: string;
  themeKey?: string;
  conclusionSummary?: string;
  contextCompression?: ContextCompression;
  routeSuggestions?: RouteSuggestion[];
  selectedRouteSuggestion?: string;
  interactionStates?: Record<string, MessageInteractionState>;
  reasoningContent?: string;
  reasoningStatus?: "idle" | "streaming" | "completed";
  reasoningExpanded?: boolean;
  streamingStatus?: "idle" | "streaming" | "completed" | "failed";
  generationStatus?: "cancelled" | "completed" | string;
  errorMessage?: string;
  feedback?: "like" | "dislike" | null;
  feedbackUpdatedAt?: string;
  messageType?: "skill_intro" | string;
  skillIntro?: SkillIntro;
  skillTransition?: SkillTransition;
  presentation?: MessagePresentation;
};

export type ChatRetryRequest = {
  content: string;
  enableThinking?: boolean;
  requestedTargetSkillId?: string;
  handoffContext?: Record<string, unknown>;
};

export type ContextCompression = {
  conversation_summary?: string;
  facts_snapshot?: Record<string, unknown>;
  facts_delta?: Array<Record<string, unknown>>;
  skill_facts?: Record<string, unknown>;
  handoff_notes?: string;
  source_skill_id?: string;
};

export type RouteSuggestion = {
  target_skill_id: string;
  skill_id?: string;
  skill_name?: string;
  agent_label: string;
  brief?: string;
  info?: string;
  description?: string;
  scene_name?: string;
  skill_theme?: string;
  reason: string;
  confidence: number;
  handoff_notes?: string;
  suggestion_source?: "final_summary" | "llm_reply_analysis" | "strong_format_fallback" | string;
};

export type SkillIntro = {
  skill_id: string;
  skill_label: string;
  brief?: string;
  info?: string;
  description: string;
  scene_name?: string;
  skill_theme?: string;
};

export type SkillCatalogItem = {
  skill_id: string;
  label: string;
  description: string;
  brief?: string;
  info?: string;
  scene_name?: string;
  skill_theme?: string;
};

export type CandidatePath = {
  path_id?: string;
  primary_category?: string;
  match_score?: number;
  eligibility_status?: string;
  feasibility_status?: string;
  feasibility_label?: string;
  risk_level?: string;
  missing_slots?: string[];
  blocking_reasons?: string[];
  reasons?: string[];
  description?: string;
  sheet_group?: string;
  target_users?: string;
};

export type FactRecordPayload = {
  value: unknown;
  confidence?: number;
  source_skill?: string;
  source_type?: string;
  source_id?: string | null;
  source_label?: string | null;
  scope?: string;
  source_turn_id?: string | null;
  updated_at?: string;
  provenance?: Record<string, unknown> | null;
};

export type FactMap = Record<string, FactRecordPayload>;

export type DebugIdentity = {
  user_id: string;
  display_name: string;
  profile_id: string;
  session_id: string;
  school_year: string;
  grade: string;
};

export type ProfileSummary = {
  profile_id: string;
  name: string;
  created_at?: string;
  updated_at?: string;
  is_default?: boolean;
  shared_facts_initialized?: boolean;
};

export type SessionListItem = {
  session_id: string;
  user_id: string;
  profile_id?: string | null;
  profile_name?: string | null;
  title?: string | null;
  message_count: number;
  created_at?: string | null;
  updated_at?: string | null;
  active_skill?: string | null;
};

export type AdmissionMatchBrief = {
  region_variant?: string;
  tier_name?: string;
  subject_group?: string;
  score_range?: {
    min_score?: number;
    max_score?: number;
  };
  sample_schools?: string[];
  recommended_paths?: string[];
};

export type AdmissionState = {
  score?: number | null;
  province?: string | null;
  subject_group?: string | null;
  matched_count?: number;
  matched_items_brief?: AdmissionMatchBrief[];
  candidate_count?: number;
};

export type SchoolIntroState = {
  matched_school_names?: string[];
  matched_count?: number;
};

export type AssetSupport = {
  available_assets?: Array<{
    path?: string;
    title?: string;
    count?: number | null;
    supports?: string[];
    enabled?: boolean;
  }>;
  supported_dimensions?: string[];
  dynamic_supported_dimensions?: string[];
  dynamic_unavailable_dimensions?: string[];
  policy?: {
    must_ground_on_assets?: boolean;
    fallback_message?: string;
    base_rule_first_missing_facts?: boolean;
  };
};

export type MessageResponse = {
  message_id?: string | null;
  assistant_message: string;
  skill_intro?: SkillIntro | null;
  reasoning?: string;
  message_blocks?: MessageBlock[];
  active_skill: string | null;
  active_skill_label?: string | null;
  agent_label?: string | null;
  skill_brief?: string | null;
  skill_info?: string | null;
  scene_name?: string | null;
  skill_theme?: string | null;
  conclusion_summary?: string | null;
  context_compression?: ContextCompression | null;
  route_suggestions?: RouteSuggestion[];
  asset_support?: AssetSupport;
  session_log_dir?: string;
  candidate_paths_brief: CandidatePath[];
  suggested_paths: string[];
  facts_updated: string[];
  risk_alerts: string[];
  user_facts: FactMap;
  shared_facts?: FactMap;
  profile_facts?: FactMap;
  session_facts: FactMap;
  effective_facts: FactMap;
  profile_id?: string | null;
  profile_name?: string | null;
  router_state: Record<string, unknown>;
  facts_extractor_state: Record<string, unknown>;
  planner_state: Record<string, unknown>;
  career_plan_state?: Record<string, unknown>;
  main_planner_state?: Record<string, unknown>;
  admission_state: AdmissionState;
  school_intro_state: SchoolIntroState;
  ranking_snapshot: Record<string, unknown>;
  conversation_state?: ConversationState;
};

export type SessionResponse = {
  session_id: string;
  user_id: string;
  user_display_name?: string;
  profile_id?: string | null;
  profile_name?: string | null;
  title?: string | null;
  recent_session_summary?: string | null;
  session_log_dir?: string;
  facts: FactMap;
  user_facts?: FactMap;
  shared_facts?: FactMap;
  profile_facts?: FactMap;
  session_facts?: FactMap;
  effective_facts?: FactMap;
  candidate_paths: CandidatePath[];
  message_count: number;
  skill_states: Record<string, Record<string, unknown>>;
  conversation_state?: ConversationState;
  profile_school_facts?: Array<{ school_year: string; grade: string }>;
  skill_display?: {
    skill_id?: string;
    skill_name?: string;
    active_skill_label?: string;
    agent_label?: string;
    skill_brief?: string;
    skill_info?: string;
    brief?: string;
    info?: string;
    scene_name?: string;
    skill_theme?: string;
    theme_key?: string;
  };
};

export type SessionContextMessage = {
  message_id?: string;
  role: MessageRole;
  content: string;
  created_at?: string;
  blocks?: MessageBlock[];
  metadata?: {
    blocks?: MessageBlock[];
    skill_id?: string;
    skill_name?: string;
    agent_label?: string;
    skill_brief?: string;
    skill_info?: string;
    brief?: string;
    info?: string;
    scene_name?: string;
    theme_key?: string;
    skill_theme?: string;
    conclusion_summary?: string;
    context_compression?: ContextCompression;
    route_suggestions?: RouteSuggestion[];
    selected_route_suggestion?: string;
    feedback?: "like" | "dislike" | null;
    feedback_updated_at?: string;
    message_type?: string;
    skill_intro?: SkillIntro;
    hidden?: boolean;
    interaction_states?: Record<string, MessageInteractionState>;
    skill_transition?: SkillTransition;
    generation_status?: string;
  };
  skill_id?: string;
  skill_name?: string;
  agent_label?: string;
  skill_brief?: string;
  skill_info?: string;
  brief?: string;
  info?: string;
  scene_name?: string;
  theme_key?: string;
  conclusion_summary?: string;
  context_compression?: ContextCompression;
  route_suggestions?: RouteSuggestion[];
  selected_route_suggestion?: string;
  feedback?: "like" | "dislike" | null;
  feedback_updated_at?: string;
  message_type?: string;
  skill_intro?: SkillIntro;
  interaction_states?: Record<string, MessageInteractionState>;
  skill_transition?: SkillTransition;
  generation_status?: string;
  presentation?: MessagePresentation;
};

export type SessionContextResponse = {
  session_id: string;
  user_id: string;
  user_display_name?: string;
  profile_id?: string | null;
  profile_name?: string | null;
  title?: string | null;
  messages: SessionContextMessage[];
  user_facts: FactMap;
  shared_facts?: FactMap;
  profile_facts?: FactMap;
  session_facts: FactMap;
  effective_facts: FactMap;
  skill_states: Record<string, Record<string, unknown>>;
  interaction_state: Record<string, unknown>;
  candidate_paths: CandidatePath[];
  event_count: number;
  conversation_state?: ConversationState;
  profile_school_facts?: Array<{ school_year: string; grade: string }>;
};

export type SkillEvent = {
  event_id: string;
  event_type: string;
  created_at: string;
  payload: Record<string, unknown>;
};

export type EventsResponse = {
  events: SkillEvent[];
};

export type FactSourcePayload = {
  type: string;
  source_id?: string | null;
  source_label?: string | null;
  turn_id?: string | null;
};

export type FactUpdateItem = {
  key: string;
  value: unknown;
};

export type FactWritePayload = {
  scope?: "user" | "session" | string;
  source?: FactSourcePayload;
  updates: FactUpdateItem[];
};

export type FactWriteResponse = {
  user_id?: string;
  session_id?: string;
  profile_id?: string;
  applied_updates?: FactUpdateItem[];
  fact_changes: Array<Record<string, unknown>>;
  current_facts: FactMap | SessionContextFactsPayload;
  sources?: FactSourceSummary[];
  shared_facts?: FactMap;
};

export type SessionContextFactsPayload = {
  shared_facts?: FactMap;
  profile_facts?: FactMap;
  user_facts: FactMap;
  session_facts: FactMap;
  effective_facts: FactMap;
};

export type FactSourceSummary = {
  type?: string | null;
  source_id?: string | null;
  source_label?: string | null;
  fact_count: number;
  fact_keys: string[];
};

export type UserFactsResponse = {
  user_id: string;
  facts: FactMap;
  sources?: FactSourceSummary[];
  shared_facts?: FactMap;
};

export type ProfileFactsResponse = {
  user_id: string;
  profile_id: string;
  facts: FactMap;
  sources?: FactSourceSummary[];
  shared_facts?: FactMap;
  profile_facts?: FactMap;
  effective_facts?: FactMap;
};

export type ProfilesResponse = {
  user_id: string;
  profiles: ProfileSummary[];
};

export type ProfileResponse = {
  user_id: string;
  profile: ProfileSummary;
  facts?: FactMap;
};

export type CreateProfileRequest = {
  name: string;
  initialize_from_shared_facts?: boolean;
};

export type UpdateProfileRequest = {
  name?: string;
  is_default?: boolean;
};

export type SessionListResponse = {
  sessions: SessionListItem[];
};

export type CreateSessionRequest = {
  session_id: string;
  user_id: string;
  profile_id: string;
  parent_name?: string | null;
  profile_school_facts: Array<{ school_year: string; grade: string }>;
};

export type CreateSessionResponse = {
  status: "created" | "resumed";
  session_id: string;
  user_id: string;
  user_display_name?: string;
  profile_id: string;
  title?: string | null;
  opening_message?: null;
  recent_session_summary?: string | null;
  message_id?: string | null;
  messages: SessionContextMessage[];
  profile_school_facts: Array<{ school_year: string; grade: string }>;
  profile_facts: FactMap;
  shared_facts: FactMap;
  session_facts: FactMap;
  effective_facts: FactMap;
  interaction_state: Record<string, unknown>;
  skill_states: Record<string, Record<string, unknown>>;
  conversation_state: ConversationState;
};

export type ConversationState = {
  security?: Record<string, unknown>;
};

export type UpdateSessionTitleRequest = {
  title: string;
};

export type FactFormFieldConfig = {
  fact_key: string;
  label: string;
  input_type: "text" | "single_select" | "multi_select" | string;
  placeholder?: string;
  example?: string;
  options?: Array<{
    label: string;
    value: string;
  }>;
  submit_mode?: "auto" | "manual" | string;
  scope: "user" | "session" | string;
  value_type?: string;
};

export type FactFormConfigResponse = {
  fields: FactFormFieldConfig[];
};

export async function updateMessageInteraction(
  baseUrl: string,
  sessionId: string,
  messageId: string,
  interactionId: string,
  payload: { status: "submitted"; submitted_fact_keys: string[] },
): Promise<{ interaction_id: string; state: MessageInteractionState }> {
  const response = await fetchWithRetry(
    `${baseUrl}/api/v1/sessions/${sessionId}/messages/${encodeURIComponent(messageId)}/interactions/${encodeURIComponent(interactionId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  return parseResponse(response);
}

const DEFAULT_RETRY_COUNT = 3;
const DEFAULT_RETRY_DELAY_MS = 500;
const DEFAULT_RETRYABLE_STATUS = [408, 429, 500, 502, 503, 504];
const DEFAULT_TIMEOUT_MS = 120000;

type RetryOptions = {
  retryCount?: number;
  retryDelayMs?: number;
  retryableStatus?: number[];
  timeoutMs?: number;
};

function parseNumberEnv(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function getRetryConfig(overrides?: RetryOptions) {
  return {
    retryCount:
      overrides?.retryCount ??
      parseNumberEnv(import.meta.env.VITE_API_RETRY_COUNT, DEFAULT_RETRY_COUNT),
    retryDelayMs:
      overrides?.retryDelayMs ??
      parseNumberEnv(import.meta.env.VITE_API_RETRY_DELAY_MS, DEFAULT_RETRY_DELAY_MS),
    retryableStatus: overrides?.retryableStatus ?? DEFAULT_RETRYABLE_STATUS,
    timeoutMs:
      overrides?.timeoutMs ??
      parseNumberEnv(import.meta.env.VITE_API_TIMEOUT_MS, DEFAULT_TIMEOUT_MS),
  };
}

function shouldRetryResponse(response: Response, retryableStatus: number[]): boolean {
  return retryableStatus.includes(response.status);
}

function wait(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function fetchWithRetry(
  input: RequestInfo | URL,
  init?: RequestInit,
  retryOptions?: RetryOptions,
): Promise<Response> {
  const { retryCount, retryDelayMs, retryableStatus, timeoutMs } = getRetryConfig(retryOptions);
  let lastError: unknown;

  for (let attempt = 0; attempt <= retryCount; attempt += 1) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const headers = new Headers(init?.headers);
      const response = await fetch(input, { ...init, headers, signal: controller.signal });
      window.clearTimeout(timeoutId);
      if (response.ok || !shouldRetryResponse(response, retryableStatus) || attempt === retryCount) {
        return response;
      }
    } catch (error) {
      window.clearTimeout(timeoutId);
      lastError = error;
      if (attempt === retryCount) {
        break;
      }
    }

    const delay = retryDelayMs * (attempt + 1);
    await wait(delay);
  }

  if (lastError instanceof DOMException && lastError.name === "AbortError") {
    throw new Error(`请求超时，${timeoutMs}ms 内未收到响应`);
  }
  if (lastError instanceof Error) {
    throw new Error(`请求异常，已重试 ${retryCount} 次：${lastError.message}`);
  }
  throw new Error(`请求异常，已重试 ${retryCount} 次`);
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `请求失败: ${response.status}`);
  }
  return (await response.json()) as T;
}

export function normalizeMessageBlocks(blocks: unknown): MessageBlock[] {
  if (!Array.isArray(blocks)) {
    return [];
  }
  return blocks
    .map((item) => {
      if (!item || typeof item !== "object") {
        return null;
      }
      const block = item as { type?: unknown; payload?: unknown };
      if (typeof block.type !== "string") {
        return null;
      }
      return {
        type: block.type,
        payload:
          block.payload && typeof block.payload === "object"
            ? (block.payload as Record<string, unknown>)
            : {},
      } as MessageBlock;
    })
    .filter((item): item is MessageBlock => Boolean(item));
}

export async function listRuntimeSkills(baseUrl: string, grade = ""): Promise<SkillCatalogItem[]> {
  const query = grade.trim() ? `?grade=${encodeURIComponent(grade.trim())}` : "";
  const response = await fetchWithRetry(`${baseUrl}/api/v1/skills${query}`, { method: "GET" });
  const payload = await parseResponse<{ skills?: SkillCatalogItem[] }>(response);
  return Array.isArray(payload.skills) ? payload.skills : [];
}

export async function createSession(
  baseUrl: string,
  payload: CreateSessionRequest,
): Promise<CreateSessionResponse> {
  const response = await fetchWithRetry(
    `${baseUrl}/api/v1/sessions`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    },
    {
      retryCount: 1,
    },
  );
  return parseResponse<CreateSessionResponse>(response);
}


export async function sendMessage(
  baseUrl: string,
  sessionId: string,
  payload: {
    content: string;
    user_id: string;
    enable_thinking?: boolean;
    return_reasoning?: boolean;
    requested_target_skill_id?: string;
    handoff_context?: Record<string, unknown>;
  },
): Promise<MessageResponse> {
  const response = await fetchWithRetry(
    `${baseUrl}/api/v1/sessions/${sessionId}/messages`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    },
    {
      retryCount: 3,
    },
  );
  return parseResponse<MessageResponse>(response);
}

export async function getSession(
  baseUrl: string,
  sessionId: string,
): Promise<SessionResponse> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/sessions/${sessionId}`);
  return parseResponse<SessionResponse>(response);
}

export async function updateMessageFeedback(
  baseUrl: string,
  sessionId: string,
  messageId: string,
  feedback: "like" | "dislike" | null,
): Promise<{ message_id: string; feedback: "like" | "dislike" | null; feedback_updated_at: string }> {
  const response = await fetchWithRetry(
    `${baseUrl}/api/v1/sessions/${sessionId}/messages/${encodeURIComponent(messageId)}/feedback`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ feedback }),
    },
    { retryCount: 1 },
  );
  return parseResponse(response);
}

export async function getSessionContext(
  baseUrl: string,
  sessionId: string,
): Promise<SessionContextResponse> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/sessions/${sessionId}/context`);
  return parseResponse<SessionContextResponse>(response);
}

export async function getEvents(
  baseUrl: string,
  sessionId: string,
): Promise<EventsResponse> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/sessions/${sessionId}/events`);
  return parseResponse<EventsResponse>(response);
}

function filenameFromContentDisposition(value: string | null, fallback: string): string {
  if (!value) {
    return fallback;
  }
  const utf8Match = value.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    return decodeURIComponent(utf8Match[1].replace(/"/g, ""));
  }
  const asciiMatch = value.match(/filename="?([^";]+)"?/i);
  return asciiMatch?.[1] ?? fallback;
}

export async function downloadSessionLogs(
  baseUrl: string,
  sessionId: string,
): Promise<{ blob: Blob; filename: string }> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/sessions/${sessionId}/logs/download`, undefined, {
    retryCount: 0,
  });
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `下载日志失败: ${response.status}`);
  }
  const fallback = `${sessionId}-logs.zip`;
  return {
    blob: await response.blob(),
    filename: filenameFromContentDisposition(response.headers.get("Content-Disposition"), fallback),
  };
}

export async function upsertUserFacts(
  baseUrl: string,
  userId: string,
  payload: FactWritePayload,
): Promise<FactWriteResponse> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/users/${userId}/facts:batch-upsert`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return parseResponse<FactWriteResponse>(response);
}

export async function getUserFacts(
  baseUrl: string,
  userId: string,
): Promise<UserFactsResponse> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/users/${userId}/facts`);
  return parseResponse<UserFactsResponse>(response);
}

export async function getProfileFacts(
  baseUrl: string,
  userId: string,
  profileId: string,
): Promise<ProfileFactsResponse> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/users/${userId}/profiles/${profileId}/facts`);
  return parseResponse<ProfileFactsResponse>(response);
}

export async function upsertProfileFacts(
  baseUrl: string,
  userId: string,
  profileId: string,
  payload: FactWritePayload,
): Promise<FactWriteResponse> {
  const response = await fetchWithRetry(
    `${baseUrl}/api/v1/users/${userId}/profiles/${profileId}/facts:batch-upsert`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    },
  );
  return parseResponse<FactWriteResponse>(response);
}

export async function resetProfileFacts(
  baseUrl: string,
  userId: string,
  profileId: string,
  payload: {
    scope?: string;
    source?: FactSourcePayload;
    fact_keys: string[];
  },
): Promise<FactWriteResponse> {
  const response = await fetchWithRetry(
    `${baseUrl}/api/v1/users/${userId}/profiles/${profileId}/facts:reset`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    },
  );
  return parseResponse<FactWriteResponse>(response);
}

export async function upsertSessionFacts(
  baseUrl: string,
  sessionId: string,
  payload: FactWritePayload,
): Promise<FactWriteResponse> {
  const response = await fetchWithRetry(
    `${baseUrl}/api/v1/sessions/${sessionId}/facts:batch-upsert`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    },
  );
  return parseResponse<FactWriteResponse>(response);
}

export async function clearUserFactsBySource(
  baseUrl: string,
  userId: string,
  payload: {
    source: FactSourcePayload;
  },
): Promise<FactWriteResponse> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/users/${userId}/facts:clear-by-source`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return parseResponse<FactWriteResponse>(response);
}

export async function getFactFormConfig(baseUrl: string): Promise<FactFormConfigResponse> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/facts/form-config`);
  return parseResponse<FactFormConfigResponse>(response);
}

export async function listProfiles(
  baseUrl: string,
  userId: string,
): Promise<ProfilesResponse> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/users/${userId}/profiles`);
  return parseResponse<ProfilesResponse>(response);
}

export async function createProfile(
  baseUrl: string,
  userId: string,
  payload: CreateProfileRequest,
): Promise<ProfileResponse> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/users/${userId}/profiles`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return parseResponse<ProfileResponse>(response);
}

export async function updateProfile(
  baseUrl: string,
  userId: string,
  profileId: string,
  payload: UpdateProfileRequest,
): Promise<ProfileResponse> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/users/${userId}/profiles/${profileId}`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return parseResponse<ProfileResponse>(response);
}

export async function listProfileSessions(
  baseUrl: string,
  userId: string,
  profileId: string,
): Promise<SessionListResponse> {
  const response = await fetchWithRetry(
    `${baseUrl}/api/v1/users/${userId}/profiles/${profileId}/sessions`,
  );
  return parseResponse<SessionListResponse>(response);
}

export async function updateSessionTitle(
  baseUrl: string,
  sessionId: string,
  payload: UpdateSessionTitleRequest,
): Promise<{ session_id: string; title: string }> {
  const response = await fetchWithRetry(`${baseUrl}/api/v1/sessions/${sessionId}`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return parseResponse<{ session_id: string; title: string }>(response);
}
