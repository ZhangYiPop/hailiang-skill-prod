export type HarnessJumpCard = {
  id?: string; intent: string; object_type: string; sub_type: string; reason: string;
  title: string; description: string;
  filter: { region: string; grade: string; keyword: string; category: string };
};
export type HarnessRoundExperts = {
  assigned_expert_ids: string[]; running_expert_ids: string[];
  completed_expert_ids: string[]; failed_expert_ids: string[];
  status: "idle" | "assigned" | "running" | "completed" | "failed" | "partial";
};
export type HarnessDispatchExpert = {
  subtask_id: string; node_id?: string | null; expert_id: string; name: string; brief: string; task_brief: string;
  completion_criteria?: string; plan_status?: "assigned"; execution_status?: "assigned" | "running" | "waiting_for_user" | "completed" | "failed";
  status: "assigned" | "running" | "waiting_for_user" | "completed" | "failed";
  depends_on: string[]; depends_on_node_ids?: string[]; can_run_now?: boolean;
};
export type HarnessDispatch = {
  status: "completed" | "pending"; source?: "orchestrator_head" | "main_agent_delegate"; planned_task_count: number;
  planned_expert_count: number; parallel_task_count: number; ready_task_count?: number;
  blocked_task_count?: number; max_parallelism?: number;
  experts: HarnessDispatchExpert[];
};
export type HarnessNodeResult = {
  status?: string; conclusion?: string; findings?: string[];
  limitations?: string[]; unresolved?: string[];
};
export type HarnessNode = {
  id: string; parent_id: string | null; kind: "main" | "expert" | "skill" | "tool";
  title: string; actor?: string; expert_id?: string | null; status: string;
  created_at?: string | null; started_at: string | null; ended_at: string | null;
  error?: string; attempts?: number; stale?: boolean;
  brief?: Record<string, unknown> | string | null; phase?: string; action?: string;
  result?: HarnessNodeResult | null; waiting_on?: string[];
  children_count?: number; completed_children_count?: number;
};
export type HarnessField = {
  key: string; label: string; input_type: string; required: boolean;
  options: Array<{ label: string; value: string }>; min?: number; max?: number;
};
export type HarnessInput = {
  id: string; title: string; fields: HarnessField[]; status: string; version: number; round: number;
  actor?: string; owner_task_id?: string;
  group_id?: string; group_position?: number;
  bindings: Array<{ task_id: string; tool_id: string }>;
  answer?: { answers: Record<string, unknown>; skipped: boolean; skipped_fields?: string[] }; clarification_reason?: string;
};
export type HarnessInputGroup = {
  id: string; status: "active" | "queued" | "completed"; created_at: string;
  request_ids: string[]; active_request_id: string | null;
  total_count: number; completed_count: number; actors: string[];
};
export type HarnessRun = {
  run_id: string; client_run_id?: string | null; assistant_message_id?: string; record_version?: number;
  session_id: string; profile_id: string | null; goal: string; status: string; outcome?: "completed" | "partial" | "failed" | "cancelled" | null; version: number;
  root_task_id: string; nodes: HarnessNode[]; inputs: HarnessInput[];
  input_groups?: HarnessInputGroup[];
  input_queue?: { active_group_id: string | null; queued_group_ids: string[]; active_request_id: string | null; active_group_total: number; active_group_completed: number };
  created_at: string; updated_at: string; result: string; phase?: string; error?: string;
  action?: string; dispatch?: HarnessDispatch | null; synthesis_status?: string; recommendations_status?: string;
  message_status?: string;
  cancellation_reason?: string; superseded_by_run_id?: string;
  input_tokens_estimated?: number; input_tokens_actual?: number | null; input_token_limit?: number;
  completed_count: number; task_count: number; waiting_count: number;
  orchestrated?: boolean;
  round_experts?: HarnessRoundExperts;
  jump_cards?: HarnessJumpCard[];
};
export type HarnessSnapshot = { runtime_mode: string; cursor: number; runs: HarnessRun[] };
export const isHarnessTerminal = (status: string) => ["completed", "failed", "cancelled"].includes(status);
