import { normalizeBaseUrl } from "@/config/runtime";
import type { MessageBlock } from "@/types/messageBlocks";

export type WorkbenchObjectType = "skill" | "expert" | "expert_team";

export type WorkbenchActor = {
  actor_id: string;
  display_name: string;
  device_token: string;
};

export type DependencyLock = {
  object_id: string;
  object_type: WorkbenchObjectType;
  object_key: string;
  release_id: string;
  release_no: number;
  content_hash: string;
};

export type ObjectRevision = {
  revision_id: string;
  object_id: string;
  revision_no: number;
  base_revision_id: string | null;
  payload: Record<string, unknown>;
  dependency_locks: DependencyLock[];
  validation: { valid: boolean; errors?: string[]; warnings?: string[] };
  content_hash: string;
  created_by: string;
  created_at: string;
};

export type RevisionAsset = {
  asset_id?: string;
  revision_id?: string;
  relative_path: string;
  media_type: string;
  size_bytes: number;
  content_hash?: string;
  content_base64: string;
};

export type ObjectRelease = {
  is_current: boolean;
  release_id: string;
  object_id: string;
  object_type: WorkbenchObjectType;
  object_key: string;
  name: string;
  revision_id: string;
  release_no: number;
  version: string;
  dependency_locks: DependencyLock[];
  content_hash: string;
  archived: boolean;
  published_by: string;
  published_at: string;
};

export type WorkbenchObject = {
  current_release_id: string | null;
  object_id: string;
  object_type: WorkbenchObjectType;
  object_key: string;
  name: string;
  description: string;
  brief?: string;
  archived: boolean;
  latest_revision_no: number;
  latest_release_no: number;
  updated_at: string;
  revisions?: ObjectRevision[];
  releases?: ObjectRelease[];
};

export type IdMigrationResult = {
  source: WorkbenchObject;
  successor: WorkbenchObject;
  revision: ObjectRevision;
  source_release_id: string | null;
  downstream: Array<{
    object_id: string;
    object_type: WorkbenchObjectType;
    object_key: string;
    name: string;
    revision_id: string;
    revision_no: number;
  }>;
};

export type Deployment = {
  deployment_id: string;
  environment: string;
  root_release_id: string;
  package_hash: string;
  manifest: {
    root?: { object_key?: string; release_no?: number; object_type?: string; name?: string };
  };
  status: "staged" | "active" | "superseded" | "rolled_back" | "deactivated";
  expert_team_id?: string | null;
  expert_team_name?: string | null;
  previous_deployment_id?: string | null;
  imported_by: string;
  imported_at: string;
};

export type WorkbenchObjectImportResult = {
  root: {
    object_id?: string;
    object_type?: WorkbenchObjectType;
    object_key?: string;
    release_id?: string;
    release_no?: number;
  };
  package_hash: string;
  created_objects: number;
  created_revisions: number;
  created_releases: number;
  reused_objects: number;
  reused_revisions: number;
  reused_releases: number;
  unarchived_objects: number;
  entries: Array<{
    object_id: string;
    object_type: WorkbenchObjectType;
    object_key: string;
    name: string;
    source_release_id?: string;
    source_release_no?: number;
    local_revision_id: string;
    local_revision_no: number;
    local_release_id: string;
    local_release_no: number;
    status: string;
    unarchived: boolean;
  }>;
};

export type EvaluationSuite = {
  suite_id: string;
  object_id: string;
  name: string;
  cases: Array<{
    case_id: string;
    name: string;
    input?: string;
    turns?: Array<{ role: string; content: string }>;
  }>;
};

export type EvaluationRun = {
  run_id: string;
  suite_id: string;
  revision_id: string;
  status: string;
  manual_result: "accepted" | "rejected" | null;
  results: Array<Record<string, unknown>>;
};

export type RevisionTestSession = {
  debug_session_id: string;
  revision_id: string;
  baseline_release_id: string | null;
  transcript: Array<{
    role: "user" | "assistant";
    content: string;
    message_id?: string;
    message_type?: string;
    metadata?: Record<string, unknown>;
    blocks?: MessageBlock[];
    team_handoff?: import("@/utils/api").TeamHandoff | null;
    interaction_states?: Record<string, import("@/utils/api").MessageInteractionState>;
    presentation?: import("@/utils/api").MessagePresentation | Record<string, unknown>;
    created_at?: string;
  }>;
  trace: Array<Record<string, unknown>>;
  status: "active" | "completed";
  conclusion: string;
  created_at: string;
  completed_at: string | null;
};

export type SoulRevision = {
  soul_revision_id: string;
  revision_no: number;
  content: string;
  content_hash: string;
  enabled: boolean;
  created_by: string;
  created_at: string;
};

export type StandardSkillConversion = {
  source: { entry_path: string; files: string[]; metadata: Record<string, unknown> };
  draft: Record<string, unknown>;
  form_preview: Array<Record<string, unknown>>;
  warnings: string[];
  ai_assistance: { status: string; message: string };
};

export type DebugHistoryItem = RevisionTestSession & {
  object_id: string;
  object_type: WorkbenchObjectType;
  object_key: string;
  object_name: string;
  revision_no: number;
  release_id: string | null;
  release_version: string | null;
};

export type EvaluationHistoryItem = EvaluationRun & {
  suite_name: string;
  object_id: string;
  object_type: WorkbenchObjectType;
  object_key: string;
  object_name: string;
  revision_no: number;
  release_id: string | null;
  release_version: string | null;
  created_at: string;
  completed_at: string | null;
};

const ACTOR_KEY = "hailiang.workbench_actor";

export function readWorkbenchActor(): WorkbenchActor | null {
  try {
    const value = JSON.parse(
      localStorage.getItem(ACTOR_KEY) ?? "null",
    ) as WorkbenchActor | null;
    return value?.actor_id && value?.display_name ? value : null;
  } catch {
    return null;
  }
}

export function storeWorkbenchActor(actor: WorkbenchActor): void {
  localStorage.setItem(ACTOR_KEY, JSON.stringify(actor));
}

function errorMessageFromPayload(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const value = payload as {
    message?: unknown;
    detail?: unknown;
  };
  if (typeof value.message === "string" && value.message.trim()) {
    return value.message;
  }
  if (typeof value.detail === "string" && value.detail.trim()) {
    return value.detail;
  }
  if (value.detail && typeof value.detail === "object" && !Array.isArray(value.detail)) {
    const detail = value.detail as { message?: unknown };
    if (typeof detail.message === "string" && detail.message.trim()) {
      return detail.message;
    }
  }
  if (Array.isArray(value.detail)) {
    const errors = value.detail
      .slice(0, 3)
      .flatMap((item) => {
        if (!item || typeof item !== "object") return [];
        const error = item as { loc?: unknown; msg?: unknown };
        if (typeof error.msg !== "string" || !error.msg.trim()) return [];
        const location = Array.isArray(error.loc)
          ? error.loc.filter((part) => typeof part === "string" || typeof part === "number").join(".")
          : "";
        return [location ? `${location}: ${error.msg}` : error.msg];
      });
    if (errors.length) return errors.join("；");
  }
  return null;
}

async function responseError(response: Response, fallback: string): Promise<Error> {
  const payload = await response.clone().json().catch(() => null);
  const message = errorMessageFromPayload(payload);
  const rawBody = message ? "" : (await response.text().catch(() => "")).trim();
  const bodyMessage = rawBody
    ? rawBody.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 240)
    : "";
  const requestId = response.headers.get("X-Request-Id");
  const suffix = `（HTTP ${response.status}${requestId ? `，请求 ID：${requestId}` : ""}）`;
  return new Error(`${message ?? bodyMessage ?? fallback}${suffix}`);
}

async function request<T>(
  baseUrl: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`${normalizeBaseUrl(baseUrl)}${path}`, {
    ...init,
    headers: {
      ...(init?.body && !(init.body instanceof Blob)
        ? { "Content-Type": "application/json" }
        : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    const payload = await response.clone().json().catch(() => null) as {
      detail?: { details?: { blockers?: Array<{ message?: string }> } };
    } | null;
    const error = await responseError(response, "请求处理失败");
    const blockers = payload?.detail?.details?.blockers
      ?.map((item) => item.message)
      .filter(Boolean);
    if (blockers?.length) error.message = `${error.message}：${blockers.join("；")}`;
    throw error;
  }
  return response.json() as Promise<T>;
}

export async function streamWorkbenchSse(
  baseUrl: string,
  path: string,
  input: Record<string, unknown>,
  onEvent: (event: string, payload: Record<string, unknown>) => void,
  options: { signal?: AbortSignal } = {},
): Promise<void> {
  const response = await fetch(`${normalizeBaseUrl(baseUrl)}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(input),
    signal: options.signal,
  });
  if (!response.ok || !response.body) {
    throw await responseError(response, "流式请求未建立");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const event = frame.match(/^event:\s*(.+)$/m)?.[1]?.trim() ?? "message";
      const raw = frame.match(/^data:\s*(.*)$/m)?.[1] ?? "{}";
      try {
        onEvent(event, JSON.parse(raw) as Record<string, unknown>);
      } catch {
        onEvent("error", { code: "REVISION_TEST_STREAM_PARSE_FAILED", message: "候选测试流式响应格式错误" });
      }
    }
    if (done) break;
  }
}

export const workbenchApi = {
  registerActor: (baseUrl: string, displayName: string, deviceToken?: string) =>
    request<WorkbenchActor>(baseUrl, "/workbench/v1/actors", {
      method: "POST",
      body: JSON.stringify({
        display_name: displayName,
        device_token: deviceToken || null,
      }),
    }),
  listObjects: (baseUrl: string, includeArchived = false) =>
    request<{ objects: WorkbenchObject[] }>(
      baseUrl,
      `/workbench/v1/objects?include_archived=${includeArchived ? "true" : "false"}`,
    ),
  getObject: (baseUrl: string, objectId: string) =>
    request<WorkbenchObject>(baseUrl, `/workbench/v1/objects/${objectId}`),
  listRevisionAssets: (baseUrl: string, revisionId: string) =>
    request<{ assets: RevisionAsset[] }>(
      baseUrl,
      `/workbench/v1/revisions/${revisionId}/assets`,
    ),
  createObject: (baseUrl: string, input: Record<string, unknown>) =>
    request<WorkbenchObject>(baseUrl, "/workbench/v1/objects", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  archiveObject: (baseUrl: string, objectId: string, actorId: string) =>
    request<{ object_id: string; archived: true }>(
      baseUrl,
      `/workbench/v1/objects/${objectId}/archive`,
      { method: "POST", body: JSON.stringify({ actor_id: actorId }) },
    ),
  unarchiveObject: (baseUrl: string, objectId: string, actorId: string) =>
    request<{ object_id: string; archived: false }>(
      baseUrl,
      `/workbench/v1/objects/${objectId}/unarchive`,
      { method: "POST", body: JSON.stringify({ actor_id: actorId }) },
    ),
  deleteObject: (
    baseUrl: string,
    objectId: string,
    input: { confirmation_name: string; actor_id: string },
  ) =>
    request<{
      object_id: string;
      name: string;
      revision_count: number;
      release_count: number;
      asset_count: number;
      permanent: true;
    }>(baseUrl, `/workbench/v1/objects/${objectId}`, {
      method: "DELETE",
      body: JSON.stringify(input),
    }),
  migrateObjectId: (
    baseUrl: string,
    objectId: string,
    input: { new_object_key: string; confirmation_name: string; actor_id: string },
  ) => request<IdMigrationResult>(baseUrl, `/workbench/v1/objects/${objectId}/id-migrations`, {
    method: "POST",
    body: JSON.stringify(input),
  }),
  migrateReferences: (
    baseUrl: string,
    input: { from_release_id: string; to_release_id: string; confirmation_name: string; actor_id: string },
  ) => request<{ created: Array<{ object: WorkbenchObject; revision: ObjectRevision }> }>(baseUrl, "/workbench/v1/reference-migrations", {
    method: "POST",
    body: JSON.stringify(input),
  }),
  saveRevision: (
    baseUrl: string,
    objectId: string,
    input: Record<string, unknown>,
  ) =>
    request<ObjectRevision>(
      baseUrl,
      `/workbench/v1/objects/${objectId}/revisions`,
      {
        method: "POST",
        body: JSON.stringify(input),
      },
    ),
  listReleases: (baseUrl: string) =>
    request<{ releases: ObjectRelease[] }>(baseUrl, "/workbench/v1/releases"),
  createDebugSession: (baseUrl: string, input: Record<string, unknown>) =>
    request<{ debug_session_id: string; status: string }>(
      baseUrl,
      "/workbench/v1/debug-sessions",
      {
        method: "POST",
        body: JSON.stringify(input),
      },
    ),
  createRevisionTestSession: (baseUrl: string, input: Record<string, unknown>) =>
    request<RevisionTestSession>(baseUrl, "/workbench/v1/revision-tests", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  listSoulRevisions: (baseUrl: string) =>
    request<{ revisions: SoulRevision[] }>(baseUrl, "/workbench/v1/soul-revisions"),
  saveSoulRevision: (baseUrl: string, input: Record<string, unknown>) =>
    request<SoulRevision>(baseUrl, "/workbench/v1/soul-revisions", { method: "POST", body: JSON.stringify(input) }),
  previewStandardSkillConversion: async (baseUrl: string, file: File, actorId: string) => {
    const response = await fetch(`${normalizeBaseUrl(baseUrl)}/workbench/v1/standard-skill-conversions/preview?actor_id=${encodeURIComponent(actorId)}`, { method: "POST", body: file });
    if (!response.ok) throw await responseError(response, "标准 Skill 转换失败");
    return response.json() as Promise<StandardSkillConversion>;
  },
  commitStandardSkillConversion: (baseUrl: string, input: Record<string, unknown>) =>
    request<{ object_id: string; revision: ObjectRevision }>(baseUrl, "/workbench/v1/standard-skill-conversions/commit", { method: "POST", body: JSON.stringify(input) }),
  runRevisionTestTurn: (
    baseUrl: string,
    debugSessionId: string,
    input: Record<string, unknown>,
  ) =>
    request<{
      assistant_message: string;
      trace: Array<Record<string, unknown>>;
      form: { type: "fact_form"; payload: { form_id?: string; title?: string; fields?: Array<Record<string, unknown>> } } | null;
      assistant_blocks: MessageBlock[];
      debug_session: RevisionTestSession;
    }>(baseUrl, `/workbench/v1/revision-tests/${debugSessionId}/turns`, {
      method: "POST",
      body: JSON.stringify(input),
    }),
  streamRevisionTestTurn: (
    baseUrl: string,
    debugSessionId: string,
    input: Record<string, unknown>,
    onEvent: (event: string, payload: Record<string, unknown>) => void,
    options?: { signal?: AbortSignal },
  ) => streamWorkbenchSse(baseUrl, `/workbench/v1/revision-tests/${debugSessionId}/turns/stream`, input, onEvent, options),
  stopRevisionTestTurn: (baseUrl: string, debugSessionId: string, runId: string, actorId: string) =>
    request<{ run_id: string; status: "stopped"; state: Record<string, unknown> | null }>(
      baseUrl,
      `/workbench/v1/revision-tests/${debugSessionId}/turns/${encodeURIComponent(runId)}/stop`,
      { method: "POST", body: JSON.stringify({ actor_id: actorId }) },
    ),
  createFormalTeamChatSession: (baseUrl: string, input: Record<string, unknown>) =>
    request<RevisionTestSession>(
      baseUrl,
      "/workbench/v1/formal-team-chat-sessions",
      { method: "POST", body: JSON.stringify(input) },
    ),
  createFormalChatSession: (baseUrl: string, input: Record<string, unknown>) =>
    request<RevisionTestSession>(baseUrl, "/workbench/v1/formal-chat-sessions", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  completeDebugSession: (
    baseUrl: string,
    debugSessionId: string,
    input: Record<string, unknown>,
  ) =>
    request<{ debug_session_id: string; status: string }>(
      baseUrl,
      `/workbench/v1/debug-sessions/${debugSessionId}/complete`,
      {
        method: "POST",
        body: JSON.stringify(input),
      },
    ),
  listDebugSessions: (
    baseUrl: string,
    filters: { object_id?: string; revision_id?: string; status?: string } = {},
  ) => {
    const search = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => value && search.set(key, value));
    return request<{ sessions: DebugHistoryItem[] }>(
      baseUrl,
      `/workbench/v1/debug-sessions${search.size ? `?${search}` : ""}`,
    );
  },
  listEvaluationSuites: (baseUrl: string, objectId?: string) =>
    request<{ suites: EvaluationSuite[] }>(
      baseUrl,
      `/workbench/v1/evaluation-suites${objectId ? `?object_id=${encodeURIComponent(objectId)}` : ""}`,
    ),
  createEvaluationSuite: (baseUrl: string, input: Record<string, unknown>) =>
    request<EvaluationSuite>(baseUrl, "/workbench/v1/evaluation-suites", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  createEvaluationRun: (baseUrl: string, input: Record<string, unknown>) =>
    request<EvaluationRun>(baseUrl, "/workbench/v1/evaluation-runs", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  completeEvaluationRun: (
    baseUrl: string,
    runId: string,
    input: Record<string, unknown>,
  ) =>
    request<EvaluationRun>(
      baseUrl,
      `/workbench/v1/evaluation-runs/${runId}/complete`,
      { method: "POST", body: JSON.stringify(input) },
    ),
  listEvaluationRuns: (
    baseUrl: string,
    filters: { object_id?: string; revision_id?: string; status?: string } = {},
  ) => {
    const search = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => value && search.set(key, value));
    return request<{ runs: EvaluationHistoryItem[] }>(
      baseUrl,
      `/workbench/v1/evaluation-runs${search.size ? `?${search}` : ""}`,
    );
  },
  publish: (baseUrl: string, input: Record<string, unknown>) =>
    request<ObjectRelease>(baseUrl, "/workbench/v1/releases", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  listAudit: (baseUrl: string) =>
    request<{
      events: Array<{
        event_id: string;
        action: string;
        actor_id: string;
        created_at: string;
      }>;
    }>(baseUrl, "/workbench/v1/audit?limit=20"),
  listDeployments: (baseUrl: string) =>
    request<{ deployments: Deployment[] }>(
      baseUrl,
      "/deployment/v1/deployments",
    ),
  activateDeployment: (
    baseUrl: string,
    deploymentId: string,
    actorId: string,
  ) =>
    request<Deployment>(
      baseUrl,
      `/deployment/v1/deployments/${deploymentId}/activate`,
      {
        method: "POST",
        body: JSON.stringify({ actor_id: actorId }),
      },
    ),
  rollbackDeployment: (
    baseUrl: string,
    deploymentId: string,
    actorId: string,
  ) =>
    request<Deployment>(
      baseUrl,
      `/deployment/v1/deployments/${deploymentId}/rollback`,
      {
        method: "POST",
        body: JSON.stringify({ actor_id: actorId }),
      },
    ),
  deactivateDeployment: (
    baseUrl: string,
    deploymentId: string,
    expertTeamId: string,
    actorId: string,
  ) => request<Deployment>(baseUrl, `/deployment/v1/deployments/${deploymentId}/deactivate`, {
    method: "POST",
    body: JSON.stringify({ expert_team_id: expertTeamId, actor_id: actorId }),
  }),
  restoreDeployment: (baseUrl: string, deploymentId: string, actorId: string) =>
    request<Deployment>(baseUrl, `/deployment/v1/deployments/${deploymentId}/restore`, {
      method: "POST",
      body: JSON.stringify({ actor_id: actorId }),
    }),
  async exportRelease(
    baseUrl: string,
    releaseId: string,
    actorId: string,
  ): Promise<Blob> {
    return this.downloadRelease(baseUrl, releaseId, actorId);
  },
  makeCurrent: (baseUrl: string, releaseId: string, currentId: string | null, actorId: string) =>
    request<ObjectRelease>(baseUrl, `/workbench/v1/releases/${releaseId}/make-current`, {
      method: "POST", body: JSON.stringify({expected_current_release_id: currentId, actor_id: actorId}),
    }),
  draftFromRelease: (baseUrl: string, releaseId: string, actorId: string) =>
    request<ObjectRevision>(baseUrl, `/workbench/v1/releases/${releaseId}/drafts`, {
      method: "POST", body: JSON.stringify({actor_id: actorId}),
    }),
  async downloadRelease(
    baseUrl: string,
    releaseId: string,
    actorId: string,
  ): Promise<Blob> {
    const response = await fetch(
      `${normalizeBaseUrl(baseUrl)}/workbench/v1/exports`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ release_id: releaseId, actor_id: actorId }),
      },
    );
    if (!response.ok) throw await responseError(response, "配置包导出失败");
    return response.blob();
  },
  async importPackage(
    baseUrl: string,
    file: File,
    actorId: string,
  ): Promise<Deployment> {
    const response = await fetch(
      `${normalizeBaseUrl(baseUrl)}/deployment/v1/imports?actor_id=${encodeURIComponent(actorId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/zip" },
        body: file,
      },
    );
    if (!response.ok) {
      throw await responseError(response, "配置包导入失败");
    }
    return response.json() as Promise<Deployment>;
  },
  async importObjectPackage(
    baseUrl: string,
    file: File,
    actorId: string,
  ): Promise<WorkbenchObjectImportResult> {
    const response = await fetch(
      `${normalizeBaseUrl(baseUrl)}/workbench/v1/imports?actor_id=${encodeURIComponent(actorId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/zip" },
        body: file,
      },
    );
    if (!response.ok) {
      throw await responseError(response, "工作台对象导入失败");
    }
    return response.json() as Promise<WorkbenchObjectImportResult>;
  },
};
