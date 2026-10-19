export type HarnessNode = {
  id: string; parent_id: string | null; kind: "main" | "expert" | "skill" | "tool";
  title: string; actor?: string; status: string;
  created_at?: string | null; started_at: string | null; ended_at: string | null;
  error?: string; attempts?: number; stale?: boolean;
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
  run_id: string; session_id: string; profile_id: string | null; goal: string; status: string; version: number;
  root_task_id: string; nodes: HarnessNode[]; inputs: HarnessInput[];
  input_groups?: HarnessInputGroup[];
  input_queue?: { active_group_id: string | null; queued_group_ids: string[]; active_request_id: string | null; active_group_total: number; active_group_completed: number };
  created_at: string; updated_at: string; result: string; phase?: string; error?: string;
  message_status?: string;
  outcome?: "completed" | "partial" | "failed" | "cancelled" | null;
  cancellation_reason?: string; superseded_by_run_id?: string;
  input_tokens_estimated?: number; input_tokens_actual?: number | null; input_token_limit?: number;
  completed_count: number; task_count: number; waiting_count: number;
};
export type HarnessSnapshot = { runtime_mode: string; cursor: number; runs: HarnessRun[] };
export const isHarnessTerminal = (status: string) => ["completed", "failed", "cancelled"].includes(status);
