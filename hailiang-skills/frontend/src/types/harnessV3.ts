export type HarnessV3Field = {
  key: string;
  fact_key?: string | null;
  label: string;
  input_type: "text" | "single_select" | "multi_select" | "integer" | "number";
  required: boolean;
  options: Array<{ label: string; value: string }>;
  min?: number | null;
  max?: number | null;
};

export type HarnessV3InputRequest = {
  id: string;
  title: string;
  fields: HarnessV3Field[];
  status: "pending" | "queued" | "answered" | "skipped" | "expired" | string;
  /** `request_version` is the public wire name; `version` is kept for old recordings. */
  request_version?: number;
  version?: number;
  round: number;
  actor?: string;
  owner_node_id?: string;
  clarification_reason?: string;
  answer?: { answers: Record<string, unknown>; skipped: boolean };
};

export type HarnessV3TaskResult = {
  conclusion: string;
  findings?: string[];
  limitations?: string[];
  unresolved?: string[];
};

export type HarnessV3Node = {
  id: string;
  parent_id: string | null;
  kind: "main" | "expert" | "skill" | "tool";
  title: string;
  actor?: string;
  brief?: string;
  agent_brief?: string | null;
  status: string;
  phase?: string;
  action?: string;
  waiting_on?: string[];
  children_count?: number;
  completed_children_count?: number;
  attempts?: number;
  error?: string | null;
  outcome?: "completed" | "partial" | "failed" | null;
  result?: HarnessV3TaskResult | null;
};

export type HarnessV3Dispatch = {
  status?: string;
  planned_task_count?: number;
  planned_expert_count?: number;
  parallel_task_count?: number;
  experts?: Array<{
    node_id: string;
    expert_id?: string;
    name: string;
    brief?: string;
    agent_brief?: string | null;
    status: string;
    depends_on?: string[];
  }>;
};

export type HarnessV3State = {
  assistant_message_id: string;
  record_version: number;
  session_id: string;
  profile_id: string | null;
  expert_team_id: string;
  goal: string;
  status: string;
  outcome?: "completed" | "partial" | "failed" | "cancelled" | null;
  phase?: string;
  action?: string;
  dispatch?: HarnessV3Dispatch;
  nodes: HarnessV3Node[];
  inputs: HarnessV3InputRequest[];
  input_queue?: {
    active_request_id?: string | null;
    queued_group_ids?: string[];
  };
  synthesis_status?: string | null;
  recommendations_status?: string;
  jump_cards?: Array<Record<string, unknown>>;
  result: string;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
  completed_count?: number;
  task_count?: number;
  waiting_count?: number;
  safety?: { replacement_applied?: boolean; replacement_source?: string | null };
};

export type HarnessV3Frame = {
  protocol: "hailiang.sse.v3";
  session_id: string;
  run_id: string;
  assistant_message_id: string;
  expert_team_id: string;
  record_version: number;
  cursor: number;
  reason: string;
  stream_status: string;
  outcome?: HarnessV3State["outcome"];
  profile_id?: string | null;
  profile_name?: string | null;
  context_scope?: "profile" | "unbound" | string;
  context_label?: string;
  context_notice?: { type?: string; text?: string } | Record<string, never>;
  profile_context_status?: string;
  superseded_assistant_message_id?: string;
  persistence: {
    terminal: boolean;
    should_store_snapshot?: boolean;
    should_save_history?: boolean;
    replace_staged?: boolean;
    should_discard_staged?: boolean;
    record_kind?: string;
  };
  state: HarnessV3State;
};

export type HarnessV3EventName = "state" | "done_suspended" | "done_timeout" | "done";

export type HarnessV3Event = { event: HarnessV3EventName; data: HarnessV3Frame };
