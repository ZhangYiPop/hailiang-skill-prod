import type { MessageBlock } from "@/types/messageBlocks";
import type { ContextCompression, ConversationState, RouteSuggestion, SkillIntro, TeamHandoff } from "@/utils/api";
import type { SkillTransition } from "@/utils/api";

export type StreamEventData = Record<string, unknown>;

export type SseV2IntentStep = {
  id: string;
  label: string;
  status: "active" | "completed" | string;
  detail?: string;
};

export type SseV2FormField = {
  fact_key: string;
  label: string;
  input_type: "text" | "single_select" | "multi_select" | string;
  required: boolean;
  placeholder: string;
  example: string;
  options: Array<{ label: string; value: string }>;
  submit_mode: "auto" | "manual" | string;
  scope: string;
  value_type: string;
  max_selections?: number;
};

export type SseV2Form = {
  form_id: string;
  title: string;
  description: string;
  status: "active" | "submitted" | "expired" | string;
  interaction_id: string;
  fields: SseV2FormField[];
};

export type SseV2SkillRoom = {
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
};

export type SseV2PathOption = {
  path_id: string;
  title: string;
  description: string;
  prompt: string;
  enabled: boolean;
};

export type SseV2PathOptions = {
  status: "active" | "selected" | "expired" | string;
  interaction_id: string;
  source_message_id: string;
  options: SseV2PathOption[];
};

export type SseV2State = {
  protocol: "hailiang.sse.v2";
  session_id: string;
  run_id: string;
  seq: number;
  ts: string;
  elapsed_ms: number;
  message_id: string | null;
  status: "streaming" | "completed" | "stopped" | "superseded" | "blocked" | "failed";
  assistant: { content: string; status: string };
  intent: { status: "streaming" | "completed"; steps: SseV2IntentStep[] } | Record<string, never>;
  form: SseV2Form | Record<string, never>;
  path_options: SseV2PathOptions | Record<string, never>;
  skill_rooms: SseV2SkillRoom[];
  team_handoff: TeamHandoff | Record<string, never>;
  expert: {
    mode: "none" | "single" | "team" | string;
    team: { team_id?: string; name?: string; coordinator_expert_id?: string } | Record<string, never>;
    active: { expert_id?: string; name?: string; mention_name?: string; is_coordinator?: boolean } | Record<string, never>;
    transition: {
      status?: "switching" | "completed" | "failed" | string;
      source?: "team_handoff" | "toolbar" | string;
      from_expert_id?: string;
      to_expert_id?: string;
      source_message_id?: string | null;
    } | Record<string, never>;
  };
  skill_transition: Record<string, unknown>;
  session: { active_skill: { skill_id?: string; title?: string; brief?: string; info?: string; description?: string; scene_name?: string } | Record<string, never> };
  risk: { status: "idle" | "checking" | "passed" | "degraded" | "blocked"; stage: string; blocked: boolean; message: string };
  error: { code: string; message: string; upstream_detail: string; retryable: boolean; terminal: boolean };
};

export type ModerationBlockedData = {
  code?: string;
  message?: string;
  stage?: "input" | "stream" | "output" | string;
  case_id?: string | null;
  risk_level?: string;
  labels?: string[];
  provider?: string;
  moderation_mode?: string;
};

export type SkillStatusData = {
  stage: string;
  label: string;
  detail?: string;
  summary?: string;
  source?: string;
};

export type SkillContextData = {
  session_id?: string;
  active_skill?: string | null;
  active_skill_label?: string | null;
  agent_label?: string | null;
  skill_brief?: string | null;
  skill_info?: string | null;
  scene_name?: string | null;
  skill_theme?: string | null;
};

export type SkillIntroData = SkillIntro & {
  session_id?: string;
};

export type FactChange = {
  key: string;
  before: unknown;
  after: unknown;
  scope?: string;
  source?: {
    type?: string;
    source_id?: string;
    source_label?: string;
  };
  updated_at?: string | null;
};

export type FinalMessageData = {
  message_id?: string | null;
  assistant_message: string;
  skill_intro?: SkillIntro | null;
  reasoning?: string;
  message_blocks?: MessageBlock[];
  active_skill?: string | null;
  active_skill_label?: string | null;
  agent_label?: string | null;
  skill_brief?: string | null;
  skill_info?: string | null;
  scene_name?: string | null;
  skill_theme?: string | null;
  conclusion_summary?: string | null;
  context_compression?: ContextCompression | null;
  route_suggestions?: RouteSuggestion[];
  user_facts?: Record<string, unknown>;
  shared_facts?: Record<string, unknown>;
  profile_facts?: Record<string, unknown>;
  session_facts?: Record<string, unknown>;
  effective_facts?: Record<string, unknown>;
  profile_id?: string | null;
  profile_name?: string | null;
  candidate_paths_brief?: Array<Record<string, unknown>>;
  suggested_paths?: string[];
  router_state?: Record<string, unknown>;
  facts_extractor_state?: Record<string, unknown>;
  planner_state?: Record<string, unknown>;
  career_plan_state?: Record<string, unknown>;
  main_planner_state?: Record<string, unknown>;
  conversation_state?: ConversationState;
};

export type MainContentEndData = {
  type?: "main_content_end";
  session_id?: string;
  assistant_message?: string;
  message_id?: string | null;
};

export type SkillActionData = {
  type?: "skill_action";
  session_id?: string;
  message_blocks?: MessageBlock[];
  active_skill?: string | null;
  active_skill_label?: string | null;
  agent_label?: string | null;
  skill_brief?: string | null;
  skill_info?: string | null;
  scene_name?: string | null;
  skill_theme?: string | null;
  conclusion_summary?: string | null;
  context_compression?: ContextCompression | null;
  route_suggestions?: RouteSuggestion[];
  is_final_summary?: boolean;
};

export type SkillLifecycleData = {
  type: "finalizing_started" | "finalized" | string;
  active_skill?: string | null;
  active_skill_label?: string | null;
  agent_label?: string | null;
  skill_brief?: string | null;
  skill_info?: string | null;
  scene_name?: string | null;
  turn_id?: string | null;
  is_final_summary?: boolean;
  conclusion_summary?: string | null;
  context_compression?: ContextCompression | null;
  route_suggestions?: RouteSuggestion[];
};

export type StreamEvent =
  | { event: "state"; data: SseV2State }
  | { event: "done"; data: SseV2State }
  | { event: "run_started"; data: { session_id?: string; run_id?: string } }
  | { event: "skill_context"; data: SkillContextData }
  | { event: "skill_intro"; data: SkillIntroData }
  | { event: "skill_transition"; data: SkillTransition & { message_id?: string } }
  | { event: "skill_status"; data: SkillStatusData }
  | { event: "skill_lifecycle"; data: SkillLifecycleData }
  | { event: "fact_changes"; data: { changes: FactChange[] } }
  | { event: "message_block"; data: MessageBlock }
  | { event: "reasoning_delta"; data: { delta: string } }
  | { event: "final_text_delta"; data: { delta: string } }
  | { event: "main_content_end"; data: MainContentEndData }
  | { event: "skill_action"; data: SkillActionData }
  | { event: "final_message"; data: FinalMessageData }
  | { event: "run_cancelled"; data: { session_id?: string; run_id?: string; message_id?: string; saved_characters?: number; cancelled_at?: string } }
  | { event: "run_completed"; data: { session_id?: string; run_id?: string; status?: string; conversation_state?: ConversationState } }
  | { event: "run_failed"; data: { message?: string } }
  | { event: "moderation_blocked"; data: ModerationBlockedData }
  | { event: string; data: StreamEventData };
