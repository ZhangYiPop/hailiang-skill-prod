export type HarnessNode = {
  id: string; parent_id: string | null; kind: "main" | "expert" | "skill" | "tool";
  title: string; actor?: string; status: string; started_at: string | null; ended_at: string | null;
  error?: string; attempts?: number; stale?: boolean;
};
export type HarnessField = {
  key: string; label: string; input_type: string; required: boolean;
  options: Array<{ label: string; value: string }>; min?: number; max?: number;
};
export type HarnessInput = {
  id: string; title: string; fields: HarnessField[]; status: string; version: number; round: number;
  actor?: string; owner_task_id?: string;
  bindings: Array<{ task_id: string; tool_id: string }>;
  answer?: { answers: Record<string, unknown>; skipped: boolean }; clarification_reason?: string;
};
export type HarnessRun = {
  run_id: string; session_id: string; profile_id: string | null; goal: string; status: string; version: number;
  root_task_id: string; nodes: HarnessNode[]; inputs: HarnessInput[];
  created_at: string; updated_at: string; result: string; phase?: string; error?: string;
  input_tokens_estimated?: number; input_tokens_actual?: number | null; input_token_limit?: number;
  completed_count: number; task_count: number; waiting_count: number;
};
export type HarnessSnapshot = { runtime_mode: string; cursor: number; runs: HarnessRun[] };
export const isHarnessTerminal = (status: string) => ["completed", "failed", "cancelled"].includes(status);
